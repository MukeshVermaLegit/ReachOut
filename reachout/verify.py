"""Email verification, honestly scoped.

There is no free way to prove an address is deliverable. What this module does
is gather cheap, real signals - syntax, DNS, provider type, role/disposable
detection, and (opt-in) an SMTP conversation - and report exactly which ones
passed. It never returns "verified".
"""

from __future__ import annotations

import random
import re
import smtplib
import socket
import string
from dataclasses import dataclass, field

from . import config, netdns
from .sources.website import ROLE_LOCALS

# RFC 5321 is wider than this; these are the rules real mailboxes actually follow.
_LOCAL_RE = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9._%+\-]{0,62}[A-Za-z0-9])?$")
_DOMAIN_RE = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9\-]{0,61}[A-Za-z0-9])?"
                        r"(?:\.[A-Za-z0-9](?:[A-Za-z0-9\-]{0,61}[A-Za-z0-9])?)+$")

FREE_PROVIDERS = {
    "gmail.com", "googlemail.com", "yahoo.com", "yahoo.co.in", "yahoo.co.uk", "ymail.com",
    "hotmail.com", "outlook.com", "live.com", "msn.com", "aol.com", "icloud.com", "me.com",
    "mac.com", "mail.com", "gmx.com", "gmx.de", "protonmail.com", "proton.me", "pm.me",
    "zoho.com", "yandex.com", "yandex.ru", "rediffmail.com", "tutanota.com", "fastmail.com",
    "hey.com", "qq.com", "163.com", "126.com", "naver.com", "hotmail.co.uk", "outlook.in",
}

DISPOSABLE = {
    "mailinator.com", "guerrillamail.com", "10minutemail.com", "tempmail.com", "temp-mail.org",
    "throwawaymail.com", "yopmail.com", "sharklasers.com", "trashmail.com", "dispostable.com",
    "getnada.com", "maildrop.cc", "fakeinbox.com", "mailnesia.com", "spamgourmet.com",
    "moakt.com", "emailondeck.com", "burnermail.io", "mintemail.com",
}


@dataclass
class Verdict:
    address: str
    syntax_ok: bool = False
    domain_resolves: bool = False
    has_mx: bool = False
    provider: str = ""
    is_role: bool = False
    is_free: bool = False
    is_disposable: bool = False
    catch_all: int = -1          # -1 unknown, 0 no, 1 yes
    smtp: str = "not-checked"    # accepted | rejected | unknown | blocked | not-checked
    smtp_detail: str = ""
    notes: list = field(default_factory=list)

    @property
    def fatal(self) -> bool:
        return not self.syntax_ok or self.is_disposable or self.smtp == "rejected"


def split(address: str) -> tuple[str, str]:
    address = (address or "").strip().lower()
    local, _, domain = address.rpartition("@")
    return local, domain


def check_syntax(address: str) -> bool:
    local, domain = split(address)
    if not local or not domain or len(address) > 254 or len(local) > 64:
        return False
    if ".." in local or local.startswith(".") or local.endswith("."):
        return False
    return bool(_LOCAL_RE.match(local) and _DOMAIN_RE.match(domain))


def _random_local() -> str:
    alphabet = string.ascii_lowercase
    return "zz" + "".join(random.choice(alphabet) for _ in range(12))


def smtp_probe(address: str, mx_host: str, sender: str | None = None,
               timeout: float | None = None) -> tuple[str, str]:
    """One SMTP RCPT TO conversation. Returns (result, detail).

    Requires outbound port 25, which most home ISPs and cloud providers block.
    """
    sender = sender or config.SMTP_FROM
    timeout = timeout if timeout is not None else config.SMTP_TIMEOUT
    server = None
    try:
        server = smtplib.SMTP(timeout=timeout)
        server.connect(mx_host, 25)
        server.ehlo_or_helo_if_needed()
        server.mail(sender)
        code, message = server.rcpt(address)
        text = message.decode("utf-8", "replace") if isinstance(message, bytes) else str(message)
        if code in (250, 251):
            return "accepted", f"{code} {text[:120]}"
        if code in (550, 551, 553, 554, 501):
            return "rejected", f"{code} {text[:120]}"
        return "unknown", f"{code} {text[:120]}"
    except (socket.timeout, TimeoutError):
        return "blocked", "timed out - outbound port 25 is probably blocked by your network"
    except (OSError, smtplib.SMTPException) as exc:
        return "blocked", f"{type(exc).__name__}: {str(exc)[:120]}"
    finally:
        if server is not None:
            try:
                server.quit()
            except Exception:
                try:
                    server.close()
                except Exception:
                    pass


def detect_catch_all(domain: str, mx_host: str) -> tuple[int, str]:
    """Ask the server about an address that cannot exist.

    If it says yes, the domain accepts everything and SMTP tells you nothing.
    """
    probe = f"{_random_local()}@{domain}"
    result, detail = smtp_probe(probe, mx_host)
    if result == "accepted":
        return 1, "server accepted a random address, so it is catch-all"
    if result == "rejected":
        return 0, "server rejected a random address, so RCPT replies are meaningful"
    return -1, detail


def verify(address: str, db=None, use_smtp: bool | None = None) -> Verdict:
    address = (address or "").strip().lower()
    verdict = Verdict(address=address)
    verdict.syntax_ok = check_syntax(address)
    if not verdict.syntax_ok:
        verdict.notes.append("address is not syntactically valid")
        return verdict

    local, domain = split(address)
    verdict.is_role = local in ROLE_LOCALS or local.replace(".", "").replace("-", "") in ROLE_LOCALS
    verdict.is_free = domain in FREE_PROVIDERS
    verdict.is_disposable = domain in DISPOSABLE

    facts = db.get_domain_facts(domain) if db else None
    if not facts:
        facts = netdns.describe(domain)
        if db:
            db.save_domain_facts(facts)
    verdict.domain_resolves = bool(facts["resolves"])
    verdict.has_mx = bool(facts["has_mx"])
    verdict.provider = facts.get("provider", "")
    if facts.get("catch_all") is not None:
        verdict.catch_all = facts.get("catch_all", -1)

    if not verdict.has_mx:
        verdict.notes.append("domain has no MX records, so it cannot receive mail")
        return verdict
    if verdict.is_role:
        verdict.notes.append("this is a shared/role mailbox, not one person")
    if verdict.is_disposable:
        verdict.notes.append("disposable-mail domain")
    if verdict.provider:
        verdict.notes.append(f"mail hosted by {verdict.provider}")

    should_smtp = config.SMTP_ENABLED if use_smtp is None else use_smtp
    if not should_smtp:
        return verdict

    hosts = netdns.mx(domain)
    if not hosts:
        return verdict
    mx_host = hosts[0][1]

    if verdict.provider in netdns.OPAQUE_PROVIDERS:
        verdict.smtp = "unknown"
        verdict.smtp_detail = (f"{verdict.provider} accepts mail for any address at the gateway, "
                               "so an SMTP probe cannot confirm a mailbox")
        return verdict

    if verdict.catch_all == -1:
        verdict.catch_all, detail = detect_catch_all(domain, mx_host)
        if db:
            db.set_catch_all(domain, verdict.catch_all)
        if detail:
            verdict.notes.append(detail)

    if verdict.catch_all == 1:
        verdict.smtp = "unknown"
        verdict.smtp_detail = "domain is catch-all; acceptance proves nothing"
        return verdict

    verdict.smtp, verdict.smtp_detail = smtp_probe(address, mx_host)
    return verdict
