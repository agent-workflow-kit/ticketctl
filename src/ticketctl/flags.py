"""Global flags: their grammars, the pre-pass that reads them from anywhere in argv."""

from __future__ import annotations

import argparse
import re
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import NoReturn

from . import __version__
from .errors import clean_message, usage_error

PROG = "ticketctl"

# TODO(#7): the shared flag grammars move to sanitize.py; these copy the interface table.
ACTOR_RE = re.compile(r"(?:human|auto):[A-Za-z0-9._@-]{1,64}")
DECISION_ID_RE = re.compile(r"[A-Za-z0-9._:@/-]{1,128}")
NUMERIC_ID_RE = re.compile(r"[0-9]{1,18}")
SHA256_RE = re.compile(r"[0-9a-f]{64}")
HOOK_NAME_RE = re.compile(r"(?:pre|post)-(?:transition|comment|create|update|sprint)")
# TODO(#6): the duration grammar is owned by config.py; replace this copy (and parse_duration).
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


def _uint(max_digits: int, what: str):
    """A strict non-negative ASCII integer: no sign, space, underscore or non-ASCII digit."""
    regex = re.compile(f"[0-9]{{1,{max_digits}}}")

    def check(value: str) -> int:
        if not regex.fullmatch(value):
            raise argparse.ArgumentTypeError(f"invalid {what} (a non-negative integer)")
        return int(value)

    return check


count_arg = _uint(9, "number")
seq_arg = _uint(18, "sequence number")
dedupe_key_arg = _pattern(DECISION_ID_RE, "dedupe key")
numeric_id_arg = _pattern(NUMERIC_ID_RE, "numeric id")


def sprint_selector_arg(value: str) -> str:
    """`open`, `next` or a numeric sprint id."""
    if value in ("open", "next"):
        return value
    return numeric_id_arg(value)


def parse_duration(value: str) -> float:
    """TODO(#6): replace with the config duration grammar.

    `<integer>(ms|s|m|h|d)` or a bare integer of seconds, 1 s to 24 h."""
    m = DURATION_RE.fullmatch(value)
    if not m:
        raise argparse.ArgumentTypeError("invalid duration (use <integer>[ms|s|m|h|d])")
    seconds = int(m.group(1)) * _UNIT_S[m.group(2)]
    if not TIMEOUT_MIN_S <= seconds <= TIMEOUT_MAX_S:
        raise argparse.ArgumentTypeError("duration must be between 1s and 24h")
    return seconds


def _hook(value: str) -> str:
    # TODO(#7): the command text restriction (it accepts a newline today) belongs to the
    # sanitizer grammar.
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
    # TODO(#6): the duration grammar is owned by config.py.
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
    """`--json`, or `--json=<anything>` (which then fails in the parser), before `--`."""
    return any(t == "--json" or _json_with_value(t) for t in split_argv(argv)[0])


def _json_with_value(tok: str) -> bool:
    return tok.startswith("--json=") and not any(c.isspace() for c in tok)


# Global options by shape. A `--name=value` token is global only for a value option, and
# an option value that merely looks like a flag (`--text "--yes=1 foo"`) is not one.
_FLAG_OPTS = frozenset(
    {"--version", "--json", "--verbose", "--quiet", "--dry-run", "--yes", "--override-escalation"}
)
_VALUE_OPTS = frozenset(
    {"--config", "--site", "--timeout", "--expect-plan", "--actor", "--decision-id", "--hook"}
)


def _is_global_with_value(tok: str) -> bool:
    name, sep, value = tok.partition("=")
    if not sep:
        return False
    # The hook command text and a config path may hold spaces. Residual: a subcommand value that
    # starts with `--config=` or `--hook=` is read as the global option.
    if name in ("--hook", "--config"):
        return True
    if name == "--override-escalation" or name in _VALUE_OPTS:
        # Every other global value (and the override scope) has a grammar with no whitespace.
        return not any(c.isspace() for c in value)
    return False


def extract_globals(argv: Sequence[str]) -> tuple[Globals, list[str]]:
    """Read the global flags from anywhere in argv; return them and the rest."""
    head, tail = split_argv(argv)
    scope: str | None = None
    taken: list[str] = []  # the tokens the global parser will consume
    rest: list[str] = []
    given: set[str] = set()
    i = 0
    while i < len(head):
        tok = head[i]
        i += 1
        if tok in _FLAG_OPTS:
            taken.append(tok)
            given.add(tok)
        elif tok in _VALUE_OPTS:
            taken.append(tok)
            given.add(tok)
            # A following global option is not this option's value: leave it unfilled so
            # argparse reports "expected one argument" and the next token is read on its own.
            if i < len(head) and head[i] not in _FLAG_OPTS and head[i] not in _VALUE_OPTS:
                taken.append(head[i])
                i += 1
        elif _is_global_with_value(tok):
            name, _, value = tok.partition("=")
            given.add(name)
            if name == "--override-escalation":
                scope = value
                if scope != OVERRIDE_SCOPE:
                    raise usage_error(
                        "invalid_argument",
                        f"--override-escalation scope must be {OVERRIDE_SCOPE}",
                    )
                taken.append(name)
            else:
                taken.append(tok)
        else:
            rest.append(tok)
    try:
        ns, extra = build_global_parser().parse_known_args(taken)
    except ArgError as e:
        raise usage_error("invalid_argument", clean_message(str(e))) from None
    rest.extend(extra)
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
        given=frozenset(given),
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
    # TODO(#27): resolve the default actor (human:<me.name> on a TTY, else auto:unknown; needs
    # the config of #6) so a bare --override-escalation without --actor on a non-TTY is human_only.
    if g.override_escalation and g.actor and g.actor.startswith("auto:"):
        raise usage_error("human_only", "--override-escalation needs a human actor")


def check_yes(g: Globals, command: str, dedupe_key: str | None) -> None:
    """`--yes` rules; a dry-run skips them (interface.md, Human actions)."""
    if not g.yes or g.dry_run:
        return
    if g.expect_plan is None:
        raise usage_error("yes_requires_expect_plan", "--yes always needs --expect-plan")
    if dedupe_key is not None and not DECISION_ID_RE.fullmatch(dedupe_key):
        raise usage_error("invalid_argument", "invalid dedupe key")
    if command in ("comment", "create") and dedupe_key is None:
        raise usage_error("dedupe_key_required", f"--yes with {command} needs --dedupe-key")


__all__ = [
    "Globals",
    "extract_globals",
    "check_globals",
    "check_yes",
    "parse_duration",
    "wants_json",
]
