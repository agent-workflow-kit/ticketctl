"""``export``: filters, tolerance of garbage, ``--to`` safety, sanitizing (issue #36)."""

from __future__ import annotations

import json
import os
import re
import stat
from pathlib import Path
from typing import Any

import pytest

from tests import state_workers as w
from tests.golden import assert_golden
from ticketctl import audit, exportcmd, state
from ticketctl.cli import main

SECRET = "tok-sentinel-0123456789"


@pytest.fixture
def sd(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    base = tmp_path / "xdg-state"
    monkeypatch.setenv("XDG_STATE_HOME", str(base))
    path = base / "ticketctl"
    fd = state.open_dir(path)
    assert fd is not None
    os.close(fd)
    return path


def run(capsys: pytest.CaptureFixture[str], *argv: str) -> tuple[int, str, str]:
    code = main(list(argv))
    cap = capsys.readouterr()
    return code, cap.out, cap.err


def out_records(out: str) -> list[dict[str, Any]]:
    return [json.loads(x) for x in out.splitlines()]


def seqs(out: str) -> list[int]:
    return [r["seq"] for r in out_records(out)]


def fill(sd: Path, n: int, **kw: Any) -> list[dict[str, Any]]:
    return [audit.append_audit(sd, w.audit_record("e", i, **kw)) for i in range(n)]


def raw(sd: Path, data: bytes) -> None:
    with open(sd / "audit.jsonl", "ab") as fh:
        fh.write(data)


def test_prints_every_record_in_order_and_matches_the_schema(
    sd: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    written = fill(sd, 3)
    code, out, err = run(capsys, "export")
    assert (code, err) == (0, "")
    assert out_records(out) == written


def test_golden_record(sd: Path, capsys: pytest.CaptureFixture[str]) -> None:
    audit.append_audit(sd, w.audit_record("g", 1) | {"ts": "2026-01-02T03:04:05Z"})
    _, out, _ = run(capsys, "export")
    assert_golden("cli", "export", "record", json.loads(out))


def test_json_flag_has_no_extra_effect(sd: Path, capsys: pytest.CaptureFixture[str]) -> None:
    fill(sd, 2)
    a = run(capsys, "export")
    b = run(capsys, "--json", "export")
    assert a == b


def test_no_state_dir_is_an_empty_export_and_creates_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "nothing"))
    assert run(capsys, "export") == (0, "", "")
    assert not (tmp_path / "nothing").exists()


def test_missing_audit_file_creates_nothing(sd: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert run(capsys, "export") == (0, "", "")
    assert not (sd / "audit.jsonl").exists()


def test_after_seq_resumes_exactly_after_the_last_record(
    sd: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    fill(sd, 5)
    _, out, _ = run(capsys, "export", "--after-seq", "2")
    assert seqs(out) == [3, 4, 5]
    _, out, _ = run(capsys, "export", "--after-seq", "5")
    assert out == ""
    _, out, _ = run(capsys, "export", "--after-seq", "0")
    assert seqs(out) == [1, 2, 3, 4, 5]


def test_since_and_after_seq_are_anded(sd: Path, capsys: pytest.CaptureFixture[str]) -> None:
    for i, ts in enumerate(
        ["2026-01-01T00:00:00Z", "2026-02-01T00:00:00Z", "2026-03-01T00:00:00Z"]
    ):
        audit.append_audit(sd, w.audit_record("e", i) | {"ts": ts})
    _, out, _ = run(capsys, "export", "--since", "2026-02-01")
    assert seqs(out) == [2, 3]  # inclusive, bare date is 00:00 UTC
    _, out, _ = run(capsys, "export", "--since", "2026-02-01", "--after-seq", "2")
    assert seqs(out) == [3]
    _, out, _ = run(capsys, "export", "--since", "2026-02-01T01:00:00+01:00")
    assert seqs(out) == [2, 3]
    _, out, _ = run(capsys, "export", "--since", "2026-02-01T00:00:01Z")
    assert seqs(out) == [3]


def test_since_duration_counts_back_from_now(sd: Path, capsys: pytest.CaptureFixture[str]) -> None:
    audit.append_audit(sd, w.audit_record("e", 0) | {"ts": "2001-01-01T00:00:00Z"})
    audit.append_audit(sd, w.audit_record("e", 1))
    _, out, _ = run(capsys, "export", "--since", "1d")
    assert seqs(out) == [2]


@pytest.mark.parametrize("value", ["yesterday", "2026-13-01", "5x", "", "2026-01-01T00:00:00"])
def test_bad_since_is_invalid_value(
    sd: Path, value: str, capsys: pytest.CaptureFixture[str]
) -> None:
    fill(sd, 1)
    code, out, _ = run(capsys, "--json", "export", "--since", value)
    assert code == 22 and json.loads(out)["error"]["reason"] == "invalid_value"


def test_garbage_lines_are_skipped_with_one_warning(
    sd: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    fill(sd, 1)
    raw(sd, b"not json\n")
    raw(sd, b'{"v":2,"seq":50}\n')
    raw(sd, b'{"v":true,"seq":51}\n')
    raw(sd, b'{"seq":52}\n')
    raw(sd, b"[1,2]\n")
    raw(sd, b'{"v":1,"seq":' + str(2**53 + 1).encode() + b"}\n")
    raw(sd, b'{"v":1,"seq":0}\n')
    raw(sd, b'{"v":1,"seq":"7"}\n')
    raw(sd, b'{"v":1,"seq":true}\n')
    raw(sd, b'{"v":1,"seq":NaN,"x":1}\n')
    raw(sd, b'{"v":1,"seq":9,"x":NaN}\n')
    raw(sd, b"\xff\xfe\n")
    raw(sd, b"[" * 200000 + b"\n")
    raw(sd, b'{"v":1,"seq":3,"future":"yes"}\n')
    raw(sd, b'{"v":1,"seq":4}')  # torn tail, no newline
    code, out, err = run(capsys, "export")
    assert code == 0
    assert seqs(out) == [1, 3]
    assert out_records(out)[1]["future"] == "yes"  # not validated against the schema
    assert len(err.strip().splitlines()) == 1 and err.startswith("warning:")


def test_quiet_drops_the_warning(sd: Path, capsys: pytest.CaptureFixture[str]) -> None:
    raw(sd, b"junk\n")
    assert run(capsys, "--quiet", "export") == (0, "", "")


def test_huge_garbage_line_is_bounded_and_the_rest_still_reads(
    sd: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    fill(sd, 1)
    raw(sd, b"x" * (5 * 1024 * 1024) + b"\n")
    raw(sd, b"y" * (audit.MAX_RECORD_BYTES + 1) + b"\n")
    raw(sd, b"z" * (audit.MAX_RECORD_BYTES + 1))  # no newline, then the next record's repair
    fill(sd, 1)  # append_audit writes the repairing newline
    _, out, err = run(capsys, "export")
    assert seqs(out) == [1, 2]
    assert err.count("warning:") == 1


def test_line_at_the_limit_is_kept_and_one_over_is_not(sd: Path) -> None:
    pad = audit.MAX_RECORD_BYTES - len(b'{"v":1,"seq":1,"p":""}\n')
    ok = b'{"v":1,"seq":1,"p":"' + b"a" * pad + b'"}\n'
    assert len(ok) == audit.MAX_RECORD_BYTES
    raw(sd, ok)
    raw(sd, b'{"v":1,"seq":2,"p":"' + b"a" * (pad + 1) + b'"}\n')
    skips = exportcmd.Skips()
    got = list(exportcmd.read_records(sd, skips=skips))
    assert [r["seq"] for r in got] == [1] and skips.count == 1


def test_records_appended_after_the_open_are_not_read(sd: Path) -> None:
    fill(sd, 2)
    skips = exportcmd.Skips()
    gen = exportcmd.read_records(sd, skips=skips)
    first = next(gen)
    fill(sd, 3)
    assert [first["seq"], *[r["seq"] for r in gen]] == [1, 2]


def test_concurrent_appender_never_produces_a_bad_line(
    sd: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    import threading

    stop = threading.Event()

    def writer() -> None:
        i = 0
        while not stop.is_set() and i < 300:
            audit.append_audit(sd, w.audit_record("c", i))
            i += 1

    t = threading.Thread(target=writer)
    t.start()
    try:
        for _ in range(20):
            code, out, err = run(capsys, "export")
            assert code == 0 and err == ""
            got = seqs(out)
            assert got == list(range(1, len(got) + 1))
    finally:
        stop.set()
        t.join()


def test_output_is_sanitized_and_scrubbed_again(
    sd: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    cfg = sd.parent / "config.yaml"
    cfg.write_text(
        "sites:\n  main:\n    url: https://example.test\n    email: a@example.test\n"
        "    token_env: EXPORT_TEST_TOKEN\n"
    )
    monkeypatch.setenv("EXPORT_TEST_TOKEN", SECRET)
    evil = (
        '{"v":1,"seq":1,"ts":"2026-01-01T00:00:00Z","note":"a\\u202eb\\u0000c ' + SECRET + '",'
        '"k\\u202e":1,"nest":[{"x":"\\u200b' + SECRET + '"}]}\n'
    ).encode()
    raw(sd, evil)
    code, out, _ = run(capsys, "--config", str(cfg), "export")
    assert code == 0
    assert SECRET not in out
    assert not re.search(r"[^\x20-\x7e\n]", out)
    rec = json.loads(out)
    assert "<U+202E>" in rec["note"] and "<U+0000>" in rec["note"] and "<redacted>" in rec["note"]
    assert "k<U+202E>" in rec


def test_broken_config_does_not_stop_an_export(
    sd: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    fill(sd, 1)
    bad = tmp_path / "bad.yaml"
    bad.write_text("sites: [\n")
    code, out, _ = run(capsys, "--config", str(bad), "export")
    assert code == 0 and seqs(out) == [1]


def test_symlinked_or_special_audit_file_is_refused(
    sd: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    target = tmp_path / "elsewhere"
    target.write_text('{"v":1,"seq":1}\n')
    (sd / "audit.jsonl").symlink_to(target)
    code, out, _ = run(capsys, "--json", "export")
    assert code == 1 and json.loads(out)["error"]["code"] == "internal" and out.count("seq") == 0


def test_fifo_audit_file_is_refused_without_hanging(
    sd: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    os.mkfifo(sd / "audit.jsonl")
    code, _, _ = run(capsys, "export")
    assert code == 1


def test_wide_permissions_are_tightened(sd: Path, capsys: pytest.CaptureFixture[str]) -> None:
    fill(sd, 1)
    os.chmod(sd / "audit.jsonl", 0o644)
    assert run(capsys, "export")[0] == 0
    assert stat.S_IMODE((sd / "audit.jsonl").stat().st_mode) == 0o600


# --- --to ------------------------------------------------------------------------------------


def test_to_creates_a_0600_file_and_prints_nothing(
    sd: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    written = fill(sd, 3)
    dest = tmp_path / "out.jsonl"
    code, out, err = run(capsys, "export", "--to", str(dest))
    assert (code, out, err) == (0, "", "")
    assert stat.S_IMODE(dest.stat().st_mode) == 0o600
    assert [json.loads(x) for x in dest.read_text().splitlines()] == written


def test_to_relative_path(
    sd: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    fill(sd, 1)
    monkeypatch.chdir(tmp_path)
    assert run(capsys, "export", "--to", "rel.jsonl")[0] == 0
    assert (tmp_path / "rel.jsonl").exists()


def test_to_existing_file_is_exit_20_and_untouched(
    sd: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    fill(sd, 1)
    dest = tmp_path / "out.jsonl"
    dest.write_text("keep")
    code, out, _ = run(capsys, "--json", "export", "--to", str(dest))
    body = json.loads(out)
    assert code == 20 and body["error"]["code"] == "already_exists"
    assert dest.read_text() == "keep"


def test_to_symlink_fails_and_creates_nothing(
    sd: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    fill(sd, 1)
    victim = tmp_path / "victim"
    link = tmp_path / "link"
    link.symlink_to(victim)  # dangling
    code, _, _ = run(capsys, "export", "--to", str(link))
    assert code == 20 and not victim.exists()
    real = tmp_path / "real"
    real.write_text("keep")
    link2 = tmp_path / "link2"
    link2.symlink_to(real)
    assert run(capsys, "export", "--to", str(link2))[0] == 20
    assert real.read_text() == "keep"


def test_to_fifo_is_refused(sd: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    fill(sd, 1)
    os.mkfifo(tmp_path / "pipe")
    assert run(capsys, "export", "--to", str(tmp_path / "pipe"))[0] == 20


@pytest.mark.parametrize("name", ["", ".", "..", "dir/", "dir/.."])
def test_to_bad_names_are_invalid(
    sd: Path, tmp_path: Path, name: str, capsys: pytest.CaptureFixture[str]
) -> None:
    fill(sd, 1)
    (tmp_path / "dir").mkdir()
    arg = f"{tmp_path}/{name}" if name else ""
    code, _, _ = run(capsys, "export", "--to", arg)
    assert code == 22


def test_to_missing_parent_is_invalid_file(
    sd: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    fill(sd, 1)
    code, out, _ = run(capsys, "--json", "export", "--to", str(tmp_path / "no" / "x"))
    assert code == 22 and json.loads(out)["error"]["reason"] == "invalid_file"
    assert not (tmp_path / "no").exists()


def test_to_parent_not_owned_is_refused(
    sd: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    fill(sd, 1)
    monkeypatch.setattr(exportcmd, "_euid", lambda: os.geteuid() + 1)
    code, out, _ = run(capsys, "--json", "export", "--to", str(tmp_path / "x"))
    assert code == 22 and json.loads(out)["error"]["reason"] == "invalid_file"
    assert not (tmp_path / "x").exists()


def test_bad_filter_creates_no_file(
    sd: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    code, _, _ = run(capsys, "export", "--since", "nope", "--to", str(tmp_path / "x"))
    assert code == 22 and not (tmp_path / "x").exists()


def test_failed_write_removes_the_new_file(
    sd: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    fill(sd, 1)

    def boom(fd: int, data: bytes) -> None:
        raise state.StateError("state_io", "disk full")

    monkeypatch.setattr(exportcmd.state, "write_all", boom)
    code, _, _ = run(capsys, "export", "--to", str(tmp_path / "x"))
    assert code == 1 and not (tmp_path / "x").exists()


def test_large_export_streams_in_chunks(sd: Path, tmp_path: Path) -> None:
    for i in range(300):
        raw(sd, json.dumps({"v": 1, "seq": i + 1, "pad": "p" * 1000}).encode() + b"\n")
    chunks = list(exportcmd._chunks(exportcmd.read_records(sd)))
    assert len(chunks) > 1 and all(c.endswith("\n") for c in chunks)
    assert sum(c.count("\n") for c in chunks) == 300


def test_process_run_end_to_end(tmp_path: Path) -> None:
    import subprocess
    import sys

    state_home = tmp_path / "state"
    path = state_home / "ticketctl"
    fd = state.open_dir(path)
    assert fd is not None
    os.close(fd)
    audit.append_audit(path, w.audit_record("p", 1))
    env = {
        "HOME": str(tmp_path),
        "XDG_CONFIG_HOME": str(tmp_path / "cfg"),
        "XDG_STATE_HOME": str(state_home),
        "PATH": "",
        "PYTHONPATH": os.pathsep.join(sys.path),
    }
    r = subprocess.run(
        [sys.executable, "-m", "ticketctl", "export"],
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert r.returncode == 0 and seqs(r.stdout) == [1]


def test_torn_tail_alone_is_silent_and_not_printed(
    sd: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    fill(sd, 1)
    raw(sd, b'{"v":1,"seq":2}')
    code, out, err = run(capsys, "export")
    assert (code, seqs(out), err) == (0, [1], "")
