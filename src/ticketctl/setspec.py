"""The ``--set`` / ``--field`` grammar, name resolution and the dedicated-field refusals
(docs/interface.md, Field mapping and Value grammar).

Pure logic: no I/O. The write flow fetches the metadata (``editmeta``, the transition's field list
or ``createmeta``) and the field list, then calls:

``check_offline(command, items, flag) -> list[Assignment]``
    The part that needs no provider data, run by the CLI before anything else: split each
    ``name=value`` at the first ``=``, check the name and the source form (inline, ``@path`` or
    ``-``), refuse the typed names that are dedicated by themselves (``assignee``, ``labels``,
    ``resolution``, ``sprint``, ``title``, and ``parent`` on ``create``) and a name typed twice.
    Grammar failures are exit 2 ``invalid_argument``; a refusal is exit 2 ``invalid_combination``.

``parse_meta(data) -> tuple[MetaField, ...]``
    The metadata answer (``{"fields": {id: {name, schema, required, operations,
    allowedValues}}}``) as untrusted data: entries that are not usable are ignored, names are
    sanitized and capped, allowed values are the option names.

``resolve_assignments(assignments, command=, site=, meta=) -> list[Resolved]``
    Name resolution in the documented order (logical names, ``extra`` names, then a unique
    case-insensitive display name from the metadata), then the refusal judged on the resolved
    provider field (so ``Labels``, ``Summary``, ``Assignee``, ``Sprint``, ``Resolution``,
    ``Reporter``, ``Parent`` on create and ``Description`` outside ``update`` refuse in any case),
    then a field given twice, compared by resolved field. A ``customfield_N`` id is
    ``unknown_field`` unless a mapping names it. A field that is in the provider's list but not
    in the metadata (or lacks the ``set`` operation) is ``not_editable``; one in neither is
    ``unknown_field``; an ambiguous display name is exit 21.

The conversion of the value (reading ``@path`` and ``-``, the types) is ``setconvert``.

Unverified against a real site: the shape of ``editmeta``, ``createmeta`` and the transition field
list (``fields`` keyed by id with ``name``, ``schema``, ``required``, ``operations``,
``allowedValues`` of objects with ``value`` or ``name``), that ``createmeta`` leaves out ``labels``
and ``reporter``, and the field ids ``summary``, ``description``, ``reporter`` and ``parent``.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Literal

from .config import LOGICAL_FIELDS
from .errors import TicketctlError, usage_error
from .fieldmap import (
    SPRINT_SUFFIX,
    FieldSchema,
    ProviderField,
    Resolution,
    SiteFields,
    Status,
)
from .sanitize import CAPS, GRAMMARS, cap_text, sanitize_field, sanitize_line

MAX_ASSIGNMENTS = 100
MAX_META_FIELDS = 10_000
MAX_ALLOWED_VALUES = 1000
INLINE_CAP = CAPS["body"]
NAME_CAP = CAPS["name"]

_ID = re.compile(r"[A-Za-z0-9_.:-]{1,128}")
COMMANDS = ("create", "update", "transition")

# Provider ids of the fields that are refused whatever they were typed as.
_REPORTER = "reporter"
_DEDICATED_IDS = {
    "assignee": "assignee",
    "labels": "labels",
    "resolution": "resolution",
    "summary": "summary",
    "reporter": _REPORTER,
}
# Typed names that mean a dedicated field by themselves (no metadata needed).
_TYPED = {
    "assignee": "assignee",
    "labels": "labels",
    "resolution": "resolution",
    "sprint": "sprint",
    "title": "summary",
    "parent": "parent",
}


@dataclass(frozen=True)
class Source:
    """Where a value comes from: ``inline`` text, a ``file`` path or ``stdin``."""

    kind: Literal["inline", "file", "stdin"]
    value: str = ""


@dataclass(frozen=True)
class Assignment:
    name: str
    source: Source
    flag: str = "--set"


@dataclass(frozen=True)
class MetaField:
    """One field of the metadata: the provider field plus what the metadata says about it."""

    field: ProviderField
    required: bool = False
    operations: tuple[str, ...] | None = None  # None: the metadata does not say (createmeta)
    allowed: tuple[str, ...] | None = None  # option names, None when not listed


@dataclass(frozen=True)
class Resolved:
    assignment: Assignment
    field: ProviderField
    meta: MetaField


# ---- grammar -----------------------------------------------------------------------------------


def _bad(flag: str, why: str) -> TicketctlError:
    return usage_error("invalid_argument", f"{flag} {why}")


def parse_assignment(text: str, flag: str = "--set") -> Assignment:
    """Split at the first ``=``; the value is inline text, ``@path`` or ``-``."""
    name, sep, value = text.partition("=")
    if not sep:
        raise _bad(flag, "needs name=value")
    if not name:
        raise _bad(flag, "needs a field name before =")
    if len(name) > NAME_CAP or sanitize_line(name) != name:
        raise _bad(flag, "field name is too long or has control or invisible characters")
    if len(value) > INLINE_CAP:
        raise TicketctlError(
            "invalid_request", f"{flag} value is too large", reason="invalid_value"
        )
    if value == "-":
        return Assignment(name, Source("stdin"), flag)
    if value.startswith("@"):
        if len(value) == 1:
            raise _bad(flag, "@ needs a file path")
        return Assignment(name, Source("file", value[1:]), flag)
    return Assignment(name, Source("inline", value), flag)


def _dedicated_message(key: str, command: str) -> str | None:
    """The refusal for a dedicated field on ``command``; None when the command accepts it."""
    by_command = {
        "assignee": {
            "create": "--assign",
            "update": "--assign",
            "transition": "--assign",
        },
        "labels": {
            "create": "--label",
            "update": "--add-label and --remove-label",
            "transition": "the update command's --add-label and --remove-label",
        },
        "summary": {
            "create": "--title or --title-file",
            "update": "--title or --title-file",
            "transition": "the update command's --title or --title-file",
        },
        "resolution": {
            "create": "a transition (create cannot set a resolution)",
            "update": "the transition command's --resolution",
            "transition": "--resolution",
        },
        "sprint": {
            "create": "sprint add on the created ticket (create has no --sprint)",
            "update": "sprint add and sprint remove",
            "transition": "sprint add and sprint remove",
        },
    }
    if key in by_command:
        return f"is set with {by_command[key][command]}, not through a field value"
    if key == _REPORTER:
        return "cannot be set"
    if key == "description" and command != "update":
        if command == "create":
            return "is set with --body-file on create"
        return "is set with update --set description=@file"
    if key == "parent" and command == "create":
        return "is set with --parent on create"
    return None


def _refuse(key: str, command: str, shown: str, flag: str) -> None:
    message = _dedicated_message(key, command)
    if message is not None:
        label, _ = cap_text(sanitize_line(shown), 64)
        raise usage_error("invalid_combination", f"{flag} {label}: the field {message}")


def check_offline(command: str, items: Sequence[str] | None, flag: str) -> list[Assignment]:
    """Parse ``items`` and refuse what needs no provider data; see the module docstring."""
    if command not in COMMANDS:
        raise ValueError(f"unknown command {command!r}")
    items = list(items or [])
    if len(items) > MAX_ASSIGNMENTS:
        raise _bad(flag, f"may be given at most {MAX_ASSIGNMENTS} times")
    out: list[Assignment] = []
    seen: set[str] = set()
    for item in items:
        a = parse_assignment(item, flag)
        key = _TYPED.get(a.name)
        if key is not None:
            _refuse(key, command, a.name, flag)
        if a.name in seen:
            raise usage_error(
                "invalid_combination", f"{flag} names the field {_shown(a.name)} twice"
            )
        seen.add(a.name)
        out.append(a)
    return out


def _shown(name: str) -> str:
    return cap_text(sanitize_line(name), 64)[0]


# ---- metadata ----------------------------------------------------------------------------------


def _allowed(raw: object) -> tuple[str, ...] | None:
    if not isinstance(raw, list):
        return None
    names: list[str] = []
    for item in raw[:MAX_ALLOWED_VALUES]:
        if not isinstance(item, dict):
            continue
        for key in ("value", "name"):
            value = item.get(key)
            if isinstance(value, str) and value:
                names.append(sanitize_field(value, cap=NAME_CAP)[0])
                break
    return tuple(names)


def _meta_schema(raw: object) -> FieldSchema | None:
    if not isinstance(raw, dict) or not isinstance(raw.get("type"), str) or not raw["type"]:
        return None

    def text(key: str) -> str | None:
        value = raw.get(key)
        return sanitize_field(value, cap=256)[0] if isinstance(value, str) and value else None

    return FieldSchema(sanitize_field(raw["type"], cap=128)[0], text("items"), text("custom"))


def parse_meta(data: object) -> tuple[MetaField, ...]:
    """The metadata answer as untrusted data; see the module docstring."""
    fields = data.get("fields") if isinstance(data, dict) else None
    if not isinstance(fields, dict):
        raise TicketctlError("provider_changed", "the field metadata is not an object of fields")
    if len(fields) > MAX_META_FIELDS:
        raise TicketctlError("provider_changed", "the field metadata is too long")
    out: list[MetaField] = []
    for fid, raw in fields.items():
        if not isinstance(fid, str) or not isinstance(raw, dict):
            continue
        name = raw.get("name")
        if not isinstance(name, str) or not name or not _id_ok(fid):
            continue
        ops = raw.get("operations")
        operations = tuple(o for o in ops if isinstance(o, str)) if isinstance(ops, list) else None
        field = ProviderField(
            fid,
            sanitize_field(name, cap=NAME_CAP)[0],
            GRAMMARS["custom_field_id"].matches(fid),
            _meta_schema(raw.get("schema")),
        )
        out.append(
            MetaField(
                field,
                required=raw.get("required") is True,
                operations=operations,
                allowed=_allowed(raw.get("allowedValues")),
            )
        )
    return tuple(out)


def _id_ok(fid: str) -> bool:
    return _ID.fullmatch(fid) is not None


# ---- resolution --------------------------------------------------------------------------------


def _dedicated_key(field: ProviderField, site: SiteFields) -> str | None:
    """The dedicated-field key of a resolved provider field, else None."""
    if not field.custom and field.id in _DEDICATED_IDS:
        return _DEDICATED_IDS[field.id]
    if not field.custom and field.id in ("description", "parent"):
        return field.id
    if field.schema is not None and (field.schema.custom or "").endswith(SPRINT_SUFFIX):
        return "sprint"
    sprint = site.resolve("sprint")
    if any(f.id == field.id for f in sprint.fields):
        return "sprint"
    return None


def _is_custom(fid: str) -> bool:
    return GRAMMARS["custom_field_id"].matches(fid)


def _mapped_ids(site: SiteFields) -> dict[str, ProviderField]:
    """Every field a logical or ``extra`` mapping resolved to exactly one provider field."""
    found: dict[str, ProviderField] = {}
    for name in (*LOGICAL_FIELDS, *sorted(site.config.extra)):
        res = site.resolve(name)
        if res.status is Status.FOUND:
            found.setdefault(res.fields[0].id, res.fields[0])
    return found


def _display_matches(
    typed: str, meta: Sequence[MetaField], site: SiteFields
) -> list[ProviderField]:
    """Fields whose display name equals ``typed`` ignoring case: from the metadata, else from the
    provider's field list (so a dedicated field the metadata leaves out is still refused)."""
    folded = typed.casefold()
    hits = [m.field for m in meta if m.field.name.casefold() == folded]
    return hits or [f for f in site.fields if f.name.casefold() == folded]


def resolve_assignments(
    assignments: Sequence[Assignment],
    *,
    command: str,
    site: SiteFields,
    meta: Sequence[MetaField],
) -> list[Resolved]:
    """Resolve every assignment to a provider field; see the module docstring."""
    if command not in COMMANDS:
        raise ValueError(f"unknown command {command!r}")
    by_id = {m.field.id: m for m in meta}
    mapped = _mapped_ids(site)
    out: list[Resolved] = []
    seen: dict[str, str] = {}
    for a in assignments:
        field = _resolve_one(a, site, meta, mapped)
        key = _dedicated_key(field, site)
        if key is not None:
            _refuse(key, command, a.name, a.flag)
        entry = by_id.get(field.id)
        if entry is None:
            raise _not_editable(a)
        if entry.operations is not None and "set" not in entry.operations:
            raise _not_editable(a)
        if field.id in seen:
            raise usage_error(
                "invalid_combination",
                f"{a.flag} names one field twice ({_shown(seen[field.id])} and {_shown(a.name)})",
            )
        seen[field.id] = a.name
        out.append(Resolved(a, field, entry))
    return out


def _not_editable(a: Assignment) -> TicketctlError:
    return TicketctlError(
        "invalid_request",
        f"the field {_shown(a.name)} cannot be set here",
        reason="not_editable",
        details={"field": _shown(a.name)},
    )


def _unknown(a: Assignment, why: str = "unknown field name") -> TicketctlError:
    return TicketctlError(
        "invalid_request", why, reason="unknown_field", details={"field": _shown(a.name)}
    )


def _resolve_one(
    a: Assignment,
    site: SiteFields,
    meta: Sequence[MetaField],
    mapped: dict[str, ProviderField],
) -> ProviderField:
    """The provider field of one assignment."""
    name = a.name
    if name in LOGICAL_FIELDS or name in site.config.extra:
        return site.resolve(name).need()
    if GRAMMARS["custom_field_id"].pattern.fullmatch(name.lower()):
        field = mapped.get(name.lower())
        if field is None:
            raise _unknown(a, "a customfield id is accepted only when a mapping names it")
        return field
    hits = _display_matches(name, meta, site)
    if not hits:
        raise _unknown(a)
    if len(hits) > 1:
        Resolution(name, Status.AMBIGUOUS, tuple(hits), mapped=True, extra=True).need()
    return hits[0]


__all__ = [
    "COMMANDS",
    "MAX_ASSIGNMENTS",
    "Assignment",
    "MetaField",
    "Resolved",
    "Source",
    "check_offline",
    "parse_assignment",
    "parse_meta",
    "resolve_assignments",
]
