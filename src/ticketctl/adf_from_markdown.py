"""Markdown to ADF: the converter for every body ticketctl sends (docs/interface.md, Writing).

Public API:
    markdown_to_adf(text, body_chars=..., reserve=..., reserve_nodes=...)
                                                         Markdown text -> ADF ``doc`` dict
    text_chars(doc)                                      the size measure: characters of text nodes

The dialect is a CommonMark subset: paragraphs, ATX headings, bullet and ordered lists, fenced
code with a language word, block quotes, inline code, bold, italic, ``http(s)`` links and hard
breaks. Everything else (tables, task lists, strikethrough, images, autolinks, raw HTML, setext
headings, ``@`` mentions, ``[~accountid:...]``, wiki markup, entities) stays
literal text. The converter builds only the nodes and marks of the ADF allowlist and never a
mention, card, media, panel or status node; it never raises except with an ``InputError``
(``empty_body``, ``body_too_large`` or, for a host the ``idna`` codec cannot encode,
``invalid_value``), a ``TypeError`` for a non-str body and a ``ValueError`` for a negative
``reserve`` or ``reserve_nodes``.

Deviations from CommonMark, all toward literal text: no lazy continuation lines, no titles on
links, no setext headings, indented code is not recognized (the indentation is dropped and the
text stays), thematic breaks and entities stay text, a soft line break is one space, and a
list or block quote nested deeper than 5 containers is literal text.
Every input is sanitized first (``sanitize_body``), so control and bidirectional characters
reach the output only as ``<U+XXXX>`` markers.

``validate_adf()`` (``adf_validate``) re-checks this output (and ``--format adf`` input) against the
allowlist; this module does not replace it.
TODO(#22): the attribution paragraph is added after conversion; callers pass its text length as
``reserve`` and its node count as ``reserve_nodes`` so the size rules count it.
"""

from __future__ import annotations

import ipaddress
import re
import unicodedata
from bisect import bisect_left
from typing import Any
from urllib.parse import quote

from ticketctl.adf_validate import HREF, LANGUAGE
from ticketctl.sanitize import InputError, sanitize_body

MAX_NEST = 5  # lists and block quotes together
MAX_NODES = 5000
DEFAULT_BODY_CHARS = 30000
_DEST_WINDOW = 4100

Node = dict[str, Any]

_LANGUAGE = LANGUAGE
_HREF = HREF
_SCHEME = re.compile(r"(https?)://", re.IGNORECASE)
_ORDERED = re.compile(r"([0-9]{1,9})([.)])(?=[ \t]|$)")
_SPECIAL = re.compile(r"[\\`*_\[\]\n]|!(?=\[)")
_BACKTICKS = re.compile(r"`+")
_ESCAPABLE = frozenset("!\"#$%&'()*+,-./:;<=>?@[\\]^_`{|}~")
_UNESCAPE = re.compile(r"\\([!-/:-@\[-`{-~])")
_DEST = re.compile(r"\((?:<((?:[^<>\n\\]|\\.)*)>|((?:[^\s()\\<]|\\.|\((?:[^\s()\\]|\\.)*\))*))\)")
_IPV6 = re.compile(r"\[([0-9A-Fa-f:.]+)\]")
_PORT = re.compile(r"[0-9]{1,5}")
_URL_SAFE = "-._~!$&'()*+,;=:@/%[]?#"
_AUTHORITY_SAFE = "-._~!$&'()*+,;=:@%[]"
_URL_FORBIDDEN = frozenset({"Cc", "Zs", "Zl", "Zp"})


def _error(reason: str, message: str) -> InputError:
    return InputError(22, "invalid_request", reason, message)


# --------------------------------------------------------------------------------------------
# Public entry points
# --------------------------------------------------------------------------------------------


def markdown_to_adf(
    text: str,
    *,
    body_chars: int = DEFAULT_BODY_CHARS,
    reserve: int = 0,
    reserve_nodes: int = 0,
) -> Node:
    """Convert Markdown to an ADF ``doc``.

    ``body_chars`` is ``limits.body_chars``; ``reserve`` and ``reserve_nodes`` are the length of
    text and the number of nodes the caller adds after conversion (the attribution paragraph),
    counted by the size rules but not by the emptiness rule. Raises only InputError 22:
    ``empty_body`` (no non-blank text), ``body_too_large`` (the input over ``4 * body_chars``
    characters, the sum of text-node characters plus ``reserve`` over ``body_chars``, or the node
    count plus ``reserve_nodes`` over the node limit) or ``invalid_value`` (a link host the
    ``idna`` codec rejects). A non-str ``text`` is a TypeError and a negative ``reserve`` or
    ``reserve_nodes`` a ValueError (caller bugs).
    """
    if not isinstance(text, str):
        raise TypeError(f"markdown_to_adf needs str, got {type(text).__name__}")
    if reserve < 0 or reserve_nodes < 0:
        raise ValueError("reserve and reserve_nodes must not be negative")
    if len(text) > 4 * body_chars:
        # The input reader caps files at 4 * body_chars bytes; nothing here is worth parsing.
        raise _error("body_too_large", "the body is too large")
    clean = sanitize_body(text)
    blocks = _parse_blocks(clean.split("\n"), 0)
    doc: Node = {"type": "doc", "version": 1, "content": blocks}
    if not _has_visible_text(doc):
        raise _error("empty_body", "the body has no text")
    if _node_count(doc) + reserve_nodes > MAX_NODES:
        raise _error("body_too_large", "the body has too many nodes")
    if text_chars(doc) + reserve > body_chars:
        raise _error("body_too_large", f"the body is over {body_chars} characters")
    return doc


def _walk(doc: Node) -> Any:
    stack: list[Node] = [doc]
    while stack:
        node = stack.pop()
        yield node
        stack.extend(node.get("content", ()))


def text_chars(doc: Node) -> int:
    """The sum of the characters of every text node (the unit of ``limits.body_chars``)."""
    return sum(len(n["text"]) for n in _walk(doc) if n.get("type") == "text")


def _node_count(doc: Node) -> int:
    return sum(1 for _ in _walk(doc))


def _has_visible_text(doc: Node) -> bool:
    return any(n.get("type") == "text" and n["text"].strip() for n in _walk(doc))


# --------------------------------------------------------------------------------------------
# Blocks
# --------------------------------------------------------------------------------------------


def _expand(line: str) -> str:
    """Expand the leading run of tabs to spaces (tab stops of 4)."""
    if line[:1] not in (" ", "\t"):
        return line
    col = 0
    i = 0
    while i < len(line) and line[i] in " \t":
        col = col + 1 if line[i] == " " else (col // 4 + 1) * 4
        i += 1
    return " " * col + line[i:]


def _strip_cols(raw: str, n: int) -> str:
    """Drop ``n`` columns of leading whitespace from a raw line (tab stops of 4); the rest of a
    tab that straddles the cut becomes spaces. Tabs after the indentation stay as they are."""
    col = 0
    i = 0
    while i < len(raw) and col < n and raw[i] in " \t":
        col += 1 if raw[i] == " " else (col // 4 + 1) * 4 - col
        i += 1
    return " " * (col - n) + raw[i:] if col > n else raw[i:]


def _blank(line: str) -> bool:
    return not line.strip(" \t")


def _indent(line: str) -> int:
    return len(line) - len(line.lstrip(" "))


def _is_hr(s: str) -> bool:
    chars = s.replace(" ", "").replace("\t", "")
    return len(chars) >= 3 and chars[0] in "-*_" and chars == chars[0] * len(chars)


def _open_fence(line: str) -> tuple[str, int, str] | None:
    if _indent(line) >= 4:
        return None
    s = line.lstrip(" ")
    ch = s[:1]
    if ch not in ("`", "~"):
        return None
    run = len(s) - len(s.lstrip(ch))
    if run < 3:
        return None
    info = s[run:].strip()
    if ch == "`" and "`" in info:
        return None
    return ch, run, info


def _closes_fence(line: str, ch: str, run: int) -> bool:
    if _indent(line) >= 4:
        return False
    s = line.strip(" \t")
    return len(s) >= run and not s.strip(ch)


def _atx(line: str) -> tuple[int, str] | None:
    if _indent(line) >= 4:
        return None
    s = line.lstrip(" ")
    level = len(s) - len(s.lstrip("#"))
    if not 1 <= level <= 6:
        return None
    rest = s[level:]
    if rest and rest[0] not in " \t":
        return None
    content = rest.strip()
    head = content.rstrip("#")
    if not head:
        content = ""
    elif head != content and head[-1] in " \t":
        content = head.rstrip()
    return level, content


def _is_quote(line: str, nest: int) -> bool:
    return nest < MAX_NEST and _indent(line) < 4 and line.lstrip(" ").startswith(">")


class _Marker:
    __slots__ = ("kind", "delim", "number", "width", "rest")

    def __init__(self, kind: str, delim: str, number: int, width: int, rest: str) -> None:
        self.kind = kind  # "b" bullet, "o" ordered
        self.delim = delim
        self.number = number
        self.width = width  # content offset of the item
        self.rest = rest  # the first line of the item, without the marker


def _marker(line: str, nest: int) -> _Marker | None:
    if nest >= MAX_NEST:
        return None
    indent = _indent(line)
    if indent >= 4:
        return None
    s = line[indent:]
    if not s:
        return None
    if s[0] in "-+*":
        if (len(s) > 1 and s[1] not in " \t") or _is_hr(s):
            return None
        kind, delim, number, mlen = "b", s[0], 0, 1
    else:
        m = _ORDERED.match(s)
        if m is None:
            return None
        kind, delim, number, mlen = "o", m.group(2), int(m.group(1)), m.end()
    end = indent + mlen
    after = line[end:]
    spaces = len(after) - len(after.lstrip(" \t"))
    if not after.strip(" \t"):
        return _Marker(kind, delim, number, end + 1, "")
    if spaces >= 5:
        return _Marker(kind, delim, number, end + 1, line[end + 1 :])
    return _Marker(kind, delim, number, end + spaces, line[end + spaces :])


def _list_start(line: str, nest: int, *, interrupting: bool) -> _Marker | None:
    m = _marker(line, nest)
    if m is None:
        return None
    if m.kind == "o" and not (1 <= m.number <= 9999):
        return None
    if interrupting and (not m.rest.strip(" \t") or (m.kind == "o" and m.number != 1)):
        return None
    return m


def _interrupts(line: str, nest: int) -> bool:
    return (
        _open_fence(line) is not None
        or _atx(line) is not None
        or _is_quote(line, nest)
        or _list_start(line, nest, interrupting=True) is not None
    )


def _parse_blocks(raws: list[str], nest: int) -> list[Node]:
    """Parse lines into blocks. Structure is judged on the tab-expanded ``lines``; code text and
    the lines handed to containers come from the ``raws`` so tabs inside code survive."""
    lines = [_expand(line) for line in raws]
    n = len(lines)
    out: list[Node] = []
    i = 0
    while i < n:
        line = lines[i]
        if _blank(line):
            i += 1
            continue
        fence = _open_fence(line)
        if fence is not None:
            node, i = _fenced_code(lines, raws, i, fence)
            out.append(node)
            continue
        heading = _atx(line)
        if heading is not None:
            level, content = heading
            node = {"type": "heading", "attrs": {"level": level}}
            inline = _parse_inline(content)
            if inline:
                node["content"] = inline
            out.append(node)
            i += 1
            continue
        if _is_quote(line, nest):
            node, i = _quote(lines, raws, i, nest)
            out.append(node)
            continue
        marker = _list_start(line, nest, interrupting=False)
        if marker is not None:
            node, i = _list(lines, raws, i, marker, nest)
            out.append(node)
            continue
        node, i = _paragraph(lines, i, nest)
        out.append(node)
    return out


def _fenced_code(
    lines: list[str], raws: list[str], i: int, fence: tuple[str, int, str]
) -> tuple[Node, int]:
    ch, run, info = fence
    indent = _indent(lines[i])
    body: list[str] = []
    j = i + 1
    while j < len(lines) and not _closes_fence(lines[j], ch, run):
        body.append(_strip_cols(raws[j], min(indent, _indent(lines[j]))))
        j += 1
    node: Node = {"type": "codeBlock"}
    word = info.split(None, 1)[0] if info else ""
    if _LANGUAGE.fullmatch(word):
        node["attrs"] = {"language": word}
    text = "\n".join(body)
    if text:
        node["content"] = [{"type": "text", "text": text}]
    return node, min(j + 1, len(lines))


def _quote(lines: list[str], raws: list[str], i: int, nest: int) -> tuple[Node, int]:
    inner: list[str] = []
    j = i
    while j < len(lines) and _is_quote(lines[j], nest):
        s = _strip_cols(raws[j], _indent(lines[j]))[1:]
        inner.append(s[1:] if s[:1] == " " else s)
        j += 1
    content = _parse_blocks(inner, nest + 1) or [{"type": "paragraph"}]
    return {"type": "blockquote", "content": content}, j


def _raw_rest(marker: _Marker, raw: str) -> str:
    """The first line of an item without its marker, from the raw line (a suffix of it)."""
    return raw[len(raw) - len(marker.rest) :] if marker.rest else ""


def _list(lines: list[str], raws: list[str], i: int, first: _Marker, nest: int) -> tuple[Node, int]:
    n = len(lines)
    items: list[Node] = []
    marker = first
    while True:
        item_lines = [_raw_rest(marker, raws[i])]
        j = i + 1
        stop = not marker.rest.strip(" \t") and j < n and _blank(lines[j])
        blanks: list[str] = []
        while not stop and j < n:
            line = lines[j]
            if _blank(line):
                # a whitespace-only line keeps what lies past the item's indent (code text)
                blanks.append(
                    _strip_cols(raws[j], marker.width) if _indent(line) >= marker.width else ""
                )
            elif _indent(line) >= marker.width:
                item_lines.extend(blanks)
                blanks = []
                item_lines.append(_strip_cols(raws[j], marker.width))
            else:
                break
            j += 1
        content = _parse_blocks(item_lines, nest + 1) or [{"type": "paragraph"}]
        items.append({"type": "listItem", "content": content})
        while j < n and _blank(lines[j]):
            j += 1
        i = j
        nxt = _marker(lines[i], nest) if i < n else None
        if nxt is None or nxt.kind != first.kind or nxt.delim != first.delim:
            break
        marker = nxt
    if first.kind == "b":
        return {"type": "bulletList", "content": items}, i
    node: Node = {"type": "orderedList", "content": items}
    if first.number != 1:
        node["attrs"] = {"order": first.number}
    return node, i


def _paragraph(lines: list[str], i: int, nest: int) -> tuple[Node, int]:
    j = i + 1
    while j < len(lines) and not _blank(lines[j]) and not _interrupts(lines[j], nest):
        j += 1
    text = "\n".join(line.lstrip(" ") for line in lines[i:j]).rstrip(" \t")
    node: Node = {"type": "paragraph"}
    inline = _parse_inline(text)
    if inline:
        node["content"] = inline
    return node, j


# --------------------------------------------------------------------------------------------
# Inline
# --------------------------------------------------------------------------------------------


class _Delim:
    __slots__ = ("ch", "count", "orig", "can_open", "can_close", "idx", "prev", "next")

    def __init__(self, ch: str, count: int, can_open: bool, can_close: bool, idx: int) -> None:
        self.ch = ch
        self.count = count
        self.orig = count
        self.can_open = can_open
        self.can_close = can_close
        self.idx = idx
        self.prev: _Delim | None = None
        self.next: _Delim | None = None


class _Bracket:
    __slots__ = ("image", "idx", "active", "bottom")

    def __init__(self, image: bool, idx: int, bottom: _Delim) -> None:
        self.image = image
        self.idx = idx
        self.active = True
        self.bottom = bottom


class _Text:
    __slots__ = ("text",)

    def __init__(self, text: str) -> None:
        self.text = text


class _Code:
    __slots__ = ("text",)

    def __init__(self, text: str) -> None:
        self.text = text


class _Break:
    __slots__ = ()


def _is_punct(ch: str) -> bool:
    return ch in _ESCAPABLE or unicodedata.category(ch).startswith("P")


def _flanking(src: str, pos: int, count: int) -> tuple[bool, bool]:
    ch = src[pos]
    prev = src[pos - 1] if pos > 0 else " "
    nxt = src[pos + count] if pos + count < len(src) else " "
    prev_ws, nxt_ws = prev.isspace(), nxt.isspace()
    prev_p, nxt_p = _is_punct(prev), _is_punct(nxt)
    left = not nxt_ws and (not nxt_p or prev_ws or prev_p)
    right = not prev_ws and (not prev_p or nxt_ws or nxt_p)
    if ch == "*":
        return left, right
    return left and (not right or prev_p), right and (not left or nxt_p)


class _Inline:
    """One paragraph's inline pass: tokens, then emphasis and links resolved over indices."""

    def __init__(self, src: str) -> None:
        self.src = src
        self.toks: list[_Text | _Delim | _Code | _Break] = []
        self.buf: list[str] = []
        self.root = _Delim("", 0, False, False, -1)
        self.tail = self.root
        self.brackets: list[_Bracket] = []
        self.floor = 0  # brackets below this index are images or already deactivated
        self.bottoms: dict[tuple[str, int, bool], _Delim | None] = {}
        self.spans: list[tuple[int, int, int]] = []  # opener idx, closer idx, 1 em / 2 strong
        self.links: list[tuple[int, int, str]] = []
        self.runs: dict[int, list[int]] = {}
        for m in _BACKTICKS.finditer(src):
            self.runs.setdefault(m.end() - m.start(), []).append(m.start())

    # -- scanning ----------------------------------------------------------------------------

    def flush(self) -> None:
        if self.buf:
            text = "".join(self.buf)
            self.buf = []
            if text:
                self.toks.append(_Text(text))

    def run(self) -> list[Node]:
        src, pos, size = self.src, 0, len(self.src)
        while pos < size:
            m = _SPECIAL.search(src, pos)
            if m is None:
                self.buf.append(src[pos:])
                break
            self.buf.append(src[pos : m.start()])
            pos = m.start()
            ch = src[pos]
            if ch == "\\":
                pos = self.backslash(pos)
            elif ch == "\n":
                pos = self.newline(pos)
            elif ch == "`":
                pos = self.code(pos)
            elif ch in "*_":
                pos = self.delimiter(pos)
            elif ch == "!":
                self.open_bracket(True)
                pos += 2
            elif ch == "[":
                self.open_bracket(False)
                pos += 1
            else:
                pos = self.close_bracket(pos)
        self.flush()
        self.emphasis(self.root)
        return self.output()

    def backslash(self, pos: int) -> int:
        nxt = self.src[pos + 1 : pos + 2]
        if nxt == "\n":
            self.flush()
            self.toks.append(_Break())
            pos += 2
            while self.src[pos : pos + 1] == " ":
                pos += 1
            return pos
        if nxt and nxt in _ESCAPABLE:
            self.buf.append(nxt)
            return pos + 2
        self.buf.append("\\")
        return pos + 1

    def newline(self, pos: int) -> int:
        spaces = 0
        while self.buf:
            last = self.buf[-1]
            kept = last.rstrip(" ")
            spaces += len(last) - len(kept)
            if kept:
                self.buf[-1] = kept
                break
            self.buf.pop()
        pos += 1
        while self.src[pos : pos + 1] == " ":
            pos += 1
        if spaces >= 2:
            self.flush()
            self.toks.append(_Break())
        else:
            self.buf.append(" ")
        return pos

    def code(self, pos: int) -> int:
        src = self.src
        k = 1
        while src[pos + k : pos + k + 1] == "`":
            k += 1
        starts = self.runs.get(k, [])
        at = bisect_left(starts, pos + k)
        if at == len(starts):
            self.buf.append("`" * k)
            return pos + k
        content = src[pos + k : starts[at]].replace("\n", " ")
        if len(content) > 2 and content[0] == " " and content[-1] == " " and content.strip(" "):
            content = content[1:-1]
        self.flush()
        self.toks.append(_Code(content))
        return starts[at] + k

    def delimiter(self, pos: int) -> int:
        src = self.src
        ch = src[pos]
        k = 1
        while src[pos + k : pos + k + 1] == ch:
            k += 1
        can_open, can_close = _flanking(src, pos, k)
        if not (can_open or can_close):
            self.buf.append(ch * k)
            return pos + k
        self.flush()
        d = _Delim(ch, k, can_open, can_close, len(self.toks))
        d.prev, self.tail.next, self.tail = self.tail, d, d
        self.toks.append(d)
        return pos + k

    def open_bracket(self, image: bool) -> None:
        self.flush()
        self.brackets.append(_Bracket(image, len(self.toks), self.tail))
        self.toks.append(_Text("![" if image else "["))

    def close_bracket(self, pos: int) -> int:
        if not self.brackets:
            self.buf.append("]")
            return pos + 1
        opener = self.brackets.pop()
        self.floor = min(self.floor, len(self.brackets))
        if opener.image or not opener.active:
            self.buf.append("]")
            return pos + 1
        m = _DEST.match(self.src, pos + 1, pos + 1 + _DEST_WINDOW)
        href = None
        if m is not None:
            angled = m.group(1) is not None
            raw = m.group(1) if angled else m.group(2)
            href = _href(raw, angled=angled)
        if href is None or m is None:
            self.buf.append("]")
            return pos + 1
        self.flush()
        if not self.visible(opener.idx + 1):
            self.buf.append("]")
            return pos + 1
        self.toks[opener.idx] = _Text("")
        self.emphasis(opener.bottom)
        self.links.append((opener.idx + 1, len(self.toks), href))
        for k in range(len(self.brackets) - 1, self.floor - 1, -1):
            b = self.brackets[k]  # earlier link openers can no longer open a link
            if not b.image:
                b.active = False
        self.floor = len(self.brackets)
        return m.end()

    def visible(self, start: int) -> bool:
        """Whether the tokens from ``start`` hold any non-whitespace text (a link needs some)."""
        for tok in self.toks[start:]:
            if isinstance(tok, _Delim):
                return True
            if isinstance(tok, (_Text, _Code)) and tok.text.strip():
                return True
        return False

    # -- emphasis (the CommonMark delimiter algorithm over a linked list) ---------------------

    def emphasis(self, bottom: _Delim) -> None:
        bottoms = self.bottoms = {}
        closer = bottom.next
        while closer is not None:
            if not closer.can_close:
                closer = closer.next
                continue
            key = (closer.ch, closer.orig % 3, closer.can_open)
            limit = bottoms.get(key, bottom)
            opener = closer.prev
            found = None
            while opener is not None and opener is not bottom and opener is not limit:
                odd = (
                    (closer.can_open or opener.can_close)
                    and closer.orig % 3 != 0
                    and (opener.orig + closer.orig) % 3 == 0
                )
                if opener.ch == closer.ch and opener.can_open and not odd:
                    found = opener
                    break
                opener = opener.prev
            if found is None:
                bottoms[key] = closer.prev
                nxt = closer.next
                if not closer.can_open:
                    self.unlink(closer)
                closer = nxt
                continue
            use = 2 if closer.count >= 2 and found.count >= 2 else 1
            self.spans.append((found.idx, closer.idx, use))
            found.count -= use
            closer.count -= use
            found.next, closer.prev = closer, found  # drop the delimiters between them
            for key, held in bottoms.items():
                if held is not None and found.idx < held.idx < closer.idx:
                    bottoms[key] = found
            if found.count == 0:
                self.unlink(found)
            if closer.count == 0:
                nxt = closer.next
                self.unlink(closer)
                closer = nxt
        # Delimiters above ``bottom`` take no part in later matches.
        bottom.next = None
        self.tail = bottom

    def unlink(self, d: _Delim) -> None:
        for key, held in self.bottoms.items():  # a scan limit must stay in the list
            if held is d:
                self.bottoms[key] = d.prev
        if d.prev is not None:
            d.prev.next = d.next
        if d.next is not None:
            d.next.prev = d.prev

    # -- output ------------------------------------------------------------------------------

    def output(self) -> list[Node]:
        size = len(self.toks)
        strong = [0] * (size + 1)
        em = [0] * (size + 1)
        for o, c, use in self.spans:
            arr = strong if use == 2 else em
            arr[o + 1] += 1
            arr[c] -= 1
        href: list[str | None] = [None] * size
        for s, e, h in self.links:
            for k in range(s, e):
                href[k] = h
        out: list[Node] = []
        cur: Node | None = None
        cur_key: tuple[bool, bool, bool, str | None] | None = None
        s_run = e_run = 0
        for idx, tok in enumerate(self.toks):
            s_run += strong[idx]
            e_run += em[idx]
            if isinstance(tok, _Break):
                out.append({"type": "hardBreak"})
                cur = cur_key = None
                continue
            is_code = isinstance(tok, _Code)
            if isinstance(tok, _Delim):
                text = tok.ch * tok.count
            else:
                text = tok.text
            if not text:
                continue
            key = (s_run > 0 and not is_code, e_run > 0 and not is_code, is_code, href[idx])
            if cur is not None and key == cur_key:
                cur["text"] += text
                continue
            cur = {"type": "text", "text": text}
            cur_key = key
            marks: list[Node] = []
            if key[0]:
                marks.append({"type": "strong"})
            if key[1]:
                marks.append({"type": "em"})
            if key[2]:
                marks.append({"type": "code"})
            if key[3] is not None:
                marks.append({"type": "link", "attrs": {"href": key[3]}})
            if marks:
                cur["marks"] = marks
            out.append(cur)
        return out


def _parse_inline(src: str) -> list[Node]:
    return _Inline(src).run()


# --------------------------------------------------------------------------------------------
# Link targets
# --------------------------------------------------------------------------------------------


def _forbidden(ch: str) -> bool:
    return ord(ch) <= 0x20 or ord(ch) == 0x7F or unicodedata.category(ch) in _URL_FORBIDDEN


def _has_ace_label(host: str) -> bool:
    return any(label.lower().startswith("xn--") for label in host.split("."))


def _split_authority(authority: str) -> tuple[str, str | None] | None:
    """Split ``host[:port]`` (the host an IPv6 literal in brackets); None when it is not valid."""
    if authority.startswith("["):
        end = authority.find("]")
        m = _IPV6.fullmatch(authority[: end + 1]) if end != -1 else None
        if m is None:
            return None
        try:
            ipaddress.IPv6Address(m.group(1))
        except ValueError:
            return None
        host, rest = authority[: end + 1], authority[end + 1 :]
        if not rest:
            return host, None
        if rest[0] != ":" or _PORT.fullmatch(rest[1:]) is None:
            return None
        return host, rest[1:]
    host, sep, port = authority.partition(":")
    if not host or any(c in host for c in "[]%:"):
        return None
    if sep and _PORT.fullmatch(port) is None:
        return None
    return host, (port if sep else None)


def _href(raw: str, *, angled: bool) -> str | None:
    """The normalized ``http(s)`` target, or None when the text is not a safe link target.

    None leaves the whole construct literal. Only an authority the ``idna`` codec rejects
    raises (``invalid_value``), as the interface requires.
    """
    dest = _UNESCAPE.sub(r"\1", raw)
    if angled:
        dest = dest.replace(" ", "%20")
    if not dest or "<U+" in dest or any(_forbidden(c) for c in dest):
        return None
    m = _SCHEME.match(dest)
    if m is None:
        return None
    rest = dest[m.end() :]
    cut = len(rest)
    for stop in "/?#":
        at = rest.find(stop)
        if at != -1:
            cut = min(cut, at)
    authority, tail = rest[:cut], rest[cut:]
    if not authority or "@" in authority or "\\" in authority:
        return None
    parsed = _split_authority(authority)
    if parsed is None:
        return None
    host, port = parsed
    if host.isascii():
        if _has_ace_label(host):  # a punycode label must decode, else the host is not valid
            try:
                host.encode("idna").decode("idna")
            except UnicodeError:
                return None
    else:
        try:
            encoded = host.encode("idna")
        except UnicodeError:
            raise _error("invalid_value", "a link host cannot be encoded") from None
        try:
            remapped = encoded.decode("idna") != host.lower()  # IDNA 2003 changed the host
        except UnicodeError:
            remapped = True
        if remapped:
            return None
        host = encoded.decode("ascii")
    authority = host + (":" + port if port is not None else "")
    href = (
        m.group(1).lower()
        + "://"
        + quote(authority, safe=_AUTHORITY_SAFE)
        + quote(tail, safe=_URL_SAFE)
    )
    if _HREF.fullmatch(href) is None:
        return None
    return href
