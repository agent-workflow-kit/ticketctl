"""Cache store: atomic replace, discard on corruption, mismatch, age or unknown version."""

from __future__ import annotations

import hashlib
import json
import os
import stat
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from ticketctl import cache, sanitize, state
from ticketctl.state import StateError

SENTINEL = "SENTINEL-s3cr3t-value-9f2c"
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


def test_round_trip_and_header(sd: Path) -> None:
    put(sd, {"fields": [{"id": "f1", "name": "Summary"}]})
    got = get(sd)
    assert got is not None and got["fields"] == [{"id": "f1", "name": "Summary"}]
    raw = json.loads(file_of(sd).read_text())
    assert raw["schema_version"] == 1 and raw["site"] == "main" and raw["url"] == URL
    assert raw["email_sha256"] == hashlib.sha256(EMAIL.encode()).hexdigest()
    assert raw["fetched_at"] == "2026-05-01T12:00:00Z"
    assert EMAIL not in file_of(sd).read_text()


def test_modes(sd: Path) -> None:
    put(sd)
    assert stat.S_IMODE(file_of(sd).stat().st_mode) == 0o600
    assert stat.S_IMODE((sd / "cache").stat().st_mode) == 0o700
    assert stat.S_IMODE((sd / "cache" / "main").stat().st_mode) == 0o700


def test_missing_returns_none_and_creates_nothing(sd: Path) -> None:
    assert get(sd) is None
    assert not (sd / "cache").exists()


def test_me_and_priorities_names_and_refused_names(sd: Path) -> None:
    cache.write_cache(sd, "main", "me", {"time_zone": "UTC"}, url=URL, email=EMAIL, now=NOW)
    cache.write_cache(sd, "main", "priorities", {"priorities": []}, url=URL, email=EMAIL, now=NOW)
    assert cache.read_cache(sd, "main", "me", url=URL, email=EMAIL, ttl=TTL, now=NOW)
    for bad in ("../x", "fields.json", "other", ""):
        with pytest.raises(StateError):
            cache.write_cache(sd, "main", bad, {}, url=URL, email=EMAIL, now=NOW)
    for site in ("../x", "a/b", "", "bad name", ".."):
        with pytest.raises(Exception):  # noqa: B017 (InputError from the site grammar)
            cache.write_cache(sd, site, "fields", {}, url=URL, email=EMAIL, now=NOW)
    assert sorted(p.name for p in (sd / "cache").iterdir()) == ["main"]


@pytest.mark.parametrize(
    "kw",
    [{"url": "https://other.test"}, {"email": "you@example.test"}, {"url": URL + "/"}],
)
def test_a_different_url_or_email_hash_is_discarded(sd: Path, kw: dict) -> None:
    put(sd)
    assert get(sd, **kw) is None
    assert not file_of(sd).exists()


def test_a_different_site_inside_the_file_is_discarded(sd: Path) -> None:
    put(sd)
    path = file_of(sd)
    doc = json.loads(path.read_text())
    doc["site"] = "other"
    path.write_text(json.dumps(doc))
    assert get(sd) is None
    assert not path.exists()


def test_age_beyond_ttl_is_discarded_and_exact_ttl_is_kept(sd: Path) -> None:
    put(sd)
    assert get(sd, now=NOW + TTL) is not None
    assert get(sd, now=NOW + TTL + timedelta(seconds=1)) is None
    assert not file_of(sd).exists()


def test_future_fetched_at_is_discarded(sd: Path) -> None:
    put(sd, now=NOW + timedelta(days=2))
    assert get(sd) is None


def test_zero_ttl_disables_the_cache(sd: Path) -> None:
    put(sd)
    assert get(sd, ttl=timedelta(0)) is None
    cache.write_cache(sd, "main", "me", {"x": 1}, url=URL, email=EMAIL, now=NOW, ttl=timedelta(0))
    assert not (sd / "cache" / "main" / "me.json").exists()


def test_unknown_schema_version_is_discarded(sd: Path) -> None:
    put(sd)
    path = file_of(sd)
    doc = json.loads(path.read_text())
    for version in (2, 0, "1", True, None, 1.0):
        path.write_text(json.dumps(doc | {"schema_version": version}))
        assert get(sd) is None, version
        assert not path.exists()


@pytest.mark.parametrize(
    "content",
    [
        b"",
        b"{",
        b"[]",
        b"null",
        b'"str"',
        b"\xff\xfe\x00bad",
        b'{"schema_version":1}',
        b"[" * 100000,
        b"{" * 50000,
        b'{"schema_version":1,"site":"main","url":"x","email_sha256":1,"fetched_at":"nope"}',
        b"x" * (20 * 1024 * 1024),
    ],
)
def test_corrupt_files_are_discarded_never_a_crash(sd: Path, content: bytes) -> None:
    put(sd)
    path = file_of(sd)
    path.write_bytes(content)
    assert get(sd) is None
    assert not path.exists()


def test_a_bad_fetched_at_value_is_discarded(sd: Path) -> None:
    put(sd)
    path = file_of(sd)
    doc = json.loads(path.read_text())
    for bad in ("2026-05-01T12:00:00+00:00", "2026-13-01T12:00:00Z", 5, None, "2026-05-01"):
        path.write_text(json.dumps(doc | {"fetched_at": bad}))
        assert get(sd) is None, bad


def test_a_symlinked_cache_file_is_refused_and_target_untouched(sd: Path, tmp_path: Path) -> None:
    put(sd)
    victim = tmp_path / "victim.json"
    victim.write_text("keep")
    file_of(sd).unlink()
    file_of(sd).symlink_to(victim)
    with pytest.raises(StateError) as info:
        get(sd)
    assert info.value.reason == "state_symlink"
    with pytest.raises(StateError):
        put(sd)
    assert victim.read_text() == "keep"


def test_a_symlinked_cache_directory_is_refused(sd: Path, tmp_path: Path) -> None:
    (tmp_path / "elsewhere").mkdir()
    (sd / "cache").symlink_to(tmp_path / "elsewhere")
    with pytest.raises(StateError):
        put(sd)
    with pytest.raises(StateError):
        get(sd)
    assert list((tmp_path / "elsewhere").iterdir()) == []


def test_a_foreign_owned_cache_file_is_refused(sd: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    put(sd)
    monkeypatch.setattr(state, "_euid", lambda: os.geteuid() + 1)
    with pytest.raises(StateError):
        get(sd)


def test_replace_is_atomic_and_leaves_no_temp_files(sd: Path) -> None:
    for i in range(5):
        put(sd, {"fields": [i]})
    assert [p.name for p in file_of(sd).parent.iterdir()] == ["fields.json"]
    assert get(sd) == get(sd)
    got = get(sd)
    assert got is not None and got["fields"] == [4]


def test_a_failed_write_keeps_the_old_file_and_removes_the_temp(
    sd: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    put(sd, {"fields": ["old"]})

    def boom(*a: object, **k: object) -> None:
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(os, "replace", boom)
    with pytest.raises(StateError) as info:
        put(sd, {"fields": ["new"]})
    assert info.value.reason == "state_io"
    monkeypatch.undo()
    got = get(sd)
    assert got is not None and got["fields"] == ["old"]
    assert [p.name for p in file_of(sd).parent.iterdir()] == ["fields.json"]


def test_old_orphan_temp_files_are_swept_young_ones_kept(sd: Path) -> None:
    put(sd)
    d = file_of(sd).parent
    old, young = d / ".tmp-old", d / ".tmp-young"
    for p in (old, young):
        p.write_text("x")
    past = NOW.timestamp() - 7200
    os.utime(old, (past, past))
    put(sd, now=datetime.now(UTC))
    assert not old.exists() and young.exists()


def test_data_cannot_override_the_header(sd: Path) -> None:
    for key in ("schema_version", "site", "url", "email_sha256", "fetched_at"):
        with pytest.raises(StateError):
            put(sd, {key: "x"})


def test_ansi_and_secret_never_reach_the_file(sd: Path) -> None:
    sanitize.configure_scrubber([SENTINEL])
    put(sd, {"fields": [{"name": f"\x1b[31m{SENTINEL}‮"}]})
    raw = file_of(sd).read_bytes()
    assert SENTINEL.encode() not in raw and b"\x1b" not in raw
    got = get(sd)
    assert got is not None
    assert got["fields"][0]["name"] == "<U+001B>[31m<redacted><U+202E>"


def test_unserializable_data_is_refused(sd: Path) -> None:
    for bad in ({"a": float("nan")}, {"a": object()}, {"a": {1, 2}}):
        with pytest.raises(StateError):
            put(sd, bad)
    assert not file_of(sd).exists()


def test_email_hash_helper() -> None:
    assert cache.email_sha256(EMAIL) == hashlib.sha256(EMAIL.encode()).hexdigest()
    assert cache.email_sha256(None) == hashlib.sha256(b"").hexdigest()


def test_discard_cache_removes_one_file_and_tolerates_a_missing_one(sd: Path) -> None:
    cache.discard_cache(sd, "main", "fields")  # no state, no cache directory: fine
    put(sd)
    cache.write_cache(sd, "main", "me", {"time_zone": "UTC"}, url=URL, email=EMAIL, now=NOW)
    cache.discard_cache(sd, "main", "fields")
    assert not file_of(sd).exists()
    assert (sd / "cache" / "main" / "me.json").exists()
    cache.discard_cache(sd, "main", "fields")
    with pytest.raises(StateError):
        cache.discard_cache(sd, "main", "../x")


def test_resolutions_is_a_cache_name(sd: Path) -> None:
    cache.write_cache(
        sd, "main", "resolutions", {"resolutions": ["Done"]}, url=URL, email=EMAIL, now=NOW
    )
    got = cache.read_cache(sd, "main", "resolutions", url=URL, email=EMAIL, ttl=TTL, now=NOW)
    assert got is not None and got["resolutions"] == ["Done"]
