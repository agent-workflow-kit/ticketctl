"""audit.jsonl and log.jsonl writers: seq, torn tails, rotation, concurrency, sanitizing."""

from __future__ import annotations

import json
import multiprocessing
import os
import signal
import stat
from pathlib import Path

import pytest

from tests import state_workers as w
from ticketctl import audit, sanitize, state
from ticketctl.state import StateError

ROOT = Path(__file__).resolve().parent.parent
SENTINEL = "SENTINEL-s3cr3t-value-9f2c"
CTX = multiprocessing.get_context("spawn")


@pytest.fixture
def sd(tmp_path: Path) -> Path:
    path = tmp_path / "state" / "ticketctl"
    fd = state.open_dir(path)
    assert fd is not None
    os.close(fd)
    return path


@pytest.fixture(autouse=True)
def _reset_scrubber():
    sanitize.configure_scrubber([])
    yield
    sanitize.configure_scrubber([])


def reap(procs: list) -> None:
    """SIGKILL exactly the still-running workers this test started (by recorded pid)."""
    for p in procs:
        pid = p.pid
        if pid is not None and p.is_alive():
            os.kill(pid, signal.SIGKILL)
            p.join(10)


def lines(path: Path) -> list[str]:
    return path.read_bytes().decode("ascii").split("\n")[:-1]


def records(path: Path) -> list[dict]:
    return [json.loads(x) for x in lines(path)]


# --- audit: basics ---------------------------------------------------------------------------


def test_first_record_has_seq_1_v_1_and_a_ts(sd: Path) -> None:
    rec = audit.append_audit(sd, w.audit_record("a", 1, result="attempted"))
    assert rec["seq"] == 1 and rec["v"] == 1
    assert audit.TS.fullmatch(str(rec["ts"]))
    assert records(sd / "audit.jsonl") == [rec]
    assert stat.S_IMODE((sd / "audit.jsonl").stat().st_mode) == 0o600


def test_seq_increases_by_one_from_the_file_tail(sd: Path) -> None:
    for i in range(5):
        assert audit.append_audit(sd, w.audit_record("a", i))["seq"] == i + 1


def test_caller_cannot_choose_seq_or_v(sd: Path) -> None:
    rec = w.audit_record("a", 1) | {"seq": 99, "v": 7}
    with pytest.raises(StateError) as info:
        audit.append_audit(sd, rec)
    assert info.value.reason == "record_invalid"
    assert not (sd / "audit.jsonl").exists() or (sd / "audit.jsonl").read_bytes() == b""


def test_supplied_ts_is_kept_when_valid(sd: Path) -> None:
    rec = audit.append_audit(sd, w.audit_record("a", 1) | {"ts": "2026-01-02T03:04:05Z"})
    assert rec["ts"] == "2026-01-02T03:04:05Z"
    with pytest.raises(StateError):
        audit.append_audit(sd, w.audit_record("a", 2) | {"ts": "yesterday"})


def test_attempted_is_fsynced_and_other_results_are_not(
    sd: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    synced: list[int] = []
    real = os.fsync
    monkeypatch.setattr(os, "fsync", lambda fd: (synced.append(fd), real(fd))[1])
    audit.append_audit(sd, w.audit_record("a", 1, result="ok"))
    ok_syncs = len(synced)
    audit.append_audit(sd, w.audit_record("a", 2, result="attempted"))
    assert len(synced) > ok_syncs
    after = len(synced)
    audit.append_audit(sd, w.audit_record("a", 3, result="failed"))
    assert len(synced) == after


def test_output_is_ascii_one_line_per_record(sd: Path) -> None:
    rec = w.audit_record("a", 1)
    rec["inputs"]["config"]["path"] = "/home/caf\u00e9/\u4e2d\u6587/\U0001f600"
    audit.append_audit(sd, rec)
    raw = (sd / "audit.jsonl").read_bytes()
    raw.decode("ascii")
    assert raw.count(b"\n") == 1 and raw.endswith(b"\n")
    assert json.loads(raw)["inputs"]["config"]["path"] == "/home/caf\u00e9/\u4e2d\u6587/\U0001f600"


# --- audit: torn tails -----------------------------------------------------------------------


def test_torn_tail_is_repaired_and_seq_comes_from_a_scan(sd: Path) -> None:
    path = sd / "audit.jsonl"
    audit.append_audit(sd, w.audit_record("a", 1))
    audit.append_audit(sd, w.audit_record("a", 2))
    with path.open("ab") as fh:
        fh.write(b'{"v":1,"seq":50,"ts":"2026-01-01T00:00:00Z","atte')
    rec = audit.append_audit(sd, w.audit_record("a", 3))
    assert rec["seq"] == 3
    got = lines(path)
    assert len(got) == 4
    assert got[2].startswith('{"v":1,"seq":50')
    assert json.loads(got[3])["seq"] == 3
    assert audit.append_audit(sd, w.audit_record("a", 4))["seq"] == 4


def test_scan_takes_the_maximum_not_the_last_good_line(sd: Path) -> None:
    path = sd / "audit.jsonl"
    path.write_bytes(b'{"seq":7}\n{"seq":3}\ngarbage')
    path.chmod(0o600)
    assert audit.append_audit(sd, w.audit_record("a", 1))["seq"] == 8


def test_complete_json_tail_without_newline_gets_a_newline_and_its_seq(sd: Path) -> None:
    path = sd / "audit.jsonl"
    path.write_bytes(b'{"seq":4}')
    path.chmod(0o600)
    assert audit.append_audit(sd, w.audit_record("a", 1))["seq"] == 5
    assert lines(path)[0] == '{"seq":4}'


@pytest.mark.parametrize(
    "tail",
    [
        b"not json\n",
        b'{"seq":"9"}\n',
        b'{"seq":true}\n',
        b'{"seq":-3}\n',
        b"[1]\n",
        b'{"seq":1.5}\n',
    ],
)
def test_unusable_tail_line_triggers_the_scan(sd: Path, tail: bytes) -> None:
    path = sd / "audit.jsonl"
    path.write_bytes(b'{"seq":6}\n' + tail)
    path.chmod(0o600)
    assert audit.append_audit(sd, w.audit_record("a", 1))["seq"] == 7


def test_a_huge_garbage_tail_does_not_crash_and_is_scanned(sd: Path) -> None:
    path = sd / "audit.jsonl"
    path.write_bytes(b'{"seq":2}\n' + b"x" * (3 * 1024 * 1024))
    path.chmod(0o600)
    assert audit.append_audit(sd, w.audit_record("a", 1))["seq"] == 3
    assert len(lines(path)) == 3


def test_empty_file_and_blank_lines(sd: Path) -> None:
    path = sd / "audit.jsonl"
    path.write_bytes(b"\n\n")
    path.chmod(0o600)
    assert audit.append_audit(sd, w.audit_record("a", 1))["seq"] == 1


# --- audit: content rules --------------------------------------------------------------------


def test_unknown_top_level_and_nested_keys_are_refused(sd: Path) -> None:
    for mutate in (
        lambda r: r.update(title="Fix the thing"),
        lambda r: r.update(headers={"Authorization": "x"}),
        lambda r: r.update(body="text"),
        lambda r: r["inputs"].update(token="x"),
        lambda r: r["target"].update(summary="t"),
        lambda r: r["inputs"]["config"].update(extra=1),
    ):
        rec = w.audit_record("a", 1)
        mutate(rec)
        with pytest.raises(StateError) as info:
            audit.append_audit(sd, rec)
        assert info.value.reason == "record_invalid"
    assert not (sd / "audit.jsonl").exists() or not (sd / "audit.jsonl").read_bytes()


def test_missing_required_key_and_bad_enum_are_refused(sd: Path) -> None:
    rec = w.audit_record("a", 1)
    del rec["action"]
    with pytest.raises(StateError):
        audit.append_audit(sd, rec)
    with pytest.raises(StateError):
        audit.append_audit(sd, w.audit_record("a", 1) | {"result": "done"})
    with pytest.raises(StateError):
        audit.append_audit(sd, w.audit_record("a", 1) | {"step": 0})
    with pytest.raises(StateError):
        audit.append_audit(sd, w.audit_record("a", 1) | {"stdin_tty": "no"})


def test_result_changed_rules_follow_the_schema(sd: Path) -> None:
    with pytest.raises(StateError):
        audit.append_audit(sd, w.audit_record("a", 1, result="attempted") | {"changed": True})
    with pytest.raises(StateError):
        audit.append_audit(sd, w.audit_record("a", 1, result="ok") | {"changed": None})
    with pytest.raises(StateError):
        audit.append_audit(sd, w.audit_record("a", 1, result="unknown") | {"changed": False})
    with pytest.raises(StateError):
        audit.append_audit(
            sd, w.audit_record("a", 1, result="attempted") | {"provider_ids": ["10001"]}
        )


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("actor", "root"),
        ("attempt_id", "short"),
        ("step_kind", "other"),
        ("decision_id", ["has space"]),
        ("provider_ids", ["x y"]),
        ("reason", "not_a_code"),
    ],
)
def test_pattern_fields_are_checked(sd: Path, field: str, value: object) -> None:
    with pytest.raises(StateError):
        audit.append_audit(sd, w.audit_record("a", 1) | {field: value})


def test_free_text_fields_cannot_carry_a_title_shaped_value(sd: Path) -> None:
    rec = w.audit_record("a", 1)
    rec["inputs"]["site_host"] = "Fix the login page for all users"
    with pytest.raises(StateError):
        audit.append_audit(sd, rec)
    rec = w.audit_record("a", 1)
    rec["inputs"]["token_env"] = SENTINEL + " value"
    with pytest.raises(StateError):
        audit.append_audit(sd, rec)


def test_append_audit_keeps_numbers_when_a_numeric_token_is_registered(sd: Path) -> None:
    sanitize.configure_scrubber(["12345678"])
    rec = w.audit_record("a", 1)
    rec["step"] = 912345678
    got = audit.append_audit(sd, rec)
    assert got["step"] == 912345678
    assert json.loads((sd / "audit.jsonl").read_bytes())["step"] == 912345678


def test_append_log_keeps_numbers_when_a_numeric_token_is_registered(sd: Path) -> None:
    sanitize.configure_scrubber(["12345678"])
    rec = w.log_record("a", 1) | {"duration_ms": 912345678}
    assert audit.append_log(sd, rec, warn=pytest.fail) is None
    (got,) = records(sd / "log.jsonl")
    assert got["duration_ms"] == 912345678


def test_sentinel_secret_and_ansi_never_reach_the_file(sd: Path) -> None:
    sanitize.configure_scrubber([SENTINEL])
    rec = w.audit_record("a", 1)
    rec["inputs"]["config"]["path"] = f"/x/{SENTINEL}/\x1b[31mred\x1b[0m/\x07\u202e"
    audit.append_audit(sd, rec)
    raw = (sd / "audit.jsonl").read_bytes()
    assert SENTINEL.encode() not in raw
    assert b"\x1b" not in raw and b"\\u001b" not in raw.lower()
    got = json.loads(raw)["inputs"]["config"]["path"]
    assert "<redacted>" in got and "<U+001B>" in got and "<U+202E>" in got


def test_a_secret_split_by_json_escaping_is_still_scrubbed(sd: Path) -> None:
    sanitize.configure_scrubber(['SENTINEL"quote\\back-slash-77'])
    rec = w.audit_record("a", 1)
    rec["inputs"]["config"]["path"] = 'p/SENTINEL"quote\\back-slash-77'
    audit.append_audit(sd, rec)
    raw = (sd / "audit.jsonl").read_text()
    assert "back-slash-77" not in raw


def test_an_oversized_record_is_refused_without_writing(sd: Path) -> None:
    rec = w.audit_record("a", 1)
    rec["inputs"]["config"]["path"] = "p" * 5000
    with pytest.raises(StateError):
        audit.append_audit(sd, rec)
    rec["decision_id"] = ["d"] * 17
    with pytest.raises(StateError):
        audit.append_audit(sd, rec)
    assert not (sd / "audit.jsonl").exists() or not (sd / "audit.jsonl").read_bytes()


def test_a_symlinked_audit_file_is_refused(sd: Path, tmp_path: Path) -> None:
    victim = tmp_path / "victim"
    victim.write_text("keep")
    (sd / "audit.jsonl").symlink_to(victim)
    with pytest.raises(StateError) as info:
        audit.append_audit(sd, w.audit_record("a", 1))
    assert info.value.reason == "state_symlink"
    assert victim.read_text() == "keep"


def test_a_foreign_owned_state_dir_is_refused(sd: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(state, "_euid", lambda: os.geteuid() + 1)
    with pytest.raises(StateError) as info:
        audit.append_audit(sd, w.audit_record("a", 1))
    assert (info.value.exit_code, info.value.code) == (1, "internal")


def test_a_full_disk_style_failure_raises_state_error(
    sd: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def boom(fd: int, data: bytes) -> int:
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(os, "write", boom)
    with pytest.raises(StateError) as info:
        audit.append_audit(sd, w.audit_record("a", 1))
    assert info.value.reason == "state_io"


def test_a_short_write_is_completed(sd: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    real = os.write
    monkeypatch.setattr(os, "write", lambda fd, data: real(fd, bytes(data)[:7]))
    audit.append_audit(sd, w.audit_record("a", 1))
    monkeypatch.undo()
    assert len(records(sd / "audit.jsonl")) == 1


# --- audit: concurrency ----------------------------------------------------------------------


def test_concurrent_appenders_never_interleave_and_seq_is_strict(sd: Path) -> None:
    workers, each = 6, 40
    go = CTX.Event()
    procs = [
        CTX.Process(target=w.append_audits, args=(str(sd), str(n), each, go))
        for n in range(workers)
    ]
    pids = []
    try:
        for p in procs:
            p.start()
            pids.append(p.pid)
        go.set()
        for p in procs:
            p.join(120)
            assert p.exitcode == 0
    finally:
        reap(procs)
    recs = records(sd / "audit.jsonl")
    assert [r["seq"] for r in recs] == list(range(1, workers * each + 1))
    assert len({r["attempt_id"] for r in recs}) == workers * each
    for n in range(workers):
        mine = [
            int(r["attempt_id"].split("-")[1]) for r in recs if r["attempt_id"].startswith(f"w{n}-")
        ]
        assert mine == list(range(each))


def test_a_killed_lock_holder_does_not_wedge_the_next_appender(sd: Path) -> None:
    path = sd / "audit.jsonl"
    path.write_bytes(b"")
    path.chmod(0o600)
    locked = CTX.Event()
    proc = CTX.Process(target=w.hold_flock, args=(str(path), locked))
    proc.start()
    pid = proc.pid
    try:
        assert locked.wait(60)
        with pytest.raises(StateError) as info:
            audit.append_audit(sd, w.audit_record("a", 1), lock_timeout=0.3)
        assert info.value.reason == "lock_timeout"
    finally:
        if proc.is_alive() and pid is not None:
            os.kill(pid, signal.SIGKILL)
            proc.join(10)
    assert audit.append_audit(sd, w.audit_record("a", 2), lock_timeout=5)["seq"] == 1


# --- request log -----------------------------------------------------------------------------


def test_log_line_has_the_documented_shape(sd: Path) -> None:
    assert audit.append_log(sd, w.log_record("a", 1), warn=pytest.fail) is None
    (rec,) = records(sd / "log.jsonl")
    assert rec["v"] == 1 and rec["path"] == "/rest/api/3/issue/{key}/wa/n1"
    assert set(rec) == {
        "v", "ts", "site", "host", "config_sha256", "method", "path", "status",
        "duration_ms", "retries", "rate_limit_remaining",
    }  # fmt: skip
    assert stat.S_IMODE((sd / "log.jsonl").stat().st_mode) == 0o600


@pytest.mark.parametrize(
    "patch",
    [
        {"path": "/rest/api/3/search?jql=secret"},
        {"path": "/a b"},
        {"path": "/rest/api/3/issue/{key}\n"},
        {"path": "/" + "a" * 300},
        {"method": "PATCH"},
        {"status": 99},
        {"status": 600},
        {"duration_ms": -1},
        {"host": "Example.Test/path"},
        {"site": "bad name"},
        {"config_sha256": "zz"},
        {"headers": {"Authorization": "Basic abc"}},
        {"body": "text"},
        {"title": "a title"},
    ],
)
def test_invalid_log_records_warn_and_write_nothing(sd: Path, patch: dict) -> None:
    got: list[str] = []
    warning = audit.append_log(sd, w.log_record("a", 1) | patch, warn=got.append)
    assert warning is not None and got == [warning]
    assert not (sd / "log.jsonl").exists() or not (sd / "log.jsonl").read_bytes()


def test_log_failure_warns_and_never_raises(sd: Path, tmp_path: Path) -> None:
    victim = tmp_path / "victim"
    victim.write_text("keep")
    (sd / "log.jsonl").symlink_to(victim)
    got: list[str] = []
    warning = audit.append_log(sd, w.log_record("a", 1), warn=got.append)
    assert warning and got == [warning]
    assert victim.read_text() == "keep"


def test_log_warning_text_is_sanitized_and_scrubbed(sd: Path) -> None:
    sanitize.configure_scrubber([SENTINEL])
    got: list[str] = []
    audit.append_log(sd, w.log_record("a", 1) | {"site": f"\x1b[31m{SENTINEL}"}, warn=got.append)
    assert got and SENTINEL not in got[0] and "\x1b" not in got[0]


def test_log_warns_to_stderr_by_default(sd: Path, capsys: pytest.CaptureFixture[str]) -> None:
    audit.append_log(sd, w.log_record("a", 1) | {"method": "X"})
    err = capsys.readouterr().err
    assert err.startswith("warning:") and err.endswith("\n")


def test_log_timeout_warns(sd: Path) -> None:
    path = sd / "log.jsonl"
    path.write_bytes(b"")
    path.chmod(0o600)
    locked = CTX.Event()
    proc = CTX.Process(target=w.hold_flock, args=(str(path), locked))
    proc.start()
    pid = proc.pid
    try:
        assert locked.wait(60)
        got: list[str] = []
        assert audit.append_log(sd, w.log_record("a", 1), warn=got.append, lock_timeout=0.2)
        assert got
    finally:
        if proc.is_alive() and pid is not None:
            os.kill(pid, signal.SIGKILL)
            proc.join(10)


def test_log_sentinel_in_an_allowed_field_is_refused_not_written(sd: Path) -> None:
    sanitize.configure_scrubber(["example.test"])
    warning = audit.append_log(
        sd, w.log_record("a", 1) | {"host": "example.test"}, warn=lambda m: None
    )
    assert warning is not None and "record_invalid" in warning
    assert not (sd / "log.jsonl").exists() or b"example.test" not in (sd / "log.jsonl").read_bytes()


# --- rotation --------------------------------------------------------------------------------


def test_default_rotation_limit_is_10_mib_with_three_archives() -> None:
    assert audit.LOG_MAX_BYTES == 10 * 1024 * 1024
    assert audit.LOG_ARCHIVES == 3


def archives(sd: Path) -> list[str]:
    return sorted(p.name for p in sd.iterdir() if p.name.startswith("log.jsonl"))


def test_rotation_shifts_archives_and_keeps_at_most_three(sd: Path) -> None:
    for i in range(400):
        assert audit.append_log(sd, w.log_record("a", i), max_bytes=2000, warn=pytest.fail) is None
    assert archives(sd) == ["log.jsonl", "log.jsonl.1", "log.jsonl.2", "log.jsonl.3"]
    for name in archives(sd):
        assert (sd / name).stat().st_size <= 2000
        assert stat.S_IMODE((sd / name).stat().st_mode) == 0o600
    # Newest lines are in the live file, older ones in higher archives, in order.
    seen = []
    for name in ("log.jsonl.3", "log.jsonl.2", "log.jsonl.1", "log.jsonl"):
        seen += [json.loads(x)["duration_ms"] for x in lines(sd / name)]
    assert seen == sorted(seen) and seen[-1] == 399 and len(set(seen)) == len(seen)


def test_rotation_happens_before_the_file_would_exceed_the_limit(sd: Path) -> None:
    one = len(json.dumps(w.log_record("a", 0))) + 80
    for i in range(10):
        audit.append_log(sd, w.log_record("a", i), max_bytes=one * 3, warn=pytest.fail)
    assert (sd / "log.jsonl.1").exists()


def test_a_line_larger_than_the_limit_is_still_written_alone(sd: Path) -> None:
    audit.append_log(sd, w.log_record("a", 1), max_bytes=10, warn=pytest.fail)
    audit.append_log(sd, w.log_record("a", 2), max_bytes=10, warn=pytest.fail)
    assert len(lines(sd / "log.jsonl")) == 1
    assert len(lines(sd / "log.jsonl.1")) == 1


def test_rotation_refuses_to_follow_a_symlinked_archive(sd: Path, tmp_path: Path) -> None:
    victim = tmp_path / "victim"
    victim.write_text("keep")
    (sd / "log.jsonl.1").symlink_to(victim)
    for i in range(30):
        audit.append_log(sd, w.log_record("a", i), max_bytes=1000, warn=lambda m: None)
    assert victim.read_text() == "keep"


def test_concurrent_appenders_across_rotation_lose_no_line(sd: Path) -> None:
    workers, each, limit = 5, 60, 30000
    go = CTX.Event()
    procs = [
        CTX.Process(target=w.append_logs, args=(str(sd), str(n), each, limit, go))
        for n in range(workers)
    ]
    pids = []
    try:
        for p in procs:
            p.start()
            pids.append(p.pid)
        go.set()
        for p in procs:
            p.join(120)
            assert p.exitcode == 0
    finally:
        reap(procs)
    names = archives(sd)
    assert len(names) >= 2 and len(names) <= 4 and "log.jsonl.4" not in names
    got = []
    for name in names:
        assert (sd / name).stat().st_size <= limit
        got += [json.loads(x)["path"] for x in lines(sd / name)]
    assert len(got) == workers * each == len(set(got))


def test_concurrent_appenders_with_many_rotations_keep_lines_whole(sd: Path) -> None:
    workers, each, limit = 5, 100, 3000
    go = CTX.Event()
    procs = [
        CTX.Process(target=w.append_logs, args=(str(sd), str(n), each, limit, go))
        for n in range(workers)
    ]
    pids = []
    try:
        for p in procs:
            p.start()
            pids.append(p.pid)
        go.set()
        for p in procs:
            p.join(120)
            assert p.exitcode == 0
    finally:
        reap(procs)
    names = archives(sd)
    assert set(names) <= {"log.jsonl", "log.jsonl.1", "log.jsonl.2", "log.jsonl.3"}
    got = []
    for name in names:
        got += [json.loads(x)["path"] for x in lines(sd / name)]
    assert len(got) == len(set(got)) and got
    assert not [n for n in os.listdir(sd) if n.startswith(".")]


def test_audit_is_never_rotated(sd: Path) -> None:
    for i in range(50):
        audit.append_audit(sd, w.audit_record("a", i))
    assert sorted(p.name for p in sd.iterdir()) == ["audit.jsonl"]


# --- schema parity ---------------------------------------------------------------------------


def schema(name: str) -> dict:
    return json.loads((ROOT / "schemas" / name).read_text())


def test_audit_key_sets_match_the_schema() -> None:
    s = schema("audit.json")
    assert set(audit.AUDIT_KEYS) == set(s["properties"])
    assert set(audit.AUDIT_REQUIRED) == set(s["required"])
    inputs = s["properties"]["inputs"]
    assert set(audit.INPUTS_KEYS) == set(inputs["properties"])
    assert set(audit.INPUTS_REQUIRED) == set(inputs["required"])


def test_log_key_sets_and_patterns_match_the_schema() -> None:
    s = schema("log.json")
    assert set(audit.LOG_KEYS) == set(s["properties"])
    assert set(audit.LOG_REQUIRED) == set(s["required"])
    assert audit.LOG_PATH.pattern == s["properties"]["path"]["pattern"]
    assert audit.LOG_HOST.pattern == s["properties"]["host"]["pattern"]
    assert audit.LOG_PATH_MAX == s["properties"]["path"]["maxLength"]
    assert list(audit.LOG_METHODS) == s["properties"]["method"]["enum"]


def test_audit_enums_match_the_schema() -> None:
    s = schema("audit.json")
    defs = schema("defs.json")["$defs"]
    assert list(audit.RESULTS) == s["properties"]["result"]["enum"]
    assert list(audit.ACTOR_SOURCES) == s["properties"]["actor_source"]["enum"]
    assert list(audit.STEP_KINDS) == defs["step_kind"]["enum"]
    assert list(audit.ACTIONS) == defs["action"]["enum"]
    assert list(audit.HOOK_NAMES) == defs["hook_name"]["enum"]
    assert list(audit.ERROR_CODES) == defs["error_code"]["enum"]
    assert audit.TS.pattern == defs["ts"]["pattern"]
