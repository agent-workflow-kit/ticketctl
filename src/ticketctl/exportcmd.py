"""``export``: print the records of ``audit.jsonl`` as JSON lines (docs/interface.md, export).

Records go to stdout, or with ``--to PATH`` to a new file and nothing to stdout. Filters:
``--since`` (the one time grammar; a bare date is 00:00 UTC; a record is kept when its ``ts`` is at
or after the cut-off) and ``--after-seq N`` (``seq`` greater than N); both must hold. Records are
printed in file order, which is ``seq`` order for a file written by ``audit.append_audit``.

Reading. The file is opened through the state directory rules (``state.open_file``: no symlink, a
regular file, owned by the caller) and read **without** the flock, up to the size seen right after
the open: a record appended later is not part of this run (the next run, with ``--after-seq``,
resumes after the last record seen). Lines are read with a bound of ``MAX_RECORD_BYTES`` (the
writer's own limit), so a huge garbage line or a file of garbage costs a bounded buffer. A line is
skipped, and counted for one warning, when it

- is longer than the bound, is not UTF-8, not JSON (``NaN`` included) or nested too deeply;
- is not a JSON object whose ``v`` is the integer 1;
- has no ``seq`` that is an integer from 1 to 2**53 (the writer's seq-scan bound);
- cannot be cleaned (more than ``state.MAX_NODES`` nodes or deeper than ``state.MAX_DEPTH``).

A last line with no newline (a torn tail, or a record whose write is still in flight when the size
was read) is neither printed nor counted: the writer repairs it with a newline before its next
record, after which it is an ordinary garbage line. Other fields are not validated, so a record
written by a later minor version passes. An absent
``audit.jsonl`` (or state directory) is an empty export and nothing is created, ``--to`` apart.

Output. Every string of a record (keys too) is sanitized and scrubbed again (``state.clean_tree``)
and the JSON is written with ``ensure_ascii``. The scrub knows the token values of the configured
sites when the configuration loads; a broken configuration does not stop an export (the records were
scrubbed when they were written).

``--to``. The parent directory is opened (a symlinked ancestor is fine) and must be a directory
owned by the caller; the file is created relative to that descriptor with
``O_WRONLY|O_CREAT|O_EXCL|O_NOFOLLOW`` and mode 0600, so an existing file, a symlink (dangling or
not) or a FIFO at the name exits 20 ``already_exists`` and nothing is created or followed. If the
write fails after the file was created, the file is removed again (if the name still is it).
"""

from __future__ import annotations

import argparse
import errno
import json
import os
import stat
import sys
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from . import audit, output, state
from .commentscmd import since_cutoff
from .errors import TicketctlError
from .flags import Globals
from .runtime import load_runtime
from .sanitize import add_secrets, basic_auth_value
from .state import StateError

LINE_LIMIT = audit.MAX_RECORD_BYTES
CHUNK_BYTES = 64 * 1024


@dataclass
class Skips:
    count: int = 0
    first_seq: list[int] = field(default_factory=list)


def _euid() -> int:
    return os.geteuid()


def _invalid(message: str) -> TicketctlError:
    return TicketctlError("invalid_request", message, reason="invalid_value")


def _lines(fd: int, limit: int) -> Iterator[bytes | None]:
    """The newline-terminated lines of the first ``limit`` bytes; None stands for a bad line."""
    with os.fdopen(os.dup(fd), "rb") as fh:
        fh.seek(0)
        remaining = limit
        while remaining > 0:
            piece = fh.readline(min(LINE_LIMIT + 1, remaining))
            if not piece:
                return
            remaining -= len(piece)
            if piece.endswith(b"\n") and len(piece) <= LINE_LIMIT:
                yield piece
                continue
            terminated = piece.endswith(b"\n")
            while not terminated and len(piece) > LINE_LIMIT and remaining > 0:
                rest = fh.readline(min(LINE_LIMIT, remaining))  # drop the rest of a long line
                if not rest:
                    break
                remaining -= len(rest)
                terminated = rest.endswith(b"\n")
            if not terminated and len(piece) <= LINE_LIMIT:
                return  # a torn tail or a write in flight: not read, not a warning
            yield None  # over-long


def _record(line: bytes) -> dict[str, Any] | None:
    try:
        doc = json.loads(line.decode("utf-8"))
    except (ValueError, RecursionError):  # UnicodeDecodeError is a ValueError
        return None
    if not isinstance(doc, dict) or type(doc.get("v")) is not int or doc["v"] != 1:
        return None
    seq = doc.get("seq")
    if not audit._is_int(seq, 1, audit.MAX_SEQ):
        return None
    try:
        cleaned = state.clean_tree(doc)
    except StateError:
        return None
    assert isinstance(cleaned, dict)
    try:
        # a NaN or an infinity does not parse as strict JSON; clean_tree refused those already
        json.dumps(cleaned, ensure_ascii=True, allow_nan=False)
    except (ValueError, TypeError, RecursionError):
        return None
    return cleaned


def read_records(
    state_dir: Path,
    *,
    since: str | None = None,
    after_seq: int | None = None,
    skips: Skips | None = None,
) -> Iterator[dict[str, Any]]:
    """The cleaned records that pass both filters, in file order; see the module docstring."""
    skips = skips if skips is not None else Skips()
    dir_fd = state.open_dir(state_dir, create=False)
    if dir_fd is None:
        return
    try:
        fd = state.open_file(dir_fd, audit.AUDIT_FILE, create=False, read_only=True)
    finally:
        os.close(dir_fd)
    if fd is None:
        return
    try:
        size = os.fstat(fd).st_size
        for line in _lines(fd, size):
            rec = _record(line) if line is not None else None
            if rec is None:
                skips.count += 1
                continue
            if after_seq is not None and rec["seq"] <= after_seq:
                continue
            if since is not None:
                ts = rec.get("ts")
                if not isinstance(ts, str) or not audit.TS.fullmatch(ts) or ts < since:
                    continue
            yield rec
    finally:
        os.close(fd)


def _encode(rec: dict[str, Any]) -> str:
    return json.dumps(rec, ensure_ascii=True, separators=(",", ":"), allow_nan=False)


def _chunks(records: Iterator[dict[str, Any]]) -> Iterator[str]:
    """JSON lines joined into chunks of about ``CHUNK_BYTES`` (each ends with a newline)."""
    parts: list[str] = []
    size = 0
    for rec in records:
        text = _encode(rec)
        parts.append(text)
        size += len(text) + 1
        if size >= CHUNK_BYTES:
            yield "\n".join(parts) + "\n"
            parts, size = [], 0
    if parts:
        yield "\n".join(parts) + "\n"


def _create_target(path: str) -> tuple[int, int, str]:
    """Create the ``--to`` file; returns (file fd, parent fd, name); the caller closes both fds."""
    if not path or "\0" in path:
        raise _invalid("--to is not a usable path")
    parent, name = os.path.split(path)
    if name in ("", ".", ".."):
        raise _invalid("--to must name a file")
    parent = parent or "."
    try:
        pfd = os.open(parent, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
    except OSError:
        raise TicketctlError(
            "invalid_request", "the directory of --to cannot be opened", reason="invalid_file"
        ) from None
    try:
        if os.fstat(pfd).st_uid != _euid():
            raise TicketctlError(
                "invalid_request",
                "the directory of --to is not owned by the current user",
                reason="invalid_file",
            )
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC
        try:
            fd = os.open(name, flags, 0o600, dir_fd=pfd)
        except FileExistsError:
            raise TicketctlError(
                "already_exists", "--to names a file that already exists"
            ) from None
        except OSError as exc:
            if exc.errno == errno.ELOOP:  # not reachable with O_EXCL, kept for other kernels
                raise TicketctlError(
                    "already_exists", "--to names a file that already exists"
                ) from None
            raise TicketctlError(
                "invalid_request", "--to cannot be created", reason="invalid_file"
            ) from None
    except BaseException:
        os.close(pfd)
        raise
    return fd, pfd, name


def _remove_if_ours(pfd: int, name: str, fd: int) -> None:
    try:
        mine = os.fstat(fd)
        now = os.stat(name, dir_fd=pfd, follow_symlinks=False)
        if stat.S_ISREG(now.st_mode) and (now.st_ino, now.st_dev) == (mine.st_ino, mine.st_dev):
            os.unlink(name, dir_fd=pfd)
    except OSError:
        pass


def _write_file(fd: int, pfd: int, name: str, chunks: Iterator[str]) -> None:
    try:
        for chunk in chunks:
            state.write_all(fd, chunk.encode("ascii"))
    except BaseException:
        _remove_if_ours(pfd, name, fd)
        raise
    finally:
        os.close(fd)
        os.close(pfd)


def _arm_scrub(g: Globals) -> None:
    """Register the configured token values with the output scrub, when the config loads."""
    try:
        rt = load_runtime(g, show_warnings=False)
        for site in rt.config.sites.values():
            token = rt.env.get(site.token_env, "")
            email = site.email if site.email is not None else rt.env.get(site.email_env or "", "")
            basic = [basic_auth_value(email, token)] if token.strip() and email.strip() else []
            add_secrets([token], basic_values=basic)
    except Exception:  # noqa: BLE001 - the records were scrubbed when written; export is offline
        return


def _since(value: str | None, now: datetime | None = None) -> str | None:
    return None if value is None else since_cutoff(value, now)


def export(g: Globals, a: argparse.Namespace) -> None:
    """Handler of ``export``; prints records itself and returns None."""
    since = _since(a.since)
    state_dir = state.state_dir_path(os.environ)
    target = _create_target(a.to) if a.to is not None else None
    _arm_scrub(g)
    skips = Skips()
    chunks = _chunks(read_records(state_dir, since=since, after_seq=a.after_seq, skips=skips))
    if target is not None:
        _write_file(*target, chunks)
    else:
        for chunk in chunks:
            output.write(sys.stdout, chunk.rstrip("\n"))
    if skips.count and not g.quiet:
        output.write(
            sys.stderr,
            f"warning: {skips.count} line(s) of {audit.AUDIT_FILE} are not audit records and "
            "were skipped",
        )
