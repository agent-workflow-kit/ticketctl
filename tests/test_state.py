"""State directory: location, permissions, ownership, symlink refusal and the flock helper.

Every test uses a private directory under ``tmp_path`` and an explicit environment mapping.
"""

from __future__ import annotations

import fcntl
import os
import stat
from pathlib import Path

import pytest

from ticketctl import sanitize, state
from ticketctl.state import StateError


@pytest.fixture
def home(tmp_path: Path) -> Path:
    (tmp_path / "home").mkdir()
    return tmp_path


def env_for(home: Path, **extra: str) -> dict[str, str]:
    return {"HOME": str(home / "home"), **extra}


# --- location --------------------------------------------------------------------------------


def test_default_location_is_under_home(home: Path) -> None:
    assert state.state_dir_path(env_for(home)) == home / "home" / ".local" / "state" / "ticketctl"


def test_xdg_state_home_is_respected(home: Path) -> None:
    env = env_for(home, XDG_STATE_HOME=str(home / "xdg"))
    assert state.state_dir_path(env) == home / "xdg" / "ticketctl"


@pytest.mark.parametrize("bad", ["", "relative/dir", "~/x"])
def test_relative_or_empty_xdg_state_home_is_ignored(home: Path, bad: str) -> None:
    env = env_for(home, XDG_STATE_HOME=bad)
    assert state.state_dir_path(env) == home / "home" / ".local" / "state" / "ticketctl"


# --- the directory ---------------------------------------------------------------------------


def test_open_dir_creates_missing_parents_and_a_0700_directory(home: Path) -> None:
    path = home / "a" / "b" / "ticketctl"
    fd = state.open_dir(path)
    try:
        assert fd is not None
    finally:
        assert fd is not None
        os.close(fd)
    assert stat.S_IMODE(path.stat().st_mode) == 0o700


def test_open_dir_without_create_reports_missing(home: Path) -> None:
    assert state.open_dir(home / "nope", create=False) is None
    assert not (home / "nope").exists()


def test_open_dir_tightens_a_loose_directory_it_owns(home: Path) -> None:
    path = home / "ticketctl"
    path.mkdir()
    path.chmod(0o755)
    fd = state.open_dir(path)
    assert fd is not None
    os.close(fd)
    assert stat.S_IMODE(path.stat().st_mode) == 0o700


def test_open_dir_is_not_confused_by_a_restrictive_umask(home: Path) -> None:
    old = os.umask(0o277)
    try:
        fd = state.open_dir(home / "ticketctl")
    finally:
        os.umask(old)
    assert fd is not None
    os.close(fd)
    assert stat.S_IMODE((home / "ticketctl").stat().st_mode) == 0o700


def test_a_foreign_owner_is_internal_exit_1(home: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = home / "ticketctl"
    path.mkdir()
    monkeypatch.setattr(state, "_euid", lambda: os.geteuid() + 1)
    with pytest.raises(StateError) as info:
        state.open_dir(path)
    assert (info.value.exit_code, info.value.code, info.value.reason) == (
        1,
        "internal",
        "state_owner",
    )


def test_a_symlinked_state_directory_is_refused(home: Path) -> None:
    target = home / "elsewhere"
    target.mkdir()
    (home / "ticketctl").symlink_to(target)
    with pytest.raises(StateError) as info:
        state.open_dir(home / "ticketctl")
    assert info.value.reason == "state_symlink"
    assert list(target.iterdir()) == []


def test_a_symlinked_ancestor_is_allowed(home: Path) -> None:
    real = home / "real"
    real.mkdir()
    (home / "link").symlink_to(real)
    fd = state.open_dir(home / "link" / "ticketctl")
    assert fd is not None
    os.close(fd)
    assert (real / "ticketctl").is_dir()


def test_a_regular_file_in_place_of_the_directory_is_refused(home: Path) -> None:
    (home / "ticketctl").write_text("x")
    with pytest.raises(StateError) as info:
        state.open_dir(home / "ticketctl")
    assert info.value.reason in {"state_type", "state_symlink"}


def test_open_subdir_creates_0700_and_refuses_a_symlink(home: Path) -> None:
    fd = state.open_dir(home / "ticketctl")
    assert fd is not None
    try:
        sub = state.open_subdir(fd, "cache")
        assert sub is not None
        os.close(sub)
        assert stat.S_IMODE((home / "ticketctl" / "cache").stat().st_mode) == 0o700
        (home / "outside").mkdir()
        (home / "ticketctl" / "evil").symlink_to(home / "outside")
        with pytest.raises(StateError) as info:
            state.open_subdir(fd, "evil")
        assert info.value.reason == "state_symlink"
        assert state.open_subdir(fd, "missing", create=False) is None
    finally:
        os.close(fd)


# --- files -----------------------------------------------------------------------------------


def dir_fd(home: Path) -> int:
    fd = state.open_dir(home / "ticketctl")
    assert fd is not None
    return fd


def test_a_new_file_is_0600(home: Path) -> None:
    fd = dir_fd(home)
    try:
        with state.locked_append_file(fd, "audit.jsonl"):
            pass
    finally:
        os.close(fd)
    assert stat.S_IMODE((home / "ticketctl" / "audit.jsonl").stat().st_mode) == 0o600


def test_a_loose_file_it_owns_is_tightened(home: Path) -> None:
    fd = dir_fd(home)
    (home / "ticketctl" / "log.jsonl").write_text("")
    (home / "ticketctl" / "log.jsonl").chmod(0o666)
    try:
        with state.locked_append_file(fd, "log.jsonl"):
            pass
    finally:
        os.close(fd)
    assert stat.S_IMODE((home / "ticketctl" / "log.jsonl").stat().st_mode) == 0o600


def test_a_symlinked_state_file_is_refused_and_its_target_untouched(home: Path) -> None:
    fd = dir_fd(home)
    victim = home / "victim.txt"
    victim.write_text("keep")
    (home / "ticketctl" / "audit.jsonl").symlink_to(victim)
    try:
        with pytest.raises(StateError) as info, state.locked_append_file(fd, "audit.jsonl"):
            pytest.fail("must not open")
    finally:
        os.close(fd)
    assert info.value.reason == "state_symlink"
    assert victim.read_text() == "keep"


def test_a_dangling_symlinked_state_file_is_refused_and_not_created(home: Path) -> None:
    fd = dir_fd(home)
    (home / "ticketctl" / "audit.jsonl").symlink_to(home / "not-there")
    try:
        with pytest.raises(StateError), state.locked_append_file(fd, "audit.jsonl"):
            pytest.fail("must not open")
    finally:
        os.close(fd)
    assert not (home / "not-there").exists()


def test_a_fifo_in_place_of_a_state_file_is_refused_without_blocking(home: Path) -> None:
    fd = dir_fd(home)
    os.mkfifo(home / "ticketctl" / "audit.jsonl")
    try:
        with pytest.raises(StateError) as info, state.locked_append_file(fd, "audit.jsonl"):
            pytest.fail("must not open")
    finally:
        os.close(fd)
    assert info.value.reason == "state_type"


def test_a_file_with_a_foreign_owner_is_refused(
    home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fd = dir_fd(home)
    (home / "ticketctl" / "audit.jsonl").write_text("")
    monkeypatch.setattr(state, "_euid", lambda: os.geteuid() + 1)
    try:
        with pytest.raises(StateError) as info, state.locked_append_file(fd, "audit.jsonl"):
            pytest.fail("must not open")
    finally:
        os.close(fd)
    assert info.value.reason == "state_owner"


def test_a_name_with_a_slash_is_refused(home: Path) -> None:
    fd = dir_fd(home)
    try:
        with pytest.raises(StateError), state.locked_append_file(fd, "../x"):
            pytest.fail("must not open")
    finally:
        os.close(fd)


# --- the flock -------------------------------------------------------------------------------


def test_the_lock_times_out_instead_of_hanging(home: Path) -> None:
    fd = dir_fd(home)
    path = home / "ticketctl" / "audit.jsonl"
    path.write_text("")
    other = os.open(path, os.O_RDWR)
    fcntl.flock(other, fcntl.LOCK_EX)
    try:
        with pytest.raises(StateError) as info:
            with state.locked_append_file(fd, "audit.jsonl", timeout=0.2):
                pytest.fail("must not get the lock")
        assert info.value.reason == "lock_timeout"
    finally:
        os.close(other)
        os.close(fd)


def test_the_lock_is_released_on_exit_and_on_error(home: Path) -> None:
    fd = dir_fd(home)
    try:
        with pytest.raises(RuntimeError), state.locked_append_file(fd, "audit.jsonl"):
            raise RuntimeError("boom")
        with state.locked_append_file(fd, "audit.jsonl", timeout=0.5):
            pass
    finally:
        os.close(fd)


def test_a_renamed_file_makes_the_waiter_reopen_the_new_one(home: Path) -> None:
    """The lock on an inode that was rotated away is not the lock on the current file."""
    fd = dir_fd(home)
    name = "log.jsonl"
    try:
        with state.locked_append_file(fd, name):
            os.rename(name, "log.jsonl.1", src_dir_fd=fd, dst_dir_fd=fd)
        # The rename happened under the lock; the next opener must create and lock a new file.
        with state.locked_append_file(fd, name) as new_fd:
            assert os.fstat(new_fd).st_size == 0
            assert os.stat(name, dir_fd=fd).st_ino == os.fstat(new_fd).st_ino
    finally:
        os.close(fd)


# --- the tree cleaner ------------------------------------------------------------------------


def test_clean_tree_sanitizes_strings_and_keys() -> None:
    cleaned = state.clean_tree({"k\x1b": ["\x1b[31mred", "ok", 3, None, True, 1.5]})
    assert cleaned == {"k<U+001B>": ["<U+001B>[31mred", "ok", 3, None, True, 1.5]}


def test_clean_tree_leaves_numbers_alone_by_default() -> None:
    sanitize.configure_scrubber(["12345678"])
    assert state.clean_tree([12345678, 912345678]) == [12345678, 912345678]


def test_clean_tree_redacts_only_an_exactly_equal_number_when_asked() -> None:
    sanitize.configure_scrubber(["12345678"])
    cleaned = state.clean_tree([12345678, 912345678, 123456789], redact_numbers=True)
    assert cleaned == ["<redacted>", 912345678, 123456789]


@pytest.mark.parametrize(
    "bad",
    [float("nan"), float("inf"), object(), b"bytes", {1: "int key"}, {"a": {1, 2}}],
)
def test_clean_tree_refuses_unserializable_values(bad: object) -> None:
    with pytest.raises(StateError) as info:
        state.clean_tree(bad)
    assert info.value.reason == "record_invalid"


def test_clean_tree_refuses_deep_and_huge_trees() -> None:
    deep: object = "x"
    for _ in range(50):
        deep = [deep]
    with pytest.raises(StateError):
        state.clean_tree(deep)
    with pytest.raises(StateError):
        state.clean_tree(list(range(1000)), max_nodes=100)
