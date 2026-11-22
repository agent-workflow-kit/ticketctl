"""``create``: one ``POST /issue`` through the write flow (docs/interface.md: ``create``, Dedupe).

``create`` is a :class:`~ticketctl.writeflow.WriteAction` with exactly one provider call, so
``partial`` cannot occur. The engine owns the flow (fresh ``/myself``, dry-run, confirmation,
hooks, the lock, the audit pair and ``outcome_unknown``); this module owns:

* **prepare** (no provider call): the title (``updatecmd.read_title``), the labels
  (``validate_label``: the reserved ``tc-<16 hex>`` pattern is refused), the description
  (``--body-file`` through ``PrepareContext.prepare_rich`` with kind ``description``: an ``auto:``
  actor always gets the attribution paragraph, a ``human:`` actor with no body sends no
  description), the dedupe key and the secret check of every typed text.
* **resolve** (once per run): ``createmeta`` of the project (the issue types, then the fields of
  the chosen type), the type by name (an unknown one is exit 22 ``invalid_value`` with the valid
  types in ``details.candidates``), the ``--set`` names and values (``setspec``, ``fieldwrite``).
  A repeated ``reduce`` re-reads nothing of this.
* **reduce**: the dedupe lookup (only with ``--dedupe-key``) and the plan. The lookup searches
  ``project`` AND the ``tc-`` label AND ``reporter`` (the fresh account); the found ticket's type
  and trimmed title must match (``reused``), else exit 20 ``already_exists`` with
  ``details.existing_key``; several matches return the oldest by key and warn
  ``duplicate_dedupe_matches``. In ``reduce(fresh=False)`` it is lock-free; in
  ``reduce(fresh=True)``
  (the engine holds the bucket lock) it goes through ``dedupe.guarded_lookup`` with
  ``scope == Planned.hash_key`` (the project), so the label index lag cannot make a re-run create a
  second ticket. A hit has no step: the result is ``unchanged`` with ``reused`` and the key.
* **check** (only while the create step remains): ``missing_required_fields``.

The body is ``{fields: {project, issuetype, summary, description?, parent?, assignee?, labels?,
<--set fields>}}`` and never holds a sprint (``--set`` refuses the sprint field), a reporter, the
``ticketctl.meta`` property or a ``notifyUsers`` flag. The dedupe label and ``--label`` values are
sent in ``fields.labels``, which is exempt from every createmeta check (the create body accepts
it although createmeta does not list it).
"""

from __future__ import annotations

import argparse
import hashlib
import re
from dataclasses import dataclass
from typing import Any, cast

from . import dedupe, fieldload, selection, setspec, transitionscmd, writeflow
from .adf_from_markdown import text_chars
from .adf_render import render_adf
from .attribution import RichText
from .audit import TICKET_KEY
from .errors import TicketctlError, usage_error
from .fieldwrite import FieldEdit, convert_edits, display_to, fields_body, value_hash
from .flags import Globals
from .gateway import Gateway
from .plan import Planned, ShownText, Step
from .readutil import hide_field_ids, warn_incomplete
from .runtime import load_runtime
from .sanitize import GRAMMARS, query, read_input, sanitize_field, segment
from .setconvert import ValueContext, validate_label
from .updatecmd import read_title
from .writeflow import Context, PrepareContext, Seams, StepResult, WriteGateway, body_info

ISSUE = "/rest/api/3/issue"
SEARCH = "/rest/api/3/search/jql"
TYPES_TEMPLATE = "/rest/api/3/issue/createmeta/{project}/issuetypes"
FIELDS_TEMPLATE = "/rest/api/3/issue/createmeta/{project}/issuetypes/{id}"
NEAR_LIMIT = 0.9
PAGE = 200
MAX_PAGES = 25
MAX_TYPES = 1000
MAX_META_FIELDS = 10_000
MAX_CANDIDATES = 50
MAX_KEY_DIGITS = 18  # a longer ticket number is not a key a provider hands out
MAX_FOUND = 100  # tickets read for one dedupe label (more is lookup_incomplete)
# Required fields the provider or ticketctl fills in: never reported missing. ``parent`` is not
# listed: whether a parent is needed, and which parents fit, is the provider's call (a 400 that
# names ``parent`` in ``details.field_errors``), and its dedicated flag is ``--parent``.
SUPPLIED = frozenset({"summary", "project", "issuetype", "reporter"})
_KEY_PARTS = re.compile(r"([A-Z][A-Z0-9_]*)-([0-9]+)")


def make_seams() -> Seams:
    """The seams of a real run (tests replace this function to fix the attempt id)."""
    return Seams()


def make_clock() -> dedupe.Clock | None:
    """The lag guard's clock (None: real time; tests replace this function with a virtual one)."""
    return None


# ---- the command line --------------------------------------------------------------------------


def check_offline(ns: argparse.Namespace) -> None:
    """The flag rules that need nothing else (the CLI runs them before the default actor)."""
    if (ns.title is None) == (ns.title_file is None):
        raise usage_error("invalid_combination", "exactly one of --title and --title-file")
    if ns.type is None:
        raise usage_error("missing_option", "create needs --type")


def create(g: Globals, a: argparse.Namespace) -> dict[str, Any]:
    rt = load_runtime(g)
    chosen = selection.select_site(
        rt.config,
        site_flag=g.site,
        refs=[a.parent] if a.parent is not None else [],
        projects=[a.project] if a.project is not None else [],
    )
    project = a.project
    if project is None:  # the site's only project, used after the site was chosen
        listed = list(rt.config.sites[chosen.site].projects)
        if len(listed) != 1:
            raise usage_error(
                "missing_option", "create needs --project (the site has no single default project)"
            )
        project = listed[0]
    action = CreateAction(
        project,
        a.type,
        title=a.title,
        title_file=a.title_file,
        body_file=a.body_file,
        parent=chosen.key,
        assign=a.assign,
        labels=a.label or [],
        assignments=setspec.check_offline("create", a.set, "--set"),
        dedupe_key=a.dedupe_key,
        clock=make_clock(),
    )
    return writeflow.execute(rt, chosen.site, action, make_seams())


# ---- createmeta --------------------------------------------------------------------------------


@dataclass(frozen=True)
class _Type:
    id: str
    name: str


@dataclass
class _Resolved:
    """Everything the provider told and the arguments meant, resolved once per run."""

    type: _Type
    meta_fields: dict[str, dict[str, Any]]  # createmeta field id -> the provider's entry
    edits: list[FieldEdit]


def _get(ctx: Context, path: str, template: str, **params: object) -> dict[str, Any]:
    response = ctx.gw.request("GET", path, template=template, query=query(params))
    if response.status == 404:
        raise TicketctlError("not_found", "the project or issue type is missing or hidden")
    data = response.data
    if not 200 <= response.status < 300 or not isinstance(data, dict):
        raise TicketctlError("provider_changed", "the provider answered no usable createmeta")
    return data


def _paged(
    ctx: Context,
    path: str,
    template: str,
    names: tuple[str, ...],
    limit: int,
    first: dict[str, Any] | None = None,
) -> list[Any]:
    """Every entry of a paged createmeta list (the first of ``names`` that is a list).

    ``first`` is an answer for ``startAt=0`` that was already fetched. The list ends at
    ``isLast: true``, at an int ``total``, at an empty page, or (when the provider says neither)
    at a page shorter than its page size; a full page that says nothing usable keeps paging up
    to ``MAX_PAGES`` and then is ``provider_changed``.
    """
    out: list[Any] = []
    start = 0
    for _ in range(MAX_PAGES):
        data = first if first is not None and start == 0 else None
        if data is None:
            data = _get(ctx, path, template, startAt=start, maxResults=PAGE)
        page = next((data[n] for n in names if isinstance(data.get(n), list)), None)
        if page is None:
            raise TicketctlError("provider_changed", "the createmeta answer has no list")
        out.extend(page)
        if len(out) > limit:
            raise TicketctlError("provider_changed", "the createmeta list is too long")
        if not page or data.get("isLast") is True:
            return out
        start += len(page)
        total = data.get("total")
        if isinstance(total, int) and not isinstance(total, bool):
            if start >= total:
                return out
            continue
        size = data.get("maxResults")
        size = size if isinstance(size, int) and not isinstance(size, bool) and size > 0 else PAGE
        if data.get("isLast") is not False and len(page) < size:
            return out
    raise TicketctlError("provider_changed", "the createmeta list has too many pages")


def _types(ctx: Context, project: str) -> list[_Type]:
    path = f"{ISSUE}/createmeta/{segment('project_key', project)}/issuetypes"
    out: list[_Type] = []
    for raw in _paged(ctx, path, TYPES_TEMPLATE, ("issueTypes", "values"), MAX_TYPES):
        if not isinstance(raw, dict):
            continue
        ident, name = raw.get("id"), raw.get("name")
        if isinstance(ident, int) and not isinstance(ident, bool):
            ident = str(ident)
        if not (isinstance(name, str) and name and GRAMMARS["numeric_id"].matches(ident)):
            continue
        out.append(_Type(str(ident), sanitize_field(name, cap=256)[0]))
    return out


def _choose_type(types: list[_Type], wanted: str) -> _Type:
    """The type named ``wanted``: an exact name, else the one case-insensitive match."""
    exact = [t for t in types if t.name == wanted]
    folded = [t for t in types if t.name.casefold() == wanted.casefold()]
    hits = exact[:1] if len(exact) == 1 else folded
    if len(hits) == 1:
        return hits[0]
    candidates = sorted({t.name for t in types})[:MAX_CANDIDATES]
    message = (
        "several issue types have that name; the candidates are listed"
        if hits
        else "the project has no issue type with that name"
    )
    raise TicketctlError(
        "invalid_request",
        message,
        reason="invalid_value",
        details={"option": "type", "candidates": candidates},
    )


def _meta_fields(ctx: Context, project: str, issue_type: _Type) -> dict[str, dict[str, Any]]:
    """The fields of the type's create screen keyed by field id (createmeta lists them by
    ``fieldId``; an object keyed by id is accepted too)."""
    path = (
        f"{ISSUE}/createmeta/{segment('project_key', project)}"
        f"/issuetypes/{segment('numeric_id', issue_type.id)}"
    )
    data = _get(ctx, path, FIELDS_TEMPLATE, startAt=0, maxResults=PAGE)
    raw_fields = data.get("fields", data.get("values"))
    if isinstance(raw_fields, dict):
        return {k: v for k, v in raw_fields.items() if isinstance(k, str) and isinstance(v, dict)}
    items = _paged(ctx, path, FIELDS_TEMPLATE, ("fields", "values"), MAX_META_FIELDS, first=data)
    out: dict[str, dict[str, Any]] = {}
    for raw in items:
        ident = raw.get("fieldId", raw.get("key")) if isinstance(raw, dict) else None
        if isinstance(ident, str) and ident and ident not in out:
            out[ident] = raw
    return out


# ---- the lookup --------------------------------------------------------------------------------


def _order(key: str) -> tuple[str, int]:
    match = _KEY_PARTS.fullmatch(key)
    return (match.group(1), int(match.group(2))) if match else (key, 0)


def _hex(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8", "surrogatepass")).hexdigest()


@dataclass(frozen=True)
class _Found:
    key: str
    type_name: str | None
    title_sha: str | None
    count: int


# ---- the action --------------------------------------------------------------------------------


class CreateAction:
    """``create``; see the module docstring."""

    name = "create"
    yes_needs_dedupe_key = True
    dedupe_lookup = True  # a keyed re-run finds the ticket: outcome_unknown stays retryable

    def __init__(
        self,
        project: str,
        issue_type: str,
        *,
        title: str | None = None,
        title_file: str | None = None,
        body_file: str | None = None,
        parent: str | None = None,
        assign: str | None = None,
        labels: list[str] | None = None,
        assignments: list[setspec.Assignment] | None = None,
        dedupe_key: str | None = None,
        clock: dedupe.Clock | None = None,
    ) -> None:
        self.project = project
        self.type_name = issue_type
        self._title_arg = title
        self._title_file = title_file
        self.body_file = body_file
        self.parent = parent
        self.assign = assign
        self.labels = list(dict.fromkeys(labels or []))
        self.assignments = assignments or []
        self.dedupe_key = dedupe_key
        self.clock = clock
        self.title = ""
        self.rich: RichText | None = None
        self.body_chars = 0
        self._resolved: _Resolved | None = None

    # -- step 1 ----------------------------------------------------------------------------
    def prepare(self, pc: PrepareContext) -> None:
        self.body_chars = pc.body_chars
        title = read_title(pc, self._title_arg, self._title_file)
        assert title is not None  # the command line demands exactly one of the two
        self.title = title
        for label in self.labels:
            validate_label(label)
        named = [self.title, self.type_name, *self.labels]
        if self.parent is not None:
            named.append(self.parent)
        if self.assign not in (None, "me", "none"):
            assert self.assign is not None
            named.append(self.assign)
        if self.dedupe_key is not None:
            GRAMMARS["dedupe_key"].check(self.dedupe_key)
            named.append(self.dedupe_key)
        pc.check_secrets(*named)
        text = None
        if self.body_file is not None:
            text = read_input(self.body_file, kind="body", body_chars=pc.body_chars)
        self.rich = pc.prepare_rich(text, kind="description")

    # -- steps 2 and 3 ---------------------------------------------------------------------
    def reduce(self, ctx: Context, *, fresh: bool) -> Planned:
        if self._resolved is None:  # createmeta and the --set values: once; only the lookup repeats
            self._resolved = self._resolve(ctx)
        found = self._find(ctx, self._resolved, fresh=fresh)
        return self._plan(ctx, self._resolved, found)

    def check(self, ctx: Context, planned: Planned) -> None:
        resolved = self._resolved
        assert resolved is not None
        supplied = set(SUPPLIED) | {e.field.id for e in resolved.edits}
        if self.rich is not None and self.rich.doc is not None:
            supplied.add("description")
        if self.assign is not None:
            supplied.add("assignee")
        if self.labels or self.dedupe_key is not None:
            supplied.add("labels")
        missing = {
            fid: raw
            for fid, raw in resolved.meta_fields.items()
            if raw.get("required") is True
            and raw.get("hasDefaultValue") is not True
            and fid not in supplied
            and fid != "parent"
        }
        marks: set[str] = set()
        required = transitionscmd.required_fields(missing, marks)
        if marks:
            warn_incomplete(ctx.rt, "the required fields", marks)
        for item in required:
            item["name"] = hide_field_ids(item["name"])
        if required:
            raise TicketctlError(
                "invalid_request",
                "the issue type needs fields that were not given (use --set)",
                reason="missing_required_fields",
                details={"required_fields": required},
            )

    def _resolve(self, ctx: Context) -> _Resolved:
        issue_type = _choose_type(_types(ctx, self.project), self.type_name)
        meta_fields = _meta_fields(ctx, self.project, issue_type)
        edits: list[FieldEdit] = []
        if self.assignments:
            gw = cast(Gateway, ctx.gw)  # the guarded gateway answers the same GET interface
            site_fields = fieldload.load_site_fields(
                ctx.rt, ctx.site, gw, needs=[a.name for a in self.assignments]
            )
            meta = setspec.parse_meta({"fields": meta_fields})
            resolved = setspec.resolve_assignments(
                self.assignments, command="create", site=site_fields, meta=meta
            )
            attribution = ctx.rt.config.attribution
            values = ValueContext(
                actor=ctx.actor,
                template=attribution.template,
                attribution_enabled=attribution.enabled,
                body_chars=ctx.body_chars,
                me_account_id=lambda: ctx.me.account_id,
            )
            edits = convert_edits(resolved, site_fields, values)
        return _Resolved(issue_type, meta_fields, edits)

    # -- the dedupe lookup -----------------------------------------------------------------
    def label(self, site: str) -> str:
        """``tc-`` and the first 16 hex characters of ``sha256(site \\0 project \\0 key)``."""
        raw = f"{site}\0{self.project}\0{self.dedupe_key}".encode("utf-8", "surrogatepass")
        return "tc-" + hashlib.sha256(raw).hexdigest()[:16]

    def _find(self, ctx: Context, resolved: _Resolved, *, fresh: bool) -> _Found | None:
        if self.dedupe_key is None:
            return None
        if fresh:  # the engine holds the bucket lock: the guarded lookup of the lag guard
            found = dedupe.guarded_lookup(
                ctx,
                scope=self.project,
                dedupe_key=self.dedupe_key,
                lookup=lambda: self._lookup(ctx),
                clock=self.clock,
            )
        else:
            found = self._lookup(ctx)
        if found is None:
            return None
        if found.type_name != resolved.type.name or found.title_sha != _hex(self.title):
            raise TicketctlError(
                "already_exists",
                "a ticket with this dedupe key and another type or title exists; "
                "use a new key or keep the existing ticket",
                details={"existing_key": found.key},
            )
        return found

    def _lookup(self, ctx: Context) -> _Found | None:
        """The oldest ticket of this account that carries the dedupe label, or None."""
        account = GRAMMARS["account_id"].check(ctx.me.account_id)
        jql = (
            f'project = "{GRAMMARS["project_key"].check(self.project)}" '
            f'AND labels = "{self.label(ctx.site)}" AND reporter = "{account}"'
        )
        response = ctx.gw.request(
            "POST",
            SEARCH,
            template=SEARCH,
            body={"jql": jql, "fields": ["summary", "issuetype"], "maxResults": MAX_FOUND},
            write=False,
        )
        data = response.data
        issues = data.get("issues") if isinstance(data, dict) else None
        if not 200 <= response.status < 300 or not isinstance(issues, list):
            raise TicketctlError("provider_changed", "the dedupe lookup got no usable answer")
        if len(issues) > MAX_FOUND or (isinstance(data, dict) and data.get("nextPageToken")):
            raise TicketctlError(
                "lookup_incomplete",
                "more tickets carry the dedupe label than can be read; nothing was created",
            )
        found: list[tuple[str, str | None, str | None]] = []
        seen: set[str] = set()
        for issue in issues:
            key = issue.get("key") if isinstance(issue, dict) else None
            if (
                not isinstance(key, str)
                or not TICKET_KEY.fullmatch(key)
                or key.rsplit("-", 1)[0] != self.project
                or len(key.rsplit("-", 1)[1]) > MAX_KEY_DIGITS
            ):
                raise TicketctlError("provider_changed", "the dedupe lookup found an unusable key")
            if key in seen:  # one ticket listed twice is one match
                continue
            seen.add(key)
            fields = issue.get("fields")
            fields = fields if isinstance(fields, dict) else {}
            kind = fields.get("issuetype")
            type_name = kind.get("name") if isinstance(kind, dict) else None
            summary = fields.get("summary")
            found.append(
                (
                    key,
                    sanitize_field(type_name, cap=256)[0] if isinstance(type_name, str) else None,
                    _hex(summary.strip()) if isinstance(summary, str) else None,
                )
            )
        if not found:
            return None
        found.sort(key=lambda item: _order(item[0]))
        key, type_name, title_sha = found[0]
        return _Found(key, type_name, title_sha, len(found))

    # -- the plan --------------------------------------------------------------------------
    def _plan(self, ctx: Context, resolved: _Resolved, found: _Found | None) -> Planned:
        rich = self.rich
        assert rich is not None
        edits = resolved.edits
        intent: dict[str, Any] = {
            "project": self.project,
            "type": resolved.type.name,
            "title": _hex(self.title),
            "parent": self.parent,
            "assign": self.assign,
            "labels": sorted(self.labels),
            "fields": {e.name: value_hash(e) for e in edits},
            "bodies": [rich.sha256] if rich.sha256 is not None else [],
            "dedupe_key": self.dedupe_key,
        }
        changes: list[dict[str, Any]] = [{"name": "title", "from": None, "to": self.title}]
        shown = [ShownText("title", self.title, "line")]
        bodies: list[dict[str, Any]] = []
        warnings: list[str] = []
        info: dict[str, Any] | None = None
        if rich.doc is not None:
            info = body_info(rich)
            bodies.append(info)
            text = render_adf(rich.doc).text
            changes.append({"name": "description", "from": None, "to": text})
            shown.append(ShownText("description", text, "body", info["sha256"]))
            if text_chars(rich.doc) > NEAR_LIMIT * ctx.body_chars:
                warnings.append("body_near_limit")
        if self.parent is not None:
            changes.append({"name": "parent", "from": None, "to": self.parent})
            shown.append(ShownText("parent", self.parent, "line"))
        target = self._target(ctx)
        if self.assign is not None:
            changes.append({"name": "assignee", "from": None, "to": target})
            if target is not None:
                shown.append(ShownText("assignee", target, "line"))
        if self.labels:
            changes.append({"name": "labels", "from": [], "to": sorted(self.labels)})
        for edit in edits:
            to = display_to(edit)
            changes.append({"name": edit.name, "from": None, "to": to})
            erich = edit.value.rich
            if erich is not None and erich.doc is not None:
                einfo = body_info(erich)
                bodies.append(einfo)
                shown.append(ShownText(edit.name, str(to or ""), "body", einfo["sha256"]))
                if text_chars(erich.doc) > NEAR_LIMIT * ctx.body_chars:
                    warnings.append("body_near_limit")
            elif isinstance(to, str | int | float | bool):
                shown.append(ShownText(edit.name, str(to), "line"))

        steps: list[Step] = []
        if found is None:
            steps.append(self._create_step(ctx, resolved, info))
        elif found.count > 1:
            warnings.append("duplicate_dedupe_matches")
        return Planned(
            steps=steps,
            intent=intent,
            facts={},
            changes=changes,
            warnings=warnings,
            key=None,
            hash_key=self.project,
            dedupe_key=self.dedupe_key,
            dedupe_match=found.key if found is not None else None,
            bodies=bodies,
            reused=found is not None,
            existing_key=found.key if found is not None else None,
            shown=shown,
            hook={
                "project": self.project,
                "create": {
                    "type": resolved.type.name,
                    "parent": self.parent,
                    "assign": self.assign,
                },
            },
        )

    def _target(self, ctx: Context) -> str | None:
        """The account id ``--assign`` means (None: unassigned, or no ``--assign``)."""
        if self.assign == "me":
            return ctx.me.account_id
        return None if self.assign in (None, "none") else self.assign

    # -- the step --------------------------------------------------------------------------
    def _create_step(self, ctx: Context, resolved: _Resolved, info: dict[str, Any] | None) -> Step:
        rich = self.rich
        assert rich is not None
        fields: dict[str, Any] = {
            "project": {"key": self.project},
            "issuetype": {"id": resolved.type.id},
            "summary": self.title,
        }
        if rich.doc is not None:
            fields["description"] = rich.doc
        if self.parent is not None:
            fields["parent"] = {"key": self.parent}
        if self.assign is not None:
            fields["assignee"] = {"accountId": self._target(ctx)}  # null: explicitly unassigned
        labels = list(self.labels)
        if self.dedupe_key is not None:
            labels.append(self.label(ctx.site))
        if labels:
            fields["labels"] = labels
        fields.update(fields_body(resolved.edits))
        has_key = self.dedupe_key is not None

        def send(gw: WriteGateway) -> StepResult:
            res = gw.request("POST", ISSUE, template=ISSUE, body={"fields": fields})
            try:
                writeflow.raise_for_write(res, object_path=False)
            except TicketctlError as err:
                errors = (err.details or {}).get("field_errors")
                if (
                    has_key
                    and err.code == "rejected"
                    and isinstance(errors, dict)
                    and ("labels" in errors)
                ):
                    err.message = (
                        "the provider rejected the labels of the create request; "
                        "create without --dedupe-key"
                    )
                    err.args = (err.message,)
                raise
            data = res.data if isinstance(res.data, dict) else {}
            key, ident = data.get("key"), data.get("id")
            ids = (
                [str(ident)] if isinstance(ident, str | int) and not isinstance(ident, bool) else []
            )
            if isinstance(key, str):
                ids.append(key)
            return StepResult(ids, key=key if isinstance(key, str) else None)

        props: dict[str, Any] = {"project": self.project, "type": resolved.type.name}
        if info is not None:
            props.update(body_sha256=info["sha256"], body_bytes=info["bytes"])
        return Step("create", send, props)


__all__ = ["CreateAction", "check_offline", "create", "make_clock", "make_seams"]
