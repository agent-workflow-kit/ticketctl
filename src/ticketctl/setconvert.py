"""The value of a ``--set`` / ``--field`` assignment: reading the source and conversion by the
resolved field's schema (docs/interface.md, Value grammar).

Public API:

``convert_assignment(resolved, ctx) -> FieldValue``
    Read the source (inline text, ``@path`` or ``-``) and convert it. ``FieldValue.payload`` is the
    JSON value for the provider's ``fields`` object (``None`` clears; ``[]`` clears an array),
    ``rich`` the prepared :class:`~ticketctl.attribution.RichText` of a rich-text value.
``convert_all(resolved, ctx) -> list[FieldValue]``
``validate_label(label)``
    The rule of a label from ``--label``, ``--add-label`` and ``--remove-label``: no whitespace or
    invisible character, 1 to 255 characters, and not the reserved dedupe pattern.
``rich_unchanged(current, rich) -> bool``
    True when the ticket's current ADF has the body hash of the prepared rich text (a repeated
    rich-text ``--set`` is ``unchanged``). Any current value that is not a valid document is False.

The rules, in the order they run:

1. An empty inline value (``name=``) clears: ``None``, or ``[]`` for an array; a required field
   refuses it. A file or stdin that is empty is refused (a file cannot clear by accident).
2. A rich-text field (``description`` and ``environment``, a ``comment`` field, a custom
   ``textarea``) takes only ``@path`` or ``-``; an inline value is exit 22 ``invalid_value``. It is
   converted like a body by ``attribution.prepare_rich_text(kind="field")``, so an ``auto:`` actor
   is refused and the converter's limits apply.
3. Every other type reads its text (one trailing newline of a file or stdin is stripped), refuses a
   configured secret (``secret_in_body``) and then checks it by schema: string (one line, 4096
   characters, no control or invisible character), number, date, datetime (to UTC, ``.000+0000``),
   option and priority (matched case-insensitively against the allowed values when the metadata
   lists them; none or several matches list the candidates), user (``me``, ``none`` or an account
   id) and an array of string, option, user, component or version (a JSON array of strings, or a
   bare value for one element). Any other type is ``invalid_value``.

No error message carries the value. Errors are :class:`~ticketctl.errors.TicketctlError`.

Unverified against a real site: the payload shapes (option ``{"value"}``, priority, component and
version ``{"name"}``, user ``{"accountId"}``), the datetime string ``YYYY-MM-DDTHH:MM:SS.mmm+0000``,
that a clear is ``null`` for every type, that ADF is accepted by ``description``, ``environment``,
a ``textarea`` field and a transition ``comment`` field, and that ``textarea`` marks rich text.

The write flow (``writeflow``) supplies ``me`` and the actor; an action fetches the metadata and
compares each payload with the current value (``writeflow.rich_changed`` for rich text).
TODO(#30): ``update`` and ``transition`` call ``resolve_assignments`` and ``convert_all``.
TODO(#31): ``create`` does the same with ``createmeta``.
"""

from __future__ import annotations

import json
import re
import sys
import unicodedata
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any

from .adf_validate import DEFAULT_BODY_CHARS, body_sha256
from .attribution import RichText, prepare_rich_text
from .errors import TicketctlError
from .fieldmap import MAX_CANDIDATES, ProviderField
from .sanitize import (
    CAPS,
    GRAMMARS,
    InputError,
    cap_text,
    ensure_no_secret,
    normalize_ticket_key,
    read_input_file,
    read_input_stdin,
    sanitize_line,
)
from .setspec import Resolved

MAX_ITEMS = 1000
TEXT_CAP = CAPS["title"]
LABEL_MAX = 255
MAX_INT = 2**63
MAX_DECIMAL_DIGITS = 17
MAX_INT_DIGITS = len(str(MAX_INT))
RICH_IDS = frozenset({"description", "environment", "comment"})
TEXTAREA_SUFFIX = ":textarea"
# The object key of an option-like value, by schema type.
OBJECT_KEY = {"option": "value", "priority": "name", "component": "name", "version": "name"}
ITEM_TYPES = frozenset({"string", "user", *OBJECT_KEY})

_NUMBER = re.compile(r"-?[0-9]+(?:\.[0-9]+)?")
_DATE = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}")
_DATETIME = re.compile(
    r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}(?:\.[0-9]{1,6})?"
    r"(?:Z|[+-][0-9]{2}:[0-9]{2})"
)
_RESERVED_LABEL = re.compile(r"tc-[0-9a-f]{16}")
_BAD_LABEL_CATEGORIES = frozenset({"Cc", "Cf", "Cs", "Co", "Cn", "Zl", "Zp", "Zs"})


@dataclass(frozen=True)
class ValueContext:
    """What a conversion needs from the write flow."""

    actor: str
    template: str
    attribution_enabled: bool
    body_chars: int = DEFAULT_BODY_CHARS
    stdin: Any = None  # a text stream with ``buffer`` and ``isatty``; None is ``sys.stdin``
    me_account_id: Callable[[], str] | None = None


@dataclass(frozen=True)
class FieldValue:
    field: ProviderField
    name: str  # the field's display name (sanitized provider text)
    payload: Any
    rich: RichText | None = None
    cleared: bool = False


def _refuse(message: str, reason: str = "invalid_value", **details: Any) -> TicketctlError:
    return TicketctlError("invalid_request", message, reason=reason, details=details or None)


def _from_input_error(exc: InputError) -> TicketctlError:
    return TicketctlError(exc.code, exc.message, reason=exc.reason, details=exc.details)


# ---- classification ----------------------------------------------------------------------------


def is_rich(field: ProviderField) -> bool:
    schema = field.schema
    if schema is None:
        return False
    if schema.type == "comment" or (field.id in RICH_IDS and not field.custom):
        return True
    return schema.type == "string" and (schema.custom or "").endswith(TEXTAREA_SUFFIX)


# ---- text checks -------------------------------------------------------------------------------


def _line(text: str, what: str, cap: int = TEXT_CAP) -> str:
    if len(text) > cap:
        raise _refuse(f"the {what} is longer than {cap} characters")
    if not text.strip() or sanitize_line(text) != text:
        raise _refuse(f"the {what} must be one line of visible text")
    return text


def _number(text: str) -> int | float:
    if _NUMBER.fullmatch(text) is None:
        raise _refuse("the value is not a number (digits with an optional minus and fraction)")
    digits = sum(c.isdigit() for c in text)
    if "." not in text:
        # Counted before int(): converting a huge digit string is slow and may raise.
        if digits > MAX_INT_DIGITS:
            raise _refuse("the number is too large")
        try:
            n = int(text)
        except ValueError:
            raise _refuse("the number is too large") from None
        if abs(n) > MAX_INT:
            raise _refuse("the number is too large")
        return n
    if digits > MAX_DECIMAL_DIGITS:
        raise _refuse("the number has too many digits")
    number = float(text)
    if Decimal(repr(number)) != Decimal(text):
        raise _refuse("the number cannot be sent exactly")
    return number


def _date(text: str) -> str:
    if _DATE.fullmatch(text) is None:
        raise _refuse("the value is not a date (YYYY-MM-DD)")
    try:
        date.fromisoformat(text)
    except ValueError:
        raise _refuse("the value is not a real date") from None
    return text


def _datetime(text: str) -> str:
    if _DATETIME.fullmatch(text) is None:
        raise _refuse("the value is not an RFC 3339 datetime with Z or an offset")
    try:
        parsed = datetime.fromisoformat(text[:-1] + "+00:00" if text.endswith("Z") else text)
    except ValueError:
        raise _refuse("the value is not a real datetime") from None
    try:
        utc = parsed.astimezone(UTC)
    except (OverflowError, ValueError):
        raise _refuse("the datetime is outside the supported range") from None
    return f"{utc:%Y-%m-%dT%H:%M:%S}.{utc.microsecond // 1000:03d}+0000"


def _candidates(names: Sequence[str]) -> list[str]:
    return sorted(set(names))[:MAX_CANDIDATES]


def _match(text: str, allowed: tuple[str, ...] | None) -> str:
    """The canonical allowed value for ``text`` (case-insensitive), or ``text`` when not listed."""
    text = _line(text, "value", CAPS["name"])
    if allowed is None:
        return text
    folded = text.casefold()
    hits = sorted({a for a in allowed if a.casefold() == folded})
    if len(hits) == 1:
        return hits[0]
    if hits:
        raise _refuse("several allowed values match", candidates=_candidates(hits))
    raise _refuse("no allowed value matches", candidates=_candidates(allowed))


def _user(text: str, ctx: ValueContext) -> dict[str, str] | None:
    if text == "none":
        return None
    if text == "me":
        if ctx.me_account_id is None:
            raise TicketctlError("internal", "the write flow gave no identity for the value me")
        return {"accountId": ctx.me_account_id()}
    if text.casefold() in ("me", "none"):
        raise _refuse("the value is not me, none or an account id (me and none are lower case)")
    if not GRAMMARS["account_id"].matches(text):
        raise _refuse("the value is not me, none or an account id")
    return {"accountId": text}


def _items(text: str) -> list[str]:
    if not text.startswith("["):
        return [text]
    try:
        data = json.loads(text)
    except (ValueError, RecursionError):
        raise _refuse("the value is not a JSON array of strings") from None
    if not isinstance(data, list) or not all(isinstance(i, str) for i in data):
        raise _refuse("the value is not a JSON array of strings")
    if len(data) > MAX_ITEMS:
        raise _refuse(f"the array has more than {MAX_ITEMS} items")
    return data


def _distinct(items: list[Any]) -> list[Any]:
    """The items without repeats, in order (a list field holds a set)."""
    seen: set[str] = set()
    out: list[Any] = []
    for item in items:
        mark = json.dumps(item, sort_keys=True)
        if mark not in seen:
            seen.add(mark)
            out.append(item)
    return out


def _item(kind: str, text: str, allowed: tuple[str, ...] | None, ctx: ValueContext) -> Any:
    if kind == "string":
        return _line(text, "item", CAPS["label"])
    if kind == "user":
        user = _user(text, ctx)
        if user is None:
            raise _refuse("none is not an item of a list of users")
        return user
    return {OBJECT_KEY[kind]: _match(text, allowed)}


def _convert_text(r: Resolved, text: str, ctx: ValueContext) -> Any:
    schema = r.field.schema
    assert schema is not None
    kind = schema.type
    allowed = r.meta.allowed
    if kind == "string":
        return _line(text, "value")
    if kind == "number":
        return _number(text)
    if kind == "date":
        return _date(text)
    if kind == "datetime":
        return _datetime(text)
    if kind in OBJECT_KEY:
        return {OBJECT_KEY[kind]: _match(text, allowed)}
    if kind == "issuelink":
        try:
            return {"key": normalize_ticket_key(text)}
        except InputError:
            raise _refuse("the value is not a ticket key") from None
    if kind == "user":
        return _user(text, ctx)
    if kind == "array" and schema.items in ITEM_TYPES:
        items = _items(text)
        if not items:
            raise _refuse("the array is empty; use name= to clear")
        converted = [_item(schema.items, i, allowed, ctx) for i in items]
        return _distinct(converted)
    shown = cap_text(sanitize_line(kind), 64)[0]
    raise _refuse(f"fields of type {shown} are not supported by --set")


# ---- sources -----------------------------------------------------------------------------------


def _read(r: Resolved, ctx: ValueContext, *, body: bool) -> str:
    source = r.assignment.source
    kind = "body" if body else "title"
    if source.kind == "stdin":
        stream = sys.stdin if ctx.stdin is None else ctx.stdin
        text = read_input_stdin(stream, kind=kind, body_chars=ctx.body_chars)
    else:
        text = read_input_file(source.value, kind=kind, body_chars=ctx.body_chars)
    if not body:
        # One trailing newline is the file's line end, not part of a scalar.
        text = text.removesuffix("\r\n") if text.endswith("\r\n") else text.removesuffix("\n")
        if not text:
            raise _refuse("the file or stdin is empty; use name= to clear the field")
    return text


def _strings(value: Any) -> list[str]:
    """Every string in a converted payload or ADF document, keys included."""
    out: list[str] = []
    stack = [value]
    while stack:
        item = stack.pop()
        if isinstance(item, str):
            out.append(item)
        elif isinstance(item, dict):
            stack.extend(item.keys())
            stack.extend(item.values())
        elif isinstance(item, list):
            stack.extend(item)
    out.reverse()
    return out


def _text_nodes(node: Any) -> list[str]:
    """The text of every ``text`` node of an ADF document, in order."""
    if isinstance(node, list):
        return [t for child in node for t in _text_nodes(child)]
    if not isinstance(node, dict):
        return []
    own = [node["text"]] if node.get("type") == "text" and isinstance(node.get("text"), str) else []
    return own + _text_nodes(node.get("content"))


def _clear(r: Resolved) -> FieldValue:
    if r.meta.required:
        raise _refuse("a required field cannot be cleared")
    schema = r.field.schema
    payload: Any = [] if schema is not None and schema.type == "array" else None
    return FieldValue(r.field, r.field.name, payload, None, True)


def convert_assignment(r: Resolved, ctx: ValueContext) -> FieldValue:
    """Read and convert one resolved assignment; see the module docstring."""
    try:
        return _convert(r, ctx)
    except InputError as exc:
        raise _from_input_error(exc) from None


def _convert(r: Resolved, ctx: ValueContext) -> FieldValue:
    source = r.assignment.source
    if source.kind == "inline" and source.value == "":
        return _clear(r)
    if r.field.schema is None:
        raise _refuse("the field's type is unknown, so its value cannot be converted")
    if is_rich(r.field):
        if source.kind == "inline":
            raise _refuse("a rich-text value needs @path or - (an inline value is refused)")
        body = _read(r, ctx, body=True)
        ensure_no_secret(body)
        rich = prepare_rich_text(
            body,
            actor=ctx.actor,
            kind="field",
            template=ctx.template,
            enabled=ctx.attribution_enabled,
            body_chars=ctx.body_chars,
        )
        # Markdown escapes and marks can turn text that is not the secret into text that is.
        ensure_no_secret(*_strings(rich.doc), "".join(_text_nodes(rich.doc)))
        return FieldValue(r.field, r.field.name, rich.doc, rich, False)
    text = source.value if source.kind == "inline" else _read(r, ctx, body=False)
    ensure_no_secret(text)
    payload = _convert_text(r, text, ctx)
    # A decoded JSON item can hold a secret the raw text did not show (an escape).
    ensure_no_secret(*_strings(payload))
    cleared = payload is None
    if cleared and r.meta.required:
        raise _refuse("a required field cannot be cleared")
    return FieldValue(r.field, r.field.name, payload, None, cleared)


def convert_all(resolved: Sequence[Resolved], ctx: ValueContext) -> list[FieldValue]:
    return [convert_assignment(r, ctx) for r in resolved]


# ---- labels and unchanged ----------------------------------------------------------------------


def validate_label(label: str) -> str:
    """Return ``label`` when a provider would take it; else exit 22 ``invalid_value``."""
    if not isinstance(label, str) or not label:
        raise _refuse("a label cannot be empty")
    if len(label) > LABEL_MAX:
        raise _refuse(f"a label is at most {LABEL_MAX} characters")
    if any(c.isspace() or unicodedata.category(c) in _BAD_LABEL_CATEGORIES for c in label):
        raise _refuse("a label cannot contain whitespace or invisible characters")
    if _RESERVED_LABEL.fullmatch(label):
        raise _refuse("this label pattern is reserved for the dedupe key")
    return label


def rich_unchanged(current: object, rich: RichText) -> bool:
    if rich.sha256 is None or not isinstance(current, dict):
        return False
    try:
        return body_sha256(current) == rich.sha256
    except Exception:  # noqa: BLE001 - provider data of any shape is "changed"
        return False


__all__ = [
    "FieldValue",
    "ValueContext",
    "convert_all",
    "convert_assignment",
    "rich_unchanged",
    "validate_label",
]
