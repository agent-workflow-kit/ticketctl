"""Handler and write action of ``transition KEY`` (docs/interface.md, Transition order).

``TransitionAction`` is a :class:`~ticketctl.writeflow.WriteAction`: the engine owns the order
(fresh ``/myself``, dry-run, ``--expect-plan``, confirmation, hooks, lock, audit, outcomes); this
module owns what is transition specific.

* ``reduce`` reads the ticket and, only while a step can remain, the live transition list (the same
  ``transitionscmd`` parser as ``transitions KEY``). Idempotency comes first: a ticket already in
  the target status has no transition step (``--to`` compares the typed name, ``--transition-id``
  the listed transition's target), and an assign step that is already satisfied is not sent, so a
  re-run after a ``partial`` sends only the remaining steps with the same plan hash.
* Refusals that need the provider context (``no_such_transition``, ``ambiguous_transition``,
  ``needs_resolution``, an invalid resolution, ``missing_required_fields``) are found by ``reduce``
  but raised by ``check``, so ``--if-status`` (``precondition_failed``) always comes first. An id
  that the live list does not hold is the exception: the interface decides it in the idempotency
  test, so ``reduce`` raises ``no_such_transition`` for it.
* ``--field`` values go through ``setspec`` and ``setconvert`` against the transition's field list.
  The converted values are kept for the fresh read under the lock (a ``-`` source can be read once).
  A comment field (schema type ``comment``, operation ``add``) is sent as ``update.comment``, every
  other field as ``fields``.
* The hash facts are the current status name and the assignee only when it is someone other than
  ``me`` and the requested target; the assign target is part of the intent whether or not a step
  results, so a re-run after the assign landed has the same hash.

Unverified against a real site: that a transition's ``comment`` field takes ``update.comment`` with
an ADF body, that a transition without a screen refuses ``fields.resolution``, and that the status
object of an issue read carries ``statusCategory.key``.
"""

from __future__ import annotations

import argparse
import hashlib
from dataclasses import dataclass, field, replace
from typing import Any

from . import selection, setspec, writeflow
from .adf_render import render_adf
from .config import LOGICAL_FIELDS
from .errors import TicketctlError
from .fieldload import load_resolutions, load_site_fields
from .fieldmap import MAX_CANDIDATES, SiteFields
from .flags import Globals
from .plan import Planned, ShownText, Step, canonical_bytes
from .readutil import hide_field_ids, object_error, warn_incomplete
from .runtime import load_runtime
from .sanitize import CAPS, GRAMMARS, query, sanitize_field, sanitize_line, segment
from .setconvert import FieldValue, ValueContext, convert_all
from .transitionscmd import (
    MAX_TRANSITIONS,
    TRANSITIONS_TEMPLATE,
    transition_list,
    transition_object,
)
from .writeflow import Context, PrepareContext, StepResult, WriteGateway, raise_for_write

ISSUE_TEMPLATE = "/rest/api/3/issue/{key}"
ASSIGN_TEMPLATE = "/rest/api/3/issue/{key}/assignee"
TICKET_FIELDS = "status,assignee,issuetype,priority"
NAME_CAP = CAPS["name"]


# ---- argument types (exit 2 invalid_argument through argparse) ------------------------


def name_arg(value: str) -> str:
    """``--to`` and ``--if-status``: one line of visible text, at most 256 characters."""
    if not value.strip() or len(value) > NAME_CAP or sanitize_line(value) != value:
        raise argparse.ArgumentTypeError("needs one line of visible text of at most 256 characters")
    return value


def assign_arg(value: str) -> str:
    """``--assign`` on ``transition``: ``me`` or an account id (``none`` cannot be "if free")."""
    if value == "me":
        return value
    if value.casefold() not in ("me", "none") and GRAMMARS["account_id"].matches(value):
        return value
    raise argparse.ArgumentTypeError("needs me or an account id")


# ---- helpers --------------------------------------------------------------------------


def _text(value: object, what: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise TicketctlError("provider_changed", f"the ticket has no readable {what}")
    return sanitize_field(value, cap=NAME_CAP)[0]


def _issue_path(key: str) -> str:
    return f"/rest/api/3/issue/{segment('ticket_key', key)}"


def _digest(value: object) -> str:
    return hashlib.sha256(canonical_bytes(value)).hexdigest()


def _same(a: str, b: str) -> bool:
    return a.casefold() == b.casefold()


@dataclass(frozen=True)
class TicketState:
    key: str
    status: dict[str, str]  # {name, category}
    assignee_present: bool
    assignee_id: str | None
    type: str
    subtask: bool
    priority: str | None


def _read_ticket(ctx: Context, key: str) -> TicketState:
    res = ctx.gw.request(
        "GET", _issue_path(key), template=ISSUE_TEMPLATE, query=query({"fields": TICKET_FIELDS})
    )
    if not 200 <= res.status < 300:
        raise object_error(res.status, "ticket")
    data = res.data
    fields = data.get("fields") if isinstance(data, dict) else None
    if not isinstance(fields, dict):
        raise TicketctlError("provider_changed", "the ticket has no fields")
    status = fields.get("status")
    if not isinstance(status, dict):
        raise TicketctlError("provider_changed", "the ticket has no readable status")
    category = status.get("statusCategory")
    cat = category.get("key") if isinstance(category, dict) else None
    if cat not in ("new", "indeterminate", "done"):
        # Jira also has the category key "undefined"; it is not a closed ticket.
        cat = "indeterminate"
    kind = fields.get("issuetype")
    if not isinstance(kind, dict) or not isinstance(kind.get("subtask", False), bool):
        raise TicketctlError("provider_changed", "the ticket has no readable type")
    assignee = fields.get("assignee")
    assignee_id = assignee.get("accountId") if isinstance(assignee, dict) else None
    priority = fields.get("priority")
    priority_name = priority.get("name") if isinstance(priority, dict) else None
    return TicketState(
        key,
        {"name": _text(status.get("name"), "status"), "category": str(cat)},
        isinstance(assignee, dict),
        assignee_id if GRAMMARS["account_id"].matches(assignee_id) else None,
        _text(kind.get("name"), "type"),
        bool(kind.get("subtask", False)),
        _text(priority_name, "priority") if priority_name else None,
    )


@dataclass
class Listing:
    transitions: list[dict[str, Any]]
    raw: dict[str, dict[str, Any]]  # by transition id: the raw item of the kept transition
    marks: set[str] = field(default_factory=set)


def _read_transitions(ctx: Context, key: str) -> Listing:
    res = ctx.gw.request(
        "GET",
        f"{_issue_path(key)}/transitions",
        template=TRANSITIONS_TEMPLATE,
        query=query({"expand": "transitions.fields"}),
    )
    if not 200 <= res.status < 300:
        raise object_error(res.status, "ticket")
    marks: set[str] = set()
    listed = transition_list(res.data, marks)
    # The raw item is the one transition_list kept: the first readable item of that id.
    raw: dict[str, dict[str, Any]] = {}
    items = res.data.get("transitions") if isinstance(res.data, dict) else None
    for item in (items if isinstance(items, list) else [])[:MAX_TRANSITIONS]:
        kept = transition_object(item, set())
        if kept is not None and kept["id"] not in raw:
            raw[kept["id"]] = item
    return Listing(listed, raw, marks)


def _targets(transitions: list[dict[str, Any]]) -> list[str]:
    return sorted({t["to"]["name"] for t in transitions})[:MAX_CANDIDATES]


def _display(payload: object) -> str | list[str] | None:
    """The value a human reads for a converted payload (``None`` clears)."""
    if payload is None or payload == []:
        return None
    if isinstance(payload, str):
        return payload
    if isinstance(payload, dict):
        for name in ("accountId", "value", "name", "key"):
            if isinstance(payload.get(name), str):
                return str(payload[name])
    if isinstance(payload, list):
        items = [_display(i) for i in payload]
        if all(isinstance(i, str) for i in items):
            return [str(i) for i in items]
    if isinstance(payload, int | float) and not isinstance(payload, bool):
        return str(payload)
    return sanitize_field(canonical_bytes(payload).decode("utf-8", "replace"), cap=500)[0]


@dataclass
class _Fields:
    """The ``--field`` values of one reduce: where each goes and how it is shown."""

    fields: dict[str, Any] = field(default_factory=dict)
    update: dict[str, Any] = field(default_factory=dict)
    hashes: dict[str, str] = field(default_factory=dict)
    changes: list[dict[str, Any]] = field(default_factory=list)
    shown: list[ShownText] = field(default_factory=list)
    bodies: list[dict[str, Any]] = field(default_factory=list)
    names: list[str] = field(default_factory=list)  # display names of the fields given


class TransitionAction:
    """``transition KEY --to <status> | --transition-id <id>`` and its options."""

    name = "transition"
    yes_needs_dedupe_key = False
    dedupe_key: str | None = None

    def __init__(
        self,
        key: str,
        *,
        to: str | None = None,
        transition_id: str | None = None,
        resolution: str | None = None,
        assign: str | None = None,
        fields: list[str] | None = None,
        if_status: str | None = None,
    ) -> None:
        if (to is None) == (transition_id is None):
            raise ValueError("exactly one of to and transition_id")
        self.key = key
        self.to = to
        self.transition_id = transition_id
        self.resolution = resolution
        self.assign = assign
        self.field_args = list(fields or [])
        self.if_status = if_status
        self.assignments: list[setspec.Assignment] = []
        self._errors: list[TicketctlError] = []
        self._current: str = ""
        self._values: dict[tuple[str, str, str | None], FieldValue] = {}

    # ------------------------------------------------------------------------------------
    def prepare(self, pc: PrepareContext) -> None:
        self.assignments = setspec.check_offline("transition", self.field_args, "--field")
        pc.check_secrets(*(t for t in (self.resolution, self.to, self.if_status) if t))

    # ------------------------------------------------------------------------------------
    def reduce(self, ctx: Context, *, fresh: bool) -> Planned:
        self._errors = []
        ticket = _read_ticket(ctx, self.key)
        self._current = ticket.status["name"]
        cfg = ctx.rt.config.sites[ctx.site].transitions
        listing: Listing | None = None
        chosen: dict[str, Any] | None = None  # the normalized transition (transitionscmd)
        if self.transition_id is not None:
            listing = _read_transitions(ctx, self.key)
            chosen = next((t for t in listing.transitions if t["id"] == self.transition_id), None)
            if chosen is None:  # rule 1: an id the live list lacks is refused even for a no-op
                warn_incomplete(ctx.rt, "the transitions", listing.marks)
                raise self._no_such(listing)
            at_target = _same(chosen["to"]["name"], ticket.status["name"])
        else:
            assert self.to is not None
            at_target = _same(self.to, ticket.status["name"])
            if not at_target:
                listing = _read_transitions(ctx, self.key)
                hits = [t for t in listing.transitions if _same(t["to"]["name"], self.to)]
                if len(hits) == 1:
                    chosen = hits[0]
                elif hits:
                    self._errors.append(self._ambiguous(hits))
                else:
                    self._errors.append(self._no_such(listing))
        if (
            listing is not None
            and listing.marks
            and (chosen is None or "required_fields" in listing.marks)
        ):
            warn_incomplete(ctx.rt, "the transitions", listing.marks)
        send_transition = not at_target

        # --- assign ---
        me = ctx.me.account_id
        target_account: str | None = None
        assign_intent: str | None = None
        if self.assign is not None:
            assign_intent = self.assign
        elif (
            cfg.assign_on_start == "me"
            and send_transition
            and chosen is not None
            and chosen["to"]["category"] != "done"
        ):
            assign_intent = "me"
        if assign_intent is not None:
            target_account = me if assign_intent == "me" else assign_intent
        has_assignee = ticket.assignee_present
        assign_step = assign_intent is not None and not has_assignee
        warnings: list[str] = []
        # --assign never replaces an assignee; it says so unless the assignee already is the target.
        if self.assign is not None and has_assignee and ticket.assignee_id != target_account:
            warnings.append("assign_skipped")
        if assign_step:
            warnings.append("assign_added")
        if ticket.status["category"] == "done":
            warnings.append("closed_ticket")

        # --- resolution and fields: only while a transition step remains ---
        resolution_name: str | None = None
        extra = _Fields()
        required_missing: list[dict[str, Any]] = []
        if send_transition and chosen is not None:
            raw = listing.raw.get(chosen["id"], {}) if listing is not None else {}
            meta = self._meta(raw)
            resolution_name = self._resolution(ctx, chosen, meta, cfg.default_resolution)
            extra = self._fields(ctx, meta)
            supplied = {hide_field_ids(n) for n in extra.names}
            res_meta = next((m for m in meta if m.field.id == "resolution"), None)
            if res_meta is not None:
                supplied.add(hide_field_ids(res_meta.field.name))
            required_missing = [f for f in chosen["required_fields"] if f["name"] not in supplied]
            if required_missing:
                self._errors.append(
                    TicketctlError(
                        "invalid_request",
                        "the transition needs fields that were not given (use --field)",
                        reason="missing_required_fields",
                        details={"required_fields": required_missing},
                    )
                )

        # --- steps ---
        steps: list[Step] = []
        if assign_step:
            assert target_account is not None
            steps.append(
                self._assign_step(target_account, "me" if assign_intent == "me" else target_account)
            )
        target = chosen["to"] if chosen is not None else None
        if send_transition:
            steps.append(
                self._transition_step(
                    chosen,
                    resolution_name,
                    extra,
                    hint=(not ticket.assignee_present and not assign_step),
                )
            )

        other: str | None = None
        if has_assignee and ticket.assignee_id not in (me, target_account):
            other = ticket.assignee_id or "unknown"
        if target is not None:
            to_name = target["name"]
        elif not send_transition:
            to_name = ticket.status["name"]
        else:  # no transition resolved: check raises the recorded refusal before the hash
            to_name = self.to or ""
        intent = {
            "to": to_name,
            "transition_id": chosen["id"] if (chosen is not None and send_transition) else None,
            "resolution": resolution_name if send_transition else None,
            "assign": assign_intent,
            "fields": extra.hashes,
        }
        facts = {"status": ticket.status["name"].casefold(), "assignee": other}
        changes: list[dict[str, Any]] = []
        shown: list[ShownText] = [*extra.shown]
        if send_transition:
            to_status = {
                "name": target["name"] if target else self.to or ticket.status["name"],
                "category": target["category"] if target else ticket.status["category"],
            }
            changes.append({"name": "status", "from": dict(ticket.status), "to": to_status})
            shown.append(
                ShownText("status", f"{ticket.status['name']} -> {to_status['name']}", "line")
            )
        if send_transition and resolution_name is not None:
            changes.append({"name": "resolution", "from": None, "to": resolution_name})
            shown.append(ShownText("resolution", resolution_name, "line"))
        if assign_step:
            shown_to = assign_intent or "me"
            changes.append({"name": "assignee", "from": None, "to": shown_to})
            shown.append(ShownText("assignee", shown_to, "line"))
        changes.extend(extra.changes)

        current_status = dict(ticket.status)
        hook: dict[str, Any] = {
            "project": self.key.rpartition("-")[0],
            "ticket": {
                "type": ticket.type,
                "subtask": ticket.subtask,
                "status": ticket.status["name"],
                "priority": ticket.priority,
                "assignee_account_id": ticket.assignee_id,
                "assignee_is_me": ticket.assignee_id == me,
            },
            "current_status": current_status,
            "target_status": dict(target) if (target and send_transition) else current_status,
            "resolution": resolution_name if send_transition else None,
            "assign": assign_intent if assign_step else None,
        }
        return Planned(
            steps=steps,
            intent=intent,
            facts=facts,
            changes=changes,
            warnings=warnings,
            key=self.key,
            bodies=extra.bodies,
            shown=shown,
            hook=hook,
        )

    # ------------------------------------------------------------------------------------
    def check(self, ctx: Context, planned: Planned) -> None:
        if self.if_status is not None and not _same(self.if_status, self._current):
            raise TicketctlError(
                "precondition_failed",
                "the ticket is not in the expected status",
                details={"current_status": self._current, "expected_status": self.if_status},
            )
        if self._errors:
            raise self._errors[0]

    # ------------------------------------------------------------------------------------
    def _no_such(self, listing: Listing) -> TicketctlError:
        return TicketctlError(
            "no_such_transition",
            "no listed transition leads to that status or has that id",
            details={"available_targets": _targets(listing.transitions)},
        )

    @staticmethod
    def _ambiguous(hits: list[dict[str, Any]]) -> TicketctlError:
        names = sorted(f"{t['id']}: {t['name']}" for t in hits)[:MAX_CANDIDATES]
        return TicketctlError(
            "ambiguous_transition",
            "several transitions lead to that status; pick one with --transition-id",
            details={"candidates": names},
        )

    def _meta(self, raw: dict[str, Any]) -> tuple[setspec.MetaField, ...]:
        fields = raw.get("fields")
        if not isinstance(fields, dict):
            return ()
        out: list[setspec.MetaField] = []
        for m in setspec.parse_meta({"fields": fields}):
            ops = m.operations
            # A comment field is posted with ``add``; it is the one way a transition sets one.
            if m.field.schema is not None and m.field.schema.type == "comment" and ops == ("add",):
                m = replace(m, operations=("set",))
            out.append(m)
        return tuple(out)

    def _resolution(
        self,
        ctx: Context,
        chosen: dict[str, Any],
        meta: tuple[setspec.MetaField, ...],
        default: str | None,
    ) -> str | None:
        done = chosen["to"]["category"] == "done"
        if not done:
            if self.resolution is not None:
                self._errors.append(
                    TicketctlError(
                        "invalid_request",
                        "--resolution needs a target status in a done category",
                        reason="invalid_value",
                    )
                )
            return None
        wanted = self.resolution if self.resolution is not None else default
        listed = next((m for m in meta if m.field.id == "resolution"), None)
        allowed = listed.allowed if listed is not None else None
        if wanted is None:
            candidates = list(allowed or ())[:MAX_CANDIDATES]
            self._errors.append(
                TicketctlError(
                    "invalid_request",
                    "a done transition needs --resolution or a default_resolution",
                    reason="needs_resolution",
                    details={"candidates": candidates} if candidates else None,
                )
            )
            return None
        if len(wanted) > NAME_CAP or sanitize_line(wanted) != wanted or not wanted.strip():
            self._errors.append(
                TicketctlError(
                    "invalid_request", "the resolution is not valid", reason="invalid_value"
                )
            )
            return wanted[:NAME_CAP]
        if allowed is None:
            try:
                allowed = load_resolutions(ctx.rt, ctx.site, ctx.gw).names
            except TicketctlError as err:  # raised by check, after --if-status
                self._errors.append(err)
                return wanted
        hits = sorted({a for a in allowed if _same(a, wanted)})
        if len(hits) == 1:
            return hits[0]
        self._errors.append(
            TicketctlError(
                "invalid_request",
                "the resolution matches no allowed value"
                if not hits
                else "several allowed resolutions match",
                reason="invalid_value",
                details={"candidates": sorted(hits or allowed)[:MAX_CANDIDATES]},
            )
        )
        return wanted

    def _fields(self, ctx: Context, meta: tuple[setspec.MetaField, ...]) -> _Fields:
        """The ``--field`` values; an error is recorded for ``check`` (after ``--if-status``)."""
        try:
            return self._collect_fields(ctx, meta)
        except TicketctlError as err:
            self._errors.append(err)
            return _Fields()

    def _collect_fields(self, ctx: Context, meta: tuple[setspec.MetaField, ...]) -> _Fields:
        out = _Fields()
        if not self.assignments:
            return out
        extra_names = ctx.rt.config.sites[ctx.site].fields.extra
        needs = [
            a.name for a in self.assignments if a.name in LOGICAL_FIELDS or a.name in extra_names
        ]
        site_fields: SiteFields = load_site_fields(
            ctx.rt,
            ctx.site,
            ctx.gw,
            needs=needs,
        )
        resolved = setspec.resolve_assignments(
            self.assignments, command="transition", site=site_fields, meta=meta
        )
        values = self._convert(ctx, resolved)
        for r, value in zip(resolved, values, strict=True):
            kind = r.field.schema.type if r.field.schema is not None else ""
            label = (
                r.assignment.name
                if r.assignment.name in LOGICAL_FIELDS or r.assignment.name in extra_names
                else hide_field_ids(r.field.name)
            )
            if value.payload is not None and value.payload != []:
                out.names.append(r.field.name)  # a cleared value does not supply a field
            if kind == "comment":
                if value.payload is None:
                    raise TicketctlError(
                        "invalid_request", "a comment cannot be cleared", reason="invalid_value"
                    )
                out.update[r.field.id] = [{"add": {"body": value.payload}}]
            else:
                out.fields[r.field.id] = value.payload
            digest = value.rich.sha256 if value.rich is not None else None
            out.hashes[r.field.id] = digest or _digest(value.payload)
            if value.rich is not None and value.rich.doc is not None:
                info = writeflow.body_info(value.rich)
                text = render_adf(value.rich.doc).text
                out.bodies.append(info)
                out.shown.append(ShownText(label, text, "body", info["sha256"]))
                out.changes.append({"name": label, "from": None, "to": text})
            else:
                shown = _display(value.payload)
                if isinstance(shown, str):
                    out.shown.append(ShownText(label, shown, "line"))
                out.changes.append({"name": label, "from": None, "to": shown})
        return out

    def _convert(self, ctx: Context, resolved: list[setspec.Resolved]) -> list[FieldValue]:
        """Convert once: a fresh read under the lock reuses the values (a ``-`` source is spent)."""
        keys = [(r.assignment.name, r.field.id, r.field.schema_key) for r in resolved]
        if all(k in self._values for k in keys):
            return [self._values[k] for k in keys]
        att = ctx.rt.config.attribution
        vctx = ValueContext(
            actor=ctx.actor,
            template=att.template,
            attribution_enabled=att.enabled,
            body_chars=ctx.body_chars,
            me_account_id=lambda: ctx.me.account_id,
        )
        values = convert_all(resolved, vctx)
        self._values.update(dict(zip(keys, values, strict=True)))
        return values

    # ------------------------------------------------------------------------------------
    def _assign_step(self, account: str, shown: str) -> Step:
        def send(gw: WriteGateway) -> StepResult:
            raise_for_write(
                gw.request(
                    "PUT",
                    f"{_issue_path(self.key)}/assignee",
                    template=ASSIGN_TEMPLATE,
                    body={"accountId": account},
                ),
                object_path=True,
            )
            return StepResult([])

        return Step("assign", send, {"account": shown})

    def _transition_step(
        self,
        chosen: dict[str, Any] | None,
        resolution: str | None,
        extra: _Fields,
        *,
        hint: bool,
    ) -> Step:
        if chosen is None:  # a refusal is recorded; check raises it before any send

            def refuse(gw: WriteGateway) -> StepResult:
                raise TicketctlError("internal", "no transition was resolved")

            return Step("transition", refuse, {"to": (self.to or "")[:NAME_CAP]})

        body: dict[str, Any] = {"transition": {"id": chosen["id"]}}
        fields = dict(extra.fields)
        if resolution is not None:
            fields["resolution"] = {"name": resolution}
        if fields:
            body["fields"] = fields
        if extra.update:
            body["update"] = dict(extra.update)

        def send(gw: WriteGateway) -> StepResult:
            res = gw.request(
                "POST",
                f"{_issue_path(self.key)}/transitions",
                template=TRANSITIONS_TEMPLATE,
                body=body,
            )
            try:
                raise_for_write(res, object_path=True)
            except TicketctlError as err:
                # Decided from the plan facts, never from the provider's text.
                if err.code == "rejected" and res.status == 400 and hint:
                    err.details = {**(err.details or {}), "reason_hint": "needs_assignee"}
                raise
            return StepResult([])

        return Step("transition", send, {"transition_id": chosen["id"], "to": chosen["to"]["name"]})


# ---- the command ----------------------------------------------------------------------


def transition(g: Globals, a: argparse.Namespace) -> dict[str, Any]:
    rt = load_runtime(g)
    picked = selection.select_site(rt.config, site_flag=g.site, refs=[a.key])
    assert picked.key is not None
    action = TransitionAction(
        picked.key,
        to=a.to,
        transition_id=a.transition_id,
        resolution=a.resolution,
        assign=a.assign,
        fields=a.field,
        if_status=a.if_status,
    )
    return writeflow.execute(rt, picked.site, action)
