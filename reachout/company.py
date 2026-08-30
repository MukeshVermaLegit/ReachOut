"""Turn a company name into the mail domain its staff actually use."""

from __future__ import annotations

import difflib
import re
from dataclasses import dataclass, field

from . import netdns
from .db import USER_SOURCE
from .names import slug
from .sources import search as websearch
from .sources.github import _api as gh_api

# Legal/marketing suffixes that are never part of the domain.
_SUFFIX_RE = re.compile(
    r"\b(inc|llc|ltd|limited|corp|corporation|co|company|gmbh|bv|nv|plc|pvt|private|"
    r"pte|sarl|srl|ag|sa|ab|oy|as|technologies|technology|tech|labs|lab|group|holdings|"
    r"solutions|systems|software|services|ventures|partners|studio|studios)\b\.?", re.I)

# Ordered by how likely a company's own domain sits there. .in and .co.in are
# as canonical for an Indian company as .com is elsewhere, so they lead - they
# used to sit at index 8 and 9, outside the slice _guess_from_tlds actually
# tried, which meant an Indian company was never guessed directly at all.
# ac.in sits inside the tried slice for the same reason .in does: every Indian
# university, IIT, IISc and NIT is on it, and while it was outside the slice a
# lookup for one could never guess its domain directly - "IIT Bombay" resolved
# to iitbombay.org, the alumni association, and answered about the wrong body.
TLDS = ["com", "in", "co.in", "ac.in", "io", "ai", "co", "net", "org", "dev",
        "app", "res.in", "edu.in", "co.uk", "de", "fr", "sg", "com.au", "ca",
        "xyz", "tech", "cloud"]

# Carry a company's full weight rather than the long-tail weight.
PRIMARY_TLDS = {"com", "in", "co.in", "ac.in"}

# Registries that verify the registrant, so a working mailbox there is evidence
# of identity rather than of somebody having paid for a name.
RESTRICTED_TLDS = ["ac.in", "res.in", "edu.in"]


@dataclass
class DomainGuess:
    domain: str
    confidence: float
    evidence: list = field(default_factory=list)


def looks_like_domain(text: str) -> bool:
    text = (text or "").strip().lower().lstrip("@")
    if not text or " " in text or "@" in text:
        return False
    return bool(re.match(r"^[a-z0-9][a-z0-9.\-]*\.[a-z]{2,24}$", text))


def normalise_company(name: str) -> str:
    cleaned = _SUFFIX_RE.sub(" ", name or "")
    cleaned = re.sub(r"[^\w\s\-&]", " ", cleaned)
    return " ".join(cleaned.split())


# Legal wrappers only. Descriptive words are deliberately NOT stripped here:
# the acronym of "Tata Consultancy Services" is TCS, and dropping "Services"
# the way normalise_company does would leave the useless two-letter "tc".
_LEGAL_ONLY_RE = re.compile(
    r"\b(inc|llc|ltd|limited|corp|corporation|co|company|gmbh|bv|nv|plc|pvt|"
    r"private|pte|sarl|srl|ag|sa|ab|oy|as)\b\.?", re.I)


# Words that mean the subject is a university, institute or lab rather than a
# company - the only case where a .ac.in / .res.in address is the main one.
_INSTITUTION_RE = re.compile(
    r"\b(iit|nit|iiit|iisc|iim|aiims|university|universities|univ|college|"
    r"institute|institutes|institution|polytechnic|academy|school|vidyalaya|"
    r"vishwavidyalaya|research|laborator(?:y|ies))\b", re.I)


def looks_institutional(company: str) -> bool:
    return bool(_INSTITUTION_RE.search(company or ""))


def acronym(company: str) -> str:
    """'Tata Consultancy Services' -> 'tcs'. '' when there is nothing useful.

    Indian corporates are overwhelmingly known by their initials - TCS, HDFC,
    ICICI, ONGC, SBI, BPCL - and that acronym is what they register. Without
    this the resolver can only match on the words, and the first word of a
    subsidiary's name is its *parent conglomerate*: 'Tata Consultancy Services'
    scored a perfect 1.0 against tata.com, the holding company, and answered
    every downstream question about the wrong legal entity.
    """
    cleaned = _LEGAL_ONLY_RE.sub(" ", company or "")
    words = [w for w in re.split(r"[^\w]+", cleaned) if w]
    if len(words) < 2:
        return ""
    # A name that already opens with an initialism combines it with what
    # follows rather than reducing to one letter per word: IIT Bombay is iitb,
    # not ib, and every IIT, NIT and IIIT campus domain is built that way.
    if len(words[0]) >= 2 and words[0].isupper():
        combined = (words[0] + "".join(w[0] for w in words[1:])).lower()
        if 3 <= len(combined) <= 8:
            return combined
    letters = "".join(w[0] for w in words).lower()
    # Two letters collide with far too much; four-plus stops being how anyone
    # refers to the company.
    return letters if 3 <= len(letters) <= 4 else ""


def _verified(domain: str) -> tuple[bool, dict]:
    facts = netdns.describe(domain)
    return facts["has_mx"], facts


def _guess_from_tlds(company: str, limit: int = 8) -> list[DomainGuess]:
    """Try <slug>.<tld> and keep the ones that actually accept mail."""
    base = slug(normalise_company(company))
    if not base or len(base) < 2:
        return []
    out: list[DomainGuess] = []
    # The acronym is tried alongside the spelled-out name because for a great
    # many companies - and most large Indian ones - the initials *are* the
    # registered domain. tcs.com is only reachable this way; no amount of
    # rescoring finds a candidate that was never generated.
    bases = [base]
    initials = acronym(company)
    if initials and initials != base:
        bases.append(initials)
    for stem in bases:
        is_acronym = stem != base
        # A short acronym is only guessed on the restricted TLDs, where the
        # registry checks who you are: .ac.in and .res.in are issued to
        # accredited Indian institutions, so mail there is real evidence.
        # An open TLD proves nothing about three or four letters - iitd.com and
        # vitc.co.in both accept mail, and neither belongs to the institution.
        # Acronyms on open TLDs still arrive via search, where rank has to
        # corroborate them.
        tlds = RESTRICTED_TLDS if is_acronym else TLDS[:limit]
        for tld in tlds:
            candidate = f"{stem}.{tld}"
            has_mx, _facts = _verified(candidate)
            if not has_mx:
                continue
            # A company's home TLD carries more weight than a long-tail one.
            score = 0.62 if tld in PRIMARY_TLDS else 0.48
            why = f"{candidate} has MX records"
            if is_acronym:
                why += f" and matches the initials of '{company}'"
                if looks_institutional(company):
                    # Initials + a working mailbox + a registry that only issues
                    # to accredited institutions is near-conclusive, and it has
                    # to beat search rank: rank is exactly what put the
                    # well-optimised iitbombay.org alumni site above the
                    # institute's own iitb.ac.in. Gated on the query naming an
                    # institution, because .res.in will happily accept mail for
                    # a company's research arm - tcs.res.in is real, and is not
                    # where anyone at TCS reads their mail.
                    score = 0.93
                    why += f"; .{tld} is issued only to accredited institutions"
            out.append(DomainGuess(candidate, score, [why]))
    return out


# A domain whose label is *nearly* the company name is more suspect than one that
# is nothing like it: near-misses are the signature of a spell-corrected search.
NEAR_MISS = 0.7


def name_affinity(company: str, domain: str) -> float:
    """1.0 when the domain label contains the company name (or vice versa),
    otherwise raw character similarity, 0..1."""
    key = slug(normalise_company(company))
    label = slug(domain.split(".")[0])
    if not key or not label:
        return 0.0
    if key in label or label in key:
        return 1.0
    if label and label == acronym(company):
        return 1.0
    return difflib.SequenceMatcher(None, key, label).ratio()


def name_caveat(company: str, domain: str) -> str:
    """Why a domain's name fails to corroborate the company we were asked about.

    Search engines silently correct spelling: asking about 'Whupps' returns
    whipps.com - a real, unrelated company that passes every MX check. Character
    similarity cannot separate that from a genuine match, because a one-letter
    difference scores 0.83. So only exact containment counts as corroboration,
    and a near-miss is called out as the likely spelling correction it is.
    """
    affinity = name_affinity(company, domain)
    if affinity >= 1.0:
        return ""
    if affinity >= NEAR_MISS:
        return (f"'{domain}' is one small edit from '{company}' - that is what a "
                f"search engine's spelling correction looks like, not a match")
    return (f"'{domain}' does not contain '{company}' anywhere in its name - "
            f"nothing but search rank connects the two")


def _guess_from_search(company: str) -> list[DomainGuess]:
    hits = websearch.search(f"{company} official website", count=8)
    if not hits:
        return []
    tally: dict[str, int] = {}
    order: list[str] = []
    for hit in hits:
        host = websearch.registrable(hit.host)
        if not host or host in websearch.AGGREGATORS:
            continue
        if host not in tally:
            order.append(host)
        tally[host] = tally.get(host, 0) + 1

    out: list[DomainGuess] = []
    for host in order[:4]:
        has_mx, _facts = _verified(host)
        if not has_mx:
            continue
        rank = order.index(host)
        score = 0.55 + 0.08 * tally[host] - 0.06 * rank
        evidence = [f"appears {tally[host]}x in web search for '{company}'"]
        caveat = name_caveat(company, host)
        if not caveat:
            score += 0.2
            evidence.append("domain name matches company name")
        else:
            # Search rank alone, for a name that is not the one we asked about.
            # Corroboration has to come from an independent method, so cap here
            # and let merge() raise it if GitHub or a live MX agrees.
            score = min(score, 0.5)
            evidence.append(caveat)
        out.append(DomainGuess(host, min(score, 0.93), evidence))
    return out


def _guess_from_github(company: str) -> list[DomainGuess]:
    handle = slug(normalise_company(company))
    if not handle:
        return []
    data = gh_api(f"/orgs/{handle}")
    if not isinstance(data, dict) or "login" not in data:
        return []
    blog = (data.get("blog") or "").strip()
    if not blog:
        return []
    if "://" not in blog:
        blog = f"https://{blog}"
    import urllib.parse
    host = websearch.registrable(urllib.parse.urlsplit(blog).netloc)
    if not host:
        return []
    has_mx, _facts = _verified(host)
    if not has_mx:
        return []
    return [DomainGuess(host, 0.7, [f"GitHub org @{handle} lists {host} as its website"])]


def resolve(company: str, db=None, use_search: bool = True,
            use_github: bool = True) -> DomainGuess:
    """Best mail domain for a company name, with the evidence behind it."""
    company = (company or "").strip()
    if not company:
        return DomainGuess("", 0.0, ["no company given"])

    # 1. The caller already gave us a domain.
    if looks_like_domain(company):
        domain = company.lower().lstrip("@")
        registrable = websearch.registrable(domain)
        has_mx, facts = _verified(registrable)
        if db:
            db.save_domain_facts(facts)
        return DomainGuess(
            registrable,
            0.95 if has_mx else 0.5,
            ["supplied directly" + (" and has MX records" if has_mx else " but has NO MX records")],
        )

    # 2. Something we resolved before.
    if db:
        cached, confidence, source = db.company_record(company)
        if cached and source == USER_SOURCE:
            # You told us this one. Nothing the resolver can work out beats it.
            return DomainGuess(cached, 0.99, ["you recorded this mapping yourself"])
        if cached and confidence >= 0.6:
            # A stored resolver guess is re-served, but it is not re-scored:
            # returning its own output as fresh evidence is how a wrong domain
            # becomes permanent. The evidence line says which it is.
            return DomainGuess(cached, confidence,
                               ["resolved earlier in this database (not re-checked; "
                                "`reachout company` overrides it)"])

    pool: dict[str, DomainGuess] = {}

    def merge(guesses: list[DomainGuess]) -> None:
        for guess in guesses:
            existing = pool.get(guess.domain)
            if existing:
                # Independent methods agreeing is strong evidence.
                existing.confidence = min(0.97, existing.confidence + guess.confidence * 0.45)
                existing.evidence.extend(guess.evidence)
            else:
                pool[guess.domain] = DomainGuess(guess.domain, guess.confidence,
                                                 list(guess.evidence))

    merge(_guess_from_tlds(company))
    if use_search:
        merge(_guess_from_search(company))
    if use_github:
        try:
            merge(_guess_from_github(company))
        except Exception:
            pass

    if not pool:
        return DomainGuess("", 0.0, [f"could not find a mail domain for '{company}'"])

    # A subsidiary's name opens with its parent conglomerate's - "Tata
    # Consultancy Services" opens with "Tata" - so the parent's domain matches
    # on containment exactly as strongly as the right answer would, and then
    # beats it on search volume because the parent is the more famous brand.
    # Character similarity cannot separate the two; the initials can. When the
    # full name's acronym resolves to a domain that accepts mail, that is the
    # more specific entity, and a domain naming only the *first part* of the
    # company is demoted beneath it.
    #
    # Deliberately narrow: it needs a verified acronym domain in the pool, so
    # "VIT Chennai" -> vit.ac.in and "Zerodha Broking" -> zerodha.com are
    # untouched - neither has an acronym that resolves anywhere.
    initials = acronym(company)
    if initials:
        key = slug(normalise_company(company))
        acro = next((g for g in pool.values()
                     if slug(g.domain.split(".")[0]) == initials), None)
        if acro is not None:
            for guess in pool.values():
                label = slug(guess.domain.split(".")[0])
                if guess is acro or not label or label == key:
                    continue
                if key.startswith(label) and guess.confidence >= acro.confidence:
                    guess.confidence = round(max(0.0, acro.confidence - 0.05), 4)
                    guess.evidence.append(
                        f"demoted below {acro.domain}: '{label}' names only the first "
                        f"part of '{company}', which is what a parent company looks "
                        f"like from a subsidiary's name")

    best = max(pool.values(), key=lambda g: g.confidence)
    if db and best.domain:
        db.upsert_company(company, best.domain, best.confidence, "resolver")
        db.save_domain_facts(netdns.describe(best.domain))
    return best
