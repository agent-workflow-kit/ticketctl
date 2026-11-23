"""Handler of ``changelog KEY`` (docs/interface.md, ``changelog KEY``; schemas/cli/changelog.json).

One run reads ``GET /issue/{key}/changelog?startAt=...&maxResults=100`` page after page and turns
every raw history entry into the entry object of ``schemas/defs.json#changelog_entry``:

* the author is the account object of a comment's author (a missing author is the unknown user,
  without a mark: system-made changes have none);
* ``created`` goes through ``timestamps.normalize_ts``; an entry whose id or ``created`` is
  unreadable is dropped and named;
* every item is ``{field, from, to}``. ``field`` is the logical or ``extra`` name when the item's
  field id is a mapped field of the site, else the fixed name of a system field, else the
  provider's display name (sanitized, with any ``customfield_N`` text hidden), so no custom field id
  is printed. The value kind follows the resolved field, never the content: ``assignee`` and
  ``reporter`` are accounts, ``labels`` a list, the sprint field a list of ``{id, name}``, the
  description a body-class text and everything else one line of text. The provider's text keys
  (``fromString``, ``toString``) are preferred to its id keys (``from``, ``to``).

Then ``--since`` keeps the entries created at or after the cut-off (inclusive), ``--field`` keeps
the items whose printed field name matches (case-insensitive) and drops the entries left empty,
and the matches are ordered by ``created`` with the id as tie-break whatever order the provider
sent; the first ``--limit`` are printed in the requested direction.

Bounds, as for ``comments``: at most 1000 raw entries are looked at, at most 20 page requests, and
the loop stops at the first empty page. The provider lists the oldest entry first (unverified); on
a history above 1000 entries ``--newest-first`` therefore starts the window at ``total - 1000`` so
the newest end is the one read. ``truncated`` is true when the limit or a bound stopped the scan
with entries unread or matching, when a page ended it early, when the provider gave no usable count
(``isLast`` true on a page with no count stands for the end) and when an entry was dropped or a
repeated id skipped. ``incomplete`` names ``changelog`` (an entry, item or value dropped or not
readable, a page that is not a list, no usable count), ``changelog.invalid_id`` (an identifier
outside its grammar), and ``changelog.truncated`` (a text or list cut at its cap, or a bound that
stopped the scan). The same names are one stderr warning (which ``--quiet`` drops).

Unverified against a real site: the page shape and paging of ``/changelog``, the oldest-first
order, which keys an item fills for each field kind (ids in ``from``/``to``, text in the
``*String`` keys), and that a sprint item joins its names with ``", "``.
"""

from __future__ import annotations

import argparse
import re
from dataclasses import dataclass
from typing import Any

from . import fieldload, selection
from .comment_object import UNKNOWN_USER
from .commentscmd import DEFAULT_LIMIT, MAX_LIMIT, effective_limit, since_cutoff
from .config import LOGICAL_FIELDS
from .errors import TicketctlError
from .fieldmap import SiteFields, Status
from .flags import Globals
from .gateway import Gateway
from .readutil import hide_field_ids, object_error, warn_incomplete
from .runtime import Runtime, for_site, load_runtime, open_gateway
from .sanitize import CAPS, GRAMMARS, query, sanitize_field, segment
from .timestamps import normalize_ts

__all__ = [
    "DEFAULT_LIMIT",
    "MAX_LIMIT",
    "changelog",
    "effective_limit",
    "human_changelog",
    "since_cutoff",
]

MAX_SCAN = 1000
PAGE_SIZE = 100
MAX_REQUESTS = 20
MAX_ITEMS = 200  # items of one entry that are kept
MAX_LIST = 1000  # labels or sprints of one value that are kept
MAX_FIELD_FILTERS = 20
CHANGELOG_TEMPLATE = "/rest/api/3/issue/{key}/changelog"
TEXT_CAP = CAPS["title"]
NAME_CAP = CAPS["name"]
BODY_CAP = CAPS["body"]
_DEDUPE_LABEL = re.compile(r"tc-[0-9a-f]{16}", re.ASCII)
_DIGITS = re.compile(r"[0-9]{1,18}", re.ASCII)

# Field ids the provider fixes, with the printed name (a site's own mapping wins over them).
SYSTEM_NAMES = {
    "status": "status",
    "resolution": "resolution",
    "assignee": "assignee",
    "reporter": "reporter",
    "priority": "priority",
    "labels": "labels",
    "summary": "title",
    "description": "description",
    "duedate": "due_date",
    "parent": "parent",
    "issuetype": "type",
}
PERSON = "person"
LABELS = "labels"
SPRINT = "sprint"
BODY = "body"
TEXT = "text"
_KIND_OF_NAME = {
    "assignee": PERSON,
    "reporter": PERSON,
    "labels": LABELS,
    "sprint": SPRINT,
    "description": BODY,
    "acceptance_criteria": BODY,
}


def _invalid(message: str) -> TicketctlError:
    return TicketctlError("invalid_request", message, reason="invalid_value")


def field_filters(values: list[str] | None) -> frozenset[str]:
    """``--field`` values as a lower-cased set; exit 22 for a blank or long one or more than 20."""
    found: set[str] = set()
    if values is not None and len(values) > MAX_FIELD_FILTERS:
        raise _invalid(f"--field takes at most {MAX_FIELD_FILTERS} values")
    for value in values or ():
        if not isinstance(value, str) or not value.strip() or len(value) > NAME_CAP:
            raise _invalid(f"--field takes a field name of 1 to {NAME_CAP} characters")
        found.add(value.strip().lower())
    return frozenset(found)


def mapped_ids(site_fields: SiteFields) -> dict[str, str]:
    """Provider field id -> the logical or ``extra`` name of the site's unique mapping of it."""
    out: dict[str, str] = {}
    for name in (*LOGICAL_FIELDS, *sorted(site_fields.config.extra)):
        try:
            found = site_fields.resolve(name)
        except TicketctlError:
            continue
        if found.status is Status.FOUND and len(found.fields) == 1:
            out.setdefault(found.fields[0].id, name)
    return out


def _author(raw: object, marks: set[str]) -> dict[str, Any]:
    if raw is None:
        return {"account_id": None, "display_name": UNKNOWN_USER}
    if not isinstance(raw, dict):
        marks.add("changelog")
        return {"account_id": None, "display_name": UNKNOWN_USER}
    account = raw.get("accountId")  # never emailAddress
    if not GRAMMARS["account_id"].matches(account):
        if account is not None:
            marks.add("changelog.invalid_id")
        return {"account_id": None, "display_name": UNKNOWN_USER}
    name = raw.get("displayName")
    shown = UNKNOWN_USER
    if isinstance(name, str) and name:
        shown, cut = sanitize_field(name, cap=NAME_CAP)
        if cut:
            marks.add("changelog.truncated")
    return {"account_id": account, "display_name": shown}


def _text(raw: object, marks: set[str], *, cap: int, body: bool = False) -> str | None:
    """One text side: null for none or empty, else sanitized text (a non-string is null, marked)."""
    if raw is None or raw == "":
        return None
    if not isinstance(raw, str):
        marks.add("changelog")
        return None
    text, cut = sanitize_field(raw, cap=cap, body=body)
    if cut:
        marks.add("changelog.truncated")
    return text


def _pick(text: object, ident: object) -> object:
    """The provider's text when it sent one, else its id (``None`` when neither)."""
    return text if text not in (None, "") else ident


def _person(ident: object, text: object, marks: set[str]) -> dict[str, Any] | None:
    account: str | None = None
    if isinstance(ident, str):
        if GRAMMARS["account_id"].matches(ident):
            account = ident
        else:
            marks.add("changelog.invalid_id")
    elif ident is not None:
        marks.add("changelog")
    name = _text(text, marks, cap=NAME_CAP)
    if account is None and name is None:
        return None
    return {"account_id": account, "display_name": name or UNKNOWN_USER}


def _labels(text: object, marks: set[str]) -> list[str]:
    if text is None or text == "":
        return []
    if not isinstance(text, str):
        marks.add("changelog")
        return []
    out: list[str] = []
    words = text.split()
    if len(words) > MAX_LIST:
        marks.add("changelog.truncated")
        words = words[:MAX_LIST]
    for word in words:
        if _DEDUPE_LABEL.fullmatch(word):
            continue  # the dedupe label is never listed (item_object names a one-sided one)
        shown = _text(word, marks, cap=CAPS["label"])
        if shown is not None:
            out.append(shown)
    return out


def _sprints(ident: object, text: object, marks: set[str]) -> list[dict[str, Any]]:
    """A sprint list; a name is paired with an id only when both lists have the same length."""
    for part in (ident, text):
        if part is not None and not isinstance(part, str):
            marks.add("changelog")
            return []
    id_parts = [p.strip() for p in ident.split(",")] if isinstance(ident, str) and ident else []
    name_all = [p.strip() for p in text.split(",")] if isinstance(text, str) and text else []
    id_parts = [p for p in id_parts if p]
    name_parts = [p for p in name_all if p]
    if len(id_parts) > MAX_LIST or len(name_parts) > MAX_LIST or len(name_all) > MAX_LIST:
        marks.add("changelog.truncated")
        id_parts, name_parts = id_parts[:MAX_LIST], name_parts[:MAX_LIST]
        name_all = name_all[:MAX_LIST]
    if not id_parts:
        return [{"id": None, "name": _text(n, marks, cap=NAME_CAP)} for n in name_parts]
    # an empty name element makes the names unpairable
    paired = len(name_all) == len(id_parts) and all(name_all)
    if name_all and not paired:
        marks.add("changelog")  # names that cannot be paired are lost
    out: list[dict[str, Any]] = []
    for n, part in enumerate(id_parts):
        sprint_id = int(part) if _DIGITS.fullmatch(part) else None
        if sprint_id is None:
            marks.add("changelog")  # an id that does not parse is lost
        name = _text(name_all[n], marks, cap=NAME_CAP) if paired else None
        out.append({"id": sprint_id, "name": name})
    return out


def _value(kind: str, ident: object, text: object, marks: set[str]) -> Any:
    if kind == PERSON:
        return _person(ident, text, marks)
    if kind == LABELS:
        return _labels(text if text not in (None, "") else None, marks)
    if kind == SPRINT:
        return _sprints(ident, text, marks)
    picked = _pick(text, ident)
    if kind == BODY:
        return _text(picked, marks, cap=BODY_CAP, body=True)
    return _text(picked, marks, cap=TEXT_CAP)


def _field(raw: dict[str, Any], names: dict[str, str], marks: set[str]) -> tuple[str, str] | None:
    """(the printed field name, the value kind) of an item, or None when it has no readable name."""
    fid = raw.get("fieldId")
    if isinstance(fid, str):
        if fid in names:
            logical = names[fid]
            return logical, _KIND_OF_NAME.get(logical, TEXT)
        if fid in SYSTEM_NAMES:
            printed = SYSTEM_NAMES[fid]
            return printed, _KIND_OF_NAME.get(printed, TEXT)
    display = raw.get("field")
    if not isinstance(display, str) or not display.strip():
        marks.add("changelog")
        return None
    shown, cut = sanitize_field(hide_field_ids(display), cap=NAME_CAP)
    if cut:
        marks.add("changelog.truncated")
    return shown, TEXT


ITEMS_CUT = "<items cut>"  # internal: matching items beyond the cap were cut (never printed)


def item_object(raw: object, names: dict[str, str], marks: set[str]) -> dict[str, Any] | None:
    if not isinstance(raw, dict):
        marks.add("changelog")
        return None
    found = _field(raw, names, marks)
    if found is None:
        return None
    printed, kind = found
    if kind == LABELS and _one_sided_dedupe(raw.get("fromString"), raw.get("toString")):
        marks.add("changelog")  # a real label of this shape may have been added or removed
    return {
        "field": printed,
        "from": _value(kind, raw.get("from"), raw.get("fromString"), marks),
        "to": _value(kind, raw.get("to"), raw.get("toString"), marks),
    }


def _dedupe_words(text: object) -> set[str]:
    if not isinstance(text, str):
        return set()
    return {w for w in text.split()[:MAX_LIST] if _DEDUPE_LABEL.fullmatch(w)}


def _one_sided_dedupe(old: object, new: object) -> bool:
    """True when a dedupe-shaped label is on one side of the pair only."""
    return _dedupe_words(old) != _dedupe_words(new)


def entry_object(
    raw: object,
    names: dict[str, str],
    marks: set[str],
    fields: frozenset[str] = frozenset(),
) -> dict[str, Any] | None:
    """One changelog entry, or None for an entry that cannot be kept (named in ``marks``).

    With ``fields`` the filter runs before the item cap, so the cap counts matching items only.
    """
    if not isinstance(raw, dict):
        marks.add("changelog")
        return None
    eid = raw.get("id")
    if not GRAMMARS["provider_id"].matches(eid):
        marks.add("changelog.invalid_id" if eid is not None else "changelog")
        return None
    created = normalize_ts(raw.get("created"))
    if created is None:
        marks.add("changelog")
        return None
    items: list[dict[str, Any]] = []
    raw_items = raw.get("items")
    if isinstance(raw_items, list):
        if not fields and len(raw_items) > MAX_ITEMS:
            marks.add("changelog.truncated")
        for entry in raw_items if fields else raw_items[:MAX_ITEMS]:
            item_marks: set[str] = set()
            item = item_object(entry, names, item_marks)
            if item is None or not fields or item["field"].lower() in fields:
                marks |= item_marks  # an unreadable item might have matched
            if item is not None and (not fields or item["field"].lower() in fields):
                items.append(item)
        if len(items) > MAX_ITEMS:
            items = items[:MAX_ITEMS]
            marks.update(("changelog.truncated", ITEMS_CUT))
    else:
        marks.add("changelog")
    return {
        "id": eid,
        "author": _author(raw.get("author"), marks),
        "created": created,
        "items": items,
    }


def order_key(obj: dict[str, Any]) -> tuple[str, int, str]:
    """Oldest-first sort key: ``created``, then the id (numeric ids by size)."""
    return obj["created"], len(obj["id"]), obj["id"]


@dataclass
class Scan:
    items: list[dict[str, Any]]
    total: int
    truncated: bool
    marks: set[str]


def _count(page: dict[str, Any]) -> int | None:
    count = page.get("total")
    if isinstance(count, int) and not isinstance(count, bool) and count >= 0:
        return count
    return None


def scan_changelog(
    gw: Gateway,
    key: str,
    names: dict[str, str],
    *,
    limit: int,
    newest_first: bool,
    since: str | None,
    fields: frozenset[str],
) -> Scan:
    """Read, filter and order the changelog of ``key`` (see the module docstring)."""
    path = f"/rest/api/3/issue/{segment('ticket_key', key)}/changelog"
    marks: set[str] = set()
    matches: list[tuple[dict[str, Any], set[str]]] = []
    seen: set[str] = set()
    start = scanned = requests = 0
    total: int | None = None
    more = False  # entries are known or suspected to remain unread
    dropped = False  # an entry was dropped or a repeated id skipped
    jumped = False
    while True:
        if requests >= MAX_REQUESTS:
            more = True
            break
        requests += 1
        response = gw.request(
            "GET",
            path,
            template=CHANGELOG_TEMPLATE,
            query=query({"startAt": start, "maxResults": PAGE_SIZE}),
        )
        if not 200 <= response.status < 300:
            raise object_error(response.status, "ticket")
        page = response.data
        if not isinstance(page, dict):
            if requests == 1:
                raise TicketctlError("provider_changed", "the provider answered no changelog page")
            marks.add("changelog")
            more = True
            break
        count = _count(page)
        if count is not None:
            total = count if total is None else max(total, count)
        if newest_first and not jumped and start == 0 and total is not None and total > MAX_SCAN:
            jumped = True  # the newest end is the wanted one: read the window that ends the list
            first = page.get("values")
            size = len(first) if isinstance(first, list) else 0
            if size == 0:
                size = PAGE_SIZE  # no page length observed: assume the requested one
            start = max(0, total - min(MAX_SCAN, (MAX_REQUESTS - 1) * size))
            more = True
            continue
        raw = page.get("values")
        if not isinstance(raw, list):
            marks.add("changelog")
            more = True
            break
        chunk = raw[: MAX_SCAN - scanned]
        for item in chunk:
            own_marks: set[str] = set()
            obj = entry_object(item, names, own_marks, fields)
            cut = ITEMS_CUT in own_marks
            own_marks.discard(ITEMS_CUT)
            if obj is None:
                marks |= own_marks
                dropped = True
            elif obj["id"] in seen:
                marks.add("changelog")  # a repeated id is kept once
                dropped = True
            else:
                seen.add(obj["id"])
                if since is not None and obj["created"] < since:
                    continue
                if cut:
                    dropped = True  # matching items beyond the cap are not shown
                if fields and not obj["items"]:
                    if own_marks:  # the lost part might have matched
                        marks |= own_marks
                        dropped = True
                    continue
                matches.append((obj, own_marks))
        scanned += len(chunk)
        start += len(raw)
        if not raw:
            more = more or total is None or start < total
            break
        if scanned >= MAX_SCAN:
            more = more or total is None or start < total or len(raw) > len(chunk)
            break
        if total is None and page.get("isLast") is True:
            total = start
            break
        if total is not None and start >= total:
            break
        if total is None and len(raw) < PAGE_SIZE:
            break
    matches.sort(key=lambda pair: order_key(pair[0]), reverse=newest_first)
    shown = matches[:limit]
    for _, own_marks in shown:
        marks |= own_marks
    truncated = len(matches) > limit or more or dropped
    if more:
        marks.add("changelog.truncated")
    if total is None:
        marks.add("changelog")  # no usable count: the history is unknown to be complete
        total = scanned
        truncated = True
    return Scan([obj for obj, _ in shown], max(total, scanned), truncated, marks)


def changelog(g: Globals, a: argparse.Namespace) -> dict[str, Any]:
    limit = effective_limit(a.limit)
    since = since_cutoff(a.since) if a.since is not None else None
    fields = field_filters(a.field)
    rt = load_runtime(g)
    picked = selection.select_site(rt.config, site_flag=g.site, refs=[a.key])
    assert picked.key is not None
    site = picked.site
    with for_site(site):
        gw = open_gateway(rt, site)
        names = mapped_ids(fieldload.load_site_fields(rt, site, gw))
        result = scan_changelog(
            gw,
            picked.key,
            names,
            limit=limit,
            newest_first=a.newest_first,
            since=since,
            fields=fields,
        )
    _warn(rt, result.marks)
    return {
        "key": picked.key,
        "site": site,
        "items": result.items,
        "total": result.total,
        "truncated": result.truncated,
        "incomplete": sorted(result.marks),
    }


def _warn(rt: Runtime, marks: set[str]) -> None:
    warn_incomplete(rt, "the changelog", marks)


def _show(value: Any) -> str:
    if value is None:
        return "(none)"
    if isinstance(value, str):
        return value
    if isinstance(value, dict):
        return str(value["display_name"])
    parts = [
        v if isinstance(v, str) else str(v["name"] if v["name"] is not None else v["id"])
        for v in value
    ]
    return ", ".join(parts) if parts else "(none)"


def human_changelog(r: dict[str, Any]) -> str:
    lines = [f"{r['key']}: {len(r['items'])} of {r['total']} entries" + (", more" * r["truncated"])]
    for e in r["items"]:
        lines.append(f"{e['id']} {e['created']} {e['author']['display_name']}:")
        for i in e["items"]:
            lines.append(f"  {i['field']}: {_show(i['from'])} -> {_show(i['to'])}")
    return "\n".join(lines)
