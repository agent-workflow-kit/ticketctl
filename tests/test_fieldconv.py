"""Conversion of a field's value by its schema (issue #12): the documented types, null otherwise."""

from __future__ import annotations

from typing import Any

import pytest

from ticketctl.fieldconv import convert
from ticketctl.fieldmap import FieldSchema

PART = "fields.points"


def conv(schema: FieldSchema | None, raw: Any) -> tuple[Any, tuple[str, ...]]:
    out = convert(schema, raw, PART)
    return out.value, out.incomplete


def S(type_: str, items: str | None = None, custom: str | None = None) -> FieldSchema:
    return FieldSchema(type_, items, custom)


ADF = {
    "type": "doc",
    "version": 1,
    "content": [{"type": "paragraph", "content": [{"type": "text", "text": "Hello"}]}],
}


@pytest.mark.parametrize(
    ("schema", "raw", "expected"),
    [
        (S("number"), 3, 3),
        (S("number"), 2.5, 2.5),
        (S("string"), "plain", "plain"),
        (S("date"), "2026-03-04", "2026-03-04"),
        (S("datetime"), "2026-03-04T10:00:00.000-0600", "2026-03-04T16:00:00Z"),
        (S("datetime"), "2026-03-04T10:00:00+02:00", "2026-03-04T08:00:00Z"),
        (S("datetime"), "2026-03-04T10:00Z", "2026-03-04T10:00:00Z"),
        (S("option"), {"value": "High", "id": "1"}, "High"),
        (S("array", "string"), ["a", "b"], ["a", "b"]),
        (S("array", "option"), [{"value": "A"}, {"value": "B"}], ["A", "B"]),
        (
            S("array", "json"),
            [{"name": "Sprint 1"}, {"name": "Sprint 2"}],
            ["Sprint 1", "Sprint 2"],
        ),
        (S("array", "version"), [{"name": "1.0"}], ["1.0"]),
        (S("array", "string"), [], []),
    ],
)
def test_documented_types_convert(schema: FieldSchema, raw: Any, expected: Any) -> None:
    assert conv(schema, raw) == (expected, ())


def test_rich_text_becomes_markdown() -> None:
    assert conv(S("string", custom="x:textarea"), ADF) == ("Hello", ())


def test_rich_text_budget_hit_names_the_part() -> None:
    deep: dict[str, Any] = {"type": "text", "text": "x"}
    for _ in range(80):
        deep = {"type": "blockquote", "content": [deep]}
    value, incomplete = conv(S("string"), {"type": "doc", "version": 1, "content": [deep]})
    assert isinstance(value, str)
    assert incomplete == (PART,)


def test_a_user_becomes_an_account_object_without_email() -> None:
    raw = {"accountId": "acct-0001", "displayName": "Pat Doe", "emailAddress": "pat@example.test"}
    value, incomplete = conv(S("user"), raw)
    assert value == {"account_id": "acct-0001", "display_name": "Pat Doe"}
    assert incomplete == ()


def test_a_user_without_id_or_name_gets_the_fallbacks() -> None:
    assert conv(S("user"), {})[0] == {"account_id": None, "display_name": "unknown user"}


def test_a_user_with_a_hostile_id_has_no_id() -> None:
    value, _ = conv(S("user"), {"accountId": "a b\n", "displayName": "X"})
    assert value == {"account_id": None, "display_name": "X"}


def test_a_user_array_becomes_account_objects() -> None:
    value, _ = conv(S("array", "user"), [{"accountId": "a1", "displayName": "A"}])
    assert value == [{"account_id": "a1", "display_name": "A"}]


def test_null_is_a_known_empty_value() -> None:
    for schema in (S("number"), S("array", "string"), S("user"), S("string"), S("date")):
        assert conv(schema, None) == (None, ())


@pytest.mark.parametrize(
    "schema",
    [S("issuelink"), S("priority"), S("status"), S("any"), S("progress"), S("weirdtype"), None],
)
def test_an_unknown_schema_type_is_null_with_incomplete(schema: FieldSchema | None) -> None:
    assert conv(schema, {"name": "x"}) == (None, (PART,))


@pytest.mark.parametrize(
    ("schema", "raw"),
    [
        (S("number"), "3"),
        (S("number"), True),
        (S("number"), float("nan")),
        (S("number"), float("inf")),
        (S("number"), [1]),
        (S("string"), 5),
        (S("string"), {"type": "paragraph"}),
        (S("string"), ["a"]),
        (S("date"), "2026-13-45"),
        (S("date"), "20260304"),
        (S("date"), "2026-03-04T00:00:00Z"),
        (S("date"), 20260304),
        (S("datetime"), "2026-03-04T10:00:00"),
        (S("datetime"), "yesterday"),
        (S("datetime"), 5),
        (S("option"), "High"),
        (S("option"), {"value": 5}),
        (S("option"), {"name": "High"}),
        (S("user"), "acct-1"),
        (S("array", "string"), "a"),
        (S("array", "string"), [1]),
        (S("array", "string"), ["a", None]),
        (S("array", "option"), [{"value": "A"}, "B"]),
        (S("array", "json"), ["bare"]),
        (S("array", "json"), [{"id": 1}]),
        (S("array"), ["a"]),
        (S("array", "issuelinks"), [{"id": "1"}]),
    ],
)
def test_a_value_that_does_not_fit_its_schema_is_null_with_incomplete(
    schema: FieldSchema, raw: Any
) -> None:
    assert conv(schema, raw) == (None, (PART,))


def test_strings_are_sanitized_and_capped() -> None:
    value, incomplete = conv(S("string"), "a\x1b[31mb‮" + "z" * 5000)
    assert isinstance(value, str)
    assert "\x1b" not in value and "‮" not in value
    assert len(value) <= 4096
    assert value.endswith("<truncated>")
    assert incomplete == (f"{PART}.truncated",)


def test_option_and_name_strings_are_sanitized() -> None:
    value, _ = conv(S("option"), {"value": "A\x07B"})
    assert value == "A<U+0007>B"


def test_a_long_array_is_cut_and_says_so() -> None:
    raw = [f"item{n}" for n in range(1500)]
    value, incomplete = conv(S("array", "string"), raw)
    assert value == raw[:1000]
    assert incomplete == (f"{PART}.truncated",)


def test_a_number_up_to_two_to_the_63_is_kept() -> None:
    assert conv(S("number"), 2**63) == (2**63, ())
    assert conv(S("number"), -(2**63)) == (-(2**63), ())


@pytest.mark.parametrize("raw", [2**63 + 1, -(2**63) - 1, 10**30])
def test_a_huge_integer_is_unknown(raw: int) -> None:
    assert conv(S("number"), raw) == (None, (PART,))


def test_a_year_below_1000_is_zero_padded() -> None:
    assert conv(S("datetime"), "0999-01-01T00:00:00Z") == ("0999-01-01T00:00:00Z", ())


def test_an_overflowing_datetime_is_unknown_and_does_not_raise() -> None:
    assert conv(S("datetime"), "0001-01-01T00:00:00+05:30") == (None, (PART,))


@pytest.mark.parametrize(
    ("schema", "raw", "expected"),
    [
        (S("array", "number"), [1, 2.5], [1, 2.5]),
        (S("array", "date"), ["2026-03-04"], ["2026-03-04"]),
        (S("array", "datetime"), ["2026-03-04T10:00:00+02:00"], ["2026-03-04T08:00:00Z"]),
    ],
)
def test_arrays_of_numbers_and_dates_convert(schema: FieldSchema, raw: Any, expected: Any) -> None:
    assert conv(schema, raw) == (expected, ())


@pytest.mark.parametrize(
    ("schema", "raw"),
    [
        (S("array", "number"), ["1"]),
        (S("array", "date"), ["nope"]),
        (S("array", "datetime"), ["2026-03-04T10:00:00"]),
    ],
)
def test_arrays_of_numbers_and_dates_that_do_not_fit_are_unknown(
    schema: FieldSchema, raw: Any
) -> None:
    assert conv(schema, raw) == (None, (PART,))


def test_an_extra_string_is_single_line() -> None:
    assert conv(S("string"), "a\nb\tc")[0] == "a b c"


def test_an_extra_string_holding_adf_is_capped_at_4096() -> None:
    doc = {
        "type": "doc",
        "version": 1,
        "content": [{"type": "paragraph", "content": [{"type": "text", "text": "w" * 6000}]}],
    }
    value, incomplete = conv(S("string"), doc)
    assert len(value) <= 4096 and value.endswith("<truncated>")
    assert incomplete == (f"{PART}.truncated",)


def test_body_mode_keeps_line_breaks_in_plain_strings() -> None:
    out = convert(S("string"), "line one\nline two\r\nline three", PART, body=True)
    assert out.value == "line one\nline two\nline three" and out.incomplete == ()


def test_body_mode_uses_the_body_cap_for_plain_strings() -> None:
    out = convert(S("string"), "x" * 5000, PART, body=True)
    assert out.value == "x" * 5000 and out.incomplete == ()
    huge = convert(S("string"), "x" * 1_000_001, PART, body=True)
    assert len(huge.value) == 1_000_000 and huge.incomplete == (f"{PART}.truncated",)


def test_body_mode_renders_adf_beyond_4096() -> None:
    doc = {
        "type": "doc",
        "version": 1,
        "content": [{"type": "paragraph", "content": [{"type": "text", "text": "w" * 6000}]}],
    }
    out = convert(S("string"), doc, PART, body=True)
    assert out.value == "w" * 6000 and out.incomplete == ()
