"""``comments``, ``transitions`` and ``sprints`` through the CLI against the fake provider (#18).

Everything is synthetic. Hostile provider answers replace one route of the fake with a handler that
returns the body a test wants (``override``).
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from tests.fake_provider import (
    ME_ACCOUNT,
    OTHER_ACCOUNT,
    SITE_EMAIL,
    SITE_TOKEN,
    STRANGER_ACCOUNT,
    FakeProvider,
    Out,
    adf_doc,
    adf_paragraph,
)
from tests.golden import assert_golden
from ticketctl import commentscmd, sanitize
from ticketctl.cli import main

TOKEN_VAR = "TICKETCTL_TEST_TOKEN"
META = "ticketctl.meta"
CUSTOM_ID = re.compile(r"customfield_[0-9]+", re.IGNORECASE)
NASTY = "a\u202eb\x07c\x1b[31m"
HASH_A = "a" * 64
HASH_B = "b" * 64
BASE = datetime(2026, 3, 1, 15, 0, tzinfo=UTC)


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

    def use(self, fake: FakeProvider, *extra: str) -> None:
        lines = [
            f"url: {fake.url}",
            f"email: {SITE_EMAIL}",
            f"token_env: {TOKEN_VAR}",
            "allow_http: true",
            *extra,
        ]
        text = "sites:\n  main:\n" + "".join(f"    {line}\n" for line in lines)
        self.config.write_text(text, encoding="utf-8")
        self.config.chmod(0o600)

    def run(self, *argv: str) -> tuple[int, str, str]:
        code = main(["--config", str(self.config), *argv])
        cap = self.capsys.readouterr()
        return code, cap.out, cap.err

    def json(self, *argv: str) -> tuple[int, Any, str]:
        code, out, err = self.run("--json", *argv)
        return code, json.loads(out) if out else None, err

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
def site(env: Env, fake: FakeProvider) -> FakeProvider:
    env.use(fake, "projects: [DEMO]")
    return fake


def override(fake: FakeProvider, method: str, suffix: str, handler: Callable[..., Out]) -> None:
    """Replace the route of ``method`` whose pattern ends with ``suffix``."""
    routes = [
        (m, p, handler if m == method and p.pattern.endswith(suffix) else h)
        for m, p, h in fake._routes
    ]
    assert any(h is handler for _, _, h in routes), suffix
    fake._routes[:] = routes


def reply(fake: FakeProvider, method: str, suffix: str, body: Any, status: int = 200) -> None:
    override(fake, method, suffix, lambda call, *groups: Out(status, body))


def add(
    fake: FakeProvider,
    key: str,
    cid: str,
    text: str,
    minutes: int,
    *,
    author: str = ME_ACCOUNT,
    props: dict[str, Any] | None = None,
    edited: bool = False,
) -> None:
    when = BASE + timedelta(minutes=minutes)
    fake.tickets[key].comments.append(
        {
            "id": cid,
            "author": fake._user(author),
            "body": adf_doc(adf_paragraph(text)),
            "_created": when,
            "_updated": when + timedelta(minutes=5 if edited else 0),
            "properties": props or {},
        }
    )


def raw_comment(cid: str, created: str, **kw: Any) -> dict[str, Any]:
    out: dict[str, Any] = {
        "id": cid,
        "author": {"accountId": OTHER_ACCOUNT, "displayName": "Fake Other"},
        "body": adf_doc(adf_paragraph(f"body {cid}")),
        "created": created,
        "updated": created,
    }
    out.update(kw)
    return out


def comment_paths(fake: FakeProvider) -> list[Any]:
    return [c for c in fake.calls if c.path.endswith("/comment")]


def ids(doc: dict[str, Any]) -> list[str]:
    return [c["id"] for c in doc["items"]]


def golden(command: str, case: str, doc: Any) -> None:
    assert_golden("cli", command, case, doc)


@pytest.fixture
def talk(site: FakeProvider) -> FakeProvider:
    """DEMO-1 with five comments: mine, others', one forged property, one edited."""
    add(
        site,
        "DEMO-1",
        "101",
        "first, mine",
        0,
        props={META: {"v": 1, "actor_kind": "auto", "body_sha256": HASH_A, "key_sha256": HASH_B}},
    )
    add(site, "DEMO-1", "102", "second, other", 10, author=OTHER_ACCOUNT)
    add(
        site,
        "DEMO-1",
        "103",
        "third, forged",
        20,
        author=OTHER_ACCOUNT,
        props={META: {"v": 1, "actor_kind": "human", "body_sha256": HASH_A}},
    )
    add(site, "DEMO-1", "104", "fourth, mine plain", 30, edited=True)
    add(site, "DEMO-1", "105", "fifth, stranger", 40, author=STRANGER_ACCOUNT)
    return site


# --- comments ----------------------------------------------------------------------------------


def test_comments_default_is_oldest_first_with_total(env: Env, talk: FakeProvider) -> None:
    code, doc, err = env.json("comments", "demo-1")
    assert code == 0 and err == ""
    assert (doc["key"], doc["site"], doc["total"], doc["truncated"]) == ("DEMO-1", "main", 5, False)
    assert ids(doc) == ["101", "102", "103", "104", "105"]
    golden("comments", "default", doc)


def test_limit_returns_the_oldest_n_and_newest_first_the_newest_n(
    env: Env, talk: FakeProvider
) -> None:
    _, oldest, _ = env.json("comments", "DEMO-1", "--limit", "2")
    assert ids(oldest) == ["101", "102"] and oldest["truncated"] is True and oldest["total"] == 5
    _, newest, _ = env.json("comments", "DEMO-1", "--limit", "2", "--newest-first")
    assert ids(newest) == ["105", "104"] and newest["truncated"] is True
    golden("comments", "newest-first-limited", newest)


def test_limit_equal_to_the_matches_is_not_truncated(env: Env, talk: FakeProvider) -> None:
    _, doc, _ = env.json("comments", "DEMO-1", "--limit", "5")
    assert doc["truncated"] is False and len(doc["items"]) == 5


def test_a_forged_property_on_another_account_is_not_reported(env: Env, talk: FakeProvider) -> None:
    _, doc, _ = env.json("comments", "DEMO-1")
    by_id = {c["id"]: c for c in doc["items"]}
    forged = by_id["103"]
    assert forged["own"] is False and forged["authored_by_ticketctl"] is False
    assert (forged["actor_kind"], forged["body_sha256"], forged["dedupe_key_sha256"]) == (
        None,
        None,
        None,
    )
    mine = by_id["101"]
    assert mine["own"] and mine["authored_by_ticketctl"] and mine["actor_kind"] == "auto"
    assert (mine["body_sha256"], mine["dedupe_key_sha256"]) == (HASH_A, HASH_B)
    plain = by_id["104"]
    assert plain["own"] and plain["authored_by_ticketctl"] is False
    assert plain["updated"] != plain["created"]


def test_own_keeps_only_the_resolved_accounts_comments_and_total_counts_all(
    env: Env, talk: FakeProvider
) -> None:
    _, doc, _ = env.json("comments", "DEMO-1", "--own")
    assert ids(doc) == ["101", "104"] and doc["total"] == 5 and doc["truncated"] is False
    _, newest, _ = env.json("comments", "DEMO-1", "--own", "--newest-first", "--limit", "1")
    assert ids(newest) == ["104"] and newest["truncated"] is True
    me_calls = [c for c in talk.calls if c.path == "/rest/api/3/myself"]
    assert len(me_calls) == 1  # the second run used the cached account


def test_an_own_comment_with_no_property_list_fails_closed(env: Env, talk: FakeProvider) -> None:
    def page(call: Any, key: str) -> Out:
        c = raw_comment(
            "7",
            "2026-03-01T09:00:00.000-0600",
            author={"accountId": ME_ACCOUNT, "displayName": "Fake Me"},
        )
        return Out(200, {"startAt": 0, "maxResults": 100, "total": 1, "comments": [c]})

    override(talk, "GET", "/comment", page)
    code, doc, err = env.json("comments", "DEMO-1")
    assert code == 0
    (c,) = doc["items"]
    assert c["own"] and c["authored_by_ticketctl"] is True and c["actor_kind"] is None
    assert "warning" in err and "comments" in err


def test_since_filters_on_created_inclusively(env: Env, talk: FakeProvider) -> None:
    _, doc, _ = env.json("comments", "DEMO-1", "--since", "2026-03-01T15:20:00Z")
    assert ids(doc) == ["103", "104", "105"] and doc["total"] == 5
    _, offset, _ = env.json("comments", "DEMO-1", "--since", "2026-03-01T10:30:00-05:00")
    assert ids(offset) == ["104", "105"]
    _, day, _ = env.json("comments", "DEMO-1", "--since", "2026-03-02")
    assert ids(day) == [] and day["truncated"] is False and day["total"] == 5


def test_since_with_a_duration_counts_back_from_now(env: Env, talk: FakeProvider) -> None:
    _, doc, _ = env.json("comments", "DEMO-1", "--since", "1w")
    assert ids(doc) == []  # the comments are from 2026-03-01, long before any real now + 0
    now = datetime(2026, 3, 1, 16, 0, tzinfo=UTC)
    assert commentscmd.since_cutoff("90m", now) == "2026-03-01T14:30:00Z"
    assert commentscmd.since_cutoff("2d", now) == "2026-02-27T16:00:00Z"
    assert commentscmd.since_cutoff("1w", now) == "2026-02-22T16:00:00Z"
    assert commentscmd.since_cutoff("3h", now) == "2026-03-01T13:00:00Z"


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("2026-03-01", "2026-03-01T00:00:00Z"),
        ("2026-03-01T00:00:00Z", "2026-03-01T00:00:00Z"),
        ("2026-03-01T00:00:00.999Z", "2026-03-01T00:00:00Z"),
        ("2026-03-01T01:30:00+01:30", "2026-03-01T00:00:00Z"),
        ("2026-11-01T01:30:00-04:00", "2026-11-01T05:30:00Z"),  # a daylight-saving fold, by offset
        ("2026-11-01T01:30:00-05:00", "2026-11-01T06:30:00Z"),
    ],
)
def test_since_cutoff_grammar(value: str, expected: str) -> None:
    assert commentscmd.since_cutoff(value) == expected


@pytest.mark.parametrize(
    "value",
    [
        "", "yesterday", "2026-13-01", "2026-02-30", "2026-03-01T10:00:00",
        "2026-03-01 10:00:00Z", "5", "5x", "99999d", "--1d", "2026-03-01\n", "２０２６-03-01",
        "1" * 100, "2026-03-01T10:00:00+24:00",
    ],
)  # fmt: skip
def test_a_bad_since_is_refused_before_any_request(
    env: Env, talk: FakeProvider, value: str
) -> None:
    code, doc, _ = env.json("comments", "DEMO-1", f"--since={value}")
    assert (code, doc["error"]["reason"]) == (22, "invalid_value")
    assert talk.calls == []


def test_a_negative_duration_is_refused(env: Env, talk: FakeProvider) -> None:
    code, doc, _ = env.json("comments", "DEMO-1", "--since=-1d")
    assert (code, doc["error"]["reason"]) == (22, "invalid_value") and talk.calls == []


@pytest.mark.parametrize("limit", ["0", "201", "1000000"])
def test_a_bad_limit_is_exit_22_before_any_request(
    env: Env, talk: FakeProvider, limit: str
) -> None:
    code, doc, _ = env.json("comments", "DEMO-1", "--limit", limit)
    assert (code, doc["error"]["reason"]) == (22, "invalid_value")
    assert talk.calls == []


def test_a_non_numeric_limit_is_a_usage_error(env: Env, talk: FakeProvider) -> None:
    code, doc, _ = env.json("comments", "DEMO-1", "--limit", "x")
    assert (code, doc["error"]["reason"]) == (2, "invalid_argument")


def test_a_bad_key_is_exit_22_and_a_missing_ticket_exit_10(env: Env, talk: FakeProvider) -> None:
    assert env.json("comments", "not a key")[0] == 22
    code, doc, _ = env.json("comments", "DEMO-99")
    assert code == 10 and doc["error"]["code"] == "not_found"


def test_comments_follow_pages_of_a_long_thread(env: Env, site: FakeProvider) -> None:
    for n in range(250):
        add(site, "DEMO-1", str(1000 + n), f"c{n}", n)
    _, doc, _ = env.json("comments", "DEMO-1", "--limit", "200")
    assert len(doc["items"]) == 200 and doc["total"] == 250 and doc["truncated"] is True
    assert ids(doc)[0] == "1000" and ids(doc)[-1] == "1199"
    _, newest, _ = env.json("comments", "DEMO-1", "--limit", "5", "--newest-first")
    assert ids(newest) == ["1249", "1248", "1247", "1246", "1245"]
    _, since, _ = env.json(
        "comments", "DEMO-1", "--since", "2026-03-01T19:00:00Z", "--limit", "200"
    )
    assert len(since["items"]) == 250 - 240 and since["truncated"] is False


def test_the_scan_stops_at_1000_comments(env: Env, site: FakeProvider) -> None:
    for n in range(1100):
        add(site, "DEMO-1", str(2000 + n), "x", n)
    site.calls.clear()
    _, doc, _ = env.json("comments", "DEMO-1", "--since", "2030-01-01")
    assert doc["items"] == [] and doc["total"] == 1100 and doc["truncated"] is True
    assert len(comment_paths(site)) == 10


def test_a_provider_that_ignores_startat_cannot_loop(env: Env, talk: FakeProvider) -> None:
    def page(call: Any, key: str) -> Out:
        c = raw_comment("5", "2026-03-01T09:00:00.000-0600")
        return Out(200, {"startAt": 0, "total": 10**9, "comments": [c]})

    override(talk, "GET", "/comment", page)
    code, doc, err = env.json("comments", "DEMO-1")
    assert code == 0 and ids(doc) == ["5"] and doc["truncated"] is True
    assert len(comment_paths(talk)) == commentscmd.MAX_REQUESTS
    assert "comments" in err  # the repeated ids


def test_a_missing_total_is_unknown_and_truncated(env: Env, talk: FakeProvider) -> None:
    reply(talk, "GET", "/comment", {"comments": [raw_comment("9", "2026-03-01T09:00:00.000-0600")]})
    code, doc, err = env.json("comments", "DEMO-1")
    assert code == 0 and doc["total"] == 1 and doc["truncated"] is True
    assert "warning" in err


@pytest.mark.parametrize("bad", [None, [], "x", 5, {"comments": "no"}, {"total": 3}])
def test_a_page_that_is_not_a_comment_list(env: Env, talk: FakeProvider, bad: Any) -> None:
    reply(talk, "GET", "/comment", bad)
    code, doc, _ = env.json("comments", "DEMO-1")
    if code == 0:
        assert doc["items"] == [] and doc["truncated"] is True
    else:
        assert code == 12 and doc["error"]["code"] == "provider_changed"


def test_hostile_comments_are_dropped_or_made_safe(env: Env, talk: FakeProvider) -> None:
    good = raw_comment("1", "2026-03-01T09:00:00.000-0600")
    comments = [
        good,
        {**good, "id": "has space"},
        {**good, "id": "2", "created": "yesterday"},
        {**good, "id": "3", "updated": None},
        {**good, "id": "4", "author": {"accountId": "bad id!", "displayName": "x"}},
        {**good, "id": "5", "author": {**good["author"], "displayName": NASTY}, "body": NASTY},
        {**good, "id": "6", "body": {"type": "doc", "content": "no"}},
        None,
        "text",
        [],
        7,
        {**good, "id": "1"},
    ]  # fmt: skip
    reply(talk, "GET", "/comment", {"total": len(comments), "comments": comments})
    code, doc, err = env.json("comments", "DEMO-1")
    assert code == 0
    assert set(ids(doc)) == {"1", "4", "5", "6"}
    five = next(c for c in doc["items"] if c["id"] == "5")
    for text in (five["author"]["display_name"], five["body"]):
        assert not re.search(r"[\u202e\x07\x1b]", text)
    assert next(c for c in doc["items"] if c["id"] == "4")["author"]["account_id"] is None
    assert "warning" in err


def test_equal_created_orders_by_id(env: Env, talk: FakeProvider) -> None:
    same = "2026-03-01T09:00:00.000-0600"
    cs = [raw_comment(i, same) for i in ("10", "9", "100", "2")]
    reply(talk, "GET", "/comment", {"total": 4, "comments": cs})
    _, doc, _ = env.json("comments", "DEMO-1")
    assert ids(doc) == ["2", "9", "10", "100"]
    _, rev, _ = env.json("comments", "DEMO-1", "--newest-first")
    assert ids(rev) == ["100", "10", "9", "2"]


def test_a_huge_body_is_cut(env: Env, talk: FakeProvider) -> None:
    big = raw_comment("1", "2026-03-01T09:00:00.000-0600", body="x" * 1_100_000)
    reply(talk, "GET", "/comment", {"total": 1, "comments": [big]})
    _, doc, _ = env.json("comments", "DEMO-1")
    (c,) = doc["items"]
    assert (
        c["truncated"] is True and len(c["body"]) <= 1_000_000 and c["body"].endswith("<truncated>")
    )


def test_comments_request_shape(env: Env, talk: FakeProvider) -> None:
    env.json("comments", "demo-1", "--newest-first")
    (call,) = comment_paths(talk)
    assert call.path == "/rest/api/3/issue/DEMO-1/comment"
    assert call.one("orderBy") == "-created" and call.one("expand") == "properties"
    assert call.one("startAt") == "0" and call.one("maxResults") == "100"


def test_comments_by_browse_url(env: Env, talk: FakeProvider) -> None:
    code, doc, _ = env.json("comments", f"{talk.url}/browse/DEMO-1?focusedCommentId=1")
    assert code == 0 and doc["key"] == "DEMO-1"


def test_comments_human_output(env: Env, talk: FakeProvider) -> None:
    code, out, _ = env.run("comments", "DEMO-1", "--limit", "1")
    assert code == 0 and "101" in out and "first, mine" in out and "more" in out


# --- transitions -------------------------------------------------------------------------------


def test_transitions_lists_the_live_list_with_required_fields(env: Env, site: FakeProvider) -> None:
    code, doc, err = env.json("transitions", "demo-2")
    assert code == 0 and err == ""
    assert (doc["key"], doc["site"]) == ("DEMO-2", "main")
    by_id = {t["id"]: t for t in doc["transitions"]}
    assert list(by_id) == ["31", "41", "51"]
    finish = by_id["41"]
    assert finish["to"] == {"name": "Done", "category": "done"} and finish["has_screen"] is True
    (res,) = finish["required_fields"]
    assert res["name"] == "Resolution" and res["kind"] == "resolution"
    assert "Duplicate" in res["allowed_values"] and len(res["allowed_values"]) == 5
    assert by_id["31"]["required_fields"] == [] and by_id["31"]["has_screen"] is False
    assert not CUSTOM_ID.search(json.dumps(doc))
    golden("transitions", "default", doc)
    (call,) = [c for c in site.calls if c.path.endswith("/transitions")]
    assert call.one("expand") == "transitions.fields"


def test_a_subtask_transition_needs_its_parent(env: Env, site: FakeProvider) -> None:
    site.add_ticket("DEMO-9", "A sub-task", type_id="3")
    _, doc, _ = env.json("transitions", "DEMO-9")
    names = {t["id"]: [f["name"] for f in t["required_fields"]] for t in doc["transitions"]}
    assert names["31"] == ["Parent"] and names["41"] == ["Resolution", "Parent"]
    assert doc["transitions"][0]["required_fields"][0]["kind"] == "issuelink"


def test_transitions_are_read_per_ticket_every_time(env: Env, site: FakeProvider) -> None:
    env.json("transitions", "DEMO-1")
    env.json("transitions", "DEMO-1")
    assert len([c for c in site.calls if c.path.endswith("/transitions")]) == 2


def test_transitions_missing_ticket_and_bad_key(env: Env, site: FakeProvider) -> None:
    assert env.json("transitions", "DEMO-99")[0] == 10
    assert env.json("transitions", "bad key")[0] == 22


def test_hostile_transitions(env: Env, site: FakeProvider) -> None:
    to = {"name": "Done", "statusCategory": {"key": "done"}}
    ok = {"id": "1", "name": "Ok", "to": to, "hasScreen": False, "fields": {}}
    listed = [
        ok,
        {**ok, "id": "1"},  # repeated id
        {**ok, "id": "x y"},
        {**ok, "id": 2, "name": "Int id"},
        {**ok, "id": "3", "name": None},
        {**ok, "id": "4", "to": {"name": "Odd", "statusCategory": {"key": "bogus"}}},
        {**ok, "id": "5", "to": "Done"},
        {**ok, "id": "6", "hasScreen": "yes", "name": "a\u202eb\x07"},
        {**ok, "id": "7", "fields": None},
        {
            **ok,
            "id": "8",
            "name": "Fields",
            "fields": {
                "customfield_90001": {
                    "required": True,
                    "name": "customfield_90001",
                    "schema": {"type": "x" * 100},
                    "allowedValues": [{"name": "a"}, {"value": "b"}, "c", 5, None, {"id": 1}],
                },
                "customfield_90002": {"required": True, "schema": "no"},
                "customfield_90003": {"required": "true", "name": "Not required"},
                "customfield_90004": {"required": False, "name": "Optional"},
                "customfield_90005": "junk",
                "customfield_90006": {"required": True, "name": "Has customfield_1 inside"},
            },
        },
        None,
        "x",
    ]  # fmt: skip
    reply(site, "GET", "/transitions", {"transitions": listed})
    code, doc, err = env.json("transitions", "DEMO-1")
    assert code == 0 and "warning" in err
    by_id = {t["id"]: t for t in doc["transitions"]}
    assert list(by_id) == ["1", "2", "6", "7", "8"]
    assert by_id["6"]["has_screen"] is True and not re.search(r"[\u202e\x07]", by_id["6"]["name"])
    assert by_id["7"]["required_fields"] == []
    fields = by_id["8"]["required_fields"]
    assert [f["name"] for f in fields] == ["field", "unknown field", "Has field inside"]
    assert fields[0]["allowed_values"] == ["a", "b", "c"] and len(fields[0]["kind"]) <= 64
    assert fields[1]["kind"] == "unknown" and "allowed_values" not in fields[1]
    assert not CUSTOM_ID.search(json.dumps(doc))


@pytest.mark.parametrize("bad", [None, [], "x", {"transitions": "no"}, {}])
def test_a_transition_answer_that_is_not_a_list(env: Env, site: FakeProvider, bad: Any) -> None:
    reply(site, "GET", "/transitions", bad)
    code, doc, _ = env.json("transitions", "DEMO-1")
    assert code == 12 and doc["error"]["code"] == "provider_changed"


def test_a_huge_transition_list_is_capped(env: Env, site: FakeProvider) -> None:
    to = {"name": "Done", "statusCategory": {"key": "done"}}
    many = [
        {"id": str(n), "name": "t", "to": to, "hasScreen": False, "fields": {}}
        for n in range(1, 700)
    ]
    reply(site, "GET", "/transitions", {"transitions": many})
    _, doc, err = env.json("transitions", "DEMO-1")
    assert len(doc["transitions"]) == 500 and "warning" in err


def test_transitions_human_output(env: Env, site: FakeProvider) -> None:
    code, out, _ = env.run("transitions", "DEMO-1")
    assert code == 0 and "41 Finish -> Done [done]" in out and "requires Resolution" in out


# --- sprints -----------------------------------------------------------------------------------


def test_sprints_list_by_the_projects_board(env: Env, site: FakeProvider) -> None:
    code, doc, err = env.json("sprints", "list")
    assert code == 0 and err == ""
    assert [s["id"] for s in doc["sprints"]] == [1, 2, 3] and doc["site"] == "main"
    first, active, future = doc["sprints"]
    assert first["start"] == "2026-02-02T15:00:00Z" and first["end"] == "2026-02-16T15:00:00Z"
    assert (active["state"], active["board"]) == ("active", 1)
    assert future["start"] is None and future["end"] is None
    golden("sprints-list", "default", doc)
    boards = [c for c in site.calls if c.path == "/rest/agile/1.0/board"]
    assert boards[0].one("projectKeyOrId") == "DEMO" and boards[0].one("type") == "scrum"


def test_sprints_list_state_and_limit(env: Env, site: FakeProvider) -> None:
    _, doc, _ = env.json("sprints", "list", "--state", "active")
    assert [s["id"] for s in doc["sprints"]] == [2]
    _, one, _ = env.json("sprints", "list", "--limit", "1")
    assert [s["id"] for s in one["sprints"]] == [1]
    sprint_calls = [c for c in site.calls if c.path.endswith("/sprint")]
    assert sprint_calls[0].one("state") == "active"


@pytest.mark.parametrize("limit", ["0", "501"])
def test_sprints_bad_limit_is_exit_22(env: Env, site: FakeProvider, limit: str) -> None:
    code, doc, _ = env.json("sprints", "list", "--limit", limit)
    assert (code, doc["error"]["reason"]) == (22, "invalid_value") and site.calls == []


def test_sprints_board_by_id_name_and_site_config(env: Env, site: FakeProvider) -> None:
    code, doc, _ = env.json("sprints", "list", "--board", "1")
    assert code == 0 and len(doc["sprints"]) == 3
    assert not [
        c for c in site.calls if c.path == "/rest/agile/1.0/board"
    ]  # an id is not looked up
    site.calls.clear()
    code, doc, _ = env.json("sprints", "list", "--board", "dEmO BoArD")
    assert code == 0 and len(doc["sprints"]) == 3
    env.use(site, "board: Demo board")
    assert env.json("sprints", "list")[0] == 0
    env.use(site, "board: 1")
    assert env.json("sprints", "list")[0] == 0


def test_sprints_board_name_is_exact(env: Env, site: FakeProvider) -> None:
    code, doc, _ = env.json("sprints", "list", "--board", "Demo")
    assert code == 10 and doc["error"]["details"]["option"] == "board"


def test_sprints_unknown_board_id_is_not_found(env: Env, site: FakeProvider) -> None:
    code, doc, _ = env.json("sprints", "list", "--board", "77")
    assert code == 10 and doc["error"]["details"]["option"] == "board"


def two_boards(
    fake: FakeProvider,
    *,
    names: tuple[str, str] = ("Twin", "twin"),
    kinds: tuple[str, str] = ("scrum", "scrum"),
    numbers: tuple[int, int] = (1, 2),
) -> None:
    boards = [
        {"id": numbers[0], "name": names[0], "type": kinds[0]},
        {"id": numbers[1], "name": names[1], "type": kinds[1]},
    ]
    reply(fake, "GET", "/board", {"isLast": True, "values": boards})


def test_several_boards_with_the_name_are_ambiguous(env: Env, site: FakeProvider) -> None:
    two_boards(site)
    code, doc, _ = env.json("sprints", "list", "--board", "TWIN")
    assert code == 21 and doc["error"]["code"] == "ambiguous_board"
    assert len(doc["error"]["details"]["candidates"]) == 2


def test_several_scrum_boards_of_the_project_are_ambiguous(env: Env, site: FakeProvider) -> None:
    two_boards(site, names=("A", "B"))
    code, doc, _ = env.json("sprints", "list")
    assert code == 21 and doc["error"]["code"] == "ambiguous_board"


def test_only_scrum_boards_count_for_a_project(env: Env, site: FakeProvider) -> None:
    two_boards(site, names=("A", "B"), kinds=("kanban", "scrum"), numbers=(2, 1))
    code, doc, _ = env.json("sprints", "list")
    assert code == 0 and doc["sprints"]
    two_boards(site, names=("A", "B"), kinds=("kanban", "simple"))
    code, doc, _ = env.json("sprints", "list")
    assert code == 10 and doc["error"]["details"]["option"] == "board"


def test_a_project_with_no_board_is_not_found(env: Env, site: FakeProvider) -> None:
    code, doc, _ = env.json("sprints", "list", "--project", "NOPE")
    assert code == 10 and doc["error"]["details"]["option"] == "board"


def test_no_board_to_derive_is_missing_option(env: Env, fake: FakeProvider) -> None:
    env.use(fake)
    code, doc, _ = env.json("sprints", "list")
    assert (code, doc["error"]["reason"]) == (2, "missing_option")
    env.use(fake, "projects: [DEMO, OTHER]")
    assert env.json("sprints", "list")[0] == 2
    assert env.json("sprints", "list", "--project", "DEMO")[0] == 0


@pytest.mark.parametrize("board", ["", " ", "a\x07b", "x" * 300, "0"])
def test_a_bad_board_value(env: Env, site: FakeProvider, board: str) -> None:
    code, doc, _ = env.json("sprints", "list", "--board", board)
    assert (code, doc["error"]["reason"]) == (22, "invalid_value")


def test_board_names_are_matched_over_pages_and_stop_at_5(env: Env, site: FakeProvider) -> None:
    def page(call: Any) -> Out:
        start = int(call.one("startAt", "0"))
        values = [
            {"id": start + i + 1, "name": f"B{start + i + 1}", "type": "scrum"} for i in range(50)
        ]
        return Out(200, {"isLast": False, "values": values})

    override(site, "GET", "/board", page)
    code, doc, _ = env.json("sprints", "list", "--board", "b120")
    # B120 sits on the third page; the fake knows only board 1, so its sprint call is the 404
    assert code == 10 and any(c.path == "/rest/agile/1.0/board/120/sprint" for c in site.calls)
    assert (
        len([c for c in site.calls if c.path == "/rest/agile/1.0/board"]) == 5
    )  # all pages: a name match must see every board
    site.calls.clear()
    code, doc, _ = env.json("sprints", "list", "--board", "nothing")
    assert code == 10 and len([c for c in site.calls if c.path == "/rest/agile/1.0/board"]) == 5


def test_sprint_paging_ends_on_a_provider_that_never_stops(env: Env, site: FakeProvider) -> None:
    def page(call: Any, board: str) -> Out:
        sprint = {"id": 5, "name": "Same", "state": "active", "originBoardId": 1}
        return Out(200, {"isLast": False, "values": [sprint]})

    override(site, "GET", "/sprint", page)
    code, doc, _ = env.json("sprints", "list", "--board", "1")
    assert code == 0 and [s["id"] for s in doc["sprints"]] == [5]
    assert len([c for c in site.calls if c.path.endswith("/sprint")]) == 20


def test_sprint_paging_reads_pages_until_the_limit(env: Env, site: FakeProvider) -> None:
    def page(call: Any, board: str) -> Out:
        start = int(call.one("startAt", "0"))
        values = [
            {
                "id": start + i + 1,
                "name": f"S{start + i + 1}",
                "state": "closed",
                "originBoardId": 1,
            }
            for i in range(50)
        ]
        return Out(200, {"isLast": False, "values": values})

    override(site, "GET", "/sprint", page)
    _, doc, _ = env.json("sprints", "list", "--board", "1", "--limit", "120")
    assert [s["id"] for s in doc["sprints"]] == list(range(1, 121))
    assert len([c for c in site.calls if c.path.endswith("/sprint")]) == 3


def test_a_provider_that_ignores_the_state_filter_cannot_leak_other_sprints(
    env: Env, site: FakeProvider
) -> None:
    rows = [
        {"id": i, "name": "n", "state": s, "originBoardId": 1}
        for i, s in ((1, "closed"), (2, "active"), (3, "future"))
    ]
    reply(site, "GET", "/sprint", {"isLast": True, "values": rows})
    _, doc, _ = env.json("sprints", "list", "--board", "1", "--state", "future")
    assert [s["id"] for s in doc["sprints"]] == [3]


def test_hostile_sprints(env: Env, site: FakeProvider) -> None:
    good = {
        "id": 1,
        "name": "Good",
        "state": "active",
        "originBoardId": 4,
        "startDate": "2026-03-01T10:00:00.000+02:00",
        "endDate": "junk",
    }
    rows = [
        good,
        {**good, "id": 2, "originBoardId": None},
        {**good, "id": 3, "state": "weird"},
        {**good, "id": "4"},
        {**good, "id": True},
        {**good, "id": -1},
        {**good, "id": 6, "name": None},
        {**good, "id": 7, "name": "a\u202eb\x07\n" + "n" * 400},
        {**good, "id": 1},
        None,
        "x",
        [],
    ]  # fmt: skip
    reply(site, "GET", "/sprint", {"isLast": True, "values": rows})
    code, doc, err = env.json("sprints", "list", "--board", "1")
    assert code == 0 and "warning" in err
    assert [s["id"] for s in doc["sprints"]] == [1, 7]
    first, seven = doc["sprints"]
    assert first["start"] == "2026-03-01T08:00:00Z" and first["end"] is None and first["board"] == 4
    assert len(seven["name"]) <= 256 and not re.search(r"[\u202e\x07\n]", seven["name"])


@pytest.mark.parametrize("bad", [None, [], "x", {"values": "no"}, {}])
def test_a_sprint_page_that_is_not_a_list(env: Env, site: FakeProvider, bad: Any) -> None:
    reply(site, "GET", "/sprint", bad)
    code, doc, _ = env.json("sprints", "list", "--board", "1")
    assert code == 12 and doc["error"]["code"] == "provider_changed"


def test_a_404_on_the_board_listing_is_a_provider_change(env: Env, site: FakeProvider) -> None:
    reply(site, "GET", "/board", {"errorMessages": ["gone"]}, status=404)
    code, doc, _ = env.json("sprints", "list")
    assert code == 12 and doc["error"]["code"] == "provider_changed"


def test_sprints_show(env: Env, site: FakeProvider) -> None:
    code, doc, err = env.json("sprints", "show", "2")
    assert code == 0 and err == ""
    assert doc["sprint"] == {
        "id": 2, "name": "Sprint two", "state": "active", "board": 1,
        "start": "2026-02-16T15:00:00Z", "end": "2026-03-02T15:00:00Z",
    }  # fmt: skip
    golden("sprints-show", "default", doc)
    assert [c.path for c in site.calls if "board" in c.path] == []  # no board is needed


def test_sprints_show_by_project_picks_the_site(env: Env, site: FakeProvider) -> None:
    assert env.json("sprints", "show", "3", "--project", "DEMO")[0] == 0


def test_sprints_show_missing_is_not_found(env: Env, site: FakeProvider) -> None:
    code, doc, _ = env.json("sprints", "show", "99")
    assert code == 10 and doc["error"]["code"] == "not_found"


@pytest.mark.parametrize(
    "body",
    [
        None,
        [],
        "x",
        {},
        {"id": 9, "name": "other", "state": "active", "originBoardId": 1},
        {"id": 2, "name": "n", "state": "active"},
    ],
)
def test_sprints_show_an_unusable_answer(env: Env, site: FakeProvider, body: Any) -> None:
    reply(site, "GET", "/sprint/(\\d+)", body)
    code, doc, _ = env.json("sprints", "show", "2")
    assert code == 12 and doc["error"]["code"] == "provider_changed"


def test_sprints_human_output(env: Env, site: FakeProvider) -> None:
    code, out, _ = env.run("sprints", "list")
    assert code == 0 and "2 Sprint two [active] board 1" in out
    assert env.run("sprints", "show", "3")[1].startswith("3 Sprint three [future]")


# --- every read: no field ids, no credentials ---------------------------------------------------


COMMANDS = [
    ["comments", "DEMO-1"],
    ["comments", "DEMO-1", "--own", "--newest-first"],
    ["transitions", "DEMO-1"],
    ["sprints", "list"],
    ["sprints", "show", "2"],
    ["comments", "DEMO-99"],
    ["sprints", "list", "--board", "77"],
]


@pytest.mark.parametrize("debug", [False, True])
@pytest.mark.parametrize("flags", [[], ["--verbose"], ["--json", "--verbose"], ["--json"]])
@pytest.mark.parametrize("argv", COMMANDS, ids=lambda a: " ".join(a))
def test_the_token_never_appears_in_any_channel(
    env: Env, talk: FakeProvider, argv: list[str], flags: list[str], debug: bool
) -> None:
    env.use(talk, "projects: [DEMO]")
    if debug:
        env.mp.setenv("TICKETCTL_DEBUG", "1")
    code, out, err = env.run(*flags, *argv)
    assert code in (0, 10)
    forbidden = [SITE_TOKEN, talk.basic_header().split(" ", 1)[1]]
    for text in (out, err, *env.files().values()):
        for secret in forbidden:
            assert secret not in text
    if "--verbose" in flags:
        assert "Authorization" not in err
    assert not CUSTOM_ID.search(out)


def test_the_token_is_scrubbed_from_a_provider_error_that_echoes_it(
    env: Env, talk: FakeProvider
) -> None:
    reply(talk, "GET", "/comment", {"errorMessages": [f"bad {SITE_TOKEN}"]}, status=418)
    code, out, err = env.run("--json", "--verbose", "comments", "DEMO-1")
    assert code == 12
    for text in (out, err, *env.files().values()):
        assert SITE_TOKEN not in text


def test_a_token_in_a_comment_or_a_sprint_name_is_redacted(env: Env, talk: FakeProvider) -> None:
    add(talk, "DEMO-1", "900", f"leak {SITE_TOKEN}", 99)
    code, out, _ = env.run("--json", "comments", "DEMO-1")
    assert code == 0 and SITE_TOKEN not in out
    rows = [{"id": 1, "name": f"s {SITE_TOKEN}", "state": "active", "originBoardId": 1}]
    reply(talk, "GET", "/sprint", {"isLast": True, "values": rows})
    code, out, _ = env.run("--json", "sprints", "list", "--board", "1")
    assert code == 0 and SITE_TOKEN not in out


def test_write_only_flags_on_the_read_commands_are_usage_errors(
    env: Env, talk: FakeProvider
) -> None:
    for argv in (
        ["comments", "DEMO-1", "--yes"],
        ["transitions", "DEMO-1", "--dry-run"],
        ["sprints", "list", "--actor", "human:x"],
    ):
        code, doc, _ = env.json(*argv)
        assert code == 2 and doc["error"]["reason"] == "invalid_argument"
