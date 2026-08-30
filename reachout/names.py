"""Name parsing and normalisation.

Everything downstream (pattern inference, candidate generation, scoring) works on
normalised ASCII tokens, so this module is the single place that has to deal with
accents, particles, suffixes and the many ways humans write their own names.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field

SUFFIXES = {
    "jr", "jnr", "sr", "snr", "ii", "iii", "iv", "v",
    "phd", "ph", "md", "mba", "msc", "bsc", "cfa", "cpa", "esq", "dds", "dvm",
}
TITLES = {
    "mr", "mrs", "ms", "miss", "dr", "prof", "professor", "sir", "madam",
    "er", "shri", "smt", "capt", "col", "lt", "rev",
}
# Particles keep their place inside a surname ("van der Berg" -> vanderberg)
PARTICLES = {
    "van", "von", "de", "del", "della", "der", "den", "da", "di", "dos", "das",
    "du", "la", "le", "lo", "bin", "ibn", "al", "ter", "ten", "op", "af",
}

# Suffixes that mean the token before them is a domain, not a surname.
_TLDS = {
    "com", "org", "net", "io", "dev", "co", "in", "me", "ai", "app", "xyz", "tech",
    "info", "biz", "us", "uk", "eu", "de", "fr", "nl", "ca", "au", "jp", "cn", "br",
    "sh", "gg", "ly", "to", "cc", "tv", "pro", "site", "online", "cloud",
}

_NON_ALPHA = re.compile(r"[^a-z]+")
_SPLIT = re.compile(r"[\s._\-,]+")


def deaccent(text: str) -> str:
    """Strip diacritics: 'José Muñoz' -> 'Jose Munoz'."""
    decomposed = unicodedata.normalize("NFKD", text)
    stripped = "".join(ch for ch in decomposed if not unicodedata.combining(ch))
    # A few letters do not decompose (ø, ß, đ...) - map the common ones.
    table = {"ø": "o", "Ø": "O", "ß": "ss", "æ": "ae", "Æ": "AE",
             "œ": "oe", "Œ": "OE", "đ": "d", "Đ": "D", "ł": "l", "Ł": "L",
             "ð": "d", "þ": "th", "ı": "i"}
    return "".join(table.get(ch, ch) for ch in stripped)


def slug(text: str) -> str:
    """Lowercase, deaccented, letters only. Used for every comparison."""
    return _NON_ALPHA.sub("", deaccent(text).lower())


# German-family umlauts have two valid ASCII readings and both occur in real
# addresses. `deaccent` gives the strip reading (Jürgen -> jurgen); this gives
# the expansion (Jürgen -> juergen), which is the one German, Austrian and Swiss
# employers actually issue - juergen.mueller@sap.com, never jurgen.muller@.
# Neither can be ruled out from the name alone, so both are offered and the
# domain's own pattern decides, exactly as with the surname splits below.
_UMLAUT_EXPAND = {"\u00e4": "ae", "\u00f6": "oe", "\u00fc": "ue",
                  "\u00c4": "Ae", "\u00d6": "Oe", "\u00dc": "Ue"}


def expand_umlauts(text: str) -> str:
    """'Jürgen Müller' -> 'Juergen Mueller'. Unchanged if there are none."""
    return "".join(_UMLAUT_EXPAND.get(ch, ch) for ch in text or "")


def has_umlaut(text: str) -> bool:
    return any(ch in _UMLAUT_EXPAND for ch in text or "")


def token_groups(name: "Name") -> list[list[str]]:
    """First and last name, each as its list of plausible ASCII spellings.

    A group per name part rather than one flat list: 'jurgen' and 'juergen' are
    two spellings of one name, so a local part needs to match *one* of them, not
    both. Flattening them would make "contains the full name" unsatisfiable for
    every umlaut name.
    """
    groups: list[list[str]] = []
    alt = parse(expand_umlauts(name.raw)) if has_umlaut(name.raw) else None
    for primary, expanded in ((name.first, alt.first if alt else ""),
                              (name.last, alt.last if alt else "")):
        forms = [f for f in (primary, expanded) if f and len(f) > 2]
        if forms:
            groups.append(list(dict.fromkeys(forms)))
    return groups


@dataclass
class Name:
    raw: str
    first: str = ""
    middle: str = ""
    last: str = ""
    tokens: list[str] = field(default_factory=list)
    # True when this reading differs from the primary only in how the same name
    # is spelled in ASCII (Jürgen -> jurgen / juergen), not in which tokens were
    # taken as the surname. Generation damps competing *structural* readings on
    # the grounds that the primary split is likelier; that reasoning does not
    # apply here, where both spellings are the same person's actual name and a
    # German-speaking employer is if anything likelier to use the expansion.
    spelling_variant: bool = False

    @property
    def ok(self) -> bool:
        return bool(self.first and self.last)

    @property
    def f(self) -> str:
        return self.first[:1]

    @property
    def l(self) -> str:  # noqa: E741 - mirrors the {l} template token
        return self.last[:1]

    @property
    def m(self) -> str:
        return self.middle[:1]

    @property
    def display(self) -> str:
        return " ".join(p.capitalize() for p in [self.first, self.middle, self.last] if p)

    def as_dict(self) -> dict:
        return {"raw": self.raw, "first": self.first, "middle": self.middle,
                "last": self.last, "tokens": self.tokens}


def parse(raw: str) -> Name:
    """Parse a human name into normalised first/middle/last tokens.

    Handles 'Smith, John', titles, suffixes, and surname particles.
    """
    raw = (raw or "").strip()
    if not raw:
        return Name(raw="")
    # An address is not a name. Harvested anchor text frequently is one, and
    # splitting it on dots makes the TLD the surname: 'sasikumar.p@vit.ac.in'
    # would otherwise read as first='sasikumar', last='in'.
    if "@" in raw:
        return Name(raw=raw)
    # Nor is a bare domain. Developers put their own site in the GitHub display
    # name, and 'rohankokane.dev' read as first='rohankokane', last='dev'. The
    # space test keeps real surnames safe - 'Kapil Dev' is a name, and a dotted
    # handle like 'john.smith' still parses, because 'smith' is not a TLD.
    if not any(ch.isspace() for ch in raw) and raw.rsplit(".", 1)[-1].lower() in _TLDS:
        return Name(raw=raw)

    working = raw
    # "Smith, John" -> "John Smith" (but not "John Smith, PhD")
    if "," in working:
        head, _, tail = working.partition(",")
        tail_tokens = [slug(t) for t in _SPLIT.split(tail) if slug(t)]
        if tail_tokens and not set(tail_tokens) <= (SUFFIXES | TITLES):
            working = f"{tail.strip()} {head.strip()}"

    parts = [slug(p) for p in _SPLIT.split(working)]
    parts = [p for p in parts if p]
    parts = [p for p in parts if p not in TITLES]
    while parts and parts[-1] in SUFFIXES:
        parts.pop()
    # A trailing single letter is KEPT. Across much of India it is the surname
    # initial - "Ramesh Kumar S" is ramesh.s@, and popping the S made that
    # address unreachable by any template. variants() still offers the reading
    # where the letter is noise, so "John Smith X" remains covered.

    if not parts:
        return Name(raw=raw)
    if len(parts) == 1:
        return Name(raw=raw, first=parts[0], tokens=parts)

    first = parts[0]
    rest = parts[1:]

    # Fold particles into the surname: ['van','der','berg'] -> 'vanderberg'
    pidx = next((i for i, tok in enumerate(rest) if tok in PARTICLES and i < len(rest) - 1), None)
    if pidx is not None:
        surname = "".join(rest[pidx:])
        middle = rest[pidx - 1] if pidx > 0 else ""
        return Name(raw=raw, first=first, middle=middle, last=surname, tokens=parts)

    last = rest[-1]
    middle = rest[0] if len(rest) > 1 else ""
    return Name(raw=raw, first=first, middle=middle, last=last, tokens=parts)


def variants(name: Name) -> list[Name]:
    """Alternative first/last splits for a name, primary reading first.

    'Maria Garcia Lopez' is ambiguous: the surname may be 'lopez' or 'garcialopez'.
    Pattern matching tries every reading and keeps whichever explains the data.

    A name built from an initial and a given name is ambiguous in the other
    direction. 'P. Sasikumar' and 'Sasikumar P' are the same person written the
    two ways India writes them - official records lead with the initial, HR
    systems trail it - and the mailbox is built from the given name either way.
    Neither order can be ruled out from the name alone, so both are offered and
    the domain's own pattern decides.
    """
    out = [name]
    seen = {(name.first, name.last)}
    toks = name.tokens

    # The umlaut expansion is offered before the surname splits because for a
    # German-speaking employer it is the likelier reading, not a fallback.
    umlaut_alt = parse(expand_umlauts(name.raw)) if has_umlaut(name.raw) else None

    def offer(first: str, last: str) -> None:
        if not first or (first, last) in seen:
            return
        seen.add((first, last))
        out.append(Name(raw=name.raw, first=first, last=last, tokens=toks))

    if umlaut_alt is not None and umlaut_alt.ok:
        offer(umlaut_alt.first, umlaut_alt.last)
        if out and (out[-1].first, out[-1].last) == (umlaut_alt.first, umlaut_alt.last):
            out[-1].spelling_variant = True

    if len(toks) >= 3:
        for split in range(1, len(toks)):
            offer("".join(toks[:split]), "".join(toks[split:]))

    initials = [t for t in toks if len(t) == 1]
    words = [t for t in toks if len(t) > 1]
    if initials and words:
        letters, word = "".join(initials), "".join(words)
        offer(word, letters)     # 'P. Sasikumar' -> sasikumar.p@
        offer(letters, word)     # ...and the other way round
        # The reading where a trailing letter really is noise, not a surname.
        if len(words) >= 2 and len(toks[-1]) == 1:
            offer(words[0], "".join(words[1:]))
    return out


_LINKEDIN_RE = re.compile(r"linkedin\.com/(?:in|pub)/([^/?#]+)", re.I)
_HASHY = re.compile(r"^[0-9a-f]{2,}$|^\d+$")


def from_linkedin_url(url: str) -> tuple[Name, str]:
    """Derive a probable name from a public LinkedIn vanity slug.

    We never fetch the profile - LinkedIn's terms forbid scraping it, and the slug
    alone carries the name. Returns (Name, slug) and an empty Name if unparseable.
    """
    match = _LINKEDIN_RE.search(url or "")
    if not match:
        return Name(raw=url or ""), ""
    handle = match.group(1)
    pieces = [p for p in handle.split("-") if p]
    # Trailing '4a7b21' style disambiguators are not part of the name.
    while pieces and _HASHY.match(pieces[-1]):
        pieces.pop()
    if not pieces:
        return Name(raw=url), handle
    return parse(" ".join(pieces)), handle
