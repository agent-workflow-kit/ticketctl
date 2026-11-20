"""Field list parsing, logical mapping, discovery by schema and the mapping problems (issue #12).

Pure logic: the field lists here are synthetic; the loader and the cache are in test_fieldload.py.
"""

from __future__ import annotations

from typing import Any

import pytest

from tests.fake_provider import F_CRITERIA, F_EFFORT, F_ESTIMATE, F_POINTS, F_SPRINT, F_VALUE
from ticketctl import fieldmap
from ticketctl.config import FieldMapping, Fields
from ticketctl.errors import TicketctlError
from ticketctl.fieldmap import ProviderField, Status

SYSTEM_IDS = {"summary", "labels", "assignee", "priority", "resolution", "parent", "duedate"}
NS = "com.atlassian.jira.plugin.system.customfieldtypes"


def entry(
    fid: str,
    name: str,
    type_: str = "string",
    *,
    custom: str | None = None,
    items: str | None = None,
    is_custom: bool | None = None,
) -> dict[str, Any]:
    schema: dict[str, Any] = {"type": type_}
    if items is not None:
        schema["items"] = items
    if custom is not None:
        schema["custom"] = custom
    return {
        "id": fid,
        "name": name,
        "custom": fid not in SYSTEM_IDS if is_custom is None else is_custom,
        "schema": schema,
    }


def system_entries() -> list[dict[str, Any]]:
    return [
        entry("summary", "Summary"),
        entry("labels", "Labels", "array", items="string"),
        entry("assignee", "Assignee", "user"),
        entry("priority", "Priority", "priority"),
        entry("resolution", "Resolution", "resolution"),
        entry("parent", "Parent", "issuelink"),
        entry("duedate", "Due date", "date"),
    ]


def site_entries() -> list[dict[str, Any]]:
    """The shape of the fake provider's list: two points fields, sprint, criteria, two floats."""
    return [
        *system_entries(),
        entry(F_POINTS, "Story Points", "number", custom=f"{NS}:float"),
        entry(F_ESTIMATE, "Story point estimate", "number", custom=f"{NS}:jsw-story-points"),
        entry(
            F_SPRINT,
            "Sprint",
            "array",
            items="json",
            custom="com.pyxis.greenhopper.jira:gh-sprint",
        ),
        entry(F_CRITERIA, "Acceptance Criteria", "string", custom=f"{NS}:textarea"),
        entry(F_VALUE, "Business value", "number", custom=f"{NS}:float"),
        entry(F_EFFORT, "Effort days", "number", custom=f"{NS}:float"),
    ]


def parsed(entries: list[dict[str, Any]]) -> tuple[ProviderField, ...]:
    return fieldmap.parse_field_list(entries).fields


def mapping(**kw: str) -> FieldMapping:
    return FieldMapping(**kw)


def ids(res: fieldmap.Resolution) -> list[str]:
    return sorted(f.id for f in res.fields)


# --- parsing -----------------------------------------------------------------------------------


def test_parse_keeps_the_documented_shape() -> None:
    result = fieldmap.parse_field_list(site_entries())
    assert result.ignored == 0
    by_id = {f.id: f for f in result.fields}
    points = by_id[F_POINTS]
    assert points.name == "Story Points"
    assert points.custom is True
    assert points.schema == fieldmap.FieldSchema("number", None, f"{NS}:float")
    assert by_id["summary"].custom is False


@pytest.mark.parametrize("bad", [None, {}, "x", 5, {"fields": []}])
def test_a_list_that_is_not_a_list_is_refused(bad: object) -> None:
    with pytest.raises(fieldmap.FieldListError):
        fieldmap.parse_field_list(bad)


def test_an_over_long_list_is_refused() -> None:
    entries = [entry(f"customfield_{n}", f"F{n}") for n in range(fieldmap.MAX_FIELDS + 1)]
    with pytest.raises(fieldmap.FieldListError):
        fieldmap.parse_field_list(entries)


def test_a_list_at_the_limit_is_accepted() -> None:
    entries = [entry(f"customfield_{n}", f"F{n}") for n in range(fieldmap.MAX_FIELDS)]
    assert len(fieldmap.parse_field_list(entries).fields) == fieldmap.MAX_FIELDS


@pytest.mark.parametrize(
    "bad",
    [
        None,
        "customfield_1",
        [],
        {"name": "No id"},
        {"id": "customfield_1"},  # no name
        {"id": 7, "name": "Numeric id"},
        {"id": "customfield_1", "name": 5},
        {"id": "", "name": "Empty id"},
        {"id": "a b", "name": "Space in id"},
        {"id": "../x", "name": "Path-shaped id"},
        {"id": "x" * 129, "name": "Long id"},
        {"id": "customfield_1\n", "name": "Newline id"},
        {"id": "customfield_é", "name": "Unicode id"},
    ],
)
def test_a_malformed_entry_is_ignored_and_counted(bad: object) -> None:
    result = fieldmap.parse_field_list([entry("summary", "Summary"), bad])
    assert [f.id for f in result.fields] == ["summary"]
    assert result.ignored == 1


def test_strict_parsing_refuses_what_lenient_parsing_ignores() -> None:
    with pytest.raises(fieldmap.FieldListError):
        fieldmap.parse_field_list([entry("summary", "Summary"), {"id": "x"}], strict=True)


def test_duplicate_ids_drop_every_entry_of_that_id() -> None:
    entries = [
        entry("customfield_1", "First", "number"),
        entry("customfield_1", "Second", "string"),
        entry("summary", "Summary"),
    ]
    result = fieldmap.parse_field_list(entries)
    assert [f.id for f in result.fields] == ["summary"]
    assert result.ignored == 2


def test_names_are_sanitized_and_capped() -> None:
    hostile = "Bad\x1b[31m name‮" + "x" * 1000
    result = fieldmap.parse_field_list([entry("customfield_1", hostile)])
    name = result.fields[0].name
    assert "\x1b" not in name
    assert "‮" not in name
    assert len(name) <= 256
    assert name.endswith("<truncated>")


def test_a_field_name_holding_a_configured_token_is_scrubbed() -> None:
    from ticketctl import sanitize

    sanitize.configure_scrubber(["tok-0123456789abcdef"])
    try:
        name = fieldmap.parse_field_list([entry("customfield_1", "A tok-0123456789abcdef B")])
        assert "tok-0123456789abcdef" not in name.fields[0].name
    finally:
        sanitize.configure_scrubber([])


@pytest.mark.parametrize(
    "schema",
    [None, "string", [], {}, {"type": 5}, {"type": ""}, {"custom": "x"}],
)
def test_an_unusable_schema_becomes_none(schema: object) -> None:
    raw = {"id": "customfield_1", "name": "Odd", "custom": True, "schema": schema}
    (field,) = fieldmap.parse_field_list([raw]).fields
    assert field.schema is None


def test_a_missing_schema_key_is_none_and_non_string_parts_are_dropped() -> None:
    (a,) = fieldmap.parse_field_list([{"id": "a", "name": "A"}]).fields
    assert a.schema is None
    assert a.custom is False
    raw = {"id": "b", "name": "B", "schema": {"type": "array", "items": 7, "custom": ["x"]}}
    (b,) = fieldmap.parse_field_list([raw]).fields
    assert b.schema == fieldmap.FieldSchema("array", None, None)


def test_custom_is_a_boolean_or_absent() -> None:
    for value, expected in ((True, True), (False, False)):
        raw = {"id": "customfield_1", "name": "X", "custom": value}
        assert fieldmap.parse_field_list([raw]).fields[0].custom is expected
    assert fieldmap.parse_field_list([{"id": "a", "name": "X"}]).fields[0].custom is False


@pytest.mark.parametrize("value", ["true", 1, None, [], {}])
def test_a_non_boolean_custom_makes_the_entry_malformed(value: object) -> None:
    raw = {"id": "customfield_1", "name": "X", "custom": value}
    result = fieldmap.parse_field_list([entry("summary", "Summary"), raw])
    assert [f.id for f in result.fields] == ["summary"]
    assert result.ignored == 1


@pytest.mark.parametrize(
    "tamper",
    [
        {"custom": "true"},
        {"custom": None},
        {"schema": "string"},
        {"schema": []},
        {"schema": {"type": "string", "x": 1}},
        {"name": "Name\x1b[31m"},
        {"name": "X" * 300},
        {"schema": {"type": "string\n"}},
        {"extra": 1},
    ],
)
def test_strict_parsing_refuses_a_poisoned_entry(tamper: dict[str, Any]) -> None:
    good = fieldmap.parse_field_list([entry("customfield_1", "X")]).fields[0].to_document()
    with pytest.raises(fieldmap.FieldListError):
        fieldmap.parse_field_list([{**good, **tamper}], strict=True)


def test_strict_parsing_accepts_a_stored_null_schema() -> None:
    doc = {"id": "a", "name": "A", "custom": False, "schema": None}
    assert fieldmap.parse_field_list([doc], strict=True).ignored == 0


def test_document_round_trip_is_stable() -> None:
    first = parsed(site_entries())
    again = fieldmap.parse_field_list([f.to_document() for f in first], strict=True).fields
    assert again == first


def test_schema_text_is_capped() -> None:
    raw = entry("customfield_1", "X", "t" * 500, custom="c" * 500, items="i" * 500)
    (f,) = fieldmap.parse_field_list([raw]).fields
    assert f.schema is not None
    assert len(f.schema.type) <= 128
    assert len(f.schema.custom or "") <= 256
    assert len(f.schema.items or "") <= 128


# --- discovery without a mapping --------------------------------------------------------------


def test_sprint_is_found_by_its_schema() -> None:
    res = fieldmap.resolve(parsed(site_entries()), "sprint", None)
    assert res.status is Status.FOUND
    assert ids(res) == [F_SPRINT]
    assert res.mapped is False


def test_a_field_named_sprint_without_the_schema_is_not_the_sprint_field() -> None:
    fields = parsed([entry("cf_five", "Sprint", "string", custom=f"{NS}:textfield")])
    assert fieldmap.resolve(fields, "sprint", None).status is Status.ABSENT


def test_estimate_is_the_union_of_the_points_schema_and_the_exact_legacy_name() -> None:
    res = fieldmap.resolve(parsed(site_entries()), "estimate", None)
    assert res.status is Status.AMBIGUOUS
    assert ids(res) == sorted([F_POINTS, F_ESTIMATE])


def test_the_legacy_points_field_is_found_only_by_its_exact_name() -> None:
    entries = [
        entry(F_POINTS, "Story Points", "number", custom=f"{NS}:float"),
        entry(F_VALUE, "Business value", "number", custom=f"{NS}:float"),
        entry(F_EFFORT, "Effort days", "number", custom=f"{NS}:float"),
        entry("cf_t1", "story points", "number", custom=f"{NS}:float"),
        entry("cf_t2", "Story Points ", "number", custom=f"{NS}:float"),
        entry("cf_t3", "Story Points", "string", custom=f"{NS}:textfield"),
    ]
    res = fieldmap.resolve(parsed(entries), "estimate", None)
    assert res.status is Status.FOUND
    assert ids(res) == [F_POINTS]


def test_float_fields_with_other_names_alone_give_no_estimate() -> None:
    entries = [entry(F_VALUE, "Business value", "number", custom=f"{NS}:float")]
    assert fieldmap.resolve(parsed(entries), "estimate", None).status is Status.ABSENT


def test_the_estimate_schema_alone_is_found() -> None:
    entries = [entry(F_ESTIMATE, "Story point estimate", "number", custom=f"{NS}:jsw-story-points")]
    res = fieldmap.resolve(parsed(entries), "estimate", None)
    assert (res.status, ids(res)) == (Status.FOUND, [F_ESTIMATE])


def test_the_legacy_name_on_a_foreign_float_namespace_is_not_the_legacy_field() -> None:
    entries = [entry(F_POINTS, "Story Points", "number", custom="other.plugin:float2")]
    assert fieldmap.resolve(parsed(entries), "estimate", None).status is Status.ABSENT


def test_epic_is_found_by_its_schema() -> None:
    entries = [entry("cf_epic", "Epic Link", "any", custom="x.y:gh-epic-link")]
    res = fieldmap.resolve(parsed(entries), "epic", None)
    assert (res.status, ids(res)) == (Status.FOUND, ["cf_epic"])


def test_acceptance_criteria_is_never_discovered() -> None:
    res = fieldmap.resolve(parsed(site_entries()), "acceptance_criteria", None)
    assert res.status is Status.ABSENT
    assert res.fields == ()


@pytest.mark.parametrize(
    ("logical", "provider_id"),
    [
        ("parent", "parent"),
        ("labels", "labels"),
        ("priority", "priority"),
        ("assignee", "assignee"),
        ("resolution", "resolution"),
        ("due_date", "duedate"),
    ],
)
def test_system_fields_resolve_to_their_system_ids(logical: str, provider_id: str) -> None:
    res = fieldmap.resolve(parsed(site_entries()), logical, None)
    assert (res.status, ids(res)) == (Status.FOUND, [provider_id])


def test_a_custom_field_that_borrows_a_system_id_is_not_the_system_field() -> None:
    fields = parsed([entry("labels", "Labels", "array", items="string", is_custom=True)])
    assert fieldmap.resolve(fields, "labels", None).status is Status.ABSENT


def test_an_unknown_logical_name_is_refused() -> None:
    with pytest.raises(ValueError, match="logical"):
        fieldmap.resolve(parsed(site_entries()), "customfield_90001", None)


# --- configured mappings ----------------------------------------------------------------------


def test_a_mapping_by_name_resolves_the_ambiguity() -> None:
    res = fieldmap.resolve(parsed(site_entries()), "estimate", mapping(name="Story Points"))
    assert (res.status, ids(res), res.mapped) == (Status.FOUND, [F_POINTS], True)


def test_a_mapping_by_id_resolves_the_ambiguity() -> None:
    res = fieldmap.resolve(parsed(site_entries()), "estimate", mapping(id=F_ESTIMATE))
    assert (res.status, ids(res)) == (Status.FOUND, [F_ESTIMATE])


def test_a_mapping_by_schema_matches_the_custom_schema() -> None:
    res = fieldmap.resolve(
        parsed(site_entries()), "estimate", mapping(schema=f"{NS}:jsw-story-points")
    )
    assert (res.status, ids(res)) == (Status.FOUND, [F_ESTIMATE])


def test_a_mapping_by_schema_uses_the_type_for_system_fields() -> None:
    res = fieldmap.resolve(parsed(site_entries()), "due_date", mapping(schema="date"))
    assert (res.status, ids(res)) == (Status.FOUND, ["duedate"])


def test_a_mapping_by_a_shared_schema_is_ambiguous() -> None:
    res = fieldmap.resolve(parsed(site_entries()), "estimate", mapping(schema=f"{NS}:float"))
    assert res.status is Status.AMBIGUOUS
    assert ids(res) == sorted([F_POINTS, F_VALUE, F_EFFORT])


def test_a_mapping_by_name_is_exact_and_case_sensitive() -> None:
    for name in ("story points", "Story Points ", "Story  Points"):
        res = fieldmap.resolve(parsed(site_entries()), "estimate", mapping(name=name))
        assert res.status is Status.MISSING


def test_a_mapping_naming_a_missing_field_is_missing_not_absent() -> None:
    res = fieldmap.resolve(parsed(site_entries()), "estimate", mapping(id="cf_gone"))
    assert (res.status, res.fields, res.mapped) == (Status.MISSING, (), True)


def test_a_mapping_beats_discovery_even_when_it_finds_nothing() -> None:
    res = fieldmap.resolve(parsed(site_entries()), "sprint", mapping(name="No such field"))
    assert res.status is Status.MISSING


def test_duplicate_display_names_make_a_name_mapping_ambiguous() -> None:
    entries = [
        entry("customfield_1", "Twin", "number", custom=f"{NS}:float"),
        entry("cf_two", "Twin", "string", custom=f"{NS}:textfield"),
    ]
    res = fieldmap.resolve(parsed(entries), "estimate", mapping(name="Twin"))
    assert res.status is Status.AMBIGUOUS
    assert ids(res) == ["cf_two", "customfield_1"]


def test_a_mapping_to_the_acceptance_criteria_field() -> None:
    res = fieldmap.resolve(
        parsed(site_entries()), "acceptance_criteria", mapping(name="Acceptance Criteria")
    )
    assert (res.status, ids(res)) == (Status.FOUND, [F_CRITERIA])


# --- lazy ambiguity ---------------------------------------------------------------------------


def test_need_returns_the_field_when_found() -> None:
    res = fieldmap.resolve(parsed(site_entries()), "sprint", None)
    assert res.need().id == F_SPRINT


def test_need_refuses_an_ambiguous_field_with_names_not_ids() -> None:
    res = fieldmap.resolve(parsed(site_entries()), "estimate", None)
    with pytest.raises(TicketctlError) as info:
        res.need(site="main")
    err = info.value
    assert (err.code, err.exit) == ("ambiguous_field", 21)
    assert err.details == {"candidates": ["Story Points", "Story point estimate"]}
    assert "customfield_" not in err.message
    assert "customfield_" not in str(err.details)
    assert "sites.main.fields.estimate" in err.message


def test_need_refuses_a_missing_mapping_as_unknown_field() -> None:
    res = fieldmap.resolve(parsed(site_entries()), "estimate", mapping(id="cf_gone"))
    with pytest.raises(TicketctlError) as info:
        res.need()
    err = info.value
    assert (err.code, err.reason, err.exit) == ("invalid_request", "unknown_field", 22)
    assert "cf_gone" not in err.message
    assert err.details == {"field": "estimate"}


def test_need_refuses_an_absent_unmapped_field_as_unknown_field() -> None:
    res = fieldmap.resolve(parsed(site_entries()), "acceptance_criteria", None)
    with pytest.raises(TicketctlError) as info:
        res.need()
    assert (info.value.code, info.value.reason) == ("invalid_request", "unknown_field")
    assert "not mapped" in info.value.message


def test_candidates_with_equal_names_are_told_apart_by_schema() -> None:
    entries = [
        entry("customfield_1", "Twin", "number", custom=f"{NS}:float"),
        entry("cf_two", "Twin", "string", custom=f"{NS}:textfield"),
    ]
    res = fieldmap.resolve(parsed(entries), "estimate", mapping(name="Twin"))
    assert res.candidate_labels() == [
        f"Twin ({NS}:float)",
        f"Twin ({NS}:textfield)",
    ]


def test_candidates_that_cannot_be_told_apart_are_kept_once_each() -> None:
    entries = [
        entry("customfield_1", "Twin", "number", custom=f"{NS}:float"),
        entry("cf_two", "Twin", "number", custom=f"{NS}:float"),
    ]
    res = fieldmap.resolve(parsed(entries), "estimate", mapping(name="Twin"))
    assert res.candidate_labels() == [f"Twin ({NS}:float)", f"Twin ({NS}:float)"]
    with pytest.raises(TicketctlError) as info:
        res.need()
    assert "customfield_" not in str(info.value.details)


def test_a_huge_candidate_set_is_capped_in_the_envelope() -> None:
    entries = [
        entry(f"customfield_{n}", "Twin", "number", custom=f"{NS}:float") for n in range(300)
    ]
    res = fieldmap.resolve(parsed(entries), "estimate", mapping(name="Twin"))
    assert res.status is Status.AMBIGUOUS
    with pytest.raises(TicketctlError) as info:
        res.need()
    assert info.value.details is not None
    assert len(info.value.details["candidates"]) <= fieldmap.MAX_CANDIDATES


def test_a_candidate_label_keeps_its_schema_suffix_when_the_name_is_long() -> None:
    entries = [
        entry(f"customfield_{n}", "N" * 256, "number", custom=f"{NS}:float{n}") for n in range(80)
    ]
    res = fieldmap.resolve(parsed(entries), "estimate", mapping(name="N" * 256))
    labels = res.candidate_labels()
    assert len(set(labels)) == 80
    assert all(len(label) <= 256 and label.endswith(")") for label in labels)


def test_a_candidate_label_never_exceeds_the_name_cap() -> None:
    entries = [
        entry("customfield_1", "Twin", "number", custom="c" * 256),
        entry("cf_two", "Twin", "number", custom="d" * 256),
    ]
    res = fieldmap.resolve(parsed(entries), "estimate", mapping(name="Twin"))
    assert all(len(label) <= 256 for label in res.candidate_labels())


def test_a_candidate_label_never_shows_a_custom_field_id() -> None:
    entries = [
        entry("customfield_1", "Twin customfield_90001", "number", custom=f"{NS}:float"),
        entry("cf_two", "Twin customfield_90001", "number", custom="customfield_90002:x"),
    ]
    res = fieldmap.resolve(parsed(entries), "estimate", mapping(name="Twin customfield_90001"))
    labels = res.candidate_labels()
    assert all("customfield_" not in label for label in labels)
    assert any("<id>" in label for label in labels)
    with pytest.raises(TicketctlError) as info:
        res.need()
    assert "customfield_" not in str(info.value.details)


def test_sixty_candidates_yield_exactly_fifty_in_the_envelope() -> None:
    entries = [
        entry(f"customfield_{n}", f"Twin {n:02d}", "number", custom=f"{NS}:float")
        for n in range(60)
    ]
    res = fieldmap.resolve(parsed(entries), "estimate", mapping(schema=f"{NS}:float"))
    with pytest.raises(TicketctlError) as info:
        res.need()
    assert info.value.details is not None
    assert len(info.value.details["candidates"]) == 50
    cfg = Fields(estimate=mapping(schema=f"{NS}:float"))
    (problem,) = [p for p in fieldmap.SiteFields(parsed(entries), cfg).problems()]
    assert len(problem.candidates) == 50


def test_a_mapping_by_name_does_not_match_a_prefix() -> None:
    res = fieldmap.resolve(parsed(site_entries()), "estimate", mapping(name="Story"))
    assert res.status is Status.MISSING


def test_a_mapping_by_id_is_case_sensitive() -> None:
    res = fieldmap.resolve(parsed(site_entries()), "estimate", mapping(id=F_ESTIMATE.upper()))
    assert res.status is Status.MISSING


@pytest.mark.parametrize(
    ("logical", "custom"),
    [
        ("estimate", f"{NS}:jsw-story-points-y"),
        ("estimate", "jsw-story-points"),
        ("sprint", "gh-sprint"),
        ("sprint", "x:gh-sprint-y"),
        ("epic", "gh-epic-link"),
        ("epic", "x:gh-epic-link-y"),
    ],
)
def test_the_discovery_suffix_must_be_a_suffix_with_its_colon(logical: str, custom: str) -> None:
    entries = [entry("customfield_1", "Other", "string", custom=custom)]
    assert fieldmap.resolve(parsed(entries), logical, None).status is Status.ABSENT


def test_an_absent_field_is_not_a_missing_mapping() -> None:
    entries = [entry("customfield_1", "Other")]
    res = fieldmap.resolve(parsed(entries), "epic", None)
    assert res.status is Status.ABSENT


# --- SiteFields: logical names and extras -----------------------------------------------------


def site(
    fields_cfg: Fields | None = None, entries: list[dict[str, Any]] | None = None
) -> fieldmap.SiteFields:
    return fieldmap.SiteFields(parsed(entries or site_entries()), fields_cfg or Fields())


def test_site_fields_resolve_logical_names_and_extras() -> None:
    cfg = Fields(estimate=mapping(name="Story Points"), extra={"value": mapping(id=F_VALUE)})
    sf = site(cfg)
    assert sf.resolve("estimate").need().id == F_POINTS
    assert sf.resolve("value").need().id == F_VALUE
    assert sf.resolve("sprint").need().id == F_SPRINT


def test_an_unknown_name_is_unknown_field_for_site_fields() -> None:
    with pytest.raises(TicketctlError) as info:
        site().resolve("nothing")
    assert (info.value.code, info.value.reason) == ("invalid_request", "unknown_field")


def test_a_logical_name_wins_over_a_same_named_extra() -> None:
    # The loader forbids this; a Config built in code must not let an extra shadow a logical field.
    cfg = Fields.model_construct(extra={"sprint": mapping(id=F_VALUE)})
    assert site(cfg).resolve("sprint").need().id == F_SPRINT


def test_extras_resolve_only_by_their_configured_mapping() -> None:
    sf = site(Fields(extra={"gone": mapping(id="cf_gone")}))
    res = sf.resolve("gone")
    assert res.status is Status.MISSING
    assert res.logical == "gone"


def test_extra_names_are_not_confused_with_provider_display_names() -> None:
    sf = site(Fields(extra={"summary_text": mapping(name="Summary")}))
    assert sf.resolve("summary_text").need().id == "summary"
    with pytest.raises(TicketctlError):
        sf.resolve("Summary")


# --- problems (used by fields check) ---------------------------------------------------------


def problems(
    cfg: Fields, entries: list[dict[str, Any]] | None = None
) -> list[tuple[str, str, list[str]]]:
    return [(p.logical, p.problem, p.candidates) for p in site(cfg, entries).problems()]


def test_sound_mappings_have_no_problem_apart_from_discovery_ambiguity() -> None:
    cfg = Fields(
        estimate=mapping(name="Story Points"),
        acceptance_criteria=mapping(name="Acceptance Criteria"),
    )
    assert problems(cfg) == []


def test_the_estimate_ambiguity_is_a_problem_even_without_a_mapping() -> None:
    assert problems(Fields()) == [
        ("estimate", "ambiguous", ["Story Points", "Story point estimate"])
    ]


def test_a_mapping_naming_a_missing_field_is_the_missing_problem() -> None:
    cfg = Fields(estimate=mapping(name="Story Points"), sprint=mapping(id="cf_gone"))
    assert problems(cfg) == [("sprint", "missing", [])]


def test_a_missing_extra_is_a_missing_problem() -> None:
    cfg = Fields(estimate=mapping(name="Story Points"), extra={"gone": mapping(name="Nope")})
    assert problems(cfg) == [("gone", "missing", [])]


def test_absent_unmapped_fields_are_not_problems() -> None:
    entries = [e for e in site_entries() if e["id"] not in (F_SPRINT, F_ESTIMATE)]
    assert problems(Fields(), entries) == []


@pytest.mark.parametrize(
    ("logical", "bad_entry"),
    [
        ("estimate", entry("cf_seven", "Text points", "string", custom=f"{NS}:textfield")),
        ("sprint", entry("cf_seven", "Not a sprint", "string", custom=f"{NS}:textfield")),
        ("acceptance_criteria", entry("cf_seven", "Number", "number", custom=f"{NS}:float")),
        ("due_date", entry("cf_seven", "Not a date", "string", custom=f"{NS}:textfield")),
    ],
)
def test_a_mapped_field_of_the_wrong_type_is_a_type_mismatch(
    logical: str, bad_entry: dict[str, Any]
) -> None:
    cfg = Fields.model_validate({logical: {"id": "cf_seven"}})
    got = problems(cfg, [*site_entries(), bad_entry])
    assert (logical, "type_mismatch", []) in got


@pytest.mark.parametrize(
    ("logical", "good_entry"),
    [
        ("estimate", entry("cf_seven", "N", "number", custom=f"{NS}:float")),
        ("sprint", entry("cf_seven", "S", "array", items="json", custom="x:gh-sprint")),
        ("acceptance_criteria", entry("cf_seven", "T", "string", custom=f"{NS}:textarea")),
        ("due_date", entry("cf_seven", "D", "date", custom=f"{NS}:datepicker")),
    ],
)
def test_a_mapped_field_of_the_right_type_is_sound(
    logical: str, good_entry: dict[str, Any]
) -> None:
    cfg = Fields.model_validate({logical: {"id": "cf_seven"}})
    entries = [e for e in site_entries() if e["id"] not in (F_ESTIMATE, F_POINTS)]
    assert problems(cfg, [*entries, good_entry]) == []


def test_a_mapped_field_without_a_schema_is_a_type_mismatch() -> None:
    bare = {"id": "cf_seven", "name": "Bare", "custom": True}
    cfg = Fields(estimate=mapping(id="cf_seven"))
    assert ("estimate", "type_mismatch", []) in problems(cfg, [*site_entries(), bare])


def test_the_fields_without_a_type_rule_never_mismatch() -> None:
    odd = entry("cf_seven", "Odd", "string", custom=f"{NS}:textfield")
    cfg = Fields(epic=mapping(id="cf_seven"), labels=mapping(id="cf_seven"))
    got = problems(cfg, [*site_entries(), odd])
    assert [p for p in got if p[1] == "type_mismatch"] == []


def test_problems_are_ordered_by_logical_name_then_extras() -> None:
    cfg = Fields(
        sprint=mapping(id="cf_gone2"),
        due_date=mapping(id="cf_gone3"),
        extra={"zeta": mapping(id="cf_gone4"), "alpha": mapping(id="cf_gone5")},
    )
    names = [p[0] for p in problems(cfg)]
    assert names == ["estimate", "sprint", "due_date", "alpha", "zeta"]


def test_an_ambiguous_mapping_reports_candidates_without_ids() -> None:
    cfg = Fields(estimate=mapping(schema=f"{NS}:float"))
    ((_, kind, cands),) = problems(cfg)
    assert kind == "ambiguous"
    assert cands == ["Business value", "Effort days", "Story Points"]


def test_problem_candidate_lists_are_capped() -> None:
    entries = [
        entry(f"customfield_{n}", "Twin", "number", custom=f"{NS}:float") for n in range(300)
    ]
    cfg = Fields(estimate=mapping(name="Twin"))
    ((_, kind, cands),) = problems(cfg, entries)
    assert kind == "ambiguous"
    assert len(cands) <= fieldmap.MAX_CANDIDATES
