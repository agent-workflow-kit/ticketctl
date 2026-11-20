"""Config loader: drop-ins, environment scalars, grammars and loader checks.

Every test passes an explicit environment mapping with private HOME and XDG_CONFIG_HOME
directories; none reads the real configuration.
"""

from __future__ import annotations

import os
import re
import traceback
from collections.abc import Callable
from datetime import timedelta
from pathlib import Path

import pytest
import yaml

from ticketctl import config as config_module
from ticketctl.config import Config, ConfigError, load_config

ROOT = Path(__file__).resolve().parent.parent
SENTINEL = "SENTINEL-s3cr3t-value-9f2c"

MINIMAL = """\
sites:
  main:
    url: https://example.test
    email: me@example.test
    token_env: TEST_TOKEN
"""


class Home:
    """A private home with helpers to write config files."""

    def __init__(self, root: Path) -> None:
        self.root = root
        self.cfg = root / "xdg" / "ticketctl"
        self.cfg.mkdir(parents=True)
        self.env = {"HOME": str(root / "home"), "XDG_CONFIG_HOME": str(root / "xdg")}

    def write(self, rel: str, text: str | bytes, mode: int = 0o600) -> Path:
        path = self.cfg / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        if isinstance(text, bytes):
            path.write_bytes(text)
        else:
            path.write_text(text, encoding="utf-8")
        path.chmod(mode)
        return path

    def main(self, text: str = MINIMAL) -> Path:
        return self.write("config.yaml", text)

    def load(self, **extra_env: str) -> config_module.LoadedConfig:
        return load_config(env={**self.env, **extra_env})


@pytest.fixture
def home(tmp_path: Path) -> Home:
    return Home(tmp_path)


def site_text(**overrides: str) -> str:
    """A minimal config whose site `main` has extra or replaced lines (raw YAML lines)."""
    lines = {
        "url": "url: https://example.test",
        "email": "email: me@example.test",
        "token_env": "token_env: TEST_TOKEN",
    }
    lines.update(overrides)
    body = "\n".join(f"    {v}" for v in lines.values() if v)
    return f"sites:\n  main:\n{body}\n"


def assert_clean(err: ConfigError) -> None:
    """No chained exception and no sentinel in repr, str or the full formatted traceback."""
    assert err.__context__ is None and err.__cause__ is None
    assert SENTINEL not in repr(err)
    assert SENTINEL not in str(err)
    assert SENTINEL not in "".join(traceback.format_exception(err))


def expect_error(home: Home, text: str, key: str | None = None, **env: str) -> ConfigError:
    home.main(text)
    with pytest.raises(ConfigError) as exc:
        home.load(**env)
    err = exc.value
    assert err.code == "invalid_config"
    assert err.reason == "config_invalid"
    assert err.exit_code == 2
    if key is not None:
        assert err.key == key
    assert SENTINEL not in str(err)
    return err


# --- defaults and the happy path -------------------------------------------------------------


def test_minimal_site_gets_defaults(home: Home) -> None:
    home.main()
    cfg = home.load().config
    site = cfg.sites["main"]
    assert site.provider == "jira"
    assert site.allow_http is False
    assert site.blocking_link_types == ["Blocks"]
    assert site.projects == []
    assert site.cache_ttl == timedelta(hours=24)
    assert site.timeouts.connect == timedelta(seconds=10)
    assert site.timeouts.read == timedelta(seconds=30)
    assert cfg.timeout == timedelta(seconds=120)
    assert cfg.default_site is None
    assert cfg.attribution.enabled is True
    assert cfg.attribution.template == "Drafted with AI assistance and posted via ticketctl."
    assert cfg.limits.body_chars == 30000
    assert cfg.limits.retry_after_max == timedelta(seconds=60)
    assert cfg.hooks == {}


def test_missing_default_config_is_an_empty_config_not_an_error(home: Home) -> None:
    loaded = home.load()
    assert loaded.config.sites == {}
    assert loaded.files == []
    with pytest.raises(ConfigError) as exc:
        loaded.config.require_sites()
    assert exc.value.reason == "no_sites"


def test_empty_file_is_an_empty_config(home: Home) -> None:
    home.main("")
    assert home.load().config.sites == {}


def test_example_config_loads(home: Home) -> None:
    home.main((ROOT / "config.example.yaml").read_text(encoding="utf-8"))
    cfg = home.load().config
    site = cfg.sites["main"]
    assert str(site.url) == "https://example.atlassian.net"
    assert site.board == 12
    assert site.fields.estimate is not None and site.fields.estimate.name == "Story Points"
    assert "team" in site.fields.extra
    assert cfg.default_site == "main"
    assert cfg.me.name == "me"


def test_example_config_documents_every_schema_key() -> None:
    import json

    schema = json.loads((ROOT / "schemas" / "config.json").read_text(encoding="utf-8"))
    text = (ROOT / "config.example.yaml").read_text(encoding="utf-8")
    site = schema["$defs"]["site"]["properties"]
    keys = set(schema["properties"]) | set(site)
    for key in keys:
        assert re.search(rf"^\s*#?\s*{re.escape(key)}:", text, re.M), key


def test_model_fields_match_schema_properties() -> None:
    import json

    from ticketctl.config import Site

    schema = json.loads((ROOT / "schemas" / "config.json").read_text(encoding="utf-8"))
    assert set(Config.model_fields) == set(schema["properties"])
    assert set(Site.model_fields) == set(schema["$defs"]["site"]["properties"])


def test_url_is_normalized(home: Home) -> None:
    home.main(site_text(url="url: HTTPS://Example.TEST:443/Jira/"))
    assert str(home.load().config.sites["main"].url) == "https://example.test/Jira"


def test_me_name_defaults_to_os_user_normalized(
    home: Home, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(config_module, "_os_user", lambda: "dev user/é")
    home.main()
    assert home.load().config.me.name == "dev_user__"


def test_me_name_is_normalized_to_actor_pattern(home: Home) -> None:
    home.main(MINIMAL + 'me:\n  name: "A b:c\\u00e9"\n')
    name = home.load().config.me.name
    assert name == "A_b_c_"
    assert re.fullmatch(r"[A-Za-z0-9._@-]{1,64}", name)


def test_me_name_truncated_to_64(home: Home) -> None:
    home.main(MINIMAL + "me:\n  name: " + "x" * 100 + "\n")
    assert home.load().config.me.name == "x" * 64


@pytest.mark.parametrize("value", ["''", "'" + "x" * 129 + "'", "5", "[a]"])
def test_me_name_rejects(home: Home, value: str) -> None:
    expect_error(home, MINIMAL + f"me:\n  name: {value}\n", "me.name")


@pytest.mark.parametrize("value", ["abc:123-x_y.z", "5b10ac8d82e05b22cc7d4ef5", "a" * 128])
def test_me_jira_accepts(home: Home, value: str) -> None:
    home.main(MINIMAL + f"me:\n  jira: '{value}'\n")
    assert home.load().config.me.jira == value


@pytest.mark.parametrize("value", ["''", "'a b'", "'a/b'", "'" + "a" * 129 + "'", "'x\\n'", "7"])
def test_me_jira_rejects(home: Home, value: str) -> None:
    expect_error(home, MINIMAL + f"me:\n  jira: {value}\n", "me.jira")


@pytest.mark.parametrize("value", ["abc:123", "x" * 128])
def test_account_id_accepts(home: Home, value: str) -> None:
    home.main(site_text(account_id=f"account_id: '{value}'"))
    assert home.load().config.sites["main"].account_id == value


@pytest.mark.parametrize("value", ["'a b'", "''", "'bad/id'", "'x\\n'", "'" + "a" * 129 + "'"])
def test_account_id_rejects(home: Home, value: str) -> None:
    expect_error(home, site_text(account_id=f"account_id: {value}"), "sites.main.account_id")


# --- drop-ins and merge order -----------------------------------------------------------------


def test_dropins_merge_in_lexical_order(home: Home) -> None:
    home.main(MINIMAL + "timeout: 10s\nme:\n  name: base\n  jira: base-id\n")
    home.write("config.d/20-b.yaml", "timeout: 30s\nme:\n  name: second\n")
    home.write("config.d/10-a.yaml", "timeout: 20s\nme:\n  name: first\n")
    loaded = home.load()
    cfg = loaded.config
    assert cfg.timeout == timedelta(seconds=30)
    assert cfg.me.name == "second"
    assert cfg.me.jira == "base-id"  # maps merge, untouched keys survive
    assert [p.name for p in loaded.files] == ["config.yaml", "10-a.yaml", "20-b.yaml"]


def test_dropins_sort_is_by_name_not_by_creation(home: Home) -> None:
    home.main(MINIMAL)
    home.write("config.d/b.yaml", "timeout: 20s\n")
    home.write("config.d/a.yaml", "timeout: 99s\n")
    assert home.load().config.timeout == timedelta(seconds=20)


def test_dropin_can_add_a_site_and_merge_into_one(home: Home) -> None:
    home.main(MINIMAL)
    home.write(
        "config.d/x.yaml",
        "sites:\n  other:\n    url: https://other.test\n    email: a@b.test\n    token_env: T2\n"
        "  main:\n    allow_http: false\n    board: 7\n",
    )
    cfg = home.load().config
    assert set(cfg.sites) == {"main", "other"}
    assert cfg.sites["main"].board == 7
    assert str(cfg.sites["main"].url) == "https://example.test"


def test_dropin_without_config_yaml(home: Home) -> None:
    home.write("config.d/only.yaml", MINIMAL)
    assert "main" in home.load().config.sites


def test_non_hook_lists_replace(home: Home) -> None:
    home.main(site_text(projects="projects: [AAA, BBB]"))
    home.write("config.d/x.yaml", "sites:\n  main:\n    projects: [CCC]\n")
    assert home.load().config.sites["main"].projects == ["CCC"]


def test_hook_lists_append_per_hook_name(home: Home) -> None:
    home.main(
        MINIMAL + "hooks:\n  pre-transition:\n    - cmd: one\n  post-comment:\n    - cmd: keep\n"
    )
    home.write("config.d/a.yaml", "hooks:\n  pre-transition:\n    - cmd: two\n      timeout: 5\n")
    home.write("config.d/b.yaml", "hooks:\n  pre-transition:\n    - cmd: three\n")
    hooks = home.load().config.hooks
    assert [h.cmd for h in hooks["pre-transition"]] == ["one", "two", "three"]
    assert hooks["pre-transition"][1].timeout == timedelta(seconds=5)
    assert [h.cmd for h in hooks["post-comment"]] == ["keep"]


def test_scalar_replaced_by_later_file(home: Home) -> None:
    home.main(MINIMAL + "limits:\n  body_chars: 100\n")
    home.write("config.d/a.yaml", "limits:\n  body_chars: 200\n")
    assert home.load().config.limits.body_chars == 200


def test_invalid_merge_is_validated_after_merging(home: Home) -> None:
    # Each file alone is fine; the merged document has both email and email_env.
    home.main(MINIMAL)
    home.write("config.d/a.yaml", "sites:\n  main:\n    email_env: MAIL\n")
    with pytest.raises(ConfigError) as exc:
        home.load()
    assert exc.value.key == "sites.main.email"


def test_dropins_ignore_other_extensions_dotfiles_and_subdirs(home: Home) -> None:
    home.main(MINIMAL)
    home.write("config.d/a.yml", "timeout: 11s\n")
    home.write("config.d/.hidden.yaml", "timeout: 12s\n")
    home.write("config.d/a.yaml.bak", "timeout: 13s\n")
    home.write("config.d/a.yaml~", "timeout: 14s\n")
    home.write("config.d/sub/x.yaml", "timeout: 15s\n")
    loaded = home.load()
    assert loaded.config.timeout == timedelta(seconds=120)
    assert [p.name for p in loaded.files] == ["config.yaml"]


def test_dropin_named_yaml_that_is_a_directory_is_refused(home: Home) -> None:
    home.main(MINIMAL)
    (home.cfg / "config.d" / "evil.yaml").mkdir(parents=True)
    with pytest.raises(ConfigError, match="regular file"):
        home.load()


def test_too_many_dropins_refused(home: Home) -> None:
    home.main(MINIMAL)
    for i in range(config_module.MAX_DROPINS + 1):
        home.write(f"config.d/{i:04}.yaml", "{}\n")
    with pytest.raises(ConfigError, match="drop-in"):
        home.load()


def test_explicit_config_loads_only_that_file(home: Home, tmp_path: Path) -> None:
    home.main(MINIMAL + "timeout: 10s\n")
    home.write("config.d/a.yaml", "timeout: 20s\n")
    other = tmp_path / "other.yaml"
    other.write_text(MINIMAL.replace("example.test", "other.test"))
    other.chmod(0o600)
    loaded = load_config(other, env=home.env)
    assert str(loaded.config.sites["main"].url) == "https://other.test"
    assert loaded.config.timeout == timedelta(seconds=120)
    assert loaded.files == [other]


def test_explicit_config_missing_is_an_error(home: Home, tmp_path: Path) -> None:
    with pytest.raises(ConfigError) as exc:
        load_config(tmp_path / "nope.yaml", env=home.env)
    assert exc.value.reason == "config_invalid"


def test_xdg_unset_falls_back_to_home_dot_config(tmp_path: Path) -> None:
    cfg = tmp_path / "home" / ".config" / "ticketctl"
    cfg.mkdir(parents=True)
    (cfg / "config.yaml").write_text(MINIMAL)
    (cfg / "config.yaml").chmod(0o600)
    assert "main" in load_config(env={"HOME": str(tmp_path / "home")}).config.sites


def test_relative_xdg_is_ignored(tmp_path: Path) -> None:
    cfg = tmp_path / "home" / ".config" / "ticketctl"
    cfg.mkdir(parents=True)
    (cfg / "config.yaml").write_text(MINIMAL)
    (cfg / "config.yaml").chmod(0o600)
    env = {"HOME": str(tmp_path / "home"), "XDG_CONFIG_HOME": "relative/dir"}
    assert "main" in load_config(env=env).config.sites


def test_default_environment_is_os_environ(home: Home, monkeypatch: pytest.MonkeyPatch) -> None:
    home.main()
    monkeypatch.setenv("XDG_CONFIG_HOME", home.env["XDG_CONFIG_HOME"])
    monkeypatch.setenv("HOME", home.env["HOME"])
    assert "main" in load_config().config.sites


# --- environment scalars ----------------------------------------------------------------------


def test_env_timeout_overrides_config(home: Home) -> None:
    home.main(MINIMAL + "timeout: 10s\n")
    assert home.load(TICKETCTL_TIMEOUT="5m").config.timeout == timedelta(minutes=5)


def test_env_timeout_bare_integer_is_seconds(home: Home) -> None:
    home.main()
    assert home.load(TICKETCTL_TIMEOUT="45").config.timeout == timedelta(seconds=45)


@pytest.mark.parametrize("value", ["0", "999ms", "25h", "2d", "abc", "1.5s", "-3", "5 s", "5s\n"])
def test_env_timeout_rejects_without_echo(home: Home, value: str) -> None:
    home.main()
    with pytest.raises(ConfigError) as exc:
        home.load(TICKETCTL_TIMEOUT=value)
    assert exc.value.key == "TICKETCTL_TIMEOUT"
    assert value.strip() not in str(exc.value) or value.strip() == ""


def test_env_empty_scalars_are_unset(home: Home) -> None:
    home.main(MINIMAL + "timeout: 10s\ndefault_site: main\n")
    cfg = home.load(TICKETCTL_TIMEOUT="", TICKETCTL_DEFAULT_SITE="").config
    assert cfg.timeout == timedelta(seconds=10)
    assert cfg.default_site == "main"


def test_env_default_site(home: Home) -> None:
    home.main(
        MINIMAL + "  other:\n    url: https://o.test\n    email: a@b.test\n    token_env: T\n"
    )
    assert home.load(TICKETCTL_DEFAULT_SITE="other").config.default_site == "other"


def test_env_default_site_must_name_a_site(home: Home) -> None:
    home.main()
    with pytest.raises(ConfigError) as exc:
        home.load(TICKETCTL_DEFAULT_SITE="ghost")
    assert exc.value.key == "TICKETCTL_DEFAULT_SITE"
    assert "ghost" not in str(exc.value)


@pytest.mark.parametrize("value", ["-bad", "a b", "x" * 65, "a/b", "a\n"])
def test_env_default_site_grammar(home: Home, value: str) -> None:
    home.main()
    with pytest.raises(ConfigError) as exc:
        home.load(TICKETCTL_DEFAULT_SITE=value)
    assert exc.value.key == "TICKETCTL_DEFAULT_SITE"


def test_env_overrides_a_config_default_site_that_is_valid(home: Home) -> None:
    home.main(
        MINIMAL + "  other:\n    url: https://other.test\n    email: a@b.test\n"
        "    token_env: T2\ndefault_site: main\n"
    )
    assert home.load().config.default_site == "main"
    assert home.load(TICKETCTL_DEFAULT_SITE="other").config.default_site == "other"
    with pytest.raises(ConfigError):
        home.load(TICKETCTL_DEFAULT_SITE="ghost")


def test_nested_and_structured_env_keys_are_ignored_with_warnings(home: Home) -> None:
    home.main()
    loaded = home.load(
        TICKETCTL_SITES__MAIN__URL="https://evil.test/" + SENTINEL,
        TICKETCTL_LIMITS__BODY_CHARS="5",
        TICKETCTL_HOOKS=SENTINEL,
        TICKETCTL_TIMEOUT="30",
    )
    assert loaded.config.limits.body_chars == 30000
    assert str(loaded.config.sites["main"].url) == "https://example.test"
    assert loaded.config.timeout == timedelta(seconds=30)
    joined = "\n".join(loaded.warnings)
    assert "TICKETCTL_SITES__MAIN__URL" in joined
    assert "TICKETCTL_LIMITS__BODY_CHARS" in joined
    assert "TICKETCTL_HOOKS" in joined
    assert SENTINEL not in joined
    assert len(loaded.warnings) == 3


def test_non_config_ticketctl_variables_do_not_warn(home: Home) -> None:
    home.main()
    loaded = home.load(
        TICKETCTL_HOOK="x",
        TICKETCTL_ID="1",
        TICKETCTL_OBJECT_TYPE="ticket",
        TICKETCTL_DEBUG="1",
        TICKETCTL_LIVE="1",
    )
    assert loaded.warnings == []


def test_no_warnings_by_default(home: Home) -> None:
    home.main()
    assert home.load().warnings == []


# --- durations --------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("90", timedelta(seconds=90)),
        ("90s", timedelta(seconds=90)),
        ("1500ms", timedelta(milliseconds=1500)),
        ("2m", timedelta(minutes=2)),
        ("24h", timedelta(hours=24)),
        ("1d", timedelta(days=1)),
        ("1s", timedelta(seconds=1)),
    ],
)
def test_timeout_accepts(home: Home, text: str, expected: timedelta) -> None:
    home.main(MINIMAL + f"timeout: '{text}'\n")
    assert home.load().config.timeout == expected


def test_timeout_accepts_yaml_integer(home: Home) -> None:
    home.main(MINIMAL + "timeout: 45\n")
    assert home.load().config.timeout == timedelta(seconds=45)


@pytest.mark.parametrize(
    "text",
    [
        "'0'",
        "0",
        "'999ms'",
        "'25h'",
        "'2d'",
        "'5x'",
        "'1.5s'",
        "'-1s'",
        "''",
        "'s'",
        "true",
        "1.5",
        "[1]",
        "'1 s'",
        "'１s'",
        "'1S'",
        "'5s\\n'",
        "'0s'",
        "'0ms'",
        "'1234567890s'",
    ],
)
def test_timeout_rejects(home: Home, text: str) -> None:
    expect_error(home, MINIMAL + f"timeout: {text}\n", "timeout")


def test_zero_allowed_for_cache_ttl(home: Home) -> None:
    home.main(site_text(cache_ttl="cache_ttl: 0"))
    assert home.load().config.sites["main"].cache_ttl == timedelta(0)


def test_zero_allowed_for_retry_after_max(home: Home) -> None:
    home.main(MINIMAL + "limits:\n  retry_after_max: '0'\n")
    assert home.load().config.limits.retry_after_max == timedelta(0)


@pytest.mark.parametrize("text", ["0", "'0s'", "'0ms'"])
def test_zero_refused_for_site_timeouts(home: Home, text: str) -> None:
    home.main(site_text(timeouts=f"timeouts: {{connect: {text}}}"))
    with pytest.raises(ConfigError) as exc:
        home.load()
    assert exc.value.key == "sites.main.timeouts.connect"
    home.main(site_text(timeouts=f"timeouts: {{read: {text}}}"))
    with pytest.raises(ConfigError) as exc:
        home.load()
    assert exc.value.key == "sites.main.timeouts.read"


def test_cache_ttl_accepts_units(home: Home) -> None:
    home.main(site_text(cache_ttl="cache_ttl: 1h"))
    assert home.load().config.sites["main"].cache_ttl == timedelta(hours=1)


@pytest.mark.parametrize("text", ["'-1'", "'x'", "1.5", "true"])
def test_cache_ttl_rejects(home: Home, text: str) -> None:
    expect_error(home, site_text(cache_ttl=f"cache_ttl: {text}"), "sites.main.cache_ttl")


# --- hooks ------------------------------------------------------------------------------------

HOOK_NAMES = [
    f"{when}-{kind}"
    for kind in ("transition", "comment", "create", "update", "sprint")
    for when in ("pre", "post")
]


@pytest.mark.parametrize("name", HOOK_NAMES)
def test_all_ten_hook_names_accepted(home: Home, name: str) -> None:
    home.main(MINIMAL + f"hooks:\n  {name}:\n    - cmd: run it\n")
    hook = home.load().config.hooks[name][0]
    assert hook.cmd == "run it"
    assert hook.timeout == timedelta(seconds=30)
    assert hook.async_ is False


def test_unknown_hook_name_refused(home: Home) -> None:
    expect_error(home, MINIMAL + "hooks:\n  pre-read:\n    - cmd: x\n")


def test_post_hook_may_be_async_pre_hook_may_not(home: Home) -> None:
    home.main(MINIMAL + "hooks:\n  post-update:\n    - cmd: x\n      async: true\n")
    assert home.load().config.hooks["post-update"][0].async_ is True
    expect_error(
        home,
        MINIMAL + "hooks:\n  pre-update:\n    - cmd: x\n      async: true\n",
        "hooks.pre-update[0].async",
    )


@pytest.mark.parametrize(
    ("text", "seconds"),
    [("1", 1), ("600", 600), ("'10s'", 10), ("'2m'", 120), ("'10m'", 600), ("'3000ms'", 3)],
)
def test_hook_timeout_accepts(home: Home, text: str, seconds: int) -> None:
    home.main(MINIMAL + f"hooks:\n  pre-create:\n    - cmd: x\n      timeout: {text}\n")
    assert home.load().config.hooks["pre-create"][0].timeout == timedelta(seconds=seconds)


@pytest.mark.parametrize(
    "text",
    ["0", "601", "'601s'", "'11m'", "'1h'", "'1500ms'", "'500ms'", "1.5", "-1", "'x'", "true"],
)
def test_hook_timeout_rejects(home: Home, text: str) -> None:
    expect_error(
        home,
        MINIMAL + f"hooks:\n  pre-create:\n    - cmd: x\n      timeout: {text}\n",
        "hooks.pre-create[0].timeout",
    )


@pytest.mark.parametrize("cmd", ["''", "'" + "x" * 4097 + "'", "5", "[a]", '"a\\x00b"'])
def test_hook_cmd_rejects(home: Home, cmd: str) -> None:
    expect_error(
        home,
        MINIMAL + f"hooks:\n  pre-create:\n    - cmd: {cmd}\n",
        "hooks.pre-create[0].cmd",
    )


def test_hook_requires_cmd_and_refuses_extra_keys(home: Home) -> None:
    expect_error(
        home, MINIMAL + "hooks:\n  pre-create:\n    - timeout: 5\n", "hooks.pre-create[0].cmd"
    )
    expect_error(
        home,
        MINIMAL + "hooks:\n  pre-create:\n    - cmd: x\n      env: {A: b}\n",
        "hooks.pre-create[0].env",
    )


def test_hooks_must_be_lists(home: Home) -> None:
    expect_error(home, MINIMAL + "hooks:\n  pre-create: {cmd: x}\n", "hooks.pre-create")


# --- loader checks: one accepting and one rejecting test each ---------------------------------


def test_default_site_names_a_site_accepts(home: Home) -> None:
    home.main(MINIMAL + "default_site: main\n")
    assert home.load().config.default_site == "main"


def test_default_site_names_a_site_rejects(home: Home) -> None:
    err = expect_error(home, MINIMAL + "default_site: ghost\n", "default_site")
    assert "ghost" not in str(err)


def test_default_site_grammar_rejects(home: Home) -> None:
    expect_error(home, MINIMAL + "default_site: 'bad name'\n", "default_site")


def test_email_xor_email_env_accepts_either(home: Home) -> None:
    home.main(site_text(email="email_env: MAIL_VAR"))
    site = home.load().config.sites["main"]
    assert site.email is None and site.email_env == "MAIL_VAR"
    home.main(site_text())
    assert home.load().config.sites["main"].email == "me@example.test"


def test_email_and_email_env_both_rejected(home: Home) -> None:
    expect_error(home, site_text(email_env="email_env: MAIL_VAR"), "sites.main.email")


def test_email_missing_both_rejected(home: Home) -> None:
    expect_error(home, site_text(email=""), "sites.main.email")


@pytest.mark.parametrize("value", ["'a b@c'", "'nope'", "'@x'", "'a@'", "'a@b@c'", "''"])
def test_email_shape_rejects(home: Home, value: str) -> None:
    expect_error(home, site_text(email=f"email: {value}"), "sites.main.email")


@pytest.mark.parametrize("value", ["'1X'", "'a-b'", "''", "'a b'", "'" + "A" * 129 + "'"])
def test_env_name_shape_rejects(home: Home, value: str) -> None:
    expect_error(home, site_text(token_env=f"token_env: {value}"), "sites.main.token_env")
    expect_error(home, site_text(email=f"email_env: {value}"), "sites.main.email_env")


def test_token_env_required(home: Home) -> None:
    expect_error(home, site_text(token_env=""), "sites.main.token_env")


def test_unset_or_empty_token_env_is_not_an_error(home: Home) -> None:
    home.main(site_text(token_env="token_env: DEFINITELY_UNSET_VAR_NAME"))
    assert home.load().config.sites["main"].token_env == "DEFINITELY_UNSET_VAR_NAME"
    assert home.load(DEFINITELY_UNSET_VAR_NAME="").config.sites["main"].token_env
    assert home.load(MAIL_VAR="").config.sites


def test_empty_email_env_variable_is_not_an_error(home: Home) -> None:
    home.main(site_text(email="email_env: MAIL_VAR"))
    assert home.load(MAIL_VAR="").config.sites["main"].email_env == "MAIL_VAR"


LOGICAL = [
    "estimate",
    "acceptance_criteria",
    "sprint",
    "parent",
    "epic",
    "labels",
    "priority",
    "assignee",
    "resolution",
    "due_date",
]


def test_queries_and_extra_names_accept_non_logical_names(home: Home) -> None:
    home.main(
        site_text(
            queries="queries: {my_open: 'assignee = x'}",
            fields="fields: {extra: {team: {name: Team}}}",
        )
    )
    site = home.load().config.sites["main"]
    assert site.queries == {"my_open": "assignee = x"}
    assert site.fields.extra["team"].name == "Team"


@pytest.mark.parametrize("name", LOGICAL)
def test_queries_name_colliding_with_logical_name_rejected(home: Home, name: str) -> None:
    expect_error(
        home, site_text(queries=f"queries: {{{name}: 'a = b'}}"), f"sites.main.queries.{name}"
    )


@pytest.mark.parametrize("name", LOGICAL)
def test_extra_name_colliding_with_logical_name_rejected(home: Home, name: str) -> None:
    expect_error(
        home,
        site_text(fields=f"fields: {{extra: {{{name}: {{name: X}}}}}}"),
        f"sites.main.fields.extra.{name}",
    )


@pytest.mark.parametrize("name", ["Upper", "1abc", "a-b", "a b", "x" * 65, "é"])
def test_queries_and_extra_name_grammar_rejects(home: Home, name: str) -> None:
    key = re.sub(r"[^A-Za-z0-9_.-]", "?", name)[:64]
    expect_error(
        home, site_text(queries=f"queries: {{'{name}': 'a = b'}}"), f"sites.main.queries.{key}"
    )
    expect_error(
        home,
        site_text(fields=f"fields: {{extra: {{'{name}': {{name: X}}}}}}"),
        f"sites.main.fields.extra.{key}",
    )


def test_query_text_limits(home: Home) -> None:
    home.main(site_text(queries="queries: {q: '" + "a" * 4096 + "'}"))
    assert len(home.load().config.sites["main"].queries["q"]) == 4096
    expect_error(
        home, site_text(queries="queries: {q: '" + "a" * 4097 + "'}"), "sites.main.queries.q"
    )
    expect_error(home, site_text(queries="queries: {q: ''}"), "sites.main.queries.q")
    expect_error(home, site_text(queries="queries: {q: 5}"), "sites.main.queries.q")


def test_site_name_grammar(home: Home) -> None:
    home.main(MINIMAL.replace("main:", "A-b_9:"))
    assert "A-b_9" in home.load().config.sites
    for bad in ("'-x'", "'a b'", "'" + "a" * 65 + "'", "'a.b'"):
        err = expect_error(home, MINIMAL.replace("main:", f"{bad}:"))
        assert err.key is not None and err.key.startswith("sites.")


@pytest.mark.parametrize(
    "url",
    [
        "https://example.test",
        "https://example.test/",
        "https://example.test/jira",
        "https://example.test:8443/a/b",
        "https://[::1]:8443",
        "https://127.0.0.1",
    ],
)
def test_https_url_accepts(home: Home, url: str) -> None:
    home.main(site_text(url=f"url: {url}"))
    home.load()


@pytest.mark.parametrize(
    "url",
    [
        "http://example.test",
        "ftp://example.test",
        "example.test",
        "https://",
        "https:///path",
        "https://user@example.test",
        "https://user:pw@example.test",
        "https://example.test?x=1",
        "https://example.test/?",
        "https://example.test/a?x=1",
        "https://example.test#frag",
        "https://example.test/#",
        "https://example.test/a b",
        "https://exa mple.test",
        "https://example.test:99999",
        "https://example.test:abc",
        "https://example.test:",
        "https://exam%70le.test",
        "https://example.test/%2e%2e",
        "https://example.test/a/../b",
        "https://example.test/a/./b",
        "https://example.test\\\\evil.test",
        "https://example.test/a\\\\b",
        "https://exämple.test",
        "https://example.test@evil.test",
        "javascript:alert(1)",
        "https://[::1",
        "https://-bad.test",
        "https://example.test/" + "a" * 2048,
    ],
)
def test_url_rejects_without_echo(home: Home, url: str) -> None:
    err = expect_error(home, site_text(url=f"url: '{url}'"), "sites.main.url")
    assert url not in str(err)


def test_url_control_characters_rejected(home: Home) -> None:
    for ch in ("\\n", "\\t", "\\x00", "\\x7f", "\\u202e"):
        expect_error(home, site_text(url=f'url: "https://example.test/a{ch}b"'), "sites.main.url")
    expect_error(home, site_text(url='url: "https://example.test\\n"'), "sites.main.url")


def test_url_must_be_a_string(home: Home) -> None:
    expect_error(home, site_text(url="url: 5"), "sites.main.url")
    expect_error(home, site_text(url=""), "sites.main.url")


@pytest.mark.parametrize(
    "url",
    [
        "http://127.0.0.1",
        "http://127.0.0.1:8080/x",
        "http://[::1]:9000",
        "http://localhost",
        "http://LOCALHOST:80",
    ],
)
def test_http_accepted_only_with_allow_http_and_loopback(home: Home, url: str) -> None:
    home.main(site_text(url=f"url: '{url}'", allow_http="allow_http: true"))
    assert str(home.load().config.sites["main"].url).startswith("http://")


@pytest.mark.parametrize(
    "url",
    [
        "http://example.test",
        "http://127.0.0.2",
        "http://localhost.evil.test",
        "http://127.0.0.1.evil.test",
        "http://0.0.0.0",
        "http://[::2]",
        "http://localhost@evil.test",
        "http://10.0.0.1",
    ],
)
def test_http_non_loopback_rejected_even_with_allow_http(home: Home, url: str) -> None:
    expect_error(
        home, site_text(url=f"url: '{url}'", allow_http="allow_http: true"), "sites.main.url"
    )


def test_http_loopback_without_allow_http_rejected(home: Home) -> None:
    expect_error(home, site_text(url="url: http://127.0.0.1"), "sites.main.url")
    expect_error(
        home,
        site_text(url="url: http://127.0.0.1", allow_http="allow_http: false"),
        "sites.main.url",
    )


def test_allow_http_must_be_boolean(home: Home) -> None:
    expect_error(home, site_text(allow_http="allow_http: 'yes'"), "sites.main.allow_http")


@pytest.mark.parametrize("value", ["12", "'Team board'", "1"])
def test_board_accepts(home: Home, value: str) -> None:
    home.main(site_text(board=f"board: {value}"))
    assert home.load().config.sites["main"].board == yaml.safe_load(value)


@pytest.mark.parametrize(
    "value", ["0", "-1", "''", "1.5", "true", "[1]", '"a\\x07b"', "'" + "x" * 257 + "'"]
)
def test_board_rejects(home: Home, value: str) -> None:
    expect_error(home, site_text(board=f"board: {value}"), "sites.main.board")


# --- remaining site keys ----------------------------------------------------------------------


def test_projects_grammar(home: Home) -> None:
    home.main(site_text(projects="projects: [AB, C_1]"))
    assert home.load().config.sites["main"].projects == ["AB", "C_1"]
    for bad in ("[ab]", "[1A]", "[A-B]", "[AB, AB]", "[" + "A" * 65 + "]", "AB", "[5]", "['A\\n']"):
        expect_error(home, site_text(projects=f"projects: {bad}"))


def test_blocking_link_types(home: Home) -> None:
    home.main(site_text(blocking_link_types="blocking_link_types: [Blocks, Is blocked by]"))
    assert home.load().config.sites["main"].blocking_link_types == ["Blocks", "Is blocked by"]
    for bad in ("[]x", "['']", "[Blocks, Blocks]", "[5]", "Blocks"):
        expect_error(home, site_text(blocking_link_types=f"blocking_link_types: {bad}"))


def test_provider_only_jira(home: Home) -> None:
    expect_error(home, site_text(provider="provider: other"), "sites.main.provider")


@pytest.mark.parametrize(
    "mapping",
    ["{name: Story Points}", "{id: customfield_10001}", "{schema: 'a.b:c'}"],
)
def test_field_mapping_exactly_one_selector_accepts(home: Home, mapping: str) -> None:
    home.main(site_text(fields=f"fields: {{estimate: {mapping}}}"))
    assert home.load().config.sites["main"].fields.estimate is not None


@pytest.mark.parametrize(
    "mapping",
    [
        "{}",
        "{name: A, id: b}",
        "{name: A, schema: b}",
        "{id: a, schema: b}",
        "{name: A, id: b, schema: c}",
        "{name: ''}",
        "{id: 'bad id'}",
        "{id: ''}",
        "{name: A, other: 1}",
        "x",
        "[name]",
        "{name: 5}",
        '{name: "a\\nb"}',
    ],
)
def test_field_mapping_rejects(home: Home, mapping: str) -> None:
    expect_error(home, site_text(fields=f"fields: {{estimate: {mapping}}}"))


def test_unknown_logical_field_name_rejected(home: Home) -> None:
    expect_error(
        home, site_text(fields="fields: {nonsense: {name: X}}"), "sites.main.fields.nonsense"
    )


def test_transitions(home: Home) -> None:
    home.main(site_text(transitions="transitions: {default_resolution: Done, assign_on_start: me}"))
    t = home.load().config.sites["main"].transitions
    assert t.default_resolution == "Done" and t.assign_on_start == "me"
    expect_error(
        home,
        site_text(transitions="transitions: {assign_on_start: you}"),
        "sites.main.transitions.assign_on_start",
    )
    expect_error(
        home,
        site_text(transitions="transitions: {default_resolution: ''}"),
        "sites.main.transitions.default_resolution",
    )


# --- top-level strict keys and limits ---------------------------------------------------------


def test_unknown_top_level_and_nested_keys_refused(home: Home) -> None:
    expect_error(home, MINIMAL + "repos: {}\n", "repos")
    expect_error(home, site_text(bogus="bogus: 1"), "sites.main.bogus")
    expect_error(home, MINIMAL + "limits: {nope: 1}\n", "limits.nope")


def test_unknown_key_name_in_path_is_sanitized(home: Home) -> None:
    err = expect_error(home, MINIMAL + "'bad key\\x1b[31m': 1\n")
    assert err.key is not None and "\x1b" not in err.key and " " not in err.key


@pytest.mark.parametrize("value", ["1", "32767", "30000"])
def test_body_chars_accepts(home: Home, value: str) -> None:
    home.main(MINIMAL + f"limits:\n  body_chars: {value}\n")
    assert home.load().config.limits.body_chars == int(value)


@pytest.mark.parametrize("value", ["0", "32768", "-1", "'5'", "1.5", "true", "null"])
def test_body_chars_rejects(home: Home, value: str) -> None:
    expect_error(home, MINIMAL + f"limits:\n  body_chars: {value}\n", "limits.body_chars")


def test_attribution_template(home: Home) -> None:
    home.main(MINIMAL + "attribution:\n  enabled: false\n  template: Hello there\n")
    a = home.load().config.attribution
    assert a.enabled is False and a.template == "Hello there"
    for bad in ("''", "'" + "x" * 301 + "'", '"a\\nb"', '"a\\x00b"', '"a\\x7fb"', "5"):
        expect_error(home, MINIMAL + f"attribution:\n  template: {bad}\n", "attribution.template")
    expect_error(home, MINIMAL + "attribution:\n  enabled: 1\n", "attribution.enabled")


def test_attribution_template_exactly_300_ok(home: Home) -> None:
    home.main(MINIMAL + "attribution:\n  template: '" + "x" * 300 + "'\n")
    assert len(home.load().config.attribution.template) == 300


def test_top_level_must_be_a_mapping(home: Home) -> None:
    for text in ("- a\n- b\n", "just a string\n", "5\n"):
        home.main(text)
        with pytest.raises(ConfigError, match="mapping"):
            home.load()


def test_null_sections_rejected(home: Home) -> None:
    expect_error(home, MINIMAL + "limits:\n", "limits")
    expect_error(home, MINIMAL + "hooks:\n", "hooks")


# --- hostile files and YAML -------------------------------------------------------------------


def test_sentinel_in_malformed_yaml_never_in_error(home: Home) -> None:
    for text in (
        f"sites: [unclosed {SENTINEL}\n  : :\n",
        f"key: {SENTINEL}\n\tbad: tab\n",
        f"a: b: {SENTINEL}\n",
        f"- {SENTINEL}\n  x: [\n",
        f'"{SENTINEL}\n',
        f"!!python/object/apply:os.system [{SENTINEL}]\n",
    ):
        home.main(text)
        with pytest.raises(ConfigError) as exc:
            home.load()
        assert SENTINEL not in str(exc.value)
        assert SENTINEL not in repr(exc.value)
        assert SENTINEL not in "".join(map(str, exc.value.args))
        assert_clean(exc.value)


def test_syntax_error_names_the_file_and_line(home: Home) -> None:
    path = home.main("a: 1\nb: [\n")
    with pytest.raises(ConfigError) as exc:
        home.load()
    assert str(path) in str(exc.value)
    assert re.search(r"line \d+", str(exc.value))


def test_sentinel_in_invalid_values_never_in_error(home: Home) -> None:
    cases = [
        f"timeout: {SENTINEL}\n",
        f"default_site: {SENTINEL}\n",
        f"me:\n  jira: '{SENTINEL} x'\n",
        f"limits:\n  body_chars: {SENTINEL}\n",
        f"attribution:\n  enabled: {SENTINEL}\n",
        f"hooks:\n  pre-create:\n    - cmd: x\n      timeout: {SENTINEL}\n",
        f"hooks:\n  pre-create:\n    - {SENTINEL}\n",
        f"hooks: {SENTINEL}\n",
        f"sites: {SENTINEL}\n",
        f"sites:\n  main: {SENTINEL}\n",
    ] + [
        site_text(**{k: f"{k}: '{SENTINEL} x'"})
        for k in (
            "url",
            "email",
            "token_env",
            "account_id",
            "projects",
            "cache_ttl",
            "provider",
            "allow_http",
            "blocking_link_types",
            "fields",
            "queries",
            "transitions",
            "timeouts",
        )
    ]
    for text in cases:
        home.main(text)
        with pytest.raises(ConfigError) as exc:
            home.load()
        assert SENTINEL not in str(exc.value), text
        assert SENTINEL not in (exc.value.key or ""), text
        assert_clean(exc.value)


def test_duplicate_keys_refused(home: Home) -> None:
    for text in (
        MINIMAL + "timeout: 1s\ntimeout: 2s\n",
        "sites:\n  a: {}\n  a: {}\n",
        MINIMAL + "limits: {body_chars: 1, body_chars: 2}\n",
    ):
        home.main(text)
        with pytest.raises(ConfigError, match="duplicate"):
            home.load()


def test_duplicate_key_error_does_not_echo_the_key(home: Home) -> None:
    home.main(f"{SENTINEL}: 1\n{SENTINEL}: 2\n")
    with pytest.raises(ConfigError) as exc:
        home.load()
    assert SENTINEL not in str(exc.value)


@pytest.mark.parametrize(
    "text",
    [
        "base: &a {x: 1}\ncopy: *a\n",
        "base: &a 5\n",
        "sites: {main: &a {x: 1}}\n",
        "x: !!str 5\n",
        "x: !!python/object/apply:os.getcwd []\n",
        "x: !custom 1\n",
        "x: !!binary aGVsbG8=\n",
        "<<: {a: 1}\n",
        "a: {<<: {b: 1}}\n",
        "? [a, b]\n: 1\n",
        "1: a\n",
        "null: a\n",
        "true: a\n",
        "--- a\n--- b\n",
    ],
)
def test_yaml_features_refused(home: Home, text: str) -> None:
    home.main(text)
    with pytest.raises(ConfigError) as exc:
        home.load()
    assert exc.value.reason == "config_invalid"


def test_yaml_bomb_is_refused_quickly(home: Home) -> None:
    lines = ["a0: &a0 [x, x]"] + [f"a{i}: &a{i} [*a{i - 1}, *a{i - 1}]" for i in range(1, 30)]
    home.main("\n".join(lines) + "\n")
    with pytest.raises(ConfigError):
        home.load()


def test_deeply_nested_yaml_is_refused_not_a_crash(home: Home) -> None:
    home.main("x: " + "[" * 5000 + "]" * 5000 + "\n")
    with pytest.raises(ConfigError):
        home.load()
    home.main("x:\n" + "".join(" " * i + "a:\n" for i in range(1, 200)) + " " * 200 + "b: 1\n")
    with pytest.raises(ConfigError):
        home.load()


def test_oversize_file_refused(home: Home) -> None:
    home.main(MINIMAL + "# " + "x" * config_module.MAX_FILE_BYTES + "\n")
    with pytest.raises(ConfigError, match="too large"):
        home.load()


def test_file_at_limit_is_read(home: Home) -> None:
    pad = "#" * (config_module.MAX_FILE_BYTES - len(MINIMAL) - 1) + "\n"
    home.main(MINIMAL + pad)
    assert (home.cfg / "config.yaml").stat().st_size == config_module.MAX_FILE_BYTES
    home.load()


def test_invalid_utf8_refused(home: Home) -> None:
    home.write("config.yaml", b"timeout: \xff\xfe\n")
    with pytest.raises(ConfigError, match="UTF-8"):
        home.load()


def test_nul_byte_refused(home: Home) -> None:
    home.write("config.yaml", b"timeout: 5s\x00\n")
    with pytest.raises(ConfigError):
        home.load()


def test_world_writable_file_refused(home: Home) -> None:
    home.write("config.yaml", MINIMAL, mode=0o666)
    with pytest.raises(ConfigError, match="writable"):
        home.load()


def test_world_writable_dropin_refused(home: Home) -> None:
    home.main()
    home.write("config.d/a.yaml", "{}\n", mode=0o606)
    with pytest.raises(ConfigError, match="writable"):
        home.load()


def test_world_writable_dropin_directory_refused(home: Home) -> None:
    home.main()
    home.write("config.d/a.yaml", "{}\n")
    (home.cfg / "config.d").chmod(0o777)
    try:
        with pytest.raises(ConfigError, match="writable"):
            home.load()
    finally:
        (home.cfg / "config.d").chmod(0o700)


def test_group_readable_and_group_writable_allowed(home: Home) -> None:
    home.write("config.yaml", MINIMAL, mode=0o664)
    home.load()


def test_file_owned_by_someone_else_refused(home: Home, monkeypatch: pytest.MonkeyPatch) -> None:
    if os.geteuid() == 0:
        pytest.skip("root may read anything")
    home.main()
    uid = os.geteuid()
    monkeypatch.setattr(config_module.os, "geteuid", lambda: uid + 1)
    with pytest.raises(ConfigError, match="owner"):
        home.load()


def test_symlink_to_regular_file_is_followed(home: Home, tmp_path: Path) -> None:
    real = tmp_path / "dotfiles.yaml"
    real.write_text(MINIMAL)
    real.chmod(0o600)
    (home.cfg / "config.yaml").symlink_to(real)
    assert "main" in home.load().config.sites


def test_symlink_to_world_writable_file_refused(home: Home, tmp_path: Path) -> None:
    real = tmp_path / "loose.yaml"
    real.write_text(MINIMAL)
    real.chmod(0o666)
    (home.cfg / "config.yaml").symlink_to(real)
    with pytest.raises(ConfigError, match="writable"):
        home.load()


def test_dangling_symlink_refused(home: Home, tmp_path: Path) -> None:
    (home.cfg / "config.yaml").symlink_to(tmp_path / "gone.yaml")
    with pytest.raises(ConfigError):
        home.load()


def test_symlink_loop_refused(home: Home) -> None:
    (home.cfg / "config.yaml").symlink_to(home.cfg / "config.yaml")
    with pytest.raises(ConfigError):
        home.load()


def test_fifo_is_refused_without_blocking(home: Home) -> None:
    os.mkfifo(home.cfg / "config.yaml")
    with pytest.raises(ConfigError, match="regular file"):
        home.load()


def test_fifo_dropin_is_refused_without_blocking(home: Home) -> None:
    home.main()
    (home.cfg / "config.d").mkdir()
    os.mkfifo(home.cfg / "config.d" / "a.yaml")
    with pytest.raises(ConfigError, match="regular file"):
        home.load()


def test_device_is_refused(home: Home) -> None:
    (home.cfg / "config.yaml").symlink_to("/dev/zero")
    with pytest.raises(ConfigError, match="regular file"):
        home.load()


def test_config_path_is_a_directory(home: Home) -> None:
    (home.cfg / "config.yaml").mkdir()
    with pytest.raises(ConfigError, match="regular file"):
        home.load()


def test_config_d_is_a_file_refused(home: Home) -> None:
    home.main()
    (home.cfg / "config.d").write_text("x")
    with pytest.raises(ConfigError, match="directory"):
        home.load()


def test_unreadable_file_refused(home: Home) -> None:
    if os.geteuid() == 0:
        pytest.skip("root reads anything")
    home.write("config.yaml", MINIMAL, mode=0o000)
    with pytest.raises(ConfigError):
        home.load()


def test_error_is_not_chained_to_yaml_internals(home: Home) -> None:
    home.main(f"a: [{SENTINEL}\n")
    with pytest.raises(ConfigError) as exc:
        home.load()
    assert exc.value.__cause__ is None
    assert exc.value.__context__ is None


def test_validation_error_is_not_chained(home: Home) -> None:
    home.main(MINIMAL + f"timeout: {SENTINEL}\n")
    with pytest.raises(ConfigError) as exc:
        home.load()
    assert exc.value.__cause__ is None
    assert exc.value.__context__ is None


def test_error_carries_constraint_and_key_in_message(home: Home) -> None:
    err = expect_error(home, MINIMAL + "limits:\n  body_chars: 0\n", "limits.body_chars")
    assert str(err).startswith("limits.body_chars: ")
    assert err.constraint
    assert err.reason == "config_invalid"


def test_unrelated_environment_names_do_not_change_the_result(home: Home) -> None:
    home.main()
    base = home.load().config
    noisy = home.load(
        UNRELATED=SENTINEL, TICKETCTL_TOKEN=SENTINEL, TICKETCTL_EMAIL=SENTINEL, TEST_TOKEN=SENTINEL
    )
    assert noisy.config == base
    assert SENTINEL not in repr(noisy.config)


def test_loads_are_equal_but_independent_objects(home: Home) -> None:
    home.main()
    a = home.load().config
    b = home.load().config
    assert a == b
    assert a is not b
    assert a.sites is not b.sites
    a.sites["extra"] = a.sites["main"]
    assert "extra" not in b.sites
    assert "extra" not in home.load().config.sites


# --- review round 1 -----------------------------------------------------------------------------

HUGE = "9" * 5000


@pytest.mark.parametrize(
    "text",
    [
        MINIMAL + f"timeout: {HUGE}\n",
        MINIMAL + f"limits:\n  retry_after_max: {HUGE}\n",
        site_text(board=f"board: {HUGE}"),
        MINIMAL + f"me:\n  name: x\nattribution:\n  enabled: true\nhooks:\n  pre-create:\n"
        f"    - cmd: x\n      timeout: {HUGE}\n",
    ],
)
def test_huge_yaml_integer_is_a_config_error(home: Home, text: str) -> None:
    home.main(text)
    with pytest.raises(ConfigError) as exc:
        home.load()
    assert exc.value.code == "invalid_config"
    assert HUGE not in str(exc.value)
    assert exc.value.__context__ is None and exc.value.__cause__ is None


@pytest.mark.parametrize("path", ["//", "///", "/a//", "/a//b", "/a/./b", "/a/../b"])
def test_url_empty_segments_rejected(home: Home, path: str) -> None:
    expect_error(home, site_text(url=f"url: https://example.test{path}"), "sites.main.url")


@pytest.mark.parametrize(
    ("path", "expected"), [("", ""), ("/", ""), ("/a/", "/a"), ("/a/b", "/a/b")]
)
def test_url_single_trailing_slash_accepted(home: Home, path: str, expected: str) -> None:
    home.main(site_text(url=f"url: https://example.test{path}"))
    assert str(home.load().config.sites["main"].url) == "https://example.test" + expected


CAPS = [
    ("timeout", "24h", "25h", lambda t: MINIMAL + f"timeout: {t}\n"),
    ("connect", "24h", "1441m", lambda t: site_text(timeouts=f"timeouts: {{connect: {t}}}")),
    ("read", "24h", "86401s", lambda t: site_text(timeouts=f"timeouts: {{read: {t}}}")),
    ("cache_ttl", "365d", "366d", lambda t: site_text(cache_ttl=f"cache_ttl: {t}")),
    ("cache_ttl", "8760h", "8761h", lambda t: site_text(cache_ttl=f"cache_ttl: {t}")),
    (
        "retry_after_max",
        "365d",
        "366d",
        lambda t: MINIMAL + f"limits:\n  retry_after_max: {t}\n",
    ),
    (
        "retry_after_max",
        "31536000",
        "31536001",
        lambda t: MINIMAL + f"limits:\n  retry_after_max: {t}\n",
    ),
]


@pytest.mark.parametrize(("name", "ok", "bad", "build"), CAPS)
def test_duration_caps(
    home: Home, name: str, ok: str, bad: str, build: Callable[[str], str]
) -> None:
    home.main(build(ok))
    home.load()
    err = expect_error(home, build(bad))
    assert "at most" in err.constraint, name


def test_cache_ttl_overflow_value_is_refused(home: Home) -> None:
    expect_error(home, site_text(cache_ttl="cache_ttl: 999999999d"), "sites.main.cache_ttl")


def _schema() -> dict:  # type: ignore[type-arg]
    import json

    return json.loads((ROOT / "schemas" / "config.json").read_text(encoding="utf-8"))


def _branch_types(node: dict) -> set[str]:  # type: ignore[type-arg]
    # A $ref branch counts as the type of its string-only target.
    return {b.get("type", "string") for b in node["oneOf"]}


def test_schema_durations_accept_bare_integers() -> None:
    schema = _schema()
    defs = schema["$defs"]
    assert _branch_types(defs["duration"]) == {"integer", "string"}
    assert _branch_types(defs["duration_or_zero"]) == {"integer", "string"}
    zero = next(b for b in defs["duration_or_zero"]["oneOf"] if b["type"] == "integer")
    assert zero["minimum"] == 0
    pos = next(b for b in defs["duration"]["oneOf"] if b["type"] == "integer")
    assert pos["minimum"] == 1
    site = defs["site"]["properties"]
    assert site["cache_ttl"]["$ref"].endswith("duration_or_zero")
    assert schema["properties"]["limits"]["properties"]["retry_after_max"]["$ref"].endswith(
        "duration_or_zero"
    )
    assert site["timeouts"]["properties"]["connect"]["$ref"].endswith("/duration")


def test_schema_timeout_branches_do_not_overlap() -> None:
    # An integer must match exactly one oneOf branch, so the string branch refs a string-only def.
    schema = _schema()
    branches = schema["properties"]["timeout"]["oneOf"]
    assert [b.get("type") for b in branches] == ["integer", None]
    assert branches[1]["$ref"].endswith("duration_string")
    assert _schema()["$defs"]["duration_string"]["type"] == "string"


def test_schema_sites_may_be_empty_or_absent() -> None:
    schema = _schema()
    assert "sites" not in schema.get("required", [])
    assert "minProperties" not in schema["properties"]["sites"]


def test_schema_queries_exclude_logical_names_like_fields_extra() -> None:
    site = _schema()["$defs"]["site"]["properties"]
    extra = site["fields"]["properties"]["extra"]["propertyNames"]["not"]["enum"]
    queries = site["queries"]["propertyNames"]["not"]["enum"]
    assert queries == extra == list(config_module.LOGICAL_FIELDS)


def test_schema_documents_caps_and_normalized_url() -> None:
    schema = _schema()
    site = schema["$defs"]["site"]["properties"]
    assert "365d" in site["cache_ttl"]["description"]
    assert "365d" in schema["properties"]["limits"]["properties"]["retry_after_max"]["description"]
    assert "24h" in site["timeouts"]["description"]
    assert "lower-case" in site["url"]["description"]


def test_file_default_site_is_validated_before_env_override(home: Home) -> None:
    home.main(MINIMAL + "default_site: ghost\n")
    with pytest.raises(ConfigError) as exc:
        home.load(TICKETCTL_DEFAULT_SITE="main")
    assert exc.value.key == "default_site"


def test_env_default_site_with_no_sites_is_not_config_invalid(home: Home) -> None:
    home.main("")
    loaded = home.load(TICKETCTL_DEFAULT_SITE="main")
    assert loaded.config.sites == {}
    with pytest.raises(ConfigError) as exc:
        loaded.config.require_sites()
    assert exc.value.reason == "no_sites"


def test_env_default_site_naming_no_configured_site_still_refused(home: Home) -> None:
    home.main()
    with pytest.raises(ConfigError) as exc:
        home.load(TICKETCTL_DEFAULT_SITE="ghost")
    assert exc.value.key == "TICKETCTL_DEFAULT_SITE"


@pytest.mark.parametrize(
    "text",
    [
        f"a: [{SENTINEL}\n",
        f"a: b: {SENTINEL}\n",
        f"!!python/object/apply:os.system [{SENTINEL}]\n",
        f"&a {SENTINEL}: 1\n",
        MINIMAL + f"timeout: {SENTINEL}\n",
        MINIMAL + f"limits:\n  body_chars: {SENTINEL}\n",
        f"{SENTINEL}: 1\n{SENTINEL}: 2\n",
    ],
)
def test_errors_have_no_context_and_leak_nothing(home: Home, text: str) -> None:
    home.main(text)
    with pytest.raises(ConfigError) as exc:
        home.load()
    assert_clean(exc.value)


def test_env_timeout_error_is_clean(home: Home) -> None:
    home.main()
    with pytest.raises(ConfigError) as exc:
        home.load(TICKETCTL_TIMEOUT=SENTINEL)
    assert_clean(exc.value)


def test_read_file_uses_o_noctty(home: Home, monkeypatch: pytest.MonkeyPatch) -> None:
    home.main()
    flags: list[int] = []
    real_open = os.open

    def spy(path: object, flag: int, *args: int) -> int:
        flags.append(flag)
        return real_open(path, flag, *args)  # type: ignore[arg-type]

    monkeypatch.setattr(config_module.os, "open", spy)
    home.load()
    assert flags and all(f & os.O_NOCTTY for f in flags)


def test_dropin_listing_stops_after_the_cap(home: Home, monkeypatch: pytest.MonkeyPatch) -> None:
    home.main()
    for i in range(config_module.MAX_DROPINS + 40):
        home.write(f"config.d/{i:04}.yaml", "{}\n")
    seen: list[str] = []
    real_scandir = os.scandir

    class Spy:
        def __init__(self, path: object) -> None:
            self.inner = real_scandir(path)  # type: ignore[call-overload]

        def __enter__(self) -> Spy:
            return self

        def __exit__(self, *args: object) -> None:
            self.inner.close()

        def __iter__(self) -> Spy:
            return self

        def __next__(self) -> os.DirEntry[str]:
            entry = next(self.inner)
            seen.append(entry.name)
            return entry

    monkeypatch.setattr(config_module.os, "scandir", Spy)
    with pytest.raises(ConfigError, match="too many drop-in files"):
        home.load()
    assert len(seen) == config_module.MAX_DROPINS + 1


def test_dropin_directory_owned_by_someone_else_refused(
    home: Home, monkeypatch: pytest.MonkeyPatch
) -> None:
    if os.geteuid() == 0:
        pytest.skip("root may read anything")
    home.main()
    home.write("config.d/a.yaml", "{}\n")
    real_stat = os.stat
    uid = os.geteuid()

    class Fake:
        def __init__(self, info: os.stat_result) -> None:
            self.st_mode = info.st_mode
            self.st_uid = uid + 1

    def fake_stat(path: object, *args: object, **kw: object) -> object:
        info = real_stat(path, *args, **kw)  # type: ignore[arg-type]
        return Fake(info) if str(path).endswith("config.d") else info

    monkeypatch.setattr(config_module.os, "stat", fake_stat)
    with pytest.raises(ConfigError, match="owner"):
        home.load()


@pytest.mark.parametrize("value", ["010", "0x10", "0b11", "1_0", "1:30", "+5", "0o7", "1e3", "1.5"])
def test_yaml_one_one_numbers_are_not_durations(home: Home, value: str) -> None:
    expect_error(home, MINIMAL + f"timeout: {value}\n", "timeout")
    expect_error(
        home,
        MINIMAL + f"hooks:\n  pre-create:\n    - cmd: x\n      timeout: {value}\n",
        "hooks.pre-create[0].timeout",
    )


def test_plain_integers_still_work_for_durations(home: Home) -> None:
    home.main(MINIMAL + "timeout: 30\nhooks:\n  pre-create:\n    - cmd: x\n      timeout: 5\n")
    cfg = home.load().config
    assert cfg.timeout == timedelta(seconds=30)
    assert cfg.hooks["pre-create"][0].timeout == timedelta(seconds=5)


def test_negative_integer_is_a_range_error_not_a_string(home: Home) -> None:
    expect_error(home, MINIMAL + "timeout: -5\n", "timeout")


def test_loose_numbers_do_not_leak_into_the_global_loader() -> None:
    assert yaml.safe_load("a: 010") == {"a": 8}


NULL_KEYS = [
    site_text(email_env="email_env:") + "",
    site_text(account_id="account_id: null"),
    site_text(board="board: null"),
    site_text(fields="fields: {estimate: null}"),
    site_text(transitions="transitions: {assign_on_start: null}"),
    site_text(transitions="transitions: {default_resolution: null}"),
    site_text(cache_ttl="cache_ttl: null"),
    site_text(projects="projects: null"),
    MINIMAL + "default_site: null\n",
    MINIMAL + "me:\n  name: null\n",
    MINIMAL + "me:\n  jira: null\n",
    MINIMAL + "timeout: null\n",
    MINIMAL + "hooks:\n",
]


@pytest.mark.parametrize("text", NULL_KEYS)
def test_explicit_null_is_rejected(home: Home, text: str) -> None:
    err = expect_error(home, text)
    assert err.key
    assert "null" in err.constraint


def test_null_email_with_email_env_is_rejected(home: Home) -> None:
    text = site_text(email="email: null", email_env="email_env: MAIL_VAR")
    expect_error(home, text, "sites.main.email")


def test_null_in_a_dropin_cannot_unset_a_value(home: Home) -> None:
    home.main(MINIMAL + "default_site: main\n")
    home.write("config.d/a.yaml", "default_site: null\n")
    with pytest.raises(ConfigError) as exc:
        home.load()
    assert exc.value.key == "default_site"


def test_omitted_optional_keys_stay_fine(home: Home) -> None:
    home.main()
    assert home.load().config.sites["main"].board is None


@pytest.mark.parametrize("value", ["1000000000000000000", "0", "-3", "10000000000000000000"])
def test_board_id_out_of_range_rejected(home: Home, value: str) -> None:
    expect_error(home, site_text(board=f"board: {value}"), "sites.main.board")


def test_board_id_bounds_accepted(home: Home) -> None:
    home.main(site_text(board="board: 999999999999999999"))
    assert home.load().config.sites["main"].board == 999999999999999999
    home.main(site_text(board="board: 1"))
    assert home.load().config.sites["main"].board == 1


@pytest.mark.parametrize("value", ["' '", "'   '", '"\\t"', "''"])
def test_board_name_blank_rejected(home: Home, value: str) -> None:
    expect_error(home, site_text(board=f"board: {value}"), "sites.main.board")


def test_board_name_accepted(home: Home) -> None:
    home.main(site_text(board="board: Team Board"))
    assert home.load().config.sites["main"].board == "Team Board"
