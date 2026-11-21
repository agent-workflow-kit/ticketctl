"""``transition KEY`` (issue #28): the write action against the fake provider.

Most tests drive ``TransitionAction`` through the real write flow with ``World``
(tests/test_writeflow);
a few go through the CLI (``main``) for the argument rules and the registered handler. Everything is
synthetic: the fake's tickets (DEMO-1 is unassigned in the backlog, DEMO-2 is in progress and mine,
DEMO-3 is done), its transitions (31 to In Progress, 41 to Done, 51 to Backlog) and its accounts.
"""

from __future__ import annotations

import copy
import json
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

import tests.fake_provider as fp
from tests.fake_provider import ME_ACCOUNT, OTHER_ACCOUNT, SITE_TOKEN, FakeProvider, Fault
from tests.golden import assert_golden, validator
from tests.test_write_cli import SITE_URL, Cli
from tests.test_writeflow import ATTEMPT, TOKEN_VAR, Hooks, World
from ticketctl import commands, transitioncmd
from ticketctl import hooks as hooks_module
from ticketctl.errors import TicketctlError
from ticketctl.plan import Planned
from ticketctl.transitioncmd import TransitionAction
from ticketctl.writeflow import HookResult, PostInfo, Seams

NS = "com.atlassian.jira.plugin.system.customfieldtypes"


class RichFake(FakeProvider):
    """The fake with a Comment and a Story Points field on every transition screen."""

    def __init__(self) -> None:
        super().__init__()
        self.bodies: list[Any] = []

    def _transition_obj(self, t: fp.Ticket, tid: str, expand: bool) -> dict[str, Any]:
        out = super()._transition_obj(t, tid, expand)
        if expand:
            out["fields"]["comment"] = {
                "required": False,
                "name": "Comment",
                "schema": {"type": "comment", "system": "comment"},
                "operations": ["add"],
            }
            out["fields"][fp.F_POINTS] = {
                "required": False,
                "name": "Story Points",
                "schema": {"type": "number", "custom": f"{NS}:float"},
                "operations": ["set"],
            }
        return out

    def _transition(self, call: fp.Call, key: str) -> fp.Out:
        body = call.body
        self.bodies.append(copy.deepcopy(body))
        kept = {
            k: v for k, v in (body.get("fields") or {}).items() if k in ("resolution", "parent")
        }
        call.body = {"transition": body["transition"], "fields": kept}
        return super()._transition(call, key)


@pytest.fixture
def rich_fake() -> Iterator[RichFake]:
    with RichFake() as provider:
        yield provider


@pytest.fixture
def world(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fake: FakeProvider) -> World:
    return World(tmp_path, monkeypatch, fake)


def act(key: str, **kw: Any) -> TransitionAction:
    return TransitionAction(key, **kw)


def apply(world: World, action: TransitionAction, **flags: Any) -> dict[str, Any]:
    """Plan, then send with the held hash (what a caller does)."""
    held = world.hash_of(action, **flags)
    return world.run(action, yes=True, expect_plan=held, **flags)


def kinds(doc: dict[str, Any]) -> list[str]:
    return [s["kind"] for s in doc["steps"]]


# ---------------------------------------------------------------------------------------------
# The plan and the result
# ---------------------------------------------------------------------------------------------


def test_plan_of_a_status_change_validates_and_is_the_hashed_intent(world: World) -> None:
    plan = world.run(act("DEMO-2", to="Done", resolution="done"), dry_run=True)
    validator("cli/transition.json").validate(plan)
    assert plan["action"] == "transition" and plan["key"] == "DEMO-2"
    assert plan["steps"] == [{"kind": "transition", "transition_id": "41", "to": "Done"}]
    assert plan["changes"] == [
        {
            "name": "status",
            "from": {"name": "In Progress", "category": "indeterminate"},
            "to": {"name": "Done", "category": "done"},
        },
        {"name": "resolution", "from": None, "to": "Done"},
    ]
    assert plan["warnings"] == [] and world.writes() == []


def test_the_typed_spelling_does_not_change_the_hash_but_the_resolution_does(world: World) -> None:
    base = world.hash_of(act("DEMO-2", to="Done", resolution="Done"))
    assert world.hash_of(act("DEMO-2", to="dOnE", resolution="done")) == base
    assert world.hash_of(act("DEMO-2", transition_id="41", resolution="Done")) == base
    assert world.hash_of(act("DEMO-2", to="Done", resolution="Duplicate")) != base
    assert world.hash_of(act("DEMO-2", to="Done", resolution="Done", assign="me")) != base


def test_apply_sends_the_transition_with_the_resolution_and_matches_the_goldens(
    world: World,
) -> None:
    result = apply(world, act("DEMO-2", to="Done", resolution="Done"))
    assert result["result"] == "ok" and kinds(result) == ["transition"]
    assert result["attempt_id"] == ATTEMPT
    validator("cli/transition.json").validate(result)
    ticket = world.fake.tickets["DEMO-2"]
    assert (ticket.status, ticket.resolution) == ("Done", "Done")
    sent = [c.body for c in world.writes()]
    assert sent == [{"transition": {"id": "41"}, "fields": {"resolution": {"name": "Done"}}}]
    assert [(r["step_kind"], r["result"]) for r in world.records()] == [
        ("transition", "attempted"),
        ("transition", "ok"),
    ]


def test_a_rerun_after_the_write_landed_is_unchanged_and_sends_nothing(world: World) -> None:
    held = world.hash_of(act("DEMO-2", to="Done", resolution="Done"))
    world.run(act("DEMO-2", to="Done", resolution="Done"), yes=True, expect_plan=held)
    writes = len(world.writes())
    for flags in ({}, {"yes": True, "expect_plan": held}, {"yes": True, "expect_plan": "0" * 64}):
        again = world.run(act("DEMO-2", to="done", resolution="Done"), **flags)
        assert (again["result"], again["changed"], again["steps"]) == ("unchanged", False, [])
        assert again["attempt_id"] is None
    assert len(world.writes()) == writes


def test_a_ticket_already_in_the_target_is_unchanged_and_ignores_resolution_and_fields(
    world: World,
) -> None:
    out = world.run(act("DEMO-2", to="in progress", resolution="Nonsense", fields=["x=1"]))
    assert out["result"] == "unchanged" and out["steps"] == []
    again = world.run(act("DEMO-2", transition_id="31"))
    assert again["result"] == "unchanged"
    assert world.writes() == []


def test_an_id_the_live_list_lacks_is_refused_even_for_a_noop(world: World) -> None:
    err = world.fail(act("DEMO-2", transition_id="99"))
    assert (err.exit, err.code, err.reason) == (20, "no_such_transition", "no_such_transition")
    assert err.details == {"available_targets": ["Backlog", "Done", "In Progress"]}


# ---------------------------------------------------------------------------------------------
# --to resolution and --if-status order
# ---------------------------------------------------------------------------------------------


def test_no_match_lists_the_available_targets_and_never_echoes_the_typed_text(
    world: World,
) -> None:
    err = world.fail(act("DEMO-1", to="Fínished‮"))
    assert (err.exit, err.code) == (20, "no_such_transition")
    assert err.details == {"available_targets": ["Backlog", "Done", "In Progress"]}
    assert "Fínished" not in err.message


def test_several_matches_are_exit_21_with_candidates(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setitem(fp.TRANSITIONS, "52", ("Rewind", "Backlog"))
    err = world.fail(act("DEMO-2", to="backlog"))
    assert (err.exit, err.code) == (21, "ambiguous_transition")
    assert err.details == {"candidates": ["51: Back to backlog", "52: Rewind"]}
    ok = apply(world, act("DEMO-2", transition_id="52"))
    assert ok["result"] == "ok" and world.fake.tickets["DEMO-2"].status == "Backlog"


def test_if_status_comes_before_the_provider_context_checks(world: World) -> None:
    err = world.fail(act("DEMO-2", to="Nowhere", if_status="Backlog"))
    assert (err.exit, err.code, err.reason) == (20, "precondition_failed", "precondition_failed")
    assert err.details == {"current_status": "In Progress", "expected_status": "Backlog"}
    err = world.fail(act("DEMO-2", to="Done", if_status="Backlog"))  # also before needs_resolution
    assert err.code == "precondition_failed"
    err = world.fail(act("DEMO-2", to="Nowhere", if_status="in progress"))
    assert err.code == "no_such_transition"


def test_if_status_is_not_judged_for_a_noop(world: World) -> None:
    out = world.run(act("DEMO-2", to="In Progress", if_status="Backlog"))
    assert out["result"] == "unchanged"


def test_a_missing_ticket_is_exit_10(world: World) -> None:
    err = world.fail(act("DEMO-99", to="Done"))
    assert (err.exit, err.code) == (10, "not_found")


# ---------------------------------------------------------------------------------------------
# Resolution
# ---------------------------------------------------------------------------------------------


def test_a_done_target_needs_a_resolution_and_the_message_lists_the_allowed_ones(
    world: World,
) -> None:
    err = world.fail(act("DEMO-2", to="Done"))
    assert (err.exit, err.code, err.reason) == (22, "invalid_request", "needs_resolution")
    assert err.details is not None and "Duplicate" in err.details["candidates"]
    assert world.writes() == []


def test_the_configured_default_resolution_fills_in(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fake: FakeProvider
) -> None:
    w = World(
        tmp_path,
        monkeypatch,
        fake,
        config_extra="    transitions:\n      default_resolution: duplicate\n",
    )
    result = apply(w, act("DEMO-2", to="Done"))
    assert result["result"] == "ok"
    assert fake.tickets["DEMO-2"].resolution == "Duplicate"  # the provider's own spelling


@pytest.mark.parametrize("typed", ["Nonsense", "", "  ", "Do​ne", "x" * 300])
def test_an_unlisted_or_malformed_resolution_is_invalid_value(world: World, typed: str) -> None:
    err = world.fail(act("DEMO-2", to="Done", resolution=typed))
    assert (err.exit, err.code, err.reason) == (22, "invalid_request", "invalid_value")
    assert world.writes() == []


def test_a_resolution_on_a_non_done_target_is_invalid_value(world: World) -> None:
    err = world.fail(act("DEMO-1", to="In Progress", resolution="Done", assign="me"))
    assert (err.exit, err.reason) == (22, "invalid_value")


def test_resolution_is_checked_against_the_provider_list_when_the_transition_lists_none(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    original = fp.FakeProvider._transition_obj

    def bare(self: FakeProvider, t: fp.Ticket, tid: str, expand: bool) -> dict[str, Any]:
        out = original(self, t, tid, expand)
        if expand:
            out["fields"].pop("resolution", None)
        return out

    monkeypatch.setattr(fp.FakeProvider, "_transition_obj", bare)
    ok = apply(world, act("DEMO-2", to="Done", resolution="won't do"))
    assert ok["result"] == "ok" and world.fake.tickets["DEMO-2"].resolution == "Won't Do"
    err = world.fail(act("DEMO-1", to="Done", resolution="Nope"))
    assert err.reason == "invalid_value"


def test_a_secret_in_the_resolution_is_refused(world: World) -> None:
    err = world.fail(act("DEMO-2", to="Done", resolution=SITE_TOKEN))
    assert (err.exit, err.reason) == (22, "secret_in_body")
    assert SITE_TOKEN not in err.message


# ---------------------------------------------------------------------------------------------
# Assign
# ---------------------------------------------------------------------------------------------


def test_assign_me_adds_an_assign_step_before_the_transition(world: World) -> None:
    plan = world.run(act("DEMO-1", to="In Progress", assign="me"), dry_run=True)
    assert [s["kind"] for s in plan["steps"]] == ["assign", "transition"]
    assert plan["steps"][0] == {"kind": "assign", "account": "me"}
    assert plan["warnings"] == ["assign_added"]
    assert {"name": "assignee", "from": None, "to": "me"} in plan["changes"]
    result = apply(world, act("DEMO-1", to="In Progress", assign="me"))
    assert [(s["kind"], s["status"]) for s in result["steps"]] == [
        ("assign", "done"),
        ("transition", "done"),
    ]
    ticket = world.fake.tickets["DEMO-1"]
    assert (ticket.assignee, ticket.status) == (ME_ACCOUNT, "In Progress")
    assert [c.body for c in world.writes()] == [
        {"accountId": ME_ACCOUNT},
        {"transition": {"id": "31"}},
    ]


def test_assign_by_account_id_sends_that_account(world: World) -> None:
    apply(world, act("DEMO-1", to="In Progress", assign=OTHER_ACCOUNT))
    assert world.fake.tickets["DEMO-1"].assignee == OTHER_ACCOUNT
    assert world.fake.tickets["DEMO-1"].status == "In Progress"


def test_assign_never_replaces_an_assignee_and_warns_when_it_is_someone_else(
    world: World,
) -> None:
    plan = world.run(
        act("DEMO-2", to="Done", resolution="Done", assign=OTHER_ACCOUNT), dry_run=True
    )
    assert kinds(plan) == ["transition"] and plan["warnings"] == ["assign_skipped"]
    plan = world.run(act("DEMO-2", to="Done", resolution="Done", assign="me"), dry_run=True)
    assert kinds(plan) == ["transition"] and plan["warnings"] == []  # already the target


def test_assign_alone_remains_for_an_unassigned_ticket_already_in_the_target(
    world: World,
) -> None:
    result = apply(world, act("DEMO-1", to="Backlog", assign="me"))
    assert (result["action"], kinds(result), result["result"]) == ("transition", ["assign"], "ok")
    assert world.fake.tickets["DEMO-1"].assignee == ME_ACCOUNT
    again = world.run(act("DEMO-1", to="Backlog", assign="me"))
    assert again["result"] == "unchanged"


def test_assign_on_start_assigns_for_a_non_done_target_only(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fake: FakeProvider
) -> None:
    w = World(
        tmp_path, monkeypatch, fake, config_extra="    transitions:\n      assign_on_start: me\n"
    )
    plan = w.run(act("DEMO-1", to="In Progress"), dry_run=True)
    assert kinds(plan) == ["assign", "transition"] and plan["warnings"] == ["assign_added"]
    done = w.run(act("DEMO-1", to="Done", resolution="Done"), dry_run=True)
    assert kinds(done) == ["transition"]
    held = w.run(act("DEMO-2", to="Backlog"), dry_run=True)  # assigned: nothing to start
    assert kinds(held) == ["transition"] and held["warnings"] == []
    noop = w.run(act("DEMO-1", to="Backlog"), dry_run=True)  # no transition step: no assign
    assert noop["steps"] == []


def test_a_rerun_after_a_partial_sends_only_the_remaining_step_with_the_same_hash(
    world: World,
) -> None:
    held = world.hash_of(act("DEMO-1", to="In Progress", assign="me"))
    world.fake.inject(Fault("status", method="POST", path="/transitions", status=403, times=1))
    err = world.fail(act("DEMO-1", to="In Progress", assign="me"), yes=True, expect_plan=held)
    assert (err.exit, err.code) == (12, "partial")
    assert err.details is not None
    assert [(s["kind"], s["status"]) for s in err.details["result"]["steps"]] == [
        ("assign", "done"),
        ("transition", "failed"),
    ]
    assert err.details["result"]["changed"] is True
    assert world.fake.tickets["DEMO-1"].assignee == ME_ACCOUNT
    assert world.fake.tickets["DEMO-1"].status == "Backlog"

    replan = world.run(act("DEMO-1", to="In Progress", assign="me"), dry_run=True)
    assert kinds(replan) == ["transition"] and replan["plan_sha256"] == held
    result = world.run(act("DEMO-1", to="In Progress", assign="me"), yes=True, expect_plan=held)
    assert (result["result"], kinds(result)) == ("ok", ["transition"])
    assert world.fake.tickets["DEMO-1"].status == "In Progress"
    assert sum(1 for c in world.writes() if c.path.endswith("/assignee")) == 1


def test_a_provider_400_on_an_unassigned_ticket_without_an_assign_step_carries_the_hint(
    world: World,
) -> None:
    err = world.fail(
        act("DEMO-1", to="In Progress"),
        yes=True,
        expect_plan=world.hash_of(act("DEMO-1", to="In Progress")),
    )
    assert (err.exit, err.code) == (12, "rejected")
    assert err.details is not None and err.details["reason_hint"] == "needs_assignee"
    assert err.details["attempt_id"] == ATTEMPT
    assert world.fake.tickets["DEMO-1"].status == "Backlog"


def test_the_hint_is_absent_when_the_plan_assigned_or_the_ticket_was_assigned(
    world: World,
) -> None:
    world.fake.quirks.validator_message = "Assignee is required to progress this issue."
    world.fake.tickets["DEMO-2"].assignee = ME_ACCOUNT
    world.fake.inject(Fault("status", method="POST", path="/transitions", status=400, times=1))
    held = world.hash_of(act("DEMO-2", to="Backlog"))
    err = world.fail(act("DEMO-2", to="Backlog"), yes=True, expect_plan=held)
    assert err.code == "rejected"
    assert err.details is None or "reason_hint" not in err.details


def test_the_hint_never_comes_from_the_message_text(world: World) -> None:
    world.fake.quirks.validator_message = "a message that says nothing about assignees"
    err = world.fail(
        act("DEMO-1", to="In Progress"),
        yes=True,
        expect_plan=world.hash_of(act("DEMO-1", to="In Progress")),
    )
    assert err.details is not None and err.details["reason_hint"] == "needs_assignee"


# ---------------------------------------------------------------------------------------------
# Required fields and --field
# ---------------------------------------------------------------------------------------------


@pytest.fixture
def subtask(fake: FakeProvider) -> str:
    fake.add_ticket("DEMO-7", "A sub task", type_id="3", assignee=ME_ACCOUNT)
    return "DEMO-7"


def test_a_required_field_that_was_not_given_is_missing_required_fields(
    world: World, subtask: str
) -> None:
    err = world.fail(act(subtask, to="In Progress"))
    assert (err.exit, err.code, err.reason) == (22, "invalid_request", "missing_required_fields")
    assert err.details == {"required_fields": [{"name": "Parent", "kind": "issuelink"}]}
    assert world.writes() == []


def test_a_required_field_given_with_field_is_sent_and_planned(world: World, subtask: str) -> None:
    action = act(subtask, to="In Progress", fields=["parent=DEMO-1"])
    plan = world.run(action, dry_run=True)
    assert {"name": "parent", "from": None, "to": "DEMO-1"} in plan["changes"]
    apply(world, act(subtask, to="In Progress", fields=["parent=DEMO-1"]))
    body = world.writes()[-1].body
    assert body == {"transition": {"id": "31"}, "fields": {"parent": {"key": "DEMO-1"}}}
    changed = world.run(act(subtask, to="In Progress", fields=["parent=DEMO-2"]), dry_run=True)
    assert changed["steps"] == []  # already in the target: the field is ignored


def test_the_field_value_is_part_of_the_hash(world: World, subtask: str) -> None:
    a = world.hash_of(act(subtask, to="In Progress", fields=["parent=DEMO-1"]))
    b = world.hash_of(act(subtask, to="In Progress", fields=["parent=DEMO-2"]))
    assert a != b


def test_field_names_are_judged_against_the_transitions_own_list(world: World) -> None:
    err = world.fail(act("DEMO-2", to="Done", resolution="Done", fields=["parent=DEMO-1"]))
    assert (err.exit, err.reason) == (22, "not_editable")
    err = world.fail(act("DEMO-2", to="Done", resolution="Done", fields=["Mystery=1"]))
    assert (err.exit, err.reason) == (22, "unknown_field")
    err = world.fail(act("DEMO-2", to="Done", resolution="Done", fields=["customfield_90001=1"]))
    assert err.reason == "unknown_field"


@pytest.mark.parametrize("name", ["resolution", "Resolution", "assignee", "labels", "title"])
def test_dedicated_fields_are_refused_with_field(world: World, name: str) -> None:
    err = world.fail(act("DEMO-2", to="Done", resolution="Done", fields=[f"{name}=x"]))
    assert (err.exit, err.reason) == (2, "invalid_combination")


def test_a_comment_field_is_sent_as_update_comment_with_no_property(
    rich_fake: RichFake, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    w = World(tmp_path, monkeypatch, rich_fake)
    note = tmp_path / "note.md"
    note.write_text("Closing **now**.\n", encoding="utf-8")
    action = act(
        "DEMO-2", to="Done", resolution="Done", fields=[f"Comment=@{note}", "Story Points=5"]
    )
    plan = w.run(action, dry_run=True)
    validator("cli/transition.json").validate(plan)
    comment = next(c for c in plan["changes"] if c["name"] == "Comment")
    assert comment["to"] == "Closing **now**."  # a field value carries no attribution
    assert {"name": "Story Points", "from": None, "to": "5"} in plan["changes"]
    apply(
        w,
        act("DEMO-2", to="Done", resolution="Done", fields=[f"Comment=@{note}", "Story Points=5"]),
    )
    body = rich_fake.bodies[-1]
    assert body["fields"] == {"resolution": {"name": "Done"}, fp.F_POINTS: 5}
    adds = body["update"]["comment"]
    assert len(adds) == 1 and list(adds[0]) == ["add"]
    assert adds[0]["add"]["body"]["type"] == "doc" and "properties" not in adds[0]["add"]


def test_a_rich_text_field_by_an_automatic_actor_is_refused(
    rich_fake: RichFake, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    w = World(tmp_path, monkeypatch, rich_fake)
    note = tmp_path / "note.md"
    note.write_text("text\n", encoding="utf-8")
    err = w.fail(
        act("DEMO-2", to="Done", resolution="Done", fields=[f"Comment=@{note}"]),
        actor="auto:unknown",
    )
    assert (err.exit, err.reason) == (22, "invalid_value")


def test_the_converted_values_survive_the_fresh_read_under_the_lock(
    rich_fake: RichFake, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A source read once (stdin) is not read again: the fresh read reuses the converted value."""
    w = World(tmp_path, monkeypatch, rich_fake)
    note = tmp_path / "note.md"
    note.write_text("text\n", encoding="utf-8")
    fields = [f"Comment=@{note}"]
    held = w.hash_of(act("DEMO-2", to="Done", resolution="Done", fields=fields))
    hooks = Hooks()
    hooks.before = note.unlink  # the pre hook runs before the fresh read
    out = w.run(
        act("DEMO-2", to="Done", resolution="Done", fields=fields),
        yes=True,
        expect_plan=held,
        seams=Seams(hooks=hooks),
    )
    assert out["result"] == "ok"


def test_a_cleared_comment_is_refused(
    rich_fake: RichFake, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    w = World(tmp_path, monkeypatch, rich_fake)
    err = w.fail(act("DEMO-2", to="Done", resolution="Done", fields=["Comment="]))
    assert err.reason == "invalid_value"


# ---------------------------------------------------------------------------------------------
# Warnings, staleness, hooks
# ---------------------------------------------------------------------------------------------


def test_a_transition_out_of_a_done_status_warns_closed_ticket(world: World) -> None:
    plan = world.run(act("DEMO-3", to="Backlog"), dry_run=True)
    assert plan["warnings"] == ["closed_ticket"]


def test_a_changed_status_between_plan_and_apply_is_stale_ticket(world: World) -> None:
    held = world.hash_of(act("DEMO-2", to="Done", resolution="Done"))
    world.fake.tickets["DEMO-2"].status = "Backlog"
    err = world.fail(act("DEMO-2", to="Done", resolution="Done"), yes=True, expect_plan=held)
    assert (err.exit, err.code) == (20, "stale_ticket")
    assert world.writes() == []


def test_a_new_assignee_between_plan_and_apply_is_stale(world: World) -> None:
    held = world.hash_of(act("DEMO-1", to="In Progress", assign="me"))
    world.fake.tickets["DEMO-1"].assignee = OTHER_ACCOUNT
    err = world.fail(act("DEMO-1", to="In Progress", assign="me"), yes=True, expect_plan=held)
    assert err.code == "stale_ticket"  # the plan said "assign"; now someone else holds it
    assert world.writes() == []


def test_an_assign_that_someone_else_made_moot_is_unchanged_when_nothing_else_remains(
    world: World,
) -> None:
    held = world.hash_of(act("DEMO-1", to="Backlog", assign="me"))
    world.fake.tickets["DEMO-1"].assignee = OTHER_ACCOUNT
    out = world.run(act("DEMO-1", to="Backlog", assign="me"), yes=True, expect_plan=held)
    assert out["result"] == "unchanged" and world.writes() == []


class PayloadHooks(Hooks):
    """Builds the real payloads with the real function and checks them against the schemas."""

    def __init__(self) -> None:
        super().__init__()
        self.payloads: list[dict[str, Any]] = []

    def pre(
        self, plan: dict[str, Any], planned: Planned, attempt_id: str, decision_ids: list[str]
    ) -> HookResult:
        payload = hooks_module.build_payload(
            "pre-transition", plan, planned, attempt_id, decision_ids
        )
        validator("hooks/pre-transition.json").validate(payload)
        self.payloads.append(payload)
        return super().pre(plan, planned, attempt_id, decision_ids)

    def post(
        self,
        plan: dict[str, Any],
        planned: Planned,
        attempt_id: str,
        decision_ids: list[str],
        info: PostInfo,
    ) -> None:
        payload = hooks_module.build_payload(
            "post-transition", plan, planned, attempt_id, decision_ids, info
        )
        validator("hooks/post-transition.json").validate(payload)
        self.payloads.append(payload)
        super().post(plan, planned, attempt_id, decision_ids, info)


def test_the_hook_payloads_validate_through_the_real_builder(world: World) -> None:
    hooks = PayloadHooks()
    action = act("DEMO-1", to="Done", resolution="Done", assign="me")
    held = world.hash_of(action)
    world.run(
        act("DEMO-1", to="Done", resolution="Done", assign="me"),
        yes=True,
        expect_plan=held,
        seams=Seams(hooks=hooks),
    )
    pre, post = hooks.payloads
    assert pre["project"] == "DEMO" and pre["object_id"] == "DEMO-1"
    assert pre["plan"]["current_status"] == {"name": "Backlog", "category": "new"}
    assert pre["plan"]["target_status"] == {"name": "Done", "category": "done"}
    assert (pre["plan"]["resolution"], pre["plan"]["assign"]) == ("Done", "me")
    assert pre["plan"]["steps"] == ["assign", "transition"]
    assert pre["ticket"] == {
        "type": "Task",
        "subtask": False,
        "status": "Backlog",
        "priority": "Medium",
        "assignee_account_id": None,
        "assignee_is_me": False,
    }
    assert set(pre["plan"]["fields_changed"]) == {"status", "resolution", "assignee"}
    assert (post["result"], post["changed"]) == ("ok", True)


def test_a_veto_stops_before_any_write(world: World) -> None:
    hooks = Hooks()
    hooks.veto = TicketctlError("vetoed", "no")
    held = world.hash_of(act("DEMO-2", to="Done", resolution="Done"))
    err = world.fail(
        act("DEMO-2", to="Done", resolution="Done"),
        yes=True,
        expect_plan=held,
        seams=Seams(hooks=hooks),
    )
    assert err.exit == 11 and world.writes() == [] and world.records() == []


def test_the_unknown_outcome_of_the_transition_is_never_resent(world: World) -> None:
    world.fake.inject(Fault("reset", method="POST", path="/transitions", after_apply=True, times=1))
    held = world.hash_of(act("DEMO-2", to="Done", resolution="Done"))
    err = world.fail(act("DEMO-2", to="Done", resolution="Done"), yes=True, expect_plan=held)
    assert (err.code, err.outcome_unknown) == ("outcome_unknown", True)
    assert sum(1 for c in world.writes() if c.path.endswith("/transitions")) == 1
    again = world.run(act("DEMO-2", to="Done", resolution="Done"), yes=True, expect_plan=held)
    assert again["result"] == "unchanged"  # the write had landed


# ---------------------------------------------------------------------------------------------
# The CLI
# ---------------------------------------------------------------------------------------------


@pytest.fixture
def cli(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: Any, fake: FakeProvider) -> Cli:
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    monkeypatch.setenv(TOKEN_VAR, SITE_TOKEN)
    return Cli(tmp_path, fake, capsys)


def test_the_registered_handler_is_this_module() -> None:
    spec = commands.REGISTRY[("transition",)]
    assert spec.handler is transitioncmd.transition and spec.kind == "write"


def test_the_whole_cycle_through_the_cli_and_the_goldens(cli: Cli) -> None:
    code, plan = cli.doc(
        "--dry-run", "transition", "DEMO-2", "--to", "Done", "--resolution", "Done"
    )
    assert code == 0
    assert_golden("cli", "transition", "plan", cli.norm(plan))
    code, result = cli.doc(
        "--yes", "--expect-plan", plan["plan_sha256"],
        "transition", "DEMO-2", "--to", "Done", "--resolution", "Done",
    )  # fmt: skip
    assert code == 0 and result["result"] == "ok"
    assert result["url"] == f"{cli.fake.url}/browse/DEMO-2"
    result["attempt_id"] = ATTEMPT
    result["url"] = f"{SITE_URL}/browse/DEMO-2"
    assert_golden("cli", "transition", "ok", result)
    code, again = cli.doc("transition", "DEMO-2", "--to", "Done", "--resolution", "Done")
    assert code == 0 and again["result"] == "unchanged"
    again["url"] = f"{SITE_URL}/browse/DEMO-2"
    assert_golden("cli", "transition", "unchanged", again)


def test_exit_codes_and_output_of_the_gates_through_the_cli(cli: Cli) -> None:
    code, env = cli.doc("transition", "DEMO-2", "--to", "Done", "--resolution", "Done")
    assert code == 13 and env["error"]["code"] == "confirmation_required"
    code, out, _ = cli.run(
        "--dry-run", "transition", "DEMO-1", "--to", "In Progress", "--assign", "me"
    )
    assert code == 0 and "step: assign" in out and "step: transition" in out


@pytest.mark.parametrize(
    "argv",
    [
        ["--to", "Done", "--transition-id", "41"],
        [],
        ["--to", "a\nb"],
        ["--to", "x" * 300],
        ["--to", " "],
        ["--if-status", "bad\x00", "--to", "Done"],
        ["--to", "Done", "--assign", "none"],
        ["--to", "Done", "--assign", "bad id!"],
        ["--to", "Done", "--assign", "ME"],
        ["--transition-id", "4x"],
        ["--to", "Done", "--field", "resolution=Done"],
        ["--to", "Done", "--field", "noequals"],
    ],
)
def test_bad_arguments_are_exit_2_and_send_nothing(cli: Cli, argv: list[str]) -> None:
    code, env = cli.doc("--dry-run", "transition", "DEMO-2", *argv)
    assert code == 2 and env["error"]["code"] == "usage"
    assert cli.fake.calls == []


def test_the_site_is_chosen_from_the_key_and_a_missing_ticket_is_exit_10(cli: Cli) -> None:
    code, env = cli.doc("--dry-run", "transition", "DEMO-99", "--to", "Done")
    assert (code, env["error"]["code"]) == (10, "not_found")
    code, env = cli.doc("--dry-run", "transition", "not a key", "--to", "Done")
    assert code == 22


def test_the_result_and_error_documents_validate(cli: Cli) -> None:
    code, env = cli.doc("--dry-run", "transition", "DEMO-2", "--to", "Nowhere")
    assert (code, env["error"]["code"]) == (20, "no_such_transition")
    validator("cli/error.json").validate(env)
    code, env = cli.doc("--dry-run", "transition", "DEMO-2", "--to", "Done")
    assert (code, env["error"]["reason"]) == (22, "needs_resolution")
    validator("cli/error.json").validate(env)
    assert json.dumps(env).count(SITE_TOKEN) == 0
