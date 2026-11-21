"""The comment dedupe lookup end to end against the fake provider (issue #33)."""

from __future__ import annotations

import hashlib
import json
import multiprocessing
import re
import time
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest

from tests import dedupe_workers
from tests.fake_provider import ME_ACCOUNT, OTHER_ACCOUNT, FakeProvider, Fault
from tests.golden import assert_golden, validator
from tests.test_comment import ATTEMPT, SITE_ENV_VAR, Cli  # noqa: F401  (fixture parts)
from tests.test_comment import cli as cli  # noqa: F401  (the fixture)
from ticketctl import attribution, dedupe

SPAWN = multiprocessing.get_context("spawn")
KEY = "DEMO-2"


def key_hash(dedupe_key: str) -> str:
    return hashlib.sha256(dedupe_key.encode()).hexdigest()


def comments(cli: Cli) -> list[dict[str, Any]]:
    return cli.fake.tickets[KEY].comments


def seed(
    cli: Cli,
    *,
    author: str = OTHER_ACCOUNT,
    props: dict[str, Any] | None = None,
    text: str = "note",
) -> str:
    """Append a comment as ``author`` (the fake's own add path, but with any author)."""
    fake: FakeProvider = cli.fake
    cid = str(fake._next_comment)
    fake._next_comment += 1
    fake.now += timedelta(seconds=1)
    fake.tickets[KEY].comments.append(
        {
            "id": cid,
            "self": f"{fake.url}/rest/api/3/issue/1/comment/{cid}",
            "author": fake._user(author),
            "updateAuthor": fake._user(author),
            "body": {
                "type": "doc",
                "version": 1,
                "content": [{"type": "paragraph", "content": [{"type": "text", "text": text}]}],
            },
            "_created": fake.now,
            "_updated": fake.now,
            "properties": props or {},
            "jsdPublic": True,
        }
    )
    return cid


def meta_for(dedupe_key: str, body_sha: str = "a" * 64) -> dict[str, Any]:
    return {
        "v": 1,
        "actor_kind": "auto",
        "body_sha256": body_sha,
        "key_sha256": key_hash(dedupe_key),
    }


def gets(cli: Cli) -> list[Any]:
    return [c for c in cli.fake.calls if c.method == "GET" and c.path.endswith("/comment")]


def test_a_rerun_posts_nothing_and_reports_already_posted_with_the_id(cli: Cli) -> None:
    code, first = cli.post("--body", "hello", "--dedupe-key", "k1")
    assert code == 0 and first["result"] == "ok"
    before = len(cli.writes())
    code, again = cli.post("--body", "hello", "--dedupe-key", "k1")
    assert code == 0 and again["result"] == "unchanged"
    assert again["already_posted"] is True and again["comment_id"] == first["comment_id"]
    assert again["changed"] is False and again["attempt_id"] is None
    assert len(cli.writes()) == before and len(comments(cli)) == 1
    validator("cli/comment.json").validate(again)
    assert_golden("cli", "comment", "already-posted", cli.norm(again))


def test_a_dry_run_after_the_post_shows_the_match_and_no_step(cli: Cli) -> None:
    _, first = cli.post("--body", "hello", "--dedupe-key", "k2")
    code, plan = cli.doc("--dry-run", "comment", KEY, "--body", "hello", "--dedupe-key", "k2")
    assert code == 0 and plan["steps"] == []
    assert plan["dedupe_match"] == first["comment_id"]


def test_a_different_key_is_posted(cli: Cli) -> None:
    cli.post("--body", "hello", "--dedupe-key", "k3")
    code, result = cli.post("--body", "hello", "--dedupe-key", "other")
    assert code == 0 and result["result"] == "ok" and len(comments(cli)) == 2


def test_the_same_key_with_another_body_is_already_exists(cli: Cli) -> None:
    _, first = cli.post("--body", "one", "--dedupe-key", "k4")
    before = len(cli.writes())
    code, env = cli.doc("--dry-run", "comment", KEY, "--body", "two", "--dedupe-key", "k4")
    assert (code, env["error"]["code"]) == (20, "already_exists")
    assert env["error"]["details"]["existing_id"] == first["comment_id"]
    assert env["error"]["retryable"] is False
    validator("cli/error.json").validate(env)
    assert len(cli.writes()) == before


def test_the_fresh_check_under_the_lock_also_finds_a_comment_that_landed_after_the_plan(
    cli: Cli,
) -> None:
    argv = ("comment", KEY, "--body", "two", "--dedupe-key", "k4b")
    _, plan = cli.doc("--dry-run", *argv)  # nothing exists yet: a plan with one step
    assert plan["steps"]
    cli.post("--body", "one", "--dedupe-key", "k4b")  # another run lands the key meanwhile
    before = len(cli.writes())
    code, env = cli.doc("--yes", "--expect-plan", plan["plan_sha256"], *argv)
    assert (code, env["error"]["code"]) == (20, "already_exists")
    assert len(cli.writes()) == before


def test_a_rerun_after_outcome_unknown_finds_the_landed_comment(cli: Cli) -> None:
    cli.fake.inject(Fault("reset", method="POST", path="/comment", after_apply=True))
    argv = ("comment", KEY, "--body", "x", "--dedupe-key", "u1")
    _, plan = cli.doc("--dry-run", *argv)
    code, env = cli.doc("--yes", "--expect-plan", plan["plan_sha256"], *argv)
    assert (code, env["error"]["code"], env["error"]["outcome_unknown"]) == (
        12, "outcome_unknown", True,
    )  # fmt: skip
    assert env["error"]["retryable"] is True  # the lookup exists now
    assert "may post the comment again" not in env["error"]["message"]
    assert len(comments(cli)) == 1
    code, result = cli.post("--body", "x", "--dedupe-key", "u1")
    assert code == 0 and result["already_posted"] is True
    assert result["comment_id"] == comments(cli)[0]["id"] and len(comments(cli)) == 1


def test_a_comment_without_a_key_is_never_looked_up_or_deduplicated(cli: Cli) -> None:
    cli.post("--body", "same", "--dedupe-key", "k5")
    cli.fake.calls.clear()
    code, plan = cli.doc("--dry-run", "comment", KEY, "--body", "same")
    assert code == 0 and plan["steps"][0]["kind"] == "add_comment"
    assert plan["dedupe_match"] is None and gets(cli) == []


def test_the_key_is_stored_as_a_hash_only(cli: Cli) -> None:
    cli.post("--body", "hello", "--dedupe-key", "raw-key-value")
    meta = comments(cli)[0]["properties"][attribution.META_PROPERTY]
    assert meta["key_sha256"] == key_hash("raw-key-value")
    assert "raw-key-value" not in json.dumps(comments(cli)[0]["properties"])


def test_another_accounts_forged_property_is_ignored(cli: Cli) -> None:
    seed(cli, author=OTHER_ACCOUNT, props={attribution.META_PROPERTY: meta_for("f1")})
    code, result = cli.post("--body", "hello", "--dedupe-key", "f1")
    assert code == 0 and result["result"] == "ok" and result["already_posted"] is False
    assert len(comments(cli)) == 2


@pytest.mark.parametrize(
    "value",
    [
        "a string",
        ["list"],
        None,
        {"v": 2, "key_sha256": key_hash("m1")},
        {"v": True, "key_sha256": key_hash("m1")},
        {"v": 1.0, "key_sha256": key_hash("m1")},
        {"v": 1, "key_sha256": key_hash("m1").upper()},
        {"v": 1, "key_sha256": key_hash("m1")[:-1]},
        {"v": 1, "key_sha256": [key_hash("m1")]},
        {"v": 1},
        {"v": 1, "key_sha256": key_hash("m1") + "0"},
        {"v": 1, "key_sha256": "z" * 64},
    ],
    ids=lambda v: re.sub(r"\W+", "_", repr(v))[:30],
)
def test_a_malformed_property_counts_as_no_marker_and_never_raises(cli: Cli, value: Any) -> None:
    seed(cli, author=ME_ACCOUNT, props={attribution.META_PROPERTY: value})
    code, result = cli.post("--body", "hello", "--dedupe-key", "m1")
    assert code == 0 and result["result"] == "ok", result


def test_a_valid_key_hash_without_a_body_hash_is_a_marker_for_another_body(cli: Cli) -> None:
    value = {"v": 1, "key_sha256": key_hash("m2"), "pad": "x" * 20000}
    cid = seed(cli, author=ME_ACCOUNT, props={attribution.META_PROPERTY: value})
    code, env = cli.doc("--dry-run", "comment", KEY, "--body", "hello", "--dedupe-key", "m2")
    assert (code, env["error"]["code"]) == (20, "already_exists")
    assert env["error"]["details"]["existing_id"] == cid


def test_a_property_under_another_key_is_not_the_marker(cli: Cli) -> None:
    seed(cli, author=ME_ACCOUNT, props={"ticketctl.Meta": meta_for("p1"), "x": meta_for("p1")})
    code, result = cli.post("--body", "hello", "--dedupe-key", "p1")
    assert code == 0 and result["result"] == "ok"


def test_a_deleted_comment_is_posted_again(cli: Cli) -> None:
    cli.post("--body", "hello", "--dedupe-key", "d1")
    comments(cli).clear()
    code, result = cli.post("--body", "hello", "--dedupe-key", "d1")
    assert code == 0 and result["result"] == "ok" and len(comments(cli)) == 1


def test_an_edited_comment_still_counts_by_the_stored_body_hash(cli: Cli) -> None:
    _, first = cli.post("--body", "hello", "--dedupe-key", "e1")
    comments(cli)[0]["body"]["content"][0]["content"][0]["text"] = "edited by a human"
    code, result = cli.post("--body", "hello", "--dedupe-key", "e1")
    assert code == 0 and result["already_posted"] is True
    assert result["comment_id"] == first["comment_id"]


def test_a_marker_deep_in_the_thread_is_found_across_pages(cli: Cli) -> None:
    cli.post("--body", "hello", "--dedupe-key", "g1")
    for _ in range(250):
        seed(cli)
    cli.fake.calls.clear()
    code, result = cli.post("--body", "hello", "--dedupe-key", "g1")
    assert code == 0 and result["already_posted"] is True
    assert len(comments(cli)) == 251
    assert len(gets(cli)) >= 3


def test_an_own_marker_with_foreign_lookalikes_newer_picks_the_own_one(cli: Cli) -> None:
    _, first = cli.post("--body", "hello", "--dedupe-key", "l1")
    seed(cli, author=OTHER_ACCOUNT, props={attribution.META_PROPERTY: meta_for("l1")})
    code, result = cli.post("--body", "hello", "--dedupe-key", "l1")
    assert result["comment_id"] == first["comment_id"]


def test_more_than_1000_newer_comments_is_lookup_incomplete_and_not_retryable(cli: Cli) -> None:
    cli.post("--body", "hello", "--dedupe-key", "t1")
    for _ in range(1000):
        seed(cli)
    before = len(cli.writes())
    code, env = cli.doc("--dry-run", "comment", KEY, "--body", "hello", "--dedupe-key", "t1")
    assert (code, env["error"]["code"], env["error"]["retryable"]) == (
        12,
        "lookup_incomplete",
        False,
    )
    validator("cli/error.json").validate(env)
    assert len(cli.writes()) == before


def test_exactly_1000_comments_with_no_marker_is_not_truncated_and_posts(cli: Cli) -> None:
    for _ in range(1000):
        seed(cli)
    code, result = cli.post("--body", "hello", "--dedupe-key", "t2")
    assert code == 0 and result["result"] == "ok"


def test_an_unreadable_comment_list_cannot_prove_absence(cli: Cli) -> None:
    cli.fake.inject(Fault("status", status=500, method="GET", path="/comment"))
    code, env = cli.doc("--dry-run", "comment", KEY, "--body", "x", "--dedupe-key", "n1")
    assert (code, env["error"]["code"]) == (12, "unavailable")
    assert env["error"]["retryable"] is True and env["error"]["outcome_unknown"] is False
    assert cli.writes() == []


def test_a_dropped_comment_cannot_prove_absence(cli: Cli) -> None:
    seed(cli)
    comments(cli)[0]["id"] = "not an id!"  # the provider sent a comment that cannot be kept
    code, env = cli.doc("--dry-run", "comment", KEY, "--body", "x", "--dedupe-key", "n2")
    assert (code, env["error"]["code"]) == (12, "lookup_incomplete")


@pytest.mark.parametrize("garbled", ["omit", "string"])
def test_an_own_comment_whose_property_cannot_be_read_cannot_prove_absence(
    cli: Cli, garbled: str
) -> None:
    cid = seed(cli, author=ME_ACCOUNT)
    comment = next(c for c in comments(cli) if c["id"] == cid)
    if garbled == "omit":
        comment["properties"] = None  # the fake leaves the field out of the list answer
    else:
        comment["properties"] = "garbage"  # the fake lists a non-dict value as it is
    code, env = cli.doc("--dry-run", "comment", KEY, "--body", "x", "--dedupe-key", "n3")
    assert (code, env["error"]["code"]) == (12, "lookup_incomplete")
    assert cli.writes() == []


def test_a_concurrent_comment_between_pages_is_rescanned_once_and_posts(cli: Cli) -> None:
    for _ in range(150):
        seed(cli)
    fake: FakeProvider = cli.fake
    index = next(
        i
        for i, (m, p, _) in enumerate(fake._routes)
        if m == "GET" and p.pattern.endswith("/comment")
    )
    method, pattern, handler = fake._routes[index]
    state = {"first": True}

    def listing(call: Any, key: str) -> Any:
        out = handler(call, key)
        if state["first"]:  # a comment lands right after the first page was read
            state["first"] = False
            seed(cli)
        return out

    fake._routes[index] = (method, pattern, listing)
    code, result = cli.post("--body", "hello", "--dedupe-key", "r1")
    assert code == 0 and result["result"] == "ok" and result["already_posted"] is False
    assert len(comments(cli)) == 152


def test_a_hit_on_a_truncated_scan_is_already_posted(cli: Cli) -> None:
    for _ in range(600):
        seed(cli)
    _, first = cli.post("--body", "hello", "--dedupe-key", "c5")
    for _ in range(600):
        seed(cli)  # 1201 comments: the scan is truncated, the marker is among the newest 1000
    code, result = cli.post("--body", "hello", "--dedupe-key", "c5")
    assert code == 0 and result["already_posted"] is True
    assert result["comment_id"] == first["comment_id"]


def test_the_newest_other_body_hit_is_reported_as_existing(cli: Cli) -> None:
    seed(cli, author=ME_ACCOUNT, props={attribution.META_PROPERTY: meta_for("s1", "b" * 64)})
    other = {attribution.META_PROPERTY: meta_for("s1", "c" * 64)}
    newest = seed(cli, author=ME_ACCOUNT, props=other)
    code, env = cli.doc("--dry-run", "comment", KEY, "--body", "hello", "--dedupe-key", "s1")
    assert (code, env["error"]["code"]) == (20, "already_exists")
    assert env["error"]["details"]["existing_id"] == newest


def test_an_older_same_body_hit_beats_a_newer_other_body_hit(cli: Cli) -> None:
    _, first = cli.post("--body", "hello", "--dedupe-key", "s2")
    seed(cli, author=ME_ACCOUNT, props={attribution.META_PROPERTY: meta_for("s2", "d" * 64)})
    code, result = cli.post("--body", "hello", "--dedupe-key", "s2")
    assert code == 0 and result["already_posted"] is True
    assert result["comment_id"] == first["comment_id"]


def test_the_lookup_is_repeated_under_the_lock_and_adds_no_plan_facts(cli: Cli) -> None:
    code, plan = cli.doc("--dry-run", "comment", KEY, "--body", "x", "--dedupe-key", "h1")
    cli.fake.calls.clear()
    code, result = cli.doc(
        "--yes", "--expect-plan", plan["plan_sha256"], "comment", KEY, "--body", "x",
        "--dedupe-key", "h1",
    )  # fmt: skip
    assert code == 0 and result["result"] == "ok"
    assert len(gets(cli)) >= 2  # resolve (lock-free) and the fresh check under the lock
    again = cli.doc("--dry-run", "comment", KEY, "--body", "x", "--dedupe-key", "h2")[1]
    seed(cli)  # an unrelated comment never stales the plan
    later = cli.doc("--dry-run", "comment", KEY, "--body", "x", "--dedupe-key", "h2")[1]
    assert later["plan_sha256"] == again["plan_sha256"]


def test_the_marker_is_checked_for_the_fresh_account_not_the_cached_one(
    cli: Cli, tmp_path: Path
) -> None:
    assert cli.doc("whoami")[0] == 0  # fills the account cache
    path = tmp_path / "state" / "ticketctl" / "cache" / "main" / "me.json"
    cached = json.loads(path.read_text())
    assert cached["account_id"] == ME_ACCOUNT
    cached["account_id"] = OTHER_ACCOUNT  # the cache now names another account
    path.write_text(json.dumps(cached))
    mine = seed(cli, author=ME_ACCOUNT, props={attribution.META_PROPERTY: meta_for("a9")})
    seed(cli, author=OTHER_ACCOUNT, props={attribution.META_PROPERTY: meta_for("a9")})
    code, env = cli.doc("--dry-run", "comment", KEY, "--body", "x", "--dedupe-key", "a9")
    assert (code, env["error"]["code"]) == (20, "already_exists")
    assert env["error"]["details"]["existing_id"] == mine


def test_two_processes_with_one_key_post_once(
    cli: Cli, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    argv = ["--config", str(cli.config), "--json"]
    cmd = ["comment", KEY, "--body", "race", "--dedupe-key", "race1"]
    code, plan = cli.doc("--dry-run", *cmd)
    assert code == 0
    full = [*argv, "--yes", "--expect-plan", plan["plan_sha256"], *cmd]
    state = tmp_path / "state" / "ticketctl"
    name = dedupe.bucket_name("main", KEY, "race1")
    ready = [SPAWN.Event(), SPAWN.Event()]
    outs = [tmp_path / "out0", tmp_path / "out1"]
    procs = [
        SPAWN.Process(target=dedupe_workers.cli_worker, args=(full, str(o), r))
        for o, r in zip(outs, ready, strict=True)
    ]
    # the parent holds the bucket until both workers signalled they are about to run the command
    with dedupe.BucketLock(state, name, timeout=30.0):
        for p in procs:
            p.start()
        assert all(r.wait(60) for r in ready)
        time.sleep(1.0)  # a short grace for both to reach the lock
        assert not any(o.exists() for o in outs)
    for p in procs:
        p.join(60)
        assert p.exitcode == 0
    docs = [json.loads(o.read_text().split("\n", 1)[1]) for o in outs]
    codes = [int(o.read_text().split("\n", 1)[0]) for o in outs]
    assert codes == [0, 0]
    assert sorted(d["result"] for d in docs) == ["ok", "unchanged"]
    assert [d["already_posted"] for d in docs].count(True) == 1
    assert len(comments(cli)) == 1
    assert len(cli.writes()) == 1
