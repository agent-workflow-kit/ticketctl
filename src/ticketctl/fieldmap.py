"""The provider's field list and the logical field mapping (docs/interface.md, Field mapping).

Pure logic, no I/O (``fieldload`` fetches and caches the list, ``fieldconv`` converts values).

Public API:

``parse_field_list(data, strict=False) -> ParsedFields``
    The provider's ``GET /field`` answer (or the cached copy) as :class:`ProviderField` objects.
    The list is untrusted: a non-list or one over ``MAX_FIELDS`` entries is a
    :class:`FieldListError`; a malformed entry (no usable id or name, an id outside the grammar)
    (including a ``custom`` that is not a boolean) is ignored and counted; every entry of a
    duplicated id is dropped (two fields cannot share an id, so the list is not believed for it);
    names and schema strings are sanitized, scrubbed and capped. ``strict`` (the cache read)
    refuses a list that needed any of that and any entry that is not exactly the stored document
    shape (so a changed name, schema or ``custom`` is a poisoned cache).

``resolve(fields, logical, mapping) -> Resolution``
    One logical name against the list. A configured mapping (``name`` exact and case-sensitive,
    ``id`` exact, ``schema`` equal to ``schema.custom`` or else ``schema.type``) wins over
    discovery, even when it finds nothing (``MISSING``). Without a mapping: ``sprint``, ``epic``
    and ``estimate`` are discovered by schema (``estimate``: the story-point schema united with
    the exact name ``Story Points`` on the generic float schema), the system fields by their fixed
    ids, and ``acceptance_criteria`` never (``ABSENT``).

``Resolution.need()``
    The lazy refusal: exit 21 ``ambiguous_field`` (candidate display names, never ids) or exit 22
    ``unknown_field``. Reads call it only for what they print; a write calls it for its field.

``SiteFields(fields, config_fields)``
    The list plus one site's ``fields`` config: ``resolve(name)`` for a logical or ``extra`` name
    (a logical name wins) and ``problems()`` for ``fields check`` (``missing``, ``ambiguous``,
    ``type_mismatch``).

Unverified against a real site: that a discovered system field has the fixed id used here, the
exact legacy name ``Story Points``, and that a sprint field's schema is an array of ``json``.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from .config import LOGICAL_FIELDS, FieldMapping, Fields
from .errors import TicketctlError
from .sanitize import CAPS, cap_text, sanitize_field

MAX_FIELDS = 10_000
MAX_CANDIDATES = 50
_ID = re.compile(r"[A-Za-z0-9_.:-]{1,128}")
_SCHEMA_TYPE_CAP = 128
_SCHEMA_CUSTOM_CAP = 256
_FIELD_ID = re.compile(r"customfield_[0-9]+")
ID_MARKER = "<id>"

STORY_POINTS_SUFFIX = ":jsw-story-points"
FLOAT_SUFFIX = "customfieldtypes:float"
LEGACY_POINTS_NAME = "Story Points"
SPRINT_SUFFIX = ":gh-sprint"
EPIC_SUFFIX = ":gh-epic-link"
# Logical names that are provider system fields, with their fixed ids.
SYSTEM_IDS = {
    "parent": "parent",
    "labels": "labels",
    "priority": "priority",
    "assignee": "assignee",
    "resolution": "resolution",
    "due_date": "duedate",
}


class FieldListError(ValueError):
    """The field list is not usable at all (not a list, too long, or strictly invalid)."""


@dataclass(frozen=True)
class FieldSchema:
    type: str
    items: str | None = None
    custom: str | None = None

    @property
    def key(self) -> str:
        """The schema string a mapping names: ``custom`` for custom fields, else ``type``."""
        return self.custom or self.type


@dataclass(frozen=True)
class ProviderField:
    id: str
    name: str
    custom: bool
    schema: FieldSchema | None

    @property
    def schema_key(self) -> str | None:
        return None if self.schema is None else self.schema.key

    def to_document(self) -> dict[str, Any]:
        """The cached shape (and the ``fields list`` shape): ``schema`` is null when unknown."""
        schema: dict[str, str] | None = None
        if self.schema is not None:
            schema = {"type": self.schema.type}
            if self.schema.items is not None:
                schema["items"] = self.schema.items
            if self.schema.custom is not None:
                schema["custom"] = self.schema.custom
        return {"id": self.id, "name": self.name, "custom": self.custom, "schema": schema}


@dataclass(frozen=True)
class ParsedFields:
    fields: tuple[ProviderField, ...]
    ignored: int


def _text(value: object, cap: int) -> str | None:
    if not isinstance(value, str) or not value:
        return None
    return sanitize_field(value, cap=cap)[0]


def _schema(raw: object) -> FieldSchema | None:
    if not isinstance(raw, dict):
        return None
    type_ = _text(raw.get("type"), _SCHEMA_TYPE_CAP)
    if type_ is None:
        return None
    return FieldSchema(
        type_,
        _text(raw.get("items"), _SCHEMA_TYPE_CAP),
        _text(raw.get("custom"), _SCHEMA_CUSTOM_CAP),
    )


def _entry(raw: object) -> ProviderField | None:
    if not isinstance(raw, dict):
        return None
    fid = raw.get("id")
    name = raw.get("name")
    if not isinstance(fid, str) or _ID.fullmatch(fid) is None or not isinstance(name, str):
        return None
    if "custom" in raw and not isinstance(raw["custom"], bool):
        return None
    return ProviderField(
        fid,
        sanitize_field(name, cap=CAPS["name"])[0],
        raw.get("custom") is True,
        _schema(raw.get("schema")),
    )


def parse_field_list(data: object, *, strict: bool = False) -> ParsedFields:
    if not isinstance(data, list):
        raise FieldListError("the field list is not a list")
    if len(data) > MAX_FIELDS:
        raise FieldListError("the field list is too long")
    entries = [_entry(raw) for raw in data]
    counts: dict[str, int] = {}
    for item in entries:
        if item is not None:
            counts[item.id] = counts.get(item.id, 0) + 1
    kept = tuple(f for f in entries if f is not None and counts[f.id] == 1)
    ignored = len(data) - len(kept)
    if strict and ignored:
        raise FieldListError("the field list holds entries that are not valid")
    if strict and [f.to_document() for f in kept] != data:
        raise FieldListError("the field list holds entries that are not as stored")
    return ParsedFields(kept, ignored)


class Status(StrEnum):
    FOUND = "found"
    AMBIGUOUS = "ambiguous"
    MISSING = "missing"  # a mapping names no field of the list
    ABSENT = "absent"  # no mapping and discovery found nothing


@dataclass(frozen=True)
class Resolution:
    logical: str
    status: Status
    fields: tuple[ProviderField, ...] = ()
    mapped: bool = False
    extra: bool = False

    def candidate_labels(self) -> list[str]:
        """Display names of the candidates (sorted); a shared name gets its schema appended."""
        cap = CAPS["name"]
        names = [f.name for f in self.fields]
        labels = []
        for f in self.fields:
            name = _FIELD_ID.sub(ID_MARKER, f.name)
            if names.count(f.name) > 1:
                key = _FIELD_ID.sub(ID_MARKER, f.schema_key or "no schema")
                suffix = f" ({cap_text(key, cap // 2)[0]})"
                name = cap_text(name, cap - len(suffix))[0] + suffix
            labels.append(cap_text(name, cap)[0])
        return sorted(labels)

    def _hint(self, site: str | None) -> str:
        where = f"sites.{site or '<site>'}.fields"
        return f"{where}.extra.{self.logical}" if self.extra else f"{where}.{self.logical}"

    def need(self, site: str | None = None) -> ProviderField:
        if self.status is Status.FOUND:
            return self.fields[0]
        if self.status is Status.AMBIGUOUS:
            raise TicketctlError(
                "ambiguous_field",
                f"{self.logical} matches {len(self.fields)} fields; "
                f"map one under {self._hint(site)} (by name, id or schema)",
                details={"candidates": self.candidate_labels()[:MAX_CANDIDATES]},
            )
        if self.status is Status.MISSING:
            message = f"the field mapped for {self.logical} is not in the provider's field list"
        else:
            message = f"{self.logical} is not mapped and no field was discovered for it"
        raise TicketctlError(
            "invalid_request", message, reason="unknown_field", details={"field": self.logical}
        )


def _discover(fields: Sequence[ProviderField], logical: str) -> list[ProviderField]:
    def custom(f: ProviderField) -> str:
        return f.schema.custom or "" if f.schema is not None else ""

    if logical == "sprint":
        return [f for f in fields if custom(f).endswith(SPRINT_SUFFIX)]
    if logical == "epic":
        return [f for f in fields if custom(f).endswith(EPIC_SUFFIX)]
    if logical == "estimate":
        return [
            f
            for f in fields
            if custom(f).endswith(STORY_POINTS_SUFFIX)
            or (custom(f).endswith(FLOAT_SUFFIX) and f.name == LEGACY_POINTS_NAME)
        ]
    if logical in SYSTEM_IDS:
        return [f for f in fields if f.id == SYSTEM_IDS[logical] and not f.custom]
    return []  # acceptance_criteria: only by configured mapping


def _matching(fields: Sequence[ProviderField], mapping: FieldMapping) -> list[ProviderField]:
    if mapping.id is not None:
        return [f for f in fields if f.id == mapping.id]
    if mapping.name is not None:
        return [f for f in fields if f.name == mapping.name]
    return [f for f in fields if f.schema_key == mapping.schema_]


def _result(
    logical: str, found: list[ProviderField], *, mapped: bool, extra: bool = False
) -> Resolution:
    if len(found) == 1:
        status = Status.FOUND
    elif found:
        status = Status.AMBIGUOUS
    else:
        status = Status.MISSING if mapped else Status.ABSENT
    return Resolution(logical, status, tuple(found), mapped, extra)


def resolve(
    fields: Sequence[ProviderField], logical: str, mapping: FieldMapping | None
) -> Resolution:
    if logical not in LOGICAL_FIELDS:
        raise ValueError(f"{logical!r} is not a logical field name")
    if mapping is not None:
        return _result(logical, _matching(fields, mapping), mapped=True)
    return _result(logical, _discover(fields, logical), mapped=False)


# The type rule of `fields check` (type_mismatch); logical fields without a rule never mismatch.
def _accepts(logical: str, schema: FieldSchema | None) -> bool:
    if logical not in ("estimate", "sprint", "acceptance_criteria", "due_date"):
        return True
    if schema is None:
        return False
    if logical == "estimate":
        return schema.type == "number"
    if logical == "sprint":
        return schema.type == "array" and schema.items in ("json", "sprint")
    if logical == "acceptance_criteria":
        return schema.type == "string"
    return schema.type == "date"


@dataclass(frozen=True)
class Problem:
    logical: str
    problem: str  # missing, ambiguous or type_mismatch
    candidates: list[str]


class SiteFields:
    """The field list with one site's ``fields`` configuration."""

    def __init__(
        self, fields: Sequence[ProviderField], config: Fields, source: str = "provider"
    ) -> None:
        self.fields = tuple(fields)
        self.config = config
        self.source = source  # "cache" or "provider": where the list came from

    def resolve(self, name: str) -> Resolution:
        if name in LOGICAL_FIELDS:
            return resolve(self.fields, name, getattr(self.config, name))
        mapping = self.config.extra.get(name)
        if mapping is None:
            raise TicketctlError(
                "invalid_request",
                "unknown field name",
                reason="unknown_field",
                details={"field": name},
            )
        return _result(name, _matching(self.fields, mapping), mapped=True, extra=True)

    def problems(self) -> list[Problem]:
        found: list[Problem] = []
        names = [*LOGICAL_FIELDS, *sorted(self.config.extra)]
        for name in names:
            res = self.resolve(name)
            if res.status is Status.MISSING:
                found.append(Problem(name, "missing", []))
            elif res.status is Status.AMBIGUOUS:
                labels = res.candidate_labels()[:MAX_CANDIDATES]
                found.append(Problem(name, "ambiguous", labels))
            elif (
                res.status is Status.FOUND
                and name in LOGICAL_FIELDS
                and not _accepts(name, res.fields[0].schema)
            ):
                found.append(Problem(name, "type_mismatch", []))
        return found


__all__ = [
    "MAX_CANDIDATES",
    "MAX_FIELDS",
    "FieldListError",
    "FieldSchema",
    "ParsedFields",
    "Problem",
    "ProviderField",
    "Resolution",
    "SiteFields",
    "Status",
    "parse_field_list",
    "resolve",
]
