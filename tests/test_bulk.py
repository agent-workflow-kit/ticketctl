"""``sprint add-many`` and ``sprint remove-many`` end to end against the fake provider (issue #44).

The batch is a driver over the single-ticket flow, so most of these tests pin what the driver adds:
the key list and its caps, one combined plan and hash, one confirmation, serial fail-stop runs, the
per-ticket outcomes, the batch audit pair and the hooks that run once per ticket.
"""

from __future__ import annotations

import itertools
import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from tests.fake_provider import SITE_EMAIL, SITE_TOKEN, FakeProvider, Fault
from tests.golden import assert_golden, validator
from ticketctl import audit, bulk, confirm_dialog, hooks, sprintcmd
from ticketctl.cli import main
from ticketctl.gateway import HookRun
from ticketctl.state import StateError
from ticketctl.writeflow import Seams

SITE_ENV_VAR = "TICKETCTL_TEST_SITE_ENV"
SITE_URL = "https://tracker.example.test"
ADD = ("sprint", "add-many")
REMOVE = ("sprint", "remove-many")


def attempt_ids() -> Callable[[], str]:
    counter = itertools.count(1)
    return lambda: f"att-{next(counter):05d}"


class Cli:
    def __init__(self, root: Path, fake: FakeProvider, capsys: Any) -> None:
        self.fake = fake
        self.capsys = capsys
        self.root = root
        self.config = root / "config.yaml"
        self.write_config()

    def write_config(self, extra: str = "") -> None:
        self.config.write_text(
            f"sites:\n  main:\n    url: {self.fake.url}\n    email: {SITE_EMAIL}\n"
            f"    token_env: {SITE_ENV_VAR}\n    allow_http: true\n" + extra
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

    def norm(self, doc: dict[str, Any]) -> dict[str, Any]:
        return json.loads(json.dumps(doc).replace(self.fake.url, SITE_URL))

    def plan(self, *argv: str) -> dict[str, Any]:
        code, plan = self.doc("--dry-run", *argv)
        assert code == 0, plan
        return plan

    def apply(self, *argv: str) -> tuple[int, dict[str, Any]]:
        """Plan with --dry-run, then apply that plan with --yes."""
        plan = self.plan(*argv)
        return self.doc("--yes", "--expect-plan", plan["plan_sha256"], *argv)

    def writes(self) -> list[Any]:
        return [c for c in self.fake.calls if c.method != "GET"]

    def audit(self) -> list[dict[str, Any]]:
        path = self.root / "state" / "ticketctl" / "audit.jsonl"
        if not path.exists():
            return []
        return [json.loads(line) for line in path.read_text().splitlines()]

    def sprints_of(self, key: str) -> list[int]:
        return list(self.fake.tickets[key].sprints)


@pytest.fixture
def cli(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: Any, fake: FakeProvider) -> Cli:
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    monkeypatch.setenv(SITE_ENV_VAR, SITE_TOKEN)
    monkeypatch.setenv("PATH", "/nonexistent")  # a test can never start a real program by PATH
    ids = attempt_ids()
    monkeypatch.setattr(sprintcmd, "make_seams", lambda: Seams(attempt_id=ids))
    fake.add_ticket("DEMO-4", "Fourth", assignee=None)
    fake.add_ticket("DEMO-5", "Fifth", assignee=None)
    return Cli(tmp_path, fake, capsys)


def post_keys(cli: Cli) -> list[str]:
    """The ticket of each provider write, in the order they were sent."""
    return [c.body["issues"][0] for c in cli.writes()]


def records(cli: Cli, action: str) -> list[dict[str, Any]]:
    return [r for r in cli.audit() if r["action"] == action]


# --------------------------------------------------------------------------------------------
# the key list and the caps
# --------------------------------------------------------------------------------------------


def test_keys_come_from_the_arguments(cli: Cli) -> None:
    plan = cli.plan(*ADD, "DEMO-1", "DEMO-4", "--sprint", "2")
    assert [t["key"] for t in plan["tickets"]] == ["DEMO-1", "DEMO-4"]
    assert plan["count"] == 2 and plan["changing"] == 2


def test_keys_come_from_a_file_with_blank_and_comment_lines(cli: Cli, tmp_path: Path) -> None:
    keys = tmp_path / "keys.txt"
    keys.write_text("# the sprint\nDEMO-4\n\n  DEMO-1  \n#DEMO-5\n")
    plan = cli.plan(*ADD, "--keys-file", str(keys), "--sprint", "2")
    assert [t["key"] for t in plan["tickets"]] == ["DEMO-4", "DEMO-1"]


def test_keys_come_from_stdin(cli: Cli, monkeypatch: pytest.MonkeyPatch) -> None:
    import io

    class Stdin(io.TextIOWrapper):
        def isatty(self) -> bool:
            return False

    monkeypatch.setattr("sys.stdin", Stdin(io.BytesIO(b"DEMO-1\nDEMO-4\n"), encoding="utf-8"))
    plan = cli.plan(*REMOVE, "--keys-file", "-")
    assert [t["key"] for t in plan["tickets"]] == ["DEMO-1", "DEMO-4"]


def test_arguments_and_a_file_together_are_refused(cli: Cli, tmp_path: Path) -> None:
    keys = tmp_path / "keys.txt"
    keys.write_text("DEMO-1\n")
    code, env = cli.doc(*ADD, "DEMO-4", "--keys-file", str(keys), "--sprint", "2")
    assert (code, env["error"]["reason"]) == (2, "invalid_combination")


def test_no_key_is_missing_option(cli: Cli) -> None:
    code, env = cli.doc(*ADD, "--sprint", "2")
    assert (code, env["error"]["reason"]) == (2, "missing_option")
    code, env = cli.doc(*REMOVE)
    assert (code, env["error"]["reason"]) == (2, "missing_option")


def test_add_many_needs_a_sprint_and_remove_many_takes_none(cli: Cli) -> None:
    code, env = cli.doc(*ADD, "DEMO-1")
    assert (code, env["error"]["reason"]) == (2, "missing_option")
    code, env = cli.doc(*REMOVE, "DEMO-1", "--sprint", "2")
    assert (code, env["error"]["code"]) == (2, "usage")
    assert cli.fake.calls == []


def test_a_repeated_key_is_refused_before_any_request(cli: Cli) -> None:
    code, env = cli.doc(*ADD, "DEMO-1", "DEMO-4", "DEMO-1", "--sprint", "2")
    assert (code, env["error"]["reason"]) == (2, "invalid_argument")
    assert cli.fake.calls == []


def test_a_bad_key_is_invalid_key_before_any_request(cli: Cli) -> None:
    code, env = cli.doc(*ADD, "DEMO-1", "not a key", "--sprint", "2")
    assert (code, env["error"]["reason"]) == (22, "invalid_key")
    assert cli.fake.calls == []


def test_the_default_cap_is_fifty_and_judged_before_any_request(cli: Cli) -> None:
    keys = [f"DEMO-{n}" for n in range(1, 52)]
    code, env = cli.doc("--dry-run", *ADD, *keys, "--sprint", "2")
    assert (code, env["error"]["reason"]) == (22, "invalid_value")
    assert "50" in env["error"]["message"] and cli.fake.calls == []


def test_fifty_keys_are_accepted(cli: Cli) -> None:
    for n in range(6, 51):
        cli.fake.add_ticket(f"DEMO-{n}", f"T{n}", assignee=None)
    keys = [f"DEMO-{n}" for n in range(1, 51)]
    plan = cli.plan(*ADD, *keys, "--sprint", "2")
    assert plan["count"] == 50
    validator("cli/sprint-add-many.json").validate(plan)


def test_the_config_cap_lowers_and_raises_the_limit(cli: Cli) -> None:
    cli.write_config("limits:\n  bulk_max: 2\n")
    code, env = cli.doc("--dry-run", *ADD, "DEMO-1", "DEMO-4", "DEMO-5", "--sprint", "2")
    assert (code, env["error"]["reason"]) == (22, "invalid_value")
    cli.write_config("limits:\n  bulk_max: 3\n")
    assert cli.plan(*ADD, "DEMO-1", "DEMO-4", "DEMO-5", "--sprint", "2")["count"] == 3


def test_the_config_cap_has_a_hard_ceiling(cli: Cli) -> None:
    cli.write_config("limits:\n  bulk_max: 201\n")
    code, env = cli.doc("--dry-run", *ADD, "DEMO-1", "--sprint", "2")
    assert (code, env["error"]["code"]) == (2, "invalid_config")


def test_max_tickets_only_lowers_the_cap(cli: Cli) -> None:
    code, env = cli.doc(
        "--dry-run", *ADD, "DEMO-1", "DEMO-4", "--max-tickets", "1", "--sprint", "2"
    )
    assert (code, env["error"]["reason"]) == (22, "invalid_value")
    code, env = cli.doc("--dry-run", *ADD, "DEMO-1", "--max-tickets", "51", "--sprint", "2")
    assert (code, env["error"]["reason"]) == (2, "invalid_argument")
    code, env = cli.doc("--dry-run", *ADD, "DEMO-1", "--max-tickets", "0", "--sprint", "2")
    assert (code, env["error"]["reason"]) == (2, "invalid_argument")
    assert cli.fake.calls == []


def test_a_file_over_the_cap_is_refused_whole(cli: Cli, tmp_path: Path) -> None:
    keys = tmp_path / "keys.txt"
    keys.write_text("".join(f"DEMO-{n}\n" for n in range(1, 60)))
    code, env = cli.doc("--dry-run", *ADD, "--keys-file", str(keys), "--sprint", "2")
    assert (code, env["error"]["reason"]) == (22, "invalid_value")


def test_keys_of_two_sites_are_refused(cli: Cli) -> None:
    site = (
        f"    url: {cli.fake.url}\n    email: {SITE_EMAIL}\n"
        f"    token_env: {SITE_ENV_VAR}\n    allow_http: true\n"
    )
    cli.config.write_text(
        f"sites:\n  main:\n{site}    projects: [DEMO]\n  other:\n{site}    projects: [OTHR]\n"
    )
    cli.config.chmod(0o600)
    code, env = cli.doc("--dry-run", *ADD, "DEMO-1", "OTHR-1", "--sprint", "2")
    assert code == 22 and env["error"]["reason"] == "mixed_sites"


# --------------------------------------------------------------------------------------------
# the combined plan
# --------------------------------------------------------------------------------------------


def test_the_dry_run_prints_every_ticket_and_writes_nothing(cli: Cli) -> None:
    plan = cli.plan(*ADD, "DEMO-1", "DEMO-2", "DEMO-4", "--sprint", "2")
    assert plan["action"] == "sprint_add" and plan["count"] == 3 and plan["changing"] == 2
    assert [t["steps"] for t in plan["tickets"]] == [
        [{"kind": "sprint_add", "sprint": "2"}],
        [],  # DEMO-2 is already in sprint 2
        [{"kind": "sprint_add", "sprint": "2"}],
    ]
    validator("cli/sprint-add-many.json").validate(plan)
    assert_golden("cli", "sprint-add-many", "dry-run", cli.norm(plan))
    assert cli.writes() == [] and cli.audit() == []


def test_a_remove_plan_lists_the_dropped_sprints_per_ticket(cli: Cli) -> None:
    plan = cli.plan(*REMOVE, "DEMO-1", "DEMO-2")
    assert plan["action"] == "sprint_remove" and plan["changing"] == 1
    assert plan["tickets"][1]["changes"] == [
        {"name": "sprints", "from": ["2: Sprint two"], "to": []}
    ]
    validator("cli/sprint-remove-many.json").validate(plan)
    assert_golden("cli", "sprint-remove-many", "dry-run", cli.norm(plan))


def test_the_hash_binds_the_exact_list_the_order_and_every_ticket_state(cli: Cli) -> None:
    base = cli.plan(*ADD, "DEMO-1", "DEMO-4", "--sprint", "2")["plan_sha256"]
    assert cli.plan(*ADD, "DEMO-1", "DEMO-4", "--sprint", "2")["plan_sha256"] == base
    assert cli.plan(*ADD, "DEMO-4", "DEMO-1", "--sprint", "2")["plan_sha256"] != base
    assert cli.plan(*ADD, "DEMO-1", "--sprint", "2")["plan_sha256"] != base
    assert cli.plan(*ADD, "DEMO-1", "DEMO-4", "--sprint", "3")["plan_sha256"] != base
    cli.fake.tickets["DEMO-4"].sprints = [3]  # one ticket's facts changed
    assert cli.plan(*ADD, "DEMO-1", "DEMO-4", "--sprint", "2")["plan_sha256"] != base


def test_the_combined_hash_covers_each_tickets_own_hash(cli: Cli) -> None:
    plan = cli.plan(*ADD, "DEMO-1", "DEMO-4", "--sprint", "2")
    single = cli.plan("sprint", "add", "DEMO-1", "--sprint", "2")
    assert plan["tickets"][0] == single  # the very plan a single `sprint add` prints
    assert plan["plan_sha256"] == bulk.batch_hash("sprint_add", "main", plan["tickets"])


def test_a_ticket_that_cannot_be_planned_ends_the_batch_before_any_write(cli: Cli) -> None:
    code, env = cli.doc("--yes", "--expect-plan", "0" * 64, *ADD, "DEMO-1", "DEMO-99", "DEMO-4",
                        "--sprint", "2")  # fmt: skip
    assert code == 10 and env["error"]["code"] == "not_found"
    assert env["error"]["key"] == "DEMO-99" and "2 of 3" in env["error"]["message"]
    validator("cli/error.json").validate(env)
    assert cli.writes() == [] and cli.audit() == []


def test_a_closed_target_is_refused_for_the_whole_batch(cli: Cli) -> None:
    code, env = cli.doc("--dry-run", *ADD, "DEMO-1", "DEMO-4", "--sprint", "1")
    assert (code, env["error"]["code"]) == (20, "sprint_closed")
    assert cli.writes() == []


def test_a_batch_where_nothing_changes_is_unchanged_and_never_prompts(cli: Cli) -> None:
    code, result = cli.doc("--yes", "--expect-plan", "0" * 64, *ADD, "DEMO-2", "--sprint", "2")
    assert code == 0 and result["complete"] is True and result["changed"] is False
    assert result["counts"]["unchanged"] == 1 and result["attempt_id"] is None
    assert result["tickets"][0]["result"]["result"] == "unchanged"
    validator("cli/sprint-add-many.json").validate(result)
    assert_golden("cli", "sprint-add-many", "unchanged", cli.norm(result))
    assert cli.writes() == [] and cli.audit() == []
    code, _ = cli.doc(*ADD, "DEMO-2", "--sprint", "2")  # no --yes, no terminal: still no prompt
    assert code == 0


# --------------------------------------------------------------------------------------------
# confirmation
# --------------------------------------------------------------------------------------------


def test_yes_needs_expect_plan(cli: Cli) -> None:
    code, env = cli.doc("--yes", *ADD, "DEMO-1", "--sprint", "2")
    assert (code, env["error"]["reason"]) == (2, "yes_requires_expect_plan")
    assert cli.fake.calls == []


def test_a_wrong_expect_plan_is_stale_and_writes_nothing(cli: Cli) -> None:
    code, env = cli.doc("--yes", "--expect-plan", "0" * 64, *ADD, "DEMO-1", "DEMO-4",
                        "--sprint", "2")  # fmt: skip
    assert (code, env["error"]["code"]) == (20, "stale_ticket")
    assert env["error"]["details"]["plan"]["count"] == 2
    validator("cli/error.json").validate(env)
    assert cli.writes() == [] and cli.audit() == []


def test_without_yes_a_non_interactive_run_needs_a_human(cli: Cli) -> None:
    code, env = cli.doc(*ADD, "DEMO-1", "DEMO-4", "--sprint", "2")
    assert (code, env["error"]["code"]) == (13, "confirmation_required")
    plan = env["error"]["details"]["plan"]
    assert plan["count"] == 2 and plan["plan_sha256"] in env["error"]["message"]
    assert cli.writes() == [] and cli.audit() == []
    validator("cli/error.json").validate(env)


def test_one_answer_covers_the_whole_batch(cli: Cli, monkeypatch: pytest.MonkeyPatch) -> None:
    shown: list[dict[str, Any]] = []
    ids = attempt_ids()

    def accept(plan: dict[str, Any]) -> bool:
        shown.append(plan)
        return True

    monkeypatch.setattr(
        sprintcmd,
        "make_seams",
        lambda: Seams(attempt_id=ids, interactive=lambda: True, confirm=accept),
    )
    code, result = cli.doc("--actor", "human:tester", *ADD, "DEMO-1", "DEMO-4", "--sprint", "2")
    assert code == 0 and result["counts"]["ok"] == 2
    assert len(shown) == 1 and shown[0]["count"] == 2  # one question, not one per ticket
    assert cli.sprints_of("DEMO-1") == [2] and cli.sprints_of("DEMO-4") == [2]


def test_a_decline_writes_nothing(cli: Cli, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        sprintcmd,
        "make_seams",
        lambda: Seams(attempt_id=attempt_ids(), interactive=lambda: True, confirm=lambda p: False),
    )
    code, env = cli.doc("--actor", "human:tester", *ADD, "DEMO-1", "DEMO-4", "--sprint", "2")
    assert (code, env["error"]["code"]) == (20, "declined")
    assert cli.writes() == [] and cli.audit() == []


def test_the_dialog_lists_every_ticket_and_caps_what_is_shown() -> None:
    hostile = "Sprint \x1b[31m" + "x" * 900
    tickets = [
        {
            "key": f"DEMO-{n}",
            "steps": [{"kind": "sprint_add", "sprint": "2"}],
            "changes": [{"name": "sprints", "from": [f"1: {hostile}"], "to": ["2: Two"]}],
            "warnings": ["sprint_moved"],
        }
        for n in range(1, 4)
    ]
    tickets.append({"key": "DEMO-9", "steps": [], "changes": [], "warnings": []})
    plan = {"action": "sprint_add", "site": "main", "actor": "human:t", "tickets": tickets,
            "plan_sha256": "a" * 64}  # fmt: skip
    text = confirm_dialog.compose_batch(plan, override=True, hook_names=["pre-sprint"])
    for n in (1, 2, 3, 9):
        assert f"DEMO-{n}" in text
    assert "4 tickets" in text and "3 to change, 1 unchanged" in text and "a" * 64 in text
    assert "\x1b" not in text and "<U+001B>" in text
    assert max(len(line) for line in text.splitlines()) <= 230
    assert "DEMO-9 (unchanged)" in text and confirm_dialog.OVERRIDE_LINE in text
    assert "hooks covered: pre-sprint" in text


def test_the_dialog_caps_the_lines_of_one_ticket() -> None:
    changes = [{"name": f"c{n}", "from": [], "to": []} for n in range(40)]
    ticket = {
        "key": "DEMO-1",
        "steps": [{"kind": "sprint_add"}],
        "changes": changes,
        "warnings": [],
    }
    text = confirm_dialog.compose_batch(
        {"action": "sprint_add", "site": "s", "actor": "a", "tickets": [ticket], "plan_sha256": "0"}
    )
    block = [line for line in text.splitlines() if line.startswith("  ") or "ticket 1" in line]
    assert len(block) == confirm_dialog.BATCH_TICKET_LINES and "more lines" in block[-1]


# --------------------------------------------------------------------------------------------
# the run
# --------------------------------------------------------------------------------------------


def test_the_batch_runs_serially_in_input_order_and_reports_every_ticket(cli: Cli) -> None:
    code, result = cli.apply(*ADD, "DEMO-4", "DEMO-2", "DEMO-1", "--sprint", "2")
    assert code == 0 and result["complete"] is True and result["changed"] is True
    assert [(t["key"], t["status"]) for t in result["tickets"]] == [
        ("DEMO-4", "ok"), ("DEMO-2", "unchanged"), ("DEMO-1", "ok"),
    ]  # fmt: skip
    assert result["counts"] == {"ok": 2, "unchanged": 1, "failed": 0, "outcome_unknown": 0,
                                "not_run": 0}  # fmt: skip
    assert post_keys(cli) == ["DEMO-4", "DEMO-1"]
    assert all(c.path == "/rest/agile/1.0/sprint/2/issue" for c in cli.writes())
    for entry in result["tickets"]:
        assert entry["error"] is None
        validator("cli/result.json").validate(entry["result"])
    validator("cli/sprint-add-many.json").validate(result)
    assert_golden("cli", "sprint-add-many", "ok", cli.norm(result))
    assert [cli.sprints_of(k) for k in ("DEMO-1", "DEMO-4")] == [[2], [2]]


def test_a_remove_batch_moves_each_ticket_to_the_backlog(cli: Cli) -> None:
    cli.fake.tickets["DEMO-4"].sprints = [3]
    code, result = cli.apply(*REMOVE, "DEMO-2", "DEMO-4")
    assert code == 0 and result["action"] == "sprint_remove" and result["counts"]["ok"] == 2
    assert [c.path for c in cli.writes()] == ["/rest/agile/1.0/backlog/issue"] * 2
    assert cli.sprints_of("DEMO-2") == [] and cli.sprints_of("DEMO-4") == []


def test_each_ticket_has_its_own_audit_pair_and_the_batch_has_one(cli: Cli) -> None:
    code, result = cli.apply(*ADD, "DEMO-1", "DEMO-4", "--sprint", "2")
    assert code == 0
    batch = records(cli, "bulk")
    assert [r["result"] for r in batch] == ["attempted", "ok"]
    assert [r["changed"] for r in batch] == [None, True]
    first = batch[0]
    assert first["step"] == 1 and first["step_kind"] == "bulk" and first["target"]["key"] is None
    assert first["inputs"]["batch_size"] == 2 and first["inputs"]["bodies"] == []
    assert first["inputs"]["plan_sha256"] == result["plan_sha256"]
    assert first["attempt_id"] == result["attempt_id"] and "batch_id" not in first["inputs"]
    tickets = records(cli, "sprint_add")
    assert [(r["target"]["key"], r["result"]) for r in tickets] == [
        ("DEMO-1", "attempted"), ("DEMO-1", "ok"), ("DEMO-4", "attempted"), ("DEMO-4", "ok"),
    ]  # fmt: skip
    assert {r["inputs"]["batch_id"] for r in tickets} == {first["attempt_id"]}
    assert len({r["attempt_id"] for r in tickets}) == 2  # one attempt id per ticket
    schema = validator("audit.json")
    for record in cli.audit():
        schema.validate(record)


def test_a_single_sprint_add_audit_has_no_batch_id(cli: Cli) -> None:
    plan = cli.plan("sprint", "add", "DEMO-1", "--sprint", "2")
    code, _ = cli.doc("--yes", "--expect-plan", plan["plan_sha256"], "sprint", "add", "DEMO-1",
                      "--sprint", "2")  # fmt: skip
    assert code == 0
    assert all("batch_id" not in r["inputs"] for r in cli.audit())


def test_the_first_failure_stops_the_batch_and_the_rest_are_not_run(cli: Cli) -> None:
    cli.fake.inject(Fault("status", status=400, method="POST", path="/sprint/2/issue", skip=1))
    code, env = cli.apply(*ADD, "DEMO-1", "DEMO-4", "DEMO-5", "--sprint", "2")
    assert code == 12 and env["error"]["code"] == "partial"
    err = env["error"]
    assert err["key"] == "DEMO-4" and err["outcome_unknown"] is False
    result = err["details"]["result"]
    assert [t["status"] for t in result["tickets"]] == ["ok", "failed", "not_run"]
    assert (
        result["tickets"][1]["error"]["code"] == "rejected"
        and result["tickets"][1]["result"] is None
    )
    assert result["complete"] is False and result["changed"] is True
    assert result["counts"] == {"ok": 1, "unchanged": 0, "failed": 1, "outcome_unknown": 0,
                                "not_run": 1}  # fmt: skip
    validator("cli/error.json").validate(env)
    assert post_keys(cli) == ["DEMO-1", "DEMO-4"]  # DEMO-5 was never sent
    assert cli.sprints_of("DEMO-5") == []
    batch = records(cli, "bulk")
    assert [(r["result"], r["changed"], r.get("reason")) for r in batch] == [
        ("attempted", None, None), ("partial", False, "rejected"),
    ]  # fmt: skip
    assert err["details"]["attempt_id"] == batch[0]["attempt_id"]


def test_a_first_ticket_failure_is_its_own_error_with_the_result(cli: Cli) -> None:
    cli.fake.inject(Fault("status", status=400, method="POST", path="/sprint/2/issue"))
    code, env = cli.apply(*ADD, "DEMO-1", "DEMO-4", "--sprint", "2")
    assert (code, env["error"]["code"]) == (12, "rejected")
    result = env["error"]["details"]["result"]
    assert [t["status"] for t in result["tickets"]] == ["failed", "not_run"]
    assert result["changed"] is False and env["error"]["key"] == "DEMO-1"
    validator("cli/error.json").validate(env)
    assert len(cli.writes()) == 1
    batch = records(cli, "bulk")
    assert [(r["result"], r["changed"]) for r in batch] == [("attempted", None), ("failed", False)]


def test_an_unknown_outcome_is_reported_never_resent_and_stops_the_batch(cli: Cli) -> None:
    cli.fake.inject(Fault("close", method="POST", path="/sprint/2/issue", skip=1, after_apply=True,
                          times=None))  # fmt: skip
    code, env = cli.apply(*ADD, "DEMO-1", "DEMO-4", "DEMO-5", "--sprint", "2")
    assert code == 12 and env["error"]["code"] == "partial"
    assert env["error"]["outcome_unknown"] is True
    result = env["error"]["details"]["result"]
    assert [t["status"] for t in result["tickets"]] == ["ok", "outcome_unknown", "not_run"]
    assert result["tickets"][1]["error"]["code"] == "outcome_unknown"
    assert post_keys(cli) == ["DEMO-1", "DEMO-4"]  # exactly one attempt for the ambiguous write
    batch = records(cli, "bulk")
    assert [(r["result"], r["changed"], r.get("reason")) for r in batch] == [
        ("attempted", None, None), ("unknown", None, "outcome_unknown"),
    ]  # fmt: skip
    sent = next(r for r in records(cli, "sprint_add") if r["target"]["key"] == "DEMO-4")
    ticket = [r for r in records(cli, "sprint_add") if r["attempt_id"] == sent["attempt_id"]]
    assert [r["result"] for r in ticket] == ["attempted", "unknown"]
    # the re-run is the resume path: the write that landed is unchanged, the others are planned
    cli.fake.faults.clear()
    code, again = cli.apply(*ADD, "DEMO-1", "DEMO-4", "DEMO-5", "--sprint", "2")
    assert code == 0, again
    assert [t["status"] for t in again["tickets"]] == ["unchanged", "unchanged", "ok"]


def test_an_unknown_first_ticket_with_nothing_landed_is_outcome_unknown(cli: Cli) -> None:
    cli.fake.inject(Fault("close", method="POST", path="/sprint/2/issue", times=None))
    code, env = cli.apply(*ADD, "DEMO-1", "DEMO-4", "--sprint", "2")
    assert (code, env["error"]["code"]) == (12, "outcome_unknown")
    assert [t["status"] for t in env["error"]["details"]["result"]["tickets"]] == [
        "outcome_unknown", "not_run",
    ]  # fmt: skip
    assert len(cli.writes()) == 1


def test_a_ticket_that_went_stale_after_the_plan_stops_the_batch(
    cli: Cli, monkeypatch: pytest.MonkeyPatch
) -> None:
    def move_then_accept(plan: dict[str, Any]) -> bool:
        cli.fake.tickets["DEMO-4"].sprints = [3]  # another user moved it after the plan
        return True

    monkeypatch.setattr(
        sprintcmd,
        "make_seams",
        lambda: Seams(attempt_id=attempt_ids(), interactive=lambda: True, confirm=move_then_accept),
    )
    code, env = cli.doc("--actor", "human:tester", *ADD, "DEMO-1", "DEMO-4", "DEMO-5",
                        "--sprint", "2")  # fmt: skip
    assert (code, env["error"]["code"]) == (12, "partial")
    result = env["error"]["details"]["result"]
    assert [t["status"] for t in result["tickets"]] == ["ok", "failed", "not_run"]
    assert result["tickets"][1]["error"]["code"] == "stale_ticket"
    assert post_keys(cli) == ["DEMO-1"]


def test_a_ticket_that_changed_to_a_no_op_is_unchanged_not_an_error(
    cli: Cli, monkeypatch: pytest.MonkeyPatch
) -> None:
    def make_it_a_member(plan: dict[str, Any]) -> bool:
        cli.fake.tickets["DEMO-4"].sprints = [2]
        return True

    monkeypatch.setattr(
        sprintcmd,
        "make_seams",
        lambda: Seams(attempt_id=attempt_ids(), interactive=lambda: True, confirm=make_it_a_member),
    )
    code, result = cli.doc("--actor", "human:tester", *ADD, "DEMO-1", "DEMO-4", "--sprint", "2")
    assert code == 0 and [t["status"] for t in result["tickets"]] == ["ok", "unchanged"]


def test_an_interrupt_midway_records_an_unknown_batch_outcome(
    cli: Cli, monkeypatch: pytest.MonkeyPatch
) -> None:
    from ticketctl import writeflow

    real = writeflow.execute
    calls: list[str] = []

    def execute(rt: Any, site: str, action: Any, seams: Any = None) -> dict[str, Any]:
        calls.append(action.key)
        if len(calls) == 2:
            raise KeyboardInterrupt
        return real(rt, site, action, seams)

    monkeypatch.setattr(writeflow, "execute", execute)
    plan = cli.plan(*ADD, "DEMO-1", "DEMO-4", "DEMO-5", "--sprint", "2")
    code, out, _ = cli.run("--json", "--yes", "--expect-plan", plan["plan_sha256"], *ADD,
                           "DEMO-1", "DEMO-4", "DEMO-5", "--sprint", "2")  # fmt: skip
    assert code == 130 and calls == ["DEMO-1", "DEMO-4"]
    assert post_keys(cli) == ["DEMO-1"]
    batch = records(cli, "bulk")
    assert [(r["result"], r["changed"]) for r in batch] == [("attempted", None), ("unknown", None)]


def test_a_failed_batch_attempted_append_sends_nothing(
    cli: Cli, monkeypatch: pytest.MonkeyPatch
) -> None:
    real = audit.append_audit

    def append(state_dir: Any, record: dict[str, Any], **kw: Any) -> Any:
        if record["action"] == "bulk":
            raise StateError("record_invalid", "no")
        return real(state_dir, record, **kw)

    monkeypatch.setattr(audit, "append_audit", append)
    code, env = cli.apply(*ADD, "DEMO-1", "DEMO-4", "--sprint", "2")
    assert (code, env["error"]["code"], env["error"]["reason"]) == (1, "internal", "audit_failed")
    assert cli.writes() == []


def test_a_failed_batch_outcome_append_is_a_warning_and_the_result_still_prints(
    cli: Cli, monkeypatch: pytest.MonkeyPatch
) -> None:
    real = audit.append_audit

    def append(state_dir: Any, record: dict[str, Any], **kw: Any) -> Any:
        if record["action"] == "bulk" and record["result"] != "attempted":
            raise StateError("record_invalid", "no")
        return real(state_dir, record, **kw)

    monkeypatch.setattr(audit, "append_audit", append)
    code, result = cli.apply(*ADD, "DEMO-1", "--sprint", "2")
    assert code == 0 and result["warnings"] == ["audit_incomplete"]
    assert cli.sprints_of("DEMO-1") == [2]


# --------------------------------------------------------------------------------------------
# read-only mode and flags
# --------------------------------------------------------------------------------------------


def test_read_only_mode_refuses_before_any_request_but_allows_the_dry_run(cli: Cli) -> None:
    cli.write_config("read_only: true\n")
    code, env = cli.doc("--yes", "--expect-plan", "0" * 64, *ADD, "DEMO-1", "--sprint", "2")
    assert (code, env["error"]["code"]) == (20, "read_only")
    assert cli.fake.calls == []
    code, plan = cli.doc("--dry-run", *ADD, "DEMO-1", "--sprint", "2")
    assert code == 0 and plan["count"] == 1 and cli.writes() == []


def test_run_hooks_is_not_supported_for_a_batch(cli: Cli) -> None:
    code, env = cli.doc("--dry-run", "--run-hooks", *ADD, "DEMO-1", "--sprint", "2")
    assert (code, env["error"]["reason"]) == (2, "invalid_combination")
    assert cli.fake.calls == []


def test_the_dry_run_ignores_yes_and_expect_plan(cli: Cli) -> None:
    code, plan = cli.doc("--dry-run", "--yes", "--expect-plan", "0" * 64, *ADD, "DEMO-1",
                         "--sprint", "2")  # fmt: skip
    assert code == 0 and plan["count"] == 1 and cli.writes() == []


# --------------------------------------------------------------------------------------------
# hooks
# --------------------------------------------------------------------------------------------


class HookLog:
    def __init__(self, cli: Cli, monkeypatch: pytest.MonkeyPatch) -> None:
        self.events: list[tuple[str, str | None, int]] = []
        self.verdict: Callable[[str, str | None], int] = lambda name, key: 0
        self.cli = cli
        monkeypatch.setattr(hooks, "run_hook", self._run)

    def _run(self, argv: list[str], *, stdin: bytes, env: dict[str, str], **kw: Any) -> HookRun:
        payload = json.loads(stdin)
        name, key = env["TICKETCTL_HOOK"], payload.get("object_id")
        self.events.append((name, key, len(self.cli.writes())))  # writes done when it ran
        return HookRun(1, self.verdict(name, key), b"", b"no", False, False, False)

    def names(self, name: str) -> list[str | None]:
        return [key for n, key, _ in self.events if n == name]


def test_pre_and_post_hooks_run_once_per_ticket_with_that_tickets_payload(
    cli: Cli, monkeypatch: pytest.MonkeyPatch
) -> None:
    cli.write_config(
        "hooks:\n  pre-sprint:\n    - cmd: ignored\n  post-sprint:\n    - cmd: ignored\n"
    )
    log = HookLog(cli, monkeypatch)
    code, result = cli.apply(*ADD, "DEMO-1", "DEMO-2", "DEMO-4", "--sprint", "2")
    assert code == 0, result
    assert log.names("pre-sprint") == ["DEMO-1", "DEMO-4"]  # DEMO-2 is a no-op: no hook
    assert log.names("post-sprint") == ["DEMO-1", "DEMO-4"]
    assert [w for n, k, w in log.events if n == "pre-sprint"] == [0, 1]  # pre runs per ticket


def test_a_before_confirm_hook_runs_for_every_ticket_before_any_write_and_only_once(
    cli: Cli, monkeypatch: pytest.MonkeyPatch
) -> None:
    cli.write_config("hooks:\n  pre-sprint:\n    - cmd: ignored\n      placement: before_confirm\n")
    log = HookLog(cli, monkeypatch)
    code, result = cli.apply(*ADD, "DEMO-1", "DEMO-4", "--sprint", "2")
    assert code == 0, result
    # the dry-run of cli.apply ran none; the real run's plan phase ran both before any write
    assert log.names("pre-sprint") == ["DEMO-1", "DEMO-4"]
    assert [w for _, _, w in log.events] == [0, 0]


def test_a_before_confirm_veto_of_a_later_ticket_writes_nothing(
    cli: Cli, monkeypatch: pytest.MonkeyPatch
) -> None:
    cli.write_config("hooks:\n  pre-sprint:\n    - cmd: ignored\n      placement: before_confirm\n")
    log = HookLog(cli, monkeypatch)
    log.verdict = lambda name, key: 1 if key == "DEMO-4" else 0
    plan = cli.plan(*ADD, "DEMO-1", "DEMO-4", "--sprint", "2")
    code, env = cli.doc("--yes", "--expect-plan", plan["plan_sha256"], *ADD, "DEMO-1", "DEMO-4",
                        "--sprint", "2")  # fmt: skip
    assert (code, env["error"]["code"]) == (11, "vetoed")
    assert env["error"]["key"] == "DEMO-4" and "2 of 2" in env["error"]["message"]
    assert cli.writes() == [] and cli.audit() == []


def test_an_after_confirm_veto_stops_at_that_ticket(
    cli: Cli, monkeypatch: pytest.MonkeyPatch
) -> None:
    cli.write_config("hooks:\n  pre-sprint:\n    - cmd: ignored\n")
    log = HookLog(cli, monkeypatch)
    log.verdict = lambda name, key: 1 if key == "DEMO-4" else 0
    code, env = cli.apply(*ADD, "DEMO-1", "DEMO-4", "DEMO-5", "--sprint", "2")
    assert (code, env["error"]["code"]) == (12, "partial")
    result = env["error"]["details"]["result"]
    assert [t["status"] for t in result["tickets"]] == ["ok", "failed", "not_run"]
    assert result["tickets"][1]["error"]["code"] == "vetoed"
    assert post_keys(cli) == ["DEMO-1"]


def test_an_escalation_by_a_before_confirm_hook_stops_the_plan(
    cli: Cli, monkeypatch: pytest.MonkeyPatch
) -> None:
    cli.write_config("hooks:\n  pre-sprint:\n    - cmd: ignored\n      placement: before_confirm\n")
    log = HookLog(cli, monkeypatch)
    log.verdict = lambda name, key: 13 if key == "DEMO-1" else 0
    code, env = cli.doc("--yes", "--expect-plan", "0" * 64, *ADD, "DEMO-1", "DEMO-4",
                        "--sprint", "2")  # fmt: skip
    assert (code, env["error"]["code"]) == (13, "escalated")
    assert cli.writes() == []


def test_the_hook_digest_of_a_batch_record_is_the_sprint_familys(
    cli: Cli, monkeypatch: pytest.MonkeyPatch
) -> None:
    cli.write_config("hooks:\n  pre-sprint:\n    - cmd: ignored\n")
    HookLog(cli, monkeypatch)
    code, _ = cli.apply(*ADD, "DEMO-1", "--sprint", "2")
    assert code == 0
    batch = records(cli, "bulk")[0]
    ticket = records(cli, "sprint_add")[0]
    assert batch["inputs"]["hooks_sha256"] == ticket["inputs"]["hooks_sha256"]
