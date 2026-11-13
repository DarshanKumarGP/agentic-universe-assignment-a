"""Pure value-normalisation helpers. No I/O, no database: easy to unit test."""
from __future__ import annotations

import re
from datetime import date, datetime
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from typing import Optional, Sequence

from .config import IST

_HEADER_WS = re.compile(r"[_\s]+")


def normalize_header(header: str) -> str:
    """lower-case, trim, then collapse every run of '_' / whitespace to one space (spec order)."""
    return _HEADER_WS.sub(" ", header.lower().strip())


def normalize_phone(raw: str) -> Optional[str]:
    """Indian mobile -> '+91XXXXXXXXXX', or None when invalid."""
    digits = re.sub(r"[^0-9]", "", raw)
    if len(digits) == 12 and digits.startswith("91"):
        digits = digits[2:]
    elif len(digits) == 11 and digits.startswith("0"):
        digits = digits[1:]
    if len(digits) == 10 and digits[0] in "6789":
        return "+91" + digits
    return None


_MONTHS = {m: i for i, m in enumerate(
    ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"], 1)}
_ISO_RE = re.compile(r"^([0-9]{4})-([0-9]{1,2})-([0-9]{1,2})$")
_DMY_RE = re.compile(r"^([0-9]{1,2})([/-])([0-9]{1,2})\2([0-9]{4})$")  # same separator twice
_DMON_RE = re.compile(r"^([0-9]{1,2})-([A-Za-z]{3})-([0-9]{4})$")


def parse_date(raw: str) -> Optional[str]:
    """YYYY-MM-DD | DD/MM/YYYY | DD-MM-YYYY | DD-Mon-YYYY (day-first) -> ISO date, or None."""
    s = raw.strip()
    if m := _ISO_RE.match(s):
        year, month, day = int(m[1]), int(m[2]), int(m[3])
    elif m := _DMY_RE.match(s):
        day, month, year = int(m[1]), int(m[3]), int(m[4])
    elif m := _DMON_RE.match(s):
        month = _MONTHS.get(m[2].lower())
        if month is None:
            return None
        day, year = int(m[1]), int(m[3])
    else:
        return None
    try:
        return date(year, month, day).isoformat()
    except ValueError:  # impossible date such as 31/02
        return None


_DECIMAL_RE = re.compile(r"^(?:₹|rs\.?|inr)?\s*([0-9][0-9,]*(?:\.[0-9]+)?|\.[0-9]+)$", re.IGNORECASE)


def parse_decimal(raw: str) -> Optional[str]:
    """Optional currency prefix, commas as thousands separators, >= 0, 2 places (half-up)."""
    m = _DECIMAL_RE.match(raw.strip())
    if not m:
        return None  # includes negatives: a leading '-' never matches
    try:
        value = Decimal(m.group(1).replace(",", ""))
    except InvalidOperation:
        return None
    return str(value.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


def parse_enum(raw: str, values: Sequence[str]) -> Optional[str]:
    """Case-insensitive match against `values`; stored lower-case."""
    candidate = raw.strip().lower()
    return candidate if candidate in {v.lower() for v in values} else None


def parse_timestamp(value: object) -> Optional[float]:
    """Event timestamp -> UTC epoch seconds. 'Z' = UTC, explicit offset honoured, naive = IST."""
    if not isinstance(value, str):
        return None
    s = value.strip()
    if not s:
        return None
    if s[-1] in "Zz":
        s = s[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(s)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=IST)
    return dt.timestamp()
