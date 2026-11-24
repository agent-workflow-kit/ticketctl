"""Bulk writes: several tickets under one plan, one confirmation and one combined hash (D-46).

A batch is a driver over the unchanged single-ticket ``WriteAction`` and the unchanged engine
(``writeflow.py``); it adds no way to write. It has two phases.

1. **Plan (nothing is written).** For each key, in order, ``writeflow.preplan`` runs steps 1 to 3
   of the write flow and the ``before_confirm`` hooks of step 5a. Any refusal of any ticket is
   that error (with the key and the position in the message) and the batch ends: nothing was
   sent. The combined plan lists every ticket's own plan object and one hash over the ordered
   list of key and plan hash.
2. **Run.** After one confirmation (``--yes --expect-plan`` of the combined hash, or one answer
   on ``/dev/tty``), each ticket runs the real write flow, one at a time, in order, with ``--yes``
   and its own plan hash as ``--expect-plan``: its stale check, ``after_confirm`` hooks, ``post-``
   hook, lock, audit pair, attribution and read-only guard are those of a single run. The first
   ticket that ends in any error stops the batch; the rest are ``not_run``. Nothing is retried.

The batch has one audit pair (``BatchAudit``) written once the batch is confirmed and before the
first ticket runs; the tickets' records carry its ``attempt_id`` as ``inputs.batch_id``.
"""

from __future__ import annotations

import dataclasses
import hashlib
from collections.abc import Callable, Sequence
from typing import Any

from . import confirm_dialog, confirm_tty, selection, writeflow
from .errors import TicketctlError, from_exception, read_only_error, usage_error
from .plan import canonical_bytes
from .runtime import Runtime
from .sanitize import read_input
from .writeflow import (
    BatchAudit,
    HookResult,
    Hooks,
    PostInfo,
    Preplan,
    Seams,
    WriteAction,
)

HARD_MAX = 200  # the schema's ceiling for limits.bulk_max
STATUSES = ("ok", "unchanged", "failed", "outcome_unknown", "not_run")


# --------------------------------------------------------------------------------------------
# The key list
# --------------------------------------------------------------------------------------------


def _invalid(message: str) -> TicketctlError:
    return TicketctlError("invalid_request", message, reason="invalid_value")


def collect_refs(rt: Runtime, keys: Sequence[str], keys_file: str | None) -> list[str]:
    """The key references as typed: the positional keys, or the lines of ``--keys-file``."""
    refs = list(keys)
    if keys_file is not None:
        if refs:
            raise usage_error(
                "invalid_combination", "give the keys as arguments or with --keys-file, not both"
            )
        text = read_input(keys_file, kind="text", body_chars=rt.config.limits.body_chars)
        # only a newline ends a line: a Unicode separator inside a line stays in it, so the line
        # is judged as a whole (a key with a separator is ``invalid_key``, a comment stays one)
        for line in text.replace("\r\n", "\n").replace("\r", "\n").split("\n"):
            line = line.strip()
            if line and not line.startswith("#"):
                refs.append(line)
    if not refs:
        raise usage_error("missing_option", "give at least one key, or --keys-file")
    return refs


def cap_for(rt: Runtime, max_tickets: int | None) -> int:
    """``limits.bulk_max`` (default 50); ``--max-tickets`` can only lower it."""
    cap = rt.config.limits.bulk_max
    if max_tickets is not None:
        if not 1 <= max_tickets <= cap:
            raise usage_error(
                "invalid_argument",
                f"--max-tickets takes 1 to {cap} (limits.bulk_max); it can only lower the cap",
            )
        return max_tickets
    return cap


def resolve_keys(
    rt: Runtime, refs: Sequence[str], *, cap: int, site_flag: str | None
) -> tuple[str, list[str]]:
    """``(site, keys)``: the cap on the whole list first, then every key and the one site, then
    duplicates (a repeated key is refused, never merged)."""
    if len(refs) > cap:
        raise _invalid(f"{len(refs)} keys given; this run accepts at most {cap} (limits.bulk_max)")
    keys: list[str] = []
    for ref in refs:
        picked = selection.select_site(rt.config, site_flag=site_flag, refs=[ref])
        assert picked.key is not None
        keys.append(picked.key)
    chosen = selection.select_site(rt.config, site_flag=site_flag, refs=list(refs))
    seen: set[str] = set()
    for key in keys:
        if key in seen:
            raise usage_error("invalid_argument", f"the key {key} is given more than once")
        seen.add(key)
    return chosen.site, keys


# --------------------------------------------------------------------------------------------
# The combined plan
# --------------------------------------------------------------------------------------------


def batch_hash(action: str, site: str, plans: Sequence[dict[str, Any]]) -> str:
    document = {
        "v": 1,
        "bulk": True,
        "action": action,
        "site": site,
        "tickets": [{"key": p["key"], "plan_sha256": p["plan_sha256"]} for p in plans],
    }
    return hashlib.sha256(canonical_bytes(document)).hexdigest()


def combined_plan(action: str, site: str, actor: str, pre: Sequence[Preplan]) -> dict[str, Any]:
    plans = [p.plan for p in pre]
    return {
        "action": action,
        "site": site,
        "actor": actor,
        "count": len(plans),
        "changing": sum(1 for p in pre if p.planned.steps),
        "tickets": plans,
        "plan_sha256": batch_hash(action, site, plans),
    }


# --------------------------------------------------------------------------------------------
# Replaying the early hooks
# --------------------------------------------------------------------------------------------


class _ReplayedEarly:
    """The hooks of a ticket's own run: ``before_confirm`` already ran in the plan phase (before
    the one question), so it returns what it decided and runs nothing again."""

    def __init__(self, inner: Hooks, decided: Preplan) -> None:
        self.inner = inner
        self.decided = decided

    def pre(
        self, plan: dict[str, Any], planned: Any, attempt_id: str, decision_ids: list[str]
    ) -> HookResult:
        return self.inner.pre(plan, planned, attempt_id, decision_ids)

    def post(
        self,
        plan: dict[str, Any],
        planned: Any,
        attempt_id: str,
        decision_ids: list[str],
        info: PostInfo,
    ) -> None:
        self.inner.post(plan, planned, attempt_id, decision_ids, info)

    def before_confirm(
        self, plan: dict[str, Any], planned: Any, attempt_id: str, decision_ids: list[str]
    ) -> HookResult:
        return HookResult(
            decision_ids=list(self.decided.early_decision_ids), escalated=[], ran=False
        )


# --------------------------------------------------------------------------------------------
# The run
# --------------------------------------------------------------------------------------------


def _annotate(err: TicketctlError, key: str, index: int, count: int) -> TicketctlError:
    err.message = f"ticket {key} ({index} of {count}): {err.message}"
    err.args = (err.message,)
    err.key = err.key or key
    return err


def _confirm(rt: Runtime, seams: Seams, plan: dict[str, Any], early: bool) -> None:
    """One question for the batch (same rules as the engine's ``_confirm``)."""
    g = rt.g
    interactive = (seams.interactive or (lambda: writeflow.is_interactive(g)))()
    auto = (g.resolved_actor or "").startswith("auto:")
    if interactive and not g.yes and not auto:
        if seams.confirm is not None:
            accepted = seams.confirm(plan)
        elif seams.interactive is None:
            family = writeflow.hook_family(plan["action"])
            names = [
                f"{phase}-{family}"
                for phase in ("pre", "post")
                if rt.config.hooks.get(f"{phase}-{family}")
            ]
            names = list(dict.fromkeys([*names, *writeflow.cli_hook_names(rt)]))
            text = confirm_dialog.compose_batch(
                plan,
                override=bool(g.override_escalation),
                hook_names=names,
                early_hooks=early,
            )
            try:
                accepted = confirm_tty.confirm(plan, (), text=text)
            except confirm_tty.ConfirmUnavailable:
                raise _confirmation_required(plan) from None
        else:
            raise _confirmation_required(plan)
        if not accepted:
            raise TicketctlError("declined", "the plan was declined", details={"plan": plan})
        return
    if not g.yes:
        raise _confirmation_required(plan)


def _confirmation_required(plan: dict[str, Any]) -> TicketctlError:
    return TicketctlError(
        "confirmation_required",
        "a human must confirm; re-run with --yes --expect-plan " + plan["plan_sha256"],
        details={"plan": plan},
    )


def _entry(key: str, status: str, result: dict[str, Any] | None, error: Any) -> dict[str, Any]:
    return {"key": key, "status": status, "result": result, "error": error}


def _landed(entry: dict[str, Any]) -> bool:
    """A write of this ticket landed (``ok``, or an error whose own result says a step did)."""
    if entry["status"] == "ok":
        return True
    details = (entry["error"] or {}).get("details") or {}
    return bool((details.get("result") or {}).get("changed"))


def execute_batch(
    rt: Runtime,
    site: str,
    factory: Callable[[str], WriteAction],
    keys: Sequence[str],
    *,
    action: str,
    seams: Seams,
) -> dict[str, Any]:
    """Plan, confirm once and run the batch; see the module docstring."""
    g = rt.g
    if rt.config.read_only and not g.dry_run:
        raise read_only_error()
    if g.run_hooks:
        raise usage_error("invalid_combination", "--run-hooks is not supported for a batch")
    if g.yes and not g.dry_run and g.expect_plan is None:
        raise usage_error("yes_requires_expect_plan", "--yes always needs --expect-plan")
    actor = g.resolved_actor
    if actor is None:
        raise ValueError("the actor must be resolved before a write")
    inner = seams.hooks if seams.hooks is not None else writeflow.real_hooks(rt)
    batch_id = seams.attempt_id()

    # Phase 1: reads and the before_confirm hooks only; any refusal ends the batch.
    pre: list[Preplan] = []
    planning = dataclasses.replace(seams, hooks=inner)
    for i, key in enumerate(keys, 1):
        try:
            one = writeflow.preplan(rt, site, factory(key), planning, hooks=not g.dry_run)
        except Exception as exc:
            err = from_exception(exc)
            if err is None:
                raise
            raise _annotate(err, key, i, len(keys)) from exc
        if one.planned.shown:  # fail closed: a batch dialog cannot show outbound text
            raise ValueError("a bulk action must not carry outbound text")
        pre.append(one)
    plan = combined_plan(action, site, actor, pre)
    if g.dry_run:
        return plan
    changing = plan["changing"] > 0
    if changing:
        if g.expect_plan is not None and g.expect_plan != plan["plan_sha256"]:
            raise TicketctlError(
                "stale_ticket",
                "the plan differs from --expect-plan; re-plan with --dry-run",
                details={"plan": plan},
            )
        _confirm(rt, seams, plan, early=any(p.early_ran for p in pre))

    log: BatchAudit | None = None
    if changing:
        log = BatchAudit(rt, site, action, batch_id, plan["plan_sha256"], len(keys))
        log.attempted()

    # Phase 2: one ticket at a time, each a complete single run.
    entries: list[dict[str, Any]] = []
    stop: TicketctlError | None = None
    stop_key: str | None = None
    try:
        for key, one in zip(keys, pre, strict=True):
            if stop is not None:
                entries.append(_entry(key, "not_run", None, None))
                continue
            ticket_g = dataclasses.replace(
                g,
                yes=True,
                dry_run=False,
                run_hooks=False,
                expect_plan=one.plan["plan_sha256"],
            )
            own = dataclasses.replace(
                seams,
                hooks=_ReplayedEarly(inner, one),
                attempt_id=(lambda a=one.attempt_id: a),
                batch_id=batch_id if changing else None,
            )
            try:
                result = writeflow.execute(
                    dataclasses.replace(rt, g=ticket_g), site, factory(key), own
                )
            except Exception as exc:
                err = from_exception(exc) or _unknown_after(exc)
                status = "outcome_unknown" if err.outcome_unknown else "failed"
                entries.append(_entry(key, status, None, err.body()))
                stop, stop_key = err, key
                continue
            entries.append(_entry(key, result["result"], result, None))
    except BaseException:
        if log is not None:  # an interrupt: what happened to the rest is unknown
            log.outcome("unknown", None, None)
        raise

    counts = {s: sum(1 for e in entries if e["status"] == s) for s in STATUSES}
    landed = any(_landed(e) for e in entries)
    complete = stop is None
    warnings: list[str] = []
    if log is not None:
        if complete:
            written = log.outcome("ok", landed, None)
        elif stop is not None and stop.outcome_unknown:
            written = log.outcome("unknown", None, stop.code)
        else:
            written = log.outcome("partial" if landed else "failed", landed, _code(stop))
        if not written:
            warnings.append("audit_incomplete")
    result_doc = {
        "action": action,
        "site": site,
        "count": len(keys),
        "plan_sha256": plan["plan_sha256"],
        "complete": complete,
        "changed": landed,
        "counts": counts,
        "tickets": entries,
        "attempt_id": batch_id if log is not None else None,
        "warnings": warnings,
    }
    if stop is None:
        return result_doc
    raise _incomplete(stop, stop_key, result_doc, landed, site, batch_id if log else None)


def _unknown_after(exc: BaseException) -> TicketctlError:
    """An exception that is not a ticketctl error, raised out of one ticket's run: the write may
    have landed before it, so the ticket is ``outcome_unknown`` (never counted ``ok``, never resent)
    and the message carries the class only, like ``internal_error``."""
    return TicketctlError(
        "outcome_unknown",
        f"unexpected internal error ({type(exc).__name__}); the write of this ticket may have "
        "landed, check the ticket before re-running",
    )


def _code(err: TicketctlError | None) -> str | None:
    return err.code if err is not None else None


def _incomplete(
    stop: TicketctlError,
    key: str | None,
    result_doc: dict[str, Any],
    landed: bool,
    site: str,
    batch_id: str | None,
) -> TicketctlError:
    """The envelope of a batch that did not complete: exit 12 ``partial`` when any ticket's write
    landed, else the stopping ticket's own error; both carry the whole result."""
    if landed:
        details: dict[str, Any] = {"result": result_doc}
        if batch_id is not None:
            details["attempt_id"] = batch_id
        return TicketctlError(
            "partial",
            f"the batch stopped at {key}: {stop.message}",
            details=details,
            site=site,
            key=key,
            retryable=stop.retryable,
            retry_after_s=stop.retry_after_s,
            outcome_unknown=stop.outcome_unknown,
        )
    stop.details = {**(stop.details or {}), "result": result_doc}
    if batch_id is not None:  # the batch's id, not the stopping ticket's own
        stop.details["attempt_id"] = batch_id
    stop.key = stop.key or key
    stop.site = stop.site or site
    return stop


# --------------------------------------------------------------------------------------------
# Plain text
# --------------------------------------------------------------------------------------------


def human(doc: dict[str, Any]) -> str:
    """Plain text of a batch plan or a batch result (no ``--json``)."""
    tickets = doc["tickets"]
    if "changing" in doc:
        lines = [
            f"plan {doc['action']} of {doc['count']} tickets on {doc['site']} "
            f"({doc['changing']} to change)"
        ]
        for t in tickets:
            lines.append(
                f"  {t['key']}: " + (", ".join(s["kind"] for s in t["steps"]) or "unchanged")
            )
            lines.extend(f"    change: {c['name']}" for c in t["changes"])
            lines.extend(f"    warning: {w}" for w in t["warnings"])
        lines.append(f"  plan_sha256 {doc['plan_sha256']}")
        return "\n".join(lines)
    c = doc["counts"]
    lines = [
        f"{doc['action']} of {doc['count']} tickets on {doc['site']}: "
        + ", ".join(f"{c[s]} {s}" for s in STATUSES if c[s])
    ]
    lines.extend(f"  {t['key']}: {t['status']}" for t in tickets)
    return "\n".join(lines)
