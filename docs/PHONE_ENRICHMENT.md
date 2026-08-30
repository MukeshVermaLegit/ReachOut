# Phone enrichment: architecture

**Input:** full name, company name, work email.
**Output:** a status (`found` / `needs_review` / `not_found`), and for anything
reported, the number, a confidence score, the signals behind that score, and the
public URL where it was seen.

The hard problem is not discovery. It is **attribution**. A company contact page
is full of valid phone numbers and almost none of them reach the person you
asked about; a staff directory hands you a direct dial for two hundred people
and you want one of them. Presenting a switchboard, or a colleague's desk phone,
as somebody's number is the failure this design exists to prevent.

Two things are never done, anywhere in the pipeline:

- **Nothing is generated.** There is no number synthesis, no range enumeration,
  no "the pattern suggests". Enumeration is the mechanism behind nuisance
  calling and would produce nothing true anyway. Every candidate is a sighting
  of something already published, and one that cannot name a URL is dropped
  before ranking (`enrich._traceable`).
- **No breach data, broker datasets, credential dumps, or password-reset and
  account-recovery probing.** These are excluded by design, not by configuration:
  they produce data the subject never published, which is the opposite of what
  every stage here is built to consume. The provider interface cannot be used to
  smuggle one in — see *Adding a provider*.

---

## Pipeline

```
  name + company + work email
             │
             ▼
   ┌──────────────────┐   identity.build()
   │  Subject         │   • domain comes from the email, not a search
   │  resolution      │   • is the local part something this name produces?
   └──────────────────┘   • role mailbox? distinctive enough to match on?
             │
             ▼
   ┌──────────────────┐   providers.select() → Provider.run()
   │  Providers       │   banked · company-website · staff-directory
   │  (fan out)       │   public-documents · personal-profiles · public-pages
   └──────────────────┘   · any authorized API you plug in
             │            each returns FoundPhone(raw, context, owner, scope,
             │            how, source_url, emails)
             ▼
   ┌──────────────────┐   phones.normalise()  → E.164
   │  Normalise       │   country from the domain unless the caller states it
   │  + group         │   '020 7946 0812' ≡ '+44 20 7946 0812' ≡ tel: link
   └──────────────────┘
             │
             ▼
   ┌──────────────────┐   phonefind.attribute() per sighting, best wins
   │  Attribute       │   PERSON | COMPANY | UNKNOWN, with every point traceable
   │  + score         │   to a named signal and a URL
   └──────────────────┘
             │
             ▼
   ┌──────────────────┐   enrich.decide()
   │  Decide          │   found ≥ 60 · needs_review ≥ 30 · not_found otherwise
   └──────────────────┘
             │
             ▼
   ┌──────────────────┐   phone_lookups + phone_lookup_evidence
   │  Persist         │   the decision and every URL behind it
   └──────────────────┘
```

Layer by layer:

| Module | Responsibility |
|---|---|
| `reachout/identity.py` | Build the `Subject`; decide whether a block of text refers to them |
| `reachout/providers.py` | Every source behind one interface, plus the registry |
| `reachout/sources/*` | Fetching and extraction: HTML, structured data, directory rows, PDFs, search |
| `reachout/phones.py` | Parsing, validation, line-type classification, placeholder detection |
| `reachout/phonefind.py` | Attribution, scoring, dedupe, corroboration (`rank`) |
| `reachout/enrich.py` | Orchestration, the decision, persistence |
| `reachout/db.py` | Storage and the audit trail |

---

## Why the work email is the useful input

A name is a weak identifier. "Amara Okafor" matches every Amara Okafor alive, so
"a number printed near that name" is satisfied by all of them.

A work email is an identifier the **employer issued to exactly one person**. That
changes what the evidence can prove:

1. **It fixes the domain with no search.** No company-resolution step, no risk of
   crawling the wrong company. (Without an email, `enrich` falls back to
   `company.resolve()` and records the guess as a warning, because everything
   downstream is then about that guess.)
2. **A block printing the address next to a number is the page stating the
   association**, not the pipeline inferring it. This is the strongest
   non-user-confirmed evidence available (`email_bound`, +76).
3. **A block printing a *colleague's* address is the clearest possible
   refutation.** On a staff listing, that is the page saying whose entry this is,
   and it is not the subject's.
4. **It validates itself.** `patterns.matching_templates` checks whether the
   local part is something this person's name produces. If not, that is recorded
   as a problem — the address may belong to someone else.

---

## Entity resolution

`Subject.mentions(*chunks)` returns a `Mention` — a record of findings rather
than a bare number, because a refusal has to be explainable too.

| Field | Meaning |
|---|---|
| `email_exact` | The subject's own work address is printed in this block |
| `email_local` | The distinctive part of it appears — or, when no address was given, one at the employer's domain that this person's name would produce |
| `name` | `full` \| `last` \| `''` |
| `company` | The employer's name or brand appears |
| `domain` | Some address at the employer's domain appears |
| `rival_email` | A *different* personal mailbox at that employer |

Three rules do most of the work:

**A `first.last` local part is not evidence beyond the name.** `amara.okafor`
slugs to exactly the name, so matching on it would count the same finding twice
under a second label — and would look like evidence from the address when the
address was never on the page. `Subject._name_keys()` blocks that.

**Role mailboxes rule nothing in or out.** `info@` belongs to nobody, so it is
never a rival and never an identifier.

**A block holding two people's addresses identifies neither.** It is the listing
running on, not one person's entry — see below.

### The neighbour trap

A staff directory prints entries one after another:

```
Amara Okafor   amara.okafor@northwind.example   020 7946 0812
Priya Raman    priya.raman@northwind.example    020 7946 0813
```

Read as flat text, a ±70-character window around `…0813` reaches backwards into
Amara's entry and finds her name *and her address*. Proximity says the number is
hers. It is Priya's.

Two independent defences:

1. **Structural binding** (`sources/directory.py`): a number binds to a person
   only when the *smallest enclosing block of markup* containing both holds
   exactly one candidate name. Two names, or none, and it stays unattributed.
2. **The two-address rule** (`phonefind.attribute`): if a block contains both the
   subject's address and a colleague's, nothing is attributed — and this is
   checked *before* the email evidence is scored, because otherwise the strongest
   signal in the system would outrank every check below it.

### The namesake trap

A different Amara Okafor publishes her studio number on her own site. Her name
is right there next to it, so `name_adjacent` (+50) fires and, with validity and
line-type bonuses, the total clears the reporting bar.

Guard: if the only attributing evidence is name proximity, and the block shows
*no* connection to the employer — no company name, no domain, no address — and
the page is not on the employer's own domain, apply `no_employer_link` (−32).
That leaves it visible as a candidate but below the threshold to be reported as
an answer. A shared name is not an identifier.

---

## Scoring

Every point traces to a named signal carrying its own source URL, so a score can
always explain itself. `scoring.Score` holds the signals and any caps that bound
the total.

**Attribution** — the strongest applicable branch wins, and only one fires:

| Signal | Points | Meaning |
|---|---|---|
| `confirmed` | +80 | You supplied this number yourself |
| `email_bound` | +76 | Same *structural* entry as the work address you gave |
| `jsonld_person` | +70 | Structured data names it as this person's number |
| `directory_row` | +62 | Bound by the page's markup to this person's entry |
| `email_adjacent` | +58 | Printed alongside the work address, proximity only |
| `own_site` | +55 | On the personal site linked from a matching profile |
| `owner_named` | +50 | The page names it as this person's number |
| `name_adjacent` | +50 | Printed next to the full name |
| `surname_adjacent` | +20 | Printed near the surname only |

**Corroboration and provenance:**

| Signal | Points |
|---|---|
| `authorized_provider` | +20 |
| `email_local` | +16 |
| `labelled_personal` (mobile/direct/cell) | +14 |
| `document` | +12 |
| `corroborated` (≥2 independent hosts) | +10 |
| `valid` | +10 |
| `mobile_series` | +8 |
| `directory` | +8 |
| `tel_link` | +6 |

**Counter-evidence:**

| Signal | Points |
|---|---|
| `placeholder` (looks fabricated) | −75 |
| `service_number` (toll-free/premium) | −70 |
| `org_scope` (structured data says organisation) | −60 |
| `switchboard` (reception/support/helpline wording) | −45 |
| `no_employer_link` | −32 |
| `rival_email` | −25 |
| `landline` | −6 |

**Caps** — recorded only when they actually bind, so a cap line always means
something:

- anything not attributed to the person is capped at **20** ("nothing ties this
  number to the person, only to the company");
- an attributed number is capped at **96** — no call has been placed;
- an invalid number is zeroed.

Corroboration counts **hosts, not pages**. Five pages on one company site are one
source; a second host is a second opinion.

### The decision

```python
found         best.confidence >= 60   # enrich.ACCEPT
needs_review  best.confidence >= 30   # enrich.REVIEW_FLOOR
not_found     otherwise, or nothing attributed at all
```

`scoring.GRADES` puts MEDIUM at 55 and LOW at 30, so the reporting bar sits a
little above merely-MEDIUM, and the review floor is exactly the LOW boundary.
Below the bar, the evidence is an *association* rather than an attribution.

One extra rule: a candidate whose only evidence is a **single search snippet** is
capped at `needs_review` however high it scores. A snippet is a fragment with no
markup — nothing in it says where one person's entry ends and the next begins,
which is exactly what attribution rests on. `PublicPages` therefore fetches
promising results and re-extracts them properly; the snippet survives only as a
fallback when the page will not open.

`not_found` is the expected outcome for most people. Personal numbers are mostly
not published. A tool that finds one every time is guessing, and a guessed phone
number is worse than silence because somebody eventually dials it.

---

## Coverage: when a source cannot look

`not_found` is only a finding about the person if the sources actually ran. A
lookup where half of them were blocked has established nothing, and reporting
that as "no number is published" is the worst failure this system can have —
it is a confident negative built on no evidence.

So every provider run has a state, and only one of them means it did its job:

| State | Meaning |
|---|---|
| `ok` | It looked, and found what it found |
| `degraded` | It ran, but something it depends on did not |
| `error` | It raised |
| `skipped` | Not applicable, or not configured |

Two degradations are common enough to name:

**The search backend is blocked.** Three providers (`staff-directory`,
`public-documents`, `public-pages`) cannot find anything without search, so a
lookup that makes five or six queries can lose most of its discovery surface to
one rate limit.

`search.search()` therefore raises `SearchUnavailable` rather than returning
`[]`. A genuine zero-result page says so in words and still ships the result
markup; a challenge page ships neither, and the two must not collapse into the
same empty list.

### The backend chain

A query walks `search.CHAIN` until something answers:

| Backend | Free tier | Fails by |
|---|---|---|
| `brave` | 2,000/month, 1 query/sec | `429` — a 2s pause |
| `google` | 100/day | `429`/`403 quota` — a 6h stand-down |
| `duckduckgo` | none needed | `202` + challenge shell — a 2min stand-down |

Two keyed backends are worth configuring because they fail *differently*: Brave
is capped per second, Google per day, so one is usually up when the other is
not. `BackendFailed` carries a `cool_off` (how long to stop asking) and a
`permanent` flag — a rejected credential is disabled for the whole run, because
a wrong key will still be wrong on the next query and retrying it just spends
wall-clock.

A backend that answers is authoritative for that query. Zero results from a
working engine is a real zero, and asking the next one would spend quota to make
"nothing published" *less* clear, not more.

### Configuring it

Put the keys in `data/keys.env` (gitignored; see `keys.env.example`) or
export them. A real environment variable always wins over the file.

```
BRAVE_API_KEY=…            # api-dashboard.search.brave.com
GOOGLE_CSE_KEY=…           # Cloud console → enable "Custom Search API" → API key
GOOGLE_CSE_CX=…            # the cx= value from the engine's snippet
```

### Restricted engines

Google no longer offers "Search the entire web" on every new Programmable Search
Engine, so most are restricted to a list of sites you supply. That is usable,
but it changes what an empty result *means*: "not on those thirty sites" is not
"not on the web".

So a backend carries `authoritative`. A whole-web engine answering zero ends the
chain — that is a real zero. A **restricted** engine answering zero does not; the
chain continues to the next backend, and only when every engine has answered
with nothing does `search()` return `[]`.

`GOOGLE_CSE_WHOLE_WEB` defaults to **off**, i.e. Google CSE is assumed
restricted. The asymmetry is deliberate: assuming restricted costs one extra
fallback query when the engine was actually whole-web, while assuming whole-web
silently narrows every search in the tool to a site list. `reachout doctor`
reports a restricted engine as usable-but-supplementary rather than broken, and
flags the opposite mistake — `GOOGLE_CSE_WHOLE_WEB=1` on an engine that finds
nothing for a query the whole web answers.

### Choosing sites for a restricted engine

For phone discovery specifically, sites differ enormously in yield:

| Yield | Sites | Why |
|---|---|---|
| High | `prnewswire.com`, `businesswire.com`, `globenewswire.com` | Press releases end in a named "Media Contact" block with a direct number, published deliberately |
| High | `muckrack.com`, `speakerhub.com`, `f6s.com` | Profile pages built around being contactable |
| High | `springer.com`, `elsevier.com`, `nature.com`, `sciencedirect.com`, `plos.org` | Corresponding-author blocks carry institutional phone numbers |
| Medium | `github.com`, `gitlab.com`, `gravatar.com`, `speakerdeck.com`, `slideshare.net`, `ted.com` | Profiles that sometimes link to a personal site |
| Low | `stackoverflow.com`, `npmjs.com`, `kaggle.com`, `dribbble.com`, `behance.net`, `indiehackers.com` | Rarely carry contact details at all |
| Never fetched | `linkedin.com` and other `NO_FETCH_HOSTS` | Terms forbid automated access; the search snippet is used as a lead, the page is not fetched |

`NO_FETCH_HOSTS` in `providers.py` is an explicit list, not a reliance on
robots.txt. robots.txt already blocks most of them, but a promise the code makes
explicitly is one a later edit cannot quietly drop — and "we chose not to" reads
better in an evidence trail than "the fetch failed".

**The company site is a JavaScript app.** If the served HTML is a shell, there is
nothing to extract and `company-website` reports `degraded`, not zero. On a
single-page-app company site that is usually the source that would have had the
answer.

When the status is `not_found` and anything was degraded, the reason says so
explicitly instead of the usual "most people do not publish theirs". The result
object exposes `lookup.coverage` (`"5/6 sources"`) and `lookup.blind_spots`, and
both appear in the JSON.

---

## Data minimisation

A directory hands you everyone's number. Keeping the rest would mean banking a
few hundred strangers' details to answer a question about one person, so:

- a number **structurally bound to a different named person** is dropped at the
  ranking step and never reaches the report or the database (`rank`);
- only numbers attributed to the *organisation* are shown as company numbers;
  unattributed leftovers are discarded rather than listed;
- `phone_lookups` records who was searched for and what was concluded, which is
  personal data in its own right — `forget --person` reaches it, or the erasure
  promise is not kept.

---

## Database schema

New tables (existing ones are unchanged; `SCHEMA_VERSION` is now 2 and every
statement is `CREATE TABLE IF NOT EXISTS`, so an existing database upgrades in
place with no migration).

```sql
-- One enrichment run: what was asked, what was decided, and on what.
CREATE TABLE phone_lookups (
    id           INTEGER PRIMARY KEY,
    subject_key  TEXT NOT NULL,        -- first.last@domain
    name_raw     TEXT, work_email TEXT, company TEXT, domain TEXT,
    status       TEXT,                 -- found | needs_review | not_found
    reason       TEXT,                 -- the sentence shown to the user
    best_e164    TEXT, best_score INTEGER, best_source TEXT,
    candidates   INTEGER,
    providers    TEXT,                 -- 'company-website:ok,public-pages:error'
    elapsed      REAL, ran_at REAL NOT NULL
);

-- Every sighting behind a lookup, one row per (number, URL).
CREATE TABLE phone_lookup_evidence (
    id          INTEGER PRIMARY KEY,
    lookup_id   INTEGER REFERENCES phone_lookups(id) ON DELETE CASCADE,
    e164        TEXT NOT NULL,
    provider    TEXT,                  -- which provider saw it
    how         TEXT,                  -- json-ld | directory | tel-link | text | …
    basis       TEXT,                  -- why that provider's data is usable
    source_url  TEXT, context TEXT, owner TEXT, seen_at REAL NOT NULL
);

-- Licensed-provider responses. Those calls are metered and usually billed.
CREATE TABLE provider_cache (
    provider    TEXT NOT NULL,
    subject_key TEXT NOT NULL,
    payload     TEXT NOT NULL,
    fetched_at  REAL NOT NULL,
    PRIMARY KEY (provider, subject_key)
);
```

Reused from the existing schema: `domain_phones` (numbers seen at a domain,
which is what makes the second person at a company nearly free), `phones`
(numbers attributed to a person), `people`, `companies`.

### Caching, at three levels

| Layer | Where | TTL |
|---|---|---|
| HTTP responses | `data/cache/` on disk, keyed by URL | `REACHOUT_CACHE_TTL`, 7 days |
| Numbers seen at a domain | `domain_phones` | indefinite, reused by the `banked` provider |
| Licensed API responses | `provider_cache` | `REACHOUT_PROVIDER_CACHE_TTL`, 30 days |

The licensed cache is written **before** parsing, so a parser bug does not cost
the same billed query twice.

---

## Adding a provider

```python
from reachout import providers
from reachout.sources.website import FoundPhone

class OurCRM(providers.Provider):
    name = "our-crm"
    kind = providers.LOCAL
    basis = "contacts our own team entered, with consent recorded at capture"

    def unavailable(self, ctx):
        return "" if ctx.subject.work_email else "needs a work email"

    def fetch(self, ctx):
        row = crm.lookup(ctx.subject.work_email)
        if not row:
            return self.result(notes=["no CRM record"])
        return self.result(candidates=[FoundPhone(
            raw=row.phone, owner=row.full_name, scope="person",
            how="authorized-api", source_url=row.record_url,
            emails=[ctx.subject.work_email])])

providers.register(OurCRM())
```

Two fields are not optional:

- **`basis`** — why this data may be used. A provider that cannot state one does
  not belong in the registry. This is the field that makes "no breach data"
  enforceable rather than aspirational: repackaged dump data has no answer here,
  and a vendor who cannot tell you where a number came from has not given you a
  basis, only a contract that moves the liability.
- **`source_url`** on every candidate — a number a human cannot go and check is
  worth less than no number at all, and `enrich` drops those before ranking.

For a paid API, subclass `AuthorizedProvider` instead: it refuses to run unless
its credential is configured (so a half-finished setup fails loudly rather than
silently returning nothing), routes through `provider_cache`, and tags its
candidates `how="authorized-api"` so licensed data is never confused with
something seen on a public page.

`Provider.run()` catches everything a provider raises and turns it into a note on
the report. One unreachable site must not cost the answer four other sources
would have given.

---

## Running it

```bash
# the full lookup
./reachout.py enrich "Amara Okafor" "Northwind Analytics" amara.okafor@northwind.example

# what is registered, and what is configured
./reachout.py providers

# JSON, for anything programmatic
./reachout.py enrich "…" "…" "…" --json

# narrow the sources
./reachout.py enrich "…" "…" "…" --providers company-website,staff-directory
./reachout.py enrich "…" "…" "…" --without public-pages --no-search
```

```python
from reachout.enrich import enrich

lookup = enrich("Amara Okafor", "Northwind Analytics",
                "amara.okafor@northwind.example", region="GB")

if lookup.status == "found":
    print(lookup.answer.e164, lookup.answer.confidence, lookup.answer.source_url)
else:
    print(lookup.status, lookup.reason)
```

`lookup.answer` is `None` unless the status is `found`. That is deliberate: the
only way to get a number out of the result object is through a property that
already checked the status.

## The demonstration

`demo/` contains a fabricated company, four fabricated people and the traps they
demonstrate. Numbers come from the ranges Ofcom reserves for drama
(`07700 900xxx`, `020 7946 0xxx`) and domains sit under `.example`, so nothing in
the corpus can reach a real person.

```bash
python3 demo/run_demo.py            # all four cases
python3 demo/run_demo.py amara      # one case
python3 demo/run_demo.py --json
```

Only the network boundary is faked (`fetch.get`, `search.search`). The HTML
parser, row binder, normaliser, attribution and scoring are the real ones — a
demo that fakes the extraction proves nothing about the extraction.

| Case | Expected | Demonstrates |
|---|---|---|
| `amara` | `found` | Work address beside the number on an independent speaker page; direct dial bound to her directory row |
| `priya` | `found` | The neighbour trap — the row above hers is Amara's, and the window spanning both is refused |
| `tobias` | `not_found` | Listed publicly, publishes no number. The honest answer |
| `namesake` | `found` (the right one) | Same name without the work email; a ceramicist's number is kept below the bar |

---

## Boundaries

Finding a number and using it are separate acts, regulated separately. See
[ETHICS.md](ETHICS.md) — in short: public availability is not permission to
process, GDPR and India's DPDP Act both cover publicly-found personal data, and
in India TRAI's DND rules govern the call you make afterwards regardless of how
you got the number.

The tool has no dialling and no messaging capability, honours `robots.txt`,
makes one request per host at a time, and supports erasure through
`reachout forget`.
