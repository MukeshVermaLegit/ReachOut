"""Web search, as a chain of backends rather than a single choice.

  1. Serper               - SERPER_API_KEY. A Google SERP passthrough. Prepaid
                            credit packs, 2,500 free to start, no card and no
                            open billing relationship.
  2. Brave Search API     - BRAVE_API_KEY. Postpaid, $5/1,000 requests, with
                            $5 of credit granted monthly (~1,000 requests).
                            The old free tier no longer exists; billing must
                            be set up even to spend only the credit.
  3. Google Custom Search - GOOGLE_CSE_KEY + GOOGLE_CSE_CX. 100/day free.
  4. SearXNG              - SEARXNG_URL. An instance you host yourself. No key,
                            no quota, no account.
  5. DuckDuckGo HTML      - no key, rate-limited, liable to be blocked.

A query walks the chain until one backend answers. Having more than one keyed
backend is worth the setup because they fail *differently*: Serper runs out of
prepaid credit, Brave is limited per second, Google per day - so one is usually
up when the others are not.

**Search operators are not a nicety here.** The queries this tool builds lean on
`site:`, `filetype:pdf`, quoted phrases and `OR` groups - see `sources.directory`
and `sources.documents`. A backend that treats those as ordinary words will
return plausible rubbish rather than failing, which is worse than an outage. Only
engines that pass operators through to a real index belong in this chain; that
rules out the semantic/embedding search APIs, whose whole design is to ignore
exactly the literal constraints these queries depend on.

Two distinctions this module exists to preserve:

  **Empty is not blocked.** A backend that answers with nothing has told you
  something. A backend that refused has not. Collapsing both into `[]` is how a
  phone lookup reports `not_found` after never actually searching - which is a
  confident negative built on no evidence, and the worst output this tool can
  produce. Hence `SearchUnavailable` rather than an empty list.

  **Out of quota is not broken.** A rate limit is a wait, a daily cap is a long
  wait, and a rejected credential is neither - it will still be wrong on the
  next lookup. `BackendFailed` carries how long to stand down, so a spent
  backend is skipped instead of re-tried five more times in the same lookup.

Search is not optional here. Three of the six phone providers - staff-directory,
public-documents and public-pages - cannot find anything without it.
"""

from __future__ import annotations

import json
import re
import time
import urllib.parse
from dataclasses import dataclass
from html.parser import HTMLParser

from .. import config
from .. import fetch
from ..fetch import get

# Domains that aggregate people data. Useful as evidence a person exists, useless
# as a company-domain answer, so company resolution skips them.
AGGREGATORS = {
    "linkedin.com", "crunchbase.com", "wikipedia.org", "wikidata.org", "facebook.com",
    "twitter.com", "x.com", "instagram.com", "youtube.com", "bloomberg.com", "reuters.com",
    "glassdoor.com", "indeed.com", "zoominfo.com", "apollo.io", "rocketreach.co",
    "signalhire.com", "lusha.com", "contactout.com", "hunter.io", "clearbit.com",
    "pitchbook.com", "owler.com", "zippia.com", "leadiq.com", "medium.com", "github.com",
    "tracxn.com", "ambitionbox.com", "naukri.com", "quora.com", "reddit.com", "pinterest.com",
}


class SearchUnavailable(Exception):
    """No backend could be queried at all.

    Kept strictly apart from an empty result list. "Nobody has published this"
    and "we were not allowed to look" are opposite conclusions, and collapsing
    them into `[]` is how a lookup reports `not_found` after never searching.
    """


class BackendFailed(SearchUnavailable):
    """One backend is out. The chain should try the next one."""

    def __init__(self, message: str, cool_off: float = 0.0, permanent: bool = False):
        super().__init__(message)
        # How long to stop asking. A per-second rate limit wants seconds; a
        # daily quota wants hours; a rejected credential wants the whole run,
        # because retrying a wrong key just wastes the next lookup too.
        self.cool_off = cool_off
        self.permanent = permanent


# What a genuine zero-result page says, as opposed to a challenge page.
_NO_RESULTS = re.compile(r"no\s+results\s+(found|for)|did not match any", re.I)

# Counters, not a latched message. A provider that runs five queries needs to
# know whether *any* of them were refused, and a later success must not erase
# that - "four of five searches were blocked" is the fact worth reporting.
_STATE = {"queries": 0, "outages": 0, "reason": "", "last_ok": True}

# Once a backend has refused, further queries in the same lookup are refused too
# and only deepen the throttling. Fail fast instead: a lookup makes five or six
# queries, and spending five blocked round-trips to learn the same thing five
# times is neither faster nor politer.
KEYLESS_COOLDOWN = 120.0
BRAVE_RATE_PAUSE = 2.0            # free tier: 1 query/second
GOOGLE_QUOTA_PAUSE = 3600.0 * 6   # free tier: 100 queries/day, resets midnight PT
SERPER_RATE_PAUSE = 2.0           # bursts are throttled per second, not per day
SEARXNG_PAUSE = 30.0              # its own limiter, or an upstream engine sulking


def snapshot() -> tuple:
    """(queries, outages, last reason) - diff two of these around a call.

    Counts whole queries, not backend attempts: a query that failed over from
    Brave to Google and succeeded is one successful query, not one outage.
    """
    return (_STATE["queries"], _STATE["outages"], _STATE["reason"])


def status() -> str:
    """Why search is not working, or '' if the last query went through."""
    return "" if _STATE["last_ok"] else _STATE["reason"]


@dataclass
class Hit:
    title: str
    url: str
    snippet: str
    source: str = ""

    @property
    def host(self) -> str:
        host = urllib.parse.urlsplit(self.url).netloc.lower()
        return host[4:] if host.startswith("www.") else host


# Second-level labels that are registry suffixes rather than an organisation.
_TWO_PART = {"co", "com", "net", "org", "ac", "gov", "edu", "or", "ne", "go"}

# India uses a wider set of second-levels than most registries, and the ones
# below carry the real organisation a further label down: tifr.res.in is an
# institute, res.in is the registry. Gated on the .in TLD so an ordinary domain
# like res.com or gen.io is left alone.
_TWO_PART_IN = {"res", "nic", "ind", "firm", "gen", "mil"}


def registrable(host: str) -> str:
    """Rough eTLD+1. Handles the common two-part suffixes we actually meet."""
    host = (host or "").lower().strip().rstrip(".")
    if host.startswith("www."):
        host = host[4:]
    parts = host.split(".")
    if len(parts) <= 2:
        return host
    suffix = parts[-2]
    if suffix in _TWO_PART and len(parts[-1]) <= 3:
        return ".".join(parts[-3:])
    if parts[-1] == "in" and suffix in _TWO_PART_IN:
        return ".".join(parts[-3:])
    return ".".join(parts[-2:])


def _brave(query: str, count: int) -> list[Hit]:
    url = ("https://api.search.brave.com/res/v1/web/search?q="
           f"{urllib.parse.quote(query)}&count={min(count, 20)}")
    response = get(url, headers={"X-Subscription-Token": config.BRAVE_API_KEY,
                                 "Accept": "application/json"}, check_robots=False)
    if not response.ok:
        if response.status == 429:
            # Over the plan's requests-per-second capacity. That is a wait, not a fault.
            raise BackendFailed("Brave rate limit (over the plan's per-second capacity)",
                                cool_off=BRAVE_RATE_PAUSE)
        if response.status in (401, 403):
            raise BackendFailed(
                f"Brave rejected the credential (HTTP {response.status}) - check "
                f"BRAVE_API_KEY", permanent=True)
        if response.status == 422:
            raise BackendFailed("Brave rejected the query as malformed", cool_off=0)
        raise BackendFailed(
            f"Brave returned {response.error or f'HTTP {response.status}'}", cool_off=60)
    try:
        data = json.loads(response.text)
    except ValueError:
        return []
    return [Hit(item.get("title", ""), item.get("url", ""),
                re.sub(r"<[^>]+>", "", item.get("description", "") or ""), "brave")
            for item in (data.get("web", {}) or {}).get("results", [])]


def _error_message(text: str) -> str:
    """The `message` an API put in its error body, or '' if there is not one."""
    try:
        payload = json.loads(text or "")
    except ValueError:
        return ""
    if not isinstance(payload, dict):
        return ""
    return str(payload.get("message") or payload.get("error") or "")[:160]


def _serper(query: str, count: int) -> list[Hit]:
    """Google's own results as JSON, operators and all.

    This is the backend the directory and document providers actually want:
    `site:`, `filetype:pdf` and quoted phrases go to Google unchanged, so a
    query that would work in the search box works here.
    """
    # Asking for more than ten results costs two credits instead of one, and no
    # caller here asks for more than eight. Capping keeps a lookup at one credit
    # per query rather than silently doubling the bill.
    body = json.dumps({"q": query, "num": min(count, 10)}).encode("utf-8")
    response = get("https://google.serper.dev/search", data=body,
                   headers={"X-API-KEY": config.SERPER_API_KEY,
                            "Content-Type": "application/json",
                            "Accept": "application/json"}, check_robots=False)
    if not response.ok:
        detail = _error_message(response.text)
        lowered = detail.lower()
        if response.status == 429:
            raise BackendFailed("Serper rate limit (too many queries per second)",
                                cool_off=SERPER_RATE_PAUSE)
        if "credit" in lowered:
            # Prepaid, so this is not a wait: the balance is zero until someone
            # buys another pack. Retrying costs round-trips and changes nothing.
            raise BackendFailed(
                f"Serper is out of prepaid credit ({detail}) - top up at "
                f"serper.dev, or the chain will fall through to the next backend",
                permanent=True)
        if response.status in (401, 403):
            raise BackendFailed(
                f"Serper rejected the credential (HTTP {response.status}"
                + (f": {detail}" if detail else "") + ") - check SERPER_API_KEY",
                permanent=True)
        if response.status == 400:
            raise BackendFailed(
                f"Serper rejected the query as malformed{': ' + detail if detail else ''}",
                cool_off=0)
        raise BackendFailed(
            f"Serper returned {response.error or f'HTTP {response.status}'}", cool_off=60)
    try:
        data = json.loads(response.text)
    except ValueError:
        return []
    return [Hit(item.get("title", ""), item.get("link", ""), item.get("snippet", "") or "",
                "serper")
            for item in (data.get("organic") or [])
            if item.get("link")]


def _searxng(query: str, count: int) -> list[Hit]:
    """A SearXNG instance you host yourself - no key, no quota, no account.

    SearXNG is a metasearch front end: it forwards the query to Google, Bing,
    DuckDuckGo and others and merges what comes back, so operators survive as
    far as whichever upstream engine handles them.

    The reason it needs its own reader rather than a generic JSON one is
    `unresponsive_engines`. When Google hands SearXNG a CAPTCHA, SearXNG does
    not fail - it answers 200 with an empty `results` list and names the engine
    that let it down. Read naively that is "the web has nothing", which is the
    exact false negative this module exists to prevent.
    """
    url = (f"{config.SEARXNG_URL}/search?q={urllib.parse.quote(query)}"
           f"&format=json&safesearch=0")
    response = get(url, headers={"Accept": "application/json"}, check_robots=False)
    if not response.ok:
        if response.status == 429:
            raise BackendFailed("SearXNG's own limiter is throttling this client",
                                cool_off=SEARXNG_PAUSE)
        if response.status == 403:
            # Both causes are configuration, not load, and both look identical
            # from here - so name them both rather than guess.
            raise BackendFailed(
                f"SearXNG at {config.SEARXNG_URL} refused the request (HTTP 403) - "
                f"either `json` is missing from the `formats:` list in its "
                f"settings.yml, or its limiter is blocking this client",
                permanent=True)
        raise BackendFailed(
            f"SearXNG at {config.SEARXNG_URL} returned "
            f"{response.error or f'HTTP {response.status}'}", cool_off=SEARXNG_PAUSE)
    try:
        data = json.loads(response.text)
    except ValueError:
        raise BackendFailed(
            f"SearXNG at {config.SEARXNG_URL} answered with something that is not "
            f"JSON - `json` is probably missing from its `formats:` list",
            permanent=True) from None

    hits = [Hit(item.get("title", ""), item.get("url", ""), item.get("content", "") or "",
                "searxng")
            for item in (data.get("results") or [])
            if item.get("url")]
    if hits:
        return hits[:count]

    # Nothing came back. Whether that is an answer depends entirely on whether
    # the engines were actually reachable.
    down = data.get("unresponsive_engines") or []
    if down:
        names = ", ".join(sorted({str(entry[0]) if isinstance(entry, (list, tuple)) and entry
                                  else str(entry) for entry in down}))[:120]
        raise BackendFailed(
            f"SearXNG returned nothing and reported its engines unreachable ({names}) - "
            f"this is an outage, not an empty result", cool_off=SEARXNG_PAUSE)
    return []


def _google_cse(query: str, count: int) -> list[Hit]:
    url = ("https://www.googleapis.com/customsearch/v1?"
           f"key={config.GOOGLE_CSE_KEY}&cx={config.GOOGLE_CSE_CX}"
           f"&q={urllib.parse.quote(query)}&num={min(count, 10)}")
    response = get(url, check_robots=False)
    detail = ""
    if response.text:
        try:
            detail = ((json.loads(response.text).get("error") or {}).get("message") or "")
        except ValueError:
            detail = ""
    if not response.ok:
        lowered = detail.lower()
        if response.status in (429,) or "quota" in lowered or "daily limit" in lowered:
            # 100 queries/day on the free tier, and it resets at midnight
            # Pacific - so there is no point retrying for hours.
            raise BackendFailed(
                f"Google CSE daily quota exhausted ({detail or '100 queries/day on the '
                'free tier'})", cool_off=GOOGLE_QUOTA_PAUSE)
        if response.status in (400, 401, 403):
            raise BackendFailed(
                f"Google CSE rejected the request (HTTP {response.status})"
                + (f": {detail}" if detail else "")
                + " - check GOOGLE_CSE_KEY, GOOGLE_CSE_CX, and that the Custom "
                  "Search API is enabled for the project", permanent=True)
        raise BackendFailed(
            f"Google CSE returned {response.error or f'HTTP {response.status}'}", cool_off=60)
    try:
        data = json.loads(response.text)
    except ValueError:
        return []
    return [Hit(item.get("title", ""), item.get("link", ""), item.get("snippet", ""), "google")
            for item in data.get("items", [])]


class _DDGParser(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.hits: list[Hit] = []
        self._href = ""
        self._mode = ""
        self._buffer: list[str] = []
        self._pending_title = ""
        self._pending_url = ""

    def handle_starttag(self, tag, attrs):
        attributes = dict(attrs)
        classes = attributes.get("class", "") or ""
        if tag == "a" and "result__a" in classes:
            self._mode = "title"
            self._href = attributes.get("href", "") or ""
            self._buffer = []
        elif tag == "a" and "result__snippet" in classes:
            self._mode = "snippet"
            self._buffer = []
        elif tag in ("td", "div") and "result__snippet" in classes:
            self._mode = "snippet"
            self._buffer = []

    def handle_endtag(self, tag):
        if self._mode == "title" and tag == "a":
            self._pending_title = " ".join(" ".join(self._buffer).split())
            self._pending_url = _ddg_unwrap(self._href)
            self._mode = ""
        elif self._mode == "snippet" and tag in ("a", "td", "div"):
            snippet = " ".join(" ".join(self._buffer).split())
            if self._pending_url:
                self.hits.append(Hit(self._pending_title, self._pending_url, snippet, "duckduckgo"))
                self._pending_title = self._pending_url = ""
            self._mode = ""

    def handle_data(self, data):
        if self._mode:
            self._buffer.append(data.strip())


def _ddg_unwrap(href: str) -> str:
    """DuckDuckGo wraps results as /l/?uddg=<encoded>."""
    if not href:
        return ""
    if href.startswith("//"):
        href = "https:" + href
    parsed = urllib.parse.urlsplit(href)
    if "uddg" in (parsed.query or ""):
        params = urllib.parse.parse_qs(parsed.query)
        target = params.get("uddg", [""])[0]
        if target:
            return urllib.parse.unquote(target)
    return href if href.startswith("http") else ""


def _duckduckgo(query: str, count: int) -> list[Hit]:
    url = f"https://html.duckduckgo.com/html/?q={urllib.parse.quote(query)}"
    response = get(url, headers={
        "Accept": "text/html,application/xhtml+xml",
        "Referer": "https://duckduckgo.com/",
    }, check_robots=False)
    if not response.ok:
        raise BackendFailed(
            f"DuckDuckGo returned {response.error or f'HTTP {response.status}'}",
            cool_off=KEYLESS_COOLDOWN)
    parser = _DDGParser()
    try:
        parser.feed(response.text)
    except Exception:
        pass
    good = [h for h in parser.hits if h.url.startswith("http")]
    if good:
        return good[:count]
    # No hits parsed. A real zero-result page says so in words and still ships
    # the result markup; a challenge or rate-limit page ships neither.
    if "result__a" in response.text or _NO_RESULTS.search(response.text):
        return []
    # DuckDuckGo answers 202 with a challenge shell when it is throttling, and
    # 200 with real markup when it is not. Nothing to parse and no "no results"
    # wording means the former.
    raise BackendFailed(
        "DuckDuckGo served a page with no results and no result markup - it is "
        "rate-limiting or bot-checking this client. Set BRAVE_API_KEY, or "
        "GOOGLE_CSE_KEY and GOOGLE_CSE_CX, for a keyed backend",
        cool_off=KEYLESS_COOLDOWN)


@dataclass
class Backend:
    """One search engine, with the state needed to stop asking a dead one."""

    name: str
    run: object                      # (query, count) -> list[Hit]
    keyed: bool = True
    setup: str = ""                  # how to configure it, for `doctor`
    # Does this backend index the whole web? A site-restricted engine finding
    # nothing has told you nothing about the open web, so its empty results must
    # not end the chain.
    authoritative: bool = True
    queries: int = 0
    outages: int = 0
    reason: str = ""
    resting_until: float = 0.0
    dead: bool = False               # credential rejected; do not retry this run

    def configured(self) -> bool:
        return True

    @property
    def resting(self) -> float:
        return max(0.0, self.resting_until - time.monotonic())

    def ready(self) -> bool:
        return self.configured() and not self.dead and not self.resting

    def state(self) -> str:
        if not self.configured():
            return "not configured"
        if self.dead:
            return f"disabled this run: {self.reason}"
        if self.resting:
            return f"resting {self.resting:.0f}s: {self.reason}"
        return "ready"

    def stand_down(self, exc: "BackendFailed") -> None:
        self.outages += 1
        self.reason = str(exc)
        if exc.permanent:
            self.dead = True
        elif exc.cool_off:
            self.resting_until = time.monotonic() + exc.cool_off


class _Serper(Backend):
    def configured(self) -> bool:
        return bool(config.SERPER_API_KEY)


class _Brave(Backend):
    def configured(self) -> bool:
        return bool(config.BRAVE_API_KEY)


class _Searxng(Backend):
    def configured(self) -> bool:
        return bool(config.SEARXNG_URL)


class _Google(Backend):
    def configured(self) -> bool:
        return bool(config.GOOGLE_CSE_KEY and config.GOOGLE_CSE_CX)

    @property
    def authoritative(self) -> bool:                   # type: ignore[override]
        return config.GOOGLE_CSE_WHOLE_WEB

    @authoritative.setter
    def authoritative(self, value) -> None:
        pass                                           # driven by config, not state


# Order matters, and it is ordered by operator fidelity before cost. Serper
# leads because it is a Google passthrough: `site:`, `filetype:` and quoted
# phrases behave exactly as they do in the search box, which is what the
# directory and document queries are written against. Brave and Google CSE
# support the same operators with their own indexes behind them. SearXNG sits
# below the keyed backends because it is only as reliable as whatever upstream
# engine answers it, and the DuckDuckGo scraper stays last.
#
# Backends earn their place by failing differently: Serper runs out of prepaid
# credit, Brave is capped per second, Google per day, SearXNG only when its
# upstreams sulk. One is usually up when the others are not.
CHAIN: list = [
    _Serper("serper", _serper, setup="SERPER_API_KEY"),
    _Brave("brave", _brave, setup="BRAVE_API_KEY"),
    _Google("google", _google_cse,
            setup="GOOGLE_CSE_KEY + GOOGLE_CSE_CX (and enable the Custom Search API)"),
    _Searxng("searxng", _searxng, keyed=False,
             setup="SEARXNG_URL, pointing at an instance you host with `json` "
                   "in its settings.yml formats list"),
    Backend("duckduckgo", _duckduckgo, keyed=False,
            setup="no key needed, but rate-limited and liable to be blocked"),
]


def chain() -> list:
    return CHAIN


def configured_backends() -> list:
    return [b for b in CHAIN if b.configured()]


def available() -> str:
    """The backend that would be tried first. Kept for display and diagnostics."""
    for backend in CHAIN:
        if backend.ready():
            return backend.name
    for backend in CHAIN:
        if backend.configured():
            return backend.name
    return "duckduckgo"


def reset_backends() -> None:
    """Clear rested/dead state. Mostly for tests and long-running processes."""
    for backend in CHAIN:
        backend.queries = backend.outages = 0
        backend.reason = ""
        backend.resting_until = 0.0
        backend.dead = False


def search(query: str, count: int = 10) -> list[Hit]:
    """Run one query. Raises SearchUnavailable if no backend could be reached.

    An empty list means the engines were asked and had nothing. That is a real
    answer and callers may act on it; SearchUnavailable is not.
    """
    _STATE["queries"] += 1
    problems: list[str] = []
    skipped: list[str] = []
    answered = False

    for backend in CHAIN:
        if not backend.configured():
            continue
        if not backend.ready():
            skipped.append(f"{backend.name} ({backend.state()})")
            continue
        backend.queries += 1
        try:
            hits = backend.run(query, count)
        except BackendFailed as exc:
            backend.stand_down(exc)
            problems.append(f"{backend.name}: {exc}")
            continue
        except Exception as exc:                       # noqa: BLE001
            backend.stand_down(BackendFailed(str(exc)[:120], cool_off=60))
            problems.append(f"{backend.name}: {exc}"[:140])
            continue
        backend.reason = ""
        answered = True
        # Broker and dump hosts are dropped here, at the one point every caller
        # goes through, so the email path cannot cite a source the phone path
        # would have refused. Dropped before the authoritative check: a page of
        # nothing but brokers is "nothing usable published", not a live result.
        hits = [h for h in hits if not fetch.disallowed_source(h.url)]
        if hits or backend.authoritative:
            # A whole-web backend that answered is authoritative for this query.
            # Zero results from one is a real zero, and asking the next engine
            # would spend quota to make "nothing published" less clear, not more.
            _STATE["last_ok"] = True
            return hits[:count]
        # A site-restricted index found nothing. That is a statement about the
        # sites it was given, not about the web, so the chain continues.
        skipped.append(f"{backend.name} (restricted index, no match)")

    if answered:
        # Every engine that could answer did, and none had anything. Empty, not
        # blocked - the caller may treat this as a real zero.
        _STATE["last_ok"] = True
        return []

    _fail(" / ".join(problems + skipped) or "no search backend is configured")
    raise SearchUnavailable(_STATE["reason"])


def _fail(reason: str) -> None:
    _STATE["outages"] += 1
    _STATE["reason"] = reason
    _STATE["last_ok"] = False


# A query any whole-web index answers. Zero results for this from a keyed
# backend means the backend is not searching the whole web.
PROBE_QUERY = "wikipedia"


def probe_backend(backend: Backend) -> tuple[bool, str]:
    """Ask one backend a question it must be able to answer."""
    if not backend.configured():
        return False, f"not configured - set {backend.setup}"
    try:
        hits = backend.run(PROBE_QUERY, 3)
    except BackendFailed as exc:
        backend.stand_down(exc)
        return False, str(exc)
    except Exception as exc:                           # noqa: BLE001
        return False, str(exc)[:140]
    if not hits:
        if not backend.authoritative:
            # Expected, and not a fault: it was only ever asked about its own
            # sites. It supplements the chain and never ends it.
            return True, ("restricted to the sites you listed - supplements the "
                          "chain, never concludes it")
        if backend.name == "google":
            # A whole-web engine that answers nothing here is misconfigured: the
            # usual cause is a Programmable Search Engine still restricted to a
            # site list while GOOGLE_CSE_WHOLE_WEB claims otherwise.
            return False, ("returned nothing for a query the whole web answers - the "
                           "engine is restricted to specific sites. Either turn on "
                           "'Search the entire web', or unset GOOGLE_CSE_WHOLE_WEB "
                           "so it is treated as a supplementary index")
        return False, "configured, but returned nothing for a query the whole web answers"
    scope = "whole web" if backend.authoritative else "restricted site list"
    return True, f"{len(hits)} result(s) for a probe query ({scope})"


def probe() -> tuple[bool, str]:
    """Check every configured backend. For `reachout doctor`."""
    results = [(b.name, *probe_backend(b)) for b in CHAIN if b.configured()]
    working = [name for name, ok, _detail in results if ok]
    if working:
        return True, ", ".join(f"{name}: {detail}" for name, ok, detail in results if ok)
    return False, "; ".join(f"{name}: {detail}" for name, _ok, detail in results) \
        or "no backend configured"


_EMAIL_RE = re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9][A-Za-z0-9.\-]*\.[A-Za-z]{2,24}")


def emails_in_results(hits: list[Hit], domain_filter: str = "") -> list[tuple[str, str]]:
    """(address, source_url) mentioned in result titles/snippets."""
    out: list[tuple[str, str]] = []
    seen: set[str] = set()
    for hit in hits:
        for match in _EMAIL_RE.finditer(f"{hit.title} {hit.snippet}"):
            address = match.group(0).lower().strip(".,;:")
            if domain_filter and address.split("@")[-1] != domain_filter.lower():
                continue
            if address in seen:
                continue
            seen.add(address)
            out.append((address, hit.url))
    return out
