"""The HTTP status mapping and the retry rules through the commands (issue #35).

tests/test_gateway.py proves the transport rules against a scripted server; these tests prove that
every command maps what the gateway raises, and what the endpoint kind decides (object path or
collection, read or write), to the exit code and code of docs/interface.md, "HTTP status mapping".
Faults are the fake provider's (``Fault``); the retry sleeps are recorded, never taken.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from tests.fake_provider import OTHER_ACCOUNT, SITE_EMAIL, SITE_TOKEN, FakeProvider, Fault, Out
from ticketctl import runtime
from ticketctl.cli import main

ENV_VAR = "TICKETCTL_TEST_RELIABILITY_ENV"
OTHER = OTHER_ACCOUNT


class Cli:
    def __init__(self, root: Path, fake: FakeProvider, capsys: Any) -> None:
        self.fake = fake
        self.capsys = capsys
        self.root = root
        self.sleeps: list[float] = []
        self.config = root / "config.yaml"
        self.config.write_text(
            f"sites:\n  main:\n    url: {fake.url}\n    email: {SITE_EMAIL}\n"
            f"    token_env: {ENV_VAR}\n    allow_http: true\n    projects: [DEMO]\n",
            encoding="utf-8",
        )
        self.config.chmod(0o600)

    def run(self, *argv: str) -> tuple[int, str, str]:
        code = main(["--config", str(self.config), *argv])
        cap = self.capsys.readouterr()
        assert SITE_TOKEN not in cap.out + cap.err
        return code, cap.out, cap.err

    def error(self, *argv: str) -> tuple[int, dict[str, Any]]:
        code, out, _ = self.run("--json", *argv)
        doc = json.loads(out)
        return code, doc["error"] if code != 0 else doc

    def apply(self, *argv: str) -> tuple[int, dict[str, Any]]:
        """Plan with --dry-run (no fault can match: the faults are armed after), then apply."""
        code, plan = self.error("--dry-run", *argv)
        assert code == 0, plan
        return self.error("--yes", "--expect-plan", plan["plan_sha256"], *argv)

    def writes(self) -> list[Any]:
        return [c for c in self.fake.calls if c.method != "GET" and "/search" not in c.path]

    def audit(self) -> list[dict[str, Any]]:
        path = self.root / "state" / "ticketctl" / "audit.jsonl"
        if not path.exists():
            return []
        return [json.loads(line) for line in path.read_text().splitlines()]


@pytest.fixture
def cli(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: Any, fake: FakeProvider) -> Cli:
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    monkeypatch.setenv(ENV_VAR, SITE_TOKEN)
    out = Cli(tmp_path, fake, capsys)
    real = runtime.Gateway

    def gateway(*args: Any, **kw: Any) -> Any:
        return real(*args, sleep=out.sleeps.append, rng=lambda: 1.0, **kw)

    monkeypatch.setattr(runtime, "Gateway", gateway)
    return out


def fail(fake: FakeProvider, method: str, path: str, status: int, **kw: Any) -> Fault:
    return fake.inject(Fault("status", method=method, path=path, times=None, status=status, **kw))


# --- reads: every row of the table, per endpoint kind -------------------------------------------

OBJECT_READS = [
    pytest.param(("get", "DEMO-1"), r"/issue/DEMO-1$", id="get"),
    pytest.param(("comments", "DEMO-1"), r"/issue/DEMO-1/comment$", id="comments"),
    pytest.param(("transitions", "DEMO-1"), r"/issue/DEMO-1/transitions$", id="transitions"),
]
COLLECTION_READS = [
    pytest.param(("whoami",), r"/myself$", id="myself"),
    pytest.param(("fields", "list"), r"/rest/api/3/field$", id="field"),
    pytest.param(("sprints", "list"), r"/rest/agile/1.0/board$", id="board-listing"),
]

# status -> (exit, code, retryable) of a read
READ_ROWS = {
    401: (12, "bad_credentials", False),
    403: (12, "forbidden", False),
    429: (12, "rate_limited", True),
    500: (12, "unavailable", True),
    502: (12, "unavailable", True),
    503: (12, "unavailable", True),
    504: (12, "unavailable", True),
}
OTHER_4XX = (400, 402, 405, 409, 410, 413, 415, 418, 422)


@pytest.mark.parametrize("status", sorted(READ_ROWS))
@pytest.mark.parametrize("argv, path", [*OBJECT_READS, *COLLECTION_READS])
def test_read_status_rows_are_the_same_on_every_endpoint(
    cli: Cli, argv: tuple[str, ...], path: str, status: int
) -> None:
    fail(cli.fake, "GET", path, status)
    code, err = cli.error(*argv)
    assert (code, err["code"], err["retryable"]) == READ_ROWS[status]
    assert err["outcome_unknown"] is False


@pytest.mark.parametrize("status", OTHER_4XX)
@pytest.mark.parametrize("argv, path", [*OBJECT_READS, *COLLECTION_READS])
def test_any_other_4xx_on_a_read_is_provider_changed(
    cli: Cli, argv: tuple[str, ...], path: str, status: int
) -> None:
    fail(cli.fake, "GET", path, status)
    code, err = cli.error(*argv)
    assert (code, err["code"], err["retryable"]) == (12, "provider_changed", False)


@pytest.mark.parametrize("argv, path", OBJECT_READS)
def test_404_on_an_object_path_is_not_found(cli: Cli, argv: tuple[str, ...], path: str) -> None:
    fail(cli.fake, "GET", path, 404)
    code, err = cli.error(*argv)
    assert (code, err["code"]) == (10, "not_found")
    assert "missing or you have no permission" in err["message"]


@pytest.mark.parametrize("argv, path", COLLECTION_READS)
def test_404_on_a_collection_endpoint_is_provider_changed(
    cli: Cli, argv: tuple[str, ...], path: str
) -> None:
    fail(cli.fake, "GET", path, 404)
    code, err = cli.error(*argv)
    assert (code, err["code"]) == (12, "provider_changed")


def test_search_rows(cli: Cli) -> None:
    query = ("search", "--jql", "project = DEMO ORDER BY key ASC")
    for status, expected in (
        (404, (12, "provider_changed")),
        (410, (12, "provider_changed")),
        (401, (12, "bad_credentials")),
        (403, (12, "forbidden")),
        (400, (22, "invalid_request")),
        (500, (12, "unavailable")),
        (429, (12, "rate_limited")),
        (409, (12, "provider_changed")),
    ):
        cli.fake.faults.clear()
        fail(cli.fake, "POST", r"/search/jql$", status)
        code, err = cli.error(*query)
        assert (code, err["code"]) == expected, status
        if status == 400:
            assert err["reason"] == "invalid_query"


@pytest.mark.parametrize("path", [r"/jql/parse$", r"/approximate-count$"])
def test_404_and_410_on_the_other_search_collections_are_provider_changed(
    cli: Cli, path: str
) -> None:
    for status in (404, 410):
        cli.fake.faults.clear()
        fail(cli.fake, "POST", path, status)
        code, err = cli.error("search", "--count", "--jql", "project = DEMO ORDER BY key ASC")
        assert (code, err["code"]) == (12, "provider_changed"), (path, status)


def test_400_on_a_board_without_sprints_is_invalid_value_with_the_board_option(cli: Cli) -> None:
    fail(cli.fake, "GET", r"/board/\d+/sprint$", 400)
    code, err = cli.error("sprints", "list")
    assert (code, err["code"], err["reason"], (err["details"] or {}).get("option")) == (
        22,
        "invalid_request",
        "invalid_value",
        "board",
    )


def test_404_on_a_named_board_is_not_found_with_the_board_option(cli: Cli) -> None:
    fail(cli.fake, "GET", r"/board/\d+/sprint$", 404)
    code, err = cli.error("sprints", "list")
    assert (code, err["code"], (err["details"] or {}).get("option")) == (10, "not_found", "board")


# --- a missing, malformed and hidden key are one answer -----------------------------------------

ERROR_BODIES = {
    "error-messages": '{"errorMessages":["Issue does not exist"],"errors":{}}',
    "errors-map": '{"errorMessages":[],"errors":{"key":"bad key"}}',
    "single-error": '{"error":"hidden"}',
    "plain-text": "Not Found",
    "empty": "",
}


@pytest.mark.parametrize("argv, path", OBJECT_READS)
def test_the_404_answer_does_not_depend_on_the_error_body_shape(
    cli: Cli, argv: tuple[str, ...], path: str
) -> None:
    seen = set()
    for shape, body in ERROR_BODIES.items():
        cli.fake.faults.clear()
        fail(cli.fake, "GET", path, 404, body=body)
        code, err = cli.error(*argv)
        assert (code, err["code"]) == (10, "not_found"), shape
        seen.add((err["message"], err["reason"], json.dumps(err["details"])))
        for word in ("gone", "hidden", "deleted", "permission denied"):
            assert word not in err["message"].lower()
    assert len(seen) == 1


@pytest.mark.parametrize("argv", [("get",), ("comments",), ("transitions",)])
def test_a_missing_key_and_a_provider_hidden_key_answer_alike(
    cli: Cli, argv: tuple[str, ...]
) -> None:
    code_missing, missing = cli.error(*argv, "DEMO-404")  # the fake has no such ticket
    fail(cli.fake, "GET", r"/issue/DEMO-2", 404, body=ERROR_BODIES["error-messages"])
    code_hidden, hidden = cli.error(*argv, "DEMO-2")
    assert code_missing == code_hidden == 10
    assert missing["code"] == hidden["code"] == "not_found"
    assert missing["message"] == hidden["message"]


@pytest.mark.parametrize("key", ["DEMO-0x", "DEMO--1", "../DEMO-1", "DEMO-1/comment", ""])
@pytest.mark.parametrize("command", ["get", "comments", "transitions"])
def test_a_malformed_key_is_refused_before_any_request(cli: Cli, command: str, key: str) -> None:
    code, err = cli.error(command, key)
    assert code in (2, 22)
    assert cli.fake.calls == []


# --- writes: every command, every row ------------------------------------------------------------

WRITES = [
    pytest.param(
        ("comment", "DEMO-2", "--body", "hello", "--dedupe-key", "k1"),
        "POST",
        r"/issue/DEMO-2/comment$",
        id="comment",
    ),
    pytest.param(
        ("transition", "DEMO-2", "--to", "Done", "--resolution", "Done"),
        "POST",
        r"/issue/DEMO-2/transitions$",
        id="transition",
    ),
    pytest.param(
        ("update", "DEMO-2", "--assign", OTHER), "PUT", r"/issue/DEMO-2/assignee$", id="update"
    ),
    pytest.param(
        ("sprint", "add", "DEMO-1", "--sprint", "2"),
        "POST",
        r"/sprint/2/issue$",
        id="sprint-add",
    ),
]


def sent(cli: Cli, method: str, path: str) -> int:
    import re

    return len([c for c in cli.fake.calls if c.method == method and re.search(path, c.path)])


@pytest.mark.parametrize("status", [400, 409, 413, 415, 422, 418])
@pytest.mark.parametrize("argv, method, path", WRITES)
def test_a_4xx_on_a_write_is_rejected_after_one_request(
    cli: Cli, argv: tuple[str, ...], method: str, path: str, status: int
) -> None:
    code, plan = cli.error("--dry-run", *argv)
    assert code == 0, plan
    fail(cli.fake, method, path, status, body="nope")
    code, err = cli.error("--yes", "--expect-plan", plan["plan_sha256"], *argv)
    assert (code, err["code"], err["retryable"], err["outcome_unknown"]) == (
        12,
        "rejected",
        False,
        False,
    )
    assert sent(cli, method, path) == 1
    assert cli.sleeps == []


@pytest.mark.parametrize("status", [401, 403])
@pytest.mark.parametrize("argv, method, path", WRITES)
def test_401_and_403_on_a_write_are_final_with_nothing_unknown(
    cli: Cli, argv: tuple[str, ...], method: str, path: str, status: int
) -> None:
    code, plan = cli.error("--dry-run", *argv)
    fail(cli.fake, method, path, status)
    code, err = cli.error("--yes", "--expect-plan", plan["plan_sha256"], *argv)
    assert (code, err["code"]) == (12, {401: "bad_credentials", 403: "forbidden"}[status])
    assert err["outcome_unknown"] is False
    assert sent(cli, method, path) == 1


@pytest.mark.parametrize("argv, method, path", WRITES)
def test_404_on_a_write_to_an_object_path_is_not_found(
    cli: Cli, argv: tuple[str, ...], method: str, path: str
) -> None:
    code, plan = cli.error("--dry-run", *argv)
    fail(cli.fake, method, path, 404)
    code, err = cli.error("--yes", "--expect-plan", plan["plan_sha256"], *argv)
    assert (code, err["code"], err["outcome_unknown"]) == (10, "not_found", False)


@pytest.mark.parametrize("status", [500, 502, 503, 504, 507])
@pytest.mark.parametrize("argv, method, path", WRITES)
def test_a_5xx_on_a_write_is_outcome_unknown_and_never_resent(
    cli: Cli, argv: tuple[str, ...], method: str, path: str, status: int
) -> None:
    code, plan = cli.error("--dry-run", *argv)
    fail(cli.fake, method, path, status)
    code, err = cli.error("--yes", "--expect-plan", plan["plan_sha256"], *argv)
    assert (code, err["code"], err["outcome_unknown"]) == (12, "outcome_unknown", True)
    assert sent(cli, method, path) == 1
    assert cli.sleeps == []
    records = cli.audit()
    assert [r["result"] for r in records][-1] == "unknown"
    assert err["details"]["attempt_id"] == records[-1]["attempt_id"]


@pytest.mark.parametrize("argv, method, path", WRITES)
def test_429_on_a_write_retries_twice_then_is_rate_limited_and_not_unknown(
    cli: Cli, argv: tuple[str, ...], method: str, path: str
) -> None:
    code, plan = cli.error("--dry-run", *argv)
    fail(cli.fake, method, path, 429)
    code, err = cli.error("--yes", "--expect-plan", plan["plan_sha256"], *argv)
    assert (code, err["code"], err["retryable"], err["outcome_unknown"]) == (
        12,
        "rate_limited",
        True,
        False,
    )
    assert sent(cli, method, path) == 3
    assert cli.sleeps == [0.5, 1.0]  # the jitter is pinned to its maximum


@pytest.mark.parametrize("argv, method, path", WRITES)
def test_a_retry_after_beyond_the_cap_ends_a_write_at_once_with_the_value(
    cli: Cli, argv: tuple[str, ...], method: str, path: str
) -> None:
    cli.fake.quirks.retry_after_format = "seconds"
    code, plan = cli.error("--dry-run", *argv)
    fail(cli.fake, method, path, 429, retry_after=100000)
    code, err = cli.error("--yes", "--expect-plan", plan["plan_sha256"], *argv)
    assert (code, err["code"], err["retry_after_s"]) == (12, "rate_limited", 100000)
    assert sent(cli, method, path) == 1
    assert cli.sleeps == []


@pytest.mark.parametrize("argv, method, path", WRITES)
def test_a_retry_after_within_the_cap_replaces_the_backoff_and_the_write_then_lands(
    cli: Cli, argv: tuple[str, ...], method: str, path: str
) -> None:
    cli.fake.quirks.retry_after_format = "seconds"
    code, plan = cli.error("--dry-run", *argv)
    cli.fake.inject(Fault("status", method=method, path=path, times=1, status=429, retry_after=3))
    code, doc = cli.error("--yes", "--expect-plan", plan["plan_sha256"], *argv)
    assert (code, doc.get("result"), doc.get("message")) == (0, "ok", None), doc
    assert cli.sleeps == [3]
    assert sent(cli, method, path) == 2  # the 429 and the landed one


def test_a_read_5xx_is_retried_three_times_and_a_write_5xx_never(cli: Cli) -> None:
    fail(cli.fake, "GET", r"/issue/DEMO-1/comment$", 503)
    cli.error("comments", "DEMO-1")
    assert sent(cli, "GET", r"/issue/DEMO-1/comment$") == 4
    assert cli.sleeps == [0.5, 1.0, 2.0]


# --- ambiguous outcomes: never resent, and the caller's re-run is idempotent --------------------

# (fault, landed): what the provider did before the answer was lost
LOST = [
    pytest.param(Fault("reset"), False, id="reset-before-apply"),
    pytest.param(Fault("reset", after_apply=True), True, id="reset-after-apply"),
    pytest.param(Fault("close"), False, id="close-before-apply"),
    pytest.param(Fault("close", after_apply=True), True, id="close-after-apply"),
    pytest.param(Fault("headers_only"), True, id="headers-only"),
    pytest.param(Fault("truncated"), True, id="truncated"),
    pytest.param(Fault("status", status=502), False, id="502-before-apply"),
    pytest.param(Fault("status", status=500, after_apply=True), True, id="500-after-apply"),
]


def arm_once(cli: Cli, fault: Fault, method: str, path: str) -> None:
    cli.fake.inject(
        Fault(
            fault.kind,
            method=method,
            path=path,
            times=1,
            after_apply=fault.after_apply,
            status=fault.status,
        )
    )


@pytest.mark.parametrize("fault, landed", LOST)
@pytest.mark.parametrize("argv, method, path", WRITES)
def test_a_lost_answer_is_unknown_sent_once_and_the_rerun_is_idempotent(
    cli: Cli, argv: tuple[str, ...], method: str, path: str, fault: Fault, landed: bool
) -> None:
    code, plan = cli.error("--dry-run", *argv)
    assert code == 0
    arm_once(cli, fault, method, path)
    code, err = cli.error("--yes", "--expect-plan", plan["plan_sha256"], *argv)
    assert (code, err["code"], err["outcome_unknown"]) == (12, "outcome_unknown", True)
    assert sent(cli, method, path) == 1  # never resent by the command
    assert cli.sleeps == []
    first = cli.audit()
    assert [r["result"] for r in first] == ["attempted", "unknown"]

    code, again = cli.apply(*argv)  # the caller's retry
    assert code == 0, again
    if landed:
        assert again["result"] == "unchanged" and again["changed"] is False
        assert sent(cli, method, path) == 1  # nothing sent a second time
        assert cli.audit() == first  # a no-op writes no audit record
    else:
        assert again["result"] == "ok" and again["changed"] is True
        assert sent(cli, method, path) == 2
        assert [r["result"] for r in cli.audit()][-1] == "ok"


def test_a_keyed_comment_unknown_is_retryable_and_its_rerun_finds_the_landed_comment(
    cli: Cli,
) -> None:
    # The keyless case needs a human prompt (--yes requires a key): tests/test_outcomes.py.
    argv = ("comment", "DEMO-2", "--body", "hello", "--dedupe-key", "k-rel")
    code, plan = cli.error("--dry-run", *argv)
    arm_once(cli, Fault("close", after_apply=True), "POST", r"/comment$")
    code, err = cli.error("--yes", "--expect-plan", plan["plan_sha256"], *argv)
    assert (code, err["code"], err["retryable"]) == (12, "outcome_unknown", True)
    code, again = cli.apply(*argv)
    assert (code, again["already_posted"], again["result"]) == (0, True, "unchanged")
    assert sent(cli, "POST", r"/comment$") == 1


@pytest.mark.parametrize("argv, method, path", WRITES)
@pytest.mark.parametrize("kind", ["reset", "close", "headers_only", "truncated"])
def test_no_secret_reaches_the_state_files_or_output_under_any_fault(
    cli: Cli, argv: tuple[str, ...], method: str, path: str, kind: str
) -> None:
    import base64

    code, plan = cli.error("--dry-run", *argv)
    arm_once(cli, Fault(kind), method, path)
    code, out, err = cli.run("--json", "--yes", "--expect-plan", plan["plan_sha256"], *argv)
    assert code == 12
    needles = [SITE_TOKEN, base64.b64encode(f"{SITE_EMAIL}:{SITE_TOKEN}".encode()).decode()]
    state = cli.root / "state"
    texts = [out, err] + [p.read_text(errors="replace") for p in state.rglob("*") if p.is_file()]
    for needle in needles:
        assert not any(needle in text for text in texts)
    assert any((state / "ticketctl").rglob("audit.jsonl"))  # the scan looked at a real audit


# --- a truncated answer -------------------------------------------------------------------------


@pytest.mark.parametrize("argv, path", [*OBJECT_READS, *COLLECTION_READS])
def test_a_body_cut_by_a_closed_connection_is_a_retried_read_failure(
    cli: Cli, argv: tuple[str, ...], path: str
) -> None:
    # The framing promised more bytes than came: a transport failure, retried like a reset
    # (interface.md, Retries), so a read that never completes ends 12 unavailable.
    cli.fake.inject(Fault("truncated", method="GET", path=path, times=None))
    code, err = cli.error(*argv)
    assert (code, err["code"], err["retryable"], err["outcome_unknown"]) == (
        12,
        "unavailable",
        True,
        False,
    )
    assert sent(cli, "GET", path) == 4


def test_a_read_that_is_cut_once_is_retried_and_succeeds(cli: Cli) -> None:
    cli.fake.inject(Fault("truncated", method="GET", path=r"/issue/DEMO-1/comment$", times=1))
    code, doc = cli.error("comments", "DEMO-1")
    assert code == 0 and doc["items"] == []
    assert cli.sleeps == [0.5]


def override(fake: FakeProvider, method: str, suffix: str, out: Out) -> None:
    routes = [
        (m, p, (lambda call, *g: out) if m == method and p.pattern.endswith(suffix) else h)
        for m, p, h in fake._routes
    ]
    fake._routes[:] = routes


CUT_JSON = '{"id": "10001", "body": {"type": "doc", "cont'


@pytest.mark.parametrize(
    "argv, suffix, path",
    [
        (("get", "DEMO-1"), "issue/([^/]+)", r"/issue/DEMO-1$"),
        (("comments", "DEMO-1"), "/comment", r"/comment$"),
        (("transitions", "DEMO-1"), "/transitions", r"/transitions$"),
        (("whoami",), "/myself", r"/myself$"),
    ],
)
def test_a_framed_but_cut_json_body_on_a_read_is_provider_changed(
    cli: Cli, argv: tuple[str, ...], suffix: str, path: str
) -> None:
    override(cli.fake, "GET", suffix, Out(200, CUT_JSON))
    code, err = cli.error(*argv)
    assert (code, err["code"], err["outcome_unknown"]) == (12, "provider_changed", False)
    assert sent(cli, "GET", path) == 1  # not a transport failure: no retry


def test_a_framed_but_cut_json_body_on_a_write_is_unknown_and_sent_once(cli: Cli) -> None:
    argv = ("comment", "DEMO-2", "--body", "hello", "--dedupe-key", "k-cut")
    code, plan = cli.error("--dry-run", *argv)
    override(cli.fake, "POST", "/comment", Out(201, CUT_JSON))
    code, err = cli.error("--yes", "--expect-plan", plan["plan_sha256"], *argv)
    assert (code, err["code"], err["outcome_unknown"]) == (12, "provider_changed", True)
    assert sent(cli, "POST", r"/comment$") == 1
    assert [r["result"] for r in cli.audit()] == ["attempted", "unknown"]


# --- the whole-command deadline -----------------------------------------------------------------


def test_a_retry_after_longer_than_the_remaining_time_ends_a_read_at_once(cli: Cli) -> None:
    cli.fake.quirks.retry_after_format = "seconds"
    fail(cli.fake, "GET", r"/issue/DEMO-1/comment$", 429, retry_after=30)
    code, err = cli.error("--timeout", "2s", "comments", "DEMO-1")
    assert (code, err["code"], err["retry_after_s"]) == (12, "rate_limited", 30)
    assert cli.sleeps == []
    assert sent(cli, "GET", r"/issue/DEMO-1/comment$") == 1


def test_a_slow_read_ends_as_timeout_within_the_command_deadline(cli: Cli) -> None:
    import time

    cli.fake.inject(
        Fault("slow", method="GET", path=r"/issue/DEMO-1/comment$", times=None, delay_s=30.0)
    )
    started = time.monotonic()
    code, err = cli.error("--timeout", "1s", "comments", "DEMO-1")
    assert (code, err["code"], err["outcome_unknown"]) == (12, "timeout", False)
    assert time.monotonic() - started < 10  # generous: the budget is 1 s, CI machines are slow
