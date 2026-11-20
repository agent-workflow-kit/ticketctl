"""The comment object of a provider comment (docs/interface.md, Comment object).

Pure logic, no I/O: :func:`comment_object` turns one provider comment (read with
``expand=properties``) into the object of ``schemas/defs.json#comment`` and :func:`comment_list`
turns a comment page into the ``{items, total, truncated}`` part of ``get --comments N``. ``get``
(``getcmd``) owns the provider call; the ``comments`` command, TODO(#18), reuses these two
functions and adds its own paging, ``--since``, ``--own`` and ordering.

Rules this module settles where the interface leaves room (all conservative):

* ``own`` is true only when the author's account id equals the resolved ``me`` account id, an
  exact, case-sensitive match. An author with no usable id is never own.
* The ``ticketctl.meta`` property is read for an own comment only. A forged property on another
  account's comment is ignored: ``authored_by_ticketctl`` false, ``actor_kind`` and both hashes
  null. On an own comment the property being present at all makes ``authored_by_ticketctl`` true
  (a consumer that distrusts ticketctl's own comments then keeps distrusting a damaged property);
  ``actor_kind`` and the hashes are read from it only when they are valid (``actor_kind`` is
  ``auto`` or ``human``, a hash is 64 lower-case hex digits), else null.
* A comment whose ``created`` or ``updated`` cannot be read, or whose id is outside its grammar, is
  dropped and named in the ticket's ``incomplete`` (``comments`` or ``comments.invalid_id``): an
  ``updated`` guessed equal to ``created`` would tell a consumer the text was never edited.
* A body that is not rich text or a string is the empty string and names ``comments``. Rich text is
  rendered like a description: the renderer's budgets and the body cap make ``truncated`` true.

Unverified against a real site: that a comment embeds its ``properties`` as a list of
``{key, value}`` objects under ``expand=properties``, and that ``total`` counts every comment.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from .adf_render import render_adf
from .attribution import META_PROPERTY, META_VERSION
from .sanitize import CAPS, GRAMMARS, cap_text, sanitize_field, scrub
from .timestamps import normalize_ts

BODY_CAP = CAPS["body"]
NAME_CAP = CAPS["name"]
UNKNOWN_USER = "unknown user"
MAX_COMMENTS = 50
_HASH = re.compile(r"[0-9a-f]{64}")
_KINDS = ("auto", "human")


@dataclass
class CommentPage:
    """``comments`` (the printed part) and the ``incomplete`` names the page earned."""

    value: dict[str, Any]
    incomplete: set[str] = field(default_factory=set)


def _author(raw: object, marks: set[str]) -> dict[str, Any]:
    if not isinstance(raw, dict):
        marks.add("comments")
        return {"account_id": None, "display_name": UNKNOWN_USER}
    account = raw.get("accountId")  # never emailAddress
    if not GRAMMARS["account_id"].matches(account):
        if account is not None:
            marks.add("comments.invalid_id")
        return {"account_id": None, "display_name": UNKNOWN_USER}
    name = raw.get("displayName")
    shown = UNKNOWN_USER
    if isinstance(name, str) and name:
        shown, cut = sanitize_field(name, cap=NAME_CAP)
        if cut:
            marks.add("comments.truncated")
    return {"account_id": account, "display_name": shown}


def _body(raw: object, marks: set[str]) -> tuple[str, bool]:
    if isinstance(raw, dict):
        rendered = render_adf(raw, "comments")
        text, cut = cap_text(scrub(rendered.text), BODY_CAP)
        return text, cut or bool(rendered.incomplete)
    if isinstance(raw, str):
        return sanitize_field(raw, cap=BODY_CAP, body=True)
    marks.add("comments")
    return "", False


def _hash(value: object) -> str | None:
    return value if isinstance(value, str) and _HASH.fullmatch(value) else None


def _meta(properties: object) -> tuple[bool, dict[str, Any]]:
    """(the property is present, the valid parts of its value)."""
    if not isinstance(properties, list):
        return False, {}
    for entry in properties:
        if isinstance(entry, dict) and entry.get("key") == META_PROPERTY:
            value = entry.get("value")
            if not isinstance(value, dict) or value.get("v") != META_VERSION:
                return True, {}
            if isinstance(value.get("v"), bool):
                return True, {}
            kind = value.get("actor_kind")
            return True, {
                "actor_kind": kind if kind in _KINDS else None,
                "body_sha256": _hash(value.get("body_sha256")),
                "dedupe_key_sha256": _hash(value.get("key_sha256")),
            }
    return False, {}


def comment_object(
    raw: object, me_account_id: str | None, marks: set[str]
) -> dict[str, Any] | None:
    """One comment object, or None for a comment that cannot be kept (named in ``marks``)."""
    if not isinstance(raw, dict):
        marks.add("comments")
        return None
    cid = raw.get("id")
    if not GRAMMARS["provider_id"].matches(cid):
        marks.add("comments.invalid_id" if cid is not None else "comments")
        return None
    created = normalize_ts(raw.get("created"))
    updated = normalize_ts(raw.get("updated"))
    if created is None or updated is None:
        marks.add("comments")
        return None
    author = _author(raw.get("author"), marks)
    own = me_account_id is not None and author["account_id"] == me_account_id
    present, meta = _meta(raw.get("properties")) if own else (False, {})
    body, cut = _body(raw.get("body"), marks)
    if cut:
        marks.add("comments.truncated")
    return {
        "id": cid,
        "author": author,
        "own": own,
        "created": created,
        "updated": updated,
        "body": body,
        "truncated": cut,
        "dedupe_key_sha256": meta.get("dedupe_key_sha256"),
        "body_sha256": meta.get("body_sha256"),
        "authored_by_ticketctl": present,
        "actor_kind": meta.get("actor_kind"),
    }


def comment_list(page: object, limit: int, me_account_id: str | None) -> CommentPage:
    """``{items, total, truncated}`` for a page read newest first with ``maxResults=limit``.

    ``items`` are the newest ``limit`` comments, oldest first. ``truncated`` is true when the ticket
    has more comments than are listed (a count the provider did not give counts as unknown: the
    page is then truncated and ``comments`` is named incomplete).
    """
    marks: set[str] = set()
    raws: list[object] = []
    total: int | None = None
    if isinstance(page, dict):
        listed = page.get("comments")
        if isinstance(listed, list):
            raws = listed[:limit]
        else:
            marks.add("comments")
        count = page.get("total")
        if isinstance(count, int) and not isinstance(count, bool) and count >= 0:
            total = count
    else:
        marks.add("comments")
    items = [c for c in (comment_object(r, me_account_id, marks) for r in raws) if c is not None]
    items.reverse()
    if total is None:
        marks.add("comments")
        total = len(raws)
    total = max(total, len(raws))
    return CommentPage(
        {"items": items, "total": total, "truncated": total > len(raws)},
        marks,
    )


__all__ = ["MAX_COMMENTS", "CommentPage", "comment_list", "comment_object"]
