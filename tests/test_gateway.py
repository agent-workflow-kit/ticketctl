"""Gateway: the only HTTP boundary (issue #8).

Every test talks to a loopback server of this process (tests/scripted_server.py) or to a closed
loopback port; nothing reaches a real host. No test starts a program except the TLS fixtures,
which run the ``openssl`` binary found at a fixed absolute path (never through the ambient PATH),
with an empty PATH and a private HOME (skipped with a reason when it is missing, a failure when the
``CI`` variable is set). The fake provider of issue #10 is not used or built here.
"""

from __future__ import annotations

import base64
import http.client
import json
import os
import re
import socket
import ssl
import subprocess
import threading
import time
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest

from tests.scripted_server import (
    Conn,
    ScriptedServer,
    closed_port,
    drip,
    drop,
    json_reply,
    reply,
    response_bytes,
    stall,
)
from ticketctl import gateway, sanitize
from ticketctl.gateway import Deadline, Gateway, GatewayError, Target

ROOT = Path(__file__).resolve().parent.parent
SENTINEL = "SENTINEL-t0ken-4c1d9e7a52"
EMAIL = "someone@example.test"
B64 = base64.b64encode(f"{EMAIL}:{SENTINEL}".encode()).decode()


@pytest.fixture(autouse=True)
def _no_ambient_proxy(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in ("HTTPS_PROXY", "https_proxy", "HTTP_PROXY", "http_proxy", "NO_PROXY", "no_proxy"):
        monkeypatch.delenv(name, raising=False)


@pytest.fixture(autouse=True)
def _scrub_reset() -> Iterator[None]:
    sanitize.configure_scrubber([])
    yield
    sanitize.configure_scrubber([])


@pytest.fixture
def servers() -> Iterator[list[ScriptedServer]]:
    started: list[ScriptedServer] = []
    yield started
    for server in started:
        server.close()


@pytest.fixture
def serve(servers: list[ScriptedServer]):
    def make(script, **kw: Any) -> ScriptedServer:
        server = ScriptedServer(script, **kw).start()
        servers.append(server)
        return server

    return make


@dataclass
class Rig:
    gw: Gateway
    sleeps: list[float] = field(default_factory=list)
    logs: list[dict[str, Any]] = field(default_factory=list)
    verbose: list[str] = field(default_factory=list)

    def get(self, path: str = "/rest/api/3/myself", **kw: Any):
        kw.setdefault("template", path)
        return self.gw.request("GET", path, **kw)

    def post(self, path: str = "/rest/api/3/issue", body: Any = None, **kw: Any):
        kw.setdefault("template", path)
        return self.gw.request("POST", path, body={"a": 1} if body is None else body, **kw)


def make_target(url: str, **kw: Any) -> Target:
    options: dict[str, Any] = dict(
        name="main",
        url=url,
        email=EMAIL,
        token=SENTINEL,
        connect_timeout=3.0,
        read_timeout=5.0,
        allow_http=url.startswith("http://"),
        retry_after_max=60.0,
    )
    options.update(kw)
    return Target(**options)


def make_rig(url: str, *, budget: float = 30.0, env: dict[str, str] | None = None, **kw: Any):
    target_kw = {
        k: kw.pop(k)
        for k in list(kw)
        if k in {"connect_timeout", "read_timeout", "retry_after_max", "token", "email"}
    }
    rig = Rig.__new__(Rig)
    rig.sleeps, rig.logs, rig.verbose = [], [], []
    rig.gw = Gateway(
        make_target(url, **target_kw),
        deadline=Deadline(budget),
        env={} if env is None else env,
        log_sink=rig.logs.append,
        verbose=rig.verbose.append,
        sleep=rig.sleeps.append,
        rng=lambda: 1.0,
        **kw,
    )
    return rig


def code_of(exc: pytest.ExceptionInfo[GatewayError]) -> str:
    return exc.value.code


# ---------------------------------------------------------------------------------------------
# Request shape and credentials
# ---------------------------------------------------------------------------------------------


def test_get_sends_basic_auth_only_and_no_accept_encoding(serve) -> None:
    server = serve(json_reply('{"ok": true}'))
    response = make_rig(server.url).get("/rest/api/3/myself")
    assert response.status == 200
    assert response.data == {"ok": True}
    (request,) = server.requests
    assert request.method == "GET"
    assert request.target == "/rest/api/3/myself"
    assert request.headers["authorization"] == [f"Basic {B64}"]
    assert "accept-encoding" not in request.headers
    assert re.fullmatch(r"ticketctl/\S+", request.header("user-agent") or "")
    assert request.header("connection") == "close"
    assert b"bearer" not in request.raw.lower()
    assert request.header("host") == f"127.0.0.1:{server.port}"


def test_credentials_are_stripped_of_surrounding_whitespace(serve) -> None:
    server = serve(json_reply())
    make_rig(server.url, token=f"  {SENTINEL}\n", email=f" {EMAIL} ").get()
    assert server.requests[0].headers["authorization"] == [f"Basic {B64}"]


def test_url_is_stripped_of_surrounding_whitespace_but_not_interior(serve) -> None:
    server = serve(json_reply())
    make_rig(server.url + "/ ").get()
    assert len(server.requests) == 1
    with pytest.raises(GatewayError):
        make_target("https://x.example.test/a b")


def test_post_sends_compact_json_body(serve) -> None:
    server = serve(json_reply("{}", status=201))
    make_rig(server.url).post(body={"fields": {"summary": "caf\u00e9"}})
    (request,) = server.requests
    assert request.method == "POST"
    assert request.header("content-type") == "application/json"
    assert int(request.header("content-length") or -1) == len(request.body)
    assert json.loads(request.body) == {"fields": {"summary": "caf\u00e9"}}
    assert b"\xc3" not in request.body  # ensure_ascii


def test_site_context_path_is_prefixed(serve) -> None:
    server = serve(json_reply())
    make_rig(server.url + "/jira").get("/rest/api/3/myself", query="a=b")
    assert server.requests[0].target == "/jira/rest/api/3/myself?a=b"


@pytest.mark.parametrize(
    "path",
    [
        "rest/api",
        "//evil.test/x",
        "http://evil.test/x",
        "/a/../b",
        "/a/./b",
        "/a b",
        "/a\r\nX-Evil: 1",
        "/a\x00",
        "/\u00e9",
        "/a?b=c",
        "/a#frag",
        "",
        "/a//b",
        "/a;b",
        "/a%2fb",
        "/a%2Fb",
        "/a%5cb",
        "/a%5Cb",
        "/..;x",
        "/a/..x",
        "/%2e%2e;/x",
    ],
)
def test_path_that_is_not_a_clean_absolute_path_is_refused(serve, path: str) -> None:
    server = serve(json_reply())
    with pytest.raises(GatewayError) as exc:
        make_rig(server.url).get(path, template="/x")
    assert exc.value.code == "internal"
    assert server.requests == []


@pytest.mark.parametrize("query", ["a b", "a=b#c", "a=\r\nX: 1", "a=\u00e9", "?a=b", "a=/ /"])
def test_query_that_is_not_encoded_is_refused(serve, query: str) -> None:
    server = serve(json_reply())
    with pytest.raises(GatewayError) as exc:
        make_rig(server.url).get("/x", template="/x", query=query)
    assert exc.value.code == "internal"
    assert server.requests == []


def test_template_must_fit_the_log_schema(serve) -> None:
    server = serve(json_reply())
    for bad in ("/rest/api/3/issue/ABC 1", "/rest/{Key}", "x", "/a?b=c", "/" + "a" * 300):
        with pytest.raises(GatewayError) as exc:
            make_rig(server.url).get("/x", template=bad)
        assert exc.value.code == "internal"


def test_http_site_must_be_loopback_and_allowed() -> None:
    with pytest.raises(GatewayError) as exc:
        make_target("http://api.example.test", allow_http=True)
    assert exc.value.code == "invalid_config"
    with pytest.raises(GatewayError):
        make_target("http://127.0.0.1:1", allow_http=False)
    make_target("http://localhost:1", allow_http=True)
    make_target("http://[::1]:1", allow_http=True)


# ---------------------------------------------------------------------------------------------
# Target from config
# ---------------------------------------------------------------------------------------------


def _site(text: str):
    import yaml

    from ticketctl.config import Config

    return Config.model_validate(yaml.safe_load(text)).sites["main"]


def test_target_from_site_reads_token_and_email_from_the_environment() -> None:
    site = _site(
        "sites:\n  main:\n    url: https://api.example.test/jira\n"
        "    email_env: MAIL_VAR\n    token_env: TOK_VAR\n"
        "    timeouts: {connect: 3s, read: 4s}\n"
    )
    target = Target.from_site(
        "main", site, {"MAIL_VAR": f" {EMAIL}", "TOK_VAR": SENTINEL + "\n"}, retry_after_max=7
    )
    assert (target.scheme, target.host, target.port, target.base_path) == (
        "https",
        "api.example.test",
        443,
        "/jira",
    )
    assert (target.email, target.token) == (EMAIL, SENTINEL)
    assert (target.connect_timeout, target.read_timeout, target.retry_after_max) == (3, 4, 7)


@pytest.mark.parametrize("env", [{}, {"TOK_VAR": ""}, {"TOK_VAR": " \n"}])
def test_missing_or_empty_token_is_auth_required_naming_the_variable(env) -> None:
    site = _site(
        "sites:\n  main:\n    url: https://a.test\n    email: a@a.test\n    token_env: TOK_VAR\n"
    )
    with pytest.raises(GatewayError) as exc:
        Target.from_site("main", site, env, retry_after_max=60)
    assert exc.value.code == "auth_required"
    assert exc.value.exit_code == 12
    assert "TOK_VAR" in exc.value.message


def test_missing_email_variable_is_auth_required() -> None:
    site = _site(
        "sites:\n  main:\n    url: https://a.test\n    email_env: MAIL\n    token_env: T\n"
    )
    with pytest.raises(GatewayError) as exc:
        Target.from_site("main", site, {"T": SENTINEL}, retry_after_max=60)
    assert exc.value.code == "auth_required"
    assert "MAIL" in exc.value.message


def test_target_from_site_honours_ipv6_loopback_and_default_ports() -> None:
    site = _site(
        "sites:\n  main:\n    url: http://[::1]:8080\n    allow_http: true\n"
        "    email: a@a.test\n    token_env: T\n"
    )
    target = Target.from_site("main", site, {"T": SENTINEL}, retry_after_max=60)
    assert (target.scheme, target.host, target.port, target.base_path) == ("http", "::1", 8080, "")


# ---------------------------------------------------------------------------------------------
# Redirects and statuses
# ---------------------------------------------------------------------------------------------


@pytest.mark.parametrize("status", [300, 301, 302, 303, 304, 307, 308])
@pytest.mark.parametrize("method", ["GET", "POST"])
def test_every_3xx_is_provider_changed_and_nothing_is_re_sent(serve, status, method) -> None:
    other = serve(json_reply())
    server = serve(reply(status, "", {"Location": f"{other.url}/stolen"}))
    rig = make_rig(server.url)
    with pytest.raises(GatewayError) as exc:
        if method == "GET":
            rig.get()
        else:
            rig.post()
    assert exc.value.code == "provider_changed"
    assert exc.value.exit_code == 12
    assert exc.value.status == status
    assert len(server.requests) == 1
    assert other.requests == [] and other.connections == 0
    assert other.url not in str(exc.value) and "stolen" not in exc.value.message
    assert exc.value.outcome_unknown is (method == "POST")  # a 303 may follow a processed POST
    assert rig.sleeps == []


def test_redirect_loop_is_not_followed(serve) -> None:
    holder: dict[str, ScriptedServer] = {}

    def loop(conn: Conn) -> None:
        conn.send(response_bytes(302, "", {"Location": holder["s"].url + "/again"}))

    holder["s"] = serve(loop)
    with pytest.raises(GatewayError) as exc:
        make_rig(holder["s"].url).get()
    assert exc.value.code == "provider_changed"
    assert len(holder["s"].requests) == 1


def test_401_with_a_plain_text_body_is_bad_credentials(serve) -> None:
    server = serve(reply(401, "Basic authentication failed", {"Content-Type": "text/plain"}))
    with pytest.raises(GatewayError) as exc:
        make_rig(server.url).get()
    assert (exc.value.code, exc.value.exit_code, exc.value.status) == ("bad_credentials", 12, 401)
    assert exc.value.retryable is False
    assert len(server.requests) == 1


def test_403_is_forbidden_without_retry(serve) -> None:
    server = serve(json_reply('{"message": "no"}', status=403))
    with pytest.raises(GatewayError) as exc:
        make_rig(server.url).get()
    assert exc.value.code == "forbidden"
    assert len(server.requests) == 1


@pytest.mark.parametrize("status", [400, 404, 409, 410, 422])
def test_other_4xx_is_returned_for_the_provider_layer(serve, status) -> None:
    server = serve(json_reply('{"errorMessages": ["nope"], "errors": {}}', status=status))
    response = make_rig(server.url).post()
    assert response.status == status
    assert response.data == {"errorMessages": ["nope"], "errors": {}}
    assert len(server.requests) == 1


def test_error_body_may_be_plain_text(serve) -> None:
    server = serve(reply(404, "page not found", {"Content-Type": "text/plain"}))
    response = make_rig(server.url).get()
    assert response.status == 404
    assert response.data is None
    assert response.text == "page not found"


def test_200_with_a_non_json_body_is_provider_changed(serve) -> None:
    server = serve(reply(200, "<html>login</html>", {"Content-Type": "text/html"}))
    with pytest.raises(GatewayError) as exc:
        make_rig(server.url).get()
    assert exc.value.code == "provider_changed"


@pytest.mark.parametrize("status", [200, 204])
def test_empty_success_body_is_none(serve, status) -> None:
    server = serve(reply(status, ""))
    response = make_rig(server.url).post()
    assert response.status == status
    assert response.data is None


# ---------------------------------------------------------------------------------------------
# Retries
# ---------------------------------------------------------------------------------------------


@pytest.mark.parametrize("status", [429, 502, 503, 504])
def test_reads_retry_on_retryable_statuses(serve, status) -> None:
    server = serve([reply(status), reply(status), json_reply('{"ok": 1}')])
    rig = make_rig(server.url)
    assert rig.get().data == {"ok": 1}
    assert len(server.requests) == 3
    assert rig.sleeps == [0.5, 1.0]
    assert rig.logs[-1]["retries"] == 2


def test_reads_give_up_after_four_attempts(serve) -> None:
    server = serve(reply(503))
    rig = make_rig(server.url)
    with pytest.raises(GatewayError) as exc:
        rig.get()
    assert (exc.value.code, exc.value.retryable, exc.value.outcome_unknown) == (
        "unavailable",
        True,
        False,
    )
    assert len(server.requests) == 4
    assert rig.sleeps == [0.5, 1.0, 2.0]


def test_a_500_on_a_read_is_not_retried(serve) -> None:
    server = serve(reply(500))
    with pytest.raises(GatewayError) as exc:
        make_rig(server.url).get()
    assert exc.value.code == "unavailable"
    assert len(server.requests) == 1


def test_read_retries_a_connection_failure_before_a_response() -> None:
    rig = make_rig(f"http://127.0.0.1:{closed_port()}")
    with pytest.raises(GatewayError) as exc:
        rig.get()
    assert exc.value.code == "unavailable"
    assert exc.value.retryable is True
    assert rig.sleeps == [0.5, 1.0, 2.0]
    assert rig.logs[-1]["retries"] == 3
    assert rig.logs[-1]["status"] is None


def test_read_dropped_after_the_request_is_retried(serve) -> None:
    server = serve([drop, drop, json_reply('{"ok": 1}')])
    rig = make_rig(server.url)
    assert rig.get().data == {"ok": 1}
    assert len(server.requests) == 3


def test_post_marked_as_read_retries_like_a_read(serve) -> None:
    server = serve([reply(503), json_reply('{"issues": []}')])
    rig = make_rig(server.url)
    assert rig.post("/rest/api/3/search/jql", write=False).data == {"issues": []}
    assert len(server.requests) == 2


@pytest.mark.parametrize("status", [502, 503, 504, 500])
def test_write_with_a_5xx_is_outcome_unknown_and_not_retried(serve, status) -> None:
    server = serve(reply(status))
    rig = make_rig(server.url)
    with pytest.raises(GatewayError) as exc:
        rig.post()
    assert (exc.value.code, exc.value.outcome_unknown, exc.value.retryable) == (
        "outcome_unknown",
        True,
        True,
    )
    assert len(server.requests) == 1
    assert rig.sleeps == []


def test_write_retries_429_then_succeeds(serve) -> None:
    server = serve([reply(429), reply(429), json_reply('{"id": "1"}', status=201)])
    rig = make_rig(server.url)
    assert rig.post().status == 201
    assert len(server.requests) == 3
    assert rig.sleeps == [0.5, 1.0]


def test_write_429_gives_up_after_two_retries(serve) -> None:
    server = serve(reply(429))
    with pytest.raises(GatewayError) as exc:
        make_rig(server.url).post()
    assert exc.value.code == "rate_limited"
    assert exc.value.outcome_unknown is False
    assert len(server.requests) == 3


def test_write_retries_only_before_any_byte_was_sent() -> None:
    rig = make_rig(f"http://127.0.0.1:{closed_port()}")
    with pytest.raises(GatewayError) as exc:
        rig.post()
    assert exc.value.code == "unavailable"
    assert exc.value.outcome_unknown is False
    assert exc.value.retryable is True
    assert rig.sleeps == [0.5, 1.0]
    assert rig.logs[-1]["retries"] == 2


def test_write_dropped_after_the_headers_is_outcome_unknown_with_no_retry(serve) -> None:
    server = serve(drop)
    rig = make_rig(server.url)
    with pytest.raises(GatewayError) as exc:
        rig.post()
    assert (exc.value.code, exc.value.outcome_unknown) == ("outcome_unknown", True)
    assert len(server.requests) == 1
    assert rig.sleeps == []


def test_write_409_with_a_truncated_body_is_not_outcome_unknown(serve) -> None:
    def partial(conn: Conn) -> None:
        conn.send(response_bytes(409, b"0123456789", content_length=100))

    server = serve(partial)
    rig = make_rig(server.url)
    with pytest.raises(GatewayError) as exc:
        rig.post()
    assert exc.value.outcome_unknown is False
    assert exc.value.code == "provider_changed"
    assert len(server.requests) == 1
    assert rig.sleeps == []


def test_write_dropped_mid_body_is_outcome_unknown(serve) -> None:
    def partial(conn: Conn) -> None:
        conn.send(response_bytes(200, b"0123456789", content_length=100))

    server = serve(partial)
    with pytest.raises(GatewayError) as exc:
        make_rig(server.url).post()
    assert exc.value.code == "outcome_unknown"
    assert len(server.requests) == 1


def test_backoff_formula() -> None:
    assert [gateway.backoff_delay(n, lambda: 1.0) for n in range(7)] == [
        0.5,
        1.0,
        2.0,
        4.0,
        8.0,
        8.0,
        8.0,
    ]
    assert gateway.backoff_delay(3, lambda: 0.0) == 0.0
    assert gateway.backoff_delay(2, lambda: 0.25) == 0.5


# ---------------------------------------------------------------------------------------------
# Retry-After
# ---------------------------------------------------------------------------------------------


def test_retry_after_above_the_cap_ends_at_once(serve) -> None:
    server = serve(reply(429, "", {"Retry-After": "120"}))
    rig = make_rig(server.url, retry_after_max=60.0)
    with pytest.raises(GatewayError) as exc:
        rig.get()
    assert (exc.value.code, exc.value.retry_after_s, exc.value.retryable) == (
        "rate_limited",
        120,
        True,
    )
    assert len(server.requests) == 1
    assert rig.sleeps == []


def test_retry_after_within_the_cap_replaces_the_backoff(serve) -> None:
    server = serve([reply(429, "", {"Retry-After": "7"}), json_reply()])
    rig = make_rig(server.url, retry_after_max=60.0)
    rig.get()
    assert rig.sleeps == [7]


def test_retry_after_longer_than_the_remaining_time_ends_at_once(serve) -> None:
    server = serve(reply(429, "", {"Retry-After": "30"}))
    rig = make_rig(server.url, budget=5.0, retry_after_max=60.0)
    with pytest.raises(GatewayError) as exc:
        rig.get()
    assert (exc.value.code, exc.value.retry_after_s) == ("rate_limited", 30)
    assert rig.sleeps == []


def test_retry_after_max_zero_never_waits(serve) -> None:
    server = serve(reply(429, "", {"Retry-After": "1"}))
    rig = make_rig(server.url, retry_after_max=0.0)
    with pytest.raises(GatewayError) as exc:
        rig.post()
    assert exc.value.code == "rate_limited"
    assert rig.sleeps == []


def test_retry_after_zero_is_honoured_as_no_wait(serve) -> None:
    server = serve([reply(429, "", {"Retry-After": "0"}), json_reply()])
    rig = make_rig(server.url, retry_after_max=0.0)
    rig.get()
    assert rig.sleeps == [0]


def test_zero_padded_retry_after_is_its_numeric_value(serve) -> None:
    server = serve([reply(429, "", {"Retry-After": "00000000005"}), json_reply()])
    rig = make_rig(server.url, retry_after_max=60.0)
    rig.get()
    assert rig.sleeps == [5]


@pytest.mark.parametrize(
    "value", ["Wed, 21 Oct 2037 07:28:00 GMT", "-1", "1e3", "abc", "", "1.5", "+5"]
)
def test_unusable_retry_after_falls_back_to_backoff(serve, value) -> None:
    server = serve([reply(429, "", {"Retry-After": value}), json_reply()])
    rig = make_rig(server.url)
    rig.get()
    assert rig.sleeps == [0.5]


def test_read_503_with_retry_after_above_the_cap_is_unavailable_with_the_value(serve) -> None:
    server = serve(reply(503, "", {"Retry-After": "600"}))
    rig = make_rig(server.url, retry_after_max=60.0)
    with pytest.raises(GatewayError) as exc:
        rig.get()
    assert (exc.value.code, exc.value.retry_after_s) == ("unavailable", 600)
    assert len(server.requests) == 1


def test_a_backoff_that_does_not_fit_the_deadline_ends_the_command(serve) -> None:
    server = serve(reply(503))
    rig = make_rig(server.url, budget=0.2)
    with pytest.raises(GatewayError) as exc:
        rig.get()
    assert exc.value.code == "timeout"
    assert rig.sleeps == []


# ---------------------------------------------------------------------------------------------
# Pacing
# ---------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("limit", "remaining", "paced"),
    [("100", "9", True), ("100", "10", False), ("100", "50", False), ("100", "0", True)],
)
def test_pacing_below_ten_percent(serve, limit, remaining, paced) -> None:
    headers = {"X-RateLimit-Limit": limit, "X-RateLimit-Remaining": remaining}
    server = serve(json_reply("{}", headers=headers))
    rig = make_rig(server.url)
    first = rig.get()
    assert first.rate_limit == {"limit": int(limit), "remaining": int(remaining)}
    assert rig.sleeps == []
    rig.get()
    assert rig.sleeps == ([1.0] if paced else [])
    assert rig.logs[0]["rate_limit_remaining"] == int(remaining)


@pytest.mark.parametrize(
    "headers",
    [
        {},
        {"X-RateLimit-Remaining": "1"},
        {"X-RateLimit-Limit": "0", "X-RateLimit-Remaining": "0"},
        {"X-RateLimit-Limit": "abc", "X-RateLimit-Remaining": "1"},
        {"X-RateLimit-Limit": "100", "X-RateLimit-Remaining": "-5"},
    ],
)
def test_pacing_ignores_missing_or_malformed_headers(serve, headers) -> None:
    server = serve(json_reply("{}", headers=headers))
    rig = make_rig(server.url)
    rig.get()
    rig.get()
    assert rig.sleeps == []


def test_rate_limit_values_are_in_verbose_output_and_the_error(serve) -> None:
    headers = {"X-RateLimit-Limit": "100", "X-RateLimit-Remaining": "3"}
    server = serve(reply(503, "", headers))
    rig = make_rig(server.url)
    with pytest.raises(GatewayError) as exc:
        rig.get()
    assert exc.value.details["rate_limit"] == {"limit": 100, "remaining": 3}
    assert any("remaining=3" in line for line in rig.verbose)
    assert any("rate_limit_limit=100" in line for line in rig.verbose)


# ---------------------------------------------------------------------------------------------
# Deadline and hostile timing
# ---------------------------------------------------------------------------------------------


def test_expired_deadline_stops_before_any_request(serve) -> None:
    server = serve(json_reply())
    rig = make_rig(server.url, budget=0.0)
    with pytest.raises(GatewayError) as exc:
        rig.get()
    assert exc.value.code == "timeout"
    with pytest.raises(GatewayError) as exc2:
        rig.post()
    assert exc2.value.code == "timeout"
    assert exc2.value.outcome_unknown is False
    assert server.requests == [] and server.connections == 0


def test_slowloris_response_is_bounded_by_the_whole_command_deadline(serve) -> None:
    body = b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: 2\r\n\r\n{}"
    server = serve(drip(body, 0.2))
    rig = make_rig(server.url, budget=1.0, read_timeout=5.0)
    started = time.monotonic()
    with pytest.raises(GatewayError) as exc:
        rig.get()
    assert time.monotonic() - started < 3.0
    assert exc.value.code == "timeout"
    assert exc.value.exit_code == 12


def test_slowloris_after_a_write_was_sent_is_outcome_unknown_never_timeout(serve) -> None:
    body = b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\n\r\n{}"
    server = serve(drip(body, 0.2))
    rig = make_rig(server.url, budget=1.0, read_timeout=5.0)
    started = time.monotonic()
    with pytest.raises(GatewayError) as exc:
        rig.post()
    assert time.monotonic() - started < 3.0
    assert (exc.value.code, exc.value.outcome_unknown) == ("outcome_unknown", True)


def test_read_timeout_per_site_is_a_failure_that_reads_retry(serve) -> None:
    server = serve([stall(1.0), json_reply('{"ok": 1}')])
    rig = make_rig(server.url, read_timeout=0.3)
    assert rig.get().data == {"ok": 1}
    assert len(server.requests) == 2


def test_read_timeout_after_all_retries_is_timeout(serve) -> None:
    server = serve(stall(1.0))
    rig = make_rig(server.url, read_timeout=0.2)
    with pytest.raises(GatewayError) as exc:
        rig.get()
    assert exc.value.code == "timeout"
    assert len(server.requests) == 4


def test_write_read_timeout_is_outcome_unknown(serve) -> None:
    server = serve(stall(1.0))
    with pytest.raises(GatewayError) as exc:
        make_rig(server.url, read_timeout=0.2).post()
    assert (exc.value.code, exc.value.outcome_unknown) == ("outcome_unknown", True)
    assert len(server.requests) == 1


def test_tls_handshake_that_never_finishes_is_a_timeout_not_an_unknown_outcome(serve) -> None:
    server = serve(stall(5.0))  # plain server: never answers a ClientHello
    url = f"https://localhost:{server.port}"
    rig = make_rig(url, budget=1.0, connect_timeout=5.0)
    started = time.monotonic()
    with pytest.raises(GatewayError) as exc:
        rig.post()
    assert time.monotonic() - started < 4.0
    assert (exc.value.code, exc.value.outcome_unknown) == ("timeout", False)
    assert server.requests == []


def _alive_attempt_threads() -> list[threading.Thread]:
    return [
        t for t in threading.enumerate() if t.name.startswith("gateway-attempt") and t.is_alive()
    ]


def test_abandoned_attempt_threads_end(serve) -> None:
    server = serve(drip(b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\n\r\n{}", 0.2))
    with pytest.raises(GatewayError):
        make_rig(server.url, budget=0.6).get()
    deadline = time.monotonic() + 3
    while _alive_attempt_threads() and time.monotonic() < deadline:
        time.sleep(0.05)
    assert _alive_attempt_threads() == []


def test_a_cancelled_attempt_never_sends_a_byte(serve) -> None:
    server = serve(json_reply())
    target = make_target(server.url)
    attempt = gateway._Attempt(  # pyright: ignore[reportPrivateUsage]
        target, None, "POST", "/x", b"{}", {}, 5.0, 5.0, None
    )
    assert attempt.cancel() is False  # nothing was sent when the deadline won
    attempt.run()
    assert attempt.sent is False
    assert server.requests == []


# ---------------------------------------------------------------------------------------------
# Response caps and parsing
# ---------------------------------------------------------------------------------------------


def test_response_cap_is_16_mib() -> None:
    assert gateway.MAX_RESPONSE_BYTES == 16 * 1024 * 1024
    assert gateway.MAX_JSON_DEPTH == 400


def test_17_mib_content_length_is_refused_without_reading(serve) -> None:
    server = serve(reply(200, b"{}", {"Content-Type": "application/json"}, content_length=17 << 20))
    with pytest.raises(GatewayError) as exc:
        make_rig(server.url).get()
    assert exc.value.code == "provider_changed"
    assert len(server.requests) == 1


def test_17_mib_chunked_body_is_cut_off_at_the_cap(serve) -> None:
    def chunked(conn: Conn) -> None:
        conn.send(b"HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\n\r\n")
        chunk = b"x" * (1 << 20)
        for _ in range(17):
            conn.send(f"{len(chunk):x}\r\n".encode() + chunk + b"\r\n")
        conn.send(b"0\r\n\r\n")

    server = serve(chunked)
    with pytest.raises(GatewayError) as exc:
        make_rig(server.url).get()
    assert exc.value.code == "provider_changed"
    assert len(server.requests) == 1


def test_endless_chunked_stream_is_cut_off(serve, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(gateway, "MAX_RESPONSE_BYTES", 100_000)

    def endless(conn: Conn) -> None:
        conn.send(b"HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\n\r\n")
        chunk = b"y" * 4096
        while True:
            conn.send(f"{len(chunk):x}\r\n".encode() + chunk + b"\r\n")

    server = serve(endless)
    with pytest.raises(GatewayError) as exc:
        make_rig(server.url).get()
    assert exc.value.code == "provider_changed"


def test_cap_boundary(serve, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(gateway, "MAX_RESPONSE_BYTES", 1000)
    exact = json.dumps({"k": "v" * 990})
    exact += " " * (1000 - len(exact))
    assert len(exact) == 1000
    server = serve(json_reply(exact))
    assert make_rig(server.url).get().status == 200
    server2 = serve(json_reply(exact + " "))
    with pytest.raises(GatewayError):
        make_rig(server2.url).get()


def test_content_length_that_lies_short_yields_only_the_declared_bytes(serve) -> None:
    def short(conn: Conn) -> None:
        conn.send(response_bytes(200, b'{"a": 1}' + b"GARBAGE" * 10_000, content_length=8))

    server = serve(short)
    assert make_rig(server.url).get().data == {"a": 1}


def test_content_length_that_lies_long_is_a_connection_failure(serve) -> None:
    def long(conn: Conn) -> None:
        conn.send(response_bytes(200, b'{"a": 1}', content_length=500))

    server = serve(long)
    rig = make_rig(server.url)
    with pytest.raises(GatewayError) as exc:
        rig.get()
    assert exc.value.code == "unavailable"
    assert len(server.requests) == 4


@pytest.mark.parametrize("value", ["abc", "-1", "5, 6", "1e3", "0x10", " "])
def test_invalid_content_length_is_provider_changed(serve, value) -> None:
    def odd(conn: Conn) -> None:
        conn.send(f"HTTP/1.1 200 OK\r\nContent-Length: {value}\r\n\r\n{{}}".encode())

    server = serve(odd)
    with pytest.raises(GatewayError) as exc:
        make_rig(server.url).get()
    assert exc.value.code == "provider_changed"
    assert len(server.requests) == 1


def test_huge_header_block_is_provider_changed(serve) -> None:
    def many(conn: Conn) -> None:
        headers = "".join(f"X-H{i}: v\r\n" for i in range(300))
        conn.send(f"HTTP/1.1 200 OK\r\n{headers}Content-Length: 2\r\n\r\n{{}}".encode())

    server = serve(many)
    with pytest.raises(GatewayError) as exc:
        make_rig(server.url).get()
    assert exc.value.code == "provider_changed"
    assert len(server.requests) == 1


def test_huge_header_line_is_provider_changed_and_unknown_for_a_write(serve) -> None:
    def long_line(conn: Conn) -> None:
        conn.send(
            b"HTTP/1.1 200 OK\r\nX-Big: " + b"a" * 70_000 + b"\r\nContent-Length: 2\r\n\r\n{}"
        )

    server = serve(long_line)
    with pytest.raises(GatewayError) as exc:
        make_rig(server.url).post()
    assert (exc.value.code, exc.value.outcome_unknown) == ("provider_changed", True)


def test_garbage_status_line_on_a_write_is_outcome_unknown(serve) -> None:
    def garbage(conn: Conn) -> None:
        conn.send(b"\x00\x01garbage\r\n\r\n")

    server = serve(garbage)
    with pytest.raises(GatewayError) as exc:
        make_rig(server.url).post()
    assert exc.value.code == "outcome_unknown"


def _nested(depth: int, open_: str = "[", close: str = "]") -> bytes:
    return (open_ * depth + close * depth).encode()


def test_depth_400_is_accepted_and_401_is_not() -> None:
    assert gateway.parse_json(_nested(400)) is not None
    with pytest.raises(GatewayError) as exc:
        gateway.parse_json(_nested(401))
    assert exc.value.code == "provider_changed"


def test_depth_counts_objects_and_mixed_nesting() -> None:
    assert gateway.parse_json((b'{"a":' * 400) + b"1" + (b"}" * 400))
    with pytest.raises(GatewayError):
        gateway.parse_json((b'{"a":[' * 201) + b"1" + (b"]}" * 201))


def test_a_500_deep_document_is_provider_changed_over_the_wire(serve) -> None:
    server = serve(json_reply(_nested(500).decode()))
    with pytest.raises(GatewayError) as exc:
        make_rig(server.url).get()
    assert exc.value.code == "provider_changed"


def test_very_deep_documents_do_not_overflow_the_stack() -> None:
    with pytest.raises(GatewayError) as exc:
        gateway.parse_json(_nested(1_000_000))
    assert exc.value.code == "provider_changed"


def test_brackets_inside_strings_do_not_count() -> None:
    text = json.dumps({"s": "[" * 5000 + "{" * 5000, "t": '\\"[[[[' * 1000})
    assert gateway.parse_json(text.encode())["s"].startswith("[[")  # type: ignore[index]


def test_escaped_quote_does_not_end_the_string_for_the_guard() -> None:
    # The string holds an escaped quote followed by 500 brackets; they are still string content.
    text = '["a\\"' + "[" * 500 + '"]'
    assert gateway.parse_json(text.encode()) == ['a"' + "[" * 500]


def test_backslash_before_the_closing_quote_is_handled() -> None:
    assert gateway.parse_json(b'["\\\\", [1]]') == ["\\", [1]]


@pytest.mark.parametrize(
    "raw",
    [b"{", b'{"a": }', b"[1,]", b"NaN", b"Infinity", b'{"a": -Infinity}', b"\xff\xfe", b'"\xc3('],
)
def test_invalid_json_is_provider_changed(raw: bytes) -> None:
    with pytest.raises(GatewayError) as exc:
        gateway.parse_json(raw)
    assert exc.value.code == "provider_changed"


def test_huge_integer_is_provider_changed_not_a_crash() -> None:
    with pytest.raises(GatewayError):
        gateway.parse_json(b"1" * 100_000)


def test_unterminated_string_full_of_brackets_is_invalid() -> None:
    with pytest.raises(GatewayError):
        gateway.parse_json(b'["' + b"[" * 1000)


def test_guard_is_fast_enough_for_a_large_document() -> None:
    document = json.dumps({"items": [{"k": "v" * 50, "n": [1, 2, 3]} for _ in range(40_000)]})
    started = time.monotonic()
    assert gateway.parse_json(document.encode())
    assert time.monotonic() - started < 5.0


def test_unicode_bodies_decode(serve) -> None:
    server = serve(json_reply('{"t": "caf\\u00e9 \u4e2d\u6587"}'))
    assert make_rig(server.url).get().data == {"t": "caf\u00e9 \u4e2d\u6587"}


# ---------------------------------------------------------------------------------------------
# Proxy selection
# ---------------------------------------------------------------------------------------------


def _select(env, host="api.example.test", port=443, scheme="https"):
    return gateway.select_proxy(env, scheme, host, port)


def test_no_proxy_variable_means_no_proxy() -> None:
    assert _select({}) is None


def test_https_proxy_is_chosen_upper_case_first() -> None:
    proxy = _select({"HTTPS_PROXY": "http://up.test:3128", "https_proxy": "http://low.test:1"})
    assert (proxy.host, proxy.port) == ("up.test", 3128)  # type: ignore[union-attr]
    proxy = _select({"https_proxy": "http://low.test:1"})
    assert (proxy.host, proxy.port) == ("low.test", 1)  # type: ignore[union-attr]
    assert _select({"HTTPS_PROXY": "", "https_proxy": "http://low.test:1"}) is not None
    assert _select({"HTTP_PROXY": "http://x.test:1"}) is None


def test_proxy_default_port_and_userinfo() -> None:
    proxy = _select({"HTTPS_PROXY": "http://alice:p%40ss@proxy.test"})
    assert proxy is not None
    assert (proxy.host, proxy.port) == ("proxy.test", 80)
    assert proxy.auth_header == "Basic " + base64.b64encode(b"alice:p@ss").decode()
    assert gateway.describe_proxy(proxy) == "proxy.test:80"
    assert "alice" not in gateway.describe_proxy(proxy)


def test_ipv6_proxy_host_is_described_with_brackets() -> None:
    proxy = _select({"HTTPS_PROXY": "http://[::1]:8080"})
    assert proxy is not None
    assert gateway.describe_proxy(proxy) == "[::1]:8080"


@pytest.mark.parametrize(
    "value",
    [
        "https://proxy.test:3128",
        "socks5://proxy.test:1080",
        "proxy.test:3128",
        "http://",
        "http://proxy.test:99999",
        "http://proxy.test:abc",
        "http://u:SECRETPW@",
        "http://proxy.test:3128/\x00",
        "http://pro xy.test",  # scan: allow
        "http://proxy.test:0",
        "http://proxy.test:",
        "http://u:SECRETPW@proxy.test:",
        "http://proxy.test:3128/path",
        "http://proxy.test:3128/a/",
        "http://proxy.test:3128?x=SECRETPW",
        "http://proxy.test:3128/?x=1",
        "http://proxy.test:3128#SECRETPW",
        "http://proxy.test?",
    ],
)
def test_bad_proxy_setting_is_invalid_config_that_never_echoes_the_value(value) -> None:
    with pytest.raises(GatewayError) as exc:
        _select({"HTTPS_PROXY": value})
    assert exc.value.exit_code == 2
    assert exc.value.code == "invalid_config"
    assert "SECRETPW" not in exc.value.message
    assert "proxy.test" not in exc.value.message
    assert "HTTPS_PROXY" in exc.value.message
    assert exc.value.details == {"reason": "config_invalid"}


def test_a_proxy_url_with_a_root_path_is_accepted() -> None:
    proxy = _select({"HTTPS_PROXY": "http://proxy.test:3128/"})
    assert proxy is not None and (proxy.host, proxy.port) == ("proxy.test", 3128)


@pytest.mark.parametrize(
    ("no_proxy", "host", "port", "bypassed"),
    [
        ("api.example.test", "api.example.test", 443, True),
        ("API.example.test", "api.example.test", 443, True),
        (".example.test", "api.example.test", 443, True),
        ("example.test", "api.example.test", 443, True),
        ("*.example.test", "api.example.test", 443, True),
        ("other.test, example.test", "api.example.test", 443, True),
        ("*", "api.example.test", 443, True),
        ("api.example.test:8443", "api.example.test", 443, False),
        ("api.example.test:443", "api.example.test", 443, True),
        ("xexample.test", "api.example.test", 443, False),
        ("", "api.example.test", 443, False),
        (",,", "api.example.test", 443, False),
        ("10.0.0.0/8", "10.1.2.3", 443, True),
        ("10.0.0.0/8", "11.1.2.3", 443, False),
        ("10.1.2.3", "10.1.2.3", 443, True),
        ("::1", "::1", 443, True),
        ("fd00::/8", "fd12::1", 443, True),
        ("notanet/zz", "api.example.test", 443, False),
        ("[::1]", "::1", 443, True),
        ("[::1]:443", "::1", 443, True),
        ("[::1]:444", "::1", 443, False),
        ("[::1]:", "::1", 443, False),
        ("[::2]", "::1", 443, False),
        ("[fd00::]/8", "fd12::1", 443, False),
    ],
)
def test_no_proxy_matching(no_proxy, host, port, bypassed) -> None:
    for name in ("NO_PROXY", "no_proxy"):
        proxy = _select({"HTTPS_PROXY": "http://p.test:1", name: no_proxy}, host, port)
        assert (proxy is None) is bypassed, name


def test_loopback_http_site_is_never_proxied() -> None:
    assert _select({"HTTPS_PROXY": "http://p.test:1"}, "127.0.0.1", 80, "http") is None


# ---------------------------------------------------------------------------------------------
# TLS and the CONNECT tunnel (needs the openssl binary to mint a throwaway certificate)
# ---------------------------------------------------------------------------------------------

_OPENSSL_PATHS = (
    "/usr/bin/openssl",
    "/bin/openssl",
    "/usr/local/bin/openssl",
    "/opt/homebrew/bin/openssl",
)


def _find_openssl() -> str | None:
    """Only fixed absolute locations: the ambient PATH never decides which program runs."""
    for candidate in _OPENSSL_PATHS:
        if os.access(candidate, os.X_OK):
            return candidate
    return None


OPENSSL = _find_openssl()


@pytest.fixture(scope="module")
def certificate(tmp_path_factory: pytest.TempPathFactory) -> tuple[Path, Path]:
    if OPENSSL is None:
        reason = "openssl was not found at " + ", ".join(_OPENSSL_PATHS)
        if "CI" in os.environ:  # only whether it is set; the TLS tests must not vanish in CI
            pytest.fail(reason)
        pytest.skip(reason)
    directory = tmp_path_factory.mktemp("tls")
    home = directory / "home"
    home.mkdir()
    key, crt = directory / "key.pem", directory / "cert.pem"
    result = subprocess.run(
        [
            OPENSSL,
            "req",
            "-x509",
            "-newkey",
            "ec",
            "-pkeyopt",
            "ec_paramgen_curve:prime256v1",
            "-nodes",
            "-keyout",
            str(key),
            "-out",
            str(crt),
            "-days",
            "2",
            "-subj",
            "/CN=localhost",
            "-addext",
            "subjectAltName=DNS:localhost",
        ],
        capture_output=True,
        env={"HOME": str(home), "XDG_CONFIG_HOME": str(home), "PATH": ""},
        timeout=60,
        check=False,
    )
    if result.returncode != 0:
        pytest.skip("openssl could not mint a certificate")
    return crt, key


def _server_context(cert: tuple[Path, Path]) -> ssl.SSLContext:
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(str(cert[0]), str(cert[1]))
    return context


def _client_context(cert: tuple[Path, Path]) -> ssl.SSLContext:
    context = ssl.create_default_context(cafile=str(cert[0]))
    return context


def test_https_request_with_a_trusted_certificate(serve, certificate) -> None:
    server = serve(json_reply('{"tls": true}'), tls=_server_context(certificate))
    rig = make_rig(f"https://localhost:{server.port}", ssl_context=_client_context(certificate))
    assert rig.get().data == {"tls": True}
    assert server.requests[0].headers["authorization"] == [f"Basic {B64}"]


def test_default_context_verifies_certificates_and_hostnames() -> None:
    context = gateway.default_ssl_context()
    assert context.verify_mode == ssl.CERT_REQUIRED
    assert context.check_hostname is True


def test_an_untrusted_certificate_is_refused_before_any_byte_and_not_retried(
    serve, certificate
) -> None:
    server = serve(json_reply(), tls=_server_context(certificate))
    rig = make_rig(f"https://localhost:{server.port}")  # default context: self-signed fails
    with pytest.raises(GatewayError) as exc:
        rig.post()
    assert exc.value.code == "unavailable"
    assert exc.value.retryable is False  # deterministic: re-running cannot help
    assert exc.value.outcome_unknown is False
    assert server.requests == []
    assert rig.sleeps == []
    assert "SSLCertVerificationError" in exc.value.message or "tls" in exc.value.message.lower()


def test_a_certificate_for_another_hostname_is_refused(serve, certificate) -> None:
    server = serve(json_reply(), tls=_server_context(certificate))
    rig = make_rig(f"https://127.0.0.1:{server.port}", ssl_context=_client_context(certificate))
    with pytest.raises(GatewayError) as exc:
        rig.get()
    assert exc.value.code == "unavailable"
    assert server.requests == []


@dataclass
class ProxyLog:
    connect_lines: list[str] = field(default_factory=list)
    headers: list[dict[str, str]] = field(default_factory=list)
    raw: bytes = b""


class ConnectProxy:
    """A CONNECT proxy: answers 200 and pipes bytes to the target, or refuses with a status."""

    def __init__(self, refuse: int | None = None) -> None:
        self.refuse = refuse
        self.log = ProxyLog()
        self._listener = socket.socket()
        self._listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._listener.bind(("127.0.0.1", 0))
        self._listener.listen(8)
        self.port = self._listener.getsockname()[1]
        self._threads: list[threading.Thread] = []
        self._socks: list[socket.socket] = []
        self._stop = threading.Event()
        self._accept = threading.Thread(target=self._loop, daemon=True)
        self._accept.start()

    def close(self) -> None:
        self._stop.set()
        try:
            socket.create_connection(("127.0.0.1", self.port), timeout=1).close()
        except OSError:
            pass
        self._accept.join(timeout=5)
        self._listener.close()
        for sock in self._socks:
            try:
                sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            sock.close()
        for thread in self._threads:
            thread.join(timeout=5)

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                client, _ = self._listener.accept()
            except OSError:
                return
            if self._stop.is_set():
                client.close()
                return
            self._socks.append(client)
            thread = threading.Thread(target=self._serve, args=(client,), daemon=True)
            self._threads.append(thread)
            thread.start()

    def _serve(self, client: socket.socket) -> None:
        client.settimeout(10)
        try:
            buffer = b""
            while b"\r\n\r\n" not in buffer:
                chunk = client.recv(4096)
                if not chunk:
                    return
                buffer += chunk
            head = buffer.split(b"\r\n\r\n")[0].decode("latin-1").split("\r\n")
            self.log.raw += buffer
            self.log.connect_lines.append(head[0])
            self.log.headers.append(
                {k.lower(): v.strip() for k, _, v in (h.partition(":") for h in head[1:])}
            )
            if self.refuse is not None:
                client.sendall(f"HTTP/1.1 {self.refuse} No\r\nContent-Length: 0\r\n\r\n".encode())
                return
            host, _, port = head[0].split(" ")[1].rpartition(":")
            upstream = socket.create_connection((host, int(port)), timeout=5)
            self._socks.append(upstream)
            client.sendall(b"HTTP/1.1 200 Connection established\r\n\r\n")
            pipes = [
                threading.Thread(target=self._pipe, args=(client, upstream, True), daemon=True),
                threading.Thread(target=self._pipe, args=(upstream, client, False), daemon=True),
            ]
            for pipe in pipes:
                pipe.start()
                self._threads.append(pipe)
            for pipe in pipes:
                pipe.join(timeout=10)
        except OSError:
            pass

    def _pipe(self, src: socket.socket, dst: socket.socket, record: bool) -> None:
        try:
            while data := src.recv(65536):
                if record:
                    self.log.raw += data
                dst.sendall(data)
        except OSError:
            pass
        finally:
            try:
                dst.shutdown(socket.SHUT_WR)
            except OSError:
                pass


@pytest.fixture
def proxies():
    started: list[ConnectProxy] = []

    def make(**kw: Any) -> ConnectProxy:
        proxy = ConnectProxy(**kw)
        started.append(proxy)
        return proxy

    yield make
    for proxy in started:
        proxy.close()


def test_connect_tunnel_keeps_tls_end_to_end(serve, proxies, certificate) -> None:
    server = serve(json_reply('{"tunnel": 1}'), tls=_server_context(certificate))
    proxy = proxies()
    env = {"HTTPS_PROXY": f"http://user:PROXYPW@127.0.0.1:{proxy.port}"}
    rig = make_rig(
        f"https://localhost:{server.port}", env=env, ssl_context=_client_context(certificate)
    )
    assert rig.get().data == {"tunnel": 1}
    assert proxy.log.connect_lines == [f"CONNECT localhost:{server.port} HTTP/1.1"]
    assert proxy.log.headers[0]["proxy-authorization"].startswith("Basic ")
    assert server.requests[0].headers["authorization"] == [f"Basic {B64}"]
    # The proxy only ever sees ciphertext: neither the credential nor the request line.
    assert SENTINEL.encode() not in proxy.log.raw
    assert B64.encode() not in proxy.log.raw
    assert b"authorization: basic" not in proxy.log.raw.lower().split(b"\r\n\r\n", 1)[1]
    assert b"/rest/api/3/myself" not in proxy.log.raw


def test_no_proxy_bypasses_the_tunnel(serve, proxies, certificate) -> None:
    server = serve(json_reply(), tls=_server_context(certificate))
    proxy = proxies()
    env = {"HTTPS_PROXY": f"http://127.0.0.1:{proxy.port}", "NO_PROXY": "localhost"}
    make_rig(
        f"https://localhost:{server.port}", env=env, ssl_context=_client_context(certificate)
    ).get()
    assert proxy.log.connect_lines == []
    assert len(server.requests) == 1


@pytest.mark.parametrize("status", [403, 407, 502])
def test_refused_tunnel_is_a_pre_send_failure_with_host_port_only(proxies, status) -> None:
    proxy = proxies(refuse=status)
    env = {"HTTPS_PROXY": f"http://user:PROXYPW@127.0.0.1:{proxy.port}"}
    rig = make_rig("https://localhost:9", env=env)
    with pytest.raises(GatewayError) as exc:
        rig.post()
    assert (exc.value.code, exc.value.outcome_unknown) == ("unavailable", False)
    assert len(proxy.log.connect_lines) == 3  # two write retries: nothing was sent
    text = exc.value.message + json.dumps(exc.value.details)
    assert f"127.0.0.1:{proxy.port}" in text
    assert "PROXYPW" not in text and "user" not in text


def test_unreachable_proxy_names_host_and_port_only() -> None:
    port = closed_port()
    env = {"HTTPS_PROXY": f"http://secretuser:PROXYPW@127.0.0.1:{port}"}
    rig = make_rig("https://localhost:9", env=env)
    with pytest.raises(GatewayError) as exc:
        rig.get()
    shown = exc.value.message + json.dumps(exc.value.details) + "".join(rig.verbose)
    assert f"127.0.0.1:{port}" in shown
    for secret in ("PROXYPW", "secretuser"):
        assert secret not in shown


# ---------------------------------------------------------------------------------------------
# Request log line
# ---------------------------------------------------------------------------------------------


def test_log_line_matches_the_schema(serve) -> None:
    server = serve([reply(503), json_reply("{}", headers={"X-RateLimit-Remaining": "40"})])
    rig = make_rig(server.url, config_sha256="a" * 64)
    rig.get("/rest/api/3/issue/ABC-1", template="/rest/api/3/issue/{key}")
    (line,) = rig.logs
    schema = json.loads((ROOT / "schemas" / "log.json").read_text())
    assert set(line) == set(schema["required"]) == set(schema["properties"])
    assert line["v"] == 1
    assert line["site"] == "main"
    assert line["method"] == "GET"
    assert line["path"] == "/rest/api/3/issue/{key}"
    assert line["status"] == 200
    assert line["retries"] == 1
    assert line["rate_limit_remaining"] == 40
    assert line["host"] == f"127.0.0.1:{server.port}"
    assert line["config_sha256"] == "a" * 64
    assert isinstance(line["duration_ms"], int) and line["duration_ms"] >= 0
    assert re.fullmatch(schema["properties"]["path"]["pattern"], line["path"])
    assert re.fullmatch(schema["properties"]["host"]["pattern"], line["host"])
    assert re.fullmatch(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ", line["ts"])
    assert "ABC-1" not in json.dumps(line)


def test_failed_calls_are_logged_with_a_null_status(serve) -> None:
    server = serve(drop)
    rig = make_rig(server.url)
    with pytest.raises(GatewayError):
        rig.post()
    assert rig.logs[0]["status"] is None
    assert rig.logs[0]["method"] == "POST"


def test_a_failing_log_sink_never_fails_the_call(serve) -> None:
    server = serve(json_reply())
    target = make_target(server.url)

    def broken(line: dict[str, Any]) -> None:
        raise OSError("disk full")

    gw = Gateway(target, deadline=Deadline(10), env={}, log_sink=broken, sleep=lambda s: None)
    assert gw.request("GET", "/x", template="/x").status == 200
    assert gw.warnings == ["log_write_failed"]


def test_default_log_sink_is_a_seam_that_drops_lines(serve) -> None:
    server = serve(json_reply())
    gw = Gateway(make_target(server.url), deadline=Deadline(10), env={})
    assert gw.request("GET", "/x", template="/x").status == 200


# ---------------------------------------------------------------------------------------------
# The token never leaves the gateway except in the Authorization header
# ---------------------------------------------------------------------------------------------


def _everything(rig: Rig, errors: list[GatewayError]) -> str:
    parts = list(rig.verbose) + [json.dumps(line) for line in rig.logs]
    for err in errors:
        parts += [str(err), repr(err), err.message, json.dumps(err.details, default=str)]
        parts.append(sanitize.format_traceback(err))
    return "\n".join(parts)


def test_sentinel_token_and_its_base64_never_reach_output(serve, capsys, monkeypatch) -> None:
    monkeypatch.setenv("TICKETCTL_DEBUG", "1")
    sanitize.configure_scrubber([SENTINEL], basic_values=[B64])

    def echo(conn: Conn) -> None:
        auth = conn.request.header("authorization") or ""
        body = json.dumps({"errorMessages": [auth, SENTINEL, B64]})
        conn.send(response_bytes(400, body, {"Content-Type": "application/json"}))

    behaviors = [
        echo,
        reply(401, f"bad {SENTINEL} {B64}"),
        reply(502, f"{SENTINEL}"),
        drop,
        reply(302, "", {"Location": f"http://x.test/?t={SENTINEL}&b={B64}"}),
        reply(200, f"<html>{SENTINEL}</html>"),
    ]
    errors: list[GatewayError] = []
    texts: list[str] = []
    for behavior in behaviors:
        server = serve(behavior)
        rig = make_rig(server.url)
        for call in (rig.get, rig.post):
            try:
                response = call()
                texts.append(response.text or "")
                texts.append(json.dumps(response.data))
            except GatewayError as err:
                errors.append(err)
        blob = _everything(rig, errors) + "\n".join(texts)
        assert SENTINEL not in blob
        assert B64 not in blob
    captured = capsys.readouterr()
    assert SENTINEL not in captured.out + captured.err
    assert B64 not in captured.out + captured.err


def test_verbose_lines_carry_no_headers(serve) -> None:
    server = serve(
        json_reply("{}", headers={"Set-Cookie": "sid=abc", "X-RateLimit-Remaining": "2"})
    )
    rig = make_rig(server.url)
    rig.get()
    blob = "\n".join(rig.verbose)
    assert rig.verbose
    for forbidden in ("Authorization", "Basic", "Set-Cookie", "sid=abc", "User-Agent", B64):
        assert forbidden.lower() not in blob.lower()


def test_gateway_source_never_enables_protocol_debug_or_prints() -> None:
    source = (ROOT / "src" / "ticketctl" / "gateway.py").read_text()
    assert "set_debuglevel" not in source
    assert "debuglevel" not in source
    assert not re.search(r"^\s*print\(", source, re.M)
    assert not re.search(r"[\"']accept-encoding[\"']", source, re.I)
    assert "skip_accept_encoding=True" in source


def test_error_objects_hold_no_exception_chain() -> None:
    rig = make_rig(f"http://127.0.0.1:{closed_port()}")
    with pytest.raises(GatewayError) as exc:
        rig.get()
    assert exc.value.__cause__ is None
    assert exc.value.__suppress_context__ is True
    assert "ConnectionRefusedError" in exc.value.message


def test_invalid_header_value_error_cannot_carry_the_token(serve) -> None:
    # http.client's ValueError quotes the offending header value; a token with a control
    # character in the middle must not reach any error text.
    server = serve(json_reply())
    bad = f"{SENTINEL}\r\nX-Evil: 1"
    try:
        make_rig(server.url, token=bad).get()
    except GatewayError as err:
        assert SENTINEL not in str(err) + repr(err) + sanitize.format_traceback(err)
    for request in server.requests:
        assert b"X-Evil" not in request.raw


# ---------------------------------------------------------------------------------------------
# Review round 1
# ---------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "raw",
    [
        b'"\\"' * ((1 << 20) // 3),  # many quote starts, each one scanning to the end
        b'"' + b'\\"' * ((1 << 20) // 2),  # one string of escaped quotes, never closed
        b'"' + b"a" * (1 << 20) + b"\\",  # trailing backslash
        b'"' * (1 << 20),
        b'["' + b"[" * (1 << 20),
    ],
    ids=["quote-escape-repeat", "escaped-quotes-open", "trailing-backslash", "quotes", "brackets"],
)
def test_depth_guard_is_linear_on_unterminated_strings(raw: bytes) -> None:
    started = time.monotonic()
    with pytest.raises(GatewayError) as exc:
        gateway.parse_json(raw)
    assert time.monotonic() - started < 10.0  # the quadratic form needs minutes at 1 MiB
    assert exc.value.code == "provider_changed"


@pytest.mark.parametrize("digits", ["1000000000", "9" * 10, "9" * 30, "9" * 5000])
def test_long_retry_after_values_are_clamped_and_end_at_once(serve, digits) -> None:
    server = serve(reply(429, "", {"Retry-After": digits}))
    rig = make_rig(server.url, retry_after_max=60.0)
    with pytest.raises(GatewayError) as exc:
        rig.get()
    assert exc.value.code == "rate_limited"
    assert exc.value.retry_after_s == (min(int(digits), 2**31) if len(digits) < 4000 else 2**31)
    assert len(server.requests) == 1
    assert rig.sleeps == []


def _early_hints(conn: Conn) -> None:
    conn.send(b"HTTP/1.1 103 Early Hints\r\nLink: </x>\r\n\r\n" + response_bytes(200, "{}"))


def _switching(conn: Conn) -> None:
    conn.send(b"HTTP/1.1 101 Switching Protocols\r\nUpgrade: x\r\n\r\n")


@pytest.mark.parametrize("behavior", [_early_hints, _switching, reply(700, "{}")])
@pytest.mark.parametrize("method", ["GET", "POST"])
def test_a_status_outside_200_to_599_is_malformed(serve, behavior, method) -> None:
    server = serve(behavior)
    rig = make_rig(server.url)
    with pytest.raises(GatewayError) as exc:
        rig.get() if method == "GET" else rig.post()
    assert exc.value.code == "provider_changed"
    assert exc.value.outcome_unknown is (method == "POST")
    assert len(server.requests) == 1
    assert rig.sleeps == []
    assert rig.logs[-1]["status"] is None


def _truncated(status: int, headers: dict[str, str] | None = None):
    return reply(status, b"short", headers, content_length=100)


def _bad_length(status: int):
    def behavior(conn: Conn) -> None:
        conn.send(f"HTTP/1.1 {status} X\r\nContent-Length: abc\r\n\r\nxx".encode())

    return behavior


@pytest.mark.parametrize("make", [_truncated, _bad_length])
@pytest.mark.parametrize("method", ["GET", "POST"])
@pytest.mark.parametrize(("status", "code"), [(401, "bad_credentials"), (403, "forbidden")])
def test_a_body_failure_does_not_override_401_and_403(serve, make, method, status, code) -> None:
    server = serve(make(status))
    rig = make_rig(server.url)
    with pytest.raises(GatewayError) as exc:
        rig.get() if method == "GET" else rig.post()
    assert (exc.value.code, exc.value.status, exc.value.outcome_unknown) == (code, status, False)
    assert len(server.requests) == 1


@pytest.mark.parametrize("make", [_truncated, _bad_length])
def test_a_429_with_a_broken_body_on_a_write_is_retried_then_rate_limited(serve, make) -> None:
    server = serve([make(429), json_reply('{"id": "1"}', status=201)])
    rig = make_rig(server.url)
    assert rig.post().status == 201
    assert rig.sleeps == [0.5]
    server2 = serve(make(429))
    rig2 = make_rig(server2.url)
    with pytest.raises(GatewayError) as exc:
        rig2.post()
    assert (exc.value.code, exc.value.outcome_unknown) == ("rate_limited", False)
    assert len(server2.requests) == 3


def test_a_429_with_a_broken_body_honours_retry_after_above_the_cap(serve) -> None:
    server = serve(_truncated(429, {"Retry-After": "120"}))
    rig = make_rig(server.url, retry_after_max=60.0)
    with pytest.raises(GatewayError) as exc:
        rig.post()
    assert (exc.value.code, exc.value.retry_after_s) == ("rate_limited", 120)
    assert len(server.requests) == 1


@pytest.mark.parametrize("make", [_truncated, _bad_length])
def test_a_read_503_with_a_broken_body_is_retried(serve, make) -> None:
    server = serve([make(503), make(502), json_reply('{"ok": 1}')])
    rig = make_rig(server.url)
    assert rig.get().data == {"ok": 1}
    assert len(server.requests) == 3
    assert rig.sleeps == [0.5, 1.0]


@pytest.mark.parametrize("make", [_truncated, _bad_length])
def test_a_3xx_with_a_broken_body_is_still_a_redirect_error(serve, make) -> None:
    server = serve(make(303))
    with pytest.raises(GatewayError) as exc:
        make_rig(server.url).post()
    assert (exc.value.code, exc.value.status, exc.value.outcome_unknown) == (
        "provider_changed",
        303,
        True,
    )


def test_a_4xx_with_an_unusable_body_is_not_an_unknown_outcome(serve) -> None:
    deep = json_reply(_nested(500).decode(), status=400)
    for behavior in (
        _bad_length(400),
        reply(404, b"{}", content_length=17 << 20),
        deep,
        reply(422, b"x" * 10, {"Content-Length": "abc"}),
    ):
        server = serve(behavior)
        with pytest.raises(GatewayError) as exc:
            make_rig(server.url).post()
        assert exc.value.code == "provider_changed"
        assert exc.value.outcome_unknown is False, behavior
        assert len(server.requests) == 1


def test_a_2xx_with_an_unusable_body_stays_an_unknown_outcome_for_a_write(serve) -> None:
    for behavior in (
        _bad_length(201),
        reply(200, b"{}", content_length=17 << 20),
        json_reply(_nested(500).decode()),
        reply(200, b"\xff\xfe"),
        reply(200, "not json"),
    ):
        server = serve(behavior)
        with pytest.raises(GatewayError) as exc:
            make_rig(server.url).post()
        assert (exc.value.code, exc.value.outcome_unknown) == ("provider_changed", True)
        with pytest.raises(GatewayError) as exc2:
            make_rig(server.url).get()
        assert exc2.value.outcome_unknown is False


def test_a_write_5xx_error_carries_no_retry_after(serve) -> None:
    server = serve(reply(503, "", {"Retry-After": "30"}))
    with pytest.raises(GatewayError) as exc:
        make_rig(server.url).post()
    assert exc.value.code == "outcome_unknown"
    assert exc.value.retry_after_s is None


def test_a_read_5xx_error_keeps_retry_after(serve) -> None:
    server = serve(reply(503, "", {"Retry-After": "600"}))
    with pytest.raises(GatewayError) as exc:
        make_rig(server.url, retry_after_max=60.0).get()
    assert exc.value.retry_after_s == 600


def test_a_registered_secret_equal_to_a_number_does_not_break_the_response(serve) -> None:
    sanitize.configure_scrubber(["12345678"])
    server = serve(json_reply('{"n": 12345678, "s": "a12345678b", "12345678": [12345678]}'))
    response = make_rig(server.url).get()
    assert response.status == 200
    assert response.data == {"n": 12345678, "s": "a<redacted>b", "<redacted>": [12345678]}
    assert "12345678" not in response.text


def test_the_discarded_text_of_a_parsed_2xx_is_not_scrubbed(serve, monkeypatch) -> None:
    seen: list[str] = []
    real = gateway.scrub

    def spy(value: str) -> str:
        seen.append(value)
        return real(value)

    monkeypatch.setattr(gateway, "scrub", spy)
    body = '{"alpha": "beta"}'
    server = serve(json_reply(body))
    response = make_rig(server.url).get()
    assert response.data == {"alpha": "beta"}
    assert response.text == ""
    assert body not in seen


def test_a_secret_in_an_error_body_is_scrubbed_in_data_and_text(serve) -> None:
    sanitize.configure_scrubber([SENTINEL])
    server = serve(json_reply(json.dumps({SENTINEL: [SENTINEL]}), status=400))
    response = make_rig(server.url).get()
    assert SENTINEL not in json.dumps(response.data) + response.text
    assert response.data == {"<redacted>": ["<redacted>"]}


def _release_and_wait(release: threading.Event) -> None:
    release.set()
    deadline = time.monotonic() + 10
    while _alive_attempt_threads() and time.monotonic() < deadline:
        time.sleep(0.02)


@pytest.fixture
def slow_connect(monkeypatch: pytest.MonkeyPatch) -> Iterator[threading.Event]:
    release = threading.Event()
    original = http.client.HTTPConnection.connect

    def slow(self: http.client.HTTPConnection) -> None:
        release.wait(15)
        original(self)

    monkeypatch.setattr(http.client.HTTPConnection, "connect", slow)
    yield release
    _release_and_wait(release)


def test_the_deadline_is_not_overrun_by_a_waiting_worker(serve, slow_connect) -> None:
    server = serve(json_reply())
    rig = make_rig(server.url, budget=1.0, connect_timeout=30.0)
    started = time.monotonic()
    with pytest.raises(GatewayError) as exc:
        rig.get()
    assert time.monotonic() - started < 2.5  # it used to wait up to 2 s more
    assert exc.value.code == "timeout"


def test_cancel_during_a_slow_connect_sends_no_request_byte(serve, slow_connect) -> None:
    server = serve(json_reply())
    rig = make_rig(server.url, budget=0.5, connect_timeout=30.0)
    with pytest.raises(GatewayError) as exc:
        rig.post()
    assert (exc.value.code, exc.value.outcome_unknown) == ("timeout", False)
    _release_and_wait(slow_connect)  # the connect now completes after the deadline
    assert _alive_attempt_threads() == []
    assert server.requests == []


_PROXY_BAD_NAMES = ("HTTPS_PROXY", "https_proxy")


@pytest.mark.parametrize("variable", _PROXY_BAD_NAMES)
def test_bad_proxy_error_names_the_variable_in_use(variable) -> None:
    with pytest.raises(GatewayError) as exc:
        _select({variable: "http://proxy.test:0"})
    assert variable in exc.value.message


def test_proxy_without_a_port_uses_80_and_an_explicit_port_is_kept() -> None:
    assert _select({"HTTPS_PROXY": "http://p.test"}).port == 80  # type: ignore[union-attr]
    assert _select({"HTTPS_PROXY": "http://p.test:65535"}).port == 65535  # type: ignore[union-attr]


def test_the_two_target_config_errors_carry_the_config_invalid_reason() -> None:
    for url, allow in (("ftp://api.example.test", False), ("http://api.example.test", True)):
        with pytest.raises(GatewayError) as exc:
            make_target(url, allow_http=allow)
        assert exc.value.details == {"reason": "config_invalid"}
        assert exc.value.exit_code == 2


@pytest.mark.parametrize(
    ("yaml_extra", "expected"),
    [
        ("    email_env: MAIL_VAR\n    token_env: TOK_VAR\n", "TOK_VAR"),
        ("    email: a@a.test\n    token_env: TOK_VAR\n", "TOK_VAR"),
    ],
)
def test_auth_required_from_a_site_carries_the_variable_name_only(yaml_extra, expected) -> None:
    site = _site(f"sites:\n  main:\n    url: https://a.test\n{yaml_extra}")
    with pytest.raises(GatewayError) as exc:
        Target.from_site("main", site, {"MAIL_VAR": EMAIL, "TOK_VAR": ""}, retry_after_max=60)
    assert exc.value.details == {"token_env": expected}


def test_auth_required_for_a_missing_email_variable_carries_a_name_not_a_value() -> None:
    site = _site(
        "sites:\n  main:\n    url: https://a.test\n    email_env: MAIL\n    token_env: T\n"
    )
    with pytest.raises(GatewayError) as exc:
        Target.from_site("main", site, {"T": SENTINEL}, retry_after_max=60)
    assert SENTINEL not in json.dumps(exc.value.details)
    assert set(exc.value.details) == {"token_env"}


@pytest.mark.parametrize(
    "url",
    [
        "https://api.example.test:99999",
        "https://api.example.test:abc",
        "https://api.example.test:0",
        "https://api.example.test:-1",
        "https://api.example.test/a b",
        "https://api.example.test/a\tb",
        "https://api .example.test/",  # scan: allow
        "https://api.example.test/a\nb",
        "https://user:HUNTER2pw@api.example.test",
        "https://user@api.example.test",
        "https://:HUNTER2pw@api.example.test",
    ],
)
def test_a_target_url_that_is_not_a_clean_site_url_is_invalid_config(url) -> None:
    with pytest.raises(GatewayError) as exc:
        make_target(url)
    assert (exc.value.code, exc.value.exit_code) == ("invalid_config", 2)
    assert exc.value.details == {"reason": "config_invalid"}
    assert "HUNTER2pw" not in str(exc.value) + repr(exc.value) + exc.value.message


def test_an_email_with_a_colon_is_invalid_config() -> None:
    with pytest.raises(GatewayError) as exc:
        make_target("https://api.example.test", email="a:b@example.test")
    assert (exc.value.code, exc.value.exit_code) == ("invalid_config", 2)
    assert exc.value.details == {"reason": "config_invalid"}
    assert "a:b" not in exc.value.message


def test_target_repr_shows_no_url_or_credential() -> None:
    target = make_target("https://api.example.test:8443/jira")
    shown = repr(target)
    assert "api.example.test" not in shown
    assert SENTINEL not in shown and EMAIL not in shown
    assert "main" in shown


def test_site_url_ports_use_explicit_values_and_scheme_defaults() -> None:
    assert make_target("https://api.example.test").port == 443
    assert make_target("https://api.example.test:8443").port == 8443
    assert make_target("http://127.0.0.1", allow_http=True).port == 80


def test_a_request_that_cannot_be_built_is_internal_and_never_unknown(serve) -> None:
    server = serve(json_reply())
    target = make_target(server.url)
    object.__setattr__(target, "url", server.url + "/a b")  # bypasses the Target validation
    gw = Gateway(target, deadline=Deadline(10), env={}, sleep=lambda s: None)
    with pytest.raises(GatewayError) as exc:
        gw.request("POST", "/x", template="/x", body={})
    assert (exc.value.code, exc.value.outcome_unknown) == ("internal", False)
    assert server.requests == []


@pytest.mark.parametrize("body", [{"a": float("nan")}, {"a": object()}, {1.5: {2}}])
def test_a_body_that_cannot_be_encoded_is_internal_and_sends_nothing(serve, body) -> None:
    server = serve(json_reply())
    with pytest.raises(GatewayError) as exc:
        make_rig(server.url).post(body=body)
    assert (exc.value.code, exc.value.outcome_unknown) == ("internal", False)
    assert server.connections == 0


def _padded_document(size: int) -> bytes:
    document = b'{"k":"' + b"v" * (size - 8) + b'"}'
    assert len(document) == size
    return document


def _chunked_reply(document: bytes):
    def behavior(conn: Conn) -> None:
        conn.send(b"HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\n\r\n")
        for start in range(0, len(document), 1 << 20):
            piece = document[start : start + (1 << 20)]
            conn.send(f"{len(piece):x}\r\n".encode() + piece + b"\r\n")
        conn.send(b"0\r\n\r\n")

    return behavior


@pytest.mark.parametrize("mode", ["content-length", "chunked"])
def test_the_16_mib_cap_is_exact(serve, mode) -> None:
    cap = gateway.MAX_RESPONSE_BYTES
    assert cap == 16 * 1024 * 1024

    def behavior(document: bytes):
        if mode == "chunked":
            return _chunked_reply(document)
        return reply(200, document, {"Content-Type": "application/json"})

    ok = serve(behavior(_padded_document(cap)))
    response = make_rig(ok.url).get()
    assert response.status == 200 and len(response.data["k"]) == cap - 8
    over = serve(behavior(_padded_document(cap + 1)))
    with pytest.raises(GatewayError) as exc:
        make_rig(over.url).get()
    assert exc.value.code == "provider_changed"
