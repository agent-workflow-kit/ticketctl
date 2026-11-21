"""The child process of the pty tests (issue #24): runs the real dialog against the terminal it
was given and writes what happened to a result file. Never imported by a test; started by
``tests/test_confirm_tty.py`` with ``python -I``. Synthetic data only.
"""

from __future__ import annotations

import fcntl
import json
import os
import resource
import select
import sys
import termios
import tty

# A regressed cap must fail fast, not eat memory: the child can never use more than 256 MiB.
_LIMIT = 256 * 1024 * 1024
try:
    resource.setrlimit(resource.RLIMIT_AS, (_LIMIT, _LIMIT))
except (ValueError, OSError):  # not every platform (macOS) allows an address-space limit
    pass

from ticketctl import confirm_tty  # noqa: E402 - after the limit, so imports obey it
from ticketctl.plan import ShownText  # noqa: E402


def main() -> int:
    cfg = json.loads(sys.argv[1])
    result_path = sys.argv[2]
    if cfg.get("ctty_fd") is not None:
        # The test made this process a session leader without a terminal; give it the pty.
        fcntl.ioctl(cfg["ctty_fd"], termios.TIOCSCTTY, 0)
    if cfg.get("raw"):
        tty.setraw(1)  # the line discipline no longer caps a line at 4096 bytes
    if cfg.get("no_flush"):
        confirm_tty._flush_input = lambda fd: None  # noqa: SLF001 - controls the flush in a test
    held = []
    if cfg.get("many_fds"):
        soft, hard = resource.getrlimit(resource.RLIMIT_NOFILE)
        top = hard if hard != resource.RLIM_INFINITY else 4096
        try:
            resource.setrlimit(resource.RLIMIT_NOFILE, (min(top, 4096), hard))
        except (ValueError, OSError):
            pass
        held = [os.dup(0) for _ in range(cfg["many_fds"])]
    plan = {
        "action": "comment",
        "site": "main",
        "key": "DEMO-1",
        "actor": cfg.get("actor", "human:tester"),
        "steps": [{"kind": "add_comment", "body_sha256": "0" * 64, "body_bytes": 3}],
        "changes": [],
        "warnings": [],
        "plan_sha256": "ab" * 32,
    }
    shown = [ShownText("comment", cfg.get("text", "hello body"))]
    outcome: dict[str, object] = {}
    try:
        accepted = confirm_tty.confirm(plan, shown, timeout_s=cfg.get("timeout", 5.0))
        outcome["result"] = "accepted" if accepted else "declined"
    except confirm_tty.ConfirmUnavailable as exc:
        outcome["result"] = "unavailable"
        outcome["why"] = str(exc)
    for extra in held:
        os.close(extra)
    try:
        fd = os.open("/dev/tty", os.O_RDWR | os.O_NOCTTY)
        outcome["tty_after"] = True
        ready, _, _ = select.select([fd], [], [], 0.3)
        outcome["leftover"] = os.read(fd, 4096).decode("latin-1") if ready else ""
        os.close(fd)
    except (OSError, ValueError):
        outcome["tty_after"] = False
    with open(result_path, "w", encoding="utf-8") as fh:
        json.dump(outcome, fh)
    return 0


if __name__ == "__main__":
    sys.exit(main())
