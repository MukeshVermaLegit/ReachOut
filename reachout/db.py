"""SQLite storage.

This file is the actual asset the tool builds. Every lookup writes back what it
learned - people, company domains, observed email samples, inferred naming
patterns - so lookup number 5,000 is cheaper and more accurate than lookup one.
"""

from __future__ import annotations

import json
import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path

from . import config

SCHEMA_VERSION = 2

# Marks a record the user supplied directly, as opposed to one we discovered.
USER_SOURCE = "you told me"

SCHEMA = """
CREATE TABLE IF NOT EXISTS companies (
    id          INTEGER PRIMARY KEY,
    name        TEXT NOT NULL,
    name_key    TEXT NOT NULL UNIQUE,
    domain      TEXT,
    confidence  REAL DEFAULT 0,
    source      TEXT,
    created_at  REAL NOT NULL,
    updated_at  REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_companies_domain ON companies(domain);

CREATE TABLE IF NOT EXISTS domains (
    domain        TEXT PRIMARY KEY,
    resolves      INTEGER DEFAULT 0,
    has_mx        INTEGER DEFAULT 0,
    provider      TEXT DEFAULT '',
    spf           TEXT DEFAULT '',
    mx_hosts      TEXT DEFAULT '',
    catch_all     INTEGER DEFAULT -1,   -- -1 unknown, 0 no, 1 yes
    checked_at    REAL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS people (
    id          INTEGER PRIMARY KEY,
    name_raw    TEXT NOT NULL,
    first       TEXT DEFAULT '',
    middle      TEXT DEFAULT '',
    last        TEXT DEFAULT '',
    company_id  INTEGER REFERENCES companies(id),
    title       TEXT DEFAULT '',
    location    TEXT DEFAULT '',
    linkedin    TEXT DEFAULT '',
    github      TEXT DEFAULT '',
    website     TEXT DEFAULT '',
    notes       TEXT DEFAULT '',
    tags        TEXT DEFAULT '',
    dedupe_key  TEXT UNIQUE,
    created_at  REAL NOT NULL,
    updated_at  REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_people_last ON people(last);
CREATE INDEX IF NOT EXISTS idx_people_company ON people(company_id);

CREATE TABLE IF NOT EXISTS emails (
    id          INTEGER PRIMARY KEY,
    person_id   INTEGER REFERENCES people(id) ON DELETE CASCADE,
    address     TEXT NOT NULL,
    local       TEXT NOT NULL,
    domain      TEXT NOT NULL,
    confidence  INTEGER DEFAULT 0,
    grade       TEXT DEFAULT '',
    status      TEXT DEFAULT 'candidate',  -- candidate | observed | confirmed | rejected
    pattern     TEXT DEFAULT '',
    checked_at  REAL DEFAULT 0,
    created_at  REAL NOT NULL,
    UNIQUE(person_id, address)
);
CREATE INDEX IF NOT EXISTS idx_emails_domain ON emails(domain);
CREATE INDEX IF NOT EXISTS idx_emails_address ON emails(address);

-- Raw evidence. Never overwritten, only appended.
CREATE TABLE IF NOT EXISTS observations (
    id          INTEGER PRIMARY KEY,
    person_id   INTEGER,
    email_id    INTEGER,
    kind        TEXT NOT NULL,
    detail      TEXT DEFAULT '',
    source_url  TEXT DEFAULT '',
    weight      REAL DEFAULT 0,
    seen_at     REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_obs_email ON observations(email_id);
CREATE INDEX IF NOT EXISTS idx_obs_person ON observations(person_id);

-- (domain, real name, real local-part) triples: the training data for patterns.
CREATE TABLE IF NOT EXISTS pattern_samples (
    id          INTEGER PRIMARY KEY,
    domain      TEXT NOT NULL,
    first       TEXT NOT NULL,
    last        TEXT NOT NULL,
    local       TEXT NOT NULL,
    template    TEXT DEFAULT '',
    source_url  TEXT DEFAULT '',
    created_at  REAL NOT NULL,
    UNIQUE(domain, local, first, last)
);
CREATE INDEX IF NOT EXISTS idx_samples_domain ON pattern_samples(domain);

CREATE TABLE IF NOT EXISTS domain_patterns (
    domain      TEXT NOT NULL,
    template    TEXT NOT NULL,
    votes       INTEGER DEFAULT 0,
    samples     INTEGER DEFAULT 0,
    confidence  REAL DEFAULT 0,
    updated_at  REAL NOT NULL,
    PRIMARY KEY (domain, template)
);

-- Every address we have ever seen published at a domain, whether or not we could
-- attach a name to it. Re-checked against each new person we look up.
CREATE TABLE IF NOT EXISTS domain_emails (
    domain      TEXT NOT NULL,
    address     TEXT NOT NULL,
    context     TEXT DEFAULT '',
    source_url  TEXT DEFAULT '',
    is_role     INTEGER DEFAULT 0,
    seen_at     REAL NOT NULL,
    PRIMARY KEY (domain, address)
);
CREATE INDEX IF NOT EXISTS idx_domain_emails_domain ON domain_emails(domain);

-- A pattern you told us directly ("I know this company uses first.last").
-- Kept apart from inferred patterns so re-harvesting never overwrites what you know.
CREATE TABLE IF NOT EXISTS pinned_patterns (
    domain      TEXT NOT NULL,
    template    TEXT NOT NULL,
    confidence  REAL DEFAULT 0.9,
    note        TEXT DEFAULT '',
    created_at  REAL NOT NULL,
    PRIMARY KEY (domain, template)
);

CREATE TABLE IF NOT EXISTS phones (
    id          INTEGER PRIMARY KEY,
    person_id   INTEGER REFERENCES people(id) ON DELETE CASCADE,
    e164        TEXT NOT NULL,
    kind        TEXT DEFAULT 'unknown',
    country     TEXT DEFAULT '',
    scope       TEXT DEFAULT 'page',
    attribution TEXT DEFAULT 'unknown',   -- person | company | unknown
    confidence  INTEGER DEFAULT 0,
    grade       TEXT DEFAULT '',
    status      TEXT DEFAULT 'candidate', -- candidate | observed | confirmed
    source_url  TEXT DEFAULT '',
    context     TEXT DEFAULT '',
    created_at  REAL NOT NULL,
    UNIQUE(person_id, e164)
);
CREATE INDEX IF NOT EXISTS idx_phones_person ON phones(person_id);

-- Numbers seen at a domain, whether or not anyone is attached to them.
CREATE TABLE IF NOT EXISTS domain_phones (
    domain      TEXT NOT NULL,
    e164        TEXT NOT NULL,
    kind        TEXT DEFAULT 'unknown',
    scope       TEXT DEFAULT 'page',
    owner       TEXT DEFAULT '',
    context     TEXT DEFAULT '',
    source_url  TEXT DEFAULT '',
    seen_at     REAL NOT NULL,
    PRIMARY KEY (domain, e164)
);

-- One phone-enrichment run: what was asked, what was decided, and on what.
-- Kept because a confidence score is only meaningful next to the evidence that
-- produced it, and because "why did it say that last week?" is a real question.
CREATE TABLE IF NOT EXISTS phone_lookups (
    id           INTEGER PRIMARY KEY,
    subject_key  TEXT NOT NULL,
    name_raw     TEXT DEFAULT '',
    work_email   TEXT DEFAULT '',
    company      TEXT DEFAULT '',
    domain       TEXT DEFAULT '',
    status       TEXT DEFAULT 'not_found',   -- found | needs_review | not_found
    reason       TEXT DEFAULT '',
    best_e164    TEXT DEFAULT '',
    best_score   INTEGER DEFAULT 0,
    best_source  TEXT DEFAULT '',
    candidates   INTEGER DEFAULT 0,
    providers    TEXT DEFAULT '',
    elapsed      REAL DEFAULT 0,
    ran_at       REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_lookups_subject ON phone_lookups(subject_key);

-- Every sighting behind a lookup, one row per (number, URL).
CREATE TABLE IF NOT EXISTS phone_lookup_evidence (
    id          INTEGER PRIMARY KEY,
    lookup_id   INTEGER REFERENCES phone_lookups(id) ON DELETE CASCADE,
    e164        TEXT NOT NULL,
    provider    TEXT DEFAULT '',
    how         TEXT DEFAULT '',
    basis       TEXT DEFAULT '',
    source_url  TEXT DEFAULT '',
    context     TEXT DEFAULT '',
    owner       TEXT DEFAULT '',
    seen_at     REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_lookup_evidence ON phone_lookup_evidence(lookup_id);

-- Responses from licensed providers. Those calls are metered and usually billed,
-- so the same subject must not cost twice inside the TTL.
CREATE TABLE IF NOT EXISTS provider_cache (
    provider    TEXT NOT NULL,
    subject_key TEXT NOT NULL,
    payload     TEXT NOT NULL,
    fetched_at  REAL NOT NULL,
    PRIMARY KEY (provider, subject_key)
);

CREATE TABLE IF NOT EXISTS searches (
    id          INTEGER PRIMARY KEY,
    query       TEXT NOT NULL,
    company     TEXT DEFAULT '',
    found       INTEGER DEFAULT 0,
    best        TEXT DEFAULT '',
    ran_at      REAL NOT NULL
);
"""


def _now() -> float:
    return time.time()


def _drop_lookups(conn, where: str, params: tuple) -> int:
    """Delete matching lookups and the evidence hanging off them."""
    ids = [r["id"] for r in conn.execute(
        f"SELECT id FROM phone_lookups WHERE {where}", params).fetchall()]
    if not ids:
        return 0
    marks = ",".join("?" * len(ids))
    removed = conn.execute(
        f"DELETE FROM phone_lookup_evidence WHERE lookup_id IN ({marks})", ids).rowcount
    return removed + conn.execute(
        f"DELETE FROM phone_lookups WHERE id IN ({marks})", ids).rowcount


class Database:
    def __init__(self, path: Path | str | None = None):
        self.path = Path(path) if path else config.DB_PATH
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(str(self.path), timeout=30.0)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA foreign_keys=ON")
        self.conn.executescript(SCHEMA)
        self.conn.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
        self.conn.commit()

    # ---------------------------------------------------------------- helpers
    @contextmanager
    def tx(self):
        try:
            yield self.conn
            self.conn.commit()
        except Exception:
            self.conn.rollback()
            raise

    def close(self) -> None:
        self.conn.close()

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        self.close()

    # -------------------------------------------------------------- companies
    def upsert_company(self, name: str, domain: str = "", confidence: float = 0.0,
                       source: str = "") -> int:
        from .names import slug
        key = slug(name) or (domain or "").lower()
        if not key:
            raise ValueError("company needs a name or domain")
        row = self.conn.execute("SELECT * FROM companies WHERE name_key=?", (key,)).fetchone()
        now = _now()
        if row:
            # Only overwrite the domain when the new evidence is at least as good.
            if domain and (not row["domain"] or confidence >= (row["confidence"] or 0)):
                self.conn.execute(
                    "UPDATE companies SET domain=?, confidence=?, source=?, updated_at=? WHERE id=?",
                    (domain.lower(), confidence, source or row["source"], now, row["id"]),
                )
                self.conn.commit()
            return int(row["id"])
        cur = self.conn.execute(
            "INSERT INTO companies(name, name_key, domain, confidence, source, created_at, updated_at)"
            " VALUES(?,?,?,?,?,?,?)",
            (name, key, (domain or "").lower(), confidence, source, now, now),
        )
        self.conn.commit()
        return int(cur.lastrowid)

    def company_domain(self, name: str) -> tuple[str, float]:
        domain, confidence, _source = self.company_record(name)
        return domain, confidence

    def company_record(self, name: str) -> tuple[str, float, str]:
        """Domain, confidence and *where it came from*.

        The source matters: a mapping you supplied is knowledge, while one the
        resolver stored is its own earlier guess. Re-serving a guess as
        "already in your local database" let one bad resolution harden into a
        high-confidence fact that nothing could dislodge - IIT Bombay came back
        as the alumni association at 85% that way.
        """
        from .names import slug
        row = self.conn.execute(
            "SELECT domain, confidence, source FROM companies WHERE name_key=?",
            (slug(name),),
        ).fetchone()
        if row and row["domain"]:
            return row["domain"], float(row["confidence"] or 0), (row["source"] or "")
        return "", 0.0, ""

    # ---------------------------------------------------------------- domains
    def save_domain_facts(self, facts: dict) -> None:
        self.conn.execute(
            "INSERT INTO domains(domain, resolves, has_mx, provider, spf, mx_hosts, checked_at)"
            " VALUES(?,?,?,?,?,?,?)"
            " ON CONFLICT(domain) DO UPDATE SET resolves=excluded.resolves, has_mx=excluded.has_mx,"
            " provider=excluded.provider, spf=excluded.spf, mx_hosts=excluded.mx_hosts,"
            " checked_at=excluded.checked_at",
            (facts["domain"], int(facts["resolves"]), int(facts["has_mx"]), facts["provider"],
             facts["spf"], json.dumps(facts["mx"]), _now()),
        )
        self.conn.commit()

    def get_domain_facts(self, domain: str, max_age: float = 86400 * 7) -> dict | None:
        row = self.conn.execute("SELECT * FROM domains WHERE domain=?", (domain,)).fetchone()
        if not row or (_now() - (row["checked_at"] or 0)) > max_age:
            return None
        return {
            "domain": row["domain"],
            "resolves": bool(row["resolves"]),
            "has_mx": bool(row["has_mx"]),
            "provider": row["provider"] or "",
            "spf": row["spf"] or "",
            "mx": json.loads(row["mx_hosts"] or "[]"),
            "catch_all": row["catch_all"],
        }

    def set_catch_all(self, domain: str, value: int) -> None:
        self.conn.execute(
            "INSERT INTO domains(domain, catch_all, checked_at) VALUES(?,?,?)"
            " ON CONFLICT(domain) DO UPDATE SET catch_all=excluded.catch_all",
            (domain, value, _now()),
        )
        self.conn.commit()

    # ----------------------------------------------------------------- people
    def upsert_person(self, name, company_id: int | None = None, **fields) -> int:
        """`name` is a names.Name. Dedupe on (first, last, company)."""
        key = f"{name.first}|{name.last}|{company_id or 0}"
        now = _now()
        row = self.conn.execute("SELECT * FROM people WHERE dedupe_key=?", (key,)).fetchone()
        allowed = {"title", "location", "linkedin", "github", "website", "notes", "tags"}
        clean = {k: v for k, v in fields.items() if k in allowed and v}
        if row:
            if clean:
                sets = ", ".join(f"{k}=?" for k in clean)
                self.conn.execute(
                    f"UPDATE people SET {sets}, updated_at=? WHERE id=?",
                    (*clean.values(), now, row["id"]),
                )
                self.conn.commit()
            return int(row["id"])
        cur = self.conn.execute(
            "INSERT INTO people(name_raw, first, middle, last, company_id, title, location,"
            " linkedin, github, website, notes, tags, dedupe_key, created_at, updated_at)"
            " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (name.raw or name.display, name.first, name.middle, name.last, company_id,
             clean.get("title", ""), clean.get("location", ""), clean.get("linkedin", ""),
             clean.get("github", ""), clean.get("website", ""), clean.get("notes", ""),
             clean.get("tags", ""), key, now, now),
        )
        self.conn.commit()
        return int(cur.lastrowid)

    # ----------------------------------------------------------------- emails
    def add_email(self, person_id: int, address: str, confidence: int, grade: str,
                  status: str = "candidate", pattern: str = "") -> int:
        address = address.lower().strip()
        local, _, domain = address.partition("@")
        now = _now()
        row = self.conn.execute(
            "SELECT id FROM emails WHERE person_id=? AND address=?", (person_id, address)
        ).fetchone()
        if row:
            self.conn.execute(
                "UPDATE emails SET confidence=?, grade=?, status=?, pattern=?, checked_at=? WHERE id=?",
                (confidence, grade, status, pattern, now, row["id"]),
            )
            self.conn.commit()
            return int(row["id"])
        cur = self.conn.execute(
            "INSERT INTO emails(person_id, address, local, domain, confidence, grade, status,"
            " pattern, checked_at, created_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
            (person_id, address, local, domain, confidence, grade, status, pattern, now, now),
        )
        self.conn.commit()
        return int(cur.lastrowid)

    def add_observation(self, kind: str, detail: str = "", source_url: str = "",
                        weight: float = 0.0, person_id: int | None = None,
                        email_id: int | None = None) -> None:
        self.conn.execute(
            "INSERT INTO observations(person_id, email_id, kind, detail, source_url, weight, seen_at)"
            " VALUES(?,?,?,?,?,?,?)",
            (person_id, email_id, kind, detail, source_url, weight, _now()),
        )
        self.conn.commit()

    def emails_for_domain(self, domain: str) -> list[str]:
        rows = self.conn.execute(
            "SELECT DISTINCT address FROM emails WHERE domain=? AND status IN ('observed','confirmed')",
            (domain,),
        ).fetchall()
        return [r["address"] for r in rows]

    # --------------------------------------------------------------- patterns
    def add_sample(self, domain: str, first: str, last: str, local: str,
                   template: str = "", source_url: str = "") -> bool:
        """Record a (name -> local-part) observation. Returns True if it was new."""
        try:
            self.conn.execute(
                "INSERT INTO pattern_samples(domain, first, last, local, template, source_url, created_at)"
                " VALUES(?,?,?,?,?,?,?)",
                (domain.lower(), first, last, local.lower(), template, source_url, _now()),
            )
            self.conn.commit()
            return True
        except sqlite3.IntegrityError:
            return False

    def samples_for_domain(self, domain: str) -> list[sqlite3.Row]:
        return self.conn.execute(
            "SELECT * FROM pattern_samples WHERE domain=? ORDER BY created_at", (domain.lower(),)
        ).fetchall()

    def save_patterns(self, domain: str, ranked: list[tuple[str, int, int, float]]) -> None:
        now = _now()
        with self.tx() as conn:
            conn.execute("DELETE FROM domain_patterns WHERE domain=?", (domain.lower(),))
            conn.executemany(
                "INSERT INTO domain_patterns(domain, template, votes, samples, confidence, updated_at)"
                " VALUES(?,?,?,?,?,?)",
                [(domain.lower(), t, v, s, c, now) for t, v, s, c in ranked],
            )

    def get_patterns(self, domain: str) -> list[sqlite3.Row]:
        return self.conn.execute(
            "SELECT * FROM domain_patterns WHERE domain=? ORDER BY confidence DESC, votes DESC",
            (domain.lower(),),
        ).fetchall()

    def known_pattern_domains(self) -> int:
        row = self.conn.execute(
            "SELECT COUNT(DISTINCT domain) AS n FROM domain_patterns WHERE confidence >= 0.5"
        ).fetchone()
        return int(row["n"])

    # --------------------------------------------------------- domain mailbox
    def add_domain_email(self, domain: str, address: str, context: str = "",
                         source_url: str = "", is_role: bool = False) -> bool:
        """Remember a published address. Returns True when it is new to us."""
        try:
            self.conn.execute(
                "INSERT INTO domain_emails(domain, address, context, source_url, is_role, seen_at)"
                " VALUES(?,?,?,?,?,?)",
                (domain.lower(), address.lower(), context, source_url, int(is_role), _now()),
            )
            self.conn.commit()
            return True
        except sqlite3.IntegrityError:
            return False

    def domain_emails(self, domain: str, include_role: bool = False) -> list[sqlite3.Row]:
        sql = "SELECT * FROM domain_emails WHERE domain=?"
        if not include_role:
            sql += " AND is_role=0"
        return self.conn.execute(sql + " ORDER BY seen_at", (domain.lower(),)).fetchall()

    # ---------------------------------------------------- patterns you assert
    def pin_pattern(self, domain: str, template: str, confidence: float = 0.9,
                    note: str = "") -> None:
        self.conn.execute(
            "INSERT INTO pinned_patterns(domain, template, confidence, note, created_at)"
            " VALUES(?,?,?,?,?)"
            " ON CONFLICT(domain, template) DO UPDATE SET confidence=excluded.confidence,"
            " note=excluded.note",
            (domain.lower(), template, confidence, note, _now()),
        )
        self.conn.commit()

    def pinned_patterns(self, domain: str) -> list[sqlite3.Row]:
        return self.conn.execute(
            "SELECT * FROM pinned_patterns WHERE domain=? ORDER BY confidence DESC",
            (domain.lower(),),
        ).fetchall()

    def unpin_pattern(self, domain: str, template: str = "") -> int:
        if template:
            cur = self.conn.execute(
                "DELETE FROM pinned_patterns WHERE domain=? AND template=?",
                (domain.lower(), template))
        else:
            cur = self.conn.execute("DELETE FROM pinned_patterns WHERE domain=?", (domain.lower(),))
        self.conn.commit()
        return cur.rowcount

    # ----------------------------------------------------------------- phones
    def add_domain_phone(self, domain: str, e164: str, kind: str = "unknown",
                         scope: str = "page", owner: str = "", context: str = "",
                         source_url: str = "") -> bool:
        try:
            self.conn.execute(
                "INSERT INTO domain_phones(domain, e164, kind, scope, owner, context,"
                " source_url, seen_at) VALUES(?,?,?,?,?,?,?,?)",
                (domain.lower(), e164, kind, scope, owner, context, source_url, _now()),
            )
            self.conn.commit()
            return True
        except sqlite3.IntegrityError:
            # Upgrade a bare sighting if we now know more about it.
            self.conn.execute(
                "UPDATE domain_phones SET owner=COALESCE(NULLIF(?,''), owner),"
                " scope=CASE WHEN scope='page' THEN ? ELSE scope END,"
                " context=COALESCE(NULLIF(?,''), context)"
                " WHERE domain=? AND e164=?",
                (owner, scope, context, domain.lower(), e164))
            self.conn.commit()
            return False

    def domain_phones(self, domain: str) -> list[sqlite3.Row]:
        return self.conn.execute(
            "SELECT * FROM domain_phones WHERE domain=? ORDER BY seen_at", (domain.lower(),)
        ).fetchall()

    def add_phone(self, person_id: int, e164: str, kind: str, country: str, scope: str,
                  attribution: str, confidence: int, grade: str, status: str = "candidate",
                  source_url: str = "", context: str = "") -> int:
        row = self.conn.execute("SELECT id FROM phones WHERE person_id=? AND e164=?",
                                (person_id, e164)).fetchone()
        if row:
            self.conn.execute(
                "UPDATE phones SET kind=?, scope=?, attribution=?, confidence=?, grade=?,"
                " status=?, source_url=?, context=? WHERE id=?",
                (kind, scope, attribution, confidence, grade, status, source_url,
                 context, row["id"]))
            self.conn.commit()
            return int(row["id"])
        cur = self.conn.execute(
            "INSERT INTO phones(person_id, e164, kind, country, scope, attribution,"
            " confidence, grade, status, source_url, context, created_at)"
            " VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
            (person_id, e164, kind, country, scope, attribution, confidence, grade,
             status, source_url, context, _now()))
        self.conn.commit()
        return int(cur.lastrowid)

    # ------------------------------------------------------- enrichment history
    def record_lookup(self, subject_key: str, name_raw: str = "", work_email: str = "",
                      company: str = "", domain: str = "", status: str = "not_found",
                      reason: str = "", best_e164: str = "", best_score: int = 0,
                      best_source: str = "", candidates: int = 0, providers: str = "",
                      elapsed: float = 0.0) -> int:
        cur = self.conn.execute(
            "INSERT INTO phone_lookups(subject_key, name_raw, work_email, company, domain,"
            " status, reason, best_e164, best_score, best_source, candidates, providers,"
            " elapsed, ran_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (subject_key, name_raw, work_email.lower(), company, domain.lower(), status,
             reason, best_e164, int(best_score), best_source, int(candidates), providers,
             float(elapsed), _now()))
        self.conn.commit()
        return int(cur.lastrowid)

    def add_lookup_evidence(self, lookup_id: int, rows) -> None:
        """rows: iterable of (e164, provider, how, basis, source_url, context, owner)."""
        now = _now()
        with self.tx() as conn:
            conn.executemany(
                "INSERT INTO phone_lookup_evidence(lookup_id, e164, provider, how, basis,"
                " source_url, context, owner, seen_at) VALUES(?,?,?,?,?,?,?,?,?)",
                [(lookup_id, *row, now) for row in rows])

    def lookups_for(self, subject_key: str, limit: int = 10) -> list[sqlite3.Row]:
        return self.conn.execute(
            "SELECT * FROM phone_lookups WHERE subject_key=? ORDER BY ran_at DESC LIMIT ?",
            (subject_key, limit)).fetchall()

    def lookup_evidence(self, lookup_id: int) -> list[sqlite3.Row]:
        return self.conn.execute(
            "SELECT * FROM phone_lookup_evidence WHERE lookup_id=? ORDER BY id",
            (lookup_id,)).fetchall()

    # ---------------------------------------------------------- provider cache
    def get_provider_cache(self, provider: str, subject_key: str,
                           max_age: float | None = None):
        row = self.conn.execute(
            "SELECT payload, fetched_at FROM provider_cache WHERE provider=? AND subject_key=?",
            (provider, subject_key)).fetchone()
        if not row:
            return None
        ttl = config.PROVIDER_CACHE_TTL if max_age is None else max_age
        if ttl and (_now() - (row["fetched_at"] or 0)) > ttl:
            return None
        try:
            return json.loads(row["payload"])
        except ValueError:
            return None

    def put_provider_cache(self, provider: str, subject_key: str, payload) -> None:
        self.conn.execute(
            "INSERT INTO provider_cache(provider, subject_key, payload, fetched_at)"
            " VALUES(?,?,?,?) ON CONFLICT(provider, subject_key) DO UPDATE SET"
            " payload=excluded.payload, fetched_at=excluded.fetched_at",
            (provider, subject_key, json.dumps(payload), _now()))
        self.conn.commit()

    # ------------------------------------------------------------- forgetting
    def preview_forget(self, person: str = "", domain: str = "", email: str = "",
                       phone: str = "") -> dict:
        """What a forget() call would remove. Always run this first."""
        found: dict[str, list] = {}
        if person:
            from .names import parse as parse_name
            parsed = parse_name(person)
            rows = self.conn.execute(
                "SELECT id, name_raw FROM people WHERE first=? AND last=?",
                (parsed.first, parsed.last)).fetchall()
            found["people"] = [dict(r) for r in rows]
            ids = [r["id"] for r in rows]
            if ids:
                marks = ",".join("?" * len(ids))
                found["emails"] = [dict(r) for r in self.conn.execute(
                    f"SELECT address FROM emails WHERE person_id IN ({marks})", ids).fetchall()]
                found["phones"] = [dict(r) for r in self.conn.execute(
                    f"SELECT e164 FROM phones WHERE person_id IN ({marks})", ids).fetchall()]
        if domain:
            for table, column in [("pattern_samples", "local"), ("domain_emails", "address"),
                                  ("domain_phones", "e164"), ("domain_patterns", "template"),
                                  ("pinned_patterns", "template"), ("companies", "name")]:
                rows = self.conn.execute(
                    f"SELECT {column} FROM {table} WHERE domain=?", (domain.lower(),)).fetchall()
                if rows:
                    found[table] = [dict(r) for r in rows]
        if email:
            found["emails"] = [dict(r) for r in self.conn.execute(
                "SELECT address FROM emails WHERE address=?", (email.lower(),)).fetchall()]
            found["domain_emails"] = [dict(r) for r in self.conn.execute(
                "SELECT address FROM domain_emails WHERE address=?", (email.lower(),)).fetchall()]
        if phone:
            found["phones"] = [dict(r) for r in self.conn.execute(
                "SELECT e164 FROM phones WHERE e164=?", (phone,)).fetchall()]
            found["domain_phones"] = [dict(r) for r in self.conn.execute(
                "SELECT e164 FROM domain_phones WHERE e164=?", (phone,)).fetchall()]
            found["lookup_evidence"] = [dict(r) for r in self.conn.execute(
                "SELECT DISTINCT source_url FROM phone_lookup_evidence WHERE e164=?",
                (phone,)).fetchall()]
        for column, value in (("work_email", email), ("domain", domain)):
            if value:
                rows = self.conn.execute(
                    f"SELECT name_raw, status FROM phone_lookups WHERE {column}=?",
                    (value.lower(),)).fetchall()
                if rows:
                    found.setdefault("phone_lookups", []).extend(dict(r) for r in rows)
        if person:
            from .names import parse as parse_name
            parsed = parse_name(person)
            rows = self.conn.execute(
                "SELECT name_raw, status FROM phone_lookups WHERE subject_key LIKE ?",
                (f"{parsed.first}.{parsed.last}@%",)).fetchall()
            if rows:
                found.setdefault("phone_lookups", []).extend(dict(r) for r in rows)
        return {k: v for k, v in found.items() if v}

    def forget(self, person: str = "", domain: str = "", email: str = "",
               phone: str = "") -> int:
        """Permanently remove records. Returns the number of rows deleted."""
        removed = 0
        with self.tx() as conn:
            if person:
                from .names import parse as parse_name
                parsed = parse_name(person)
                ids = [r["id"] for r in conn.execute(
                    "SELECT id FROM people WHERE first=? AND last=?",
                    (parsed.first, parsed.last)).fetchall()]
                for pid in ids:
                    for table in ("emails", "phones"):
                        removed += conn.execute(
                            f"DELETE FROM {table} WHERE person_id=?", (pid,)).rowcount
                    removed += conn.execute(
                        "DELETE FROM observations WHERE person_id=?", (pid,)).rowcount
                    removed += conn.execute("DELETE FROM people WHERE id=?", (pid,)).rowcount
            if domain:
                key = domain.lower()
                for table in ("pattern_samples", "domain_patterns", "pinned_patterns",
                              "domain_emails", "domain_phones", "domains"):
                    removed += conn.execute(
                        f"DELETE FROM {table} WHERE domain=?", (key,)).rowcount
                removed += conn.execute("DELETE FROM emails WHERE domain=?", (key,)).rowcount
                # people.company_id references companies(id); detach before deleting
                # the company or the foreign key blocks it.
                conn.execute(
                    "UPDATE people SET company_id=NULL WHERE company_id IN"
                    " (SELECT id FROM companies WHERE domain=?)", (key,))
                removed += conn.execute("DELETE FROM companies WHERE domain=?", (key,)).rowcount
            if email:
                key = email.lower()
                removed += conn.execute("DELETE FROM emails WHERE address=?", (key,)).rowcount
                removed += conn.execute(
                    "DELETE FROM domain_emails WHERE address=?", (key,)).rowcount
                local, _, dom = key.partition("@")
                removed += conn.execute(
                    "DELETE FROM pattern_samples WHERE domain=? AND local=?",
                    (dom, local)).rowcount
            if phone:
                removed += conn.execute("DELETE FROM phones WHERE e164=?", (phone,)).rowcount
                removed += conn.execute(
                    "DELETE FROM domain_phones WHERE e164=?", (phone,)).rowcount
                removed += conn.execute(
                    "DELETE FROM phone_lookup_evidence WHERE e164=?", (phone,)).rowcount
                removed += conn.execute(
                    "UPDATE phone_lookups SET best_e164='' WHERE best_e164=?", (phone,)).rowcount
            # The lookup history is personal data in its own right - it records
            # who was searched for and what was concluded - so erasure has to
            # reach it too, or `forget` is a promise the tool does not keep.
            for column, value in (("work_email", email), ("domain", domain)):
                if value:
                    removed += _drop_lookups(conn, f"{column}=?", (value.lower(),))
            if person:
                from .names import parse as parse_name
                parsed = parse_name(person)
                removed += _drop_lookups(conn, "subject_key LIKE ?",
                                         (f"{parsed.first}.{parsed.last}@%",))
            if email:
                removed += conn.execute(
                    "DELETE FROM provider_cache WHERE subject_key LIKE ?",
                    (f"%@{email.lower().partition('@')[2]}",)).rowcount
        return removed

    # ------------------------------------------------------------------ misc
    def log_search(self, query: str, company: str, found: int, best: str) -> None:
        self.conn.execute(
            "INSERT INTO searches(query, company, found, best, ran_at) VALUES(?,?,?,?,?)",
            (query, company, found, best, _now()),
        )
        self.conn.commit()

    def stats(self) -> dict:
        one = lambda sql: int(self.conn.execute(sql).fetchone()[0])  # noqa: E731
        return {
            "people": one("SELECT COUNT(*) FROM people"),
            "companies": one("SELECT COUNT(*) FROM companies"),
            "domains_resolved": one("SELECT COUNT(*) FROM domains WHERE has_mx=1"),
            "emails": one("SELECT COUNT(*) FROM emails"),
            "emails_observed": one("SELECT COUNT(*) FROM emails WHERE status IN ('observed','confirmed')"),
            "pattern_samples": one("SELECT COUNT(*) FROM pattern_samples"),
            "pinned_patterns": one("SELECT COUNT(*) FROM pinned_patterns"),
            "domain_emails": one("SELECT COUNT(*) FROM domain_emails"),
            "phones": one("SELECT COUNT(*) FROM phones"),
            "phone_lookups": one("SELECT COUNT(*) FROM phone_lookups"),
            "phone_lookups_found": one(
                "SELECT COUNT(*) FROM phone_lookups WHERE status='found'"),
            "lookup_evidence": one("SELECT COUNT(*) FROM phone_lookup_evidence"),
            "domain_phones": one("SELECT COUNT(*) FROM domain_phones"),
            "pattern_domains": self.known_pattern_domains(),
            "observations": one("SELECT COUNT(*) FROM observations"),
            "searches": one("SELECT COUNT(*) FROM searches"),
            "db_path": str(self.path),
            "db_size_kb": round(self.path.stat().st_size / 1024, 1) if self.path.exists() else 0,
        }
