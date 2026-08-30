"""A minimal DNS client built on raw sockets.

Only what ReachOut needs: A, MX and TXT lookups, with UDP -> TCP fallback when a
response is truncated. Written against the wire format directly so the tool has
zero third-party dependencies.
"""

from __future__ import annotations

import random
import socket
import struct
import threading
import time
from dataclasses import dataclass

from . import config

TYPE_A = 1
TYPE_NS = 2
TYPE_CNAME = 5
TYPE_MX = 15
TYPE_TXT = 16

RCODE_OK = 0
RCODE_FORMERR = 1
RCODE_SERVFAIL = 2
RCODE_NXDOMAIN = 3
RCODE_REFUSED = 5

_TYPE_NAMES = {TYPE_A: "A", TYPE_NS: "NS", TYPE_CNAME: "CNAME", TYPE_MX: "MX", TYPE_TXT: "TXT"}


class DNSError(Exception):
    pass


@dataclass
class Answer:
    rcode: int
    records: list  # list[tuple[int_type, value]] - MX values are (pref, host)
    error: str = ""

    @property
    def ok(self) -> bool:
        return self.rcode == RCODE_OK

    @property
    def nxdomain(self) -> bool:
        return self.rcode == RCODE_NXDOMAIN


def _nameservers() -> list[str]:
    servers: list[str] = []
    try:
        with open("/etc/resolv.conf", "r", encoding="utf-8", errors="replace") as handle:
            for line in handle:
                line = line.strip()
                if line.startswith("nameserver"):
                    bits = line.split()
                    if len(bits) > 1 and ":" not in bits[1]:
                        servers.append(bits[1])
    except OSError:
        pass
    for fallback in ("1.1.1.1", "8.8.8.8"):
        if fallback not in servers:
            servers.append(fallback)
    return servers


_SERVERS = _nameservers()


def _encode_name(name: str) -> bytes:
    out = b""
    for label in name.rstrip(".").split("."):
        if not label:
            continue
        try:
            raw = label.encode("idna") if any(ord(c) > 127 for c in label) else label.encode("ascii")
        except UnicodeError as exc:
            raise DNSError(f"bad label {label!r}") from exc
        if len(raw) > 63:
            raise DNSError(f"label too long: {label!r}")
        out += bytes([len(raw)]) + raw
    return out + b"\x00"


def _parse_name(data: bytes, offset: int) -> tuple[str, int]:
    labels: list[str] = []
    end = offset
    jumped = False
    hops = 0
    while True:
        if offset >= len(data):
            raise DNSError("truncated name")
        length = data[offset]
        if length & 0xC0 == 0xC0:
            if offset + 1 >= len(data):
                raise DNSError("truncated pointer")
            pointer = struct.unpack(">H", data[offset:offset + 2])[0] & 0x3FFF
            if not jumped:
                end = offset + 2
            offset = pointer
            jumped = True
            hops += 1
            if hops > 32:
                raise DNSError("compression loop")
            continue
        offset += 1
        if length == 0:
            if not jumped:
                end = offset
            break
        labels.append(data[offset:offset + length].decode("ascii", "replace"))
        offset += length
    return ".".join(labels), end


def _decode_rdata(rtype: int, data: bytes, start: int, length: int):
    if rtype == TYPE_A and length == 4:
        return ".".join(str(b) for b in data[start:start + 4])
    if rtype == TYPE_MX:
        pref = struct.unpack(">H", data[start:start + 2])[0]
        host, _ = _parse_name(data, start + 2)
        return (pref, host.lower())
    if rtype in (TYPE_NS, TYPE_CNAME):
        host, _ = _parse_name(data, start)
        return host.lower()
    if rtype == TYPE_TXT:
        chunks = []
        pos = start
        while pos < start + length:
            clen = data[pos]
            chunks.append(data[pos + 1:pos + 1 + clen].decode("utf-8", "replace"))
            pos += 1 + clen
        return "".join(chunks)
    return data[start:start + length]


def _parse_response(data: bytes, want_id: int) -> tuple[int, bool, list]:
    if len(data) < 12:
        raise DNSError("short response")
    tid, flags, qdcount, ancount, _nscount, _arcount = struct.unpack(">HHHHHH", data[:12])
    if tid != want_id:
        raise DNSError("transaction id mismatch")
    truncated = bool(flags & 0x0200)
    rcode = flags & 0x000F
    offset = 12
    for _ in range(qdcount):
        _, offset = _parse_name(data, offset)
        offset += 4
    records = []
    for _ in range(ancount):
        _, offset = _parse_name(data, offset)
        if offset + 10 > len(data):
            break
        rtype, _rclass, _ttl, rdlength = struct.unpack(">HHIH", data[offset:offset + 10])
        offset += 10
        try:
            value = _decode_rdata(rtype, data, offset, rdlength)
        except DNSError:
            value = None
        if value is not None:
            records.append((rtype, value))
        offset += rdlength
    return rcode, truncated, records


def _query_udp(server: str, payload: bytes, want_id: int, timeout: float):
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.settimeout(timeout)
    try:
        sock.sendto(payload, (server, 53))
        data, _ = sock.recvfrom(4096)
    finally:
        sock.close()
    return _parse_response(data, want_id)


def _query_tcp(server: str, payload: bytes, want_id: int, timeout: float):
    with socket.create_connection((server, 53), timeout=timeout) as sock:
        sock.sendall(struct.pack(">H", len(payload)) + payload)
        header = b""
        while len(header) < 2:
            chunk = sock.recv(2 - len(header))
            if not chunk:
                raise DNSError("tcp closed")
            header += chunk
        want = struct.unpack(">H", header)[0]
        body = b""
        while len(body) < want:
            chunk = sock.recv(want - len(body))
            if not chunk:
                raise DNSError("tcp closed early")
            body += chunk
    return _parse_response(body, want_id)


_cache: dict[tuple[str, int], tuple[float, Answer]] = {}
_cache_lock = threading.Lock()


def query(name: str, rtype: int, timeout: float | None = None, use_cache: bool = True) -> Answer:
    """Resolve `name` for `rtype`, trying each configured nameserver in turn."""
    name = (name or "").strip().rstrip(".").lower()
    if not name:
        return Answer(RCODE_FORMERR, [], "empty name")
    key = (name, rtype)
    now = time.time()
    if use_cache:
        with _cache_lock:
            hit = _cache.get(key)
            if hit and hit[0] > now:
                return hit[1]

    timeout = timeout if timeout is not None else config.DNS_TIMEOUT
    try:
        question = _encode_name(name)
    except DNSError as exc:
        return Answer(RCODE_FORMERR, [], str(exc))

    last_error = "no nameserver responded"
    answer: Answer | None = None
    for server in _SERVERS:
        tid = random.randrange(0, 65536)
        payload = struct.pack(">HHHHHH", tid, 0x0100, 1, 0, 0, 0) + question + struct.pack(">HH", rtype, 1)
        try:
            rcode, truncated, records = _query_udp(server, payload, tid, timeout)
            if truncated:
                rcode, _, records = _query_tcp(server, payload, tid, timeout)
        except (OSError, DNSError) as exc:
            last_error = f"{server}: {exc}"
            continue
        if rcode in (RCODE_SERVFAIL, RCODE_REFUSED):
            last_error = f"{server}: rcode={rcode}"
            continue
        answer = Answer(rcode, [value for rt, value in records if rt == rtype])
        break

    if answer is None:
        return Answer(RCODE_SERVFAIL, [], last_error)

    if use_cache:
        with _cache_lock:
            _cache[key] = (now + config.DNS_CACHE_TTL, answer)
    return answer


def mx(domain: str) -> list[tuple[int, str]]:
    """MX hosts sorted by preference (lowest first)."""
    answer = query(domain, TYPE_MX)
    return sorted(answer.records) if answer.ok else []


def a(domain: str) -> list[str]:
    answer = query(domain, TYPE_A)
    return answer.records if answer.ok else []


def txt(domain: str) -> list[str]:
    answer = query(domain, TYPE_TXT)
    return answer.records if answer.ok else []


def domain_exists(domain: str) -> bool:
    """True when the domain resolves at all (A or MX)."""
    if mx(domain):
        return True
    return bool(a(domain))


# Mail-host fingerprints -> provider. Provider identity is a strong hint about
# whether SMTP probing will tell you anything useful (Google/Microsoft: it won't).
_PROVIDER_SIGNS = (
    ("google", "Google Workspace"),
    ("googlemail", "Google Workspace"),
    ("outlook.com", "Microsoft 365"),
    ("protection.outlook", "Microsoft 365"),
    ("microsoft", "Microsoft 365"),
    ("pphosted", "Proofpoint"),
    ("ppe-hosted", "Proofpoint"),
    ("mimecast", "Mimecast"),
    ("barracuda", "Barracuda"),
    ("zoho", "Zoho Mail"),
    ("yandex", "Yandex"),
    ("qq.com", "Tencent"),
    ("mailgun", "Mailgun"),
    ("sendgrid", "SendGrid"),
    ("amazonaws", "Amazon SES"),
    ("secureserver.net", "GoDaddy"),
    ("registrar-servers", "Namecheap"),
    ("zimbra", "Zimbra"),
    ("titan.email", "Titan"),
    # Indian market. Zoho above is the largest of these and is Chennai-based;
    # the rest carry most of the country's SME mail. Worth naming individually
    # rather than falling through to "unknown", because the provider decides
    # whether an SMTP probe means anything - and because a domain on Titan,
    # MailHostbox or Rediffmail is nearly always a small organisation, whose
    # naming pattern is correspondingly simple.
    ("mailhostbox", "MailHostbox"),        # Directi/Newfold platform behind
    ("hostbox", "MailHostbox"),            # BigRock, ResellerClub, HostGator IN
    ("bigrock", "BigRock"),
    ("rediffmailpro", "Rediffmail Enterprise"),
    ("rediffmail", "Rediffmail Enterprise"),
    ("hostinger", "Hostinger"),
    ("netcore", "Netcore"),
    ("pepipost", "Netcore"),
    ("hostgator.in", "HostGator India"),
    ("bluehost.in", "Bluehost India"),
    ("improvmx", "ImprovMX"),
    ("fastmail", "Fastmail"),
    ("icloud", "iCloud"),
    ("proton", "Proton Mail"),
)

# Providers that accept mail for any address at the domain-gateway level, so an
# SMTP RCPT probe cannot distinguish a real mailbox from a made-up one.
OPAQUE_PROVIDERS = {"Google Workspace", "Microsoft 365", "Proofpoint", "Mimecast", "Barracuda"}


def provider_for(hosts: list[tuple[int, str]]) -> str:
    for _pref, host in hosts:
        for needle, label in _PROVIDER_SIGNS:
            if needle in host:
                return label
    return ""


def describe(domain: str) -> dict:
    """One call that gathers every DNS fact ReachOut scores on."""
    hosts = mx(domain)
    addresses = a(domain) if not hosts else []
    provider = provider_for(hosts)
    spf = next((t for t in txt(domain) if t.lower().startswith("v=spf1")), "")
    return {
        "domain": domain,
        "mx": [f"{p} {h}" for p, h in hosts],
        "has_mx": bool(hosts),
        "resolves": bool(hosts or addresses),
        "provider": provider,
        "spf": spf,
        "smtp_probe_useful": bool(hosts) and provider not in OPAQUE_PROVIDERS,
    }
