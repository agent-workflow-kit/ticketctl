"""The live read suite (issue #37): observes the read-side facts still unverified, writes nothing.

Run it by hand against your own site (``docs/live-verification.md``); it is skipped without
``TICKETCTL_LIVE=1`` and never runs in CI. Every probe drives the ``ticketctl`` command line as a
subprocess, counts its requests from ticketctl's own request log and records what it saw in a
findings file (git-ignored). An assertion fails only when ticketctl breaks its documented
interface; a provider behavior that differs from what the fake provider assumes is recorded as a
``differs`` finding, never as a failure, and never claimed as verified.

No site, project, key, board or account is named here: the targets come from the environment
(``TICKETCTL_LIVE_*``) and from the user's own ticketctl configuration.
"""

from __future__ import annotations

import base64
import json
from typing import Any

import pytest

from tests.live.support import LiveCli, LiveEnv, Recorder, Result, shape

World = tuple[LiveEnv, LiveCli, Recorder]
DONE_AT_MOST = 6  # a bound on how many tickets a probe prints back, to keep findings small


def need(value: str, name: str, what: str = "") -> str:
    if not value:
        pytest.skip(f"{name} is not set{(': ' + what) if what else ''}")
    return value


def project_of(live: LiveEnv) -> str:
    return need(live.project_key(), "TICKETCTL_LIVE_PROJECT or TICKETCTL_LIVE_TICKET", "a project")


def error_facts(res: Result) -> dict[str, Any]:
    err = res.error
    return {
        "exit": res.code,
        "code": err.get("code"),
        "reason": err.get("reason"),
        "retryable": err.get("retryable"),
        "http_statuses": res.statuses,
        "message_chars": len(str(err.get("message", ""))),
        "requests": res.requests,
    }


def ok(res: Result) -> dict[str, Any]:
    assert res.code == 0, (res.argv[:2], res.code, res.error.get("code"), res.stderr[-300:])
    assert isinstance(res.doc, dict), res.stdout[:200]
    return res.doc


def tickets_of(doc: dict[str, Any]) -> list[dict[str, Any]]:
    items = doc.get("tickets")
    assert isinstance(items, list)
    return [t for t in items if isinstance(t, dict)]


# ---- identity and credentials ------------------------------------------------------------------


def test_whoami_shape(read_world: World) -> None:
    live, cli, rec = read_world
    res = cli.run("whoami", "--refresh")
    doc = ok(res)
    assert isinstance(doc.get("account_id"), str) and isinstance(doc.get("display_name"), str)
    assert doc.get("source") == "provider"
    rec.record("L17", "whoami: GET /myself answered 200 with usable identity fields",
               {"shape": shape(doc), "http_statuses": res.statuses})  # fmt: skip


def test_wrong_token_is_a_401(read_world: World) -> None:
    live, cli, rec = read_world
    name = need(live.token_env, "TICKETCTL_LIVE_TOKEN_ENV", "the name of the token variable")
    res = cli.run("whoami", "--refresh", extra_env={name: "not-a-real-token-0000"})
    assert res.code == 12 and res.error.get("code") == "bad_credentials", res.error
    facts = error_facts(res)
    status = "observed" if facts["http_statuses"] == [401] else "differs"
    rec.record("L18", "a wrong token", facts, status=status, expected="HTTP 401, bad_credentials")


# ---- get ---------------------------------------------------------------------------------------


def test_get_default_shape(read_world: World) -> None:
    live, cli, rec = read_world
    key = need(live.ticket, "TICKETCTL_LIVE_TICKET")
    res = cli.run("get", key, "--comments", "5")
    doc = ok(res)
    (ticket,) = tickets_of(doc)
    sprints = ticket.get("sprints") or []
    status = ticket.get("status") or {}
    rec.record(
        "L26", "get: the normalized ticket and what could not be read",
        {
            "shape": shape(ticket),
            "incomplete": ticket.get("incomplete"),
            "status_category": status.get("category") if isinstance(status, dict) else None,
            "sprint_states": sorted({str(s.get("state")) for s in sprints if isinstance(s, dict)}),
            "has_parent": ticket.get("parent") is not None,
            "has_epic": ticket.get("epic") is not None,
            "link_count": len(ticket.get("links") or []),
            "attachment_keys": sorted(
                {k for a in ticket.get("attachments") or [] for k in a} if ticket.get("attachments") else []
            ),
            "comment_count_printed": len(ticket.get("comments") or []),
            "requests": res.requests,
        },
    )  # fmt: skip


def test_get_fields_summary_only(read_world: World) -> None:
    live, cli, rec = read_world
    key = need(live.ticket, "TICKETCTL_LIVE_TICKET")
    res = cli.run("get", key, "--fields", "title")
    doc = ok(res)
    (ticket,) = tickets_of(doc)
    assert "title" in ticket
    rec.record("L26", "get --fields title: a title-only read is valid",
               {"printed_properties": sorted(ticket), "incomplete": ticket.get("incomplete")})  # fmt: skip


def test_get_missing_and_hidden_status(read_world: World) -> None:
    live, cli, rec = read_world
    project = project_of(live)
    missing = live.missing_ticket or f"{project}-999999999"
    miss = cli.run("get", missing)
    assert miss.code != 0 and miss.error, miss.stdout[:200]
    rec.record("L01", "a key that does not exist", {"key_given": bool(live.missing_ticket),
                                                    **error_facts(miss)})  # fmt: skip
    hidden = need(live.hidden_ticket, "TICKETCTL_LIVE_HIDDEN_TICKET", "a key you cannot see")
    hid = cli.run("get", hidden)
    assert hid.code != 0 and hid.error, hid.stdout[:200]
    same = (miss.code, miss.error.get("code"), miss.statuses) == (
        hid.code, hid.error.get("code"), hid.statuses)  # fmt: skip
    rec.record(
        "L01",
        "a hidden key against a missing key",
        {"hidden": error_facts(hid), "same_as_missing": same},
        status="observed" if same and hid.error.get("code") == "not_found" else "differs",
        expected="both answer 404 and not_found, so a consumer treats a hidden ticket as gone",
    )


def test_get_moved_ticket(read_world: World) -> None:
    live, cli, rec = read_world
    old = need(live.moved_ticket, "TICKETCTL_LIVE_MOVED_TICKET", "an old key of a moved ticket")
    res = cli.run("get", old, "--fields", "title")
    doc = ok(res)
    (ticket,) = tickets_of(doc)
    rec.record(
        "L02", "a moved ticket's old key",
        {
            "canonical_differs": ticket.get("key") != old,
            "requested_key_kept": ticket.get("requested_key") == old,
            "incomplete": ticket.get("incomplete"),
        },
        expected="the canonical key is printed and requested_key holds the typed key",
        status="observed" if ticket.get("key") != old and ticket.get("requested_key") == old else "differs",
    )  # fmt: skip


# ---- search ------------------------------------------------------------------------------------


def test_search_semantic_jql_is_caught_by_parse(read_world: World) -> None:
    live, cli, rec = read_world
    project = project_of(live)
    jql = f'project = "{project}" AND status = "ticketctl-no-such-status-0000"'
    res = cli.run("search", "--jql", jql, "--limit", "1")
    facts = error_facts(res)
    facts["answered_empty"] = res.code == 0 and not tickets_of(res.doc or {})
    assert res.code == 22 or res.code == 0, facts  # invalid_query, or an empty 200 (the finding)
    rec.record(
        "L14", "a semantic JQL error (an unknown status value)", facts,
        status="observed" if res.code == 22 else "differs",
        expected="the strict parse check reports it: exit 22 invalid_query, never an empty 200",
    )  # fmt: skip


@pytest.fixture(scope="module")
def big_search(read_world: World) -> dict[str, Any]:
    """One two-page search over the probe project, shared by the paging and priority probes."""
    live, cli, rec = read_world
    project = project_of(live)
    res = cli.run("search", "--project", project, "--order-by", "updated:desc", "--limit", "150",
                  "--fields", "title,priority,status,updated")  # fmt: skip
    doc = ok(res)
    return {"doc": doc, "requests": res.requests, "tickets": tickets_of(doc)}


def test_search_multi_page_cursor(read_world: World, big_search: dict[str, Any]) -> None:
    live, cli, rec = read_world
    project = project_of(live)
    tickets = big_search["tickets"]
    if len(tickets) < 101:
        rec.record("L10", "multi-page search", {"tickets_returned": len(tickets)}, status="skipped")
        pytest.skip("fewer than 101 visible tickets in the probe project: one page only")
    doc = big_search["doc"]
    keys = [t["key"] for t in tickets]
    assert len(keys) == len(set(keys))
    nxt = doc.get("next")
    rec.record(
        "L10", "a two-page search: 100 per page, cursor handed out",
        {"returned": len(tickets), "next_present": nxt is not None, "requests": big_search["requests"],
         "truncated": doc.get("truncated"), "count_key": "count" in doc},
    )  # fmt: skip
    if nxt is None:
        pytest.skip("the project has no ticket beyond the first 150: no cursor to resume")
    res = cli.run("search", "--project", project, "--order-by", "updated:desc", "--limit", "5",
                  "--fields", "title", "--cursor", nxt)  # fmt: skip
    more = ok(res)
    again = {t["key"] for t in tickets_of(more)}
    assert not (again & set(keys)), "a resumed page repeated a ticket"
    rec.record("L10", "the cursor resumes after the first run (the token stays valid)",
               {"resumed_returned": len(again), "requests": res.requests})  # fmt: skip


def _tamper(cursor: str) -> str:
    """The documented cursor with another page token (format: interface.md, search)."""
    raw = base64.urlsafe_b64decode(cursor + "=" * (-len(cursor) % 4))
    doc = json.loads(raw)
    doc["page_token"] = "ticketctl-not-a-real-page-token"
    text = json.dumps(doc, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return base64.urlsafe_b64encode(text.encode()).decode().rstrip("=")


def test_search_rejected_cursor(read_world: World, big_search: dict[str, Any]) -> None:
    live, cli, rec = read_world
    project = project_of(live)
    nxt = big_search["doc"].get("next")
    if nxt is None:
        pytest.skip("no cursor was handed out (the project has 150 tickets or fewer)")
    res = cli.run("search", "--project", project, "--order-by", "updated:desc", "--limit", "5",
                  "--fields", "title", "--cursor", _tamper(nxt))  # fmt: skip
    facts = error_facts(res)
    assert res.code == 22, facts
    rec.record("L11", "a page token the provider did not issue", facts,
               status="observed" if res.error.get("code") == "cursor_expired" else "differs",
               expected="HTTP 400 on the page, mapped to exit 22 cursor_expired")  # fmt: skip


def test_search_count_with_order_by(read_world: World) -> None:
    live, cli, rec = read_world
    project = project_of(live)
    res = cli.run("search", "--project", project, "--order-by", "updated:desc", "--count",
                  "--limit", "0")  # fmt: skip
    doc = ok(res)
    assert isinstance(doc.get("count"), int)
    rec.record("L13", "approximate-count with an ORDER BY tail",
               {"count_is_int": True, "requests": res.requests, "http_statuses": res.statuses})  # fmt: skip


def test_incremental_refresh_combination(read_world: World) -> None:
    live, cli, rec = read_world
    project = project_of(live)
    base = ("search", "--project", project, "--updated-since", "3650d", "--order-by",
            "updated:asc", "--limit", "3", "--fields", "title,updated")  # fmt: skip
    first = ok(cli.run(*base))
    got = tickets_of(first)
    stamps = [str(t.get("updated")) for t in got]
    nxt = first.get("next")
    facts: dict[str, Any] = {"first_count": len(got), "ascending": stamps == sorted(stamps),
                             "next_present": nxt is not None}  # fmt: skip
    assert facts["ascending"], "an updated:asc search was not ascending"
    if nxt is not None:
        second = ok(cli.run(*base, "--cursor", nxt))
        facts["second_count"] = len(tickets_of(second))
        facts["no_repeat"] = not ({t["key"] for t in got} & {t["key"] for t in tickets_of(second)})
        assert facts["no_repeat"]
    rec.record("L08", "named-query style refresh: --updated-since, updated:asc and --cursor", facts)


SAMPLES = (
    ("phrase", ["--text", 'quoted "phrase" with a back\\slash']),
    ("quote", ["--text", "it's a single quote"]),
    ("raw-single-quote", ["--jql", "summary ~ 'ticketctl'"]),
)


def test_text_and_scanner_samples(read_world: World) -> None:
    live, cli, rec = read_world
    project = project_of(live)
    seen: dict[str, Any] = {}
    for name, flags in SAMPLES:
        args = ["search", "--limit", "1", "--fields", "title", *flags]
        if flags[0] == "--text":
            args += ["--project", project]
        res = cli.run(*args)
        seen[name] = {"exit": res.code, "code": res.error.get("code"), "requests": res.requests}
        assert res.code in (0, 22), (name, seen[name])
    rec.record(
        "L06", "--text phrases and raw quoting accepted or refused (0 accepted, 22 refused)", seen,
        expected="a phrase with quotes and a backslash compiles and searches; a refusal is "
        "ticketctl's scanner or Jira's parse, told apart by the code",
    )  # fmt: skip
    rec.record("L07", "backslash and single-quote handling", seen)


def test_status_category_names(read_world: World) -> None:
    live, cli, rec = read_world
    project = project_of(live)
    seen: dict[str, Any] = {}
    for category in ("new", "indeterminate", "done"):
        res = cli.run("search", "--project", project, "--status-category", category,
                      "--limit", "1", "--fields", "status")  # fmt: skip
        got = tickets_of(res.doc) if res.code == 0 and isinstance(res.doc, dict) else []
        seen[category] = {
            "exit": res.code,
            "code": res.error.get("code"),
            "result_status_categories": sorted(
                {t["status"].get("category") for t in got if isinstance(t.get("status"), dict)}
            ),
        }
        assert res.code == 0, (category, seen[category])
    rec.record("L04", "--status-category compiles to names Jira accepts (no parse error)", seen,
               expected="new=To Do, indeterminate=In Progress, done=Done are valid category names")  # fmt: skip


def test_priority_order(read_world: World, big_search: dict[str, Any]) -> None:
    live, cli, rec = read_world
    pairs = {
        (t["priority"].get("name"), t["priority"].get("rank"))
        for t in big_search["tickets"]
        if isinstance(t.get("priority"), dict)
    }
    ordered = [name for name, _ in sorted(pairs, key=lambda p: (p[1] is None, p[1]))]
    ranks = [r for _, r in sorted(pairs, key=lambda p: (p[1] is None, p[1]))]
    rec.record(
        "L05", "the priority names seen, in the provider's rank order (highest first expected)",
        {"names_in_rank_order": ordered, "ranks": ranks},
        expected="the provider's /priority list is in rank order, highest first",
    )  # fmt: skip
    if len(ordered) < 2:
        pytest.skip("fewer than two distinct priorities among the sampled tickets")
    assert len(set(ranks)) == len(ranks), "two priorities share a rank"


# ---- comments, transitions, sprints, fields ----------------------------------------------------


def test_comments_shape(read_world: World) -> None:
    live, cli, rec = read_world
    key = need(live.ticket, "TICKETCTL_LIVE_TICKET")
    res = cli.run("comments", key, "--limit", "20", "--newest-first")
    doc = ok(res)
    items = doc.get("items")
    assert isinstance(items, list) and isinstance(doc.get("total"), int)
    created = [str(c.get("created")) for c in items if isinstance(c, dict)]
    rec.record(
        "L27", "comments: list shape, order and the properties of own comments",
        {
            "total_is_int": True,
            "returned": len(items),
            "newest_first_is_descending": created == sorted(created, reverse=True),
            "own": sum(1 for c in items if c.get("own")),
            "own_with_ticketctl_meta": sum(1 for c in items if c.get("authored_by_ticketctl")),
            "truncated": doc.get("truncated"),
            "requests": res.requests,
        },
    )  # fmt: skip


def test_transitions_shape(read_world: World) -> None:
    live, cli, rec = read_world
    key = need(live.ticket, "TICKETCTL_LIVE_TICKET")
    res = cli.run("transitions", key)
    doc = ok(res)
    items = doc.get("transitions")
    assert isinstance(items, list)
    rec.record(
        "L28", "transitions: categories, screens and the kinds of required fields",
        {
            "count": len(items),
            "categories": sorted({t["to"]["category"] for t in items}),
            "has_screen_values": sorted({str(t.get("has_screen")) for t in items}),
            "required_kinds": sorted({f["kind"] for t in items for f in t["required_fields"]}),
            "stderr_warning": bool(res.stderr.strip()),
        },
    )  # fmt: skip


def test_sprints_list_and_show(read_world: World) -> None:
    live, cli, rec = read_world
    board = need(live.board, "TICKETCTL_LIVE_BOARD", "a scrum board id")
    res = cli.run("sprints", "list", "--board", board, "--limit", "20")
    doc = ok(res)
    sprints = doc.get("sprints")
    assert isinstance(sprints, list)
    facts: dict[str, Any] = {
        "count": len(sprints),
        "states": sorted({s.get("state") for s in sprints}),
        "shape": shape(sprints[0]) if sprints else None,
        "stderr_warning": bool(res.stderr.strip()),
    }
    for state in ("active", "future", "closed"):
        one = cli.run("sprints", "list", "--board", board, "--state", state, "--limit", "2")
        facts[f"state_{state}"] = {
            "exit": one.code,
            "count": len((one.doc or {}).get("sprints", [])),
        }
        assert one.code == 0, (state, one.error)
    if sprints:
        show = cli.run("sprints", "show", str(sprints[0]["id"]))
        facts["show_exit"] = show.code
        assert show.code == 0, show.error
    rec.record("L29", "sprints: shapes, one state per request, show by id", facts)


def test_board_errors(read_world: World) -> None:
    live, cli, rec = read_world
    missing = cli.run("sprints", "list", "--board", "999999999")
    facts: dict[str, Any] = {"missing_board": error_facts(missing)}
    assert missing.code in (10, 22), facts
    if live.kanban_board:
        kanban = cli.run("sprints", "list", "--board", live.kanban_board)
        facts["board_without_sprints"] = error_facts(kanban)
        assert kanban.code in (0, 22), facts
    rec.record("L30", "a missing board and (when named) a board without sprints", facts,
               expected="404 means a missing board (exit 10); 400 a board without sprints (exit 22)")  # fmt: skip


def test_fields_check_and_list(read_world: World) -> None:
    live, cli, rec = read_world
    check = cli.run("fields", "check")
    doc = ok(check)
    problems = doc.get("problems") or []
    listing = ok(cli.run("fields", "list"))
    fields = listing.get("fields") or []
    rec.record(
        "L25", "fields check and list: the mapping problems by kind and the list shape",
        {
            "problems": sorted({(p.get("logical"), p.get("problem")) for p in problems}),
            "field_count": len(fields),
            "entry_shape": shape(fields[0]) if fields else None,
            "mapping_keys": sorted(listing.get("mapping") or {}),
        },
    )  # fmt: skip


def test_validate_probe(read_world: World) -> None:
    live, cli, rec = read_world
    res = cli.run("validate", "--probe")
    doc = ok(res)
    sites = doc.get("sites") or []
    rec.record(
        "L16", "validate --probe: authenticates, checks fields and parses every named query",
        {
            "sites": len(sites),
            "queries": [
                {"valid": q.get("valid")} for s in sites for q in (s.get("queries") or [])
            ],
            "problems": sorted({str(p.get("problem")) for s in sites for p in s.get("problems") or []}),
            "requests": res.requests,
        },
    )  # fmt: skip


def test_passive_request_log(read_world: World) -> None:
    """Last: what the whole run's request log says about statuses and the rate-limit header."""
    live, cli, rec = read_world
    statuses = cli.statuses_by_kind()
    rec.record(
        "L20", "HTTP statuses and rate-limit header presence over the whole run",
        {"statuses": statuses, "rate_limit_remaining": cli.rate_limit_values(),
         "a_429_was_met": "429" in statuses},
        expected="no 429 in a polite run; the header is present on every answer (unknown)",
    )  # fmt: skip
