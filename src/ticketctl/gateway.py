"""Gateway: the single boundary for subprocess and HTTP calls.

Every call to an external program (or HTTP endpoint) goes through this module so that
tests can substitute fakes (see design guide §14). Commands are always run with an argv
list, never through a shell.

The HTTP half implements docs/interface.md, "Invoking the provider" and "Reliability":

* ``http.client`` only, one fresh connection per request, an explicit connect phase (TCP, CONNECT
  tunnel and TLS handshake) so a failure before the first request byte is distinguishable from an
  ambiguous one. Each attempt runs in a worker thread; the main thread enforces the whole-command
  deadline, because a socket timeout does not bound a server that drips one byte at a time.
* Basic authentication is the only credential and is built from the site URL's own host: URLs are
  ``<site url> + path``, an absolute URL from a response is never followed, every 3xx is
  ``provider_changed`` (``outcome_unknown`` for a write: a 303 may follow a processed POST) and
  nothing is re-sent. No ``Accept-Encoding``, no protocol debug output,
  headers are never printed.
* The error text never contains an exception message (only its class name), a header value, a URL
  from a response or a proxy URL (proxies are shown as ``host:port``). Errors are raised with no
  chained exception so a traceback cannot reach request internals.

Public API: ``Target``, ``Deadline``, ``Gateway.request``, ``Response``, ``GatewayError``,
``parse_json``, ``select_proxy``/``describe_proxy``, ``backoff_delay``, ``run`` (subprocess) and
``run_hook``/``spawn_detached`` (hook processes: own session, capped output, group signals).
"""

from __future__ import annotations

import base64
import contextlib
import functools
import http.client
import ipaddress
import json
import os
import random
import re
import selectors
import signal
import socket
import ssl
import subprocess
import tempfile
import threading
import time
from collections.abc import Callable, Iterator, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from itertools import accumulate
from typing import TYPE_CHECKING, Any
from urllib.parse import unquote, urlsplit

from . import __version__
from .sanitize import render, scrub

if TYPE_CHECKING:
    from .config import Site

MAX_RESPONSE_BYTES = 16 * 1024 * 1024
MAX_JSON_DEPTH = 400
READ_RETRIES = 3
WRITE_RETRIES = 2
PACING_SLEEP_S = 1.0
RETRY_AFTER_CLAMP = 2**31
USER_AGENT = f"ticketctl/{__version__}"
LOOPBACK_HOSTS = frozenset({"127.0.0.1", "::1", "localhost"})
_METHODS = frozenset({"GET", "POST", "PUT", "DELETE"})
_READ_RETRY_STATUSES = frozenset({502, 503, 504})
_RATE_HEADERS = ("retry-after", "x-ratelimit-limit", "x-ratelimit-remaining")


# --------------------------------------------------------------------------------------------
# Errors
# --------------------------------------------------------------------------------------------

# ``errors.from_exception`` maps GatewayError onto the error envelope and exit code; ``exit_code``,
# ``code``, ``retryable``, ``outcome_unknown`` and ``retry_after_s`` are exactly the envelope's.


class GatewayError(Exception):
    """A failure of the HTTP boundary, carrying the interface's exit code and error code.

    ``message`` is fixed text: no exception message, header, response URL or proxy URL.
    ``retryable`` is the table's default; the caller makes ``outcome_unknown`` non-retryable for a
    ``comment`` or ``create`` without a dedupe key.
    """

    def __init__(
        self,
        code: str,
        message: str,
        *,
        exit_code: int = 12,
        retryable: bool | None = None,
        outcome_unknown: bool = False,
        retry_after_s: int | None = None,
        status: int | None = None,
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.exit_code = exit_code
        self.retryable = (
            code in {"timeout", "unavailable", "rate_limited", "outcome_unknown"}
            if retryable is None
            else retryable
        )
        self.outcome_unknown = outcome_unknown
        self.retry_after_s = retry_after_s
        self.status = status
        self.details: dict[str, Any] = dict(details or {})
        self.__suppress_context__ = True


def _internal(message: str) -> GatewayError:
    return GatewayError("internal", message, exit_code=1)


def _provider_changed(message: str, *, status: int | None = None, unknown: bool = False):
    return GatewayError("provider_changed", message, status=status, outcome_unknown=unknown)


# --------------------------------------------------------------------------------------------
# Target and deadline
# --------------------------------------------------------------------------------------------


class Deadline:
    """The whole-command clock; ``remaining`` never goes below zero."""

    def __init__(self, seconds: float, clock: Callable[[], float] = time.monotonic) -> None:
        self._clock = clock
        self._end = clock() + seconds
        self._pauses = 0  # nesting depth: only the outermost block extends the deadline
        self._paused_at = 0.0

    def remaining(self) -> float:
        return max(0.0, self._end - self._clock())

    @property
    def expired(self) -> bool:
        return self.remaining() <= 0.0

    @contextlib.contextmanager
    def paused(self) -> Iterator[None]:
        """The time spent inside the block does not count (a prompt, a hook run)."""
        if self._pauses == 0:
            self._paused_at = self._clock()
        self._pauses += 1
        try:
            yield
        finally:
            self._pauses -= 1
            if self._pauses == 0:
                self._end += max(0.0, self._clock() - self._paused_at)


_CONTROL = re.compile(r"[\x00-\x1f\x7f]")
_URL_BLANK = re.compile(r"[\s\x00-\x1f\x7f]")


def _bad_site_url(message: str) -> GatewayError:
    return GatewayError(
        "invalid_config", message, exit_code=2, details={"reason": "config_invalid"}
    )


@dataclass(frozen=True)
class Target:
    """One site as the gateway needs it: where to connect and which credential to send.

    The credential fields are excluded from ``repr`` so a traceback or a debug print of a target
    cannot show them.
    """

    name: str
    url: str = field(repr=False)
    email: str = field(repr=False)
    token: str = field(repr=False)
    connect_timeout: float
    read_timeout: float
    retry_after_max: float
    allow_http: bool = False

    def __post_init__(self) -> None:
        for value in (self.email, self.token):
            stripped = value.strip()
            if not stripped or _CONTROL.search(stripped):
                raise GatewayError(
                    "auth_required",
                    "the site's email or token is empty or contains control characters",
                )
        if ":" in self.email:
            raise _bad_site_url("the site's email must not contain a colon")
        object.__setattr__(self, "email", self.email.strip())
        object.__setattr__(self, "token", self.token.strip())
        object.__setattr__(self, "url", self.url.strip())
        if _URL_BLANK.search(self.url):
            raise _bad_site_url("the site url must not contain whitespace or control characters")
        try:
            parts = urlsplit(self.url)
            port = parts.port
            userinfo = parts.username is not None or parts.password is not None
        except ValueError:
            raise _bad_site_url("the site url is not a valid http(s) url") from None
        scheme = parts.scheme
        if scheme not in {"https", "http"} or not parts.hostname:
            raise _bad_site_url("the site url must be an http(s) url")
        if port == 0:
            raise _bad_site_url("the site url port must be between 1 and 65535")
        if userinfo or "@" in parts.netloc:
            raise _bad_site_url("the site url must not contain user information")
        if scheme == "http" and not (self.allow_http and parts.hostname in LOOPBACK_HOSTS):
            raise _bad_site_url("an http site url needs allow_http and a loopback host")

    @property
    def scheme(self) -> str:
        return urlsplit(self.url).scheme

    @property
    def host(self) -> str:
        return (urlsplit(self.url).hostname or "").lower()

    @property
    def port(self) -> int:
        port = urlsplit(self.url).port
        return port if port is not None else (443 if self.scheme == "https" else 80)

    @property
    def base_path(self) -> str:
        return urlsplit(self.url).path.rstrip("/")

    @property
    def host_port(self) -> str:
        """The host and port requested, as the request log shows them (default port omitted)."""
        shown = f"[{self.host}]" if ":" in self.host else self.host
        default = 443 if self.scheme == "https" else 80
        return shown if self.port == default else f"{shown}:{self.port}"

    @classmethod
    def from_site(
        cls, name: str, site: Site, env: Mapping[str, str], retry_after_max: float
    ) -> Target:
        """Resolve the credential variables of a configured site (``auth_required`` if empty)."""
        email = site.email
        if email is None:
            var = site.email_env or ""
            email = env.get(var, "")
            if not email.strip():
                raise GatewayError(
                    "auth_required",
                    f"the environment variable {var} is unset or empty",
                    details={"token_env": var},
                )
        token = env.get(site.token_env, "")
        if not token.strip():
            raise GatewayError(
                "auth_required",
                f"the environment variable {site.token_env} is unset or empty",
                details={"token_env": site.token_env},
            )
        return cls(
            name=name,
            url=site.url,
            email=email,
            token=token,
            connect_timeout=site.timeouts.connect.total_seconds(),
            read_timeout=site.timeouts.read.total_seconds(),
            retry_after_max=float(retry_after_max),
            allow_http=site.allow_http,
        )


# --------------------------------------------------------------------------------------------
# Proxies
# --------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class Proxy:
    host: str
    port: int
    auth_header: str | None = field(default=None, repr=False)


def describe_proxy(proxy: Proxy) -> str:
    """``host:port`` only: never the scheme, user information, path or query."""
    shown = f"[{proxy.host}]" if ":" in proxy.host else proxy.host
    return f"{shown}:{proxy.port}"


def _bad_proxy(variable: str) -> GatewayError:
    return GatewayError(
        "invalid_config",
        f"{variable} is not an http://host[:port] proxy url",
        exit_code=2,
        details={"reason": "config_invalid"},
    )


def _bypassed(no_proxy: str, host: str, port: int) -> bool:
    try:
        address: ipaddress.IPv4Address | ipaddress.IPv6Address | None = ipaddress.ip_address(host)
    except ValueError:
        address = None
    for raw in no_proxy.split(","):
        entry = raw.strip().lower()
        if not entry:
            continue
        if entry == "*":
            return True
        entry_port: int | None = None
        if entry.startswith("["):  # a bracketed IPv6 address, optionally ``[addr]:port``
            close = entry.find("]")
            rest = entry[close + 1 :] if close > 0 else "?"
            if rest:
                if not (rest.startswith(":") and rest[1:].isascii() and rest[1:].isdigit()):
                    continue
                entry_port = int(rest[1:])
            entry = entry[1:close]
        try:
            network = ipaddress.ip_network(entry, strict=False)
        except ValueError:
            network = None
        if network is not None:
            if entry_port is not None and entry_port != port:
                continue
            if address is not None and address.version == network.version and address in network:
                return True
            continue
        name = entry
        if entry_port is None and entry.count(":") == 1:
            name, _, port_text = entry.partition(":")
            if not port_text.isascii() or not port_text.isdigit():
                continue
            entry_port = int(port_text)
        if entry_port is not None and entry_port != port:
            continue
        name = name.removeprefix("*").lstrip(".")
        if name and (host == name or host.endswith("." + name)):
            return True
    return False


def select_proxy(env: Mapping[str, str], scheme: str, host: str, port: int) -> Proxy | None:
    """The CONNECT proxy for an https site from ``HTTPS_PROXY``/``https_proxy`` and ``NO_PROXY``.

    A loopback ``http`` site is never proxied. A malformed proxy value is exit 2
    ``invalid_config`` naming only the variable, never the value.
    """
    if scheme != "https":
        return None
    variable = "HTTPS_PROXY" if env.get("HTTPS_PROXY") else "https_proxy"
    value = (env.get(variable) or "").strip()
    if not value:
        return None
    if _bypassed(env.get("NO_PROXY") or env.get("no_proxy") or "", host.lower(), port):
        return None
    if re.search(r"[\x00-\x20\x7f-￿]", value):
        raise _bad_proxy(variable)
    try:
        parts = urlsplit(value)
        hostname, proxy_port = parts.hostname, parts.port
        user, password = parts.username, parts.password
    except ValueError:
        raise _bad_proxy(variable) from None
    if parts.scheme.lower() != "http" or not hostname:
        raise _bad_proxy(variable)
    if (
        proxy_port == 0
        or parts.netloc.endswith(":")
        or parts.path not in {"", "/"}
        or "?" in value
        or "#" in value
    ):
        raise _bad_proxy(variable)
    auth = None
    if user is not None:
        raw = f"{unquote(user)}:{unquote(password or '')}".encode()
        auth = "Basic " + base64.b64encode(raw).decode("ascii")
    return Proxy(hostname.lower(), 80 if proxy_port is None else proxy_port, auth)


# --------------------------------------------------------------------------------------------
# JSON
# --------------------------------------------------------------------------------------------

# Linear on any input: possessive repetition never backtracks, and an unterminated string (or one
# ending in a lone backslash) runs to the end of the input, which is invalid JSON anyway.
_STRING = re.compile(rb'"(?:[^"\\]++|\\.)*+(?:"|\\?\Z)', re.DOTALL)
_NON_BRACKETS = bytes(b for b in range(256) if b not in b"[]{}")
_DELTA = tuple(1 if chr(b) in "[{" else -1 if chr(b) in "]}" else 0 for b in range(256))


class _Syntax(Exception):
    """The body is not valid JSON (distinct from a depth violation)."""


def _max_depth(raw: bytes) -> int:
    """The deepest bracket nesting, with string contents skipped and no recursion."""
    brackets = _STRING.sub(b"", raw).translate(None, _NON_BRACKETS)
    return max(accumulate(map(_DELTA.__getitem__, brackets)), default=0)


def _reject_constant(_name: str) -> Any:
    raise ValueError("non-finite number")


def _loads(raw: bytes) -> Any:
    if _max_depth(raw) > MAX_JSON_DEPTH:
        raise _provider_changed("the response document is nested too deeply")
    try:
        return json.loads(raw.decode("utf-8"), parse_constant=_reject_constant)
    except (ValueError, RecursionError):
        raise _Syntax from None


def parse_json(raw: bytes) -> Any:
    """Parse a response document behind the depth guard; any failure is ``provider_changed``."""
    try:
        return _loads(raw)
    except _Syntax:
        raise _provider_changed("the response is not valid JSON") from None


# --------------------------------------------------------------------------------------------
# Retry helpers
# --------------------------------------------------------------------------------------------


def backoff_delay(n: int, rng: Callable[[], float]) -> float:
    """``min(0.5 s * 2^n, 8 s)`` with full jitter."""
    return min(0.5 * (2 ** min(n, 16)), 8.0) * rng()


def _parse_retry_after(value: str | None) -> int | None:
    """Delta-seconds only; an HTTP date or anything else is ignored (backoff applies)."""
    if value is None:
        return None
    text = value.strip()
    if re.fullmatch(r"[0-9]+", text, re.ASCII):
        # Clamped, so any value above the cap ends the command at once (and int() of a
        # 5000-digit header never runs).
        digits = text.lstrip("0") or "0"
        return RETRY_AFTER_CLAMP if len(digits) > 10 else min(int(digits), RETRY_AFTER_CLAMP)
    return None


def _parse_rate_limit(headers: Mapping[str, str | None]) -> dict[str, int] | None:
    limit, remaining = headers.get("x-ratelimit-limit"), headers.get("x-ratelimit-remaining")
    if limit is None or remaining is None:
        return None
    pattern = re.compile(r"[0-9]{1,12}", re.ASCII)
    if not (pattern.fullmatch(limit.strip()) and pattern.fullmatch(remaining.strip())):
        return None
    return {"limit": int(limit), "remaining": int(remaining)}


# --------------------------------------------------------------------------------------------
# One attempt (worker thread)
# --------------------------------------------------------------------------------------------


def default_ssl_context() -> ssl.SSLContext:
    """The default context: certificate and hostname verification on."""
    return ssl.create_default_context()


class _Attempt:
    """One connection and one request, run in a worker thread the main thread can cancel.

    ``sent`` flips (under ``lock``) immediately before the first request byte can be written; a
    cancellation that wins the lock first guarantees no byte is ever written, even if a slow DNS
    lookup or handshake finishes after the deadline.
    """

    def __init__(
        self,
        target: Target,
        proxy: Proxy | None,
        method: str,
        path: str,
        body: bytes | None,
        headers: Mapping[str, str],
        connect_timeout: float,
        read_timeout: float,
        context: ssl.SSLContext | None,
    ) -> None:
        self.target, self.proxy = target, proxy
        self.method, self.path, self.body = method, path, body
        self.headers = dict(headers)
        self.connect_timeout, self.read_timeout = connect_timeout, read_timeout
        self.context = context
        self.lock = threading.Lock()
        self.done = threading.Event()
        self.cancelled = False
        self.sent = False
        self.abandoned = False  # the deadline won: nothing the worker records afterwards counts
        self.conn: http.client.HTTPConnection | None = None
        self.thread: threading.Thread | None = None
        # Outcome. ``failure`` is a category (or None when a response arrived).
        self.failure: str | None = None
        self.exc_name = ""
        self.stage = "connect"
        self.status: int | None = None
        self.payload = b""
        self.response_headers: dict[str, str | None] = {}

    def start(self) -> None:
        self.thread = threading.Thread(target=self.run, name="gateway-attempt", daemon=True)
        self.thread.start()

    def cancel(self) -> bool:
        """Cancel the attempt; return whether a request byte may already have been written."""
        with self.lock:
            self.cancelled = True
            sent = self.sent
        self._close()
        return sent

    def _close(self) -> None:
        conn = self.conn
        if conn is None:
            return
        sock = conn.sock
        if sock is not None:
            try:
                sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
        try:
            conn.close()
        except OSError:
            pass

    def _connection(self) -> http.client.HTTPConnection:
        target = self.target
        if target.scheme != "https":
            return http.client.HTTPConnection(
                target.host, target.port, timeout=self.connect_timeout
            )
        context = self.context or default_ssl_context()
        if self.proxy is None:
            return http.client.HTTPSConnection(
                target.host, target.port, timeout=self.connect_timeout, context=context
            )
        proxy = self.proxy
        conn = http.client.HTTPSConnection(
            proxy.host, proxy.port, timeout=self.connect_timeout, context=context
        )
        tunnel_headers = {"Proxy-Authorization": proxy.auth_header} if proxy.auth_header else {}
        conn.set_tunnel(target.host, target.port, headers=tunnel_headers)
        return conn

    def run(self) -> None:
        try:
            self._run()
        except BaseException as exc:  # a worker must always report, whatever happened
            self._classify(exc)
        finally:
            self._close()
            self.done.set()

    def _run(self) -> None:
        with self.lock:
            if self.cancelled:
                self.failure = "cancelled"
                return
        conn = self._connection()
        self.conn = conn
        conn.connect()  # TCP, CONNECT tunnel and TLS handshake: nothing of the request yet
        if conn.sock is None:
            raise OSError
        conn.sock.settimeout(self.read_timeout)
        # Building only buffers: a path or header that http.client refuses fails here, before the
        # first request byte can exist, so it is never an ambiguous outcome.
        self.stage = "build"
        conn.putrequest(self.method, self.path, skip_accept_encoding=True)
        for name, value in self.headers.items():
            conn.putheader(name, value)
        with self.lock:
            if self.cancelled:
                self.failure = "cancelled"
                return
            self.sent = True
        self.stage = "send"
        conn.endheaders(self.body)
        self.stage = "response"
        response = conn.getresponse()
        if not 200 <= response.status <= 599:  # 101, 103 and the like: never a final answer
            self.failure = "malformed"
            return
        self.status = response.status
        self.response_headers = {name: response.getheader(name) for name in _RATE_HEADERS}
        if 300 <= response.status < 400:
            return  # never read, never follow
        length_text = response.getheader("Content-Length")
        declared: int | None = None
        if length_text is not None:
            if not re.fullmatch(r"[0-9]{1,18}", length_text.strip(), re.ASCII):
                self.failure = "malformed"
                return
            declared = int(length_text)
            if declared > MAX_RESPONSE_BYTES:
                self.failure = "too_big"
                return
        self.stage = "body"
        chunks: list[bytes] = []
        total = 0
        while chunk := response.read(65536):
            total += len(chunk)
            if total > MAX_RESPONSE_BYTES:
                self.failure = "too_big"
                return
            chunks.append(chunk)
        if declared is not None and not response.chunked and total != declared:
            self.failure = "conn"
            self.exc_name = "IncompleteRead"
            return
        self.payload = b"".join(chunks)

    def _classify(self, exc: BaseException) -> None:
        self.exc_name = type(exc).__name__
        if self.stage == "build":
            self.failure = "internal"
        elif isinstance(exc, ssl.SSLCertVerificationError):
            self.failure = "tls_verify"
        elif isinstance(exc, TimeoutError) or (
            isinstance(exc, ssl.SSLError) and "timed out" in str(exc)
        ):
            self.failure = "timeout"
        elif isinstance(exc, http.client.IncompleteRead | http.client.BadStatusLine):
            self.failure = "conn"
        elif isinstance(exc, http.client.HTTPException):
            self.failure = "malformed"
        elif isinstance(exc, OSError):
            self.failure = "conn"
        else:
            self.failure = "internal"
        if self.cancelled:
            self.failure = "cancelled"


# --------------------------------------------------------------------------------------------
# The gateway
# --------------------------------------------------------------------------------------------

_PCHAR = r"(?:[A-Za-z0-9._~!$&'()*+,;=:@-]|%[0-9A-Fa-f]{2})"
_PATH = re.compile(rf"(?:/{_PCHAR}+)+")
_QUERY = re.compile(rf"(?:{_PCHAR}|[/?])*")
# ``sanitize.segment`` encodes all of these; the gateway refuses them again as a cheap second line.
_PATH_FORBIDDEN = re.compile(r";|%2f|%5c", re.IGNORECASE)
_TEMPLATE = re.compile(r"(?:/(?:[A-Za-z0-9._~-]+|\{[a-z_]+\}))+")


@dataclass(frozen=True)
class Response:
    """A completed exchange: any 2xx or non-retryable 4xx, parsed and scrubbed.

    ``data`` is the parsed JSON document (None for an empty or non-JSON error body); ``text`` is
    the scrubbed body text of a non-2xx answer (empty for a parsed 2xx). ``rate_limit`` holds the
    ``limit`` and ``remaining`` of the response when both headers were valid.
    """

    status: int
    data: Any
    text: str
    rate_limit: dict[str, int] | None
    retries: int


class Gateway:
    """The HTTP boundary for one site; one instance per command (pacing state is per command).

    ``log_sink`` receives each request-log line (the schema of schemas/log.json); the CLI wires it
    to ``audit.append_log`` (``runtime.open_gateway``). The default drops lines.
    """

    def __init__(
        self,
        target: Target,
        *,
        deadline: Deadline,
        env: Mapping[str, str] | None = None,
        ssl_context: ssl.SSLContext | None = None,
        log_sink: Callable[[dict[str, Any]], None] | None = None,
        verbose: Callable[[str], None] | None = None,
        config_sha256: str = "0" * 64,
        sleep: Callable[[float], None] = time.sleep,
        rng: Callable[[], float] = random.random,
    ) -> None:
        self.target = target
        self.deadline = deadline
        self._env: Mapping[str, str] = os.environ if env is None else env
        self._context = ssl_context
        self._log_sink = log_sink
        self._verbose = verbose
        self._config_sha256 = config_sha256
        self._sleep = sleep
        self._rng = rng
        # Pacing state is shared by the threads of a multi-key command; every access holds the
        # lock. ``_hold_until`` (monotonic) is the end of a pacing or Retry-After sleep in
        # progress: a request of another thread waits for it too. It is None when nobody sleeps.
        self._lock = threading.Lock()
        self._pace_pending = False
        self._hold_until: float | None = None
        self.warnings: list[str] = []

    # request ---------------------------------------------------------------------------------
    def request(
        self,
        method: str,
        path: str,
        *,
        template: str,
        query: str = "",
        body: object | None = None,
        write: bool | None = None,
    ) -> Response:
        """Send one request with the retry table applied; raise ``GatewayError`` on failure.

        ``path`` and ``query`` come from ``sanitize.segment`` and ``sanitize.query``. ``write``
        defaults to "every method but GET"; a read that needs a POST (search, parse, count) passes
        ``write=False``.
        """
        if method not in _METHODS:
            raise _internal("unsupported method")
        if (
            not _PATH.fullmatch(path)
            or _PATH_FORBIDDEN.search(path)
            or any(
                unquote(part).startswith("..") or unquote(part) == "." for part in path.split("/")
            )
        ):
            raise _internal("the request path is not a clean absolute path")
        if query and (query.startswith("?") or not _QUERY.fullmatch(query)):
            raise _internal("the request query is not encoded")
        if len(template) > 256 or not _TEMPLATE.fullmatch(template):
            raise _internal("the log path template is invalid")
        is_write = method != "GET" if write is None else write
        try:
            payload = (
                None
                if body is None
                else json.dumps(
                    body, ensure_ascii=True, separators=(",", ":"), allow_nan=False
                ).encode()
            )
        except (TypeError, ValueError, RecursionError):
            raise _internal("the request body cannot be encoded as JSON") from None
        target = self.target
        headers = {
            "Authorization": "Basic " + _basic(target),
            "User-Agent": USER_AGENT,
            "Accept": "application/json",
            "Connection": "close",
        }
        if payload is not None:
            headers["Content-Type"] = "application/json"
        if payload is not None or method in {"POST", "PUT"}:
            headers["Content-Length"] = str(len(payload or b""))
        url_path = target.base_path + path + (f"?{query}" if query else "")
        proxy = select_proxy(self._env, target.scheme, target.host, target.port)
        call = _Call(method, template, is_write)
        started = time.monotonic()
        try:
            return self._run(call, proxy, url_path, payload, headers)
        except GatewayError as err:
            if call.rate_limit is not None:
                err.details.setdefault("rate_limit", call.rate_limit)
            raise
        finally:
            self._record(call, int((time.monotonic() - started) * 1000))

    # internals -------------------------------------------------------------------------------
    def _run(
        self,
        call: _Call,
        proxy: Proxy | None,
        url_path: str,
        payload: bytes | None,
        headers: dict[str, str],
    ) -> Response:
        max_retries = WRITE_RETRIES if call.write else READ_RETRIES
        while True:
            self._check_deadline()
            self._pace()
            attempt = self._attempt(call, proxy, url_path, payload, headers)
            status = None if attempt.abandoned else attempt.status
            call.status = status
            if not attempt.abandoned:
                call.note_rate(attempt.response_headers)
            # A status that already decides the outcome beats a failure to read its body.
            if status is not None and (
                attempt.failure is None or self._status_decides(call, status)
            ):
                if 300 <= status < 400:
                    # a 303 may follow a processed POST, so a write's outcome is unknown
                    raise _provider_changed(
                        "the provider answered with a redirect", status=status, unknown=call.write
                    )
                if status == 401:
                    raise GatewayError(
                        "bad_credentials", "the provider rejected the credentials", status=401
                    )
                if status == 403:
                    raise GatewayError("forbidden", "the provider refused the request", status=403)
                retry_after = _parse_retry_after(attempt.response_headers.get("retry-after"))
                if status == 429 or (not call.write and status in _READ_RETRY_STATUSES):
                    self._wait_for_retry(call, status, retry_after, max_retries)
                    continue
                if status >= 500:
                    if call.write:
                        raise GatewayError(
                            "outcome_unknown",
                            "the provider failed after the request was sent",
                            outcome_unknown=True,
                            status=status,
                        )
                    raise GatewayError(
                        "unavailable",
                        "the provider is unavailable",
                        status=status,
                        retry_after_s=retry_after,
                    )
                return self._response(attempt, call)
            self._handle_failure(call, attempt, proxy, max_retries)

    def _status_decides(self, call: _Call, status: int) -> bool:
        return (
            300 <= status < 400
            or status in {401, 403, 429}
            or (not call.write and status in _READ_RETRY_STATUSES)
        )

    def _wait_for_retry(
        self, call: _Call, status: int, retry_after: int | None, max_retries: int
    ) -> None:
        """Sleep before a retry or raise the terminal error for this status."""

        def terminal() -> GatewayError:
            if status == 429:
                return GatewayError(
                    "rate_limited",
                    "the provider rate limit was hit",
                    status=429,
                    retry_after_s=retry_after,
                )
            return GatewayError(
                "unavailable",
                "the provider is unavailable",
                status=status,
                retry_after_s=retry_after,
            )

        remaining = self.deadline.remaining()
        if retry_after is not None:
            if retry_after > self.target.retry_after_max or retry_after > remaining:
                raise terminal()
        if call.retries >= max_retries:
            raise terminal()
        delay = (
            float(retry_after)
            if retry_after is not None
            else backoff_delay(call.retries, self._rng)
        )
        if delay > remaining:
            raise GatewayError("timeout", "the command timed out while waiting to retry")
        self._sleep_for_retry(call, delay)

    def _sleep_for_retry(self, call: _Call, delay: float) -> None:
        until = self._hold(delay)
        self._sleep(delay)
        call.retries += 1
        with self._lock:
            self._pace_pending = False
        self._release(until)

    def _hold(self, delay: float) -> float:
        """Open (or extend) the shared sleep window by ``delay`` seconds; returns its end."""
        with self._lock:
            until = max(time.monotonic() + delay, self._hold_until or 0.0)
            self._hold_until = until
            return until

    def _release(self, until: float) -> None:
        with self._lock:
            if self._hold_until == until:
                self._hold_until = None

    def _handle_failure(
        self, call: _Call, attempt: _Attempt, proxy: Proxy | None, max_retries: int
    ) -> None:
        category = "deadline" if attempt.abandoned else attempt.failure
        sent = attempt.sent
        unknown = call.write and sent
        # A 4xx means nothing was applied, so an unusable body after one is not ambiguous.
        status = attempt.status
        body_unknown = unknown and not (status is not None and 400 <= status < 500)
        if category in {"cancelled", "deadline"}:
            if unknown:
                raise GatewayError(
                    "outcome_unknown",
                    "the command timed out after the request was sent",
                    outcome_unknown=True,
                )
            raise GatewayError("timeout", "the command timed out")
        if category in {"too_big", "malformed"}:
            what = "is too large" if category == "too_big" else "is malformed"
            raise _provider_changed(f"the provider's response {what}", unknown=body_unknown)
        if category == "internal":
            raise GatewayError(
                "internal",
                f"internal error while sending ({attempt.exc_name})",
                exit_code=1,
                outcome_unknown=unknown,
            )
        if category == "tls_verify":
            raise GatewayError(
                "unavailable", f"TLS verification failed ({attempt.exc_name})", retryable=False
            )
        if body_unknown:
            raise GatewayError(
                "outcome_unknown",
                f"the connection failed after the request was sent ({attempt.exc_name})",
                outcome_unknown=True,
            )
        if unknown:
            # A 4xx whose body could not be read: nothing was applied, and a sent write is never
            # re-sent.
            raise _provider_changed(
                f"the provider's response could not be read ({attempt.exc_name})", status=status
            )
        where = ""
        if proxy is not None and attempt.stage == "connect":
            where = f" through the proxy {describe_proxy(proxy)}"
        if call.retries >= max_retries:
            if not call.write and category == "timeout":
                raise GatewayError("timeout", f"the provider did not answer in time{where}")
            raise GatewayError(
                "unavailable", f"could not reach the provider{where} ({attempt.exc_name})"
            )
        delay = backoff_delay(call.retries, self._rng)
        if delay > self.deadline.remaining():
            raise GatewayError("timeout", "the command timed out while waiting to retry")
        self._sleep_for_retry(call, delay)

    def _attempt(
        self,
        call: _Call,
        proxy: Proxy | None,
        url_path: str,
        payload: bytes | None,
        headers: dict[str, str],
    ) -> _Attempt:
        target = self.target
        attempt = _Attempt(
            target,
            proxy,
            call.method,
            url_path,
            payload,
            headers,
            min(target.connect_timeout, max(self.deadline.remaining(), 0.001)),
            target.read_timeout,
            self._context,
        )
        attempt.start()
        if not attempt.done.wait(self.deadline.remaining()):
            # The worker is a daemon and cannot write after this: no wait, so the deadline holds.
            attempt.cancel()
            attempt.abandoned = True
            attempt.failure = "deadline"
            attempt.status = None
        return attempt

    def _check_deadline(self) -> None:
        if self.deadline.expired:
            raise GatewayError("timeout", "the command timed out before the request was sent")

    def _pace(self) -> None:
        with self._lock:
            pending = self._pace_pending
            self._pace_pending = False
            held = self._hold_until
        remaining = self.deadline.remaining()
        if pending and remaining > PACING_SLEEP_S:
            until = self._hold(PACING_SLEEP_S)
            self._sleep(PACING_SLEEP_S)
            self._release(until)
        elif held is not None:
            wait = held - time.monotonic()
            if wait >= remaining:
                # Another thread's hold outlasts this command: never send during it.
                raise GatewayError("timeout", "the command timed out while waiting for a hold")
            if wait > 0:
                self._sleep(wait)  # another thread is sleeping for pacing or a Retry-After

    def _response(self, attempt: _Attempt, call: _Call) -> Response:
        status = attempt.status or 0
        raw = attempt.payload
        ok = 200 <= status < 300
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError:
            if ok:
                raise _provider_changed(
                    "the response is not valid text", status=status, unknown=call.write
                ) from None
            return Response(
                status, None, scrub(raw.decode("utf-8", "replace")), call.rate_limit, call.retries
            )
        data: Any = None
        if text.strip():
            try:
                data = _loads(text.encode("utf-8"))
            except _Syntax:
                if ok:
                    raise _provider_changed(
                        "the response is not valid JSON", status=status, unknown=call.write
                    ) from None
            except GatewayError as err:
                err.outcome_unknown = call.write and not 400 <= status < 500
                err.status = status
                raise
        # Parse first, scrub after: a replacement inside the raw text could break the structure.
        data = _scrub_tree(data)
        text = "" if ok and data is not None else scrub(text)
        return Response(status, data, text, call.rate_limit, call.retries)

    def _record(self, call: _Call, duration_ms: int) -> None:
        target = self.target
        remaining = call.remaining
        line: dict[str, Any] = {
            "v": 1,
            "ts": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "site": target.name,
            "method": call.method,
            "path": call.template,
            "status": call.status,
            "duration_ms": duration_ms,
            "retries": call.retries,
            "rate_limit_remaining": remaining,
            "host": target.host_port,
            "config_sha256": self._config_sha256,
        }
        if self._log_sink is not None:
            try:
                self._log_sink(line)
            except Exception:  # a failed log write warns and never fails the command
                self.warnings.append("log_write_failed")
        if self._verbose is not None:
            shown = "-" if call.status is None else str(call.status)
            limit = None if call.rate_limit is None else call.rate_limit["limit"]
            self._verbose(
                render(
                    f"{call.method} {call.template} status={shown} duration_ms={duration_ms} "
                    f"retries={call.retries} rate_limit_remaining={remaining} "
                    f"rate_limit_limit={limit}"
                )
            )
        if call.rate_limit and call.rate_limit["remaining"] * 10 < call.rate_limit["limit"]:
            with self._lock:  # a late healthy answer never clears what another thread set
                self._pace_pending = True


@dataclass
class _Call:
    method: str
    template: str
    write: bool
    retries: int = 0
    status: int | None = None
    rate_limit: dict[str, int] | None = None
    remaining: int | None = None

    def note_rate(self, headers: Mapping[str, str | None]) -> None:
        text = (headers.get("x-ratelimit-remaining") or "").strip()
        if re.fullmatch(r"[0-9]{1,12}", text, re.ASCII):
            self.remaining = int(text)
        parsed = _parse_rate_limit(headers)
        if parsed is not None:
            self.rate_limit = parsed


def _scrub_tree(node: Any) -> Any:
    """Scrub every string (keys and values) of a parsed document; depth is capped at 400."""
    if isinstance(node, str):
        return scrub(node)
    if isinstance(node, list):
        return [_scrub_tree(item) for item in node]
    if isinstance(node, dict):
        return {scrub(key): _scrub_tree(value) for key, value in node.items()}
    return node


def _basic(target: Target) -> str:
    raw = f"{target.email.strip()}:{target.token.strip()}".encode()
    return base64.b64encode(raw).decode("ascii")


# --------------------------------------------------------------------------------------------
# Subprocesses
# --------------------------------------------------------------------------------------------


def run(
    argv: list[str],
    *,
    stdin: str | None = None,
    timeout: float | None = None,
) -> subprocess.CompletedProcess[str]:
    """Run ``argv`` without a shell and capture its text output."""
    return subprocess.run(
        argv,
        input=stdin,
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )


# --------------------------------------------------------------------------------------------
# Hook processes
# --------------------------------------------------------------------------------------------

HOOK_OUTPUT_CAP = 65536  # bytes kept of each of a hook's stdout and stderr
HOOK_KILL_GRACE = 2.0  # seconds between SIGTERM and SIGKILL after a timeout
HOOK_DRAIN = 0.25  # seconds a hook's pipes may stay open after its leader exited
_POLL = 0.02  # wake-up interval where no pidfd can signal the leader's exit

# ``waitid`` with ``WNOWAIT`` observes an exit without reaping, so the group id cannot be reused
# before the group is signaled. It does not exist on macOS; ``Popen.poll`` is the fallback there.
_HAS_WAITID = hasattr(os, "waitid") and hasattr(os, "WNOWAIT")


@dataclass
class HookRun:
    """What a finished hook process left: its exit and capped output.

    ``returncode`` is negative for a death by signal. ``pid`` is the process group id too (the
    hook starts its own session).
    """

    pid: int
    returncode: int
    stdout: bytes
    stderr: bytes
    stdout_truncated: bool
    stderr_truncated: bool
    timed_out: bool


def _pidfd_open(pid: int) -> int | None:
    opener = getattr(os, "pidfd_open", None)
    if opener is None:
        return None
    try:
        return int(opener(pid))
    except OSError:  # old kernel, seccomp, or a process that is already gone
        return None


def _leader_exited(proc: subprocess.Popen[bytes]) -> bool:
    """True once the leader has exited. With ``waitid`` it stays unreaped (a zombie keeps the
    process group id from being reused until the group has been signaled)."""
    if _HAS_WAITID:
        try:
            return os.waitid(os.P_PID, proc.pid, os.WEXITED | os.WNOHANG | os.WNOWAIT) is not None
        except ChildProcessError:
            return True
    return proc.poll() is not None


def _signal_group(pgid: int, sig: int) -> None:
    """Signal the group this module created (``pgid`` is the pid recorded at launch)."""
    try:
        os.killpg(pgid, sig)
    except (ProcessLookupError, PermissionError):
        pass


def _close(fd: int) -> None:
    try:
        os.close(fd)
    except OSError:
        pass


_SIGNALS = {"armed": False, "pending": 0}


def _on_terminate(signum: int, _frame: object) -> None:
    """SIGTERM and SIGHUP while a hook runs: unwind like SIGINT so the ``finally`` kills the
    group. Once the cleanup has begun (disarmed) the signal is only remembered, never raised, so
    a second signal cannot skip the reaping; it is re-sent after the handlers are restored."""
    if _SIGNALS["armed"]:
        _SIGNALS["armed"] = False  # disarm first: a later signal is only recorded, never raised
        raise KeyboardInterrupt
    _SIGNALS["pending"] = signum


def _disarm() -> None:
    _SIGNALS["armed"] = False


@contextlib.contextmanager
def _hook_signals() -> Iterator[None]:
    """For the duration of a hook run (main thread only): SIGTERM and SIGHUP raise
    ``KeyboardInterrupt``, and an ignored SIGCHLD becomes the default one (an ignored SIGCHLD makes
    the kernel reap the child, so its exit status, a veto included, would be lost). Everything is
    restored in a ``finally`` that tolerates each restoration failing."""
    if threading.current_thread() is not threading.main_thread():
        if signal.getsignal(signal.SIGCHLD) is signal.SIG_IGN:
            raise OSError("SIGCHLD is ignored: a hook's exit status cannot be read")
        yield
        return
    saved: dict[int, Any] = {}
    _SIGNALS["pending"] = 0
    try:
        for sig in (signal.SIGTERM, signal.SIGHUP):
            saved[sig] = signal.signal(sig, _on_terminate)
        if signal.getsignal(signal.SIGCHLD) is signal.SIG_IGN:
            saved[signal.SIGCHLD] = signal.signal(signal.SIGCHLD, signal.SIG_DFL)
        _SIGNALS["armed"] = True
        yield
    finally:
        _disarm()
        for sig, old in saved.items():
            with contextlib.suppress(ValueError, OSError, TypeError):
                signal.signal(sig, signal.SIG_DFL if old is None else old)
        pending, _SIGNALS["pending"] = _SIGNALS["pending"], 0
        if pending:
            os.kill(os.getpid(), pending)  # the interrupted command ends as the signal asks


def _guarded[**P, R](func: Callable[P, R]) -> Callable[P, R]:
    @functools.wraps(func)
    def wrapper(*args: P.args, **kwargs: P.kwargs) -> R:
        with _hook_signals():
            return func(*args, **kwargs)

    return wrapper


@_guarded
def run_hook(
    argv: list[str],
    *,
    stdin: bytes,
    env: Mapping[str, str],
    timeout: float,
    out_cap: int = HOOK_OUTPUT_CAP,
    err_cap: int = HOOK_OUTPUT_CAP,
    kill_grace: float = HOOK_KILL_GRACE,
    drain: float = HOOK_DRAIN,
    on_start: Callable[[int], None] | None = None,
) -> HookRun:
    """Run ``argv`` (never through a shell of this module's making) as a hook.

    The child starts its own session, so its pid is its process group id. It gets exactly ``env``,
    ``stdin`` on a pipe (written without blocking this loop, a child that never reads cannot hang
    it) and capped stdout and stderr (output past the cap is read and dropped so the child never
    blocks on a full pipe). ``on_start`` receives the pid right after the launch.

    A timeout sends SIGTERM to the group, then SIGKILL after ``kill_grace``. Whatever the exit,
    the group is SIGKILLed before the leader is reaped: no descendant of a finished hook is left
    running, and no process this call did not start is signaled (the group id belongs to the
    unreaped leader until then). Every exit path, an interrupt included, reaps the leader.
    """
    pidfd: int | None = None  # state the cleanup needs is bound before the launch, so that
    sel: selectors.BaseSelector | None = None  # no statement sits between Popen and the ``try``
    out = bytearray()
    err = bytearray()
    truncated = {"out": False, "err": False}
    timed_out = False
    proc = subprocess.Popen(  # noqa: S603 - argv list, no shell=True
        argv,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=dict(env),
        close_fds=True,
        start_new_session=True,
        shell=False,
    )
    try:  # everything after the launch is inside: any failure kills and reaps the group
        pgid = proc.pid
        if on_start is not None:
            on_start(pgid)
        sel = selectors.DefaultSelector()
        pidfd = _pidfd_open(pgid)
        assert proc.stdin is not None and proc.stdout is not None and proc.stderr is not None
        for stream in (proc.stdin, proc.stdout, proc.stderr):
            os.set_blocking(stream.fileno(), False)
        sel.register(proc.stdout, selectors.EVENT_READ, "out")
        sel.register(proc.stderr, selectors.EVENT_READ, "err")
        pending = memoryview(stdin)
        if pending:
            sel.register(proc.stdin, selectors.EVENT_WRITE, "in")
        else:
            proc.stdin.close()
        if pidfd is not None:
            sel.register(pidfd, selectors.EVENT_READ, "pid")

        deadline = time.monotonic() + timeout
        exited_at: float | None = None
        while True:
            now = time.monotonic()
            if exited_at is None and _leader_exited(proc):
                exited_at = now
            pipes_open = any(k.data != "pid" for k in sel.get_map().values())
            if exited_at is not None and (not pipes_open or now - exited_at >= drain):
                break
            if now >= deadline:
                timed_out = exited_at is None
                break
            wait = deadline - now
            if exited_at is not None:
                wait = min(wait, exited_at + drain - now)
            if pidfd is None or exited_at is not None:
                wait = min(wait, _POLL)
            for key, _mask in sel.select(max(wait, 0)):
                if key.data == "pid":
                    sel.unregister(pidfd)  # type: ignore[arg-type]
                    continue
                if key.data == "in":
                    try:
                        sent = os.write(proc.stdin.fileno(), pending[:65536])  # type: ignore[union-attr]
                        pending = pending[sent:]
                    except BlockingIOError:
                        continue
                    except OSError:  # EPIPE: the child stopped reading
                        pending = pending[len(pending) :]
                    if not pending:
                        sel.unregister(proc.stdin)  # type: ignore[arg-type]
                        proc.stdin.close()  # type: ignore[union-attr]
                    continue
                stream = proc.stdout if key.data == "out" else proc.stderr
                try:
                    chunk = os.read(stream.fileno(), 65536)  # type: ignore[union-attr]
                except BlockingIOError:
                    continue
                except OSError:
                    chunk = b""
                if not chunk:
                    sel.unregister(stream)  # type: ignore[arg-type]
                    continue
                buf, cap = (out, out_cap) if key.data == "out" else (err, err_cap)
                room = cap - len(buf)
                if len(chunk) > room:
                    truncated[key.data] = True
                buf.extend(chunk[: max(room, 0)])
        if timed_out:
            _signal_group(pgid, signal.SIGTERM)
            stop = time.monotonic() + kill_grace
            while not _leader_exited(proc) and time.monotonic() < stop:
                time.sleep(_POLL)
    finally:
        # The one exit: kill what is left of the group while the leader is still unreaped, then
        # reap it. Runs for an interrupt too.
        _disarm()
        try:
            _signal_group(proc.pid, signal.SIGKILL)
            proc.wait()
        finally:
            if sel is not None:
                sel.close()
            if pidfd is not None:
                _close(pidfd)
            for stream in (proc.stdin, proc.stdout, proc.stderr):
                if stream is not None:
                    try:
                        stream.close()
                    except OSError:
                        pass
    return HookRun(
        pid=proc.pid,
        returncode=proc.returncode,
        stdout=bytes(out),
        stderr=bytes(err),
        stdout_truncated=truncated["out"],
        stderr_truncated=truncated["err"],
        timed_out=timed_out,
    )


@_guarded
def spawn_detached(
    argv: list[str],
    *,
    stdin: bytes,
    env: Mapping[str, str],
    on_start: Callable[[int], None] | None = None,
) -> int:
    """Start ``argv`` in its own session with no output and no wait (an ``async`` post hook).

    The payload is handed over on an anonymous file, so a hook that never reads cannot block the
    caller. A daemon thread reaps the child, so it never becomes a zombie of a long-lived caller.
    There is no timeout: the caller does not outlive its reach over the process.
    """
    proc: subprocess.Popen[bytes] | None = None
    reaper_started = False
    try:
        with tempfile.TemporaryFile() as source:
            source.write(stdin)
            source.flush()
            source.seek(0)
            proc = subprocess.Popen(  # noqa: S603 - argv list, no shell=True
                argv,
                stdin=source,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                env=dict(env),
                close_fds=True,
                start_new_session=True,
                shell=False,
            )
        threading.Thread(target=proc.wait, daemon=True).start()  # first: on_start may raise
        reaper_started = True
        _disarm()  # the child has its watcher: a later signal is only remembered
    finally:
        if proc is not None and not reaper_started:  # no watcher will reap it: do it here
            try:
                _signal_group(proc.pid, signal.SIGKILL)
            finally:
                proc.wait()
    assert proc is not None
    if on_start is not None:
        on_start(proc.pid)
    return proc.pid
