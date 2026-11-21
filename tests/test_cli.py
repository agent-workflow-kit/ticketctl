from __future__ import annotations

import dataclasses
import io
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest
from jsonschema import Draft202012Validator
from referencing import Registry, Resource

from ticketctl import __version__
from ticketctl.cli import main
from ticketctl.commands import REGISTRY
from ticketctl.errors import CODES, DETAILS_KEYS, TicketctlError, clean_message
from ticketctl.flags import Globals, check_yes

SCHEMAS = Path(__file__).resolve().parent.parent / "schemas"


def _validator(rel: str) -> Draft202012Validator:
    resources = []
    for path in SCHEMAS.rglob("*.json"):
        doc = json.loads(path.read_text(encoding="utf-8"))
        uri = (
            "ticketctl/" + path.relative_to(SCHEMAS).as_posix() if "$id" not in doc else doc["$id"]
        )
        resources.append((uri, Resource.from_contents(doc)))
        resources.append((path.resolve().as_uri(), Resource.from_contents(doc)))
    registry = Registry().with_resources(resources)
    target = json.loads((SCHEMAS / rel).read_text(encoding="utf-8"))
    return Draft202012Validator(target, registry=registry)


def _check_envelope(out: str) -> dict[str, Any]:
    doc = json.loads(out)
    _validator("cli/error.json").validate(doc)
    return doc["error"]


def run(capsys: pytest.CaptureFixture[str], *argv: str) -> tuple[int, str, str]:
    code = main(list(argv))
    cap = capsys.readouterr()
    return code, cap.out, cap.err


def patch_handler(monkeypatch: pytest.MonkeyPatch, path: tuple[str, ...], handler: Any) -> None:
    monkeypatch.setitem(REGISTRY, path, dataclasses.replace(REGISTRY[path], handler=handler))


def raiser(exc: BaseException):
    def handler(g: Any, a: Any) -> Any:
        raise exc

    return handler


# --- version and basics ---------------------------------------------------------------


def test_version_prints_version_and_exits_zero(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as exc:
        main(["--version"])
    assert exc.value.code == 0
    assert capsys.readouterr().out.strip() == f"ticketctl {__version__}"


def test_version_works_after_the_command(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as exc:
        main(["whoami", "--version"])
    assert exc.value.code == 0


def test_no_arguments_is_a_usage_error(capsys: pytest.CaptureFixture[str]) -> None:
    code, out, err = run(capsys, "--json")
    assert code == 2
    body = _check_envelope(out)
    assert (body["code"], body["reason"]) == ("usage", "invalid_argument")
    assert err.startswith("ticketctl: error:")


def test_help_exits_zero(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as exc:
        main(["transition", "--help"])
    assert exc.value.code == 0
    assert "--to" in capsys.readouterr().out


# --- the whole command surface parses -------------------------------------------------

SURFACE = [
    ["whoami"],
    ["get", "AB-1", "AB-2", "--fields", "a,b", "--comments", "3", "--include-adf"],
    ["search", "--project", "AB", "--status", "Open", "--limit", "5", "--count"],
    ["comments", "AB-1", "--limit", "5", "--newest-first"],
    ["transitions", "AB-1"],
    ["sprints", "list", "--state", "active"],
    ["sprints", "show", "12"],
    ["fields", "list"],
    ["fields", "check"],
    ["fields", "refresh"],
    ["sites"],
    ["sites", "resolve", "AB-1"],
    ["validate", "--probe"],
    ["export", "--after-seq", "3"],
    ["transition", "AB-1", "--to", "Done"],
    ["comment", "AB-1", "--body", "hi"],
    ["create", "--project", "AB", "--type", "Task", "--title", "t"],
    ["update", "AB-1", "--title", "t"],
    ["sprint", "add", "AB-1", "--sprint", "open"],
    ["sprint", "remove", "AB-1"],
]


@pytest.mark.parametrize("argv", SURFACE, ids=lambda a: " ".join(a[:2]))
def test_every_command_parses_and_dispatches_to_its_handler(
    argv: list[str], monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    seen: list[Any] = []
    path = tuple(a for a in argv[:2] if a in {p[-1] for p in REGISTRY})
    path = next(p for p in sorted(REGISTRY, key=len, reverse=True) if list(p) == argv[: len(p)])
    patch_handler(monkeypatch, path, lambda g, a: seen.append(a) or {"ok": True})
    code, out, _ = run(capsys, "--json", *argv)
    assert code == 0
    assert json.loads(out) == {"ok": True}
    assert len(seen) == 1


def test_surface_covers_the_registry() -> None:
    covered = set()
    for argv in SURFACE:
        covered.add(
            next(p for p in sorted(REGISTRY, key=len, reverse=True) if list(p) == argv[: len(p)])
        )
    assert covered == set(REGISTRY)


def test_global_flags_before_and_after_the_command(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    seen: list[Any] = []
    patch_handler(monkeypatch, ("whoami",), lambda g, a: seen.append(g) or None)
    sha = "a" * 64
    for argv in (
        ["--site", "s1", "--timeout", "90", "whoami", "--refresh"],
        ["whoami", "--refresh", "--site", "s1", "--timeout", "90s"],
    ):
        code, _, _ = run(capsys, *argv)
        assert code == 0
    assert all(g.site == "s1" and g.timeout_s == 90 for g in seen)
    patch_handler(monkeypatch, ("comment",), lambda g, a: seen.append(g) or None)
    run(
        capsys,
        "comment", "AB-1", "--body", "x", "--dry-run", "--yes", "--expect-plan", sha,
        "--decision-id", "d1", "--decision-id", "d2", "--hook", "pre-comment=true",
        "--override-escalation=ticketctl", "--actor", "human:tester",
    )  # fmt: skip
    g = seen[-1]
    assert g.decision_ids == ["d1", "d2"] and g.hooks == ["pre-comment=true"]
    assert g.override_escalation == "ticketctl" and g.expect_plan == sha


def test_override_escalation_does_not_swallow_the_next_argument(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    seen: list[Any] = []
    patch_handler(monkeypatch, ("transition",), lambda g, a: seen.append(a) or None)
    code, _, _ = run(
        capsys,
        "transition", "--override-escalation", "AB-1", "--to", "Done", "--actor", "human:tester",
    )  # fmt: skip
    assert code == 0 and seen[0].key == "AB-1"


@pytest.mark.parametrize(
    "argv",
    [
        ["--config=/tmp/a b/c.toml", "whoami"],
        ["whoami", "--config=/tmp/a b/c.toml"],
        ["--config", "/tmp/a b/c.toml", "whoami"],
    ],
    ids=["equals-before", "equals-after", "separate"],
)
def test_config_path_with_spaces_reaches_the_command(
    argv: list[str], monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    seen: list[Any] = []
    patch_handler(monkeypatch, ("whoami",), lambda g, a: seen.append(g) or None)
    code, _, _ = run(capsys, *argv)
    assert code == 0 and seen[0].config == "/tmp/a b/c.toml"


def test_value_option_does_not_eat_a_following_global_option(
    capsys: pytest.CaptureFixture[str],
) -> None:
    status, out, _ = run(capsys, "--config", "--json", "get", "AB-1")
    assert status == 2
    body = _check_envelope(out)
    assert (body["code"], body["reason"]) == ("usage", "invalid_argument")


# --- exit-code table: one command-line test per row ------------------------------------

# (code, reason, exit) for every row of the table that a handler (not the CLI itself) raises.
HANDLER_ROWS = [
    ("not_found", None, 10),
    ("vetoed", None, 11),
    ("auth_required", None, 12),
    ("bad_credentials", None, 12),
    ("forbidden", None, 12),
    ("rejected", None, 12),
    ("unavailable", None, 12),
    ("unavailable", "locked", 12),
    ("timeout", None, 12),
    ("outcome_unknown", None, 12),
    ("rate_limited", None, 12),
    ("provider_changed", None, 12),
    ("partial", None, 12),
    ("lookup_incomplete", None, 12),
    ("confirmation_required", None, 13),
    ("escalated", None, 13),
    ("stale_ticket", None, 20),
    ("precondition_failed", None, 20),
    ("no_such_transition", None, 20),
    ("sprint_closed", None, 20),
    ("already_exists", None, 20),
    ("declined", None, 20),
    ("ambiguous_site", None, 21),
    ("ambiguous_field", None, 21),
    ("ambiguous_transition", None, 21),
    ("ambiguous_board", None, 21),
    ("ambiguous_sprint", None, 21),
    ("invalid_config", "config_invalid", 2),
    ("invalid_config", "no_sites", 2),
    ("invalid_config", "account_mismatch", 2),
    ("internal", "audit_failed", 1),
    ("internal", "hooks_unavailable", 1),
    ("usage", "human_only", 2),
    ("invalid_request", "invalid_query", 22),
    ("invalid_request", "unknown_field", 22),
    ("invalid_request", "not_editable", 22),
    ("invalid_request", "invalid_value", 22),
    ("invalid_request", "needs_resolution", 22),
    ("invalid_request", "missing_required_fields", 22),
    ("invalid_request", "node_not_allowed", 22),
    ("invalid_request", "body_too_large", 22),
    ("invalid_request", "empty_body", 22),
    ("invalid_request", "unknown_site", 22),
    ("invalid_request", "invalid_key", 22),
    ("invalid_request", "mixed_sites", 22),
    ("invalid_request", "invalid_file", 22),
    ("invalid_request", "secret_in_body", 22),
    ("invalid_request", "cursor_invalid", 22),
    ("invalid_request", "cursor_expired", 22),
]


def test_handler_rows_cover_every_code_and_reason() -> None:
    assert {c for c, _, _ in HANDLER_ROWS} | {"usage"} == set(CODES)
    for code, spec in CODES.items():
        if code == "usage":
            continue  # covered by test_every_usage_reason_has_a_producer
        reasons = {r for c, r, _ in HANDLER_ROWS if c == code}
        if spec.reason_is_code:
            reasons.add(code)
        assert spec.reasons <= reasons, code


SHA = "a" * 64
# One command line per `usage` reason the parser itself produces.
USAGE_PRODUCERS = {
    "human_only": [
        "transition", "AB-1", "--to", "x", "--actor", "auto:bot", "--override-escalation",
    ],
    "yes_requires_expect_plan": ["transition", "AB-1", "--to", "x", "--yes"],
    "dedupe_key_required": ["comment", "AB-1", "--body", "x", "--yes", "--expect-plan", SHA],
    "stdin_conflict": ["create", "--title-file", "-", "--body-file", "-"],
    "invalid_combination": ["whoami", "--verbose", "--quiet"],
    "invalid_argument": ["get"],
}  # fmt: skip
# `missing_option` has no parser-level producer: it needs config and provider context (the
# board of `sprints`, the project of `create`), so it is raised as a TicketctlError by the
# handlers. TODO(#18,#31): add the command-line test with the handlers that own it.
HANDLER_ONLY_USAGE_REASONS = {"missing_option"}


def test_every_usage_reason_has_a_producer(capsys: pytest.CaptureFixture[str]) -> None:
    assert set(USAGE_PRODUCERS) | HANDLER_ONLY_USAGE_REASONS == CODES["usage"].reasons
    for reason, argv in USAGE_PRODUCERS.items():
        status, out, _ = run(capsys, "--json", *argv)
        assert status == 2, reason
        assert _check_envelope(out)["reason"] == reason


def test_missing_option_is_produced_through_ticketctl_error(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    patch_handler(
        monkeypatch,
        ("sprints", "list"),
        raiser(TicketctlError("usage", "no board", reason="missing_option")),
    )
    status, out, _ = run(capsys, "--json", "sprints", "list")
    assert status == 2 and _check_envelope(out)["reason"] == "missing_option"


@pytest.mark.parametrize(("code", "reason", "exit_"), HANDLER_ROWS)
def test_each_table_row_from_the_command_line(
    code: str,
    reason: str | None,
    exit_: int,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    patch_handler(monkeypatch, ("whoami",), raiser(TicketctlError(code, "boom", reason=reason)))
    status, out, err = run(capsys, "--json", "whoami")
    assert status == exit_
    body = _check_envelope(out)
    assert body["code"] == code
    assert body["reason"] == (reason or (code if exit_ == 20 else None))
    assert body["retryable"] is (
        code in {"timeout", "unavailable", "rate_limited", "outcome_unknown"}
    )
    assert body["outcome_unknown"] is (code == "outcome_unknown")
    assert "boom" in err and "Traceback" not in err


def test_exit_zero_row(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    patch_handler(monkeypatch, ("whoami",), lambda g, a: {"x": 1})
    assert run(capsys, "--json", "whoami")[0] == 0


def test_exit_one_row_unexpected_exception(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    patch_handler(monkeypatch, ("whoami",), raiser(RuntimeError("secret-sentinel-123")))
    monkeypatch.delenv("TICKETCTL_DEBUG", raising=False)
    status, out, err = run(capsys, "--json", "whoami")
    assert status == 1
    body = _check_envelope(out)
    assert body["code"] == "internal" and "RuntimeError" in body["message"]
    assert "secret-sentinel-123" not in out + err
    assert "Traceback" not in err


def test_traceback_only_with_debug(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    patch_handler(monkeypatch, ("whoami",), raiser(RuntimeError("x")))
    monkeypatch.setenv("TICKETCTL_DEBUG", "1")
    status, out, err = run(capsys, "--json", "whoami")
    assert status == 1 and "Traceback" in err
    _check_envelope(out)


def test_stub_handler_is_exit_one(capsys: pytest.CaptureFixture[str]) -> None:
    status, out, _ = run(capsys, "--json", "export")  # owned by #36
    assert status == 1
    assert _check_envelope(out)["code"] == "internal"


def test_human_mode_error_is_stderr_only(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    patch_handler(monkeypatch, ("whoami",), raiser(TicketctlError("not_found", "no such thing")))
    status, out, err = run(capsys, "whoami")
    assert (status, out) == (10, "")
    assert "no such thing" in err


def test_json_failure_puts_envelope_alone_on_stdout(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    patch_handler(monkeypatch, ("whoami",), raiser(TicketctlError("not_found", "gone")))
    _, out, err = run(capsys, "whoami", "--json")
    assert len(out.strip().splitlines()) == 1
    assert out.strip() != err.strip()


# --- argparse failures ----------------------------------------------------------------

BAD_ARGV = [
    ["nosuchcommand"],
    ["get"],
    ["get", "AB-1", "--comments", "x"],
    ["search", "--status-category", "bogus"],
    ["transition", "AB-1", "--bogus"],
    ["sprints"],
    ["sprints", "show"],
    ["--timeout", "0s", "whoami"],
    ["--timeout", "25h", "whoami"],
    ["--timeout", "abc", "whoami"],
    ["--timeout", "-5", "whoami"],
    ["--actor", "robot:x", "whoami"],
    ["--actor", "human:", "whoami"],
    ["--expect-plan", "XYZ", "comment", "AB-1", "--body", "x"],
    ["--decision-id", "bad id", "comment", "AB-1", "--body", "x"],
    ["--hook", "nonsense", "comment", "AB-1", "--body", "x"],
    ["--hook", "pre-bogus=cmd", "comment", "AB-1", "--body", "x"],
    ["--override-escalation=other", "comment", "AB-1", "--body", "x"],
    ["--site"],
    ["whoami", "\x1b[31mevil\x1b[0m"],
]


@pytest.mark.parametrize("argv", BAD_ARGV, ids=lambda a: " ".join(a)[:40])
def test_argparse_failure_with_json_prints_a_valid_envelope(
    argv: list[str], capsys: pytest.CaptureFixture[str]
) -> None:
    status, out, err = run(capsys, *argv, "--json")
    assert status == 2
    body = _check_envelope(out)
    assert (body["code"], body["reason"]) == ("usage", "invalid_argument")
    assert "\x1b" not in out and "\x1b" not in err


def test_argparse_failure_without_json_is_stderr_only(capsys: pytest.CaptureFixture[str]) -> None:
    status, out, err = run(capsys, "get")
    assert (status, out) == (2, "")
    assert err


def test_json_after_double_dash_is_not_the_flag(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    seen: list[Any] = []
    patch_handler(monkeypatch, ("get",), lambda g, a: seen.append((g, a)))
    status, out, _ = run(capsys, "get", "--", "--json")
    assert (status, out) == (0, "")
    assert seen[0][0].json is False and seen[0][1].keys == ["--json"]


# --- flag combination rules ------------------------------------------------------------

WRITE_ONLY = [
    ["--yes"],
    ["--dry-run"],
    ["--expect-plan", "a" * 64],
    ["--actor", "human:me"],
    ["--decision-id", "d1"],
    ["--override-escalation"],
    ["--hook", "pre-comment=true"],
]
READ_COMMANDS = [["whoami"], ["get", "AB-1"], ["search"], ["sites"], ["export"], ["fields", "list"]]


@pytest.mark.parametrize("cmd", READ_COMMANDS, ids=lambda c: " ".join(c))
@pytest.mark.parametrize("flag", WRITE_ONLY, ids=lambda f: f[0])
def test_write_only_flag_on_read_command(
    cmd: list[str], flag: list[str], capsys: pytest.CaptureFixture[str]
) -> None:
    status, out, _ = run(capsys, "--json", *cmd, *flag)
    assert status == 2
    body = _check_envelope(out)
    assert (body["code"], body["reason"]) == ("usage", "invalid_argument")


def test_if_status_on_a_read_command(capsys: pytest.CaptureFixture[str]) -> None:
    status, out, _ = run(capsys, "--json", "get", "AB-1", "--if-status", "Open")
    assert status == 2 and _check_envelope(out)["reason"] == "invalid_argument"


def test_write_only_flags_are_fine_on_write_commands(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    patch_handler(monkeypatch, ("transition",), lambda g, a: None)
    status, _, _ = run(
        capsys, "transition", "AB-1", "--to", "Done", "--dry-run", "--actor", "auto:bot"
    )
    assert status == 0


def test_verbose_with_quiet(capsys: pytest.CaptureFixture[str]) -> None:
    status, out, _ = run(capsys, "--json", "whoami", "--verbose", "--quiet")
    assert status == 2
    body = _check_envelope(out)
    assert (body["code"], body["reason"]) == ("usage", "invalid_combination")


def test_two_stdin_sources(capsys: pytest.CaptureFixture[str]) -> None:
    status, out, _ = run(
        capsys, "--json", "create", "--title-file", "-", "--body-file", "-", "--type", "Task"
    )
    assert status == 2
    assert _check_envelope(out)["reason"] == "stdin_conflict"


def test_stdin_source_in_set_value_counts(capsys: pytest.CaptureFixture[str]) -> None:
    status, out, _ = run(capsys, "--json", "update", "AB-1", "--set", "a=-", "--set", "b=-")
    assert status == 2 and _check_envelope(out)["reason"] == "stdin_conflict"


def test_one_stdin_source_is_fine(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    patch_handler(monkeypatch, ("comment",), lambda g, a: None)
    assert run(capsys, "comment", "AB-1", "--body-file", "-")[0] == 0


def test_yes_needs_expect_plan(capsys: pytest.CaptureFixture[str]) -> None:
    status, out, _ = run(capsys, "--json", "transition", "AB-1", "--to", "Done", "--yes")
    assert status == 2
    assert _check_envelope(out)["reason"] == "yes_requires_expect_plan"


def test_yes_with_dry_run_skips_the_check(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    patch_handler(monkeypatch, ("transition",), lambda g, a: None)
    assert run(capsys, "transition", "AB-1", "--to", "Done", "--yes", "--dry-run")[0] == 0


@pytest.mark.parametrize("cmd", [["comment", "AB-1", "--body", "x"], ["create", "--type", "Task"]])
def test_yes_on_comment_and_create_needs_dedupe_key(
    cmd: list[str], capsys: pytest.CaptureFixture[str]
) -> None:
    status, out, _ = run(capsys, "--json", *cmd, "--yes", "--expect-plan", "b" * 64)
    assert status == 2 and _check_envelope(out)["reason"] == "dedupe_key_required"


def test_other_combination_rules_run_before_yes_checks(capsys: pytest.CaptureFixture[str]) -> None:
    status, out, _ = run(
        capsys, "--json", "transition", "AB-1", "--to", "x", "--yes", "--verbose", "--quiet"
    )
    assert status == 2 and _check_envelope(out)["reason"] == "invalid_combination"


def test_override_escalation_with_auto_actor_is_human_only(
    capsys: pytest.CaptureFixture[str],
) -> None:
    status, out, _ = run(
        capsys, "--json", "transition", "AB-1", "--to", "x", "--actor", "auto:bot",
        "--override-escalation",
    )  # fmt: skip
    assert status == 2 and _check_envelope(out)["reason"] == "human_only"


@pytest.mark.parametrize(
    "argv",
    [
        ["transition", "AB-1"],
        ["transition", "AB-1", "--to", "x", "--transition-id", "3"],
        ["comment", "AB-1"],
        ["comment", "AB-1", "--body", "x", "--body-file", "f"],
        ["comment", "AB-1", "--body", "x", "--format", "adf"],
        ["sprint", "remove", "AB-1", "--sprint", "open"],
    ],
)
def test_exactly_one_of_rules(argv: list[str], capsys: pytest.CaptureFixture[str]) -> None:
    status, out, _ = run(capsys, "--json", *argv)
    assert status == 2 and _check_envelope(out)["reason"] == "invalid_combination"


# --- error type and envelope -----------------------------------------------------------


def test_reason_required_rows_reject_a_missing_reason() -> None:
    for code in ("usage", "invalid_config", "invalid_request"):
        with pytest.raises(ValueError):
            TicketctlError(code, "m")


def test_reason_must_belong_to_the_code() -> None:
    with pytest.raises(ValueError):
        TicketctlError("usage", "m", reason="no_sites")
    with pytest.raises(ValueError):
        TicketctlError("not_found", "m", reason="locked")
    with pytest.raises(ValueError):
        TicketctlError("nonsense", "m")


def test_retry_after_only_for_rate_limited_and_unavailable() -> None:
    assert TicketctlError("rate_limited", "m", retry_after_s=5).body()["retry_after_s"] == 5
    assert TicketctlError("unavailable", "m", retry_after_s=0).retry_after_s == 0
    with pytest.raises(ValueError):
        TicketctlError("timeout", "m", retry_after_s=5)


def test_partial_copies_the_failing_steps_flags() -> None:
    e = TicketctlError("partial", "m", retryable=True, outcome_unknown=True)
    assert e.body()["retryable"] and e.body()["outcome_unknown"]
    assert not TicketctlError("partial", "m").body()["retryable"]


def test_partial_carries_retry_after() -> None:
    e = TicketctlError("partial", "m", retryable=True, retry_after_s=7)
    assert e.body()["retry_after_s"] == 7 and e.body()["retryable"] is True


def test_retryable_override_only_for_partial_and_outcome_unknown() -> None:
    assert TicketctlError("outcome_unknown", "m", retryable=False).retryable is False
    assert TicketctlError("outcome_unknown", "m").retryable is True
    for code in ("not_found", "timeout", "rate_limited", "rejected"):
        default = TicketctlError(code, "m").retryable
        with pytest.raises(ValueError):
            TicketctlError(code, "m", retryable=not default)
        assert TicketctlError(code, "m", retryable=default).retryable is default


def test_outcome_unknown_override_only_for_partial() -> None:
    assert TicketctlError("partial", "m", outcome_unknown=True).outcome_unknown is True
    assert TicketctlError("outcome_unknown", "m").outcome_unknown is True
    with pytest.raises(ValueError):
        TicketctlError("outcome_unknown", "m", outcome_unknown=False)
    for code in ("not_found", "timeout", "rejected"):
        with pytest.raises(ValueError):
            TicketctlError(code, "m", outcome_unknown=True)
        assert TicketctlError(code, "m", outcome_unknown=False).outcome_unknown is False


def test_unknown_details_key_is_refused() -> None:
    with pytest.raises(ValueError):
        TicketctlError("not_found", "m", details={"bogus": 1})
    assert TicketctlError("not_found", "m", details={"option": "board"}).details == {
        "option": "board"
    }


def test_details_keys_match_the_schema() -> None:
    defs = json.loads((SCHEMAS / "defs.json").read_text(encoding="utf-8"))["$defs"]
    assert DETAILS_KEYS == set(defs["error_details"]["properties"])


def test_table_matches_schema_enums() -> None:
    defs = json.loads((SCHEMAS / "defs.json").read_text(encoding="utf-8"))["$defs"]
    assert set(CODES) == set(defs["error_code"]["enum"])
    reasons = {r for s in CODES.values() for r in s.reasons} | {
        c for c, s in CODES.items() if s.reason_is_code
    }
    assert reasons == set(defs["error_reason"]["enum"])


def test_envelope_with_every_field_validates() -> None:
    e = TicketctlError(
        "ambiguous_site",
        "pick one",
        site="s1",
        key="AB-1",
        details={"candidates": ["a", "b"], "attempt_id": "attempt-0001"},
    )
    _validator("cli/error.json").validate(e.envelope())


def test_message_is_cleaned_and_capped() -> None:
    assert clean_message("a\nb\x1b[0m‮") == "a<U+000A>b<U+001B>[0m<U+202E>"
    long = clean_message("x" * 10000)
    assert len(long) == 4096 and long.endswith("<truncated>")
    assert clean_message("héllo ✓") == "héllo ✓"


def test_hostile_message_in_envelope_validates() -> None:
    e = TicketctlError("not_found", "\x00" * 5000 + " ")
    _validator("cli/error.json").validate(e.envelope())


# --- the global-flag pre-pass ----------------------------------------------------------


@pytest.mark.parametrize(
    ("path", "argv", "attr", "value"),
    [
        (("search",), ["search", "--text", "--yes=1 foo"], "text", "--yes=1 foo"),
        (
            ("comment",),
            ["comment", "AB-1", "--body", "--override-escalation=x y"],
            "body",
            "--override-escalation=x y",
        ),
        (("comment",), ["comment", "AB-1", "--body", "--json=1 x"], "body", "--json=1 x"),
        (("comment",), ["comment", "AB-1", "--body", "--dry-run=1 x"], "body", "--dry-run=1 x"),
    ],
)
def test_option_value_that_looks_like_a_global_flag_reaches_the_subcommand(
    path: tuple[str, ...],
    argv: list[str],
    attr: str,
    value: str,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    seen: list[Any] = []
    patch_handler(monkeypatch, path, lambda g, a: seen.append((g, a)) or None)
    status, _, err = run(capsys, *argv)
    assert status == 0, err
    g, a = seen[0]
    assert getattr(a, attr) == value
    assert not (g.yes or g.json or g.dry_run or g.override_escalation)
    assert g.given == frozenset()


def test_given_holds_only_the_flags_the_pre_pass_consumed(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    seen: list[Any] = []
    patch_handler(monkeypatch, ("comment",), lambda g, a: seen.append(g) or None)
    run(capsys, "comment", "AB-1", "--body", "--yes=1 x", "--dry-run", "--actor=human:me")
    assert seen[0].given == frozenset({"--dry-run", "--actor"})


@pytest.mark.parametrize("flag", ["--json=1", "--json=true"])
def test_json_with_a_value_prints_an_envelope(
    flag: str, capsys: pytest.CaptureFixture[str]
) -> None:
    status, out, _ = run(capsys, "get", flag, "AB-1")
    assert status == 2
    body = _check_envelope(out)
    assert (body["code"], body["reason"]) == ("usage", "invalid_argument")


def test_help_and_version_stay_plain_text_with_json(capsys: pytest.CaptureFixture[str]) -> None:
    for argv in (["--json", "--version"], ["--json", "whoami", "--help"], ["-h", "--json"]):
        with pytest.raises(SystemExit) as exc:
            main(argv)
        assert exc.value.code == 0
        out = capsys.readouterr().out
        with pytest.raises(ValueError):
            json.loads(out)


# --- shared flag grammars at the parser -------------------------------------------------

BAD_DEDUPE = ["bad key", "k\n", "", "a" * 129, "k\u00e9", "a;b"]


@pytest.mark.parametrize("value", BAD_DEDUPE, ids=repr)
@pytest.mark.parametrize(
    "cmd", [["comment", "AB-1", "--body", "x"], ["create", "--type", "Task"]], ids=lambda c: c[0]
)
def test_bad_dedupe_key_is_invalid_argument(
    cmd: list[str], value: str, capsys: pytest.CaptureFixture[str]
) -> None:
    status, out, _ = run(capsys, "--json", *cmd, "--dedupe-key", value)
    assert status == 2 and _check_envelope(out)["reason"] == "invalid_argument"


def test_bad_decision_id_with_trailing_newline(capsys: pytest.CaptureFixture[str]) -> None:
    status, out, _ = run(capsys, "--json", "comment", "AB-1", "--body", "x", "--decision-id", "d\n")
    assert status == 2 and _check_envelope(out)["reason"] == "invalid_argument"


def test_good_dedupe_key_is_accepted(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    patch_handler(monkeypatch, ("comment",), lambda g, a: None)
    assert run(capsys, "comment", "AB-1", "--body", "x", "--dedupe-key", "run.1:a@b/c-d")[0] == 0


BAD_NUMBERS = ["abc", "1_0", "\u0663", " 5", "5 ", "-5", "+5", "5\n", "9" * 29, ""]


@pytest.mark.parametrize("value", BAD_NUMBERS, ids=repr)
@pytest.mark.parametrize(
    "argv",
    [
        ["search", "--limit"],
        ["comments", "AB-1", "--limit"],
        ["sprints", "list", "--limit"],
        ["export", "--after-seq"],
        ["transition", "AB-1", "--transition-id"],
        ["sprint", "add", "AB-1", "--sprint"],
    ],
    ids=lambda a: a[-1],
)
def test_bad_number_is_invalid_argument(
    argv: list[str], value: str, capsys: pytest.CaptureFixture[str]
) -> None:
    status, out, _ = run(capsys, "--json", *argv, value)
    assert status == 2 and _check_envelope(out)["reason"] == "invalid_argument"


@pytest.mark.parametrize("value", ["abc", "1_0", "\u0663", " 5", "5 ", "+5", "5\n", ""], ids=repr)
def test_get_comments_non_integer_text_is_invalid_argument(
    value: str, capsys: pytest.CaptureFixture[str]
) -> None:
    status, out, _ = run(capsys, "--json", "get", "AB-1", "--comments", value)
    assert status == 2 and _check_envelope(out)["reason"] == "invalid_argument"


@pytest.mark.parametrize("value", ["-5", "9" * 29], ids=repr)
def test_get_comments_out_of_range_integer_is_invalid_value(
    value: str, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from ticketctl import getcmd

    patch_handler(monkeypatch, ("get",), lambda g, a: getcmd.comment_count(a.comments))
    status, out, _ = run(capsys, "--json", "get", "AB-1", "--comments", value)
    doc = _check_envelope(out)
    assert status == 22 and doc["code"] == "invalid_request" and doc["reason"] == "invalid_value"


@pytest.mark.parametrize("argv", [["sprints", "show"], ["sprints", "show", "x1"]])
def test_sprint_show_id_must_be_numeric(
    argv: list[str], capsys: pytest.CaptureFixture[str]
) -> None:
    status, out, _ = run(capsys, "--json", *argv)
    assert status == 2 and _check_envelope(out)["reason"] == "invalid_argument"


def test_numbers_parse_to_ints_and_sprint_selectors(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    seen: list[Any] = []
    patch_handler(monkeypatch, ("get",), lambda g, a: seen.append(a) or None)
    patch_handler(monkeypatch, ("sprint", "add"), lambda g, a: seen.append(a) or None)
    assert run(capsys, "get", "AB-1", "--comments", "0")[0] == 0
    assert seen[0].comments == 0
    for sel in ("open", "next", "123456789012345678"):
        assert run(capsys, "sprint", "add", "AB-1", "--sprint", sel)[0] == 0
        assert seen[-1].sprint == sel


def test_check_yes_rejects_an_empty_dedupe_key_by_grammar() -> None:
    g = Globals(yes=True, expect_plan=SHA)
    for key in ("", "bad key", "k\n"):
        with pytest.raises(TicketctlError) as exc:
            check_yes(g, "comment", key)
        assert exc.value.reason == "invalid_argument"
    check_yes(g, "comment", "ok-key")


# --- output failures and interrupts -----------------------------------------------------


class _BrokenStream:
    def __init__(self, exc: BaseException) -> None:
        self.exc = exc

    def write(self, text: str) -> int:
        raise self.exc

    def flush(self) -> None:
        raise self.exc


def _closed_stream() -> io.StringIO:
    s = io.StringIO()
    s.close()
    return s


BAD_STREAMS = [
    lambda: _BrokenStream(OSError(28, "no space")),
    lambda: _BrokenStream(BrokenPipeError()),
    lambda: _BrokenStream(ValueError("I/O operation on closed file")),
    _closed_stream,
    lambda: None,
]


@pytest.mark.parametrize("make", BAD_STREAMS)
def test_failing_streams_never_change_the_exit_code(
    make: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    patch_handler(monkeypatch, ("whoami",), raiser(TicketctlError("not_found", "gone")))
    monkeypatch.setattr(sys, "stdout", make())
    monkeypatch.setattr(sys, "stderr", make())
    assert main(["--json", "whoami"]) == 10
    patch_handler(monkeypatch, ("whoami",), lambda g, a: {"x": 1})
    assert main(["--json", "whoami"]) == 0


def test_keyboard_interrupt_is_one_line_and_exit_130(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    patch_handler(monkeypatch, ("whoami",), raiser(KeyboardInterrupt()))
    monkeypatch.delenv("TICKETCTL_DEBUG", raising=False)
    status, out, err = run(capsys, "--json", "whoami")
    assert (status, out, err) == (130, "", "ticketctl: interrupted\n")


def test_keyboard_interrupt_traceback_with_debug(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    patch_handler(monkeypatch, ("whoami",), raiser(KeyboardInterrupt()))
    monkeypatch.setenv("TICKETCTL_DEBUG", "1")
    status, out, err = run(capsys, "whoami")
    assert status == 130 and out == "" and "Traceback" in err and "ticketctl: interrupted" in err


# --- real process ----------------------------------------------------------------------


def _proc(tmp_path: Path, *argv: str, debug: bool = False) -> subprocess.CompletedProcess[str]:
    env = {
        "HOME": str(tmp_path),
        "XDG_CONFIG_HOME": str(tmp_path / "xdg"),
        "XDG_CACHE_HOME": str(tmp_path / "cache"),
        "XDG_STATE_HOME": str(tmp_path / "state"),
        "PATH": "",
        "PYTHONPATH": os.pathsep.join(sys.path),
    }
    if debug:
        env["TICKETCTL_DEBUG"] = "1"
    return subprocess.run(
        [sys.executable, "-m", "ticketctl", *argv],
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )


def test_process_version(tmp_path: Path) -> None:
    r = _proc(tmp_path, "--version")
    assert (r.returncode, r.stdout.strip()) == (0, f"ticketctl {__version__}")


def test_process_usage_error_exit_two_with_envelope(tmp_path: Path) -> None:
    r = _proc(tmp_path, "get", "--json")
    assert r.returncode == 2
    _check_envelope(r.stdout)


def test_process_unexpected_exception_exit_one_without_traceback(tmp_path: Path) -> None:
    r = _proc(tmp_path, "export", "--json")  # a stub (#36)
    assert r.returncode == 1
    assert "Traceback" not in r.stderr
    assert _check_envelope(r.stdout)["code"] == "internal"
    r = _proc(tmp_path, "export", debug=True)
    assert r.returncode == 1 and "Traceback" in r.stderr
