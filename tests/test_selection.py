"""Site selection: every row of the table of docs/interface.md ("Site selection")."""

from __future__ import annotations

from pathlib import Path

import pytest

from ticketctl.config import Config, ConfigError, load_config
from ticketctl.errors import TicketctlError, from_exception
from ticketctl.sanitize import InputError
from ticketctl.selection import select_site

ONE = """\
sites:
  main:
    url: https://example.test
    email: me@example.test
    token_env: TEST_TOKEN
    projects: [AB]
"""


def site(name: str, url: str, projects: str = "", extra: str = "") -> str:
    lines = [f"  {name}:", f"    url: {url}", "    email: me@example.test", "    token_env: T"]
    if projects:
        lines.append(f"    projects: [{projects}]")
    if extra:
        lines.append(f"    {extra}")
    return "\n".join(lines) + "\n"


def make(tmp_path: Path, text: str, default: str | None = None) -> Config:
    path = tmp_path / "config.yaml"
    body = text if text.startswith("sites:") else "sites:\n" + text
    if default:
        body += f"default_site: {default}\n"
    path.write_text(body, encoding="utf-8")
    path.chmod(0o600)
    return load_config(path, env={"HOME": str(tmp_path)}).config


@pytest.fixture
def two(tmp_path: Path) -> Config:
    """Two sites with distinct hosts and projects (no default)."""
    return make(
        tmp_path,
        site("alpha", "https://example.test", "AB, ABC")
        + site("beta", "https://other.test", "DEMO"),
    )


def code(exc: pytest.ExceptionInfo[BaseException]) -> tuple[int, str, str | None]:
    err = from_exception(exc.value)
    assert err is not None
    return err.exit, err.code, err.reason


def pick(config: Config, **kw: object) -> tuple[str, str]:
    chosen = select_site(config, **kw)  # type: ignore[arg-type]
    return chosen.site, chosen.rule


# --- row 1: --site ---------------------------------------------------------------------------


def test_site_flag_selects_and_beats_everything(two: Config) -> None:
    assert pick(two, site_flag="beta", refs=["AB-1"]) == ("beta", "site_flag")
    assert pick(two, site_flag="beta") == ("beta", "site_flag")


@pytest.mark.parametrize("name", ["nope", "bad name", "a/b", "é", "", "x" * 64])
def test_unknown_site_flag_is_exit_22_unknown_site(two: Config, name: str) -> None:
    with pytest.raises(Exception) as exc:
        select_site(two, site_flag=name)
    assert code(exc) == (22, "invalid_request", "unknown_site")


def test_site_flag_longer_than_256_is_invalid_value(two: Config) -> None:
    with pytest.raises(Exception) as exc:
        select_site(two, site_flag="a" * 300)
    assert code(exc) == (22, "invalid_request", "invalid_value")


def test_site_flag_is_case_sensitive(two: Config) -> None:
    with pytest.raises(Exception) as exc:
        select_site(two, site_flag="ALPHA")
    assert code(exc) == (22, "invalid_request", "unknown_site")


# --- row 2: a browse URL ---------------------------------------------------------------------


@pytest.mark.parametrize(
    "url",
    [
        "https://example.test/browse/AB-7",
        "https://example.test/browse/AB-7/",
        "https://example.test/browse/AB-7?focusedCommentId=10001",
        "https://example.test/browse/AB-7#comment-1",
        "https://example.test/browse/AB-7?focusedCommentId=1&page=com.x#c",
        "HTTPS://EXAMPLE.TEST/browse/AB-7",
        "https://example.test:443/browse/AB-7",
    ],
)
def test_browse_url_selects_by_host(two: Config, url: str) -> None:
    chosen = select_site(two, refs=[url])
    assert (chosen.site, chosen.rule, chosen.key) == ("alpha", "url", "AB-7")


def test_url_beats_the_project_rule(two: Config) -> None:
    # DEMO belongs to beta, but the host is alpha's: the URL wins (row 2 before row 3).
    assert pick(two, refs=["https://example.test/browse/DEMO-1"]) == ("alpha", "url")


@pytest.mark.parametrize(
    ("url", "reason"),
    [
        ("https://nowhere.test/browse/AB-1", "unknown_site"),
        ("https://example.test.other.test/browse/AB-1", "unknown_site"),
        ("https://example.test:8443/browse/AB-1", "unknown_site"),
        ("https://x.test/browse/AB-1?u=https://example.test/browse/AB-1", "unknown_site"),
        ("http://example.test/browse/AB-1", "invalid_value"),
        ("ftp://example.test/browse/AB-1", "invalid_value"),
        ("https://user@example.test/browse/AB-1", "invalid_value"),
        ("https://user:pw@example.test/browse/AB-1", "invalid_value"),
        ("https://example.test@other.test/browse/AB-1", "invalid_value"),
        ("https://example.test/browse/AB-1/extra", "invalid_value"),
        ("https://example.test/browse/ab-1", "invalid_value"),
        ("https://example.test/projects/AB", "invalid_value"),
        ("https://example.test/jira/software/c/projects/AB/boards/1", "invalid_value"),
        ("https://example.test/", "invalid_value"),
        ("https://example.test", "invalid_value"),
        ("https://example.test/browse/AB-1\n", "invalid_value"),
        ("https://example.test/browse/AB-1\t", "invalid_value"),
        ("https://example.test/browse/AB-1\x00", "invalid_value"),
        ("https://example.test/browse/AB-1 ", "invalid_value"),
        ("https://example.test:99999/browse/AB-1", "invalid_value"),
        ("https://[::1/browse/AB-1", "invalid_value"),
        ("https:///browse/AB-1", "invalid_value"),
        ("https://example.test/browse/" + "A" * 5000 + "-1", "invalid_value"),
    ],
)
def test_bad_browse_url_is_refused(two: Config, url: str, reason: str) -> None:
    with pytest.raises(Exception) as exc:
        select_site(two, refs=[url])
    assert code(exc) == (22, "invalid_request", reason)


def test_shared_host_is_ambiguous_and_lists_the_candidates(tmp_path: Path) -> None:
    cfg = make(
        tmp_path,
        site("zed", "https://example.test") + site("alpha", "https://example.test/Jira"),
    )
    with pytest.raises(TicketctlError) as exc:
        select_site(cfg, refs=["https://example.test/browse/AB-1"])
    assert (exc.value.exit, exc.value.code) == (21, "ambiguous_site")
    assert exc.value.details == {"candidates": ["alpha", "zed"]}


def test_site_flag_resolves_a_shared_host(tmp_path: Path) -> None:
    cfg = make(
        tmp_path,
        site("zed", "https://example.test") + site("alpha", "https://example.test/Jira"),
    )
    assert pick(cfg, site_flag="zed", refs=["https://example.test/browse/AB-1"]) == (
        "zed",
        "site_flag",
    )


def test_url_and_site_flag_disagree_is_invalid_value(two: Config) -> None:
    with pytest.raises(Exception) as exc:
        select_site(two, site_flag="beta", refs=["https://example.test/browse/AB-1"])
    assert code(exc) == (22, "invalid_request", "invalid_value")


def test_http_url_matches_an_allow_http_loopback_site(tmp_path: Path) -> None:
    cfg = make(
        tmp_path,
        site("local", "http://127.0.0.1:8080", extra="allow_http: true")
        + site("alpha", "https://example.test"),
    )
    assert pick(cfg, refs=["http://127.0.0.1:8080/browse/AB-1"]) == ("local", "url")
    with pytest.raises(Exception) as exc:  # the https URL of the same host:port is not that site
        select_site(cfg, refs=["https://127.0.0.1:8080/browse/AB-1"])
    assert code(exc) == (22, "invalid_request", "invalid_value")


# --- row 3: key and project ------------------------------------------------------------------


def test_key_selects_the_site_that_owns_its_project(two: Config) -> None:
    assert pick(two, refs=["DEMO-5"]) == ("beta", "project")
    assert pick(two, refs=["abc-5"]) == ("alpha", "project")  # input is case-insensitive


def test_project_values_select_like_keys(two: Config) -> None:
    assert pick(two, projects=["DEMO"]) == ("beta", "project")


def test_project_claimed_by_two_sites_is_ambiguous(tmp_path: Path) -> None:
    cfg = make(
        tmp_path,
        site("one", "https://example.test", "AB") + site("two", "https://other.test", "AB"),
    )
    with pytest.raises(TicketctlError) as exc:
        select_site(cfg, refs=["AB-1"])
    assert exc.value.exit == 21 and exc.value.details == {"candidates": ["one", "two"]}
    assert pick(cfg, site_flag="two", refs=["AB-1"]) == ("two", "site_flag")


def test_unclaimed_project_falls_through_to_default_site(tmp_path: Path) -> None:
    cfg = make(
        tmp_path,
        site("one", "https://example.test", "AB") + site("two", "https://other.test"),
        default="two",
    )
    assert pick(cfg, refs=["TEST-1"]) == ("two", "default_site")


def test_unclaimed_project_falls_through_to_the_only_site(tmp_path: Path) -> None:
    cfg = make(tmp_path, ONE)
    assert pick(cfg, refs=["TEST-1"]) == ("main", "only_site")


def test_unclaimed_project_with_several_sites_and_no_default_is_ambiguous(two: Config) -> None:
    with pytest.raises(TicketctlError) as exc:
        select_site(two, refs=["TEST-1"])
    assert exc.value.code == "ambiguous_site"
    assert exc.value.details == {"candidates": ["alpha", "beta"]}


@pytest.mark.parametrize(
    ("text", "reason"),
    [
        ("AB", "invalid_key"),
        ("AB-", "invalid_key"),
        ("1B-1", "invalid_key"),
        ("AB-1\n", "invalid_key"),
        ("AB-１", "invalid_key"),
        ("ſb-1", "invalid_key"),  # the long s upper-cases to ASCII S: refused first
        ("../AB-1", "invalid_key"),
        ("A" * 300 + "-1", "invalid_value"),  # the length check comes first
    ],
)
def test_bad_key_is_refused(two: Config, text: str, reason: str) -> None:
    with pytest.raises(Exception) as exc:
        select_site(two, refs=[text])
    assert code(exc) == (22, "invalid_request", reason)


@pytest.mark.parametrize("text", ["ab", "A-B", "", "AB 1"])
def test_bad_project_is_refused(two: Config, text: str) -> None:
    with pytest.raises(InputError) as exc:
        select_site(two, projects=[text])
    assert exc.value.exit_code == 22


def test_keys_of_different_sites_are_mixed_sites(two: Config) -> None:
    with pytest.raises(Exception) as exc:
        select_site(two, refs=["AB-1", "DEMO-1"])
    assert code(exc) == (22, "invalid_request", "mixed_sites")
    with pytest.raises(Exception) as exc:
        select_site(two, refs=["AB-1"], projects=["DEMO"])
    assert code(exc) == (22, "invalid_request", "mixed_sites")


def test_keys_of_one_site_agree_and_site_flag_forces_one(two: Config) -> None:
    assert pick(two, refs=["AB-1", "ABC-2"]) == ("alpha", "project")
    assert pick(two, site_flag="beta", refs=["AB-1", "DEMO-1"]) == ("beta", "site_flag")


def test_a_url_and_a_key_of_another_site_are_mixed(two: Config) -> None:
    with pytest.raises(Exception) as exc:
        select_site(two, refs=["https://example.test/browse/AB-1", "DEMO-1"])
    assert code(exc) == (22, "invalid_request", "mixed_sites")


# --- rows 4 to 6 -----------------------------------------------------------------------------


def test_default_site_selects(tmp_path: Path) -> None:
    cfg = make(
        tmp_path, site("a", "https://example.test") + site("b", "https://other.test"), default="b"
    )
    assert pick(cfg) == ("b", "default_site")


def test_the_only_site_selects(tmp_path: Path) -> None:
    assert pick(make(tmp_path, ONE)) == ("main", "only_site")


def test_the_default_site_beats_the_only_site_rule(tmp_path: Path) -> None:
    assert pick(make(tmp_path, ONE, default="main")) == ("main", "default_site")


def test_nothing_selecting_with_several_sites_lists_the_names(two: Config) -> None:
    with pytest.raises(TicketctlError) as exc:
        select_site(two)
    assert (exc.value.exit, exc.value.code) == (21, "ambiguous_site")
    assert exc.value.details == {"candidates": ["alpha", "beta"]}


def test_no_sites_is_exit_2_invalid_config(tmp_path: Path) -> None:
    cfg = make(tmp_path, "sites: {}\n")
    with pytest.raises(ConfigError) as exc:
        select_site(cfg)
    assert exc.value.reason == "no_sites"
    # --site does not hide it
    with pytest.raises(ConfigError):
        select_site(cfg, site_flag="main")


def test_the_selection_makes_no_network_or_credential_access(
    two: Config, monkeypatch: pytest.MonkeyPatch
) -> None:
    import socket

    def refuse(*a: object, **k: object) -> None:
        raise AssertionError("selection must be offline")

    monkeypatch.setattr(socket, "socket", refuse)
    monkeypatch.setenv("TEST_TOKEN", "never-read-0123456789")
    assert pick(two, refs=["AB-1"]) == ("alpha", "project")
