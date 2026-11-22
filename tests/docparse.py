"""A small reader for the Markdown documents the parity tests compare (issue #40).

It finds sections by heading and tables by their header row, never by line number, so an edit that
moves text does not break a test and an edit that removes or renames a heading or a column does.
Every failure is a ``ParityError`` that names the heading, table or value that is missing.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass


class ParityError(AssertionError):
    """The document does not have the shape a parity test reads."""


@dataclass(frozen=True)
class Section:
    level: int
    title: str
    body: str


@dataclass(frozen=True)
class Table:
    header: tuple[str, ...]
    rows: tuple[tuple[str, ...], ...]


_HEADING = re.compile(r"^(#{1,6})[ \t]+(.+?)[ \t]*#*[ \t]*$")
_FENCE = re.compile(r"^[ \t]*(`{3,}|~{3,})")
_SPAN = re.compile(r"`([^`\n]+)`")


def sections(md: str) -> list[Section]:
    """Every heading outside a fenced block with the text up to the next heading of any level."""
    found: list[tuple[int, str, list[str]]] = []
    fence: str | None = None
    for line in md.splitlines():
        marker = _FENCE.match(line)
        if marker:
            mark = marker.group(1)
            if fence is None:
                fence = mark[0]
            elif mark[0] == fence:
                fence = None
        heading = None if fence is not None or marker else _HEADING.match(line)
        if heading:
            found.append((len(heading.group(1)), heading.group(2).replace("`", ""), []))
        elif found:
            found[-1][2].append(line)
    return [Section(level, title, "\n".join(lines)) for level, title, lines in found]


def section(md: str, title: str) -> str:
    """The body of the one section called ``title`` (backticks in the heading are ignored)."""
    hits = [s for s in sections(md) if s.title == title]
    if len(hits) != 1:
        raise ParityError(f"expected exactly one heading {title!r}, found {len(hits)}")
    return hits[0].body


def subheadings(md: str, title: str, level: int) -> list[str]:
    """Titles of the headings of ``level`` inside the section ``title`` (up to its next sibling)."""
    all_sections = sections(md)
    parents = [i for i, s in enumerate(all_sections) if s.title == title]
    if len(parents) != 1:
        raise ParityError(f"expected exactly one heading {title!r}, found {len(parents)}")
    start = parents[0]
    out: list[str] = []
    for s in all_sections[start + 1 :]:
        if s.level < level:
            break
        if s.level == level:
            out.append(s.title)
    return out


def _cells(line: str) -> tuple[str, ...]:
    text = line.strip()
    text = (
        text.removeprefix("|").removesuffix("|") if text.endswith("|") else text.removeprefix("|")
    )
    cells: list[str] = []
    cur: list[str] = []
    i = 0
    in_code = False
    while i < len(text):
        ch = text[i]
        if ch == "\\" and i + 1 < len(text) and text[i + 1] == "|":
            cur.append("|")
            i += 2
            continue
        if ch == "`":
            in_code = not in_code
        if ch == "|" and not in_code:
            cells.append("".join(cur).strip())
            cur = []
        else:
            cur.append(ch)
        i += 1
    cells.append("".join(cur).strip())
    return tuple(cells)


def tables(body: str) -> list[Table]:
    """Every pipe table in ``body`` (a header row, a separator row, then body rows)."""
    lines = body.splitlines()
    out: list[Table] = []
    i = 0
    while i < len(lines):
        if (
            lines[i].lstrip().startswith("|")
            and i + 1 < len(lines)
            and re.fullmatch(r"[ \t]*\|?[ \t:|-]+\|[ \t:|-]*", lines[i + 1])
            and "-" in lines[i + 1]
        ):
            header = _cells(lines[i])
            rows: list[tuple[str, ...]] = []
            j = i + 2
            while j < len(lines) and lines[j].lstrip().startswith("|"):
                rows.append(_cells(lines[j]))
                j += 1
            out.append(Table(header, tuple(rows)))
            i = j
        else:
            i += 1
    return out


def table(body: str, *header: str) -> Table:
    """The one table whose header row starts with ``header``."""
    hits = [t for t in tables(body) if t.header[: len(header)] == header]
    if len(hits) != 1:
        raise ParityError(
            f"expected exactly one table with header {list(header)}, found {len(hits)}"
        )
    return hits[0]


def spans(text: str) -> list[str]:
    """The text of every backtick span, in order."""
    return _SPAN.findall(text)


def names(text: str, pattern: str = r"[a-z][a-z0-9_]*") -> list[str]:
    """The backtick spans of ``text`` that are one plain name (a code, a key, a hook)."""
    return [s for s in spans(text) if re.fullmatch(pattern, s)]


def without_parens(text: str) -> str:
    """``text`` with every parenthetical removed (they hold asides, not list members)."""
    prev = None
    while prev != text:
        prev = text
        text = re.sub(r"\([^()]*\)", "", text)
    return text


def capture(text: str, pattern: str, what: str) -> str:
    """Group 1 of the first match of ``pattern``; a missing match names ``what``."""
    match = re.search(pattern, text, re.S)
    if not match:
        raise ParityError(f"cannot find {what} in the document")
    return match.group(1)


def difference(left_name: str, left: Iterable[str], right_name: str, right: Iterable[str]) -> str:
    """An empty string when the two collections agree, else a message naming each difference."""
    a, b = set(left), set(right)
    if a == b:
        return ""
    parts: list[str] = []
    if a - b:
        parts.append(f"in {left_name} but not in {right_name}: {sorted(a - b)}")
    if b - a:
        parts.append(f"in {right_name} but not in {left_name}: {sorted(b - a)}")
    return "; ".join(parts)


def assert_same(left_name: str, left: Iterable[str], right_name: str, right: Iterable[str]) -> None:
    left, right = list(left), list(right)
    for name, items in ((left_name, left), (right_name, right)):
        if len(items) != len(set(items)):
            dup = sorted({x for x in items if items.count(x) > 1})
            raise AssertionError(f"{name} lists a value twice: {dup}")
    message = difference(left_name, left, right_name, right)
    if message:
        raise AssertionError(message)
