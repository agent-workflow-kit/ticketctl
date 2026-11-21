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

Dedupe (#33) plugs in at :meth:`CommentAction.find_existing`: it returns the id of a comment that
already carries the dedupe key (then the plan has no step and the result is ``already_posted``).
It returns ``None`` here: nothing is looked up until #33.
"""

from __future__ import annotations

import argparse
from typing import Any

from . import attribution, selection, writeflow
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


class CommentAction:
    """``comment KEY`` as a :class:`~ticketctl.writeflow.WriteAction`."""

    name = "comment"
    yes_needs_dedupe_key = True

    def __init__(
        self,
        key: str,
        body: str,
        *,
        fmt: attribution.Format = "markdown",
        dedupe_key: str | None = None,
    ) -> None:
        self.key = key
        self.body = body
        self.fmt = fmt
        self.dedupe_key = dedupe_key
        self.rich: attribution.RichText | None = None
        self.body_chars = 0

    # -- step 1 ------------------------------------------------------------------------------
    def prepare(self, pc: PrepareContext) -> None:
        self.body_chars = pc.body_chars
        self.rich = pc.prepare_rich(self.body, kind="comment", fmt=self.fmt)
        if self.dedupe_key is not None:
            pc.check_secrets(self.dedupe_key)

    # -- step 3 ------------------------------------------------------------------------------
    def find_existing(self, ctx: Context, rich: attribution.RichText) -> str | None:
        """The id of a comment of this account that already holds the dedupe key, or None.

        TODO(#33): the lookup by ``ticketctl.meta.key_sha256`` through ``commentscmd.scan_comments``
        (a truncated scan cannot prove absence).
        """
        return None

    def reduce(self, ctx: Context, *, fresh: bool) -> Planned:
        rich = self.rich
        assert rich is not None and rich.doc is not None
        info = body_info(rich)
        fields = self._read_ticket(ctx)
        status = fields.get("status") if isinstance(fields.get("status"), dict) else {}
        category_raw = (status.get("statusCategory") or {}).get("key")
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
        response = ctx.gw.request(
            "GET",
            f"/rest/api/3/issue/{segment('ticket_key', self.key)}",
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

    def _hook_facts(
        self, ctx: Context, fields: dict[str, Any], status: dict[str, Any], category: str | None
    ) -> dict[str, Any]:
        project = (fields.get("project") or {}).get("key")
        itype = fields.get("issuetype") or {}
        assignee = (fields.get("assignee") or {}).get("accountId")
        priority = (fields.get("priority") or {}).get("name")
        facts: dict[str, Any] = {
            "ticket": {
                "type": _name(itype.get("name")),
                "subtask": itype.get("subtask") is True,
                "status": _name(status.get("name")),
                "priority": _name(priority),
                "assignee_account_id": assignee
                if GRAMMARS["account_id"].matches(assignee)
                else None,
                "assignee_is_me": assignee == ctx.me.account_id,
            },
            "current_status": {"name": _name(status.get("name")), "category": category},
        }
        if GRAMMARS["project_key"].matches(project):
            facts["project"] = project
        return facts

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
