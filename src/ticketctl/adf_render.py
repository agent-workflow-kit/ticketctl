"""ADF to Markdown renderer with budgets (docs/interface.md, Text conversion > Reading).

Public API:
    render_adf(doc, part="description") -> Rendered(text, incomplete, reasons)
    DEPTH_LIMIT, NODE_LIMIT, OUTPUT_LIMIT       the documented budgets

The renderer never raises: whatever the provider sends (unknown nodes, malformed attributes, a
document nested thousands deep, shared or cyclic Python structures) ends in text. Every text
piece passes the body-class sanitizer at the leaf, so the output is already sanitized. Text is
not escaped (the consumer fences); the only escape is ``|`` inside a table cell, which would
otherwise end the cell.

Budgets: nodes nested deeper than DEPTH_LIMIT render ``[too deep]``; more than NODE_LIMIT visited
nodes or more than OUTPUT_LIMIT characters of output end the text with ``[too large]``. Any
budget hit (and a table cut to 200 rows) names ``part`` in ``incomplete`` and the budget in
``reasons`` (``too_deep``, ``too_many_nodes``, ``too_large``, ``table_rows``, ``internal``).
An ``internal`` failure keeps the text rendered so far and appends ``[render error]``.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any
from urllib.parse import quote

from ticketctl.sanitize import sanitize_body, sanitize_line

DEPTH_LIMIT = 50
NODE_LIMIT = 50_000
OUTPUT_LIMIT = 1 << 20
TABLE_ROW_LIMIT = 200
_MARK_LIMIT = 20

TOO_DEEP = "[too deep]"
TOO_LARGE = "[too large]"
UNSAFE_LINK = "[unsafe link removed]"
RENDER_ERROR = "[render error]"
HREF_LIMIT = 2048

_MEDIA_ID = re.compile(r"[A-Za-z0-9-]{1,64}")
_TYPE_WORD = re.compile(r"[A-Za-z]{1,32}")
_LANGUAGE = re.compile(r"[A-Za-z0-9+#._-]{1,32}")
_SAFE_SCHEME = re.compile(r"(?:https?|mailto):", re.IGNORECASE | re.ASCII)
_HREF_ENCODE = re.compile(r"[)\x00-\x20\x7f-\U0010ffff]")
_SPLIT_WS = re.compile(r"(\s*)(.*?)(\s*)", re.DOTALL)
_PARTIAL_MARKER = re.compile(r"<(?:U(?:\+[0-9A-F]{0,6})?)?$")
_BACKTICKS = re.compile(r"`+")

# Known block nodes: rendered line by line. Everything else is inline content (text, marks,
# emoji, ...) or an unknown node.
_BLOCKS = frozenset(
    {
        "doc",
        "paragraph",
        "heading",
        "blockquote",
        "bulletList",
        "orderedList",
        "codeBlock",
        "rule",
        "panel",
        "expand",
        "nestedExpand",
        "table",
        "taskList",
        "mediaSingle",
        "mediaGroup",
        "caption",
    }
)
# Known containers that are flattened to one line when they occur inside inline content.
_FLATTEN = _BLOCKS | {"listItem", "taskItem", "tableRow", "tableCell", "tableHeader"}
_LISTS = frozenset({"bulletList", "orderedList", "taskList"})
_MARK_ORDER = ("code", "em", "strong", "strike")
_MARKERS = {"em": "*", "strong": "**", "strike": "~~"}


@dataclass(frozen=True)
class Rendered:
    """Markdown text plus the budget outcome (``incomplete`` names ``part`` when cut)."""

    text: str
    incomplete: tuple[str, ...] = ()
    reasons: tuple[str, ...] = ()


def render_adf(doc: object, part: str = "description") -> Rendered:
    """Render ``doc`` (any JSON-like value) to sanitized Markdown; never raises."""
    ctx = _Ctx()
    try:
        ctx.blocks([doc], 1, tight=False)
        text = ctx.finish()
    except Exception:  # noqa: BLE001 - the contract is "never raises"; the tests pin "internal"
        ctx.reasons.add("internal")
        text = ctx.partial()
    reasons = tuple(sorted(ctx.reasons))
    return Rendered(text=text, incomplete=(part,) if reasons else (), reasons=reasons)


class _Ctx:
    """Budgets, the line emitter with container prefixes, and the node renderers."""

    def __init__(self) -> None:
        self.nodes = 0
        self.used = 0
        self.stopped = False
        self.reasons: set[str] = set()
        self.lines: list[str] = []
        self.levels: list[list[str | None]] = []  # [continuation prefix, first-line prefix]
        self.last_blank = True
        self.room_limit = OUTPUT_LIMIT - len(TOO_LARGE) - 1

    # ---- budgets -------------------------------------------------------------------------

    def stop(self, reason: str) -> None:
        self.stopped = True
        self.reasons.add(reason)

    def visit(self, depth: int) -> str | None:
        """Count a node; ``"stop"`` when rendering must end, ``"deep"`` when too nested."""
        if self.stopped:
            return "stop"
        self.nodes += 1
        if self.nodes > NODE_LIMIT:
            self.stop("too_many_nodes")
            return "stop"
        if depth > DEPTH_LIMIT:
            self.reasons.add("too_deep")
            return "deep"
        return None

    def leaf(self, value: object) -> str:
        """Sanitize one text piece and charge it to the output budget."""
        if not isinstance(value, str) or self.stopped:
            return ""
        room = self.room_limit - self.used
        if room <= 0:
            if value:
                self.stop("too_large")
            return ""
        raw = value[: room + 1]
        text = sanitize_body(raw)
        if len(text) > room or len(raw) < len(value):
            text = _PARTIAL_MARKER.sub("", text[:room])
            self.stop("too_large")
        self.used += len(text)
        return text

    def charge(self, size: int) -> None:
        """Charge synthetic text (link suffixes, table padding) to the output budget."""
        self.used += size
        if self.used > self.room_limit:
            self.stop("too_large")

    def partial(self) -> str:
        """The text rendered so far plus the error marker, for an internal failure."""
        try:
            out = self.finish()
        except Exception:  # noqa: BLE001 - finish itself failed; fall back to the raw lines
            out = "\n".join(self.lines)
        out = _PARTIAL_MARKER.sub("", out[: OUTPUT_LIMIT - len(RENDER_ERROR) - 1])
        return out + "\n" + RENDER_ERROR if out else RENDER_ERROR

    def finish(self) -> str:
        while self.lines and not self.lines[-1].strip():
            self.lines.pop()
        out = "\n".join(self.lines)
        if len(out) > self.room_limit:
            out = _PARTIAL_MARKER.sub("", out[: self.room_limit])
            self.stop("too_large")
        if "too_large" in self.reasons or "too_many_nodes" in self.reasons:
            out = out + "\n" + TOO_LARGE if out else TOO_LARGE
        return out

    # ---- line output ---------------------------------------------------------------------

    def push(self, cont: str, first: str | None = None) -> None:
        self.levels.append([cont, first])

    def pop(self) -> None:
        self.levels.pop()

    def _prefix(self, use_first: bool) -> str:
        return "".join(
            lv[1] if use_first and lv[1] is not None else (lv[0] or "") for lv in self.levels
        )

    def pending(self) -> bool:
        return any(lv[1] is not None for lv in self.levels)

    def line(self, text: str) -> None:
        # A stopped renderer still emits text it already built (the clipped last piece); only
        # the output budget refuses a line.
        prefix = self._prefix(True)
        for lv in self.levels:
            lv[1] = None
        out = prefix + text
        if not text:
            out = out.rstrip()
        self.used += len(prefix) + (1 if self.lines else 0)
        if self.used > self.room_limit:
            self.stop("too_large")
            return
        self.lines.append(out)
        self.last_blank = False

    def text(self, text: str) -> None:
        for part in text.split("\n"):
            self.line(part)

    def blank(self) -> None:
        if self.stopped or self.last_blank or self.pending():
            return
        prefix = self._prefix(False)
        self.used += len(prefix) + 1
        self.lines.append(prefix.rstrip())
        self.last_blank = True

    # ---- blocks --------------------------------------------------------------------------

    def blocks(self, children: object, depth: int, *, tight: bool) -> None:
        """Render sibling nodes (all at ``depth``); runs of inline nodes become paragraphs."""
        if not isinstance(children, list):
            return
        run: list[Any] = []
        emitted = False

        def flush() -> None:
            nonlocal emitted
            if not run:
                return
            text = self.inline(run, depth)
            run.clear()
            if text.strip():
                if emitted:
                    self.blank()
                self.text(text)
                emitted = True

        for child in children:
            if self.stopped:
                break
            if _kind(child) in _BLOCKS:
                flush()
                if emitted and not (tight and _kind(child) in _LISTS):
                    self.blank()
                before = len(self.lines)
                self.block(child, depth)
                emitted = emitted or len(self.lines) > before
            else:
                run.append(child)
        flush()

    def block(self, node: dict[str, Any], depth: int) -> None:
        state = self.visit(depth)
        if state == "stop":
            return
        if state == "deep":
            self.line(TOO_DEEP)
            return
        kind = _kind(node)
        content = node.get("content")
        attrs = _attrs(node)
        if kind == "doc":
            self.blocks(content, depth + 1, tight=False)
        elif kind in ("paragraph", "caption"):
            text = self.inline(_list(content), depth + 1)
            if text.strip():
                self.text(text)
        elif kind == "heading":
            level = attrs.get("level")
            n = level if isinstance(level, int) and not isinstance(level, bool) else 1
            text = " ".join(self.inline(_list(content), depth + 1).split())
            self.line("#" * min(max(n, 1), 6) + (" " + text if text else ""))
        elif kind == "blockquote":
            self.push("> ")
            self.blocks(content, depth + 1, tight=False)
            self.pop()
        elif kind in ("panel", "expand", "nestedExpand"):
            self.push("> ")
            title = attrs.get("title")
            if isinstance(title, str) and title.strip():
                self.line(" ".join(self.leaf(title).split()))
            self.blocks(content, depth + 1, tight=False)
            self.pop()
        elif kind in ("bulletList", "orderedList", "taskList"):
            self.list_block(kind, attrs, content, depth)
        elif kind == "codeBlock":
            self.code_block(attrs, content, depth)
        elif kind == "rule":
            self.line("---")
        elif kind == "table":
            self.table(content, depth)
        else:  # mediaSingle, mediaGroup: each child on its own line
            for child in _list(content):
                self.blocks([child], depth + 1, tight=True)

    def list_block(self, kind: str, attrs: dict[str, Any], content: object, depth: int) -> None:
        start = attrs.get("order")
        number = 1
        if isinstance(start, int) and not isinstance(start, bool) and 0 <= start <= 9999:
            number = start
        for child in _list(content):
            if self.stopped:
                break
            if _kind(child) in ("listItem", "taskItem"):
                state = self.visit(depth + 1)
                if state == "stop":
                    break
                if state == "deep":
                    self.line(TOO_DEEP)
                    continue
                if kind == "orderedList":
                    marker = f"{number}. "
                    number += 1
                elif kind == "taskList":
                    done = _attrs(child).get("state") == "DONE"
                    marker = "- [x] " if done else "- [ ] "
                else:
                    marker = "- "
                self.push(" " * len(marker), marker)
                self.blocks(child.get("content"), depth + 2, tight=True)
                if self.pending():
                    self.line("")
                self.pop()
            else:  # a nested list (taskList inside taskList) or stray content
                self.push("  ")
                self.blocks([child], depth + 1, tight=True)
                self.pop()

    def code_block(self, attrs: dict[str, Any], content: object, depth: int) -> None:
        pieces: list[str] = []
        for child in _list(content):
            if self.visit(depth + 1) is not None:
                continue
            if _kind(child) == "text":
                pieces.append(self.leaf(child.get("text")))
        code = "".join(pieces)
        longest = max((len(m.group()) for m in _BACKTICKS.finditer(code)), default=0)
        fence = "`" * max(3, longest + 1)
        language = attrs.get("language")
        tag = language if isinstance(language, str) and _LANGUAGE.fullmatch(language) else ""
        self.line(fence + tag)
        if code:
            for part in code.split("\n"):
                self.line(part)
        self.line(fence)

    def table(self, content: object, depth: int) -> None:
        rows = _list(content)
        grid: list[list[str]] = []
        for row in rows[:TABLE_ROW_LIMIT]:
            state = self.visit(depth + 1)
            if state == "stop":
                break
            if state == "deep":
                grid.append([TOO_DEEP])
                continue
            cells: list[str] = []
            for cell in _list(row.get("content") if isinstance(row, dict) else None):
                cstate = self.visit(depth + 2)
                if cstate == "stop":
                    break
                if cstate == "deep":
                    cells.append(TOO_DEEP)
                    continue
                inner = cell.get("content") if isinstance(cell, dict) else [cell]
                text = " ".join(self.inline(_list(inner), depth + 3).split())
                cells.append(text.replace("|", "\\|"))
            grid.append(cells)
        if len(rows) > TABLE_ROW_LIMIT:
            self.reasons.add("table_rows")
        width = max((len(r) for r in grid), default=0)
        if width:
            for index, cells in enumerate(grid):
                self.charge(3 * width + 4 + (3 * width + 2 if index == 0 else 0))
                if self.stopped:
                    break
                padded = cells + [""] * (width - len(cells))
                self.line("| " + " | ".join(padded) + " |")
                if index == 0:
                    self.line("|" + " --- |" * width)
        if len(rows) > TABLE_ROW_LIMIT:
            self.line(f"[{len(rows) - TABLE_ROW_LIMIT} more rows not shown]")

    # ---- inline --------------------------------------------------------------------------

    def inline(self, nodes: Sequence[Any], depth: int) -> str:
        parts: list[str] = []
        i = 0
        while i < len(nodes) and not self.stopped:
            href = _link_href(nodes[i])
            if href is None:
                parts.append(self.inline_node(nodes[i], depth))
                i += 1
                continue
            j = i
            texts: list[str] = []
            while j < len(nodes) and _link_href(nodes[j]) == href:
                texts.append(self.text_node(nodes[j], depth))
                j += 1
            parts.append(self.link("".join(texts), href))
            i = j
        return "".join(parts)

    def text_node(self, node: dict[str, Any], depth: int) -> str:
        state = self.visit(depth)
        if state == "stop":
            return ""
        if state == "deep":
            return TOO_DEEP
        return self.text_body(node)

    def inline_node(self, node: Any, depth: int) -> str:
        state = self.visit(depth)
        if state == "stop":
            return ""
        if state == "deep":
            return TOO_DEEP
        if not isinstance(node, dict):
            return "[unsupported: unknown]"
        kind = _kind(node)
        attrs = _attrs(node)
        if kind == "text":
            return self.text_body(node)
        if kind == "hardBreak":
            return "\n"
        if kind == "emoji":
            return self.emoji(attrs)
        if kind == "status":
            return self.leaf(attrs.get("text")) or "[status]"
        if kind == "date":
            return self.date(attrs)
        if kind == "mention":
            name = attrs.get("text")
            name = name.lstrip("@").strip() if isinstance(name, str) else ""
            return "@" + (self.leaf(name) or "unknown")
        if kind in ("inlineCard", "blockCard", "embedCard"):
            data = attrs.get("data")
            url = attrs.get("url")
            if not isinstance(url, str) and isinstance(data, dict):
                url = data.get("url")
            if not isinstance(url, str):
                return f"[unsupported: {kind}]"
            shown = _safe_href(url) or UNSAFE_LINK
            self.charge(len(shown))
            return shown
        if kind in ("media", "mediaInline"):
            ident = attrs.get("id")
            ok = isinstance(ident, str) and _MEDIA_ID.fullmatch(ident)
            return f"[attachment: {ident if ok else '?'}]"
        children = _list(node.get("content"))
        if kind in _FLATTEN:
            return " " + self.inline(children, depth + 1).strip() + " "
        label = kind if kind and _TYPE_WORD.fullmatch(kind) else "unknown"
        plain = " ".join(self.inline(children, depth + 1).split())
        return f"[unsupported: {label}]" + (" " + plain if plain else "")

    def text_body(self, node: dict[str, Any]) -> str:
        text = self.leaf(node.get("text"))
        kinds: set[str] = set()
        marks = node.get("marks")
        if isinstance(marks, list):
            for mark in marks[:_MARK_LIMIT]:
                if isinstance(mark, dict) and isinstance(mark.get("type"), str):
                    kinds.add(mark["type"])
        match = _SPLIT_WS.fullmatch(text)
        lead, core, trail = match.groups() if match else ("", text, "")
        if not core:
            return text
        for kind in _MARK_ORDER:
            if kind not in kinds:
                continue
            if kind == "code":
                run = max((len(m.group()) for m in _BACKTICKS.finditer(core)), default=0)
                tick = "`" * (run + 1)
                pad = " " if core.startswith("`") or core.endswith("`") else ""
                core = f"{tick}{pad}{core}{pad}{tick}"
            else:
                core = _MARKERS[kind] + core + _MARKERS[kind]
        return lead + core + trail

    def link(self, text: str, href: str) -> str:
        safe = _safe_href(href)
        if safe is None:
            self.charge(len(UNSAFE_LINK))
            return UNSAFE_LINK
        shown = text.strip()
        bare = href[len("mailto:") :] if href[:7].lower() == "mailto:" else href
        if not shown:
            self.charge(len(safe))
            return safe
        if shown in (href, bare):
            return text
        self.charge(len(safe) + 3)
        return f"{text} ({safe})"

    def emoji(self, attrs: dict[str, Any]) -> str:
        text = attrs.get("text")
        if isinstance(text, str) and text:
            return self.leaf(text)
        short = attrs.get("shortName")
        if isinstance(short, str) and short.strip(":"):
            return ":" + self.leaf(short.strip(":")) + ":"
        return "[emoji]"

    def date(self, attrs: dict[str, Any]) -> str:
        raw = attrs.get("timestamp")
        try:
            if isinstance(raw, bool) or not isinstance(raw, (int, float, str)):
                raise ValueError(raw)
            moment = datetime.fromtimestamp(float(raw) / 1000, tz=UTC)
            return moment.strftime("%Y-%m-%d")
        except (ValueError, OverflowError, OSError):
            return "[invalid date]"


def _kind(node: object) -> str | None:
    """The node's ``type`` when it is a string (an unhashable or non-string type is None)."""
    if isinstance(node, dict):
        kind = node.get("type")
        if isinstance(kind, str):
            return kind
    return None


def _attrs(node: object) -> dict[str, Any]:
    attrs = node.get("attrs") if isinstance(node, dict) else None
    return attrs if isinstance(attrs, dict) else {}


def _list(value: object) -> list[Any]:
    return value if isinstance(value, list) else []


def _link_href(node: object) -> str | None:
    """The href of a text node's link mark, else None (a malformed link mark is dropped)."""
    if _kind(node) != "text":
        return None
    assert isinstance(node, dict)
    marks = node.get("marks")
    if not isinstance(marks, list):
        return None
    for mark in marks[:_MARK_LIMIT]:
        if _kind(mark) == "link":
            href = _attrs(mark).get("href")
            return href if isinstance(href, str) else None
    return None


def _encode_char(match: re.Match[str]) -> str:
    ch = match.group()
    if "\ud800" <= ch <= "\udfff":  # a lone surrogate cannot be UTF-8 encoded
        return "%EF%BF%BD"
    if ord(ch) < 0x80 or ch.isspace() or sanitize_line(ch) != ch:
        return quote(ch, safe="")
    return ch


def _safe_href(href: str) -> str | None:
    """The href with ``)``, spaces and control characters percent-encoded, or None if unsafe."""
    if len(href) > HREF_LIMIT or not _SAFE_SCHEME.match(href):
        return None
    return _HREF_ENCODE.sub(_encode_char, href)
