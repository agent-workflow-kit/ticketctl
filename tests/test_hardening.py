"""Review round 1 hardening: log repair, scrub revalidation, caps, cache edge cases, parity.

Every test uses a private state directory under ``tmp_path``; none starts an external program.
"""

from __future__ import annotations

import inspect
import json
import os
import pwd
import re
import stat
import threading
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from tests import state_workers as w
from ticketctl import audit, cache, sanitize, state
from ticketctl.state import StateError

ROOT = Path(__file__).resolve().parent.parent
URL = "https://example.test"
EMAIL = "me@example.test"
NOW = datetime(2026, 5, 1, 12, 0, 0, tzinfo=UTC)
TTL = timedelta(hours=24)


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


def put(sd: Path, data: dict | None = None, *, now: datetime = NOW, **kw: object) -> None:
    args: dict = {"url": URL, "email": EMAIL, "now": now}
    args.update(kw)
    cache.write_cache(sd, "main", "fields", data if data is not None else {"fields": [1]}, **args)


def get(sd: Path, *, now: datetime = NOW, **kw: object) -> dict | None:
    args: dict = {"url": URL, "email": EMAIL, "ttl": TTL, "now": now}
    args.update(kw)
    return cache.read_cache(sd, "main", "fields", **args)


def file_of(sd: Path) -> Path:
    return sd / "cache" / "main" / "fields.json"


# --- 1: request log torn-tail repair ---------------------------------------------------------


def test_log_torn_tail_is_repaired_before_the_next_record(sd: Path) -> None:
    assert audit.append_log(sd, w.log_record("a", 1), warn=lambda m: None) is None
    with (sd / "log.jsonl").open("ab") as fh:
        fh.write(b'{"v":1,"ts":"2026')
    assert audit.append_log(sd, w.log_record("a", 2), warn=lambda m: None) is None
    parsed = []
    bad = 0
    for line in (sd / "log.jsonl").read_text().split("\n")[:-1]:
        try:
            parsed.append(json.loads(line))
        except ValueError:
            bad += 1
    assert bad == 1
    assert [p["duration_ms"] for p in parsed] == [1, 2]


# --- 2: validation after the scrub -----------------------------------------------------------


def test_audit_refuses_when_the_scrub_breaks_the_schema(sd: Path) -> None:
    rec = w.audit_record("a", 1)
    rec["attempt_id"] = "attempt-secret-1234"
    sanitize.configure_scrubber(["attempt-secret-1234"])
    with pytest.raises(StateError) as info:
        audit.append_audit(sd, rec)
    assert info.value.reason == "record_invalid"
    assert not (sd / "audit.jsonl").exists() or not (sd / "audit.jsonl").read_bytes()


def test_audit_refuses_when_rendering_expands_a_value_past_its_limit(sd: Path) -> None:
    rec = w.audit_record("a", 1)
    rec["inputs"]["config"]["path"] = "\x01" * 3000
    with pytest.raises(StateError) as info:
        audit.append_audit(sd, rec)
    assert info.value.reason == "record_invalid"
    assert not (sd / "audit.jsonl").exists() or not (sd / "audit.jsonl").read_bytes()


def test_log_refuses_when_the_scrub_breaks_the_schema(sd: Path) -> None:
    sanitize.configure_scrubber(["example.test"])
    got: list[str] = []
    warning = audit.append_log(sd, w.log_record("a", 1), warn=got.append)
    assert warning is not None and "record_invalid" in warning
    assert not (sd / "log.jsonl").exists() or not (sd / "log.jsonl").read_bytes()


# --- 3: no caps beyond the schema ------------------------------------------------------------


def test_lists_longer_than_the_old_caps_are_accepted(sd: Path) -> None:
    rec = w.audit_record("a", 1)
    rec["inputs"]["bodies"] = [{"sha256": "a" * 64, "bytes": 1}] * 100
    rec["inputs"]["config"]["files_sha256"] = ["b" * 64] * 100
    rec["provider_ids"] = [f"id{i}" for i in range(100)]
    audit.append_audit(sd, rec)
    rec2 = w.audit_record("a", 2)
    rec2["inputs"]["cli_hooks"] = list(audit.HOOK_NAMES)
    audit.append_audit(sd, rec2)


def test_cli_hooks_must_still_be_unique(sd: Path) -> None:
    rec = w.audit_record("a", 1)
    rec["inputs"]["cli_hooks"] = ["pre-comment", "pre-comment"]
    with pytest.raises(StateError):
        audit.append_audit(sd, rec)


# --- 4: schema parity ------------------------------------------------------------------------


def _schema(name: str) -> dict:
    return json.loads((ROOT / "schemas" / name).read_text())


def _resolve(node: dict, base: str) -> dict:
    ref = node.get("$ref")
    if ref is None:
        return node
    file, _, pointer = ref.partition("#")
    doc = _schema(file or base)
    for part in pointer.strip("/").split("/"):
        doc = doc[part]
    return doc


def _prop(schema_name: str, *path: str) -> dict:
    node = _schema(schema_name)
    for key in path:
        node = _resolve(node, schema_name)
        node = node["properties"][key]
    return _resolve(node, schema_name)


def _items(node: dict, schema_name: str) -> dict:
    return _resolve(node["items"], schema_name)


def _def(name: str) -> dict:
    return _schema("defs.json")["$defs"][name]


@pytest.mark.parametrize(
    ("pattern", "node"),
    [
        (audit.SITE, _def("site_name")),
        (audit.SHA256, _def("sha256")),
        (audit.ATTEMPT_ID, _def("attempt_id")),
        (audit.DECISION_ID, _def("decision_id")),
        (audit.PROVIDER_ID, _def("provider_id")),
        (audit.ACTOR, _prop("audit.json", "actor")),
        (audit.TOKEN_ENV, _prop("audit.json", "inputs", "token_env")),
        (audit.LOG_HOST, _prop("audit.json", "inputs", "site_host")),
        (audit.LOG_HOST, _prop("log.json", "host")),
        (audit.LOG_PATH, _prop("log.json", "path")),
        (audit.TS, _def("ts")),
    ],
)
def test_every_mirrored_pattern_equals_the_schema(pattern: re.Pattern[str], node: dict) -> None:
    assert pattern.pattern == node["pattern"]


def test_ticket_key_pattern_and_length_match_the_schema() -> None:
    key = _prop("audit.json", "target", "key")
    options = [_resolve(o, "audit.json") for o in key["oneOf"]]
    typed = next(o for o in options if o.get("type") == "string")
    assert audit.TICKET_KEY.pattern == typed["pattern"]
    assert typed["maxLength"] == 64


def test_length_limits_match_the_schema() -> None:
    assert _prop("audit.json", "inputs", "site_host")["maxLength"] == 300
    assert _prop("log.json", "host")["maxLength"] == 300
    assert _prop("log.json", "path")["maxLength"] == audit.LOG_PATH_MAX
    path = _prop("audit.json", "inputs", "config", "path")
    typed = next(o for o in path["oneOf"] if o.get("type") == "string")
    assert typed["maxLength"] == 4096
    # the key length the code applies to ticket keys and the site-name bound
    assert audit.SITE.fullmatch("a" * 64) and not audit.SITE.fullmatch("a" * 65)


def test_item_limits_match_the_schema() -> None:
    assert _def("decision_ids")["maxItems"] == 16
    hooks = _prop("audit.json", "inputs", "cli_hooks")
    assert hooks["uniqueItems"] is True and "maxItems" not in hooks
    for path in (("inputs", "config", "files_sha256"), ("inputs", "bodies"), ("provider_ids",)):
        node = _prop("audit.json", *path)
        assert "maxItems" not in node and "uniqueItems" not in node
    rules = _schema("audit.json")["allOf"]
    caps = [
        r["then"]["properties"]["provider_ids"]["maxItems"]
        for r in rules
        if "provider_ids" in r.get("then", {}).get("properties", {})
    ]
    assert caps == [0]


def test_a_decision_id_list_of_16_passes_and_17_fails(sd: Path) -> None:
    rec = w.audit_record("a", 1)
    rec["decision_id"] = ["d"] * 16
    audit.append_audit(sd, rec)
    rec["decision_id"] = ["d"] * 17
    with pytest.raises(StateError):
        audit.append_audit(sd, rec)


def test_limits_hold_for_the_values_the_schema_allows(sd: Path) -> None:
    rec = w.audit_record("a", 1)
    rec["inputs"]["site_host"] = "a" * 300
    with pytest.raises(StateError):
        audit.append_audit(sd, rec)
    rec = w.audit_record("a", 1)
    rec["target"]["key"] = "K-" + "1" * 63
    with pytest.raises(StateError):
        audit.append_audit(sd, rec)
    rec["target"]["key"] = "K-" + "1" * 62
    audit.append_audit(sd, rec)


# --- 5: mode 000 files -----------------------------------------------------------------------


def _dir_fd(path: Path) -> int:
    fd = state.open_dir(path)
    assert fd is not None
    return fd


def test_open_file_tightens_a_mode_000_file_it_owns(tmp_path: Path) -> None:
    target = tmp_path / "ticketctl"
    fd = _dir_fd(target)
    (target / "audit.jsonl").write_text("x\n")
    (target / "audit.jsonl").chmod(0)
    try:
        opened = state.open_file(fd, "audit.jsonl", create=False)
        assert opened is not None
        os.close(opened)
    finally:
        os.close(fd)
    assert stat.S_IMODE((target / "audit.jsonl").stat().st_mode) == 0o600


def test_audit_append_works_over_a_mode_000_file(sd: Path) -> None:
    audit.append_audit(sd, w.audit_record("a", 1))
    (sd / "audit.jsonl").chmod(0)
    assert audit.append_audit(sd, w.audit_record("a", 2))["seq"] == 2


def test_a_mode_000_cache_file_is_tightened_and_read(sd: Path) -> None:
    put(sd)
    file_of(sd).chmod(0)
    assert get(sd) is not None
    assert stat.S_IMODE(file_of(sd).stat().st_mode) == 0o600


# --- 7, 8: cache name and entry type ---------------------------------------------------------


def test_a_bad_site_name_is_a_state_error(sd: Path) -> None:
    with pytest.raises(StateError) as info:
        cache.write_cache(sd, "../x", "fields", {}, url=URL, email=EMAIL)
    assert info.value.reason == "state_type"
    with pytest.raises(StateError) as info:
        cache.read_cache(sd, "bad name", "fields", url=URL, email=EMAIL, ttl=TTL)
    assert info.value.reason == "state_type"


def test_write_cache_over_a_fifo_is_refused_and_leaves_it(sd: Path) -> None:
    put(sd)
    file_of(sd).unlink()
    os.mkfifo(file_of(sd))
    with pytest.raises(StateError) as info:
        put(sd)
    assert info.value.reason == "state_type"
    assert stat.S_ISFIFO(file_of(sd).lstat().st_mode)
    assert [p.name for p in file_of(sd).parent.iterdir()] == ["fields.json"]


# --- 9: discard only the file that was read --------------------------------------------------


def test_discard_does_not_delete_a_file_swapped_in_after_the_read(
    sd: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    put(sd, {"fields": ["old"]})
    real_valid = cache._valid

    def swap_then_reject(*a: Any, **k: Any) -> None:
        real_valid(*a, **k)
        fresh = file_of(sd).with_name("fresh")
        fresh.write_text("fresh")
        os.replace(fresh, file_of(sd))
        return None

    monkeypatch.setattr(cache, "_valid", swap_then_reject)
    assert get(sd) is None
    assert file_of(sd).read_text() == "fresh"


# --- 10: asserts become StateError -----------------------------------------------------------


def test_a_directory_that_vanishes_is_a_state_error_for_audit(
    sd: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(state, "open_dir", lambda *a, **k: None)
    with pytest.raises(StateError) as info:
        audit.append_audit(sd, w.audit_record("a", 1))
    assert info.value.reason == "state_io"


def test_append_log_never_raises_when_the_directory_vanishes(
    sd: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(state, "open_dir", lambda *a, **k: None)
    got: list[str] = []
    warning = audit.append_log(sd, w.log_record("a", 1), warn=got.append)
    assert warning is not None and got == [warning]


def test_a_file_that_vanishes_is_a_state_error_from_the_lock_helper(
    sd: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fd = _dir_fd(sd)
    monkeypatch.setattr(state, "open_file", lambda *a, **k: None)
    try:
        with pytest.raises(StateError) as info, state.locked_append_file(fd, "audit.jsonl"):
            pytest.fail("must not lock")
    finally:
        os.close(fd)
    assert info.value.reason == "state_io"


def test_write_cache_with_a_vanished_directory_is_a_state_error(
    sd: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(state, "open_dir", lambda *a, **k: None)
    with pytest.raises(StateError) as info:
        put(sd)
    assert info.value.reason == "state_io"


def test_write_cache_with_a_vanished_site_directory_is_a_state_error(
    sd: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(cache, "_open_site_dir", lambda *a, **k: None)
    with pytest.raises(StateError) as info:
        put(sd)
    assert info.value.reason == "state_io"


# --- 11: seq edge cases ----------------------------------------------------------------------


def _write_lines(sd: Path, *lines: bytes) -> None:
    (sd / "audit.jsonl").write_bytes(b"".join(x + b"\n" for x in lines))


def test_a_tail_seq_with_thousands_of_digits_does_not_restart_the_sequence(sd: Path) -> None:
    _write_lines(sd, b'{"seq":7}', b'{"seq":' + b"9" * 6000 + b"}")
    assert audit.append_audit(sd, w.audit_record("a", 1))["seq"] == 8


def test_a_seq_above_2_to_the_53_is_ignored_as_garbage(sd: Path) -> None:
    _write_lines(sd, b'{"seq":7}', b'{"seq":%d}' % (2**53 + 1))
    assert audit.append_audit(sd, w.audit_record("a", 1))["seq"] == 8


def test_a_seq_of_exactly_2_to_the_53_is_refused_not_wrapped(sd: Path) -> None:
    _write_lines(sd, b'{"seq":%d}' % 2**53)
    with pytest.raises(StateError):
        audit.append_audit(sd, w.audit_record("a", 1))


# --- 12: relative HOME -----------------------------------------------------------------------


def test_a_relative_home_is_ignored_for_the_passwd_home() -> None:
    expected = Path(pwd.getpwuid(os.geteuid()).pw_dir) / ".local" / "state" / "ticketctl"
    assert state.state_dir_path({"HOME": "relative/home"}) == expected
    assert state.state_dir_path({"HOME": "", "XDG_STATE_HOME": "rel"}) == expected


# --- 13: mutation-sensitive gaps -------------------------------------------------------------


def test_read_cache_on_a_fifo_does_not_hang(sd: Path) -> None:
    put(sd)
    file_of(sd).unlink()
    os.mkfifo(file_of(sd))
    outcome: list[object] = []

    def run() -> None:
        try:
            get(sd)
        except StateError as exc:
            outcome.append(exc.reason)

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    thread.join(10)
    assert not thread.is_alive()
    assert outcome == ["state_type"]


@pytest.mark.parametrize(
    ("skew", "kept"),
    [
        (timedelta(minutes=4), True),
        (timedelta(minutes=5), True),
        (timedelta(minutes=6), False),
        (timedelta(hours=1), False),
    ],
)
def test_future_fetched_at_boundary(sd: Path, skew: timedelta, kept: bool) -> None:
    put(sd, now=NOW + skew)
    assert (get(sd) is not None) is kept
    assert file_of(sd).exists() is kept


def test_the_read_limit_is_8_mib_and_applies_exactly(
    sd: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    assert cache.MAX_CACHE_BYTES == 8 * 1024 * 1024
    put(sd)
    size = file_of(sd).stat().st_size
    monkeypatch.setattr(cache, "MAX_CACHE_BYTES", size)
    assert get(sd) is not None
    monkeypatch.setattr(cache, "MAX_CACHE_BYTES", size - 1)
    assert get(sd) is None
    assert not file_of(sd).exists()


def test_write_cache_refuses_over_the_limit_and_writes_nothing(
    sd: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    put(sd)
    size = file_of(sd).stat().st_size
    file_of(sd).unlink()
    monkeypatch.setattr(cache, "MAX_CACHE_BYTES", size - 1)
    with pytest.raises(StateError) as info:
        put(sd)
    assert info.value.reason == "record_too_large"
    assert not file_of(sd).exists()
    monkeypatch.setattr(cache, "MAX_CACHE_BYTES", size)
    put(sd)
    assert file_of(sd).exists()


def test_sweep_removes_only_old_regular_temp_files(sd: Path) -> None:
    put(sd)
    d = file_of(sd).parent
    past = NOW.timestamp() - 7200
    old_plain = d / "old-not-temp"
    old_plain.write_text("x")
    young = d / ".tmp-young"
    young.write_text("x")
    old_dir = d / ".tmp-dir"
    old_dir.mkdir()
    old_fifo = d / ".tmp-fifo"
    os.mkfifo(old_fifo)
    old_link = d / ".tmp-link"
    old_link.symlink_to(old_plain)
    old_temp = d / ".tmp-old"
    old_temp.write_text("x")
    for p in (old_plain, old_dir, old_fifo, old_temp):
        os.utime(p, (past, past))
    os.utime(old_link, (past, past), follow_symlinks=False)
    put(sd, now=datetime.now(UTC))
    assert not old_temp.exists()
    for p in (old_plain, young, old_dir, old_fifo):
        assert p.exists(), p.name
    assert old_link.is_symlink()


def test_the_temp_file_is_removed_when_the_failure_is_a_state_error(
    sd: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    put(sd)

    def boom(*a: object, **k: object) -> None:
        raise StateError("state_io", "boom")

    monkeypatch.setattr(state, "write_all", boom)
    with pytest.raises(StateError):
        put(sd, {"fields": ["new"]})
    monkeypatch.undo()
    assert [p.name for p in file_of(sd).parent.iterdir()] == ["fields.json"]


def test_a_symlinked_target_removes_the_temp_file_too(sd: Path, tmp_path: Path) -> None:
    put(sd)
    file_of(sd).unlink()
    victim = tmp_path / "victim"
    victim.write_text("keep")
    file_of(sd).symlink_to(victim)
    with pytest.raises(StateError) as info:
        put(sd)
    assert info.value.reason == "state_symlink"
    assert victim.read_text() == "keep"
    assert [p.name for p in file_of(sd).parent.iterdir()] == ["fields.json"]


def test_the_temp_file_does_not_follow_a_planted_symlink(
    sd: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    put(sd)
    monkeypatch.setattr(cache.secrets, "token_hex", lambda n: "f" * (2 * n))
    victim = tmp_path / "victim"
    victim.write_text("keep")
    (file_of(sd).parent / f".tmp-{os.getpid()}-{'f' * 16}").symlink_to(victim)
    with pytest.raises(StateError) as info:
        put(sd, {"fields": ["new"]})
    assert info.value.reason == "state_io"
    assert victim.read_text() == "keep"


def test_append_log_returns_a_warning_on_an_os_error_and_never_raises(
    sd: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def boom(*a: object, **k: object) -> None:
        raise OSError(5, "io")

    monkeypatch.setattr(audit, "_append_log_line", boom)
    got: list[str] = []
    warning = audit.append_log(sd, w.log_record("a", 1), warn=got.append)
    assert warning is not None and got == [warning]


def test_append_log_survives_an_unexpected_exception(
    sd: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def boom(*a: object, **k: object) -> None:
        raise RuntimeError("unexpected")

    monkeypatch.setattr(audit, "_append_log_line", boom)
    got: list[str] = []
    warning = audit.append_log(sd, w.log_record("a", 1), warn=got.append)
    assert warning is not None and got == [warning]


def test_the_default_lock_timeout_is_10_seconds() -> None:
    assert state.LOCK_TIMEOUT == 10.0
    for fn in (state.locked_append_file, audit.append_audit, audit.append_log):
        assert (
            inspect.signature(fn)
            .parameters["timeout" if fn is state.locked_append_file else "lock_timeout"]
            .default
            == 10.0
        )


def test_the_email_hash_is_not_lowercased_but_is_stripped() -> None:
    assert cache.email_sha256("A@x.test") != cache.email_sha256("a@x.test")
    assert cache.email_sha256(" a@x.test ") == cache.email_sha256("a@x.test")


# --- 15: the 64 KiB limit --------------------------------------------------------------------


def test_the_largest_legal_audit_record_exceeds_64_kib_and_is_refused(sd: Path) -> None:
    assert audit.MAX_RECORD_BYTES == 64 * 1024
    rec = w.audit_record("a", 1)
    rec["inputs"]["config"]["files_sha256"] = ["b" * 64] * 1200
    with pytest.raises(StateError) as info:
        audit.append_audit(sd, rec)
    assert info.value.reason == "record_too_large"
    assert not (sd / "audit.jsonl").exists() or not (sd / "audit.jsonl").read_bytes()


def test_a_log_record_over_the_limit_is_refused(sd: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(audit, "MAX_RECORD_BYTES", 50)
    got: list[str] = []
    warning = audit.append_log(sd, w.log_record("a", 1), warn=got.append)
    assert warning is not None and "record_too_large" in warning
    assert not (sd / "log.jsonl").exists() or not (sd / "log.jsonl").read_bytes()
