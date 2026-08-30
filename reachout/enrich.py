"""The phone-enrichment pipeline: (name, company, work email) in, a decision out.

    subject -> providers -> candidates -> normalise -> group -> attribute
            -> corroborate -> decide -> persist

The decision at the end is the part worth arguing about. Everything upstream
produces *sightings*, and a sighting is not an answer: a number printed on a
company contact page is a real number that does not reach the person you asked
about. So this module returns a status rather than a list, and only one status
means "you may dial this":

    found         a number is tied to this person by evidence you can go and
                  read yourself, at or above the acceptance threshold
    needs_review  something plausible turned up, but a human has to look at the
                  page before it is used
    not_found     nothing published ties a number to this person

`not_found` is the expected outcome for most people, and returning it is a
result, not a failure. Personal numbers are mostly not published; a tool that
finds one every time is guessing, and a guessed phone number is worse than
silence because somebody eventually dials it.

Two invariants are enforced in code rather than left to reviewers:

  * nothing is ever generated. Every candidate traces to a URL, and a candidate
    that cannot name its source is dropped before ranking (`_traceable`).
  * a number is only reported as found when the evidence identifies *this*
    person - by an identifier their employer issued, or a structural binding on
    a page - never because a number happened to be near a common name.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field

from . import company as company_mod
from . import identity, phonefind, providers
from . import phones as phonelib
from .db import USER_SOURCE, Database
from .identity import Subject

FOUND = "found"
NEEDS_REVIEW = "needs_review"
NOT_FOUND = "not_found"

# Report as an answer at or above this. `scoring.GRADES` puts MEDIUM at 55, so
# the bar is deliberately a little above merely-MEDIUM: below it the evidence is
# an association rather than an attribution.
ACCEPT = 60
# The LOW floor in `scoring.GRADES`. Under it a candidate is not worth a human's
# time either, so it is not offered for review - it is simply not an answer.
REVIEW_FLOOR = 30


@dataclass
class ProviderRun:
    """What one provider did, whether or not it found anything."""
    name: str
    kind: str = ""
    basis: str = ""
    found: int = 0
    notes: list = field(default_factory=list)
    error: str = ""
    skipped: str = ""
    degraded: str = ""
    elapsed: float = 0.0

    @property
    def state(self) -> str:
        if self.skipped:
            return "skipped"
        if self.error:
            return "error"
        if self.degraded:
            return "degraded"
        return "ok"

    @property
    def looked(self) -> bool:
        """Did this source actually get to do its job?"""
        return self.state == "ok"

    def as_dict(self) -> dict:
        return {"provider": self.name, "kind": self.kind, "basis": self.basis,
                "state": self.state, "found": self.found, "notes": self.notes,
                "error": self.error, "skipped": self.skipped,
                "degraded": self.degraded, "elapsed": self.elapsed}


@dataclass
class PhoneLookup:
    subject: Subject
    status: str = NOT_FOUND
    reason: str = ""
    candidates: list = field(default_factory=list)   # phonefind.PhoneResult, best first
    company_numbers: list = field(default_factory=list)
    providers: list = field(default_factory=list)    # ProviderRun
    warnings: list = field(default_factory=list)
    checked: int = 0
    dropped: int = 0                                 # sightings with no usable source
    elapsed: float = 0.0
    lookup_id: int = 0

    @property
    def answer(self):
        """The number, or None. Only ever set when status is `found`."""
        return self.candidates[0] if self.status == FOUND and self.candidates else None

    @property
    def best(self):
        """The strongest candidate whatever the status - for display, not for dialling."""
        return self.candidates[0] if self.candidates else None

    @property
    def blind_spots(self) -> list:
        """Sources that could not do their job on this run.

        The number that matters when the answer is `not_found`: a lookup where
        four of six sources were blocked has not established that nothing is
        published, only that it could not find out.
        """
        return [r for r in self.providers if r.state in ("error", "degraded")]

    @property
    def coverage(self) -> str:
        worked = sum(1 for r in self.providers if r.looked)
        usable = sum(1 for r in self.providers if r.state != "skipped")
        return f"{worked}/{usable} sources"

    def as_dict(self) -> dict:
        return {
            "status": self.status,
            "reason": self.reason,
            "subject": self.subject.as_dict(),
            "phone": ({"e164": self.answer.e164, "pretty": self.answer.pretty,
                       "confidence": self.answer.confidence, "grade": self.answer.grade,
                       "source_url": self.answer.source_url}
                      if self.answer else None),
            "candidates": [_candidate_dict(c) for c in self.candidates],
            "company_numbers": [{"e164": c.e164, "pretty": c.pretty, "kind": c.kind,
                                 "source_url": c.source_url} for c in self.company_numbers],
            "providers": [p.as_dict() for p in self.providers],
            "coverage": self.coverage,
            "blind_spots": [{"provider": r.name, "reason": r.degraded or r.error}
                            for r in self.blind_spots],
            "warnings": self.warnings,
            "checked": self.checked,
            "dropped_untraceable": self.dropped,
            "elapsed": self.elapsed,
        }


def _candidate_dict(candidate) -> dict:
    return {
        "e164": candidate.e164,
        "pretty": candidate.pretty,
        "kind": candidate.kind,
        "line_type_certain": candidate.kind_certain,
        "country": candidate.country,
        "attribution": candidate.attribution,
        "confidence": candidate.confidence,
        "grade": candidate.grade,
        "source_url": candidate.source_url,
        "provider": candidate.provider,
        "independent_sources": len(candidate.hosts),
        "signals": [{"key": s.key, "label": s.label, "points": s.points,
                     "source_url": s.source_url} for s in candidate.score.signals],
        "caps": candidate.score.caps,
        "contradictions": candidate.contradictions,
        "evidence": [e.as_dict() for e in candidate.evidence],
    }


def _traceable(found) -> bool:
    """A candidate must be able to say where it was seen.

    This is the line between reporting and inventing. A sighting with no URL
    cannot be checked by the person who has to make the call, and an unverifiable
    number presented with a confidence score is exactly the failure this tool is
    built not to have.
    """
    return bool((found.source_url or "").strip())


def gather(subject: Subject, db=None, only: list | None = None, exclude: list | None = None,
           use_search: bool = True, max_pages: int = 6, directory_budget: int = 6,
           document_limit: int = 3, search_pages: int = 3, extra: list | None = None
           ) -> tuple[list, list[ProviderRun], int]:
    """Run every selected provider and return (candidates, runs, dropped)."""
    ctx = providers.LookupContext(
        subject=subject, db=db, use_search=use_search, max_pages=max_pages,
        directory_budget=directory_budget, document_limit=document_limit,
        search_pages=search_pages)

    candidates = list(extra or [])
    runs: list[ProviderRun] = []
    dropped = 0
    for provider in providers.select(only=only, exclude=exclude):
        started = time.monotonic()
        result = provider.run(ctx)
        usable = []
        for found in result.candidates:
            if _traceable(found):
                usable.append(found)
            else:
                dropped += 1
        candidates.extend(usable)
        runs.append(ProviderRun(
            name=result.provider, kind=result.kind, basis=result.basis,
            found=len(usable), notes=result.notes, error=result.error,
            skipped=result.skipped, degraded=result.degraded,
            elapsed=round(time.monotonic() - started, 2)))
    return candidates, runs, dropped


def decide(personal: list, subject: Subject, runs: list | None = None) -> tuple[str, str]:
    """Turn ranked candidates into a status and a sentence explaining it.

    `runs` matters most when the answer is nothing. A `not_found` reported after
    half the sources were blocked is not a finding about the person, and saying
    so plainly is the difference between a result and a shrug.
    """
    blocked = [r for r in (runs or []) if r.state in ("error", "degraded")]
    if not personal:
        if blocked:
            names = ", ".join(r.name for r in blocked)
            return NOT_FOUND, (
                f"nothing found, but {len(blocked)} source(s) could not do their job "
                f"({names}) - so this is not evidence that no number is published, "
                f"only that the places one would be published could not all be checked")
        return NOT_FOUND, ("no public source ties a phone number to this person - "
                           "which is the normal outcome, since most people do not "
                           "publish theirs")
    best = personal[0]
    if best.confidence < REVIEW_FLOOR:
        return NOT_FOUND, (
            f"the strongest candidate scores {best.confidence}, which is an association "
            f"rather than an attribution - a number near a name is not that person's number")

    # A search snippet is a fragment with no structure around it: there is no
    # row, no markup and no document to say what the number was next to. One of
    # those alone is a lead to check, never an answer.
    snippet_only = all(e.how == "search" for e in best.evidence)
    if snippet_only and len(best.hosts) < 2:
        return NEEDS_REVIEW, (
            "the only evidence is a single search snippet, which carries no page "
            "structure - open the source URL and confirm before using it")

    if best.confidence >= ACCEPT:
        return FOUND, (f"published at {best.source_url} with evidence tying it to "
                       f"{subject.name.display}")
    return NEEDS_REVIEW, (
        f"scored {best.confidence}, below the {ACCEPT} needed to report it as an answer - "
        f"read {best.source_url} and decide for yourself")


def _persist(lookup: PhoneLookup, db: Database) -> None:
    """Write the decision and its evidence, so the answer stays auditable."""
    subject = lookup.subject
    best = lookup.answer or lookup.best
    lookup.lookup_id = db.record_lookup(
        subject_key=subject.key, name_raw=subject.name.display,
        work_email=subject.work_email, company=subject.company, domain=subject.domain,
        status=lookup.status, reason=lookup.reason,
        best_e164=lookup.answer.e164 if lookup.answer else "",
        best_score=best.confidence if best else 0,
        best_source=best.source_url if best else "",
        candidates=len(lookup.candidates),
        providers=",".join(f"{r.name}:{r.state}" for r in lookup.providers),
        elapsed=lookup.elapsed)

    basis = {r.name: r.basis for r in lookup.providers}
    rows = []
    for candidate in lookup.candidates + lookup.company_numbers:
        for item in candidate.evidence:
            rows.append((candidate.e164, item.provider, item.how,
                         basis.get(item.provider, ""), item.source_url,
                         item.context[:300], item.owner))
    if rows:
        db.add_lookup_evidence(lookup.lookup_id, rows)

    # Only numbers actually attributed to the person are filed against them. A
    # switchboard is already in domain_phones; writing it here too would make the
    # person's record claim a number that does not reach them.
    if not lookup.candidates:
        return
    company_id = db.upsert_company(subject.company, subject.domain, 0.9, "enrich") \
        if subject.company else None
    person_id = db.upsert_person(subject.name, company_id)
    for candidate in lookup.candidates:
        db.add_phone(person_id, candidate.e164, candidate.kind, candidate.country,
                     candidate.how, candidate.attribution, candidate.confidence,
                     candidate.grade,
                     "confirmed" if candidate.source_url == USER_SOURCE else "observed",
                     candidate.source_url, candidate.context)


def enrich(full_name: str, company: str = "", work_email: str = "", db: Database | None = None,
           only: list | None = None, exclude: list | None = None, use_search: bool = True,
           max_pages: int = 6, directory_budget: int = 6, document_limit: int = 3,
           search_pages: int = 3, region: str = "", extra: list | None = None,
           persist: bool = True) -> PhoneLookup:
    """Find a phone number for one person, or say honestly that there isn't one.

    `full_name` and one of `company` / `work_email` are the useful minimum. The
    work email is what makes the answer trustworthy rather than plausible: it
    fixes the employer's domain without a search, and it is the identifier that
    lets a page state whose number it is printing.
    """
    started = time.time()
    # Validate the provider names before opening anything, so a typo fails
    # immediately rather than part-way through with a database handle held.
    providers.select(only=only, exclude=exclude)

    owns_db = db is None and persist
    if persist and db is None:
        db = Database()

    subject = identity.build(full_name, company=company, work_email=work_email, region=region)
    lookup = PhoneLookup(subject=subject)
    lookup.warnings.extend(subject.problems)

    if not subject.ok:
        lookup.reason = "no usable name was supplied, so there is nobody to match against"
        lookup.elapsed = round(time.time() - started, 2)
        if owns_db:
            db.close()
        return lookup
    if not subject.domain and not subject.company:
        lookup.reason = ("no company or work email was supplied - without an employer "
                         "there is nothing to distinguish this person from anyone "
                         "sharing their name")
        lookup.elapsed = round(time.time() - started, 2)
        if owns_db:
            db.close()
        return lookup

    if not subject.domain:
        # The work email normally settles this for free. Without one, the
        # employer's own site has to be located before it can be read - and a
        # resolved domain is a guess, so it is recorded as one.
        guess = company_mod.resolve(subject.company, db=db, use_search=use_search)
        if guess.domain:
            subject.domain = guess.domain
            subject.region = region.upper() if region else \
                phonelib.region_for_domain(guess.domain, subject.region)
            lookup.warnings.append(
                f"no work email given, so {subject.company} was resolved to "
                f"{guess.domain} at {guess.confidence:.0%} confidence - if that is the "
                f"wrong company, everything below is about the wrong people")
        else:
            lookup.warnings.append(
                f"could not work out a mail domain for {subject.company}, so its own "
                f"site and staff listings could not be read")

    candidates, runs, dropped = gather(
        subject, db=db, only=only, exclude=exclude, use_search=use_search,
        max_pages=max_pages, directory_budget=directory_budget,
        document_limit=document_limit, search_pages=search_pages, extra=extra)
    lookup.providers = runs
    lookup.dropped = dropped
    lookup.warnings.extend(f"{r.name}: {r.error}" for r in runs if r.error)
    lookup.warnings.extend(f"{r.name} ran blind: {r.degraded}" for r in runs if r.degraded)

    personal, company_numbers, checked = phonefind.rank(
        candidates, subject.name, region=subject.region, subject=subject)
    lookup.candidates = [c for c in personal if c.confidence >= REVIEW_FLOOR]
    # Only numbers the evidence actually attributes to the organisation are
    # reported alongside. What is left over is unattributed - it belongs to
    # somebody, and keeping a list of numbers we cannot place is collecting
    # other people's contact details to answer a question about this one.
    lookup.company_numbers = [c for c in company_numbers
                              if c.attribution == phonefind.COMPANY]
    lookup.checked = checked
    lookup.status, lookup.reason = decide(personal, subject, runs)
    # Anything too weak to review is not offered at all, so the list a caller
    # sees never contains a number the tool would not stand behind.
    if lookup.status == NOT_FOUND:
        lookup.candidates = []
    lookup.elapsed = round(time.time() - started, 2)

    if persist:
        _persist(lookup, db)
    if owns_db:
        db.close()
    return lookup
