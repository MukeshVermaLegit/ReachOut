# ReachOut

A personal contact-discovery engine. You give it a name and a company; it finds
the most likely work email address and shows you exactly why it believes that.

Pure Python standard library — **no pip install, no dependencies, no API keys
required, no data purchased.** Everything runs and stays on your machine.

```
$ ./reachout.py find "George Robinson" "Grafana Labs"

George Robinson
Grafana Labs · grafana.com (Google Workspace)
  domain confidence 97% — grafana.com has MX records; appears 3x in web search
  pattern john.smith (first.last) — 77% confident from 24 samples, 24 newly learned

  1. george.robinson@grafana.com     ██████████████····  77 MEDIUM
       + matches this company's naming pattern ({first}.{last}, learned from 24 real addresses)  53
       + pattern backed by 24 real addresses                                                     10
       + domain accepts mail (MX present, Google Workspace)                                       8
       + local part contains the person's full first and last name                                6

  2. robinson.george@grafana.com     ███···············  17 VERY LOW
```

## The idea

A commercial provider's moat is a purchased database of hundreds of millions of
records. You cannot rebuild that, and you do not need to. For personal use the
valuable half is the **engine**, not the warehouse:

```
   name + company
         │
         ▼
   company → mail domain          DNS + search + GitHub, cross-checked
         │
         ▼
   learn the company's format     mine real published addresses at that domain
         │
         ▼
   apply it to this person        rank candidates by evidence
         │
         ▼
   check what is checkable        syntax, DNS, MX, provider, role, catch-all
         │
         ▼
   store everything you learned   ← the part that compounds
```

The last step is the whole point. Every lookup writes back what it discovered:
company domains, real addresses seen in public, and the naming pattern each
company uses. Lookup 500 is faster, cheaper and more accurate than lookup 1,
because by then you own a small private dataset that nobody sold you.

**Learning a pattern once answers every future question at that company.**
Twenty-four real `@grafana.com` addresses mined from public commits establish
that Grafana uses `first.last` — and that now applies to every Grafana employee
you will ever look up, without another request.

## What it is not

It is not a ContactOut clone, and it will not match one on coverage. There is no
purchased database behind it, so people with no public footprint at a company
with no discoverable addresses will return a format guess and nothing more. The
tool is built to *tell you that* rather than dress a guess up as a hit.

## Install

```bash
git clone https://github.com/MukeshVermaLegit/ReachOut  && cd ReachOut
run - ./reachout.py serve --port 9000 
./reachout.py doctor

```

Python 3.10+. That is the entire installation.

Optional, all free, all genuinely optional:

| Variable | Why | Without it |
|---|---|---|
| `GITHUB_TOKEN` | 5,000 API req/hour instead of 60 | works, but you get ~6 lookups/hour |
| `BRAVE_API_KEY` | reliable search | falls back to keyless DuckDuckGo |
| `GOOGLE_CSE_KEY` + `GOOGLE_CSE_CX` | reliable search | same |

A GitHub token needs **no scopes at all** — create a classic token with nothing
ticked. This is the single highest-value thing you can do for the tool.

## Commands

```bash
# find one person
./reachout.py find "Jane Doe" "Acme Inc"
./reachout.py find "https://linkedin.com/in/jane-doe" acme.com
./reachout.py find "Jane Doe" acme.com --json          # machine-readable
./reachout.py find "Jane Doe" acme.com --smtp          # opt-in mail-server check

# teach it a company, once - then every lookup there is instant
./reachout.py learn acme.com                       # harvest public sources
./reachout.py learn grafana.com --org grafana --repos 6
./reachout.py pattern grafana.com --samples

# tell it what you already know - the fastest path for small companies
./reachout.py teach acme.com "Alice Wong" alice.wong@acme.com
./reachout.py teach acme.com --pattern "{first}.{last}"
./reachout.py teach --file contacts.csv             # bulk: your own address book
./reachout.py teach acme.com --forget               # drop a stated pattern

# apply what you know, offline, zero network calls
./reachout.py guess "Jane Doe" grafana.com

# how good is this actually? (leave-one-out on your own data)
./reachout.py eval --misses

# phone numbers from name + company + work email - discovery only, never inferred
./reachout.py enrich "Jane Doe" "Acme Inc" jane.doe@acme.com
./reachout.py enrich "Jane Doe" "Acme Inc" jane.doe@acme.com --json
./reachout.py providers                           # what sources are registered

# the older name-only phone command
./reachout.py phone "Jane Doe" "Acme Inc"        # reads site + public PDFs
./reachout.py phone "Jane Doe" "Acme Inc" --no-docs        # skip PDFs
./reachout.py phone "Jane Doe" "Acme Inc" --no-directory   # skip staff directories
./reachout.py find "Jane Doe" acme.com --phone
./reachout.py checkphone "+91 98765 43210"          # validate/classify, offline
./reachout.py teach acme.com "Jane Doe" "+91 98765 43210"

# remove records - previews by default, deletes only with --yes
./reachout.py forget --person "Jane Doe"
./reachout.py forget --domain acme.com --yes
./reachout.py forget --phone "+91 98765 43210" --yes

# other
./reachout.py verify jane.doe@acme.com
./reachout.py github --org grafana --domain grafana.com --save
./reachout.py batch people.csv -o enriched.csv
./reachout.py stats
./reachout.py export --format csv --min 55 > contacts.csv
./reachout.py serve                     # local web UI on 127.0.0.1:8765
./reachout.py serve --port 9000         # everything above, in a browser
```



One real address collapses a four-way guess. Three modes:

| Command | Use when |
|---|---|
| `teach <domain> "<Name>" <email>` | you know one real address |
| `teach <domain> --pattern "{first}"` | you know the format but have no example |
| `teach --file contacts.csv` | you have an address book |

A **stated pattern is never overwritten** by later harvesting, and the tool
checks your input rather than trusting it: an address that no format can build
from the name you gave is banked as a real address but explicitly teaches
nothing, and says so.

### The pattern language

A pattern is written over `{first}` `{last}` `{middle}` and their initials `{f}`
`{l}` `{m}`, plus literal separators — `{f}.{last}` is `j.smith`. Two extensions
cover conventions that whole-token concatenation cannot express:

| Written | Means | Example |
|---|---|---|
| `{first:2}` | the first N characters of a token (`{f}` is `{first:1}` in short) | `{first:2}{last}` → Dakota Long is `dalong` |
| `~8` | cut the finished local part to N characters | `{f}{last}~8` → Florian Bergmann is `fbergman` |

They compose: `{first:2}{last}~8` renders Tim Williams as `tiwillia`. This is the
classic 8-character UNIX/Kerberos login still issued across a lot of legacy
enterprise and Indian IT services, and it is inferred from harvested samples like
any other format — you only need to write one by hand if you are stating a format
`learn` has not seen enough addresses to work out for itself.

The bulk import is the highest-leverage command in the tool. Exporting your
contacts (Google Contacts, Outlook, your mail client) and running one command
turns years of accumulated correspondence into naming patterns for every company
you have ever emailed. Consumer addresses are skipped automatically.

```
$ ./reachout.py teach --file contacts.csv
  imported 340 contacts — 291 addresses banked, 240 taught a format,
                          49 consumer addresses skipped

  companies you can now guess confidently:
    northwind.co        john.smith (first.last)          82%
    vertexlabs.io       jsmith (first initial + last)     65%
```

## The local UI

```bash
./reachout.py serve            # http://127.0.0.1:8765
```

Everything the CLI does, in a browser, against the same database and the same
engine — there is no second implementation of anything, so the two cannot drift
into disagreeing about what the tool knows.

| Tab | What it is for |
|---|---|
| **Find** | a lookup, with every signal, cap and check that produced the score |
| **Phone** | the `enrich` pipeline: the answer, the evidence, and which sources were blocked |
| **Teach** | one known address, a stated format, or a whole address book dropped in |
| **Domains** | every company you have learned — its pattern, its samples, the addresses behind them; harvest or guess from here |
| **Database** | what you have accumulated, who is in it, export it, delete from it |
| **Health** | search backends, GitHub rate limit, DNS, and your own leave-one-out accuracy |

It binds to loopback and refuses anything that did not come from this machine's
own browser: a `Host` header that is not local is rejected (which is what stops a
page on the open web resolving its own name to `127.0.0.1` and reading your
database through your browser), a cross-origin request is rejected, and writes
are accepted only as JSON — a form on another site cannot shape that request, and
a script that tries is stopped by its own preflight. Deleting still previews
first. Binding beyond loopback is possible and says so loudly.

```bash
./reachout.py serve --host 0.0.0.0 --port 9000
warning         bound beyond loopback - anyone who can reach this port
                can read and delete your database
```

## How confidence works

A score is never a bare number — every point is attributable to a named signal,
and the tool refuses to score past ceilings it cannot justify.

| Grade | Score | Means |
|---|---|---|
| **HIGH** | 80–100 | evidence about **this person**: the address was found published, or a mail server accepted it |
| **MEDIUM** | 55–79 | you know the company's format (learned or stated), but nothing confirms this person has a mailbox — the ceiling for any inference |
| **LOW** | 30–54 | weak inference — thin pattern, or a generic format |
| **VERY LOW** | 0–29 | a format guess, nothing more |

Hard ceilings, always shown when they bind:

- **79** for any pattern inference, however certain the format — knowing how a
  company builds addresses says nothing about whether this individual has one
- **93** for a published address that no mail server has confirmed
- **72** on a catch-all domain — the server accepts everything, so acceptance proves nothing
- **30** if the domain has no MX records — it cannot receive mail at all
- **0** if a mail server explicitly rejected the recipient

Nothing ever returns "100% verified", because nothing here can establish that.

## Measured accuracy

`reachout eval` holds out each real address you have collected, re-infers the
pattern from the rest, and checks whether the guess would have been right. It is
free, offline, and runs on *your* data rather than a vendor's claim.

Across 131 real addresses mined from public commits at eight companies:

```
  domain                        n    top-1    top-3
  elastic.co                   42     83%      90%
  grafana.com                  23     78%      83%
  hashicorp.com                15     80%      93%
  redhat.com                   15     53%      93%
  razorpay.com                 12     83%      83%
  zerodha.com                  10     60%      90%
  whipps.com                    8     88%     100%
  sourcegraph.com               6     83%     100%

  overall  131 trials  top-1 77%  top-3 90%
```

The two low top-1 scores are honest, and they are different problems.
**zerodha.com** genuinely runs two formats side by side — `rakesh.ranjan` and
`nithin.k` are both real — so 60% is close to the ceiling for any single-format
guess, and its 90% top-3 is the number that matters. **redhat.com** issues
classic 8-character UNIX logins where the first-name prefix grows to break
collisions (`dlong` was taken, so Dakota Long is `dalong`); which prefix a given
person got is not recoverable from their name, so the alternates are offered
rather than guessed at.

Leave-one-out is deliberately pessimistic — it throws away the very address it
is trying to predict. In real use the tool also remembers every address it has
ever seen, so cases that fail here often succeed in practice:

```
$ ./reachout.py find "Torkel Odegaard" grafana.com
  1. torkel@grafana.com    85 HIGH  ← found published
```

That name is a leave-one-out *miss* (`{first}` is unguessable from a `first.last`
company) and a real-world *hit*, because the address was banked during `learn`.

The remaining misses are the honest limits of inferring an address from a name:

| Person | Actual | Guessed | Why |
|---|---|---|---|
| Tolyan Korniltsev | `anatoly.korniltsev` | `tolyan.korniltsev` | goes by a nickname |
| Roberto Sanchez | `roberto.jimenez` | `roberto.sanchez` | two surnames, company used the other |
| Yuri Tseretyan | `yuriy.tseretyan` | `yuri.tseretyan` | transliteration variant |
| Sanika Chavan | `sanika.vikaschavan` | `sanika.chavan` | middle name inside the surname |

No amount of engineering fixes these from a name alone — the employer used a name
you were never given. Run `eval` on your own
data rather than trusting a number in a README.

## Where the data comes from

Public sources only, and each one is recorded against every address it produced:

- **Public git commits** (GitHub API) — the highest-yield free source by a wide
  margin. Commits carry the author's name *and* the address they committed with,
  which is exactly the (name → address) pair pattern inference needs.
- **Company websites** — team, about and contact pages, including Cloudflare-
  obfuscated and `name [at] company [dot] com` addresses.
- **Web search** result snippets.
- **DNS** — MX records, mail provider fingerprinting, SPF.

LinkedIn is never scraped. A LinkedIn URL is only read for the name in its slug.

## Phone numbers

**Email works because it is derivable. Phone numbers are not.** `first.last@domain`
is a rule you can learn from four examples and apply to a thousand people. No
such rule exists for mobiles — nobody can compute a number from a name. Any tool
that appears to is reading a purchased database or enumerating ranges, and
ReachOut does neither. Number generation is deliberately not implemented.

So this half of the tool is pure discovery plus validation, and the hard part is
not finding numbers — a company contact page is full of them — but working out
**whose they are**:

```
$ ./reachout.py phone "Nithin Kamath" "Zerodha"

Nithin Kamath  ·  zerodha.com

  phone — numbers that reach Nithin Kamath
    none found. No public page ties a number to this person.

  company numbers — these reach the switchboard, not Nithin Kamath
    +91 80404 02020                  mobile
```

That number is a real, valid, mobile-series line. It scores **0** for reaching
Nithin Kamath, because the page's structured data attributes it to the
organisation. Line type and ownership are separate questions, and conflating
them is how a switchboard gets presented as someone's cell.

### Giving it the work email

`enrich` takes the person's **work email** as well as their name and company, and
that changes what the evidence can prove. A name is a weak identifier — every
page about anybody with that name matches. A work address was issued by the
employer to exactly one person, so a page printing it beside a number is
*stating* the association rather than leaving it to be guessed at, and a page
printing a colleague's address instead is the clearest possible sign the entry
is not theirs.

```
$ ./reachout.py enrich "Amara Okafor" "Northwind Analytics" amara.okafor@northwind.example

  Amara Okafor  ·  amara.okafor@northwind.example
  FOUND — published at https://dataday.example/speakers/2026 with evidence tying it
          to Amara Okafor

  +44 7700 900412   mobile   96 HIGH
  seen at https://dataday.example/speakers/2026

  evidence for +44 7700 900412  (1 independent source(s))
     + published in the same entry as amara.okafor@northwind.example, the work
       address you gave for this person 76
     + labelled as a mobile/direct/personal line 14
     + valid GB number 10
     + allocated in the mobile series 8
     · capped at 96: no call has been placed to confirm it
```

Three statuses, and only one of them is an answer: `found`, `needs_review`,
`not_found`. `not_found` is the expected outcome for most people — personal
numbers are mostly not published, and a tool that finds one every time is
guessing. Sources are pluggable, so an authorized data provider you hold a
contract with can be registered alongside the public ones.

**Set a search key.** Three of the six providers cannot find anything without
search, and the keyless DuckDuckGo fallback rate-limits after two or three
queries — while one lookup makes five or six. Copy `keys.env.example` to
`data/keys.env` and fill in Brave, Google, or both; the backends are tried as a
chain and fail differently (Brave is capped per second, Google per day), so one
is usually up when the other is not. A blocked engine is reported as `degraded`,
never as "nothing found", and a `not_found` reached with sources blocked says so
in its reason. `./reachout.py doctor` probes each backend.

Google no longer offers "Search the entire web" on every account, so most
Programmable Search Engines are restricted to a list of sites. That is fine and
supported: a restricted index is treated as **supplementary**, so finding
nothing there falls through to the next backend instead of ending the search —
"not on those thirty sites" is not "not on the web". Set
`GOOGLE_CSE_WHOLE_WEB=1` only if yours genuinely searches everything.
`keys.env.example` lists which sites actually carry phone numbers; press-release
wires are the best of them, because every release ends in a named media contact
with a direct line.

Full write-up: **[docs/PHONE_ENRICHMENT.md](docs/PHONE_ENRICHMENT.md)** —
architecture, schema, entity resolution, the scoring table, and how to add a
provider. Runnable synthetic demonstration: `python3 demo/run_demo.py`.

### What it reads

| Source | Strength |
|---|---|
| JSON-LD / schema.org `telephone` | strongest — it states whether a `Person` or an `Organization` owns the number |
| **staff directory rows** | **structural — a number and a name inside the same block of markup, see below** |
| `tel:` links | reliable, and carries the surrounding text |
| microdata `itemprop="telephone"` | reliable |
| public PDFs | CVs, papers, conference programmes — see below |
| plain page text | weakest — strict patterns, since loose digit matching turns invoice numbers into phone numbers |
| web search snippets | only when the person's name appears alongside |

When the same number is seen more than one way, the **strongest sighting wins**.
Without that rule a plain-text sighting found first would quietly override the
directory row that actually names the owner.

### Staff directories, and why proximity is not attribution

A contact page gives you the switchboard. A *directory* gives you a row per
person, and that row usually carries a direct line — so it is the one public
source that answers "which number reaches this person" instead of "which
numbers does this organisation own".

It cannot be read like an ordinary page. Here is a real VIT staff list, as the
text scanner sees it:

```
... Relations Officer  international@vit.ac.in / ir2@vit.ac.in  +91-416-2202070
Mr. Manoj Kumar Kannan  International Relations Officer  ir7@vit.ac.in  +91-416-2202072
Dr. Sainu Franco  International Relations Officer  ...        +91-416-2202074
```

A ±70-character window around `…2070` reaches forward into the next entry, finds
"Mr. Manoj Kumar Kannan", and hands you **the number above his** — the person
one row off. On a 200-row directory that failure repeats 200 times, and every
answer looks confident.

So attribution here is structural, not positional:

> A number belongs to a person only when the **smallest enclosing block of
> markup** containing both holds **exactly one** candidate name.

Two names in the block, or none, and the number stays unattributed. A `<tr>`
qualifies; a `<div>` wrapping the whole page does not. Job titles and company
names are rejected before the count, or "Assistant Professor" and "Morrow Water
Technologies" become people. A binding found this way also overrides mere
proximity elsewhere on the page:

```
$ ./reachout.py phone "Manoj Kumar Kannan" vit.ac.in

  phone — numbers that reach Manoj Kumar Kannan
    +91 416 2202072                    landline  74 MEDIUM
       + listed in a staff directory on the same row as Manoj Kumar Kannan  62
       + published in a staff directory, one entry per person                8
       + valid IN number                                                    10
       − a fixed line, so not a mobile                                       6
```

`…2070` — the number proximity would have given you — is no longer offered, and
says why.

Discovery follows the site's own navigation and descends one level when a page
turns out to be a landing page rather than a listing, within a hard page budget.
Where a search key is configured it also runs a site-scoped search, which is the
more reliable route: **without `BRAVE_API_KEY` or Google CSE, keyless DuckDuckGo
is usually blocked, and directory discovery degrades to link-following only.**
Pages `robots.txt` forbids are skipped and *reported as skipped* rather than
reported as empty — those are different answers.

Rows belonging to other people are discarded on sight. Answering a question
about one person is not a reason to bank a few hundred strangers' direct dials,
so they never reach the database.

### It reads PDFs, with no dependency

Contact details reach the open web in documents far more often than on company
pages, and those documents are nearly all PDFs. `zlib` is in the standard
library, and PDF content streams are Flate-compressed, so no dependency is
needed to read them.

Two things make the difference between recovering text and recovering nothing:

**Stream bounds come from `/Length`, not from scanning for `endstream`.**
Compressed image data routinely contains those bytes, which truncates one stream
and desynchronises every stream after it. On one test paper that lost the entire
title page — exactly where the contact details are.

**Academic brace groups are expanded.** Papers write addresses as
`{alice,bob,carol}@dept.edu`, and PDF extraction renders the LaTeX braces as
stray `f` and `g` glyphs from a math font. Handling that turns a miss into four
addresses:

```
f jacobdevlin,mingweichang,kentonl,kristout g @google.com
        ↓
jacobdevlin@google.com   mingweichang@google.com
kentonl@google.com       kristout@google.com
```

Measured on four well-known papers: two yield every author address, one
correctly yields nothing (it publishes no emails), one still loses its title
page. Scanned/image-only PDFs yield nothing, which is correct.

### Country is inferred from the domain

A bare ten-digit number means different things in different places. `617-253-1000`
on a `.edu` page is a Boston landline; read as Indian it becomes a mobile that
does not exist. The region comes from the domain suffix before any number is
parsed — `.edu`/`.gov` → US, `.ac.in` → IN, `.co.uk` → GB — and neutral suffixes
(`.com`, `.io`, `.ai`) fall back to your configured default.

### Validation is genuinely solid

Unlike email, phone validation needs no network and no guessing:

```
$ ./reachout.py checkphone "9876543210"        $ ./reachout.py checkphone "+1 415 555 2671"
  +91 98765 43210                                +1 (415) 555-2671
  country      IN (+91)                          country      US/CA (+1)
  line type    mobile                            line type    unknown — cannot be determined
                                                 · US/Canada numbering cannot
                                                   distinguish mobile from landline
```

India allocates 6–9 exclusively to mobile, so the answer is certain. The US and
Canada lost that property to number portability, and the tool says so rather
than guessing. UK `07` is mobile; unknown countries are validated for shape only.

### Expect low yield, and that is correct

Most people's mobile numbers are simply not published anywhere. You will get
good results for academics (faculty pages, papers), small-business owners,
people with personal sites, and anyone whose company publishes direct dials.
You will get nothing for a salaried engineer at a private startup — and nothing
is the honest answer, not a guess dressed up as one.

`teach` works here too, and is the realistic path for people you actually know:

```bash
./reachout.py teach acme.com "Jane Doe" "+91 98765 43210"
```

Confidence caps at 96 even then — your word is strong evidence, but no call has
been placed.

### Guarding against made-up numbers

A dummy number passes every structural check — `9876543210` is a perfectly valid
Indian mobile by the numbering rules. What gives placeholders away is that real
allocations are not tidy, so `teach` refuses numbers whose digits run in
sequence, repeat, or fall in the NANP `555-01xx` fiction range:

```
$ ./reachout.py teach acme.com "Jane Doe" "+91 98765 43210"
  +91 98765 43210 looks like a placeholder: digits run in descending sequence
  Refusing to record it. Re-run with --force if it is genuinely real.
```

The same check penalises placeholders found while crawling, so a demo number on
a template site never ranks.

## Removing records

Keeping data about real people means being able to delete it. `forget` previews
by default and only deletes with `--yes`:

```
$ ./reachout.py forget --person "Jane Doe"
would remove:
  people  (1)
    Jane Doe
  emails  (3)
    jane.doe@acme.com …

  4 record group(s). Re-run with --yes to delete.
```

It removes across every table — the person, their addresses and numbers, the
evidence trail, and the pattern samples an address contributed. `--domain` wipes
everything known about a company; `--email` and `--phone` remove one record.

## Boundaries

This is a research tool for finding a professional contact you have a real
reason to reach. It is not built for bulk outreach and deliberately makes that
awkward: one request per host at a time, robots.txt honoured by default, and no
send capability of any kind.

- Everything stays in a local SQLite file. Nothing is uploaded anywhere.
- `robots.txt` is respected unless you set `REACHOUT_RESPECT_ROBOTS=0`.
- SMTP probing is **off** by default — see [docs/VERIFICATION.md](docs/VERIFICATION.md).
- If you are in the EU, UK, or India, personal data you collect about identifiable
  people falls under GDPR / the DPDP Act even when it is publicly available and
  even when it is only for you. See [docs/ETHICS.md](docs/ETHICS.md).

## Layout

```
reachout.py            entry point
reachout/
  cli.py               commands and terminal output
  engine.py            the find/learn pipeline
  patterns.py          naming-pattern inference        ← the core idea
  scoring.py           evidence → confidence
  verify.py            syntax, DNS, role, catch-all, SMTP
  company.py           company name → mail domain
  names.py             name parsing and normalisation
  netdns.py            DNS client written on raw sockets
  fetch.py             polite cached HTTP
  db.py                SQLite storage (your accumulating asset)
  evaluate.py          leave-one-out accuracy
  pdf.py               PDF text extraction, standard library only
  phones.py            number parsing, validation, line-type rules
  phonefind.py         phone discovery and whose-number-is-it attribution
  identity.py          who the subject is, and does this page mean them
  providers.py         every phone source behind one interface, plus the registry
  enrich.py            name + company + work email → found / needs_review / not_found
  web.py               local web UI: routing, guards, and JSON over the engine
  ui/                  the page itself - index.html, app.css, app.js
  sources/
    github.py          public commits and profiles
    website.py         team/about/contact pages
    search.py          Brave / Google CSE / DuckDuckGo
    documents.py       public PDFs and CVs, academic brace groups
    directory.py       staff/faculty directories, row-scoped attribution
demo/                  a synthetic company and four synthetic people
  synthetic.py         the fixture corpus, and the traps it is built from
  fake_web.py          stands in for the network so the demo runs offline
  run_demo.py          python3 demo/run_demo.py
tests/                 292 offline tests, no network
```

```bash
python3 -m unittest discover -s tests
```

## Configuration

Every setting is an environment variable; the defaults are sensible.

```bash
REACHOUT_HOME=~/.reachout      # database + cache location
REACHOUT_HOST_DELAY=1.5        # seconds between requests to the same host
REACHOUT_CACHE_TTL=604800      # HTTP cache lifetime (7 days)
REACHOUT_RESPECT_ROBOTS=1      # set 0 only if you know why
REACHOUT_SMTP=0                # set 1 to enable SMTP probing by default
REACHOUT_KEYS=…                # key file location (default: data/keys.env)
```

API keys can live in `data/keys.env` instead of your shell profile — gitignored,
one `NAME=value` per line, and a real environment variable always overrides it.
See `keys.env.example`.

```bash
BRAVE_API_KEY=…                # 2,000 searches/month, 1/second
GOOGLE_CSE_KEY=… GOOGLE_CSE_CX=…   # 100 searches/day
GITHUB_TOKEN=…                 # 60/hour → 5,000/hour
```

## Next steps that would actually pay off

1. **Set `GITHUB_TOKEN`.** 60 → 5,000 requests/hour, so `learn` can scan 20 repos
   instead of 4 and patterns get materially more confident.
2. **Import your address book.** `teach --file contacts.csv` is the single
   highest-yield command — your own correspondence is ground truth that no
   provider could sell you.
3. **Pre-learn the companies you care about.** `learn` each one once; afterwards
   `guess` answers offline and instantly.
4. **Run `eval` regularly.** It is the only number about this tool that is yours.
5. A browser extension is the obvious wrapper, but build it last — the engine and
   its accuracy are what matter, and both are testable from the CLI today.
