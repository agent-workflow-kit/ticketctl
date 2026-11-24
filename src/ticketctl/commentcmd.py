"""Handler and write action of ``comment KEY`` (docs/interface.md, ``comment KEY``).

``comment`` is one ``add_comment`` step through the write flow engine (``writeflow.py``):

* **prepare** converts the body (Markdown, or ADF from ``--body-file`` with ``--format adf``),
  adds the attribution paragraph and refuses a secret, an empty body and a body over
  ``limits.body_chars`` (``body_too_large``, nothing is sent). A body never leaves as wiki markup:
  the converter only builds ADF nodes, and the guarded gateway validates the document again.
* **reduce** reads the ticket once (``GET issue``) for the facts the plan and the hooks need: the
  plan warns ``closed_ticket`` when the status category is ``done`` (a closed ticket still takes
  comments), ``body_near_limit`` above 90% of ``limits.body_chars``, and ``Planned.hook`` carries
  the hook facts (project, ticket, current status). The plan hash has no facts: the intent is the
  body hash and the dedupe key, so a ticket that changed meanwhile never makes a comment stale.
* The step posts ``{body, properties: [ticketctl.meta]}`` as the authenticated account; the
  ``ticketctl.meta`` value is built by ``attribution.comment_meta`` and rides in the same request
  as the comment, so the comment and its property cannot part.

Dedupe plugs in at :meth:`CommentAction.find_existing`: it returns the id of a comment of the
fresh account that already carries the dedupe key and the same body hash (then the plan has no
step and the result is ``already_posted``). The same key with another body is exit 20
``already_exists`` (``details.existing_id``), and a key that is not found while the scan cannot
prove absence in two scans is exit 12 ``lookup_incomplete`` (not retryable). ``reduce`` runs it
lock-free for the plan and again under the bucket lock (``dedupe.bucket_lock``, scope = the ticket
key) as the fresh check; a comment list is read-your-writes, so no lag guard
(``dedupe.guarded_lookup``) is needed.
"""

from __future__ import annotations

import argparse
import hashlib
from typing import Any

from . import attribution, commentscmd, selection, writeflow
from .adf_render import render_adf
from .errors import TicketctlError
from .flags import Globals
from .plan import Planned, ShownText, Step
from .readutil import object_error
from .runtime import load_runtime
from .sanitize import GRAMMARS, query, read_input, segment
from .writeflow import (
    Context,
    PrepareContext,
    Seams,
    StepResult,
    WriteGateway,
    body_info,
    raise_for_write,
)

COMMENT_TEMPLATE = "/rest/api/3/issue/{key}/comment"
ISSUE_TEMPLATE = "/rest/api/3/issue/{key}"
NEAR_LIMIT = 0.9
CATEGORIES = ("new", "indeterminate", "done")


def make_seams() -> Seams:
    """The seams of a real run (tests replace this function to fix the attempt id)."""
    return Seams()


def _name(value: object) -> str | None:
    return value if isinstance(value, str) and value else None


def _obj(value: object) -> dict[str, Any]:
    """A provider object, or an empty one when the provider sent anything else (no fact then)."""
    return value if isinstance(value, dict) else {}


class CommentAction:
    """``comment KEY`` as a :class:`~ticketctl.writeflow.WriteAction`."""

    name = "comment"
    yes_needs_dedupe_key = True
    dedupe_lookup = True  # a keyed re-run finds the comment: outcome_unknown stays retryable

    def __init__(
        self,
        key: str,
        body: str,
        *,
        fmt: str = "markdown",
        dedupe_key: str | None = None,
    ) -> None:
        self.key = key
        self.body = body
        self.fmt: attribution.Format = "adf" if fmt == "adf" else "markdown"
        self.dedupe_key = dedupe_key
        self.rich: attribution.RichText | None = None
        self.body_chars = 0

    # -- step 1 ------------------------------------------------------------------------------
    def prepare(self, pc: PrepareContext) -> None:
        self.body_chars = pc.body_chars
        self.rich = pc.prepare_rich(self.body, kind="comment", fmt=self.fmt)
        if self.dedupe_key is not None:
            GRAMMARS["dedupe_key"].check(self.dedupe_key)
            pc.check_secrets(self.dedupe_key)

    # -- step 3 ------------------------------------------------------------------------------
    def find_existing(self, ctx: Context, rich: attribution.RichText) -> str | None:
        """The id of a comment of this account that already holds the dedupe key, or None.

        Reads the comments newest first (at most 1000, ``commentscmd.scan_comments``) and keeps the
        own comments whose ``ticketctl.meta`` carries ``key_sha256`` of this key (the reader
        accepts only ``v == 1`` and a 64-hex hash, so a malformed or foreign property is no
        marker). A hit with the same body hash is returned (the newest such one); a hit with
        another body hash is exit 20 ``already_exists``. With no hit, a scan that cannot prove
        absence (bounds reached, a comment dropped, a property that could not be read) is scanned
        once more and then is exit 12 ``lookup_incomplete``; it is not retryable. A comment list
        that cannot be read at all is the provider error (``scan_comments`` raises it).
        """
        assert self.dedupe_key is not None
        wanted = hashlib.sha256(self.dedupe_key.encode("utf-8")).hexdigest()
        for _ in range(2):  # a concurrent comment can repeat an id between pages: rescan once
            scan = commentscmd.scan_comments(
                ctx.gw,
                self.key,
                ctx.me.account_id,
                limit=commentscmd.MAX_SCAN,
                newest_first=True,
                since=None,
                own_only=True,
            )
            hits = [c for c in scan.items if c["dedupe_key_sha256"] == wanted]
            for hit in hits:  # scan.items is newest first
                if hit["body_sha256"] == rich.sha256:
                    return str(hit["id"])
            if hits:
                raise TicketctlError(
                    "already_exists",
                    "a comment with this dedupe key and another body exists; "
                    "use a new key or keep the existing comment",
                    details={"existing_id": str(hits[0]["id"])},
                )
            if not (scan.truncated or "comments" in scan.marks):
                return None
        raise TicketctlError(
            "lookup_incomplete",
            "the ticket's comments could not be read completely, so the dedupe key cannot be "
            "ruled out; nothing was posted (only a human may post without a key)",
        )

    def reduce(self, ctx: Context, *, fresh: bool) -> Planned:
        rich = self.rich
        assert rich is not None and rich.doc is not None
        info = body_info(rich)
        fields = self._read_ticket(ctx)
        raw_status = fields.get("status")
        status: dict[str, Any] = raw_status if isinstance(raw_status, dict) else {}
        raw_cat = status.get("statusCategory")
        category_raw = raw_cat.get("key") if isinstance(raw_cat, dict) else None
        category = category_raw if category_raw in CATEGORIES else None

        existing = self.find_existing(ctx, rich) if self.dedupe_key is not None else None
        steps = [] if existing is not None else [self._add_step(ctx, rich, info)]
        warnings: list[str] = []
        if category == "done":
            warnings.append("closed_ticket")
        if sum(len(t) for t in _texts(rich.doc)) > NEAR_LIMIT * self.body_chars:
            warnings.append("body_near_limit")
        return Planned(
            steps=steps,
            intent={"bodies": [info["sha256"]], "dedupe_key": self.dedupe_key},
            facts={},
            changes=[],
            warnings=warnings,
            key=self.key,
            dedupe_key=self.dedupe_key,
            dedupe_match=existing,
            bodies=[info],
            already_posted=existing is not None,
            comment_id=existing,
            shown=[ShownText("comment", render_adf(rich.doc).text, "body", info["sha256"])],
            hook=self._hook_facts(ctx, fields, status, category),
        )

    def check(self, ctx: Context, planned: Planned) -> None:
        return None  # no precondition flag and no provider-context check for a comment

    # -- helpers -----------------------------------------------------------------------------
    def _read_ticket(self, ctx: Context) -> dict[str, Any]:
        return read_ticket(ctx, self.key)

    def _hook_facts(
        self, ctx: Context, fields: dict[str, Any], status: dict[str, Any], category: str | None
    ) -> dict[str, Any]:
        return hook_facts(ctx, fields, status, category)

    def _add_step(self, ctx: Context, rich: attribution.RichText, info: dict[str, Any]) -> Step:
        meta = attribution.comment_meta(ctx.actor, rich, dedupe_key=self.dedupe_key)
        key = self.key

        def send(gw: WriteGateway) -> StepResult:
            res = gw.request(
                "POST",
                f"/rest/api/3/issue/{segment('ticket_key', key)}/comment",
                template=COMMENT_TEMPLATE,
                body={
                    "body": rich.doc,
                    "properties": [{"key": attribution.META_PROPERTY, "value": meta}],
                },
            )
            raise_for_write(res, object_path=True)
            data = res.data
            cid = data.get("id") if isinstance(data, dict) else None
            ids = [str(cid)] if isinstance(cid, str | int) and not isinstance(cid, bool) else []
            return StepResult(ids, comment_id=ids[0] if ids else None)

        return Step(
            "add_comment", send, {"body_sha256": info["sha256"], "body_bytes": info["bytes"]}
        )


def read_ticket(ctx: Context, key: str) -> dict[str, Any]:
    """The ticket's facts for the plan and the hooks (also used by comment edit and delete)."""
    response = ctx.gw.request(
        "GET",
        f"/rest/api/3/issue/{segment('ticket_key', key)}",
        template=ISSUE_TEMPLATE,
        query=query({"fields": "status,issuetype,project,priority,assignee"}),
    )
    if not 200 <= response.status < 300:
        raise object_error(response.status, "ticket")
    data = response.data
    fields = data.get("fields") if isinstance(data, dict) else None
    if not isinstance(fields, dict):
        raise TicketctlError("provider_changed", "the provider answered no ticket fields")
    return fields


def hook_facts(
    ctx: Context, fields: dict[str, Any], status: dict[str, Any], category: str | None
) -> dict[str, Any]:
    project = _obj(fields.get("project")).get("key")
    itype = _obj(fields.get("issuetype"))
    assignee = _obj(fields.get("assignee")).get("accountId")
    priority = _obj(fields.get("priority")).get("name")
    facts: dict[str, Any] = {
        "ticket": {
            "type": _name(itype.get("name")),
            "subtask": itype.get("subtask") is True,
            "status": _name(status.get("name")),
            "priority": _name(priority),
            "assignee_account_id": assignee if GRAMMARS["account_id"].matches(assignee) else None,
            "assignee_is_me": assignee == ctx.me.account_id,
        },
        "current_status": {"name": _name(status.get("name")), "category": category},
    }
    if GRAMMARS["project_key"].matches(project):
        facts["project"] = project
    return facts


def _texts(doc: object) -> list[str]:
    """The visible text nodes of a document (link targets excluded: ``document_texts`` has them)."""
    out: list[str] = []
    stack: list[object] = [doc]
    while stack:
        node = stack.pop()
        if isinstance(node, dict):
            text = node.get("text")
            if isinstance(text, str):
                out.append(text)
            content = node.get("content")
            if isinstance(content, list):
                stack.extend(content)
    return out


def comment(g: Globals, a: argparse.Namespace) -> dict[str, Any]:
    rt = load_runtime(g)
    chosen = selection.select_site(rt.config, site_flag=g.site, refs=[a.key])
    assert chosen.key is not None
    body_chars = rt.config.limits.body_chars
    if a.body_file is not None:
        text = read_input(a.body_file, kind="body", body_chars=body_chars)
    else:
        text = a.body
    action = CommentAction(chosen.key, text, fmt=a.format or "markdown", dedupe_key=a.dedupe_key)
    return writeflow.execute(rt, chosen.site, action, make_seams())
