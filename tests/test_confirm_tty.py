"""The confirmation on /dev/tty, against real pseudo-terminals (issue #24).

Each case starts ``tests/confirm_child.py`` in a new session whose stdio is a pty created here
(``os.openpty``); the child acquires that pty as its controlling terminal, so ``/dev/tty`` is the
test's pty and never the user's. The test answers on the pty master. Every child's PID is
recorded and only that PID is ever signalled. HOME and the config home are private, PATH is empty.
"""

from __future__ import annotations

import json
import os
import re
import select
import signal
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

import pytest

from ticketctl import confirm_tty
from ticketctl.confirm_tty import ConfirmUnavailable, parse_answer

CHILD = Path(__file__).with_name("confirm_child.py")
PROMPT = confirm_tty.PROMPT.encode()
CTRL_D = b"\x04"
CTRL_C = b"\x03"


class Session:
    """One child on one pty. ``answer`` runs after the prompt appears; ``before`` before spawn."""

    def __init__(self, tmp: Path, **cfg: Any) -> None:
        self.tmp = tmp
        self.cfg = {"timeout": 8.0, **cfg}
        self.out = b""
        self.result: dict[str, Any] | None = None
        self.returncode: int | None = None
        self.pid: int | None = None

    def _drain(self, master: int, seconds: float, until: bytes | None = None) -> None:
        end = time.monotonic() + seconds
        while time.monotonic() < end:
            if until is not None and until in self.out:
                return
            ready, _, _ = select.select([master], [], [], 0.05)
            if ready:
                try:
                    chunk = os.read(master, 65536)
                except OSError:
                    return
                if not chunk:
                    return
                self.out += chunk

    def run(
        self,
        answer: bytes | None = None,
        *,
        before: bytes | None = None,
        ctty: bool = True,
        stdin: bytes | None = None,
        stderr_is_pty: bool = True,
        signal_after_prompt: int | None = None,
        wait_prompt: bool = True,
    ) -> Session:
        master, slave = os.openpty()
        result_file = self.tmp / "result.json"
        cfg = dict(self.cfg)
        cfg["ctty_fd"] = 1 if ctty else None
        err_pipe = None if stderr_is_pty else subprocess.PIPE
        env = {
            "PATH": "",
            "HOME": str(self.tmp),
            "XDG_CONFIG_HOME": str(self.tmp / ".config"),
            "PYTHONDONTWRITEBYTECODE": "1",
        }
        proc: subprocess.Popen[bytes] | None = None
        try:
            if before is not None:
                os.write(master, before)  # type-ahead, typed before the child starts
            proc = subprocess.Popen(  # noqa: S603 - this interpreter and our own helper script
                [sys.executable, "-I", str(CHILD), json.dumps(cfg), str(result_file)],
                stdin=subprocess.PIPE if stdin is not None else subprocess.DEVNULL,
                stdout=slave,
                stderr=slave if err_pipe is None else err_pipe,
                env=env,
                start_new_session=True,
                close_fds=True,
            )
            self.pid = proc.pid
            if stdin is not None and proc.stdin is not None:
                proc.stdin.write(stdin)
                proc.stdin.close()
            if wait_prompt:
                self._drain(master, 10.0, until=PROMPT)
            if signal_after_prompt is not None:
                os.kill(self.pid, signal_after_prompt)  # the recorded child only
            elif answer is not None:
                os.write(master, answer)
            try:
                self.returncode = proc.wait(timeout=15)
            finally:
                self._drain(master, 0.3)
        finally:
            if proc is not None and proc.poll() is None:
                proc.kill()  # exactly our child
                proc.wait(timeout=5)
            os.close(slave)
            os.close(master)
        if result_file.exists():
            self.result = json.loads(result_file.read_text())
        return self


def decided(tmp_path: Path, answer: bytes, **kw: Any) -> str:
    s = Session(tmp_path).run(answer, **kw)
    assert s.result is not None, s.out
    return str(s.result["result"])


# --------------------------------------------------------------------------------------------
# The answer
# --------------------------------------------------------------------------------------------


@pytest.mark.parametrize("answer", [b"y\n", b"yes\n", b"Y\n", b"YES\n", b"  yEs \n", b"y\r"])
def test_only_y_or_yes_accepts(tmp_path: Path, answer: bytes) -> None:
    assert decided(tmp_path, answer) == "accepted"


@pytest.mark.parametrize(
    "answer",
    [
        b"n\n",
        b"no\n",
        b"\n",
        b"   \n",
        b"yess\n",
        b"y es\n",
        b"yes please\n",
        b"ye\n",
        b"true\n",
        b"1\n",
        b"ok\n",
        b"\xef\xbd\x99\n",  # a fullwidth y
        b"\xffy\n",  # not UTF-8
        b"y" * 10_000 + b"\n",
        b"yes\x00\n",
        b"\x1b[Ay\n",
    ],
)
def test_everything_else_declines(tmp_path: Path, answer: bytes) -> None:
    assert decided(tmp_path, answer) == "declined"


def test_eof_declines(tmp_path: Path) -> None:
    assert decided(tmp_path, CTRL_D) == "declined"


def test_a_partial_line_then_eof_declines(tmp_path: Path) -> None:
    assert decided(tmp_path, b"y" + CTRL_D + CTRL_D) == "declined"


def test_ctrl_c_declines_and_the_child_ends_cleanly(tmp_path: Path) -> None:
    s = Session(tmp_path).run(CTRL_C)
    assert s.result is not None and s.result["result"] == "declined"
    assert s.returncode == 0
    assert b"interrupted" in s.out


def test_timeout_declines(tmp_path: Path) -> None:
    s = Session(tmp_path, timeout=1.0).run(None)
    assert s.result is not None and s.result["result"] == "declined"
    assert b"no answer" in s.out


def test_sigterm_ends_the_process_without_an_answer(tmp_path: Path) -> None:
    s = Session(tmp_path).run(signal_after_prompt=signal.SIGTERM)
    assert s.result is None
    assert s.returncode == -signal.SIGTERM


def test_type_ahead_before_the_prompt_does_not_answer_it(tmp_path: Path) -> None:
    s = Session(tmp_path, timeout=1.5).run(None, before=b"y\nyes\n", wait_prompt=False)
    assert s.result is not None and s.result["result"] == "declined"


def test_type_ahead_that_is_not_flushed_would_have_answered(tmp_path: Path) -> None:
    # Control: the same bytes typed after the prompt do accept, so the case above proves the flush.
    assert decided(tmp_path, b"y\n") == "accepted"


def test_a_piped_stdin_does_not_answer_and_is_never_read(tmp_path: Path) -> None:
    s = Session(tmp_path, timeout=1.5).run(None, stdin=b"y\nyes\ny\n")
    assert s.result is not None and s.result["result"] == "declined"
    s2 = Session(tmp_path).run(b"n\n", stdin=b"y\ny\n")
    assert s2.result is not None and s2.result["result"] == "declined"


# --------------------------------------------------------------------------------------------
# What is shown
# --------------------------------------------------------------------------------------------


def test_the_dialog_precedes_the_prompt_and_control_characters_are_markers(tmp_path: Path) -> None:
    text = "ok\x1b[2J\x1b]0;spoof\x07 Send this? y‮"
    s = Session(tmp_path, text=text).run(b"n\n")
    assert b"hello" not in s.out
    out = s.out
    assert out.index(b"| ok<U+001B>") < out.index(PROMPT)
    assert b"<U+202E>" in out and b"spoof" in out
    assert not re.search(rb"\x1b|\x07", out.split(PROMPT)[0])


def test_the_prompt_text_holds_no_environment_value(tmp_path: Path) -> None:
    s = Session(tmp_path).run(b"n\n")
    assert str(tmp_path).encode() not in s.out


# --------------------------------------------------------------------------------------------
# When the human cannot be asked
# --------------------------------------------------------------------------------------------


def test_no_controlling_terminal_is_unavailable_and_none_is_acquired(tmp_path: Path) -> None:
    s = Session(tmp_path).run(None, ctty=False, wait_prompt=False)
    assert s.result is not None
    assert s.result["result"] == "unavailable" and s.result["tty_after"] is False
    assert PROMPT not in s.out


def test_stderr_that_is_not_a_terminal_is_unavailable(tmp_path: Path) -> None:
    s = Session(tmp_path).run(None, stderr_is_pty=False, wait_prompt=False)
    assert s.result is not None and s.result["result"] == "unavailable"
    assert PROMPT not in s.out


def test_an_automatic_actor_is_never_asked(tmp_path: Path) -> None:
    s = Session(tmp_path, actor="auto:unknown").run(b"y\n", wait_prompt=False)
    assert s.result is not None and s.result["result"] == "unavailable"
    assert PROMPT not in s.out


# --------------------------------------------------------------------------------------------
# open_tty and parse_answer, in this process
# --------------------------------------------------------------------------------------------


def test_open_tty_accepts_a_real_pty_and_refuses_everything_else(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    master, slave = os.openpty()
    try:
        name = os.ttyname(slave)
        monkeypatch.setattr(confirm_tty, "TTY_PATH", name)
        fd = confirm_tty.open_tty()
        assert os.isatty(fd)
        os.close(fd)
        link = tmp_path / "tty"
        link.symlink_to(name)
        monkeypatch.setattr(confirm_tty, "TTY_PATH", str(link))
        with pytest.raises(ConfirmUnavailable):  # O_NOFOLLOW: a symlink is not the terminal
            confirm_tty.open_tty()
        plain = tmp_path / "plain"
        plain.write_text("x")
        for target in (str(plain), "/dev/null", str(tmp_path / "missing")):
            monkeypatch.setattr(confirm_tty, "TTY_PATH", target)
            with pytest.raises(ConfirmUnavailable):
                confirm_tty.open_tty()
    finally:
        os.close(slave)
        os.close(master)


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        (b"y", True),
        (b"yes", True),
        (b" Yes\t", True),
        (b"YES\r", True),
        (None, False),
        (b"", False),
        (b"yes!", False),
        (b"\xc5\xbfes", False),  # a long s
        (b"\xc4\xb0", False),
        (b"y\x00", False),
        (b"yy", False),
    ],
)
def test_parse_answer(raw: bytes | None, expected: bool) -> None:
    assert parse_answer(raw) is expected


def test_read_line_gives_up_on_a_closed_peer_and_on_overlong_input() -> None:
    master, slave = os.openpty()
    try:
        os.close(master)
        assert confirm_tty.read_line(slave, 1.0) is None
    finally:
        os.close(slave)
    r, w = os.pipe()
    try:
        os.write(w, b"a" * (confirm_tty.MAX_ANSWER_BYTES + 10))
        assert confirm_tty.read_line(r, 1.0) is None
    finally:
        os.close(r)
        os.close(w)
