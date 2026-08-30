# Boundaries

## What this tool is for

Finding a professional contact you have a genuine reason to reach: a recruiter
you want to follow up with, an author whose paper you have questions about, an
alum in your field, a maintainer whose project you contribute to.

## What it deliberately makes hard

- **No sending.** There is no mail capability anywhere in the codebase.
- **One request per host at a time**, with a delay between them. Bulk scraping
  would take longer than it is worth.
- **robots.txt honoured** by default.
- **No LinkedIn scraping.** A LinkedIn URL is read for the name in its slug and
  nothing else. Scraping profiles violates their terms and has been litigated.
- **SMTP probing off** by default.

## Phone numbers deserve more care than email

A work email address is a professional contact point that companies publish on
purpose. A personal mobile number usually is not. Treat them differently.

ReachOut will not generate or enumerate phone numbers — the only way to "find"
an unpublished number is to try ranges, which is the mechanism behind nuisance
calling and is useless anyway. It reports only numbers already published, and
separates numbers that reach a person from numbers that reach a company.

Worth noting the precedent from IIT Bombay's own alumni portal: education and
employment history are visible to other alumni, while **email and mobile are
private by default** and shown only with the person's explicit permission. An
institution with a first-party relationship to its own members still treats a
mobile number as something requiring consent. A tool with no relationship at all
should be at least as careful.

In India, phone numbers are also covered by TRAI's unsolicited-communications
regulations and the DND registry. Calling or messaging someone commercially
because you found their number is a separate act from finding it, and is
regulated separately.

## Authorized providers

`reachout.providers` lets a licensed data source be registered alongside the
public ones. Every provider must state a `basis` — why its data may be used —
and give a URL for every number it returns. That field is what makes the
exclusion of breach dumps and scraped broker datasets enforceable rather than
aspirational: repackaged dump data has no answer to it. If a vendor cannot tell
you where a number came from and on what basis they hold it, a contract does not
make the data usable; it only moves the liability to you.

Licensed data is tagged `authorized-api` throughout, so it is never presented as
something seen on a public page — the two carry different obligations, including
whether you may keep it after the contract ends.

## Legal reality, briefly

Public availability is not the same as permission to process.

**GDPR (EU/UK)** covers personal data about identifiable people regardless of
whether you found it publicly. A genuine one-to-one professional approach can
rest on legitimate interest; building a marketing list generally cannot. People
retain the right to access and erasure — `reachout export` and
`reachout forget --person "…"` are how you honour that.

**India's DPDP Act 2023** is stricter on consent than GDPR and has narrower
legitimate-interest grounds. Publicly available personal data is partly carved
out, but the carve-out is not a blanket exemption for building a contact
database.

**CAN-SPAM (US)** governs the message rather than the collection: commercial
mail needs a real postal address and a working opt-out.

**Purchased data cannot be redistributed** under most provider licences. Not
relevant here — nothing is purchased — but relevant the moment you consider
buying to fill gaps.

## A workable line

Reasonable: looking up one person you have a real reason to contact; learning
that a company uses `first.last`; keeping a private record of contacts you
actually deal with; deleting someone's record when they ask.

Not reasonable: enumerating every employee at a company; building a list to sell
or share; bulk cold outreach; probing thousands of addresses over SMTP; ignoring
someone who has asked you to stop.

The technical difference between the two is small. The legal and ethical
difference is not.
