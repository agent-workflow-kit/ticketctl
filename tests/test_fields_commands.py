"""fields list, fields check, fields refresh and validate --probe through the CLI (issue #19).

Uses the fake provider (loopback http, ``allow_http``), a private config and a private state
directory. Provider field ids are the synthetic ``customfield_90001..90006`` ones of the fake.
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from tests.fake_provider import (
    F_CRITERIA,
    F_ESTIMATE,
    F_POINTS,
    F_SPRINT,
    ME_ACCOUNT,
    SITE_EMAIL,
    SITE_TOKEN,
    FakeProvider,
    Fault,
    Quirks,
)
from tests.golden import assert_golden
from ticketctl import probe as probe_module
from ticketctl import runtime, sanitize
from ticketctl.cli import main
from ticketctl.flags import Globals
from ticketctl.gateway import Gateway

TOKEN_VAR = "TICKETCTL_TEST_TOKEN"
IDS = re.compile(r"customfield_[0-9]+")


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
        self.state = root / "state" / "ticketctl"
        monkeypatch.setenv("XDG_STATE_HOME", str(root / "state"))
        monkeypatch.setenv(TOKEN_VAR, SITE_TOKEN)

    def site(
        self, fake: FakeProvider, name: str = "main", extra: str = "", token_var: str = TOKEN_VAR
    ) -> str:
        lines = [
            f"url: {fake.url}",
            f"email: {SITE_EMAIL}",
            f"token_env: {token_var}",
            "allow_http: true",
            *([extra] if extra else []),
        ]
        return f"  {name}:\n" + "".join(f"    {line}\n" for line in lines)

    def write(self, text: str) -> None:
        self.config.write_text(text, encoding="utf-8")
        self.config.chmod(0o600)

    def use(self, fake: FakeProvider, extra: str = "") -> None:
        self.write("sites:\n" + self.site(fake, extra=extra))

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


def answer_parse(mp: pytest.MonkeyPatch, data: Any, status: int = 200) -> list[str]:
    """Replace the answer of every parse request; returns the queries sent, in order."""
    sent: list[str] = []

    class Resp:
        def __init__(self) -> None:
            self.status = status
            self.data = data

    real = Gateway.request

    def request(self: Any, method: str, path: str, **kw: Any) -> Any:
        if path.endswith("/jql/parse"):
            sent.extend(kw["body"]["queries"])
            return Resp()
        return real(self, method, path, **kw)

    mp.setattr(Gateway, "request", request)
    return sent


def one_query(env: Env, fake: FakeProvider) -> None:
    env.use(fake, "queries: {open: 'project = DEMO'}")


def paths(fake: FakeProvider) -> list[str]:
    return [c.path for c in fake.calls]


def err_of(out: str) -> dict[str, Any]:
    doc = json.loads(out)
    assert list(doc) == ["error"]
    return doc["error"]


def only(doc: dict[str, Any], logical: str) -> dict[str, Any]:
    found = [p for p in doc["problems"] if p.get("logical") == logical]
    assert len(found) == 1, doc
    return found[0]


# --- fields list ------------------------------------------------------------------------------


def test_fields_list_prints_ids_schemas_and_the_mapping(env: Env, fake: FakeProvider) -> None:
    env.use(fake, f"fields: {{estimate: {{id: {F_ESTIMATE}}}}}")
    code, out, err = env.run("--json", "fields", "list")
    assert code == 0, err
    doc = json.loads(out)
    assert_golden("cli", "fields-list", "mapped", doc)
    assert doc["site"] == "main"
    by_id = {f["id"]: f for f in doc["fields"]}
    assert by_id[F_ESTIMATE]["logical"] == "estimate"
    assert by_id[F_SPRINT]["logical"] == "sprint"
    assert by_id[F_POINTS]["logical"] is None
    assert by_id["labels"]["logical"] == "labels" and by_id["labels"]["custom"] is False
    assert doc["mapping"]["estimate"] == {"id": F_ESTIMATE, "name": by_id[F_ESTIMATE]["name"]}
    assert doc["mapping"]["acceptance_criteria"] is None
    assert set(doc["mapping"]) == {
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
    }
    assert [f["id"] for f in doc["fields"]] == sorted(by_id)  # a stable order


def test_fields_list_an_ambiguous_mapping_is_null_and_not_an_error(
    env: Env, fake: FakeProvider
) -> None:
    env.use(fake)  # estimate: two discovered candidates
    code, out, _ = env.run("--json", "fields", "list")
    doc = json.loads(out)
    assert code == 0 and doc["mapping"]["estimate"] is None
    assert all(f["logical"] != "estimate" for f in doc["fields"])
    assert_golden("cli", "fields-list", "ambiguous", doc)


def test_fields_list_is_served_from_the_cache_then_not_refetched(
    env: Env, fake: FakeProvider
) -> None:
    env.use(fake)
    env.run("--json", "fields", "list")
    env.run("--json", "fields", "list")
    assert paths(fake).count("/rest/api/3/field") == 1
    assert "cache/main/fields.json" in env.files()


def test_fields_list_human_output(env: Env, fake: FakeProvider) -> None:
    env.use(fake)
    code, out, _ = env.run("fields", "list")
    assert code == 0 and F_SPRINT in out and "sprint" in out


def test_fields_list_sanitizes_hostile_names_and_schemas(env: Env, fake: FakeProvider) -> None:
    fake.extra_fields = [
        {
            "id": "cf_hostile",
            "name": f"Evil\x1b[31m {SITE_TOKEN}‮",
            "custom": True,
            "schema": {"type": "string", "custom": "x:text\x00"},
        },
        {"id": "bad id!", "name": "ignored", "custom": True},
    ]
    env.use(fake)
    code, out, err = env.run("--json", "fields", "list")
    assert code == 0
    assert "\x1b" not in out + err and "‮" not in out and SITE_TOKEN not in out + err
    assert "ignored" not in out
    assert "1 entries of the provider's field list were ignored" in err


def test_fields_list_unusable_provider_answer_is_provider_changed(
    env: Env, fake: FakeProvider
) -> None:
    fake.field_list_body = {"not": "a list"}
    env.use(fake)
    code, out, _ = env.run("--json", "fields", "list")
    assert (code, err_of(out)["code"]) == (12, "provider_changed")


# --- fields check -----------------------------------------------------------------------------


def test_fields_check_reports_an_ambiguity_by_display_names_and_exits_0(
    env: Env, fake: FakeProvider
) -> None:
    env.use(fake)
    code, out, err = env.run("--json", "fields", "check")
    assert code == 0, err
    doc = json.loads(out)
    assert_golden("cli", "fields-check", "ambiguous", doc)
    problem = only(doc, "estimate")
    assert problem["problem"] == "ambiguous"
    assert len(problem["candidates"]) == 2
    assert not IDS.search(out)


def test_fields_check_sound_mappings_have_no_problems(env: Env, fake: FakeProvider) -> None:
    env.use(fake, f"fields: {{estimate: {{id: {F_ESTIMATE}}}}}")
    code, out, _ = env.run("--json", "fields", "check")
    doc = json.loads(out)
    assert (code, doc) == (0, {"site": "main", "problems": []})
    assert_golden("cli", "fields-check", "clean", doc)
    code, out, _ = env.run("fields", "check")
    assert code == 0 and "no problems" in out


def test_fields_check_missing_mapping(env: Env, fake: FakeProvider) -> None:
    env.use(
        fake,
        "fields: {acceptance_criteria: {name: No Such Field}, extra: {gone: {id: customfield_1}}}",
    )
    code, out, _ = env.run("--json", "fields", "check")
    doc = json.loads(out)
    assert code == 0
    assert only(doc, "acceptance_criteria") == {
        "logical": "acceptance_criteria",
        "problem": "missing",
    }
    assert only(doc, "gone")["problem"] == "missing"
    assert "customfield_1" not in out
    assert_golden("cli", "fields-check", "missing", doc)


def test_fields_check_type_mismatch_for_a_non_numeric_estimate(
    env: Env, fake: FakeProvider
) -> None:
    env.use(fake, f"fields: {{estimate: {{id: {F_CRITERIA}}}}}")
    code, out, _ = env.run("--json", "fields", "check")
    doc = json.loads(out)
    assert code == 0
    assert only(doc, "estimate") == {"logical": "estimate", "problem": "type_mismatch"}
    assert_golden("cli", "fields-check", "type-mismatch", doc)


def test_fields_check_refetches_a_missing_mapping_once_when_the_cache_is_old(
    env: Env, fake: FakeProvider
) -> None:
    env.use(fake, "fields: {acceptance_criteria: {name: Late Field}}")
    env.run("--json", "fields", "list")  # caches the list
    fake.extra_fields = [
        {
            "id": "cf_late",
            "name": "Late Field",
            "custom": True,
            "schema": {"type": "string"},
        }
    ]
    # fetched within the last minute: not refetched, so still missing
    code, out, _ = env.run("--json", "fields", "check")
    assert only(json.loads(out), "acceptance_criteria")["problem"] == "missing"
    assert paths(fake).count("/rest/api/3/field") == 1
    # a refresh sees it
    env.run("--json", "fields", "refresh")
    code, out, _ = env.run("--json", "fields", "check")
    assert not [p for p in json.loads(out)["problems"] if p["logical"] == "acceptance_criteria"]


def test_fields_check_invalid_config_is_exit_2_and_offline(env: Env, fake: FakeProvider) -> None:
    env.use(fake, "fields: {estimate: {id: x, name: y}}")
    code, out, _ = env.run("--json", "fields", "check")
    assert (code, err_of(out)["code"]) == (2, "invalid_config")
    assert fake.calls == []


def test_fields_check_provider_failure_is_an_error_not_a_problem(
    env: Env, fake: FakeProvider
) -> None:
    fake.inject(Fault("status", path="field", status=403, body="no"))
    env.use(fake)
    code, out, _ = env.run("--json", "fields", "check")
    assert code == 12 and err_of(out)["code"] == "forbidden"


def test_fields_check_ambiguous_candidates_hide_ids_in_names(env: Env, fake: FakeProvider) -> None:
    fake.extra_fields = [
        {
            "id": "cf_pts_a",
            "name": "Story Points",
            "custom": True,
            "schema": {"type": "number", "custom": "x:customfieldtypes:float"},
        },
        {
            "id": "cf_pts_b",
            "name": "Story Points",
            "custom": True,
            "schema": {"type": "number", "custom": "x:customfieldtypes:float"},
        },
        {
            "id": "cf_pts_c",
            "name": "see customfield_90001",
            "custom": True,
            "schema": {"type": "number", "custom": "y:jsw-story-points"},
        },
    ]
    env.use(fake)
    code, out, _ = env.run("--json", "fields", "check")
    assert code == 0 and not IDS.search(out)
    assert "see <id>" in json.loads(out)["problems"][0]["candidates"]


# --- fields refresh ---------------------------------------------------------------------------


def test_fields_refresh_replaces_the_cache(env: Env, fake: FakeProvider) -> None:
    env.use(fake)
    env.run("--json", "fields", "list")
    assert paths(fake).count("/rest/api/3/field") == 1
    code, out, _ = env.run("--json", "fields", "refresh")
    doc = json.loads(out)
    assert (code, doc) == (0, {"site": "main", "refreshed": True})
    assert_golden("cli", "fields-refresh", "default", doc)
    assert paths(fake).count("/rest/api/3/field") == 2  # bypassed the fresh cache
    assert "cache/main/fields.json" in env.files()
    code, out, _ = env.run("fields", "refresh")
    assert code == 0 and "main" in out


def test_fields_refresh_failure_keeps_the_old_cache(env: Env, fake: FakeProvider) -> None:
    env.use(fake)
    env.run("--json", "fields", "list")
    before = env.files()["cache/main/fields.json"]
    fake.field_list_body = "garbage"
    code, out, _ = env.run("--json", "fields", "refresh")
    assert (code, err_of(out)["code"]) == (12, "provider_changed")
    assert env.files()["cache/main/fields.json"] == before


# --- validate --probe -------------------------------------------------------------------------


def test_probe_clean_site_prints_no_problems(env: Env, fake: FakeProvider) -> None:
    env.use(
        fake,
        f"fields: {{estimate: {{id: {F_ESTIMATE}}}}}\n"
        "    queries: {open: 'project = DEMO ORDER BY created DESC'}",
    )
    code, out, err = env.run("--json", "validate", "--probe")
    assert code == 0, err
    doc = json.loads(out)
    assert doc == {
        "valid": True,
        "probe": True,
        "warnings": [],
        "sites": [
            {
                "site": "main",
                "account_id": ME_ACCOUNT,
                "problems": [],
                "queries": [{"name": "open", "valid": True, "error": None}],
            }
        ],
    }
    assert_golden("cli", "validate", "probe-clean", doc)


def test_probe_reports_every_kind_of_problem_as_data_and_exits_0(
    env: Env, fake: FakeProvider
) -> None:
    env.use(
        fake,
        "account_id: someone-else-entirely\n"
        f"    fields: {{estimate: {{id: {F_CRITERIA}}}, acceptance_criteria: {{name: Nope}}}}\n"
        "    queries: {good: 'project = DEMO', badfield: 'nosuchfield = 1', syntax: 'project = ('}",
    )
    code, out, err = env.run("--json", "validate", "--probe")
    assert code == 0, err
    doc = json.loads(out)
    site = doc["sites"][0]
    kinds = {(p.get("logical"), p["problem"]) for p in site["problems"]}
    assert (None, "account_mismatch") in kinds
    assert ("estimate", "type_mismatch") in kinds
    assert ("acceptance_criteria", "missing") in kinds
    queries = {q["name"]: q for q in site["queries"]}
    assert [q["name"] for q in site["queries"]] == sorted(queries)
    assert queries["good"] == {"name": "good", "valid": True, "error": None}
    assert queries["badfield"]["valid"] is False and "nosuchfield" in queries["badfield"]["error"]
    assert queries["syntax"]["valid"] is False and queries["syntax"]["error"]
    assert doc["valid"] is True
    assert_golden("cli", "validate", "probe-problems", doc)


def test_probe_writes_nothing_to_the_provider(env: Env, fake: FakeProvider) -> None:
    env.use(fake, "queries: {open: 'project = DEMO'}")
    env.run("--json", "validate", "--probe")
    assert paths(fake)  # it did call
    allowed = {
        ("GET", "/rest/api/3/myself"),
        ("GET", "/rest/api/3/field"),
        ("POST", "/rest/api/3/jql/parse"),
    }
    assert {(c.method, c.path) for c in fake.calls} <= allowed
    assert ("POST", "/rest/api/3/jql/parse") in {(c.method, c.path) for c in fake.calls}
    assert not [e for e in env.files() if e.startswith("audit")]  # no audit record: no write


def test_probe_query_that_cannot_compile_is_a_problem_not_a_failure(
    env: Env, fake: FakeProvider
) -> None:
    env.use(fake, "queries: {orderonly: 'ORDER BY created', unbalanced: 'project = (DEMO'}")
    code, out, _ = env.run("--json", "validate", "--probe")
    assert code == 0
    queries = {q["name"]: q for q in json.loads(out)["sites"][0]["queries"]}
    assert not queries["orderonly"]["valid"] and not queries["unbalanced"]["valid"]


def test_probe_a_parse_answer_of_the_wrong_shape_is_provider_changed(
    env: Env, fake: FakeProvider
) -> None:
    fake.inject(Fault("status", path="jql/parse", status=200, body="[1, 2]"))
    env.use(fake, "queries: {open: 'project = DEMO'}")
    code, out, _ = env.run("--json", "validate", "--probe")
    assert (code, err_of(out)["code"]) == (12, "provider_changed")


def test_probe_a_provider_error_exits_with_its_own_code(env: Env, fake: FakeProvider) -> None:
    fake.inject(Fault("status", path="myself", status=401, body="no"))
    env.use(fake)
    code, out, _ = env.run("--json", "validate", "--probe")
    assert code == 12 and err_of(out)["site"] == "main"


def test_probe_invalid_config_is_exit_2_and_offline(env: Env, fake: FakeProvider) -> None:
    env.use(fake, "fields: {estimate: {}}")
    code, out, _ = env.run("--json", "validate", "--probe")
    assert (code, err_of(out)["code"]) == (2, "invalid_config")
    assert fake.calls == []


def test_probe_never_prints_a_provider_field_id(env: Env, fake: FakeProvider) -> None:
    env.use(fake, "queries: {open: 'project = DEMO'}")
    _, out, err = env.run("--json", "validate", "--probe")
    assert not IDS.search(out + err)
    _, out, err = env.run("validate", "--probe")
    assert not IDS.search(out + err)


def test_probe_human_output(env: Env, fake: FakeProvider) -> None:
    env.use(fake, "queries: {bad: 'nosuchfield = 1'}")
    code, out, _ = env.run("validate", "--probe")
    assert code == 0
    assert "main" in out and "bad" in out and "estimate" in out


def test_probe_scrubs_every_sites_token_in_a_two_site_run(
    env: Env, fake_factory: Callable[[Any], FakeProvider]
) -> None:
    a, b = fake_factory(None), fake_factory(Quirks(credentials=None))
    other = "another-token-value-77"
    env.mp.setenv(BETA_VAR, other)
    two_sites(env, a, b, ("alpha", "beta"))
    code, out, err = env.run("--json", "validate", "--probe")
    assert code == 0, err
    assert [s["site"] for s in json.loads(out)["sites"]] == ["alpha", "beta"]
    assert sanitize.scrub(SITE_TOKEN) == "<redacted>" and sanitize.scrub(other) == "<redacted>"


# --- no credential in any output --------------------------------------------------------------

COMMANDS = [
    ["fields", "list"],
    ["fields", "check"],
    ["fields", "refresh"],
    ["validate", "--probe"],
]


@pytest.mark.parametrize("argv", COMMANDS, ids=lambda a: " ".join(a))
@pytest.mark.parametrize("debug", [False, True])
@pytest.mark.parametrize("flags", [[], ["--verbose"], ["--json", "--verbose"]])
def test_token_never_appears_in_any_channel(
    env: Env, fake: FakeProvider, argv: list[str], debug: bool, flags: list[str]
) -> None:
    env.use(fake, "queries: {open: 'project = DEMO'}")
    if debug:
        env.mp.setenv("TICKETCTL_DEBUG", "1")
    code, out, err = env.run(*flags, *argv)
    assert code == 0, err
    forbidden = [SITE_TOKEN, fake.basic_header().split(" ", 1)[1]]
    for text in (out, err, *env.files().values()):
        for secret in forbidden:
            assert secret not in text
    if "--verbose" in flags:
        assert "Authorization" not in err


@pytest.mark.parametrize("argv", COMMANDS, ids=lambda a: " ".join(a))
def test_token_is_scrubbed_from_an_error_that_echoes_it(
    env: Env, fake: FakeProvider, argv: list[str]
) -> None:
    fake.inject(Fault("status", path="field", status=403, body=f"denied {SITE_TOKEN}"))
    fake.inject(Fault("status", path="myself", status=403, body=f"denied {SITE_TOKEN}"))
    env.use(fake)
    env.mp.setenv("TICKETCTL_DEBUG", "1")
    code, out, err = env.run("--json", "--verbose", *argv)
    assert code == 12
    for text in (out, err, *env.files().values()):
        assert SITE_TOKEN not in text


def test_a_token_in_a_provider_field_name_is_redacted(env: Env, fake: FakeProvider) -> None:
    fake.extra_fields = [
        {"id": "cf_token", "name": f"Mr {SITE_TOKEN}", "custom": True, "schema": None}
    ]
    env.use(fake)
    for argv in (["fields", "list"], ["--json", "fields", "list"]):
        code, out, err = env.run(*argv)
        assert code == 0 and SITE_TOKEN not in out + err and "<redacted>" in out


def test_a_token_in_a_parse_error_is_redacted(
    env: Env, fake: FakeProvider, monkeypatch: pytest.MonkeyPatch
) -> None:
    env.use(fake, "queries: {open: 'project = DEMO'}")

    class Resp:
        status = 200
        data = {"queries": [{"query": "x", "errors": [f"bad {SITE_TOKEN} here"]}]}

    real = __import__("ticketctl.gateway", fromlist=["Gateway"]).Gateway.request

    def request(self: Any, method: str, path: str, **kw: Any) -> Any:
        if path.endswith("/jql/parse"):
            return Resp()
        return real(self, method, path, **kw)

    monkeypatch.setattr("ticketctl.gateway.Gateway.request", request)
    code, out, err = env.run("--json", "validate", "--probe")
    assert code == 0 and SITE_TOKEN not in out + err and "<redacted>" in out


# --- review round 1 ---------------------------------------------------------------------------

BETA_VAR = "TICKETCTL_TEST_BETA_TOKEN"


def two_sites(env: Env, a: FakeProvider, b: FakeProvider, order: tuple[str, str]) -> None:
    sites = {"alpha": env.site(a, "alpha"), "beta": env.site(b, "beta", token_var=BETA_VAR)}
    env.write("sites:\n" + "".join(sites[n] for n in order))


def test_probe_site_flag_probes_only_that_site(
    env: Env, fake_factory: Callable[[Any], FakeProvider]
) -> None:
    a, b = fake_factory(None), fake_factory(None)
    two_sites(env, a, b, ("alpha", "beta"))  # beta's token variable is unset
    code, out, err = env.run("--json", "--site", "alpha", "validate", "--probe")
    assert code == 0, err
    assert [s["site"] for s in json.loads(out)["sites"]] == ["alpha"]
    assert b.calls == []


def test_probe_unknown_site_flag_is_exit_22_and_offline(
    env: Env, fake_factory: Callable[[Any], FakeProvider]
) -> None:
    a, b = fake_factory(None), fake_factory(None)
    two_sites(env, a, b, ("alpha", "beta"))
    code, out, _ = env.run("--json", "--site", "nowhere", "validate", "--probe")
    assert code == 22 and err_of(out)["code"]
    assert a.calls == [] and b.calls == []


def test_probe_without_site_flag_probes_every_site_sorted_not_in_config_order(
    env: Env, fake_factory: Callable[[Any], FakeProvider]
) -> None:
    a, b = fake_factory(None), fake_factory(None)
    env.mp.setenv(BETA_VAR, SITE_TOKEN)
    two_sites(env, a, b, ("beta", "alpha"))
    code, out, err = env.run("--json", "validate", "--probe")
    assert code == 0, err
    assert [s["site"] for s in json.loads(out)["sites"]] == ["alpha", "beta"]


def test_probe_a_site_without_a_token_fails_the_whole_probe_with_no_partial_output(
    env: Env, fake_factory: Callable[[Any], FakeProvider]
) -> None:
    a, b = fake_factory(None), fake_factory(None)
    two_sites(env, a, b, ("alpha", "beta"))  # beta has no token
    code, out, _ = env.run("--json", "validate", "--probe")
    error = err_of(out)
    assert (code, error["code"], error["site"]) == (12, "auth_required", "beta")
    assert "sites" not in out


def test_probe_a_400_from_the_parse_endpoint_is_provider_changed(
    env: Env, fake: FakeProvider
) -> None:
    fake.inject(Fault("status", path="jql/parse", status=400, body="bad request"))
    one_query(env, fake)
    code, out, _ = env.run("--json", "validate", "--probe")
    assert (code, err_of(out)["code"]) == (12, "provider_changed")


def test_probe_an_entry_without_errors_is_a_valid_query(
    env: Env, fake: FakeProvider, monkeypatch: pytest.MonkeyPatch
) -> None:
    one_query(env, fake)
    answer_parse(monkeypatch, {"queries": [{"query": "x"}]})
    code, out, _ = env.run("--json", "validate", "--probe")
    assert code == 0
    queries = json.loads(out)["sites"][0]["queries"]
    assert queries == [{"name": "open", "valid": True, "error": None}]


@pytest.mark.parametrize(
    "entry", [{"errors": "oops"}, {"errors": [1]}, {"errors": None}, [], "x"], ids=repr
)
def test_probe_errors_that_are_not_a_list_of_strings_are_provider_changed(
    env: Env, fake: FakeProvider, monkeypatch: pytest.MonkeyPatch, entry: Any
) -> None:
    one_query(env, fake)
    answer_parse(monkeypatch, {"queries": [entry]})
    code, out, _ = env.run("--json", "validate", "--probe")
    assert (code, err_of(out)["code"]) == (12, "provider_changed")


def test_probe_blank_error_strings_are_dropped(
    env: Env, fake: FakeProvider, monkeypatch: pytest.MonkeyPatch
) -> None:
    one_query(env, fake)
    answer_parse(monkeypatch, {"queries": [{"errors": ["", "  \t"]}]})
    _, out, _ = env.run("--json", "validate", "--probe")
    assert json.loads(out)["sites"][0]["queries"][0]["valid"] is True
    answer_parse(monkeypatch, {"queries": [{"errors": ["", "real problem"]}]})
    _, out, _ = env.run("--json", "validate", "--probe")
    query = json.loads(out)["sites"][0]["queries"][0]
    assert (query["valid"], query["error"]) == (False, "real problem")


def test_probe_parse_errors_never_carry_a_field_id(
    env: Env, fake: FakeProvider, monkeypatch: pytest.MonkeyPatch
) -> None:
    one_query(env, fake)
    text = "Field 'customfield_90001' and 'cf[90002]' are unknown"
    answer_parse(monkeypatch, {"queries": [{"errors": [text]}]})
    for argv in (["--json", "validate", "--probe"], ["validate", "--probe"]):
        _, out, err = env.run(*argv)
        assert not re.search(r"customfield_[0-9]+|cf\[[0-9]+\]", out + err)
        assert "<id>" in out


def test_probe_sends_one_parse_request_per_query(
    env: Env, fake: FakeProvider, monkeypatch: pytest.MonkeyPatch
) -> None:
    env.use(fake, "queries: {c: 'project = C', a: 'project = A', b: 'project = B'}")
    sent = answer_parse(monkeypatch, {"queries": [{"errors": []}]})
    code, out, _ = env.run("--json", "validate", "--probe")
    assert code == 0 and len(sent) == 3 and [q[-2] for q in sent] == ["A", "B", "C"]
    assert [q["name"] for q in json.loads(out)["sites"][0]["queries"]] == ["a", "b", "c"]


def test_probe_joins_several_errors_with_a_semicolon(
    env: Env, fake: FakeProvider, monkeypatch: pytest.MonkeyPatch
) -> None:
    one_query(env, fake)
    answer_parse(monkeypatch, {"queries": [{"errors": ["first", "second"]}]})
    _, out, _ = env.run("--json", "validate", "--probe")
    assert json.loads(out)["sites"][0]["queries"][0]["error"] == "first; second"


def test_probe_caps_an_oversized_error(
    env: Env, fake: FakeProvider, monkeypatch: pytest.MonkeyPatch
) -> None:
    one_query(env, fake)
    answer_parse(monkeypatch, {"queries": [{"errors": ["x" * 5000]}]})
    _, out, _ = env.run("--json", "validate", "--probe")
    error = json.loads(out)["sites"][0]["queries"][0]["error"]
    assert len(error) <= 1024 and error.startswith("xxx")


def test_probe_sanitizes_control_characters_in_an_error(
    env: Env, fake: FakeProvider, monkeypatch: pytest.MonkeyPatch
) -> None:
    one_query(env, fake)
    answer_parse(monkeypatch, {"queries": [{"errors": ["a\x1b[31mred\x00\u202eflip\x07"]}]})
    for argv in (["--json", "validate", "--probe"], ["validate", "--probe"]):
        _, out, err = env.run(*argv)
        assert not re.search("[\x00-\x08\x0b-\x1f\x7f\u202e]", out + err)
        assert "red" in out


def test_probe_fetches_myself_and_the_field_list_fresh_despite_a_fresh_cache(
    env: Env, fake: FakeProvider
) -> None:
    env.use(fake)
    env.run("--json", "whoami")
    env.run("--json", "fields", "list")
    before = paths(fake)
    assert "/rest/api/3/myself" in before and "/rest/api/3/field" in before
    env.run("--json", "validate", "--probe")
    after = paths(fake)[len(before) :]
    assert after.count("/rest/api/3/myself") == 1 and after.count("/rest/api/3/field") == 1


def test_probe_a_503_on_parse_is_retried_like_a_read(env: Env, fake: FakeProvider) -> None:
    fake.inject(Fault("status", path="jql/parse", status=503, times=1))
    one_query(env, fake)
    code, out, err = env.run("--json", "validate", "--probe")
    assert code == 0, err
    assert json.loads(out)["sites"][0]["queries"][0]["valid"] is True


def test_probe_shares_one_deadline_across_the_sites(
    env: Env, fake_factory: Callable[[Any], FakeProvider], monkeypatch: pytest.MonkeyPatch
) -> None:
    a, b = fake_factory(None), fake_factory(None)
    env.mp.setenv(BETA_VAR, SITE_TOKEN)
    two_sites(env, a, b, ("alpha", "beta"))
    deadlines: list[Any] = []
    real_open = probe_module.open_gateway

    def opener(rt: Any, name: str, **kw: Any) -> Any:
        gw = real_open(rt, name, **kw)
        deadlines.append(gw.deadline)
        return gw

    monkeypatch.setattr(probe_module, "open_gateway", opener)
    code, _, err = env.run("--json", "validate", "--probe")
    assert code == 0, err
    assert len(deadlines) == 2 and deadlines[0] is deadlines[1]


def test_open_gateway_adds_every_sites_token_to_the_scrub(
    env: Env, fake_factory: Callable[[Any], FakeProvider]
) -> None:
    a, b = fake_factory(None), fake_factory(Quirks(credentials=None))
    other = "another-token-value-88"
    env.mp.setenv(BETA_VAR, other)
    two_sites(env, a, b, ("alpha", "beta"))
    rt = runtime.load_runtime(Globals(config=str(env.config)))
    runtime.open_gateway(rt, "alpha")
    runtime.open_gateway(rt, "beta")
    assert sanitize.scrub(SITE_TOKEN) == "<redacted>" and sanitize.scrub(other) == "<redacted>"


def test_add_secrets_unions_and_configure_scrubber_resets() -> None:
    sanitize.configure_scrubber(["first-secret-value"])
    sanitize.add_secrets(["second-secret-value"], basic_values=["third-secret-value"])
    for value in ("first-secret-value", "second-secret-value", "third-secret-value"):
        assert sanitize.scrub(value) == "<redacted>"
    sanitize.configure_scrubber([])
    assert sanitize.scrub("first-secret-value") == "first-secret-value"


def test_an_empty_field_list_keeps_the_old_cache_and_is_provider_changed(
    env: Env, fake: FakeProvider
) -> None:
    env.use(fake)
    env.run("--json", "fields", "list")
    before = env.files()["cache/main/fields.json"]
    fake.field_list_body = []
    code, out, _ = env.run("--json", "fields", "refresh")
    assert (code, err_of(out)["code"]) == (12, "provider_changed")
    assert env.files()["cache/main/fields.json"] == before


def test_an_empty_field_list_is_never_cached_on_a_cold_load(env: Env, fake: FakeProvider) -> None:
    fake.field_list_body = []
    env.use(fake)
    code, out, _ = env.run("--json", "fields", "list")
    assert (code, err_of(out)["code"]) == (12, "provider_changed")
    assert "cache/main/fields.json" not in env.files()


def test_fields_refresh_with_no_cache_ttl_still_reports_refreshed(
    env: Env, fake: FakeProvider
) -> None:
    env.use(fake, "cache_ttl: 0")
    code, out, err = env.run("--json", "fields", "refresh")
    assert (code, json.loads(out)) == (0, {"site": "main", "refreshed": True}), err
    assert "cache/main/fields.json" not in env.files()


def test_fields_list_human_output_ends_with_the_mapping(env: Env, fake: FakeProvider) -> None:
    env.use(fake, f"fields: {{estimate: {{id: {F_ESTIMATE}}}}}")
    code, out, _ = env.run("fields", "list")
    assert code == 0
    lines = out.splitlines()
    assert re.search(rf"^estimate: {F_ESTIMATE} \S", "\n".join(lines), re.M)
    assert "acceptance_criteria: -" in lines


def test_fields_check_refetches_a_missing_extra_name_when_the_cache_is_old(
    env: Env, fake: FakeProvider
) -> None:
    env.use(fake, "fields: {extra: {late: {name: Late Field}}}")
    env.run("--json", "fields", "list")
    cache_file = env.state / "cache" / "main" / "fields.json"
    doc = json.loads(cache_file.read_text())
    doc["fetched_at"] = (datetime.now(UTC) - timedelta(minutes=10)).strftime("%Y-%m-%dT%H:%M:%SZ")
    cache_file.write_text(json.dumps(doc))
    fake.extra_fields = [
        {"id": "cf_late", "name": "Late Field", "custom": True, "schema": {"type": "string"}}
    ]
    code, out, _ = env.run("--json", "fields", "check")
    assert code == 0 and not [p for p in json.loads(out)["problems"] if p["logical"] == "late"]
    assert paths(fake).count("/rest/api/3/field") == 2
