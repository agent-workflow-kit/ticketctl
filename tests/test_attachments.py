"""``attachment-add`` and ``attachment-download`` against the fake provider (issue #42)."""

from __future__ import annotations

import hashlib
import json
import os
import stat
from pathlib import Path
from typing import Any

import pytest

from tests.fake_provider import ATTACHMENT_LIMIT, SITE_TOKEN, FakeProvider, Fault
from tests.golden import assert_golden, validator
from tests.test_comment import ATTEMPT, SITE_ENV_VAR, Cli
from ticketctl import attachmentcmd, exportcmd, hooks
from ticketctl.errors import TicketctlError
from ticketctl.gateway import HookRun
from ticketctl.writeflow import Seams

KEY = "DEMO-2"
OTHER = "DEMO-1"
BINARY = b"BIN-DATA\r\n\x00\xff\xfe\r\n--not-a-boundary\r\n" + bytes(range(256))
MARK = "zq7x-attach-marker-4f9c2e"  # must never reach a hook payload


@pytest.fixture
def cli(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: Any, fake: FakeProvider) -> Cli:
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    monkeypatch.setenv(SITE_ENV_VAR, SITE_TOKEN)
    monkeypatch.setenv("PATH", "/nonexistent")
    monkeypatch.setattr(attachmentcmd, "make_seams", lambda: Seams(attempt_id=lambda: ATTEMPT))
    (tmp_path / "files").mkdir()
    (tmp_path / "out").mkdir()
    return Cli(tmp_path, fake, capsys)


def make_file(cli: Cli, name: str = "report.bin", data: bytes = BINARY) -> Path:
    path = cli.root / "files" / name
    path.write_bytes(data)
    return path


def attachments(cli: Cli, key: str = KEY) -> list[dict[str, Any]]:
    return cli.fake.tickets[key].attachments


def add(cli: Cli, path: Path | str, *extra: str, key: str = KEY) -> tuple[int, dict[str, Any]]:
    """Plan with --dry-run, then apply that plan with --yes."""
    argv = ("attachment-add", key, "--file", str(path), *extra)
    code, plan = cli.doc("--dry-run", *argv)
    if code != 0:
        return code, plan
    return cli.doc("--yes", "--expect-plan", plan["plan_sha256"], *argv)


def posts(cli: Cli) -> list[Any]:
    return [c for c in cli.fake.calls if c.method == "POST" and c.path.endswith("/attachments")]


def content_gets(cli: Cli) -> list[Any]:
    return [c for c in cli.fake.calls if "/attachment/content/" in c.path]


# --- attachment-add: the happy path -------------------------------------------------------------


def test_uploads_the_exact_bytes_under_the_base_name(cli: Cli) -> None:
    path = make_file(cli)
    code, result = add(cli, path)
    assert code == 0, result
    assert result["action"] == "attachment_add" and result["result"] == "ok"
    assert result["changed"] is True and result["comment_id"] is None
    assert result["steps"] == [{"kind": "add_attachment", "status": "done"}]
    assert result["attempt_id"] == ATTEMPT and result["attachment_id"].isdigit()
    validator("cli/attachment-add.json").validate(result)
    assert_golden("cli", "attachment-add", "ok", cli.norm(result))
    (stored,) = attachments(cli)
    assert stored["id"] == result["attachment_id"]
    assert stored["filename"] == "report.bin" and stored["_content"] == BINARY
    (post,) = posts(cli)
    assert (post.headers.get("x-atlassian-token") or [""])[0] == "no-check"


def test_the_dry_run_plan_names_the_file_by_hash_and_size_and_sends_nothing(cli: Cli) -> None:
    path = make_file(cli)
    code, plan = cli.doc("--dry-run", "attachment-add", KEY, "--file", str(path))
    assert code == 0, plan
    digest = hashlib.sha256(BINARY).hexdigest()
    assert plan["action"] == "attachment_add" and plan["dedupe_key"] is None
    assert plan["steps"] == [
        {"kind": "add_attachment", "file_sha256": digest, "file_bytes": len(BINARY)}
    ]
    assert plan["changes"] == [
        {
            "name": "attachment",
            "from": None,
            "to": f"report.bin ({len(BINARY)} bytes, sha256 {digest})",
        }
    ]
    validator("cli/attachment-add.json").validate(plan)
    assert_golden("cli", "attachment-add", "dry-run", cli.norm(plan))
    assert posts(cli) == [] and attachments(cli) == []


def test_non_ascii_file_names_are_sent_as_given(cli: Cli) -> None:
    path = make_file(cli, "résumé ☃.txt", b"hello")
    code, result = add(cli, path)
    assert code == 0, result
    assert attachments(cli)[0]["filename"] == "résumé ☃.txt"


def test_the_ticket_key_is_canonicalized_and_the_attachment_listed_by_get(cli: Cli) -> None:
    path = make_file(cli)
    code, result = add(cli, path, key=KEY.lower())
    assert code == 0 and result["key"] == KEY
    code, doc = cli.doc("get", KEY, "--fields", "attachments")
    assert code == 0, doc
    (item,) = doc["tickets"][0]["attachments"] if "tickets" in doc else doc["attachments"]
    assert item["name"] == "report.bin" and item["size"] == len(BINARY)


# --- attachment-add: idempotency by content -----------------------------------------------------


def test_a_rerun_after_success_is_unchanged_and_names_the_existing_attachment(cli: Cli) -> None:
    path = make_file(cli)
    code, first = add(cli, path)
    assert code == 0, first
    code, again = add(cli, path)
    assert code == 0, again
    assert again["result"] == "unchanged" and again["changed"] is False
    assert again["steps"] == [] and again["attempt_id"] is None
    assert again["attachment_id"] == first["attachment_id"]
    validator("cli/attachment-add.json").validate(again)
    assert_golden("cli", "attachment-add", "unchanged", cli.norm(again))
    assert len(posts(cli)) == 1 and len(attachments(cli)) == 1


def test_same_name_and_size_but_other_bytes_is_uploaded(cli: Cli) -> None:
    path = make_file(cli, "a.bin", b"AAAA")
    assert add(cli, path)[0] == 0
    path.write_bytes(b"BBBB")  # the same name, the same size, other content
    code, result = add(cli, path)
    assert code == 0 and result["result"] == "ok"
    assert [a["_content"] for a in attachments(cli)] == [b"AAAA", b"BBBB"]


def test_same_name_but_another_size_is_uploaded_without_reading_content(cli: Cli) -> None:
    cli.fake.attach(KEY, "a.bin", b"short")
    path = make_file(cli, "a.bin", b"a longer text")
    code, result = add(cli, path)
    assert code == 0 and result["result"] == "ok"
    assert content_gets(cli) == []


def test_another_name_with_the_same_bytes_is_uploaded(cli: Cli) -> None:
    cli.fake.attach(KEY, "other.bin", BINARY)
    code, result = add(cli, make_file(cli))
    assert code == 0 and result["result"] == "ok" and len(attachments(cli)) == 2


def test_an_identical_attachment_added_by_someone_else_counts(cli: Cli) -> None:
    existing = cli.fake.attach(KEY, "report.bin", BINARY, author="acct-0002")
    code, result = add(cli, make_file(cli))
    assert code == 0 and result["result"] == "unchanged"
    assert result["attachment_id"] == existing


def test_too_many_candidates_cannot_prove_absence(cli: Cli) -> None:
    for _ in range(attachmentcmd.MAX_CANDIDATES + 1):
        cli.fake.attach(KEY, "a.bin", b"XXXX")
    code, env = add(cli, make_file(cli, "a.bin", b"YYYY"))
    assert (code, env["error"]["code"]) == (12, "lookup_incomplete")
    assert posts(cli) == []


def test_a_candidate_that_cannot_be_read_stops_the_upload(cli: Cli) -> None:
    cli.fake.attach(KEY, "a.bin", b"XXXX")
    cli.fake.inject(Fault("status", status=404, method="GET", path="/attachment/content/"))
    code, env = cli.doc(
        "--dry-run", "attachment-add", KEY, "--file", str(make_file(cli, "a.bin", b"YYYY"))
    )
    assert code in (10, 12), env
    assert posts(cli) == []


def test_a_lost_answer_is_outcome_unknown_and_the_rerun_finds_the_file(cli: Cli) -> None:
    path = make_file(cli)
    cli.fake.inject(Fault("reset", method="POST", path="/attachments", after_apply=True))
    code, env = add(cli, path)
    assert (code, env["error"]["code"]) == (12, "outcome_unknown")
    assert env["error"]["retryable"] is True and env["error"]["outcome_unknown"] is True
    assert len(posts(cli)) == 1 and len(attachments(cli)) == 1  # the upload landed
    code, again = add(cli, path)
    assert code == 0 and again["result"] == "unchanged"
    assert again["attachment_id"] == attachments(cli)[0]["id"]
    assert len(posts(cli)) == 1  # never sent twice


# --- attachment-add: refusals -------------------------------------------------------------------


def refused(cli: Cli, path: Path | str) -> dict[str, Any]:
    code, env = cli.doc("--dry-run", "attachment-add", KEY, "--file", str(path))
    assert (code, env["error"]["code"], env["error"]["reason"]) == (
        22,
        "invalid_request",
        "invalid_file",
    )
    assert posts(cli) == [] and not [c for c in cli.fake.calls if c.method != "GET"]
    return env


def test_a_missing_file_is_invalid_file_before_any_request(cli: Cli) -> None:
    before = len(cli.fake.calls)
    refused(cli, cli.root / "files" / "nope.bin")
    assert len(cli.fake.calls) == before  # nothing at all, not even a read


def test_a_symbolic_link_is_refused(cli: Cli) -> None:
    target = make_file(cli, "real.bin")
    link = cli.root / "files" / "link.bin"
    link.symlink_to(target)
    refused(cli, link)


def test_a_directory_a_fifo_and_a_device_are_refused(cli: Cli) -> None:
    refused(cli, cli.root / "files")
    fifo = cli.root / "files" / "pipe.bin"
    os.mkfifo(fifo)
    refused(cli, fifo)
    refused(cli, "/dev/null")


def test_an_empty_file_is_refused(cli: Cli) -> None:
    refused(cli, make_file(cli, "empty.bin", b""))


def test_a_file_over_the_cap_is_refused_without_being_sent(cli: Cli) -> None:
    path = cli.root / "files" / "big.bin"
    with path.open("wb") as handle:
        handle.truncate(attachmentcmd.MAX_ATTACHMENT_BYTES + 1)  # sparse
    refused(cli, path)


def test_a_file_exactly_at_the_cap_is_accepted(cli: Cli) -> None:
    path = cli.root / "files" / "edge.bin"
    path.write_bytes(b"\x01" * attachmentcmd.MAX_ATTACHMENT_BYTES)
    code, result = add(cli, path)
    assert code == 0, result
    assert len(attachments(cli)[0]["_content"]) == attachmentcmd.MAX_ATTACHMENT_BYTES


def test_the_fake_and_the_tool_agree_on_the_cap() -> None:
    assert attachmentcmd.MAX_ATTACHMENT_BYTES == ATTACHMENT_LIMIT


@pytest.mark.parametrize(
    "name",
    ['quo"te.txt', "back\\slash.txt", "new\nline.txt", "tab\t.txt", " lead.txt", "trail.txt "],
)
def test_a_file_name_that_is_not_plain_is_refused(cli: Cli, name: str) -> None:
    refused(cli, make_file(cli, name, b"x"))


@pytest.mark.parametrize(
    "name",
    ["", ".", "..", "a‮b.txt", "zero​width.txt", "x" * 256, "\ud800.txt", "nul\x00.txt"],
)
def test_check_name_refuses_what_the_sanitizer_would_change(name: str) -> None:
    with pytest.raises(TicketctlError) as exc:
        attachmentcmd.check_name(name)
    assert exc.value.reason == "invalid_file"


def test_a_plain_name_passes_check_name() -> None:
    assert attachmentcmd.check_name("a b-c_d.e (1).tar.gz") == "a b-c_d.e (1).tar.gz"
    assert attachmentcmd.check_name("x" * 255) == "x" * 255


def test_a_file_that_holds_the_token_is_refused(cli: Cli) -> None:
    path = make_file(cli, "notes.txt", b"before " + SITE_TOKEN.encode() + b" after")
    code, env = cli.doc("--dry-run", "attachment-add", KEY, "--file", str(path))
    assert (code, env["error"]["reason"]) == (22, "secret_in_body")
    assert SITE_TOKEN not in json.dumps(env) and posts(cli) == []


def test_the_file_argument_is_required(cli: Cli) -> None:
    code, env = cli.doc("--dry-run", "attachment-add", KEY)
    assert (code, env["error"]["reason"]) == (2, "missing_option")


def test_a_dedupe_key_is_not_a_flag_of_this_command(cli: Cli) -> None:
    code, env = cli.doc("--dry-run", "attachment-add", KEY, "--file", "x", "--dedupe-key", "k")
    assert code == 2


def test_a_file_changed_after_the_plan_is_a_different_plan(cli: Cli) -> None:
    path = make_file(cli, "a.bin", b"first")
    code, plan = cli.doc("--dry-run", "attachment-add", KEY, "--file", str(path))
    path.write_bytes(b"other")
    code, env = cli.doc(
        "--yes", "--expect-plan", plan["plan_sha256"], "attachment-add", KEY, "--file", str(path)
    )
    assert (code, env["error"]["code"]) == (20, "stale_ticket")
    assert posts(cli) == []


def test_without_yes_a_driven_run_asks_for_confirmation_and_sends_nothing(cli: Cli) -> None:
    code, env = cli.doc("attachment-add", KEY, "--file", str(make_file(cli)))
    assert (code, env["error"]["code"]) == (13, "confirmation_required")
    assert posts(cli) == []


def test_yes_needs_expect_plan_but_no_dedupe_key(cli: Cli) -> None:
    path = str(make_file(cli))
    code, env = cli.doc("--yes", "attachment-add", KEY, "--file", path)
    assert (code, env["error"]["reason"]) == (2, "yes_requires_expect_plan")
    code, plan = cli.doc("--dry-run", "attachment-add", KEY, "--file", path)
    code, env = cli.doc(
        "--yes", "--expect-plan", plan["plan_sha256"], "attachment-add", KEY, "--file", path
    )
    assert code == 0, env  # no dedupe key was needed


def test_read_only_mode_refuses_before_any_request(
    cli: Cli, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("TICKETCTL_READ_ONLY", "1")
    seen = len(cli.fake.calls)
    code, env = cli.doc(
        "--yes", "--expect-plan", "0" * 64, "attachment-add", KEY, "--file", str(make_file(cli))
    )
    assert (code, env["error"]["code"]) == (20, "read_only")
    assert len(cli.fake.calls) == seen


@pytest.mark.parametrize(
    "fault, code, error",
    [
        (Fault("status", status=413, method="POST", path="/attachments"), 12, "rejected"),
        (Fault("status", status=400, method="POST", path="/attachments"), 12, "rejected"),
        (Fault("status", status=403, method="POST", path="/attachments"), 12, "forbidden"),
        (Fault("status", status=404, method="POST", path="/attachments"), 10, "not_found"),
    ],
)
def test_a_provider_refusal_maps_to_its_code_and_nothing_is_resent(
    cli: Cli, fault: Fault, code: int, error: str
) -> None:
    cli.fake.inject(fault)
    got, env = add(cli, make_file(cli))
    assert (got, env["error"]["code"]) == (code, error)
    assert len(posts(cli)) == 1 and attachments(cli) == []
    validator("cli/error.json").validate(env)


def test_the_provider_size_limit_is_a_rejection(cli: Cli, monkeypatch: pytest.MonkeyPatch) -> None:
    import tests.fake_provider as fp

    monkeypatch.setattr(fp, "ATTACHMENT_LIMIT", 10)
    got, env = add(cli, make_file(cli, "b.bin", b"x" * 11))
    assert (got, env["error"]["code"]) == (12, "rejected")


def test_a_ticket_that_does_not_exist_is_not_found(cli: Cli) -> None:
    code, env = cli.doc("--dry-run", "attachment-add", "DEMO-999", "--file", str(make_file(cli)))
    assert (code, env["error"]["code"]) == (10, "not_found")


# --- attachment-add: audit and hooks ------------------------------------------------------------


def _audit(cli: Cli) -> list[dict[str, Any]]:
    path = cli.root / "state" / "ticketctl" / "audit.jsonl"
    return [json.loads(line) for line in path.read_text().splitlines()]


def test_the_audit_holds_the_hash_and_never_the_name_or_bytes(cli: Cli) -> None:
    code, result = add(cli, make_file(cli))
    assert code == 0, result
    records = _audit(cli)
    assert [(r["action"], r["step_kind"], r["result"]) for r in records] == [
        ("attachment_add", "add_attachment", "attempted"),
        ("attachment_add", "add_attachment", "ok"),
    ]
    for record in records:
        validator("audit.json").validate(record)
    assert records[0]["inputs"]["bodies"] == [
        {"sha256": hashlib.sha256(BINARY).hexdigest(), "bytes": len(BINARY)}
    ]
    assert records[1]["provider_ids"] == [result["attachment_id"]]
    assert "report.bin" not in json.dumps(records)


def test_hooks_have_their_own_family_and_carry_neither_name_nor_content(
    cli: Cli, monkeypatch: pytest.MonkeyPatch
) -> None:
    cli.config.write_text(
        cli.config.read_text()
        + "hooks:\n  pre-attachment-add:\n    - cmd: ignored\n  post-attachment-add:\n"
        + "    - cmd: ignored\n  pre-comment:\n    - cmd: other\n"
    )
    seen: dict[str, bytes] = {}

    def fake_run(argv: list[str], *, stdin: bytes, env: dict[str, str], **kw: Any) -> HookRun:
        seen[env["TICKETCTL_HOOK"]] = stdin
        assert env["TICKETCTL_ID"] == KEY
        return HookRun(1, 0, b"", b"", False, False, False)

    monkeypatch.setattr(hooks, "run_hook", fake_run)
    code, result = add(cli, make_file(cli, f"{MARK}.bin"))
    assert code == 0, result
    assert set(seen) == {"pre-attachment-add", "post-attachment-add"}
    for name, raw in seen.items():
        payload = json.loads(raw)
        validator(f"hooks/{name}.json").validate(payload)
        assert MARK not in raw.decode() and payload["object_id"] == KEY
        assert payload["plan"]["action"] == "attachment_add"
        assert payload["plan"]["steps"] == ["add_attachment"]
        assert payload["body"] == [
            {"sha256": hashlib.sha256(BINARY).hexdigest(), "bytes": len(BINARY)}
        ]
        assert_golden("hooks", name, "ok", cli.norm(payload))
    assert json.loads(seen["post-attachment-add"])["provider_ids"] == [result["attachment_id"]]


def test_a_pre_hook_veto_stops_the_upload(cli: Cli, monkeypatch: pytest.MonkeyPatch) -> None:
    cli.config.write_text(
        cli.config.read_text() + "hooks:\n  pre-attachment-add:\n    - cmd: ignored\n"
    )
    monkeypatch.setattr(
        hooks, "run_hook", lambda *a, **k: HookRun(1, 1, b"", b"no", False, False, False)
    )
    code, env = add(cli, make_file(cli))
    assert (code, env["error"]["code"]) == (11, "vetoed")
    assert posts(cli) == []


def test_the_hook_flag_accepts_the_new_family(cli: Cli) -> None:
    code, env = cli.doc(
        "--hook", "pre-attachment-add=exit 1", "--dry-run", "attachment-add", KEY,
        "--file", str(make_file(cli)),
    )  # fmt: skip
    assert code == 0, env


def test_the_confirmation_dialog_shows_the_name_size_and_hash(
    cli: Cli, monkeypatch: pytest.MonkeyPatch
) -> None:
    shown: list[dict[str, Any]] = []
    monkeypatch.setattr(
        attachmentcmd,
        "make_seams",
        lambda: Seams(
            attempt_id=lambda: ATTEMPT,
            interactive=lambda: True,
            confirm=lambda p: shown.append(p) or True,
        ),
    )
    code, out, _ = cli.run(
        "--json", "--actor", "human:tester", "attachment-add", KEY, "--file", str(make_file(cli))
    )
    assert code == 0, out
    (plan,) = shown
    digest = hashlib.sha256(BINARY).hexdigest()
    assert plan["changes"][0]["to"] == f"report.bin ({len(BINARY)} bytes, sha256 {digest})"


# --- attachment-download -----------------------------------------------------------------------


def download(cli: Cli, aid: str, dest: Path | str, *extra: str, key: str = KEY):
    return cli.doc("attachment-download", key, aid, "--to", str(dest), *extra)


def test_downloads_the_bytes_to_a_new_file_with_mode_0600(cli: Cli) -> None:
    aid = cli.fake.attach(KEY, "report.bin", BINARY)
    dest = cli.root / "out" / "copy.bin"
    code, result = download(cli, aid, dest)
    assert code == 0, result
    assert dest.read_bytes() == BINARY and stat.S_IMODE(dest.stat().st_mode) == 0o600
    digest = hashlib.sha256(BINARY).hexdigest()
    assert result == {
        "key": KEY,
        "site": "main",
        "id": aid,
        "name": "report.bin",
        "mime": "application/octet-stream",
        "size": len(BINARY),
        "sha256": digest,
        "path": str(dest),
    }
    validator("cli/attachment-download.json").validate(result)
    golden = dict(result, path="<dest>")
    assert_golden("cli", "attachment-download", "ok", cli.norm(golden))


def test_the_content_is_requested_with_redirect_false_and_no_other_url(cli: Cli) -> None:
    aid = cli.fake.attach(KEY, "a.bin", b"x")
    assert download(cli, aid, cli.root / "out" / "a.bin")[0] == 0
    gets = [c for c in cli.fake.calls if c.method == "GET" and "/myself" not in c.path]
    assert [c.path for c in gets] == [
        f"/rest/api/3/issue/{KEY}",
        f"/rest/api/3/attachment/content/{aid}",
    ]
    assert gets[1].query == {"redirect": ["false"]}


def test_a_hostile_provider_name_never_builds_the_path_and_is_printed_sanitized(cli: Cli) -> None:
    hostile = "../../escape‮\x1b[31m.txt"
    aid = cli.fake.attach(KEY, hostile, b"data")
    dest = cli.root / "out" / "safe.txt"
    code, result = download(cli, aid, dest)
    assert code == 0, result
    assert "‮" not in result["name"] and "\x1b" not in result["name"]
    assert sorted(p.name for p in (cli.root / "out").iterdir()) == ["safe.txt"]
    assert not (cli.root.parent / "escape").exists()


def test_an_existing_path_is_never_overwritten_and_nothing_is_downloaded(cli: Cli) -> None:
    aid = cli.fake.attach(KEY, "a.bin", b"new")
    dest = cli.root / "out" / "keep.bin"
    dest.write_bytes(b"precious")
    code, env = download(cli, aid, dest)
    assert (code, env["error"]["code"]) == (20, "already_exists")
    assert dest.read_bytes() == b"precious" and content_gets(cli) == []


def test_a_symbolic_link_as_the_target_is_refused_and_not_followed(cli: Cli) -> None:
    aid = cli.fake.attach(KEY, "a.bin", b"new")
    victim = cli.root / "out" / "victim.txt"
    victim.write_bytes(b"safe")
    link = cli.root / "out" / "link.bin"
    link.symlink_to(victim)
    code, env = download(cli, aid, link)
    assert (code, env["error"]["code"]) == (20, "already_exists")
    assert victim.read_bytes() == b"safe"
    dangling = cli.root / "out" / "dangling.bin"
    dangling.symlink_to(cli.root / "out" / "created-through-link.txt")
    code, env = download(cli, aid, dangling)
    assert code == 20 and not (cli.root / "out" / "created-through-link.txt").exists()


def test_a_missing_parent_directory_is_invalid_file(cli: Cli) -> None:
    aid = cli.fake.attach(KEY, "a.bin", b"x")
    code, env = download(cli, aid, cli.root / "no-such-dir" / "a.bin")
    assert (code, env["error"]["reason"]) == (22, "invalid_file")


def test_an_attachment_of_another_ticket_is_not_found_and_not_downloaded(cli: Cli) -> None:
    aid = cli.fake.attach(OTHER, "a.bin", b"secret")
    dest = cli.root / "out" / "a.bin"
    code, env = download(cli, aid, dest)  # asked under KEY, lives on OTHER
    assert (code, env["error"]["code"]) == (10, "not_found")
    assert not dest.exists() and content_gets(cli) == []


def test_an_unknown_attachment_id_is_not_found(cli: Cli) -> None:
    code, env = download(cli, "99999", cli.root / "out" / "a.bin")
    assert (code, env["error"]["code"]) == (10, "not_found")


def test_an_attachment_over_the_cap_is_refused_before_the_download(cli: Cli) -> None:
    aid = cli.fake.attach(KEY, "big.bin", b"x")
    attachments(cli)[0]["size"] = attachmentcmd.MAX_ATTACHMENT_BYTES + 1
    dest = cli.root / "out" / "big.bin"
    code, env = download(cli, aid, dest)
    assert (code, env["error"]["reason"]) == (22, "body_too_large")
    assert not dest.exists() and content_gets(cli) == []


def test_content_longer_than_the_cap_is_refused_even_if_the_metadata_lies(cli: Cli) -> None:
    aid = cli.fake.attach(KEY, "big.bin", b"x" * (attachmentcmd.MAX_ATTACHMENT_BYTES + 1))
    attachments(cli)[0]["size"] = 5  # the metadata claims a small file
    dest = cli.root / "out" / "big.bin"
    code, env = download(cli, aid, dest)
    assert code == 22 and not dest.exists()


def test_a_size_that_differs_from_the_metadata_saves_nothing(cli: Cli) -> None:
    aid = cli.fake.attach(KEY, "a.bin", b"12345")
    attachments(cli)[0]["size"] = 4
    dest = cli.root / "out" / "a.bin"
    code, env = download(cli, aid, dest)
    assert (code, env["error"]["code"]) == (12, "provider_changed")
    assert not dest.exists()


def test_a_redirect_is_never_followed(cli: Cli) -> None:
    aid = cli.fake.attach(KEY, "a.bin", b"x")
    cli.fake.inject(Fault("status", status=303, method="GET", path="/attachment/content/"))
    dest = cli.root / "out" / "a.bin"
    code, env = download(cli, aid, dest)
    assert (code, env["error"]["code"]) == (12, "provider_changed")
    assert not dest.exists() and len(content_gets(cli)) == 1


def test_a_truncated_answer_saves_nothing(cli: Cli) -> None:
    aid = cli.fake.attach(KEY, "a.bin", b"x" * 1000)
    cli.fake.inject(Fault("truncated", method="GET", path="/attachment/content/", times=None))
    dest = cli.root / "out" / "a.bin"
    code, env = download(cli, aid, dest)
    assert code == 12 and not dest.exists()


def test_a_failed_write_removes_the_partial_file(cli: Cli, monkeypatch: pytest.MonkeyPatch) -> None:
    aid = cli.fake.attach(KEY, "a.bin", b"x")

    def boom(fd: int, data: bytes) -> None:
        os.write(fd, b"par")
        raise OSError("disk full")

    monkeypatch.setattr(attachmentcmd, "write_all", boom)
    dest = cli.root / "out" / "a.bin"
    code, _, _ = cli.run("--json", "attachment-download", KEY, aid, "--to", str(dest))
    assert code == 1 and not dest.exists()


def test_download_works_in_read_only_mode(cli: Cli, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TICKETCTL_READ_ONLY", "1")
    aid = cli.fake.attach(KEY, "a.bin", b"x")
    code, result = download(cli, aid, cli.root / "out" / "a.bin")
    assert code == 0, result
    assert [c for c in cli.fake.calls if c.method != "GET"] == []


@pytest.mark.parametrize("flag", [["--dry-run"], ["--yes"], ["--actor", "auto:x"]])
def test_write_flags_are_refused_on_a_download(cli: Cli, flag: list[str]) -> None:
    code, env = cli.doc(*flag, "attachment-download", KEY, "1", "--to", str(cli.root / "x"))
    assert (code, env["error"]["reason"]) == (2, "invalid_argument")


def test_the_target_and_a_numeric_id_are_required(cli: Cli) -> None:
    code, env = cli.doc("attachment-download", KEY, "1")
    assert (code, env["error"]["reason"]) == (2, "missing_option")
    code, env = cli.doc("attachment-download", KEY, "12a", "--to", str(cli.root / "x"))
    assert (code, env["error"]["reason"]) == (2, "invalid_argument")


def test_human_output_names_the_path_and_the_hash(cli: Cli) -> None:
    aid = cli.fake.attach(KEY, "a.bin", b"abc")
    dest = cli.root / "out" / "a.bin"
    code, out, _ = cli.run("attachment-download", KEY, aid, "--to", str(dest))
    assert code == 0
    assert str(dest) in out and hashlib.sha256(b"abc").hexdigest() in out


def test_an_empty_attachment_downloads_as_an_empty_file(cli: Cli) -> None:
    aid = cli.fake.attach(KEY, "empty.bin", b"")
    dest = cli.root / "out" / "empty.bin"
    code, result = download(cli, aid, dest)
    assert code == 0, result
    assert dest.read_bytes() == b"" and result["size"] == 0


def test_parse_attachments_drops_what_it_cannot_trust() -> None:
    fields = {
        "attachment": [
            {"id": "1", "filename": "ok.txt", "size": 3, "mimeType": "text/plain"},
            {"id": "x", "filename": "bad-id", "size": 1},
            {"id": "2", "filename": 5, "size": 1},
            {"id": "3", "filename": "neg", "size": -1},
            {"id": "4", "filename": "bool", "size": True},
            "junk",
            {"id": "5", "filename": "no-mime", "size": 0},
        ]
    }
    assert [a["id"] for a in attachmentcmd.parse_attachments(fields)] == ["1", "5"]
    with pytest.raises(TicketctlError) as exc:
        attachmentcmd.parse_attachments({"attachment": None})
    assert exc.value.code == "provider_changed"


# --- review round 1 ----------------------------------------------------------------------------


def fill_attachments(cli: Cli, count: int, key: str = KEY) -> list[str]:
    return [cli.fake.attach(key, f"f{i}.bin", b"x") for i in range(count)]


def test_add_with_a_list_past_the_cap_cannot_prove_absence(cli: Cli) -> None:
    fill_attachments(cli, attachmentcmd.MAX_ATTACHMENTS + 1)
    code, env = add(cli, make_file(cli, "late.bin", b"x"))
    assert (code, env["error"]["code"]) == (12, "lookup_incomplete")
    assert posts(cli) == []


def test_add_with_exactly_the_cap_of_attachments_still_works(cli: Cli) -> None:
    fill_attachments(cli, attachmentcmd.MAX_ATTACHMENTS)
    code, result = add(cli, make_file(cli, "late.bin", b"x"))
    assert code == 0 and result["result"] == "ok"


def test_download_of_an_id_past_the_cap_is_lookup_incomplete_never_not_found(cli: Cli) -> None:
    ids = fill_attachments(cli, attachmentcmd.MAX_ATTACHMENTS + 1)
    dest = cli.root / "out" / "late.bin"
    code, env = download(cli, ids[-1], dest)
    assert (code, env["error"]["code"]) == (12, "lookup_incomplete")
    code, env = download(cli, "99999", dest)
    assert (code, env["error"]["code"]) == (12, "lookup_incomplete")
    assert not dest.exists() and content_gets(cli) == []
    code, result = download(cli, ids[0], dest)  # an id inside the window still downloads
    assert code == 0 and dest.read_bytes() == b"x"


def test_a_same_named_entry_with_an_unreadable_size_cannot_prove_absence(cli: Cli) -> None:
    cli.fake.attach(KEY, "report.bin", BINARY)
    attachments(cli)[0]["size"] = "unknown"
    code, env = add(cli, make_file(cli))
    assert (code, env["error"]["code"]) == (12, "lookup_incomplete")
    assert posts(cli) == []


def test_an_unreadable_size_under_another_name_does_not_block(cli: Cli) -> None:
    cli.fake.attach(KEY, "other.bin", BINARY)
    attachments(cli)[0]["size"] = None
    code, result = add(cli, make_file(cli))
    assert code == 0 and result["result"] == "ok"


def test_download_picks_the_second_of_several_attachments(cli: Cli) -> None:
    first = cli.fake.attach(KEY, "one.bin", b"first-bytes")
    second = cli.fake.attach(KEY, "two.bin", b"second-bytes")
    dest = cli.root / "out" / "two.bin"
    code, result = download(cli, second, dest)
    assert code == 0, result
    assert dest.read_bytes() == b"second-bytes" and result["id"] == second
    assert result["name"] == "two.bin" and first != second
    code, env = download(cli, "99999", cli.root / "out" / "none.bin")
    assert (code, env["error"]["code"]) == (10, "not_found")
    assert [c.path.rsplit("/", 1)[-1] for c in content_gets(cli)] == [second]


def test_a_fifo_and_a_directory_are_refused_as_not_regular_files(cli: Cli) -> None:
    fifo = cli.root / "files" / "pipe.bin"
    os.mkfifo(fifo)
    for path in (fifo, cli.root / "files"):
        env = refused(cli, path)
        assert "not a regular file" in env["error"]["message"]


def test_the_parent_directory_of_the_target_must_be_owned_by_the_caller(
    cli: Cli, monkeypatch: pytest.MonkeyPatch
) -> None:
    aid = cli.fake.attach(KEY, "a.bin", b"x")
    monkeypatch.setattr(exportcmd, "_euid", lambda: os.geteuid() + 1)
    dest = cli.root / "out" / "a.bin"
    code, env = download(cli, aid, dest)
    assert (code, env["error"]["reason"]) == (22, "invalid_file")
    assert not dest.exists()


def test_a_path_that_appears_before_the_open_is_refused_and_kept(
    cli: Cli, monkeypatch: pytest.MonkeyPatch
) -> None:
    aid = cli.fake.attach(KEY, "a.bin", b"new-bytes")
    dest = cli.root / "out" / "race.bin"
    real = attachmentcmd.fetch_content

    def racing(gw: Any, attachment_id: str) -> bytes:
        dest.write_bytes(b"first writer")  # after the early check, before the O_EXCL open
        return real(gw, attachment_id)

    monkeypatch.setattr(attachmentcmd, "fetch_content", racing)
    code, env = download(cli, aid, dest)
    assert (code, env["error"]["code"]) == (20, "already_exists")
    assert dest.read_bytes() == b"first writer"


def test_a_file_name_that_holds_the_token_is_refused(cli: Cli) -> None:
    path = make_file(cli, f"{SITE_TOKEN}.txt", b"harmless")
    code, env = cli.doc("--dry-run", "attachment-add", KEY, "--file", str(path))
    assert (code, env["error"]["reason"]) == (22, "secret_in_body")
    assert SITE_TOKEN not in json.dumps(env) and posts(cli) == []
