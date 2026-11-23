"""The write flow engine: prepare, resolve, reduce and check, dry-run, confirm, hooks, lock and
fresh check, audit and send (docs/interface.md, Write flow).

A write command is a :class:`WriteAction` (its own prepare, reduce and check logic and the
:class:`~ticketctl.plan.Step` objects that perform the provider calls); :func:`execute` runs the
one order for it and returns the ``--json`` object (a plan with ``--dry-run``, else a result) or
raises ``TicketctlError``. The commands of #28 to #34 are actions; this module knows no action.

Order, with the guarantees the tests pin:

1. **Prepare** (no provider call): ``action.prepare`` gets a :class:`PrepareContext`, whose
   ``prepare_rich`` runs ``attribution.prepare_rich_text`` and refuses a configured secret
   (``secret_in_body``). Then ``attempt_id`` is generated (16 random hex characters).
2. **Resolve:** a fresh ``/myself`` (``refresh=True``; the ``me.json`` cache is never trusted for a
   write) and ``identity.check_expectation(write=True)`` (``account_mismatch``, exit 2).
3. **Reduce and check:** ``action.reduce`` returns the remaining steps; none left is ``unchanged``
   and ``action.check`` (the ``--if-status`` and provider-context checks) is not even called.
   Then the plan and its hash are built and ``--expect-plan`` is compared.
4. **Dry-run** returns the plan after step 3's reduce and check: it ignores ``--expect-plan`` and
   ``--yes``, takes no lock, writes no audit and runs no hook.
5. **Confirm:** an interactive run (never an ``auto:`` actor, whatever the seam says) shows the
   dialog of ``confirm_tty`` on ``/dev/tty``, or ``Seams.confirm`` when a test supplies one; a
   decline is exit 20 ``declined``. A non-interactive run without ``--yes``, or one whose
   terminal cannot be opened, is exit 13 ``confirmation_required``.
6. **Pre hooks** through ``Seams.hooks`` (``ticketctl.hooks.HookRunner`` unless a test substitutes
   it): a veto is exit 11, an escalation is exit 13 unless ``--override-escalation``.
7. **Lock and fresh check:** with a dedupe key ``Seams.lock`` is taken (#32), the action reads
   again, and a hash that differs from the held plan is exit 20 ``stale_ticket`` with the new plan
   (no steps left is ``unchanged``). The steps that run are the held plan's.
8. **Audit and send:** the ``attempted`` record is appended and ``fsync``'d before the step's
   request is made (a ``StateError`` refuses the send), the step runs, the outcome record follows
   (a failure there only warns and adds ``audit_incomplete``). A step that raises mid-way records an
   outcome; a failure after an earlier step landed is exit 12 ``partial`` with ``details.result``.

Gate decisions (veto, escalation, stale, decline, ``confirmation_required``) write no audit record.

Rich text: every request body that goes through :class:`WriteGateway` is walked, and every
dictionary of ``{"type": "doc"}`` shape in it is checked with ``validate_adf`` before the request
is made, so no rich-text path (a comment, a description, a ``--set`` value, a hook-less retry) can
reach the provider unvalidated whatever the action does.

Outcomes (docs/interface.md, Result object and Audit and log): a request that may have been sent
and got no usable answer is ``unknown`` and is never sent again; ``changed`` of a partial result is
true because a step landed; the command deadline does not run during the prompt and the ``pre-``
hooks; an interrupt anywhere in step 8 still reaches the ``post-`` hook with what is known.
The dedupe lock (``Seams.lock``, default ``dedupe.bucket_lock``) and the create lag guard
(``dedupe.guarded_lookup``, called by the create action's ``reduce(fresh=True)``) live in
``dedupe.py`` (#32).
"""

from __future__ import annotations

import contextlib
import hashlib
import secrets
import sys
from collections.abc import Callable, Mapping, Sequence
from contextlib import AbstractContextManager, nullcontext
from dataclasses import dataclass, field
from typing import Any, Protocol

from . import attribution, audit, confirm_tty, flags, identity
from .adf_validate import canonical_json, validate_adf
from .attribution import Format, Kind, RichText
from .dedupe import bucket_lock
from .errors import TicketctlError, from_exception, read_only_error, usage_error
from .flags import Globals
from .gateway import Deadline, Gateway, Response, Target
from .plan import Planned, Step, build_plan, canonical_bytes
from .runtime import Runtime, for_site, open_gateway
from .sanitize import GRAMMARS, add_secrets, ensure_no_secret, sanitize_field
from .state import StateError

MAX_DECISION_IDS = 16
MAX_OUTBOUND_NODES = 500_000
CONFIRM_ACTIONS = ("comment", "create")  # outcome_unknown is not retryable without a dedupe key


# --------------------------------------------------------------------------------------------
# Results of a step and the guarded gateway
# --------------------------------------------------------------------------------------------


@dataclass
class StepResult:
    """What a step reports after a successful call (nothing here is provider free text)."""

    provider_ids: list[str] = field(default_factory=list)
    key: str | None = None  # the created ticket's key
    comment_id: str | None = None


def check_outbound(body: object, *, body_chars: int) -> None:
    """``validate_adf`` for every ADF document anywhere in a request body (iterative, bounded).

    A mapping is a document, validated whole and not walked further, when its ``type`` is a string
    that reads ``doc`` ignoring case and surrounding space (a miscased ``Doc`` is refused, never
    sent), or when it holds both ``version`` and ``content``. Anything else is walked through
    mappings and sequences (lists, tuples; not strings or bytes). Raises ``InputError`` (exit 22)
    for a refused document and ``TicketctlError`` ``internal`` for a body too large to walk (a
    caller bug: an action sent more than the limits allow).
    """
    stack: list[object] = [body]
    seen = 0
    while stack:
        node = stack.pop()
        seen += 1
        if seen > MAX_OUTBOUND_NODES:
            raise TicketctlError("internal", "the outbound request body is too large to check")
        if isinstance(node, Mapping):
            kind = node.get("type")
            looks_like_doc = (isinstance(kind, str) and kind.strip().lower() == "doc") or (
                "version" in node and "content" in node
            )
            if looks_like_doc:
                validate_adf(node, body_chars=body_chars)  # exactly a dict and exactly "doc"
                continue
            stack.extend(node.values())
        elif isinstance(node, Sequence) and not isinstance(node, str | bytes | bytearray):
            stack.extend(node)


class WriteGateway:
    """The gateway as an action sees it: every request body is checked for ADF first."""

    def __init__(self, gateway: Gateway, *, body_chars: int) -> None:
        self._gw = gateway
        self._body_chars = body_chars

    @property
    def target(self) -> Target:
        return self._gw.target

    @property
    def deadline(self) -> Deadline:
        """The whole-command clock (the lock wait and the lag guard are bounded by it)."""
        return self._gw.deadline

    def request(
        self,
        method: str,
        path: str,
        *,
        template: str,
        query: str = "",
        body: object | None = None,
        write: bool | None = None,
    ) -> Response:
        check_outbound(body, body_chars=self._body_chars)
        return self._gw.request(
            method, path, template=template, query=query, body=body, write=write
        )


# --------------------------------------------------------------------------------------------
# Contexts and the action protocol
# --------------------------------------------------------------------------------------------


@dataclass
class PrepareContext:
    """Step 1: no provider call. ``prepare_rich`` is the one path every rich-text value takes."""

    rt: Runtime
    site: str
    actor: str
    secret_values: list[str] = field(default_factory=list)

    @property
    def body_chars(self) -> int:
        return self.rt.config.limits.body_chars

    def check_secrets(self, *texts: str) -> None:
        """Exit 22 ``secret_in_body`` when a text holds a configured token value."""
        ensure_no_secret(*texts)
        if self.secret_values:
            ensure_no_secret(*texts, secrets=self.secret_values)

    def prepare_rich(self, body: str | None, *, kind: Kind, fmt: Format = "markdown") -> RichText:
        """Convert, attribute and validate one rich-text value, then refuse a secret in it."""
        attribution_cfg = self.rt.config.attribution
        rich = attribution.prepare_rich_text(
            body,
            actor=self.actor,
            kind=kind,
            template=attribution_cfg.template,
            enabled=attribution_cfg.enabled,
            fmt=fmt,
            body_chars=self.body_chars,
        )
        if rich.doc is not None:
            self.check_secrets(*document_texts(rich.doc), *joined_texts(rich.doc))
        return rich


@dataclass
class Context:
    """Steps 2 to 8: what an action reads with. ``gw`` is the guarded gateway."""

    rt: Runtime
    site: str
    gw: WriteGateway
    me: identity.Me
    actor: str
    actor_source: str
    attempt_id: str
    action: str

    @property
    def g(self) -> Globals:
        return self.rt.g

    @property
    def body_chars(self) -> int:
        return self.rt.config.limits.body_chars


class WriteAction(Protocol):
    """One write command.

    ``name`` is the plan ``action``; ``dedupe_key`` the caller's key (or None);
    ``yes_needs_dedupe_key`` is true for ``comment`` and ``create``. ``reduce`` reads and returns
    the steps that remain; it must apply idempotency first (no step left is ``unchanged``) and is
    called again, with ``fresh=True``, under the lock. ``check`` runs only while a step remains and
    raises the refusals of interface.md, Write flow step 3 (``--if-status`` first).
    """

    name: str
    dedupe_key: str | None
    yes_needs_dedupe_key: bool

    def prepare(self, pc: PrepareContext) -> None: ...

    def reduce(self, ctx: Context, *, fresh: bool) -> Planned: ...

    def check(self, ctx: Context, planned: Planned) -> None: ...


MAX_PROVIDER_MESSAGES = 20
PROVIDER_TEXT_CAP = 500


def _provider_text(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    return sanitize_field(value, cap=PROVIDER_TEXT_CAP)[0]


def raise_for_write(response: Response, *, object_path: bool) -> None:
    """Map the status of a write's answer (docs/interface.md, HTTP status mapping).

    A 2xx returns. A 404 on an endpoint whose path names an object is exit 10 ``not_found``
    (``object_path`` false: a collection endpoint, exit 12 ``provider_changed``; the caller
    says which, there is no default); every other 4xx
    (400 and 409, 413, 415, 422, ...) is exit 12 ``rejected`` with nothing applied, and the
    sanitized provider messages in ``details.errorMessages`` and ``details.field_errors``
    (untrusted). The gateway has already raised for 3xx, 401, 403, 429 and 5xx.
    """
    status = response.status
    if 200 <= status < 300:
        return
    if status == 404:
        if object_path:
            raise TicketctlError(
                "not_found", "the ticket is missing or you have no permission to see it"
            )
        raise TicketctlError("provider_changed", "the provider answered 404 on a collection")
    if not 400 <= status < 500:
        raise TicketctlError("provider_changed", "the provider answered an unexpected status")
    messages: list[str] = []
    field_errors: dict[str, str] = {}
    data = response.data
    if isinstance(data, dict):
        raw = data.get("errorMessages")
        if isinstance(raw, list):
            messages = [m for m in map(_provider_text, raw[:MAX_PROVIDER_MESSAGES]) if m]
        single = _provider_text(data.get("error"))
        if single:
            messages.append(single)
        errors = data.get("errors")
        if isinstance(errors, dict):
            for name, text in list(errors.items())[:MAX_PROVIDER_MESSAGES]:
                shown, value = _provider_text(name), _provider_text(text)
                if shown and value:
                    field_errors[shown] = value
    elif response.text:
        text = _provider_text(response.text)
        messages = [text] if text else []
    details: dict[str, Any] = {}
    if messages:
        details["errorMessages"] = messages
    if field_errors:
        details["field_errors"] = field_errors
    raise TicketctlError("rejected", "the provider rejected the request", details=details or None)


def document_texts(doc: object) -> list[str]:
    """Every text and link target of an ADF document (iterative), for the secret check."""
    out: list[str] = []
    stack: list[object] = [doc]
    while stack:
        node = stack.pop()
        if isinstance(node, dict):
            text = node.get("text")
            if isinstance(text, str):
                out.append(text)
            href = node.get("href")
            if isinstance(href, str):
                out.append(href)
            stack.extend(node.values())
        elif isinstance(node, list):
            stack.extend(node)
    return out


def _ordered_text(node: object) -> str:
    """The text nodes of a subtree in document order, joined (marks and breaks split text)."""
    parts: list[str] = []
    stack: list[object] = [node]
    while stack:
        item = stack.pop()
        if isinstance(item, dict):
            text = item.get("text")
            if isinstance(text, str):
                parts.append(text)
            content = item.get("content")
            if isinstance(content, list):
                stack.extend(reversed(content))
        elif isinstance(item, list):
            stack.extend(reversed(item))
    return "".join(parts)


def joined_texts(doc: object) -> list[str]:
    """The whole document's text joined, and each top-level block's: a secret split across text
    nodes (a mark, a link, a hard break) still shows in one of them."""
    out = [_ordered_text(doc)]
    content = doc.get("content") if isinstance(doc, dict) else None
    if isinstance(content, list):
        out.extend(_ordered_text(block) for block in content)
    return out


def body_info(rich: RichText) -> dict[str, Any]:
    """``{sha256, bytes}`` of a prepared value (bytes of the hashed canonical JSON)."""
    if rich.doc is None or rich.sha256 is None:
        raise ValueError("body_info needs a prepared document")
    return {"sha256": rich.sha256, "bytes": len(canonical_json(rich.doc).encode("utf-8"))}


def rich_changed(current: object, rich: RichText) -> bool:
    """False when the stored ADF already equals the new value (the rich-text unchanged test)."""
    from .setconvert import rich_unchanged

    return not rich_unchanged(current, rich)


# --------------------------------------------------------------------------------------------
# Seams owned by later issues
# --------------------------------------------------------------------------------------------


@dataclass
class HookResult:
    decision_ids: list[str] = field(default_factory=list)
    escalated: list[str] = field(default_factory=list)  # hook names that exited 13


@dataclass
class PostInfo:
    """What a ``post-`` hook is told (``ticketctl.hooks.build_payload`` makes the payload)."""

    result: str  # ok, failed, partial, unknown
    error: dict[str, str | None] | None
    steps: list[dict[str, str]]
    provider_ids: list[str]
    changed: bool | None
    key: str | None = None  # the created ticket's key (post-create's object_id)


class Hooks(Protocol):
    def pre(
        self, plan: dict[str, Any], planned: Planned, attempt_id: str, decision_ids: list[str]
    ) -> HookResult:
        """Run the ``pre-`` hooks; raise ``TicketctlError("vetoed", ...)`` for a veto."""
        ...

    def post(
        self,
        plan: dict[str, Any],
        planned: Planned,
        attempt_id: str,
        decision_ids: list[str],
        info: PostInfo,
    ) -> None: ...


def _new_attempt_id() -> str:
    return secrets.token_hex(8)


@dataclass
class Seams:
    """The parts other issues fill in, and the hooks tests use to substitute the world.

    ``gateway`` replaces ``open_gateway``; ``interactive`` replaces the TTY probes; ``confirm`` is
    a replacement for the confirmation dialog (``plan -> accepted``; tests only). With neither
    ``confirm`` nor ``interactive`` set the real dialog on ``/dev/tty`` runs; with only
    ``interactive`` set (no dialog to ask) the run is exit 13 ``confirmation_required``;
    ``lock`` returns a
    context manager held from the fresh check to the outcome record.
    """

    gateway: Gateway | None = None
    interactive: Callable[[], bool] | None = None
    confirm: Callable[[dict[str, Any]], bool] | None = None
    hooks: Hooks | None = None  # None: the real runner (ticketctl.hooks.HookRunner)
    lock: Callable[[Planned, Context], AbstractContextManager[None]] = bucket_lock
    attempt_id: Callable[[], str] = _new_attempt_id


# --------------------------------------------------------------------------------------------
# The result object
# --------------------------------------------------------------------------------------------


def browse_url(rt: Runtime, site: str, key: str) -> str:
    return str(rt.config.sites[site].url).rstrip("/") + "/browse/" + key


def _decision_list(*groups: list[str]) -> tuple[list[str], bool]:
    """Flag and hook ids, in order, without duplicates; an id outside the grammar is dropped.

    Returns the ids (at most 16, flag ids first) and whether valid ids beyond 16 were dropped.
    """
    grammar = GRAMMARS["decision_id"]
    merged = list(dict.fromkeys(i for group in groups for i in group if grammar.matches(i)))
    return merged[:MAX_DECISION_IDS], len(merged) > MAX_DECISION_IDS


def result_object(
    ctx: Context,
    planned: Planned,
    *,
    result: str,
    steps: list[dict[str, str]],
    key: str | None,
    comment_id: str | None,
    attempt_id: str | None,
    decision_ids: list[str],
    warnings: list[str],
) -> dict[str, Any]:
    # Never raise after a write landed: a key or comment id outside its grammar is dropped (a
    # landed create whose key is lost prints a null key and url).
    if key is not None and not audit.TICKET_KEY.fullmatch(key):
        key = None
    if comment_id is not None and not GRAMMARS["provider_id"].matches(comment_id):
        comment_id = None
    return {
        "action": ctx.action,
        "site": ctx.site,
        "key": key,
        "url": browse_url(ctx.rt, ctx.site, key) if key is not None else None,
        "changed": result in ("ok", "partial"),  # true: something landed
        "result": result,
        "steps": steps,
        "already_posted": planned.already_posted if result == "unchanged" else False,
        "reused": planned.reused if result == "unchanged" else False,
        "comment_id": comment_id,
        "attempt_id": attempt_id,
        "decision_ids": decision_ids,
        "warnings": list(dict.fromkeys(warnings)),
    }


# --------------------------------------------------------------------------------------------
# The audit records of one command
# --------------------------------------------------------------------------------------------


def _config_file_hashes(rt: Runtime) -> list[str]:
    hashes = []
    for path in rt.loaded.files:
        hashes.append(hashlib.sha256(path.read_bytes()).hexdigest())
    return hashes


def hook_family(action: str) -> str:
    return "sprint" if action.startswith("sprint_") else action


def hooks_digest(rt: Runtime, action: str) -> str:
    """sha256 of the effective hook list of the action's family: configured, then ``--hook``."""
    family = hook_family(action)
    entries: list[dict[str, Any]] = []
    for phase in ("pre", "post"):
        for hook in rt.config.hooks.get(f"{phase}-{family}", []):
            entries.append(
                {
                    "name": f"{phase}-{family}",
                    "cmd": hook.cmd,
                    "timeout": int(hook.timeout.total_seconds()),
                    "async": hook.async_,
                    "source": "config",
                }
            )
    for spec in rt.g.hooks:
        name, _, cmd = spec.partition("=")
        if name.endswith(f"-{family}"):
            entries.append({"name": name, "cmd": cmd, "source": "flag"})
    return hashlib.sha256(canonical_bytes(entries)).hexdigest()


def cli_hook_names(rt: Runtime) -> list[str]:
    return list(dict.fromkeys(spec.partition("=")[0] for spec in rt.g.hooks))


def _stdin_tty() -> bool:
    try:
        return sys.stdin.isatty()
    except (AttributeError, ValueError):
        return False


class _Audit:
    """Builds and appends the records of one command; every record shares its static part."""

    def __init__(
        self,
        ctx: Context,
        action: str,
        planned: Planned,
        plan_sha256: str,
        decision_ids: list[str],
        config_hashes: list[str],
    ) -> None:
        rt = ctx.rt
        site = rt.config.sites[ctx.site]
        self.rt = rt
        self.ctx = ctx
        self.action = action
        self.planned = planned
        self.decision_ids = decision_ids
        self.token_env = site.token_env
        self.inputs: dict[str, Any] = {
            "plan_sha256": plan_sha256,
            "bodies": list(planned.bodies),
            "dedupe_key": planned.dedupe_key,
            "override_escalation": rt.g.override_escalation,
            "config": {
                "path": rt.g.config,
                "files_sha256": config_hashes,
                "sha256": rt.config_sha256(),
            },
            "hooks_sha256": hooks_digest(rt, action),
            "site_host": ctx.gw.target.host_port,
            "cli_hooks": cli_hook_names(rt),
            "token_env": site.token_env,
        }
        self.stdin_tty = _stdin_tty()

    def append(
        self,
        step_no: int,
        step: Step,
        result: str,
        *,
        changed: bool | None,
        reason: str | None,
        provider_ids: list[str],
        key: str | None,
    ) -> None:
        record: dict[str, Any] = {
            "attempt_id": self.ctx.attempt_id,
            "step": step_no,
            "step_kind": step.kind,
            "actor": self.ctx.actor,
            "actor_source": self.ctx.actor_source,
            "stdin_tty": self.stdin_tty,
            "decision_id": self.decision_ids,
            "target": {"site": self.ctx.site, "key": key},
            "action": self.action,
            "inputs": self.inputs,
            "result": result,
            "provider_ids": provider_ids,
            "changed": changed,
        }
        if reason is not None:
            record["reason"] = reason
        audit.append_audit(self.rt.state_dir, record)


# --------------------------------------------------------------------------------------------
# The engine
# --------------------------------------------------------------------------------------------


def is_interactive(g: Globals) -> bool:
    """A human can be asked: no ``--yes``, not an ``auto:`` actor, a TTY and ``/dev/tty``."""
    if g.yes or (g.resolved_actor or "").startswith("auto:"):
        return False
    return flags.stderr_is_tty() and flags.probe_dev_tty()


def _with_details(err: TicketctlError, **details: Any) -> TicketctlError:
    err.details = {**(err.details or {}), **details}
    return err


def execute(
    rt: Runtime, site: str, action: WriteAction, seams: Seams | None = None
) -> dict[str, Any]:
    """Run the write flow for ``action`` on ``site``; see the module docstring.

    Returns the plan (``--dry-run``) or the result of an ``ok`` or ``unchanged`` run. Every
    failure is a ``TicketctlError`` (or another module's error that ``from_exception`` maps).
    """
    if rt.config.read_only and not rt.g.dry_run:
        # The engine's own guard, for a caller that did not come through the CLI dispatch: before
        # the action is touched, so no read, prompt, hook, lock or audit record happens.
        raise read_only_error()
    with for_site(site):
        return _Run(rt, site, action, seams or Seams()).run()


def _real_hooks(rt: Runtime) -> Hooks:
    from .hooks import HookRunner  # hooks.py imports this module

    return HookRunner(rt)


class _Run:
    def __init__(self, rt: Runtime, site: str, action: WriteAction, seams: Seams) -> None:
        self.rt = rt
        self.g = rt.g
        self.site = site
        self.action = action
        self.seams = seams
        self.hooks = seams.hooks if seams.hooks is not None else _real_hooks(rt)
        self.audit_incomplete = False
        self.extra_warnings: list[str] = []
        self.config_hashes: list[str] | OSError = []
        self.gw: Gateway | None = None

    # ------------------------------------------------------------------------------------
    def run(self) -> dict[str, Any]:
        g = self.g
        actor = g.resolved_actor
        if actor is None:
            raise ValueError("the actor must be resolved before a write")
        try:
            attribution.actor_kind(actor)
        except ValueError:
            raise usage_error("invalid_argument", "invalid actor") from None
        self._flag_rules()
        try:  # once, before the confirmation and the hooks; an error surfaces at the audit
            self.config_hashes = _config_file_hashes(self.rt)
        except OSError as exc:
            self.config_hashes = exc

        site_cfg = self.rt.config.sites[self.site]
        token = self.rt.env.get(site_cfg.token_env, "")
        pc = PrepareContext(
            self.rt, self.site, actor, secret_values=[token] if len(token.strip()) >= 8 else []
        )
        if pc.secret_values:
            add_secrets(pc.secret_values)
        self.action.prepare(pc)  # step 1: no provider call
        attempt_id = self.seams.attempt_id()

        gw = self.seams.gateway or open_gateway(self.rt, self.site)
        self.gw = gw
        wgw = WriteGateway(gw, body_chars=pc.body_chars)
        # Step 2: a fresh account, never the cache, then the configured expectation.
        me = identity.load_me(self.rt, self.site, gw, refresh=True)
        identity.check_expectation(self.rt.config, self.site, me.account_id, write=True)
        ctx = Context(
            self.rt, self.site, wgw, me, actor, g.actor_source or "default", attempt_id,
            self.action.name,
        )  # fmt: skip
        return self._flow(ctx)

    def _flag_rules(self) -> None:
        g = self.g
        if g.yes and not g.dry_run:
            if g.expect_plan is None:
                raise usage_error("yes_requires_expect_plan", "--yes always needs --expect-plan")
            if self.action.yes_needs_dedupe_key and self.action.dedupe_key is None:
                raise usage_error(
                    "dedupe_key_required", f"--yes with {self.action.name} needs --dedupe-key"
                )

    # ------------------------------------------------------------------------------------
    def _read(self, ctx: Context, *, fresh: bool) -> tuple[Planned, dict[str, Any]]:
        """Reduce, check (only while a step remains) and build the plan."""
        planned = self.action.reduce(ctx, fresh=fresh)
        if planned.steps:
            self.action.check(ctx, planned)
        plan = build_plan(self.action.name, ctx.site, ctx.actor, planned)
        return planned, plan

    def _flow(self, ctx: Context) -> dict[str, Any]:
        g = self.g
        planned, plan = self._read(ctx, fresh=False)  # step 3
        decision_ids, cut = _decision_list(g.decision_ids)
        self._note_truncated(cut)
        if g.dry_run:  # step 4
            return plan
        if not planned.steps:  # idempotency first: before expect-plan, prompt, hook, audit
            return self._unchanged(ctx, planned, decision_ids)
        held = plan["plan_sha256"]
        if g.expect_plan is not None and g.expect_plan != held:
            raise TicketctlError(
                "stale_ticket",
                "the plan differs from --expect-plan; re-plan with --dry-run",
                details={"plan": plan},
            )
        # The prompt and the pre hooks are not provider work: the command deadline does not run
        # while they do (interface.md, Reliability).
        with self._deadline_paused():
            self._confirm(plan, planned, decision_ids)  # step 5
            try:
                hook_result = self.hooks.pre(plan, planned, ctx.attempt_id, decision_ids)  # 6
            except TicketctlError as err:
                if err.code == "vetoed":  # the gate's decision carries the plan the human accepted
                    err.details = {
                        "plan": plan,
                        "decision_ids": decision_ids,
                        **(err.details or {}),
                    }
                raise
        decision_ids, cut = _decision_list(g.decision_ids, hook_result.decision_ids)
        self._note_truncated(cut)
        if hook_result.escalated and not g.override_escalation:
            names = list(dict.fromkeys(hook_result.escalated))
            raise TicketctlError(
                "escalated",
                "a hook asked for review; confirm with --override-escalation=ticketctl",
                details={
                    "plan": plan,
                    "decision_ids": decision_ids,
                    "hook": names[0],
                    "escalated_hooks": names,
                },
            )

        lock = self._lock(ctx, planned)
        taken = False
        interrupted: _Interrupt | None = None
        outcome = _Outcome(None, None, None)
        try:  # steps 7 and 8; the finally is the one release point for every exit
            lock.__enter__()  # a failure to take the lock is before any send: it propagates
            taken = True
            fresh, fresh_plan = self._read(ctx, fresh=True)
            if not fresh.steps:
                return self._unchanged(ctx, fresh, decision_ids)
            if fresh_plan["plan_sha256"] != held:
                raise TicketctlError(
                    "stale_ticket",
                    "the ticket changed since the plan was made; re-plan with --dry-run",
                    details={"plan": fresh_plan},
                )
            outcome = self._send(ctx, planned, held, decision_ids)
        except _Interrupt as stop:
            interrupted = stop
        finally:
            stopped = self._release(lock) if taken else None
            if stopped is not None:  # an interrupt in the release: the hook still runs
                info = interrupted.info if interrupted is not None else outcome.info
                self._post(plan, planned, ctx, decision_ids, _Outcome(None, None, info))
                raise stopped
        if interrupted is not None:
            self._post(plan, planned, ctx, decision_ids, _Outcome(None, None, interrupted.info))
            raise interrupted.exc from None
        self._post(plan, planned, ctx, decision_ids, outcome)
        if outcome.error is not None:
            raise outcome.error
        assert outcome.result is not None
        return outcome.result

    # ------------------------------------------------------------------------------------
    def _deadline_paused(self) -> AbstractContextManager[None]:
        deadline = getattr(self.gw, "deadline", None)
        return deadline.paused() if isinstance(deadline, Deadline) else nullcontext()

    def _note_truncated(self, cut: bool) -> None:
        if cut and "decision_ids_truncated" not in self.extra_warnings:
            self.extra_warnings.append("decision_ids_truncated")

    def _release(self, lock: AbstractContextManager[None]) -> BaseException | None:
        """A release failure after the outcome is a warning: the result must survive it. An
        interrupt in the release is returned for the caller to raise after the post hook."""
        try:
            lock.__exit__(None, None, None)
        except Exception:  # noqa: BLE001 - the write landed or was refused; say so, do not hide it
            self.rt.warn("warning: the lock could not be released")
        except BaseException as exc:  # noqa: BLE001
            return exc
        return None

    def _lock(self, ctx: Context, planned: Planned) -> AbstractContextManager[None]:
        if planned.dedupe_key is None:
            return nullcontext()
        return self.seams.lock(planned, ctx)

    def _unchanged(self, ctx: Context, planned: Planned, decision_ids: list[str]) -> dict[str, Any]:
        key = planned.existing_key or planned.key
        return result_object(
            ctx,
            planned,
            result="unchanged",
            steps=[],
            key=key,
            comment_id=planned.comment_id,
            attempt_id=None,
            decision_ids=decision_ids,
            warnings=[*planned.warnings, *self.extra_warnings],
        )

    def _confirm(self, plan: dict[str, Any], planned: Planned, decision_ids: list[str]) -> None:
        g = self.g
        interactive = (self.seams.interactive or (lambda: is_interactive(g)))()
        # An automatic actor is never asked, whatever the seam says: the human is the gate.
        auto = (g.resolved_actor or "").startswith("auto:")
        if interactive and not g.yes and not auto:
            if self.seams.confirm is not None:
                accepted = self.seams.confirm(plan)
            elif self.seams.interactive is None:
                try:
                    accepted = confirm_tty.confirm(
                        plan,
                        planned.shown,
                        override=bool(g.override_escalation),
                        hook_names=self._hook_names(),
                    )
                except confirm_tty.ConfirmUnavailable:
                    raise self._confirmation_required(plan, decision_ids) from None
            else:
                raise self._confirmation_required(plan, decision_ids)
            if not accepted:
                raise TicketctlError("declined", "the plan was declined", details={"plan": plan})
            return
        if not g.yes:
            raise self._confirmation_required(plan, decision_ids)

    def _hook_names(self) -> list[str]:
        """The hooks an override covers: every configured hook of the action, every ``--hook``."""
        family = hook_family(self.action.name)
        names = [
            f"{phase}-{family}"
            for phase in ("pre", "post")
            if self.rt.config.hooks.get(f"{phase}-{family}")
        ]
        return list(dict.fromkeys([*names, *cli_hook_names(self.rt)]))

    @staticmethod
    def _confirmation_required(plan: dict[str, Any], decision_ids: list[str]) -> TicketctlError:
        return TicketctlError(
            "confirmation_required",
            "a human must confirm; re-run with --yes --expect-plan " + plan["plan_sha256"],
            details={"plan": plan, "decision_ids": decision_ids},
        )

    # ------------------------------------------------------------------------------------
    def _send(
        self, ctx: Context, planned: Planned, plan_sha: str, decision_ids: list[str]
    ) -> _Outcome:
        try:
            if isinstance(self.config_hashes, OSError):
                raise self.config_hashes
            log = _Audit(ctx, self.action.name, planned, plan_sha, decision_ids, self.config_hashes)
        except Exception as exc:  # noqa: BLE001 - no audit, no send
            return _Outcome(None, self._audit_failed(exc), None)
        done: list[dict[str, str]] = []
        provider_ids: list[str] = []
        st = _SendState()
        try:  # any interrupt anywhere in the send: the post hook still runs (_interrupt_info)
            return self._send_steps(
                ctx, planned, plan_sha, decision_ids, log, done, provider_ids, st
            )
        except _Interrupt:
            raise
        except BaseException as exc:  # noqa: BLE001 - a gap between two statements of the send
            steps = planned.steps
            if st.number and st.sent and len(done) < st.number:  # the write landed, unrecorded
                done.append({"kind": steps[st.number - 1].kind, "status": "done"})
                st.landed = True
            elif st.number and len(done) < st.number:  # the step in flight has no outcome yet
                st.unknown = st.calling  # the request may have gone out
                code = "outcome_unknown" if st.unknown else "internal"
                st.error = {"code": code, "reason": None}
                done.append({"kind": steps[st.number - 1].kind, "status": "failed", "code": code})
            info = None
            if st.started:
                try:
                    info = self._interrupt_info(planned, done, provider_ids, st)
                except Exception:  # noqa: BLE001 - never mask the original exception
                    info = None
            raise _Interrupt(exc, info) from None

    def _interrupt_info(
        self, planned: Planned, done: list[dict[str, str]], provider_ids: list[str], st: _SendState
    ) -> PostInfo:
        """What the post hook is told when the command ends here: ``done`` holds the steps up to
        and including the failing one, the rest are listed as skipped. Any ``partial`` has
        ``changed`` true (a step landed); only ``unknown`` has null."""
        steps = planned.steps
        kinds = _kinds(
            [*done, *({"kind": s.kind, "status": "skipped"} for s in steps[len(done) :])]
        )
        key = st.created_key or planned.key or planned.existing_key
        failed = any(s["status"] == "failed" for s in done)
        if st.landed:
            if not failed and len(done) == len(steps):
                return PostInfo("ok", None, kinds, provider_ids, True, key)
            error = st.error or {"code": "internal", "reason": None}
            return PostInfo("partial", error, kinds, provider_ids, True, key)
        error = st.error or {"code": "internal", "reason": None}
        if st.unknown:
            return PostInfo("unknown", error, kinds, provider_ids, None)
        return PostInfo("failed", error, kinds, provider_ids, False)

    def _send_steps(
        self,
        ctx: Context,
        planned: Planned,
        plan_sha: str,
        decision_ids: list[str],
        log: _Audit,
        done: list[dict[str, str]],
        provider_ids: list[str],
        st: _SendState,
    ) -> _Outcome:
        comment_id: str | None = None
        # the error, whether the outcome is unknown, whether the audit refused the send
        failure: tuple[TicketctlError, bool, bool] | None = None
        steps = planned.steps

        for number, step in enumerate(steps, start=1):
            st.number, st.calling, st.sent = number, False, False
            try:
                log.append(
                    number,
                    step,
                    "attempted",
                    changed=None,
                    reason=None,
                    provider_ids=[],
                    key=planned.key,
                )
            except Exception as exc:  # noqa: BLE001 - any failure: no attempted record, no send
                # Nothing may be sent without its attempted record.
                err = self._audit_failed(exc)
                failure = (err, False, True)
                st.error = {"code": err.code, "reason": err.reason}
                done.append({"kind": step.kind, "status": "failed", "code": err.code})
                break
            st.started = True  # an interrupt inside the first append runs no hook (interface.md)
            try:
                st.calling = True
                res = step.send(ctx.gw)
            except BaseException as exc:  # noqa: BLE001 - an outcome record is written for all
                err, unknown = self._classify(exc)
                result_name = "unknown" if unknown else ("partial" if st.landed else "failed")
                done.append({"kind": step.kind, "status": "failed", "code": err.code})
                st.error, st.unknown = {"code": err.code, "reason": err.reason}, unknown
                info = self._interrupt_info(planned, done, provider_ids, st)
                self._outcome(
                    log, number, step, result_name,
                    changed=None if unknown else False,
                    reason=err.code, provider_ids=[], key=None,
                    interrupted=info,
                )  # fmt: skip
                if not isinstance(exc, Exception):
                    raise _Interrupt(exc, info) from None
                failure = (err, unknown, False)
                break
            st.sent = True
            ids = [i for i in res.provider_ids if GRAMMARS["provider_id"].matches(i)]
            key = res.key if res.key and audit.TICKET_KEY.fullmatch(res.key) else None
            st.landed = True
            provider_ids.extend(ids)
            st.created_key = st.created_key or key
            comment_id = comment_id or res.comment_id
            done.append({"kind": step.kind, "status": "done"})
            self._outcome(
                log, number, step, "ok", changed=True, reason=None, provider_ids=ids,
                key=key or planned.key,
                interrupted=self._interrupt_info(planned, done, provider_ids, st),
            )  # fmt: skip
        st.number = 0  # the loop is over: no step is in flight

        for step in steps[len(done) :]:
            done.append({"kind": step.kind, "status": "skipped"})
        warnings = [
            *planned.warnings,
            *self.extra_warnings,
            *(["audit_incomplete"] if self.audit_incomplete else []),
        ]
        key = st.created_key or planned.key or planned.existing_key

        if failure is None:
            result = result_object(
                ctx, planned, result="ok", steps=done, key=key, comment_id=comment_id,
                attempt_id=ctx.attempt_id, decision_ids=decision_ids, warnings=warnings,
            )  # fmt: skip
            return _Outcome(result, None, self._interrupt_info(planned, done, provider_ids, st))

        err, unknown, audit_refused = failure
        self._retry_rules(err, planned)
        st.error, st.unknown = {"code": err.code, "reason": err.reason}, unknown
        if not st.landed:
            if audit_refused:  # no attempted record exists: no attempt_id, no post hook
                return _Outcome(None, err, None)
            info = self._interrupt_info(planned, done, provider_ids, st)
            return _Outcome(None, _with_details(err, attempt_id=ctx.attempt_id), info)
        partial = result_object(
            ctx, planned, result="partial", steps=done, key=key, comment_id=comment_id,
            attempt_id=ctx.attempt_id, decision_ids=decision_ids, warnings=warnings,
        )  # fmt: skip
        failed_kind = next(s["kind"] for s in done if s["status"] == "failed")
        envelope = TicketctlError(
            "partial",
            f"{self.action.name} partly applied: step {failed_kind} failed ({err.code}); "
            "nothing was rolled back",
            details={"result": partial, "attempt_id": ctx.attempt_id},
            retryable=err.retryable,
            retry_after_s=err.retry_after_s,
            outcome_unknown=unknown,
            site=ctx.site,
            key=key,
        )
        info = self._interrupt_info(planned, done, provider_ids, st)
        return _Outcome(None, envelope, info)

    def _outcome(
        self,
        log: _Audit,
        number: int,
        step: Step,
        result: str,
        *,
        changed: bool | None,
        reason: str | None,
        provider_ids: list[str],
        key: str | None,
        interrupted: PostInfo | None,
    ) -> None:
        """Append an outcome record. The request was made, so a failure here never changes the
        outcome: it warns and marks the result ``audit_incomplete``. An interrupt during the append
        still ends the command, with what the post hook must be told (``interrupted``)."""
        try:
            log.append(
                number, step, result, changed=changed, reason=reason,
                provider_ids=provider_ids, key=key,
            )  # fmt: skip
        except Exception:  # noqa: BLE001 - the request was made; the real outcome must survive
            # Only a warning is possible now (exit codes keep the outcome).
            self.audit_incomplete = True
            self.rt.warn("warning: the audit outcome record could not be written")
        except BaseException as exc:
            raise _Interrupt(exc, interrupted) from None

    @staticmethod
    def _classify(exc: BaseException) -> tuple[TicketctlError, bool]:
        """The envelope error and whether the outcome is unknown (a request may have been sent)."""
        err = from_exception(exc)
        if err is not None:
            return err, err.outcome_unknown
        # Any other exception, an interrupt included: a request may have been sent. The class name
        # is the only detail (its text can hold anything).
        name = type(exc).__name__
        message = f"the outcome is unknown after an unexpected error ({name})"
        return TicketctlError("outcome_unknown", message), True

    @staticmethod
    def _audit_failed(exc: Exception) -> TicketctlError:
        why = exc.reason if isinstance(exc, StateError) else type(exc).__name__
        message = f"the audit record could not be written ({why}); nothing was sent"
        return TicketctlError("internal", message, reason="audit_failed")

    def _retry_rules(self, err: TicketctlError, planned: Planned) -> None:
        """``outcome_unknown`` of comment and create is retryable only with a dedupe key.

        An action whose dedupe lookup does not exist yet sets ``dedupe_lookup = False``: a keyed
        re-run would post again, so the error is not retryable either (default: lookup works).
        """
        if err.code != "outcome_unknown" or self.action.name not in CONFIRM_ACTIONS:
            return
        if planned.dedupe_key is None:
            err.retryable = False
            err.message = f"{err.message}; re-run only with --dedupe-key"
            err.args = (err.message,)
        elif not getattr(self.action, "dedupe_lookup", True):
            err.retryable = False
            err.message = f"{err.message}; a re-run may post the comment again"
            err.args = (err.message,)

    def _post(
        self,
        plan: dict[str, Any],
        planned: Planned,
        ctx: Context,
        decision_ids: list[str],
        outcome: _Outcome,
    ) -> None:
        """The ``post-`` hook: best effort, once, only when an ``attempted`` record was written."""
        if outcome.info is None:
            return
        try:
            self.hooks.post(plan, planned, ctx.attempt_id, decision_ids, outcome.info)
        except Exception:  # noqa: BLE001 - a post hook is never fatal
            with contextlib.suppress(Exception):
                self.rt.warn("warning: a post hook failed")


@dataclass
class _SendState:
    """Where a send stands, for the post hook when an interrupt ends it anywhere."""

    number: int = 0  # the 1-based step in flight (0: none)
    calling: bool = False  # the in-flight step's send was called
    sent: bool = False  # the in-flight step's request returned
    started: bool = False  # an attempted record was written: a post hook is owed
    landed: bool = False
    unknown: bool = False
    error: dict[str, str | None] | None = None
    created_key: str | None = None


class _Interrupt(Exception):
    """An interrupt midway through a send: carries it and what the post hook is told (None: no
    ``attempted`` record exists, so no hook)."""

    def __init__(self, exc: BaseException, info: PostInfo | None) -> None:
        super().__init__("interrupted")
        self.exc = exc
        self.info = info


@dataclass
class _Outcome:
    result: dict[str, Any] | None
    error: TicketctlError | None
    info: PostInfo | None


def _kinds(done: list[dict[str, str]]) -> list[dict[str, str]]:
    return [{"kind": s["kind"], "status": s["status"]} for s in done]


def human(result: Mapping[str, Any]) -> str:
    """Plain text of a plan or a result (no ``--json``)."""
    if "plan_sha256" in result:
        lines = [
            f"plan {result['action']} {result['key'] or '(new)'} on {result['site']}",
            *(f"  step: {s['kind']}" for s in result["steps"]),
            *(f"  change: {c['name']}" for c in result["changes"]),
            *(f"  warning: {w}" for w in result["warnings"]),
            f"  plan_sha256 {result['plan_sha256']}",
        ]
        if not result["steps"]:
            lines.append("  nothing to do (unchanged)")
        return "\n".join(lines)
    return f"{result['action']} {result['key']}: {result['result']} ({result['url']})"
