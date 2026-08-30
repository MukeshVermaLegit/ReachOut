"""Harvest publicly published contact details from a company's own website.

Team, about and contact pages routinely publish staff addresses. When a page
gives us both a person's name and an address, we get a *confirmed* pattern
sample - the strongest free training data there is, because the pairing is
self-validating: the name must actually render to the local-part.
"""

from __future__ import annotations

import json
import re
import urllib.parse
from collections import deque
from dataclasses import dataclass, field
from html.parser import HTMLParser

from .. import phones as phonelib
from ..fetch import get
from ..names import Name, parse as parse_name, slug
from ..patterns import matching_reading

EMAIL_RE = re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9](?:[A-Za-z0-9.\-]*[A-Za-z0-9])?\.[A-Za-z]{2,24}")

# "jane [at] acme [dot] com" and friends.
_OBFUSCATED = [
    (re.compile(r"\s*[\[\(\{]\s*at\s*[\]\)\}]\s*", re.I), "@"),
    (re.compile(r"\s*[\[\(\{]\s*dot\s*[\]\)\}]\s*", re.I), "."),
    (re.compile(r"\s+&#64;\s*"), "@"),
    (re.compile(r"\s*&#46;\s*"), "."),
]

CONTACT_PATHS = [
    "", "/about", "/about-us", "/team", "/our-team", "/people", "/contact",
    "/contact-us", "/leadership", "/company", "/staff", "/who-we-are",
]

# Mailboxes that belong to a department, not a person.
ROLE_LOCALS = {
    "info", "hello", "hi", "hey", "contact", "support", "help", "sales", "admin", "office",
    "team", "press", "media", "careers", "jobs", "hr", "recruiting", "billing",
    "accounts", "legal", "privacy", "security", "abuse", "postmaster", "webmaster",
    "noreply", "no-reply", "donotreply", "marketing", "partners", "enquiries",
    "inquiries", "general", "mail", "email", "service", "customerservice", "feedback",
    # Shared mailboxes that read like names but reach a group, not a person.
    "founder", "founders", "ceo", "cto", "cfo", "coo", "reach", "connect", "partnerships",
    "biz", "business", "invest", "investors", "ir", "pr", "talent", "people", "community",
    "events", "newsletter", "subscribe", "orders", "booking", "bookings", "reservations",
    "finance", "payments", "hiring", "apply", "ask", "query", "queries", "student",
    "admissions", "registrar", "alumni", "dean", "principal", "office365",
    # Indian institutions publish these constantly, and each one otherwise reads
    # as a person and teaches the domain a pattern built from a job title.
    "vc", "pvc", "fo", "hod", "tpo", "provost", "proctor", "warden", "controller",
    "placement", "placements", "training", "exam", "exams", "examination",
    "academics", "academic", "research", "library", "librarian", "hostel",
    "transport", "admission", "grievance", "antiragging", "nss", "ncc",
    "international", "outreach", "director", "chairman", "secretary", "principal",
}

_NAME_TEXT_RE = re.compile(r"^[A-Z][a-z'’\-]{1,20}(?: [A-Z][a-z'’\-]{1,20}){1,3}$")
_STOPWORDS = {"privacy policy", "terms of service", "contact us", "about us", "our team",
              "read more", "learn more", "get started", "sign in", "log in", "cookie policy"}

# Capitalised English words that start marketing copy, not people. Filtering on the
# first token removes most of the false "names" scraped from nav bars and headings.
_NOT_A_FIRST_NAME = {
    "the", "our", "your", "their", "his", "her", "its", "a", "an", "all", "any", "more",
    "most", "less", "new", "top", "best", "free", "get", "join", "learn", "read", "see",
    "start", "try", "build", "make", "meet", "find", "why", "how", "what", "when", "where",
    "who", "can", "will", "we", "you", "they", "it", "is", "are", "was", "were", "be",
    "book", "watch", "download", "explore", "discover", "customer", "enterprise", "product",
    "products", "solution", "solutions", "pricing", "about", "contact", "careers", "team",
    "teams", "company", "platform", "features", "resources", "support", "help", "docs",
    "blog", "news", "press", "privacy", "terms", "cookie", "sign", "log", "create", "add",
    "microsoft", "google", "apple", "amazon", "slack", "jira", "zoom", "executive", "senior",
    "junior", "chief", "head", "vice", "managing", "general", "global", "united", "north",
    "south", "east", "west", "first", "second", "next", "last", "one", "two", "three",
    # Words that open a sentence, not a name: "A note from Founders", "Dear Team"
    "from", "note", "message", "letter", "word", "dear", "hi", "hey", "thanks", "thank",
    "welcome", "introducing", "meet", "say", "talk", "call", "email", "reach", "follow",
}


# Honorifics printed in front of a name, especially in academic directories.
# The name regex allows no dots, so "Dr. Ramesh Kumar" fails to match until the
# prefix comes off.
_HONORIFIC_RE = re.compile(
    r"^(?:dr|prof|professor|mr|mrs|ms|miss|shri|smt|sri|er|ar|capt|col|maj|rev|hon"
    r"|adv|ca|cs|fr|sr)\.?\s+", re.I)

# Job titles and column headers that pass the "two capitalised words" test but
# name a role, not a person. Without these, "Assistant Professor" reads as a
# person and every faculty row looks ambiguous.
_TITLE_WORDS = {
    "professor", "prof", "assistant", "associate", "lecturer", "reader", "instructor",
    "dean", "principal", "registrar", "director", "manager", "officer", "engineer",
    "scientist", "fellow", "coordinator", "coordinators", "secretary", "chairperson",
    "chairman", "chair", "warden", "librarian", "controller", "deputy", "joint",
    "additional", "emeritus", "adjunct", "visiting", "distinguished", "administrative",
    "technical", "clerical", "academic", "research", "teaching", "department",
    "departments", "school", "centre", "center", "faculty", "staff", "office", "division",
    "section", "unit", "cell", "laboratory", "institute", "university", "college",
    "campus", "designation", "name", "phone", "telephone", "mobile", "extension",
    "extn", "email", "mail", "address", "room", "cabin", "building", "block", "floor",
    "sno", "srno", "position", "role", "title", "profile", "details", "list", "member",
    "members", "scholar", "scholars", "student", "students", "intern", "consultant",
    "advisor", "adviser", "trustee", "governor", "president", "secretariat",
    # Site-navigation nouns. Institutional sites are full of two-capitalised-word
    # headings, and every one that survives here is a candidate owner for a number.
    "programme", "programmes", "program", "programs", "placement", "placements",
    "admission", "admissions", "curriculum", "syllabus", "examination", "examinations",
    "result", "results", "achievement", "achievements", "publication", "publications",
    "activity", "activities", "facility", "facilities", "infrastructure", "overview",
    "highlight", "highlights", "gallery", "event", "events", "notice", "notices",
    "circular", "circulars", "tracker", "representative", "representatives", "process",
    "offered", "quick", "links", "link", "engineering", "science", "sciences",
    "technology", "management", "studies", "intelligence", "computing", "electrical",
    "mechanical", "civil", "chemical", "electronics", "downloads", "download",
    "announcement", "announcements", "testimonial", "testimonials", "collaboration",
    "collaborations", "ranking", "rankings", "accreditation", "scholarship",
    "scholarships", "hostel", "transport", "library", "alumni", "recruiters",
    # Job descriptions that read as two capitalised words. "Data Entry Operator"
    # sat in a real VIT row and was bound to a number as if it were a person.
    "data", "entry", "operator", "operators", "clerk", "attendant", "technician",
    "supervisor", "superintendent", "analyst", "developer", "designer", "architect",
    "administrator", "assistants", "incharge", "charge", "duty", "shift", "helpdesk",
    "desk", "counter", "reception", "receptionist", "security", "guard", "driver",
    # Company-name words. A contact page listing distributors binds each firm's
    # name to its number exactly as if the firm were a person.
    "technologies", "corporation", "corp", "incorporated", "industries", "enterprise",
    "enterprises", "solutions", "systems", "controls", "services", "associates",
    "partners", "holdings", "group", "company", "works", "waterworks", "products",
    "equipment", "supply", "supplies", "distributors", "distribution", "ventures",
    "international", "national", "regional", "limited", "private", "trading",
}


def person_names(chunks, brand: str = "") -> list:
    """Text chunks that plausibly name a person, in the order they appeared.

    Deliberately conservative in both directions: honorifics come off so real
    names are not missed, and role words are rejected so job titles are not
    mistaken for people. A directory row binds a number to a name only when
    exactly one survives, so a false positive here costs a real attribution.
    """
    out: list[str] = []
    brand = (brand or "").lower()
    for chunk in chunks:
        chunk = _HONORIFIC_RE.sub("", (chunk or "").strip()).strip()
        if not chunk or len(chunk) > 48 or chunk.lower() in _STOPWORDS:
            continue
        if not _NAME_TEXT_RE.match(chunk):
            continue
        words = [w.lower().strip("'\u2019-") for w in chunk.split()]
        if words[0] in _NOT_A_FIRST_NAME or words[-1] in _NOT_A_FIRST_NAME:
            continue
        if any(w in _TITLE_WORDS for w in words):
            continue
        if brand and brand in words:
            continue
        out.append(chunk)
    return out


def _decode_cfemail(token: str) -> str:
    """Cloudflare's email obfuscation: hex string, first byte is the XOR key."""
    try:
        raw = bytes.fromhex(token)
    except ValueError:
        return ""
    if len(raw) < 2:
        return ""
    key = raw[0]
    return "".join(chr(byte ^ key) for byte in raw[1:])


@dataclass
class PageHarvest:
    url: str
    emails: dict = field(default_factory=dict)   # address -> anchor/nearby text
    names: list = field(default_factory=list)    # candidate person names
    phones: list = field(default_factory=list)   # list[FoundPhone]
    error: str = ""


@dataclass
class FoundPhone:
    raw: str
    context: str = ""        # words printed around it
    owner: str = ""          # a person's name, when the markup ties one to it
    scope: str = "page"      # 'person' | 'organization' | 'page'
    how: str = ""            # 'tel-link' | 'json-ld' | 'microdata' | 'text'
    source_url: str = ""
    # Addresses published in the same block. Kept apart from `context` because
    # context is truncated for display and an identifier must not be lost to a
    # slice: when the subject's own work address is in here, the page has stated
    # whose number this is rather than merely printed the two things near
    # each other.
    emails: list = field(default_factory=list)
    provider: str = ""       # which registered provider produced the sighting


class _Extractor(HTMLParser):
    """Pulls mailto links (with their anchor text), Cloudflare-obfuscated
    addresses, and short capitalised strings that look like people's names."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.mailtos: list[tuple[str, str]] = []   # (address, anchor text)
        self.cfemails: list[str] = []
        self.text_chunks: list[str] = []
        self.tels: list[tuple[str, str, str]] = []  # (number, anchor, preceding text)
        self.links: list[tuple[str, str]] = []      # (href, anchor text)
        self.jsonld: list[str] = []
        self.microtels: list[tuple[str, str]] = []  # (number, preceding text)
        self._stack: list[str] = []
        self._pending_href = ""
        self._pending_tel = ""
        self._buffer: list[str] = []
        self._skip_depth = 0
        self._recent: deque = deque(maxlen=8)
        self._in_jsonld = False
        self._jsonld_buffer: list[str] = []

    def handle_starttag(self, tag, attrs):
        attributes = dict(attrs)
        if tag == "script" and "ld+json" in (attributes.get("type") or "").lower():
            self._in_jsonld = True
            self._jsonld_buffer = []
        if tag in ("script", "style", "noscript"):
            self._skip_depth += 1
        if (attributes.get("itemprop") or "").lower() == "telephone":
            value = attributes.get("content") or ""
            if value:
                self.microtels.append((value, " ".join(self._recent)))
        if "data-cfemail" in attributes:
            decoded = _decode_cfemail(attributes["data-cfemail"] or "")
            if decoded:
                self.cfemails.append(decoded)
        if tag == "a":
            href = (attributes.get("href") or "").strip()
            self._pending_href = href
            self._pending_tel = " ".join(self._recent)
            self._buffer = []
        self._stack.append(tag)

    def handle_endtag(self, tag):
        if tag == "script" and self._in_jsonld:
            self.jsonld.append("".join(self._jsonld_buffer))
            self._in_jsonld = False
            self._jsonld_buffer = []
        if tag in ("script", "style", "noscript") and self._skip_depth:
            self._skip_depth -= 1
        if tag == "a":
            text = " ".join(" ".join(self._buffer).split())
            lowered = self._pending_href.lower()
            if lowered.startswith("mailto:"):
                address = urllib.parse.unquote(self._pending_href[7:].split("?")[0]).strip()
                if address:
                    self.mailtos.append((address, text))
            elif lowered.startswith("tel:") or lowered.startswith("callto:"):
                number = urllib.parse.unquote(self._pending_href.split(":", 1)[1]).strip()
                if number:
                    self.tels.append((number, text, self._pending_tel))
            elif self._pending_href and not lowered.startswith(("javascript:", "#", "data:")):
                self.links.append((self._pending_href, text))
            self._pending_href = ""
            self._buffer = []
        if self._stack and tag in self._stack:
            while self._stack:
                if self._stack.pop() == tag:
                    break

    def handle_data(self, data):
        if self._in_jsonld:
            self._jsonld_buffer.append(data)
            return
        if self._skip_depth:
            return
        stripped = data.strip()
        if not stripped:
            return
        self._buffer.append(stripped)
        self.text_chunks.append(stripped)
        self._recent.append(stripped)


# --------------------------------------------------------------------- phones
# Deliberately strict: loose digit matching turns dates, order numbers and
# tracking IDs into "phone numbers". Everything here is validated afterwards.
_PHONE_TEXT_RE = re.compile(
    r"(?<![\d])(?:"
    r"\+\d{1,3}[\s.\-]?(?:\(\d{1,4}\)[\s.\-]?)?\d{2,5}(?:[\s.\-]?\d{2,5}){1,3}"  # +cc ...
    r"|\(\d{3}\)[\s.\-]?\d{3}[\s.\-]?\d{4}"                                          # (415) 555-2671
    r"|\b0\d{2,4}[\s.\-]\d{6,8}"                                                      # 022-67891234
    r"|\b[6-9]\d{4}[\s.\-]\d{5}"                                                      # 98765 43210
    r")(?![\d])")

_PERSON_TYPES = {"person"}
_ORG_TYPES = {"organization", "corporation", "localbusiness", "educationalorganization",
              "ngo", "governmentorganization", "postaladdress", "contactpoint"}


def _walk_jsonld(node, out: list, inherited: str = "") -> None:
    """Collect (number, owner_name, scope, context) from a JSON-LD tree."""
    if isinstance(node, list):
        for item in node:
            _walk_jsonld(item, out, inherited)
        return
    if not isinstance(node, dict):
        return

    raw_type = node.get("@type") or node.get("type") or ""
    types = [raw_type] if isinstance(raw_type, str) else list(raw_type or [])
    lowered = {str(t).lower() for t in types}
    name = node.get("name") if isinstance(node.get("name"), str) else ""

    scope = inherited
    if lowered & _PERSON_TYPES:
        scope = "person"
    elif lowered & _ORG_TYPES:
        scope = "organization"

    telephone = node.get("telephone")
    numbers = telephone if isinstance(telephone, list) else [telephone]
    # A Person node that carries both an email and a telephone is the strongest
    # statement a page can make about whose number it is, so the address travels
    # with the number instead of being dropped as unrelated markup.
    raw_email = node.get("email")
    node_emails = [e.strip().lower().replace("mailto:", "")
                   for e in (raw_email if isinstance(raw_email, list) else [raw_email])
                   if isinstance(e, str) and e.strip()]
    for number in numbers:
        if isinstance(number, str) and number.strip():
            hint = node.get("contactType") or node.get("jobTitle") or ""
            out.append((number.strip(), name if scope == "person" else "",
                        scope or "page",
                        " ".join([str(hint)] + node_emails).strip(), node_emails))

    for key, value in node.items():
        if key in ("telephone", "@type", "type"):
            continue
        if isinstance(value, (dict, list)):
            _walk_jsonld(value, out, scope)


def _phones_from_page(parser: "_Extractor", body: str, region: str = "IN") -> list:
    """Every number on a page, tagged with how it was found and what surrounds it.

    `region` decides how a bare national number is read. Guessing it here rather
    than passing it in is how '617-253-1000' on an MIT page becomes an Indian
    mobile - so the caller, which knows the domain, supplies it.
    """
    found: list[FoundPhone] = []
    seen: set[str] = set()

    def push(raw: str, context: str, owner: str, scope: str, how: str,
             emails: list | None = None) -> None:
        parsed = phonelib.normalise(raw, default_region=region)
        if not parsed.valid:
            return
        if parsed.e164 in seen:
            return
        seen.add(parsed.e164)
        window = " ".join((context or "").split())
        found.append(FoundPhone(raw=parsed.e164, context=window[:160],
                                owner=owner, scope=scope, how=how,
                                emails=list(emails or EMAIL_RE.findall(window))))

    # 1. tel: links - the phone equivalent of mailto:, and the most reliable.
    for number, anchor, before in parser.tels:
        push(number, f"{before} {anchor}", "", "page", "tel-link")

    # 2. JSON-LD - the only source that states whose number it is.
    for block in parser.jsonld:
        try:
            data = json.loads(block)
        except ValueError:
            continue
        entries: list = []
        _walk_jsonld(data, entries)
        for number, owner, scope, hint, node_emails in entries:
            push(number, hint, owner, scope, "json-ld", emails=node_emails)

    # 3. microdata
    for number, before in parser.microtels:
        push(number, before, "", "page", "microdata")

    # 4. plain text, with the words around it kept for classification
    for match in _PHONE_TEXT_RE.finditer(body):
        start, end = max(0, match.start() - 70), min(len(body), match.end() + 40)
        push(match.group(0), body[start:end], "", "page", "text")

    return found


def _clean_email(raw: str) -> str:
    address = (raw or "").strip().strip(".,;:<>()[]'\"").lower()
    address = address.replace("mailto:", "")
    if address.count("@") != 1:
        return ""
    local, _, domain = address.partition("@")
    if not local or not domain or "." not in domain:
        return ""
    # Strip trailing junk picked up from minified markup.
    if domain.endswith("."):
        domain = domain.rstrip(".")
    for ext in (".png", ".jpg", ".jpeg", ".gif", ".svg", ".webp", ".css", ".js"):
        if domain.endswith(ext):
            return ""
    return f"{local}@{domain}"


def _deobfuscate(text: str) -> str:
    for pattern, replacement in _OBFUSCATED:
        text = pattern.sub(replacement, text)
    return text


# Words that mark a page as worth reading, in the URL path or the link text.
# Matched against whole URL segments, not substrings: "management" as a loose
# substring drags in product pages like /solution/asset-management.
_PAGE_HINTS = {
    "contact": 10, "contact-us": 10, "contactus": 10, "get-in-touch": 9, "reach-us": 9,
    "team": 9, "our-team": 9, "people": 8, "leadership": 8, "founders": 8, "founder": 8,
    "staff": 7, "directory": 7, "faculty": 7, "about": 6, "about-us": 6, "who-we-are": 6,
    "our-story": 5, "connect": 5, "imprint": 6, "impressum": 6,
}
_PAGE_SKIP = re.compile(
    r"\.(pdf|jpe?g|png|gif|svg|zip|mp4|webp|css|js)$|/(blog|news|careers|jobs|privacy|"
    r"terms|cookie|login|signin|signup|cart|pricing|docs|help)(/|$)", re.I)


def _score_link(path: str, anchor: str) -> int:
    segments = [s for s in path.lower().replace("_", "-").strip("/").split("/") if s]
    anchor_words = set(re.split(r"[^a-z]+", anchor.lower()))
    score = 0
    for segment in segments:
        if segment in _PAGE_HINTS:                      # whole segment: /contact-us
            score = max(score, _PAGE_HINTS[segment])
    # Only trust link text that reads like a nav label. Marketing sentences say
    # "Thousands of people use Basecamp" and would otherwise score as a team page.
    if 0 < len([w for w in anchor_words if w]) <= 4:
        for phrase, weight in _PAGE_HINTS.items():
            if "-" not in phrase and phrase in anchor_words:
                score = max(score, weight - 1)
    # Shallow pages beat deep ones at the same relevance.
    return score - max(0, len(segments) - 1) if score else 0


def discover_pages(domain: str, limit: int = 6) -> tuple[list[str], dict]:
    """Read the homepage and follow the links it actually offers.

    Guessing fixed paths misses sites that nest their pages ('/company/contact-us'),
    which is most of them. Returns (urls, diagnostics).
    """
    domain = domain.lower().strip().lstrip("@")
    base = f"https://{domain}"
    info = {"client_rendered": False, "homepage_ok": False, "followed": []}

    response = get(base)
    if not response.ok:
        return [base], info
    info["homepage_ok"] = True

    parser = _Extractor()
    try:
        parser.feed(response.text)
    except Exception:
        pass

    visible = " ".join(" ".join(parser.text_chunks).split())
    info["client_rendered"] = looks_client_rendered(response.text, visible)

    root = urllib.parse.urlsplit(response.url)
    host = root.netloc.lower()
    scored: dict[str, int] = {}
    for href, anchor in parser.links:
        absolute = urllib.parse.urljoin(response.url, href)
        parts = urllib.parse.urlsplit(absolute)
        if parts.scheme not in ("http", "https"):
            continue
        if parts.netloc.lower().lstrip("www.") != host.lstrip("www."):
            continue
        if _PAGE_SKIP.search(parts.path or "/"):
            continue
        path = parts.path.rstrip("/") or "/"
        clean = urllib.parse.urlunsplit((parts.scheme, parts.netloc, path, "", ""))
        rank = _score_link(path, anchor)
        if rank > 0 and rank > scored.get(clean, 0):
            scored[clean] = rank

    ordered = [url for url, _rank in sorted(scored.items(), key=lambda kv: -kv[1])]
    info["followed"] = ordered[:limit - 1]

    # Keep the classic guesses as a backstop for sites with no usable navigation.
    pages = [response.url] + ordered[:limit - 1]
    if len(pages) < limit:
        for path in CONTACT_PATHS[1:]:
            candidate = base + path
            if candidate not in pages:
                pages.append(candidate)
            if len(pages) >= limit:
                break
    return pages[:limit], info


_JS_SHELL_HINTS = re.compile(
    r"you need to enable javascript|please enable javascript|<div id=\"root\"|"
    r"<div id=\"__next\"|<div id=\"app\"|ng-version", re.I)


def looks_client_rendered(html_text: str, visible_text: str) -> bool:
    """True when a page ships a JavaScript shell instead of content.

    Worth knowing: 'no numbers found' and 'no readable page' are very different
    answers, and only one of them means the information is not out there.
    """
    if len(visible_text) > 400:
        return False
    return bool(_JS_SHELL_HINTS.search(html_text)) or (
        len(visible_text) < 120 and html_text.count("<script") >= 1)


def harvest_page(url: str, domain_filter: str = "", region: str = "") -> PageHarvest:
    response = get(url)
    if not response.ok:
        return PageHarvest(url=url, error=response.error or f"HTTP {response.status}")

    parser = _Extractor()
    try:
        parser.feed(response.text)
    except Exception:
        pass  # malformed markup - keep whatever we parsed

    found: dict[str, str] = {}

    def record(address: str, context: str) -> None:
        address = _clean_email(address)
        if not address:
            return
        if domain_filter and address.split("@")[-1] != domain_filter.lower():
            return
        if address not in found or (context and not found[address]):
            found[address] = context

    for address, anchor in parser.mailtos:
        record(address, anchor)
    for address in parser.cfemails:
        record(address, "")

    body = _deobfuscate(response.text)
    for match in EMAIL_RE.finditer(body):
        record(match.group(0), "")

    brand = domain_filter.split(".")[0].lower() if domain_filter else ""
    names = person_names(parser.text_chunks, brand)
    names.extend(person_names(
        [anchor for _address, anchor in parser.mailtos if anchor and "@" not in anchor],
        brand))

    # Strip tags before text-scanning for numbers so markup cannot create digits.
    visible = " ".join(parser.text_chunks)
    if not region:
        host = urllib.parse.urlsplit(url).netloc
        region = phonelib.region_for_domain(domain_filter or host)
    phones = _phones_from_page(parser, visible, region=region)

    return PageHarvest(url=response.url, emails=found, phones=phones,
                       names=list(dict.fromkeys(names)))


def harvest_site(domain: str, max_pages: int = 6, paths: list[str] | None = None,
                 region: str = "") -> dict:
    """Walk a small set of likely contact pages on one domain.

    `region` decides how a bare national number is read. The domain implies it
    for most suffixes, but a caller who knows the country - because the subject's
    own address told them - should say so rather than let '.example' fall back.
    """
    domain = domain.lower().strip().lstrip("@")
    base = f"https://{domain}"
    emails: dict[str, str] = {}
    names: list[str] = []
    pages: list[str] = []
    errors: list[str] = []
    phones: dict[str, FoundPhone] = {}
    phone_pages: dict[str, str] = {}
    info: dict = {}

    if paths is not None:
        targets = [base + path for path in paths][:max_pages]
    else:
        targets, info = discover_pages(domain, limit=max_pages)

    region = region or phonelib.region_for_domain(domain)
    for url in targets:
        page = harvest_page(url, domain_filter=domain, region=region)
        if page.error:
            errors.append(f"{urllib.parse.urlsplit(url).path or '/'}: {page.error}")
            continue
        pages.append(page.url)
        for address, context in page.emails.items():
            if address not in emails or (context and not emails[address]):
                emails[address] = context
        names.extend(page.names)
        for phone in page.phones:
            # Keep the richest sighting: an owner or a real scope beats a bare hit.
            best = phones.get(phone.raw)
            if not best or (phone.owner and not best.owner) or \
                    (phone.scope != "page" and best.scope == "page"):
                phones[phone.raw] = phone
                phone_pages[phone.raw] = page.url

    unique_names = list(dict.fromkeys(names))
    return {"domain": domain, "emails": emails, "names": unique_names,
            "phones": list(phones.values()), "phone_pages": phone_pages,
            "pages": pages, "errors": errors,
            "client_rendered": bool(info.get("client_rendered"))}


# Machine accounts commit constantly, so they dominate a harvest if left in.
_SERVICE_SUFFIXES = ("svc", "service", "bot", "ci", "cd", "ops", "daemon", "agent", "job")


def is_role_account(address: str) -> bool:
    local = address.split("@")[0].lower()
    if local in ROLE_LOCALS or local.replace(".", "").replace("-", "") in ROLE_LOCALS:
        return True
    parts = [p for p in re.split(r"[.\-_]+", local) if p]
    # 'security-svc' and 'ship-help-github' reach a system, not a person.
    return bool(parts) and (parts[-1] in _SERVICE_SUFFIXES
                            or any(p in ROLE_LOCALS for p in parts))


def pair_samples(emails: dict, names: list[str], domain: str) -> list[tuple[str, str, str, str]]:
    """Match harvested names to harvested addresses.

    A pairing only counts when some template actually renders the name into the
    local-part, so this cannot invent samples: it either fits or it does not.
    Returns (first, last, local, template).
    """
    parsed: list[Name] = []
    for raw in names:
        candidate = parse_name(raw)
        if candidate.ok:
            parsed.append(candidate)

    samples: list[tuple[str, str, str, str]] = []
    seen: set[str] = set()
    for address, context in emails.items():
        if address.split("@")[-1] != domain.lower() or is_role_account(address):
            continue
        local = address.split("@")[0]
        if local in seen:
            continue
        # The anchor text sitting on the mailto link is the best name candidate -
        # but only once it has passed the same filter every other harvested name
        # goes through. Untested, "Finance Officer" and the address itself both
        # read as people, and a role mailbox teaches the domain a false pattern.
        pool = parsed
        if context:
            for cleaned in person_names([context]):
                from_context = parse_name(cleaned)
                if from_context.ok:
                    pool = [from_context] + parsed
                break
        for candidate in pool:
            hit = matching_reading(local, candidate)
            if hit:
                reading, template = hit
                samples.append((reading.first, reading.last, local, template))
                seen.add(local)
                break
    return samples
