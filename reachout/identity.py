"""Who the subject is, and whether a page is talking about *them*.

Adding a work email to the query changes the problem. A name is a weak
identifier - "Amara Okafor" matches every Amara Okafor alive - but a work email
is an identifier the subject's employer issued to exactly one person. When that
address is printed next to a number, the binding is no longer an inference; the
page is stating the association itself.

So this module does two jobs:

  1. Build a `Subject` from (name, company, work email): the domain comes from
     the address rather than a search, and the local part is checked against the
     name so a mistyped or borrowed address is caught before anything is crawled.

  2. Decide, for one block of text, how strongly it refers to that subject.
     `Mention` reports what was found rather than a bare number, because the
     caller has to be able to explain a refusal as well as an answer.

Nothing here reaches the network.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from . import patterns
from . import phones as phonelib
from .names import Name, parse as parse_name, slug

EMAIL_RE = re.compile(
    r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9](?:[A-Za-z0-9.\-]*[A-Za-z0-9])?\.[A-Za-z]{2,24}")

# A local part shorter than this identifies nothing. "ir7", "hr", "pa2" are real
# institutional mailboxes and matching text on them attributes numbers at random.
MIN_DISTINCTIVE_LOCAL = 4


def name_presence(text: str, name: Name) -> str:
    """'full' | 'last' | '' - how strongly a person's name appears in some text."""
    key = slug(text)
    if not key:
        return ""
    if name.first and name.last and name.first in key and name.last in key:
        return "full"
    if name.last and len(name.last) > 3 and name.last in key:
        return "last"
    return ""


@dataclass
class Mention:
    """What one block of text says about the subject.

    Deliberately a record of findings rather than a score: 'their name is here
    but somebody else's work address is too' and 'nothing matched' are different
    refusals, and only the first one is worth reporting back.
    """

    email_exact: bool = False       # the subject's own work address is printed here
    email_local: bool = False       # only the distinctive local part appears
    name: str = ""                  # 'full' | 'last' | ''
    company: bool = False           # the employer's name or brand appears
    domain: bool = False            # some address at the employer's domain appears
    rival_email: str = ""           # a *different* personal address at that domain

    @property
    def identifies(self) -> bool:
        """True when this block names the subject specifically, not just plausibly."""
        return self.email_exact or (self.name == "full" and not self.rival_email)

    @property
    def strength(self) -> float:
        """0-1 summary, for ranking blocks against each other."""
        if self.email_exact:
            value = 1.0
        elif self.name == "full" and self.email_local:
            value = 0.85
        elif self.name == "full":
            value = 0.6
        elif self.email_local:
            value = 0.5
        elif self.name == "last":
            value = 0.25
        else:
            value = 0.0
        if self.rival_email and not self.email_exact:
            value = min(value, 0.2)
        if self.company or self.domain:
            value = min(1.0, value + 0.05)
        return round(value, 2)

    def describe(self) -> str:
        parts = []
        if self.email_exact:
            parts.append("their work address is printed here")
        elif self.email_local:
            parts.append("the distinctive part of their work address appears here")
        if self.name == "full":
            parts.append("their full name appears here")
        elif self.name == "last":
            parts.append("only their surname appears here")
        if self.rival_email:
            parts.append(f"but so does {self.rival_email}, which is somebody else's")
        return "; ".join(parts) or "nothing here refers to this person"


@dataclass
class Subject:
    """The person being looked up, plus everything derivable about them offline."""

    name: Name
    company: str = ""
    work_email: str = ""
    domain: str = ""
    local: str = ""
    region: str = "IN"

    email_valid: bool = False
    email_is_role: bool = False
    name_email_template: str = ""    # the format that renders local from the name
    aliases: list = field(default_factory=list)
    problems: list = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return bool(self.name.first)

    @property
    def brand(self) -> str:
        return self.domain.split(".")[0] if self.domain else slug(self.company)

    @property
    def key(self) -> str:
        """Stable identity for caching and for the lookup audit trail."""
        return f"{self.name.first}.{self.name.last}@{self.domain or slug(self.company)}"

    @property
    def email_is_distinctive(self) -> bool:
        """Can the local part identify this person on its own?

        'a.okafor' can. 'hr', 'info' and '2021ug042' cannot - the first two reach
        a team and the third appears in a hundred unrelated rows.
        """
        if not self.email_valid or self.email_is_role:
            return False
        bare = self.local.replace(".", "").replace("-", "").replace("_", "")
        if len(bare) < MIN_DISTINCTIVE_LOCAL or bare.isdigit():
            return False
        # A local part that shares nothing with the name is a mailbox we cannot
        # tie back to the person, so text matching on it proves nothing.
        return bool(self.name_email_template) or any(
            token and len(token) > 3 and token in bare
            for token in (self.name.first, self.name.last))

    def as_dict(self) -> dict:
        return {
            "name": self.name.display, "company": self.company,
            "work_email": self.work_email, "domain": self.domain,
            "region": self.region, "email_valid": self.email_valid,
            "email_is_role": self.email_is_role,
            "name_email_template": self.name_email_template,
            "email_is_distinctive": self.email_is_distinctive,
            "problems": list(self.problems),
        }

    # ------------------------------------------------------------------ matching
    def emails_in(self, text: str) -> list[str]:
        return [m.group(0).lower().strip(".,;:") for m in EMAIL_RE.finditer(text or "")]

    def mentions(self, *chunks: str) -> Mention:
        """How strongly the given text refers to this subject."""
        from .sources.website import is_role_account

        text = " ".join(c for c in chunks if c)
        found = Mention()
        if not text:
            return found

        found.name = name_presence(text, self.name)
        if self.company and slug(self.company) and slug(self.company) in slug(text):
            found.company = True
        if self.brand and len(self.brand) > 2 and self.brand in slug(text):
            found.company = True

        addresses = self.emails_in(text)
        for address in addresses:
            local, _, domain = address.partition("@")
            if self.work_email and address == self.work_email:
                found.email_exact = True
                continue
            if not self.domain or domain != self.domain:
                continue
            found.domain = True
            if is_role_account(address):
                continue        # info@ belongs to nobody, so it rules nothing out
            # A personal mailbox at the same employer that is not the subject's.
            # When their address is known, this is definitively a colleague's and
            # is the clearest sign the block is somebody else's entry. When it is
            # not known, an address the subject's own name would produce is more
            # likely theirs than a colleague's, so it corroborates instead.
            if not self.work_email and self.name.ok and \
                    patterns.matching_templates(local, self.name):
                found.email_local = True
            elif not found.rival_email:
                found.rival_email = address

        if not found.email_exact and self.email_is_distinctive:
            local_key = slug(self.local)
            # For a `first.last` mailbox the local part slugs to exactly the name,
            # so "the local part appears here" would be the name signal counted a
            # second time under another label - and worse, it would look like
            # evidence from the address when the address was never on the page.
            if local_key and local_key not in self._name_keys() and local_key in slug(text):
                found.email_local = True
        return found

    def _name_keys(self) -> set:
        """Slugs the name alone produces, which the local part must beat to inform."""
        first, last = self.name.first, self.name.last
        return {slug(s) for s in (first, last, f"{first}{last}", f"{last}{first}") if s}


def _region_for(domain: str, region: str) -> str:
    if region:
        return region.upper()
    return phonelib.region_for_domain(domain, "IN")


def build(full_name: str, company: str = "", work_email: str = "",
          domain: str = "", region: str = "") -> Subject:
    """Assemble a Subject, and say plainly what does not add up.

    A problem never aborts the lookup - a person can perfectly well have an
    address that does not follow their own name. It is recorded so the evidence
    trail shows the identifier was weaker than it looked.
    """
    from .sources.website import is_role_account

    name = parse_name(full_name)
    subject = Subject(name=name, company=(company or "").strip())
    if not name.first:
        subject.problems.append(f"could not parse a name from {full_name!r}")

    address = (work_email or "").strip().lower()
    if address:
        if not EMAIL_RE.fullmatch(address):
            subject.problems.append(f"{address!r} is not a valid email address")
        else:
            subject.email_valid = True
            subject.work_email = address
            subject.local, _, subject.domain = address.partition("@")
            subject.email_is_role = is_role_account(address)
            if subject.email_is_role:
                subject.problems.append(
                    f"{address} is a shared mailbox, so it identifies a team rather "
                    f"than this person")
            hits = patterns.matching_templates(subject.local, name) if name.ok else []
            if hits:
                subject.name_email_template = hits[0]
            elif name.ok and not subject.email_is_role:
                subject.problems.append(
                    f"'{subject.local}' cannot be built from '{name.display}' by any known "
                    f"format - the address may belong to someone else, or the company "
                    f"may not name mailboxes after people")

    if domain and not subject.domain:
        subject.domain = domain.lower().strip().lstrip("@")
    subject.region = _region_for(subject.domain, region)

    aliases = []
    if subject.local:
        bare = subject.local.replace(".", "").replace("-", "").replace("_", "")
        aliases = [subject.local, bare]
    if name.ok:
        aliases.append(f"{name.first}{name.last}")
        aliases.append(f"{name.first}.{name.last}")
    subject.aliases = list(dict.fromkeys(a for a in aliases if a))
    return subject
