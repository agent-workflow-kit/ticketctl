"""``update KEY`` through the CLI against the fake provider (issue #30).

The fake is loopback http with synthetic accounts, fields and tickets. Every test runs the real
registry entry, the real write flow and the real hook runner (an explicit ``/bin/sh``; PATH is empty
in tests).
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

import pytest

from tests.fake_provider import (
    F_CRITERIA,
    F_ESTIMATE,
    F_POINTS,
    ME_ACCOUNT,
    OTHER_ACCOUNT,
    SITE_EMAIL,
    SITE_TOKEN,
    STRANGER_ACCOUNT,
    FakeProvider,
    Fault,
    Out,
    adf_doc,
    adf_paragraph,
)
from tests.golden import assert_golden, validator
from tests.test_writeflow import TOKEN_VAR
from ticketctl import hooks
from ticketctl.cli import main

SITE_URL = "https://tracker.example.test"
ATTEMPT_RE = re.compile(r"[0-9a-f]{16}")
HUMAN = ("--actor", "human:tester")


class Env:
    def __init__(self, root: Path, fake: FakeProvider, capsys: Any) -> None:
        self.root = root
        self.fake = fake
        self.capsys = capsys
        self.config = root / "config.yaml"
        self.use()

    def use(
        self, extra: str = f"fields: {{estimate: {{id: {F_POINTS}}}}}", hooks_: str = ""
    ) -> None:
        lines = [
            "sites:",
            "  main:",
            f"    url: {self.fake.url}",
            f"    email: {SITE_EMAIL}",
            f"    token_env: {TOKEN_VAR}",
            "    allow_http: true",
            f"    {extra}",
            hooks_,
        ]
        self.config.write_text("\n".join(lines) + "\n", encoding="utf-8")
        self.config.chmod(0o600)

    def file(self, name: str, text: str) -> str:
        path = self.root / name
        path.write_text(text, encoding="utf-8")
        return str(path)

    def run(self, *argv: str) -> tuple[int, str, str]:
        code = main(["--config", str(self.config), *argv])
        cap = self.capsys.readouterr()
        assert SITE_TOKEN not in cap.out + cap.err
        return code, cap.out, cap.err

    def doc(self, *argv: str) -> tuple[int, dict[str, Any]]:
        code, out, _ = self.run("--json", *argv)
        return code, json.loads(out)

    def plan(self, *argv: str) -> dict[str, Any]:
        code, doc = self.doc("--dry-run", "update", *argv)
        assert code == 0, doc
        return doc

    def apply(self, *argv: str, actor: tuple[str, ...] = ()) -> tuple[int, dict[str, Any]]:
        held = self.plan(*argv) if not actor else self.plan_as(actor, *argv)
        return self.doc(*actor, "--yes", "--expect-plan", held["plan_sha256"], "update", *argv)

    def plan_as(self, actor: tuple[str, ...], *argv: str) -> dict[str, Any]:
        code, doc = self.doc(*actor, "--dry-run", "update", *argv)
        assert code == 0, doc
        return doc

    def norm(self, doc: dict[str, Any]) -> dict[str, Any]:
        return json.loads(json.dumps(doc).replace(self.fake.url, SITE_URL))

    def writes(self) -> list[Any]:
        return [c for c in self.fake.calls if c.method in ("PUT", "POST")]


@pytest.fixture
def env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: Any, fake: FakeProvider) -> Env:
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    monkeypatch.setenv(TOKEN_VAR, SITE_TOKEN)
    return Env(tmp_path, fake, capsys)


# ---- the command line --------------------------------------------------------------------------


def test_no_change_flag_is_exit_2_missing_option(env: Env) -> None:
    code, doc = env.doc("update", "DEMO-1")
    assert (code, doc["error"]["code"], doc["error"]["reason"]) == (2, "usage", "missing_option")
    assert env.fake.calls == []


def test_title_and_title_file_together_are_refused(env: Env) -> None:
    path = env.file("t.txt", "x\n")
    code, doc = env.doc("update", "DEMO-1", "--title", "a", "--title-file", path)
    assert (code, doc["error"]["reason"]) == (2, "invalid_combination")


def test_there_is_no_body_file_on_update(env: Env) -> None:
    code, doc = env.doc("update", "DEMO-1", "--body-file", env.file("b.md", "x"))
    assert (code, doc["error"]["reason"]) == (2, "invalid_argument")


@pytest.mark.parametrize("value", ["ME", "None", "has space", "x" * 129, ""])
def test_a_bad_assign_value_is_a_usage_error(env: Env, value: str) -> None:
    code, doc = env.doc("update", "DEMO-1", "--assign", value)
    assert (code, doc["error"]["reason"]) == (2, "invalid_argument")
    assert env.fake.calls == []


def test_dedicated_names_are_refused_and_nothing_is_written(env: Env) -> None:
    for name in ("title", "labels", "assignee", "sprint"):
        code, doc = env.doc("update", "DEMO-1", "--set", f"{name}=x")
        assert (code, doc["error"]["reason"]) == (2, "invalid_combination"), name
    assert env.fake.calls == []
    # a display name is judged on the resolved field, so it needs the metadata first
    for name in ("Labels", "Summary", "Assignee", "Sprint", "Reporter"):
        code, doc = env.doc("update", "DEMO-1", "--set", f"{name}=x")
        assert (code, doc["error"]["reason"]) == (2, "invalid_combination"), name
    assert env.writes() == []


# ---- values refused before the first provider call ---------------------------------------------


def refused(env: Env, *argv: str, reason: str = "invalid_value") -> dict[str, Any]:
    code, doc = env.doc("update", "DEMO-1", *argv)
    assert (code, doc["error"]["code"], doc["error"]["reason"]) == (22, "invalid_request", reason)
    assert env.fake.calls == []  # prepare makes no provider call
    return doc


def test_the_same_label_in_add_and_remove_is_invalid_value(env: Env) -> None:
    refused(env, "--add-label", "x", "--remove-label", "x")


@pytest.mark.parametrize(
    "label", ["has space", "tc-0123456789abcdef", "a" * 256, "tab\tx", "zero​width"]
)
def test_a_bad_label_is_invalid_value(env: Env, label: str) -> None:
    refused(env, "--add-label", label)
    refused(env, "--remove-label", label)


@pytest.mark.parametrize("title", ["", "   ", "x" * 256, "two\nlines", "bell\x07"])
def test_a_bad_title_is_invalid_value(env: Env, title: str) -> None:
    refused(env, "--title", title)


def test_a_title_of_255_characters_is_accepted(env: Env) -> None:
    plan = env.plan("DEMO-1", "--title", "é" * 255)
    assert [s["kind"] for s in plan["steps"]] == ["set_fields"]


def test_a_title_file_loses_its_line_end_and_surrounding_space(env: Env) -> None:
    path = env.file("t.txt", "  A new title  \n")
    plan = env.plan("DEMO-1", "--title-file", path)
    assert plan["changes"] == [
        {"name": "title", "from": "Seeded backlog task", "to": "A new title"}
    ]


def test_a_title_file_of_blank_text_is_refused(env: Env) -> None:
    refused(env, "--title-file", env.file("t.txt", "\n"))


def test_a_secret_in_the_title_or_a_label_is_refused(env: Env) -> None:
    refused(env, "--title", f"leak {SITE_TOKEN}", reason="secret_in_body")
    refused(env, "--add-label", SITE_TOKEN, reason="secret_in_body")


def test_a_secret_as_the_assignee_is_refused(env: Env) -> None:
    refused(env, "--assign", SITE_TOKEN, reason="secret_in_body")


def test_a_secret_as_the_assignee_runs_no_hook_and_sends_nothing(env: Env) -> None:
    out = env.root / "hook-ran"
    env.use(hooks_=f"hooks:\n  pre-update:\n    - cmd: ': > {out}'")
    refused(env, "--assign", SITE_TOKEN, reason="secret_in_body")
    assert not out.exists()
    assert env.writes() == []


# ---- the plan ----------------------------------------------------------------------------------

ALL = (
    "DEMO-1",
    "--title",
    "Renamed task",
    "--assign",
    "me",
    "--add-label",
    "beta",
    "--remove-label",
    "alpha",
    "--set",
    "estimate=5",
)


def test_the_step_order_is_set_fields_first_then_assign_and_the_plan_golden(env: Env) -> None:
    plan = env.plan(*ALL)
    assert [s["kind"] for s in plan["steps"]] == ["set_fields", "assign"]
    assert plan["steps"][0]["fields"] == ["title", "estimate", "labels"]
    assert plan["steps"][1] == {"kind": "assign", "account": "me"}
    assert plan["warnings"] == []
    by_name = {c["name"]: c for c in plan["changes"]}
    assert by_name["labels"] == {"name": "labels", "from": ["alpha"], "to": ["beta"]}
    assert by_name["estimate"] == {"name": "estimate", "from": None, "to": 5}
    assert by_name["assignee"] == {"name": "assignee", "from": None, "to": ME_ACCOUNT}
    validator("cli/update.json").validate(plan)
    assert_golden("cli", "update", "dry-run", env.norm(plan))
    assert env.writes() == []  # a dry-run writes nothing


def test_the_plan_hash_is_stable_and_covers_the_values(env: Env) -> None:
    first = env.plan(*ALL)["plan_sha256"]
    assert env.plan(*ALL)["plan_sha256"] == first
    changed = env.plan(*ALL[:-1], "estimate=6")["plan_sha256"]
    assert changed != first


def test_the_hash_facts_are_the_current_values(env: Env) -> None:
    before = env.plan("DEMO-1", "--title", "Renamed task")["plan_sha256"]
    env.fake.tickets["DEMO-1"].summary = "Edited meanwhile"
    assert env.plan("DEMO-1", "--title", "Renamed task")["plan_sha256"] != before


def test_a_label_fact_is_whether_the_label_is_present(env: Env) -> None:
    before = env.plan("DEMO-1", "--add-label", "beta")["plan_sha256"]
    env.fake.tickets["DEMO-1"].labels["beta"] = env.fake.now
    code, after = env.doc("--dry-run", "update", "DEMO-1", "--add-label", "beta")
    assert code == 0 and after["steps"] == []  # present now: nothing to do
    assert before != after["plan_sha256"]


# ---- applying ----------------------------------------------------------------------------------


def test_apply_changes_everything_in_one_request_plus_the_assign(env: Env) -> None:
    code, result = env.apply(*ALL)
    assert code == 0, result
    assert result["result"] == "ok" and result["changed"] is True
    assert [(s["kind"], s["status"]) for s in result["steps"]] == [
        ("set_fields", "done"),
        ("assign", "done"),
    ]
    assert ATTEMPT_RE.fullmatch(result["attempt_id"])
    validator("cli/update.json").validate(result)
    result["attempt_id"] = "0" * 16
    assert_golden("cli", "update", "ok", env.norm(result))
    ticket = env.fake.tickets["DEMO-1"]
    assert (ticket.summary, ticket.assignee, ticket.points) == ("Renamed task", ME_ACCOUNT, 5)
    assert sorted(ticket.labels) == ["beta"]
    puts = [c for c in env.writes() if c.method == "PUT"]
    assert [c.path.rsplit("/", 1)[-1] for c in puts] == ["DEMO-1", "assignee"]
    body = puts[0].body
    assert body["fields"] == {"summary": "Renamed task", F_POINTS: 5}
    assert body["update"] == {"labels": [{"add": "beta"}, {"remove": "alpha"}]}
    assert puts[1].body == {"accountId": ME_ACCOUNT}


def test_notify_users_is_never_sent(env: Env) -> None:
    code, result = env.apply(*ALL)
    assert code == 0, result
    assert all(c.one("notifyUsers") is None for c in env.fake.calls)
    assert all("notifyUsers" not in c.target for c in env.fake.calls)


def test_an_update_of_only_the_assignee_is_one_assign_step(env: Env) -> None:
    code, result = env.apply("DEMO-2", "--assign", OTHER_ACCOUNT)
    assert code == 0, result
    assert [s["kind"] for s in result["steps"]] == ["assign"]
    assert env.fake.tickets["DEMO-2"].assignee == OTHER_ACCOUNT


def test_assign_none_unassigns_and_is_not_refused_as_empty(env: Env) -> None:
    plan = env.plan("DEMO-2", "--assign", "none")
    assert plan["steps"] == [{"kind": "assign", "account": "none"}]
    assert plan["changes"] == [{"name": "assignee", "from": ME_ACCOUNT, "to": None}]
    code, result = env.apply("DEMO-2", "--assign", "none")
    assert code == 0 and result["result"] == "ok"
    assert env.fake.tickets["DEMO-2"].assignee is None
    assert env.writes()[-1].body == {"accountId": None}  # an explicit null, never "-1"


def test_assign_is_always_applied_even_over_another_assignee(env: Env) -> None:
    code, _ = env.apply("DEMO-3", "--assign", "me")
    assert code == 0
    assert env.fake.tickets["DEMO-3"].assignee == ME_ACCOUNT


def test_a_204_with_no_body_is_success(env: Env) -> None:
    code, result = env.apply("DEMO-1", "--add-label", "gamma")
    assert code == 0 and result["result"] == "ok"
    assert [c.status for c in env.writes()] == [204]


def test_a_dry_run_ignores_yes_and_expect_plan(env: Env) -> None:
    code, doc = env.doc("--dry-run", "--yes", "update", "DEMO-1", "--title", "Other")
    assert code == 0 and doc["steps"][0]["kind"] == "set_fields"
    assert env.writes() == []


def test_a_stale_expect_plan_is_exit_20(env: Env) -> None:
    held = env.plan("DEMO-1", "--title", "Renamed")["plan_sha256"]
    env.fake.tickets["DEMO-1"].summary = "Someone else"
    code, doc = env.doc("--yes", "--expect-plan", held, "update", "DEMO-1", "--title", "Renamed")
    assert (code, doc["error"]["code"]) == (20, "stale_ticket")
    assert doc["error"]["details"]["plan"]["plan_sha256"] != held
    assert env.writes() == []


def test_without_yes_a_run_needs_a_human(env: Env) -> None:
    code, doc = env.doc("update", "DEMO-1", "--title", "Renamed")
    assert (code, doc["error"]["code"]) == (13, "confirmation_required")
    assert env.writes() == []


# ---- unchanged ---------------------------------------------------------------------------------


def unchanged(env: Env, *argv: str) -> dict[str, Any]:
    before = len(env.writes())
    code, result = env.doc("--yes", "--expect-plan", "f" * 64, "update", *argv)
    assert code == 0, result  # idempotency comes before the hash comparison
    assert result["result"] == "unchanged" and result["changed"] is False
    assert result["steps"] == [] and result["attempt_id"] is None
    assert len(env.writes()) == before
    return result


def test_the_same_title_is_unchanged(env: Env) -> None:
    unchanged(env, "DEMO-1", "--title", "Seeded backlog task")
    unchanged(env, "DEMO-1", "--title", "  Seeded backlog task \n")


def test_adding_an_existing_label_or_removing_an_absent_one_is_unchanged(env: Env) -> None:
    result = unchanged(env, "DEMO-1", "--add-label", "alpha", "--remove-label", "nothere")
    validator("cli/update.json").validate(result)
    assert_golden("cli", "update", "unchanged", env.norm(result))


def test_the_same_assignee_is_unchanged(env: Env) -> None:
    unchanged(env, "DEMO-2", "--assign", "me")
    unchanged(env, "DEMO-2", "--assign", ME_ACCOUNT)
    unchanged(env, "DEMO-1", "--assign", "none")


def test_a_number_is_compared_as_a_decimal(env: Env) -> None:
    unchanged(env, "DEMO-2", "--set", "estimate=3")  # the ticket holds 3.0
    unchanged(env, "DEMO-2", "--set", "estimate=3.00")
    plan = env.plan("DEMO-2", "--set", "estimate=3.5")
    assert plan["changes"] == [{"name": "estimate", "from": 3, "to": 3.5}]


def test_an_empty_value_clears_and_a_cleared_field_is_unchanged(env: Env) -> None:
    unchanged(env, "DEMO-1", "--set", "estimate=")  # no points yet
    code, result = env.apply("DEMO-2", "--set", "estimate=")
    assert code == 0 and result["result"] == "ok"
    assert env.fake.tickets["DEMO-2"].points is None
    assert env.writes()[-1].body == {"fields": {F_POINTS: None}}


def test_a_date_and_an_option_are_compared_by_value(env: Env) -> None:
    code, _ = env.apply("DEMO-1", "--set", "Due date=2026-04-01", "--set", "priority=High")
    assert code == 0
    unchanged(env, "DEMO-1", "--set", "Due date=2026-04-01", "--set", "priority=High")
    assert env.fake.tickets["DEMO-1"].priority == "High"


def test_a_rerun_after_a_landed_update_is_unchanged(env: Env) -> None:
    held = env.plan(*ALL)["plan_sha256"]
    code, _ = env.doc("--yes", "--expect-plan", held, "update", *ALL)
    assert code == 0
    code, result = env.doc("--yes", "--expect-plan", held, "update", *ALL)
    assert code == 0 and result["result"] == "unchanged"


def test_an_unchanged_run_runs_no_hook_and_writes_no_audit(env: Env) -> None:
    out = env.root / "hook-ran"
    env.use(hooks_=f"hooks:\n  pre-update:\n    - cmd: ': > {out}'")
    unchanged(env, "DEMO-1", "--title", "Seeded backlog task")
    assert not out.exists()
    assert not (env.root / "state" / "ticketctl" / "audit.jsonl").exists()


# ---- rich text ---------------------------------------------------------------------------------


def test_a_description_is_set_from_a_file_and_a_repeat_is_unchanged(env: Env) -> None:
    path = env.file("d.md", "A **new** description.\n\n- one\n- two\n")
    argv = ("DEMO-1", "--set", f"description=@{path}")
    code, result = env.apply(*argv, actor=HUMAN)
    assert code == 0 and result["result"] == "ok", result
    stored = env.fake.tickets["DEMO-1"].description
    assert stored is not None and stored["type"] == "doc"
    code, again = env.doc(*HUMAN, "--yes", "--expect-plan", "0" * 64, "update", *argv)
    assert code == 0 and again["result"] == "unchanged", again
    assert len(env.writes()) == 1


def test_a_rich_value_is_compared_as_rendered_markdown_not_as_stored_json(env: Env) -> None:
    # The provider re-serializes ADF (extra attributes): equal text is still unchanged.
    env.fake.tickets["DEMO-1"].description = {
        "type": "doc",
        "version": 1,
        "content": [
            {
                "type": "paragraph",
                "attrs": {"localId": "abc"},
                "content": [{"type": "text", "text": "Same words"}],
            }
        ],
    }
    path = env.file("d.md", "Same words\n")
    code, result = env.doc(*HUMAN, "--yes", "--expect-plan", "0" * 64, "update", "DEMO-1",
                           "--set", f"description=@{path}")  # fmt: skip
    assert code == 0 and result["result"] == "unchanged", result


def test_the_rich_value_is_in_the_plan_as_rendered_markdown_and_shown(env: Env) -> None:
    env.fake.tickets["DEMO-1"].description = adf_doc(adf_paragraph("Old words"))
    path = env.file("d.md", "New words\n")
    plan = env.plan_as(HUMAN, "DEMO-1", "--set", f"description=@{path}")
    (change,) = plan["changes"]
    assert change["name"] == "description"
    assert change["from"].strip() == "Old words" and change["to"].strip() == "New words"
    assert "attribut" not in change["to"].lower()  # a field value carries no attribution


def test_description_from_stdin_is_one_stdin_source(env: Env) -> None:
    path = env.file("t.txt", "Title\n")
    code, doc = env.doc("update", "DEMO-1", "--title-file", "-", "--set", "description=-")
    assert (code, doc["error"]["reason"]) == (2, "stdin_conflict")
    code, doc = env.doc("update", "DEMO-1", "--title-file", path, "--set", "description=text")
    assert code == 22 and doc["error"]["reason"] == "invalid_value"  # inline rich text refused


def test_an_auto_actor_cannot_set_a_rich_value(env: Env) -> None:
    path = env.file("d.md", "text\n")
    code, doc = env.doc("--actor", "auto:bot", "update", "DEMO-1", "--set", f"description=@{path}")
    assert (code, doc["error"]["code"], doc["error"]["reason"]) == (
        22,
        "invalid_request",
        "invalid_value",
    )
    assert env.writes() == []


def test_a_custom_rich_field_is_set_by_its_display_name(env: Env) -> None:
    path = env.file("c.md", "Criteria text\n")
    code, result = env.apply("DEMO-1", "--set", f"Acceptance Criteria=@{path}", actor=HUMAN)
    assert code == 0, result
    assert env.fake.tickets["DEMO-1"].criteria is not None
    assert env.writes()[-1].body["fields"].keys() == {F_CRITERIA}
    plan = env.plan_as(HUMAN, "DEMO-1", "--set", f"Acceptance Criteria=@{path}")
    assert plan["steps"] == []  # now unchanged


# ---- unknown and non-editable fields ----------------------------------------------------------


def test_an_unknown_field_is_exit_22_before_anything_is_sent(env: Env) -> None:
    code, doc = env.doc("update", "DEMO-1", "--set", "No such field=1")
    assert (code, doc["error"]["reason"]) == (22, "unknown_field")
    assert env.writes() == []


def test_a_field_missing_from_editmeta_is_not_editable(env: Env) -> None:
    env.use(extra=f"fields: {{extra: {{plain: {{id: {F_ESTIMATE}}}}}}}")
    code, doc = env.doc("update", "DEMO-1", "--set", "plain=2")
    assert (code, doc["error"]["reason"]) == (22, "not_editable")
    assert env.writes() == []


def test_a_customfield_id_is_refused_unless_mapped(env: Env) -> None:
    code, doc = env.doc("update", "DEMO-1", "--set", "customfield_90006=2")
    assert (code, doc["error"]["reason"]) == (22, "unknown_field")
    code, result = env.apply("DEMO-1", "--set", f"{F_POINTS}=2")  # estimate is mapped to it
    assert code == 0, result
    assert env.fake.tickets["DEMO-1"].points == 2


def test_the_provider_400_field_errors_surface_in_details(env: Env) -> None:
    # The metadata lists a field the provider's screen does not accept (a non-screen field).
    env.use(extra=f"fields: {{extra: {{plain: {{id: {F_ESTIMATE}}}}}}}")
    route = next(i for i, r in enumerate(env.fake._routes) if r[2] == env.fake._editmeta)
    method, pattern, _ = env.fake._routes[route]

    def listing(call: Any, key: str) -> Out:
        fields = env.fake._editable_fields()
        fields[F_ESTIMATE] = {
            "required": False,
            "name": "Story point estimate",
            "schema": {"type": "number", "custom": "x:jsw-story-points"},
            "operations": ["set"],
        }
        return Out(200, {"fields": fields})

    env.fake._routes[route] = (method, pattern, listing)
    code, result = env.apply("DEMO-1", "--title", "Never applied", "--set", "plain=2")
    assert (code, result["error"]["code"]) == (12, "rejected")
    details = result["error"]["details"]
    assert F_ESTIMATE in details["field_errors"]  # the provider's own key, sanitized
    assert env.fake.tickets["DEMO-1"].summary == "Seeded backlog task"  # nothing applied (V-10)
    assert details["attempt_id"]


# ---- partial and failures ----------------------------------------------------------------------


def test_set_fields_landing_and_assign_failing_is_exit_12_partial(env: Env) -> None:
    env.fake.inject(Fault("status", method="PUT", path="/assignee$", status=400, body="no way"))
    code, doc = env.apply("DEMO-1", "--title", "Renamed", "--assign", "me")
    assert (code, doc["error"]["code"]) == (12, "partial")
    result = doc["error"]["details"]["result"]
    assert result["result"] == "partial" and result["changed"] is True
    assert [(s["kind"], s["status"]) for s in result["steps"]] == [
        ("set_fields", "done"),
        ("assign", "failed"),
    ]
    assert result["steps"][1]["code"] == "rejected"
    assert env.fake.tickets["DEMO-1"].summary == "Renamed"
    assert env.fake.tickets["DEMO-1"].assignee is None
    validator("cli/error.json").validate(doc)


def test_a_rerun_after_that_partial_sends_only_the_assign(env: Env) -> None:
    env.fake.inject(Fault("status", method="PUT", path="/assignee$", status=400, body="no way"))
    argv = ("DEMO-1", "--title", "Renamed", "--assign", "me")
    held = env.plan(*argv)["plan_sha256"]
    code, doc = env.doc("--yes", "--expect-plan", held, "update", *argv)
    assert doc["error"]["code"] == "partial"
    before = len(env.writes())
    # The old values are gone, so the held hash is stale; the new plan has one step.
    code, doc = env.doc("--yes", "--expect-plan", held, "update", *argv)
    assert (code, doc["error"]["code"]) == (20, "stale_ticket")
    new = doc["error"]["details"]["plan"]
    assert [s["kind"] for s in new["steps"]] == ["assign"]
    code, result = env.doc("--yes", "--expect-plan", new["plan_sha256"], "update", *argv)
    assert code == 0 and [s["kind"] for s in result["steps"]] == ["assign"]
    sent = env.writes()[before:]
    assert [c.path.rsplit("/", 1)[-1] for c in sent] == ["assignee"]
    assert env.fake.tickets["DEMO-1"].assignee == ME_ACCOUNT


def test_a_failed_set_fields_applies_nothing_and_skips_the_assign(env: Env) -> None:
    env.fake.inject(Fault("status", method="PUT", path="/DEMO-1$", status=400, body="bad"))
    code, doc = env.apply("DEMO-1", "--title", "Renamed", "--assign", "me")
    assert (code, doc["error"]["code"]) == (12, "rejected")
    assert "result" not in doc["error"]["details"]  # nothing landed: not a partial
    assert env.fake.tickets["DEMO-1"].summary == "Seeded backlog task"
    assert env.fake.tickets["DEMO-1"].assignee is None
    assert [c.path.rsplit("/", 1)[-1] for c in env.writes()] == ["DEMO-1"]


def test_an_assign_the_provider_rejects_names_the_field(env: Env) -> None:
    code, doc = env.apply("DEMO-1", "--assign", "acct-nobody")
    assert (code, doc["error"]["code"]) == (12, "rejected")
    assert doc["error"]["details"]["field_errors"] == {"assignee": "User does not exist."}


def test_a_dropped_connection_after_the_write_is_outcome_unknown(env: Env) -> None:
    env.fake.inject(Fault("reset", method="PUT", path="/DEMO-1$", after_apply=True))
    code, doc = env.apply("DEMO-1", "--title", "Renamed")
    assert (code, doc["error"]["code"], doc["error"]["outcome_unknown"]) == (
        12,
        "outcome_unknown",
        True,
    )
    # the write landed, so a re-run is unchanged and never double-writes
    code, result = env.doc(
        "--yes", "--expect-plan", "0" * 64, "update", "DEMO-1", "--title", "Renamed"
    )
    assert code == 0 and result["result"] == "unchanged"


def test_a_missing_ticket_is_not_found(env: Env) -> None:
    code, doc = env.doc("--dry-run", "update", "DEMO-99", "--title", "x")
    assert (code, doc["error"]["code"]) == (10, "not_found")


def test_a_moved_ticket_is_written_and_reported_under_its_canonical_key(env: Env) -> None:
    env.fake.move("DEMO-1", "DEMO-7")
    # the old key answers reads but not writes: a write under the typed key is a 404
    env.fake.inject(
        Fault("status", method="PUT", path="/DEMO-1(/assignee)?$", status=404, body="gone")
    )
    out = env.root / "hook-stdin"
    env.use(hooks_=f"hooks:\n  pre-update:\n    - cmd: '/bin/cat > {out}'")
    code, result = env.apply("DEMO-1", "--title", "Renamed", "--assign", "me", actor=HUMAN)
    assert code == 0 and result["key"] == "DEMO-7", result
    assert env.fake.tickets["DEMO-7"].summary == "Renamed"
    assert env.fake.tickets["DEMO-7"].assignee == ME_ACCOUNT
    puts = [c.path for c in env.writes() if c.method == "PUT"]
    assert puts == ["/rest/api/3/issue/DEMO-7", "/rest/api/3/issue/DEMO-7/assignee"]
    lines = (env.root / "state" / "ticketctl" / "audit.jsonl").read_text().splitlines()
    assert {json.loads(line)["target"]["key"] for line in lines} == {"DEMO-7"}
    assert json.loads(out.read_text())["object_id"] == "DEMO-7"


# ---- hooks and audit ---------------------------------------------------------------------------


def test_the_hook_payload_is_built_from_the_plan_and_matches_its_schema(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen: dict[str, Any] = {}
    real = hooks.build_payload

    def spy(hook: str, *a: Any, **kw: Any) -> dict[str, Any]:
        payload = real(hook, *a, **kw)
        seen[hook] = payload
        return payload

    monkeypatch.setattr(hooks, "build_payload", spy)
    env.use(
        hooks_="hooks:\n  pre-update:\n    - cmd: '/bin/cat > /dev/null'\n"
        "  post-update:\n    - cmd: '/bin/cat > /dev/null'"
    )
    code, result = env.apply(*ALL)
    assert code == 0, result
    pre, post = seen["pre-update"], seen["post-update"]
    validator("hooks/pre-update.json").validate(pre)
    validator("hooks/post-update.json").validate(post)
    assert pre["object_id"] == "DEMO-1" and pre["project"] == "DEMO"
    assert pre["plan"]["steps"] == ["set_fields", "assign"]
    assert pre["plan"]["assign"] == "me"
    assert pre["plan"]["current_status"] == {"name": "Backlog", "category": "new"}
    assert set(pre["plan"]["fields_changed"]) == {"title", "estimate", "labels", "assignee"}
    assert pre["ticket"]["assignee_account_id"] is None and pre["ticket"]["assignee_is_me"] is False
    assert post["result"] == "ok" and post["changed"] is True
    assert "Renamed task" not in json.dumps(pre)  # no ticket text in a payload
    fixed = {"attempt_id": "0123456789abcdef"}  # the attempt id is random per run
    assert_golden("hooks", "pre-update", "ok", env.norm({**pre, **fixed}))
    assert_golden("hooks", "post-update", "ok", env.norm({**post, **fixed}))


def test_a_veto_by_the_pre_hook_sends_nothing(env: Env) -> None:
    env.use(hooks_="hooks:\n  pre-update:\n    - cmd: 'exit 3'")
    code, doc = env.apply(*ALL)
    assert (code, doc["error"]["code"]) == (11, "vetoed")
    assert env.writes() == []


def test_a_dry_run_runs_no_pre_update_hook(env: Env) -> None:
    out = env.root / "ran"
    env.use(hooks_=f"hooks:\n  pre-update:\n    - cmd: ': > {out}'")
    code, _ = env.doc("--dry-run", "update", "DEMO-1", "--title", "x")
    assert code == 0 and not out.exists()  # a dry-run runs no hook


def test_the_audit_records_each_step_with_the_actor_and_plan(env: Env) -> None:
    code, result = env.apply(*ALL, actor=("--actor", "human:tester"))
    assert code == 0, result
    lines = (env.root / "state" / "ticketctl" / "audit.jsonl").read_text().splitlines()
    records = [json.loads(line) for line in lines]
    assert [(r["step_kind"], r["result"]) for r in records] == [
        ("set_fields", "attempted"),
        ("set_fields", "ok"),
        ("assign", "attempted"),
        ("assign", "ok"),
    ]
    assert {r["action"] for r in records} == {"update"}
    assert {r["actor"] for r in records} == {"human:tester"}
    assert {r["target"]["key"] for r in records} == {"DEMO-1"}


# ---- warnings and the human form ---------------------------------------------------------------


def test_a_done_ticket_warns_closed_ticket(env: Env) -> None:
    plan = env.plan("DEMO-3", "--add-label", "late")
    assert plan["warnings"] == ["closed_ticket"]


def test_a_stranger_account_is_a_grammar_valid_assignee(env: Env) -> None:
    plan = env.plan("DEMO-1", "--assign", STRANGER_ACCOUNT)
    assert plan["steps"] == [{"kind": "assign", "account": STRANGER_ACCOUNT}]


def test_plain_output_names_the_steps(env: Env) -> None:
    code, out, _ = env.run("--dry-run", "update", "DEMO-1", "--title", "Renamed")
    assert code == 0 and "plan update DEMO-1" in out and "step: set_fields" in out


# ---- hostile and edge inputs -------------------------------------------------------------------


def test_the_dedupe_label_is_hidden_from_the_plan_and_never_touched(env: Env) -> None:
    hidden = "tc-0123456789abcdef"
    env.fake.tickets["DEMO-1"].labels[hidden] = env.fake.now
    plan = env.plan("DEMO-1", "--add-label", "beta")
    (change,) = plan["changes"]
    assert change == {"name": "labels", "from": ["alpha"], "to": ["alpha", "beta"]}
    code, _ = env.apply("DEMO-1", "--add-label", "beta")
    assert code == 0 and hidden in env.fake.tickets["DEMO-1"].labels


def test_injection_shaped_and_unicode_labels_travel_as_json_data(env: Env) -> None:
    labels = ['a"b', "x;DROP", '{"k":1}', "é-ü-日本", "<script>"]
    argv = [part for label in labels for part in ("--add-label", label)]
    code, result = env.apply("DEMO-1", *argv)
    assert code == 0, result
    assert set(labels) <= set(env.fake.tickets["DEMO-1"].labels)
    sent = env.writes()[-1].body["update"]["labels"]
    assert sent == [{"add": label} for label in labels]


def test_a_repeated_label_is_added_once(env: Env) -> None:
    plan = env.plan("DEMO-1", "--add-label", "beta", "--add-label", "beta")
    assert plan["changes"][0]["to"] == ["alpha", "beta"]


def test_a_secret_in_a_rich_value_is_refused_and_nothing_is_written(env: Env) -> None:
    path = env.file("d.md", f"the token is {SITE_TOKEN}\n")
    code, doc = env.doc(*HUMAN, "update", "DEMO-1", "--set", f"description=@{path}")
    assert (code, doc["error"]["reason"]) == (22, "secret_in_body")
    assert env.writes() == []


def test_a_secret_in_a_scalar_value_is_refused(env: Env) -> None:
    code, doc = env.doc("update", "DEMO-1", "--set", f"Business value={SITE_TOKEN}")
    assert (code, doc["error"]["reason"]) == (22, "secret_in_body")
    assert env.writes() == []


def test_a_title_file_over_the_cap_or_missing_is_invalid_file(env: Env) -> None:
    code, doc = env.doc("update", "DEMO-1", "--title-file", str(env.root / "missing.txt"))
    assert (code, doc["error"]["reason"]) == (22, "invalid_file")
    big = env.file("big.txt", "x" * ((1 << 20) + 1))
    code, doc = env.doc("update", "DEMO-1", "--title-file", big)
    assert (code, doc["error"]["reason"]) == (22, "invalid_file")


def test_a_bad_number_and_an_unlisted_type_are_refused_before_a_write(env: Env) -> None:
    code, doc = env.doc("update", "DEMO-1", "--set", "estimate=lots")
    assert (code, doc["error"]["reason"]) == (22, "invalid_value")
    code, doc = env.doc("update", "DEMO-1", "--set", "Business value=1e3")
    assert (code, doc["error"]["reason"]) == (22, "invalid_value")
    assert env.writes() == []


def test_the_same_field_given_by_two_spellings_is_refused(env: Env) -> None:
    code, doc = env.doc("update", "DEMO-1", "--set", "estimate=1", "--set", "Story Points=2")
    assert (code, doc["error"]["reason"]) == (2, "invalid_combination")
    assert env.writes() == []


def test_a_title_holding_markup_or_quotes_is_sent_as_is(env: Env) -> None:
    title = 'Fix "quotes" & <tags> [x](y) *stars* `ticks`'
    code, result = env.apply("DEMO-1", "--title", title)
    assert code == 0, result
    assert env.fake.tickets["DEMO-1"].summary == title


def test_a_body_near_the_limit_warns_and_a_small_one_does_not(env: Env) -> None:
    env.use(hooks_="limits:\n  body_chars: 100")
    near = env.file("near.md", "x" * 95 + "\n")
    small = env.file("small.md", "x" * 40 + "\n")
    plan = env.plan_as(HUMAN, "DEMO-1", "--set", f"description=@{near}")
    assert "body_near_limit" in plan["warnings"]
    plan = env.plan_as(HUMAN, "DEMO-1", "--set", f"description=@{small}")
    assert "body_near_limit" not in plan["warnings"]


@pytest.mark.parametrize(
    ("key", "value"), [("DEMO-2", "none"), ("DEMO-1", OTHER_ACCOUNT), ("DEMO-2", OTHER_ACCOUNT)]
)
def test_the_hook_assign_fact_is_the_target_and_matches_the_schema(
    env: Env, monkeypatch: pytest.MonkeyPatch, key: str, value: str
) -> None:
    seen: dict[str, Any] = {}
    real = hooks.build_payload

    def spy(hook: str, *a: Any, **kw: Any) -> dict[str, Any]:
        seen[hook] = payload = real(hook, *a, **kw)
        return payload

    monkeypatch.setattr(hooks, "build_payload", spy)
    env.use(hooks_="hooks:\n  pre-update:\n    - cmd: '/bin/cat > /dev/null'")
    code, result = env.apply(key, "--assign", value)
    assert code == 0, result
    validator("hooks/pre-update.json").validate(seen["pre-update"])
    assert seen["pre-update"]["plan"]["assign"] == value
