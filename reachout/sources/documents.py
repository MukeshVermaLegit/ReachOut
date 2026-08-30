"""Contact details published in documents.

Public CVs, papers, conference programmes, committee minutes and faculty
handbooks are where people's real contact details end up in the open, far more
often than on a company website. They are nearly all PDFs.

Academic documents in particular use conventions that a naive email regex misses
entirely - the brace group `{alice,bob}@dept.edu` is the big one, and it comes
out of a PDF with the braces rendered as stray `f`/`g` glyphs from a math font.
"""

from __future__ import annotations

import re
import urllib.parse
from dataclasses import dataclass, field

from .. import phones as phonelib
from ..fetch import get
from ..names import Name, slug
from ..pdf import extract_text, looks_like_pdf
from . import search as websearch

_EMAIL = re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9](?:[A-Za-z0-9.\-]*[A-Za-z0-9])?\.[A-Za-z]{2,24}")
_LOCAL_OK = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._%+\-]{1,62}$")


def _plausible(address: str) -> bool:
    """Filter the debris that scanning a whole document inevitably produces."""
    local, _, domain = address.partition("@")
    if not _LOCAL_OK.match(local):
        return False
    labels = domain.split(".")
    if len(labels) < 2 or len(labels[0]) < 2:
        return False           # 'cw@l.id' - a stray letter glued to a fragment
    if not labels[-1].isalpha() or not 2 <= len(labels[-1]) <= 24:
        return False
    return True

# {alice,bob}@dept.edu  and its PDF artefact  f alice,bob g @dept.edu
_GROUP = re.compile(
    r"[{\[]\s*([^{}\[\]@\n]{2,200}?)\s*[}\]]\s*@\s*([A-Za-z0-9\-]+(?:\.[A-Za-z0-9\-]+)*\.[A-Za-z]{2,24})")
# The 'f' and 'g' must stand alone and the locals must contain no whitespace -
# without both constraints the 'f' in a word like "for" starts a match that
# swallows the entire title block.
# The 'f' and 'g' must stand alone, and whitespace inside the group is only
# allowed next to a comma. Without that second rule the 'f' in a word like "for"
# starts a match that swallows the entire title block.
_LOCAL_LIST = r"[A-Za-z0-9][A-Za-z0-9._;\-]{0,62}(?:\s{0,3},\s{0,3}[A-Za-z0-9][A-Za-z0-9._;\-]{0,62}){0,30}"
_MATH_BRACES = re.compile(
    r"(?:^|\s)f\s{0,3}(" + _LOCAL_LIST + r")\s{0,3}g\s{0,3}"
    r"@\s{0,3}([A-Za-z0-9\-]+(?:\.[A-Za-z0-9\-]+)*\.[A-Za-z]{2,24})")


def normalise_document_text(text: str) -> str:
    """Repair the ways PDF extraction breaks contact details apart."""
    if not text:
        return ""
    # LaTeX renders \{ and \} in a math font; they extract as bare f and g.
    text = _MATH_BRACES.sub(lambda m: " {" + m.group(1) + "}@" + m.group(2), text)
    # A local part and its @domain routinely land on different lines.
    text = re.sub(r"([A-Za-z0-9._%+\-])[ \t]*\n[ \t]*(@)", r"\1\2", text)
    text = re.sub(r"(@[A-Za-z0-9.\-]*)[ \t]*\n[ \t]*([A-Za-z0-9\-]+\.[A-Za-z]{2,24})", r"\1\2", text)
    # Phone numbers split across a line break.
    text = re.sub(r"(\+\d{1,3})[ \t]*\n[ \t]*(\d)", r"\1 \2", text)
    return text


def expand_groups(text: str) -> list[tuple[str, str]]:
    """Turn '{alice,bob}@dept.edu' into individual addresses.

    Returns (address, the group it came from) so the caller keeps the context.
    """
    out: list[tuple[str, str]] = []
    for match in _GROUP.finditer(text):
        domain = match.group(2).lower().rstrip(".")
        for part in re.split(r"[,;|]", match.group(1)):
            local = part.strip().strip(".").replace(" ", "")
            if local and _LOCAL_OK.match(local):
                out.append((f"{local}@{domain}".lower(), match.group(0)))
    return out


@dataclass
class DocumentHarvest:
    url: str
    title: str = ""
    text_length: int = 0
    emails: dict = field(default_factory=dict)   # address -> surrounding text
    phones: list = field(default_factory=list)   # list[(e164, context)]
    error: str = ""


def _context(text: str, start: int, end: int, width: int = 90) -> str:
    return " ".join(text[max(0, start - width):end + 40].split())


def contacts_in_text(text: str, domain_filter: str = "", region: str = "IN") -> DocumentHarvest:
    """Every address and number in a block of document text, with context."""
    harvest = DocumentHarvest(url="", text_length=len(text))
    text = normalise_document_text(text)

    for address, group in expand_groups(text):
        if not _plausible(address):
            continue
        if domain_filter and address.split("@")[-1] != domain_filter.lower():
            continue
        index = text.find(group)
        harvest.emails[address] = _context(text, max(index, 0), max(index, 0) + len(group))

    for match in _EMAIL.finditer(text):
        address = match.group(0).lower().strip(".")
        if not _plausible(address):
            continue
        if domain_filter and address.split("@")[-1] != domain_filter.lower():
            continue
        harvest.emails.setdefault(address, _context(text, match.start(), match.end()))

    seen: set[str] = set()
    for match in phonelib_candidates(text):
        parsed = phonelib.normalise(match.group(0), default_region=region)
        if not parsed.valid or parsed.e164 in seen:
            continue
        if phonelib.looks_placeholder(parsed):
            continue
        seen.add(parsed.e164)
        harvest.phones.append((parsed.e164, _context(text, match.start(), match.end())))
    return harvest


# Documents are dense with numbers (years, page ranges, grant IDs), so the
# pattern here insists on a phone-like separator or an explicit country code.
_DOC_PHONE = re.compile(
    r"(?<![\d])(?:"
    r"\+\d{1,3}[\s.\-]?(?:\(\d{1,4}\)[\s.\-]?)?\d{2,5}(?:[\s.\-]?\d{2,5}){1,3}"
    r"|\(\d{3}\)[\s.\-]?\d{3}[\s.\-]?\d{4}"
    r"|\b0\d{2,4}[\s.\-]\d{6,8}"
    r"|\b[6-9]\d{4}[\s.\-]\d{5}"
    r")(?![\d])")


def phonelib_candidates(text: str):
    # Only look near words that mark a contact detail; documents are full of
    # digit strings that merely look like numbers.
    return _DOC_PHONE.finditer(text)


def harvest_document(url: str, domain_filter: str = "") -> DocumentHarvest:
    """Fetch one document (PDF or HTML) and pull contact details out of it."""
    response = get(url, check_robots=True)
    if not response.ok and not response.text:
        return DocumentHarvest(url=url, error=response.error or f"HTTP {response.status}")

    raw = response.text.encode("latin-1", "replace")
    if looks_like_pdf(raw):
        text = extract_text(raw)
    else:
        stripped = re.sub(r"(?s)<(script|style).*?</\1>", " ", response.text)
        text = re.sub(r"(?s)<[^>]+>", " ", stripped)

    region = phonelib.region_for_domain(urllib.parse.urlsplit(response.url).netloc)
    harvest = contacts_in_text(text, domain_filter=domain_filter, region=region)
    harvest.url = response.url
    return harvest


_DOC_HINT = re.compile(r"\.pdf($|\?)|/cv|/resume|curriculum", re.I)


def find_documents(name: Name, org: str = "", limit: int = 4) -> list[str]:
    """Search for public documents likely to carry this person's details."""
    queries = [
        f'"{name.display}" {org} filetype:pdf'.strip(),
        f'"{name.display}" {org} (cv OR resume OR "curriculum vitae")'.strip(),
    ]
    urls: list[str] = []
    for query in queries:
        try:
            hits = websearch.search(query, count=8)
        except Exception:
            continue
        for hit in hits:
            if not hit.url.startswith("http"):
                continue
            if _DOC_HINT.search(hit.url) and hit.url not in urls:
                urls.append(hit.url)
            if len(urls) >= limit:
                return urls
    return urls


def mentions_person(harvest_text: str, name: Name) -> bool:
    key = slug(harvest_text)
    return bool(name.first and name.last and name.first in key and name.last in key)
