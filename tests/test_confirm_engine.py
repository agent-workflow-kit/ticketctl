"""The real dialog inside the write flow engine (issue #24).

The engine reaches ``confirm_tty`` when it decides a run is interactive and no test replaced the
dialog. The pty here is made in the test; ``open_tty`` and the two TTY probes are pointed at it.
"""

from __future__ import annotations

import os
import select
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import pytest

from tests.fake_provider import FakeProvider
from tests.test_writeflow import ATTEMPT, Hooks, World
from tests.write_stub import StubComment, StubTransition
from ticketctl import confirm_tty, flags
from ticketctl.writeflow import Seams


@pytest.fixture
def world(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fake: FakeProvider) -> World:
    return World(tmp_path, monkeypatch, fake)


MARK = confirm_tty.PROMPT.encode()


class Human:
    """A thread that waits for the prompt on the pty master and types ``answer``."""

    def __init__(self, master: int, answer: bytes | None) -> None:
        self.master = master
        self.answer = answer
        self.seen = b""
        self.thread = threading.Thread(target=self._run, daemon=True)
        self.thread.start()

    def _run(self) -> None:
        end = time.monotonic() + 20
        while time.monotonic() < end:
            ready, _, _ = select.select([self.master], [], [], 0.05)
            if not ready:
                continue
            try:
                chunk = os.read(self.master, 65536)
            except OSError:
                return
            if not chunk:
                return
            self.seen += chunk
            if MARK in self.seen and self.answer is not None:
                os.write(self.master, self.answer)
                self.answer = None
                return


@contextmanager
def terminal(
    monkeypatch: pytest.MonkeyPatch, answer: bytes | None, *, timeout: float | None = None
) -> Iterator[Human]:
    master, slave = os.openpty()
    monkeypatch.setattr(confirm_tty, "open_tty", lambda: os.dup(slave))
    monkeypatch.setattr(flags, "stderr_is_tty", lambda: True)
    monkeypatch.setattr(flags, "probe_dev_tty", lambda: True)
    if timeout is not None:
        monkeypatch.setattr(confirm_tty, "PROMPT_TIMEOUT_S", timeout)
        original = confirm_tty.confirm
        monkeypatch.setattr(
            "ticketctl.writeflow.confirm_tty.confirm",
            lambda *a, **kw: original(*a, **{**kw, "timeout_s": timeout}),
        )
    human = Human(master, answer)
    try:
        yield human
    finally:
        human.thread.join(timeout=5)
        os.close(slave)
        os.close(master)


def test_accept_sends_and_the_dialog_shows_the_body(
    world: World,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    with terminal(monkeypatch, b"y\n"):
        out = _run_real(world, StubComment("DEMO-2", "the **comment** text"))
    assert out["result"] == "ok"
    assert len(world.writes()) == 1 and len(world.records()) == 2
    err = capsys.readouterr().err
    assert "the **comment** text" in err and "Drafted with AI assistance" in err


def _run_real(world: World, action: Any, **flags_: Any) -> dict[str, Any]:
    seams = Seams()  # no interactive probe, no confirm: the real dialog
    seams.attempt_id = lambda: ATTEMPT
    from ticketctl.writeflow import execute

    return execute(world.rt(**flags_), "main", action, seams)


def _fail_real(world: World, action: Any, **flags_: Any) -> Any:
    from ticketctl.errors import TicketctlError

    with pytest.raises(TicketctlError) as info:
        _run_real(world, action, **flags_)
    return info.value


@pytest.mark.parametrize("answer", [b"n\n", b"\n", b"\x04", b"yes!\n"])
def test_a_decline_is_exit_20_with_nothing_written(
    world: World,
    monkeypatch: pytest.MonkeyPatch,
    answer: bytes,
) -> None:
    with terminal(monkeypatch, answer):
        err = _fail_real(world, StubTransition("DEMO-2", "Done"))
    assert (err.exit, err.code, err.reason) == (20, "declined", "declined")
    assert err.details is not None and err.details["plan"]["plan_sha256"]
    assert world.writes() == [] and world.records() == []


def test_a_timeout_is_a_decline(world: World, monkeypatch: pytest.MonkeyPatch) -> None:
    with terminal(monkeypatch, None, timeout=0.5):
        err = _fail_real(world, StubTransition("DEMO-2", "Done"))
    assert err.code == "declined" and world.writes() == [] and world.records() == []


def test_a_terminal_that_cannot_be_opened_is_confirmation_required(
    world: World,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fail() -> int:
        raise confirm_tty.ConfirmUnavailable("no controlling terminal")

    monkeypatch.setattr(confirm_tty, "open_tty", fail)
    monkeypatch.setattr(flags, "stderr_is_tty", lambda: True)
    monkeypatch.setattr(flags, "probe_dev_tty", lambda: True)  # the race: probe ok, open fails
    err = _fail_real(world, StubTransition("DEMO-2", "Done"))
    assert (err.exit, err.code) == (13, "confirmation_required")
    assert world.writes() == [] and world.records() == []


def test_an_automatic_actor_is_never_asked_even_when_a_seam_says_interactive(
    world: World,
) -> None:
    asked: list[Any] = []
    seams = Seams(interactive=lambda: True, confirm=lambda p: asked.append(p) or True)
    err = world.fail(StubTransition("DEMO-2", "Done"), actor="auto:bot", seams=seams)
    assert (err.exit, err.code) == (13, "confirmation_required")
    assert asked == [] and world.writes() == []


def test_an_automatic_actor_on_the_real_path_never_opens_the_terminal(
    world: World,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def boom() -> int:
        raise AssertionError("the terminal was opened for an auto: actor")

    monkeypatch.setattr(confirm_tty, "open_tty", boom)
    monkeypatch.setattr(flags, "stderr_is_tty", lambda: True)
    monkeypatch.setattr(flags, "probe_dev_tty", lambda: True)
    err = _fail_real(world, StubTransition("DEMO-2", "Done"), actor="auto:bot")
    assert err.code == "confirmation_required"


def test_yes_skips_the_dialog_as_before(world: World, monkeypatch: pytest.MonkeyPatch) -> None:
    def boom() -> int:
        raise AssertionError("the dialog ran under --yes")

    monkeypatch.setattr(confirm_tty, "open_tty", boom)
    held = world.hash_of(StubTransition("DEMO-2", "Done"))
    out = world.run(StubTransition("DEMO-2", "Done"), yes=True, expect_plan=held)
    assert out["result"] == "ok"


def test_override_names_the_hooks_it_covers(
    world: World,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    hooks = Hooks()
    seams = Seams(hooks=hooks)
    seams.attempt_id = lambda: ATTEMPT
    from ticketctl.writeflow import execute

    rt = world.rt(
        override_escalation="ticketctl", hooks=["pre-transition=true", "post-transition=true"]
    )
    with terminal(monkeypatch, b"y\n"):
        execute(rt, "main", StubTransition("DEMO-2", "Done"), seams)
    err = capsys.readouterr().err
    assert "escalation override is ON" in err
    assert "hooks covered: pre-transition, post-transition" in err
    assert len(hooks.pre_calls) == 1  # the hook ran after the answer, not before


def test_no_hook_runs_before_the_answer(world: World, monkeypatch: pytest.MonkeyPatch) -> None:
    hooks = Hooks()
    seams = Seams(hooks=hooks)
    seams.attempt_id = lambda: ATTEMPT
    from ticketctl.errors import TicketctlError
    from ticketctl.writeflow import execute

    with terminal(monkeypatch, b"n\n"), pytest.raises(TicketctlError) as info:
        execute(world.rt(), "main", StubTransition("DEMO-2", "Done"), seams)
    assert info.value.code == "declined"
    assert hooks.pre_calls == [] and hooks.post_calls == []
