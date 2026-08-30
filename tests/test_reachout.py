"""Offline test suite - no network, safe to run anywhere."""

import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from reachout import engine, patterns, phonefind, scoring, verify   # noqa: E402
from reachout import phones as phonelib                 # noqa: E402
from reachout.company import (looks_like_domain, name_affinity,  # noqa: E402
                              name_caveat, normalise_company)
from reachout.db import Database                        # noqa: E402
from reachout.names import Name, from_linkedin_url, parse, slug, variants  # noqa: E402
from reachout.sources.github import is_usable_email     # noqa: E402
from reachout.engine import is_company_handle           # noqa: E402
from reachout.company import PRIMARY_TLDS, TLDS         # noqa: E402
from reachout.sources.search import registrable         # noqa: E402
from reachout.sources.directory import (attribute_rows,  # noqa: E402
                                        rows_from_html)
from reachout.sources.website import (FoundPhone, _clean_email,  # noqa: E402
                                      _decode_cfemail, _phones_from_page,
                                      _walk_jsonld, is_role_account, pair_samples,
                                      person_names)


class TestNames(unittest.TestCase):
    def test_basic(self):
        n = parse("John Smith")
        self.assertEqual((n.first, n.last, n.f), ("john", "smith", "j"))

    def test_reversed(self):
        self.assertEqual(parse("Smith, John").first, "john")

    def test_suffix_and_title_stripped(self):
        n = parse("Dr. Arun Kumar Sharma Jr.")
        self.assertEqual((n.first, n.middle, n.last), ("arun", "kumar", "sharma"))

    def test_reversed_with_suffix_is_not_flipped(self):
        n = parse("John Smith, PhD")
        self.assertEqual((n.first, n.last), ("john", "smith"))

    def test_accents(self):
        self.assertEqual(parse("José Muñoz").first, "jose")
        self.assertEqual(slug("Łukasz Jr"), "lukaszjr")

    def test_particles_fold_into_surname(self):
        self.assertEqual(parse("Jan van der Berg").last, "vanderberg")
        self.assertEqual(parse("Ana de la Cruz").last, "delacruz")

    def test_single_token(self):
        n = parse("Cher")
        self.assertTrue(n.first and not n.ok)

    def test_empty(self):
        self.assertFalse(parse("").ok)
        self.assertFalse(parse("   ").ok)

    def test_variants_cover_surname_ambiguity(self):
        combos = {(v.first, v.last) for v in variants(parse("Maria Garcia Lopez"))}
        self.assertIn(("maria", "garcialopez"), combos)
        self.assertIn(("maria", "lopez"), combos)

    def test_trailing_initial_is_a_surname_not_noise(self):
        """'Ramesh Kumar S' is ramesh.s@ - popping the S made that unreachable."""
        n = parse("Ramesh Kumar S")
        self.assertEqual(n.tokens, ["ramesh", "kumar", "s"])
        self.assertEqual((n.first, n.last), ("ramesh", "s"))

    def test_trailing_noise_is_still_reachable_as_a_reading(self):
        """Keeping the letter must not lose the reading where it really is noise."""
        combos = {(v.first, v.last) for v in variants(parse("John Smith X"))}
        self.assertIn(("john", "smith"), combos)

    def test_initial_first_names_offer_both_orders(self):
        """India writes the same person both ways; neither can be ruled out."""
        combos = {(v.first, v.last) for v in variants(parse("P. Sasikumar"))}
        self.assertIn(("p", "sasikumar"), combos)
        self.assertIn(("sasikumar", "p"), combos)
        combos = {(v.first, v.last) for v in variants(parse("K. R. Narayanan"))}
        self.assertIn(("narayanan", "kr"), combos)

    def test_an_ordinary_name_has_one_reading(self):
        """No new ambiguity for names that were never ambiguous."""
        self.assertEqual(len(variants(parse("John Smith"))), 1)
        self.assertEqual(len(variants(parse("Nithin Kamath"))), 1)

    def test_linkedin(self):
        name, handle = from_linkedin_url("https://www.linkedin.com/in/john-smith-4a7b21/")
        self.assertEqual((name.first, name.last), ("john", "smith"))
        self.assertEqual(handle, "john-smith-4a7b21")

    def test_linkedin_non_url(self):
        name, handle = from_linkedin_url("not a url")
        self.assertEqual(handle, "")


class TestPatterns(unittest.TestCase):
    def test_render(self):
        n = parse("John Smith")
        self.assertEqual(patterns.render("{first}.{last}", n), "john.smith")
        self.assertEqual(patterns.render("{f}{last}", n), "jsmith")
        self.assertEqual(patterns.render("{f}{m}{last}", n), "")  # no middle name

    def test_matching(self):
        n = parse("John Smith")
        self.assertIn("{first}.{last}", patterns.matching_templates("john.smith", n))
        self.assertIn("{f}{last}", patterns.matching_templates("jsmith", n))
        self.assertEqual(patterns.matching_templates("someoneelse", n), [])

    def test_infer_consistent_domain(self):
        samples = [("alice", "wong", "alice.wong"), ("bob", "hall", "bob.hall"),
                   ("cy", "ng", "cy.ng"), ("dee", "roy", "dee.roy")]
        top = patterns.infer(samples)[0]
        self.assertEqual(top.template, "{first}.{last}")
        self.assertGreater(top.confidence, 0.7)

    def test_infer_mixed_domain_is_not_overconfident(self):
        samples = [("alice", "wong", "alice.wong"), ("bob", "hall", "bhall"),
                   ("cy", "ng", "cy"), ("dee", "roy", "roy.dee")]
        top = patterns.infer(samples)[0]
        self.assertLess(top.confidence, 0.45)

    def test_infer_empty(self):
        self.assertEqual(patterns.infer([]), [])
        self.assertEqual(patterns.infer([("", "", "")]), [])

    def test_shape_compatibility(self):
        self.assertTrue(patterns.shape_compatible("darrell.gehring", "{first}.{last}"))
        self.assertFalse(patterns.shape_compatible("darrell.gehring", "{f}.{last}"))
        self.assertTrue(patterns.shape_compatible("j.smith", "{f}.{last}"))
        self.assertFalse(patterns.shape_compatible("jsmith", "{first}.{last}"))

    def test_shape_rescues_bad_display_names(self):
        # 'benclive' is a login, not a name - the address shape still fits first.last
        samples = [("alice", "wong", "alice.wong"), ("bob", "hall", "bob.hall"),
                   ("ben", "clive", "ben.clive"), ("zz", "top", "julie.stickler")]
        top = patterns.infer(samples)[0]
        self.assertEqual(top.template, "{first}.{last}")

    def test_generate_uses_domain_pattern_first(self):
        guesses = patterns.infer([("a", "wong", "a.wong"), ("bob", "hall", "bob.hall"),
                                  ("cy", "ng", "cy.ng")])
        out = patterns.generate(parse("John Smith"), "acme.com", guesses, limit=5)
        self.assertEqual(out[0].address, "john.smith@acme.com")
        self.assertEqual(out[0].source, "domain-pattern")

    def test_generate_without_pattern_uses_prior(self):
        out = patterns.generate(parse("Jane Doe"), "newco.io", None, limit=3)
        self.assertEqual(out[0].address, "jane.doe@newco.io")
        self.assertTrue(all(c.source == "global-prior" for c in out))

    def test_generate_needs_a_domain(self):
        self.assertEqual(patterns.generate(parse("Jane Doe"), "", None), [])

    def test_generate_dedupes(self):
        out = patterns.generate(parse("Jane Doe"), "x.com", None, limit=20)
        self.assertEqual(len(out), len({c.address for c in out}))

    def test_domain_observed_outranks_generic_prior(self):
        guesses = patterns.infer([("a", "b", "b.a")])          # {last}.{first}, one sample
        out = patterns.generate(parse("John Smith"), "acme.com", guesses, limit=8)
        addresses = [c.address for c in out]
        self.assertLess(addresses.index("smith.john@acme.com"),
                        addresses.index("johnsmith@acme.com"))


class TestTruncatedLogins(unittest.TestCase):
    """Legacy 8-character login conventions - {first:N} widths and the ~N cap.

    Every local part below is a real address harvested from redhat.com, which
    issues classic UNIX/Kerberos logins: first[:k] + last, cut to 8 characters,
    with k grown to break collisions.
    """

    REAL = [
        ("tomas", "david", "tdavid", "{f}{last}"),
        ("dakota", "long", "dalong", "{first:2}{last}"),
        ("matt", "clark", "maclark", "{first:2}{last}"),
        ("gianluca", "stella", "gistella", "{first:2}{last}"),
        ("daniel", "hall", "danhall", "{first:3}{last}"),
        ("florian", "bergmann", "fbergman", "{f}{last}~8"),
        ("tim", "williams", "tiwillia", "{first:2}{last}~8"),
        ("emika", "hammond", "emhammon", "{first:2}{last}~8"),
    ]

    def test_renders_real_locals(self):
        for first, last, local, template in self.REAL:
            with self.subTest(local=local):
                name = Name(raw=f"{first} {last}", first=first, last=last)
                self.assertEqual(patterns.render(template, name), local)

    def test_every_real_local_is_recognised(self):
        # A local nothing can rebuild teaches the domain nothing, so recognition
        # matters as much as generation.
        for first, last, local, _ in self.REAL:
            with self.subTest(local=local):
                name = parse(f"{first} {last}")
                self.assertTrue(patterns.matching_templates(local, name),
                                f"no template explains {local}")

    def test_split_cap(self):
        self.assertEqual(patterns.split_cap("{f}{last}~8"), ("{f}{last}", 8))
        self.assertEqual(patterns.split_cap("{f}{last}"), ("{f}{last}", 0))
        self.assertEqual(patterns.split_cap(""), ("", 0))

    def test_width_generalises_the_initial_tokens(self):
        n = parse("John Smith")
        self.assertEqual(patterns.render("{first:1}{last}", n),
                         patterns.render("{f}{last}", n))

    def test_cap_shorter_than_the_name_truncates(self):
        n = parse("Jonathan Smitherton")
        self.assertEqual(patterns.render("{f}{last}~8", n), "jsmither")

    def test_cap_never_leaves_a_trailing_separator(self):
        # 'john.smith' cut to 5 is 'john.', which no mailbox uses.
        n = parse("John Smith")
        self.assertEqual(patterns.render("{first}.{last}~5", n), "john")

    def test_missing_token_still_wins_over_cap(self):
        n = parse("Cher")           # no surname
        self.assertEqual(patterns.render("{f}{last}~8", n), "")

    def test_capped_templates_take_no_part_in_shape_voting(self):
        # Truncation destroys the length relationship a shape signature encodes,
        # so a capped form counts only when a real name reproduces it exactly.
        self.assertIsNone(patterns.template_shape("{f}{last}~8"))
        self.assertFalse(patterns.shape_compatible("fbergman", "{f}{last}~8"))

    def test_width_tokens_are_shape_checked_by_their_width(self):
        # shape_compatible asks whether *some* name could produce this local, so
        # a wider prefix stays possible ('dal' + 'ong'). What it must rule out is
        # a local too short to hold the fixed width plus a two-character word.
        self.assertTrue(patterns.shape_compatible("dalong", "{first:2}{last}"))
        self.assertTrue(patterns.shape_compatible("dalong", "{first:3}{last}"))
        self.assertFalse(patterns.shape_compatible("dal", "{first:3}{last}"))
        self.assertFalse(patterns.shape_compatible("da", "{first:2}{last}"))

    def test_new_syntax_counts_as_a_template(self):
        self.assertTrue(patterns.is_template("{first:2}{last}~8"))
        self.assertFalse(patterns.is_template("plainliteral"))

    def test_domain_learns_the_convention(self):
        samples = [(f, l, loc) for f, l, loc, _ in self.REAL]
        top = [g.template for g in patterns.infer(samples)[:4]]
        self.assertTrue(any(patterns.split_cap(t)[1] == 8 or ":" in t for t in top),
                        f"no truncating template surfaced: {top}")

    def test_alternate_prefix_is_offered_as_a_candidate(self):
        # k is chosen per person to break collisions and cannot be known from a
        # name, so the alternate readings must at least be on the list.
        samples = [(f, l, loc) for f, l, loc, _ in self.REAL]
        guesses = patterns.infer(samples)
        name = Name(raw="dakota long", first="dakota", last="long",
                    tokens=["dakota", "long"])
        locals_ = [c.address.split("@")[0]
                   for c in patterns.generate(name, "redhat.com", guesses, limit=3)]
        self.assertIn("dalong", locals_)

    def test_fallbacks_do_not_crowd_out_a_well_evidenced_domain(self):
        # redhat.com has no first.last address at all; it must not outrank the
        # domain's own alternates just because it is common everywhere.
        samples = [(f, l, loc) for f, l, loc, _ in self.REAL]
        guesses = patterns.infer(samples)
        name = Name(raw="matt clark", first="matt", last="clark",
                    tokens=["matt", "clark"])
        locals_ = [c.address.split("@")[0]
                   for c in patterns.generate(name, "redhat.com", guesses, limit=3)]
        self.assertIn("maclark", locals_)
        self.assertNotIn("matt.clark", locals_[:2])


class TestIndianNameGeneration(unittest.TestCase):
    """South Indian initial-style names, generated as well as recognised."""

    def _locals(self, raw, guesses=None, limit=8):
        return [c.address.split("@")[0]
                for c in patterns.generate(parse(raw), "acme.com", guesses, limit=limit)]

    def test_initial_first_name_reaches_the_given_name_address(self):
        """'P. Sasikumar' used to produce p.sasikumar@ and nothing else."""
        self.assertIn("sasikumar.p", self._locals("P. Sasikumar"))

    def test_trailing_initial_is_generated(self):
        self.assertIn("ramesh.s", self._locals("Ramesh Kumar S"))

    def test_primary_reading_still_ranks_first(self):
        """A leading initial is a Western form too - T. S. Eliot must not flip."""
        top = self._locals("T. S. Eliot")[0]
        self.assertTrue(top.startswith("t"), top)

    def test_alternate_readings_are_damped(self):
        """The way the name was written outranks the way it might have been."""
        cands = patterns.generate(parse("P. Sasikumar"), "acme.com")
        primary = [c for c in cands if c.address.startswith("p.sasikumar@")][0]
        alternate = [c for c in cands if c.address.startswith("sasikumar.p@")][0]
        self.assertGreater(primary.prior, alternate.prior)

    def test_recognition_learns_from_the_reversed_reading(self):
        """The learning half: sasikumar.p@ is explained by 'P. Sasikumar'."""
        hit = patterns.matching_reading("sasikumar.p", parse("P. Sasikumar"))
        self.assertIsNotNone(hit)
        reading, template = hit
        self.assertEqual(template, "{first}.{last}")
        self.assertEqual((reading.first, reading.last), ("sasikumar", "p"))

    def test_the_domain_pattern_decides_the_order(self):
        """Given a company that uses {last}.{first}, the reversed reading wins."""
        guesses = [patterns.PatternGuess("{last}.{first}", 9, 9, 0.9)]
        self.assertEqual(self._locals("P. Sasikumar", guesses)[0], "sasikumar.p")


class TestVerify(unittest.TestCase):
    def test_syntax(self):
        self.assertTrue(verify.check_syntax("john.smith@acme.com"))
        self.assertFalse(verify.check_syntax("john..smith@acme.com"))
        self.assertFalse(verify.check_syntax(".john@acme.com"))
        self.assertFalse(verify.check_syntax("john@acme"))
        self.assertFalse(verify.check_syntax("no-at-sign.com"))
        self.assertFalse(verify.check_syntax("a" * 65 + "@acme.com"))

    def test_split(self):
        self.assertEqual(verify.split("A.B@Acme.COM"), ("a.b", "acme.com"))

    def test_role_and_free_lists(self):
        self.assertTrue(is_role_account("info@acme.com"))
        self.assertTrue(is_role_account("no-reply@acme.com"))
        self.assertFalse(is_role_account("john.smith@acme.com"))
        self.assertIn("gmail.com", verify.FREE_PROVIDERS)
        self.assertIn("mailinator.com", verify.DISPOSABLE)


class TestScoring(unittest.TestCase):
    def _verdict(self, **kw):
        v = verify.Verdict(address=kw.pop("address", "john.smith@acme.com"))
        v.syntax_ok = kw.pop("syntax_ok", True)
        v.has_mx = kw.pop("has_mx", True)
        v.domain_resolves = kw.pop("domain_resolves", True)
        for key, value in kw.items():
            setattr(v, key, value)
        return v

    def test_observed_beats_inference(self):
        name = parse("John Smith")
        strong = patterns.Candidate("john.smith@acme.com", "{first}.{last}", 0.9, "domain-pattern")
        observed = scoring.score_candidate(strong, self._verdict(), name,
                                           observed_sources=["https://acme.com/team"],
                                           pattern_samples=20)
        inferred = scoring.score_candidate(strong, self._verdict(), name, pattern_samples=20)
        self.assertGreater(observed.value, inferred.value)

    def test_no_mx_is_capped(self):
        name = parse("John Smith")
        candidate = patterns.Candidate("john.smith@dead.com", "{first}.{last}", 0.95, "domain-pattern")
        score = scoring.score_candidate(candidate, self._verdict(has_mx=False, domain_resolves=False),
                                        name, pattern_samples=20)
        self.assertLessEqual(score.value, 30)

    def test_inference_never_reaches_certainty(self):
        name = parse("John Smith")
        candidate = patterns.Candidate("john.smith@acme.com", "{first}.{last}", 0.99, "domain-pattern")
        score = scoring.score_candidate(candidate, self._verdict(), name, pattern_samples=99)
        self.assertLessEqual(score.value, 79)

    def test_pattern_alone_never_grades_high(self):
        """HIGH must mean evidence about the person, not just the company format."""
        name = parse("John Smith")
        for source in ("domain-pattern", "pinned"):
            candidate = patterns.Candidate("john.smith@acme.com", "{first}.{last}", 0.99, source)
            score = scoring.score_candidate(candidate, self._verdict(), name, pattern_samples=99)
            self.assertNotEqual(score.grade, "HIGH", f"{source} reached HIGH without person evidence")

    def test_user_confirmed_outranks_everything(self):
        name = parse("John Smith")
        candidate = patterns.Candidate("john.smith@acme.com", "{first}.{last}", 0.9, "observed")
        confirmed = scoring.score_candidate(candidate, self._verdict(), name,
                                            observed_sources=["you told me"], user_confirmed=True)
        published = scoring.score_candidate(candidate, self._verdict(), name,
                                            observed_sources=["https://acme.com/team"])
        self.assertGreater(confirmed.value, published.value)
        self.assertEqual(confirmed.grade, "HIGH")
        keys = {s.key for s in confirmed.signals}
        self.assertIn("confirmed", keys)
        self.assertNotIn("observed", keys)   # never claim we found what you supplied

    def test_published_address_can_grade_high(self):
        name = parse("John Smith")
        candidate = patterns.Candidate("john.smith@acme.com", "{first}.{last}", 0.9, "observed")
        score = scoring.score_candidate(candidate, self._verdict(), name,
                                        observed_sources=["https://acme.com/team"],
                                        person_match=0.9)
        self.assertEqual(score.grade, "HIGH")

    def test_smtp_rejection_sinks_the_score(self):
        name = parse("John Smith")
        candidate = patterns.Candidate("john.smith@acme.com", "{first}.{last}", 0.95, "domain-pattern")
        score = scoring.score_candidate(candidate, self._verdict(smtp="rejected"), name,
                                        pattern_samples=20)
        self.assertEqual(score.value, 0)

    def test_role_account_penalised(self):
        name = parse("John Smith")
        candidate = patterns.Candidate("info@acme.com", "{first}", 0.5, "global-prior")
        plain = scoring.score_candidate(candidate, self._verdict(), name)
        role = scoring.score_candidate(candidate, self._verdict(is_role=True), name)
        self.assertLess(role.value, plain.value)

    def test_caps_only_recorded_when_they_bind(self):
        name = parse("John Smith")
        weak = patterns.Candidate("j@acme.com", "{f}", 0.02, "global-prior")
        score = scoring.score_candidate(weak, self._verdict(), name)
        self.assertEqual(score.caps, [])

    def test_grades(self):
        self.assertEqual(scoring.grade_for(95), "HIGH")
        self.assertEqual(scoring.grade_for(60), "MEDIUM")
        self.assertEqual(scoring.grade_for(35), "LOW")
        self.assertEqual(scoring.grade_for(5), "VERY LOW")


class TestCompanyHelpers(unittest.TestCase):
    def test_looks_like_domain(self):
        self.assertTrue(looks_like_domain("acme.com"))
        self.assertTrue(looks_like_domain("iitb.ac.in"))
        self.assertFalse(looks_like_domain("Acme Inc"))
        self.assertFalse(looks_like_domain("john@acme.com"))

    def test_normalise(self):
        self.assertEqual(normalise_company("Grafana Labs Inc."), "Grafana")
        self.assertEqual(slug(normalise_company("Acme Technologies Pvt Ltd")), "acme")

    def test_registrable(self):
        self.assertEqual(registrable("www.mail.iitb.ac.in"), "iitb.ac.in")
        self.assertEqual(registrable("sub.acme.com"), "acme.com")
        self.assertEqual(registrable("acme.co.uk"), "acme.co.uk")

    def test_indian_second_levels_keep_the_organisation(self):
        """res.in and nic.in are registries - the organisation is one label down.

        Collapsing tifr.res.in to res.in pointed every candidate at the registry
        rather than the institute.
        """
        self.assertEqual(registrable("tifr.res.in"), "tifr.res.in")
        self.assertEqual(registrable("www.ncbs.res.in"), "ncbs.res.in")
        self.assertEqual(registrable("india.nic.in"), "india.nic.in")
        self.assertEqual(registrable("acme.ind.in"), "acme.ind.in")
        self.assertEqual(registrable("acme.firm.in"), "acme.firm.in")
        self.assertEqual(registrable("acme.gen.in"), "acme.gen.in")
        # Still collapses a genuine subdomain of an Indian organisation.
        self.assertEqual(registrable("mail.tifr.res.in"), "tifr.res.in")
        self.assertEqual(registrable("cse.iitb.ac.in"), "iitb.ac.in")

    def test_indian_second_levels_are_gated_on_the_in_tld(self):
        """The same labels are ordinary names under any other TLD."""
        self.assertEqual(registrable("mail.res.com"), "res.com")
        self.assertEqual(registrable("api.gen.io"), "gen.io")

    def test_indian_tlds_are_actually_tried(self):
        """.in and .co.in used to sit outside the slice _guess_from_tlds reads,
        so an Indian company was never guessed directly at all."""
        tried = TLDS[:8]
        self.assertIn("in", tried)
        self.assertIn("co.in", tried)
        self.assertLessEqual({"in", "co.in"}, PRIMARY_TLDS)


class TestWebsiteHelpers(unittest.TestCase):
    def test_clean_email(self):
        self.assertEqual(_clean_email("  John.Smith@Acme.com. "), "john.smith@acme.com")
        self.assertEqual(_clean_email("logo@2x.png"), "")
        self.assertEqual(_clean_email("not-an-email"), "")

    def test_cloudflare_decode(self):
        secret = "john@acme.com"
        key = 0x2a
        encoded = f"{key:02x}" + "".join(f"{ord(c) ^ key:02x}" for c in secret)
        self.assertEqual(_decode_cfemail(encoded), secret)
        self.assertEqual(_decode_cfemail("zzzz"), "")

    def test_pairing_requires_a_real_match(self):
        emails = {"alice.wong@acme.com": "Alice Wong", "info@acme.com": ""}
        pairs = pair_samples(emails, ["Alice Wong", "Bob Hall"], "acme.com")
        self.assertEqual(pairs, [("alice", "wong", "alice.wong", "{first}.{last}")])

    def test_pairing_invents_nothing(self):
        emails = {"xyz123@acme.com": ""}
        self.assertEqual(pair_samples(emails, ["Alice Wong"], "acme.com"), [])


class TestCompanyHandles(unittest.TestCase):
    """Employer abbreviations posing as surnames in git display names."""

    def test_abbreviations_are_recognised(self):
        for token in ("razorpay", "raz", "rzp", "rzpx"):
            self.assertTrue(is_company_handle(token, "razorpay"), token)

    def test_real_surnames_survive(self):
        for token in ("rafiq", "rastogi", "nawal", "hazra", "shah", "dhir"):
            self.assertFalse(is_company_handle(token, "razorpay"), token)
        for token in ("pai", "gohil", "kamath", "patil", "kumar", "ranjan"):
            self.assertFalse(is_company_handle(token, "zerodha"), token)

    def test_a_long_token_is_never_an_abbreviation(self):
        self.assertFalse(is_company_handle("razorpaying", "razorpay"))

    def test_harvest_drops_the_handle_but_keeps_the_person(self):
        """The junk row teaches nothing; the real pair still lands."""
        with tempfile.TemporaryDirectory() as tmp:
            db = Database(os.path.join(tmp, "t.db"))
            engine._store_samples(db, "razorpay.com", [
                ("abhishek rzp", "abhishek.sk@razorpay.com", "u"),
                ("rohitr raz", "rohit.ravipudi@razorpay.com", "u"),
                ("aditya nawal", "aditya.nawal@razorpay.com", "u"),
            ])
            stored = {(r["first"], r["last"]) for r in db.samples_for_domain("razorpay.com")}
            self.assertEqual(stored, {("aditya", "nawal")})
            db.close()

    def test_a_handle_that_explains_its_own_address_is_kept(self):
        """The template gate: a name that rebuilds its local part is real evidence
        however much it looks like a handle."""
        with tempfile.TemporaryDirectory() as tmp:
            db = Database(os.path.join(tmp, "t.db"))
            engine._store_samples(db, "whipps.com", [
                ("Kevin White", "kevinw@whipps.com", "u"),
            ])
            rows = db.samples_for_domain("whipps.com")
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]["template"], "{first}{l}")
            db.close()


class TestGitHubHelpers(unittest.TestCase):
    def test_noise_filtered(self):
        self.assertFalse(is_usable_email("1234+user@users.noreply.github.com"))
        self.assertFalse(is_usable_email("dependabot[bot]@users.noreply.github.com"))
        self.assertFalse(is_usable_email("root@localhost"))
        self.assertFalse(is_usable_email("bad"))
        self.assertTrue(is_usable_email("jane.doe@acme.com"))


class TestDatabase(unittest.TestCase):
    def setUp(self):
        self.path = Path(tempfile.mkdtemp()) / "t.db"
        self.db = Database(self.path)

    def tearDown(self):
        self.db.close()

    def test_company_and_person_roundtrip(self):
        cid = self.db.upsert_company("Acme Inc", "acme.com", 0.9, "test")
        self.assertEqual(cid, self.db.upsert_company("Acme Inc"))   # dedupes
        self.assertEqual(self.db.company_domain("acme inc"), ("acme.com", 0.9))
        pid = self.db.upsert_person(parse("John Smith"), cid, title="Engineer")
        self.assertEqual(pid, self.db.upsert_person(parse("John Smith"), cid))

    def test_weaker_evidence_does_not_overwrite_domain(self):
        self.db.upsert_company("Acme", "acme.com", 0.9, "good")
        self.db.upsert_company("Acme", "wrong.com", 0.2, "bad")
        self.assertEqual(self.db.company_domain("Acme")[0], "acme.com")

    def test_samples_are_unique(self):
        self.assertTrue(self.db.add_sample("acme.com", "a", "w", "a.w"))
        self.assertFalse(self.db.add_sample("acme.com", "a", "w", "a.w"))
        self.assertEqual(len(self.db.samples_for_domain("acme.com")), 1)

    def test_domain_emails(self):
        self.assertTrue(self.db.add_domain_email("acme.com", "a@acme.com", "A", "u"))
        self.assertFalse(self.db.add_domain_email("acme.com", "a@acme.com"))
        self.db.add_domain_email("acme.com", "info@acme.com", is_role=True)
        self.assertEqual(len(self.db.domain_emails("acme.com")), 1)
        self.assertEqual(len(self.db.domain_emails("acme.com", include_role=True)), 2)

    def test_patterns_roundtrip(self):
        self.db.save_patterns("acme.com", [("{first}.{last}", 4, 5, 0.8)])
        rows = self.db.get_patterns("acme.com")
        self.assertEqual(rows[0]["template"], "{first}.{last}")
        self.db.save_patterns("acme.com", [("{f}{last}", 2, 5, 0.4)])
        self.assertEqual(len(self.db.get_patterns("acme.com")), 1)  # replaced, not appended

    def test_stats_keys(self):
        for key in ("people", "companies", "emails", "pattern_samples", "domain_emails"):
            self.assertIn(key, self.db.stats())


class TestEvaluate(unittest.TestCase):
    def test_leave_one_out(self):
        from reachout.evaluate import evaluate_domain
        path = Path(tempfile.mkdtemp()) / "e.db"
        db = Database(path)
        for first, last in [("alice", "wong"), ("bob", "hall"), ("cy", "ng"), ("dee", "roy")]:
            db.add_sample("acme.com", first, last, f"{first}.{last}")
        report = evaluate_domain("acme.com", db)
        self.assertEqual(report.trials, 4)
        self.assertEqual(report.top1, 4)
        db.close()

    def test_too_few_samples(self):
        from reachout.evaluate import evaluate_domain
        path = Path(tempfile.mkdtemp()) / "e2.db"
        db = Database(path)
        db.add_sample("acme.com", "a", "b", "a.b")
        self.assertIsNone(evaluate_domain("acme.com", db))
        db.close()


class TestTeach(unittest.TestCase):
    def setUp(self):
        self.path = Path(tempfile.mkdtemp()) / "t.db"
        self.db = Database(self.path)

    def tearDown(self):
        self.db.close()

    def test_known_address_teaches_a_pattern(self):
        from reachout.engine import load_patterns, refresh_pattern, teach
        out = teach("acme.com", "Alice Wong", "alice.wong@acme.com", self.db)
        self.assertEqual(out["template"], "{first}.{last}")
        self.assertTrue(out["stored"])
        self.assertTrue(out["banked"])
        refresh_pattern("acme.com", self.db)
        self.assertEqual(load_patterns("acme.com", self.db)[0].template, "{first}.{last}")

    def test_mismatched_address_is_banked_but_teaches_nothing(self):
        from reachout.engine import teach
        out = teach("acme.com", "Alice Wong", "zx9q7@acme.com", self.db)
        self.assertEqual(out["template"], "")
        self.assertFalse(out["stored"])
        self.assertTrue(out["banked"])         # still worth remembering
        self.assertIn("teaches no pattern", out["error"])

    def test_wrong_domain_rejected(self):
        from reachout.engine import teach
        out = teach("acme.com", "Alice Wong", "alice@other.com", self.db)
        self.assertIn("not an address at", out["error"])
        self.assertFalse(out["stored"])

    def test_pinned_pattern_wins_over_inferred(self):
        from reachout.engine import load_patterns, refresh_pattern
        for first, last in [("bob", "hall"), ("cy", "ng"), ("dee", "roy")]:
            self.db.add_sample("acme.com", first, last, f"{first[0]}{last}")
        refresh_pattern("acme.com", self.db)
        self.assertEqual(load_patterns("acme.com", self.db)[0].template, "{f}{last}")
        self.db.pin_pattern("acme.com", "{first}.{last}", 0.9)
        top = load_patterns("acme.com", self.db)[0]
        self.assertEqual(top.template, "{first}.{last}")
        self.assertTrue(top.pinned)

    def test_pinned_keeps_backing_sample_count(self):
        from reachout.engine import load_patterns, refresh_pattern
        for first, last in [("bob", "hall"), ("cy", "ng")]:
            self.db.add_sample("acme.com", first, last, f"{first}.{last}")
        refresh_pattern("acme.com", self.db)
        self.db.pin_pattern("acme.com", "{first}.{last}", 0.9)
        top = load_patterns("acme.com", self.db)[0]
        self.assertTrue(top.pinned)
        self.assertEqual(top.samples, 2)

    def test_harvest_never_overwrites_a_pinned_pattern(self):
        from reachout.engine import load_patterns, refresh_pattern
        self.db.pin_pattern("acme.com", "{first}", 0.9)
        for first, last in [("bob", "hall"), ("cy", "ng"), ("dee", "roy")]:
            self.db.add_sample("acme.com", first, last, f"{first}.{last}")
        refresh_pattern("acme.com", self.db)      # would infer {first}.{last}
        self.assertEqual(load_patterns("acme.com", self.db)[0].template, "{first}")

    def test_unpin(self):
        from reachout.engine import load_patterns
        self.db.pin_pattern("acme.com", "{first}", 0.9)
        self.assertEqual(self.db.unpin_pattern("acme.com"), 1)
        self.assertEqual(load_patterns("acme.com", self.db), [])

    def test_pinned_candidate_is_scored_as_stated_not_thin(self):
        guesses = [patterns.PatternGuess("{first}", 0.0, 0, 0.9, pinned=True)]
        candidate = patterns.generate(parse("Jane Doe"), "acme.com", guesses, limit=1)[0]
        self.assertEqual(candidate.source, "pinned")
        verdict = verify.Verdict(address=candidate.address)
        verdict.syntax_ok = verdict.has_mx = verdict.domain_resolves = True
        score = scoring.score_candidate(candidate, verdict, parse("Jane Doe"), pattern_samples=0)
        keys = {s.key for s in score.signals}
        self.assertIn("pinned", keys)
        self.assertNotIn("pattern_thin", keys)


class TestContactsImport(unittest.TestCase):
    def _write(self, text):
        path = Path(tempfile.mkdtemp()) / "c.csv"
        path.write_text(text, encoding="utf-8")
        return str(path)

    def test_simple_headers(self):
        from reachout.cli import _read_contacts
        path = self._write("Name,Email\nAlice Wong,alice.wong@acme.com\n")
        self.assertEqual(_read_contacts(path), [("Alice Wong", "alice.wong@acme.com")])

    def test_google_export_headers(self):
        from reachout.cli import _read_contacts
        path = self._write("First Name,Last Name,E-mail 1 - Value\nAlice,Wong,alice@acme.com\n")
        self.assertEqual(_read_contacts(path), [("Alice Wong", "alice@acme.com")])

    def test_rows_without_an_address_are_skipped(self):
        from reachout.cli import _read_contacts
        path = self._write("Name,Email\nNo Address,\nAlice Wong,alice@acme.com\n")
        self.assertEqual(len(_read_contacts(path)), 1)


class TestPhoneParsing(unittest.TestCase):
    def test_india_mobile(self):
        for raw in ["+91 98765 43210", "09876543210", "9876543210", "+91-98765-43210"]:
            p = phonelib.normalise(raw)
            self.assertTrue(p.valid, raw)
            self.assertEqual(p.e164, "+919876543210")
            self.assertEqual(p.kind, phonelib.MOBILE)
            self.assertTrue(p.kind_certain)

    def test_india_landline(self):
        p = phonelib.normalise("022-67891234")
        self.assertEqual(p.kind, phonelib.LANDLINE)
        self.assertEqual(p.area, "22")
        self.assertIn("Mumbai", " ".join(p.notes))

    def test_india_tollfree_and_premium(self):
        self.assertEqual(phonelib.normalise("1800 123 4567").kind, phonelib.TOLL_FREE)
        self.assertEqual(phonelib.normalise("1860 500 1111").kind, phonelib.PREMIUM)

    def test_india_rejects_bad_leading_digit(self):
        p = phonelib.normalise("+91 1234567890")
        self.assertFalse(p.valid)

    def test_india_rejects_wrong_length(self):
        self.assertFalse(phonelib.normalise("+91 12345").valid)

    def test_nanp_cannot_determine_line_type(self):
        p = phonelib.normalise("+1 (415) 555-2671")
        self.assertTrue(p.valid)
        self.assertFalse(p.kind_certain)
        self.assertEqual(p.kind, phonelib.UNKNOWN)
        self.assertTrue(phonelib.could_be_mobile(p))
        self.assertFalse(phonelib.is_mobile(p))

    def test_nanp_tollfree_and_invalid_area(self):
        self.assertEqual(phonelib.normalise("+1 800 555 0199").kind, phonelib.TOLL_FREE)
        self.assertFalse(phonelib.normalise("+1 (015) 555-2671").valid)

    def test_uk(self):
        self.assertEqual(phonelib.normalise("+44 7911 123456").kind, phonelib.MOBILE)
        landline = phonelib.normalise("+44 20 7946 0958")
        self.assertEqual(landline.kind, phonelib.LANDLINE)
        self.assertEqual(landline.area, "20")

    def test_extension_preserved(self):
        p = phonelib.normalise("+1 415 555 2671 ext 890")
        self.assertEqual(p.extension, "890")
        self.assertIn("ext. 890", p.pretty)

    def test_garbage_rejected(self):
        for raw in ["", "   ", "555", "not a number", "1" * 20]:
            self.assertFalse(phonelib.normalise(raw).valid, raw)

    def test_unknown_country_is_accepted_but_unclassified(self):
        p = phonelib.normalise("+65 6123 4567")
        self.assertTrue(p.valid)
        self.assertFalse(p.kind_certain)

    def test_context_signal(self):
        self.assertEqual(phonelib.context_signal("Office / reception"), "switchboard")
        self.assertEqual(phonelib.context_signal("Mobile:"), "personal")
        self.assertEqual(phonelib.context_signal("Sameer Verma"), "")


class TestPhoneAttribution(unittest.TestCase):
    # Fixtures use a realistic number on purpose: tidy sequences like 9876543210
    # trip the placeholder guard, which is tested separately.
    def _attr(self, found, who="Sameer Kumar Verma"):
        name = parse(who)
        return phonefind.attribute(found, name, phonelib.normalise(found.raw))

    def test_organisation_number_is_never_personal(self):
        found = FoundPhone(raw="+918040402020", scope="organization", context="customer service")
        attribution, score = self._attr(found)
        self.assertEqual(attribution, phonefind.COMPANY)
        self.assertLessEqual(score.value, 20)

    def test_switchboard_wording_demotes(self):
        found = FoundPhone(raw="+912267891234", context="Office reception: ")
        attribution, score = self._attr(found)
        self.assertEqual(attribution, phonefind.COMPANY)

    def test_tollfree_can_never_be_personal(self):
        found = FoundPhone(raw="+911800123456", context="Sameer Kumar Verma direct")
        attribution, _score = self._attr(found)
        self.assertEqual(attribution, phonefind.COMPANY)

    def test_structured_data_owner_attributes_to_person(self):
        found = FoundPhone(raw="+919821347756", owner="Sameer Kumar Verma",
                           scope="person", how="json-ld")
        attribution, score = self._attr(found)
        self.assertEqual(attribution, phonefind.PERSON)
        self.assertGreater(score.value, 55)

    def test_name_in_surrounding_text_attributes(self):
        found = FoundPhone(raw="+919821347756", context="Sameer Kumar Verma, mobile")
        attribution, _score = self._attr(found)
        self.assertEqual(attribution, phonefind.PERSON)

    def test_surname_alone_is_not_enough(self):
        found = FoundPhone(raw="+919821347756", context="Verma")
        attribution, score = self._attr(found)
        self.assertEqual(attribution, phonefind.UNKNOWN)
        self.assertLessEqual(score.value, 20)

    def test_user_confirmed_wins_and_is_capped(self):
        from reachout.db import USER_SOURCE
        found = FoundPhone(raw="+919821347756", owner="Sameer Kumar Verma",
                           scope="person", source_url=USER_SOURCE, context="mobile")
        attribution, score = self._attr(found)
        self.assertEqual(attribution, phonefind.PERSON)
        self.assertEqual(score.value, 96)
        self.assertIn("confirmed", {s.key for s in score.signals})
        self.assertNotIn("jsonld_person", {s.key for s in score.signals})

    def test_invalid_number_scores_zero(self):
        found = FoundPhone(raw="12345", owner="Sameer Kumar Verma", scope="person")
        _attribution, score = self._attr(found)
        self.assertEqual(score.value, 0)

    def test_landline_penalised_when_a_mobile_was_wanted(self):
        found = FoundPhone(raw="+912267891234", context="Sameer Kumar Verma")
        _a, score = self._attr(found)
        self.assertIn("landline", {s.key for s in score.signals})


class TestPhoneExtraction(unittest.TestCase):
    def test_jsonld_person_vs_organization(self):
        blob = {
            "@type": "Organization", "name": "Acme", "telephone": "+91 22 6789 1234",
            "employee": [{"@type": "Person", "name": "Alice Wong",
                          "telephone": "+91 98765 43210"}],
        }
        out = []
        _walk_jsonld(blob, out)
        by_number = {n: (owner, scope) for n, owner, scope, _hint, _mail in out}
        self.assertEqual(by_number["+91 22 6789 1234"][1], "organization")
        self.assertEqual(by_number["+91 98765 43210"], ("Alice Wong", "person"))

    def test_jsonld_keeps_the_email_beside_the_number(self):
        """A Person node stating both is the page identifying whose number it is."""
        out = []
        _walk_jsonld({"@type": "Person", "name": "Alice Wong",
                      "email": "mailto:Alice.Wong@acme.com",
                      "telephone": "+91 98765 43210"}, out)
        self.assertEqual(out[0][4], ["alice.wong@acme.com"])

    def test_jsonld_handles_lists_and_junk(self):
        out = []
        _walk_jsonld([{"@type": "Person", "name": "A", "telephone": ["+919876543210"]},
                      "a string", 42, None], out)
        self.assertEqual(len(out), 1)

    def test_text_scan_ignores_non_numbers(self):
        class FakeParser:
            tels, jsonld, microtels = [], [], []
        noise = "Order 12345678 placed on 2024 01 15, invoice 9900112233445566778"
        self.assertEqual(_phones_from_page(FakeParser(), noise), [])

    def test_text_scan_finds_real_numbers(self):
        class FakeParser:
            tels, jsonld, microtels = [], [], []
        found = _phones_from_page(FakeParser(), "Call us on +91 98765 43210 today")
        self.assertEqual([f.raw for f in found], ["+919876543210"])

    def test_tel_links_are_picked_up(self):
        class FakeParser:
            tels = [("+91 98765 43210", "Call Alice", "Alice Wong")]
            jsonld, microtels = [], []
        found = _phones_from_page(FakeParser(), "")
        self.assertEqual(found[0].how, "tel-link")
        self.assertIn("Alice Wong", found[0].context)

    def test_duplicates_collapse_to_one(self):
        class FakeParser:
            tels = [("+919876543210", "", "")]
            jsonld, microtels = [], []
        found = _phones_from_page(FakeParser(), "reach me on 98765 43210")
        self.assertEqual(len(found), 1)


class TestForget(unittest.TestCase):
    def setUp(self):
        self.path = Path(tempfile.mkdtemp()) / "f.db"
        self.db = Database(self.path)
        self.cid = self.db.upsert_company("Acme", "acme.com", 0.9, "t")
        self.pid = self.db.upsert_person(parse("Alice Wong"), self.cid)
        self.db.add_email(self.pid, "alice.wong@acme.com", 80, "HIGH")
        self.db.add_phone(self.pid, "+919876543210", "mobile", "IN", "person",
                          "person", 90, "HIGH")
        self.db.add_sample("acme.com", "alice", "wong", "alice.wong")
        self.db.add_domain_email("acme.com", "alice.wong@acme.com")
        self.db.add_domain_phone("acme.com", "+919876543210")

    def tearDown(self):
        self.db.close()

    def test_preview_does_not_delete(self):
        preview = self.db.preview_forget(person="Alice Wong")
        self.assertTrue(preview)
        self.assertEqual(len(self.db.conn.execute("SELECT * FROM people").fetchall()), 1)

    def test_forget_person_removes_their_records(self):
        self.db.forget(person="Alice Wong")
        for table in ("people", "emails", "phones"):
            self.assertEqual(self.db.conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0],
                             0, table)

    def test_forget_domain_clears_company_without_fk_error(self):
        """people.company_id references companies(id) - detach before deleting."""
        removed = self.db.forget(domain="acme.com")
        self.assertGreater(removed, 0)
        self.assertEqual(self.db.conn.execute("SELECT COUNT(*) FROM companies").fetchone()[0], 0)
        row = self.db.conn.execute("SELECT company_id FROM people").fetchone()
        self.assertIsNone(row["company_id"])

    def test_forget_email_also_drops_its_pattern_sample(self):
        self.db.forget(email="alice.wong@acme.com")
        self.assertEqual(len(self.db.samples_for_domain("acme.com")), 0)
        self.assertEqual(len(self.db.domain_emails("acme.com")), 0)

    def test_forget_phone(self):
        self.db.forget(phone="+919876543210")
        self.assertEqual(self.db.conn.execute("SELECT COUNT(*) FROM phones").fetchone()[0], 0)
        self.assertEqual(len(self.db.domain_phones("acme.com")), 0)

    def test_forget_nothing_matches(self):
        self.assertEqual(self.db.preview_forget(person="Nobody Here"), {})


class TestPlaceholderDetection(unittest.TestCase):
    def test_catches_made_up_numbers(self):
        cases = {
            "9876543210": "descending",
            "1234567890": "ascending",
            "9999999999": "identical",
            "1212121212": "two distinct",
            "+1 415 555 0123": "reserved",
        }
        for raw, expect in cases.items():
            reason = phonelib.looks_placeholder(phonelib.normalise(raw))
            self.assertTrue(reason, f"{raw} should be flagged")
            self.assertIn(expect, reason, raw)

    def test_leaves_real_numbers_alone(self):
        for raw in ["9821347756", "+1 415 555 2671", "+44 7911 123456", "+91 22 6789 1234"]:
            self.assertEqual(phonelib.looks_placeholder(phonelib.normalise(raw)), "", raw)

    def test_placeholder_is_penalised_even_when_attributed(self):
        found = FoundPhone(raw="+919876543210", owner="Alice Wong", scope="person",
                           how="json-ld")
        _attribution, score = phonefind.attribute(
            found, parse("Alice Wong"), phonelib.normalise("+919876543210"))
        self.assertIn("placeholder", {s.key for s in score.signals})
        self.assertLess(score.value, 40)


class TestLinkDiscovery(unittest.TestCase):
    def test_whole_segments_score_not_substrings(self):
        from reachout.sources.website import _score_link
        self.assertGreater(_score_link("/company/contact-us", ""), 0)
        self.assertGreater(_score_link("/team", ""), 0)
        # "management" inside a product path must not read as a leadership page
        self.assertEqual(_score_link("/solution/asset-management", ""), 0)
        self.assertEqual(_score_link("/ai-agents/management-risk", ""), 0)

    def test_only_nav_length_anchors_count(self):
        from reachout.sources.website import _score_link
        self.assertGreater(_score_link("/live", "Our Team"), 0)
        # marketing copy that happens to contain a hint word must not score
        self.assertEqual(_score_link("/live", "Thousands of people use this now"), 0)
        self.assertEqual(_score_link("/paths", "Why people switch to us"), 0)

    def test_shallow_pages_win_ties(self):
        from reachout.sources.website import _score_link
        self.assertGreater(_score_link("/contact", ""), _score_link("/a/b/contact", ""))

    def test_client_rendered_detection(self):
        from reachout.sources.website import looks_client_rendered
        shell = '<html><body><div id="root"></div><script src="app.js"></script></body></html>'
        self.assertTrue(looks_client_rendered(shell, "You need to enable JavaScript"))
        self.assertTrue(looks_client_rendered("<script></script>", "Greenflow"))
        self.assertFalse(looks_client_rendered("<p>x</p>", "a real page " * 60))


class TestRoleMailboxes(unittest.TestCase):
    def test_group_mailboxes_recognised(self):
        for local in ["founders", "founder", "hello", "ceo", "partnerships", "alumni",
                      "admissions", "investors"]:
            self.assertTrue(is_role_account(f"{local}@acme.com"), local)

    def test_real_people_unaffected(self):
        for local in ["sameer.verma", "jsmith", "priya", "a.gupta"]:
            self.assertFalse(is_role_account(f"{local}@acme.com"), local)

    def test_role_mailbox_never_becomes_a_pattern_sample(self):
        """'A note from Founders' + founders@ must not teach a {last} pattern."""
        pairs = pair_samples({"founders@acme.com": ""}, ["From Founders"], "acme.com")
        self.assertEqual(pairs, [])


def _tiny_pdf(text_ops: bytes, compress: bool = True) -> bytes:
    """Build a minimal single-stream PDF so PDF tests stay offline."""
    import zlib
    body = zlib.compress(text_ops) if compress else text_ops
    return (b"%PDF-1.4\n1 0 obj\n<< /Length " + str(len(body)).encode() +
            (b" /Filter /FlateDecode" if compress else b"") + b" >>\nstream\n" +
            body + b"\nendstream\nendobj\n%%EOF")


class TestPdfExtraction(unittest.TestCase):
    def test_literal_strings(self):
        from reachout.pdf import extract_text
        pdf = _tiny_pdf(b"BT (Hello) Tj (World) Tj ET")
        self.assertIn("Hello", extract_text(pdf))
        self.assertIn("World", extract_text(pdf))

    def test_uncompressed_stream(self):
        from reachout.pdf import extract_text
        self.assertIn("Plain", extract_text(_tiny_pdf(b"BT (Plain) Tj ET", compress=False)))

    def test_hex_strings(self):
        from reachout.pdf import extract_text
        self.assertIn("Hi", extract_text(_tiny_pdf(b"BT <4869> Tj ET")))

    def test_escapes_and_nesting(self):
        from reachout.pdf import extract_text
        out = extract_text(_tiny_pdf(rb"BT (a\(b\)c) Tj ET"))
        self.assertIn("a(b)c", out)

    def test_kerning_becomes_a_word_gap(self):
        from reachout.pdf import extract_text
        out = extract_text(_tiny_pdf(b"BT [(one)-500(two)] TJ ET"))
        self.assertIn("one two", out)
        tight = extract_text(_tiny_pdf(b"BT [(on)-20(e)] TJ ET"))
        self.assertIn("one", tight)          # small kerns are not word breaks

    def test_binary_stream_is_rejected(self):
        from reachout.pdf import extract_text
        noise = bytes(range(256)) * 20 + b"BT Tj"
        self.assertEqual(extract_text(_tiny_pdf(noise)), "")

    def test_non_pdf_returns_nothing(self):
        from reachout.pdf import extract_text, looks_like_pdf
        self.assertEqual(extract_text(b"<html>hi</html>"), "")
        self.assertFalse(looks_like_pdf(b"<html>"))
        self.assertTrue(looks_like_pdf(b"%PDF-1.7"))


class TestDocumentContacts(unittest.TestCase):
    def test_brace_group_expands(self):
        from reachout.sources.documents import expand_groups
        got = dict(expand_groups("{alice,bob.jones}@dept.edu"))
        self.assertEqual(set(got), {"alice@dept.edu", "bob.jones@dept.edu"})

    def test_latex_math_braces_recovered(self):
        """PDF renders LaTeX braces as bare f and g - the usual academic case."""
        from reachout.sources.documents import contacts_in_text
        text = "Google AI Language\nf\njacobdevlin,kentonl\ng\n@google.com\nAbstract"
        self.assertEqual(set(contacts_in_text(text).emails),
                         {"jacobdevlin@google.com", "kentonl@google.com"})

    def test_spaces_after_commas_allowed(self):
        from reachout.sources.documents import contacts_in_text
        text = "Microsoft Research\nf\nkahe, v-xiangz, jiansun\ng\n@microsoft.com\n"
        self.assertEqual(set(contacts_in_text(text).emails),
                         {"kahe@microsoft.com", "v-xiangz@microsoft.com",
                          "jiansun@microsoft.com"})

    def test_word_starting_with_f_does_not_start_a_group(self):
        """The 'f' in 'for' once swallowed an entire title block."""
        from reachout.sources.documents import contacts_in_text
        text = "Transformers for\nLanguage Understanding\nJacob Devlin\ng\n@google.com"
        for address in contacts_in_text(text).emails:
            self.assertNotIn(" ", address)
            self.assertLess(len(address), 64)

    def test_local_split_across_a_line_break_rejoins(self):
        from reachout.sources.documents import contacts_in_text
        self.assertIn("alice@dept.edu", contacts_in_text("alice\n@dept.edu").emails)

    def test_implausible_addresses_rejected(self):
        from reachout.sources.documents import contacts_in_text
        found = contacts_in_text("noise cw@l.id and g@x.com but real a.smith@dept.edu")
        self.assertIn("a.smith@dept.edu", found.emails)
        self.assertNotIn("cw@l.id", found.emails)
        self.assertNotIn("g@x.com", found.emails)


class TestRegionInference(unittest.TestCase):
    def test_tld_implies_country(self):
        self.assertEqual(phonelib.region_for_domain("mit.edu"), "US")
        self.assertEqual(phonelib.region_for_domain("iitb.ac.in"), "IN")
        self.assertEqual(phonelib.region_for_domain("bbc.co.uk"), "GB")
        self.assertEqual(phonelib.region_for_domain("example.gov"), "US")

    def test_neutral_tlds_use_the_fallback(self):
        self.assertEqual(phonelib.region_for_domain("acme.com", "IN"), "IN")
        self.assertEqual(phonelib.region_for_domain("acme.io", "US"), "US")
        self.assertEqual(phonelib.region_for_domain("", "IN"), "IN")

    def test_us_number_is_not_read_as_an_indian_mobile(self):
        """617-253-1000 on a .edu page must not become +91 61725 31000."""
        as_us = phonelib.normalise("617-253-1000", default_region="US")
        self.assertEqual(as_us.e164, "+16172531000")
        self.assertEqual(as_us.country, "US/CA")
        self.assertFalse(as_us.kind_certain)

    def test_harvester_uses_the_domain_region(self):
        from reachout.sources.website import _phones_from_page

        class FakeParser:
            tels = [("617-253-1000", "Contact", "MIT")]
            jsonld, microtels = [], []
        us = _phones_from_page(FakeParser(), "", region="US")
        self.assertEqual(us[0].raw, "+16172531000")
        india = _phones_from_page(FakeParser(), "", region="IN")
        self.assertEqual(india[0].raw, "+916172531000")


class TestPersonNames(unittest.TestCase):
    def test_honorific_stripped(self):
        self.assertEqual(person_names(["Dr. Ramesh Kumar"]), ["Ramesh Kumar"])
        self.assertEqual(person_names(["Shri Mohan Lal"]), ["Mohan Lal"])

    def test_job_titles_are_not_people(self):
        for title in ("Assistant Professor", "Vice President", "Staff Directory",
                      "Phone Number", "Dean Academics", "Research Scholar"):
            self.assertEqual(person_names([title]), [], title)

    def test_ordinary_names_survive(self):
        self.assertEqual(person_names(["Anita Desai"]), ["Anita Desai"])

    def test_brand_word_rejected(self):
        self.assertEqual(person_names(["Grafana Labs"], brand="grafana"), [])


class TestStaffDirectory(unittest.TestCase):
    """Row-scoped attribution: a number binds to a name only when the smallest
    enclosing block holds exactly one candidate name."""

    TABLE = """<table>
      <tr><th>Name</th><th>Designation</th><th>Phone</th></tr>
      <tr><td>Dr. Ramesh Kumar</td><td>Assistant Professor</td><td>0416-2202096</td></tr>
      <tr><td>Anita Desai</td><td>Associate Professor</td><td>0416-2202097</td></tr>
    </table>"""

    CARDS = """<div class="grid">
      <div class="card"><h3>Priya Nair</h3><p>Head, Physics</p>
        <a href="tel:+914162202188">0416 220 2188</a></div>
      <div class="card"><h3>Sanjay Rao</h3><p>Registrar</p>
        <a href="tel:+914162245544">0416 224 5544</a></div>
    </div>"""

    FLAT = ("<div>Ramesh Kumar 0416-2202096 Anita Desai 0416-2202097 "
            "Mukesh Verma 0416-2202098</div>")

    def _bind(self, html):
        return {f.raw: f for f in attribute_rows(rows_from_html(html), region="IN")}

    def test_table_row_binds_name_to_number(self):
        found = self._bind(self.TABLE)
        self.assertEqual(found["+914162202096"].owner, "Ramesh Kumar")
        self.assertEqual(found["+914162202097"].owner, "Anita Desai")
        self.assertEqual(found["+914162202096"].scope, "person")

    def test_header_row_is_not_a_person(self):
        self.assertEqual(len(self._bind(self.TABLE)), 2)

    def test_card_layout_binds_per_card(self):
        found = self._bind(self.CARDS)
        self.assertEqual(found["+914162202188"].owner, "Priya Nair")
        self.assertEqual(found["+914162245544"].owner, "Sanjay Rao")

    def test_flat_block_with_many_names_binds_nothing(self):
        """The whole point: proximity in one blob is not evidence. Attributing
        here is how a directory hands you your neighbour's number."""
        for found in self._bind(self.FLAT).values():
            self.assertEqual(found.owner, "")
            self.assertEqual(found.scope, "page")

    def test_oversized_block_is_not_a_row(self):
        filler = "lorem ipsum " * 60
        html = f"<div>Anita Desai {filler} 0416-2202097</div>"
        found = self._bind(html)
        self.assertTrue(all(f.owner == "" for f in found.values()))

    def test_directory_row_attributes_to_the_person(self):
        found = FoundPhone(raw="+914162202096", owner="Ramesh Kumar",
                           scope="person", how="directory")
        attribution, score = phonefind.attribute(found, parse("Ramesh Kumar"),
                                                 phonelib.normalise("+914162202096"))
        self.assertEqual(attribution, phonefind.PERSON)
        self.assertIn("directory_row", {s.key for s in score.signals})

    def test_directory_row_for_someone_else_does_not_attribute(self):
        found = FoundPhone(raw="+914162202096", owner="Ramesh Kumar",
                           scope="person", how="directory")
        attribution, score = phonefind.attribute(found, parse("Mukesh Verma"),
                                                 phonelib.normalise("+914162202096"))
        self.assertNotEqual(attribution, phonefind.PERSON)
        self.assertLessEqual(score.value, 20)


class TestDomainAffinity(unittest.TestCase):
    def test_exact_containment_is_corroboration(self):
        for company, domain in (("Grafana Labs", "grafana.com"), ("Zerodha", "zerodha.com"),
                                ("GreenFlow Ventures", "greenflow.ventures"),
                                ("VIT CHENNAI", "vit.ac.in")):
            self.assertEqual(name_affinity(company, domain), 1.0, domain)
            self.assertEqual(name_caveat(company, domain), "")

    def test_spelling_correction_is_flagged_as_a_near_miss(self):
        """A search for 'Whupps' returns whipps.com - a real, unrelated company.
        Character similarity is 0.83, which is why similarity alone cannot be
        the test."""
        self.assertGreater(name_affinity("Whupps", "whipps.com"), 0.7)
        self.assertIn("spelling correction", name_caveat("Whupps", "whipps.com"))

    def test_unrelated_domain_is_flagged_differently(self):
        self.assertIn("does not contain", name_caveat("Alphabet", "abc.xyz"))


class TestSightingStrength(unittest.TestCase):
    """The same number is seen several ways; the strongest sighting must be the
    one that survives dedupe, or a structural binding is silently thrown away."""

    def _trust(self, **kw):
        return phonefind._sighting_trust(FoundPhone(raw="+914162202072", **kw))

    def test_directory_beats_plain_text(self):
        self.assertGreater(self._trust(how="directory", owner="Manoj Kumar Kannan",
                                       scope="person"),
                           self._trust(how="text", scope="page"))

    def test_structured_data_beats_directory(self):
        self.assertGreater(self._trust(how="json-ld", owner="A B", scope="person"),
                           self._trust(how="directory", owner="A B", scope="person"))

    def test_user_supplied_outranks_everything(self):
        from reachout.db import USER_SOURCE
        self.assertGreater(self._trust(how="text", source_url=USER_SOURCE),
                           self._trust(how="json-ld", owner="A B", scope="person"))


class TestProximityVersusBinding(unittest.TestCase):
    """Real VIT page, real failure: a staff list prints

        ... Officer ir2@vit.ac.in +91-416-2202070
        Mr. Manoj Kumar Kannan  Officer  ir7@vit.ac.in  +91-416-2202072

    so the text window around ...070 reaches forward into the next entry and
    finds a name that belongs to the number *after* it."""

    CONTEXT = ("ternational Relations Officer international@vit.ac.in / ir2@vit.ac.in "
               "+91-416-2202070 Mr. Manoj Kumar Kannan International Re")

    def _attr(self, bound=""):
        found = FoundPhone(raw="+914162202070", context=self.CONTEXT, how="text")
        return phonefind.attribute(found, parse("Manoj Kumar Kannan"),
                                   phonelib.normalise("+914162202070"), bound=bound)

    def test_without_a_binding_proximity_still_attributes(self):
        attribution, _score = self._attr()
        self.assertEqual(attribution, phonefind.PERSON)

    def test_a_directory_binding_overrides_proximity(self):
        attribution, score = self._attr(bound="+914162202072")
        self.assertNotEqual(attribution, phonefind.PERSON)
        self.assertTrue(any("staff directory gives their number" in c
                            for c in score.caps), score.caps)
        self.assertLessEqual(score.value, 20)

    def test_the_bound_number_itself_is_unaffected(self):
        found = FoundPhone(raw="+914162202072", owner="Manoj Kumar Kannan",
                           scope="person", how="directory")
        attribution, score = phonefind.attribute(
            found, parse("Manoj Kumar Kannan"), phonelib.normalise("+914162202072"),
            bound="+914162202072")
        self.assertEqual(attribution, phonefind.PERSON)
        self.assertGreater(score.value, 55)


class TestHarvestDoesNotCorruptItself(unittest.TestCase):
    """Regressions from real @vit.ac.in and @cred.club data.

    Every case here was found in a live lookup, not invented: the samples they
    describe were sitting in the database and dragged that domain's leave-one-out
    accuracy to 0%.
    """

    def test_an_address_is_not_a_name(self):
        """'sasikumar.p@vit.ac.in' parsed as first='sasikumar', last='in'.

        Splitting an address on dots turns the TLD into a surname, and India's
        multi-label domains (.ac.in, .co.in) make it likelier still.
        """
        self.assertFalse(parse("sasikumar.p@vit.ac.in").ok)
        self.assertFalse(parse("Contact: hr@acme.co.in").ok)
        self.assertTrue(parse("Sasikumar P").ok)

    def test_stored_reading_can_rebuild_its_own_address(self):
        """The stored (first, last) must be the reading the template matched.

        matching_templates() searches every reading but reports only the template,
        so storing the default split filed 'sasikumar.p' under last='in' with
        template '{first}.{l}' - which renders 'sasikumar.i'.
        """
        for raw, local in [("Sasikumar P", "sasikumar.p"),
                           ("Maria Garcia Lopez", "maria.garcialopez")]:
            reading, template = patterns.matching_reading(local, parse(raw))
            rebuilt = patterns.render(template, reading)
            self.assertEqual(rebuilt, local,
                             f"{raw}: {template} on {reading.first}/{reading.last} "
                             f"rebuilt {rebuilt!r}, not {local!r}")

    def test_pair_samples_stores_a_rebuildable_row(self):
        pairs = pair_samples({"sasikumar.p@vit.ac.in": "Sasikumar P"},
                             ["Sasikumar P"], "vit.ac.in")
        self.assertEqual(len(pairs), 1)
        first, last, local, template = pairs[0]
        self.assertEqual(patterns.render(template, Name(raw="", first=first, last=last)), local)

    def test_anchor_text_passes_the_same_filter_as_any_other_name(self):
        """Role headings reached the sample store through the mailto anchor text.

        person_names() already rejects all of these; pair_samples() was not asking it.
        """
        for anchor, address in [("Finance Officer", "fo@vit.ac.in"),
                                ("Placement Highlights", "placement@vit.ac.in"),
                                ("Vellore Campus", "vc@vit.ac.in"),
                                ("International Projects", "international@vit.ac.in"),
                                ("sasikumar.p@vit.ac.in", "sasikumar.p@vit.ac.in")]:
            self.assertEqual(pair_samples({address: anchor}, [anchor], "vit.ac.in"), [],
                             f"{anchor!r} was learned as a person")

    def test_a_bare_domain_is_not_a_name(self):
        """Developers put their own site in the git display name.

        'rohankokane.dev' read as first='rohankokane', last='dev' and was paired
        against rohan.kokane@razorpay.com.
        """
        self.assertFalse(parse("rohankokane.dev").ok)
        self.assertFalse(parse("johnsmith.com").ok)
        # ...without catching real surnames or dotted handles.
        self.assertEqual((parse("Kapil Dev").first, parse("Kapil Dev").last), ("kapil", "dev"))
        self.assertEqual((parse("john.smith").first, parse("john.smith").last), ("john", "smith"))

    def test_machine_accounts_are_role_accounts(self):
        for address in ("security-svc@razorpay.com", "deploy-bot@acme.com",
                        "jenkins-ci@acme.com", "ship-help-github@redhat.com"):
            self.assertTrue(is_role_account(address), address)
        for address in ("nithin.k@zerodha.com", "anurag.hazra@razorpay.com"):
            self.assertFalse(is_role_account(address), address)

    def test_institutional_role_mailboxes_are_recognised(self):
        for local in ("vc", "fo", "hod", "tpo", "placement", "registrar", "warden"):
            self.assertTrue(is_role_account(f"{local}@vit.ac.in"), local)
        self.assertFalse(is_role_account("nithin.k@zerodha.com"))


class TestObservedAttribution(unittest.TestCase):
    def _verdict(self, address, **kw):
        v = verify.Verdict(address=address)
        v.syntax_ok, v.has_mx, v.domain_resolves = True, True, True
        for key, value in kw.items():
            setattr(v, key, value)
        return v

    def test_published_address_unrelated_to_the_name_is_not_high(self):
        """'oxygen@cred.club' scored 82 HIGH for Kunal Shah.

        The address is real, and it really was published from his own account -
        but nothing connects that mailbox to him personally. Seeing an address on
        a page about someone proves the address exists, not that it is theirs.
        """
        name = parse("Kunal Shah")
        candidate = patterns.Candidate("oxygen@cred.club", "", 0.0, "observed")
        score = scoring.score_candidate(candidate, self._verdict("oxygen@cred.club"), name,
                                        observed_sources=["https://x.com/kunalb11/status/1"])
        self.assertNotEqual(score.grade, "HIGH")
        self.assertIn("observed_unrelated", {s.key for s in score.signals})

    def test_published_address_carrying_the_name_still_ranks_high(self):
        """The fix must not punish the case it was built to reward."""
        name = parse("Torkel Odegaard")
        candidate = patterns.Candidate("torkel@grafana.com", "{first}", 0.2, "observed")
        score = scoring.score_candidate(candidate, self._verdict("torkel@grafana.com"), name,
                                        observed_sources=["https://github.com/grafana/x"])
        self.assertEqual(score.grade, "HIGH")
        self.assertNotIn("observed_unrelated", {s.key for s in score.signals})

    def test_smtp_cannot_lift_an_unattributable_sighting_to_high(self):
        name = parse("Kunal Shah")
        candidate = patterns.Candidate("oxygen@cred.club", "", 0.0, "observed")
        score = scoring.score_candidate(candidate,
                                        self._verdict("oxygen@cred.club", smtp="accepted"), name,
                                        observed_sources=["https://x.com/kunalb11/status/1"])
        self.assertLessEqual(score.value, 72)


if __name__ == "__main__":
    unittest.main(verbosity=2)
