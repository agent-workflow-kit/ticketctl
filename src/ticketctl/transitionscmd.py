"""Handler of ``transitions KEY`` (docs/interface.md; ``schemas/cli/transitions.json``).

One call, always live: ``GET /issue/{key}/transitions?expand=transitions.fields``. Nothing is
cached and nothing assumes that transitions are global (they are on the supported provider).
:func:`transition_object` turns one provider transition into the object of
``schemas/defs.json#transition`` and is the reader the write side (``transition KEY``) reuses.

Rules this module settles where the interface leaves room (all conservative; a transition or part
that is dropped is one stderr warning, because the schema has no ``incomplete`` list):

* A transition with an id outside the numeric id grammar, no readable name, or no readable target
  status (a name and a status category of ``new``, ``indeterminate`` or ``done``) is dropped: the
  caller could not act on it. A repeated id is kept once (the first); at most 500 are read.
* ``required_fields`` lists only fields whose ``required`` is exactly ``true``, in the provider's
  field order, with the field's display name (a name that is a provider custom field id, or
  contains one, has the id replaced: no ``customfield_N`` is printed). ``kind`` is the schema
  ``type`` of the field (at most 64 characters, ``unknown`` when missing). ``allowed_values``
  is present only when the provider listed some: each entry's ``name``, else ``value``, at most
  200 values of at most 256 characters. A required field without a readable name is listed as
  ``unknown field`` rather than hidden, because a required field that vanished would hide the
  reason a transition fails.
* ``has_screen`` is the provider's flag; a missing or non-boolean flag is ``true`` (the safe
  answer: the transition may ask for fields). A transition with no ``fields`` object has an empty
  ``required_fields`` and is named in the warning, since "none required" is then unknown.
* The assignee rule of the provider (a transition out of the backlog can need an assignee) is not
  visible here: only the provider's 400 on the write reveals it.

Unverified against a real site: that ``expand=transitions.fields`` embeds ``fields`` with
``required``, ``name``, ``schema.type`` and ``allowedValues`` entries carrying ``name`` or
``value``, and that ``hasScreen`` is present on every transition.
"""

from __future__ import annotations

import argparse
from typing import Any

from . import selection
from .errors import TicketctlError
from .flags import Globals
from .readutil import hide_field_ids, object_error, warn_incomplete
from .runtime import for_site, load_runtime, open_gateway
from .sanitize import CAPS, GRAMMARS, query, sanitize_field, segment

TRANSITIONS_TEMPLATE = "/rest/api/3/issue/{key}/transitions"
CATEGORIES = ("new", "indeterminate", "done")
MAX_TRANSITIONS = 500
MAX_FIELDS = 200
MAX_ALLOWED = 200
KIND_CAP = 64
UNKNOWN_FIELD = "unknown field"


def _text(value: object, cap: int, marks: set[str], part: str) -> str | None:
    if not isinstance(value, str):
        return None
    text, cut = sanitize_field(hide_field_ids(value), cap=cap)
    if cut:
        marks.add(f"{part}.truncated")
    return text


def _id(raw: object) -> str | None:
    if isinstance(raw, int) and not isinstance(raw, bool):
        raw = str(raw)
    if isinstance(raw, str) and GRAMMARS["numeric_id"].matches(raw):
        return raw
    return None


def _allowed(raw: object, marks: set[str]) -> list[str]:
    if not isinstance(raw, list):
        return []
    if len(raw) > MAX_ALLOWED:
        marks.add("allowed_values.truncated")
    values: list[str] = []
    for entry in raw[:MAX_ALLOWED]:
        name = entry.get("name", entry.get("value")) if isinstance(entry, dict) else entry
        text = _text(name, CAPS["name"], marks, "allowed_values")
        if text is None:
            marks.add("allowed_values")
        else:
            values.append(text)
    return values


def _required_fields(raw: object, marks: set[str]) -> list[dict[str, Any]]:
    if not isinstance(raw, dict):
        return []
    out: list[dict[str, Any]] = []
    for count, field in enumerate(raw.values()):
        if count >= MAX_FIELDS:
            marks.add("required_fields.truncated")
            break
        if not isinstance(field, dict) or field.get("required") is not True:
            continue
        name = _text(field.get("name"), CAPS["name"], marks, "required_fields")
        if not name:
            marks.add("required_fields")
            name = UNKNOWN_FIELD
        schema = field.get("schema")
        kind = _text(
            schema.get("type") if isinstance(schema, dict) else None, KIND_CAP, marks, "kind"
        )
        item: dict[str, Any] = {"name": name, "kind": kind or "unknown"}
        allowed = _allowed(field.get("allowedValues"), marks)
        if allowed:
            item["allowed_values"] = allowed
        out.append(item)
    return out


def _target(raw: object, marks: set[str]) -> dict[str, str] | None:
    if not isinstance(raw, dict):
        return None
    name = _text(raw.get("name"), CAPS["name"], marks, "to")
    category = raw.get("statusCategory")
    key = category.get("key") if isinstance(category, dict) else None
    if not name or key not in CATEGORIES:
        return None
    return {"name": name, "category": key}


def transition_object(raw: object, marks: set[str]) -> dict[str, Any] | None:
    """One transition object, or None for a transition that cannot be kept (named in ``marks``)."""
    if not isinstance(raw, dict):
        marks.add("transitions")
        return None
    tid = _id(raw.get("id"))
    name = _text(raw.get("name"), CAPS["name"], marks, "name")
    target = _target(raw.get("to"), marks)
    if tid is None or not name or target is None:
        marks.add("transitions.unreadable")
        return None
    fields = raw.get("fields")
    if not isinstance(fields, dict):
        marks.add("required_fields")
    screen = raw.get("hasScreen")
    if not isinstance(screen, bool):
        marks.add("has_screen")
        screen = True
    return {
        "id": tid,
        "name": name,
        "to": target,
        "required_fields": _required_fields(fields, marks),
        "has_screen": screen,
    }


def transition_list(data: object, marks: set[str]) -> list[dict[str, Any]]:
    raw = data.get("transitions") if isinstance(data, dict) else None
    if not isinstance(raw, list):
        raise TicketctlError("provider_changed", "the provider answered no transition list")
    if len(raw) > MAX_TRANSITIONS:
        marks.add("transitions.truncated")
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    for item in raw[:MAX_TRANSITIONS]:
        obj = transition_object(item, marks)
        if obj is None:
            continue
        if obj["id"] in seen:
            marks.add("transitions")
            continue
        seen.add(obj["id"])
        out.append(obj)
    return out


def transitions(g: Globals, a: argparse.Namespace) -> dict[str, Any]:
    rt = load_runtime(g)
    picked = selection.select_site(rt.config, site_flag=g.site, refs=[a.key])
    assert picked.key is not None
    site = picked.site
    marks: set[str] = set()
    with for_site(site):
        gw = open_gateway(rt, site)
        response = gw.request(
            "GET",
            f"/rest/api/3/issue/{segment('ticket_key', picked.key)}/transitions",
            template=TRANSITIONS_TEMPLATE,
            query=query({"expand": "transitions.fields"}),
        )
        if not 200 <= response.status < 300:
            raise object_error(response.status, "ticket")
        listed = transition_list(response.data, marks)
    warn_incomplete(rt, "the transitions", marks)
    return {"key": picked.key, "site": site, "transitions": listed}


def human_transitions(r: dict[str, Any]) -> str:
    if not r["transitions"]:
        return f"{r['key']}: no transitions are available"
    lines = [f"{r['key']}:"]
    for t in r["transitions"]:
        screen = " (screen)" if t["has_screen"] else ""
        lines.append(
            f"  {t['id']} {t['name']} -> {t['to']['name']} [{t['to']['category']}]{screen}"
        )
        for f in t["required_fields"]:
            allowed = ": " + ", ".join(f["allowed_values"]) if "allowed_values" in f else ""
            lines.append(f"    requires {f['name']} ({f['kind']}){allowed}")
    return "\n".join(lines)
