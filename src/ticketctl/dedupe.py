"""The dedupe bucket lock and the create lag guard (docs/interface.md, Dedupe).

Two independent parts.

**The bucket lock** (:func:`bucket_lock`, the default ``Seams.lock``). A write with a dedupe key
takes ``flock`` on one of 256 files ``<state>/locks/<two hex>``, named after the first byte of
``sha256(site \\0 project-or-key \\0 dedupe key)``: the raw key never reaches a path, a collision
only serializes two unrelated writers, and nothing accumulates (at most 256 files, never removed).
The directory and the files go through ``state.py`` (``0700`` and ``0600``, ``O_NOFOLLOW``, owner
and type checks, the inode recheck of ``locked_append_file``), and a file with more than one link
is refused. The descriptor is close-on-exec (``state.open_file``), so a hook or any child never
inherits it. The wait is bounded by what is left of the command deadline (which, today, also
counts the confirmation prompt and the ``pre-`` hook time, see ``todo(#24)`` in ``writeflow.py``),
then exit 12 ``unavailable`` with ``reason: locked``; the wait uses the monotonic clock. The
kernel drops the lock when its holder dies, so a killed holder never wedges a waiter.

On release the holder sets the file's modification time to now (a lock file that was never
released holds size 0 and its mtime is its creation, which says nothing; a released one holds one
byte). The lock is not re-entrant: taking a bucket the same thread already holds is a caller bug
(``ValueError``, exit 1) rather than a wait that could only time out. The engine releases it on
every exit path and before the ``post-`` hook, so a hook that runs ticketctl with the same key
takes a free lock.

**The lag guard** (:func:`guarded_lookup`). The label index lags a create, so under the lock a
lookup that finds nothing is repeated before it concludes "absent": once after 2 s when the lock
file was modified within the last 5 s, and every 5 s until 60 s after the record's ``ts`` when the
tail of ``audit.jsonl`` holds a same-site, same-key create attempt of the last 120 s that is
``attempted`` without an outcome, ``unknown`` or ``ok``. A hit ends the wait at once. Waits are
bounded by the command deadline's remaining time (prompt and hook time included, as above): when
a needed wait does not fit (strictly longer than the time left), nothing is sent and the error is
``timeout`` (a create must not conclude "absent" from a lookup the guard distrusts).
The decision is a pure function (:func:`decide`) of the lock mtime, the audit records and the
wall clock; waits go through an injectable :class:`Clock` so tests run on virtual time.

TODO(#31): the ``create`` action calls :func:`guarded_lookup` from ``reduce(fresh=True)``.
TODO(#33): the comment dedupe needs no guard (its list is read-your-writes).
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import threading
import time
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

from ticketctl import state
from ticketctl.errors import TicketctlError
from ticketctl.state import StateError

if TYPE_CHECKING:
    from ticketctl.plan import Planned
    from ticketctl.writeflow import Context

BUCKETS = 256
LOCKS_DIR = "locks"
_BUCKET = re.compile(r"[0-9a-f]{2}")

QUICK_WINDOW_S = 5.0  # a lock file modified this recently means a waiting or quick re-run
QUICK_DELAY_S = 2.0
AUDIT_WINDOW_S = 120.0  # a same-key create record at most this old starts the polling
POLL_INTERVAL_S = 5.0
POLL_MAX_S = 60.0  # polling ends this long after the record's ts
TAIL_BYTES = 1024 * 1024

_TS = "%Y-%m-%dT%H:%M:%SZ"
_PENDING = ("attempted", "unknown", "ok")


def bucket_name(site: str, scope: str, dedupe_key: str) -> str:
    """The lock file name: two hex characters of ``sha256(site \\0 scope \\0 key)``.

    ``scope`` is the project for a create and the ticket key for a comment.
    """
    digest = hashlib.sha256(
        b"\0".join(p.encode("utf-8", "surrogatepass") for p in (site, scope, dedupe_key))
    )
    return digest.hexdigest()[:2]


# --- the lock --------------------------------------------------------------------------------

_held = threading.local()


def _held_names() -> set[tuple[str, str]]:
    names = getattr(_held, "names", None)
    if names is None:
        names = _held.names = set()
    return names


class BucketLock:
    """A context manager holding one bucket's ``flock``; see the module docstring."""

    def __init__(self, state_dir: Path, name: str, *, timeout: float) -> None:
        if not _BUCKET.fullmatch(name):
            raise ValueError("a lock bucket name is two lowercase hex characters")
        self._state_dir = state_dir
        self._name = name
        self._timeout = max(0.0, timeout)
        self._locks_fd: int | None = None
        self._cm: Any = None
        self._fd: int | None = None
        self._ident = (str(state_dir), name)

    def __enter__(self) -> None:
        held = _held_names()
        if self._ident in held:
            raise ValueError("the dedupe lock is not re-entrant")
        dir_fd = state.open_dir(self._state_dir)
        if dir_fd is None:
            raise StateError("state_io", "cannot open the state directory")
        try:
            locks_fd = state.open_subdir(dir_fd, LOCKS_DIR)
        finally:
            os.close(dir_fd)
        if locks_fd is None:
            raise StateError("state_io", "cannot open the lock directory")
        cm = state.locked_append_file(locks_fd, self._name, timeout=self._timeout, single_link=True)
        try:
            try:
                fd = cm.__enter__()
            except StateError as exc:
                if exc.reason == "lock_timeout":
                    raise TicketctlError(
                        "unavailable",
                        "another ticketctl run holds the dedupe lock; try again",
                        reason="locked",
                    ) from None
                raise
        except BaseException:
            os.close(locks_fd)
            raise
        self._locks_fd, self._cm, self._fd = locks_fd, cm, fd
        held.add(self._ident)

    def __exit__(self, *exc: object) -> None:
        cm, fd, locks_fd = self._cm, self._fd, self._locks_fd
        if cm is None or fd is None or locks_fd is None:
            return
        self._cm = self._fd = self._locks_fd = None
        _held_names().discard(self._ident)
        failure: OSError | None = None
        try:
            os.ftruncate(fd, 1)  # one byte: "released at least once"
            os.utime(fd, None)  # the modification time is the release time
        except OSError as err:
            failure = err
        finally:
            try:
                cm.__exit__(None, None, None)  # closes the fd, which drops the flock
            finally:
                os.close(locks_fd)
        if failure is not None:
            raise StateError("state_io", "cannot update the lock file time")


def bucket_lock(planned: Planned, ctx: Context) -> BucketLock:
    """The default ``Seams.lock``: the bucket of the plan's site, scope and dedupe key.

    The wait is what is left of the command timeout.
    """
    if planned.dedupe_key is None:
        raise ValueError("the dedupe lock needs a dedupe key")
    name = bucket_name(ctx.site, planned.hash_key or "", planned.dedupe_key)
    return BucketLock(ctx.rt.state_dir, name, timeout=ctx.gw.deadline.remaining())


# --- the lag guard ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Clock:
    """The time the guard uses: wall time for windows, a sleep for waits."""

    wall: Callable[[], float] = time.time
    sleep: Callable[[float], None] = time.sleep


@dataclass(frozen=True)
class LagDecision:
    """What the guard must do after a lookup found nothing."""

    quick_rerun: bool = False
    poll_until: float | None = None  # wall-clock epoch seconds


def lock_mtime(state_dir: Path, name: str) -> float | None:
    """The release time of a bucket lock, or None when it was never released (or is missing).

    Read-only; never creates anything.
    """
    dir_fd = state.open_dir(state_dir, create=False)
    if dir_fd is None:
        return None
    try:
        locks_fd = state.open_subdir(dir_fd, LOCKS_DIR, create=False)
    finally:
        os.close(dir_fd)
    if locks_fd is None:
        return None
    try:
        fd = state.open_file(locks_fd, name, create=False, read_only=True)
        if fd is None:
            return None
        try:
            info = os.fstat(fd)
        finally:
            os.close(fd)
        return info.st_mtime if info.st_size > 0 else None
    finally:
        os.close(locks_fd)


def audit_tail(state_dir: Path, *, max_bytes: int = TAIL_BYTES) -> list[dict[str, Any]]:
    """The parseable records of the last ``max_bytes`` of ``audit.jsonl``, oldest first.

    Read without the audit lock: a partial first line (cut by the window) and a torn last line
    are dropped, every line that does not parse into an object is ignored.
    """
    dir_fd = state.open_dir(state_dir, create=False)
    if dir_fd is None:
        return []
    try:
        fd = state.open_file(dir_fd, "audit.jsonl", create=False, read_only=True)
    finally:
        os.close(dir_fd)
    if fd is None:
        return []
    try:
        size = os.fstat(fd).st_size
        start = max(0, size - max_bytes)
        # one byte before the window tells whether the cut fell on a line boundary
        data = os.pread(fd, size - start + (start > 0), max(0, start - 1))
    except OSError as exc:
        raise StateError("state_io", "cannot read the audit log") from exc
    finally:
        os.close(fd)
    cut = start > 0 and data[:1] != b"\n"  # the window starts inside a line
    if start > 0:
        data = data[1:]
    lines = data.split(b"\n")
    lines.pop()  # the piece after the last newline: empty, or a torn line
    if cut and lines:
        lines.pop(0)  # the first line is only a tail
    out: list[dict[str, Any]] = []
    for line in lines:
        try:
            doc = json.loads(line)
        except (ValueError, RecursionError):
            continue
        if isinstance(doc, dict):
            out.append(doc)
    return out


def _epoch(ts: object) -> float | None:
    if not isinstance(ts, str):
        return None
    try:
        return datetime.strptime(ts, _TS).replace(tzinfo=UTC).timestamp()
    except ValueError:
        return None


def pending_create_ts(
    records: Iterable[Mapping[str, Any]], site: str, dedupe_key: str, now: float
) -> float | None:
    """The ``ts`` (epoch) of the newest same-key create attempt that may still be landing.

    Per ``attempt_id`` only the newest record counts (highest ``seq``). It qualifies when it is
    ``attempted``, ``unknown`` or ``ok`` and its ``ts`` is at most 120 s old. None when no
    attempt qualifies.
    """
    latest: dict[str, Mapping[str, Any]] = {}
    for rec in records:
        target, inputs = rec.get("target"), rec.get("inputs")
        if (
            rec.get("action") != "create"
            or not isinstance(target, Mapping)
            or target.get("site") != site
            or not isinstance(inputs, Mapping)
            or inputs.get("dedupe_key") != dedupe_key
            or not isinstance(rec.get("attempt_id"), str)
        ):
            continue
        seq = rec.get("seq")
        old = latest.get(rec["attempt_id"])
        old_seq = old.get("seq") if old else None
        if old is None or (
            isinstance(seq, int) and not isinstance(seq, bool)
            and (not isinstance(old_seq, int) or seq > old_seq)
        ):  # fmt: skip
            latest[rec["attempt_id"]] = rec
    best: float | None = None
    for rec in latest.values():
        when = _epoch(rec.get("ts"))
        if rec.get("result") not in _PENDING or when is None or now - when > AUDIT_WINDOW_S:
            continue
        if best is None or when > best:
            best = when
    return best


def decide(
    *,
    lock_released_at: float | None,
    records: Iterable[Mapping[str, Any]],
    site: str,
    dedupe_key: str,
    now: float,
) -> LagDecision:
    """The pure guard decision (docs/interface.md, Dedupe: Lag guard)."""
    quick = lock_released_at is not None and now - lock_released_at <= QUICK_WINDOW_S
    when = pending_create_ts(records, site, dedupe_key, now)
    until = None if when is None else min(when + POLL_MAX_S, now + POLL_MAX_S)
    return LagDecision(quick_rerun=quick, poll_until=until)


def run_guard[T](
    lookup: Callable[[], T | None],
    decision: LagDecision,
    *,
    clock: Clock,
    budget: Callable[[], float],
) -> T | None:
    """Run ``lookup``; while it finds nothing, repeat it as ``decision`` says.

    A hit returns at once. ``budget`` is the command's remaining seconds: a wait that does not
    fit in it raises ``timeout`` instead of concluding "absent" (nothing is sent).
    """

    def wait(seconds: float) -> None:
        if budget() < seconds:
            raise TicketctlError(
                "timeout",
                "the dedupe lookup could not be confirmed within --timeout; nothing was sent",
            )
        clock.sleep(seconds)

    hit = lookup()
    if hit is not None:
        return hit
    if decision.quick_rerun:
        wait(QUICK_DELAY_S)
        hit = lookup()
        if hit is not None:
            return hit
    if decision.poll_until is not None:
        while True:
            left = decision.poll_until - clock.wall()
            if left <= 0:
                break
            wait(min(POLL_INTERVAL_S, left))
            hit = lookup()
            if hit is not None:
                return hit
    return None


def guarded_lookup[T](
    ctx: Context,
    *,
    scope: str,
    dedupe_key: str,
    lookup: Callable[[], T | None],
    clock: Clock | None = None,
    budget: Callable[[], float] | None = None,
) -> T | None:
    """The create lookup under the bucket lock, with the lag guard (``reduce(fresh=True)``).

    ``scope`` is the project and must equal the create plan's ``Planned.hash_key``; ``lookup``
    returns the found object or None. Call it only while the bucket lock is held: the lock
    file's time is read as the previous holder's release. ``ValueError`` (an internal error)
    when the bucket derived from ``scope`` and ``dedupe_key`` is not one this thread holds for
    the state directory (a wrong ``scope`` would silently disable the quick re-run). The held
    check needs the real ``bucket_lock`` seam: an injected fake lock never registers the
    bucket, so it makes this raise ``ValueError``; tests of the create action (#31) must keep
    the real lock.
    """
    clock = clock or Clock()
    name = bucket_name(ctx.site, scope, dedupe_key)
    state_dir = ctx.rt.state_dir
    if (str(state_dir), name) not in _held_names():
        raise ValueError("guarded_lookup needs the dedupe lock of its own bucket to be held")
    decision = decide(
        lock_released_at=lock_mtime(state_dir, name),
        records=audit_tail(state_dir),
        site=ctx.site,
        dedupe_key=dedupe_key,
        now=clock.wall(),
    )
    return run_guard(lookup, decision, clock=clock, budget=budget or ctx.gw.deadline.remaining)


__all__ = [
    "BUCKETS",
    "BucketLock",
    "Clock",
    "LagDecision",
    "audit_tail",
    "bucket_lock",
    "bucket_name",
    "decide",
    "guarded_lookup",
    "lock_mtime",
    "pending_create_ts",
    "run_guard",
]
