"""``browse``: a read-only, full-screen viewer over the read commands of ticketctl.

It is a thin layer. Every request is made by the existing read handlers (``searchcmd.search``,
``getcmd.get``, ``commentscmd.comments`` and ``changelogcmd.changelog``) through the same
configuration and gateway as the command line; this module never opens a connection, imports no
write command and has no key that reaches one (``ACTIONS`` is the closed list of what a key can do).

The code is split so that nothing but ``draw`` and ``run_screen`` touches a terminal:

* :class:`Browser` is the view state (tickets, selection, pane, scroll) and its key handling. It
  takes a :class:`Source` (the loader) and produces the screen as a list of strings with
  :meth:`Browser.view`, so tests drive it with synthetic data.
* ``run_loop`` reads keys from any object that looks like a curses window (a fake in the tests).
* ``browse`` is the command handler: it checks that a terminal can be used, reads the first page
  (so a configuration or query error exits with the usual envelope before any screen is drawn) and
  starts the loop.

Everything the provider sent is untrusted: each text passes the output sanitizer again here, tabs
become spaces, and lines and item counts are capped before anything is drawn, so no control
character or escape sequence reaches the terminal.
"""

from __future__ import annotations

import argparse
import copy
import dataclasses
import os
import sys
import unicodedata
from collections.abc import Callable, Mapping, Sequence
from typing import Any, Protocol

from . import changelogcmd, commentscmd, getcmd, searchcmd
from .errors import from_exception, usage_error
from .flags import ArgError, Globals
from .sanitize import render

MAX_TICKETS = 2000  # tickets held in the list (pages of `--limit` are appended up to this)
MAX_PANE_LINES = 5000  # lines of one pane (ticket, comments, history)
MAX_PANE_CHARS = 400_000  # characters of one pane before it is split into lines
MAX_LINE_CHARS = 2000  # one line is cut here before it is wrapped to the screen
MAX_ROW_CHARS = 500  # one list row
MAX_MESSAGE_CHARS = 300
PANE_ENTRIES = 20  # comments and history entries read for a ticket

PANES = ("ticket", "comments", "history")

# What a key can do. Every action is a view change or a read; there is no write action.
ACTIONS = frozenset(
    {
        "up",
        "down",
        "page_up",
        "page_down",
        "top",
        "bottom",
        "open",
        "back",
        "pane_ticket",
        "pane_comments",
        "pane_history",
        "reload",
        "help",
        "redraw",
    }
)

HELP_LINES = (
    "ticketctl browse: a read-only viewer",
    "",
    "  up, down, k, j      move or scroll",
    "  PgUp, PgDn          move a page",
    "  g, G, Home, End     first, last",
    "  Enter, Right, l     open the selected ticket",
    "  1, 2, 3             ticket, comments, history (in a ticket)",
    "  q, Left, h          back; quit from the list",
    "  r                   run the search again",
    "  ?                   this help",
    "",
    "Moving down past the last ticket reads the next page.",
    "Nothing here changes a ticket.",
)


class Source(Protocol):
    """Where a :class:`Browser` reads from. Each call is one read command."""

    def search(self, cursor: str | None) -> Mapping[str, Any]: ...

    def pane(self, pane: str, ticket: Mapping[str, Any]) -> str: ...


# --------------------------------------------------------------------------------------------
# Text: sanitizing, width, wrapping
# --------------------------------------------------------------------------------------------


def _cells(ch: str) -> int:
    if unicodedata.combining(ch) or unicodedata.category(ch) in ("Mn", "Me"):
        return 0
    return 2 if unicodedata.east_asian_width(ch) in ("W", "F") else 1


def display_width(text: str) -> int:
    return sum(_cells(ch) for ch in text)


def fit(text: str, width: int) -> str:
    """``text`` cut to at most ``width`` terminal cells (a wide character is never split)."""
    used = 0
    for i, ch in enumerate(text):
        used += _cells(ch)
        if used > width:
            return text[:i]
    return text


def wrap(text: str, width: int) -> list[str]:
    """``text`` split into pieces of at most ``width`` cells (at least one piece)."""
    width = max(1, width)
    pieces: list[str] = []
    rest = text
    while rest:
        head = fit(rest, width)
        if not head:  # one character wider than the line: skip it, never loop
            rest = rest[1:]
            continue
        pieces.append(head)
        rest = rest[len(head) :]
    return pieces or [""]


def clean_line(text: object, limit: int = MAX_ROW_CHARS) -> str:
    """One display line: sanitized (control and invisible characters become markers)."""
    return render(str(text)[:limit], body=False)


def clean_lines(text: str) -> list[str]:
    """A pane's text as capped, sanitized lines (tabs become four spaces)."""
    safe = render(text[:MAX_PANE_CHARS], body=True).replace("\t", "    ")
    lines = [line[:MAX_LINE_CHARS] for line in safe.split("\n")]
    if len(lines) > MAX_PANE_LINES:
        lines = lines[:MAX_PANE_LINES] + [f"[... more than {MAX_PANE_LINES} lines not shown]"]
    return lines


def ticket_row(ticket: Mapping[str, Any]) -> str:
    status = ticket.get("status")
    name = status.get("name") if isinstance(status, dict) else None
    parts = [str(ticket.get("key", "?"))]
    if name:
        parts.append(f"[{name}]")
    if ticket.get("title"):
        parts.append(str(ticket["title"]))
    return clean_line("  ".join(parts))


# --------------------------------------------------------------------------------------------
# The view state
# --------------------------------------------------------------------------------------------


class Browser:
    """The state of the viewer and its key handling; :meth:`view` draws it as strings."""

    def __init__(
        self,
        source: Source,
        first_page: Mapping[str, Any],
        *,
        busy: Callable[[str], None] | None = None,
    ) -> None:
        self.source = source
        self.busy = busy  # called with a message before a blocking read (the loop redraws)
        self.tickets: list[dict[str, Any]] = []
        self.next_cursor: str | None = None
        self.selected = 0
        self.mode = "list"  # "list" or "detail"
        self.pane = "ticket"
        self.scroll = 0
        self.message = ""
        self.show_help = False
        self.done = False
        self._panes: dict[tuple[str, str], list[str]] = {}
        self._take(first_page, replace=True)

    # data ------------------------------------------------------------------------------------
    def _take(self, page: Mapping[str, Any], *, replace: bool) -> None:
        incoming = [t for t in page.get("tickets", []) if isinstance(t, dict)]
        if replace:
            self.tickets = []
            self.selected = 0
            self._panes.clear()
        known = {t.get("key") for t in self.tickets}
        for t in incoming:
            if len(self.tickets) >= MAX_TICKETS:
                break
            if t.get("key") not in known:
                self.tickets.append(t)
                known.add(t.get("key"))
        cursor = page.get("next")
        self.next_cursor = cursor if isinstance(cursor, str) else None
        if len(self.tickets) >= MAX_TICKETS and self.next_cursor is not None:
            self.message = f"list limit reached ({MAX_TICKETS} tickets)"
            self.next_cursor = None

    def _read(self, what: str, call: Callable[[], Any]) -> Any | None:
        """Run one read; a failure becomes the status message and the state stays as it was."""
        if self.busy is not None:
            self.busy(f"reading {what}...")
        try:
            result = call()
        except KeyboardInterrupt:
            raise
        except Exception as exc:  # noqa: BLE001 - shown in the status line, never a traceback
            err = from_exception(exc)
            if err is None:
                self.message = clean_line(f"error: {type(exc).__name__}", MAX_MESSAGE_CHARS)
            else:
                code = f"{err.code} [{err.reason}]" if err.reason else err.code
                self.message = clean_line(f"error: {code}: {err.message}", MAX_MESSAGE_CHARS)
            return None
        self.message = ""
        return result

    def _more(self) -> None:
        if self.next_cursor is None:
            return
        page = self._read("the next page", lambda: self.source.search(self.next_cursor))
        if page is not None:
            self._take(page, replace=False)

    def _reload(self) -> None:
        page = self._read("the search", lambda: self.source.search(None))
        if page is not None:
            self._take(page, replace=True)
            self.mode, self.scroll = "list", 0

    def _pane_key(self) -> tuple[str, str]:
        return (str(self.tickets[self.selected].get("key")), self.pane)

    def _load_pane(self) -> None:
        """Read the shown pane of the selected ticket unless it is held (the view never reads)."""
        key = self._pane_key()
        if key in self._panes:
            return
        ticket = self.tickets[self.selected]
        text = self._read(f"{key[1]} of {key[0]}", lambda: self.source.pane(key[1], ticket))
        if text is not None:
            self._panes[key] = clean_lines(text)

    def _lines(self) -> list[str]:
        held = self._panes.get(self._pane_key())
        if held is None:
            return ["(not loaded: press the pane's number to try again)"]
        return held

    # keys ------------------------------------------------------------------------------------
    def handle(self, action: str, size: tuple[int, int]) -> None:
        """Apply one action; ``size`` is ``(rows, columns)`` of the screen."""
        if action not in ACTIONS:
            raise ValueError(f"unknown action {action!r}")
        rows = max(1, size[0] - 3)
        if action == "redraw":
            return
        if action == "help":
            self.show_help = not self.show_help
            return
        if self.show_help:
            self.show_help = False  # any other key closes the help
            return
        if action == "reload":
            self._reload()
        elif self.mode == "list":
            self._list_action(action, rows)
        else:
            self._detail_action(action, rows, size[1])
        if self.mode == "detail" and action in (
            "open",
            "pane_ticket",
            "pane_comments",
            "pane_history",
        ):
            self._load_pane()

    def _list_action(self, action: str, rows: int) -> None:
        last = len(self.tickets) - 1
        if action in ("down", "page_down"):
            step = 1 if action == "down" else rows
            if self.selected + step > last and self.next_cursor is not None:
                self._more()
                last = len(self.tickets) - 1
            self.selected = min(self.selected + step, max(last, 0))
        elif action in ("up", "page_up"):
            self.selected = max(self.selected - (1 if action == "up" else rows), 0)
        elif action == "top":
            self.selected = 0
        elif action == "bottom":
            while self.next_cursor is not None and len(self.tickets) < MAX_TICKETS:
                before = len(self.tickets)
                self._more()
                if len(self.tickets) == before:
                    break
            self.selected = max(len(self.tickets) - 1, 0)
        elif action == "open" and self.tickets:
            self.mode, self.pane, self.scroll = "detail", "ticket", 0
        elif action == "back":
            self.done = True

    def _detail_action(self, action: str, rows: int, width: int) -> None:
        total = len(self._wrapped(max(width - 1, 1)))
        limit = max(total - rows, 0)
        if action == "down":
            self.scroll = min(self.scroll + 1, limit)
        elif action == "up":
            self.scroll = max(self.scroll - 1, 0)
        elif action == "page_down":
            self.scroll = min(self.scroll + rows, limit)
        elif action == "page_up":
            self.scroll = max(self.scroll - rows, 0)
        elif action == "top":
            self.scroll = 0
        elif action == "bottom":
            self.scroll = limit
        elif action == "back":
            self.mode, self.scroll, self.message = "list", 0, ""
        elif action.startswith("pane_"):
            self.pane, self.scroll = action.removeprefix("pane_"), 0

    # drawing ---------------------------------------------------------------------------------
    def _wrapped(self, width: int) -> list[str]:
        out: list[str] = []
        for line in self._lines():
            out.extend(wrap(line, width))
        return out

    def view(self, rows: int, cols: int) -> list[str]:
        """The screen: at most ``rows`` lines of at most ``cols - 1`` cells each."""
        if rows < 4 or cols < 12:
            return [fit("terminal too small", max(cols - 1, 0))][:rows]
        width = cols - 1
        body_rows = rows - 2
        if self.show_help:
            title, body = "help", list(HELP_LINES)
        elif self.mode == "list":
            title, body = self._list_view(body_rows)
        else:
            title, body = self._detail_view(body_rows, width)
        footer = self.message or "? help   q quit"
        lines = [fit(title, width), *(fit(b, width) for b in body[:body_rows])]
        lines += [""] * (rows - 1 - len(lines))
        lines.append(fit(clean_line(footer, MAX_MESSAGE_CHARS), width))
        return lines

    def _list_view(self, body_rows: int) -> tuple[str, list[str]]:
        total = len(self.tickets)
        more = "+" if self.next_cursor is not None else ""
        position = self.selected + 1 if total else 0
        title = f"ticketctl browse   {position}/{total}{more}"
        if not total:
            return title, ["no tickets"]
        top = max(0, min(self.selected - body_rows // 2, total - body_rows))
        body = []
        for i in range(top, min(top + body_rows, total)):
            mark = ">" if i == self.selected else " "
            body.append(f"{mark} {ticket_row(self.tickets[i])}")
        return title, body

    def _detail_view(self, body_rows: int, width: int) -> tuple[str, list[str]]:
        key = clean_line(self.tickets[self.selected].get("key", "?"), 80)
        tabs = "  ".join(
            f"[{i + 1} {name}]" if name == self.pane else f"{i + 1} {name}"
            for i, name in enumerate(PANES)
        )
        lines = self._wrapped(width)
        scroll = min(self.scroll, max(len(lines) - body_rows, 0))
        shown = lines[scroll : scroll + body_rows]
        return f"{key}   {tabs}   line {min(scroll + 1, len(lines))}/{len(lines)}", shown


# --------------------------------------------------------------------------------------------
# Keys and the curses loop
# --------------------------------------------------------------------------------------------

CHAR_KEYS: dict[str, str] = {
    "j": "down",
    "k": "up",
    "g": "top",
    "G": "bottom",
    "l": "open",
    "\n": "open",
    "\r": "open",
    "h": "back",
    "q": "back",
    "1": "pane_ticket",
    "2": "pane_comments",
    "3": "pane_history",
    "r": "reload",
    "?": "help",
}
# Names of the curses key constants and the action each one has.
NAMED_KEYS: dict[str, str] = {
    "KEY_UP": "up",
    "KEY_DOWN": "down",
    "KEY_PPAGE": "page_up",
    "KEY_NPAGE": "page_down",
    "KEY_HOME": "top",
    "KEY_END": "bottom",
    "KEY_ENTER": "open",
    "KEY_RIGHT": "open",
    "KEY_LEFT": "back",
    "KEY_RESIZE": "redraw",
}


def key_actions(curses_module: Any) -> dict[int, str]:
    """Key code -> action, built from the curses constants (so no constant is guessed)."""
    table = {ord(ch): action for ch, action in CHAR_KEYS.items()}
    for name, action in NAMED_KEYS.items():
        code = getattr(curses_module, name, None)
        if isinstance(code, int):
            table[code] = action
    return table


def draw(window: Any, lines: Sequence[str], rows: int, cols: int, curses_module: Any) -> None:
    """Paint ``lines`` (already sanitized and cut to the width); a failing cell is ignored."""
    window.erase()
    for y, line in enumerate(lines[:rows]):
        try:
            window.addstr(y, 0, line)
        except curses_module.error:
            pass  # writing the last cell of the last row raises after drawing; nothing to do
    window.refresh()


def run_loop(window: Any, browser: Browser, curses_module: Any) -> None:
    """Read keys and redraw until the browser is done or the input ends."""
    keys = key_actions(curses_module)

    def repaint(message: str | None = None) -> None:
        rows, cols = window.getmaxyx()
        lines = browser.view(rows, cols)
        if message is not None and lines:
            lines[-1] = fit(clean_line(message, MAX_MESSAGE_CHARS), max(cols - 1, 0))
        draw(window, lines, rows, cols, curses_module)

    browser.busy = repaint
    while not browser.done:
        repaint()
        code = window.getch()
        if code == -1:  # the input ended (a closed terminal): stop, never spin
            return
        action = keys.get(code)
        if action is not None:
            rows, cols = window.getmaxyx()
            browser.handle(action, (rows, cols))


def run_screen(browser: Browser) -> None:
    """Start curses around the loop; the terminal is restored on every exit."""
    import curses

    def main(window: Any) -> None:
        try:
            curses.curs_set(0)
        except curses.error:
            pass
        window.keypad(True)
        run_loop(window, browser, curses)

    curses.wrapper(main)


# --------------------------------------------------------------------------------------------
# The command
# --------------------------------------------------------------------------------------------


def search_namespace(ns: argparse.Namespace, cursor: str | None) -> argparse.Namespace:
    """The ``search`` arguments of this run (the fields, count and cursor are browse's own)."""
    out = copy.copy(ns)
    out.fields = None
    out.count = False
    out.cursor = cursor
    return out


def require_terminal(
    env: Mapping[str, str],
    *,
    stdin: Any = None,
    stdout: Any = None,
    curses_module: Any = None,
) -> None:
    """Exit 2 `invalid_argument` unless a full-screen terminal can be used.

    The checks: stdin and stdout are terminals, ``TERM`` names one that is not ``dumb``, and
    the curses module exists and can read that terminal's description.
    """
    stdin = sys.stdin if stdin is None else stdin
    stdout = sys.stdout if stdout is None else stdout
    for name, stream in (("stdin", stdin), ("stdout", stdout)):
        try:
            tty = bool(stream.isatty())
        except (AttributeError, ValueError):
            tty = False
        if not tty:
            raise usage_error(
                "invalid_argument", f"browse needs a terminal: {name} is not one (use `search`)"
            )
    term = env.get("TERM", "")
    if term in ("", "dumb"):
        raise usage_error(
            "invalid_argument", "browse needs a terminal: TERM is unset or `dumb` (use `search`)"
        )
    module = curses_module
    if module is None:
        try:
            import curses as module
        except ImportError:
            raise usage_error(
                "invalid_argument", "browse needs the Python curses module, which is not available"
            ) from None
    try:
        module.setupterm(term, stdout.fileno())
    except Exception:  # curses.error, or a stream with no descriptor
        raise usage_error(
            "invalid_argument", "browse needs a terminal: curses cannot use this TERM"
        ) from None


class RuntimeSource:
    """The real :class:`Source`: each call runs one existing read handler."""

    def __init__(self, g: Globals, ns: argparse.Namespace) -> None:
        # Warnings and verbose lines go to stderr, which is the screen: both are off.
        self.g = dataclasses.replace(g, quiet=True, verbose=False, json=False)
        self.ns = ns

    def search(self, cursor: str | None) -> Mapping[str, Any]:
        return searchcmd.search(self.g, search_namespace(self.ns, cursor))

    def pane(self, pane: str, ticket: Mapping[str, Any]) -> str:
        key = str(ticket.get("key"))
        # The ticket's own site: a key resolves by project, which may not be the searched site.
        site = ticket.get("site")
        g = dataclasses.replace(self.g, site=site if isinstance(site, str) else self.g.site)
        from .cli import build_parser  # late: cli imports the command registry that imports us

        parser = build_parser()
        try:
            if pane == "ticket":
                a = parser.parse_args(["get", "--", key])
                return getcmd.human_get(getcmd.get(g, a))
            if pane == "comments":
                lim = str(PANE_ENTRIES)
                a = parser.parse_args(["comments", "--limit", lim, "--newest-first", "--", key])
                return commentscmd.human_comments(commentscmd.comments(g, a))
            if pane == "history":
                lim = str(PANE_ENTRIES)
                a = parser.parse_args(["changelog", "--limit", lim, "--newest-first", "--", key])
                return changelogcmd.human_changelog(changelogcmd.changelog(g, a))
        except ArgError as exc:
            raise usage_error("invalid_argument", str(exc)) from None
        raise ValueError(f"unknown pane {pane!r}")


def browse(g: Globals, a: argparse.Namespace) -> None:
    if g.json:
        raise usage_error("invalid_argument", "browse prints no JSON (use `search --json`)")
    if g.verbose:
        raise usage_error("invalid_argument", "browse takes no --verbose (stderr is the screen)")
    require_terminal(os.environ)
    source = RuntimeSource(g, a)
    # The first page is read before the screen starts: a bad query, config or credential is the
    # usual error envelope and exit code, not a flash of an empty screen.
    first = source.search(None)
    run_screen(Browser(source, first))


__all__ = ["ACTIONS", "Browser", "browse"]
