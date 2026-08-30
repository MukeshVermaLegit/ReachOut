"""Finding phone numbers, and being honest about whose they are.

There is no pattern to apply here - a number is either published somewhere or it
is not. The hard part is not discovery, it is *attribution*: a company contact
page is full of numbers, and almost none of them reach the person you asked
about. Presenting a switchboard as someone's mobile is the failure mode this
module exists to prevent.
"""

from __future__ import annotations

import re
import urllib.parse
from dataclasses import dataclass, field

from . import phones as phonelib
from .db import USER_SOURCE
from .identity import Subject, name_presence
from .names import Name
from .scoring import Score, grade_for
from .sources import directory as staffdir
from .sources import documents as docs
from .sources import search as websearch
from .sources import website

PERSON = "person"
COMPANY = "company"
UNKNOWN = "unknown"


@dataclass
class Evidence:
    """One sighting of one number: where it was, and what was printed around it."""
    source_url: str = ""
    how: str = ""
    provider: str = ""
    context: str = ""
    owner: str = ""
    emails: list = field(default_factory=list)

    @property
    def host(self) -> str:
        return websearch.registrable(urllib.parse.urlsplit(self.source_url).netloc)

    def as_dict(self) -> dict:
        return {"source_url": self.source_url, "how": self.how, "provider": self.provider,
                "context": self.context, "owner": self.owner, "emails": list(self.emails)}


@dataclass
class PhoneResult:
    e164: str
    pretty: str = ""
    kind: str = phonelib.UNKNOWN
    kind_certain: bool = False
    country: str = ""
    attribution: str = UNKNOWN
    score: Score = field(default_factory=Score)
    source_url: str = ""
    context: str = ""
    how: str = ""
    provider: str = ""
    # Every sighting of this number, strongest first. The winning one decided the
    # score; the rest are what a human would check to confirm or reject it.
    evidence: list = field(default_factory=list)
    contradictions: list = field(default_factory=list)

    @property
    def confidence(self) -> int:
        return self.score.value

    @property
    def grade(self) -> str:
        return self.score.grade

    @property
    def hosts(self) -> list:
        """Distinct sites that published this number."""
        return list(dict.fromkeys(e.host for e in self.evidence if e.host))

    @property
    def sources(self) -> list:
        return list(dict.fromkeys(e.source_url for e in self.evidence if e.source_url))


@dataclass
class PhoneReport:
    personal: list = field(default_factory=list)
    company: list = field(default_factory=list)
    warnings: list = field(default_factory=list)
    documents: list = field(default_factory=list)
    directories: list = field(default_factory=list)
    checked: int = 0

    @property
    def best(self):
        return self.personal[0] if self.personal else None


def _on_employer_site(found, subject) -> bool:
    """Was this seen on the employer's own domain?

    A staff listing on the company's own site carries the employment link in the
    URL, so the row does not have to repeat it.
    """
    if not subject.domain or not found.source_url:
        return False
    host = urllib.parse.urlsplit(found.source_url).netloc
    return bool(host) and websearch.registrable(host) == websearch.registrable(subject.domain)


# Sightings whose structure states the association, rather than leaving it to be
# inferred from how close two things were printed. In one of these, finding the
# subject's own work address means the page said whose number this is.
_STRUCTURAL = {"json-ld", "directory", "document", "microdata", "authorized-api"}


def _name_in(text: str, name: Name) -> str:
    """'full' | 'last' | '' - how strongly a person's name appears in some text."""
    return name_presence(text, name)


def attribute(found, name: Name, parsed: phonelib.Phone, bound: str = "",
              subject: Subject | None = None, corroborating_hosts: int = 0
              ) -> tuple[str, Score]:
    """Decide whether a number reaches this person, and show the reasoning.

    The score answers one question only: *does dialling this reach this person?*
    A perfectly valid company switchboard therefore scores near zero.

    `bound` is the number a structural source (a directory row) already tied to
    this person. When it is set, mere proximity to their name cannot claim a
    different number: on a page listing staff one after another, the text window
    around one person's number reaches into the next person's entry, and the
    name it finds there belongs to the number that follows it, not the one
    before.

    `subject` carries the work email, when one was supplied. It changes what the
    evidence can prove. A name is a weak identifier - every page about any
    Amara Okafor matches - whereas a work address was issued by the employer to
    exactly one person, so a block printing both the address and a number is the
    page itself stating the association. The same block printing a *colleague's*
    address instead is the clearest evidence available that the number is not
    the subject's, and it is treated as such.

    `corroborating_hosts` is how many independent sites published this same
    number. Independence is the point: five pages on one company site are one
    source, and only a second host is a second opinion.
    """
    score = Score()
    signal = phonelib.context_signal(found.context)
    owner_match = _name_in(found.owner, name) if found.owner else ""
    context_match = _name_in(found.context, name)
    structural = found.how in _STRUCTURAL

    # Everything printed in the same block: the display context, the addresses
    # the extractor kept whole, and any owner the markup named.
    mention = subject.mentions(found.context, " ".join(found.emails or []), found.owner) \
        if subject is not None else None

    attribution = UNKNOWN

    if found.source_url == USER_SOURCE:
        # You supplied this. Nothing discovered on the open web outranks it.
        score.add("confirmed", "you confirmed this number yourself", 80)
        attribution = PERSON
    elif mention is not None and mention.email_exact and mention.rival_email:
        # Both their address and a colleague's inside one block means the block
        # is not one person's entry - it is the listing running on. Neither
        # address can claim the number, and the stronger evidence being present
        # makes that more dangerous rather than less: it would otherwise outrank
        # every check below it.
        score.caps.append(
            f"not attributed: the block prints both {subject.work_email} and "
            f"{mention.rival_email}, so it spans more than one person's entry and "
            f"cannot say whose number this is")
    elif mention is not None and mention.email_exact and structural:
        score.add("email_bound",
                  f"published in the same entry as {subject.work_email}, the work "
                  f"address you gave for this person", 76, found.source_url)
        attribution = PERSON
    elif mention is not None and mention.email_exact:
        # Same evidence, weaker container: a text window is a guess about what
        # counts as "the same entry", so proximity risk applies here and not above.
        score.add("email_adjacent",
                  f"printed alongside {subject.work_email}, the work address you gave "
                  f"for this person", 58, found.source_url)
        attribution = PERSON
    elif owner_match == "full" and found.how == "directory":
        score.add("directory_row",
                  f"bound by the page's own markup to the entry for {found.owner}", 62,
                  found.source_url)
        attribution = PERSON
    elif owner_match == "full" and found.how in ("json-ld", "microdata"):
        score.add("jsonld_person",
                  f"page's structured data lists this as {found.owner}'s number", 70,
                  found.source_url)
        attribution = PERSON
    elif owner_match == "full" and found.how == "profile":
        score.add("own_site",
                  f"published on the personal site linked from a profile matching "
                  f"{found.owner}", 55, found.source_url)
        attribution = PERSON
    elif owner_match == "full":
        score.add("owner_named", f"the page names this as {found.owner}'s number", 50,
                  found.source_url)
        attribution = PERSON
    elif context_match == "full" and bound and parsed.e164 != bound:
        # Score.add drops zero-point signals, and this one carries no points -
        # only a reason. It belongs with the caps, which is where the tool
        # explains what it refused to conclude.
        score.caps.append(
            f"not attributed: their name is printed nearby, but a staff directory "
            f"gives their number as {bound} - on a page of stacked entries, "
            f"proximity picks up the neighbour")
    elif context_match == "full":
        score.add("name_adjacent", "published next to this person's full name", 50,
                  found.source_url)
        attribution = PERSON
    elif context_match == "last":
        score.add("surname_adjacent", "published near this person's surname only", 20,
                  found.source_url)

    # A colleague's address in the same block is the page telling us whose entry
    # this is, and it is not the subject's. Proximity to their name elsewhere in
    # the block does not survive that.
    if mention is not None and mention.rival_email and not mention.email_exact:
        if attribution == PERSON:
            attribution = UNKNOWN
            score.caps.append(
                f"not attributed: {mention.rival_email} is printed in the same block, so "
                f"this entry is a colleague's - a name appearing nearby is the listing "
                f"running on, not an association")
        else:
            score.add("rival_email",
                      f"the block belongs to {mention.rival_email}, not to this person", -25)
    elif mention is not None and mention.email_local and not mention.email_exact:
        # Two different findings share this flag: the local part of an address we
        # were given, or - when we were given none - an address at the employer
        # that this person's own name would produce. Say which.
        score.add("email_local",
                  (f"the distinctive part of their work address ('{subject.local}') "
                   f"appears in the same block") if subject.work_email else
                  (f"an address at {subject.domain} that this person's name would "
                   f"produce appears in the same block"), 16, found.source_url)

    if found.scope == "organization":
        score.add("org_scope", "structured data attributes it to the organisation, not a person", -60)
        attribution = COMPANY
    if signal == "switchboard":
        score.add("switchboard", "labelled as an office/support/reception line", -45)
        if attribution != PERSON:
            attribution = COMPANY
    elif signal == "personal":
        score.add("labelled_personal", "labelled as a mobile/direct/personal line", 14)

    if parsed.kind in (phonelib.TOLL_FREE, phonelib.PREMIUM):
        score.add("service_number", f"{parsed.kind} numbers are never personal lines", -70)
        attribution = COMPANY

    placeholder = phonelib.looks_placeholder(parsed)
    if placeholder:
        score.add("placeholder", f"looks like a made-up example number: {placeholder}", -75)

    if parsed.valid:
        score.add("valid", f"valid {parsed.country} number", 10)
    if parsed.kind == phonelib.MOBILE:
        score.add("mobile_series", "allocated in the mobile series", 8)
    elif parsed.kind == phonelib.LANDLINE:
        score.add("landline", "a fixed line, so not a mobile", -6)
    elif not parsed.kind_certain:
        score.add("kind_unknown",
                  f"{parsed.country} numbering cannot tell mobile from landline", 0)

    # A name is not an identifier. On a page with no connection to the employer,
    # "their name is near this number" is satisfied by every other person who
    # shares the name, so proximity alone must not reach the reporting bar.
    if mention is not None and attribution == PERSON and (subject.domain or subject.company):
        linked = (mention.email_exact or mention.email_local or mention.company
                  or mention.domain or _on_employer_site(found, subject))
        if not linked:
            score.add("no_employer_link",
                      f"the page names them but shows no connection to "
                      f"{subject.company or subject.domain} - a shared name is not an "
                      f"identifier", -32, found.source_url)

    if corroborating_hosts > 1:
        score.add("corroborated",
                  f"the same number is published on {corroborating_hosts} independent sites",
                  10)

    if found.how == "authorized-api":
        score.add("authorized_provider",
                  "returned by a data provider you are licensed to query", 20,
                  found.source_url)
    if found.how == "tel-link":
        score.add("tel_link", "published as a click-to-call link", 6)
    elif found.how == "document":
        score.add("document", "printed in a public document that names this person", 12)
    elif found.how == "directory":
        score.add("directory", "found on a page that gives one entry per person", 8)

    total = sum(s.points for s in score.signals)

    if attribution != PERSON:
        total = min(total, 20)
        score.caps.append(
            "capped at 20: nothing ties this number to the person, only to the company")
    elif total > 96:
        # Even your own word is not a dialled-and-answered confirmation.
        total = 96
        score.caps.append("capped at 96: no call has been placed to confirm it")
    if not parsed.valid:
        total = 0
        score.caps.append("zeroed: not a valid number")

    score.value = max(0, min(100, int(round(total))))
    score.grade = grade_for(score.value)
    return attribution, score


_SNIPPET_PHONE = re.compile(
    r"(?<![\d])(?:\+\d{1,3}[\s.\-]?)?(?:\(\d{2,4}\)[\s.\-]?)?\d{3,5}[\s.\-]?\d{3,5}"
    r"(?:[\s.\-]?\d{2,5})?(?![\d])")


def _from_search(name: Name, company: str, domain: str) -> list:
    """Look for a number published alongside the person's name."""
    out: list = []
    query = f'"{name.display}" {company or domain} (mobile OR phone OR contact)'
    try:
        hits = websearch.search(query, count=8)
    except Exception:
        return out
    for hit in hits:
        blob = f"{hit.title} {hit.snippet}"
        if not _name_in(blob, name):
            continue
        for match in _SNIPPET_PHONE.finditer(blob):
            start = max(0, match.start() - 70)
            out.append(website.FoundPhone(
                raw=match.group(0), context=blob[start:match.end() + 40],
                owner="", scope="page", how="search"))
    return out


def _from_documents(name: Name, org: str, limit: int = 3) -> tuple[list, list]:
    """Mine public PDFs and CVs that mention the person.

    Documents are where personal contact details are actually published in the
    open - a CV, a conference programme, a faculty listing - far more often than
    on a company website.
    """
    found: list = []
    notes: list[str] = []
    try:
        urls = docs.find_documents(name, org, limit=limit)
    except Exception as exc:
        return found, [f"document search: {exc}"]
    if not urls:
        return found, notes

    for url in urls:
        try:
            harvest = docs.harvest_document(url)
        except Exception:
            continue
        if harvest.error or not harvest.text_length:
            continue
        # Only trust a document that actually names the person.
        whole = " ".join(harvest.emails.values()) + " " + " ".join(c for _n, c in harvest.phones)
        owns = docs.mentions_person(whole, name)
        for number, context in harvest.phones:
            found.append(website.FoundPhone(
                raw=number, context=context, owner=name.display if owns else "",
                scope="person" if owns else "page", how="document", source_url=url))
        notes.append(f"read {url.rsplit('/', 1)[-1][:44]} ({harvest.text_length} chars)")
    return found, notes


def _from_directory(name: Name, domain: str, org: str = "", limit: int = 3,
                    extra_seeds: list | None = None) -> tuple[list, list]:
    """Read staff directories and keep only the rows that name *this* person.

    A directory page hands you a direct dial for everybody on it. Keeping the
    rest would mean quietly banking a few hundred strangers' work numbers to
    answer a question about one person, so every row bound to someone else is
    dropped on the spot and never reaches the database.
    """
    if not domain:
        return [], []
    try:
        hits, notes = staffdir.crawl(domain, name, org, budget=limit * 2,
                                     extra_seeds=extra_seeds)
    except Exception as exc:
        return [], [f"directory search: {exc}"]
    mine = [hit for hit in hits if hit.owner and _name_in(hit.owner, name) == "full"]
    if notes:
        named = sum(1 for hit in hits if hit.owner)
        notes.append(f"{len(hits)} numbers across those pages, {named} bound to a "
                     f"named person, {len(mine)} to {name.display}")
    return mine, notes


# How much a sighting is worth *as a sighting*, before anything is scored. The
# same number is usually seen several ways; whichever sighting survives dedupe
# decides how it gets attributed, so the strongest one has to be the survivor.
# Without this the plain-text window on a contact page silently overrides the
# structural binding a directory row gives you for the very same number.
_SIGHTING_TRUST = {
    "json-ld": 60, "directory": 55, "authorized-api": 50, "document": 45,
    "profile": 40, "microdata": 32, "tel-link": 30, "banked": 20, "text": 10,
    "search": 5,
}


def _sighting_trust(found) -> int:
    if found.source_url == USER_SOURCE:
        return 1000
    trust = _SIGHTING_TRUST.get(found.how, 10)
    if found.owner:
        trust += 25          # a sighting that names an owner says strictly more
    if found.scope in ("person", "organization"):
        trust += 10          # ...as does one that states whose kind of number it is
    return trust


def _same_source(sightings, refusal: str, winning_host: str) -> bool:
    """True when every sighting that could have produced `refusal` is the same site."""
    hosts = {websearch.registrable(urllib.parse.urlsplit(s.source_url).netloc)
             for s in sightings if s.source_url}
    return hosts <= {winning_host}


def _region_for_sighting(found, fallback: str) -> str:
    """A number in a document belongs to that document's host, not to the company."""
    if not found.source_url or found.source_url == USER_SOURCE:
        return fallback
    host = urllib.parse.urlsplit(found.source_url).netloc
    return phonelib.region_for_domain(host, fallback) if host else fallback


def _bound_to_someone_else(found, name: Name) -> str:
    """The name of a *different* person this sighting is structurally bound to.

    A staff directory hands you a direct dial for everyone on it, and a speaker
    list hands you a mobile for every speaker. Keeping the ones bound to other
    people would mean banking a roomful of strangers' numbers to answer a
    question about one person, so they are dropped at the ranking step and never
    reach the report or the database.
    """
    owner = (found.owner or "").strip()
    if not owner or found.how not in _STRUCTURAL:
        return ""
    if _name_in(owner, name):
        return ""
    from .sources.website import person_names
    return owner if person_names([owner]) else ""


def rank(candidates: list, name: Name, region: str = "IN",
         subject: Subject | None = None) -> tuple[list, list, int]:
    """Normalise, deduplicate and attribute a pile of raw sightings.

    Returns (personal, company, checked). Grouping happens on the E.164 form, so
    '020 7946 0812', '+44 20 7946 0812' and 'tel:+442079460812' are one number
    with three pieces of evidence rather than three candidates - which is also
    what makes independent corroboration countable.

    Every sighting of a number is attributed, not just the strongest, and the
    best result wins. A directory row can prove ownership that a text window
    cannot, but so can a conference page printing the work address next to the
    number, and neither is reliably found first.
    """
    # A structural binding for this person outranks proximity anywhere else.
    bound = ""
    for found in sorted(candidates, key=_sighting_trust, reverse=True):
        if found.how == "directory" and found.owner and _name_in(found.owner, name) == "full":
            parsed = phonelib.normalise(found.raw, default_region=_region_for_sighting(found, region))
            if parsed.valid:
                bound = parsed.e164
                break

    grouped: dict[str, list] = {}
    parsed_by_number: dict[str, phonelib.Phone] = {}
    others: dict[str, str] = {}
    checked = 0
    for found in candidates:
        checked += 1
        parsed = phonelib.normalise(found.raw, default_region=_region_for_sighting(found, region))
        if not parsed.valid:
            continue
        owner = _bound_to_someone_else(found, name)
        if owner:
            others.setdefault(parsed.e164, owner)
        grouped.setdefault(parsed.e164, []).append(found)
        parsed_by_number.setdefault(parsed.e164, parsed)

    personal, company = [], []
    for e164, sightings in grouped.items():
        if e164 in others and not any(_name_in(s.owner or "", name) == "full"
                                      for s in sightings):
            # Somebody else's number, and nothing anywhere says otherwise.
            continue
        parsed = parsed_by_number[e164]
        sightings.sort(key=_sighting_trust, reverse=True)
        evidence = [Evidence(source_url=s.source_url, how=s.how,
                             provider=getattr(s, "provider", ""), context=s.context,
                             owner=s.owner, emails=list(getattr(s, "emails", []) or []))
                    for s in sightings]
        hosts = len({e.host for e in evidence if e.host})

        best_found, best_attr, best_score, refusals = None, UNKNOWN, None, []
        for found in sightings:
            attribution, score = attribute(found, name, parsed, bound=bound, subject=subject,
                                           corroborating_hosts=hosts)
            refusals.extend(c for c in score.caps if c.startswith("not attributed"))
            better = best_score is None or (
                (attribution == PERSON, score.value) > (best_attr == PERSON, best_score.value))
            if better:
                best_found, best_attr, best_score = found, attribution, score

        result = PhoneResult(
            e164=e164, pretty=parsed.pretty, kind=parsed.kind,
            kind_certain=parsed.kind_certain, country=parsed.country,
            attribution=best_attr, score=best_score, context=best_found.context,
            how=best_found.how, source_url=best_found.source_url,
            provider=getattr(best_found, "provider", ""), evidence=evidence,
            contradictions=list(dict.fromkeys(refusals)),
        )
        # A refusal only counts as a contradiction when it came from a different
        # page. The same page read twice - once as a document, once as entries -
        # routinely produces one reading that declines to conclude, and that is
        # the looser reading losing, not evidence in conflict.
        winning_host = websearch.registrable(
            urllib.parse.urlsplit(best_found.source_url).netloc)
        result.contradictions = [c for c in result.contradictions
                                 if not _same_source(sightings, c, winning_host)]
        if best_attr == PERSON and result.contradictions:
            result.score.caps.append(
                "another page's evidence disagrees - see the contradictions below")
        (personal if best_attr == PERSON else company).append(result)

    personal.sort(key=lambda r: -r.confidence)
    company.sort(key=lambda r: (r.kind != phonelib.MOBILE, r.e164))
    return personal, company, checked


def find_phones(name: Name, domain: str, db=None, company: str = "",
                use_website: bool = True, use_search: bool = True,
                use_documents: bool = True, use_directory: bool = True,
                extra: list | None = None, subject: Subject | None = None) -> PhoneReport:
    """Gather every published number around a person and sort it by who it reaches."""
    report = PhoneReport()
    candidates: list = list(extra or [])
    region = phonelib.region_for_domain(domain)

    if domain and db:
        # Reuse anything a previous run already banked for this domain.
        for row in db.domain_phones(domain):
            candidates.append(website.FoundPhone(
                raw=row["e164"], context=row["context"] or "", owner=row["owner"] or "",
                scope=row["scope"] or "page", how="banked",
                source_url=row["source_url"] or ""))

    site_pages: list[str] = []
    if domain and use_website:
        try:
            site = website.harvest_site(domain, max_pages=6)
            site_pages = list(site.get("pages", []))
            if site.get("client_rendered"):
                report.warnings.append(
                    f"{domain} renders its content with JavaScript - the served HTML is an "
                    f"empty shell, so nothing on the site is readable without a browser")
            for found in site.get("phones", []):
                found_url = site.get("phone_pages", {}).get(found.raw, "")
                candidates.append(found)
                if db:
                    parsed = phonelib.normalise(found.raw, default_region=region)
                    db.add_domain_phone(domain, parsed.e164 or found.raw, parsed.kind,
                                        found.scope, found.owner, found.context, found_url)
        except Exception as exc:
            report.warnings.append(f"website phones: {exc}")

    if use_search and (company or domain):
        try:
            candidates.extend(_from_search(name, company, domain))
        except Exception as exc:
            report.warnings.append(f"search phones: {exc}")

    if use_directory and domain:
        dir_hits, notes = _from_directory(name, domain, company or domain,
                                          extra_seeds=site_pages)
        candidates.extend(dir_hits)
        report.directories = notes

    if use_documents:
        doc_hits, notes = _from_documents(name, company or domain)
        candidates.extend(doc_hits)
        report.documents = notes

    report.personal, report.company, report.checked = rank(candidates, name, region,
                                                           subject=subject)
    return report
