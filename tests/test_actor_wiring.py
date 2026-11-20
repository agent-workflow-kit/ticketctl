"""Default-actor resolution in check_globals and the --set / --field checks in the CLI (#27)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from ticketctl import cli, flags
from ticketctl.cli import main
from ticketctl.errors import TicketctlError
from ticketctl.flags import Globals, check_globals


def write_globals(**kw: object) -> Globals:
    g = Globals(**kw)  # type: ignore[arg-type]
    return g


def never() -> str | None:
    raise AssertionError("the configuration must not be read")


def resolve(g: Globals, *, tty: bool, openable: bool, name: str | None = "alice") -> Globals:
    check_globals(
        g,
        "write",
        "update",
        me_name=lambda: name,
        stderr_tty=lambda: tty,
        tty_openable=lambda: openable,
    )
    return g


def test_tty_gives_a_human_actor() -> None:
    g = resolve(write_globals(), tty=True, openable=True)
    assert (g.resolved_actor, g.actor_source) == ("human:alice", "tty")


@pytest.mark.parametrize("tty", [(True, False), (False, True), (False, False)])
def test_no_tty_gives_auto_unknown(tty: tuple[bool, bool]) -> None:
    g = resolve(write_globals(), tty=tty[0], openable=tty[1])
    assert (g.resolved_actor, g.actor_source) == ("auto:unknown", "default")


def test_unusable_me_name_gives_auto_unknown() -> None:
    g = resolve(write_globals(), tty=True, openable=True, name=None)
    assert g.resolved_actor == "auto:unknown"


def test_explicit_actor_wins_and_skips_every_probe() -> None:
    g = write_globals(actor="auto:bot")
    check_globals(g, "write", "update", me_name=never, stderr_tty=never, tty_openable=never)  # type: ignore[arg-type]
    assert (g.resolved_actor, g.actor_source) == ("auto:bot", "flag")


def test_override_escalation_without_actor_on_a_non_tty_is_human_only() -> None:
    g = write_globals(override_escalation="ticketctl")
    with pytest.raises(TicketctlError) as exc:
        resolve(g, tty=False, openable=False)
    assert (exc.value.code, exc.value.reason) == ("usage", "human_only")


def test_override_escalation_with_a_tty_default_human_is_allowed() -> None:
    g = resolve(write_globals(override_escalation="ticketctl"), tty=True, openable=True)
    assert g.resolved_actor == "human:alice"


def test_override_escalation_with_explicit_human_actor_is_allowed_off_tty() -> None:
    g = write_globals(override_escalation="ticketctl", actor="human:bob")
    resolve(g, tty=False, openable=False)
    assert g.resolved_actor == "human:bob"


def test_override_escalation_with_explicit_auto_actor_is_human_only() -> None:
    g = write_globals(override_escalation="ticketctl", actor="auto:bot")
    with pytest.raises(TicketctlError) as exc:
        resolve(g, tty=True, openable=True)
    assert exc.value.reason == "human_only"


def test_reads_do_not_resolve_an_actor() -> None:
    g = write_globals()
    check_globals(g, "read", "get", me_name=never, stderr_tty=never, tty_openable=never)  # type: ignore[arg-type]
    assert g.resolved_actor is None


def test_me_name_is_read_only_when_the_tty_probes_pass() -> None:
    g = write_globals()
    check_globals(
        g,
        "write",
        "update",
        me_name=never,
        stderr_tty=lambda: False,
        tty_openable=lambda: True,  # type: ignore[arg-type]
    )
    assert g.resolved_actor == "auto:unknown"


def test_tty_probe_reports_false_without_a_controlling_terminal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def deny(*_a: object, **_k: object) -> int:
        raise OSError("no tty")

    monkeypatch.setattr(flags.os, "open", deny)
    assert flags.probe_dev_tty() is False


def test_tty_probe_opens_and_closes_dev_tty(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[tuple[str, object]] = []
    monkeypatch.setattr(flags.os, "open", lambda p, f: (calls.append(("open", p)), 99)[1])
    monkeypatch.setattr(flags.os, "close", lambda fd: calls.append(("close", fd)))
    assert flags.probe_dev_tty() is True
    assert calls == [("open", "/dev/tty"), ("close", 99)]


# ---- through the CLI ---------------------------------------------------------------------------


def run(capsys: pytest.CaptureFixture[str], *argv: str) -> tuple[int, dict[str, object]]:
    code = main(["--json", *argv])
    out = capsys.readouterr().out
    return code, (json.loads(out)["error"] if out.strip() else {})


def test_cli_override_escalation_without_actor_off_tty_exits_2_human_only(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(flags, "probe_dev_tty", lambda: False)
    code, err = run(capsys, "update", "AB-1", "--set", "x=1", "--override-escalation")
    assert code == 2 and err["reason"] == "human_only"


def test_cli_bad_actor_exits_2_before_attribution(capsys: pytest.CaptureFixture[str]) -> None:
    code, err = run(capsys, "update", "AB-1", "--set", "x=1", "--actor", "robot:x")
    assert code == 2 and err["reason"] == "invalid_argument"


@pytest.mark.parametrize("name", ["assignee", "labels", "resolution", "sprint", "title"])
def test_cli_set_of_a_dedicated_name_exits_2(capsys: pytest.CaptureFixture[str], name: str) -> None:
    code, err = run(capsys, "update", "AB-1", "--set", f"{name}=x")
    assert code == 2 and err["reason"] == "invalid_combination"


def test_cli_field_of_a_dedicated_name_exits_2(capsys: pytest.CaptureFixture[str]) -> None:
    code, err = run(capsys, "transition", "AB-1", "--to", "Done", "--field", "resolution=Done")
    assert code == 2 and err["reason"] == "invalid_combination"


def test_cli_create_refuses_parent_and_description(capsys: pytest.CaptureFixture[str]) -> None:
    code, err = run(capsys, "create", "--set", "parent=AB-1")
    assert code == 2 and err["reason"] == "invalid_combination"


def test_cli_malformed_set_exits_2(capsys: pytest.CaptureFixture[str]) -> None:
    code, err = run(capsys, "update", "AB-1", "--set", "noequals")
    assert code == 2 and err["reason"] == "invalid_argument"


def test_cli_the_same_name_twice_exits_2(capsys: pytest.CaptureFixture[str]) -> None:
    code, err = run(capsys, "update", "AB-1", "--set", "x=1", "--set", "x=2")
    assert code == 2 and err["reason"] == "invalid_combination"


def test_cli_two_stdin_sources_still_conflict(capsys: pytest.CaptureFixture[str]) -> None:
    code, err = run(capsys, "update", "AB-1", "--set", "x=-", "--set", "y=-")
    assert code == 2 and err["reason"] == "stdin_conflict"


def test_cli_a_valid_set_reaches_the_handler(
    capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    # the update command is owned by #30: the stub is reached after the checks pass
    code, _ = run(capsys, "update", "AB-1", "--set", "effort=3")
    assert code == 1


def test_cli_usage_errors_come_before_the_configuration_is_read(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    def boom(_g: object) -> str:
        raise AssertionError("the configuration was read")

    monkeypatch.setattr(cli, "_me_name", boom)
    monkeypatch.setattr(flags, "probe_dev_tty", lambda: True)
    monkeypatch.setattr(flags, "_stderr_is_tty", lambda: True)
    code, err = run(capsys, "update", "AB-1", "--set", "noequals")
    assert code == 2 and err["reason"] == "invalid_argument"
    code, err = run(capsys, "update", "AB-1", "--set", "x=-", "--set", "y=-")
    assert code == 2 and err["reason"] == "stdin_conflict"
