"""Normalize a provider issue into the ticket object (docs/interface.md, Ticket object).

Pure logic, no I/O. The callers (``getcmd`` and ``searchcmd``) load the field list
(``fieldload.load_site_fields``) and the priority list (``fieldload.load_priorities``), fetch the
issue with the explicit field list of :func:`plan_request`, and hand the payload to
:func:`normalize_issue` or :func:`normalize_issues`.

Public API:

``parse_properties(raw, command=, comments=0, include_adf=False) -> Properties``
    The ``--fields`` vocabulary. ``raw`` is the comma list (None: the command's default). An unknown
    name is exit 22 ``unknown_field``; an empty entry, ``comments`` without ``--comments N`` and
    ``description_adf`` without ``--include-adf`` are exit 22 ``invalid_value``. ``key``, ``site``
    and ``requested_key`` are accepted and always handled.

``plan_request(props, site_fields) -> ProviderRequest``
    The provider field ids to fetch: exactly the ones the properties need (``blocked`` pulls
    ``issuelinks``, ``epic`` pulls ``parent`` and the issue type), always an explicit non-empty
    list (never ``*all``), so unrequested properties are neither fetched nor printed.

``normalize_issue(raw, ctx, requested_key=None) -> Normalized | None``
    One payload. It never raises: a property that cannot be read is null (or an empty list, or
    omitted where the schema has no null: ``id``, ``type``, ``title``, ``status``, ``url`` and
    an unreadable ``description_adf``) and named in ``incomplete``; a payload with no usable
    ``fields`` (or that is not an object) is a stub (``key``, ``site``, ``incomplete:
    ["ticket"]``, plus ``requested_key`` when it differs); a payload with no usable key at all
    (and no ``requested_key``) is None. ``normalize_issues``
    maps a page and counts the skipped ones.

Rules of this module that the interface leaves open (see docs/interface.md, Ticket object):

* ``incomplete`` is printed when requested (an empty list is then printed) and also whenever it is
  not empty, so a cut-off is never hidden by a narrow ``--fields``.
* A logical field that is ambiguous or mapped to a field the list lacks is null and named, with a
  warning; one that is neither mapped nor discoverable is null and named only when the caller asked
  for it by name (``--fields``), never in a default read.
* A list the provider did not send at all (``labels``, ``links``, ``attachments``) is unknown, not
  empty; a missing scalar is null.
* ``comments`` is not built here: the comment page is its own call that ``get`` attaches through
  ``comment_object`` (``comment_list``); the ``comments`` command (``commentscmd``) has its own
  paging, ``--since``, ``--own`` and ordering.

Unverified against a real site: that ``hierarchyLevel`` is embedded in a parent's ``issuetype`` and
that the epic level is 1, that an issue link names the other end under ``inwardIssue`` and
``outwardIssue`` with ``fields.status.statusCategory.key``, that an issue omits (rather than nulls)
a field that is not on its screen, that a sprint field value carries ``id``, ``name``, ``state``,
``startDate`` and ``endDate``, that the epic-link field holds the epic's key as a string, and that
``resolutiondate`` and ``attachment`` are valid names in a ``fields`` request.
"""

from __future__ import annotations

import math
import re
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field
from typing import Any, Literal

from .adf_render import render_adf
from .errors import TicketctlError
from .fieldconv import MAX_INT, convert
from .fieldload import NameList
from .fieldmap import SYSTEM_IDS, FieldSchema, Resolution, SiteFields, Status
from .sanitize import CAPS, GRAMMARS, cap_text, sanitize_field, sanitize_line, scrub
from .timestamps import normalize_date, normalize_ts

# The table of docs/interface.md, in its order (the order of the printed object).
PROPERTIES = (
    "url",
    "id",
    "type",
    "title",
    "description",
    "description_adf",
    "status",
    "resolution",
    "assignee",
    "reporter",
    "priority",
    "estimate",
    "acceptance_criteria",
    "labels",
    "sprints",
    "parent",
    "epic",
    "links",
    "blocked",
    "due_date",
    "created",
    "updated",
    "resolved",
    "comments",
    "attachments",
    "fields",
    "incomplete",
)
ALWAYS = ("key", "site", "requested_key")
GET_DEFAULT = tuple(p for p in PROPERTIES if p not in ("comments", "description_adf"))
_SEARCH = frozenset(
    {
        "type", "title", "status", "assignee", "priority", "estimate", "sprints", "parent",
        "labels", "updated",
    }
)  # fmt: skip
SEARCH_DEFAULT = tuple(p for p in PROPERTIES if p in _SEARCH)
MAX_NAMES = 100
MAX_LINKS = 500
MAX_LABELS = 1000
MAX_SPRINTS = 200
MAX_ATTACHMENTS = 500
ADF_NODE_LIMIT = 500_000
NAME_CAP = CAPS["name"]
SUMMARY_CAP = CAPS["summary"]
BODY_CAP = CAPS["body"]
DEDUPE_LABEL = re.compile(r"tc-[0-9a-f]{16}")
STATUS_CATEGORIES = ("new", "indeterminate", "done")
SPRINT_STATES = ("future", "active", "closed")
EPIC_LEVEL = 1
UNKNOWN_USER = "unknown user"
UNKNOWN_TYPE = "unknown"
FALLBACK_FIELD = "summary"
_NUMERIC = re.compile(r"[0-9]{1,18}")
_OMIT = object()


# --- the --fields vocabulary ---------------------------------------------------------------


@dataclass(frozen=True)
class Properties:
    names: tuple[str, ...]
    explicit: frozenset[str] = frozenset()  # the names given by --fields (empty: a default read)


def _shown(name: str) -> str:
    return cap_text(sanitize_line(name), 64)[0]


def _invalid(reason: str, message: str, **details: Any) -> TicketctlError:
    return TicketctlError("invalid_request", message, reason=reason, details=details or None)


def parse_properties(
    raw: str | None,
    *,
    command: Literal["get", "search"],
    comments: int = 0,
    include_adf: bool = False,
) -> Properties:
    if raw is None:
        names = GET_DEFAULT if command == "get" else SEARCH_DEFAULT
        wanted = set(names)
        if command == "get" and comments > 0:
            wanted.add("comments")
        if command == "get" and include_adf:
            wanted.add("description_adf")
        return Properties(tuple(p for p in PROPERTIES if p in wanted))
    items = raw.split(",")
    if len(items) > MAX_NAMES:
        raise _invalid("invalid_value", f"--fields names more than {MAX_NAMES} properties")
    chosen: set[str] = set()
    for item in items:
        if item == "":
            raise _invalid("invalid_value", "--fields has an empty name")
        if item in ALWAYS:
            continue
        if item not in PROPERTIES:
            raise _invalid(
                "unknown_field",
                f"unknown --fields name: {_shown(item)}",
                field=_shown(item),
            )
        chosen.add(item)
    if "comments" in chosen and comments <= 0:
        raise _invalid("invalid_value", "--fields comments needs --comments N")
    if "description_adf" in chosen and not include_adf:
        raise _invalid("invalid_value", "--fields description_adf needs --include-adf")
    return Properties(tuple(p for p in PROPERTIES if p in chosen), frozenset(chosen))


# --- the provider request ------------------------------------------------------------------


@dataclass(frozen=True)
class ProviderRequest:
    fields: tuple[str, ...]


@dataclass(frozen=True)
class _Source:
    """Where a logical field lives: a provider field id (or None) and why not."""

    id: str | None
    status: Status
    resolution: Resolution | None = None


def _source(sf: SiteFields, logical: str) -> _Source:
    res = sf.resolve(logical)
    if res.status is Status.FOUND:
        return _Source(res.fields[0].id, Status.FOUND, res)
    if res.status is Status.ABSENT and logical in SYSTEM_IDS:
        return _Source(SYSTEM_IDS[logical], Status.FOUND, res)  # a system field always exists
    return _Source(None, res.status, res)


def plan_request(props: Properties, site_fields: SiteFields) -> ProviderRequest:
    ids: list[str] = []

    def add(*values: str | None) -> None:
        for value in values:
            if value is not None and value not in ids:
                ids.append(value)

    for name in props.names:
        if name == "title":
            add("summary")
        elif name in ("description", "description_adf"):
            add("description")
        elif name == "type":
            add("issuetype")
        elif name in ("status", "reporter", "created", "updated"):
            add(name)
        elif name == "resolved":
            add("resolutiondate")
        elif name == "attachments":
            add("attachment")
        elif name in ("links", "blocked"):
            add("issuelinks")
        elif name == "epic":
            add(
                _source(site_fields, "parent").id,
                "issuetype",
                _source(site_fields, "epic").id,
            )
        elif name == "sprints":
            add(_source(site_fields, "sprint").id)
        elif name == "fields":
            for extra in sorted(site_fields.config.extra):
                res = site_fields.resolve(extra)
                if res.status is Status.FOUND:
                    add(res.fields[0].id)
        elif name in (
            "resolution",
            "assignee",
            "priority",
            "estimate",
            "acceptance_criteria",
            "labels",
            "parent",
            "due_date",
        ):
            add(_source(site_fields, name).id)
    return ProviderRequest(tuple(ids) if ids else (FALLBACK_FIELD,))


# --- one issue -----------------------------------------------------------------------------


@dataclass(frozen=True)
class Context:
    site: str
    base_url: str
    props: Properties
    site_fields: SiteFields
    priorities: NameList | None = None
    blocking_link_types: Sequence[str] = ("Blocks",)


@dataclass(frozen=True)
class Normalized:
    ticket: dict[str, Any]
    warnings: tuple[str, ...] = ()


@dataclass(frozen=True)
class NormalizedPage:
    tickets: list[dict[str, Any]]
    warnings: tuple[str, ...]
    skipped: int


@dataclass
class _State:
    ctx: Context
    raw: dict[str, Any]
    fields: dict[str, Any]
    incomplete: set[str] = field(default_factory=set)
    warnings: list[str] = field(default_factory=list)
    cache: dict[str, Any] = field(default_factory=dict)

    def mark(self, part: str) -> None:
        self.incomplete.add(part)

    def unavailable(self, part: str, src: _Source, logical: str | None = None) -> None:
        """A property whose logical field cannot be read: named when ambiguous or mapped to nothing.

        ``part`` is the property name (what ``incomplete`` and ``--fields`` use); ``logical`` is
        the config key when it differs (``sprints`` reads the ``sprint`` field).
        """
        logical = logical or part
        res = src.resolution
        site = self.ctx.site
        if src.status is Status.AMBIGUOUS:
            count = len(res.fields) if res is not None else 0
            self.mark(part)
            self.warnings.append(
                f"warning: {logical} matches {count} fields; map one under "
                f"sites.{site}.fields.{logical} (by name, id or schema)"
            )
        elif src.status is Status.MISSING:
            self.mark(part)
            self.warnings.append(
                f"warning: the field mapped for {logical} is not in the provider's field list "
                f"(sites.{site}.fields.{logical})"
            )
        elif part in self.ctx.props.explicit:
            self.mark(part)  # not mapped, not discoverable, and the caller asked for it by name


def _name(st: _State, raw: object, part: str) -> str | None:
    if not isinstance(raw, str) or not raw:
        return None
    out, cut = sanitize_field(raw, cap=NAME_CAP)
    if cut:
        st.mark(f"{part}.truncated")
    return out


def _text(st: _State, raw: str, cap: int, part: str, *, body: bool = False) -> str:
    out, cut = sanitize_field(raw, cap=cap, body=body)
    if cut:
        st.mark(f"{part}.truncated")
    return out


def _convert(
    st: _State, schema: FieldSchema | None, raw: object, part: str, *, body: bool = False
) -> Any:
    done = convert(schema, raw, part, body=body)
    for mark in done.incomplete:
        # fieldconv names a renderer budget hit with the bare part; with a value it is a cut.
        st.mark(f"{part}.truncated" if mark == part and done.value is not None else mark)
    return done.value


# --- property builders ---------------------------------------------------------------------


def _prop_url(st: _State) -> Any:
    return f"{st.ctx.base_url.rstrip('/')}/browse/{st.raw['key']}"


def _prop_id(st: _State) -> Any:
    raw = st.raw.get("id")
    if not GRAMMARS["provider_id"].matches(raw):
        st.mark("id.invalid_id" if raw is not None else "id")
        return _OMIT
    return raw


def _prop_type(st: _State) -> Any:
    raw = st.fields.get("issuetype")
    name = _name(st, raw.get("name"), "type") if isinstance(raw, dict) else None
    if name is None or not isinstance(raw, dict) or not isinstance(raw.get("subtask"), bool):
        st.mark("type")
        return _OMIT
    return {"name": name, "subtask": raw["subtask"]}


def _prop_title(st: _State) -> Any:
    raw = st.fields.get("summary")
    if not isinstance(raw, str):
        st.mark("title")
        return _OMIT
    return _text(st, raw, CAPS["title"], "title")


def _prop_description(st: _State) -> Any:
    raw = st.fields.get("description")
    if raw is None:
        return None
    if isinstance(raw, dict):
        rendered = render_adf(raw, "description")
        text, cut = cap_text(scrub(rendered.text), BODY_CAP)
        if cut or rendered.incomplete:
            st.mark("description.truncated")
        return text
    if isinstance(raw, str):
        return _text(st, raw, BODY_CAP, "description", body=True)
    st.mark("description")
    return None


def _clean_json(root: object) -> tuple[Any, set[str]]:
    """A copy of an opaque JSON value, its key order kept, and the parts it could not keep.

    Every string is sanitized (body class), scrubbed and cut at the body cap; a non-finite number
    and an integer beyond ``fieldconv.MAX_INT`` become null. Iterative (no recursion limit), with
    a node budget: the value is ``_OMIT`` when the budget is exceeded (a cyclic or heavily shared
    input ends there). The set holds ``truncated`` (a string was cut), ``lossy`` (a number was
    replaced or two keys collapsed into one).
    """
    budget = ADF_NODE_LIMIT
    marks: set[str] = set()
    out: list[Any] = [None]
    stack: list[tuple[Any, Any, Any]] = [(root, out, 0)]
    while stack:
        value, holder, slot = stack.pop()
        budget -= 1
        if budget < 0:
            return _OMIT, marks
        if isinstance(value, dict):
            new_dict: dict[str, Any] = {}
            holder[slot] = new_dict
            pending: list[tuple[Any, str]] = []
            for k, v in value.items():
                key, cut = sanitize_field(k if isinstance(k, str) else str(k), cap=NAME_CAP)
                if cut:
                    marks.add("truncated")
                if key in new_dict:
                    marks.add("lossy")
                    continue
                new_dict[key] = None  # the slot, so the order of the source is kept
                pending.append((v, key))
            stack.extend((v, new_dict, key) for v, key in reversed(pending))
        elif isinstance(value, list):
            new_list: list[Any] = [None] * len(value)
            holder[slot] = new_list
            stack.extend((v, new_list, i) for i, v in reversed(list(enumerate(value))))
        elif isinstance(value, str):
            holder[slot], cut = sanitize_field(value, cap=BODY_CAP, body=True)
            if cut:
                marks.add("truncated")
        elif isinstance(value, float) and not math.isfinite(value):
            holder[slot] = None
            marks.add("lossy")
        elif isinstance(value, int) and not isinstance(value, bool) and abs(value) > MAX_INT:
            holder[slot] = None
            marks.add("lossy")
        else:
            holder[slot] = value
    return out[0], marks


def _prop_description_adf(st: _State) -> Any:
    raw = st.fields.get("description")
    if raw is None:
        return None
    if not isinstance(raw, dict):
        st.mark("description_adf")
        return _OMIT
    cleaned, marks = _clean_json(raw)
    if cleaned is _OMIT:
        st.mark("description_adf.truncated")
        return _OMIT
    if "truncated" in marks:
        st.mark("description_adf.truncated")
    if "lossy" in marks:
        st.mark("description_adf")
    return cleaned


def _prop_status(st: _State) -> Any:
    raw = st.fields.get("status")
    name = _name(st, raw.get("name"), "status") if isinstance(raw, dict) else None
    category = raw.get("statusCategory") if isinstance(raw, dict) else None
    key = category.get("key") if isinstance(category, dict) else None
    if name is None or key not in STATUS_CATEGORIES:
        st.mark("status")
        return _OMIT
    return {"name": name, "category": key}


def _prop_resolution(st: _State) -> Any:
    src = _source(st.ctx.site_fields, "resolution")
    if src.id is None:
        st.unavailable("resolution", src)
        return None
    raw = st.fields.get(src.id)
    if raw is None:
        return None
    name = _name(st, raw.get("name"), "resolution") if isinstance(raw, dict) else None
    if name is None:
        st.mark("resolution")
    return name


def _account(st: _State, raw: object, part: str) -> Any:
    if raw is None:
        return None
    if not isinstance(raw, dict):
        st.mark(part)
        return None
    account = raw.get("accountId")  # never emailAddress
    if not GRAMMARS["account_id"].matches(account):
        if account is not None:
            st.mark(f"{part}.invalid_id")
        return {"account_id": None, "display_name": UNKNOWN_USER}
    name = raw.get("displayName")
    shown = UNKNOWN_USER
    if isinstance(name, str) and name:
        shown = _text(st, name, NAME_CAP, part)
    return {"account_id": account, "display_name": shown}


def _prop_assignee(st: _State) -> Any:
    src = _source(st.ctx.site_fields, "assignee")
    if src.id is None:
        st.unavailable("assignee", src)
        return None
    return _account(st, st.fields.get(src.id), "assignee")


def _prop_reporter(st: _State) -> Any:
    return _account(st, st.fields.get("reporter"), "reporter")


def _prop_priority(st: _State) -> Any:
    src = _source(st.ctx.site_fields, "priority")
    if src.id is None:
        st.unavailable("priority", src)
        return None
    raw = st.fields.get(src.id)
    if raw is None:
        return None
    name = _name(st, raw.get("name"), "priority") if isinstance(raw, dict) else None
    if name is None:
        st.mark("priority")
        return None
    rank = st.ctx.priorities.rank(name) if st.ctx.priorities is not None else None
    return {"name": name, "rank": rank}


def _prop_estimate(st: _State) -> Any:
    src = _source(st.ctx.site_fields, "estimate")
    if src.id is None:
        st.unavailable("estimate", src)
        return None
    return _convert(st, FieldSchema("number"), st.fields.get(src.id), "estimate")


def _prop_acceptance_criteria(st: _State) -> Any:
    src = _source(st.ctx.site_fields, "acceptance_criteria")
    if src.id is None or src.resolution is None or not src.resolution.fields:
        st.unavailable("acceptance_criteria", src)
        return None
    schema = src.resolution.fields[0].schema
    value = _convert(st, schema, st.fields.get(src.id), "acceptance_criteria", body=True)
    if value is not None and not isinstance(value, str):
        st.mark("acceptance_criteria")
        return None
    return value


def _prop_labels(st: _State) -> Any:
    src = _source(st.ctx.site_fields, "labels")
    if src.id is None:
        st.unavailable("labels", src)
        return []
    raw = st.fields.get(src.id)
    if not isinstance(raw, list):
        st.mark("labels")
        return []
    out: list[str] = []
    for item in raw[:MAX_LABELS]:
        if not isinstance(item, str):
            st.mark("labels")
        elif DEDUPE_LABEL.fullmatch(item) is None:
            out.append(_text(st, item, CAPS["label"], "labels"))
    if len(raw) > MAX_LABELS:
        st.mark("labels")
    return out


def _sprint_ts(st: _State, raw: object) -> str | None:
    if raw is None:
        return None
    out = normalize_ts(raw)
    if out is None:
        st.mark("sprints")
    return out


def _one_sprint(st: _State, raw: object) -> dict[str, Any] | None:
    if not isinstance(raw, dict):
        st.mark("sprints")
        return None
    sid = raw.get("id")
    if isinstance(sid, str) and _NUMERIC.fullmatch(sid):
        sid = int(sid)
    if isinstance(sid, bool) or not isinstance(sid, int) or not 0 <= sid < 10**18:
        st.mark("sprints.invalid_id")
        return None
    state = raw.get("state")
    state = state.lower() if isinstance(state, str) else None
    name = raw.get("name")
    if state not in SPRINT_STATES or not isinstance(name, str):
        st.mark("sprints")
        return None
    return {
        "id": sid,
        "name": _text(st, name, NAME_CAP, "sprints"),
        "state": state,
        "start": _sprint_ts(st, raw.get("startDate")),
        "end": _sprint_ts(st, raw.get("endDate")),
    }


def _prop_sprints(st: _State) -> Any:
    src = _source(st.ctx.site_fields, "sprint")
    if src.id is None:
        st.unavailable("sprints", src, "sprint")
        return []
    raw = st.fields.get(src.id)
    if raw is None:
        return []
    if not isinstance(raw, list):
        st.mark("sprints")
        return []
    if len(raw) > MAX_SPRINTS:
        st.mark("sprints")
    return [s for s in (_one_sprint(st, item) for item in raw[:MAX_SPRINTS]) if s is not None]


def _parent_object(st: _State, raw: object, part: str) -> dict[str, Any] | None:
    if not isinstance(raw, dict):
        st.mark(part)
        return None
    key = raw.get("key")
    if not GRAMMARS["ticket_key"].matches(key):
        st.mark(f"{part}.invalid_id")
        return None
    inner = raw.get("fields")
    inner = inner if isinstance(inner, dict) else {}
    itype = inner.get("issuetype")
    type_name = _name(st, itype.get("name"), part) if isinstance(itype, dict) else None
    summary = inner.get("summary")
    title = _text(st, summary, SUMMARY_CAP, part) if isinstance(summary, str) else None
    return {"key": key, "type": type_name or UNKNOWN_TYPE, "title": title}


def _prop_parent(st: _State) -> Any:
    src = _source(st.ctx.site_fields, "parent")
    if src.id is None:
        st.unavailable("parent", src)
        return None
    raw = st.fields.get(src.id)
    return None if raw is None else _parent_object(st, raw, "parent")


def _prop_epic(st: _State) -> Any:
    own = st.fields.get("issuetype")
    if isinstance(own, dict) and own.get("subtask") is True:
        return None  # a sub-task has no epic of its own and costs no extra read
    psrc = _source(st.ctx.site_fields, "parent")
    raw = st.fields.get(psrc.id) if psrc.id is not None else None
    unknown_level = False
    if isinstance(raw, dict):
        inner = raw.get("fields")
        itype = inner.get("issuetype") if isinstance(inner, dict) else None
        level = itype.get("hierarchyLevel") if isinstance(itype, dict) else None
        if isinstance(level, int) and not isinstance(level, bool):
            if level == EPIC_LEVEL:
                return _parent_object(st, raw, "epic")
        else:
            unknown_level = True
    elif raw is not None:
        unknown_level = True
    esrc = _source(st.ctx.site_fields, "epic")
    if esrc.id is not None:
        link = st.fields.get(esrc.id)
        if isinstance(link, str) and link:
            if GRAMMARS["ticket_key"].matches(link):
                return {"key": link, "type": "Epic", "title": None}
            st.mark("epic.invalid_id")
            return None
        if link is not None:
            st.mark("epic")
            return None
    elif esrc.status in (Status.AMBIGUOUS, Status.MISSING):
        st.unavailable("epic", esrc)
    if unknown_level:
        st.mark("epic")
    return None


def _links(st: _State) -> dict[str, Any]:
    """Parse ``issuelinks`` once: the links, whether the list is partial and the blocking keys."""
    if "links" in st.cache:
        return st.cache["links"]
    raw = st.fields.get("issuelinks")
    out: dict[str, Any] = {"links": [], "partial": False, "blocking": [], "unreadable": False}
    if not isinstance(raw, list):
        st.mark("links")
        out["partial"] = True
        st.cache["links"] = out
        return out
    if len(raw) > MAX_LINKS:
        st.mark("links")
        out["partial"] = True
    blocking = {b.casefold() for b in st.ctx.blocking_link_types}
    for entry in raw[:MAX_LINKS]:
        parsed = _one_link(st, entry)
        if parsed is None:
            out["partial"] = True
            continue
        link, raw_type = parsed
        out["links"].append(link)
        if link["direction"] == "inward" and raw_type.casefold() in blocking:
            if link["status_category"] != "done":
                if link["status_category"] is None:
                    out["unreadable"] = True
                if link["key"] not in out["blocking"]:
                    out["blocking"].append(link["key"])
    st.cache["links"] = out  # only a finished parse is cached: a failure is never half-read
    return out


def _one_link(st: _State, entry: object) -> tuple[dict[str, Any], str] | None:
    if not isinstance(entry, dict):
        st.mark("links")
        return None
    kind = entry.get("type")
    kind_name = kind.get("name") if isinstance(kind, dict) else None
    inward, outward = entry.get("inwardIssue"), entry.get("outwardIssue")
    if not isinstance(kind_name, str) or not kind_name or (inward is None) == (outward is None):
        st.mark("links")
        return None
    other = inward if inward is not None else outward
    if not isinstance(other, dict):
        st.mark("links")
        return None
    key = other.get("key")
    if not GRAMMARS["ticket_key"].matches(key):
        st.mark("links.invalid_id")
        return None
    inner = other.get("fields")
    inner = inner if isinstance(inner, dict) else {}
    summary = inner.get("summary")
    state = inner.get("status")
    category = state.get("statusCategory") if isinstance(state, dict) else None
    ckey = category.get("key") if isinstance(category, dict) else None
    return (
        {
            "type": _text(st, kind_name, NAME_CAP, "links"),
            "direction": "inward" if inward is not None else "outward",
            "key": key,
            "status_category": ckey if ckey in STATUS_CATEGORIES else None,
            "summary": _text(st, summary, SUMMARY_CAP, "links")
            if isinstance(summary, str)
            else None,
        },
        kind_name,
    )


def _prop_links(st: _State) -> Any:
    return _links(st)["links"]


def _prop_blocked(st: _State) -> Any:
    info = _links(st)
    if info["unreadable"]:
        st.mark("links.unreadable")
    by = list(info["blocking"])
    if by:
        return {"value": True, "by": by}  # true wins over a cut-off list
    return {"value": None if info["partial"] else False, "by": []}


def _date_like(st: _State, name: str, raw_key: str, parse: Callable[[object], str | None]) -> Any:
    raw = st.fields.get(raw_key)
    if raw is None:
        return None
    out = parse(raw)
    if out is None:
        st.mark(name)
    return out


def _prop_due_date(st: _State) -> Any:
    src = _source(st.ctx.site_fields, "due_date")
    if src.id is None:
        st.unavailable("due_date", src)
        return None
    return _date_like(st, "due_date", src.id, normalize_date)


def _prop_created(st: _State) -> Any:
    return _date_like(st, "created", "created", normalize_ts)


def _prop_updated(st: _State) -> Any:
    return _date_like(st, "updated", "updated", normalize_ts)


def _prop_resolved(st: _State) -> Any:
    return _date_like(st, "resolved", "resolutiondate", normalize_ts)


def _prop_comments(st: _State) -> Any:
    return _OMIT  # the comment page is its own provider call: getcmd attaches it


def _prop_attachments(st: _State) -> Any:
    raw = st.fields.get("attachment")
    if not isinstance(raw, list):
        st.mark("attachments")
        return []
    if len(raw) > MAX_ATTACHMENTS:
        st.mark("attachments")
    out = []
    for item in raw[:MAX_ATTACHMENTS]:
        if not isinstance(item, dict):
            st.mark("attachments")
            continue
        aid, name = item.get("id"), item.get("filename")
        size, mime = item.get("size"), item.get("mimeType")
        if not GRAMMARS["provider_id"].matches(aid):
            st.mark("attachments.invalid_id")
        elif (
            not isinstance(name, str)
            or not isinstance(mime, str)
            or isinstance(size, bool)
            or not isinstance(size, int)
            or size < 0
        ):
            st.mark("attachments")
        else:
            out.append(
                {
                    "id": aid,
                    "name": _text(st, name, CAPS["attachment_name"], "attachments"),
                    "size": size,
                    "mime": _text(st, mime, NAME_CAP, "attachments"),
                }
            )
    return out


def _prop_fields(st: _State) -> Any:
    sf = st.ctx.site_fields
    out: dict[str, Any] = {}
    for name in sorted(sf.config.extra):
        part = f"fields.{name}"
        res = sf.resolve(name)
        if res.status is not Status.FOUND:
            out[name] = None
            st.mark(part)
            kind = "ambiguous" if res.status is Status.AMBIGUOUS else "mapped to a missing field"
            st.warnings.append(
                f"warning: extra field {name} is {kind} (sites.{st.ctx.site}.fields.extra.{name})"
            )
            continue
        found = res.fields[0]
        if found.id not in st.fields:
            out[name] = None
            st.mark(part)
            continue
        out[name] = _convert(st, found.schema, st.fields[found.id], part)
    return out


_DEFAULTS: dict[str, Any] = {
    "labels": [],
    "sprints": [],
    "links": [],
    "attachments": [],
    "fields": {},
    "blocked": {"value": None, "by": []},
}
_OMIT_ON_FAILURE = frozenset(
    {"id", "type", "title", "status", "comments", "url", "description_adf"}
)
_BUILDERS = {name: f"_prop_{name}" for name in PROPERTIES if name != "incomplete"}


def _stub(key: str, ctx: Context, requested: str | None = None) -> dict[str, Any]:
    out: dict[str, Any] = {"key": key, "site": ctx.site}
    if requested is not None and requested != key:
        out["requested_key"] = requested
    out["incomplete"] = ["ticket"]
    return out


def normalize_issue(
    raw: object, ctx: Context, *, requested_key: str | None = None
) -> Normalized | None:
    fallback = (
        requested_key
        if isinstance(requested_key, str) and GRAMMARS["ticket_key"].matches(requested_key)
        else None
    )
    key = raw.get("key") if isinstance(raw, dict) else None
    if (
        not isinstance(raw, dict)
        or not isinstance(key, str)
        or not GRAMMARS["ticket_key"].matches(key)
    ):
        return Normalized(_stub(fallback, ctx)) if fallback is not None else None
    stub = _stub(key, ctx, fallback)
    fields = raw.get("fields")
    if not isinstance(fields, dict):
        return Normalized(stub)
    st = _State(ctx, raw, fields)
    ticket: dict[str, Any] = {"key": key, "site": ctx.site}
    if "requested_key" in stub:
        ticket["requested_key"] = stub["requested_key"]
    for name in ctx.props.names:
        if name == "incomplete":
            continue
        try:
            build = globals()[_BUILDERS[name]]
            value = build(st)
        except Exception:  # noqa: BLE001 - provider data must never abort a read
            if name not in _BUILDERS:
                continue  # a name outside the vocabulary (a hand-built Properties) is ignored
            st.mark(name)
            value = _OMIT if name in _OMIT_ON_FAILURE else _default(name)
        if value is not _OMIT:
            ticket[name] = value
    if "incomplete" in ctx.props.names or st.incomplete:
        ticket["incomplete"] = sorted(st.incomplete)
    return Normalized(ticket, tuple(st.warnings))


def _default(name: str) -> Any:
    value = _DEFAULTS.get(name)
    if isinstance(value, list):
        return []
    if isinstance(value, dict):
        return {k: (list(v) if isinstance(v, list) else v) for k, v in value.items()}
    return value


def normalize_issues(raws: Iterable[object], ctx: Context) -> NormalizedPage:
    tickets: list[dict[str, Any]] = []
    warnings: dict[str, None] = {}
    skipped = 0
    for raw in raws:
        out = normalize_issue(raw, ctx)
        if out is None:
            skipped += 1
            continue
        tickets.append(out.ticket)
        for warning in out.warnings:
            warnings[warning] = None
    return NormalizedPage(tickets, tuple(warnings), skipped)


__all__ = [
    "ALWAYS",
    "GET_DEFAULT",
    "MAX_LINKS",
    "PROPERTIES",
    "SEARCH_DEFAULT",
    "Context",
    "Normalized",
    "NormalizedPage",
    "Properties",
    "ProviderRequest",
    "normalize_issue",
    "normalize_issues",
    "parse_properties",
    "plan_request",
]
