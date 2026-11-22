"""A cron-style run from a minimal environment (issue #39), against the fake provider.

``scripts/cron_check.py`` runs ticketctl as a real process with only a configured ``PATH``, the
XDG directories and the token variable: no ``TERM``, stdin from ``/dev/null``, a new session (no
controlling terminal). These tests prove the CLI passes that check on Linux; the same script, run
from a real crontab, is the human part (docs/platform-checklist.md).
"""

from __future__ import annotations

import importlib.util
import json
import re
import sys
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

from tests.fake_provider import SITE_EMAIL, SITE_TOKEN, FakeProvider
from ticketctl.audit import AUDIT_FILE

ROOT = Path(__file__).resolve().parent.parent
TOKEN_VAR = "CRON_TEST_API_KEY"
CRON_PATH = "/usr/bin:/bin"
KEY = "DEMO-2"
TO = "Done"
RES = "Done"
TICKETCTL = [sys.executable, "-m", "ticketctl"]


def _load() -> ModuleType:
    spec = importlib.util.spec_from_file_location("cron_check", ROOT / "scripts" / "cron_check.py")
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules["cron_check"] = module
    spec.loader.exec_module(module)
    return module


cron = _load()


def write_config(config_dir: Path, fake: FakeProvider) -> Path:
    config_dir.mkdir(parents=True)
    path = config_dir / "config.yaml"
    path.write_text(
        f"sites:\n  main:\n    url: {fake.url}\n    email: {SITE_EMAIL}\n"
        f"    token_env: {TOKEN_VAR}\n    allow_http: true\n"
    )
    path.chmod(0o600)
    return path


def audit_records(state: Path) -> list[dict[str, Any]]:
    text = (state / "ticketctl" / AUDIT_FILE).read_text(encoding="utf-8")
    return [json.loads(line) for line in text.splitlines()]


@pytest.fixture
def xdg(tmp_path: Path, fake: FakeProvider) -> dict[str, Path]:
    """State and config homes under a private root; the config lives at the XDG location."""
    state, conf = tmp_path / "xdg-state", tmp_path / "xdg-config"
    write_config(conf / "ticketctl", fake)
    return {"state": state, "config": conf}


def scrubbed(xdg: dict[str, Path]) -> dict[str, str]:
    return cron.minimal_env(
        path=CRON_PATH,
        state=xdg["state"],
        config_home=xdg["config"],
        token_env=TOKEN_VAR,
        token=SITE_TOKEN,
    )


def test_the_scrubbed_environment_holds_only_what_cron_would_configure(
    xdg: dict[str, Path],
) -> None:
    env = scrubbed(xdg)
    assert sorted(env) == sorted(["PATH", "XDG_STATE_HOME", "XDG_CONFIG_HOME", TOKEN_VAR])
    assert "TERM" not in env and "HOME" not in env


def test_a_read_a_dry_run_and_a_yes_write_pass_from_the_minimal_environment(
    xdg: dict[str, Path], fake: FakeProvider
) -> None:
    report = cron.run_checks(
        TICKETCTL,
        key=KEY,
        to=TO,
        resolution=RES,
        env=scrubbed(xdg),
        apply=True,
        token_env=TOKEN_VAR,
        timeout=60,
    )
    assert report.ok, report.render()
    names = {s.name for s in report.steps}
    assert {"read", "dry-run write", "--yes --expect-plan write"} <= names
    # The harness's verdict is not taken on trust: the provider saw exactly one write and the
    # audit, read here by the test, carries the cron actor.
    assert len(fake.applied) == 1
    records = audit_records(xdg["state"])
    assert [r["result"] for r in records] == ["attempted", "ok"]
    for r in records:
        assert (r["actor"], r["actor_source"], r["stdin_tty"]) == ("auto:unknown", "default", False)
        assert r["target"]["key"] == KEY


def test_without_apply_nothing_is_written(xdg: dict[str, Path], fake: FakeProvider) -> None:
    report = cron.run_checks(
        TICKETCTL,
        key=KEY,
        to=TO,
        resolution=RES,
        env=scrubbed(xdg),
        apply=False,
        token_env=TOKEN_VAR,
    )
    assert report.ok, report.render()
    assert fake.applied == []
    assert not (xdg["state"] / "ticketctl" / AUDIT_FILE).exists()


def test_the_state_goes_where_xdg_state_home_says_and_not_under_home(
    xdg: dict[str, Path], tmp_path: Path
) -> None:
    home = tmp_path / "unused-home"
    home.mkdir()
    env = scrubbed(xdg) | {"HOME": str(home)}
    report = cron.run_checks(TICKETCTL, key=KEY, to=TO, resolution=RES, env=env, apply=True)
    assert report.ok, report.render()
    assert (xdg["state"] / "ticketctl" / AUDIT_FILE).exists()
    assert list(home.iterdir()) == []


def test_without_xdg_variables_state_and_config_come_from_home(
    tmp_path: Path, fake: FakeProvider
) -> None:
    home = tmp_path / "cron-home"
    write_config(home / ".config" / "ticketctl", fake)
    env = {"PATH": CRON_PATH, "HOME": str(home), TOKEN_VAR: SITE_TOKEN}
    report = cron.run_checks(
        TICKETCTL, key=KEY, to=TO, resolution=RES, env=env, apply=True, token_env=TOKEN_VAR
    )
    assert report.ok, report.render()
    records = audit_records(home / ".local" / "state")
    assert [r["result"] for r in records] == ["attempted", "ok"]


def test_a_missing_ticket_fails_the_read_step(xdg: dict[str, Path]) -> None:
    report = cron.run_checks(
        TICKETCTL, key="DEMO-999", to=TO, resolution=RES, env=scrubbed(xdg), apply=False
    )
    failed = [s.name for s in report.steps if not s.ok]
    assert not report.ok and "read" in failed


def test_a_missing_token_variable_fails_instead_of_prompting(xdg: dict[str, Path]) -> None:
    env = scrubbed(xdg)
    del env[TOKEN_VAR]
    report = cron.run_checks(
        TICKETCTL, key=KEY, to=TO, resolution=RES, env=env, apply=True, timeout=30
    )
    assert not report.ok
    # A bounded failure, not a hang: every step ended with an exit code.
    assert not [s for s in report.steps if s.name.startswith("bounded")]


def test_a_command_that_never_answers_is_reported_as_a_timeout(
    xdg: dict[str, Path], tmp_path: Path
) -> None:
    hang = tmp_path / "hang.py"
    hang.write_text("import time\ntime.sleep(60)\n")
    report = cron.run_checks(
        [sys.executable, str(hang)], key=KEY, to=TO, resolution=RES, env=scrubbed(xdg), timeout=1.0
    )
    assert not report.ok
    assert any(s.name.startswith("bounded") and not s.ok for s in report.steps)


def test_main_runs_the_inherited_environment_and_prints_names_only(
    xdg: dict[str, Path], monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    for name, value in scrubbed(xdg).items():
        monkeypatch.setenv(name, value)
    code = cron.main(
        [
            "--ticketctl",
            f"{sys.executable} -m ticketctl",
            "--key",
            KEY,
            "--to",
            TO,
            "--resolution",
            RES,
            "--token-env",
            TOKEN_VAR,
            "--apply",
        ]
    )
    out = capsys.readouterr().out
    assert code == 0, out
    assert re.search(r"^RESULT PASS$", out, re.M)
    assert TOKEN_VAR in out  # the variable's name is listed
    assert SITE_TOKEN not in out  # never its value
    assert re.search(r"^PASS --yes --expect-plan write", out, re.M)


def test_main_exits_one_when_a_step_fails(
    xdg: dict[str, Path], monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    for name, value in scrubbed(xdg).items():
        monkeypatch.setenv(name, value)
    code = cron.main(
        [
            "--ticketctl",
            f"{sys.executable} -m ticketctl",
            "--key",
            "DEMO-999",
            "--to",
            TO,
            "--resolution",
            RES,
        ]
    )
    assert code == 1
    assert re.search(r"^RESULT FAIL$", capsys.readouterr().out, re.M)
