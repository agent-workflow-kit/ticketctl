"""``fields types``: the read-only listing of creatable issue types (issue #90)."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

import pytest

from tests.fake_provider import (
    F_POINTS,
    SITE_EMAIL,
    SITE_TOKEN,
    FakeProvider,
    Out,
    ProviderError,
)
from tests.golden import assert_golden, validator
from ticketctl import createcmd
from ticketctl.cli import main
from ticketctl.readutil import hide_field_ids

SITE_ENV_VAR = "TICKETCTL_TEST_SITE_ENV"


class Cli:
    def __init__(self, root: Path, fake: FakeProvider, capsys: Any, projects: str = "") -> None:
        self.fake, self.capsys, self.root = fake, capsys, root
        self.config = root / "config.yaml"
        self.config.write_text(
            f"sites:\n  main:\n    url: {fake.url}\n    email: {SITE_EMAIL}\n"
            f"    token_env: {SITE_ENV_VAR}\n    allow_http: true\n{projects}"
        )
        self.config.chmod(0o600)

    def run(self, *argv: str) -> tuple[int, str, str]:
        code = main(["--config", str(self.config), *argv])
        cap = self.capsys.readouterr()
        assert SITE_TOKEN not in cap.out + cap.err
        return code, cap.out, cap.err

    def doc(self, *argv: str) -> tuple[int, dict[str, Any]]:
        code, out, _ = self.run("--json", *argv)
        return code, json.loads(out)

    def types(self, *argv: str) -> tuple[int, dict[str, Any]]:
        return self.doc("fields", "types", *argv)

    def writes(self) -> list[Any]:
        return [c for c in self.fake.calls if c.method != "GET"]


@pytest.fixture
def cli(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: Any, fake: FakeProvider) -> Cli:
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    monkeypatch.setenv(SITE_ENV_VAR, SITE_TOKEN)
    monkeypatch.setenv("PATH", "/nonexistent")
    return Cli(tmp_path, fake, capsys)


def swap(cli: Cli, **handlers: Any) -> None:
    for name, handler in handlers.items():
        setattr(cli.fake, name, handler)
    cli.fake._routes = []
    cli.fake._build_routes()


def with_fields(cli: Cli, **extra: dict[str, Any]) -> None:
    """Make createmeta list extra fields (``id -> entry keys``; an existing one is updated)."""
    original = cli.fake._createmeta_fields

    def fields(call: Any, project: str, type_id: str) -> Out:
        out = original(call, project, type_id)
        for fid, entry in extra.items():
            old = next((f for f in out.body["fields"] if f["fieldId"] == fid), None)
            if old is None:
                out.body["fields"].append({"fieldId": fid, "schema": {"type": "string"}, **entry})
            else:
                old.update(entry)
        return out

    swap(cli, _createmeta_fields=fields)


def type_entry(ident: str, name: str, **more: Any) -> dict[str, Any]:
    return {"id": ident, "name": name, "subtask": False, **more}


# ---- the listing -------------------------------------------------------------------------------


def test_lists_the_types_sorted_with_no_required_fields_read(cli: Cli) -> None:
    code, doc = cli.types("--project", "DEMO")
    assert code == 0, doc
    assert [t["name"] for t in doc["types"]] == ["Bug", "Epic", "Sub-task", "Task"]
    assert all(t["required_fields"] is None for t in doc["types"])
    assert {t["name"]: t["subtask"] for t in doc["types"]}["Sub-task"] is True
    assert (doc["site"], doc["project"]) == ("main", "DEMO")
    validator("cli/fields-types.json").validate(doc)
    assert_golden("cli", "fields-types", "list", doc)
    assert not [c for c in cli.fake.calls if "/issuetypes/" in c.path]  # no per-type read


def test_one_type_lists_its_required_fields(cli: Cli) -> None:
    with_fields(
        cli,
        **{
            F_POINTS: {
                "name": "Story Points",
                "required": True,
                "schema": {"type": "number"},
            },
            "priority": {
                "required": True,
                "allowedValues": [{"name": "High"}, {"name": "Low"}],
            },
        },
    )
    code, doc = cli.types("--project", "DEMO", "--type", "Task")
    assert code == 0, doc
    (task,) = doc["types"]
    assert task["name"] == "Task" and task["subtask"] is False
    names = {r["name"]: r for r in task["required_fields"]}
    assert set(names) == {"Story Points", "Priority"}
    assert names["Story Points"]["kind"] == "number"
    assert names["Priority"]["allowed_values"] == ["High", "Low"]
    validator("cli/fields-types.json").validate(doc)
    assert_golden("cli", "fields-types", "one-type", doc)


def test_required_fields_match_what_create_reports_missing(cli: Cli) -> None:
    with_fields(cli, **{F_POINTS: {"name": "Story Points", "required": True}})
    _, listed = cli.types("--project", "DEMO", "--type", "Task")
    _, refused = cli.doc(
        "--dry-run", "create", "--project", "DEMO", "--type", "Task", "--title", "t"
    )
    assert refused["error"]["reason"] == "missing_required_fields"
    assert listed["types"][0]["required_fields"] == refused["error"]["details"]["required_fields"]


def test_a_type_with_nothing_required_has_an_empty_list(cli: Cli) -> None:
    code, doc = cli.types("--project", "DEMO", "--type", "Bug")
    assert code == 0 and doc["types"][0]["required_fields"] == []


def test_parent_is_never_a_required_field_but_subtask_says_it(cli: Cli) -> None:
    # The fake lists Parent as required on the sub-task; create never reports it (the provider
    # judges it), and neither does the listing: subtask: true is the honest signal.
    code, doc = cli.types("--project", "DEMO", "--type", "Sub-task")
    assert code == 0, doc
    (sub,) = doc["types"]
    assert sub["subtask"] is True and sub["required_fields"] == []


@pytest.mark.parametrize("fid", ["summary", "project", "issuetype", "reporter"])
def test_fields_ticketctl_supplies_are_not_listed(cli: Cli, fid: str) -> None:
    with_fields(cli, **{fid: {"name": fid.title(), "required": True}})
    _, doc = cli.types("--project", "DEMO", "--type", "Task")
    assert doc["types"][0]["required_fields"] == []


def test_a_required_field_with_a_default_is_not_listed(cli: Cli) -> None:
    with_fields(
        cli, **{F_POINTS: {"name": "Story Points", "required": True, "hasDefaultValue": True}}
    )
    _, doc = cli.types("--project", "DEMO", "--type", "Task")
    assert doc["types"][0]["required_fields"] == []


def test_a_field_create_fills_from_a_flag_is_still_listed(cli: Cli) -> None:
    with_fields(cli, description={"required": True})
    _, doc = cli.types("--project", "DEMO", "--type", "Task")
    assert [r["name"] for r in doc["types"][0]["required_fields"]] == ["Description"]


def test_the_type_name_matches_like_create(cli: Cli) -> None:
    code, doc = cli.types("--project", "DEMO", "--type", "tAsK")
    assert code == 0 and [t["name"] for t in doc["types"]] == ["Task"]


def test_an_unknown_type_is_invalid_value_with_candidates(cli: Cli) -> None:
    code, env = cli.types("--project", "DEMO", "--type", "Nope")
    assert (code, env["error"]["reason"]) == (22, "invalid_value")
    assert env["error"]["details"]["candidates"] == ["Bug", "Epic", "Sub-task", "Task"]


def test_two_case_variants_are_ambiguous(cli: Cli) -> None:
    swap(
        cli,
        _createmeta_types=lambda call, project: Out(
            200, {"total": 2, "issueTypes": [type_entry("1", "Task"), type_entry("4", "task")]}
        ),
    )
    code, env = cli.types("--project", "DEMO", "--type", "TASK")
    assert (code, env["error"]["reason"]) == (22, "invalid_value")
    code, doc = cli.types("--project", "DEMO", "--type", "task")
    assert code == 0 and [t["id"] for t in doc["types"]] == ["4"]


# ---- the project -------------------------------------------------------------------------------


def test_the_sites_only_project_is_the_default(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: Any, fake: FakeProvider
) -> None:
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    monkeypatch.setenv(SITE_ENV_VAR, SITE_TOKEN)
    one = Cli(tmp_path, fake, capsys, projects="    projects: [DEMO]\n")
    code, doc = one.types()
    assert code == 0 and doc["project"] == "DEMO"


def test_no_project_and_no_single_default_is_missing_option(cli: Cli) -> None:
    code, env = cli.types()
    assert (code, env["error"]["reason"]) == (2, "missing_option")
    assert not cli.fake.calls


@pytest.mark.parametrize("project", ["demo", "DEMO/..", "A B", "", "D" * 80, "DEMO?x=1"])
def test_a_hostile_project_never_reaches_the_provider(cli: Cli, project: str) -> None:
    code, env = cli.types("--project", project)
    assert code in (2, 22), env
    assert not [c for c in cli.fake.calls if "createmeta" in c.path]


def test_a_missing_project_is_not_found(cli: Cli) -> None:
    code, env = cli.types("--project", "NOPE")
    assert (code, env["error"]["code"]) == (10, "not_found")


@pytest.mark.parametrize(
    "flags",
    [
        ("--yes",),
        ("--dry-run",),
        ("--actor", "human:a"),
        ("--decision-id", "d"),
        ("--override-escalation",),
    ],
)
def test_write_only_flags_are_refused(cli: Cli, flags: tuple[str, ...]) -> None:
    code, env = cli.doc(*flags, "fields", "types", "--project", "DEMO")
    assert code == 2, env
    assert env["error"]["reason"] == "invalid_argument"
    assert not cli.fake.calls


# ---- createmeta shapes, paging and failures (shared with create) -------------------------------


def test_the_values_shape_and_paging_are_the_ones_of_create(cli: Cli) -> None:
    pages: list[tuple[str | None, str | None]] = []

    def types(call: Any, project: str) -> Out:
        start, size = int(call.one("startAt", "0") or 0), int(call.one("maxResults", "0") or 0)
        pages.append((call.one("startAt"), call.one("maxResults")))
        everything = [type_entry(str(1000 + i), f"T{i:03}") for i in range(449)]
        everything.append(type_entry("4", "Last"))
        return Out(
            200,
            {
                "startAt": start,
                "maxResults": size,
                "total": 450,
                "values": everything[start : start + size],
            },
        )

    swap(cli, _createmeta_types=types)
    code, doc = cli.types("--project", "DEMO")
    assert code == 0 and len(doc["types"]) == 450
    assert pages == [("0", "200"), ("200", "200"), ("400", "200")]


def test_a_never_ending_type_list_is_provider_changed(cli: Cli) -> None:
    swap(
        cli,
        _createmeta_types=lambda call, project: Out(
            200, {"issueTypes": [type_entry(str(1000 + len(cli.fake.calls)), "X")], "total": 10**6}
        ),
    )
    code, env = cli.types("--project", "DEMO")
    assert (code, env["error"]["code"]) == (12, "provider_changed")
    assert len([c for c in cli.fake.calls if c.path.endswith("/issuetypes")]) == createcmd.MAX_PAGES


def test_entries_without_a_usable_id_or_name_are_left_out(cli: Cli) -> None:
    bad: list[Any] = [
        "text",
        {"id": "x", "name": "Bad id"},
        {"id": "9", "name": ""},
        {"id": "7", "name": "Good"},
    ]
    swap(cli, _createmeta_types=lambda call, project: Out(200, {"total": 4, "issueTypes": bad}))
    code, doc = cli.types("--project", "DEMO")
    assert code == 0 and [t["name"] for t in doc["types"]] == ["Good"]
    assert doc["types"][0]["subtask"] is None  # the provider said nothing


def test_a_non_boolean_subtask_flag_is_null(cli: Cli) -> None:
    swap(
        cli,
        _createmeta_types=lambda call, project: Out(
            200, {"total": 1, "issueTypes": [{"id": "1", "name": "Task", "subtask": "yes"}]}
        ),
    )
    _, doc = cli.types("--project", "DEMO")
    assert doc["types"][0]["subtask"] is None


def test_type_names_are_sanitized_and_capped(cli: Cli) -> None:
    long_name = "N" * 400
    swap(
        cli,
        _createmeta_types=lambda call, project: Out(
            200,
            {
                "total": 2,
                "issueTypes": [type_entry("1", "Ta\x1b[31msk‮"), type_entry("2", long_name)],
            },
        ),
    )
    _, doc = cli.types("--project", "DEMO")
    validator("cli/fields-types.json").validate(doc)
    for t in doc["types"]:
        assert "\x1b" not in t["name"] and "‮" not in t["name"]
        assert len(t["name"]) <= 256


def test_a_provider_field_id_in_a_required_name_is_hidden(cli: Cli) -> None:
    with_fields(cli, **{"customfield_90001": {"name": "customfield_90005", "required": True}})
    _, doc = cli.types("--project", "DEMO", "--type", "Task")
    assert "customfield_" not in json.dumps(doc)


def test_a_404_on_the_fields_call_is_not_found(cli: Cli) -> None:
    def gone(call: Any, project: str, type_id: str) -> Out:
        raise ProviderError(404, ["No issue type could be found."])

    swap(cli, _createmeta_fields=gone)
    code, env = cli.types("--project", "DEMO", "--type", "Task")
    assert (code, env["error"]["code"]) == (10, "not_found")


def test_an_unusable_fields_answer_is_provider_changed(cli: Cli) -> None:
    swap(cli, _createmeta_fields=lambda call, project, type_id: Out(200, {"nothing": 1}))
    code, env = cli.types("--project", "DEMO", "--type", "Task")
    assert (code, env["error"]["code"]) == (12, "provider_changed")


def test_a_provider_outage_is_unavailable(cli: Cli) -> None:
    swap(
        cli,
        _createmeta_types=lambda call, project: (_ for _ in ()).throw(ProviderError(503, ["down"])),
    )
    code, env = cli.types("--project", "DEMO")
    assert (code, env["error"]["code"]) == (12, "unavailable")


# ---- the same rules in both commands (create and fields types) ---------------------------------

BOTH = pytest.mark.parametrize("cmd", ["create", "types"])


def invoke(cli: Cli, cmd: str, type_name: str) -> tuple[int, dict[str, Any], str]:
    """One run of ``create --dry-run`` or ``fields types`` for the type; its code, doc, stderr."""
    if cmd == "create":
        argv = ["--dry-run", "create", "--project", "DEMO", "--type", type_name, "--title", "t"]
    else:
        argv = ["fields", "types", "--project", "DEMO", "--type", type_name]
    code, out, err = cli.run("--json", *argv)
    return code, json.loads(out), err


def required_names(cmd: str, code: int, doc: dict[str, Any]) -> list[str]:
    """The required field names the command reported for the one type (none when it needs none)."""
    if cmd == "create":
        if code == 0:
            return []
        assert doc["error"]["reason"] == "missing_required_fields", doc
        return [r["name"] for r in doc["error"]["details"]["required_fields"]]
    assert code == 0, doc
    return [r["name"] for r in doc["types"][0]["required_fields"]]


@BOTH
def test_more_types_than_the_limit_is_provider_changed_and_exactly_the_limit_is_fine(
    cli: Cli, monkeypatch: pytest.MonkeyPatch, cmd: str
) -> None:
    monkeypatch.setattr(createcmd, "MAX_TYPES", 5)

    def types(count: int) -> Any:
        entries = [type_entry("1", "Task")] + [
            type_entry(str(10 + i), f"T{i}") for i in range(count)
        ]
        return lambda call, project: Out(200, {"isLast": True, "issueTypes": entries})

    swap(cli, _createmeta_types=types(4))
    code, doc, _ = invoke(cli, cmd, "Task")
    assert code == 0, doc
    swap(cli, _createmeta_types=types(5))
    code, doc, _ = invoke(cli, cmd, "Task")
    assert (code, doc["error"]["code"]) == (12, "provider_changed")


@BOTH
def test_more_fields_than_the_limit_is_provider_changed_and_exactly_the_limit_is_fine(
    cli: Cli, monkeypatch: pytest.MonkeyPatch, cmd: str
) -> None:
    monkeypatch.setattr(createcmd, "MAX_META_FIELDS", 5)

    def fields(count: int) -> Any:
        entries = [{"fieldId": "summary", "name": "Summary", "required": True}] + [
            {"fieldId": f"f{i}", "name": f"F{i}", "required": False} for i in range(count)
        ]
        return lambda call, project, type_id: Out(200, {"isLast": True, "fields": entries})

    swap(cli, _createmeta_fields=fields(4))
    code, doc, _ = invoke(cli, cmd, "Task")
    assert code == 0, doc
    swap(cli, _createmeta_fields=fields(5))
    code, doc, _ = invoke(cli, cmd, "Task")
    assert (code, doc["error"]["code"]) == (12, "provider_changed")


@BOTH
def test_a_required_field_named_like_an_internal_id_is_shown_hidden(cli: Cli, cmd: str) -> None:
    with_fields(cli, **{"customfield_90001": {"name": "customfield_90002", "required": True}})
    code, doc, _ = invoke(cli, cmd, "Task")
    (name,) = required_names(cmd, code, doc)
    assert name == hide_field_ids("customfield_90002") and name != "customfield_90002"
    assert "customfield_" not in json.dumps(doc)


@BOTH
def test_a_nameless_required_field_warns_that_the_list_is_incomplete(cli: Cli, cmd: str) -> None:
    with_fields(cli, **{"customfield_90003": {"required": True}})
    code, doc, err = invoke(cli, cmd, "Task")
    assert required_names(cmd, code, doc)
    assert re.search(r"the required fields could not be read completely \(required_fields\)", err)


@BOTH
def test_an_unknown_type_names_at_most_max_candidates_types(cli: Cli, cmd: str) -> None:
    entries = [type_entry(str(100 + i), f"T{i:03}") for i in range(createcmd.MAX_CANDIDATES + 10)]
    swap(
        cli,
        _createmeta_types=lambda call, project: Out(
            200, {"total": len(entries), "issueTypes": entries}
        ),
    )
    code, doc, _ = invoke(cli, cmd, "Nope")
    assert (code, doc["error"]["reason"]) == (22, "invalid_value")
    candidates = doc["error"]["details"]["candidates"]
    assert candidates == sorted(candidates) and len(candidates) == createcmd.MAX_CANDIDATES


@BOTH
def test_a_padded_type_name_is_not_stripped(cli: Cli, cmd: str) -> None:
    code, doc, _ = invoke(cli, cmd, " Bug")
    assert (code, doc["error"]["reason"]) == (22, "invalid_value")


@BOTH
@pytest.mark.parametrize("flag", ["true", 1])
def test_a_non_boolean_required_flag_is_not_required(cli: Cli, cmd: str, flag: object) -> None:
    with_fields(cli, **{F_POINTS: {"name": "Story Points", "required": flag}})
    code, doc, _ = invoke(cli, cmd, "Task")
    assert required_names(cmd, code, doc) == []


# ---- read-only ---------------------------------------------------------------------------------


def test_the_command_only_reads_and_leaves_no_state(cli: Cli, tmp_path: Path) -> None:
    code, _ = cli.types("--project", "DEMO", "--type", "Task")
    assert code == 0
    assert cli.writes() == []
    state = tmp_path / "state" / "ticketctl"
    assert not (state / "audit.jsonl").exists()
    assert not list(state.glob("**/*.lock")) if state.exists() else True


def test_the_text_form_lists_one_line_per_type(cli: Cli) -> None:
    code, out, _ = cli.run("fields", "types", "--project", "DEMO")
    assert code == 0 and "Task" in out and "Sub-task" in out
    code, out, _ = cli.run("fields", "types", "--project", "DEMO", "--type", "Task")
    assert code == 0 and "Task" in out
