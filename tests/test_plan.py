"""The plan object and its canonical hash (issue #23)."""

from __future__ import annotations

import hashlib
import json
from typing import Any

import pytest

from tests.golden import validator
from ticketctl.plan import (
    ACTIONS,
    STEP_KINDS,
    STEP_PROPS,
    WARNINGS,
    Planned,
    Step,
    build_plan,
    canonical_bytes,
    plan_hash,
)
from ticketctl.writeflow import StepResult


def _send(gw: Any) -> StepResult:  # pragma: no cover - never called
    return StepResult([])


def planned(**kw: Any) -> Planned:
    base: dict[str, Any] = {
        "steps": [Step("transition", _send, {"transition_id": "41", "to": "Done"})],
        "intent": {"to": "done", "transition_id": "41"},
        "facts": {"status": "in progress"},
        "key": "DEMO-2",
    }
    base.update(kw)
    return Planned(**base)


def test_hash_is_the_sha256_of_the_canonical_json_of_the_documented_object() -> None:
    p = planned()
    text = (
        '{"action":"transition","facts":{"status":"in progress"},'
        '"intent":{"to":"done","transition_id":"41"},"key":"DEMO-2","site":"main","v":1}'
    )
    assert plan_hash("transition", "main", p) == hashlib.sha256(text.encode()).hexdigest()


def test_hash_ignores_steps_actor_changes_warnings_and_dedupe_match() -> None:
    a = plan_hash("transition", "main", planned())
    b = plan_hash(
        "transition",
        "main",
        planned(
            steps=[],
            changes=[{"name": "status", "from": 1, "to": 2}],
            warnings=["closed_ticket"],
            dedupe_match="X-1",
        ),
    )
    assert a == b


@pytest.mark.parametrize(
    "change",
    [
        {"intent": {"to": "done", "transition_id": "42"}},
        {"facts": {"status": "backlog"}},
        {"key": "DEMO-3"},
        {"hash_key": "DEMO"},
    ],
)
def test_hash_covers_intent_facts_key(change: dict[str, Any]) -> None:
    assert plan_hash("transition", "main", planned(**change)) != plan_hash(
        "transition", "main", planned()
    )


def test_hash_covers_action_and_site() -> None:
    base = plan_hash("transition", "main", planned())
    assert plan_hash("update", "main", planned()) != base
    assert plan_hash("transition", "other", planned()) != base


def test_create_hashes_the_project_not_the_null_key() -> None:
    p = planned(key=None, hash_key="DEMO", steps=[])
    plan = build_plan("create", "main", "human:a", p)
    assert plan["key"] is None
    assert plan["plan_sha256"] == plan_hash("create", "main", p)
    assert plan["plan_sha256"] != plan_hash("create", "main", planned(key=None, steps=[]))


def test_key_order_does_not_matter_and_unicode_is_utf8() -> None:
    a = planned(intent={"b": 1, "a": "é "}, facts={})
    b = planned(intent={"a": "é ", "b": 1}, facts={})
    assert plan_hash("update", "s", a) == plan_hash("update", "s", b)
    assert "é".encode() in canonical_bytes({"a": "é"})


def test_hostile_values() -> None:
    assert plan_hash("update", "s", planned(facts={"x": "\ud800"}))  # a lone surrogate
    for bad in (float("nan"), {1, 2}, object()):
        with pytest.raises(ValueError):
            plan_hash("update", "s", planned(facts={"x": bad}))
    deep: Any = []
    for _ in range(200_000):
        deep = [deep]
    with pytest.raises(ValueError):
        plan_hash("update", "s", planned(facts={"x": deep}))
    huge = "x" * 5_000_000
    assert len(plan_hash("update", "s", planned(facts={"x": huge}))) == 64


def test_build_plan_shape_and_schema() -> None:
    plan = build_plan("transition", "main", "auto:bot", planned(warnings=["assign_added"] * 2))
    validator("cli/plan.json").validate(plan)
    assert plan["steps"] == [{"kind": "transition", "transition_id": "41", "to": "Done"}]
    assert plan["warnings"] == ["assign_added"]  # de-duplicated
    assert set(plan) == {
        "action", "site", "key", "actor", "steps", "changes", "warnings", "dedupe_key",
        "dedupe_match", "plan_sha256",
    }  # fmt: skip
    json.dumps(plan)


def test_empty_steps_is_a_valid_plan() -> None:
    validator("cli/plan.json").validate(build_plan("update", "main", "human:a", planned(steps=[])))


def test_warning_and_action_enums_are_closed() -> None:
    with pytest.raises(ValueError):
        build_plan("transition", "main", "human:a", planned(warnings=["made_up"]))
    with pytest.raises(ValueError):
        build_plan("export", "main", "human:a", planned())


def test_enums_match_the_schema() -> None:
    defs = json.load(open("schemas/defs.json"))["$defs"]
    assert set(ACTIONS) == set(defs["action"]["enum"])
    assert set(STEP_KINDS) == set(defs["step_kind"]["enum"])
    assert set(WARNINGS) == set(defs["warning_code"]["enum"])
    allowed = set(defs["step"]["properties"]) - {"kind"}
    assert set().union(*STEP_PROPS.values()) <= allowed
    assert set(STEP_PROPS) == set(STEP_KINDS)


def test_a_step_refuses_a_foreign_property_or_kind() -> None:
    with pytest.raises(ValueError):
        Step("assign", _send, {"transition_id": "1"})
    with pytest.raises(ValueError):
        Step("teleport", _send)


# Round 1 review findings: hash_key, step body hashes, canonical JSON collisions.


def test_a_create_plan_needs_a_hash_key() -> None:
    step = Step("create", _send, {"project": "DEMO", "type": "Task"})
    with pytest.raises(ValueError):
        build_plan("create", "main", "human:x", Planned(steps=[step], intent={"p": 1}))
    plan = build_plan(
        "create", "main", "human:x", Planned(steps=[step], intent={"p": 1}, hash_key="DEMO")
    )
    assert plan["key"] is None


def test_every_step_body_hash_must_be_in_the_intent() -> None:
    step = Step("add_comment", _send, {"body_sha256": "a" * 64, "body_bytes": 3})
    with pytest.raises(ValueError):
        build_plan("comment", "main", "human:x", planned(steps=[step], intent={"b": ["c" * 64]}))
    ok = planned(steps=[step], intent={"bodies": ["a" * 64]})
    assert build_plan("comment", "main", "human:x", ok)["steps"][0]["body_sha256"] == "a" * 64


def test_canonical_json_refuses_what_would_collide() -> None:
    assert canonical_bytes({"1": "x"}) == b'{"1":"x"}'
    with pytest.raises(ValueError):
        canonical_bytes({1: "x"})
    with pytest.raises(ValueError):
        canonical_bytes({"a": {"b": [{2: 1}]}})
    with pytest.raises(ValueError):
        canonical_bytes({"a": (1, 2)})
    with pytest.raises(ValueError):
        canonical_bytes({"a": [("x",)]})
    assert canonical_bytes({"a": [1, 2]}) == b'{"a":[1,2]}'
