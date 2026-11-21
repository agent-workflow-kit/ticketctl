"""The dedupe lock and the create lag guard inside the write flow (issue #32).

``StubCreate`` (tests/write_stub.py) stands in for the create command of #31: it searches the
``tc-`` label on the fake provider, whose label index lags a write (``label_lag_s``) on the fake's
own clock. The guard's clock here is virtual: a sleep advances the fake's clock and an offset on
the wall clock, so a 60 s poll takes no real time.
"""

from __future__ import annotations

import contextlib
import fcntl
import hashlib
import multiprocessing
import os
import signal
import threading
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from tests import dedupe_workers
from tests.fake_provider import FakeProvider
from tests.test_writeflow import Hooks, World
from tests.write_stub import StubComment, StubCreate
from ticketctl import audit, dedupe
from ticketctl.dedupe import BucketLock, Clock
from ticketctl.errors import TicketctlError
from ticketctl.writeflow import Seams

SPAWN = multiprocessing.get_context("spawn")


@pytest.fixture
def world(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fake: FakeProvider) -> World:
    return World(tmp_path, monkeypatch, fake)


class Virtual:
    """Real time plus an offset; ``sleep`` moves the offset and the fake's clock together."""

    def __init__(self, fake: FakeProvider) -> None:
        self.fake = fake
        self.offset = 0.0
        self.waits: list[float] = []
        self._lock = threading.Lock()

    def clock(self) -> Clock:
        return Clock(wall=self.wall, sleep=self.sleep)

    def wall(self) -> float:
        return time.time() + self.offset

    def sleep(self, seconds: float) -> None:
        with self._lock:
            self.waits.append(seconds)
            self.offset += seconds
            self.fake.advance(seconds)


def ghost_record(world: World, key: str, result: str, age_s: float) -> None:
    """An audit record of an earlier same-key create run that is ``age_s`` old."""
    audit.append_audit(
        world.state,
        {
            "attempt_id": "feedfacefeedface", "step": 1, "step_kind": "create",
            "actor": "human:tester", "actor_source": "flag", "stdin_tty": False,
            "decision_id": [], "target": {"site": "main", "key": None}, "action": "create",
            "inputs": {
                "plan_sha256": "a" * 64, "dedupe_key": key, "override_escalation": None,
                "config": {"path": None, "files_sha256": [], "sha256": "b" * 64},
                "hooks_sha256": "c" * 64, "site_host": "example.test", "cli_hooks": [],
                "token_env": "TEST_TOKEN",
            },
            "result": result, "provider_ids": [], "changed": None,
        },
        now=datetime.fromtimestamp(time.time() - age_s, UTC),
    )  # fmt: skip


def creates(world: World) -> list[Any]:
    return [c for c in world.fake.calls if c.method == "POST" and c.path == "/rest/api/3/issue"]


def run_create(world: World, action: StubCreate, **kw: Any) -> dict[str, Any]:
    held = world.hash_of(StubCreate(action.title, dedupe_key=action.dedupe_key))
    return world.run(action, yes=True, expect_plan=held, **kw)


def locked_probe(world: World, name: str) -> bool:
    """True when the bucket is free right now (a raw non-blocking flock on a second descriptor)."""
    path = world.state / "locks" / name
    try:
        fd = os.open(path, os.O_RDONLY)
    except FileNotFoundError:
        return True
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        return False
    finally:
        os.close(fd)
    return True


# --- the lag guard against the fake's label lag -------------------------------------------------


def test_the_fake_really_lags(world: World) -> None:
    """The fixture the guard is tested against: right after a create the label is not found."""
    out = world.run(StubCreate("t", dedupe_key="lag"), yes=True,
                    expect_plan=world.hash_of(StubCreate("t", dedupe_key="lag")))  # fmt: skip
    assert out["result"] == "ok"
    probe = StubCreate("t", dedupe_key="lag")
    plan = world.run(probe, dry_run=True)
    assert plan["steps"] and plan["dedupe_match"] is None  # the index does not show it yet
    world.fake.advance(2)
    assert world.run(StubCreate("t", dedupe_key="lag"), dry_run=True)["steps"] == []


def test_a_quick_rerun_does_not_create_a_second_ticket(world: World, fake: FakeProvider) -> None:
    v = Virtual(fake)
    first = run_create(world, StubCreate("t", dedupe_key="k1", clock=v.clock()))
    assert first["result"] == "ok" and first["key"] is not None
    second = run_create(world, StubCreate("t", dedupe_key="k1", clock=v.clock()))
    assert second["result"] == "unchanged" and second["reused"] is True
    assert second["key"] == first["key"]
    assert len(creates(world)) == 1
    assert v.waits == [2.0]  # the quick re-run repeated once after 2 s


def test_without_the_guard_the_lag_would_create_twice(world: World, fake: FakeProvider) -> None:
    """The negative control: a guard that never repeats the lookup reproduces the duplicate."""
    world.mp.setattr(dedupe, "run_guard", lambda lookup, decision, **kw: lookup())
    run_create(world, StubCreate("t", dedupe_key="k2"))
    run_create(world, StubCreate("t", dedupe_key="k2"))
    assert len(creates(world)) == 2


def test_two_concurrent_creators_with_one_key_make_one_ticket(
    world: World, fake: FakeProvider
) -> None:
    v = Virtual(fake)
    held = world.hash_of(StubCreate("t", dedupe_key="race"))
    results: list[Any] = []
    gate = threading.Barrier(2)

    def one() -> None:
        action = StubCreate("t", dedupe_key="race", clock=v.clock())
        gate.wait(10)
        try:
            results.append(world.run(action, yes=True, expect_plan=held))
        except Exception as exc:  # noqa: BLE001 - reported by the assertion below
            results.append(exc)

    threads = [threading.Thread(target=one) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(60)
    assert all(isinstance(r, dict) for r in results), results
    assert sorted(r["result"] for r in results) == ["ok", "unchanged"]
    assert len(creates(world)) == 1


@pytest.mark.parametrize("round_", range(3))
def test_the_concurrent_race_repeats_cleanly(round_: int, world: World, fake: FakeProvider) -> None:
    test_two_concurrent_creators_with_one_key_make_one_ticket(world, fake)


def test_rerun_thirty_seconds_after_an_unknown_record_polls_instead_of_creating(
    world: World, fake: FakeProvider
) -> None:
    """The create landed but its answer was lost (``unknown``); the label shows up 20 s later."""
    v = Virtual(fake)
    label = StubCreate("t", dedupe_key="unk").label("main")
    ghost = fake.add_ticket("DEMO-9", "t", labels=[])
    ghost.labels[label] = fake.now + timedelta(seconds=20)  # indexed 20 s from now
    ghost_record(world, "unk", "unknown", 30)
    out = run_create(world, StubCreate("t", dedupe_key="unk", clock=v.clock()))
    assert out["result"] == "unchanged" and out["key"] == "DEMO-9"
    assert creates(world) == []
    assert v.waits == [5.0] * 4  # polled every 5 s until the index showed it


def test_an_attempted_record_without_outcome_also_polls(world: World, fake: FakeProvider) -> None:
    """A crashed run that left only ``attempted`` (the ticket may exist) is not retried blind."""
    v = Virtual(fake)
    ghost_record(world, "att", "attempted", 10)
    out = run_create(world, StubCreate("t", dedupe_key="att", clock=v.clock()))
    # nothing ever shows up: the poll runs its full window (to 60 s after the record), then creates
    assert out["result"] == "ok" and len(creates(world)) == 1
    assert 45 <= sum(v.waits) <= 55 and max(v.waits) <= 5.0


def test_a_poll_that_does_not_fit_the_timeout_sends_nothing(
    world: World, fake: FakeProvider
) -> None:
    v = Virtual(fake)
    ghost_record(world, "slow", "unknown", 10)
    held = world.hash_of(StubCreate("t", dedupe_key="slow"))
    err = world.fail(
        StubCreate("t", dedupe_key="slow", clock=v.clock()),
        yes=True, expect_plan=held, timeout_s=1.0,
    )  # fmt: skip
    assert (err.exit, err.code) == (12, "timeout")
    assert creates(world) == [] and v.waits == []
    assert [r["result"] for r in world.records()] == ["unknown"]  # only the ghost: no new record


# --- the lock inside the engine ---------------------------------------------------------------


def test_a_held_lock_past_the_timeout_is_exit_12_unavailable_locked(world: World) -> None:
    name = dedupe.bucket_name("main", "DEMO", "busy")
    with BucketLock(world.state, name, timeout=1.0):
        pass  # creates the directory and the file
    locked = SPAWN.Event()
    proc = SPAWN.Process(
        target=dedupe_workers.hold_bucket, args=(str(world.state), name, locked, 120.0)
    )
    proc.start()
    try:
        assert locked.wait(30)
        held = world.hash_of(StubCreate("t", dedupe_key="busy"))
        err = world.fail(StubCreate("t", dedupe_key="busy"), yes=True, expect_plan=held,
                         timeout_s=1.5)  # fmt: skip
        assert (err.exit, err.code, err.reason) == (12, "unavailable", "locked")
        assert err.retryable is True
        assert creates(world) == [] and world.records() == []  # nothing sent, nothing audited
    finally:
        assert proc.pid is not None
        os.kill(proc.pid, signal.SIGKILL)  # the exact pid started above
        proc.join(30)
    # the holder died: the same command now goes through
    out = world.run(StubCreate("t", dedupe_key="busy"), yes=True, expect_plan=held)
    assert out["result"] == "ok"


def test_the_lock_is_held_to_the_outcome_and_free_before_the_post_hook(world: World) -> None:
    name = dedupe.bucket_name("main", "DEMO", "order")
    seen: dict[str, Any] = {}
    hooks = Hooks()

    def post(plan: Any, planned: Any, attempt: str, ids: Any, info: Any) -> None:
        seen["free_in_post"] = locked_probe(world, name)

    hooks.post = post  # type: ignore[method-assign]
    real = audit.append_audit

    def spy(state_dir: Path, record: Any, **kw: Any) -> Any:
        seen.setdefault("held_at_" + record["result"], not locked_probe(world, name))
        return real(state_dir, record, **kw)

    world.mp.setattr(audit, "append_audit", spy)
    held = world.hash_of(StubCreate("t", dedupe_key="order"))
    world.run(StubCreate("t", dedupe_key="order"), yes=True, expect_plan=held,
              seams=Seams(hooks=hooks))  # fmt: skip
    assert seen == {"held_at_attempted": True, "held_at_ok": True, "free_in_post": True}


def test_a_post_hook_calling_ticketctl_with_the_same_key_does_not_deadlock(
    world: World, fake: FakeProvider
) -> None:
    v = Virtual(fake)
    hooks = Hooks()
    inner: list[Any] = []

    def post(plan: Any, planned: Any, attempt: str, ids: Any, info: Any) -> None:
        # the same process, the same key, from inside the hook: the lock must be free by now
        inner.append(
            world.run(
                StubCreate("t", dedupe_key="hooked", clock=v.clock()),
                yes=True,
                expect_plan=plan["plan_sha256"],
                timeout_s=5.0,
            )  # fmt: skip
        )

    hooks.post = post  # type: ignore[method-assign]
    held = world.hash_of(StubCreate("t", dedupe_key="hooked"))
    out = world.run(StubCreate("t", dedupe_key="hooked", clock=v.clock()), yes=True,
                    expect_plan=held, seams=Seams(hooks=hooks))  # fmt: skip
    assert out["result"] == "ok"
    assert inner and inner[0]["result"] == "unchanged" and inner[0]["reused"] is True
    assert len(creates(world)) == 1


def test_pre_hook_time_longer_than_the_timeout_does_not_expire_the_lock_budget(
    world: World,
) -> None:
    """The deadline does not run during the ``pre-`` hooks: the lock and the guard keep the time."""
    hooks = Hooks()
    hooks.before = lambda: time.sleep(1.4)
    held = world.hash_of(StubCreate("t", dedupe_key="paused"))
    out = world.run(StubCreate("t", dedupe_key="paused"), yes=True, expect_plan=held,
                    timeout_s=1.0, seams=Seams(hooks=hooks))  # fmt: skip
    assert out["result"] == "ok" and len(creates(world)) == 1


def test_no_dedupe_key_takes_no_lock_and_makes_no_lock_files(world: World) -> None:
    world.run(StubCreate("t", dedupe_key=None), yes=True,
              expect_plan=world.hash_of(StubCreate("t", dedupe_key=None)))  # fmt: skip
    assert not (world.state / "locks").exists()


def test_a_dry_run_takes_no_lock(world: World) -> None:
    world.run(StubCreate("t", dedupe_key="dry"), dry_run=True)
    assert not (world.state / "locks").exists()


@pytest.fixture
def probe_name() -> str:
    return dedupe.bucket_name("main", "DEMO", "exit")


def run_and_probe(world: World, action: Any, probe_name: str, **kw: Any) -> bool:
    """Run ``action`` (expecting whatever it does), then say whether its bucket is free."""
    with contextlib.suppress(BaseException):
        world.run(action, **kw)
    return locked_probe(world, probe_name)


class Failing(StubCreate):
    """A create whose request raises ``exc`` just before it is sent."""

    def __init__(self, exc: BaseException) -> None:
        def boom() -> None:
            raise exc

        super().__init__("t", dedupe_key="exit", on_send=boom)


def test_the_lock_is_free_after_ok(world: World, probe_name: str) -> None:
    held = world.hash_of(StubCreate("t", dedupe_key="exit"))
    assert run_and_probe(world, StubCreate("t", dedupe_key="exit"), probe_name,
                         yes=True, expect_plan=held)  # fmt: skip


def test_the_lock_is_free_after_an_unexpected_exception(world: World, probe_name: str) -> None:
    held = world.hash_of(StubCreate("t", dedupe_key="exit"))
    action = Failing(RuntimeError("boom"))
    assert run_and_probe(world, action, probe_name, yes=True, expect_plan=held)
    assert [r["result"] for r in world.records()] == ["attempted", "unknown"]


def test_the_lock_is_free_after_an_interrupt(world: World, probe_name: str) -> None:
    held = world.hash_of(StubCreate("t", dedupe_key="exit"))
    with pytest.raises(KeyboardInterrupt):
        world.run(Failing(KeyboardInterrupt()), yes=True, expect_plan=held)
    assert locked_probe(world, probe_name)


def test_the_lock_is_free_after_a_provider_refusal(world: World, probe_name: str) -> None:
    held = world.hash_of(StubCreate("t", dedupe_key="exit", issue_type="Nope"))
    action = StubCreate("t", dedupe_key="exit", issue_type="Nope")
    err = world.fail(action, yes=True, expect_plan=held)
    assert err.code == "rejected"
    assert locked_probe(world, probe_name)


def test_the_lock_is_free_after_the_early_unchanged_path(
    world: World, fake: FakeProvider, probe_name: str
) -> None:
    """A High found in review of #23: the no-steps return under the lock must release."""
    v = Virtual(fake)
    held = world.hash_of(StubCreate("t", dedupe_key="exit"))
    hooks = Hooks()
    label = StubCreate("t", dedupe_key="exit").label("main")
    # the ticket appears while the hooks run: the fresh lookup under the lock finds it
    hooks.before = lambda: fake.add_ticket("DEMO-9", "t", labels=[label])
    out = world.run(StubCreate("t", dedupe_key="exit", clock=v.clock()), yes=True,
                    expect_plan=held, seams=Seams(hooks=hooks))  # fmt: skip
    assert out["result"] == "unchanged" and out["key"] == "DEMO-9"
    assert locked_probe(world, probe_name)
    assert world.records() == [] and creates(world) == []


def test_a_hooks_refusal_never_takes_the_lock(world: World) -> None:
    hooks = Hooks()
    hooks.veto = TicketctlError("vetoed", "no")
    held = world.hash_of(StubCreate("t", dedupe_key="veto"))
    err = world.fail(StubCreate("t", dedupe_key="veto"), yes=True, expect_plan=held,
                     seams=Seams(hooks=hooks))  # fmt: skip
    assert err.code == "vetoed"
    assert not (world.state / "locks").exists()


def test_a_lock_that_cannot_release_is_a_warning_and_the_result_survives(
    world: World, capsys: pytest.CaptureFixture[str]
) -> None:
    original = BucketLock.__exit__

    def bad_exit(self: BucketLock, *exc: object) -> None:
        original(self, *exc)
        from ticketctl.state import StateError

        raise StateError("state_io", "cannot update the lock file time")

    world.mp.setattr(BucketLock, "__exit__", bad_exit)
    held = world.hash_of(StubCreate("t", dedupe_key="warn"))
    out = world.run(StubCreate("t", dedupe_key="warn"), yes=True, expect_plan=held)
    assert out["result"] == "ok"
    assert "the lock could not be released" in capsys.readouterr().err


def test_comment_with_a_key_takes_the_default_lock_on_its_own_scope(world: World) -> None:
    held = world.hash_of(StubComment("DEMO-1", "hi", dedupe_key="c1"))
    world.run(StubComment("DEMO-1", "hi", dedupe_key="c1"), yes=True, expect_plan=held)
    want = dedupe.bucket_name("main", "DEMO-1", "c1")
    assert (world.state / "locks" / want).exists()
    assert len(list((world.state / "locks").iterdir())) == 1


def test_many_keys_never_exceed_256_lock_files(world: World) -> None:
    for i in range(300):
        name = dedupe.bucket_name("main", "DEMO", f"many-{i}")
        with BucketLock(world.state, name, timeout=1.0):
            pass
    assert len(list((world.state / "locks").iterdir())) <= 256


def test_the_lock_name_is_the_documented_hash_prefix() -> None:
    raw = b"main\0DEMO\0some-key"
    assert dedupe.bucket_name("main", "DEMO", "some-key") == hashlib.sha256(raw).hexdigest()[:2]
