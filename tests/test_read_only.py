"""Read-only mode (#49): the command layer, the gateway guard, the config and the environment.

The mode is an accident guard, not a boundary (docs/interface.md, Read-only mode). These tests prove
that, with it on, no write command sends a request or leaves a side effect, that the gateway
refuses a write on its own, and that nothing turns the mode off once it is on.
"""

from __future__ import annotations

import argparse
import json
import re
from pathlib import Path
from typing import Any

import pytest

from tests.fake_provider import SITE_EMAIL, SITE_TOKEN, FakeProvider
from tests.golden import assert_golden, validator
from tests.scripted_server import ScriptedServer, json_reply
from tests.test_gateway import make_rig
from ticketctl import commands, config
from ticketctl.cli import main
from ticketctl.commands import REGISTRY, WRITE, CommandSpec
from ticketctl.errors import CODES, TicketctlError
from ticketctl.flags import Globals
from ticketctl.gateway import GatewayError
from ticketctl.runtime import load_runtime, open_gateway


@pytest.fixture
def serve():
    started: list[ScriptedServer] = []

    def make(script: Any, **kw: Any) -> ScriptedServer:
        server = ScriptedServer(script, **kw).start()
        started.append(server)
        return server

    yield make
    for server in started:
        server.close()


TOKEN_VAR = "TICKETCTL_TEST_RO_TOKEN"

# One valid invocation per write command. A command registered as a write that is missing here
# fails `test_every_registered_write_command_has_a_case`: add its case, and the tests below then
# prove it is refused in read-only mode.
LINK_ARGS = ["--type", "Blocks", "--direction", "outward", "--other", "DEMO-2"]
WRITE_CASES: dict[tuple[str, ...], list[str]] = {
    ("transition",): ["transition", "DEMO-1", "--to", "Done"],
    ("comment",): ["comment", "DEMO-1", "--body", "hello"],
    ("comment-edit",): ["comment-edit", "DEMO-1", "1", "--body", "hello"],
    ("comment-delete",): ["comment-delete", "DEMO-1", "1"],
    ("create",): ["create", "--project", "DEMO", "--type", "Task", "--title", "t"],
    ("update",): ["update", "DEMO-1", "--title", "new"],
    ("sprint", "add"): ["sprint", "add", "DEMO-1", "--sprint", "1"],
    ("sprint", "remove"): ["sprint", "remove", "DEMO-1"],
    ("link", "add"): [
        "link",
        "add",
        "DEMO-1",
        "--type",
        "Blocks",
        "--direction",
        "outward",
        "--other",
        "DEMO-2",
    ],
    ("link", "remove"): [
        "link",
        "remove",
        "DEMO-1",
        "--type",
        "Blocks",
        "--direction",
        "outward",
        "--other",
        "DEMO-2",
    ],
}


class Rig:
    def __init__(self, root: Path, fake: FakeProvider, capsys: Any, read_only: bool) -> None:
        self.root, self.fake, self.capsys = root, fake, capsys
        self.config = root / "config.yaml"
        self.write_config(read_only)

    def write_config(self, read_only: bool | None, hooks: str = "") -> None:
        extra = "" if read_only is None else f"read_only: {str(read_only).lower()}\n"
        self.config.write_text(
            f"{extra}{hooks}sites:\n  main:\n    url: {self.fake.url}\n    email: {SITE_EMAIL}\n"
            f"    token_env: {TOKEN_VAR}\n    allow_http: true\n"
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

    @property
    def state(self) -> Path:
        return self.root / "state" / "ticketctl"

    def side_effects(self) -> list[str]:
        """Audit, lock and pending files under the state directory (none may appear)."""
        if not self.state.exists():
            return []
        return sorted(
            str(p.relative_to(self.state))
            for p in self.state.rglob("*")
            if p.name == "audit.jsonl" or "locks" in p.parts
        )


@pytest.fixture
def rig(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: Any, fake: FakeProvider) -> Rig:
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    monkeypatch.setenv(TOKEN_VAR, SITE_TOKEN)
    monkeypatch.delenv("TICKETCTL_READ_ONLY", raising=False)
    monkeypatch.setenv("PATH", "/nonexistent")
    return Rig(tmp_path, fake, capsys, read_only=True)


# --- the command layer ---------------------------------------------------------------------------


def test_every_registered_write_command_has_a_case() -> None:
    writes = {path for path, spec in REGISTRY.items() if spec.kind == WRITE}
    assert writes == set(WRITE_CASES), "add (or remove) a WRITE_CASES entry for a write command"


@pytest.mark.parametrize("argv", list(WRITE_CASES.values()), ids=[" ".join(p) for p in WRITE_CASES])
def test_a_write_command_is_refused_with_nothing_sent_and_no_side_effect(
    rig: Rig, argv: list[str]
) -> None:
    code, env = rig.doc(*argv)
    assert code == 20
    assert (env["error"]["code"], env["error"]["reason"]) == ("read_only", "read_only")
    assert env["error"]["retryable"] is False and env["error"]["outcome_unknown"] is False
    assert re.search(r"read-only", env["error"]["message"])
    assert rig.fake.calls == []  # not even the /myself read
    assert rig.side_effects() == []
    validator("cli/error.json").validate(env)


def test_the_refusal_also_applies_to_yes_with_a_plan_hash(rig: Rig) -> None:
    code, env = rig.doc(
        "--yes", "--expect-plan", "0" * 64, "comment", "DEMO-1", "--body", "x", "--dedupe-key", "k"
    )
    assert (code, env["error"]["code"]) == (20, "read_only")
    assert rig.fake.calls == []
    assert rig.side_effects() == []


def test_a_refusal_runs_no_pre_or_post_hook(rig: Rig, tmp_path: Path) -> None:
    pre, post = tmp_path / "pre-ran", tmp_path / "post-ran"
    rig.write_config(
        True,
        hooks=(
            f"hooks:\n  pre-comment:\n    - cmd: /bin/sh -c 'echo x > {pre}'\n"
            f"  post-comment:\n    - cmd: /bin/sh -c 'echo x > {post}'\n"
        ),
    )
    code, env = rig.doc("comment", "DEMO-1", "--body", "x")
    assert (code, env["error"]["code"]) == (20, "read_only")
    assert not pre.exists() and not post.exists()
    assert rig.fake.calls == [] and rig.side_effects() == []


# Each upper layer on its own: the layers below it are replaced by recorders, so a layer that lets a
# write through would show up as a recorded call, a hook, a lock or an audit record.
NON_DRY_GLOBALS = [
    ["--verbose"],
    ["--quiet"],
    ["--yes", "--expect-plan", "0" * 64],
    ["--actor", "human:tester", "--override-escalation"],
    ["--json"],
]


@pytest.mark.parametrize("argv", list(WRITE_CASES.values()), ids=[" ".join(p) for p in WRITE_CASES])
def test_the_dispatch_layer_refuses_a_real_write_on_its_own(
    rig: Rig, monkeypatch: pytest.MonkeyPatch, argv: list[str]
) -> None:
    from ticketctl import writeflow

    reached: list[str] = []
    monkeypatch.setattr(writeflow, "execute", lambda *a, **k: reached.append("execute") or {})
    code, env = rig.doc(*argv)
    assert (code, env["error"]["code"]) == (20, "read_only")
    assert reached == [] and rig.fake.calls == [] and rig.side_effects() == []


@pytest.mark.parametrize("flags", NON_DRY_GLOBALS, ids=[" ".join(f) for f in NON_DRY_GLOBALS])
def test_only_dry_run_exempts_a_write_at_dispatch(
    rig: Rig, monkeypatch: pytest.MonkeyPatch, flags: list[str]
) -> None:
    from ticketctl import writeflow

    reached: list[str] = []
    monkeypatch.setattr(writeflow, "execute", lambda *a, **k: reached.append("execute") or {})
    code, env = rig.doc(*flags, "comment", "DEMO-1", "--body", "x", "--dedupe-key", "k")
    assert (code, env["error"]["code"]) == (20, "read_only")
    assert reached == [] and rig.fake.calls == [] and rig.side_effects() == []


@pytest.mark.parametrize(
    "overrides",
    [
        {"verbose": True},
        {"quiet": True},
        {"yes": True, "expect_plan": "0" * 64},
        {"override_escalation": "ticketctl"},
        {"json": True},
        {},
    ],
)
def test_the_write_flow_refuses_every_non_dry_run_on_its_own(
    rig: Rig, overrides: dict[str, Any]
) -> None:
    from ticketctl import writeflow

    class Boom:
        name = "boom"

        def __getattr__(self, name: str) -> Any:
            raise AssertionError(f"the action was touched: {name}")

    rt = load_runtime(Globals(config=str(rig.config), **overrides))
    with pytest.raises(TicketctlError) as err:
        writeflow.execute(rt, "main", Boom())  # type: ignore[arg-type]
    assert err.value.code == "read_only"
    assert rig.fake.calls == [] and rig.side_effects() == []


def test_a_refusal_in_plain_text_names_the_mode(rig: Rig) -> None:
    code, _, err = rig.run("comment", "DEMO-1", "--body", "x")
    assert code == 20 and re.search(r"read_only.*read-only mode", err)


def test_usage_errors_still_come_first(rig: Rig) -> None:
    code, env = rig.doc("comment", "DEMO-1")  # neither --body nor --body-file
    assert (code, env["error"]["code"]) == (2, "usage")


def test_dry_run_still_plans_and_sends_no_write(rig: Rig) -> None:
    code, plan = rig.doc("--dry-run", "comment", "DEMO-1", "--body", "hello")
    assert code == 0 and plan["action"] == "comment" and plan["steps"]
    assert [c for c in rig.fake.calls if c.method != "GET"] == []
    assert rig.side_effects() == []


def test_dry_run_with_run_hooks_passes_the_gate_and_a_plain_write_does_not(
    rig: Rig, tmp_path: Path
) -> None:
    ran = tmp_path / "pre-ran"
    rig.write_config(
        True, hooks=f"hooks:\n  pre-comment:\n    - cmd: /bin/sh -c 'echo x > {ran}'\n"
    )
    code, plan = rig.doc("--dry-run", "--run-hooks", "comment", "DEMO-1", "--body", "hello")
    assert code == 0 and plan["action"] == "comment" and plan["steps"]
    assert ran.exists()  # the operator's own hook ran; read-only mode does not restrict it
    assert [c for c in rig.fake.calls if c.method != "GET"] == []
    assert rig.side_effects() == []
    ran.unlink()
    code, env = rig.doc("comment", "DEMO-1", "--body", "hello", "--dedupe-key", "k")
    assert (code, env["error"]["code"]) == (20, "read_only") and not ran.exists()


def test_dry_run_of_every_write_command_sends_no_write(rig: Rig) -> None:
    for argv in WRITE_CASES.values():
        code, doc = rig.doc("--dry-run", *argv)
        # a plan or an ordinary refusal, never read_only
        assert code == 0 or doc["error"]["code"] != "read_only", argv
    assert [c for c in rig.fake.calls if c.method != "GET" and "search" not in c.path] == []


def test_reads_work(rig: Rig) -> None:
    code, doc = rig.doc("get", "DEMO-1")
    assert code == 0 and doc["tickets"][0]["key"] == "DEMO-1"
    code, doc = rig.doc("search", "--jql", "project = DEMO")  # a read over POST
    assert code == 0 and "tickets" in doc
    assert rig.run("whoami")[0] == 0
    assert rig.run("sites")[0] == 0
    assert rig.run("fields", "refresh")[0] == 0  # the cache is local bookkeeping
    assert rig.run("validate")[0] == 0


def test_export_to_a_file_stays_allowed(rig: Rig, tmp_path: Path) -> None:
    target = tmp_path / "out.jsonl"
    assert rig.run("export", "--to", str(target))[0] == 0
    assert target.exists()


def test_validate_reports_the_mode_and_omits_it_when_off(rig: Rig) -> None:
    code, doc = rig.doc("validate")
    assert code == 0 and doc["read_only"] is True
    validator("cli/validate.json").validate(doc)
    assert_golden("cli", "validate", "read-only", doc)
    rig.write_config(None)
    code, doc = rig.doc("validate")
    assert code == 0 and "read_only" not in doc


def test_the_error_golden(rig: Rig) -> None:
    code, env = rig.doc("comment", "DEMO-1", "--body", "x")
    assert code == 20
    assert_golden("cli", "error", "read-only", env)


def test_off_by_default(rig: Rig) -> None:
    rig.write_config(None)
    code, doc = rig.doc("--dry-run", "comment", "DEMO-1", "--body", "hello")
    assert code == 0 and doc["action"] == "comment"


# --- turning it on and keeping it on -------------------------------------------------------------


@pytest.mark.parametrize("value", ["1", "true", "TRUE", "yes", "On"])
def test_the_environment_turns_it_on(rig: Rig, monkeypatch: pytest.MonkeyPatch, value: str) -> None:
    rig.write_config(None)
    monkeypatch.setenv("TICKETCTL_READ_ONLY", value)
    code, env = rig.doc("comment", "DEMO-1", "--body", "x")
    assert (code, env["error"]["code"]) == (20, "read_only")
    assert rig.fake.calls == []


@pytest.mark.parametrize("value", ["", "0", "false", "No", "OFF"])
def test_a_false_environment_value_means_not_set(
    rig: Rig, monkeypatch: pytest.MonkeyPatch, value: str
) -> None:
    rig.write_config(None)
    monkeypatch.setenv("TICKETCTL_READ_ONLY", value)
    code, doc = rig.doc("--dry-run", "comment", "DEMO-1", "--body", "hello")
    assert code == 0 and doc["action"] == "comment"


@pytest.mark.parametrize("value", ["2", "ture", "enabled", "1 ", "١", "true\n", "x" * 5000])
def test_an_unreadable_environment_value_is_a_config_error_not_a_silent_off(
    rig: Rig, monkeypatch: pytest.MonkeyPatch, value: str
) -> None:
    rig.write_config(None)
    monkeypatch.setenv("TICKETCTL_READ_ONLY", value)
    code, env = rig.doc("comment", "DEMO-1", "--body", "x")
    assert (code, env["error"]["code"], env["error"]["reason"]) == (
        2,
        "invalid_config",
        "config_invalid",
    )
    assert "x" * 100 not in env["error"]["message"]  # the value is never echoed
    assert rig.fake.calls == []


@pytest.mark.parametrize("value", ["0", "false", "off", ""])
def test_the_environment_cannot_turn_a_config_true_off(
    rig: Rig, monkeypatch: pytest.MonkeyPatch, value: str
) -> None:
    monkeypatch.setenv("TICKETCTL_READ_ONLY", value)
    code, env = rig.doc("comment", "DEMO-1", "--body", "x")
    assert (code, env["error"]["code"]) == (20, "read_only")


@pytest.mark.parametrize(
    "flag", ["--no-read-only", "--read-only=false", "--read-only", "--writable", "--read_only=0"]
)
def test_no_flag_turns_it_off(rig: Rig, flag: str) -> None:
    code, env = rig.doc(flag, "comment", "DEMO-1", "--body", "x")
    assert code == 2 and env["error"]["code"] == "usage"
    assert rig.fake.calls == []
    code, env = rig.doc("comment", "DEMO-1", "--body", "x", flag)
    assert code == 2 and env["error"]["code"] == "usage"


def test_a_nested_or_non_boolean_config_value_is_refused(rig: Rig) -> None:
    for text in ("read_only: 'false'\n", "read_only: 0\n", "read_only: null\n", "read_only: []\n"):
        rig.config.write_text(
            text + f"sites:\n  main:\n    url: {rig.fake.url}\n    email: {SITE_EMAIL}\n"
            f"    token_env: {TOKEN_VAR}\n    allow_http: true\n"
        )
        code, env = rig.doc("validate")
        assert (code, env["error"]["code"]) == (2, "invalid_config"), text


def test_a_later_dropin_cannot_turn_a_config_true_off(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, rig: Rig
) -> None:
    home = tmp_path / "xdg"
    directory = home / "ticketctl"
    (directory / "config.d").mkdir(parents=True)
    site = (
        f"sites:\n  main:\n    url: {rig.fake.url}\n    email: {SITE_EMAIL}\n"
        f"    token_env: {TOKEN_VAR}\n    allow_http: true\n"
    )
    (directory / "config.yaml").write_text(f"read_only: true\n{site}")
    (directory / "config.d" / "zz.yaml").write_text("read_only: false\n")
    for file in (directory / "config.yaml", directory / "config.d" / "zz.yaml"):
        file.chmod(0o600)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(home))
    code = main(["--json", "comment", "DEMO-1", "--body", "hi"])
    env = json.loads(rig.capsys.readouterr().out)
    assert (code, env["error"]["code"]) == (20, "read_only")
    assert rig.fake.calls == []
    assert rig.side_effects() == []


def test_a_dropin_that_turns_it_on_wins_over_an_earlier_false(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    directory = tmp_path / "ticketctl"
    (directory / "config.d").mkdir(parents=True)
    (directory / "config.yaml").write_text("read_only: false\n")
    (directory / "config.d" / "zz.yaml").write_text("read_only: true\n")
    for file in (directory / "config.yaml", directory / "config.d" / "zz.yaml"):
        file.chmod(0o600)
    loaded = config.load_config(env={"XDG_CONFIG_HOME": str(tmp_path)})
    assert loaded.config.read_only is True
    only_false = tmp_path / "c.yaml"
    only_false.write_text("read_only: false\n")
    only_false.chmod(0o600)
    assert config.load_config(only_false, env={}).config.read_only is False


def test_the_environment_turns_on_what_the_file_leaves_off(tmp_path: Path) -> None:
    path = tmp_path / "c.yaml"
    path.write_text("read_only: false\n")
    path.chmod(0o600)
    off = config.load_config(path, env={})
    on = config.load_config(path, env={"TICKETCTL_READ_ONLY": "1"})
    assert (off.config.read_only, on.config.read_only) == (False, True)


# --- the engine entry and the registry ----------------------------------------------------------


def test_the_write_flow_entry_refuses_on_its_own(rig: Rig) -> None:
    from ticketctl import writeflow

    rt = load_runtime(Globals(config=str(rig.config), json=True))
    assert rt.config.read_only is True

    class Boom:
        name = "boom"

        def __getattr__(self, name: str) -> Any:  # any use of the action is a failure
            raise AssertionError(f"the action was touched: {name}")

    with pytest.raises(TicketctlError) as err:
        writeflow.execute(rt, "main", Boom())  # type: ignore[arg-type]
    assert err.value.code == "read_only"
    assert rig.fake.calls == []


def test_a_future_write_command_that_skips_the_write_flow_is_still_refused(
    rig: Rig, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A write registered tomorrow that talks to the gateway itself gets the refusal for free."""
    reached: list[str] = []

    def handler(g: Globals, a: argparse.Namespace) -> dict[str, Any]:
        reached.append("handler")
        rt = load_runtime(g)
        gw = open_gateway(rt, "main")
        gw.request("POST", "/rest/api/3/issue", template="/rest/api/3/issue", body={})
        return {"ok": True}

    spec = CommandSpec(("link", "add"), WRITE, "future", handler, lambda p: None)
    monkeypatch.setitem(commands.REGISTRY, ("link", "add"), spec)
    code, env = rig.doc("link", "add")
    assert (code, env["error"]["code"]) == (20, "read_only")
    assert reached == [] and rig.fake.calls == []


def test_a_write_command_with_a_wrong_kind_is_caught_by_the_gateway(
    rig: Rig, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A write mislabelled as a read passes the command layer; the gateway still refuses."""

    def handler(g: Globals, a: argparse.Namespace) -> dict[str, Any]:
        rt = load_runtime(g)
        gw = open_gateway(rt, "main")
        gw.request("PUT", "/rest/api/3/issue/DEMO-1", template="/rest/api/3/issue/{key}", body={})
        return {"ok": True}

    spec = CommandSpec(("sneaky",), commands.READ, "mislabelled", handler, lambda p: None)
    monkeypatch.setitem(commands.REGISTRY, ("sneaky",), spec)
    code, env = rig.doc("sneaky")
    assert (code, env["error"]["code"]) == (20, "read_only")
    assert rig.fake.calls == []


def test_the_codes_table_has_the_new_code_under_exit_20() -> None:
    spec = CODES["read_only"]
    assert spec.exit == 20 and spec.reason_is_code and not spec.retryable


# --- the gateway guard ---------------------------------------------------------------------------

READ_POSTS = [
    ("/rest/api/3/search/jql", ""),
    ("/rest/api/3/search/approximate-count", ""),
    ("/rest/api/3/jql/parse", "validation=strict"),
]


def test_gateway_refuses_every_write_method_before_sending(serve: Any) -> None:
    server = serve(json_reply("{}"))
    rig = make_rig(server.url, read_only=True)
    for method in ("POST", "PUT", "DELETE"):
        with pytest.raises(GatewayError) as err:
            rig.gw.request(method, "/rest/api/3/issue", template="/rest/api/3/issue", body={"a": 1})
        assert err.value.code == "read_only"
    assert server.requests == []
    assert rig.logs == []  # nothing was attempted, nothing is logged


def test_gateway_refuses_a_write_even_when_the_caller_says_it_is_a_read(serve: Any) -> None:
    server = serve(json_reply("{}"))
    rig = make_rig(server.url, read_only=True)
    with pytest.raises(GatewayError) as err:
        rig.gw.request(
            "POST", "/rest/api/3/issue", template="/rest/api/3/issue", body={}, write=False
        )
    assert err.value.code == "read_only"
    assert server.requests == []


@pytest.mark.parametrize(
    "path",
    [
        "/rest/api/3/search/jql/extra",
        "/rest/api/3/search",
        "/rest/api/3/search/jql/",
        "/rest/api/3/jql/parse/x",
        "/rest/api/3/issue/DEMO-1/comment",
        "/rest/api/3/SEARCH/jql",
        "/rest/api/3/search/jql%2f..",
    ],
)
def test_gateway_allows_only_the_exact_read_post_paths(serve: Any, path: str) -> None:
    server = serve(json_reply("{}"))
    rig = make_rig(server.url, read_only=True)
    with pytest.raises(GatewayError) as err:
        rig.gw.request("POST", path, template=path.split("%")[0], body={})
    assert err.value.code == "read_only"
    assert server.requests == []


@pytest.mark.parametrize(("path", "query"), READ_POSTS)
def test_gateway_still_sends_the_documented_read_posts(serve: Any, path: str, query: str) -> None:
    server = serve(json_reply("{}"))
    rig = make_rig(server.url, read_only=True)
    response = rig.gw.request(
        "POST", path, template=path, query=query, body={"jql": "a"}, write=False
    )
    assert response.status == 200
    assert [r.method for r in server.requests] == ["POST"]


def test_gateway_still_sends_gets(serve: Any) -> None:
    server = serve(json_reply("{}"))
    rig = make_rig(server.url, read_only=True)
    assert rig.get().status == 200


def test_gateway_off_by_default_sends_writes(serve: Any) -> None:
    server = serve(json_reply("{}", status=201))
    rig = make_rig(server.url)
    rig.post()
    assert [r.method for r in server.requests] == ["POST"]


def test_open_gateway_wires_the_mode_from_the_config(rig: Rig) -> None:
    rt = load_runtime(Globals(config=str(rig.config)))
    gw = open_gateway(rt, "main")
    with pytest.raises(GatewayError) as err:
        gw.request("POST", "/rest/api/3/issue", template="/rest/api/3/issue", body={})
    assert err.value.code == "read_only"
    assert rig.fake.calls == []
