"""The state directory: location, safe opening, the flock helper and the tree cleaner.

Everything ticketctl keeps on disk lives under ``$XDG_STATE_HOME/ticketctl`` (default
``~/.local/state/ticketctl``). The rules are in docs/interface.md under "Files".

Safety rules, applied on every open (nothing is cached between calls):

- The directory is opened with ``O_NOFOLLOW|O_DIRECTORY`` (a symlinked state directory is
  refused; symlinked *ancestors* such as a linked home are fine), must be owned by the
  effective uid, and is set to exactly ``0700``. Subdirectories and files are opened relative to
  the directory's file descriptor, so a path swapped in later cannot redirect them.
- Files are opened with ``O_NOFOLLOW`` (and ``O_NONBLOCK``, so a FIFO planted in place cannot hang
  the process), must be regular, owned by the effective uid, and are set to exactly ``0600``.
- Every refusal is a :class:`StateError`: exit 1, code ``internal``.

Locking: ``locked_append_file`` takes ``flock(LOCK_EX)`` on the data file itself, polling with
``LOCK_NB`` up to a timeout (a stuck holder cannot hang the CLI). After the lock is held it checks
that the name still refers to the locked inode; a file that was rotated away meanwhile makes the
waiter reopen the current one. Lock ordering: no code path holds two state-file locks at once
(audit and log are independent), so there is no lock cycle. The kernel releases a flock when its
holder dies, so a killed process never leaves a stale lock.

Public API:
    state_dir_path(env), open_dir(path, create=True), open_subdir(fd, name, create=True)
    locked_append_file(dir_fd, name, timeout=...)   context manager yielding the locked fd
    write_all(fd, data), clean_tree(value)
    StateError
"""

from __future__ import annotations

import errno
import fcntl
import os
import pwd
import stat
import time
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from pathlib import Path

from ticketctl.sanitize import render

LOCK_TIMEOUT = 10.0
_REOPEN_LIMIT = 50
_POLL = 0.005
_POLL_MAX = 0.05


class StateError(Exception):
    """A state directory or file problem: exit 1, code ``internal``.

    ``errors.from_exception`` maps this onto the error envelope (exit 1 `internal`).
    ``reason`` is one of ``state_owner``, ``state_symlink``, ``state_type``, ``state_io``,
    ``lock_timeout``, ``record_invalid`` or ``record_too_large``. The message never holds a
    record value.
    """

    exit_code = 1
    code = "internal"

    def __init__(self, reason: str, message: str) -> None:
        super().__init__(message)
        self.reason = reason
        self.message = message


def _euid() -> int:
    return os.geteuid()


def _home(env: Mapping[str, str]) -> str:
    home = env.get("HOME")
    if home and os.path.isabs(home):
        return home
    try:
        return pwd.getpwuid(os.geteuid()).pw_dir
    except KeyError:
        return "/nonexistent"


def state_dir_path(env: Mapping[str, str] | None = None) -> Path:
    """``$XDG_STATE_HOME/ticketctl`` (absolute values only) or ``~/.local/state/ticketctl``."""
    env = os.environ if env is None else env
    xdg = env.get("XDG_STATE_HOME", "")
    if xdg and os.path.isabs(xdg):
        return Path(xdg) / "ticketctl"
    return Path(_home(env)) / ".local" / "state" / "ticketctl"


def _io_error(what: str, exc: OSError) -> StateError:
    return StateError(
        "state_io", f"cannot use state {what}: {errno.errorcode.get(exc.errno or 0, 'error')}"
    )


def _check_dir(fd: int, what: str) -> None:
    info = os.fstat(fd)
    if not stat.S_ISDIR(info.st_mode):
        raise StateError("state_type", f"state {what} is not a directory")
    if info.st_uid != _euid():
        raise StateError("state_owner", f"state {what} is not owned by the current user")
    if stat.S_IMODE(info.st_mode) != 0o700:
        os.fchmod(fd, 0o700)


def _open_dir_at(name: str | Path, dir_fd: int | None, what: str) -> int:
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
    try:
        fd = os.open(name, flags, dir_fd=dir_fd)
    except OSError as exc:
        if exc.errno in (errno.ELOOP, errno.ENOTDIR):
            try:
                is_link = stat.S_ISLNK(os.stat(name, dir_fd=dir_fd, follow_symlinks=False).st_mode)
            except OSError:
                is_link = False
            if is_link:
                raise StateError("state_symlink", f"state {what} is a symlink") from exc
            raise StateError("state_type", f"state {what} is not a directory") from exc
        raise
    try:
        _check_dir(fd, what)
    except BaseException:
        os.close(fd)
        raise
    return fd


def open_dir(path: Path, *, create: bool = True) -> int | None:
    """Open (and with ``create`` make) the state directory; the caller closes the fd.

    Returns None when it is missing and ``create`` is false.
    """
    try:
        parent = path.parent
        if create:
            os.makedirs(parent, exist_ok=True)
        try:
            pfd = os.open(parent, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
        except FileNotFoundError:
            if create:
                raise
            return None
        try:
            if create:
                try:
                    os.mkdir(path.name, 0o700, dir_fd=pfd)
                except FileExistsError:
                    pass
            try:
                return _open_dir_at(path.name, pfd, "directory")
            except FileNotFoundError:
                return None
        finally:
            os.close(pfd)
    except OSError as exc:
        raise _io_error("directory", exc) from exc


def open_subdir(dir_fd: int, name: str, *, create: bool = True) -> int | None:
    """Open a ``0700`` subdirectory relative to ``dir_fd``; None when missing and not created."""
    _check_name(name)
    try:
        if create:
            try:
                os.mkdir(name, 0o700, dir_fd=dir_fd)
            except FileExistsError:
                pass
        try:
            return _open_dir_at(name, dir_fd, "subdirectory")
        except FileNotFoundError:
            return None
    except OSError as exc:
        raise _io_error("subdirectory", exc) from exc


def _check_name(name: str) -> None:
    if not name or "/" in name or "\0" in name or name in (".", ".."):
        raise StateError("state_type", "invalid state file name")


def _open_at(name: str, flags: int, dir_fd: int) -> int:
    """``os.open``; on EACCES a regular file the user owns (mode 000, say) is tightened first."""
    try:
        return os.open(name, flags, 0o600, dir_fd=dir_fd)
    except PermissionError as first:
        try:
            info = os.stat(name, dir_fd=dir_fd, follow_symlinks=False)
            if not stat.S_ISREG(info.st_mode) or info.st_uid != _euid():
                raise first from None
            os.chmod(name, 0o600, dir_fd=dir_fd)
        except OSError:
            raise first from None
    return os.open(name, flags, 0o600, dir_fd=dir_fd)


def open_file(dir_fd: int, name: str, *, create: bool, read_only: bool = False) -> int | None:
    """Open a state file safely; None when missing and not created. The caller closes it."""
    _check_name(name)
    flags = os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK
    flags |= os.O_RDONLY if read_only else (os.O_RDWR | os.O_APPEND)
    if create:
        flags |= os.O_CREAT
    try:
        fd = _open_at(name, flags, dir_fd)
    except FileNotFoundError:
        return None
    except OSError as exc:
        if exc.errno == errno.ELOOP:
            raise StateError("state_symlink", "state file is a symlink") from exc
        raise _io_error("file", exc) from exc
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode):
            raise StateError("state_type", "state file is not a regular file")
        if info.st_uid != _euid():
            raise StateError("state_owner", "state file is not owned by the current user")
        if stat.S_IMODE(info.st_mode) != 0o600:
            os.fchmod(fd, 0o600)
    except OSError as exc:
        os.close(fd)
        raise _io_error("file", exc) from exc
    except BaseException:
        os.close(fd)
        raise
    return fd


def _lock(fd: int, deadline: float) -> None:
    delay = _POLL
    while True:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return
        except OSError as exc:
            if exc.errno not in (errno.EWOULDBLOCK, errno.EAGAIN, errno.EACCES):
                raise _io_error("lock", exc) from exc
        if time.monotonic() >= deadline:
            raise StateError("lock_timeout", "timed out waiting for the state file lock")
        time.sleep(delay)
        delay = min(delay * 2, _POLL_MAX)


def _is_current(fd: int, dir_fd: int, name: str) -> bool:
    try:
        now = os.stat(name, dir_fd=dir_fd, follow_symlinks=False)
    except FileNotFoundError:
        return False
    mine = os.fstat(fd)
    return (now.st_ino, now.st_dev) == (mine.st_ino, mine.st_dev)


@contextmanager
def locked_append_file(dir_fd: int, name: str, *, timeout: float = LOCK_TIMEOUT) -> Iterator[int]:
    """Open ``name`` (created ``0600``) and hold its exclusive flock for the ``with`` body.

    The fd is ``O_RDWR|O_APPEND``: writes always land at the end. The file is re-opened when the
    name no longer refers to the locked inode (it was rotated while this process waited).
    """
    deadline = time.monotonic() + timeout
    for _ in range(_REOPEN_LIMIT):
        fd = open_file(dir_fd, name, create=True)
        if fd is None:
            raise StateError("state_io", "cannot open the state file")
        try:
            _lock(fd, deadline)
            current = _is_current(fd, dir_fd, name)
        except BaseException:
            os.close(fd)
            raise
        if current:
            try:
                yield fd
            finally:
                os.close(fd)  # closing drops the flock
            return
        os.close(fd)
    raise StateError("lock_timeout", "state file kept changing while waiting for its lock")


def write_all(fd: int, data: bytes) -> None:
    """Write every byte (a short write is continued); OSError becomes ``state_io``."""
    view = memoryview(data)
    try:
        while view:
            written = os.write(fd, view)
            view = view[written:]
    except OSError as exc:
        raise _io_error("file", exc) from exc


# --- the tree cleaner --------------------------------------------------------------------------

MAX_DEPTH = 12
MAX_NODES = 100_000


def clean_tree(value: object, *, max_nodes: int = MAX_NODES) -> object:
    """Return ``value`` with every string (and key) sanitized then scrubbed.

    Accepts dicts with string keys, lists, tuples, strings, ints, finite floats, bools and None;
    anything else, a tree deeper than 12 levels or one with more than ``max_nodes`` nodes is
    ``record_invalid``. The result is a new structure; the input is not changed.
    """
    budget = [max_nodes]

    def walk(node: object, depth: int) -> object:
        budget[0] -= 1
        if budget[0] < 0 or depth > MAX_DEPTH:
            raise StateError("record_invalid", "record is too large or too deeply nested")
        if isinstance(node, str):
            return render(node)
        if node is None or isinstance(node, bool | int):
            return node
        if isinstance(node, float):
            if node != node or node in (float("inf"), float("-inf")):
                raise StateError("record_invalid", "record holds a non-finite number")
            return node
        if isinstance(node, Mapping):
            out: dict[str, object] = {}
            for key, item in node.items():
                if not isinstance(key, str):
                    raise StateError("record_invalid", "record holds a non-string key")
                out[render(key)] = walk(item, depth + 1)
            return out
        if isinstance(node, list | tuple):
            return [walk(item, depth + 1) for item in node]
        raise StateError("record_invalid", f"record holds a {type(node).__name__} value")

    return walk(value, 0)
