"""A small, polite HTTP client.

Rules it enforces so the tool stays a good citizen:
  * honours robots.txt (can be disabled, but is on by default)
  * one request per host at a time, with a configurable delay between them
  * caches every response on disk so re-running a search costs nothing
  * hard cap on response size, and a real User-Agent that identifies the tool
"""

from __future__ import annotations

import gzip
import hashlib
import json
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import urllib.robotparser
import zlib
from dataclasses import dataclass

# ---------------------------------------------------------------- source policy
# These live here, in the leaf module, because both the phone path and the email
# path have to honour them. They used to sit in `providers`, which imports
# `sources`, so `sources` could not import them back - and the email path
# consequently had no broker filter at all. A lookup for a name at a large
# employer was citing contactout.com as a published source at HIGH confidence,
# which is exactly the data this tool promises never to report.
#
# Data brokers and people-search sites. Everything they hold was scraped,
# purchased or inferred, so unlike the social networks below their *snippets*
# are off limits too, not just their pages.
BROKER_HOSTS = {
    "zoominfo.com", "apollo.io", "rocketreach.co", "signalhire.com", "lusha.com",
    "contactout.com", "leadiq.com", "seamless.ai", "uplead.com", "snov.io",
    "adapt.io", "getprospect.com", "cufinder.io", "kaspr.io", "wiza.co",
    "spokeo.com", "whitepages.com", "beenverified.com", "truepeoplesearch.com",
    "fastpeoplesearch.com", "thatsthem.com", "usphonebook.com", "radaris.com",
    "peoplefinders.com", "intelius.com", "anywho.com", "411.com", "zabasearch.com",
    "truthfinder.com", "instantcheckmate.com", "searchpeoplefree.com",
}

# Anonymous PDF re-upload mirrors, where scraped contact lists get parked.
DUMP_HOSTS = {
    "kupdf.net", "pdfcoffee.com", "dokumen.pub", "idoc.pub", "vdoc.pub",
    "vdocuments.net", "vdocuments.mx", "vdocuments.site", "fdocuments.in",
    "fdocuments.net", "dokumen.tips", "pdfslide.net", "pdfslide.tips",
    "cupdf.com", "documents.pub", "pdffox.com", "anyflip.com",
}


def host_of(url: str) -> str:
    host = urllib.parse.urlsplit(url).netloc.lower().split(":")[0]
    return host[4:] if host.startswith("www.") else host


def _matches(host: str, blocked: set) -> bool:
    """True for the host itself and any subdomain of it."""
    return any(host == b or host.endswith("." + b) for b in blocked)


def disallowed_source(url: str) -> str:
    """'' when this URL may be used as evidence at all, else why not.

    Stronger than the robots/terms check: here it is the *data* that is out of
    bounds, so the snippet is refused along with the page.
    """
    host = host_of(url)
    if _matches(host, BROKER_HOSTS):
        return f"{host} is a data broker; its records are scraped, bought or inferred"
    if _matches(host, DUMP_HOSTS):
        return f"{host} is an anonymous document mirror"
    return ""

from . import config


@dataclass
class Response:
    url: str
    status: int
    text: str
    from_cache: bool = False
    error: str = ""

    @property
    def ok(self) -> bool:
        # Any 2xx. Insisting on exactly 200 made DuckDuckGo's 202 look like a
        # transport failure, which hid the more useful fact that it had served
        # a bot-check page instead of results.
        return 200 <= self.status < 300 and bool(self.text)


_host_lock = threading.Lock()
_last_hit: dict[str, float] = {}
_robots: dict[str, urllib.robotparser.RobotFileParser | None] = {}
_robots_lock = threading.Lock()


def _throttle(host: str) -> None:
    while True:
        with _host_lock:
            now = time.monotonic()
            previous = _last_hit.get(host, 0.0)
            wait = config.PER_HOST_DELAY - (now - previous)
            if wait <= 0:
                _last_hit[host] = now
                return
        time.sleep(min(wait, config.PER_HOST_DELAY))


def _cache_path(key: str):
    digest = hashlib.sha256(key.encode("utf-8")).hexdigest()
    return config.CACHE_DIR / digest[:2] / f"{digest}.json"


def _cache_key(url: str, data: bytes | None) -> str:
    """What identifies a request on disk.

    For a GET that is the URL. For a POST it cannot be: two searches for
    different people are the same URL and differ only in the body, so keying on
    the URL alone would serve the first answer to every later query.
    """
    if data is None:
        return url
    return f"{url}\n{hashlib.sha256(data).hexdigest()}"


def _cache_read(key: str) -> Response | None:
    path = _cache_path(key)
    if not path.exists():
        return None
    try:
        if (time.time() - path.stat().st_mtime) > config.HTTP_CACHE_TTL:
            return None
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return Response(url=payload["url"], status=payload["status"], text=payload["text"],
                    from_cache=True, error=payload.get("error", ""))


def _cache_write(response: Response, key: str = "") -> None:
    path = _cache_path(key or response.url)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({
            "url": response.url, "status": response.status,
            "text": response.text, "error": response.error,
        }), encoding="utf-8")
    except OSError:
        pass


def robots_allows(url: str) -> bool:
    if not config.RESPECT_ROBOTS:
        return True
    parts = urllib.parse.urlsplit(url)
    origin = f"{parts.scheme}://{parts.netloc}"
    with _robots_lock:
        parser = _robots.get(origin, "missing")
    if parser == "missing":
        parser = urllib.robotparser.RobotFileParser()
        parser.set_url(f"{origin}/robots.txt")
        try:
            _throttle(parts.netloc)
            request = urllib.request.Request(
                f"{origin}/robots.txt", headers={"User-Agent": config.USER_AGENT})
            with urllib.request.urlopen(request, timeout=config.HTTP_TIMEOUT) as handle:
                parser.parse(handle.read(200_000).decode("utf-8", "replace").splitlines())
        except Exception:
            # No robots.txt (or unreachable) means no stated restriction.
            parser = None
        with _robots_lock:
            _robots[origin] = parser
    if parser is None:
        return True
    try:
        return parser.can_fetch(config.USER_AGENT, url)
    except Exception:
        return True


def _decode(raw: bytes, headers) -> str:
    encoding = (headers.get("Content-Encoding") or "").lower()
    try:
        if "gzip" in encoding:
            raw = gzip.decompress(raw)
        elif "deflate" in encoding:
            raw = zlib.decompress(raw, -zlib.MAX_WBITS)
    except Exception:
        pass
    charset = "utf-8"
    ctype = headers.get("Content-Type") or ""
    if "charset=" in ctype:
        charset = ctype.split("charset=")[-1].split(";")[0].strip() or "utf-8"
    try:
        return raw.decode(charset, "replace")
    except LookupError:
        return raw.decode("utf-8", "replace")


def get(url: str, headers: dict | None = None, use_cache: bool = True,
        check_robots: bool = True, data: bytes | None = None) -> Response:
    """Fetch a URL. Passing `data` makes it a POST - some search APIs require one."""
    key = _cache_key(url, data)
    if use_cache:
        cached = _cache_read(key)
        if cached:
            return cached

    if check_robots and not robots_allows(url):
        return Response(url=url, status=0, text="", error="blocked by robots.txt")

    parts = urllib.parse.urlsplit(url)
    if parts.scheme not in ("http", "https"):
        return Response(url=url, status=0, text="", error="unsupported scheme")

    send = {
        "User-Agent": config.USER_AGENT,
        "Accept": "text/html,application/json,application/xhtml+xml,*/*;q=0.8",
        "Accept-Language": "en",
        "Accept-Encoding": "gzip, deflate",
    }
    send.update(headers or {})

    _throttle(parts.netloc)
    request = urllib.request.Request(url, data=data, headers=send)
    try:
        with urllib.request.urlopen(request, timeout=config.HTTP_TIMEOUT) as handle:
            raw = handle.read(config.MAX_PAGE_BYTES)
            response = Response(url=handle.geturl(), status=handle.status,
                                text=_decode(raw, handle.headers))
    except urllib.error.HTTPError as exc:
        body = ""
        try:
            body = _decode(exc.read(config.MAX_PAGE_BYTES), exc.headers)
        except Exception:
            pass
        response = Response(url=url, status=exc.code, text=body, error=f"HTTP {exc.code}")
    except Exception as exc:  # socket errors, TLS failures, timeouts, bad redirects
        response = Response(url=url, status=0, text="", error=str(exc)[:200])

    if use_cache and response.status in (200, 404, 403):
        _cache_write(response, key)
    return response


def get_json(url: str, headers: dict | None = None, use_cache: bool = True,
             check_robots: bool = False, data: bytes | None = None):
    response = get(url, headers=headers, use_cache=use_cache, check_robots=check_robots,
                   data=data)
    if not response.text:
        return None
    try:
        return json.loads(response.text)
    except ValueError:
        return None
