"""The command registry: every command of docs/interface.md with its flags.

Handlers are stubs until the issue that owns each command lands; a stub raises
NotImplementedError, which the top-level handler reports as exit 1 `internal`.
The dispatcher looks a command up in REGISTRY at run time, so an owning issue
replaces its entry (and tests may patch it).
"""

from __future__ import annotations

import argparse
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

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


def _stub(owner: int) -> Handler:
    def handler(g: Globals, a: argparse.Namespace) -> Any:
        raise NotImplementedError(f"command is owned by #{owner}")

    return handler


def _none(p: argparse.ArgumentParser) -> None:
    return None


def _add(p: argparse.ArgumentParser, *names: str, **kw: Any) -> None:
    p.add_argument(*names, **kw)


def _whoami(p: argparse.ArgumentParser) -> None:
    _add(p, "--refresh", action="store_true", help="read the account fresh")


def _get(p: argparse.ArgumentParser) -> None:
    _add(p, "keys", metavar="KEY", nargs="+")
    _add(p, "--fields")
    _add(p, "--comments", type=count_arg, metavar="N")
    _add(p, "--include-adf", action="store_true")


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
    # TODO(#16): raw query versus structured flag combination rules.


def _key(p: argparse.ArgumentParser) -> None:
    _add(p, "key", metavar="KEY")


def _comments(p: argparse.ArgumentParser) -> None:
    _key(p)
    _add(p, "--limit", type=count_arg, metavar="N")
    _add(p, "--since")
    _add(p, "--newest-first", action="store_true")
    _add(p, "--own", action="store_true")


def _sprints_list(p: argparse.ArgumentParser) -> None:
    _add(p, "--state", choices=["future", "active", "closed"])
    _add(p, "--board")
    _add(p, "--project")
    _add(p, "--limit", type=count_arg, metavar="N")


def _sprints_show(p: argparse.ArgumentParser) -> None:
    _add(p, "id", metavar="ID", type=numeric_id_arg)
    _add(p, "--board")
    _add(p, "--project")


def _sites_resolve(p: argparse.ArgumentParser) -> None:
    _key(p)


def _validate(p: argparse.ArgumentParser) -> None:
    _add(p, "--probe", action="store_true")


def _export(p: argparse.ArgumentParser) -> None:
    _add(p, "--since")
    _add(p, "--after-seq", type=seq_arg, metavar="N")
    _add(p, "--to", metavar="PATH")


def _transition(p: argparse.ArgumentParser) -> None:
    _key(p)
    _add(p, "--to")
    _add(p, "--transition-id", type=numeric_id_arg)
    _add(p, "--resolution")
    _add(p, "--assign")
    _add(p, "--field", action="append")
    _add(p, "--if-status")


def _comment(p: argparse.ArgumentParser) -> None:
    _key(p)
    _add(p, "--body")
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
    _add(p, "--assign")
    _add(p, "--label", action="append")
    _add(p, "--set", action="append")
    _add(p, "--dedupe-key", type=dedupe_key_arg)


def _update(p: argparse.ArgumentParser) -> None:
    _key(p)
    _add(p, "--title")
    _add(p, "--title-file")
    _add(p, "--assign")
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
        CommandSpec(("whoami",), r, "print the authenticated account", _stub(11), _whoami),
        CommandSpec(("get",), r, "read tickets", _stub(15), _get),
        CommandSpec(("search",), r, "search tickets", _stub(17), _search),
        CommandSpec(("comments",), r, "list comments", _stub(18), _comments),
        CommandSpec(("transitions",), r, "list available transitions", _stub(18), _key),
        CommandSpec(("sprints", "list"), r, "list sprints", _stub(18), _sprints_list),
        CommandSpec(("sprints", "show"), r, "show one sprint", _stub(18), _sprints_show),
        CommandSpec(("fields", "list"), r, "list discovered fields", _stub(19), _none),
        CommandSpec(("fields", "check"), r, "check the field mappings", _stub(19), _none),
        CommandSpec(("fields", "refresh"), r, "replace the field cache", _stub(19), _none),
        CommandSpec(("sites",), r, "list configured sites", _stub(11), _none),
        CommandSpec(
            ("sites", "resolve"), r, "show which site a key selects", _stub(11), _sites_resolve
        ),
        CommandSpec(("validate",), r, "validate the configuration", _stub(11), _validate),
        CommandSpec(("export",), r, "print audit records", _stub(36), _export),
        CommandSpec(("transition",), w, "transition a ticket", _stub(28), _transition),
        CommandSpec(("comment",), w, "post a comment", _stub(29), _comment),
        CommandSpec(("create",), w, "create a ticket", _stub(31), _create),
        CommandSpec(("update",), w, "update a ticket", _stub(30), _update),
        CommandSpec(("sprint", "add"), w, "add a ticket to a sprint", _stub(34), _sprint_add),
        CommandSpec(
            ("sprint", "remove"), w, "move a ticket to the backlog", _stub(34), _sprint_remove
        ),
    ]


REGISTRY: dict[tuple[str, ...], CommandSpec] = {s.path: s for s in _specs()}
