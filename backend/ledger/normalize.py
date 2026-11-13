"""Pure value normalisers: every lead/event value rule from the spec lives here.

No I/O and no database access, so each rule can be unit-tested in isolation.
All regexes use ASCII digits only (``[0-9]``) so Unicode digits never sneak in.
"""
from __future__ import annotations

import re
from datetime import date, datetime, timedelta, timezone
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation

IST = timezone(timedelta(hours=5, minutes=30))

_NON_DIGIT = re.compile(r"[^0-9]")
_HEADER_SEP = re.compile(r"[_\s]+")

_ISO = re.compile(r"([0-9]{4})-([0-9]{1,2})-([0-9]{1,2})")
_DMY_SLASH = re.compile(r"([0-9]{1,2})/([0-9]{1,2})/([0-9]{4})")
_DMY_DASH = re.compile(r"([0-9]{1,2})-([0-9]{1,2})-([0-9]{4})")
_D_MON_Y = re.compile(r"([0-9]{1,2})-([A-Za-z]{3})-([0-9]{4})")
_MONTHS = {
    m: i
    for i, m in enumerate(
        ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"], start=1
    )
}

_CURRENCY_PREFIX = re.compile(r"(?:₹|rs\.?|inr)\s*", re.IGNORECASE)
_NUMBER = re.compile(r"[0-9]+(?:\.[0-9]+)?|\.[0-9]+")


def normalize_header(header: str) -> str:
    """Lower-case, trim, then collapse every run of ``_`` / whitespace to one space."""
    return _HEADER_SEP.sub(" ", header.lower().strip())


def normalize_phone(raw: object) -> str | None:
    """Indian mobile number -> ``+91XXXXXXXXXX`` or ``None`` when invalid.

    Strip non-digits; drop a leading ``91`` from 12 digits or a leading ``0`` from 11 digits;
    valid only if exactly 10 digits starting with 6-9.
    """
    if isinstance(raw, int) and not isinstance(raw, bool):
        raw = str(raw)
    if not isinstance(raw, str):
        return None
    digits = _NON_DIGIT.sub("", raw)
    if len(digits) == 12 and digits.startswith("91"):
        digits = digits[2:]
    elif len(digits) == 11 and digits.startswith("0"):
        digits = digits[1:]
    if len(digits) == 10 and digits[0] in "6789":
        return "+91" + digits
    return None


def _safe_date(year: int, month: int, day: int) -> date | None:
    try:
        return date(year, month, day)
    except ValueError:  # impossible dates such as 31/02 or month 13
        return None


def parse_date(raw: str) -> date | None:
    """Accept YYYY-MM-DD, DD/MM/YYYY, DD-MM-YYYY, DD-Mon-YYYY. Always day-first."""
    text = raw.strip()
    if m := _ISO.fullmatch(text):
        return _safe_date(int(m[1]), int(m[2]), int(m[3]))
    if m := (_DMY_SLASH.fullmatch(text) or _DMY_DASH.fullmatch(text)):
        return _safe_date(int(m[3]), int(m[2]), int(m[1]))
    if m := _D_MON_Y.fullmatch(text):
        month = _MONTHS.get(m[2].lower())
        return _safe_date(int(m[3]), month, int(m[1])) if month else None
    return None


def parse_decimal(raw: str) -> Decimal | None:
    """Optional ₹ / Rs / Rs. / INR prefix, comma thousands separators, >= 0, 2 places (half-up)."""
    text = raw.strip()
    if prefix := _CURRENCY_PREFIX.match(text):
        text = text[prefix.end():]
    text = text.replace(",", "")
    if not _NUMBER.fullmatch(text):  # also rejects negatives and words
        return None
    try:
        return Decimal(text).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    except InvalidOperation:
        return None


def parse_enum(raw: str, allowed: tuple[str, ...]) -> str | None:
    """Case-insensitive match against the allowed values, stored lower-case."""
    value = raw.strip().lower()
    return value if value in {a.lower() for a in allowed} else None


def parse_timestamp(raw: object) -> datetime | None:
    """ISO-8601 with ``Z`` / offset / naive. Naive means IST, per the spec."""
    if not isinstance(raw, str):
        return None
    try:
        parsed = datetime.fromisoformat(raw.strip())
    except ValueError:
        return None
    return parsed.replace(tzinfo=IST) if parsed.tzinfo is None else parsed
