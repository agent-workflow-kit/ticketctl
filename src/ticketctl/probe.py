"""``validate --probe``: read-only calls that check a site against its configuration.

For every configured site (sorted by name) the probe authenticates (a fresh ``/myself``, never the
cache), compares the account with the configured expectation (``account_mismatch``), runs the
routine of ``fields check`` on a fresh field list and parses every named query with the provider's
parse endpoint. What it finds is data: the problems and the invalid queries are printed and the
command exits 0. A provider or credential failure is still an error (the exit code of that
failure, naming the site), because nothing was learned. The probe sends no write: its only POST is
the parse endpoint, passed to the gateway as a read.

A named query is compiled exactly as ``search`` would compile it with no other flag
(``search_compile.compile_search``), so a query that ``search --query`` would refuse is reported
invalid without a request. Provider text (a parse error) is sanitized, scrubbed and capped.

Each site opens its own gateway, and ``open_gateway`` replaces the registered secrets, so after
each site the probe registers every token and Basic value opened so far: the final output, written
after the last site, must still scrub the first one.

Unverified against a real site (the fake provider is the only evidence): that ``POST
/rest/api/3/jql/parse?validation=strict`` answers 200 with ``{"queries": [{"errors": [...]}]}``
for a bad query, with one entry per query sent and strings in ``errors``.
"""

from __future__ import annotations

from typing import Any

from . import fieldload, fieldscmd, identity
from .errors import TicketctlError
from .gateway import Gateway
from .runtime import Runtime, for_site, open_gateway
from .sanitize import InputError, basic_auth_value, configure_scrubber, sanitize_field
from .search_compile import compile_search

PARSE = "/rest/api/3/jql/parse"
MAX_ERROR = 1024  # schemas/cli/validate.json: sites[].queries[].error


def _parse_errors(gw: Gateway, jql: str) -> list[str]:
    """The parse endpoint's error strings for one query (empty: it parses)."""
    response = gw.request(
        "POST",
        PARSE,
        template=PARSE,
        query="validation=strict",
        body={"queries": [jql]},
        write=False,  # a read that needs a POST
    )
    data = response.data
    results = data.get("queries") if isinstance(data, dict) else None
    entry = results[0] if isinstance(results, list) and len(results) == 1 else None
    errors = entry.get("errors") if isinstance(entry, dict) else None
    if (
        not 200 <= response.status < 300
        or not isinstance(errors, list)
        or not all(isinstance(e, str) for e in errors)
    ):
        raise TicketctlError("provider_changed", f"unexpected answer from {PARSE}")
    return errors


def _query_result(gw: Gateway, name: str, text: str) -> dict[str, Any]:
    try:
        jql = compile_search(query=text)
    except InputError as exc:
        return {"name": name, "valid": False, "error": _error_text(exc.message)}
    errors = _parse_errors(gw, jql)
    if not errors:
        return {"name": name, "valid": True, "error": None}
    return {"name": name, "valid": False, "error": _error_text("; ".join(errors))}


def _error_text(text: str) -> str:
    return sanitize_field(text, cap=MAX_ERROR)[0]


def probe_site(rt: Runtime, site_name: str, secrets: list[tuple[str, str]]) -> dict[str, Any]:
    """The ``sites[]`` object of one site; ``secrets`` collects (token, basic value) pairs."""
    site = rt.config.sites[site_name]
    with for_site(site_name):
        gw = open_gateway(rt, site_name)
        secrets.append((gw.target.token, basic_auth_value(gw.target.email, gw.target.token)))
        configure_scrubber([t for t, _ in secrets], basic_values=[b for _, b in secrets])
        me = identity.load_me(rt, site_name, gw, refresh=True)
        problems: list[dict[str, Any]] = []
        expected = identity.expected_account(rt.config, site_name)
        if expected is not None and expected != me.account_id:
            problems.append({"problem": "account_mismatch"})
        site_fields = fieldload.load_site_fields(
            rt, site_name, gw, refresh=True, needs=fieldscmd.needs_of(rt, site_name)
        )
        problems += fieldscmd.problem_documents(site_fields)
        queries = [_query_result(gw, n, site.queries[n]) for n in sorted(site.queries)]
    return {
        "site": site_name,
        "account_id": me.account_id,
        "problems": problems,
        "queries": queries,
    }


def probe(rt: Runtime) -> list[dict[str, Any]]:
    secrets: list[tuple[str, str]] = []
    return [probe_site(rt, name, secrets) for name in sorted(rt.config.sites)]


def human_site(site: dict[str, Any]) -> list[str]:
    lines = [f"{site['site']}: account {site['account_id']}"]
    if not site["problems"]:
        lines.append("  fields: no problems")
    lines += [f"  {fieldscmd.human_problem(p)}" for p in site["problems"]]
    for q in site["queries"]:
        lines.append(f"  query {q['name']}: " + ("ok" if q["valid"] else f"invalid ({q['error']})"))
    return lines


__all__ = ["human_site", "probe", "probe_site"]
