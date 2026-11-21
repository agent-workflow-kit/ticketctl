"""The plan object and its canonical hash (docs/interface.md, Plan object).

Pure logic, no I/O. A command's action builds a :class:`Planned` (the steps still to run, the
intent and the facts, the changes shown to the human, the warnings); ``build_plan`` turns it into
the ``schemas/cli/plan.json`` object and ``plan_hash`` computes ``plan_sha256``:

    sha256(canonical JSON of {v: 1, action, site, key, intent, facts})

``key`` is the ticket key, or the project for ``create`` (``Planned.hash_key``). The actor is not
hashed, and the steps are not hashed: they are derived from intent plus facts, so an assign step
that appears or disappears between the plan and the apply does not change the hash. A body that
carries the attribution paragraph already differs in its body hash, so the attribution setting is
part of the intent through the body hashes.

Canonical JSON: sorted keys, separators ``,`` and ``:``, UTF-8, no ASCII escaping; a lone surrogate
(provider text can hold one) is written with ``surrogatepass`` so the hash never raises for it.
A value that JSON cannot hold (NaN, a set, a cycle, nesting past the recursion limit) is a
``ValueError``, which the top-level handler reports as exit 1 ``internal``: it is a caller bug.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .writeflow import StepResult, WriteGateway

HASH_VERSION = 1
ACTIONS = ("transition", "comment", "create", "update", "sprint_add", "sprint_remove")
STEP_KINDS = (
    "assign", "transition", "set_fields", "add_comment", "create", "sprint_add", "sprint_remove",
)  # fmt: skip
WARNINGS = (
    "closed_ticket", "assign_added", "other_sprints_dropped", "sprint_moved", "body_near_limit",
    "duplicate_dedupe_matches", "assign_skipped", "audit_incomplete",
)  # fmt: skip
# The properties a step may carry, by kind (the plan's `step` definition is closed).
STEP_PROPS: dict[str, frozenset[str]] = {
    "assign": frozenset({"account"}),
    "transition": frozenset({"transition_id", "to"}),
    "set_fields": frozenset({"fields"}),
    "add_comment": frozenset({"body_sha256", "body_bytes"}),
    "create": frozenset({"project", "type", "body_sha256", "body_bytes"}),
    "sprint_add": frozenset({"sprint"}),
    "sprint_remove": frozenset({"sprint"}),
}


@dataclass
class Step:
    """One provider call: its public description and the function that performs it.

    ``props`` are the plan's properties of the step (``STEP_PROPS``); ``send`` receives the
    guarded gateway (every ADF document in a request body is validated before the request) and
    returns what the audit and the result need. It raises ``TicketctlError`` or ``GatewayError``
    for a failure; any other exception is treated as an unknown outcome.
    """

    kind: str
    send: Callable[[WriteGateway], StepResult]
    props: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.kind not in STEP_PROPS:
            raise ValueError(f"unknown step kind {self.kind!r}")
        extra = set(self.props) - STEP_PROPS[self.kind]
        if extra:
            raise ValueError(f"step {self.kind} cannot carry {sorted(extra)}")

    def public(self) -> dict[str, Any]:
        return {"kind": self.kind, **self.props}


@dataclass
class Planned:
    """What an action reads and decides; ``steps`` empty means the intended state already holds.

    ``intent`` and ``facts`` feed the hash only (see the module docstring); ``changes`` is the
    list of ``{name, from, to}`` shown to the human (a rich-text ``to`` is the rendered Markdown
    string, attribution paragraph included); ``bodies`` is ``[{sha256, bytes}]`` for the audit and
    the hooks. ``key`` is null for create, ``hash_key`` is what the hash holds in its ``key``
    place (the project for create, else the key). For a no-op, ``already_posted``, ``reused`` and
    ``comment_id`` describe the existing object (and ``existing_key`` the reused ticket).
    """

    steps: list[Step]
    intent: dict[str, Any]
    facts: dict[str, Any] = field(default_factory=dict)
    changes: list[dict[str, Any]] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    key: str | None = None
    hash_key: str | None = None
    dedupe_key: str | None = None
    dedupe_match: str | None = None
    bodies: list[dict[str, Any]] = field(default_factory=list)
    already_posted: bool = False
    reused: bool = False
    comment_id: str | None = None
    existing_key: str | None = None

    def __post_init__(self) -> None:
        if self.hash_key is None:
            self.hash_key = self.key


def canonical_bytes(document: Any) -> bytes:
    try:
        text = json.dumps(
            document, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False
        )
        return text.encode("utf-8", "surrogatepass")
    except (TypeError, ValueError, RecursionError):
        raise ValueError("the plan cannot be written as canonical JSON") from None


def plan_hash(action: str, site: str, planned: Planned) -> str:
    document = {
        "v": HASH_VERSION,
        "action": action,
        "site": site,
        "key": planned.hash_key,
        "intent": planned.intent,
        "facts": planned.facts,
    }
    return hashlib.sha256(canonical_bytes(document)).hexdigest()


def build_plan(action: str, site: str, actor: str, planned: Planned) -> dict[str, Any]:
    """The ``schemas/cli/plan.json`` object; ``ValueError`` for a warning outside the enum."""
    if action not in ACTIONS:
        raise ValueError(f"unknown action {action!r}")
    bad = [w for w in planned.warnings if w not in WARNINGS]
    if bad:
        raise ValueError(f"unknown warning code {bad[0]!r}")
    warnings = list(dict.fromkeys(planned.warnings))
    return {
        "action": action,
        "site": site,
        "key": planned.key,
        "actor": actor,
        "steps": [s.public() for s in planned.steps],
        "changes": planned.changes,
        "warnings": warnings,
        "dedupe_key": planned.dedupe_key,
        "dedupe_match": planned.dedupe_match,
        "plan_sha256": plan_hash(action, site, planned),
    }
