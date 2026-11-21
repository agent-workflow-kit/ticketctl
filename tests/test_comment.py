"""The ``comment`` command end to end against the fake provider (issue #29)."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

import pytest

from tests.fake_provider import SITE_EMAIL, SITE_TOKEN, FakeProvider, Fault
from tests.golden import assert_golden, validator
from ticketctl import attribution, commentcmd
from ticketctl.cli import main
from ticketctl.writeflow import Seams

TOKEN_VAR = "TICKETCTL_TEST_TOKEN"
ATTEMPT = "0123456789abcdef"
SITE_URL = "https://tracker.example.test"


class Cli:
    def __init__(self, root: Path, fake: FakeProvider, capsys: Any) -> None:
        self.fake = fake
        self.capsys = capsys
        self.root = root
        self.config = root / "config.yaml"
        self.config.write_text(
            f"sites:\n  main:\n    url: {fake.url}\n    email: {SITE_EMAIL}\n"
            f"    token_env: {TOKEN_VAR}\n    allow_http: true\n"
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
    monkeypatch.setenv(TOKEN_VAR, SITE_TOKEN)
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
    assert code == 22
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


def test_outcome_unknown_with_a_dedupe_key_is_retryable(cli: Cli) -> None:
    cli.fake.inject(Fault("reset", method="POST", path="/comment", after_apply=True))
    _, plan = cli.doc("--dry-run", "comment", "DEMO-2", "--body", "x", "--dedupe-key", "u1")
    code, env = cli.doc(
        "--yes", "--expect-plan", plan["plan_sha256"], "comment", "DEMO-2", "--body", "x",
        "--dedupe-key", "u1",
    )  # fmt: skip
    assert (code, env["error"]["code"], env["error"]["outcome_unknown"]) == (
        12, "outcome_unknown", True,
    )  # fmt: skip
    assert env["error"]["retryable"] is True  # with a dedupe key a re-run is safe (#33)
    assert env["error"]["details"]["attempt_id"] == ATTEMPT


def test_a_provider_refusal_is_rejected(cli: Cli) -> None:
    cli.fake.inject(Fault("status", status=400, method="POST", path="/comment"))
    code, env = cli.post("--body", "x", "--dedupe-key", "r1")
    assert (code, env["error"]["code"]) == (12, "rejected")
    validator("cli/error.json").validate(env)


def test_the_dedupe_seam_makes_the_comment_already_posted(
    cli: Cli, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(commentcmd.CommentAction, "find_existing", lambda self, ctx, rich: "777")
    code, result = cli.post("--body", "x", "--dedupe-key", "d1")
    assert code == 0 and result["result"] == "unchanged"
    assert result["already_posted"] is True and result["comment_id"] == "777"
    assert result["attempt_id"] is None and cli.writes() == []
    assert_golden("cli", "comment", "already-posted", cli.norm(result))


def test_the_seam_is_not_asked_without_a_dedupe_key(
    cli: Cli, monkeypatch: pytest.MonkeyPatch
) -> None:
    def boom(self: Any, ctx: Any, rich: Any) -> str:
        raise AssertionError("looked up without a key")

    monkeypatch.setattr(commentcmd.CommentAction, "find_existing", boom)
    code, plan = cli.doc("--dry-run", "comment", "DEMO-2", "--body", "x")
    assert code == 0


def test_the_hook_payload_is_built_by_the_real_runner(cli: Cli) -> None:
    from ticketctl import hooks, identity
    from ticketctl.flags import Globals
    from ticketctl.plan import build_plan
    from ticketctl.runtime import for_site, load_runtime, open_gateway
    from ticketctl.writeflow import Context, PrepareContext, WriteGateway

    rt = load_runtime(Globals(config=str(cli.config), resolved_actor="human:t"))
    action = commentcmd.CommentAction("DEMO-2", "hello", dedupe_key="h1")
    pc = PrepareContext(rt, "main", "human:t")
    action.prepare(pc)
    with for_site("main"):
        gw = open_gateway(rt, "main")
        me = identity.load_me(rt, "main", gw, refresh=True)
        ctx = Context(
            rt,
            "main",
            WriteGateway(gw, body_chars=pc.body_chars),
            me,
            "human:t",
            "flag",
            ATTEMPT,
            "comment",
        )
        planned = action.reduce(ctx, fresh=False)
    plan = build_plan("comment", "main", "human:t", planned)
    for name in ("pre-comment", "post-comment"):
        info = None
        if name.startswith("post"):
            from ticketctl.writeflow import PostInfo

            info = PostInfo("ok", None, [{"kind": "add_comment", "status": "done"}], ["1"], True)
        payload = hooks.build_payload(name, plan, planned, ATTEMPT, [], info)
        assert payload["project"] == "DEMO" and payload["plan"]["dedupe_key"] == "h1"
        assert payload["plan"]["current_status"]["category"] in ("new", "indeterminate", "done")
        assert payload["ticket"]["type"]
        assert "hello" not in json.dumps(payload)


def test_a_dedupe_key_outside_the_grammar_is_exit_2(cli: Cli) -> None:
    code, env = cli.doc("--dry-run", "comment", "DEMO-2", "--body", "x", "--dedupe-key", "bad key!")
    assert code == 2 and env["error"]["reason"] == "invalid_argument"
