"""The ADF allowlist validator: the one function every rich-text value passes before it is sent.

Public API (docs/interface.md, Text conversion > ADF allowlist):
    validate_adf(doc, body_chars=..., reserve=..., reserve_nodes=...)
                                          raise ``InputError`` unless ``doc`` is on the allowlist
    parse_adf_json(text, body_chars=..., reserve=..., reserve_nodes=...)
                                          the ``--format adf`` input path: strict JSON, then
                                          ``validate_adf()``, then the emptiness rule
    canonical_json(doc), body_sha256(doc) the body hash: sha256 of canonical JSON of the final ADF
                                          (the same for Markdown and ``--format adf`` input)

The validator refuses and never repairs. It accepts exactly what ``schemas/adf.json`` accepts
(the nodes, marks, attributes and the ``href`` pattern; a test generates documents from the
schema and compares) plus the limits a schema cannot express: container depth 10, 5000 nodes, the
body size, and the sanitizer rule for text (a text node the sanitizer would change is refused).
Failures are exit 22 ``invalid_request`` with reason ``node_not_allowed`` (``details.field`` names
the path) for a shape, attribute, URL, depth or text violation, and ``body_too_large`` for the node
or character limit (as in the converter). ``parse_adf_json`` adds ``invalid_value`` for malformed
JSON, and ``empty_body`` for a document with no visible text.

The walk is iterative (a 200000-deep document gets no ``RecursionError``), stops at the first
violation, and counts nodes as they are queued, so a node bomb is refused before it is walked.
Depth is counted like the converter does: over ``bulletList``, ``orderedList``, ``listItem`` and
``blockquote`` nodes only, so five nested lists are depth 10 and a paragraph inside is fine.
Type checks are exact (``type(x) is dict``): a ``bool`` is never an int, a float is never an int
and a subclass is never accepted. The error message and ``details`` never echo the rejected value,
only a sanitized, capped path.

TODO(#22): the attribution paragraph is added by the write flow before the final validation;
callers pass nothing special (the final document already contains it).
TODO(#23): every rich-text write path (body, description, ``--set``/``--field``) calls
``validate_adf()`` on the final document right before the gateway; the write flow owns the test
that nothing unvalidated reaches the gateway.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
from typing import Any

from ticketctl.sanitize import InputError, sanitize_body, sanitize_line

MAX_DEPTH = 10  # list, listItem and blockquote containers only
MAX_NODES = 5000
DEFAULT_BODY_CHARS = 30000
MAX_JSON_NESTING = 64  # arrays and objects in the JSON text; a legal document needs about 30
_MAX_INT_LITERAL = 20  # characters of an integer literal; the allowlist has no number above 9999
_MAX_KEY_SHOWN = 64

HREF = re.compile(r"(?!https?://[^/?#]*@)https?://[\x21-\x7e]{1,2000}")
LANGUAGE = re.compile(r"[A-Za-z0-9+#._-]{1,32}")

NODE_TYPES = (
    "doc",
    "paragraph",
    "text",
    "heading",
    "bulletList",
    "orderedList",
    "listItem",
    "codeBlock",
    "blockquote",
    "hardBreak",
)
MARK_TYPES = ("strong", "em", "code", "link")

Node = dict[str, Any]
_Path = tuple[Any, Any] | None  # a linked list: (parent, segment)

_BLOCKS = frozenset({"paragraph", "heading", "codeBlock", "bulletList", "orderedList", "blockquote"})
_INLINE = frozenset({"text", "hardBreak"})
_CONTAINERS = frozenset({"bulletList", "orderedList", "listItem", "blockquote"})
_LISTS = frozenset({"bulletList", "orderedList"})
_PLAIN_MARKS = frozenset({"strong", "em", "code"})

# type -> (required keys, optional keys)
_KEYS: dict[str, tuple[frozenset[str], frozenset[str]]] = {
    "paragraph": (frozenset({"type"}), frozenset({"content"})),
    "heading": (frozenset({"type", "attrs"}), frozenset({"content"})),
    "codeBlock": (frozenset({"type"}), frozenset({"attrs", "content"})),
    "bulletList": (frozenset({"type", "content"}), frozenset()),
    "orderedList": (frozenset({"type", "content"}), frozenset({"attrs"})),
    "listItem": (frozenset({"type", "content"}), frozenset()),
    "blockquote": (frozenset({"type", "content"}), frozenset()),
    "text": (frozenset({"type", "text"}), frozenset({"marks"})),
    "hardBreak": (frozenset({"type"}), frozenset()),
}
_NON_EMPTY_CONTENT = frozenset({"bulletList", "orderedList", "listItem", "blockquote"})
_MAX_MARKS = len(MARK_TYPES)


def _fail(reason: str, message: str, path: _Path, *extra: str) -> InputError:
    return InputError(22, "invalid_request", reason, message, {"field": _format_path(path, extra)})


def _format_path(path: _Path, extra: tuple[str, ...]) -> str:
    segments: list[Any] = list(extra)[::-1]
    while path is not None:
        path, seg = path
        segments.append(seg)
    segments.reverse()
    out: list[str] = []
    for seg in segments:
        if isinstance(seg, int):
            out.append(f"[{seg}]")
        else:
            shown = sanitize_line(str(seg))[:_MAX_KEY_SHOWN]
            out.append(shown if not out else f".{shown}")
    return "".join(out) or "$"


def _too_large(what: str) -> InputError:
    return InputError(22, "invalid_request", "body_too_large", f"the document has {what}")


def validate_adf(
    doc: Any,
    *,
    body_chars: int = DEFAULT_BODY_CHARS,
    reserve: int = 0,
    reserve_nodes: int = 0,
) -> None:
    """Raise ``InputError`` (exit 22) unless ``doc`` is on the allowlist and within the limits.

    ``reserve`` and ``reserve_nodes`` are characters and nodes a caller will still add (the
    converter's convention); a final document that already holds everything passes none.
    Returns ``None`` and never modifies ``doc``. Raises nothing else for any JSON-shaped value.
    """
    if reserve < 0 or reserve_nodes < 0:
        raise ValueError("reserve and reserve_nodes must not be negative")
    node_budget = MAX_NODES - reserve_nodes
    char_budget = body_chars - reserve
    if type(doc) is not dict:
        raise _fail("node_not_allowed", "the document must be an object", None)
    _check_keys(doc, frozenset({"type", "version", "content"}), frozenset(), None)
    if doc["type"] != "doc":
        raise _fail("node_not_allowed", "the document type must be doc", None, "type")
    version = doc.get("version")
    if type(version) is not int or version != 1:
        raise _fail("node_not_allowed", "the document version must be 1", None, "version")

    count = 1
    chars = 0
    # (node, path, depth, allowed child kinds); the root's children are queued below
    stack: list[tuple[Any, _Path, int, str]] = []
    count = _queue(doc, None, 0, "block", stack, count, node_budget)
    while stack:
        node, path, depth, kind = stack.pop()
        if type(node) is not dict:
            raise _fail("node_not_allowed", "a node must be an object", path)
        chars = _check_node(node, path, depth, kind, chars, char_budget, stack)
        count = _queue_children(node, path, depth, stack, count, node_budget)


def _queue(
    parent: Node,
    path: _Path,
    depth: int,
    kind: str,
    stack: list[tuple[Any, _Path, int, str]],
    count: int,
    node_budget: int,
) -> int:
    content = parent.get("content")
    if type(content) is not list:
        raise _fail("node_not_allowed", "content must be an array", path, "content")
    count += len(content)
    if count > node_budget:
        raise _too_large("too many nodes")
    seg_path: _Path = (path, "content")
    for i in range(len(content) - 1, -1, -1):
        stack.append((content[i], (seg_path, i), depth, kind))
    return count


def _queue_children(
    node: Node,
    path: _Path,
    depth: int,
    stack: list[tuple[Any, _Path, int, str]],
    count: int,
    node_budget: int,
) -> int:
    t = node["type"]
    if t not in _KEYS or t in ("text", "hardBreak") or "content" not in node:
        return count
    if t in _CONTAINERS:
        depth += 1
    kind = {"paragraph": "inline", "heading": "inline", "codeBlock": "code", "bulletList": "item",
            "orderedList": "item"}.get(t, "block")
    return _queue(node, path, depth, kind, stack, count, node_budget)


def _check_keys(
    node: Node, required: frozenset[str], optional: frozenset[str], path: _Path
) -> None:
    for key in node:
        if type(key) is not str:
            raise _fail("node_not_allowed", "a key must be a string", path, "?")
        if key not in required and key not in optional:
            raise _fail("node_not_allowed", "an attribute is not allowed", path, key)
    for key in required:
        if key not in node:
            raise _fail("node_not_allowed", "a required attribute is missing", path, key)


def _check_node(
    node: Node,
    path: _Path,
    depth: int,
    kind: str,
    chars: int,
    char_budget: int,
    stack: list[tuple[Any, _Path, int, str]],
) -> int:
    t = node.get("type")
    if type(t) is not str:
        raise _fail("node_not_allowed", "the node type must be a string", path)
    if kind == "block":
        allowed = t in _BLOCKS
    elif kind == "item":
        allowed = t == "listItem"
    elif kind == "inline":
        allowed = t in _INLINE
    else:  # code: plain text only
        allowed = t == "text"
    if not allowed:
        raise _fail("node_not_allowed", "this node type is not allowed here", path)
    if t in _CONTAINERS and depth + 1 > MAX_DEPTH:
        raise _fail("node_not_allowed", f"nesting is deeper than {MAX_DEPTH}", path)
    required, optional = _KEYS[t]
    if kind == "code":
        optional = frozenset()
    _check_keys(node, required, optional, path)
    if "content" in node and t not in ("text", "hardBreak"):
        content = node["content"]
        if type(content) is not list:
            raise _fail("node_not_allowed", "content must be an array", path, "content")
        if t in _NON_EMPTY_CONTENT and not content:
            raise _fail("node_not_allowed", "content must not be empty", path, "content")
    if t == "text":
        return _check_text(node, path, chars, char_budget)
    if "attrs" in node:
        _check_attrs(t, node["attrs"], path)
    return chars


def _check_text(node: Node, path: _Path, chars: int, char_budget: int) -> int:
    text = node["text"]
    if type(text) is not str or not text:
        raise _fail("node_not_allowed", "text must be a non-empty string", path, "text")
    chars += len(text)
    if chars > char_budget:
        raise _too_large("too much text")
    if sanitize_body(text) != text:
        raise _fail("node_not_allowed", "text has a control or bidirectional character", path, "text")
    if "marks" in node:
        _check_marks(node["marks"], path)
    return chars


def _check_marks(marks: Any, path: _Path) -> None:
    if type(marks) is not list:
        raise _fail("node_not_allowed", "marks must be an array", path, "marks")
    if len(marks) > _MAX_MARKS:
        raise _fail("node_not_allowed", "too many marks", path, "marks")
    mpath: _Path = (path, "marks")
    seen: set[str] = set()
    for i, mk in enumerate(marks):
        here: _Path = (mpath, i)
        if type(mk) is not dict:
            raise _fail("node_not_allowed", "a mark must be an object", here)
        kind = mk.get("type")
        if type(kind) is not str or kind not in MARK_TYPES:
            raise _fail("node_not_allowed", "this mark is not allowed", here)
        if kind in seen:
            raise _fail("node_not_allowed", "a mark type appears twice", here)
        seen.add(kind)
        if kind in _PLAIN_MARKS:
            _check_keys(mk, frozenset({"type"}), frozenset(), here)
        else:
            _check_keys(mk, frozenset({"type", "attrs"}), frozenset(), here)
            attrs = mk["attrs"]
            apath: _Path = (here, "attrs")
            if type(attrs) is not dict:
                raise _fail("node_not_allowed", "attrs must be an object", apath)
            _check_keys(attrs, frozenset({"href"}), frozenset(), apath)
            href = attrs["href"]
            if type(href) is not str or HREF.fullmatch(href) is None:
                raise _fail("node_not_allowed", "the link target is not allowed", apath, "href")
    if "code" in seen and not seen <= {"code", "link"}:
        raise _fail("node_not_allowed", "code combines only with link", path, "marks")


def _check_attrs(t: str, attrs: Any, path: _Path) -> None:
    apath: _Path = (path, "attrs")
    if type(attrs) is not dict:
        raise _fail("node_not_allowed", "attrs must be an object", apath)
    if t == "heading":
        _check_keys(attrs, frozenset({"level"}), frozenset(), apath)
        _check_int(attrs["level"], 1, 6, apath, "level")
    elif t == "orderedList":
        _check_keys(attrs, frozenset(), frozenset({"order"}), apath)
        if "order" in attrs:
            _check_int(attrs["order"], 1, 9999, apath, "order")
    elif t == "codeBlock":
        _check_keys(attrs, frozenset(), frozenset({"language"}), apath)
        if "language" in attrs:
            lang = attrs["language"]
            if type(lang) is not str or LANGUAGE.fullmatch(lang) is None:
                raise _fail("node_not_allowed", "the language is not allowed", apath, "language")
    else:  # unreachable for the keys table, which allows attrs on these three only
        raise _fail("node_not_allowed", "attrs are not allowed", path, "attrs")


def _check_int(value: Any, low: int, high: int, path: _Path, key: str) -> None:
    if type(value) is not int or not low <= value <= high:
        raise _fail("node_not_allowed", f"{key} must be an integer {low} to {high}", path, key)


# --------------------------------------------------------------------------------------------
# The --format adf input path
# --------------------------------------------------------------------------------------------


def _bad_json(message: str) -> InputError:
    return InputError(22, "invalid_request", "invalid_value", message)


def _check_nesting(text: str) -> None:
    """Refuse JSON nested deeper than ``MAX_JSON_NESTING`` without recursion (string aware)."""
    depth = 0
    in_string = False
    escaped = False
    for ch in text:
        if in_string:
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == '"':
                in_string = False
        elif ch == '"':
            in_string = True
        elif ch in "[{":
            depth += 1
            if depth > MAX_JSON_NESTING:
                raise _bad_json("the JSON is nested too deeply")
        elif ch in "]}":
            depth -= 1


def _pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for key, value in pairs:
        if key in out:
            raise ValueError("duplicate key")
        out[key] = value
    return out


def _no_constant(_name: str) -> Any:
    raise ValueError("NaN and Infinity are not JSON")


def _int(literal: str) -> int:
    if len(literal) > _MAX_INT_LITERAL:
        raise ValueError("integer literal too long")
    return int(literal)


def _float(literal: str) -> float:
    value = float(literal)
    if not math.isfinite(value):
        raise ValueError("number out of range")
    return value


def parse_adf_json(
    text: str,
    *,
    body_chars: int = DEFAULT_BODY_CHARS,
    reserve: int = 0,
    reserve_nodes: int = 0,
) -> Node:
    """Parse and validate a prepared ADF document (``--format adf``) and return it.

    Raises ``InputError`` 22: ``body_too_large`` (input over 4 times ``body_chars`` characters, or
    the node and character limits), ``invalid_value`` (not strict JSON: syntax, duplicate keys,
    ``NaN``/``Infinity``, a number out of range, nesting over 64), ``node_not_allowed`` (the
    allowlist, with ``details.field``) and ``empty_body`` (no visible text; judged on the document
    before any attribution paragraph is added, which the caller adds afterwards).
    A non-str ``text`` is a ``TypeError`` (caller bug).
    """
    if not isinstance(text, str):
        raise TypeError(f"parse_adf_json needs str, got {type(text).__name__}")
    if len(text) > 4 * body_chars:
        raise _too_large("too much input")
    _check_nesting(text)
    try:
        value = json.loads(
            text,
            object_pairs_hook=_pairs,
            parse_constant=_no_constant,
            parse_int=_int,
            parse_float=_float,
        )
    except (ValueError, RecursionError) as exc:
        raise _bad_json(f"the document is not strict JSON ({type(exc).__name__})") from None
    validate_adf(value, body_chars=body_chars, reserve=reserve, reserve_nodes=reserve_nodes)
    if not _has_visible_text(value):
        raise InputError(22, "invalid_request", "empty_body", "the document has no text")
    return value


def _has_visible_text(doc: Node) -> bool:
    stack: list[Node] = [doc]
    while stack:
        node = stack.pop()
        if node.get("type") == "text" and node["text"].strip():
            return True
        stack.extend(node.get("content", ()))
    return False


# --------------------------------------------------------------------------------------------
# The body hash
# --------------------------------------------------------------------------------------------


def canonical_json(doc: Node) -> str:
    """Sorted keys, separators ``,`` and ``:``, no ASCII escaping (``ensure_ascii=False``)."""
    return json.dumps(
        doc, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False
    )


def body_sha256(doc: Node, **limits: int) -> str:
    """sha256 (hex) of the canonical JSON of ``doc``, which is validated first (``limits`` are
    the ``validate_adf`` keyword arguments)."""
    validate_adf(doc, **limits)
    return hashlib.sha256(canonical_json(doc).encode("utf-8")).hexdigest()
