"""Handlers of ``whoami``, ``sites``, ``sites resolve`` and ``validate``.

Each returns the ``--json`` object (``schemas/cli/<command>.json``); ``human_*`` renders it as the
plain text of a run without ``--json``. ``sites``, ``sites resolve`` and an offline ``validate``
are offline by construction: they never open a gateway, so they never read a credential variable.
"""

from __future__ import annotations

import argparse
from typing import Any

from . import identity, selection
from .errors import TicketctlError
from .flags import Globals
from .runtime import for_site, load_runtime, open_gateway


def whoami(g: Globals, a: argparse.Namespace) -> dict[str, Any]:
    rt = load_runtime(g)
    chosen = selection.select_site(rt.config, site_flag=g.site)
    name = chosen.site
    with for_site(name):
        gw = open_gateway(rt, name)
        me = identity.load_me(rt, name, gw, refresh=a.refresh)
        warning = identity.check_expectation(rt.config, name, me.account_id, write=False)
    if warning is not None:
        rt.warn(warning)
    return {
        "site": name,
        "account_id": me.account_id,
        "display_name": me.display_name,
        "source": me.source,
    }


def human_whoami(r: dict[str, Any]) -> str:
    who = r["display_name"] or "(no display name)"
    return f"{r['site']}: {who} ({r['account_id']}), from the {r['source']}"


def sites(g: Globals, a: argparse.Namespace) -> dict[str, Any]:
    rt = load_runtime(g)
    config = rt.config
    listing = [
        {
            "name": name,
            "provider": site.provider,
            "url": str(site.url),
            "projects": list(site.projects),
            "default": name == config.default_site,
        }
        for name, site in sorted(config.sites.items())
    ]
    return {"sites": listing, "default_site": config.default_site}


def human_sites(r: dict[str, Any]) -> str:
    if not r["sites"]:
        return "no sites are configured"
    lines = []
    for s in r["sites"]:
        mark = " (default)" if s["default"] else ""
        projects = ", ".join(s["projects"]) or "-"
        lines.append(f"{s['name']}{mark}: {s['url']} projects: {projects}")
    return "\n".join(lines)


def sites_resolve(g: Globals, a: argparse.Namespace) -> dict[str, Any]:
    rt = load_runtime(g)
    chosen = selection.select_site(rt.config, site_flag=g.site, refs=[a.key])
    assert chosen.key is not None
    return {"key": chosen.key, "site": chosen.site, "rule": chosen.rule}


def human_sites_resolve(r: dict[str, Any]) -> str:
    return f"{r['key']}: {r['site']} (rule {r['rule']})"


def validate(g: Globals, a: argparse.Namespace) -> dict[str, Any]:
    if a.probe:
        # TODO(#19): --probe authenticates, compares the account, runs the `fields check` routine
        # and parses the named queries (needs #17 and #19); only the offline form exists.
        raise TicketctlError("internal", "validate --probe is not implemented yet")
    rt = load_runtime(g, show_warnings=False)  # they are part of the result
    # A configuration with no site validates nothing a command could use: `no_sites`.
    rt.config.require_sites()
    return {"valid": True, "probe": False, "warnings": list(rt.loaded.warnings)}


def human_validate(r: dict[str, Any]) -> str:
    lines = ["the configuration is valid"]
    lines += [f"warning: {w}" for w in r["warnings"]]
    return "\n".join(lines)
