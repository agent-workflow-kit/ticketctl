"""Importable worker functions for the multi-process state tests (``spawn`` start method).

Kept out of the ``test_*`` modules so a spawned child can import them by name. Nothing here
touches the real home: every function takes the private state directory explicitly.
"""

from __future__ import annotations

import fcntl
import os
import time
from multiprocessing.synchronize import Event
from pathlib import Path

from ticketctl import audit

LONG_PATH = "/p" * 1500


def audit_record(tag: str, i: int, *, result: str = "ok") -> dict:
    return {
        "attempt_id": f"w{tag}-{i:06d}",
        "step": 1,
        "step_kind": "transition",
        "actor": "human:tester",
        "actor_source": "flag",
        "stdin_tty": False,
        "decision_id": [],
        "target": {"site": "main", "key": "ABC-1"},
        "action": "transition",
        "inputs": {
            "plan_sha256": "a" * 64,
            "override_escalation": None,
            "config": {"path": LONG_PATH, "files_sha256": [], "sha256": "b" * 64},
            "hooks_sha256": "c" * 64,
            "site_host": "example.test",
            "cli_hooks": [],
            "token_env": "TEST_TOKEN",
        },
        "result": result,
        "provider_ids": [],
        "changed": None if result in ("attempted", "unknown") else True,
    }


def log_record(tag: str, i: int) -> dict:
    return {
        "site": "main",
        "host": "example.test",
        "config_sha256": "a" * 64,
        "method": "GET",
        "path": f"/rest/api/3/issue/{{key}}/w{tag}/n{i}",
        "status": 200,
        "duration_ms": i,
        "retries": 0,
        "rate_limit_remaining": None,
    }


def append_audits(state_dir: str, tag: str, count: int, go: Event) -> None:
    go.wait(30)
    for i in range(count):
        audit.append_audit(Path(state_dir), audit_record(tag, i))


def append_logs(state_dir: str, tag: str, count: int, max_bytes: int, go: Event) -> None:
    go.wait(30)
    for i in range(count):
        warning = audit.append_log(
            Path(state_dir), log_record(tag, i), max_bytes=max_bytes, warn=lambda m: None
        )
        assert warning is None, warning


def hold_flock(path: str, locked: Event) -> None:
    """Take the flock on ``path`` and sleep; the test kills this exact pid."""
    fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
    fcntl.flock(fd, fcntl.LOCK_EX)
    locked.set()
    time.sleep(120)
