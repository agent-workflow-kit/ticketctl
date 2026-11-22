"""The live-suite harness, proved against the fake provider (issues #37 and #38).

No real site is involved and no flag is needed: these are ordinary tests that run in CI. They prove
the skip logic, the redaction of the findings file, the request budget, and that the closer closes
every ticket the run created even when a test body raises. The live tests themselves
(``test_live_read.py``, ``test_live_write.py``) run only with ``TICKETCTL_LIVE=1`` by a human.
"""

from __future__ import annotations

import ast
import json
import os
import re
import subprocess
import sys
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from tests.fake_provider import SITE_EMAIL, FakeProvider
from tests.fake_provider import SITE_TOKEN as FAKE_SITE_KEY
from tests.live import checklist
from tests.live.conftest import CLEANUP_RESERVE, READ_LIMIT, WRITE_LIMIT
from tests.live.support import (
    ENV_FINDINGS,
    ENV_KEY_VAR,
    ENV_LIVE,
    ENV_PASS,
    ENV_PROJECT,
    ENV_WRITE,
    MAX_COMMENTS_PER_TICKET,
    MAX_TEST_TICKETS,
    SWEEP_RETRIES,
    SWEEP_WAIT_SECONDS,
    Budget,
    BudgetExceeded,
    Ledger,
    LiveCli,
    LiveEnv,
    Recorder,
    Redactor,
    Result,
    StdinWriteRefused,
    TooManyComments,
    TooManyTickets,
    child_env,
    command_name,
    default_findings_dir,
    explain,
    masked_env_values,
    shape,
    skip_reason,
)
from tests.test_fixture_scan import files, scan

REPO = Path(__file__).resolve().parents[2]
LIVE_DIR = Path(__file__).resolve().parent
KEY_VAR_NAME = "FAKE_SITE_KEY_ENV"


# ---- skip logic --------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("env", "write", "needle"),
    [
        ({}, False, "TICKETCTL_LIVE=1"),
        ({ENV_LIVE: "0"}, False, "TICKETCTL_LIVE=1"),
        ({ENV_LIVE: "true"}, False, "TICKETCTL_LIVE=1"),
        ({ENV_LIVE: "1", "CI": "true"}, False, "CI"),
        ({ENV_LIVE: "1"}, True, "TICKETCTL_LIVE_WRITE=1"),
        ({ENV_LIVE: "1", ENV_WRITE: "yes", ENV_PROJECT: "ABC"}, True, "TICKETCTL_LIVE_WRITE=1"),
        ({ENV_LIVE: "1", ENV_WRITE: "1"}, True, ENV_PROJECT),
        ({ENV_LIVE: "1", ENV_WRITE: "1", ENV_PROJECT: "not a key"}, True, ENV_PROJECT),
        ({ENV_LIVE: "1", ENV_WRITE: "1", ENV_PROJECT: "A;B"}, True, ENV_PROJECT),
        ({ENV_WRITE: "1", ENV_PROJECT: "ABC"}, True, "TICKETCTL_LIVE=1"),
    ],
)
def test_the_suites_skip_without_their_flags(env: dict[str, str], write: bool, needle: str) -> None:
    reason = skip_reason(env, write=write)
    assert reason is not None and needle in reason


def _gated(**more: str) -> dict[str, str]:
    """A gate environment with the key variable named and set (the fake value)."""
    return {ENV_LIVE: "1", ENV_KEY_VAR: KEY_VAR_NAME, KEY_VAR_NAME: FAKE_SITE_KEY, **more}


def test_the_suites_run_only_with_every_flag() -> None:
    assert skip_reason(_gated(), write=False) is None
    assert skip_reason(_gated(**{ENV_WRITE: "1", ENV_PROJECT: "ABC"}), write=True) is None
    # the write flag alone never turns the read suite on
    assert skip_reason({ENV_WRITE: "1"}, write=False) is not None


@pytest.mark.parametrize("write", [False, True])
@pytest.mark.parametrize(
    "change",
    [
        {ENV_KEY_VAR: None},
        {ENV_KEY_VAR: ""},
        {ENV_KEY_VAR: "HOME"},
        {ENV_KEY_VAR: "TICKETCTL_ANYTHING"},
        {ENV_KEY_VAR: "has space"},
        {KEY_VAR_NAME: None},
        {KEY_VAR_NAME: ""},
    ],
)
def test_both_suites_need_the_key_variable_named_and_set(
    write: bool, change: dict[str, str | None]
) -> None:
    env = _gated(**{ENV_WRITE: "1", ENV_PROJECT: "ABC"})
    for name, value in change.items():
        if value is None:
            del env[name]
        else:
            env[name] = value
    reason = skip_reason(env, write=write)
    assert reason is not None
    assert re.search(ENV_KEY_VAR, reason) or re.search(KEY_VAR_NAME, reason)
    assert not re.search(re.escape(FAKE_SITE_KEY), reason)
    assert not re.search("has space", reason)


def test_a_pytest_run_without_the_flag_skips_every_live_test(tmp_path: Path) -> None:
    """The real collection hook, in a child pytest with a scrubbed environment: all skipped."""
    env = {
        "HOME": str(tmp_path),
        "PYTHONPATH": os.pathsep.join(sys.path),
        "PATH": "",
        "PYTHONDONTWRITEBYTECODE": "1",
    }
    done = subprocess.run(  # noqa: S603 - the interpreter running the tests
        [
            sys.executable,
            "-m",
            "pytest",
            "-m",
            "live",
            "-rs",
            "-p",
            "no:cacheprovider",
            "tests/live/test_live_read.py",
            "tests/live/test_live_write.py",
        ],  # fmt: skip
        cwd=REPO,
        env=env,
        capture_output=True,
        text=True,
        timeout=300,
        check=False,
    )
    out = done.stdout + done.stderr
    assert done.returncode == 0, out
    assert "passed" not in out and "failed" not in out and "error" not in out.lower(), out
    assert "skipped" in out and "TICKETCTL_LIVE=1" in out


def test_live_tests_are_deselected_by_default(tmp_path: Path) -> None:
    env = {"HOME": str(tmp_path), "PYTHONPATH": os.pathsep.join(sys.path), "PATH": ""}
    done = subprocess.run(  # noqa: S603 - the interpreter running the tests
        [
            sys.executable,
            "-m",
            "pytest",
            "-p",
            "no:cacheprovider",
            "--collect-only",
            "-q",
            "tests/live",
        ],  # fmt: skip
        cwd=REPO,
        env=env,
        capture_output=True,
        text=True,
        timeout=300,
        check=False,
    )
    assert done.returncode == 0, done.stdout + done.stderr
    assert "test_live_read" not in done.stdout and "test_live_write" not in done.stdout
    assert "test_harness" in done.stdout


# ---- environment -------------------------------------------------------------------------------


def test_the_probe_targets_come_from_the_environment_only() -> None:
    empty = LiveEnv.from_env({})
    assert not (empty.site or empty.project or empty.ticket or empty.board or empty.config)
    env = LiveEnv.from_env({"TICKETCTL_LIVE_TICKET": "ABC-12", "TICKETCTL_LIVE_BOARD": "7"})
    assert env.ticket == "ABC-12" and env.board == "7" and env.project_key() == "ABC"
    assert env.literals() == ["ABC-12", "ABC", "7"]


@pytest.mark.parametrize(
    "bad",
    [
        {"TICKETCTL_LIVE_TICKET": "abc"},
        {"TICKETCTL_LIVE_TICKET": "ABC-1; rm"},
        {"TICKETCTL_LIVE_PROJECT": "A B"},
        {"TICKETCTL_LIVE_BOARD": "seven"},
        {"TICKETCTL_LIVE_SITE": "../x"},
        {"TICKETCTL_LIVE_TOKEN_ENV": "has space"},
        {ENV_KEY_VAR: "XDG_STATE_HOME"},
        {ENV_KEY_VAR: "XDG_CONFIG_HOME"},
        {ENV_KEY_VAR: "HOME"},
        {ENV_KEY_VAR: "PATH"},
        {ENV_KEY_VAR: "TICKETCTL_LIVE"},
        {ENV_KEY_VAR: "TICKETCTL_LIVE_WRITE"},
        {ENV_KEY_VAR: "TICKETCTL_ANYTHING"},
        {ENV_PASS: "FINE_NAME,HOME"},
        {ENV_PASS: "has space"},
    ],
)
def test_a_malformed_probe_target_is_refused(bad: dict[str, str]) -> None:
    with pytest.raises(ValueError):
        LiveEnv.from_env(bad)


# ---- redaction and the findings file -----------------------------------------------------------

FAKE_KEY_VALUE = "tok-" + "a1b2c3d4" * 3
ACCOUNT = "5b10a2844c20165700ede21" + "f"  # a real-looking id, built so the scan stays quiet
ACCOUNT_UUID = "557058:" + "f58131cb-b67d-43c7-b30d-6b58d40bd077"


def _redactor() -> Redactor:
    return Redactor(["ABC", "tracker-literal"])


@pytest.mark.parametrize(
    ("raw", "gone"),
    [
        ("Authorization: Basic dXNlcjp0b2tlbg==", "dXNlcjp0b2tlbg"),
        ("Authorization: Bearer abcDEF123456", "abcDEF123456"),
        ("mail me at jane.doe@example.test now", "jane.doe"),
        ("see https://tracker.example.test/browse/ABC-12?x=1", "browse"),
        ("host tracker.example.test answered", "tracker.example"),
        ("ticket ABC-12 and XYZ-9", "XYZ-9"),  # scan: allow
        (f"account {ACCOUNT} here", ACCOUNT),
        (f"account {ACCOUNT_UUID}", "f58131cb"),
        ("opaque " + "Zm9vYmFy" * 6, "Zm9vYmFy"),
        ("a tracker-literal appears", "tracker-literal"),
        ("project ABC", "ABC"),
    ],
)
def test_the_redactor_removes_what_must_not_be_kept(raw: str, gone: str) -> None:
    assert not re.search(re.escape(gone), _redactor().text(raw))


def test_the_redactor_keeps_plain_facts_and_walks_structures() -> None:
    r = _redactor()
    assert r.text("exit 22 invalid_query on page 2 of 3") == "exit 22 invalid_query on page 2 of 3"
    assert r.text("ticketctl.meta property accepted") == "ticketctl.meta property accepted"
    out = r.any({"k DEMO-1": ["a@b.example", {"n": 3, "ok": True, "x": None}], "s": {"b", "a"}})
    assert out == {"k <KEY>": ["<email>", {"n": 3, "ok": True, "x": None}], "s": ["a", "b"]}


def test_secret_looking_environment_values_are_masked_by_value_not_name() -> None:
    env = {"MY_API_TOKEN": FAKE_KEY_VALUE, "SHORT_TOKEN": "x", "OTHER": "visible-value-12"}
    assert masked_env_values(env) == [FAKE_KEY_VALUE]
    masked = Redactor(masked_env_values(env)).text(f"saw {FAKE_KEY_VALUE} here")
    assert not re.search(re.escape(FAKE_KEY_VALUE), masked)


def test_shape_keeps_structure_and_drops_every_value() -> None:
    doc = {"a": "secret text", "b": [{"c": 1, "d": None}], "e": [], "f": {"g": True}}
    assert shape(doc) == {"a": "str", "b": [{"c": "int", "d": "null"}], "e": [], "f": {"g": "bool"}}


def test_the_findings_file_holds_no_token_email_host_or_key(tmp_path: Path) -> None:
    rec = Recorder(
        "read",
        tmp_path / "out",
        Redactor(["tracker-literal", FAKE_KEY_VALUE]),
        clock=lambda: datetime(2026, 11, 21, 8, 30, 5, tzinfo=UTC),
    )
    dirty = (
        f"{FAKE_KEY_VALUE} jane.doe@example.test https://tracker.example.test/rest ABC-77 "
        f"{ACCOUNT} tracker-literal"
    )
    rec.record("L01", f"title {dirty}", {"body": dirty, "list": [dirty]})
    rec.differs("L17", "shape", {"code": 404}, expected=f"expected {dirty}")
    path = rec.write()
    assert path.name == "findings-read-20261121T083005Z.json"
    assert path.with_suffix(".md").exists()
    text = path.read_text() + path.with_suffix(".md").read_text()
    for needle in (
        FAKE_KEY_VALUE, "jane.doe", "example.test", "ABC-77", ACCOUNT,
        "tracker-literal",
    ):  # fmt: skip
        assert not re.search(re.escape(needle), text), "a masked value reached the findings file"
    doc = json.loads(path.read_text())
    assert doc["date"] == "2026-11-21" and [f["status"] for f in doc["findings"]] == [
        "observed",
        "differs",
    ]
    assert "not verified facts" in doc["note"]
    assert path.stat().st_mode & 0o777 == 0o600
    assert "[differs] L17" in rec.summary() and not re.search(
        re.escape(FAKE_KEY_VALUE), rec.summary()
    )


def test_an_unknown_finding_status_is_refused(tmp_path: Path) -> None:
    rec = Recorder("read", tmp_path, Redactor())
    with pytest.raises(ValueError):
        rec.record("L01", "t", {}, status="verified")


def test_the_findings_directory_is_git_ignored_and_outside_tests() -> None:
    default = default_findings_dir({})
    assert default == REPO / "live-findings"
    assert "live-findings/" in (REPO / ".gitignore").read_text().splitlines()
    assert default_findings_dir({ENV_FINDINGS: "/some/dir"}) == Path("/some/dir")
    assert not str(default).startswith(str(REPO / "tests"))


# ---- a CLI over the fake provider --------------------------------------------------------------


@pytest.fixture
def world(tmp_path: Path, fake: FakeProvider) -> Iterator[tuple[LiveCli, LiveEnv, Path]]:
    config = tmp_path / "config.yaml"
    config.write_text(
        f"sites:\n  main:\n    url: {fake.url}\n    email: {SITE_EMAIL}\n"
        f"    token_env: {KEY_VAR_NAME}\n    allow_http: true\n    projects: [DEMO]\n"
    )
    config.chmod(0o600)
    home = tmp_path / "home"
    home.mkdir()
    env = {
        "HOME": str(home),
        "XDG_CONFIG_HOME": str(home / ".config"),
        "PATH": "",
        "PYTHONPATH": os.pathsep.join(sys.path),
        KEY_VAR_NAME: FAKE_SITE_KEY,
    }
    state = tmp_path / "state"
    state.mkdir()
    budget = Budget(state, limit=60, reserve=20)
    cli = LiveCli(
        env=env,
        state_home=state,
        budget=budget,
        config=str(config),
        site="main",
        pass_env=(KEY_VAR_NAME,),
    )
    live = LiveEnv.from_env({ENV_PROJECT: "DEMO"})
    yield cli, live, state


def _statuses(fake: FakeProvider) -> dict[str, tuple[str, str | None]]:
    return {k: (t.status, t.resolution) for k, t in fake.tickets.items()}


def _create(cli: LiveCli, ledger: Ledger, label: str, key: str) -> str:
    plan, done = cli.apply(
        "create", "--project", "DEMO", "--type", "Task", "--title", ledger.title(label),
        "--dedupe-key", key,
    )  # fmt: skip
    assert done is not None and done.code == 0, (plan.doc, done and done.doc)
    ticket = done.doc["key"]
    ledger.add(ticket)
    ledger.create_resolved()
    return ticket


def test_the_runner_counts_requests_from_the_request_log(
    world: tuple[LiveCli, LiveEnv, Path], fake: FakeProvider
) -> None:
    cli, _, _ = world
    first = cli.run("whoami", "--refresh")
    assert first.code == 0 and first.doc["source"] == "provider"
    assert first.requests == 1 and first.writes == 0
    assert cli.budget.used() == len(fake.calls) == 1
    cli.run("get", "DEMO-1")
    assert cli.budget.used() == len(fake.calls)


def test_a_dry_run_that_sends_a_write_is_caught(world: tuple[LiveCli, LiveEnv, Path]) -> None:
    cli, _, _ = world
    real = cli.run

    def lying(*argv: str, **kw: Any) -> Result:
        res = real(*argv, **kw)
        if "--dry-run" in argv:
            res.calls.append({"method": "POST", "path": "/rest/api/3/issue", "status": 201})
        return res

    cli.run = lying  # type: ignore[method-assign]
    with pytest.raises(AssertionError, match="--dry-run sent 1 write"):
        cli.apply("create", "--project", "DEMO", "--type", "Task", "--title", "t",
                  "--dedupe-key", "k")  # fmt: skip


def test_a_real_dry_run_writes_nothing(
    world: tuple[LiveCli, LiveEnv, Path], fake: FakeProvider
) -> None:
    cli, _, _ = world
    plan = cli.run("--dry-run", "create", "--project", "DEMO", "--type", "Task", "--title", "t",
                   "--dedupe-key", "k")  # fmt: skip
    assert plan.code == 0 and plan.writes == 0
    assert not [c for c in fake.calls if c.method == "POST" and c.path.endswith("/issue")]


# ---- the request budget ------------------------------------------------------------------------


def test_the_budget_weighs_a_line_as_one_request_plus_its_retries(tmp_path: Path) -> None:
    log = tmp_path / "ticketctl" / "log.jsonl"
    log.parent.mkdir()
    log.write_text(
        '{"retries": 0}\n{"retries": 2}\nnot json\n[1]\n{"retries": -5}\n{"retries": "x"}\n'
    )
    assert Budget(tmp_path, limit=10).used() == 1 + 3 + 1 + 1 + 1 + 1


def test_a_missing_log_counts_nothing(tmp_path: Path) -> None:
    assert Budget(tmp_path, limit=1).used() == 0


def test_the_budget_stops_the_run_before_and_after_a_command(tmp_path: Path) -> None:
    log = tmp_path / "ticketctl" / "log.jsonl"
    log.parent.mkdir()
    budget = Budget(tmp_path, limit=3, reserve=2)
    log.write_text('{"retries": 0}\n' * 2)
    budget.check_before()
    budget.check_after()
    assert not budget.aborted
    log.write_text('{"retries": 0}\n' * 3)  # exactly the limit: nothing more may start
    budget.check_after()
    assert budget.aborted
    with pytest.raises(BudgetExceeded):
        budget.check_before()
    budget.check_before(cleanup=True)  # cleanup may use the reserve
    log.write_text('{"retries": 0}\n' * 6)
    with pytest.raises(BudgetExceeded):
        budget.check_after(cleanup=True)
    with pytest.raises(BudgetExceeded):
        budget.check_before(cleanup=True)


def test_the_cli_aborts_when_the_cap_is_hit(
    world: tuple[LiveCli, LiveEnv, Path], fake: FakeProvider
) -> None:
    cli, _, state = world
    cli.budget = Budget(state, limit=2, reserve=3)
    cli.run("whoami", "--refresh")
    with pytest.raises(BudgetExceeded):
        cli.run("get", "DEMO-1", "DEMO-2")  # reads 2 more: over the cap after the command
    assert cli.budget.aborted
    before = len(fake.calls)
    with pytest.raises(BudgetExceeded):
        cli.run("whoami", "--refresh")
    assert len(fake.calls) == before  # nothing was sent once the cap was reached


# ---- the closer --------------------------------------------------------------------------------


def test_the_closer_closes_every_created_ticket_when_the_test_body_raises(
    world: tuple[LiveCli, LiveEnv, Path], fake: FakeProvider
) -> None:
    cli, live, _ = world
    ledger = Ledger(cli, live, "ab12cd34")
    before = _statuses(fake)
    outcome: dict[str, str] = {}
    with pytest.raises(RuntimeError, match="forced failure"):
        try:
            a = _create(cli, ledger, "A", "ka")
            b = _create(cli, ledger, "B", "kb")
            assert a != b
            raise RuntimeError("forced failure inside the test body")
        finally:
            outcome = ledger.close_all()
    assert sorted(outcome.values()) == ["closed", "closed"]
    after = _statuses(fake)
    new = set(after) - set(before)
    assert len(new) == 2 and set(ledger.created) == new
    for key in new:
        assert after[key] == ("Done", "Won't Do")
    for key in before:
        assert after[key] == before[key]  # a ticket the run did not create is untouched
    assert ledger.still_open() == []


def test_closing_twice_is_a_no_op_and_an_already_done_ticket_is_not_an_error(
    world: tuple[LiveCli, LiveEnv, Path], fake: FakeProvider
) -> None:
    cli, live, _ = world
    ledger = Ledger(cli, live, "ab12cd34")
    key = _create(cli, ledger, "A", "ka")
    assert ledger.close_all() == {key: "closed"}
    used = cli.budget.used()
    assert ledger.close_all() == {key: "closed"} and cli.budget.used() == used
    other = Ledger(cli, live, "ab12cd35")
    other.created.append(key)
    assert other.close_all() == {key: "already"}


def test_the_closer_goes_on_after_a_failure_and_reports_what_is_left_open(
    world: tuple[LiveCli, LiveEnv, Path], fake: FakeProvider
) -> None:
    cli, live, _ = world
    ledger = Ledger(cli, live, "ab12cd34")
    good = _create(cli, ledger, "A", "ka")
    ledger.created.insert(0, "DEMO-9999")  # a key the site does not know
    outcome = ledger.close_all()
    assert outcome[good] == "closed" and outcome["DEMO-9999"].startswith("failed")
    assert ledger.still_open() == ["DEMO-9999"]


def test_the_closer_can_use_the_reserve_after_the_suite_ran_out_of_requests(
    world: tuple[LiveCli, LiveEnv, Path], fake: FakeProvider
) -> None:
    cli, live, state = world
    ledger = Ledger(cli, live, "ab12cd34")
    key = _create(cli, ledger, "A", "ka")
    cli.budget = Budget(state, limit=cli.budget.used(), reserve=20)
    with pytest.raises(BudgetExceeded):
        cli.run("get", key)
    assert ledger.close_all() == {key: "closed"}
    assert fake.tickets[key].status == "Done"


def test_a_create_whose_answer_was_lost_is_found_by_its_title_and_closed(
    world: tuple[LiveCli, LiveEnv, Path], fake: FakeProvider
) -> None:
    cli, live, _ = world
    ledger = Ledger(cli, live, "ab12cd34")
    other_title = fake.add_ticket("DEMO-500", "ticketctl live test zzzzzzzz not mine")
    plan = cli.run("--dry-run", "create", "--project", "DEMO", "--type", "Task", "--title",
                   ledger.title("A"), "--dedupe-key", "kx")  # fmt: skip
    landed = cli.run("--yes", "--expect-plan", plan.doc["plan_sha256"], "create", "--project",
                     "DEMO", "--type", "Task", "--title", ledger.title("A"), "--dedupe-key",
                     "kx")  # fmt: skip
    assert landed.code == 0
    ledger.unknown_create = True  # as if the answer had been lost: no key was recorded
    outcome = ledger.close_all()
    assert list(outcome.values()) == ["closed"]
    assert other_title.status != "Done"  # another run's ticket (other marker) is never touched


def test_at_most_three_tickets_and_one_comment_per_ticket(
    world: tuple[LiveCli, LiveEnv, Path],
) -> None:
    cli, live, _ = world
    ledger = Ledger(cli, live, "ab12cd34")
    assert (MAX_TEST_TICKETS, MAX_COMMENTS_PER_TICKET) == (3, 1)
    for n in range(MAX_TEST_TICKETS):
        ledger.add(f"DEMO-{n + 1}")
    with pytest.raises(TooManyTickets):
        ledger.add("DEMO-4")
    assert "DEMO-4" in ledger.created  # the surplus ticket is still this run's: it gets closed
    ledger.add("DEMO-1")  # a repeat is not a new ticket
    assert ledger.may_comment("DEMO-1")
    ledger.note_comment("DEMO-1")
    assert not ledger.may_comment("DEMO-1")
    with pytest.raises(TooManyComments):
        ledger.note_comment("DEMO-1")
    with pytest.raises(ValueError):
        ledger.add("not a key")


# ---- repository hygiene and the checklist ------------------------------------------------------


def test_the_scan_for_real_names_covers_the_live_directory() -> None:
    scanned = {p.name for p, _ in files() if LIVE_DIR in p.parents}
    assert {
        "support.py", "conftest.py", "checklist.py", "test_harness.py", "test_live_read.py",
        "test_live_write.py",
    } <= scanned  # fmt: skip
    for path, text in files(LIVE_DIR):
        assert scan(text) == [], path.name


def test_the_live_modules_import_nothing_from_the_product_package() -> None:
    for name in ("test_live_read.py", "test_live_write.py", "support.py", "conftest.py"):
        tree = ast.parse((LIVE_DIR / name).read_text())
        for node in ast.walk(tree):
            mods = []
            if isinstance(node, ast.Import):
                mods = [a.name for a in node.names]
            elif isinstance(node, ast.ImportFrom):
                mods = [node.module or ""]
            for mod in mods:
                assert mod.split(".")[0] != "ticketctl", f"{name} imports {mod}"
                assert mod.split(".")[0] not in {"urllib", "http", "requests", "socket"}, name


def _test_functions(module: str) -> set[str]:
    tree = ast.parse((LIVE_DIR / module).read_text())
    return {
        n.name for n in tree.body if isinstance(n, ast.FunctionDef) and n.name.startswith("test_")
    }


def test_every_probe_the_checklist_names_exists_and_every_live_test_is_in_the_checklist() -> None:
    read, write = _test_functions("test_live_read.py"), _test_functions("test_live_write.py")
    assert read and write
    named: set[str] = set()
    for item in checklist.ITEMS:
        assert item.kind in {checklist.PROBED, checklist.INDIRECT, checklist.HUMAN}
        if item.kind != checklist.HUMAN:
            assert item.probes, item.id
        if item.kind != checklist.PROBED:
            assert item.why, item.id
        for probe in item.probes:
            assert probe in read | write, (item.id, probe)
            named.add(probe)
    assert (read | write) - named == set()


def test_the_checklist_ids_are_unique_and_the_read_ids_belong_to_read_tests() -> None:
    ids = [i.id for i in checklist.ITEMS]
    assert len(ids) == len(set(ids))
    read, write = _test_functions("test_live_read.py"), _test_functions("test_live_write.py")
    for item in checklist.ITEMS:
        if item.kind == checklist.PROBED:
            pool = read if item.id.startswith("L") else write
            assert set(item.probes) <= pool, item.id


def test_the_runbook_table_is_the_rendered_checklist() -> None:
    doc = (REPO / "docs" / "live-verification.md").read_text()
    begin, end = "<!-- checklist:begin -->", "<!-- checklist:end -->"
    assert begin in doc and end in doc
    body = doc.split(begin, 1)[1].split(end, 1)[0].strip("\n")
    assert body == checklist.render_table()


# ---- the live test bodies, end to end against the fake ----------------------------------------


def test_the_write_suite_runs_against_the_fake_and_closes_everything(
    world: tuple[LiveCli, LiveEnv, Path],
    fake: FakeProvider,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from tests.live import test_live_write as w

    monkeypatch.setattr(w.time, "sleep", lambda seconds: fake.advance(seconds))
    cli, _, state = world
    live = LiveEnv.from_env({ENV_PROJECT: "DEMO", "TICKETCTL_LIVE_BOARD": "1"})
    cli.budget = Budget(state, limit=WRITE_LIMIT, reserve=CLEANUP_RESERVE)
    rec = Recorder("write", tmp_path / "findings", Redactor(live.literals()))
    ledger = Ledger(cli, live, "feedbeef")
    seeded = _statuses(fake)
    bodies = [
        w.test_create_with_dedupe_key_and_the_label_lag,
        w.test_create_explicit_unassigned,
        w.test_transition_with_assign,
        w.test_update_title_labels_order,
        w.test_update_description_is_unchanged_on_rerun,
        w.test_comment_with_key,
        w.test_sprint_add_move_and_remove,
        w.test_sprint_errors,
        w.test_every_created_ticket_is_closed,
    ]
    outcome: dict[str, str] = {}
    try:
        for body in bodies:
            try:
                if body is w.test_update_description_is_unchanged_on_rerun:
                    body((live, cli, rec, ledger), tmp_path)
                else:
                    body((live, cli, rec, ledger))
                outcome[body.__name__] = "passed"
            except pytest.skip.Exception as exc:
                outcome[body.__name__] = f"skipped: {exc}"
    finally:
        ledger.close_all()
    assert not [n for n, o in outcome.items() if o.startswith("skipped")], outcome
    assert cli.budget.used() <= WRITE_LIMIT
    assert len(ledger.created) <= MAX_TEST_TICKETS
    after = _statuses(fake)
    for key in set(after) - set(seeded):
        assert after[key] == ("Done", "Won't Do")
    for key, state_of in seeded.items():
        assert after[key] == state_of
    assert ledger.still_open() == []
    # at most one comment on one created ticket, and nothing else was commented
    assert sum(len(t.comments) for k, t in fake.tickets.items() if k in ledger.created) == 1
    path = rec.write()
    assert "W10" in path.read_text()


PASSES = {
    "test_whoami_shape",
    "test_wrong_token_is_a_401",
    "test_get_default_shape",
    "test_get_fields_summary_only",
    "test_get_missing_and_hidden_status",
    "test_get_moved_ticket",
    "test_search_semantic_jql_is_caught_by_parse",
    "test_search_count_with_order_by",
    "test_incremental_refresh_combination",
    "test_text_and_scanner_samples",
    "test_status_category_names",
    "test_priority_order",
    "test_comments_shape",
    "test_transitions_shape",
    "test_sprints_list_and_show",
    "test_board_errors",
    "test_fields_check_and_list",
    "test_validate_probe",
    "test_passive_request_log",
}  # the probes the fake can serve: the paging pair needs more than 150 tickets and may skip
BIG_SEARCH_USERS = (
    "test_search_multi_page_cursor",
    "test_search_rejected_cursor",
    "test_priority_order",
)


def test_the_read_suite_runs_against_the_fake_and_writes_nothing(
    world: tuple[LiveCli, LiveEnv, Path], fake: FakeProvider, tmp_path: Path
) -> None:
    from tests.live import test_live_read as r

    cli, _, state = world
    live = LiveEnv.from_env(
        {
            ENV_PROJECT: "DEMO",
            "TICKETCTL_LIVE_TICKET": "DEMO-2",
            "TICKETCTL_LIVE_BOARD": "1",
            ENV_KEY_VAR: KEY_VAR_NAME,
            "TICKETCTL_LIVE_MISSING_TICKET": "DEMO-4040",
            "TICKETCTL_LIVE_MOVED_TICKET": "DEMO-70",
            "TICKETCTL_LIVE_HIDDEN_TICKET": "DEMO-4041",
        }
    )
    cli.budget = Budget(state, limit=READ_LIMIT, reserve=CLEANUP_RESERVE)
    fake.add_ticket("DEMO-70", "a ticket that will be moved")
    fake.move("DEMO-70", "DEMO-71")  # its old key still resolves, to the new one
    for ticket, name in zip(list(fake.tickets.values())[:2], ("High", "Low"), strict=False):
        ticket.priority = name  # two distinct priorities for the priority-order probe
    rec = Recorder("read", tmp_path / "findings", Redactor(live.literals()))
    big: dict[str, Any] = {}
    outcome: dict[str, str] = {}
    for name in sorted(n for n in dir(r) if n.startswith("test_")):
        body = getattr(r, name)
        try:
            if name in BIG_SEARCH_USERS:
                if not big:
                    res = cli.run(
                        "search", "--project", "DEMO", "--limit", "150", "--fields",
                        "title,priority,status,updated",
                    )  # fmt: skip
                    big.update(doc=res.doc, requests=res.requests, tickets=res.doc["tickets"])
                body((live, cli, rec), big)
            else:
                body((live, cli, rec))
            outcome[name] = "passed"
        except pytest.skip.Exception as exc:
            outcome[name] = f"skipped: {exc}"
    assert all(o == "passed" or o.startswith("skipped") for o in outcome.values())
    assert PASSES <= {n for n, o in outcome.items() if o == "passed"}, outcome
    assert cli.budget.used() <= READ_LIMIT
    assert not [c for c in fake.calls if c.method in ("PUT", "DELETE")]
    assert not [
        c for c in fake.calls if c.method == "POST" and not re.search(r"search|jql", c.path)
    ]
    assert "L18" in rec.write().read_text()


# ---- round 1 of the review: lost creates, caps in the runner, gates, redaction ------------------


def _lost_create_cli(cli: LiveCli, fake: FakeProvider) -> None:
    """Make the next applied create raise after the fake created the ticket (a lost answer)."""
    real = cli.run

    def boom(*argv: str, **kw: Any) -> Result:
        res = real(*argv, **kw)
        if "--yes" in argv and "create" in argv:
            cli.run = real  # type: ignore[method-assign]
            raise subprocess.TimeoutExpired("ticketctl", 1)
        return res

    cli.run = boom  # type: ignore[method-assign]


def test_a_create_that_raises_after_the_post_is_swept_and_closed(
    world: tuple[LiveCli, LiveEnv, Path], fake: FakeProvider
) -> None:
    cli, live, _ = world
    ledger = Ledger(cli, live, "ab12cd34", sleep=lambda seconds: None)
    before = set(fake.tickets)
    _lost_create_cli(cli, fake)
    with pytest.raises(subprocess.TimeoutExpired):
        cli.apply("create", "--project", "DEMO", "--type", "Task", "--title", ledger.title("A"),
                  "--dedupe-key", "kl")  # fmt: skip
    new = set(fake.tickets) - before
    assert len(new) == 1 and ledger.created == []  # created at the site, unknown to the ledger
    assert ledger.unknown_create
    outcome = ledger.close_all()
    assert list(outcome.values()) == ["closed"]
    assert {fake.tickets[k].status for k in new} == {"Done"}
    assert ledger.still_open() == []


def test_a_failed_closer_entry_is_retried_by_the_finalizer(
    world: tuple[LiveCli, LiveEnv, Path], fake: FakeProvider
) -> None:
    cli, live, _ = world
    ledger = Ledger(cli, live, "ab12cd34")
    key = _create(cli, ledger, "A", "ka")
    ledger.closed[key] = "failed: exit 12 timeout"
    assert ledger.still_open() == [key]
    assert ledger.close_all() == {key: "closed"}
    assert fake.tickets[key].status == "Done" and ledger.still_open() == []


def test_an_unknown_create_that_found_nothing_is_reported(
    world: tuple[LiveCli, LiveEnv, Path],
) -> None:
    cli, live, _ = world
    sleeps: list[float] = []
    ledger = Ledger(cli, live, "ab12cd34", sleep=sleeps.append)
    ledger.unknown_create = True
    assert ledger.close_all() == {}
    assert sleeps == [SWEEP_WAIT_SECONDS] * SWEEP_RETRIES
    (note,) = ledger.still_open()
    assert re.search(r"outcome was unknown", note) and re.search(re.escape(ledger.title_stem), note)
    ledger.left_open.append("sweep failed: OSError")
    assert "sweep failed: OSError" in ledger.still_open()


def test_the_sweep_is_not_started_without_a_project_and_says_so(
    world: tuple[LiveCli, LiveEnv, Path], fake: FakeProvider
) -> None:
    cli, _, _ = world
    ledger = Ledger(cli, LiveEnv(), "ab12cd34", sleep=lambda seconds: None)
    ledger.unknown_create = True
    before = len(fake.calls)
    ledger.close_all()
    assert len(fake.calls) == before
    assert any(re.search(r"outcome was unknown", n) for n in ledger.still_open())


def test_the_sweep_waits_for_a_lagging_search_index(
    world: tuple[LiveCli, LiveEnv, Path], fake: FakeProvider
) -> None:
    cli, live, _ = world
    sleeps: list[float] = []
    ledger = Ledger(cli, live, "ab12cd34", sleep=sleeps.append)

    def appear_after_the_first_wait(seconds: float) -> None:
        sleeps.append(seconds)
        if len(sleeps) == 1:
            fake.add_ticket("DEMO-600", ledger.title("A"))

    ledger.sleep = appear_after_the_first_wait
    ledger.unknown_create = True
    outcome = ledger.close_all()
    assert sleeps == [SWEEP_WAIT_SECONDS] and outcome == {"DEMO-600": "closed"}
    assert ledger.still_open() == []


def test_the_sweep_closes_only_titles_that_start_with_the_marker(
    world: tuple[LiveCli, LiveEnv, Path], fake: FakeProvider
) -> None:
    cli, live, _ = world
    ledger = Ledger(cli, live, "ab12cd34", sleep=lambda seconds: None)
    mid = fake.add_ticket("DEMO-501", f"someone else's note about {ledger.title_stem} here")
    mine = fake.add_ticket("DEMO-502", ledger.title("A"))
    found = cli.run("search", "--project", "DEMO", "--text", ledger.title_stem, "--fields", "title")
    assert {t["key"] for t in found.doc["tickets"]} >= {"DEMO-501", "DEMO-502"}  # both are hits
    ledger.unknown_create = True
    ledger.close_all()
    assert mine.status == "Done" and mid.status != "Done"
    assert ledger.created == ["DEMO-502"]


def _wrapped(world: tuple[LiveCli, LiveEnv, Path]) -> tuple[LiveCli, Ledger]:
    cli, live, _ = world
    return cli, Ledger(cli, live, "ab12cd34")


def test_the_runner_refuses_an_unapproved_comment_before_any_request(
    world: tuple[LiveCli, LiveEnv, Path], fake: FakeProvider
) -> None:
    cli, ledger = _wrapped(world)
    key = _create(cli, ledger, "A", "ka")
    before = len(fake.calls)
    with pytest.raises(TooManyComments):
        cli.apply("comment", key, "--body", "b", "--dedupe-key", "kc")
    with pytest.raises(TooManyComments):
        cli.run("--yes", "--expect-plan", "0" * 64, "comment", "DEMO-1", "--body", "b")
    assert len(fake.calls) == before


def test_the_runner_allows_one_approved_comment_per_ticket_and_no_second(
    world: tuple[LiveCli, LiveEnv, Path], fake: FakeProvider
) -> None:
    cli, ledger = _wrapped(world)
    key = _create(cli, ledger, "A", "ka")
    ledger.note_comment(key)
    plan, done = cli.apply("comment", key, "--body", "b", "--dedupe-key", "kc")
    assert done is not None and done.code == 0, (plan.error, done and done.error)
    before = len(fake.calls)
    with pytest.raises(TooManyComments):
        cli.run("--yes", "--expect-plan", "0" * 64, "comment", key, "--body", "again")
    assert len(fake.calls) == before
    assert len(fake.tickets[key].comments) == 1


def test_the_runner_refuses_a_fourth_create_before_the_request(
    world: tuple[LiveCli, LiveEnv, Path], fake: FakeProvider
) -> None:
    cli, ledger = _wrapped(world)
    for n in range(MAX_TEST_TICKETS):
        _create(cli, ledger, f"T{n}", f"k{n}")
    before = len(fake.calls)
    with pytest.raises(TooManyTickets):
        cli.run("--yes", "--expect-plan", "0" * 64, "create", "--project", "DEMO", "--type",
                "Task", "--title", ledger.title("X"), "--dedupe-key", "kx")  # fmt: skip
    assert len(fake.calls) == before and len(fake.tickets) >= MAX_TEST_TICKETS
    ledger.close_all()


def _create_argv(ledger: Ledger) -> tuple[str, ...]:
    return ("--yes", "--expect-plan", "0" * 64, "create", "--project", "DEMO", "--type", "Task",
            "--title", ledger.title("X"), "--dedupe-key", "kx")  # fmt: skip


def test_a_budget_refusal_before_any_request_is_not_an_unknown_create(
    world: tuple[LiveCli, LiveEnv, Path], fake: FakeProvider
) -> None:
    cli, ledger = _wrapped(world)
    cli.budget.limit = 0
    before = len(fake.calls)
    with pytest.raises(BudgetExceeded):
        cli.run(*_create_argv(ledger))
    assert not ledger.unknown_create and ledger.create_launches == 0
    assert ledger.close_all() == {} and len(fake.calls) == before
    assert ledger.still_open() == []


@pytest.mark.parametrize(
    "argv",
    [
        ("--yes", "--expect-plan", "0" * 64, "create", "--project", "DEMO", "--type", "Task"),
        ("update", "DEMO-1", "--title", "t"),
        ("transition", "DEMO-1", "--to", "Done"),
        ("sprint", "add", "DEMO-1", "--sprint", "1"),
    ],
)
def test_a_write_with_stdin_input_is_refused_before_any_request(
    world: tuple[LiveCli, LiveEnv, Path], fake: FakeProvider, argv: tuple[str, ...]
) -> None:
    cli, ledger = _wrapped(world)
    before = len(fake.calls)
    with pytest.raises(StdinWriteRefused):
        cli.run(*argv, stdin="y\n")
    assert len(fake.calls) == before and ledger.create_launches == 0 and not ledger.unknown_create


def test_stdin_is_fine_for_a_read_and_for_a_dry_run(
    world: tuple[LiveCli, LiveEnv, Path],
) -> None:
    cli, _ = _wrapped(world)
    assert cli.run("whoami", "--refresh", stdin="").code == 0
    cli.run("--dry-run", "update", "DEMO-1", "--title", "t", stdin="")


def test_a_refused_create_is_a_known_outcome_and_prints_no_unknown_note(
    world: tuple[LiveCli, LiveEnv, Path], fake: FakeProvider
) -> None:
    cli, ledger = _wrapped(world)
    seeded = set(fake.tickets)
    res = cli.run(*_create_argv(ledger))  # a plan digest that matches nothing: refused
    assert res.code != 0 and res.error and set(fake.tickets) == seeded
    assert not ledger.unknown_create
    ledger.close_all()
    assert ledger.still_open() == []


def test_a_refusal_keeps_an_earlier_unknown_create_unknown(
    world: tuple[LiveCli, LiveEnv, Path],
) -> None:
    cli, ledger = _wrapped(world)
    ledger.unknown_create = True  # an earlier create whose answer was lost
    cli.run(*_create_argv(ledger))
    assert ledger.unknown_create
    unknown = Result(_create_argv(ledger), 12, {"error": {"code": "x", "outcome_unknown": True}},
                     "", "", 0.0, [])  # fmt: skip
    ledger.unknown_create = False
    ledger.cli.guard(_create_argv(ledger))  # type: ignore[misc]
    ledger.observe(unknown)
    assert ledger.unknown_create  # an answer that says "unknown" never clears it


def test_the_sweep_stops_at_once_when_every_hit_is_already_known(
    world: tuple[LiveCli, LiveEnv, Path], fake: FakeProvider
) -> None:
    cli, live, _ = world
    sleeps: list[float] = []
    ledger = Ledger(cli, live, "ab12cd34", sleep=sleeps.append)
    key = _create(cli, ledger, "A", "ka")
    ledger.unknown_create = True
    before = len(fake.calls)
    ledger.close_all()
    assert sleeps == [] and ledger.created == [key]
    searches = [c for c in fake.calls[before:] if re.search(r"/search/jql$", c.path)]
    assert len(searches) == 1


def test_a_sweep_failure_note_is_listed_once_when_the_closer_runs_twice(
    world: tuple[LiveCli, LiveEnv, Path],
) -> None:
    cli, live, _ = world
    ledger = Ledger(cli, live, "ab12cd34", sleep=lambda seconds: None)
    ledger.unknown_create = True

    def broken() -> None:
        raise OSError("x")

    ledger._sweep = broken  # type: ignore[method-assign]
    ledger.close_all()
    ledger.close_all()
    notes = [n for n in ledger.still_open() if re.search(r"sweep failed", n)]
    assert notes == ["sweep failed: OSError"]


def test_the_command_name_skips_the_global_flags() -> None:
    argv = ("--actor", "human:x", "--dry-run", "--yes", "--expect-plan", "a" * 64, "comment", "K-1")
    assert command_name(argv) == ("comment", ["K-1"])
    assert command_name(()) == ("", [])


def test_a_read_only_runner_refuses_yes_and_any_write(
    world: tuple[LiveCli, LiveEnv, Path], fake: FakeProvider
) -> None:
    cli, _, _ = world
    cli.read_only = True
    before = len(fake.calls)
    with pytest.raises(AssertionError, match="--yes"):
        cli.run("--yes", "--expect-plan", "0" * 64, "update", "DEMO-1", "--title", "t")
    assert len(fake.calls) == before
    cli.run("whoami", "--refresh")  # a read is fine
    sent = Result(("x",), 0, {}, "", "", 0.0, [{"method": "PUT", "path": "/rest/api/3/issue/X"}])
    with pytest.raises(AssertionError, match="write"):
        cli.check_read_only(sent)
    cli.read_only = False
    cli.check_read_only(sent)


def test_the_child_sees_only_a_minimal_environment(tmp_path: Path) -> None:
    probe = tmp_path / "probe"
    probe.write_text(
        f"#!{sys.executable}\nimport json, os\nprint(json.dumps(sorted(os.environ)))\n"
    )
    probe.chmod(0o755)
    (tmp_path / "state").mkdir()
    env = {
        "PATH": "", "HOME": str(tmp_path), "XDG_CONFIG_HOME": str(tmp_path), "TICKETCTL_X": "1",
        "UNRELATED_VAR": "1", "FAKE_SITE_KEY_ENV": "v", "OTHER_KEY": "v", "AWS_PROFILE": "p",
    }  # fmt: skip
    cli = LiveCli(env=env, state_home=tmp_path / "state", budget=Budget(tmp_path / "state", 5),
                  python=str(probe), pass_env=(KEY_VAR_NAME,))  # fmt: skip
    names = cli.run("anything").doc
    assert {"PATH", "HOME", "TICKETCTL_X", "XDG_STATE_HOME", KEY_VAR_NAME} <= set(names)
    assert not {"UNRELATED_VAR", "OTHER_KEY", "AWS_PROFILE"} & set(names)
    assert "UNRELATED_VAR" not in child_env(env)


def test_extra_env_can_never_move_the_private_state_directory(tmp_path: Path) -> None:
    probe = tmp_path / "probe"
    probe.write_text(
        f"#!{sys.executable}\nimport json, os\nprint(json.dumps(os.environ['XDG_STATE_HOME']))\n"
    )
    probe.chmod(0o755)
    state = tmp_path / "state"
    state.mkdir()
    cli = LiveCli(env={"PATH": ""}, state_home=state, budget=Budget(state, 5), python=str(probe))
    assert cli.run("x", extra_env={"XDG_STATE_HOME": "/elsewhere"}).doc == str(state)


# ---- the gates and the closer, through the real fixtures (a child pytest over the fake) ---------

CHILD_SOURCE = """
import os

import pytest


def test_uses_the_write_world(write_world):
    live, cli, rec, ledger = write_world
    for name in ("A", "B"):
        plan, done = cli.apply("create", "--project", "DEMO", "--type", "Task",
                               "--title", ledger.title(name), "--dedupe-key", "k-" + name)
        ledger.add(done.doc["key"], name)
        ledger.create_resolved()
    if os.environ["FORCED_MODE"] == "fail":
        raise RuntimeError("forced failure")
    cli.budget.limit = cli.budget.used()  # the cap is reached: the next command aborts
    cli.run("whoami", "--refresh")


def test_uses_the_read_world(read_world):
    pass
"""


def _child_pytest(
    tmp_path: Path, args: list[str], env_more: dict[str, str]
) -> subprocess.CompletedProcess[str]:
    (tmp_path / "pytest.ini").write_text("[pytest]\n")
    (tmp_path / "test_renamed.py").write_text(CHILD_SOURCE)
    home = tmp_path / "home"
    home.mkdir(exist_ok=True)
    env = {
        "HOME": str(home),
        "XDG_CONFIG_HOME": str(home / ".config"),
        "PATH": "",
        "PYTHONPATH": os.pathsep.join([str(REPO), *sys.path]),
        "PYTHONDONTWRITEBYTECODE": "1",
        **env_more,
    }
    return subprocess.run(  # noqa: S603 - the interpreter running the tests
        [sys.executable, "-m", "pytest", "-p", "tests.live.conftest", "-p", "no:cacheprovider",
         "-c", str(tmp_path / "pytest.ini"), "--rootdir", str(tmp_path), *args],
        cwd=tmp_path, env=env, capture_output=True, text=True, timeout=300, check=False,
    )  # fmt: skip


def test_a_renamed_module_still_hits_the_gates_in_the_fixtures(tmp_path: Path) -> None:
    done = _child_pytest(tmp_path, ["-rs", "test_renamed.py"], {})
    out = done.stdout + done.stderr
    assert done.returncode == 0, out
    assert re.search(r"2 skipped", out) and re.search(r"TICKETCTL_LIVE=1", out), out
    done = _child_pytest(
        tmp_path,
        ["-rs", "-k", "write_world", "test_renamed.py"],
        {ENV_LIVE: "1", ENV_FINDINGS: str(tmp_path / "findings")},
    )
    out = done.stdout + done.stderr
    assert re.search(r"TICKETCTL_LIVE_WRITE=1", out) and not re.search(r"\bpassed\b", out), out


@pytest.mark.parametrize("mode", ["fail", "cap"])
def test_the_real_fixture_closes_every_ticket_after_a_failure_or_a_cap_abort(
    mode: str, tmp_path: Path, fake: FakeProvider
) -> None:
    config = tmp_path / "config.yaml"
    config.write_text(
        f"sites:\n  main:\n    url: {fake.url}\n    email: {SITE_EMAIL}\n"
        f"    token_env: {KEY_VAR_NAME}\n    allow_http: true\n    projects: [DEMO]\n"
    )
    config.chmod(0o600)
    seeded = _statuses(fake)
    env = {
        ENV_LIVE: "1", ENV_WRITE: "1", ENV_PROJECT: "DEMO", "FORCED_MODE": mode,
        "TICKETCTL_LIVE_CONFIG": str(config), "TICKETCTL_LIVE_SITE": "main",
        ENV_PASS: KEY_VAR_NAME, KEY_VAR_NAME: FAKE_SITE_KEY, ENV_KEY_VAR: KEY_VAR_NAME,
        ENV_FINDINGS: str(tmp_path / "findings"),
    }  # fmt: skip
    done = _child_pytest(tmp_path, ["-x", "-k", "write_world", "test_renamed.py"], env)
    out = done.stdout + done.stderr
    assert done.returncode != 0, out  # the forced failure or the cap abort failed the test
    after = _statuses(fake)
    new = set(after) - set(seeded)
    assert len(new) == 2, out
    for key in new:
        assert after[key] == ("Done", "Won't Do"), (mode, out)
    assert not re.search(r"LEFT OPEN", out), out


# ---- redaction of what the review found missing ---------------------------------------------------


def test_the_redactor_masks_field_ids_digit_accounts_and_named_values() -> None:
    field = "customfield_" + "10042"
    bracket = "cf[" + "10042" + "]"
    digits = "123456789" + "012345678" + "901234"  # 24 digits, built at run time
    named = "plain-value-" + "xyz"
    r = Redactor(masked_env_values({"SOME_VAR": named, "UNRELATED": "other-value-1"}, ["SOME_VAR"]))
    text = r.text(f"{field} and {bracket} and {digits} and {named} and other-value-1")
    for gone in (field, bracket, digits, named):
        assert not re.search(re.escape(gone), text), gone
    assert re.search(r"other-value-1", text)  # a value not named is left alone


def test_every_value_the_user_named_is_a_literal() -> None:
    live = LiveEnv.from_env(
        {
            "TICKETCTL_LIVE_BOARD": "4242",
            "TICKETCTL_LIVE_KANBAN_BOARD": "5252",
            ENV_KEY_VAR: "SITE_KEY_NAME",
            ENV_PASS: "MAIL_NAME",
            "TICKETCTL_LIVE_TYPE": "Chore",
            "TICKETCTL_LIVE_RESOLUTION": "Declined",
            "TICKETCTL_LIVE_DONE_STATUS": "Closed",
        }
    )
    assert {"4242", "5252", "SITE_KEY_NAME", "MAIL_NAME", "Chore", "Declined", "Closed"} <= set(
        live.literals()
    )
    assert not {"Task", "Done", "Won't Do"} & set(LiveEnv().literals())


def test_assertion_messages_are_masked() -> None:
    message = explain(Redactor(["tracker-literal"]), "tracker-literal", "jane@example.test")
    assert not re.search(r"tracker-literal|jane", message)
