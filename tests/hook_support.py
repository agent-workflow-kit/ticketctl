"""Helpers for tests that start hook processes (issue #25).

Every process a test starts is recorded at launch (pid plus its start time where ``/proc`` has
it) and, at teardown, exactly those that are still alive are killed: never by pattern, never a pid
that was reused by something else.
"""

from __future__ import annotations

import os
import shlex
import signal
import sys
import time
from collections.abc import Iterator
from pathlib import Path

import pytest


def _start_time(pid: int) -> str | None:
    try:
        stat = Path(f"/proc/{pid}/stat").read_text()
    except OSError:
        return None
    return stat.rsplit(")", 1)[-1].split()[19]  # field 22: starttime


class PidRegistry:
    def __init__(self) -> None:
        self.recorded: list[tuple[int, str | None]] = []

    def add(self, pid: int) -> None:
        self.recorded.append((pid, _start_time(pid)))

    def add_file(self, path: Path) -> list[int]:
        """Register the pids a script wrote (one per line) once the file exists."""
        pids = [int(line) for line in path.read_text().split()]
        for pid in pids:
            self.add(pid)
        return pids

    def kill_all(self) -> None:
        for pid, started in self.recorded:
            if started is not None and _start_time(pid) != started:
                continue  # reused or gone
            try:
                os.killpg(pid, signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
                pass
            try:
                os.kill(pid, signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
                pass


@pytest.fixture
def pids() -> Iterator[PidRegistry]:
    registry = PidRegistry()
    yield registry
    registry.kill_all()


def alive(pid: int) -> bool:
    """True while the pid exists and is not a zombie."""
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    try:
        state = Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[-1].split()[0]
    except OSError:
        return True
    return state != "Z"


def wait_dead(pid: int, seconds: float = 3.0) -> bool:
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        if not alive(pid):
            return True
        time.sleep(0.02)
    return not alive(pid)


def wait_file(path: Path, seconds: float = 5.0) -> bool:
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        if path.exists() and path.read_text().strip():
            return True
        time.sleep(0.02)
    return False


def script(directory: Path, name: str, body: str) -> list[str]:
    """A tiny Python script in ``directory`` and the argv that runs it with the test interpreter."""
    path = directory / name
    path.write_text(body, encoding="utf-8")
    return [sys.executable, "-I", str(path)]


def shell_command(argv: list[str]) -> str:
    """The ``sh -c`` text for an argv (what a configured hook's ``cmd`` looks like)."""
    return " ".join(shlex.quote(a) for a in argv)
