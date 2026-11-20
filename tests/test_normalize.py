"""Normalization of a provider issue into the ticket object (issue #13).

Payloads are synthetic and built by hand (the shapes follow docs/design.md, Live verification);
one class goes through the fake provider server to pin that its answers normalize.
"""

from __future__ import annotations

import copy
import json
import random
import re
import time
from typing import Any

import pytest

from tests.conftest import make_gateway
from tests.fake_provider import F_CRITERIA, F_ESTIMATE, F_POINTS, F_SPRINT, FakeProvider
from ticketctl.config import FieldMapping, Fields
from ticketctl.errors import TicketctlError
from ticketctl.fieldload import NameList
from ticketctl.fieldmap import FieldSchema, ProviderField, SiteFields, parse_field_list
from ticketctl.normalize import (
    GET_DEFAULT,
    MAX_LINKS,
    PROPERTIES,
    SEARCH_DEFAULT,
    Context,
    normalize_issue,
    normalize_issues,
    parse_properties,
    plan_request,
)
from ticketctl.sanitize import query

SITE = "demo"
BASE = "https://jira.example.test"
CUSTOM_ID = re.compile(r"customfield_[0-9]+")


def fm(**kw: Any) -> FieldMapping:
    return FieldMapping.model_validate(kw)


def system_fields() -> list[ProviderField]:
    def sysf(fid: str, name: str, type_: str, items: str | None = None) -> ProviderField:
        return ProviderField(fid, name, False, FieldSchema(type_, items, None))

    return [
        sysf("summary", "Summary", "string"),
        sysf("labels", "Labels", "array", "string"),
        sysf("assignee", "Assignee", "user"),
        sysf("priority", "Priority", "priority"),
        sysf("resolution", "Resolution", "resolution"),
        sysf("duedate", "Due date", "date"),
        sysf("parent", "Parent", "issuelink"),
    ]


def custom(
    fid: str, name: str, type_: str, custom_: str, items: str | None = None
) -> ProviderField:
    return ProviderField(fid, name, True, FieldSchema(type_, items, custom_))


def site_fields(
    config: Fields | None = None, extra_fields: list[ProviderField] | None = None
) -> SiteFields:
    fields = [
        *system_fields(),
        custom("customfield_90002", "Points", "number", "com.example:jsw-story-points"),
        custom("customfield_90003", "Sprint", "array", "com.example:gh-sprint", "json"),
        custom("customfield_90004", "Criteria", "string", "com.example:textarea"),
        custom("customfield_90005", "Epic link", "any", "com.example:gh-epic-link"),
        *(extra_fields or []),
    ]
    mapped = config or Fields(acceptance_criteria=fm(id="customfield_90004"))
    return SiteFields(fields, mapped)


def props(raw: str | None = None, command: str = "get", **kw: Any) -> Any:
    return parse_properties(raw, command=command, **kw)  # type: ignore[arg-type]


def ctx(
    raw: str | None = None,
    command: str = "get",
    *,
    sf: SiteFields | None = None,
    priorities: NameList | None = None,
    blocking: tuple[str, ...] = ("Blocks",),
    **kw: Any,
) -> Context:
    return Context(
        site=SITE,
        base_url=BASE,
        props=props(raw, command, **kw),
        site_fields=sf or site_fields(),
        priorities=priorities,
        blocking_link_types=blocking,
    )


def adf(text: str) -> dict[str, Any]:
    return {
        "type": "doc",
        "version": 1,
        "content": [{"type": "paragraph", "content": [{"type": "text", "text": text}]}],
    }


def status(name: str = "In Progress", cat: str = "indeterminate") -> dict[str, Any]:
    return {"name": name, "statusCategory": {"key": cat, "name": cat}}


def issue(key: str = "DEMO-1", **fields: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "summary": "A title",
        "issuetype": {"id": "10", "name": "Task", "subtask": False, "hierarchyLevel": 0},
        "status": status(),
        "issuelinks": [],
    }
    base.update(fields)
    return {"id": "10001", "key": key, "self": "https://elsewhere.example.test/x", "fields": base}


def one(raw: Any, c: Context | None = None) -> dict[str, Any]:
    out = normalize_issue(raw, c or ctx("incomplete,key"))
    assert out is not None
    return out.ticket


def link(
    kind: str,
    other: str,
    *,
    inward: bool,
    cat: str | None = "indeterminate",
    summary: str | None = "Other",
) -> dict[str, Any]:
    fields: dict[str, Any] = {}
    if summary is not None:
        fields["summary"] = summary
    if cat is not None:
        fields["status"] = status("S", cat)
    side = {"id": "2", "key": other, "fields": fields}
    return {"id": "1", "type": {"name": kind}, ("inwardIssue" if inward else "outwardIssue"): side}


def get(raw: dict[str, Any], names: str, **kw: Any) -> dict[str, Any]:
    out = normalize_issue(raw, ctx(names, **kw))
    assert out is not None
    return out.ticket


# --- the --fields vocabulary ---------------------------------------------------------------


def test_default_get_is_everything_but_the_opt_in_parts() -> None:
    names = props().names
    assert "comments" not in names
    assert "description_adf" not in names
    assert {"title", "estimate", "acceptance_criteria", "blocked", "links", "incomplete"} <= set(
        names
    )
    assert names == GET_DEFAULT
    assert "comments" in props(comments=5).names
    assert "description_adf" in props(include_adf=True).names


def test_default_search_is_the_small_list() -> None:
    names = props(None, "search").names
    assert names == SEARCH_DEFAULT
    assert set(names) == {
        "title",
        "status",
        "assignee",
        "priority",
        "estimate",
        "sprints",
        "parent",
        "updated",
        "labels",
        "type",
    }
    assert "description" not in names and "links" not in names


def test_every_property_of_the_table_is_in_the_vocabulary() -> None:
    for name in ("reporter", "incomplete", "created", "resolved", "due_date", "epic", "blocked"):
        assert name in PROPERTIES
        assert name in props(name, comments=1, include_adf=True).names


def test_fields_selection_keeps_canonical_order_and_dedupes() -> None:
    assert props("updated,title,title,key,site").names == ("title", "updated")


@pytest.mark.parametrize(
    ("raw", "reason"),
    [
        ("nope", "unknown_field"),
        ("Title", "unknown_field"),
        ("customfield_90001", "unknown_field"),
        ("title,nope", "unknown_field"),
        ("title,,updated", "invalid_value"),
        ("", "invalid_value"),
        (",", "invalid_value"),
        ("comments", "invalid_value"),
        ("description_adf", "invalid_value"),
        ("x" * 5000, "unknown_field"),
        (",".join(["title"] * 500), "invalid_value"),
        ("title\n", "unknown_field"),
        ("ti‮tle", "unknown_field"),
    ],
)
def test_bad_field_names_are_refused(raw: str, reason: str) -> None:
    with pytest.raises(TicketctlError) as err:
        props(raw)
    assert err.value.code == "invalid_request"
    assert err.value.reason == reason


def test_an_unknown_name_in_the_message_is_sanitized_and_cut() -> None:
    with pytest.raises(TicketctlError) as err:
        props("a\x1b[31m" + "z" * 500)
    assert "\x1b" not in str(err.value)
    assert len(str(err.value)) < 300


def test_comments_and_adf_are_allowed_when_opted_in() -> None:
    assert props("comments", comments=3).names == ("comments",)
    assert props("description_adf", include_adf=True).names == ("description_adf",)


# --- the provider request ------------------------------------------------------------------


def test_request_is_an_explicit_list_of_only_what_is_printed() -> None:
    req = plan_request(props(None, "search"), site_fields())
    assert set(req.fields) == {
        "summary",
        "status",
        "assignee",
        "priority",
        "customfield_90002",
        "customfield_90003",
        "parent",
        "updated",
        "labels",
        "issuetype",
    }
    assert "description" not in req.fields and "issuelinks" not in req.fields


def test_request_for_one_property() -> None:
    assert plan_request(props("title"), site_fields()).fields == ("summary",)
    assert plan_request(props("due_date"), site_fields()).fields == ("duedate",)
    assert plan_request(props("resolved"), site_fields()).fields == ("resolutiondate",)


def test_request_is_never_empty() -> None:
    assert plan_request(props("incomplete"), site_fields()).fields
    assert plan_request(props("url"), site_fields()).fields


def test_blocked_pulls_links_and_epic_pulls_parent_and_type() -> None:
    assert plan_request(props("blocked"), site_fields()).fields == ("issuelinks",)
    assert set(plan_request(props("epic"), site_fields()).fields) == {
        "parent",
        "issuetype",
        "customfield_90005",
    }


def test_an_ambiguous_estimate_is_not_requested() -> None:
    twin = custom("customfield_90001", "Story Points", "number", "com.example:jsw-story-points")
    sf = site_fields(extra_fields=[twin])
    assert "customfield_90002" not in plan_request(props("estimate"), sf).fields
    assert plan_request(props("estimate"), sf).fields  # still an explicit, non-empty list


def test_extra_fields_are_requested_by_their_resolved_id() -> None:
    extra = custom("customfield_90001", "Business value", "number", "com.example:float")
    cfg = Fields(extra={"value": fm(id="customfield_90001")})
    sf = site_fields(cfg, [extra])
    assert plan_request(props("fields"), sf).fields == ("customfield_90001",)


def test_a_mapped_system_field_uses_the_mapped_id() -> None:
    other = custom("customfield_90001", "Due", "date", "com.example:datepicker")
    sf = site_fields(Fields(due_date=fm(id="customfield_90001")), [other])
    assert plan_request(props("due_date"), sf).fields == ("customfield_90001",)
    ticket = get(issue(customfield_90001="2026-05-06"), "due_date", sf=sf)
    assert ticket["due_date"] == "2026-05-06"


def test_comments_are_not_part_of_the_issue_request() -> None:
    # TODO(#18): the comment page is a separate call
    assert plan_request(props("comments", comments=2), site_fields()).fields


# --- identity ------------------------------------------------------------------------------


def test_key_site_url_and_id() -> None:
    t = get(issue(), "url,id,incomplete")
    assert t == {
        "key": "DEMO-1",
        "site": SITE,
        "url": f"{BASE}/browse/DEMO-1",
        "id": "10001",
        "incomplete": [],
    }


def test_the_self_link_of_the_payload_is_never_followed_or_printed() -> None:
    assert "elsewhere" not in json.dumps(get(issue(), "url,id"))


def test_a_moved_ticket_carries_requested_key() -> None:
    out = normalize_issue(issue("DEMO-9"), ctx("title"), requested_key="DEMO-77")
    assert out is not None
    assert out.ticket["key"] == "DEMO-9"
    assert out.ticket["requested_key"] == "DEMO-77"


def test_requested_key_is_absent_when_it_matches() -> None:
    out = normalize_issue(issue("DEMO-9"), ctx("title"), requested_key="DEMO-9")
    assert out is not None
    assert "requested_key" not in out.ticket


def test_an_id_outside_the_grammar_is_dropped_and_named() -> None:
    raw = issue()
    raw["id"] = "bad id/../x"
    t = get(raw, "id,incomplete")
    assert "id" not in t
    assert t["incomplete"] == ["id.invalid_id"]


def test_a_key_outside_the_grammar_gives_no_ticket() -> None:
    raw = issue()
    raw["key"] = "demo-1"
    assert normalize_issue(raw, ctx("title")) is None
    raw["key"] = "DEMO-1\n"
    assert normalize_issue(raw, ctx("title")) is None


def test_a_bad_key_falls_back_to_the_requested_key_as_a_stub() -> None:
    raw = issue()
    raw["key"] = {"x": 1}
    out = normalize_issue(raw, ctx("title"), requested_key="DEMO-4")
    assert out is not None
    assert out.ticket["key"] == "DEMO-4"
    assert out.ticket["incomplete"] == ["ticket"]
    assert "title" not in out.ticket


# --- scalars -------------------------------------------------------------------------------


def test_timestamps_offsets_without_a_colon() -> None:
    raw = issue(
        created="2026-03-04T10:00:00.000-0600",
        updated="2026-03-04T10:00:00.123+0000",
        resolutiondate="2026-03-04T10:00:00.000+0530",
    )
    t = get(raw, "created,updated,resolved")
    assert t["created"] == "2026-03-04T16:00:00Z"
    assert t["updated"] == "2026-03-04T10:00:00Z"
    assert t["resolved"] == "2026-03-04T04:30:00Z"


def test_a_missing_timestamp_is_null_without_incomplete() -> None:
    t = get(issue(created=None), "created,resolved,incomplete")
    assert t["created"] is None and t["resolved"] is None
    assert t["incomplete"] == []


@pytest.mark.parametrize(
    "bad", ["yesterday", "2026-03-04T10:00:00", 12, {}, "9999-12-31T23:59:59-0600"]
)
def test_an_unreadable_timestamp_is_null_and_named(bad: Any) -> None:
    t = get(issue(updated=bad), "updated,incomplete")
    assert t["updated"] is None
    assert t["incomplete"] == ["updated"]


def test_due_date() -> None:
    assert get(issue(duedate="2026-03-04"), "due_date")["due_date"] == "2026-03-04"
    assert get(issue(duedate=None), "due_date")["due_date"] is None
    t = get(issue(duedate="2026-02-30"), "due_date,incomplete")
    assert t["due_date"] is None and t["incomplete"] == ["due_date"]


def test_title_is_cut_with_the_marker() -> None:
    t = get(issue(summary="x" * 5000), "title,incomplete")
    assert len(t["title"]) == 4096
    assert t["title"].endswith("<truncated>")
    assert t["incomplete"] == ["title.truncated"]


def test_title_is_single_line_and_sanitized() -> None:
    t = get(issue(summary="a\nb‮c\x00"), "title")
    assert t["title"] == "a b<U+202E>c<U+0000>"


def test_a_title_that_is_not_text_is_unknown() -> None:
    t = get(issue(summary=5), "title,incomplete")
    assert "title" not in t
    assert t["incomplete"] == ["title"]


def test_type_status_and_resolution() -> None:
    t = get(issue(resolution={"name": "Done"}), "type,status,resolution")
    assert t["type"] == {"name": "Task", "subtask": False}
    assert t["status"] == {"name": "In Progress", "category": "indeterminate"}
    assert t["resolution"] == "Done"


def test_a_done_ticket_with_no_resolution_is_valid_and_done_by_category() -> None:
    t = get(issue(status=status("Done", "done"), resolution=None), "status,resolution,incomplete")
    assert t["status"]["category"] == "done"
    assert t["resolution"] is None
    assert t["incomplete"] == []


@pytest.mark.parametrize(
    "bad",
    [
        None,
        "Done",
        {"name": "Done"},
        {"name": "Done", "statusCategory": {"key": "weird"}},
        {"name": "Done", "statusCategory": None},
        {"statusCategory": {"key": "done"}},
        {"name": 4, "statusCategory": {"key": "done"}},
    ],
)
def test_a_status_that_cannot_be_read_is_omitted_and_named(bad: Any) -> None:
    t = get(issue(status=bad), "status,incomplete")
    assert "status" not in t
    assert t["incomplete"] == ["status"]


def test_a_subtask_flag_must_be_boolean() -> None:
    t = get(issue(issuetype={"name": "Task", "subtask": "yes"}), "type,incomplete")
    assert "type" not in t and t["incomplete"] == ["type"]


def test_labels_hide_dedupe_labels_and_are_capped() -> None:
    labels = [
        "alpha",
        "tc-0123456789abcdef",
        "tc-0123456789abcdeg",
        "TC-0123456789abcdef",
        "x" * 300,
    ]
    t = get(issue(labels=labels), "labels,incomplete")
    assert "tc-0123456789abcdef" not in t["labels"]
    assert t["labels"][:3] == ["alpha", "tc-0123456789abcdeg", "TC-0123456789abcdef"]
    assert len(t["labels"][3]) == 256 and t["labels"][3].endswith("<truncated>")
    assert t["incomplete"] == ["labels.truncated"]


def test_a_dedupe_label_with_a_trailing_newline_is_not_a_dedupe_label() -> None:
    t = get(issue(labels=["tc-0123456789abcdef\n"]), "labels")
    assert len(t["labels"]) == 1


def test_labels_that_are_not_text_are_dropped_and_named() -> None:
    t = get(issue(labels=["a", 5, None, ["x"]]), "labels,incomplete")
    assert t["labels"] == ["a"]
    assert t["incomplete"] == ["labels"]


def test_labels_not_a_list_is_unknown() -> None:
    t = get(issue(labels="a"), "labels,incomplete")
    assert t["labels"] == [] and t["incomplete"] == ["labels"]


def test_too_many_labels_are_cut_and_named() -> None:
    t = get(issue(labels=[f"l{i}" for i in range(5000)]), "labels,incomplete")
    assert len(t["labels"]) == 1000
    assert t["incomplete"] == ["labels"]


def test_an_absent_label_list_is_unknown_not_empty() -> None:
    raw = issue()
    t = get(raw, "labels,incomplete")
    assert t["labels"] == [] and t["incomplete"] == ["labels"]


# --- accounts and priority -----------------------------------------------------------------


def test_accounts_never_carry_an_email() -> None:
    user = {"accountId": "acct-1", "displayName": "Fake One", "emailAddress": "x@example.test"}
    t = get(issue(assignee=user, reporter=user), "assignee,reporter")
    assert t["assignee"] == {"account_id": "acct-1", "display_name": "Fake One"}
    assert not re.search(r"@|emailAddress", json.dumps(t))


def test_an_account_without_id_or_name_is_the_unknown_user() -> None:
    t = get(issue(assignee={"displayName": ""}, reporter={}), "assignee,reporter")
    assert t["assignee"] == {"account_id": None, "display_name": "unknown user"}
    assert t["reporter"] == {"account_id": None, "display_name": "unknown user"}


def test_an_account_id_outside_the_grammar_is_null() -> None:
    t = get(issue(assignee={"accountId": "a b/../", "displayName": "N"}), "assignee")
    assert t["assignee"]["account_id"] is None


def test_unassigned_is_null_and_not_incomplete() -> None:
    t = get(issue(assignee=None), "assignee,incomplete")
    assert t["assignee"] is None and t["incomplete"] == []


@pytest.mark.parametrize("bad", ["me", 5, [], True])
def test_an_account_of_the_wrong_type_is_null_and_named(bad: Any) -> None:
    t = get(issue(assignee=bad), "assignee,incomplete")
    assert t["assignee"] is None and t["incomplete"] == ["assignee"]


def test_display_names_are_untrusted_text() -> None:
    t = get(issue(assignee={"accountId": "a", "displayName": "N‮evil\n" + "y" * 400}), "assignee")
    name = t["assignee"]["display_name"]
    assert "‮" not in name and "\n" not in name and len(name) == 256


def test_priority_rank() -> None:
    names = NameList(("Highest", "High", "Not Set", "Low"), "cache")
    c = ctx("priority", priorities=names)
    assert normalize_issue(issue(priority={"name": "High"}), c).ticket["priority"] == {  # type: ignore[union-attr]
        "name": "High",
        "rank": 1,
    }
    assert normalize_issue(issue(priority={"name": "Not Set"}), c).ticket["priority"]["rank"] == 2  # type: ignore[union-attr]
    assert normalize_issue(issue(priority={"name": "Weird"}), c).ticket["priority"] == {  # type: ignore[union-attr]
        "name": "Weird",
        "rank": None,
    }


def test_priority_null_is_null_and_without_a_list_rank_is_null() -> None:
    assert get(issue(priority=None), "priority")["priority"] is None
    assert get(issue(priority={"name": "High"}), "priority")["priority"] == {
        "name": "High",
        "rank": None,
    }


def test_a_priority_name_is_ranked_after_the_same_sanitizing_as_the_list() -> None:
    sanitized = "Hi<U+202E>gh"
    c = ctx("priority", priorities=NameList(("Low", sanitized), "cache"))
    out = normalize_issue(issue(priority={"name": "Hi‮gh"}), c)
    assert out is not None and out.ticket["priority"] == {"name": sanitized, "rank": 1}


@pytest.mark.parametrize("bad", ["High", 3, {}, {"name": 4}, {"name": ""}, []])
def test_a_priority_that_cannot_be_read_is_null_and_named(bad: Any) -> None:
    t = get(issue(priority=bad), "priority,incomplete")
    assert t["priority"] is None and t["incomplete"] == ["priority"]


# --- descriptions and rich text ------------------------------------------------------------


def test_description_is_rendered_markdown() -> None:
    t = get(issue(description=adf("Hello")), "description")
    assert t["description"] == "Hello"


def test_description_null_and_string() -> None:
    assert get(issue(description=None), "description")["description"] is None
    assert get(issue(description="plain\ttext"), "description")["description"] == "plain\ttext"


def test_description_of_the_wrong_type_is_null_and_named() -> None:
    t = get(issue(description=42), "description,incomplete")
    assert t["description"] is None and t["incomplete"] == ["description"]


def test_description_budgets_name_the_part() -> None:
    deep: dict[str, Any] = {"type": "text", "text": "leaf"}
    for _ in range(80):
        deep = {"type": "blockquote", "content": [deep]}
    t = get(
        issue(description={"type": "doc", "version": 1, "content": [deep]}),
        "description,incomplete",
    )
    assert "[too deep]" in t["description"]
    assert t["incomplete"] == ["description.truncated"]


def test_description_text_is_cut_at_the_body_cap() -> None:
    big = adf("y" * 1_050_000)
    t = get(issue(description=big), "description,incomplete")
    assert len(t["description"]) <= 1_000_000
    assert "description.truncated" in t["incomplete"]


def test_description_adf_is_passed_through_only_when_requested() -> None:
    doc = adf("Hello")
    t = get(issue(description=doc), "description_adf", include_adf=True)
    assert t["description_adf"] == doc
    assert "description_adf" not in get(issue(description=doc), "description")
    t = get(issue(description="str"), "description_adf,incomplete", include_adf=True)
    assert "description_adf" not in t and t["incomplete"] == ["description_adf"]


def test_description_adf_is_a_sanitized_copy_without_non_finite_numbers() -> None:
    doc = {
        "type": "doc",
        "content": [
            {
                "type": "text",
                "text": "a\u202eb\x00",
                "n": float("inf"),
                "k\u202e": [float("nan"), 3],
            }
        ],
    }
    t = get(issue(description=doc), "description_adf", include_adf=True)
    node = t["description_adf"]["content"][0]
    assert node["text"] == "a<U+202E>b<U+0000>"
    assert node["n"] is None and node["k<U+202E>"] == [None, 3]
    assert doc["content"][0]["text"] == "a\u202eb\x00"  # the payload itself is untouched


def test_a_very_deep_description_adf_is_copied_without_recursing() -> None:
    deep: Any = {"type": "text", "text": "x"}
    for _ in range(300):
        deep = {"type": "blockquote", "content": [deep]}
    t = get(
        issue(description={"type": "doc", "content": [deep]}), "description_adf", include_adf=True
    )
    assert t["description_adf"]["type"] == "doc"


def test_acceptance_criteria_from_the_mapped_field() -> None:
    assert (
        get(issue(customfield_90004=adf("Done when")), "acceptance_criteria")["acceptance_criteria"]
        == "Done when"
    )
    assert (
        get(issue(customfield_90004="line1\nline2"), "acceptance_criteria")["acceptance_criteria"]
        == "line1\nline2"
    )
    assert get(issue(customfield_90004=None), "acceptance_criteria")["acceptance_criteria"] is None


def test_acceptance_criteria_without_a_mapping_is_null_and_named_only_when_asked_for() -> None:
    sf = site_fields(Fields())
    default = get(issue(), "acceptance_criteria,incomplete", sf=sf)
    assert default["acceptance_criteria"] is None
    assert default["incomplete"] == ["acceptance_criteria"]
    # a default read (no explicit --fields) does not name it
    out = normalize_issue(issue(), ctx(None, sf=sf))
    assert out is not None
    assert out.ticket["acceptance_criteria"] is None
    assert "acceptance_criteria" not in out.ticket["incomplete"]


def test_acceptance_criteria_mapped_to_a_missing_field_is_named_and_warned() -> None:
    sf = site_fields(Fields(acceptance_criteria=fm(id="customfield_1")))
    out = normalize_issue(issue(), ctx("acceptance_criteria,incomplete", sf=sf))
    assert out is not None
    assert out.ticket["incomplete"] == ["acceptance_criteria"]
    assert out.warnings
    assert "customfield_1" not in json.dumps(out.warnings)


def test_acceptance_criteria_of_a_wrong_type_is_null_and_named() -> None:
    t = get(issue(customfield_90004=["x"]), "acceptance_criteria,incomplete")
    assert t["acceptance_criteria"] is None and t["incomplete"] == ["acceptance_criteria"]


# --- estimate, sprints, epic, parent, extras -----------------------------------------------


def test_estimate_from_the_discovered_field() -> None:
    assert get(issue(customfield_90002=3.5), "estimate")["estimate"] == 3.5
    assert get(issue(customfield_90002=None), "estimate")["estimate"] is None


@pytest.mark.parametrize("bad", ["3", True, 2**70, float("nan"), float("inf"), [], {}])
def test_an_estimate_that_is_not_a_finite_number_is_null_and_named(bad: Any) -> None:
    t = get(issue(customfield_90002=bad), "estimate,incomplete")
    assert t["estimate"] is None and t["incomplete"] == ["estimate"]


def test_an_ambiguous_estimate_is_null_named_and_warned() -> None:
    twin = custom("customfield_90001", "Story Points", "number", "com.example:jsw-story-points")
    out = normalize_issue(
        issue(customfield_90002=3), ctx("estimate,incomplete", sf=site_fields(extra_fields=[twin]))
    )
    assert out is not None
    assert out.ticket["estimate"] is None
    assert out.ticket["incomplete"] == ["estimate"]
    assert len(out.warnings) == 1
    assert "customfield" not in out.warnings[0]


def test_an_absent_estimate_field_is_null() -> None:
    sf = SiteFields(system_fields(), Fields())
    t = get(issue(), "estimate,incomplete", sf=sf)
    assert t["estimate"] is None and t["incomplete"] == ["estimate"]
    out = normalize_issue(issue(), ctx(None, sf=sf))
    assert out is not None and "estimate" not in out.ticket["incomplete"]


def _sprint(sid: Any, state: Any = "active", **kw: Any) -> dict[str, Any]:
    return {
        "id": sid,
        "name": "Sprint",
        "state": state,
        "startDate": "2026-03-02T09:00:00.000-0600",
        "endDate": "2026-03-16T09:00:00.000+0000",
        **kw,
    }


def test_sprints() -> None:
    t = get(
        issue(customfield_90003=[_sprint(7), _sprint(8, "FUTURE", startDate=None, endDate=None)]),
        "sprints",
    )
    assert t["sprints"] == [
        {
            "id": 7,
            "name": "Sprint",
            "state": "active",
            "start": "2026-03-02T15:00:00Z",
            "end": "2026-03-16T09:00:00Z",
        },
        {"id": 8, "name": "Sprint", "state": "future", "start": None, "end": None},
    ]


def test_no_sprints_is_an_empty_list() -> None:
    t = get(issue(customfield_90003=None), "sprints,incomplete")
    assert t["sprints"] == [] and t["incomplete"] == []


@pytest.mark.parametrize(
    ("bad", "part"),
    [
        (_sprint("7; drop"), "sprints.invalid_id"),
        (_sprint(-1), "sprints.invalid_id"),
        (_sprint(True), "sprints.invalid_id"),
        (_sprint(7, "weird"), "sprints"),
        (_sprint(7, None), "sprints"),
        ("not a dict", "sprints"),
        (None, "sprints"),
    ],
)
def test_a_bad_sprint_is_dropped_and_named(bad: Any, part: str) -> None:
    t = get(issue(customfield_90003=[bad, _sprint(9)]), "sprints,incomplete")
    assert [s["id"] for s in t["sprints"]] == [9]
    assert t["incomplete"] == [part]


def test_a_sprint_field_that_is_not_a_list_is_unknown() -> None:
    t = get(issue(customfield_90003="x"), "sprints,incomplete")
    assert t["sprints"] == [] and t["incomplete"] == ["sprints"]


def test_sprint_name_is_untrusted_text() -> None:
    t = get(issue(customfield_90003=[_sprint(1, name="a‮b" + "z" * 300)]), "sprints,incomplete")
    assert "‮" not in t["sprints"][0]["name"] and len(t["sprints"][0]["name"]) == 256
    assert t["incomplete"] == ["sprints.truncated"]


def test_parent_with_embedded_title() -> None:
    parent = {
        "id": "5",
        "key": "DEMO-5",
        "fields": {"summary": "The parent", "issuetype": {"name": "Story", "hierarchyLevel": 0}},
    }
    t = get(issue(parent=parent), "parent")
    assert t["parent"] == {"key": "DEMO-5", "type": "Story", "title": "The parent"}


def test_parent_without_embedded_fields() -> None:
    t = get(issue(parent={"key": "DEMO-5"}), "parent,incomplete")
    assert t["parent"] == {"key": "DEMO-5", "type": "unknown", "title": None}
    assert t["incomplete"] == []


def test_no_parent_is_null() -> None:
    assert get(issue(parent=None), "parent")["parent"] is None


def test_a_parent_key_outside_the_grammar_is_dropped_and_named() -> None:
    t = get(issue(parent={"key": "../etc"}), "parent,incomplete")
    assert t["parent"] is None and t["incomplete"] == ["parent.invalid_id"]


def test_a_parent_of_the_wrong_type_is_null_and_named() -> None:
    t = get(issue(parent="DEMO-5"), "parent,incomplete")
    assert t["parent"] is None and t["incomplete"] == ["parent"]


def test_parent_title_is_cut() -> None:
    parent = {"key": "DEMO-5", "fields": {"summary": "p" * 6000, "issuetype": {"name": "Story"}}}
    t = get(issue(parent=parent), "parent,incomplete")
    assert len(t["parent"]["title"]) == 4096
    assert t["incomplete"] == ["parent.truncated"]


def _epic_parent(level: Any = 1) -> dict[str, Any]:
    return {
        "key": "DEMO-7",
        "fields": {"summary": "Big", "issuetype": {"name": "Epic", "hierarchyLevel": level}},
    }


def test_epic_is_the_parent_when_its_hierarchy_level_is_the_epic_level() -> None:
    t = get(issue(parent=_epic_parent()), "epic,parent")
    assert t["epic"] == {"key": "DEMO-7", "type": "Epic", "title": "Big"}
    assert t["parent"] == t["epic"]


def test_epic_is_null_for_a_non_epic_parent_and_for_a_subtask() -> None:
    assert get(issue(parent=_epic_parent(0)), "epic")["epic"] is None
    sub = issue(
        parent=_epic_parent(), issuetype={"name": "Sub-task", "subtask": True, "hierarchyLevel": -1}
    )
    assert get(sub, "epic")["epic"] is None


def test_epic_with_an_unknown_hierarchy_level_is_null_and_named() -> None:
    t = get(issue(parent=_epic_parent(None)), "epic,incomplete")
    assert t["epic"] is None and t["incomplete"] == ["epic"]
    t = get(issue(parent=_epic_parent("1")), "epic,incomplete")
    assert t["epic"] is None and t["incomplete"] == ["epic"]


def test_the_mapped_epic_field_fills_epic_when_the_parent_is_not_the_epic() -> None:
    t = get(issue(parent=None, customfield_90005="DEMO-8"), "epic")
    assert t["epic"] == {"key": "DEMO-8", "type": "Epic", "title": None}
    t = get(issue(parent=None, customfield_90005="../x"), "epic,incomplete")
    assert t["epic"] is None and t["incomplete"] == ["epic.invalid_id"]


def test_extra_fields_are_converted_by_schema_and_keyed_by_the_extra_name() -> None:
    extra = [
        custom("customfield_90001", "Business value", "number", "com.example:float"),
        custom("customfield_90006", "Notes", "string", "com.example:textfield"),
    ]
    cfg = Fields(
        extra={
            "value": fm(id="customfield_90001"),
            "notes": fm(id="customfield_90006"),
        }
    )
    sf = site_fields(cfg, extra)
    t = get(issue(customfield_90001=8, customfield_90006="n"), "fields", sf=sf)
    assert t["fields"] == {"notes": "n", "value": 8}
    assert not CUSTOM_ID.search(json.dumps(t))


def test_an_extra_field_that_does_not_fit_is_null_and_named_by_extra_name() -> None:
    extra = [custom("customfield_90001", "Business value", "number", "com.example:float")]
    sf = site_fields(Fields(extra={"value": fm(id="customfield_90001")}), extra)
    t = get(issue(customfield_90001="eight"), "fields,incomplete", sf=sf)
    assert t["fields"] == {"value": None}
    assert t["incomplete"] == ["fields.value"]


def test_an_extra_field_mapped_to_nothing_is_null_and_named() -> None:
    sf = site_fields(Fields(extra={"gone": fm(id="customfield_1")}))
    out = normalize_issue(issue(), ctx("fields,incomplete", sf=sf))
    assert out is not None
    assert out.ticket["fields"] == {"gone": None}
    assert out.ticket["incomplete"] == ["fields.gone"]
    assert "customfield_1" not in json.dumps(out.warnings)


def test_an_extra_field_missing_from_the_payload_is_named() -> None:
    extra = [custom("customfield_90001", "Business value", "number", "com.example:float")]
    sf = site_fields(Fields(extra={"value": fm(id="customfield_90001")}), extra)
    t = get(issue(), "fields,incomplete", sf=sf)
    assert t["fields"] == {"value": None} and t["incomplete"] == ["fields.value"]


def test_fields_is_an_empty_object_without_extras() -> None:
    assert get(issue(), "fields")["fields"] == {}


def test_extra_text_inherits_sanitizing_and_cut() -> None:
    extra = [custom("customfield_90001", "Notes", "string", "com.example:textfield")]
    sf = site_fields(Fields(extra={"notes": fm(id="customfield_90001")}), extra)
    t = get(issue(customfield_90001="a‮\n" + "q" * 5000), "fields,incomplete", sf=sf)
    assert "‮" not in t["fields"]["notes"] and "\n" not in t["fields"]["notes"]
    assert t["incomplete"] == ["fields.notes.truncated"]


# --- attachments ---------------------------------------------------------------------------


def test_attachments_are_metadata_only() -> None:
    raw = issue(
        attachment=[
            {
                "id": "5",
                "filename": "a.txt",
                "size": 12,
                "mimeType": "text/plain",
                "content": "https://x.example.test/a",
            }
        ]
    )
    t = get(raw, "attachments")
    assert t["attachments"] == [{"id": "5", "name": "a.txt", "size": 12, "mime": "text/plain"}]


def test_attachment_names_are_untrusted_and_cut() -> None:
    raw = issue(
        attachment=[{"id": "5", "filename": "a‮\n" + "n" * 2000, "size": 1, "mimeType": "x/y"}]
    )
    t = get(raw, "attachments,incomplete")
    assert len(t["attachments"][0]["name"]) == 1024
    assert "‮" not in t["attachments"][0]["name"]
    assert t["incomplete"] == ["attachments.truncated"]


@pytest.mark.parametrize(
    ("entry", "part"),
    [
        ({"id": "a b", "filename": "n", "size": 1, "mimeType": "x/y"}, "attachments.invalid_id"),
        ({"id": 5, "filename": "n", "size": 1, "mimeType": "x/y"}, "attachments.invalid_id"),
        ({"id": "5", "filename": "n", "size": -1, "mimeType": "x/y"}, "attachments"),
        ({"id": "5", "filename": "n", "size": "1", "mimeType": "x/y"}, "attachments"),
        ({"id": "5", "filename": "n", "size": True, "mimeType": "x/y"}, "attachments"),
        ({"id": "5", "filename": None, "size": 1, "mimeType": "x/y"}, "attachments"),
        ({"id": "5", "filename": "n", "size": 1, "mimeType": None}, "attachments"),
        ("junk", "attachments"),
    ],
)
def test_a_bad_attachment_is_dropped_and_named(entry: Any, part: str) -> None:
    t = get(issue(attachment=[entry]), "attachments,incomplete")
    assert t["attachments"] == [] and t["incomplete"] == [part]


def test_attachments_not_a_list_or_absent_is_unknown() -> None:
    t = get(issue(attachment="x"), "attachments,incomplete")
    assert t["attachments"] == [] and t["incomplete"] == ["attachments"]
    t = get(issue(), "attachments,incomplete")
    assert t["attachments"] == [] and t["incomplete"] == ["attachments"]


def test_too_many_attachments_are_cut_and_named() -> None:
    many = [{"id": str(i), "filename": "n", "size": 1, "mimeType": "x/y"} for i in range(1000)]
    t = get(issue(attachment=many), "attachments,incomplete")
    assert len(t["attachments"]) == 500 and t["incomplete"] == ["attachments"]


# --- links and blocked ---------------------------------------------------------------------


def test_links_map_direction_and_fields() -> None:
    raw = issue(
        issuelinks=[
            link("Blocks", "DEMO-2", inward=True, cat="done", summary="Two"),
            link("Relates", "DEMO-3", inward=False, cat=None, summary=None),
        ]
    )
    t = get(raw, "links")
    assert t["links"] == [
        {
            "type": "Blocks",
            "direction": "inward",
            "key": "DEMO-2",
            "status_category": "done",
            "summary": "Two",
        },
        {
            "type": "Relates",
            "direction": "outward",
            "key": "DEMO-3",
            "status_category": None,
            "summary": None,
        },
    ]


def test_link_summary_is_untrusted_and_cut() -> None:
    raw = issue(issuelinks=[link("Blocks", "DEMO-2", inward=True, summary="s‮" + "z" * 5000)])
    t = get(raw, "links,incomplete")
    assert "‮" not in t["links"][0]["summary"] and len(t["links"][0]["summary"]) == 4096
    assert t["incomplete"] == ["links.truncated"]


@pytest.mark.parametrize("cat", ["weird", 4, ""])
def test_an_unknown_link_category_is_null(cat: Any) -> None:
    raw = issue(issuelinks=[link("Blocks", "DEMO-2", inward=True, cat=None)])
    raw["fields"]["issuelinks"][0]["inwardIssue"]["fields"]["status"] = {
        "statusCategory": {"key": cat}
    }
    assert get(raw, "links")["links"][0]["status_category"] is None


def test_blocked_true_for_an_inward_blocks_link_with_a_not_done_other_end() -> None:
    raw = issue(issuelinks=[link("Blocks", "DEMO-2", inward=True, cat="indeterminate")])
    assert get(raw, "blocked,incomplete") == {
        "key": "DEMO-1",
        "site": SITE,
        "blocked": {"value": True, "by": ["DEMO-2"]},
        "incomplete": [],
    }


def test_blocked_false_when_the_blocker_is_done() -> None:
    raw = issue(issuelinks=[link("Blocks", "DEMO-2", inward=True, cat="done")])
    assert get(raw, "blocked")["blocked"] == {"value": False, "by": []}


def test_blocked_false_with_no_links_at_all() -> None:
    assert get(issue(), "blocked")["blocked"] == {"value": False, "by": []}


def test_blocked_ignores_outward_links_and_other_types() -> None:
    raw = issue(
        issuelinks=[
            link("Blocks", "DEMO-2", inward=False, cat="new"),
            link("Relates", "DEMO-3", inward=True, cat="new"),
        ]
    )
    assert get(raw, "blocked")["blocked"] == {"value": False, "by": []}


def test_blocking_types_match_by_name_case_insensitively_and_are_configurable() -> None:
    raw = issue(issuelinks=[link("DEPENDS ON", "DEMO-2", inward=True, cat="new")])
    assert get(raw, "blocked")["blocked"]["value"] is False
    out = normalize_issue(raw, ctx("blocked", blocking=("Depends On", "Blocks")))
    assert out is not None and out.ticket["blocked"] == {"value": True, "by": ["DEMO-2"]}
    raw = issue(issuelinks=[link("blocks", "DEMO-2", inward=True, cat="new")])
    assert get(raw, "blocked")["blocked"]["value"] is True


def test_a_null_category_counts_as_not_done_and_adds_links_unreadable() -> None:
    raw = issue(issuelinks=[link("Blocks", "DEMO-2", inward=True, cat=None)])
    t = get(raw, "blocked,incomplete")
    assert t["blocked"] == {"value": True, "by": ["DEMO-2"]}
    assert t["incomplete"] == ["links.unreadable"]


def test_blocked_by_lists_unique_keys_in_order() -> None:
    raw = issue(
        issuelinks=[
            link("Blocks", "DEMO-3", inward=True, cat="new"),
            link("Blocks", "DEMO-2", inward=True, cat="new"),
            link("Blocks", "DEMO-3", inward=True, cat="new"),
            link("Blocks", "DEMO-4", inward=True, cat="done"),
        ]
    )
    assert get(raw, "blocked")["blocked"] == {"value": True, "by": ["DEMO-3", "DEMO-2"]}


def test_blocked_is_null_when_links_are_missing() -> None:
    raw = issue()
    del raw["fields"]["issuelinks"]
    t = get(raw, "blocked,incomplete")
    assert t["blocked"] == {"value": None, "by": []}
    assert t["incomplete"] == ["links"]
    raw["fields"]["issuelinks"] = None
    assert get(raw, "blocked")["blocked"]["value"] is None
    raw["fields"]["issuelinks"] = "x"
    assert get(raw, "blocked")["blocked"]["value"] is None


def test_blocked_is_null_when_the_list_was_cut_and_nothing_qualifies() -> None:
    many = [link("Relates", f"DEMO-{i + 10}", inward=False) for i in range(MAX_LINKS + 5)]
    t = get(issue(issuelinks=many), "blocked,links,incomplete")
    assert len(t["links"]) == MAX_LINKS
    assert t["blocked"] == {"value": None, "by": []}
    assert t["incomplete"] == ["links"]


def test_blocked_true_wins_over_a_cut_list() -> None:
    many = [link("Blocks", "DEMO-2", inward=True, cat="new")]
    many += [link("Relates", f"DEMO-{i + 10}", inward=False) for i in range(MAX_LINKS + 5)]
    t = get(issue(issuelinks=many), "blocked,incomplete")
    assert t["blocked"] == {"value": True, "by": ["DEMO-2"]}
    assert t["incomplete"] == ["links"]


def test_an_unreadable_link_entry_makes_blocked_unknown_but_true_still_wins() -> None:
    bad = [
        {"type": {"name": "Blocks"}},
        "junk",
        None,
        {"type": 5, "inwardIssue": {"key": "DEMO-2"}},
    ]
    t = get(issue(issuelinks=bad), "blocked,links,incomplete")
    assert t["links"] == []
    assert t["blocked"] == {"value": None, "by": []}
    assert t["incomplete"] == ["links"]
    good = bad + [link("Blocks", "DEMO-2", inward=True, cat="new")]
    assert get(issue(issuelinks=good), "blocked")["blocked"] == {"value": True, "by": ["DEMO-2"]}


def test_a_link_key_outside_the_grammar_is_dropped_and_named() -> None:
    raw = issue(issuelinks=[link("Blocks", "x/../y", inward=True, cat="new")])
    t = get(raw, "blocked,links,incomplete")
    assert t["links"] == []
    assert t["blocked"]["value"] is None
    assert t["incomplete"] == ["links.invalid_id"]


def test_a_link_with_both_sides_is_unreadable() -> None:
    entry = link("Blocks", "DEMO-2", inward=True)
    entry["outwardIssue"] = {"key": "DEMO-3", "fields": {}}
    t = get(issue(issuelinks=[entry]), "links,incomplete")
    assert t["links"] == [] and t["incomplete"] == ["links"]


def test_blocked_computes_without_printing_links_when_only_blocked_is_requested() -> None:
    raw = issue(issuelinks=[link("Blocks", "DEMO-2", inward=True, cat="new")])
    t = get(raw, "blocked")
    assert "links" not in t


# --- incomplete ----------------------------------------------------------------------------


def test_incomplete_is_printed_empty_when_requested() -> None:
    assert get(issue(), "title,incomplete")["incomplete"] == []


def test_incomplete_is_printed_when_not_empty_even_if_not_requested() -> None:
    t = get(issue(labels="x"), "labels")
    assert t["incomplete"] == ["labels"]


def test_incomplete_is_not_printed_when_empty_and_not_requested() -> None:
    assert "incomplete" not in get(issue(), "title")


def test_incomplete_is_unique_sorted_and_in_its_grammar() -> None:
    raw = issue(labels=[5, 6, "x" * 300, "y" * 300], assignee=4, priority=3)
    t = get(raw, "labels,assignee,priority,incomplete")
    assert t["incomplete"] == sorted(set(t["incomplete"]))
    assert all(re.fullmatch(r"[a-z][a-z0-9_.]{0,63}", p) for p in t["incomplete"])


# --- whole-ticket failures -----------------------------------------------------------------


@pytest.mark.parametrize(
    "raw",
    [
        None,
        [],
        "x",
        5,
        {"key": "DEMO-1"},
        {"key": "DEMO-1", "fields": None},
        {"key": "DEMO-1", "fields": []},
    ],
)
def test_a_payload_that_is_not_an_issue_is_a_stub_or_nothing(raw: Any) -> None:
    out = normalize_issue(raw, ctx("title,status"))
    if isinstance(raw, dict) and raw.get("key") == "DEMO-1":
        assert out is not None
        assert out.ticket == {"key": "DEMO-1", "site": SITE, "incomplete": ["ticket"]}
    else:
        assert out is None


def test_a_property_that_raises_becomes_incomplete_not_an_abort(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import ticketctl.normalize as mod

    def boom(*_a: Any, **_k: Any) -> Any:
        raise RuntimeError("boom")

    monkeypatch.setattr(mod, "_prop_title", boom)
    t = get(issue(), "title,status,incomplete")
    assert t["incomplete"] == ["title"]
    assert "title" not in t
    assert t["status"]["name"] == "In Progress"


def test_a_page_keeps_good_tickets_when_one_is_a_stub_and_skips_the_unusable() -> None:
    raws = [issue("DEMO-1"), {"key": "DEMO-2", "fields": 5}, "junk", issue("DEMO-3")]
    page = normalize_issues(raws, ctx("title"))
    assert [t["key"] for t in page.tickets] == ["DEMO-1", "DEMO-2", "DEMO-3"]
    assert page.tickets[1]["incomplete"] == ["ticket"]
    assert page.skipped == 1


def test_a_page_dedupes_warnings() -> None:
    twin = custom("customfield_90001", "Story Points", "number", "com.example:jsw-story-points")
    c = ctx("estimate", sf=site_fields(extra_fields=[twin]))
    page = normalize_issues([issue("DEMO-1"), issue("DEMO-2")], c)
    assert len(page.warnings) == 1


def test_the_payload_is_not_mutated() -> None:
    raw = issue(
        description=adf("x"), labels=["a"], issuelinks=[link("Blocks", "DEMO-2", inward=True)]
    )
    before = copy.deepcopy(raw)
    normalize_issue(raw, ctx(None))
    assert raw == before


# --- no leakage ----------------------------------------------------------------------------


def test_no_customfield_id_in_any_output_for_a_default_read() -> None:
    raw = issue(
        customfield_90002=3,
        customfield_90003=[_sprint(1)],
        customfield_90004=adf("c"),
        customfield_90005="DEMO-8",
        customfield_1="filler",
        description=adf("d"),
    )
    out = normalize_issue(raw, ctx(None))
    assert out is not None
    assert not CUSTOM_ID.search(json.dumps([out.ticket, out.warnings]))


def test_unrequested_properties_are_not_printed() -> None:
    t = get(issue(description=adf("x"), customfield_90002=3), "title")
    assert set(t) == {"key", "site", "title"}


def test_the_default_search_prints_only_the_search_properties() -> None:
    out = normalize_issue(issue(description=adf("x")), ctx(None, "search"))
    assert out is not None
    assert set(out.ticket) <= {"key", "site", "incomplete", *SEARCH_DEFAULT}
    assert "description" not in out.ticket


# --- against the fake provider -------------------------------------------------------------


class TestAgainstTheFake:
    @staticmethod
    def _site_fields(fake: FakeProvider, gw: Any) -> SiteFields:
        status_, data = fake.json("GET", "/rest/api/3/field")
        assert status_ == 200
        return SiteFields(
            parse_field_list(data).fields,
            Fields(acceptance_criteria=fm(name="Acceptance Criteria")),
        )

    def test_get_normalizes_a_seeded_ticket(self, fake: FakeProvider) -> None:
        gw = make_gateway(fake)
        sf = self._site_fields(fake, gw)
        c = Context(
            site=SITE,
            base_url=fake.url,
            props=parse_properties(None, command="get"),
            site_fields=sf,
            priorities=NameList(("Highest", "High", "Medium", "Low"), "provider"),
            blocking_link_types=("Blocks",),
        )
        req = plan_request(c.props, sf)
        assert req.fields and "*all" not in req.fields
        resp = gw.request(
            "GET",
            "/rest/api/3/issue/DEMO-2",
            query=query({"fields": ",".join(req.fields)}),
            template="/rest/api/3/issue/{key}",
        )
        out = normalize_issue(resp.data, c)
        assert out is not None
        t = out.ticket
        assert t["key"] == "DEMO-2"
        assert t["status"] == {"name": "In Progress", "category": "indeterminate"}
        assert t["priority"] == {"name": "Medium", "rank": 2}
        assert t["assignee"]["display_name"] == "Fake Me"
        assert t["created"].endswith("Z") and len(t["created"]) == 20
        assert t["sprints"][0]["state"] == "active"
        assert t["estimate"] == 3.0 or t["estimate"] is None
        assert not CUSTOM_ID.search(json.dumps(t))
        assert F_POINTS not in json.dumps(t) and F_ESTIMATE not in json.dumps(t)
        assert F_SPRINT not in json.dumps(t) and F_CRITERIA not in json.dumps(t)

    def test_the_default_search_request_does_not_fetch_everything(self, fake: FakeProvider) -> None:
        gw = make_gateway(fake)
        sf = self._site_fields(fake, gw)
        req = plan_request(parse_properties(None, command="search"), sf)
        resp = gw.request(
            "GET",
            "/rest/api/3/issue/DEMO-1",
            query=query({"fields": ",".join(req.fields)}),
            template="/rest/api/3/issue/{key}",
        )
        sent = set(resp.data["fields"])
        assert "description" not in sent and "comment" not in sent and "issuelinks" not in sent
        assert sent <= set(req.fields)  # no filler: only what was asked for


# --- fuzz ----------------------------------------------------------------------------------


def _junk(rng: random.Random, depth: int = 0) -> Any:
    kinds = ["none", "bool", "int", "big", "float", "nan", "str", "ctl", "list", "dict"]
    if depth > 3:
        kinds = kinds[:8]
    kind = rng.choice(kinds)
    if kind == "none":
        return None
    if kind == "bool":
        return rng.random() < 0.5
    if kind == "int":
        return rng.randrange(-5, 10)
    if kind == "big":
        return 10 ** rng.randrange(19, 400)
    if kind == "float":
        return rng.random() * 1e300
    if kind == "nan":
        return rng.choice([float("nan"), float("inf"), -float("inf")])
    if kind == "str":
        return rng.choice(
            [
                "",
                "DEMO-2",
                "Blocks",
                "done",
                "new",
                "2026-03-04T10:00:00-0600",
                "tc-0123456789abcdef",
                "x" * rng.randrange(0, 6000),
            ]
        )
    if kind == "ctl":
        return "".join(rng.choice("a‮\x00\n\r\t​<U+0041>") for _ in range(rng.randrange(1, 30)))
    if kind == "list":
        return [_junk(rng, depth + 1) for _ in range(rng.randrange(0, 5))]
    return {
        rng.choice(
            [
                "key",
                "name",
                "id",
                "type",
                "fields",
                "summary",
                "status",
                "statusCategory",
                "inwardIssue",
                "outwardIssue",
                "accountId",
                "displayName",
                "hierarchyLevel",
                "subtask",
                "value",
            ]
        ): _junk(rng, depth + 1)
        for _ in range(rng.randrange(0, 5))
    }


def test_fuzz_hostile_payloads_never_raise_and_keep_their_shape() -> None:
    rng = random.Random(1313)
    extra = [custom("customfield_90001", "Value", "number", "com.example:float")]
    sf = site_fields(
        Fields(
            extra={"value": fm(id="customfield_90001")},
            acceptance_criteria=fm(id="customfield_90004"),
        ),
        extra,
    )
    c = Context(
        site=SITE,
        base_url=BASE,
        props=parse_properties(None, command="get", comments=1, include_adf=True),
        site_fields=sf,
        priorities=NameList(("A", "B"), "cache"),
        blocking_link_types=("Blocks",),
    )
    names = [
        "summary",
        "issuetype",
        "status",
        "resolution",
        "assignee",
        "reporter",
        "priority",
        "labels",
        "created",
        "updated",
        "resolutiondate",
        "duedate",
        "parent",
        "issuelinks",
        "attachment",
        "description",
        "customfield_90001",
        "customfield_90002",
        "customfield_90003",
        "customfield_90004",
        "customfield_90005",
    ]
    deadline = time.monotonic() + 5.0
    runs = 0
    while time.monotonic() < deadline:
        fields = {n: _junk(rng) for n in names if rng.random() < 0.8}
        raw = {
            "id": _junk(rng) if rng.random() < 0.3 else "1",
            "key": rng.choice(["DEMO-1", "DEMO-1", _junk(rng)]),
            "fields": fields,
        }
        out = normalize_issue(raw, c)
        runs += 1
        if out is None:
            continue
        text = json.dumps([out.ticket, out.warnings], allow_nan=False)
        assert not CUSTOM_ID.search(text)
        assert "‮" not in text and "\\u0000" not in text
        assert re.fullmatch(r"[A-Z][A-Z0-9_]*-[0-9]+", out.ticket["key"])
        for part in out.ticket.get("incomplete", []):
            assert re.fullmatch(r"[a-z][a-z0-9_.]{0,63}", part), part
        blocked = out.ticket.get("blocked")
        if blocked is not None:
            assert blocked["value"] in (True, False, None)
            assert (blocked["value"] is not True) or blocked["by"]
        for label in out.ticket.get("labels", []):
            assert not re.fullmatch(r"tc-[0-9a-f]{16}", label)
    assert runs > 50


# --- review round 1 ------------------------------------------------------------------------

ALL_NAMES = ",".join(p for p in PROPERTIES if p != "comments")


def _full_issue() -> dict[str, Any]:
    user = {"accountId": "acct-1", "displayName": "Fake One"}
    return issue(
        description=adf("Hello"),
        resolution=None,
        assignee=user,
        reporter=user,
        priority={"name": "High"},
        labels=["a", "b"],
        parent=_epic_parent(),
        issuelinks=[link("Blocks", "DEMO-2", inward=True, cat="new")],
        attachment=[{"id": "5", "filename": "a.txt", "size": 12, "mimeType": "text/plain"}],
        created="2026-03-02T09:00:00.000-0600",
        updated="2026-03-02T09:00:00.000+0000",
        resolutiondate=None,
        duedate="2026-04-01",
        customfield_90002=3,
        customfield_90003=[_sprint(1)],
        customfield_90004=adf("c"),
        customfield_90006="note",
    )


def _full_ctx() -> Context:
    extra = [custom("customfield_90006", "Notes", "string", "com.example:textfield")]
    sf = site_fields(
        Fields(
            acceptance_criteria=fm(id="customfield_90004"),
            extra={"notes": fm(id="customfield_90006")},
        ),
        extra,
    )
    names = NameList(("Low", "High"), "cache")
    return ctx(ALL_NAMES, sf=sf, priorities=names, include_adf=True)


def _ticket_validator() -> Any:
    from jsonschema import Draft202012Validator

    from tests.golden import format_checker, registry

    return Draft202012Validator(
        {"$ref": "ticketctl/defs.json#/$defs/ticket"},
        registry=registry(),
        format_checker=format_checker(),
    )


def _assert_valid(ticket: dict[str, Any]) -> None:
    errors = [e.message[:200] for e in _ticket_validator().iter_errors(ticket)]
    assert errors == []


def test_normalized_output_validates_against_the_ticket_schema() -> None:
    c = _full_ctx()
    out = normalize_issue(_full_issue(), c)
    assert out is not None
    _assert_valid(out.ticket)
    assert out.ticket["description_adf"]["type"] == "doc"
    for names in ("title", "key", "incomplete", "url,id,type,status"):
        got = normalize_issue(_full_issue(), ctx(names))
        assert got is not None
        _assert_valid(got.ticket)
    stub = normalize_issue({"key": "DEMO-1", "fields": 5}, c, requested_key="DEMO-2")
    assert stub is not None
    _assert_valid(stub.ticket)
    assert get(issue(description=None), "description_adf", include_adf=True) == {
        "key": "DEMO-1",
        "site": SITE,
        "description_adf": None,
    }
    _assert_valid(get(issue(description=None), "description_adf", include_adf=True))


_JUNK: list[Any] = [
    None,
    5,
    -1,
    2**80,
    1.5,
    float("nan"),
    True,
    "",
    "x" * 5000,
    "a‮b\x00\n",
    [],
    [None, 5, "x", {}],
    {},
    {"name": 5},
    {"key": "demo", "fields": 3},
    {"accountId": "a b", "displayName": 4},
    {"type": {"name": 1}, "inwardIssue": 4},
]


def test_hostile_payloads_always_validate_against_the_ticket_schema() -> None:
    rng = random.Random(1307)
    c = _full_ctx()
    base = _full_issue()
    names = list(base["fields"])
    for _ in range(300):
        raw = copy.deepcopy(base)
        for name in rng.sample(names, rng.randint(1, 6)):
            raw["fields"][name] = copy.deepcopy(rng.choice(_JUNK))
        if rng.random() < 0.2:
            raw["id"] = rng.choice(_JUNK)
        out = normalize_issue(raw, c)
        assert out is not None
        _assert_valid(out.ticket)


def test_an_unreadable_title_is_omitted_even_when_the_builder_raises(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import ticketctl.normalize as mod

    def boom(*_a: Any, **_k: Any) -> Any:
        raise RuntimeError("boom")

    monkeypatch.setattr(mod, "_prop_title", boom)
    monkeypatch.setattr(mod, "_clean_json", boom)
    t = get(issue(description=adf("x")), "title,description_adf,incomplete", include_adf=True)
    assert "title" not in t and "description_adf" not in t
    assert t["incomplete"] == ["description_adf", "title"]


def test_a_description_adf_that_cannot_be_copied_is_omitted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import ticketctl.normalize as mod

    monkeypatch.setattr(mod, "ADF_NODE_LIMIT", 5)
    doc = {"type": "doc", "content": [{"type": "text", "text": str(i)} for i in range(20)]}
    t = get(issue(description=doc), "description_adf,incomplete", include_adf=True)
    assert "description_adf" not in t
    assert t["incomplete"] == ["description_adf.truncated"]


# --- accounts ---


def test_an_account_with_no_usable_id_is_the_unknown_user() -> None:
    t = get(issue(assignee={"displayName": "Named"}), "assignee,incomplete")
    assert t["assignee"] == {"account_id": None, "display_name": "unknown user"}
    assert t["incomplete"] == []


def test_an_account_id_outside_the_grammar_is_named_and_shown_as_unknown() -> None:
    raw = issue(
        assignee={"accountId": "a b/../", "displayName": "N"},
        reporter={"accountId": 5, "displayName": "M"},
    )
    t = get(raw, "assignee,reporter,incomplete")
    assert t["assignee"] == {"account_id": None, "display_name": "unknown user"}
    assert t["reporter"] == {"account_id": None, "display_name": "unknown user"}
    assert t["incomplete"] == ["assignee.invalid_id", "reporter.invalid_id"]


# --- stubs ---


def test_a_non_dict_payload_with_a_requested_key_is_a_stub() -> None:
    for raw in (None, [], "x", 5):
        out = normalize_issue(raw, ctx("title"), requested_key="DEMO-4")
        assert out is not None
        assert out.ticket == {"key": "DEMO-4", "site": SITE, "incomplete": ["ticket"]}
        assert normalize_issue(raw, ctx("title")) is None


def test_a_stub_carries_requested_key_when_the_canonical_key_differs() -> None:
    out = normalize_issue({"key": "DEMO-9", "fields": None}, ctx("title"), requested_key="DEMO-4")
    assert out is not None
    assert out.ticket == {
        "key": "DEMO-9",
        "site": SITE,
        "requested_key": "DEMO-4",
        "incomplete": ["ticket"],
    }
    same = normalize_issue({"key": "DEMO-9", "fields": None}, ctx("title"), requested_key="DEMO-9")
    assert same is not None and "requested_key" not in same.ticket


# --- the opaque ADF copy ---


def _adf_copy(doc: Any) -> dict[str, Any]:
    return get(issue(description=doc), "description_adf,incomplete", include_adf=True)


def test_description_adf_keeps_the_key_order() -> None:
    doc = {"type": "doc", "zeta": 1, "alpha": [{"b": 1, "a": 2, "c": 3}], "mid": "m"}
    t = _adf_copy(doc)
    assert list(t["description_adf"]) == ["type", "zeta", "alpha", "mid"]
    assert list(t["description_adf"]["alpha"][0]) == ["b", "a", "c"]


def test_a_key_collision_after_sanitizing_is_named() -> None:
    t = _adf_copy({"type": "doc", "k‮": 1, "k<U+202E>": 2})
    assert t["incomplete"] == ["description_adf"]
    assert len(t["description_adf"]) == 2


def test_description_adf_strings_are_cut_at_the_body_cap_and_named() -> None:
    t = _adf_copy({"type": "doc", "text": "y" * 1_050_000})
    assert len(t["description_adf"]["text"]) <= 1_000_000
    assert t["incomplete"] == ["description_adf.truncated"]


def test_description_adf_keeps_line_breaks_and_scrubs_secrets() -> None:
    from ticketctl import sanitize

    secret = "s3cr3t-token-value"
    sanitize.configure_scrubber([secret])
    try:
        t = _adf_copy({"type": "doc", "text": f"a\nb {secret}", f"k{secret}": 1})
        assert t["description_adf"]["text"] == "a\nb <redacted>"
        assert "<redacted>" in "".join(t["description_adf"])
        t = get(issue(description=adf(f"x {secret}")), "description")
        assert t["description"] == "x <redacted>"
    finally:
        sanitize.configure_scrubber([])


def test_description_adf_bounds_huge_integers() -> None:
    t = _adf_copy({"type": "doc", "a": 2**70, "b": -(2**70), "c": 2**63, "d": True})
    node = t["description_adf"]
    assert node["a"] is None and node["b"] is None
    assert node["c"] == 2**63 and node["d"] is True
    assert t["incomplete"] == ["description_adf"]


def test_description_adf_node_budget_stops_cyclic_wide_and_shared_inputs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import ticketctl.normalize as mod

    monkeypatch.setattr(mod, "ADF_NODE_LIMIT", 1000)
    cyclic: dict[str, Any] = {"type": "doc"}
    cyclic["self"] = cyclic
    wide = {"type": "doc", "content": list(range(5000))}
    shared: Any = {"type": "text"}
    for _ in range(40):
        shared = {"type": "doc", "content": [shared, shared]}
    for doc in (cyclic, wide, shared):
        t = _adf_copy(doc)
        assert "description_adf" not in t
        assert t["incomplete"] == ["description_adf.truncated"]
    monkeypatch.setattr(mod, "ADF_NODE_LIMIT", 6)
    assert "description_adf" in _adf_copy({"type": "doc", "content": [1, 2]})
    assert "description_adf" not in _adf_copy({"type": "doc", "content": [1, 2, 3, 4, 5]})


# --- names that are cut ---


def test_a_cut_name_is_named_truncated() -> None:
    long = "n" * 400
    c = ctx("type,status,resolution,priority,parent,links,incomplete")
    raw = issue(
        issuetype={"name": long, "subtask": False},
        status=status(long),
        resolution={"name": long},
        priority={"name": long},
        parent={"key": "DEMO-7", "fields": {"issuetype": {"name": long}}},
        issuelinks=[link(long, "DEMO-2", inward=False)],
    )
    t = one(raw, c)
    assert t["incomplete"] == [
        "links.truncated",
        "parent.truncated",
        "priority.truncated",
        "resolution.truncated",
        "status.truncated",
        "type.truncated",
    ]


# --- extra names ---


def test_incomplete_parts_of_the_longest_extra_name_fit_the_schema_pattern() -> None:
    from ticketctl.config import EXTRA_NAME

    name = "x" * 47
    assert re.fullmatch(EXTRA_NAME, name) and not re.fullmatch(EXTRA_NAME, name + "x")
    extra = [custom("customfield_90001", "Notes", "string", "com.example:textfield")]
    sf = site_fields(Fields(extra={name: fm(id="customfield_90001")}), extra)
    t = get(issue(customfield_90001="q" * 5000), "fields,incomplete", sf=sf)
    assert t["incomplete"] == [f"fields.{name}.truncated"]
    assert all(re.fullmatch(r"[a-z][a-z0-9_.]{0,63}", p) for p in t["incomplete"])


# --- warnings name the logical field ---


def test_the_sprint_warning_names_the_logical_sprint_field() -> None:
    twin = custom("customfield_90001", "Sprint", "array", "com.example:gh-sprint", "json")
    out = normalize_issue(issue(), ctx("sprints,incomplete", sf=site_fields(extra_fields=[twin])))
    assert out is not None
    assert out.ticket["incomplete"] == ["sprints"]
    (warning,) = out.warnings
    assert re.search(rf"sites\.{SITE}\.fields\.sprint\b(?!s)", warning)
    assert not re.search(r"sprints", warning)


def test_the_sprint_not_mapped_is_named_only_when_asked_for_as_sprints() -> None:
    sf = SiteFields(system_fields(), Fields())
    assert get(issue(), "sprints,incomplete", sf=sf)["incomplete"] == ["sprints"]


# --- links cache and dispatch ---


def test_a_bad_blocking_link_types_context_makes_blocked_unknown() -> None:
    raw = issue(issuelinks=[link("Blocks", "DEMO-2", inward=True, cat="new")])
    for names in ("blocked,incomplete", "links,blocked,incomplete"):
        t = get(raw, names, blocking=(5,))  # type: ignore[arg-type]
        assert t["blocked"] == {"value": None, "by": []}
        assert "blocked" in t["incomplete"] or "links" in t["incomplete"]


def test_an_unknown_property_name_never_raises_out_of_normalize() -> None:
    from ticketctl.normalize import Properties

    c = Context(
        site=SITE,
        base_url=BASE,
        props=Properties(("title", "nope", "__class__", "status")),
        site_fields=site_fields(),
    )
    out = normalize_issue(issue(), c)
    assert out is not None
    assert out.ticket["title"] == "A title" and out.ticket["status"]["name"] == "In Progress"
    assert set(out.ticket) == {"key", "site", "title", "status"}
    assert normalize_issues([issue(), issue("DEMO-2")], c).skipped == 0


# --- pinned limits and sanitizing (mutation guards) ---


def test_max_links_is_five_hundred() -> None:
    assert MAX_LINKS == 500
    many = [link("Relates", f"DEMO-{i + 10}", inward=False) for i in range(501)]
    t = get(issue(issuelinks=many), "links,incomplete")
    assert len(t["links"]) == 500 and t["incomplete"] == ["links"]
    t = get(issue(issuelinks=many[:500]), "links,incomplete")
    assert len(t["links"]) == 500 and t["incomplete"] == []


def test_link_type_status_name_and_attachment_mime_are_sanitized() -> None:
    raw = issue(
        status=status("a‮b\n", "done"),
        issuelinks=[link("T‮x\n", "DEMO-2", inward=False)],
        attachment=[{"id": "5", "filename": "n", "size": 1, "mimeType": "x‮/y\n"}],
    )
    t = get(raw, "status,links,attachments")
    assert t["status"]["name"] == "a<U+202E>b "
    assert t["links"][0]["type"] == "T<U+202E>x "
    assert t["attachments"][0]["mime"] == "x<U+202E>/y "


def test_a_boolean_hierarchy_level_is_not_the_epic_level() -> None:
    t = get(issue(parent=_epic_parent(True)), "epic,incomplete")
    assert t["epic"] is None and t["incomplete"] == ["epic"]
