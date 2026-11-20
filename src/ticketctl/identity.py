"""The authenticated account: ``/myself``, the ``me.json`` cache and the account expectation.

``load_me`` serves ``whoami`` and the time-zone lookups of later commands: a fresh cache entry
(``me.json``, per site, ``cache_ttl``) unless ``refresh`` is set, else a ``GET /rest/api/3/myself``
that is then cached. ``check_expectation`` compares the account with the configured expectation
(``sites.<name>.account_id``, else ``me.jira``): a warning for reads, exit 2 ``account_mismatch``
for writes (the write flow of #23 calls it with ``write=True`` after a fresh ``load_me``).

Unverified against a real site (the fake provider is the only evidence): that ``/myself`` answers
``accountId``, ``displayName`` and ``timeZone`` as strings.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from . import cache
from .config import Config
from .errors import TicketctlError
from .gateway import Gateway
from .runtime import Runtime
from .sanitize import CAPS, GRAMMARS, sanitize_field
from .state import StateError

MYSELF = "/rest/api/3/myself"


@dataclass(frozen=True)
class Me:
    account_id: str
    display_name: str | None
    time_zone: str | None
    source: str  # "cache" or "provider"


def _text(value: object, cap: int) -> str | None:
    if not isinstance(value, str):
        return None
    return sanitize_field(value, cap=cap)[0]


def _from_document(doc: dict[str, Any], source: str) -> Me | None:
    """A ``Me`` from a cache entry or a provider answer; None when it fails its grammars."""
    account_id = doc.get("account_id")
    if not GRAMMARS["account_id"].matches(account_id):
        return None
    zone = doc.get("time_zone")
    if zone is not None and not GRAMMARS["time_zone"].matches(zone):
        return None
    name = doc.get("display_name")
    if name is not None and not isinstance(name, str):
        return None
    assert isinstance(account_id, str)
    return Me(account_id, _text(name, CAPS["name"]), zone, source)


def fetch_me(gw: Gateway) -> Me:
    """A fresh ``/myself``. A missing or malformed account id is ``provider_changed``."""
    response = gw.request("GET", MYSELF, template=MYSELF)
    data = response.data
    if not 200 <= response.status < 300 or not isinstance(data, dict):
        raise TicketctlError("provider_changed", f"unexpected answer from {MYSELF}")
    zone = data.get("timeZone")
    name = data.get("displayName")
    document = {
        "account_id": data.get("accountId"),
        # Like the zone, a non-string name is dropped, not an error: only the id matters.
        "display_name": name if isinstance(name, str) else None,
        # A zone that fails its grammar is dropped, not an error: only time-zone users need it.
        "time_zone": zone if GRAMMARS["time_zone"].matches(zone) else None,
    }
    me = _from_document(document, "provider")
    if me is None:
        raise TicketctlError("provider_changed", f"{MYSELF} answered no usable account id")
    return me


# Cache-read failures that only cost a refetch; any other state failure stays fatal.
_CACHE_READ_DOWNGRADES = frozenset({"state_symlink", "state_type", "state_io"})


def load_me(rt: Runtime, site_name: str, gw: Gateway, *, refresh: bool) -> Me:
    """The cached account (fresh within ``cache_ttl``) or a fresh ``/myself`` that is cached."""
    site = rt.config.sites[site_name]
    url = str(site.url)
    email = gw.target.email
    if not refresh:
        try:
            stored = cache.read_cache(
                rt.state_dir, site_name, "me", url=url, email=email, ttl=site.cache_ttl
            )
        except StateError as exc:
            if exc.reason not in _CACHE_READ_DOWNGRADES:
                raise
            rt.warn(f"warning: the account cache cannot be read ({exc.reason}); refetching")
            stored = None
        me = None if stored is None else _from_document(stored, "cache")
        if me is not None:
            return me
    me = fetch_me(gw)
    try:
        cache.write_cache(
            rt.state_dir,
            site_name,
            "me",
            {
                "account_id": me.account_id,
                "display_name": me.display_name,
                "time_zone": me.time_zone,
            },
            url=url,
            email=email,
            ttl=site.cache_ttl,
        )
    except StateError as exc:
        # The answer is good; a cache that cannot be written only costs the next call a fetch.
        rt.warn(f"warning: the account cache cannot be written ({exc.reason})")
    return me


def expected_account(config: Config, site_name: str) -> str | None:
    """The configured expectation: the site's ``account_id`` overrides ``me.jira``."""
    return config.sites[site_name].account_id or config.me.jira


# todo(#23): the write flow must call check_expectation(write=True) after a fresh /myself
# (load_me with refresh=True, never the cache) and then validate_adf() on rich text.
def check_expectation(
    config: Config, site_name: str, account_id: str, *, write: bool
) -> str | None:
    """None when the account matches (or nothing is expected); else the warning for a read.

    For a write a mismatch is exit 2 ``invalid_config`` (reason ``account_mismatch``), raised
    before anything is sent.
    """
    expected = expected_account(config, site_name)
    if expected is None or expected == account_id:
        return None
    message = f"the account differs from the configured expectation for site {site_name}"
    if write:
        raise TicketctlError("invalid_config", message, reason="account_mismatch", site=site_name)
    return f"warning: {message}"
