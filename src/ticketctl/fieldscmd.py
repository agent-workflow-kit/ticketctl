"""Handlers of ``fields list``, ``fields check`` and ``fields refresh`` (docs/interface.md, fields).

Each returns the ``--json`` object (``schemas/cli/fields-*.json``); ``human_*`` renders it as plain
text. The three are diagnostics: ``fields check`` prints the problems of ``SiteFields.problems()``
as data and exits 0 whenever it ran (only a provider or config failure is an error). ``fields
list`` is the one command that prints provider field ids; the problems and candidates of
``check`` name display names only. The field list is untrusted provider data: it is already
sanitized and capped by ``fieldmap.parse_field_list``, and every line leaves through
``output.write``.

Unverified against a real site: the field list answer itself (see ``fieldload``).
"""

from __future__ import annotations

import argparse
from typing import Any

from . import fieldload, selection
from .config import LOGICAL_FIELDS
from .fieldmap import SiteFields, Status
from .flags import Globals
from .runtime import Runtime, for_site, load_runtime, open_gateway


def problem_documents(site_fields: SiteFields) -> list[dict[str, Any]]:
    """``SiteFields.problems()`` as the objects of ``fields-check.json`` (no ids anywhere)."""
    documents: list[dict[str, Any]] = []
    for p in site_fields.problems():
        doc: dict[str, Any] = {"logical": p.logical, "problem": p.problem}
        if p.problem == "ambiguous":
            doc["candidates"] = list(p.candidates)
        documents.append(doc)
    return documents


def needs_of(rt: Runtime, site: str) -> list[str]:
    """Every name a check judges: the refetch-on-missing rule applies to all of them."""
    return [*LOGICAL_FIELDS, *sorted(rt.config.sites[site].fields.extra)]


def _site(g: Globals, rt: Runtime) -> str:
    return selection.select_site(rt.config, site_flag=g.site).site


def fields_list(g: Globals, a: argparse.Namespace) -> dict[str, Any]:
    rt = load_runtime(g)
    name = _site(g, rt)
    with for_site(name):
        gw = open_gateway(rt, name)
        site_fields = fieldload.load_site_fields(rt, name, gw)
    logical_of: dict[str, str] = {}
    mapping: dict[str, dict[str, str] | None] = {}
    for logical in LOGICAL_FIELDS:
        res = site_fields.resolve(logical)
        if res.status is Status.FOUND:
            field = res.fields[0]
            logical_of.setdefault(field.id, logical)
            mapping[logical] = {"id": field.id, "name": field.name}
        else:
            mapping[logical] = None
    fields = [
        {**f.to_document(), "logical": logical_of.get(f.id)}
        for f in sorted(site_fields.fields, key=lambda f: f.id)
    ]
    return {"site": name, "fields": fields, "mapping": mapping}


def human_fields_list(r: dict[str, Any]) -> str:
    lines = [f"{r['site']}: {len(r['fields'])} fields"]
    for f in r["fields"]:
        schema = f["schema"]
        kind = "-" if schema is None else schema.get("custom") or schema["type"]
        mapped = f" -> {f['logical']}" if f["logical"] else ""
        lines.append(f"{f['id']}  {f['name']}  [{kind}]{mapped}")
    return "\n".join(lines)


def fields_check(g: Globals, a: argparse.Namespace) -> dict[str, Any]:
    rt = load_runtime(g)
    name = _site(g, rt)
    with for_site(name):
        gw = open_gateway(rt, name)
        site_fields = fieldload.load_site_fields(rt, name, gw, needs=needs_of(rt, name))
        problems = problem_documents(site_fields)
    return {"site": name, "problems": problems}


def human_problem(p: dict[str, Any]) -> str:
    label = p.get("logical") or "site"
    text = f"{label}: {p['problem']}"
    if p.get("candidates"):
        text += " (" + "; ".join(p["candidates"]) + ")"
    return text


def human_fields_check(r: dict[str, Any]) -> str:
    if not r["problems"]:
        return f"{r['site']}: no problems"
    return "\n".join([f"{r['site']}:", *(f"  {human_problem(p)}" for p in r["problems"])])


def fields_refresh(g: Globals, a: argparse.Namespace) -> dict[str, Any]:
    rt = load_runtime(g)
    name = _site(g, rt)
    with for_site(name):
        gw = open_gateway(rt, name)
        # A failed fetch raises before the cache is touched, so the old cache stays.
        fieldload.load_fields(rt, name, gw, refresh=True)
    return {"site": name, "refreshed": True}


def human_fields_refresh(r: dict[str, Any]) -> str:
    return f"{r['site']}: field list refreshed"
