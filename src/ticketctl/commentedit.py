"""Handlers and write actions of ``comment-edit KEY ID`` and ``comment-delete KEY ID``
(docs/interface.md, ``comment-edit`` and ``comment-delete``).

Both are one step through the write flow engine (``writeflow.py``), built like ``comment``
(``commentcmd.py``): the same preparation of the body (Markdown or ADF, attribution paragraph,
secret and size checks), the same guarded gateway, plan, confirmation, hooks, audit and outcomes.

* **Which comments are safe to change.** The tool acts only on a comment it can tie to itself:
  the author is the authenticated account and the comment carries a valid ``ticketctl.meta``
  property (``v`` 1, an ``actor_kind`` and a ``body_sha256``). Anything else is exit 20
  ``not_ticketctl_comment`` and nothing is sent. The marker is advisory like the rest of the
  attribution (anyone who may edit the comment can forge it); the author test and the provider's
  own permission checks are the real gates. An auto actor also never changes a comment whose
  marker says a human posted it (exit 20 ``human_comment``; a human actor may change both kinds).
  An edit also refuses a comment with a visibility restriction or an internal-only flag
  (``jsdPublic`` false) (exit 20 ``restricted_comment``): the update request carries no
  ``visibility``, and
  whether the provider then keeps, drops or widens the restriction is unverified, so the tool never
  risks making a restricted comment public.
* **Edit** replaces the whole body (attribution paragraph included, as for a new comment) and
  rewrites ``ticketctl.meta`` in the same ``PUT``, so the property always describes the stored
  body. The dedupe key hash of the original post survives (``key_sha256``), so a keyed re-run of
  ``comment`` finds an edited comment and reports ``already_exists`` rather than posting again.
  A new body that equals the stored one is ``unchanged`` (no step, no hook, no audit record).
* **Delete** removes the comment. The comment is gone afterwards, so a re-run is exit 10
  ``not_found`` (a missing comment cannot be told from one that never existed).
* **Plan.** The intent is the comment id (and the new body hash); the fact is the hash of the
  stored body, so a comment changed since the plan is exit 20 ``stale_ticket``. ``changes`` has one
  ``comment`` entry: the stored text as ``from`` and the new text (null for a delete) as ``to``.
  Both commands warn nothing but ``body_near_limit`` (edit).
* The comment is read with ``GET issue/{key}/comment/{id}?expand=properties`` (unverified against
  a real site: that the single-comment read embeds ``properties`` under ``expand``; when it does
  not, the marker is absent and the command refuses, which is the safe failure).
"""

from __future__ import annotations

import argparse
import hashlib
from typing import Any

from . import attribution, commentcmd, selection, writeflow
from .adf_render import render_adf
from .adf_validate import canonical_json
from .comment_object import comment_object
from .errors import TicketctlError
from .flags import Globals
from .plan import Planned, ShownText, Step
from .readutil import object_error
from .runtime import load_runtime
from .sanitize import read_input, segment
from .writeflow import (
    Context,
    PrepareContext,
    Seams,
    StepResult,
    WriteGateway,
    body_info,
    raise_for_write,
)

ITEM_TEMPLATE = "/rest/api/3/issue/{key}/comment/{id}"


def make_seams() -> Seams:
    """The seams of a real run (tests replace this function to fix the attempt id)."""
    return Seams()


def _item_path(key: str, cid: str) -> str:
    return f"/rest/api/3/issue/{segment('ticket_key', key)}/comment/{segment('provider_id', cid)}"


class Found:
    """The stored comment, read and judged: what a change may rely on."""

    def __init__(self, obj: dict[str, Any], doc: dict[str, Any], digest: str, restricted: bool):
        self.obj = obj  # the comment object (``comments`` shape); its body is Markdown
        self.doc = doc  # the stored ADF
        self.digest = digest  # sha256 of the canonical stored ADF
        self.restricted = restricted


def read_comment(ctx: Context, key: str, cid: str) -> Found:
    """Read comment ``cid`` of ``key`` and refuse one that is not safe to change.

    Exit 10 ``not_found`` for a missing or hidden comment, exit 12 ``provider_changed`` for an
    answer that is not a comment of that id, exit 20 ``not_ticketctl_comment`` for a comment this
    account did not post through ticketctl.
    """
    response = ctx.gw.request(
        "GET",
        _item_path(key, cid),
        template=ITEM_TEMPLATE,
        query="expand=properties",
    )
    if not 200 <= response.status < 300:
        raise object_error(response.status, "comment")
    raw = response.data
    marks: set[str] = set()
    obj = comment_object(raw, ctx.me.account_id, marks) if isinstance(raw, dict) else None
    if obj is None or obj["id"] != cid or not isinstance(raw, dict):
        raise TicketctlError("provider_changed", "the provider answered no usable comment")
    doc = raw.get("body")
    if not isinstance(doc, dict):
        raise TicketctlError("provider_changed", "the comment has no rich-text body")
    ours = (
        obj["own"]
        and obj["authored_by_ticketctl"]
        and obj["actor_kind"] is not None
        and obj["body_sha256"] is not None
    )
    if not ours:
        raise TicketctlError(
            "not_ticketctl_comment",
            "only a comment this account posted through ticketctl can be changed",
            reason="not_ticketctl_comment",
        )
    if obj["actor_kind"] == "human" and attribution.actor_kind(ctx.actor) == "auto":
        raise TicketctlError(
            "human_comment",
            "an auto actor cannot change a comment a human posted through ticketctl",
            reason="human_comment",
        )
    try:
        digest = hashlib.sha256(canonical_json(doc).encode("utf-8")).hexdigest()
    except (ValueError, TypeError, RecursionError):
        raise TicketctlError("provider_changed", "the comment body cannot be read") from None
    restricted = raw.get("visibility") not in (None, {}) or raw.get("jsdPublic") is False
    return Found(obj, doc, digest, restricted)


class _Base:
    """Shared parts of both actions."""

    name = ""
    dedupe_key: str | None = None
    yes_needs_dedupe_key = False
    dedupe_lookup = True

    def __init__(self, key: str, comment_id: str) -> None:
        self.key = key
        self.comment_id = comment_id

    def check(self, ctx: Context, planned: Planned) -> None:
        return None

    def _hook(self, ctx: Context) -> dict[str, Any]:
        fields = commentcmd.read_ticket(ctx, self.key)
        raw_status = fields.get("status")
        status: dict[str, Any] = raw_status if isinstance(raw_status, dict) else {}
        raw_cat = status.get("statusCategory")
        cat = raw_cat.get("key") if isinstance(raw_cat, dict) else None
        category = cat if cat in commentcmd.CATEGORIES else None
        facts = commentcmd.hook_facts(ctx, fields, status, category)
        facts["comment_id"] = self.comment_id
        return facts


class CommentEditAction(_Base):
    """``comment-edit KEY ID`` as a :class:`~ticketctl.writeflow.WriteAction`."""

    name = "comment_edit"

    def __init__(self, key: str, comment_id: str, body: str, *, fmt: str = "markdown") -> None:
        super().__init__(key, comment_id)
        self.body = body
        self.fmt: attribution.Format = "adf" if fmt == "adf" else "markdown"
        self.rich: attribution.RichText | None = None
        self.body_chars = 0

    def prepare(self, pc: PrepareContext) -> None:
        self.body_chars = pc.body_chars
        self.rich = pc.prepare_rich(self.body, kind="comment", fmt=self.fmt)

    def reduce(self, ctx: Context, *, fresh: bool) -> Planned:
        rich = self.rich
        assert rich is not None and rich.doc is not None
        info = body_info(rich)
        found = read_comment(ctx, self.key, self.comment_id)
        if found.restricted:
            raise TicketctlError(
                "restricted_comment",
                "a comment with a visibility restriction cannot be edited through ticketctl",
                reason="restricted_comment",
            )
        new_text = render_adf(rich.doc).text
        unchanged = found.digest == rich.sha256
        steps = [] if unchanged else [self._step(ctx, rich, info, found)]
        warnings: list[str] = []
        if (
            sum(len(t) for t in commentcmd._texts(rich.doc))
            > commentcmd.NEAR_LIMIT * self.body_chars
        ):
            warnings.append("body_near_limit")
        return Planned(
            steps=steps,
            intent={"comment_id": self.comment_id, "bodies": [info["sha256"]]},
            facts={"comment_sha256": found.digest},
            changes=[{"name": "comment", "from": found.obj["body"], "to": new_text}],
            warnings=warnings,
            key=self.key,
            bodies=[info],
            comment_id=self.comment_id,
            shown=[ShownText("comment", new_text, "body", info["sha256"])],
            hook=self._hook(ctx),
        )

    def _step(
        self, ctx: Context, rich: attribution.RichText, info: dict[str, Any], found: Found
    ) -> Step:
        meta = attribution.comment_meta(ctx.actor, rich)
        old_key = found.obj["dedupe_key_sha256"]
        if old_key is not None:
            meta["key_sha256"] = old_key  # the post's dedupe key stays findable
        key, cid = self.key, self.comment_id

        def send(gw: WriteGateway) -> StepResult:
            res = gw.request(
                "PUT",
                _item_path(key, cid),
                template=ITEM_TEMPLATE,
                body={
                    "body": rich.doc,
                    "properties": [{"key": attribution.META_PROPERTY, "value": meta}],
                },
            )
            raise_for_write(res, object_path=True)
            return StepResult([cid], comment_id=cid)

        return Step(
            "edit_comment",
            send,
            {"comment_id": cid, "body_sha256": info["sha256"], "body_bytes": info["bytes"]},
        )


class CommentDeleteAction(_Base):
    """``comment-delete KEY ID`` as a :class:`~ticketctl.writeflow.WriteAction`."""

    name = "comment_delete"

    def prepare(self, pc: PrepareContext) -> None:
        return None  # nothing is sent but the id: no body to prepare

    def reduce(self, ctx: Context, *, fresh: bool) -> Planned:
        found = read_comment(ctx, self.key, self.comment_id)
        key, cid = self.key, self.comment_id

        def send(gw: WriteGateway) -> StepResult:
            res = gw.request("DELETE", _item_path(key, cid), template=ITEM_TEMPLATE)
            raise_for_write(res, object_path=True)
            return StepResult([cid], comment_id=cid)

        return Planned(
            steps=[Step("delete_comment", send, {"comment_id": cid})],
            intent={"comment_id": cid},
            facts={"comment_sha256": found.digest},
            changes=[{"name": "comment", "from": found.obj["body"], "to": None}],
            key=key,
            comment_id=cid,
            hook=self._hook(ctx),
        )


def _target(g: Globals, a: argparse.Namespace) -> tuple[Any, str, str]:
    """The runtime, the selected site and the canonical ticket key."""
    rt = load_runtime(g)
    chosen = selection.select_site(rt.config, site_flag=g.site, refs=[a.key])
    assert chosen.key is not None
    return rt, chosen.site, chosen.key


def comment_edit(g: Globals, a: argparse.Namespace) -> dict[str, Any]:
    rt, site, key = _target(g, a)
    if a.body_file is not None:
        text = read_input(a.body_file, kind="body", body_chars=rt.config.limits.body_chars)
    else:
        text = a.body
    action = CommentEditAction(key, a.comment_id, text, fmt=a.format or "markdown")
    return writeflow.execute(rt, site, action, make_seams())


def comment_delete(g: Globals, a: argparse.Namespace) -> dict[str, Any]:
    rt, site, key = _target(g, a)
    return writeflow.execute(rt, site, CommentDeleteAction(key, a.comment_id), make_seams())
