"""The ``create`` command through the CLI against the fake provider (issue #31)."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

import pytest

from tests.fake_provider import (
    F_POINTS,
    ME_ACCOUNT,
    OTHER_ACCOUNT,
    SITE_EMAIL,
    SITE_TOKEN,
    FakeProvider,
    Fault,
    Out,
    ProviderError,
)
from tests.golden import assert_golden, validator
from ticketctl import createcmd, hooks
from ticketctl.cli import main
from ticketctl.gateway import HookRun
from ticketctl.writeflow import Seams

SITE_ENV_VAR = "TICKETCTL_TEST_SITE_ENV"
ATTEMPT = "0123456789abcdef"
SITE_URL = "https://tracker.example.test"
BODY_MARK = "zq7x-body-marker-4f9c2e"
KEY_RE = re.compile(r"DEMO-[0-9]+")


class Cli:
    def __init__(self, root: Path, fake: FakeProvider, capsys: Any) -> None:
        self.fake, self.capsys, self.root = fake, capsys, root
        self.config = root / "config.yaml"
        self.config.write_text(
            f"sites:\n  main:\n    url: {fake.url}\n    email: {SITE_EMAIL}\n"
            f"    token_env: {SITE_ENV_VAR}\n    allow_http: true\n"
            f"    fields: {{estimate: {{id: {F_POINTS}}}}}\n"
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

    def create(self, *argv: str, pre: tuple[str, ...] = ()) -> tuple[int, dict[str, Any]]:
        """Plan with --dry-run, then apply that plan with --yes."""
        code, plan = self.doc(*pre, "--dry-run", "create", *argv)
        assert code == 0, plan
        return self.doc(*pre, "--yes", "--expect-plan", plan["plan_sha256"], "create", *argv)

    def posts(self) -> list[Any]:
        return [c for c in self.fake.calls if c.method == "POST" and c.path == "/rest/api/3/issue"]

    def writes(self) -> list[Any]:
        return [c for c in self.fake.calls if c.method != "GET" and "search" not in c.path]


@pytest.fixture
def cli(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: Any, fake: FakeProvider) -> Cli:
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    monkeypatch.setenv(SITE_ENV_VAR, SITE_TOKEN)
    monkeypatch.setenv("PATH", "/nonexistent")
    monkeypatch.setattr(createcmd, "make_seams", lambda: Seams(attempt_id=lambda: ATTEMPT))
    return Cli(tmp_path, fake, capsys)


BASE = ("--project", "DEMO", "--type", "Task", "--title")


def body(call: Any) -> dict[str, Any]:
    return call.body["fields"]


def test_creates_one_ticket_with_one_post_and_the_golden(cli: Cli) -> None:
    code, result = cli.create(*BASE, "A new thing", "--dedupe-key", "k1")
    assert code == 0 and result["result"] == "ok" and result["changed"] is True
    assert result["steps"] == [{"kind": "create", "status": "done"}]
    assert KEY_RE.fullmatch(result["key"]) and result["url"].endswith("/browse/" + result["key"])
    assert result["reused"] is False and result["attempt_id"] == ATTEMPT
    validator("cli/create.json").validate(result)
    assert_golden("cli", "create", "ok", cli.norm(result))
    (post,) = cli.posts()
    fields = body(post)
    assert fields["summary"] == "A new thing" and fields["project"] == {"key": "DEMO"}
    assert fields["issuetype"] == {"id": "1"}
    assert len(fields["labels"]) == 1 and re.fullmatch(r"tc-[0-9a-f]{16}", fields["labels"][0])
    assert set(fields) == {"project", "issuetype", "summary", "labels", "description"}
    t = cli.fake.tickets[result["key"]]
    assert t.summary == "A new thing" and t.reporter == ME_ACCOUNT


def test_the_dry_run_plan_has_one_create_step_and_sends_nothing(cli: Cli) -> None:
    code, plan = cli.doc("--dry-run", "create", *BASE, "T", "--dedupe-key", "k1")
    assert code == 0 and [s["kind"] for s in plan["steps"]] == ["create"]
    assert plan["key"] is None and plan["dedupe_match"] is None
    assert plan["steps"][0]["project"] == "DEMO" and plan["steps"][0]["type"] == "Task"
    assert [c["name"] for c in plan["changes"]] == ["title", "description"]
    assert not any(re.search(r"tc-[0-9a-f]{16}", json.dumps(c)) for c in plan["changes"])
    validator("cli/create.json").validate(plan)
    assert_golden("cli", "create", "dry-run", cli.norm(plan))
    assert cli.posts() == []


def test_yes_without_a_key_is_dedupe_key_required(cli: Cli) -> None:
    code, env = cli.doc("--yes", "--expect-plan", "0" * 64, "create", *BASE, "Twin")
    assert (code, env["error"]["reason"]) == (2, "dedupe_key_required")
    assert cli.posts() == []


def test_a_keyed_re_run_is_reused_and_sends_nothing(cli: Cli) -> None:
    code, first = cli.create(*BASE, "Once", "--dedupe-key", "same")
    assert code == 0
    cli.fake.advance(5)  # the label index catches up
    code, second = cli.doc("--dry-run", "create", *BASE, "Once", "--dedupe-key", "same")
    assert code == 0 and second["steps"] == [] and second["dedupe_match"] == first["key"]
    code, again = cli.doc("create", *BASE, "Once", "--dedupe-key", "same")
    assert code == 0 and again["result"] == "unchanged" and again["reused"] is True
    assert again["key"] == first["key"] and again["changed"] is False
    assert again["attempt_id"] is None and len(cli.posts()) == 1
    validator("cli/create.json").validate(again)
    assert_golden("cli", "create", "reused", cli.norm(again))


def test_a_title_with_surrounding_whitespace_matches_its_own_ticket(cli: Cli) -> None:
    code, first = cli.create(*BASE, "  padded  ", "--dedupe-key", "ws")
    assert code == 0 and cli.fake.tickets[first["key"]].summary == "padded"
    cli.fake.advance(5)
    code, again = cli.doc("create", *BASE, " padded", "--dedupe-key", "ws")
    assert code == 0 and again["reused"] is True and len(cli.posts()) == 1


def test_a_key_reused_with_another_title_or_type_is_already_exists(cli: Cli) -> None:
    code, first = cli.create(*BASE, "One", "--dedupe-key", "kk")
    assert code == 0
    cli.fake.advance(5)
    for argv in (
        ["--project", "DEMO", "--type", "Task", "--title", "Two"],
        ["--project", "DEMO", "--type", "Bug", "--title", "One"],
    ):
        code, env = cli.doc("--dry-run", "create", *argv, "--dedupe-key", "kk")
        assert (code, env["error"]["code"]) == (20, "already_exists")
        assert env["error"]["details"]["existing_key"] == first["key"]
    assert len(cli.posts()) == 1


def test_a_planted_label_of_another_reporter_is_not_reused(cli: Cli) -> None:
    label = createcmd.CreateAction("DEMO", "Task", dedupe_key="plant").label("main")
    t = cli.fake.add_ticket("DEMO-50", "Planted", labels=[label])
    t.reporter = OTHER_ACCOUNT
    cli.fake.advance(5)
    code, plan = cli.doc("--dry-run", "create", *BASE, "Planted", "--dedupe-key", "plant")
    assert code == 0 and [s["kind"] for s in plan["steps"]] == ["create"]
    assert plan["dedupe_match"] is None


def test_several_own_matches_return_the_oldest_with_a_warning(cli: Cli) -> None:
    label = createcmd.CreateAction("DEMO", "Task", dedupe_key="race").label("main")
    for key in ("DEMO-61", "DEMO-60"):
        t = cli.fake.add_ticket(key, "Racy", labels=[label])
        t.reporter = ME_ACCOUNT
    cli.fake.advance(5)
    code, result = cli.doc("create", *BASE, "Racy", "--dedupe-key", "race")
    assert code == 0 and result["key"] == "DEMO-60" and result["reused"] is True
    assert "duplicate_dedupe_matches" in result["warnings"]


def test_an_unknown_type_lists_the_valid_ones(cli: Cli) -> None:
    code, env = cli.doc(
        "--dry-run", "create", "--project", "DEMO", "--type", "Nope", "--title", "t"
    )
    assert (code, env["error"]["reason"]) == (22, "invalid_value")
    assert env["error"]["details"]["candidates"] == ["Bug", "Epic", "Sub-task", "Task"]
    validator("cli/error.json").validate(env)


def test_the_type_matches_case_insensitively_and_is_sent_by_id(cli: Cli) -> None:
    code, plan = cli.doc(
        "--dry-run", "create", "--project", "DEMO", "--type", "bug", "--title", "t"
    )
    assert code == 0 and plan["steps"][0]["type"] == "Bug"


def test_missing_required_fields_with_the_discovery_route(cli: Cli) -> None:
    # A Task needs nothing more, so plant a required field through the createmeta answer.
    original = cli.fake._createmeta_fields

    def with_required(call: Any, project: str, type_id: str) -> Any:
        out = original(call, project, type_id)
        for f in out.body["fields"]:
            if f["fieldId"] == F_POINTS:
                f["required"] = True
        return out

    cli.fake._createmeta_fields = with_required  # type: ignore[method-assign]
    cli.fake._routes = []
    cli.fake._build_routes()
    code, env = cli.doc("--dry-run", "create", *BASE, "t")
    assert (code, env["error"]["reason"]) == (22, "missing_required_fields")
    assert env["error"]["details"]["required_fields"] == [
        {"name": "Story Points", "kind": "number"}
    ]
    validator("cli/error.json").validate(env)
    code, plan = cli.doc("--dry-run", "create", *BASE, "t", "--set", "estimate=3")
    assert code == 0 and plan["steps"][0]["kind"] == "create"
    assert cli.posts() == []


def test_the_subtask_without_a_parent_is_rejected_by_the_provider(cli: Cli) -> None:
    argv = ("--project", "DEMO", "--type", "Sub-task", "--title", "child", "--dedupe-key", "st")
    code, env = cli.create(*argv)
    assert (code, env["error"]["code"]) == (12, "rejected")
    assert "parent" in env["error"]["details"]["field_errors"]
    assert env["error"]["details"]["attempt_id"] == ATTEMPT
    assert len(cli.posts()) == 1


def test_a_task_under_a_task_parent_is_rejected_with_errors_parent(cli: Cli) -> None:
    argv = (*BASE, "child", "--parent", "DEMO-1", "--dedupe-key", "tp")
    code, env = cli.create(*argv)
    assert (code, env["error"]["code"]) == (12, "rejected")
    assert "parent" in env["error"]["details"]["field_errors"]


def test_a_subtask_under_a_task_parent_is_created(cli: Cli) -> None:
    argv = ("--project", "DEMO", "--type", "Sub-task", "--title", "c", "--parent", "demo-1")
    code, result = cli.create(*argv, "--dedupe-key", "sp")
    assert code == 0, result
    assert body(cli.posts()[0])["parent"] == {"key": "DEMO-1"}
    assert cli.fake.tickets[result["key"]].parent == "DEMO-1"


def test_assign_me_none_and_an_account(cli: Cli) -> None:
    for i, (flag, want) in enumerate(
        [("me", ME_ACCOUNT), ("none", None), (OTHER_ACCOUNT, OTHER_ACCOUNT)]
    ):
        code, result = cli.create(*BASE, f"a{i}", "--assign", flag, "--dedupe-key", f"as{i}")
        assert code == 0, result
        assert body(cli.posts()[-1])["assignee"] == {"accountId": want}
        assert cli.fake.tickets[result["key"]].assignee == want
    code, result = cli.create(*BASE, "plain", "--dedupe-key", "as9")
    assert "assignee" not in body(cli.posts()[-1])


@pytest.mark.parametrize("bad", ["ME", "None", "a b", ""])
def test_assign_grammar_is_exit_2(cli: Cli, bad: str) -> None:
    code, env = cli.doc("--dry-run", "create", *BASE, "t", "--assign", bad)
    assert code == 2 and env["error"]["reason"] == "invalid_argument"


def test_labels_go_in_the_body_with_the_dedupe_label(cli: Cli) -> None:
    code, result = cli.create(
        *BASE, "L", "--label", "alpha", "--label", "beta", "--dedupe-key", "lb"
    )
    assert code == 0
    labels = body(cli.posts()[0])["labels"]
    assert labels[:2] == ["alpha", "beta"] and re.fullmatch(r"tc-[0-9a-f]{16}", labels[2])
    assert set(cli.fake.tickets[result["key"]].labels) == set(labels)
    code, plan = cli.doc(
        "--dry-run", "create", *BASE, "L", "--label", "alpha", "--dedupe-key", "lb2"
    )
    (change,) = [c for c in plan["changes"] if c["name"] == "labels"]
    assert change["to"] == ["alpha"]


@pytest.mark.parametrize("label", ["tc-0123456789abcdef", "has space", "x" * 256, ""])
def test_bad_labels_are_invalid_value(cli: Cli, label: str) -> None:
    code, env = cli.doc("--dry-run", "create", *BASE, "t", "--label", label)
    assert (code, env["error"]["reason"]) == (22, "invalid_value")


def test_a_provider_400_on_labels_says_to_create_without_a_key(
    cli: Cli, monkeypatch: pytest.MonkeyPatch
) -> None:
    def refuse(labels: list[Any]) -> list[str]:
        raise ProviderError(400, errors={"labels": "Field 'labels' cannot be set."})

    monkeypatch.setattr(cli.fake, "_check_labels", refuse)
    code, env = cli.create(*BASE, "t", "--dedupe-key", "lbl")
    assert (code, env["error"]["code"]) == (12, "rejected")
    assert "without --dedupe-key" in env["error"]["message"]
    assert "labels" in env["error"]["details"]["field_errors"]


def test_set_fields_priority_and_estimate_and_never_a_sprint(cli: Cli) -> None:
    code, result = cli.create(
        *BASE, "S", "--set", "priority=High", "--set", "estimate=5", "--dedupe-key", "sf"
    )
    assert code == 0, result
    fields = body(cli.posts()[0])
    assert fields["priority"] == {"name": "High"} and fields[F_POINTS] == 5
    assert not any("sprint" in k.lower() or k == "customfield_90003" for k in fields)
    code, env = cli.doc("--dry-run", "create", *BASE, "S", "--set", "sprint=1")
    assert code == 2 and env["error"]["reason"] == "invalid_combination"
    for typed in ("Sprint", "Parent", "Labels", "Reporter", "description", "Summary"):
        code, env = cli.doc("--dry-run", "create", *BASE, "S", "--set", f"{typed}=x")
        assert code == 2, typed


def test_set_unknown_field_and_bad_value(cli: Cli) -> None:
    code, env = cli.doc("--dry-run", "create", *BASE, "S", "--set", "Nonesuch=1")
    assert (code, env["error"]["reason"]) == (22, "unknown_field")
    code, env = cli.doc("--dry-run", "create", *BASE, "S", "--set", "estimate=abc")
    assert (code, env["error"]["reason"]) == (22, "invalid_value")


def test_the_description_has_the_attribution_for_auto_and_none_for_human_without_body(
    cli: Cli,
) -> None:
    code, result = cli.create(*BASE, "D", "--dedupe-key", "d1")  # default actor is auto
    doc = body(cli.posts()[0])["description"]
    assert doc["type"] == "doc" and doc["content"][0]["type"] == "paragraph"
    code, result = cli.create(*BASE, "D2", "--dedupe-key", "d2", pre=("--actor", "human:tester"))
    assert code == 0 and "description" not in body(cli.posts()[1])
    assert "description" not in [
        c["name"]
        for c in cli.doc("--actor", "human:tester", "--dry-run", "create", *BASE, "D3")[1][
            "changes"
        ]
    ]


def test_the_body_file_follows_the_attribution_paragraph(cli: Cli, tmp_path: Path) -> None:
    f = tmp_path / "b.md"
    f.write_text("hello **world**\n")
    code, result = cli.create(*BASE, "B", "--body-file", str(f), "--dedupe-key", "b1")
    assert code == 0, result
    doc = body(cli.posts()[0])["description"]
    assert len(doc["content"]) == 2 and doc["content"][1]["content"][0]["text"] == "hello "
    code, plan = cli.doc("--dry-run", "create", *BASE, "B", "--body-file", str(f))
    (desc,) = [c for c in plan["changes"] if c["name"] == "description"]
    assert "hello" in desc["to"]


def test_title_rules(cli: Cli, tmp_path: Path) -> None:
    for title in ("", "   ", "x" * 256, "two\nlines", "bell\x07"):
        code, env = cli.doc(
            "--dry-run", "create", "--project", "DEMO", "--type", "Task", "--title", title
        )
        assert (code, env["error"]["reason"]) == (22, "invalid_value"), repr(title)
    f = tmp_path / "t.txt"
    f.write_text("from a file\n")
    code, plan = cli.doc(
        "--dry-run", "create", "--project", "DEMO", "--type", "Task", "--title-file", str(f)
    )
    assert code == 0
    code, env = cli.doc("--dry-run", "create", "--project", "DEMO", "--type", "Task")
    assert (code, env["error"]["reason"]) == (2, "invalid_combination")
    code, env = cli.doc(
        "--dry-run",
        "create",
        "--project",
        "DEMO",
        "--type",
        "Task",
        "--title",
        "a",
        "--title-file",
        str(f),
    )
    assert (code, env["error"]["reason"]) == (2, "invalid_combination")


def test_a_missing_type_is_missing_option(cli: Cli) -> None:
    code, env = cli.doc("--dry-run", "create", "--project", "DEMO", "--title", "t")
    assert (code, env["error"]["reason"]) == (2, "missing_option")


def test_the_project_defaults_to_the_sites_only_project(cli: Cli) -> None:
    code, env = cli.doc("--dry-run", "create", "--type", "Task", "--title", "t")
    assert (code, env["error"]["reason"]) == (2, "missing_option")
    cli.config.write_text(cli.config.read_text() + "    projects: [DEMO]\n")
    code, plan = cli.doc("--dry-run", "create", "--type", "Task", "--title", "t")
    assert code == 0 and plan["steps"][0]["project"] == "DEMO"
    cli.config.write_text(cli.config.read_text().replace("[DEMO]", "[DEMO, OTHER]"))
    code, env = cli.doc("--dry-run", "create", "--type", "Task", "--title", "t")
    assert (code, env["error"]["reason"]) == (2, "missing_option")


def test_an_unknown_project_is_not_found(cli: Cli) -> None:
    code, env = cli.doc(
        "--dry-run", "create", "--project", "NOPE", "--type", "Task", "--title", "t"
    )
    assert (code, env["error"]["code"]) == (10, "not_found")


def test_without_yes_a_non_interactive_run_needs_confirmation(cli: Cli) -> None:
    code, env = cli.doc("create", *BASE, "t", "--dedupe-key", "c1")
    assert (code, env["error"]["code"]) == (13, "confirmation_required")
    assert cli.posts() == []


def test_a_secret_in_a_title_label_or_body_is_refused_and_never_echoed(
    cli: Cli, tmp_path: Path
) -> None:
    secret = SITE_TOKEN
    f = tmp_path / "s.md"
    f.write_text(f"leak {secret}")
    for argv in (
        [*BASE, f"x {secret}"],
        [*BASE, "t", "--label", secret],
        [*BASE, "t", "--body-file", str(f)],
        [*BASE, "t", "--dedupe-key", secret],
    ):
        code, out, err = cli.run("--json", "--dry-run", "create", *argv)
        assert SITE_TOKEN not in out + err
        assert code in (2, 22), argv
    assert cli.posts() == []


def test_hostile_values_are_sent_as_data(cli: Cli) -> None:
    title = 'x" OR project = "OTHER'
    code, result = cli.create(*BASE, title, "--dedupe-key", "h")
    assert code == 0 and cli.fake.tickets[result["key"]].summary == title


def test_an_outcome_unknown_create_is_never_resent_and_the_rerun_finds_it(
    cli: Cli, monkeypatch: pytest.MonkeyPatch
) -> None:
    from tests.test_dedupe_flow import Virtual

    virtual = Virtual(cli.fake)
    monkeypatch.setattr(createcmd, "make_clock", virtual.clock)
    code, plan = cli.doc("--dry-run", "create", *BASE, "Lost", "--dedupe-key", "lost")
    # The ticket lands, but the answer is cut: the outcome is unknown.
    cli.fake.inject(Fault("truncated", method="POST", path="/issue$"))
    code, env = cli.doc(
        "--yes",
        "--expect-plan",
        plan["plan_sha256"],
        "create",
        *BASE,
        "Lost",
        "--dedupe-key",
        "lost",
    )
    assert (code, env["error"]["code"]) == (12, "outcome_unknown")
    assert env["error"]["retryable"] is True and len(cli.posts()) == 1
    assert len(cli.fake.tickets) == 4  # the three seeded and the landed one
    code, result = cli.doc(
        "--yes",
        "--expect-plan",
        plan["plan_sha256"],
        "create",
        *BASE,
        "Lost",
        "--dedupe-key",
        "lost",
    )
    assert code == 0 and result["reused"] is True and result["result"] == "unchanged"
    assert len(cli.posts()) == 1 and len(cli.fake.tickets) == 4
    assert virtual.waits and set(virtual.waits) <= {2.0, 5.0}  # the guard polled the lag


def test_the_hook_payloads_come_from_the_real_runner_and_match_the_schemas(
    cli: Cli, monkeypatch: pytest.MonkeyPatch
) -> None:
    cli.config.write_text(
        cli.config.read_text()
        + "hooks:\n  pre-create:\n    - cmd: ignored\n  post-create:\n    - cmd: ignored\n"
    )
    seen: dict[str, bytes] = {}

    def fake_run(argv: list[str], *, stdin: bytes, env: dict[str, str], **kw: Any) -> HookRun:
        seen[env["TICKETCTL_HOOK"]] = stdin
        return HookRun(1, 0, b"", b"", False, False, False)

    monkeypatch.setattr(hooks, "run_hook", fake_run)
    code, result = cli.create(
        *BASE,
        f"title {BODY_MARK}",
        "--assign",
        "me",
        "--set",
        "priority=High",
        "--dedupe-key",
        "h1",
    )
    assert code == 0 and result["result"] == "ok", result
    assert set(seen) == {"pre-create", "post-create"}
    for name, raw in seen.items():
        payload = json.loads(raw)
        validator(f"hooks/{name}.json").validate(payload)
        assert BODY_MARK not in raw.decode()
        assert payload["project"] == "DEMO" and payload["plan"]["dedupe_key"] == "h1"
        assert payload["create"] == {"type": "Task", "parent": None, "assign": "me"}
        assert "priority" in payload["plan"]["fields_changed"]
        assert_golden("hooks", name, "ok", cli.norm(payload))
    post = json.loads(seen["post-create"])
    assert post["object_id"] == result["key"] and post["changed"] is True


def test_the_audit_record_has_the_key_and_no_title(cli: Cli, tmp_path: Path) -> None:
    code, result = cli.create(*BASE, f"secret title {BODY_MARK}", "--dedupe-key", "au")
    assert code == 0
    audit_text = (tmp_path / "state" / "ticketctl" / "audit.jsonl").read_text()
    assert BODY_MARK not in audit_text
    records = [json.loads(line) for line in audit_text.splitlines()]
    assert [r["result"] for r in records] == ["attempted", "ok"]
    assert (
        records[0]["inputs"]["dedupe_key"] == "au" and records[1]["target"]["key"] == result["key"]
    )


def test_a_landed_create_with_an_unusable_answer_prints_a_null_key(
    cli: Cli, monkeypatch: pytest.MonkeyPatch
) -> None:
    real = cli.fake._create

    def odd(call: Any) -> Out:
        real(call)
        return Out(201, {"id": "1", "key": "bad key"}, applied=True)

    monkeypatch.setattr(cli.fake, "_create", odd)
    cli.fake._routes = []
    cli.fake._build_routes()
    code, result = cli.create(*BASE, "t", "--dedupe-key", "nk")
    assert code == 0 and result["key"] is None and result["url"] is None
    assert result["changed"] is True
