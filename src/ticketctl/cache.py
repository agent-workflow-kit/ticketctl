"""The cache store: ``cache/<site>/{fields,me,priorities}.json`` (docs/interface.md "Files").

Public API:

``write_cache(state_dir, site, name, data, *, url, email, ttl=None, now=None)``
    Replace the file atomically (a ``0600`` temp file created ``O_EXCL|O_NOFOLLOW`` in the same
    directory, ``fsync``, ``rename``). The header ``{schema_version, site, url, email_sha256,
    fetched_at}`` is added; ``data`` must not use those keys. Every string is sanitized and
    scrubbed first, so no credential or control character is stored. ``ttl`` of zero (the
    ``cache_ttl: 0`` setting) writes nothing.

``read_cache(state_dir, site, name, *, url, email, ttl, now=None) -> dict | None``
    The stored document, or None (and the file is deleted) when it is corrupt, over-long, of an
    unknown ``schema_version``, for another site, url or email hash, older than ``ttl`` or
    fetched in the future. A damaged cache never raises. A symlink, a non-regular file or a
    foreign owner is a :class:`StateError`, not a silent discard. ``ttl`` of zero always misses.

Readers never see a partial file: replacement is a rename.
"""

from __future__ import annotations

import hashlib
import json
import os
import secrets
import stat
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from ticketctl import state
from ticketctl.audit import TS
from ticketctl.sanitize import GRAMMARS
from ticketctl.state import StateError

SCHEMA_VERSION = 1
NAMES = ("fields", "me", "priorities")
HEADER = ("schema_version", "site", "url", "email_sha256", "fetched_at")
MAX_CACHE_BYTES = 8 * 1024 * 1024
_CLOCK_SKEW = timedelta(minutes=5)
_ORPHAN_AGE = 3600.0
_TEMP_PREFIX = ".tmp-"


def email_sha256(email: str | None) -> str:
    return hashlib.sha256((email or "").strip().encode("utf-8")).hexdigest()


def _check(site: str, name: str) -> str:
    GRAMMARS["site_name"].check(site)
    if name not in NAMES:
        raise StateError("state_type", "unknown cache file")
    return f"{name}.json"


def _open_site_dir(state_dir_fd: int, site: str, *, create: bool) -> int | None:
    cache_fd = state.open_subdir(state_dir_fd, "cache", create=create)
    if cache_fd is None:
        return None
    try:
        return state.open_subdir(cache_fd, site, create=create)
    finally:
        os.close(cache_fd)


def _stamp(now: datetime | None) -> datetime:
    return (now or datetime.now(UTC)).astimezone(UTC).replace(microsecond=0)


def _discard(site_fd: int, filename: str) -> None:
    try:
        os.unlink(filename, dir_fd=site_fd)
    except OSError:
        pass


def _valid(doc: Any, site: str, url: str, email_hash: str) -> datetime | None:
    if not isinstance(doc, dict):
        return None
    version = doc.get("schema_version")
    if type(version) is not int or version != SCHEMA_VERSION:
        return None
    if doc.get("site") != site or doc.get("url") != url or doc.get("email_sha256") != email_hash:
        return None
    stamp = doc.get("fetched_at")
    if not isinstance(stamp, str) or not TS.fullmatch(stamp):
        return None
    try:
        return datetime.strptime(stamp, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC)
    except ValueError:
        return None


def read_cache(
    state_dir: Path,
    site: str,
    name: str,
    *,
    url: str,
    email: str | None,
    ttl: timedelta,
    now: datetime | None = None,
) -> dict[str, Any] | None:
    filename = _check(site, name)
    if ttl <= timedelta(0):
        return None
    root = state.open_dir(state_dir, create=False)
    if root is None:
        return None
    try:
        site_fd = _open_site_dir(root, site, create=False)
    finally:
        os.close(root)
    if site_fd is None:
        return None
    try:
        fd = state.open_file(site_fd, filename, create=False, read_only=True)
        if fd is None:
            return None
        try:
            size = os.fstat(fd).st_size
            raw = os.read(fd, MAX_CACHE_BYTES + 1) if size <= MAX_CACHE_BYTES else b""
        except OSError as exc:
            raise StateError("state_io", "cannot read the cache file") from exc
        finally:
            os.close(fd)
        fetched = None
        doc: Any = None
        if 0 < len(raw) <= MAX_CACHE_BYTES:
            try:
                doc = json.loads(raw)
            except (ValueError, RecursionError):
                pass
            fetched = _valid(doc, site, url, email_sha256(email))
        current = _stamp(now)
        if fetched is None or fetched > current + _CLOCK_SKEW or current - fetched > ttl:
            _discard(site_fd, filename)
            return None
        return doc
    finally:
        os.close(site_fd)


def _sweep_orphans(site_fd: int) -> None:
    """Delete temp files an interrupted writer left behind (older than an hour)."""
    cutoff = time.time() - _ORPHAN_AGE
    try:
        names = [n for n in os.listdir(site_fd) if n.startswith(_TEMP_PREFIX)]
    except OSError:
        return
    for entry in names:
        try:
            info = os.stat(entry, dir_fd=site_fd, follow_symlinks=False)
            if stat.S_ISREG(info.st_mode) and info.st_mtime < cutoff:
                os.unlink(entry, dir_fd=site_fd)
        except OSError:
            pass


def write_cache(
    state_dir: Path,
    site: str,
    name: str,
    data: dict[str, Any],
    *,
    url: str,
    email: str | None,
    ttl: timedelta | None = None,
    now: datetime | None = None,
) -> None:
    filename = _check(site, name)
    if ttl is not None and ttl <= timedelta(0):
        return
    if set(data) & set(HEADER):
        raise StateError("record_invalid", "cache data uses a reserved header key")
    document = {
        "schema_version": SCHEMA_VERSION,
        "site": site,
        "url": url,
        "email_sha256": email_sha256(email),
        "fetched_at": _stamp(now).strftime("%Y-%m-%dT%H:%M:%SZ"),
        **data,
    }
    cleaned = state.clean_tree(document, max_nodes=500_000)
    try:
        payload = json.dumps(cleaned, ensure_ascii=True, separators=(",", ":"), allow_nan=False)
    except (ValueError, TypeError, RecursionError) as exc:
        raise StateError("record_invalid", "cache data cannot be encoded as JSON") from exc
    body = payload.encode("ascii")
    if len(body) > MAX_CACHE_BYTES:
        raise StateError("record_too_large", "cache data is too large")
    root = state.open_dir(state_dir)
    assert root is not None
    try:
        site_fd = _open_site_dir(root, site, create=True)
    finally:
        os.close(root)
    assert site_fd is not None
    try:
        _sweep_orphans(site_fd)
        _replace(site_fd, filename, body)
    finally:
        os.close(site_fd)


def _replace(site_fd: int, filename: str, body: bytes) -> None:
    temp = f"{_TEMP_PREFIX}{os.getpid()}-{secrets.token_hex(8)}"
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC
    try:
        fd = os.open(temp, flags, 0o600, dir_fd=site_fd)
    except OSError as exc:
        raise StateError("state_io", "cannot create a cache temp file") from exc
    try:
        try:
            state.write_all(fd, body)
            os.fsync(fd)
        finally:
            os.close(fd)
        existing = None
        try:
            existing = os.stat(filename, dir_fd=site_fd, follow_symlinks=False)
        except FileNotFoundError:
            pass
        if existing is not None and stat.S_ISLNK(existing.st_mode):
            raise StateError("state_symlink", "state file is a symlink")
        os.replace(temp, filename, src_dir_fd=site_fd, dst_dir_fd=site_fd)
        os.fsync(site_fd)
    except OSError as exc:
        _discard(site_fd, temp)
        raise StateError("state_io", "cannot replace the cache file") from exc
    except BaseException:
        _discard(site_fd, temp)
        raise
