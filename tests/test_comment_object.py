"""The comment object of a provider comment (issue #15): hostile and edge-case payloads."""

from __future__ import annotations

import json
from typing import Any

import pytest

from ticketctl.comment_object import comment_list, comment_object

ME = "acct-0001"
T0 = "2026-03-01T09:00:00.000-0600"


def raw(**kw: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "id": "10",
        "author": {"accountId": ME, "displayName": "Fake Me", "emailAddress": "x@example.test"},
        "body": {"type": "doc", "version": 1, "content": []},
        "created": T0,
        "updated": T0,
    }
    base.update(kw)
    return base


def one(r: object, me: str | None = ME) -> tuple[dict[str, Any] | None, set[str]]:
    marks: set[str] = set()
    return comment_object(r, me, marks), marks


@pytest.mark.parametrize("bad", [None, 5, "x", [], {}])
def test_a_comment_that_is_not_an_object_is_dropped(bad: object) -> None:
    obj, marks = one(bad)
    assert obj is None and marks


@pytest.mark.parametrize("cid", ["../x", "a b", "", 7, None, "x" * 65])
def test_a_bad_id_drops_the_comment(cid: object) -> None:
    obj, marks = one(raw(id=cid))
    assert obj is None and marks & {"comments", "comments.invalid_id"}


@pytest.mark.parametrize("field", ["created", "updated"])
@pytest.mark.parametrize("value", [None, "yesterday", 5, "2026-03-01T09:00:00"])
def test_an_unreadable_time_drops_the_comment(field: str, value: object) -> None:
    obj, marks = one(raw(**{field: value}))
    assert obj is None and "comments" in marks


def test_no_email_address_is_printed() -> None:
    obj, _ = one(raw())
    assert obj is not None and "x@example.test" not in json.dumps(obj)


def test_an_author_without_a_usable_id_is_unknown_and_never_own() -> None:
    obj, marks = one(raw(author={"accountId": "bad id!", "displayName": "Z"}), me="bad id!")
    assert obj is not None and obj["author"] == {"account_id": None, "display_name": "unknown user"}
    assert obj["own"] is False and "comments.invalid_id" in marks


def test_own_is_an_exact_match() -> None:
    obj, _ = one(raw(), me=ME.upper())
    assert obj is not None and obj["own"] is False
    obj, _ = one(raw(), me=None)
    assert obj is not None and obj["own"] is False


@pytest.mark.parametrize("body", [None, 5, ["x"]])
def test_an_unreadable_body_is_empty_and_named(body: object) -> None:
    obj, marks = one(raw(body=body))
    assert obj is not None and obj["body"] == "" and "comments" in marks


def test_a_string_body_is_sanitized() -> None:
    obj, _ = one(raw(body="a\x1b[2J‮b"))
    assert obj is not None and "\x1b" not in obj["body"] and "‮" not in obj["body"]


def test_an_oversized_body_is_cut_and_truncated() -> None:
    obj, marks = one(raw(body="y" * 1_100_000))
    assert obj is not None and obj["truncated"] is True and "comments.truncated" in marks
    assert len(obj["body"]) <= 1_000_000


@pytest.mark.parametrize("hexed", ["A" * 64, "a" * 63, "g" * 64, 5, None])
def test_a_bad_hash_is_null(hexed: object) -> None:
    props = [
        {"key": "ticketctl.meta", "value": {"v": 1, "actor_kind": "human", "body_sha256": hexed}}
    ]
    obj, _ = one(raw(properties=props))
    assert obj is not None and obj["authored_by_ticketctl"] is True
    assert obj["body_sha256"] is None and obj["actor_kind"] == "human"


@pytest.mark.parametrize("kind", ["bot", None, 3, True])
def test_an_unknown_actor_kind_is_null(kind: object) -> None:
    props = [{"key": "ticketctl.meta", "value": {"v": 1, "actor_kind": kind}}]
    obj, _ = one(raw(properties=props))
    assert obj is not None and obj["actor_kind"] is None


@pytest.mark.parametrize("props", [None, {}, "x", [None], [{"key": "other", "value": {}}]])
def test_no_property_is_not_authored_by_the_tool(props: object) -> None:
    obj, _ = one(raw(properties=props))
    assert obj is not None and obj["authored_by_ticketctl"] is False


def test_the_page_lists_the_newest_n_oldest_first() -> None:
    comments = [raw(id=str(i)) for i in (5, 4, 3, 2, 1)]  # newest first
    page = comment_list({"comments": comments, "total": 5}, 3, ME)
    assert [c["id"] for c in page.value["items"]] == ["3", "4", "5"]
    assert page.value["total"] == 5 and page.value["truncated"] is True


def test_a_provider_that_ignores_max_results_is_cut_to_n() -> None:
    page = comment_list({"comments": [raw(id=str(i)) for i in range(9)], "total": 9}, 2, ME)
    assert len(page.value["items"]) == 2


@pytest.mark.parametrize("page", [None, [], "x", {"comments": "x"}, {"total": -1, "comments": []}])
def test_an_unusable_page_is_empty_and_named(page: object) -> None:
    out = comment_list(page, 5, ME)
    assert out.value["items"] == [] and "comments" in out.incomplete


def test_a_total_below_the_listed_count_is_raised() -> None:
    out = comment_list({"comments": [raw(id="1"), raw(id="2")], "total": 1}, 5, ME)
    assert out.value["total"] == 2 and out.value["truncated"] is False
