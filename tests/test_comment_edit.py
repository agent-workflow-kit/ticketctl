"""``comment-edit`` and ``comment-delete`` end to end against the fake provider (issue #43)."""

from __future__ import annotations

import json
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from tests.fake_provider import (
    ME_ACCOUNT,
    OTHER_ACCOUNT,
    SITE_TOKEN,
    FakeProvider,
    Fault,
)
from tests.golden import assert_golden, validator
from tests.test_comment import ATTEMPT, SITE_ENV_VAR, Cli
from ticketctl import attribution, commentcmd, commentedit, config, hooks
from ticketctl.adf_validate import body_sha256
from ticketctl.errors import TicketctlError
from ticketctl.gateway import HookRun
from ticketctl.sanitize import InputError
from ticketctl.writeflow import Seams

KEY = "DEMO-2"
MARK = "zq7x-edit-marker-4f9c2e"  # must never reach a hook payload


@pytest.fixture
def cli(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: Any, fake: FakeProvider) -> Cli:
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    monkeypatch.setenv(SITE_ENV_VAR, SITE_TOKEN)
    monkeypatch.setenv("PATH", "/nonexistent")
    seams = lambda: Seams(attempt_id=lambda: ATTEMPT)  # noqa: E731
    monkeypatch.setattr(commentcmd, "make_seams", seams)
    monkeypatch.setattr(commentedit, "make_seams", seams)
    return Cli(tmp_path, fake, capsys)


def comments(cli: Cli) -> list[dict[str, Any]]:
    return cli.fake.tickets[KEY].comments


def posted(cli: Cli, body: str = "first draft", *extra: str) -> str:
    """Post a comment through ticketctl and return its id."""
    code, result = cli.post("--body", body, "--dedupe-key", "k-edit", *extra, key=KEY)
    assert code == 0, result
    return str(result["comment_id"])


def seed(
    cli: Cli, *, author: str, props: dict[str, Any], extra: dict[str, Any] | None = None
) -> str:
    fake: FakeProvider = cli.fake
    cid = str(fake._next_comment)
    fake._next_comment += 1
    fake.now += timedelta(seconds=1)
    comment = {
        "id": cid,
        "self": f"{fake.url}/rest/api/3/issue/1/comment/{cid}",
        "author": fake._user(author),
        "updateAuthor": fake._user(author),
        "body": {
            "type": "doc",
            "version": 1,
            "content": [{"type": "paragraph", "content": [{"type": "text", "text": "seeded"}]}],
        },
        "_created": fake.now,
        "_updated": fake.now,
        "properties": props,
        "jsdPublic": True,
        **(extra or {}),
    }
    fake.tickets[KEY].comments.append(comment)
    return cid


def valid_meta() -> dict[str, Any]:
    return {"v": 1, "actor_kind": "human", "body_sha256": "a" * 64}


def change(cli: Cli, command: str, cid: str, *argv: str) -> tuple[int, dict[str, Any]]:
    """Plan with --dry-run, then apply that plan with --yes."""
    code, plan = cli.doc("--dry-run", command, KEY, cid, *argv)
    if code != 0:
        return code, plan
    return cli.doc("--yes", "--expect-plan", plan["plan_sha256"], command, KEY, cid, *argv)


# --- edit ---------------------------------------------------------------------------------------


def test_edit_replaces_the_body_and_rewrites_the_property_together(cli: Cli) -> None:
    cid = posted(cli)
    before = len(cli.writes())
    code, result = change(cli, "comment-edit", cid, "--body", "second **draft**")
    assert code == 0, result
    assert result["action"] == "comment_edit" and result["result"] == "ok"
    assert result["changed"] is True and result["comment_id"] == cid
    assert result["steps"] == [{"kind": "edit_comment", "status": "done"}]
    assert result["already_posted"] is False and result["attempt_id"] == ATTEMPT
    validator("cli/comment-edit.json").validate(result)
    assert_golden("cli", "comment-edit", "ok", cli.norm(result))
    stored = comments(cli)[-1]
    assert stored["id"] == cid and len(comments(cli)) == 1
    meta = stored["properties"][attribution.META_PROPERTY]
    assert meta["body_sha256"] == body_sha256(stored["body"])  # the property stays consistent
    assert meta["v"] == 1 and meta["actor_kind"] == "auto"
    writes = cli.writes()[before:]
    assert [(c.method, c.path.rsplit("/", 1)[-1]) for c in writes] == [("PUT", cid)]
    assert writes[0].body["properties"][0]["key"] == attribution.META_PROPERTY


def test_edit_keeps_the_dedupe_key_hash_of_the_original_post(cli: Cli) -> None:
    cid = posted(cli)
    old = comments(cli)[-1]["properties"][attribution.META_PROPERTY]["key_sha256"]
    code, result = change(cli, "comment-edit", cid, "--body", "edited")
    assert code == 0, result
    assert comments(cli)[-1]["properties"][attribution.META_PROPERTY]["key_sha256"] == old
    # the keyed post now sees a comment with its key and another body: nothing is posted twice
    code, env = cli.doc(
        "--dry-run", "comment", KEY, "--body", "first draft", "--dedupe-key", "k-edit"
    )
    assert (code, env["error"]["code"]) == (20, "already_exists")


def test_edit_dry_run_is_a_plan_with_the_change_and_sends_nothing(cli: Cli) -> None:
    cid = posted(cli, f"old text {MARK}")
    before = len(cli.writes())
    code, plan = cli.doc("--dry-run", "comment-edit", KEY, cid, "--body", "new text")
    assert code == 0 and plan["action"] == "comment_edit" and plan["key"] == KEY
    assert plan["steps"][0]["kind"] == "edit_comment" and plan["steps"][0]["comment_id"] == cid
    (entry,) = plan["changes"]
    assert entry["name"] == "comment" and MARK in entry["from"] and "new text" in entry["to"]
    assert plan["dedupe_key"] is None and plan["dedupe_match"] is None
    validator("cli/comment-edit.json").validate(plan)
    assert_golden("cli", "comment-edit", "dry-run", cli.norm(plan))
    assert len(cli.writes()) == before


def test_editing_to_the_same_body_is_unchanged_and_writes_nothing(cli: Cli) -> None:
    cid = posted(cli, "same text")
    before = len(cli.writes())
    code, result = cli.doc(
        "--yes", "--expect-plan", "0" * 64, "comment-edit", KEY, cid, "--body", "same text"
    )
    assert code == 0 and result["result"] == "unchanged" and result["changed"] is False
    assert result["comment_id"] == cid and result["steps"] == []
    validator("cli/comment-edit.json").validate(result)
    assert_golden("cli", "comment-edit", "unchanged", cli.norm(result))
    assert len(cli.writes()) == before


def test_edit_needs_confirmation_without_yes(cli: Cli) -> None:
    cid = posted(cli)
    code, env = cli.doc("comment-edit", KEY, cid, "--body", "x")
    assert (code, env["error"]["code"]) == (13, "confirmation_required")
    assert env["error"]["details"]["plan"]["steps"][0]["kind"] == "edit_comment"


def test_edit_yes_needs_expect_plan_but_no_dedupe_key(cli: Cli) -> None:
    cid = posted(cli)
    code, env = cli.doc("--yes", "comment-edit", KEY, cid, "--body", "x")
    assert (code, env["error"]["reason"]) == (2, "yes_requires_expect_plan")
    code, plan = cli.doc("--dry-run", "comment-edit", KEY, cid, "--body", "x")
    code, result = cli.doc(
        "--yes", "--expect-plan", plan["plan_sha256"], "comment-edit", KEY, cid, "--body", "x"
    )
    assert code == 0, result


def test_a_comment_changed_since_the_plan_is_stale(cli: Cli) -> None:
    cid = posted(cli)
    _, plan = cli.doc("--dry-run", "comment-edit", KEY, cid, "--body", "mine")
    comments(cli)[-1]["body"]["content"][0]["content"][0]["text"] = "someone else edited this"
    before = len(cli.writes())
    code, env = cli.doc(
        "--yes", "--expect-plan", plan["plan_sha256"], "comment-edit", KEY, cid, "--body", "mine"
    )
    assert (code, env["error"]["code"]) == (20, "stale_ticket")
    assert len(cli.writes()) == before


@pytest.mark.parametrize(
    "author, props",
    [
        pytest.param(OTHER_ACCOUNT, None, id="foreign comment, no property"),
        pytest.param(OTHER_ACCOUNT, "valid", id="forged property on a foreign comment"),
        pytest.param(ME_ACCOUNT, {}, id="own comment, no property"),
        pytest.param(ME_ACCOUNT, {attribution.META_PROPERTY: {"v": 2}}, id="own, wrong version"),
        pytest.param(
            ME_ACCOUNT, {attribution.META_PROPERTY: "text"}, id="own, property not an object"
        ),
        pytest.param(
            ME_ACCOUNT,
            {attribution.META_PROPERTY: {"v": 1, "actor_kind": "robot", "body_sha256": "a" * 64}},
            id="own, bad actor kind",
        ),
        pytest.param(
            ME_ACCOUNT,
            {attribution.META_PROPERTY: {"v": 1, "actor_kind": "auto", "body_sha256": "xyz"}},
            id="own, bad hash",
        ),
        pytest.param(ME_ACCOUNT, {"ticketctl.META": valid_meta()}, id="own, key spelled otherwise"),
    ],
)
@pytest.mark.parametrize("command", ["comment-edit", "comment-delete"])
def test_a_comment_ticketctl_cannot_prove_is_its_own_is_refused(
    cli: Cli, command: str, author: str, props: Any
) -> None:
    props = {attribution.META_PROPERTY: valid_meta()} if props == "valid" else (props or {})
    cid = seed(cli, author=author, props=props)
    argv = ["--body", "x"] if command == "comment-edit" else []
    before = len(cli.writes())
    code, env = cli.doc("--dry-run", command, KEY, cid, *argv)
    assert (code, env["error"]["code"], env["error"]["reason"]) == (
        20,
        "not_ticketctl_comment",
        "not_ticketctl_comment",
    )
    validator("cli/error.json").validate(env)
    code, env = cli.doc("--yes", "--expect-plan", "0" * 64, command, KEY, cid, *argv)
    assert (code, env["error"]["code"]) == (20, "not_ticketctl_comment")
    assert len(cli.writes()) == before
    assert (
        len(comments(cli)) == 1
        and comments(cli)[0]["body"]["content"][0]["content"][0]["text"] == "seeded"
    )


def test_edit_refuses_a_restricted_comment_and_delete_does_not(cli: Cli) -> None:
    cid = posted(cli)
    comments(cli)[-1]["visibility"] = {"type": "role", "value": "Administrators"}
    code, env = cli.doc("--dry-run", "comment-edit", KEY, cid, "--body", "x")
    assert (code, env["error"]["code"], env["error"]["reason"]) == (
        20,
        "restricted_comment",
        "restricted_comment",
    )
    code, plan = cli.doc("--dry-run", "comment-delete", KEY, cid)
    assert code == 0 and plan["steps"][0]["kind"] == "delete_comment"


def test_a_missing_comment_is_not_found(cli: Cli) -> None:
    code, env = cli.doc("--dry-run", "comment-edit", KEY, "99999", "--body", "x")
    assert (code, env["error"]["code"]) == (10, "not_found")
    code, env = cli.doc("--dry-run", "comment-delete", KEY, "99999")
    assert (code, env["error"]["code"]) == (10, "not_found")


def test_a_missing_ticket_is_not_found(cli: Cli) -> None:
    code, env = cli.doc("--dry-run", "comment-edit", "DEMO-99999", "1", "--body", "x")
    assert (code, env["error"]["code"]) == (10, "not_found")


@pytest.mark.parametrize("bad", ["abc", "1.5", "-1", "", "1" * 19, "../1", "1/2"])
def test_a_comment_id_outside_the_numeric_grammar_is_exit_2(cli: Cli, bad: str) -> None:
    for command in ("comment-edit", "comment-delete"):
        argv = ["--body", "x"] if command == "comment-edit" else []
        code, env = cli.doc("--dry-run", command, KEY, bad, *argv)
        assert (code, env["error"]["code"]) == (2, "usage"), (command, bad)
    assert cli.fake.calls == [] or all(c.method == "GET" for c in cli.fake.calls)


@pytest.mark.parametrize(
    "argv", [[], ["--body", "a", "--body-file", "x.md"]], ids=["neither", "both"]
)
def test_edit_takes_exactly_one_body_source(cli: Cli, argv: list[str]) -> None:
    code, env = cli.doc("--dry-run", "comment-edit", KEY, "1", *argv)
    assert (code, env["error"]["reason"]) == (2, "invalid_combination")


def test_edit_format_adf_needs_a_body_file(cli: Cli) -> None:
    code, env = cli.doc("--dry-run", "comment-edit", KEY, "1", "--body", "{}", "--format", "adf")
    assert (code, env["error"]["reason"]) == (2, "invalid_combination")


def test_edit_from_an_adf_file(cli: Cli) -> None:
    cid = posted(cli)
    doc = {
        "type": "doc",
        "version": 1,
        "content": [{"type": "paragraph", "content": [{"type": "text", "text": "from adf"}]}],
    }
    path = cli.root / "body.json"
    path.write_text(json.dumps(doc))
    code, result = change(cli, "comment-edit", cid, "--body-file", str(path), "--format", "adf")
    assert code == 0, result
    texts = json.dumps(comments(cli)[-1]["body"])
    assert "from adf" in texts


def test_edit_refuses_an_empty_body_a_secret_and_a_long_body_before_any_write(cli: Cli) -> None:
    cid = posted(cli)
    before = len(cli.writes())
    code, env = cli.doc("--dry-run", "comment-edit", KEY, cid, "--body", "   ")
    assert (code, env["error"]["reason"]) == (22, "empty_body")
    code, env = cli.doc("--dry-run", "comment-edit", KEY, cid, "--body", f"has {SITE_TOKEN} in it")
    assert (code, env["error"]["reason"]) == (22, "secret_in_body")
    code, env = cli.doc("--dry-run", "comment-edit", KEY, cid, "--body", "x" * 40000)
    assert (code, env["error"]["reason"]) == (22, "body_too_large")
    assert SITE_TOKEN not in json.dumps(env)
    assert len(cli.writes()) == before


def test_an_auto_actor_edit_keeps_the_attribution_paragraph(cli: Cli) -> None:
    cid = posted(cli)
    code, result = change(cli, "comment-edit", cid, "--body", "second")
    assert code == 0, result
    text = attribution.attribution_text(config.Attribution().template)
    assert comments(cli)[-1]["body"]["content"][0]["content"][0]["text"] == text


def test_a_human_edit_records_the_human_actor_kind(cli: Cli) -> None:
    cid = posted(cli)
    code, plan = cli.doc(
        "--actor", "human:tester", "--dry-run", "comment-edit", KEY, cid, "--body", "h"
    )
    assert code == 0
    code, result = cli.doc(
        "--actor", "human:tester", "--yes", "--expect-plan", plan["plan_sha256"],
        "comment-edit", KEY, cid, "--body", "h",
    )  # fmt: skip
    assert code == 0, result
    meta = comments(cli)[-1]["properties"][attribution.META_PROPERTY]
    assert meta["actor_kind"] == "human" and meta["body_sha256"] == body_sha256(
        comments(cli)[-1]["body"]
    )


# --- delete -------------------------------------------------------------------------------------


def test_delete_removes_the_comment(cli: Cli) -> None:
    cid = posted(cli, f"to be removed {MARK}")
    code, plan = cli.doc("--dry-run", "comment-delete", KEY, cid)
    assert code == 0 and plan["action"] == "comment_delete"
    assert plan["steps"] == [{"kind": "delete_comment", "comment_id": cid}]
    (entry,) = plan["changes"]
    assert entry["name"] == "comment" and MARK in entry["from"] and entry["to"] is None
    validator("cli/comment-delete.json").validate(plan)
    assert_golden("cli", "comment-delete", "dry-run", cli.norm(plan))
    assert len(comments(cli)) == 1
    code, result = cli.doc(
        "--yes", "--expect-plan", plan["plan_sha256"], "comment-delete", KEY, cid
    )
    assert code == 0, result
    assert result["action"] == "comment_delete" and result["result"] == "ok"
    assert result["steps"] == [{"kind": "delete_comment", "status": "done"}]
    assert result["comment_id"] == cid and result["changed"] is True
    validator("cli/comment-delete.json").validate(result)
    assert_golden("cli", "comment-delete", "ok", cli.norm(result))
    assert comments(cli) == []
    last = cli.fake.calls[-1] if cli.fake.calls[-1].method != "GET" else None
    assert last is None or last.method == "DELETE"


def test_delete_needs_confirmation_and_never_runs_without_it(cli: Cli) -> None:
    cid = posted(cli)
    code, env = cli.doc("comment-delete", KEY, cid)
    assert (code, env["error"]["code"]) == (13, "confirmation_required")
    assert len(comments(cli)) == 1
    code, env = cli.doc("--yes", "comment-delete", KEY, cid)
    assert (code, env["error"]["reason"]) == (2, "yes_requires_expect_plan")
    assert len(comments(cli)) == 1


def test_delete_with_a_wrong_hash_is_stale_and_deletes_nothing(cli: Cli) -> None:
    cid = posted(cli)
    code, env = cli.doc("--yes", "--expect-plan", "0" * 64, "comment-delete", KEY, cid)
    assert (code, env["error"]["code"]) == (20, "stale_ticket")
    assert len(comments(cli)) == 1


def test_delete_of_a_comment_edited_since_the_plan_is_stale(cli: Cli) -> None:
    cid = posted(cli)
    _, plan = cli.doc("--dry-run", "comment-delete", KEY, cid)
    comments(cli)[-1]["body"]["content"][0]["content"][0]["text"] = "changed meanwhile"
    code, env = cli.doc("--yes", "--expect-plan", plan["plan_sha256"], "comment-delete", KEY, cid)
    assert (code, env["error"]["code"]) == (20, "stale_ticket")
    assert len(comments(cli)) == 1


def test_a_second_delete_is_not_found(cli: Cli) -> None:
    cid = posted(cli)
    code, _ = change(cli, "comment-delete", cid)
    assert code == 0
    code, env = cli.doc("--dry-run", "comment-delete", KEY, cid)
    assert (code, env["error"]["code"]) == (10, "not_found")


def test_delete_of_a_foreign_comment_sends_no_delete(cli: Cli) -> None:
    cid = seed(cli, author=OTHER_ACCOUNT, props={})
    code, _ = cli.doc("--yes", "--expect-plan", "0" * 64, "comment-delete", KEY, cid)
    assert code == 20
    assert not [c for c in cli.fake.calls if c.method == "DELETE"]


# --- failures, outcomes, audit, hooks ------------------------------------------------------------


@pytest.mark.parametrize(
    "method, command, argv",
    [("PUT", "comment-edit", ["--body", "x"]), ("DELETE", "comment-delete", [])],
)
def test_a_write_whose_answer_is_lost_is_outcome_unknown_and_a_rerun_is_safe(
    cli: Cli, method: str, command: str, argv: list[str]
) -> None:
    cid = posted(cli)
    cli.fake.inject(Fault("reset", method=method, path="/comment/", after_apply=True))
    code, env = change(cli, command, cid, *argv)
    assert (code, env["error"]["code"]) == (12, "outcome_unknown")
    assert env["error"]["retryable"] is True and env["error"]["outcome_unknown"] is True
    code, again = cli.doc("--dry-run", command, KEY, cid, *argv)
    if command == "comment-edit":  # the write landed: the plan has nothing left
        assert code == 0 and again["steps"] == []
    else:  # the comment is gone
        assert (code, again["error"]["code"]) == (10, "not_found")


@pytest.mark.parametrize("status", [400, 403, 404])
def test_a_provider_refusal_of_the_write_maps_to_its_code(cli: Cli, status: int) -> None:
    cid = posted(cli)
    cli.fake.inject(Fault("status", status=status, method="PUT", path="/comment/"))
    code, env = change(cli, "comment-edit", cid, "--body", "x")
    assert code in (10, 12), env
    validator("cli/error.json").validate(env)


def _audit(cli: Cli) -> list[dict[str, Any]]:
    path = cli.root / "state" / "ticketctl" / "audit.jsonl"
    return [json.loads(line) for line in path.read_text().splitlines()]


def test_the_audit_records_the_action_and_step_kind(cli: Cli) -> None:
    cid = posted(cli)
    change(cli, "comment-edit", cid, "--body", "second")
    change(cli, "comment-delete", cid)
    records = _audit(cli)[2:]  # after the post's two records
    assert [(r["action"], r["step_kind"], r["result"]) for r in records] == [
        ("comment_edit", "edit_comment", "attempted"),
        ("comment_edit", "edit_comment", "ok"),
        ("comment_delete", "delete_comment", "attempted"),
        ("comment_delete", "delete_comment", "ok"),
    ]
    for record in records:
        validator("audit.json").validate(record)
        assert record["provider_ids"] in ([], [cid])


def test_hooks_have_their_own_families_and_carry_no_text(
    cli: Cli, monkeypatch: pytest.MonkeyPatch
) -> None:
    cid = posted(cli)
    cli.config.write_text(
        cli.config.read_text()
        + "hooks:\n  pre-comment:\n    - cmd: other\n  post-comment:\n    - cmd: other\n"
        + "  pre-comment-edit:\n    - cmd: ignored\n  post-comment-edit:\n    - cmd: ignored\n"
        + "  pre-comment-delete:\n    - cmd: ignored\n  post-comment-delete:\n    - cmd: ignored\n"
    )
    seen: dict[str, bytes] = {}

    def fake_run(argv: list[str], *, stdin: bytes, env: dict[str, str], **kw: Any) -> HookRun:
        seen[env["TICKETCTL_HOOK"]] = stdin
        assert env["TICKETCTL_ID"] == KEY
        return HookRun(1, 0, b"", b"", False, False, False)

    monkeypatch.setattr(hooks, "run_hook", fake_run)
    code, result = change(cli, "comment-edit", cid, "--body", f"secret-looking {MARK}")
    assert code == 0, result
    assert set(seen) == {"pre-comment-edit", "post-comment-edit"}  # the comment hooks stay quiet
    code, result = change(cli, "comment-delete", cid)
    assert code == 0, result
    assert set(seen) == {
        "pre-comment-edit",
        "post-comment-edit",
        "pre-comment-delete",
        "post-comment-delete",
    }
    for name, raw in seen.items():
        payload = json.loads(raw)
        validator(f"hooks/{name}.json").validate(payload)
        assert MARK not in raw.decode() and "first draft" not in raw.decode()
        assert payload["plan"]["comment_id"] == cid and payload["object_id"] == KEY
        assert payload["project"] == "DEMO" and payload["ticket"]["type"]
        assert_golden("hooks", name, "ok", cli.norm(payload))
    assert json.loads(seen["post-comment-delete"])["provider_ids"] == [cid]


def test_a_pre_hook_veto_stops_the_edit(cli: Cli, monkeypatch: pytest.MonkeyPatch) -> None:
    cid = posted(cli)
    cli.config.write_text(
        cli.config.read_text() + "hooks:\n  pre-comment-delete:\n    - cmd: ignored\n"
    )
    monkeypatch.setattr(
        hooks, "run_hook", lambda *a, **k: HookRun(1, 1, b"", b"no", False, False, False)
    )
    code, env = change(cli, "comment-delete", cid)
    assert (code, env["error"]["code"]) == (11, "vetoed")
    assert len(comments(cli)) == 1


def test_a_hook_flag_names_the_new_families(cli: Cli) -> None:
    cid = posted(cli)
    code, env = cli.doc(
        "--hook", "pre-comment-delete=exit 1", "--dry-run", "comment-delete", KEY, cid
    )
    assert code == 0, env  # a dry run runs no hook; the flag name is accepted
    code, env = cli.doc(
        "--hook", "pre-comment-remove=exit 1", "--dry-run", "comment-delete", KEY, cid
    )
    assert code == 2


@pytest.mark.parametrize(
    "command, argv", [("comment-edit", ["--body", "x"]), ("comment-delete", [])]
)
def test_read_only_mode_refuses_both_before_any_request(
    cli: Cli, monkeypatch: pytest.MonkeyPatch, command: str, argv: list[str]
) -> None:
    cid = posted(cli)
    monkeypatch.setenv("TICKETCTL_READ_ONLY", "1")
    seen = len(cli.fake.calls)
    code, env = cli.doc("--yes", "--expect-plan", "0" * 64, command, KEY, cid, *argv)
    assert (code, env["error"]["code"]) == (20, "read_only")
    assert len(cli.fake.calls) == seen


def test_an_account_mismatch_refuses_before_any_write(cli: Cli) -> None:
    cid = posted(cli)
    cli.config.write_text(cli.config.read_text() + "me:\n  jira: acct-9999\n")
    code, env = cli.doc("--dry-run", "comment-delete", KEY, cid)
    assert (code, env["error"]["reason"]) == (2, "account_mismatch")


def test_a_provider_that_answers_another_comment_id_fails_closed(
    cli: Cli, monkeypatch: pytest.MonkeyPatch
) -> None:
    cid = posted(cli)
    original = FakeProvider._comment_obj

    def liar(self: FakeProvider, c: dict[str, Any], *, props: bool) -> dict[str, Any]:
        out = original(self, c, props=props)
        out["id"] = "77777"
        return out

    monkeypatch.setattr(FakeProvider, "_comment_obj", liar)
    code, env = cli.doc("--dry-run", "comment-delete", KEY, cid)
    assert (code, env["error"]["code"]) == (12, "provider_changed")


# --- review round 1 ------------------------------------------------------------------------------


def seed_human(cli: Cli) -> str:
    """An own comment that carries a valid property saying a human posted it."""
    return seed(cli, author=ME_ACCOUNT, props={attribution.META_PROPERTY: valid_meta()})


@pytest.mark.parametrize(
    "command, argv", [("comment-edit", ["--body", "x"]), ("comment-delete", [])]
)
def test_an_auto_actor_cannot_change_a_human_comment(
    cli: Cli, command: str, argv: list[str]
) -> None:
    cid = seed_human(cli)
    before = len(cli.writes())
    code, env = cli.doc("--dry-run", command, KEY, cid, *argv)
    assert (code, env["error"]["code"], env["error"]["reason"]) == (
        20,
        "human_comment",
        "human_comment",
    )
    validator("cli/error.json").validate(env)
    code, env = cli.doc("--yes", "--expect-plan", "0" * 64, command, KEY, cid, *argv)
    assert (code, env["error"]["code"]) == (20, "human_comment")
    assert len(cli.writes()) == before and len(comments(cli)) == 1


def test_a_human_actor_may_change_a_human_comment(cli: Cli) -> None:
    cid = seed_human(cli)
    code, plan = cli.doc("--actor", "human:tester", "--dry-run", "comment-delete", KEY, cid)
    assert code == 0, plan
    code, result = cli.doc(
        "--actor", "human:tester", "--yes", "--expect-plan", plan["plan_sha256"],
        "comment-delete", KEY, cid,
    )  # fmt: skip
    assert code == 0, result
    assert comments(cli) == []


def test_a_human_actor_may_delete_an_auto_comment(cli: Cli) -> None:
    cid = posted(cli)
    code, plan = cli.doc("--actor", "human:tester", "--dry-run", "comment-delete", KEY, cid)
    assert code == 0, plan
    code, result = cli.doc(
        "--actor", "human:tester", "--yes", "--expect-plan", plan["plan_sha256"],
        "comment-delete", KEY, cid,
    )  # fmt: skip
    assert code == 0, result
    assert comments(cli) == []


def test_edit_refuses_an_internal_comment_without_a_visibility_object(cli: Cli) -> None:
    cid = posted(cli)
    comments(cli)[-1]["jsdPublic"] = False
    code, env = cli.doc("--dry-run", "comment-edit", KEY, cid, "--body", "x")
    assert (code, env["error"]["code"], env["error"]["reason"]) == (
        20,
        "restricted_comment",
        "restricted_comment",
    )
    code, plan = cli.doc("--dry-run", "comment-delete", KEY, cid)
    assert code == 0 and plan["steps"][0]["kind"] == "delete_comment"


def test_a_keyed_comment_posts_again_after_the_comment_was_deleted(cli: Cli) -> None:
    cid = posted(cli)
    code, _ = change(cli, "comment-delete", cid)
    assert code == 0
    new_id = posted(cli)
    assert new_id != cid and len(comments(cli)) == 1


def test_the_edit_request_carries_exactly_the_body_and_one_property(cli: Cli) -> None:
    cid = posted(cli)
    before = len(cli.writes())
    code, result = change(cli, "comment-edit", cid, "--body", "second")
    assert code == 0, result
    (call,) = cli.writes()[before:]
    assert set(call.body) == {"body", "properties"}
    assert [p["key"] for p in call.body["properties"]] == [attribution.META_PROPERTY]


def _twin_comments(cli: Cli) -> tuple[str, str]:
    ids = []
    for n in (1, 2):
        code, result = cli.post("--body", "twin", "--dedupe-key", f"twin-{n}", key=KEY)
        assert code == 0, result
        ids.append(str(result["comment_id"]))
    return ids[0], ids[1]


def test_the_plan_hash_of_an_edit_binds_the_comment_id(cli: Cli) -> None:
    a, b = _twin_comments(cli)
    hashes = [
        cli.doc("--dry-run", "comment-edit", KEY, cid, "--body", "new")[1]["plan_sha256"]
        for cid in (a, b)
    ]
    assert hashes[0] != hashes[1]


def test_the_plan_hash_of_a_delete_binds_the_comment_id(cli: Cli) -> None:
    a, b = _twin_comments(cli)
    hashes = [cli.doc("--dry-run", "comment-delete", KEY, cid)[1]["plan_sha256"] for cid in (a, b)]
    assert hashes[0] != hashes[1]


class _Gw:
    def __init__(self, data: Any) -> None:
        self.data = data

    def request(self, method: str, path: str, **kw: Any) -> Any:
        return SimpleNamespace(status=200, data=self.data)


def _ctx(data: Any, actor: str = "auto:t") -> Any:
    return SimpleNamespace(gw=_Gw(data), me=SimpleNamespace(account_id="me"), actor=actor)


def _obj(**kw: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "id": "5",
        "own": True,
        "authored_by_ticketctl": True,
        "actor_kind": "auto",
        "body_sha256": "a" * 64,
        "body": "text",
        "dedupe_key_sha256": None,
    }
    base.update(kw)
    return base


_RAW = {"id": "5", "body": {"type": "doc", "version": 1, "content": []}}


def _read(monkeypatch: pytest.MonkeyPatch, obj: dict[str, Any]) -> Any:
    monkeypatch.setattr(commentedit, "comment_object", lambda raw, me, marks: obj)
    return commentedit.read_comment(_ctx(dict(_RAW)), "DEMO-1", "5")


def test_read_comment_accepts_a_fully_proven_own_comment(monkeypatch: pytest.MonkeyPatch) -> None:
    assert _read(monkeypatch, _obj()).obj["id"] == "5"


@pytest.mark.parametrize(
    "override",
    [
        {"own": False},
        {"authored_by_ticketctl": False},
        {"actor_kind": None},
        {"body_sha256": None},
    ],
    ids=["not own", "marker missing", "no actor kind", "no body hash"],
)
def test_read_comment_refuses_each_missing_proof(
    monkeypatch: pytest.MonkeyPatch, override: dict[str, Any]
) -> None:
    with pytest.raises(TicketctlError) as err:
        _read(monkeypatch, _obj(**override))
    assert err.value.code == "not_ticketctl_comment"


@pytest.mark.parametrize("bad", ["1/2", "..", "1?x", "1 2", "", "1\n2", "#1"])
def test_the_comment_id_segment_refuses_path_tricks(bad: str) -> None:
    with pytest.raises(InputError):
        commentedit._item_path("DEMO-1", bad)


@pytest.mark.parametrize("bad", ["DEMO-1/x", "DEMO-1?x", "DEMO 1", "..", ""])
def test_the_ticket_key_segment_refuses_path_tricks(bad: str) -> None:
    with pytest.raises(InputError):
        commentedit._item_path(bad, "5")
