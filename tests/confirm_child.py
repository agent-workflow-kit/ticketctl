"""The child process of the pty tests (issue #24): runs the real dialog against the terminal it
was given and writes what happened to a result file. Never imported by a test; started by
``tests/test_confirm_tty.py`` with ``python -I``. Synthetic data only.
"""

from __future__ import annotations

import fcntl
import json
import os
import sys
import termios

from ticketctl import confirm_tty
from ticketctl.plan import ShownText


def main() -> int:
    cfg = json.loads(sys.argv[1])
    result_path = sys.argv[2]
    if cfg.get("ctty_fd") is not None:
        # The test made this process a session leader without a terminal; give it the pty.
        fcntl.ioctl(cfg["ctty_fd"], termios.TIOCSCTTY, 0)
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
    try:
        os.close(os.open("/dev/tty", os.O_RDWR | os.O_NOCTTY))
        outcome["tty_after"] = True
    except OSError:
        outcome["tty_after"] = False
    with open(result_path, "w", encoding="utf-8") as fh:
        json.dump(outcome, fh)
    return 0


if __name__ == "__main__":
    sys.exit(main())
