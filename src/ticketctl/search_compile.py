"""Search compilation: structured flags and named queries to one JQL string.

The rules live in docs/interface.md under "search" and "Search compilation"; this module is the
only place JQL text is built from caller input. Nothing here talks to the provider: the result is
always validated by the parse endpoint afterwards (TODO(#17)).

Public API:
    SearchFlags                                the structured flag values of one ``search``
    compile_search(flags, query=, raw_jql=, ...) the compiled JQL, or an InputError
    split_order_by(jql)                        the string-aware, balance-proving ORDER BY scanner
    lookup_named_query(queries, name, site)    a site's named query, or exit 22 ``invalid_value``
    quote_string(value)                        one double-quoted JQL string literal
    MAX_JQL                                    the 4096-character cap on any JQL text

A structured flag yields exactly one parenthesized clause or an InputError; the clauses are ANDed
in a fixed order. A named query is wrapped in parentheses and ANDed with them; a raw query
(``raw_jql``) stands alone and is returned unchanged.
"""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from .sanitize import GRAMMARS, InputError, cap_text, normalize_ticket_key, render

MAX_JQL = 4096
MAX_VALUE = 256
MAX_TEXT = 200
MAX_VALUES = 512
_MAX_TIME = 64

# Categories the sanitizer replaces: never allowed inside a value or a query.
_BAD = frozenset({"Cc", "Cf", "Cs", "Co", "Cn", "Zl", "Zp"})

STATUS_CATEGORIES = {"new": "To Do", "indeterminate": "In Progress", "done": "Done"}

# Logical ``--order-by`` names whose JQL clause name does not depend on a site's mapping. The
# others (estimate, acceptance_criteria, epic and the configured extra names) come from the
# caller through ``order_clauses``, which also overrides these.
BUILTIN_ORDER_CLAUSES = {
    "assignee": "assignee",
    "priority": "priority",
    "labels": "labels",
    "resolution": "resolution",
    "due_date": "duedate",
    "parent": "parent",
    "sprint": "sprint",
    "updated": "updated",
    "created": "created",
}
_LOGICAL = frozenset(
    {
        "estimate",
        "acceptance_criteria",
        "sprint",
        "parent",
        "epic",
        "labels",
        "priority",
        "assignee",
        "resolution",
        "due_date",
        "updated",
        "created",
    }
)
_ORDER_FIELD = re.compile(r"[A-Za-z][A-Za-z0-9_]{0,63}")
_CLAUSE_NAME = re.compile(r"[A-Za-z][A-Za-z0-9_]{0,63}|cf\[[0-9]{1,18}\]")

_DURATION = re.compile(r"([0-9]{1,4})([mhdw])")
_DATE = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}")
_RFC3339 = re.compile(
    r"([0-9]{4})-([0-9]{2})-([0-9]{2})T([0-9]{2}):([0-9]{2}):([0-9]{2})"
    r"(?:\.[0-9]{1,9})?(Z|[+-][0-9]{2}:[0-9]{2})"
)
# Possessive whitespace run: no backtracking, so a long run costs one pass.
_ORDER_BY = re.compile(r"order\s++by(?![A-Za-z0-9_])", re.IGNORECASE | re.ASCII)
_IDENT = re.compile(r"[A-Za-z0-9_]", re.ASCII)


# --------------------------------------------------------------------------------------------
# Errors
# --------------------------------------------------------------------------------------------


def _invalid_request(reason: str, message: str) -> InputError:
    return InputError(22, "invalid_request", reason, message)


def _value_error(flag: str, why: str) -> InputError:
    return _invalid_request("invalid_value", f"{flag}: {why}")


def _query_error(why: str) -> InputError:
    return _invalid_request("invalid_query", why)


def _combination(why: str) -> InputError:
    return InputError(2, "usage", "invalid_combination", why)


def _shown(value: object) -> str:
    text = value if isinstance(value, str) else repr(value)
    return cap_text(render(text), 64)[0]


# --------------------------------------------------------------------------------------------
# Strings
# --------------------------------------------------------------------------------------------


def _has_bad_char(text: str) -> bool:
    return any(unicodedata.category(c) in _BAD for c in text)


def _escape(value: str) -> str:
    """Backslash first, then the double quote."""
    return value.replace("\\", "\\\\").replace('"', '\\"')


def _dq(value: str) -> str:
    return '"' + _escape(value) + '"'


def quote_string(value: str, *, flag: str = "value") -> str:
    """One double-quoted JQL string: ``\\`` then ``"`` escaped.

    A value that is not text, is empty, holds a control, format, surrogate, private-use,
    unassigned or line-separator character, or is longer than 256 characters is exit 22
    ``invalid_value`` (the length is judged before anything else reads the value).
    """
    if not isinstance(value, str):
        raise _value_error(flag, "not text")
    if not value:
        raise _value_error(flag, "empty value")
    if len(value) > MAX_VALUE:
        raise _value_error(flag, f"longer than {MAX_VALUE} characters")
    if _has_bad_char(value):
        raise _value_error(flag, f"control or invisible character in {_shown(value)}")
    return _dq(value)


def _values(flag: str, values: Sequence[str]) -> list[str]:
    if isinstance(values, str):
        values = (values,)
    if len(values) > MAX_VALUES:
        raise _value_error(flag, f"more than {MAX_VALUES} values")
    seen: dict[str, None] = {}
    for v in values:
        if not isinstance(v, str):
            raise _value_error(flag, "not text")
        seen.setdefault(v)
    return list(seen)


def _in(clause: str, op: str, quoted: Sequence[str]) -> str:
    return f"({clause} {op} ({', '.join(quoted)}))"


# --------------------------------------------------------------------------------------------
# Time
# --------------------------------------------------------------------------------------------


def _zone(name: str | None) -> ZoneInfo:
    if name is None:
        raise _value_error("time", "the site user's time zone is unknown")
    if not GRAMMARS["time_zone"].matches(name):
        raise _value_error("time", "invalid time zone")
    try:
        return ZoneInfo(name)
    except (ValueError, OSError, KeyError):  # ZoneInfoNotFoundError is a KeyError
        raise _value_error("time", "unknown time zone") from None


def _minute(dt: datetime) -> str:
    return f"{dt.year:04d}-{dt.month:02d}-{dt.day:02d} {dt.hour:02d}:{dt.minute:02d}"


def compile_time(flag: str, value: str, time_zone: str | None) -> str:
    """The quoted JQL time operand for the one time grammar.

    A duration ``<n>m|h|d|w`` is relative (``"-5d"``); an RFC 3339 timestamp with ``Z`` or an
    offset is converted into the site user's time zone and rounded down to the minute; a bare
    date is midnight in that zone (absolute JQL dates are read there, so no conversion).
    """
    if not isinstance(value, str) or len(value) > _MAX_TIME:
        raise _value_error(flag, "not a time")
    if m := _DURATION.fullmatch(value):
        return f'"-{int(m.group(1))}{m.group(2)}"'
    if _DATE.fullmatch(value):
        try:
            d = date.fromisoformat(value)
        except ValueError:
            raise _value_error(flag, "not a valid date") from None
        return f'"{d.year:04d}-{d.month:02d}-{d.day:02d} 00:00"'
    if m := _RFC3339.fullmatch(value):
        year, month, day, hour, minute, second = (int(g) for g in m.groups()[:6])
        off = m.group(7)
        try:
            if off == "Z":
                tz = UTC
            else:
                oh, om = int(off[1:3]), int(off[4:6])
                if oh > 23 or om > 59:
                    raise ValueError
                delta = timedelta(hours=oh, minutes=om)
                tz = timezone(-delta if off[0] == "-" else delta)
            moment = datetime(year, month, day, hour, minute, second, tzinfo=tz)
        except ValueError:
            raise _value_error(flag, "not a valid timestamp") from None
        zone = _zone(time_zone)
        try:
            return f'"{_minute(moment.astimezone(zone))}"'
        except (OverflowError, ValueError):
            raise _value_error(flag, "timestamp out of range") from None
    raise _value_error(flag, "not a time (RFC 3339 timestamp, YYYY-MM-DD or <n>m|h|d|w)")


# --------------------------------------------------------------------------------------------
# Structured flags
# --------------------------------------------------------------------------------------------


_SEQUENCE_FLAGS = (
    "project",
    "status",
    "status_category",
    "assignee",
    "sprint",
    "label",
    "parent",
    "type",
    "priority",
    "key",
    "not_status",
    "not_label",
    "not_parent",
    "not_type",
)


@dataclass(frozen=True)
class SearchFlags:
    """The structured flags of one ``search``.

    Repeatable flags are sequences; a ``--*-since`` flag is one value (the CLI keeps the last).
    """

    project: Sequence[str] = ()
    status: Sequence[str] = ()
    status_category: Sequence[str] = ()
    assignee: Sequence[str] = ()
    sprint: Sequence[str] = ()
    label: Sequence[str] = ()
    parent: Sequence[str] = ()
    type: Sequence[str] = ()
    priority: Sequence[str] = ()
    key: Sequence[str] = ()
    not_status: Sequence[str] = ()
    not_label: Sequence[str] = ()
    not_parent: Sequence[str] = ()
    not_type: Sequence[str] = ()
    updated_since: str | None = None
    created_since: str | None = None
    text: str | None = None
    order_by: str | None = None

    def __post_init__(self) -> None:
        # A bare string is one value, never a sequence of characters.
        for name in _SEQUENCE_FLAGS:
            value = getattr(self, name)
            if isinstance(value, str):
                object.__setattr__(self, name, (value,))

    def any_given(self) -> bool:
        """True when any structured flag, ``--text`` or ``--order-by`` was given."""
        seq = any(len(getattr(self, name)) > 0 for name in _SEQUENCE_FLAGS)
        single = any(
            getattr(self, name) is not None
            for name in ("updated_since", "created_since", "text", "order_by")
        )
        return seq or single


def _plain(flag: str, values: Sequence[str]) -> list[str]:
    return [quote_string(v, flag=flag) for v in _values(flag, values)]


def _identifier(grammar: str, flag: str, values: Sequence[str]) -> list[str]:
    out = []
    for v in _values(flag, values):
        if grammar == "ticket_key":
            out.append(quote_string(normalize_ticket_key(v), flag=flag))
        else:
            out.append(quote_string(GRAMMARS[grammar].check(v), flag=flag))
    return out


def _assignee(values: Sequence[str]) -> str:
    terms = []
    for v in _values("--assignee", values):
        if v == "me":
            terms.append("assignee = currentUser()")
        elif v == "none":
            terms.append("assignee is EMPTY")
        else:
            terms.append(f"assignee = {_dq(GRAMMARS['account_id'].check(v))}")
    return "(" + " OR ".join(terms) + ")"


def _sprint(values: Sequence[str]) -> str:
    terms = []
    for v in _values("--sprint", values):
        if v == "open":
            terms.append("sprint in openSprints()")
        elif v == "future":
            terms.append("sprint in futureSprints()")
        else:
            terms.append(f"sprint = {GRAMMARS['numeric_id'].check(v)}")
    return "(" + " OR ".join(terms) + ")"


def _status_category(values: Sequence[str]) -> str:
    quoted = []
    for v in _values("--status-category", values):
        if v not in STATUS_CATEGORIES:
            raise _value_error("--status-category", f"unknown category {_shown(v)}")
        quoted.append(_dq(STATUS_CATEGORIES[v]))
    return _in("statusCategory", "in", quoted)


def _text(value: str) -> str:
    if not isinstance(value, str):
        raise _value_error("--text", "not text")
    if len(value) > MAX_TEXT:
        raise _value_error("--text", f"longer than {MAX_TEXT} characters")
    if not value.strip():
        raise _value_error("--text", "empty text")
    if _has_bad_char(value):
        raise _value_error("--text", "control or invisible character")
    # A Lucene phrase inside the JQL string: escape and quote the phrase, then the whole again.
    return f"(text ~ {_dq(_dq(value))})"


def _clauses(f: SearchFlags, time_zone: str | None) -> list[str]:
    out: list[str] = []
    if f.project:
        out.append(_in("project", "in", _identifier("project_key", "--project", f.project)))
    if f.status:
        out.append(_in("status", "in", _plain("--status", f.status)))
    if f.status_category:
        out.append(_status_category(f.status_category))
    if f.assignee:
        out.append(_assignee(f.assignee))
    if f.sprint:
        out.append(_sprint(f.sprint))
    if f.label:
        labels = _plain("--label", f.label)
        out.append("(" + " AND ".join(f"labels = {q}" for q in labels) + ")")
    if f.parent:
        out.append(_in("parent", "in", _identifier("ticket_key", "--parent", f.parent)))
    if f.type:
        out.append(_in("issuetype", "in", _plain("--type", f.type)))
    if f.priority:
        out.append(_in("priority", "in", _plain("--priority", f.priority)))
    if f.key:
        out.append(_in("key", "in", _identifier("ticket_key", "--key", f.key)))
    if f.not_status:
        out.append(_in("status", "not in", _plain("--not-status", f.not_status)))
    if f.not_label:
        out.append(_in("labels", "not in", _plain("--not-label", f.not_label)))
    if f.not_parent:
        out.append(_in("parent", "not in", _identifier("ticket_key", "--not-parent", f.not_parent)))
    if f.not_type:
        out.append(_in("issuetype", "not in", _plain("--not-type", f.not_type)))
    if f.updated_since is not None:
        out.append(f"(updated >= {compile_time('--updated-since', f.updated_since, time_zone)})")
    if f.created_since is not None:
        out.append(f"(created >= {compile_time('--created-since', f.created_since, time_zone)})")
    if f.text is not None:
        out.append(_text(f.text))
    return out


def _order_clause(spec: str, order_clauses: Mapping[str, str] | None) -> str:
    """``<field>[:asc|:desc]`` to ``<clause> [ASC|DESC]``."""
    if not isinstance(spec, str) or len(spec) > 80:
        raise _value_error("--order-by", "not a field")
    field, sep, direction = spec.partition(":")
    if sep and direction not in ("asc", "desc"):
        raise _value_error("--order-by", "direction must be :asc or :desc")
    mapped = dict(BUILTIN_ORDER_CLAUSES)
    mapped.update(order_clauses or {})
    if field in mapped or field in _LOGICAL:
        clause = mapped.get(field)
        if clause is None or not _CLAUSE_NAME.fullmatch(clause):
            raise _invalid_request(
                "unknown_field", f"--order-by: no field is mapped to {_shown(field)}"
            )
    elif _ORDER_FIELD.fullmatch(field):
        clause = field
    else:
        raise _value_error("--order-by", f"invalid field {_shown(field)}")
    return clause + (f" {direction.upper()}" if sep else "")


# --------------------------------------------------------------------------------------------
# The ORDER BY scanner
# --------------------------------------------------------------------------------------------


def _check_jql_text(text: str, what: str) -> None:
    """Length first (it bounds every later pass), then the character classes."""
    if not isinstance(text, str):
        raise _query_error(f"{what} is not text")
    if len(text) > MAX_JQL:
        raise _query_error(f"{what} is longer than {MAX_JQL} characters")
    for c in text:
        cat = unicodedata.category(c)
        if (
            (cat == "Cc" and c not in "\n\r\t")
            or cat in _BAD - {"Cc"}
            or (cat == "Zs" and c != " ")
        ):
            raise _query_error(f"{what} holds a control or invisible character")


def _scan(text: str) -> tuple[int, int] | None:
    """Prove ``text`` balanced and return the span of its last top-level ``ORDER BY``.

    One pass over at most 4096 characters. Double- and single-quoted strings with backslash
    escapes are skipped; parentheses never go negative and end at zero; no quote is left open.
    ``ORDER BY`` is matched by a compiled pattern at a position (never on a slice) and only
    outside strings, at parenthesis depth zero, and not inside a longer word.
    """
    depth = 0
    quote: str | None = None
    escaped = False
    span: tuple[int, int] | None = None
    i = 0
    n = len(text)
    while i < n:
        ch = text[i]
        if quote is not None:
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == quote:
                quote = None
        elif ch == '"' or ch == "'":
            quote = ch
        elif ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
            if depth < 0:
                raise _query_error("unbalanced parentheses or quote")
        elif depth == 0 and ch in "oO" and (i == 0 or not _IDENT.match(text[i - 1])):
            m = _ORDER_BY.match(text, i)
            if m:
                span = (i, m.end())
                i = m.end()
                continue
        i += 1
    if quote is not None or depth != 0:
        raise _query_error("unbalanced parentheses or quote")
    return span


def split_order_by(jql: str) -> tuple[str, str | None]:
    """Split a named query at its last top-level ``ORDER BY``: ``(where, order tail or None)``.

    ``where`` is the text before the keyword unchanged; the tail is what follows it, stripped.
    Exit 22 ``invalid_query`` for text over 4096 characters (before any scanning), for a control
    character, for an unbalanced where-part or tail, and for a keyword with no tail.
    """
    _check_jql_text(jql, "query")
    span = _scan(jql)
    if span is None:
        return jql, None
    tail = jql[span[1] :].strip()
    if not tail:
        raise _query_error("ORDER BY without a field")
    return jql[: span[0]], tail


# --------------------------------------------------------------------------------------------
# Compilation
# --------------------------------------------------------------------------------------------


def lookup_named_query(queries: Mapping[str, str], name: str, site: str) -> str:
    """A site's named query text; an unknown name is exit 22 ``invalid_value`` naming both."""
    text = queries.get(name) if isinstance(name, str) else None
    if text is None:
        raise _invalid_request(
            "invalid_value", f"unknown query {_shown(name)} for site {_shown(site)}"
        )
    return text


def compile_search(
    flags: SearchFlags | None = None,
    *,
    query: str | None = None,
    raw_jql: str | None = None,
    time_zone: str | None = None,
    order_clauses: Mapping[str, str] | None = None,
) -> str:
    """Compile one ``search`` into JQL.

    ``query`` is a named query's text (from ``lookup_named_query``); ``raw_jql`` is the text of
    ``--jql`` or ``--jql-file``. A raw query given with a structured flag, ``--text``,
    ``--order-by`` or a named query is exit 2 ``invalid_combination``; otherwise it is checked
    for length and characters only and returned unchanged (the parse endpoint judges it).

    A named query is split at its last top-level ``ORDER BY``, wrapped in parentheses and ANDed
    with one parenthesized clause per structured flag; the order clause is ``--order-by`` when
    given, else the query's. ``time_zone`` is the site user's zone (``/myself.timeZone``),
    needed only for an RFC 3339 timestamp; ``order_clauses`` maps logical ``--order-by`` names
    (a site's field mappings and extra names) to JQL clause names.

    A search with nothing to select is exit 2 ``missing_option``; the compiled whole over 4096
    characters is exit 22 ``invalid_query``.
    """
    flags = flags or SearchFlags()
    if raw_jql is not None:
        if query is not None:
            raise _combination("a raw query cannot be given with a named query")
        if flags.any_given():
            raise _combination(
                "a raw query cannot be given with a structured flag, --text or --order-by"
            )
        _check_jql_text(raw_jql, "query")
        if not raw_jql.strip():
            raise _query_error("empty query")
        return raw_jql

    where = ""
    order = ""
    if query is not None:
        where_part, tail = split_order_by(query)
        where = where_part.strip()
        order = tail or ""
    clauses = _clauses(flags, time_zone)
    if flags.order_by is not None:
        order = _order_clause(flags.order_by, order_clauses)
    parts = ([f"({where})"] if where else []) + clauses
    if not parts:
        raise InputError(2, "usage", "missing_option", "search needs a query or a filter flag")
    jql = " AND ".join(parts) + (f" ORDER BY {order}" if order else "")
    if len(jql) > MAX_JQL:
        raise _query_error(f"compiled query is longer than {MAX_JQL} characters")
    return jql
