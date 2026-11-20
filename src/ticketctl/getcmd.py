"""Handler of ``get KEY...`` (docs/interface.md, ``get KEY...``; ``schemas/cli/get.json``).

The flow: validate every key (grammar, browse URLs, length), drop duplicates, cap at 50 distinct
keys, parse ``--fields`` and ``--comments`` (all offline, so a usage error never costs a request),
pick the one site, then load the field list (and the priority list and the account only when the
requested output needs them), build the provider field list with ``normalize.plan_request`` and
read every key with ``GET /issue/{key}?fields=...`` at concurrency 4. Every payload goes through
``normalize_issue``; the order of the input is restored before printing.

Per-key failures (a 404, a provider answer that is not usable, a transport failure of that read)
become ``{key, error}`` entries. With at least one ticket read the command exits 0 and a consumer
must read ``errors``; with none read it raises the first error (in input order) carrying the whole
per-key list in ``details.errors``, except for a single key, which keeps the plain envelope.

``--comments N`` reads ``GET /issue/{key}/comment?orderBy=-created&maxResults=N&expand=properties``
once per ticket, never the comments embedded in the issue (the search embeds only the first 20).

Unverified against a real site: that ``GET /issue/{key}`` answers the requested ``fields`` only and
resolves a moved ticket's old key to the new one, that a comment page honors ``orderBy=-created``
and ``maxResults``, and that ``expand=properties`` embeds the comment properties.
"""

from __future__ import annotations

import argparse
import json
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from typing import Any

from . import fieldload, identity, selection
from .comment_object import MAX_COMMENTS, comment_list
from .errors import TicketctlError, from_exception
from .flags import Globals
from .gateway import Gateway
from .normalize import (
    Context,
    Properties,
    normalize_issue,
    parse_properties,
    plan_request,
)
from .runtime import Runtime, for_site, load_runtime, open_gateway
from .sanitize import query, segment

MAX_KEYS = 50
CONCURRENCY = 4
ISSUE = "/rest/api/3/issue"
ISSUE_TEMPLATE = "/rest/api/3/issue/{key}"
COMMENT_TEMPLATE = "/rest/api/3/issue/{key}/comment"
# The logical field names (config keys) behind the properties that read a mapped field.
_LOGICAL = {
    "estimate": "estimate",
    "acceptance_criteria": "acceptance_criteria",
    "sprints": "sprint",
    "labels": "labels",
    "priority": "priority",
    "assignee": "assignee",
    "resolution": "resolution",
    "due_date": "due_date",
    "parent": "parent",
    "epic": "epic",
}


def _invalid(message: str) -> TicketctlError:
    return TicketctlError("invalid_request", message, reason="invalid_value")


def distinct_keys(rt: Runtime, g: Globals, refs: list[str]) -> list[tuple[str, str]]:
    """``(key, the reference that named it)`` for each distinct key, in input order.

    Every reference is validated first (key grammar and length, browse URL rules, ``--site``), so
    a bad key anywhere is exit 22 ``invalid_key`` before the cap is judged.
    """
    seen: dict[str, str] = {}
    for ref in refs:
        key = selection.select_site(rt.config, site_flag=g.site, refs=[ref]).key
        assert key is not None
        seen.setdefault(key, ref)
    if len(seen) > MAX_KEYS:
        raise _invalid(f"get reads at most {MAX_KEYS} distinct keys")
    return list(seen.items())


def comment_count(value: int | None) -> int:
    if value is None:
        return 0
    if not 1 <= value <= MAX_COMMENTS:
        raise _invalid(f"--comments takes a number from 1 to {MAX_COMMENTS}")
    return value


def _needs(props: Properties, rt: Runtime, site: str) -> list[str]:
    names = [_LOGICAL[p] for p in props.names if p in _LOGICAL]
    if "epic" in props.names:
        names.append("parent")
    if "fields" in props.names:
        names += sorted(rt.config.sites[site].fields.extra)
    return names


def _object_error(status: int) -> TicketctlError:
    """The envelope of a non-2xx read of an object (the gateway has raised 401, 403, 429, 5xx)."""
    if status == 404:
        return TicketctlError(
            "not_found", "the ticket is missing or you have no permission to see it"
        )
    return TicketctlError("provider_changed", f"unexpected answer from the provider ({status})")


@dataclass(frozen=True)
class _Read:
    ticket: dict[str, Any] | None = None
    warnings: tuple[str, ...] = ()
    error: TicketctlError | None = None


class _Reader:
    def __init__(
        self,
        gw: Gateway,
        ctx: Context,
        fields: tuple[str, ...],
        comments: int,
        me_account_id: str | None,
    ) -> None:
        self.gw = gw
        self.ctx = ctx
        self.fields = fields
        self.comments = comments
        self.me = me_account_id

    def one(self, key: str) -> _Read:
        try:
            return self._read(key)
        except Exception as exc:  # noqa: BLE001 - only the modules' own errors are per-key
            err = from_exception(exc)
            if err is None:
                raise
            err.site = err.site or self.ctx.site
            err.key = err.key or key
            return _Read(error=err)

    def _read(self, key: str) -> _Read:
        path = f"{ISSUE}/{segment('ticket_key', key)}"
        response = self.gw.request(
            "GET",
            path,
            template=ISSUE_TEMPLATE,
            query=query({"fields": ",".join(self.fields)}),
        )
        if not 200 <= response.status < 300:
            raise _object_error(response.status)
        out = normalize_issue(response.data, self.ctx, requested_key=key)
        if out is None:
            raise TicketctlError("provider_changed", "the provider answered no usable ticket")
        ticket = out.ticket
        if self.comments and "ticket" not in ticket.get("incomplete", ()):
            self._comments(ticket)
        return _Read(ticket=ticket, warnings=out.warnings)

    def _comments(self, ticket: dict[str, Any]) -> None:
        canonical = ticket["key"]
        response = self.gw.request(
            "GET",
            f"{ISSUE}/{segment('ticket_key', canonical)}/comment",
            template=COMMENT_TEMPLATE,
            query=query(
                {
                    "orderBy": "-created",
                    "startAt": 0,
                    "maxResults": self.comments,
                    "expand": "properties",
                }
            ),
        )
        if not 200 <= response.status < 300:
            raise _object_error(response.status)
        page = comment_list(response.data, self.comments, self.me)
        ticket["comments"] = page.value
        if page.incomplete or "incomplete" in ticket:
            ticket["incomplete"] = sorted({*ticket.get("incomplete", ()), *page.incomplete})


def _aggregate(errors: list[tuple[str, TicketctlError]]) -> TicketctlError:
    """The command's error when no key was read: the first one, with the whole list."""
    first = errors[0][1]
    if len(errors) == 1:
        return first
    details = dict(first.details or {})
    details["errors"] = [{"key": key, "error": err.body()} for key, err in errors]
    return TicketctlError(
        first.code,
        first.message,
        reason=first.reason,
        details=details,
        site=first.site,
        key=first.key,
        retryable=first.retryable,
        retry_after_s=first.retry_after_s,
        outcome_unknown=first.outcome_unknown,
    )


def get(g: Globals, a: argparse.Namespace) -> dict[str, Any]:
    rt = load_runtime(g)
    refs: list[str] = list(a.keys)
    keys = distinct_keys(rt, g, refs)
    comments = comment_count(a.comments)
    props = parse_properties(a.fields, command="get", comments=comments, include_adf=a.include_adf)
    site = selection.select_site(rt.config, site_flag=g.site, refs=[ref for _, ref in keys]).site
    with for_site(site):
        gw = open_gateway(rt, site)
        site_fields = fieldload.load_site_fields(rt, site, gw, needs=_needs(props, rt, site))
        priorities = fieldload.load_priorities(rt, site, gw) if "priority" in props.names else None
        me = (
            identity.load_me(rt, site, gw, refresh=False).account_id
            if "comments" in props.names
            else None
        )
        config = rt.config.sites[site]
        ctx = Context(
            site=site,
            base_url=str(config.url),
            props=props,
            site_fields=site_fields,
            priorities=priorities,
            blocking_link_types=tuple(config.blocking_link_types),
        )
        reader = _Reader(
            gw,
            ctx,
            plan_request(props, site_fields).fields,
            comments if "comments" in props.names else 0,
            me,
        )
        names = [key for key, _ in keys]
        if len(names) == 1:
            reads = [reader.one(names[0])]
        else:
            with ThreadPoolExecutor(max_workers=CONCURRENCY) as pool:
                reads = list(pool.map(reader.one, names))
    warnings: dict[str, None] = {}
    tickets: list[dict[str, Any]] = []
    failed: list[tuple[str, TicketctlError]] = []
    for key, read in zip(names, reads, strict=True):
        for warning in read.warnings:
            warnings[warning] = None
        if read.ticket is not None:
            tickets.append(read.ticket)
        else:
            assert read.error is not None
            failed.append((key, read.error))
    for warning in warnings:
        rt.warn(warning)
    if not tickets:
        raise _aggregate(failed)
    return {
        "tickets": tickets,
        "errors": [{"key": key, "error": err.body()} for key, err in failed],
    }


def _value(value: Any) -> str:
    if isinstance(value, str):
        return value
    return json.dumps(value, ensure_ascii=True, sort_keys=True)


def human_get(r: dict[str, Any]) -> str:
    lines: list[str] = []
    for t in r["tickets"]:
        moved = f" (requested {t['requested_key']})" if "requested_key" in t else ""
        lines.append(f"{t['key']}{moved}: {t.get('title', '(no title)')}")
        for name, value in t.items():
            if name in ("key", "site", "requested_key", "title"):
                continue
            text = _value(value)
            if "\n" in text:
                lines.append(f"  {name}:")
                lines += [f"    {line}" for line in text.split("\n")]
            else:
                lines.append(f"  {name}: {text}")
    for e in r["errors"]:
        body = e["error"]
        lines.append(f"error {e['key']}: {body['code']}: {body['message']}")
    return "\n".join(lines)
