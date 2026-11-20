"""Conversion of a provider field value by its schema (docs/interface.md, Field mapping).

``convert(schema, raw, part) -> Converted(value, incomplete)`` never raises. The supported types
are rich text (a ``string`` field holding an ADF document, rendered to Markdown), ``string``,
``number``, ``date``, ``datetime`` (UTC, ``Z``, to the second), ``option`` (its ``value``), ``user``
(an account object without any email address) and ``array`` of ``string``, ``option``, ``user`` or
of objects with a ``name`` or ``value``. A null value is a known empty value. Anything else, a
value that does not fit its schema and an unknown or missing schema give ``None`` and name
``part`` (``fields.<name>``) in ``incomplete``. Text is sanitized and capped at 4096 characters
(an extra field is single-line text, not a body); an array is cut at 1000 items; either cut names
``<part>.truncated``.

Unverified against a real site: the value shapes of option, user and array items (``value`` or
``name``), and that a rich-text custom field answers an ADF document under the ``string`` type.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass
from datetime import UTC, date, datetime
from typing import Any

from .adf_render import render_adf
from .fieldmap import FieldSchema
from .sanitize import CAPS, GRAMMARS, sanitize_field

TEXT_CAP = CAPS["title"]
MAX_ITEMS = 1000
UNKNOWN_USER = "unknown user"
_DATE = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}")
_DATETIME = re.compile(
    r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}(?::[0-9]{2}(?:\.[0-9]{1,6})?)?"
    r"(?:Z|[+-][0-9]{2}:?[0-9]{2})"
)


@dataclass(frozen=True)
class Converted:
    value: Any
    incomplete: tuple[str, ...] = ()


class _NoFit(Exception):
    """The value does not fit its schema."""


class _Cut:
    """Collects whether any text was cut."""

    def __init__(self) -> None:
        self.cut = False

    def text(self, raw: str) -> str:
        out, was_cut = sanitize_field(raw, cap=TEXT_CAP)
        self.cut = self.cut or was_cut
        return out


def _number(raw: object) -> int | float:
    if isinstance(raw, bool) or not isinstance(raw, int | float):
        raise _NoFit
    if isinstance(raw, float) and not math.isfinite(raw):
        raise _NoFit
    return raw


def _date(raw: object) -> str:
    if not isinstance(raw, str) or _DATE.fullmatch(raw) is None:
        raise _NoFit
    try:
        date.fromisoformat(raw)
    except ValueError:
        raise _NoFit from None
    return raw


def _datetime(raw: object) -> str:
    if not isinstance(raw, str) or _DATETIME.fullmatch(raw) is None:
        raise _NoFit
    try:
        parsed = datetime.fromisoformat(raw[:-1] + "+00:00" if raw.endswith("Z") else raw)
    except ValueError:
        raise _NoFit from None
    if parsed.tzinfo is None:
        raise _NoFit
    return parsed.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _user(raw: object, cut: _Cut) -> dict[str, Any]:
    if not isinstance(raw, dict):
        raise _NoFit
    account = raw.get("accountId")
    name = raw.get("displayName")
    return {
        "account_id": account if GRAMMARS["account_id"].matches(account) else None,
        "display_name": cut.text(name) if isinstance(name, str) and name else UNKNOWN_USER,
    }


def _named(raw: object, cut: _Cut) -> str:
    """An object's ``value``, else its ``name``."""
    if not isinstance(raw, dict):
        raise _NoFit
    for key in ("value", "name"):
        if isinstance(raw.get(key), str):
            return cut.text(raw[key])
    raise _NoFit


def _item(items: str | None, raw: object, cut: _Cut) -> Any:
    if items == "string":
        if not isinstance(raw, str):
            raise _NoFit
        return cut.text(raw)
    if items == "user":
        return _user(raw, cut)
    if items == "option":
        if not isinstance(raw, dict) or not isinstance(raw.get("value"), str):
            raise _NoFit
        return cut.text(raw["value"])
    if items is None or items in ("issuelinks", "issuelink"):
        raise _NoFit
    return _named(raw, cut)


def _convert(schema: FieldSchema, raw: Any, part: str, cut: _Cut) -> tuple[Any, tuple[str, ...]]:
    kind = schema.type
    if kind == "number":
        return _number(raw), ()
    if kind == "date":
        return _date(raw), ()
    if kind == "datetime":
        return _datetime(raw), ()
    if kind == "string":
        if isinstance(raw, dict) and raw.get("type") == "doc":
            rendered = render_adf(raw, part)
            return rendered.text, rendered.incomplete
        if not isinstance(raw, str):
            raise _NoFit
        return cut.text(raw), ()
    if kind == "option":
        if not isinstance(raw, dict) or not isinstance(raw.get("value"), str):
            raise _NoFit
        return cut.text(raw["value"]), ()
    if kind == "user":
        return _user(raw, cut), ()
    if kind == "array":
        if not isinstance(raw, list):
            raise _NoFit
        shown = raw[:MAX_ITEMS]
        if len(raw) > MAX_ITEMS:
            cut.cut = True
        return [_item(schema.items, item, cut) for item in shown], ()
    raise _NoFit


def convert(schema: FieldSchema | None, raw: object, part: str) -> Converted:
    if schema is None or schema.type not in _KNOWN:
        return Converted(None, (part,))
    if raw is None:
        return Converted(None)
    cut = _Cut()
    try:
        value, incomplete = _convert(schema, raw, part, cut)
    except _NoFit:
        return Converted(None, (part,))
    except Exception:  # noqa: BLE001 - the contract is "never raises" on provider data
        return Converted(None, (part,))
    marks = list(incomplete)
    if cut.cut:
        marks.append(f"{part}.truncated")
    return Converted(value, tuple(marks))


_KNOWN = frozenset({"number", "date", "datetime", "string", "option", "user", "array"})
