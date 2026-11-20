"""Shared fixtures: the fake provider and a gateway pointed at it (issue #10)."""

from __future__ import annotations

from collections.abc import Callable, Iterator

import pytest

from tests.fake_provider import SITE_EMAIL, SITE_TOKEN, FakeProvider, Quirks
from ticketctl.gateway import Deadline, Gateway, Target


def make_target(fake: FakeProvider, **kw: object) -> Target:
    """A gateway target for the fake: loopback http, the fake's credentials, short timeouts."""
    args: dict[str, object] = {
        "name": "fake",
        "url": fake.url,
        "email": SITE_EMAIL,
        "token": SITE_TOKEN,
        "connect_timeout": 2.0,
        "read_timeout": 5.0,
        "retry_after_max": 10.0,
        "allow_http": True,
    }
    args.update(kw)
    return Target(**args)  # type: ignore[arg-type]


def make_gateway(fake: FakeProvider, *, seconds: float = 20.0, **kw: object) -> Gateway:
    """A gateway with a no-op sleep (retries do not wait) and an empty environment."""
    target_kw = {k: v for k, v in kw.items() if k in {"email", "token", "read_timeout"}}
    gw_kw = {k: v for k, v in kw.items() if k not in target_kw}
    gw_kw.setdefault("sleep", lambda _s: None)
    gw_kw.setdefault("env", {})
    return Gateway(make_target(fake, **target_kw), deadline=Deadline(seconds), **gw_kw)  # type: ignore[arg-type]


@pytest.fixture
def fake() -> Iterator[FakeProvider]:
    with FakeProvider() as provider:
        yield provider


@pytest.fixture
def fake_factory() -> Iterator[Callable[[Quirks | None], FakeProvider]]:
    """Fakes with chosen quirks; every one started here is closed at teardown."""
    started: list[FakeProvider] = []

    def build(quirks: Quirks | None = None) -> FakeProvider:
        provider = FakeProvider(quirks).start()
        started.append(provider)
        return provider

    yield build
    for provider in started:
        provider.close()
