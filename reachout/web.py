"""A local web UI, served from the standard library.

Binds to 127.0.0.1 by default: this is your private database, not a service.

The browser is a second face on the same engine the CLI drives. Every route here
is a thin translation of a function in `engine`, `enrich`, `evaluate` or `db` -
no discovery logic lives in this file - so the two front ends cannot drift into
disagreeing about what the tool knows.
"""

from __future__ import annotations

import csv
import io
import json
import threading
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from . import config, engine, enrich as enrich_mod, evaluate, netdns, patterns
from . import phones as phonelib
from . import providers as providers_mod
from . import verify as verify_mod
from .cli import contact_pairs, result_to_dict
from .db import Database
from .names import parse as parse_name

UI_DIR = Path(__file__).resolve().parent / "ui"
STATIC = {"/": ("index.html", "text/html; charset=utf-8"),
          "/index.html": ("index.html", "text/html; charset=utf-8"),
          "/app.css": ("app.css", "text/css; charset=utf-8"),
          "/app.js": ("app.js", "text/javascript; charset=utf-8")}

# Large enough for a full address-book export, small enough that a stray request
# cannot exhaust memory.
MAX_BODY = 8 * 1024 * 1024


class Invalid(ValueError):
    """A request the user can fix by typing something different. Answered as 400."""


# ---------------------------------------------------------------- coercion
def _flag(get, key: str, default: bool = True) -> bool:
    raw = get(key, "1" if default else "0")
    return str(raw).lower() not in ("0", "false", "no", "off", "")


def _int(raw, default: int, low: int, high: int) -> int:
    try:
        return max(low, min(high, int(raw)))
    except (TypeError, ValueError):
        return default


def _domain(raw: str) -> str:
    """Accept anything domain-shaped: a bare domain, a URL, or an address."""
    value = (raw or "").strip().lower()
    if "//" in value:
        value = urllib.parse.urlsplit(value).netloc
    value = value.rsplit("@", 1)[-1].split("/")[0].strip().strip(".")
    return value


def _bare(host: str) -> str:
    return (host or "").strip().lower().strip("[]")


def _pattern_row(row, pinned: bool = False) -> dict:
    return {"template": row["template"], "example": patterns.describe(row["template"]),
            "confidence": float(row["confidence"]),
            "samples": int(row["samples"]) if "samples" in row.keys() else 0,
            "votes": int(row["votes"]) if "votes" in row.keys() else 0,
            "pinned": pinned, "note": row["note"] if "note" in row.keys() else ""}


# ------------------------------------------------------------------ reading
def _api_stats(get) -> dict:
    with Database() as db:
        return db.stats()


def _api_find(get) -> dict:
    name = get("name").strip()
    if not name:
        raise Invalid("give a name, or a LinkedIn URL")
    with Database() as db:
        result = engine.find(
            name, company_name=get("company").strip(), db=db,
            use_github=_flag(get, "github"), use_website=_flag(get, "web"),
            use_search=_flag(get, "search"), use_smtp=_flag(get, "smtp", False),
            limit=_int(get("limit"), 6, 1, 20),
            force_learn=_flag(get, "relearn", False),
            want_phones=_flag(get, "phone", False))
        payload = result_to_dict(result)
    payload["pattern_example"] = patterns.describe(result.pattern) if result.pattern else ""
    return payload


def _api_guess(get) -> dict:
    """Apply what is already known, offline. No network, no waiting."""
    name = parse_name(get("name"))
    domain = _domain(get("domain"))
    if not name.first or not domain:
        raise Invalid("give a name and a domain")
    with Database() as db:
        guesses = engine.load_patterns(domain, db)
        candidates = patterns.generate(name, domain, guesses,
                                       limit=_int(get("limit"), 8, 1, 25))
    return {"name": name.display, "domain": domain,
            "known": bool(guesses),
            "candidates": [{"address": c.address, "template": c.template,
                            "prior": c.prior, "source": c.source} for c in candidates]}


def _api_verify(get) -> dict:
    address = get("address").strip().lower()
    if "@" not in address:
        raise Invalid("give an email address to check")
    with Database() as db:
        verdict = verify_mod.verify(address, db=db, use_smtp=_flag(get, "smtp", False))
    return {"address": verdict.address, "syntax_ok": verdict.syntax_ok,
            "domain_resolves": verdict.domain_resolves, "has_mx": verdict.has_mx,
            "provider": verdict.provider, "is_role": verdict.is_role,
            "is_free": verdict.is_free, "is_disposable": verdict.is_disposable,
            "catch_all": verdict.catch_all, "smtp": verdict.smtp,
            "smtp_detail": verdict.smtp_detail, "notes": verdict.notes}


def _api_checkphone(get) -> dict:
    number = get("number").strip()
    if not number:
        raise Invalid("give a number to check")
    parsed = phonelib.normalise(number, default_region=(get("region") or "IN").upper())
    return {"input": number, "valid": parsed.valid, "e164": parsed.e164,
            "pretty": parsed.pretty, "country": parsed.country,
            "calling_code": parsed.calling_code, "kind": parsed.kind,
            "kind_certain": parsed.kind_certain, "area": parsed.area,
            "extension": parsed.extension, "notes": parsed.notes,
            "placeholder": phonelib.looks_placeholder(parsed) if parsed.valid else ""}


def _api_patterns(get) -> list:
    domain = _domain(get("domain"))
    if not domain:
        raise Invalid("give a domain")
    with Database() as db:
        return [_pattern_row(row, pinned=True) for row in db.pinned_patterns(domain)] \
            + [_pattern_row(row) for row in db.get_patterns(domain)]


def _api_domains(get) -> list:
    """Every domain the database knows anything about, best-known first."""
    needle = _domain(get("q"))
    index: dict[str, dict] = {}

    def slot(domain: str) -> dict:
        return index.setdefault(domain, {
            "domain": domain, "company": "", "samples": 0, "banked_emails": 0,
            "banked_phones": 0, "template": "", "example": "", "confidence": 0.0,
            "pinned": False, "has_mx": None, "provider": ""})

    with Database() as db:
        counts = [("pattern_samples", "samples"), ("domain_emails", "banked_emails"),
                  ("domain_phones", "banked_phones")]
        for table, key in counts:
            for row in db.conn.execute(
                    f"SELECT domain, COUNT(*) n FROM {table} GROUP BY domain"):
                slot(row["domain"])[key] = row["n"]
        for row in db.conn.execute(
                "SELECT name, domain FROM companies WHERE IFNULL(domain,'') != ''"):
            slot(row["domain"])["company"] = row["name"]
        for row in db.conn.execute(
                "SELECT domain, provider, has_mx FROM domains"):
            entry = slot(row["domain"])
            entry["provider"] = row["provider"] or ""
            entry["has_mx"] = bool(row["has_mx"])
        for row in db.conn.execute(
                "SELECT domain, template, confidence FROM domain_patterns ORDER BY confidence"):
            entry = slot(row["domain"])
            if not entry["pinned"]:
                entry.update(template=row["template"],
                             example=patterns.describe(row["template"]),
                             confidence=float(row["confidence"]))
        for row in db.conn.execute("SELECT domain, template, confidence FROM pinned_patterns"):
            slot(row["domain"]).update(template=row["template"],
                                       example=patterns.describe(row["template"]),
                                       confidence=float(row["confidence"]), pinned=True)

    rows = [row for row in index.values()
            if not needle or needle in row["domain"] or needle in row["company"].lower()]
    rows.sort(key=lambda r: (-r["samples"], -r["banked_emails"], r["domain"]))
    return rows


def _api_domain(get) -> dict:
    domain = _domain(get("domain"))
    if not domain:
        raise Invalid("give a domain")
    with Database() as db:
        facts = db.get_domain_facts(domain, max_age=float("inf")) or {}
        pattern_rows = [_pattern_row(row, pinned=True) for row in db.pinned_patterns(domain)] \
            + [_pattern_row(row) for row in db.get_patterns(domain)]
        samples = [{"first": r["first"], "last": r["last"], "local": r["local"],
                    "template": r["template"], "source_url": r["source_url"]}
                   for r in db.samples_for_domain(domain)]
        emails = [{"address": r["address"], "context": r["context"],
                   "source_url": r["source_url"], "is_role": bool(r["is_role"])}
                  for r in db.domain_emails(domain, include_role=True)]
        phones = [{"e164": r["e164"], "kind": r["kind"], "scope": r["scope"],
                   "owner": r["owner"], "context": r["context"],
                   "source_url": r["source_url"]} for r in db.domain_phones(domain)]
    return {"domain": domain, "facts": facts, "patterns": pattern_rows,
            "samples": samples, "emails": emails, "phones": phones}


PEOPLE_SQL = """
SELECT p.id, p.name_raw, p.title, p.linkedin, p.github, c.name AS company,
  (SELECT e.address    FROM emails e WHERE e.person_id=p.id
     ORDER BY e.confidence DESC LIMIT 1) AS address,
  (SELECT e.confidence FROM emails e WHERE e.person_id=p.id
     ORDER BY e.confidence DESC LIMIT 1) AS confidence,
  (SELECT e.grade      FROM emails e WHERE e.person_id=p.id
     ORDER BY e.confidence DESC LIMIT 1) AS grade,
  (SELECT e.status     FROM emails e WHERE e.person_id=p.id
     ORDER BY e.confidence DESC LIMIT 1) AS status,
  (SELECT COUNT(*)     FROM emails e WHERE e.person_id=p.id) AS candidates,
  (SELECT COUNT(*)     FROM phones ph WHERE ph.person_id=p.id) AS phones
FROM people p LEFT JOIN companies c ON c.id = p.company_id
WHERE ? = ''
   OR LOWER(p.name_raw) LIKE ?
   OR LOWER(IFNULL(c.name,'')) LIKE ?
   OR EXISTS (SELECT 1 FROM emails e WHERE e.person_id=p.id AND e.address LIKE ?)
ORDER BY p.updated_at DESC LIMIT ?
"""


def _api_people(get) -> list:
    needle = get("q").strip().lower()
    like = f"%{needle}%"
    with Database() as db:
        rows = db.conn.execute(
            PEOPLE_SQL, (needle, like, like, like, _int(get("limit"), 100, 1, 500))).fetchall()
    return [dict(row) for row in rows]


def _api_eval(get) -> dict:
    minimum = _int(get("min"), 3, 2, 100)
    domain = _domain(get("domain"))
    with Database() as db:
        reports = [evaluate.evaluate_domain(domain, db, minimum)] if domain \
            else evaluate.evaluate_all(db, minimum)
    reports = [r for r in reports if r]
    reports.sort(key=lambda r: -r.trials)
    trials = sum(r.trials for r in reports)
    return {
        "min_samples": minimum,
        "rows": [{"domain": r.domain, "trials": r.trials, "top1": r.top1, "top3": r.top3,
                  "top1_rate": r.top1_rate, "top3_rate": r.top3_rate,
                  "missed": [{"person": person, "actual": actual, "guessed": guessed}
                             for person, actual, guessed in r.missed[:12]]}
                 for r in reports],
        "totals": {"domains": len(reports), "trials": trials,
                   "top1_rate": sum(r.top1 for r in reports) / trials if trials else 0.0,
                   "top3_rate": sum(r.top3 for r in reports) / trials if trials else 0.0},
    }


def _api_providers(get) -> list:
    return providers_mod.describe()


def _api_doctor(get) -> dict:
    """What the CLI's `doctor` reports, as data."""
    from .sources import github, search as websearch

    backends = []
    for backend in websearch.chain():
        if not backend.configured():
            backends.append({"name": backend.name, "configured": False, "working": False,
                             "detail": f"set {backend.setup}"})
            continue
        working, detail = websearch.probe_backend(backend)
        backends.append({"name": backend.name, "configured": True, "working": working,
                         "detail": detail})
    try:
        limits = github.rate_limit()
        limits["error"] = ""
    except Exception as exc:                                 # noqa: BLE001 - reported, not raised
        limits = {"authenticated": False, "remaining": 0, "limit": 0, "error": str(exc)[:120]}

    return {"db_path": str(config.DB_PATH), "cache_dir": str(config.CACHE_DIR),
            "key_file": config.KEY_FILE, "respect_robots": config.RESPECT_ROBOTS,
            "host_delay": config.PER_HOST_DELAY, "smtp_enabled": config.SMTP_ENABLED,
            "search_backends": backends, "any_search": any(b["working"] for b in backends),
            "github": limits, "dns_ok": bool(netdns.mx("gmail.com"))}


# ------------------------------------------------------------------ writing
def _api_enrich(body: dict) -> dict:
    name = (body.get("name") or "").strip()
    if not name:
        raise Invalid("give a name")
    only = [p for p in (body.get("providers") or "").split(",") if p]
    exclude = [p for p in (body.get("without") or "").split(",") if p]
    try:
        with Database() as db:
            lookup = enrich_mod.enrich(
                name, (body.get("company") or "").strip(),
                (body.get("email") or "").strip(), db=db, only=only, exclude=exclude,
                use_search=bool(body.get("search", True)),
                region=(body.get("region") or "").strip().upper(),
                max_pages=_int(body.get("pages"), 6, 1, 30),
                search_pages=_int(body.get("search_pages"), 3, 1, 10))
    except ValueError as exc:
        raise Invalid(str(exc)) from exc
    return lookup.as_dict()


def _api_learn(body: dict) -> dict:
    domain = _domain(body.get("domain"))
    if not domain:
        raise Invalid("give a domain to harvest")
    with Database() as db:
        report = engine.learn_domain(
            domain, db, org_hint=(body.get("org") or "").strip(),
            max_repos=_int(body.get("repos"), 4, 1, 30),
            use_github=bool(body.get("github", True)),
            use_website=bool(body.get("web", True)),
            use_search=bool(body.get("search", True)))
        rows = [_pattern_row(row) for row in db.get_patterns(domain)]
    return {"domain": domain, "learned": report["learned"], "sources": report["sources"],
            "errors": report["errors"], "patterns": rows}


def _pairs_from_text(text: str) -> list:
    """Read (name, address) pairs out of pasted or uploaded CSV."""
    pairs = contact_pairs(list(csv.DictReader(io.StringIO(text))))
    if pairs:
        return pairs
    # A paste is often bare "Name, address" rows with no header at all.
    out = []
    for row in csv.reader(io.StringIO(text)):
        cells = [cell.strip() for cell in row if cell.strip()]
        address = next((c for c in cells if "@" in c and "." in c.rsplit("@", 1)[-1]), "")
        display = next((c for c in cells if c != address), "")
        if address and display:
            out.append((display, address.lower()))
    return out


def _teach_address(body: dict, db: Database) -> dict:
    domain = _domain(body.get("domain"))
    display = (body.get("name") or "").strip()
    value = (body.get("value") or "").strip()
    if not (domain and display and value):
        raise Invalid("give a domain, a full name, and an address or phone number")

    # The third field may be an address or a number; a number teaches no pattern.
    if "@" not in value:
        parsed = phonelib.normalise(value, default_region=(body.get("region") or "IN").upper())
        if not parsed.valid:
            raise Invalid(f"'{value}' is neither an email address nor a valid number: "
                          + "; ".join(parsed.notes[:2]))
        suspect = phonelib.looks_placeholder(parsed)
        if suspect and not body.get("force"):
            return {"kind": "phone", "recorded": False, "refused": suspect,
                    "pretty": parsed.pretty, "e164": parsed.e164}
        db.add_domain_phone(domain, parsed.e164, parsed.kind, "person", display,
                            f"{display} mobile", "you told me")
        return {"kind": "phone", "recorded": True, "refused": "", "pretty": parsed.pretty,
                "e164": parsed.e164, "country": parsed.country, "line": parsed.kind,
                "line_certain": parsed.kind_certain, "name": display, "domain": domain}

    outcome = engine.teach(domain, display, value, db)
    engine.refresh_pattern(domain, db)
    return {"kind": "email", "recorded": bool(outcome["banked"] or outcome["stored"]),
            "example": patterns.describe(outcome["template"]) if outcome["template"] else "",
            "patterns": [_pattern_row(row) for row in db.get_patterns(domain)], **outcome}


def _teach_pattern(body: dict, db: Database) -> dict:
    domain = _domain(body.get("domain"))
    template = (body.get("template") or "").strip()
    if not (domain and template):
        raise Invalid("give a domain and a template, e.g. {first}.{last}")
    probe = patterns.render(template, parse_name("Jane Ann Doe"))
    if not probe or not patterns.is_template(template):
        raise Invalid(f"'{template}' is not a usable template. "
                      "Tokens: {first} {last} {middle} {f} {l} {m}")
    confidence = max(0.01, min(1.0, float(body.get("confidence") or 0.9)))
    db.pin_pattern(domain, template, confidence, "stated by you")
    return {"kind": "pattern", "domain": domain, "template": template,
            "example": patterns.describe(template), "probe": f"{probe}@{domain}",
            "confidence": confidence,
            "patterns": [_pattern_row(row, pinned=True) for row in db.pinned_patterns(domain)]}


def _api_teach(body: dict) -> dict:
    mode = (body.get("mode") or "address").strip()
    with Database() as db:
        if mode == "address":
            return _teach_address(body, db)
        if mode == "pattern":
            return _teach_pattern(body, db)
        if mode == "unpin":
            domain = _domain(body.get("domain"))
            if not domain:
                raise Invalid("give a domain")
            return {"kind": "unpin", "domain": domain, "removed": db.unpin_pattern(domain)}
        if mode == "bulk":
            pairs = _pairs_from_text(body.get("text") or "")
            if not pairs:
                raise Invalid("no name/email pairs found — expected columns 'name' and 'email'")
            return {"kind": "bulk", **engine.bulk_teach(pairs, db)}
    raise Invalid(f"unknown teach mode {mode!r}")


def _api_forget(body: dict) -> dict:
    """Preview by default; delete only when the browser sends confirm."""
    kind = (body.get("kind") or "").strip()
    value = (body.get("value") or "").strip()
    if kind not in ("person", "domain", "email", "phone"):
        raise Invalid("say what kind of record to forget")
    if not value:
        raise Invalid("say what to forget")
    if kind == "domain":
        value = _domain(value)
    elif kind == "email":
        value = value.lower()
    elif kind == "phone":
        parsed = phonelib.normalise(value, default_region=(body.get("region") or "IN").upper())
        value = parsed.e164 if parsed.valid else value

    targets = {"person": "", "domain": "", "email": "", "phone": "", kind: value}
    with Database() as db:
        preview = db.preview_forget(**targets)
        found = {table: [", ".join(str(v) for v in row.values()) for row in rows]
                 for table, rows in preview.items()}
        total = sum(len(rows) for rows in preview.values())
        if not body.get("confirm"):
            return {"kind": kind, "value": value, "confirmed": False,
                    "found": found, "total": total}
        removed = db.forget(**targets) if preview else 0
    return {"kind": kind, "value": value, "confirmed": True, "found": found,
            "total": total, "removed": removed}


GET_ROUTES = {
    "/api/stats": _api_stats, "/api/find": _api_find, "/api/guess": _api_guess,
    "/api/verify": _api_verify, "/api/checkphone": _api_checkphone,
    "/api/patterns": _api_patterns, "/api/domains": _api_domains, "/api/domain": _api_domain,
    "/api/people": _api_people, "/api/eval": _api_eval, "/api/providers": _api_providers,
    "/api/doctor": _api_doctor,
}
POST_ROUTES = {
    "/api/enrich": _api_enrich, "/api/learn": _api_learn, "/api/teach": _api_teach,
    "/api/forget": _api_forget,
}
# Routes that go to the network. They share per-host delays and API rate limits
# with every other route, so exactly one of them runs at a time.
SERIALISED = {"/api/find", "/api/enrich", "/api/learn", "/api/doctor", "/api/verify"}


class Handler(BaseHTTPRequestHandler):
    server_version = "ReachOut"
    protocol_version = "HTTP/1.1"
    allowed_hosts = {"localhost", "127.0.0.1", "::1"}
    _lock = threading.Lock()

    def log_message(self, fmt, *args):  # quieter console
        return

    # ------------------------------------------------------------- responses
    def _send(self, code: int, body: bytes, ctype: str, extra: dict | None = None) -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Cache-Control", "no-store")
        for key, value in (extra or {}).items():
            self.send_header(key, value)
        self.end_headers()
        self.wfile.write(body)

    def _json(self, payload, code: int = 200) -> None:
        self._send(code, json.dumps(payload).encode("utf-8"),
                   "application/json; charset=utf-8")

    # -------------------------------------------------------------- guarding
    def _guard(self) -> bool:
        """Refuse anything that did not come from this machine's own browser.

        The Host check stops DNS rebinding - a page on the open web resolving its
        own name to 127.0.0.1 and reading your database through your browser.
        """
        host = _bare((self.headers.get("Host") or "").rsplit(":", 1)[0])
        if host and host not in self.allowed_hosts:
            self._refuse(421, "wrong host")
            return False
        origin = self.headers.get("Origin")
        if origin and _bare(urllib.parse.urlsplit(origin).hostname or "") \
                not in self.allowed_hosts:
            self._refuse(403, "cross-origin request refused")
            return False
        return True

    def _refuse(self, code: int, message: str) -> None:
        """Answer without reading the body, so close rather than keep alive.

        Unread request bytes would otherwise be parsed as the next request on a
        kept-alive connection.
        """
        self.close_connection = True
        self._send(code, message.encode("utf-8"), "text/plain; charset=utf-8")

    def _read_body(self) -> dict:
        length = _int(self.headers.get("Content-Length"), 0, 0, MAX_BODY + 1)
        if length > MAX_BODY:
            self.close_connection = True
            raise Invalid("that file is too large to import in one go")
        # Only JSON is accepted, which is what keeps a form on another site from
        # posting here: a cross-site form cannot set this content type, and a
        # cross-site fetch that does gets stopped by its own preflight.
        ctype = (self.headers.get("Content-Type") or "").split(";")[0].strip().lower()
        if ctype != "application/json":
            self.close_connection = True
            raise Invalid("expected application/json")
        try:
            return json.loads(self.rfile.read(length) or b"{}")
        except (ValueError, UnicodeDecodeError) as exc:
            raise Invalid(f"could not read the request: {exc}") from exc

    def _dispatch(self, path: str, call) -> None:
        try:
            if path in SERIALISED:
                with self._lock:
                    self._json(call())
            else:
                self._json(call())
        except Invalid as exc:
            self._json({"error": str(exc)}, 400)
        except Exception as exc:                             # noqa: BLE001 - shown, not swallowed
            self._json({"error": f"{type(exc).__name__}: {exc}"}, 500)

    # ---------------------------------------------------------------- routes
    def do_GET(self):  # noqa: N802
        if not self._guard():
            return
        parsed = urllib.parse.urlsplit(self.path)
        params = urllib.parse.parse_qs(parsed.query)
        get = lambda key, default="": (params.get(key) or [default])[0]  # noqa: E731

        if parsed.path in STATIC:
            filename, ctype = STATIC[parsed.path]
            try:
                self._send(200, (UI_DIR / filename).read_bytes(), ctype)
            except OSError:
                self._send(500, b"the UI files are missing", "text/plain; charset=utf-8")
            return

        if parsed.path == "/api/export":
            self._export(get)
            return

        route = GET_ROUTES.get(parsed.path)
        if not route:
            self._send(404, b"not found", "text/plain; charset=utf-8")
            return
        self._dispatch(parsed.path, lambda: route(get))

    def do_POST(self):  # noqa: N802
        if not self._guard():
            return
        path = urllib.parse.urlsplit(self.path).path
        route = POST_ROUTES.get(path)
        if not route:
            self._refuse(404, "not found")
            return
        try:
            body = self._read_body()
        except Invalid as exc:
            self._json({"error": str(exc)}, 400)
            return
        self._dispatch(path, lambda: route(body))

    def _export(self, get) -> None:
        """Everything found, as a file. The database is yours to take away."""
        fmt = "json" if get("format", "csv").lower() == "json" else "csv"
        minimum = _int(get("min"), 0, 0, 100)
        with Database() as db:
            rows = db.conn.execute("""
                SELECT p.name_raw, p.first, p.last, c.name AS company, e.address,
                       e.confidence, e.grade, e.status, e.pattern, p.linkedin, p.github
                FROM emails e JOIN people p ON p.id = e.person_id
                LEFT JOIN companies c ON c.id = p.company_id
                WHERE e.confidence >= ?
                ORDER BY e.confidence DESC
            """, (minimum,)).fetchall()

        if fmt == "json":
            body = json.dumps([dict(row) for row in rows], indent=2).encode("utf-8")
            ctype = "application/json; charset=utf-8"
        else:
            buffer = io.StringIO()
            writer = csv.writer(buffer)
            writer.writerow(["name", "first", "last", "company", "email", "confidence",
                             "grade", "status", "pattern", "linkedin", "github"])
            for row in rows:
                writer.writerow([row[key] for key in row.keys()])
            body = buffer.getvalue().encode("utf-8")
            ctype = "text/csv; charset=utf-8"
        self._send(200, body, ctype,
                   {"Content-Disposition": f'attachment; filename="reachout-contacts.{fmt}"'})


def serve(host: str = "127.0.0.1", port: int = 8765) -> None:
    config.ensure_dirs()
    Handler.allowed_hosts = {"localhost", "127.0.0.1", "::1", _bare(host)}
    ThreadingHTTPServer.daemon_threads = True
    httpd = ThreadingHTTPServer((host, port), Handler)
    shown = f"[{host}]" if ":" in host else host
    print(f"ReachOut UI  ->  http://{shown}:{port}")
    print(f"database        {config.DB_PATH}")
    if _bare(host) not in ("127.0.0.1", "localhost", "::1"):
        print("warning         bound beyond loopback - anyone who can reach this port "
              "can read and delete your database")
    print("ctrl-c to stop")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nstopped")
    finally:
        httpd.server_close()
