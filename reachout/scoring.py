"""Turn evidence into a number, and keep the reasons attached to it.

Design rule: a score is never a bare percentage. Every point is traceable to a
named signal, so the output can always explain itself and you can tell a strong
guess apart from a weak one at a glance.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from . import names

GRADES = (
    (80, "HIGH"),
    (55, "MEDIUM"),
    (30, "LOW"),
    (0, "VERY LOW"),
)


@dataclass
class Signal:
    key: str
    label: str
    points: float
    source_url: str = ""

    @property
    def sign(self) -> str:
        if self.points > 0:
            return "+"
        return "-" if self.points < 0 else " "


@dataclass
class Score:
    value: int = 0
    grade: str = "VERY LOW"
    signals: list = field(default_factory=list)
    caps: list = field(default_factory=list)

    def add(self, key: str, label: str, points: float, source_url: str = "") -> None:
        if points:
            self.signals.append(Signal(key, label, points, source_url))

    @property
    def positives(self) -> list:
        return [s for s in self.signals if s.points > 0]

    @property
    def negatives(self) -> list:
        return [s for s in self.signals if s.points < 0]


def grade_for(value: int) -> str:
    for threshold, label in GRADES:
        if value >= threshold:
            return label
    return "VERY LOW"


def score_candidate(candidate, verdict, name, observed_sources: list | None = None,
                    person_match: float = 0.0, pattern_samples: int = 0,
                    user_confirmed: bool = False) -> Score:
    """Score one candidate address.

    candidate        - patterns.Candidate (carries the template and its prior)
    verdict          - verify.Verdict for the address
    name             - names.Name of the person
    observed_sources - public URLs where this exact address was seen
    person_match     - 0-1 confidence that the profile we found is the right person
    pattern_samples  - how many real samples backed the domain pattern
    user_confirmed   - you told us this exact address is real
    """
    score = Score()
    observed_sources = observed_sources or []
    local = candidate.address.split("@")[0]
    # Groups, not a flat list: an umlaut name has two ASCII spellings and the
    # local part will carry one of them, so matching is any-within-group and
    # all-across-groups. A flat list marked juergen@ as unrelated to Jürgen.
    name_groups = names.token_groups(name)
    name_tokens = [g[0] for g in name_groups]
    shares_name = any(any(t in local for t in g) for g in name_groups)
    full_name_in_local = bool(name_groups) and all(
        any(t in local for t in g) for g in name_groups)

    if user_confirmed:
        # You vouched for this address. Nothing available here beats that.
        score.add("confirmed", "you confirmed this exact address yourself", 80)
    elif observed_sources:
        # Seeing the literal address published somewhere public outranks any guess.
        primary = observed_sources[0]
        score.add("observed", "address found published on a public source", 74, primary)
        if len(observed_sources) > 1:
            score.add("observed_multi",
                      f"seen on {len(observed_sources)} independent sources", 8,
                      observed_sources[1])
        if person_match >= 0.6:
            score.add("profile_match",
                      f"source profile matches the person ({person_match:.0%})", 8)
        elif person_match and person_match < 0.4:
            score.add("weak_profile",
                      f"source profile is a weak match for the person ({person_match:.0%})", -12)
        # Finding an address on a page about someone shows the address exists, not
        # that it is theirs. A campaign mailbox published from a founder's own
        # account is a real address at the right company and still reaches a team.
        # With no part of the name in the local part, the sighting cannot attribute.
        if name_tokens and not shares_name and not verdict.is_role:
            score.add("observed_unrelated",
                      "local part bears no relation to the person's name", -30)
    elif candidate.source == "pinned":
        score.add("pinned",
                  f"you told me this company uses {candidate.template}",
                  round(candidate.prior * 68, 1))
        if pattern_samples:
            score.add("pinned_backed",
                      f"and {pattern_samples} real address(es) here match it", 8)
    elif candidate.source == "domain-pattern":
        score.add("pattern",
                  f"matches this company's naming pattern ({candidate.template}, "
                  f"learned from {pattern_samples} real addresses)",
                  round(candidate.prior * 68, 1))
        # Depth only reinforces a template that actually dominates the domain;
        # otherwise a big sample set would inflate every weak alternative too.
        if candidate.prior >= 0.4:
            if pattern_samples >= 15:
                score.add("pattern_depth", f"pattern backed by {pattern_samples} real addresses", 10)
            elif pattern_samples >= 5:
                score.add("pattern_depth", f"pattern backed by {pattern_samples} real addresses", 6)
            elif pattern_samples <= 1:
                score.add("pattern_thin", "pattern inferred from a single sample", -6)
    else:
        score.add("prior",
                  f"common email format ({candidate.template}) - no company pattern known",
                  round(candidate.prior * 45, 1))

    # --- deliverability signals -------------------------------------------------
    if verdict.has_mx:
        score.add("mx", f"domain accepts mail (MX present{', ' + verdict.provider if verdict.provider else ''})", 8)
    elif verdict.domain_resolves:
        score.add("dns", "domain resolves but has no MX records", -20)
    else:
        score.add("nodns", "domain does not resolve", -45)

    if verdict.smtp == "accepted":
        score.add("smtp", "mail server accepted the recipient (not a catch-all domain)", 22)
    elif verdict.smtp == "rejected":
        score.add("smtp_reject", "mail server rejected this recipient", -70)
    elif verdict.smtp == "blocked":
        score.add("smtp_blocked", "SMTP check could not run (port 25 blocked)", 0)

    # --- shape of the local part ------------------------------------------------
    if full_name_in_local:
        score.add("name_tokens", "local part contains the person's full first and last name", 6)
    elif shares_name:
        score.add("name_token", "local part contains part of the person's name", 3)

    if verdict.is_role:
        score.add("role", "shared/role mailbox - reaches a team, not this person", -28)
    if verdict.is_free:
        score.add("free", "consumer mailbox rather than a company address", -8)
    if verdict.is_disposable:
        score.add("disposable", "disposable-mail domain", -45)

    total = sum(s.points for s in score.signals)

    # --- ceilings we refuse to score past ---------------------------------------
    # Only recorded when they actually bind, so a cap line always means something.
    def cap(limit: float, reason: str) -> None:
        nonlocal total
        if total > limit:
            total = limit
            score.caps.append(reason)

    if observed_sources and name_tokens and not shares_name:
        cap(72, "capped at 72: the address is published, but its local part bears no "
                "relation to this person's name")
    if verdict.catch_all == 1:
        cap(72, "capped at 72: domain is catch-all, so no probe can confirm the mailbox")
    if not verdict.has_mx:
        cap(30, "capped at 30: the domain cannot receive mail")
    if verdict.smtp != "accepted":
        if user_confirmed:
            cap(96, "capped at 96: you confirmed it, but no mail server has been asked")
        elif observed_sources:
            cap(93, "capped at 93: published somewhere public, but not confirmed by a mail server")
        else:
            # HIGH is reserved for evidence about this *person*. Knowing how a company
            # builds addresses - however certainly - says nothing about whether this
            # individual has a mailbox there, so a pure inference stops at MEDIUM.
            cap(79, "capped at 79: the format is known, but nothing confirms this person "
                    "has an address here")
    if verdict.smtp == "rejected":
        total = 0
        score.caps.append("zeroed: the mail server said this recipient does not exist")
    if not verdict.syntax_ok:
        total = 0
        score.caps.append("zeroed: not a valid address")

    score.value = max(0, min(100, int(round(total))))
    score.grade = grade_for(score.value)
    return score
