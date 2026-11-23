"""``changelog KEY`` through the CLI against the fake provider (#46).

Everything is synthetic. A hostile provider answer replaces the changelog route of the fake with a
handler that returns the body a test wants (``serve`` and ``reply``).
"""

from __future__ import annotations

import json
import re
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from tests.fake_provider import (
    F_SPRINT,
    F_VALUE,
    ME_ACCOUNT,
    OTHER_ACCOUNT,
    SITE_TOKEN,
    FakeProvider,
    Out,
)
from tests.golden import assert_golden
from tests.test_read_commands import Env, override, reply
from ticketctl import changelogcmd, sanitize

BASE = datetime(2026, 3, 1, 15, 0, tzinfo=UTC)
CUSTOM_ID = re.compile(r"customfield_[0-9]+", re.IGNORECASE)
NASTY = "a‮b\x07c\x1b[31m"
PATH_END = "/changelog"


@pytest.fixture(autouse=True)
def _scrubber_reset() -> Any:
    sanitize.configure_scrubber([])
    yield
    sanitize.configure_scrubber([])


@pytest.fixture
def env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: Any) -> Env:
    return Env(tmp_path, monkeypatch, capsys)


@pytest.fixture
def site(env: Env, fake: FakeProvider) -> FakeProvider:
    env.use(
        fake,
        "projects: [DEMO]",
        "fields:",
        "  extra:",
        f"    value: {{id: {F_VALUE}}}",
    )
    return fake


def status(old: str | None, new: str | None) -> dict[str, Any]:
    return FakeProvider.history_item(
        "status", old, new, field_id="status", old_id="10000", new_id="10001"
    )


def at(minutes: int) -> datetime:
    return BASE + timedelta(minutes=minutes)


def hist(fake: FakeProvider, minutes: int, *items: dict[str, Any], **kw: Any) -> None:
    fake.add_history("DEMO-2", *items, when=at(minutes), **kw)


def raw(eid: str, minutes: int, items: list[Any] | None = None, **kw: Any) -> dict[str, Any]:
    stamp = at(minutes).strftime("%Y-%m-%dT%H:%M:%S.000+0000")
    out: dict[str, Any] = {
        "id": eid,
        "author": {"accountId": OTHER_ACCOUNT, "displayName": "Fake Other"},
        "created": stamp,
        "items": [status("To Do", "Done")] if items is None else items,
    }
    out.update(kw)
    return out


def rows(count: int, first: int = 1) -> list[dict[str, Any]]:
    return [raw(str(first + n), first + n) for n in range(count)]


def serve(
    fake: FakeProvider,
    served: list[dict[str, Any]],
    *,
    total: int | None = None,
    page_cap: int | None = None,
    whole: bool = False,
) -> list[tuple[int, int]]:
    """Answer the changelog endpoint from ``served`` in that order; collect ``(startAt, size)``."""
    seen: list[tuple[int, int]] = []

    def page(call: Any, key: str) -> Out:
        start = int(call.one("startAt", "0"))
        size = int(call.one("maxResults", "100"))
        chunk = served[start:] if whole else served[start : start + min(size, page_cap or size)]
        seen.append((start, len(chunk)))
        count = len(served) if total is None else total
        return Out(
            200,
            {"startAt": start, "maxResults": size, "total": count, "values": chunk},
        )

    override(fake, "GET", PATH_END, page)
    return seen


def ids(doc: dict[str, Any]) -> list[str]:
    return [e["id"] for e in doc["items"]]


def calls(fake: FakeProvider) -> list[Any]:
    return [c for c in fake.calls if c.path.endswith(PATH_END)]


def golden(case: str, doc: Any) -> None:
    assert_golden("cli", "changelog", case, doc)


@pytest.fixture
def story(site: FakeProvider) -> FakeProvider:
    """DEMO-2 with a history of five entries covering the typed fields."""
    hist(site, 0, status("To Do", "In Progress"), entry_id="9001")
    hist(
        site,
        10,
        FakeProvider.history_item(
            "assignee",
            "Fake Other",
            "Fake Me",
            field_id="assignee",
            old_id=OTHER_ACCOUNT,
            new_id=ME_ACCOUNT,
        ),
        FakeProvider.history_item("labels", "alpha", "alpha beta", field_id="labels"),
        author=OTHER_ACCOUNT,
        entry_id="9002",
    )
    hist(
        site,
        20,
        FakeProvider.history_item(
            "Sprint",
            "Sprint one",
            "Sprint one, Sprint two",
            field_id=F_SPRINT,
            old_id="1",
            new_id="1, 2",
            custom=True,
        ),
        FakeProvider.history_item("Business value", "1", "3", field_id=F_VALUE, custom=True),
        entry_id="9003",
    )
    hist(
        site,
        30,
        FakeProvider.history_item("resolution", None, "Done", field_id="resolution"),
        FakeProvider.history_item("priority", "Medium", "High", field_id="priority"),
        FakeProvider.history_item("summary", "Old title", "New title", field_id="summary"),
        entry_id="9004",
    )
    hist(
        site,
        40,
        FakeProvider.history_item("Flavor", "mild", "hot", field_id="customfield_1", custom=True),
        entry_id="9005",
        author=None,
    )
    return site


# --- shape and typing ---------------------------------------------------------------------------


def test_default_is_oldest_first_with_typed_values(env: Env, story: FakeProvider) -> None:
    code, doc, err = env.json("changelog", "demo-2")
    assert code == 0 and err == ""
    assert (doc["key"], doc["site"], doc["total"], doc["truncated"]) == ("DEMO-2", "main", 5, False)
    assert doc["incomplete"] == []
    assert ids(doc) == ["9001", "9002", "9003", "9004", "9005"]
    first = doc["items"][0]
    assert first["created"] == "2026-03-01T15:00:00Z"
    assert first["author"] == {"account_id": ME_ACCOUNT, "display_name": "Fake Me"}
    assert first["items"] == [{"field": "status", "from": "To Do", "to": "In Progress"}]
    golden("default", doc)


def test_assignee_and_labels_are_typed(env: Env, story: FakeProvider) -> None:
    _, doc, _ = env.json("changelog", "DEMO-2")
    by_field = {i["field"]: i for e in doc["items"] for i in e["items"]}
    assert by_field["assignee"]["from"] == {
        "account_id": OTHER_ACCOUNT,
        "display_name": "Fake Other",
    }
    assert by_field["assignee"]["to"] == {"account_id": ME_ACCOUNT, "display_name": "Fake Me"}
    assert by_field["labels"]["from"] == ["alpha"]
    assert by_field["labels"]["to"] == ["alpha", "beta"]


def test_sprint_values_pair_ids_with_names(env: Env, story: FakeProvider) -> None:
    _, doc, _ = env.json("changelog", "DEMO-2")
    by_field = {i["field"]: i for e in doc["items"] for i in e["items"]}
    assert by_field["sprint"]["from"] == [{"id": 1, "name": "Sprint one"}]
    assert by_field["sprint"]["to"] == [
        {"id": 1, "name": "Sprint one"},
        {"id": 2, "name": "Sprint two"},
    ]


def test_field_names_follow_the_field_map_and_no_custom_id_is_printed(
    env: Env, story: FakeProvider
) -> None:
    code, out, err = env.run("--json", "changelog", "DEMO-2")
    names = {i["field"] for e in json.loads(out)["items"] for i in e["items"]}
    assert {"status", "assignee", "labels", "sprint", "value", "resolution", "priority"} <= names
    assert "title" in names  # the summary
    assert "Flavor" in names  # unmapped: the provider's display name
    assert CUSTOM_ID.search(out) is None and CUSTOM_ID.search(err) is None


def test_a_missing_author_is_an_unknown_user_without_a_mark(env: Env, story: FakeProvider) -> None:
    _, doc, err = env.json("changelog", "DEMO-2")
    assert doc["items"][-1]["author"] == {"account_id": None, "display_name": "unknown user"}
    assert doc["incomplete"] == [] and err == ""


def test_an_empty_history(env: Env, site: FakeProvider) -> None:
    code, doc, err = env.json("changelog", "DEMO-1")
    assert code == 0 and err == ""
    assert doc == {
        "key": "DEMO-1",
        "site": "main",
        "items": [],
        "total": 0,
        "truncated": False,
        "incomplete": [],
    }
    golden("empty", doc)


def test_the_request_is_a_plain_paged_get_and_nothing_is_written(
    env: Env, story: FakeProvider
) -> None:
    env.json("changelog", "DEMO-2")
    (call,) = calls(story)
    assert call.method == "GET"
    assert (call.one("startAt"), call.one("maxResults")) == ("0", "100")
    assert story.applied == []
    assert not any("audit" in name for name in env.files())
    assert not any("lock" in name for name in env.files())


def test_a_moved_ticket_is_read_under_its_old_key(env: Env, story: FakeProvider) -> None:
    story.moved["DEMO-77"] = "DEMO-2"
    code, doc, _ = env.json("changelog", "DEMO-77")
    assert code == 0 and doc["key"] == "DEMO-77" and len(doc["items"]) == 5


# --- order, limit, filters ----------------------------------------------------------------------


def test_limit_takes_the_oldest_n_and_newest_first_the_newest_n(
    env: Env, story: FakeProvider
) -> None:
    _, oldest, _ = env.json("changelog", "DEMO-2", "--limit", "2")
    assert ids(oldest) == ["9001", "9002"] and oldest["truncated"] is True and oldest["total"] == 5
    _, newest, _ = env.json("changelog", "DEMO-2", "--limit", "2", "--newest-first")
    assert ids(newest) == ["9005", "9004"] and newest["truncated"] is True
    golden("newest-first-limited", newest)


def test_limit_equal_to_the_matches_is_not_truncated(env: Env, story: FakeProvider) -> None:
    _, doc, _ = env.json("changelog", "DEMO-2", "--limit", "5")
    assert doc["truncated"] is False and len(doc["items"]) == 5


def test_field_keeps_only_the_named_items_and_drops_empty_entries(
    env: Env, story: FakeProvider
) -> None:
    _, doc, _ = env.json("changelog", "DEMO-2", "--field", "status", "--field", "PRIORITY")
    assert ids(doc) == ["9001", "9004"]
    assert [i["field"] for i in doc["items"][1]["items"]] == ["priority"]
    assert doc["total"] == 5 and doc["truncated"] is False


def test_field_matches_the_printed_name_not_the_id(env: Env, story: FakeProvider) -> None:
    _, by_logical, _ = env.json("changelog", "DEMO-2", "--field", "value")
    assert ids(by_logical) == ["9003"]
    _, by_id, _ = env.json("changelog", "DEMO-2", "--field", F_VALUE)
    assert by_id["items"] == []
    _, display, _ = env.json("changelog", "DEMO-2", "--field", "flavor")
    assert ids(display) == ["9005"]


def test_since_filters_on_created_inclusively_and_total_counts_all(
    env: Env, story: FakeProvider
) -> None:
    _, doc, _ = env.json("changelog", "DEMO-2", "--since", "2026-03-01T15:20:00Z")
    assert ids(doc) == ["9003", "9004", "9005"] and doc["total"] == 5
    assert doc["truncated"] is False


@pytest.mark.parametrize(
    ("since", "expected"),
    [
        ("2026-03-01T10:20:00-05:00", ["9003", "9004", "9005"]),  # 15:20Z, offset west
        ("2026-03-01T20:50:00+05:30", ["9003", "9004", "9005"]),  # 15:20Z, offset east
        ("2026-03-01T15:20:01Z", ["9004", "9005"]),
        ("2026-03-01T15:19:59.999Z", ["9003", "9004", "9005"]),  # fraction dropped, cut-off earlier
        ("2026-03-01", ["9001", "9002", "9003", "9004", "9005"]),  # 00:00 UTC
        ("2026-03-02", []),
        ("2026-03-01T09:00:00-06:00", ["9001", "9002", "9003", "9004", "9005"]),  # 15:00Z
        ("2026-03-01T09:30:00-06:00", ["9004", "9005"]),  # 15:30Z
    ],
)
def test_since_across_time_zones(
    env: Env, story: FakeProvider, since: str, expected: list[str]
) -> None:
    _, doc, _ = env.json("changelog", "DEMO-2", "--since", since)
    assert ids(doc) == expected


def test_a_since_on_an_offset_changelog_stamp(env: Env, site: FakeProvider) -> None:
    """The fake stamps with a west-of-UTC offset: the filter compares instants, not strings."""
    hist(site, 0, status("a", "b"), entry_id="1")
    hist(site, 60, status("b", "c"), entry_id="2")
    _, doc, _ = env.json("changelog", "DEMO-2", "--since", "2026-03-01T16:00:00Z")
    assert ids(doc) == ["2"]
    assert doc["items"][0]["created"] == "2026-03-01T16:00:00Z"


@pytest.mark.parametrize(
    "value",
    ["", "yesterday", "2026-13-01", "2026-03-01T10:00:00", "5", "99999d", "--1d", "2026-03-01\n"],
)
def test_a_bad_since_is_refused_before_any_request(
    env: Env, story: FakeProvider, value: str
) -> None:
    code, doc, _ = env.json("changelog", "DEMO-2", f"--since={value}")
    assert (code, doc["error"]["reason"]) == (22, "invalid_value")
    assert story.calls == []


@pytest.mark.parametrize("limit", ["0", "201", "1000000"])
def test_a_bad_limit_is_exit_22_before_any_request(
    env: Env, story: FakeProvider, limit: str
) -> None:
    code, doc, _ = env.json("changelog", "DEMO-2", "--limit", limit)
    assert (code, doc["error"]["reason"]) == (22, "invalid_value") and story.calls == []


def test_a_non_numeric_limit_is_a_usage_error(env: Env, story: FakeProvider) -> None:
    code, doc, _ = env.json("changelog", "DEMO-2", "--limit", "x")
    assert (code, doc["error"]["reason"]) == (2, "invalid_argument")


@pytest.mark.parametrize("bad", ["", "   ", "x" * 257])
def test_a_bad_field_filter_is_exit_22_before_any_request(
    env: Env, story: FakeProvider, bad: str
) -> None:
    code, doc, _ = env.json("changelog", "DEMO-2", f"--field={bad}")
    assert (code, doc["error"]["reason"]) == (22, "invalid_value") and story.calls == []


def test_more_than_20_field_filters_are_refused(env: Env, story: FakeProvider) -> None:
    args = [a for n in range(21) for a in ("--field", f"f{n}")]
    code, doc, _ = env.json("changelog", "DEMO-2", *args)
    assert (code, doc["error"]["reason"]) == (22, "invalid_value") and story.calls == []


def test_write_flags_are_refused_on_a_read(env: Env, story: FakeProvider) -> None:
    code, doc, _ = env.json("changelog", "DEMO-2", "--yes")
    assert (code, doc["error"]["reason"]) == (2, "invalid_argument") and story.calls == []


# --- errors -------------------------------------------------------------------------------------


def test_a_bad_key_is_exit_22_and_a_missing_ticket_exit_10(env: Env, story: FakeProvider) -> None:
    assert env.json("changelog", "not a key")[0] == 22
    code, doc, _ = env.json("changelog", "DEMO-99")
    assert code == 10 and doc["error"]["code"] == "not_found"
    assert "missing or you have no permission" in doc["error"]["message"]


def test_a_forbidden_ticket_is_exit_12(env: Env, story: FakeProvider) -> None:
    reply(story, "GET", PATH_END, {"errorMessages": ["no"]}, status=403)
    code, doc, _ = env.json("changelog", "DEMO-2")
    assert code == 12 and doc["error"]["code"] == "forbidden"


def test_an_unexpected_status_is_a_provider_change(env: Env, story: FakeProvider) -> None:
    reply(story, "GET", PATH_END, {"errorMessages": ["odd"]}, status=400)
    code, doc, _ = env.json("changelog", "DEMO-2")
    assert code == 12 and doc["error"]["code"] == "provider_changed"


@pytest.mark.parametrize("bad", [None, [], "x", 5])
def test_a_first_answer_that_is_not_an_object_is_a_provider_change(
    env: Env, story: FakeProvider, bad: Any
) -> None:
    reply(story, "GET", PATH_END, bad)
    code, doc, _ = env.json("changelog", "DEMO-2")
    assert code == 12 and doc["error"]["code"] == "provider_changed"


@pytest.mark.parametrize("bad", [{"total": 3}, {"values": "no", "total": 1}, {"values": None}])
def test_a_page_with_no_value_list_is_truncated_and_named(
    env: Env, story: FakeProvider, bad: Any
) -> None:
    reply(story, "GET", PATH_END, bad)
    code, doc, err = env.json("changelog", "DEMO-2")
    assert code == 0 and doc["items"] == [] and doc["truncated"] is True
    assert "changelog" in doc["incomplete"] and "warning" in err


# --- paging and caps ----------------------------------------------------------------------------


def test_a_long_history_is_read_in_pages(env: Env, site: FakeProvider) -> None:
    for n in range(250):
        hist(site, n, status("a", "b"), entry_id=str(1000 + n))
    _, doc, _ = env.json("changelog", "DEMO-2", "--limit", "200")
    assert len(doc["items"]) == 200 and doc["total"] == 250 and doc["truncated"] is True
    assert ids(doc)[0] == "1000" and ids(doc)[-1] == "1199"
    assert [c.one("startAt") for c in calls(site)] == ["0", "100", "200"]
    _, newest, _ = env.json("changelog", "DEMO-2", "--limit", "3", "--newest-first")
    assert ids(newest) == ["1249", "1248", "1247"]


def test_the_scan_stops_at_1000_entries_and_names_the_cut(env: Env, site: FakeProvider) -> None:
    served = rows(1100)
    seen = serve(site, served)
    _, doc, err = env.json("changelog", "DEMO-2", "--since", "2030-01-01")
    assert doc["items"] == [] and doc["total"] == 1100 and doc["truncated"] is True
    assert len(seen) == 10 and "changelog.truncated" in doc["incomplete"] and "warning" in err


def test_newest_first_on_a_history_above_the_window_reads_the_newest_end(
    env: Env, site: FakeProvider
) -> None:
    seen = serve(site, rows(1100))
    _, doc, _ = env.json("changelog", "DEMO-2", "--newest-first", "--limit", "2")
    assert ids(doc) == ["1100", "1099"] and doc["truncated"] is True and doc["total"] == 1100
    assert seen[0] == (0, 100) and seen[1][0] == 100  # the count came from page 1, then the jump
    assert min(start for start, _ in seen[1:]) == 100


def test_a_provider_that_ignores_startat_cannot_loop(env: Env, site: FakeProvider) -> None:
    def page(call: Any, key: str) -> Out:
        return Out(200, {"startAt": 0, "total": 10**9, "values": [raw("5", 1)]})

    override(site, "GET", PATH_END, page)
    code, doc, err = env.json("changelog", "DEMO-2")
    assert code == 0 and ids(doc) == ["5"] and doc["truncated"] is True
    assert len(calls(site)) == 20 and "changelog" in doc["incomplete"]


def test_an_empty_first_page_with_a_larger_total_is_truncated(env: Env, site: FakeProvider) -> None:
    serve(site, [], total=7)
    _, doc, _ = env.json("changelog", "DEMO-2")
    assert doc["items"] == [] and doc["total"] == 7 and doc["truncated"] is True


def test_short_pages_advance_start_by_the_items_received(env: Env, site: FakeProvider) -> None:
    seen = serve(site, rows(100), page_cap=30)
    _, doc, _ = env.json("changelog", "DEMO-2", "--limit", "200")
    assert [start for start, _ in seen] == [0, 30, 60, 90]
    assert len(doc["items"]) == 100 and doc["truncated"] is False


def test_a_provider_ignoring_order_still_yields_the_true_oldest(
    env: Env, site: FakeProvider
) -> None:
    serve(site, list(reversed(rows(150))))  # newest first although the doc assumes oldest first
    _, doc, _ = env.json("changelog", "DEMO-2", "--limit", "3")
    assert ids(doc) == ["1", "2", "3"] and doc["truncated"] is True


def test_a_missing_total_is_unknown_and_truncated_unless_the_page_is_last(
    env: Env, site: FakeProvider
) -> None:
    reply(site, "GET", PATH_END, {"values": [raw("9", 1)]})
    _, doc, err = env.json("changelog", "DEMO-2")
    assert doc["total"] == 1 and doc["truncated"] is True and "changelog" in doc["incomplete"]
    assert "warning" in err
    reply(site, "GET", PATH_END, {"values": [raw("9", 1)], "isLast": True})
    _, last, _ = env.json("changelog", "DEMO-2")
    assert last["truncated"] is False and last["total"] == 1 and last["incomplete"] == []


# --- malformed entries and hostile values -------------------------------------------------------


def test_malformed_entries_are_dropped_with_marks(env: Env, site: FakeProvider) -> None:
    good = raw("1", 1)
    bad = [
        "text",
        None,
        {**good, "id": "has space"},
        {**good, "id": 7},
        {**good, "id": "2", "created": "x"},
        {**good, "id": "3", "created": 5},
        {k: v for k, v in good.items() if k != "id"},
        {**good, "id": "4", "created": "2026-03-01T10:00:00"},  # naive
    ]
    reply(site, "GET", PATH_END, {"total": len(bad) + 1, "values": [good, *bad]})
    code, doc, err = env.json("changelog", "DEMO-2")
    assert code == 0 and ids(doc) == ["1"] and doc["truncated"] is True
    assert {"changelog", "changelog.invalid_id"} <= set(doc["incomplete"])
    assert "warning" in err
    quiet = env.run("--quiet", "changelog", "DEMO-2")
    assert quiet[0] == 0 and quiet[2] == ""


def test_a_repeated_entry_id_is_kept_once(env: Env, site: FakeProvider) -> None:
    good = raw("1", 1)
    reply(site, "GET", PATH_END, {"total": 2, "values": [good, good]})
    _, doc, _ = env.json("changelog", "DEMO-2")
    assert ids(doc) == ["1"] and doc["truncated"] is True and "changelog" in doc["incomplete"]


def test_entries_are_ordered_by_created_then_id_whatever_the_provider_sent(
    env: Env, site: FakeProvider
) -> None:
    served = [raw("20", 5), raw("3", 5), raw("100", 1), raw("9", 5)]
    serve(site, served)
    _, doc, _ = env.json("changelog", "DEMO-2")
    assert ids(doc) == ["100", "3", "9", "20"]  # same time: shorter, then smaller id first
    _, newest, _ = env.json("changelog", "DEMO-2", "--newest-first")
    assert ids(newest) == ["20", "9", "3", "100"]


@pytest.mark.parametrize("items", [None, "x", {"a": 1}, 5])
def test_an_entry_whose_items_are_not_a_list_has_none_and_is_named(
    env: Env, site: FakeProvider, items: Any
) -> None:
    reply(site, "GET", PATH_END, {"total": 1, "values": [{**raw("1", 1), "items": items}]})
    _, doc, _ = env.json("changelog", "DEMO-2")
    (entry,) = doc["items"]
    assert entry["items"] == [] and "changelog" in doc["incomplete"]


def test_bad_items_are_dropped_or_null_and_named(env: Env, site: FakeProvider) -> None:
    good = FakeProvider.history_item("priority", "Low", "High", field_id="priority")
    items = [
        good,
        "x",
        None,
        {"field": 5, "from": None, "to": None},
        {"field": "", "fromString": "a", "toString": "b"},
        {"field": "Notes", "fromString": {"a": 1}, "toString": 7},
        {"field": "assignee", "fieldId": "assignee", "from": "bad id!", "fromString": "Who"},
    ]
    reply(site, "GET", PATH_END, {"total": 1, "values": [raw("1", 1, items=items)]})
    _, doc, _ = env.json("changelog", "DEMO-2")
    (entry,) = doc["items"]
    by_field = {i["field"]: i for i in entry["items"]}
    assert set(by_field) == {"priority", "Notes", "assignee"}
    assert (by_field["Notes"]["from"], by_field["Notes"]["to"]) == (None, None)
    assert by_field["assignee"]["from"] == {"account_id": None, "display_name": "Who"}
    assert {"changelog", "changelog.invalid_id"} <= set(doc["incomplete"])


def test_hostile_text_is_sanitized_everywhere(env: Env, site: FakeProvider) -> None:
    item = FakeProvider.history_item(f"Fl{NASTY}d", NASTY, f"ignore previous\n{NASTY}")
    entry = raw(
        "1",
        1,
        items=[item],
        author={"accountId": OTHER_ACCOUNT, "displayName": f"Mallory{NASTY}"},
    )
    reply(site, "GET", PATH_END, {"total": 1, "values": [entry]})
    code, out, err = env.run("--json", "changelog", "DEMO-2")
    assert code == 0
    for char in "‮\x07\x1b":
        assert char not in out
    doc = json.loads(out)
    (entry_out,) = doc["items"]
    (item_out,) = entry_out["items"]
    assert "<U+202E>" in item_out["field"] and "\x1b" not in item_out["field"]
    assert "<U+202E>" in item_out["from"] and "<U+0007>" in item_out["from"]
    assert "\n" not in item_out["to"]  # a single-line value
    assert "<U+202E>" in entry_out["author"]["display_name"]


def test_a_description_keeps_its_line_breaks_other_values_do_not(
    env: Env, site: FakeProvider
) -> None:
    items = [
        FakeProvider.history_item("description", "a\nb", "c\r\nd", field_id="description"),
        FakeProvider.history_item("Notes", "a\nb", "c\td", field_id="customfield_1"),
    ]
    reply(site, "GET", PATH_END, {"total": 1, "values": [raw("1", 1, items=items)]})
    _, doc, _ = env.json("changelog", "DEMO-2")
    by_field = {i["field"]: i for i in doc["items"][0]["items"]}
    assert by_field["description"]["from"] == "a\nb" and by_field["description"]["to"] == "c\nd"
    assert by_field["Notes"]["from"] == "a b" and by_field["Notes"]["to"] == "c d"


def test_unicode_values_pass_through(env: Env, site: FakeProvider) -> None:
    item = FakeProvider.history_item("priority", "Médio", "高い", field_id="priority")
    reply(site, "GET", PATH_END, {"total": 1, "values": [raw("1", 1, items=[item])]})
    _, doc, _ = env.json("changelog", "DEMO-2")
    assert doc["items"][0]["items"][0] == {"field": "priority", "from": "Médio", "to": "高い"}


def test_a_huge_value_is_cut_with_the_marker_and_named(env: Env, site: FakeProvider) -> None:
    items = [
        FakeProvider.history_item("Notes", "x" * 10_000, "ok", field_id="customfield_1"),
        FakeProvider.history_item("description", "y", "z" * 1_200_000, field_id="description"),
        FakeProvider.history_item("F" * 600, "a", "b", field_id="customfield_1"),
    ]
    reply(site, "GET", PATH_END, {"total": 1, "values": [raw("1", 1, items=items)]})
    _, doc, _ = env.json("changelog", "DEMO-2")
    by_field = {i["field"]: i for i in doc["items"][0]["items"]}
    notes = by_field["Notes"]["from"]
    assert len(notes) == 4096 and notes.endswith("<truncated>")
    description = by_field["description"]["to"]
    assert len(description) == 1_000_000 and description.endswith("<truncated>")
    (long_name,) = [n for n in by_field if n.startswith("FFF")]
    assert len(long_name) == 256 and long_name.endswith("<truncated>")
    assert "changelog.truncated" in doc["incomplete"]


def test_at_most_200_items_of_an_entry_are_kept(env: Env, site: FakeProvider) -> None:
    items = [
        FakeProvider.history_item(f"F{n}", "a", "b", field_id=f"customfield_{n}")
        for n in range(250)
    ]
    reply(site, "GET", PATH_END, {"total": 1, "values": [raw("1", 1, items=items)]})
    _, doc, _ = env.json("changelog", "DEMO-2")
    assert len(doc["items"][0]["items"]) == 200 and "changelog.truncated" in doc["incomplete"]


def test_a_huge_label_list_is_cut(env: Env, site: FakeProvider) -> None:
    labels = " ".join(f"l{n}" for n in range(1500))
    item = FakeProvider.history_item("labels", "", labels, field_id="labels")
    reply(site, "GET", PATH_END, {"total": 1, "values": [raw("1", 1, items=[item])]})
    _, doc, _ = env.json("changelog", "DEMO-2")
    value = doc["items"][0]["items"][0]
    assert value["from"] == [] and len(value["to"]) == 1000
    assert "changelog.truncated" in doc["incomplete"]


@pytest.mark.parametrize(
    ("ids_text", "names_text", "expected"),
    [
        ("1, 2", "A, B", [{"id": 1, "name": "A"}, {"id": 2, "name": "B"}]),
        ("1, 2", "A, B, C", [{"id": 1, "name": None}, {"id": 2, "name": None}]),
        ("7", "Has, comma", [{"id": 7, "name": None}]),
        ("x, 2", "A, B", [{"id": None, "name": "A"}, {"id": 2, "name": "B"}]),
        (None, "A, B", [{"id": None, "name": "A"}, {"id": None, "name": "B"}]),
        (None, None, []),
        ("", "", []),
        ("1" * 30, "A", [{"id": None, "name": "A"}]),
    ],
)
def test_sprint_pairing_is_conservative(
    env: Env,
    site: FakeProvider,
    ids_text: str | None,
    names_text: str | None,
    expected: list[dict[str, Any]],
) -> None:
    item = FakeProvider.history_item(
        "Sprint", "", names_text, field_id=F_SPRINT, old_id=None, new_id=ids_text, custom=True
    )
    reply(site, "GET", PATH_END, {"total": 1, "values": [raw("1", 1, items=[item])]})
    _, doc, _ = env.json("changelog", "DEMO-2")
    assert doc["items"][0]["items"][0]["to"] == expected


def test_a_secret_in_a_value_is_scrubbed(env: Env, site: FakeProvider) -> None:
    item = FakeProvider.history_item(
        "Notes", f"key {SITE_TOKEN}", f"{SITE_TOKEN}", field_id="customfield_1"
    )
    entry = raw(
        "1", 1, items=[item], author={"accountId": OTHER_ACCOUNT, "displayName": SITE_TOKEN}
    )
    reply(site, "GET", PATH_END, {"total": 1, "values": [entry]})
    code, out, err = env.run("--json", "changelog", "DEMO-2")
    assert code == 0 and SITE_TOKEN not in out and SITE_TOKEN not in err
    assert "<redacted>" in out


def test_a_custom_field_id_in_a_display_name_is_hidden(env: Env, site: FakeProvider) -> None:
    item = FakeProvider.history_item("Copy of customfield_1", "a", "b")
    reply(site, "GET", PATH_END, {"total": 1, "values": [raw("1", 1, items=[item])]})
    code, out, _ = env.run("--json", "changelog", "DEMO-2")
    assert code == 0 and CUSTOM_ID.search(out) is None and "<id>" in out


def test_hostile_json_values_do_not_crash(env: Env, site: FakeProvider) -> None:
    entry = raw("1", 1, items=[{"field": "Notes", "fieldId": ["x"], "from": ["a"], "to": {"b": 1}}])
    reply(site, "GET", PATH_END, {"total": 1, "values": [entry]})
    code, doc, _ = env.json("changelog", "DEMO-2")
    assert code == 0 and doc["items"][0]["items"][0]["field"] == "Notes"


# --- output forms and the pure helpers ----------------------------------------------------------


def test_human_output_lists_each_change(env: Env, story: FakeProvider) -> None:
    code, out, err = env.run("changelog", "DEMO-2", "--limit", "1")
    assert code == 0
    assert out.startswith("DEMO-2: 1 of 5 entries, more")
    assert "status: To Do -> In Progress" in out


def test_the_one_time_grammar_is_shared_with_comments() -> None:
    assert changelogcmd.since_cutoff("2026-03-01") == "2026-03-01T00:00:00Z"
    assert changelogcmd.effective_limit(None) == 50
    assert changelogcmd.MAX_SCAN == 1000 and changelogcmd.MAX_REQUESTS == 20


# --- round 1 review fixes -----------------------------------------------------------------------


def _summary_items(count: int) -> list[dict[str, Any]]:
    return [
        FakeProvider.history_item("summary", "a", "b", field_id="summary") for _ in range(count)
    ]


def test_field_filter_is_applied_before_the_item_cap(env: Env, site: FakeProvider) -> None:
    items = [*_summary_items(250), status("To Do", "Done")]
    reply(site, "GET", PATH_END, {"total": 1, "values": [raw("1", 1, items=items)]})
    _, doc, _ = env.json("changelog", "DEMO-2", "--field", "status")
    assert [i["field"] for i in doc["items"][0]["items"]] == ["status"]
    assert doc["truncated"] is False and doc["incomplete"] == []


def test_field_filter_that_still_exceeds_the_item_cap_is_truncated_and_named(
    env: Env, site: FakeProvider
) -> None:
    items = [status("a", "b") for _ in range(250)]
    reply(site, "GET", PATH_END, {"total": 1, "values": [raw("1", 1, items=items)]})
    _, doc, _ = env.json("changelog", "DEMO-2", "--field", "status")
    assert len(doc["items"][0]["items"]) == 200 and doc["truncated"] is True
    assert "changelog.truncated" in doc["incomplete"]


def test_field_filter_keeps_the_marks_of_an_entry_it_drops(env: Env, site: FakeProvider) -> None:
    unreadable = {"fieldId": ["x"], "from": "a", "to": "b"}
    entry = raw("1", 1, items=[status("a", "b"), unreadable])
    reply(site, "GET", PATH_END, {"total": 1, "values": [entry]})
    _, doc, _ = env.json("changelog", "DEMO-2", "--field", "priority")
    assert doc["items"] == [] and doc["truncated"] is True and "changelog" in doc["incomplete"]


@pytest.mark.parametrize("items", [None, "x", 5])
def test_field_filter_keeps_the_mark_of_a_bad_items_list(
    env: Env, site: FakeProvider, items: Any
) -> None:
    reply(site, "GET", PATH_END, {"total": 1, "values": [{**raw("1", 1), "items": items}]})
    _, doc, _ = env.json("changelog", "DEMO-2", "--field", "status")
    assert doc["items"] == [] and doc["truncated"] is True and "changelog" in doc["incomplete"]


def test_the_newest_window_follows_the_providers_page_size(env: Env, site: FakeProvider) -> None:
    seen = serve(site, rows(2500), page_cap=50)
    _, doc, _ = env.json("changelog", "DEMO-2", "--newest-first", "--limit", "2")
    assert ids(doc) == ["2500", "2499"] and doc["total"] == 2500 and len(seen) <= 20


def test_a_page_larger_than_the_scan_cap_is_cut_and_named(env: Env, site: FakeProvider) -> None:
    serve(site, rows(5000), whole=True)
    stamp = at(1001).strftime("%Y-%m-%dT%H:%M:%SZ")
    _, doc, _ = env.json("changelog", "DEMO-2", "--since", stamp)
    assert doc["items"] == [] and doc["total"] == 5000 and doc["truncated"] is True
    assert "changelog.truncated" in doc["incomplete"]
    edge = at(1000).strftime("%Y-%m-%dT%H:%M:%SZ")
    _, inside, _ = env.json("changelog", "DEMO-2", "--since", edge)
    assert ids(inside) == ["1000"]


@pytest.mark.parametrize(
    ("ids_text", "names_text"),
    [
        ("1, 2", "A, B, C"),
        ("7", "Has, comma"),
        ("1" * 30, "A"),
        ("x, 2", "A, B"),
        ("1, 2", "A, , B"),
    ],
)
def test_unpairable_sprint_values_name_the_loss(
    env: Env, site: FakeProvider, ids_text: str, names_text: str
) -> None:
    item = FakeProvider.history_item(
        "Sprint", "", names_text, field_id=F_SPRINT, old_id=None, new_id=ids_text, custom=True
    )
    reply(site, "GET", PATH_END, {"total": 1, "values": [raw("1", 1, items=[item])]})
    _, doc, _ = env.json("changelog", "DEMO-2")
    assert "changelog" in doc["incomplete"]


def test_an_empty_name_element_is_never_paired(env: Env, site: FakeProvider) -> None:
    item = FakeProvider.history_item(
        "Sprint", "", "A, ", field_id=F_SPRINT, old_id=None, new_id="1, 2", custom=True
    )
    reply(site, "GET", PATH_END, {"total": 1, "values": [raw("1", 1, items=[item])]})
    _, doc, _ = env.json("changelog", "DEMO-2")
    assert doc["items"][0]["items"][0]["to"] == [
        {"id": 1, "name": None},
        {"id": 2, "name": None},
    ]


def test_a_pairable_sprint_value_names_nothing(env: Env, site: FakeProvider) -> None:
    item = FakeProvider.history_item(
        "Sprint", "", "A, B", field_id=F_SPRINT, old_id=None, new_id="1, 2", custom=True
    )
    reply(site, "GET", PATH_END, {"total": 1, "values": [raw("1", 1, items=[item])]})
    _, doc, _ = env.json("changelog", "DEMO-2")
    assert doc["incomplete"] == []


def test_a_dropped_dedupe_shaped_label_is_named(env: Env, site: FakeProvider) -> None:
    item = FakeProvider.history_item(
        "labels", "alpha", "alpha tc-0123456789abcdef", field_id="labels"
    )
    reply(site, "GET", PATH_END, {"total": 1, "values": [raw("1", 1, items=[item])]})
    _, doc, _ = env.json("changelog", "DEMO-2")
    value = doc["items"][0]["items"][0]
    assert value["from"] == value["to"] == ["alpha"] and "changelog" in doc["incomplete"]


def test_field_filter_ignores_marks_of_known_unrequested_fields(
    env: Env, site: FakeProvider
) -> None:
    label = FakeProvider.history_item(
        "labels", "alpha", "alpha tc-0123456789abcdef", field_id="labels"
    )
    values = [raw("1", 1, items=[label]), raw("2", 2, items=[status("a", "b")])]
    reply(site, "GET", PATH_END, {"total": 2, "values": values})
    _, doc, _ = env.json("changelog", "DEMO-2", "--field", "status")
    assert doc["incomplete"] == [] and doc["truncated"] is False


def test_a_dedupe_shaped_label_on_both_sides_names_nothing(env: Env, site: FakeProvider) -> None:
    item = FakeProvider.history_item(
        "labels", "a tc-0123456789abcdef", "a b tc-0123456789abcdef", field_id="labels"
    )
    reply(site, "GET", PATH_END, {"total": 1, "values": [raw("1", 1, items=[item])]})
    _, doc, _ = env.json("changelog", "DEMO-2")
    assert doc["items"][0]["items"][0]["to"] == ["a", "b"] and doc["incomplete"] == []


def test_an_entry_older_than_since_contributes_no_truncation(env: Env, site: FakeProvider) -> None:
    old = raw("1", 1, items=[status("a", "b") for _ in range(250)])
    new = raw("2", 5, items=[status("a", "b")])
    reply(site, "GET", PATH_END, {"total": 2, "values": [old, new]})
    since = at(3).strftime("%Y-%m-%dT%H:%M:%SZ")
    _, doc, _ = env.json("changelog", "DEMO-2", "--field", "status", "--since", since)
    assert ids(doc) == ["2"] and doc["truncated"] is False and doc["incomplete"] == []
