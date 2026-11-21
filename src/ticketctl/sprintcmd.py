"""Handlers and write action of ``sprint add KEY`` and ``sprint remove KEY`` (docs/interface.md).

Each command is one provider call through the write flow engine (``writeflow.py``); a command
takes exactly one key, so there is one step, one plan, one audit pair and one hook pair, and no
chunking (the agile endpoints take up to 50 issues per call; multi-key writes are a 1.x candidate).
The action names are ``sprint_add`` and ``sprint_remove`` (the hook family is ``sprint``).

* **Membership** is the ticket's ``future`` and ``active`` sprints, read from the ticket's sprint
  field (mapped or discovered, ``sprint``). Closed sprints are history and ignored. A sprint value
  that cannot be read (not a list, an entry without a numeric id, a name or a known state) is
  ``provider_changed`` and never counts as "no sprint": a write must not act on a guess.
* **add** sends ``POST /rest/agile/1.0/sprint/{id}/issue`` ``{"issues": [KEY]}``. The target is
  an id, ``open`` (the board's one ``active`` sprint; several exit 21 ``ambiguous_sprint``, none
  exit 10 ``not_found`` with ``details.option: sprint``) or ``next`` (the ``future`` sprint with
  the earliest non-null ``start``, else the lowest id; a listing the cap cut off, or one that
  dropped a sprint it could not read, is never guessed from: ``lookup_incomplete`` and
  ``provider_changed``). The board
  (``--board``, ``sites.<site>.board``, else the scrum board of the ticket's project) is resolved by
  ``sprintscmd.resolve_board`` and only for ``open`` and ``next``: an explicit id names the sprint,
  and a ``--board`` given with it is checked for its grammar only. Already a member is
  ``unchanged``; a member of another open sprint warns ``sprint_moved`` (the provider moves it); a
  ``closed`` target exits 20 ``sprint_closed`` while a step remains (also for ``--dry-run``).
* **remove** sends ``POST /rest/agile/1.0/backlog/issue`` ``{"issues": [KEY]}``: the ticket goes to
  the backlog and so leaves every open sprint (verified for one active sprint, V-17). It needs no
  board (``--board`` is accepted and ignored). The open and future sprints dropped are listed in the
  plan's ``changes`` (``other_sprints_dropped`` when more than one); none is ``unchanged``.
* **Plan.** ``changes`` is one ``sprints`` entry whose ``from`` and ``to`` are lists of
  ``"<id>: <name>"`` strings (the names are untrusted and sanitized, field ids hidden). The hash
  intent is the resolved sprint id (add) or the backlog move (remove); the facts are the ticket's
  current open and future sprint ids, so renaming a sprint or editing the ticket does not stale a
  plan, and a membership change or another ``open`` sprint does. ``Planned.hook`` carries
  ``sprints`` (the target for add, the dropped sprints for remove), ``ticket``, ``current_status``
  and ``project``.
* **Re-runs.** ``reduce`` reads the live ticket every time, so a re-run after a write that landed
  (even an ``outcome_unknown`` one) is ``unchanged`` and a re-run after a refusal sends the one
  remaining step again with the same hash.

Unverified against a real site (the code takes the conservative reading; reported on #3 and #38):
that ``POST sprint/{id}/issue`` moves an issue out of the sprint it is in rather than adding it to
a second one (so ``sprint_moved`` is a warning, not a refusal); that the issue's sprint field is an
array of objects with ``id``, ``name`` and ``state`` (also with string ids and upper-case states);
that a 400 on either call is the provider's refusal (a sub-task, a ticket outside the board, a
closed sprint) and a 404 on the sprint call means the sprint or ticket is missing or hidden; that
the backlog call moves a ticket out of several open sprints at once; and that ``GET
board/{id}/sprint?state=active|future`` lists them. Moving a ticket out of a closed sprint, and
what the backlog call does to a closed-sprint membership, are not claimed (unverified).
"""

from __future__ import annotations

import argparse
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, cast

from . import fieldload, selection, writeflow
from .errors import TicketctlError
from .flags import Globals
from .gateway import Gateway
from .plan import Planned, Step
from .readutil import hide_field_ids, object_error
from .runtime import Runtime, load_runtime
from .sanitize import CAPS, GRAMMARS, query, sanitize_field, segment
from .sprintscmd import (
    MAX_LIMIT,
    STATES,
    board_spec,
    read_sprints,
    resolve_board,
    sprint_object,
)
from .writeflow import (
    Context,
    PrepareContext,
    Seams,
    StepResult,
    WriteGateway,
    raise_for_write,
)

ISSUE_TEMPLATE = "/rest/api/3/issue/{key}"
SPRINT_TEMPLATE = "/rest/agile/1.0/sprint/{id}"
ADD_TEMPLATE = "/rest/agile/1.0/sprint/{id}/issue"
BACKLOG_PATH = "/rest/agile/1.0/backlog/issue"
OPEN_STATES = ("future", "active")
CATEGORIES = ("new", "indeterminate", "done")
CANDIDATES = 20


def make_seams() -> Seams:
    """The seams of a real run (tests replace this function to fix the attempt id)."""
    return Seams()


def _name(value: object) -> str | None:
    return value if isinstance(value, str) and value else None


def _obj(value: object) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


@dataclass(frozen=True)
class Member:
    """One open or closed sprint of the ticket (or the target): what a plan and a hook need."""

    id: int
    name: str
    state: str
    start: str | None = None

    @property
    def shown(self) -> str:
        return f"{self.id}: {self.name}"

    @property
    def hook(self) -> dict[str, Any]:
        return {"id": self.id, "state": self.state}


def _display(name: str) -> str:
    return sanitize_field(hide_field_ids(name), cap=CAPS["name"])[0]


def _unreadable() -> TicketctlError:
    return TicketctlError(
        "provider_changed",
        "the ticket's sprint field holds a value that cannot be read; nothing was changed",
    )


def _member(raw: object) -> Member:
    """One entry of the ticket's sprint field (``provider_changed`` when it cannot be read)."""
    if not isinstance(raw, dict):
        raise _unreadable()
    sid = raw.get("id")
    if isinstance(sid, str) and sid.isascii() and GRAMMARS["numeric_id"].matches(sid):
        sid = int(sid)
    state, name = raw.get("state"), raw.get("name")
    if (
        isinstance(sid, bool)
        or not isinstance(sid, int)
        or not 1 <= sid < 10**18
        or not isinstance(state, str)
        or state.lower() not in STATES
        or not isinstance(name, str)
    ):
        raise _unreadable()
    return Member(sid, _display(name), state.lower())


def members_of(value: object) -> list[Member]:
    """The ticket's future and active sprints, by ascending id (closed sprints are history)."""
    if value is None:
        return []
    if not isinstance(value, list):
        raise _unreadable()
    found = {m.id: m for m in map(_member, value) if m.state in OPEN_STATES}
    return [found[i] for i in sorted(found)]


class SprintAction:
    """``sprint add KEY`` and ``sprint remove KEY`` as a ``WriteAction`` (one class, two names)."""

    dedupe_key: str | None = None
    yes_needs_dedupe_key = False

    def __init__(
        self, key: str, *, sprint: str | None = None, board: str | None = None, remove: bool = False
    ) -> None:
        self.key = key
        self.selector = sprint
        self.board = board
        self.remove = remove
        self.name = "sprint_remove" if remove else "sprint_add"
        self.target: Member | None = None
        self._field_id: str | None = None

    # -- step 1 ------------------------------------------------------------------------------
    def prepare(self, pc: PrepareContext) -> None:
        if self.board is not None:
            board_spec(self.board)  # the grammar of --board, whether or not a board is needed
        if not self.remove and self.selector is None:
            raise TicketctlError("usage", "--sprint is required", reason="missing_option")

    # -- step 3 ------------------------------------------------------------------------------
    def reduce(self, ctx: Context, *, fresh: bool) -> Planned:
        fields = self._read_ticket(ctx)
        members = members_of(fields.get(self._sprint_field(ctx)))
        project = _obj(fields.get("project")).get("key")
        project = project if GRAMMARS["project_key"].matches(project) else None
        if self.remove:
            return self._reduce_remove(ctx, fields, members)
        target = self._resolve_target(ctx, project)
        self.target = target
        return self._reduce_add(ctx, fields, members, target)

    def check(self, ctx: Context, planned: Planned) -> None:
        if self.target is not None and self.target.state == "closed":
            raise TicketctlError(
                "sprint_closed",
                f"sprint {self.target.id} is closed; a ticket cannot be added to it",
            )

    # -- the two plans -----------------------------------------------------------------------
    def _reduce_add(
        self, ctx: Context, fields: dict[str, Any], members: list[Member], target: Member
    ) -> Planned:
        ids = [m.id for m in members]
        steps: list[Step] = []
        changes: list[dict[str, Any]] = []
        warnings: list[str] = []
        if target.id not in ids:
            steps.append(Step("sprint_add", self._sender(target.id), {"sprint": str(target.id)}))
            changes.append(
                {"name": "sprints", "from": [m.shown for m in members], "to": [target.shown]}
            )
            if members:
                warnings.append("sprint_moved")
        return Planned(
            steps=steps,
            intent={"sprint": target.id},
            facts={"sprint_ids": ids},
            changes=changes,
            warnings=warnings,
            key=self.key,
            hook=self._hook_facts(ctx, fields, [target]),
        )

    def _reduce_remove(
        self, ctx: Context, fields: dict[str, Any], members: list[Member]
    ) -> Planned:
        steps: list[Step] = []
        changes: list[dict[str, Any]] = []
        warnings: list[str] = []
        if members:
            steps.append(Step("sprint_remove", self._sender(None)))
            changes.append({"name": "sprints", "from": [m.shown for m in members], "to": []})
            if len(members) > 1:
                warnings.append("other_sprints_dropped")
        return Planned(
            steps=steps,
            intent={"to": "backlog"},
            facts={"sprint_ids": [m.id for m in members]},
            changes=changes,
            warnings=warnings,
            key=self.key,
            hook=self._hook_facts(ctx, fields, members),
        )

    # -- reads -------------------------------------------------------------------------------
    def _sprint_field(self, ctx: Context) -> str:
        if self._field_id is None:
            site_fields = fieldload.load_site_fields(
                ctx.rt, ctx.site, _plain(ctx.gw), needs=("sprint",)
            )
            self._field_id = site_fields.resolve("sprint").need(ctx.site).id
        return self._field_id

    def _read_ticket(self, ctx: Context) -> dict[str, Any]:
        wanted = f"status,issuetype,project,priority,assignee,{self._sprint_field(ctx)}"
        response = ctx.gw.request(
            "GET",
            f"/rest/api/3/issue/{segment('ticket_key', self.key)}",
            template=ISSUE_TEMPLATE,
            query=query({"fields": wanted}),
        )
        if not 200 <= response.status < 300:
            raise object_error(response.status, "ticket")
        data = response.data
        fields = data.get("fields") if isinstance(data, dict) else None
        if not isinstance(fields, dict):
            raise TicketctlError("provider_changed", "the provider answered no ticket fields")
        return fields

    def _resolve_target(self, ctx: Context, project: str | None) -> Member:
        selector = self.selector
        assert selector is not None
        gw = _plain(ctx.gw)
        if selector not in ("open", "next"):
            return self._sprint_by_id(ctx, selector)
        site = ctx.rt.config.sites[ctx.site]
        board = resolve_board(gw, site, board=self.board, project=project)
        marks: set[str] = set()
        if selector == "open":
            found = read_sprints(gw, board, "active", CANDIDATES, marks)
            self._whole(marks)
            if len(found) > 1:
                raise TicketctlError(
                    "ambiguous_sprint",
                    "several sprints are active; name one with --sprint <id>",
                    details={"candidates": [f"{s['id']} ({s['name']})" for s in found]},
                )
            if not found:
                raise _no_sprint("the board has no active sprint")
            return _from_object(found[0])
        found = read_sprints(gw, board, "future", MAX_LIMIT, marks)
        self._whole(marks)
        if len(found) >= MAX_LIMIT:
            raise TicketctlError(
                "lookup_incomplete",
                "the board has too many future sprints to pick the next one; "
                "name the sprint with --sprint <id>",
            )
        if not found:
            raise _no_sprint("the board has no future sprint")
        dated = [s for s in found if s["start"] is not None]
        pool = dated or found
        best = min(pool, key=lambda s: (s["start"] or "", s["id"]))
        return _from_object(best)

    @staticmethod
    def _whole(marks: set[str]) -> None:
        """A sprint the listing dropped (or with an unreadable date) could be the right one."""
        if marks - {"sprints.truncated"}:
            raise TicketctlError(
                "provider_changed",
                "the board's sprint list holds a sprint that cannot be read; "
                "name the sprint with --sprint <id>",
            )

    def _sprint_by_id(self, ctx: Context, selector: str) -> Member:
        response = ctx.gw.request(
            "GET",
            f"/rest/agile/1.0/sprint/{segment('numeric_id', selector)}",
            template=SPRINT_TEMPLATE,
        )
        if not 200 <= response.status < 300:
            raise object_error(response.status, "sprint", option="sprint")
        marks: set[str] = set()
        found = sprint_object(response.data, marks)
        if found is None or found["id"] != int(selector):
            raise TicketctlError("provider_changed", "the provider answered no usable sprint")
        return _from_object(found)

    # -- send --------------------------------------------------------------------------------
    def _sender(self, sprint_id: int | None) -> Callable[[WriteGateway], StepResult]:
        key = self.key

        def send(gw: WriteGateway) -> StepResult:
            if sprint_id is None:
                url, template, what = BACKLOG_PATH, BACKLOG_PATH, "ticket"
            else:
                url = f"/rest/agile/1.0/sprint/{segment('numeric_id', str(sprint_id))}/issue"
                template, what = ADD_TEMPLATE, "sprint or ticket"
            res = gw.request("POST", url, template=template, body={"issues": [key]})
            if res.status == 404:
                raise TicketctlError(
                    "not_found", f"the {what} is missing or you have no permission to see it"
                )
            raise_for_write(res, object_path=False)
            return StepResult()

        return send

    # -- hook facts --------------------------------------------------------------------------
    def _hook_facts(
        self, ctx: Context, fields: dict[str, Any], sprints: list[Member]
    ) -> dict[str, Any]:
        status = _obj(fields.get("status"))
        raw_cat = _obj(status.get("statusCategory")).get("key")
        category = raw_cat if raw_cat in CATEGORIES else None
        itype = _obj(fields.get("issuetype"))
        assignee = _obj(fields.get("assignee")).get("accountId")
        project = _obj(fields.get("project")).get("key")
        facts: dict[str, Any] = {
            "ticket": {
                "type": _name(itype.get("name")),
                "subtask": itype.get("subtask") is True,
                "status": _name(status.get("name")),
                "priority": _name(_obj(fields.get("priority")).get("name")),
                "assignee_account_id": assignee
                if GRAMMARS["account_id"].matches(assignee)
                else None,
                "assignee_is_me": assignee == ctx.me.account_id,
            },
            "current_status": {"name": _name(status.get("name")), "category": category},
            "sprints": [m.hook for m in sprints],
        }
        if GRAMMARS["project_key"].matches(project):
            facts["project"] = project
        return facts


def _plain(gw: WriteGateway) -> Gateway:
    """The guarded gateway as the read helpers want it (they only send GET requests)."""
    return cast("Gateway", gw)


def _no_sprint(why: str) -> TicketctlError:
    return TicketctlError("not_found", why, details={"option": "sprint"})


def _from_object(obj: dict[str, Any]) -> Member:
    return Member(obj["id"], obj["name"], obj["state"], obj["start"])


def _run(g: Globals, a: argparse.Namespace, *, remove: bool) -> dict[str, Any]:
    rt: Runtime = load_runtime(g)
    chosen = selection.select_site(rt.config, site_flag=g.site, refs=[a.key])
    assert chosen.key is not None
    action = SprintAction(
        chosen.key,
        sprint=None if remove else a.sprint,
        board=a.board,
        remove=remove,
    )
    return writeflow.execute(rt, chosen.site, action, make_seams())


def sprint_add(g: Globals, a: argparse.Namespace) -> dict[str, Any]:
    return _run(g, a, remove=False)


def sprint_remove(g: Globals, a: argparse.Namespace) -> dict[str, Any]:
    return _run(g, a, remove=True)
