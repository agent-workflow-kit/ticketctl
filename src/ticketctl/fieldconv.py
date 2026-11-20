"""Conversion of a provider field value by its schema (docs/interface.md, Field mapping).

``convert(schema, raw, part) -> Converted(value, incomplete)`` never raises. The supported types
are rich text (a ``string`` field holding an ADF document, rendered to Markdown), ``string``,
``number``, ``date``, ``datetime`` (UTC, ``Z``, to the second), ``option`` (its ``value``), ``user``
(an account object built without reading ``emailAddress``) and ``array`` of ``string``,
``number``, ``date``, ``datetime``, ``option``, ``user`` or of objects with a ``name`` or
``value``. A null value is a known empty value. Anything else, a value that does not fit its
schema and an unknown or missing schema give ``None`` and name
``part`` (``fields.<name>``) in ``incomplete``. An integer above 2**63 in absolute value is
unknown. Text is sanitized and capped at 4096 characters: an extra field is single-line text, plain
or rendered from ADF. With ``body=True`` (a body-class field such as ``acceptance_criteria``) a
plain string keeps its line breaks and both plain and rendered text use the body cap; an array is
cut at 1000 items; any cut names ``<part>.truncated``.

Unverified against a real site: the value shapes of option, user and array items (``value`` or
``name``), and that a rich-text custom field answers an ADF document under the ``string`` type.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any

from .adf_render import render_adf
from .fieldmap import FieldSchema
from .sanitize import CAPS, GRAMMARS, cap_text, sanitize_field
from .timestamps import normalize_date, normalize_ts

TEXT_CAP = CAPS["title"]
BODY_CAP = CAPS["body"]
MAX_INT = 2**63
MAX_ITEMS = 1000
UNKNOWN_USER = "unknown user"


@dataclass(frozen=True)
class Converted:
    value: Any
    incomplete: tuple[str, ...] = ()


class _NoFit(Exception):
    """The value does not fit its schema."""


class _Cut:
    """Collects whether any text was cut."""

    def __init__(self, *, body: bool = False) -> None:
        self.cut = False
        self.body = body

    def text(self, raw: str, *, whole: bool = False) -> str:
        """Sanitize and cap ``raw``; ``whole`` (a body string) keeps line breaks, body cap."""
        keep = whole and self.body
        out, was_cut = sanitize_field(raw, cap=BODY_CAP if keep else TEXT_CAP, body=keep)
        self.cut = self.cut or was_cut
        return out

    def rendered(self, text: str) -> str:
        """Cap already sanitized text (rendered ADF)."""
        out, was_cut = cap_text(text, BODY_CAP if self.body else TEXT_CAP)
        self.cut = self.cut or was_cut
        return out


def _number(raw: object) -> int | float:
    if isinstance(raw, bool) or not isinstance(raw, int | float):
        raise _NoFit
    if isinstance(raw, float) and not math.isfinite(raw):
        raise _NoFit
    if isinstance(raw, int) and abs(raw) > MAX_INT:
        raise _NoFit
    return raw


def _date(raw: object) -> str:
    out = normalize_date(raw)
    if out is None:
        raise _NoFit
    return out


def _datetime(raw: object) -> str:
    out = normalize_ts(raw)
    if out is None:
        raise _NoFit
    return out


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
    if items == "number":
        return _number(raw)
    if items == "date":
        return _date(raw)
    if items == "datetime":
        return _datetime(raw)
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
            return cut.rendered(rendered.text), rendered.incomplete
        if not isinstance(raw, str):
            raise _NoFit
        return cut.text(raw, whole=True), ()
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


def convert(schema: FieldSchema | None, raw: object, part: str, *, body: bool = False) -> Converted:
    if schema is None or schema.type not in _KNOWN:
        return Converted(None, (part,))
    if raw is None:
        return Converted(None)
    cut = _Cut(body=body)
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
