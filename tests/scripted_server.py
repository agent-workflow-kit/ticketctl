"""A tiny scripted loopback HTTP server for the gateway tests.

This is not the fake provider of issue #10: it knows no Jira. Each accepted connection runs the
next behavior of a script (the last one repeats); a behavior gets the parsed request and the raw
socket, so a test can answer normally, drip bytes, drop the connection or lie about lengths.

Safety: binds 127.0.0.1 on an ephemeral port only, runs in threads of this process, and tears down
exactly what it started (the listener, its accept thread and the sockets it accepted).
"""

from __future__ import annotations

import socket
import ssl
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field

CRLF = b"\r\n"


@dataclass
class Request:
    method: str
    target: str
    headers: dict[str, list[str]]
    body: bytes
    raw: bytes

    def header(self, name: str) -> str | None:
        values = self.headers.get(name.lower())
        return values[0] if values else None


@dataclass
class Conn:
    sock: socket.socket
    request: Request
    server: ScriptedServer = field(repr=False)

    def send(self, data: bytes) -> None:
        self.sock.sendall(data)


Behavior = Callable[[Conn], None]


def response_bytes(
    status: int = 200,
    body: bytes | str = b"",
    headers: dict[str, str] | None = None,
    *,
    content_length: int | None = None,
    reason: str = "X",
) -> bytes:
    data = body.encode() if isinstance(body, str) else body
    lines = [f"HTTP/1.1 {status} {reason}"]
    sent = dict(headers or {})
    if not any(k.lower() == "content-length" for k in sent):
        sent["Content-Length"] = str(len(data) if content_length is None else content_length)
    for key, value in sent.items():
        lines.append(f"{key}: {value}")
    return ("\r\n".join(lines) + "\r\n\r\n").encode() + data


def reply(
    status: int = 200,
    body: bytes | str = b"",
    headers: dict[str, str] | None = None,
    **kw: int | str | None,
) -> Behavior:
    def behavior(conn: Conn) -> None:
        conn.send(response_bytes(status, body, headers, **kw))  # type: ignore[arg-type]

    return behavior


def json_reply(text: str = "{}", status: int = 200, headers: dict[str, str] | None = None):
    merged = {"Content-Type": "application/json"}
    merged.update(headers or {})
    return reply(status, text, merged)


def drop(conn: Conn) -> None:
    """Read the request, then close without a single response byte."""


def stall(seconds: float) -> Behavior:
    def behavior(conn: Conn) -> None:
        time.sleep(seconds)

    return behavior


def drip(data: bytes, interval: float) -> Behavior:
    def behavior(conn: Conn) -> None:
        for i in range(len(data)):
            conn.send(data[i : i + 1])
            time.sleep(interval)

    return behavior


def _read_request(sock: socket.socket) -> Request | None:
    buffer = b""
    while b"\r\n\r\n" not in buffer:
        chunk = sock.recv(65536)
        if not chunk:
            return None
        buffer += chunk
        if len(buffer) > 1 << 20:
            return None
    head, _, rest = buffer.partition(b"\r\n\r\n")
    lines = head.decode("latin-1").split("\r\n")
    method, target, _ = lines[0].split(" ", 2)
    headers: dict[str, list[str]] = {}
    for line in lines[1:]:
        name, _, value = line.partition(":")
        headers.setdefault(name.strip().lower(), []).append(value.strip())
    length = int((headers.get("content-length") or ["0"])[0])
    body = rest
    while len(body) < length:
        chunk = sock.recv(65536)
        if not chunk:
            break
        body += chunk
    return Request(method, target, headers, body, buffer)


class ScriptedServer:
    def __init__(
        self,
        script: list[Behavior] | Behavior,
        *,
        tls: ssl.SSLContext | None = None,
    ) -> None:
        self.script = [script] if callable(script) else list(script)
        self.requests: list[Request] = []
        self.connections = 0
        self.tls = tls
        self._lock = threading.Lock()
        self._listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._listener.bind(("127.0.0.1", 0))
        self._listener.listen(16)
        self.port: int = self._listener.getsockname()[1]
        self._stop = threading.Event()
        self._open: list[socket.socket] = []
        self._threads: list[threading.Thread] = []
        self._accept_thread = threading.Thread(
            target=self._accept_loop, name="scripted-accept", daemon=True
        )

    # lifecycle ---------------------------------------------------------------------------
    def start(self) -> ScriptedServer:
        self._accept_thread.start()
        return self

    def close(self) -> None:
        self._stop.set()
        try:
            # Wake the accept loop with one connection of our own, then close the listener.
            socket.create_connection(("127.0.0.1", self.port), timeout=1).close()
        except OSError:
            pass
        self._accept_thread.join(timeout=5)
        self._listener.close()
        with self._lock:
            sockets = list(self._open)
        for sock in sockets:
            try:
                sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            sock.close()
        for thread in list(self._threads):
            thread.join(timeout=5)

    def __enter__(self) -> ScriptedServer:
        return self.start()

    def __exit__(self, *exc: object) -> None:
        self.close()

    @property
    def url(self) -> str:
        return f"{'https' if self.tls else 'http'}://127.0.0.1:{self.port}"

    # internals ---------------------------------------------------------------------------
    def _accept_loop(self) -> None:
        while not self._stop.is_set():
            try:
                sock, _ = self._listener.accept()
            except OSError:
                return
            if self._stop.is_set():
                sock.close()
                return
            thread = threading.Thread(target=self._serve, args=(sock,), daemon=True)
            self._threads.append(thread)
            thread.start()

    def _serve(self, sock: socket.socket) -> None:
        with self._lock:
            index = self.connections
            self.connections += 1
            self._open.append(sock)
        sock.settimeout(30)
        try:
            if self.tls is not None:
                try:
                    sock = self.tls.wrap_socket(sock, server_side=True)
                except (ssl.SSLError, OSError):
                    return
                with self._lock:
                    self._open.append(sock)
            request = _read_request(sock)
            if request is None:
                return
            with self._lock:
                self.requests.append(request)
            behavior = self.script[min(index, len(self.script) - 1)]
            behavior(Conn(sock, request, self))
        except OSError:
            pass
        finally:
            try:
                sock.close()
            except OSError:
                pass


def closed_port() -> int:
    """A loopback port that nothing listens on (bound, read and released immediately)."""
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    return port
