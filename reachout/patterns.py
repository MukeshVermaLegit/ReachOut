"""Email naming-pattern inference.

The single highest-leverage idea in the tool. Instead of guessing one person's
address, learn how a *company* builds addresses from a handful of publicly known
ones, then apply that rule to everyone else there.

A template is a string over the tokens {first} {last} {middle} {f} {l} {m} plus
literal separators, e.g. "{f}{last}" -> jsmith.

Two extensions cover the legacy-enterprise conventions that whole-token
concatenation cannot express:

  {first:2}   the first N characters of a token, so {f} is {first:1} written
              short. Red Hat's 'dalong' (dakota long) needs N=2.
  ~8          a cap on the whole rendered local part. Sites that issue classic
              8-character UNIX/Kerberos logins truncate: florian bergmann is
              'fbergman', not 'fbergmann'.

The two compose - "{first:2}{last}~8" renders tim williams as 'tiwillia'.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from .names import Name, variants

# Ordered by how common each format is in the wild. The weights are a prior used
# when a domain has no samples yet, and as shrinkage when it has very few.
GLOBAL_PRIOR: dict[str, float] = {
    "{first}.{last}": 0.34,
    "{f}{last}": 0.19,
    "{first}": 0.11,
    "{first}{last}": 0.08,
    "{first}_{last}": 0.05,
    "{f}.{last}": 0.045,
    "{first}{l}": 0.03,
    "{last}.{first}": 0.02,
    "{last}{f}": 0.02,
    "{first}-{last}": 0.015,
    "{last}": 0.015,
    "{f}{m}{last}": 0.01,
    "{first}.{m}.{last}": 0.01,
    "{first}{m}{last}": 0.01,
    "{last}{first}": 0.01,
    "{f}{l}": 0.008,
    "{last}_{first}": 0.005,
    "{last}-{first}": 0.005,
    "{first}.{l}": 0.005,
    "{m}{first}{last}": 0.002,
    # Legacy 8-character-login conventions. Rare in the wild, so the priors stay
    # low and only a domain that actually demonstrates one will rank it.
    "{f}{last}~8": 0.006,
    "{first:2}{last}": 0.005,
    "{first:2}{last}~8": 0.004,
    "{first:3}{last}": 0.003,
    "{first:3}{last}~8": 0.002,
}

TEMPLATES: list[str] = list(GLOBAL_PRIOR)

_TOKEN_RE = re.compile(r"\{(first|last|middle|f|l|m)(?::(\d+))?\}")
_LOCAL_OK = re.compile(r"^[a-z0-9](?:[a-z0-9._+-]{0,62}[a-z0-9])?$")
# '~' cannot appear in a local part, so a cap can never collide with real data.
_CAP_RE = re.compile(r"~(\d+)$")

_SEPARATORS = ".-_+"
_PIECE_RE = re.compile(r"\{(first|last|middle|f|l|m)(?::(\d+))?\}|([^{}]+)")
_WORD_TOKENS = {"first", "last", "middle"}


def split_cap(template: str) -> tuple[str, int]:
    """Separate a template from its trailing length cap: '{f}{last}~8' -> ('{f}{last}', 8)."""
    match = _CAP_RE.search(template or "")
    if not match:
        return template or "", 0
    return template[:match.start()], int(match.group(1))


def template_shape(template: str):
    """Structural signature of a template, ignoring the actual name.

    '{first}.{last}' -> segments ((0,), (0,)) with separators ('.',), where a slot
    of 0 is a whole word (two characters minimum) and a slot of N is exactly N
    characters. Returned as (separators, segments).

    A capped template returns None. Truncation destroys the very length
    relationship this signature encodes, so a capped form is credible only when a
    real name reproduces it exactly - it takes no part in shape-only voting.
    """
    body, cap = split_cap(template)
    if cap:
        return None
    segments: list[list[int]] = [[]]
    separators: list[str] = []
    for match in _PIECE_RE.finditer(body):
        token, width, literal = match.group(1), match.group(2), match.group(3)
        if token:
            if width:
                segments[-1].append(int(width))
            else:
                segments[-1].append(0 if token in _WORD_TOKENS else 1)
        else:
            for char in literal:
                if char in _SEPARATORS:
                    separators.append(char)
                    segments.append([])
                else:
                    return None  # unexpected literal - treat as unshapeable
    return (tuple(separators), tuple(tuple(s) for s in segments))


def local_shape(local: str) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """Split a local-part into its separator sequence and its segments."""
    separators: list[str] = []
    segments: list[str] = [""]
    for char in local:
        if char in _SEPARATORS:
            separators.append(char)
            segments.append("")
        else:
            segments[-1] += char
    return tuple(separators), tuple(segments)


def shape_compatible(local: str, template: str) -> bool:
    """Could `template` have produced a local-part of this shape, for *some* name?

    Used when a person's recorded name is unusable (a GitHub login instead of a
    real name, an initial instead of a first name). The structure of the address
    still carries information about the domain's format.
    """
    tshape = template_shape(template)
    if tshape is None:
        return False
    tseps, tsegs = tshape
    lseps, lsegs = local_shape(local)
    if tseps != lseps or len(tsegs) != len(lsegs):
        return False
    for want, got in zip(tsegs, lsegs):
        if not got:
            return False
        fixed = sum(slot for slot in want if slot)
        words = sum(1 for slot in want if not slot)
        if words == 0:
            if len(got) != fixed:
                return False
        elif len(got) < fixed + 2 * words:
            return False
    return True



def is_template(template: str) -> bool:
    """Does this string actually name a part of the name?

    A literal with no tokens renders to itself, so pinning one would give every
    person at a company the same address. Checking `render()` is not enough to
    catch that - it happily returns the literal.
    """
    return bool(_TOKEN_RE.search(template or ""))


def render(template: str, name: Name) -> str:
    """Fill a template from a name. Returns '' when a needed token is missing."""
    body, cap = split_cap(template)
    values = {
        "first": name.first, "last": name.last, "middle": name.middle,
        "f": name.f, "l": name.l, "m": name.m,
    }
    missing = False

    def sub(match: re.Match) -> str:
        nonlocal missing
        value = values.get(match.group(1), "")
        if not value:
            missing = True
            return ""
        width = match.group(2)
        return value[:int(width)] if width else value

    out = _TOKEN_RE.sub(sub, body)
    if missing or not out:
        return ""
    out = out.lower()
    if cap:
        out = out[:cap]
        # A cap that lands on a separator leaves a local part no mailbox uses.
        out = out.rstrip(_SEPARATORS)
    return out


def matching_templates(local: str, name: Name, pool: list[str] | None = None) -> list[str]:
    """Every template that reproduces `local` for any reading of `name`."""
    local = (local or "").lower().strip()
    if not local:
        return []
    found: list[str] = []
    for template in (pool or TEMPLATES):
        for candidate_name in variants(name):
            if render(template, candidate_name) == local:
                found.append(template)
                break
    return found


def matching_reading(local: str, name: Name,
                     pool: list[str] | None = None) -> tuple[Name, str] | None:
    """The (reading, template) pair that actually reproduces `local`.

    matching_templates() searches every reading of a name but reports only the
    template. Anything that *stores* a sample needs the reading too: filing the
    template against the wrong first/last writes a row that cannot rebuild its
    own address, which then teaches the domain a pattern nobody uses.
    """
    local = (local or "").lower().strip()
    if not local:
        return None
    for template in (pool or TEMPLATES):
        for candidate_name in variants(name):
            if render(template, candidate_name) == local:
                return candidate_name, template
    return None


@dataclass
class PatternGuess:
    template: str
    votes: float
    samples: int
    confidence: float
    pinned: bool = False   # stated by the user rather than inferred from samples

    @property
    def example(self) -> str:
        return render(self.template, Name(raw="", first="jane", last="doe", middle="ann"))

    def as_tuple(self) -> tuple[str, int, int, float]:
        return (self.template, int(round(self.votes)), self.samples, self.confidence)


SHRINKAGE = 1.5      # pseudo-samples pulling a thin domain back toward the global prior
SHAPE_WEIGHT = 0.5   # a shape-only match is worth half an exact name match


def infer(samples: list[tuple[str, str, str]]) -> list[PatternGuess]:
    """Rank templates for a domain.

    `samples` is a list of (first, last, local) observed at that domain.

    Two kinds of evidence are combined:
      * exact  - the template reproduces the local-part from the person's name.
                 One vote, split across every template that explains it.
      * shape  - the name is unusable (a login, a bare initial) but the address
                 structure still fits the template. Counts for half a vote.
    """
    usable = [(f, l, loc) for f, l, loc in samples if f and l and loc]
    if not usable:
        return []

    votes: dict[str, float] = {}
    counted = 0
    for first, last, local in usable:
        name = Name(raw="", first=first, last=last)
        hits = matching_templates(local, name)
        if hits:
            share = 1.0 / len(hits)
            for template in hits:
                votes[template] = votes.get(template, 0.0) + share
            counted += 1
            continue
        fits = [t for t in TEMPLATES if shape_compatible(local, t)]
        if not fits:
            continue
        share = SHAPE_WEIGHT / len(fits)
        for template in fits:
            votes[template] = votes.get(template, 0.0) + share
        counted += 1

    if not counted:
        return []

    guesses = []
    for template, vote in votes.items():
        prior = GLOBAL_PRIOR.get(template, 0.001)
        confidence = (vote + SHRINKAGE * prior) / (counted + SHRINKAGE)
        guesses.append(PatternGuess(template, vote, counted, round(confidence, 4)))
    guesses.sort(key=lambda g: (-g.confidence, -g.votes,
                                TEMPLATES.index(g.template) if g.template in TEMPLATES else 99))
    return guesses


def coverage(guesses: list[PatternGuess]) -> float:
    """How much of the domain's behaviour the top template explains (0-1)."""
    if not guesses:
        return 0.0
    return guesses[0].confidence


# A format actually observed at this company outranks a format that is merely
# common everywhere. Applied to ordering only - never to the prior that feeds
# scoring, so evidence strength is not inflated.
DOMAIN_RANK_BONUS = 0.05


@dataclass
class Candidate:
    address: str
    template: str
    prior: float          # 0-1 belief from pattern evidence alone
    source: str           # 'domain-pattern' | 'global-prior' | 'observed'

    @property
    def rank(self) -> float:
        return self.prior + (DOMAIN_RANK_BONUS
                             if self.source in ("domain-pattern", "pinned") else 0.0)


# An alternate reading of the name is a real possibility but a weaker bet than
# the way the name was actually written, so it never outranks the primary.
READING_DAMP = 0.55


def generate(name: Name, domain: str, guesses: list[PatternGuess] | None = None,
             limit: int = 8) -> list[Candidate]:
    """Build ranked candidate addresses for a person at a domain."""
    domain = (domain or "").lower().lstrip("@").strip()
    if not domain or not name.first:
        return []

    # Recognition has always searched every reading of a name; generation used
    # to render the primary one only. That is why 'P. Sasikumar' could never
    # produce sasikumar.p@ - the reading existed, it was just never built.
    readings = variants(name)
    out: list[Candidate] = []
    seen: set[str] = set()

    def push(template: str, prior: float, source: str) -> None:
        for index, reading in enumerate(readings):
            local = render(template, reading)
            if not local or not _LOCAL_OK.match(local) or len(local) > 64:
                continue
            address = f"{local}@{domain}"
            if address in seen:
                continue
            seen.add(address)
            # A spelling variant is the same structural reading, so it keeps the
            # full prior; only competing surname/initial splits are damped.
            damped = index > 0 and not reading.spelling_variant
            weight = prior * READING_DAMP if damped else prior
            out.append(Candidate(address, template, round(min(weight, 0.99), 4), source))

    if guesses:
        # A confident domain pattern dominates; weaker ones still get a look-in.
        for guess in guesses:
            if guess.confidence < 0.05:
                continue
            push(guess.template, guess.confidence, "pinned" if guess.pinned else "domain-pattern")

    # Always keep a few global fallbacks so a wrong/thin pattern is not a dead end.
    # Measure the domain's evidence as the mass its templates explain *together*,
    # not by its single best one. Near-duplicate templates split one domain's votes
    # between them - the {f}{last} / {f}{last}~8 pair renders identically for every
    # short surname - so the top template alone understates a domain that is in
    # fact well understood, and the fallbacks then crowd out its real alternates.
    explained = min(1.0, sum(g.confidence for g in guesses
                             if g.confidence >= 0.05)) if guesses else 0.0
    damp = 0.45 if explained >= 0.75 else 1.0
    for template, prior in GLOBAL_PRIOR.items():
        push(template, prior * damp, "global-prior")

    out.sort(key=lambda c: -c.rank)
    return out[:limit]


def describe(template: str) -> str:
    """Human label: '{f}{last}' -> 'jsmith (first initial + last)'."""
    # The surname has to be long enough for a capped template to visibly truncate,
    # and the whole label short enough for the 40-column table the CLI prints it in.
    sample = render(template, Name(raw="", first="jon", middle="quincy",
                                   last="smithers"))
    body, cap = split_cap(template)
    words = {
        "{first}.{last}": "first.last", "{f}{last}": "first initial + last",
        "{first}": "first name only", "{first}{last}": "firstlast",
        "{first}_{last}": "first_last", "{f}.{last}": "f.last",
        "{first}{l}": "first + last initial", "{last}.{first}": "last.first",
        "{last}{f}": "last + first initial", "{first}-{last}": "first-last",
        "{last}": "last name only", "{f}{l}": "initials",
        "{first:2}{last}": "first 2 + last",
        "{first:3}{last}": "first 3 + last",
    }
    label = words.get(body, body)
    if cap:
        label = f"{label}, max {cap}"
    return f"{sample} ({label})" if sample else label
