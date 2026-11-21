"""The dedupe bucket lock and the lag guard decision (issue #32), without the write flow.

The engine-level behavior (two creators, release on every exit path, the post-hook case) is in
``tests/test_dedupe_flow.py``.
"""

from __future__ import annotations

import fcntl
import json
import multiprocessing
import os
import re
import signal
import stat
import time
from pathlib import Path
from typing import Any

import pytest

from tests import dedupe_workers
from ticketctl import dedupe
from ticketctl.dedupe import BucketLock, Clock, LagDecision
from ticketctl.errors import TicketctlError
from ticketctl.state import StateError

SPAWN = multiprocessing.get_context("spawn")
NOW = 1_800_000_000.0


@pytest.fixture
def state_dir(tmp_path: Path) -> Path:
    return tmp_path / "state" / "ticketctl"


def lock_files(state_dir: Path) -> list[str]:
    return sorted(p.name for p in (state_dir / "locks").iterdir())


def free(state_dir: Path, name: str) -> bool:
    try:
        with BucketLock(state_dir, name, timeout=0.2):
            return True
    except TicketctlError:
        return False


# --- names -----------------------------------------------------------------------------------


def test_bucket_name_is_two_hex_of_the_documented_hash() -> None:
    import hashlib

    want = hashlib.sha256(b"main\0DEMO\0key-1").hexdigest()[:2]
    assert dedupe.bucket_name("main", "DEMO", "key-1") == want
    assert re.fullmatch(r"[0-9a-f]{2}", want)


def test_bucket_name_separates_its_parts() -> None:
    parts = [("a", "bc", "d"), ("ab", "c", "d"), ("a", "b", "cd"), ("a", "bcd", "")]
    names = {dedupe.bucket_name(*p) for p in parts}
    # four distinct inputs; two hex characters can collide, but not for these fixed inputs
    assert len(names) >= 3


@pytest.mark.parametrize("key", ["", "../../etc/passwd", "a/b", "é中", "x" * 5000, "\x00"])
def test_hostile_keys_never_reach_a_path(key: str, state_dir: Path) -> None:
    name = dedupe.bucket_name("main", "DEMO", key)
    assert re.fullmatch(r"[0-9a-f]{2}", name)
    with BucketLock(state_dir, name, timeout=1.0):
        pass
    assert lock_files(state_dir) == [name]


def test_a_bad_bucket_name_is_refused() -> None:
    for bad in ("", "g0", "000", "../", "A1", "0"):
        with pytest.raises(ValueError):
            BucketLock(Path("/nonexistent"), bad, timeout=1.0)


def test_many_keys_never_exceed_256_lock_files(state_dir: Path) -> None:
    names = {dedupe.bucket_name("main", "DEMO", f"key-{i}") for i in range(300)}
    assert all(re.fullmatch(r"[0-9a-f]{2}", n) for n in names)
    for i in range(300):
        name = dedupe.bucket_name("main", "DEMO", f"key-{i}")
        with BucketLock(state_dir, name, timeout=1.0):
            pass
    assert set(lock_files(state_dir)) == names  # exactly the names produced, nothing else
    assert len(names) > 150  # the hash spreads (a stuck name would show here)


# --- the lock ---------------------------------------------------------------------------------


def test_directory_and_file_modes(state_dir: Path) -> None:
    with BucketLock(state_dir, "ab", timeout=1.0):
        pass
    assert stat.S_IMODE((state_dir / "locks").stat().st_mode) == 0o700
    assert stat.S_IMODE((state_dir / "locks" / "ab").stat().st_mode) == 0o600


def test_release_updates_the_modification_time_and_marks_the_file(state_dir: Path) -> None:
    lock = BucketLock(state_dir, "ab", timeout=1.0)
    lock.__enter__()
    path = state_dir / "locks" / "ab"
    os.utime(path, (1000, 1000))
    assert path.stat().st_size == 0
    assert dedupe.lock_mtime(state_dir, "ab") is None  # never released: no information
    before = time.time()
    lock.__exit__(None, None, None)
    assert path.stat().st_mtime >= before - 1
    released = dedupe.lock_mtime(state_dir, "ab")
    assert released is not None and abs(released - time.time()) < 5
    assert path.stat().st_size == 1


def test_lock_mtime_never_creates_anything(state_dir: Path) -> None:
    assert dedupe.lock_mtime(state_dir, "ab") is None
    assert not state_dir.exists()
    state_dir.mkdir(parents=True)
    assert dedupe.lock_mtime(state_dir, "ab") is None
    assert not (state_dir / "locks").exists()


def test_the_descriptor_is_close_on_exec(state_dir: Path) -> None:
    lock = BucketLock(state_dir, "ab", timeout=1.0)
    lock.__enter__()
    try:
        assert lock._fd is not None
        assert fcntl.fcntl(lock._fd, fcntl.F_GETFD) & fcntl.FD_CLOEXEC
    finally:
        lock.__exit__(None, None, None)


def test_the_lock_is_released_when_the_body_raises(state_dir: Path) -> None:
    with pytest.raises(RuntimeError):
        with BucketLock(state_dir, "ab", timeout=1.0):
            raise RuntimeError("boom")
    assert free(state_dir, "ab")


def test_the_lock_is_released_on_keyboard_interrupt(state_dir: Path) -> None:
    with pytest.raises(KeyboardInterrupt):
        with BucketLock(state_dir, "ab", timeout=1.0):
            raise KeyboardInterrupt
    assert free(state_dir, "ab")


def test_the_lock_is_not_reentrant_and_the_refusal_keeps_the_first_lock(state_dir: Path) -> None:
    with BucketLock(state_dir, "ab", timeout=1.0):
        with pytest.raises(ValueError, match="re-entrant"):
            BucketLock(state_dir, "ab", timeout=0.1).__enter__()
        assert len(dedupe._held_names()) == 1  # the refusal left the first lock held
    assert free(state_dir, "ab")


def test_a_second_instance_after_release_works_repeatedly(state_dir: Path) -> None:
    for _ in range(5):
        with BucketLock(state_dir, "ab", timeout=1.0):
            pass


def test_a_symlinked_lock_directory_is_refused(state_dir: Path, tmp_path: Path) -> None:
    state_dir.mkdir(parents=True)
    target = tmp_path / "elsewhere"
    target.mkdir()
    (state_dir / "locks").symlink_to(target)
    with pytest.raises(StateError) as info:
        BucketLock(state_dir, "ab", timeout=1.0).__enter__()
    assert info.value.reason == "state_symlink"
    assert list(target.iterdir()) == []


def test_a_symlinked_lock_file_is_refused(state_dir: Path, tmp_path: Path) -> None:
    (state_dir / "locks").mkdir(parents=True)
    victim = tmp_path / "victim"
    victim.write_text("keep")
    (state_dir / "locks" / "ab").symlink_to(victim)
    with pytest.raises(StateError) as info:
        BucketLock(state_dir, "ab", timeout=1.0).__enter__()
    assert info.value.reason == "state_symlink"
    assert victim.read_text() == "keep"


def test_a_hardlinked_lock_file_is_refused_and_not_left_locked(
    state_dir: Path, tmp_path: Path
) -> None:
    (state_dir / "locks").mkdir(parents=True)
    other = tmp_path / "other"
    other.write_text("x")
    other.chmod(0o644)
    os.link(other, state_dir / "locks" / "ab")
    with pytest.raises(StateError) as info:
        BucketLock(state_dir, "ab", timeout=1.0).__enter__()
    assert info.value.reason == "state_type"
    assert stat.S_IMODE(other.stat().st_mode) == 0o644  # the target's mode was never changed
    assert other.read_text() == "x"
    fd = os.open(other, os.O_RDONLY)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)  # the refusal released its flock
    finally:
        os.close(fd)
    assert not dedupe._held_names()


def test_a_directory_or_fifo_in_place_of_a_lock_file_is_refused(state_dir: Path) -> None:
    (state_dir / "locks").mkdir(parents=True)
    (state_dir / "locks" / "ab").mkdir()
    os.mkfifo(state_dir / "locks" / "cd")
    for name in ("ab", "cd"):
        with pytest.raises(StateError):
            BucketLock(state_dir, name, timeout=0.5).__enter__()


def test_a_loose_lock_file_mode_is_tightened(state_dir: Path) -> None:
    (state_dir / "locks").mkdir(parents=True)
    path = state_dir / "locks" / "ab"
    path.write_text("")
    path.chmod(0o666)
    with BucketLock(state_dir, "ab", timeout=1.0):
        pass
    assert stat.S_IMODE(path.stat().st_mode) == 0o600


def test_a_file_removed_while_waiting_is_reopened(state_dir: Path) -> None:
    """The inode recheck of ``locked_append_file``: no two holders on different inodes."""
    import threading

    held = BucketLock(state_dir, "ab", timeout=1.0)
    held.__enter__()
    path = state_dir / "locks" / "ab"
    outcome: list[object] = []

    def waiter() -> None:
        try:
            with BucketLock(state_dir, "ab", timeout=20.0):
                outcome.append(os.stat(path).st_ino == os.fstat(lock_fd(path)).st_ino)
        except BaseException as exc:  # noqa: BLE001
            outcome.append(exc)

    def lock_fd(p: Path) -> int:
        return os.open(p, os.O_RDONLY)

    thread = threading.Thread(target=waiter)
    thread.start()
    time.sleep(0.3)  # the waiter has the old inode open and is polling its flock
    path.unlink()  # someone removed the file under the holder
    held.__exit__(None, None, None)
    thread.join(30)
    assert outcome == [True]  # it reopened the current file instead of holding the unlinked one
    assert path.exists()


def test_the_lock_directory_descriptor_is_closed(state_dir: Path) -> None:
    if not os.path.isdir("/proc/self/fd"):
        pytest.skip("no /proc/self/fd")
    with BucketLock(state_dir, "ab", timeout=1.0):  # warm up: create the directory and file
        pass
    before = len(os.listdir("/proc/self/fd"))
    for _ in range(50):
        with BucketLock(state_dir, "ab", timeout=1.0):
            pass
    assert len(os.listdir("/proc/self/fd")) == before


@pytest.mark.parametrize("call", ["utime", "ftruncate"])
def test_a_failing_release_update_is_state_io_and_frees_the_lock(
    call: str, state_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    real = getattr(os, call)

    def boom(target: Any, *args: Any, **kwargs: Any) -> Any:
        if isinstance(target, int):  # only the lock's descriptor; paths keep working
            raise OSError(5, "injected")
        return real(target, *args, **kwargs)

    lock = BucketLock(state_dir, "ab", timeout=1.0)
    lock.__enter__()
    monkeypatch.setattr(os, call, boom)
    with pytest.raises(StateError) as info:
        lock.__exit__(None, None, None)
    monkeypatch.undo()
    assert info.value.reason == "state_io"
    assert not dedupe._held_names()
    assert free(state_dir, "ab")  # the fd was closed: the lock can be taken again


# --- real processes ---------------------------------------------------------------------------


def test_a_held_lock_past_the_timeout_is_unavailable_locked(state_dir: Path) -> None:
    locked = SPAWN.Event()
    proc = SPAWN.Process(
        target=dedupe_workers.hold_bucket, args=(str(state_dir), "ab", locked, 120.0)
    )
    proc.start()
    try:
        assert locked.wait(30)
        t0 = time.monotonic()
        with pytest.raises(TicketctlError) as info:
            BucketLock(state_dir, "ab", timeout=0.4).__enter__()
        waited = time.monotonic() - t0
        err = info.value
        assert (err.exit, err.code, err.reason, err.retryable) == (
            12,
            "unavailable",
            "locked",
            True,
        )
        assert 0.3 <= waited < 5  # monotonic wait, bounded by the timeout
        assert not dedupe._held_names()
    finally:
        assert proc.pid is not None
        os.kill(proc.pid, signal.SIGKILL)  # the exact pid started above
        proc.join(30)
    assert free(state_dir, "ab")  # a dead holder never wedges the bucket


def test_a_waiter_takes_the_lock_when_the_holder_dies(state_dir: Path) -> None:
    locked = SPAWN.Event()
    proc = SPAWN.Process(
        target=dedupe_workers.hold_bucket, args=(str(state_dir), "ab", locked, 120.0)
    )
    proc.start()
    try:
        assert locked.wait(30)
        killer_pid = proc.pid
        assert killer_pid is not None
        import threading

        threading.Timer(0.3, lambda: os.kill(killer_pid, signal.SIGKILL)).start()
        t0 = time.monotonic()
        with BucketLock(state_dir, "ab", timeout=20.0):
            assert time.monotonic() - t0 < 15
    finally:
        proc.join(30)


def test_a_waiter_blocks_until_the_holder_releases(state_dir: Path) -> None:
    locked = SPAWN.Event()
    proc = SPAWN.Process(
        target=dedupe_workers.hold_bucket, args=(str(state_dir), "ab", locked, 1.0)
    )
    proc.start()
    try:
        assert locked.wait(30)
        t0 = time.monotonic()
        with BucketLock(state_dir, "ab", timeout=30.0):
            pass
        assert time.monotonic() - t0 >= 0.3
    finally:
        proc.join(30)


def test_processes_exclude_each_other(state_dir: Path, tmp_path: Path) -> None:
    counter = tmp_path / "counter"
    counter.write_text("0")
    go = SPAWN.Event()
    procs = [
        SPAWN.Process(
            target=dedupe_workers.counter_worker, args=(str(state_dir), "ab", str(counter), 15, go)
        )
        for _ in range(4)
    ]
    for p in procs:
        p.start()
    go.set()
    for p in procs:
        p.join(120)
        assert p.exitcode == 0
    assert counter.read_text() == "60"  # a lost update would show a smaller number


# --- the guard decision -----------------------------------------------------------------------


def rec(
    attempt: str, seq: int, result: str, age: float, *, key: str = "k", site: str = "main",
    action: str = "create",
) -> dict[str, Any]:  # fmt: skip
    ts = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(NOW - age))
    return {
        "v": 1, "seq": seq, "ts": ts, "attempt_id": attempt, "action": action,
        "target": {"site": site, "key": None}, "inputs": {"dedupe_key": key},
        "result": result,
    }  # fmt: skip


@pytest.mark.parametrize("result", ["attempted", "unknown", "ok"])
def test_pending_create_states_start_the_polling(result: str) -> None:
    got = dedupe.pending_create_ts([rec("a", 1, result, 30)], "main", "k", NOW)
    assert got == NOW - 30


@pytest.mark.parametrize("result", ["failed", "partial"])
def test_failed_creates_do_not_poll(result: str) -> None:
    assert dedupe.pending_create_ts([rec("a", 1, result, 30)], "main", "k", NOW) is None


def test_only_the_newest_record_of_an_attempt_counts() -> None:
    records = [rec("a", 1, "attempted", 40), rec("a", 2, "failed", 39)]
    assert dedupe.pending_create_ts(records, "main", "k", NOW) is None
    records = [rec("a", 2, "unknown", 39), rec("a", 1, "attempted", 40)]  # file order irrelevant
    assert dedupe.pending_create_ts(records, "main", "k", NOW) == NOW - 39


def test_the_record_must_match_site_key_action_and_be_recent() -> None:
    cases = [
        rec("a", 1, "unknown", 30, site="other"),
        rec("a", 1, "unknown", 30, key="k2"),
        rec("a", 1, "unknown", 30, action="comment"),
        rec("a", 1, "unknown", 121),
    ]
    for case in cases:
        assert dedupe.pending_create_ts([case], "main", "k", NOW) is None
    assert dedupe.pending_create_ts([rec("a", 1, "unknown", 120)], "main", "k", NOW) == NOW - 120


def test_junk_records_are_ignored() -> None:
    junk: list[Any] = [
        {}, {"action": "create"}, {"action": "create", "target": 1, "inputs": 2},
        {**rec("a", 1, "unknown", 30), "ts": "not a time"},
        {**rec("b", 1, "unknown", 30), "ts": 5},
        {**rec("c", 1, "unknown", 30), "attempt_id": 7},
    ]  # fmt: skip
    assert dedupe.pending_create_ts(junk, "main", "k", NOW) is None


def test_a_future_ts_is_clamped_to_the_poll_maximum() -> None:
    d = dedupe.decide(
        lock_released_at=None,
        records=[rec("a", 1, "unknown", -3600)],
        site="main", dedupe_key="k", now=NOW,
    )  # fmt: skip
    assert d.poll_until == NOW + 60


def test_decide_quick_rerun_window() -> None:
    def quick(age: float | None) -> bool:
        released = None if age is None else NOW - age
        return dedupe.decide(
            lock_released_at=released, records=[], site="main", dedupe_key="k", now=NOW
        ).quick_rerun

    assert quick(0.5) and quick(5.0)
    assert not quick(5.5) and not quick(None)


def test_decide_poll_until_is_sixty_seconds_from_the_record() -> None:
    d = dedupe.decide(
        lock_released_at=None, records=[rec("a", 1, "unknown", 30)],
        site="main", dedupe_key="k", now=NOW,
    )  # fmt: skip
    assert d == LagDecision(quick_rerun=False, poll_until=NOW - 30 + 60)


# --- running the guard ------------------------------------------------------------------------


class Virtual:
    """Virtual time: ``sleep`` advances ``wall`` and records the waits."""

    def __init__(self, start: float = NOW) -> None:
        self.now = start
        self.waits: list[float] = []
        self.budget_left = 10_000.0

    def clock(self) -> Clock:
        return Clock(wall=lambda: self.now, sleep=self.sleep)

    def sleep(self, seconds: float) -> None:
        self.waits.append(seconds)
        self.now += seconds
        self.budget_left -= seconds

    def budget(self) -> float:
        return self.budget_left


def hits_after(v: Virtual, seconds: float, value: str = "found") -> Any:
    start = v.now
    calls: list[float] = []

    def lookup() -> str | None:
        calls.append(v.now - start)
        return value if v.now - start >= seconds else None

    lookup.calls = calls  # type: ignore[attr-defined]
    return lookup


def test_a_first_hit_waits_for_nothing() -> None:
    v = Virtual()
    look = hits_after(v, 0)
    got = dedupe.run_guard(look, LagDecision(True, NOW + 60), clock=v.clock(), budget=v.budget)
    assert got == "found" and v.waits == [] and len(look.calls) == 1


def test_no_guard_no_repeat() -> None:
    v = Virtual()
    look = hits_after(v, 99)
    assert dedupe.run_guard(look, LagDecision(), clock=v.clock(), budget=v.budget) is None
    assert v.waits == [] and len(look.calls) == 1


def test_quick_rerun_repeats_once_after_two_seconds() -> None:
    v = Virtual()
    look = hits_after(v, 2)
    got = dedupe.run_guard(look, LagDecision(quick_rerun=True), clock=v.clock(), budget=v.budget)
    assert got == "found" and v.waits == [2.0] and look.calls == [0, 2]
    v = Virtual()
    look = hits_after(v, 99)  # still nothing: exactly one repeat, then absent
    assert dedupe.run_guard(look, LagDecision(True), clock=v.clock(), budget=v.budget) is None
    assert v.waits == [2.0] and len(look.calls) == 2


def test_polling_every_five_seconds_until_a_hit() -> None:
    v = Virtual()
    look = hits_after(v, 20)
    got = dedupe.run_guard(look, LagDecision(poll_until=NOW + 30), clock=v.clock(), budget=v.budget)
    assert got == "found" and v.waits == [5.0] * 4 and look.calls == [0, 5, 10, 15, 20]


def test_polling_stops_sixty_seconds_after_the_record() -> None:
    v = Virtual()
    look = hits_after(v, 10_000)
    got = dedupe.run_guard(look, LagDecision(poll_until=NOW + 12), clock=v.clock(), budget=v.budget)
    assert got is None
    assert v.waits == [5.0, 5.0, 2.0]  # the last wait is cut to the window end
    assert look.calls == [0, 5, 10, 12]


def test_quick_rerun_then_polling_both_run() -> None:
    v = Virtual()
    look = hits_after(v, 12)
    got = dedupe.run_guard(look, LagDecision(True, NOW + 40), clock=v.clock(), budget=v.budget)
    assert got == "found" and v.waits == [2.0, 5.0, 5.0] and look.calls == [0, 2, 7, 12]


def test_a_wait_that_does_not_fit_the_command_timeout_is_a_timeout_not_absent() -> None:
    v = Virtual()
    v.budget_left = 1.0
    look = hits_after(v, 99)
    with pytest.raises(TicketctlError) as info:
        dedupe.run_guard(look, LagDecision(quick_rerun=True), clock=v.clock(), budget=v.budget)
    assert (info.value.exit, info.value.code) == (12, "timeout")
    assert v.waits == [] and len(look.calls) == 1  # nothing waited, nothing concluded
    v = Virtual()
    v.budget_left = 7.0  # two polls fit, the third does not
    look = hits_after(v, 99)
    with pytest.raises(TicketctlError):
        dedupe.run_guard(look, LagDecision(poll_until=NOW + 60), clock=v.clock(), budget=v.budget)
    assert v.waits == [5.0]


def test_a_wait_exactly_equal_to_the_time_left_fits_and_is_taken() -> None:
    v = Virtual()
    v.budget_left = 2.0  # "does not fit" means the wait is strictly greater than the time left
    look = hits_after(v, 2)
    got = dedupe.run_guard(look, LagDecision(quick_rerun=True), clock=v.clock(), budget=v.budget)
    assert got == "found" and v.waits == [2.0]


def test_an_exhausted_budget_with_no_guard_still_looks_once() -> None:
    v = Virtual()
    v.budget_left = 0.0
    look = hits_after(v, 0)
    assert dedupe.run_guard(look, LagDecision(), clock=v.clock(), budget=v.budget) == "found"


# --- the audit tail ---------------------------------------------------------------------------


def write_audit(state_dir: Path, lines: list[bytes]) -> Path:
    state_dir.mkdir(parents=True, exist_ok=True)
    path = state_dir / "audit.jsonl"
    path.write_bytes(b"".join(lines))
    path.chmod(0o600)
    return path


def test_audit_tail_missing_state_or_file(state_dir: Path) -> None:
    assert dedupe.audit_tail(state_dir) == []
    state_dir.mkdir(parents=True)
    assert dedupe.audit_tail(state_dir) == []
    assert not (state_dir / "audit.jsonl").exists()


def test_audit_tail_drops_torn_cut_and_junk_lines(state_dir: Path) -> None:
    good = [json.dumps({"seq": i, "pad": "x" * 50}).encode() + b"\n" for i in range(1, 40)]
    write_audit(state_dir, [*good, b"not json\n", b"[1,2]\n", b'{"seq": 99, "torn'])
    got = dedupe.audit_tail(state_dir)
    assert [r["seq"] for r in got] == list(range(1, 40))
    cut = dedupe.audit_tail(state_dir, max_bytes=300)  # the first (cut) line is dropped
    seqs = [r["seq"] for r in cut]
    assert seqs and seqs[-1] == 39 and seqs == list(range(seqs[0], 40))
    assert all(r["seq"] > 33 for r in cut)


def test_audit_tail_refuses_a_symlinked_audit_file(state_dir: Path, tmp_path: Path) -> None:
    state_dir.mkdir(parents=True)
    target = tmp_path / "t"
    target.write_text('{"a":1}\n')
    (state_dir / "audit.jsonl").symlink_to(target)
    with pytest.raises(StateError):
        dedupe.audit_tail(state_dir)


def test_audit_tail_keeps_the_first_line_when_the_cut_is_on_a_line_boundary(
    state_dir: Path,
) -> None:
    write_audit(state_dir, [b'{"x":1}\n', b'{"y":2}\n'])
    assert dedupe.audit_tail(state_dir, max_bytes=8) == [{"y": 2}]


# --- guarded_lookup and the held bucket ---------------------------------------------------------


def fake_ctx(state_dir: Path) -> Any:
    from types import SimpleNamespace

    gw = SimpleNamespace(deadline=SimpleNamespace(remaining=lambda: 1000.0))
    return SimpleNamespace(site="main", rt=SimpleNamespace(state_dir=state_dir), gw=gw)


def test_guarded_lookup_under_the_matching_lock_runs_the_quick_rerun(state_dir: Path) -> None:
    name = dedupe.bucket_name("main", "DEMO", "k")
    with BucketLock(state_dir, name, timeout=1.0):
        pass  # a release: the next holder sees a fresh modification time
    v = Virtual(start=time.time())  # the lock file's time is real wall time
    look = hits_after(v, 2)
    with BucketLock(state_dir, name, timeout=1.0):
        got = dedupe.guarded_lookup(
            fake_ctx(state_dir), scope="DEMO", dedupe_key="k", lookup=look, clock=v.clock()
        )
    assert got == "found" and v.waits == [2.0]


def test_guarded_lookup_with_a_scope_that_is_not_the_held_bucket_is_an_internal_error(
    state_dir: Path,
) -> None:
    name = dedupe.bucket_name("main", "DEMO", "k")
    wrong = "OTHER"
    assert dedupe.bucket_name("main", wrong, "k") != name
    with BucketLock(state_dir, name, timeout=1.0):
        with pytest.raises(ValueError, match="bucket"):
            dedupe.guarded_lookup(
                fake_ctx(state_dir), scope=wrong, dedupe_key="k", lookup=lambda: None
            )
    with pytest.raises(ValueError, match="bucket"):  # no lock held at all
        dedupe.guarded_lookup(
            fake_ctx(state_dir), scope="DEMO", dedupe_key="k", lookup=lambda: None
        )
