"""Where candidate numbers come from, behind one interface.

Every source - a company website, a staff directory, a PDF, a licensed API you
sign a contract for - answers the same question: *given this subject, what
numbers can you show me, and on what page did you see them?* Putting them behind
one interface means the ranking and scoring code never learns which source it is
talking to, and adding a provider is an import plus a `register()` call rather
than a new branch inside the pipeline.

Two things every provider must supply, and neither is optional:

  `basis`      why the data is usable - published by the subject or their
               employer, or licensed under a contract you hold. A provider that
               cannot state one does not belong in the registry.
  `source_url` the page the number was actually seen on. A candidate with no URL
               cannot be checked by a human, and an unverifiable candidate is
               worth less than no candidate at all.

Excluded on purpose, and not pluggable later either: breach and credential
dumps, scraped broker datasets, password-reset or account-recovery probing, and
anything that enumerates number ranges. These are not "sources with worse
licensing" - they produce data the subject never published, and the whole design
of this module is that a candidate is a sighting of something a person or their
employer chose to make public.
"""

from __future__ import annotations

import os
import re
import urllib.parse
from dataclasses import dataclass, field

from . import phones as phonelib
from .fetch import BROKER_HOSTS, DUMP_HOSTS  # noqa: F401  (shared policy)
from .db import USER_SOURCE
from .identity import Subject
from .names import parse as parse_name
from .sources import directory as staffdir
from .sources import documents as docs
from .sources import github
from .sources import search as websearch
from .sources import website

# Loose enough to catch a number inside a search snippet, where there is no
# markup to lean on. Everything it produces is validated by `phones.normalise`.
_SNIPPET_PHONE = re.compile(
    r"(?<![\d])(?:\+\d{1,3}[\s.\-]?)?(?:\(\d{2,4}\)[\s.\-]?)?\d{3,5}[\s.\-]?\d{3,5}"
    r"(?:[\s.\-]?\d{2,5})?(?![\d])")

# Sites whose terms forbid automated access. A search *snippet* about one of
# these is public search-result data and may be used as a lead; fetching the
# page is what their terms prohibit, and what ReachOut promises not to do (see
# docs/ETHICS.md - scraping LinkedIn profiles has been litigated).
#
# robots.txt already blocks most of these, so this list is not the only defence.
# It is here because a promise the code makes explicitly is one a future edit
# cannot quietly drop, and because "we chose not to" reads better in a report
# than "the fetch failed".
NO_FETCH_HOSTS = {
    "linkedin.com", "facebook.com", "instagram.com", "x.com", "twitter.com",
    "threads.net", "tiktok.com",
}

# Data brokers and people-search sites. Everything they hold was scraped,
# purchased or inferred, which is the one thing this tool promises never to
# report - so unlike the social networks above, their *snippets* are off limits
# too, not just their pages. Blocking them costs almost no recall: a number that
# only a broker has is, by definition, one the subject never published.
#
# It also removes a way to be confidently wrong. Broker records go stale and get
# mis-joined to the wrong person, and a stale number attributed to the subject
# is a worse outcome than not_found - the user calls a stranger and never learns
# the tool was at fault.

# Anonymous PDF re-upload mirrors. They are where scraped contact lists get
# parked, and a single one of those files can outweigh every honest source in a
# lookup - the "hni-email-data" dump that prompted this rule yielded 57
# candidate numbers in one fetch.
#
# Nothing is lost by refusing them: they host copies, so anything legitimate on
# them was published somewhere else first, and that original is indexed too.

# One page that prints this many numbers is publishing a dataset, not a bio.
BULK_PAGE_SIGHTINGS = 15


def excluded(url: str) -> str:
    """'' when this source may be used at all, else why not.

    Deliberately stronger than `fetchable`. That one blocks the fetch and keeps
    the search snippet as a lead, which is right for a site whose *terms* forbid
    crawling - the data itself is fine, the access route is not. Here it is the
    data that is out of bounds, so the snippet is refused with the page.
    """
    host = websearch.registrable(urllib.parse.urlsplit(url).netloc)
    if host in BROKER_HOSTS:
        return f"{host} is a data broker - excluded, snippet included (see docs/ETHICS.md)"
    if host in DUMP_HOSTS:
        return f"{host} hosts re-uploaded documents - excluded as a likely scraped dump"
    return ""


def fetchable(url: str) -> str:
    """'' when the page may be fetched, else why not."""
    host = websearch.registrable(urllib.parse.urlsplit(url).netloc)
    if host in NO_FETCH_HOSTS:
        return f"{host} forbids automated access - using the search snippet only"
    return ""


def bulk_list(found: list) -> bool:
    """True when a page's numbers look like a dataset rather than a page.

    Volume alone cannot decide this. A university staff directory legitimately
    prints two hundred direct dials, and it is one of the better sources this
    tool has - but it binds each number to a row that names somebody. A scraped
    dump prints just as many and binds none. So it is the *unbound* share that
    separates them, and blocking on the total would throw away the good case
    along with the bad one.
    """
    if len(found) < BULK_PAGE_SIGHTINGS:
        return False
    bound = sum(1 for phone in found if phone.owner or phone.scope == "person")
    return bound * 4 < len(found)


# What kind of arrangement makes a provider usable.
PUBLIC_WEB = "public-web"          # published openly; read politely, robots honoured
AUTHORIZED = "authorized-api"      # licensed data, used under a contract you hold
LOCAL = "local"                    # your own database, or something you told us


@dataclass
class LookupContext:
    """Everything a provider is allowed to know, and its budget for finding out."""

    subject: Subject
    db: object = None
    use_search: bool = True
    max_pages: int = 6
    directory_budget: int = 6
    document_limit: int = 3
    search_pages: int = 3          # result pages actually fetched and parsed

    @property
    def domain(self) -> str:
        return self.subject.domain

    @property
    def region(self) -> str:
        return self.subject.region


@dataclass
class ProviderResult:
    provider: str
    kind: str = PUBLIC_WEB
    basis: str = ""
    candidates: list = field(default_factory=list)   # list[website.FoundPhone]
    notes: list = field(default_factory=list)
    error: str = ""
    skipped: str = ""                                # why it did not run
    # It ran, but something it depends on did not. "Found nothing" and "could
    # not look" are opposite conclusions and must not print the same way.
    degraded: str = ""

    @property
    def ran(self) -> bool:
        return not self.skipped


class Provider:
    """Base class. Subclass, set the metadata, implement `fetch`."""

    name = ""
    kind = PUBLIC_WEB
    basis = ""
    # Numbers found here are about a person by construction (a CV, a speaker
    # bio) rather than about an organisation (a switchboard). Used only for
    # notes; attribution is still decided from the evidence.
    person_scoped = False
    # True when this provider cannot find anything without web search, so an
    # outage there has to be reported rather than read as "nothing published".
    uses_search = False

    def unavailable(self, ctx: LookupContext) -> str:
        """Reason this provider cannot run now, or '' when it can."""
        return ""

    def fetch(self, ctx: LookupContext) -> ProviderResult:
        raise NotImplementedError

    # ------------------------------------------------------------------ plumbing
    def result(self, **kw) -> ProviderResult:
        return ProviderResult(provider=self.name, kind=self.kind, basis=self.basis, **kw)

    def run(self, ctx: LookupContext) -> ProviderResult:
        """Call `fetch`, and never let a source's failure end the lookup.

        One unreachable site must not cost the answer that four other sources
        would have given, so an exception here becomes a note on the report.
        """
        reason = self.unavailable(ctx)
        if reason:
            return self.result(skipped=reason)
        before = websearch.snapshot()
        try:
            found = self.fetch(ctx)
        except websearch.SearchUnavailable as exc:
            return self.result(degraded=str(exc)[:200])
        except Exception as exc:                       # noqa: BLE001 - see docstring
            return self.result(error=f"{type(exc).__name__}: {exc}"[:200])
        if self.uses_search and not found.degraded:
            queries, outages, reason = websearch.snapshot()
            refused = outages - before[1]
            if refused:
                found.degraded = (f"{refused} of {queries - before[0]} searches were "
                                  f"refused: {reason}")
        for candidate in found.candidates:
            candidate.provider = self.name
        return found


# --------------------------------------------------------------------- providers
class BankedNumbers(Provider):
    """Numbers a previous lookup already saw at this domain.

    Free, instant, and the reason a second person at the same company costs no
    crawling at all.
    """

    name = "banked"
    kind = LOCAL
    basis = "already in your own database from an earlier lookup"

    def unavailable(self, ctx):
        if ctx.db is None:
            return "no database open"
        return "" if ctx.domain else "no company domain"

    def fetch(self, ctx):
        found = []
        for row in ctx.db.domain_phones(ctx.domain):
            found.append(website.FoundPhone(
                raw=row["e164"], context=row["context"] or "", owner=row["owner"] or "",
                scope=row["scope"] or "page", how="banked",
                source_url=row["source_url"] or ""))
        note = [f"{len(found)} number(s) already known at {ctx.domain}"] if found else []
        return self.result(candidates=found, notes=note)


class CompanyWebsite(Provider):
    """The employer's own site: contact, team, about and leadership pages."""

    name = "company-website"
    basis = "published by the subject's employer on its own website"

    def unavailable(self, ctx):
        return "" if ctx.domain else "no company domain"

    def fetch(self, ctx):
        site = website.harvest_site(ctx.domain, max_pages=ctx.max_pages, region=ctx.region)
        notes = [f"read {len(site['pages'])} page(s) on {ctx.domain}"]
        degraded = ""
        if site.get("client_rendered"):
            # There is nothing in the served HTML to extract from. That is a
            # source we could not read, not a source that had nothing - and on a
            # single-page-app company site it is usually the source that would
            # have had the answer.
            degraded = (f"{ctx.domain} serves a JavaScript shell - the HTML carries no "
                        f"content, so nothing on the site is readable without a browser")
        found = []
        for phone in site.get("phones", []):
            phone.source_url = site.get("phone_pages", {}).get(phone.raw, f"https://{ctx.domain}")
            found.append(phone)
            if ctx.db is not None:
                parsed = phonelib.normalise(phone.raw, default_region=ctx.region)
                ctx.db.add_domain_phone(ctx.domain, parsed.e164 or phone.raw, parsed.kind,
                                        phone.scope, phone.owner, phone.context,
                                        phone.source_url)
        return self.result(candidates=found, degraded=degraded,
                           notes=notes + site.get("errors", [])[:3])


class StaffDirectory(Provider):
    """Per-person listings, where a direct dial is actually published.

    Read under different rules from an ordinary page: a number binds to a person
    only when the smallest block of markup holding both holds exactly one name.
    See `sources.directory` for why proximity is not good enough here.
    """

    name = "staff-directory"
    basis = "published by the subject's employer as a staff listing"
    uses_search = True

    def unavailable(self, ctx):
        return "" if ctx.domain else "no company domain"

    def fetch(self, ctx):
        found, notes = staffdir.crawl(
            ctx.domain, ctx.subject.name, ctx.subject.company or ctx.domain,
            budget=ctx.directory_budget, use_search=ctx.use_search, region=ctx.region)
        return self.result(candidates=found, notes=notes)


class PublicDocuments(Provider):
    """CVs, papers, conference programmes, committee minutes - mostly PDFs.

    This is where people publish their own contact details far more often than
    on an employer's website, and the document usually names them explicitly.
    """

    name = "public-documents"
    person_scoped = True
    basis = "published in a public document that names the subject"
    uses_search = True

    def unavailable(self, ctx):
        return "" if ctx.subject.name.ok else "no parsable name"

    def fetch(self, ctx):
        subject = ctx.subject
        urls = docs.find_documents(subject.name, subject.company or ctx.domain,
                                   limit=ctx.document_limit)
        found, notes = [], []
        for url in urls:
            barred = excluded(url)
            if barred:
                notes.append(barred)
                continue
            harvest = docs.harvest_document(url)
            if harvest.error or not harvest.text_length:
                notes.append(f"{url.rsplit('/', 1)[-1][:44]} - {harvest.error or 'no text'}")
                continue
            whole = " ".join(harvest.emails.values()) + " " + \
                " ".join(c for _n, c in harvest.phones)
            owns = docs.mentions_person(whole, subject.name)
            if len(harvest.phones) >= BULK_PAGE_SIGHTINGS and not owns:
                # A document carrying dozens of numbers that never names the
                # subject is a list they happen to appear near, not a CV.
                notes.append(f"{url.rsplit('/', 1)[-1][:44]} - {len(harvest.phones)} "
                             f"numbers and no mention of the subject: a bulk list, skipped")
                continue
            for number, context in harvest.phones:
                found.append(website.FoundPhone(
                    raw=number, context=context,
                    owner=subject.name.display if owns else "",
                    scope="person" if owns else "page", how="document", source_url=url,
                    emails=list(harvest.emails)))
            notes.append(f"read {url.rsplit('/', 1)[-1][:44]} ({harvest.text_length} chars, "
                         f"{len(harvest.phones)} number(s))")
        return self.result(candidates=found, notes=notes)


class PublicPages(Provider):
    """Search for pages that mention the subject, then actually read them.

    Speaker listings, author bios, programme committee pages, personal sites -
    the places where someone publishes their own number, none of which live on
    their employer's domain.

    A search snippet is treated as a lead, not as evidence. The snippet is a
    fragment with no structure: nothing in it says where one person's entry ends
    and the next begins, which is precisely the distinction attribution rests on.
    So promising results are fetched and run through both extractors - the page
    harvester for tel: links and structured data, and the row binder for the
    per-entry blocks a speaker list is built from. The snippet is kept only as a
    fallback when the page itself cannot be read.
    """

    name = "public-pages"
    person_scoped = True
    basis = "publicly indexed page, published openly by its host"
    uses_search = True

    def unavailable(self, ctx):
        if not ctx.use_search:
            return "search disabled for this lookup"
        return "" if ctx.subject.name.ok else "no parsable name"

    def queries(self, subject) -> list:
        found = [f'"{subject.name.display}" {subject.company or subject.domain} '
                 f'(mobile OR phone OR contact)']
        if subject.email_valid and not subject.email_is_role:
            # The address is the sharpest query available: a page printing it is
            # already about this person, with no name ambiguity to resolve.
            found.insert(0, f'"{subject.work_email}"')
        return found

    def fetch(self, ctx):
        subject = ctx.subject
        found, notes, read = [], [], []

        outage = ""
        for query in self.queries(subject):
            try:
                hits = websearch.search(query, count=8)
            except websearch.SearchUnavailable as exc:
                outage = str(exc)
                notes.append(f"search refused for {query[:44]}")
                continue
            notes.append(f"{len(hits)} result(s) for {query[:58]}")
            for hit in hits:
                if not subject.mentions(hit.title, hit.snippet).strength:
                    continue
                if hit.url in read:
                    continue
                barred = excluded(hit.url)
                if barred:
                    # Not `blocked` below: that keeps the snippet as a lead.
                    # Here the data itself is out of bounds, so nothing from
                    # this result is used at all.
                    notes.append(barred)
                    continue
                blocked = fetchable(hit.url)
                if blocked:
                    notes.append(blocked)
                elif len(read) < ctx.search_pages:
                    read.append(hit.url)
                    page_hits, note = self._read(hit.url, subject)
                    notes.append(note)
                    if page_hits:
                        found.extend(page_hits)
                        continue
                # The budget is spent, the page would not open, or its terms say
                # not to. Keep the snippet as a lead, clearly marked as one.
                found.extend(self._from_snippet(hit, subject))
        return self.result(candidates=found, notes=notes, degraded=outage[:200])

    def _read(self, url: str, subject) -> tuple[list, str]:
        """Fetch one page and extract with structure. Returns (candidates, note)."""
        host = urllib.parse.urlsplit(url).netloc
        region = phonelib.region_for_domain(host, subject.region)
        label = url.split("://")[-1][:52]
        try:
            page = website.harvest_page(url, region=region)
        except Exception as exc:                       # noqa: BLE001
            return [], f"{label} - could not read: {exc}"
        if page.error:
            return [], f"{label} - {page.error}"

        found = []
        for phone in page.phones:
            phone.source_url = page.url
            found.append(phone)
        # The same page again, read as entries rather than as one flat document.
        # On a speaker list this is what separates one bio from the next.
        try:
            bound = staffdir.harvest_directory(url, region=region, brand=subject.brand)
            found.extend(bound.phones)
        except Exception:                              # noqa: BLE001 - best effort
            pass
        if bulk_list(found):
            # A blocklist only ever catches the dump hosts someone has already
            # heard of. This catches the next one, which is the one that matters.
            return [], (f"{label} - {len(found)} numbers, almost none bound to a "
                        f"named person: a bulk contact list, discarded")
        return found, f"read {label} ({len(found)} sighting(s))"

    def _from_snippet(self, hit, subject) -> list:
        blob = f"{hit.title} {hit.snippet}"
        out = []
        for match in _SNIPPET_PHONE.finditer(blob):
            start = max(0, match.start() - 70)
            out.append(website.FoundPhone(
                raw=match.group(0), context=blob[start:match.end() + 40],
                owner="", scope="page", how="search", source_url=hit.url,
                emails=subject.emails_in(blob)))
        return out


class PersonalProfiles(Provider):
    """The subject's own pages: a GitHub profile and the site it links to.

    A personal site is the one place where a number, if it is there at all, was
    put there by the person themselves. The GitHub account is only used as a way
    to find that site, and only when it plausibly belongs to the subject.
    """

    name = "personal-profiles"
    person_scoped = True
    basis = "published by the subject on their own profile or website"

    def unavailable(self, ctx):
        return "" if ctx.subject.name.ok else "no parsable name"

    def fetch(self, ctx):
        subject = ctx.subject
        notes, found = [], []
        hits = github.search_users(subject.name.display, subject.company or subject.brand,
                                   limit=4)
        best, best_score = None, 0.0
        for hit in hits:
            user = github.get_user(hit["login"])
            if not user:
                continue
            score = github.match_score(user, subject.name.display, subject.company)
            # A public commit address equal to the work email we were given is
            # not a similarity judgement - it is the same identifier.
            if subject.work_email and any(a == subject.work_email for a, _s in user.emails):
                score = 1.0
            elif subject.domain and github.domain_hint(user) == subject.domain:
                score = min(1.0, score + 0.2)
            if score > best_score:
                best, best_score = user, score
        if not best or best_score < 0.6:
            notes.append("no GitHub account matched this person confidently")
            return self.result(notes=notes)

        notes.append(f"github.com/{best.login} matches {best_score:.0%}")
        blog = (best.blog or "").strip()
        if not blog:
            notes.append("their profile links to no personal site")
            return self.result(notes=notes)
        if "://" not in blog:
            blog = f"https://{blog}"
        host = urllib.parse.urlsplit(blog).netloc
        page = website.harvest_page(
            blog, region=phonelib.region_for_domain(host, subject.region))
        if page.error:
            notes.append(f"{blog} - {page.error}")
            return self.result(notes=notes)
        for phone in page.phones:
            phone.source_url = page.url
            # It is their own site, so a number on it is probably theirs. That is
            # a scope hint, not a free pass - attribution still has to survive
            # scoring, and `owner` is only ever the name the *page* or the
            # profile gave. Writing the subject's own name in here would be
            # asserting the thing we are trying to establish.
            phone.scope = "person" if phone.scope == "page" else phone.scope
            phone.owner = phone.owner or best.name
            phone.how = phone.how if phone.how == "json-ld" else "profile"
            found.append(phone)
        notes.append(f"read {blog} ({len(page.phones)} number(s))")
        return self.result(candidates=found, notes=notes)


class UserSupplied(Provider):
    """Numbers you told the tool yourself. Outranks anything discovered."""

    name = "user-supplied"
    kind = LOCAL
    basis = "you supplied it"

    def __init__(self, numbers: list | None = None):
        self.numbers = list(numbers or [])

    def unavailable(self, ctx):
        return "" if self.numbers else "nothing supplied"

    def fetch(self, ctx):
        return self.result(candidates=[
            website.FoundPhone(raw=raw, context="supplied by you", owner=ctx.subject.name.display,
                               scope="person", how="banked", source_url=USER_SOURCE)
            for raw in self.numbers])


# ------------------------------------------------------- authorized data providers
class AuthorizedProvider(Provider):
    """Template for a licensed data source you have a contract with.

    Subclass this for a provider you are entitled to query - a paid enrichment
    API, your own CRM, a partner's directory you have written permission to read.
    Two rules the base class enforces rather than trusts you to remember:

      * it refuses to run unless its credential is actually configured, so a
        half-set-up provider fails loudly instead of silently returning nothing;
      * every candidate it returns carries `source_url` and the licence under
        which it came, so a number sourced from a contract is never confused
        with one seen on a public page.

    What this is not a route to: repackaged breach data or scraped broker
    datasets. If a vendor cannot tell you where a number came from and on what
    basis they hold it, the contract does not make the data usable - it only
    moves the liability.
    """

    kind = AUTHORIZED
    env_key = ""            # environment variable holding the credential
    env_url = ""            # environment variable holding the endpoint

    @property
    def credential(self) -> str:
        return os.environ.get(self.env_key, "").strip() if self.env_key else ""

    @property
    def endpoint(self) -> str:
        return os.environ.get(self.env_url, "").strip() if self.env_url else ""

    def unavailable(self, ctx):
        if not self.credential:
            return f"not configured (set {self.env_key})"
        if self.env_url and not self.endpoint:
            return f"not configured (set {self.env_url})"
        return ""

    def parse(self, payload, ctx: LookupContext) -> list:
        """Map the vendor's JSON to FoundPhone. Implement in the subclass."""
        raise NotImplementedError

    def request(self, ctx: LookupContext):
        """Call the vendor. Implement in the subclass; use `fetch.get_json`."""
        raise NotImplementedError

    def fetch(self, ctx):
        cached = None
        if ctx.db is not None:
            cached = ctx.db.get_provider_cache(self.name, ctx.subject.key)
        payload = cached if cached is not None else self.request(ctx)
        if payload is None:
            return self.result(error="provider returned nothing")
        if cached is None and ctx.db is not None:
            # Licensed calls are metered and usually billed. Cache before parsing
            # so a parser bug does not cost the same query twice.
            ctx.db.put_provider_cache(self.name, ctx.subject.key, payload)
        found = self.parse(payload, ctx)
        for candidate in found:
            candidate.how = candidate.how or "authorized-api"
        return self.result(candidates=found,
                           notes=[f"{len(found)} record(s)"
                                  + (" (cached)" if cached is not None else "")])


class ExampleAuthorizedProvider(AuthorizedProvider):
    """A worked example. Off unless both environment variables are set.

    Assumes a vendor that takes an email and returns
    `{"phones": [{"number": ..., "type": ..., "source_url": ..., "label": ...}]}`.
    Real vendors differ; the point is the shape, not the field names.
    """

    name = "example-authorized"
    env_key = "REACHOUT_AUTHORIZED_KEY"
    env_url = "REACHOUT_AUTHORIZED_URL"
    basis = "licensed under your contract with the provider"

    def request(self, ctx):
        from .fetch import get_json
        query = urllib.parse.urlencode({
            "email": ctx.subject.work_email, "name": ctx.subject.name.display,
            "company": ctx.subject.company})
        return get_json(f"{self.endpoint}?{query}",
                        headers={"Authorization": f"Bearer {self.credential}",
                                 "Accept": "application/json"})

    def parse(self, payload, ctx):
        found = []
        for record in (payload or {}).get("phones", []):
            number = (record.get("number") or "").strip()
            if not number:
                continue
            found.append(website.FoundPhone(
                raw=number, context=record.get("label") or "",
                owner=ctx.subject.name.display, scope="person", how="authorized-api",
                source_url=record.get("source_url") or self.endpoint,
                emails=[ctx.subject.work_email] if ctx.subject.work_email else []))
        return found


# ------------------------------------------------------------------- the registry
DEFAULT_PROVIDERS: list = [
    BankedNumbers(), CompanyWebsite(), StaffDirectory(), PublicDocuments(),
    PersonalProfiles(), PublicPages(), ExampleAuthorizedProvider(),
]

_REGISTRY: list = list(DEFAULT_PROVIDERS)


def register(provider: Provider, replace: bool = True) -> None:
    """Add a provider to the default set. Later registrations win by name."""
    global _REGISTRY
    if replace:
        _REGISTRY = [p for p in _REGISTRY if p.name != provider.name]
    _REGISTRY.append(provider)


def unregister(name: str) -> None:
    global _REGISTRY
    _REGISTRY = [p for p in _REGISTRY if p.name != name]


def registry() -> list:
    return list(_REGISTRY)


def reset_registry() -> None:
    global _REGISTRY
    _REGISTRY = list(DEFAULT_PROVIDERS)


def select(only: list | None = None, exclude: list | None = None) -> list:
    """The providers to run, honouring an explicit allow- or deny-list."""
    chosen = registry()
    if only:
        wanted = {n.strip() for n in only if n.strip()}
        unknown = wanted - {p.name for p in chosen}
        if unknown:
            raise ValueError(f"unknown provider(s): {', '.join(sorted(unknown))}. "
                             f"Known: {', '.join(p.name for p in chosen)}")
        chosen = [p for p in chosen if p.name in wanted]
    if exclude:
        blocked = {n.strip() for n in exclude if n.strip()}
        chosen = [p for p in chosen if p.name not in blocked]
    return chosen


def describe() -> list:
    """(name, kind, basis, configured) for every registered provider."""
    blank = LookupContext(subject=Subject(name=parse_name("A B"), domain="example.com"))
    out = []
    for provider in registry():
        reason = provider.unavailable(blank)
        out.append({"name": provider.name, "kind": provider.kind, "basis": provider.basis,
                    "ready": not reason.startswith("not configured"), "note": reason})
    return out
