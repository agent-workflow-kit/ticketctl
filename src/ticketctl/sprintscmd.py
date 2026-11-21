"""Handlers of ``sprints list`` and ``sprints show`` and the board rule (docs/interface.md).

``resolve_board`` is the one place that turns ``--board``, ``sites.<site>.board`` and the project
into a board id; ``sprint add|remove`` (#34) reuses it. Its order, per the interface:

1. ``--board`` (else the site's ``board``): digits only (at most 18) is an id, used as given and not
   looked up; any other text is a board name, matched case-insensitively and exactly (``str.lower``
   on both sides, so ``Ab`` and ``aB`` are the same name) against the boards of the site (``GET
   /rest/agile/1.0/board``, pages of 50, at most 5 pages). Several matches exit 21
   ``ambiguous_board``, none exit 10 ``not_found`` with ``details.option: board``. One match in a
   search that the page cap cut off is exit 12 ``lookup_incomplete`` (a second board of that name
   may lie beyond): the message asks for the board id.
2. Else the scrum boards of ``--project``, else of the site's only project (``GET
   /rest/agile/1.0/board?projectKeyOrId=KEY&type=scrum``, also filtered on ``type == "scrum"``
   here): one is used, several exit 21 ``ambiguous_board``, none exit 10 ``not_found``; one in a
   cut-off listing is exit 12 ``lookup_incomplete`` like a name.
3. Else, with no project to derive from, exit 2 ``missing_option``.

``sprints list`` reads ``GET /rest/agile/1.0/board/{id}/sprint?state=...&startAt=...&maxResults=50``
in the provider's order until ``--limit`` sprints (default 50, 1 to 500) are in hand, the provider
says it is the last page, a page is empty, or 20 pages were read. The state filter is also applied
here, so a provider that ignores ``state`` cannot leak other sprints. A sprint id seen twice is
kept once. ``sprints show ID`` reads ``GET /rest/agile/1.0/sprint/{id}`` (no board is needed) and
exits 12 ``provider_changed`` when the answer is not that sprint.

A sprint object needs a numeric ``id``, a ``name``, a ``state`` (``future``, ``active``, ``closed``)
and the origin board id (``originBoardId``); one that lacks any of them is dropped from a listing
and named in a stderr warning (the schemas have no ``incomplete`` list), and is
``provider_changed`` for ``show``. ``startDate`` and ``endDate`` are UTC with ``Z``, null when
absent or unreadable (unreadable also warns). Names pass the sanitizer.

Unverified against a real site: the agile shapes (``values``, ``isLast``, ``originBoardId``,
``startDate``, ``endDate``, board ``type`` and ``name``), that ``state`` accepts a single state name
and ``projectKeyOrId`` and ``type`` filter the board list, that ``GET /board`` returns the boards
the user may see in pages of 50, and that a 404 on ``/board/{id}/sprint`` means the board is
missing or hidden, and that a 400 on it means a board without sprints (exit 22 ``invalid_value``
with ``details.option: board``).
"""

from __future__ import annotations

import argparse
import unicodedata
from dataclasses import dataclass
from typing import Any

from . import selection
from .config import Site
from .errors import TicketctlError, usage_error
from .flags import Globals
from .gateway import Gateway
from .readutil import hide_field_ids, object_error, warn_incomplete
from .runtime import for_site, load_runtime, open_gateway
from .sanitize import CAPS, GRAMMARS, query, sanitize_field, segment
from .timestamps import normalize_ts

BOARDS = "/rest/agile/1.0/board"
BOARD_SPRINTS_TEMPLATE = "/rest/agile/1.0/board/{id}/sprint"
SPRINT_TEMPLATE = "/rest/agile/1.0/sprint/{id}"
BOARD_PAGE = 50
BOARD_PAGES = 5
SPRINT_PAGE = 50
SPRINT_PAGES = 20
SPRINTS_CUT = "sprints.cut"  # a mark of read_sprints: the page loop ended before the listing
DEFAULT_LIMIT = 50
MAX_LIMIT = 500
STATES = ("future", "active", "closed")


def _invalid(message: str) -> TicketctlError:
    return TicketctlError("invalid_request", message, reason="invalid_value")


def effective_limit(value: int | None) -> int:
    limit = DEFAULT_LIMIT if value is None else value
    if not 1 <= limit <= MAX_LIMIT:
        raise _invalid(f"--limit takes a number from 1 to {MAX_LIMIT}")
    return limit


# --- sprint objects ----------------------------------------------------------------------------


def _int(value: object, *, low: int = 0) -> int | None:
    if isinstance(value, int) and not isinstance(value, bool) and low <= value < 10**18:
        return value
    return None


def _time(raw: object, part: str, marks: set[str]) -> str | None:
    if raw is None:
        return None
    stamp = normalize_ts(raw)
    if stamp is None:
        marks.add(part)
    return stamp


def sprint_object(raw: object, marks: set[str]) -> dict[str, Any] | None:
    """One sprint object of ``schemas/defs.json#sprint``, or None (named in ``marks``)."""
    if not isinstance(raw, dict):
        marks.add("sprints")
        return None
    sid, board = _int(raw.get("id")), _int(raw.get("originBoardId"), low=1)
    name, state = raw.get("name"), raw.get("state")
    if sid is None or board is None or not isinstance(name, str) or state not in STATES:
        marks.add("sprints.unreadable")
        return None
    shown, cut = sanitize_field(hide_field_ids(name), cap=CAPS["name"])
    if not shown.strip():
        marks.add("sprints.unreadable")  # blank to a reader (zero-width text shows a marker)
        return None
    if cut:
        marks.add("sprints.truncated")
    return {
        "id": sid,
        "name": shown,
        "state": state,
        "start": _time(raw.get("startDate"), "sprints.start", marks),
        "end": _time(raw.get("endDate"), "sprints.end", marks),
        "board": board,
    }


# --- boards ------------------------------------------------------------------------------------


@dataclass(frozen=True)
class Board:
    id: int
    name: str
    type: str | None


def _page(
    gw: Gateway, path: str, template: str, params: dict[str, object], *, named: bool
) -> dict[str, Any]:
    """One page; ``named`` is true when the path names a board (a 404 is then ``not_found``)."""
    response = gw.request("GET", path, template=template, query=query(params))
    if not 200 <= response.status < 300:
        if not named:  # a 404 of the collection endpoint is a provider change
            raise TicketctlError(
                "provider_changed", f"unexpected answer from the provider ({response.status})"
            )
        if response.status == 400:  # a board without sprints (a kanban board) answers 400
            raise TicketctlError(
                "invalid_request",
                "the board does not support sprints; name a scrum board with --board <id>",
                reason="invalid_value",
                details={"option": "board"},
            )
        raise object_error(response.status, "board", option="board")
    if not isinstance(response.data, dict):
        raise TicketctlError("provider_changed", "the provider answered no page")
    return response.data


def list_boards(gw: Gateway, extra: dict[str, object] | None = None) -> tuple[list[Board], bool]:
    """``(boards, complete)``: up to 5 pages of 50; ``complete`` is false if the cap stopped it."""
    boards: dict[int, Board] = {}
    start = 0
    for _ in range(BOARD_PAGES):
        page = _page(
            gw,
            BOARDS,
            BOARDS,
            {"startAt": start, "maxResults": BOARD_PAGE, **(extra or {})},
            named=False,
        )
        values = page.get("values")
        if not isinstance(values, list):
            raise TicketctlError("provider_changed", "the provider answered no board list")
        for item in values:
            if not isinstance(item, dict):
                continue
            bid, name = _int(item.get("id")), item.get("name")
            if bid is None or bid == 0 or not isinstance(name, str):
                continue
            kind = item.get("type")
            boards.setdefault(bid, Board(bid, name, kind if isinstance(kind, str) else None))
        if page.get("isLast") is True or not values:
            return list(boards.values()), True
        start += len(values)
    return list(boards.values()), False


def _candidates(boards: list[Board]) -> list[str]:
    out = []
    for b in sorted(boards, key=lambda b: b.id):
        label, _ = sanitize_field(hide_field_ids(b.name), cap=CAPS["name"])
        out.append(f"{b.id} ({label})")
    return out


def _ambiguous(boards: list[Board], what: str) -> TicketctlError:
    return TicketctlError(
        "ambiguous_board",
        f"{what} matches several boards; name one with --board <id>",
        details={"candidates": _candidates(boards)},
    )


def _incomplete(what: str) -> TicketctlError:
    """One board matched, but the search was cut off, so a second match may lie beyond it."""
    return TicketctlError(
        "lookup_incomplete",
        f"{what} matched one board, but the board search was cut off before the end; "
        "name the board with --board <id>",
    )


def _no_board(why: str) -> TicketctlError:
    return TicketctlError("not_found", why, details={"option": "board"})


def board_spec(value: object) -> int | str:
    """A ``--board`` or configured board: an id (digits only) or a name; else exit 22."""
    if isinstance(value, int) and not isinstance(value, bool):
        value = str(value)
    if not isinstance(value, str) or not value.strip():
        raise _invalid("--board is empty")
    if len(value) > 256:
        raise _invalid("--board is too long")
    if any(unicodedata.category(ch) in ("Cc", "Cs", "Zl", "Zp") for ch in value):
        raise _invalid("--board holds a control character")
    if value.isascii() and GRAMMARS["numeric_id"].matches(value):
        return int(value)
    return value


def resolve_board(
    gw: Gateway,
    site: Site,
    *,
    board: str | None,
    project: str | None,
) -> int:
    """The one board id for a command (see the module docstring)."""
    spec = board_spec(board) if board is not None else None
    if spec is None and site.board is not None:
        spec = board_spec(site.board)
    if isinstance(spec, int):
        if spec == 0:
            raise _invalid("--board is not a board id")
        return spec
    if spec is not None:
        boards, complete = list_boards(gw)
        wanted = spec.lower()
        found = [b for b in boards if b.name.lower() == wanted]
        if len(found) > 1:
            raise _ambiguous(found, "the board name")
        if not found:
            more = "" if complete else " (only the first boards were searched)"
            raise _no_board(f"no board has that name{more}")
        if not complete:
            raise _incomplete("the board name")
        return found[0].id
    derived = (
        project if project is not None else (site.projects[0] if len(site.projects) == 1 else None)
    )
    if derived is None:
        raise usage_error(
            "missing_option", "no board: give --board or --project (or set the site's board)"
        )
    boards, complete = list_boards(gw, {"projectKeyOrId": derived, "type": "scrum"})
    scrum = [b for b in boards if b.type == "scrum"]
    if len(scrum) > 1:
        raise _ambiguous(scrum, "the project")
    if not scrum:
        more = "" if complete else " (only the first boards were searched)"
        raise _no_board(f"the project has no scrum board{more}")
    if not complete:
        raise _incomplete("the project")
    return scrum[0].id


# --- commands ----------------------------------------------------------------------------------


def _select(g: Globals, rt: Any, project: str | None) -> str:
    projects = [project] if project is not None else []
    return selection.select_site(rt.config, site_flag=g.site, projects=projects).site


def sprints_list(g: Globals, a: argparse.Namespace) -> dict[str, Any]:
    limit = effective_limit(a.limit)
    rt = load_runtime(g)
    site = _select(g, rt, a.project)
    marks: set[str] = set()
    with for_site(site):
        gw = open_gateway(rt, site)
        board = resolve_board(gw, rt.config.sites[site], board=a.board, project=a.project)
        sprints = read_sprints(gw, board, a.state, limit, marks)
    warn_incomplete(rt, "the sprints", marks - {SPRINTS_CUT})
    return {"site": site, "sprints": sprints}


def read_sprints(
    gw: Gateway, board: int, state: str | None, limit: int, marks: set[str]
) -> list[dict[str, Any]]:
    path = f"{BOARDS}/{segment('numeric_id', str(board))}/sprint"
    out: list[dict[str, Any]] = []
    seen: set[int] = set()
    start = 0
    for _ in range(SPRINT_PAGES):
        params: dict[str, object] = {"startAt": start, "maxResults": SPRINT_PAGE}
        if state is not None:
            params["state"] = state
        page = _page(gw, path, BOARD_SPRINTS_TEMPLATE, params, named=True)
        values = page.get("values")
        if not isinstance(values, list):
            raise TicketctlError("provider_changed", "the provider answered no sprint list")
        for item in values:
            obj = sprint_object(item, marks)
            if obj is None or (state is not None and obj["state"] != state):
                continue
            if obj["id"] in seen:
                marks.add("sprints")
                continue
            seen.add(obj["id"])
            out.append(obj)
            if len(out) >= limit:
                return out
        if page.get("isLast") is True or not values:
            return out
        start += len(values)
    marks.add(SPRINTS_CUT)
    return out


def sprints_show(g: Globals, a: argparse.Namespace) -> dict[str, Any]:
    rt = load_runtime(g)
    site = _select(g, rt, a.project)
    marks: set[str] = set()
    wanted = int(a.id)
    with for_site(site):
        gw = open_gateway(rt, site)
        response = gw.request(
            "GET",
            f"/rest/agile/1.0/sprint/{segment('numeric_id', a.id)}",
            template=SPRINT_TEMPLATE,
        )
        if not 200 <= response.status < 300:
            raise object_error(response.status, "sprint")
        sprint = sprint_object(response.data, marks)
        if sprint is None or sprint["id"] != wanted:
            raise TicketctlError("provider_changed", "the provider answered no usable sprint")
    warn_incomplete(rt, "the sprint", marks)
    return {"site": site, "sprint": sprint}


def _line(s: dict[str, Any]) -> str:
    when = f" {s['start']} to {s['end']}" if s["start"] else ""
    return f"{s['id']} {s['name']} [{s['state']}] board {s['board']}{when}"


def human_sprints_list(r: dict[str, Any]) -> str:
    return "\n".join(_line(s) for s in r["sprints"]) or "no sprints"


def human_sprints_show(r: dict[str, Any]) -> str:
    return _line(r["sprint"])
