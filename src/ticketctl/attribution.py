"""The attribution paragraph, the comment property and the actor rules (docs/interface.md,
Attribution and Human actions).

Public API:
    prepare_rich_text(body, actor=..., kind=..., template=..., enabled=..., fmt=..., body_chars=...)
                                   Markdown (or ``--format adf`` text) -> the final ADF and its hash
    comment_meta(actor, rich, dedupe_key=None)
                                   the ``ticketctl.meta`` comment property value
    default_actor(flag, me_name=..., stderr_tty=..., tty_openable=...)
                                   the actor string and its ``actor_source``
    attribution_text(template)     the sanitized text of the first paragraph
    META_PROPERTY                  the comment property key

``kind`` is the rich-text path: ``comment`` (a comment body), ``description`` (a created ticket's
description) or ``field`` (a rich-text ``--set`` or ``--field`` value, which includes a transition
``Comment`` field). The rules, in the order they run:

1. An ``auto:`` actor on a ``field`` path is exit 22 ``invalid_value``: its only rich-text paths
   are a comment body and a created description.
2. The body is converted (or, for ``fmt="adf"``, parsed and validated) with the attribution size
   reserved (the template's text length and 2 nodes), so ``empty_body`` is judged on the body
   alone, before the paragraph exists, and the size and node limits count the paragraph.
3. The attribution paragraph is inserted as the first block, then ``validate_adf()`` runs on the
   final document with no reserve, and the body hash is taken over that document.

The paragraph is mandatory for an ``auto:`` actor on ``comment`` and ``description`` whatever
``attribution.enabled`` says; a ``human:`` actor gets it when ``enabled``, and never on ``field``.
A ``description`` with no body is the paragraph alone for ``auto:`` and no description (``doc`` is
``None``) for ``human:``. The template is config text: it is sanitized (single line, control and
bidirectional characters become ``<U+XXXX>`` markers) and becomes one plain text node with no
marks, so it can neither carry a mention or link nor escape the ADF allowlist. Markdown in a
template stays literal.

``ticketctl.meta`` is written in the create request of the comment itself (the write flow owns
that); a comment through a transition ``Comment`` field is a ``field`` path and has no property.

TODO(#23): the write flow calls ``prepare_rich_text`` in its prepare step, sends
``comment_meta(actor, rich)`` (the same ``RichText``) in the create request, and hands the same
``RichText.sha256`` to the plan, hooks and audit.
TODO(#29): ``comment`` calls this with ``kind="comment"`` and the dedupe lookup reads the property.
TODO(#30): ``create`` calls this with ``kind="description"``; ``update`` and ``transition`` call it
with ``kind="field"`` for each rich-text value.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from typing import Any, Literal

from ticketctl.adf_from_markdown import markdown_to_adf
from ticketctl.adf_validate import DEFAULT_BODY_CHARS, body_sha256, parse_adf_json, validate_adf
from ticketctl.sanitize import GRAMMARS, InputError, sanitize_line

META_PROPERTY = "ticketctl.meta"
META_VERSION = 1
ATTRIBUTION_NODES = 2  # a paragraph and its one text node

Kind = Literal["comment", "description", "field"]
Format = Literal["markdown", "adf"]
Node = dict[str, Any]

_MARKER = re.compile(r"<U\+[0-9A-F]{4,6}>")
_KINDS = ("comment", "description", "field")
_FORMATS = ("markdown", "adf")


@dataclass(frozen=True)
class RichText:
    """The result of preparing one rich-text value.

    ``doc`` is the final ADF to send (``None`` only for a human create with no body), ``sha256``
    its body hash (``None`` with ``doc``) and ``attributed`` whether the paragraph was added.
    """

    doc: Node | None
    sha256: str | None
    attributed: bool


def attribution_text(template: str) -> str:
    """The sanitized, stripped template. A template with no visible text is a config error."""
    text = sanitize_line(template).strip()
    # A template of only sanitizer markers (zero-width, control characters) shows nothing.
    if not _MARKER.sub("", text).strip():
        raise InputError(
            2, "invalid_config", "config_invalid", "attribution.template has no visible text"
        )
    return text


def actor_kind(actor: str) -> Literal["auto", "human"]:
    """``auto`` or ``human`` from an actor string; anything outside the grammar is a ValueError."""
    if not isinstance(actor, str) or not GRAMMARS["actor"].pattern.fullmatch(actor):
        raise ValueError("actor must be human:<name> or auto:<name>")
    return "auto" if actor.startswith("auto:") else "human"


def prepare_rich_text(
    body: str | None,
    *,
    actor: str,
    kind: Kind,
    template: str,
    enabled: bool,
    fmt: Format = "markdown",
    body_chars: int = DEFAULT_BODY_CHARS,
) -> RichText:
    """Convert ``body`` and apply the attribution rule; see the module docstring.

    Raises ``InputError`` 22 (``invalid_value`` for an ``auto:`` ``field``; ``empty_body``,
    ``body_too_large``, ``node_not_allowed`` and the converter's refusals otherwise) and
    ``InputError`` 2 for an unusable template. A bad ``kind``, ``fmt`` or ``actor`` is a
    ``ValueError`` and a non-str body a ``TypeError`` (caller bugs).
    """
    if kind not in _KINDS:
        raise ValueError(f"unknown rich-text kind: {kind!r}")
    if fmt not in _FORMATS:
        raise ValueError(f"unknown body format: {fmt!r}")
    if body is not None and not isinstance(body, str):
        raise TypeError(f"body must be str or None, got {type(body).__name__}")
    auto = actor_kind(actor) == "auto"
    if auto and kind == "field":
        raise InputError(
            22,
            "invalid_request",
            "invalid_value",
            "rich text by an automatic actor carries no attribution",
        )
    attribute = kind != "field" and (auto or enabled)
    text = attribution_text(template) if attribute else ""
    reserve = len(text)
    reserve_nodes = ATTRIBUTION_NODES if attribute else 0

    if body is None:
        if kind == "description":
            if not auto:
                return RichText(None, None, False)
            doc: Node = {"type": "doc", "version": 1, "content": []}
        else:
            raise InputError(22, "invalid_request", "empty_body", "the body has no text")
    elif fmt == "adf":
        doc = parse_adf_json(
            body, body_chars=body_chars, reserve=reserve, reserve_nodes=reserve_nodes
        )
    else:
        doc = markdown_to_adf(
            body, body_chars=body_chars, reserve=reserve, reserve_nodes=reserve_nodes
        )

    if attribute:
        paragraph: Node = {"type": "paragraph", "content": [{"type": "text", "text": text}]}
        doc = {**doc, "content": [paragraph, *doc["content"]]}
    # The final document is the one that is hashed and sent; no reserve here.
    validate_adf(doc, body_chars=body_chars)
    return RichText(doc, body_sha256(doc, body_chars=body_chars), attribute)


def comment_meta(
    actor: str,
    rich: RichText,
    *,
    dedupe_key: str | None = None,
) -> dict[str, Any]:
    """The value of the ``ticketctl.meta`` property for the comment ``rich`` was prepared for.

    ``rich`` is the result of ``prepare_rich_text`` for that comment; its ``sha256`` is the body
    hash (no re-validation, so no size limit is applied here). A ``ValueError`` (caller bug) if
    ``rich.doc`` is ``None`` or if an ``auto`` actor's text lacks the attribution paragraph.
    ``key_sha256`` (the sha256 of the key string alone, UTF-8) is present only for a dedupe key,
    which must match the dedupe-key grammar (else ``InputError`` exit 2, code ``usage``, reason
    ``invalid_argument``).
    """
    kind = actor_kind(actor)
    if rich.doc is None or rich.sha256 is None:
        raise ValueError("comment_meta needs a prepared document")
    if kind == "auto" and not rich.attributed:
        raise ValueError("an auto actor's comment must carry the attribution paragraph")
    meta: dict[str, Any] = {"v": META_VERSION, "actor_kind": kind, "body_sha256": rich.sha256}
    if dedupe_key is not None:
        grammar = GRAMMARS["dedupe_key"]
        if not isinstance(dedupe_key, str) or not grammar.pattern.fullmatch(dedupe_key):
            raise InputError(
                grammar.exit_code, grammar.code, grammar.reason, "the dedupe key is not valid"
            )
        meta["key_sha256"] = hashlib.sha256(dedupe_key.encode("utf-8")).hexdigest()
    return meta


def default_actor(
    flag: str | None,
    *,
    me_name: str | None,
    stderr_tty: bool,
    tty_openable: bool,
) -> tuple[str, Literal["flag", "tty", "default"]]:
    """The actor and its ``actor_source``.

    An explicit ``--actor`` wins (``flag``; a string outside the grammar is a ValueError, the
    flag parser has already refused it). Without one the actor is ``human:<me.name>`` (``tty``)
    only when stderr is a TTY and ``/dev/tty`` can be opened and ``me.name`` fits the actor
    grammar, else ``auto:unknown`` (``default``): the conservative reading is that an unusable
    name never yields a human claim.
    """
    if flag is not None:
        actor_kind(flag)
        return flag, "flag"
    # Defensive: config normalization owns me.name; never trust it to be a usable string.
    if stderr_tty and tty_openable and isinstance(me_name, str) and me_name:
        candidate = f"human:{me_name}"
        if GRAMMARS["actor"].pattern.fullmatch(candidate):
            return candidate, "tty"
    return "auto:unknown", "default"
