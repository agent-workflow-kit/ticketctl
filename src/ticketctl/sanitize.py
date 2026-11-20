"""The one sanitizer, the output scrub, the identifier grammars and the input file reader.

Public API (everything else is private); the rules live in docs/interface.md under
"Sanitizing", "Identifiers" and "Input files".

Text:
    sanitize_line(text), sanitize_body(text)   the one sanitizer rule (single-line / body class)
    cap_text(text, cap), sanitize_field(...)   over-long text cut with the ``<truncated>`` marker
    CAPS                                        the documented per-field caps
Output scrub (last step of the one output path):
    configure_scrubber(secrets, basic_values)  register the configured token values
    basic_auth_value(email, token)             the base64 value of the Basic header
    scrub(text), render(text, body=False)      ``render`` = sanitize, then scrub
    format_traceback(exc)                      a scrubbed, sanitized traceback
Identifiers:
    GRAMMARS, Grammar.matches/.check           ``fullmatch`` grammars; ``check`` raises InputError
    normalize_ticket_key(text)                 ASCII case-insensitive input, upper-case output
    segment(grammar_name, value), query(params) the only way to build a URL path segment / query
    split_url(url), browse_key(path)           ``urlsplit`` with ValueError -> invalid_value
Input:
    read_input(path, kind=..., body_chars=...) one reader for every file argument (``-`` = stdin)
    ensure_no_secret(*texts)                   exit 22 ``secret_in_body`` before any send
"""

from __future__ import annotations

import base64
import fcntl
import json
import os
import re
import stat
import sys
import traceback
import unicodedata
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Protocol
from urllib.parse import SplitResult, quote, urlencode, urlsplit

# --------------------------------------------------------------------------------------------
# Errors
# --------------------------------------------------------------------------------------------

# TODO(#5): the CLI owns the exit-code table and the error envelope; it maps InputError (which
# carries exit_code, code and reason exactly as in the interface's table) onto its envelope.


class InputError(Exception):
    """A refusal of caller input, carrying the interface's exit code, code and reason."""

    def __init__(self, exit_code: int, code: str, reason: str, message: str) -> None:
        super().__init__(message)
        self.exit_code = exit_code
        self.code = code
        self.reason = reason
        self.message = message


def _invalid_request(reason: str, message: str) -> InputError:
    return InputError(22, "invalid_request", reason, message)


def _usage(reason: str, message: str) -> InputError:
    return InputError(2, "usage", reason, message)


# --------------------------------------------------------------------------------------------
# Sanitizer
# --------------------------------------------------------------------------------------------

_CATEGORIES = frozenset({"Cc", "Cf", "Cs", "Co", "Cn", "Zl", "Zp"})
_EXPLICIT = frozenset(
    {0x034F, 0x115F, 0x1160, 0x16FE4, 0x17B4, 0x17B5, 0x2800, 0x3164, 0xFFA0, 0xFFFC}
    | set(range(0x180B, 0x1810))
    | set(range(0xFE00, 0xFE10))
    | set(range(0xE0100, 0xE01F0))
)
# Printable ASCII never needs a lookup; every other run goes through the table.
_NON_PRINTABLE_ASCII_RUN = re.compile(r"[^\x20-\x7e]+")
_NEWLINES = re.compile(r"\r\n|\r")
# A cut never leaves the start of a ``<U+XXXX>`` or ``<redacted>`` marker at the end of the text.
_MARKER_PREFIX = re.compile(
    r"<(?:U(?:\+[0-9A-F]{0,6})?|r(?:e(?:d(?:a(?:c(?:t(?:e(?:d)?)?)?)?)?)?)?)?$"
)
TRUNCATED = "<truncated>"

CAPS: dict[str, int] = {
    "title": 4096,
    "summary": 4096,
    "body": 1_000_000,
    "label": 256,
    "name": 256,
    "attachment_name": 1024,
}


def _replacement(ch: str, *, body: bool) -> str:
    cp = ord(ch)
    if ch in "\n\t":
        return ch if body else " "
    if cp in _EXPLICIT or unicodedata.category(ch) in _CATEGORIES:
        return f"<U+{cp:04X}>"
    return ch


def sanitize_line(text: str) -> str:
    """Sanitize a single-line field: ``\\n``, ``\\r`` and ``\\t`` become one space each."""
    return _sanitize_all(text, body=False)


def sanitize_body(text: str) -> str:
    """Sanitize a body-class field: ``\\n`` and ``\\t`` stay, CR and CRLF become ``\\n``."""
    return _sanitize_all(text, body=True)


def _sanitize_all(text: str, *, body: bool) -> str:
    if not isinstance(text, str):
        raise TypeError(f"sanitizer needs str, got {type(text).__name__}")
    text = _NEWLINES.sub("\n", text)
    return _NON_PRINTABLE_ASCII_RUN.sub(
        lambda m: "".join(_replacement(c, body=body) for c in m.group()), text
    )


def cap_text(text: str, cap: int) -> tuple[str, bool]:
    """Cut ``text`` to at most ``cap`` characters, ending in ``<truncated>``; report the cut.

    Cuts at a code-point boundary and never inside a ``<U+XXXX>`` or ``<redacted>`` marker. Call
    it on sanitized, scrubbed text.
    """
    if len(text) <= cap:
        return text, False
    keep = max(cap - len(TRUNCATED), 0)
    head = text[:keep]
    head = _MARKER_PREFIX.sub("", head)
    return (head + TRUNCATED)[: max(cap, 0)], True


def sanitize_field(text: str, *, cap: int, body: bool = False) -> tuple[str, bool]:
    """Sanitize, scrub, then cap: returns the text and whether it was cut.

    The scrub runs before the cut so a secret that straddles the cap cannot leave a prefix; a
    final scrub of the capped text is harmless.
    """
    capped, cut = cap_text(render(text, body=body), cap)
    return scrub(capped), cut


# --------------------------------------------------------------------------------------------
# Output scrub
# --------------------------------------------------------------------------------------------

REDACTED = "<redacted>"
MIN_SECRET_LENGTH = 8

_scrub_values: list[str] = []


def basic_auth_value(email: str, token: str) -> str:
    """The base64 of ``email:token``, both stripped as the gateway does."""
    raw = f"{email.strip()}:{token.strip()}".encode()
    return base64.b64encode(raw).decode("ascii")


def _secret_forms(value: object) -> set[str]:
    """The stripped secret, its URL- and JSON-encoded forms and their sanitized forms.

    Only forms of at least 8 characters count; the minimum applies to each form's own length.
    """
    if not isinstance(value, str):
        return set()
    stripped = value.strip()
    if len(stripped) < MIN_SECRET_LENGTH:
        return set()
    bases = {
        stripped,
        quote(stripped, safe=""),
        quote(stripped),
        json.dumps(stripped)[1:-1],
    }
    forms: set[str] = set()
    for base in bases:
        for form in (base, sanitize_line(base), sanitize_body(base)):
            if len(form) >= MIN_SECRET_LENGTH:
                forms.add(form)
    return forms


def configure_scrubber(secrets: Iterable[str], *, basic_values: Iterable[str] = ()) -> None:
    """Replace the registered secrets.

    Each value is stripped of surrounding whitespace first, and the 8-character minimum applies
    to the stripped length. The stripped value, its percent-encoded forms (``quote`` with and
    without safe characters) and its JSON-escaped form are registered, each also in its
    sanitized form since the scrub runs after the sanitizer.

    TODO(#6): the CLI and the config loader must resolve the ``token_env`` values and call this
    once before any output; until it is called the scrub is the identity and fails open.
    """
    global _scrub_values
    values: set[str] = set()
    for value in list(secrets) + list(basic_values):
        values |= _secret_forms(value)
    _scrub_values = sorted(values, key=lambda v: (-len(v), v))


def _configured_secrets() -> list[str]:
    return list(_scrub_values)


def _match_spans(text: str) -> list[tuple[int, int]]:
    spans: list[tuple[int, int]] = []
    for value in _scrub_values:
        pos = text.find(value)
        while pos != -1:
            spans.append((pos, pos + len(value)))
            pos = text.find(value, pos + 1)
    return spans


def scrub(text: str) -> str:
    """Replace every occurrence of a configured secret by ``<redacted>``.

    Matches of all registered forms are collected over the original text; overlapping and
    adjacent matches merge into one span, so overlapping secrets leave no partial.
    """
    if not _scrub_values:
        return text
    spans = sorted(_match_spans(text))
    if not spans:
        return text
    out: list[str] = []
    cursor = 0
    start, end = spans[0]
    for s, e in spans[1:]:
        if s <= end:
            end = max(end, e)
            continue
        out.append(text[cursor:start])
        out.append(REDACTED)
        cursor = end
        start, end = s, e
    out.append(text[cursor:start])
    out.append(REDACTED)
    out.append(text[end:])
    return "".join(out)


def render(text: str, *, body: bool = False) -> str:
    """The one output path: sanitizer first, scrub last."""
    return scrub(sanitize_body(text) if body else sanitize_line(text))


def format_traceback(exc: BaseException) -> str:
    """A traceback (with its chain) as body-class text, sanitized and scrubbed."""
    text = "".join(traceback.format_exception(exc))
    return render(text, body=True)


# --------------------------------------------------------------------------------------------
# Identifier grammars
# --------------------------------------------------------------------------------------------

_EXCERPT = 64


@dataclass(frozen=True)
class Grammar:
    """One identifier grammar, matched with ``fullmatch`` and the failure it maps to."""

    name: str
    pattern: re.Pattern[str]
    exit_code: int
    code: str
    reason: str

    def matches(self, value: object) -> bool:
        return isinstance(value, str) and self.pattern.fullmatch(value) is not None

    def check(self, value: str) -> str:
        """Return ``value`` when it matches; else raise the grammar's InputError."""
        if self.matches(value):
            return value
        shown = render(value if isinstance(value, str) else repr(value))
        shown, _ = cap_text(shown, _EXCERPT)
        raise InputError(self.exit_code, self.code, self.reason, f"invalid {self.name}: {shown}")


def _g(name: str, regex: str, kind: str) -> Grammar:
    # Explicit [0-9] and [A-Za-z] classes only: no \d, \w or IGNORECASE, so no Unicode digits.
    pattern = re.compile(regex)
    if kind == "key":
        return Grammar(name, pattern, 22, "invalid_request", "invalid_key")
    if kind == "site":
        return Grammar(name, pattern, 22, "invalid_request", "unknown_site")
    if kind == "value":
        return Grammar(name, pattern, 22, "invalid_request", "invalid_value")
    return Grammar(name, pattern, 2, "usage", "invalid_argument")


GRAMMARS: dict[str, Grammar] = {
    g.name: g
    for g in (
        _g("ticket_key", r"(?=.{1,64}$)[A-Z][A-Z0-9_]*-[0-9]+", "key"),
        _g("project_key", r"(?=.{1,64}$)[A-Z][A-Z0-9_]*", "key"),
        _g("site_name", r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}", "site"),
        _g("provider_id", r"[A-Za-z0-9_.:-]{1,64}", "flag"),
        _g("numeric_id", r"[0-9]{1,18}", "flag"),
        _g("custom_field_id", r"customfield_[0-9]+", "value"),
        _g("account_id", r"[A-Za-z0-9:_.-]{1,128}", "flag"),
        _g("decision_id", r"[A-Za-z0-9._:@/-]{1,128}", "flag"),
        _g("dedupe_key", r"[A-Za-z0-9._:@/-]{1,128}", "flag"),
        _g("actor", r"(?:human|auto):[A-Za-z0-9._@-]{1,64}", "flag"),
        _g("time_zone", r"[A-Za-z0-9_+/-]{1,64}", "value"),
    )
}

_URL_FORBIDDEN = frozenset({"Cc", "Zs", "Zl", "Zp"})
_BROWSE_PATH = re.compile(r"/browse/([A-Z][A-Z0-9_]*-[0-9]+)/?")


def normalize_ticket_key(text: str) -> str:
    """Case-insensitive input, upper-case output; non-ASCII input is refused before upper().

    ``str.upper`` maps some non-ASCII letters to ASCII ones (the long s, the sharp s), which
    would let ``\\u017fbc-1`` through as ``SBC-1``.
    """
    if isinstance(text, str) and text.isascii():
        return GRAMMARS["ticket_key"].check(text.upper())
    return GRAMMARS["ticket_key"].check(text if isinstance(text, str) else repr(text))


def segment(grammar: str, value: str) -> str:
    """Check ``value`` against the named grammar and percent-encode it with no safe characters."""
    checked = GRAMMARS[grammar].check(value)
    if checked in (".", ".."):
        # The provider-id grammars allow dots, and quote() leaves them: a dot segment would
        # climb the path, so it is refused like any other grammar failure.
        g = GRAMMARS[grammar]
        raise InputError(g.exit_code, g.code, g.reason, f"invalid {g.name}: {checked}")
    return quote(checked, safe="")


def query(params: Mapping[str, object] | Sequence[tuple[str, object]]) -> str:
    """The one query-string builder: ``urlencode`` with no safe characters."""
    pairs = params.items() if isinstance(params, Mapping) else params
    try:
        return urlencode([(str(k), str(v)) for k, v in pairs], safe="", quote_via=quote)
    except UnicodeEncodeError:
        raise _invalid_request("invalid_value", "query value is not valid text") from None


def _is_url_forbidden(ch: str) -> bool:
    return ord(ch) <= 0x20 or ord(ch) == 0x7F or unicodedata.category(ch) in _URL_FORBIDDEN


def split_url(url: str) -> SplitResult:
    """``urlsplit`` where a ValueError is exit 22 ``invalid_value``.

    A URL with a character at or below U+0020, U+007F or a Cc, Zs, Zl or Zp character is refused
    first: ``urlsplit`` silently strips tab, CR, LF and leading control characters.
    """
    if any(_is_url_forbidden(c) for c in url):
        shown, _ = cap_text(render(url), _EXCERPT)
        raise _invalid_request("invalid_value", f"invalid url: {shown}")
    try:
        return urlsplit(url)
    except ValueError:
        shown, _ = cap_text(render(url), _EXCERPT)
        raise _invalid_request("invalid_value", f"invalid url: {shown}") from None


def browse_key(path: str) -> str | None:
    """The key of a ``/browse/<KEY>`` path (``fullmatch``), else None."""
    m = _BROWSE_PATH.fullmatch(path)
    return m.group(1) if m else None


# --------------------------------------------------------------------------------------------
# Input file reader
# --------------------------------------------------------------------------------------------

_FORBIDDEN_ROOTS = ("/proc", "/sys", "/dev")
_OTHER_CAP = 1 << 20


class _ByteReader(Protocol):
    def read(self, size: int, /) -> bytes: ...


class _TextStdin(Protocol):
    @property
    def buffer(self) -> _ByteReader: ...

    def isatty(self) -> bool: ...


def is_forbidden_path(path: str) -> bool:
    """True for ``/proc``, ``/sys``, ``/dev`` and anything below them."""
    return any(path == root or path.startswith(root + "/") for root in _FORBIDDEN_ROOTS)


def _cap_for(kind: str, body_chars: int) -> int:
    if kind == "body":
        return 4 * body_chars
    if kind in ("title", "jql", "text"):
        return _OTHER_CAP
    raise ValueError(f"unknown input kind: {kind!r}")


def _file_error(path: str, why: str) -> InputError:
    shown, _ = cap_text(render(path), _EXCERPT)
    return _invalid_request("invalid_file", f"cannot read {shown}: {why}")


def _resolved_fd_path(fd: int, path: str) -> str:
    getpath = getattr(fcntl, "F_GETPATH", None)
    if getpath is not None:  # macOS
        try:
            raw = fcntl.fcntl(fd, getpath, b"\0" * 1024)
            return os.fsdecode(raw.split(b"\0", 1)[0])
        except OSError:
            pass
    try:  # Linux
        return os.readlink(f"/proc/self/fd/{fd}")
    except OSError:
        return os.path.realpath(path)


def open_input_fd(path: str) -> int:
    """Open ``path`` for reading under the Input files rules and return the descriptor.

    Raises InputError ``invalid_file`` for a symlink, a non-regular file or a path under
    ``/proc``, ``/sys`` or ``/dev``. The descriptor is blocking and close-on-exec.
    """
    if not isinstance(path, str) or not path or "\0" in path:
        raise _file_error(str(path), "invalid path")
    # lstat first: refuses a device or FIFO before open() could have a side effect on it.
    try:
        pre = os.lstat(path)
    except UnicodeError:
        raise _file_error(path, "invalid path") from None
    except OSError as exc:
        raise _file_error(path, os.strerror(exc.errno or 0) or "unreadable") from None
    if stat.S_ISLNK(pre.st_mode):
        raise _file_error(path, "symbolic links are refused")
    if not stat.S_ISREG(pre.st_mode):
        raise _file_error(path, "not a regular file")
    flags = os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW | os.O_CLOEXEC
    flags |= getattr(os, "O_NOCTTY", 0)
    try:
        fd = os.open(path, flags)
    except UnicodeError:
        raise _file_error(path, "invalid path") from None
    except OSError as exc:
        raise _file_error(path, os.strerror(exc.errno or 0) or "unreadable") from None
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode):
            raise _file_error(path, "not a regular file")
        if (info.st_dev, info.st_ino) != (pre.st_dev, pre.st_ino):
            raise _file_error(path, "file changed while opening")
        if is_forbidden_path(_resolved_fd_path(fd, path)):
            raise _file_error(path, "files under /proc, /sys and /dev are refused")
        fcntl.fcntl(fd, fcntl.F_SETFL, fcntl.fcntl(fd, fcntl.F_GETFL) & ~os.O_NONBLOCK)
        return fd
    except BaseException:
        os.close(fd)
        raise


def _decode(data: bytes, cap: int, label: str) -> str:
    if len(data) > cap:
        raise _file_error(label, "too large")
    try:
        return data.decode("utf-8", errors="strict")
    except UnicodeDecodeError:
        raise _file_error(label, "not valid UTF-8") from None


def read_input_file(path: str, *, kind: str, body_chars: int) -> str:
    """Read a file argument: regular file only, capped, strict UTF-8. No sanitizing here.

    ``kind`` is ``body`` (cap ``4 * body_chars`` bytes) or ``title``, ``jql`` or ``text``
    (1 MiB). Any failure is exit 22 ``invalid_file``; the message never carries file content.
    """
    cap = _cap_for(kind, body_chars)
    fd = open_input_fd(path)
    try:
        chunks: list[bytes] = []
        remaining = cap + 1
        while remaining > 0:
            try:
                chunk = os.read(fd, min(remaining, 1 << 16))
            except OSError as exc:
                raise _file_error(path, os.strerror(exc.errno or 0) or "unreadable") from None
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
    finally:
        os.close(fd)
    return _decode(b"".join(chunks), cap, path)


def read_input_stdin(stdin: _TextStdin | None, *, kind: str, body_chars: int) -> str:
    """Read ``-``: refused (exit 2 ``invalid_argument``) on a TTY, else the same caps."""
    cap = _cap_for(kind, body_chars)
    isatty = getattr(stdin, "isatty", None)
    if isatty is not None and isatty():
        raise _usage("invalid_argument", "stdin is a terminal; pipe the input or use a file")
    reader = getattr(stdin, "buffer", None)
    if reader is None:
        raise _usage("invalid_argument", "stdin is not available; use a file")
    data = reader.read(cap + 1)
    return _decode(data, cap, "stdin")


def read_input(path: str, *, kind: str, body_chars: int, stdin: _TextStdin | None = None) -> str:
    """``-`` reads stdin (``sys.stdin`` unless given); anything else is a file argument."""
    if path == "-":
        source = sys.stdin if stdin is None else stdin
        return read_input_stdin(source, kind=kind, body_chars=body_chars)
    return read_input_file(path, kind=kind, body_chars=body_chars)


def ensure_no_secret(*texts: str, secrets: Iterable[str] | None = None) -> None:
    """Exit 22 ``secret_in_body`` when any text holds a configured secret.

    A secret counts at 8 or more characters once stripped; its encoded forms are checked too.
    The message is fixed: it never echoes the value or says where it was found. With no
    ``secrets`` the values given to ``configure_scrubber`` are used.
    """
    if secrets is None:
        values = _configured_secrets()
    else:
        values = sorted({form for v in secrets for form in _secret_forms(v)})
    for text in texts:
        if any(v in text for v in values):
            raise _invalid_request(
                "secret_in_body", "the text contains a configured secret and was not sent"
            )
