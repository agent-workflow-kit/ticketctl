"""The write flow engine against the fake provider with stub actions (issue #23).

Covers the eight-step order, idempotency first, the plan hash, ``--dry-run`` edges, the audit
pair around every send, outcome_unknown and partial results, the account check, secrets and the
nothing-unvalidated-reaches-the-gateway rule. The stub actions live in ``tests/write_stub.py``.
"""

from __future__ import annotations

import contextlib
import dataclasses
import hashlib
import json
import re
from pathlib import Path
from types import MappingProxyType
from typing import Any

import pytest

from tests.fake_provider import (
    ME_ACCOUNT,
    OTHER_ACCOUNT,
    SITE_EMAIL,
    SITE_TOKEN,
    FakeProvider,
    Fault,
)
from tests.golden import validator
from tests.write_stub import StubComment, StubDescription, StubTransition
from ticketctl import audit, cache, writeflow
from ticketctl.errors import TicketctlError
from ticketctl.flags import Globals
from ticketctl.plan import Planned, Step
from ticketctl.runtime import Runtime, load_runtime
from ticketctl.sanitize import InputError
from ticketctl.state import StateError
from ticketctl.writeflow import (
    Context,
    HookResult,
    PostInfo,
    Seams,
    StepResult,
    WriteGateway,
    check_outbound,
    execute,
)

TOKEN_VAR = "TICKETCTL_TEST_TOKEN"
ATTEMPT = "0123456789abcdef"
ZERO = "0" * 64


class World:
    def __init__(
        self, root: Path, mp: pytest.MonkeyPatch, fake: FakeProvider, config_extra: str = ""
    ) -> None:
        self.root = root
        self.mp = mp
        self.fake = fake
        self.config = root / "config.yaml"
        self.state = root / "state" / "ticketctl"
        mp.setenv("XDG_STATE_HOME", str(root / "state"))
        mp.setenv(TOKEN_VAR, SITE_TOKEN)
        self.write_config(config_extra)

    def write_config(self, extra: str = "") -> None:
        text = (
            "sites:\n  main:\n"
            f"    url: {self.fake.url}\n"
            f"    email: {SITE_EMAIL}\n"
            f"    token_env: {TOKEN_VAR}\n"
            "    allow_http: true\n" + extra
        )
        self.config.write_text(text, encoding="utf-8")
        self.config.chmod(0o600)

    def rt(self, actor: str = "human:tester", source: str = "flag", **flags: Any) -> Runtime:
        g = Globals(config=str(self.config), resolved_actor=actor, actor_source=source, **flags)
        return load_runtime(g)

    def run(
        self, action: Any, *, seams: Seams | None = None, actor: str = "human:tester", **flags: Any
    ) -> dict[str, Any]:
        seams = seams or Seams()
        seams.interactive = seams.interactive or (lambda: False)
        seams.attempt_id = lambda: ATTEMPT
        return execute(self.rt(actor, **flags), "main", action, seams)

    def fail(self, action: Any, **kw: Any) -> TicketctlError:
        with pytest.raises(TicketctlError) as info:
            self.run(action, **kw)
        return info.value

    def records(self) -> list[dict[str, Any]]:
        path = self.state / "audit.jsonl"
        if not path.exists():
            return []
        return [json.loads(line) for line in path.read_text().splitlines()]

    def writes(self) -> list[Any]:
        return [c for c in self.fake.calls if c.method != "GET"]

    def hash_of(self, action: Any, **kw: Any) -> str:
        return self.run(action, dry_run=True, **kw)["plan_sha256"]


@pytest.fixture
def world(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fake: FakeProvider) -> World:
    return World(tmp_path, monkeypatch, fake)


class Hooks:
    """Records the hook calls; ``pre`` may veto, escalate, mutate the world or add ids."""

    def __init__(self) -> None:
        self.pre_calls: list[dict[str, Any]] = []
        self.post_calls: list[PostInfo] = []
        self.ids: list[str] = []
        self.escalate: list[str] = []
        self.veto: TicketctlError | None = None
        self.before: Any = None  # called inside pre
        self.post_raises = False

    def pre(self, plan: dict[str, Any], planned: Planned, attempt_id: str, decision_ids: list[str]):
        self.pre_calls.append(plan)
        if self.before:
            self.before()
        if self.veto:
            raise self.veto
        return HookResult(list(self.ids), list(self.escalate))

    def post(self, plan, planned, attempt_id, decision_ids, info: PostInfo) -> None:
        self.post_calls.append(info)
        if self.post_raises:
            raise RuntimeError("hook crashed")


# ---------------------------------------------------------------------------------------------
# Idempotency first, the hash, dry-run
# ---------------------------------------------------------------------------------------------


def test_rerun_after_landed_change_is_unchanged_whatever_expect_plan_says(world: World) -> None:
    held = world.hash_of(StubTransition("DEMO-2", "Done"))
    first = world.run(StubTransition("DEMO-2", "Done"), yes=True, expect_plan=held)
    assert first["result"] == "ok" and first["changed"] is True
    before = (len(world.records()), len(world.writes()))
    for flags in ({"yes": True, "expect_plan": held}, {"yes": True, "expect_plan": ZERO}, {}):
        again = world.run(StubTransition("DEMO-2", "Done"), **flags)
        assert again["result"] == "unchanged" and again["changed"] is False
        assert again["steps"] == [] and again["attempt_id"] is None
    assert (len(world.records()), len(world.writes())) == before


def test_unchanged_runs_no_check_hook_prompt_or_audit(world: World) -> None:
    hooks = Hooks()
    action = StubTransition("DEMO-2", "In Progress", if_status="Backlog")
    out = world.run(action, seams=Seams(hooks=hooks, confirm=lambda p: pytest.fail("prompt")))
    assert out["result"] == "unchanged"
    assert action.checked == 0  # --if-status never runs while no step remains
    assert hooks.pre_calls == [] and hooks.post_calls == [] and world.records() == []


def test_plan_hash_has_no_actor_but_binds_the_attribution(
    world: World, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fake: FakeProvider
) -> None:
    auto = world.hash_of(StubComment("DEMO-1", "same body"), actor="auto:bot")
    human = world.hash_of(StubComment("DEMO-1", "same body"), actor="human:someone")
    assert auto == human
    plan = world.run(StubComment("DEMO-1", "same body"), actor="auto:bot", dry_run=True)
    assert plan["actor"] == "auto:bot"
    world.write_config("attribution:\n  enabled: false\n")
    plain = world.hash_of(StubComment("DEMO-1", "same body"), actor="human:someone")
    assert plain != human
    # An auto actor cannot switch the paragraph off: the config is ignored for it.
    assert world.hash_of(StubComment("DEMO-1", "same body"), actor="auto:bot") == auto


def test_takeover_between_plan_and_apply_is_stale(world: World) -> None:
    held = world.hash_of(StubTransition("DEMO-2", "Done"))
    world.fake.tickets["DEMO-2"].assignee = OTHER_ACCOUNT
    err = world.fail(StubTransition("DEMO-2", "Done"), yes=True, expect_plan=held)
    assert (err.exit, err.code, err.reason) == (20, "stale_ticket", "stale_ticket")
    assert err.details is not None and err.details["plan"]["plan_sha256"] != held
    assert world.writes() == [] and world.records() == []


def test_takeover_after_the_hooks_is_stale_with_the_new_plan(world: World) -> None:
    held = world.hash_of(StubTransition("DEMO-2", "Done"))
    hooks = Hooks()
    hooks.before = lambda: setattr(world.fake.tickets["DEMO-2"], "assignee", OTHER_ACCOUNT)
    err = world.fail(
        StubTransition("DEMO-2", "Done"), yes=True, expect_plan=held, seams=Seams(hooks=hooks)
    )
    assert err.code == "stale_ticket" and err.details is not None
    assert err.details["plan"]["plan_sha256"] != held
    assert world.writes() == [] and world.records() == []
    assert hooks.post_calls == []  # no attempted record: no post hook


def test_an_own_assign_step_appearing_or_disappearing_keeps_the_hash(world: World) -> None:
    with_assign = world.run(StubTransition("DEMO-1", "In Progress", assign_me=True), dry_run=True)
    assert [s["kind"] for s in with_assign["steps"]] == ["assign", "transition"]
    world.fake.tickets["DEMO-1"].assignee = ME_ACCOUNT
    without = world.run(StubTransition("DEMO-1", "In Progress", assign_me=True), dry_run=True)
    assert [s["kind"] for s in without["steps"]] == ["transition"]
    assert without["plan_sha256"] == with_assign["plan_sha256"]
    # Applying the held hash runs the plan as the human saw it, whatever the fresh read shows.
    out = world.run(
        StubTransition("DEMO-1", "In Progress", assign_me=True),
        yes=True,
        expect_plan=with_assign["plan_sha256"],
    )
    assert out["result"] == "ok"


def test_yes_needs_expect_plan_and_dry_run_yes_prints_the_plan(world: World) -> None:
    err = world.fail(StubTransition("DEMO-2", "Done"), yes=True)
    assert (err.exit, err.code, err.reason) == (2, "usage", "yes_requires_expect_plan")
    assert world.fake.calls == []
    plan = world.run(StubTransition("DEMO-2", "Done"), yes=True, dry_run=True)
    assert plan["steps"] and "plan_sha256" in plan


def test_yes_needs_a_dedupe_key_for_comment(world: World) -> None:
    err = world.fail(StubComment("DEMO-1", "hi"), yes=True, expect_plan=ZERO)
    assert (err.code, err.reason) == ("usage", "dedupe_key_required")
    assert world.fake.calls == []


def test_dry_run_edges(world: World) -> None:
    hooks = Hooks()
    locks: list[str] = []

    def lock(planned: Planned, ctx: Context):  # pragma: no cover - must not run
        locks.append("taken")
        return contextlib.nullcontext()

    seams = Seams(hooks=hooks, lock=lock, confirm=lambda p: pytest.fail("prompt"))
    plan = world.run(
        StubComment("DEMO-1", "hello", dedupe_key="k1"),
        dry_run=True,
        yes=True,
        expect_plan=ZERO,  # ignored
        seams=seams,
    )
    assert plan["steps"][0]["kind"] == "add_comment"
    assert hooks.pre_calls == [] and hooks.post_calls == [] and locks == []
    assert world.records() == [] and world.writes() == []


def test_dry_run_of_a_noop_prints_an_empty_plan_with_the_dedupe_match(world: World) -> None:
    held = world.hash_of(StubComment("DEMO-1", "hello", dedupe_key="k1"))
    world.run(StubComment("DEMO-1", "hello", dedupe_key="k1"), yes=True, expect_plan=held)
    plan = world.run(StubComment("DEMO-1", "hello", dedupe_key="k1"), dry_run=True)
    assert plan["steps"] == [] and plan["dedupe_match"] is not None
    again = world.run(StubComment("DEMO-1", "hello", dedupe_key="k1"), yes=True, expect_plan=ZERO)
    assert again["result"] == "unchanged" and again["already_posted"] is True
    assert again["comment_id"] == plan["dedupe_match"]
    assert len([c for c in world.fake.tickets["DEMO-1"].comments]) == 1


def test_dry_run_shows_refusals(world: World) -> None:
    err = world.fail(StubTransition("DEMO-1", "Done", if_status="Done"), dry_run=True)
    assert err.code == "precondition_failed" and err.exit == 20


def test_plan_and_result_validate_against_the_schemas(world: World) -> None:
    plan = world.run(StubTransition("DEMO-2", "Done"), dry_run=True)
    validator("cli/plan.json").validate(plan)
    result = world.run(StubTransition("DEMO-2", "Done"), yes=True, expect_plan=plan["plan_sha256"])
    validator("cli/result.json").validate(result)
    empty = world.run(StubTransition("DEMO-2", "Done"), dry_run=True)
    assert empty["steps"] == []
    validator("cli/plan.json").validate(empty)
    validator("cli/result.json").validate(world.run(StubTransition("DEMO-2", "Done")))


# ---------------------------------------------------------------------------------------------
# Confirmation, hooks, the gate decisions
# ---------------------------------------------------------------------------------------------


def test_non_interactive_without_yes_is_confirmation_required(world: World) -> None:
    err = world.fail(StubTransition("DEMO-2", "Done"), decision_ids=["flag.1"])
    assert (err.exit, err.code) == (13, "confirmation_required")
    assert err.details is not None
    assert err.details["plan"]["plan_sha256"] and err.details["decision_ids"] == ["flag.1"]
    assert world.writes() == [] and world.records() == []


def test_interactive_accept_and_decline(world: World) -> None:
    seen: list[dict[str, Any]] = []

    def accept(plan: dict[str, Any]) -> bool:
        seen.append(plan)
        return True

    out = world.run(
        StubTransition("DEMO-2", "Done"), seams=Seams(interactive=lambda: True, confirm=accept)
    )
    assert out["result"] == "ok" and seen[0]["steps"]
    err = world.fail(
        StubTransition("DEMO-3", "Backlog"),
        seams=Seams(interactive=lambda: True, confirm=lambda p: False),
    )
    assert (err.exit, err.code, err.reason) == (20, "declined", "declined")
    assert err.details is not None and "plan" in err.details
    assert len(world.records()) == 2  # only the accepted write was audited


def test_interactive_without_a_dialog_is_confirmation_required(world: World) -> None:
    err = world.fail(StubTransition("DEMO-2", "Done"), seams=Seams(interactive=lambda: True))
    assert err.code == "confirmation_required"


def test_expect_plan_mismatch_refuses_with_the_plan(world: World) -> None:
    err = world.fail(StubTransition("DEMO-2", "Done"), yes=True, expect_plan=ZERO)
    assert err.code == "stale_ticket" and err.details is not None
    assert err.details["plan"]["plan_sha256"] != ZERO
    assert world.writes() == [] and world.records() == []


def test_veto_escalation_and_override(world: World) -> None:
    held = world.hash_of(StubTransition("DEMO-2", "Done"))
    hooks = Hooks()
    hooks.veto = TicketctlError("vetoed", "no", details={"hook": "pre-transition"})
    err = world.fail(
        StubTransition("DEMO-2", "Done"), yes=True, expect_plan=held, seams=Seams(hooks=hooks)
    )
    assert err.code == "vetoed" and world.records() == [] and world.writes() == []
    assert err.details is not None and err.details["plan"]["plan_sha256"] == held
    assert err.details["hook"] == "pre-transition"

    hooks = Hooks()
    hooks.escalate = ["pre-transition"]
    hooks.ids = ["hook.7"]
    err = world.fail(
        StubTransition("DEMO-2", "Done"), yes=True, expect_plan=held, seams=Seams(hooks=hooks)
    )
    assert (err.exit, err.code) == (13, "escalated")
    assert err.details is not None
    assert err.details["decision_ids"] == ["hook.7"] and err.details["hook"] == "pre-transition"
    assert world.records() == [] and hooks.post_calls == []

    out = world.run(
        StubTransition("DEMO-2", "Done"),
        yes=True,
        expect_plan=held,
        override_escalation="ticketctl",
        seams=Seams(hooks=hooks),
    )
    assert out["result"] == "ok" and out["decision_ids"] == ["hook.7"]
    assert world.records()[0]["decision_id"] == ["hook.7"]
    assert world.records()[0]["inputs"]["override_escalation"] == "ticketctl"


def test_decision_ids_of_flags_and_hooks_reach_the_audit(world: World) -> None:
    held = world.hash_of(StubTransition("DEMO-2", "Done"))
    hooks = Hooks()
    hooks.ids = ["hook.1", "flag.1"]
    out = world.run(
        StubTransition("DEMO-2", "Done"),
        yes=True,
        expect_plan=held,
        decision_ids=["flag.1", "flag.2"],
        seams=Seams(hooks=hooks),
    )
    assert out["decision_ids"] == ["flag.1", "flag.2", "hook.1"]
    assert {tuple(r["decision_id"]) for r in world.records()} == {("flag.1", "flag.2", "hook.1")}


def test_post_hook_runs_once_after_an_attempt_and_never_fails_the_command(world: World) -> None:
    held = world.hash_of(StubTransition("DEMO-2", "Done"))
    hooks = Hooks()
    hooks.post_raises = True
    out = world.run(
        StubTransition("DEMO-2", "Done"), yes=True, expect_plan=held, seams=Seams(hooks=hooks)
    )
    assert out["result"] == "ok"
    assert len(hooks.post_calls) == 1 and hooks.post_calls[0].result == "ok"
    assert hooks.post_calls[0].changed is True


def test_lock_is_held_from_the_fresh_check_to_the_outcome_and_only_with_a_key(
    world: World,
) -> None:
    events: list[str] = []
    real = audit.append_audit

    def spy(state_dir: Path, record: Any, **kw: Any) -> Any:
        events.append(f"audit:{record['result']}")
        return real(state_dir, record, **kw)

    world.mp.setattr(audit, "append_audit", spy)

    @contextlib.contextmanager
    def lock_cm():
        events.append("lock")
        yield
        events.append("unlock")

    hooks = Hooks()
    hooks.post = lambda *a: events.append("post")  # type: ignore[method-assign]
    seams = Seams(hooks=hooks, lock=lambda p, c: lock_cm())
    held = world.hash_of(StubComment("DEMO-1", "locked", dedupe_key="k-lock"))
    world.run(
        StubComment("DEMO-1", "locked", dedupe_key="k-lock"),
        yes=True,
        expect_plan=held,
        seams=seams,
    )
    assert events == ["lock", "audit:attempted", "audit:ok", "unlock", "post"]
    events.clear()
    world.run(StubTransition("DEMO-2", "Done"), seams=Seams(lock=lambda p, c: lock_cm()), yes=True,
              expect_plan=world.hash_of(StubTransition("DEMO-2", "Done")))  # fmt: skip
    assert "lock" not in events  # no dedupe key: no lock


def test_fresh_check_with_no_steps_is_unchanged(world: World) -> None:
    held = world.hash_of(StubTransition("DEMO-2", "Done"))
    hooks = Hooks()
    hooks.before = lambda: setattr(world.fake.tickets["DEMO-2"], "status", "Done")
    out = world.run(
        StubTransition("DEMO-2", "Done"), yes=True, expect_plan=held, seams=Seams(hooks=hooks)
    )
    assert out["result"] == "unchanged" and world.records() == [] and world.writes() == []


# ---------------------------------------------------------------------------------------------
# The audit pair and the failure shapes
# ---------------------------------------------------------------------------------------------


def test_attempted_record_is_durable_before_the_request_is_made(world: World) -> None:
    seen: list[list[dict[str, Any]]] = []
    calls_at_send: list[int] = []

    class Spy(StubTransition):
        def _transition(self) -> Step:
            step = super()._transition()
            real = step.send

            def send(gw: WriteGateway) -> StepResult:
                seen.append(world.records())
                calls_at_send.append(len(world.writes()))
                return real(gw)

            step.send = send
            return step

    fsyncs: list[int] = []
    real_fsync = audit._fsync
    world.mp.setattr(audit, "_fsync", lambda fd: (fsyncs.append(fd), real_fsync(fd))[1])
    held = world.hash_of(Spy("DEMO-2", "Done"))
    world.run(Spy("DEMO-2", "Done"), yes=True, expect_plan=held)
    assert len(seen[0]) == 1 and seen[0][0]["result"] == "attempted"
    assert calls_at_send == [0] and fsyncs, "attempted record written and fsynced before the write"
    records = world.records()
    assert [r["result"] for r in records] == ["attempted", "ok"]
    assert records[0]["attempt_id"] == records[1]["attempt_id"] == ATTEMPT
    assert [r["step"] for r in records] == [1, 1] and records[1]["changed"] is True
    for r in records:
        validator("audit.json").validate(r)
    assert records[0]["inputs"]["plan_sha256"] == held
    assert records[0]["target"] == {"site": "main", "key": "DEMO-2"}


def test_audit_inputs_carry_evidence_and_no_secrets_or_text(world: World) -> None:
    world.run(StubComment("DEMO-1", "super secret body text", dedupe_key="dk-1"), yes=True,
              expect_plan=world.hash_of(StubComment("DEMO-1", "super secret body text",
                                                    dedupe_key="dk-1")))  # fmt: skip
    raw = (world.state / "audit.jsonl").read_text()
    assert "super secret body text" not in raw and SITE_TOKEN not in raw
    rec = world.records()[0]
    inputs = rec["inputs"]
    assert inputs["token_env"] == TOKEN_VAR and inputs["dedupe_key"] == "dk-1"
    assert inputs["bodies"][0]["sha256"] and inputs["bodies"][0]["bytes"] > 0
    assert inputs["config"]["path"] == str(world.config)
    assert inputs["config"]["files_sha256"] == [
        hashlib.sha256(world.config.read_bytes()).hexdigest()
    ]
    assert inputs["site_host"].startswith("127.0.0.1:") is True
    assert rec["actor"] == "human:tester" and rec["actor_source"] == "flag"


def test_audit_append_failure_refuses_the_send(world: World) -> None:
    def broken(state_dir: Path, record: Any, **kw: Any) -> Any:
        raise StateError("lock_timeout", "stuck")

    hooks = Hooks()
    world.mp.setattr(audit, "append_audit", broken)
    held = world.hash_of(StubTransition("DEMO-2", "Done"))
    err = world.fail(
        StubTransition("DEMO-2", "Done"), yes=True, expect_plan=held, seams=Seams(hooks=hooks)
    )
    assert (err.exit, err.code, err.reason) == (1, "internal", "audit_failed")
    assert world.writes() == []  # nothing was sent
    assert err.details is None or "attempt_id" not in err.details
    assert hooks.post_calls == [] and world.records() == []  # no attempted record: no post hook


def test_audit_failure_on_a_later_step_is_partial_with_an_internal_step(world: World) -> None:
    real = audit.append_audit
    count = {"n": 0}

    def flaky(state_dir: Path, record: Any, **kw: Any) -> Any:
        count["n"] += 1
        if count["n"] == 3:  # the second step's attempted record
            raise StateError("state_io", "disk")
        return real(state_dir, record, **kw)

    world.mp.setattr(audit, "append_audit", flaky)
    held = world.hash_of(StubTransition("DEMO-1", "In Progress", assign_me=True))
    err = world.fail(
        StubTransition("DEMO-1", "In Progress", assign_me=True), yes=True, expect_plan=held
    )
    assert (err.exit, err.code) == (12, "partial") and err.details is not None
    result = err.details["result"]
    assert [(s["kind"], s["status"]) for s in result["steps"]] == [
        ("assign", "done"),
        ("transition", "failed"),
    ]
    assert result["steps"][1]["code"] == "internal" and result["result"] == "partial"
    assert err.details["attempt_id"] == ATTEMPT
    assert [c.path for c in world.writes()] == ["/rest/api/3/issue/DEMO-1/assignee"]
    validator("cli/result.json").validate(result)


def test_outcome_append_failure_warns_and_marks_the_result(
    world: World, capsys: pytest.CaptureFixture[str]
) -> None:
    real = audit.append_audit

    def flaky(state_dir: Path, record: Any, **kw: Any) -> Any:
        if record["result"] != "attempted":
            raise StateError("state_io", "disk")
        return real(state_dir, record, **kw)

    world.mp.setattr(audit, "append_audit", flaky)
    held = world.hash_of(StubTransition("DEMO-2", "Done"))
    out = world.run(StubTransition("DEMO-2", "Done"), yes=True, expect_plan=held)
    assert out["result"] == "ok" and "audit_incomplete" in out["warnings"]
    assert "could not be written" in capsys.readouterr().err
    assert [r["result"] for r in world.records()] == ["attempted"]
    validator("cli/result.json").validate(out)


def test_outcome_unknown_after_a_dropped_connection_then_a_rerun_is_unchanged(
    world: World,
) -> None:
    world.fake.inject(Fault("reset", method="POST", path="/transitions", after_apply=True))
    held = world.hash_of(StubTransition("DEMO-2", "Done"))
    err = world.fail(StubTransition("DEMO-2", "Done"), yes=True, expect_plan=held)
    assert (err.exit, err.code, err.outcome_unknown) == (12, "outcome_unknown", True)
    assert err.retryable is True
    assert err.details is not None and err.details["attempt_id"] == ATTEMPT
    records = world.records()
    assert [r["result"] for r in records] == ["attempted", "unknown"]
    assert records[1]["changed"] is None and records[1]["reason"] == "outcome_unknown"
    # The write landed although no answer came; the re-run never double-writes.
    assert world.fake.tickets["DEMO-2"].status == "Done"
    again = world.run(StubTransition("DEMO-2", "Done"), yes=True, expect_plan=held)
    assert again["result"] == "unchanged"
    assert len([c for c in world.writes() if c.path.endswith("/transitions")]) == 1


def test_comment_outcome_unknown_is_not_retryable_without_a_key_and_dedupes_with_one(
    world: World,
) -> None:
    world.fake.inject(Fault("reset", method="POST", path="/comment", after_apply=True))
    # Without --yes the run would need a human: use an accepting dialog instead of --yes.
    err = world.fail(
        StubComment("DEMO-1", "no key"),
        seams=Seams(interactive=lambda: True, confirm=lambda p: True),
    )
    assert err.code == "outcome_unknown" and err.retryable is False
    assert "--dedupe-key" in err.message

    world.fake.inject(Fault("reset", method="POST", path="/comment", after_apply=True))
    held = world.hash_of(StubComment("DEMO-1", "keyed", dedupe_key="k-9"))
    err = world.fail(StubComment("DEMO-1", "keyed", dedupe_key="k-9"), yes=True, expect_plan=held)
    assert err.code == "outcome_unknown" and err.retryable is True
    again = world.run(StubComment("DEMO-1", "keyed", dedupe_key="k-9"), yes=True, expect_plan=held)
    assert again["result"] == "unchanged" and again["already_posted"] is True
    keyed = [c for c in world.fake.tickets["DEMO-1"].comments]
    assert len(keyed) == 2  # one unkeyed comment (landed, unknown) and exactly one keyed


def test_partial_after_a_definite_failure_of_the_second_step(world: World) -> None:
    action = StubTransition("DEMO-1", "In Progress", assign_me=True, transition_id="99")
    held = world.hash_of(action)
    err = world.fail(
        StubTransition("DEMO-1", "In Progress", assign_me=True, transition_id="99"),
        yes=True,
        expect_plan=held,
    )
    assert (err.exit, err.code) == (12, "partial") and err.outcome_unknown is False
    assert err.details is not None
    result = err.details["result"]
    assert [(s["kind"], s["status"], s.get("code")) for s in result["steps"]] == [
        ("assign", "done", None),
        ("transition", "failed", "rejected"),
    ]
    assert result["attempt_id"] == ATTEMPT and result["changed"] is True
    records = world.records()
    assert [(r["step"], r["result"]) for r in records] == [
        (1, "attempted"), (1, "ok"), (2, "attempted"), (2, "partial"),
    ]  # fmt: skip
    assert records[3]["changed"] is False and records[3]["reason"] == "rejected"
    assert world.fake.tickets["DEMO-1"].assignee == ME_ACCOUNT  # nothing rolled back
    validator("cli/result.json").validate(result)


def test_partial_with_an_unknown_second_step(world: World) -> None:
    world.fake.inject(Fault("status", method="POST", path="/transitions", status=503))
    held = world.hash_of(StubTransition("DEMO-1", "In Progress", assign_me=True))
    err = world.fail(
        StubTransition("DEMO-1", "In Progress", assign_me=True), yes=True, expect_plan=held
    )
    assert (err.code, err.outcome_unknown) == ("partial", True)
    assert (
        err.details is not None and err.details["result"]["steps"][1]["code"] == "outcome_unknown"
    )
    last = world.records()[-1]
    assert last["result"] == "unknown" and last["changed"] is None


def test_nothing_applied_failure_keeps_its_code_with_the_attempt_id(world: World) -> None:
    held = world.hash_of(StubTransition("DEMO-2", "Done", transition_id="99"))
    err = world.fail(
        StubTransition("DEMO-2", "Done", transition_id="99"), yes=True, expect_plan=held
    )
    assert err.code == "rejected" and err.details is not None
    assert err.details["attempt_id"] == ATTEMPT and "result" not in err.details
    assert [r["result"] for r in world.records()] == ["attempted", "failed"]


class Boom(StubTransition):
    def __init__(self, exc: BaseException, *a: Any, **kw: Any) -> None:
        super().__init__(*a, **kw)
        self.exc = exc

    def _transition(self) -> Step:
        def send(gw: WriteGateway) -> StepResult:
            raise self.exc

        return Step("transition", send, {"transition_id": "41", "to": self.to})


def test_a_send_that_raises_midway_records_an_unknown_outcome(world: World) -> None:
    held = world.hash_of(Boom(RuntimeError("secret " + SITE_TOKEN), "DEMO-2", "Done"))
    err = world.fail(
        Boom(RuntimeError("secret " + SITE_TOKEN), "DEMO-2", "Done"), yes=True, expect_plan=held
    )
    assert (err.exit, err.code, err.outcome_unknown) == (12, "outcome_unknown", True)
    assert err.details is not None and err.details["attempt_id"] == ATTEMPT
    assert "RuntimeError" in err.message and "secret" not in err.message
    assert SITE_TOKEN not in json.dumps(err.envelope())
    records = world.records()
    assert [r["result"] for r in records] == ["attempted", "unknown"]
    assert records[1]["reason"] == "outcome_unknown" and records[1]["changed"] is None


def test_an_unexpected_error_in_a_comment_send_is_not_retryable_without_a_key(
    world: World,
) -> None:
    class BoomComment(StubComment):
        def _add(self, ctx: Context, rich: Any, info: dict[str, Any]) -> Step:
            def send(gw: WriteGateway) -> StepResult:
                raise RuntimeError("x")

            return Step(
                "add_comment", send, {"body_sha256": info["sha256"], "body_bytes": info["bytes"]}
            )

    err = world.fail(
        BoomComment("DEMO-1", "no key"),
        seams=Seams(interactive=lambda: True, confirm=lambda p: True),
    )
    assert (err.code, err.retryable, err.outcome_unknown) == ("outcome_unknown", False, True)
    assert "--dedupe-key" in err.message


def test_an_interrupt_midway_records_the_outcome_and_propagates(world: World) -> None:
    held = world.hash_of(Boom(KeyboardInterrupt(), "DEMO-2", "Done"))
    with pytest.raises(KeyboardInterrupt):
        world.run(Boom(KeyboardInterrupt(), "DEMO-2", "Done"), yes=True, expect_plan=held)
    assert [r["result"] for r in world.records()] == ["attempted", "unknown"]


def test_an_interrupt_midway_still_runs_the_post_hook_once_with_unknown(world: World) -> None:
    hooks = Hooks()
    held = world.hash_of(Boom(KeyboardInterrupt(), "DEMO-2", "Done"))
    with pytest.raises(KeyboardInterrupt):
        world.run(
            Boom(KeyboardInterrupt(), "DEMO-2", "Done"),
            yes=True,
            expect_plan=held,
            seams=Seams(hooks=hooks),
        )
    assert [p.result for p in hooks.post_calls] == ["unknown"]
    assert hooks.post_calls[0].changed is None


def test_a_gateway_failure_with_a_3xx_is_unknown(world: World) -> None:
    world.fake.inject(Fault("status", method="POST", path="/transitions", status=503))
    held = world.hash_of(StubTransition("DEMO-2", "Done"))
    err = world.fail(StubTransition("DEMO-2", "Done"), yes=True, expect_plan=held)
    assert err.code == "outcome_unknown"
    assert world.records()[-1]["result"] == "unknown"


# ---------------------------------------------------------------------------------------------
# Resolve: the account, secrets, the actor, rich text
# ---------------------------------------------------------------------------------------------


def test_account_mismatch_refuses_before_any_write_and_ignores_a_planted_cache(
    world: World,
) -> None:
    world.write_config("")
    text = world.config.read_text() + "me:\n  jira: acct-9999\n"
    world.config.write_text(text)
    rt = world.rt()
    site = rt.config.sites["main"]
    cache.write_cache(
        world.state,
        "main",
        "me",
        {"account_id": "acct-9999", "display_name": "x", "time_zone": None},
        url=str(site.url),
        email=SITE_EMAIL,
        ttl=site.cache_ttl,
    )
    err = world.fail(StubTransition("DEMO-2", "Done"), yes=True, expect_plan=ZERO)
    assert (err.exit, err.code, err.reason) == (2, "invalid_config", "account_mismatch")
    assert world.writes() == [] and world.records() == []
    # Dry-run goes through the same check.
    err = world.fail(StubTransition("DEMO-2", "Done"), dry_run=True)
    assert err.reason == "account_mismatch"


def test_resolve_takes_a_fresh_myself_even_with_a_cache(world: World) -> None:
    world.run(StubTransition("DEMO-2", "Done"), dry_run=True)
    world.run(StubTransition("DEMO-2", "Done"), dry_run=True)
    myself = [c for c in world.fake.calls if c.path == "/rest/api/3/myself"]
    assert len(myself) == 2


def test_an_invalid_actor_is_exit_2_before_prepare(world: World) -> None:
    action = StubComment("DEMO-1", "hi", on_prepare=lambda: pytest.fail("prepare ran"))
    err = world.fail(action, actor="robot", dry_run=True)
    assert (err.exit, err.code, err.reason) == (2, "usage", "invalid_argument")
    assert world.fake.calls == []


def test_a_configured_secret_in_the_body_is_refused_before_any_request(world: World) -> None:
    err = world.fail(StubComment("DEMO-1", f"look {SITE_TOKEN} here"), dry_run=True)
    assert (err.exit, err.code, err.reason) == (22, "invalid_request", "secret_in_body")
    assert SITE_TOKEN not in err.message
    assert world.fake.calls == []  # prepare makes no provider call


def test_a_secret_in_a_link_target_is_refused(world: World) -> None:
    body = f"[x](https://example.test/{SITE_TOKEN})"
    err = world.fail(StubComment("DEMO-1", body), dry_run=True)
    assert err.reason == "secret_in_body"


def test_comment_carries_the_meta_property_and_the_plan_hash_matches_the_body(
    world: World,
) -> None:
    plan = world.run(StubComment("DEMO-1", "meta check", dedupe_key="dk-m"), dry_run=True)
    step = plan["steps"][0]
    out = world.run(
        StubComment("DEMO-1", "meta check", dedupe_key="dk-m"),
        yes=True,
        expect_plan=plan["plan_sha256"],
    )
    comment = world.fake.tickets["DEMO-1"].comments[-1]
    meta = comment["properties"]["ticketctl.meta"]
    assert meta["body_sha256"] == step["body_sha256"] and meta["actor_kind"] == "human"
    assert meta["key_sha256"]
    assert out["comment_id"] == comment["id"] and out["changed"] is True
    texts = writeflow.document_texts(comment["body"])
    assert any("Drafted with AI assistance" in t for t in texts)  # the attribution paragraph
    assert world.records()[0]["inputs"]["bodies"] == [
        {"sha256": step["body_sha256"], "bytes": step["body_bytes"]}
    ]


def test_format_adf_is_validated_and_a_bad_document_sends_nothing(world: World) -> None:
    good = json.dumps(
        {"type": "doc", "version": 1,
         "content": [{"type": "paragraph", "content": [{"type": "text", "text": "prepared"}]}]}
    )  # fmt: skip
    plan = world.run(StubComment("DEMO-1", good, fmt="adf"), dry_run=True)
    assert plan["steps"][0]["kind"] == "add_comment"
    bad = json.dumps(
        {"type": "doc", "version": 1,
         "content": [{"type": "paragraph", "content": [{"type": "mention", "attrs": {"id": "x"}}]}]}
    )  # fmt: skip
    err = world.fail(StubComment("DEMO-1", bad, fmt="adf"), dry_run=True)
    assert (err.code, err.reason) == ("invalid_request", "node_not_allowed")
    err = world.fail(StubComment("DEMO-1", "{not json", fmt="adf"), dry_run=True)
    assert err.reason == "invalid_value"
    assert world.fake.calls == [c for c in world.fake.calls if c.method == "GET"]


def test_unchanged_rich_text_field_is_skipped(world: World) -> None:
    held = world.hash_of(StubDescription("DEMO-1", "new description"))
    out = world.run(StubDescription("DEMO-1", "new description"), yes=True, expect_plan=held)
    assert out["result"] == "ok"
    again = world.run(StubDescription("DEMO-1", "new description"))
    assert again["result"] == "unchanged" and again["steps"] == []
    changed = world.run(StubDescription("DEMO-1", "other description"), dry_run=True)
    assert [s["kind"] for s in changed["steps"]] == ["set_fields"]


def test_hostile_key_is_refused_before_a_request_is_sent_for_it(world: World) -> None:
    err = world.fail(StubTransition("../etc/passwd", "Done"), dry_run=True)
    assert (err.exit, err.reason) == (22, "invalid_key")
    assert all("etc" not in c.path for c in world.fake.calls)


# ---------------------------------------------------------------------------------------------
# Nothing unvalidated reaches the gateway
# ---------------------------------------------------------------------------------------------

MENTION = {"type": "paragraph", "content": [{"type": "mention", "attrs": {"id": "acct-1"}}]}
BAD_DOC = {"type": "doc", "version": 1, "content": [MENTION]}
GOOD_DOC = {
    "type": "doc",
    "version": 1,
    "content": [{"type": "paragraph", "content": [{"type": "text", "text": "ok"}]}],
}

BODIES = {
    "comment": lambda d: {"body": d},
    "comment_with_property": lambda d: {"body": GOOD_DOC, "properties": [{"key": "k", "value": d}]},
    "create_description": lambda d: {"fields": {"summary": "s", "description": d}},
    "update_set": lambda d: {"fields": {"customfield_90004": d}},
    "update_verbs": lambda d: {"update": {"customfield_90004": [{"set": d}]}},
    "transition_field": lambda d: {"transition": {"id": "41"}, "fields": {"comment": [d]}},
    "deeply_nested": lambda d: {"a": [{"b": {"c": [{"d": d}]}}]},
}


@pytest.mark.parametrize("name", sorted(BODIES))
def test_every_rich_text_path_is_validated_before_the_request(name: str) -> None:
    check_outbound(BODIES[name](GOOD_DOC), body_chars=30000)
    with pytest.raises(InputError) as info:
        check_outbound(BODIES[name](BAD_DOC), body_chars=30000)
    assert info.value.reason == "node_not_allowed"


@pytest.mark.parametrize(
    "doc",
    [
        {"type": "doc", "version": 2, "content": []},
        {"type": "doc", "version": 1, "content": [], "extra": 1},
        {"type": "doc"},
        {"type": "doc", "version": 1, "content": [{"type": "paragraph", "content": [
            {"type": "text", "text": "x",
             "marks": [{"type": "link", "attrs": {"href": "javascript:alert(1)"}}]}]}]},
    ],
)  # fmt: skip
def test_malformed_documents_are_refused(doc: dict[str, Any]) -> None:
    with pytest.raises(InputError):
        check_outbound({"body": doc}, body_chars=30000)


def test_a_step_that_posts_an_invalid_document_never_reaches_the_provider(world: World) -> None:
    class Sneaky(StubTransition):
        def _transition(self) -> Step:
            def send(gw: WriteGateway) -> StepResult:
                gw.request(
                    "POST",
                    "/rest/api/3/issue/DEMO-2/comment",
                    template="/rest/api/3/issue/{key}/comment",
                    body={"body": BAD_DOC},
                )
                return StepResult([])

            return Step("transition", send, {"transition_id": "41", "to": "Done"})

    held = world.hash_of(Sneaky("DEMO-2", "Done"))
    err = world.fail(Sneaky("DEMO-2", "Done"), yes=True, expect_plan=held)
    assert (err.exit, err.code, err.reason) == (22, "invalid_request", "node_not_allowed")
    assert world.writes() == []
    assert [r["result"] for r in world.records()] == ["attempted", "failed"]


def test_walk_is_bounded(world: World) -> None:
    deep: Any = GOOD_DOC
    for _ in range(100_000):
        deep = {"a": deep}  # not recursive: an iterative walk handles it
    check_outbound(deep, body_chars=30000)
    with pytest.raises(TicketctlError):
        check_outbound([0] * (writeflow.MAX_OUTBOUND_NODES + 1), body_chars=30000)


# ---------------------------------------------------------------------------------------------
# The plan object
# ---------------------------------------------------------------------------------------------


def test_sentinel_token_in_provider_text_does_not_reach_the_audit(world: World) -> None:
    world.fake.tickets["DEMO-2"].summary = f"leak {SITE_TOKEN}"
    held = world.hash_of(StubTransition("DEMO-2", "Done"))
    world.run(StubTransition("DEMO-2", "Done"), yes=True, expect_plan=held)
    assert SITE_TOKEN not in (world.state / "audit.jsonl").read_text()
    assert SITE_TOKEN not in (world.state / "log.jsonl").read_text()


def test_hook_decision_ids_outside_the_grammar_are_dropped(world: World) -> None:
    held = world.hash_of(StubTransition("DEMO-2", "Done"))
    hooks = Hooks()
    hooks.ids = ["ok.1", "not valid!", "x" * 200]
    out = world.run(
        StubTransition("DEMO-2", "Done"), yes=True, expect_plan=held, seams=Seams(hooks=hooks)
    )
    assert out["decision_ids"] == ["ok.1"]


def test_an_unexpected_error_in_the_outcome_record_never_hides_the_result(world: World) -> None:
    real = audit.append_audit

    def flaky(state_dir: Path, record: Any, **kw: Any) -> Any:
        if record["result"] != "attempted":
            raise RuntimeError("unexpected")
        return real(state_dir, record, **kw)

    world.mp.setattr(audit, "append_audit", flaky)
    held = world.hash_of(StubTransition("DEMO-2", "Done"))
    out = world.run(StubTransition("DEMO-2", "Done"), yes=True, expect_plan=held)
    assert out["result"] == "ok" and "audit_incomplete" in out["warnings"]


def test_raise_for_write_maps_the_status_table() -> None:
    from ticketctl.gateway import Response

    def resp(status: int, data: Any = None, text: str = "") -> Response:
        return Response(status, data, text, None, 0)

    writeflow.raise_for_write(resp(204), object_path=True)
    with pytest.raises(TicketctlError) as info:
        writeflow.raise_for_write(resp(404), object_path=True)
    assert info.value.code == "not_found"
    with pytest.raises(TicketctlError) as info:
        writeflow.raise_for_write(resp(404), object_path=False)
    assert info.value.code == "provider_changed"
    with pytest.raises(TicketctlError) as info:
        writeflow.raise_for_write(
            resp(400, {"errorMessages": ["bad \x07 thing", 7], "errors": {"labels": "no"}}),
            object_path=True,
        )
    err = info.value
    assert (err.code, err.exit) == ("rejected", 12) and err.details is not None
    assert err.details["field_errors"] == {"labels": "no"}
    assert all("\x07" not in m for m in err.details["errorMessages"])
    for status in (409, 413, 415, 422):
        with pytest.raises(TicketctlError) as info:
            writeflow.raise_for_write(resp(status, text="plain"), object_path=True)
        assert info.value.code == "rejected"
    with pytest.raises(TicketctlError) as info:
        writeflow.raise_for_write(resp(302), object_path=True)
    assert info.value.code == "provider_changed"


# ---------------------------------------------------------------------------------------------
# Round 1 review findings
# ---------------------------------------------------------------------------------------------


def test_hooks_of_another_family_or_a_substituted_runner_or_a_dry_run_do_not_run(
    world: World,
) -> None:
    world.write_config("hooks:\n  pre-comment:\n    - cmd: /bin/true\n")
    held = world.hash_of(StubTransition("DEMO-2", "Done"))
    out = world.run(StubTransition("DEMO-2", "Done"), yes=True, expect_plan=held)
    assert out["result"] == "ok"
    world.write_config("hooks:\n  pre-transition:\n    - cmd: /bin/true\n")
    hooks = Hooks()
    held = world.hash_of(StubTransition("DEMO-3", "Backlog"))
    out = world.run(
        StubTransition("DEMO-3", "Backlog"), yes=True, expect_plan=held, seams=Seams(hooks=hooks)
    )
    assert out["result"] == "ok" and len(hooks.pre_calls) == 1
    plan = world.run(StubTransition("DEMO-1", "Done"), dry_run=True)
    assert plan["steps"]


def test_a_lock_that_fails_to_release_only_warns(
    world: World, capsys: pytest.CaptureFixture[str]
) -> None:
    @contextlib.contextmanager
    def lock_cm():
        yield
        raise OSError("release failed")

    held = world.hash_of(StubComment("DEMO-1", "locked", dedupe_key="k-rel"))
    out = world.run(
        StubComment("DEMO-1", "locked", dedupe_key="k-rel"),
        yes=True,
        expect_plan=held,
        seams=Seams(lock=lambda p, c: lock_cm()),
    )
    assert out["result"] == "ok" and out["changed"] is True
    assert "lock could not be released" in capsys.readouterr().err
    assert len(world.fake.tickets["DEMO-1"].comments) == 1


def test_a_lock_that_fails_to_release_keeps_the_error_of_a_failed_write(world: World) -> None:
    @contextlib.contextmanager
    def lock_cm():
        yield
        raise OSError("release failed")

    world.fake.inject(Fault("reset", method="POST", path="/comment", after_apply=True))
    held = world.hash_of(StubComment("DEMO-1", "keyed", dedupe_key="k-rel2"))
    err = world.fail(
        StubComment("DEMO-1", "keyed", dedupe_key="k-rel2"),
        yes=True,
        expect_plan=held,
        seams=Seams(lock=lambda p, c: lock_cm()),
    )
    assert err.code == "outcome_unknown"


@pytest.mark.parametrize("exc", [OSError("disk"), RuntimeError("x"), ValueError("y")])
def test_any_exception_from_the_attempted_append_refuses_the_send(
    world: World, exc: Exception
) -> None:
    def broken(state_dir: Path, record: Any, **kw: Any) -> Any:
        raise exc

    held = world.hash_of(StubTransition("DEMO-2", "Done"))
    world.mp.setattr(audit, "append_audit", broken)
    err = world.fail(StubTransition("DEMO-2", "Done"), yes=True, expect_plan=held)
    assert (err.exit, err.code, err.reason) == (1, "internal", "audit_failed")
    assert world.writes() == []


def test_an_oserror_reading_the_config_files_for_the_audit_refuses_the_send(world: World) -> None:
    held = world.hash_of(StubTransition("DEMO-2", "Done"))

    def broken(rt: Runtime) -> list[str]:
        raise OSError("gone")

    world.mp.setattr(writeflow, "_config_file_hashes", broken)
    err = world.fail(StubTransition("DEMO-2", "Done"), yes=True, expect_plan=held)
    assert (err.exit, err.code, err.reason) == (1, "internal", "audit_failed")
    assert world.writes() == [] and world.records() == []


def test_the_config_hashes_are_read_once_before_the_confirmation(world: World) -> None:
    original = hashlib.sha256(world.config.read_bytes()).hexdigest()
    real = writeflow._config_file_hashes
    calls: list[int] = []
    world.mp.setattr(writeflow, "_config_file_hashes", lambda rt: (calls.append(1), real(rt))[1])

    def confirm(plan: dict[str, Any]) -> bool:
        world.config.write_text(world.config.read_text() + "\n# edited during the prompt\n")
        return True

    out = world.run(
        StubTransition("DEMO-2", "Done"), seams=Seams(interactive=lambda: True, confirm=confirm)
    )
    assert out["result"] == "ok" and calls == [1]
    assert world.records()[0]["inputs"]["config"]["files_sha256"] == [original]


def test_the_engine_reads_fresh_only_under_the_lock(world: World) -> None:
    seen: list[bool] = []

    class Reads(StubTransition):
        def reduce(self, ctx: Context, *, fresh: bool) -> Planned:
            seen.append(fresh)
            return super().reduce(ctx, fresh=fresh)

    held = world.hash_of(StubTransition("DEMO-2", "Done"))
    world.run(Reads("DEMO-2", "Done"), yes=True, expect_plan=held)
    assert seen == [False, True]


class StubCreate:
    """A create whose step answers with a chosen (possibly malformed) key."""

    name = "create"
    yes_needs_dedupe_key = True
    dedupe_key: str | None = "ck-1"

    def __init__(self, answer: str | None) -> None:
        self.answer = answer

    def prepare(self, pc: Any) -> None:
        return None

    def reduce(self, ctx: Context, *, fresh: bool) -> Planned:
        answer = self.answer

        def send(gw: WriteGateway) -> StepResult:
            return StepResult(["10001"], key=answer)

        return Planned(
            steps=[Step("create", send, {"project": "DEMO", "type": "Task"})],
            intent={"project": "DEMO", "title": "t"},
            hash_key="DEMO",
            dedupe_key=self.dedupe_key,
        )

    def check(self, ctx: Context, planned: Planned) -> None:
        return None


@pytest.mark.parametrize("answer", ["not a key\x07", "demo-1", "", None])
def test_a_landed_create_with_a_malformed_key_still_returns_ok(
    world: World, answer: str | None
) -> None:
    hooks = Hooks()
    held = world.hash_of(StubCreate(answer))
    out = world.run(StubCreate(answer), yes=True, expect_plan=held, seams=Seams(hooks=hooks))
    assert out["result"] == "ok" and out["key"] is None and out["url"] is None
    assert out["attempt_id"] == ATTEMPT
    assert [p.result for p in hooks.post_calls] == ["ok"]
    assert [r["result"] for r in world.records()] == ["attempted", "ok"]
    validator("cli/result.json").validate(out)


def test_a_landed_create_with_a_good_key_has_a_url(world: World) -> None:
    held = world.hash_of(StubCreate("DEMO-77"))
    out = world.run(StubCreate("DEMO-77"), yes=True, expect_plan=held)
    assert out["key"] == "DEMO-77" and re.search(r"/browse/DEMO-77$", out["url"])


def test_decision_ids_beyond_16_are_dropped_with_a_warning_flag_ids_first(world: World) -> None:
    held = world.hash_of(StubTransition("DEMO-2", "Done"))
    hooks = Hooks()
    hooks.ids = [f"hook.{i}" for i in range(10)]
    flag_ids = [f"flag.{i}" for i in range(10)]
    out = world.run(
        StubTransition("DEMO-2", "Done"),
        yes=True,
        expect_plan=held,
        decision_ids=flag_ids,
        seams=Seams(hooks=hooks),
    )
    assert out["decision_ids"] == [*flag_ids, *hooks.ids[:6]]
    assert "decision_ids_truncated" in out["warnings"]
    validator("cli/result.json").validate(out)
    assert len(world.records()[0]["decision_id"]) == 16


def test_sixteen_decision_ids_or_fewer_carry_no_warning(world: World) -> None:
    held = world.hash_of(StubTransition("DEMO-2", "Done"))
    out = world.run(
        StubTransition("DEMO-2", "Done"),
        yes=True,
        expect_plan=held,
        decision_ids=[f"flag.{i}" for i in range(16)],
    )
    assert len(out["decision_ids"]) == 16 and "decision_ids_truncated" not in out["warnings"]


def test_the_cli_refuses_more_than_16_decision_id_flags() -> None:
    from ticketctl import flags

    argv = [tok for i in range(17) for tok in ("--decision-id", f"d{i}")]
    with pytest.raises(TicketctlError) as info:
        flags.extract_globals([*argv, "comment", "DEMO-1"])
    assert (info.value.exit, info.value.reason) == (2, "invalid_argument")
    g, _rest = flags.extract_globals([*argv[:32], "comment", "DEMO-1"])
    assert len(g.decision_ids) == 16


def test_body_info_bytes_is_the_length_of_the_canonical_json(world: World) -> None:
    from ticketctl.adf_validate import canonical_json

    plan = world.run(StubComment("DEMO-1", "ünï bödy"), dry_run=True)
    pc = writeflow.PrepareContext(world.rt(), "main", "human:tester")
    rich = pc.prepare_rich("ünï bödy", kind="comment")
    assert rich.doc is not None
    info = writeflow.body_info(rich)
    assert info["bytes"] == len(canonical_json(rich.doc).encode("utf-8"))
    assert info["bytes"] > len(canonical_json(rich.doc))  # multi-byte text: bytes, not characters
    assert plan["steps"][0]["body_bytes"] == info["bytes"]


def test_hooks_digest_covers_pre_and_post_of_the_family_configured_then_flags(
    world: World,
) -> None:
    world.write_config(
        "hooks:\n"
        "  pre-transition:\n    - cmd: one\n      timeout: 10s\n"
        "  post-transition:\n    - cmd: two\n"
        "  pre-sprint:\n    - cmd: three\n"
    )
    rt = world.rt(hooks=["pre-transition=flagcmd", "pre-sprint=sprintflag", "pre-comment=other"])

    def digest(entries: list[dict[str, Any]]) -> str:
        text = json.dumps(entries, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(text.encode()).hexdigest()

    configured = [
        {"name": "pre-transition", "cmd": "one", "timeout": 10, "async": False, "source": "config"},
        {
            "name": "post-transition",
            "cmd": "two",
            "timeout": 30,
            "async": False,
            "source": "config",
        },
    ]
    flagged = [{"name": "pre-transition", "cmd": "flagcmd", "source": "flag"}]
    assert writeflow.hooks_digest(rt, "transition") == digest([*configured, *flagged])
    sprint = [
        {"name": "pre-sprint", "cmd": "three", "timeout": 30, "async": False, "source": "config"},
        {"name": "pre-sprint", "cmd": "sprintflag", "source": "flag"},
    ]
    assert writeflow.hook_family("sprint_add") == writeflow.hook_family("sprint_remove") == "sprint"
    for action in ("sprint_add", "sprint_remove"):
        assert writeflow.hooks_digest(rt, action) == digest(sprint)
    assert writeflow.hooks_digest(rt, "update") == digest([])


def test_the_default_interactive_probe_follows_yes_actor_tty_and_dev_tty(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    from ticketctl import flags

    monkeypatch.setenv("PATH", "")
    state = {"tty": True, "dev": True}
    monkeypatch.setattr(flags, "_stderr_is_tty", lambda: state["tty"])
    monkeypatch.setattr(flags, "probe_dev_tty", lambda: state["dev"])
    human = Globals(resolved_actor="human:tester")
    assert writeflow.is_interactive(human) is True
    assert writeflow.is_interactive(Globals(resolved_actor="human:t", yes=True)) is False
    assert writeflow.is_interactive(Globals(resolved_actor="auto:bot")) is False
    state["tty"] = False
    assert writeflow.is_interactive(human) is False
    state.update(tty=True, dev=False)
    assert writeflow.is_interactive(human) is False
    # Through the engine: the default probe lets the dialog run.
    state["dev"] = True
    seams = Seams(confirm=lambda p: True)
    seams.attempt_id = lambda: ATTEMPT
    out = execute(world.rt(), "main", StubTransition("DEMO-2", "Done"), seams)
    assert out["result"] == "ok"
    state["tty"] = False
    seams = Seams(confirm=lambda p: True)
    with pytest.raises(TicketctlError) as info:
        execute(world.rt(), "main", StubTransition("DEMO-3", "Backlog"), seams)
    assert info.value.code == "confirmation_required"


SPLIT = SITE_TOKEN[:11], SITE_TOKEN[11:]


@pytest.mark.parametrize(
    "body",
    [
        f"{SPLIT[0]}**{SPLIT[1]}**",
        f"{SPLIT[0]}*{SPLIT[1]}*",
        f"{SPLIT[0]}\\\n{SPLIT[1]}",
        f"{SPLIT[0]}`{SPLIT[1]}`",
        f"[{SPLIT[0]}](https://example.test/x){SPLIT[1]}",
        f"{SPLIT[0]}[{SPLIT[1]}](https://example.test/x)",
    ],
    ids=["strong", "em", "hard_break", "code", "link_head", "link_tail"],
)
def test_a_secret_split_across_text_nodes_is_refused_before_any_request(
    world: World, body: str
) -> None:
    err = world.fail(StubComment("DEMO-1", body), dry_run=True)
    assert (err.exit, err.code, err.reason) == (22, "invalid_request", "secret_in_body")
    assert world.fake.calls == []


def test_a_secret_split_over_two_adf_text_nodes_is_refused(world: World) -> None:
    doc = {
        "type": "doc",
        "version": 1,
        "content": [
            {
                "type": "paragraph",
                "content": [
                    {"type": "text", "text": SPLIT[0]},
                    {"type": "text", "text": SPLIT[1], "marks": [{"type": "strong"}]},
                ],
            }
        ],
    }
    err = world.fail(StubComment("DEMO-1", json.dumps(doc), fmt="adf"), dry_run=True)
    assert err.reason == "secret_in_body" and world.fake.calls == []
    doc["content"] = [
        {"type": "paragraph", "content": [{"type": "text", "text": SPLIT[0]}]},
        {"type": "paragraph", "content": [{"type": "text", "text": SPLIT[1]}]},
    ]
    err = world.fail(StubComment("DEMO-1", json.dumps(doc), fmt="adf"), dry_run=True)
    assert err.reason == "secret_in_body"


def test_comment_id_and_existing_key_pass_the_grammar_filters(world: World) -> None:
    class Odd(StubComment):
        def reduce(self, ctx: Context, *, fresh: bool) -> Planned:
            return Planned(
                steps=[],
                intent={"x": 1},
                key="DEMO-1",
                comment_id="12\x07 34",
                existing_key="DEMO-9\x1b[31m",
            )

    out = world.run(Odd("DEMO-1", "x"))
    assert out["comment_id"] is None and out["key"] is None and out["url"] is None
    validator("cli/result.json").validate(out)

    class Sent(StubComment):
        def _add(self, ctx: Context, rich: Any, info: dict[str, Any]) -> Step:
            def send(gw: WriteGateway) -> StepResult:
                return StepResult(["77"], comment_id="77\x07 bad")

            return Step(
                "add_comment", send, {"body_sha256": info["sha256"], "body_bytes": info["bytes"]}
            )

    held = world.hash_of(Sent("DEMO-1", "y", dedupe_key="k-c"))
    out = world.run(Sent("DEMO-1", "y", dedupe_key="k-c"), yes=True, expect_plan=held)
    assert out["comment_id"] is None and out["key"] == "DEMO-1"


def test_raise_for_write_needs_object_path() -> None:
    from ticketctl.gateway import Response

    with pytest.raises(TypeError):
        writeflow.raise_for_write(Response(404, None, "", None, 0))  # type: ignore[call-arg]


# R7: what check_outbound treats as a document.


class _Str(str):
    __slots__ = ()


PARA = {"type": "paragraph", "content": [{"type": "text", "text": "ok"}]}


@pytest.mark.parametrize(
    "body",
    [
        {"a": (BAD_DOC,)},
        (BAD_DOC,),
        {"a": [(BAD_DOC,)]},
        MappingProxyType(BAD_DOC),
        {"a": MappingProxyType({"b": BAD_DOC})},
        {"type": _Str("doc"), "version": 1, "content": [MENTION]},
        {"type": "Doc", "version": 1, "content": [PARA]},
        {"type": " DOC ", "version": 1, "content": [PARA]},
        {"type": "paragraph", "version": 1, "content": [PARA]},
        {"a": {"version": 1, "content": [PARA]}},
    ],
    ids=[
        "tuple_in_dict", "tuple_top", "tuple_in_list", "mapping_top", "mapping_nested",
        "str_subclass", "Doc", "spaced_DOC", "version_content_shape", "nested_shape",
    ],
)  # fmt: skip
def test_odd_document_shapes_are_refused_not_sent(body: object) -> None:
    with pytest.raises(InputError):
        check_outbound(body, body_chars=30000)


def test_strings_and_bytes_are_not_walked_as_sequences() -> None:
    check_outbound({"a": "doc", "b": b"doc", "c": ("x", "y"), "d": {"type": "text"}}, body_chars=1)


def test_a_miscased_document_never_reaches_the_provider(world: World) -> None:
    class Sneaky(StubTransition):
        def _transition(self) -> Step:
            def send(gw: WriteGateway) -> StepResult:
                gw.request(
                    "POST",
                    "/rest/api/3/issue/DEMO-2/comment",
                    template="/rest/api/3/issue/{key}/comment",
                    body={"body": ({"type": "Doc", "version": 1, "content": [PARA]},)},
                )
                return StepResult([])

            return Step("transition", send, {"transition_id": "41", "to": "Done"})

    held = world.hash_of(Sneaky("DEMO-2", "Done"))
    err = world.fail(Sneaky("DEMO-2", "Done"), yes=True, expect_plan=held)
    assert err.exit == 22 and world.writes() == []


# ---------------------------------------------------------------------------------------------
# Round 2 review findings
# ---------------------------------------------------------------------------------------------


class CountingLock:
    """A lock that counts its entries and exits, as a real dedupe lock would hold a resource."""

    def __init__(self) -> None:
        self.enters = 0
        self.exits = 0

    def __enter__(self) -> None:
        self.enters += 1

    def __exit__(self, *exc: object) -> None:
        self.exits += 1


class FreshNoSteps(StubComment):
    """The fresh read under the lock finds the work already done (reused/already posted)."""

    def reduce(self, ctx: Context, *, fresh: bool) -> Planned:
        planned = super().reduce(ctx, fresh=fresh)
        return dataclasses.replace(planned, steps=[]) if fresh else planned


def test_the_lock_is_released_exactly_once_when_the_fresh_read_has_no_steps(world: World) -> None:
    lock = CountingLock()
    hooks = Hooks()
    held = world.hash_of(StubComment("DEMO-1", "again", dedupe_key="k-nosteps"))
    out = world.run(
        FreshNoSteps("DEMO-1", "again", dedupe_key="k-nosteps"),
        yes=True,
        expect_plan=held,
        seams=Seams(hooks=hooks, lock=lambda p, c: lock),
    )
    assert out["result"] == "unchanged"
    assert (lock.enters, lock.exits) == (1, 1)


def test_the_lock_is_released_exactly_once_on_the_ok_path(world: World) -> None:
    lock = CountingLock()
    held = world.hash_of(StubComment("DEMO-1", "ok", dedupe_key="k-ok"))
    out = world.run(
        StubComment("DEMO-1", "ok", dedupe_key="k-ok"),
        yes=True,
        expect_plan=held,
        seams=Seams(lock=lambda p, c: lock),
    )
    assert out["result"] == "ok"
    assert (lock.enters, lock.exits) == (1, 1)


def test_the_lock_is_released_exactly_once_on_the_error_path(world: World) -> None:
    lock = CountingLock()
    world.fake.inject(Fault("reset", method="POST", path="/comment", after_apply=True))
    held = world.hash_of(StubComment("DEMO-1", "err", dedupe_key="k-err"))
    err = world.fail(
        StubComment("DEMO-1", "err", dedupe_key="k-err"),
        yes=True,
        expect_plan=held,
        seams=Seams(lock=lambda p, c: lock),
    )
    assert err.code == "outcome_unknown"
    assert (lock.enters, lock.exits) == (1, 1)


def test_the_lock_is_released_once_before_the_post_hook_on_the_interrupt_path(
    world: World,
) -> None:
    events: list[str] = []

    class Ordered(CountingLock):
        def __exit__(self, *exc: object) -> None:
            events.append("unlock")
            super().__exit__(*exc)

    class KeyedBoom(Boom):
        def reduce(self, ctx: Context, *, fresh: bool) -> Planned:
            return dataclasses.replace(super().reduce(ctx, fresh=fresh), dedupe_key="k-int")

    lock = Ordered()
    hooks = Hooks()
    hooks.post = lambda *a: events.append("post")  # type: ignore[method-assign]
    held = world.hash_of(KeyedBoom(KeyboardInterrupt(), "DEMO-2", "Done"))
    with pytest.raises(KeyboardInterrupt):
        world.run(
            KeyedBoom(KeyboardInterrupt(), "DEMO-2", "Done"),
            yes=True,
            expect_plan=held,
            seams=Seams(hooks=hooks, lock=lambda p, c: lock),
        )
    assert (lock.enters, lock.exits) == (1, 1)
    assert events == ["unlock", "post"]


def test_a_mapping_with_version_and_content_is_a_document_even_when_not_adf() -> None:
    with pytest.raises(InputError):
        check_outbound({"payload": {"version": 3, "content": "plain text"}}, body_chars=30000)


def test_a_configured_hook_does_not_run_for_a_noop(world: World) -> None:
    # a hook that would veto if it ran: the no-op returns before any hook
    world.write_config("hooks:\n  pre-transition:\n    - cmd: exit 1\n")
    world.fake.tickets["DEMO-2"].status = "Done"
    out = world.run(StubTransition("DEMO-2", "Done"), yes=True, expect_plan="0" * 64)
    assert out["result"] == "unchanged"
    assert world.writes() == [] and world.records() == []
