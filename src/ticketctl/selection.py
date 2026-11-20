"""Site selection: the one total table of docs/interface.md ("Site selection").

``select_site`` takes the configuration, the ``--site`` flag and the command's references (ticket
keys or browse URLs) and project keys, and returns the one site and the rule that chose it. It
never touches the network or a credential. The inputs are tried in order and the first that selects
a site wins: ``--site``, a browse URL, a key or project, ``default_site``, the only site; nothing
selecting is ``ambiguous_site`` (several sites) or ``no_sites`` (none).

Browse URLs are judged after ``urlsplit`` (never by substring): scheme ``https`` (``http`` only for
an ``allow_http`` site), no userinfo, a path of ``/browse/<KEY>`` with an optional trailing slash;
the query and fragment are ignored and only scheme, host and port pick the site.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass
from urllib.parse import urlsplit

from .config import Config, Site
from .errors import TicketctlError
from .sanitize import GRAMMARS, InputError, browse_key, normalize_ticket_key, split_url

MAX_INPUT = 256
MAX_URL = 2048
RULES = ("site_flag", "url", "project", "default_site", "only_site")
_SCHEME = re.compile(r"[A-Za-z][A-Za-z0-9+.-]*://")


@dataclass(frozen=True)
class Selection:
    """The selected site and the table rule that chose it (``sites-resolve.json``)."""

    site: str
    rule: str
    key: str | None = None  # the key of a key or browse-URL reference, upper-case


def _invalid(reason: str, message: str) -> TicketctlError:
    return TicketctlError("invalid_request", message, reason=reason)


def _too_long() -> InputError:
    return InputError(22, "invalid_request", "invalid_value", "value is too long")


def _origin(url: str) -> tuple[str, str, int]:
    """(scheme, lower-cased host, port) of an already validated URL; default port filled in."""
    parts = urlsplit(url)
    scheme = parts.scheme.lower()
    host = (parts.hostname or "").lower()
    port = parts.port or (443 if scheme == "https" else 80)
    return scheme, host, port


def _site_origin(site: Site) -> tuple[str, str, int]:
    return _origin(str(site.url))


def _site_name_checked(config: Config, name: str) -> str:
    if len(name) > MAX_INPUT:
        raise _too_long()
    GRAMMARS["site_name"].check(name)  # InputError exit 22 unknown_site
    if name not in config.sites:
        raise _invalid("unknown_site", "unknown site name")
    return name


def _candidates(names: Sequence[str]) -> list[str]:
    return sorted(names)


def _ambiguous(names: Sequence[str], message: str) -> TicketctlError:
    return TicketctlError(
        "ambiguous_site",
        f"{message}; name one with --site",
        details={"candidates": _candidates(names)},
    )


def _parse_url(text: str) -> tuple[str, str, int, str]:
    """(scheme, host, port, key) of a browse URL, or exit 22 ``invalid_value``."""
    if len(text) > MAX_URL:
        raise _too_long()
    parts = split_url(text)
    scheme = parts.scheme.lower()
    if scheme not in ("https", "http"):
        raise _invalid("invalid_value", "a browse url must use https")
    try:
        port = parts.port
        userinfo = parts.username is not None or parts.password is not None
    except ValueError:
        raise _invalid("invalid_value", "a browse url has an invalid port") from None
    if userinfo or "@" in parts.netloc:
        raise _invalid("invalid_value", "a browse url must not contain user information")
    host = (parts.hostname or "").lower()
    if not host:
        raise _invalid("invalid_value", "a browse url needs a host")
    key = browse_key(parts.path)
    if key is None:
        raise _invalid("invalid_value", "a browse url must have the path /browse/<KEY>")
    return scheme, host, port or (443 if scheme == "https" else 80), key


def _select_by_url(config: Config, text: str) -> tuple[str, list[str]]:
    """(key, the sites whose scheme, host and port equal the URL's)."""
    scheme, host, port, key = _parse_url(text)
    if scheme == "http" and not any(site.allow_http for site in config.sites.values()):
        raise _invalid("invalid_value", "an http browse url needs a site with allow_http")
    same_host = [
        name for name, site in config.sites.items() if _site_origin(site)[1:] == (host, port)
    ]
    if not same_host:
        raise _invalid("unknown_site", "the url's host is not a configured site")
    matching = [n for n in same_host if _site_origin(config.sites[n])[0] == scheme]
    if not matching:
        raise _invalid("invalid_value", "the url's scheme differs from the configured site's")
    return key, matching


def _project_of(key: str) -> str:
    return key.rpartition("-")[0]


def _claimed_by(config: Config, project: str) -> list[str]:
    return [name for name, site in config.sites.items() if project in site.projects]


def _by_project(config: Config, project: str) -> str | None:
    claimed = _claimed_by(config, project)
    if len(claimed) > 1:
        raise _ambiguous(claimed, f"project {project} is claimed by several sites")
    return claimed[0] if claimed else None


def _fallback(config: Config) -> tuple[str, str]:
    if config.default_site is not None:
        return config.default_site, "default_site"
    if len(config.sites) == 1:
        return next(iter(config.sites)), "only_site"
    raise _ambiguous(list(config.sites), "no rule selects one of the configured sites")


def _is_url(text: str) -> bool:
    return _SCHEME.match(text) is not None


def _check_project(text: str) -> str:
    if len(text) > MAX_INPUT:
        raise _too_long()
    return GRAMMARS["project_key"].check(text)


def _check_ref(text: str) -> str:
    if len(text) > MAX_INPUT:
        raise _too_long()
    return normalize_ticket_key(text)


def select_site(
    config: Config,
    *,
    site_flag: str | None = None,
    refs: Sequence[str] = (),
    projects: Sequence[str] = (),
) -> Selection:
    """Pick the one site for a command (see the module docstring).

    ``refs`` are ticket keys or browse URLs as typed, ``projects`` are project keys (``--project``
    values, ``--key`` project parts and the ``create`` project). Several inputs must agree on one
    site (``mixed_sites``); ``--site`` forces it. The key of the first reference is returned.
    """
    config.require_sites()
    forced = _site_name_checked(config, site_flag) if site_flag is not None else None
    chosen: list[tuple[str, str]] = []  # (site, rule) per input
    first_key: str | None = None
    for text in refs:
        if not _is_url(text):
            key = _check_ref(text)
            chosen.append(_by_key(config, forced, _project_of(key)))
        elif forced is not None:
            key = _url_for_forced(config, text, forced)
            chosen.append((forced, "site_flag"))
        else:
            key, matching = _select_by_url(config, text)
            if len(matching) > 1:
                raise _ambiguous(matching, "several sites have the url's host")
            chosen.append((matching[0], "url"))
        first_key = first_key or key
    for text in projects:
        chosen.append(_by_key(config, forced, _check_project(text)))
    if not chosen:
        chosen.append((forced, "site_flag") if forced is not None else _fallback(config))
    sites = {site for site, _ in chosen}
    if len(sites) > 1:
        raise _invalid("mixed_sites", "the keys select different sites; use --site")
    site, rule = chosen[0]
    return Selection(site=site, rule=rule, key=first_key)


def _url_for_forced(config: Config, text: str, forced: str) -> str:
    scheme, host, port, key = _parse_url(text)
    if _site_origin(config.sites[forced]) != (scheme, host, port):
        raise _invalid("invalid_value", "url and --site disagree")
    return key


def _by_key(config: Config, forced: str | None, project: str) -> tuple[str, str]:
    if forced is not None:
        return forced, "site_flag"
    claimed = _by_project(config, project)
    if claimed is not None:
        return claimed, "project"
    return _fallback(config)
