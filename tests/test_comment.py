"""The ``comment`` command end to end against the fake provider (issue #29)."""

from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from tests.fake_provider import SITE_EMAIL, SITE_TOKEN, FakeProvider, Fault
from tests.golden import assert_golden, validator
from ticketctl import attribution, commentcmd, config, hooks
from ticketctl.adf_validate import body_sha256
from ticketctl.cli import main
from ticketctl.errors import TicketctlError
from ticketctl.flags import Globals
from ticketctl.gateway import HookRun
from ticketctl.runtime import load_runtime
from ticketctl.writeflow import Seams, execute

SITE_ENV_VAR = "TICKETCTL_TEST_SITE_ENV"
ATTEMPT = "0123456789abcdef"
REAL_MAKE_SEAMS = commentcmd.make_seams
BODY_MARK = "zq7x-body-marker-4f9c2e"  # cannot occur by accident; must never reach a hook
SITE_URL = "https://tracker.example.test"


class Cli:
    def __init__(self, root: Path, fake: FakeProvider, capsys: Any) -> None:
        self.fake = fake
        self.capsys = capsys
        self.root = root
        self.config = root / "config.yaml"
        self.config.write_text(
            f"sites:\n  main:\n    url: {fake.url}\n    email: {SITE_EMAIL}\n"
            f"    token_env: {SITE_ENV_VAR}\n    allow_http: true\n"
        )
        self.config.chmod(0o600)

    def run(self, *argv: str) -> tuple[int, str, str]:
        code = main(["--config", str(self.config), *argv])
        cap = self.capsys.readouterr()
        assert SITE_TOKEN not in cap.out + cap.err
        return code, cap.out, cap.err

    def doc(self, *argv: str) -> tuple[int, dict[str, Any]]:
        code, out, _ = self.run("--json", *argv)
        return code, json.loads(out)

    def norm(self, doc: dict[str, Any]) -> dict[str, Any]:
        return json.loads(json.dumps(doc).replace(self.fake.url, SITE_URL))

    def post(self, *argv: str, key: str = "DEMO-2") -> tuple[int, dict[str, Any]]:
        """Plan with --dry-run, then apply that plan with --yes."""
        code, plan = self.doc("--dry-run", "comment", key, *argv)
        assert code == 0, plan
        return self.doc(
            "--yes", "--expect-plan", plan["plan_sha256"], "comment", key, *argv,
        )  # fmt: skip

    def writes(self) -> list[Any]:
        return [c for c in self.fake.calls if c.method != "GET"]


@pytest.fixture
def cli(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: Any, fake: FakeProvider) -> Cli:
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    monkeypatch.setenv(SITE_ENV_VAR, SITE_TOKEN)
    monkeypatch.setenv("PATH", "/nonexistent")  # a test can never start a real program by PATH
    monkeypatch.setattr(commentcmd, "make_seams", lambda: Seams(attempt_id=lambda: ATTEMPT))
    return Cli(tmp_path, fake, capsys)


def test_posts_the_comment_with_the_property_and_golden(cli: Cli) -> None:
    code, result = cli.post("--body", "hello **world**", "--dedupe-key", "k1")
    assert code == 0 and result["result"] == "ok" and result["changed"] is True
    assert result["steps"] == [{"kind": "add_comment", "status": "done"}]
    assert re.fullmatch(r"[0-9]+", result["comment_id"])
    assert result["attempt_id"] == ATTEMPT and result["already_posted"] is False
    validator("cli/comment.json").validate(result)
    assert_golden("cli", "comment", "ok", cli.norm(result))
    posted = cli.fake.tickets["DEMO-2"].comments[-1]
    assert posted["body"]["type"] == "doc"
    meta = posted["properties"][attribution.META_PROPERTY]
    assert meta["v"] == 1 and meta["actor_kind"] == "auto" and "key_sha256" in meta
    assert len(cli.writes()) == 1


@pytest.mark.parametrize("actor", ["auto:bot", "human:tester"])
def test_the_property_body_hash_is_the_hash_of_the_posted_body(cli: Cli, actor: str) -> None:
    args = ("--actor", actor, "comment", "DEMO-2", "--body", "hi **x**", "--dedupe-key", "h1")
    code, plan = cli.doc("--dry-run", *args)
    assert code == 0, plan
    code, result = cli.doc("--yes", "--expect-plan", plan["plan_sha256"], *args)
    assert code == 0, result
    posted = cli.fake.tickets["DEMO-2"].comments[-1]
    meta = posted["properties"][attribution.META_PROPERTY]
    assert meta["body_sha256"] == body_sha256(posted["body"])
    if actor.startswith("auto:"):  # the hash covers the attribution paragraph that leads the body
        text = attribution.attribution_text(config.Attribution().template)
        assert posted["body"]["content"][0]["content"][0]["text"] == text
    assert meta["actor_kind"] == actor.split(":")[0]


def test_dry_run_is_a_plan_and_sends_nothing(cli: Cli) -> None:
    code, plan = cli.doc("--dry-run", "comment", "DEMO-2", "--body", "hi")
    assert code == 0 and plan["steps"][0]["kind"] == "add_comment"
    assert plan["changes"] == [] and plan["dedupe_match"] is None
    validator("cli/comment.json").validate(plan)
    assert_golden("cli", "comment", "dry-run", cli.norm(plan))
    assert cli.writes() == []


def test_yes_with_expect_plan_but_no_dedupe_key_is_dedupe_key_required(cli: Cli) -> None:
    code, env = cli.doc("--yes", "--expect-plan", "0" * 64, "comment", "DEMO-2", "--body", "hi")
    assert (code, env["error"]["reason"]) == (2, "dedupe_key_required")
    assert cli.writes() == []


def test_without_yes_a_non_interactive_run_needs_confirmation(cli: Cli) -> None:
    code, env = cli.doc("comment", "DEMO-2", "--body", "hi")
    assert (code, env["error"]["code"]) == (13, "confirmation_required")
    assert cli.writes() == []


@pytest.mark.parametrize(
    "argv", [[], ["--body", "a", "--body-file", "x.md"]], ids=["neither", "both"]
)
def test_body_and_body_file_exactly_one(cli: Cli, argv: list[str]) -> None:
    code, env = cli.doc("--dry-run", "comment", "DEMO-2", *argv)
    assert (code, env["error"]["reason"]) == (2, "invalid_combination")


def test_format_adf_needs_a_body_file(cli: Cli) -> None:
    code, env = cli.doc("--dry-run", "comment", "DEMO-2", "--body", "{}", "--format", "adf")
    assert (code, env["error"]["reason"]) == (2, "invalid_combination")


def test_wiki_markup_reaches_the_gateway_only_as_text_nodes(cli: Cli) -> None:
    body = "*x* and\n\nh2. title\n\n{code}\nprint(1)\n{code}"
    code, result = cli.post("--body", body, "--dedupe-key", "w1")
    assert code == 0, result
    sent = next(c for c in cli.writes() if c.path.endswith("/comment"))
    doc = sent.body["body"]
    texts: list[str] = []
    stack: list[Any] = [doc]
    while stack:
        node = stack.pop()
        if isinstance(node, dict):
            if "text" in node:
                texts.append(node["text"])
                assert node["type"] == "text"
            stack.extend(node.get("content", []))
    joined = "\n".join(texts)
    # `*x*` is Markdown emphasis (an ADF mark), never wiki bold; `h2.` and `{code}` stay literal
    assert "h2. title" in joined and "{code}" in joined and "*x*" not in joined
    assert doc["type"] == "doc"


def test_literal_wiki_text_in_an_adf_body_is_sent_verbatim(cli: Cli) -> None:
    adf = {
        "type": "doc",
        "version": 1,
        "content": [{"type": "paragraph", "content": [{"type": "text", "text": "*x* h2. {code}"}]}],
    }
    path = cli.root / "wiki.json"
    path.write_text(json.dumps(adf))
    code, result = cli.post("--body-file", str(path), "--format", "adf", "--dedupe-key", "w2")
    assert code == 0, result
    sent = next(c for c in cli.writes() if c.path.endswith("/comment"))
    assert sent.body["body"]["content"][-1]["content"][0] == {
        "type": "text",
        "text": "*x* h2. {code}",
    }


def test_body_file_and_adf_format(cli: Cli) -> None:
    adf = {
        "type": "doc",
        "version": 1,
        "content": [{"type": "paragraph", "content": [{"type": "text", "text": "from adf"}]}],
    }
    path = cli.root / "c.json"
    path.write_text(json.dumps(adf))
    code, result = cli.post("--body-file", str(path), "--format", "adf", "--dedupe-key", "a1")
    assert code == 0, result
    body = cli.fake.tickets["DEMO-2"].comments[-1]["body"]
    flat = json.dumps(body)
    assert "from adf" in flat


def test_invalid_adf_is_refused_before_any_request(cli: Cli) -> None:
    path = cli.root / "bad.json"
    path.write_text('{"type":"doc","version":1,"content":[{"type":"nope"}]}')
    code, env = cli.doc(
        "--dry-run", "comment", "DEMO-2", "--body-file", str(path), "--format", "adf"
    )
    assert (code, env["error"]["code"], env["error"]["reason"]) == (
        22, "invalid_request", "node_not_allowed",
    )  # fmt: skip
    assert cli.fake.calls == []


def test_a_body_of_32768_characters_is_body_too_large(cli: Cli) -> None:
    code, env = cli.doc("--dry-run", "comment", "DEMO-2", "--body", "a" * 32768)
    assert (code, env["error"]["reason"]) == (22, "body_too_large")
    assert cli.fake.calls == []


def test_an_empty_body_is_refused(cli: Cli) -> None:
    code, env = cli.doc("--dry-run", "comment", "DEMO-2", "--body", "   ")
    assert (code, env["error"]["reason"]) == (22, "empty_body")


def test_a_secret_in_the_body_is_refused_without_a_request(cli: Cli) -> None:
    code, env = cli.doc("--dry-run", "comment", "DEMO-2", "--body", "token " + SITE_TOKEN)
    assert (code, env["error"]["reason"]) == (22, "secret_in_body")
    assert cli.fake.calls == []


def test_a_comment_on_a_done_ticket_works_and_warns(cli: Cli) -> None:
    done = next(k for k, t in cli.fake.tickets.items() if t.status == "Done")
    code, plan = cli.doc("--dry-run", "comment", done, "--body", "late note")
    assert code == 0 and plan["warnings"] == ["closed_ticket"]
    code, result = cli.post("--body", "late note", "--dedupe-key", "c1", key=done)
    assert code == 0 and result["result"] == "ok" and result["warnings"] == ["closed_ticket"]
    assert_golden("cli", "comment", "closed-ticket", cli.norm(result))


def test_an_open_ticket_has_no_closed_warning(cli: Cli) -> None:
    code, plan = cli.doc("--dry-run", "comment", "DEMO-2", "--body", "x")
    assert code == 0 and "closed_ticket" not in plan["warnings"]


def test_body_near_limit_warns(cli: Cli) -> None:
    code, plan = cli.doc("--dry-run", "comment", "DEMO-2", "--body", "a" * 29000)
    assert code == 0 and "body_near_limit" in plan["warnings"]


def test_a_missing_ticket_is_not_found(cli: Cli) -> None:
    code, env = cli.doc("--dry-run", "comment", "DEMO-999", "--body", "x")
    assert (code, env["error"]["code"]) == (10, "not_found")


def test_a_ticket_that_changed_does_not_stale_the_plan(cli: Cli) -> None:
    _, plan = cli.doc("--dry-run", "comment", "DEMO-2", "--body", "x", "--dedupe-key", "s1")
    cli.fake.tickets["DEMO-2"].status = "In Progress"
    code, result = cli.doc(
        "--yes", "--expect-plan", plan["plan_sha256"], "comment", "DEMO-2", "--body", "x",
        "--dedupe-key", "s1",
    )  # fmt: skip
    assert code == 0 and result["result"] == "ok"


def test_the_plan_hash_changes_with_the_body_and_the_key(cli: Cli) -> None:
    def h(*extra: str) -> str:
        return cli.doc("--dry-run", "comment", "DEMO-2", *extra)[1]["plan_sha256"]

    base = h("--body", "one")
    assert base == h("--body", "one")
    assert base != h("--body", "two")
    assert base != h("--body", "one", "--dedupe-key", "k")


def test_a_provider_refusal_is_rejected(cli: Cli) -> None:
    cli.fake.inject(Fault("status", status=400, method="POST", path="/comment"))
    code, env = cli.post("--body", "x", "--dedupe-key", "r1")
    assert (code, env["error"]["code"]) == (12, "rejected")
    validator("cli/error.json").validate(env)


def test_the_seam_is_not_asked_without_a_dedupe_key(
    cli: Cli, monkeypatch: pytest.MonkeyPatch
) -> None:
    def boom(self: Any, ctx: Any, rich: Any) -> str:
        raise AssertionError("looked up without a key")

    monkeypatch.setattr(commentcmd.CommentAction, "find_existing", boom)
    code, plan = cli.doc("--dry-run", "comment", "DEMO-2", "--body", "x")
    assert code == 0


def test_the_hook_payloads_come_from_the_real_runner_and_match_the_schemas(
    cli: Cli, monkeypatch: pytest.MonkeyPatch
) -> None:
    cli.config.write_text(
        cli.config.read_text()
        + "hooks:\n  pre-comment:\n    - cmd: ignored\n  post-comment:\n    - cmd: ignored\n"
    )
    seen: dict[str, bytes] = {}

    def fake_run(argv: list[str], *, stdin: bytes, env: dict[str, str], **kw: Any) -> HookRun:
        seen[env["TICKETCTL_HOOK"]] = stdin
        return HookRun(1, 0, b"", b"", False, False, False)

    monkeypatch.setattr(hooks, "run_hook", fake_run)
    body = f"hello {BODY_MARK} and a secret-looking sk-live-{BODY_MARK}"
    code, result = cli.post("--body", body, "--dedupe-key", "h1")
    assert code == 0 and result["result"] == "ok", result
    assert set(seen) == {"pre-comment", "post-comment"}
    for name, raw in seen.items():
        payload = json.loads(raw)
        validator(f"hooks/{name}.json").validate(payload)
        assert BODY_MARK not in raw.decode() and "hello" not in raw.decode()
        assert payload["project"] == "DEMO" and payload["plan"]["dedupe_key"] == "h1"
        assert payload["plan"]["current_status"]["category"] in ("new", "indeterminate", "done")
        assert payload["ticket"]["type"]
        assert_golden("hooks", name, "ok", cli.norm(payload))
    post = json.loads(seen["post-comment"])
    assert post["result"] == "ok" and post["changed"] is True


def test_a_dedupe_key_outside_the_grammar_is_exit_2(cli: Cli) -> None:
    code, env = cli.doc("--dry-run", "comment", "DEMO-2", "--body", "x", "--dedupe-key", "bad key!")
    assert code == 2 and env["error"]["reason"] == "invalid_argument"


# ---------------------------------------------------------------------------------------------
# stdin, provider failures, audit, account, boundaries (review round 1)
# ---------------------------------------------------------------------------------------------


@pytest.fixture
def pipe_stdin(monkeypatch: pytest.MonkeyPatch) -> Any:
    """``feed(data)`` makes the next read of ``sys.stdin`` come from a real pipe (not a TTY)."""
    opened: list[Any] = []

    def feed(data: bytes) -> None:
        read_fd, write_fd = os.pipe()
        os.write(write_fd, data)
        os.close(write_fd)
        stream = os.fdopen(read_fd, "r", encoding="utf-8")
        opened.append(stream)
        monkeypatch.setattr(sys, "stdin", stream)

    yield feed
    for stream in opened:
        stream.close()


def test_the_body_from_stdin_through_a_pipe_is_posted_with_yes(cli: Cli, pipe_stdin: Any) -> None:
    pipe_stdin(f"from a pipe {BODY_MARK}".encode())
    code, plan = cli.doc("--dry-run", "comment", "DEMO-2", "--body-file", "-", "--dedupe-key", "p1")
    assert code == 0, plan
    pipe_stdin(f"from a pipe {BODY_MARK}".encode())
    code, result = cli.doc(
        "--yes", "--expect-plan", plan["plan_sha256"], "comment", "DEMO-2",
        "--body-file", "-", "--dedupe-key", "p1",
    )  # fmt: skip
    assert code == 0 and result["result"] == "ok", result
    assert BODY_MARK in json.dumps(cli.fake.tickets["DEMO-2"].comments[-1]["body"])


def test_stdin_body_without_yes_and_no_tty_is_exit_13(cli: Cli, pipe_stdin: Any) -> None:
    pipe_stdin(b"from a pipe")
    code, env = cli.doc("comment", "DEMO-2", "--body-file", "-")
    assert (code, env["error"]["code"]) == (13, "confirmation_required")
    assert cli.writes() == []


@pytest.mark.parametrize(
    ("fault", "code_name", "exit_code"),
    [
        (Fault("status", status=404, method="POST", path="/comment"), "not_found", 10),
        (
            Fault(
                "status", status=429, method="POST", path="/comment", times=None, retry_after=3600
            ),
            "rate_limited",
            12,
        ),
        (Fault("status", status=500, method="POST", path="/comment"), "outcome_unknown", 12),
    ],
    ids=["404", "429", "500"],
)
def test_post_failures_map_to_their_codes(
    cli: Cli, fault: Fault, code_name: str, exit_code: int
) -> None:
    cli.fake.inject(fault)
    code, env = cli.post("--body", "x", "--dedupe-key", "f1")
    assert (code, env["error"]["code"]) == (exit_code, code_name)
    validator("cli/error.json").validate(env)


def _audit(cli: Cli) -> list[dict[str, Any]]:
    path = cli.root / "state" / "ticketctl" / "audit.jsonl"
    return [json.loads(line) for line in path.read_text().splitlines()]


def test_the_audit_has_an_attempted_and_an_ok_record(cli: Cli) -> None:
    code, result = cli.post("--body", "x", "--dedupe-key", "a1")
    assert code == 0
    records = _audit(cli)
    assert [r["result"] for r in records] == ["attempted", "ok"]
    assert {r["action"] for r in records} == {"comment"}
    assert records[0]["step_kind"] == "add_comment"
    assert {r["attempt_id"] for r in records} == {ATTEMPT}
    for r in records:
        validator("audit.json").validate(r)
    assert BODY_MARK not in json.dumps(records)


def test_a_failed_post_leaves_attempted_and_a_failure_record(cli: Cli) -> None:
    cli.fake.inject(Fault("status", status=400, method="POST", path="/comment"))
    code, _ = cli.post("--body", "x", "--dedupe-key", "a2")
    assert code == 12
    records = _audit(cli)
    assert records[0]["result"] == "attempted" and len(records) == 2
    assert records[1]["result"] != "ok"


def test_an_account_mismatch_refuses_before_any_write(cli: Cli) -> None:
    cli.config.write_text(cli.config.read_text() + "me:\n  jira: acct-9999\n")
    code, env = cli.doc("--dry-run", "comment", "DEMO-2", "--body", "x")
    assert (code, env["error"]["reason"]) == (2, "account_mismatch")
    assert cli.writes() == []


def test_dry_run_ignores_expect_plan(cli: Cli) -> None:
    code, plan = cli.doc("--dry-run", "--expect-plan", "0" * 64, "comment", "DEMO-2", "--body", "x")
    assert code == 0 and plan["steps"][0]["kind"] == "add_comment"
    assert cli.writes() == []


def test_an_unknown_status_category_gives_no_closed_warning(
    cli: Cli, monkeypatch: pytest.MonkeyPatch
) -> None:
    def fields(self: Any, ctx: Any) -> dict[str, Any]:
        return {"status": {"name": "Odd", "statusCategory": {"key": "other"}}}

    monkeypatch.setattr(commentcmd.CommentAction, "_read_ticket", fields)
    code, plan = cli.doc("--dry-run", "comment", "DEMO-2", "--body", "x")
    assert code == 0 and "closed_ticket" not in plan["warnings"]


def test_the_real_seams_give_a_random_attempt_id(cli: Cli, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(commentcmd, "make_seams", REAL_MAKE_SEAMS)
    code, result = cli.post("--body", "x", "--dedupe-key", "r2")
    assert code == 0, result
    assert re.fullmatch(r"[0-9a-f]{16}", result["attempt_id"]) and result["attempt_id"] != ATTEMPT


def _plan_warnings(cli: Cli, chars: int) -> list[str]:
    code, plan = cli.doc("--dry-run", "comment", "DEMO-2", "--body", "a" * chars)
    assert code == 0, plan
    return plan["warnings"]


ATTRIBUTION_LEN = len(attribution.attribution_text(config.Attribution().template))


def test_body_near_limit_is_absent_at_25000_and_present_at_27500(cli: Cli) -> None:
    assert "body_near_limit" not in _plan_warnings(cli, 25000)
    assert "body_near_limit" in _plan_warnings(cli, 27500)


def test_body_near_limit_counts_the_attribution_at_exactly_90_percent(cli: Cli) -> None:
    at_limit = 27000 - ATTRIBUTION_LEN  # text plus attribution is exactly 90% of 30000
    assert "body_near_limit" not in _plan_warnings(cli, at_limit)
    assert "body_near_limit" in _plan_warnings(cli, at_limit + 1)


def test_the_size_limit_counts_the_attribution(cli: Cli) -> None:
    fits = 30000 - ATTRIBUTION_LEN
    code, plan = cli.doc("--dry-run", "comment", "DEMO-2", "--body", "a" * fits)
    assert code == 0, plan
    code, env = cli.doc("--dry-run", "comment", "DEMO-2", "--body", "a" * (fits + 1))
    assert (code, env["error"]["reason"]) == (22, "body_too_large")


def test_the_engine_refuses_yes_without_a_key_for_the_comment_action(
    cli: Cli,
) -> None:
    g = Globals(config=str(cli.config), resolved_actor="human:t", yes=True, expect_plan="0" * 64)
    with pytest.raises(TicketctlError) as info:
        execute(load_runtime(g), "main", commentcmd.CommentAction("DEMO-2", "x"), Seams())
    assert (info.value.exit, info.value.reason) == (2, "dedupe_key_required")
    assert cli.fake.calls == []


def test_a_secret_as_the_dedupe_key_is_refused_and_never_printed(cli: Cli) -> None:
    code, out, err = cli.run("--json", "--dry-run", "comment", "DEMO-2", "--body", "x",
                             "--dedupe-key", SITE_TOKEN)  # fmt: skip
    assert code == 22 and json.loads(out)["error"]["reason"] == "secret_in_body"
    assert SITE_TOKEN not in out + err
    assert cli.fake.calls == []


def test_the_action_checks_the_dedupe_key_grammar_before_any_request(cli: Cli) -> None:
    g = Globals(config=str(cli.config), resolved_actor="human:t", dry_run=True)
    with pytest.raises(TicketctlError) as info:
        execute(
            load_runtime(g), "main", commentcmd.CommentAction("DEMO-2", "x", dedupe_key="bad key!"),
            Seams(),
        )  # fmt: skip
    assert (info.value.exit, info.value.reason) == (2, "invalid_argument")
    assert cli.fake.calls == []


def test_a_provider_that_sends_odd_shapes_gives_no_facts_and_no_crash(
    cli: Cli, monkeypatch: pytest.MonkeyPatch
) -> None:
    def fields(self: Any, ctx: Any) -> dict[str, Any]:
        return {"project": "DEMO", "issuetype": 5, "assignee": ["x"], "priority": "High"}

    monkeypatch.setattr(commentcmd.CommentAction, "_read_ticket", fields)
    code, plan = cli.doc("--dry-run", "comment", "DEMO-2", "--body", "x")
    assert code == 0 and plan["steps"][0]["kind"] == "add_comment"


def test_hook_facts_of_odd_shapes_leave_no_fact(cli: Cli) -> None:
    action = commentcmd.CommentAction("DEMO-2", "x")
    odd: dict[str, Any] = {"project": 1, "issuetype": "t", "assignee": [], "priority": 3}
    ctx: Any = SimpleNamespace(me=SimpleNamespace(account_id="a"))
    facts = action._hook_facts(ctx, odd, {}, None)
    assert "project" not in facts
    assert facts["ticket"]["type"] is None and facts["ticket"]["priority"] is None
    assert facts["ticket"]["assignee_account_id"] is None


def test_a_body_file_over_four_times_the_limit_is_invalid_file(cli: Cli) -> None:
    path = cli.root / "big.md"
    path.write_text("a" * (4 * 30000 + 1))
    code, env = cli.doc("--dry-run", "comment", "DEMO-2", "--body-file", str(path))
    assert (code, env["error"]["reason"]) == (22, "invalid_file")
    assert cli.fake.calls == []
