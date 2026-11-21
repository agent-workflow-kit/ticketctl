"""The hook process runner of the gateway (issue #25): own session, capped output, timeouts that
signal exactly the group it started, no zombies and no orphans, interrupts included.

Every process is recorded at launch (``on_start``) and the leftovers killed by exact pid.
"""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from tests import hook_support
from tests.hook_support import PidRegistry, alive, script, wait_dead, wait_file
from ticketctl import gateway
from ticketctl.gateway import run_hook, spawn_detached

pids = hook_support.pids  # the fixture, by name
ENV = {"PATH": "", "LC_ALL": "C"}

ECHO = """
import sys
data = sys.stdin.buffer.read()
sys.stdout.buffer.write(data)
sys.stderr.write("err:" + str(len(data)))
sys.exit(7)
"""


def go(argv: list[str], pids: PidRegistry, **kw: object) -> gateway.HookRun:
    kw.setdefault("stdin", b"")
    kw.setdefault("env", ENV)
    kw.setdefault("timeout", 10.0)
    return run_hook(argv, on_start=pids.add, **kw)  # type: ignore[arg-type]


def test_stdin_stdout_stderr_and_exit_code(tmp_path: Path, pids: PidRegistry) -> None:
    res = go(script(tmp_path, "echo.py", ECHO), pids, stdin=b'{"a": 1}')
    assert (res.returncode, res.stdout, res.stderr) == (7, b'{"a": 1}', b"err:8")
    assert not res.timed_out and not res.stdout_truncated and not alive(res.pid)


def test_death_by_signal_is_a_negative_code(tmp_path: Path, pids: PidRegistry) -> None:
    argv = script(tmp_path, "die.py", "import os, signal\nos.kill(os.getpid(), signal.SIGKILL)\n")
    assert go(argv, pids).returncode == -signal.SIGKILL


def test_the_child_gets_exactly_the_given_environment(tmp_path: Path, pids: PidRegistry) -> None:
    body = "import os, sys\nsys.stdout.write(','.join(sorted(os.environ)))\n"
    res = go(script(tmp_path, "env.py", body), pids, env={"B_VAR": "1", "A_VAR": "2"})
    names = set(res.stdout.decode().split(","))
    assert {"A_VAR", "B_VAR"} <= names
    assert names - {"A_VAR", "B_VAR", "LC_CTYPE"} == set()  # the interpreter adds LC_CTYPE itself


def test_the_child_is_a_session_leader_with_its_own_group(
    tmp_path: Path, pids: PidRegistry
) -> None:
    body = "import os\nprint(os.getpid(), os.getpgid(0), os.getsid(0))\n"
    res = go(script(tmp_path, "grp.py", body), pids)
    pid, pgid, sid = map(int, res.stdout.split())
    assert pid == pgid == sid == res.pid != os.getpgid(0)


def test_a_timeout_signals_the_whole_group_and_only_it(tmp_path: Path, pids: PidRegistry) -> None:
    marker = tmp_path / "pids"
    body = f"""
import os, subprocess, sys, time
child = subprocess.Popen([sys.executable, "-I", "-c", "import time; time.sleep(60)"])
open({str(marker)!r}, "w").write(f"{{os.getpid()}} {{child.pid}}\\n")
time.sleep(60)
"""
    start = time.monotonic()
    res = go(script(tmp_path, "slow.py", body), pids, timeout=0.6, kill_grace=1.0)
    assert res.timed_out and time.monotonic() - start < 4
    leader, grandchild = pids.add_file(marker)
    assert res.pid == leader and res.returncode == -signal.SIGTERM
    assert wait_dead(leader) and wait_dead(grandchild)
    assert os.getpgid(0) != leader and alive(os.getpid())  # we are untouched


def test_a_hook_that_ignores_sigterm_is_killed_after_the_grace(
    tmp_path: Path, pids: PidRegistry
) -> None:
    body = "import signal, time\nsignal.signal(signal.SIGTERM, signal.SIG_IGN)\ntime.sleep(60)\n"
    start = time.monotonic()
    res = go(script(tmp_path, "stubborn.py", body), pids, timeout=0.5, kill_grace=0.4)
    assert res.timed_out and res.returncode == -signal.SIGKILL
    assert time.monotonic() - start < 4 and not alive(res.pid)


def test_output_past_the_cap_is_dropped_and_the_child_never_blocks(
    tmp_path: Path, pids: PidRegistry
) -> None:
    body = (
        "import sys\n"
        "sys.stdout.buffer.write(b'x' * 3_000_000); sys.stdout.flush()\n"
        "sys.stderr.buffer.write(b'e' * 500_000)\n"
    )
    res = go(script(tmp_path, "loud.py", body), pids, out_cap=1000, err_cap=2000)
    assert res.returncode == 0 and not res.timed_out
    assert (len(res.stdout), len(res.stderr)) == (1000, 2000)
    assert res.stdout_truncated and res.stderr_truncated


def test_a_background_child_holding_the_pipes_does_not_hold_us_and_is_killed(
    tmp_path: Path, pids: PidRegistry
) -> None:
    marker = tmp_path / "bg"
    body = f"""
import os, subprocess, sys
child = subprocess.Popen([sys.executable, "-I", "-c", "import time; time.sleep(60)"])
open({str(marker)!r}, "w").write(str(child.pid))
"""
    start = time.monotonic()
    res = go(script(tmp_path, "daemonize.py", body), pids, drain=0.2)
    assert res.returncode == 0 and not res.timed_out and time.monotonic() - start < 4
    (orphan,) = pids.add_file(marker)
    assert wait_dead(orphan)  # the group is cleaned up, nothing is left behind


def test_a_child_that_never_reads_stdin_does_not_hang_the_run(
    tmp_path: Path, pids: PidRegistry
) -> None:
    body = "import time\ntime.sleep(60)\n"
    start = time.monotonic()
    res = go(script(tmp_path, "deaf.py", body), pids, stdin=b"x" * 5_000_000, timeout=0.5)
    assert res.timed_out and time.monotonic() - start < 5


def test_a_child_that_exits_without_reading_stdin_is_not_an_error(
    tmp_path: Path, pids: PidRegistry
) -> None:
    res = go(script(tmp_path, "quit.py", "import sys\nsys.exit(3)\n"), pids, stdin=b"y" * 3_000_000)
    assert res.returncode == 3 and not res.timed_out


def test_an_interrupt_midway_kills_the_group_and_reaps_the_leader(
    tmp_path: Path, pids: PidRegistry
) -> None:
    marker = tmp_path / "pids"
    body = f"""
import os, subprocess, sys, time
child = subprocess.Popen([sys.executable, "-I", "-c", "import time; time.sleep(60)"])
open({str(marker)!r}, "w").write(f"{{os.getpid()}} {{child.pid}}\\n")
time.sleep(60)
"""
    argv = script(tmp_path, "wait.py", body)

    def interrupt() -> None:
        wait_file(marker)
        os.kill(os.getpid(), signal.SIGINT)

    threading.Thread(target=interrupt, daemon=True).start()
    with pytest.raises(KeyboardInterrupt):
        go(argv, pids, timeout=30)
    leader, grandchild = pids.add_file(marker)
    assert wait_dead(leader) and wait_dead(grandchild)
    with pytest.raises(ProcessLookupError):
        os.kill(leader, 0)  # reaped: not even a zombie


@pytest.mark.parametrize("pidfd", [True, False])
@pytest.mark.parametrize("waitid", [True, False])
def test_every_wait_strategy_works(
    tmp_path: Path,
    pids: PidRegistry,
    monkeypatch: pytest.MonkeyPatch,
    pidfd: bool,
    waitid: bool,
) -> None:
    if not pidfd:
        monkeypatch.setattr(gateway, "_pidfd_open", lambda pid: None)
    if not waitid:
        monkeypatch.setattr(gateway, "_HAS_WAITID", False)
    ok = go(script(tmp_path, "echo.py", ECHO), pids, stdin=b"hi")
    assert (ok.returncode, ok.stdout) == (7, b"hi")
    slow = go(script(tmp_path, "slow.py", "import time\ntime.sleep(60)\n"), pids, timeout=0.4)
    assert slow.timed_out and not alive(slow.pid)


def test_a_missing_program_raises_and_leaves_nothing(pids: PidRegistry) -> None:
    with pytest.raises(OSError):
        go(["/nonexistent/ticketctl-test-program"], pids)


def test_spawn_detached_hands_over_the_payload_without_waiting(
    tmp_path: Path, pids: PidRegistry
) -> None:
    out = tmp_path / "got"
    body = f"import sys\nopen({str(out)!r}, 'w').write(sys.stdin.read())\n"
    pid = spawn_detached(
        script(tmp_path, "sink.py", body), stdin=b"payload" * 100_000, env=ENV, on_start=pids.add
    )
    assert pid > 0 and wait_file(out, 10)
    assert len(out.read_text()) == 700_000
    assert wait_dead(pid)  # reaped by its watcher, not a zombie


# ---------------------------------------------------------------------------------------------
# Round 1 hardening: set-up failures, SIGCHLD, SIGTERM and SIGHUP
# ---------------------------------------------------------------------------------------------


def _reaped(pid: int, seconds: float = 3.0) -> bool:
    """True once the pid is gone altogether (not even a zombie)."""
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return True
        time.sleep(0.02)
    return False


def test_a_failure_after_the_launch_still_kills_and_reaps_the_group(
    tmp_path: Path, pids: PidRegistry, monkeypatch: pytest.MonkeyPatch
) -> None:
    def boom() -> None:
        raise OSError("EMFILE")

    fake = SimpleNamespace(
        DefaultSelector=boom, EVENT_READ=1, EVENT_WRITE=2
    )  # as if no descriptor were left
    monkeypatch.setattr(gateway, "selectors", fake)
    seen: list[int] = []

    def remember(pid: int) -> None:
        seen.append(pid)
        pids.add(pid)

    with pytest.raises(OSError):
        run_hook(
            script(tmp_path, "sleep.py", "import time\ntime.sleep(60)\n"),
            stdin=b"",
            env=ENV,
            timeout=10.0,
            on_start=remember,
        )
    (pid,) = seen
    assert _reaped(pid)


def test_a_failing_on_start_still_reaps_a_detached_child(tmp_path: Path, pids: PidRegistry) -> None:
    seen: list[int] = []

    def remember(pid: int) -> None:
        seen.append(pid)
        pids.add(pid)
        raise RuntimeError("on_start failed")

    with pytest.raises(RuntimeError):
        spawn_detached(
            script(tmp_path, "quick.py", "pass\n"), stdin=b"x", env=ENV, on_start=remember
        )
    (pid,) = seen
    assert _reaped(pid)  # the watcher thread reaped it: no zombie


def test_an_ignored_sigchld_does_not_hide_the_exit_status(
    tmp_path: Path, pids: PidRegistry
) -> None:
    previous = signal.signal(signal.SIGCHLD, signal.SIG_IGN)
    try:
        res = go(script(tmp_path, "exit13.py", "import sys\nsys.exit(13)\n"), pids)
        assert res.returncode == 13
        assert signal.getsignal(signal.SIGCHLD) is signal.SIG_IGN  # put back as it was
    finally:
        signal.signal(signal.SIGCHLD, previous)


def test_handlers_are_put_back_after_a_run(tmp_path: Path, pids: PidRegistry) -> None:
    before = {s: signal.getsignal(s) for s in (signal.SIGTERM, signal.SIGHUP, signal.SIGCHLD)}
    go(script(tmp_path, "ok.py", "pass\n"), pids)
    assert {s: signal.getsignal(s) for s in before} == before


RUNNER = """
import sys
from ticketctl.gateway import run_hook
marker, hook = sys.argv[1], sys.argv[2]
def started(pgid):
    open(marker, "w").write(str(pgid))
run_hook([sys.executable, "-I", hook], stdin=b"", env={"PATH": ""}, timeout=60.0, on_start=started)
"""


@pytest.mark.parametrize("sig", [signal.SIGTERM, signal.SIGHUP])
def test_a_terminating_signal_kills_the_hook_group(
    tmp_path: Path, pids: PidRegistry, sig: signal.Signals
) -> None:
    hook = script(tmp_path, "forever.py", "import time\ntime.sleep(60)\n")[-1]
    marker = tmp_path / "pgid"
    runner = subprocess.Popen(  # noqa: S603
        [sys.executable, "-I", "-c", RUNNER, str(marker), hook],
        env={"PATH": "", "HOME": str(tmp_path), "XDG_CONFIG_HOME": str(tmp_path)},
        stderr=subprocess.DEVNULL,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
    )
    pids.add(runner.pid)
    try:
        assert wait_file(marker, 10)
        (pgid,) = pids.add_file(marker)
        runner.send_signal(sig)
        runner.wait(timeout=10)
        assert wait_dead(pgid) and _reaped(pgid)
    finally:
        if runner.poll() is None:
            runner.kill()
            runner.wait()


def test_a_second_signal_during_the_unwind_cannot_skip_the_reaping(
    tmp_path: Path, pids: PidRegistry, monkeypatch: pytest.MonkeyPatch
) -> None:
    """SIGTERM unwinds the run; a SIGHUP landing before the cleanup must not interrupt it."""
    resent: list[int] = []
    real_kill = os.kill

    def fake_kill(pid: int, sig: int) -> None:
        if pid == os.getpid() and sig == signal.SIGHUP:
            resent.append(sig)  # the deferred signal is re-sent after the handlers are restored
            return
        real_kill(pid, sig)

    monkeypatch.setattr(os, "kill", fake_kill)
    real_disarm = gateway._disarm

    def second_signal_first() -> None:
        gateway._on_terminate(signal.SIGHUP, None)  # lands in the gap before the real disarm
        real_disarm()

    monkeypatch.setattr(gateway, "_disarm", second_signal_first)
    seen: list[int] = []

    def first_signal(pid: int) -> None:
        seen.append(pid)
        pids.add(pid)
        gateway._on_terminate(signal.SIGTERM, None)

    with pytest.raises(KeyboardInterrupt):
        run_hook(
            script(tmp_path, "sleep.py", "import time\ntime.sleep(60)\n"),
            stdin=b"",
            env=ENV,
            timeout=10.0,
            on_start=first_signal,
        )
    (pgid,) = seen
    assert _reaped(pgid)  # group killed and leader waited, not a zombie
    assert resent == [signal.SIGHUP]


def test_a_failing_reaper_start_kills_and_reaps_the_detached_child(
    tmp_path: Path, pids: PidRegistry, monkeypatch: pytest.MonkeyPatch
) -> None:
    started: list[int] = []
    real_popen = subprocess.Popen

    def recording_popen(*args: object, **kwargs: object) -> subprocess.Popen[bytes]:
        proc = real_popen(*args, **kwargs)  # type: ignore[call-overload]
        started.append(proc.pid)
        pids.add(proc.pid)
        return proc  # type: ignore[no-any-return]

    class Boom:
        def __init__(self, *a: object, **k: object) -> None:
            pass

        def start(self) -> None:
            raise KeyboardInterrupt

    monkeypatch.setattr(subprocess, "Popen", recording_popen)
    monkeypatch.setattr(threading, "Thread", Boom)
    with pytest.raises(KeyboardInterrupt):
        spawn_detached(
            script(tmp_path, "sleep.py", "import time\ntime.sleep(60)\n"), stdin=b"x", env=ENV
        )
    (pid,) = started
    assert _reaped(pid)


def test_the_registry_never_kills_an_unverified_pid(monkeypatch: pytest.MonkeyPatch) -> None:
    sent: list[tuple[str, int]] = []
    monkeypatch.setattr(os, "killpg", lambda pid, sig: sent.append(("killpg", pid)))
    monkeypatch.setattr(os, "kill", lambda pid, sig: sent.append(("kill", pid)))
    registry = PidRegistry()
    registry.recorded = [(4_000_001, None), (os.getpid(), "not-the-start-time")]
    registry.kill_all()
    assert sent == []
