"""``create`` dedupe through real ticketctl processes (the acceptance of issue #32).

The fake provider runs in the test process; every command is a real ``python -m ticketctl`` child
with a private home and state directory. The guard waits (2 s and 5 s) are real time. The fake's
label index lags by its own clock, which only a write moves, so a test decides when a hidden label
becomes visible: at the Nth search (deterministic) or on a ticker thread (the race).
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import time
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from tests.fake_provider import ME_ACCOUNT, SITE_EMAIL, SITE_TOKEN, FakeProvider, Fault
from ticketctl import audit, createcmd

TOKEN_VAR = "TICKETCTL_TEST_SITE_ENV"
ARGS = ["--project", "DEMO", "--type", "Task", "--title", "Raced", "--dedupe-key", "race1"]


class World:
    def __init__(self, root: Path, fake: FakeProvider) -> None:
        self.root, self.fake = root, fake
        self.state = root / "state"
        (root / "home").mkdir()
        self.config = root / "config.yaml"
        self.config.write_text(
            f"sites:\n  main:\n    url: {fake.url}\n    email: {SITE_EMAIL}\n"
            f"    token_env: {TOKEN_VAR}\n    allow_http: true\n"
        )
        self.config.chmod(0o600)
        self.env = {
            "HOME": str(root / "home"),
            "XDG_CONFIG_HOME": str(root / "xdg"),
            "XDG_CACHE_HOME": str(root / "cache"),
            "XDG_STATE_HOME": str(self.state),
            "PATH": "",
            "PYTHONPATH": os.pathsep.join(sys.path),
            TOKEN_VAR: SITE_TOKEN,
        }

    def start(self, *argv: str) -> subprocess.Popen[str]:
        return subprocess.Popen(  # noqa: S603 - the interpreter running the tests
            [sys.executable, "-m", "ticketctl", "--config", str(self.config), "--json", *argv],
            env=self.env,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )

    def finish(self, proc: subprocess.Popen[str]) -> tuple[int, dict[str, Any]]:
        out, err = proc.communicate(timeout=120)
        assert SITE_TOKEN not in out + err
        return proc.returncode, json.loads(out)

    def run(self, *argv: str) -> tuple[int, dict[str, Any]]:
        return self.finish(self.start(*argv))

    def plan_hash(self, *args: str) -> str:
        code, plan = self.run("--dry-run", "create", *args)
        assert code == 0, plan
        return str(plan["plan_sha256"])

    def create(self, held: str, *args: str) -> subprocess.Popen[str]:
        return self.start("--yes", "--expect-plan", held, "create", *args)

    def posts(self) -> list[Any]:
        return [c for c in self.fake.calls if c.method == "POST" and c.path == "/rest/api/3/issue"]

    def searches(self) -> int:
        return sum(1 for c in self.fake.calls if c.path == "/rest/api/3/search/jql")


@pytest.fixture
def world(tmp_path: Path, fake: FakeProvider) -> World:
    return World(tmp_path, fake)


def advance_at_search(fake: FakeProvider, n: int) -> None:
    """Move the fake's clock 10 s on when the Nth search arrives, so a hidden label shows."""
    real = fake._search

    def counted(call: Any) -> Any:
        if sum(1 for c in fake.calls if c.path == "/rest/api/3/search/jql") == n:
            fake.advance(10)
        return real(call)

    fake._search = counted  # type: ignore[method-assign]
    fake._routes = []
    fake._build_routes()


@pytest.fixture
def ticker(fake: FakeProvider) -> Iterator[None]:
    """Moves the fake's clock on every 0.3 s of real time (labels become visible soon)."""
    stop = threading.Event()

    def tick() -> None:
        while not stop.wait(0.3):
            fake.advance(2)

    thread = threading.Thread(target=tick, daemon=True)
    thread.start()
    yield
    stop.set()
    thread.join(5)


def test_two_real_processes_with_one_key_create_one_ticket(world: World, ticker: None) -> None:
    held = world.plan_hash(*ARGS)
    first, second = world.create(held, *ARGS), world.create(held, *ARGS)
    results = [world.finish(first), world.finish(second)]
    assert [code for code, _ in results] == [0, 0], results
    assert sorted(r["result"] for _, r in results) == ["ok", "unchanged"]
    reused = next(r for _, r in results if r["result"] == "unchanged")
    made = next(r for _, r in results if r["result"] == "ok")
    assert reused["reused"] is True and reused["key"] == made["key"]
    assert len(world.posts()) == 1
    assert sum(1 for t in world.fake.tickets.values() if t.summary == "Raced") == 1


def test_a_rerun_just_after_a_create_waits_the_two_second_quick_rerun(world: World) -> None:
    held = world.plan_hash(*ARGS)
    code, made = world.finish(world.create(held, *ARGS))
    assert code == 0 and made["result"] == "ok"
    # The label is hidden until the fake's clock moves; it moves at the 6th search, which is the
    # re-run's lookup after the guard's 2 s wait (searches: 1 the dry run, 2-3 the first run,
    # 4-5 the second run's own lookups).
    advance_at_search(world.fake, 6)
    begun = time.monotonic()
    code, again = world.finish(world.create(held, *ARGS))
    took = time.monotonic() - begun
    assert code == 0 and again["result"] == "unchanged" and again["reused"] is True
    assert again["key"] == made["key"] and len(world.posts()) == 1
    assert world.searches() == 6
    assert took >= 2.0


def test_a_rerun_after_an_unknown_create_polls_for_five_seconds(world: World) -> None:
    held = world.plan_hash(*ARGS)
    label = createcmd.CreateAction("DEMO", "Task", dedupe_key="race1").label("main")
    landed = world.fake.add_ticket("DEMO-70", "Raced", labels=[label])
    landed.reporter = ME_ACCOUNT
    landed.labels = {label: world.fake.now + timedelta(seconds=5)}  # hidden from search for now
    audit.append_audit(
        world.state / "ticketctl",
        {
            "attempt_id": "feedfacefeedface", "step": 1, "step_kind": "create",
            "actor": "auto:unknown", "actor_source": "default", "stdin_tty": False,
            "decision_id": [], "target": {"site": "main", "key": None}, "action": "create",
            "inputs": {
                "plan_sha256": held, "dedupe_key": "race1", "override_escalation": None,
                "config": {"path": None, "files_sha256": [], "sha256": "b" * 64},
                "hooks_sha256": "c" * 64, "site_host": "example.test", "cli_hooks": [],
                "token_env": TOKEN_VAR,
            },
            "result": "unknown", "provider_ids": [], "changed": None,
        },
        now=datetime.now(UTC) - timedelta(seconds=1),
    )  # fmt: skip
    # Searches: 1 the dry run, 2 lock-free, 3 under the lock (the guard sees the unknown record),
    # 4 after 5 s.
    advance_at_search(world.fake, 4)
    begun = time.monotonic()
    code, result = world.finish(world.create(held, *ARGS))
    took = time.monotonic() - begun
    assert code == 0 and result["result"] == "unchanged" and result["key"] == "DEMO-70"
    assert world.posts() == [] and world.searches() == 4
    assert took >= 5.0


def test_an_unknown_create_then_a_real_rerun_finds_the_landed_ticket(world: World) -> None:
    world.fake.inject(Fault("truncated", method="POST", path="/issue$"))
    held = world.plan_hash(*ARGS)
    code, env = world.finish(world.create(held, *ARGS))
    assert (code, env["error"]["code"]) == (12, "outcome_unknown")
    assert env["error"]["retryable"] is True and len(world.posts()) == 1
    # The ticket landed with a hidden label; the re-run's guard polls until the clock moves on.
    advance_at_search(world.fake, world.searches() + 3)
    code, result = world.finish(world.create(held, *ARGS))
    assert code == 0 and result["reused"] is True and len(world.posts()) == 1
    assert sum(1 for t in world.fake.tickets.values() if t.summary == "Raced") == 1
