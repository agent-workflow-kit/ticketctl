"""The live write suite (issue #38): at most three titled test tickets, always closed as done.

READ THIS BEFORE RUNNING. The suite creates up to three tickets in the project named by
``TICKETCTL_LIVE_PROJECT`` and posts one comment on one of them; ``TICKETCTL_LIVE_MAX_TICKETS``
(1, 2 or 3) lowers that cap and ``TICKETCTL_LIVE_COMMENT_TEXT`` sets the comment text. A comment cannot be deleted; a
created ticket can only be closed. The suite refuses to run without ``TICKETCTL_LIVE=1`` and
``TICKETCTL_LIVE_WRITE=1`` and a named test project. Every ticket it created is closed as done with
the won't-do resolution by the module fixture's ``finally`` (``conftest.write_world``), also when a
test fails, the request cap aborts the run or the run is interrupted. It never touches a ticket it
did not create: the only tickets it writes are the keys a create returned (and, after a create
whose answer was lost, the tickets whose title carries this run's marker).

Each write is planned with ``--dry-run`` first (which must send no write), then applied with
``--yes --expect-plan``. Requests are counted from ticketctl's request log; the cap aborts the
suite. An assertion fails only when ticketctl breaks its documented interface; a provider behavior
that differs from the fake provider's assumption is recorded as a ``differs`` finding.
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any

import pytest

from tests.live.support import (
    ENV_MAX_TICKETS,
    Ledger,
    LiveCli,
    LiveEnv,
    Recorder,
    Result,
    explain,
    shape,
)

World = tuple[LiveEnv, LiveCli, Recorder, Ledger]
LAG_POLLS = (5.0,)  # extra waits (seconds) after the re-run's own lag guard


def need_ticket(ledger: Ledger, name: str) -> str:
    key = ledger.names.get(name)
    if not key:
        pytest.skip(f"test ticket {name} was not created (an earlier test failed or skipped)")
    return key


def dedupe_key(ledger: Ledger, label: str) -> str:
    return f"live-{ledger.marker}-{label}"


def create_args(live: LiveEnv, ledger: Ledger, label: str, *more: str) -> tuple[str, ...]:
    return (
        "create", "--project", live.project, "--type", live.issue_type,
        "--title", ledger.title(label), "--dedupe-key", dedupe_key(ledger, label), *more,
    )  # fmt: skip


def codes(doc: Any, key: str) -> list[Any]:
    items = doc.get(key) if isinstance(doc, dict) else None
    return [i.get("code", i.get("kind")) if isinstance(i, dict) else i for i in items or []]


def landed(ledger: Ledger, res: Result, name: str) -> str:
    """The key of a landed create, recorded in the ledger at once (the closer needs it)."""
    if res.code == 12 and res.error.get("outcome_unknown"):
        ledger.unknown_create = True
    assert res.code == 0 and isinstance(res.doc, dict), (res.code, res.error.get("code"))
    key = res.doc.get("key")
    assert isinstance(key, str), "a landed create printed no key"
    ledger.add(key, name)
    ledger.create_resolved()  # the key is recorded: the closer no longer needs the title sweep
    return key


def read_ticket(cli: LiveCli, key: str, *fields: str, cleanup: bool = False) -> dict[str, Any]:
    res = cli.run("get", key, "--fields", ",".join(fields), cleanup=cleanup)
    assert res.code == 0 and isinstance(res.doc, dict), (res.code, res.error.get("code"))
    (ticket,) = res.doc["tickets"]
    return ticket


# ---- create ------------------------------------------------------------------------------------


def test_create_with_dedupe_key_and_the_label_lag(write_world: World) -> None:
    live, cli, rec, ledger = write_world
    # One ticket serves the create, dedupe, unassigned, transition, update, comment and sprint
    # probes: every extra ticket would cost about ten more requests to create and as many to close.
    args = create_args(live, ledger, "A", "--assign", "none")
    plan, done = cli.apply(*args)
    if plan.code == 22 and plan.error.get("reason") == "missing_required_fields":
        rec.record("W03", "create dry run", {"exit": 22, "reason": "missing_required_fields"},
                   status="skipped")  # fmt: skip
        pytest.skip(
            "the project's create screen has required fields ticketctl cannot supply: "
            "set TICKETCTL_LIVE_TYPE to a simpler type or use another project"
        )
    if done is not None and done.code == 12 and done.error.get("code") == "rejected":
        # The site refused the explicit unassigned value: record it, create without.
        ledger.create_resolved()  # a refusal is a known outcome: nothing was created
        rec.differs("W02", "an explicit unassigned value in the create body",
                    {"exit": 12, "code": "rejected", "field_errors": sorted(
                        (done.error.get("details") or {}).get("field_errors") or {})},
                    expected="assignee: {accountId: null} is accepted")  # fmt: skip
        if ledger.create_launches >= ledger.cap:
            # the retry would be a second applied create: a cap of 1 allows only one
            rec.record(
                "W03",
                "create retry without the unassigned value",
                {
                    "ticket_cap": ledger.cap,
                    "reason": "the create was refused and the cap allows no retry",
                },
                status="skipped",
            )
            pytest.skip(
                f"the create was refused and the ticket cap is {ledger.cap}: no retry is allowed "
                f"(unset {ENV_MAX_TICKETS} or set it to 2 or 3 to run the retry)"
            )
        args = create_args(live, ledger, "A")
        plan, done = cli.apply(*args)
    assert done is not None, explain(rec.redactor, plan.code, plan.error)
    key = landed(ledger, done, "A")
    assert done.doc.get("reused") is False
    rec.record("W03", "create: createmeta read, plan with one create step, POST /issue gave a key",
               {"plan_steps": codes(plan.doc, "steps"), "plan_requests": plan.requests,
                "apply_requests": done.requests, "http_statuses": done.statuses})  # fmt: skip
    # The re-run has no --yes: if the lookup missed, it stops at the confirmation, never creates.
    again = cli.run(*args)
    facts: dict[str, Any] = {
        "first_rerun_reused": again.code == 0 and bool(again.doc.get("reused")),
        "first_rerun_seconds": round(again.seconds, 1),
        "first_rerun_requests": again.requests,
    }
    waited = 0.0
    for wait in LAG_POLLS:
        if facts.get("found_after_seconds") is not None or facts["first_rerun_reused"]:
            break
        time.sleep(wait)
        waited += wait
        poll = cli.run(*args)
        if poll.code == 0 and poll.doc.get("reused"):
            facts["found_after_seconds"] = round(waited, 1)
            again = poll
    if facts["first_rerun_reused"]:
        facts["found_after_seconds"] = 0.0
    if again.code == 0:
        assert again.doc.get("key") == key, "a reused create named another ticket"
    rec.record(
        "W01", "the label index lag: how long a created ticket stayed invisible to the dedupe lookup",
        facts,
        status="observed" if facts.get("found_after_seconds") is not None else "differs",
        expected="the ticket is found within 2 s of a quick re-run (the lag guard of D-36)",
    )  # fmt: skip


def test_create_explicit_unassigned(write_world: World) -> None:
    live, cli, rec, ledger = write_world
    key = need_ticket(ledger, "A")
    ticket = read_ticket(cli, key, "assignee")
    unassigned = ticket.get("assignee") is None
    rec.record(
        "W02", "the ticket created with --assign none, read back before anything else touches it",
        {"assignee_is_null": unassigned},
        status="observed" if unassigned else "differs",
        expected="assignee: {accountId: null} leaves the ticket unassigned",
    )  # fmt: skip


# ---- transition, update, comment ---------------------------------------------------------------


def test_transition_with_assign(write_world: World) -> None:
    live, cli, rec, ledger = write_world
    key = need_ticket(ledger, "A")
    listing = cli.run("transitions", key)
    assert listing.code == 0, explain(rec.redactor, listing.error)
    options = [
        t for t in listing.doc["transitions"]
        if t["to"]["category"] != "done" and not t["required_fields"]
    ]  # fmt: skip
    if not options:
        pytest.skip("no transition to a non-done status without required fields on this workflow")
    target = options[0]["to"]["name"]
    args = ("transition", key, "--to", target, "--assign", "me")
    plan, done = cli.apply(*args)
    assert done is not None and done.code == 0, explain(
        rec.redactor, plan.code, plan.error, done and done.error
    )
    ticket = read_ticket(cli, key, "status", "assignee")
    rerun = cli.run("--dry-run", *args)
    rec.record(
        "W19", "transition --to <status> --assign me, then the same plan again",
        {
            "plan_steps": codes(plan.doc, "steps"),
            "plan_warnings": codes(plan.doc, "warnings"),
            "result": done.doc.get("result"),
            "status_is_target": (ticket.get("status") or {}).get("name") == target,
            "assigned": ticket.get("assignee") is not None,
            "rerun_steps": codes(rerun.doc, "steps"),
            "requests": plan.requests + done.requests,
        },
    )  # fmt: skip
    assert codes(rerun.doc, "steps") == [], "a repeated transition still had steps"


def test_update_title_labels_order(write_world: World) -> None:
    live, cli, rec, ledger = write_world
    key = need_ticket(ledger, "A")
    one, two, three = (f"live-{ledger.marker}-{n}" for n in ("one", "two", "three"))
    _, first = cli.apply("update", key, "--add-label", one, "--add-label", two)
    assert first is not None and first.code == 0, explain(rec.redactor, first and first.error)
    _, second = cli.apply(
        "update", key, "--add-label", three, "--remove-label", one,
        "--title", ledger.title("A renamed"),
    )  # fmt: skip
    assert second is not None and second.code == 0, explain(rec.redactor, second and second.error)
    ticket = read_ticket(cli, key, "labels", "title")
    mine = sorted(label.replace(ledger.marker, "<run>") for label in ticket.get("labels") or []
                  if ledger.marker in label)  # fmt: skip
    expected = [f"live-<run>-{n}" for n in ("three", "two")]
    rec.record(
        "W06",
        "one update that adds a label and removes another (the same label in both is refused by "
        "ticketctl, so the order for one label is not probed)",
        {"labels_of_this_run": mine, "title_changed": ledger.title("A renamed") == ticket.get("title"),
         "result": second.doc.get("result")},
        status="observed" if mine == expected else "differs",
        expected="the removed label is gone, the added one present, the title changed",
    )  # fmt: skip
    assert "labels" in ticket, "get --fields labels printed no labels property"


def test_update_description_is_unchanged_on_rerun(write_world: World, tmp_path: Path) -> None:
    live, cli, rec, ledger = write_world
    key = need_ticket(ledger, "A")
    text = tmp_path / "description.md"
    text.write_text("A paragraph.\n\n- first item\n- second item\n\nAnother paragraph.\n")
    args = ("update", key, "--set", f"description=@{text}")
    pre = ("--actor", "human:live-suite")  # rich text needs a human actor
    _, done = cli.apply(*args, pre=pre)
    assert done is not None and done.code == 0, explain(rec.redactor, done and done.error)
    again = cli.run(*pre, "--dry-run", *args)
    spurious = bool(codes(again.doc, "steps"))
    rec.record(
        "W07", "the unchanged check after a rich-text write (the provider may add ids to the ADF)",
        {"second_plan_steps": codes(again.doc, "steps"), "spurious_update": spurious},
        status="differs" if spurious else "observed",
        expected="the same description is unchanged: no steps",
    )  # fmt: skip


def test_comment_with_key(write_world: World, tmp_path: Path) -> None:
    live, cli, rec, ledger = write_world
    key = need_ticket(ledger, "A")
    assert ledger.may_comment(key)
    ledger.note_comment(key)  # counted before the post: one per ticket, ever
    # The one comment of the ticket doubles as the dedupe-key probe. The text goes through a body
    # file (never a shell, never an argument that could read as an option) and is not recorded.
    body = tmp_path / "comment.md"
    body.write_text(live.comment_text + "\n", encoding="utf-8")
    args = ("comment", key, "--body-file", str(body), "--dedupe-key", dedupe_key(ledger, "comment"))
    plan, done = cli.apply(*args)
    assert done is not None and done.code == 0, explain(
        rec.redactor, plan.code, plan.error, done and done.error
    )
    assert done.doc.get("comment_id"), "a landed comment printed no id"
    rerun = cli.run("--dry-run", *args)  # a dry run: a lookup that missed could never post twice
    mine = cli.run("comments", key, "--own", "--limit", "5")
    assert mine.code == 0, explain(rec.redactor, mine.error)
    items = mine.doc.get("items") or []
    rec.record(
        "W10", "a keyed comment: property accepted on create, read back, found again by its key",
        {
            "result": done.doc.get("result"),
            "comment_chars": len(live.comment_text),
            "rerun_steps": codes(rerun.doc, "steps"),
            "rerun_dedupe": shape(rerun.doc.get("dedupe_match")),
            "own_comments_listed": len(items),
            "authored_by_ticketctl": [bool(c.get("authored_by_ticketctl")) for c in items],
            "requests": plan.requests + done.requests,
        },
        status="observed" if len(items) == 1 and items[0].get("authored_by_ticketctl") else "differs",
        expected="the comment is listed at once (read-your-writes) with its ticketctl.meta property",
    )  # fmt: skip
    assert codes(rerun.doc, "steps") == [], "a repeated keyed comment still had steps"


# ---- sprints -----------------------------------------------------------------------------------


def _sprints(cli: LiveCli, rec: Recorder, board: str, state: str) -> list[dict[str, Any]]:
    res = cli.run("sprints", "list", "--board", board, "--state", state, "--limit", "5")
    assert res.code == 0, explain(rec.redactor, state, res.error)
    return res.doc["sprints"]


def _member_ids(cli: LiveCli, key: str) -> list[Any]:
    ticket = read_ticket(cli, key, "sprints")
    return [s.get("id") for s in ticket.get("sprints") or []]


def test_sprint_add_move_and_remove(write_world: World) -> None:
    live, cli, rec, ledger = write_world
    board = live.board
    if not board:
        pytest.skip("TICKETCTL_LIVE_BOARD is not set: no sprint probe")
    key = need_ticket(ledger, "A")
    future = _sprints(cli, rec, board, "future")
    if not future:
        pytest.skip("the board has no future sprint: create one and run again")
    first = future[0]["id"]
    facts: dict[str, Any] = {}
    added = False
    try:
        _, done = cli.apply("sprint", "add", key, "--sprint", str(first), "--board", board)
        assert done is not None and done.code == 0, explain(rec.redactor, done and done.error)
        added = True
        facts["after_add"] = [i == first for i in _member_ids(cli, key)]
        again = cli.run("--dry-run", "sprint", "add", key, "--sprint", str(first), "--board", board)
        facts["add_again_steps"] = codes(again.doc, "steps")
        assert facts["add_again_steps"] == []
        if len(future) >= 2:
            second = future[1]["id"]
            plan, moved = cli.apply("sprint", "add", key, "--sprint", str(second), "--board", board)
            assert moved is not None and moved.code == 0, explain(
                rec.redactor, moved and moved.error
            )
            members = _member_ids(cli, key)
            facts["second_add"] = {
                "plan_warnings": codes(plan.doc, "warnings"),
                "member_count": len(members),
                "only_the_second": members == [second],
            }
            rec.record(
                "W12", "adding a ticket that is in a future sprint to another one",
                facts["second_add"],
                status="observed" if members == [second] else "differs",
                expected="the ticket is moved: it ends in the second sprint only",
            )  # fmt: skip
        else:
            rec.record(
                "W12", "move versus append", {"future_sprints": len(future)}, status="skipped"
            )
        _, removed = cli.apply("sprint", "remove", key)
        assert removed is not None and removed.code == 0, explain(
            rec.redactor, removed and removed.error
        )
        added = False
        left = _member_ids(cli, key)
        facts["after_remove"] = len(left)
        rec.record(
            "W13", "sprint remove sends the ticket to the backlog", {"sprints_left": len(left)},
            status="observed" if not left else "differs",
            expected="no open or future sprint is left",
        )  # fmt: skip
    finally:
        if added:  # a failed step: put the ticket back in the backlog (best effort)
            cli.run("--yes", "--expect-plan", _plan_hash(cli, key), "sprint", "remove", key)


def _plan_hash(cli: LiveCli, key: str) -> str:
    plan = cli.run("--dry-run", "sprint", "remove", key)
    digest = plan.doc.get("plan_sha256") if isinstance(plan.doc, dict) else None
    return digest if isinstance(digest, str) else "0" * 64


def test_sprint_errors(write_world: World) -> None:
    live, cli, rec, ledger = write_world
    board = live.board
    if not board:
        pytest.skip("TICKETCTL_LIVE_BOARD is not set: no sprint probe")
    key = need_ticket(ledger, "A")
    facts: dict[str, Any] = {}
    bogus = cli.run("--dry-run", "sprint", "add", key, "--sprint", "999999999", "--board", board)
    facts["unknown_sprint_plan"] = {"exit": bogus.code, "code": bogus.error.get("code"),
                                    "http_statuses": bogus.statuses}  # fmt: skip
    if bogus.code == 0 and isinstance(bogus.doc, dict):
        plan, done = cli.apply("sprint", "add", key, "--sprint", "999999999", "--board", board)
        assert done is not None
        facts["unknown_sprint_write"] = {"exit": done.code, "code": done.error.get("code"),
                                         "http_statuses": done.statuses}  # fmt: skip
        assert done.code != 0, "a write to an unknown sprint landed"
    closed = _sprints(cli, rec, board, "closed")
    if closed:
        res = cli.run("--dry-run", "sprint", "add", key, "--sprint", str(closed[0]["id"]),
                      "--board", board)  # fmt: skip
        facts["closed_sprint_plan"] = {"exit": res.code, "code": res.error.get("code")}
        assert (res.code, res.error.get("code")) == (20, "sprint_closed"), facts
    rec.record("W15", "what the provider says to a bad sprint write, and a closed target", facts)


# ---- the closer --------------------------------------------------------------------------------


def test_every_created_ticket_is_closed(write_world: World) -> None:
    live, cli, rec, ledger = write_world
    if not ledger.created and not ledger.unknown_create:
        # nothing was created and no create is in doubt (a refusal at a cap of 1, or a create
        # screen ticketctl cannot fill): there is nothing to close or to check
        reason = "no ticket was created: nothing was probed (see the skipped W03 finding)"
        rec.record("W20", "the closer check", {"tickets": 0}, status="skipped")
        pytest.skip(reason)
    outcome = ledger.close_all()
    facts: dict[str, Any] = {"closed_outcomes": sorted(outcome.values()), "tickets": len(outcome)}
    states = []
    for key in ledger.created:
        ticket = read_ticket(cli, key, "status", "resolution", cleanup=True)  # verifies the closer
        resolution = ticket.get("resolution")
        states.append({
            "category": (ticket.get("status") or {}).get("category"),
            "resolution": (resolution or {}).get("name") if isinstance(resolution, dict) else resolution,
        })  # fmt: skip
    facts["final_states"] = states
    # one comment per ticket, the one the suite posted: read every ticket back (the posted ones)
    counts: dict[str, int] = {}
    for key, posted in ledger.comments.items():
        if posted and key in ledger.created:
            res = cli.run("comments", key, "--limit", "5", cleanup=True)
            assert res.code == 0, explain(rec.redactor, res.error)
            counts[key] = len(res.doc.get("items") or [])
    facts["comments_per_posted_ticket"] = sorted(counts.values())
    wrong = {k: n for k, n in counts.items() if n != 1}
    assert not wrong, explain(rec.redactor, "a ticket does not hold exactly one comment", wrong)
    rec.record("W20", "every test ticket ends done with the won't-do resolution", facts)
    assert ledger.still_open() == [], "tickets are still open: close them by hand"
    assert all(s["category"] == "done" for s in states), states
