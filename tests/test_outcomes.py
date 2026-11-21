"""Partial and ``outcome_unknown`` results and the audit failure handling (issue #26).

Every ambiguous write outcome must be ``outcome_unknown`` and never be sent twice; a failed audit
write must never hide a landed write; a partial result names exactly the steps that landed. The
faults are the fake provider's (resets, drops after the headers, 5xx, slow and dripping answers).
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

import pytest

from tests.fake_provider import ME_ACCOUNT, FakeProvider, Fault
from tests.golden import validator
from tests.test_writeflow import ATTEMPT, Hooks, World
from tests.write_stub import StubComment, StubTransition
from ticketctl import audit
from ticketctl.errors import TicketctlError
from ticketctl.plan import Planned, Step
from ticketctl.state import StateError
from ticketctl.writeflow import Context, Seams, StepResult, WriteGateway


@pytest.fixture
def world(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fake: FakeProvider) -> World:
    return World(tmp_path, monkeypatch, fake, config_extra="    timeouts:\n      read: 1s\n")


def two_steps() -> StubTransition:
    """DEMO-1 is unassigned: an assign step then a transition step."""
    return StubTransition("DEMO-1", "In Progress", assign_me=True)


def transitions_sent(world: World) -> int:
    return len([c for c in world.writes() if c.path.endswith("/transitions")])


AMBIGUOUS = [
    pytest.param(Fault("reset"), id="reset-before-apply"),
    pytest.param(Fault("reset", after_apply=True), id="reset-after-apply"),
    pytest.param(Fault("close"), id="close-before-apply"),
    pytest.param(Fault("close", after_apply=True), id="close-after-apply"),
    pytest.param(Fault("headers_only"), id="dropped-after-headers"),
    pytest.param(Fault("truncated"), id="truncated-body"),
    pytest.param(Fault("status", status=500, after_apply=True), id="500"),
    pytest.param(Fault("status", status=502), id="502"),
    pytest.param(Fault("status", status=503, after_apply=True), id="503"),
    pytest.param(Fault("status", status=504), id="504"),
    pytest.param(Fault("slow", delay_s=3.0, after_apply=True), id="slower-than-read-timeout"),
]


def arm(fault: Fault) -> Fault:
    return Fault(
        fault.kind,
        method="POST",
        path="/transitions",
        times=1,
        after_apply=fault.after_apply,
        status=fault.status,
        delay_s=fault.delay_s,
    )


@pytest.mark.parametrize("fault", AMBIGUOUS)
def test_every_ambiguous_single_step_write_is_unknown_and_never_resent(
    world: World, fault: Fault
) -> None:
    hooks = Hooks()
    world.fake.inject(arm(fault))
    held = world.hash_of(StubTransition("DEMO-2", "Done"))
    err = world.fail(
        StubTransition("DEMO-2", "Done"), yes=True, expect_plan=held, seams=Seams(hooks=hooks)
    )
    assert (err.exit, err.code, err.outcome_unknown) == (12, "outcome_unknown", True)
    assert err.details is not None and err.details["attempt_id"] == ATTEMPT
    assert "result" not in err.details
    assert transitions_sent(world) <= 1, "an ambiguous write is never retried"
    records = world.records()
    assert [r["result"] for r in records] == ["attempted", "unknown"]
    assert records[1]["changed"] is None and records[1]["reason"] == "outcome_unknown"
    assert [(p.result, p.changed) for p in hooks.post_calls] == [("unknown", None)]
    assert hooks.post_calls[0].error == {"code": "outcome_unknown", "reason": None}
    validator("cli/error.json").validate(err.envelope())


@pytest.mark.parametrize("fault", AMBIGUOUS)
def test_every_ambiguous_second_step_is_partial_with_unknown_and_not_resent(
    world: World, fault: Fault
) -> None:
    hooks = Hooks()
    world.fake.inject(arm(fault))
    held = world.hash_of(two_steps())
    err = world.fail(two_steps(), yes=True, expect_plan=held, seams=Seams(hooks=hooks))
    assert (err.exit, err.code, err.outcome_unknown) == (12, "partial", True)
    assert err.retryable is True
    assert err.details is not None and err.details["attempt_id"] == ATTEMPT
    result = err.details["result"]
    assert [(s["kind"], s["status"], s.get("code")) for s in result["steps"]] == [
        ("assign", "done", None),
        ("transition", "failed", "outcome_unknown"),
    ]
    assert result["result"] == "partial" and result["changed"] is True
    assert transitions_sent(world) <= 1
    assert world.fake.tickets["DEMO-1"].assignee == ME_ACCOUNT
    assert [(r["step"], r["result"]) for r in world.records()] == [
        (1, "attempted"), (1, "ok"), (2, "attempted"), (2, "unknown"),
    ]  # fmt: skip
    assert [(p.result, p.changed) for p in hooks.post_calls] == [("partial", None)]
    validator("cli/result.json").validate(result)
    validator("cli/error.json").validate(err.envelope())


def test_an_unknown_first_step_skips_the_rest_and_is_not_partial(world: World) -> None:
    world.fake.inject(Fault("reset", method="PUT", path="/assignee", after_apply=True))
    held = world.hash_of(two_steps())
    err = world.fail(two_steps(), yes=True, expect_plan=held)
    assert (err.code, err.outcome_unknown) == ("outcome_unknown", True)
    assert err.details is not None and "result" not in err.details
    assert transitions_sent(world) == 0  # nothing after an unknown step is sent
    assert [r["result"] for r in world.records()] == ["attempted", "unknown"]


def test_a_rate_limited_second_step_is_partial_and_copies_the_retry_hint(world: World) -> None:
    world.fake.quirks.retry_after_format = "seconds"
    world.fake.inject(
        Fault(
            "status", method="POST", path="/transitions", status=429, retry_after=3600, times=None
        )
    )
    held = world.hash_of(two_steps())
    err = world.fail(two_steps(), yes=True, expect_plan=held)
    assert (err.code, err.outcome_unknown, err.retryable) == ("partial", False, True)
    assert err.retry_after_s == 3600
    assert err.details is not None
    assert err.details["result"]["steps"][1]["code"] == "rate_limited"
    assert world.records()[-1]["result"] == "partial"
    assert world.records()[-1]["changed"] is False


def test_a_definite_failure_after_two_landed_steps_lists_exactly_the_landed_ones(
    world: World,
) -> None:
    class Three(StubTransition):
        def reduce(self, ctx: Context, *, fresh: bool) -> Planned:
            planned = super().reduce(ctx, fresh=fresh)

            def ok(gw: WriteGateway) -> StepResult:
                return StepResult([])

            return Planned(
                steps=[*planned.steps[:1], Step("set_fields", ok, {}), *planned.steps[1:],
                       Step("assign", ok, {})],
                intent=planned.intent, facts=planned.facts, changes=planned.changes,
                key=planned.key, shown=planned.shown,
            )  # fmt: skip

    held = world.hash_of(Three("DEMO-1", "In Progress", assign_me=True, transition_id="99"))
    err = world.fail(
        Three("DEMO-1", "In Progress", assign_me=True, transition_id="99"),
        yes=True,
        expect_plan=held,
    )
    assert err.code == "partial" and err.details is not None
    steps = err.details["result"]["steps"]
    assert [(s["kind"], s["status"]) for s in steps] == [
        ("assign", "done"),
        ("set_fields", "done"),
        ("transition", "failed"),
        ("assign", "skipped"),
    ]
    assert len(world.records()) == 6  # 3 pairs: the skipped step has no record


# ---------------------------------------------------------------------------------------------
# The deadline
# ---------------------------------------------------------------------------------------------


class Sleepy(StubTransition):
    """The assign step takes longer than the command's deadline."""

    def _assign(self, account: str) -> Step:
        step = super()._assign(account)
        real = step.send

        def send(gw: WriteGateway) -> StepResult:
            out = real(gw)
            time.sleep(0.8)
            return out

        step.send = send
        return step


def test_expiry_before_the_second_write_is_timeout_partial_not_unknown(world: World) -> None:
    held = world.hash_of(Sleepy("DEMO-1", "In Progress", assign_me=True))
    err = world.fail(
        Sleepy("DEMO-1", "In Progress", assign_me=True),
        yes=True,
        expect_plan=held,
        timeout_s=0.5,
    )
    assert (err.code, err.outcome_unknown, err.retryable) == ("partial", False, True)
    assert err.details is not None
    assert err.details["result"]["steps"][1] == {
        "kind": "transition",
        "status": "failed",
        "code": "timeout",
    }
    assert transitions_sent(world) == 0


def test_a_dripping_answer_cut_by_the_deadline_is_unknown(world: World) -> None:
    world.fake.inject(arm(Fault("drip", delay_s=0.3)))
    held = world.hash_of(StubTransition("DEMO-2", "Done"))
    err = world.fail(StubTransition("DEMO-2", "Done"), yes=True, expect_plan=held, timeout_s=0.8)
    assert (err.code, err.outcome_unknown) == ("outcome_unknown", True)
    assert transitions_sent(world) <= 1


def test_expiry_while_the_write_is_in_flight_is_unknown_never_timeout(world: World) -> None:
    world.fake.inject(arm(Fault("slow", delay_s=2.0, after_apply=True)))
    held = world.hash_of(StubTransition("DEMO-2", "Done"))
    err = world.fail(StubTransition("DEMO-2", "Done"), yes=True, expect_plan=held, timeout_s=0.5)
    assert (err.code, err.outcome_unknown) == ("outcome_unknown", True)
    assert world.records()[-1]["result"] == "unknown"


def test_time_spent_in_the_prompt_and_the_pre_hooks_does_not_count(world: World) -> None:
    def slow_confirm(plan: dict[str, Any]) -> bool:
        time.sleep(0.8)
        return True

    out = world.run(
        StubTransition("DEMO-2", "Done"),
        timeout_s=0.5,
        seams=Seams(interactive=lambda: True, confirm=slow_confirm),
    )
    assert out["result"] == "ok"

    class SlowHooks(Hooks):
        def pre(self, plan: Any, planned: Any, attempt_id: Any, decision_ids: Any) -> Any:
            time.sleep(0.8)
            return super().pre(plan, planned, attempt_id, decision_ids)

    held = world.hash_of(two_steps())
    out = world.run(
        two_steps(),
        yes=True,
        expect_plan=held,
        timeout_s=0.5,
        seams=Seams(hooks=SlowHooks()),
    )
    assert out["result"] == "ok"


# ---------------------------------------------------------------------------------------------
# Audit failures
# ---------------------------------------------------------------------------------------------


def fail_when(world: World, predicate: Any, *, exc: BaseException | None = None) -> list[Any]:
    real = audit.append_audit
    seen: list[Any] = []

    def flaky(state_dir: Path, record: Any, **kw: Any) -> Any:
        seen.append((record["step"], record["result"]))
        if predicate(record):
            raise exc or StateError("state_io", "disk")
        return real(state_dir, record, **kw)

    world.mp.setattr(audit, "append_audit", flaky)
    return seen


def test_a_failed_attempted_append_sends_nothing_and_runs_no_post_hook(world: World) -> None:
    hooks = Hooks()
    fail_when(world, lambda r: True)
    held = world.hash_of(two_steps())
    err = world.fail(two_steps(), yes=True, expect_plan=held, seams=Seams(hooks=hooks))
    assert (err.exit, err.code, err.reason) == (1, "internal", "audit_failed")
    assert world.writes() == []
    assert err.details is None or "attempt_id" not in err.details
    assert hooks.post_calls == []
    assert world.records() == []


def test_a_failed_outcome_append_of_a_landed_first_step_marks_the_partial_result(
    world: World, capsys: pytest.CaptureFixture[str]
) -> None:
    fail_when(world, lambda r: r["step"] == 1 and r["result"] == "ok")
    held = world.hash_of(
        StubTransition("DEMO-1", "In Progress", assign_me=True, transition_id="99")
    )
    err = world.fail(
        StubTransition("DEMO-1", "In Progress", assign_me=True, transition_id="99"),
        yes=True,
        expect_plan=held,
    )
    assert err.code == "partial" and err.details is not None
    result = err.details["result"]
    assert "audit_incomplete" in result["warnings"]
    assert result["steps"][0]["status"] == "done"
    assert "audit" in capsys.readouterr().err
    validator("cli/result.json").validate(result)


def test_a_failed_outcome_append_never_changes_an_unknown_into_anything_else(
    world: World, capsys: pytest.CaptureFixture[str]
) -> None:
    fail_when(world, lambda r: r["result"] == "unknown")
    world.fake.inject(arm(Fault("reset", after_apply=True)))
    held = world.hash_of(StubTransition("DEMO-2", "Done"))
    err = world.fail(StubTransition("DEMO-2", "Done"), yes=True, expect_plan=held)
    assert (err.code, err.outcome_unknown) == ("outcome_unknown", True)
    assert "audit" in capsys.readouterr().err
    assert [r["result"] for r in world.records()] == ["attempted"]


def test_a_failed_outcome_append_of_a_failed_write_keeps_the_real_error(world: World) -> None:
    fail_when(world, lambda r: r["result"] == "failed")
    held = world.hash_of(StubTransition("DEMO-2", "Done", transition_id="99"))
    err = world.fail(
        StubTransition("DEMO-2", "Done", transition_id="99"), yes=True, expect_plan=held
    )
    assert err.code == "rejected" and err.details is not None
    assert err.details["attempt_id"] == ATTEMPT


def test_every_outcome_append_failing_still_reports_a_full_ok(world: World) -> None:
    fail_when(world, lambda r: r["result"] != "attempted")
    held = world.hash_of(two_steps())
    out = world.run(two_steps(), yes=True, expect_plan=held)
    assert out["result"] == "ok" and out["warnings"].count("audit_incomplete") == 1
    assert [s["status"] for s in out["steps"]] == ["done", "done"]
    assert world.fake.tickets["DEMO-1"].status == "In Progress"


# ---------------------------------------------------------------------------------------------
# Interrupts and unexpected errors
# ---------------------------------------------------------------------------------------------


def test_an_interrupt_between_the_steps_reports_the_landed_step_to_the_post_hook(
    world: World,
) -> None:
    hooks = Hooks()
    fail_when(
        world,
        lambda r: r["step"] == 2 and r["result"] == "attempted",
        exc=KeyboardInterrupt(),
    )
    held = world.hash_of(two_steps())
    with pytest.raises(KeyboardInterrupt):
        world.run(two_steps(), yes=True, expect_plan=held, seams=Seams(hooks=hooks))
    assert transitions_sent(world) == 0
    assert [p.result for p in hooks.post_calls] == ["partial"]
    assert [s["status"] for s in hooks.post_calls[0].steps] == ["done", "failed"]


def test_an_interrupt_in_the_outcome_append_after_the_send_still_runs_the_post_hook(
    world: World,
) -> None:
    hooks = Hooks()
    fail_when(world, lambda r: r["result"] == "ok", exc=KeyboardInterrupt())
    held = world.hash_of(StubTransition("DEMO-2", "Done"))
    with pytest.raises(KeyboardInterrupt):
        world.run(
            StubTransition("DEMO-2", "Done"), yes=True, expect_plan=held, seams=Seams(hooks=hooks)
        )
    assert world.fake.tickets["DEMO-2"].status == "Done"
    assert [p.result for p in hooks.post_calls] == ["ok"]


def test_an_interrupt_before_any_send_runs_no_post_hook_and_sends_nothing(world: World) -> None:
    hooks = Hooks()
    fail_when(world, lambda r: True, exc=KeyboardInterrupt())
    held = world.hash_of(StubTransition("DEMO-2", "Done"))
    with pytest.raises(KeyboardInterrupt):
        world.run(
            StubTransition("DEMO-2", "Done"), yes=True, expect_plan=held, seams=Seams(hooks=hooks)
        )
    assert world.writes() == [] and hooks.post_calls == []


@pytest.mark.parametrize("exc", [MemoryError(), RecursionError(), OSError("x"), ValueError("y")])
def test_any_unexpected_exception_in_a_send_is_unknown_without_a_traceback(
    world: World, exc: Exception
) -> None:
    from tests.test_writeflow import Boom

    held = world.hash_of(Boom(exc, "DEMO-2", "Done"))
    err = world.fail(Boom(exc, "DEMO-2", "Done"), yes=True, expect_plan=held)
    assert (err.code, err.outcome_unknown, err.exit) == ("outcome_unknown", True, 12)
    assert type(exc).__name__ in err.message
    assert "Traceback" not in json.dumps(err.envelope())


def test_a_second_step_that_raises_unexpectedly_is_partial_unknown(world: World) -> None:
    class Second(StubTransition):
        def _transition(self) -> Step:
            def send(gw: WriteGateway) -> StepResult:
                raise RuntimeError("boom")

            return Step("transition", send, {"transition_id": "41", "to": self.to})

    held = world.hash_of(Second("DEMO-1", "In Progress", assign_me=True))
    err = world.fail(Second("DEMO-1", "In Progress", assign_me=True), yes=True, expect_plan=held)
    assert (err.code, err.outcome_unknown) == ("partial", True)
    assert err.details is not None
    assert err.details["result"]["steps"][1]["code"] == "outcome_unknown"


def test_a_keyless_comment_unknown_is_not_retryable_and_a_keyed_one_is(world: World) -> None:
    for key, retry in ((None, False), ("kk-1", True)):
        world.fake.inject(Fault("headers_only", method="POST", path="/comment"))
        action = StubComment("DEMO-1", f"body {key}", dedupe_key=key)
        err = world.fail(action, seams=Seams(interactive=lambda: True, confirm=lambda p: True))
        assert (err.code, err.retryable, err.outcome_unknown) == ("outcome_unknown", retry, True)
        assert ("--dedupe-key" in err.message) is (key is None)


def test_the_lock_is_released_on_every_outcome_path(world: World) -> None:
    from tests.test_writeflow import CountingLock

    class Keyed(StubTransition):
        def reduce(self, ctx: Context, *, fresh: bool) -> Planned:
            import dataclasses

            return dataclasses.replace(super().reduce(ctx, fresh=fresh), dedupe_key="lk")

    scenarios: list[Fault | None] = [
        None,
        arm(Fault("reset", after_apply=True)),
        arm(Fault("status", status=500)),
    ]
    for fault in scenarios:
        lock = CountingLock()
        if fault is not None:
            world.fake.inject(fault)
        world.fake.tickets["DEMO-2"].status = "In Progress"
        try:
            world.run(
                Keyed("DEMO-2", "Done"),
                yes=True,
                expect_plan=world.hash_of(Keyed("DEMO-2", "Done")),
                seams=Seams(lock=lambda p, c, lock=lock: lock),
            )
        except TicketctlError:
            pass
        assert (lock.enters, lock.exits) == (1, 1)

    for broken in (lambda r: r["result"] != "attempted", lambda r: r["result"] == "attempted"):
        lock = CountingLock()
        fail_when(world, broken)
        world.fake.tickets["DEMO-2"].status = "In Progress"
        try:
            world.run(
                Keyed("DEMO-2", "Done"),
                yes=True,
                expect_plan=world.hash_of(Keyed("DEMO-2", "Done")),
                seams=Seams(lock=lambda p, c, lock=lock: lock),
            )
        except TicketctlError:
            pass
        assert (lock.enters, lock.exits) == (1, 1)


def test_results_and_audit_never_carry_the_token_or_provider_text(world: World) -> None:
    from tests.fake_provider import SITE_TOKEN

    world.fake.inject(
        Fault(
            "status",
            method="POST",
            path="/transitions",
            status=500,
            body=f"echo {SITE_TOKEN} Authorization: Basic abc",
        )  # fmt: skip
    )
    held = world.hash_of(two_steps())
    err = world.fail(two_steps(), yes=True, expect_plan=held)
    raw = json.dumps(err.envelope()) + (world.state / "audit.jsonl").read_text()
    assert SITE_TOKEN not in raw


def test_a_paused_deadline_gets_the_paused_time_back() -> None:
    from ticketctl.gateway import Deadline

    now = [0.0]
    deadline = Deadline(10.0, clock=lambda: now[0])
    with deadline.paused():
        now[0] = 50.0
    assert deadline.remaining() == 10.0
    now[0] = 5.0
    other = Deadline(10.0, clock=lambda: now[0])
    with pytest.raises(RuntimeError), other.paused():  # a raise inside still resumes the clock
        now[0] = 9.0
        raise RuntimeError
    assert other.remaining() == 10.0
