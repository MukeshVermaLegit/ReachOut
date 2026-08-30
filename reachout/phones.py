"""Phone number parsing, validation and classification.

Unlike email, a phone number cannot be derived from a person's name - there is
no pattern to learn and nothing to infer. Everything here is about numbers we
actually found: normalising them, proving what kind of line they are, and being
explicit about what cannot be determined.

Deliberately not implemented: number generation or range enumeration. It would
produce nothing useful and is the mechanism behind nuisance calling.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

# What kind of line a number is
MOBILE = "mobile"
LANDLINE = "landline"
TOLL_FREE = "toll-free"
PREMIUM = "premium"
VOIP = "voip"
UNKNOWN = "unknown"

_DIGITS = re.compile(r"\d")
_EXT_RE = re.compile(r"(?:\s*(?:ext|x|extn|extension)\.?\s*(\d{1,6}))\s*$", re.I)
_CLEAN = re.compile(r"[^\d+]")


@dataclass
class Phone:
    e164: str = ""
    country: str = ""          # ISO-ish label, e.g. "IN"
    calling_code: str = ""     # e.g. "91"
    national: str = ""         # digits after the calling code
    pretty: str = ""           # human formatting
    kind: str = UNKNOWN
    kind_certain: bool = False  # can this country's numbering tell mobile from landline?
    extension: str = ""
    area: str = ""             # STD/area code where we can identify one
    notes: list = field(default_factory=list)
    valid: bool = False

    def __str__(self) -> str:
        return self.pretty or self.e164


def _fmt_groups(digits: str, groups: list[int]) -> str:
    out, index = [], 0
    for size in groups:
        if index >= len(digits):
            break
        out.append(digits[index:index + size])
        index += size
    if index < len(digits):
        out.append(digits[index:])
    return " ".join(out)


# --------------------------------------------------------------------- India
_IN_TOLLFREE = ("1800", "1600")
_IN_PREMIUM = ("1860", "1861")


def _service_number(national: str, phone: Phone, kind: str) -> Phone:
    """1800 / 1860 style service lines: 4-digit prefix, then 6 or 7 digits."""
    rest = national[4:]
    if not 6 <= len(rest) <= 7:
        phone.notes.append(f"service numbers carry 6-7 digits after {national[:4]}; "
                           f"this has {len(rest)}")
        return phone
    phone.kind, phone.kind_certain, phone.valid = kind, True, True
    split = 3 if len(rest) == 6 else 3
    phone.pretty = f"+91 {national[:4]} {rest[:split]} {rest[split:]}"
    return phone


def _india(national: str, phone: Phone) -> Phone:
    if national.startswith(_IN_TOLLFREE):
        return _service_number(national, phone, TOLL_FREE)
    if national.startswith(_IN_PREMIUM):
        return _service_number(national, phone, PREMIUM)
    if len(national) != 10:
        phone.notes.append(f"Indian numbers are 10 digits; this has {len(national)}")
        return phone
    metro = {"11": "Delhi", "22": "Mumbai", "33": "Kolkata", "44": "Chennai",
             "20": "Pune", "40": "Hyderabad", "79": "Ahmedabad", "80": "Bengaluru"}
    # The metro STD codes have to be read before the 6-9 mobile rule. "6-9 is
    # mobile" is true of a *subscriber* number, but a landline carrying its STD
    # code is a ten-digit national number starting 079 (Ahmedabad) or 080
    # (Bengaluru), and those were being reported as mobile with kind_certain
    # set - turning every Bengaluru switchboard into someone's personal mobile,
    # which is the expensive direction to be wrong in. Delhi failed the other
    # way: 011 leads with '1', so it was rejected as not a valid number at all.
    #
    # 11/20/22/33/40/44 lead with a digit no mobile can use, so they stay
    # certain. 79 and 80 collide with the mobile range and genuinely cannot be
    # told apart from the digits alone - 8022932368 is a well-formed number of
    # either kind - so they are decided on the subscriber digit and reported
    # as uncertain.
    if national[:2] in metro and (national[0] not in "6789" or national[2] in "2345"):
        ambiguous = national[0] in "6789"
        phone.kind, phone.valid = LANDLINE, True
        phone.kind_certain = not ambiguous
        phone.area = national[:2]
        phone.notes.append(f"landline in {metro[national[:2]]}")
        if ambiguous:
            phone.notes.append(
                f"+91 {national[:2]} is both a metro STD code and a valid mobile "
                "prefix; read as a landline on the subscriber digits, not certain")
        phone.pretty = f"+91 {national[:2]} {_fmt_groups(national[2:], [4, 4])}"
        return phone
    if national[0] in "6789":
        # Outside the metro-code collision above, 6-9 is mobile-only.
        phone.kind, phone.kind_certain, phone.valid = MOBILE, True, True
        phone.pretty = f"+91 {national[:5]} {national[5:]}"
        return phone
    if national[0] in "2345":
        phone.kind, phone.kind_certain, phone.valid = LANDLINE, True, True
        # STD codes are 2-4 digits; the metros use 2.
        if national[:2] in metro:
            phone.area = national[:2]
            phone.notes.append(f"landline in {metro[national[:2]]}")
            phone.pretty = f"+91 {national[:2]} {_fmt_groups(national[2:], [4, 4])}"
        else:
            phone.area = national[:3]
            phone.pretty = f"+91 {national[:3]} {national[3:]}"
        return phone
    phone.notes.append(f"'{national[0]}' is not a valid leading digit for an Indian number")
    return phone


# ------------------------------------------------------ North America (NANP)
def _nanp(national: str, phone: Phone) -> Phone:
    if len(national) != 10:
        phone.notes.append(f"NANP numbers are 10 digits; this has {len(national)}")
        return phone
    area, exchange = national[:3], national[3:6]
    if area[0] in "01" or exchange[0] in "01":
        phone.notes.append("invalid NANP area or exchange code")
        return phone
    phone.valid, phone.area = True, area
    phone.pretty = f"+1 ({area}) {exchange}-{national[6:]}"
    if area in {"800", "833", "844", "855", "866", "877", "888"}:
        phone.kind, phone.kind_certain = TOLL_FREE, True
        return phone
    if area == "900":
        phone.kind, phone.kind_certain = PREMIUM, True
        return phone
    # Number portability means the prefix no longer tells you the line type.
    phone.kind, phone.kind_certain = UNKNOWN, False
    phone.notes.append("US/Canada numbering cannot distinguish mobile from landline")
    return phone


# ------------------------------------------------------------------ UK, misc
def _uk(national: str, phone: Phone) -> Phone:
    if not 9 <= len(national) <= 10:
        phone.notes.append(f"UK numbers are 9-10 digits after +44; this has {len(national)}")
        return phone
    phone.valid = True
    if national.startswith("7"):
        phone.kind, phone.kind_certain = MOBILE, True
        phone.pretty = f"+44 {_fmt_groups(national, [4, 6])}"
    elif national.startswith("800") or national.startswith("808"):
        phone.kind, phone.kind_certain = TOLL_FREE, True
        phone.pretty = f"+44 {national}"
    elif national[0] in "12":
        phone.kind, phone.kind_certain = LANDLINE, True
        # London and the other 02x areas use a 2-digit code; most others use 3.
        if national.startswith("2"):
            phone.area = national[:2]
            phone.pretty = f"+44 {national[:2]} {_fmt_groups(national[2:], [4, 4])}"
        else:
            phone.area = national[:3]
            phone.pretty = f"+44 {_fmt_groups(national, [3, 3, 4])}"
    else:
        phone.pretty = f"+44 {national}"
    return phone


def _generic(national: str, phone: Phone, low: int = 6, high: int = 14) -> Phone:
    if low <= len(national) <= high:
        phone.valid = True
        phone.pretty = f"+{phone.calling_code} {national}"
        phone.notes.append(f"no line-type rules known for +{phone.calling_code}")
    else:
        phone.notes.append("implausible length for an international number")
    return phone


# Longest-prefix match, so +1 does not swallow +12 style codes wrongly.
_HANDLERS = {"91": _india, "1": _nanp, "44": _uk}
_COUNTRY_NAMES = {"91": "IN", "1": "US/CA", "44": "GB", "61": "AU", "65": "SG",
                  "971": "AE", "49": "DE", "33": "FR", "81": "JP", "86": "CN"}
_KNOWN_CODES = sorted(set(list(_HANDLERS) + list(_COUNTRY_NAMES)), key=len, reverse=True)

DEFAULT_REGION_CODE = {"IN": "91", "US": "1", "CA": "1", "GB": "44", "AU": "61",
                       "SG": "65", "AE": "971", "DE": "49"}


def normalise(raw: str, default_region: str = "IN") -> Phone:
    """Parse anything human-written into a validated Phone.

    `default_region` is used only when the number carries no country code.
    """
    phone = Phone()
    text = (raw or "").strip()
    if not text:
        return phone

    match = _EXT_RE.search(text)
    if match:
        phone.extension = match.group(1)
        text = text[:match.start()]

    had_plus = "+" in text or text.strip().startswith("00")
    cleaned = _CLEAN.sub("", text)
    if cleaned.startswith("00"):
        cleaned = "+" + cleaned[2:]
    digits = cleaned.lstrip("+")
    if not digits or len(digits) < 6:
        phone.notes.append("too few digits to be a phone number")
        return phone
    if len(digits) > 15:
        phone.notes.append("more than 15 digits (E.164 maximum)")
        return phone

    code = ""
    if had_plus or cleaned.startswith("+"):
        for candidate in _KNOWN_CODES:
            if digits.startswith(candidate):
                code = candidate
                break
        if not code:
            code = digits[:2]
    else:
        code = DEFAULT_REGION_CODE.get(default_region.upper(), "")
        if code and digits.startswith(code) and len(digits) > 10:
            digits = digits[len(code):]
        # A leading 0 is a national trunk prefix, not part of the number.
        digits = digits.lstrip("0") if digits.startswith("0") else digits
        digits = code + digits

    national = digits[len(code):] if digits.startswith(code) else digits
    phone.calling_code = code
    phone.country = _COUNTRY_NAMES.get(code, f"+{code}")
    phone.national = national

    handler = _HANDLERS.get(code)
    phone = handler(national, phone) if handler else _generic(national, phone)

    if phone.valid:
        phone.e164 = f"+{code}{national}"
        if not phone.pretty:
            phone.pretty = phone.e164
        if phone.extension:
            phone.pretty += f" ext. {phone.extension}"
    return phone


def looks_placeholder(phone: Phone) -> str:
    """Reason a number looks like a made-up example, or '' if it looks real.

    Test/dummy numbers pass every structural check - '9876543210' is a perfectly
    valid Indian mobile by the rules - so shape alone cannot catch them. What
    gives them away is that real allocations are not tidy.
    """
    national = phone.national
    if not national or len(national) < 6:
        return ""
    if len(set(national)) == 1:
        return "every digit is identical"
    steps = {(int(national[i + 1]) - int(national[i])) % 10 for i in range(len(national) - 1)}
    if steps == {1}:
        return "digits run in ascending sequence"
    if steps == {9}:
        return "digits run in descending sequence"
    if len(set(national)) == 2 and len(national) >= 8:
        return "only two distinct digits"
    if phone.calling_code == "1" and len(national) == 10 and national[3:6] == "555" \
            and "0100" <= national[6:] <= "0199":
        return "in the NANP 555-01xx range reserved for fiction"
    return ""


# A bare 10-digit number means different things in different countries. Guessing
# India for a .edu site turns a Boston landline into an Indian mobile, so take
# the hint from the domain wherever the suffix actually implies a country.
_TLD_REGION = {
    "in": "IN", "co.in": "IN", "ac.in": "IN", "org.in": "IN", "net.in": "IN", "gov.in": "IN",
    "uk": "GB", "co.uk": "GB", "ac.uk": "GB", "org.uk": "GB", "gov.uk": "GB",
    "edu": "US", "gov": "US", "mil": "US", "us": "US",
    "ca": "CA", "au": "AU", "com.au": "AU", "edu.au": "AU", "nz": "NZ",
    "sg": "SG", "com.sg": "SG", "ae": "AE", "de": "DE", "fr": "FR", "jp": "JP", "cn": "CN",
}


def region_for_domain(domain: str, fallback: str = "IN") -> str:
    """Best guess at the country a domain's phone numbers belong to.

    Returns the fallback for suffixes that say nothing (.com, .org, .ai, .io).
    """
    host = (domain or "").lower().strip().rstrip(".")
    if not host:
        return fallback
    labels = host.split(".")
    for size in (2, 1):
        if len(labels) >= size:
            suffix = ".".join(labels[-size:])
            if suffix in _TLD_REGION:
                return _TLD_REGION[suffix]
    return fallback


def is_mobile(phone: Phone) -> bool:
    return phone.valid and phone.kind == MOBILE


def could_be_mobile(phone: Phone) -> bool:
    """True when it is a mobile, or the country's numbering cannot rule it out."""
    return phone.valid and (phone.kind == MOBILE or not phone.kind_certain)


# Numbers that are published precisely because they are not personal.
SWITCHBOARD_HINTS = re.compile(
    r"\b(office|reception|switchboard|front desk|main line|head ?office|toll ?free|"
    r"helpline|support|sales|enquir|inquir|customer care|hotline|fax)\b", re.I)
PERSONAL_HINTS = re.compile(
    r"\b(mobile|cell|cellular|direct|whats ?app|personal|dial"
    # A press release ends with "Media Contact: <name>, <phone>, <email>". That
    # number is published deliberately, for people to ring that named person -
    # which is a stronger statement of intent than most of the web makes. It
    # still has to survive attribution: on a wire release the named contact is
    # often a PR agency, not the subject.
    r"|media contact|press contact|press enquiries|for (?:more )?information,? contact)\b",
    re.I)


def context_signal(context: str) -> str:
    """'switchboard' | 'personal' | '' from the words printed next to a number."""
    text = context or ""
    if SWITCHBOARD_HINTS.search(text):
        return "switchboard"
    if PERSONAL_HINTS.search(text):
        return "personal"
    return ""
