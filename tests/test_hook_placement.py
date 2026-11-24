"""Hook placement (issue #47): ``placement: before_confirm`` on a ``pre-`` hook and
``--dry-run --run-hooks``.

Real ``sh -c`` hooks against the fake provider, with the rig of ``tests/test_hooks.py``.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from tests import hook_support
from tests.fake_provider import FakeProvider
from tests.golden import validator
from tests.hook_support import PidRegistry
from tests.test_confirm_engine import _run_real, terminal
from tests.test_hooks import HComment, HTransition, Rig, details, new
from tests.test_writeflow import World
from ticketctl import writeflow
from ticketctl.errors import TicketctlError
from ticketctl.writeflow import Seams

pids = hook_support.pids  # the fixture, by name


@pytest.fixture
def world(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fake: FakeProvider) -> World:
    return World(tmp_path, monkeypatch, fake)


@pytest.fixture
def rig(world: World, tmp_path: Path, pids: PidRegistry) -> Rig:
    return Rig(world, tmp_path, pids)


def config(rig: Rig, **by_name: list[tuple[str, str]]) -> None:
    """Write hooks given as ``name=[(cmd, placement), ...]``."""
    lines = ["hooks:"]
    for name, items in by_name.items():
        lines.append(f"  {name.replace('_', '-')}:")
        for cmd, placement in items:
            lines.append(f"    - cmd: {json.dumps(cmd)}")
            if placement:
                lines.append(f"      placement: {placement}")
    rig.world.write_config("\n".join(lines) + "\n")


BEFORE = "before_confirm"
AFTER = "after_confirm"


class Dialog:
    """A confirm seam that records how many hooks had run when the dialog was shown."""

    def __init__(self, rig: Rig, answer: bool = True) -> None:
        self.rig = rig
        self.answer = answer
        self.seen: list[int] = []

    def __call__(self, plan: dict[str, Any]) -> bool:
        self.seen.append(len(self.rig.calls()))
        return self.answer

    def seams(self) -> Seams:
        return Seams(interactive=lambda: True, confirm=self)


# ---------------------------------------------------------------------------------------------
# Placement before the confirmation
# ---------------------------------------------------------------------------------------------


def test_a_before_confirm_hook_runs_before_the_dialog_and_an_after_one_after(
    rig: Rig, world: World
) -> None:
    config(rig, pre_transition=[(rig.hook(), BEFORE), (rig.hook(), AFTER), (rig.hook(), "")])
    dialog = Dialog(rig)
    out = world.run(new(), seams=dialog.seams())
    assert out["result"] == "ok"
    assert dialog.seen == [1]  # only the first hook had run when the human was asked
    assert [c["n"] for c in rig.calls()] == [1, 2, 3]  # then the others, in list order


def test_the_payload_is_the_same_whatever_the_placement(rig: Rig, world: World) -> None:
    config(rig, pre_transition=[(rig.hook(), BEFORE), (rig.hook(), AFTER)])
    world.run(new(), seams=Dialog(rig).seams())
    early, late = (c["payload"] for c in rig.calls())
    for payload in (early, late):
        validator("hooks/pre-transition.json").validate(payload)
    assert early == late
    assert "Seeded" not in json.dumps(early)  # no ticket text before the human has seen any


def test_a_before_confirm_veto_stops_before_the_dialog(rig: Rig, world: World) -> None:
    config(rig, pre_transition=[(rig.hook(exit=1, err="no"), BEFORE), (rig.hook(), AFTER)])
    dialog = Dialog(rig)
    err = world.fail(new(), seams=dialog.seams())
    assert (err.exit, err.code) == (11, "vetoed")
    assert details(err)["hook"] == "pre-transition" and details(err)["hook_stderr"] == "no"
    assert details(err)["plan"]["plan_sha256"]
    assert dialog.seen == [] and [c["n"] for c in rig.calls()] == [1]
    assert world.writes() == [] and world.records() == []


def test_a_before_confirm_veto_beats_confirmation_required(rig: Rig, world: World) -> None:
    config(rig, pre_transition=[(rig.hook(exit=1), BEFORE)])
    err = world.fail(new(), seams=Seams(interactive=lambda: False))  # no --yes, no human
    assert (err.exit, err.code) == (11, "vetoed")


def test_an_unvetoed_run_without_yes_still_asks_a_human_and_carries_the_early_ids(
    rig: Rig, world: World
) -> None:
    config(rig, pre_transition=[(rig.hook(out='{"decision_id": "early.1"}'), BEFORE)])
    err = world.fail(new(), seams=Seams(interactive=lambda: False))
    assert (err.exit, err.code) == (13, "confirmation_required")
    assert details(err)["decision_ids"] == ["early.1"]
    assert world.writes() == []


def test_early_ids_reach_later_hooks_and_the_audit(rig: Rig, world: World) -> None:
    config(
        rig,
        pre_transition=[
            (rig.hook(out='{"decision_id": "early.1"}'), BEFORE),
            (rig.hook(out='{"decision_id": "late.1"}'), AFTER),
        ],
    )
    out = world.run(new(), seams=Dialog(rig).seams(), decision_ids=["cli.1"])
    early, late = (c["payload"] for c in rig.calls())
    assert early["decision_ids"] == ["cli.1"]
    assert late["decision_ids"] == ["cli.1", "early.1"]
    assert out["decision_ids"] == ["cli.1", "early.1", "late.1"]
    assert {tuple(r["decision_id"]) for r in world.records()} == {("cli.1", "early.1", "late.1")}


def test_a_before_confirm_escalation_stops_before_the_dialog_and_names_the_hook(
    rig: Rig, world: World
) -> None:
    config(
        rig,
        pre_transition=[
            (rig.hook(exit=13, out='{"decision_id": "esc.1"}'), BEFORE),
            (rig.hook(), AFTER),
        ],
    )
    dialog = Dialog(rig)
    err = world.fail(new(), seams=dialog.seams())
    assert (err.exit, err.code) == (13, "escalated")
    assert details(err)["hook"] == "pre-transition" and details(err)["decision_ids"] == ["esc.1"]
    assert dialog.seen == [] and [c["n"] for c in rig.calls()] == [1]  # nothing after it ran
    assert world.writes() == []


def test_the_override_lets_a_before_confirm_escalation_through_to_the_dialog(
    rig: Rig, world: World
) -> None:
    config(rig, pre_transition=[(rig.hook(exit=13), BEFORE), (rig.hook(), AFTER)])
    dialog = Dialog(rig)
    out = world.run(new(), seams=dialog.seams(), override_escalation="ticketctl")
    assert out["result"] == "ok" and dialog.seen == [1]


def test_the_override_never_overrides_a_before_confirm_veto(rig: Rig, world: World) -> None:
    config(rig, pre_transition=[(rig.hook(exit=1), BEFORE)])
    err = world.fail(new(), seams=Dialog(rig).seams(), override_escalation="ticketctl")
    assert err.code == "vetoed" and world.writes() == []


def test_a_decline_after_a_before_confirm_hook_runs_no_later_hook(rig: Rig, world: World) -> None:
    config(rig, pre_transition=[(rig.hook(), BEFORE), (rig.hook(), AFTER)])
    err = world.fail(new(), seams=Dialog(rig, answer=False).seams())
    assert (err.exit, err.code) == (20, "declined")
    assert [c["n"] for c in rig.calls()] == [1]
    assert world.writes() == [] and world.records() == []


def test_a_noop_runs_no_before_confirm_hook(rig: Rig, world: World) -> None:
    config(rig, pre_transition=[(rig.hook(), BEFORE)])
    out = world.run(HTransition("DEMO-3", "Done"), yes=True, expect_plan="0" * 64)
    assert out["result"] == "unchanged" and rig.calls() == []


def test_a_stale_expect_plan_runs_no_before_confirm_hook(rig: Rig, world: World) -> None:
    config(rig, pre_transition=[(rig.hook(), BEFORE)])
    err = world.fail(new(), yes=True, expect_plan="0" * 64)
    assert err.code == "stale_ticket" and rig.calls() == []


def test_a_flag_hook_is_always_after_the_confirmation(rig: Rig, world: World) -> None:
    flag = rig.hook()
    dialog = Dialog(rig)
    world.run(new(), seams=dialog.seams(), hooks=[f"pre-transition={flag}"])
    assert dialog.seen == [0]


def test_the_default_placement_is_after_the_confirmation(rig: Rig, world: World) -> None:
    config(rig, pre_transition=[(rig.hook(), "")])
    dialog = Dialog(rig)
    world.run(new(), seams=dialog.seams())
    assert dialog.seen == [0]


def test_a_missing_shell_refuses_for_a_before_confirm_hook(
    rig: Rig, world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    from ticketctl import hooks

    config(rig, pre_transition=[(rig.hook(), BEFORE)])
    monkeypatch.setattr(hooks, "SHELL", "/nonexistent/sh")
    err = world.fail(new(), seams=Dialog(rig).seams())
    assert (err.exit, err.code, err.reason) == (1, "internal", "hooks_unavailable")


def test_a_before_confirm_hook_of_another_family_does_not_run(rig: Rig, world: World) -> None:
    config(rig, pre_comment=[(rig.hook(), BEFORE)])
    world.run(new(), seams=Dialog(rig).seams())
    assert rig.calls() == []


def test_a_before_confirm_comment_hook_sees_hashes_not_text(rig: Rig, world: World) -> None:
    config(rig, pre_comment=[(rig.hook(), BEFORE)])
    action = HComment("DEMO-2", "the secret sentence of this comment", dedupe_key="d1")
    world.run(action, seams=Dialog(rig).seams())
    (call,) = rig.calls()
    validator("hooks/pre-comment.json").validate(call["payload"])
    assert "secret sentence" not in json.dumps(call)


# ---------------------------------------------------------------------------------------------
# --dry-run --run-hooks
# ---------------------------------------------------------------------------------------------


def test_a_plain_dry_run_still_runs_no_hook(rig: Rig, world: World) -> None:
    config(rig, pre_transition=[(rig.hook(), BEFORE), (rig.hook(), AFTER)])
    world.run(new(), dry_run=True)
    assert rig.calls() == []


def test_run_hooks_runs_every_pre_hook_in_the_real_order_and_never_a_post_hook(
    rig: Rig, world: World
) -> None:
    config(
        rig,
        pre_transition=[(rig.hook(), AFTER), (rig.hook(), BEFORE), (rig.hook(), "")],
        post_transition=[(rig.hook(), "")],
    )
    plain = world.hash_of(new())
    plan = world.run(new(), dry_run=True, run_hooks=True)
    assert plan["plan_sha256"] == plain and plan["steps"]
    # the before_confirm hook first, then the others in list order
    assert [c["n"] for c in rig.calls()] == [2, 1, 3]
    assert world.writes() == [] and world.records() == []


def test_a_dry_run_hook_is_told_so_by_its_environment_only(rig: Rig, world: World) -> None:
    config(rig, pre_transition=[(rig.hook(), "")])
    world.run(new(), dry_run=True, run_hooks=True)
    world.run(new(), seams=Dialog(rig).seams())
    dry, real = rig.calls()
    assert dry["env"]["TICKETCTL_DRY_RUN"] == "1"
    assert "TICKETCTL_DRY_RUN" not in real["env"]
    assert dry["payload"].keys() == real["payload"].keys()
    validator("hooks/pre-transition.json").validate(dry["payload"])


def test_a_dry_run_hook_does_not_inherit_the_callers_dry_run_variable(
    rig: Rig, world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("TICKETCTL_DRY_RUN", "1")
    config(rig, pre_transition=[(rig.hook(), "")])
    world.run(new(), seams=Dialog(rig).seams())
    (real,) = rig.calls()
    assert "TICKETCTL_DRY_RUN" not in real["env"]


def test_a_dry_run_veto_is_a_veto_result_with_the_plan_and_no_write(rig: Rig, world: World) -> None:
    cmd = rig.hook(exit=1, err="not now", out='{"decision_id": "v.1"}')
    config(rig, pre_transition=[(cmd, "")])
    err = world.fail(new(), dry_run=True, run_hooks=True)
    assert (err.exit, err.code) == (11, "vetoed")
    assert "dry run" in err.message
    assert details(err)["hook_stderr"] == "not now" and details(err)["decision_ids"] == ["v.1"]
    assert details(err)["plan"]["plan_sha256"] == world.hash_of(new())
    assert world.writes() == [] and world.records() == []


def test_a_dry_run_escalation_is_reported_and_the_override_gives_the_plan(
    rig: Rig, world: World
) -> None:
    config(rig, pre_transition=[(rig.hook(exit=13), BEFORE), (rig.hook(), AFTER)])
    err = world.fail(new(), dry_run=True, run_hooks=True)
    assert (err.exit, err.code) == (13, "escalated")
    assert [c["n"] for c in rig.calls()] == [1]
    plan = world.run(new(), dry_run=True, run_hooks=True, override_escalation="ticketctl")
    assert plan["plan_sha256"] == details(err)["plan"]["plan_sha256"]


def test_a_dry_run_noop_runs_no_hook(rig: Rig, world: World) -> None:
    config(rig, pre_transition=[(rig.hook(), BEFORE)])
    plan = world.run(HTransition("DEMO-3", "Done"), dry_run=True, run_hooks=True)
    assert plan["steps"] == [] and rig.calls() == []


def test_a_dry_run_refusal_runs_no_hook(rig: Rig, world: World) -> None:
    config(rig, pre_transition=[(rig.hook(), BEFORE)])
    err = world.fail(HTransition("DEMO-2", "Done", if_status="Done"), dry_run=True, run_hooks=True)
    assert err.code == "precondition_failed" and rig.calls() == []


def test_run_hooks_with_a_dry_run_takes_no_lock_and_asks_nobody(rig: Rig, world: World) -> None:
    config(rig, pre_transition=[(rig.hook(), "")])
    asked: list[Any] = []
    seams = Seams(
        interactive=lambda: True,
        confirm=lambda plan: asked.append(plan) or True,
        lock=lambda p, c: pytest.fail("a dry run takes no lock"),
    )
    world.run(new(), dry_run=True, run_hooks=True, seams=seams)
    assert asked == []


def test_read_only_mode_allows_a_dry_run_with_hooks_but_still_refuses_the_write(
    rig: Rig, world: World
) -> None:
    cmd = rig.hook()
    hooks = f"hooks:\n  pre-transition:\n    - cmd: {json.dumps(cmd)}\n"
    world.write_config("read_only: true\n" + hooks)
    plan = world.run(new(), dry_run=True, run_hooks=True)
    assert plan["steps"] and len(rig.calls()) == 1
    err = world.fail(new(), yes=True, expect_plan=plan["plan_sha256"])
    assert (err.exit, err.code) == (20, "read_only")
    assert len(rig.calls()) == 1


def test_a_dry_run_hook_timeout_is_a_veto_result(rig: Rig, world: World) -> None:
    world.write_config(
        f"hooks:\n  pre-transition:\n    - cmd: {json.dumps(rig.hook(sleep=30))}\n"
        "      timeout: 1\n"
    )
    err = world.fail(new(), dry_run=True, run_hooks=True)
    assert (err.exit, err.code) == (11, "vetoed") and "timed out" in err.message


def test_run_hooks_outside_a_dry_run_is_refused() -> None:
    from ticketctl import flags

    g, _ = flags.extract_globals(["--run-hooks"])
    assert g.run_hooks is True
    with pytest.raises(TicketctlError) as info:
        flags.check_globals(g, "write", "transition", resolve_actor=False)
    assert (info.value.exit, info.value.reason) == (2, "invalid_combination")
    g, _ = flags.extract_globals(["--run-hooks", "--dry-run"])
    flags.check_globals(g, "write", "transition", resolve_actor=False)


def test_run_hooks_on_a_read_command_is_refused() -> None:
    from ticketctl import flags

    g, _ = flags.extract_globals(["--run-hooks"])
    with pytest.raises(TicketctlError) as info:
        flags.check_globals(g, "read", "get", resolve_actor=False)
    assert (info.value.exit, info.value.reason) == (2, "invalid_argument")


def test_a_flag_hook_runs_in_a_dry_run_after_the_configured_ones(rig: Rig, world: World) -> None:
    config(rig, pre_transition=[(rig.hook(), AFTER), (rig.hook(), BEFORE)])
    flag = rig.hook()
    world.run(new(), dry_run=True, run_hooks=True, hooks=[f"pre-transition={flag}"])
    calls = rig.calls()
    assert [c["n"] for c in calls] == [2, 1, 3]  # the flag hook is the third, as in a real run
    assert all(c["env"]["TICKETCTL_DRY_RUN"] == "1" for c in calls)


def test_late_dry_run_hooks_get_the_early_hooks_decision_ids(rig: Rig, world: World) -> None:
    config(
        rig,
        pre_transition=[
            (rig.hook(out='{"decision_id": "early.1"}'), BEFORE),
            (rig.hook(), AFTER),
        ],
    )
    world.run(new(), dry_run=True, run_hooks=True, decision_ids=["cli.1"])
    early, late = (c["payload"] for c in rig.calls())
    assert early["decision_ids"] == ["cli.1"]
    assert late["decision_ids"] == ["cli.1", "early.1"]


def test_a_late_veto_in_a_dry_run_after_an_early_hook_passed(rig: Rig, world: World) -> None:
    config(
        rig,
        pre_transition=[
            (rig.hook(out='{"decision_id": "early.1"}'), BEFORE),
            (rig.hook(exit=1, err="late no"), AFTER),
        ],
    )
    err = world.fail(new(), dry_run=True, run_hooks=True)
    assert (err.exit, err.code) == (11, "vetoed") and "dry run" in err.message
    assert [c["n"] for c in rig.calls()] == [1, 2]
    assert details(err)["hook_stderr"] == "late no"
    assert details(err)["decision_ids"] == ["early.1"]
    assert details(err)["plan"]["plan_sha256"] == world.hash_of(new())
    assert world.writes() == [] and world.records() == []


def test_a_late_escalation_in_a_dry_run_is_reported_after_every_hook_ran(
    rig: Rig, world: World
) -> None:
    config(
        rig,
        pre_transition=[
            (rig.hook(), BEFORE),
            (rig.hook(exit=13, out='{"decision_id": "esc.1"}'), AFTER),
            (rig.hook(), AFTER),
        ],
    )
    err = world.fail(new(), dry_run=True, run_hooks=True)
    assert (err.exit, err.code) == (13, "escalated")
    assert details(err)["escalated_hooks"] == ["pre-transition"]
    assert details(err)["decision_ids"] == ["esc.1"]
    assert [c["n"] for c in rig.calls()] == [1, 2, 3]  # an after-confirm escalation stops nothing
    plan = world.run(new(), dry_run=True, run_hooks=True, override_escalation="ticketctl")
    assert plan["plan_sha256"] == details(err)["plan"]["plan_sha256"]
    assert world.writes() == [] and world.records() == []


def test_a_real_run_veto_does_not_say_dry_run(rig: Rig, world: World) -> None:
    config(rig, pre_transition=[(rig.hook(exit=1), BEFORE)])
    assert "dry run" not in world.fail(new(), seams=Dialog(rig).seams()).message
    config(rig, pre_transition=[(rig.hook(exit=1), AFTER)])
    assert "dry run" not in world.fail(new(), seams=Dialog(rig).seams()).message


# ---------------------------------------------------------------------------------------------
# What the real dialog says (the real runner, the real dialog on a pty)
# ---------------------------------------------------------------------------------------------

EARLY_LINE = "hooks placed before this question have run"
LATE_LINE = "pre- hooks run after your answer"


def test_the_real_dialog_says_the_early_hook_ran_when_one_did(
    rig: Rig,
    world: World,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    config(rig, pre_transition=[(rig.hook(), BEFORE)])
    with terminal(monkeypatch, b"y\n"):
        out = _run_real(world, new())
    assert out["result"] == "ok"
    err = capsys.readouterr().err
    assert EARLY_LINE in err and LATE_LINE not in err


def test_the_real_dialog_does_not_claim_early_hooks_when_none_is_configured_early(
    rig: Rig,
    world: World,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    config(rig, pre_transition=[(rig.hook(), AFTER)])
    with terminal(monkeypatch, b"y\n"):
        _run_real(world, new())
    err = capsys.readouterr().err
    assert LATE_LINE in err and EARLY_LINE not in err


def test_the_real_dialog_does_not_claim_early_hooks_with_no_hooks_at_all(
    rig: Rig,
    world: World,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    with terminal(monkeypatch, b"y\n"):
        _run_real(world, new())
    err = capsys.readouterr().err
    assert LATE_LINE in err and EARLY_LINE not in err


# ---------------------------------------------------------------------------------------------
# The hooks digest
# ---------------------------------------------------------------------------------------------


def test_the_hooks_digest_changes_only_when_a_gate_moves_before_the_confirmation(
    rig: Rig, world: World
) -> None:
    cmd = rig.hook()
    config(rig, pre_transition=[(cmd, "")])
    default = writeflow.hooks_digest(world.rt(), "transition")
    config(rig, pre_transition=[(cmd, AFTER)])
    assert writeflow.hooks_digest(world.rt(), "transition") == default  # byte-identical
    config(rig, pre_transition=[(cmd, BEFORE)])
    assert writeflow.hooks_digest(world.rt(), "transition") != default
