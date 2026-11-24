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
    attachmentcmd,
    browsecmd,
    bulk,
    changelogcmd,
    commentcmd,
    commentedit,
    commentscmd,
    createcmd,
    exportcmd,
    fieldscmd,
    getcmd,
    handlers,
    linkcmd,
    resolutioncmd,
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


def _search_filters(p: argparse.ArgumentParser) -> None:
    """The query form, the filters, `--order-by` and `--limit`: shared by `search` and `browse`."""
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


def _search(p: argparse.ArgumentParser) -> None:
    _search_filters(p)
    _add(p, "--fields")
    _add(p, "--count", action="store_true")
    _add(p, "--cursor")


def _browse(p: argparse.ArgumentParser) -> None:
    p.description = (
        "Browse the tickets of a search in a read-only full-screen view. The query and the "
        "filters are those of `search`; --limit is the size of a page."
    )
    _search_filters(p)


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


def _fields_types(p: argparse.ArgumentParser) -> None:
    _add(p, "--project", help="default: the site's only project")
    _add(p, "--type", help="list only this type, with its required fields")


def _sprints_resolve(p: argparse.ArgumentParser) -> None:
    _add(p, "selector", metavar="open|next", choices=["open", "next"])
    _add(p, "--board")
    _add(p, "--project")


def _sites_resolve(p: argparse.ArgumentParser) -> None:
    _add(p, "key", metavar="KEY_OR_URL", nargs="?")
    _add(
        p,
        "--project",
        action="append",
        help="resolve a project key instead (once); the result's key is null",
    )


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


def _comment_id(p: argparse.ArgumentParser) -> None:
    _key(p)
    _add(p, "comment_id", metavar="ID", type=numeric_id_arg, help="the comment's numeric id")


def _comment_edit(p: argparse.ArgumentParser) -> None:
    _comment_id(p)
    _add(
        p,
        "--body",
        help="the new comment text (Markdown); a body starting with '-' needs --body=TEXT "
        "or --body-file",
    )
    _add(p, "--body-file")
    _add(p, "--format", choices=["markdown", "adf"])


def _attachment_add(p: argparse.ArgumentParser) -> None:
    _key(p)
    _add(p, "--file", metavar="PATH", help="the file to attach (a regular file, at most 10 MiB)")


def _attachment_download(p: argparse.ArgumentParser) -> None:
    _key(p)
    _add(p, "attachment_id", metavar="ID", type=numeric_id_arg, help="the attachment's numeric id")
    _add(p, "--to", metavar="PATH", help="the new file to write (an existing path is refused)")


def _resolution_backfill(p: argparse.ArgumentParser) -> None:
    _key(p)
    _add(
        p,
        "--resolution",
        type=resolutioncmd.resolution_arg,
        help="the resolution to set (default: the site's transitions.default_resolution)",
    )


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


def _many(p: argparse.ArgumentParser) -> None:
    """The key list and the cap of a batch (`sprint add-many|remove-many`)."""
    p.description = (
        "Apply the single-ticket command to several tickets of one site under one plan and one "
        "confirmation (at most limits.bulk_max keys, default 50); tickets run one at a time and "
        "the run stops at the first failure."
    )
    _add(p, "keys", metavar="KEY", nargs="*", help="ticket keys or browse URLs (or --keys-file)")
    _add(p, "--keys-file", metavar="PATH", help="one key per line; - reads stdin")
    _add(p, "--max-tickets", type=count_arg, metavar="N", help="lower the cap for this run")
    _add(p, "--board")


def _sprint_add_many(p: argparse.ArgumentParser) -> None:
    _many(p)
    _add(p, "--sprint", type=sprint_selector_arg, metavar="open|next|ID")


def _sprint_remove_many(p: argparse.ArgumentParser) -> None:
    _many(p)


def _link(p: argparse.ArgumentParser) -> None:
    _key(p)
    _add(p, "--type", required=True, type=transitioncmd.name_arg, help="the link type's name")
    _add(
        p,
        "--direction",
        required=True,
        type=linkcmd.direction_arg,
        metavar="inward|outward",
        help="relative to KEY, as in the ticket object's links: outward means KEY is the source "
        "(KEY blocks OTHER), inward that KEY is the target (KEY is blocked by OTHER)",
    )
    _add(p, "--other", required=True, metavar="KEY", help="the ticket at the other end")


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
            ("browse",),
            r,
            "browse a search in a read-only terminal view",
            browsecmd.browse,
            _browse,
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
            ("sprints", "resolve"),
            r,
            "resolve open or next to one sprint",
            sprintscmd.sprints_resolve,
            _sprints_resolve,
            sprintscmd.human_sprints_resolve,
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
            ("fields", "types"),
            r,
            "list the creatable issue types of a project",
            fieldscmd.fields_types,
            _fields_types,
            fieldscmd.human_fields_types,
        ),
        CommandSpec(
            ("sites",), r, "list configured sites", handlers.sites, _none, handlers.human_sites
        ),
        CommandSpec(
            ("sites", "resolve"),
            r,
            "show which site a key or project selects",
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
            ("attachment-download",),
            r,
            "save an attachment of a ticket to a new file",
            attachmentcmd.attachment_download,
            _attachment_download,
            attachmentcmd.human_download,
        ),
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
        CommandSpec(
            ("comment-edit",),
            w,
            "replace the body of a comment ticketctl posted",
            commentedit.comment_edit,
            _comment_edit,
            writeflow.human,
        ),
        CommandSpec(
            ("comment-delete",),
            w,
            "delete a comment ticketctl posted",
            commentedit.comment_delete,
            _comment_id,
            writeflow.human,
        ),
        CommandSpec(
            ("attachment-add",),
            w,
            "attach a file to a ticket",
            attachmentcmd.attachment_add,
            _attachment_add,
            writeflow.human,
        ),
        CommandSpec(
            ("resolution-backfill",),
            w,
            "set the resolution of a done ticket that has none",
            resolutioncmd.resolution_backfill,
            _resolution_backfill,
            writeflow.human,
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
        CommandSpec(
            ("sprint", "add-many"),
            w,
            "add several tickets to a sprint",
            sprintcmd.sprint_add_many,
            _sprint_add_many,
            bulk.human,
        ),
        CommandSpec(
            ("sprint", "remove-many"),
            w,
            "move several tickets to the backlog",
            sprintcmd.sprint_remove_many,
            _sprint_remove_many,
            bulk.human,
        ),
        CommandSpec(
            ("link", "add"), w, "link two tickets", linkcmd.link_add, _link, writeflow.human
        ),
        CommandSpec(
            ("link", "remove"),
            w,
            "remove a link between two tickets",
            linkcmd.link_remove,
            _link,
            writeflow.human,
        ),
    ]


REGISTRY: dict[tuple[str, ...], CommandSpec] = {s.path: s for s in _specs()}
