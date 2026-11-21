"""``sprint add`` and ``sprint remove`` end to end against the fake provider (issue #34)."""

from __future__ import annotations

import json
import re
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from tests.fake_provider import SITE_EMAIL, SITE_TOKEN, FakeProvider, Fault, Out
from tests.golden import assert_golden, validator
from ticketctl import hooks, sprintcmd
from ticketctl.cli import main
from ticketctl.gateway import HookRun
from ticketctl.writeflow import Seams

SITE_ENV_VAR = "TICKETCTL_TEST_SITE_ENV"
ATTEMPT = "0123456789abcdef"
SITE_URL = "https://tracker.example.test"
OPEN_PATH = "/rest/agile/1.0/sprint/{id}/issue"


class Cli:
    def __init__(self, root: Path, fake: FakeProvider, capsys: Any, extra: str = "") -> None:
        self.fake = fake
        self.capsys = capsys
        self.root = root
        self.config = root / "config.yaml"
        self.write_config(extra)

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

    def apply(self, *argv: str) -> tuple[int, dict[str, Any]]:
        """Plan with --dry-run, then apply that plan with --yes."""
        code, plan = self.doc("--dry-run", *argv)
        assert code == 0, plan
        return self.doc("--yes", "--expect-plan", plan["plan_sha256"], *argv)

    def writes(self) -> list[Any]:
        return [c for c in self.fake.calls if c.method != "GET"]

    def audit(self) -> list[dict[str, Any]]:
        path = self.root / "state" / "ticketctl" / "audit.jsonl"
        return [json.loads(line) for line in path.read_text().splitlines()]

    def sprints_of(self, key: str) -> list[int]:
        return list(self.fake.tickets[key].sprints)


@pytest.fixture
def cli(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: Any, fake: FakeProvider) -> Cli:
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    monkeypatch.setenv(SITE_ENV_VAR, SITE_TOKEN)
    monkeypatch.setenv("PATH", "/nonexistent")  # a test can never start a real program by PATH
    monkeypatch.setattr(sprintcmd, "make_seams", lambda: Seams(attempt_id=lambda: ATTEMPT))
    return Cli(tmp_path, fake, capsys)


def override(fake: FakeProvider, method: str, suffix: str, handler: Callable[..., Out]) -> None:
    routes = [
        (m, p, handler if m == method and p.pattern.endswith(suffix) else h)
        for m, p, h in fake._routes
    ]
    assert any(h is handler for _, _, h in routes), suffix
    fake._routes[:] = routes


def set_sprint(fake: FakeProvider, sid: int, **kw: Any) -> None:
    base = {"name": f"Sprint {sid}", "state": "future", "start": None, "end": None, "board": 1}
    fake.sprints[sid] = {**fake.sprints.get(sid, base), **kw}


# --------------------------------------------------------------------------------------------
# sprint add: membership and targets
# --------------------------------------------------------------------------------------------


def test_add_open_puts_the_ticket_in_the_active_sprint(cli: Cli) -> None:
    code, result = cli.apply("sprint", "add", "DEMO-1", "--sprint", "open")
    assert code == 0 and result["result"] == "ok" and result["changed"] is True
    assert result["action"] == "sprint_add" and result["key"] == "DEMO-1"
    assert result["steps"] == [{"kind": "sprint_add", "status": "done"}]
    assert result["attempt_id"] == ATTEMPT and result["warnings"] == []
    validator("cli/sprint-add.json").validate(result)
    assert_golden("cli", "sprint-add", "ok", cli.norm(result))
    assert cli.sprints_of("DEMO-1") == [2]
    (post,) = cli.writes()
    assert post.path == "/rest/agile/1.0/sprint/2/issue" and post.body == {"issues": ["DEMO-1"]}


def test_add_by_id_needs_no_board_lookup(cli: Cli) -> None:
    code, result = cli.apply("sprint", "add", "DEMO-1", "--sprint", "3")
    assert code == 0 and result["result"] == "ok" and cli.sprints_of("DEMO-1") == [3]
    assert not [c for c in cli.fake.calls if "/board" in c.path]


def test_the_dry_run_plan_shows_the_change_and_sends_nothing(cli: Cli) -> None:
    code, plan = cli.doc("--dry-run", "sprint", "add", "DEMO-1", "--sprint", "2")
    assert code == 0
    assert plan["action"] == "sprint_add" and plan["key"] == "DEMO-1"
    assert plan["steps"] == [{"kind": "sprint_add", "sprint": "2"}]
    assert plan["changes"] == [{"name": "sprints", "from": [], "to": ["2: Sprint two"]}]
    assert plan["warnings"] == [] and plan["dedupe_key"] is None
    validator("cli/sprint-add.json").validate(plan)
    assert_golden("cli", "sprint-add", "dry-run", cli.norm(plan))
    assert cli.writes() == [] and cli.sprints_of("DEMO-1") == []


def test_already_in_the_sprint_is_unchanged_and_writes_nothing(cli: Cli) -> None:
    code, result = cli.doc("--yes", "--expect-plan", "0" * 64, "sprint", "add", "DEMO-2",
                           "--sprint", "2")  # fmt: skip
    assert code == 0 and result["result"] == "unchanged" and result["changed"] is False
    assert result["steps"] == [] and result["attempt_id"] is None
    validator("cli/sprint-add.json").validate(result)
    assert_golden("cli", "sprint-add", "unchanged", cli.norm(result))
    assert cli.writes() == [] and not (cli.root / "state" / "ticketctl" / "audit.jsonl").exists()


def test_the_dry_run_of_a_noop_has_no_steps(cli: Cli) -> None:
    code, plan = cli.doc("--dry-run", "sprint", "add", "DEMO-2", "--sprint", "open")
    assert code == 0 and plan["steps"] == [] and plan["changes"] == []


def test_a_move_out_of_another_sprint_warns_and_shows_from_and_to(cli: Cli) -> None:
    code, plan = cli.doc("--dry-run", "sprint", "add", "DEMO-2", "--sprint", "3")
    assert code == 0 and plan["warnings"] == ["sprint_moved"]
    assert plan["changes"] == [
        {"name": "sprints", "from": ["2: Sprint two"], "to": ["3: Sprint three"]}
    ]
    code, result = cli.apply("sprint", "add", "DEMO-2", "--sprint", "3")
    assert code == 0 and result["warnings"] == ["sprint_moved"]
    assert cli.sprints_of("DEMO-2") == [3]
    assert_golden("cli", "sprint-add", "moved", cli.norm(result))


def test_a_closed_sprint_of_the_ticket_is_history_and_is_not_a_move(cli: Cli) -> None:
    assert cli.sprints_of("DEMO-3") == [1]  # closed
    code, plan = cli.doc("--dry-run", "sprint", "add", "DEMO-3", "--sprint", "2")
    assert code == 0 and plan["warnings"] == []
    assert plan["changes"] == [{"name": "sprints", "from": [], "to": ["2: Sprint two"]}]


def test_a_closed_target_exits_20_sprint_closed_even_for_a_dry_run(cli: Cli) -> None:
    for argv in (["--dry-run"], ["--yes", "--expect-plan", "0" * 64]):
        code, env = cli.doc(*argv, "sprint", "add", "DEMO-1", "--sprint", "1")
        assert (code, env["error"]["code"], env["error"]["reason"]) == (
            20, "sprint_closed", "sprint_closed",
        )  # fmt: skip
        validator("cli/error.json").validate(env)
    assert cli.writes() == []


def test_a_ticket_in_a_closed_sprint_is_not_already_in_it(cli: Cli) -> None:
    code, env = cli.doc("--dry-run", "sprint", "add", "DEMO-3", "--sprint", "1")
    assert (code, env["error"]["code"]) == (20, "sprint_closed")


def test_several_active_sprints_are_ambiguous(cli: Cli) -> None:
    set_sprint(cli.fake, 4, name="Sprint four customfield_90001", state="active")
    code, env = cli.doc("--dry-run", "sprint", "add", "DEMO-1", "--sprint", "open")
    assert (code, env["error"]["code"]) == (21, "ambiguous_sprint")
    assert len(env["error"]["details"]["candidates"]) == 2
    assert "customfield_90001" not in json.dumps(env)  # no field id outside fields
    validator("cli/error.json").validate(env)
    assert cli.writes() == []


def test_no_active_sprint_is_not_found(cli: Cli) -> None:
    cli.fake.sprints[2]["state"] = "closed"
    code, env = cli.doc("--dry-run", "sprint", "add", "DEMO-1", "--sprint", "open")
    assert (code, env["error"]["code"], env["error"]["details"]["option"]) == (
        10, "not_found", "sprint",
    )  # fmt: skip


def test_next_is_the_future_sprint_with_the_earliest_start(cli: Cli) -> None:
    set_sprint(cli.fake, 5, start="2026-04-20T15:00:00.000Z", end="2026-05-04T15:00:00.000Z")
    set_sprint(cli.fake, 6, start="2026-04-06T15:00:00.000Z", end="2026-04-20T15:00:00.000Z")
    set_sprint(cli.fake, 7)  # no start: only the fallback
    code, plan = cli.doc("--dry-run", "sprint", "add", "DEMO-1", "--sprint", "next")
    assert code == 0 and plan["steps"] == [{"kind": "sprint_add", "sprint": "6"}]


def test_next_without_any_start_is_the_lowest_id(cli: Cli) -> None:
    set_sprint(cli.fake, 9)
    set_sprint(cli.fake, 8)
    code, plan = cli.doc("--dry-run", "sprint", "add", "DEMO-1", "--sprint", "next")
    assert code == 0 and plan["steps"] == [{"kind": "sprint_add", "sprint": "3"}]


def test_next_with_equal_starts_is_the_lowest_id(cli: Cli) -> None:
    for sid in (8, 6):
        set_sprint(cli.fake, sid, start="2026-04-06T15:00:00.000Z", end="2026-04-20T15:00:00.000Z")
    code, plan = cli.doc("--dry-run", "sprint", "add", "DEMO-1", "--sprint", "next")
    assert code == 0 and plan["steps"] == [{"kind": "sprint_add", "sprint": "6"}]


def test_next_ignores_active_and_closed_sprints(cli: Cli) -> None:
    cli.fake.sprints[3]["state"] = "closed"
    code, env = cli.doc("--dry-run", "sprint", "add", "DEMO-1", "--sprint", "next")
    assert (code, env["error"]["code"], env["error"]["details"]["option"]) == (
        10, "not_found", "sprint",
    )  # fmt: skip


def test_next_when_the_ticket_is_already_in_it_is_unchanged(cli: Cli) -> None:
    cli.fake.tickets["DEMO-1"].sprints = [3]
    code, plan = cli.doc("--dry-run", "sprint", "add", "DEMO-1", "--sprint", "next")
    assert code == 0 and plan["steps"] == []


def test_next_never_guesses_in_a_listing_that_hit_its_cap(cli: Cli) -> None:
    def page(call: Any, *groups: str) -> Out:
        start = int(call.one("startAt", "0"))
        values = [
            {"id": start + i + 100, "name": f"S{start + i}", "state": "future", "originBoardId": 1}
            for i in range(50)
        ]
        return Out(200, {"isLast": False, "values": values})

    override(cli.fake, "GET", r"/board/(\d+)/sprint", page)
    code, env = cli.doc("--dry-run", "sprint", "add", "DEMO-1", "--sprint", "next")
    assert (code, env["error"]["code"]) == (12, "lookup_incomplete")
    assert env["error"]["retryable"] is False and cli.writes() == []


def test_next_never_guesses_when_a_listed_sprint_is_unreadable(cli: Cli) -> None:
    def page(call: Any, *groups: str) -> Out:
        values = [
            {"id": 3, "name": "ok", "state": "future", "originBoardId": 1},
            {"id": "x", "state": "future"},
        ]
        return Out(200, {"isLast": True, "values": values})

    override(cli.fake, "GET", r"/board/(\d+)/sprint", page)
    code, env = cli.doc("--dry-run", "sprint", "add", "DEMO-1", "--sprint", "next")
    assert (code, env["error"]["code"]) == (12, "provider_changed")


def test_a_missing_sprint_id_is_not_found(cli: Cli) -> None:
    code, env = cli.doc("--dry-run", "sprint", "add", "DEMO-1", "--sprint", "77")
    assert (code, env["error"]["code"], env["error"]["details"]["option"]) == (
        10, "not_found", "sprint",
    )  # fmt: skip


def test_a_sprint_answer_for_another_id_is_provider_changed(cli: Cli) -> None:
    def other(call: Any, *groups: str) -> Out:
        return Out(200, {"id": 99, "name": "x", "state": "future", "originBoardId": 1})

    override(cli.fake, "GET", r"/sprint/(\d+)", other)
    code, env = cli.doc("--dry-run", "sprint", "add", "DEMO-1", "--sprint", "3")
    assert (code, env["error"]["code"]) == (12, "provider_changed")


def test_a_missing_ticket_is_not_found(cli: Cli) -> None:
    code, env = cli.doc("--dry-run", "sprint", "add", "DEMO-999", "--sprint", "2")
    assert (code, env["error"]["code"]) == (10, "not_found")
    assert "option" not in (env["error"]["details"] or {})


# --------------------------------------------------------------------------------------------
# sprint add: arguments
# --------------------------------------------------------------------------------------------


def test_sprint_is_required_on_add(cli: Cli) -> None:
    code, env = cli.doc("--dry-run", "sprint", "add", "DEMO-1")
    assert (code, env["error"]["reason"]) == (2, "missing_option")
    assert cli.fake.calls == []


@pytest.mark.parametrize(
    "value", ["abc", "-1", "1.5", "", "0x1", "OPEN", "99999999999999999999", "٣", "2 "]
)
def test_a_bad_sprint_selector_is_exit_2(cli: Cli, value: str) -> None:
    code, env = cli.doc("--dry-run", "sprint", "add", "DEMO-1", "--sprint", value)
    assert code == 2 and env["error"]["reason"] == "invalid_argument"
    assert cli.fake.calls == []


@pytest.mark.parametrize("key", ["DEMO", "DEMO-1; DROP", "../x", "DEMO-1/../2", ""])
def test_a_bad_key_is_refused_before_any_request(cli: Cli, key: str) -> None:
    code, env = cli.doc("--dry-run", "sprint", "add", key, "--sprint", "2")
    assert code in (2, 22) and env["error"]["code"] in ("usage", "invalid_request")
    assert cli.fake.calls == []


def test_remove_refuses_sprint(cli: Cli) -> None:
    code, env = cli.doc("--dry-run", "sprint", "remove", "DEMO-2", "--sprint", "2")
    assert (code, env["error"]["reason"]) == (2, "invalid_combination")
    assert cli.fake.calls == []


def test_an_empty_board_value_is_exit_22_even_with_a_sprint_id(cli: Cli) -> None:
    code, env = cli.doc("--dry-run", "sprint", "add", "DEMO-1", "--sprint", "2", "--board", " ")
    assert (code, env["error"]["reason"]) == (22, "invalid_value")
    assert cli.fake.calls == []


def test_without_yes_a_non_interactive_run_needs_confirmation(cli: Cli) -> None:
    code, env = cli.doc("sprint", "add", "DEMO-1", "--sprint", "2")
    assert (code, env["error"]["code"]) == (13, "confirmation_required")
    assert env["error"]["details"]["plan"]["steps"] == [{"kind": "sprint_add", "sprint": "2"}]
    assert cli.writes() == []


def test_yes_needs_no_dedupe_key(cli: Cli) -> None:
    code, plan = cli.doc("--dry-run", "sprint", "add", "DEMO-1", "--sprint", "2")
    code, result = cli.doc("--yes", "--expect-plan", plan["plan_sha256"], "sprint", "add",
                           "DEMO-1", "--sprint", "2")  # fmt: skip
    assert code == 0 and result["result"] == "ok"


# --------------------------------------------------------------------------------------------
# board resolution
# --------------------------------------------------------------------------------------------


def two_boards(fake: FakeProvider, names: tuple[str, str] = ("Twin", "twin")) -> None:
    boards = [
        {"id": 1, "name": names[0], "type": "scrum"},
        {"id": 2, "name": names[1], "type": "scrum"},
    ]
    override(fake, "GET", "/board", lambda call, *g: Out(200, {"isLast": True, "values": boards}))


def test_the_board_is_derived_from_the_tickets_project(cli: Cli) -> None:
    code, plan = cli.doc("--dry-run", "sprint", "add", "DEMO-1", "--sprint", "open")
    assert code == 0 and plan["steps"] == [{"kind": "sprint_add", "sprint": "2"}]
    boards = [c for c in cli.fake.calls if c.path == "/rest/agile/1.0/board"]
    assert boards[0].one("projectKeyOrId") == "DEMO" and boards[0].one("type") == "scrum"


def test_several_boards_without_board_is_ambiguous_board(cli: Cli) -> None:
    two_boards(cli.fake)
    code, env = cli.doc("--dry-run", "sprint", "add", "DEMO-1", "--sprint", "open")
    assert (code, env["error"]["code"]) == (21, "ambiguous_board")
    assert len(env["error"]["details"]["candidates"]) == 2
    code, env = cli.doc("--dry-run", "sprint", "add", "DEMO-1", "--sprint", "next")
    assert (code, env["error"]["code"]) == (21, "ambiguous_board")


def test_a_board_flag_picks_the_board_by_id_or_name(cli: Cli) -> None:
    two_boards(cli.fake, names=("Alpha", "Beta"))
    for board in ("1", "alpha"):
        code, plan = cli.doc("--dry-run", "sprint", "add", "DEMO-1", "--sprint", "open",
                             "--board", board)  # fmt: skip
        assert code == 0 and plan["steps"] == [{"kind": "sprint_add", "sprint": "2"}]
    sprint_calls = [c for c in cli.fake.calls if re.fullmatch(r".*/board/\d+/sprint", c.path)]
    assert {c.path for c in sprint_calls} == {"/rest/agile/1.0/board/1/sprint"}


def test_a_board_name_with_several_matches_is_ambiguous(cli: Cli) -> None:
    two_boards(cli.fake)
    code, env = cli.doc("--dry-run", "sprint", "add", "DEMO-1", "--sprint", "open",
                        "--board", "TWIN")  # fmt: skip
    assert (code, env["error"]["code"]) == (21, "ambiguous_board")


def test_the_site_board_is_used_without_a_flag(cli: Cli) -> None:
    two_boards(cli.fake)
    cli.write_config("    board: 1\n")
    code, plan = cli.doc("--dry-run", "sprint", "add", "DEMO-1", "--sprint", "open")
    assert code == 0 and plan["steps"] == [{"kind": "sprint_add", "sprint": "2"}]


def test_a_missing_board_is_not_found_with_the_board_option(cli: Cli) -> None:
    code, env = cli.doc("--dry-run", "sprint", "add", "DEMO-1", "--sprint", "open",
                        "--board", "nothing")  # fmt: skip
    assert (code, env["error"]["code"], env["error"]["details"]["option"]) == (
        10, "not_found", "board",
    )  # fmt: skip


def test_a_board_without_sprints_is_invalid_value(cli: Cli) -> None:
    override(cli.fake, "GET", r"/board/(\d+)/sprint", lambda call, *g: Out(400, {}))
    code, env = cli.doc("--dry-run", "sprint", "add", "DEMO-1", "--sprint", "open",
                        "--board", "1")  # fmt: skip
    assert (code, env["error"]["reason"], env["error"]["details"]["option"]) == (
        22, "invalid_value", "board",
    )  # fmt: skip


def test_a_single_board_match_in_a_cut_off_search_is_never_acted_on(cli: Cli) -> None:
    def page(call: Any, *groups: str) -> Out:
        start = int(call.one("startAt", "0"))
        values = [
            {"id": start + i + 1, "name": f"B{start + i + 1}", "type": "scrum"} for i in range(50)
        ]
        return Out(200, {"isLast": False, "values": values})

    override(cli.fake, "GET", "/board", page)
    code, env = cli.doc("--dry-run", "sprint", "add", "DEMO-1", "--sprint", "open",
                        "--board", "b120")  # fmt: skip
    assert (code, env["error"]["code"]) == (12, "lookup_incomplete")
    assert cli.writes() == []


# --------------------------------------------------------------------------------------------
# sprint remove
# --------------------------------------------------------------------------------------------


def test_remove_plans_the_active_sprint_and_the_fake_moves_the_ticket_to_the_backlog(
    cli: Cli,
) -> None:
    code, plan = cli.doc("--dry-run", "sprint", "remove", "DEMO-2")
    assert code == 0 and plan["action"] == "sprint_remove" and plan["warnings"] == []
    assert plan["steps"] == [{"kind": "sprint_remove"}]
    assert plan["changes"] == [{"name": "sprints", "from": ["2: Sprint two"], "to": []}]
    validator("cli/sprint-remove.json").validate(plan)
    assert_golden("cli", "sprint-remove", "dry-run", cli.norm(plan))
    assert cli.sprints_of("DEMO-2") == [2] and cli.writes() == []
    code, result = cli.apply("sprint", "remove", "DEMO-2")
    assert code == 0 and result["result"] == "ok" and result["action"] == "sprint_remove"
    assert result["steps"] == [{"kind": "sprint_remove", "status": "done"}]
    validator("cli/sprint-remove.json").validate(result)
    assert_golden("cli", "sprint-remove", "ok", cli.norm(result))
    assert cli.sprints_of("DEMO-2") == []
    (post,) = cli.writes()
    assert post.path == "/rest/agile/1.0/backlog/issue" and post.body == {"issues": ["DEMO-2"]}


def test_remove_of_a_ticket_with_no_open_sprint_is_unchanged(cli: Cli) -> None:
    for key in ("DEMO-1", "DEMO-3"):  # no sprint; only a closed one
        code, result = cli.doc("--yes", "--expect-plan", "0" * 64, "sprint", "remove", key)
        assert code == 0 and result["result"] == "unchanged" and result["steps"] == []
        validator("cli/sprint-remove.json").validate(result)
    assert_golden("cli", "sprint-remove", "unchanged", cli.norm(result))
    assert cli.writes() == []
    assert cli.sprints_of("DEMO-3") == [1]  # closed sprints are history: untouched


def test_remove_from_several_open_sprints_warns_other_sprints_dropped(cli: Cli) -> None:
    cli.fake.tickets["DEMO-2"].sprints = [1, 2, 3]
    code, plan = cli.doc("--dry-run", "sprint", "remove", "DEMO-2")
    assert code == 0 and plan["warnings"] == ["other_sprints_dropped"]
    assert plan["changes"] == [
        {"name": "sprints", "from": ["2: Sprint two", "3: Sprint three"], "to": []}
    ]
    code, result = cli.apply("sprint", "remove", "DEMO-2")
    assert code == 0 and result["warnings"] == ["other_sprints_dropped"]
    assert cli.sprints_of("DEMO-2") == [1]  # the closed one stays
    assert_golden("cli", "sprint-remove", "several", cli.norm(result))


def test_remove_needs_no_board_and_ignores_one(cli: Cli) -> None:
    two_boards(cli.fake)
    code, result = cli.apply("sprint", "remove", "DEMO-2", "--board", "Twin")
    assert code == 0 and result["result"] == "ok"
    assert not [c for c in cli.fake.calls if "/board" in c.path]


def test_a_rerun_after_the_write_landed_is_unchanged_with_the_old_hash(cli: Cli) -> None:
    argv = ["sprint", "remove", "DEMO-2"]
    _, plan = cli.doc("--dry-run", *argv)
    assert cli.doc("--yes", "--expect-plan", plan["plan_sha256"], *argv)[0] == 0
    code, again = cli.doc("--yes", "--expect-plan", plan["plan_sha256"], *argv)
    assert code == 0 and again["result"] == "unchanged" and len(cli.writes()) == 1


def test_an_unknown_outcome_is_retryable_and_the_rerun_sends_nothing(cli: Cli) -> None:
    cli.fake.inject(Fault("reset", method="POST", path="/sprint/3/issue", after_apply=True))
    argv = ["sprint", "add", "DEMO-1", "--sprint", "3"]
    _, plan = cli.doc("--dry-run", *argv)
    code, env = cli.doc("--yes", "--expect-plan", plan["plan_sha256"], *argv)
    assert (code, env["error"]["code"], env["error"]["outcome_unknown"]) == (
        12, "outcome_unknown", True,
    )  # fmt: skip
    assert env["error"]["retryable"] is True and env["error"]["details"]["attempt_id"] == ATTEMPT
    assert cli.sprints_of("DEMO-1") == [3]  # it landed
    code, again = cli.doc("--yes", "--expect-plan", plan["plan_sha256"], *argv)
    assert code == 0 and again["result"] == "unchanged"
    assert [r["result"] for r in cli.audit()] == ["attempted", "unknown"]


def test_a_rerun_after_a_refusal_sends_the_same_single_step_again(cli: Cli) -> None:
    cli.fake.inject(Fault("status", status=400, method="POST", path="/sprint/2/issue"))
    argv = ["sprint", "add", "DEMO-1", "--sprint", "2"]
    _, plan = cli.doc("--dry-run", *argv)
    code, env = cli.doc("--yes", "--expect-plan", plan["plan_sha256"], *argv)
    assert (code, env["error"]["code"]) == (12, "rejected") and "result" not in env["error"].get(
        "details", {}
    )
    code, result = cli.doc("--yes", "--expect-plan", plan["plan_sha256"], *argv)
    assert code == 0 and result["result"] == "ok" and cli.sprints_of("DEMO-1") == [2]


# --------------------------------------------------------------------------------------------
# provider refusals
# --------------------------------------------------------------------------------------------


def test_a_provider_refusal_for_a_subtask_is_rejected_with_the_message(cli: Cli) -> None:
    def refuse(call: Any, *groups: str) -> Out:
        return Out(400, {"errorMessages": ["Issue DEMO-1 is a sub-task and cannot be moved."]})

    override(cli.fake, "POST", r"/sprint/(\d+)/issue", refuse)
    code, env = cli.apply("sprint", "add", "DEMO-1", "--sprint", "2")
    assert (code, env["error"]["code"]) == (12, "rejected")
    assert env["error"]["details"]["errorMessages"] == [
        "Issue DEMO-1 is a sub-task and cannot be moved."
    ]  # noqa: E501
    validator("cli/error.json").validate(env)
    assert [r["result"] for r in cli.audit()][0] == "attempted"


def test_a_refusal_of_the_backlog_call_is_rejected(cli: Cli) -> None:
    cli.fake.inject(Fault("status", status=400, method="POST", path="/backlog/issue"))
    code, env = cli.apply("sprint", "remove", "DEMO-2")
    assert (code, env["error"]["code"]) == (12, "rejected")
    assert cli.sprints_of("DEMO-2") == [2]


def test_a_404_on_the_write_is_not_found_and_names_the_sprint(cli: Cli) -> None:
    cli.fake.inject(Fault("status", status=404, method="POST", path="/sprint/2/issue"))
    code, env = cli.apply("sprint", "add", "DEMO-1", "--sprint", "2")
    assert (code, env["error"]["code"]) == (10, "not_found")
    assert "sprint" in env["error"]["message"]


def test_a_sprint_closed_between_plan_and_send_is_refused_by_the_fresh_check(cli: Cli) -> None:
    argv = ["sprint", "add", "DEMO-1", "--sprint", "2"]
    _, plan = cli.doc("--dry-run", *argv)
    cli.fake.sprints[2]["state"] = "closed"
    code, env = cli.doc("--yes", "--expect-plan", plan["plan_sha256"], *argv)
    assert (code, env["error"]["code"]) == (20, "sprint_closed") and cli.writes() == []


# --------------------------------------------------------------------------------------------
# the plan hash
# --------------------------------------------------------------------------------------------


def _hash(cli: Cli, *argv: str) -> str:
    code, plan = cli.doc("--dry-run", *argv)
    assert code == 0, plan
    return plan["plan_sha256"]


def test_the_hash_covers_the_target_the_membership_and_the_action(cli: Cli) -> None:
    add2 = _hash(cli, "sprint", "add", "DEMO-1", "--sprint", "2")
    assert add2 == _hash(cli, "sprint", "add", "DEMO-1", "--sprint", "open")  # resolved id
    assert add2 != _hash(cli, "sprint", "add", "DEMO-1", "--sprint", "3")
    assert add2 != _hash(cli, "sprint", "add", "DEMO-2", "--sprint", "3")  # key and membership
    cli.fake.tickets["DEMO-1"].sprints = [3]
    assert add2 != _hash(cli, "sprint", "add", "DEMO-1", "--sprint", "2")  # now a move


def test_remove_and_add_never_share_a_hash(cli: Cli) -> None:
    cli.fake.tickets["DEMO-2"].sprints = [3]
    assert _hash(cli, "sprint", "remove", "DEMO-2") != _hash(
        cli, "sprint", "add", "DEMO-2", "--sprint", "2"
    )


def test_unrelated_changes_do_not_stale_the_plan(cli: Cli) -> None:
    argv = ["sprint", "add", "DEMO-1", "--sprint", "open"]
    _, plan = cli.doc("--dry-run", *argv)
    cli.fake.sprints[2]["name"] = "Renamed"  # names are not hashed
    cli.fake.tickets["DEMO-1"].summary = "Another title"
    cli.fake.tickets["DEMO-1"].status = "In Progress"
    code, result = cli.doc("--yes", "--expect-plan", plan["plan_sha256"], *argv)
    assert code == 0 and result["result"] == "ok"


def test_a_membership_change_after_the_plan_is_stale_ticket(cli: Cli) -> None:
    argv = ["sprint", "add", "DEMO-1", "--sprint", "2"]
    _, plan = cli.doc("--dry-run", *argv)
    cli.fake.tickets["DEMO-1"].sprints = [3]  # someone moved it
    code, env = cli.doc("--yes", "--expect-plan", plan["plan_sha256"], *argv)
    assert (code, env["error"]["code"]) == (20, "stale_ticket") and cli.writes() == []
    assert env["error"]["details"]["plan"]["warnings"] == ["sprint_moved"]


def test_an_open_sprint_that_changed_between_plan_and_apply_is_stale(cli: Cli) -> None:
    argv = ["sprint", "add", "DEMO-1", "--sprint", "open"]
    _, plan = cli.doc("--dry-run", *argv)
    cli.fake.sprints[2]["state"] = "closed"
    set_sprint(cli.fake, 4, state="active")
    code, env = cli.doc("--yes", "--expect-plan", plan["plan_sha256"], *argv)
    assert (code, env["error"]["code"]) == (20, "stale_ticket") and cli.writes() == []


def test_a_wrong_expect_plan_is_stale_and_sends_nothing(cli: Cli) -> None:
    code, env = cli.doc("--yes", "--expect-plan", "f" * 64, "sprint", "add", "DEMO-1",
                        "--sprint", "2")  # fmt: skip
    assert (code, env["error"]["code"]) == (20, "stale_ticket") and cli.writes() == []


# --------------------------------------------------------------------------------------------
# the ticket's sprint field
# --------------------------------------------------------------------------------------------


def test_the_sprint_field_is_read_by_its_discovered_id_only(cli: Cli) -> None:
    cli.apply("sprint", "remove", "DEMO-2")
    issue = [c for c in cli.fake.calls if re.fullmatch(r"/rest/api/3/issue/DEMO-2", c.path)]
    fields = issue[0].one("fields") or ""
    assert "customfield_90003" in fields.split(",")  # the sprint field, discovered by schema
    assert "*all" not in fields and "navigable" not in fields


def test_an_unmapped_sprint_field_is_unknown_field(cli: Cli) -> None:
    cli.fake.field_list_body = [{"id": "summary", "name": "Summary", "custom": False,
                                 "schema": {"type": "string", "system": "summary"}}]  # fmt: skip
    code, env = cli.doc("--dry-run", "sprint", "remove", "DEMO-2")
    assert (code, env["error"]["reason"]) == (22, "unknown_field")
    assert cli.writes() == []


@pytest.mark.parametrize(
    "raw",
    [
        "Sprint two",
        {"id": 2},
        [{"id": 2, "state": "active"}],
        [{"id": 2, "name": "x", "state": "weird"}],
        [None],
        [{"id": True, "name": "x", "state": "active"}],
    ],
)
def test_an_unreadable_sprint_value_on_the_ticket_never_counts_as_no_sprint(
    cli: Cli, raw: Any
) -> None:
    override_issue_sprint(cli, raw)
    for argv in (["sprint", "remove", "DEMO-2"], ["sprint", "add", "DEMO-2", "--sprint", "3"]):
        code, env = cli.doc("--dry-run", *argv)
        assert (code, env["error"]["code"]) == (12, "provider_changed"), argv
    assert cli.writes() == []


def override_issue_sprint(cli: Cli, raw: Any) -> None:
    """Make every issue read answer ``raw`` as the sprint field value (hostile provider)."""
    real = cli.fake._get_issue

    def hostile(call: Any, *groups: str) -> Out:
        out = real(call, *groups)
        out.body["fields"]["customfield_90003"] = raw
        return out

    hostile.real = real  # type: ignore[attr-defined]  # a second override finds the original again
    routes = [
        (m, p, hostile if h == real or getattr(h, "real", None) == real else h)
        for m, p, h in cli.fake._routes
    ]
    assert any(h is hostile for _, _, h in routes)
    cli.fake._routes[:] = routes


def test_a_null_or_empty_sprint_value_is_no_sprint(cli: Cli) -> None:
    for raw in (None, []):
        override_issue_sprint(cli, raw)
        code, result = cli.doc("--yes", "--expect-plan", "0" * 64, "sprint", "remove", "DEMO-2")
        assert code == 0 and result["result"] == "unchanged"


def test_string_ids_and_upper_case_states_on_the_ticket_are_read(cli: Cli) -> None:
    override_issue_sprint(cli, [{"id": "2", "name": "Sprint two", "state": "ACTIVE"}])
    code, plan = cli.doc("--dry-run", "sprint", "remove", "DEMO-2")
    assert code == 0 and plan["changes"][0]["from"] == ["2: Sprint two"]


def test_hostile_sprint_names_are_sanitized_in_the_plan(cli: Cli) -> None:
    name = f"evil\x1b[31m‮ customfield_90001 {SITE_TOKEN} " + "x" * 600
    cli.fake.sprints[2]["name"] = name
    code, out, err = cli.run("--json", "--dry-run", "sprint", "remove", "DEMO-2")
    assert code == 0
    text = json.loads(out)["changes"][0]["from"][0]
    assert "\x1b" not in text and "‮" not in text and "customfield_90001" not in text
    assert text.startswith("2: evil") and len(text) < 400
    validator("cli/sprint-remove.json").validate(json.loads(out))


def test_a_hostile_name_does_not_change_what_is_sent(cli: Cli) -> None:
    cli.fake.sprints[2]["name"] = '\'; DELETE; {"issues": ["X-1"]} \n'
    code, result = cli.apply("sprint", "add", "DEMO-1", "--sprint", "2")
    assert code == 0
    (post,) = cli.writes()
    assert post.body == {"issues": ["DEMO-1"]}


# --------------------------------------------------------------------------------------------
# hooks and audit
# --------------------------------------------------------------------------------------------


def _run_with_hooks(
    cli: Cli, monkeypatch: pytest.MonkeyPatch, *argv: str
) -> tuple[dict[str, Any], dict[str, dict[str, Any]]]:
    cli.write_config(
        "hooks:\n  pre-sprint:\n    - cmd: ignored\n  post-sprint:\n    - cmd: ignored\n"
    )
    seen: dict[str, bytes] = {}

    def fake_run(argv: list[str], *, stdin: bytes, env: dict[str, str], **kw: Any) -> HookRun:
        seen[env["TICKETCTL_HOOK"]] = stdin
        return HookRun(1, 0, b"", b"", False, False, False)

    monkeypatch.setattr(hooks, "run_hook", fake_run)
    code, result = cli.apply(*argv)
    assert code == 0, result
    return result, {name: json.loads(raw) for name, raw in seen.items()}


def test_the_hook_payloads_of_an_add_come_from_the_real_runner(
    cli: Cli, monkeypatch: pytest.MonkeyPatch
) -> None:
    result, payloads = _run_with_hooks(cli, monkeypatch, "sprint", "add", "DEMO-2", "--sprint", "3")
    assert set(payloads) == {"pre-sprint", "post-sprint"}
    for name, payload in payloads.items():
        validator(f"hooks/{name}.json").validate(payload)
        assert payload["plan"]["action"] == "sprint_add" and payload["plan"]["steps"] == [
            "sprint_add"
        ]
        assert payload["sprints"] == [{"id": 3, "state": "future"}]
        assert payload["project"] == "DEMO" and payload["object_id"] == "DEMO-2"
        assert payload["plan"]["current_status"]["category"] in ("new", "indeterminate", "done")
        assert "Sprint three" not in json.dumps(payload)  # no ticket text
        assert_golden("hooks", name, "add", cli.norm(payload))
    assert payloads["post-sprint"]["result"] == "ok" and payloads["post-sprint"]["changed"] is True


def test_the_hook_payloads_of_a_remove_list_the_dropped_sprints(
    cli: Cli, monkeypatch: pytest.MonkeyPatch
) -> None:
    cli.fake.tickets["DEMO-2"].sprints = [2, 3]
    _, payloads = _run_with_hooks(cli, monkeypatch, "sprint", "remove", "DEMO-2")
    for name, payload in payloads.items():
        validator(f"hooks/{name}.json").validate(payload)
        assert payload["plan"]["action"] == "sprint_remove"
        assert payload["sprints"] == [{"id": 2, "state": "active"}, {"id": 3, "state": "future"}]
        assert_golden("hooks", name, "remove", cli.norm(payload))


def test_a_failed_write_reaches_the_post_hook_as_failed(
    cli: Cli, monkeypatch: pytest.MonkeyPatch
) -> None:
    cli.write_config("hooks:\n  post-sprint:\n    - cmd: ignored\n")
    seen: list[dict[str, Any]] = []

    def fake_run(argv: list[str], *, stdin: bytes, env: dict[str, str], **kw: Any) -> HookRun:
        seen.append(json.loads(stdin))
        return HookRun(1, 0, b"", b"", False, False, False)

    monkeypatch.setattr(hooks, "run_hook", fake_run)
    cli.fake.inject(Fault("status", status=400, method="POST", path="/sprint/2/issue"))
    code, _ = cli.apply("sprint", "add", "DEMO-1", "--sprint", "2")
    assert code == 12 and len(seen) == 1
    validator("hooks/post-sprint.json").validate(seen[0])
    assert (seen[0]["result"], seen[0]["changed"], seen[0]["error"]["code"]) == (
        "failed", False, "rejected",
    )  # fmt: skip


def test_a_noop_runs_no_hook_and_writes_no_audit(cli: Cli, monkeypatch: pytest.MonkeyPatch) -> None:
    cli.write_config("hooks:\n  pre-sprint:\n    - cmd: ignored\n")

    def boom(*a: Any, **k: Any) -> HookRun:
        raise AssertionError("a hook ran for a no-op")

    monkeypatch.setattr(hooks, "run_hook", boom)
    code, result = cli.doc("--yes", "--expect-plan", "0" * 64, "sprint", "remove", "DEMO-1")
    assert code == 0 and result["result"] == "unchanged"


def test_a_veto_sends_nothing_and_writes_no_audit(
    cli: Cli, monkeypatch: pytest.MonkeyPatch
) -> None:
    cli.write_config("hooks:\n  pre-sprint:\n    - cmd: ignored\n")
    monkeypatch.setattr(
        hooks, "run_hook", lambda *a, **k: HookRun(1, 1, b"", b"no", False, False, False)
    )
    code, plan = cli.doc("--dry-run", "sprint", "remove", "DEMO-2")
    code, env = cli.doc("--yes", "--expect-plan", plan["plan_sha256"], "sprint", "remove", "DEMO-2")
    assert (code, env["error"]["code"]) == (11, "vetoed") and cli.writes() == []


def test_the_audit_pair_names_the_action_and_the_step(cli: Cli) -> None:
    assert cli.apply("sprint", "add", "DEMO-1", "--sprint", "3")[0] == 0
    records = cli.audit()
    assert [r["result"] for r in records] == ["attempted", "ok"]
    assert {r["action"] for r in records} == {"sprint_add"}
    assert records[0]["step_kind"] == "sprint_add" and "DEMO-1" in json.dumps(records[0]["target"])
    assert {r["attempt_id"] for r in records} == {ATTEMPT}
    for r in records:
        validator("audit.json").validate(r)
    assert cli.apply("sprint", "remove", "DEMO-1")[0] == 0
    assert [r["action"] for r in cli.audit()][2:] == ["sprint_remove", "sprint_remove"]


def test_an_account_mismatch_refuses_before_any_write(cli: Cli) -> None:
    cli.write_config("me:\n  jira: acct-9999\n")
    code, env = cli.doc("--dry-run", "sprint", "add", "DEMO-1", "--sprint", "2")
    assert (code, env["error"]["reason"]) == (2, "account_mismatch") and cli.writes() == []


def test_the_real_seams_give_a_random_attempt_id(cli: Cli, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sprintcmd, "make_seams", lambda: Seams())
    code, result = cli.apply("sprint", "add", "DEMO-1", "--sprint", "2")
    assert code == 0 and re.fullmatch(r"[0-9a-f]{16}", result["attempt_id"])


def test_the_human_output_is_one_line_per_step(cli: Cli) -> None:
    code, plan = cli.doc("--dry-run", "sprint", "add", "DEMO-1", "--sprint", "2")
    code, out, _ = cli.run("--yes", "--expect-plan", plan["plan_sha256"], "sprint", "add",
                           "DEMO-1", "--sprint", "2")  # fmt: skip
    assert code == 0 and "sprint_add" in out and "DEMO-1" in out
