"""The ``search`` command: compile, parse check, count, token paging and normalization.

The rules live in docs/interface.md under "search", "Search compilation" and "Caps". The order of
one run:

1. offline checks (``check_offline``, called by the CLI before any config is read): exactly-one
   rules, the raw query combination rule, ``--limit`` and the ``--fields`` vocabulary;
2. the cursor is decoded (every failure exit 22 ``cursor_invalid``), the site is selected and the
   query is compiled by ``search_compile`` (the only place JQL is built; nothing here
   concatenates JQL);
3. the cursor must belong to this site and to the sha256 of the compiled JQL;
4. the compiled JQL goes to the parse endpoint with strict validation (``probe._parse_errors``):
   the provider answers a semantic error with an empty 200 on the search itself, so this check is
   mandatory and runs before any search or count request, also with a cursor;
5. ``--count`` calls the approximate-count endpoint;
6. pages of ``min(remaining, 100)`` with the explicit field list of ``normalize.plan_request``,
   at most 5 page requests and 500 tickets; the old search endpoint is never used.

Failure policy: the result is printed only when the whole run succeeded. A failure on any page
(also after earlier pages) is that failure's error and prints no tickets: a caller re-runs, with
``--cursor`` when it kept one from an earlier run. Provider answers that would make the loop unsafe
(a page larger than asked, a repeated or unusable ``nextPageToken``, a missing ``issues`` list) are
exit 12 ``provider_changed``. A page with no issues but a token is followed and counts as a
request.

Unverified against a real site (the fake provider is the only evidence): that ``POST
/rest/api/3/search/jql`` answers ``{"issues": [...], "nextPageToken": "..."}`` with the token
present only when more results exist, that it honors ``maxResults`` up to 100 with an explicit
field list, answers 400 for a rejected ``nextPageToken``, that ``POST
/rest/api/3/search/approximate-count`` answers ``{"count": N}`` and accepts a query with an
``ORDER BY`` tail, and that a page token stays valid for the time of a paging run.
"""

from __future__ import annotations

import argparse
import re
from collections.abc import Sequence
from dataclasses import dataclass, replace
from typing import Any

from . import fieldload, fieldscmd, identity, probe, selection
from .config import LOGICAL_FIELDS
from .errors import TicketctlError, usage_error
from .fieldmap import SiteFields, Status
from .flags import Globals
from .gateway import Gateway, Response
from .normalize import (
    Context,
    normalize_issues,
    parse_properties,
    plan_request,
)
from .runtime import Runtime, for_site, load_runtime, open_gateway
from .sanitize import read_input
from .search_compile import SearchFlags, compile_search, lookup_named_query
from .search_cursor import Cursor, check, decode, encode, jql_digest, valid_token

SEARCH = "/rest/api/3/search/jql"
COUNT = "/rest/api/3/search/approximate-count"
DEFAULT_LIMIT = 50
MAX_LIMIT = 500
PAGE_SIZE = 100
MAX_PAGES = 5
MAX_MESSAGES = 10
_DURATION = re.compile(r"[0-9]{1,4}[mhdw]")
_DATE = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}")
_CUSTOM_ID = re.compile(r"customfield_([0-9]{1,18})")

_SEQUENCES = (
    "project",
    "status",
    "status_category",
    "assignee",
    "sprint",
    "label",
    "parent",
    "type",
    "priority",
    "key",
    "not_status",
    "not_label",
    "not_parent",
    "not_type",
)


# --- offline rules -----------------------------------------------------------------------------


def _flags(a: argparse.Namespace, text: str | None) -> SearchFlags:
    values: dict[str, Any] = {name: tuple(getattr(a, name) or ()) for name in _SEQUENCES}
    return SearchFlags(
        **values,
        updated_since=a.updated_since,
        created_since=a.created_since,
        text=text,
        order_by=a.order_by,
    )


def _exactly_one(a: argparse.Namespace, first: str, second: str, label: str) -> None:
    if getattr(a, first) is not None and getattr(a, second) is not None:
        raise usage_error("invalid_combination", f"exactly one of {label} is allowed")


def _invalid_value(message: str) -> TicketctlError:
    return TicketctlError("invalid_request", message, reason="invalid_value")


def effective_limit(a: argparse.Namespace) -> int:
    """``--limit`` (default 50): 1 to 500, or 0 together with ``--count``."""
    limit = DEFAULT_LIMIT if a.limit is None else a.limit
    if limit > MAX_LIMIT:
        raise _invalid_value(f"--limit is at most {MAX_LIMIT}")
    if limit == 0 and not a.count:
        raise _invalid_value("--limit 0 is only allowed with --count")
    return limit


def check_offline(a: argparse.Namespace) -> None:
    """Every rule of ``search`` that needs neither the config nor the provider."""
    _exactly_one(a, "jql", "jql_file", "--jql and --jql-file")
    _exactly_one(a, "text", "text_file", "--text and --text-file")
    flags = _flags(a, "x" if a.text is not None or a.text_file is not None else None)
    raw = a.jql is not None or a.jql_file is not None
    if raw and (a.query is not None or flags.any_given()):
        raise usage_error(
            "invalid_combination",
            "a raw query (--jql, --jql-file) cannot be given with a structured flag, --text, "
            "--text-file, --order-by or --query",
        )
    if not raw and a.query is None and not replace(flags, order_by=None).any_given():
        raise usage_error("missing_option", "search needs a query or a filter flag")
    effective_limit(a)
    parse_properties(a.fields, command="search")


def _text_of(a: argparse.Namespace) -> str | None:
    if a.text_file is None:
        return a.text
    text = read_input(a.text_file, kind="text", body_chars=0)
    # Exactly one trailing newline: an editor's, not part of the phrase.
    if text.endswith("\r\n"):
        return text[:-2]
    return text[:-1] if text.endswith("\n") else text


def _raw_of(a: argparse.Namespace) -> str | None:
    if a.jql_file is not None:
        return read_input(a.jql_file, kind="jql", body_chars=0)
    return a.jql


# --- compilation inputs ------------------------------------------------------------------------


def _needs_zone(flags: SearchFlags) -> bool:
    """Whether a ``--*-since`` value is an RFC 3339 timestamp (a duration and a date need none)."""
    return any(
        v is not None and not _DURATION.fullmatch(v) and not _DATE.fullmatch(v)
        for v in (flags.updated_since, flags.created_since)
    )


def _order_clauses(site_fields: SiteFields) -> dict[str, str]:
    """Logical ``--order-by`` names mapped to a custom field's JQL clause name (``cf[N]``)."""
    out: dict[str, str] = {}
    for name in (*LOGICAL_FIELDS, *sorted(site_fields.config.extra)):
        res = site_fields.resolve(name)
        if res.status is not Status.FOUND or len(res.fields) != 1:
            continue
        match = _CUSTOM_ID.fullmatch(res.fields[0].id)
        if match:
            out[name] = f"cf[{match.group(1)}]"
    return out


# --- provider calls ----------------------------------------------------------------------------


def _messages(response: Response) -> list[str]:
    """The provider's own error text, sanitized, with field ids masked and capped."""
    data = response.data
    listed = data.get("errorMessages") if isinstance(data, dict) else None
    items = [m for m in listed if isinstance(m, str)] if isinstance(listed, list) else []
    if not items and response.text.strip():
        items = [response.text]
    texts = (probe._error_text(m) for m in items[:MAX_MESSAGES])
    return [t for t in texts if t.strip()]


def _invalid_query(prefix: str, messages: Sequence[str]) -> TicketctlError:
    detail = "; ".join(messages) if messages else "no detail"
    return TicketctlError(
        "invalid_request",
        f"{prefix}: {detail}",
        reason="invalid_query",
        details={"errorMessages": list(messages)} if messages else None,
    )


def _changed(what: str) -> TicketctlError:
    return TicketctlError("provider_changed", f"unexpected answer from {what}")


def parse_check(gw: Gateway, jql: str) -> None:
    """Exit 22 ``invalid_query`` when the strict parse endpoint finds any error in ``jql``."""
    errors = probe._parse_errors(gw, jql)
    if errors:
        raise _invalid_query("invalid query", [probe._error_text(e) for e in errors[:MAX_MESSAGES]])


def approximate_count(gw: Gateway, jql: str) -> int:
    response = gw.request("POST", COUNT, template=COUNT, body={"jql": jql}, write=False)
    data = response.data
    count = data.get("count") if isinstance(data, dict) else None
    if not 200 <= response.status < 300 or type(count) is not int or count < 0:
        raise _changed(COUNT)
    return count


@dataclass(frozen=True)
class Collected:
    issues: list[Any]
    next_token: str | None  # the provider token of the first page not fetched, else None


def _fetch_page(
    gw: Gateway, jql: str, fields: Sequence[str], size: int, token: str | None, user_token: bool
) -> tuple[list[Any], str | None]:
    body: dict[str, Any] = {"jql": jql, "maxResults": size, "fields": list(fields)}
    if token is not None:
        body["nextPageToken"] = token
    response = gw.request("POST", SEARCH, template=SEARCH, body=body, write=False)
    if response.status == 400:
        # The query passed the parse check, so a cursor's token is what the provider rejected.
        if user_token:
            raise TicketctlError(
                "invalid_request",
                "the provider rejected the cursor; restart without --cursor",
                reason="cursor_expired",
            )
        raise _invalid_query("the provider rejected the query", _messages(response))
    data = response.data
    issues = data.get("issues") if isinstance(data, dict) else None
    if not 200 <= response.status < 300 or not isinstance(issues, list):
        raise _changed(SEARCH)
    if len(issues) > size:
        raise TicketctlError(
            "provider_changed", f"{SEARCH} answered more issues than were asked for"
        )
    following = data.get("nextPageToken")
    if following is not None and not valid_token(following):
        raise TicketctlError("provider_changed", f"{SEARCH} answered an unusable page token")
    return issues, following


def collect(
    gw: Gateway, jql: str, fields: Sequence[str], limit: int, start: str | None
) -> Collected:
    """Page through the search: at most ``MAX_PAGES`` requests and ``limit`` issues."""
    issues: list[Any] = []
    token = start
    seen = {start} if start is not None else set()
    for _ in range(MAX_PAGES):
        size = min(limit - len(issues), PAGE_SIZE)
        page, following = _fetch_page(
            gw, jql, fields, size, token, token is not None and token == start
        )
        issues.extend(page)
        if following is None:
            return Collected(issues, None)
        if following in seen:
            raise TicketctlError("provider_changed", f"{SEARCH} repeated a page token")
        seen.add(following)
        token = following
        if len(issues) >= limit:
            break
    return Collected(issues, token)


# --- the command -------------------------------------------------------------------------------


def _context(rt: Runtime, name: str, gw: Gateway, site_fields: SiteFields, props: Any) -> Context:
    site = rt.config.sites[name]
    priorities = fieldload.load_priorities(rt, name, gw) if "priority" in props.names else None
    return Context(
        site=name,
        base_url=str(site.url),
        props=props,
        site_fields=site_fields,
        priorities=priorities,
        blocking_link_types=tuple(site.blocking_link_types),
    )


def _unique(tickets: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """The first of any repeated key (a provider that repeats an issue across pages)."""
    seen: set[str] = set()
    out: list[dict[str, Any]] = []
    for ticket in tickets:
        key = ticket["key"]
        if key not in seen:
            seen.add(key)
            out.append(ticket)
    return out


def search(g: Globals, a: argparse.Namespace) -> dict[str, Any]:
    props = parse_properties(a.fields, command="search")
    limit = effective_limit(a)
    cursor: Cursor | None = decode(a.cursor) if a.cursor is not None else None
    text = _text_of(a)
    raw = _raw_of(a)
    flags = _flags(a, text)
    rt = load_runtime(g)
    name = selection.select_site(
        rt.config, site_flag=g.site, refs=a.key or [], projects=a.project or []
    ).site
    site = rt.config.sites[name]
    with for_site(name):
        gw = open_gateway(rt, name)
        site_fields: SiteFields | None = None
        clauses = None
        if flags.order_by is not None:
            site_fields = fieldload.load_site_fields(
                rt, name, gw, needs=fieldscmd.needs_of(rt, name)
            )
            clauses = _order_clauses(site_fields)
        zone = None
        if _needs_zone(flags):
            zone = identity.load_me(rt, name, gw, refresh=False).time_zone
        named = lookup_named_query(site.queries, a.query, name) if a.query is not None else None
        jql = compile_search(flags, query=named, raw_jql=raw, time_zone=zone, order_clauses=clauses)
        digest = jql_digest(jql)
        if cursor is not None:
            check(cursor, name, digest)
        parse_check(gw, jql)
        count = approximate_count(gw, jql) if a.count else None
        if limit == 0:
            assert count is not None
            return _result([], count > 0, None, count)
        if site_fields is None:
            site_fields = fieldload.load_site_fields(
                rt, name, gw, needs=fieldscmd.needs_of(rt, name)
            )
        ctx = _context(rt, name, gw, site_fields, props)
        found = collect(
            gw,
            jql,
            plan_request(props, site_fields).fields,
            limit,
            cursor.page_token if cursor is not None else None,
        )
    page = normalize_issues(found.issues, ctx)
    for warning in page.warnings:
        rt.warn(warning)
    if page.skipped:
        rt.warn(f"warning: {page.skipped} result(s) had no usable key and were skipped")
    following = encode(name, digest, found.next_token) if found.next_token is not None else None
    return _result(_unique(page.tickets), found.next_token is not None, following, count)


def _result(
    tickets: list[dict[str, Any]], truncated: bool, following: str | None, count: int | None
) -> dict[str, Any]:
    result: dict[str, Any] = {"tickets": tickets, "truncated": truncated, "next": following}
    if count is not None:
        result["count"] = count
    return result


def human_search(r: dict[str, Any]) -> str:
    lines = []
    for t in r["tickets"]:
        status = t.get("status")
        parts = [t["key"]]
        if isinstance(status, dict) and status.get("name"):
            parts.append(f"[{status['name']}]")
        if t.get("title"):
            parts.append(str(t["title"]))
        if t.get("incomplete"):
            parts.append("(incomplete: " + ", ".join(t["incomplete"]) + ")")
        lines.append(" ".join(parts))
    if not r["tickets"]:
        lines.append("no tickets")
    if "count" in r:
        lines.append(f"about {r['count']} match")
    if r["next"] is not None:
        lines.append(f"more results: --cursor {r['next']}")
    elif r["truncated"]:
        lines.append("more results exist than were returned")
    return "\n".join(lines)


__all__ = ["check_offline", "human_search", "search"]
