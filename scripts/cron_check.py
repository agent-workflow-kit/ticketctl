#!/usr/bin/env python3
"""Cron-style check of ticketctl (issue #39): a read and the write gates, with no terminal.

The same code serves two runs.

* The test suite (``tests/test_cron_env.py``) runs it against the fake provider with a scrubbed
  environment: only a configured ``PATH``, the state and config directories and the token
  variable, no ``TERM``, stdin from ``/dev/null``, a new session. The tests prove the harness
  gives each child ``stdin=DEVNULL`` and ``start_new_session=True`` and that a child sees a null
  stdin and leads its own session; they do not run the child under a real pty.
* A human runs this file from a real crontab: every step then runs in whatever environment cron
  really hands over, and the report shows what that was (names only).

Steps (each one is a ``ticketctl`` process, bounded by ``--timeout``):

1. ``get KEY`` (a read) exits 0.
2. ``--dry-run transition KEY --to NAME`` exits 0, prints a plan with ``actor`` ``auto:unknown``
   and writes no audit record.
3. ``transition KEY --to NAME`` with no ``--yes`` exits 13 ``confirmation_required`` at once (no
   prompt, no hang) and writes no audit record.
4. ``--yes`` without ``--expect-plan`` exits 2 ``yes_requires_expect_plan``.
5. With ``--apply`` only: ``--yes --expect-plan HASH transition ...`` exits 0 and the audit holds an
   ``attempted`` and an ``ok`` record whose ``actor`` is ``auto:unknown``, ``actor_source`` is
   ``default`` and ``stdin_tty`` is false. Step 5 is a REAL WRITE: point it at a throwaway ticket.
6. The token (the value of the ``--token-env`` variable, when set) appears in no output and no
   audit line.

The report prints variable NAMES only, never values. Exit status: 0 when every step passed, 1
when one failed, 2 on a usage error.
"""

from __future__ import annotations

import argparse
import errno
import json
import os
import shlex
import signal
import subprocess
import sys
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

EXPECTED_ACTOR = "auto:unknown"
EXPECTED_SOURCE = "default"
AUDIT_NAME = "audit.jsonl"


@dataclass
class Step:
    name: str
    ok: bool
    detail: str


@dataclass
class Report:
    steps: list[Step] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return all(s.ok for s in self.steps)

    def add(self, name: str, ok: bool, detail: str = "") -> None:
        self.steps.append(Step(name, ok, detail))

    def render(self) -> str:
        lines = [
            f"{'PASS' if s.ok else 'FAIL'} {s.name}" + (f": {s.detail}" if s.detail else "")
            for s in self.steps
        ]
        lines += [f"note {n}" for n in self.notes]
        lines.append("RESULT " + ("PASS" if self.ok else "FAIL"))
        return "\n".join(lines)


def state_dir(env: Mapping[str, str]) -> Path:
    """``$XDG_STATE_HOME/ticketctl`` (absolute only) or ``$HOME/.local/state/ticketctl``."""
    xdg = env.get("XDG_STATE_HOME", "")
    if xdg and os.path.isabs(xdg):
        return Path(xdg) / "ticketctl"
    return Path(env.get("HOME") or os.path.expanduser("~")) / ".local" / "state" / "ticketctl"


def has_controlling_terminal() -> bool:
    """True when this process can open ``/dev/tty`` (cron and ``setsid`` children cannot)."""
    try:
        fd = os.open("/dev/tty", os.O_RDWR | os.O_NOCTTY)
    except OSError as exc:
        if exc.errno in (errno.ENXIO, errno.ENODEV, errno.ENOENT, errno.EACCES, errno.EPERM):
            return False
        raise
    os.close(fd)
    return True


def minimal_env(
    *,
    path: str,
    state: Path,
    config_home: Path,
    token_env: str | None = None,
    token: str | None = None,
) -> dict[str, str]:
    """The scrubbed environment: a configured PATH and the directories, nothing else."""
    env = {"PATH": path, "XDG_STATE_HOME": str(state), "XDG_CONFIG_HOME": str(config_home)}
    if token_env and token is not None:
        env[token_env] = token
    return env


def _audit_text(env: Mapping[str, str]) -> str:
    try:
        return (state_dir(env) / AUDIT_NAME).read_text(encoding="utf-8")
    except FileNotFoundError:
        return ""


def _audit(env: Mapping[str, str]) -> list[dict[str, Any]]:
    return [json.loads(line) for line in _audit_text(env).splitlines() if line.strip()]


def _doc(out: str) -> dict[str, Any]:
    try:
        value = json.loads(out)
    except ValueError:
        return {}
    return value if isinstance(value, dict) else {}


def run_checks(
    ticketctl: Sequence[str],
    *,
    key: str,
    to: str,
    env: Mapping[str, str] | None,
    resolution: str | None = None,
    config: str | None = None,
    apply: bool = False,
    token_env: str | None = None,
    timeout: float = 60.0,
) -> Report:
    """Run the steps; ``env`` None means inherit the caller's environment (a real cron)."""
    effective = dict(os.environ) if env is None else dict(env)
    report = Report()
    outputs: list[str] = []
    target = ["--to", to, *(["--resolution", resolution] if resolution else [])]
    prefix = [*ticketctl, *(["--config", config] if config else []), "--json"]

    def run(*args: str) -> tuple[int | None, str]:
        # A new session makes the child a group leader, so a timeout can kill the whole group.
        proc = subprocess.Popen(
            [*prefix, *args],
            env=None if env is None else dict(env),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            start_new_session=True,
        )
        try:
            stdout, stderr = proc.communicate(timeout=timeout)
        except subprocess.TimeoutExpired:
            try:
                os.killpg(proc.pid, signal.SIGKILL)  # the group this run started, never a pattern
            except ProcessLookupError:
                pass
            proc.communicate()
            report.add("bounded: " + " ".join(args[:3]), False, f"no answer in {timeout} s")
            return None, ""
        outputs.extend([stdout, stderr])
        return proc.returncode, stdout

    report.notes.append("environment variable names: " + (", ".join(sorted(effective)) or "(none)"))
    report.notes.append("PATH entries: " + str(len(effective.get("PATH", "").split(os.pathsep))))
    if env is None:
        report.notes.append(
            "controlling terminal of the checker: "
            + ("PRESENT (not a cron run)" if has_controlling_terminal() else "none")
        )

    before = len(_audit(effective))

    code, out = run("get", key)
    read_code = (_doc(out).get("error") or {}).get("code")
    report.add(
        "read",
        code == 0 and bool(_doc(out)),
        f"exit {code}" + (f", code {read_code!r}" if read_code else ""),
    )

    code, out = run("--dry-run", "transition", key, *target)
    plan = _doc(out)
    held = plan.get("plan_sha256")
    report.add(
        "dry-run write",
        code == 0 and isinstance(held, str) and plan.get("actor") == EXPECTED_ACTOR,
        f"exit {code}, actor {plan.get('actor')!r}",
    )
    report.add("dry-run writes no audit", len(_audit(effective)) == before)

    code, out = run("transition", key, *target)
    error = _doc(out).get("error") or {}
    details = error.get("details") or {}
    carried = (details.get("plan") or {}).get("plan_sha256")
    same = isinstance(held, str) and carried == held
    report.add(
        "write without --yes is exit 13, no prompt",
        code == 13 and error.get("code") == "confirmation_required" and same,
        f"exit {code}, code {error.get('code')!r}, same hash as dry run: {same}",
    )
    report.add("unconfirmed write writes no audit", len(_audit(effective)) == before)

    code, out = run("--yes", "transition", key, *target)
    reason = (_doc(out).get("error") or {}).get("reason")
    report.add(
        "--yes without --expect-plan is exit 2",
        code == 2 and reason == "yes_requires_expect_plan",
        f"exit {code}, reason {reason!r}",
    )

    if apply:
        if not isinstance(held, str):
            report.add("--yes --expect-plan write", False, "no plan hash from the dry run")
        else:
            code, out = run("--yes", "--expect-plan", held, "transition", key, *target)
            result = _doc(out)
            records = _audit(effective)[before:]
            results = [r.get("result") for r in records]
            right = [
                r
                for r in records
                if r.get("actor") == EXPECTED_ACTOR
                and r.get("actor_source") == EXPECTED_SOURCE
                and r.get("stdin_tty") is False
            ]
            report.add(
                "--yes --expect-plan write",
                code == 0 and result.get("result") == "ok",
                f"exit {code}, result {result.get('result')!r}",
            )
            report.add(
                "audit actor, actor_source and stdin_tty",
                bool(records)
                and len(right) == len(records)
                and "attempted" in results
                and "ok" in results,
                f"results {results}, "
                f"actors {sorted({(r.get('actor'), r.get('actor_source')) for r in records})}",
            )
    else:
        report.notes.append("--apply not given: the --yes --expect-plan write was not run")

    secret = effective.get(token_env or "")
    if secret:
        blob = "\n".join(outputs) + _audit_text(effective)
        report.add("no token in output or audit", secret not in blob)
    else:
        report.notes.append("token variable not set in this environment: leak check skipped")
    return report


def main(argv: Sequence[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="Cron-style check of ticketctl (see the module doc).")
    p.add_argument("--ticketctl", default="ticketctl", help="the ticketctl command, shell-quoted")
    p.add_argument("--key", required=True, help="a ticket key the check may read and transition")
    p.add_argument("--to", required=True, help="the target status for the transition")
    p.add_argument("--resolution", help="the resolution a done target needs")
    p.add_argument("--config", help="a config file (default: the XDG location)")
    p.add_argument("--token-env", help="name of the token variable, for the leak check")
    p.add_argument("--apply", action="store_true", help="also run the real --yes write (step 5)")
    p.add_argument("--timeout", type=float, default=60.0)
    args = p.parse_args(argv)
    report = run_checks(
        shlex.split(args.ticketctl),
        key=args.key,
        to=args.to,
        env=None,
        resolution=args.resolution,
        config=args.config,
        apply=args.apply,
        token_env=args.token_env,
        timeout=args.timeout,
    )
    print(report.render())
    return 0 if report.ok else 1


if __name__ == "__main__":
    sys.exit(main())
