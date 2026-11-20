"""whoami, sites, sites resolve and offline validate through the CLI (issue #11).

Uses the fake provider (loopback http, ``allow_http``), a private config and a private state
directory. The fake's token is the sentinel of the leak tests.
"""

from __future__ import annotations

import json
import os
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from tests.fake_provider import ME_ACCOUNT, SITE_EMAIL, SITE_TOKEN, FakeProvider, Fault
from tests.golden import assert_golden
from ticketctl import identity, output, sanitize
from ticketctl.cli import main
from ticketctl.config import load_config
from ticketctl.errors import TicketctlError

TOKEN_VAR = "TICKETCTL_TEST_TOKEN"
Run = Callable[..., tuple[int, str, str]]


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
        self.state = root / "state" / "ticketctl"  # $XDG_STATE_HOME/ticketctl
        monkeypatch.setenv("XDG_STATE_HOME", str(root / "state"))
        monkeypatch.setenv(TOKEN_VAR, SITE_TOKEN)

    def write(self, text: str) -> Path:
        self.config.write_text(text, encoding="utf-8")
        self.config.chmod(0o600)
        return self.config

    def site_yaml(self, name: str = "main", url: str = "https://example.test", **kw: str) -> str:
        lines = {
            "url": f"url: {url}",
            "email": f"email: {SITE_EMAIL}",
            "token_env": f"token_env: {TOKEN_VAR}",
            **kw,
        }
        body = "\n".join(f"    {v}" for v in lines.values() if v)
        return f"  {name}:\n{body}\n"

    def fake_site(self, fake: FakeProvider, name: str = "main", **kw: str) -> str:
        return self.site_yaml(name, fake.url, allow_http="allow_http: true", **kw)

    def run(self, *argv: str) -> tuple[int, str, str]:
        code = main(["--config", str(self.config), *argv])
        cap = self.capsys.readouterr()
        return code, cap.out, cap.err

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
def live(env: Env, fake: FakeProvider) -> Env:
    """A config with one site pointing at the fake provider."""
    env.write("sites:\n" + env.fake_site(fake, projects="projects: [DEMO]"))
    return env


def err_of(out: str) -> dict[str, Any]:
    doc = json.loads(out)
    assert list(doc) == ["error"]
    return doc["error"]


# --- whoami ----------------------------------------------------------------------------------


def test_whoami_fresh_then_cached_then_refreshed(live: Env, fake: FakeProvider) -> None:
    code, out, _ = live.run("--json", "whoami")
    assert code == 0
    first = json.loads(out)
    assert first == {
        "site": "main",
        "account_id": ME_ACCOUNT,
        "display_name": "Fake Me",
        "source": "provider",
    }
    assert_golden("cli", "whoami", "provider", first)
    assert [c.path for c in fake.calls] == ["/rest/api/3/myself"]

    code, out, _ = live.run("--json", "whoami")
    cached = json.loads(out)
    assert code == 0 and cached["source"] == "cache" and len(fake.calls) == 1
    assert_golden("cli", "whoami", "cache", cached)

    code, out, _ = live.run("--json", "whoami", "--refresh")
    assert code == 0 and json.loads(out)["source"] == "provider" and len(fake.calls) == 2


def test_whoami_caches_the_time_zone_in_me_json(live: Env) -> None:
    live.run("--json", "whoami")
    doc = json.loads(live.files()["cache/main/me.json"])
    assert doc["time_zone"] == "America/Chicago"
    assert doc["account_id"] == ME_ACCOUNT and doc["schema_version"] == 1


def test_whoami_with_cache_ttl_zero_never_caches(env: Env, fake: FakeProvider) -> None:
    env.write("sites:\n" + env.fake_site(fake, cache_ttl="cache_ttl: 0"))
    env.run("--json", "whoami")
    env.run("--json", "whoami")
    assert len(fake.calls) == 2
    assert "cache/main/me.json" not in env.files()


def test_whoami_human_output(live: Env) -> None:
    code, out, _ = live.run("whoami")
    assert code == 0
    assert "main" in out and ME_ACCOUNT in out and "Fake Me" in out and "provider" in out


def test_whoami_uses_the_selected_site_only(env: Env, fake: FakeProvider) -> None:
    env.write(
        "sites:\n" + env.fake_site(fake, "alpha") + env.site_yaml("beta") + "default_site: alpha\n"
    )
    env.mp.setenv("TICKETCTL_DEFAULT_SITE", "alpha")
    assert json.loads(env.run("--json", "whoami")[1])["site"] == "alpha"
    code, out, _ = env.run("--json", "--site", "alpha", "whoami")
    assert code == 0 and json.loads(out)["site"] == "alpha"


def test_whoami_ambiguous_site_lists_candidates(env: Env, fake: FakeProvider) -> None:
    env.write("sites:\n" + env.fake_site(fake, "alpha") + env.site_yaml("beta"))
    code, out, _ = env.run("--json", "whoami")
    body = err_of(out)
    assert code == 21 and body["code"] == "ambiguous_site"
    assert body["details"] == {"candidates": ["alpha", "beta"]}
    assert_golden("cli", "error", "ambiguous-site", json.loads(out))
    assert fake.calls == []


def test_whoami_unknown_site_flag(live: Env, fake: FakeProvider) -> None:
    code, out, _ = live.run("--json", "--site", "ghost", "whoami")
    body = err_of(out)
    assert (code, body["code"], body["reason"]) == (22, "invalid_request", "unknown_site")
    assert fake.calls == []


def test_whoami_auth_required_names_the_variable(live: Env, fake: FakeProvider) -> None:
    live.mp.delenv(TOKEN_VAR)
    code, out, err = live.run("--json", "whoami")
    body = err_of(out)
    assert (code, body["code"]) == (12, "auth_required")
    assert body["details"] == {"token_env": TOKEN_VAR} and TOKEN_VAR in body["message"]
    assert body["site"] == "main"
    assert_golden("cli", "error", "auth-required", json.loads(out))
    assert fake.calls == []
    live.mp.setenv(TOKEN_VAR, "   ")
    assert live.run("--json", "whoami")[0] == 12


def test_whoami_email_env_unset_is_auth_required(env: Env, fake: FakeProvider) -> None:
    env.write(
        "sites:\n"
        + env.fake_site(fake, email="email_env: TICKETCTL_TEST_EMAIL").replace(
            f"    email: {SITE_EMAIL}\n", ""
        )
    )
    code, out, _ = env.run("--json", "whoami")
    assert code == 12 and err_of(out)["details"] == {"token_env": "TICKETCTL_TEST_EMAIL"}
    env.mp.setenv("TICKETCTL_TEST_EMAIL", SITE_EMAIL)
    assert env.run("--json", "whoami")[0] == 0


@pytest.mark.parametrize(
    ("fault_status", "exit_", "code"),
    [(401, 12, "bad_credentials"), (403, 12, "forbidden"), (404, 12, "provider_changed")],
)
def test_whoami_provider_failures(
    live: Env, fake: FakeProvider, fault_status: int, exit_: int, code: str
) -> None:
    fake.inject(Fault("status", path="myself", status=fault_status, body="nope"))
    status, out, _ = live.run("--json", "whoami")
    body = err_of(out)
    assert (status, body["code"]) == (exit_, code)
    assert body["site"] == "main"


def test_whoami_wrong_credentials_are_bad_credentials(live: Env) -> None:
    live.mp.setenv(TOKEN_VAR, "another-token-0123456789")
    code, out, err = live.run("--json", "whoami")
    assert (code, err_of(out)["code"]) == (12, "bad_credentials")
    assert "another-token-0123456789" not in out + err


def test_whoami_with_the_cache_of_another_email_refetches(live: Env, fake: FakeProvider) -> None:
    live.run("--json", "whoami")
    path = live.state / "cache" / "main" / "me.json"
    doc = json.loads(path.read_text())
    doc["email_sha256"] = "0" * 64
    path.write_text(json.dumps(doc))
    assert json.loads(live.run("--json", "whoami")[1])["source"] == "provider"


@pytest.mark.parametrize(
    "tamper",
    [
        {"account_id": "bad id"},
        {"account_id": 7},
        {"time_zone": "not a zone"},
        {"display_name": ["x"]},
    ],
)
def test_whoami_ignores_a_cache_entry_that_fails_its_grammar(
    live: Env, fake: FakeProvider, tamper: dict[str, Any]
) -> None:
    live.run("--json", "whoami")
    path = live.state / "cache" / "main" / "me.json"
    doc = json.loads(path.read_text())
    doc.update(tamper)
    path.write_text(json.dumps(doc))
    code, out, _ = live.run("--json", "whoami")
    assert code == 0 and json.loads(out)["source"] == "provider"
    assert json.loads(out)["account_id"] == ME_ACCOUNT


def test_whoami_corrupt_cache_is_refetched(live: Env) -> None:
    live.run("--json", "whoami")
    (live.state / "cache" / "main" / "me.json").write_text("{not json")
    code, out, _ = live.run("--json", "whoami")
    assert code == 0 and json.loads(out)["source"] == "provider"


def test_whoami_writes_the_request_log_with_a_path_template(live: Env) -> None:
    live.run("--json", "whoami")
    lines = [json.loads(x) for x in live.files()["log.jsonl"].splitlines()]
    assert len(lines) == 1
    assert lines[0]["path"] == "/rest/api/3/myself" and lines[0]["site"] == "main"
    assert lines[0]["status"] == 200 and lines[0]["method"] == "GET"
    assert len(lines[0]["config_sha256"]) == 64 and lines[0]["config_sha256"] != "0" * 64
    assert "audit.jsonl" not in live.files()  # a read is not audited


def test_an_unusable_state_directory_is_a_warning_not_a_failure(live: Env) -> None:
    live.state.parent.write_text("a file where the state home should be")
    code, out, err = live.run("--json", "whoami")
    assert code == 0 and json.loads(out)["account_id"] == ME_ACCOUNT
    assert "warning" in err and "Traceback" not in err
    assert SITE_TOKEN not in out + err


@pytest.mark.parametrize("argv", [["whoami"], ["whoami", "--refresh"]])
def test_a_symlinked_me_json_is_a_warning_and_a_refetch(live: Env, argv: list[str]) -> None:
    live.run("--json", "whoami")
    target = live.root / "elsewhere.json"
    target.write_text("{}")
    me_file = live.state / "cache" / "main" / "me.json"
    me_file.unlink()
    me_file.symlink_to(target)
    code, out, err = live.run("--json", *argv)
    assert code == 0 and json.loads(out)["source"] == "provider"
    assert "warning" in err and "Traceback" not in err


def test_quiet_drops_warnings_not_results(env: Env) -> None:
    env.write(TWO)
    env.mp.setenv("TICKETCTL_SITES__ZED__URL", "https://evil.test")  # a loader warning
    code, out, err = env.run("sites")
    assert code == 0 and "warning" in err and "TICKETCTL_SITES__ZED__URL" in err
    code, out, err = env.run("--quiet", "sites")
    assert code == 0 and "alpha" in out and err == ""


# --- the account expectation -----------------------------------------------------------------


def test_expectation_mismatch_is_a_warning_for_reads(env: Env, fake: FakeProvider) -> None:
    env.write("sites:\n" + env.fake_site(fake, account_id="account_id: acct-9999"))
    code, out, err = env.run("--json", "whoami")
    assert code == 0 and json.loads(out)["account_id"] == ME_ACCOUNT
    assert "warning" in err and "expectation" in err and "main" in err


def test_me_jira_expectation_and_site_override(env: Env, fake: FakeProvider) -> None:
    env.write("me:\n  jira: acct-9999\nsites:\n" + env.fake_site(fake))
    assert "warning" in env.run("--json", "whoami")[2]
    env.write(
        "me:\n  jira: acct-9999\nsites:\n"
        + env.fake_site(fake, account_id=f"account_id: {ME_ACCOUNT}")
    )
    assert env.run("--json", "whoami")[2] == ""  # the site's account_id overrides me.jira


def test_matching_expectation_is_silent(env: Env, fake: FakeProvider) -> None:
    env.write("sites:\n" + env.fake_site(fake, account_id=f"account_id: {ME_ACCOUNT}"))
    assert env.run("--json", "whoami")[2] == ""


def test_write_expectation_mismatch_is_exit_2_account_mismatch(
    env: Env, fake: FakeProvider
) -> None:
    env.write("sites:\n" + env.fake_site(fake, account_id="account_id: acct-9999"))
    config = load_config(env.config, env=dict(os.environ)).config
    with pytest.raises(TicketctlError) as exc:
        identity.check_expectation(config, "main", ME_ACCOUNT, write=True)
    assert (exc.value.exit, exc.value.code, exc.value.reason) == (
        2,
        "invalid_config",
        "account_mismatch",
    )
    assert exc.value.site == "main"
    assert identity.check_expectation(config, "main", "acct-9999", write=True) is None
    assert_golden("cli", "error", "account-mismatch", {"error": exc.value.body()})


def test_no_expectation_never_warns() -> None:
    from ticketctl.config import Config

    cfg = Config.model_validate(
        {"sites": {"a": {"url": "https://example.test", "email": "a@b.test", "token_env": "T"}}}
    )
    assert identity.check_expectation(cfg, "a", "anything", write=False) is None


class _Resp:
    def __init__(self, status: int, data: Any) -> None:
        self.status, self.data = status, data


class _Gw:
    def __init__(self, status: int, data: Any) -> None:
        self._r = _Resp(status, data)

    def request(self, *a: Any, **k: Any) -> _Resp:
        return self._r


@pytest.mark.parametrize(
    ("status", "data"),
    [
        (200, None),
        (200, []),
        (200, "text"),
        (200, {}),
        (200, {"accountId": "bad id"}),
        (200, {"accountId": 12}),
        (200, {"accountId": "x" * 129}),
        (400, {"accountId": "acct-1"}),
        (404, None),
    ],
)
def test_fetch_me_refuses_an_unusable_answer(status: int, data: Any) -> None:
    with pytest.raises(TicketctlError) as exc:
        identity.fetch_me(_Gw(status, data))  # type: ignore[arg-type]
    assert (exc.value.exit, exc.value.code) == (12, "provider_changed")


def test_fetch_me_sanitizes_the_untrusted_display_name() -> None:
    hostile = "A\x1b[31m‮B" + "z" * 400
    me = identity.fetch_me(
        _Gw(200, {"accountId": "acct-1", "displayName": hostile, "timeZone": "Not A Zone"})  # type: ignore[arg-type]
    )
    assert me.display_name is not None
    assert "\x1b" not in me.display_name and "‮" not in me.display_name
    assert len(me.display_name) <= 256
    assert me.time_zone is None  # fails its grammar: dropped, not an error


@pytest.mark.parametrize("name", [12, ["x"], {"a": 1}, True])
def test_fetch_me_drops_a_non_string_display_name(name: Any) -> None:
    me = identity.fetch_me(_Gw(200, {"accountId": "acct-1", "displayName": name}))  # type: ignore[arg-type]
    assert me.account_id == "acct-1" and me.display_name is None


# --- sites -----------------------------------------------------------------------------------

TWO = """\
sites:
  zed:
    url: https://example.test/Jira
    email: me@example.test
    token_env: TICKETCTL_UNSET_ONE
    projects: [DEMO, AB]
  alpha:
    url: https://other.test
    email_env: TICKETCTL_UNSET_TWO
    token_env: TICKETCTL_UNSET_THREE
default_site: alpha
"""


def test_sites_lists_sites_sorted_with_default(env: Env) -> None:
    env.write(TWO)
    code, out, err = env.run("--json", "sites")
    assert code == 0 and err == ""
    doc = json.loads(out)
    assert [s["name"] for s in doc["sites"]] == ["alpha", "zed"]
    assert doc["default_site"] == "alpha"
    assert doc["sites"][0] == {
        "name": "alpha",
        "provider": "jira",
        "url": "https://other.test",
        "projects": [],
        "default": True,
    }
    assert doc["sites"][1]["url"] == "https://example.test/Jira"
    assert_golden("cli", "sites", "two-sites", doc)


def test_sites_with_no_sites_is_an_empty_listing(env: Env) -> None:
    env.write("sites: {}\n")
    code, out, _ = env.run("--json", "sites")
    assert code == 0
    assert_golden("cli", "sites", "empty", json.loads(out))


def test_sites_human_output(env: Env) -> None:
    env.write(TWO)
    code, out, _ = env.run("sites")
    assert code == 0 and "alpha (default)" in out and "DEMO, AB" in out


def test_sites_json_has_no_credential_fields(env: Env) -> None:
    env.write(TWO)
    out = env.run("--json", "sites")[1]
    for word in ("token", "email", "TICKETCTL_UNSET", "me@example.test"):
        assert word not in out


@pytest.mark.parametrize("argv", [["sites"], ["sites", "resolve", "DEMO-1"], ["validate"]])
def test_offline_commands_never_read_a_credential_variable(
    env: Env, monkeypatch: pytest.MonkeyPatch, argv: list[str]
) -> None:
    env.write(TWO)
    reads: list[str] = []

    class Spy(dict):  # type: ignore[type-arg]
        def get(self, key: str, default: Any = None) -> Any:
            reads.append(key)
            return super().get(key, default)

        def __getitem__(self, key: str) -> Any:
            reads.append(key)
            return super().__getitem__(key)

    monkeypatch.setattr("ticketctl.runtime.Target.from_site", lambda *a, **k: 1 / 0)
    spy = Spy(os.environ)
    monkeypatch.setattr(os, "environ", spy)
    code, _, _ = env.run("--json", *argv)
    assert code == 0
    assert not [k for k in reads if k.startswith("TICKETCTL_UNSET")]


def test_offline_commands_never_touch_the_network(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    import socket

    env.write(TWO)

    def refuse(*a: object, **k: object) -> None:
        raise AssertionError("offline command used a socket")

    monkeypatch.setattr(socket, "socket", refuse)
    monkeypatch.setattr(socket, "create_connection", refuse)
    for argv in (["sites"], ["sites", "resolve", "DEMO-1"], ["validate"]):
        assert env.run("--json", *argv)[0] == 0


# --- sites resolve ---------------------------------------------------------------------------


def test_resolve_rules(env: Env) -> None:
    env.write(TWO)
    cases = {
        "project": (["DEMO-4"], ("zed", "project")),
        "default-site": (["TEST-1"], ("alpha", "default_site")),
        "site-flag": (["--site", "zed", "TEST-1"], ("zed", "site_flag")),
    }
    for case, (argv, expected) in cases.items():
        code, out, _ = env.run("--json", "sites", "resolve", *argv)
        doc = json.loads(out)
        assert code == 0 and (doc["site"], doc["rule"]) == expected
        assert_golden("cli", "sites-resolve", case, doc)


def test_resolve_by_browse_url_compares_only_host_and_port(env: Env) -> None:
    env.write(TWO)
    code, out, _ = env.run(
        "--json", "sites", "resolve", "https://other.test/browse/AB-2?focusedCommentId=5"
    )
    assert code == 0 and json.loads(out) == {"key": "AB-2", "site": "alpha", "rule": "url"}
    # the path must be exactly /browse/<KEY>: a site context path is not part of it
    code, out, _ = env.run("--json", "sites", "resolve", "https://example.test/Jira/browse/AB-2")
    assert (code, err_of(out)["reason"]) == (22, "invalid_value")


def test_resolve_url_and_only_site_rules(env: Env) -> None:
    env.write("sites:\n" + env.site_yaml("solo"))
    doc = json.loads(env.run("--json", "sites", "resolve", "ab-3")[1])
    assert doc == {"key": "AB-3", "site": "solo", "rule": "only_site"}
    assert_golden("cli", "sites-resolve", "only-site", doc)
    code, out, _ = env.run("--json", "sites", "resolve", "https://example.test/BROWSE/AB-3")
    assert code == 22  # the path is case-sensitive: /browse/<KEY> only
    code, out, _ = env.run(
        "--json", "sites", "resolve", "https://example.test/browse/AB-3#comment-1"
    )
    doc = json.loads(out)
    assert code == 0 and doc == {"key": "AB-3", "site": "solo", "rule": "url"}
    assert_golden("cli", "sites-resolve", "url", doc)


def test_resolve_ambiguous_is_exit_21_with_candidates(env: Env) -> None:
    env.write(
        "sites:\n"
        + env.site_yaml("one", projects="projects: [AB]")
        + env.site_yaml("two", "https://other.test", projects="projects: [AB]")
    )
    code, out, err = env.run("--json", "sites", "resolve", "AB-1")
    body = err_of(out)
    assert code == 21 and body["code"] == "ambiguous_site"
    assert body["details"]["candidates"] == ["one", "two"]
    assert "ambiguous_site" in err
    assert_golden("cli", "error", "ambiguous-key", json.loads(out))
    assert env.run("--json", "--site", "two", "sites", "resolve", "AB-1")[0] == 0


def test_resolve_invalid_key_and_mixed(env: Env) -> None:
    env.write("sites:\n" + env.site_yaml("solo"))
    code, out, _ = env.run("--json", "sites", "resolve", "nope")
    assert (code, err_of(out)["reason"]) == (22, "invalid_key")
    code, out, _ = env.run("--json", "sites", "resolve")
    assert code == 2  # argparse: the key is required


def test_resolve_no_sites(env: Env) -> None:
    env.write("sites: {}\n")
    code, out, _ = env.run("--json", "sites", "resolve", "AB-1")
    body = err_of(out)
    assert (code, body["code"], body["reason"]) == (2, "invalid_config", "no_sites")
    assert_golden("cli", "error", "no-sites", json.loads(out))


# --- validate --------------------------------------------------------------------------------


def test_validate_offline(env: Env) -> None:
    env.write(TWO)
    code, out, err = env.run("--json", "validate")
    doc = json.loads(out)
    assert code == 0 and err == ""
    assert doc == {"valid": True, "probe": False, "warnings": []}
    assert_golden("cli", "validate", "offline", doc)


def test_validate_reports_loader_warnings(env: Env) -> None:
    env.write(TWO)
    env.mp.setenv("TICKETCTL_SITES__MAIN__URL", "https://evil.test")
    code, out, err = env.run("--json", "validate")
    doc = json.loads(out)
    assert code == 0 and len(doc["warnings"]) == 1
    assert "TICKETCTL_SITES__MAIN__URL" in doc["warnings"][0] and "evil.test" not in out
    assert_golden("cli", "validate", "with-warning", doc)
    code, out, _ = env.run("validate")
    assert "warning:" in out


def test_validate_invalid_config_names_the_key_not_the_value(env: Env) -> None:
    secret = "SENTINEL-s3cr3t-value-9f2c"
    env.write(
        "sites:\n  main:\n    url: https://example.test\n    email: me@example.test\n"
        f"    token_env: 'bad name {secret}'\n"
    )
    code, out, err = env.run("--json", "validate")
    body = err_of(out)
    assert (code, body["code"], body["reason"]) == (2, "invalid_config", "config_invalid")
    assert "sites.main.token_env" in body["message"]
    assert secret not in out + err
    assert_golden("cli", "error", "config-invalid", json.loads(out))


def test_validate_with_no_sites_is_no_sites(env: Env) -> None:
    env.write("{}\n")
    code, out, _ = env.run("--json", "validate")
    assert (code, err_of(out)["reason"]) == (2, "no_sites")


def test_validate_missing_config_file_is_config_invalid(env: Env) -> None:
    code = main(["--config", str(env.root / "missing.yaml"), "--json", "validate"])
    out = env.capsys.readouterr().out
    assert (code, err_of(out)["reason"]) == (2, "config_invalid")


def test_validate_probe_is_not_implemented_yet(env: Env) -> None:
    env.write(TWO)
    code, out, err = env.run("--json", "validate", "--probe")
    assert code == 1 and err_of(out)["code"] == "internal"
    assert err_of(out)["message"] == "validate --probe is not implemented yet"
    assert "NotImplementedError" not in out + err and "Traceback" not in err


def test_config_file_contents_are_never_echoed(env: Env) -> None:
    env.write("sites: [unterminated SENTINEL-s3cr3t-value-9f2c\n  : :\n")
    code, out, err = env.run("--json", "sites")
    assert code == 2
    assert "SENTINEL" not in out + err


# --- the token never leaves the process ------------------------------------------------------

COMMANDS = [
    ["whoami"],
    ["whoami", "--refresh"],
    ["sites"],
    ["sites", "resolve", "DEMO-1"],
    ["validate"],
]


@pytest.mark.parametrize("argv", COMMANDS, ids=lambda a: " ".join(a))
@pytest.mark.parametrize("debug", [False, True])
@pytest.mark.parametrize("flags", [[], ["--verbose"], ["--json", "--verbose"]])
def test_token_never_appears_in_any_channel(
    live: Env, fake: FakeProvider, argv: list[str], debug: bool, flags: list[str]
) -> None:
    if debug:
        live.mp.setenv("TICKETCTL_DEBUG", "1")
    code, out, err = live.run(*flags, *argv)
    assert code == 0
    forbidden = [SITE_TOKEN, fake.basic_header().split(" ", 1)[1]]
    for text in (out, err, *live.files().values()):
        for secret in forbidden:
            assert secret not in text
    # the verbose line shows the template, never headers
    if "--verbose" in flags and argv[0] == "whoami":
        assert "GET /rest/api/3/myself" in err
        assert "Authorization" not in err


def test_token_is_scrubbed_from_an_error_that_echoes_it(live: Env, fake: FakeProvider) -> None:
    fake.inject(Fault("status", path="myself", status=403, body=f"denied {SITE_TOKEN}"))
    code, out, err = live.run("--json", "--verbose", "whoami")
    assert code == 12
    for text in (out, err, *live.files().values()):
        assert SITE_TOKEN not in text


def test_the_scrub_covers_a_traceback_of_an_unexpected_error(
    live: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    def boom(*a: Any, **k: Any) -> None:
        raise RuntimeError(f"leaked {SITE_TOKEN}")

    monkeypatch.setattr("ticketctl.identity.fetch_me", boom)
    live.mp.setenv("TICKETCTL_DEBUG", "1")
    code, out, err = live.run("--json", "whoami")
    assert code == 1 and "Traceback" in err
    assert SITE_TOKEN not in out + err


def test_the_scrub_is_armed_before_the_first_output(
    live: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen: list[bool] = []
    real = sanitize.configure_scrubber

    def spy(secrets: Any, **kw: Any) -> None:
        seen.append(SITE_TOKEN in list(secrets))
        real(secrets, **kw)

    monkeypatch.setattr("ticketctl.runtime.configure_scrubber", spy)
    live.run("--json", "whoami")
    assert seen == [True]
    # the Basic value is registered too
    assert sanitize.basic_auth_value(SITE_EMAIL, SITE_TOKEN)  # sanity


def test_a_token_in_a_direct_write_is_redacted_by_the_output_path() -> None:
    import io

    from ticketctl.output import write

    sanitize.configure_scrubber(
        [SITE_TOKEN], basic_values=[sanitize.basic_auth_value(SITE_EMAIL, SITE_TOKEN)]
    )
    buf = io.StringIO()
    write(buf, f"x {SITE_TOKEN} y {sanitize.basic_auth_value(SITE_EMAIL, SITE_TOKEN)}")
    assert SITE_TOKEN not in buf.getvalue() and "<redacted>" in buf.getvalue()
    write(buf, "bad\x1b[31m")
    assert "\x1b" not in buf.getvalue()


@pytest.mark.parametrize(
    "block",
    [
        "  'bad name':\n    url: https://example.test\n    email: a@b.test\n    token_env: T\n",
        "  main:\n    url: 'https://example.test/a b'\n    email: a@b.test\n    token_env: T\n",
        "  main:\n    url: 'https://user@example.test'\n    email: a@b.test\n    token_env: T\n",
        "  main:\n    url: 'http://example.test'\n    email: a@b.test\n    token_env: T\n",
        "  main:\n    url: https://example.test\n    email: a@b.test\n    token_env: 'A B'\n",
    ],
)
def test_hostile_site_definitions_are_refused_by_every_command(env: Env, block: str) -> None:
    env.write("sites:\n" + block)
    for argv in (["sites"], ["validate"], ["whoami"], ["sites", "resolve", "AB-1"]):
        code, out, err = env.run("--json", *argv)
        assert (code, err_of(out)["reason"]) == (2, "config_invalid"), argv
        assert "Traceback" not in err


def test_resolve_a_browse_url_with_an_overlong_key_is_invalid_key(env: Env) -> None:
    env.write(TWO)
    long_key = "A" * 70 + "-1"
    code, out, _ = env.run("--json", "sites", "resolve", f"https://example.test/browse/{long_key}")
    assert (code, err_of(out)["code"]) == (22, "invalid_request")
    assert err_of(out)["reason"] == "invalid_key"


def test_no_sites_wins_over_an_unknown_site_flag(env: Env) -> None:
    env.write("sites: {}\n")
    code, out, _ = env.run("--json", "--site", "nope", "sites", "resolve", "AB-1")
    assert (code, err_of(out)["reason"]) == (2, "no_sites")


def test_a_nul_in_the_config_path_is_config_invalid(env: Env) -> None:
    code = main(["--json", "--config", "a\x00b", "sites"])
    cap = env.capsys.readouterr()
    assert code == 2 and err_of(cap.out)["reason"] == "config_invalid"
    assert "Traceback" not in cap.err


# --- the scrub really guards the output (fails when it is disarmed) ---------------------------


def test_a_token_in_a_provider_display_name_is_redacted_everywhere(
    live: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    hostile = _Resp(200, {"accountId": "acct-1", "displayName": f"Mr {SITE_TOKEN}"})
    monkeypatch.setattr("ticketctl.gateway.Gateway.request", lambda *a, **k: hostile)
    for flags in (["--json"], [], ["--json", "--verbose"]):
        code, out, err = live.run(*flags, "whoami", "--refresh")
        assert code == 0, (flags, err)
        assert SITE_TOKEN not in out + err
        assert "<redacted>" in out


def test_a_token_in_an_error_message_is_redacted_in_envelope_and_stderr(
    live: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    def boom(*a: Any, **k: Any) -> None:
        raise TicketctlError("internal", f"failed with {SITE_TOKEN} inside")

    monkeypatch.setattr("ticketctl.identity.fetch_me", boom)
    code, out, err = live.run("--json", "whoami")
    assert code == 1
    assert SITE_TOKEN not in out + err
    assert "<redacted>" in out and "<redacted>" in err


def test_the_scrub_is_armed_before_any_output_is_written(
    live: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    events: list[str] = []
    real_configure = sanitize.configure_scrubber

    def configure(secrets: Any, **kw: Any) -> None:
        events.append("armed")
        real_configure(secrets, **kw)

    monkeypatch.setattr("ticketctl.runtime.configure_scrubber", configure)
    real_write = output.write

    def write(stream: Any, text: str) -> None:
        events.append("write")
        real_write(stream, text)

    monkeypatch.setattr("ticketctl.output.write", write)
    monkeypatch.setattr("ticketctl.cli._write", write)
    live.run("--json", "--verbose", "whoami")
    assert "write" in events and events[0] == "armed"
