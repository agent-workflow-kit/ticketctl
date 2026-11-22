"""The HTTP status mapping and the retry rules through the commands (issue #35).

tests/test_gateway.py proves the transport rules against a scripted server; these tests prove that
every command maps what the gateway raises, and what the endpoint kind decides (object path or
collection, read or write), to the exit code and code of docs/interface.md, "HTTP status mapping".
Faults are the fake provider's (``Fault``); the retry sleeps are recorded, never taken.
"""

from __future__ import annotations

import dataclasses
import json
import re
from pathlib import Path
from typing import Any

import pytest

from tests.fake_provider import OTHER_ACCOUNT, SITE_EMAIL, SITE_TOKEN, FakeProvider, Fault, Out
from tests.scripted_server import closed_port
from tests.test_dedupe_flow import Virtual
from ticketctl import createcmd, runtime
from ticketctl.cli import main
from ticketctl.writeflow import Seams

ENV_VAR = "TICKETCTL_TEST_RELIABILITY_ENV"
OTHER = OTHER_ACCOUNT


class Cli:
    def __init__(self, root: Path, fake: FakeProvider, capsys: Any) -> None:
        self.fake = fake
        self.capsys = capsys
        self.root = root
        self.sleeps: list[float] = []
        self.virtual = Virtual(fake)
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
    # The create lag guard waits on a virtual clock (it also moves the fake's clock), never for real.
    monkeypatch.setattr(createcmd, "make_clock", out.virtual.clock)
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
    assert re.search(r"missing or .*no permission", err["message"])


@pytest.mark.parametrize("argv, path", COLLECTION_READS)
def test_404_on_a_collection_endpoint_is_provider_changed(
    cli: Cli, argv: tuple[str, ...], path: str
) -> None:
    fail(cli.fake, "GET", path, 404)
    code, err = cli.error(*argv)
    assert (code, err["code"]) == (12, "provider_changed")


@pytest.mark.parametrize("status", [404, 410])
def test_the_board_listing_answer_is_provider_changed_and_never_not_found(
    cli: Cli, status: int
) -> None:
    # The unnamed collection (no board is named) is a provider change; only a named board's 404
    # is not_found (below). The message and the missing option tell the two mappings apart.
    fail(cli.fake, "GET", r"/rest/agile/1.0/board$", status)
    code, err = cli.error("sprints", "list")
    assert (code, err["code"], err["reason"]) == (12, "provider_changed", None)
    assert re.search(rf"unexpected answer from the provider \({status}\)", err["message"])
    assert "option" not in (err["details"] or {})


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
    # A key that fails the grammar exits 22 invalid_key locally (interface.md, Identifiers): it
    # never reaches the provider, so it cannot be told from a hidden or missing ticket by a request.
    # The answers differ on purpose; only the provider's 404s are one answer.
    code, err = cli.error(command, key)
    assert (code, err["code"], err["reason"]) == (22, "invalid_request", "invalid_key")
    assert cli.fake.calls == []


@pytest.mark.parametrize("argv", [("get",), ("comments",), ("transitions",)])
def test_a_well_formed_nonexistent_key_answers_like_a_hidden_one(
    cli: Cli, argv: tuple[str, ...]
) -> None:
    code_far, far = cli.error(*argv, "DEMO-999999999")
    fail(cli.fake, "GET", r"/issue/DEMO-2", 404, body=ERROR_BODIES["error-messages"])
    code_hidden, hidden = cli.error(*argv, "DEMO-2")
    assert code_far == code_hidden == 10
    assert (far["code"], far["reason"], far["message"]) == (
        hidden["code"],
        hidden["reason"],
        hidden["message"],
    )


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
    pytest.param(
        ("create", "--project", "DEMO", "--type", "Task", "--title", "Rel", "--dedupe-key", "k-new"),
        "POST",
        r"/issue$",
        id="create",
    ),
]


def sent(cli: Cli, method: str, path: str) -> int:
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


# shape -> the sanitized details the envelope carries (interface.md, Error envelope)
REJECTED_DETAILS = {
    "error-messages": {"errorMessages": ["Issue does not exist"]},
    "errors-map": {"field_errors": {"key": "bad key"}},
    "single-error": {"errorMessages": ["hidden"]},
    "plain-text": {"errorMessages": ["Not Found"]},
    "empty": {},
}


@pytest.mark.parametrize("shape", sorted(ERROR_BODIES))
@pytest.mark.parametrize("argv, method, path", WRITES)
def test_a_400_on_a_write_carries_the_providers_sanitized_messages_in_every_body_shape(
    cli: Cli, argv: tuple[str, ...], method: str, path: str, shape: str
) -> None:
    code, plan = cli.error("--dry-run", *argv)
    fail(cli.fake, method, path, 400, body=ERROR_BODIES[shape])
    code, err = cli.error("--yes", "--expect-plan", plan["plan_sha256"], *argv)
    assert (code, err["code"], err["outcome_unknown"]) == (12, "rejected", False)
    details = {k: v for k, v in err["details"].items() if k != "attempt_id"}
    assert details == REJECTED_DETAILS[shape]
    assert sent(cli, method, path) == 1


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


@pytest.mark.parametrize("argv, method, path", [w for w in WRITES if w.id != "create"])
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
    assert err["retryable"] is True  # every WRITES row is keyed or idempotent: the re-run is safe
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


def test_a_read_5xx_is_retried_three_times_then_unavailable(cli: Cli) -> None:
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
    assert err["retryable"] is True  # keyed or idempotent rows: the re-run recipe applies
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


# --- create: what its single POST and its dedupe lookup add to the rules above -------------------

CREATE = ("create", "--project", "DEMO", "--type", "Task", "--title", "Rel")
KEYED = (*CREATE, "--dedupe-key", "k-rel")
ISSUE_POST = r"/issue$"
SEARCH = r"/search/jql$"


def test_404_on_the_create_post_is_provider_changed_not_not_found(cli: Cli) -> None:
    # Pinned current behaviour: POST /issue names no object, so it is a collection write and
    # CreateAction maps it with object_path=False. interface.md's status table does not list it.
    code, plan = cli.error("--dry-run", *KEYED)
    fail(cli.fake, "POST", ISSUE_POST, 404)
    code, err = cli.error("--yes", "--expect-plan", plan["plan_sha256"], *KEYED)
    assert (code, err["code"], err["retryable"], err["outcome_unknown"]) == (
        12,
        "provider_changed",
        False,
        False,
    )
    assert sent(cli, "POST", ISSUE_POST) == 1
    assert cli.sleeps == []


def test_410_on_the_create_post_is_rejected_like_any_other_4xx_on_a_write(cli: Cli) -> None:
    # Pinned current behaviour: raise_for_write maps only 404 specially, so a 410 is "rejected"
    # although the table says any 410 on a collection endpoint is provider_changed (a read rule).
    code, plan = cli.error("--dry-run", *KEYED)
    fail(cli.fake, "POST", ISSUE_POST, 410)
    code, err = cli.error("--yes", "--expect-plan", plan["plan_sha256"], *KEYED)
    assert (code, err["code"], err["outcome_unknown"]) == (12, "rejected", False)
    assert sent(cli, "POST", ISSUE_POST) == 1


@pytest.mark.parametrize("status", [404, 410])
def test_a_missing_createmeta_is_not_found_only_on_404(cli: Cli, status: int) -> None:
    # The createmeta reads name a project and a type, so 404 is "project or type missing" (exit 10).
    # A 410 is not mapped there: it is an unusable answer, 12 provider_changed.
    fail(cli.fake, "GET", r"/issue/createmeta/DEMO/issuetypes$", status)
    code, err = cli.error("--dry-run", *CREATE)
    assert (code, err["code"]) == ((10, "not_found") if status == 404 else (12, "provider_changed"))
    assert sent(cli, "POST", ISSUE_POST) == 0


def test_a_keyless_create_with_a_lost_answer_is_unknown_and_not_retryable(
    cli: Cli, monkeypatch: pytest.MonkeyPatch
) -> None:
    # --yes needs a key, so the keyless run goes through the (substituted) confirmation dialog.
    monkeypatch.setattr(
        createcmd, "make_seams", lambda: Seams(interactive=lambda: True, confirm=lambda p: True)
    )
    arm_once(cli, Fault("close", after_apply=True), "POST", ISSUE_POST)
    code, err = cli.error("--actor", "human:tester", *CREATE)  # an auto actor is never asked
    assert (code, err["code"], err["retryable"], err["outcome_unknown"]) == (
        12,
        "outcome_unknown",
        False,
        True,
    )
    assert re.search(r"--dedupe-key", err["message"])
    assert sent(cli, "POST", ISSUE_POST) == 1
    assert len(cli.fake.tickets) == 4  # the three seeded and the landed one


def test_a_framed_but_cut_json_create_answer_is_unknown_and_not_retryable(cli: Cli) -> None:
    code, plan = cli.error("--dry-run", *KEYED)
    override(cli.fake, "POST", "/issue", Out(201, CUT_JSON))
    code, err = cli.error("--yes", "--expect-plan", plan["plan_sha256"], *KEYED)
    assert (code, err["code"], err["outcome_unknown"]) == (12, "provider_changed", True)
    assert err["retryable"] is False
    assert sent(cli, "POST", ISSUE_POST) == 1
    assert [r["result"] for r in cli.audit()] == ["attempted", "unknown"]


@pytest.mark.parametrize(
    "fault",
    [Fault("close", after_apply=True), Fault("reset"), Fault("status", status=503)],
    ids=["close-after-apply", "reset", "503"],
)
def test_create_is_one_step_so_a_failure_is_never_partial(cli: Cli, fault: Fault) -> None:
    code, plan = cli.error("--dry-run", *KEYED)
    arm_once(cli, fault, "POST", ISSUE_POST)
    code, err = cli.error("--yes", "--expect-plan", plan["plan_sha256"], *KEYED)
    assert err["code"] == "outcome_unknown"
    assert "result" not in (err["details"] or {})  # a partial result lists its steps
    assert [r["step"] for r in cli.audit()] == [1, 1]  # attempted, unknown: one step only


def test_the_rerun_after_a_lost_create_waits_on_the_virtual_clock_and_finds_the_ticket(
    cli: Cli,
) -> None:
    code, plan = cli.error("--dry-run", *KEYED)
    arm_once(cli, Fault("close", after_apply=True), "POST", ISSUE_POST)
    code, err = cli.error("--yes", "--expect-plan", plan["plan_sha256"], *KEYED)
    assert err["code"] == "outcome_unknown"
    assert cli.virtual.waits == []
    code, again = cli.apply(*KEYED)
    assert (code, again["reused"], again["result"]) == (0, True, "unchanged")
    assert sent(cli, "POST", ISSUE_POST) == 1  # one search found it; no second POST
    # The guard waited on the virtual clock (2 s quick re-run, 5 s polls), never for real.
    assert cli.virtual.waits and set(cli.virtual.waits) <= {2.0, 5.0}
    assert cli.sleeps == []


def test_a_quick_rerun_of_a_landed_create_repeats_the_lookup_once_after_two_seconds(
    cli: Cli,
) -> None:
    code, first = cli.apply(*KEYED)
    assert (code, first["result"]) == (0, "ok")
    cli.virtual.waits.clear()
    code, again = cli.apply(*KEYED)
    assert (code, again["reused"]) == (0, True)
    assert sent(cli, "POST", ISSUE_POST) == 1
    assert set(cli.virtual.waits) <= {2.0}


def test_a_reused_label_rerun_costs_one_search_and_no_post(cli: Cli) -> None:
    code, first = cli.apply(*KEYED)
    assert code == 0
    searches = sent(cli, "POST", SEARCH)
    code, again = cli.apply(*KEYED)
    assert (code, again["reused"]) == (0, True)
    assert sent(cli, "POST", ISSUE_POST) == 1
    assert sent(cli, "POST", SEARCH) > searches


@pytest.mark.parametrize(
    "answer",
    [
        {"issues": [], "nextPageToken": "more"},
        {"issues": [{"id": str(n), "key": f"DEMO-{n}"} for n in range(101, 202)]},
    ],
    ids=["next-page-token", "over-100-results"],
)
def test_a_label_lookup_that_cannot_prove_absence_is_lookup_incomplete(
    cli: Cli, answer: dict[str, Any]
) -> None:
    override(cli.fake, "POST", "search/jql", Out(200, json.dumps(answer)))
    code, err = cli.error("--dry-run", *KEYED)
    assert (code, err["code"], err["retryable"]) == (12, "lookup_incomplete", False)
    assert sent(cli, "POST", ISSUE_POST) == 0


def test_a_label_lookup_with_an_unusable_answer_is_provider_changed(cli: Cli) -> None:
    override(cli.fake, "POST", "search/jql", Out(200, json.dumps({"unexpected": True})))
    code, err = cli.error("--dry-run", *KEYED)
    assert (code, err["code"]) == (12, "provider_changed")
    assert sent(cli, "POST", ISSUE_POST) == 0


@pytest.mark.parametrize("status", [401, 403, 404, 429, 503])
def test_the_label_lookup_follows_the_search_rows(cli: Cli, status: int) -> None:
    expected = {
        401: "bad_credentials",
        403: "forbidden",
        404: "provider_changed",
        429: "rate_limited",
        503: "unavailable",
    }[status]
    fail(cli.fake, "POST", SEARCH, status)
    code, err = cli.error("--dry-run", *KEYED)
    assert (code, err["code"]) == (12, expected)
    assert sent(cli, "POST", ISSUE_POST) == 0


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
    assert err["retryable"] is False  # the contract changed: check the ticket before any re-run
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


# --- a write whose connection never opened ------------------------------------------------------


@pytest.fixture
def closed_writes(cli: Cli, monkeypatch: pytest.MonkeyPatch) -> None:
    """Reads reach the fake; every write is sent to a loopback port that nothing listens on."""
    inner = runtime.Gateway
    dead = f"http://127.0.0.1:{closed_port()}"

    def gateway(*args: Any, **kw: Any) -> Any:
        gw = inner(*args, **kw)
        send = gw.request

        def request(method: str, path: str, **opts: Any) -> Any:
            write = opts.get("write")
            if not (method != "GET" if write is None else write):
                return send(method, path, **opts)
            live = gw.target
            gw.target = dataclasses.replace(live, url=dead)
            try:
                return send(method, path, **opts)
            finally:
                gw.target = live

        monkeypatch.setattr(gw, "request", request)
        return gw

    monkeypatch.setattr(runtime, "Gateway", gateway)


@pytest.mark.parametrize("argv, method, path", WRITES)
def test_a_write_whose_connection_never_opens_is_retried_then_unavailable(
    cli: Cli, closed_writes: None, argv: tuple[str, ...], method: str, path: str
) -> None:
    # No request byte can have left, so the write is retried twice like a read and nothing is
    # unknown (interface.md, Retries).
    code, plan = cli.error("--dry-run", *argv)
    assert code == 0, plan
    code, err = cli.error("--yes", "--expect-plan", plan["plan_sha256"], *argv)
    assert (code, err["code"], err["retryable"], err["outcome_unknown"]) == (
        12,
        "unavailable",
        True,
        False,
    )
    assert cli.sleeps == [0.5, 1.0]
    assert sent(cli, method, path) == 0


# --- the retry delay counts against --timeout ---------------------------------------------------

COMMENTS = r"/issue/DEMO-1/comment$"


@pytest.mark.parametrize("status", [503, 429])
def test_a_status_backoff_longer_than_the_remaining_time_ends_a_read_as_timeout(
    cli: Cli, status: int
) -> None:
    # The second delay is 1.0 s and a budget of 1 s has been spent a little by then.
    # Timing margin: the first 0.5 s backoff is taken only if under 0.5 s of the 1 s budget has
    # elapsed (generous: 0.5 s of stall before the first retry would be needed to fail).
    fail(cli.fake, "GET", COMMENTS, status)
    code, err = cli.error("--timeout", "1s", "comments", "DEMO-1")
    assert (code, err["code"], err["outcome_unknown"]) == (12, "timeout", False)
    assert cli.sleeps == [0.5]  # never a sleep the budget cannot cover
    assert sent(cli, "GET", COMMENTS) == 2


def test_a_failure_backoff_longer_than_the_remaining_time_ends_a_read_as_timeout(
    cli: Cli,
) -> None:
    # Timing margin: the first 0.5 s backoff is taken only if under 0.5 s of the 1 s budget has
    # elapsed (generous: 0.5 s of stall before the first retry would be needed to fail).
    cli.fake.inject(Fault("reset", method="GET", path=COMMENTS, times=None))
    code, err = cli.error("--timeout", "1s", "comments", "DEMO-1")
    assert (code, err["code"], err["outcome_unknown"]) == (12, "timeout", False)
    assert cli.sleeps == [0.5]
    assert sent(cli, "GET", COMMENTS) == 2


@pytest.mark.parametrize("argv, method, path", WRITES)
def test_a_429_backoff_longer_than_the_remaining_time_ends_a_write_as_timeout_nothing_unknown(
    cli: Cli, argv: tuple[str, ...], method: str, path: str
) -> None:
    # Timing margin: the first 0.5 s backoff is taken only if under 0.5 s of the 1 s budget has
    # elapsed (generous: 0.5 s of stall before the first retry would be needed to fail).
    code, plan = cli.error("--dry-run", *argv)
    fail(cli.fake, method, path, 429)
    code, err = cli.error("--timeout", "1s", "--yes", "--expect-plan", plan["plan_sha256"], *argv)
    assert (code, err["code"], err["outcome_unknown"]) == (12, "timeout", False)
    assert cli.sleeps == [0.5]
    assert sent(cli, method, path) == 2


@pytest.mark.parametrize("argv, method, path", WRITES)
def test_a_connect_failure_backoff_longer_than_the_remaining_time_ends_a_write_as_timeout(
    cli: Cli, closed_writes: None, argv: tuple[str, ...], method: str, path: str
) -> None:
    # Timing margin: the first 0.5 s backoff is taken only if under 0.5 s of the 1 s budget has
    # elapsed (generous: 0.5 s of stall before the first retry would be needed to fail).
    code, plan = cli.error("--dry-run", *argv)
    code, err = cli.error("--timeout", "1s", "--yes", "--expect-plan", plan["plan_sha256"], *argv)
    assert (code, err["code"], err["outcome_unknown"]) == (12, "timeout", False)
    assert cli.sleeps == [0.5]


@pytest.mark.parametrize("argv, method, path", WRITES)
def test_a_write_reset_after_send_is_outcome_unknown_at_once_without_a_retry(
    cli: Cli, argv: tuple[str, ...], method: str, path: str
) -> None:
    code, plan = cli.error("--dry-run", *argv)
    cli.fake.inject(Fault("reset", method=method, path=path, times=None))
    code, err = cli.error("--timeout", "1s", "--yes", "--expect-plan", plan["plan_sha256"], *argv)
    assert (code, err["code"], err["outcome_unknown"]) == (12, "outcome_unknown", True)
    assert cli.sleeps == []
    assert sent(cli, method, path) == 1


@pytest.mark.parametrize("argv, method, path", WRITES)
def test_a_write_still_unanswered_at_the_deadline_is_outcome_unknown_never_timeout(
    cli: Cli, argv: tuple[str, ...], method: str, path: str
) -> None:
    import time

    code, plan = cli.error("--dry-run", *argv)
    cli.fake.inject(Fault("slow", method=method, path=path, times=None, delay_s=30.0))
    started = time.monotonic()
    code, err = cli.error("--timeout", "1s", "--yes", "--expect-plan", plan["plan_sha256"], *argv)
    assert (code, err["code"], err["outcome_unknown"]) == (12, "outcome_unknown", True)
    assert sent(cli, method, path) == 1
    assert time.monotonic() - started < 10  # generous: the budget is 1 s, CI machines are slow


def test_a_pacing_sleep_is_not_taken_when_it_would_outlast_the_budget(cli: Cli) -> None:
    cli.fake.quirks.rate_limit_limit = 100
    cli.fake.quirks.rate_limit_remaining = 5
    code, _ = cli.error("--timeout", "1s", "sprints", "list")
    assert code == 0
    assert len(cli.fake.calls) >= 2
    assert cli.sleeps == []


# --- the Retry-After cap, isolated from the remaining time --------------------------------------


def test_a_retry_after_above_the_cap_but_within_the_time_ends_a_read_at_once(cli: Cli) -> None:
    # The cap is limits.retry_after_max (conftest sets 10 s); 120 s remain, and 90 s is above both,
    # so only the cap decides.
    cli.fake.quirks.retry_after_format = "seconds"
    fail(cli.fake, "GET", COMMENTS, 429, retry_after=90)
    code, err = cli.error("--timeout", "120s", "comments", "DEMO-1")
    assert (code, err["code"], err["retryable"], err["retry_after_s"]) == (
        12,
        "rate_limited",
        True,
        90,
    )
    assert cli.sleeps == []
    assert sent(cli, "GET", COMMENTS) == 1


@pytest.mark.parametrize("argv, method, path", WRITES)
def test_a_retry_after_above_the_cap_but_within_the_time_ends_a_write_at_once(
    cli: Cli, argv: tuple[str, ...], method: str, path: str
) -> None:
    cli.fake.quirks.retry_after_format = "seconds"
    code, plan = cli.error("--dry-run", *argv)
    fail(cli.fake, method, path, 429, retry_after=90)
    code, err = cli.error("--timeout", "120s", "--yes", "--expect-plan", plan["plan_sha256"], *argv)
    assert (code, err["code"], err["outcome_unknown"], err["retry_after_s"]) == (
        12,
        "rate_limited",
        False,
        90,
    )
    assert cli.sleeps == []
    assert sent(cli, method, path) == 1


# --- a status that decides beats an unreadable body ---------------------------------------------

LONG_BODY = "x" * 64
SAMPLES = {
    "create": "/rest/api/3/issue",  # first: its path pattern also matches the sprint sample
    "comment": "/rest/api/3/issue/DEMO-2/comment",
    "transition": "/rest/api/3/issue/DEMO-2/transitions",
    "update": "/rest/api/3/issue/DEMO-2/assignee",
    "sprint-add": "/rest/agile/1.0/sprint/2/issue",
    "read": "/rest/api/3/issue/DEMO-1/comment",
}


def answer_cut(fake: FakeProvider, method: str, sample: str, status: int, path: str) -> None:
    """Answer ``method sample`` with ``status`` and a body cut short of its Content-Length."""
    out = Out(status, LONG_BODY)
    fake._routes[:] = [
        (m, p, (lambda call, *g: out) if m == method and p.fullmatch(sample) else h)
        for m, p, h in fake._routes
    ]
    fake.inject(Fault("truncated", method=method, path=path, times=None))


# status -> (code, retryable, requests sent) when the body is cut
CUT_READ_ROWS = {
    401: ("bad_credentials", False, 1),
    403: ("forbidden", False, 1),
    429: ("rate_limited", True, 4),
    503: ("unavailable", True, 4),
}


@pytest.mark.parametrize("status", sorted(CUT_READ_ROWS))
def test_a_read_status_beats_a_body_cut_short(cli: Cli, status: int) -> None:
    answer_cut(cli.fake, "GET", SAMPLES["read"], status, COMMENTS)
    code, err = cli.error("comments", "DEMO-1")
    expected, retryable, requests = CUT_READ_ROWS[status]
    assert (code, err["code"], err["retryable"], err["outcome_unknown"]) == (
        12,
        expected,
        retryable,
        False,
    )
    assert sent(cli, "GET", COMMENTS) == requests
    assert not re.search(r"could not reach", err["message"])  # not the transport-failure mapping


@pytest.mark.parametrize("status", [401, 403, 429])
@pytest.mark.parametrize("argv, method, path", WRITES)
def test_a_write_status_beats_a_body_cut_short(
    cli: Cli, argv: tuple[str, ...], method: str, path: str, status: int
) -> None:
    code, plan = cli.error("--dry-run", *argv)
    sample = SAMPLES[next(k for k, v in SAMPLES.items() if re.search(path, v))]
    answer_cut(cli.fake, method, sample, status, path)
    code, err = cli.error("--yes", "--expect-plan", plan["plan_sha256"], *argv)
    expected = {401: "bad_credentials", 403: "forbidden", 429: "rate_limited"}[status]
    assert (code, err["code"], err["outcome_unknown"]) == (12, expected, False)
    assert sent(cli, method, path) == (3 if status == 429 else 1)


# --- pacing and the rate-limit detail -----------------------------------------------------------


def test_a_low_remaining_count_paces_the_next_request_of_the_same_command(cli: Cli) -> None:
    cli.fake.quirks.rate_limit_limit = 100
    cli.fake.quirks.rate_limit_remaining = 5  # below 10% of the limit
    code, _ = cli.error("sprints", "list")
    assert code == 0
    assert len(cli.fake.calls) >= 2
    assert cli.sleeps == [1.0] * (len(cli.fake.calls) - 1)


def test_a_healthy_remaining_count_does_not_pace(cli: Cli) -> None:
    cli.fake.quirks.rate_limit_limit = 100
    cli.fake.quirks.rate_limit_remaining = 50
    code, _ = cli.error("sprints", "list")
    assert code == 0
    assert len(cli.fake.calls) >= 2
    assert cli.sleeps == []


def test_a_429_end_carries_the_rate_limit_values_on_a_read(cli: Cli) -> None:
    cli.fake.quirks.rate_limit_limit = 100
    cli.fake.quirks.rate_limit_remaining = 3
    fail(cli.fake, "GET", COMMENTS, 429)
    code, err = cli.error("comments", "DEMO-1")
    assert (code, err["code"]) == (12, "rate_limited")
    assert err["details"]["rate_limit"] == {"limit": 100, "remaining": 3}


@pytest.mark.parametrize("argv, method, path", WRITES)
def test_a_429_end_carries_the_rate_limit_values_on_a_write(
    cli: Cli, argv: tuple[str, ...], method: str, path: str
) -> None:
    code, plan = cli.error("--dry-run", *argv)
    cli.fake.quirks.rate_limit_limit = 100
    cli.fake.quirks.rate_limit_remaining = 3
    fail(cli.fake, method, path, 429)
    code, err = cli.error("--yes", "--expect-plan", plan["plan_sha256"], *argv)
    assert (code, err["code"]) == (12, "rate_limited")
    assert err["details"]["rate_limit"] == {"limit": 100, "remaining": 3}
