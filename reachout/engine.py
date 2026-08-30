"""The pipeline: a name (and maybe a company) in, ranked addresses with evidence out."""

from __future__ import annotations

import time
from dataclasses import dataclass, field

from . import company as company_mod
from . import netdns, patterns, phonefind, verify
from .db import USER_SOURCE, Database
from .names import Name, from_linkedin_url, parse as parse_name, slug
from .patterns import Candidate
from .scoring import Score, score_candidate
from .sources import github, search as websearch, website

MIN_SAMPLES_BEFORE_HARVEST = 6


def _leading_subsequence(token: str, brand: str) -> int:
    """How many of `token`'s leading characters occur, in order, inside `brand`."""
    pos = matched = 0
    for char in token:
        found = brand.find(char, pos)
        if found < 0:
            break
        pos = found + 1
        matched += 1
    return matched


def is_company_handle(token: str, brand: str) -> bool:
    """True when a name token is really the employer glued onto a login.

    Indian engineering teams commit under handles like 'rohitr raz',
    'abhishek rzp' and 'prashantupadhyay rzpx' - the trailing token abbreviates
    razorpay, it is not a surname. Testing against the full brand catches
    'Suresh Hashicorp' and misses every abbreviation, so match a short token
    whose leading characters are a subsequence of the brand.
    """
    if not token or not brand:
        return False
    if token == brand:
        return True
    return len(token) <= 5 and _leading_subsequence(token, brand) >= 3


@dataclass
class EmailResult:
    address: str
    score: Score
    template: str = ""
    status: str = "candidate"
    sources: list = field(default_factory=list)
    verdict: object = None

    @property
    def confidence(self) -> int:
        return self.score.value

    @property
    def grade(self) -> str:
        return self.score.grade


@dataclass
class FindResult:
    name: Name
    company: str = ""
    domain: str = ""
    domain_confidence: float = 0.0
    domain_evidence: list = field(default_factory=list)
    domain_facts: dict = field(default_factory=dict)
    pattern: str = ""
    pattern_confidence: float = 0.0
    pattern_samples: int = 0
    pattern_pinned: bool = False
    emails: list = field(default_factory=list)   # list[EmailResult]
    phones: object = None                        # phonefind.PhoneReport when asked for
    profile: dict = field(default_factory=dict)  # discovered GitHub/site profile
    learned: int = 0                             # new pattern samples stored this run
    warnings: list = field(default_factory=list)
    elapsed: float = 0.0

    @property
    def best(self):
        return self.emails[0] if self.emails else None


def load_patterns(domain: str, db: Database) -> list[patterns.PatternGuess]:
    """Everything known about a domain's format: what you told us, then what we inferred.

    A pinned pattern always ranks first - if you have stated how a company builds
    its addresses, that outranks anything harvested.
    """
    inferred = {row["template"]: row for row in db.get_patterns(domain)}
    out: list[patterns.PatternGuess] = []
    seen: set[str] = set()
    for row in db.pinned_patterns(domain):
        # Keep the sample count from the inferred row: a stated pattern that real
        # addresses also agree with is stronger than a bare assertion.
        backing = inferred.get(row["template"])
        out.append(patterns.PatternGuess(row["template"], 0.0,
                                         backing["samples"] if backing else 0,
                                         float(row["confidence"]), pinned=True))
        seen.add(row["template"])
    for template, row in inferred.items():
        if template in seen:
            continue
        out.append(patterns.PatternGuess(template, row["votes"], row["samples"],
                                         row["confidence"]))
    return out


def teach(domain: str, display_name: str, address: str, db: Database) -> dict:
    """Record an address you already know to be real.

    The strongest input the tool can get: a confirmed (name -> address) pair needs
    no harvesting, no guessing, and immediately teaches the company's format.
    """
    domain = domain.lower().strip().lstrip("@")
    address = address.lower().strip()
    local, _, addr_domain = address.partition("@")
    result = {"address": address, "domain": domain, "template": "", "stored": False,
              "banked": False, "error": ""}
    if not local or addr_domain != domain:
        result["error"] = f"{address} is not an address at {domain}"
        return result

    result["banked"] = db.add_domain_email(domain, address, display_name, USER_SOURCE,
                                           website.is_role_account(address))
    parsed = parse_name(display_name)
    if not parsed.ok:
        result["error"] = f"could not parse a first and last name from {display_name!r}"
        return result

    hits = patterns.matching_templates(local, parsed)
    if not hits:
        result["error"] = (f"'{local}' cannot be built from '{parsed.display}' by any known "
                           f"format - address banked, but it teaches no pattern")
        return result

    result["template"] = hits[0]
    result["stored"] = db.add_sample(domain, parsed.first, parsed.last, local, hits[0],
                                     USER_SOURCE)
    return result


def bulk_teach(pairs, db: Database) -> dict:
    """Record a whole address book at once.

    Your own correspondence is ground truth nobody could sell you: every company
    you have ever emailed arrives with at least one confirmed (name -> address)
    pair. Consumer addresses teach nothing about a company's format, so they are
    counted and skipped rather than banked.
    """
    touched: dict[str, list] = {}
    banked = taught = skipped = 0
    for display, address in pairs:
        domain = address.split("@")[-1]
        if domain in verify.FREE_PROVIDERS:
            skipped += 1
            continue
        outcome = teach(domain, display, address, db)
        banked += bool(outcome["banked"])
        taught += bool(outcome["stored"])
        touched.setdefault(domain, []).append(outcome)
    for domain in touched:
        refresh_pattern(domain, db)

    learned = []
    for domain in sorted(touched, key=lambda d: -len(touched[d])):
        rows = db.get_patterns(domain)
        if rows and rows[0]["confidence"] >= 0.5:
            learned.append({"domain": domain, "template": rows[0]["template"],
                            "confidence": float(rows[0]["confidence"]),
                            "samples": int(rows[0]["samples"])})
    return {"contacts": len(pairs), "banked": banked, "taught": taught, "skipped": skipped,
            "domains": len(touched), "learned": learned}


def _org_candidates(domain: str, org_hint: str = "") -> list[str]:
    """Plausible GitHub org handles for a company, best guess first."""
    brand = domain.split(".")[0]
    out = [c for c in (org_hint, brand, brand.replace("-", "")) if c]
    # "iitb.ac.in" -> also try the full second-level label
    if "-" in brand:
        out.append(brand.split("-")[0])
    seen, ordered = set(), []
    for item in out:
        item = item.strip().lower().lstrip("@")
        if item and item not in seen:
            seen.add(item)
            ordered.append(item)
    return ordered


def _store_samples(db: Database, domain: str, triples, source_url: str = "") -> int:
    """triples: iterable of (display_name, address, source_url).

    A sample is only worth storing when the display name is plausibly a real
    person's name. Git display names are frequently logins ("dgehring-grafana",
    "suresh hashicorp"), and feeding those in teaches the wrong pattern.
    """
    brand = domain.split(".")[0].lower()
    learned = 0
    for display, address, url in triples:
        if "@" not in address:
            continue
        local, _, addr_domain = address.partition("@")
        if addr_domain.lower() != domain.lower():
            continue
        if not github.is_usable_email(address):
            continue
        # A shared or machine mailbox has no naming pattern to teach.
        if website.is_role_account(address):
            continue
        parsed = parse_name(display)
        if not parsed.ok:
            continue
        # "Suresh Hashicorp" is a username with the company glued on, not a surname.
        if brand and (parsed.last == brand or parsed.first == brand):
            continue
        # Store the reading that actually renders `local`, not the default split -
        # otherwise the row cannot rebuild its own address. See patterns.matching_reading.
        hit = patterns.matching_reading(local, parsed)
        reading, template = hit if hit else (parsed, "")
        # Abbreviated employer handles, which the exact-brand test above misses.
        # A row whose name rebuilds its own address is trustworthy whatever it
        # looks like, so only discard when nothing matched *and* the company is
        # sitting in the name - otherwise the row contributes shape votes only,
        # and those are what drag a domain's real pattern down.
        if not template and brand and (is_company_handle(parsed.last, brand)
                                       or is_company_handle(parsed.first, brand)):
            continue
        if db.add_sample(domain, reading.first, reading.last, local, template,
                         url or source_url):
            learned += 1
    return learned


def learn_domain(domain: str, db: Database, org_hint: str = "", max_repos: int = 4,
                 use_github: bool = True, use_website: bool = True,
                 use_search: bool = True) -> dict:
    """Gather real name->address pairs at a domain and (re)infer its pattern."""
    domain = domain.lower().strip().lstrip("@")
    report = {"domain": domain, "learned": 0, "sources": [], "errors": []}

    if use_github:
        # A company's GitHub org is usually its brand, not its legal name:
        # "Grafana Labs" -> grafana.com -> @grafana. Try the likely handles in order.
        tried: list[str] = []
        for org in _org_candidates(domain, org_hint):
            if org in tried:
                continue
            tried.append(org)
            try:
                harvest = github.harvest_org(org, max_repos=max_repos, domain_filter=domain)
            except github.RateLimited as exc:
                report["errors"].append(str(exc))
                break
            except Exception as exc:
                report["errors"].append(f"github: {exc}")
                continue
            if harvest["pairs"]:
                report["learned"] += _store_samples(db, domain, harvest["pairs"])
                for display, address, source in harvest["pairs"]:
                    db.add_domain_email(domain, address, display, source,
                                        website.is_role_account(address))
                report["sources"].append(f"github:@{org} ({len(harvest['repos'])} repos, "
                                         f"{len(harvest['pairs'])} addresses)")
                break
        else:
            report["errors"].append(
                f"github: no public commits at @{'/@'.join(tried)} using {domain} addresses")

    if use_website:
        try:
            site = website.harvest_site(domain, max_pages=6)
            if site.get("client_rendered"):
                report["errors"].append(
                    f"{domain} is a JavaScript app - its served HTML carries no content")
            paired = website.pair_samples(site["emails"], site["names"], domain)
            for first, last, local, template in paired:
                if db.add_sample(domain, first, last, local, template, site["pages"][0] if site["pages"] else ""):
                    report["learned"] += 1
            if paired:
                report["sources"].append(f"website:{domain} ({len(paired)} name/address pairs)")
            # An address we could not pair with a name today may still be the answer
            # for someone we look up tomorrow, so keep all of them.
            page = site["pages"][0] if site["pages"] else f"https://{domain}"
            kept = 0
            for address, context in site["emails"].items():
                if db.add_domain_email(domain, address, context, page,
                                       website.is_role_account(address)):
                    kept += 1
            if kept:
                report["sources"].append(f"website:{domain} ({kept} published addresses)")
        except Exception as exc:
            report["errors"].append(f"website: {exc}")

    if use_search:
        try:
            hits = websearch.search(f'"@{domain}" email contact', count=8)
            found = websearch.emails_in_results(hits, domain_filter=domain)
            triples = []
            for address, url in found:
                for hit in hits:
                    if hit.url == url:
                        triples.append((hit.title.split("-")[0].split("|")[0].strip(), address, url))
                        break
            gained = _store_samples(db, domain, triples)
            report["learned"] += gained
            if gained:
                report["sources"].append(f"search ({gained} pairs)")
        except Exception as exc:
            report["errors"].append(f"search: {exc}")

    refresh_pattern(domain, db)
    return report


def refresh_pattern(domain: str, db: Database) -> list[patterns.PatternGuess]:
    rows = db.samples_for_domain(domain)
    guesses = patterns.infer([(r["first"], r["last"], r["local"]) for r in rows])
    db.save_patterns(domain, [g.as_tuple() for g in guesses])
    return guesses


def _find_person_on_github(name: Name, company_name: str, domain: str) -> dict:
    """Look for this specific person's public commit addresses."""
    profile = {"login": "", "match": 0.0, "emails": [], "url": "", "title": "", "location": ""}
    try:
        hits = github.search_users(name.display, company_name or domain.split(".")[0], limit=4)
    except github.RateLimited:
        return profile
    except Exception:
        return profile

    best_user, best_score = None, 0.0
    for hit in hits:
        try:
            user = github.get_user(hit["login"])
        except Exception:
            continue
        if not user:
            continue
        score = github.match_score(user, name.display, company_name)
        if score > best_score:
            best_user, best_score = user, score

    if not best_user or best_score < 0.5:
        return profile

    profile.update({"login": best_user.login, "match": best_score,
                    "url": best_user.profile_url, "title": best_user.bio,
                    "location": best_user.location, "company": best_user.company})
    found = list(best_user.emails)
    try:
        for _display, address, source in github.emails_from_events(best_user.login):
            found.append((address, source))
    except Exception:
        pass

    seen, deduped = set(), []
    for address, source in found:
        address = address.lower()
        if address in seen:
            continue
        seen.add(address)
        deduped.append((address, source))
    profile["emails"] = deduped
    return profile


def find(query: str, company_name: str = "", db: Database | None = None,
         use_github: bool = True, use_website: bool = True, use_search: bool = True,
         use_smtp: bool = False, limit: int = 6, force_learn: bool = False,
         want_phones: bool = False) -> FindResult:
    started = time.time()
    owns_db = db is None
    db = db or Database()

    if "linkedin.com/" in (query or ""):
        name, handle = from_linkedin_url(query)
        linkedin_url = query
    else:
        name, handle, linkedin_url = parse_name(query), "", ""

    result = FindResult(name=name)
    if not name.first:
        result.warnings.append("could not parse a name from the input")
        result.elapsed = time.time() - started
        if owns_db:
            db.close()
        return result

    # ---- 1. company -> mail domain -------------------------------------------
    guess = company_mod.resolve(company_name, db=db, use_search=use_search,
                                use_github=use_github) if company_name else \
        company_mod.DomainGuess("", 0.0, ["no company supplied"])
    result.company = company_name
    result.domain = guess.domain
    result.domain_confidence = guess.confidence
    result.domain_evidence = guess.evidence

    if not result.domain:
        result.warnings.append(
            "no company domain - give a company name or domain to get work-address candidates")

    if result.domain:
        facts = db.get_domain_facts(result.domain)
        if not facts:
            facts = netdns.describe(result.domain)
            db.save_domain_facts(facts)
        result.domain_facts = facts

    # ---- 2. learn the domain's naming pattern (cached across runs) ------------
    guesses: list[patterns.PatternGuess] = []
    if result.domain:
        existing = db.samples_for_domain(result.domain)
        if force_learn or len(existing) < MIN_SAMPLES_BEFORE_HARVEST:
            report = learn_domain(result.domain, db,
                                  org_hint=slug(company_mod.normalise_company(company_name)),
                                  use_github=use_github, use_website=use_website,
                                  use_search=use_search)
            result.learned = report["learned"]
            result.warnings.extend(report["errors"])
            refresh_pattern(result.domain, db)
            guesses = load_patterns(result.domain, db)
        else:
            guesses = load_patterns(result.domain, db)
            if not guesses:
                refresh_pattern(result.domain, db)
                guesses = load_patterns(result.domain, db)

        if guesses:
            result.pattern = guesses[0].template
            result.pattern_confidence = guesses[0].confidence
            result.pattern_samples = guesses[0].samples
            result.pattern_pinned = guesses[0].pinned
            if result.pattern_pinned:
                # A pinned pattern has no samples of its own, but real samples for
                # the same template are still worth reporting.
                for other in guesses:
                    if other.template == result.pattern and other.samples:
                        result.pattern_samples = other.samples
                        break

    # ---- 3. hunt for this person's address directly --------------------------
    observed: dict[str, list[str]] = {}
    confirmed: set[str] = set()
    person_match = 0.0
    if use_github:
        profile = _find_person_on_github(name, company_name, result.domain)
        if profile["login"]:
            result.profile = profile
            person_match = profile["match"]
            for address, source in profile["emails"]:
                observed.setdefault(address, []).append(source)

    # Anything already banked for this domain that this person's name explains is a
    # published address for them - no new network calls needed.
    if result.domain:
        for row in db.domain_emails(result.domain):
            local = row["address"].split("@")[0]
            if patterns.matching_templates(local, name):
                if row["source_url"] == USER_SOURCE:
                    confirmed.add(row["address"])
                    observed.setdefault(row["address"], []).append(USER_SOURCE)
                else:
                    observed.setdefault(row["address"], []).append(
                        row["source_url"] or f"https://{result.domain}")

    if use_search and result.domain:
        try:
            query_text = f'"{name.display}" "{result.domain}" email'
            hits = websearch.search(query_text, count=8)
            for address, url in websearch.emails_in_results(hits, domain_filter=result.domain):
                observed.setdefault(address, []).append(url)
        except Exception as exc:
            result.warnings.append(f"search: {exc}")

    # ---- 4. build the candidate set ------------------------------------------
    candidates: list[Candidate] = []
    seen: set[str] = set()

    for address, sources in observed.items():
        local = address.split("@")[0]
        hits = patterns.matching_templates(local, name)
        candidates.append(Candidate(address, hits[0] if hits else "", 0.9, "observed"))
        seen.add(address)

    if result.domain:
        for candidate in patterns.generate(name, result.domain, guesses, limit=limit + 2):
            if candidate.address not in seen:
                candidates.append(candidate)
                seen.add(candidate.address)

    # ---- 5. verify and score --------------------------------------------------
    company_id = db.upsert_company(company_name, result.domain, guess.confidence, "find") \
        if company_name else None
    person_id = db.upsert_person(name, company_id, linkedin=linkedin_url,
                                 github=result.profile.get("url", ""),
                                 title=result.profile.get("title", ""),
                                 location=result.profile.get("location", ""))

    scored: list[EmailResult] = []
    for candidate in candidates:
        verdict = verify.verify(candidate.address, db=db, use_smtp=use_smtp)
        sources = observed.get(candidate.address, [])
        is_confirmed = candidate.address in confirmed
        score = score_candidate(candidate, verdict, name, observed_sources=sources,
                                person_match=person_match,
                                pattern_samples=result.pattern_samples,
                                user_confirmed=is_confirmed)
        status = "confirmed" if is_confirmed else ("observed" if sources else "candidate")
        scored.append(EmailResult(address=candidate.address, score=score,
                                  template=candidate.template, status=status,
                                  sources=sources, verdict=verdict))

    scored.sort(key=lambda e: -e.confidence)
    result.emails = scored[:limit]

    for entry in result.emails:
        email_id = db.add_email(person_id, entry.address, entry.confidence, entry.grade,
                                entry.status, entry.template)
        for signal in entry.score.signals:
            db.add_observation(signal.key, signal.label, signal.source_url, signal.points,
                               person_id=person_id, email_id=email_id)
        # An address seen in public, whose local-part its owner's name explains,
        # is training data for next time. File the reading that actually renders
        # the local part: the template may come from an alternate reading of the
        # name, and filing it against the primary one writes a row that cannot
        # rebuild its own address. Same reason _store_samples uses it.
        if entry.status == "observed" and entry.template and result.domain:
            if entry.address.endswith("@" + result.domain):
                local = entry.address.split("@")[0]
                hit = patterns.matching_reading(local, name)
                reading, template = hit if hit else (name, entry.template)
                db.add_sample(result.domain, reading.first, reading.last,
                              local, template,
                              entry.sources[0] if entry.sources else "")

    if want_phones and (result.domain or company_name):
        result.phones = phonefind.find_phones(
            name, result.domain, db=db, company=company_name,
            use_website=use_website, use_search=use_search)
        result.warnings.extend(result.phones.warnings)
        # Only numbers actually attributed to this person are filed against them.
        # A switchboard belongs to the company, and it is already banked in
        # domain_phones - writing it here too makes the person's record claim a
        # number that does not reach them.
        for entry in result.phones.personal:
            db.add_phone(person_id, entry.e164, entry.kind, entry.country, entry.how,
                         entry.attribution, entry.confidence, entry.grade,
                         "observed", entry.source_url, entry.context)

    db.log_search(name.display, company_name, len(result.emails),
                  result.best.address if result.best else "")
    result.elapsed = round(time.time() - started, 2)
    if owns_db:
        db.close()
    return result
