"""The confirmation on ``/dev/tty`` (docs/interface.md, Human actions). The human gate.

``confirm`` shows the dialog on stderr, discards pending terminal input, prints the prompt on the
terminal itself and reads one line from ``/dev/tty``: never from stdin, so a piped body does not
remove the human. Only ``y`` or ``yes`` (ASCII, any case, surrounding blanks trimmed) accepts.
An empty line, any other text, EOF (Ctrl-D), Ctrl-C, a line that is not ASCII, input that does
not end in a line within 4096 bytes, a read error and the timeout all decline. Nothing defaults
to yes.

``ConfirmUnavailable`` means the human cannot be asked (an ``auto:`` actor, stderr not a TTY, no
controlling terminal, ``/dev/tty`` not a character terminal, stderr not that controlling terminal
(the dialog would be shown on one terminal and asked on another), a descriptor ``select`` cannot
wait on): the engine turns it into exit 13
``confirmation_required``, never into an acceptance. ``/dev/tty`` is opened ``O_NOCTTY`` (a
session without a terminal does not acquire one), ``O_NOFOLLOW`` and close-on-exec, and checked
with ``fstat`` and ``isatty`` after the open, so a probe that passed a moment ago does not make
the open trustworthy. SIGTERM and SIGHUP keep their default action: the process ends before
anything is sent.
"""

from __future__ import annotations

import os
import select
import stat
import sys
import termios
import time
from collections.abc import Callable, Sequence
from typing import Any

from . import flags, output
from .confirm_dialog import compose
from .plan import ShownText

TTY_PATH = "/dev/tty"
PROMPT_TIMEOUT_S = 600.0
MAX_ANSWER_BYTES = 4096
PROMPT = "Send this? Type y or yes to accept, anything else declines [y/N]: "
ACCEPTED = frozenset({b"y", b"yes"})


class ConfirmUnavailable(Exception):
    """The human cannot be asked; the write is not confirmed."""


def open_tty() -> int:
    """Open the controlling terminal for reading and writing; ``ConfirmUnavailable`` otherwise."""
    flags_ = os.O_RDWR | os.O_NOCTTY | os.O_CLOEXEC | getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(TTY_PATH, flags_)
    except OSError:
        raise ConfirmUnavailable("no controlling terminal") from None
    try:
        if not stat.S_ISCHR(os.fstat(fd).st_mode) or not os.isatty(fd):
            raise ConfirmUnavailable("/dev/tty is not a terminal")
    except OSError:
        os.close(fd)
        raise ConfirmUnavailable("/dev/tty is not a terminal") from None
    except ConfirmUnavailable:
        os.close(fd)
        raise
    try:  # the dialog goes to stderr and the question to /dev/tty: they must be one terminal
        same = os.tcgetpgrp(2) == os.tcgetpgrp(fd)  # ENOTTY when stderr is another terminal
    except OSError:
        same = False
    if not same:
        os.close(fd)
        raise ConfirmUnavailable("stderr is not the controlling terminal")
    return fd


def parse_answer(raw: bytes | None) -> bool:
    """True only for ``y`` or ``yes``; ``None`` (EOF, timeout, error) and all else decline."""
    if raw is None or not raw.isascii():
        return False
    return raw.strip(b" \t\r").lower() in ACCEPTED


def read_line(fd: int, timeout_s: float) -> bytes | None:
    """One line (without its newline), or ``None`` for EOF, a timeout, an error or too much."""
    deadline = time.monotonic() + timeout_s
    buf = b""
    while True:
        left = deadline - time.monotonic()
        if left <= 0:
            return None
        try:
            ready, _, _ = select.select([fd], [], [], left)
            if not ready:
                continue
            chunk = os.read(fd, MAX_ANSWER_BYTES)
        except InterruptedError:
            continue
        except OSError:
            return None
        if not chunk:
            return None  # EOF; a partial line without its newline is never an answer
        buf += chunk
        if b"\n" in buf:
            line = buf.split(b"\n", 1)[0]
            return line if len(line) <= MAX_ANSWER_BYTES else None  # one read can hold it all
        if len(buf) > MAX_ANSWER_BYTES:
            return None


def _flush_input(fd: int) -> None:
    try:
        termios.tcflush(fd, termios.TCIFLUSH)
    except (termios.error, OSError):
        pass


def _say(fd: int, text: str) -> None:
    try:
        os.write(fd, text.encode("ascii", "replace"))
    except OSError:
        pass


def confirm(
    plan: dict[str, Any],
    shown: Sequence[ShownText] = (),
    *,
    override: bool = False,
    hook_names: Sequence[str] = (),
    timeout_s: float = PROMPT_TIMEOUT_S,
    opener: Callable[[], int] | None = None,
    stderr: Any = None,
) -> bool:
    """Show the dialog and read the answer from ``/dev/tty``; True only when accepted."""
    if str(plan.get("actor", "")).startswith("auto:"):
        raise ConfirmUnavailable("an automatic actor cannot be confirmed")
    if not flags.stderr_is_tty():
        raise ConfirmUnavailable("stderr is not a terminal")
    fd = (opener or open_tty)()
    try:
        try:
            select.select([fd], [], [], 0)  # select cannot wait on a descriptor of 1024 or more
        except ValueError:
            raise ConfirmUnavailable("the terminal descriptor is out of range") from None
        text = compose(plan, shown, override=override, hook_names=hook_names)
        output.write(stderr if stderr is not None else sys.stderr, text)
        _flush_input(fd)  # type-ahead typed before the prompt never answers it
        _say(fd, PROMPT)
        try:
            raw = read_line(fd, timeout_s)
        except KeyboardInterrupt:
            _say(fd, "\ninterrupted\n")
            return False
        accepted = parse_answer(raw)
        if raw is None:
            _say(fd, "\nno answer\n")
        _flush_input(fd)  # nothing the human typed past the answer reaches the shell
        return accepted
    finally:
        os.close(fd)
