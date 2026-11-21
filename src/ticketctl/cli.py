"""Command-line entry point: parsing, flag rules, dispatch and error output."""

from __future__ import annotations

import argparse
import json
import os
import sys
from typing import Any

from . import searchcmd, setspec
from .commands import REGISTRY, CommandSpec
from .errors import TicketctlError, clean_message, from_exception, internal_error, usage_error
from .flags import (
    GLOBAL_HELP,
    PROG,
    ArgError,
    Globals,
    Parser,
    check_globals,
    check_yes,
    extract_globals,
    resolve_write_actor,
    wants_json,
)
from .output import write as _write
from .runtime import load_runtime
from .sanitize import format_traceback

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


def _me_name(g: Globals) -> str | None:
    """`me.name` from the configuration; read only when the default actor needs it."""
    return load_runtime(g, show_warnings=False).config.me.name


def check_command(g: Globals, spec: CommandSpec, ns: argparse.Namespace) -> None:
    """Flag combination rules, in the order: argument grammar, combinations, `--yes`."""
    command = " ".join(spec.path)
    check_globals(g, spec.kind, command, resolve_actor=False)
    if _stdin_sources(ns) > 1:
        raise usage_error("stdin_conflict", "at most one `-` source (stdin) per command")
    if spec.path == ("transition",):
        _exactly_one(ns, "to", "transition_id", "--to and --transition-id")
    elif spec.path == ("comment",):
        _exactly_one(ns, "body", "body_file", "--body and --body-file")
        if ns.format == "adf" and ns.body_file is None:
            raise usage_error("invalid_combination", "--format adf needs --body-file")
    elif spec.path == ("search",):
        searchcmd.check_offline(ns)
    elif spec.path == ("sprint", "remove") and ns.sprint is not None:
        raise usage_error("invalid_combination", "sprint remove takes no --sprint")
    if spec.path in (("create",), ("update",), ("transition",)):
        flag = "--field" if spec.path == ("transition",) else "--set"
        setspec.check_offline(spec.path[0], getattr(ns, flag[2:]), flag)
    if spec.kind == "write":
        resolve_write_actor(g, me_name=lambda: _me_name(g))
    # TODO(#29,#30,#31,#34): the remaining "exactly one of" and per-command rules.
    check_yes(g, spec.path[0], getattr(ns, "dedupe_key", None))


def _report(err: TicketctlError, as_json: bool) -> int:
    if as_json:
        _write(sys.stdout, json.dumps(err.envelope(), ensure_ascii=True, sort_keys=True))
    detail = f" [{err.reason}]" if err.reason else ""
    _write(sys.stderr, f"{PROG}: error: {err.code}{detail}: {clean_message(err.message)}")
    # None of the keys of a multi-key `get` was read: one line per key, as `get` prints them.
    for item in [] if as_json else (err.details or {}).get("errors") or []:
        body = item["error"]
        _write(sys.stderr, f"error {item['key']}: {body['code']}: {clean_message(body['message'])}")
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
    result = spec.handler(g, ns)
    if result is not None:
        if g.json:
            _write(sys.stdout, json.dumps(result, ensure_ascii=True, sort_keys=True))
        elif spec.human is not None:
            _write(sys.stdout, spec.human(result))
    return 0


def _debug_traceback(exc: BaseException) -> None:
    """The traceback of `TICKETCTL_DEBUG`, sanitized and scrubbed like every other output."""
    if os.environ.get("TICKETCTL_DEBUG"):
        _write(sys.stderr, format_traceback(exc).rstrip("\n"))


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    as_json = wants_json(args)
    try:
        return run(args)
    except KeyboardInterrupt as exc:
        _debug_traceback(exc)
        _write(sys.stderr, f"{PROG}: interrupted")
        return 130
    except Exception as exc:
        err = from_exception(exc)
        if err is None:
            _debug_traceback(exc)
            err = internal_error(exc)
        return _report(err, as_json)
