"""Global flags: their grammars, the pre-pass that reads them from anywhere in argv."""

from __future__ import annotations

import argparse
import re
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import NoReturn

from . import __version__
from .errors import TicketctlError, clean_message, usage_error

PROG = "ticketctl"

# TODO(#7): the shared flag grammars move to sanitize.py; these copy the interface table.
ACTOR_RE = re.compile(r"(?:human|auto):[A-Za-z0-9._@-]{1,64}")
DECISION_ID_RE = re.compile(r"[A-Za-z0-9._:@/-]{1,128}")
SHA256_RE = re.compile(r"[0-9a-f]{64}")
HOOK_NAME_RE = re.compile(r"(?:pre|post)-(?:transition|comment|create|update|sprint)")
DURATION_RE = re.compile(r"([0-9]{1,9})(ms|s|m|h|d)?")
_UNIT_S = {"ms": 0.001, "s": 1.0, "m": 60.0, "h": 3600.0, "d": 86400.0, None: 1.0}
TIMEOUT_MIN_S = 1.0
TIMEOUT_MAX_S = 86400.0
OVERRIDE_SCOPE = "ticketctl"

# Flags that only make sense for a write; on a read command they exit 2 invalid_argument.
WRITE_ONLY_FLAGS = (
    "--yes",
    "--dry-run",
    "--expect-plan",
    "--actor",
    "--decision-id",
    "--override-escalation",
    "--hook",
)


class ArgError(Exception):
    """Raised by the parsers instead of printing usage and exiting."""


class Parser(argparse.ArgumentParser):
    def error(self, message: str) -> NoReturn:
        raise ArgError(message)


@dataclass
class Globals:
    json: bool = False
    config: str | None = None
    site: str | None = None
    verbose: bool = False
    quiet: bool = False
    timeout_s: float | None = None
    dry_run: bool = False
    yes: bool = False
    expect_plan: str | None = None
    actor: str | None = None
    decision_ids: list[str] = field(default_factory=list)
    override_escalation: str | None = None
    hooks: list[str] = field(default_factory=list)
    given: frozenset[str] = frozenset()  # option strings present on the command line


def _pattern(regex: re.Pattern[str], what: str):
    def check(value: str) -> str:
        if not regex.fullmatch(value):
            raise argparse.ArgumentTypeError(f"invalid {what}")
        return value

    return check


def parse_duration(value: str) -> float:
    """`<integer>(ms|s|m|h|d)` or a bare integer of seconds, 1 s to 24 h."""
    m = DURATION_RE.fullmatch(value)
    if not m:
        raise argparse.ArgumentTypeError("invalid duration (use <integer>[ms|s|m|h|d])")
    seconds = int(m.group(1)) * _UNIT_S[m.group(2)]
    if not TIMEOUT_MIN_S <= seconds <= TIMEOUT_MAX_S:
        raise argparse.ArgumentTypeError("duration must be between 1s and 24h")
    return seconds


def _hook(value: str) -> str:
    name, sep, command = value.partition("=")
    if not sep or not command or not HOOK_NAME_RE.fullmatch(name):
        raise argparse.ArgumentTypeError("invalid hook (use <name>=<command>)")
    return value


def build_global_parser() -> argparse.ArgumentParser:
    p = Parser(prog=PROG, add_help=False, allow_abbrev=False)
    p.add_argument("--version", action="version", version=f"{PROG} {__version__}")
    p.add_argument("--json", action="store_true")
    p.add_argument("--config", metavar="PATH")
    p.add_argument("--site", metavar="NAME")
    p.add_argument("--verbose", action="store_true")
    p.add_argument("--quiet", action="store_true")
    p.add_argument("--timeout", type=parse_duration, metavar="DURATION")
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--yes", action="store_true")
    p.add_argument("--expect-plan", type=_pattern(SHA256_RE, "sha256"), metavar="SHA256")
    p.add_argument("--actor", type=_pattern(ACTOR_RE, "actor"))
    p.add_argument(
        "--decision-id", action="append", default=[], type=_pattern(DECISION_ID_RE, "decision id")
    )
    p.add_argument("--override-escalation", action="store_true")
    p.add_argument("--hook", action="append", default=[], type=_hook, metavar="NAME=COMMAND")
    return p


GLOBAL_HELP = (
    "global flags (accepted before or after the command): --json --config PATH --site NAME "
    "--verbose --quiet --timeout DURATION --dry-run --yes --expect-plan SHA256 --actor ACTOR "
    "--decision-id ID --override-escalation[=ticketctl] --hook NAME=COMMAND --version"
)


def split_argv(argv: Sequence[str]) -> tuple[list[str], list[str]]:
    """Split at the first `--`: (flag part, tail that is never read as flags)."""
    if "--" in argv:
        i = list(argv).index("--")
        return list(argv[:i]), list(argv[i:])
    return list(argv), []


def wants_json(argv: Sequence[str]) -> bool:
    return "--json" in split_argv(argv)[0]


def extract_globals(argv: Sequence[str]) -> tuple[Globals, list[str]]:
    """Read the global flags from anywhere in argv; return them and the rest."""
    head, tail = split_argv(argv)
    scope: str | None = None
    prepared: list[str] = []
    for tok in head:
        if tok.startswith("--override-escalation="):
            scope = tok.split("=", 1)[1]
            if scope != OVERRIDE_SCOPE:
                raise usage_error(
                    "invalid_argument",
                    f"--override-escalation scope must be {OVERRIDE_SCOPE}",
                )
            prepared.append("--override-escalation")
        else:
            prepared.append(tok)
    try:
        ns, rest = build_global_parser().parse_known_args(prepared)
    except ArgError as e:
        raise usage_error("invalid_argument", clean_message(str(e))) from None
    given = frozenset(t.split("=", 1)[0] for t in head if t.startswith("--"))
    g = Globals(
        json=ns.json,
        config=ns.config,
        site=ns.site,
        verbose=ns.verbose,
        quiet=ns.quiet,
        timeout_s=ns.timeout,
        dry_run=ns.dry_run,
        yes=ns.yes,
        expect_plan=ns.expect_plan,
        actor=ns.actor,
        decision_ids=list(ns.decision_id),
        override_escalation=(scope or OVERRIDE_SCOPE) if ns.override_escalation else None,
        hooks=list(ns.hook),
        given=given,
    )
    return g, rest + tail


def check_globals(g: Globals, kind: str, command: str) -> None:
    """Flag combination rules that need only the global flags and the command kind."""
    if kind == "read":
        present = [f for f in WRITE_ONLY_FLAGS if f in g.given]
        if present:
            raise usage_error(
                "invalid_argument",
                f"{present[0]} is only valid on a write command, not on {command}",
            )
    if g.verbose and g.quiet:
        raise usage_error("invalid_combination", "--verbose and --quiet cannot be combined")
    if g.override_escalation and g.actor and g.actor.startswith("auto:"):
        raise usage_error("human_only", "--override-escalation needs a human actor")


def check_yes(g: Globals, command: str, dedupe_key: str | None) -> None:
    """`--yes` rules; a dry-run skips them (interface.md, Human actions)."""
    if not g.yes or g.dry_run:
        return
    if g.expect_plan is None:
        raise usage_error("yes_requires_expect_plan", "--yes always needs --expect-plan")
    if command in ("comment", "create") and dedupe_key is None:
        raise usage_error("dedupe_key_required", f"--yes with {command} needs --dedupe-key")


__all__ = [
    "Globals",
    "TicketctlError",
    "extract_globals",
    "check_globals",
    "check_yes",
    "parse_duration",
    "wants_json",
]
