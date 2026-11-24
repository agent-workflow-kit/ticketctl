"""Handler and write action of ``resolution-backfill KEY`` (docs/interface.md, design D-45).

One write through the write flow engine (``writeflow.py``): set the resolution of one ticket that is
in a done status category and has none. It is one provider call, ``PUT /issue/{key}`` with
``fields.resolution``; it is never a transition and never changes the status.

* **reduce**: the ticket is read (``status``, ``resolution``). A ticket that has a resolution,
  whatever it is, is ``unchanged`` before every other check, so a re-run after a landed write is
  safe and ticketctl never overwrites a resolution. An absent ``resolution`` key or one that cannot
  be read is ``provider_changed``: only an explicit null means "none".
* **check** (only while a step remains): the status category must be ``done`` (exit 20
  ``precondition_failed``), a resolution name must be given (``--resolution`` or the site's
  ``transitions.default_resolution``), the ticket's ``editmeta`` must list ``resolution`` with the
  ``set`` operation (``not_editable`` otherwise: no other route is tried) and the name must be one
  of its allowed values, else one of the provider's resolution list.
* The plan hash covers the resolution name and the lower-cased status name.

Unverified against a real site (W24): that the edit screen of a done ticket lists ``resolution``
with ``set``, the shape of its ``allowedValues``, that ``PUT fields.resolution`` answers 204 and
stores the value on a done ticket, and what happens to ``resolutiondate``.
"""

from __future__ import annotations

import argparse
from typing import Any

from . import selection, setspec, writeflow
from .errors import TicketctlError
from .fieldload import load_resolutions
from .fieldmap import MAX_CANDIDATES
from .fieldwrite import read_object
from .flags import Globals
from .plan import Planned, ShownText, Step
from .runtime import load_runtime
from .sanitize import CAPS, GRAMMARS, query, sanitize_field, sanitize_line, segment
from .writeflow import Context, PrepareContext, Seams, StepResult, WriteGateway, raise_for_write

ISSUE = "/rest/api/3/issue"
ISSUE_TEMPLATE = "/rest/api/3/issue/{key}"
EDITMETA_TEMPLATE = "/rest/api/3/issue/{key}/editmeta"
READ_FIELDS = "status,resolution,issuetype,priority,assignee"
CATEGORIES = ("new", "indeterminate", "done")
NAME_CAP = CAPS["name"]


def make_seams() -> Seams:
    """The seams of a real run (tests replace this function to fix the attempt id)."""
    return Seams()


def resolution_arg(value: str) -> str:
    """``--resolution``: one line of visible text, at most 256 characters."""
    if not value.strip() or len(value) > NAME_CAP or sanitize_line(value) != value:
        raise argparse.ArgumentTypeError("needs one line of visible text of at most 256 characters")
    return value


def _obj(value: object) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _name(value: object) -> str | None:
    return value if isinstance(value, str) and value else None


def _unreadable(what: str) -> TicketctlError:
    return TicketctlError(
        "provider_changed",
        f"the provider answered {what} that cannot be read; nothing was changed",
    )


def _same(a: str, b: str) -> bool:
    return a.casefold() == b.casefold()


class ResolutionBackfillAction:
    """``resolution-backfill KEY [--resolution NAME]`` as a ``WriteAction``."""

    name = "resolution_backfill"
    dedupe_key: str | None = None
    yes_needs_dedupe_key = False

    def __init__(self, key: str, *, resolution: str | None = None) -> None:
        self.key = key
        self.resolution = resolution

    # -- step 1 ------------------------------------------------------------------------------
    def prepare(self, pc: PrepareContext) -> None:
        if self.resolution is not None:
            pc.check_secrets(self.resolution)

    # -- step 3 ------------------------------------------------------------------------------
    def reduce(self, ctx: Context, *, fresh: bool) -> Planned:
        fields = self._read_ticket(ctx)
        status = _obj(fields.get("status"))
        status_name = _name(status.get("name"))
        if status_name is None:
            raise _unreadable("no status")
        shown_status = sanitize_field(status_name, cap=NAME_CAP)[0]
        raw_category = _obj(status.get("statusCategory")).get("key")
        # Jira also has the category key "undefined"; it is not a done ticket.
        category = raw_category if raw_category in CATEGORIES else "indeterminate"
        current = {"name": shown_status, "category": category}

        if "resolution" not in fields:
            raise _unreadable("no resolution field")
        existing = fields["resolution"]
        has_resolution = False
        if existing is not None:
            if not _name(_obj(existing).get("name")):
                raise _unreadable("a resolution")
            has_resolution = True

        steps: list[Step] = []
        changes: list[dict[str, Any]] = []
        shown: list[ShownText] = []
        wanted: str | None = None
        if not has_resolution:
            wanted = self._choose(ctx, fields, current)
            steps.append(self._step(wanted))
            changes.append({"name": "resolution", "from": None, "to": wanted})
            shown.append(ShownText("resolution", wanted, "line"))
        return Planned(
            steps=steps,
            intent={"resolution": wanted},
            facts={"status": status_name.casefold()},
            changes=changes,
            key=self.key,
            shown=shown,
            hook=self._hook_facts(ctx, fields, current, wanted),
        )

    def check(self, ctx: Context, planned: Planned) -> None:
        return None  # every refusal ran in reduce, where the step is known to remain

    # -- the checks that matter only while a step remains ------------------------------------
    def _choose(self, ctx: Context, fields: dict[str, Any], current: dict[str, str]) -> str:
        if current["category"] != "done":
            raise TicketctlError(
                "precondition_failed",
                "the ticket is not in a done status category",
                details={"current_status": current["name"]},
            )
        wanted = self.resolution
        if wanted is None:
            wanted = ctx.rt.config.sites[ctx.site].transitions.default_resolution
        if wanted is None:
            raise TicketctlError(
                "invalid_request",
                "the command needs --resolution or a default_resolution",
                reason="needs_resolution",
            )
        if len(wanted) > NAME_CAP or sanitize_line(wanted) != wanted or not wanted.strip():
            raise TicketctlError(
                "invalid_request", "the resolution is not valid", reason="invalid_value"
            )
        meta = setspec.parse_meta(
            read_object(
                ctx, f"{ISSUE}/{segment('ticket_key', self.key)}/editmeta", EDITMETA_TEMPLATE
            )
        )
        listed = next((m for m in meta if m.field.id == "resolution"), None)
        # An entry that does not say its operations is not trusted: only an explicit "set" counts.
        if listed is None or listed.operations is None or "set" not in listed.operations:
            raise TicketctlError(
                "invalid_request",
                "the resolution cannot be set on this ticket (its edit screen does not list it)",
                reason="not_editable",
                details={"field": "Resolution"},
            )
        allowed = listed.allowed
        if allowed is None:
            allowed = load_resolutions(ctx.rt, ctx.site, ctx.gw).names
        hits = sorted({a for a in allowed if _same(a, wanted)})
        if len(hits) == 1:
            return hits[0]
        raise TicketctlError(
            "invalid_request",
            "the resolution matches no allowed value"
            if not hits
            else "several allowed resolutions match",
            reason="invalid_value",
            details={"candidates": sorted(set(hits or allowed))[:MAX_CANDIDATES]},
        )

    # -- helpers -----------------------------------------------------------------------------
    def _read_ticket(self, ctx: Context) -> dict[str, Any]:
        data = read_object(
            ctx,
            f"{ISSUE}/{segment('ticket_key', self.key)}",
            ISSUE_TEMPLATE,
            query=query({"fields": READ_FIELDS}),
        )
        fields = data.get("fields")
        if not isinstance(fields, dict):
            raise TicketctlError("provider_changed", "the provider answered no ticket fields")
        return fields

    def _step(self, resolution: str) -> Step:
        key = self.key

        def send(gw: WriteGateway) -> StepResult:
            # No query: notifyUsers is never sent.
            raise_for_write(
                gw.request(
                    "PUT",
                    f"{ISSUE}/{segment('ticket_key', key)}",
                    template=ISSUE_TEMPLATE,
                    body={"fields": {"resolution": {"name": resolution}}},
                ),
                object_path=True,
            )
            return StepResult()

        return Step("set_resolution", send, {"resolution": resolution})

    def _hook_facts(
        self, ctx: Context, fields: dict[str, Any], current: dict[str, str], wanted: str | None
    ) -> dict[str, Any]:
        itype = _obj(fields.get("issuetype"))
        assignee = _obj(fields.get("assignee")).get("accountId")
        facts: dict[str, Any] = {
            "ticket": {
                "type": _name(itype.get("name")),
                "subtask": itype.get("subtask") is True,
                "status": current["name"],
                "priority": _name(_obj(fields.get("priority")).get("name")),
                "assignee_account_id": assignee
                if GRAMMARS["account_id"].matches(assignee)
                else None,
                "assignee_is_me": assignee == ctx.me.account_id,
            },
            "current_status": current,
            "resolution": wanted,
        }
        project = self.key.rpartition("-")[0]
        if GRAMMARS["project_key"].matches(project):
            facts["project"] = project
        return facts


def resolution_backfill(g: Globals, a: argparse.Namespace) -> dict[str, Any]:
    rt = load_runtime(g)
    picked = selection.select_site(rt.config, site_flag=g.site, refs=[a.key])
    assert picked.key is not None
    action = ResolutionBackfillAction(picked.key, resolution=a.resolution)
    return writeflow.execute(rt, picked.site, action, make_seams())
