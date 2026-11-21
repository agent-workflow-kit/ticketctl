"""``update KEY``: title, assignee, labels and fields in one ``set_fields`` request and an optional
``assign`` step (docs/interface.md, ``update KEY``; ``schemas/cli/update.json``).

The command is a :class:`~ticketctl.writeflow.WriteAction`; the engine owns the flow (prepare,
fresh ``/myself``, dry-run, confirmation, hooks, lock, audit, ``partial`` and ``outcome_unknown``).
This module owns:

* **prepare** (no provider call): the title (``--title`` or ``--title-file``, trimmed, one line, at
  most 255 characters, no configured secret), the label rules (``validate_label``, the same label
  in ``--add-label`` and ``--remove-label`` is exit 22 ``invalid_value``).
* **reduce**: the field list and ``editmeta`` (only with ``--set``), name resolution and
  conversion (``setspec``, ``setconvert``, once per run), then a read of the ticket's current
  values. Every named value is compared canonically with the current one (``fieldwrite``); only
  what differs becomes a step. ``set_fields`` (title, fields and label operations, the provider's
  ``update`` verbs, one ``PUT /issue/{key}``) comes first and ``assign`` second, so a failure of
  the second is exit 12 ``partial``. Nothing is sent when nothing differs (``unchanged``).
* **the plan**: ``intent`` is what was asked for, resolved (title and value hashes, label sets, the
  assign target); ``facts`` are the current values of what the plan changes (for a label operation,
  whether the label is present); ``Planned.shown`` holds the text of every string, number and rich
  text value that is sent, and ``Planned.hook`` the facts the hook payloads need.

``notifyUsers`` is never sent. The notification setting is the provider's default.

Hash and partial runs: the facts of ``set_fields`` are the values before the change, so after a
``partial`` whose ``set_fields`` landed the plan hash of a re-run differs from the held one (the
old values are gone) and a re-run with the old ``--expect-plan`` is exit 20 ``stale_ticket``
carrying the new plan (one step, ``assign``); a re-run without it sends only the remaining step.

``create`` (#31) reuses :mod:`ticketctl.fieldwrite` for the same conversion and comparison.
"""

from __future__ import annotations

import argparse
import hashlib
import re
from dataclasses import dataclass
from typing import Any, cast

from . import audit, fieldload, selection, setspec, writeflow
from .adf_from_markdown import text_chars
from .errors import TicketctlError, usage_error
from .fieldwrite import (
    FieldEdit,
    convert_edits,
    display_from,
    display_to,
    fact,
    fields_body,
    read_object,
    same_value,
)
from .flags import Globals
from .gateway import Gateway
from .plan import Planned, ShownText, Step, canonical_bytes
from .runtime import load_runtime
from .sanitize import (
    CAPS,
    GRAMMARS,
    query,
    read_input,
    sanitize_field,
    sanitize_line,
    segment,
)
from .setconvert import ValueContext, validate_label
from .writeflow import Context, PrepareContext, StepResult, WriteGateway, body_info, raise_for_write

ISSUE = "/rest/api/3/issue"
ISSUE_TEMPLATE = "/rest/api/3/issue/{key}"
EDITMETA_TEMPLATE = "/rest/api/3/issue/{key}/editmeta"
ASSIGNEE_TEMPLATE = "/rest/api/3/issue/{key}/assignee"
TITLE_MAX = 255
NEAR_LIMIT = 0.9
BASE_FIELDS = ("summary", "labels", "assignee", "status", "issuetype", "priority")
CHANGE_FLAGS = ("title", "title_file", "assign", "add_label", "remove_label", "set")
_DEDUPE_LABEL = re.compile(r"tc-[0-9a-f]{16}")
STATUS_CATEGORIES = ("new", "indeterminate", "done")


# ---- the command line --------------------------------------------------------------------------


def assign_arg(text: str) -> str:
    """``--assign``: ``me``, ``none`` (exact lower case) or an account id."""
    if text in ("me", "none"):
        return text
    if text.casefold() in ("me", "none") or not GRAMMARS["account_id"].matches(text):
        raise argparse.ArgumentTypeError("invalid assignee (me, none or an account id)")
    return text


def check_offline(ns: argparse.Namespace) -> None:
    """The flag rules that need nothing else (the CLI runs them before the default actor)."""
    if all(getattr(ns, name) is None for name in CHANGE_FLAGS):
        raise usage_error(
            "missing_option",
            "update needs a change: --title, --title-file, --assign, --add-label, "
            "--remove-label or --set",
        )
    if ns.title is not None and ns.title_file is not None:
        raise usage_error("invalid_combination", "give --title or --title-file, not both")


def _invalid(message: str) -> TicketctlError:
    return TicketctlError("invalid_request", message, reason="invalid_value")


def update(g: Globals, a: argparse.Namespace) -> dict[str, Any]:
    rt = load_runtime(g)
    chosen = selection.select_site(rt.config, site_flag=g.site, refs=[a.key])
    assert chosen.key is not None
    action = UpdateAction(
        chosen.key,
        title=a.title,
        title_file=a.title_file,
        assign=a.assign,
        add_labels=a.add_label or [],
        remove_labels=a.remove_label or [],
        assignments=setspec.check_offline("update", a.set, "--set"),
    )
    return writeflow.execute(rt, chosen.site, action)


# ---- reading the ticket ------------------------------------------------------------------------


@dataclass(frozen=True)
class _Ticket:
    key: str
    fields: dict[str, Any]


def _name(value: object, cap: int = CAPS["name"]) -> str | None:
    if not isinstance(value, dict) or not isinstance(value.get("name"), str):
        return None
    return sanitize_field(value["name"], cap=cap)[0]


def _account(value: object) -> str | None:
    """The assignee's account id; ``"?"`` for an account with no usable id (never equal to a
    target), None for no assignee."""
    if value is None:
        return None
    account = value.get("accountId") if isinstance(value, dict) else None
    return account if GRAMMARS["account_id"].matches(account) else "?"


def _visible(labels: list[str]) -> list[str]:
    return sorted(label for label in labels if not _DEDUPE_LABEL.fullmatch(label))


def _hex(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8", "surrogatepass")).hexdigest()


# ---- the action --------------------------------------------------------------------------------


class UpdateAction:
    """``update KEY``; see the module docstring."""

    name = "update"
    yes_needs_dedupe_key = False
    dedupe_key: str | None = None

    def __init__(
        self,
        key: str,
        *,
        title: str | None = None,
        title_file: str | None = None,
        assign: str | None = None,
        add_labels: list[str] | None = None,
        remove_labels: list[str] | None = None,
        assignments: list[setspec.Assignment] | None = None,
    ) -> None:
        self.key = key
        self._title_arg = title
        self._title_file = title_file
        self.assign = assign
        self.add_labels = list(dict.fromkeys(add_labels or []))
        self.remove_labels = list(dict.fromkeys(remove_labels or []))
        self.assignments = assignments or []
        self.title: str | None = None
        self._edits: list[FieldEdit] | None = None

    # -- step 1 ----------------------------------------------------------------------------
    def prepare(self, pc: PrepareContext) -> None:
        self.title = self._read_title(pc)
        for label in (*self.add_labels, *self.remove_labels):
            validate_label(label)
        both = sorted(set(self.add_labels) & set(self.remove_labels))
        if both:
            raise _invalid("the same label is in --add-label and --remove-label")
        named = [self.title] if self.title is not None else []
        if self.assign not in (None, "me", "none"):
            named.append(self.assign)
        pc.check_secrets(*named, *self.add_labels, *self.remove_labels)

    def _read_title(self, pc: PrepareContext) -> str | None:
        if self._title_file is not None:
            text = read_input(self._title_file, kind="title", body_chars=pc.body_chars)
            text = text.removesuffix("\r\n") if text.endswith("\r\n") else text.removesuffix("\n")
        elif self._title_arg is not None:
            text = self._title_arg
        else:
            return None
        text = text.strip()
        if not text:
            raise _invalid("the title is empty")
        if len(text) > TITLE_MAX:
            raise _invalid(f"the title is longer than {TITLE_MAX} characters")
        if sanitize_line(text) != text:
            raise _invalid("the title must be one line of visible text")
        return text

    # -- steps 2 and 3 ---------------------------------------------------------------------
    def reduce(self, ctx: Context, *, fresh: bool) -> Planned:
        if self._edits is None:  # names and values are resolved once; only the ticket is re-read
            self._edits = self._resolve(ctx)
        return self._plan(ctx, self._read(ctx, self._edits), self._edits)

    def check(self, ctx: Context, planned: Planned) -> None:
        # unknown_field, not_editable and every value refusal ran in reduce (they are needed to
        # decide what remains); update has no other provider-context check.
        return None

    def _resolve(self, ctx: Context) -> list[FieldEdit]:
        if not self.assignments:
            return []
        gw = cast(Gateway, ctx.gw)  # the guarded gateway answers the same GET interface
        site_fields = fieldload.load_site_fields(
            ctx.rt, ctx.site, gw, needs=[a.name for a in self.assignments]
        )
        path = f"{ISSUE}/{segment('ticket_key', self.key)}"
        meta = setspec.parse_meta(read_object(ctx, f"{path}/editmeta", EDITMETA_TEMPLATE))
        resolved = setspec.resolve_assignments(
            self.assignments, command="update", site=site_fields, meta=meta
        )
        attribution = ctx.rt.config.attribution
        values = ValueContext(
            actor=ctx.actor,
            template=attribution.template,
            attribution_enabled=attribution.enabled,
            body_chars=ctx.body_chars,
            me_account_id=lambda: ctx.me.account_id,
        )
        return convert_edits(resolved, site_fields, values)

    def _read(self, ctx: Context, edits: list[FieldEdit]) -> _Ticket:
        ids = list(dict.fromkeys([*BASE_FIELDS, *(e.field.id for e in edits)]))
        data = read_object(
            ctx,
            f"{ISSUE}/{segment('ticket_key', self.key)}",
            ISSUE_TEMPLATE,
            query=query({"fields": ",".join(ids)}),
        )
        fields = data.get("fields")
        if not isinstance(fields, dict):
            raise TicketctlError("provider_changed", "the ticket answer has no fields")
        key = data.get("key")
        canonical = key if isinstance(key, str) and audit.TICKET_KEY.fullmatch(key) else self.key
        return _Ticket(canonical, fields)

    # -- the plan --------------------------------------------------------------------------
    def _plan(self, ctx: Context, ticket: _Ticket, edits: list[FieldEdit]) -> Planned:
        fields, key = ticket.fields, ticket.key
        target = self._target(ctx)
        intent: dict[str, Any] = {
            "title": None if self.title is None else _hex(self.title),
            "fields": {e.name: self._value_hash(e) for e in edits},
            "labels": {"add": sorted(self.add_labels), "remove": sorted(self.remove_labels)},
            "assign": self.assign,
        }
        facts: dict[str, Any] = {}
        changes: list[dict[str, Any]] = []
        shown: list[ShownText] = []
        bodies: list[dict[str, Any]] = []
        warnings: list[str] = []
        names: list[str] = []  # the fields the set_fields step writes
        body: dict[str, Any] = {}
        labels_update: list[dict[str, str]] = []
        written: list[FieldEdit] = []

        if self.title is not None:
            current = fields.get("summary")
            facts["title"] = current if isinstance(current, str) else None
            if current != self.title:
                body["summary"] = self.title
                names.append("title")
                changes.append({"name": "title", "from": facts["title"], "to": self.title})
                shown.append(ShownText("title", self.title, "line"))

        if edits:
            facts["fields"] = {e.name: fact(e, fields.get(e.field.id)) for e in edits}
        for edit in edits:
            raw = fields.get(edit.field.id)
            if same_value(edit, raw):
                continue
            written.append(edit)
            names.append(edit.name)
            to = display_to(edit)
            changes.append({"name": edit.name, "from": display_from(edit, raw), "to": to})
            rich = edit.value.rich
            if rich is not None and rich.doc is not None:
                info = body_info(rich)
                bodies.append(info)
                shown.append(ShownText(edit.name, str(to or ""), "body", info["sha256"]))
                if text_chars(rich.doc) > NEAR_LIMIT * ctx.body_chars:
                    warnings.append("body_near_limit")
            elif isinstance(to, str | int | float | bool):
                shown.append(ShownText(edit.name, str(to), "line"))

        body.update(fields_body(written))

        if self.add_labels or self.remove_labels:
            current_labels = fields.get("labels")
            if not isinstance(current_labels, list) or not all(
                isinstance(label, str) for label in current_labels
            ):
                raise TicketctlError("provider_changed", "the ticket answer has no usable labels")
            have = set(current_labels)
            facts["labels"] = {
                "add": {label: label in have for label in sorted(self.add_labels)},
                "remove": {label: label in have for label in sorted(self.remove_labels)},
            }
            adds = [label for label in self.add_labels if label not in have]
            removes = [label for label in self.remove_labels if label in have]
            if adds or removes:
                labels_update = [
                    *({"add": label} for label in adds),
                    *({"remove": label} for label in removes),
                ]
                names.append("labels")
                after = (have | set(adds)) - set(removes)
                changes.append(
                    {"name": "labels", "from": _visible(list(have)), "to": _visible(list(after))}
                )

        steps: list[Step] = []
        if names:
            steps.append(self._set_fields_step(key, names, body, labels_update))

        assignee = _account(fields.get("assignee"))
        assign_label: str | None = None  # the assign step's target as a hook sees it
        if self.assign is not None:
            facts["assignee"] = assignee
            if assignee != target:
                assign_label = self.assign
                steps.append(self._assign_step(key, target, self.assign))
                changes.append({"name": "assignee", "from": assignee, "to": target})
                if target is not None:
                    shown.append(ShownText("assignee", target, "line"))

        hook: dict[str, Any] = {}
        if steps:
            hook = self._hook_facts(ctx, fields, assign_label)
            if hook["current_status"]["category"] == "done":
                warnings.append("closed_ticket")
        return Planned(
            steps=steps,
            intent=intent,
            facts=facts,
            changes=changes,
            warnings=warnings,
            key=key,
            bodies=bodies,
            shown=shown,
            hook=hook,
        )

    def _target(self, ctx: Context) -> str | None:
        """The account id the ``--assign`` value means (None: unassigned or no ``--assign``)."""
        if self.assign == "me":
            return ctx.me.account_id
        if self.assign in (None, "none"):
            return None
        return self.assign

    @staticmethod
    def _value_hash(edit: FieldEdit) -> str:
        rich = edit.value.rich
        if rich is not None and rich.sha256 is not None:
            return rich.sha256
        return hashlib.sha256(canonical_bytes(edit.value.payload)).hexdigest()

    @staticmethod
    def _hook_facts(ctx: Context, fields: dict[str, Any], assign: str | None) -> dict[str, Any]:
        """What the hook payloads need and the hash does not (names and ids, never ticket text)."""
        status, issuetype = fields.get("status"), fields.get("issuetype")
        category = None
        if isinstance(status, dict) and isinstance(status.get("statusCategory"), dict):
            category = status["statusCategory"].get("key")
        status_name, type_name = _name(status), _name(issuetype)
        if status_name is None or category not in STATUS_CATEGORIES or type_name is None:
            raise TicketctlError("provider_changed", "the ticket answer has no usable status")
        assignee = _account(fields.get("assignee"))
        return {
            "current_status": {"name": status_name, "category": category},
            "assign": assign,
            "ticket": {
                "type": type_name,
                "subtask": isinstance(issuetype, dict) and issuetype.get("subtask") is True,
                "status": status_name,
                "priority": _name(fields.get("priority")),
                "assignee_account_id": None if assignee == "?" else assignee,
                "assignee_is_me": assignee == ctx.me.account_id,
            },
        }

    # -- the steps -------------------------------------------------------------------------
    @staticmethod
    def _set_fields_step(
        key: str, names: list[str], fields: dict[str, Any], labels: list[dict[str, str]]
    ) -> Step:
        request: dict[str, Any] = {}
        if fields:
            request["fields"] = fields
        if labels:
            request["update"] = {"labels": labels}

        def send(gw: WriteGateway) -> StepResult:
            # No query: notifyUsers is never sent.
            raise_for_write(
                gw.request(
                    "PUT",
                    f"{ISSUE}/{segment('ticket_key', key)}",
                    template=ISSUE_TEMPLATE,
                    body=request,
                ),
                object_path=True,
            )
            return StepResult()

        return Step("set_fields", send, {"fields": list(dict.fromkeys(names))})

    @staticmethod
    def _assign_step(key: str, target: str | None, label: str) -> Step:

        def send(gw: WriteGateway) -> StepResult:
            raise_for_write(
                gw.request(
                    "PUT",
                    f"{ISSUE}/{segment('ticket_key', key)}/assignee",
                    template=ASSIGNEE_TEMPLATE,
                    body={"accountId": target},  # null unassigns; never the automatic value
                ),
                object_path=True,
            )
            return StepResult()

        return Step("assign", send, {"account": label})


__all__ = ["UpdateAction", "assign_arg", "check_offline", "update"]
