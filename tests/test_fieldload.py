"""Fetching and caching the field, priority and resolution lists (issue #12), with the fake."""

from __future__ import annotations

import json
import os
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from tests.conftest import make_gateway
from tests.fake_provider import (
    F_ESTIMATE,
    F_POINTS,
    F_SPRINT,
    PRIORITIES,
    RESOLUTIONS,
    SITE_EMAIL,
    SITE_TOKEN,
    FakeProvider,
    Fault,
)
from ticketctl import cache, fieldload, sanitize
from ticketctl.config import load_config
from ticketctl.errors import TicketctlError
from ticketctl.fieldmap import Status
from ticketctl.flags import Globals
from ticketctl.gateway import GatewayError
from ticketctl.runtime import Runtime
from ticketctl.state import StateError

FIELDS = "/rest/api/3/field"
NS = "com.atlassian.jira.plugin.system.customfieldtypes"


@pytest.fixture(autouse=True)
def _scrubber_reset() -> Any:
    sanitize.configure_scrubber([])
    yield
    sanitize.configure_scrubber([])


class Harness:
    def __init__(self, root: Path, fake: FakeProvider, warnings: list[str]) -> None:
        self.root = root
        self.fake = fake
        self.warnings = warnings
        self.state = root / "state" / "ticketctl"
        self.gw = make_gateway(fake)

    def runtime(self, site_yaml: str = "") -> Runtime:
        config = self.root / "config.yaml"
        config.write_text(
            "sites:\n  main:\n"
            f"    url: {self.fake.url}\n    allow_http: true\n    email: {SITE_EMAIL}\n"
            f"    token_env: TICKETCTL_TEST_TOKEN\n{site_yaml}",
            encoding="utf-8",
        )
        config.chmod(0o600)
        env = {"TICKETCTL_TEST_TOKEN": SITE_TOKEN, "XDG_STATE_HOME": str(self.root / "state")}
        loaded = load_config(config, env=env)
        rt = Runtime(g=Globals(), loaded=loaded, env=env, state_dir=self.state)
        rt.warn = self.warnings.append  # type: ignore[method-assign]
        return rt

    def cache_file(self, name: str = "fields") -> Path:
        return self.state / "cache" / "main" / f"{name}.json"

    def age_cache(self, seconds: int, name: str = "fields") -> None:
        """Rewrite the cache header so the file looks fetched ``seconds`` ago."""
        doc = json.loads(self.cache_file(name).read_text())
        stamp = datetime.now(UTC) - timedelta(seconds=seconds)
        doc["fetched_at"] = stamp.strftime("%Y-%m-%dT%H:%M:%SZ")
        self.cache_file(name).write_text(json.dumps(doc))

    def field_calls(self) -> int:
        return sum(1 for c in self.fake.calls if c.path == FIELDS)


@pytest.fixture
def h(tmp_path: Path, fake: FakeProvider) -> Harness:
    return Harness(tmp_path, fake, [])


def calls(fake: FakeProvider, path: str) -> int:
    return sum(1 for c in fake.calls if c.path == path)


# --- the field list ---------------------------------------------------------------------------


def test_fetch_then_cache_then_refresh(h: Harness) -> None:
    rt = h.runtime()
    first = fieldload.load_fields(rt, "main", h.gw, refresh=False)
    assert first.source == "provider"
    assert F_POINTS in {f.id for f in first.fields}
    second = fieldload.load_fields(rt, "main", h.gw, refresh=False)
    assert second.source == "cache" and second.fields == first.fields
    assert h.field_calls() == 1
    third = fieldload.load_fields(rt, "main", h.gw, refresh=True)
    assert third.source == "provider" and h.field_calls() == 2


def test_the_cache_holds_the_header_and_no_credential(h: Harness) -> None:
    rt = h.runtime()
    fieldload.load_fields(rt, "main", h.gw, refresh=False)
    text = h.cache_file().read_text()
    doc = json.loads(text)
    assert doc["schema_version"] == 1 and doc["site"] == "main" and doc["url"] == h.fake.url
    assert doc["email_sha256"] == cache.email_sha256(SITE_EMAIL)
    assert SITE_TOKEN not in text
    assert SITE_EMAIL not in text
    assert oct(h.cache_file().stat().st_mode & 0o777) == "0o600"


def test_the_cache_is_per_email_and_url(h: Harness) -> None:
    rt = h.runtime()
    fieldload.load_fields(rt, "main", h.gw, refresh=False)
    h.fake.quirks.credentials = None  # accept any account: only the cache key differs
    other = make_gateway(h.fake, email="someone.else@example.test")
    assert fieldload.load_fields(rt, "main", other, refresh=False).source == "provider"


def test_cache_ttl_zero_reads_and_writes_nothing(h: Harness) -> None:
    rt = h.runtime("    cache_ttl: 0\n")
    fieldload.load_fields(rt, "main", h.gw, refresh=False)
    fieldload.load_fields(rt, "main", h.gw, refresh=False)
    assert h.field_calls() == 2
    assert not h.cache_file().exists()


@pytest.mark.parametrize(
    "content",
    [
        b"{not json",
        b"",
        b"[]",
        b'{"schema_version": 99}',
        b"\xff\xfe\x00",
    ],
)
def test_a_corrupt_cache_is_discarded_and_refetched(h: Harness, content: bytes) -> None:
    rt = h.runtime()
    fieldload.load_fields(rt, "main", h.gw, refresh=False)
    h.cache_file().write_bytes(content)
    got = fieldload.load_fields(rt, "main", h.gw, refresh=False)
    assert got.source == "provider"
    assert json.loads(h.cache_file().read_text())["schema_version"] == 1


def test_an_oversized_cache_is_discarded(h: Harness) -> None:
    rt = h.runtime()
    fieldload.load_fields(rt, "main", h.gw, refresh=False)
    text = h.cache_file().read_text()
    h.cache_file().write_text(text + " " * (cache.MAX_CACHE_BYTES + 1 - len(text)))
    assert fieldload.load_fields(rt, "main", h.gw, refresh=False).source == "provider"


def test_a_stale_cache_is_refetched(h: Harness) -> None:
    rt = h.runtime()
    fieldload.load_fields(rt, "main", h.gw, refresh=False)
    doc = json.loads(h.cache_file().read_text())
    doc["fetched_at"] = "2020-01-01T00:00:00Z"
    h.cache_file().write_text(json.dumps(doc))
    assert fieldload.load_fields(rt, "main", h.gw, refresh=False).source == "provider"


def test_a_future_dated_cache_is_refetched(h: Harness) -> None:
    rt = h.runtime()
    fieldload.load_fields(rt, "main", h.gw, refresh=False)
    doc = json.loads(h.cache_file().read_text())
    doc["fetched_at"] = "2999-01-01T00:00:00Z"
    h.cache_file().write_text(json.dumps(doc))
    assert fieldload.load_fields(rt, "main", h.gw, refresh=False).source == "provider"


@pytest.mark.parametrize(
    "tamper",
    [
        {"fields": "not a list"},
        {"fields": None},
        {"fields": [1, 2]},
        {"fields": [{"id": "x"}]},
        {"fields": [{"id": "customfield_1", "name": "A"}, {"id": "customfield_1", "name": "B"}]},
        {"fields": [{"id": "a b", "name": "A"}]},
        {"fields": {"id": "x"}},
        {"fields": [{"id": "customfield_1", "name": "A", "custom": "yes", "schema": None}]},
        {"fields": [{"id": "customfield_1", "name": "A", "custom": True, "schema": "string"}]},
        {"fields": [{"id": "customfield_1", "name": "A\x1b[0m", "custom": True, "schema": None}]},
        {},
    ],
)
def test_a_poisoned_cache_body_is_discarded_and_refetched(
    h: Harness, tamper: dict[str, Any]
) -> None:
    rt = h.runtime()
    fieldload.load_fields(rt, "main", h.gw, refresh=False)
    doc = json.loads(h.cache_file().read_text())
    doc.pop("fields")
    doc.update(tamper)
    h.cache_file().write_text(json.dumps(doc))
    got = fieldload.load_fields(rt, "main", h.gw, refresh=False)
    assert got.source == "provider"
    assert h.field_calls() == 2
    again = fieldload.load_fields(rt, "main", h.gw, refresh=False)
    assert again.source == "cache"


def test_a_symlinked_cache_file_is_refetched_with_a_warning(h: Harness, tmp_path: Path) -> None:
    rt = h.runtime()
    fieldload.load_fields(rt, "main", h.gw, refresh=False)
    target = tmp_path / "elsewhere.json"
    target.write_text(h.cache_file().read_text())
    h.cache_file().unlink()
    os.symlink(target, h.cache_file())
    got = fieldload.load_fields(rt, "main", h.gw, refresh=False)
    assert got.source == "provider"
    assert any("cannot be read" in w for w in h.warnings)
    assert target.read_text()  # the link target is untouched


def test_a_cache_that_cannot_be_written_only_warns(h: Harness) -> None:
    rt = h.runtime()
    fieldload.load_fields(rt, "main", h.gw, refresh=False)
    h.cache_file().unlink()
    h.cache_file().mkdir()  # a directory where the file goes: the write is refused
    got = fieldload.load_fields(rt, "main", h.gw, refresh=True)
    assert got.source == "provider"
    assert any("cannot be written" in w for w in h.warnings)


def test_ignored_entries_warn_without_naming_them(h: Harness) -> None:
    h.fake.extra_fields = [{"id": "bad id", "name": "SECRET-NAME"}, 5]
    rt = h.runtime()
    got = fieldload.load_fields(rt, "main", h.gw, refresh=False)
    assert got.ignored == 2
    assert any("2" in w and "ignored" in w for w in h.warnings)
    assert all("SECRET-NAME" not in w for w in h.warnings)


@pytest.mark.parametrize("body", [{"fields": []}, "text", 5, [[]] * 10001, ["x"] * 10001])
def test_an_unusable_field_list_is_provider_changed(h: Harness, body: Any) -> None:
    h.fake.field_list_body = body  # a bare text body is refused by the gateway itself
    with pytest.raises((TicketctlError, GatewayError)) as info:
        fieldload.load_fields(h.runtime(), "main", h.gw, refresh=False)
    assert info.value.code == "provider_changed"
    assert not h.cache_file().exists()


def test_a_non_2xx_answer_is_provider_changed(h: Harness) -> None:
    h.fake.inject(Fault("status", path=FIELDS, status=404, body="Not found"))
    with pytest.raises((TicketctlError, GatewayError)) as info:
        fieldload.load_fields(h.runtime(), "main", h.gw, refresh=False)
    assert info.value.code in ("provider_changed", "not_found")
    assert not h.cache_file().exists()


def test_a_hostile_list_is_cached_sanitized(h: Harness) -> None:
    h.fake.extra_fields = [
        {"id": "customfield_1", "name": "Evil\x1b[31m‮Name", "schema": {"type": "string"}}
    ]
    rt = h.runtime()
    fieldload.load_fields(rt, "main", h.gw, refresh=False)
    text = h.cache_file().read_text()
    assert "\x1b" not in text and "‮" not in text
    names = [f.name for f in fieldload.load_fields(rt, "main", h.gw, refresh=False).fields]
    assert any(n.startswith("Evil") for n in names)


def test_a_huge_but_valid_list_round_trips_through_the_cache(h: Harness) -> None:
    h.fake.extra_fields = [
        {
            "id": f"customfield_{n}",
            "name": f"Field {n}",
            "custom": True,
            "schema": {"type": "string"},
        }
        for n in range(2000, 7000)
    ]
    rt = h.runtime()
    first = fieldload.load_fields(rt, "main", h.gw, refresh=False)
    second = fieldload.load_fields(rt, "main", h.gw, refresh=False)
    assert second.source == "cache" and second.fields == first.fields
    assert len(second.fields) > 5000


# --- SiteFields through the loader ------------------------------------------------------------


def test_load_site_fields_applies_the_site_mapping(h: Harness) -> None:
    rt = h.runtime("    fields:\n      estimate:\n        name: Story Points\n")
    sf = fieldload.load_site_fields(rt, "main", h.gw)
    assert sf.resolve("estimate").need().id == F_POINTS
    assert sf.resolve("sprint").need().id == F_SPRINT


def test_get_with_ambiguous_points_still_needs_nothing_for_other_fields(h: Harness) -> None:
    sf = fieldload.load_site_fields(h.runtime(), "main", h.gw)
    assert sf.resolve("estimate").status is Status.AMBIGUOUS
    assert sf.resolve("sprint").need().id == F_SPRINT  # lazy: only the needed field refuses
    with pytest.raises(TicketctlError) as info:
        sf.resolve("estimate").need("main")
    assert info.value.code == "ambiguous_field"


def test_a_missing_mapping_from_an_old_cached_list_refetches_once(h: Harness) -> None:
    rt = h.runtime("    fields:\n      extra:\n        late:\n          name: Late field\n")
    sf = fieldload.load_site_fields(rt, "main", h.gw, needs=["late"])
    assert sf.resolve("late").status is Status.MISSING
    assert h.field_calls() == 1  # the list was fresh from the provider: nothing to retry
    h.fake.extra_fields = [
        {
            "id": "cf_late",
            "name": "Late field",
            "custom": True,
            "schema": {"type": "string"},
        }
    ]
    h.age_cache(120)
    sf = fieldload.load_site_fields(rt, "main", h.gw, needs=["late"])
    assert sf.source == "provider" and h.field_calls() == 2
    assert sf.resolve("late").need().id == "cf_late"
    assert fieldload.load_site_fields(rt, "main", h.gw, needs=["late"]).source == "cache"


def test_a_missing_mapping_from_a_list_cached_within_a_minute_is_not_refetched(h: Harness) -> None:
    rt = h.runtime("    fields:\n      sprint:\n        id: cf_gone\n")
    fieldload.load_site_fields(rt, "main", h.gw)
    h.age_cache(30)
    sf = fieldload.load_site_fields(rt, "main", h.gw, needs=["sprint"])
    assert sf.source == "cache" and h.field_calls() == 1


def test_a_persistently_missing_mapping_costs_one_fetch_per_minute_not_per_command(
    h: Harness,
) -> None:
    rt = h.runtime("    fields:\n      sprint:\n        id: cf_gone\n")
    fieldload.load_site_fields(rt, "main", h.gw, needs=["sprint"])
    for _ in range(5):
        fieldload.load_site_fields(rt, "main", h.gw, needs=["sprint"])
    assert h.field_calls() == 1
    h.age_cache(120)
    sf = fieldload.load_site_fields(rt, "main", h.gw, needs=["sprint"])
    for _ in range(4):
        sf = fieldload.load_site_fields(rt, "main", h.gw, needs=["sprint"])
    assert h.field_calls() == 2
    assert sf.resolve("sprint").status is Status.MISSING


def test_two_missing_names_refetch_once(h: Harness) -> None:
    rt = h.runtime(
        "    fields:\n      sprint:\n        id: cf_gone\n      epic:\n        id: cf_gone_too\n"
    )
    fieldload.load_site_fields(rt, "main", h.gw)
    h.age_cache(120)
    fieldload.load_site_fields(rt, "main", h.gw, needs=["sprint", "epic"])
    assert h.field_calls() == 2


def test_an_absent_undiscovered_field_does_not_refetch(h: Harness) -> None:
    rt = h.runtime()
    fieldload.load_site_fields(rt, "main", h.gw)
    h.age_cache(120)
    sf = fieldload.load_site_fields(rt, "main", h.gw, needs=["acceptance_criteria", "epic"])
    assert sf.resolve("acceptance_criteria").status is Status.ABSENT
    assert sf.source == "cache" and h.field_calls() == 1


def test_an_unknown_needed_name_does_not_refetch_or_raise(h: Harness) -> None:
    rt = h.runtime()
    fieldload.load_site_fields(rt, "main", h.gw)
    h.age_cache(120)
    sf = fieldload.load_site_fields(rt, "main", h.gw, needs=["no_such_name"])
    assert sf.source == "cache" and h.field_calls() == 1


def test_ambiguity_and_unneeded_names_do_not_refetch(h: Harness) -> None:
    rt = h.runtime()
    fieldload.load_site_fields(rt, "main", h.gw)
    h.age_cache(120)
    sf = fieldload.load_site_fields(rt, "main", h.gw, needs=["estimate", "sprint"])
    assert sf.source == "cache" and h.field_calls() == 1


def test_a_list_that_changes_between_reads_is_seen_after_refresh(h: Harness) -> None:
    rt = h.runtime()
    fieldload.load_site_fields(rt, "main", h.gw)
    h.fake.field_list_body = [
        {
            "id": F_ESTIMATE,
            "name": "Story point estimate",
            "custom": True,
            "schema": {"type": "number", "custom": f"{NS}:jsw-story-points"},
        },
    ]
    assert (
        fieldload.load_site_fields(rt, "main", h.gw).resolve("estimate").status is Status.AMBIGUOUS
    )
    sf = fieldload.load_site_fields(rt, "main", h.gw, refresh=True)
    assert sf.resolve("estimate").need().id == F_ESTIMATE


# --- priority and resolution lists ------------------------------------------------------------


def test_priorities_keep_the_provider_order_and_are_cached(h: Harness) -> None:
    rt = h.runtime()
    first = fieldload.load_priorities(rt, "main", h.gw)
    assert (first.names, first.source) == (tuple(PRIORITIES), "provider")
    second = fieldload.load_priorities(rt, "main", h.gw)
    assert (second.names, second.source) == (tuple(PRIORITIES), "cache")
    assert calls(h.fake, "/rest/api/3/priority") == 1
    assert json.loads(h.cache_file("priorities").read_text())["priorities"] == PRIORITIES
    assert fieldload.load_priorities(rt, "main", h.gw, refresh=True).source == "provider"


def test_resolutions_are_cached_in_their_own_file(h: Harness) -> None:
    rt = h.runtime()
    first = fieldload.load_resolutions(rt, "main", h.gw)
    assert first.names == tuple(RESOLUTIONS)
    assert fieldload.load_resolutions(rt, "main", h.gw).source == "cache"
    assert json.loads(h.cache_file("resolutions").read_text())["resolutions"] == RESOLUTIONS


def test_the_rank_of_a_priority_is_its_position(h: Harness) -> None:
    got = fieldload.load_priorities(h.runtime(), "main", h.gw)
    assert got.rank("Highest") == 0 and got.rank("Lowest") == len(PRIORITIES) - 1
    assert got.rank("Not in the list") is None
    assert got.rank(None) is None


def test_a_duplicate_priority_name_ranks_at_its_first_position(h: Harness) -> None:
    h.fake.priority_list_body = [
        {"id": "1", "name": "A"},
        {"id": "2", "name": "B"},
        {"id": "3", "name": "A"},
    ]
    got = fieldload.load_priorities(h.runtime(), "main", h.gw)
    assert got.names == ("A", "B", "A") and got.rank("A") == 0


LISTS = [
    ("priorities", "priority_list_body", fieldload.load_priorities, "/rest/api/3/priority"),
    ("resolutions", "resolution_list_body", fieldload.load_resolutions, "/rest/api/3/resolution"),
]


@pytest.mark.parametrize(("name", "attr", "load", "path"), LISTS)
@pytest.mark.parametrize(
    "body",
    [
        {"values": []},
        "x",
        [None],
        [{"id": "1"}],
        [{"name": 5}],
        [{"name": ""}],
        [{"name": "ok"}, "bad"],
        [{"name": "n"}] * 1001,
    ],
)
def test_a_malformed_name_list_is_provider_changed(
    h: Harness, name: str, attr: str, load: Any, path: str, body: Any
) -> None:
    setattr(h.fake, attr, body)
    with pytest.raises((TicketctlError, GatewayError)) as info:
        load(h.runtime(), "main", h.gw)
    assert info.value.code == "provider_changed"
    assert not h.cache_file(name).exists()


@pytest.mark.parametrize(("name", "attr", "load", "path"), LISTS)
def test_a_name_list_of_1000_entries_is_accepted(
    h: Harness, name: str, attr: str, load: Any, path: str
) -> None:
    setattr(h.fake, attr, [{"id": str(n), "name": f"n{n}"} for n in range(1000)])
    assert len(load(h.runtime(), "main", h.gw).names) == 1000


def test_priority_names_are_sanitized(h: Harness) -> None:
    h.fake.priority_list_body = [{"id": "1", "name": "High\x1b[0m‮"}]
    got = fieldload.load_priorities(h.runtime(), "main", h.gw)
    assert "\x1b" not in got.names[0] and "‮" not in got.names[0]


@pytest.mark.parametrize(("name", "attr", "load", "path"), LISTS)
@pytest.mark.parametrize("poison", [[1, 2], "x", None, [""], ["ok"] * 1001])
def test_a_poisoned_name_cache_is_discarded(
    h: Harness, name: str, attr: str, load: Any, path: str, poison: Any
) -> None:
    rt = h.runtime()
    load(rt, "main", h.gw)
    doc = json.loads(h.cache_file(name).read_text())
    doc[name] = poison
    h.cache_file(name).write_text(json.dumps(doc))
    assert load(rt, "main", h.gw).source == "provider"
    assert calls(h.fake, path) == 2
    assert load(rt, "main", h.gw).source == "cache"


@pytest.mark.parametrize(("name", "attr", "load", "path"), LISTS)
def test_name_cache_entries_are_sanitized_on_read(
    h: Harness, name: str, attr: str, load: Any, path: str
) -> None:
    rt = h.runtime()
    load(rt, "main", h.gw)
    doc = json.loads(h.cache_file(name).read_text())
    doc[name] = ["Evil\x1b[31m\u202eName"]
    h.cache_file(name).write_text(json.dumps(doc))
    got = load(rt, "main", h.gw)
    assert got.source == "cache"
    assert "\x1b" not in got.names[0] and "\u202e" not in got.names[0]


def test_a_cache_read_state_error_that_is_not_downgradable_is_raised(
    h: Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    def boom(*args: Any, **kwargs: Any) -> Any:
        raise StateError("state_owner", "state file is not owned by the current user")

    monkeypatch.setattr(cache, "read_entry", boom)
    with pytest.raises(StateError) as info:
        fieldload.load_fields(h.runtime(), "main", h.gw, refresh=False)
    assert info.value.reason == "state_owner"
    assert h.field_calls() == 0


def test_a_poisoned_cache_is_discarded_by_the_identity_that_was_read(
    h: Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    rt = h.runtime()
    fieldload.load_fields(rt, "main", h.gw, refresh=False)
    doc = json.loads(h.cache_file().read_text())
    doc["fields"] = "poison"
    h.cache_file().write_text(json.dumps(doc))
    seen: list[Any] = []
    real = cache.discard_cache

    def spy(*args: Any, **kwargs: Any) -> None:
        seen.append(kwargs.get("ident"))
        real(*args, **kwargs)

    monkeypatch.setattr(cache, "discard_cache", spy)
    fieldload.load_fields(rt, "main", h.gw, refresh=False)
    assert len(seen) == 1 and seen[0] is not None


def test_a_list_too_large_to_cache_is_fetched_every_call_with_a_warning(h: Harness) -> None:
    h.fake.extra_fields = [
        {
            "id": f"customfield_{n}",
            "name": "\u00e9" * 200,
            "custom": True,
            "schema": {"type": "string"},
        }
        for n in range(2000, 9000)
    ]
    rt = h.runtime()
    first = fieldload.load_fields(rt, "main", h.gw, refresh=False)
    second = fieldload.load_fields(rt, "main", h.gw, refresh=False)
    assert first.source == second.source == "provider" and len(second.fields) > 5000
    assert h.field_calls() == 2
    assert sum("cannot be written" in w for w in h.warnings) == 2
    assert not h.cache_file().exists()


def test_an_empty_priority_list_is_valid(h: Harness) -> None:
    h.fake.priority_list_body = []
    got = fieldload.load_priorities(h.runtime(), "main", h.gw)
    assert got.names == () and got.rank("High") is None
