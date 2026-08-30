"""Command line interface."""

from __future__ import annotations

import argparse
import csv
import json
import sys
from pathlib import Path

from . import company as company_mod, config, engine, enrich as enrich_mod, netdns, patterns, phonefind
from . import providers as providers_mod
from . import verify
from . import phones as phonelib
from .db import Database, USER_SOURCE
from .names import parse as parse_name
from .sources import github

USE_COLOUR = sys.stdout.isatty()


def paint(text: str, code: str) -> str:
    return f"\033[{code}m{text}\033[0m" if USE_COLOUR else text


BOLD = lambda s: paint(s, "1")          # noqa: E731
DIM = lambda s: paint(s, "2")           # noqa: E731
GREEN = lambda s: paint(s, "32")        # noqa: E731
YELLOW = lambda s: paint(s, "33")       # noqa: E731
RED = lambda s: paint(s, "31")          # noqa: E731
CYAN = lambda s: paint(s, "36")         # noqa: E731

GRADE_COLOUR = {"HIGH": GREEN, "MEDIUM": YELLOW, "LOW": RED, "VERY LOW": DIM}


def bar(value: int, width: int = 18) -> str:
    filled = round(width * value / 100)
    return "█" * filled + "·" * (width - filled)


# --------------------------------------------------------------------- output
def print_find(result, show_evidence: bool = True) -> None:
    name = result.name
    print()
    print(BOLD(name.display or "(unparsed name)"))
    bits = []
    if result.profile.get("title"):
        bits.append(result.profile["title"][:70])
    if result.company:
        bits.append(result.company)
    if result.domain:
        provider = result.domain_facts.get("provider", "")
        bits.append(result.domain + (f" ({provider})" if provider else ""))
    if bits:
        print(DIM(" · ".join(bits)))

    if result.domain:
        print(DIM(f"  domain confidence {result.domain_confidence:.0%} — "
                  + "; ".join(result.domain_evidence[:2])))
    if result.pattern:
        if result.pattern_pinned:
            backing = (f"you set this; {result.pattern_samples} real address(es) agree"
                       if result.pattern_samples else "you set this")
        else:
            backing = f"from {result.pattern_samples} samples"
            if result.learned:
                backing += f", {result.learned} newly learned"
        print(DIM(f"  pattern {patterns.describe(result.pattern)} — "
                  f"{result.pattern_confidence:.0%} confident ({backing})"))
    if result.profile.get("login"):
        print(DIM(f"  github @{result.profile['login']} "
                  f"(match {result.profile['match']:.0%}) {result.profile['url']}"))

    if not result.emails:
        print(RED("\n  no candidates produced"))
        for warning in result.warnings:
            print(DIM(f"  ! {warning}"))
        return

    print()
    for index, entry in enumerate(result.emails, 1):
        colour = GRADE_COLOUR.get(entry.grade, DIM)
        flag = {"confirmed": CYAN(" ← you confirmed this"),
                "observed": CYAN(" ← found published")}.get(entry.status, "")
        print(f"  {index}. {BOLD(entry.address):<44} {bar(entry.confidence)} "
              f"{colour(f'{entry.confidence:3d} {entry.grade}')}{flag}")
        if not show_evidence:
            continue
        for signal in entry.score.signals:
            if signal.points == 0:
                continue
            mark = GREEN("+") if signal.points > 0 else RED("−")
            points = f"{abs(signal.points):>4.0f}"
            print(f"       {mark} {signal.label} {DIM(points)}")
            if signal.source_url:
                print(DIM(f"         {signal.source_url}"))
        for cap in entry.score.caps:
            print(DIM(f"       · {cap}"))
        print()

    if result.phones is not None:
        print_phones(result.phones, result.name.display, show_evidence)

    for warning in result.warnings:
        print(DIM(f"  ! {warning}"))
    print(DIM(f"  {result.elapsed}s"))


def print_phones(report, name_display: str, show_evidence: bool = True) -> None:
    if report is None:
        return
    print()
    print(BOLD(f"  phone — numbers that reach {name_display}"))
    if not report.personal:
        print(DIM("    none found. No public page ties a number to this person."))
    for entry in report.personal:
        colour = GRADE_COLOUR.get(entry.grade, DIM)
        certainty = "" if entry.kind_certain else DIM(" (line type unverifiable here)")
        print(f"    {BOLD(entry.pretty):<34} {entry.kind}{certainty} "
              f"{colour(f'{entry.confidence:3d} {entry.grade}')}")
        if not show_evidence:
            continue
        for signal in entry.score.signals:
            if signal.points:
                mark = GREEN("+") if signal.points > 0 else RED("−")
                print(f"       {mark} {signal.label} {DIM(f'{abs(signal.points):.0f}')}")
        for cap in entry.score.caps:
            print(DIM(f"       · {cap}"))

    if report.company:
        print()
        print(DIM(f"  company numbers — these reach the switchboard, not {name_display}"))
        for entry in report.company:
            # Line type and context label answer different questions. Printing
            # only the context turns a landline into "personal", which is the
            # exact confusion this module exists to prevent.
            label = entry.kind if entry.kind_certain or entry.kind != phonelib.UNKNOWN \
                else "line type unknown"
            signal = phonelib.context_signal(entry.context)
            if signal:
                label = f"{label} · labelled {signal}"
            print(DIM(f"    {entry.pretty:<32} {label}"))
    if report.checked and not report.personal and not report.company:
        print(DIM(f"    ({report.checked} candidate strings checked, none were valid numbers)"))


STATUS_COLOUR = {enrich_mod.FOUND: GREEN, enrich_mod.NEEDS_REVIEW: YELLOW,
                 enrich_mod.NOT_FOUND: DIM}


def print_enrichment(lookup, show_evidence: bool = True) -> None:
    subject = lookup.subject
    tint = STATUS_COLOUR.get(lookup.status, DIM)
    print()
    header = BOLD(subject.name.display)
    if subject.work_email:
        header += DIM(f"  ·  {subject.work_email}")
    elif subject.domain:
        header += DIM(f"  ·  {subject.domain}")
    print("  " + header)
    print(f"  {tint(BOLD(lookup.status.upper()))} — {lookup.reason}")

    if lookup.status == enrich_mod.FOUND:
        best = lookup.answer
        certainty = "" if best.kind_certain else DIM(" (line type unverifiable here)")
        print()
        print(f"  {BOLD(best.pretty)}   {best.kind}{certainty}   "
              f"{tint(f'{best.confidence} {best.grade}')}")
        print(DIM(f"  seen at {best.source_url}"))

    others = lookup.candidates[1:] if lookup.status == enrich_mod.FOUND else lookup.candidates
    if others:
        print()
        print(DIM("  other candidates"))
        for candidate in others:
            colour = GRADE_COLOUR.get(candidate.grade, DIM)
            print(f"    {candidate.pretty:<22} {candidate.kind:<9} "
                  f"{colour(f'{candidate.confidence:3d} {candidate.grade}')}  "
                  + DIM(candidate.source_url[:52]))

    if show_evidence:
        for candidate in lookup.candidates:
            print()
            print(f"  evidence for {BOLD(candidate.pretty)}"
                  + DIM(f"  ({len(candidate.hosts)} independent source(s))"))
            for signal in candidate.score.signals:
                mark = GREEN("+") if signal.points > 0 else RED("−")
                print(f"     {mark} {signal.label} {DIM(f'{abs(signal.points):.0f}')}")
            for cap in candidate.score.caps:
                print(DIM(f"     · {cap}"))
            for clash in candidate.contradictions:
                print(YELLOW(f"     ! {clash}"))
            for item in candidate.evidence:
                print(DIM(f"     ↳ {item.how:<10} {item.source_url}"))

    if lookup.company_numbers:
        print()
        print(DIM(f"  company numbers — these reach {subject.company or 'the switchboard'}, "
                  f"not {subject.name.display}"))
        for candidate in lookup.company_numbers:
            print(DIM(f"    {candidate.pretty:<22} {candidate.kind}"))

    print()
    print(DIM("  sources consulted"))
    for run in lookup.providers:
        mark = {"ok": GREEN("✓"), "skipped": DIM("–"), "error": RED("✗"),
                "degraded": YELLOW("!")}[run.state]
        detail = (run.skipped or run.error or run.degraded
                  or f"{run.found} sighting(s), {run.elapsed}s")
        print(f"    {mark} {run.name:<20} {DIM(detail[:70])}")
        if show_evidence:
            for note in run.notes[:4]:
                print(DIM(f"        {note[:88]}"))

    for warning in lookup.warnings:
        print(YELLOW(f"  ! {warning}"))
    if lookup.dropped:
        print(DIM(f"  {lookup.dropped} sighting(s) dropped: no source URL to check them against"))
    print(DIM(f"  {lookup.coverage} worked · {lookup.checked} sighting(s) considered "
              f"· {lookup.elapsed}s"))
    if lookup.status != enrich_mod.FOUND:
        print(DIM("  Phone numbers are never inferred or generated - only reported when "
                  "already published somewhere public."))


def result_to_dict(result) -> dict:
    return {
        "name": result.name.display,
        "parsed": result.name.as_dict(),
        "company": result.company,
        "domain": result.domain,
        "domain_confidence": result.domain_confidence,
        "domain_evidence": result.domain_evidence,
        "domain_facts": result.domain_facts,
        "pattern": result.pattern,
        "pattern_confidence": result.pattern_confidence,
        "pattern_samples": result.pattern_samples,
        "pattern_pinned": result.pattern_pinned,
        "profile": result.profile,
        "learned": result.learned,
        "warnings": result.warnings,
        "elapsed": result.elapsed,
        "phones": {
            "personal": [{"number": p.e164, "pretty": p.pretty, "kind": p.kind,
                          "kind_certain": p.kind_certain, "confidence": p.confidence,
                          "grade": p.grade, "context": p.context,
                          "signals": [{"label": s.label, "points": s.points}
                                      for s in p.score.signals],
                          "caps": p.score.caps}
                         for p in result.phones.personal],
            "company": [{"number": p.e164, "pretty": p.pretty, "kind": p.kind}
                        for p in result.phones.company],
        } if result.phones is not None else None,
        "emails": [
            {
                "address": e.address,
                "confidence": e.confidence,
                "grade": e.grade,
                "status": e.status,
                "template": e.template,
                "sources": e.sources,
                "signals": [{"key": s.key, "label": s.label, "points": s.points,
                             "source": s.source_url} for s in e.score.signals],
                "caps": e.score.caps,
                "checks": {
                    "syntax_ok": e.verdict.syntax_ok,
                    "has_mx": e.verdict.has_mx,
                    "provider": e.verdict.provider,
                    "is_role": e.verdict.is_role,
                    "is_free": e.verdict.is_free,
                    "catch_all": e.verdict.catch_all,
                    "smtp": e.verdict.smtp,
                    "smtp_detail": e.verdict.smtp_detail,
                } if e.verdict else {},
            }
            for e in result.emails
        ],
    }


# ------------------------------------------------------------------- commands
def cmd_find(args) -> int:
    with Database() as db:
        result = engine.find(
            args.name, company_name=args.company or "", db=db,
            use_github=not args.no_github, use_website=not args.no_web,
            use_search=not args.no_search, use_smtp=args.smtp,
            limit=args.limit, force_learn=args.relearn, want_phones=args.phone,
        )
    if args.json:
        print(json.dumps(result_to_dict(result), indent=2))
    else:
        print_find(result, show_evidence=not args.quiet)
    return 0 if result.emails else 1


def cmd_learn(args) -> int:
    with Database() as db:
        report = engine.learn_domain(
            args.domain, db, org_hint=args.org or "", max_repos=args.repos,
            use_github=not args.no_github, use_website=not args.no_web,
            use_search=not args.no_search)
        guesses = db.get_patterns(args.domain)
    print(f"\n{BOLD(args.domain)}  +{report['learned']} new samples")
    for source in report["sources"]:
        print(DIM(f"  from {source}"))
    for error in report["errors"]:
        print(DIM(f"  ! {error}"))
    if not guesses:
        print(RED("  no pattern could be inferred yet"))
        return 1
    print()
    for row in guesses[:5]:
        print(f"  {patterns.describe(row['template']):<40} "
              f"{bar(int(row['confidence'] * 100))} {row['confidence']:.0%} "
              + DIM(f"({row['votes']}/{row['samples']} samples)"))
    return 0


def cmd_pattern(args) -> int:
    with Database() as db:
        rows = db.get_patterns(args.domain)
        samples = db.samples_for_domain(args.domain)
    if not rows:
        print(f"nothing known about {args.domain} yet — run: reachout learn {args.domain}")
        return 1
    print(f"\n{BOLD(args.domain)} — {len(samples)} samples")
    for row in rows[:6]:
        print(f"  {patterns.describe(row['template']):<40} "
              f"{bar(int(row['confidence']*100))} {row['confidence']:.0%}")
    if args.samples:
        print(DIM("\n  samples:"))
        for sample in samples[:40]:
            print(DIM(f"    {sample['first']} {sample['last']:<18} -> {sample['local']}"
                      f"@{args.domain}   {sample['template']}"))
    return 0


def cmd_guess(args) -> int:
    """Apply a known pattern to a name without doing any network discovery."""
    name = parse_name(args.name)
    with Database() as db:
        guesses = engine.load_patterns(args.domain, db)
    print()
    for candidate in patterns.generate(name, args.domain, guesses, limit=args.limit):
        print(f"  {candidate.address:<42} {candidate.prior:.0%}  {DIM(candidate.source)}")
    return 0


def cmd_verify(args) -> int:
    with Database() as db:
        verdict = verify.verify(args.address, db=db, use_smtp=args.smtp)
    print()
    print(BOLD(verdict.address))
    checks = [
        ("valid syntax", verdict.syntax_ok),
        ("domain resolves", verdict.domain_resolves),
        ("domain accepts mail (MX)", verdict.has_mx),
        ("not a role mailbox", not verdict.is_role),
        ("not a disposable domain", not verdict.is_disposable),
        ("company address (not consumer)", not verdict.is_free),
    ]
    for label, passed in checks:
        print(f"  {GREEN('✓') if passed else RED('✗')} {label}")
    if verdict.provider:
        print(DIM(f"  · mail provider: {verdict.provider}"))
    if verdict.catch_all == 1:
        print(YELLOW("  · catch-all domain: acceptance proves nothing"))
    elif verdict.catch_all == 0:
        print(DIM("  · not catch-all"))
    if verdict.smtp != "not-checked":
        colour = {"accepted": GREEN, "rejected": RED}.get(verdict.smtp, YELLOW)
        print(f"  · SMTP: {colour(verdict.smtp)} {DIM(verdict.smtp_detail)}")
    else:
        print(DIM("  · SMTP not checked (use --smtp; see docs/VERIFICATION.md)"))
    for note in verdict.notes:
        print(DIM(f"  · {note}"))
    print(DIM("\n  These are signals, not proof of delivery."))
    return 0 if verdict.syntax_ok and verdict.has_mx else 1


def cmd_github(args) -> int:
    if args.org:
        harvest = github.harvest_org(args.org, max_repos=args.repos,
                                     domain_filter=args.domain or "")
        print(f"\n{BOLD('@' + args.org)} — {len(harvest['pairs'])} addresses "
              f"from {len(harvest['repos'])} repos")
        for display, address, source in harvest["pairs"][:args.limit]:
            print(f"  {display[:26]:<28} {address}")
            if args.verbose:
                print(DIM(f"    {source}"))
        if harvest["error"]:
            print(RED(f"  {harvest['error']}"))
        if args.save:
            domain = args.domain or (harvest["pairs"][0][1].split("@")[-1] if harvest["pairs"] else "")
            if domain:
                with Database() as db:
                    stored = engine._store_samples(db, domain, harvest["pairs"])
                    engine.refresh_pattern(domain, db)
                print(GREEN(f"  saved {stored} new samples for {domain}"))
        return 0

    user = github.get_user(args.user)
    if not user:
        print(RED(f"no GitHub user '{args.user}'"))
        return 1
    print(f"\n{BOLD(user.name or user.login)}  @{user.login}")
    for label, value in [("company", user.company), ("location", user.location),
                         ("blog", user.blog), ("bio", user.bio)]:
        if value:
            print(DIM(f"  {label}: {value}"))
    addresses = list(user.emails)
    addresses += [(a, s) for _n, a, s in github.emails_from_events(user.login)]
    seen = set()
    print()
    for address, source in addresses:
        if address in seen:
            continue
        seen.add(address)
        print(f"  {address:<40} {DIM(source)}")
    if not seen:
        print(DIM("  no public addresses in the profile or recent public commits"))
    return 0


def cmd_batch(args) -> int:
    with Path(args.csv).open(encoding="utf-8") as handle:
        rows = list(csv.DictReader(handle))
    if not rows:
        print(RED("empty CSV"))
        return 1
    name_key = next((k for k in rows[0] if k.lower() in ("name", "full_name", "fullname")), None)
    company_key = next((k for k in rows[0] if k.lower() in
                        ("company", "employer", "organisation", "organization", "domain")), None)
    if not name_key:
        print(RED("CSV needs a 'name' column"))
        return 1

    out_path = Path(args.out)
    with Database() as db, out_path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(["name", "company", "domain", "best_email", "confidence", "grade",
                         "status", "pattern", "alternates"])
        for index, row in enumerate(rows, 1):
            name = (row.get(name_key) or "").strip()
            if not name:
                continue
            company_name = (row.get(company_key) or "").strip() if company_key else ""
            result = engine.find(name, company_name, db=db,
                                 use_github=not args.no_github, use_website=not args.no_web,
                                 use_search=not args.no_search, use_smtp=args.smtp, limit=4)
            best = result.best
            alternates = "; ".join(f"{e.address}({e.confidence})" for e in result.emails[1:4])
            writer.writerow([result.name.display, company_name, result.domain,
                             best.address if best else "", best.confidence if best else 0,
                             best.grade if best else "", best.status if best else "",
                             result.pattern, alternates])
            handle.flush()
            mark = GREEN("✓") if best and best.confidence >= 55 else DIM("·")
            print(f"  {mark} [{index}/{len(rows)}] {result.name.display:<26} "
                  f"{(best.address if best else '—'):<38} "
                  f"{best.confidence if best else 0:>3}")
    print(f"\nwrote {out_path}")
    return 0


def cmd_phone(args) -> int:
    """Find published phone numbers for a person."""
    name = parse_name(args.name)
    if not name.first:
        print(RED("could not parse a name"))
        return 1
    with Database() as db:
        domain = ""
        if args.company:
            from . import company as company_mod
            domain = company_mod.resolve(args.company, db=db,
                                         use_search=not args.no_search).domain
        report = phonefind.find_phones(name, domain, db=db, company=args.company,
                                       use_website=not args.no_web,
                                       use_search=not args.no_search,
                                       use_documents=not args.no_docs,
                                       use_directory=not args.no_directory)
    print()
    print(BOLD(name.display) + (DIM(f"  ·  {domain}") if domain else ""))
    print_phones(report, name.display)
    if report.directories:
        print(DIM("\n  staff directories read:"))
        for note in report.directories:
            print(DIM(f"    {note}"))
    if report.documents:
        print(DIM("\n  documents read:"))
        for note in report.documents:
            print(DIM(f"    {note}"))
    for warning in report.warnings:
        print(YELLOW(f"\n  ! {warning}"))
    print(DIM("\n  Phone numbers cannot be inferred from a name - these are only "
              "numbers already published somewhere public."))
    return 0 if report.personal else 1


def cmd_enrich(args) -> int:
    """Find a phone number from (name, company, work email)."""
    with Database() as db:
        try:
            lookup = enrich_mod.enrich(
                args.name, args.company, args.email, db=db,
                only=[p for p in (args.providers or "").split(",") if p],
                exclude=[p for p in (args.without or "").split(",") if p],
                use_search=not args.no_search, region=args.region,
                max_pages=args.pages, search_pages=args.search_pages)
        except ValueError as exc:
            print(RED(str(exc)))
            return 2
    if args.json:
        print(json.dumps(lookup.as_dict(), indent=2))
    else:
        print_enrichment(lookup, show_evidence=not args.quiet)
    return 0 if lookup.status == enrich_mod.FOUND else 1


def cmd_providers(args) -> int:
    """List the registered data providers and whether they are usable."""
    print()
    print(BOLD("  registered phone providers"))
    for row in providers_mod.describe():
        state = GREEN("ready") if row["ready"] else YELLOW("not configured")
        print(f"    {row['name']:<22} {DIM(row['kind']):<26} {state}")
        print(DIM(f"        basis: {row['basis']}"))
        if row["note"] and not row["ready"]:
            print(DIM(f"        {row['note']}"))
    print()
    print(DIM("  Add your own with reachout.providers.register(). A provider must state"))
    print(DIM("  the basis on which its data may be used and a URL for every number."))
    return 0


def cmd_check_phone(args) -> int:
    """Validate and classify a number without looking anything up."""
    parsed = phonelib.normalise(args.number, default_region=args.region)
    print()
    print(BOLD(parsed.pretty or args.number))
    if not parsed.valid:
        print(RED("  not a valid number"))
        for note in parsed.notes:
            print(DIM(f"  · {note}"))
        return 1
    print(f"  E.164        {parsed.e164}")
    print(f"  country      {parsed.country} (+{parsed.calling_code})")
    line = parsed.kind if parsed.kind_certain else f"{parsed.kind} — cannot be determined"
    print(f"  line type    {line}")
    if parsed.area:
        print(f"  area code    {parsed.area}")
    if parsed.extension:
        print(f"  extension    {parsed.extension}")
    for note in parsed.notes:
        print(DIM(f"  · {note}"))
    return 0


def cmd_company(args) -> int:
    """Record company -> mail domain mappings you already know.

    There is no downloadable index of Indian company domains: NIXI does not
    publish the .in zone, and MCA21 registers companies without recording a
    website. So the resolver guesses, and guessing is where it gets Tata
    Consultancy Services wrong. Anything recorded here is treated as knowledge
    rather than inference and is never overridden by a guess.
    """
    with Database() as db:
        if args.file:
            import csv as _csv
            added = skipped = 0
            with open(args.file, newline="", encoding="utf-8") as handle:
                for row in _csv.reader(handle):
                    cells = [c.strip() for c in row if c.strip()]
                    if len(cells) < 2 or cells[0].lower() in ("company", "name"):
                        continue
                    name, domain = cells[0], cells[1].lower().lstrip("@")
                    if not company_mod.looks_like_domain(domain):
                        print(f"  skipped {name}: '{domain}' is not a domain")
                        skipped += 1
                        continue
                    db.upsert_company(name, domain, 0.99, USER_SOURCE)
                    added += 1
            print(f"recorded {added} mapping(s)" + (f", skipped {skipped}" if skipped else ""))
            return 0

        if not args.name:
            rows = db.conn.execute(
                "SELECT name, domain, confidence, source FROM companies "
                "WHERE domain != '' ORDER BY source=? DESC, name", (USER_SOURCE,)
            ).fetchall()
            if not rows:
                print("no company domains recorded yet")
                return 0
            print(f"{len(rows)} company domain(s) - yours first\n")
            for r in rows:
                mark = "you" if r["source"] == USER_SOURCE else "resolved"
                print(f"  {r['name'][:38]:40} {r['domain']:26} {r['confidence']:.0%}  {mark}")
            return 0

        if not args.domain:
            domain, confidence, source = db.company_record(args.name)
            if not domain:
                print(f"nothing recorded for '{args.name}'")
                return 1
            mark = "recorded by you" if source == USER_SOURCE else f"resolved ({source})"
            print(f"{args.name} -> {domain}  {confidence:.0%}  {mark}")
            return 0

        domain = args.domain.lower().lstrip("@")
        if not company_mod.looks_like_domain(domain):
            print(f"'{domain}' does not look like a domain")
            return 1
        db.upsert_company(args.name, domain, 0.99, USER_SOURCE)
        facts = netdns.describe(domain)
        db.save_domain_facts(facts)
        note = "accepts mail" if facts["has_mx"] else "has NO MX records"
        print(f"recorded {args.name} -> {domain} ({note}"
              + (f", {facts['provider']}" if facts.get("provider") else "") + ")")
        return 0


def cmd_teach(args) -> int:
    """Record addresses you already know, or state a company's format outright."""
    with Database() as db:
        # --- state a format directly -----------------------------------------
        if args.pattern:
            if not args.domain:
                print(RED("give a domain: reachout teach acme.com --pattern '{first}.{last}'"))
                return 1
            template = args.pattern.strip()
            probe = patterns.render(template, parse_name("Jane Ann Doe"))
            if not probe or not patterns.is_template(template):
                print(RED(f"'{template}' is not a usable template."))
                print(DIM("  tokens: {first} {last} {middle} {f} {l} {m}, "
                          "e.g. '{first}.{last}' or '{f}{last}'"))
                return 1
            db.pin_pattern(args.domain, template, args.confidence, "stated by you")
            print(f"\n{BOLD(args.domain)} → {GREEN(template)}  "
                  f"{DIM(f'(jane.doe style: {probe}@{args.domain})')}")
            print(DIM(f"  pinned at {args.confidence:.0%}; harvesting will not overwrite it"))
            return 0

        if args.forget:
            if not args.domain:
                print(RED("give a domain to forget"))
                return 1
            removed = db.unpin_pattern(args.domain)
            print(f"removed {removed} pinned pattern(s) for {args.domain}")
            return 0

        # --- bulk import from a contacts export --------------------------------
        if args.file:
            pairs = _read_contacts(args.file)
            if not pairs:
                print(RED(f"no name/email pairs found in {args.file}"))
                print(DIM("  expected columns: name (or full_name) and email"))
                return 1
            summary = engine.bulk_teach(pairs, db)
            print(f"\n{BOLD('imported')} {summary['contacts']} contacts — "
                  f"{summary['banked']} addresses banked, {summary['taught']} taught a format"
                  + (f", {summary['skipped']} consumer addresses skipped"
                     if summary["skipped"] else ""))
            if summary["learned"]:
                print(DIM("\n  companies you can now guess confidently:"))
                for row in summary["learned"][:20]:
                    print(f"    {row['domain']:<30} {patterns.describe(row['template']):<34} "
                          f"{row['confidence']:.0%}")
            return 0

        # --- one known address -------------------------------------------------
        if not (args.domain and args.name and args.email):
            print(RED("usage: reachout teach <domain> \"<Full Name>\" <email>"))
            print(DIM("   or: reachout teach <domain> --pattern '{first}.{last}'"))
            print(DIM("   or: reachout teach --file contacts.csv"))
            return 1

        # The third argument may be an address or a phone number.
        if "@" not in args.email:
            parsed = phonelib.normalise(args.email, default_region=args.region)
            if not parsed.valid:
                print(RED(f"\n  '{args.email}' is neither an email address nor a valid number"))
                for note in parsed.notes:
                    print(DIM(f"  · {note}"))
                return 1
            suspect = phonelib.looks_placeholder(parsed)
            if suspect and not args.force:
                print(YELLOW(f"\n  {parsed.pretty} looks like a placeholder: {suspect}"))
                print(DIM("  Refusing to record it. Re-run with --force if it is genuinely real."))
                return 1
            db.add_domain_phone(args.domain, parsed.e164, parsed.kind, "person",
                                args.name, f"{args.name} mobile", "you told me")
            print(f"\n{BOLD(parsed.pretty)} recorded as {args.name}'s number at {args.domain}")
            print(DIM(f"  {parsed.country} {parsed.kind}"
                      + ("" if parsed.kind_certain else " (line type not determinable)")))
            return 0

        outcome = engine.teach(args.domain, args.name, args.email, db)
        engine.refresh_pattern(args.domain, db)
        rows = db.get_patterns(args.domain)

    if outcome["error"]:
        print(YELLOW(f"\n  {outcome['error']}"))
        if not outcome["template"]:
            return 1
    print(f"\n{BOLD(outcome['address'])} recorded as a real address at {args.domain}")
    if outcome["template"]:
        print(DIM(f"  it follows {patterns.describe(outcome['template'])}"))
    if rows:
        print()
        for row in rows[:4]:
            print(f"  {patterns.describe(row['template']):<40} "
                  f"{bar(int(row['confidence']*100))} {row['confidence']:.0%} "
                  + DIM(f"({row['samples']} samples)"))
    return 0


def _read_contacts(path: str) -> list:
    """Pull (name, email) out of a CSV file."""
    with Path(path).open(encoding="utf-8", errors="replace") as handle:
        return contact_pairs(list(csv.DictReader(handle)))


def contact_pairs(rows: list) -> list:
    """Pull (name, email) out of parsed CSV rows. Tolerates Google/Outlook headers."""
    if not rows:
        return []
    headers = list(rows[0])
    def pick(*wanted):
        for want in wanted:
            for header in headers:
                if header and header.strip().lower() == want:
                    return header
        for want in wanted:
            for header in headers:
                if header and want in header.strip().lower():
                    return header
        return None

    name_key = pick("name", "full_name", "fullname", "display name", "display_name")
    email_key = pick("email", "e-mail", "email address", "email 1 - value", "primary email")
    first_key = pick("first name", "given name", "first")
    last_key = pick("last name", "surname", "family name", "last")
    # A Google export has "First Name"/"Last Name" and no full-name column; the
    # loose substring match above would otherwise latch onto "First Name".
    if name_key and name_key in (first_key, last_key):
        name_key = None

    out = []
    for row in rows:
        address = (row.get(email_key) or "").strip().lower() if email_key else ""
        if "@" not in address:
            continue
        display = (row.get(name_key) or "").strip() if name_key else ""
        if not display and first_key and last_key:
            display = f"{(row.get(first_key) or '').strip()} {(row.get(last_key) or '').strip()}".strip()
        if display:
            out.append((display, address))
    return out


def cmd_eval(args) -> int:
    """Leave-one-out accuracy on the samples already in your database."""
    from .evaluate import evaluate_all, evaluate_domain
    with Database() as db:
        reports = [evaluate_domain(args.domain, db, args.min)] if args.domain \
            else evaluate_all(db, args.min)
        reports = [r for r in reports if r]
    if not reports:
        print(f"not enough samples yet (need >= {args.min} per domain). "
              f"Run: reachout learn <domain>")
        return 1

    print(f"\n{BOLD('leave-one-out accuracy')}  {DIM('- would the top guess have been right?')}\n")
    print(f"  {'domain':<26}{'n':>5}{'top-1':>9}{'top-3':>9}")
    total_trials = total_top1 = total_top3 = 0
    for report in sorted(reports, key=lambda r: -r.trials):
        total_trials += report.trials
        total_top1 += report.top1
        total_top3 += report.top3
        colour = GREEN if report.top1_rate >= 0.7 else (YELLOW if report.top1_rate >= 0.4 else RED)
        print(f"  {report.domain:<26}{report.trials:>5}"
              f"{colour(f'{report.top1_rate:>8.0%}')}{report.top3_rate:>9.0%}")
    if total_trials:
        print(DIM(f"\n  overall  {total_trials} trials  "
                  f"top-1 {total_top1/total_trials:.0%}  top-3 {total_top3/total_trials:.0%}"))
    if args.misses:
        print(DIM("\n  misses:"))
        for report in reports:
            for person, actual, guessed in report.missed[:8]:
                print(DIM(f"    {person:<26} actual {actual:<22} guessed {guessed}"))
    return 0


def cmd_forget(args) -> int:
    """Remove records. The erasure half of keeping data about real people."""
    targets = {"person": args.person, "domain": args.domain,
               "email": args.email, "phone": args.phone}
    if not any(targets.values()):
        print(RED("say what to forget: --person, --domain, --email or --phone"))
        return 1
    if args.phone:
        parsed = phonelib.normalise(args.phone, default_region=args.region)
        if parsed.valid:
            targets["phone"] = parsed.e164

    with Database() as db:
        preview = db.preview_forget(**targets)
        if not preview:
            print(DIM("nothing matches — nothing to remove"))
            return 1
        print(f"\n{BOLD('would remove:')}")
        total = 0
        for table, rows in preview.items():
            total += len(rows)
            print(f"  {table}  ({len(rows)})")
            for row in rows[:10]:
                print(DIM(f"    {', '.join(str(v) for v in row.values())}"))
            if len(rows) > 10:
                print(DIM(f"    … and {len(rows) - 10} more"))
        if not args.yes:
            print(YELLOW(f"\n  {total} record group(s). Re-run with --yes to delete."))
            return 0
        removed = db.forget(**targets)
    print(GREEN(f"\n  removed {removed} rows permanently"))
    return 0


def cmd_stats(args) -> int:
    with Database() as db:
        stats = db.stats()
        top = db.conn.execute(
            "SELECT domain, COUNT(*) n FROM pattern_samples GROUP BY domain ORDER BY n DESC LIMIT 10"
        ).fetchall()
    print(f"\n{BOLD('your database')}  {DIM(stats['db_path'])}")
    for label, key in [("people", "people"), ("companies", "companies"),
                       ("domains with mail", "domains_resolved"),
                       ("email candidates", "emails"),
                       ("addresses seen in public", "emails_observed"),
                       ("pattern samples", "pattern_samples"),
                       ("domains with a known pattern", "pattern_domains"),
                       ("addresses banked by domain", "domain_emails"),
                       ("phone numbers", "phones"),
                       ("numbers banked by domain", "domain_phones"),
                       ("evidence records", "observations"),
                       ("searches run", "searches")]:
        print(f"  {label:<32} {BOLD(str(stats[key]))}")
    print(f"  {'size on disk':<32} {stats['db_size_kb']} KB")
    if top:
        print(DIM("\n  best-known domains:"))
        for row in top:
            print(DIM(f"    {row['domain']:<28} {row['n']} samples"))
    return 0


def cmd_export(args) -> int:
    with Database() as db:
        rows = db.conn.execute("""
            SELECT p.name_raw, p.first, p.last, c.name AS company, e.address, e.confidence,
                   e.grade, e.status, e.pattern, p.linkedin, p.github
            FROM emails e JOIN people p ON p.id = e.person_id
            LEFT JOIN companies c ON c.id = p.company_id
            WHERE e.confidence >= ?
            ORDER BY e.confidence DESC
        """, (args.min,)).fetchall()
    if args.format == "json":
        print(json.dumps([dict(r) for r in rows], indent=2))
        return 0
    writer = csv.writer(sys.stdout)
    writer.writerow(["name", "first", "last", "company", "email", "confidence", "grade",
                     "status", "pattern", "linkedin", "github"])
    for row in rows:
        writer.writerow([row[k] for k in row.keys()])
    return 0


def cmd_serve(args) -> int:
    from .web import serve
    serve(host=args.host, port=args.port)
    return 0


def cmd_doctor(args) -> int:
    print(f"\n{BOLD('ReachOut environment')}")
    print(f"  database        {config.DB_PATH}")
    print(f"  cache           {config.CACHE_DIR}")
    print(f"  robots.txt      {'respected' if config.RESPECT_ROBOTS else 'IGNORED'}")
    print(f"  per-host delay  {config.PER_HOST_DELAY}s")

    if config.KEY_FILE:
        print(f"  keys file       {config.KEY_FILE}")

    from .sources import search as websearch
    print(f"\n  {BOLD('search backends')}  (tried in order; three providers need one)")
    any_working = False
    for backend in websearch.chain():
        if not backend.configured():
            print(f"    {DIM('–')} {backend.name:<12} {DIM('not configured')} — "
                  + DIM(f"set {backend.setup}"))
            continue
        working, detail = websearch.probe_backend(backend)
        any_working = any_working or working
        mark = GREEN("✓") if working else RED("✗")
        print(f"    {mark} {backend.name:<12} {(GREEN if working else RED)(detail[:88])}")
    if not any_working:
        print(RED("    no working search backend — staff-directory, public-documents"))
        print(RED("    and public-pages cannot find anything without one"))
    print()
    try:
        limits = github.rate_limit()
        state = "authenticated" if limits["authenticated"] else "anonymous"
        colour = GREEN if limits["authenticated"] else YELLOW
        print(f"  github          {colour(state)} — {limits['remaining']}/{limits['limit']} left this hour")
        if not limits["authenticated"]:
            print(DIM("                  set GITHUB_TOKEN for 5000/hour instead of 60"))
    except Exception as exc:
        print(f"  github          {RED(str(exc)[:60])}")

    ok = netdns.mx("gmail.com")
    print(f"  dns             {GREEN('working') if ok else RED('failing')}")
    print(f"  smtp probing    {'enabled' if config.SMTP_ENABLED else 'disabled (default)'}")
    return 0


# ---------------------------------------------------------------------- parser
def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="reachout",
        description="Find and rank likely work email addresses from public sources.")
    sub = parser.add_subparsers(dest="command", required=True)

    def add_source_flags(sp):
        sp.add_argument("--no-github", action="store_true", help="skip the GitHub API")
        sp.add_argument("--no-web", action="store_true", help="skip company website crawling")
        sp.add_argument("--no-search", action="store_true", help="skip web search")

    find = sub.add_parser("find", help="find a person's likely address")
    find.add_argument("name", help="full name, or a LinkedIn profile URL")
    find.add_argument("company", nargs="?", default="", help="company name or domain")
    find.add_argument("--company", dest="company_flag", default="", help=argparse.SUPPRESS)
    find.add_argument("--limit", type=int, default=6)
    find.add_argument("--phone", action="store_true", help="also look for phone numbers")
    find.add_argument("--smtp", action="store_true", help="attempt an SMTP RCPT check")
    find.add_argument("--relearn", action="store_true", help="re-harvest the domain pattern")
    find.add_argument("--json", action="store_true")
    find.add_argument("-q", "--quiet", action="store_true", help="hide the evidence lines")
    add_source_flags(find)
    find.set_defaults(func=cmd_find)

    learn = sub.add_parser("learn", help="harvest a domain and infer its naming pattern")
    learn.add_argument("domain")
    learn.add_argument("--org", default="", help="GitHub org if it differs from the domain")
    learn.add_argument("--repos", type=int, default=5)
    add_source_flags(learn)
    learn.set_defaults(func=cmd_learn)

    pattern = sub.add_parser("pattern", help="show what you know about a domain")
    pattern.add_argument("domain")
    pattern.add_argument("--samples", action="store_true")
    pattern.set_defaults(func=cmd_pattern)

    guess = sub.add_parser("guess", help="apply a known pattern offline, no network")
    guess.add_argument("name")
    guess.add_argument("domain")
    guess.add_argument("--limit", type=int, default=6)
    guess.set_defaults(func=cmd_guess)

    ver = sub.add_parser("verify", help="run checks on one address")
    ver.add_argument("address")
    ver.add_argument("--smtp", action="store_true")
    ver.set_defaults(func=cmd_verify)

    gh = sub.add_parser("github", help="query GitHub directly")
    gh.add_argument("user", nargs="?", default="")
    gh.add_argument("--org", default="", help="harvest an org's public commits instead")
    gh.add_argument("--domain", default="", help="only keep addresses at this domain")
    gh.add_argument("--repos", type=int, default=5)
    gh.add_argument("--limit", type=int, default=30)
    gh.add_argument("--save", action="store_true", help="store results as pattern samples")
    gh.add_argument("-v", "--verbose", action="store_true")
    gh.set_defaults(func=cmd_github)

    batch = sub.add_parser("batch", help="enrich a CSV of names")
    batch.add_argument("csv")
    batch.add_argument("-o", "--out", default="enriched.csv")
    batch.add_argument("--smtp", action="store_true")
    add_source_flags(batch)
    batch.set_defaults(func=cmd_batch)

    comp = sub.add_parser("company", help="record company -> domain mappings you know")
    comp.add_argument("name", nargs="?", default="", help="company name; omit to list all")
    comp.add_argument("domain", nargs="?", default="", help="its mail domain; omit to look up")
    comp.add_argument("--file", default="", help="bulk import a CSV of company,domain")
    comp.set_defaults(func=cmd_company)

    teach = sub.add_parser("teach", help="record addresses you already know to be real")
    teach.add_argument("domain", nargs="?", default="")
    teach.add_argument("name", nargs="?", default="")
    teach.add_argument("email", nargs="?", default="")
    teach.add_argument("--pattern", default="", help="state the format, e.g. '{first}.{last}'")
    teach.add_argument("--confidence", type=float, default=0.9)
    teach.add_argument("--file", default="", help="bulk import a contacts CSV (name,email)")
    teach.add_argument("--force", action="store_true",
                       help="record a number even if it looks like a placeholder")
    teach.add_argument("--region", default="IN", help="default country for bare numbers")
    teach.add_argument("--forget", action="store_true", help="drop pinned patterns for a domain")
    teach.set_defaults(func=cmd_teach)

    phone = sub.add_parser("phone", help="find published phone numbers for a person")
    phone.add_argument("name")
    phone.add_argument("company", nargs="?", default="")
    phone.add_argument("--no-web", action="store_true")
    phone.add_argument("--no-search", action="store_true")
    phone.add_argument("--no-docs", action="store_true", help="skip public PDFs and CVs")
    phone.add_argument("--no-directory", action="store_true",
                       help="skip staff/faculty directory pages")
    phone.set_defaults(func=cmd_phone)

    enr = sub.add_parser("enrich", help="find a phone number from name + company + work email")
    enr.add_argument("name")
    enr.add_argument("company", nargs="?", default="")
    enr.add_argument("email", nargs="?", default="", help="the person's work email address")
    enr.add_argument("--region", default="", help="country for bare national numbers, e.g. GB")
    enr.add_argument("--providers", default="", help="comma-separated allow-list")
    enr.add_argument("--without", default="", help="comma-separated deny-list")
    enr.add_argument("--pages", type=int, default=6, help="pages to read on the company site")
    enr.add_argument("--search-pages", dest="search_pages", type=int, default=3,
                     help="search results to fetch and parse")
    enr.add_argument("--no-search", action="store_true")
    enr.add_argument("--json", action="store_true")
    enr.add_argument("-q", "--quiet", action="store_true", help="hide the evidence lines")
    enr.set_defaults(func=cmd_enrich)

    prov = sub.add_parser("providers", help="list the registered phone data providers")
    prov.set_defaults(func=cmd_providers)

    chk = sub.add_parser("checkphone", help="validate and classify a number, offline")
    chk.add_argument("number")
    chk.add_argument("--region", default="IN", help="assumed country for bare numbers")
    chk.set_defaults(func=cmd_check_phone)

    ev = sub.add_parser("eval", help="measure your own hit rate (leave-one-out, offline)")
    ev.add_argument("domain", nargs="?", default="")
    ev.add_argument("--min", type=int, default=3, help="minimum samples per domain")
    ev.add_argument("--misses", action="store_true", help="show what it got wrong")
    ev.set_defaults(func=cmd_eval)

    forget = sub.add_parser("forget", help="permanently remove records from your database")
    forget.add_argument("--person", default="", help="full name")
    forget.add_argument("--domain", default="", help="everything known about a domain")
    forget.add_argument("--email", default="")
    forget.add_argument("--phone", default="")
    forget.add_argument("--region", default="IN")
    forget.add_argument("--yes", action="store_true", help="actually delete (default: preview)")
    forget.set_defaults(func=cmd_forget)

    stats = sub.add_parser("stats", help="what your database has accumulated")
    stats.set_defaults(func=cmd_stats)

    export = sub.add_parser("export", help="dump everything you have found")
    export.add_argument("--format", choices=["csv", "json"], default="csv")
    export.add_argument("--min", type=int, default=0, help="minimum confidence")
    export.set_defaults(func=cmd_export)

    serve = sub.add_parser("serve", help="run the local web UI")
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=8765)
    serve.set_defaults(func=cmd_serve)

    doctor = sub.add_parser("doctor", help="check the environment and API limits")
    doctor.set_defaults(func=cmd_doctor)

    return parser


def main(argv: list[str] | None = None) -> int:
    config.ensure_dirs()
    parser = build_parser()
    args = parser.parse_args(argv)
    if getattr(args, "company_flag", ""):
        args.company = args.company_flag
    try:
        return args.func(args)
    except KeyboardInterrupt:
        print("\ninterrupted")
        return 130
