"""Importable worker functions for the multi-process dedupe lock tests (``spawn`` start method).

Every function takes the private state directory explicitly and touches nothing else.
"""

from __future__ import annotations

import time
from multiprocessing.synchronize import Event
from pathlib import Path

from ticketctl import dedupe


def hold_bucket(state_dir: str, name: str, locked: Event, seconds: float) -> None:
    """Take the bucket lock and sleep; the test kills this exact pid (or lets it finish)."""
    with dedupe.BucketLock(Path(state_dir), name, timeout=30.0):
        locked.set()
        time.sleep(seconds)


def counter_worker(state_dir: str, name: str, counter: str, rounds: int, go: Event) -> None:
    """A non-atomic read-modify-write of ``counter``, safe only while the lock excludes."""
    go.wait(30)
    for _ in range(rounds):
        with dedupe.BucketLock(Path(state_dir), name, timeout=60.0):
            value = int(Path(counter).read_text() or "0")
            time.sleep(0.002)
            Path(counter).write_text(str(value + 1))


def cli_worker(argv: list[str], out: str, ready: Event) -> None:
    """Run one ``ticketctl`` command in this process; its stdout goes to ``out``.

    ``ready`` is set right before the command starts (everything is imported by then).

    The exit code is written first on its own line, then the JSON document.
    """
    import contextlib
    import io

    from ticketctl.cli import main

    buf = io.StringIO()
    ready.set()
    with contextlib.redirect_stdout(buf):
        code = main(argv)
    Path(out).write_text(f"{code}\n{buf.getvalue()}")
