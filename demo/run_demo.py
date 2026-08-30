#!/usr/bin/env python3
"""Run the phone-enrichment pipeline against the synthetic corpus.

    python3 demo/run_demo.py            # all cases
    python3 demo/run_demo.py --json     # machine-readable
    python3 demo/run_demo.py amara      # one case

No network, no real people. The database is a throwaway file in a temp
directory, so nothing here touches your own.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
os.environ.setdefault("REACHOUT_HOME", tempfile.mkdtemp(prefix="reachout-demo-"))

from demo import synthetic                                    # noqa: E402
from demo.fake_web import install                             # noqa: E402
from reachout import enrich as enrich_mod                     # noqa: E402
from reachout.cli import print_enrichment                     # noqa: E402
from reachout.db import Database                              # noqa: E402

# (label, name, work email, what should happen and why)
CASES = [
    ("amara", "Amara Okafor", "amara.okafor@northwind.example",
     "found — her mobile is on a speaker page beside the work address we were "
     "given, and her direct dial is bound to her row in the staff directory"),
    ("priya", "Priya Raman", "priya.raman@northwind.example",
     "found — direct dial only. The row above hers belongs to Amara, and the "
     "text window that spans both is refused rather than guessed at"),
    ("tobias", "Tobias Lindqvist", "t.lindqvist@northwind.example",
     "not_found — he is on the team page and publishes no number anywhere. "
     "This is the normal outcome and it is a result, not a failure"),
    ("namesake", "Amara Okafor", "",
     "the same person without the work email. A ceramicist of the same name "
     "publishes a number on her own site; with no link to the employer it is "
     "not allowed to reach the reporting bar"),
]


def run(selected: list[str], as_json: bool) -> int:
    web = synthetic.web()
    output = []
    with install(web):
        with Database() as db:
            # A real first run resolves the company by search; the corpus has no
            # search engine to do that, so the mapping is seeded directly.
            db.upsert_company(synthetic.COMPANY, synthetic.DOMAIN, 0.95, "demo fixture")
            for label, name, email, expectation in CASES:
                if selected and label not in selected:
                    continue
                lookup = enrich_mod.enrich(
                    name, synthetic.COMPANY, email, db=db, region=synthetic.REGION,
                    # The corpus has no GitHub, and a live call to it would make
                    # the demo depend on the network it is built to avoid.
                    exclude=["personal-profiles"])
                if as_json:
                    output.append({"case": label, "expectation": expectation,
                                   **lookup.as_dict()})
                    continue
                print("\n" + "=" * 78)
                print(f"  case: {label}   ({expectation})")
                print("=" * 78)
                print_enrichment(lookup)
    if as_json:
        print(json.dumps(output, indent=2))
    else:
        print("\n" + "-" * 78)
        print(f"  {len(web.requested)} page fetches, {len(web.searched)} searches - "
              f"all served from demo/synthetic.py, none from the network.")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("case", nargs="*", default=[],
                        help=f"one or more of: {', '.join(c[0] for c in CASES)}")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()
    return run(args.case, args.json)


if __name__ == "__main__":
    raise SystemExit(main())
