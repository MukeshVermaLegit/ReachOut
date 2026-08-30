"""Staff and faculty directories - where direct dials are actually published.

A company contact page gives you the switchboard. A *directory* gives you a row
per person, and that row usually carries an extension or a direct line. This is
the one public source that answers "which number reaches this person" rather
than "which numbers does this organisation own", so it is worth reading with
different rules from an ordinary page.

Those rules are all about **binding**. Scanning a directory the way
`website._phones_from_page` scans a contact page is actively harmful: on a page
listing two hundred staff, a +-70-character window around a number lands on a
neighbouring row and attributes the number to the wrong person. Confidently
wrong is worse than nothing, so attribution here is structural, not proximity
based:

  a number belongs to a person only when the smallest enclosing block of markup
  that contains both contains exactly *one* candidate name.

Two names in the block, or none, and the number stays unattributed. That single
rule is what makes a 200-row table safe to read.
"""

from __future__ import annotations

import re
import urllib.parse
from dataclasses import dataclass, field
from html.parser import HTMLParser

from .. import phones as phonelib
from ..fetch import get
from ..names import Name
from . import search as websearch
from .website import EMAIL_RE, _PHONE_TEXT_RE, FoundPhone, person_names

# Tags that can plausibly delimit "one person's entry". `div` and `section` are
# included because card layouts use nothing else - the size cap below is what
# stops a page-wrapping div from counting as a row.
_BLOCK_TAGS = {"tr", "li", "article", "section", "div", "td", "dd", "p"}

# A row describes one person. Anything longer is a page, and a name and a number
# sitting that far apart are not evidence of anything.
MAX_ROW_CHARS = 400

# URL and link-text markers for a directory rather than a marketing page.
_DIRECTORY_HINTS = {
    "directory": 10, "staff-directory": 10, "faculty-directory": 10,
    "phone-directory": 10, "phonebook": 10, "phone-book": 10, "telephone-directory": 10,
    "faculty": 8, "staff": 8, "people": 7, "employees": 7, "our-people": 7,
    "administration": 6, "departments": 5, "department": 5, "team": 5,
    "contact-directory": 9, "who-is-who": 8, "whos-who": 8, "extensions": 8,
}
_DIRECTORY_SKIP = re.compile(
    r"\.(pdf|jpe?g|png|gif|svg|zip|mp4|webp|css|js)$|/(blog|news|careers|jobs|privacy|"
    r"terms|cookie|login|signin|signup|cart|pricing|docs|help)(/|$)", re.I)


@dataclass
class Row:
    """One block of markup, with everything found inside it."""
    tag: str
    text: str = ""
    chunks: list = field(default_factory=list)
    tels: list = field(default_factory=list)

    @property
    def size(self) -> int:
        return len(self.text)

    @property
    def emails(self) -> list:
        """Addresses printed inside this row.

        A directory row that carries a work address is self-labelling: the row
        states whose entry it is, in an identifier the employer issued, rather
        than leaving the caller to infer it from where the text happens to sit.
        """
        return [m.group(0).lower() for m in EMAIL_RE.finditer(self.text)]


class _RowParser(HTMLParser):
    """Collects every block-level element as a candidate 'row'.

    Text flows into every open block, so an inner `td` and its enclosing `tr`
    both end up holding it. That redundancy is the point: it lets the caller
    pick the *smallest* block that happens to contain both a name and a number.
    """

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.rows: list[Row] = []
        self._open: list[tuple[str, Row]] = []
        self._skip_depth = 0
        self._pending_tel = ""

    def handle_starttag(self, tag, attrs):
        if tag in ("script", "style", "noscript"):
            self._skip_depth += 1
            return
        attributes = dict(attrs)
        if tag == "a":
            href = (attributes.get("href") or "").strip().lower()
            self._pending_tel = ""
            if href.startswith(("tel:", "callto:")):
                number = urllib.parse.unquote(href.split(":", 1)[1]).strip()
                if number:
                    self._pending_tel = number
                    for _name, row in self._open:
                        row.tels.append(number)
        if (attributes.get("itemprop") or "").lower() == "telephone":
            value = (attributes.get("content") or "").strip()
            if value:
                for _name, row in self._open:
                    row.tels.append(value)
        if tag in _BLOCK_TAGS:
            self._open.append((tag, Row(tag=tag)))

    def handle_endtag(self, tag):
        if tag in ("script", "style", "noscript"):
            self._skip_depth = max(0, self._skip_depth - 1)
            return
        if tag == "a":
            self._pending_tel = ""
        if tag not in _BLOCK_TAGS:
            return
        # Close back to the matching tag; malformed markup is the norm, not the
        # exception, so unwind rather than assume balance.
        for index in range(len(self._open) - 1, -1, -1):
            if self._open[index][0] == tag:
                for _name, row in self._open[index:]:
                    row.text = " ".join(" ".join(row.chunks).split())
                    if row.text:
                        self.rows.append(row)
                del self._open[index:]
                return

    def handle_data(self, data):
        if self._skip_depth:
            return
        stripped = data.strip()
        if not stripped:
            return
        for _name, row in self._open:
            row.chunks.append(stripped)

    def close(self):
        super().close()
        for _name, row in self._open:
            row.text = " ".join(" ".join(row.chunks).split())
            if row.text:
                self.rows.append(row)
        self._open = []


def rows_from_html(html: str) -> list[Row]:
    parser = _RowParser()
    try:
        parser.feed(html)
        parser.close()
    except Exception:
        pass  # keep whatever parsed - directory pages are rarely well formed
    return parser.rows


def _numbers_in(row: Row, region: str) -> list[str]:
    """Every valid number inside one row, as E.164, tel: links first."""
    out: list[str] = []
    for raw in row.tels + [m.group(0) for m in _PHONE_TEXT_RE.finditer(row.text)]:
        parsed = phonelib.normalise(raw, default_region=region)
        if parsed.valid and parsed.e164 not in out:
            out.append(parsed.e164)
    return out


def attribute_rows(rows: list[Row], region: str, brand: str = "") -> list[FoundPhone]:
    """Bind numbers to names by enclosing block, smallest block wins.

    Returns one FoundPhone per distinct number. `owner` is set only when some
    block containing that number holds exactly one candidate person name; that
    block is also the smallest such block, so the binding is as tight as the
    page's own markup allows.
    """
    # (number -> best row so far), plus every number seen with no owner at all.
    owned: dict[str, tuple[int, str, Row]] = {}
    unowned: dict[str, Row] = {}

    for row in rows:
        if row.size > MAX_ROW_CHARS:
            continue
        numbers = _numbers_in(row, region)
        if not numbers:
            continue
        names = list(dict.fromkeys(person_names(row.chunks, brand)))
        for number in numbers:
            unowned.setdefault(number, row)
            if len(names) != 1:
                # No name, or an ambiguous pair - this block proves nothing.
                continue
            best = owned.get(number)
            if best is None or row.size < best[0]:
                owned[number] = (row.size, names[0], row)

    found: list[FoundPhone] = []
    for number, (_size, owner, row) in owned.items():
        found.append(FoundPhone(
            raw=number, context=row.text[:160], owner=owner,
            scope="person", how="directory", emails=row.emails))
    for number, row in unowned.items():
        if number in owned:
            continue
        # No name in the block, but an address in it can still identify the
        # person a caller asked about, so the row's addresses travel either way.
        found.append(FoundPhone(
            raw=number, context=row.text[:160], owner="",
            scope="page", how="directory", emails=row.emails))
    return found


@dataclass
class DirectoryPage:
    url: str
    phones: list = field(default_factory=list)
    links: list = field(default_factory=list)   # further directory-ish pages
    error: str = ""


def harvest_directory(url: str, region: str = "", brand: str = "") -> DirectoryPage:
    """Read one directory page, and note where it says the real listing lives.

    Large organisations put a navigation page at /faculty and the actual per-person
    rows one level down, under each department. Returning the onward links lets the
    caller descend without crawling the whole site.
    """
    response = get(url)
    if not response.ok:
        return DirectoryPage(url=url, error=response.error or f"HTTP {response.status}")
    if not region:
        region = phonelib.region_for_domain(urllib.parse.urlsplit(url).netloc)
    found = attribute_rows(rows_from_html(response.text), region, brand)
    for phone in found:
        phone.source_url = response.url
    return DirectoryPage(url=response.url, phones=found,
                         links=_directory_links(response.text, response.url))


def _directory_links(html: str, base_url: str) -> list[str]:
    """Same-host links whose path or text marks them as a directory, best first."""
    from .website import _Extractor
    parser = _Extractor()
    try:
        parser.feed(html)
    except Exception:
        pass
    host = urllib.parse.urlsplit(base_url).netloc.lower()
    if host.startswith("www."):
        host = host[4:]
    scored: dict[str, int] = {}
    for href, anchor in parser.links:
        absolute = urllib.parse.urljoin(base_url, href)
        parts = urllib.parse.urlsplit(absolute)
        if parts.scheme not in ("http", "https"):
            continue
        candidate_host = parts.netloc.lower()
        if candidate_host.startswith("www."):
            candidate_host = candidate_host[4:]
        if candidate_host != host:
            continue
        if _DIRECTORY_SKIP.search(parts.path or "/"):
            continue
        rank = _score_directory_link(parts.path or "/", anchor)
        if rank <= 0:
            continue
        clean = urllib.parse.urlunsplit(
            (parts.scheme, parts.netloc, parts.path.rstrip("/") or "/", "", ""))
        scored[clean] = max(rank, scored.get(clean, 0))
    ordered = sorted(scored.items(), key=lambda kv: -kv[1])
    return [url for url, _rank in ordered]


def _score_directory_link(path: str, anchor: str) -> int:
    segments = [s for s in path.lower().replace("_", "-").strip("/").split("/") if s]
    words = set(re.split(r"[^a-z]+", anchor.lower()))
    score = 0
    for segment in segments:
        if segment in _DIRECTORY_HINTS:
            score = max(score, _DIRECTORY_HINTS[segment])
    if 0 < len([w for w in words if w]) <= 5:
        for phrase, weight in _DIRECTORY_HINTS.items():
            if "-" not in phrase and phrase in words:
                score = max(score, weight - 1)
    return score


def find_directories(domain: str, name: Name | None = None, org: str = "",
                     limit: int = 3, use_search: bool = True) -> list[str]:
    """Candidate directory pages for a domain, best first.

    Two independent routes, because they fail in different places: following the
    site's own navigation works when the directory is linked from the homepage,
    and site-scoped search works when it is buried three levels down, which on a
    university site it usually is.
    """
    domain = (domain or "").lower().strip().lstrip("@")
    if not domain:
        return []
    urls: list[str] = []

    def offer(candidate: str) -> None:
        candidate = candidate.split("#")[0].rstrip("/")
        if candidate and candidate not in urls and not _DIRECTORY_SKIP.search(candidate):
            urls.append(candidate)

    if use_search:
        queries = [f'site:{domain} (directory OR "staff directory" OR "phone directory")']
        if name is not None and name.display:
            # The person's own page, if the directory has one per person.
            queries.insert(0, f'site:{domain} "{name.display}" (phone OR contact OR extension)')
        if org:
            queries.append(f'{org} staff directory phone site:{domain}')
        for query in queries:
            try:
                hits = websearch.search(query, count=8)
            except Exception:
                continue
            for hit in hits:
                if websearch.registrable(hit.host) != websearch.registrable(domain):
                    continue
                offer(hit.url)
            if len(urls) >= limit:
                return urls[:limit]

    # Follow the site's own navigation as a backstop.
    try:
        from .website import _Extractor
        response = get(f"https://{domain}")
        if response.ok:
            parser = _Extractor()
            try:
                parser.feed(response.text)
            except Exception:
                pass
            host = urllib.parse.urlsplit(response.url).netloc.lower().lstrip("www.")
            scored: dict[str, int] = {}
            for href, anchor in parser.links:
                absolute = urllib.parse.urljoin(response.url, href)
                parts = urllib.parse.urlsplit(absolute)
                if parts.scheme not in ("http", "https"):
                    continue
                if parts.netloc.lower().lstrip("www.") != host:
                    continue
                rank = _score_directory_link(parts.path or "/", anchor)
                if rank > 0:
                    clean = urllib.parse.urlunsplit(
                        (parts.scheme, parts.netloc, parts.path.rstrip("/") or "/", "", ""))
                    scored[clean] = max(rank, scored.get(clean, 0))
            for url, _rank in sorted(scored.items(), key=lambda kv: -kv[1]):
                offer(url)
    except Exception:
        pass

    return urls[:limit]



def crawl(domain: str, name: Name | None = None, org: str = "", budget: int = 6,
          use_search: bool = True, extra_seeds: list | None = None,
          region: str = "") -> tuple[list, list]:
    """Read a site's directories, descending one level when a page is only nav.

    Returns (found phones, notes). The budget is a hard page cap: a directory
    crawl that wanders is a crawl that gets you blocked, and the polite one
    request-per-host delay makes every extra page cost real time.
    """
    # Pages the site harvester already fetched are free to re-read (they are in
    # the HTTP cache) and are often the real listing: a contact page that names
    # four staff and gives each a direct dial is a directory in all but name.
    seeds = list(extra_seeds or [])
    for url in find_directories(domain, name, org, limit=3, use_search=use_search):
        if url not in seeds:
            seeds.append(url)
    if not seeds:
        return [], []
    budget = max(budget, len(seeds) + 2)

    region = region or phonelib.region_for_domain(domain)
    brand = domain.split(".")[0]
    queue = list(seeds)
    seen: set[str] = set()
    found: list = []
    notes: list[str] = []
    descended = 0

    while queue and len(seen) < budget:
        url = queue.pop(0)
        if url in seen:
            continue
        seen.add(url)
        path = urllib.parse.urlsplit(url).path or "/"
        try:
            page = harvest_directory(url, region=region, brand=brand)
        except Exception as exc:
            notes.append(f"{path[:44]} - could not read: {exc}")
            continue
        if page.error:
            # Saying nothing here reads as "the directory was empty" when what
            # actually happened is that we were refused. Those are different
            # answers and the difference is the whole point of the tool.
            notes.append(f"{path[:44]} - {page.error}")
            continue
        named = [phone for phone in page.phones if phone.owner]
        found.extend(page.phones)
        path = urllib.parse.urlsplit(page.url).path or "/"
        notes.append(f"{path[:44]} - {len(page.phones)} numbers, {len(named)} named")
        if named or descended >= 2:
            continue
        # Numbers but nobody named means a footer, not a listing - which is
        # exactly what a landing page looks like. Follow its best onward links.
        descended += 1
        here = path.rstrip("/").rsplit("/", 1)[-1]
        for child in page.links[:4]:
            # /hindi/staff is a translation of /staff, not the listing behind it.
            # A child that repeats the parent's last segment is a sibling.
            if child.rstrip("/").rsplit("/", 1)[-1] == here:
                continue
            if child not in seen and child not in queue:
                queue.append(child)
    return found, notes
