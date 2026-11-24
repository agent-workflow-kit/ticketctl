"""``link add`` and ``link remove`` end to end against the fake provider (issue #41)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from tests.fake_provider import SITE_TOKEN, FakeProvider, Fault
from tests.golden import assert_golden, validator
from tests.test_comment import ATTEMPT, SITE_ENV_VAR, Cli
from ticketctl import hooks, linkcmd
from ticketctl.errors import TicketctlError
from ticketctl.gateway import HookRun
from ticketctl.writeflow import Seams

KEY = "DEMO-2"
OTHER = "DEMO-3"
MARK = "zq7x-link-marker-4f9c2e"  # a ticket summary: must never reach a hook payload or a plan


@pytest.fixture
def cli(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: Any, fake: FakeProvider) -> Cli:
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    monkeypatch.setenv(SITE_ENV_VAR, SITE_TOKEN)
    monkeypatch.setenv("PATH", "/nonexistent")
    monkeypatch.setattr(linkcmd, "make_seams", lambda: Seams(attempt_id=lambda: ATTEMPT))
    return Cli(tmp_path, fake, capsys)


def args(kind: str = "Blocks", direction: str = "outward", other: str = OTHER) -> list[str]:
    return ["--type", kind, "--direction", direction, "--other", other]


def change(cli: Cli, verb: str, *argv: str, key: str = KEY) -> tuple[int, dict[str, Any]]:
    """Plan with --dry-run, then apply that plan with --yes."""
    code, plan = cli.doc("--dry-run", "link", verb, key, *argv)
    if code != 0:
        return code, plan
    return cli.doc("--yes", "--expect-plan", plan["plan_sha256"], "link", verb, key, *argv)


def links(cli: Cli, key: str = KEY) -> list[dict[str, Any]]:
    return cli.fake.tickets[key].links


def linked(cli: Cli) -> str:
    code, result = change(cli, "add", *args())
    assert code == 0, result
    return str(links(cli)[0]["id"])


# --- add ----------------------------------------------------------------------------------------


def test_add_creates_one_link_visible_on_both_tickets_with_the_documented_request(
    cli: Cli,
) -> None:
    before = len(cli.writes())
    code, result = change(cli, "add", *args())
    assert code == 0, result
    assert result["action"] == "link_add" and result["result"] == "ok" and result["changed"] is True
    assert result["steps"] == [{"kind": "add_link", "status": "done"}]
    assert result["comment_id"] is None and result["attempt_id"] == ATTEMPT
    validator("cli/link-add.json").validate(result)
    assert_golden("cli", "link-add", "ok", cli.norm(result))
    (write,) = cli.writes()[before:]
    assert (write.method, write.path) == ("POST", "/rest/api/3/issueLink")
    # KEY blocks OTHER: the ticket with the outward phrase is the request's inwardIssue (W22)
    assert write.body == {
        "type": {"name": "Blocks"},
        "inwardIssue": {"key": KEY},
        "outwardIssue": {"key": OTHER},
    }
    (mine,) = links(cli)
    assert mine["outwardIssue"]["key"] == OTHER and mine["type"]["name"] == "Blocks"
    (theirs,) = links(cli, OTHER)
    assert theirs["inwardIssue"]["key"] == KEY and theirs["id"] == mine["id"]


def test_the_inward_direction_swaps_the_two_tickets_in_the_request(cli: Cli) -> None:
    code, result = change(cli, "add", *args(direction="inward"))
    assert code == 0, result
    (write,) = cli.writes()
    assert write.body["inwardIssue"] == {"key": OTHER}
    assert write.body["outwardIssue"] == {"key": KEY}
    (mine,) = links(cli)
    assert mine["inwardIssue"]["key"] == OTHER  # KEY is blocked by OTHER


def test_the_link_reads_back_through_get_with_the_requested_direction(cli: Cli) -> None:
    change(cli, "add", *args(direction="inward"))
    code, doc = cli.doc("get", KEY, "--fields", "links")
    assert code == 0, doc
    (item,) = doc["tickets"][0]["links"]
    assert (item["type"], item["direction"], item["key"]) == ("Blocks", "inward", OTHER)


def test_add_dry_run_is_a_plan_with_the_link_change_and_sends_nothing(cli: Cli) -> None:
    code, plan = cli.doc("--dry-run", "link", "add", KEY, *args())
    assert code == 0 and plan["action"] == "link_add" and plan["key"] == KEY
    assert plan["steps"] == [
        {"kind": "add_link", "link_type": "Blocks", "direction": "outward", "other": OTHER}
    ]
    assert plan["changes"] == [
        {"name": "links", "from": [], "to": [f"Blocks: {KEY} blocks {OTHER}"]}
    ]
    assert plan["dedupe_key"] is None and plan["dedupe_match"] is None
    validator("cli/link-add.json").validate(plan)
    assert_golden("cli", "link-add", "dry-run", cli.norm(plan))
    assert cli.writes() == []


def test_adding_an_existing_link_is_unchanged_and_writes_nothing(cli: Cli) -> None:
    linked(cli)
    before = len(cli.writes())
    code, result = change(cli, "add", *args())
    assert code == 0, result
    assert result["result"] == "unchanged" and result["changed"] is False
    assert result["steps"] == [] and result["attempt_id"] is None
    validator("cli/link-add.json").validate(result)
    assert_golden("cli", "link-add", "unchanged", cli.norm(result))
    assert len(cli.writes()) == before
    assert len(links(cli)) == 1


def test_the_same_pair_in_the_other_direction_or_type_is_a_new_link(cli: Cli) -> None:
    linked(cli)
    for argv in (args(direction="inward"), args(kind="Relates")):
        code, result = change(cli, "add", *argv)
        assert code == 0 and result["result"] == "ok", result
    assert len(links(cli)) == 3


def test_the_type_matches_case_insensitively_and_is_sent_by_its_canonical_name(cli: Cli) -> None:
    code, result = change(cli, "add", *args(kind="blocks"))
    assert code == 0, result
    assert cli.writes()[0].body["type"] == {"name": "Blocks"}


@pytest.mark.parametrize("kind", ["Nope", "Blocks ", "bl"])
def test_an_unknown_link_type_is_invalid_value_with_candidates_and_sends_nothing(
    cli: Cli, kind: str
) -> None:
    code, env = cli.doc("--dry-run", "link", "add", KEY, *args(kind=kind))
    assert (code, env["error"]["code"], env["error"]["reason"]) == (
        22,
        "invalid_request",
        "invalid_value",
    )
    assert env["error"]["details"]["candidates"] == ["Blocks", "Relates"]
    assert cli.writes() == []


def test_a_ticket_cannot_be_linked_to_itself_and_nothing_is_read(cli: Cli) -> None:
    code, env = cli.doc("--dry-run", "link", "add", KEY, *args(other=KEY.lower()))
    assert (code, env["error"]["reason"]) == (22, "invalid_value")
    assert all(c.path.endswith("/myself") or c.method == "GET" for c in cli.fake.calls)
    assert not any("issueLink" in c.path or f"/issue/{KEY}" in c.path for c in cli.fake.calls)


@pytest.mark.parametrize("other", ["", "demo", "DEMO-1/../x", "DEMO-1\n", "ＤＥＭＯ-1", "x" * 5000])
def test_a_malformed_other_key_is_refused_before_any_request(cli: Cli, other: str) -> None:
    code, env = cli.doc("--dry-run", "link", "add", KEY, *args(other=other))
    assert code in (2, 22), env
    assert cli.fake.calls == [] or all(c.path.endswith("/myself") for c in cli.fake.calls)


@pytest.mark.parametrize(
    "argv",
    [
        ["--direction", "outward", "--other", OTHER],
        ["--type", "Blocks", "--other", OTHER],
        ["--type", "Blocks", "--direction", "outward"],
        ["--type", "Blocks", "--direction", "sideways", "--other", OTHER],
        ["--type", "Blocks", "--direction", "Outward", "--other", OTHER],
    ],
)
def test_the_three_options_are_required_and_the_direction_is_exact(
    cli: Cli, argv: list[str]
) -> None:
    code, env = cli.doc("--dry-run", "link", "add", KEY, *argv)
    assert code == 2, env
    assert cli.fake.calls == []


def test_a_missing_other_ticket_is_not_found_and_nothing_is_linked(cli: Cli) -> None:
    code, env = change(cli, "add", *args(other="DEMO-999"))
    assert (code, env["error"]["code"]) == (10, "not_found")
    assert links(cli) == [] and links(cli, OTHER) == []


def test_a_missing_ticket_is_not_found(cli: Cli) -> None:
    code, env = cli.doc("--dry-run", "link", "add", "DEMO-999", *args())
    assert (code, env["error"]["code"]) == (10, "not_found")


def test_add_needs_confirmation_without_yes(cli: Cli) -> None:
    code, env = cli.doc("link", "add", KEY, *args())
    assert code == 13 and env["error"]["code"] == "confirmation_required"
    assert links(cli) == []


def test_add_yes_needs_expect_plan_and_no_dedupe_key(cli: Cli) -> None:
    code, env = cli.doc("--yes", "link", "add", KEY, *args())
    assert (code, env["error"]["reason"]) == (2, "yes_requires_expect_plan")
    code, plan = cli.doc("--dry-run", "link", "add", KEY, *args())
    code, result = cli.doc(
        "--yes", "--expect-plan", plan["plan_sha256"], "link", "add", KEY, *args()
    )
    assert code == 0 and result["result"] == "ok"


def test_a_link_added_by_someone_else_since_the_plan_makes_the_add_unchanged(cli: Cli) -> None:
    _, plan = cli.doc("--dry-run", "link", "add", KEY, *args())
    linked(cli)  # someone else adds it first: the intended state already holds
    code, result = cli.doc(
        "--yes", "--expect-plan", plan["plan_sha256"], "link", "add", KEY, *args()
    )
    assert code == 0 and result["result"] == "unchanged"
    assert len(links(cli)) == 1


def test_an_unrelated_link_or_edit_does_not_stale_the_plan(cli: Cli) -> None:
    _, plan = cli.doc("--dry-run", "link", "add", KEY, *args())
    cli.fake.tickets[KEY].summary = "edited meanwhile"
    code, _ = change(cli, "add", *args(kind="Relates"))
    assert code == 0
    code, result = cli.doc(
        "--yes", "--expect-plan", plan["plan_sha256"], "link", "add", KEY, *args()
    )
    assert code == 0 and result["result"] == "ok"


def test_the_plan_hash_binds_type_direction_and_other(cli: Cli) -> None:
    hashes = {
        cli.doc("--dry-run", "link", "add", KEY, *argv)[1]["plan_sha256"]
        for argv in (
            args(),
            args(direction="inward"),
            args(kind="Relates"),
            args(other="DEMO-1"),
        )
    }
    assert len(hashes) == 4
    assert cli.doc("--dry-run", "link", "add", KEY, *args())[1]["plan_sha256"] in hashes


def test_add_and_remove_never_share_a_plan_hash(cli: Cli) -> None:
    add = cli.doc("--dry-run", "link", "add", KEY, *args())[1]["plan_sha256"]
    remove = cli.doc("--dry-run", "link", "remove", KEY, *args())[1]["plan_sha256"]
    assert add != remove


# --- remove -------------------------------------------------------------------------------------


def test_remove_deletes_the_link_by_its_id_from_both_tickets(cli: Cli) -> None:
    lid = linked(cli)
    before = len(cli.writes())
    code, result = change(cli, "remove", *args())
    assert code == 0, result
    assert result["action"] == "link_remove" and result["result"] == "ok"
    assert result["steps"] == [{"kind": "remove_link", "status": "done"}]
    validator("cli/link-remove.json").validate(result)
    assert_golden("cli", "link-remove", "ok", cli.norm(result))
    (write,) = cli.writes()[before:]
    assert (write.method, write.path) == ("DELETE", f"/rest/api/3/issueLink/{lid}")
    assert links(cli) == [] and links(cli, OTHER) == []


def test_remove_dry_run_shows_the_link_and_sends_nothing(cli: Cli) -> None:
    lid = linked(cli)
    before = len(cli.writes())
    code, plan = cli.doc("--dry-run", "link", "remove", KEY, *args())
    assert code == 0, plan
    assert plan["steps"] == [
        {
            "kind": "remove_link",
            "link_type": "Blocks",
            "direction": "outward",
            "other": OTHER,
            "link_id": lid,
        }
    ]
    assert plan["changes"] == [
        {"name": "links", "from": [f"Blocks: {KEY} blocks {OTHER}"], "to": []}
    ]
    validator("cli/link-remove.json").validate(plan)
    assert_golden("cli", "link-remove", "dry-run", cli.norm(plan))
    assert len(cli.writes()) == before


def test_removing_an_absent_link_is_unchanged(cli: Cli) -> None:
    code, result = change(cli, "remove", *args())
    assert code == 0 and result["result"] == "unchanged" and result["steps"] == []
    assert cli.writes() == []
    validator("cli/link-remove.json").validate(result)
    assert_golden("cli", "link-remove", "unchanged", cli.norm(result))


def test_remove_matches_the_direction_the_type_and_the_other_ticket(cli: Cli) -> None:
    linked(cli)
    for argv in (args(direction="inward"), args(kind="Relates"), args(other="DEMO-1")):
        code, result = change(cli, "remove", *argv)
        assert code == 0 and result["result"] == "unchanged", result
    assert len(links(cli)) == 1


def test_remove_deletes_every_matching_link_one_step_each(cli: Cli) -> None:
    linked(cli)
    # a duplicate the tool would never create, as another client can
    dup = json.loads(json.dumps(links(cli)[0]))
    dup["id"] = "39999"
    links(cli).append(dup)
    links(cli, OTHER).append({**json.loads(json.dumps(links(cli, OTHER)[0])), "id": "39999"})
    code, result = change(cli, "remove", *args())
    assert code == 0, result
    assert result["steps"] == [{"kind": "remove_link", "status": "done"}] * 2
    assert links(cli) == []


def test_a_link_replaced_since_the_plan_is_stale_and_removes_nothing(cli: Cli) -> None:
    linked(cli)
    _, plan = cli.doc("--dry-run", "link", "remove", KEY, *args())
    change(cli, "remove", *args())
    linked(cli)  # same content, new id
    before = len(cli.writes())
    code, env = cli.doc(
        "--yes", "--expect-plan", plan["plan_sha256"], "link", "remove", KEY, *args()
    )
    assert (code, env["error"]["code"]) == (20, "stale_ticket")
    assert len(cli.writes()) == before and len(links(cli)) == 1


def test_remove_needs_confirmation_without_yes(cli: Cli) -> None:
    linked(cli)
    code, env = cli.doc("link", "remove", KEY, *args())
    assert code == 13 and env["error"]["code"] == "confirmation_required"
    assert len(links(cli)) == 1


def test_a_link_deleted_by_someone_else_between_plan_and_send_is_not_found(cli: Cli) -> None:
    linked(cli)
    cli.fake.inject(Fault("status", status=404, method="DELETE", path="issueLink"))
    code, env = change(cli, "remove", *args())
    assert (code, env["error"]["code"]) == (10, "not_found")


# --- unreadable provider data --------------------------------------------------------------------


@pytest.mark.parametrize(
    "entry",
    [
        "text",
        {"id": "1", "type": {"name": "Blocks"}},
        {
            "id": "1",
            "type": {"name": "Blocks"},
            "inwardIssue": {"key": "DEMO-1"},
            "outwardIssue": {"key": "DEMO-3"},
        },
        {"id": "1", "type": {"name": "Blocks"}, "inwardIssue": {"key": "not a key"}},
        {"id": "1", "type": {}, "inwardIssue": {"key": "DEMO-1"}},
        {"type": {"name": "Blocks"}, "inwardIssue": {"key": "DEMO-1"}},
        {"id": "../x", "type": {"name": "Blocks"}, "inwardIssue": {"key": "DEMO-1"}},
    ],
)
@pytest.mark.parametrize("verb", ["add", "remove"])
def test_an_unreadable_link_entry_is_provider_changed_never_skipped(
    cli: Cli, verb: str, entry: object
) -> None:
    links(cli).append(entry)  # type: ignore[arg-type]
    code, env = cli.doc("--dry-run", "link", verb, KEY, *args())
    assert (code, env["error"]["code"]) == (12, "provider_changed")
    assert cli.writes() == []


def test_a_ticket_without_a_link_list_is_provider_changed(
    cli: Cli, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(cli.fake.tickets[KEY], "links", None)
    code, env = cli.doc("--dry-run", "link", "add", KEY, *args())
    assert (code, env["error"]["code"]) == (12, "provider_changed")


def test_parse_links_accepts_integer_ids_and_both_sides() -> None:
    got = linkcmd.parse_links(
        [
            {"id": 7, "type": {"name": "Blocks"}, "inwardIssue": {"key": "DEMO-1"}},
            {"id": "8", "type": {"name": "Relates"}, "outwardIssue": {"key": "DEMO-4"}},
        ]
    )
    assert got == [
        linkcmd.Existing("7", "Blocks", "inward", "DEMO-1"),
        linkcmd.Existing("8", "Relates", "outward", "DEMO-4"),
    ]
    with pytest.raises(TicketctlError):
        linkcmd.parse_links([{"id": True, "type": {"name": "x"}, "inwardIssue": {"key": "A-1"}}])


# --- failures, outcomes, audit, hooks -------------------------------------------------------------


@pytest.mark.parametrize("verb, method", [("add", "POST"), ("remove", "DELETE")])
def test_a_write_whose_answer_is_lost_is_outcome_unknown_and_a_rerun_is_safe(
    cli: Cli, verb: str, method: str
) -> None:
    if verb == "remove":
        linked(cli)
    cli.fake.inject(Fault("reset", method=method, path="issueLink", after_apply=True))
    code, env = change(cli, verb, *args())
    assert (code, env["error"]["code"]) == (12, "outcome_unknown")
    assert env["error"]["retryable"] is True and env["error"]["outcome_unknown"] is True
    code, again = change(cli, verb, *args())  # the write landed: nothing is sent twice
    assert code == 0 and again["result"] == "unchanged"
    assert len(links(cli)) == (1 if verb == "add" else 0)


@pytest.mark.parametrize("status", [400, 403, 409])
def test_a_provider_refusal_of_the_write_maps_to_its_code(cli: Cli, status: int) -> None:
    cli.fake.inject(Fault("status", status=status, method="POST", path="issueLink"))
    code, env = change(cli, "add", *args())
    assert code == 12, env
    validator("cli/error.json").validate(env)
    assert links(cli) == []


def _audit(cli: Cli) -> list[dict[str, Any]]:
    path = cli.root / "state" / "ticketctl" / "audit.jsonl"
    return [json.loads(line) for line in path.read_text().splitlines()]


def test_the_audit_records_the_action_and_step_kind(cli: Cli) -> None:
    change(cli, "add", *args())
    change(cli, "remove", *args())
    records = _audit(cli)
    assert [(r["action"], r["step_kind"], r["result"]) for r in records] == [
        ("link_add", "add_link", "attempted"),
        ("link_add", "add_link", "ok"),
        ("link_remove", "remove_link", "attempted"),
        ("link_remove", "remove_link", "ok"),
    ]
    for record in records:
        validator("audit.json").validate(record)
        assert MARK not in json.dumps(record)


def test_both_actions_use_one_hook_family_that_carries_no_ticket_text(
    cli: Cli, monkeypatch: pytest.MonkeyPatch
) -> None:
    cli.fake.tickets[OTHER].summary = MARK
    cli.config.write_text(
        cli.config.read_text()
        + "hooks:\n  pre-link:\n    - cmd: ignored\n  post-link:\n    - cmd: ignored\n"
        + "  pre-sprint:\n    - cmd: other\n"
    )
    seen: list[tuple[str, bytes]] = []

    def fake_run(argv: list[str], *, stdin: bytes, env: dict[str, str], **kw: Any) -> HookRun:
        seen.append((env["TICKETCTL_HOOK"], stdin))
        assert env["TICKETCTL_ID"] == KEY
        return HookRun(1, 0, b"", b"", False, False, False)

    monkeypatch.setattr(hooks, "run_hook", fake_run)
    assert change(cli, "add", *args())[0] == 0
    assert change(cli, "remove", *args())[0] == 0
    assert [name for name, _ in seen] == ["pre-link", "post-link", "pre-link", "post-link"]
    for name, raw in seen:
        payload = json.loads(raw)
        validator(f"hooks/{name}.json").validate(payload)
        assert MARK not in raw.decode()
        assert payload["links"] == [{"type": "Blocks", "direction": "outward", "key": OTHER}]
        assert payload["project"] == "DEMO" and payload["ticket"]["type"]
    assert [json.loads(raw)["plan"]["action"] for _, raw in seen] == [
        "link_add",
        "link_add",
        "link_remove",
        "link_remove",
    ]
    assert_golden("hooks", "pre-link", "add", cli.norm(json.loads(seen[0][1])))
    assert_golden("hooks", "post-link", "add", cli.norm(json.loads(seen[1][1])))
    assert_golden("hooks", "pre-link", "remove", cli.norm(json.loads(seen[2][1])))


def test_an_unchanged_link_runs_no_hook(cli: Cli, monkeypatch: pytest.MonkeyPatch) -> None:
    linked(cli)
    cli.config.write_text(cli.config.read_text() + "hooks:\n  pre-link:\n    - cmd: ignored\n")
    calls: list[str] = []
    monkeypatch.setattr(
        hooks,
        "run_hook",
        lambda *a, **k: calls.append("run") or HookRun(1, 0, b"", b"", False, False, False),
    )
    code, result = change(cli, "add", *args())
    assert code == 0 and result["result"] == "unchanged" and calls == []


def test_a_pre_hook_veto_stops_the_link(cli: Cli, monkeypatch: pytest.MonkeyPatch) -> None:
    cli.config.write_text(cli.config.read_text() + "hooks:\n  pre-link:\n    - cmd: ignored\n")
    monkeypatch.setattr(
        hooks, "run_hook", lambda *a, **k: HookRun(1, 1, b"", b"no", False, False, False)
    )
    code, env = change(cli, "add", *args())
    assert (code, env["error"]["code"]) == (11, "vetoed")
    assert links(cli) == []


def test_a_hook_flag_names_the_new_family(cli: Cli) -> None:
    code, env = cli.doc("--hook", "pre-link=exit 1", "--dry-run", "link", "add", KEY, *args())
    assert code == 0, env  # a dry run runs no hook; the flag name is accepted
    code, env = cli.doc("--hook", "pre-links=exit 1", "--dry-run", "link", "add", KEY, *args())
    assert code == 2


@pytest.mark.parametrize("verb", ["add", "remove"])
def test_read_only_mode_refuses_both_before_any_request(
    cli: Cli, monkeypatch: pytest.MonkeyPatch, verb: str
) -> None:
    monkeypatch.setenv("TICKETCTL_READ_ONLY", "1")
    code, env = cli.doc("--yes", "--expect-plan", "0" * 64, "link", verb, KEY, *args())
    assert (code, env["error"]["code"]) == (20, "read_only")
    assert cli.fake.calls == []


def test_an_account_mismatch_refuses_before_any_write(cli: Cli) -> None:
    cli.config.write_text(cli.config.read_text() + "me:\n  jira: acct-9999\n")
    _, plan = cli.doc("--dry-run", "link", "add", KEY, *args())
    code, env = cli.doc(
        "--yes", "--expect-plan", plan.get("plan_sha256", "0" * 64), "link", "add", KEY, *args()
    )
    assert code == 2 and cli.writes() == []


def test_other_ticket_of_another_site_is_mixed_sites(cli: Cli) -> None:
    cli.config.write_text(
        cli.config.read_text().replace(
            "sites:\n",
            "sites:\n  second:\n    url: http://127.0.0.1:9\n"
            "    email: a@example.test\n    token_env: X\n    allow_http: true\n"
            "    projects: [AB]\n",
            1,
        )
    )
    code, env = cli.doc("--dry-run", "link", "add", KEY, *args(other="AB-1"))
    assert code in (21, 22), env  # no single site can take both keys
    assert cli.writes() == []


def test_a_site_flag_and_lowercase_keys_are_accepted(cli: Cli) -> None:
    code, result = change(cli, "add", *args(other=OTHER.lower()), key=KEY.lower())
    assert code == 0 and result["key"] == KEY


def test_the_link_id_segment_refuses_path_tricks(cli: Cli) -> None:
    links(cli).append({"id": "..", "type": {"name": "Blocks"}, "outwardIssue": {"key": OTHER}})
    code, env = cli.doc("--dry-run", "link", "remove", KEY, *args())
    assert (code, env["error"]["code"]) == (12, "provider_changed") and cli.writes() == []


# --- review round 1 ---------------------------------------------------------------------------

ZW = "Dep\u200bends"  # a provider type name with a zero-width character
ZW_SHOWN = "Dep<U+200B>ends"  # how a plan, a hook and the candidate list show it


def types(cli: Cli, *entries: tuple[str, str, str]) -> None:
    cli.fake.LINK_TYPES = tuple(  # type: ignore[misc]
        {"id": str(10000 + i), "name": n, "inward": a, "outward": b}
        for i, (n, a, b) in enumerate(entries)
    )


def hook_payloads(
    cli: Cli, monkeypatch: pytest.MonkeyPatch, verb: str, *argv: str, key: str = KEY
) -> list[dict[str, Any]]:
    cli.config.write_text(cli.config.read_text() + "hooks:\n  pre-link:\n    - cmd: ignored\n")
    seen: list[dict[str, Any]] = []

    def fake_run(argv: list[str], *, stdin: bytes, env: dict[str, str], **kw: Any) -> HookRun:
        seen.append(json.loads(stdin))
        return HookRun(1, 0, b"", b"", False, False, False)

    monkeypatch.setattr(hooks, "run_hook", fake_run)
    assert change(cli, verb, *argv, key=key)[0] == 0
    return seen


def test_a_symmetric_type_is_one_link_whichever_direction_it_was_added(cli: Cli) -> None:
    assert change(cli, "add", *args(kind="Relates"))[0] == 0
    before = len(cli.writes())
    for direction in ("outward", "inward"):
        code, result = change(cli, "add", *args(kind="Relates", direction=direction))
        assert code == 0 and result["result"] == "unchanged", result
    assert len(cli.writes()) == before and len(links(cli)) == 1


def test_a_symmetric_link_added_inward_is_unchanged_when_added_outward(cli: Cli) -> None:
    assert change(cli, "add", *args(kind="Relates", direction="inward"))[0] == 0
    code, result = change(cli, "add", *args(kind="Relates", direction="outward"))
    assert code == 0 and result["result"] == "unchanged", result
    assert len(links(cli)) == 1


@pytest.mark.parametrize("stored", ["outward", "inward"])
@pytest.mark.parametrize("asked", ["outward", "inward"])
def test_remove_of_a_symmetric_link_works_in_either_direction(
    cli: Cli, stored: str, asked: str
) -> None:
    assert change(cli, "add", *args(kind="Relates", direction=stored))[0] == 0
    code, result = change(cli, "remove", *args(kind="Relates", direction=asked))
    assert code == 0 and result["result"] == "ok", result
    assert links(cli) == [] and links(cli, OTHER) == []


def test_an_asymmetric_type_still_keeps_its_direction(cli: Cli) -> None:
    linked(cli)  # Blocks outward
    code, result = change(cli, "add", *args(direction="inward"))
    assert code == 0 and result["result"] == "ok"
    code, result = change(cli, "remove", *args(direction="inward"))
    assert code == 0 and result["result"] == "ok" and len(links(cli)) == 1


def test_a_type_name_with_a_zero_width_character_is_matched_and_sent_raw(cli: Cli) -> None:
    types(cli, (ZW, "depends on", "is depended on by"))
    code, plan = cli.doc("--dry-run", "link", "add", KEY, *args(kind=ZW_SHOWN))
    assert code == 0, plan
    assert "\u200b" not in json.dumps(plan, ensure_ascii=False)  # shown text is sanitised
    assert change(cli, "add", *args(kind=ZW_SHOWN))[0] == 0
    assert cli.writes()[0].body["type"] == {"name": ZW}  # the request keeps the raw name
    before = len(cli.writes())
    code, result = change(cli, "add", *args(kind=ZW_SHOWN))
    assert code == 0 and result["result"] == "unchanged" and len(cli.writes()) == before
    code, result = change(cli, "remove", *args(kind=ZW_SHOWN))
    assert code == 0 and result["result"] == "ok" and links(cli) == []


def test_remove_deletes_a_link_of_a_raw_type_name_through_the_zero_width_name(
    cli: Cli,
) -> None:
    types(cli, (ZW, "depends on", "is depended on by"))
    assert change(cli, "add", *args(kind=ZW_SHOWN))[0] == 0
    _, plan = cli.doc("--dry-run", "link", "remove", KEY, *args(kind=ZW_SHOWN))
    assert [s["kind"] for s in plan["steps"]] == ["remove_link"]


def test_remove_delete_refused_with_403_leaves_the_link(cli: Cli) -> None:
    linked(cli)
    cli.fake.inject(Fault("status", status=403, method="DELETE", path="issueLink"))
    code, env = change(cli, "remove", *args())
    assert code == 12 and env["error"]["code"] != "outcome_unknown", env
    validator("cli/error.json").validate(env)
    assert len(links(cli)) == 1 and len(links(cli, OTHER)) == 1


def test_two_types_that_differ_only_in_case_pick_the_exact_name(cli: Cli) -> None:
    types(cli, ("Blocks", "is blocked by", "blocks"), ("blocks", "is under", "covers"))
    assert change(cli, "add", *args(kind="blocks"))[0] == 0
    assert cli.writes()[0].body["type"] == {"name": "blocks"}
    code, env = change(cli, "add", *args(kind="BLOCKS"))
    assert (code, env["error"]["reason"]) == (22, "invalid_value")
    assert env["error"]["details"]["candidates"] == ["Blocks", "blocks"]
    assert len(cli.writes()) == 1


def test_the_candidate_list_is_capped(cli: Cli) -> None:
    types(cli, *((f"t{i:03d}", "in", "out") for i in range(linkcmd.MAX_CANDIDATES + 10)))
    code, env = cli.doc("--dry-run", "link", "add", KEY, *args(kind="nope"))
    assert code == 22
    assert env["error"]["details"]["candidates"] == [
        f"t{i:03d}" for i in range(linkcmd.MAX_CANDIDATES)
    ]


def _second_site(cli: Cli) -> None:
    cli.config.write_text(
        cli.config.read_text().replace(
            "allow_http: true\n", "allow_http: true\n    projects: [DEMO]\n", 1
        )
        + "  second:\n    url: http://127.0.0.1:9\n    email: a@example.test\n"
        + "    token_env: X\n    allow_http: true\n    projects: [AB]\n"
    )


def test_an_other_ticket_of_another_site_is_exactly_mixed_sites(cli: Cli) -> None:
    _second_site(cli)
    code, env = cli.doc("--dry-run", "link", "add", KEY, *args(other="AB-1"))
    assert (code, env["error"]["code"], env["error"]["reason"]) == (
        22,
        "invalid_request",
        "mixed_sites",
    )
    assert cli.fake.calls == []


def test_a_site_flag_selects_the_site_of_both_tickets(cli: Cli) -> None:
    code, result = change(cli, "add", *args(), key=KEY)
    assert code == 0
    code, plan = cli.doc("--site", "main", "--dry-run", "link", "add", KEY, *args(kind="Relates"))
    assert code == 0 and plan["site"] == "main", plan
    _second_site(cli)
    code, env = cli.doc("--site", "main", "--dry-run", "link", "add", KEY, *args(other="AB-1"))
    assert env.get("error", {}).get("reason") != "mixed_sites", env


def test_the_inward_phrase_is_shown_in_the_plan(cli: Cli) -> None:
    code, plan = cli.doc("--dry-run", "link", "add", KEY, *args(direction="inward"))
    assert code == 0, plan
    assert plan["changes"] == [
        {"name": "links", "from": [], "to": [f"Blocks: {KEY} is blocked by {OTHER}"]}
    ]


def test_the_hook_payload_carries_the_inward_direction(
    cli: Cli, monkeypatch: pytest.MonkeyPatch
) -> None:
    (payload,) = hook_payloads(cli, monkeypatch, "add", *args(direction="inward"))
    assert payload["links"] == [{"type": "Blocks", "direction": "inward", "key": OTHER}]
    validator("hooks/pre-link.json").validate(payload)


def test_the_hook_facts_of_the_ticket(cli: Cli, monkeypatch: pytest.MonkeyPatch) -> None:
    (payload,) = hook_payloads(cli, monkeypatch, "add", *args())
    assert payload["project"] == "DEMO"
    assert payload["ticket"] == {
        "type": "Task",
        "subtask": False,
        "status": "In Progress",
        "priority": "Medium",
        "assignee_account_id": "acct-0001",
        "assignee_is_me": True,
    }
    assert payload["plan"]["current_status"] == {"name": "In Progress", "category": "indeterminate"}


def test_the_hook_facts_of_a_subtask_assigned_to_someone_else(
    cli: Cli, monkeypatch: pytest.MonkeyPatch
) -> None:
    cli.fake.add_ticket("DEMO-9", "sub", type_id="3", status="Done", assignee="acct-0002")
    (payload,) = hook_payloads(cli, monkeypatch, "add", *args(), key="DEMO-9")
    assert payload["ticket"]["subtask"] is True and payload["ticket"]["type"] == "Sub-task"
    assert payload["ticket"]["assignee_account_id"] == "acct-0002"
    assert payload["ticket"]["assignee_is_me"] is False
    assert payload["plan"]["current_status"]["category"] == "done"


def test_a_malformed_assignee_account_id_is_not_sent_to_a_hook(
    cli: Cli, monkeypatch: pytest.MonkeyPatch
) -> None:
    cli.fake.tickets[KEY].assignee = "bad id!"
    (payload,) = hook_payloads(cli, monkeypatch, "add", *args())
    assert payload["ticket"]["assignee_account_id"] is None


def test_an_unchanged_remove_plan_has_no_changes_and_no_steps(cli: Cli) -> None:
    code, plan = cli.doc("--dry-run", "link", "remove", KEY, *args())
    assert code == 0 and plan["steps"] == [] and plan["changes"] == []


@pytest.mark.parametrize("status", [403, 500])
def test_a_failing_read_of_the_link_types_changes_nothing(cli: Cli, status: int) -> None:
    cli.fake.inject(Fault("status", status=status, method="GET", path="issueLinkType", times=None))
    code, env = cli.doc("--dry-run", "link", "add", KEY, *args())
    assert code != 0 and "error" in env
    assert cli.writes() == []


def test_too_many_link_types_is_provider_changed(cli: Cli, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(linkcmd, "MAX_TYPES", 1)
    code, env = cli.doc("--dry-run", "link", "add", KEY, *args())
    assert (code, env["error"]["code"]) == (12, "provider_changed")


def test_too_many_links_on_the_ticket_is_provider_changed(
    cli: Cli, monkeypatch: pytest.MonkeyPatch
) -> None:
    linked(cli)
    assert change(cli, "add", *args(kind="Relates"))[0] == 0
    monkeypatch.setattr(linkcmd, "MAX_LINKS", 1)
    code, env = cli.doc("--dry-run", "link", "add", KEY, *args())
    assert (code, env["error"]["code"]) == (12, "provider_changed")


@pytest.mark.parametrize("phrase", ["", "   ", None, 5])
@pytest.mark.parametrize("which", ["inward", "outward", "name"])
def test_a_link_type_with_an_unreadable_phrase_is_provider_changed(
    cli: Cli, which: str, phrase: object
) -> None:
    entry: dict[str, Any] = {"id": "1", "name": "Blocks", "inward": "in", "outward": "out"}
    entry[which] = phrase
    cli.fake.LINK_TYPES = (entry,)  # type: ignore[misc]
    code, env = cli.doc("--dry-run", "link", "add", KEY, *args())
    assert (code, env["error"]["code"]) == (12, "provider_changed")


def test_one_null_side_next_to_a_real_side_reads_as_the_real_side() -> None:
    got = linkcmd.parse_links(
        [
            {
                "id": "1",
                "type": {"name": "Blocks"},
                "inwardIssue": None,
                "outwardIssue": {"key": "DEMO-4"},
            }
        ]
    )
    assert got == [linkcmd.Existing("1", "Blocks", "outward", "DEMO-4")]


def test_a_type_whose_raw_name_equals_another_types_shown_name_is_selectable(cli: Cli) -> None:
    types(cli, (ZW, "depends on", "is depended on by"), (ZW_SHOWN, "needs", "is needed by"))
    assert change(cli, "add", *args(kind=ZW_SHOWN))[0] == 0
    assert cli.writes()[0].body["type"] == {"name": ZW_SHOWN}  # the raw hit wins


def test_a_type_swapped_under_an_equal_shown_name_changes_the_plan_hash(cli: Cli) -> None:
    types(cli, ("Blocks", "is blocked by", "blocks"))
    _, first = cli.doc("--dry-run", "link", "add", KEY, *args())
    cli.fake.LINK_TYPES = tuple(  # type: ignore[misc]
        {**t, "id": "20000"} for t in cli.fake.LINK_TYPES
    )
    _, second = cli.doc("--dry-run", "link", "add", KEY, *args())
    assert first["plan_sha256"] != second["plan_sha256"]
