"""The command registry: every command of docs/interface.md with its flags.

Handlers are stubs until the issue that owns each command lands (``whoami``, ``sites`` and
``validate`` live in ``handlers.py``); a stub raises NotImplementedError, which the top-level
handler reports as exit 1 `internal`.
The dispatcher looks a command up in REGISTRY at run time, so an owning issue
replaces its entry (and tests may patch it).
"""

from __future__ import annotations

import argparse
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from . import (
    changelogcmd,
    commentcmd,
    commentscmd,
    createcmd,
    exportcmd,
    fieldscmd,
    getcmd,
    handlers,
    searchcmd,
    sprintcmd,
    sprintscmd,
    transitioncmd,
    transitionscmd,
    updatecmd,
    writeflow,
)
from .flags import (
    Globals,
    count_arg,
    dedupe_key_arg,
    numeric_id_arg,
    seq_arg,
    sprint_selector_arg,
)

Handler = Callable[[Globals, argparse.Namespace], Any]
Adder = Callable[[argparse.ArgumentParser], None]

READ = "read"
WRITE = "write"


@dataclass(frozen=True)
class CommandSpec:
    path: tuple[str, ...]
    kind: str  # READ or WRITE
    help: str
    handler: Handler
    add_arguments: Adder
    human: Callable[[Any], str] | None = None  # plain-text form of the result (no `--json`)


def _none(p: argparse.ArgumentParser) -> None:
    return None


def _add(p: argparse.ArgumentParser, *names: str, **kw: Any) -> None:
    p.add_argument(*names, **kw)


def _whoami(p: argparse.ArgumentParser) -> None:
    _add(p, "--refresh", action="store_true", help="read the account fresh")


def _get(p: argparse.ArgumentParser) -> None:
    p.description = (
        "Read tickets by key or browse URL (at most 50 distinct keys, one site). A key that "
        "cannot be read is listed under errors and the others are still printed."
    )
    _add(p, "keys", metavar="KEY", nargs="+", help="ticket key or browse URL (at most 50 distinct)")
    _add(p, "--fields", help="comma list of the properties to read (default: all but comments)")
    _add(
        p,
        "--comments",
        type=getcmd.comments_arg,
        metavar="N",
        help="also read the latest N comments of each ticket (1 to 50)",
    )
    _add(p, "--include-adf", action="store_true", help="also print the description as ADF")


def _search(p: argparse.ArgumentParser) -> None:
    for flag in ("--query", "--jql-file", "--jql", "--text", "--text-file"):
        _add(p, flag)
    for flag in (
        "--project",
        "--status",
        "--assignee",
        "--sprint",
        "--label",
        "--parent",
        "--type",
        "--priority",
        "--key",
        "--not-status",
        "--not-label",
        "--not-parent",
        "--not-type",
    ):
        _add(p, flag, action="append")
    _add(p, "--status-category", action="append", choices=["new", "indeterminate", "done"])
    _add(p, "--updated-since")
    _add(p, "--created-since")
    _add(p, "--order-by")
    _add(p, "--limit", type=count_arg, metavar="N")
    _add(p, "--fields")
    _add(p, "--count", action="store_true")
    _add(p, "--cursor")


def _key(p: argparse.ArgumentParser) -> None:
    _add(p, "key", metavar="KEY")


def _comments(p: argparse.ArgumentParser) -> None:
    _key(p)
    _add(p, "--limit", type=count_arg, metavar="N")
    _add(p, "--since")
    _add(p, "--newest-first", action="store_true")
    _add(p, "--own", action="store_true")


def _changelog(p: argparse.ArgumentParser) -> None:
    _key(p)
    _add(p, "--limit", type=count_arg, metavar="N")
    _add(p, "--since")
    _add(p, "--field", action="append", metavar="NAME")
    _add(p, "--newest-first", action="store_true")


def _sprints_list(p: argparse.ArgumentParser) -> None:
    _add(p, "--state", choices=["future", "active", "closed"])
    _add(p, "--board")
    _add(p, "--project")
    _add(p, "--limit", type=count_arg, metavar="N")


def _sprints_show(p: argparse.ArgumentParser) -> None:
    _add(p, "id", metavar="ID", type=numeric_id_arg)
    _add(p, "--project", help="selects the site; a sprint is read by id, no board is needed")


def _sites_resolve(p: argparse.ArgumentParser) -> None:
    _add(p, "key", metavar="KEY_OR_URL")


def _validate(p: argparse.ArgumentParser) -> None:
    _add(p, "--probe", action="store_true")


def _export(p: argparse.ArgumentParser) -> None:
    _add(p, "--since")
    _add(p, "--after-seq", type=seq_arg, metavar="N")
    _add(p, "--to", metavar="PATH")


def _transition(p: argparse.ArgumentParser) -> None:
    _key(p)
    _add(p, "--to", type=transitioncmd.name_arg)
    _add(p, "--transition-id", type=numeric_id_arg)
    _add(p, "--resolution")
    _add(p, "--assign", type=transitioncmd.assign_arg, metavar="me|ACCOUNT")
    _add(p, "--field", action="append")
    _add(p, "--if-status", type=transitioncmd.name_arg)


def _comment(p: argparse.ArgumentParser) -> None:
    _key(p)
    _add(
        p,
        "--body",
        help="the comment text (Markdown); a body starting with '-' needs --body=TEXT "
        "or --body-file",
    )
    _add(p, "--body-file")
    _add(p, "--format", choices=["markdown", "adf"])
    _add(p, "--dedupe-key", type=dedupe_key_arg)


def _create(p: argparse.ArgumentParser) -> None:
    _add(p, "--project")
    _add(p, "--type")
    _add(p, "--title")
    _add(p, "--title-file")
    _add(p, "--body-file")
    _add(p, "--parent")
    _add(p, "--assign", type=updatecmd.assign_arg, metavar="me|none|ACCOUNT")
    _add(p, "--label", action="append")
    _add(p, "--set", action="append")
    _add(p, "--dedupe-key", type=dedupe_key_arg)


def _update(p: argparse.ArgumentParser) -> None:
    _key(p)
    _add(p, "--title")
    _add(p, "--title-file")
    _add(p, "--assign", type=updatecmd.assign_arg, metavar="me|none|ACCOUNT")
    _add(p, "--add-label", action="append")
    _add(p, "--remove-label", action="append")
    _add(p, "--set", action="append")


def _sprint_add(p: argparse.ArgumentParser) -> None:
    _key(p)
    _add(p, "--sprint", type=sprint_selector_arg, metavar="open|next|ID")
    _add(p, "--board")


def _sprint_remove(p: argparse.ArgumentParser) -> None:
    _key(p)
    _add(p, "--sprint")  # accepted only to be refused: invalid_combination
    _add(p, "--board")


def _specs() -> list[CommandSpec]:
    r, w = READ, WRITE
    return [
        CommandSpec(
            ("whoami",),
            r,
            "print the authenticated account",
            handlers.whoami,
            _whoami,
            handlers.human_whoami,
        ),
        CommandSpec(("get",), r, "read tickets", getcmd.get, _get, getcmd.human_get),
        CommandSpec(
            ("search",),
            r,
            "search tickets",
            searchcmd.search,
            _search,
            searchcmd.human_search,
        ),
        CommandSpec(
            ("comments",),
            r,
            "list comments",
            commentscmd.comments,
            _comments,
            commentscmd.human_comments,
        ),
        CommandSpec(
            ("changelog",),
            r,
            "list the change history",
            changelogcmd.changelog,
            _changelog,
            changelogcmd.human_changelog,
        ),
        CommandSpec(
            ("transitions",),
            r,
            "list available transitions",
            transitionscmd.transitions,
            _key,
            transitionscmd.human_transitions,
        ),
        CommandSpec(
            ("sprints", "list"),
            r,
            "list sprints",
            sprintscmd.sprints_list,
            _sprints_list,
            sprintscmd.human_sprints_list,
        ),
        CommandSpec(
            ("sprints", "show"),
            r,
            "show one sprint",
            sprintscmd.sprints_show,
            _sprints_show,
            sprintscmd.human_sprints_show,
        ),
        CommandSpec(
            ("fields", "list"),
            r,
            "list discovered fields",
            fieldscmd.fields_list,
            _none,
            fieldscmd.human_fields_list,
        ),
        CommandSpec(
            ("fields", "check"),
            r,
            "check the field mappings",
            fieldscmd.fields_check,
            _none,
            fieldscmd.human_fields_check,
        ),
        CommandSpec(
            ("fields", "refresh"),
            r,
            "replace the field cache",
            fieldscmd.fields_refresh,
            _none,
            fieldscmd.human_fields_refresh,
        ),
        CommandSpec(
            ("sites",), r, "list configured sites", handlers.sites, _none, handlers.human_sites
        ),
        CommandSpec(
            ("sites", "resolve"),
            r,
            "show which site a key selects",
            handlers.sites_resolve,
            _sites_resolve,
            handlers.human_sites_resolve,
        ),
        CommandSpec(
            ("validate",),
            r,
            "validate the configuration",
            handlers.validate,
            _validate,
            handlers.human_validate,
        ),
        CommandSpec(("export",), r, "print audit records", exportcmd.export, _export),
        CommandSpec(
            ("transition",),
            w,
            "transition a ticket",
            transitioncmd.transition,
            _transition,
            writeflow.human,
        ),
        CommandSpec(
            ("comment",), w, "post a comment", commentcmd.comment, _comment, writeflow.human
        ),
        CommandSpec(("create",), w, "create a ticket", createcmd.create, _create, writeflow.human),
        CommandSpec(
            ("update",),
            w,
            "update a ticket",
            updatecmd.update,
            _update,
            writeflow.human,
        ),
        CommandSpec(
            ("sprint", "add"),
            w,
            "add a ticket to a sprint",
            sprintcmd.sprint_add,
            _sprint_add,
            writeflow.human,
        ),
        CommandSpec(
            ("sprint", "remove"),
            w,
            "move a ticket to the backlog",
            sprintcmd.sprint_remove,
            _sprint_remove,
            writeflow.human,
        ),
    ]


REGISTRY: dict[tuple[str, ...], CommandSpec] = {s.path: s for s in _specs()}
