"""Marker and skip logic of the live suites (issues #37 and #38), and their fixtures.

``test_live_read.py`` and ``test_live_write.py`` are marked ``live`` here (the root configuration
deselects that marker) and skipped unless ``TICKETCTL_LIVE=1`` is set; the write suite also needs
``TICKETCTL_LIVE_WRITE=1`` and a named test project. ``test_harness.py`` is an ordinary test: it
proves this logic against the fake provider and needs no flag.

The fixtures never print the environment. The state directory is private to a run, so the
request log it holds is exactly this run's requests.
"""

from __future__ import annotations

import os
import sys
from collections.abc import Iterator
from pathlib import Path

import pytest

from tests.live.support import (
    Budget,
    LiveCli,
    LiveEnv,
    Recorder,
    Redactor,
    default_findings_dir,
    masked_env_values,
    skip_reason,
)

LIVE_MODULES = {"test_live_read.py": False, "test_live_write.py": True}  # name -> writes
READ_LIMIT = 70
WRITE_LIMIT = 130
CLEANUP_RESERVE = 25


def _module_writes(item: pytest.Item) -> bool | None:
    return LIVE_MODULES.get(Path(str(item.fspath)).name)


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    for item in items:
        writes = _module_writes(item)
        if writes is None:
            continue
        item.add_marker(pytest.mark.live)
        reason = skip_reason(os.environ, write=writes)
        if reason is not None:
            item.add_marker(pytest.mark.skip(reason=reason))


@pytest.fixture(autouse=True)
def _stop_when_the_budget_is_gone(request: pytest.FixtureRequest) -> Iterator[None]:
    """Once the request cap was hit nothing else runs: the session exits (cleanup still ran)."""
    if _module_writes(request.node) is None:
        yield
        return
    cap = request.session.stash.get(_BUDGET_KEY, None)
    if cap is not None and cap.aborted:
        pytest.exit("the live suite hit its request cap and stopped", returncode=3)
    yield


_BUDGET_KEY = pytest.StashKey[Budget]()


def _gate(*, write: bool) -> None:
    """The fixtures re-check the gate: a renamed or copied module cannot bypass the collection hook."""
    reason = skip_reason(os.environ, write=write)
    if reason is not None:
        pytest.skip(reason)


def _build(
    tmp_path_factory: pytest.TempPathFactory,
    suite: str,
    limit: int,
    session: pytest.Session,
    *,
    read_only: bool,
) -> tuple[LiveEnv, LiveCli, Recorder, Budget]:
    live = LiveEnv.from_env(os.environ)
    state = tmp_path_factory.mktemp(f"live-state-{suite}")
    budget = Budget(state, limit=limit, reserve=CLEANUP_RESERVE)
    session.stash[_BUDGET_KEY] = budget
    cli = LiveCli(
        env=os.environ,
        state_home=state,
        budget=budget,
        config=live.config,
        site=live.site,
        pass_env=live.child_env_names(),
        read_only=read_only,
    )
    redactor = Redactor([*live.literals(), *masked_env_values(os.environ, live.child_env_names())])
    recorder = Recorder(suite, default_findings_dir(os.environ), redactor)
    return live, cli, recorder, budget


@pytest.fixture(scope="module")
def read_world(
    request: pytest.FixtureRequest, tmp_path_factory: pytest.TempPathFactory
) -> Iterator[tuple[LiveEnv, LiveCli, Recorder]]:
    _gate(write=False)
    live, cli, recorder, budget = _build(
        tmp_path_factory, "read", READ_LIMIT, request.session, read_only=True
    )
    yield live, cli, recorder
    recorder.requests = budget.used()
    path = recorder.write()
    sys.stderr.write(recorder.summary() + f"\nfindings file: {path}\n")


@pytest.fixture(scope="module")
def write_world(
    request: pytest.FixtureRequest, tmp_path_factory: pytest.TempPathFactory
) -> Iterator[tuple[LiveEnv, LiveCli, Recorder, object]]:
    from tests.live.support import Ledger, run_marker

    _gate(write=True)
    live, cli, recorder, budget = _build(
        tmp_path_factory, "write", WRITE_LIMIT, request.session, read_only=False
    )
    ledger = Ledger(cli, live, run_marker())
    try:
        yield live, cli, recorder, ledger
    finally:
        # The safety net behind every test's own finally: close whatever is still open.
        outcome = ledger.close_all()
        recorder.record("W20", "closer outcome per ticket (counts)", sorted(outcome.values()))
        recorder.requests = budget.used()
        path = recorder.write()
        sys.stderr.write(recorder.summary() + f"\nfindings file: {path}\n")
        left = ledger.still_open()
        if left:
            sys.stderr.write(
                "TEST TICKETS LEFT OPEN OR UNSETTLED, close them by hand: " + "; ".join(left) + "\n"
            )
