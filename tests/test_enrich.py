"""Offline tests for phone enrichment - identity, providers, ranking, decision.

Everything here runs against the synthetic corpus in demo/, so there is no
network and no real person's details anywhere in the suite.
"""

import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from demo import synthetic                                      # noqa: E402
from demo.fake_web import FakeWeb, install                      # noqa: E402
from reachout import enrich as enrich_mod                       # noqa: E402
from reachout import identity, phonefind, providers             # noqa: E402
from reachout import phones as phonelib                         # noqa: E402
from reachout.db import Database                                # noqa: E402
from reachout.names import parse                                # noqa: E402
from reachout.sources.website import FoundPhone                 # noqa: E402

SUBJECT = dict(full_name="Amara Okafor", company="Northwind Analytics",
               work_email="amara.okafor@northwind.example", region="GB")


def subject(**over):
    return identity.build(**{**SUBJECT, **over})


class TestSubject(unittest.TestCase):
    def test_email_supplies_the_domain(self):
        s = subject()
        self.assertEqual(s.domain, "northwind.example")
        self.assertEqual(s.local, "amara.okafor")
        self.assertEqual(s.region, "GB")

    def test_name_and_address_agreeing_is_recorded(self):
        self.assertEqual(subject().name_email_template, "{first}.{last}")
        self.assertEqual(subject().problems, [])

    def test_address_that_cannot_come_from_the_name_is_flagged(self):
        s = subject(work_email="kt99@northwind.example")
        self.assertTrue(any("cannot be built from" in p for p in s.problems))
        # Flagged, not fatal: people do have addresses that ignore their name.
        self.assertTrue(s.email_valid)

    def test_role_mailbox_identifies_a_team_not_a_person(self):
        s = subject(work_email="info@northwind.example")
        self.assertTrue(s.email_is_role)
        self.assertFalse(s.email_is_distinctive)

    def test_short_or_numeric_local_parts_identify_nobody(self):
        self.assertFalse(subject(work_email="ir7@northwind.example").email_is_distinctive)
        self.assertFalse(subject(work_email="2021042@northwind.example").email_is_distinctive)

    def test_invalid_address_is_rejected_not_guessed_at(self):
        s = subject(work_email="not-an-address")
        self.assertFalse(s.email_valid)
        self.assertTrue(any("not a valid email" in p for p in s.problems))

    def test_region_falls_back_to_the_domain_when_not_stated(self):
        self.assertEqual(subject(work_email="a.b@example.co.uk", region="").region, "GB")


class TestMentions(unittest.TestCase):
    def test_own_address_identifies(self):
        found = subject().mentions("Amara Okafor amara.okafor@northwind.example 020 7946 0812")
        self.assertTrue(found.email_exact)
        self.assertTrue(found.identifies)
        self.assertEqual(found.strength, 1.0)

    def test_a_colleagues_address_is_counter_evidence(self):
        found = subject().mentions("Amara Okafor mentioned. priya.raman@northwind.example")
        self.assertEqual(found.rival_email, "priya.raman@northwind.example")
        self.assertFalse(found.identifies)

    def test_role_mailbox_is_not_a_colleague(self):
        found = subject().mentions("Reception info@northwind.example")
        self.assertEqual(found.rival_email, "")
        self.assertTrue(found.domain)

    def test_bare_name_does_not_identify_strongly(self):
        found = subject().mentions("Amara Okafor, ceramics studio, Leeds")
        self.assertTrue(found.identifies)          # nothing contradicts it...
        self.assertLess(found.strength, 0.7)       # ...but it is not an identifier

    def test_without_a_known_address_a_name_shaped_one_corroborates(self):
        """With no address given, one the subject's name would produce is theirs."""
        unknown = identity.build("Amara Okafor", "Northwind Analytics",
                                 domain="northwind.example", region="GB")
        found = unknown.mentions("Amara Okafor amara.okafor@northwind.example")
        self.assertTrue(found.email_local)
        self.assertEqual(found.rival_email, "")

    def test_without_a_known_address_a_colleagues_is_still_counter_evidence(self):
        unknown = identity.build("Amara Okafor", "Northwind Analytics",
                                 domain="northwind.example", region="GB")
        found = unknown.mentions("Amara Okafor priya.raman@northwind.example")
        self.assertEqual(found.rival_email, "priya.raman@northwind.example")

    def test_a_first_last_local_part_is_not_evidence_beyond_the_name(self):
        """'amara.okafor' slugs to the name itself, so it must not score twice."""
        found = subject().mentions("Amara Okafor, ceramics studio, Leeds")
        self.assertFalse(found.email_local)

    def test_unrelated_text_matches_nothing(self):
        self.assertEqual(subject().mentions("Quarterly results webinar").strength, 0.0)


class TestEmailAttribution(unittest.TestCase):
    def _attr(self, found, **kw):
        return phonefind.attribute(found, parse("Amara Okafor"),
                                   phonelib.normalise(found.raw, default_region="GB"),
                                   subject=subject(), **kw)

    def test_address_in_a_structural_entry_attributes_strongly(self):
        found = FoundPhone(raw="+442079460812", how="directory", scope="person",
                           context="Amara Okafor Head of Data Platform",
                           emails=["amara.okafor@northwind.example"],
                           source_url="https://northwind.example/about/people")
        attribution, score = self._attr(found)
        self.assertEqual(attribution, phonefind.PERSON)
        self.assertGreaterEqual(score.value, 80)

    def test_a_block_holding_two_peoples_addresses_attributes_nothing(self):
        """The neighbour trap: a text window spanning two directory rows."""
        found = FoundPhone(
            raw="+442079460813", how="text",
            context=("Amara Okafor amara.okafor@northwind.example 020 7946 0812 "
                     "Priya Raman priya.raman@northwind.example 020 7946 0813"),
            source_url="https://northwind.example/about/people")
        attribution, score = self._attr(found)
        self.assertNotEqual(attribution, phonefind.PERSON)
        self.assertTrue(any("spans more than one person's entry" in c for c in score.caps),
                        score.caps)

    def test_a_page_with_no_link_to_the_employer_cannot_reach_the_bar(self):
        """Same name, different person: the namesake trap."""
        found = FoundPhone(
            raw="+447700900654", how="text",
            context="Amara Okafor. Hand-thrown stoneware. Call the studio on 07700 900654",
            source_url="https://amara-okafor.example")
        attribution, score = self._attr(found)
        self.assertEqual(attribution, phonefind.PERSON)   # it is a candidate...
        self.assertLess(score.value, enrich_mod.ACCEPT)   # ...but never an answer
        self.assertTrue(any(s.key == "no_employer_link" for s in score.signals))

    def test_the_employers_own_site_supplies_the_link_by_itself(self):
        found = FoundPhone(raw="+442079460812", how="text",
                           context="Amara Okafor direct line 020 7946 0812",
                           source_url="https://northwind.example/about/people")
        _attribution, score = self._attr(found)
        self.assertFalse(any(s.key == "no_employer_link" for s in score.signals))

    def test_corroboration_only_counts_independent_hosts(self):
        found = FoundPhone(raw="+447700900412", how="directory", scope="person",
                           context="Amara Okafor", emails=["amara.okafor@northwind.example"],
                           source_url="https://dataday.example/speakers/2026")
        _a, one = self._attr(found, corroborating_hosts=1)
        _b, two = self._attr(found, corroborating_hosts=2)
        self.assertFalse(any(s.key == "corroborated" for s in one.signals))
        self.assertTrue(any(s.key == "corroborated" for s in two.signals))

    def test_every_attributing_signal_names_its_url(self):
        found = FoundPhone(raw="+442079460812", how="directory", scope="person",
                           context="Amara Okafor", emails=["amara.okafor@northwind.example"],
                           source_url="https://northwind.example/about/people")
        _attribution, score = self._attr(found)
        top = max(score.signals, key=lambda s: s.points)
        self.assertEqual(top.source_url, "https://northwind.example/about/people")


class TestRanking(unittest.TestCase):
    def _rank(self, sightings, who="Amara Okafor"):
        return phonefind.rank(sightings, parse(who), region="GB", subject=subject())

    def test_the_same_number_written_three_ways_is_one_candidate(self):
        url = "https://northwind.example/about/people"
        sightings = [
            FoundPhone(raw="020 7946 0812", how="text", context="Amara Okafor", source_url=url),
            FoundPhone(raw="+44 20 7946 0812", how="tel-link", context="Amara Okafor",
                       source_url=url),
            FoundPhone(raw="+442079460812", how="directory", owner="Amara Okafor",
                       scope="person", context="Amara Okafor", source_url=url),
        ]
        personal, _company, checked = self._rank(sightings)
        self.assertEqual(len(personal), 1)
        self.assertEqual(personal[0].e164, "+442079460812")
        self.assertEqual(len(personal[0].evidence), 3)
        self.assertEqual(checked, 3)

    def test_independent_hosts_are_counted_not_pages(self):
        sightings = [
            FoundPhone(raw="+447700900412", how="directory", owner="Amara Okafor",
                       scope="person", source_url="https://northwind.example/a"),
            FoundPhone(raw="+447700900412", how="text", context="Amara Okafor",
                       source_url="https://northwind.example/b"),
            FoundPhone(raw="+447700900412", how="text", context="Amara Okafor",
                       source_url="https://dataday.example/speakers/2026"),
        ]
        personal, _company, _checked = self._rank(sightings)
        self.assertEqual(len(personal[0].hosts), 2)

    def test_a_number_bound_to_someone_else_is_dropped_entirely(self):
        """Data minimisation: a colleague's direct dial must not be retained."""
        sightings = [
            FoundPhone(raw="+442079460813", how="directory", owner="Priya Raman",
                       scope="person", source_url="https://northwind.example/about/people"),
        ]
        personal, company, _checked = self._rank(sightings)
        self.assertEqual(personal, [])
        self.assertEqual(company, [])

    def test_a_switchboard_is_kept_but_never_personal(self):
        sightings = [FoundPhone(raw="+442079460800", how="json-ld", scope="organization",
                                context="reception", source_url="https://northwind.example/contact")]
        personal, company, _checked = self._rank(sightings)
        self.assertEqual(personal, [])
        self.assertEqual(company[0].attribution, phonefind.COMPANY)

    def test_the_best_reading_of_a_number_wins_not_the_first_seen(self):
        url = "https://dataday.example/speakers/2026"
        weak = FoundPhone(raw="+447700900412", how="search", context="... 07700 900412",
                          source_url=url)
        strong = FoundPhone(raw="+447700900412", how="directory", owner="Amara Okafor",
                            scope="person", emails=["amara.okafor@northwind.example"],
                            context="Amara Okafor", source_url=url)
        personal, _c, _n = self._rank([weak, strong])
        self.assertGreaterEqual(personal[0].confidence, 80)


class TestProviders(unittest.TestCase):
    def tearDown(self):
        providers.reset_registry()

    def test_every_provider_states_a_basis(self):
        for provider in providers.registry():
            self.assertTrue(provider.basis, f"{provider.name} has no stated basis")

    def test_licensed_providers_are_off_until_configured(self):
        example = next(p for p in providers.registry() if p.name == "example-authorized")
        blank = providers.LookupContext(subject=subject())
        self.assertIn("not configured", example.unavailable(blank))

    def test_a_custom_provider_can_be_registered(self):
        class Fake(providers.Provider):
            name = "fake"
            basis = "test double"

            def fetch(self, ctx):
                return self.result(candidates=[FoundPhone(
                    raw="+447700900999", source_url="https://example.test/x")])

        providers.register(Fake())
        self.assertIn("fake", [p.name for p in providers.registry()])
        found, runs, _dropped = enrich_mod.gather(subject(), only=["fake"])
        self.assertEqual(len(found), 1)
        self.assertEqual(found[0].provider, "fake")
        self.assertEqual(runs[0].state, "ok")

    def test_a_provider_that_raises_does_not_end_the_lookup(self):
        class Broken(providers.Provider):
            name = "broken"
            basis = "test double"

            def fetch(self, ctx):
                raise RuntimeError("upstream is down")

        providers.register(Broken())
        _found, runs, _dropped = enrich_mod.gather(subject(), only=["broken"])
        self.assertEqual(runs[0].state, "error")
        self.assertIn("upstream is down", runs[0].error)

    def test_a_candidate_with_no_source_url_is_discarded(self):
        """Nothing is ever reported that a human cannot go and check."""
        class Sourceless(providers.Provider):
            name = "sourceless"
            basis = "test double"

            def fetch(self, ctx):
                return self.result(candidates=[FoundPhone(raw="+447700900111")])

        providers.register(Sourceless())
        found, _runs, dropped = enrich_mod.gather(subject(), only=["sourceless"])
        self.assertEqual(found, [])
        self.assertEqual(dropped, 1)

    def test_an_unknown_provider_name_is_an_error_not_a_silent_skip(self):
        with self.assertRaises(ValueError):
            providers.select(only=["no-such-provider"])

    def test_deny_list_removes_a_provider(self):
        chosen = [p.name for p in providers.select(exclude=["public-pages"])]
        self.assertNotIn("public-pages", chosen)


class TestExcludedSources(unittest.TestCase):
    """Breach dumps and broker datasets must not reach the ranking pool.

    The module docstring in providers.py promises these are excluded. Before
    these guards that promise was kept only by the scorer refusing to promote
    them - which is a different thing from never reading them.
    """

    def test_a_broker_is_refused_snippet_and_all(self):
        self.assertIn("data broker", providers.excluded("https://www.rocketreach.co/x"))
        self.assertIn("data broker", providers.excluded("https://spokeo.com/y"))

    def test_a_pdf_mirror_is_refused(self):
        barred = providers.excluded("https://kupdf.net/download/hni-email-data_58fa")
        self.assertIn("scraped dump", barred)

    def test_an_ordinary_host_is_not_refused(self):
        self.assertEqual(providers.excluded("https://dataday.example/speakers/2026"), "")
        self.assertEqual(providers.excluded("https://cam.ac.uk/staff/jsmith"), "")

    def test_a_social_network_keeps_its_snippet(self):
        """The two rules are different and must stay different."""
        url = "https://www.linkedin.com/in/someone"
        # Terms forbid the fetch...
        self.assertIn("forbids automated access", providers.fetchable(url))
        # ...but the snippet is public search data and stays usable as a lead.
        self.assertEqual(providers.excluded(url), "")

    def test_a_dump_of_unbound_numbers_is_a_bulk_list(self):
        dump = [FoundPhone(raw=f"+4479009004{n:02d}", context="", owner="",
                           scope="page", how="text", source_url="https://x.test/d")
                for n in range(57)]
        self.assertTrue(providers.bulk_list(dump))

    def test_a_real_staff_directory_is_not_a_bulk_list(self):
        """Volume is not the signal - a directory binds its rows to people."""
        directory = [FoundPhone(raw=f"+442079460{n:03d}", context="", owner=f"Person {n}",
                                scope="person", how="directory",
                                source_url="https://uni.test/staff")
                     for n in range(200)]
        self.assertFalse(providers.bulk_list(directory))

    def test_a_handful_of_unbound_numbers_is_still_fine(self):
        """A contact page has a few numbers and binds none. That is normal."""
        page = [FoundPhone(raw="+442079460800", context="", owner="", scope="page",
                           how="tel-link", source_url="https://acme.test/contact")
                for _ in range(4)]
        self.assertFalse(providers.bulk_list(page))

    def test_a_mostly_bound_directory_survives_a_few_stray_numbers(self):
        rows = [FoundPhone(raw=f"+442079460{n:03d}", context="", owner=f"Person {n}",
                           scope="person", how="directory", source_url="https://u.test/s")
                for n in range(30)]
        strays = [FoundPhone(raw="+442079460999", context="", owner="", scope="page",
                             how="text", source_url="https://u.test/s") for _ in range(5)]
        self.assertFalse(providers.bulk_list(rows + strays))


class TestSearchOutage(unittest.TestCase):
    """A blocked search engine must never read as "nothing is published"."""

    def setUp(self):
        from reachout.sources import search as websearch
        self.websearch = websearch
        self.saved = dict(websearch._STATE)

    def tearDown(self):
        self.websearch._STATE.update(self.saved)
        self.websearch.reset_backends()
        providers.reset_registry()

    def test_a_bot_check_page_is_an_outage_not_an_empty_result(self):
        from reachout.sources import search as websearch
        challenge = "<html><body>Please verify you are human</body></html>"
        saved = websearch.get
        websearch.get = lambda *a, **k: type("R", (), {
            "ok": True, "text": challenge, "status": 202, "error": ""})()
        try:
            with self.assertRaises(websearch.SearchUnavailable):
                websearch._duckduckgo("anything", 5)
        finally:
            websearch.get = saved

    def test_a_real_zero_result_page_is_an_empty_list(self):
        from reachout.sources import search as websearch
        empty = "<html><body>No results found for that query.</body></html>"
        saved = websearch.get
        websearch.get = lambda *a, **k: type("R", (), {
            "ok": True, "text": empty, "status": 200, "error": ""})()
        try:
            self.assertEqual(websearch._duckduckgo("anything", 5), [])
        finally:
            websearch.get = saved

    def test_a_202_response_is_not_treated_as_a_failure(self):
        from reachout.fetch import Response
        self.assertTrue(Response(url="u", status=202, text="body").ok)
        self.assertFalse(Response(url="u", status=500, text="body").ok)

    def test_the_chain_fails_over_to_the_next_backend(self):
        """Brave hitting its per-second limit must not cost the query."""
        from reachout.sources import search as websearch
        calls = []

        def brave(query, count):
            calls.append("brave")
            raise websearch.BackendFailed("rate limited", cool_off=2.0)

        def google(query, count):
            calls.append("google")
            return [websearch.Hit("t", "https://example.test/a", "s", "google")]

        chain = websearch.CHAIN
        websearch.CHAIN = [
            websearch.Backend("brave", brave), websearch.Backend("google", google)]
        try:
            hits = websearch.search("anything", 5)
            self.assertEqual(calls, ["brave", "google"])
            self.assertEqual(len(hits), 1)
            # ...and the exhausted one is not asked again while it is resting.
            websearch.search("another", 5)
            self.assertEqual(calls, ["brave", "google", "google"])
        finally:
            websearch.CHAIN = chain

    def test_every_backend_down_is_an_outage_not_an_empty_result(self):
        from reachout.sources import search as websearch

        def dead(query, count):
            raise websearch.BackendFailed("down", cool_off=1.0)

        chain = websearch.CHAIN
        websearch.CHAIN = [websearch.Backend("a", dead), websearch.Backend("b", dead)]
        try:
            with self.assertRaises(websearch.SearchUnavailable):
                websearch.search("anything", 5)
        finally:
            websearch.CHAIN = chain

    def test_a_rejected_credential_is_not_retried_all_run(self):
        from reachout.sources import search as websearch
        calls = []

        def bad_key(query, count):
            calls.append(1)
            raise websearch.BackendFailed("bad key", permanent=True)

        chain = websearch.CHAIN
        websearch.CHAIN = [websearch.Backend("keyed", bad_key)]
        try:
            for _ in range(3):
                try:
                    websearch.search("anything", 5)
                except websearch.SearchUnavailable:
                    pass
            self.assertEqual(len(calls), 1)
        finally:
            websearch.CHAIN = chain

    def test_a_working_backend_returning_zero_is_a_real_zero(self):
        """Don't spend a second backend's quota making 'nothing' ambiguous."""
        from reachout.sources import search as websearch
        calls = []
        chain = websearch.CHAIN
        websearch.CHAIN = [
            websearch.Backend("first", lambda q, c: calls.append("first") or []),
            websearch.Backend("second", lambda q, c: calls.append("second") or []),
        ]
        try:
            self.assertEqual(websearch.search("anything", 5), [])
            self.assertEqual(calls, ["first"])
        finally:
            websearch.CHAIN = chain

    # ------------------------------------------------------------ new backends
    def _fake_response(self, status=200, text="", error=""):
        return type("R", (), {
            "ok": 200 <= status < 300 and bool(text), "status": status,
            "text": text, "error": error})()

    def test_serper_reads_the_organic_results(self):
        from reachout.sources import search as websearch
        import json as _json
        body = _json.dumps({"organic": [
            {"title": "Amara Okafor", "link": "https://example.test/bio",
             "snippet": "Direct line 020 7946 0000"},
            {"title": "no link here", "snippet": "dropped"},
        ]})
        saved = websearch.get
        websearch.get = lambda *a, **k: self._fake_response(text=body)
        try:
            hits = websearch._serper("anything", 8)
        finally:
            websearch.get = saved
        # The entry with no URL is dropped: a hit nobody can open is not a hit.
        self.assertEqual(len(hits), 1)
        self.assertEqual(hits[0].url, "https://example.test/bio")
        self.assertEqual(hits[0].source, "serper")
        self.assertIn("020 7946 0000", hits[0].snippet)

    def test_serper_sends_the_query_as_a_post_body(self):
        """Operators only survive if the query reaches Google intact."""
        from reachout.sources import search as websearch
        import json as _json
        seen = {}

        def capture(url, **kw):
            seen.update(url=url, **kw)
            return self._fake_response(text='{"organic": []}')

        saved = websearch.get
        websearch.get = capture
        try:
            websearch._serper('site:acme.test "Amara Okafor" (phone OR contact)', 8)
        finally:
            websearch.get = saved
        sent = _json.loads(seen["data"].decode("utf-8"))
        self.assertEqual(sent["q"], 'site:acme.test "Amara Okafor" (phone OR contact)')
        # Over ten results costs a second credit, and nothing here asks for more.
        self.assertLessEqual(sent["num"], 10)

    def test_serper_out_of_credit_is_permanent_not_a_wait(self):
        """Prepaid means retrying cannot help - only a top-up can."""
        from reachout.sources import search as websearch
        saved = websearch.get
        websearch.get = lambda *a, **k: self._fake_response(
            status=403, text='{"message": "Not enough credits"}')
        try:
            with self.assertRaises(websearch.BackendFailed) as caught:
                websearch._serper("anything", 8)
        finally:
            websearch.get = saved
        self.assertTrue(caught.exception.permanent)
        self.assertIn("credit", str(caught.exception).lower())

    def test_searxng_empty_with_engines_down_is_an_outage(self):
        """SearXNG answers 200 with no results when Google CAPTCHAs it."""
        from reachout.sources import search as websearch
        saved = websearch.get
        websearch.get = lambda *a, **k: self._fake_response(
            text='{"results": [], "unresponsive_engines": [["google", "CAPTCHA"]]}')
        try:
            with self.assertRaises(websearch.BackendFailed) as caught:
                websearch._searxng("anything", 8)
        finally:
            websearch.get = saved
        self.assertIn("google", str(caught.exception))

    def test_searxng_empty_with_healthy_engines_is_a_real_zero(self):
        from reachout.sources import search as websearch
        saved = websearch.get
        websearch.get = lambda *a, **k: self._fake_response(
            text='{"results": [], "unresponsive_engines": []}')
        try:
            self.assertEqual(websearch._searxng("anything", 8), [])
        finally:
            websearch.get = saved

    def test_searxng_reads_content_as_the_snippet(self):
        from reachout.sources import search as websearch
        saved = websearch.get
        websearch.get = lambda *a, **k: self._fake_response(
            text='{"results": [{"url": "https://example.test/a", "title": "T",'
                 ' "content": "call 020 7946 0000"}]}')
        try:
            hits = websearch._searxng("anything", 8)
        finally:
            websearch.get = saved
        self.assertEqual(hits[0].snippet, "call 020 7946 0000")
        self.assertEqual(hits[0].source, "searxng")

    def test_post_bodies_do_not_share_a_cache_entry(self):
        """Every Serper query is the same URL; only the body differs."""
        from reachout import fetch
        url = "https://google.serper.dev/search"
        first = fetch._cache_key(url, b'{"q": "amara"}')
        second = fetch._cache_key(url, b'{"q": "someone else"}')
        self.assertNotEqual(first, second)
        # A plain GET must keep keying on the URL alone, or every cached page
        # written before this change is orphaned.
        self.assertEqual(fetch._cache_key(url, None), url)

    def test_a_restricted_index_finding_nothing_does_not_end_the_chain(self):
        """'Not on those 30 sites' is not 'not on the web'."""
        from reachout.sources import search as websearch
        calls = []
        restricted = websearch.Backend(
            "restricted", lambda q, c: calls.append("restricted") or [],
            authoritative=False)
        whole = websearch.Backend("whole", lambda q, c: calls.append("whole") or [
            websearch.Hit("t", "https://example.test/a", "s", "whole")])
        chain = websearch.CHAIN
        websearch.CHAIN = [restricted, whole]
        try:
            hits = websearch.search("anything", 5)
            self.assertEqual(calls, ["restricted", "whole"])
            self.assertEqual(len(hits), 1)
        finally:
            websearch.CHAIN = chain

    def test_every_index_answering_nothing_is_empty_not_blocked(self):
        from reachout.sources import search as websearch
        chain = websearch.CHAIN
        websearch.CHAIN = [
            websearch.Backend("restricted", lambda q, c: [], authoritative=False),
            websearch.Backend("whole", lambda q, c: []),
        ]
        try:
            self.assertEqual(websearch.search("anything", 5), [])
            self.assertEqual(websearch.status(), "")
        finally:
            websearch.CHAIN = chain

    def test_google_defaults_to_supplementary_not_authoritative(self):
        """Assuming restricted is safe; assuming whole-web silently narrows search."""
        from reachout.sources import search as websearch
        google = next(b for b in websearch.chain() if b.name == "google")
        self.assertFalse(google.authoritative)

    def test_a_restricted_engine_probes_as_usable_not_broken(self):
        from reachout.sources import search as websearch
        backend = websearch.Backend("google", lambda q, c: [], authoritative=False)
        backend.configured = lambda: True
        working, detail = websearch.probe_backend(backend)
        self.assertTrue(working)
        self.assertIn("supplements the chain", detail)

    def test_a_google_engine_claiming_whole_web_but_finding_nothing_is_flagged(self):
        from reachout.sources import search as websearch
        backend = websearch.Backend("google", lambda q, c: [], authoritative=True)
        backend.configured = lambda: True
        working, detail = websearch.probe_backend(backend)
        self.assertFalse(working)
        self.assertIn("restricted to specific sites", detail)

    def test_sites_that_forbid_automated_access_are_never_fetched(self):
        """The project promises not to scrape LinkedIn; the snippet is still a lead."""
        self.assertIn("forbids automated access",
                      providers.fetchable("https://www.linkedin.com/in/someone"))
        self.assertEqual(providers.fetchable("https://muckrack.com/someone"), "")

    def test_a_search_dependent_provider_reports_that_it_ran_blind(self):
        from reachout.sources import search as websearch

        class Blocked(providers.Provider):
            name = "blocked-source"
            basis = "test double"
            uses_search = True

            def fetch(self, ctx):
                raise websearch.SearchUnavailable("engine is rate-limiting")

        providers.register(Blocked())
        _found, runs, _dropped = enrich_mod.gather(subject(), only=["blocked-source"])
        self.assertEqual(runs[0].state, "degraded")
        self.assertIn("rate-limiting", runs[0].degraded)

    def test_not_found_says_so_when_sources_could_not_look(self):
        blind = enrich_mod.ProviderRun(name="public-pages", degraded="engine refused")
        working = enrich_mod.ProviderRun(name="company-website")
        status, reason = enrich_mod.decide([], subject(), [working, blind])
        self.assertEqual(status, enrich_mod.NOT_FOUND)
        self.assertIn("could not do their job", reason)
        self.assertIn("public-pages", reason)
        self.assertNotIn("normal outcome", reason)

    def test_not_found_with_every_source_working_is_a_real_finding(self):
        runs = [enrich_mod.ProviderRun(name="company-website"),
                enrich_mod.ProviderRun(name="public-pages")]
        _status, reason = enrich_mod.decide([], subject(), runs)
        self.assertIn("normal outcome", reason)

    def test_a_javascript_only_site_is_unread_not_empty(self):
        """greenflow.ventures serves a shell: 'could not read' beats 'nothing there'."""
        from reachout.sources import website as website_mod
        provider = next(p for p in providers.registry() if p.name == "company-website")
        saved = website_mod.harvest_site
        website_mod.harvest_site = lambda *a, **k: {
            "pages": ["https://spa.example"], "phones": [], "phone_pages": {},
            "emails": {}, "names": [], "errors": [], "client_rendered": True}
        try:
            result = provider.run(providers.LookupContext(
                subject=identity.build("A B", "SPA", domain="spa.example")))
        finally:
            website_mod.harvest_site = saved
        self.assertIn("JavaScript shell", result.degraded)

    def test_coverage_counts_only_sources_that_worked(self):
        lookup = enrich_mod.PhoneLookup(subject=subject(), providers=[
            enrich_mod.ProviderRun(name="a"),
            enrich_mod.ProviderRun(name="b", degraded="blocked"),
            enrich_mod.ProviderRun(name="c", skipped="not configured"),
        ])
        self.assertEqual(lookup.coverage, "1/2 sources")
        self.assertEqual([r.name for r in lookup.blind_spots], ["b"])


class TestDecision(unittest.TestCase):
    def _candidate(self, value, how="directory", hosts=1):
        found = FoundPhone(raw="+447700900412", how=how,
                           source_url="https://dataday.example/speakers/2026")
        result = phonefind.PhoneResult(e164="+447700900412", attribution=phonefind.PERSON)
        result.score.value = value
        result.evidence = [phonefind.Evidence(source_url=f"https://host{i}.example/p",
                                              how=how) for i in range(hosts)]
        return result

    def test_nothing_found_is_not_found(self):
        status, reason = enrich_mod.decide([], subject())
        self.assertEqual(status, enrich_mod.NOT_FOUND)
        self.assertIn("normal outcome", reason)

    def test_a_weak_association_is_not_offered_at_all(self):
        status, _reason = enrich_mod.decide([self._candidate(18)], subject())
        self.assertEqual(status, enrich_mod.NOT_FOUND)

    def test_a_middling_candidate_needs_review(self):
        status, _reason = enrich_mod.decide([self._candidate(45)], subject())
        self.assertEqual(status, enrich_mod.NEEDS_REVIEW)

    def test_a_strong_candidate_is_found(self):
        status, _reason = enrich_mod.decide([self._candidate(88)], subject())
        self.assertEqual(status, enrich_mod.FOUND)

    def test_a_lone_search_snippet_never_counts_as_an_answer(self):
        status, reason = enrich_mod.decide([self._candidate(95, how="search")], subject())
        self.assertEqual(status, enrich_mod.NEEDS_REVIEW)
        self.assertIn("snippet", reason)


class TestEndToEnd(unittest.TestCase):
    """The whole pipeline over the synthetic corpus."""

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp()
        os.environ["REACHOUT_HOME"] = cls.tmp

    def _lookup(self, name, email, **kw):
        with install(synthetic.web()):
            with Database(Path(self.tmp) / "e2e.db") as db:
                db.upsert_company(synthetic.COMPANY, synthetic.DOMAIN, 0.95, "test")
                return enrich_mod.enrich(name, synthetic.COMPANY, email, db=db,
                                         region=synthetic.REGION,
                                         exclude=["personal-profiles"], **kw)

    def test_a_published_mobile_is_found_with_its_url(self):
        lookup = self._lookup("Amara Okafor", "amara.okafor@northwind.example")
        self.assertEqual(lookup.status, enrich_mod.FOUND)
        self.assertEqual(lookup.answer.e164, "+447700900412")
        self.assertEqual(lookup.answer.kind, phonelib.MOBILE)
        self.assertTrue(lookup.answer.source_url.startswith("https://dataday.example/"))

    def test_a_colleagues_direct_dial_is_not_offered(self):
        lookup = self._lookup("Amara Okafor", "amara.okafor@northwind.example")
        self.assertNotIn("+442079460813", [c.e164 for c in lookup.candidates])

    def test_the_switchboard_is_reported_separately_from_the_person(self):
        lookup = self._lookup("Amara Okafor", "amara.okafor@northwind.example")
        self.assertIn("+442079460800", [c.e164 for c in lookup.company_numbers])
        self.assertNotIn("+442079460800", [c.e164 for c in lookup.candidates])

    def test_somebody_with_no_published_number_is_not_found(self):
        lookup = self._lookup("Tobias Lindqvist", "t.lindqvist@northwind.example")
        self.assertEqual(lookup.status, enrich_mod.NOT_FOUND)
        self.assertEqual(lookup.candidates, [])
        self.assertIsNone(lookup.answer)

    def test_the_neighbouring_row_does_not_leak_between_people(self):
        amara = self._lookup("Amara Okafor", "amara.okafor@northwind.example")
        priya = self._lookup("Priya Raman", "priya.raman@northwind.example")
        self.assertIn("+442079460812", [c.e164 for c in amara.candidates])
        self.assertEqual([c.e164 for c in priya.candidates], ["+442079460813"])

    def test_a_namesakes_number_never_reaches_the_answer(self):
        lookup = self._lookup("Amara Okafor", "")
        self.assertNotIn("+447700900654", [c.e164 for c in lookup.candidates])

    def test_the_decision_and_its_evidence_are_written_down(self):
        with install(synthetic.web()):
            with Database(Path(self.tmp) / "audit.db") as db:
                db.upsert_company(synthetic.COMPANY, synthetic.DOMAIN, 0.95, "test")
                lookup = enrich_mod.enrich(
                    "Amara Okafor", synthetic.COMPANY, "amara.okafor@northwind.example",
                    db=db, region="GB", exclude=["personal-profiles"])
                rows = db.lookups_for(lookup.subject.key)
                self.assertEqual(rows[0]["status"], enrich_mod.FOUND)
                self.assertEqual(rows[0]["best_e164"], "+447700900412")
                evidence = db.lookup_evidence(lookup.lookup_id)
                self.assertTrue(evidence)
                self.assertTrue(all(row["source_url"] for row in evidence))

    def test_forgetting_a_person_removes_their_lookup_history(self):
        with install(synthetic.web()):
            with Database(Path(self.tmp) / "forget.db") as db:
                db.upsert_company(synthetic.COMPANY, synthetic.DOMAIN, 0.95, "test")
                enrich_mod.enrich("Amara Okafor", synthetic.COMPANY,
                                  "amara.okafor@northwind.example", db=db, region="GB",
                                  exclude=["personal-profiles"])
                self.assertIn("phone_lookups", db.preview_forget(person="Amara Okafor"))
                db.forget(person="Amara Okafor")
                self.assertEqual(db.preview_forget(person="Amara Okafor"), {})

    def test_a_missing_name_is_refused_before_anything_is_fetched(self):
        web = FakeWeb(synthetic.PAGES, synthetic.RESULTS)
        with install(web):
            lookup = enrich_mod.enrich("", "Northwind Analytics", "", persist=False)
        self.assertEqual(lookup.status, enrich_mod.NOT_FOUND)
        self.assertEqual(web.requested, [])

    def test_json_output_carries_the_url_for_every_candidate(self):
        payload = self._lookup("Amara Okafor", "amara.okafor@northwind.example").as_dict()
        self.assertEqual(payload["status"], "found")
        self.assertTrue(payload["phone"]["source_url"])
        for candidate in payload["candidates"]:
            self.assertTrue(candidate["source_url"])
            self.assertTrue(candidate["evidence"])


if __name__ == "__main__":
    unittest.main()
