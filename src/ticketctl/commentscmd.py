"""Handler of ``comments KEY`` (docs/interface.md, ``comments KEY``; ``schemas/cli/comments.json``).

One run reads ``GET /issue/{key}/comment?orderBy=...&startAt=...&maxResults=100&expand=properties``
page after page. ``orderBy`` is ``created`` (oldest first) by default and ``-created`` with
``--newest-first``, so the scan meets the wanted end of the ticket first. Every raw comment goes
through ``comment_object.comment_object`` (the same object ``get --comments`` prints), then:

* ``--since`` keeps the comments whose ``created`` is at or after the cut-off (inclusive, so an
  incremental refresh that reuses the last ``created`` it saw loses nothing);
* ``--own`` keeps the comments whose author is the resolved ``/myself`` account (``own``);
* the matches are ordered by ``created`` with the id as the tie-break whatever order the provider
  sent, and the first ``--limit`` are printed in the requested direction.

Bounds: at most 1000 raw comments are looked at (the dedupe cap), at most 20 page requests, and the
loop stops at the first empty page, so a provider that repeats a page, sends short pages or
ignores ``startAt`` cannot make it run on. The scan never stops early on the strength of the
provider's order: the whole bounded window is read and sorted here, so a provider that ignores
``orderBy`` still yields the true oldest (or newest) ``limit`` comments of the window. Only the
window itself depends on the provider: on a thread above 1000 comments it follows ``orderBy``
(unverified, see design.md), so the sort is exact within the window, not over the whole thread.

``total`` is the provider's comment count before the filters (never less than the comments seen).
``truncated`` is true when the limit or a bound stopped the scan with more comments unread or
matching, when a page ended the scan (empty, or not a comment list) while the count says more
remain, when the provider gave no usable count, and when a comment was dropped or a repeated id
was skipped (the schema has no ``incomplete`` list, so this is how stdout shows that the listing
is not the whole thread). Those parts, and a count that is missing, are also one stderr warning
(which ``--quiet`` drops). ``key`` is the requested key in canonical form; a moved ticket's new
key is not printed here (the comment endpoint resolves the old key).

``--since`` takes the one time grammar: an RFC 3339 timestamp with ``Z`` or an offset, a date
(00:00 UTC) or a duration ``<n>m|h|d|w`` counted back from now. It is judged before any request.

Unverified against a real site: that ``orderBy=created`` and ``-created`` are honored, that
``startAt`` and ``maxResults=100`` page the way the fake does, and that ``total`` counts every
comment of the ticket.
"""

from __future__ import annotations

import argparse
import re
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from typing import Any

from . import identity, selection
from .comment_object import comment_object, order_key
from .errors import TicketctlError
from .flags import Globals
from .gateway import Gateway
from .readutil import object_error, warn_incomplete
from .runtime import Runtime, for_site, load_runtime, open_gateway
from .sanitize import query, segment
from .search_compile import is_rfc3339
from .timestamps import normalize_ts

DEFAULT_LIMIT = 50
MAX_LIMIT = 200
MAX_SCAN = 1000
PAGE_SIZE = 100
MAX_REQUESTS = 20
COMMENT_TEMPLATE = "/rest/api/3/issue/{key}/comment"
_DURATION = re.compile(r"([0-9]{1,4})([mhdw])", re.ASCII)
_DATE = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}", re.ASCII)
_UNITS = {"m": "minutes", "h": "hours", "d": "days", "w": "weeks"}
_MAX_TIME = 64


def _invalid(message: str) -> TicketctlError:
    return TicketctlError("invalid_request", message, reason="invalid_value")


def effective_limit(value: int | None) -> int:
    """``--limit``: 1 to 200, default 50."""
    limit = DEFAULT_LIMIT if value is None else value
    if not 1 <= limit <= MAX_LIMIT:
        raise _invalid(f"--limit takes a number from 1 to {MAX_LIMIT}")
    return limit


def _iso(moment: datetime) -> str:
    return moment.astimezone(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


def since_cutoff(value: str, now: datetime | None = None) -> str:
    """The ``--since`` cut-off as UTC ISO 8601 with ``Z`` (comparable with a comment's ``created``).

    Exit 22 ``invalid_value`` for anything outside the time grammar. A duration is counted back
    from ``now`` (default the current time), a bare date is 00:00 UTC, a timestamp keeps its
    instant (fractions of a second are dropped, which keeps the cut-off no later than written).
    """
    if not isinstance(value, str) or len(value) > _MAX_TIME:
        raise _invalid("--since is not a time")
    if m := _DURATION.fullmatch(value):
        moment = (now or datetime.now(UTC)) - timedelta(**{_UNITS[m.group(2)]: int(m.group(1))})
        return _iso(moment)
    if _DATE.fullmatch(value):
        try:
            day = date.fromisoformat(value)
        except ValueError:
            raise _invalid("--since is not a valid date") from None
        return _iso(datetime(day.year, day.month, day.day, tzinfo=UTC))
    if is_rfc3339(value):
        normalized = normalize_ts(value)
        if normalized is not None:
            return normalized
        raise _invalid("--since is not a valid timestamp")
    raise _invalid("--since is not a time (RFC 3339 timestamp, YYYY-MM-DD or <n>m|h|d|w)")


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


def scan_comments(
    gw: Gateway,
    key: str,
    me_account_id: str | None,
    *,
    limit: int,
    newest_first: bool,
    since: str | None,
    own_only: bool,
) -> Scan:
    """Read, filter and order the comments of ``key`` (see the module docstring)."""
    path = f"/rest/api/3/issue/{segment('ticket_key', key)}/comment"
    marks: set[str] = set()
    matches: list[tuple[dict[str, Any], set[str]]] = []
    seen: set[str] = set()
    start = scanned = requests = 0
    total: int | None = None
    more = False  # comments are known or suspected to remain unread
    dropped = False  # a comment was dropped or a repeated id skipped
    while True:
        if requests >= MAX_REQUESTS:
            more = True
            break
        requests += 1
        response = gw.request(
            "GET",
            path,
            template=COMMENT_TEMPLATE,
            query=query(
                {
                    "orderBy": "-created" if newest_first else "created",
                    "startAt": start,
                    "maxResults": PAGE_SIZE,
                    "expand": "properties",
                }
            ),
        )
        if not 200 <= response.status < 300:
            raise object_error(response.status, "ticket")
        page = response.data
        if not isinstance(page, dict):
            if requests == 1:
                raise TicketctlError("provider_changed", "the provider answered no comment page")
            marks.add("comments")
            more = True
            break
        count = _count(page)
        if count is not None:
            total = count if total is None else max(total, count)
        raw = page.get("comments")
        if not isinstance(raw, list):
            marks.add("comments")
            more = True
            break
        chunk = raw[: MAX_SCAN - scanned]
        for item in chunk:
            own_marks: set[str] = set()
            obj = comment_object(item, me_account_id, own_marks)
            if obj is None:
                marks |= own_marks
                dropped = True
            elif obj["id"] in seen:
                marks.add("comments")  # a repeated id is kept once
                dropped = True
            else:
                seen.add(obj["id"])
                if (since is None or obj["created"] >= since) and (not own_only or obj["own"]):
                    matches.append((obj, own_marks))
        scanned += len(chunk)
        start += len(raw)
        if not raw:
            more = total is None or start < total
            break
        if scanned >= MAX_SCAN:
            more = total is None or start < total or len(raw) > len(chunk)
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
    if total is None:
        marks.add("comments")  # no usable count: the page is unknown to be complete
        total = scanned
        truncated = True
    return Scan([obj for obj, _ in shown], max(total, scanned), truncated, marks)


def comments(g: Globals, a: argparse.Namespace) -> dict[str, Any]:
    limit = effective_limit(a.limit)
    since = since_cutoff(a.since) if a.since is not None else None
    rt = load_runtime(g)
    picked = selection.select_site(rt.config, site_flag=g.site, refs=[a.key])
    assert picked.key is not None
    site = picked.site
    with for_site(site):
        gw = open_gateway(rt, site)
        me = identity.load_me(rt, site, gw, refresh=False)
        result = scan_comments(
            gw,
            picked.key,
            me.account_id,
            limit=limit,
            newest_first=a.newest_first,
            since=since,
            own_only=a.own,
        )
    _warn(rt, result.marks)
    return {
        "key": picked.key,
        "site": site,
        "items": result.items,
        "total": result.total,
        "truncated": result.truncated,
    }


def _warn(rt: Runtime, marks: set[str]) -> None:
    warn_incomplete(rt, "the comments", marks)


def human_comments(r: dict[str, Any]) -> str:
    lines = [
        f"{r['key']}: {len(r['items'])} of {r['total']} comments" + (", more" * r["truncated"])
    ]
    for c in r["items"]:
        mark = " (own)" if c["own"] else ""
        lines.append(f"{c['id']} {c['created']} {c['author']['display_name']}{mark}:")
        lines += [f"  {line}" for line in c["body"].split("\n")]
    return "\n".join(lines)
