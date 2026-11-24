"""``audit.jsonl`` and the request log ``log.jsonl``: validated, sanitized, flock-guarded appends.

Rules: docs/interface.md "Files" and "Audit and log"; shapes: schemas/audit.json, schemas/log.json.

Public API:

``append_audit(state_dir, record, *, now=None, lock_timeout=...) -> dict``
    Append one audit record and return it as written. The caller (the write flow) passes every
    schema field except ``v`` and ``seq``; ``ts`` is optional (default: now, UTC, to the second).
    ``seq`` is assigned here under the flock: 1 plus the ``seq`` of the last line, read from the
    file tail; if the tail line does not parse (or its ``seq`` is not a positive integer) the
    maximum ``seq`` found by scanning the whole file is used instead. If the last byte of the file
    is not a newline (a torn tail) a newline is written first, in the same write as the record. A
    record whose ``result`` is ``attempted`` is ``fsync``'d. Raises :class:`StateError` (exit 1
    ``internal``) on any failure; per the interface the caller then refuses to send the write (for
    an ``attempted`` record) or only warns (for an outcome record).

``append_log(state_dir, record, *, warn=..., max_bytes=LOG_MAX_BYTES, now=None) -> str | None``
    The request-log sink the gateway calls once per provider call. ``record`` holds ``site``,
    ``host``, ``config_sha256``, ``method``, ``path`` (the full path template with placeholders,
    never values), ``status`` (None when no response arrived), ``duration_ms``, ``retries`` and
    ``rate_limit_remaining``; ``v`` and ``ts`` are added here. It never raises: a failed write
    (including an invalid record) calls ``warn`` with a one-line message (default: stderr) and
    returns that message; success returns None. The message names the reason, never a value.
    When the live file would exceed ``max_bytes`` (10 MiB) it is rotated to ``log.jsonl.1`` (older
    archives shift up, the third is dropped) under the same flock, so no line is lost.

Both writers are closed over the schemas' key sets: a header, body, title or token field cannot be
written because no such key exists, and each free-text value is checked against its schema
pattern. Every string that passes is then sanitized and scrubbed (``sanitize.render``), and the
JSON is written with ``ensure_ascii=True``. Every writer holds the flock for its whole append.

``export`` (``exportcmd.py``) is the deliberate exception: it only reads ``audit.jsonl``, without
the flock, with the same tolerance.
"""

from __future__ import annotations

import json
import os
import re
import sys
from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from pathlib import Path

from ticketctl import state
from ticketctl.sanitize import render
from ticketctl.state import StateError

AUDIT_FILE = "audit.jsonl"
LOG_FILE = "log.jsonl"
LOG_MAX_BYTES = 10 * 1024 * 1024
LOG_ARCHIVES = 3
MAX_RECORD_BYTES = 64 * 1024
_TAIL_BYTES = 64 * 1024
_SCAN_LINE = 1024 * 1024
_ROTATION_TRIES = 5
MAX_SEQ = 2**53

# --- the schemas, mirrored (tests/test_audit.py compares each to schemas/) ----------------------

TS = re.compile(r"^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z$")
SITE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")
SHA256 = re.compile(r"^[0-9a-f]{64}$")
ATTEMPT_ID = re.compile(r"^[A-Za-z0-9._-]{8,64}$")
ACTOR = re.compile(r"^(?:human|auto):[A-Za-z0-9._@-]{1,64}$")
DECISION_ID = re.compile(r"^[A-Za-z0-9._:@/-]{1,128}$")
PROVIDER_ID = re.compile(r"^[A-Za-z0-9_.:-]{1,64}$")
TICKET_KEY = re.compile(r"^[A-Z][A-Z0-9_]*-[0-9]+$")
HOST_PATTERN = r"^(?:[a-z0-9.-]{1,253}|\[[0-9a-f:]{2,45}\])(?::[0-9]{1,5})?$"
LOG_HOST = re.compile(HOST_PATTERN)
LOG_PATH = re.compile(r"^(?:/(?:[A-Za-z0-9._~-]+|\{[a-z_]+\}))+$")
LOG_PATH_MAX = 256
TOKEN_ENV = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,127}$")

RESULTS = ("attempted", "ok", "failed", "partial", "unknown")
ACTOR_SOURCES = ("flag", "tty", "default")
STEP_KINDS = (
    "assign", "transition", "set_fields", "add_comment", "create", "sprint_add", "sprint_remove",
    "add_link", "remove_link", "edit_comment", "delete_comment", "add_attachment", "set_resolution",
)  # fmt: skip
ACTIONS = (
    "transition", "comment", "create", "update", "sprint_add", "sprint_remove", "link_add",
    "link_remove", "comment_edit", "comment_delete", "attachment_add", "resolution_backfill",
)  # fmt: skip
HOOK_NAMES = (
    "pre-transition", "post-transition", "pre-comment", "post-comment", "pre-create",
    "post-create", "pre-update", "post-update", "pre-sprint", "post-sprint", "pre-link",
    "post-link", "pre-comment-edit", "post-comment-edit", "pre-comment-delete",
    "post-comment-delete", "pre-attachment-add", "post-attachment-add", "pre-resolution-backfill",
    "post-resolution-backfill",
)  # fmt: skip
ERROR_CODES = (
    "internal", "usage", "invalid_config", "not_found", "vetoed", "invalid_request",
    "auth_required", "bad_credentials", "forbidden", "rejected", "unavailable", "timeout",
    "outcome_unknown", "rate_limited", "provider_changed", "partial", "lookup_incomplete",
    "confirmation_required", "escalated", "stale_ticket", "precondition_failed",
    "no_such_transition", "sprint_closed", "already_exists", "declined", "read_only",
    "ambiguous_site", "ambiguous_field", "ambiguous_transition", "ambiguous_board",
    "ambiguous_sprint", "not_ticketctl_comment", "restricted_comment", "human_comment",
)  # fmt: skip
LOG_METHODS = ("GET", "POST", "PUT", "DELETE")

AUDIT_KEYS = (
    "v", "seq", "ts", "attempt_id", "step", "step_kind", "actor", "actor_source", "stdin_tty",
    "decision_id", "target", "action", "inputs", "result", "provider_ids", "changed", "reason",
)  # fmt: skip
AUDIT_REQUIRED = tuple(k for k in AUDIT_KEYS if k != "reason")
INPUTS_KEYS = (
    "plan_sha256", "bodies", "dedupe_key", "override_escalation", "config", "hooks_sha256",
    "site_host", "cli_hooks", "token_env",
)  # fmt: skip
INPUTS_REQUIRED = (
    "plan_sha256", "override_escalation", "config", "hooks_sha256", "site_host", "cli_hooks",
    "token_env",
)  # fmt: skip
LOG_KEYS = (
    "v", "ts", "site", "method", "path", "status", "duration_ms", "retries",
    "rate_limit_remaining", "host", "config_sha256",
)  # fmt: skip
LOG_REQUIRED = LOG_KEYS


# --- validation --------------------------------------------------------------------------------


def _bad(what: str) -> StateError:
    return StateError("record_invalid", f"record field {what} is invalid")


def _is_int(value: object, minimum: int = 0, maximum: int | None = None) -> bool:
    return (
        isinstance(value, int)
        and not isinstance(value, bool)
        and value >= minimum
        and (maximum is None or value <= maximum)
    )


def valid_seq(value: object) -> bool:
    """True for a ``seq`` the writer accepts: an integer (not a bool) from 1 to 2**53."""
    return _is_int(value, 1, MAX_SEQ)


def _text(value: object, pattern: re.Pattern[str], what: str, maximum: int | None = None) -> None:
    if not isinstance(value, str) or not pattern.fullmatch(value):
        raise _bad(what)
    if maximum is not None and len(value) > maximum:
        raise _bad(what)


def _enum(value: object, allowed: tuple[str, ...], what: str) -> None:
    if not isinstance(value, str) or value not in allowed:
        raise _bad(what)


def _list_of(
    value: object, what: str, check: Callable[[object], None], maximum: int | None = None
) -> None:
    if not isinstance(value, list) or (maximum is not None and len(value) > maximum):
        raise _bad(what)
    for item in value:
        check(item)


def _object(
    value: object, what: str, keys: tuple[str, ...], required: tuple[str, ...]
) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise _bad(what)
    if not set(value) <= set(keys) or not set(required) <= set(value):
        raise _bad(what)
    return value


def _nullable(value: object, check: Callable[[object], None]) -> None:
    if value is not None:
        check(value)


def _validate_inputs(value: object) -> None:
    inputs = _object(value, "inputs", INPUTS_KEYS, INPUTS_REQUIRED)
    _text(inputs["plan_sha256"], SHA256, "inputs.plan_sha256")
    if "bodies" in inputs:

        def body(item: object) -> None:
            b = _object(item, "inputs.bodies", ("sha256", "bytes"), ("sha256", "bytes"))
            _text(b["sha256"], SHA256, "inputs.bodies")
            if not _is_int(b["bytes"]):
                raise _bad("inputs.bodies")

        _list_of(inputs["bodies"], "inputs.bodies", body)
    if "dedupe_key" in inputs:
        _nullable(inputs["dedupe_key"], lambda v: _text(v, DECISION_ID, "inputs.dedupe_key"))
    if inputs["override_escalation"] not in (None, "ticketctl"):
        raise _bad("inputs.override_escalation")
    config = _object(
        inputs["config"], "inputs.config", ("path", "files_sha256", "sha256"),
        ("path", "files_sha256", "sha256"),
    )  # fmt: skip
    path = config["path"]
    if path is not None and (not isinstance(path, str) or len(path) > 4096):
        raise _bad("inputs.config.path")
    _list_of(
        config["files_sha256"], "inputs.config.files_sha256",
        lambda v: _text(v, SHA256, "inputs.config.files_sha256"),
    )  # fmt: skip
    _text(config["sha256"], SHA256, "inputs.config.sha256")
    _text(inputs["hooks_sha256"], SHA256, "inputs.hooks_sha256")
    _text(inputs["site_host"], LOG_HOST, "inputs.site_host", 300)
    hooks = inputs["cli_hooks"]
    _list_of(hooks, "inputs.cli_hooks", lambda v: _enum(v, HOOK_NAMES, "inputs.cli_hooks"))
    assert isinstance(hooks, list)
    if len(set(hooks)) != len(hooks):
        raise _bad("inputs.cli_hooks")
    _text(inputs["token_env"], TOKEN_ENV, "inputs.token_env")


def _validate_audit(record: Mapping[str, object]) -> None:
    keys = tuple(k for k in AUDIT_KEYS if k not in ("v", "seq"))
    required = tuple(k for k in AUDIT_REQUIRED if k not in ("v", "seq", "ts"))
    _object(record, "record", keys, required)
    if "ts" in record:
        _text(record["ts"], TS, "ts")
    _text(record["attempt_id"], ATTEMPT_ID, "attempt_id")
    if not _is_int(record["step"], 1):
        raise _bad("step")
    _enum(record["step_kind"], STEP_KINDS, "step_kind")
    _text(record["actor"], ACTOR, "actor")
    _enum(record["actor_source"], ACTOR_SOURCES, "actor_source")
    if not isinstance(record["stdin_tty"], bool):
        raise _bad("stdin_tty")
    _list_of(
        record["decision_id"], "decision_id", lambda v: _text(v, DECISION_ID, "decision_id"), 16
    )
    target = _object(record["target"], "target", ("site", "key"), ("site", "key"))
    _text(target["site"], SITE, "target.site")
    _nullable(target["key"], lambda v: _text(v, TICKET_KEY, "target.key", 64))
    _enum(record["action"], ACTIONS, "action")
    _validate_inputs(record["inputs"])
    result = record["result"]
    _enum(result, RESULTS, "result")
    ids = record["provider_ids"]
    _list_of(ids, "provider_ids", lambda v: _text(v, PROVIDER_ID, "provider_ids"))
    changed = record["changed"]
    if result in ("attempted", "unknown"):
        if changed is not None:
            raise _bad("changed")
    elif not isinstance(changed, bool):
        raise _bad("changed")
    assert isinstance(ids, list)
    if result == "attempted" and ids:
        raise _bad("provider_ids")
    if "reason" in record:
        _nullable(record["reason"], lambda v: _enum(v, ERROR_CODES, "reason"))


def _validate_log(record: Mapping[str, object]) -> None:
    keys = tuple(k for k in LOG_KEYS if k not in ("v", "ts"))
    _object(record, "record", keys, keys)
    if "ts" in record:
        _text(record["ts"], TS, "ts")
    _text(record["site"], SITE, "site")
    _enum(record["method"], LOG_METHODS, "method")
    _text(record["path"], LOG_PATH, "path", LOG_PATH_MAX)
    status = record["status"]
    if status is not None and not _is_int(status, 100, 599):
        raise _bad("status")
    if not _is_int(record["duration_ms"]) or not _is_int(record["retries"]):
        raise _bad("duration_ms")
    remaining = record["rate_limit_remaining"]
    if remaining is not None and not _is_int(remaining):
        raise _bad("rate_limit_remaining")
    _text(record["host"], LOG_HOST, "host", 300)
    _text(record["config_sha256"], SHA256, "config_sha256")


# --- encoding ----------------------------------------------------------------------------------


def _timestamp(now: datetime | None) -> str:
    moment = (now or datetime.now(UTC)).astimezone(UTC)
    return moment.strftime("%Y-%m-%dT%H:%M:%SZ")


def _encode(ordered: dict[str, object]) -> bytes:
    try:
        text = json.dumps(ordered, ensure_ascii=True, separators=(",", ":"), allow_nan=False)
    except (ValueError, TypeError, RecursionError) as exc:
        raise StateError("record_invalid", "record cannot be encoded as JSON") from exc
    return text.encode("ascii") + b"\n"


def _ordered(
    keys: tuple[str, ...], cleaned: Mapping[str, object], first: dict[str, object]
) -> dict[str, object]:
    out = dict(first)
    for key in keys:
        if key in cleaned and key not in out:
            out[key] = cleaned[key]
    return out


# --- audit -------------------------------------------------------------------------------------


def _seq_of(line: bytes) -> int | None:
    try:
        doc = json.loads(line)
    except (ValueError, RecursionError):
        return None
    seq = doc.get("seq") if isinstance(doc, dict) else None
    return seq if _is_int(seq, 1, MAX_SEQ) else None


def _scan_max_seq(fd: int) -> int:
    best = 0
    with os.fdopen(os.dup(fd), "rb") as fh:
        fh.seek(0)
        while True:
            piece = fh.readline(_SCAN_LINE)
            if not piece:
                return best
            if not piece.endswith(b"\n"):
                if len(piece) == _SCAN_LINE:  # an over-long line: drop the rest of it
                    while (rest := fh.readline(_SCAN_LINE)) and not rest.endswith(b"\n"):
                        pass
                    continue
            seq = _seq_of(piece)
            if seq is not None:
                best = max(best, seq)


def _next_seq(fd: int, size: int) -> tuple[int, bytes]:
    """The next ``seq`` and the repair prefix (a newline when the tail is torn)."""
    if size == 0:
        return 1, b""
    start = max(0, size - _TAIL_BYTES)
    tail = os.pread(fd, size - start, start)
    prefix = b"" if tail.endswith(b"\n") else b"\n"
    body = tail[:-1] if tail.endswith(b"\n") else tail
    cut = body.rfind(b"\n")
    last = None if cut == -1 and start > 0 else body[cut + 1 :]
    seq = _seq_of(last) if last is not None else None
    if seq is None:
        seq = _scan_max_seq(fd)
    if seq >= MAX_SEQ:
        raise StateError("state_io", "the audit sequence is exhausted")
    return seq + 1, prefix


def append_audit(
    state_dir: Path,
    record: Mapping[str, object],
    *,
    now: datetime | None = None,
    lock_timeout: float = state.LOCK_TIMEOUT,
) -> dict[str, object]:
    """Append one audit record under the flock and return the record as written."""
    _validate_audit(record)
    cleaned = state.clean_tree(record)
    assert isinstance(cleaned, dict)
    _validate_audit(cleaned)  # scrubbing and rendering must not break the schema
    dir_fd = state.open_dir(state_dir)
    if dir_fd is None:
        raise StateError("state_io", "cannot open the state directory")
    try:
        with state.locked_append_file(dir_fd, AUDIT_FILE, timeout=lock_timeout) as fd:
            size = os.fstat(fd).st_size
            seq, prefix = _next_seq(fd, size)
            first: dict[str, object] = {
                "v": 1,
                "seq": seq,
                "ts": cleaned.get("ts") or _timestamp(now),
            }
            written = _ordered(AUDIT_KEYS, cleaned, first)
            line = _encode(written)
            if len(line) > MAX_RECORD_BYTES:
                raise StateError("record_too_large", "audit record is too large")
            state.write_all(fd, prefix + line)
            if record.get("result") == "attempted":
                _fsync(fd)
                if size == 0:
                    _fsync(dir_fd)
            return written
    finally:
        os.close(dir_fd)


def _fsync(fd: int) -> None:
    try:
        os.fsync(fd)
    except OSError as exc:
        raise StateError("state_io", "cannot sync the audit log") from exc


# --- request log -------------------------------------------------------------------------------


def _stderr_warning(message: str) -> None:
    try:
        sys.stderr.write(message + "\n")
    except (OSError, ValueError):
        pass


def _rotate(dir_fd: int) -> None:
    """Shift ``log.jsonl`` into the archives; the caller holds the flock on the live file."""
    for i in range(LOG_ARCHIVES - 1, 0, -1):
        try:
            os.rename(
                f"{LOG_FILE}.{i}", f"{LOG_FILE}.{i + 1}", src_dir_fd=dir_fd, dst_dir_fd=dir_fd
            )
        except FileNotFoundError:
            pass
    os.rename(LOG_FILE, f"{LOG_FILE}.1", src_dir_fd=dir_fd, dst_dir_fd=dir_fd)


def _last_byte(fd: int, size: int) -> bytes:
    try:
        return os.pread(fd, 1, size - 1)
    except OSError as exc:
        raise StateError("state_io", "cannot read the request log tail") from exc


def _append_log_line(state_dir: Path, line: bytes, max_bytes: int, lock_timeout: float) -> None:
    dir_fd = state.open_dir(state_dir)
    if dir_fd is None:
        raise StateError("state_io", "cannot open the state directory")
    try:
        for _ in range(_ROTATION_TRIES):
            rotated = False
            with state.locked_append_file(dir_fd, LOG_FILE, timeout=lock_timeout) as fd:
                size = os.fstat(fd).st_size
                prefix = b"" if size == 0 or _last_byte(fd, size) == b"\n" else b"\n"
                if size > 0 and size + len(prefix) + len(line) > max_bytes:
                    try:
                        _rotate(dir_fd)
                    except OSError as exc:
                        raise StateError("state_io", "cannot rotate the request log") from exc
                    rotated = True
                else:
                    state.write_all(fd, prefix + line)
                    return
            if not rotated:  # pragma: no cover - the with body always returns or rotates
                return
        raise StateError("lock_timeout", "request log kept rotating while waiting")
    finally:
        os.close(dir_fd)


def append_log(
    state_dir: Path,
    record: Mapping[str, object],
    *,
    warn: Callable[[str], None] = _stderr_warning,
    max_bytes: int = LOG_MAX_BYTES,
    now: datetime | None = None,
    lock_timeout: float = state.LOCK_TIMEOUT,
) -> str | None:
    """Append one request-log line; on any failure warn, return the warning, never raise."""
    try:
        _validate_log(record)
        cleaned = state.clean_tree(record)
        assert isinstance(cleaned, dict)
        _validate_log(cleaned)  # scrubbing and rendering must not break the schema
        first: dict[str, object] = {"v": 1, "ts": cleaned.get("ts") or _timestamp(now)}
        line = _encode(_ordered(LOG_KEYS, cleaned, first))
        if len(line) > MAX_RECORD_BYTES:
            raise StateError("record_too_large", "log record is too large")
        _append_log_line(state_dir, line, max_bytes, lock_timeout)
    except StateError as exc:
        return _warn(warn, exc.reason)
    except Exception as exc:  # the sink never fails the command, whatever went wrong
        return _warn(warn, type(exc).__name__)
    return None


def _warn(warn: Callable[[str], None], reason: str) -> str:
    message = render(f"warning: the request log was not written ({reason})")
    warn(message)
    return message
