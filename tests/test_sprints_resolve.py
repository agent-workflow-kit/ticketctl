"""``sprints resolve open|next`` end to end, and its sameness with ``sprint add`` (issue #70)."""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from tests.fake_provider import SITE_TOKEN, FakeProvider, Out
from tests.golden import assert_golden, validator
from tests.test_sprint_write import SITE_ENV_VAR, Cli, override, set_sprint

P = ("--project", "DEMO")
Setup = Callable[[Cli], None]


@pytest.fixture
def cli(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: Any, fake: FakeProvider) -> Cli:
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    monkeypatch.setenv(SITE_ENV_VAR, SITE_TOKEN)
    monkeypatch.setenv("PATH", "/nonexistent")  # a test can never start a real program by PATH
    return Cli(tmp_path, fake, capsys)


def _two_active(c: Cli) -> None:
    set_sprint(c.fake, 4, name="Sprint four customfield_90001", state="active")


def _no_active(c: Cli) -> None:
    c.fake.sprints[2]["state"] = "closed"


def _dated(c: Cli) -> None:
    set_sprint(c.fake, 5, start="2026-04-20T15:00:00.000Z", end="2026-05-04T15:00:00.000Z")
    set_sprint(c.fake, 6, start="2026-04-06T15:00:00.000Z", end="2026-04-20T15:00:00.000Z")
    set_sprint(c.fake, 7)


def _undated(c: Cli) -> None:
    set_sprint(c.fake, 9)
    set_sprint(c.fake, 8)


def _equal_starts(c: Cli) -> None:
    for sid in (8, 6):
        set_sprint(c.fake, sid, start="2026-04-06T15:00:00.000Z", end="2026-04-20T15:00:00.000Z")


def _no_future(c: Cli) -> None:
    c.fake.sprints[3]["state"] = "closed"


def _future_cap(c: Cli) -> None:
    def page(call: Any, *groups: str) -> Out:
        start = int(call.one("startAt", "0"))
        values = [
            {"id": start + i + 100, "name": f"S{start + i}", "state": "future", "originBoardId": 1}
            for i in range(50)
        ]
        return Out(200, {"isLast": False, "values": values})

    override(c.fake, "GET", r"/board/(\d+)/sprint", page)


def _page_loop_cut(c: Cli) -> None:
    def page(call: Any, *groups: str) -> Out:
        start = int(call.one("startAt", "0"))
        values = [{"id": start + 1000, "name": "old", "state": "closed", "originBoardId": 1}]
        return Out(200, {"isLast": False, "values": values})

    override(c.fake, "GET", r"/board/(\d+)/sprint", page)


def _unreadable(c: Cli) -> None:
    def page(call: Any, *groups: str) -> Out:
        values = [
            {"id": 3, "name": "ok", "state": "future", "originBoardId": 1},
            {"id": 2, "name": "ok", "state": "active", "originBoardId": 1},
            {"id": "x", "state": call.one("state", "")},
        ]
        return Out(200, {"isLast": True, "values": values})

    override(c.fake, "GET", r"/board/(\d+)/sprint", page)


def _kanban(c: Cli) -> None:
    override(c.fake, "GET", r"/board/(\d+)/sprint", lambda call, *g: Out(400, {}))


CASES: list[tuple[str, str, Setup | None]] = [
    ("open", "open", None),
    ("open", "two-active", _two_active),
    ("open", "no-active", _no_active),
    ("open", "unreadable", _unreadable),
    ("open", "page-loop-cut", _page_loop_cut),
    ("open", "kanban", _kanban),
    ("next", "next", None),
    ("next", "dated", _dated),
    ("next", "undated", _undated),
    ("next", "equal-starts", _equal_starts),
    ("next", "no-future", _no_future),
    ("next", "future-cap", _future_cap),
    ("next", "unreadable", _unreadable),
    ("next", "page-loop-cut", _page_loop_cut),
    ("next", "kanban", _kanban),
]


def _normal(c: Cli, doc: dict[str, Any]) -> dict[str, Any]:
    return c.norm(doc)


@pytest.mark.parametrize(("selector", "case", "setup"), CASES, ids=lambda v: str(v)[:24])
def test_resolve_and_sprint_add_agree_on_every_case(
    cli: Cli, selector: str, case: str, setup: Setup | None
) -> None:
    if setup:
        setup(cli)
    add_code, add = cli.doc("--dry-run", "sprint", "add", "DEMO-1", "--sprint", selector)
    res_code, res = cli.doc("sprints", "resolve", selector, "--project", "DEMO")
    assert res_code == add_code
    if add_code != 0:
        assert res["error"] == add["error"]  # code, reason, message, details alike
        validator("cli/error.json").validate(res)
    else:
        (step,) = add["steps"]
        assert res["sprint"]["id"] == int(step["sprint"])
        validator("cli/sprints-resolve.json").validate(res)


def test_resolve_open_prints_the_show_object(cli: Cli) -> None:
    code, doc = cli.doc("sprints", "resolve", "open", *P)
    assert code == 0
    assert doc["sprint"] == {
        "id": 2, "name": "Sprint two", "state": "active", "board": 1,
        "start": "2026-02-16T15:00:00Z", "end": "2026-03-02T15:00:00Z",
    }  # fmt: skip
    _, shown = cli.doc("sprints", "show", "2", *P)
    assert doc == shown
    validator("cli/sprints-resolve.json").validate(doc)
    assert_golden("cli", "sprints-resolve", "open", cli.norm(doc))


def test_resolve_next_golden(cli: Cli) -> None:
    code, doc = cli.doc("sprints", "resolve", "next", "--board", "1", *P)
    assert code == 0 and doc["sprint"]["id"] == 3 and doc["sprint"]["state"] == "future"
    assert_golden("cli", "sprints-resolve", "next", cli.norm(doc))


def test_resolve_is_read_only(cli: Cli) -> None:
    for selector in ("open", "next"):
        assert cli.doc("sprints", "resolve", selector, *P)[0] == 0
    assert cli.writes() == []
    assert not (cli.root / "state" / "ticketctl" / "audit.jsonl").exists()
    assert not (cli.root / "state" / "ticketctl").exists() or not list(
        (cli.root / "state" / "ticketctl").glob("*.lock")
    )


def test_resolve_names_are_sanitized_and_hide_field_ids(cli: Cli) -> None:
    cli.fake.sprints[2]["name"] = "Sprint \x1b[31mred customfield_90001‮"
    code, doc = cli.doc("sprints", "resolve", "open", *P)
    assert code == 0
    assert "\x1b" not in doc["sprint"]["name"] and "‮" not in doc["sprint"]["name"]
    assert "customfield_90001" not in json.dumps(doc)


def test_resolve_ambiguous_candidates_are_sanitized(cli: Cli) -> None:
    _two_active(cli)
    code, doc = cli.doc("sprints", "resolve", "open", *P)
    assert (code, doc["error"]["code"]) == (21, "ambiguous_sprint")
    assert "customfield_90001" not in json.dumps(doc)


@pytest.mark.parametrize("word", ["2", "OPEN", "", "closed", "--board"])
def test_resolve_takes_only_open_or_next(cli: Cli, word: str) -> None:
    code, doc = cli.doc("sprints", "resolve", word, *P)
    assert code == 2 and doc["error"]["reason"] == "invalid_argument"


def test_resolve_needs_a_selector(cli: Cli) -> None:
    code, doc = cli.doc("sprints", "resolve")
    assert code == 2


def test_resolve_board_rules_match_sprints_list(cli: Cli) -> None:
    code, doc = cli.doc("sprints", "resolve", "open", "--board", "No Such Board", *P)
    assert (code, doc["error"]["code"], doc["error"]["details"]["option"]) == (
        10, "not_found", "board",
    )  # fmt: skip
    code, doc = cli.doc("sprints", "resolve", "open", "--board", "", *P)
    assert (code, doc["error"]["reason"]) == (22, "invalid_value")


def test_resolve_is_a_read_command_and_refuses_write_flags(cli: Cli) -> None:
    code, doc = cli.doc("--yes", "sprints", "resolve", "open", *P)
    assert code == 2 and doc["error"]["reason"] == "invalid_argument"


def test_resolve_human_output_is_one_line(cli: Cli) -> None:
    code, out, _ = cli.run("sprints", "resolve", "open", *P)
    assert code == 0 and out.startswith("2 Sprint two [active] board 1")
    assert out.count("\n") == 1
