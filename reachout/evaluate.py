"""Measure how well the guesser actually works, using data you already have.

Leave-one-out: for each real (name -> address) pair a domain has, re-infer the
pattern from *every other* pair and see whether the engine would have guessed
this person correctly. No network, no cost, and it answers the only question
that matters - what is my hit rate?
"""

from __future__ import annotations

from dataclasses import dataclass, field

from . import patterns
from .db import Database
from .names import Name


@dataclass
class DomainEval:
    domain: str
    trials: int = 0
    top1: int = 0
    top3: int = 0
    missed: list = field(default_factory=list)

    @property
    def top1_rate(self) -> float:
        return self.top1 / self.trials if self.trials else 0.0

    @property
    def top3_rate(self) -> float:
        return self.top3 / self.trials if self.trials else 0.0


def evaluate_domain(domain: str, db: Database, min_samples: int = 3) -> DomainEval | None:
    rows = db.samples_for_domain(domain)
    triples = [(r["first"], r["last"], r["local"]) for r in rows]
    if len(triples) < min_samples:
        return None

    report = DomainEval(domain=domain)
    for index, (first, last, actual) in enumerate(triples):
        held_out = triples[:index] + triples[index + 1:]
        guesses = patterns.infer(held_out)
        # Carry the tokens, so generate() sees the same readings it would see
        # from a parsed name. Without them variants() is inert and the harness
        # measures a code path find() never takes.
        name = Name(raw=f"{first} {last}", first=first, last=last,
                    tokens=[first, last])
        candidates = patterns.generate(name, domain, guesses, limit=3)
        locals_ = [c.address.split("@")[0] for c in candidates]
        report.trials += 1
        if locals_ and locals_[0] == actual:
            report.top1 += 1
            report.top3 += 1
        elif actual in locals_:
            report.top3 += 1
        else:
            report.missed.append((f"{first} {last}", actual, locals_[0] if locals_ else "-"))
    return report


def evaluate_all(db: Database, min_samples: int = 3) -> list[DomainEval]:
    rows = db.conn.execute(
        "SELECT domain, COUNT(*) n FROM pattern_samples GROUP BY domain HAVING n >= ?"
        " ORDER BY n DESC", (min_samples,)
    ).fetchall()
    out = []
    for row in rows:
        report = evaluate_domain(row["domain"], db, min_samples)
        if report:
            out.append(report)
    return out
