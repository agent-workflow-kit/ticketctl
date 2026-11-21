"""The write flow through the CLI with a stub command (issue #23).

A stub ``transition`` command replaces the stub registry entry, so the exit codes, the flag rules
of the CLI (``--yes``), the output (``--json`` and plain) and the golden files of the plan and the
result objects are checked end to end. The real commands are #28 to #34.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

import pytest

from tests.fake_provider import SITE_EMAIL, SITE_TOKEN, FakeProvider, Fault
from tests.golden import assert_golden, validator
from tests.write_stub import StubTransition
from ticketctl import commands, selection, writeflow
from ticketctl.cli import main
from ticketctl.commands import CommandSpec
from ticketctl.flags import Globals
from ticketctl.runtime import load_runtime
from ticketctl.writeflow import Seams

TOKEN_VAR = "TICKETCTL_TEST_TOKEN"
ATTEMPT = "0123456789abcdef"
SITE_URL = "https://tracker.example.test"


def stub_handler(g: Globals, a: argparse.Namespace) -> dict[str, Any]:
    rt = load_runtime(g)
    chosen = selection.select_site(rt.config, site_flag=g.site, refs=[a.key])
    action = StubTransition(
        a.key,
        a.to or "Done",
        assign_me=bool(a.assign),
        if_status=a.if_status,
        transition_id=a.transition_id,
    )
    seams = Seams(attempt_id=lambda: ATTEMPT, interactive=lambda: False)
    return writeflow.execute(rt, chosen.site, action, seams)


class Cli:
    def __init__(self, root: Path, fake: FakeProvider, capsys: Any) -> None:
        self.fake = fake
        self.capsys = capsys
        self.config = root / "config.yaml"
        self.config.write_text(
            f"sites:\n  main:\n    url: {fake.url}\n    email: {SITE_EMAIL}\n"
            f"    token_env: {TOKEN_VAR}\n    allow_http: true\n"
        )
        self.config.chmod(0o600)

    def run(self, *argv: str) -> tuple[int, str, str]:
        code = main(["--config", str(self.config), *argv])
        cap = self.capsys.readouterr()
        assert SITE_TOKEN not in cap.out + cap.err
        return code, cap.out, cap.err

    def doc(self, *argv: str) -> tuple[int, dict[str, Any]]:
        code, out, _ = self.run("--json", *argv)
        doc = json.loads(out)
        return code, doc

    def norm(self, doc: dict[str, Any]) -> dict[str, Any]:
        text = json.dumps(doc).replace(self.fake.url, SITE_URL)
        return json.loads(text)


@pytest.fixture
def cli(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: Any,
    fake: FakeProvider,
) -> Cli:
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    monkeypatch.setenv(TOKEN_VAR, SITE_TOKEN)
    real = commands.REGISTRY[("transition",)]
    monkeypatch.setitem(
        commands.REGISTRY,
        ("transition",),
        CommandSpec(
            ("transition",), "write", real.help, stub_handler, real.add_arguments, writeflow.human
        ),
    )
    return Cli(tmp_path, fake, capsys)


def test_dry_run_json_is_a_plan_and_golden(cli: Cli) -> None:
    code, plan = cli.doc("--dry-run", "transition", "DEMO-2", "--to", "Done")
    assert code == 0 and plan["actor"] == "auto:unknown"
    assert_golden("cli", "plan", "transition-dry-run", cli.norm(plan))
    assert cli.fake.applied == []


def test_the_whole_cycle_and_the_goldens(cli: Cli) -> None:
    _, plan = cli.doc("--dry-run", "transition", "DEMO-2", "--to", "Done")
    held = plan["plan_sha256"]
    code, result = cli.doc("--yes", "--expect-plan", held, "transition", "DEMO-2", "--to", "Done")
    assert code == 0 and result["result"] == "ok"
    assert result["attempt_id"] == ATTEMPT
    assert_golden("cli", "result", "transition-ok", cli.norm(result))
    validator("cli/transition.json").validate(result)

    code, result = cli.doc(
        "--yes", "--expect-plan", "0" * 64, "transition", "DEMO-2", "--to", "Done"
    )
    assert code == 0 and result["result"] == "unchanged" and result["steps"] == []
    assert_golden("cli", "result", "transition-unchanged", cli.norm(result))

    code, empty = cli.doc("--dry-run", "transition", "DEMO-2", "--to", "Done")
    assert code == 0 and empty["steps"] == []
    assert_golden("cli", "plan", "unchanged", cli.norm(empty))


def test_partial_is_an_exit_12_envelope_with_the_result(cli: Cli) -> None:
    cli.fake.tickets["DEMO-1"].assignee = None
    _, plan = cli.doc(
        "--dry-run", "transition", "DEMO-1", "--assign", "me", "--transition-id", "99",
    )  # fmt: skip
    code, env = cli.doc(
        "--yes", "--expect-plan", plan["plan_sha256"], "transition", "DEMO-1", "--assign", "me",
        "--transition-id", "99",
    )  # fmt: skip
    assert code == 12 and env["error"]["code"] == "partial"
    assert env["error"]["details"]["attempt_id"] == ATTEMPT
    validator("cli/error.json").validate(env)
    assert_golden("cli", "result", "partial", cli.norm(env["error"]["details"]["result"]))


def test_exit_codes_of_the_gates(cli: Cli) -> None:
    code, env = cli.doc("transition", "DEMO-2", "--to", "Done")
    assert code == 13 and env["error"]["code"] == "confirmation_required"
    held = env["error"]["details"]["plan"]["plan_sha256"]
    validator("cli/error.json").validate(env)

    code, env = cli.doc("--yes", "transition", "DEMO-2", "--to", "Done")
    assert (code, env["error"]["reason"]) == (2, "yes_requires_expect_plan")

    code, env = cli.doc("--yes", "--expect-plan", "1" * 64, "transition", "DEMO-2", "--to", "Done")
    assert (code, env["error"]["code"]) == (20, "stale_ticket")
    assert env["error"]["details"]["plan"]["plan_sha256"] == held

    code, plan = cli.doc("--dry-run", "--yes", "transition", "DEMO-2", "--to", "Done")
    assert code == 0 and plan["plan_sha256"] == held
    assert cli.fake.applied == []


def test_an_unknown_exception_in_the_flow_maps_to_the_envelope(
    cli: Cli, monkeypatch: pytest.MonkeyPatch
) -> None:
    def boom(self: Any, ctx: Any, *, fresh: bool) -> Any:
        raise RuntimeError("boom " + SITE_TOKEN)

    monkeypatch.setattr(StubTransition, "reduce", boom)
    code, env = cli.doc("--dry-run", "transition", "DEMO-2", "--to", "Done")
    assert (code, env["error"]["code"]) == (1, "internal")


def test_outcome_unknown_envelope(cli: Cli) -> None:
    cli.fake.inject(Fault("reset", method="POST", path="/transitions", after_apply=True))
    _, plan = cli.doc("--dry-run", "transition", "DEMO-2", "--to", "Done")
    code, env = cli.doc(
        "--yes", "--expect-plan", plan["plan_sha256"], "transition", "DEMO-2", "--to", "Done"
    )
    assert (code, env["error"]["code"], env["error"]["outcome_unknown"]) == (
        12, "outcome_unknown", True,
    )  # fmt: skip
    assert env["error"]["details"]["attempt_id"] == ATTEMPT
    validator("cli/error.json").validate(env)


def test_plain_output(cli: Cli) -> None:
    code, out, _ = cli.run("--dry-run", "transition", "DEMO-2", "--to", "Done")
    assert code == 0 and "plan transition DEMO-2" in out and "plan_sha256" in out
