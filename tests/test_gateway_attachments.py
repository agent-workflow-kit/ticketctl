"""Gateway: the multipart upload and the binary read (issue #42).

Loopback scripted servers only (``tests/scripted_server.py``); nothing reaches a real host.
"""

from __future__ import annotations

import re
from collections.abc import Iterator
from typing import Any

import pytest

from tests.scripted_server import ScriptedServer, json_reply, reply
from tests.test_gateway import B64, make_rig
from ticketctl import gateway
from ticketctl.gateway import GatewayError, Upload


@pytest.fixture
def servers() -> Iterator[list[ScriptedServer]]:
    started: list[ScriptedServer] = []
    yield started
    for server in started:
        server.close()


@pytest.fixture
def serve(servers: list[ScriptedServer]):
    def make(script: Any) -> ScriptedServer:
        server = ScriptedServer(script).start()
        servers.append(server)
        return server

    return make


PATH = "/rest/api/3/issue/DEMO-1/attachments"
TEMPLATE = "/rest/api/3/issue/{key}/attachments"
PAYLOAD = b"line one\r\n\x00\xff\xfe binary \r\n--boundary-look-alike\r\n"


def upload_rig(url: str, **kw: Any):
    return make_rig(url, **kw)


def send(rig: Any, upload: Upload, **kw: Any):
    return rig.gw.request("POST", PATH, template=TEMPLATE, upload=upload, **kw)


def test_upload_is_one_multipart_post_with_the_no_check_header(serve) -> None:
    server = serve(json_reply('[{"id": "1"}]'))
    rig = upload_rig(server.url)
    res = send(rig, Upload("report.bin", PAYLOAD))
    assert res.status == 200 and res.data == [{"id": "1"}]
    (req,) = server.requests
    assert req.method == "POST" and req.target == PATH
    assert req.header("X-Atlassian-Token") == "no-check"
    assert req.header("Authorization") == f"Basic {B64}"
    ctype = req.header("Content-Type") or ""
    match = re.fullmatch(r"multipart/form-data; boundary=([A-Za-z0-9-]{16,70})", ctype)
    assert match, ctype
    boundary = match.group(1).encode()
    assert req.header("Content-Length") == str(len(req.body))
    assert req.body.startswith(b"--" + boundary + b"\r\n")
    assert req.body.endswith(b"\r\n--" + boundary + b"--\r\n")
    assert b'Content-Disposition: form-data; name="file"; filename="report.bin"\r\n' in req.body
    assert b"Content-Type: application/octet-stream\r\n\r\n" + PAYLOAD + b"\r\n--" in req.body
    assert req.body.count(b"--" + boundary) == 2  # the content never ends the part early


def test_a_boundary_found_in_the_content_is_replaced(
    serve, monkeypatch: pytest.MonkeyPatch
) -> None:
    server = serve(json_reply("[]"))
    tokens = iter(["a" * 32, "b" * 32])
    monkeypatch.setattr(gateway.secrets, "token_hex", lambda n: next(tokens))
    content = b"x\r\n--ticketctl-" + b"a" * 32 + b"\r\ny"
    send(upload_rig(server.url), Upload("f.txt", content))
    (req,) = server.requests
    assert ("boundary=ticketctl-" + "b" * 32) in (req.header("Content-Type") or "")
    assert content in req.body


def test_non_ascii_names_are_sent_as_utf8(serve) -> None:
    server = serve(json_reply("[]"))
    send(upload_rig(server.url), Upload("résumé ☃.txt", b"x"))
    (req,) = server.requests
    assert 'filename="résumé ☃.txt"'.encode() in req.body


@pytest.mark.parametrize(
    "name",
    [
        "",
        "a" * 256,
        'quo"te.txt',
        "back\\slash.txt",
        "line\nbreak.txt",
        "cr\rname.txt",
        "nul\x00.txt",
        "tab\t.txt",
        "bell\x07.txt",
        "del\x7f.txt",
        "../up.txt",
        "dir/name.txt",
    ],
)
def test_a_name_that_could_break_the_part_is_refused_before_any_request(serve, name: str) -> None:
    server = serve(json_reply("[]"))
    with pytest.raises(GatewayError) as exc:
        send(upload_rig(server.url), Upload(name, b"x"))
    assert exc.value.code == "internal"
    assert server.requests == []


@pytest.mark.parametrize("field_name", ["", 'fi"le', "a b", "x" * 40])
def test_the_form_field_and_content_type_are_closed_grammars(serve, field_name: str) -> None:
    server = serve(json_reply("[]"))
    with pytest.raises(GatewayError):
        send(upload_rig(server.url), Upload("f.txt", b"x", field=field_name))
    with pytest.raises(GatewayError):
        send(upload_rig(server.url), Upload("f.txt", b"x", content_type="text/plain\r\nX: y"))
    assert server.requests == []


def test_upload_and_json_body_together_are_refused(serve) -> None:
    server = serve(json_reply("[]"))
    with pytest.raises(GatewayError) as exc:
        send(upload_rig(server.url), Upload("f.txt", b"x"), body={"a": 1})
    assert exc.value.code == "internal"
    assert server.requests == []


def test_upload_needs_a_post(serve) -> None:
    server = serve(json_reply("[]"))
    rig = upload_rig(server.url)
    with pytest.raises(GatewayError) as exc:
        rig.gw.request("PUT", PATH, template=TEMPLATE, upload=Upload("f.txt", b"x"))
    assert exc.value.code == "internal"
    assert server.requests == []


def test_read_only_mode_refuses_the_upload_before_any_request(serve) -> None:
    server = serve(json_reply("[]"))
    rig = upload_rig(server.url, read_only=True)
    with pytest.raises(GatewayError) as exc:
        send(rig, Upload("f.txt", b"x"))
    assert exc.value.code == "read_only" and exc.value.exit_code == 20
    assert server.requests == []


def test_a_failed_upload_after_the_bytes_were_sent_is_never_resent(serve) -> None:
    server = serve(reply(502, "bad gateway"))
    rig = upload_rig(server.url)
    with pytest.raises(GatewayError) as exc:
        send(rig, Upload("f.txt", b"x"))
    assert exc.value.code == "outcome_unknown"
    assert len(server.requests) == 1


def test_upload_redirect_is_provider_changed_and_unknown(serve) -> None:
    server = serve(reply(303, "", {"Location": "https://other.example.test/x"}))
    rig = upload_rig(server.url)
    with pytest.raises(GatewayError) as exc:
        send(rig, Upload("f.txt", b"x"))
    assert exc.value.code == "provider_changed" and exc.value.outcome_unknown is True
    assert len(server.requests) == 1


# --- binary reads ------------------------------------------------------------------------------


def test_a_binary_read_returns_the_raw_bytes_and_asks_for_any_type(serve) -> None:
    server = serve(reply(200, b"\xff\x00\x01 not utf-8", {"Content-Type": "image/png"}))
    rig = upload_rig(server.url)
    res = rig.gw.request(
        "GET", "/rest/api/3/attachment/content/1", template="/x/{id}", query="redirect=false",
        binary=True,
    )  # fmt: skip
    assert res.status == 200 and res.raw == b"\xff\x00\x01 not utf-8"
    assert res.data is None and res.text == ""
    (req,) = server.requests
    assert req.header("Accept") == "*/*"
    assert req.target == "/rest/api/3/attachment/content/1?redirect=false"
    assert req.header("X-Atlassian-Token") is None


def test_a_binary_read_that_is_a_redirect_is_not_followed(serve) -> None:
    server = serve(reply(303, "", {"Location": "https://media.example.test/blob"}))
    rig = upload_rig(server.url)
    with pytest.raises(GatewayError) as exc:
        rig.gw.request("GET", "/rest/api/3/attachment/content/1", template="/x/{id}", binary=True)
    assert exc.value.code == "provider_changed"
    assert exc.value.outcome_unknown is False
    assert len(server.requests) == 1


def test_a_binary_read_error_keeps_the_normal_error_parsing(serve) -> None:
    server = serve(json_reply('{"errorMessages": ["nope"]}', status=404))
    rig = upload_rig(server.url)
    res = rig.gw.request("GET", "/rest/api/3/attachment/content/1", template="/x/{id}", binary=True)
    assert res.status == 404 and res.raw == b"" and res.data == {"errorMessages": ["nope"]}


def test_a_binary_answer_over_the_response_cap_is_provider_changed(
    serve, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(gateway, "MAX_RESPONSE_BYTES", 1024)
    server = serve(reply(200, b"z" * 2048))
    rig = upload_rig(server.url)
    with pytest.raises(GatewayError) as exc:
        rig.gw.request("GET", "/rest/api/3/attachment/content/1", template="/x/{id}", binary=True)
    assert exc.value.code == "provider_changed"
