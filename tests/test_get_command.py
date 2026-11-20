"""``get KEY...`` through the CLI against the fake provider (issue #15).

Uses the fake (loopback http, ``allow_http``), a private config and a private state directory.
Provider field ids are the synthetic ``customfield_90001..90006`` ones of the fake.
"""

from __future__ import annotations

import json
import re
import threading
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from tests.fake_provider import (
    ME_ACCOUNT,
    OTHER_ACCOUNT,
    SITE_EMAIL,
    SITE_TOKEN,
    FakeProvider,
    Fault,
    adf_doc,
    adf_paragraph,
)
from tests.golden import assert_golden
from ticketctl import getcmd, sanitize
from ticketctl.cli import main

TOKEN_VAR = "TICKETCTL_TEST_TOKEN"
CUSTOM_ID = re.compile(r"customfield_[0-9]+")
META = "ticketctl.meta"


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

    def site(self, fake: FakeProvider, name: str, extra: str = "") -> str:
        lines = [
            f"url: {fake.url}",
            f"email: {SITE_EMAIL}",
            f"token_env: {TOKEN_VAR}",
            "allow_http: true",
            *([extra] if extra else []),
        ]
        return f"  {name}:\n" + "".join(f"    {line}\n" for line in lines)

    def write(self, text: str) -> None:
        self.config.write_text(text, encoding="utf-8")
        self.config.chmod(0o600)

    def use(self, fake: FakeProvider, extra: str = "") -> None:
        self.write("sites:\n" + self.site(fake, "main", extra))

    def run(self, *argv: str) -> tuple[int, str, str]:
        code = main(["--config", str(self.config), *argv])
        cap = self.capsys.readouterr()
        return code, cap.out, cap.err

    def get(self, *argv: str) -> tuple[int, Any, str]:
        code, out, err = self.run("--json", "get", *argv)
        return code, json.loads(out), err


@pytest.fixture
def env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: Any) -> Env:
    return Env(tmp_path, monkeypatch, capsys)


@pytest.fixture
def site(env: Env, fake: FakeProvider) -> FakeProvider:
    env.use(fake)
    return fake


def golden(fake: FakeProvider, case: str, doc: Any) -> None:
    """The golden check with the fake's port replaced, so the file is the same on every run."""
    text = json.dumps(doc).replace(fake.url, "https://tracker.example.test")
    assert_golden("cli", "get", case, json.loads(text))


def issue_gets(fake: FakeProvider) -> list[str]:
    return [
        c.path
        for c in fake.calls
        if c.method == "GET" and re.fullmatch(r"/rest/api/3/issue/[^/]+", c.path)
    ]


def add_comment(
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
    when = datetime(2026, 3, 1, 15, 0, tzinfo=UTC) + timedelta(minutes=minutes)
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


# --- single key ------------------------------------------------------------------------------


def test_a_single_key_prints_the_ticket_and_no_errors(env: Env, site: FakeProvider) -> None:
    code, doc, err = env.get("DEMO-2")
    assert code == 0
    assert doc["errors"] == []
    (t,) = doc["tickets"]
    assert t["key"] == "DEMO-2" and t["site"] == "main"
    assert t["status"] == {"name": "In Progress", "category": "indeterminate"}
    assert t["priority"] == {"name": "Medium", "rank": 2}
    assert "comments" not in t and "description_adf" not in t
    assert not CUSTOM_ID.search(json.dumps(doc) + err)
    golden(site, "single", doc)


def test_the_provider_request_names_its_fields_and_never_everything(
    env: Env, site: FakeProvider
) -> None:
    env.get("DEMO-2")
    (call,) = [c for c in site.calls if c.path == "/rest/api/3/issue/DEMO-2"]
    fields = (call.one("fields") or "").split(",")
    assert fields and "*all" not in fields and "*navigable" not in fields
    assert "comment" not in fields


def test_a_missing_key_is_exit_10_with_the_envelope(env: Env, site: FakeProvider) -> None:
    code, out, _ = env.run("--json", "get", "DEMO-99")
    doc = json.loads(out)
    assert code == 10
    body = doc["error"]
    assert body["code"] == "not_found" and body["key"] == "DEMO-99" and body["site"] == "main"
    assert "missing or" in body["message"] and "permission" in body["message"]
    assert body["details"] is None


def test_lower_case_keys_and_browse_urls_are_accepted(env: Env, site: FakeProvider) -> None:
    code, doc, _ = env.get("demo-1", f"{site.url}/browse/DEMO-2")
    assert code == 0, doc
    assert [t["key"] for t in doc["tickets"]] == ["DEMO-1", "DEMO-2"]


def test_a_moved_ticket_carries_the_requested_key(env: Env, site: FakeProvider) -> None:
    site.move("DEMO-1", "DEMO-40")
    code, doc, _ = env.get("DEMO-1")
    assert code == 0
    (t,) = doc["tickets"]
    assert t["key"] == "DEMO-40" and t["requested_key"] == "DEMO-1"
    golden(site, "moved", doc)


# --- fields ----------------------------------------------------------------------------------


def test_fields_selects_the_properties_and_nothing_else(env: Env, site: FakeProvider) -> None:
    code, doc, _ = env.get("DEMO-2", "--fields", "status,labels")
    assert code == 0
    assert set(doc["tickets"][0]) == {"key", "site", "status", "labels"}


def test_an_ambiguous_points_field_only_hurts_the_estimate(env: Env, site: FakeProvider) -> None:
    # The fake has two candidate estimate fields: key,title still works (issue #12 criterion 2).
    code, doc, err = env.get("DEMO-2", "--fields", "key,title")
    assert code == 0 and not err.strip()
    assert doc["tickets"][0] == {
        "key": "DEMO-2",
        "site": "main",
        "title": "Seeded task in progress",
    }
    code, doc, err = env.get("DEMO-2", "--fields", "title,estimate")
    t = doc["tickets"][0]
    assert code == 0 and t["estimate"] is None and t["incomplete"] == ["estimate"]
    assert "estimate matches 2 fields" in err
    assert not CUSTOM_ID.search(err)


def test_the_default_read_warns_about_the_ambiguity_once(env: Env, site: FakeProvider) -> None:
    code, doc, err = env.get("DEMO-1", "DEMO-2", "DEMO-3")
    assert code == 0
    assert err.count("estimate matches 2 fields") == 1
    assert all(t["incomplete"] == ["estimate"] for t in doc["tickets"])


def test_an_unknown_field_is_exit_22_before_any_ticket_call(env: Env, site: FakeProvider) -> None:
    code, out, _ = env.run("--json", "get", "DEMO-1", "--fields", "title,nope")
    err = json.loads(out)["error"]
    assert (code, err["code"], err["reason"]) == (22, "invalid_request", "unknown_field")
    assert issue_gets(site) == [] and site.calls == []


@pytest.mark.parametrize("fields", ["comments", "description_adf"])
def test_gated_fields_need_their_flag(env: Env, site: FakeProvider, fields: str) -> None:
    code, out, _ = env.run("--json", "get", "DEMO-1", "--fields", fields)
    err = json.loads(out)["error"]
    assert (code, err["reason"]) == (22, "invalid_value")
    assert site.calls == []


def test_include_adf_adds_the_document(env: Env, site: FakeProvider) -> None:
    site.tickets["DEMO-1"].description = adf_doc(adf_paragraph("hello"))
    code, doc, _ = env.get("DEMO-1", "--include-adf", "--fields", "description,description_adf")
    t = doc["tickets"][0]
    assert code == 0 and t["description"] == "hello" and t["description_adf"]["type"] == "doc"


def test_acceptance_criteria_keeps_its_line_breaks(env: Env, fake: FakeProvider) -> None:
    env.use(fake, "fields:\n      acceptance_criteria:\n        name: Acceptance Criteria")
    fake.tickets["DEMO-1"].criteria = adf_doc(adf_paragraph("one"), adf_paragraph("two"))
    code, doc, _ = env.get("DEMO-1", "--fields", "acceptance_criteria")
    assert code == 0
    assert doc["tickets"][0]["acceptance_criteria"] == "one\n\ntwo"


def test_a_hostile_title_is_sanitized(env: Env, site: FakeProvider) -> None:
    site.tickets["DEMO-1"].summary = f"a\x1b[31mred‮bidi\x00 {SITE_TOKEN} customfield_1"
    code, out, err = env.run("--json", "get", "DEMO-1")
    assert code == 0
    title = json.loads(out)["tickets"][0]["title"]
    assert "\x1b" not in title and "‮" not in title and "\x00" not in title
    assert SITE_TOKEN not in out + err


# --- keys ------------------------------------------------------------------------------------


def test_input_order_is_kept_and_duplicates_are_read_once(env: Env, site: FakeProvider) -> None:
    code, doc, _ = env.get("DEMO-3", "DEMO-1", "demo-3", "DEMO-2", "DEMO-1")
    assert code == 0
    assert [t["key"] for t in doc["tickets"]] == ["DEMO-3", "DEMO-1", "DEMO-2"]
    assert sorted(issue_gets(site)) == [f"/rest/api/3/issue/DEMO-{n}" for n in (1, 2, 3)]


def test_a_bad_key_anywhere_is_exit_22_invalid_key_and_nothing_is_read(
    env: Env, site: FakeProvider
) -> None:
    code, out, _ = env.run("--json", "get", "DEMO-1", "not a key")
    err = json.loads(out)["error"]
    assert (code, err["code"], err["reason"]) == (22, "invalid_request", "invalid_key")
    assert site.calls == []


@pytest.mark.parametrize("bad", ["DEMO-1\n", "../x-1", "DEMO-", "DEMO-1/comment", "-1", "x" * 300])
def test_hostile_keys_never_reach_the_provider(env: Env, site: FakeProvider, bad: str) -> None:
    code, out, _ = env.run("--json", "get", bad)
    assert code == 22 and json.loads(out)["error"]["reason"] in ("invalid_key", "invalid_value")
    assert site.calls == []


@pytest.mark.parametrize("bad", ["bad key", "DEMO-1\n"])
def test_an_invalid_key_beats_the_cap_error_and_nothing_is_read(
    env: Env, site: FakeProvider, bad: str
) -> None:
    keys = seed_many(site, 51)
    site.calls.clear()
    code, out, _ = env.run("--json", "get", *keys, bad)
    err = json.loads(out)["error"]
    assert (code, err["code"], err["reason"]) == (22, "invalid_request", "invalid_key")
    assert site.calls == []


@pytest.mark.parametrize("order", [0, 1])
def test_one_key_typed_against_two_sites_is_mixed_sites(
    env: Env, fake: FakeProvider, fake_factory: Any, order: int
) -> None:
    other = fake_factory(None)
    other.seed()
    env.write(
        "sites:\n"
        + env.site(fake, "one", "projects: [DEMO]")
        + env.site(other, "two", "projects: [ZZNOPE]")
    )
    refs = ["DEMO-1", f"{other.url}/browse/DEMO-1"]
    code, out, _ = env.run("--json", "get", *(refs if order == 0 else refs[::-1]))
    err = json.loads(out)["error"]
    assert (code, err["code"], err["reason"]) == (22, "invalid_request", "mixed_sites")
    assert fake.calls == [] and other.calls == []


def seed_many(fake: FakeProvider, n: int) -> list[str]:
    keys = [f"DEMO-{100 + i}" for i in range(n)]
    for k in keys:
        fake.add_ticket(k, f"ticket {k}")
    return keys


def test_fifty_distinct_keys_are_read_and_the_51st_is_refused(env: Env, site: FakeProvider) -> None:
    keys = seed_many(site, 51)
    code, doc, _ = env.get(*keys[:50], keys[0])  # a duplicate does not count
    assert code == 0 and [t["key"] for t in doc["tickets"]] == keys[:50]
    site.calls.clear()
    code, out, _ = env.run("--json", "get", *keys)
    err = json.loads(out)["error"]
    assert (code, err["reason"]) == (22, "invalid_value")
    assert site.calls == []


def test_a_huge_list_of_one_key_is_one_read(env: Env, site: FakeProvider) -> None:
    code, doc, _ = env.get(*(["DEMO-1"] * 5000))
    assert code == 0 and len(doc["tickets"]) == 1
    assert issue_gets(site) == ["/rest/api/3/issue/DEMO-1"]


def test_no_key_is_a_usage_error(env: Env, site: FakeProvider) -> None:
    code, out, _ = env.run("--json", "get")
    assert code == 2 and json.loads(out)["error"]["code"] == "usage"


class _Gauge:
    """The fake's wake event with a count of the waits in progress (the slow requests in flight)."""

    def __init__(self, event: Any) -> None:
        self.event = event
        self.lock = threading.Lock()
        self.now = 0
        self.peak = 0

    def wait(self, timeout: float | None = None) -> bool:
        with self.lock:
            self.now += 1
            self.peak = max(self.peak, self.now)
        try:
            return bool(self.event.wait(timeout))
        finally:
            with self.lock:
                self.now -= 1

    def __getattr__(self, name: str) -> Any:
        return getattr(self.event, name)


def test_reads_run_four_at_a_time(
    env: Env, site: FakeProvider, monkeypatch: pytest.MonkeyPatch
) -> None:
    keys = seed_many(site, 12)
    site.inject(Fault("slow", method="GET", path=r"/issue/DEMO-1\d\d$", times=None, delay_s=0.05))
    gauge = _Gauge(site._wake)
    monkeypatch.setattr(site, "_wake", gauge)
    code, doc, _ = env.get(*keys)
    assert code == 0 and [t["key"] for t in doc["tickets"]] == keys
    assert 1 < gauge.peak <= 4


# --- partial results -------------------------------------------------------------------------


def test_some_missing_is_exit_0_with_errors(env: Env, site: FakeProvider) -> None:
    code, doc, _ = env.get("DEMO-1", "DEMO-77", "DEMO-2")
    assert code == 0
    assert [t["key"] for t in doc["tickets"]] == ["DEMO-1", "DEMO-2"]
    (e,) = doc["errors"]
    assert e["key"] == "DEMO-77" and e["error"]["code"] == "not_found"
    assert e["error"]["key"] == "DEMO-77" and e["error"]["site"] == "main"
    golden(site, "partial", doc)


def test_none_readable_exits_with_the_first_errors_code_and_the_list(
    env: Env, site: FakeProvider
) -> None:
    code, out, _ = env.run("--json", "get", "DEMO-77", "DEMO-78")
    err = json.loads(out)["error"]
    assert code == 10 and err["code"] == "not_found" and err["key"] == "DEMO-77"
    listed = err["details"]["errors"]
    assert [e["key"] for e in listed] == ["DEMO-77", "DEMO-78"]
    assert all(e["error"]["code"] == "not_found" for e in listed)


def test_a_missing_and_a_hidden_ticket_look_the_same(env: Env, site: FakeProvider) -> None:
    # The provider answers 404 for a ticket the account may not see, as for a missing one.
    site.inject(Fault("status", method="GET", path=r"/issue/DEMO-1$", status=404, times=None))
    code, out, _ = env.run("--json", "get", "DEMO-1", "DEMO-77")
    errs = json.loads(out)["error"]["details"]["errors"]
    messages = {e["error"]["message"] for e in errs}
    assert code == 10 and [e["key"] for e in errs] == ["DEMO-1", "DEMO-77"]
    assert len(messages) == 1
    assert re.search(r"missing or you have no permission", messages.pop())


def test_the_first_error_wins_over_a_later_one(env: Env, site: FakeProvider) -> None:
    site.inject(Fault("status", method="GET", path=r"/issue/DEMO-1$", status=403, times=None))
    code, out, _ = env.run("--json", "get", "DEMO-1", "DEMO-77")
    err = json.loads(out)["error"]
    assert code == 12 and err["code"] == "forbidden"
    assert [e["error"]["code"] for e in err["details"]["errors"]] == ["forbidden", "not_found"]


@pytest.mark.parametrize(
    ("status", "code", "exit_"),
    [
        (401, "bad_credentials", 12),
        (403, "forbidden", 12),
        (429, "rate_limited", 12),
        (503, "unavailable", 12),
        (500, "unavailable", 12),
        (400, "provider_changed", 12),
        (410, "provider_changed", 12),
        (418, "provider_changed", 12),
    ],
)
def test_provider_statuses_map_through_the_error_layer(
    env: Env, site: FakeProvider, status: int, code: str, exit_: int
) -> None:
    site.inject(Fault("status", method="GET", path=r"/issue/DEMO-1$", status=status, times=None))
    got, out, err = env.run("--json", "get", "DEMO-1")
    body = json.loads(out)["error"]
    assert (got, body["code"]) == (exit_, code)
    assert body["key"] == "DEMO-1" and body["site"] == "main"
    assert SITE_TOKEN not in out + err


def test_a_failure_of_one_key_does_not_hide_the_others(env: Env, site: FakeProvider) -> None:
    site.inject(Fault("status", method="GET", path=r"/issue/DEMO-1$", status=403, times=None))
    code, doc, _ = env.get("DEMO-1", "DEMO-2")
    assert code == 0 and [t["key"] for t in doc["tickets"]] == ["DEMO-2"]
    assert doc["errors"][0]["error"]["code"] == "forbidden"


def test_a_payload_without_fields_is_a_stub_not_an_error(
    env: Env, site: FakeProvider, monkeypatch: pytest.MonkeyPatch
) -> None:
    real = site._get_issue

    def broken(call: Any, key: str) -> Any:
        out = real(call, key)
        out.body = {"key": out.body["key"], "id": out.body["id"]}
        return out

    monkeypatch.setattr(site, "_get_issue", broken)
    site._routes.clear()
    site._build_routes()
    code, doc, _ = env.get("DEMO-1", "DEMO-2")
    assert code == 0 and doc["errors"] == []
    assert doc["tickets"][0] == {
        "key": "DEMO-1",
        "site": "main",
        "incomplete": ["ticket"],
    }


# --- sites -----------------------------------------------------------------------------------


def test_keys_of_different_sites_are_mixed_sites_and_site_forces_one(
    env: Env, fake: FakeProvider, fake_factory: Any
) -> None:
    other = fake_factory(None)
    other.seed()
    env.write(
        "sites:\n"
        + env.site(fake, "one", "projects: [DEMO]")
        + env.site(other, "two", "projects: [ZZNOPE]")
    )
    other.add_ticket("ZZNOPE-1", "other site ticket")
    code, out, _ = env.run("--json", "get", "DEMO-1", "ZZNOPE-1")
    err = json.loads(out)["error"]
    assert (code, err["reason"]) == (22, "mixed_sites")
    assert fake.calls == [] and other.calls == []
    code, doc, _ = env.get("DEMO-1", "ZZNOPE-1", "--site", "two")
    # Forced onto site two, DEMO-1 exists there too (the seed); ZZNOPE-1 is read there as well.
    assert code == 0 and {t["site"] for t in doc["tickets"]} == {"two"}
    assert fake.calls == []


def test_an_unknown_site_is_exit_22(env: Env, site: FakeProvider) -> None:
    code, out, _ = env.run("--json", "get", "DEMO-1", "--site", "nope")
    assert (code, json.loads(out)["error"]["reason"]) == (22, "unknown_site")


# --- comments --------------------------------------------------------------------------------


def test_comments_reads_the_comment_endpoint_and_not_the_embedded_list(
    env: Env, site: FakeProvider
) -> None:
    for n in range(25):
        add_comment(site, "DEMO-2", str(1000 + n), f"c{n}", n)
    code, doc, _ = env.get("DEMO-2", "--comments", "22", "--fields", "key,comments")
    assert code == 0
    c = doc["tickets"][0]["comments"]
    assert c["total"] == 25 and c["truncated"] is True and len(c["items"]) == 22
    bodies = [i["body"] for i in c["items"]]
    assert bodies[0] == "c3" and bodies[-1] == "c24"  # the latest 22, oldest first
    issue = [x for x in site.calls if x.path == "/rest/api/3/issue/DEMO-2"]
    assert "comment" not in (issue[0].one("fields") or "").split(",")
    reads = [x for x in site.calls if x.path.endswith("/DEMO-2/comment")]
    assert len(reads) == 1 and reads[0].one("orderBy") == "-created"
    assert reads[0].one("maxResults") == "22"


def test_comments_costs_one_extra_call_per_ticket(env: Env, site: FakeProvider) -> None:
    env.get("DEMO-1", "DEMO-2", "--comments", "3", "--fields", "key,comments")
    reads = [x for x in site.calls if x.path.endswith("/comment")]
    assert sorted(x.path for x in reads) == [
        "/rest/api/3/issue/DEMO-1/comment",
        "/rest/api/3/issue/DEMO-2/comment",
    ]


@pytest.mark.parametrize("n", ["0", "51", "1000", "-1", "-50", "99999999999999999999999999"])
def test_comments_outside_one_to_fifty_is_exit_22(env: Env, site: FakeProvider, n: str) -> None:
    code, out, _ = env.run("--json", "get", "DEMO-1", "--comments", n)
    assert (code, json.loads(out)["error"]["reason"]) == (22, "invalid_value")
    assert site.calls == []


def test_comments_that_is_not_a_number_is_a_usage_error(env: Env, site: FakeProvider) -> None:
    code, _, _ = env.run("--json", "get", "DEMO-1", "--comments", "x")
    assert code == 2


def test_a_default_read_with_comments_adds_the_comments(env: Env, site: FakeProvider) -> None:
    add_comment(site, "DEMO-2", "2001", "hello", 0)
    code, doc, _ = env.get("DEMO-2", "--comments", "5")
    assert code == 0 and doc["tickets"][0]["comments"]["items"][0]["body"] == "hello"


def test_the_comment_object_marks_own_and_forged_properties(env: Env, site: FakeProvider) -> None:
    meta = {"v": 1, "actor_kind": "auto", "body_sha256": "a" * 64, "key_sha256": "b" * 64}
    add_comment(site, "DEMO-2", "3001", "mine, posted by the tool", 0, props={META: meta})
    add_comment(site, "DEMO-2", "3002", "mine, by hand", 1, edited=True)
    add_comment(site, "DEMO-2", "3003", "forged", 2, author=OTHER_ACCOUNT, props={META: meta})
    add_comment(site, "DEMO-2", "3004", "damaged", 3, props={META: {"v": 7, "actor_kind": "x"}})
    code, doc, _ = env.get("DEMO-2", "--comments", "10", "--fields", "key,comments")
    assert code == 0
    by_id = {i["id"]: i for i in doc["tickets"][0]["comments"]["items"]}
    tool = by_id["3001"]
    assert tool["own"] and tool["authored_by_ticketctl"] and tool["actor_kind"] == "auto"
    assert tool["body_sha256"] == "a" * 64 and tool["dedupe_key_sha256"] == "b" * 64
    assert tool["updated"] == tool["created"]
    hand = by_id["3002"]
    assert hand["own"] and not hand["authored_by_ticketctl"] and hand["actor_kind"] is None
    assert hand["updated"] != hand["created"]
    forged = by_id["3003"]
    assert not forged["own"] and not forged["authored_by_ticketctl"]
    assert forged["actor_kind"] is None and forged["body_sha256"] is None
    assert forged["dedupe_key_sha256"] is None
    damaged = by_id["3004"]
    assert damaged["authored_by_ticketctl"] and damaged["actor_kind"] is None
    assert damaged["body_sha256"] is None
    golden(site, "comments", doc)


def test_a_comment_with_an_unreadable_time_is_dropped_and_named(
    env: Env, site: FakeProvider
) -> None:
    add_comment(site, "DEMO-2", "4001", "ok", 0)
    add_comment(site, "DEMO-2", "4002", "bad", 1)
    site.tickets["DEMO-2"].comments[1]["_created"] = datetime(2026, 3, 1, tzinfo=UTC)
    real = site._comment_obj

    def mangled(c: dict[str, Any], *, props: bool) -> dict[str, Any]:
        out = real(c, props=props)
        if c["id"] == "4002":
            out["updated"] = "yesterday"
        return out

    site._comment_obj = mangled  # type: ignore[method-assign]
    code, doc, _ = env.get("DEMO-2", "--comments", "10", "--fields", "key,comments")
    t = doc["tickets"][0]
    assert code == 0 and [i["id"] for i in t["comments"]["items"]] == ["4001"]
    assert t["incomplete"] == ["comments"]


def test_a_comment_body_is_sanitized_and_fenced_text_only(env: Env, site: FakeProvider) -> None:
    add_comment(site, "DEMO-2", "5001", f"\x1b[2Jpwn ‮ {SITE_TOKEN}", 0)
    code, out, err = env.run("--json", "get", "DEMO-2", "--comments", "5")
    assert code == 0 and SITE_TOKEN not in out + err
    body = json.loads(out)["tickets"][0]["comments"]["items"][0]["body"]
    assert "\x1b" not in body and "‮" not in body


def test_a_comment_call_failure_is_a_per_key_error(env: Env, site: FakeProvider) -> None:
    site.inject(Fault("status", method="GET", path=r"/DEMO-1/comment$", status=403, times=None))
    code, doc, _ = env.get("DEMO-1", "DEMO-2", "--comments", "2")
    assert code == 0 and [t["key"] for t in doc["tickets"]] == ["DEMO-2"]
    assert doc["errors"][0]["key"] == "DEMO-1" and doc["errors"][0]["error"]["code"] == "forbidden"


# --- human output and secrets ----------------------------------------------------------------


def test_human_output_shows_tickets_incomplete_and_errors(env: Env, site: FakeProvider) -> None:
    code, out, _ = env.run("get", "DEMO-2", "DEMO-77")
    assert code == 0
    assert out.startswith("DEMO-2: Seeded task in progress")
    assert "incomplete: " in out and "error DEMO-77: not_found" in out
    assert not CUSTOM_ID.search(out)


@pytest.mark.parametrize("flags", [[], ["--verbose"], ["--json"], ["--verbose", "--json"]])
@pytest.mark.parametrize("debug", [False, True])
def test_the_token_never_reaches_any_output(
    env: Env, site: FakeProvider, flags: list[str], debug: bool
) -> None:
    if debug:
        env.mp.setenv("TICKETCTL_DEBUG", "1")
    basic = site.basic_header().removeprefix("Basic ")
    site.tickets["DEMO-1"].summary = f"echo {SITE_TOKEN} {basic}"
    add_comment(site, "DEMO-2", "6001", f"echo {SITE_TOKEN}", 0)
    argv = [*flags, "get", "DEMO-1", "DEMO-2", "DEMO-77", "--comments", "3"]
    code, out, err = env.run(*argv)
    logs = "".join(p.read_text() for p in env.state.rglob("*") if p.is_file())
    assert code == 0
    for text in (out, err, logs):
        assert SITE_TOKEN not in text and basic not in text
    site.inject(Fault("status", method="GET", path=r"/DEMO-1$", status=401, times=None))
    code, out, err = env.run(*[*flags, "get", "DEMO-1"])
    assert code == 12 and SITE_TOKEN not in out + err and basic not in out + err


def test_credentials_are_required(env: Env, site: FakeProvider) -> None:
    env.mp.delenv(TOKEN_VAR)
    code, out, _ = env.run("--json", "get", "DEMO-1")
    assert (code, json.loads(out)["error"]["code"]) == (12, "auth_required")
    assert site.calls == []


def test_a_write_only_flag_is_refused(env: Env, site: FakeProvider) -> None:
    code, _, _ = env.run("--json", "get", "DEMO-1", "--yes")
    assert code == 2


# --- review round 1 --------------------------------------------------------------------------


def test_fields_without_comments_ignores_comments_and_include_adf(
    env: Env, site: FakeProvider
) -> None:
    add_comment(site, "DEMO-2", "7001", "hello", 0)
    site.calls.clear()
    code, doc, _ = env.get("DEMO-2", "--fields", "key,title", "--comments", "5", "--include-adf")
    assert code == 0
    assert doc["tickets"][0] == {
        "key": "DEMO-2",
        "site": "main",
        "title": "Seeded task in progress",
    }
    assert [c for c in site.calls if c.path.endswith("/comment")] == []


def test_two_keys_of_one_moved_ticket_print_twice_and_the_comment_call_uses_the_new_key(
    env: Env, site: FakeProvider
) -> None:
    site.move("DEMO-1", "DEMO-40")
    del site.tickets["DEMO-3"]
    site.moved["DEMO-3"] = "DEMO-40"
    add_comment(site, "DEMO-40", "7101", "moved", 0)
    code, doc, _ = env.get("DEMO-1", "DEMO-3", "--comments", "2")
    assert code == 0
    assert [(t["key"], t["requested_key"]) for t in doc["tickets"]] == [
        ("DEMO-40", "DEMO-1"),
        ("DEMO-40", "DEMO-3"),
    ]
    reads = {c.path for c in site.calls if c.path.endswith("/comment")}
    assert reads == {"/rest/api/3/issue/DEMO-40/comment"}


def test_an_own_comment_without_a_property_list_is_treated_as_the_tools(
    env: Env, site: FakeProvider, monkeypatch: pytest.MonkeyPatch
) -> None:
    add_comment(site, "DEMO-2", "7201", "mine", 0)
    real = site._comment_obj

    def without(c: dict[str, Any], *, props: bool) -> dict[str, Any]:
        out = real(c, props=props)
        out.pop("properties", None)
        return out

    monkeypatch.setattr(site, "_comment_obj", without)
    code, doc, _ = env.get("DEMO-2", "--comments", "3", "--fields", "key,comments")
    t = doc["tickets"][0]
    (item,) = t["comments"]["items"]
    assert code == 0 and item["own"] and item["authored_by_ticketctl"] is True
    assert t["incomplete"] == ["comments"]


def test_an_unexpected_exception_is_one_keys_internal_error(
    env: Env, site: FakeProvider, monkeypatch: pytest.MonkeyPatch
) -> None:
    real = getcmd._Reader._read

    def boom(self: Any, key: str) -> Any:
        if key == "DEMO-2":
            raise RuntimeError(f"boom {SITE_TOKEN}")
        return real(self, key)

    monkeypatch.setattr(getcmd._Reader, "_read", boom)
    code, out, err = env.run("--json", "get", "DEMO-1", "DEMO-2", "DEMO-3")
    doc = json.loads(out)
    assert code == 0 and [t["key"] for t in doc["tickets"]] == ["DEMO-1", "DEMO-3"]
    (e,) = doc["errors"]
    assert e["key"] == "DEMO-2" and e["error"]["code"] == "internal"
    assert e["error"]["key"] == "DEMO-2" and e["error"]["site"] == "main"
    assert "boom" not in out + err and SITE_TOKEN not in out + err


def test_repeated_rejected_credentials_stop_the_remaining_reads(
    env: Env, site: FakeProvider
) -> None:
    keys = seed_many(site, 12)
    site.inject(Fault("status", method="GET", path=r"/issue/DEMO-1\d\d$", status=401, times=None))
    site.calls.clear()
    code, out, _ = env.run("--json", "get", *keys)
    err = json.loads(out)["error"]
    assert code == 12 and err["code"] == "bad_credentials"
    listed = err["details"]["errors"]
    assert [e["key"] for e in listed] == keys
    assert all(e["error"]["code"] == "bad_credentials" for e in listed)
    assert [e["error"]["key"] for e in listed] == keys
    assert len(issue_gets(site)) <= getcmd.CONCURRENCY


def test_a_stopped_reader_makes_no_request(env: Env, site: FakeProvider) -> None:
    env.mp.setattr(getcmd, "_read_all", getcmd._read_all)  # the real one, named for the reader
    reads: list[Any] = []
    real = getcmd._Reader._read

    def counting(self: Any, key: str) -> Any:
        reads.append(key)
        if key == "DEMO-100":
            raise KeyboardInterrupt
        assert self.stop.wait(10)
        return getcmd._Read(error=getcmd.TicketctlError("internal", "interrupted"))

    env.mp.setattr(getcmd._Reader, "_read", counting)
    keys = seed_many(site, 12)
    code, _, err = env.run("--json", "get", *keys)
    assert code == 130 and "interrupted" in err
    assert len(reads) < len(keys)
    assert real is not counting


def test_the_input_order_of_warnings_does_not_depend_on_the_threads(
    env: Env, site: FakeProvider, monkeypatch: pytest.MonkeyPatch
) -> None:
    keys = seed_many(site, 8)
    real = getcmd.normalize_issue

    def late(payload: Any, ctx: Any, *, requested_key: str) -> Any:
        time.sleep(0.01 * (9 - keys.index(requested_key)))  # the first key finishes last
        out = real(payload, ctx, requested_key=requested_key)
        assert out is not None
        return type(out)(out.ticket, (*out.warnings, f"note for {requested_key}"))

    monkeypatch.setattr(getcmd, "normalize_issue", late)
    code, _, err = env.run("--json", "get", *keys)
    assert code == 0
    assert re.findall(r"note for (DEMO-\d+)", err) == keys


def test_human_output_of_none_readable_lists_every_key(env: Env, site: FakeProvider) -> None:
    code, out, err = env.run("get", "DEMO-77", "DEMO-78")
    assert code == 10 and out == ""
    lines = [line for line in err.splitlines() if line.startswith("error ")]
    assert [line.split(":")[0] for line in lines] == ["error DEMO-77", "error DEMO-78"]
    assert all(re.search(r": not_found: ", line) for line in lines)


def test_get_help_describes_every_argument(capsys: Any) -> None:
    with pytest.raises(SystemExit):
        main(["get", "--help"])
    text = capsys.readouterr().out
    assert re.search(r"at most 50 distinct keys", text)
    for name in ("KEY", "--fields", "--comments", "--include-adf"):
        assert re.search(rf"{re.escape(name)}[^\n]*\n?[^\n]*\S", text)
    assert re.search(r"1 to 50", text)
