"""``search`` through the CLI against the fake provider (issue #17).

Covers the parse check (V-3), paging and the caps (V-2), the cursor, ``--count``, the limit rules,
normalization wiring, hostile provider answers and the leak tests. Everything is synthetic.
"""

from __future__ import annotations

import base64
import json
import re
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from tests.fake_provider import (
    F_ESTIMATE,
    ME_ACCOUNT,
    SITE_EMAIL,
    SITE_TOKEN,
    FakeProvider,
    Fault,
    Quirks,
)
from tests.golden import assert_golden
from ticketctl import sanitize
from ticketctl.cli import main
from ticketctl.gateway import Gateway
from ticketctl.search_cursor import encode, jql_digest

TOKEN_VAR = "TICKETCTL_TEST_TOKEN"
SEARCH = "/rest/api/3/search/jql"
PARSE = "/rest/api/3/jql/parse"
COUNT = "/rest/api/3/search/approximate-count"
# The fake's world has two story-point fields, so the estimate mapping is configured.
LIVE_EXTRA = (
    "queries: {open: 'project = DEMO ORDER BY key ASC'}\n"
    f"    fields: {{estimate: {{id: {F_ESTIMATE}}}}}"
)
ALL_DEMO = ["--jql", "project = DEMO ORDER BY key ASC"]


@pytest.fixture(autouse=True)
def _scrubber_reset() -> Any:
    sanitize.configure_scrubber([])
    yield
    sanitize.configure_scrubber([])


class Env:
    def __init__(self, root: Path, monkeypatch: pytest.MonkeyPatch, capsys: Any) -> None:
        self.root = root
        self.mp = monkeypatch
        self.capsys = capsys
        self.config = root / "config.yaml"
        self.state = root / "state" / "ticketctl"
        monkeypatch.setenv("XDG_STATE_HOME", str(root / "state"))
        monkeypatch.setenv(TOKEN_VAR, SITE_TOKEN)

    def site(self, fake: FakeProvider, name: str = "main", extra: str = "") -> str:
        lines = [
            f"url: {fake.url}",
            f"email: {SITE_EMAIL}",
            f"token_env: {TOKEN_VAR}",
            "allow_http: true",
            *([extra] if extra else []),
        ]
        return f"  {name}:\n" + "".join(f"    {line}\n" for line in lines)

    def use(self, fake: FakeProvider, extra: str = "") -> None:
        self.config.write_text("sites:\n" + self.site(fake, extra=extra), encoding="utf-8")
        self.config.chmod(0o600)

    def run(self, *argv: str) -> tuple[int, str, str]:
        code = main(["--config", str(self.config), *argv])
        cap = self.capsys.readouterr()
        return code, cap.out, cap.err

    def files(self) -> dict[str, str]:
        found: dict[str, str] = {}
        for path in sorted(self.state.rglob("*")) if self.state.exists() else []:
            if path.is_file():
                found[str(path.relative_to(self.state))] = path.read_text(encoding="utf-8")
        return found


@pytest.fixture
def env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: Any) -> Env:
    return Env(tmp_path, monkeypatch, capsys)


@pytest.fixture
def live(env: Env, fake: FakeProvider) -> Env:
    env.use(fake, LIVE_EXTRA)
    return env


def paths(fake: FakeProvider) -> list[str]:
    return [c.path for c in fake.calls]


def searches(fake: FakeProvider) -> list[Any]:
    return [c for c in fake.calls if c.path == SEARCH]


def err_of(out: str) -> dict[str, Any]:
    doc = json.loads(out)
    assert list(doc) == ["error"]
    return doc["error"]


def many(fake: FakeProvider, n: int) -> None:
    """``n`` more tickets, DEMO-4 and up (the seed holds three)."""
    for _ in range(n):
        fake.add_ticket(fake.next_key(), "Bulk ticket")


def keys(doc: dict[str, Any]) -> list[str]:
    return [t["key"] for t in doc["tickets"]]


# --- basics --------------------------------------------------------------------------------


def test_a_raw_query_prints_the_schema_shape(live: Env, fake: FakeProvider) -> None:
    code, out, err = live.run("--json", "search", *ALL_DEMO)
    doc = json.loads(out)
    assert (code, err) == (0, "")
    assert keys(doc) == ["DEMO-1", "DEMO-2", "DEMO-3"]
    assert doc["truncated"] is False and doc["next"] is None and "count" not in doc
    assert_golden("cli", "search", "basic", doc)


def test_default_properties_are_the_search_set(live: Env) -> None:
    doc = json.loads(live.run("--json", "search", *ALL_DEMO)[1])
    first = doc["tickets"][0]
    assert set(first) <= {
        "key",
        "site",
        "type",
        "title",
        "status",
        "assignee",
        "priority",
        "estimate",
        "sprints",
        "parent",
        "labels",
        "updated",
        "incomplete",
    }
    assert first["site"] == "main" and first["title"] == "Seeded backlog task"
    assert "description" not in first and "comments" not in first


def test_fields_narrows_the_request_and_the_output(live: Env, fake: FakeProvider) -> None:
    code, out, _ = live.run("--json", "search", *ALL_DEMO, "--fields", "title,status")
    doc = json.loads(out)
    assert code == 0 and set(doc["tickets"][0]) == {"key", "site", "title", "status"}
    sent = searches(fake)[0].body["fields"]
    assert sorted(sent) == ["status", "summary"]
    assert "*all" not in sent and "*navigable" not in sent


def test_a_named_query_combines_with_structured_flags(live: Env, fake: FakeProvider) -> None:
    code, out, _ = live.run(
        "--json", "search", "--query", "open", "--status", "In Progress", "--order-by", "key:desc"
    )
    doc = json.loads(out)
    assert code == 0 and keys(doc) == ["DEMO-2"]
    sent = [c.body["queries"][0] for c in fake.calls if c.path == PARSE]
    assert sent == ['(project = DEMO) AND (status in ("In Progress")) ORDER BY key DESC']


def test_the_compiled_query_is_what_the_provider_sees(live: Env, fake: FakeProvider) -> None:
    live.run("--json", "search", "--project", "DEMO", "--label", "alpha")
    parsed = [c.body["queries"][0] for c in fake.calls if c.path == PARSE]
    searched = [c.body["jql"] for c in searches(fake)]
    assert len(parsed) == 1 and searched == parsed


def test_the_old_search_endpoint_is_never_used(live: Env, fake: FakeProvider) -> None:
    live.run("--json", "search", *ALL_DEMO)
    assert "/rest/api/3/search" not in paths(fake)


def test_human_output(live: Env) -> None:
    code, out, _ = live.run("search", *ALL_DEMO)
    assert code == 0
    lines = out.splitlines()
    assert lines[0].startswith("DEMO-1 [") and "Seeded backlog task" in lines[0]
    assert len(lines) == 3


def test_human_output_of_an_empty_result(live: Env) -> None:
    code, out, _ = live.run(
        "search", "--jql", 'project = DEMO AND status = "Done" AND key = DEMO-1'
    )
    assert code == 0 and out.strip() == "no tickets"


def test_an_empty_result_is_a_real_zero(live: Env) -> None:
    code, out, _ = live.run("--json", "search", "--jql", "project = DEMO AND key = DEMO-999")
    doc = json.loads(out)
    assert code == 0 and doc == {"tickets": [], "truncated": False, "next": None}
    assert_golden("cli", "search", "empty", doc)


# --- the parse check (V-3) ----------------------------------------------------------------


@pytest.mark.parametrize(
    "jql",
    [
        'project = DEMO AND status = "No such status"',
        "project = ZZNOPE",
        "project = DEMO AND nosuchfield = 1",
        "project = DEMO AND assignee = nosuchfunction()",
    ],
)
def test_a_semantic_error_is_caught_before_any_search(
    live: Env, fake: FakeProvider, jql: str
) -> None:
    # without the parse check the fake answers 200 with no issues (V-3)
    assert fake.quirks.silent_semantic_jql
    code, out, _ = live.run("--json", "search", "--jql", jql)
    body = err_of(out)
    assert (code, body["code"], body["reason"]) == (22, "invalid_request", "invalid_query")
    assert body["details"]["errorMessages"]
    assert SEARCH not in paths(fake) and COUNT not in paths(fake)


def test_the_error_names_the_offending_token(live: Env) -> None:
    code, out, _ = live.run("--json", "search", "--jql", "project = DEMO AND nosuchfield = 1")
    assert code == 22 and "nosuchfield" in err_of(out)["message"]


def test_a_syntax_error_is_invalid_query_too(live: Env, fake: FakeProvider) -> None:
    code, out, _ = live.run("--json", "search", "--jql", "project = = DEMO")
    assert (code, err_of(out)["reason"]) == (22, "invalid_query")
    assert SEARCH not in paths(fake)


def test_a_malformed_order_by_tail_is_judged_by_the_parse_endpoint(
    live: Env, fake: FakeProvider
) -> None:
    code, out, _ = live.run("--json", "search", "--project", "DEMO", "--order-by", "rank ASC OR x")
    assert code == 22 and SEARCH not in paths(fake)


def test_the_parse_check_also_runs_with_a_cursor(live: Env, fake: FakeProvider) -> None:
    many(fake, 100)
    code, out, _ = live.run("--json", "search", *ALL_DEMO, "--limit", "10")
    assert code == 0
    fake.calls.clear()
    code, out, _ = live.run(
        "--json", "search", *ALL_DEMO, "--limit", "10", "--cursor", json.loads(out)["next"]
    )
    assert code == 0 and paths(fake)[0] == PARSE


def test_the_provider_text_is_scrubbed_and_ids_are_masked(
    live: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    real = Gateway.request

    def request(self: Any, method: str, path: str, **kw: Any) -> Any:
        if path == PARSE:
            return type(
                "R",
                (),
                {
                    "status": 200,
                    "data": {
                        "queries": [
                            {"errors": [f"bad customfield_90005 cf[777] {SITE_TOKEN}\x1b[31m"]}
                        ]
                    },
                },
            )()
        return real(self, method, path, **kw)

    monkeypatch.setattr(Gateway, "request", request)
    code, out, err = live.run("--json", "--verbose", "search", *ALL_DEMO)
    assert code == 22
    for text in (out, err):
        assert "customfield_90005" not in text and "cf[777]" not in text
        assert SITE_TOKEN not in text and "\x1b" not in text


@pytest.mark.parametrize("status", [404, 410, 500 + 1, 418])
def test_a_failing_parse_endpoint_is_provider_changed_or_unavailable(
    live: Env, fake: FakeProvider, status: int
) -> None:
    fake.inject(Fault("status", path="jql/parse", status=status, times=None))
    code, out, _ = live.run("--json", "search", *ALL_DEMO)
    assert code == 12 and SEARCH not in paths(fake)
    assert err_of(out)["code"] in {"provider_changed", "unavailable"}


# --- a 400 on the search itself --------------------------------------------------------------


def parse_says_valid(mp: pytest.MonkeyPatch) -> None:
    real = Gateway.request

    def request(self: Any, method: str, path: str, **kw: Any) -> Any:
        if path == PARSE:
            return type("R", (), {"status": 200, "data": {"queries": [{}]}})()
        return real(self, method, path, **kw)

    mp.setattr(Gateway, "request", request)


def test_a_400_on_the_search_is_invalid_query(
    live: Env, fake: FakeProvider, monkeypatch: pytest.MonkeyPatch
) -> None:
    parse_says_valid(monkeypatch)
    code, out, _ = live.run("--json", "search", "--jql", "project = = DEMO")
    body = err_of(out)
    assert (code, body["code"], body["reason"]) == (22, "invalid_request", "invalid_query")
    assert body["details"]["errorMessages"]


# --- paging and caps (V-2) -------------------------------------------------------------------


def test_250_tickets_come_back_with_the_right_next(live: Env, fake: FakeProvider) -> None:
    many(fake, 247)  # 250 in all
    assert fake.quirks.all_fields_page_cap == 100
    code, out, _ = live.run("--json", "search", *ALL_DEMO, "--limit", "250")
    doc = json.loads(out)
    assert code == 0 and len(doc["tickets"]) == 250
    assert doc["truncated"] is False and doc["next"] is None
    sizes = [c.body["maxResults"] for c in searches(fake)]
    assert sizes == [100, 100, 50]


def test_the_default_limit_is_50_and_next_continues(live: Env, fake: FakeProvider) -> None:
    many(fake, 117)  # 120 in all
    code, out, _ = live.run("--json", "search", *ALL_DEMO)
    first = json.loads(out)
    assert code == 0 and len(first["tickets"]) == 50 and first["truncated"] is True
    assert first["next"] and [c.body["maxResults"] for c in searches(fake)] == [50]
    assert_golden("cli", "search", "truncated", first)
    seen = keys(first)
    cursor = first["next"]
    while cursor:
        code, out, _ = live.run("--json", "search", *ALL_DEMO, "--cursor", cursor)
        doc = json.loads(out)
        assert code == 0
        seen += keys(doc)
        cursor = doc["next"]
    assert len(seen) == 120 and len(set(seen)) == 120


def test_the_500_cap_and_5_requests(live: Env, fake: FakeProvider) -> None:
    many(fake, 647)  # 650 in all
    code, out, _ = live.run("--json", "search", *ALL_DEMO, "--limit", "500")
    doc = json.loads(out)
    assert code == 0 and len(doc["tickets"]) == 500
    assert doc["truncated"] is True and doc["next"]
    assert [c.body["maxResults"] for c in searches(fake)] == [100] * 5


def test_exactly_500_available_is_not_truncated(live: Env, fake: FakeProvider) -> None:
    many(fake, 497)
    doc = json.loads(live.run("--json", "search", *ALL_DEMO, "--limit", "500")[1])
    assert len(doc["tickets"]) == 500 and doc["truncated"] is False and doc["next"] is None


def test_limit_501_and_0_are_refused(live: Env, fake: FakeProvider) -> None:
    for limit, word in (("501", "500"), ("0", "--count")):
        code, out, _ = live.run("--json", "search", *ALL_DEMO, "--limit", limit)
        body = err_of(out)
        assert (code, body["reason"]) == (22, "invalid_value") and word in body["message"]
    assert fake.calls == []


@pytest.mark.parametrize("limit", ["-1", "x", "1.5", " 1", "10000000000"])
def test_a_malformed_limit_is_exit_2(live: Env, fake: FakeProvider, limit: str) -> None:
    code, out, _ = live.run("--json", "search", *ALL_DEMO, f"--limit={limit}")
    assert (code, err_of(out)["reason"]) == (2, "invalid_argument") and fake.calls == []


def test_limit_1(live: Env) -> None:
    doc = json.loads(live.run("--json", "search", *ALL_DEMO, "--limit", "1")[1])
    assert keys(doc) == ["DEMO-1"] and doc["truncated"] is True and doc["next"]


def test_a_provider_that_pages_small_stops_at_5_requests(
    live: Env, fake: FakeProvider, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A provider cap below the page size: the request cap ends the loop, truncated, with next."""
    many(fake, 97)
    real = Gateway.request

    def request(self: Any, method: str, path: str, **kw: Any) -> Any:
        if path == SEARCH:
            kw["body"] = {**kw["body"], "maxResults": 10}
        return real(self, method, path, **kw)

    monkeypatch.setattr(Gateway, "request", request)
    doc = json.loads(live.run("--json", "search", *ALL_DEMO, "--limit", "300")[1])
    assert len(doc["tickets"]) == 50 and doc["truncated"] is True and doc["next"]
    assert len(searches(fake)) == 5


# --- hostile provider answers while paging ---------------------------------------------------


def script_search(mp: pytest.MonkeyPatch, answers: list[Any]) -> list[dict[str, Any]]:
    """Answer the search requests in order with these ``data`` documents; returns the bodies."""
    real = Gateway.request
    bodies: list[dict[str, Any]] = []

    def request(self: Any, method: str, path: str, **kw: Any) -> Any:
        if path == SEARCH:
            bodies.append(kw["body"])
            data = answers[min(len(bodies), len(answers)) - 1]
            if isinstance(data, Exception):
                raise data
            return type("R", (), {"status": 200, "data": data, "text": ""})()
        return real(self, method, path, **kw)

    mp.setattr(Gateway, "request", request)
    return bodies


def issue(n: int) -> dict[str, Any]:
    return {"key": f"DEMO-{n}", "fields": {"summary": f"Ticket {n}"}}


def test_a_repeated_page_token_is_provider_changed(
    live: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    bodies = script_search(
        monkeypatch,
        [
            {"issues": [issue(1)], "nextPageToken": "same"},
            {"issues": [issue(2)], "nextPageToken": "same"},
        ],
    )
    code, out, _ = live.run("--json", "search", *ALL_DEMO, "--limit", "300")
    assert (code, err_of(out)["code"]) == (12, "provider_changed")
    assert len(bodies) == 2 and "tickets" not in out


def test_the_token_sent_back_is_the_one_received(
    live: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    bodies = script_search(
        monkeypatch,
        [{"issues": [issue(1)], "nextPageToken": "t-1"}, {"issues": [issue(2)]}],
    )
    code, out, _ = live.run("--json", "search", *ALL_DEMO, "--limit", "300")
    assert code == 0 and keys(json.loads(out)) == ["DEMO-1", "DEMO-2"]
    assert "nextPageToken" not in bodies[0] and bodies[1]["nextPageToken"] == "t-1"


def test_empty_pages_with_tokens_end_at_5_requests(
    live: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    answers = [{"issues": [], "nextPageToken": f"t-{i}"} for i in range(50)]
    bodies = script_search(monkeypatch, answers)
    code, out, _ = live.run("--json", "search", *ALL_DEMO, "--limit", "300")
    doc = json.loads(out)
    assert code == 0 and len(bodies) == 5
    assert doc["tickets"] == [] and doc["truncated"] is True and doc["next"]


@pytest.mark.parametrize("token", ["", "café", "a\nb", "x" * 1025, 5, ["t"], {"a": 1}, "\x00", "١"])
def test_an_unusable_next_token_is_provider_changed(
    live: Env, monkeypatch: pytest.MonkeyPatch, token: Any
) -> None:
    script_search(monkeypatch, [{"issues": [issue(1)], "nextPageToken": token}])
    code, out, _ = live.run("--json", "search", *ALL_DEMO)
    assert (code, err_of(out)["code"]) == (12, "provider_changed")


@pytest.mark.parametrize(
    "data",
    [None, [], "x", {}, {"issues": None}, {"issues": {}}, {"issues": "x"}, {"nextPageToken": "t"}],
)
def test_a_page_without_an_issue_list_is_provider_changed(
    live: Env, monkeypatch: pytest.MonkeyPatch, data: Any
) -> None:
    script_search(monkeypatch, [data])
    code, out, _ = live.run("--json", "search", *ALL_DEMO)
    assert (code, err_of(out)["code"]) == (12, "provider_changed")


def test_a_page_larger_than_asked_is_provider_changed(
    live: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    script_search(monkeypatch, [{"issues": [issue(n) for n in range(1, 6)], "nextPageToken": "t"}])
    code, out, _ = live.run("--json", "search", *ALL_DEMO, "--limit", "3")
    assert (code, err_of(out)["code"]) == (12, "provider_changed")


def test_a_failure_after_the_first_page_prints_no_tickets(
    live: Env, fake: FakeProvider, monkeypatch: pytest.MonkeyPatch
) -> None:
    many(fake, 247)
    real = Gateway.request
    calls: list[str] = []

    def request(self: Any, method: str, path: str, **kw: Any) -> Any:
        if path == SEARCH:
            calls.append(path)
            if len(calls) == 2:
                from ticketctl.gateway import GatewayError

                raise GatewayError("unavailable", "the provider is unavailable", status=503)
        return real(self, method, path, **kw)

    monkeypatch.setattr(Gateway, "request", request)
    code, out, err = live.run("--json", "search", *ALL_DEMO, "--limit", "250")
    body = err_of(out)
    assert (code, body["code"], body["retryable"]) == (12, "unavailable", True)
    assert "DEMO-1" not in out


def test_issues_without_a_usable_key_are_skipped_with_a_warning(
    live: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    script_search(
        monkeypatch, [{"issues": [issue(1), {"fields": {}}, 7, None, {"key": "bad key"}]}]
    )
    code, out, err = live.run("--json", "search", *ALL_DEMO)
    assert code == 0 and keys(json.loads(out)) == ["DEMO-1"]
    assert "4 result(s) had no usable key" in err


def test_a_ticket_without_fields_is_an_incomplete_stub(
    live: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    script_search(monkeypatch, [{"issues": [{"key": "DEMO-9"}]}])
    doc = json.loads(live.run("--json", "search", *ALL_DEMO)[1])
    assert doc["tickets"] == [{"key": "DEMO-9", "site": "main", "incomplete": ["ticket"]}]


def test_a_repeated_issue_is_printed_once(live: Env, monkeypatch: pytest.MonkeyPatch) -> None:
    script_search(
        monkeypatch,
        [{"issues": [issue(1), issue(2)], "nextPageToken": "t"}, {"issues": [issue(2), issue(3)]}],
    )
    doc = json.loads(live.run("--json", "search", *ALL_DEMO, "--limit", "300")[1])
    assert keys(doc) == ["DEMO-1", "DEMO-2", "DEMO-3"]


def test_the_quiet_flag_drops_the_warnings(live: Env, monkeypatch: pytest.MonkeyPatch) -> None:
    script_search(monkeypatch, [{"issues": [issue(1), 7]}])
    code, out, err = live.run("--json", "--quiet", "search", *ALL_DEMO)
    assert code == 0 and err == ""


# --- --count ---------------------------------------------------------------------------------


def test_count_is_printed_next_to_the_tickets(live: Env, fake: FakeProvider) -> None:
    code, out, _ = live.run("--json", "search", *ALL_DEMO, "--count")
    doc = json.loads(out)
    assert code == 0 and doc["count"] == 3 and len(doc["tickets"]) == 3
    assert [p for p in paths(fake) if p in (PARSE, COUNT, SEARCH)] == [PARSE, COUNT, SEARCH]
    assert_golden("cli", "search", "with-count", doc)


def test_limit_0_with_count_makes_no_search_request(live: Env, fake: FakeProvider) -> None:
    code, out, _ = live.run("--json", "search", *ALL_DEMO, "--limit", "0", "--count")
    doc = json.loads(out)
    assert code == 0 and doc == {"tickets": [], "truncated": True, "next": None, "count": 3}
    assert SEARCH not in paths(fake)
    assert_golden("cli", "search", "count-only", doc)


def test_limit_0_with_a_zero_count(live: Env) -> None:
    doc = json.loads(
        live.run(
            "--json",
            "search",
            "--jql",
            "project = DEMO AND key = DEMO-999",
            "--limit",
            "0",
            "--count",
        )[1]
    )
    assert doc == {"tickets": [], "truncated": False, "next": None, "count": 0}


@pytest.mark.parametrize(
    "data", [{}, {"count": -1}, {"count": "3"}, {"count": True}, {"count": 1.5}, [], None]
)
def test_a_bad_count_answer_is_provider_changed(
    live: Env, monkeypatch: pytest.MonkeyPatch, data: Any
) -> None:
    real = Gateway.request

    def request(self: Any, method: str, path: str, **kw: Any) -> Any:
        if path == COUNT:
            return type("R", (), {"status": 200, "data": data, "text": ""})()
        return real(self, method, path, **kw)

    monkeypatch.setattr(Gateway, "request", request)
    code, out, _ = live.run("--json", "search", *ALL_DEMO, "--count")
    assert (code, err_of(out)["code"]) == (12, "provider_changed")


def test_a_400_on_the_count_is_provider_changed(live: Env, fake: FakeProvider) -> None:
    fake.inject(Fault("status", path="approximate-count", status=400, body="no"))
    code, out, _ = live.run("--json", "search", *ALL_DEMO, "--count")
    assert (code, err_of(out)["code"]) == (12, "provider_changed")


# --- the cursor ------------------------------------------------------------------------------


def make_cursor(fake: FakeProvider, live: Env) -> str:
    many(fake, 20)
    out = live.run("--json", "search", *ALL_DEMO, "--limit", "5")[1]
    return str(json.loads(out)["next"])


def test_a_cursor_resumes_where_the_last_page_ended(live: Env, fake: FakeProvider) -> None:
    cursor = make_cursor(fake, live)
    doc = json.loads(live.run("--json", "search", *ALL_DEMO, "--limit", "5", "--cursor", cursor)[1])
    assert keys(doc) == ["DEMO-6", "DEMO-7", "DEMO-8", "DEMO-9", "DEMO-10"]


def test_a_cursor_for_another_query_is_cursor_invalid(live: Env, fake: FakeProvider) -> None:
    cursor = make_cursor(fake, live)
    fake.calls.clear()
    code, out, _ = live.run(
        "--json", "search", "--jql", "project = DEMO ORDER BY key DESC", "--cursor", cursor
    )
    body = err_of(out)
    assert (code, body["reason"]) == (22, "cursor_invalid")
    assert paths(fake) == []  # refused before the parse request and any search


def test_a_cursor_after_the_named_query_changed_is_cursor_invalid(
    live: Env, fake: FakeProvider, env: Env
) -> None:
    env.use(fake, LIVE_EXTRA)
    many(fake, 20)
    cursor = json.loads(env.run("--json", "search", "--query", "open", "--limit", "5")[1])["next"]
    env.use(
        fake, LIVE_EXTRA.replace("project = DEMO ORDER", "project = DEMO AND key > DEMO-0 ORDER")
    )
    code, out, _ = env.run(
        "--json", "search", "--query", "open", "--limit", "5", "--cursor", cursor
    )
    assert (code, err_of(out)["reason"]) == (22, "cursor_invalid")


def test_a_cursor_from_another_site_is_cursor_invalid(env: Env, fake: FakeProvider) -> None:
    env.config.write_text(
        "sites:\n" + env.site(fake, "alpha") + env.site(fake, "beta") + "default_site: alpha\n",
        encoding="utf-8",
    )
    env.config.chmod(0o600)
    many(fake, 20)
    cursor = json.loads(
        env.run("--json", "--site", "alpha", "search", *ALL_DEMO, "--limit", "5")[1]
    )["next"]
    code, out, _ = env.run(
        "--json", "--site", "beta", "search", *ALL_DEMO, "--limit", "5", "--cursor", cursor
    )
    assert (code, err_of(out)["reason"]) == (22, "cursor_invalid")
    code, out, _ = env.run(
        "--json", "--site", "alpha", "search", *ALL_DEMO, "--limit", "5", "--cursor", cursor
    )
    assert code == 0


def b64(raw: str) -> str:
    return base64.urlsafe_b64encode(raw.encode()).decode().rstrip("=")


def canonical(document: dict[str, Any], *, ensure_ascii: bool = True) -> str:
    """Encode a cursor document the way the decoder's canonical re-encode check expects."""
    raw = json.dumps(document, sort_keys=True, separators=(",", ":"), ensure_ascii=ensure_ascii)
    return b64(raw)


# A document that passes every check for the raw query ALL_DEMO on site "main": each hostile entry
# below changes one field, so only the guard under test can reject it.
GOOD_DOC = {
    "v": 1,
    "site": "main",
    "jql_sha256": jql_digest(ALL_DEMO[1]),
    "page_token": "t",
}

HOSTILE = {
    "over 4096 bytes": "A" * 4097,
    "nested 100000 deep": b64("[" * 100_000)[:4096],
    "nested 3000 deep": b64("[" * 3000 + "]" * 3000),
    "not base64": "!!!not-base64!!!",
    "non-ASCII": "café",
    "non-ASCII digits": "١٢٣٤",
    "empty": "",
    "json null": b64("null"),
    "json list": b64("[]"),
    "bytes": b64("\xff\xfe"),
    "non-ASCII token": canonical({**GOOD_DOC, "page_token": "café"}, ensure_ascii=False),
    "wrong version": canonical({**GOOD_DOC, "v": 2}),
    "path injection": canonical({**GOOD_DOC, "site": "../../etc"}),
}


@pytest.mark.parametrize("cursor", HOSTILE.values(), ids=HOSTILE.keys())
def test_hostile_cursors_are_cursor_invalid_and_never_exit_1(
    live: Env, fake: FakeProvider, cursor: str
) -> None:
    code, out, err = live.run("--json", "search", *ALL_DEMO, "--cursor", cursor)
    body = err_of(out)
    assert (code, body["code"], body["reason"]) == (22, "invalid_request", "cursor_invalid")
    assert paths(fake) == []
    assert cursor[:30] not in body["message"] or len(cursor) < 8
    assert "Traceback" not in err


def test_a_forged_cursor_with_a_valid_shape_is_only_sent_back_as_a_token(
    live: Env, fake: FakeProvider
) -> None:
    jql = "project = DEMO ORDER BY key ASC"
    forged = encode("main", jql_digest(jql), '"}, "jql": "project = X')
    code, out, _ = live.run("--json", "search", "--jql", jql, "--cursor", forged)
    body = err_of(out)
    assert (code, body["reason"]) == (22, "cursor_expired")
    sent = searches(fake)[0].body
    assert sent["jql"] == jql and sent["nextPageToken"] == '"}, "jql": "project = X'


def test_a_token_the_provider_rejects_is_cursor_expired(live: Env, fake: FakeProvider) -> None:
    jql = "project = DEMO ORDER BY key ASC"
    cursor = encode("main", jql_digest(jql), "no-such-token")
    code, out, _ = live.run("--json", "search", "--jql", jql, "--cursor", cursor)
    body = err_of(out)
    assert (code, body["code"], body["reason"]) == (22, "invalid_request", "cursor_expired")
    assert "restart without --cursor" in body["message"]


def test_a_400_on_a_later_page_is_not_cursor_expired(
    live: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    real = Gateway.request
    n = 0

    def request(self: Any, method: str, path: str, **kw: Any) -> Any:
        nonlocal n
        if path == SEARCH:
            n += 1
            if n == 1:
                return type(
                    "R",
                    (),
                    {
                        "status": 200,
                        "text": "",
                        "data": {"issues": [issue(1)], "nextPageToken": "t"},
                    },
                )()
            return type("R", (), {"status": 400, "text": "boom", "data": None})()
        return real(self, method, path, **kw)

    monkeypatch.setattr(Gateway, "request", request)
    code, out, _ = live.run("--json", "search", *ALL_DEMO, "--limit", "300")
    assert (code, err_of(out)["reason"]) == (22, "invalid_query")


def test_the_incremental_refresh_combination(live: Env, fake: FakeProvider) -> None:
    """Named query, --updated-since, updated:asc and --cursor paging; the digest stays stable."""
    many(fake, 12)
    base = [
        "--json", "search", "--query", "open", "--updated-since", "2026-01-01",
        "--order-by", "updated:asc", "--limit", "5",
    ]  # fmt: skip
    first = json.loads(live.run(*base)[1])
    assert len(first["tickets"]) == 5 and first["next"]
    second = json.loads(live.run(*base, "--cursor", first["next"])[1])
    assert len(second["tickets"]) == 5 and not set(keys(first)) & set(keys(second))
    # a different window is a different query, so the old cursor is refused
    other = [*base[:-2], "--updated-since", "2026-01-02", "--limit", "5"]
    out = live.run(*other, "--cursor", first["next"])[1]
    assert err_of(out)["reason"] == "cursor_invalid"


# --- flags, combinations and inputs ---------------------------------------------------------


@pytest.mark.parametrize(
    "extra",
    [
        ["--project", "DEMO"],
        ["--status", "Done"],
        ["--text", "x"],
        ["--order-by", "key"],
        ["--query", "open"],
        ["--updated-since", "1d"],
        ["--label", "a"],
    ],
)
def test_a_raw_query_stands_alone(live: Env, fake: FakeProvider, extra: list[str]) -> None:
    for form in (["--jql", "project = DEMO"],):
        code, out, _ = live.run("--json", "search", *form, *extra)
        assert (code, err_of(out)["reason"]) == (2, "invalid_combination")
    assert fake.calls == []


def test_a_raw_query_file_stands_alone_too(live: Env, fake: FakeProvider, tmp_path: Path) -> None:
    path = tmp_path / "q.jql"
    path.write_text("project = DEMO\n", encoding="utf-8")
    code, out, _ = live.run("--json", "search", "--jql-file", str(path), "--project", "DEMO")
    assert (code, err_of(out)["reason"]) == (2, "invalid_combination") and fake.calls == []
    code, out, _ = live.run("--json", "search", "--jql-file", str(path))
    assert code == 0 and len(keys(json.loads(out))) == 3


def test_exactly_one_raw_form(live: Env, fake: FakeProvider, tmp_path: Path) -> None:
    path = tmp_path / "q.jql"
    path.write_text("project = DEMO", encoding="utf-8")
    code, out, _ = live.run("--json", "search", "--jql", "project = DEMO", "--jql-file", str(path))
    assert (code, err_of(out)["reason"]) == (2, "invalid_combination")
    code, out, _ = live.run("--json", "search", "--text", "a", "--text-file", str(path))
    assert (code, err_of(out)["reason"]) == (2, "invalid_combination")
    assert fake.calls == []


def test_nothing_to_select_is_missing_option(live: Env, fake: FakeProvider) -> None:
    for argv in ([], ["--limit", "5"], ["--order-by", "key"], ["--count"]):
        code, out, _ = live.run("--json", "search", *argv)
        assert (code, err_of(out)["reason"]) == (2, "missing_option")
    assert fake.calls == []


def test_an_unknown_named_query_is_invalid_value(live: Env, fake: FakeProvider) -> None:
    code, out, _ = live.run("--json", "search", "--query", "nope")
    body = err_of(out)
    assert (code, body["reason"]) == (22, "invalid_value") and "nope" in body["message"]
    assert "main" in body["message"] and fake.calls == []


def test_text_file_strips_exactly_one_trailing_newline(
    live: Env, fake: FakeProvider, tmp_path: Path
) -> None:
    path = tmp_path / "t.txt"
    path.write_text("Seeded finished bug\n", encoding="utf-8")
    code, out, _ = live.run("--json", "search", "--text-file", str(path))
    assert code == 0 and keys(json.loads(out)) == ["DEMO-3"]
    sent = searches(fake)[0].body["jql"]
    assert sent == r'(text ~ "\"Seeded finished bug\"")'
    path.write_text("Seeded finished bug\n\n", encoding="utf-8")
    code, out, _ = live.run("--json", "search", "--text-file", str(path))
    assert (code, err_of(out)["reason"]) == (22, "invalid_value")
    path.write_text("Seeded\nfinished\n", encoding="utf-8")
    code, out, _ = live.run("--json", "search", "--text-file", str(path))
    assert (code, err_of(out)["reason"]) == (22, "invalid_value")


def test_a_missing_jql_file_is_invalid_file(live: Env, tmp_path: Path) -> None:
    code, out, _ = live.run("--json", "search", "--jql-file", str(tmp_path / "none"))
    assert (code, err_of(out)["reason"]) == (22, "invalid_file")


def test_stdin_jql(live: Env, monkeypatch: pytest.MonkeyPatch) -> None:
    import io

    class Stdin(io.TextIOWrapper):
        def isatty(self) -> bool:
            return False

    monkeypatch.setattr("sys.stdin", Stdin(io.BytesIO(b"project = DEMO ORDER BY key ASC\n")))
    code, out, _ = live.run("--json", "search", "--jql-file", "-")
    assert code == 0 and len(keys(json.loads(out))) == 3


def test_a_wrong_fields_name_is_unknown_field_before_any_request(
    live: Env, fake: FakeProvider
) -> None:
    code, out, _ = live.run("--json", "search", *ALL_DEMO, "--fields", "nope")
    assert (code, err_of(out)["reason"]) == (22, "unknown_field") and fake.calls == []


def test_the_last_since_flag_wins(live: Env, fake: FakeProvider) -> None:
    live.run(
        "--json", "search", "--project", "DEMO", "--updated-since", "1d", "--updated-since", "2d"
    )
    parsed = [c.body["queries"][0] for c in fake.calls if c.path == PARSE][0]
    assert "-2d" in parsed and "-1d" not in parsed


def test_an_rfc3339_timestamp_loads_the_account_zone_once(live: Env, fake: FakeProvider) -> None:
    code, _, _ = live.run(
        "--json", "search", "--project", "DEMO", "--updated-since", "2026-03-01T15:00:00Z"
    )
    assert code == 0 and paths(fake).count("/rest/api/3/myself") == 1
    parsed = [c.body["queries"][0] for c in fake.calls if c.path == PARSE][0]
    assert re.search(r'updated >= "2026-03-01 09:00"', parsed)
    live.run("--json", "search", "--project", "DEMO", "--updated-since", "2026-03-01T15:00:00Z")
    assert paths(fake).count("/rest/api/3/myself") == 1  # cached


def test_a_duration_or_date_needs_no_account_request(live: Env, fake: FakeProvider) -> None:
    for value in ("5d", "2026-03-01"):
        assert live.run("--json", "search", "--project", "DEMO", "--updated-since", value)[0] == 0
    assert "/rest/api/3/myself" not in paths(fake)


def test_order_by_maps_a_logical_name_to_its_custom_clause(live: Env, fake: FakeProvider) -> None:
    live.run("--json", "search", "--project", "DEMO", "--order-by", "estimate:desc")
    parsed = [c.body["queries"][0] for c in fake.calls if c.path == PARSE]
    assert parsed and parsed[0].endswith("ORDER BY cf[" + F_ESTIMATE.split("_")[1] + "] DESC")


def test_an_unmapped_logical_order_by_is_unknown_field(live: Env, fake: FakeProvider) -> None:
    code, out, _ = live.run(
        "--json", "search", "--project", "DEMO", "--order-by", "acceptance_criteria"
    )
    assert (code, err_of(out)["reason"]) == (22, "unknown_field") and PARSE not in paths(fake)


def test_the_project_flag_selects_the_site(env: Env, fake: FakeProvider) -> None:
    env.config.write_text(
        "sites:\n"
        + env.site(fake, "alpha", "projects: [DEMO]")
        + env.site(fake, "beta", "projects: [FAKE]"),
        encoding="utf-8",
    )
    env.config.chmod(0o600)
    code, out, _ = env.run("--json", "search", "--project", "DEMO", "--label", "alpha")
    assert code == 0 and json.loads(out)["tickets"][0]["site"] == "alpha"
    code, out, _ = env.run("--json", "search", "--project", "DEMO", "--project", "FAKE")
    assert (code, err_of(out)["reason"]) == (22, "mixed_sites")


def test_priority_is_normalized_with_a_rank(live: Env, fake: FakeProvider) -> None:
    doc = json.loads(live.run("--json", "search", *ALL_DEMO, "--fields", "priority")[1])
    assert doc["tickets"][0]["priority"] == {"name": "Medium", "rank": 2}
    assert "/rest/api/3/priority" in paths(fake)


def test_no_priority_request_without_the_property(live: Env, fake: FakeProvider) -> None:
    live.run("--json", "search", *ALL_DEMO, "--fields", "title")
    assert "/rest/api/3/priority" not in paths(fake)


def test_provider_field_ids_never_reach_the_output(live: Env) -> None:
    out = live.run("--json", "search", *ALL_DEMO, "--fields", "estimate,sprints,fields")[1]
    assert not re.search(r"customfield_[0-9]+", out)


def test_a_warning_from_normalization_goes_to_stderr(env: Env, fake: FakeProvider) -> None:
    env.use(fake, "fields: {estimate: {name: 'No such field'}}")
    code, out, err = env.run("--json", "search", *ALL_DEMO, "--fields", "estimate")
    assert code == 0 and "estimate" in err
    assert "incomplete" in json.loads(out)["tickets"][0]


# --- failures --------------------------------------------------------------------------------


def test_200_tickets_are_not_cut_at_the_all_fields_page_cap(live: Env, fake: FakeProvider) -> None:
    many(fake, 197)
    fake.quirks.all_fields_page_cap = 100
    doc = json.loads(live.run("--json", "search", *ALL_DEMO, "--limit", "200")[1])
    assert len(doc["tickets"]) == 200


def test_the_deadline_is_honored(live: Env, fake: FakeProvider) -> None:
    fake.quirks.response_delay_s = 2.0
    code, out, _ = live.run("--json", "--timeout", "1s", "search", *ALL_DEMO)
    assert code == 12 and err_of(out)["code"] == "timeout"


@pytest.mark.parametrize("status,code_name", [(401, "bad_credentials"), (403, "forbidden")])
def test_auth_failures_keep_their_codes(
    live: Env, fake: FakeProvider, status: int, code_name: str
) -> None:
    fake.inject(Fault("status", path="search/jql", status=status, times=None))
    fake.inject(Fault("status", path="jql/parse", status=status, times=None))
    code, out, _ = live.run("--json", "search", *ALL_DEMO)
    assert (code, err_of(out)["code"]) == (12, code_name)


def test_a_410_on_the_search_is_provider_changed(live: Env, fake: FakeProvider) -> None:
    fake.inject(Fault("status", path="search/jql", status=410, times=None))
    code, out, _ = live.run("--json", "search", *ALL_DEMO)
    assert (code, err_of(out)["code"]) == (12, "provider_changed")


def test_the_quirk_switch_for_semantic_errors_does_not_matter(
    fake_factory: Callable[[Quirks | None], FakeProvider], env: Env
) -> None:
    fake = fake_factory(Quirks(silent_semantic_jql=False))
    env.use(fake)
    code, out, _ = env.run("--json", "search", "--jql", "project = ZZNOPE")
    assert (code, err_of(out)["reason"]) == (22, "invalid_query")


# --- leaks -----------------------------------------------------------------------------------


@pytest.mark.parametrize("debug", [False, True])
@pytest.mark.parametrize("flags", [[], ["--verbose"], ["--json", "--verbose"], ["--json"]])
@pytest.mark.parametrize(
    "argv",
    [
        ALL_DEMO,
        [*ALL_DEMO, "--count"],
        [*ALL_DEMO, "--limit", "1"],
        ["--jql", "project = ZZNOPE"],
    ],
    ids=lambda a: " ".join(a),
)
def test_the_token_never_appears_in_any_channel(
    live: Env, fake: FakeProvider, argv: list[str], flags: list[str], debug: bool
) -> None:
    if debug:
        live.mp.setenv("TICKETCTL_DEBUG", "1")
    code, out, err = live.run(*flags, "search", *argv)
    assert code in (0, 22)
    forbidden = [SITE_TOKEN, fake.basic_header().split(" ", 1)[1]]
    for text in (out, err, *live.files().values()):
        for secret in forbidden:
            assert secret not in text
    if "--verbose" in flags:
        assert "POST /rest/api/3/jql/parse" in err and "Authorization" not in err


def test_the_token_is_scrubbed_from_a_provider_error_that_echoes_it(
    live: Env, fake: FakeProvider
) -> None:
    fake.inject(
        Fault("status", path="search/jql", status=400, body=f"bad {SITE_TOKEN}", times=None)
    )
    code, out, err = live.run("--json", "--verbose", "search", *ALL_DEMO)
    assert code == 22
    for text in (out, err, *live.files().values()):
        assert SITE_TOKEN not in text


def test_the_scrub_covers_a_traceback_in_search(live: Env, monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(*a: Any, **k: Any) -> None:
        raise RuntimeError(f"leaked {SITE_TOKEN}")

    monkeypatch.setattr("ticketctl.searchcmd.collect", boom)
    live.mp.setenv("TICKETCTL_DEBUG", "1")
    code, out, err = live.run("--json", "search", *ALL_DEMO)
    assert code == 1 and "Traceback" in err
    assert SITE_TOKEN not in out + err


def test_the_cursor_never_holds_a_credential(live: Env, fake: FakeProvider) -> None:
    many(fake, 10)
    cursor = json.loads(live.run("--json", "search", *ALL_DEMO, "--limit", "2")[1])["next"]
    raw = base64.urlsafe_b64decode(cursor + "=" * (-len(cursor) % 4)).decode()
    assert SITE_TOKEN not in raw and SITE_EMAIL not in raw and ME_ACCOUNT not in raw


# --- review round 1 ----------------------------------------------------------------------------


def test_a_garbage_since_value_is_refused_before_any_request(live: Env, fake: FakeProvider) -> None:
    code, out, _ = live.run("--json", "search", "--project", "DEMO", "--updated-since", "garbage")
    assert (code, err_of(out)["reason"]) == (22, "invalid_value")
    assert fake.calls == []


@pytest.mark.parametrize(
    ("argv", "reason"),
    [
        (["--limit", "501"], "invalid_value"),
        (["--limit", "0"], "invalid_value"),
        (["--fields", "bogus"], "unknown_field"),
    ],
)
def test_limit_and_fields_are_judged_before_missing_option(
    live: Env, fake: FakeProvider, argv: list[str], reason: str
) -> None:
    code, out, _ = live.run("--json", "search", *argv)
    assert (code, err_of(out)["reason"]) == (22, reason)
    assert fake.calls == []


def test_the_provider_text_of_a_search_400_is_masked_everywhere(
    live: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    parse_says_valid(monkeypatch)
    real = Gateway.request
    text = f"bad customfield_90005 cf[90005] {SITE_TOKEN}"

    def request(self: Any, method: str, path: str, **kw: Any) -> Any:
        if path == SEARCH:
            data = {"errorMessages": [text]}
            return type("R", (), {"status": 400, "text": text, "data": data})()
        return real(self, method, path, **kw)

    monkeypatch.setattr(Gateway, "request", request)
    code, out, err = live.run("--json", "--verbose", "search", *ALL_DEMO)
    body = err_of(out)
    assert (code, body["reason"]) == (22, "invalid_query")
    shown = [body["message"], *body["details"]["errorMessages"], out, err]
    for item in shown:
        assert "customfield_90005" not in item and "cf[90005]" not in item
        assert SITE_TOKEN not in item
    assert body["details"]["errorMessages"]

    # the same with a plain-text body and no errorMessages list
    def plain(self: Any, method: str, path: str, **kw: Any) -> Any:
        if path == SEARCH:
            return type("R", (), {"status": 400, "text": text, "data": None})()
        return real(self, method, path, **kw)

    monkeypatch.setattr(Gateway, "request", plain)
    code, out, err = live.run("--json", "--verbose", "search", *ALL_DEMO)
    body = err_of(out)
    assert code == 22 and body["details"]["errorMessages"]
    for item in (out, err):
        assert "customfield_90005" not in item and "cf[90005]" not in item
