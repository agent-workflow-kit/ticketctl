"""``resolution-backfill`` end to end against the fake provider (issue #48, design D-45)."""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from tests import fake_provider
from tests.fake_provider import SITE_TOKEN, FakeProvider, Fault, Quirks
from tests.golden import assert_golden, validator
from tests.test_comment import ATTEMPT, SITE_ENV_VAR, Cli
from ticketctl import hooks, resolutioncmd
from ticketctl.fieldmap import MAX_CANDIDATES
from ticketctl.gateway import HookRun
from ticketctl.writeflow import Seams

KEY = "DEMO-7"
MARK = "zq7x-resolution-marker-4f9c2e"  # a ticket summary: must never reach a hook payload


@pytest.fixture
def cli(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: Any,
    fake_factory: Callable[[Quirks | None], FakeProvider],
) -> Cli:
    """A site whose edit screen lists the resolution, and one done ticket with none."""
    fake = fake_factory(Quirks(resolution_on_edit_screen=True))
    fake.add_ticket(KEY, MARK, status="Done", resolution=None)
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    monkeypatch.setenv(SITE_ENV_VAR, SITE_TOKEN)
    monkeypatch.setenv("PATH", "/nonexistent")
    monkeypatch.setattr(resolutioncmd, "make_seams", lambda: Seams(attempt_id=lambda: ATTEMPT))
    return Cli(tmp_path, fake, capsys)


def backfill(cli: Cli, *argv: str, key: str = KEY) -> tuple[int, dict[str, Any]]:
    """Plan with --dry-run, then apply that plan with --yes."""
    code, plan = cli.doc("--dry-run", "resolution-backfill", key, *argv)
    if code != 0:
        return code, plan
    return cli.doc("--yes", "--expect-plan", plan["plan_sha256"], "resolution-backfill", key, *argv)


def stored(cli: Cli, key: str = KEY) -> str | None:
    return cli.fake.tickets[key].resolution


def with_default(cli: Cli, name: str = "Done") -> None:
    extra = f"    transitions:\n      default_resolution: {name}\n"
    cli.config.write_text(cli.config.read_text() + extra)


# --- the write -------------------------------------------------------------------------------


def test_sets_the_resolution_with_one_put_and_leaves_the_status_alone(cli: Cli) -> None:
    code, result = backfill(cli, "--resolution", "Won't Do")
    assert code == 0, result
    assert result["action"] == "resolution_backfill" and result["result"] == "ok"
    assert result["changed"] is True and result["comment_id"] is None
    assert result["steps"] == [{"kind": "set_resolution", "status": "done"}]
    assert result["attempt_id"] == ATTEMPT and "attachment_id" not in result
    validator("cli/resolution-backfill.json").validate(result)
    assert_golden("cli", "resolution-backfill", "ok", cli.norm(result))
    (write,) = cli.writes()
    assert (write.method, write.path) == ("PUT", f"/rest/api/3/issue/{KEY}")
    assert write.body == {"fields": {"resolution": {"name": "Won't Do"}}}
    assert "notifyUsers" not in (write.query or "")
    assert stored(cli) == "Won't Do"
    assert cli.fake.tickets[KEY].status == "Done"  # never a status change
    assert not any("transitions" in c.path and c.method != "GET" for c in cli.fake.calls)


def test_dry_run_is_a_plan_with_one_resolution_change_and_sends_nothing(cli: Cli) -> None:
    code, plan = cli.doc("--dry-run", "resolution-backfill", KEY, "--resolution", "Done")
    assert code == 0 and plan["action"] == "resolution_backfill" and plan["key"] == KEY
    assert plan["steps"] == [{"kind": "set_resolution", "resolution": "Done"}]
    assert plan["changes"] == [{"name": "resolution", "from": None, "to": "Done"}]
    assert plan["warnings"] == [] and plan["dedupe_key"] is None
    validator("cli/resolution-backfill.json").validate(plan)
    assert_golden("cli", "resolution-backfill", "dry-run", cli.norm(plan))
    assert cli.writes() == [] and stored(cli) is None


def test_the_name_matches_case_insensitively_and_is_sent_in_the_providers_spelling(
    cli: Cli,
) -> None:
    code, result = backfill(cli, "--resolution", "won't do")
    assert code == 0, result
    assert cli.writes()[0].body["fields"]["resolution"] == {"name": "Won't Do"}


def test_the_site_default_is_used_without_the_option(cli: Cli) -> None:
    with_default(cli, "Duplicate")
    code, result = backfill(cli)
    assert code == 0, result
    assert stored(cli) == "Duplicate"


def test_the_option_beats_the_site_default(cli: Cli) -> None:
    with_default(cli, "Duplicate")
    assert backfill(cli, "--resolution", "Declined")[0] == 0
    assert stored(cli) == "Declined"


def test_no_name_and_no_default_is_needs_resolution_and_sends_nothing(cli: Cli) -> None:
    code, env = cli.doc("--dry-run", "resolution-backfill", KEY)
    assert (code, env["error"]["code"], env["error"]["reason"]) == (
        22,
        "invalid_request",
        "needs_resolution",
    )
    assert cli.writes() == []


def test_needs_confirmation_without_yes(cli: Cli) -> None:
    code, env = cli.doc("resolution-backfill", KEY, "--resolution", "Done")
    assert code == 13 and env["error"]["code"] == "confirmation_required"
    assert stored(cli) is None


def test_yes_needs_expect_plan_and_no_dedupe_key(cli: Cli) -> None:
    code, env = cli.doc("--yes", "resolution-backfill", KEY, "--resolution", "Done")
    assert (code, env["error"]["reason"]) == (2, "yes_requires_expect_plan")
    assert cli.doc("resolution-backfill", KEY, "--resolution", "Done", "--dedupe-key", "k")[0] == 2


# --- idempotency: a resolution is never replaced -------------------------------------------------


def test_a_ticket_with_a_resolution_is_unchanged_and_the_requested_one_is_ignored(
    cli: Cli,
) -> None:
    code, result = backfill(cli, "--resolution", "Duplicate", key="DEMO-3")  # resolved: Done
    assert code == 0, result
    assert result["result"] == "unchanged" and result["changed"] is False
    assert result["steps"] == [] and result["attempt_id"] is None
    validator("cli/resolution-backfill.json").validate(result)
    assert_golden("cli", "resolution-backfill", "unchanged", cli.norm(result))
    assert cli.writes() == [] and stored(cli, "DEMO-3") == "Done"


def test_an_unchanged_run_needs_no_resolution_name_and_reads_no_editmeta(cli: Cli) -> None:
    code, result = cli.doc("--dry-run", "resolution-backfill", "DEMO-3")
    assert code == 0 and result["steps"] == [], result
    assert not any(c.path.endswith("/editmeta") for c in cli.fake.calls)


def test_a_ticket_that_is_not_done_but_has_a_resolution_is_unchanged(cli: Cli) -> None:
    cli.fake.tickets["DEMO-2"].resolution = "Done"  # in progress, resolved by hand
    code, result = backfill(cli, "--resolution", "Done", key="DEMO-2")
    assert code == 0 and result["result"] == "unchanged"


def test_a_rerun_after_the_write_landed_is_unchanged(cli: Cli) -> None:
    assert backfill(cli, "--resolution", "Done")[0] == 0
    before = len(cli.writes())
    code, again = backfill(cli, "--resolution", "Duplicate")
    assert code == 0 and again["result"] == "unchanged" and len(cli.writes()) == before
    assert stored(cli) == "Done"


def test_a_resolution_set_by_someone_else_since_the_plan_makes_the_write_unchanged(
    cli: Cli,
) -> None:
    _, plan = cli.doc("--dry-run", "resolution-backfill", KEY, "--resolution", "Done")
    cli.fake.tickets[KEY].resolution = "Declined"
    code, result = cli.doc(
        "--yes", "--expect-plan", plan["plan_sha256"], "resolution-backfill", KEY,
        "--resolution", "Done",
    )  # fmt: skip
    assert code == 0 and result["result"] == "unchanged"
    assert cli.writes() == [] and stored(cli) == "Declined"


# --- the checks that matter only while a step remains --------------------------------------------


@pytest.mark.parametrize("status", ["Backlog", "In Progress"])
def test_a_ticket_that_is_not_done_is_a_precondition_failure(cli: Cli, status: str) -> None:
    cli.fake.tickets[KEY].status = status
    code, env = cli.doc("--dry-run", "resolution-backfill", KEY, "--resolution", "Done")
    assert (code, env["error"]["code"]) == (20, "precondition_failed"), env
    assert env["error"]["details"] == {"current_status": status}
    validator("cli/error.json").validate(env)
    assert cli.writes() == [] and stored(cli) is None
    assert not any(c.path.endswith("/editmeta") for c in cli.fake.calls)


def test_a_move_to_another_done_status_since_the_plan_is_stale(
    cli: Cli, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setitem(fake_provider.STATUSES, "Closed", ("13", "done"))
    _, plan = cli.doc("--dry-run", "resolution-backfill", KEY, "--resolution", "Done")
    cli.fake.tickets[KEY].status = "Closed"  # still done, but another status
    code, env = cli.doc(
        "--yes", "--expect-plan", plan["plan_sha256"], "resolution-backfill", KEY,
        "--resolution", "Done",
    )  # fmt: skip
    assert (code, env["error"]["code"]) == (20, "stale_ticket"), env
    assert cli.writes() == [] and stored(cli) is None


def test_a_ticket_reopened_since_the_plan_is_a_precondition_failure(cli: Cli) -> None:
    _, plan = cli.doc("--dry-run", "resolution-backfill", KEY, "--resolution", "Done")
    cli.fake.tickets[KEY].status = "Backlog"  # reopened between plan and apply
    code, env = cli.doc(
        "--yes", "--expect-plan", plan["plan_sha256"], "resolution-backfill", KEY,
        "--resolution", "Done",
    )  # fmt: skip
    assert (code, env["error"]["code"]) == (20, "precondition_failed"), env
    assert cli.writes() == [] and stored(cli) is None


def test_an_edit_screen_without_the_resolution_is_not_editable_and_nothing_else_is_tried(
    cli: Cli,
) -> None:
    cli.fake.quirks.resolution_on_edit_screen = False
    code, env = cli.doc("--dry-run", "resolution-backfill", KEY, "--resolution", "Done")
    assert (code, env["error"]["code"], env["error"]["reason"]) == (
        22,
        "invalid_request",
        "not_editable",
    )
    assert env["error"]["details"] == {"field": "Resolution"}
    assert cli.writes() == [] and stored(cli) is None
    assert not any("transitions" in c.path for c in cli.fake.calls)


def test_an_edit_screen_entry_without_the_set_operation_is_not_editable(
    cli: Cli, monkeypatch: pytest.MonkeyPatch
) -> None:
    original = cli.fake._editable_fields

    def read_only_screen() -> dict[str, dict[str, Any]]:
        fields = original()
        fields["resolution"]["operations"] = ["add"]
        return fields

    monkeypatch.setattr(cli.fake, "_editable_fields", read_only_screen)
    code, env = cli.doc("--dry-run", "resolution-backfill", KEY, "--resolution", "Done")
    assert (code, env["error"]["reason"]) == (22, "not_editable")


def test_a_name_the_screen_does_not_list_is_invalid_with_sorted_candidates(cli: Cli) -> None:
    code, env = cli.doc("--dry-run", "resolution-backfill", KEY, "--resolution", "Whatever")
    assert (code, env["error"]["reason"]) == (22, "invalid_value")
    assert env["error"]["details"]["candidates"] == sorted(
        ["Done", "Won't Do", "Duplicate", "Cannot Reproduce", "Declined"]
    )
    assert cli.writes() == []


def test_without_listed_allowed_values_the_providers_resolution_list_decides(cli: Cli) -> None:
    cli.fake.quirks.resolution_allowed_values = False
    code, env = cli.doc("--dry-run", "resolution-backfill", KEY, "--resolution", "Nope")
    assert (code, env["error"]["reason"]) == (22, "invalid_value")
    assert any(c.path.endswith("/resolution") for c in cli.fake.calls)
    code, result = backfill(cli, "--resolution", "duplicate")
    assert code == 0 and stored(cli) == "Duplicate", result


def test_an_edit_screen_entry_without_an_operations_key_is_not_editable(
    cli: Cli, monkeypatch: pytest.MonkeyPatch
) -> None:
    original = cli.fake._editable_fields

    def silent_screen() -> dict[str, dict[str, Any]]:
        fields = original()
        del fields["resolution"]["operations"]
        return fields

    monkeypatch.setattr(cli.fake, "_editable_fields", silent_screen)
    code, env = cli.doc("--dry-run", "resolution-backfill", KEY, "--resolution", "Done")
    assert (code, env["error"]["reason"]) == (22, "not_editable")
    assert cli.writes() == []


def test_an_empty_allowed_values_list_counts_as_not_listed(
    cli: Cli, monkeypatch: pytest.MonkeyPatch
) -> None:
    original = cli.fake._editable_fields

    def empty_list() -> dict[str, dict[str, Any]]:
        fields = original()
        fields["resolution"]["allowedValues"] = []
        return fields

    monkeypatch.setattr(cli.fake, "_editable_fields", empty_list)
    code, result = backfill(cli, "--resolution", "duplicate")
    assert code == 0 and stored(cli) == "Duplicate", result
    assert any(c.path.endswith("/resolution") for c in cli.fake.calls)


def test_a_not_done_ticket_without_a_name_is_a_precondition_failure_first(cli: Cli) -> None:
    cli.fake.tickets[KEY].status = "Backlog"
    code, env = cli.doc("--dry-run", "resolution-backfill", KEY)
    assert (code, env["error"]["code"]) == (20, "precondition_failed"), env


def test_the_candidate_list_is_capped(cli: Cli) -> None:
    cli.fake.quirks.resolution_allowed_values = False
    cli.fake.resolution_list_body = [{"id": str(i), "name": f"R{i:03}"} for i in range(80)]
    code, env = cli.doc("--dry-run", "resolution-backfill", KEY, "--resolution", "Nope")
    assert (code, env["error"]["reason"]) == (22, "invalid_value")
    assert len(env["error"]["details"]["candidates"]) == MAX_CANDIDATES


def test_two_names_that_differ_only_in_case_are_ambiguous(cli: Cli) -> None:
    cli.fake.quirks.resolution_allowed_values = False
    cli.fake.resolution_list_body = [{"id": "1", "name": "Fixed"}, {"id": "2", "name": "FIXED"}]
    code, env = cli.doc("--dry-run", "resolution-backfill", KEY, "--resolution", "fixed")
    assert (code, env["error"]["reason"]) == (22, "invalid_value")
    assert env["error"]["details"]["candidates"] == ["FIXED", "Fixed"]


@pytest.mark.parametrize("bad", ["", " ", "a\nb", "x" * 257, "bell\x07"])
def test_a_malformed_name_is_refused_before_any_request(cli: Cli, bad: str) -> None:
    code, env = cli.doc("--dry-run", "resolution-backfill", KEY, "--resolution", bad)
    assert code == 2, env
    assert not any(c.path.startswith("/rest/api/3/issue/") for c in cli.fake.calls)


def test_a_too_long_site_default_is_a_config_error_before_any_request(cli: Cli) -> None:
    with_default(cli, "'" + "x" * 257 + "'")
    code, env = cli.doc("--dry-run", "resolution-backfill", KEY)
    assert (code, env["error"]["code"]) == (2, "invalid_config"), env
    assert cli.writes() == [] and cli.fake.calls == []


@pytest.mark.parametrize("name", ["Done\u200b", "Do\u200bne"])
def test_a_site_default_that_changes_when_sanitized_is_invalid_value_before_editmeta(
    cli: Cli, name: str
) -> None:
    with_default(cli, f'"{name}"')
    code, env = cli.doc("--dry-run", "resolution-backfill", KEY)
    assert (code, env["error"]["code"], env["error"]["reason"]) == (
        22,
        "invalid_request",
        "invalid_value",
    ), env
    assert not any(c.path.endswith("/editmeta") for c in cli.fake.calls)
    assert cli.writes() == []


@pytest.mark.parametrize("key", ["", "demo", "DEMO-7/../x", "DEMO-7\n", "ＤＥＭＯ-7", "x" * 5000])
def test_a_malformed_key_is_refused_before_any_ticket_read(cli: Cli, key: str) -> None:
    code, env = cli.doc("--dry-run", "resolution-backfill", key, "--resolution", "Done")
    assert code in (2, 22), env
    assert not any(c.path.startswith("/rest/api/3/issue") for c in cli.fake.calls)


def test_a_missing_ticket_is_not_found(cli: Cli) -> None:
    code, env = cli.doc("--dry-run", "resolution-backfill", "DEMO-999", "--resolution", "Done")
    assert (code, env["error"]["code"]) == (10, "not_found")


def test_a_secret_in_the_name_is_refused(cli: Cli) -> None:
    code, env = cli.doc("--dry-run", "resolution-backfill", KEY, "--resolution", SITE_TOKEN)
    assert code == 22 and env["error"]["reason"] == "secret_in_body", env
    assert SITE_TOKEN not in json.dumps(env)


# --- unreadable provider data --------------------------------------------------------------------


def _alter(
    cli: Cli, monkeypatch: pytest.MonkeyPatch, mutate: Callable[[dict[str, Any]], None]
) -> None:
    original = cli.fake._issue_json

    def issue_json(*a: Any, **k: Any) -> dict[str, Any]:
        doc = original(*a, **k)
        mutate(doc["fields"])
        return doc

    monkeypatch.setattr(cli.fake, "_issue_json", issue_json)


@pytest.mark.parametrize(
    "mutate",
    [
        lambda f: f.pop("resolution", None),
        lambda f: f.update(resolution="Done"),
        lambda f: f.update(resolution={}),
        lambda f: f.update(resolution={"name": ""}),
        lambda f: f.update(resolution={"name": 7}),
        lambda f: f.update(resolution=[]),
        lambda f: f.pop("status", None),
        lambda f: f.update(status={"statusCategory": {"key": "done"}}),
    ],
)
def test_an_unreadable_resolution_or_status_is_provider_changed_never_none(
    cli: Cli, monkeypatch: pytest.MonkeyPatch, mutate: Callable[[dict[str, Any]], None]
) -> None:
    _alter(cli, monkeypatch, mutate)
    code, env = cli.doc("--dry-run", "resolution-backfill", KEY, "--resolution", "Done")
    assert (code, env["error"]["code"]) == (12, "provider_changed")
    assert cli.writes() == []


def test_an_unknown_status_category_is_not_done(cli: Cli, monkeypatch: pytest.MonkeyPatch) -> None:
    def undefined(f: dict[str, Any]) -> None:
        f["status"]["statusCategory"]["key"] = "undefined"

    _alter(cli, monkeypatch, undefined)
    code, env = cli.doc("--dry-run", "resolution-backfill", KEY, "--resolution", "Done")
    assert (code, env["error"]["code"]) == (20, "precondition_failed")


# --- provider failures --------------------------------------------------------------------------


def test_a_lost_answer_is_outcome_unknown_and_a_rerun_is_safe(cli: Cli) -> None:
    cli.fake.inject(Fault("reset", method="PUT", path=f"/issue/{KEY}", after_apply=True))
    code, env = backfill(cli, "--resolution", "Done")
    assert (code, env["error"]["code"]) == (12, "outcome_unknown")
    assert env["error"]["retryable"] is True and env["error"]["outcome_unknown"] is True
    code, again = backfill(cli, "--resolution", "Done")
    assert code == 0 and again["result"] == "unchanged"
    assert stored(cli) == "Done"


@pytest.mark.parametrize("status", [400, 403, 404])
def test_a_provider_refusal_maps_to_its_code_and_changes_nothing(cli: Cli, status: int) -> None:
    cli.fake.inject(Fault("status", status=status, method="PUT", path=f"/issue/{KEY}"))
    code, env = backfill(cli, "--resolution", "Done")
    assert code in (10, 12), env
    validator("cli/error.json").validate(env)
    assert stored(cli) is None


def test_a_workflow_that_refuses_the_value_is_rejected_with_the_field_errors(cli: Cli) -> None:
    # the edit screen lists the name but the provider's own check still says no
    cli.fake.resolution_list_body = None
    cli.fake.tickets[KEY].status = "Done"
    cli.fake.inject(Fault("status", status=400, method="PUT", path=f"/issue/{KEY}"))
    code, env = backfill(cli, "--resolution", "Done")
    assert (code, env["error"]["code"]) == (12, "rejected")
    assert stored(cli) is None


# --- audit, hooks, modes ------------------------------------------------------------------------


def _audit(cli: Cli) -> list[dict[str, Any]]:
    path = cli.root / "state" / "ticketctl" / "audit.jsonl"
    return [json.loads(line) for line in path.read_text().splitlines()]


def test_the_audit_records_the_action_and_step_kind_without_ticket_text(cli: Cli) -> None:
    backfill(cli, "--resolution", "Done")
    records = _audit(cli)
    assert [(r["action"], r["step_kind"], r["result"]) for r in records] == [
        ("resolution_backfill", "set_resolution", "attempted"),
        ("resolution_backfill", "set_resolution", "ok"),
    ]
    for record in records:
        validator("audit.json").validate(record)
        assert MARK not in json.dumps(record)


def test_the_hooks_carry_the_resolution_and_no_ticket_text(
    cli: Cli, monkeypatch: pytest.MonkeyPatch
) -> None:
    cli.config.write_text(
        cli.config.read_text()
        + "hooks:\n  pre-resolution-backfill:\n    - cmd: ignored\n"
        + "  post-resolution-backfill:\n    - cmd: ignored\n"
        + "  pre-transition:\n    - cmd: other\n"
    )
    seen: list[tuple[str, bytes]] = []

    def fake_run(argv: list[str], *, stdin: bytes, env: dict[str, str], **kw: Any) -> HookRun:
        seen.append((env["TICKETCTL_HOOK"], stdin))
        assert env["TICKETCTL_ID"] == KEY
        return HookRun(1, 0, b"", b"", False, False, False)

    monkeypatch.setattr(hooks, "run_hook", fake_run)
    assert backfill(cli, "--resolution", "Won't Do")[0] == 0
    assert [n for n, _ in seen] == ["pre-resolution-backfill", "post-resolution-backfill"]
    for name, raw in seen:
        payload = json.loads(raw)
        validator(f"hooks/{name}.json").validate(payload)
        assert MARK not in raw.decode()
        assert payload["plan"]["action"] == "resolution_backfill"
        assert payload["plan"]["resolution"] == "Won't Do"
        assert payload["plan"]["current_status"] == {"name": "Done", "category": "done"}
        assert payload["project"] == "DEMO" and payload["body"] == []
    assert json.loads(seen[1][1])["provider_ids"] == []
    assert json.loads(seen[0][1])["plan"]["steps"] == ["set_resolution"]
    for name, raw in seen:
        assert_golden("hooks", name, "ok", cli.norm(json.loads(raw)))


def test_a_long_allowed_name_reaches_the_hook_payload_whole(
    cli: Cli, monkeypatch: pytest.MonkeyPatch
) -> None:
    name = "R" * 200
    monkeypatch.setattr(fake_provider, "RESOLUTIONS", [*fake_provider.RESOLUTIONS, name])
    cli.config.write_text(
        cli.config.read_text() + "hooks:\n  pre-resolution-backfill:\n    - cmd: ignored\n"
    )
    seen: list[bytes] = []

    def fake_run(argv: list[str], *, stdin: bytes, **kw: Any) -> HookRun:
        seen.append(stdin)
        return HookRun(1, 0, b"", b"", False, False, False)

    monkeypatch.setattr(hooks, "run_hook", fake_run)
    code, result = backfill(cli, "--resolution", name)
    assert code == 0, result
    (raw,) = seen
    payload = json.loads(raw)
    validator("hooks/pre-resolution-backfill.json").validate(payload)
    assert payload["plan"]["resolution"] == name
    assert cli.writes()[0].body["fields"]["resolution"] == {"name": name}


def test_an_unchanged_run_runs_no_hook(cli: Cli, monkeypatch: pytest.MonkeyPatch) -> None:
    cli.config.write_text(
        cli.config.read_text() + "hooks:\n  pre-resolution-backfill:\n    - cmd: ignored\n"
    )
    calls: list[str] = []
    monkeypatch.setattr(
        hooks,
        "run_hook",
        lambda *a, **k: calls.append("run") or HookRun(1, 0, b"", b"", False, False, False),
    )
    code, result = backfill(cli, "--resolution", "Done", key="DEMO-3")
    assert code == 0 and result["result"] == "unchanged" and calls == []


def test_a_pre_hook_veto_stops_the_write(cli: Cli, monkeypatch: pytest.MonkeyPatch) -> None:
    cli.config.write_text(
        cli.config.read_text() + "hooks:\n  pre-resolution-backfill:\n    - cmd: ignored\n"
    )
    monkeypatch.setattr(
        hooks, "run_hook", lambda *a, **k: HookRun(1, 1, b"", b"no", False, False, False)
    )
    code, env = backfill(cli, "--resolution", "Done")
    assert (code, env["error"]["code"]) == (11, "vetoed")
    assert stored(cli) is None


def test_a_hook_flag_names_the_new_family(cli: Cli) -> None:
    argv = ["--dry-run", "resolution-backfill", KEY, "--resolution", "Done"]
    assert cli.doc("--hook", "pre-resolution-backfill=exit 1", *argv)[0] == 0
    assert cli.doc("--hook", "pre-resolution-backfills=exit 1", *argv)[0] == 2


def test_read_only_mode_refuses_before_any_request(
    cli: Cli, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("TICKETCTL_READ_ONLY", "1")
    code, env = cli.doc("--yes", "--expect-plan", "0" * 64, "resolution-backfill", KEY)
    assert (code, env["error"]["code"]) == (20, "read_only")
    assert cli.fake.calls == []
    assert cli.doc("--dry-run", "resolution-backfill", KEY, "--resolution", "Done")[0] == 0


def test_an_account_mismatch_refuses_before_any_write(cli: Cli) -> None:
    cli.config.write_text(cli.config.read_text() + "me:\n  jira: acct-9999\n")
    code, env = cli.doc("--dry-run", "resolution-backfill", KEY, "--resolution", "Done")
    assert code != 0 and cli.writes() == []


def test_a_lowercase_key_and_the_site_flag_are_accepted(cli: Cli) -> None:
    code, result = backfill(cli, "--resolution", "Done", key=KEY.lower())
    assert code == 0 and result["key"] == KEY, result


def test_an_auto_actor_and_a_human_actor_plan_the_same_hash(cli: Cli) -> None:
    tail = ["--dry-run", "resolution-backfill", KEY, "--resolution", "Done"]
    a = cli.doc("--actor", "auto:bot", *tail)
    b = cli.doc("--actor", "human:me", *tail)
    assert a[1]["plan_sha256"] == b[1]["plan_sha256"]
