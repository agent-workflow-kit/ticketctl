"""``browse`` (#45): the read-only terminal viewer.

The view state is tested with a synthetic source and no terminal; the curses loop is driven through
a fake window; the command is run end to end against the fake provider with the screen replaced by
scripted keys. No test needs a real terminal and none waits on a clock.
"""

from __future__ import annotations

import ast
import curses
import json
import unicodedata
from pathlib import Path
from typing import Any

import pytest

from tests.fake_provider import SITE_EMAIL, SITE_TOKEN, FakeProvider
from tests.golden import validator
from ticketctl import browsecmd
from ticketctl.browsecmd import ACTIONS, MAX_TICKETS, Browser
from ticketctl.cli import main
from ticketctl.errors import TicketctlError

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
    assert b.scroll == 100 - (24 - 3)
    press(b, "down", "page_down")
    assert b.scroll == 100 - (24 - 3)
    last = screen(b)[-2]
    assert last == "line 99"
    press(b, "page_up", "top")
    assert b.scroll == 0 and screen(b)[1] == "line 0"


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


def test_the_browse_module_imports_no_write_command_and_no_network_code() -> None:
    tree = ast.parse((ROOT / "src" / "ticketctl" / "browsecmd.py").read_text())
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            imported |= {a.name for a in node.names} | {node.module or ""}
        elif isinstance(node, ast.Import):
            imported |= {a.name for a in node.names}
    forbidden = {
        "writeflow", "transitioncmd", "commentcmd", "commentedit", "createcmd", "updatecmd",
        "sprintcmd", "linkcmd", "attachmentcmd", "gateway", "http", "http.client", "urllib",
        "socket", "subprocess", "requests",
    }  # fmt: skip
    assert not (imported & forbidden), imported & forbidden


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


def test_a_cell_that_cannot_be_written_is_ignored() -> None:
    b, _ = make([[ticket(1)]])
    w = FakeWindow([ord("q")], fail_cells=True)
    browsecmd.run_loop(w, b, curses)
    assert b.done


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
    code, out, err = rig.run("--json", "browse", "--project", "DEMO")
    assert code == 2  # --json is refused
    code, out, err = rig.run("browse", "--jql", "this is not jql ((")
    assert code != 0 and seen == [] and "error:" in err


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
browsecmd.run_screen(browsecmd.Browser(source, source.search(None)))
"""


def test_the_real_curses_screen_draws_and_quits_on_a_pty(tmp_path: Path) -> None:
    """A smoke test on a real pty: the key is typed first, so nothing depends on timing."""
    import os
    import pty
    import select
    import sys

    env = {
        "TERM": "xterm",
        "HOME": str(tmp_path),
        "XDG_CONFIG_HOME": str(tmp_path / "cfg"),
        "PATH": "",
        "PYTHONPATH": str(ROOT / "src"),
    }
    pid, fd = pty.fork()
    if pid == 0:  # the child: curses runs on the pty slave
        try:
            os.execve(sys.executable, [sys.executable, "-I", "-c", CHILD], env)
        finally:
            os._exit(127)
    os.write(fd, b"q")
    output = b""
    while True:
        ready, _, _ = select.select([fd], [], [], 20)
        if not ready:
            os.waitpid(pid, os.WNOHANG)
            pytest.fail("the viewer did not finish after `q`")
        try:
            chunk = os.read(fd, 65536)
        except OSError:  # the slave closed: the child is done
            break
        if not chunk:
            break
        output += chunk
    _, status = os.waitpid(pid, 0)
    os.close(fd)
    assert os.WIFEXITED(status) and os.WEXITSTATUS(status) == 0, output[-500:]
    assert b"AB-1" in output and b"ticketctl browse" in output
    assert b"<U+001B>" in output  # the escape in the title was made visible, not sent
    assert b"\x1b[31mticket" not in output
