"""Canonical equality and plan values of field writes (issue #30; reused by ``create``, #31)."""

from __future__ import annotations

from typing import Any

import pytest

from ticketctl import attribution
from ticketctl.config import Fields
from ticketctl.fieldmap import FieldSchema, ProviderField, SiteFields
from ticketctl.fieldwrite import (
    UNREADABLE,
    FieldEdit,
    canon,
    display_from,
    display_to,
    edit_name,
    fact,
    fields_body,
    same_value,
)
from ticketctl.setconvert import FieldValue
from ticketctl.setspec import Assignment, MetaField, Resolved, Source


def edit(kind: str, payload: Any, *, items: str | None = None, name: str = "Thing") -> FieldEdit:
    field = ProviderField("customfield_90001", name, True, FieldSchema(kind, items))
    return FieldEdit(name, FieldValue(field, name, payload, None, payload in (None, [])))


def rich_edit(text: str) -> FieldEdit:
    rich = attribution.prepare_rich_text(
        text, actor="human:tester", kind="field", template="t", enabled=False
    )
    field = ProviderField("description", "Description", False, FieldSchema("string"))
    return FieldEdit("description", FieldValue(field, "Description", rich.doc, rich, False))


def doc(*paragraphs: str, attrs: bool = False) -> dict[str, Any]:
    extra = {"attrs": {"localId": "x"}} if attrs else {}
    return {
        "type": "doc",
        "version": 1,
        "content": [
            {"type": "paragraph", **extra, "content": [{"type": "text", "text": p}]}
            for p in paragraphs
        ],
    }


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        (None, None),
        ([], None),
        (3, "3"),
        (3.0, "3"),
        (3.50, "3.5"),
        (-0.0, "0"),
        (10**30, "1" + "0" * 30),
        ("text", "text"),
        (True, True),
        ({"accountId": "a1", "displayName": "x"}, "a1"),
        ({"value": "High", "id": "3"}, "High"),
        ({"name": "Medium"}, "Medium"),
        ({"key": "DEMO-1", "id": "1", "fields": {"summary": "s"}}, "DEMO-1"),
        (["b", "a"], ["a", "b"]),
        ([{"name": "y"}, {"name": "x"}], ["x", "y"]),
    ],
)
def test_canon(raw: Any, expected: Any) -> None:
    assert canon(raw) == expected


@pytest.mark.parametrize("raw", [float("nan"), float("inf"), {"id": "1"}, object(), [[[[[[1]]]]]]])
def test_canon_refuses_what_has_no_form(raw: Any) -> None:
    with pytest.raises(Exception):  # noqa: B017 - _Unreadable is private
        canon(raw)


def test_a_number_is_equal_as_a_decimal_whatever_its_spelling() -> None:
    e = edit("number", 3)
    assert same_value(e, 3.0) and same_value(e, 3) and same_value(e, "3") is False
    assert not same_value(e, 3.5) and not same_value(e, None)
    assert not same_value(e, float("nan"))


def test_a_clear_equals_none_and_an_empty_list() -> None:
    assert same_value(edit("number", None), None)
    assert same_value(edit("array", [], items="string"), None)
    assert same_value(edit("array", [], items="string"), [])
    assert not same_value(edit("number", None), 0)


def test_dates_and_datetimes_compare_by_instant() -> None:
    assert same_value(edit("date", "2026-04-01"), "2026-04-01")
    assert not same_value(edit("date", "2026-04-01"), "2026-04-02")
    assert not same_value(edit("date", "2026-04-01"), "not a date")
    stamp = edit("datetime", "2026-04-01T12:00:00.000+0000")
    assert same_value(stamp, "2026-04-01T06:00:00.000-0600")
    assert same_value(stamp, "2026-04-01T12:00:00Z")
    assert not same_value(stamp, "2026-04-01T12:00:01.000+0000")
    assert not same_value(stamp, "2026-04-01T12:00:00")  # naive: unknown, never equal


def test_options_users_and_links_compare_by_name_account_or_key() -> None:
    assert same_value(edit("option", {"value": "High"}), {"value": "High", "id": "1"})
    assert not same_value(edit("option", {"value": "High"}), {"value": "high"})
    assert same_value(edit("user", {"accountId": "a1"}), {"accountId": "a1", "displayName": "n"})
    assert same_value(edit("user", None), None)
    assert not same_value(edit("user", None), {"accountId": "a1"})
    assert same_value(edit("issuelink", {"key": "DEMO-1"}), {"key": "DEMO-1", "id": "7"})
    assert same_value(edit("priority", {"name": "High"}), {"name": "High", "iconUrl": "u"})


def test_lists_compare_as_sorted_sets() -> None:
    e = edit("array", [{"value": "b"}, {"value": "a"}], items="option")
    assert same_value(e, [{"value": "a", "id": "1"}, {"value": "b", "id": "2"}])
    assert not same_value(e, [{"value": "a"}])
    assert not same_value(e, None)


def test_unreadable_provider_values_are_never_equal() -> None:
    assert not same_value(edit("string", "x"), {"unexpected": True})
    assert not same_value(edit("number", 1), object())
    assert fact(edit("string", "x"), {"unexpected": True}) == UNREADABLE


def test_rich_text_compares_as_rendered_markdown() -> None:
    e = rich_edit("Same words")
    assert same_value(e, doc("Same words", attrs=True))
    assert not same_value(e, doc("Other words"))
    assert not same_value(e, None)
    assert not same_value(e, "a plain string")  # a rich field holds a document
    assert same_value(
        FieldEdit("d", FieldValue(e.field, "D", None, None, True)), None
    )  # a clear against an empty value


def test_rich_facts_are_a_hash_of_the_rendered_text_and_display_is_the_text() -> None:
    e = rich_edit("Same words")
    assert fact(e, doc("Same words", attrs=True)) == fact(e, doc("Same words"))
    assert len(fact(e, doc("Same words"))) == 64
    assert fact(e, None) is None
    assert display_to(e).strip() == "Same words"
    assert display_from(e, doc("Old")).strip() == "Old"
    assert display_from(e, "garbage") is None


def test_numbers_are_displayed_as_numbers() -> None:
    assert display_to(edit("number", 5)) == 5
    assert display_to(edit("number", 2.5)) == 2.5
    assert display_from(edit("number", 5), 3.0) == 3
    assert display_to(edit("option", {"value": "High"})) == "High"
    assert display_to(edit("array", [{"name": "b"}, {"name": "a"}], items="component")) == [
        "a",
        "b",
    ]
    assert display_to(edit("user", None)) is None


def test_fields_body_is_keyed_by_provider_id() -> None:
    e = edit("number", 5)
    assert fields_body([e]) == {"customfield_90001": 5}


def _site(extra: dict[str, Any] | None = None) -> tuple[SiteFields, ProviderField]:
    field = ProviderField("customfield_90001", "Points", True, FieldSchema("number"))
    config = Fields.model_validate({"extra": extra or {}})
    return SiteFields([field], config), field


def _resolved(typed: str, field: ProviderField) -> Resolved:
    meta = MetaField(field, operations=("set",))
    return Resolved(Assignment(typed, Source("inline", "1")), field, meta)


def test_a_plan_name_is_the_logical_or_extra_name_never_a_bare_field_id() -> None:
    site, field = _site({"points": {"id": "customfield_90001"}})
    assert edit_name(_resolved("points", field), site) == "points"
    assert edit_name(_resolved("customfield_90001", field), site) == "points"
    assert edit_name(_resolved("POINTS ", field), site) == "points"  # found by the mapping
    plain, field = _site()
    assert edit_name(_resolved("Points", field), plain) == "Points"
    hidden = ProviderField("customfield_90002", "Cost customfield_90001", True, None)
    assert edit_name(_resolved("Cost customfield_90001", hidden), plain) == "Cost <id>"
    desc = ProviderField("description", "Description", False, FieldSchema("string"))
    assert edit_name(_resolved("Description", desc), plain) == "description"
