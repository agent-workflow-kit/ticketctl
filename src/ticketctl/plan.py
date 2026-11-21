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
    "duplicate_dedupe_matches", "assign_skipped", "audit_incomplete", "decision_ids_truncated",
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


@dataclass(frozen=True)
class ShownText:
    """One outbound text the confirmation dialog must show (docs/interface.md, Human actions).

    ``kind`` is ``body`` (rendered Markdown of the final ADF, attribution paragraph included; the
    dialog caps it at 200 lines and 20000 characters) or ``line`` (a title or a string value;
    capped at 500 characters). ``sha256`` is the hash of the full final body when it differs from
    the hash of ``text`` (a rich-text value hashes its canonical ADF).
    """

    label: str
    text: str
    kind: str = "body"
    sha256: str | None = None


@dataclass
class Planned:
    """What an action reads and decides; ``steps`` empty means the intended state already holds.

    ``intent`` and ``facts`` feed the hash only (see the module docstring); ``changes`` is the
    list of ``{name, from, to}`` shown to the human (a rich-text ``to`` is the rendered Markdown
    string, attribution paragraph included); ``bodies`` is ``[{sha256, bytes}]`` for the audit and
    the hooks. ``key`` is null for create, ``hash_key`` is what the hash holds in its ``key``
    place (the project for create, else the key). For a no-op, ``already_posted``, ``reused`` and
    ``comment_id`` describe the existing object (and ``existing_key`` the reused ticket). ``shown``
    holds the outbound texts (a comment body, a created ticket's title and description, the
    rendered text of a rich-text change). ``build_plan`` refuses the plan, before any dialog, unless
    everything sent is shown: every step ``body_sha256`` and every ``bodies[*].sha256`` is the hash
    of a ``shown`` text; every change whose ``to`` is a string or a number or a boolean has a
    ``shown`` text of the same label (and, for a string, the text is equal or its hash is a step or
    ``bodies`` hash); a list of strings is shown by the dialog from the change itself. An action
    fills ``shown`` for every such value (the dialog is the human's only view of what is sent).
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
    shown: list[ShownText] = field(default_factory=list)  # outbound text no change entry carries

    def __post_init__(self) -> None:
        if self.hash_key is None:
            self.hash_key = self.key


def canonical_bytes(document: Any) -> bytes:
    try:
        text = json.dumps(
            document, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False
        )
        encoded = text.encode("utf-8", "surrogatepass")
    except (TypeError, ValueError, RecursionError):
        raise ValueError("the plan cannot be written as canonical JSON") from None
    _refuse_ambiguous(document)  # only after dumps: it has proved there is no cycle
    return encoded


def _refuse_ambiguous(document: Any) -> None:
    """``{1: "x"}`` and ``{"1": "x"}`` (and a tuple and a list) dump alike: refuse the first."""
    stack: list[Any] = [document]
    while stack:
        node = stack.pop()
        if isinstance(node, dict):
            if any(not isinstance(k, str) for k in node):
                raise ValueError("the plan has a non-string dictionary key")
            stack.extend(node.values())
        elif isinstance(node, tuple):
            raise ValueError("the plan holds a tuple (use a list)")
        elif isinstance(node, list):
            stack.extend(node)


def _strings(node: Any) -> set[str]:
    out: set[str] = set()
    stack = [node]
    while stack:
        item = stack.pop()
        if isinstance(item, str):
            out.add(item)
        elif isinstance(item, dict):
            stack.extend(item.values())
        elif isinstance(item, list):
            stack.extend(item)
    return out


def _shown_digest(item: ShownText) -> str:
    return item.sha256 or hashlib.sha256(item.text.encode("utf-8", "surrogatepass")).hexdigest()


def _require_shown(planned: Planned) -> None:
    """Fail closed: nothing may be sent that the confirmation dialog would not show."""
    shown = {_shown_digest(s) for s in planned.shown}
    sent = [s.props["body_sha256"] for s in planned.steps if s.props.get("body_sha256") is not None]
    sent += [b.get("sha256") for b in planned.bodies]
    for digest in sent:
        if digest not in shown:
            raise ValueError("a body hash in the plan has no shown text with that hash")
    for change in planned.changes:
        to = change.get("to")
        if not isinstance(to, (str, bool, int, float)):
            continue  # None, a summarized value, or a list the dialog shows from the change
        label = str(change.get("name"))
        same = [s for s in planned.shown if s.label == label]
        if not same:
            raise ValueError(f"change {label!r} has no shown text of that label")
        if isinstance(to, str) and not any(s.text == to or _shown_digest(s) in sent for s in same):
            raise ValueError(f"the shown text of change {label!r} differs from the text sent")


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
    if action == "create" and planned.hash_key is None:
        raise ValueError("a create plan needs hash_key (the project)")
    hashed = _strings(planned.intent)
    for step in planned.steps:
        digest = step.props.get("body_sha256")
        if digest is not None and digest not in hashed:
            raise ValueError("a step body_sha256 is not in the plan intent")
    _require_shown(planned)
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
