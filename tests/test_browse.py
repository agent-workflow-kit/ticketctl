"""``browse`` (#45): the read-only terminal viewer.

The view state is tested with a synthetic source and no terminal; the curses loop is driven through
a fake window; the command is run end to end against the fake provider with the screen replaced by
scripted keys. No test needs a real terminal and none waits on a clock.
"""

from __future__ import annotations

import ast
import curses
import json
import signal
import unicodedata
from pathlib import Path
from typing import Any

import pytest

from tests.fake_provider import ME_ACCOUNT, SITE_EMAIL, SITE_TOKEN, FakeProvider
from tests.golden import validator
from ticketctl import browsecmd, commentscmd, getcmd
from ticketctl.browsecmd import ACTIONS, MAX_TICKETS, Browser
from ticketctl.cli import build_parser, main
from ticketctl.errors import TicketctlError
from ticketctl.flags import extract_globals

ROOT = Path(__file__).resolve().parent.parent


def ticket(n: int, **kw: Any) -> dict[str, Any]:
    return {
        "key": f"AB-{n}",
        "site": "main",
        "title": kw.pop("title", f"title {n}"),
        "status": {"name": "Open", "category": "new"},
        **kw,
    }


class FakeSource:
    """A synthetic source: pages of tickets and a text per pane. Counts every read."""

    def __init__(self, pages: list[list[dict[str, Any]]], fail: BaseException | None = None):
        self.pages = pages
        self.fail = fail
        self.searches: list[str | None] = []
        self.panes: list[tuple[str, str]] = []
        self.text: dict[str, str] = {}

    def search(self, cursor: str | None) -> dict[str, Any]:
        self.searches.append(cursor)
        if self.fail is not None and cursor is not None:
            raise self.fail
        index = 0 if cursor is None else int(cursor)
        more = index + 1 < len(self.pages)
        return {
            "tickets": self.pages[index],
            "truncated": more,
            "next": str(index + 1) if more else None,
        }

    def pane(self, pane: str, ticket: Any) -> str:
        self.panes.append((pane, ticket["key"]))
        if self.fail is not None:
            raise self.fail
        return self.text.get(pane, f"{pane} of {ticket['key']}")


def make(pages: list[list[dict[str, Any]]], **kw: Any) -> tuple[Browser, FakeSource]:
    source = FakeSource(pages, **kw)
    return Browser(source, source.search(None)), source


SIZE = (24, 80)


def press(b: Browser, *actions: str, size: tuple[int, int] = SIZE) -> None:
    for a in actions:
        b.handle(a, size)


def screen(b: Browser, size: tuple[int, int] = SIZE) -> list[str]:
    return b.view(*size)


# --- the list --------------------------------------------------------------------------------


def test_the_list_shows_key_status_and_title_and_marks_the_selection() -> None:
    b, _ = make([[ticket(1), ticket(2)]])
    lines = screen(b)
    assert lines[0].startswith("ticketctl browse") and "1/2" in lines[0]
    assert lines[1] == "> AB-1  [Open]  title 1"
    assert lines[2] == "  AB-2  [Open]  title 2"


def test_the_selection_stays_inside_the_list() -> None:
    b, _ = make([[ticket(1), ticket(2)]])
    press(b, "up", "up")
    assert b.selected == 0
    press(b, "down", "down", "down")
    assert b.selected == 1
    press(b, "top")
    assert b.selected == 0
    press(b, "bottom")
    assert b.selected == 1


def test_an_empty_search_shows_a_message_and_open_does_nothing() -> None:
    b, source = make([[]])
    assert "no tickets" in screen(b)[1]
    press(b, "open", "down", "page_down", "bottom", "top")
    assert b.mode == "list" and source.panes == []


def test_down_past_the_last_ticket_reads_the_next_page_once() -> None:
    b, source = make([[ticket(1), ticket(2)], [ticket(3)]])
    assert "1/2+" in screen(b)[0]
    press(b, "down", "down")
    assert [t["key"] for t in b.tickets] == ["AB-1", "AB-2", "AB-3"]
    assert source.searches == [None, "1"]
    assert b.selected == 2
    press(b, "down", "down")  # no cursor left: no further read
    assert source.searches == [None, "1"]
    assert "3/3" in screen(b)[0]


def test_a_repeated_key_on_a_later_page_is_listed_once() -> None:
    b, _ = make([[ticket(1), ticket(2)], [ticket(2), ticket(3)]])
    press(b, "bottom")
    assert [t["key"] for t in b.tickets] == ["AB-1", "AB-2", "AB-3"]


def test_the_list_is_capped_and_says_so() -> None:
    pages = [[ticket(i) for i in range(p * 500, p * 500 + 500)] for p in range(6)]
    b, _ = make(pages)
    press(b, "bottom")
    assert len(b.tickets) == MAX_TICKETS
    assert b.next_cursor is None
    assert "limit" in screen(b)[-1]
    assert f"{MAX_TICKETS}/{MAX_TICKETS} (limit)" in screen(b)[0]


def test_the_cap_stays_in_the_title_after_open_back_and_a_message_change() -> None:
    pages = [[ticket(i) for i in range(p * 500, p * 500 + 500)] for p in range(6)]
    b, source = make(pages)
    press(b, "bottom", "open", "back")
    assert b.message == "" and "(limit)" in screen(b)[0]
    source.fail = TicketctlError("unavailable", "x")
    press(b, "open", "pane_comments")
    assert "unavailable" in b.message
    press(b, "back")
    assert b.message == "" and "(limit)" in screen(b)[0]


def test_reload_forgets_the_cap_and_a_short_list_never_shows_it() -> None:
    pages = [[ticket(i) for i in range(p * 500, p * 500 + 500)] for p in range(6)]
    b, source = make(pages)
    press(b, "bottom")
    assert b.capped
    source.pages = [[ticket(1), ticket(2)]]
    press(b, "reload")
    assert not b.capped and "(limit)" not in screen(b)[0]
    assert "(limit)" not in make([[ticket(1)]])[0].view(*SIZE)[0]


def test_a_failed_next_page_is_a_message_and_the_list_stays() -> None:
    err = TicketctlError("unavailable", "the provider is down")
    b, _ = make([[ticket(1)], [ticket(2)]], fail=err)
    press(b, "down")
    assert [t["key"] for t in b.tickets] == ["AB-1"]
    assert "unavailable" in screen(b)[-1] and "provider is down" in screen(b)[-1]
    assert b.next_cursor == "1"  # it can be tried again


def test_reload_replaces_the_list_and_returns_to_it() -> None:
    b, source = make([[ticket(1)]])
    press(b, "open")
    press(b, "reload")
    assert b.mode == "list" and b.selected == 0
    assert source.searches == [None, None]


def test_back_from_the_list_ends_the_viewer() -> None:
    b, _ = make([[ticket(1)]])
    press(b, "back")
    assert b.done


def test_a_list_taller_than_the_screen_keeps_the_selection_visible() -> None:
    b, _ = make([[ticket(i) for i in range(1, 101)]])
    press(b, "bottom")
    lines = screen(b, (10, 60))
    assert len(lines) == 10
    assert any(line.startswith("> AB-100") for line in lines)
    press(b, "top")
    assert any(line.startswith("> AB-1 ") for line in screen(b, (10, 60)))


# --- the detail panes ------------------------------------------------------------------------


def test_open_reads_the_ticket_once_and_switching_panes_reads_each_once() -> None:
    b, source = make([[ticket(1), ticket(2)]])
    press(b, "open")
    assert b.mode == "detail" and "ticket of AB-1" in screen(b)[1]
    press(b, "pane_comments", "pane_history", "pane_ticket", "pane_comments")
    assert source.panes == [("ticket", "AB-1"), ("comments", "AB-1"), ("history", "AB-1")]
    assert "[1 ticket]" in screen(b)[0] or "[2 comments]" in screen(b)[0]


def test_back_from_a_detail_returns_to_the_list_with_the_same_selection() -> None:
    b, _ = make([[ticket(1), ticket(2)]])
    press(b, "down", "open", "back")
    assert b.mode == "list" and b.selected == 1 and not b.done


def test_a_failed_pane_read_is_a_message_not_an_exception() -> None:
    b, source = make([[ticket(1)]])
    source.fail = TicketctlError("not_found", "no such ticket")
    press(b, "open")
    lines = screen(b)
    assert lines[1].startswith("(not loaded")
    assert "not_found" in lines[-1]
    source.fail = None
    press(b, "pane_comments")  # a later read still works
    assert "comments of AB-1" in screen(b)[1]


def test_an_unexpected_exception_shows_its_class_only() -> None:
    b, source = make([[ticket(1)]])
    source.fail = RuntimeError("secret detail \x1b[31m")
    press(b, "open")
    assert "RuntimeError" in screen(b)[-1] and "secret" not in screen(b)[-1]


def test_scrolling_a_long_pane_is_clamped_at_both_ends() -> None:
    b, source = make([[ticket(1)]])
    source.text["ticket"] = "\n".join(f"line {i}" for i in range(100))
    press(b, "open", "up")
    assert b.scroll == 0
    press(b, "bottom")
    assert b.scroll == 100 - (24 - 2)  # the body is rows - 2 lines: title and status bar
    press(b, "down", "page_down")
    assert b.scroll == 100 - (24 - 2)
    last = screen(b)[-2]
    assert last == "line 99"
    press(b, "page_up", "top")
    assert b.scroll == 0 and screen(b)[1] == "line 0"


def test_the_last_scroll_position_is_the_largest_one_that_still_moves_up() -> None:
    b, source = make([[ticket(1)]])
    source.text["ticket"] = "\n".join(f"line {i}" for i in range(100))
    press(b, "open", "bottom")
    at_end = screen(b)
    shown = [x for x in at_end[1:-1] if x]
    assert shown[0] == "line 78" and shown[-1] == "line 99" and len(shown) == 22
    press(b, "up")  # one line back must change the screen (it did not when the end was off by one)
    assert screen(b) != at_end and screen(b)[1] == "line 77"
    press(b, "down", "down")  # and the end is not passed
    assert screen(b) == at_end


def test_a_pane_that_just_fits_does_not_scroll() -> None:
    b, source = make([[ticket(1)]])
    source.text["ticket"] = "\n".join(f"line {i}" for i in range(22))  # 24 rows - 2
    press(b, "open", "bottom", "page_down", "down")
    assert b.scroll == 0


def test_long_lines_wrap_and_a_pane_is_capped() -> None:
    b, source = make([[ticket(1)]])
    source.text["ticket"] = (
        "x" * 200 + "\n" + "\n".join("y" for _ in range(browsecmd.MAX_PANE_LINES + 10))
    )
    press(b, "open")
    lines = screen(b, (24, 41))
    assert lines[1] == "x" * 40 and lines[2] == "x" * 40
    assert len(b._lines()) == browsecmd.MAX_PANE_LINES + 1  # noqa: SLF001
    assert "not shown" in b._lines()[-1]  # noqa: SLF001


def test_selecting_another_ticket_does_not_reuse_the_previous_pane() -> None:
    b, source = make([[ticket(1), ticket(2)]])
    press(b, "open", "back", "down", "open")
    assert source.panes == [("ticket", "AB-1"), ("ticket", "AB-2")]


# --- hostile text ----------------------------------------------------------------------------

HOSTILE = [
    "\x1b[2J\x1b[31mred\x1b[0m",
    "\x9b31m c1 csi",
    "\x1b]0;window title\x07",
    "nul\x00byte",
    "bidi ‮ reversed ⁦",
    "﻿bom ​ zero width",
    "tab\there\r\nnewline",
    "lone surrogate \ud800",
    "\x7f del \x08 backspace",
    "wide " + "漢" * 300,
    "combining " + "é" * 300,
    "z" * 100_000,
]


def bad(line: str) -> list[str]:
    return [c for c in line if unicodedata.category(c) in ("Cc", "Cf", "Cs", "Co", "Cn")]


@pytest.mark.parametrize("text", HOSTILE)
def test_no_control_or_invisible_character_reaches_the_screen(text: str) -> None:
    b, source = make([[ticket(1, title=text)]])
    source.text = {"ticket": text, "comments": text, "history": text}
    sizes = [(24, 80), (5, 13), (4, 12), (60, 200)]
    for size in sizes:
        assert not [c for line in screen(b, size) for c in bad(line)]
        assert all(browsecmd.display_width(line) <= size[1] - 1 for line in screen(b, size))
        assert len(screen(b, size)) <= size[0]
    for pane in ("pane_ticket", "pane_comments", "pane_history"):
        press(b, "open") if b.mode == "list" else None
        press(b, pane)
        for size in sizes:
            lines = screen(b, size)
            assert not [c for line in lines for c in bad(line)], text
            assert all(browsecmd.display_width(line) <= size[1] - 1 for line in lines)


def test_an_error_message_with_an_escape_sequence_is_sanitized() -> None:
    err = TicketctlError("rejected", "bad \x1b[2J value \x9b")
    b, _ = make([[ticket(1)], [ticket(2)]], fail=err)
    press(b, "down")
    last = screen(b)[-1]
    assert "\x1b" not in last and "\x9b" not in last and "<U+001B>" in last


def test_a_ticket_with_odd_values_does_not_break_the_row() -> None:
    odd = {"key": 5, "status": "Open", "title": None}
    b, _ = make([[odd, {"key": "AB-2", "status": {"name": None}}]])
    assert screen(b)[1].startswith("> 5")


def test_a_tiny_screen_says_so_and_never_overflows() -> None:
    b, _ = make([[ticket(1)]])
    for size in [(1, 1), (3, 80), (24, 5), (0, 0)]:
        lines = b.view(*size)
        assert len(lines) <= max(size[0], 0)
        assert all(browsecmd.display_width(x) <= max(size[1] - 1, 0) for x in lines)


def test_width_helpers() -> None:
    assert browsecmd.fit("漢字漢", 5) == "漢字"
    assert browsecmd.fit("éé", 1) == "é"
    assert browsecmd.wrap("", 5) == [""]
    assert browsecmd.wrap("漢" * 3, 1) == [""]  # too wide for the line: dropped, never a loop
    assert browsecmd.wrap("abcdef", 4) == ["abcd", "ef"]


# --- keys: no write can be reached -----------------------------------------------------------

WRITE_WORDS = (
    "write", "send", "post", "comment", "transition", "create", "update", "delete", "edit",
    "assign", "link", "attach", "sprint", "label", "save", "submit", "confirm", "yes",
)  # fmt: skip


def test_every_key_maps_to_a_view_or_read_action() -> None:
    assert set(browsecmd.CHAR_KEYS.values()) <= ACTIONS
    assert set(browsecmd.NAMED_KEYS.values()) <= ACTIONS
    # The closed list: a new action must be added here on purpose, and none may write.
    assert ACTIONS == {
        "up", "down", "page_up", "page_down", "top", "bottom", "open", "back",
        "pane_ticket", "pane_comments", "pane_history", "reload", "help", "redraw",
    }  # fmt: skip
    for action in ACTIONS:
        assert not any(word in action for word in WRITE_WORDS if word != "comment"), action


def test_the_key_table_is_built_from_curses_constants() -> None:
    table = browsecmd.key_actions(curses)
    assert table[curses.KEY_UP] == "up" and table[curses.KEY_NPAGE] == "page_down"
    assert table[ord("q")] == "back" and table[ord("?")] == "help"
    assert curses.KEY_RESIZE in table


def test_an_unknown_action_is_refused() -> None:
    b, _ = make([[ticket(1)]])
    with pytest.raises(ValueError):
        b.handle("transition", SIZE)


FORBIDDEN_MODULES = {
    # the write commands and what they run on
    "writeflow", "transitioncmd", "commentcmd", "commentedit", "createcmd", "updatecmd",
    "sprintcmd", "linkcmd", "attachmentcmd", "audit", "hooks", "plan", "dedupe", "commands",
    "handlers", "provider", "jira",
    # one way in: the gateway is reached only through the read handlers
    "gateway", "cli",
    # no network or process code of its own
    "http", "urllib", "socket", "ssl", "subprocess", "requests", "importlib", "ctypes",
}  # fmt: skip
# The only imports allowed although forbidden above: (qualified function, module). The parser
# is built where it is used because cli imports the command registry that imports this module.
ALLOWED_LATE_IMPORTS = {("RuntimeSource.pane", "cli")}
FORBIDDEN_OS_CALLS = ("system", "popen", "exec", "spawn", "fork", "posix_spawn")


def import_violations(source: str) -> list[str]:
    """What in ``source`` imports a forbidden module or starts a process (see the lists above)."""
    found: list[str] = []

    def visit(node: ast.AST, scope: tuple[str, ...]) -> None:
        here = ".".join(scope)
        if isinstance(node, ast.Import | ast.ImportFrom):
            if isinstance(node, ast.Import):
                paths = [a.name for a in node.names]
            else:
                base = node.module or ""
                paths = [base, *(f"{base}.{a.name}".strip(".") for a in node.names)]
            for path in paths:
                bad = FORBIDDEN_MODULES & set(path.split("."))
                for module in sorted(bad):
                    if (here, module) not in ALLOWED_LATE_IMPORTS:
                        found.append(f"import {path} in {here or '<module>'}")
            if isinstance(node, ast.ImportFrom) and node.module == "os":
                found.extend(
                    f"from os import {a.name}"
                    for a in node.names
                    if a.name.startswith(FORBIDDEN_OS_CALLS)
                )
        if isinstance(node, ast.Call):
            func = node.func
            if isinstance(func, ast.Name) and func.id == "__import__":
                found.append("__import__ call")
            if (
                isinstance(func, ast.Attribute)
                and isinstance(func.value, ast.Name)
                and func.value.id == "os"
                and func.attr.startswith(FORBIDDEN_OS_CALLS)
            ):
                found.append(f"os.{func.attr} call")
        inner = scope
        if isinstance(node, ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef):
            inner = (*scope, node.name)
        for child in ast.iter_child_nodes(node):
            visit(child, inner)

    visit(ast.parse(source), ())
    return found


def test_the_browse_module_imports_no_write_command_and_no_network_code() -> None:
    source = (ROOT / "src" / "ticketctl" / "browsecmd.py").read_text()
    assert import_violations(source) == []


def test_the_late_cli_import_is_the_one_allowed_and_exists() -> None:
    # The allowlist is not dead: the documented late import is really there.
    source = (ROOT / "src" / "ticketctl" / "browsecmd.py").read_text()
    assert "from .cli import build_parser" in source
    assert (
        import_violations("class RuntimeSource:\n    def pane(self):\n        from .cli import x\n")
        == []
    )


@pytest.mark.parametrize(
    "source",
    [
        "import ticketctl.writeflow",
        "from ticketctl import transitioncmd",
        "from .transitioncmd import transition",
        "from . import commands",
        "import urllib.request",
        "from urllib import request",
        "import http.client",
        "import ssl",
        "import importlib",
        "from importlib import import_module",
        "import subprocess",
        "import socket",
        "from .gateway import open_gateway",
        "from .cli import build_parser",  # at the top of the module
        "def other():\n    from .cli import build_parser",  # a lazy import elsewhere
        "class RuntimeSource:\n    def search(self):\n        from .cli import x",
        "class Other:\n    def pane(self):\n        from .cli import x",
        "def f():\n    import urllib.request",
        "__import__('subprocess')",
        "def f():\n    __import__('x')",
        "import os\nos.system('x')",
        "import os\nos.popen('x')",
        "import os\nos.execv('x', [])",
        "import os\nos.execvp('x', [])",
        "import os\nos.spawnl(0, 'x')",
        "from os import system",
        "from os import execv",
    ],
)
def test_the_import_checker_flags_each_forbidden_form(source: str) -> None:
    assert import_violations(source) != [], source


@pytest.mark.parametrize(
    "source",
    [
        "import os\nos.environ.get('x')",
        "from . import changelogcmd, commentscmd, getcmd, searchcmd",
        "import curses",
        "from .sanitize import render",
        "import argparse, copy, dataclasses, sys, unicodedata",
    ],
)
def test_the_import_checker_passes_the_allowed_forms(source: str) -> None:
    assert import_violations(source) == [], source


def test_help_is_a_toggle_and_any_key_closes_it() -> None:
    b, _ = make([[ticket(1), ticket(2)]])
    press(b, "help")
    assert "help" in screen(b)[0] and "read-only" in "\n".join(screen(b))
    press(b, "down")
    assert not b.show_help and b.selected == 0  # the key only closed the help
    press(b, "help", "help")
    assert not b.show_help


# --- the curses loop through a fake window ---------------------------------------------------


class FakeWindow:
    def __init__(self, keys: list[int], size: tuple[int, int] = (24, 80), fail_cells: bool = False):
        self.keys = list(keys)
        self.size = size
        self.fail_cells = fail_cells
        self.frames: list[list[str]] = []
        self._rows: dict[int, str] = {}
        self.refreshes = 0
        self.keypad_calls: list[bool] = []

    def keypad(self, flag: bool) -> None:
        self.keypad_calls.append(flag)

    def getmaxyx(self) -> tuple[int, int]:
        return self.size

    def erase(self) -> None:
        self._rows = {}

    def addstr(self, y: int, x: int, text: str) -> None:
        assert x == 0 and "\x1b" not in text and "\n" not in text
        self._rows[y] = text
        if self.fail_cells and y == self.size[0] - 1:
            raise curses.error("last cell")

    def refresh(self) -> None:
        self.refreshes += 1
        self.frames.append([self._rows[i] for i in sorted(self._rows)])

    def getch(self) -> int:
        return self.keys.pop(0) if self.keys else -1


def test_the_loop_draws_handles_keys_and_stops_on_back() -> None:
    b, _ = make([[ticket(1), ticket(2)]])
    w = FakeWindow([ord("j"), curses.KEY_RESIZE, ord("q")])
    browsecmd.run_loop(w, b, curses)
    assert b.done and b.selected == 1
    assert w.frames[0][1].startswith("> AB-1") and w.frames[-1][2].startswith("> AB-2")


def test_the_loop_stops_when_the_input_ends() -> None:
    b, _ = make([[ticket(1)]])
    w = FakeWindow([])  # getch returns -1 for ever
    browsecmd.run_loop(w, b, curses)
    assert not b.done and w.refreshes == 1


def test_an_unknown_key_is_ignored() -> None:
    b, _ = make([[ticket(1)]])
    w = FakeWindow([ord("x"), ord("w"), ord("c"), 0, 27, 999, ord("q")])
    browsecmd.run_loop(w, b, curses)
    assert b.done and b.mode == "list"


def test_the_bottom_right_cell_that_curses_cannot_write_is_ignored() -> None:
    b, _ = make([[ticket(1)]])
    # 16 columns: the status line "? help   q quit" is 15 cells, reaching the last column.
    w = FakeWindow([ord("q")], size=(6, 16), fail_cells=True)
    browsecmd.run_loop(w, b, curses)
    assert b.done and w.frames


def test_any_other_curses_failure_surfaces() -> None:
    last_row_short = FakeWindow([], size=(6, 80), fail_cells=True)  # the line does not reach it
    with pytest.raises(curses.error):
        browsecmd.draw(last_row_short, ["a", "b", "c", "d", "e", "short"], 6, 80, curses)

    class FailsEarlyRow(FakeWindow):
        def addstr(self, y: int, x: int, text: str) -> None:
            if y == 0:
                raise curses.error("not the corner")
            super().addstr(y, x, text)

    with pytest.raises(curses.error):
        browsecmd.draw(FailsEarlyRow([], size=(6, 16)), ["x" * 15] * 6, 6, 16, curses)
    assert FakeWindow([]).refreshes == 0


def test_a_slow_read_repaints_with_a_message_first() -> None:
    b, source = make([[ticket(1)]])
    w = FakeWindow([ord("l"), ord("q"), ord("q")])
    browsecmd.run_loop(w, b, curses)
    assert any(frame[-1] == "reading ticket of AB-1..." for frame in w.frames)


def test_the_loop_draws_only_what_fits_a_small_window() -> None:
    b, _ = make([[ticket(1, title="t" * 500)]])
    w = FakeWindow([ord("q")], size=(6, 20))
    browsecmd.run_loop(w, b, curses)
    assert all(len(line) <= 19 for line in w.frames[-1]) and len(w.frames[-1]) <= 6


# --- the terminal check ----------------------------------------------------------------------


class Stream:
    def __init__(self, tty: bool) -> None:
        self.tty = tty

    def isatty(self) -> bool:
        return self.tty

    def fileno(self) -> int:
        return 1


class FakeCurses:
    def __init__(self, fail: bool = False) -> None:
        self.fail = fail
        self.terms: list[str] = []

    def setupterm(self, term: str, fd: int) -> None:
        if self.fail:
            raise RuntimeError("unknown terminal")
        self.terms.append(term)


def reason(**kw: Any) -> str:
    with pytest.raises(TicketctlError) as exc:
        browsecmd.require_terminal(**kw)
    assert (exc.value.exit, exc.value.code, exc.value.reason) == (2, "usage", "invalid_argument")
    return exc.value.message


def test_a_usable_terminal_passes() -> None:
    c = FakeCurses()
    browsecmd.require_terminal(
        {"TERM": "xterm"}, stdin=Stream(True), stdout=Stream(True), curses_module=c
    )
    assert c.terms == ["xterm"]


@pytest.mark.parametrize(
    ("env", "stdin", "stdout", "curses_module", "needle"),
    [
        ({"TERM": "xterm"}, False, True, FakeCurses(), "stdin"),
        ({"TERM": "xterm"}, True, False, FakeCurses(), "stdout"),
        ({}, True, True, FakeCurses(), "TERM"),
        ({"TERM": ""}, True, True, FakeCurses(), "TERM"),
        ({"TERM": "dumb"}, True, True, FakeCurses(), "TERM"),
        ({"TERM": "nonsense"}, True, True, FakeCurses(fail=True), "curses cannot use"),
    ],
)
def test_an_unusable_terminal_is_a_usage_error(
    env: dict[str, str], stdin: bool, stdout: bool, curses_module: Any, needle: str
) -> None:
    message = reason(
        env=env, stdin=Stream(stdin), stdout=Stream(stdout), curses_module=curses_module
    )
    assert needle in message


def test_a_stream_without_isatty_is_not_a_terminal() -> None:
    assert "stdin" in reason(env={"TERM": "xterm"}, stdin=object(), stdout=Stream(True))


def test_missing_curses_is_a_clear_usage_error(monkeypatch: pytest.MonkeyPatch) -> None:
    import builtins

    real = builtins.__import__

    def refuse(name: str, *a: Any, **k: Any) -> Any:
        if name == "curses":
            raise ImportError("no _curses")
        return real(name, *a, **k)

    monkeypatch.setattr(builtins, "__import__", refuse)
    message = reason(env={"TERM": "xterm"}, stdin=Stream(True), stdout=Stream(True))
    assert "curses module" in message


# --- the command, end to end against the fake provider ---------------------------------------

TOKEN_VAR = "TICKETCTL_TEST_BROWSE_TOKEN"


class Rig:
    def __init__(
        self, tmp: Path, fake: FakeProvider, capsys: Any, mp: pytest.MonkeyPatch, ro: bool
    ) -> None:
        self.fake, self.capsys = fake, capsys
        mp.setenv("XDG_STATE_HOME", str(tmp / "state"))
        mp.setenv(TOKEN_VAR, SITE_TOKEN)
        mp.delenv("TICKETCTL_READ_ONLY", raising=False)
        self.config = tmp / "config.yaml"
        flag = "read_only: true\n" if ro else ""
        self.config.write_text(
            f"{flag}sites:\n  main:\n    url: {fake.url}\n    email: {SITE_EMAIL}\n"
            f"    token_env: {TOKEN_VAR}\n    allow_http: true\n    projects: [DEMO]\n"
        )
        self.config.chmod(0o600)
        self.state = tmp / "state"

    def run(self, *argv: str) -> tuple[int, str, str]:
        code = main(["--config", str(self.config), *argv])
        cap = self.capsys.readouterr()
        return code, cap.out, cap.err


@pytest.fixture
def rig(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: Any, fake: FakeProvider) -> Rig:
    return Rig(tmp_path, fake, capsys, monkeypatch, ro=False)


@pytest.fixture
def ro_rig(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: Any, fake: FakeProvider) -> Rig:
    return Rig(tmp_path, fake, capsys, monkeypatch, ro=True)


def scripted(monkeypatch: pytest.MonkeyPatch, actions: list[str]) -> list[Browser]:
    """Replace the terminal: the viewer is built as usual and given these actions."""
    seen: list[Browser] = []

    def screen_stub(browser: Browser) -> None:
        seen.append(browser)
        for action in actions:
            browser.handle(action, SIZE)
            if browser.done:
                break

    monkeypatch.setattr(browsecmd, "require_terminal", lambda env: None)
    monkeypatch.setattr(browsecmd, "run_screen", screen_stub)
    return seen


def test_without_a_terminal_the_command_exits_2_with_the_envelope(rig: Rig) -> None:
    code, out, err = rig.run("--json", "browse", "--project", "DEMO")
    # --json is refused first; without it the terminal check answers
    assert code == 2 and "no JSON" in json.loads(out)["error"]["message"]
    code, out, err = rig.run("browse", "--project", "DEMO")
    assert code == 2 and out == ""
    assert "browse needs a terminal" in err and "Traceback" not in err
    assert rig.fake.calls == []  # nothing was sent


def test_a_missing_filter_is_the_search_usage_error(rig: Rig) -> None:
    code, out, _ = rig.run("--json", "browse")
    env = json.loads(out)
    validator("cli/error.json").validate(env)
    assert (code, env["error"]["reason"]) == (2, "missing_option")


def test_verbose_is_refused(rig: Rig, monkeypatch: pytest.MonkeyPatch) -> None:
    scripted(monkeypatch, [])
    code, _, err = rig.run("--verbose", "browse", "--project", "DEMO")
    assert code == 2 and "--verbose" in err


def test_write_only_flags_are_refused_like_on_any_read(rig: Rig) -> None:
    for flag in ("--yes", "--dry-run"):
        code, _, err = rig.run(flag, "browse", "--project", "DEMO")
        assert code == 2 and "only valid on a write command" in err


def test_the_search_flags_are_accepted_and_search_only_flags_are_not(rig: Rig) -> None:
    for flag in ("--count", "--cursor", "--fields"):
        code, _, err = rig.run("browse", "--project", "DEMO", flag)
        assert code == 2 and "ticketctl: error:" in err


def test_a_session_lists_opens_and_reads_every_pane(
    rig: Rig, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen = scripted(
        monkeypatch,
        ["open", "pane_comments", "pane_history", "back", "down", "open", "pane_ticket"],
    )
    code, out, err = rig.run("browse", "--project", "DEMO", "--order-by", "key")
    assert (code, out, err) == (0, "", "")
    b = seen[0]
    assert len(b.tickets) == 3
    text = "\n".join(b._lines())  # noqa: SLF001
    assert "DEMO-2" in text and "Seeded task in progress" in text


def test_every_request_of_a_session_is_a_read(rig: Rig, monkeypatch: pytest.MonkeyPatch) -> None:
    scripted(monkeypatch, ["open", "pane_comments", "pane_history", "back", "reload", "bottom"])
    code, _, _ = rig.run("browse", "--project", "DEMO")
    assert code == 0
    methods = {(c.method, c.path) for c in rig.fake.calls}
    allowed_posts = {
        "/rest/api/3/search/jql",
        "/rest/api/3/jql/parse",
        "/rest/api/3/search/approximate-count",
    }
    assert {p for m, p in methods if m != "GET"} <= allowed_posts
    assert rig.fake.applied == []


def test_a_session_works_with_read_only_mode_on(
    ro_rig: Rig, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen = scripted(monkeypatch, ["open", "pane_comments", "pane_history", "back"])
    code, out, err = ro_rig.run("browse", "--project", "DEMO")
    assert (code, out, err) == (0, "", "")
    assert len(seen[0].tickets) == 3 and ro_rig.fake.applied == []
    side = [
        p.name for p in ro_rig.state.rglob("*") if p.name == "audit.jsonl" or "locks" in p.parts
    ]
    assert side == []  # no audit record and no lock: a read


def test_the_pane_reads_use_the_ticket_site_and_stay_quiet(
    rig: Rig, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen = scripted(monkeypatch, [])
    rig.run("browse", "--project", "DEMO")
    source = seen[0].source
    assert isinstance(source, browsecmd.RuntimeSource)
    assert source.g.quiet and not source.g.verbose and not source.g.json


def test_a_first_page_failure_is_the_usual_envelope_before_any_screen(
    rig: Rig, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen = scripted(monkeypatch, [])
    code, out, err = rig.run("browse", "--jql", "this is not jql ((")
    assert seen == [] and out == ""
    assert code == 22 and err.startswith("ticketctl: error: invalid_request [invalid_query]:"), (
        code,
        err,
    )


def test_a_bad_token_is_an_error_exit_not_a_screen(
    rig: Rig, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen = scripted(monkeypatch, [])
    monkeypatch.setenv(TOKEN_VAR, "wrong-token-value")
    code, _, err = rig.run("browse", "--project", "DEMO")
    assert code == 12 and seen == [] and "wrong-token-value" not in err


def test_help_text_names_the_command(capsys: Any) -> None:
    with pytest.raises(SystemExit):
        main(["browse", "--help"])
    out = capsys.readouterr().out
    assert "read-only" in out and "--project" in out and "--cursor" not in out


# --- real curses on a pseudo-terminal --------------------------------------------------------

CHILD = """
import sys
from ticketctl import browsecmd

class Source:
    def search(self, cursor):
        t = {"key": "AB-1", "title": "pty \\x1b[31mticket", "status": {"name": "Open"}}
        return {"tickets": [t], "truncated": False, "next": None}
    def pane(self, pane, ticket):
        return pane

source = Source()
try:
    browsecmd.run_screen(browsecmd.Browser(source, source.search(None)))
except KeyboardInterrupt:  # what the command turns into exit 130
    sys.exit(130)
"""

RMCUP = b"\x1b[?1049l"  # the xterm sequence that leaves the alternate screen


def run_on_pty(tmp_path: Path, *, before: bytes = b"", after: bytes = b"") -> tuple[int, bytes]:
    """Run CHILD on a pty: ``before`` is typed first, ``after`` once the screen is drawn.

    Every wait is a ``select`` bounded to 20 s; a child that hangs is killed by its exact pid.
    """
    import os
    import pty
    import select
    import sys

    env = {
        "TERM": "xterm",
        "HOME": str(tmp_path),
        "XDG_CONFIG_HOME": str(tmp_path / "cfg"),
        "PATH": "",  # no ambient program can be started by the child
        "PYTHONPATH": str(ROOT / "src"),  # honored: the child runs without -I
    }
    pid, fd = pty.fork()
    if pid == 0:  # the child: curses runs on the pty slave
        try:
            os.execve(sys.executable, [sys.executable, "-c", CHILD], env)
        finally:
            os._exit(127)
    if before:
        os.write(fd, before)
    output = b""
    sent = not after
    try:
        while True:
            ready, _, _ = select.select([fd], [], [], 20)
            if not ready:
                pytest.fail(f"the viewer did not respond within 20 s: {output[-300:]!r}")
            try:
                chunk = os.read(fd, 65536)
            except OSError:  # the slave closed: the child is done
                break
            if not chunk:
                break
            output += chunk
            if not sent and b"AB-1" in output:
                os.write(fd, after)
                sent = True
    except BaseException:
        os.kill(pid, signal.SIGKILL)  # the pid captured above, never a pattern
        os.waitpid(pid, 0)
        raise
    finally:
        os.close(fd)
    _, status = os.waitpid(pid, 0)
    assert os.WIFEXITED(status), (status, output[-500:])
    return os.WEXITSTATUS(status), output


def test_the_real_curses_screen_draws_and_quits_on_a_pty(tmp_path: Path) -> None:
    """A smoke test on a real pty: the key is typed first, so nothing depends on timing."""
    code, output = run_on_pty(tmp_path, before=b"q")
    assert code == 0, output[-500:]
    assert b"AB-1" in output and b"ticketctl browse" in output
    assert b"<U+001B>" in output  # the escape in the title was made visible, not sent
    assert b"\x1b[31mticket" not in output
    assert RMCUP in output


def test_ctrl_c_on_a_pty_restores_the_terminal_and_ends_with_130(tmp_path: Path) -> None:
    code, output = run_on_pty(tmp_path, after=b"\x03")
    assert code == 130, output[-500:]
    assert RMCUP in output
    assert b"Traceback" not in output


def test_ctrl_backslash_on_a_pty_restores_the_terminal_and_does_not_hang(tmp_path: Path) -> None:
    code, output = run_on_pty(tmp_path, after=b"\x1c")  # SIGQUIT
    assert code == 130, output[-500:]
    assert RMCUP in output and b"Traceback" not in output


# --- round 1 of the review: pass-through, limits, restore, hostile fields --------------------


def config_text(sites: dict[str, FakeProvider], projects: dict[str, str], default: str) -> str:
    lines = [f"default_site: {default}", "sites:"]
    for name, provider in sites.items():
        lines += [
            f"  {name}:",
            f"    url: {provider.url}",
            f"    email: {SITE_EMAIL}",
            f"    token_env: {TOKEN_VAR}",
            "    allow_http: true",
            f"    projects: [{projects[name]}]",
        ]
    return "\n".join(lines) + "\n"


def runtime_source(config: Path, *argv: str) -> browsecmd.RuntimeSource:
    g, _ = extract_globals(["--config", str(config)])
    ns = build_parser().parse_args(["browse", "--project", "DEMO", *argv])
    return browsecmd.RuntimeSource(g, ns)


@pytest.fixture
def two_sites(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fake: FakeProvider, fake_factory: Any
) -> tuple[Path, FakeProvider, FakeProvider]:
    other = fake_factory(None)
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    monkeypatch.setenv(TOKEN_VAR, SITE_TOKEN)
    monkeypatch.delenv("TICKETCTL_READ_ONLY", raising=False)
    config = tmp_path / "config.yaml"
    config.write_text(
        config_text({"main": fake, "other": other}, {"main": "DEMO", "other": "OTH"}, "main")
    )
    config.chmod(0o600)
    return config, fake, other


def test_a_pane_is_read_from_the_site_of_the_ticket_not_the_default(
    two_sites: tuple[Path, FakeProvider, FakeProvider],
) -> None:
    config, main_site, other_site = two_sites
    source = runtime_source(config)
    text = source.pane("ticket", {"key": "DEMO-2", "site": "other"})
    assert "DEMO-2" in text
    assert other_site.calls and main_site.calls == []  # nothing reached the default site
    for pane in ("comments", "history"):
        source.pane(pane, {"key": "DEMO-2", "site": "other"})
    assert main_site.calls == []
    source.pane("ticket", {"key": "DEMO-2", "site": "main"})
    assert main_site.calls  # and the ticket's own site is the one that is used


@pytest.mark.parametrize("key", ["--help", "-x", "--", "-"])
def test_a_key_that_looks_like_a_flag_reaches_the_handler_as_one_positional(
    key: str, two_sites: tuple[Path, FakeProvider, FakeProvider], monkeypatch: pytest.MonkeyPatch
) -> None:
    config, main_site, _ = two_sites
    seen: list[Any] = []
    real = getcmd.get

    def spy(g: Any, a: Any) -> Any:
        seen.append(a)
        return real(g, a)

    monkeypatch.setattr(getcmd, "get", spy)
    source = runtime_source(config)
    b = Browser(source, {"tickets": [{"key": key, "site": "main"}], "next": None})
    press(b, "open")  # must neither exit the process nor print help
    assert b.mode == "detail" and not b.done
    assert [a.keys for a in seen] == [[key]]
    assert "error:" in b.message and main_site.calls == []  # refused as a key, nothing sent


def test_the_comment_and_history_reads_ask_for_the_newest_twenty(
    two_sites: tuple[Path, FakeProvider, FakeProvider], monkeypatch: pytest.MonkeyPatch
) -> None:
    config, main_site, _ = two_sites
    args: list[Any] = []
    real = commentscmd.comments

    def spy(g: Any, a: Any) -> Any:
        args.append(a)
        return real(g, a)

    monkeypatch.setattr(commentscmd, "comments", spy)
    source = runtime_source(config)
    source.pane("comments", {"key": "DEMO-1", "site": "main"})
    (a,) = args
    assert (a.key, a.limit, a.newest_first) == ("DEMO-1", browsecmd.PANE_ENTRIES, True)
    sent = [c for c in main_site.calls if c.path.endswith("/issue/DEMO-1/comment")]
    assert sent and {c.one("orderBy") for c in sent} == {"-created"}


def test_a_session_sends_no_count_request_for_a_page(
    rig: Rig, monkeypatch: pytest.MonkeyPatch
) -> None:
    scripted(monkeypatch, ["down", "down", "bottom", "reload"])
    code, _, _ = rig.run("browse", "--project", "DEMO", "--limit", "1")
    assert code == 0
    paths = [c.path for c in rig.fake.calls]
    assert paths.count("/rest/api/3/search/jql") >= 4  # pages of one ticket, then the reload
    assert not [p for p in paths if "count" in p]


def runtime_session(rig: Rig) -> browsecmd.RuntimeSource:
    return runtime_source(rig.config)


def add_comments(fake: FakeProvider, key: str, n: int) -> None:
    for i in range(n):
        when = fake.now
        fake.tickets[key].comments.append(
            {
                "id": str(40000 + i),
                "author": fake._user(ME_ACCOUNT),  # noqa: SLF001
                "updateAuthor": fake._user(ME_ACCOUNT),  # noqa: SLF001
                "body": {
                    "type": "doc",
                    "version": 1,
                    "content": [{"type": "paragraph", "content": [{"type": "text", "text": "c"}]}],
                },
                "_created": when,
                "_updated": when,
                "properties": {},
                "jsdPublic": True,
            }
        )


@pytest.mark.parametrize(("n", "shown", "more"), [(19, 19, False), (20, 20, False), (21, 20, True)])
def test_a_pane_holds_at_most_the_newest_twenty_entries(
    rig: Rig, n: int, shown: int, more: bool
) -> None:
    assert browsecmd.PANE_ENTRIES == 20
    add_comments(rig.fake, "DEMO-1", n)
    for i in range(n):
        rig.fake.add_history("DEMO-1", rig.fake.history_item("summary", f"a{i}", f"b{i}"))
    source = runtime_session(rig)
    ticket_ = {"key": "DEMO-1", "site": "main"}
    for pane, noun in (("comments", "comments"), ("history", "entries")):
        first = source.pane(pane, ticket_).split("\n")[0]
        expected = f"DEMO-1: {shown} of {n} {noun}" + (", more" if more else "")
        assert first == expected, first


# the pane limits at N - 1, N and N + 1 -----------------------------------------------------


def paged(total: int, size: int) -> list[list[dict[str, Any]]]:
    keys = [ticket(i) for i in range(total)]
    return [keys[i : i + size] for i in range(0, total, size)] or [[]]


@pytest.mark.parametrize("size", [500, 999])
@pytest.mark.parametrize("delta", [-1, 0, 1])
@pytest.mark.parametrize("how", ["down", "bottom"])
def test_the_ticket_cap_at_one_less_exactly_and_one_more(how: str, delta: int, size: int) -> None:
    total = MAX_TICKETS + delta
    b, _ = make(paged(total, size))
    if how == "bottom":
        press(b, "bottom")
    else:
        press(b, *["down"] * (total + 3))
    assert len(b.tickets) == min(total, MAX_TICKETS)
    assert b.capped is (delta > 0)
    assert b.next_cursor is None
    assert ("(limit)" in screen(b)[0]) is (delta > 0)
    assert b.selected == len(b.tickets) - 1


def test_the_pane_line_cap_at_one_less_exactly_and_one_more() -> None:
    n = browsecmd.MAX_PANE_LINES
    for count in (n - 1, n):
        lines = browsecmd.clean_lines("\n".join(["x"] * count))
        assert len(lines) == count and "not shown" not in lines[-1]
    lines = browsecmd.clean_lines("\n".join(["x"] * (n + 1)))
    assert len(lines) == n + 1 and lines[:n] == ["x"] * n and "not shown" in lines[-1]


def test_a_line_is_cut_at_the_line_cap() -> None:
    n = browsecmd.MAX_LINE_CHARS
    assert [len(browsecmd.clean_lines("a" * c)[0]) for c in (n - 1, n, n + 1)] == [n - 1, n, n]


def test_a_pane_text_is_cut_at_the_character_cap() -> None:
    n = browsecmd.MAX_PANE_CHARS
    for chars, kept in ((n - 1, True), (n, True), (n + 1, False)):
        text = "a" * (chars - 2) + "\nb"
        assert len(text) == chars
        assert ("b" in browsecmd.clean_lines(text)) is kept, chars


def test_a_row_is_cut_at_the_row_cap() -> None:
    n = browsecmd.MAX_ROW_CHARS
    rows = [
        browsecmd.ticket_row({"key": "K", "title": "t" * (total - 3)})
        for total in (n - 1, n, n + 1)
    ]
    assert [len(r) for r in rows] == [n - 1, n, n]


def test_a_status_message_is_cut_at_the_message_cap() -> None:
    n = browsecmd.MAX_MESSAGE_CHARS
    b, source = make([[ticket(1)], [ticket(2)]], fail=TicketctlError("unavailable", "m"))
    press(b, "down")
    prefix = len(b.message) - 1  # "error: unavailable...: " around the one-character message
    lengths = []
    for total in (n - 1, n, n + 1):
        source.fail = TicketctlError("unavailable", "m" * (total - prefix))
        press(b, "down")
        lengths.append(len(b.message))
    assert lengths == [n - 1, n, n]
    assert len(screen(b, (24, n + 50))[-1]) == n


# hostile text in the other fields, in every frame --------------------------------------------


def frames_are_clean(w: FakeWindow) -> None:
    assert w.frames
    for frame in w.frames:
        for line in frame:
            assert not bad(line), line
            assert browsecmd.display_width(line) <= w.size[1] - 1


@pytest.mark.parametrize("field", ["key", "status", "site"])
@pytest.mark.parametrize("text", HOSTILE)
def test_hostile_ticket_fields_never_reach_a_frame_or_the_busy_message(
    field: str, text: str
) -> None:
    t = ticket(1)
    if field == "key":
        t["key"] = text
    elif field == "status":
        t["status"] = {"name": text, "category": "new"}
    else:
        t["site"] = text
    b, _ = make([[t]])
    w = FakeWindow([ord("l"), ord("2"), ord("3"), ord("q"), ord("q")])
    browsecmd.run_loop(w, b, curses)
    frames_are_clean(w)
    assert any(frame[-1].startswith("reading ") for frame in w.frames)  # the busy frame is covered


@pytest.mark.parametrize("text", HOSTILE)
def test_a_hostile_footer_alone_is_sanitized(text: str) -> None:
    b, _ = make([[ticket(1)]])
    b.message = text
    for size in [(24, 80), (5, 13), (60, 200)]:
        for line in screen(b, size):
            assert not bad(line)


@pytest.mark.parametrize("text", HOSTILE)
def test_a_hostile_error_alone_is_sanitized_in_every_frame(text: str) -> None:
    b, source = make([[ticket(1)], [ticket(2)]], fail=TicketctlError("rejected", text))
    w = FakeWindow([ord("j"), ord("l"), ord("2"), ord("q"), ord("q")])
    browsecmd.run_loop(w, b, curses)
    frames_are_clean(w)
    assert any("error:" in frame[-1] for frame in w.frames)
    b, source = make([[ticket(1)]])
    source.fail = RuntimeError(text)  # an unexpected exception shows its class only
    press(b, "open")
    assert not [c for line in screen(b) for c in bad(line)]


# --- the restore of the terminal and the interrupt -------------------------------------------


class ScreenCurses:
    """A stand-in for the curses module as ``run_screen`` uses it."""

    error = curses.error

    def __init__(
        self, window: FakeWindow, *, cursor_fails: bool = False, teardown_fails: bool = False
    ) -> None:
        self.window = window
        self.cursor_fails = cursor_fails
        self.teardown_fails = teardown_fails
        self.cursor_calls: list[int] = []
        self.restored = 0
        self.quit_handler: Any = None

    def curs_set(self, visibility: int) -> None:
        self.cursor_calls.append(visibility)
        if self.cursor_fails:
            raise curses.error("this terminal has no cursor control")

    def wrapper(self, func: Any) -> Any:
        self.quit_handler = signal.getsignal(signal.SIGQUIT)
        try:
            return func(self.window)
        finally:
            self.restored += 1  # endwin
            if self.teardown_fails:
                raise curses.error("nocbreak() returned ERR")


class Interrupting(FakeWindow):
    def getch(self) -> int:
        raise KeyboardInterrupt


def test_the_screen_starts_with_keypad_on_and_survives_a_terminal_with_no_cursor_control() -> None:
    b, _ = make([[ticket(1)]])
    w = FakeWindow([ord("q")])
    fake_curses = ScreenCurses(w, cursor_fails=True)
    browsecmd.run_screen(b, fake_curses)
    assert w.keypad_calls == [True] and fake_curses.cursor_calls == [0]
    assert b.done and fake_curses.restored == 1


def test_an_interrupt_in_the_loop_restores_the_terminal_and_propagates() -> None:
    b, _ = make([[ticket(1)]])
    fake_curses = ScreenCurses(Interrupting([]))
    with pytest.raises(KeyboardInterrupt):
        browsecmd.run_screen(b, fake_curses)
    assert fake_curses.restored == 1


def test_an_interrupt_in_a_read_restores_the_terminal_and_propagates() -> None:
    b, source = make([[ticket(1)]])
    source.fail = KeyboardInterrupt()
    fake_curses = ScreenCurses(FakeWindow([ord("l")]))
    with pytest.raises(KeyboardInterrupt):
        browsecmd.run_screen(b, fake_curses)
    assert fake_curses.restored == 1


def test_any_other_exception_in_the_loop_restores_the_terminal_too() -> None:
    b, _ = make([[ticket(1)]])

    def boom(action: str, size: tuple[int, int]) -> None:
        raise RuntimeError("from the loop")

    b.handle = boom  # type: ignore[method-assign]
    fake_curses = ScreenCurses(FakeWindow([ord("j")]))
    with pytest.raises(RuntimeError, match="from the loop"):
        browsecmd.run_screen(b, fake_curses)
    assert fake_curses.restored == 1


def test_ctrl_c_in_a_pane_read_is_exit_130_through_the_command(
    rig: Rig, monkeypatch: pytest.MonkeyPatch
) -> None:
    scripted(monkeypatch, ["open"])

    def interrupted(self: Any, pane: str, ticket_: Any) -> str:
        raise KeyboardInterrupt

    monkeypatch.setattr(browsecmd.RuntimeSource, "pane", interrupted)
    code, out, err = rig.run("browse", "--project", "DEMO")
    assert (code, out, err) == (130, "", "ticketctl: interrupted\n")


def test_ctrl_c_in_the_key_loop_is_exit_130_through_the_command(
    rig: Rig, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(browsecmd, "require_terminal", lambda env: None)
    real = browsecmd.run_screen
    holder: list[ScreenCurses] = []

    def screen_with_interrupt(browser: Browser) -> None:
        holder.append(ScreenCurses(Interrupting([])))
        real(browser, holder[0])

    monkeypatch.setattr(browsecmd, "run_screen", screen_with_interrupt)
    code, out, err = rig.run("browse", "--project", "DEMO")
    assert (code, out, err) == (130, "", "ticketctl: interrupted\n")
    assert holder[0].restored == 1


def test_sigquit_is_handled_during_the_screen_and_the_old_handler_comes_back() -> None:
    sentinel = lambda signum, frame: None  # noqa: E731
    old = signal.signal(signal.SIGQUIT, sentinel)
    try:
        b, _ = make([[ticket(1)]])
        fake_curses = ScreenCurses(FakeWindow([ord("q")]))
        browsecmd.run_screen(b, fake_curses)
        assert fake_curses.quit_handler is browsecmd._interrupt  # noqa: SLF001
        assert signal.getsignal(signal.SIGQUIT) is sentinel
        # the handler ends the loop like Ctrl-C, and is put back on that path too
        b, _ = make([[ticket(1)]])
        fake_curses = ScreenCurses(Interrupting([]))
        with pytest.raises(KeyboardInterrupt):
            browsecmd.run_screen(b, fake_curses)
        assert signal.getsignal(signal.SIGQUIT) is sentinel
        with pytest.raises(KeyboardInterrupt):
            browsecmd._interrupt(signal.SIGQUIT, None)  # noqa: SLF001
    finally:
        signal.signal(signal.SIGQUIT, old)


@pytest.mark.parametrize("keys", [[], [ord("q")]])
def test_a_teardown_error_after_the_loop_ended_is_a_clean_end(keys: list[int]) -> None:
    b, _ = make([[ticket(1)]])
    fake_curses = ScreenCurses(FakeWindow(keys), teardown_fails=True)
    browsecmd.run_screen(b, fake_curses)  # no exception: the input ended or the user quit
    assert fake_curses.restored == 1


def test_a_curses_error_before_the_loop_ended_still_surfaces() -> None:
    b, _ = make([[ticket(1)]])

    class Failing(FakeWindow):
        def getch(self) -> int:
            raise curses.error("getch failed")

    with pytest.raises(curses.error):
        browsecmd.run_screen(b, ScreenCurses(Failing([]), teardown_fails=True))


# --- reload and the wrap memo ----------------------------------------------------------------


def test_reload_drops_the_held_panes_so_a_reopened_ticket_is_read_again() -> None:
    b, source = make([[ticket(1)]])
    press(b, "open", "back")
    assert source.panes == [("ticket", "AB-1")]
    press(b, "reload", "open")
    assert source.panes == [("ticket", "AB-1")] * 2


def test_a_pane_is_wrapped_once_for_several_views_and_a_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    b, source = make([[ticket(1)]])
    source.text["ticket"] = "\n".join(f"line {i}" for i in range(100))
    calls: list[str] = []
    real = browsecmd.wrap

    def counting(text: str, width: int) -> list[str]:
        calls.append(text)
        return real(text, width)

    monkeypatch.setattr(browsecmd, "wrap", counting)
    press(b, "open")
    screen(b)
    first = len(calls)
    assert first == 100
    screen(b)
    screen(b)
    press(b, "down")  # a key that scrolls
    screen(b)
    assert len(calls) == first
    press(b, "pane_comments")  # another pane has its own text: it is wrapped, once
    screen(b)
    screen(b)
    assert len(calls) == first + 1
    press(b, "pane_ticket")
    screen(b)
    assert len(calls) == first + 1 + 100  # back to the first pane: wrapped again, then held
    screen(b, (24, 60))  # a different width is a different wrap
    assert len(calls) == first + 1 + 200


def test_a_changed_pane_text_is_wrapped_again() -> None:
    b, source = make([[ticket(1)]])
    source.text["ticket"] = "first text"
    press(b, "open")
    assert "first text" in screen(b)[1]
    source.text["ticket"] = "second text"
    press(b, "reload", "open")
    assert "second text" in screen(b)[1] and "first text" not in screen(b)[1]
