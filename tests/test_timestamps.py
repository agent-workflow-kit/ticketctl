"""Timestamp and date normalization (issue #13): UTC with Z to the second, unknown is None."""

from __future__ import annotations

import random
import time
from typing import Any

import pytest

from ticketctl.timestamps import normalize_date, normalize_ts


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("2026-03-04T10:00:00.000-0600", "2026-03-04T16:00:00Z"),  # V-13: no colon in the offset
        ("2026-03-04T10:00:00.000+0000", "2026-03-04T10:00:00Z"),
        ("2026-03-04T10:00:00.000-06:00", "2026-03-04T16:00:00Z"),
        ("2026-03-04T10:00:00+0530", "2026-03-04T04:30:00Z"),
        ("2026-03-04T10:00:00Z", "2026-03-04T10:00:00Z"),
        ("2026-03-04T10:00Z", "2026-03-04T10:00:00Z"),
        ("2026-03-04T23:59:59.999999999Z", "2026-03-04T23:59:59Z"),  # milliseconds are dropped
        ("2026-03-04T23:30:00-0600", "2026-03-05T05:30:00Z"),  # crosses midnight
        ("2026-12-31T23:00:00-0100", "2027-01-01T00:00:00Z"),  # crosses the year
        ("2024-02-29T00:00:00+0000", "2024-02-29T00:00:00Z"),
        ("0999-01-01T00:00:00Z", "0999-01-01T00:00:00Z"),
    ],
)
def test_valid_timestamps_normalize(raw: str, expected: str) -> None:
    assert normalize_ts(raw) == expected


@pytest.mark.parametrize(
    "raw",
    [
        "2026-03-04T10:00:00",  # naive: unknown
        "2026-03-04T10:00:00.000",
        "2026-03-04",
        "",
        "yesterday",
        "2026-03-04 10:00:00Z",
        "2026-03-04t10:00:00z",
        "2026-13-04T10:00:00Z",
        "2026-02-30T10:00:00Z",
        "2025-02-29T10:00:00Z",
        "2026-03-04T24:00:00Z",
        "2026-03-04T10:60:00Z",
        "2026-03-04T10:00:60Z",  # leap seconds are not representable
        "2026-03-04T10:00:00+2400",
        "2026-03-04T10:00:00+0060",
        "2026-03-04T10:00:00+24:00",
        "2026-03-04T10:00:00+1",
        "2026-03-04T10:00:00+00:0",
        "2026-03-04T10:00:00Z\n",  # fullmatch: a trailing newline never passes
        " 2026-03-04T10:00:00Z",
        "２０２６-03-04T10:00:00Z",  # full-width digits
        "2026-03-04T10:00:00.Z",
        "2026-03-04T10:00:00.1234567890Z",
        "0001-01-01T00:00:00+0530",  # underflows UTC
        "9999-12-31T23:59:59-0600",  # overflows UTC
        "2026-03-04T10:00:00Z" + "0" * 100,
        "-2026-03-04T10:00:00Z",
        "+12026-03-04T10:00:00Z",
    ],
)
def test_invalid_timestamps_are_unknown(raw: str) -> None:
    assert normalize_ts(raw) is None


@pytest.mark.parametrize("raw", [None, 0, 1.5, True, [], {}, b"2026-03-04T10:00:00Z", object()])
def test_a_non_string_is_unknown(raw: Any) -> None:
    assert normalize_ts(raw) is None


def test_the_far_edges_that_fit_still_normalize() -> None:
    assert normalize_ts("0001-01-01T00:00:00Z") == "0001-01-01T00:00:00Z"
    assert normalize_ts("9999-12-31T23:59:59Z") == "9999-12-31T23:59:59Z"
    assert normalize_ts("0001-01-01T05:30:00+0530") == "0001-01-01T00:00:00Z"


@pytest.mark.parametrize(
    ("raw", "ok"),
    [
        ("2026-03-04", True),
        ("2024-02-29", True),
        ("2025-02-29", False),
        ("2026-13-01", False),
        ("2026-3-4", False),
        ("20260304", False),
        ("2026-03-04\n", False),
        ("2026-03-04T00:00:00Z", False),
        ("０２６-03-04", False),
        ("0000-01-01", False),
        ("", False),
        (5, False),
        (None, False),
    ],
)
def test_dates(raw: Any, ok: bool) -> None:
    assert normalize_date(raw) == (raw if ok else None)


def test_fuzz_never_raises_and_only_answers_the_grammar() -> None:
    rng = random.Random(1307)
    alphabet = "0123456789-:+TZz. \n０é"
    deadline = time.monotonic() + 2.0
    seen_valid = 0
    while time.monotonic() < deadline:
        raw = "".join(rng.choice(alphabet) for _ in range(rng.randrange(0, 40)))
        out = normalize_ts(raw)
        if out is not None:
            seen_valid += 1
            assert len(out) == 20
            assert out.endswith("Z")
            assert normalize_ts(out) == out  # idempotent
        assert normalize_date(raw) in (None, raw)
    assert seen_valid >= 0
