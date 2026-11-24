"""Hook execution: ``pre-`` and ``post-`` hooks of the write flow (docs/interface.md, Hooks).

:class:`HookRunner` is the real :class:`~ticketctl.writeflow.Hooks` seam. For each phase it takes
the hooks of the action's family in order, configured first and then ``--hook``, builds the JSON
payload once per hook, runs ``/bin/sh -c <cmd>`` through :func:`ticketctl.gateway.run_hook` (own
session and process group, capped output, timeout) and reads the outcome:

* exit 0 continues; exit 13 escalates and the remaining hooks still run; any other exit, a death by
  signal, a crash to start and a timeout is a **veto** at once (``vetoed``, exit 11, the sanitized
  stderr in ``details.hook_stderr``). A ``pre-`` hook that cannot run fails closed.
* stdout is not validated. It is read only for ``decision_id`` (a string or a list) when it parses
  as one JSON object within the size cap; anything else is ignored. An id outside the grammar is
  dropped with one warning. No stdout key can change the plan, the exit code or what is written: the
  decision belongs to the exit status alone, so a hook can never widen what the plan does.
* ``post-`` hooks all run once, best effort: a failure, timeout or crash is a warning, never fatal
  and never a veto. ``async: true`` starts the hook detached (no timeout is enforced on it).

The payload (``schemas/hooks/<hook>.json``) holds ids, names, hashes and sizes and no ticket text.
Its facts about the ticket come from ``Planned.hook`` (set by the action from its resolve read);
every string that comes from the provider passes the sanitizer, and a payload that lacks a
required fact is refused rather than sent short.

The environment is the tool's minus every configured ``token_env`` and ``email_env`` variable (of
every site, not only the selected one) and minus any variable whose value contains a configured
token value,
plus ``TICKETCTL_HOOK``, ``TICKETCTL_ID`` and ``TICKETCTL_OBJECT_TYPE``. Allowlist, not mask: the
child gets nothing else added. Working directory is the caller's.
"""

from __future__ import annotations

import json
import os
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any

from . import audit
from .errors import TicketctlError
from .gateway import HookRun, run_hook, spawn_detached
from .plan import Planned
from .runtime import Runtime
from .sanitize import GRAMMARS, MIN_SECRET_LENGTH, sanitize_field
from .writeflow import MAX_DECISION_IDS, HookResult, PostInfo, hook_family

SHELL = "/bin/sh"
STDERR_BYTES = 4096  # details.hook_stderr
STDOUT_CAP = 65536  # a decision-id document is tiny; more is not read
MAX_IDS_PER_HOOK = 4 * MAX_DECISION_IDS
NAME_CAP = 256
ESCALATE = 13

# The plan keys each hook family adds to ``action``, ``steps`` and ``plan_sha256``.
PLAN_KEYS: dict[str, tuple[str, ...]] = {
    "transition": ("current_status", "target_status", "fields_changed", "resolution", "assign"),
    "update": ("current_status", "fields_changed", "assign"),
    "comment": ("current_status", "dedupe_key"),
    "create": ("fields_changed", "dedupe_key"),
    "sprint": ("current_status",),
    "comment-edit": ("current_status", "comment_id"),
    "comment-delete": ("current_status", "comment_id"),
    "attachment-add": ("current_status",),
}
STATUS_CATEGORIES = ("new", "indeterminate", "done")
SPRINT_STATES = ("future", "active", "closed")
MAX_FIELDS_CHANGED = 200
MAX_SPRINTS = 200


@dataclass(frozen=True)
class Entry:
    """One hook to run: ``source`` is ``config`` or ``flag``."""

    name: str
    cmd: str
    timeout: float
    async_: bool
    source: str


def entries_for(rt: Runtime, phase: str, family: str) -> list[Entry]:
    """The hooks of ``<phase>-<family>``: configured in list order, then ``--hook`` in order."""
    name = f"{phase}-{family}"
    out = [
        Entry(name, h.cmd, h.timeout.total_seconds(), h.async_, "config")
        for h in rt.config.hooks.get(name, [])
    ]
    for spec in rt.g.hooks:
        flag_name, _, cmd = spec.partition("=")
        if flag_name == name:
            out.append(Entry(name, cmd, 30.0, False, "flag"))
    return out


def hook_env(rt: Runtime, name: str, object_id: str | None) -> dict[str, str]:
    """The hook's environment (see the module docstring)."""
    secret_vars: set[str] = set()
    secret_values: set[str] = set()
    for site in rt.config.sites.values():
        for var in (site.token_env, site.email_env):
            if var is None:
                continue
            secret_vars.add(var)
            value = rt.env.get(var, "").strip()
            if len(value) >= MIN_SECRET_LENGTH:
                secret_values.add(value)
    env = {
        k: v
        for k, v in rt.env.items()
        if k not in secret_vars and not any(token in v for token in secret_values)
    }
    env.update(
        {"TICKETCTL_HOOK": name, "TICKETCTL_ID": object_id or "", "TICKETCTL_OBJECT_TYPE": "ticket"}
    )
    return env


# --------------------------------------------------------------------------------------------
# The payload
# --------------------------------------------------------------------------------------------


def _incomplete(what: str) -> TicketctlError:
    return TicketctlError("internal", f"the hook payload lacks a required fact ({what})")


def _name(value: object, what: str, *, cap: int = NAME_CAP) -> str:
    if not isinstance(value, str):
        raise _incomplete(what)
    return sanitize_field(value, cap=cap)[0]


def _name_or_null(value: object, what: str, *, cap: int = NAME_CAP) -> str | None:
    return None if value is None else _name(value, what, cap=cap)


def _status(value: object, what: str) -> dict[str, str] | None:
    if value is None:
        return None
    if not isinstance(value, Mapping):
        raise _incomplete(what)
    category = value.get("category")
    if category not in STATUS_CATEGORIES:
        raise _incomplete(what)
    return {"name": _name(value.get("name"), what), "category": str(category)}


def _account(value: object, what: str) -> str | None:
    if value is None:
        return None
    if not GRAMMARS["account_id"].matches(value):
        raise _incomplete(what)
    return str(value)


def _assign(value: object, what: str) -> str | None:
    """An assign target: ``me``, ``none`` or an account id."""
    if value is None:
        return None
    if value in ("me", "none") or GRAMMARS["account_id"].matches(value):
        return str(value)
    raise _incomplete(what)


def _ticket(value: object) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise _incomplete("ticket")
    subtask, mine = value.get("subtask"), value.get("assignee_is_me")
    if not isinstance(subtask, bool) or not isinstance(mine, bool):
        raise _incomplete("ticket")
    return {
        "type": _name(value.get("type"), "ticket.type"),
        "subtask": subtask,
        "status": _name(value.get("status"), "ticket.status"),
        "priority": _name_or_null(value.get("priority"), "ticket.priority"),
        "assignee_account_id": _account(value.get("assignee_account_id"), "ticket.assignee"),
        "assignee_is_me": mine,
    }


def _create(value: object) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise _incomplete("create")
    parent = value.get("parent")
    if parent is not None and not GRAMMARS["ticket_key"].matches(parent):
        raise _incomplete("create.parent")
    return {
        "type": _name(value.get("type"), "create.type"),
        "parent": parent,
        "assign": _assign(value.get("assign"), "create.assign"),
    }


def _sprints(value: object) -> list[dict[str, Any]]:
    if not isinstance(value, list) or not value or len(value) > MAX_SPRINTS:
        raise _incomplete("sprints")
    out = []
    for item in value:
        if (
            not isinstance(item, Mapping)
            or isinstance(item.get("id"), bool)
            or not isinstance(item.get("id"), int)
            or item["id"] < 0
            or item.get("state") not in SPRINT_STATES
        ):
            raise _incomplete("sprints")
        out.append({"id": item["id"], "state": item["state"]})
    return out


def _fields_changed(facts: Mapping[str, Any], plan: Mapping[str, Any]) -> list[str]:
    raw = facts.get("fields_changed")
    if raw is None:
        raw = [c.get("name") for c in plan.get("changes", []) if isinstance(c, Mapping)]
    if not isinstance(raw, list):
        raise _incomplete("fields_changed")
    names = dict.fromkeys(_name(n, "fields_changed") for n in raw[:MAX_FIELDS_CHANGED])
    return list(names)


def _project(facts: Mapping[str, Any], key: str | None) -> str:
    project = facts.get("project")
    if project is None and key is not None:
        project = key.rpartition("-")[0]
    if not GRAMMARS["project_key"].matches(project):
        raise _incomplete("project")
    return str(project)


def build_payload(
    hook: str,
    plan: Mapping[str, Any],
    planned: Planned,
    attempt_id: str,
    decision_ids: list[str],
    info: PostInfo | None = None,
) -> dict[str, Any]:
    """The stdin document of ``hook`` (``pre-x`` or ``post-x``), ``schemas/hooks/<hook>.json``."""
    phase, _, family = hook.partition("-")
    facts = planned.hook
    key = plan.get("key")
    if family != "create" and not (isinstance(key, str) and audit.TICKET_KEY.fullmatch(key)):
        raise _incomplete("key")
    object_id: str | None = key if family != "create" else None
    if family == "create" and info is not None and info.key is not None:
        object_id = info.key if audit.TICKET_KEY.fullmatch(info.key) else None
    site, actor = plan.get("site"), plan.get("actor")
    if not GRAMMARS["site_name"].matches(site) or not GRAMMARS["actor"].matches(actor):
        raise _incomplete("site")
    steps = [s["kind"] for s in plan.get("steps", [])]
    plan_part: dict[str, Any] = {
        "action": plan["action"],
        "steps": steps,
        "plan_sha256": plan["plan_sha256"],
    }
    for name in PLAN_KEYS[family]:
        if name in ("current_status", "target_status"):
            plan_part[name] = _status(facts.get(name), name)
        elif name == "fields_changed":
            plan_part[name] = _fields_changed(facts, plan)
        elif name == "comment_id":
            cid = facts.get("comment_id")
            if not GRAMMARS["provider_id"].matches(cid):
                raise _incomplete("comment_id")
            plan_part[name] = cid
        elif name == "dedupe_key":
            plan_part[name] = plan.get("dedupe_key")
        elif name == "assign":
            plan_part[name] = _assign(facts.get(name), name)
        else:  # resolution
            plan_part[name] = _name_or_null(facts.get(name), name, cap=128)
    payload: dict[str, Any] = {
        "tool": "ticketctl",
        "hook": hook,
        "object_type": "ticket",
        "object_id": object_id,
        "site": site,
        "project": _project(facts, key if isinstance(key, str) else None),
        "actor": actor,
        "attempt_id": attempt_id,
        "decision_ids": list(decision_ids),
        "plan": plan_part,
        "body": [{"sha256": b["sha256"], "bytes": b["bytes"]} for b in planned.bodies],
    }
    if family == "create":
        payload["create"] = _create(facts.get("create"))
    else:
        payload["ticket"] = _ticket(facts.get("ticket"))
    if family == "sprint":
        payload["sprints"] = _sprints(facts.get("sprints"))
    if phase == "post":
        assert info is not None
        error = None
        if info.error is not None:
            error = {"code": info.error.get("code"), "reason": info.error.get("reason")}
        payload.update(
            {
                "result": info.result,
                "error": error,
                "steps": [{"kind": s["kind"], "status": s["status"]} for s in info.steps],
                "provider_ids": [
                    i for i in info.provider_ids if GRAMMARS["provider_id"].matches(i)
                ],
                "changed": info.changed,
            }
        )
    return payload


# --------------------------------------------------------------------------------------------
# Reading a hook's answer
# --------------------------------------------------------------------------------------------


def stderr_text(raw: bytes) -> str:
    """Sanitized, scrubbed hook stderr of at most ``STDERR_BYTES`` bytes (single line)."""
    text = sanitize_field(raw.decode("utf-8", errors="replace"), cap=STDERR_BYTES)[0]
    encoded = text.encode("utf-8")
    if len(encoded) > STDERR_BYTES:  # multi-byte text: cut again, on a character boundary
        text = encoded[:STDERR_BYTES].decode("utf-8", errors="ignore")
    return text


def read_decision_ids(run: HookRun) -> tuple[list[str], bool]:
    """The valid decision ids of a hook's stdout and whether any value was dropped.

    Only an object within the size cap that parses as strict JSON is read; a hook that printed
    anything else simply returned no id (stdout is not validated).
    """
    if run.stdout_truncated or not run.stdout.strip():
        return [], False
    try:
        document = json.loads(
            run.stdout.decode("utf-8"),
            parse_constant=_reject_constant,
        )
    except (ValueError, RecursionError):
        return [], False
    if not isinstance(document, dict) or "decision_id" not in document:
        return [], False
    raw = document["decision_id"]
    values = raw if isinstance(raw, list) else [raw]
    ids = [v for v in values[:MAX_IDS_PER_HOOK] if GRAMMARS["decision_id"].matches(v)]
    return ids, len(ids) != len(values)


def _reject_constant(name: str) -> Any:
    raise ValueError(name)


# --------------------------------------------------------------------------------------------
# The runner
# --------------------------------------------------------------------------------------------


class HookRunner:
    """The ``Seams.hooks`` implementation. ``run`` and ``spawn`` default to the gateway's."""

    def __init__(
        self,
        rt: Runtime,
        *,
        on_start: Callable[[int], None] | None = None,
        run: Callable[..., HookRun] | None = None,
        spawn: Callable[..., int] | None = None,
    ) -> None:
        self.rt = rt
        self.on_start = on_start
        self._run = run
        self._spawn = spawn
        self._warned_ids = False

    # -- helpers ------------------------------------------------------------------------------
    def _argv(self, entry: Entry) -> list[str]:
        return [SHELL, "-c", entry.cmd]

    def _require_shell(self) -> None:
        if not os.access(SHELL, os.X_OK):
            raise TicketctlError(
                "internal",
                "a hook is configured or given but the shell to run it is not available; "
                "nothing was sent",
                reason="hooks_unavailable",
            )

    def _warn_dropped_ids(self) -> None:
        if not self._warned_ids:
            self._warned_ids = True
            self.rt.warn("warning: a hook returned a decision id that was dropped")

    @staticmethod
    def _merge(*groups: list[str]) -> list[str]:
        grammar = GRAMMARS["decision_id"]
        return list(dict.fromkeys(i for g in groups for i in g if grammar.matches(i)))[
            :MAX_DECISION_IDS
        ]

    # -- pre ----------------------------------------------------------------------------------
    def pre(
        self, plan: dict[str, Any], planned: Planned, attempt_id: str, decision_ids: list[str]
    ) -> HookResult:
        family = hook_family(str(plan["action"]))
        entries = entries_for(self.rt, "pre", family)
        if entries or entries_for(self.rt, "post", family):  # any hook needs the shell, post too
            self._require_shell()
        if not entries:
            return HookResult()
        returned: list[str] = []
        escalated: list[str] = []
        for entry in entries:
            payload = build_payload(
                entry.name, plan, planned, attempt_id, self._merge(decision_ids, returned)
            )
            run = self._execute(entry, payload)
            ids, dropped = read_decision_ids(run) if run is not None else ([], False)
            if dropped:
                self._warn_dropped_ids()
            returned.extend(ids)
            if run is None or run.timed_out or run.returncode not in (0, ESCALATE):
                raise self._veto(entry, run, self._merge(decision_ids, returned))
            if run.returncode == ESCALATE:
                escalated.append(entry.name)
        return HookResult(returned, escalated)

    def _execute(self, entry: Entry, payload: dict[str, Any]) -> HookRun | None:
        """Run one hook to completion; None when it could not even be started."""
        stdin = json.dumps(payload, ensure_ascii=True, separators=(",", ":")).encode("ascii")
        env = hook_env(self.rt, entry.name, payload["object_id"])
        try:
            return (self._run or run_hook)(
                self._argv(entry),
                stdin=stdin,
                env=env,
                timeout=entry.timeout,
                out_cap=STDOUT_CAP,
                err_cap=STDOUT_CAP,
                on_start=self.on_start,
            )
        except (OSError, ValueError):  # ValueError: Popen refuses e.g. an embedded NUL
            return None

    @staticmethod
    def _veto(entry: Entry, run: HookRun | None, decision_ids: list[str]) -> TicketctlError:
        if run is None:
            why = "could not be started"
        elif run.timed_out:
            why = "timed out"
        elif run.returncode < 0:
            why = "was killed by a signal"
        else:
            why = f"exited {run.returncode}"
        details: dict[str, Any] = {"hook": entry.name, "decision_ids": decision_ids}
        stderr = stderr_text(run.stderr) if run is not None else ""
        if stderr:
            details["hook_stderr"] = stderr
        return TicketctlError(
            "vetoed", f"hook {entry.name} vetoed the write: it {why}", details=details
        )

    # -- post ---------------------------------------------------------------------------------
    def post(
        self,
        plan: dict[str, Any],
        planned: Planned,
        attempt_id: str,
        decision_ids: list[str],
        info: PostInfo,
    ) -> None:
        family = hook_family(str(plan["action"]))
        for entry in entries_for(self.rt, "post", family):
            try:
                self._post_one(entry, plan, planned, attempt_id, decision_ids, info)
            except Exception:  # noqa: BLE001 - a post hook is never fatal and never stops the rest
                self.rt.warn(f"warning: {entry.name} could not be run")

    def _post_one(
        self,
        entry: Entry,
        plan: dict[str, Any],
        planned: Planned,
        attempt_id: str,
        decision_ids: list[str],
        info: PostInfo,
    ) -> None:
        payload = build_payload(entry.name, plan, planned, attempt_id, decision_ids, info)
        if entry.async_:
            stdin = json.dumps(payload, ensure_ascii=True, separators=(",", ":")).encode("ascii")
            env = hook_env(self.rt, entry.name, payload["object_id"])
            (self._spawn or spawn_detached)(
                self._argv(entry), stdin=stdin, env=env, on_start=self.on_start
            )
            return
        run = self._execute(entry, payload)
        if run is None:
            self.rt.warn(f"warning: {entry.name} could not be started")
        elif run.timed_out:
            self.rt.warn(f"warning: {entry.name} timed out")
        elif run.returncode != 0:
            code = "was killed by a signal" if run.returncode < 0 else f"exited {run.returncode}"
            self.rt.warn(f"warning: {entry.name} {code}")
