"""Fetch and cache the field, priority and resolution lists (docs/interface.md, Files).

``load_fields`` / ``load_priorities`` / ``load_resolutions`` serve a fresh cache entry
(``cache/<site>/{fields,priorities,resolutions}.json``, per site, url and email hash, within
``cache_ttl``) unless ``refresh`` is set, else the provider's list, which is then cached. All three
follow ``identity.load_me``: a cache that cannot be read or written only costs a fetch and a
warning; a cache whose header is valid but whose body is not a usable list is deleted and
refetched (the cache is a state file, so its content is as untrusted as the provider's). The
provider's own answer that is not a usable list, or an empty field list (a site always has
system fields), is exit 12 ``provider_changed`` and never replaces a cache.

``load_site_fields(rt, site, gw, refresh=False, needs=())`` adds the site's ``fields`` config
(:class:`fieldmap.SiteFields`). When the list came from the cache and a name in ``needs`` is mapped
to a field the cached list lacks, the list is refetched once (a field created after the cache was
written must not be reported missing); a fresh list is never refetched, a cached list fetched within
the last ``REFETCH_AFTER`` (60 seconds) is not either (a mapping that stays missing must not cost a
fetch on every command), and neither is a mere ambiguity or an unmapped, undiscovered field. A list
too large to cache is fetched on every call, with one warning each time.

Unverified against a real site: that ``GET /field``, ``/priority`` and ``/resolution`` answer a
plain JSON array of objects with ``name`` strings, that the priority list is in rank order, and
that none of them pages.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from . import cache
from .errors import TicketctlError
from .fieldmap import (
    FieldListError,
    ProviderField,
    SiteFields,
    Status,
    parse_field_list,
)
from .gateway import Gateway
from .runtime import Runtime
from .sanitize import CAPS, sanitize_field
from .state import StateError

FIELD = "/rest/api/3/field"
PRIORITY = "/rest/api/3/priority"
RESOLUTION = "/rest/api/3/resolution"
MAX_NAMES = 1000
REFETCH_AFTER = timedelta(seconds=60)

# Cache-read failures that only cost a refetch; any other state failure stays fatal.
_CACHE_READ_DOWNGRADES = frozenset({"state_symlink", "state_type", "state_io"})


@dataclass(frozen=True)
class LoadedFields:
    fields: tuple[ProviderField, ...]
    source: str  # "cache" or "provider"
    ignored: int = 0  # provider entries that were not usable (always 0 from the cache)
    fetched_at: datetime | None = None  # when a cached list was fetched (None: just fetched)


@dataclass(frozen=True)
class NameList:
    """An ordered list of provider names (priorities, resolutions)."""

    names: tuple[str, ...]
    source: str

    def rank(self, name: str | None) -> int | None:
        """The zero-based position of ``name`` (its first one), or None when not listed."""
        if name is None or name not in self.names:
            return None
        return self.names.index(name)


def _fetch(gw: Gateway, path: str, what: str) -> Any:
    response = gw.request("GET", path, template=path)
    if not 200 <= response.status < 300:
        raise TicketctlError("provider_changed", f"unexpected answer from {path} ({what})")
    return response.data


def _read(
    rt: Runtime, site_name: str, gw: Gateway, name: str, label: str
) -> cache.CacheEntry | None:
    site = rt.config.sites[site_name]
    try:
        return cache.read_entry(
            rt.state_dir,
            site_name,
            name,
            url=str(site.url),
            email=gw.target.email,
            ttl=site.cache_ttl,
        )
    except StateError as exc:
        if exc.reason not in _CACHE_READ_DOWNGRADES:
            raise
        rt.warn(f"warning: the {label} cache cannot be read ({exc.reason}); refetching")
        return None


def _write(
    rt: Runtime, site_name: str, gw: Gateway, name: str, label: str, data: dict[str, Any]
) -> None:
    site = rt.config.sites[site_name]
    try:
        cache.write_cache(
            rt.state_dir,
            site_name,
            name,
            data,
            url=str(site.url),
            email=gw.target.email,
            ttl=site.cache_ttl,
        )
    except StateError as exc:
        # The answer is good; a cache that cannot be written only costs the next call a fetch.
        rt.warn(f"warning: the {label} cache cannot be written ({exc.reason})")


def _discard(rt: Runtime, site_name: str, name: str, entry: cache.CacheEntry) -> None:
    try:
        cache.discard_cache(rt.state_dir, site_name, name, ident=entry.ident)
    except StateError:
        pass  # the refetch below then fails to write too and warns


# --- the field list ---------------------------------------------------------------------------


def load_fields(rt: Runtime, site_name: str, gw: Gateway, *, refresh: bool) -> LoadedFields:
    if not refresh:
        stored = _read(rt, site_name, gw, "fields", "field list")
        if stored is not None:
            try:
                parsed = parse_field_list(stored.doc.get("fields"), strict=True)
            except FieldListError:
                _discard(rt, site_name, "fields", stored)
            else:
                return LoadedFields(parsed.fields, "cache", fetched_at=stored.fetched_at)
    data = _fetch(gw, FIELD, "field list")
    try:
        parsed = parse_field_list(data)
    except FieldListError as exc:
        raise TicketctlError(
            "provider_changed", f"{FIELD} answered no usable list ({exc})"
        ) from None
    if not parsed.fields:
        # A site always has system fields, so an empty list is not an answer worth caching.
        raise TicketctlError("provider_changed", f"{FIELD} answered an empty list")
    if parsed.ignored:
        rt.warn(f"warning: {parsed.ignored} entries of the provider's field list were ignored")
    _write(
        rt,
        site_name,
        gw,
        "fields",
        "field list",
        {"fields": [f.to_document() for f in parsed.fields]},
    )
    return LoadedFields(parsed.fields, "provider", parsed.ignored)


def load_site_fields(
    rt: Runtime,
    site_name: str,
    gw: Gateway,
    *,
    refresh: bool = False,
    needs: Iterable[str] = (),
) -> SiteFields:
    config = rt.config.sites[site_name].fields
    loaded = load_fields(rt, site_name, gw, refresh=refresh)
    site_fields = SiteFields(loaded.fields, config, loaded.source)
    if loaded.source == "cache" and _old_enough(loaded):
        for name in needs:
            try:
                missing = site_fields.resolve(name).status is Status.MISSING
            except TicketctlError:
                continue  # an unknown name is the caller's refusal, not a stale list
            if missing:
                loaded = load_fields(rt, site_name, gw, refresh=True)
                return SiteFields(loaded.fields, config, loaded.source)
    return site_fields


def _old_enough(loaded: LoadedFields) -> bool:
    """A cached list is retried for a missing mapping only once it is older than a minute."""
    return loaded.fetched_at is None or datetime.now(UTC) - loaded.fetched_at > REFETCH_AFTER


# --- priorities and resolutions ---------------------------------------------------------------


def _names_from_provider(data: object) -> tuple[str, ...]:
    if not isinstance(data, list) or len(data) > MAX_NAMES:
        raise FieldListError("not a usable list")
    names = []
    for item in data:
        raw = item.get("name") if isinstance(item, dict) else None
        if not isinstance(raw, str) or not raw:
            raise FieldListError("an entry has no name")
        names.append(sanitize_field(raw, cap=CAPS["name"])[0])
    return tuple(names)


def _names_from_cache(data: object) -> tuple[str, ...]:
    if not isinstance(data, list) or len(data) > MAX_NAMES:
        raise FieldListError("not a usable list")
    if not all(isinstance(n, str) and n for n in data):
        raise FieldListError("an entry is not a name")
    return tuple(sanitize_field(n, cap=CAPS["name"])[0] for n in data)


def _load_names(
    rt: Runtime, site_name: str, gw: Gateway, *, name: str, path: str, label: str, refresh: bool
) -> NameList:
    if not refresh:
        stored = _read(rt, site_name, gw, name, label)
        if stored is not None:
            try:
                return NameList(_names_from_cache(stored.doc.get(name)), "cache")
            except FieldListError:
                _discard(rt, site_name, name, stored)
    data = _fetch(gw, path, label)
    try:
        names = _names_from_provider(data)
    except FieldListError:
        raise TicketctlError("provider_changed", f"{path} answered no usable list") from None
    _write(rt, site_name, gw, name, label, {name: list(names)})
    return NameList(names, "provider")


def load_priorities(rt: Runtime, site_name: str, gw: Gateway, *, refresh: bool = False) -> NameList:
    return _load_names(
        rt, site_name, gw, name="priorities", path=PRIORITY, label="priority list", refresh=refresh
    )


def load_resolutions(
    rt: Runtime, site_name: str, gw: Gateway, *, refresh: bool = False
) -> NameList:
    return _load_names(
        rt,
        site_name,
        gw,
        name="resolutions",
        path=RESOLUTION,
        label="resolution list",
        refresh=refresh,
    )


__all__ = [
    "LoadedFields",
    "NameList",
    "load_fields",
    "load_priorities",
    "load_resolutions",
    "load_site_fields",
]
