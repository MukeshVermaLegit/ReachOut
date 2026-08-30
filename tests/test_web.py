"""Offline tests for the local web UI - no network, no lookups.

These drive a real server on a loopback port against a throwaway database, so
they cover the routing, the guards and the JSON shaping that the browser
actually depends on. The routes that reach the network (find, learn, enrich,
doctor) are exercised through their coercion helpers only.
"""

import json
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from reachout import config, patterns, web                     # noqa: E402
from reachout.db import Database                               # noqa: E402


class TestCoercion(unittest.TestCase):
    def test_domain_accepts_anything_domain_shaped(self):
        for raw, want in [
            ("acme.com", "acme.com"), ("  ACME.com ", "acme.com"),
            ("https://acme.com/team", "acme.com"), ("jane@acme.com", "acme.com"),
            ("@acme.com", "acme.com"), ("acme.com/", "acme.com"), ("acme.com.", "acme.com"),
            ("", ""),
        ]:
            self.assertEqual(web._domain(raw), want, raw)

    def test_flags_default_and_override(self):
        params = {"a": "0", "b": "true", "c": "", "d": "off"}
        get = lambda key, default="": params.get(key, default)   # noqa: E731
        self.assertFalse(web._flag(get, "a"))
        self.assertTrue(web._flag(get, "b"))
        self.assertFalse(web._flag(get, "c"))
        self.assertFalse(web._flag(get, "d"))
        self.assertTrue(web._flag(get, "missing"))               # default True
        self.assertFalse(web._flag(get, "missing", False))

    def test_int_is_clamped_not_trusted(self):
        self.assertEqual(web._int("500", 6, 1, 20), 20)
        self.assertEqual(web._int("-9", 6, 1, 20), 1)
        self.assertEqual(web._int("nonsense", 6, 1, 20), 6)
        self.assertEqual(web._int(None, 6, 1, 20), 6)

    def test_bare_host(self):
        self.assertEqual(web._bare("[::1]"), "::1")
        self.assertEqual(web._bare(" LOCALHOST "), "localhost")


class TestTemplateValidation(unittest.TestCase):
    """A literal renders to itself, which would pin a whole company to one address."""

    def test_literal_is_not_a_template(self):
        self.assertFalse(patterns.is_template("nonsense"))
        self.assertFalse(patterns.is_template(""))
        self.assertTrue(patterns.is_template("{first}.{last}"))
        self.assertTrue(patterns.is_template("{f}{last}"))

    def test_render_alone_would_have_accepted_it(self):
        from reachout.names import parse
        self.assertEqual(patterns.render("nonsense", parse("Jane Doe")), "nonsense")


class TestPairsFromText(unittest.TestCase):
    def test_reads_a_headed_csv(self):
        pairs = web._pairs_from_text("Name,Email\nAlice Wong,alice.wong@acme.test\n")
        self.assertEqual(pairs, [("Alice Wong", "alice.wong@acme.test")])

    def test_reads_a_bare_paste_with_no_header(self):
        pairs = web._pairs_from_text("Erin Moss, erin.moss@w.test\nFrank Ng, frank.ng@w.test")
        self.assertEqual(len(pairs), 2)
        self.assertEqual(pairs[0], ("Erin Moss", "erin.moss@w.test"))

    def test_prose_yields_nothing(self):
        self.assertEqual(web._pairs_from_text("no addresses here at all"), [])


class ServerCase(unittest.TestCase):
    """A real server on a real socket, over a throwaway database."""

    @classmethod
    def setUpClass(cls):
        cls.home = Path(tempfile.mkdtemp())
        cls.saved_db = config.DB_PATH
        config.DB_PATH = cls.home / "t.db"
        Database(config.DB_PATH).close()

        web.Handler.allowed_hosts = {"localhost", "127.0.0.1", "::1"}
        cls.httpd = ThreadingHTTPServer(("127.0.0.1", 0), web.Handler)
        cls.base = f"http://127.0.0.1:{cls.httpd.server_address[1]}"
        cls.thread = threading.Thread(target=cls.httpd.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.httpd.server_close()
        config.DB_PATH = cls.saved_db

    # ------------------------------------------------------------- requesting
    def request(self, path, body=None, headers=None, method=None, raw=False):
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(self.base + path, data=data,
                                     method=method or ("POST" if data else "GET"))
        if data is not None:
            req.add_header("Content-Type", "application/json")
        for key, value in (headers or {}).items():
            req.add_header(key, value)
        try:
            with urllib.request.urlopen(req, timeout=20) as res:
                payload = res.read()
                return res.status, (payload if raw else json.loads(payload)), res.headers
        except urllib.error.HTTPError as err:
            payload = err.read()
            try:
                return err.code, json.loads(payload), err.headers
            except ValueError:
                return err.code, payload, err.headers

    def post(self, path, body, **kw):
        return self.request(path, body=body, **kw)


class TestServing(ServerCase):
    def test_serves_the_page_and_its_assets(self):
        for path, want in [("/", b"<title>ReachOut</title>"), ("/index.html", b"<!doctype html>"),
                           ("/app.css", b":root"), ("/app.js", b"async function api")]:
            code, body, headers = self.request(path, raw=True)
            self.assertEqual(code, 200, path)
            self.assertIn(want, body, path)
            self.assertEqual(headers["X-Content-Type-Options"], "nosniff")

    def test_unknown_paths_are_404(self):
        self.assertEqual(self.request("/nope", raw=True)[0], 404)
        self.assertEqual(self.post("/api/nope", {}, raw=True)[0], 404)

    def test_stats(self):
        code, data, _ = self.request("/api/stats")
        self.assertEqual(code, 200)
        self.assertEqual(data["db_path"], str(config.DB_PATH))
        self.assertIn("pattern_samples", data)


class TestGuards(ServerCase):
    """The database is private. Only this machine's own browser may reach it."""

    def test_a_rebound_hostname_is_refused(self):
        code, _, _ = self.request("/api/stats", headers={"Host": "evil.example"}, raw=True)
        self.assertEqual(code, 421)

    def test_a_cross_origin_page_is_refused(self):
        code, _, _ = self.request("/api/stats", headers={"Origin": "http://evil.example"},
                                  raw=True)
        self.assertEqual(code, 403)

    def test_our_own_origin_is_allowed(self):
        code, _, _ = self.request("/api/stats", headers={"Origin": "http://localhost:1"})
        self.assertEqual(code, 200)

    def test_a_cross_site_form_post_cannot_be_shaped(self):
        # A form on another site can only send these content types, and none of
        # them are accepted - which is what stops it deleting your records.
        req = urllib.request.Request(self.base + "/api/forget", data=b"kind=domain&value=x",
                                     method="POST")
        req.add_header("Content-Type", "application/x-www-form-urlencoded")
        with self.assertRaises(urllib.error.HTTPError) as caught:
            urllib.request.urlopen(req, timeout=10)
        self.assertEqual(caught.exception.code, 400)

    def test_a_refused_post_does_not_desync_the_connection(self):
        code, data, _ = self.post("/api/teach", {"mode": "nope"})
        self.assertEqual(code, 400)
        self.assertEqual(self.request("/api/stats")[0], 200)


class TestTeach(ServerCase):
    def test_one_real_address_teaches_a_format(self):
        code, data, _ = self.post("/api/teach", {
            "mode": "address", "domain": "green.test", "name": "Isha Rao",
            "value": "isha@green.test"})
        self.assertEqual(code, 200)
        self.assertTrue(data["banked"])
        self.assertEqual(data["template"], "{first}")
        self.assertTrue(data["patterns"])

        code, guess, _ = self.request("/api/guess?name=Harshit+Jain&domain=green.test")
        self.assertEqual(guess["candidates"][0]["address"], "harshit@green.test")
        self.assertTrue(guess["known"])

    def test_an_address_no_format_can_build_is_banked_but_teaches_nothing(self):
        _, data, _ = self.post("/api/teach", {
            "mode": "address", "domain": "green.test", "name": "Bob Smith",
            "value": "xyzzy@green.test"})
        self.assertTrue(data["banked"])
        self.assertFalse(data["stored"])
        self.assertIn("teaches no pattern", data["error"])

    def test_a_phone_is_recorded_but_never_inferred(self):
        _, data, _ = self.post("/api/teach", {
            "mode": "address", "domain": "green.test", "name": "Isha Rao",
            "value": "+91 99450 12345"})
        self.assertEqual(data["kind"], "phone")
        self.assertTrue(data["recorded"])
        self.assertEqual(data["e164"], "+919945012345")

    def test_a_placeholder_number_is_refused_not_stored(self):
        _, data, _ = self.post("/api/teach", {
            "mode": "address", "domain": "green.test", "name": "Isha Rao",
            "value": "+91 98765 43210"})
        self.assertFalse(data["recorded"])
        self.assertTrue(data["refused"])

    def test_pinning_and_unpinning_a_format(self):
        code, data, _ = self.post("/api/teach", {
            "mode": "pattern", "domain": "pin.test", "template": "{f}{last}",
            "confidence": 0.85})
        self.assertEqual(code, 200)
        self.assertEqual(data["probe"], "jdoe@pin.test")
        self.assertTrue(data["patterns"][0]["pinned"])

        _, dropped, _ = self.post("/api/teach", {"mode": "unpin", "domain": "pin.test"})
        self.assertEqual(dropped["removed"], 1)

    def test_a_literal_template_is_refused(self):
        code, data, _ = self.post("/api/teach", {
            "mode": "pattern", "domain": "pin.test", "template": "nonsense"})
        self.assertEqual(code, 400)
        self.assertIn("not a usable template", data["error"])

    def test_bulk_import_skips_consumer_addresses(self):
        code, data, _ = self.post("/api/teach", {"mode": "bulk", "text":
            "name,email\nAlice Wong,alice.wong@bulk.test\nBob Ray,bob.ray@bulk.test\n"
            "Carl Fox,carl.fox@bulk.test\nDee Lin,dee@gmail.com\n"})
        self.assertEqual(code, 200)
        self.assertEqual((data["contacts"], data["banked"], data["skipped"]), (4, 3, 1))
        self.assertEqual(data["learned"][0]["domain"], "bulk.test")

    def test_bulk_import_needs_something_to_read(self):
        code, data, _ = self.post("/api/teach", {"mode": "bulk", "text": "nothing here"})
        self.assertEqual(code, 400)
        self.assertIn("no name/email pairs", data["error"])

    def test_incomplete_input_says_what_is_missing(self):
        code, data, _ = self.post("/api/teach", {"mode": "address", "domain": "green.test"})
        self.assertEqual(code, 400)
        self.assertIn("full name", data["error"])


class TestBrowsing(ServerCase):
    def setUp(self):
        self.post("/api/teach", {"mode": "bulk", "text":
            "name,email\nAlice Wong,alice.wong@browse.test\nBob Ray,bob.ray@browse.test\n"
            "Carl Fox,carl.fox@browse.test\nDina Ek,dina.ek@browse.test\n"})

    def test_domain_index_counts_what_is_known(self):
        code, rows, _ = self.request("/api/domains")
        self.assertEqual(code, 200)
        row = next(r for r in rows if r["domain"] == "browse.test")
        self.assertEqual(row["samples"], 4)
        self.assertEqual(row["template"], "{first}.{last}")
        self.assertFalse(row["pinned"])

    def test_domain_index_filters(self):
        _, rows, _ = self.request("/api/domains?q=browse")
        self.assertTrue(rows)
        self.assertTrue(all("browse" in r["domain"] for r in rows))

    def test_domain_detail_shows_the_evidence(self):
        _, data, _ = self.request("/api/domain?domain=browse.test")
        self.assertEqual(len(data["samples"]), 4)
        self.assertEqual(len(data["emails"]), 4)
        self.assertTrue(data["patterns"][0]["example"])

    def test_patterns_route_lists_pinned_first(self):
        self.post("/api/teach", {"mode": "pattern", "domain": "browse.test",
                                 "template": "{last}"})
        _, rows, _ = self.request("/api/patterns?domain=browse.test")
        self.assertTrue(rows[0]["pinned"])
        self.post("/api/teach", {"mode": "unpin", "domain": "browse.test"})

    def test_eval_measures_only_what_it_has_enough_of(self):
        _, data, _ = self.request("/api/eval?min=3")
        row = next(r for r in data["rows"] if r["domain"] == "browse.test")
        self.assertEqual(row["trials"], 4)
        self.assertGreater(row["top1_rate"], 0.5)
        _, empty, _ = self.request("/api/eval?min=99")
        self.assertEqual(empty["rows"], [])

    def test_checkphone_is_offline_and_honest(self):
        _, data, _ = self.request("/api/checkphone?number=%2B919945012345")
        self.assertTrue(data["valid"])
        self.assertEqual(data["country"], "IN")
        _, bad, _ = self.request("/api/checkphone?number=12")
        self.assertFalse(bad["valid"])

    def test_export_hands_the_database_back(self):
        code, body, headers = self.request("/api/export?format=csv&min=0", raw=True)
        self.assertEqual(code, 200)
        self.assertIn("attachment", headers["Content-Disposition"])
        self.assertTrue(body.startswith(b"name,first,last,company,email"))
        _, as_json, _ = self.request("/api/export?format=json&min=0")
        self.assertIsInstance(as_json, list)


class TestForget(ServerCase):
    def test_preview_then_delete(self):
        self.post("/api/teach", {"mode": "address", "domain": "gone.test",
                                 "name": "Zoe Park", "value": "zoe.park@gone.test"})
        _, preview, _ = self.post("/api/forget", {"kind": "domain", "value": "gone.test"})
        self.assertFalse(preview["confirmed"])
        self.assertGreater(preview["total"], 0)
        self.assertIn("pattern_samples", preview["found"])

        # Still there: a preview must not delete anything.
        _, still, _ = self.request("/api/domain?domain=gone.test")
        self.assertEqual(len(still["samples"]), 1)

        _, done, _ = self.post("/api/forget", {"kind": "domain", "value": "gone.test",
                                               "confirm": True})
        self.assertTrue(done["confirmed"])
        self.assertGreater(done["removed"], 0)
        _, after, _ = self.request("/api/domain?domain=gone.test")
        self.assertEqual(after["samples"], [])

    def test_nothing_matching_removes_nothing(self):
        _, data, _ = self.post("/api/forget", {"kind": "person", "value": "Nobody At All"})
        self.assertEqual(data["total"], 0)
        self.assertEqual(data["found"], {})

    def test_an_unknown_kind_is_refused(self):
        code, data, _ = self.post("/api/forget", {"kind": "everything", "value": "x"})
        self.assertEqual(code, 400)

    def test_a_phone_is_normalised_before_matching(self):
        _, data, _ = self.post("/api/forget", {"kind": "phone", "value": "+91 99450 12345"})
        self.assertEqual(data["value"], "+919945012345")


class TestBadInput(ServerCase):
    def test_missing_arguments_are_answered_not_crashed(self):
        for path in ["/api/find", "/api/guess", "/api/verify?address=nope",
                     "/api/domain", "/api/patterns"]:
            code, data, _ = self.request(path)
            self.assertEqual(code, 400, path)
            self.assertIn("error", data)

    def test_unparseable_json_is_a_400(self):
        req = urllib.request.Request(self.base + "/api/teach", data=b"{not json",
                                     method="POST")
        req.add_header("Content-Type", "application/json")
        with self.assertRaises(urllib.error.HTTPError) as caught:
            urllib.request.urlopen(req, timeout=10)
        self.assertEqual(caught.exception.code, 400)


if __name__ == "__main__":
    unittest.main()
