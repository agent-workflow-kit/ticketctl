"""The one input file reader and the secret_in_body check (docs/interface.md, Input files)."""

from __future__ import annotations

import io
import os
import socket
import stat
import threading
from pathlib import Path

import pytest

from ticketctl import sanitize as s

BODY_CHARS = 100  # a small limits.body_chars keeps the cap tests cheap: cap is 4 * 100 bytes


def _read(path: str | Path, kind: str = "body") -> str:
    return s.read_input_file(str(path), kind=kind, body_chars=BODY_CHARS)


def _fail(path: str | Path, kind: str = "body") -> s.InputError:
    with pytest.raises(s.InputError) as info:
        _read(path, kind)
    err = info.value
    assert (err.exit_code, err.code, err.reason) == (22, "invalid_request", "invalid_file")
    return err


def test_reads_a_regular_file(tmp_path: Path) -> None:
    f = tmp_path / "b.md"
    f.write_text("hello é\n", encoding="utf-8")
    assert _read(f) == "hello é\n"


def test_reads_an_empty_file(tmp_path: Path) -> None:
    f = tmp_path / "e"
    f.write_bytes(b"")
    assert _read(f) == ""


def test_reader_does_not_sanitize_or_normalize(tmp_path: Path) -> None:
    f = tmp_path / "b"
    f.write_bytes(b"a\r\n\x1b[0m\x00b")
    assert _read(f) == "a\r\n\x1b[0m\x00b"  # the sanitizer is applied downstream, never here


def test_strict_utf8(tmp_path: Path) -> None:
    for payload in [b"\xff\xfe", b"\xc0\xaf", b"\xed\xa0\x80", b"ok\x80", b"\xf4\x90\x80\x80"]:
        f = tmp_path / "bad"
        f.write_bytes(payload)
        _fail(f)


def test_missing_file_and_directory(tmp_path: Path) -> None:
    _fail(tmp_path / "nope")
    _fail(tmp_path)


def test_symlink_is_refused(tmp_path: Path) -> None:
    real = tmp_path / "real"
    real.write_text("x", encoding="utf-8")
    link = tmp_path / "link"
    link.symlink_to(real)
    _fail(link)


def test_dangling_symlink_and_symlink_loop_are_refused(tmp_path: Path) -> None:
    (tmp_path / "dangling").symlink_to(tmp_path / "gone")
    _fail(tmp_path / "dangling")
    (tmp_path / "loop").symlink_to(tmp_path / "loop")
    _fail(tmp_path / "loop")


def test_fifo_is_refused_without_blocking(tmp_path: Path) -> None:
    fifo = tmp_path / "fifo"
    os.mkfifo(fifo)
    result: list[BaseException | str] = []

    def run() -> None:
        try:
            result.append(_read(fifo))
        except BaseException as exc:  # noqa: BLE001
            result.append(exc)

    t = threading.Thread(target=run, daemon=True)
    t.start()
    t.join(5)
    if t.is_alive():
        # Unblock the reader so the test process can exit, then fail.
        fd = os.open(fifo, os.O_WRONLY | os.O_NONBLOCK)
        os.close(fd)
        t.join(5)
        pytest.fail("reading a FIFO blocked")
    assert isinstance(result[0], s.InputError)
    assert result[0].reason == "invalid_file"


def test_fifo_with_a_writer_is_still_refused(tmp_path: Path) -> None:
    fifo = tmp_path / "fifo"
    os.mkfifo(fifo)
    r = os.open(fifo, os.O_RDONLY | os.O_NONBLOCK)
    w = os.open(fifo, os.O_WRONLY)
    try:
        os.write(w, b"secret data")
        _fail(fifo)
    finally:
        os.close(w)
        os.close(r)


def test_unix_socket_is_refused(tmp_path: Path) -> None:
    path = tmp_path / "sock"
    srv = socket.socket(socket.AF_UNIX)
    try:
        srv.bind(str(path))
        _fail(path)
    finally:
        srv.close()


@pytest.mark.parametrize("dev", ["/dev/zero", "/dev/null", "/dev/urandom", "/dev/full"])
def test_device_files_are_refused(dev: str) -> None:
    if not os.path.exists(dev):
        pytest.skip(f"{dev} missing")
    _fail(dev)


@pytest.mark.skipif(not os.path.isdir("/proc/self"), reason="needs /proc")
def test_path_under_proc_is_refused() -> None:
    _fail("/proc/self/status")
    _fail("/proc/version")


@pytest.mark.skipif(not os.path.isdir("/proc/self"), reason="needs /proc")
def test_proc_reached_through_a_symlinked_directory_is_refused(tmp_path: Path) -> None:
    # O_NOFOLLOW covers only the last component; the descriptor's resolved path catches this.
    (tmp_path / "p").symlink_to("/proc/self")
    _fail(tmp_path / "p" / "status")


@pytest.mark.skipif(not os.path.isdir("/sys/kernel"), reason="needs /sys")
def test_path_under_sys_is_refused() -> None:
    candidates = [
        p
        for p in ("/sys/kernel/mm/transparent_hugepage/enabled", "/sys/kernel/uevent_seqnum")
        if os.path.exists(p)
    ]
    if not candidates:
        pytest.skip("no readable sysfs file")
    for c in candidates:
        _fail(c)


def test_resolved_fd_path_check_refuses_dev_prefixes() -> None:
    for p in ["/dev", "/dev/shm/x", "/proc/1/x", "/sys/x", "/proc", "/sys"]:
        assert s.is_forbidden_path(p), p
    for p in ["/devices/x", "/procfs/x", "/home/u/dev/x", "/tmp/x", "/system/x", "/"]:
        assert not s.is_forbidden_path(p), p


def test_directory_is_refused_by_the_lstat_pre_check(tmp_path: Path) -> None:
    d = tmp_path / "d"
    d.mkdir()
    _fail(d)


def test_fifo_that_lstat_reports_as_regular_is_refused_by_fstat_without_blocking(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fifo = tmp_path / "fifo"
    os.mkfifo(fifo)
    real_lstat = os.lstat
    real_fstat = os.fstat
    fstat_calls: list[int] = []

    def fake_lstat(path: str, *a: object, **k: object) -> os.stat_result:
        info = real_lstat(path, *a, **k)  # type: ignore[arg-type]
        if str(path) != str(fifo):
            return info
        fields = list(info)
        fields[0] = stat.S_IFREG | 0o600
        return os.stat_result(fields)

    def spy_fstat(fd: int) -> os.stat_result:
        fstat_calls.append(fd)
        return real_fstat(fd)

    monkeypatch.setattr(os, "lstat", fake_lstat)
    monkeypatch.setattr(os, "fstat", spy_fstat)
    result: list[BaseException | str] = []

    def run() -> None:
        try:
            result.append(_read(fifo))
        except BaseException as exc:  # noqa: BLE001
            result.append(exc)

    t = threading.Thread(target=run, daemon=True)
    t.start()
    t.join(5)
    if t.is_alive():
        fd = os.open(fifo, os.O_WRONLY | os.O_NONBLOCK)
        os.close(fd)
        t.join(5)
        pytest.fail("reading a FIFO blocked")
    assert fstat_calls, "the refusal must come from the fstat branch"
    assert isinstance(result[0], s.InputError)
    assert result[0].reason == "invalid_file"
    assert "not a regular file" in str(result[0])


def test_dev_ino_mismatch_between_lstat_and_fstat_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    f = tmp_path / "f"
    f.write_text("x", encoding="utf-8")
    real_lstat = os.lstat

    def fake_lstat(path: str, *a: object, **k: object) -> os.stat_result:
        info = real_lstat(path, *a, **k)  # type: ignore[arg-type]
        if str(path) != str(f):
            return info
        fields = list(info)
        fields[1] = info.st_ino + 1
        return os.stat_result(fields)

    monkeypatch.setattr(os, "lstat", fake_lstat)
    assert "changed while opening" in str(_fail(f))


def test_path_with_a_lone_surrogate_is_invalid_file() -> None:
    _fail("bad\ud800name")
    _fail("/tmp/bad\ud800name")


def test_bom_and_nul_pass_the_reader_and_become_markers_in_the_sanitizer(tmp_path: Path) -> None:
    f = tmp_path / "b"
    f.write_bytes("\ufeffa\x00b".encode())
    text = _read(f)
    assert text == "\ufeffa\x00b"  # strict UTF-8 only: no BOM stripping, no NUL refusal
    assert s.sanitize_line(text) == "<U+FEFF>a<U+0000>b"
    assert s.sanitize_body(text) == "<U+FEFF>a<U+0000>b"


def test_body_size_cap_is_four_times_body_chars_bytes(tmp_path: Path) -> None:
    ok = tmp_path / "ok"
    ok.write_bytes(b"a" * (4 * BODY_CHARS))
    assert len(_read(ok)) == 4 * BODY_CHARS
    big = tmp_path / "big"
    big.write_bytes(b"a" * (4 * BODY_CHARS + 1))
    _fail(big)


def test_other_kinds_are_capped_at_one_mebibyte(tmp_path: Path) -> None:
    ok = tmp_path / "ok"
    ok.write_bytes(b"a" * (1 << 20))
    assert len(_read(ok, "title")) == 1 << 20
    big = tmp_path / "big"
    big.write_bytes(b"a" * ((1 << 20) + 1))
    for kind in ("title", "jql", "text"):
        _fail(big, kind)


def test_unknown_kind_is_a_value_error(tmp_path: Path) -> None:
    f = tmp_path / "f"
    f.write_text("x", encoding="utf-8")
    with pytest.raises(ValueError):
        s.read_input_file(str(f), kind="other", body_chars=BODY_CHARS)


def test_error_message_is_sanitized_and_cut(tmp_path: Path) -> None:
    name = "\x1b[31m" + "n" * 300
    err = _fail(tmp_path / name)
    assert "\x1b" not in str(err)
    assert "n" * 100 not in str(err)


def test_error_message_does_not_echo_file_content(tmp_path: Path) -> None:
    f = tmp_path / "f"
    f.write_bytes(b"TOPSECRETCONTENT\xff")
    assert "TOPSECRETCONTENT" not in str(_fail(f))


def test_file_descriptor_is_not_leaked(tmp_path: Path) -> None:
    before = len(os.listdir("/proc/self/fd")) if os.path.isdir("/proc/self/fd") else None
    f = tmp_path / "f"
    f.write_text("x", encoding="utf-8")
    for _ in range(20):
        _read(f)
        _fail(tmp_path / "nope")
        _fail(tmp_path)
    if before is not None:
        assert len(os.listdir("/proc/self/fd")) == before


def test_o_nonblock_is_cleared_after_open(tmp_path: Path) -> None:
    # A large regular file is read completely: a still non-blocking descriptor would be fine for
    # files, so assert the helper that opens clears the flag explicitly.
    f = tmp_path / "f"
    f.write_bytes(b"a" * 300)
    fd = s.open_input_fd(str(f))
    try:
        import fcntl

        assert not (fcntl.fcntl(fd, fcntl.F_GETFL) & os.O_NONBLOCK)
        assert fcntl.fcntl(fd, fcntl.F_GETFD) & fcntl.FD_CLOEXEC
    finally:
        os.close(fd)


# --- stdin ----------------------------------------------------------------------------------


class _Stdin:
    def __init__(self, data: bytes, tty: bool = False) -> None:
        self.buffer = io.BytesIO(data)
        self._tty = tty

    def isatty(self) -> bool:
        return self._tty


def test_stdin_dash_reads_with_the_same_caps_and_utf8() -> None:
    assert s.read_input_stdin(_Stdin(b"hi \xc3\xa9"), kind="body", body_chars=BODY_CHARS) == "hi é"
    with pytest.raises(s.InputError) as info:
        s.read_input_stdin(_Stdin(b"a" * (4 * BODY_CHARS + 1)), kind="body", body_chars=BODY_CHARS)
    assert info.value.reason == "invalid_file"
    with pytest.raises(s.InputError):
        s.read_input_stdin(_Stdin(b"\xff"), kind="body", body_chars=BODY_CHARS)


def test_stdin_tty_is_exit_2_invalid_argument() -> None:
    with pytest.raises(s.InputError) as info:
        s.read_input_stdin(_Stdin(b"", tty=True), kind="body", body_chars=BODY_CHARS)
    assert (info.value.exit_code, info.value.code, info.value.reason) == (
        2,
        "usage",
        "invalid_argument",
    )


def test_stdin_none_or_without_buffer_is_exit_2_invalid_argument(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class _NoBuffer:
        def isatty(self) -> bool:
            return False

    for source in (None, _NoBuffer()):
        monkeypatch.setattr("sys.stdin", source)
        with pytest.raises(s.InputError) as info:
            s.read_input("-", kind="body", body_chars=BODY_CHARS)
        assert (info.value.exit_code, info.value.code, info.value.reason) == (
            2,
            "usage",
            "invalid_argument",
        )


def test_dash_path_is_not_opened_as_a_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    (tmp_path / "-").write_text("file named dash", encoding="utf-8")
    assert (
        s.read_input("-", kind="body", body_chars=BODY_CHARS, stdin=_Stdin(b"from stdin"))
        == "from stdin"
    )
    assert s.read_input("./-", kind="body", body_chars=BODY_CHARS) == "file named dash"


# --- secret_in_body -------------------------------------------------------------------------

TOKEN = "tok-SENTINEL-0123456789"


def test_secret_in_body_is_exit_22_and_never_echoes() -> None:
    text = f"line one\nprefix {TOKEN} suffix"
    with pytest.raises(s.InputError) as info:
        s.ensure_no_secret(text, secrets=[TOKEN])
    err = info.value
    assert (err.exit_code, err.code, err.reason) == (22, "invalid_request", "secret_in_body")
    msg = str(err)
    assert TOKEN not in msg
    assert "tok-" not in msg
    assert "prefix" not in msg and "line one" not in msg
    assert (
        "offset" not in msg.lower()
        and "line 2" not in msg.lower()
        and "position" not in msg.lower()
    )
    assert not any(ch.isdigit() for ch in msg)  # no position or length


def test_secret_check_is_the_same_message_for_every_position() -> None:
    msgs = set()
    for text in [TOKEN, "x" * 50 + TOKEN, TOKEN + "y" * 900, f"a\n{TOKEN}\nb"]:
        with pytest.raises(s.InputError) as info:
            s.ensure_no_secret(text, secrets=[TOKEN])
        msgs.add(str(info.value))
    assert len(msgs) == 1


def test_secret_check_covers_every_value_given() -> None:
    with pytest.raises(s.InputError):
        s.ensure_no_secret("fine", "also fine", f"bad {TOKEN}", secrets=[TOKEN])


def test_secret_check_passes_clean_text_and_short_or_empty_secrets() -> None:
    s.ensure_no_secret("nothing here", secrets=[TOKEN])
    s.ensure_no_secret("1234567 abc", secrets=["1234567", "", "   "])
    s.ensure_no_secret("", secrets=[TOKEN])
    s.ensure_no_secret("anything", secrets=[])


def test_secret_check_uses_the_configured_scrubber_by_default() -> None:
    s.configure_scrubber([TOKEN])
    try:
        with pytest.raises(s.InputError):
            s.ensure_no_secret(f"x {TOKEN}")
    finally:
        s.configure_scrubber([])
    s.ensure_no_secret(f"x {TOKEN}")


def test_secret_check_does_not_trip_on_a_near_miss() -> None:
    s.ensure_no_secret(TOKEN[:-1], secrets=[TOKEN])
    s.ensure_no_secret(TOKEN.upper(), secrets=[TOKEN])
