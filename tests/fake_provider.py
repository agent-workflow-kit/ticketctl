"""A fake of the Jira Cloud REST API for tests (issue #10).

Everything here is synthetic: the site, the project key (``DEMO``), the accounts (``acct-*``), the
custom field ids (the 90001 to 90006 range), the sprints and every text. Shapes follow the facts of
docs/design.md (Live verification, V-1 to V-33) and the endpoint table of docs/interface.md; the
places where the fake had to guess are listed in the ``UNVERIFIED`` constant at the end of this
module and are reported on the live-verification issues.

Safety: the server is ``tests/scripted_server.py`` (loopback 127.0.0.1, an ephemeral port, daemon
threads of this process, torn down exactly: the listener, its accept thread and every accepted
socket). It starts no process and opens no connection except to itself. A test that needs a real
timeout injects ``Fault("slow", delay_s=...)`` or ``Quirks.response_delay_s``; ``close()`` wakes
every sleeping handler.

Quirks (``Quirks``) default to the behavior the live checks verified, so a test that does not look
at them sees the real provider's surprises. Failure injection (``Fault``) is off by default.
Time is a fake wall clock (``FakeProvider.now``): it moves only through ``advance()`` and one
second per applied write, so label lag and timestamps are deterministic.

Unverified (guessed, never seen live): see the ``UNVERIFIED`` constant at the bottom of this module.
"""

from __future__ import annotations

import base64
import copy
import email.parser
import email.policy
import http.client
import json
import re
import socket
import struct
import threading
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta, timezone
from email.utils import format_datetime
from typing import Any
from urllib.parse import parse_qs, unquote, urlsplit

from tests.scripted_server import Conn, Request, ScriptedServer, response_bytes

ME_ACCOUNT = "acct-0001"
OTHER_ACCOUNT = "acct-0002"
STRANGER_ACCOUNT = "acct-0003"
SITE_EMAIL = "fake.user@example.test"
SITE_TOKEN = "fake-token-0123456789"
PROJECT = "DEMO"
# The site's local time zone is a fixed offset (no daylight saving) so goldens are stable.
LOCAL_TZ = timezone(timedelta(hours=-6))

F_POINTS = "customfield_90001"  # legacy float "Story Points"
F_ESTIMATE = "customfield_90002"  # jsw-story-points, no data
F_SPRINT = "customfield_90003"  # gh-sprint
F_CRITERIA = "customfield_90004"  # rich text, found by name only
F_VALUE = "customfield_90005"  # another legacy float field, other name
F_EFFORT = "customfield_90006"  # and a third
# Float custom fields besides "Story Points": the ``:float`` schema is shared, only names differ.
EXTRA_FLOATS = {F_VALUE: "Business value", F_EFFORT: "Effort days"}

CATEGORY = {
    "new": {"id": 2, "key": "new", "name": "To Do"},
    "indeterminate": {"id": 4, "key": "indeterminate", "name": "In Progress"},
    "done": {"id": 3, "key": "done", "name": "Done"},
}
STATUSES = {
    "Backlog": ("10", "new"),
    "In Progress": ("11", "indeterminate"),
    "Done": ("12", "done"),
}
# Global transitions: the same ids from every status (V-7).
TRANSITIONS = {
    "31": ("Start progress", "In Progress"),
    "41": ("Finish", "Done"),
    "51": ("Back to backlog", "Backlog"),
}
RESOLUTIONS = ["Done", "Won't Do", "Duplicate", "Cannot Reproduce", "Declined"]
PRIORITIES = ["Highest", "High", "Medium", "Low", "Lowest"]
ISSUE_TYPES = {
    "1": ("Task", False),
    "2": ("Bug", False),
    "3": ("Sub-task", True),
    "4": ("Epic", False),
}

NOT_SETTABLE = "Field '{name}' cannot be set. It is not on the appropriate screen, or unknown."
COMMENT_BODY_LIMIT = 32768
ATTACHMENT_LIMIT = 10 * 1024 * 1024  # the provider's default upload limit (the fake enforces it)
PROPERTY_VALUE_LIMIT = 32768
LABEL_LIMIT = 255
EMBEDDED_COMMENTS = 20  # V-25


# --------------------------------------------------------------------------------------------
# Quirks and faults
# --------------------------------------------------------------------------------------------


ERROR_SHAPES = {None, "messages", "error", "text"}
RETRY_AFTER_FORMATS = {"seconds", "http_date", "junk", "none"}
FAULT_KINDS = {"reset", "close", "headers_only", "truncated", "status", "slow", "drip"}


@dataclass
class Quirks:
    """Switches for the provider's behaviors. The defaults are the verified (real) behavior."""

    # V-3: a semantic JQL error (unknown field, status value or function) is a 200 with no issues.
    # Off: it is a 400 like a syntax error (not real behavior; for contrast tests only).
    silent_semantic_jql: bool = True
    # V-2: with all fields a page silently holds at most this many issues (None: no cap).
    all_fields_page_cap: int | None = 100
    # V-2: the old ``POST /search`` answers 410.
    old_search_gone: bool = True
    # V-7: a done transition without a resolution is accepted. Off: a 400 on ``resolution``.
    done_without_resolution: bool = True
    # V-8, V-30: a transition out of the backlog (and a sub-task's done transition) is a 400
    # until the ticket has an assignee; the transition list and editmeta never show it.
    assignee_validator: bool = True
    validator_message: str = "Assignee is required to progress this issue."  # locale dependent
    # V-13: timestamps carry the user's offset without a colon (-0600). Off: with a colon.
    colonless_offsets: bool = True
    # ADF variety: descriptions and comment bodies hold every node and mark the fake knows.
    adf_variety: bool = False
    # V-16: the body shape of errors: None (natural per status), "messages", "error" or "text".
    error_shape: str | None = None
    # V-15: rate-limit headers on every response; the remaining count is pinned when not None.
    rate_limit_headers: bool = True
    rate_limit_limit: int = 1000
    rate_limit_remaining: int | None = None
    # How a 429 renders Retry-After: "seconds", "http_date", "junk" or "none". V-15: no
    # Retry-After was ever seen, so the default sends none; a test opts in to a format.
    retry_after_format: str = "none"
    # V-20, V-33: a label added by update or create is JQL-visible only this many seconds after
    # the write completes (verified about 0.6 to 1 s; 1.0 on the fake clock spans one tick).
    # ``label_lag_s=0`` opts out: the label is searchable at once.
    label_lag_s: float = 1.0
    # Sleep before every response (a slow provider).
    response_delay_s: float = 0.0
    # V-4: a read with all fields (or no ``fields``) carries this many extra field keys with
    # non-null values (23 real fields plus 532 filler keys make the verified 555 keys, about 31 KB).
    all_fields_filler: int = 532
    # UNVERIFIED (W24): the edit screen lists ``resolution``. Off (the default) it is absent from
    # editmeta and a write of it is a 400, as in a workflow that keeps it off the screen.
    resolution_on_edit_screen: bool = False
    # With the screen on: editmeta lists ``allowedValues`` (UNVERIFIED shape). Off: it does not.
    resolution_allowed_values: bool = True
    # V-18: ``notifyUsers=false`` is a 403 for a non-admin.
    notify_users_false_forbidden: bool = True
    # V-1: the credentials the fake accepts (email, token); None accepts any Basic header.
    credentials: tuple[str, str] | None = (SITE_EMAIL, SITE_TOKEN)

    def __post_init__(self) -> None:
        if self.error_shape not in ERROR_SHAPES:
            raise ValueError(f"unknown error_shape {self.error_shape!r}")
        if self.retry_after_format not in RETRY_AFTER_FORMATS:
            raise ValueError(f"unknown retry_after_format {self.retry_after_format!r}")


@dataclass
class Fault:
    """One injected failure for the next ``times`` matching requests (``None``: every one).

    ``kind``:
    - ``reset``: a TCP reset with no response byte (``after_apply``: the write lands first).
    - ``close``: a clean close with no response byte (same ``after_apply`` rule).
    - ``headers_only``: the status line and headers (Content-Length of the real body, or 2 for an
      empty one), no body, then close; the write has landed.
    - ``truncated``: headers and half the body (one byte of an empty one that advertises 2), then
      close; the write has landed.
    - ``status``: the given ``status`` (a 5xx or 429) and text ``body``; nothing applied, or with
      ``after_apply`` the write lands first and the client still sees the status.
    - ``slow``: sleep ``delay_s`` before answering normally (``after_apply``: apply first, then
      sleep). ``close()`` wakes the sleeper, which then returns without dispatching or answering.
    - ``drip``: the normal answer, one byte per ``delay_s``.
    """

    kind: str
    method: str | None = None
    path: str | None = None  # regex searched in the request path
    times: int | None = 1
    after_apply: bool = False
    status: int = 503
    body: str = "Service Unavailable"
    delay_s: float = 0.0
    retry_after: int | str | None = None
    skip: int = 0  # let this many matching requests through first (a fault at the Nth call)
    hits: int = 0
    seen: int = 0

    def __post_init__(self) -> None:
        if self.kind not in FAULT_KINDS:
            raise ValueError(f"unknown fault kind {self.kind!r}")

    def matches(self, method: str, path: str) -> bool:
        if self.times is not None and self.hits >= self.times:
            return False
        if self.method is not None and self.method != method:
            return False
        if self.path is not None and re.search(self.path, path) is None:
            return False
        if self.seen < self.skip:
            self.seen += 1
            return False
        return True


@dataclass
class Out:
    """A handler's answer."""

    status: int = 200
    body: Any = None  # dict/list -> JSON; str -> text; None -> empty
    headers: dict[str, str] = field(default_factory=dict)
    applied: bool = False  # a write that changed state


@dataclass
class HandlerError:
    """An unexpected exception in a handler or a body parse (not a deliberate ProviderError)."""

    call: Call
    exc: BaseException
    status: int  # 400 or 500: what the fake answered
    # True only for input the fake deliberately rejects as malformed; an exception that escapes a
    # handler (even one mapped to a 400) is unexpected and fails the fixture teardown.
    client_malformed: bool = False


class ProviderError(Exception):
    """Raised by a handler to answer an error with the natural body shape for its status."""

    def __init__(
        self,
        status: int,
        messages: list[str] | None = None,
        errors: dict[str, str] | None = None,
        text: str | None = None,
    ) -> None:
        super().__init__(status)
        self.status = status
        self.messages = messages or []
        self.errors = errors or {}
        self.text = text


@dataclass
class Call:
    """One request as the fake saw it."""

    method: str
    path: str
    query: dict[str, list[str]]
    body: Any
    headers: dict[str, list[str]]
    status: int = 0
    target: str = ""  # the raw request target, as sent
    malformed: str | None = None  # why the target could not be parsed

    def one(self, name: str, default: str | None = None) -> str | None:
        values = self.query.get(name)
        return values[0] if values else default


# --------------------------------------------------------------------------------------------
# ADF fixtures
# --------------------------------------------------------------------------------------------


def _stem(token: str) -> str:
    for suffix in ("ing", "ed", "s"):
        if token.endswith(suffix) and len(token) - len(suffix) >= 3:
            return token[: -len(suffix)]
    return token


def _near(query: str, word: str) -> bool:
    """A near match of one query token against one word of the text (both lower case)."""
    return _stem(query) == _stem(word) or (len(query) >= 4 and word.startswith(query))


def _is_number(value: Any) -> bool:
    return isinstance(value, int | float) and not isinstance(value, bool)


def _adf_shape_ok(doc: Any) -> bool:
    """Every node is an object with a string type, list content and string text (iterative)."""
    stack = [doc]
    while stack:
        node = stack.pop()
        if not isinstance(node, dict) or not isinstance(node.get("type"), str):
            return False
        content = node.get("content", [])
        if not isinstance(content, list):
            return False
        if "text" in node and not isinstance(node["text"], str):
            return False
        stack.extend(content)
    return True


def adf_doc(*content: dict[str, Any]) -> dict[str, Any]:
    return {"version": 1, "type": "doc", "content": list(content)}


def adf_paragraph(text: str) -> dict[str, Any]:
    return {"type": "paragraph", "content": [{"type": "text", "text": text}]}


def adf_text(text: str) -> dict[str, Any]:
    """A one-paragraph ADF document."""
    return adf_doc(adf_paragraph(text))


def _t(text: str, *marks: dict[str, Any]) -> dict[str, Any]:
    node: dict[str, Any] = {"type": "text", "text": text}
    if marks:
        node["marks"] = list(marks)
    return node


def adf_variety_doc() -> dict[str, Any]:
    """Every node and mark kind the renderer must survive (synthetic text)."""
    cell = lambda text, kind="tableCell": {  # noqa: E731
        "type": kind,
        "attrs": {},
        "content": [adf_paragraph(text)],
    }
    return adf_doc(
        {"type": "heading", "attrs": {"level": 2}, "content": [_t("A heading")]},
        {
            "type": "paragraph",
            "content": [
                _t("bold", {"type": "strong"}),
                _t(" "),
                _t("italic", {"type": "em"}),
                _t(" "),
                _t("code", {"type": "code"}),
                _t(" "),
                _t("struck", {"type": "strike"}),
                _t(" "),
                _t("under", {"type": "underline"}),
                _t(" "),
                _t("colored", {"type": "textColor", "attrs": {"color": "#ff0000"}}),
                _t(" "),
                _t("link", {"type": "link", "attrs": {"href": "https://example.test/page"}}),
                {"type": "hardBreak"},
                {"type": "mention", "attrs": {"id": STRANGER_ACCOUNT, "text": "@Someone"}},
                {"type": "emoji", "attrs": {"shortName": ":smile:", "text": "smile"}},
                {"type": "date", "attrs": {"timestamp": "1772640000000"}},
                {"type": "status", "attrs": {"text": "READY", "color": "green"}},
                {"type": "inlineCard", "attrs": {"url": "https://example.test/card"}},
            ],
        },
        {
            "type": "bulletList",
            "content": [{"type": "listItem", "content": [adf_paragraph("bullet")]}],
        },
        {
            "type": "orderedList",
            "attrs": {"order": 1},
            "content": [{"type": "listItem", "content": [adf_paragraph("number")]}],
        },
        {
            "type": "taskList",
            "attrs": {"localId": "t1"},
            "content": [
                {
                    "type": "taskItem",
                    "attrs": {"localId": "t2", "state": "TODO"},
                    "content": [_t("task")],
                }
            ],
        },
        {
            "type": "codeBlock",
            "attrs": {"language": "text"},
            "content": [_t("line one\nline two")],
        },
        {"type": "blockquote", "content": [adf_paragraph("quoted")]},
        {"type": "rule"},
        {
            "type": "panel",
            "attrs": {"panelType": "info"},
            "content": [adf_paragraph("a panel")],
        },
        {
            "type": "expand",
            "attrs": {"title": "More"},
            "content": [adf_paragraph("hidden")],
        },
        {
            "type": "table",
            "attrs": {"isNumberColumnEnabled": False, "layout": "default"},
            "content": [
                {"type": "tableRow", "content": [cell("h", "tableHeader")]},
                {"type": "tableRow", "content": [cell("c")]},
            ],
        },
        {
            "type": "mediaSingle",
            "attrs": {"layout": "center"},
            "content": [{"type": "media", "attrs": {"id": "media-0001", "type": "file"}}],
        },
        {"type": "unknownFutureNode", "attrs": {"x": 1}, "content": [adf_paragraph("inside")]},
    )


# --------------------------------------------------------------------------------------------
# JQL (a small evaluator: AND, OR, NOT, parentheses, the fields below, ORDER BY)
# --------------------------------------------------------------------------------------------

KNOWN_FIELDS = {
    "project",
    "key",
    "issuekey",
    "status",
    "statuscategory",
    "labels",
    "assignee",
    "reporter",
    "issuetype",
    "type",
    "priority",
    "resolution",
    "summary",
    "text",
    "created",
    "updated",
    "parent",
    "sprint",
}
KNOWN_FUNCTIONS = {"currentuser", "now", "startofday", "endofday", "membersof"}
_TOKEN = re.compile(
    r"""\s*(?:
        (?P<str>"(?:[^"\\]|\\.)*"|'(?:[^'\\]|\\.)*')
      | (?P<op><=|>=|!=|!~|=|~|<|>)
      | (?P<punct>[(),])
      | (?P<word>[^\s()=!~<>,"']+)
    )""",
    re.VERBOSE,
)


class JqlSyntax(Exception):
    pass


class Bare(str):
    """An unquoted word of a JQL value. ``bad``: a text search value that V-31 answers with no
    results (an unquoted phrase, or one holding reserved characters)."""

    bad: bool = False


TEXT_RESERVED = set(":*?+&|{}[]^\\/")
DATE_FUNCTIONS = {"now", "startofday", "endofday"}


@dataclass
class Clause:
    field: str
    op: str
    values: list[Any]  # plain strings, or ("fn", name) tuples
    position: int = 0


@dataclass
class Node:
    kind: str  # "and", "or", "not", "clause"
    children: list[Node] = field(default_factory=list)
    clause: Clause | None = None


def _tokens(text: str) -> list[tuple[str, str, int]]:
    out: list[tuple[str, str, int]] = []
    pos = 0
    while pos < len(text):
        if text[pos:].strip() == "":
            break
        match = _TOKEN.match(text, pos)
        if not match or match.end() == pos:
            raise JqlSyntax(f"unexpected character at position {pos + 1}")
        kind = match.lastgroup or ""
        value = match.group(kind)
        if kind == "str":
            value = re.sub(r"\\(.)", r"\1", value[1:-1])
        out.append((kind, value, match.start(kind)))
        pos = match.end()
    return out


class _Parser:
    def __init__(self, text: str) -> None:
        self.toks = _tokens(text)
        self.i = 0

    def peek(self) -> tuple[str, str, int] | None:
        return self.toks[self.i] if self.i < len(self.toks) else None

    def word_is(self, *names: str) -> bool:
        tok = self.peek()
        return tok is not None and tok[0] == "word" and tok[1].lower() in names

    def take(self) -> tuple[str, str, int]:
        tok = self.peek()
        if tok is None:
            raise JqlSyntax("unexpected end of the query")
        self.i += 1
        return tok

    def expect_punct(self, char: str) -> None:
        tok = self.take()
        if tok[:2] != ("punct", char):
            raise JqlSyntax(f"expected '{char}' at position {tok[2] + 1}")

    def parse(self) -> tuple[Node | None, list[tuple[str, bool]]]:
        node: Node | None = None
        if self.peek() is not None and not self.word_is("order"):
            node = self.or_()
        order: list[tuple[str, bool]] = []
        if self.word_is("order"):
            self.take()
            if not self.word_is("by"):
                raise JqlSyntax("expected BY after ORDER")
            self.take()
            while True:
                name = self.take()
                if name[0] not in {"word", "str"}:
                    raise JqlSyntax("expected a field after ORDER BY")
                desc = False
                if self.word_is("asc", "desc"):
                    desc = self.take()[1].lower() == "desc"
                order.append((name[1].lower(), desc))
                if self.peek() and self.peek()[:2] == ("punct", ","):  # type: ignore[index]
                    self.take()
                    continue
                break
        if self.peek() is not None:
            raise JqlSyntax(f"unexpected '{self.peek()[1]}' at position {self.peek()[2] + 1}")  # type: ignore[index]
        return node, order

    def or_(self) -> Node:
        parts = [self.and_()]
        while self.word_is("or"):
            self.take()
            parts.append(self.and_())
        return parts[0] if len(parts) == 1 else Node("or", parts)

    def and_(self) -> Node:
        parts = [self.not_()]
        while self.word_is("and"):
            self.take()
            parts.append(self.not_())
        return parts[0] if len(parts) == 1 else Node("and", parts)

    def not_(self) -> Node:
        if self.word_is("not"):
            self.take()
            return Node("not", [self.not_()])
        tok = self.peek()
        if tok and tok[:2] == ("punct", "("):
            self.take()
            inner = self.or_()
            self.expect_punct(")")
            return inner
        return Node("clause", clause=self.clause())

    def value(self) -> Any:
        tok = self.take()
        if tok[0] == "str":
            return tok[1]
        if tok[0] != "word":
            raise JqlSyntax(f"expected a value at position {tok[2] + 1}")
        nxt = self.peek()
        if nxt and nxt[:2] == ("punct", "("):
            self.take()
            while not (self.peek() and self.peek()[:2] == ("punct", ")")):  # type: ignore[index]
                self.take()
            self.take()
            return ("fn", tok[1])
        return Bare(tok[1])

    def clause(self) -> Clause:
        name = self.take()
        if name[0] not in {"word", "str"}:
            raise JqlSyntax(f"expected a field at position {name[2] + 1}")
        tok = self.take()
        op = tok[1].lower()
        if tok[0] == "word" and op == "not":
            nxt = self.take()
            if nxt[1].lower() != "in":
                raise JqlSyntax(f"expected IN at position {nxt[2] + 1}")
            op = "not in"
        elif tok[0] == "word" and op == "is":
            if self.word_is("not"):
                self.take()
                op = "is not"
        elif not (tok[0] == "op" or (tok[0] == "word" and op == "in")):
            raise JqlSyntax(f"expected an operator at position {tok[2] + 1}")
        if op in {"in", "not in"}:
            self.expect_punct("(")
            values = [self.value()]
            while self.peek() and self.peek()[:2] == ("punct", ","):  # type: ignore[index]
                self.take()
                values.append(self.value())
            self.expect_punct(")")
        else:
            values = [self.value()]
            if op in {"~", "!~"} and isinstance(values[0], Bare):
                words = [str(values[0])]
                while (
                    self.peek() is not None
                    and self.peek()[0] == "word"  # type: ignore[index]
                    and not self.word_is("and", "or", "not", "order")
                ):
                    words.append(self.take()[1])
                phrase = Bare(" ".join(words))
                phrase.bad = (
                    len(words) > 1
                    or any(ch in TEXT_RESERVED for ch in phrase)
                    or phrase.startswith("-")
                )
                values = [phrase]
        return Clause(name[1].lower(), op, values, name[2])


def parse_jql(text: str) -> tuple[Node | None, list[tuple[str, bool]]]:
    try:
        return _Parser(text).parse()
    except RecursionError:
        raise JqlSyntax("the query is nested too deeply") from None


def _walk(node: Node | None) -> list[Clause]:
    if node is None:
        return []
    if node.clause is not None:
        return [node.clause]
    return [c for child in node.children for c in _walk(child)]


# --------------------------------------------------------------------------------------------
# The fake
# --------------------------------------------------------------------------------------------


@dataclass
class Ticket:
    id: int
    key: str
    type_id: str
    summary: str
    status: str = "Backlog"
    resolution: str | None = None
    assignee: str | None = None
    reporter: str = ME_ACCOUNT
    priority: str | None = "Medium"
    description: dict[str, Any] | None = None
    # label -> time it becomes JQL-visible
    labels: dict[str, datetime] = field(default_factory=dict)
    parent: str | None = None
    created: datetime = datetime(2026, 3, 1, 15, 0, tzinfo=UTC)
    updated: datetime = datetime(2026, 3, 1, 15, 0, tzinfo=UTC)
    comments: list[dict[str, Any]] = field(default_factory=list)
    # {id, filename, size, mimeType, author, _created, _content}; ``attach`` adds one
    attachments: list[dict[str, Any]] = field(default_factory=list)
    sprints: list[int] = field(default_factory=list)
    links: list[dict[str, Any]] = field(default_factory=list)
    points: float | None = None
    criteria: dict[str, Any] | None = None
    due: str | None = None
    extra: dict[str, Any] = field(default_factory=dict)  # values of the EXTRA_FLOATS fields
    properties: dict[str, Any] = field(default_factory=dict)  # issue properties
    # raw changelog entries, oldest first: {id, author, _created, items}; see ``add_history``
    history: list[dict[str, Any]] = field(default_factory=list)


SPRINTS: dict[int, dict[str, Any]] = {
    1: {
        "name": "Sprint one",
        "state": "closed",
        "start": "2026-02-02T15:00:00.000Z",
        "end": "2026-02-16T15:00:00.000Z",
        "board": 1,
    },
    2: {
        "name": "Sprint two",
        "state": "active",
        "start": "2026-02-16T15:00:00.000Z",
        "end": "2026-03-02T15:00:00.000Z",
        "board": 1,
    },
    3: {"name": "Sprint three", "state": "future", "start": None, "end": None, "board": 1},
}
USERS = {
    ME_ACCOUNT: "Fake Me",
    OTHER_ACCOUNT: "Fake Other",
    STRANGER_ACCOUNT: "Fake Stranger",
}


class FakeProvider:
    """The fake provider server. Use as a context manager; see ``quirks`` and ``inject``."""

    def __init__(self, quirks: Quirks | None = None) -> None:
        self.quirks = quirks or Quirks()
        self.faults: list[Fault] = []
        self.calls: list[Call] = []
        self.applied: list[Call] = []  # writes that changed state, in order
        # Unexpected exceptions of a handler or body parse (answered 400 or 500, never a drop).
        self.errors: list[HandlerError] = []
        # A test that provokes a 500-class internal error on purpose sets this; the ``fake``
        # fixture otherwise fails the test at teardown when ``errors`` holds one.
        self.allow_internal_errors = False
        self.tickets: dict[str, Ticket] = {}
        self.moved: dict[str, str] = {}  # old key (upper case) -> current key
        # Hostile or changing metadata lists: a non-None value replaces the whole answer body of
        # that list endpoint (any JSON value); ``extra_fields`` are appended to the field list.
        self.field_list_body: Any = None
        self.priority_list_body: Any = None
        self.resolution_list_body: Any = None
        self.extra_fields: list[Any] = []
        self.sprints = copy.deepcopy(SPRINTS)
        self.now = datetime(2026, 3, 4, 15, 0, tzinfo=UTC)
        self._next_id = 10000
        self._next_comment = 20000
        self._next_attachment = 30000
        self._next_link = 30000
        self._lock = threading.RLock()
        self._wake = threading.Event()
        self._served = 0
        self._server = ScriptedServer(self._behavior, backlog=128)
        self._routes: list[tuple[str, re.Pattern[str], Callable[..., Out]]] = []
        self._build_routes()
        self.seed()

    # lifecycle ---------------------------------------------------------------------------
    def start(self) -> FakeProvider:
        self._server.start()
        return self

    def close(self) -> None:
        self._wake.set()
        self._server.close()

    def __enter__(self) -> FakeProvider:
        return self.start()

    def __exit__(self, *exc: object) -> None:
        self.close()

    @property
    def url(self) -> str:
        return self._server.url

    @property
    def port(self) -> int:
        return self._server.port

    def basic_header(self, email: str = SITE_EMAIL, token: str = SITE_TOKEN) -> str:
        return "Basic " + base64.b64encode(f"{email}:{token}".encode()).decode()

    def inject(self, fault: Fault) -> Fault:
        with self._lock:
            self.faults.append(fault)
        return fault

    def advance(self, seconds: float) -> None:
        with self._lock:
            self.now += timedelta(seconds=seconds)

    # state helpers (tests build their world through these) -----------------------------------
    def seed(self) -> None:
        """The default world: three tickets in ``DEMO``."""
        self.add_ticket("DEMO-1", "Seeded backlog task", labels=["alpha"], assignee=None)
        self.add_ticket(
            "DEMO-2",
            "Seeded task in progress",
            status="In Progress",
            assignee=ME_ACCOUNT,
            sprints=[2],
            points=3.0,
        )
        self.add_ticket(
            "DEMO-3",
            "Seeded finished bug",
            type_id="2",
            status="Done",
            resolution="Done",
            assignee=OTHER_ACCOUNT,
            sprints=[1],
        )

    def add_ticket(self, key: str, summary: str, **kw: Any) -> Ticket:
        with self._lock:
            labels = kw.pop("labels", [])
            ticket = Ticket(
                id=self._next_id,
                key=key,
                type_id=kw.pop("type_id", "1"),
                summary=summary,
                created=kw.pop("created", self.now),
                updated=kw.pop("updated", self.now),
                **kw,
            )
            self._next_id += 1
            ticket.labels = {label: self.now for label in labels}
            self.tickets[key] = ticket
            return ticket

    def move(self, old_key: str, new_key: str) -> Ticket:
        """Rename a ticket as a project move does: the old key still resolves, to the new one."""
        with self._lock:
            ticket = self.tickets.pop(old_key)
            ticket.key = new_key
            self.tickets[new_key] = ticket
            self.moved[old_key.upper()] = new_key
            return ticket

    def next_key(self, project: str = PROJECT) -> str:
        numbers = [int(k.split("-")[1]) for k in self.tickets if k.startswith(project + "-")] or [0]
        return f"{project}-{max(numbers) + 1}"

    # a tiny client for tests -----------------------------------------------------------------
    def request(
        self,
        method: str,
        path: str,
        body: Any = None,
        *,
        headers: dict[str, str] | None = None,
        auth: bool | str = True,
        timeout: float = 10,
    ) -> tuple[int, dict[str, str], bytes]:
        """One request over a fresh loopback connection (never through a proxy)."""
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=timeout)
        sent = {"Accept": "application/json", "Connection": "close"}
        if auth is True:
            sent["Authorization"] = self.basic_header()
        elif isinstance(auth, str):
            sent["Authorization"] = auth
        sent.update(headers or {})
        payload = None
        if body is not None:
            payload = body if isinstance(body, bytes) else json.dumps(body).encode()
            sent["Content-Type"] = "application/json"
        try:
            conn.request(method, path, body=payload, headers=sent)
            resp = conn.getresponse()
            data = resp.read()
            return resp.status, {k.lower(): v for k, v in resp.getheaders()}, data
        finally:
            conn.close()

    def json(self, method: str, path: str, body: Any = None, **kw: Any) -> tuple[int, Any]:
        status, _, data = self.request(method, path, body, **kw)
        return status, (json.loads(data) if data else None)

    # time formats ----------------------------------------------------------------------------
    def stamp(self, when: datetime) -> str:
        local = when.astimezone(LOCAL_TZ)
        text = local.strftime("%Y-%m-%dT%H:%M:%S.000%z")
        return text if self.quirks.colonless_offsets else text[:-2] + ":" + text[-2:]

    @staticmethod
    def utc_stamp(when: datetime) -> str:
        return when.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S.000Z")

    def _tick(self) -> datetime:
        self.now += timedelta(seconds=1)
        return self.now

    # request handling ------------------------------------------------------------------------
    @staticmethod
    def _route_path(raw: str) -> str:
        """Split on ``/`` first, then unquote each segment, so ``%2F`` never becomes a separator."""
        return "/".join(unquote(part).replace("/", "%2F") for part in raw.split("/"))

    def _make_call(self, req: Request) -> Call:
        malformed: str | None = None
        path = req.target
        query: dict[str, list[str]] = {}
        try:
            split = urlsplit(req.target)
            path = self._route_path(split.path)
            query = parse_qs(split.query, keep_blank_values=True)
        except ValueError as exc:
            malformed = str(exc)
        body: Any
        try:
            if (req.header("content-type") or "").lower().startswith("multipart/form-data"):
                body = self._parse_multipart(req)
            else:
                body = json.loads(req.body) if req.body else None
        except (ValueError, RecursionError):  # bad JSON, a digit-limit number, nesting too deep
            body = ("invalid", req.body)
        return Call(
            req.method, path, query, body, req.headers, target=req.target, malformed=malformed
        )

    @staticmethod
    def _parse_multipart(req: Request) -> dict[str, Any]:
        """``{"_multipart": [{name, filename, content_type, content}]}`` (bytes content)."""
        head = f"Content-Type: {req.header('content-type')}\r\nMIME-Version: 1.0\r\n\r\n"
        msg = email.parser.BytesParser(policy=email.policy.HTTP).parsebytes(
            head.encode() + req.body
        )
        if not msg.is_multipart():
            raise ValueError("not a multipart body")
        parts = []
        for part in msg.iter_parts():
            parts.append(
                {
                    "name": part.get_param("name", header="content-disposition"),
                    "filename": part.get_filename(),
                    "content_type": part.get_content_type(),
                    "content": part.get_payload(decode=True) or b"",
                }
            )
        return {"_multipart": parts}

    def _behavior(self, conn: Conn) -> None:
        req = conn.request
        call = self._make_call(req)
        with self._lock:
            self.calls.append(call)
            fault = next((f for f in self.faults if f.matches(req.method, call.path)), None)
            if fault is not None:
                fault.hits += 1
        kind = fault.kind if fault is not None else None
        after = fault is not None and fault.after_apply
        if kind == "slow" and not after:
            assert fault is not None
            if self._wake.wait(fault.delay_s):
                return  # closed while sleeping: do not dispatch
        elif kind != "slow" and self.quirks.response_delay_s:
            if self._wake.wait(self.quirks.response_delay_s):
                return
        if kind in {"reset", "close"} and not after:
            self._cut(conn, kind)
            return
        if kind == "status" and not after:
            assert fault is not None
            self._send_status(conn, call, fault)
            return
        with self._lock:
            out = self._dispatch(call)
            if out.applied:
                self.applied.append(call)
                self._tick()
        call.status = out.status
        if kind == "slow" and after:
            assert fault is not None
            if self._wake.wait(fault.delay_s):
                return
        if kind in {"reset", "close"}:
            assert kind is not None
            self._cut(conn, kind)
            return
        if kind == "status":
            assert fault is not None
            self._send_status(conn, call, fault)
            return
        status, headers, data = self._parts(call, out)
        if kind == "headers_only":
            # An empty body (a 204) still advertises bytes that never come.
            conn.send(response_bytes(status, b"", headers, content_length=len(data) or 2))
        elif kind == "truncated":
            body = data[: len(data) // 2] if data else b"{"
            conn.send(response_bytes(status, body, headers, content_length=len(data) or 2))
        elif kind == "drip":
            assert fault is not None
            raw = response_bytes(status, data, headers)
            for i in range(len(raw)):
                conn.send(raw[i : i + 1])
                if self._wake.wait(fault.delay_s):
                    return
        else:
            conn.send(response_bytes(status, data, headers))

    def _send_status(self, conn: Conn, call: Call, fault: Fault) -> None:
        call.status = fault.status
        headers = {"Content-Type": "text/plain"}
        if fault.status == 429 or fault.retry_after is not None:
            headers.update(self._retry_after(fault.retry_after))
        headers.update(self._rate_headers())
        conn.send(response_bytes(fault.status, fault.body, headers))

    @staticmethod
    def _cut(conn: Conn, kind: str) -> None:
        if kind == "reset":
            # SO_LINGER with a zero timeout makes close() send a RST instead of a FIN.
            conn.sock.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, struct.pack("ii", 1, 0))

    def _rate_headers(self) -> dict[str, str]:
        q = self.quirks
        if not q.rate_limit_headers:
            return {}
        remaining = (
            q.rate_limit_remaining
            if q.rate_limit_remaining is not None
            else max(0, q.rate_limit_limit - len(self.calls))
        )
        return {
            "X-RateLimit-Limit": str(q.rate_limit_limit),
            "X-RateLimit-Remaining": str(remaining),
            "Ratelimit": f"limit={q.rate_limit_limit}, remaining={remaining}, reset=30",
            "Ratelimit-Policy": f"{q.rate_limit_limit};w=60",
        }

    def _retry_after(self, value: int | str | None) -> dict[str, str]:
        seconds = 5 if value is None else value
        fmt = self.quirks.retry_after_format
        if fmt == "none":
            return {}
        if fmt == "http_date":
            when = self.now + timedelta(seconds=int(seconds))
            return {"Retry-After": format_datetime(when, usegmt=True)}
        if fmt == "junk":
            return {"Retry-After": "soon"}
        return {"Retry-After": str(seconds)}

    def _parts(self, call: Call, out: Out) -> tuple[int, dict[str, str], bytes]:
        headers = dict(out.headers)
        body = out.body
        try:
            if isinstance(body, bytes):
                headers.setdefault("Content-Type", "application/octet-stream")
                data: bytes = body
            elif isinstance(body, str):
                headers.setdefault("Content-Type", "text/plain;charset=UTF-8")
                data = body.encode()
            elif body is None:
                data = b""
            else:
                headers.setdefault("Content-Type", "application/json;charset=UTF-8")
                data = json.dumps(body).encode()
        except (ValueError, TypeError, RecursionError) as exc:  # a body that cannot be rendered
            self.errors.append(HandlerError(call, exc, 500))
            out = Out(500, "Internal Server Error")
            headers = {"Content-Type": "text/plain;charset=UTF-8"}
            data = b"Internal Server Error"
        headers.update(self._rate_headers())
        return out.status, headers, data

    # errors ----------------------------------------------------------------------------------
    def _error_out(self, err: ProviderError) -> Out:
        shape = self.quirks.error_shape
        if shape is None:
            shape = "text" if err.status == 401 else "messages"
            if err.text is not None:
                shape = "text"
        if shape == "text":
            text = err.text or "; ".join([*err.messages, *err.errors.values()]) or "error"
            return Out(err.status, text)
        if shape == "error":
            message = (
                err.messages[0]
                if err.messages
                else (next(iter(err.errors.values()), err.text or "error"))
            )
            return Out(err.status, {"error": message})
        messages = err.messages or ([err.text] if err.text else [])
        return Out(err.status, {"errorMessages": messages, "errors": err.errors})

    def _dispatch(self, call: Call) -> Out:
        try:
            self._authenticate(call)
            if call.malformed is not None:
                raise ProviderError(400, ["The request target is malformed."])
            for method, pattern, handler in self._routes:
                match = pattern.fullmatch(call.path)
                if match and method == call.method:
                    return handler(call, *match.groups())
            if any(p.fullmatch(call.path) for _, p, _ in self._routes):
                raise ProviderError(405, text="Method Not Allowed")
            raise ProviderError(404, ["The requested resource was not found."])
        except ProviderError as err:
            return self._error_out(err)
        except Exception as exc:  # noqa: BLE001 - nothing may drop the connection
            # A ValueError or RecursionError is answered as a 400 (malformed input), anything else
            # as a 500; either way it escaped a handler, so it is recorded as unexpected.
            status = 400 if isinstance(exc, ValueError | RecursionError) else 500
            self.errors.append(HandlerError(call, exc, status))
            message = "Malformed request." if status == 400 else "Internal Server Error"
            return self._error_out(ProviderError(status, [message]))

    def _authenticate(self, call: Call) -> None:
        header = (call.headers.get("authorization") or [""])[0]
        scheme, _, value = header.partition(" ")
        if scheme.lower() == "bearer":
            # V-1: a bearer header with an API token is refused with a 403.
            raise ProviderError(403, ["Bearer tokens are not accepted for this API."])
        if scheme.lower() != "basic":
            raise ProviderError(401, text="Client must be authenticated to access this resource.")
        try:
            decoded = base64.b64decode(value, validate=True).decode()
        except ValueError:  # bad base64, non-ASCII characters, bytes that are not UTF-8
            raise ProviderError(
                401, text="Client must be authenticated to access this resource."
            ) from None
        creds = self.quirks.credentials
        if creds is not None and decoded != f"{creds[0]}:{creds[1]}":
            raise ProviderError(401, text="Client must be authenticated to access this resource.")

    # routes ----------------------------------------------------------------------------------
    def _route(self, method: str, pattern: str, handler: Callable[..., Out]) -> None:
        self._routes.append((method, re.compile(pattern), handler))

    def _build_routes(self) -> None:
        api = r"/rest/api/3"
        key = r"([^/]+)"
        r = self._route
        r("GET", api + "/myself", self._myself)
        r("GET", api + "/field", self._fields)
        r("GET", api + "/resolution", self._resolutions)
        r("GET", api + "/priority", self._priorities)
        r("POST", api + "/search", self._old_search)
        r("POST", api + "/search/jql", self._search)
        r("POST", api + "/search/approximate-count", self._count)
        r("POST", api + "/jql/parse", self._parse)
        r("GET", api + "/issue/createmeta/([^/]+)/issuetypes", self._createmeta_types)
        r("GET", api + "/issue/createmeta/([^/]+)/issuetypes/([^/]+)", self._createmeta_fields)
        r("POST", api + "/issue", self._create)
        r("GET", api + f"/issue/{key}", self._get_issue)
        r("PUT", api + f"/issue/{key}", self._edit)
        r("GET", api + f"/issue/{key}/editmeta", self._editmeta)
        r("PUT", api + f"/issue/{key}/assignee", self._assign)
        r("GET", api + f"/issue/{key}/transitions", self._transitions)
        r("POST", api + f"/issue/{key}/transitions", self._transition)
        r("GET", api + f"/issue/{key}/comment", self._comments)
        r("POST", api + f"/issue/{key}/comment", self._add_comment)
        r("GET", api + "/issueLinkType", self._link_types)
        r("POST", api + "/issueLink", self._add_link)
        r("DELETE", api + r"/issueLink/([^/]+)", self._delete_link)
        r("GET", api + f"/issue/{key}/comment/([^/]+)", self._one_comment)
        r("PUT", api + f"/issue/{key}/comment/([^/]+)", self._one_comment)
        r("DELETE", api + f"/issue/{key}/comment/([^/]+)", self._one_comment)
        r("POST", api + f"/issue/{key}/attachments", self._add_attachment)
        r("GET", api + r"/attachment/content/([^/]+)", self._attachment_content)
        r("GET", api + r"/attachment/([^/]+)", self._attachment_meta)
        r("GET", api + f"/issue/{key}/changelog", self._changelog)
        r("GET", api + f"/issue/{key}/properties/([^/]+)", self._issue_property)
        r("PUT", api + f"/issue/{key}/properties/([^/]+)", self._issue_property)
        comment_props = api + f"/issue/{key}/comment/([^/]+)/properties/([^/]+)"
        r("GET", comment_props, self._comment_property)
        r("PUT", comment_props, self._comment_property)
        agile = r"/rest/agile/1.0"
        r("GET", agile + "/board", self._boards)
        r("GET", agile + r"/board/(\d+)/sprint", self._board_sprints)
        r("GET", agile + r"/sprint/(\d+)", self._sprint)
        r("POST", agile + r"/sprint/(\d+)/issue", self._sprint_add)
        r("POST", agile + "/backlog/issue", self._backlog)

    # lookups ---------------------------------------------------------------------------------
    def _find(self, key: str) -> Ticket | None:
        """By key (any case), by numeric id, or by a moved ticket's old key."""
        upper = key.upper()
        found = self.tickets.get(upper)
        if found is None and key.isascii() and key.isdigit():
            found = next((t for t in self.tickets.values() if str(t.id) == key), None)
        if found is None and upper in self.moved:
            found = self.tickets.get(self.moved[upper])
        return found

    def _ticket(self, key: str) -> Ticket:
        ticket = self._find(key)
        # V-16: a missing, malformed or hidden key is the same 404 with the same text.
        if ticket is None:
            raise ProviderError(
                404, ["Issue does not exist or you do not have permission to see it."]
            )
        return ticket

    @staticmethod
    def _int_param(call: Call, name: str, default: int) -> int:
        """A non-negative integer query parameter; anything else is a 400."""
        raw = call.one(name)
        if raw is None or raw == "":
            return default
        if not (raw.isascii() and raw.isdigit()) or len(raw) > 9:
            raise ProviderError(400, errors={name: f"The value '{raw}' is not valid for {name}."})
        return int(raw)

    def _need_body(self, call: Call) -> dict[str, Any]:
        if not isinstance(call.body, dict):
            raise ProviderError(400, ["Request body must be a JSON object."])
        return call.body

    def _user(self, account: str | None) -> dict[str, Any] | None:
        if account is None:
            return None
        return {
            "accountId": account,
            "displayName": USERS.get(account, "Fake User"),
            "active": True,
            "accountType": "atlassian",
        }

    def _status_obj(self, name: str) -> dict[str, Any]:
        sid, cat = STATUSES[name]
        return {"id": sid, "name": name, "statusCategory": dict(CATEGORY[cat])}

    def _sprint_obj(self, sid: int) -> dict[str, Any]:
        sp = self.sprints[sid]
        out: dict[str, Any] = {
            "id": sid,
            "name": sp["name"],
            "state": sp["state"],
            "boardId": sp["board"],
            "goal": "",
        }
        if sp["start"]:
            out["startDate"] = sp["start"]
            out["endDate"] = sp["end"]
        return out

    def _body_doc(
        self, doc: dict[str, Any] | None, *, force: bool = False
    ) -> dict[str, Any] | None:
        if self.quirks.adf_variety and (doc is not None or force):
            return adf_variety_doc()
        return doc

    # issue rendering -------------------------------------------------------------------------
    def _comment_obj(self, c: dict[str, Any], *, props: bool) -> dict[str, Any]:
        out = {k: v for k, v in c.items() if k not in {"_created", "_updated", "properties"}}
        out["body"] = self._body_doc(c["body"])
        out["created"] = self.stamp(c["_created"])
        out["updated"] = self.stamp(c["_updated"])
        if props and "properties" in c and c["properties"] is not None:
            raw = c["properties"]
            out["properties"] = (
                [{"key": k, "value": v} for k, v in raw.items()] if isinstance(raw, dict) else raw
            )
        return out

    def _all_fields(self, t: Ticket) -> dict[str, Any]:
        itype = ISSUE_TYPES[t.type_id]
        fields: dict[str, Any] = {
            "summary": t.summary,
            "description": self._body_doc(t.description, force=True),
            "issuetype": {"id": t.type_id, "name": itype[0], "subtask": itype[1]},
            "project": {"key": t.key.split("-")[0], "name": "Demo project"},
            "status": self._status_obj(t.status),
            "resolution": {"name": t.resolution} if t.resolution else None,
            "assignee": self._user(t.assignee),
            "reporter": self._user(t.reporter),
            "priority": {"name": t.priority} if t.priority else None,
            "labels": sorted(t.labels),
            "created": self.stamp(t.created),
            "updated": self.stamp(t.updated),
            "duedate": t.due,
            "parent": self._parent(t),
            "issuelinks": t.links,
            "comment": {
                "comments": [
                    self._comment_obj(c, props=False) for c in t.comments[:EMBEDDED_COMMENTS]
                ],
                "total": len(t.comments),
                "maxResults": EMBEDDED_COMMENTS,
                "startAt": 0,
            },
            "attachment": [self._attachment_obj(a) for a in t.attachments],
            F_POINTS: t.points,
            F_ESTIMATE: None,
            F_SPRINT: [self._sprint_obj(s) for s in t.sprints] or None,
            F_CRITERIA: t.criteria,
        }
        for fid in EXTRA_FLOATS:
            fields[fid] = t.extra.get(fid)
        if t.status in ("Done",):
            fields["resolutiondate"] = self.stamp(t.updated)
        return fields

    def _parent(self, t: Ticket) -> dict[str, Any] | None:
        if t.parent is None:
            return None
        p = self.tickets.get(t.parent)
        if p is None:
            return {"key": t.parent}
        return {
            "id": str(p.id),
            "key": p.key,
            "fields": {
                "summary": p.summary,
                "issuetype": {"name": ISSUE_TYPES[p.type_id][0]},
                "status": self._status_obj(p.status),
            },
        }

    def _issue_json(
        self, t: Ticket, wanted: list[str] | None, *, default_all: bool
    ) -> dict[str, Any]:
        full = self._all_fields(t)
        names = wanted
        if names is None:
            names = ["*all"] if default_all else []
        fields: dict[str, Any] = {}
        if "*all" in names:
            fields = dict(full)
            for n in range(self.quirks.all_fields_filler):
                fields["customfield_" + str(80000 + n)] = f"filler value {n:04d} " + "x" * 13
        elif "*navigable" in names:
            fields = dict(full)
        else:
            for name in names:
                if name in full:
                    fields[name] = full[name]
        return {
            "expand": "operations,versionedRepresentations,editmeta,changelog,renderedFields",
            "id": str(t.id),
            "self": f"{self.url}/rest/api/3/issue/{t.id}",
            "key": t.key,
            "fields": fields,
        }

    @staticmethod
    def _field_list(call: Call) -> list[str] | None:
        values = call.query.get("fields")
        if not values:
            return None
        return [p.strip() for v in values for p in v.split(",") if p.strip()]

    # handlers --------------------------------------------------------------------------------
    def _myself(self, call: Call) -> Out:
        return Out(
            200,
            {
                "self": f"{self.url}/rest/api/3/user?accountId={ME_ACCOUNT}",
                "accountId": ME_ACCOUNT,
                "emailAddress": SITE_EMAIL,
                "displayName": USERS[ME_ACCOUNT],
                "active": True,
                "timeZone": "America/Chicago",
                "accountType": "atlassian",
            },
        )

    def _fields(self, call: Call) -> Out:
        def f(fid: str, name: str, schema: dict[str, Any], custom: bool = False) -> dict[str, Any]:
            return {
                "id": fid,
                "key": fid,
                "name": name,
                "custom": custom,
                "orderable": True,
                "navigable": True,
                "searchable": True,
                "schema": schema,
            }

        sys_fields = [
            f("summary", "Summary", {"type": "string", "system": "summary"}),
            f("description", "Description", {"type": "string", "system": "description"}),
            f("status", "Status", {"type": "status", "system": "status"}),
            f("labels", "Labels", {"type": "array", "items": "string", "system": "labels"}),
            f("assignee", "Assignee", {"type": "user", "system": "assignee"}),
            f("reporter", "Reporter", {"type": "user", "system": "reporter"}),
            f("priority", "Priority", {"type": "priority", "system": "priority"}),
            f("resolution", "Resolution", {"type": "resolution", "system": "resolution"}),
            f("parent", "Parent", {"type": "issuelink", "system": "parent"}),
            f("duedate", "Due date", {"type": "date", "system": "duedate"}),
            f(
                "issuelinks",
                "Linked Issues",
                {"type": "array", "items": "issuelinks", "system": "issuelinks"},
            ),
        ]
        ns = "com.atlassian.jira.plugin.system.customfieldtypes"
        custom = [
            f(
                F_POINTS,
                "Story Points",
                {"type": "number", "custom": f"{ns}:float", "customId": 90001},
                True,
            ),
            f(
                F_ESTIMATE,
                "Story point estimate",
                {"type": "number", "custom": f"{ns}:jsw-story-points", "customId": 90002},
                True,
            ),
            f(
                F_SPRINT,
                "Sprint",
                {
                    "type": "array",
                    "items": "json",
                    "custom": "com.pyxis.greenhopper.jira:gh-sprint",
                    "customId": 90003,
                },
                True,
            ),
            f(
                F_CRITERIA,
                "Acceptance Criteria",
                {"type": "string", "custom": f"{ns}:textarea", "customId": 90004},
                True,
            ),
        ]
        for fid, name in EXTRA_FLOATS.items():
            custom.append(
                f(
                    fid,
                    name,
                    {"type": "number", "custom": f"{ns}:float", "customId": int(fid[-5:])},
                    True,
                )
            )
        if self.field_list_body is not None:
            return Out(200, self.field_list_body)
        return Out(200, sys_fields + custom + list(self.extra_fields))

    def _resolutions(self, call: Call) -> Out:
        if self.resolution_list_body is not None:
            return Out(200, self.resolution_list_body)
        return Out(
            200,
            [{"id": str(100 + i), "name": n, "description": ""} for i, n in enumerate(RESOLUTIONS)],
        )

    def _priorities(self, call: Call) -> Out:
        if self.priority_list_body is not None:
            return Out(200, self.priority_list_body)
        return Out(200, [{"id": str(1 + i), "name": n} for i, n in enumerate(PRIORITIES)])

    def _old_search(self, call: Call) -> Out:
        if self.quirks.old_search_gone:
            raise ProviderError(
                410,
                ["The requested API has been removed. Please migrate to /rest/api/3/search/jql."],
            )
        return self._search(call)

    # -- JQL evaluation ------------------------------------------------------------------------
    def _semantic_errors(self, clauses: list[Clause], order: list[tuple[str, bool]]) -> list[str]:
        errors: list[str] = []
        for c in clauses:
            if c.field not in KNOWN_FIELDS:
                errors.append(
                    f"Field '{c.field}' does not exist or you do not have permission to view it."
                )
                continue
            for v in c.values:
                if isinstance(v, tuple):
                    if v[1].lower() not in KNOWN_FUNCTIONS:
                        errors.append(f"A function '{v[1]}' does not exist.")
                elif (
                    c.field == "status"
                    and c.op in {"=", "!=", "in", "not in"}
                    and v.lower() not in {name.lower() for name in STATUSES}
                ):
                    errors.append(f"The value '{v}' does not exist for the field 'status'.")
                elif c.field == "project" and v.upper() != PROJECT:
                    errors.append(f"The value '{v}' does not exist for the field 'project'.")
                elif isinstance(v, Bare) and v.bad and c.op in {"~", "!~"}:
                    # V-31: an unquoted phrase or reserved characters answer 200 with no results.
                    errors.append(f"The text query '{v}' must be quoted.")
        for name, _ in order:
            if name not in KNOWN_FIELDS:
                errors.append(
                    f"Field '{name}' does not exist or you do not have permission to view it."
                )
        return errors

    def _date_errors(self, clauses: list[Clause]) -> list[str]:
        """A date value that cannot be read is a 400 (never the silent empty answer)."""
        errors: list[str] = []
        for c in clauses:
            if c.field not in {"created", "updated"}:
                continue
            for v in c.values:
                if self._date_value(v) is None and not (
                    isinstance(v, tuple) and v[1].lower() not in KNOWN_FUNCTIONS
                ):
                    errors.append(f"Date value '{v if isinstance(v, str) else v[1]}' is invalid.")
        return errors

    def _values(self, t: Ticket, fld: str) -> list[str]:
        visible = [lab for lab, at in t.labels.items() if at <= self.now]
        table: dict[str, list[str | None]] = {
            "project": [t.key.split("-")[0]],
            "key": [t.key],
            "issuekey": [t.key],
            "status": [t.status],
            "statuscategory": [CATEGORY[STATUSES[t.status][1]]["name"]],
            "labels": list(visible),
            "assignee": [t.assignee],
            "reporter": [t.reporter],
            "issuetype": [ISSUE_TYPES[t.type_id][0]],
            "type": [ISSUE_TYPES[t.type_id][0]],
            "priority": [t.priority],
            "resolution": [t.resolution],
            "summary": [t.summary],
            "text": [t.summary],
            "parent": [t.parent],
            "sprint": [str(s) for s in t.sprints],
        }
        return [v for v in table.get(fld, []) if v is not None]

    def _fn_value(self, name: str) -> str:
        if name.lower() == "currentuser":
            return ME_ACCOUNT
        return ""

    def _date_value(self, value: Any) -> datetime | None:
        if isinstance(value, tuple):
            name = value[1].lower()
            local = self.now.astimezone(LOCAL_TZ)
            midnight = local.replace(hour=0, minute=0, second=0, microsecond=0)
            if name == "now":
                return self.now
            if name == "startofday":
                return midnight.astimezone(UTC)
            if name == "endofday":
                return (midnight + timedelta(days=1)).astimezone(UTC)
            return None
        return self._date(value)

    def _date(self, text: str) -> datetime | None:
        relative = re.fullmatch(r"([+-])(\d{1,6})([mhdw])", text.strip())
        if relative:
            unit = {"m": "minutes", "h": "hours", "d": "days", "w": "weeks"}[relative[3]]
            delta = timedelta(**{unit: int(relative[2])})
            return self.now + delta if relative[1] == "+" else self.now - delta
        # V-32: an absolute date is read in the searching user's time zone.
        for fmt in ("%Y-%m-%d %H:%M", "%Y/%m/%d %H:%M", "%Y-%m-%d", "%Y/%m/%d"):
            try:
                return datetime.strptime(text, fmt).replace(tzinfo=LOCAL_TZ).astimezone(UTC)
            except ValueError:
                continue
        return None

    def _match(self, t: Ticket, c: Clause) -> bool:
        wanted = [self._fn_value(v[1]) if isinstance(v, tuple) else v for v in c.values]
        if c.field in {"created", "updated"}:
            stamp = t.created if c.field == "created" else t.updated
            when = self._date_value(c.values[0]) if c.values else None
            if when is None:
                return False
            return {
                "=": stamp == when,
                "!=": stamp != when,
                ">": stamp > when,
                ">=": stamp >= when,
                "<": stamp < when,
                "<=": stamp <= when,
            }.get(c.op, False)
        have = self._values(t, c.field)
        low = [h.lower() for h in have]
        want = [w.lower() for w in wanted]
        if c.field == "key":
            want = [w.upper().lower() for w in want]
        if c.op in {"is", "is not"}:
            empty = not have
            return empty if c.op == "is" else not empty
        if c.op == "=":
            return bool(want) and want[0] in low
        if c.op == "!=":
            return not (want and want[0] in low)
        if c.op == "in":
            return any(w in low for w in want)
        if c.op == "not in":
            return not any(w in low for w in want)
        if c.op in {"~", "!~"}:
            # V-31: a tokenized phrase match: punctuation is ignored and near matches count
            # (a trailing s, ed or ing, and a prefix of 4 or more characters).
            tokens = re.findall(r"\w+", " ".join(want))
            hit = bool(tokens) and all(
                any(_near(tok, doc) for h in low for doc in re.findall(r"\w+", h)) for tok in tokens
            )
            return hit if c.op == "~" else not hit
        return False

    def _eval(self, t: Ticket, node: Node | None) -> bool:
        if node is None:
            return True
        if node.kind == "clause":
            assert node.clause is not None
            return self._match(t, node.clause)
        if node.kind == "not":
            return not self._eval(t, node.children[0])
        results = (self._eval(t, ch) for ch in node.children)
        return all(results) if node.kind == "and" else any(results)

    def _run_query(self, jql: str) -> list[Ticket]:
        node, order = parse_jql(jql)
        found = [t for t in self.tickets.values() if self._eval(t, node)]
        sort_keys = {
            "key": lambda t: (t.key.split("-")[0], int(t.key.split("-")[1])),
            "created": lambda t: t.created,
            "updated": lambda t: t.updated,
            "priority": lambda t: PRIORITIES.index(t.priority) if t.priority in PRIORITIES else 99,
        }
        found.sort(key=lambda t: (t.key.split("-")[0], int(t.key.split("-")[1])))
        for name, desc in reversed(order):
            if name in sort_keys:
                found.sort(key=sort_keys[name], reverse=desc)
        return found

    def _jql_text(self, call: Call) -> str:
        body = self._need_body(call)
        jql = body.get("jql")
        if not isinstance(jql, str):
            raise ProviderError(400, ["Request body must carry a 'jql' string."])
        return jql

    def _search(self, call: Call) -> Out:
        body = self._need_body(call)
        jql = self._jql_text(call)
        try:
            node, order = parse_jql(jql)
        except JqlSyntax as exc:
            raise ProviderError(400, [f"Error in the JQL Query: {exc}"]) from None
        dates = self._date_errors(_walk(node))
        if dates:
            raise ProviderError(400, dates)
        semantic = self._semantic_errors(_walk(node), order)
        if semantic:
            if not self.quirks.silent_semantic_jql:
                raise ProviderError(400, semantic)
            return Out(200, {"issues": []})  # V-3
        found = self._run_query(jql)
        fields = body.get("fields")
        wanted = [str(f) for f in fields] if isinstance(fields, list) else None
        all_fields = wanted is not None and any(f in {"*all", "*navigable"} for f in wanted)
        cap = 5000
        requested = body.get("maxResults", 50)
        if not isinstance(requested, int) or isinstance(requested, bool) or requested < 1:
            raise ProviderError(400, ["maxResults must be a positive integer."])
        page = min(requested, cap)
        if all_fields and self.quirks.all_fields_page_cap is not None:
            page = min(page, self.quirks.all_fields_page_cap)  # V-2: silent cap
        start = 0
        token = body.get("nextPageToken")
        if token is not None:
            try:
                prefix, _, digits = (
                    base64.urlsafe_b64decode(str(token).encode()).decode().partition(":")
                )
                if prefix != "page" or not digits.isascii() or not digits.isdigit():
                    raise ValueError
                start = int(digits)
            except ValueError:  # includes bad base64, bad UTF-8 and the int digit limit
                raise ProviderError(400, ["The 'nextPageToken' is invalid."]) from None
        chunk = found[start : start + page]
        out: dict[str, Any] = {
            "issues": [self._issue_json(t, wanted, default_all=False) for t in chunk],
        }
        if start + page < len(found):
            out["nextPageToken"] = base64.urlsafe_b64encode(
                f"page:{start + page}".encode()
            ).decode()
        return Out(200, out)

    def _count(self, call: Call) -> Out:
        jql = self._jql_text(call)
        try:
            node, order = parse_jql(jql)
        except JqlSyntax as exc:
            raise ProviderError(400, [f"Error in the JQL Query: {exc}"]) from None
        dates = self._date_errors(_walk(node))
        if dates:
            raise ProviderError(400, dates)
        if self._semantic_errors(_walk(node), order):
            return Out(200, {"count": 0})
        return Out(200, {"count": len(self._run_query(jql))})

    def _parse(self, call: Call) -> Out:
        body = self._need_body(call)
        queries = body.get("queries")
        if not isinstance(queries, list):
            raise ProviderError(400, ["'queries' must be a list."])
        mode = call.one("validation", "strict")
        results: list[dict[str, Any]] = []
        for q in queries:
            errors: list[str] = []
            structure: dict[str, Any] = {}
            try:
                node, order = parse_jql(str(q))
                errors = self._date_errors(_walk(node))
                if mode != "none":
                    errors += self._semantic_errors(_walk(node), order)
                structure = {"where": {} if node else None}
            except JqlSyntax as exc:
                errors = [f"Error in the JQL Query: {exc}"]
            results.append({"query": q, "structure": structure, "errors": errors})
        return Out(200, {"queries": results})  # V-22: always 200

    # -- issues --------------------------------------------------------------------------------
    def _get_issue(self, call: Call, key: str) -> Out:
        return Out(
            200, self._issue_json(self._ticket(key), self._field_list(call), default_all=True)
        )

    def _editable_fields(self) -> dict[str, dict[str, Any]]:
        def meta(name: str, schema: dict[str, Any], required: bool = False) -> dict[str, Any]:
            return {"required": required, "name": name, "schema": schema, "operations": ["set"]}

        ns = "com.atlassian.jira.plugin.system.customfieldtypes"
        # V-24: ``labels`` is writable but absent from the edit metadata.
        screen: dict[str, dict[str, Any]] = {}
        if self.quirks.resolution_on_edit_screen:
            screen["resolution"] = meta(
                "Resolution", {"type": "resolution", "system": "resolution"}
            )
            if self.quirks.resolution_allowed_values:
                screen["resolution"]["allowedValues"] = [
                    {"id": str(100 + i), "name": n} for i, n in enumerate(RESOLUTIONS)
                ]
        return {
            **screen,
            "summary": meta("Summary", {"type": "string", "system": "summary"}, True),
            "description": meta("Description", {"type": "string", "system": "description"}),
            "priority": meta("Priority", {"type": "priority", "system": "priority"}),
            "duedate": meta("Due date", {"type": "date", "system": "duedate"}),
            F_POINTS: meta("Story Points", {"type": "number", "custom": f"{ns}:float"}),
            F_CRITERIA: meta("Acceptance Criteria", {"type": "string", "custom": f"{ns}:textarea"}),
            **{
                fid: meta(name, {"type": "number", "custom": f"{ns}:float"})
                for fid, name in EXTRA_FLOATS.items()
            },
        }

    def _editmeta(self, call: Call, key: str) -> Out:
        self._ticket(key)
        return Out(200, {"fields": self._editable_fields()})

    def _check_labels(self, labels: list[Any]) -> list[str]:
        out: list[str] = []
        for label in labels:
            if not isinstance(label, str) or not label:
                raise ProviderError(400, errors={"labels": "A label must be a non-empty string."})
            if re.search(r"\s", label):
                # V-18: a label containing a space is a 400.
                raise ProviderError(400, errors={"labels": f"The label '{label}' contains spaces."})
            if len(label) > LABEL_LIMIT:
                raise ProviderError(400, errors={"labels": "The label is too long."})
            out.append(label)
        return out

    def _plan_fields(
        self, t: Ticket | None, fields: dict[str, Any], *, creating: bool
    ) -> dict[str, Any]:
        """Validate ``fields``; return the changes, or raise with nothing applied (V-10)."""
        changes: dict[str, Any] = {}
        editable = self._editable_fields()
        for name, value in fields.items():
            if name in {"project", "issuetype"} and creating:
                continue
            if name == "labels":
                changes["labels"] = self._check_labels(
                    value if isinstance(value, list) else [value]
                )
            elif name == "assignee":
                changes["assignee"] = self._account(value)
            elif name == "parent" and creating:
                changes["parent"] = value.get("key") if isinstance(value, dict) else None
            elif name == F_SPRINT and creating:
                continue  # V-28: silently ignored in a create body
            elif name == "reporter" and creating:
                changes["reporter"] = self._account(value)
            elif name == "resolution" and not creating and name in editable:
                chosen = value.get("name") if isinstance(value, dict) else None
                if chosen not in RESOLUTIONS:
                    raise ProviderError(400, errors={"resolution": "Resolution is not valid."})
                changes["resolution"] = chosen
            elif name in editable or (creating and name in {"summary"}):
                changes[name] = value
            else:
                raise ProviderError(
                    400,
                    errors={name: NOT_SETTABLE.format(name=name)},
                )
        if "summary" in changes and (
            not isinstance(changes["summary"], str) or not changes["summary"].strip()
        ):
            raise ProviderError(400, errors={"summary": "You must specify a summary of the issue."})
        for name in ("description", F_CRITERIA):
            if name in changes:
                self._check_adf(changes[name], name)
        for name in (F_POINTS, *EXTRA_FLOATS):
            value = changes.get(name)
            if name in changes and value is not None and not _is_number(value):
                raise ProviderError(400, errors={name: "Number value expected."})
        return changes

    @staticmethod
    def _check_adf(value: Any, name: str) -> None:
        if value is None:
            return
        if not (
            isinstance(value, dict)
            and value.get("type") == "doc"
            and value.get("version") == 1
            and _adf_shape_ok(value)
        ):
            raise ProviderError(400, errors={name: "Operation value must be an Atlassian Document"})
        if len(json.dumps(value)) >= COMMENT_BODY_LIMIT * 8 and name != "description":
            raise ProviderError(400, errors={name: "CONTENT_LIMIT_EXCEEDED"})

    def _account(self, value: Any) -> str | None:
        if value is None:
            return None
        account = value.get("accountId") if isinstance(value, dict) else None
        if account is None and isinstance(value, dict) and "accountId" in value:
            return None
        if not isinstance(account, str) or account not in USERS:
            raise ProviderError(400, errors={"assignee": "User does not exist."})
        return account

    def _apply(
        self, t: Ticket, changes: dict[str, Any], label_adds: list[str], label_removes: list[str]
    ) -> bool:
        changed = False
        for name, value in changes.items():
            if name == "labels":
                new = {lab: self._label_visible_at() for lab in value}
                for lab in new:
                    if lab not in t.labels:
                        t.labels[lab] = new[lab]
                        changed = True
                for lab in list(t.labels):
                    if lab not in value:
                        del t.labels[lab]
                        changed = True
                continue
            if name in EXTRA_FLOATS:
                if t.extra.get(name) != value:
                    t.extra[name] = value
                    changed = True
                continue
            attr = {"duedate": "due", F_POINTS: "points", F_CRITERIA: "criteria"}.get(name, name)
            if name == "priority":
                value = (value or {}).get("name") if isinstance(value, dict) else value
            if getattr(t, attr) != value:
                setattr(t, attr, value)
                changed = True
        for lab in label_adds:
            if lab not in t.labels:
                t.labels[lab] = self._label_visible_at()
                changed = True
        for lab in label_removes:
            if lab in t.labels:
                del t.labels[lab]
                changed = True
        if changed:
            t.updated = self.now
        return changed

    def _label_visible_at(self) -> datetime:
        # The applied write ticks the clock one second after the handler returns.
        return self.now + timedelta(seconds=1 + self.quirks.label_lag_s)

    def _update_ops(self, update: Any) -> tuple[list[str], list[str]]:
        adds: list[str] = []
        removes: list[str] = []
        if update is None:
            return adds, removes
        if not isinstance(update, dict):
            raise ProviderError(400, ["'update' must be an object."])
        for name, ops in update.items():
            if name != "labels":
                raise ProviderError(
                    400, errors={name: f"Field '{name}' cannot be set through update."}
                )
            for op in ops if isinstance(ops, list) else []:
                if isinstance(op, dict) and "add" in op:
                    adds += self._check_labels([op["add"]])
                elif isinstance(op, dict) and "remove" in op:
                    removes += self._check_labels([op["remove"]])
                else:
                    raise ProviderError(400, errors={"labels": "Unsupported label operation."})
        return adds, removes

    def _edit(self, call: Call, key: str) -> Out:
        t = self._ticket(key)
        body = self._need_body(call)
        if call.one("notifyUsers") == "false" and self.quirks.notify_users_false_forbidden:
            raise ProviderError(
                403, ["To discard the notification, you need to be an administrator."]
            )
        fields = body.get("fields") or {}
        if not isinstance(fields, dict):
            raise ProviderError(400, ["'fields' must be an object."])
        changes = self._plan_fields(t, fields, creating=False)
        adds, removes = self._update_ops(body.get("update"))
        changed = self._apply(t, changes, adds, removes)  # V-10: validation above applied nothing
        return Out(204, None, applied=changed)

    def _assign(self, call: Call, key: str) -> Out:
        t = self._ticket(key)
        body = self._need_body(call)
        if "accountId" not in body:
            raise ProviderError(400, ["'accountId' is required."])
        account = self._account(body)
        changed = t.assignee != account
        t.assignee = account
        if changed:
            t.updated = self.now
        return Out(204, None, applied=changed)

    def _create(self, call: Call) -> Out:
        body = self._need_body(call)
        fields = body.get("fields")
        if not isinstance(fields, dict):
            raise ProviderError(400, ["'fields' is required."])
        project_field = fields.get("project")
        project = project_field.get("key") if isinstance(project_field, dict) else None
        if project != PROJECT:
            raise ProviderError(400, errors={"project": "Valid project is required"})
        itype = fields.get("issuetype")
        if not isinstance(itype, dict):
            raise ProviderError(400, errors={"issuetype": "Specify a valid issue type"})
        type_id = str(itype.get("id")) if itype.get("id") is not None else None
        if type_id is None and itype.get("name"):
            type_id = next((i for i, (n, _) in ISSUE_TYPES.items() if n == itype["name"]), None)
        if type_id not in ISSUE_TYPES:
            raise ProviderError(400, errors={"issuetype": "Specify a valid issue type"})
        changes = self._plan_fields(None, fields, creating=True)
        if "summary" not in changes:
            raise ProviderError(400, errors={"summary": "You must specify a summary of the issue."})
        subtask = ISSUE_TYPES[type_id][1]
        parent = changes.get("parent")
        if subtask and not parent:
            raise ProviderError(
                400, errors={"parent": "Sub-tasks must be created with a parent."}
            )  # V-29
        if parent:
            ptype = self._find(parent) if isinstance(parent, str) else None
            if ptype is None:
                raise ProviderError(400, errors={"parent": "Parent issue does not exist."})
            parent = ptype.key
            if subtask and ISSUE_TYPES[ptype.type_id][0] not in {"Task", "Bug"}:
                raise ProviderError(
                    400,
                    errors={"parent": "Sub-tasks can only be created under a task or a bug."},
                )
            if not subtask and ISSUE_TYPES[ptype.type_id][0] != "Epic":
                raise ProviderError(
                    400,
                    errors={
                        "parent": "Given parent issue does not belong to appropriate hierarchy."
                    },
                )
        adds, _ = self._update_ops(body.get("update"))
        props = self._parse_props(body.get("properties"))  # V-27
        key = self.next_key()
        t = self.add_ticket(key, str(changes["summary"]), type_id=type_id)
        t.status = "Backlog"
        t.reporter = changes.get("reporter") or ME_ACCOUNT
        t.parent = parent
        t.assignee = changes.get("assignee")
        t.description = changes.get("description")
        t.priority = (
            changes["priority"].get("name")
            if isinstance(changes.get("priority"), dict)
            else "Medium"
        )
        t.labels = {}
        self._apply(t, {"labels": changes.get("labels", [])}, adds, [])
        for name in ("duedate", F_POINTS, F_CRITERIA, *EXTRA_FLOATS):
            if name in changes:
                self._apply(t, {name: changes[name]}, [], [])
        t.properties = props
        t.created = t.updated = self.now
        return Out(
            201,
            {"id": str(t.id), "key": t.key, "self": f"{self.url}/rest/api/3/issue/{t.id}"},
            applied=True,
        )

    # -- transitions -----------------------------------------------------------------------------
    def _transition_obj(self, t: Ticket, tid: str, expand: bool) -> dict[str, Any]:
        name, target = TRANSITIONS[tid]
        out: dict[str, Any] = {
            "id": tid,
            "name": name,
            "to": self._status_obj(target),
            "hasScreen": STATUSES[target][1] == "done",
            "isGlobal": True,
            "isInitial": False,
            "isAvailable": True,
            "isConditional": False,
        }
        if expand:
            fields: dict[str, Any] = {}
            if STATUSES[target][1] == "done":
                fields["resolution"] = {
                    "required": True,
                    "name": "Resolution",
                    "key": "resolution",
                    "schema": {"type": "resolution", "system": "resolution"},
                    "operations": ["set"],
                    "allowedValues": [
                        {"id": str(100 + i), "name": n} for i, n in enumerate(RESOLUTIONS)
                    ],
                }
            if ISSUE_TYPES[t.type_id][1]:
                fields["parent"] = {
                    "required": True,
                    "name": "Parent",
                    "key": "parent",
                    "schema": {"type": "issuelink", "system": "parent"},
                    "operations": ["set"],
                }
            out["fields"] = fields
        return out

    def _transitions(self, call: Call, key: str) -> Out:
        t = self._ticket(key)
        expand = "transitions.fields" in (call.one("expand") or "")
        return Out(
            200, {"transitions": [self._transition_obj(t, tid, expand) for tid in TRANSITIONS]}
        )

    def _transition(self, call: Call, key: str) -> Out:
        t = self._ticket(key)
        body = self._need_body(call)
        chosen = body.get("transition")
        if not isinstance(chosen, dict):
            raise ProviderError(400, ["'transition' must be an object with an 'id'."])
        tid = str(chosen.get("id", ""))
        if tid not in TRANSITIONS:
            raise ProviderError(400, [f"Transition id '{tid}' is not valid for this issue."])
        target = TRANSITIONS[tid][1]
        done = STATUSES[target][1] == "done"
        fields = body.get("fields") or {}
        if not isinstance(fields, dict):
            raise ProviderError(400, ["'fields' must be an object."])
        allowed = {"resolution", "parent"}
        for name in fields:
            if name not in allowed:
                raise ProviderError(
                    400,
                    errors={name: NOT_SETTABLE.format(name=name)},
                )
        chosen_resolution = fields.get("resolution")
        if chosen_resolution is not None and not isinstance(chosen_resolution, dict):
            raise ProviderError(400, errors={"resolution": "Resolution is not valid."})
        resolution = chosen_resolution.get("name") if chosen_resolution else None
        if resolution is not None and resolution not in RESOLUTIONS:
            raise ProviderError(400, errors={"resolution": "Resolution is not valid."})
        if done and resolution is None and not self.quirks.done_without_resolution:
            raise ProviderError(400, errors={"resolution": "Resolution is required."})
        subtask = ISSUE_TYPES[t.type_id][1]
        if subtask and t.parent is None and "parent" not in fields:
            raise ProviderError(400, errors={"parent": "Parent is required."})
        if self.quirks.assignee_validator and t.assignee is None:
            leaving_backlog = STATUSES[t.status][1] == "new"
            if leaving_backlog or (subtask and done):
                # V-8, V-30: only the 400 reveals it, with locale-dependent text.
                raise ProviderError(400, [self.quirks.validator_message])
        t.status = target
        if done:
            t.resolution = resolution  # may stay empty (V-7)
        else:
            t.resolution = None
        t.updated = self.now
        return Out(204, None, applied=True)

    # -- comments --------------------------------------------------------------------------------
    def _comments(self, call: Call, key: str) -> Out:
        t = self._ticket(key)
        order = call.one("orderBy", "created") or "created"
        if order not in {"created", "-created", "+created"}:
            raise ProviderError(400, errors={"orderBy": "Unsupported order."})
        items = list(t.comments)
        if order == "-created":
            items.reverse()
        start = self._int_param(call, "startAt", 0)
        limit = self._int_param(call, "maxResults", 50)
        props = "properties" in (call.one("expand") or "")
        chunk = items[start : start + limit]
        return Out(
            200,
            {
                "startAt": start,
                "maxResults": limit,
                "total": len(items),
                "comments": [self._comment_obj(c, props=props) for c in chunk],
            },
        )

    # -- changelog -------------------------------------------------------------------------------
    def add_history(
        self,
        key: str,
        *items: dict[str, Any],
        when: datetime | None = None,
        author: str | None = ME_ACCOUNT,
        entry_id: str | None = None,
    ) -> dict[str, Any]:
        """Append one changelog entry (the ``items`` are raw, as the provider sends them)."""
        with self._lock:
            ticket = self.tickets[key]
            eid = entry_id if entry_id is not None else str(30000 + len(ticket.history))
            entry = {
                "id": eid,
                "author": self._user(author),
                "_created": when if when is not None else self.now + timedelta(seconds=1),
                "items": list(items),
            }
            ticket.history.append(entry)
            return entry

    @staticmethod
    def history_item(
        field_name: str,
        old: str | None,
        new: str | None,
        *,
        field_id: str | None = None,
        old_id: str | None = None,
        new_id: str | None = None,
        custom: bool = False,
    ) -> dict[str, Any]:
        """A raw item in the real shape: ``from``/``to`` hold ids, ``*String`` keys hold text."""
        item: dict[str, Any] = {
            "field": field_name,
            "fieldtype": "custom" if custom else "jira",
            "from": old_id,
            "fromString": old,
            "to": new_id,
            "toString": new,
        }
        if field_id is not None:
            item["fieldId"] = field_id
        return item

    def _changelog(self, call: Call, key: str) -> Out:
        t = self._ticket(key)
        start = self._int_param(call, "startAt", 0)
        limit = min(self._int_param(call, "maxResults", 100), 100)
        entries = list(t.history)
        chunk = entries[start : start + limit]
        values = []
        for e in chunk:
            out = {k: v for k, v in e.items() if k != "_created"}
            out["created"] = self.stamp(e["_created"])
            values.append(out)
        return Out(
            200,
            {
                "self": f"{self.url}/rest/api/3/issue/{t.id}/changelog",
                "startAt": start,
                "maxResults": limit,
                "total": len(entries),
                "isLast": start + limit >= len(entries),
                "values": values,
            },
        )

    def _add_comment(self, call: Call, key: str) -> Out:
        t = self._ticket(key)
        body = self._need_body(call)
        doc = body.get("body")
        if not (isinstance(doc, dict) and doc.get("type") == "doc" and _adf_shape_ok(doc)):
            raise ProviderError(400, ["Comment body is not valid!"])
        text_size = sum(len(n.get("text", "")) for n in self._text_nodes(doc))
        if text_size >= COMMENT_BODY_LIMIT:
            raise ProviderError(400, errors={"comment": "CONTENT_LIMIT_EXCEEDED"})  # V-21
        props = self._parse_props(body.get("properties"))
        cid = str(self._next_comment)
        self._next_comment += 1
        when = self.now
        comment = {
            "id": cid,
            "self": f"{self.url}/rest/api/3/issue/{t.id}/comment/{cid}",
            "author": self._user(ME_ACCOUNT),
            "updateAuthor": self._user(ME_ACCOUNT),
            "body": doc,
            "_created": when,
            "_updated": when,
            "properties": props,
            "jsdPublic": True,
        }
        t.comments.append(comment)
        t.updated = self.now
        return Out(201, self._comment_obj(comment, props=False), applied=True)

    def _attachment_obj(self, a: dict[str, Any]) -> dict[str, Any]:
        out = {k: v for k, v in a.items() if not k.startswith("_")}
        out["created"] = self.stamp(a["_created"])
        out["self"] = f"{self.url}/rest/api/3/attachment/{a['id']}"
        # An absolute URL that ticketctl must never follow (the interface says so).
        out["content"] = f"{self.url}/rest/api/3/attachment/content/{a['id']}"
        return out

    def attach(self, key: str, filename: str, content: bytes, *, author: str = ME_ACCOUNT) -> str:
        """Add an attachment to ``key`` directly (no request); returns its id."""
        t = self._ticket(key)
        aid = str(self._next_attachment)
        self._next_attachment += 1
        t.attachments.append(
            {
                "id": aid,
                "filename": filename,
                "size": len(content),
                "mimeType": "application/octet-stream",
                "author": self._user(author),
                "_created": self.now,
                "_content": content,
            }
        )
        return aid

    def _add_attachment(self, call: Call, key: str) -> Out:
        t = self._ticket(key)
        if (call.headers.get("x-atlassian-token") or [""])[0] != "no-check":
            raise ProviderError(403, text="XSRF check failed")  # the real provider's refusal
        body = call.body
        parts = body.get("_multipart") if isinstance(body, dict) else None
        files = [p for p in parts or [] if p["name"] == "file" and p["filename"] is not None]
        if len(files) != 1 or len(parts or []) != 1:
            raise ProviderError(400, ["Exactly one file part named file is required."])
        file = files[0]
        if len(file["content"]) > ATTACHMENT_LIMIT:
            raise ProviderError(413, text="The attachment is too large.")
        self.attach(key, file["filename"], file["content"])
        t.updated = self.now
        return Out(200, [self._attachment_obj(t.attachments[-1])], applied=True)

    def _find_attachment(self, aid: str) -> dict[str, Any]:
        for t in self.tickets.values():
            for a in t.attachments:
                if a["id"] == aid:
                    return a
        raise ProviderError(404, ["The attachment does not exist or you have no permission."])

    def _attachment_meta(self, call: Call, aid: str) -> Out:
        return Out(200, self._attachment_obj(self._find_attachment(aid)))

    def _attachment_content(self, call: Call, aid: str) -> Out:
        a = self._find_attachment(aid)
        if call.one("redirect") != "false":  # the real endpoint answers 303 to a media host
            return Out(303, None, {"Location": "https://media.example.test/blob/" + aid})
        return Out(200, a["_content"])

    LINK_TYPES = (
        {"id": "10000", "name": "Blocks", "inward": "is blocked by", "outward": "blocks"},
        {"id": "10001", "name": "Relates", "inward": "relates to", "outward": "relates to"},
    )

    def _link_types(self, call: Call) -> Out:
        return Out(200, {"issueLinkTypes": [dict(t) for t in self.LINK_TYPES]})

    def _link_side(self, t: Ticket) -> dict[str, Any]:
        return {
            "key": t.key,
            "id": str(t.id),
            "fields": {"summary": t.summary, "status": self._status_obj(t.status)},
        }

    def _add_link(self, call: Call) -> Out:
        """``POST issueLink``: 201 with an empty body; both tickets gain the link, one id."""
        body = self._need_body(call)
        kind = body.get("type")
        known = next(
            (
                x
                for x in self.LINK_TYPES
                if isinstance(kind, dict) and x["name"] == kind.get("name")
            ),
            None,
        )
        sides = {}
        for name in ("inwardIssue", "outwardIssue"):
            side = body.get(name)
            if not isinstance(side, dict) or not isinstance(side.get("key"), str):
                raise ProviderError(400, ["The issue link request is invalid."])
            sides[name] = self._ticket(side["key"])
        if known is None:
            raise ProviderError(404, ["No issue link type with name found."])
        if sides["inwardIssue"] is sides["outwardIssue"]:
            raise ProviderError(400, ["Can not link an issue to itself."])
        lid = str(self._next_link)
        self._next_link += 1
        kind_obj = {k: known[k] for k in ("id", "name", "inward", "outward")}
        sides["outwardIssue"].links.append(
            {"id": lid, "type": kind_obj, "inwardIssue": self._link_side(sides["inwardIssue"])}
        )
        sides["inwardIssue"].links.append(
            {"id": lid, "type": kind_obj, "outwardIssue": self._link_side(sides["outwardIssue"])}
        )
        for t in sides.values():
            t.updated = self.now
        return Out(201, None, applied=True)

    def _delete_link(self, call: Call, lid: str) -> Out:
        found = [t for t in self.tickets.values() if any(x.get("id") == lid for x in t.links)]
        if not found:
            raise ProviderError(404, ["No issue link with id found."])
        for t in found:
            t.links[:] = [x for x in t.links if x.get("id") != lid]
            t.updated = self.now
        return Out(204, None, applied=True)

    def _one_comment(self, call: Call, key: str, cid: str) -> Out:
        """Read, update or delete one comment; only the author may change it (403 otherwise)."""
        t = self._ticket(key)
        comment = next((c for c in t.comments if c["id"] == cid), None)
        if comment is None:
            raise ProviderError(404, ["Comment does not exist or you do not have permission."])
        if call.method == "GET":
            props = "properties" in (call.one("expand") or "")
            return Out(200, self._comment_obj(comment, props=props))
        if comment["author"]["accountId"] != ME_ACCOUNT:
            raise ProviderError(403, ["You do not have permission to change this comment."])
        if call.method == "DELETE":
            t.comments.remove(comment)
            t.updated = self.now
            return Out(204, None, applied=True)
        body = self._need_body(call)
        doc = body.get("body")
        if not (isinstance(doc, dict) and doc.get("type") == "doc" and _adf_shape_ok(doc)):
            raise ProviderError(400, ["Comment body is not valid!"])
        props = self._parse_props(body.get("properties"))
        comment["body"] = doc
        comment["_updated"] = self.now
        comment["updateAuthor"] = self._user(ME_ACCOUNT)
        comment["properties"] = {**(comment.get("properties") or {}), **props}
        t.updated = self.now
        return Out(200, self._comment_obj(comment, props=False), applied=True)

    @staticmethod
    def _check_property(key: Any, value: Any) -> None:
        """V-19: a key up to 255 bytes and a value up to 32,768 bytes of JSON."""
        if not isinstance(key, str) or not key:
            raise ProviderError(400, ["Invalid property key."])
        if len(key.encode()) > 255:
            raise ProviderError(400, ["The property key is too long."])
        if len(json.dumps(value).encode()) > PROPERTY_VALUE_LIMIT:
            raise ProviderError(400, ["The property value is too large."])

    def _parse_props(self, raw: Any) -> dict[str, Any]:
        """A body's ``properties``: a list of ``{key, value}`` (V-19, V-27)."""
        props: dict[str, Any] = {}
        if raw is None:
            return props
        if not isinstance(raw, list):
            raise ProviderError(400, ["'properties' must be a list."])
        for prop in raw:
            if not isinstance(prop, dict):
                raise ProviderError(400, ["Invalid property."])
            self._check_property(prop.get("key"), prop.get("value"))
            props[prop["key"]] = prop.get("value")
        return props

    def _property_get(self, bag: dict[str, Any], pkey: str) -> Out:
        if pkey not in bag:
            raise ProviderError(404, [f"Property with key '{pkey}' does not exist."])
        return Out(200, {"key": pkey, "value": bag[pkey]})

    def _property_put(self, call: Call, bag: dict[str, Any], pkey: str) -> Out:
        if call.body is None or (isinstance(call.body, tuple) and call.body[:1] == ("invalid",)):
            raise ProviderError(400, ["The request body must be valid JSON."])
        self._check_property(pkey, call.body)
        created = pkey not in bag
        changed = created or bag[pkey] != call.body
        bag[pkey] = call.body
        return Out(201 if created else 200, None, applied=changed)

    def _issue_property(self, call: Call, key: str, pkey: str) -> Out:
        t = self._ticket(key)
        if call.method == "GET":
            return self._property_get(t.properties, pkey)
        return self._property_put(call, t.properties, pkey)

    def _comment_property(self, call: Call, key: str, cid: str, pkey: str) -> Out:
        t = self._ticket(key)
        comment = next((c for c in t.comments if c["id"] == cid), None)
        if comment is None:
            raise ProviderError(404, ["Comment does not exist or you do not have permission."])
        if call.method == "GET":
            return self._property_get(comment["properties"], pkey)
        return self._property_put(call, comment["properties"], pkey)

    @staticmethod
    def _text_nodes(node: Any) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        stack = [node]
        while stack:
            cur = stack.pop()
            if isinstance(cur, dict):
                if cur.get("type") == "text":
                    out.append(cur)
                stack.extend(cur.get("content") or [])
        return out

    # -- metadata ------------------------------------------------------------------------------
    def _createmeta_types(self, call: Call, project: str) -> Out:
        if project.upper() != PROJECT:
            raise ProviderError(404, ["No project could be found."])
        types = [
            {"id": i, "name": n, "subtask": sub, "hierarchyLevel": -1 if sub else 0}
            for i, (n, sub) in ISSUE_TYPES.items()
        ]
        return Out(200, {"startAt": 0, "maxResults": 50, "total": len(types), "issueTypes": types})

    def _createmeta_fields(self, call: Call, project: str, type_id: str) -> Out:
        if project.upper() != PROJECT or type_id not in ISSUE_TYPES:
            raise ProviderError(404, ["No issue type could be found."])
        fields = [
            {
                "fieldId": "summary",
                "name": "Summary",
                "required": True,
                "schema": {"type": "string", "system": "summary"},
                "operations": ["set"],
            },
            {
                "fieldId": "description",
                "name": "Description",
                "required": False,
                "schema": {"type": "string", "system": "description"},
                "operations": ["set"],
            },
            {
                "fieldId": "priority",
                "name": "Priority",
                "required": False,
                "schema": {"type": "priority", "system": "priority"},
                "operations": ["set"],
            },
            {
                "fieldId": "assignee",
                "name": "Assignee",
                "required": False,
                "schema": {"type": "user", "system": "assignee"},
                "operations": ["set"],
            },
        ]
        ns = "com.atlassian.jira.plugin.system.customfieldtypes"
        for fid, name in {F_POINTS: "Story Points", **EXTRA_FLOATS}.items():
            fields.append(
                {
                    "fieldId": fid,
                    "name": name,
                    "required": False,
                    "schema": {"type": "number", "custom": f"{ns}:float"},
                    "operations": ["set"],
                }
            )
        if ISSUE_TYPES[type_id][1]:
            fields.append(
                {
                    "fieldId": "parent",
                    "name": "Parent",
                    "required": True,
                    "schema": {"type": "issuelink", "system": "parent"},
                    "operations": ["set"],
                }
            )
        return Out(200, {"startAt": 0, "maxResults": 50, "total": len(fields), "fields": fields})

    # -- agile -------------------------------------------------------------------------------------
    def _boards(self, call: Call) -> Out:
        project = call.one("projectKeyOrId")
        boards = [
            {"id": 1, "name": "Demo board", "type": "scrum", "location": {"projectKey": PROJECT}}
        ]
        if project is not None and project.upper() != PROJECT:
            boards = []
        return Out(
            200,
            {
                "maxResults": 50,
                "startAt": 0,
                "total": len(boards),
                "isLast": True,
                "values": boards,
            },
        )

    def _agile_sprint(self, sid: int) -> dict[str, Any]:
        sp = self.sprints[sid]
        out: dict[str, Any] = {
            "id": sid,
            "self": f"{self.url}/rest/agile/1.0/sprint/{sid}",
            "state": sp["state"],
            "name": sp["name"],
            "originBoardId": sp["board"],
        }
        if sp["start"]:
            out["startDate"] = sp["start"]
            out["endDate"] = sp["end"]
        if sp["state"] == "closed":
            out["completeDate"] = sp["end"]
        return out

    def _board_sprints(self, call: Call, board: str) -> Out:
        if board != "1":
            raise ProviderError(
                404, ["Board does not exist or you do not have permission to see it."]
            )
        states = {s for v in call.query.get("state", []) for s in v.split(",") if s}
        values = [
            self._agile_sprint(i)
            for i, sp in self.sprints.items()
            if not states or sp["state"] in states
        ]
        return Out(200, {"maxResults": 50, "startAt": 0, "isLast": True, "values": values})

    def _sprint_id(self, sid: str) -> int:
        found = next((i for i in self.sprints if str(i) == sid), None)
        if found is None:
            raise ProviderError(
                404, ["Sprint does not exist or you do not have permission to view it."]
            )
        return found

    def _sprint(self, call: Call, sid: str) -> Out:
        return Out(200, self._agile_sprint(self._sprint_id(sid)))

    def _issues_in(self, call: Call) -> list[Ticket]:
        body = self._need_body(call)
        keys = body.get("issues")
        if not isinstance(keys, list) or not keys:
            raise ProviderError(400, ["'issues' must be a non-empty list of keys."])
        return [self._ticket(str(k)) for k in keys]

    def _sprint_add(self, call: Call, sid_text: str) -> Out:
        sid = self._sprint_id(sid_text)
        tickets = self._issues_in(call)
        if self.sprints[sid]["state"] == "closed":
            raise ProviderError(400, ["Issues cannot be added to a closed sprint."])
        changed = False
        for t in tickets:
            if sid not in t.sprints:
                t.sprints = [s for s in t.sprints if self.sprints[s]["state"] == "closed"] + [sid]
                t.updated = self.now
                changed = True
        return Out(204, None, applied=changed)

    def _backlog(self, call: Call) -> Out:
        tickets = self._issues_in(call)
        changed = False
        for t in tickets:
            # V-17: the backlog call moves a ticket out of its open sprint.
            kept = [s for s in t.sprints if self.sprints[s]["state"] == "closed"]
            if kept != t.sprints:
                t.sprints = kept
                t.updated = self.now
                changed = True
        return Out(204, None, applied=changed)


UNVERIFIED = """
Behaviors of this fake that were never seen against the real provider (reported on the live
verification issues):
- the exact 401 body text, the 403 for a bearer header (V-1 gives only the status), the 410 body
- the Ratelimit and Ratelimit-Policy header formats (V-15 names the headers, not their values)
- the Retry-After forms on a 429 (none was seen live), including an HTTP date and junk
- search/jql: the response keys (no isLast, nextPageToken only when more exist), the page size
  cap with explicit fields (5000 assumed), that an absent fields list returns ids only, the
  nextPageToken format (opaque here), approximate-count's response key
- jql/parse: the response shape beyond ``errors``, and the wording of every JQL error message
- semantic JQL: which values count as unknown (the fake knows one project, three statuses)
- the JQL subset (functions, dates, text matching) beyond V-31 and V-32
- createmeta response keys (issueTypes, fields) and field entries
- transitions: the shape of ``fields`` with expand, a sub-task transition's ``parent`` field,
  the 400 body of an unknown transition id and of a missing resolution
- the 400 and 403 body texts for labels, summary, parent, notifyUsers, comment limits and
  properties (V-18, V-21, V-29 give statuses and key names only)
- the comment list shape (startAt, maxResults, total) and ``expand=properties`` entries
- the changelog endpoint (``GET /issue/{key}/changelog``): that it pages by ``startAt`` and
  ``maxResults`` (capped at 100 here) in the shape {startAt, maxResults, total, isLast, values},
  that ``values`` are oldest first, the item keys (field, fieldtype, fieldId, from, fromString,
  to, toString) and which of them a system field, a sprint, a label list and a custom field fill
  (ids in ``from``/``to``, text in the ``*String`` keys), and a hidden ticket's 404
- resolution-backfill (W24): that the edit screen of a done ticket lists ``resolution`` with the
  ``set`` operation (the ``resolution_on_edit_screen`` quirk, off by default), the shape of its
  ``allowedValues``, that ``PUT fields.resolution`` answers 204 and is stored on a done ticket,
  and the 400 body of a refused value
- the agile responses (board, sprint) and the closed-sprint add error
- the issue ``self`` links, the ``expand`` string and the default priority list order
- an empty-text or oversized ADF description on create or edit
- Basic auth with a wrong email but a valid token
- attachments: that the upload is ``POST /issue/{key}/attachments`` with one ``file`` part and
  the header ``X-Atlassian-Token: no-check`` (403 without it), answering a list with one
  attachment object; that ``GET /attachment/content/{id}?redirect=false`` answers the bytes
  while the plain endpoint answers a 303 to a media host; the 413 of an oversized upload; how a
  file name with a non-ASCII character is stored; the object keys (id, filename, size, mimeType,
  author, created, self, content); that an uploaded file is listed under ``fields.attachment``
  at once
"""
