"""Hook execution through the write flow (issue #25): real ``sh -c`` processes against the fake
provider, with the stub actions of ``tests/write_stub.py``.

Every process is a tiny script run with the test interpreter by absolute path; the environment has
an empty ``PATH`` (the autouse fixture). Every pid is recorded at launch and killed exactly at
teardown (``tests/hook_support.py``).
"""

from __future__ import annotations

import json
import re
import sys
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from tests.fake_provider import SITE_TOKEN, FakeProvider
from tests.golden import validator
from tests.hook_support import PidRegistry, shell_command, wait_dead, wait_file
from tests.test_writeflow import TOKEN_VAR, World
from tests.write_stub import StubComment, StubTransition
from ticketctl import flags, hooks
from ticketctl.errors import TicketctlError
from ticketctl.plan import Planned
from ticketctl.writeflow import PostInfo, Seams

pytest_plugins = ["tests.hook_support"]  # the `pids` fixture

PY = shell_command([sys.executable, "-I"])

TICKET = {
    "type": "Task",
    "subtask": False,
    "status": "In Progress",
    "priority": "Medium",
    "assignee_account_id": "acct-0001",
    "assignee_is_me": True,
}
STATUS_NEW = {"name": "In Progress", "category": "indeterminate"}
STATUS_DONE = {"name": "Done", "category": "done"}
FACTS = {
    "project": "DEMO",
    "ticket": TICKET,
    "current_status": STATUS_NEW,
    "target_status": STATUS_DONE,
    "resolution": None,
    "assign": None,
}


class HTransition(StubTransition):
    """The stub transition that also reports the facts a hook payload needs."""

    def reduce(self, ctx: Any, *, fresh: bool) -> Planned:
        planned = super().reduce(ctx, fresh=fresh)
        planned.hook = dict(FACTS)
        return planned


class HComment(StubComment):
    def reduce(self, ctx: Any, *, fresh: bool) -> Planned:
        planned = super().reduce(ctx, fresh=fresh)
        planned.hook = {k: v for k, v in FACTS.items() if k in ("project", "ticket")}
        planned.hook["current_status"] = STATUS_NEW
        return planned


class Rig:
    """Writes hook scripts that record what they were given and answer as told."""

    def __init__(self, world: World, tmp: Path, registry: PidRegistry) -> None:
        self.world = world
        self.tmp = tmp
        self.pids = registry
        self.log = tmp / "calls.jsonl"
        self.count = 0

    def hook(
        self,
        *,
        exit: int = 0,
        out: str = "",
        err: str = "",
        sleep: float = 0.0,
        extra: str = "",
    ) -> str:
        """The ``cmd`` of a hook that logs its call and then answers."""
        self.count += 1
        path = self.tmp / f"hook{self.count}.py"
        path.write_text(
            "import json, os, sys, time\n"
            "payload = sys.stdin.read()\n"
            f"with open({str(self.log)!r}, 'a') as f:\n"
            f"    f.write(json.dumps({{'n': {self.count}, 'payload': json.loads(payload),"
            " 'env': dict(os.environ), 'cwd': os.getcwd(), 'pid': os.getpid()}) + '\\n')\n"
            f"{extra}\n"
            f"time.sleep({sleep})\n"
            f"sys.stderr.write({err!r})\n"
            f"sys.stdout.write({out!r})\n"
            f"sys.exit({exit})\n",
            encoding="utf-8",
        )
        return f"{PY} {path}"

    def calls(self) -> list[dict[str, Any]]:
        if not self.log.exists():
            return []
        rows = [json.loads(line) for line in self.log.read_text().splitlines()]
        for row in rows:
            self.pids.add(row["pid"])
        return rows

    def config(self, **hooks_by_name: list[Any]) -> None:
        lines = ["hooks:"]
        for name, cmds in hooks_by_name.items():
            lines.append(f"  {name.replace('_', '-')}:")
            for item in cmds:
                cmd, timeout = item if isinstance(item, tuple) else (item, 30)
                lines += [f"    - cmd: {json.dumps(cmd)}", f"      timeout: {timeout}"]
        self.world.write_config("\n".join(lines) + "\n")


@pytest.fixture
def world(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fake: FakeProvider) -> World:
    return World(tmp_path, monkeypatch, fake)


@pytest.fixture
def rig(world: World, tmp_path: Path, pids: PidRegistry) -> Rig:
    return Rig(world, tmp_path, pids)


def held(world: World, action_factory: Callable[[], Any]) -> str:
    return world.hash_of(action_factory())


def details(err: TicketctlError) -> dict[str, Any]:
    assert err.details is not None
    return err.details


def new() -> HTransition:
    return HTransition("DEMO-2", "Done")


def run_ok(world: World, action: Any = None, **flags_: Any) -> dict[str, Any]:
    h = world.hash_of(new())
    return world.run(action or new(), yes=True, expect_plan=h, **flags_)


# ---------------------------------------------------------------------------------------------
# Payload, environment, ordering
# ---------------------------------------------------------------------------------------------


def test_pre_and_post_payloads_validate_and_carry_no_ticket_text(rig: Rig, world: World) -> None:
    rig.config(pre_transition=[rig.hook()], post_transition=[rig.hook()])
    out = run_ok(world)
    assert out["result"] == "ok"
    pre, post = (c["payload"] for c in rig.calls())
    validator("hooks/pre-transition.json").validate(pre)
    validator("hooks/post-transition.json").validate(post)
    assert (pre["hook"], pre["object_id"], pre["project"]) == ("pre-transition", "DEMO-2", "DEMO")
    assert pre["attempt_id"] == "0123456789abcdef" and pre["actor"] == "human:tester"
    assert pre["plan"]["plan_sha256"] == world.records()[0]["inputs"]["plan_sha256"]
    assert post["result"] == "ok" and post["changed"] is True and post["error"] is None
    assert [s["status"] for s in post["steps"]] == ["done"]
    everything = json.dumps([pre, post])
    for text in ("Seeded task in progress", "Seeded"):
        assert text not in everything


def test_a_comment_payload_has_body_hashes_and_never_the_text(rig: Rig, world: World) -> None:
    rig.config(pre_comment=[rig.hook()], post_comment=[rig.hook()])
    action = lambda: HComment("DEMO-2", "the secret sentence of this comment", dedupe_key="d1")  # noqa: E731
    out = world.run(action(), yes=True, expect_plan=world.hash_of(action()))
    assert out["result"] == "ok"
    pre, post = (c["payload"] for c in rig.calls())
    validator("hooks/pre-comment.json").validate(pre)
    validator("hooks/post-comment.json").validate(post)
    assert len(pre["body"]) == 1 and re.fullmatch(r"[0-9a-f]{64}", pre["body"][0]["sha256"])
    assert "secret sentence" not in json.dumps([pre, post])
    assert pre["plan"]["dedupe_key"] == "d1"
    assert post["provider_ids"]


def test_the_environment_is_the_tools_minus_every_credential_variable(
    rig: Rig, world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    other_token, other_email = "TICKETCTL_TEST_OTHER_TOKEN", "TICKETCTL_TEST_OTHER_EMAIL"
    monkeypatch.setenv(other_token, "another-token-value-123")
    monkeypatch.setenv(other_email, "someone@example.test")
    monkeypatch.setenv("TICKETCTL_TEST_COPY", SITE_TOKEN)  # the token under another name
    monkeypatch.setenv("TICKETCTL_TEST_KEEP", "kept")
    monkeypatch.setenv("TICKETCTL_ID", "inherited")
    cmd = rig.hook()
    world.write_config(
        f"  other:\n    url: http://127.0.0.1:9\n    email_env: {other_email}\n"
        f"    token_env: {other_token}\n    allow_http: true\n"
        f"hooks:\n  pre-transition:\n    - cmd: {json.dumps(cmd)}\n"
    )
    run_ok(world)
    (call,) = rig.calls()
    env = call["env"]
    assert env["TICKETCTL_HOOK"] == "pre-transition"
    assert env["TICKETCTL_ID"] == "DEMO-2" and env["TICKETCTL_OBJECT_TYPE"] == "ticket"
    assert env["TICKETCTL_TEST_KEEP"] == "kept"
    present = [n for n in (TOKEN_VAR, other_token, other_email, "TICKETCTL_TEST_COPY") if n in env]
    assert present == []  # names only: never a value in a failure message
    leaked = SITE_TOKEN in json.dumps(call)
    assert not leaked
    assert call["cwd"] == str(Path.cwd())


def test_configured_hooks_run_before_flag_hooks_in_order_and_see_earlier_ids(
    rig: Rig, world: World
) -> None:
    rig.config(
        pre_transition=[
            rig.hook(out='{"decision_id": "cfg.1"}'),
            rig.hook(out='{"decision_id": ["cfg.2", "cfg.1"]}'),
        ]
    )
    flag = rig.hook(out='{"decision_id": "flag.hook"}')
    out = run_ok(world, decision_ids=["cli.1"], hooks=[f"pre-transition={flag}"])
    assert [c["n"] for c in rig.calls()] == [1, 2, 3]
    ids = [c["payload"]["decision_ids"] for c in rig.calls()]
    assert ids == [["cli.1"], ["cli.1", "cfg.1"], ["cli.1", "cfg.1", "cfg.2"]]
    assert out["decision_ids"] == ["cli.1", "cfg.1", "cfg.2", "flag.hook"]
    assert {tuple(r["decision_id"]) for r in world.records()} == {
        ("cli.1", "cfg.1", "cfg.2", "flag.hook")
    }


def test_hooks_of_other_families_do_not_run(rig: Rig, world: World) -> None:
    rig.config(pre_comment=[rig.hook()], pre_sprint=[rig.hook()])
    run_ok(world)
    assert rig.calls() == []


def test_a_dry_run_runs_no_hook(rig: Rig, world: World) -> None:
    rig.config(pre_transition=[rig.hook()], post_transition=[rig.hook()])
    world.run(new(), dry_run=True)
    assert rig.calls() == []


# ---------------------------------------------------------------------------------------------
# Exit codes: continue, escalate, veto
# ---------------------------------------------------------------------------------------------


def test_exit_13_escalates_later_hooks_still_run_and_the_override_continues(
    rig: Rig, world: World
) -> None:
    rig.config(pre_transition=[rig.hook(exit=13, out='{"decision_id": "esc.1"}'), rig.hook(exit=0)])
    h = world.hash_of(new())
    err = world.fail(new(), yes=True, expect_plan=h)
    assert (err.exit, err.code) == (13, "escalated")
    assert [c["n"] for c in rig.calls()] == [1, 2]
    assert details(err)["hook"] == "pre-transition" and details(err)["decision_ids"] == ["esc.1"]
    assert world.writes() == [] and world.records() == []
    out = world.run(new(), yes=True, expect_plan=h, override_escalation="ticketctl")
    assert out["result"] == "ok" and out["decision_ids"] == ["esc.1"]
    assert world.records()[0]["decision_id"] == ["esc.1"]


@pytest.mark.parametrize("code", [1, 2, 11, 12, 14, 255])
def test_any_other_nonzero_exit_vetoes_at_once(rig: Rig, world: World, code: int) -> None:
    rig.config(pre_transition=[rig.hook(exit=code, err="not today"), rig.hook()])
    h = world.hash_of(new())
    err = world.fail(new(), yes=True, expect_plan=h)
    assert (err.exit, err.code) == (11, "vetoed")
    assert details(err)["hook"] == "pre-transition" and details(err)["hook_stderr"] == "not today"
    assert details(err)["plan"]["plan_sha256"] == h
    assert [c["n"] for c in rig.calls()] == [1]  # the later hook did not run
    assert world.writes() == [] and world.records() == []


def test_the_override_never_overrides_a_veto_and_a_later_veto_beats_an_escalation(
    rig: Rig, world: World
) -> None:
    rig.config(
        pre_transition=[rig.hook(exit=13), rig.hook(exit=1, out='{"decision_id": "veto.id"}')]
    )
    h = world.hash_of(new())
    err = world.fail(new(), yes=True, expect_plan=h, override_escalation="ticketctl")
    assert err.code == "vetoed" and details(err)["decision_ids"] == ["veto.id"]
    assert world.writes() == []


def test_a_hook_killed_by_a_signal_or_missing_is_a_veto(rig: Rig, world: World) -> None:
    kill = rig.hook(extra="import signal\nos.kill(os.getpid(), signal.SIGKILL)")
    rig.config(pre_transition=[kill])
    err = world.fail(new(), yes=True, expect_plan=world.hash_of(new()))
    assert err.code == "vetoed" and re.search(r"signal|exited 137", err.message)  # sh may relay it
    rig.config(pre_transition=["/nonexistent/ticketctl-test-hook"])
    err = world.fail(new(), yes=True, expect_plan=world.hash_of(new()))
    assert err.code == "vetoed"  # sh answers 127: command not found


def test_a_hook_that_cannot_be_started_is_a_veto(
    rig: Rig, world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    rig.config(pre_transition=[rig.hook()])

    def boom(*a: object, **k: object) -> None:
        raise OSError("no exec")

    monkeypatch.setattr(hooks, "run_hook", boom)
    err = world.fail(new(), yes=True, expect_plan=world.hash_of(new()))
    assert err.code == "vetoed" and "could not be started" in err.message
    assert world.writes() == []


def test_a_missing_shell_refuses_before_any_write(
    rig: Rig, world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    rig.config(pre_transition=[rig.hook()])
    monkeypatch.setattr(hooks, "SHELL", "/nonexistent/sh")
    err = world.fail(new(), yes=True, expect_plan=world.hash_of(new()))
    assert (err.exit, err.code, err.reason) == (1, "internal", "hooks_unavailable")
    assert world.writes() == [] and rig.calls() == []


def test_a_payload_that_lacks_a_fact_fails_closed(rig: Rig, world: World) -> None:
    rig.config(pre_transition=[rig.hook()])
    h = world.hash_of(StubTransition("DEMO-2", "Done"))  # the plain stub sets no hook facts
    err = world.fail(StubTransition("DEMO-2", "Done"), yes=True, expect_plan=h)
    assert err.code == "internal" and "lacks a required fact" in err.message
    assert world.writes() == [] and rig.calls() == []


# ---------------------------------------------------------------------------------------------
# Timeouts
# ---------------------------------------------------------------------------------------------


def test_a_timed_out_pre_hook_is_a_veto_and_its_group_is_signaled(
    rig: Rig,
    world: World,
    pids: PidRegistry,
) -> None:
    marker = rig.tmp / "grandchild"
    extra = (
        "import subprocess\n"
        f"c = subprocess.Popen([sys.executable, '-I', '-c', 'import time; time.sleep(60)'])\n"
        f"open({str(marker)!r}, 'w').write(str(c.pid))\n"
    )
    rig.config(pre_transition=[(rig.hook(sleep=60, extra=extra), 1), rig.hook()])
    h = world.hash_of(new())
    start = time.monotonic()
    err = world.fail(new(), yes=True, expect_plan=h)
    assert time.monotonic() - start < 6
    assert (err.code, err.exit) == ("vetoed", 11) and "timed out" in err.message
    assert wait_file(marker)
    (grandchild,) = pids.add_file(marker)
    (call,) = rig.calls()
    assert wait_dead(call["pid"]) and wait_dead(grandchild)
    assert world.writes() == [] and world.records() == []


# ---------------------------------------------------------------------------------------------
# stderr and stdout handling
# ---------------------------------------------------------------------------------------------


def test_veto_stderr_is_sanitized_scrubbed_and_capped(rig: Rig, world: World) -> None:
    err_text = f"\x1b[31mred\x1b[0m token {SITE_TOKEN} end\n" + "z" * 20000
    rig.config(pre_transition=[rig.hook(exit=2, err=err_text)])
    err = world.fail(new(), yes=True, expect_plan=world.hash_of(new()))
    shown = details(err)["hook_stderr"]
    assert len(shown.encode()) <= 4096 and "\x1b" not in shown and "\n" not in shown
    assert "<U+001B>" in shown and SITE_TOKEN not in shown and "<redacted>" in shown


def test_multibyte_stderr_stays_within_4096_bytes() -> None:
    text = hooks.stderr_text(("é" * 5000).encode())
    assert len(text.encode()) <= 4096 and text.startswith("é")
    assert hooks.stderr_text(b"\xff\xfe bad bytes").startswith("\ufffd")


@pytest.mark.parametrize(
    "out",
    ["", "not json", "[1, 2]", '"str"', "{broken", '{"other": 1}', "\xe9", "NaN", "{" * 5000],
)
def test_garbage_stdout_is_ignored_and_never_a_veto(rig: Rig, world: World, out: str) -> None:
    rig.config(pre_transition=[rig.hook(out=out)])
    result = run_ok(world)
    assert result["result"] == "ok" and result["decision_ids"] == []


def test_oversized_stdout_is_not_read_for_ids(rig: Rig, world: World) -> None:
    big = '{"decision_id": "late.id", "pad": "' + "x" * 200_000 + '"}'
    rig.config(pre_transition=[rig.hook(out=big)])
    assert run_ok(world)["decision_ids"] == []


def test_invalid_decision_ids_are_dropped_with_one_warning_and_never_a_veto(
    rig: Rig, world: World, capsys: pytest.CaptureFixture[str]
) -> None:
    out = json.dumps({"decision_id": ["good.1", "bad id!", "x" * 200, 7, None, "good.2"]})
    rig.config(pre_transition=[rig.hook(out=out), rig.hook(out='{"decision_id": 5}')])
    result = run_ok(world)
    assert result["decision_ids"] == ["good.1", "good.2"]
    assert capsys.readouterr().err.count("decision id that was dropped") == 1


def test_more_than_16_ids_are_cut_with_the_truncation_warning(rig: Rig, world: World) -> None:
    out = json.dumps({"decision_id": [f"id.{i}" for i in range(20)]})
    rig.config(pre_transition=[rig.hook(out=out)])
    result = run_ok(world, decision_ids=["cli.1"])
    assert len(result["decision_ids"]) == 16 and result["decision_ids"][0] == "cli.1"
    assert "decision_ids_truncated" in result["warnings"]


def test_a_hook_cannot_change_the_plan_through_its_output(rig: Rig, world: World) -> None:
    out = json.dumps(
        {"decision_id": "x.1", "plan": {"steps": []}, "action": "delete", "key": "X-1"}
    )
    rig.config(pre_transition=[rig.hook(out=out)])
    result = run_ok(world)
    assert (result["action"], result["key"]) == ("transition", "DEMO-2")
    assert [c.path for c in world.writes()] == ["/rest/api/3/issue/DEMO-2/transitions"]


# ---------------------------------------------------------------------------------------------
# post- hooks
# ---------------------------------------------------------------------------------------------


def test_post_hooks_never_run_without_an_attempt(rig: Rig, world: World) -> None:
    rig.config(pre_transition=[rig.hook(exit=1)], post_transition=[rig.hook()])
    h = world.hash_of(new())
    assert world.fail(new(), yes=True, expect_plan=h).code == "vetoed"  # veto
    assert world.fail(new()).code == "confirmation_required"  # confirmation_required
    assert world.fail(new(), yes=True, expect_plan="0" * 64).code == "stale_ticket"  # stale
    rig.config(post_transition=[rig.hook()])
    run_ok(world)
    assert [c["payload"]["hook"] for c in rig.calls()][-1:] == ["post-transition"]
    posts = len([c for c in rig.calls() if c["payload"]["hook"].startswith("post-")])
    assert posts == 1
    again = world.run(new(), yes=True, expect_plan=h)  # a no-op
    assert again["result"] == "unchanged"
    assert len([c for c in rig.calls() if c["payload"]["hook"].startswith("post-")]) == 1


def test_a_failing_post_hook_is_a_warning_and_the_rest_still_run(
    rig: Rig, world: World, capsys: pytest.CaptureFixture[str]
) -> None:
    rig.config(
        post_transition=[
            rig.hook(exit=1, err="boom"),
            (rig.hook(sleep=60), 1),
            "/nonexistent/ticketctl-test-hook",
            rig.hook(),
        ]
    )
    result = run_ok(world)
    assert result["result"] == "ok"
    assert [c["n"] for c in rig.calls()] == [1, 2, 3]
    warnings = capsys.readouterr().err
    assert "post-transition exited 1" in warnings and "timed out" in warnings
    assert "boom" not in warnings  # stderr of a post hook is not relayed


def test_post_hooks_run_after_the_outcome_record_and_the_lock_release(
    rig: Rig, world: World
) -> None:
    events: list[str] = []
    extra = f"open({str(world.state / 'audit.jsonl')!r}).read()"
    rig.config(post_transition=[rig.hook(extra=extra)])

    class Lock:
        def __enter__(self) -> None:
            events.append("lock")

        def __exit__(self, *a: object) -> None:
            events.append("release")

    class Tracking(HComment):
        pass

    action = lambda: HComment("DEMO-2", "text", dedupe_key="d")  # noqa: E731
    rig.config(post_comment=[rig.hook()])
    world.run(
        action(),
        yes=True,
        expect_plan=world.hash_of(action()),
        seams=Seams(lock=lambda p, c: Lock()),
    )
    assert events == ["lock", "release"] and [c["payload"]["hook"] for c in rig.calls()] == [
        "post-comment"
    ]
    assert [r["result"] for r in world.records()] == ["attempted", "ok"]


def test_an_async_post_hook_is_detached(rig: Rig, world: World, pids: PidRegistry) -> None:
    cmd = rig.hook(sleep=0.3)
    world.write_config(
        f"hooks:\n  post-transition:\n    - cmd: {json.dumps(cmd)}\n      async: true\n"
    )
    start = time.monotonic()
    assert run_ok(world)["result"] == "ok"
    assert time.monotonic() - start < 5
    assert wait_file(rig.log)
    (call,) = rig.calls()
    assert call["payload"]["hook"] == "post-transition"
    assert wait_dead(call["pid"], 5)


# ---------------------------------------------------------------------------------------------
# Payloads for every family and outcome, against the schemas
# ---------------------------------------------------------------------------------------------


def _plan(action: str, key: str | None = "DEMO-2") -> dict[str, Any]:
    return {
        "action": action,
        "site": "main",
        "key": key,
        "actor": "human:tester",
        "steps": [{"kind": "set_fields", "fields": ["a"]}],
        "changes": [{"name": "summary", "from": "a", "to": "b"}],
        "dedupe_key": None,
        "plan_sha256": "a" * 64,
    }


def _planned(**hook: Any) -> Planned:
    return Planned(steps=[], intent={}, bodies=[{"sha256": "b" * 64, "bytes": 10}], hook=hook)


@pytest.mark.parametrize(
    ("action", "hook_name", "facts"),
    [
        ("transition", "transition", FACTS),
        ("update", "update", FACTS),
        ("comment", "comment", FACTS),
        (
            "sprint_add",
            "sprint",
            {**FACTS, "sprints": [{"id": 5, "state": "future"}]},
        ),
        (
            "sprint_remove",
            "sprint",
            {**FACTS, "sprints": [{"id": 5, "state": "active"}, {"id": 6, "state": "future"}]},
        ),
        (
            "create",
            "create",
            {"project": "DEMO", "create": {"type": "Task", "parent": None, "assign": "me"}},
        ),
    ],
)
@pytest.mark.parametrize("phase", ["pre", "post"])
def test_every_family_payload_validates(
    action: str, hook_name: str, facts: dict[str, Any], phase: str
) -> None:
    key = None if action == "create" else "DEMO-2"
    planned = _planned(**facts)
    name = f"{phase}-{hook_name}"
    info = None
    if phase == "post":
        info = PostInfo(
            "ok", None, [{"kind": "create", "status": "done"}], ["10001"], True, "DEMO-9"
        )
    payload = hooks.build_payload(
        name, _plan(action, key), planned, "0123456789abcdef", ["d.1"], info
    )
    validator(f"hooks/{name}.json").validate(payload)
    if action == "create":
        assert payload["object_id"] == ("DEMO-9" if phase == "post" else None)


@pytest.mark.parametrize(
    "info",
    [
        PostInfo(
            "failed",
            {"code": "rejected", "reason": None},
            [{"kind": "set_fields", "status": "failed"}],
            [],
            False,
        ),
        PostInfo(
            "unknown",
            {"code": "outcome_unknown", "reason": None},
            [{"kind": "set_fields", "status": "failed"}],
            [],
            None,
        ),
        PostInfo(
            "partial",
            {"code": "timeout", "reason": None},
            [{"kind": "assign", "status": "done"}, {"kind": "transition", "status": "failed"}],
            [],
            True,
        ),
    ],
)
def test_post_payloads_of_a_failure_validate(info: PostInfo) -> None:
    payload = hooks.build_payload(
        "post-transition", _plan("transition"), _planned(**FACTS), "0123456789abcdef", [], info
    )
    validator("hooks/post-transition.json").validate(payload)
    assert payload["result"] == info.result and payload["changed"] is info.changed


def test_a_create_that_created_nothing_has_a_null_object_id() -> None:
    info = PostInfo("failed", {"code": "rejected", "reason": None}, [], [], False)
    facts = {"project": "DEMO", "create": {"type": "Task", "parent": "DEMO-1", "assign": None}}
    payload = hooks.build_payload(
        "post-create", _plan("create", None), _planned(**facts), "0123456789abcdef", [], info
    )
    assert payload["object_id"] is None
    validator("hooks/post-create.json").validate(payload)


def test_provider_names_in_the_payload_pass_the_sanitizer() -> None:
    ticket = {**TICKET, "status": "Do\x1bne\u202e", "type": "T" * 900}
    payload = hooks.build_payload(
        "pre-update",
        _plan("update"),
        _planned(**{**FACTS, "ticket": ticket}),
        "0123456789abcdef",
        [],
    )
    assert "\x1b" not in json.dumps(payload) and "\u202e" not in payload["ticket"]["status"]
    assert len(payload["ticket"]["type"]) <= 256
    validator("hooks/pre-update.json").validate(payload)


@pytest.mark.parametrize(
    "bad",
    [
        {"ticket": None},
        {"ticket": {**TICKET, "subtask": "no"}},
        {"ticket": {**TICKET, "assignee_account_id": "bad id"}},
        {"project": "lower"},
        {"current_status": {"name": "x", "category": "weird"}},
    ],
)
def test_a_payload_with_a_bad_fact_is_refused(bad: dict[str, Any]) -> None:
    with pytest.raises(TicketctlError):
        hooks.build_payload(
            "pre-update", _plan("update"), _planned(**{**FACTS, **bad}), "0123456789abcdef", []
        )


def test_the_sprint_payload_needs_sprints() -> None:
    with pytest.raises(TicketctlError):
        hooks.build_payload(
            "pre-sprint", _plan("sprint_add"), _planned(**FACTS), "0123456789abcdef", []
        )


# ---------------------------------------------------------------------------------------------
# The --hook command text
# ---------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "command",
    ["a\nb", "a\rb", "a\x00b", "a\x1bb", "a\x7fb", "a\x85b", "a\u2028b", "a\u200bb", "x" * 4097],
)
def test_a_hook_command_with_control_characters_or_too_long_is_refused(command: str) -> None:
    import argparse

    with pytest.raises(argparse.ArgumentTypeError) as info:
        flags._hook(f"pre-comment={command}")  # noqa: SLF001
    assert command[:5] not in str(info.value) or command[:1] == "x"


@pytest.mark.parametrize(
    "command", ["true", "my-check --flag 'a b'\t| cat", "écho ünï", "x" * 4096]
)
def test_ordinary_hook_commands_are_accepted(command: str) -> None:
    assert flags._hook(f"pre-comment={command}") == f"pre-comment={command}"  # noqa: SLF001
