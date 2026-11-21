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


def rows(count: int, first: int = 1) -> list[dict[str, Any]]:
    """``count`` raw comments, ids ``first``..., one minute apart, oldest first."""
    return [
        raw_comment(
            str(first + n),
            (BASE + timedelta(minutes=first + n)).strftime("%Y-%m-%dT%H:%M:%S.000+0000"),
        )
        for n in range(count)
    ]


def serve(
    fake: FakeProvider,
    served: list[dict[str, Any]],
    *,
    total: int | None = None,
    page_cap: int | None = None,
    whole: bool = False,
) -> list[tuple[int, int]]:
    """Answer the comment endpoint from ``served`` in that order, whatever ``orderBy`` says.

    ``page_cap`` caps a page below ``maxResults``; ``whole`` ignores ``maxResults`` (the page is
    the rest of the list). The returned list collects the ``(startAt, size)`` of every page.
    """
    seen: list[tuple[int, int]] = []

    def page(call: Any, key: str) -> Out:
        start = int(call.one("startAt", "0"))
        size = int(call.one("maxResults", "100"))
        chunk = served[start:] if whole else served[start : start + min(size, page_cap or size)]
        seen.append((start, len(chunk)))
        count = len(served) if total is None else total
        return Out(200, {"startAt": start, "maxResults": size, "total": count, "comments": chunk})

    override(fake, "GET", "/comment", page)
    return seen


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
    assert doc["truncated"] is False  # the comment is listed, so nothing was dropped


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


def test_a_duration_since_cuts_at_the_clock_of_the_command(
    env: Env, talk: FakeProvider, monkeypatch: pytest.MonkeyPatch
) -> None:
    class Clock(datetime):
        @classmethod
        def now(cls, tz: Any = None) -> Any:
            return datetime(2026, 3, 1, 15, 45, tzinfo=UTC)

    monkeypatch.setattr(commentscmd, "datetime", Clock)
    # the comments are at 15:00, 15:10, 15:20, 15:30 and 15:40
    _, doc, _ = env.json("comments", "DEMO-1", "--since", "20m")  # cut-off 15:25
    assert ids(doc) == ["104", "105"]
    _, edge, _ = env.json("comments", "DEMO-1", "--since", "25m")  # cut-off 15:20, inclusive
    assert ids(edge) == ["103", "104", "105"]
    _, hours, _ = env.json("comments", "DEMO-1", "--since", "1h")
    assert len(hours["items"]) == 5


def test_since_with_newest_first_filters_from_the_newest_end(env: Env, talk: FakeProvider) -> None:
    _, doc, _ = env.json(
        "comments", "DEMO-1", "--since", "2026-03-01T15:20:00Z", "--newest-first", "--limit", "2"
    )
    assert ids(doc) == ["105", "104"] and doc["truncated"] is True and doc["total"] == 5
    _, all_of_them, _ = env.json(
        "comments", "DEMO-1", "--since", "2026-03-01T15:20:00Z", "--newest-first"
    )
    assert ids(all_of_them) == ["105", "104", "103"] and all_of_them["truncated"] is False


def test_since_on_a_thread_above_the_window_filters_inside_the_first_1000(
    env: Env, site: FakeProvider
) -> None:
    for n in range(1100):
        add(site, "DEMO-1", str(2000 + n), "x", n)
    cut = (BASE + timedelta(minutes=1050)).strftime("%Y-%m-%dT%H:%M:%SZ")
    _, oldest, _ = env.json("comments", "DEMO-1", "--since", cut)
    assert oldest["items"] == [] and oldest["truncated"] is True  # only the oldest 1000 were read
    _, newest, _ = env.json(
        "comments", "DEMO-1", "--since", cut, "--newest-first", "--limit", "200"
    )
    assert len(newest["items"]) == 50  # the newest end holds every match
    assert newest["truncated"] is True  # the window still stopped short of the 100 oldest


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
    assert len(comment_paths(talk)) == 20
    assert "comments" in err  # the repeated ids


def test_an_empty_first_page_with_a_larger_total_is_truncated(env: Env, talk: FakeProvider) -> None:
    serve(talk, [], total=7)
    code, doc, _ = env.json("comments", "DEMO-1")
    assert code == 0 and doc["items"] == [] and doc["total"] == 7 and doc["truncated"] is True


def test_an_empty_page_after_some_comments_is_truncated(env: Env, talk: FakeProvider) -> None:
    serve(talk, rows(100), total=250)  # the second page is empty
    _, doc, _ = env.json("comments", "DEMO-1", "--limit", "200")
    assert len(doc["items"]) == 100 and doc["total"] == 250 and doc["truncated"] is True


def test_an_empty_page_when_the_limit_is_met_is_truncated(env: Env, talk: FakeProvider) -> None:
    serve(talk, rows(200), total=500)
    _, doc, _ = env.json("comments", "DEMO-1", "--limit", "200")
    assert len(doc["items"]) == 200 and doc["total"] == 500 and doc["truncated"] is True


def test_a_complete_scan_ending_on_the_count_is_not_truncated(env: Env, talk: FakeProvider) -> None:
    serve(talk, rows(120))
    _, doc, _ = env.json("comments", "DEMO-1", "--limit", "200")
    assert len(doc["items"]) == 120 and doc["truncated"] is False


def test_a_dropped_comment_makes_the_listing_truncated(env: Env, talk: FakeProvider) -> None:
    good = raw_comment("1", "2026-03-01T09:00:00.000-0600")
    reply(talk, "GET", "/comment", {"total": 2, "comments": [good, {**good, "id": "has space"}]})
    code, doc, err = env.json("comments", "DEMO-1")
    assert code == 0 and ids(doc) == ["1"] and doc["total"] == 2 and doc["truncated"] is True
    assert "warning" in err
    quiet = env.run("--quiet", "comments", "DEMO-1")
    assert quiet[0] == 0 and quiet[2] == ""  # --quiet drops the warning, the flag stays


def test_an_unreadable_time_drops_the_comment_and_truncates(env: Env, talk: FakeProvider) -> None:
    good = raw_comment("1", "2026-03-01T09:00:00.000-0600")
    reply(
        talk,
        "GET",
        "/comment",
        {"total": 2, "comments": [good, {**good, "id": "2", "created": "x"}]},
    )
    _, doc, _ = env.json("comments", "DEMO-1")
    assert ids(doc) == ["1"] and doc["truncated"] is True


def test_a_repeated_comment_id_makes_the_listing_truncated(env: Env, talk: FakeProvider) -> None:
    good = raw_comment("1", "2026-03-01T09:00:00.000-0600")
    reply(talk, "GET", "/comment", {"total": 2, "comments": [good, good]})
    _, doc, _ = env.json("comments", "DEMO-1")
    assert ids(doc) == ["1"] and doc["truncated"] is True


def test_a_provider_ignoring_order_by_still_returns_the_true_oldest(
    env: Env, site: FakeProvider
) -> None:
    serve(site, list(reversed(rows(250))))  # newest first although the request says oldest first
    _, doc, _ = env.json("comments", "DEMO-1", "--limit", "5")
    assert ids(doc) == ["1", "2", "3", "4", "5"] and doc["truncated"] is True
    assert len(comment_paths(site)) == 3  # the whole window is read before the cut
    _, newest, _ = env.json("comments", "DEMO-1", "--limit", "2", "--newest-first")
    assert ids(newest) == ["250", "249"]


def test_one_page_larger_than_the_allowance_is_cut_at_1000(env: Env, site: FakeProvider) -> None:
    seen = serve(site, rows(1500), whole=True)
    _, doc, _ = env.json("comments", "DEMO-1", "--newest-first", "--limit", "1")
    assert ids(doc) == ["1000"] and doc["total"] == 1500 and doc["truncated"] is True
    assert seen == [(0, 1500)]


def test_short_pages_advance_start_by_the_items_received(env: Env, site: FakeProvider) -> None:
    seen = serve(site, rows(100), page_cap=30)
    _, doc, _ = env.json("comments", "DEMO-1", "--limit", "200")
    assert [start for start, _ in seen] == [0, 30, 60, 90]
    assert len(doc["items"]) == 100 and doc["truncated"] is False


def test_the_default_limit_is_50(env: Env, site: FakeProvider) -> None:
    serve(site, rows(60))
    _, doc, _ = env.json("comments", "DEMO-1")
    assert len(doc["items"]) == 50 and doc["truncated"] is True
    assert commentscmd.DEFAULT_LIMIT == 50 and commentscmd.effective_limit(None) == 50


VALID_META = {"v": 1, "actor_kind": "human", "body_sha256": HASH_A}


@pytest.mark.parametrize(
    "value", ["text", ["x"], None, {"v": 2}, {"v": True}, {"v": "1"}, {}], ids=repr
)
def test_a_damaged_property_on_an_own_comment_is_still_authored_by_the_tool(
    env: Env, site: FakeProvider, value: Any
) -> None:
    add(site, "DEMO-1", "301", "own", 0, props={META: value})
    add(site, "DEMO-1", "302", "other", 1, author=OTHER_ACCOUNT, props={META: value})
    code, doc, err = env.json("comments", "DEMO-1")
    by_id = {c["id"]: c for c in doc["items"]}
    assert code == 0 and err == ""
    mine, other = by_id["301"], by_id["302"]
    assert mine["authored_by_ticketctl"] is True
    assert (mine["actor_kind"], mine["body_sha256"], mine["dedupe_key_sha256"]) == (None,) * 3
    assert other["authored_by_ticketctl"] is False and other["actor_kind"] is None


def test_a_property_key_that_differs_in_case_is_not_the_property(
    env: Env, site: FakeProvider
) -> None:
    add(site, "DEMO-1", "301", "own", 0, props={"Ticketctl.Meta": VALID_META})
    _, doc, _ = env.json("comments", "DEMO-1")
    (mine,) = doc["items"]
    assert mine["own"] is True and mine["authored_by_ticketctl"] is False
    assert (mine["actor_kind"], mine["body_sha256"]) == (None, None)


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
    assert [f["name"] for f in fields] == ["<id>", "unknown field", "Has <id> inside"]
    assert fields[0]["allowed_values"] == ["a", "b", "c"] and len(fields[0]["kind"]) <= 64
    assert fields[1]["kind"] == "unknown" and "allowed_values" not in fields[1]
    assert not CUSTOM_ID.search(json.dumps(doc))


def _transition(tid: str, **kw: Any) -> dict[str, Any]:
    to = {"name": "Done", "statusCategory": {"key": "done"}}
    return {"id": tid, "name": f"t{tid}", "to": to, "hasScreen": False, "fields": {}, **kw}


def test_repeated_transition_ids_are_kept_once_in_the_list(env: Env, site: FakeProvider) -> None:
    listed = [_transition("1"), _transition("1", name="again"), _transition("2")]
    reply(site, "GET", "/transitions", {"transitions": listed})
    code, doc, err = env.json("transitions", "DEMO-1")
    assert code == 0 and "warning" in err
    assert [(t["id"], t["name"]) for t in doc["transitions"]] == [("1", "t1"), ("2", "t2")]


def test_optional_fields_do_not_count_against_the_required_field_cap(
    env: Env, site: FakeProvider
) -> None:
    fields: dict[str, Any] = {
        f"cf_{n}": {"required": False, "name": f"Opt {n}"} for n in range(250)
    }
    fields["cf_must"] = {"required": True, "name": "Must", "schema": {"type": "string"}}
    reply(site, "GET", "/transitions", {"transitions": [_transition("1", fields=fields)]})
    code, doc, err = env.json("transitions", "DEMO-1")
    assert code == 0 and err == ""
    (t,) = doc["transitions"]
    assert [f["name"] for f in t["required_fields"]] == ["Must"]


def test_more_than_200_required_fields_are_cut_with_a_warning(env: Env, site: FakeProvider) -> None:
    fields = {f"cf_{n}": {"required": True, "name": f"Req {n}"} for n in range(230)}
    reply(site, "GET", "/transitions", {"transitions": [_transition("1", fields=fields)]})
    code, doc, err = env.json("transitions", "DEMO-1")
    assert code == 0 and "warning" in err and "required_fields" in err
    assert len(doc["transitions"][0]["required_fields"]) == 200


@pytest.mark.parametrize("flag", ["true", 1, None, [], {}])
def test_a_required_flag_that_is_not_a_boolean_is_warned_about(
    env: Env, site: FakeProvider, flag: Any
) -> None:
    fields = {"cf_a": {"required": flag, "name": "Odd"}}
    reply(site, "GET", "/transitions", {"transitions": [_transition("1", fields=fields)]})
    code, doc, err = env.json("transitions", "DEMO-1")
    assert code == 0 and doc["transitions"][0]["required_fields"] == []
    assert "warning" in err and "required_fields" in err


def test_a_field_without_a_required_flag_is_silently_optional(env: Env, site: FakeProvider) -> None:
    fields = {"cf_a": {"name": "Plain"}, "cf_b": {"required": False, "name": "No"}}
    reply(site, "GET", "/transitions", {"transitions": [_transition("1", fields=fields)]})
    code, _, err = env.json("transitions", "DEMO-1")
    assert code == 0 and err == ""


def test_an_uppercase_field_id_in_a_name_is_hidden(env: Env, site: FakeProvider) -> None:
    fields = {"cf_a": {"required": True, "name": "CUSTOMFIELD_90001 and CustomField_2"}}
    reply(site, "GET", "/transitions", {"transitions": [_transition("1", fields=fields)]})
    _, doc, _ = env.json("transitions", "DEMO-1")
    assert doc["transitions"][0]["required_fields"][0]["name"] == "<id> and <id>"
    assert not CUSTOM_ID.search(json.dumps(doc))


def test_a_transition_name_that_is_blank_is_dropped(env: Env, site: FakeProvider) -> None:
    listed = [
        _transition("1", name=""),
        _transition("2", name="   "),
        _transition("3", name="\u200b"),
        _transition("4", name="Real"),
    ]
    reply(site, "GET", "/transitions", {"transitions": listed})
    code, doc, err = env.json("transitions", "DEMO-1")
    assert code == 0 and "warning" in err
    # a zero-width name is shown as a visible marker, so it is not blank to a reader
    assert [(t["id"], t["name"]) for t in doc["transitions"]] == [("3", "<U+200B>"), ("4", "Real")]


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


def _board_pages(site: FakeProvider) -> None:
    """A provider with endless pages of 50 boards named B1, B2, ... (never ``isLast``)."""

    def page(call: Any) -> Out:
        start = int(call.one("startAt", "0"))
        values = [
            {"id": start + i + 1, "name": f"B{start + i + 1}", "type": "scrum"} for i in range(50)
        ]
        return Out(200, {"isLast": False, "values": values})

    override(site, "GET", "/board", page)


def board_calls(site: FakeProvider) -> list[Any]:
    return [c for c in site.calls if c.path == "/rest/agile/1.0/board"]


def test_a_name_with_no_match_after_5_pages_is_not_found(env: Env, site: FakeProvider) -> None:
    _board_pages(site)
    code, doc, _ = env.json("sprints", "list", "--board", "nothing")
    assert code == 10 and doc["error"]["details"]["option"] == "board"
    assert len(board_calls(site)) == 5  # a name match must see every board it can
    assert "first boards were searched" in doc["error"]["message"]


def test_a_single_name_match_in_a_cut_off_search_is_refused(env: Env, site: FakeProvider) -> None:
    _board_pages(site)
    code, doc, _ = env.json("sprints", "list", "--board", "b120")
    assert code == 12 and doc["error"]["code"] == "lookup_incomplete"
    assert doc["error"]["retryable"] is False and "--board" in doc["error"]["message"]
    assert len(board_calls(site)) == 5
    assert not [c for c in site.calls if c.path.endswith("/sprint")]  # no sprint call was made


def test_a_name_matching_twice_in_a_cut_off_search_is_still_ambiguous(
    env: Env, site: FakeProvider
) -> None:
    def page(call: Any) -> Out:
        start = int(call.one("startAt", "0"))
        values = [{"id": start + i + 1, "name": "Same", "type": "scrum"} for i in range(50)]
        return Out(200, {"isLast": False, "values": values})

    override(site, "GET", "/board", page)
    code, doc, _ = env.json("sprints", "list", "--board", "same")
    assert code == 21 and doc["error"]["code"] == "ambiguous_board"


def test_a_single_project_board_in_a_cut_off_listing_is_refused(
    env: Env, site: FakeProvider
) -> None:
    def page(call: Any) -> Out:
        start = int(call.one("startAt", "0"))
        values = [
            {"id": start + i + 1, "name": f"B{start + i + 1}", "type": "kanban"} for i in range(50)
        ]
        if start == 0:
            values[0]["type"] = "scrum"  # the only scrum board of the first 250
        return Out(200, {"isLast": False, "values": values})

    override(site, "GET", "/board", page)
    code, doc, _ = env.json("sprints", "list")
    assert code == 12 and doc["error"]["code"] == "lookup_incomplete"
    assert "--board" in doc["error"]["message"]


def test_a_cut_off_project_listing_with_no_scrum_board_is_not_found(
    env: Env, site: FakeProvider
) -> None:
    def page(call: Any) -> Out:
        start = int(call.one("startAt", "0"))
        values = [{"id": start + i + 1, "name": "K", "type": "kanban"} for i in range(50)]
        return Out(200, {"isLast": False, "values": values})

    override(site, "GET", "/board", page)
    code, doc, _ = env.json("sprints", "list")
    assert code == 10 and doc["error"]["details"]["option"] == "board"


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


def test_a_400_on_the_board_sprint_path_means_the_board_has_no_sprints(
    env: Env, site: FakeProvider
) -> None:
    reply(site, "GET", "/sprint", {"errorMessages": ["no sprints"]}, status=400)
    code, doc, _ = env.json("sprints", "list", "--board", "1")
    assert (code, doc["error"]["code"], doc["error"]["reason"]) == (
        22,
        "invalid_request",
        "invalid_value",
    )
    assert doc["error"]["details"]["option"] == "board"
    assert "sprint" in doc["error"]["message"] and "no sprints" not in doc["error"]["message"]


def test_a_400_on_the_board_list_stays_a_provider_change(env: Env, site: FakeProvider) -> None:
    reply(site, "GET", "/board", {"errorMessages": ["bad"]}, status=400)
    code, doc, _ = env.json("sprints", "list")
    assert code == 12 and doc["error"]["code"] == "provider_changed"


def test_a_blank_sprint_name_is_dropped_and_a_zero_width_one_is_visible(
    env: Env, site: FakeProvider
) -> None:
    def sprint(sid: int, name: str, **kw: Any) -> dict[str, Any]:
        return {"id": sid, "name": name, "state": "active", "originBoardId": 1, **kw}

    rows = [sprint(1, ""), sprint(2, "   "), sprint(3, "\u200b"), sprint(4, "Real")]
    reply(site, "GET", "/sprint", {"isLast": True, "values": rows})
    code, doc, err = env.json("sprints", "list", "--board", "1")
    assert code == 0 and "warning" in err
    assert [(s["id"], s["name"]) for s in doc["sprints"]] == [(3, "<U+200B>"), (4, "Real")]


@pytest.mark.parametrize("origin", [0, -1])
def test_a_sprint_with_an_origin_board_below_one_is_dropped(
    env: Env, site: FakeProvider, origin: int
) -> None:
    rows = [
        {"id": 1, "name": "Zero", "state": "active", "originBoardId": origin},
        {"id": 2, "name": "Fine", "state": "active", "originBoardId": 1},
    ]
    reply(site, "GET", "/sprint", {"isLast": True, "values": rows})
    code, doc, err = env.json("sprints", "list", "--board", "1")
    assert code == 0 and [s["id"] for s in doc["sprints"]] == [2] and "warning" in err
    reply(site, "GET", "/sprint/(\\d+)", rows[0])
    assert env.json("sprints", "show", "1")[0] == 12


def test_field_ids_in_sprint_and_board_names_are_hidden(env: Env, site: FakeProvider) -> None:
    rows = [{"id": 1, "name": "Sprint CUSTOMFIELD_90001 x", "state": "active", "originBoardId": 1}]
    reply(site, "GET", "/sprint", {"isLast": True, "values": rows})
    _, doc, _ = env.json("sprints", "list", "--board", "1")
    assert doc["sprints"][0]["name"] == "Sprint <id> x"
    assert not CUSTOM_ID.search(json.dumps(doc))
    two_boards(site, names=("Twin customfield_90002", "twin customfield_90002"))
    code, doc, _ = env.json("sprints", "list", "--board", "TWIN CUSTOMFIELD_90002")
    assert code == 21
    assert doc["error"]["details"]["candidates"] == ["1 (Twin <id>)", "2 (twin <id>)"]


def test_the_default_sprint_limit_is_50(env: Env, site: FakeProvider) -> None:
    def page(call: Any, board: str) -> Out:
        start = int(call.one("startAt", "0"))
        size = int(call.one("maxResults", "50"))
        values = [
            {"id": n + 1, "name": f"S{n + 1}", "state": "closed", "originBoardId": 1}
            for n in range(start, min(start + size, 80))
        ]
        return Out(200, {"isLast": start + size >= 80, "values": values})

    override(site, "GET", "/sprint", page)
    _, doc, _ = env.json("sprints", "list", "--board", "1")
    assert len(doc["sprints"]) == 50


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
