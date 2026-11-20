"""The fake provider reproduces each verified quirk (issue #10).

One test per quirk switch of docs/design.md (Live verification) plus the endpoint shapes, the
failure injection and a few runs through the real gateway. Loopback only; nothing starts a program.
"""

from __future__ import annotations

import http.client
import json
import re
import socket
import threading
import time
from collections.abc import Callable
from datetime import timedelta
from typing import Any

import pytest

from tests.conftest import make_gateway
from tests.fake_provider import (
    EXTRA_FLOATS,
    F_CRITERIA,
    F_EFFORT,
    F_POINTS,
    F_SPRINT,
    F_VALUE,
    ME_ACCOUNT,
    OTHER_ACCOUNT,
    FakeProvider,
    Fault,
    JqlSyntax,
    ProviderError,
    Quirks,
    adf_text,
    adf_variety_doc,
    parse_jql,
)
from tests.scripted_server import ScriptedServer
from ticketctl.gateway import GatewayError

API = "/rest/api/3"
AGILE = "/rest/agile/1.0"

Factory = Callable[[Quirks | None], FakeProvider]


def search(fake: FakeProvider, jql: str, **extra: Any) -> tuple[int, Any]:
    return fake.json("POST", f"{API}/search/jql", {"jql": jql, **extra})


def _filler(fields: dict[str, Any]) -> list[str]:
    """The filler keys of a read (numbered from 80000; the real custom fields are 90001 up)."""
    return [k for k in fields if k.startswith("customfield_") and 80000 <= int(k[12:]) < 90000]


def keys(doc: Any) -> list[str]:
    return [i["key"] for i in doc["issues"]]


MISSING_TICKET = ["Issue does not exist or you do not have permission to see it."]


def raw_exchange(fake: FakeProvider, head: str, body: bytes = b"", *, auth: bool = True) -> bytes:
    """Send hand-built request bytes and read until the server closes (every byte it sent)."""
    lines = [head, "Host: 127.0.0.1", "Connection: close"]
    if auth:
        lines.append("Authorization: " + fake.basic_header())
    if body:
        lines.append(f"Content-Length: {len(body)}")
    payload = ("\r\n".join(lines) + "\r\n\r\n").encode() + body
    with socket.create_connection(("127.0.0.1", fake.port), timeout=5) as sock:
        sock.sendall(payload)
        chunks = []
        while chunk := sock.recv(65536):
            chunks.append(chunk)
    return b"".join(chunks)


def advertised_and_received(raw: bytes) -> tuple[int, int]:
    """The Content-Length a response advertises and the body bytes that actually came."""
    head, _, body = raw.partition(b"\r\n\r\n")
    match = re.search(rb"(?im)^content-length:\s*(\d+)", head)
    assert match, head
    return int(match.group(1)), len(body)


# --- lifecycle and safety ---------------------------------------------------------------


def test_binds_loopback_on_an_ephemeral_port(fake: FakeProvider) -> None:
    assert fake.url == f"http://127.0.0.1:{fake.port}"
    assert fake.port > 1024


def test_close_stops_listening_and_wakes_a_sleeping_handler(fake_factory: Factory) -> None:
    provider = fake_factory(Quirks(response_delay_s=30))
    box: list[object] = []

    def call() -> None:
        try:
            provider.request("GET", f"{API}/myself", timeout=10)
        except OSError as exc:
            box.append(exc)
        else:
            box.append("answered")

    thread = threading.Thread(target=call)
    thread.start()
    time.sleep(0.2)
    started = time.monotonic()
    provider.close()
    thread.join(10)
    assert not thread.is_alive()
    assert time.monotonic() - started < 8
    with pytest.raises(OSError):
        http.client.HTTPConnection("127.0.0.1", provider.port, timeout=1).request("GET", "/")


# --- authentication (V-1) ---------------------------------------------------------------


def test_wrong_token_is_401_with_a_plain_text_body(fake: FakeProvider) -> None:
    status, headers, body = fake.request(
        "GET", f"{API}/myself", auth=fake.basic_header(token="nope")
    )
    assert status == 401
    assert headers["content-type"].startswith("text/plain")
    assert not body.startswith(b"{")


def test_missing_authorization_is_401(fake: FakeProvider) -> None:
    assert fake.request("GET", f"{API}/myself", auth=False)[0] == 401


def test_bearer_with_an_api_token_is_403(fake: FakeProvider) -> None:
    assert fake.request("GET", f"{API}/myself", auth="Bearer fake-token-0123456789")[0] == 403


def test_malformed_basic_value_is_401(fake: FakeProvider) -> None:
    assert fake.request("GET", f"{API}/myself", auth="Basic !!!not-base64")[0] == 401


def test_myself(fake: FakeProvider) -> None:
    status, doc = fake.json("GET", f"{API}/myself")
    assert status == 200 and doc["accountId"] == ME_ACCOUNT and "emailAddress" in doc


# --- V-2 and V-3: search ------------------------------------------------------------------


def test_old_search_is_410(fake: FakeProvider) -> None:
    status, doc = fake.json("POST", f"{API}/search", {"jql": "project = DEMO"})
    assert status == 410 and doc["errorMessages"]


def test_old_search_works_when_the_quirk_is_off(fake_factory: Factory) -> None:
    provider = fake_factory(Quirks(old_search_gone=False))
    assert provider.json("POST", f"{API}/search", {"jql": "project = DEMO"})[0] == 200


def test_search_pages_with_a_token_and_has_no_total(fake: FakeProvider) -> None:
    status, first = search(
        fake, "project = DEMO ORDER BY key ASC", maxResults=2, fields=["summary"]
    )
    assert status == 200 and keys(first) == ["DEMO-1", "DEMO-2"]
    assert "total" not in first and first["nextPageToken"]
    _, second = search(
        fake,
        "project = DEMO ORDER BY key ASC",
        maxResults=2,
        fields=["summary"],
        nextPageToken=first["nextPageToken"],
    )
    assert keys(second) == ["DEMO-3"] and "nextPageToken" not in second


def test_bad_page_token_is_400(fake: FakeProvider) -> None:
    assert search(fake, "project = DEMO", nextPageToken="garbage!")[0] == 400


def _many(fake: FakeProvider, n: int) -> None:
    for i in range(n):
        fake.add_ticket(f"DEMO-{100 + i}", f"bulk {i}")


def test_all_fields_silently_caps_a_page_at_100(fake: FakeProvider) -> None:
    _many(fake, 150)
    _, doc = search(fake, "project = DEMO", maxResults=1000, fields=["*all"])
    assert len(doc["issues"]) == 100 and doc["nextPageToken"]


def test_explicit_fields_are_not_capped_at_100(fake: FakeProvider) -> None:
    _many(fake, 150)
    _, doc = search(fake, "project = DEMO", maxResults=1000, fields=["summary"])
    assert len(doc["issues"]) == 153 and "nextPageToken" not in doc


def test_page_cap_can_be_switched_off(fake_factory: Factory) -> None:
    provider = fake_factory(Quirks(all_fields_page_cap=None))
    _many(provider, 150)
    _, doc = search(provider, "project = DEMO", maxResults=1000, fields=["*all"])
    assert len(doc["issues"]) == 153


def test_semantic_jql_error_is_a_silent_empty_200(fake: FakeProvider) -> None:
    for jql in (
        "nosuchfield = 1",
        'status = "Bogus Status"',
        "assignee = nosuchfunction()",
        "project = ZZNOPE",
    ):
        status, doc = search(fake, jql)
        assert (status, doc["issues"]) == (200, []), jql


def test_syntax_error_is_400(fake: FakeProvider) -> None:
    for jql in ("project =", "project = DEMO AND", "((project = DEMO)", 'summary ~ "open'):
        assert search(fake, jql)[0] == 400, jql


def test_semantic_error_is_400_when_the_quirk_is_off(fake_factory: Factory) -> None:
    provider = fake_factory(Quirks(silent_semantic_jql=False))
    assert search(provider, "nosuchfield = 1")[0] == 400


def test_parse_endpoint_reports_what_search_hides(fake: FakeProvider) -> None:
    path = f"{API}/jql/parse"
    status, doc = fake.json(
        "POST",
        path + "?validation=strict",
        {"queries": ["nosuchfield = 1", "project = DEMO", "project ="]},
    )
    assert status == 200
    semantic, fine, syntax = (q["errors"] for q in doc["queries"])
    assert semantic and not fine and syntax


def test_parse_with_validation_none_reports_only_syntax_errors(fake: FakeProvider) -> None:
    _, doc = fake.json(
        "POST", f"{API}/jql/parse?validation=none", {"queries": ["nosuchfield = 1", "project ="]}
    )
    semantic, syntax = (q["errors"] for q in doc["queries"])
    assert not semantic and syntax


def test_parse_always_answers_200_even_for_garbage(fake: FakeProvider) -> None:
    assert fake.json("POST", f"{API}/jql/parse", {"queries": ["))))"]})[0] == 200


def test_approximate_count(fake: FakeProvider) -> None:
    assert fake.json("POST", f"{API}/search/approximate-count", {"jql": "project = DEMO"}) == (
        200,
        {"count": 3},
    )


def test_jql_precedence_parentheses_and_order(fake: FakeProvider) -> None:
    _, doc = search(
        fake, "project = DEMO AND (status = Backlog OR status = Done) ORDER BY key DESC"
    )
    assert keys(doc) == ["DEMO-3", "DEMO-1"]
    _, doc = search(fake, "project = DEMO AND NOT status = Done AND assignee = currentUser()")
    assert keys(doc) == ["DEMO-2"]


def test_jql_escaped_quote_and_in_list(fake: FakeProvider) -> None:
    fake.add_ticket("DEMO-9", 'He said "hi"', labels=["x"])
    _, doc = search(fake, 'summary ~ "He said \\"hi\\"" AND labels in (x, y)')
    assert keys(doc) == ["DEMO-9"]


def test_text_match_is_a_tokenized_phrase_match(fake: FakeProvider) -> None:
    # V-31: punctuation is ignored and near matches are returned.
    _, doc = search(fake, 'summary ~ "seeded: backlog, task!"')
    assert "DEMO-1" in keys(doc)


def test_absolute_dates_are_read_in_the_users_time_zone(fake: FakeProvider) -> None:
    from datetime import UTC, datetime

    fake.tickets["DEMO-1"].updated = datetime(
        2026, 3, 10, 5, 30, tzinfo=UTC
    )  # 23:30 the day before, local
    _, doc = search(fake, 'updated >= "2026-03-10" AND key = DEMO-1')
    assert keys(doc) == []
    _, doc = search(fake, 'updated >= "2026-03-09" AND key = DEMO-1')
    assert keys(doc) == ["DEMO-1"]


def test_parse_jql_helper_rejects_trailing_garbage() -> None:
    with pytest.raises(JqlSyntax):
        parse_jql("project = DEMO )")


# --- V-4, V-25: reads ---------------------------------------------------------------------


def test_default_read_is_big_and_fields_param_makes_it_small(fake: FakeProvider) -> None:
    big = fake.request("GET", f"{API}/issue/DEMO-2")[2]
    small = fake.request("GET", f"{API}/issue/DEMO-2?fields=summary,status")[2]
    assert len(json.loads(big)["fields"]) > 500
    assert len(big) > 10_000 and len(small) < 2_000
    assert set(json.loads(small)["fields"]) == {"summary", "status"}


def test_issue_self_link_is_absolute_and_unfollowed_data(fake: FakeProvider) -> None:
    _, doc = fake.json("GET", f"{API}/issue/DEMO-1?fields=summary")
    assert doc["self"].startswith(fake.url)


def test_missing_key_is_one_404_with_the_same_text(fake: FakeProvider) -> None:
    a = fake.json("GET", f"{API}/issue/DEMO-999")
    b = fake.json("GET", f"{API}/issue/not-a-key")
    assert a[0] == b[0] == 404 and a[1] == b[1]
    assert a[1]["errorMessages"] == MISSING_TICKET


@pytest.mark.parametrize("key", ["DEMO-999", "not-a-key", "DEMO-", "-1", "DEMO-1;x", "%20"])
def test_missing_and_malformed_keys_get_the_exact_same_404_text(
    fake: FakeProvider, key: str
) -> None:
    assert fake.json("GET", f"{API}/issue/{key}") == (
        404,
        {"errorMessages": MISSING_TICKET, "errors": {}},
    )


def test_search_embeds_only_the_first_20_comments(fake: FakeProvider) -> None:
    for i in range(25):
        assert (
            fake.json("POST", f"{API}/issue/DEMO-1/comment", {"body": adf_text(f"c{i}")})[0] == 201
        )
    _, doc = search(fake, "key = DEMO-1", fields=["comment"])
    embedded = doc["issues"][0]["fields"]["comment"]
    assert (len(embedded["comments"]), embedded["total"]) == (20, 25)


# --- V-13: timestamps ---------------------------------------------------------------------


def test_timestamps_have_an_offset_without_a_colon(fake: FakeProvider) -> None:
    _, doc = fake.json("GET", f"{API}/issue/DEMO-1?fields=created,updated")
    assert re.fullmatch(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d\.000-0600", doc["fields"]["created"])


def test_timestamps_have_a_colon_when_the_quirk_is_off(fake_factory: Factory) -> None:
    provider = fake_factory(Quirks(colonless_offsets=False))
    _, doc = provider.json("GET", f"{API}/issue/DEMO-1?fields=created")
    assert doc["fields"]["created"].endswith("-06:00")


def test_sprint_dates_are_utc_with_z(fake: FakeProvider) -> None:
    _, doc = fake.json("GET", f"{AGILE}/sprint/2")
    assert doc["startDate"].endswith("Z")


def test_utc_stamps_end_in_z_and_local_stamps_in_a_colonless_offset(fake: FakeProvider) -> None:
    assert FakeProvider.utc_stamp(fake.now) == "2026-03-04T15:00:00.000Z"
    assert fake.stamp(fake.now) == "2026-03-04T09:00:00.000-0600"


# --- ADF variety --------------------------------------------------------------------------


def test_adf_variety_reaches_descriptions_and_comments(fake_factory: Factory) -> None:
    provider = fake_factory(Quirks(adf_variety=True))
    provider.json("POST", f"{API}/issue/DEMO-1/comment", {"body": adf_text("plain")})
    _, issue = provider.json("GET", f"{API}/issue/DEMO-1?fields=description,comment")
    kinds = {n["type"] for n in _walk(issue["fields"]["description"])}
    assert {"table", "mention", "mediaSingle", "panel", "codeBlock", "unknownFutureNode"} <= kinds
    assert issue["fields"]["comment"]["comments"][0]["body"] == adf_variety_doc()


def _walk(node: Any) -> list[dict[str, Any]]:
    out = [node]
    for child in node.get("content", []):
        out += _walk(child)
    return out


# --- V-16: error body shapes --------------------------------------------------------------


@pytest.mark.parametrize("shape", ["messages", "error", "text"])
def test_error_shapes(fake_factory: Factory, shape: str) -> None:
    provider = fake_factory(Quirks(error_shape=shape))
    status, headers, body = provider.request("GET", f"{API}/issue/DEMO-999")
    assert status == 404
    if shape == "messages":
        assert set(json.loads(body)) == {"errorMessages", "errors"}
    elif shape == "error":
        assert set(json.loads(body)) == {"error"}
    else:
        assert headers["content-type"].startswith("text/plain") and not body.startswith(b"{")


def test_natural_error_shapes_by_status(fake: FakeProvider) -> None:
    assert "errorMessages" in fake.json("GET", f"{API}/issue/DEMO-999")[1]
    assert fake.request("GET", f"{API}/myself", auth=False)[2].decode().startswith("Client must")
    status, doc = fake.json("PUT", f"{API}/issue/DEMO-1", {"fields": {"nope": 1}})
    assert status == 400 and "nope" in doc["errors"]


def test_unknown_path_is_404_and_wrong_method_is_405(fake: FakeProvider) -> None:
    assert fake.json("GET", f"{API}/nothing")[0] == 404
    assert fake.request("DELETE", f"{API}/myself")[0] == 405


# --- V-15 and 429: rate limits ----------------------------------------------------------------


def test_every_response_carries_rate_limit_headers(fake: FakeProvider) -> None:
    for path in (f"{API}/myself", f"{API}/issue/DEMO-999"):
        _, headers, _ = fake.request("GET", path)
        assert {
            "x-ratelimit-limit",
            "x-ratelimit-remaining",
            "ratelimit",
            "ratelimit-policy",
        } <= set(headers)
    _, headers, _ = fake.request("GET", f"{API}/myself", auth=False)
    assert "x-ratelimit-limit" in headers


def test_rate_limit_remaining_can_be_pinned_and_headers_switched_off(fake_factory: Factory) -> None:
    pinned = fake_factory(Quirks(rate_limit_limit=100, rate_limit_remaining=5))
    _, headers, _ = pinned.request("GET", f"{API}/myself")
    assert (headers["x-ratelimit-limit"], headers["x-ratelimit-remaining"]) == ("100", "5")
    off = fake_factory(Quirks(rate_limit_headers=False))
    assert "x-ratelimit-limit" not in off.request("GET", f"{API}/myself")[1]


@pytest.mark.parametrize(
    ("fmt", "check"),
    [
        ("seconds", lambda v: v == "7"),
        ("http_date", lambda v: v.endswith("GMT")),
        ("junk", lambda v: v == "soon"),
        ("none", lambda v: v is None),
    ],
)
def test_429_retry_after_formats(
    fake_factory: Factory, fmt: str, check: Callable[[Any], bool]
) -> None:
    provider = fake_factory(Quirks(retry_after_format=fmt))
    provider.inject(Fault("status", status=429, retry_after=7))
    status, headers, _ = provider.request("GET", f"{API}/myself")
    assert status == 429 and check(headers.get("retry-after"))
    assert provider.request("GET", f"{API}/myself")[0] == 200  # the fault was spent


def test_a_429_carries_no_retry_after_by_default(fake: FakeProvider) -> None:
    # V-15: no Retry-After was ever seen live, so a test must opt in to one.
    fake.inject(Fault("status", status=429))
    status, headers, _ = fake.request("GET", f"{API}/myself")
    assert status == 429 and "retry-after" not in headers


# --- V-7, V-8, V-30: transitions ----------------------------------------------------------------


def _transitions(fake: FakeProvider, key: str) -> dict[str, Any]:
    _, doc = fake.json("GET", f"{API}/issue/{key}/transitions?expand=transitions.fields")
    return {t["name"]: t for t in doc["transitions"]}


def test_transition_ids_are_the_same_from_every_status(fake: FakeProvider) -> None:
    ids = [
        {n: t["id"] for n, t in _transitions(fake, k).items()}
        for k in ("DEMO-1", "DEMO-2", "DEMO-3")
    ]
    assert ids[0] == ids[1] == ids[2]


def test_done_transition_lists_a_required_resolution_with_five_values(fake: FakeProvider) -> None:
    field = _transitions(fake, "DEMO-2")["Finish"]["fields"]["resolution"]
    assert field["required"] and len(field["allowedValues"]) == 5
    # a transition that is not to a done status asks for no fields at all
    assert _transitions(fake, "DEMO-2")["Start progress"]["fields"] == {}


def test_done_transition_is_accepted_without_a_resolution(fake: FakeProvider) -> None:
    assert (
        fake.json("POST", f"{API}/issue/DEMO-2/transitions", {"transition": {"id": "41"}})[0] == 204
    )
    _, doc = fake.json("GET", f"{API}/issue/DEMO-2?fields=status,resolution")
    assert doc["fields"]["status"]["name"] == "Done" and doc["fields"]["resolution"] is None


def test_done_transition_with_a_resolution_sets_it(fake: FakeProvider) -> None:
    body = {"transition": {"id": "41"}, "fields": {"resolution": {"name": "Won't Do"}}}
    assert fake.json("POST", f"{API}/issue/DEMO-2/transitions", body)[0] == 204
    assert fake.tickets["DEMO-2"].resolution == "Won't Do"


def test_done_without_resolution_is_400_when_the_quirk_is_off(fake_factory: Factory) -> None:
    provider = fake_factory(Quirks(done_without_resolution=False))
    status, doc = provider.json(
        "POST", f"{API}/issue/DEMO-2/transitions", {"transition": {"id": "41"}}
    )
    assert status == 400 and "resolution" in doc["errors"]
    assert provider.tickets["DEMO-2"].status == "In Progress"


def test_leaving_the_backlog_needs_an_assignee_and_applies_nothing(fake: FakeProvider) -> None:
    status, doc = fake.json("POST", f"{API}/issue/DEMO-1/transitions", {"transition": {"id": "31"}})
    assert status == 400 and doc["errorMessages"]
    assert fake.tickets["DEMO-1"].status == "Backlog" and not fake.applied
    # neither the transitions list nor the edit metadata shows the validator: the transition is
    # listed as available and unconditional, with no field, and the edit metadata holds exactly
    # the editable fields
    listed = _transitions(fake, "DEMO-1")
    assert set(listed) == {"Start progress", "Finish", "Back to backlog"}
    assert listed["Start progress"]["isAvailable"] and not listed["Start progress"]["isConditional"]
    assert listed["Start progress"]["fields"] == {}
    editable = set(fake.json("GET", f"{API}/issue/DEMO-1/editmeta")[1]["fields"])
    assert editable == {
        "summary",
        "description",
        "priority",
        "duedate",
        F_POINTS,
        F_CRITERIA,
        *EXTRA_FLOATS,
    }
    # and the validator still answers the same 400 afterwards
    again = fake.json("POST", f"{API}/issue/DEMO-1/transitions", {"transition": {"id": "31"}})
    assert again[0] == 400 and again[1]["errorMessages"] == [
        "Assignee is required to progress this issue."
    ]


def test_assigning_first_lets_the_transition_through(fake: FakeProvider) -> None:
    assert fake.json("PUT", f"{API}/issue/DEMO-1/assignee", {"accountId": ME_ACCOUNT})[0] == 204
    assert (
        fake.json("POST", f"{API}/issue/DEMO-1/transitions", {"transition": {"id": "31"}})[0] == 204
    )
    assert fake.tickets["DEMO-1"].status == "In Progress"


def test_validator_message_is_configurable_for_locales(fake_factory: Factory) -> None:
    provider = fake_factory(Quirks(validator_message="Se requiere un asignado."))
    _, doc = provider.json("POST", f"{API}/issue/DEMO-1/transitions", {"transition": {"id": "31"}})
    assert doc["errorMessages"] == ["Se requiere un asignado."]


def test_validator_can_be_switched_off(fake_factory: Factory) -> None:
    provider = fake_factory(Quirks(assignee_validator=False))
    assert (
        provider.json("POST", f"{API}/issue/DEMO-1/transitions", {"transition": {"id": "31"}})[0]
        == 204
    )


def test_subtask_done_needs_an_assignee_and_a_parent(fake: FakeProvider) -> None:
    sub = fake.add_ticket("DEMO-20", "sub", type_id="3", status="In Progress", parent="DEMO-1")
    assert (
        fake.json("POST", f"{API}/issue/DEMO-20/transitions", {"transition": {"id": "41"}})[0]
        == 400
    )
    sub.assignee = ME_ACCOUNT
    assert (
        fake.json("POST", f"{API}/issue/DEMO-20/transitions", {"transition": {"id": "41"}})[0]
        == 204
    )


def test_unknown_transition_and_unknown_field_are_400(fake: FakeProvider) -> None:
    assert (
        fake.json("POST", f"{API}/issue/DEMO-2/transitions", {"transition": {"id": "99"}})[0] == 400
    )
    body = {"transition": {"id": "41"}, "fields": {"nope": 1}}
    assert fake.json("POST", f"{API}/issue/DEMO-2/transitions", body)[0] == 400


# --- V-9, V-10, V-18, V-23, V-24, V-26 to V-29: writes ----------------------------------------


def test_identical_creates_make_duplicates(fake: FakeProvider) -> None:
    body = {
        "fields": {"project": {"key": "DEMO"}, "issuetype": {"name": "Task"}, "summary": "Same"}
    }
    a = fake.json("POST", f"{API}/issue", body)
    b = fake.json("POST", f"{API}/issue", body)
    assert a[0] == b[0] == 201 and a[1]["key"] != b[1]["key"]


def test_identical_comments_are_not_deduplicated(fake: FakeProvider) -> None:
    for _ in range(2):
        assert (
            fake.json("POST", f"{API}/issue/DEMO-1/comment", {"body": adf_text("same")})[0] == 201
        )
    assert fake.json("GET", f"{API}/issue/DEMO-1/comment")[1]["total"] == 2


def test_update_is_204_and_resending_an_unchanged_value_is_a_noop(fake: FakeProvider) -> None:
    body = {"fields": {"summary": "New title"}}
    assert fake.request("PUT", f"{API}/issue/DEMO-1", body)[:1] == (204,)
    assert len(fake.applied) == 1
    assert fake.request("PUT", f"{API}/issue/DEMO-1", body)[2] == b""
    assert len(fake.applied) == 1


def test_a_failed_multi_field_update_applies_nothing(fake: FakeProvider) -> None:
    body = {"fields": {"summary": "Half applied?", "nope": 1}}
    status, doc = fake.json("PUT", f"{API}/issue/DEMO-1", body)
    assert status == 400 and "nope" in doc["errors"]
    assert fake.tickets["DEMO-1"].summary == "Seeded backlog task"


def test_non_screen_field_and_labels_are_not_in_editmeta(fake: FakeProvider) -> None:
    fields = fake.json("GET", f"{API}/issue/DEMO-1/editmeta")[1]["fields"]
    assert "labels" not in fields and "summary" in fields


def test_labels_are_writable_though_editmeta_omits_them(fake: FakeProvider) -> None:
    body = {"update": {"labels": [{"add": "beta"}, {"remove": "alpha"}]}}
    assert fake.json("PUT", f"{API}/issue/DEMO-1", body)[0] == 204
    assert sorted(fake.tickets["DEMO-1"].labels) == ["beta"]


def test_label_with_a_space_is_400_and_255_chars_is_fine(fake: FakeProvider) -> None:
    assert (
        fake.json("PUT", f"{API}/issue/DEMO-1", {"update": {"labels": [{"add": "a b"}]}})[0] == 400
    )
    assert (
        fake.json("PUT", f"{API}/issue/DEMO-1", {"update": {"labels": [{"add": "x" * 255}]}})[0]
        == 204
    )
    assert (
        fake.json("PUT", f"{API}/issue/DEMO-1", {"update": {"labels": [{"add": "x" * 256}]}})[0]
        == 400
    )


def test_adding_an_existing_label_or_removing_an_absent_one_is_a_204_noop(
    fake: FakeProvider,
) -> None:
    body = {"update": {"labels": [{"add": "alpha"}, {"remove": "never-there"}]}}
    assert fake.json("PUT", f"{API}/issue/DEMO-1", body)[0] == 204
    assert not fake.applied


def test_notify_users_false_is_403_for_a_non_admin(fake: FakeProvider) -> None:
    assert (
        fake.json("PUT", f"{API}/issue/DEMO-1?notifyUsers=false", {"fields": {"summary": "x"}})[0]
        == 403
    )


def test_closed_tickets_still_accept_label_comment_writes(fake: FakeProvider) -> None:
    assert (
        fake.json("PUT", f"{API}/issue/DEMO-3", {"update": {"labels": [{"add": "late"}]}})[0] == 204
    )
    assert fake.json("POST", f"{API}/issue/DEMO-3/comment", {"body": adf_text("late")})[0] == 201


def _create(fake: FakeProvider, **fields: Any) -> tuple[int, Any]:
    base = {"project": {"key": "DEMO"}, "issuetype": {"name": "Task"}, "summary": "New one"}
    return fake.json("POST", f"{API}/issue", {"fields": {**base, **fields}})


def test_create_accepts_labels_though_createmeta_does_not_list_them(fake: FakeProvider) -> None:
    listed = fake.json("GET", f"{API}/issue/createmeta/DEMO/issuetypes/1")[1]["fields"]
    assert "labels" not in {f["fieldId"] for f in listed}
    status, doc = _create(fake, labels=["tc-0123456789abcdef"])
    assert status == 201
    assert fake.tickets[doc["key"]].labels.keys() == {"tc-0123456789abcdef"}


def test_create_accepts_update_labels(fake: FakeProvider) -> None:
    body = {
        "fields": {"project": {"key": "DEMO"}, "issuetype": {"id": "1"}, "summary": "s"},
        "update": {"labels": [{"add": "viaupdate"}]},
    }
    status, doc = fake.json("POST", f"{API}/issue", body)
    assert status == 201 and "viaupdate" in fake.tickets[doc["key"]].labels


def test_create_silently_ignores_the_sprint_field(fake: FakeProvider) -> None:
    status, doc = _create(fake, **{F_SPRINT: 2})
    assert status == 201 and fake.tickets[doc["key"]].sprints == []


def test_create_parent_rules(fake: FakeProvider) -> None:
    assert _create(fake, issuetype={"name": "Sub-task"})[0] == 400  # no parent
    status, doc = _create(fake, parent={"key": "DEMO-1"})  # Task under Task
    assert status == 400 and "parent" in doc["errors"]
    assert _create(fake, issuetype={"name": "Sub-task"}, parent={"key": "DEMO-1"})[0] == 201


def test_create_validation(fake: FakeProvider) -> None:
    assert _create(fake, summary="  ")[0] == 400
    assert _create(fake, issuetype={"name": "Nope"})[0] == 400
    assert _create(fake, project={"key": "ZZNOPE"})[0] == 400
    assert _create(fake, nonsense=1)[0] == 400
    assert _create(fake, description="plain string")[0] == 400  # v3 wants ADF
    assert fake.json("POST", f"{API}/issue", [1])[0] == 400
    assert fake.request("POST", f"{API}/issue", b"{not json")[0] == 400


def test_create_reports_the_caller_as_reporter(fake: FakeProvider) -> None:
    _, doc = _create(fake)
    assert fake.tickets[doc["key"]].reporter == ME_ACCOUNT


def test_assign_unknown_account_is_400_and_null_unassigns(fake: FakeProvider) -> None:
    assert fake.json("PUT", f"{API}/issue/DEMO-2/assignee", {"accountId": "acct-nope"})[0] == 400
    assert fake.json("PUT", f"{API}/issue/DEMO-2/assignee", {"accountId": None})[0] == 204
    assert fake.tickets["DEMO-2"].assignee is None


# --- comments, V-12, V-19, V-21 ------------------------------------------------------------------


def test_comment_requires_adf_and_caps_the_text(fake: FakeProvider) -> None:
    assert fake.json("POST", f"{API}/issue/DEMO-1/comment", {"body": "wiki *markup*"})[0] == 400
    ok = fake.json("POST", f"{API}/issue/DEMO-1/comment", {"body": adf_text("x" * 32767)})
    assert ok[0] == 201
    status, doc = fake.json("POST", f"{API}/issue/DEMO-1/comment", {"body": adf_text("x" * 32768)})
    assert status == 400 and doc["errors"]["comment"] == "CONTENT_LIMIT_EXCEEDED"


def test_comment_properties_round_trip_with_expand(fake: FakeProvider) -> None:
    prop = {"key": "demo.meta", "value": {"v": 1}}
    fake.json("POST", f"{API}/issue/DEMO-1/comment", {"body": adf_text("a"), "properties": [prop]})
    _, plain = fake.json("GET", f"{API}/issue/DEMO-1/comment")
    _, rich = fake.json("GET", f"{API}/issue/DEMO-1/comment?expand=properties")
    assert "properties" not in plain["comments"][0]
    assert rich["comments"][0]["properties"] == [prop]


def test_property_limits(fake: FakeProvider) -> None:
    big = {"key": "k", "value": "x" * 40000}
    assert (
        fake.json(
            "POST", f"{API}/issue/DEMO-1/comment", {"body": adf_text("a"), "properties": [big]}
        )[0]
        == 400
    )
    long_key = {"key": "k" * 256, "value": 1}
    assert (
        fake.json(
            "POST", f"{API}/issue/DEMO-1/comment", {"body": adf_text("a"), "properties": [long_key]}
        )[0]
        == 400
    )


def test_comment_order_and_paging(fake: FakeProvider) -> None:
    for i in range(3):
        fake.json("POST", f"{API}/issue/DEMO-1/comment", {"body": adf_text(f"c{i}")})
    _, newest = fake.json("GET", f"{API}/issue/DEMO-1/comment?orderBy=-created&maxResults=2")
    texts = [c["body"]["content"][0]["content"][0]["text"] for c in newest["comments"]]
    assert texts == ["c2", "c1"] and newest["total"] == 3
    assert fake.json("GET", f"{API}/issue/DEMO-1/comment?orderBy=bogus")[0] == 400


# --- V-20, V-33: label lag ----------------------------------------------------------------------


def test_a_label_becomes_searchable_only_after_the_lag(fake_factory: Factory) -> None:
    provider = fake_factory(Quirks(label_lag_s=0.6))
    provider.json("PUT", f"{API}/issue/DEMO-1", {"update": {"labels": [{"add": "fresh"}]}})
    assert keys(search(provider, "labels = fresh")[1]) == []
    # the label is on the ticket at once
    assert (
        "fresh" in provider.json("GET", f"{API}/issue/DEMO-1?fields=labels")[1]["fields"]["labels"]
    )
    provider.advance(0.7)
    assert keys(search(provider, "labels = fresh")[1]) == ["DEMO-1"]


def test_a_label_set_at_create_lags_too(fake_factory: Factory) -> None:
    provider = fake_factory(Quirks(label_lag_s=2))
    status, doc = _create(provider, labels=["tc-aaaaaaaaaaaaaaaa"])
    assert status == 201
    assert keys(search(provider, "labels = tc-aaaaaaaaaaaaaaaa")[1]) == []
    provider.advance(2)
    assert keys(search(provider, "labels = tc-aaaaaaaaaaaaaaaa")[1]) == [doc["key"]]


def test_properties_are_never_searchable(fake: FakeProvider) -> None:
    fake.json(
        "POST",
        f"{API}/issue/DEMO-1/comment",
        {"body": adf_text("a"), "properties": [{"key": "p", "value": 1}]},
    )
    assert search(fake, "issue.property[p].v = 1")[1]["issues"] == []


# --- metadata and agile (V-6, V-17) --------------------------------------------------------------


def test_field_list_has_two_story_point_fields_and_a_sprint_field(fake: FakeProvider) -> None:
    _, fields = fake.json("GET", f"{API}/field")
    by_id = {f["id"]: f for f in fields}
    assert by_id[F_POINTS]["name"] == "Story Points"
    assert by_id[F_POINTS]["schema"]["custom"].endswith(":float")
    assert [f for f in fields if f["schema"].get("custom", "").endswith(":jsw-story-points")]
    assert by_id[F_SPRINT]["schema"]["custom"].endswith(":gh-sprint")
    floats = [f for f in fields if f["schema"].get("custom", "").endswith(":float")]
    # the generic ``:float`` schema is shared by several fields; only the name identifies one
    assert {f["name"] for f in floats} == {"Story Points", "Business value", "Effort days"}
    assert {F_VALUE, F_EFFORT} <= {f["id"] for f in floats}


def test_resolutions_priorities_and_createmeta(fake: FakeProvider) -> None:
    assert len(fake.json("GET", f"{API}/resolution")[1]) == 5
    assert fake.json("GET", f"{API}/priority")[1][0]["name"] == "Highest"
    types = fake.json("GET", f"{API}/issue/createmeta/DEMO/issuetypes")[1]["issueTypes"]
    assert {t["name"] for t in types} >= {"Task", "Sub-task"}
    assert fake.json("GET", f"{API}/issue/createmeta/ZZNOPE/issuetypes")[0] == 404


def test_sprint_field_is_an_array_of_objects(fake: FakeProvider) -> None:
    _, doc = fake.json("GET", f"{API}/issue/DEMO-2?fields={F_SPRINT}")
    assert doc["fields"][F_SPRINT][0]["state"] == "active"


def test_agile_boards_and_sprints(fake: FakeProvider) -> None:
    assert fake.json("GET", f"{AGILE}/board")[1]["values"][0]["id"] == 1
    assert fake.json("GET", f"{AGILE}/board?projectKeyOrId=ZZNOPE")[1]["values"] == []
    _, active = fake.json("GET", f"{AGILE}/board/1/sprint?state=active")
    assert [s["id"] for s in active["values"]] == [2]
    assert fake.json("GET", f"{AGILE}/board/9/sprint")[0] == 404
    assert fake.json("GET", f"{AGILE}/sprint/99")[0] == 404


def test_sprint_add_and_remove_are_204_and_remove_goes_to_the_backlog(fake: FakeProvider) -> None:
    assert fake.json("POST", f"{AGILE}/sprint/3/issue", {"issues": ["DEMO-1"]})[0] == 204
    assert fake.tickets["DEMO-1"].sprints == [3]
    assert fake.json("POST", f"{AGILE}/backlog/issue", {"issues": ["DEMO-1", "DEMO-2"]})[0] == 204
    assert fake.tickets["DEMO-1"].sprints == [] and fake.tickets["DEMO-2"].sprints == []
    assert fake.json("POST", f"{AGILE}/sprint/1/issue", {"issues": ["DEMO-1"]})[0] == 400  # closed
    assert fake.json("POST", f"{AGILE}/sprint/3/issue", {"issues": []})[0] == 400


def test_closed_sprint_membership_survives_the_backlog_call(fake: FakeProvider) -> None:
    assert fake.json("POST", f"{AGILE}/backlog/issue", {"issues": ["DEMO-3"]})[0] == 204
    assert fake.tickets["DEMO-3"].sprints == [1]


# --- failure injection ------------------------------------------------------------------------


def test_reset_before_apply_loses_the_write_cleanly(fake: FakeProvider) -> None:
    fake.inject(Fault("reset", method="PUT"))
    with pytest.raises(OSError):
        fake.request("PUT", f"{API}/issue/DEMO-1", {"fields": {"summary": "lost"}})
    assert fake.tickets["DEMO-1"].summary == "Seeded backlog task" and not fake.applied


def test_reset_after_apply_lands_the_write_without_an_answer(fake: FakeProvider) -> None:
    fake.inject(Fault("reset", method="PUT", after_apply=True))
    with pytest.raises(OSError):
        fake.request("PUT", f"{API}/issue/DEMO-1", {"fields": {"summary": "landed"}})
    assert fake.tickets["DEMO-1"].summary == "landed" and len(fake.applied) == 1


def test_close_before_and_after_apply(fake: FakeProvider) -> None:
    fake.inject(Fault("close", method="POST", path="comment"))
    with pytest.raises(http.client.HTTPException):
        fake.request("POST", f"{API}/issue/DEMO-1/comment", {"body": adf_text("a")})
    assert not fake.tickets["DEMO-1"].comments
    fake.inject(Fault("close", method="POST", path="comment", after_apply=True))
    with pytest.raises(http.client.HTTPException):
        fake.request("POST", f"{API}/issue/DEMO-1/comment", {"body": adf_text("b")})
    assert len(fake.tickets["DEMO-1"].comments) == 1


def test_headers_then_a_dropped_body(fake: FakeProvider) -> None:
    fake.inject(Fault("headers_only"))
    with pytest.raises(http.client.IncompleteRead):
        fake.request("GET", f"{API}/myself")


def test_truncated_body(fake: FakeProvider) -> None:
    fake.inject(Fault("truncated"))
    with pytest.raises(http.client.IncompleteRead):
        fake.request("GET", f"{API}/myself")


def test_5xx_is_plain_text_and_applies_nothing(fake: FakeProvider) -> None:
    fake.inject(Fault("status", status=502, body="Bad Gateway", times=2))
    for _ in range(2):
        status, _, body = fake.request("PUT", f"{API}/issue/DEMO-1", {"fields": {"summary": "x"}})
        assert (status, body) == (502, b"Bad Gateway")
    assert not fake.applied


def test_a_fault_matches_by_method_and_path_and_counts_down(fake: FakeProvider) -> None:
    fault = fake.inject(Fault("status", status=503, method="GET", path=r"/myself$", times=1))
    assert fake.request("GET", f"{API}/field")[0] == 200
    assert fake.request("GET", f"{API}/myself")[0] == 503
    assert fake.request("GET", f"{API}/myself")[0] == 200
    assert fault.hits == 1


def test_slow_response(fake: FakeProvider) -> None:
    fake.inject(Fault("slow", delay_s=0.4))
    started = time.monotonic()
    assert fake.request("GET", f"{API}/myself")[0] == 200
    assert time.monotonic() - started >= 0.35


def test_drip_sends_one_byte_at_a_time(fake: FakeProvider) -> None:
    fake.inject(Fault("drip", delay_s=0.01, path="/myself"))
    started = time.monotonic()
    assert fake.request("GET", f"{API}/myself")[0] == 200
    assert time.monotonic() - started > 0.5


def test_response_delay_quirk(fake_factory: Factory) -> None:
    provider = fake_factory(Quirks(response_delay_s=0.3))
    started = time.monotonic()
    provider.request("GET", f"{API}/myself")
    assert time.monotonic() - started >= 0.25


# --- through the real gateway ------------------------------------------------------------------


def test_gateway_reads_myself_through_the_fake(fake: FakeProvider) -> None:
    resp = make_gateway(fake).request("GET", f"{API}/myself", template=f"{API}/myself")
    assert resp.status == 200 and resp.data["accountId"] == ME_ACCOUNT
    assert resp.rate_limit is not None and resp.rate_limit["limit"] == 1000
    call = fake.calls[0]
    assert call.headers["authorization"][0] == fake.basic_header()


def test_gateway_maps_401_and_403(fake: FakeProvider) -> None:
    with pytest.raises(GatewayError) as bad:
        make_gateway(fake, token="wrong-token").request(
            "GET", f"{API}/myself", template=f"{API}/myself"
        )
    assert bad.value.code == "bad_credentials"
    fake.inject(Fault("status", status=403, body="Forbidden"))
    with pytest.raises(GatewayError) as forbidden:
        make_gateway(fake).request("GET", f"{API}/myself", template=f"{API}/myself")
    assert forbidden.value.code == "forbidden"


def test_gateway_retries_a_read_after_a_reset_and_after_a_503(fake: FakeProvider) -> None:
    fake.inject(Fault("reset", method="GET", times=1))
    fake.inject(Fault("status", method="GET", status=503, times=1))
    resp = make_gateway(fake).request("GET", f"{API}/myself", template=f"{API}/myself")
    assert resp.status == 200 and len(fake.calls) == 3


def test_gateway_honors_a_whole_second_retry_after_and_ignores_a_date(
    fake_factory: Factory,
) -> None:
    slept: list[float] = []
    for fmt in ("seconds", "http_date"):
        provider = fake_factory(Quirks(retry_after_format=fmt))
        provider.inject(Fault("status", status=429, retry_after=3))
        gw = make_gateway(provider, sleep=slept.append)
        assert gw.request("GET", f"{API}/myself", template=f"{API}/myself").status == 200
    assert slept[0] == 3 and slept[1] != 3


def test_gateway_does_not_retry_a_write_after_a_reset_that_landed(fake: FakeProvider) -> None:
    fake.inject(Fault("reset", method="PUT", after_apply=True, times=None))
    with pytest.raises(GatewayError) as err:
        make_gateway(fake).request(
            "PUT",
            f"{API}/issue/DEMO-1",
            template=f"{API}/issue/{{key}}",
            body={"fields": {"summary": "once"}},
        )
    assert err.value.outcome_unknown
    assert len(fake.applied) == 1 and len(fake.calls) == 1


def test_gateway_410_on_old_search_is_a_410_response(fake: FakeProvider) -> None:
    resp = make_gateway(fake).request(
        "POST",
        f"{API}/search",
        template=f"{API}/search",
        body={"jql": "project = DEMO"},
        write=False,
    )
    assert resp.status == 410


def test_gateway_pages_the_search_to_the_end(fake: FakeProvider) -> None:
    gw = make_gateway(fake)
    token: str | None = None
    seen: list[str] = []
    while True:
        body: dict[str, Any] = {
            "jql": "project = DEMO ORDER BY key",
            "maxResults": 2,
            "fields": ["summary"],
        }
        if token:
            body["nextPageToken"] = token
        data = gw.request(
            "POST", f"{API}/search/jql", template=f"{API}/search/jql", body=body, write=False
        ).data
        seen += keys(data)
        token = data.get("nextPageToken")
        if not token:
            break
    assert seen == ["DEMO-1", "DEMO-2", "DEMO-3"]


def test_clock_advance_moves_timestamps(fake: FakeProvider) -> None:
    before = fake.now
    fake.advance(60)
    assert fake.now - before == timedelta(seconds=60)


def test_other_account_constants_are_distinct() -> None:
    assert len({ME_ACCOUNT, OTHER_ACCOUNT}) == 2


# --- review round 1: label lag defaults (V-20, V-33) ---------------------------------------------


def test_default_quirks_hide_a_new_label_from_search_until_the_clock_advances(
    fake: FakeProvider,
) -> None:
    assert fake.quirks.label_lag_s == 1.0
    fake.json("PUT", f"{API}/issue/DEMO-1", {"update": {"labels": [{"add": "fresh"}]}})
    assert keys(search(fake, "labels = fresh")[1]) == []
    fake.advance(1)
    assert keys(search(fake, "labels = fresh")[1]) == ["DEMO-1"]


def test_label_lag_zero_opts_out_of_the_lag(fake_factory: Factory) -> None:
    provider = fake_factory(Quirks(label_lag_s=0))
    provider.json("PUT", f"{API}/issue/DEMO-1", {"update": {"labels": [{"add": "fresh"}]}})
    # the applied write ticks the clock once, which is all the lag a zero setting leaves
    assert keys(search(provider, "labels = fresh")[1]) == ["DEMO-1"]


# --- review round 1: faults (items 15, 16, 18, 27) -----------------------------------------------


def test_a_5xx_after_apply_lands_the_comment_and_the_client_sees_503(fake: FakeProvider) -> None:
    fake.inject(Fault("status", status=503, method="POST", path="comment", after_apply=True))
    status, _, body = fake.request(
        "POST", f"{API}/issue/DEMO-1/comment", {"body": adf_text("landed")}
    )
    assert (status, body) == (503, b"Service Unavailable")
    assert len(fake.tickets["DEMO-1"].comments) == 1 and len(fake.applied) == 1
    assert fake.calls[-1].status == 503


def test_headers_only_on_a_204_put_advertises_bytes_that_never_come(fake: FakeProvider) -> None:
    fake.inject(Fault("headers_only", method="PUT"))
    raw = raw_exchange(
        fake,
        f"PUT {API}/issue/DEMO-1 HTTP/1.1",
        json.dumps({"fields": {"summary": "landed"}}).encode(),
    )
    assert raw.startswith(b"HTTP/1.1 204")
    advertised, received = advertised_and_received(raw)
    assert advertised > 0 and received == 0
    assert fake.tickets["DEMO-1"].summary == "landed"


def test_truncated_on_a_204_post_advertises_more_than_it_sends(fake: FakeProvider) -> None:
    fake.json("PUT", f"{API}/issue/DEMO-2/assignee", {"accountId": ME_ACCOUNT})
    fake.inject(Fault("truncated", method="POST", path="transitions"))
    raw = raw_exchange(
        fake,
        f"POST {API}/issue/DEMO-2/transitions HTTP/1.1",
        json.dumps({"transition": {"id": "41"}}).encode(),
    )
    assert raw.startswith(b"HTTP/1.1 204")
    advertised, received = advertised_and_received(raw)
    assert advertised > received
    assert fake.tickets["DEMO-2"].status == "Done"


def test_truncated_on_a_204_put_advertises_more_than_it_sends(fake: FakeProvider) -> None:
    fake.inject(Fault("truncated", method="PUT"))
    raw = raw_exchange(
        fake,
        f"PUT {API}/issue/DEMO-1 HTTP/1.1",
        json.dumps({"fields": {"summary": "again"}}).encode(),
    )
    advertised, received = advertised_and_received(raw)
    assert advertised > received


def test_headers_only_on_a_204_post_advertises_bytes_that_never_come(fake: FakeProvider) -> None:
    fake.json("PUT", f"{API}/issue/DEMO-2/assignee", {"accountId": ME_ACCOUNT})
    fake.inject(Fault("headers_only", method="POST", path="transitions"))
    raw = raw_exchange(
        fake,
        f"POST {API}/issue/DEMO-2/transitions HTTP/1.1",
        json.dumps({"transition": {"id": "41"}}).encode(),
    )
    advertised, received = advertised_and_received(raw)
    assert advertised > 0 and received == 0


def test_slow_wakes_on_close_and_does_not_dispatch(fake_factory: Factory) -> None:
    provider = fake_factory(None)
    provider.inject(Fault("slow", method="PUT", delay_s=30))
    box: list[object] = []

    def call() -> None:
        try:
            box.append(provider.request("PUT", f"{API}/issue/DEMO-1", {"fields": {"summary": "x"}}))
        except OSError as exc:
            box.append(exc)

    thread = threading.Thread(target=call)
    thread.start()
    time.sleep(0.2)
    provider.close()
    thread.join(10)
    assert not thread.is_alive()
    assert not provider.applied and provider.tickets["DEMO-1"].summary == "Seeded backlog task"
    assert isinstance(box[0], (OSError, http.client.HTTPException))


def test_slow_after_apply_lands_the_write_first(fake_factory: Factory) -> None:
    provider = fake_factory(None)
    provider.inject(Fault("slow", method="PUT", delay_s=30, after_apply=True))
    thread = threading.Thread(
        target=lambda: _quiet(
            provider.request, "PUT", f"{API}/issue/DEMO-1", {"fields": {"summary": "x"}}
        )
    )
    thread.start()
    deadline = time.monotonic() + 5
    while not provider.applied and time.monotonic() < deadline:
        time.sleep(0.01)
    assert provider.tickets["DEMO-1"].summary == "x"  # applied while the answer still sleeps
    provider.close()
    thread.join(10)
    assert not thread.is_alive()


def _quiet(fn: Callable[..., Any], *args: Any) -> None:
    try:
        fn(*args)
    except (OSError, http.client.HTTPException):
        pass


def test_unknown_fault_and_quirk_values_are_rejected() -> None:
    with pytest.raises(ValueError):
        Fault("explode")
    with pytest.raises(ValueError):
        Quirks(error_shape="xml")
    with pytest.raises(ValueError):
        Quirks(retry_after_format="fortnight")


# --- review round 1: unexpected input never drops the connection (item 17) ------------------------


def test_wrong_typed_input_is_a_400_and_never_a_dropped_connection(fake: FakeProvider) -> None:
    ok = {"project": {"key": "DEMO"}, "issuetype": {"name": "Task"}, "summary": "s"}
    # Every case is client-malformed input and so a 400, except the two marked below.
    cases: list[tuple[str, str, Any]] = [
        ("POST", f"{API}/issue/DEMO-2/transitions", {"transition": "31"}),
        ("POST", f"{API}/issue/DEMO-2/transitions", {"transition": {"id": "41"}, "fields": 5}),
        (
            "POST",
            f"{API}/issue/DEMO-2/transitions",
            {"transition": {"id": "41"}, "fields": {"resolution": "Done"}},
        ),
        ("GET", f"{API}/issue/DEMO-1/comment?startAt=x", None),
        ("GET", f"{API}/issue/DEMO-1/comment?maxResults=" + "9" * 5000, None),
        ("POST", f"{API}/issue", {"fields": {**ok, "project": "DEMO"}}),
        ("POST", f"{API}/issue", {"fields": {**ok, "issuetype": "Task"}}),
        ("POST", f"{API}/issue", {"fields": {**ok, "parent": {"key": ["DEMO-1"]}}}),
        ("POST", f"{API}/issue", {"fields": {**ok, "labels": [1]}}),
        ("POST", f"{API}/issue", {"fields": {**ok, F_POINTS: "many"}}),
        ("POST", f"{API}/issue", {"fields": ok, "properties": {"a": 1}}),
        (
            "POST",
            f"{API}/issue/DEMO-1/comment",
            {"body": {"type": "doc", "version": 1, "content": 5}},
        ),
        ("POST", f"{API}/issue/DEMO-1/comment", {"body": adf_text("a"), "properties": 5}),
        (
            "PUT",
            f"{API}/issue/DEMO-1",
            {"fields": {"description": {"type": "doc", "version": 1, "content": [5]}}},
        ),
        ("PUT", f"{API}/issue/DEMO-1", {"fields": {F_VALUE: True}}),
        ("PUT", f"{API}/issue/DEMO-1/assignee", {"accountId": ["x"]}),
        ("POST", f"{API}/search/jql", {"jql": "project = DEMO", "maxResults": "ten"}),
        ("POST", f"{API}/search/jql", {"jql": "project = DEMO", "nextPageToken": "éé"}),
        ("POST", f"{API}/search/jql", {"jql": "(" * 5000 + "project = DEMO" + ")" * 5000}),
        ("POST", f"{API}/search/jql", b'{"jql": "x", "maxResults": 1' + b"0" * 5000 + b"}"),
        ("POST", f"{API}/search/jql", b"[" * 100000 + b"]" * 100000),
        ("POST", f"{API}/search/jql", b"\xff\xfe\x00"),
        # 200 by design: /jql/parse reports a bad query inside the body, not as an HTTP error
        ("POST", f"{API}/jql/parse", {"queries": ["(" * 5000]}),
        # 404: sprint 3 does not exist, so the sprint is refused before the body is looked at
        ("POST", f"{AGILE}/sprint/3/issue", {"issues": [[1], {"a": 1}]}),
        ("POST", f"{AGILE}/backlog/issue", {"issues": "DEMO-1"}),
    ]
    legit = {("POST", f"{API}/jql/parse"): 200, ("POST", f"{AGILE}/sprint/3/issue"): 404}
    for method, path, body in cases:
        want = legit.get((method, path), 400)
        status = fake.request(method, path, body)[0]
        assert status == want, (method, path[:80], status)
    assert fake.request("GET", f"{API}/myself")[0] == 200  # still serving
    # none of it was an unexpected exception: the fixture teardown also checks this
    assert fake.errors == []


def test_a_huge_sprint_id_and_a_deep_property_are_answered(fake: FakeProvider) -> None:
    assert fake.request("GET", f"{AGILE}/sprint/" + "9" * 5000)[0] == 404
    deep = b"[" * 900 + b"]" * 900
    status = fake.request("PUT", f"{API}/issue/DEMO-1/properties/deep", deep)[0]
    assert status in {200, 201, 400}


def test_non_ascii_authorization_bytes_are_a_401(fake: FakeProvider) -> None:
    payload = (
        f"GET {API}/myself HTTP/1.1\r\nHost: x\r\nConnection: close\r\n".encode()
        + b"Authorization: Basic \xff\xfe\xfd\r\n\r\n"
    )
    with socket.create_connection(("127.0.0.1", fake.port), timeout=5) as sock:
        sock.sendall(payload)
        raw = sock.recv(65536)
    assert raw.startswith(b"HTTP/1.1 401")


def test_an_unexpected_exception_is_recorded_and_answered(fake: FakeProvider) -> None:
    def boom(call: Any) -> Any:
        raise RuntimeError("the fake has a bug")

    def odd(call: Any) -> Any:
        raise ValueError("client-shaped")

    fake._routes.insert(0, ("GET", re.compile(f"{API}/boom"), boom))
    fake._routes.insert(0, ("GET", re.compile(f"{API}/odd"), odd))
    fake.allow_internal_errors = True
    assert fake.json("GET", f"{API}/boom")[0] == 500
    assert fake.json("GET", f"{API}/odd")[0] == 400
    assert [(type(e.exc), e.status, e.client_malformed, e.call.path) for e in fake.errors] == [
        (RuntimeError, 500, False, f"{API}/boom"),
        (ValueError, 400, False, f"{API}/odd"),
    ]
    assert fake.request("GET", f"{API}/myself")[0] == 200


def test_the_fixture_fails_a_test_that_leaves_an_internal_error(
    fake_factory: Factory,
) -> None:
    from tests.conftest import _assert_no_internal_errors

    provider = fake_factory(None)

    def divide(call: Any) -> Any:
        raise ZeroDivisionError

    provider._routes.insert(0, ("GET", re.compile(f"{API}/boom"), divide))
    assert provider.request("GET", f"{API}/boom")[0] == 500
    with pytest.raises(AssertionError, match="internal error"):
        _assert_no_internal_errors(provider)
    provider.allow_internal_errors = True
    _assert_no_internal_errors(provider)
    provider.errors.clear()  # the factory teardown must not fail this test


def test_the_fixture_fails_on_an_unexpected_exception_answered_as_a_400(
    fake_factory: Factory,
) -> None:
    from tests.conftest import _assert_no_internal_errors

    provider = fake_factory(None)

    def odd(call: Any) -> Any:
        raise ValueError("a bug that looks like malformed input")

    provider._routes.insert(0, ("GET", re.compile(f"{API}/odd"), odd))
    assert provider.request("GET", f"{API}/odd")[0] == 400
    with pytest.raises(AssertionError, match="internal error"):
        _assert_no_internal_errors(provider)
    provider.errors.clear()


# --- review round 1: lookups and routing (items 19, 20, 21) --------------------------------------


def test_a_ticket_resolves_by_any_case_key_and_by_numeric_id(fake: FakeProvider) -> None:
    ticket = fake.tickets["DEMO-2"]
    for ref in ("demo-2", "Demo-2", str(ticket.id)):
        status, doc = fake.json("GET", f"{API}/issue/{ref}?fields=summary")
        assert status == 200 and doc["key"] == "DEMO-2", ref


def test_a_moved_ticket_answers_its_old_key_with_the_new_key_in_the_body(
    fake: FakeProvider,
) -> None:
    fake.move("DEMO-2", "AB-7")
    for ref in ("DEMO-2", "demo-2", "AB-7"):
        status, doc = fake.json("GET", f"{API}/issue/{ref}?fields=summary")
        assert status == 200 and doc["key"] == "AB-7", ref
    assert "DEMO-2" not in fake.tickets
    assert fake.json("GET", f"{API}/issue/DEMO-2/comment")[0] == 200


def test_an_encoded_slash_is_not_a_path_separator(fake: FakeProvider) -> None:
    assert fake.json("GET", f"{API}/issue/DEMO-1%2Ftransitions") == (
        404,
        {"errorMessages": MISSING_TICKET, "errors": {}},
    )
    assert fake.request("GET", f"{API}/issue/DEMO-1/transitions")[0] == 200
    # an encoded character inside a segment still decodes
    assert fake.json("GET", f"{API}/issue/DEMO%2D1?fields=summary")[1]["key"] == "DEMO-1"


def test_the_call_keeps_the_raw_target(fake: FakeProvider) -> None:
    fake.request("GET", f"{API}/issue/DEMO-1%2Ftransitions?fields=a%20b")
    call = fake.calls[-1]
    assert call.target == f"{API}/issue/DEMO-1%2Ftransitions?fields=a%20b"
    assert call.one("fields") == "a b"


def test_adding_a_comment_bumps_the_ticket_updated_stamp(fake: FakeProvider) -> None:
    fake.advance(60)
    before = fake.json("GET", f"{API}/issue/DEMO-1?fields=updated")[1]["fields"]["updated"]
    assert fake.json("POST", f"{API}/issue/DEMO-1/comment", {"body": adf_text("bump")})[0] == 201
    after = fake.json("GET", f"{API}/issue/DEMO-1?fields=updated")[1]["fields"]["updated"]
    assert after != before


# --- review round 1: JQL (items 22, 23) ----------------------------------------------------------


def test_a_status_value_compares_case_insensitively_in_the_semantic_check(
    fake: FakeProvider,
) -> None:
    _, doc = search(fake, "status = backlog")
    assert keys(doc) == ["DEMO-1"]
    _, doc = search(fake, 'status in ("IN PROGRESS", done)')
    assert keys(doc) == ["DEMO-2", "DEMO-3"]


def test_relative_dates_are_read_against_the_fake_clock(fake: FakeProvider) -> None:
    fake.tickets["DEMO-1"].updated = fake.now - timedelta(hours=3)
    fake.tickets["DEMO-2"].updated = fake.now - timedelta(days=2)
    fake.tickets["DEMO-3"].updated = fake.now - timedelta(weeks=2)
    assert keys(search(fake, 'updated >= "-4h" ORDER BY key')[1]) == ["DEMO-1"]
    assert keys(search(fake, "updated >= -1d ORDER BY key")[1]) == ["DEMO-1"]
    assert keys(search(fake, "updated >= -3d ORDER BY key")[1]) == ["DEMO-1", "DEMO-2"]
    assert keys(search(fake, "updated >= -3w ORDER BY key")[1]) == ["DEMO-1", "DEMO-2", "DEMO-3"]
    assert keys(search(fake, "updated < -30m AND key = DEMO-1")[1]) == ["DEMO-1"]


def test_date_functions_work(fake: FakeProvider) -> None:
    fake.tickets["DEMO-1"].updated = fake.now
    assert keys(search(fake, "updated <= now() AND key = DEMO-1")[1]) == ["DEMO-1"]
    assert keys(search(fake, "updated >= startOfDay() AND key = DEMO-1")[1]) == ["DEMO-1"]


@pytest.mark.parametrize("jql", ['updated > "soon"', "created = 2026-13-45", 'updated >= "1d"'])
def test_an_unparseable_date_is_a_400_even_with_silent_semantic_errors(
    fake: FakeProvider, jql: str
) -> None:
    status, doc = search(fake, jql)
    assert status == 400 and "invalid" in doc["errorMessages"][0]
    assert fake.json("POST", f"{API}/search/approximate-count", {"jql": jql})[0] == 400
    _, parsed = fake.json("POST", f"{API}/jql/parse", {"queries": [jql]})
    assert parsed["queries"][0]["errors"]


@pytest.mark.parametrize(
    "jql", ["summary ~ seeded:backlog", "summary ~ seeded*", "summary ~ seeded backlog"]
)
def test_an_unquoted_phrase_or_reserved_text_answers_200_with_no_issues(
    fake: FakeProvider, jql: str
) -> None:
    assert search(fake, jql) == (200, {"issues": []})


def test_an_unquoted_text_phrase_is_a_400_when_semantic_errors_are_loud(
    fake_factory: Factory,
) -> None:
    provider = fake_factory(Quirks(silent_semantic_jql=False))
    assert search(provider, "summary ~ seeded:backlog")[0] == 400


def test_an_unquoted_single_word_still_matches(fake: FakeProvider) -> None:
    assert keys(search(fake, "summary ~ seeded ORDER BY key")[1]) == ["DEMO-1", "DEMO-2", "DEMO-3"]


@pytest.mark.parametrize(
    "phrase",
    ["seeds backlogs", "Seeded BACKLOG", "seeding backlog", "seed backl", "task backlog seeded"],
)
def test_a_quoted_phrase_matches_near_forms(fake: FakeProvider, phrase: str) -> None:
    assert keys(search(fake, f'summary ~ "{phrase}" AND key = DEMO-1')[1]) == ["DEMO-1"]


@pytest.mark.parametrize("phrase", ["seeded finished task", "bac", "backlogged banana", "xyz"])
def test_a_quoted_phrase_that_does_not_fit_does_not_match(fake: FakeProvider, phrase: str) -> None:
    assert keys(search(fake, f'summary ~ "{phrase}" AND key = DEMO-1')[1]) == []


# --- review round 1: fields, properties, subtasks (items 24 to 26, 29, 30) -----------------------


def test_acceptance_criteria_requires_adf_on_edit_and_create(fake: FakeProvider) -> None:
    assert fake.json("PUT", f"{API}/issue/DEMO-1", {"fields": {F_CRITERIA: "plain"}})[0] == 400
    assert (
        fake.json("PUT", f"{API}/issue/DEMO-1", {"fields": {F_CRITERIA: adf_text("ok")}})[0] == 204
    )
    assert _create(fake, **{F_CRITERIA: "plain"})[0] == 400
    status, doc = _create(fake, **{F_CRITERIA: adf_text("ok")})
    assert status == 201 and fake.tickets[doc["key"]].criteria == adf_text("ok")


def test_the_extra_float_fields_are_listed_and_settable(fake: FakeProvider) -> None:
    _, listing = fake.json("GET", f"{API}/field")
    named = {
        f["id"]: f["name"] for f in listing if f["schema"].get("custom", "").endswith(":float")
    }
    assert named[F_VALUE] != "Story Points" and named[F_EFFORT] != "Story Points"
    assert {F_VALUE, F_EFFORT} <= set(named)
    meta = fake.json("GET", f"{API}/issue/createmeta/DEMO/issuetypes/1")[1]["fields"]
    assert {F_VALUE, F_EFFORT} <= {f["fieldId"] for f in meta}
    assert fake.json("PUT", f"{API}/issue/DEMO-1", {"fields": {F_VALUE: 2.5}})[0] == 204
    assert fake.json("GET", f"{API}/issue/DEMO-1?fields={F_VALUE}")[1]["fields"][F_VALUE] == 2.5
    assert _create(fake, **{F_EFFORT: 4})[0] == 201


def test_a_default_read_has_about_555_keys_and_about_31_kb(fake: FakeProvider) -> None:
    raw = fake.request("GET", f"{API}/issue/DEMO-2")[2]
    fields = json.loads(raw)["fields"]
    assert 550 <= len(fields) <= 560
    assert 25_000 <= len(raw) <= 40_000
    assert len(_filler(fields)) == fake.quirks.all_fields_filler
    assert all(fields[k] is not None for k in _filler(fields))
    small = fake.request("GET", f"{API}/issue/DEMO-2?fields=summary")[2]
    assert len(small) < 2_000


def test_the_filler_count_switch_changes_the_key_count(fake_factory: Factory) -> None:
    provider = fake_factory(Quirks(all_fields_filler=10))
    fields = provider.json("GET", f"{API}/issue/DEMO-2")[1]["fields"]
    assert len(_filler(fields)) == 10
    assert len(fields) < 50


def test_issue_properties_round_trip_with_limits(fake: FakeProvider) -> None:
    path = f"{API}/issue/DEMO-1/properties/demo.meta"
    assert fake.json("GET", path)[0] == 404
    assert fake.json("PUT", path, {"v": 1})[0] == 201
    assert fake.json("GET", path) == (200, {"key": "demo.meta", "value": {"v": 1}})
    assert fake.json("PUT", path, {"v": 2})[0] == 200
    assert fake.json("GET", path)[1]["value"] == {"v": 2}
    assert fake.json("PUT", f"{API}/issue/DEMO-1/properties/{'k' * 256}", 1)[0] == 400
    assert fake.json("PUT", f"{API}/issue/DEMO-1/properties/{'k' * 255}", 1)[0] == 201
    assert fake.json("PUT", f"{API}/issue/DEMO-1/properties/big", "x" * 40000)[0] == 400
    assert fake.request("PUT", f"{API}/issue/DEMO-1/properties/bad", b"{nope")[0] == 400
    assert fake.json("GET", f"{API}/issue/DEMO-999/properties/demo.meta")[0] == 404


def test_comment_properties_round_trip_by_their_own_route(fake: FakeProvider) -> None:
    _, comment = fake.json("POST", f"{API}/issue/DEMO-1/comment", {"body": adf_text("c")})
    base = f"{API}/issue/DEMO-1/comment/{comment['id']}/properties/demo.flag"
    assert fake.json("GET", base)[0] == 404
    assert fake.json("PUT", base, {"on": True})[0] == 201
    assert fake.json("GET", base)[1] == {"key": "demo.flag", "value": {"on": True}}
    _, rich = fake.json("GET", f"{API}/issue/DEMO-1/comment?expand=properties")
    assert rich["comments"][0]["properties"] == [{"key": "demo.flag", "value": {"on": True}}]
    assert fake.json("GET", f"{API}/issue/DEMO-1/comment/99999/properties/x")[0] == 404
    assert (
        fake.json("PUT", f"{API}/issue/DEMO-1/comment/{comment['id']}/properties/{'k' * 256}", 1)[0]
        == 400
    )


def test_properties_of_a_create_body_are_stored_and_read_back(fake: FakeProvider) -> None:
    body = {
        "fields": {"project": {"key": "DEMO"}, "issuetype": {"id": "1"}, "summary": "s"},
        "properties": [{"key": "demo.origin", "value": {"n": 1}}],
    }
    status, doc = fake.json("POST", f"{API}/issue", body)
    assert status == 201
    got = fake.json("GET", f"{API}/issue/{doc['key']}/properties/demo.origin")
    assert got == (200, {"key": "demo.origin", "value": {"n": 1}})
    assert fake.json("POST", f"{API}/issue", {**body, "properties": [{"key": "k" * 256}]})[0] == 400


def test_a_subtask_needs_a_task_or_bug_parent(fake: FakeProvider) -> None:
    epic = fake.add_ticket("DEMO-30", "An epic", type_id="4")
    sub = fake.add_ticket("DEMO-31", "Already a subtask", type_id="3", parent="DEMO-1")
    status, doc = _create(fake, issuetype={"name": "Sub-task"}, parent={"key": epic.key})
    assert status == 400 and "parent" in doc["errors"]
    assert _create(fake, issuetype={"name": "Sub-task"}, parent={"key": sub.key})[0] == 400
    assert (
        _create(fake, issuetype={"name": "Sub-task"}, parent={"key": "DEMO-3"})[0] == 201
    )  # a Bug
    assert (
        _create(fake, issuetype={"name": "Sub-task"}, parent={"key": "DEMO-1"})[0] == 201
    )  # a Task


def test_notify_users_false_is_accepted_when_the_quirk_is_off(fake_factory: Factory) -> None:
    provider = fake_factory(Quirks(notify_users_false_forbidden=False))
    path = f"{API}/issue/DEMO-1?notifyUsers=false"
    assert provider.json("PUT", path, {"fields": {"summary": "x"}})[0] == 204


# --- review round 1: error shapes, paging (items 33, 34) -----------------------------------------


def test_the_messages_shape_carries_the_plain_text_of_a_401_and_a_405(
    fake_factory: Factory,
) -> None:
    provider = fake_factory(Quirks(error_shape="messages"))
    status, doc = provider.json("GET", f"{API}/myself", auth=False)
    assert status == 401 and doc["errorMessages"] == [
        "Client must be authenticated to access this resource."
    ]
    status, doc = provider.json("DELETE", f"{API}/myself")
    assert status == 405 and doc["errorMessages"] == ["Method Not Allowed"]


def test_the_text_shape_joins_field_errors_into_the_text(fake_factory: Factory) -> None:
    provider = fake_factory(Quirks(error_shape="text"))
    out = provider._error_out(ProviderError(400, errors={"a": "first problem", "b": "second one"}))
    assert out.body == "first problem; second one"
    status, headers, body = provider.request("PUT", f"{API}/issue/DEMO-1", {"fields": {"nope": 1}})
    assert status == 400 and headers["content-type"].startswith("text/plain")
    assert "'nope' cannot be set" in body.decode()


def test_the_error_shape_carries_a_field_error_or_the_text(fake_factory: Factory) -> None:
    provider = fake_factory(Quirks(error_shape="error"))
    status, doc = provider.json("PUT", f"{API}/issue/DEMO-1", {"fields": {"nope": 1}})
    assert status == 400 and "nope" in doc["error"]
    assert provider.json("GET", f"{API}/myself", auth=False)[1] == {
        "error": "Client must be authenticated to access this resource."
    }


@pytest.mark.parametrize(
    "path",
    [
        f"{API}/issue/DEMO-1/comment?startAt=-1",
        f"{API}/issue/DEMO-1/comment?maxResults=-5",
        f"{API}/issue/DEMO-1/comment?startAt=x",
    ],
)
def test_negative_or_malformed_comment_paging_is_a_400(fake: FakeProvider, path: str) -> None:
    assert fake.json("GET", path)[0] == 400


def test_negative_search_paging_is_a_400(fake: FakeProvider) -> None:
    assert search(fake, "project = DEMO", maxResults=-1)[0] == 400
    assert search(fake, "project = DEMO", maxResults=0)[0] == 400


# --- review round 1: the server itself (item 32) -------------------------------------------------


def test_closing_a_fake_that_never_started_does_not_raise() -> None:
    FakeProvider().close()
    ScriptedServer(lambda conn: None).close()


def test_the_fake_listens_with_a_deep_backlog() -> None:
    with FakeProvider() as provider:
        assert provider._server.backlog == 128


def test_a_non_numeric_content_length_is_a_400_without_a_thread_traceback(
    fake: FakeProvider, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen: list[object] = []
    monkeypatch.setattr(threading, "excepthook", lambda args: seen.append(args))
    payload = (
        f"POST {API}/issue HTTP/1.1\r\nHost: x\r\nContent-Length: abc\r\n"
        "Connection: close\r\n\r\n{}"
    ).encode()
    with socket.create_connection(("127.0.0.1", fake.port), timeout=5) as sock:
        sock.sendall(payload)
        raw = sock.recv(65536)
    assert raw.startswith(b"HTTP/1.1 400")
    assert fake.request("GET", f"{API}/myself")[0] == 200
    assert seen == []
