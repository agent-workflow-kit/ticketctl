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


# --- review round 1 ---------------------------------------------------------------------------


def _config(sd: Path, text: str) -> Path:
    cfg = sd.parent / "config.yaml"
    cfg.write_text(text)
    return cfg


def _site(name: str, token_env: str, email: str) -> str:
    return (
        f"  {name}:\n    url: https://{name}.example.test\n    {email}\n"
        f"    token_env: {token_env}\n"
    )


def _child_env(tmp_path: Path) -> dict[str, str]:
    import sys

    return {
        "HOME": str(tmp_path),
        "XDG_CONFIG_HOME": str(tmp_path / "cfg"),
        "XDG_STATE_HOME": str(tmp_path / "state"),
        "PATH": "",
        "PYTHONPATH": os.pathsep.join(sys.path),
    }


def _big_state(tmp_path: Path, count: int) -> None:
    path = tmp_path / "state" / "ticketctl"
    fd = state.open_dir(path)
    assert fd is not None
    os.close(fd)
    with open(path / "audit.jsonl", "ab") as fh:
        for i in range(count):
            fh.write(json.dumps({"v": 1, "seq": i + 1, "pad": "p" * 1000}).encode() + b"\n")


def test_valid_seq_is_the_writers_rule() -> None:
    assert audit.valid_seq(1) and audit.valid_seq(2**53)
    assert not any(audit.valid_seq(x) for x in (0, 2**53 + 1, True, "1", 1.0, None))


def test_stdout_to_a_full_device_exits_1_internal(tmp_path: Path) -> None:
    import subprocess
    import sys

    if not os.path.exists("/dev/full"):
        pytest.skip("no /dev/full")
    _big_state(tmp_path, 10)
    with open("/dev/full", "wb") as full:
        r = subprocess.run(
            [sys.executable, "-m", "ticketctl", "export"],
            env=_child_env(tmp_path),
            stdout=full,
            stderr=subprocess.PIPE,
            text=True,
            timeout=60,
            check=False,
        )
    assert r.returncode == 1 and "internal" in r.stderr and "Traceback" not in r.stderr


def test_closed_stdout_pipe_stops_quietly_with_exit_141(tmp_path: Path) -> None:
    import subprocess
    import sys

    _big_state(tmp_path, 3000)
    proc = subprocess.Popen(
        [sys.executable, "-m", "ticketctl", "export"],
        env=_child_env(tmp_path),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        assert proc.stdout is not None
        assert proc.stdout.readline().startswith("{")
        proc.stdout.close()
        _, err = proc.communicate(timeout=60)
    except BaseException:
        proc.kill()  # our own child, by its Popen handle
        proc.wait()
        raise
    assert proc.returncode == 141 and err == ""


def test_numeric_token_is_redacted_identically_on_stdout_and_to(
    sd: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    cfg = _config(sd, "sites:\n" + _site("main", "EXPORT_TEST_TOKEN", "email: a@example.test"))
    monkeypatch.setenv("EXPORT_TEST_TOKEN", "123456789012")
    raw(sd, b'{"v":1,"seq":1,"n":123456789012,"f":123456789012.5,"m":[123456789012]}\n')
    _, out, _ = run(capsys, "--config", str(cfg), "export")
    dest = tmp_path / "out.jsonl"
    run(capsys, "--config", str(cfg), "export", "--to", str(dest))
    rec = json.loads(out)
    assert rec["n"] == "<redacted>" and rec["m"] == ["<redacted>"] and rec["f"] == "<redacted>"
    assert dest.read_text() == out


def test_over_long_unterminated_tail_is_silent(
    sd: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    fill(sd, 1)
    raw(sd, b"t" * 70000)
    code, out, err = run(capsys, "export")
    assert (code, seqs(out), err) == (0, [1], "")


def test_since_compares_instants_and_counts_records_without_a_usable_ts(
    sd: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    raw(sd, b'{"v":1,"seq":1,"ts":"2026-02-01T00:00:00.500Z"}\n')  # fraction, same second
    raw(sd, b'{"v":1,"seq":2,"ts":"2026-02-01T01:30:00+02:00"}\n')  # 2026-01-31T23:30Z: before
    raw(sd, b'{"v":1,"seq":3,"ts":"2026-02-01T02:00:00+02:00"}\n')  # 2026-02-01T00:00Z: kept
    raw(sd, b'{"v":1,"seq":4}\n')
    raw(sd, b'{"v":1,"seq":5,"ts":"yesterday"}\n')
    raw(sd, b'{"v":1,"seq":6,"ts":7}\n')
    code, out, err = run(capsys, "export", "--since", "2026-02-01")
    assert code == 0 and seqs(out) == [1, 3]
    assert err.startswith("warning: 3 line(s)")
    _, out, err = run(capsys, "export")  # without --since the ts is not looked at
    assert seqs(out) == [1, 2, 3, 4, 5, 6] and err == ""
    _, out, err = run(capsys, "export", "--since", "2026-02-01", "--after-seq", "5")
    assert seqs(out) == [] and err.startswith("warning: 1 line(s)")


def test_a_bad_site_skips_only_itself_when_arming_the_scrub(
    sd: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    cfg = _config(
        sd,
        "sites:\n"
        + _site("first", "EXPORT_TEST_TOKEN_A", "email_env: EXPORT_TEST_EMAIL")
        + _site("second", "EXPORT_TEST_TOKEN_B", "email: b@example.test"),
    )
    monkeypatch.setenv("EXPORT_TEST_TOKEN_A", "tok-aaaa-0123456789")
    monkeypatch.setenv("EXPORT_TEST_TOKEN_B", "tok-bbbb-0123456789")
    monkeypatch.setenv("EXPORT_TEST_EMAIL", "bad\udcffmail@example.test")
    raw(sd, b'{"v":1,"seq":1,"a":"tok-aaaa-0123456789","b":"tok-bbbb-0123456789"}\n')
    code, out, _ = run(capsys, "--config", str(cfg), "export")
    assert code == 0 and "tok-aaaa" not in out and "tok-bbbb" not in out


def test_interrupt_before_the_write_leaves_no_file(
    sd: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    fill(sd, 1)

    def stop(*args: object, **kwargs: object) -> None:
        raise KeyboardInterrupt

    monkeypatch.setattr(exportcmd, "load_runtime", stop)
    code, _, _ = run(capsys, "export", "--to", str(tmp_path / "x"))
    assert code == 130 and not (tmp_path / "x").exists()


def test_interrupt_after_the_create_removes_the_file(
    sd: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    fill(sd, 1)

    def stop(*args: object, **kwargs: object) -> None:
        raise KeyboardInterrupt

    monkeypatch.setattr(exportcmd, "read_records", stop)
    code, _, _ = run(capsys, "export", "--to", str(tmp_path / "x"))
    assert code == 130 and not (tmp_path / "x").exists()


@pytest.mark.parametrize("mask", [0o022, 0o077, 0o277, 0o777, 0])
def test_to_is_0600_whatever_the_umask(
    sd: Path, tmp_path: Path, mask: int, capsys: pytest.CaptureFixture[str]
) -> None:
    fill(sd, 1)
    old = os.umask(mask)
    try:
        code = run(capsys, "export", "--to", str(tmp_path / "x"))[0]
    finally:
        os.umask(old)
    assert code == 0 and stat.S_IMODE((tmp_path / "x").stat().st_mode) == 0o600


def test_a_replaced_to_file_survives_a_failed_write(
    sd: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    fill(sd, 1)
    dest = tmp_path / "x"

    def swap(fd: int, data: bytes) -> None:
        dest.unlink()
        dest.write_text("someone else's")
        raise state.StateError("state_io", "disk full")

    monkeypatch.setattr(exportcmd.state, "write_all", swap)
    code, _, _ = run(capsys, "export", "--to", str(dest))
    assert code == 1 and dest.read_text() == "someone else's"


def test_read_failure_after_the_create_leaves_no_file(
    sd: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    target = tmp_path / "elsewhere"
    target.write_text('{"v":1,"seq":1}\n')
    (sd / "audit.jsonl").symlink_to(target)
    code, _, _ = run(capsys, "export", "--to", str(tmp_path / "x"))
    assert code == 1 and not (tmp_path / "x").exists()


def test_rotated_archives_are_ignored(sd: Path, capsys: pytest.CaptureFixture[str]) -> None:
    fill(sd, 1)
    (sd / "audit.jsonl.1").write_text('{"v":1,"seq":99}\n')
    _, out, _ = run(capsys, "export")
    assert seqs(out) == [1]


def test_to_with_skipped_lines_warns_once_on_stderr(
    sd: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    fill(sd, 1)
    raw(sd, b"junk\n")
    dest = tmp_path / "x"
    code, out, err = run(capsys, "export", "--to", str(dest))
    assert code == 0 and out == "" and err.startswith("warning: 1 line(s)")
    assert len(err.strip().splitlines()) == 1 and len(dest.read_text().splitlines()) == 1
