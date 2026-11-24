"""Handlers and write action of ``link add KEY`` and ``link remove KEY`` (docs/interface.md).

Each command is one write through the write flow engine (``writeflow.py``) and takes exactly one
ticket key and one other key (``--other``), so there is one plan, one audit pair and one hook pair.
The action names are ``link_add`` and ``link_remove`` (the hook family is ``link``).

* **A link is named by its content**: the link type, the direction and the other ticket. The
  direction is read like the ticket object's ``links[].direction``, relative to ``KEY``:
  ``inward`` means ``KEY`` is the target of the type's inward phrase ("is blocked by"),
  ``outward`` that it is the source ("blocks"). So ``link add KEY --type Blocks --direction
  outward --other X`` makes ``KEY blocks X``. The type is matched against ``GET issueLinkType``
  (an exact name, else one case-insensitive match; anything else is exit 22 ``invalid_value``
  with the known names in ``details.candidates``).
* **Symmetric types** (inward and outward phrase equal, such as "relates to") have no direction:
  add is ``unchanged`` if the link exists either way and remove deletes it whichever way stored.
* **Raw type names**: the link match and the request use the provider's own type name (the option
  is matched against it or its sanitised form, as the candidate list shows it); only plan, hook and
  candidate text is sanitised.
* **add** is ``unchanged`` when ``KEY`` already has that link (type, direction and other key), so
  a re-run (also after ``outcome_unknown``) is safe and no dedupe key exists. It sends
  ``POST issueLink`` once. Linking a ticket to itself is refused (exit 22 ``invalid_value``)
  before anything is read.
* **remove** is ``unchanged`` when ``KEY`` has no such link, else it sends ``DELETE issueLink/{id}``
  for each matching link (the provider normally holds at most one). It removes any link of that
  content, not only one ticketctl made: a link carries no ownership marker, so the policy lives in
  the ``pre-link`` hook and the confirmation, not in the tool.
* **Plan.** ``changes`` has one ``links`` entry holding only the links this command adds or
  removes, as readable strings (``"<type>: KEY <phrase> OTHER"``; provider configuration text,
  never ticket text; no ticket summary is shown or sent to a hook). The hash intent is the type
  (shown name, provider id and a hash of the raw name), the direction and the other key; the facts
  are the ids of the matching links, so a link that appeared or disappeared meanwhile is
  ``stale_ticket``, while other links of the ticket and edits to it are not.
* Both links of the other ticket are the provider's concern: one link is one object with one id
  that shows on both tickets.

Unverified against a real site (reported for the live checklist, W22): that ``inwardIssue`` of
``POST issueLink`` is the ticket that gets the outward phrase (the source: ``inwardIssue`` blocks
``outwardIssue``), which is the mapping used here; that the request answers 201 with an empty
body; that ``GET issue?fields=issuelinks`` lists each link with its ``id`` on both tickets; that
``DELETE issueLink/{id}`` answers 204; and what the provider does with a duplicate link (this tool
never sends one) and with a link between tickets of different projects without permission.
"""

from __future__ import annotations

import argparse
import hashlib
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from . import selection, writeflow
from .errors import TicketctlError
from .flags import Globals
from .plan import Planned, Step
from .readutil import object_error
from .runtime import load_runtime
from .sanitize import CAPS, GRAMMARS, normalize_ticket_key, query, sanitize_field, segment
from .writeflow import (
    Context,
    PrepareContext,
    Seams,
    StepResult,
    WriteGateway,
    raise_for_write,
)

ISSUE_TEMPLATE = "/rest/api/3/issue/{key}"
TYPES_PATH = "/rest/api/3/issueLinkType"
LINK_PATH = "/rest/api/3/issueLink"
LINK_TEMPLATE = "/rest/api/3/issueLink/{id}"
DIRECTIONS = ("inward", "outward")
CATEGORIES = ("new", "indeterminate", "done")
MAX_CANDIDATES = 50
MAX_TYPES = 500
MAX_LINKS = 1000


def make_seams() -> Seams:
    """The seams of a real run (tests replace this function to fix the attempt id)."""
    return Seams()


def _name(value: object) -> str | None:
    return value if isinstance(value, str) and value else None


def _obj(value: object) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _unreadable(what: str) -> TicketctlError:
    return TicketctlError(
        "provider_changed",
        f"the provider answered {what} that cannot be read; nothing was changed",
    )


def direction_arg(value: str) -> str:
    """``--direction``: ``inward`` or ``outward`` (exactly, so a typo is never a default)."""
    if value not in DIRECTIONS:
        raise argparse.ArgumentTypeError("needs inward or outward")
    return value


@dataclass(frozen=True)
class LinkType:
    """One link type as the provider names it: ``name`` is raw (matched and sent as is); the
    ``shown_*`` properties are the sanitised forms for plans, hooks and error candidates."""

    name: str
    inward: str
    outward: str
    id: str | None = None  # the provider's type id when it is readable; part of the plan intent

    @property
    def shown_name(self) -> str:
        return _text(self.name) or ""

    @property
    def symmetric(self) -> bool:
        """Both phrases are the same (e.g. "relates to"): the direction carries no meaning."""
        return self.inward == self.outward

    def phrase(self, direction: str) -> str:
        return _text(self.inward if direction == "inward" else self.outward) or ""


@dataclass(frozen=True)
class Existing:
    """One link of the ticket: the provider's id, the type name, the direction, the other key."""

    id: str
    type: str
    direction: str
    other: str


def _text(value: object) -> str | None:
    """A provider phrase made safe to show; None when nothing readable is left."""
    if not isinstance(value, str):
        return None
    shown = sanitize_field(value, cap=CAPS["name"])[0]
    return shown if shown.strip() else None


def read_types(gw: WriteGateway) -> list[LinkType]:
    response = gw.request("GET", TYPES_PATH, template=TYPES_PATH)
    if not 200 <= response.status < 300:
        raise object_error(response.status, "link type")
    raw = _obj(response.data).get("issueLinkTypes")
    if not isinstance(raw, list) or len(raw) > MAX_TYPES:
        raise _unreadable("no list of link types")
    out: list[LinkType] = []
    for entry in raw:
        item = _obj(entry)
        name, inward, outward = item.get("name"), item.get("inward"), item.get("outward")
        # a type is readable only when each part shows something; the raw text is what is kept
        if not (
            isinstance(name, str)
            and isinstance(inward, str)
            and isinstance(outward, str)
            and all(_text(v) is not None for v in (name, inward, outward))
        ):
            raise _unreadable("a link type")
        tid = item.get("id")
        if isinstance(tid, int) and not isinstance(tid, bool):
            tid = str(tid)
        type_id = tid if isinstance(tid, str) and GRAMMARS["provider_id"].matches(tid) else None
        out.append(LinkType(name, inward, outward, type_id))
    return out


def pick_type(types: list[LinkType], wanted: str) -> LinkType:
    """The type named ``wanted``: an exact name, else the one case-insensitive match."""
    # a name is matched by its raw form first and by the sanitised form the candidate list shows
    # only when no raw name matches (an option cannot carry the invisible characters a raw name may
    # hold, but a type whose raw name equals another's sanitised form must stay selectable); the raw
    # name is used after
    folded = wanted.casefold()
    found = (
        [t for t in types if t.name == wanted]
        or [t for t in types if t.shown_name == wanted]
        or [t for t in types if t.name.casefold() == folded]
        or [t for t in types if t.shown_name.casefold() == folded]
    )
    if len(found) == 1:
        return found[0]
    candidates = sorted({t.shown_name for t in types})[:MAX_CANDIDATES]
    message = "several link types have that name" if found else "no link type has that name"
    raise TicketctlError(
        "invalid_request",
        message + "; the candidates are listed",
        reason="invalid_value",
        details={"option": "type", "candidates": candidates},
    )


def parse_links(raw: object) -> list[Existing]:
    """The ticket's ``issuelinks``; an absent list or any unreadable entry is ``provider_changed``.

    A write must not act on a guess: a link that cannot be read could be the one being added or
    removed, so it is never skipped.
    """
    if not isinstance(raw, list) or len(raw) > MAX_LINKS:
        raise _unreadable("an issue link list")
    out: list[Existing] = []
    for entry in raw:
        item = _obj(entry)
        lid = item.get("id")
        if isinstance(lid, int) and not isinstance(lid, bool):
            lid = str(lid)
        kind = _obj(item.get("type")).get("name")
        sides = [d for d in DIRECTIONS if item.get(f"{d}Issue") is not None]
        key = _obj(item.get(f"{sides[0]}Issue")).get("key") if len(sides) == 1 else None
        if (
            not GRAMMARS["provider_id"].matches(lid)
            or lid in (".", "..")  # the id becomes a path segment of the delete
            or not isinstance(kind, str)
            or not kind
            or not GRAMMARS["ticket_key"].matches(key)
        ):
            raise _unreadable("an issue link")
        out.append(Existing(str(lid), kind, sides[0], str(key)))
    return out


class LinkAction:
    """``link add KEY`` and ``link remove KEY`` as a ``WriteAction`` (one class, two names)."""

    dedupe_key: str | None = None
    yes_needs_dedupe_key = False

    def __init__(
        self, key: str, other: str, *, link_type: str, direction: str, remove: bool = False
    ) -> None:
        self.key = key
        self.other = other
        self.wanted_type = link_type
        self.direction = direction
        self.remove = remove
        self.name = "link_remove" if remove else "link_add"

    # -- step 1 ------------------------------------------------------------------------------
    def prepare(self, pc: PrepareContext) -> None:
        if self.key == self.other:
            raise TicketctlError(
                "invalid_request",
                "a ticket cannot be linked to itself",
                reason="invalid_value",
                details={"option": "other"},
            )

    # -- step 3 ------------------------------------------------------------------------------
    def reduce(self, ctx: Context, *, fresh: bool) -> Planned:
        kind = pick_type(read_types(ctx.gw), self.wanted_type)
        fields = self._read_ticket(ctx)
        links = parse_links(fields.get("issuelinks"))
        # a symmetric type ("relates to" both ways) is one link whichever way it is stored
        matching = [
            e
            for e in links
            if (e.type, e.other) == (kind.name, self.other)
            and (kind.symmetric or e.direction == self.direction)
        ]
        shown = f"{kind.shown_name}: {self.key} {kind.phrase(self.direction)} {self.other}"
        steps: list[Step] = []
        changes: list[dict[str, Any]] = []
        if self.remove:
            for found in matching:
                steps.append(
                    Step(
                        "remove_link",
                        self._remove_sender(found.id),
                        self._props(kind.shown_name, found.id),
                    )
                )
            if matching:
                changes.append({"name": "links", "from": [shown], "to": []})
        elif not matching:
            steps.append(
                Step("add_link", self._add_sender(kind.name), self._props(kind.shown_name))
            )
            changes.append({"name": "links", "from": [], "to": [shown]})
        return Planned(
            steps=steps,
            intent={
                "type": kind.shown_name,
                # the id and a hash of the raw name, so a type swapped under an equal shown name is
                # stale; neither carries provider text into a plan
                "type_id": kind.id,
                "type_name_sha256": hashlib.sha256(kind.name.encode()).hexdigest(),
                "direction": self.direction,
                "other": self.other,
            },
            facts={"link_ids": sorted(e.id for e in matching)},
            changes=changes,
            key=self.key,
            hook=self._hook_facts(ctx, fields, kind.shown_name),
        )

    def check(self, ctx: Context, planned: Planned) -> None:
        return None  # no precondition flag and no provider-context check for a link

    # -- helpers -----------------------------------------------------------------------------
    def _props(self, kind: str, link_id: str | None = None) -> dict[str, Any]:
        props: dict[str, Any] = {
            "link_type": kind,
            "direction": self.direction,
            "other": self.other,
        }
        if link_id is not None:
            props["link_id"] = link_id
        return props

    def _read_ticket(self, ctx: Context) -> dict[str, Any]:
        wanted = "status,issuetype,project,priority,assignee,issuelinks"
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

    # -- send --------------------------------------------------------------------------------
    def _add_sender(self, kind: str) -> Callable[[WriteGateway], StepResult]:
        # The ticket that gets the outward phrase ("blocks") is the request's inwardIssue
        # (unverified live, W22): KEY is that ticket when the direction is outward.
        source, target = (
            (self.key, self.other) if self.direction == "outward" else (self.other, self.key)
        )

        def send(gw: WriteGateway) -> StepResult:
            res = gw.request(
                "POST",
                LINK_PATH,
                template=LINK_PATH,
                body={
                    "type": {"name": kind},
                    "inwardIssue": {"key": source},
                    "outwardIssue": {"key": target},
                },
            )
            if res.status == 404:
                raise TicketctlError(
                    "not_found",
                    "a ticket or the link type is missing or you have no permission to see it",
                )
            raise_for_write(res, object_path=False)
            return StepResult()

        return send

    def _remove_sender(self, link_id: str) -> Callable[[WriteGateway], StepResult]:
        def send(gw: WriteGateway) -> StepResult:
            res = gw.request(
                "DELETE",
                f"{LINK_PATH}/{segment('provider_id', link_id)}",
                template=LINK_TEMPLATE,
            )
            if res.status == 404:
                raise TicketctlError(
                    "not_found", "the link is missing or you have no permission to see it"
                )
            raise_for_write(res, object_path=False)
            return StepResult()

        return send

    # -- hook facts --------------------------------------------------------------------------
    def _hook_facts(self, ctx: Context, fields: dict[str, Any], kind: str) -> dict[str, Any]:
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
            "links": [{"type": kind, "direction": self.direction, "key": self.other}],
        }
        if GRAMMARS["project_key"].matches(project):
            facts["project"] = project
        return facts


def _run(g: Globals, a: argparse.Namespace, *, remove: bool) -> dict[str, Any]:
    rt = load_runtime(g)
    other = normalize_ticket_key(a.other)
    chosen = selection.select_site(rt.config, site_flag=g.site, refs=[a.key, other])
    assert chosen.key is not None
    action = LinkAction(chosen.key, other, link_type=a.type, direction=a.direction, remove=remove)
    return writeflow.execute(rt, chosen.site, action, make_seams())


def link_add(g: Globals, a: argparse.Namespace) -> dict[str, Any]:
    return _run(g, a, remove=False)


def link_remove(g: Globals, a: argparse.Namespace) -> dict[str, Any]:
    return _run(g, a, remove=True)
