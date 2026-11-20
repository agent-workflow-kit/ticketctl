"""The --set / --field grammar, name resolution and dedicated-field refusals (issue #27).

Synthetic field lists and metadata only; no provider is involved.
"""

from __future__ import annotations

from typing import Any

import pytest

from ticketctl import fieldmap, setspec
from ticketctl.config import FieldMapping, Fields
from ticketctl.errors import TicketctlError

NS = "com.atlassian.jira.plugin.system.customfieldtypes"
SPRINT = f"{NS}:gh-sprint"
SYSTEM = {"summary", "labels", "assignee", "priority", "resolution", "parent", "duedate"}


def mapping(**kw: str) -> FieldMapping:
    return FieldMapping(**kw)


def fld(fid: str, name: str, type_: str = "string", **schema: Any) -> dict[str, Any]:
    return {
        "id": fid,
        "name": name,
        "custom": fid not in SYSTEM and fid not in ("description", "reporter"),
        "schema": {"type": type_, **schema},
    }


FIELD_LIST = [
    fld("summary", "Summary"),
    fld("description", "Description"),
    fld("reporter", "Reporter", "user"),
    fld("labels", "Labels", "array", items="string"),
    fld("assignee", "Assignee", "user"),
    fld("priority", "Priority", "priority"),
    fld("resolution", "Resolution", "resolution"),
    fld("parent", "Parent", "issuelink"),
    fld("duedate", "Due date", "date"),
    fld("customfield_90001", "Sprint", "array", items="json", custom=SPRINT),
    fld("customfield_90002", "Effort", "number"),
    fld("customfield_90003", "Size", "option"),
    fld("customfield_90004", "Locked", "string"),
    fld("customfield_90005", "Twin", "string"),
    fld("customfield_90006", "twin", "string"),
]


def site(config: Fields | None = None) -> fieldmap.SiteFields:
    cfg = config or Fields(extra={"effort": mapping(id="customfield_90002")})
    return fieldmap.SiteFields(fieldmap.parse_field_list(FIELD_LIST).fields, cfg)


def meta(*ids: str, operations: dict[str, list[str]] | None = None) -> tuple[Any, ...]:
    raw: dict[str, Any] = {}
    for f in FIELD_LIST:
        if f["id"] in ids:
            entry = {k: f[k] for k in ("name", "schema")}
            if operations and f["id"] in operations:
                entry["operations"] = operations[f["id"]]
            raw[f["id"]] = entry
    return setspec.parse_meta({"fields": raw})


ALL = ("summary", "description", "reporter", "labels", "assignee", "priority", "resolution")
ALL += ("parent", "duedate", "customfield_90001", "customfield_90002", "customfield_90003")
ALL += ("customfield_90004", "customfield_90005", "customfield_90006")


def resolve(items: list[str], command: str = "update", metadata: Any = None, flag: str = "--set"):
    assigns = setspec.check_offline(command, items, flag)
    return setspec.resolve_assignments(
        assigns, command=command, site=site(), meta=metadata if metadata is not None else meta(*ALL)
    )


def refusal(exc: pytest.ExceptionInfo[BaseException]) -> tuple[str, str | None, str]:
    err = exc.value
    assert isinstance(err, TicketctlError)
    return err.code, err.reason, err.message


# ---- grammar ---------------------------------------------------------------------------------


def test_split_at_first_equals() -> None:
    (a,) = setspec.check_offline("update", ["effort=a=b"], "--set")
    assert a.name == "effort" and a.source == setspec.Source("inline", "a=b")


def test_sources() -> None:
    a, b, c, d = setspec.check_offline("update", ["x=@some/file", "y=-", "z=", "w=@@x"], "--set")
    assert (a.source.kind, a.source.value) == ("file", "some/file")
    assert (b.source.kind, c.source.value, c.source.kind) == ("stdin", "", "inline")
    assert (d.source.kind, d.source.value) == ("file", "@x")


@pytest.mark.parametrize(
    "item",
    ["noequals", "=value", "x=@", "a\nb=1", "a\x00b=1", "a‮b=1", "x" * 257 + "=1"],
)
def test_malformed_assignment_is_invalid_argument(item: str) -> None:
    with pytest.raises(TicketctlError) as exc:
        setspec.check_offline("update", [item], "--set")
    assert refusal(exc)[:2] == ("usage", "invalid_argument")


def test_error_message_never_echoes_the_value() -> None:
    with pytest.raises(TicketctlError) as exc:
        setspec.check_offline("update", ["noequals-SECRETVALUE"], "--set")
    assert "SECRETVALUE" not in refusal(exc)[2]
    with pytest.raises(TicketctlError) as exc:
        setspec.check_offline("update", ["x=@"], "--set")
    assert "x=@" not in refusal(exc)[2]


def test_too_many_assignments() -> None:
    items = [f"n{i}=1" for i in range(setspec.MAX_ASSIGNMENTS + 1)]
    with pytest.raises(TicketctlError) as exc:
        setspec.check_offline("update", items, "--set")
    assert refusal(exc)[:2] == ("usage", "invalid_argument")


def test_huge_inline_value_is_refused() -> None:
    with pytest.raises(TicketctlError) as exc:
        setspec.check_offline("update", ["x=" + "a" * 1_000_001], "--set")
    assert refusal(exc)[:2] == ("invalid_request", "invalid_value")


# ---- typed refusals (offline) ------------------------------------------------------------------


@pytest.mark.parametrize("name", ["assignee", "labels", "resolution", "sprint", "title"])
def test_dedicated_logical_names_are_invalid_combination(name: str) -> None:
    for command in ("create", "update", "transition"):
        with pytest.raises(TicketctlError) as exc:
            setspec.check_offline(command, [f"{name}=x"], "--set")
        code, reason, _ = refusal(exc)
        assert (code, reason) == ("usage", "invalid_combination")


def test_refusal_message_names_the_flag() -> None:
    cases = {
        ("update", "assignee"): "--assign",
        ("update", "labels"): "--add-label",
        ("create", "labels"): "--label",
        ("update", "title"): "--title",
        ("transition", "resolution"): "--resolution",
        ("create", "sprint"): "sprint add",
        ("create", "parent"): "--parent",
    }
    for (command, name), flag in cases.items():
        with pytest.raises(TicketctlError) as exc:
            setspec.check_offline(command, [f"{name}=x"], "--set")
        assert flag in refusal(exc)[2]


def test_parent_is_refused_only_on_create() -> None:
    setspec.check_offline("update", ["parent=x"], "--set")
    with pytest.raises(TicketctlError):
        setspec.check_offline("create", ["parent=x"], "--set")


def test_same_typed_name_twice_is_invalid_combination() -> None:
    with pytest.raises(TicketctlError) as exc:
        setspec.check_offline("update", ["effort=1", "effort=2"], "--set")
    assert refusal(exc)[:2] == ("usage", "invalid_combination")


# ---- resolution --------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "name",
    ["Labels", "LABELS", "Summary", "SUMMARY", "Assignee", "Sprint", "sPrInT", "Resolution"]
    + ["Reporter", "REPORTER"],
)
def test_display_names_of_dedicated_fields_refuse_in_any_case(name: str) -> None:
    with pytest.raises(TicketctlError) as exc:
        resolve([f"{name}=x"])
    assert refusal(exc)[:2] == ("usage", "invalid_combination")


def test_parent_display_name_refused_on_create_not_update() -> None:
    with pytest.raises(TicketctlError) as exc:
        resolve(["Parent=x"], "create")
    assert refusal(exc)[:2] == ("usage", "invalid_combination")
    (r,) = resolve(["Parent=x"], "update")
    assert r.field.id == "parent"


def test_description_is_update_only() -> None:
    (r,) = resolve(["Description=@f"], "update")
    assert r.field.id == "description"
    for command in ("create", "transition"):
        with pytest.raises(TicketctlError) as exc:
            resolve(["description=@f"], command)
        assert refusal(exc)[:2] == ("usage", "invalid_combination")
        assert "body-file" in refusal(exc)[2] or command == "transition"


def test_reporter_message_says_it_cannot_be_set() -> None:
    with pytest.raises(TicketctlError) as exc:
        resolve(["reporter=x"])
    assert "cannot be set" in refusal(exc)[2]


def test_dedicated_field_with_no_metadata_entry_is_still_a_refusal() -> None:
    # createmeta does not list labels or reporter; the refusal is exit 2, not not_editable
    with pytest.raises(TicketctlError) as exc:
        resolve(["Labels=x"], "create", meta("summary", "priority"))
    assert refusal(exc)[:2] == ("usage", "invalid_combination")


def test_mapped_sprint_field_is_refused_by_its_resolved_id() -> None:
    cfg = Fields(sprint=mapping(id="customfield_90004"), extra={})
    assigns = setspec.check_offline("update", ["Locked=x"], "--set")
    with pytest.raises(TicketctlError) as exc:
        setspec.resolve_assignments(assigns, command="update", site=site(cfg), meta=meta(*ALL))
    assert refusal(exc)[:2] == ("usage", "invalid_combination")


def test_logical_then_extra_then_display_order() -> None:
    (a, b, c) = resolve(["priority=High", "effort=3", "Size=Large"])
    assert (a.field.id, b.field.id, c.field.id) == (
        "priority",
        "customfield_90002",
        "customfield_90003",
    )


def test_display_name_is_case_insensitive_and_unique() -> None:
    (r,) = resolve(["size=x"])
    assert r.field.id == "customfield_90003"
    assert r.assignment.name == "size"


def test_ambiguous_display_name_exits_21() -> None:
    with pytest.raises(TicketctlError) as exc:
        resolve(["TWIN=x"])
    err = exc.value
    assert err.exit == 21 and err.code == "ambiguous_field"
    assert err.details is not None and len(err.details["candidates"]) == 2
    assert "customfield_" not in " ".join(err.details["candidates"])


def test_one_field_spelled_twice_is_invalid_combination() -> None:
    with pytest.raises(TicketctlError) as exc:
        resolve(["effort=1", "Effort=2"])
    assert refusal(exc)[:2] == ("usage", "invalid_combination")
    with pytest.raises(TicketctlError) as exc:
        resolve(["due date=2020-01-01", "DUE DATE=2020-01-02"])
    assert refusal(exc)[:2] == ("usage", "invalid_combination")


def test_logical_and_display_spelling_of_one_field_is_a_duplicate() -> None:
    with pytest.raises(TicketctlError) as exc:
        resolve(["priority=High", "Priority=Low"])
    assert refusal(exc)[:2] == ("usage", "invalid_combination")


@pytest.mark.parametrize("typed", ["customfield_90002", "CUSTOMFIELD_90002", "customfield_1"])
def test_customfield_ids_are_unknown_unless_mapped(typed: str) -> None:
    cfg = Fields(extra={"effort": mapping(id="customfield_90002")})
    assigns = setspec.check_offline("update", [f"{typed}=1"], "--set")
    if typed.lower() == "customfield_90002":
        # mapped: allowed, and it is the field the mapping names
        (r,) = setspec.resolve_assignments(
            assigns, command="update", site=site(cfg), meta=meta(*ALL)
        )
        assert r.field.id == "customfield_90002"
    else:
        with pytest.raises(TicketctlError) as exc:
            setspec.resolve_assignments(assigns, command="update", site=site(cfg), meta=meta(*ALL))
        assert refusal(exc)[:2] == ("invalid_request", "unknown_field")


def test_unmapped_customfield_id_is_unknown_field() -> None:
    assigns = setspec.check_offline("update", ["customfield_90003=x"], "--set")
    with pytest.raises(TicketctlError) as exc:
        setspec.resolve_assignments(assigns, command="update", site=site(), meta=meta(*ALL))
    assert refusal(exc)[:2] == ("invalid_request", "unknown_field")


def test_unknown_name_is_unknown_field() -> None:
    with pytest.raises(TicketctlError) as exc:
        resolve(["Nothing here=1"])
    assert refusal(exc)[:2] == ("invalid_request", "unknown_field")


def test_field_in_list_but_not_in_metadata_is_not_editable() -> None:
    with pytest.raises(TicketctlError) as exc:
        resolve(["Size=x"], metadata=meta("summary"))
    assert refusal(exc)[:2] == ("invalid_request", "not_editable")


def test_field_without_set_operation_is_not_editable() -> None:
    metadata = meta(*ALL, operations={"customfield_90003": ["add"]})
    with pytest.raises(TicketctlError) as exc:
        resolve(["Size=x"], metadata=metadata)
    assert refusal(exc)[:2] == ("invalid_request", "not_editable")


def test_unmapped_absent_logical_name_is_unknown_field() -> None:
    with pytest.raises(TicketctlError) as exc:
        resolve(["estimate=1"])
    assert refusal(exc)[:2] == ("invalid_request", "unknown_field")


def test_ambiguous_logical_mapping_exits_21() -> None:
    cfg = Fields(estimate=mapping(schema="string"))
    assigns = setspec.check_offline("update", ["estimate=1"], "--set")
    with pytest.raises(TicketctlError) as exc:
        setspec.resolve_assignments(assigns, command="update", site=site(cfg), meta=meta(*ALL))
    assert exc.value.exit == 21


def test_refused_field_is_judged_before_any_source_is_read() -> None:
    # resolution never reads a file: resolve_assignments takes no filesystem access at all
    with pytest.raises(TicketctlError):
        resolve(["resolution=@/nonexistent/file"])


# ---- metadata parsing --------------------------------------------------------------------------


def test_parse_meta_is_defensive() -> None:
    with pytest.raises(TicketctlError) as exc:
        setspec.parse_meta(["not", "a", "dict"])
    assert exc.value.code == "provider_changed"
    with pytest.raises(TicketctlError):
        setspec.parse_meta({"fields": []})
    parsed = setspec.parse_meta(
        {
            "fields": {
                "customfield_90001": {"name": 5},
                "bad id!": {"name": "X", "schema": {"type": "string"}},
                "customfield_90002": {
                    "name": "Ok‮",
                    "schema": {"type": "option"},
                    "required": True,
                    "allowedValues": [{"value": "a"}, {"name": "b"}, 7, {"value": 3}, None],
                },
            }
        }
    )
    assert [m.field.id for m in parsed] == ["customfield_90002"]
    (m,) = parsed
    assert m.required is True and m.allowed == ("a", "b")
    assert "‮" not in m.field.name


def test_parse_meta_caps_field_count() -> None:
    raw = {f"customfield_{i}": {"name": f"n{i}"} for i in range(setspec.MAX_META_FIELDS + 1)}
    with pytest.raises(TicketctlError) as exc:
        setspec.parse_meta({"fields": raw})
    assert exc.value.code == "provider_changed"


# ---- review round 1 ----------------------------------------------------------------------------


BASE = [f for f in FIELD_LIST if not f["id"].startswith("customfield_")]


def custom_site(extra_fields: list[dict[str, Any]], config: Fields | None = None) -> Any:
    parsed = fieldmap.parse_field_list([*BASE, *extra_fields]).fields
    return fieldmap.SiteFields(parsed, config or Fields())


def custom_meta(extra_fields: list[dict[str, Any]], *ids: str) -> tuple[Any, ...]:
    raw = {f["id"]: {"name": f["name"], "schema": f["schema"]} for f in [*BASE, *extra_fields]}
    return setspec.parse_meta({"fields": {k: v for k, v in raw.items() if k in ids}})


@pytest.mark.parametrize(
    "name", ["Labels", "LABELS", "lAbels", "Assignee", "ASSIGNEE", "Resolution"]
)
def test_custom_field_with_a_dedicated_name_does_not_hide_the_refusal(name: str) -> None:
    clone = fld("customfield_90001", name, "string")
    metadata = custom_meta([clone], "customfield_90001", "summary")
    assigns = setspec.check_offline("update", [f"{name}=x"], "--set")
    with pytest.raises(TicketctlError) as exc:
        setspec.resolve_assignments(
            assigns, command="update", site=custom_site([clone]), meta=metadata
        )
    assert refusal(exc)[:2] == ("usage", "invalid_combination")


def test_custom_field_named_sprint_does_not_hide_the_sprint_refusal() -> None:
    real = fld("customfield_90001", "Sprint", "array", items="json", custom=SPRINT)
    clone = fld("customfield_90002", "Sprint", "string")
    metadata = custom_meta([real, clone], "customfield_90002")
    assigns = setspec.check_offline("update", ["Sprint=x"], "--set")
    with pytest.raises(TicketctlError) as exc:
        setspec.resolve_assignments(
            assigns, command="update", site=custom_site([real, clone]), meta=metadata
        )
    assert refusal(exc)[:2] == ("usage", "invalid_combination")


def test_sprint_schema_refuses_when_the_sprint_field_is_not_mapped_to_it() -> None:
    cfg = Fields(sprint=mapping(id="customfield_90004"), extra={})
    assigns = setspec.check_offline("update", ["Sprint=x"], "--set")
    with pytest.raises(TicketctlError) as exc:
        setspec.resolve_assignments(assigns, command="update", site=site(cfg), meta=meta(*ALL))
    assert refusal(exc)[:2] == ("usage", "invalid_combination")


@pytest.mark.parametrize("allowed", [[], [None, 5, {"x": "y"}], "nope"])
def test_option_field_without_usable_allowed_values_lists_none(allowed: Any) -> None:
    (m,) = setspec.parse_meta(
        {"fields": {"customfield_90003": {"name": "Size", "allowedValues": allowed}}}
    )
    assert m.allowed is None


@pytest.mark.parametrize("operations", ["set", {"set": True}, 5, None])
def test_malformed_operations_mean_no_set_operation(operations: Any) -> None:
    raw = {"customfield_90003": {"name": "Size", "schema": {"type": "option"}}}
    raw["customfield_90003"]["operations"] = operations
    parsed = setspec.parse_meta({"fields": raw})
    assigns = setspec.check_offline("update", ["Size=x"], "--set")
    with pytest.raises(TicketctlError) as exc:
        setspec.resolve_assignments(assigns, command="update", site=site(), meta=parsed)
    assert refusal(exc)[:2] == ("invalid_request", "not_editable")


def test_absent_operations_mean_the_metadata_does_not_say() -> None:
    parsed = setspec.parse_meta(
        {"fields": {"customfield_90003": {"name": "Size", "schema": {"type": "option"}}}}
    )
    (r,) = setspec.resolve_assignments(
        setspec.check_offline("update", ["Size=x"], "--set"),
        command="update",
        site=site(),
        meta=parsed,
    )
    assert r.field.id == "customfield_90003"


def test_customfield_id_is_accepted_for_a_discovered_logical_mapping() -> None:
    points = fld("customfield_90003", "Points", "number", custom=f"{NS}:jsw-story-points")
    metadata = custom_meta([points], "customfield_90003")
    assigns = setspec.check_offline("update", ["CustomField_90003=3"], "--set")
    (r,) = setspec.resolve_assignments(
        assigns, command="update", site=custom_site([points]), meta=metadata
    )
    assert r.field.id == "customfield_90003"
