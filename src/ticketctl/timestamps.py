"""Timestamp and date normalization (docs/interface.md, Ticket object: created, updated, resolved).

``normalize_ts(raw)`` turns a provider datetime into UTC ISO 8601 with ``Z``, to the second:
the provider's offsets are accepted with or without a colon (``-0600`` is the real format, V-13),
its fractional seconds are dropped, and anything else is unknown (``None``): a naive datetime, a
value outside the grammar, an offset of a day or more, a date that does not exist and a result
that falls outside years 1 to 9999. It never raises. ``normalize_date(raw)`` accepts only a real
``YYYY-MM-DD`` date. Digits are the ASCII digits only and every pattern is matched with
``fullmatch``.
"""

from __future__ import annotations

import re
from datetime import UTC, date, datetime, timedelta, timezone

MAX_LENGTH = 64
_DATE = re.compile(r"([0-9]{4})-([0-9]{2})-([0-9]{2})")
_DATETIME = re.compile(
    r"([0-9]{4})-([0-9]{2})-([0-9]{2})T([0-9]{2}):([0-9]{2})(?::([0-9]{2})(?:\.[0-9]{1,9})?)?"
    r"(?:(Z)|([+-])([0-9]{2}):?([0-9]{2}))"
)


def normalize_date(raw: object) -> str | None:
    """``raw`` when it is a real ``YYYY-MM-DD`` date, else None."""
    if not isinstance(raw, str) or len(raw) > MAX_LENGTH or _DATE.fullmatch(raw) is None:
        return None
    try:
        date.fromisoformat(raw)
    except ValueError:
        return None
    return raw


def normalize_ts(raw: object) -> str | None:
    """A provider datetime as UTC with ``Z`` to the second, or None when it is unknown."""
    if not isinstance(raw, str) or len(raw) > MAX_LENGTH:
        return None
    match = _DATETIME.fullmatch(raw)
    if match is None:
        return None
    year, month, day, hour, minute, second, zulu, sign, off_h, off_m = match.groups()
    try:
        if zulu:
            zone = UTC
        else:
            hours, minutes = int(off_h), int(off_m)
            if hours > 23 or minutes > 59:
                return None
            delta = timedelta(hours=hours, minutes=minutes)
            zone = timezone(-delta if sign == "-" else delta)
        local = datetime(
            int(year), int(month), int(day), int(hour), int(minute), int(second or 0), tzinfo=zone
        )
        return local.astimezone(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")
    except (ValueError, OverflowError):
        return None
