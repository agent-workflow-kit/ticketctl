"""Field writes shared by ``update`` and, later, ``create`` (docs/interface.md, Field mapping, Value
grammar and Plan object).

Pure logic plus one read helper; it sends nothing. A command resolves its ``--set`` assignments
against its metadata (``setspec.resolve_assignments``: ``editmeta`` for ``update``, ``createmeta``
for ``create``), then:

``convert_edits(resolved, site_fields, ctx) -> list[FieldEdit]``
    Converts every value (``setconvert.convert_all``) and names the edit the way a plan does: the
    logical or ``extra`` name when the field is mapped to one, ``description`` for the description,
    else the field's display name (a custom field id inside it shows as ``<id>``).
``same_value(edit, raw) -> bool``
    The canonical equality of the converted value and the ticket's current raw value (the
    ``unchanged`` test): rich text as rendered Markdown, numbers as decimals, dates and datetimes
    as normalized strings, options, users and links by name or account id, lists as sorted sets.
    A current value that cannot be read is never equal.
``fact(edit, raw)``, ``display_from(edit, raw)`` and ``display_to(edit)``
    The hash fact (the canonical current value; rich text as the sha256 of its rendered text), and
    the ``from`` and ``to`` of a plan change.
``fields_body(edits)``
    ``{field id: payload}`` for the provider's ``fields`` object.
``read_object(ctx, path, template, query=...)``
    A GET of an object that must answer a JSON object (404 is ``not_found``, any other failure
    ``provider_changed``).

Canonical equality is the one rule of the interface (Plan object, rule 1), so the value written
here and the value read back compare the same way whatever the provider's formatting.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from typing import Any

from .adf_render import render_adf
from .config import LOGICAL_FIELDS
from .errors import TicketctlError
from .fieldmap import ProviderField, SiteFields, Status
from .setconvert import FieldValue, ValueContext, convert_all, is_rich
from .setspec import Resolved
from .timestamps import MAX_LENGTH, normalize_date
from .writeflow import Context

_FIELD_ID = re.compile(r"customfield_[0-9]+")
_STAMP = re.compile(
    r"([0-9]{4})-([0-9]{2})-([0-9]{2})T([0-9]{2}):([0-9]{2})(?::([0-9]{2})(?:\.([0-9]{1,9}))?)?"
    r"(?:(Z)|([+-])([0-9]{2}):?([0-9]{2}))"
)
_OBJECT_KEYS = ("accountId", "value", "name", "key")
UNREADABLE: dict[str, Any] = {"unreadable": True}


class _Unreadable(Exception):
    """A provider value of a shape the canonical form cannot hold."""


@dataclass(frozen=True)
class FieldEdit:
    """One converted ``--set`` assignment: its plan name and the value to send."""

    name: str
    value: FieldValue

    @property
    def field(self) -> ProviderField:
        return self.value.field

    @property
    def rich(self) -> bool:
        return is_rich(self.field)


def _millis(raw: object) -> str | None:
    """A provider datetime as UTC to the millisecond (the precision ``--set`` sends), else None.

    Extra fractional digits are cut, not rounded, the way the converter cuts them.
    """
    if not isinstance(raw, str) or len(raw) > MAX_LENGTH:
        return None
    match = _STAMP.fullmatch(raw)
    if match is None:
        return None
    year, month, day, hour, minute, second, frac, zulu, sign, off_h, off_m = match.groups()
    try:
        if zulu:
            zone = UTC
        else:
            hours, minutes = int(off_h), int(off_m)
            if hours > 23 or minutes > 59:
                return None
            delta = timedelta(hours=hours, minutes=minutes)
            zone = timezone(-delta if sign == "-" else delta)
        local = datetime(
            int(year), int(month), int(day), int(hour), int(minute), int(second or 0), tzinfo=zone
        )
        utc = local.astimezone(UTC)
    except (ValueError, OverflowError):
        return None
    ms = int((frac or "0").ljust(3, "0")[:3])
    return f"{utc:%Y-%m-%dT%H:%M:%S}.{ms:03d}Z"


# ---- naming and conversion ---------------------------------------------------------------------


def edit_name(resolved: Resolved, site: SiteFields) -> str:
    """The name a plan shows for a resolved field (never a bare ``customfield_N``)."""
    typed = resolved.assignment.name
    if typed in LOGICAL_FIELDS or typed in site.config.extra:
        return typed
    for name in (*LOGICAL_FIELDS, *sorted(site.config.extra)):
        found = site.resolve(name)
        if found.status is Status.FOUND and found.fields[0].id == resolved.field.id:
            return name
    if not resolved.field.custom and resolved.field.id == "description":
        return "description"
    return _FIELD_ID.sub("<id>", resolved.field.name)


def convert_edits(resolved: list[Resolved], site: SiteFields, ctx: ValueContext) -> list[FieldEdit]:
    values = convert_all(resolved, ctx)
    return [FieldEdit(edit_name(r, site), v) for r, v in zip(resolved, values, strict=True)]


def fields_body(edits: list[FieldEdit]) -> dict[str, Any]:
    return {e.field.id: e.value.payload for e in edits}


# ---- canonical form ----------------------------------------------------------------------------


def _decimal(raw: int | float) -> str:
    try:
        number = Decimal(str(raw))
    except InvalidOperation:
        raise _Unreadable from None
    if not number.is_finite():
        raise _Unreadable
    text = format(number.normalize(), "f")
    return "0" if text in ("-0", "") else text


def _sort_key(item: Any) -> str:
    return repr(item)


def canon(raw: object, *, depth: int = 0) -> Any:
    """JSON-safe canonical form of a provider value or a payload; ``_Unreadable`` when it has none.

    ``None`` and ``[]`` are both the empty value; a number is its decimal string; an object is its
    account id, ``value``, ``name`` or ``key`` (the first present string); a list is its sorted
    items.
    """
    if depth > 4:
        raise _Unreadable
    if raw is None:
        return None
    if isinstance(raw, bool):
        return raw
    if isinstance(raw, int | float):
        return _decimal(raw)
    if isinstance(raw, str):
        return raw
    if isinstance(raw, dict):
        for key in _OBJECT_KEYS:
            value = raw.get(key)
            if isinstance(value, str):
                return value
        raise _Unreadable
    if isinstance(raw, list):
        if not raw:
            return None
        items = {_sort_key(c): c for c in (canon(i, depth=depth + 1) for i in raw)}
        return [items[k] for k in sorted(items)]
    raise _Unreadable


def _canon_typed(edit: FieldEdit, raw: object) -> Any:
    schema = edit.field.schema
    kind = schema.type if schema is not None else None
    if edit.rich:
        if raw is None:
            return None
        if not isinstance(raw, dict):
            raise _Unreadable
        rendered = render_adf(raw)
        if rendered.incomplete:
            raise _Unreadable
        return rendered.text or None
    if kind == "number" and raw is not None:
        if isinstance(raw, bool) or not isinstance(raw, int | float):
            raise _Unreadable  # a string in a number field does not fit its schema
        return _decimal(raw)
    if kind == "datetime" and raw is not None:
        out = _millis(raw)
        if out is None:
            raise _Unreadable
        return out
    if kind == "date" and raw is not None:
        out = normalize_date(raw)
        if out is None:
            raise _Unreadable
        return out
    if kind == "string" and raw is not None and not isinstance(raw, str):
        raise _Unreadable  # a string field holds text
    return canon(raw)


def _target(edit: FieldEdit) -> Any:
    rich = edit.value.rich
    if edit.rich:
        return None if rich is None else render_adf(rich.doc).text or None
    return _canon_typed(edit, edit.value.payload)


def same_value(edit: FieldEdit, raw: object) -> bool:
    """True when the ticket's current ``raw`` value already equals the value to send."""
    try:
        return bool(_canon_typed(edit, raw) == _target(edit))
    except _Unreadable:
        return False


# ---- facts and plan values ---------------------------------------------------------------------


def fact(edit: FieldEdit, raw: object) -> Any:
    """The hash fact of a field: its canonical current value (rich text: sha256 of its text)."""
    try:
        value = _canon_typed(edit, raw)
    except _Unreadable:
        return UNREADABLE
    if edit.rich and value is not None:
        return hashlib.sha256(str(value).encode("utf-8", "surrogatepass")).hexdigest()
    return value


def _shown(edit: FieldEdit, value: Any) -> Any:
    """A canonical value as a plan shows it: a number stays a number."""
    schema = edit.field.schema
    if schema is not None and schema.type == "number" and isinstance(value, str) and not edit.rich:
        try:
            number = Decimal(value)
        except InvalidOperation:
            return value
        return int(number) if number == number.to_integral_value() else float(number)
    return value


def display_from(edit: FieldEdit, raw: object) -> Any:
    try:
        return _shown(edit, _canon_typed(edit, raw))
    except _Unreadable:
        return None


def display_to(edit: FieldEdit) -> Any:
    try:
        return _shown(edit, _target(edit))
    except _Unreadable:  # a converted payload always has a canonical form
        raise TicketctlError("internal", "a converted value has no canonical form") from None


# ---- reading -----------------------------------------------------------------------------------


def read_object(ctx: Context, path: str, template: str, *, query: str = "") -> dict[str, Any]:
    """GET an object a write needs: 404 is ``not_found``, anything unusable ``provider_changed``."""
    response = ctx.gw.request("GET", path, template=template, query=query)
    if response.status == 404:
        raise TicketctlError("not_found", "the ticket is missing or you have no permission")
    data = response.data
    if not 200 <= response.status < 300 or not isinstance(data, dict):
        raise TicketctlError("provider_changed", "the provider answered no usable object")
    return data


__all__ = [
    "UNREADABLE",
    "FieldEdit",
    "canon",
    "convert_edits",
    "display_from",
    "display_to",
    "edit_name",
    "fact",
    "fields_body",
    "read_object",
    "same_value",
]
