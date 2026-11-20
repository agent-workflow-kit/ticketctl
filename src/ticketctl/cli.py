"""Command-line entry point: parsing, flag rules, dispatch and error output."""

from __future__ import annotations

import argparse
import json
import os
import sys
import traceback
from typing import Any

from .commands import REGISTRY, CommandSpec
from .errors import TicketctlError, clean_message, internal_error, usage_error
from .flags import (
    GLOBAL_HELP,
    PROG,
    ArgError,
    Globals,
    Parser,
    check_globals,
    check_yes,
    extract_globals,
    wants_json,
)

DESCRIPTION = "Normalized ticket access (Jira first; other providers later)."
# Options whose value `-` reads stdin; `--set` and `--field` take `name=-`.
STDIN_FILE_OPTIONS = ("body_file", "title_file", "jql_file", "text_file")
STDIN_SET_OPTIONS = ("set", "field")


def build_parser() -> argparse.ArgumentParser:
    parser = Parser(prog=PROG, description=DESCRIPTION, epilog=GLOBAL_HELP, allow_abbrev=False)
    top = parser.add_subparsers(dest="_c0", metavar="<command>")
    top.required = True
    groups: dict[tuple[str, ...], Any] = {(): top}
    parsers: dict[tuple[str, ...], argparse.ArgumentParser] = {}

    def group_for(path: tuple[str, ...]) -> Any:
        """The subparsers action that holds the children of `path`."""
        if path not in groups:
            sub = parsers[path]
            action = sub.add_subparsers(dest=f"_c{len(path)}", metavar="<subcommand>")
            # `sites` alone is a command; `sprint` and `sprints` need a subcommand.
            action.required = path not in REGISTRY
            groups[path] = action
        return groups[path]

    def parser_for(path: tuple[str, ...], helptext: str) -> argparse.ArgumentParser:
        if path not in parsers:
            name = path[len(path) - 1]
            parent = group_for(path[:-1]) if len(path) > 1 else top
            parsers[path] = parent.add_parser(
                name,
                help=helptext,
                description=helptext,
                epilog=GLOBAL_HELP,
                allow_abbrev=False,
            )
        return parsers[path]

    for spec in sorted(REGISTRY.values(), key=lambda s: (len(s.path), s.path)):
        for i in range(1, len(spec.path)):
            parser_for(spec.path[:i], f"{spec.path[i - 1]} commands")
        spec.add_arguments(parser_for(spec.path, spec.help))
    return parser


def _command_path(ns: argparse.Namespace) -> tuple[str, ...]:
    path: list[str] = []
    i = 0
    while (v := getattr(ns, f"_c{i}", None)) is not None:
        path.append(v)
        i += 1
    return tuple(path)


def _stdin_sources(ns: argparse.Namespace) -> int:
    n = sum(1 for o in STDIN_FILE_OPTIONS if getattr(ns, o, None) == "-")
    for o in STDIN_SET_OPTIONS:
        n += sum(1 for v in getattr(ns, o, None) or [] if v.partition("=")[2] == "-")
    return n


def _exactly_one(ns: argparse.Namespace, a: str, b: str, label: str) -> None:
    if (getattr(ns, a) is None) == (getattr(ns, b) is None):
        raise usage_error("invalid_combination", f"exactly one of {label} is required")


def check_command(g: Globals, spec: CommandSpec, ns: argparse.Namespace) -> None:
    """Flag combination rules, in the order: argument grammar, combinations, `--yes`."""
    command = " ".join(spec.path)
    check_globals(g, spec.kind, command)
    if _stdin_sources(ns) > 1:
        raise usage_error("stdin_conflict", "at most one `-` source (stdin) per command")
    if spec.path == ("transition",):
        _exactly_one(ns, "to", "transition_id", "--to and --transition-id")
    elif spec.path == ("comment",):
        _exactly_one(ns, "body", "body_file", "--body and --body-file")
        if ns.format == "adf" and ns.body_file is None:
            raise usage_error("invalid_combination", "--format adf needs --body-file")
    elif spec.path == ("sprint", "remove") and ns.sprint is not None:
        raise usage_error("invalid_combination", "sprint remove takes no --sprint")
    # TODO(#27,#29,#30,#31,#34): the remaining "exactly one of" and per-command rules.
    check_yes(g, spec.path[0], getattr(ns, "dedupe_key", None))


def _write(stream: Any, text: str) -> None:
    # TODO(#7): the output scrub (token redaction, sanitizer) is applied here, the one output path.
    try:
        stream.write(text + "\n")
        stream.flush()
    except BrokenPipeError:
        pass


def _report(err: TicketctlError, as_json: bool) -> int:
    if as_json:
        _write(sys.stdout, json.dumps(err.envelope(), ensure_ascii=True, sort_keys=True))
    detail = f" [{err.reason}]" if err.reason else ""
    _write(sys.stderr, f"{PROG}: error: {err.code}{detail}: {clean_message(err.message)}")
    return err.exit


def run(argv: list[str]) -> int:
    g, rest = extract_globals(argv)
    parser = build_parser()
    try:
        ns = parser.parse_args(rest)
    except ArgError as e:
        raise usage_error("invalid_argument", clean_message(str(e))) from None
    spec = REGISTRY[_command_path(ns)]
    check_command(g, spec, ns)
    result = REGISTRY[spec.path].handler(g, ns)
    if g.json and result is not None:
        _write(sys.stdout, json.dumps(result, ensure_ascii=True, sort_keys=True))
    return 0


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    as_json = wants_json(args)
    try:
        return run(args)
    except TicketctlError as err:
        return _report(err, as_json)
    except Exception as exc:
        if os.environ.get("TICKETCTL_DEBUG"):
            traceback.print_exc()
        return _report(internal_error(exc), as_json)
