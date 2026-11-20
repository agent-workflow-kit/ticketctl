"""Search compilation: structured flags, named queries and the ORDER BY scanner (issue #16)."""

from __future__ import annotations

import random
import string
import time

import pytest

from ticketctl import search_compile as sc
from ticketctl.sanitize import InputError

TZ = "America/New_York"


def flags(**kw):
    return sc.SearchFlags(**kw)


def compile_(f=None, **kw):
    kw.setdefault("time_zone", TZ)
    return sc.compile_search(f or flags(), **kw)


def err(exc: pytest.ExceptionInfo[InputError]) -> tuple[int, str, str]:
    e = exc.value
    return e.exit_code, e.code, e.reason


# --- string escaping ---------------------------------------------------------------------------


def test_escape_backslash_then_quote() -> None:
    assert sc.quote_string('a\\"b') == '"a\\\\\\"b"'


def test_quote_rejects_control_and_long_and_empty() -> None:
    for bad in [
        "a\nb",
        "a\x00b",
        "a\rb",
        "a\tb",
        "a\u2028b",
        "a\u202eb",
        "x" * 257,
        "",
        "a\ud800b",
    ]:
        with pytest.raises(InputError) as exc:
            sc.quote_string(bad)
        assert err(exc) == (22, "invalid_request", "invalid_value")


def test_quote_accepts_256_and_unicode() -> None:
    assert sc.quote_string("é" * 256) == '"' + "é" * 256 + '"'


# --- single flags ------------------------------------------------------------------------------


def test_project_or() -> None:
    assert compile_(flags(project=("ABC", "DEF"))) == '(project in ("ABC", "DEF"))'


def test_project_grammar() -> None:
    with pytest.raises(InputError) as exc:
        compile_(flags(project=('A") OR (x',)))
    assert err(exc) == (22, "invalid_request", "invalid_key")


def test_status_and_category() -> None:
    assert compile_(flags(status=("Open", 'We "q"'))) == '(status in ("Open", "We \\"q\\""))'
    assert (
        compile_(flags(status_category=("new", "indeterminate", "done")))
        == '(statusCategory in ("To Do", "In Progress", "Done"))'
    )


def test_status_category_unknown() -> None:
    with pytest.raises(InputError) as exc:
        compile_(flags(status_category=("blocked",)))
    assert err(exc) == (22, "invalid_request", "invalid_value")


def test_assignee_forms() -> None:
    assert compile_(flags(assignee=("none", "me"))) == (
        "(assignee is EMPTY OR assignee = currentUser())"
    )
    assert compile_(flags(assignee=("5b10:ab-c",))) == '(assignee = "5b10:ab-c")'


def test_assignee_bad_account() -> None:
    with pytest.raises(InputError) as exc:
        compile_(flags(assignee=('x" OR "1"="1',)))
    assert err(exc) == (2, "usage", "invalid_argument")


def test_sprint_forms() -> None:
    assert compile_(flags(sprint=("open", "future", "42"))) == (
        "(sprint in openSprints() OR sprint in futureSprints() OR sprint = 42)"
    )
    for bad in ("OPEN", "closed", "4 2", "-1", "x"):
        with pytest.raises(InputError) as exc:
            compile_(flags(sprint=(bad,)))
        assert err(exc) == (2, "usage", "invalid_argument")


def test_label_and() -> None:
    assert compile_(flags(label=("a", "b"))) == '(labels = "a" AND labels = "b")'


def test_parent_type_priority_key() -> None:
    assert compile_(flags(parent=("abc-1",))) == '(parent in ("ABC-1"))'
    assert compile_(flags(type=("Bug",), priority=("High",))) == (
        '(issuetype in ("Bug")) AND (priority in ("High"))'
    )
    assert compile_(flags(key=("a-1", "B-22"))) == '(key in ("A-1", "B-22"))'
    with pytest.raises(InputError) as exc:
        compile_(flags(key=("A-1) OR (x",)))
    assert err(exc) == (22, "invalid_request", "invalid_key")


def test_exclusions() -> None:
    assert compile_(flags(not_status=("Done", "Closed"))) == '(status not in ("Done", "Closed"))'
    assert compile_(flags(not_label=("x",))) == '(labels not in ("x"))'
    assert compile_(flags(not_parent=("A-1",))) == '(parent not in ("A-1"))'
    assert compile_(flags(not_type=("Epic",))) == '(issuetype not in ("Epic"))'


def test_duplicates_collapsed_in_order() -> None:
    assert compile_(flags(status=("A", "B", "A"))) == '(status in ("A", "B"))'


def test_different_flags_anded_in_fixed_order() -> None:
    out = compile_(flags(status=("Open",), project=("ABC",)))
    assert out == '(project in ("ABC")) AND (status in ("Open"))'


def test_too_many_values() -> None:
    with pytest.raises(InputError) as exc:
        compile_(flags(label=tuple(f"l{i}" for i in range(600))))
    assert err(exc) == (22, "invalid_request", "invalid_value")


# --- time --------------------------------------------------------------------------------------


def test_duration() -> None:
    assert compile_(flags(updated_since="30m")) == '(updated >= "-30m")'
    assert compile_(flags(created_since="0005d")) == '(created >= "-5d")'
    assert compile_(flags(created_since="2w")) == '(created >= "-2w")'


def test_timestamp_converted_to_user_zone_and_rounded_down() -> None:
    # 2024-01-15 12:34:59Z is 07:34 in New York (UTC-5)
    assert compile_(flags(updated_since="2024-01-15T12:34:59Z")) == (
        '(updated >= "2024-01-15 07:34")'
    )
    # offset input, DST in July (UTC-4)
    assert compile_(flags(updated_since="2024-07-01T00:00:00+02:00")) == (
        '(updated >= "2024-06-30 18:00")'
    )
    assert compile_(flags(updated_since="2024-07-01T00:00:00.999Z"), time_zone="UTC") == (
        '(updated >= "2024-07-01 00:00")'
    )


def test_date_is_midnight_in_user_zone() -> None:
    assert compile_(flags(updated_since="2024-02-29")) == '(updated >= "2024-02-29 00:00")'
    assert compile_(flags(updated_since="2024-02-29"), time_zone=None) == (
        '(updated >= "2024-02-29 00:00")'
    )


@pytest.mark.parametrize(
    "bad",
    [
        "",
        "5",
        "5x",
        "12345d",
        "-5d",
        "2024-13-01",
        "2023-02-29",
        "2024-01-15T12:00:00",
        "2024-01-15 12:00:00Z",
        "2024-01-15T12:00:60Z",
        "2024-01-15T24:00:00Z",
        "2024-01-15t12:00:00z",
        "２０２４-01-15",
        "2024-01-15\n",
        "0000-01-01",
        "5d\n",
        '5d") OR ("a"="a',
    ],
)
def test_bad_times(bad: str) -> None:
    with pytest.raises(InputError) as exc:
        compile_(flags(updated_since=bad))
    assert err(exc) == (22, "invalid_request", "invalid_value")


def test_timestamp_needs_zone() -> None:
    with pytest.raises(InputError) as exc:
        compile_(flags(updated_since="2024-01-15T12:00:00Z"), time_zone=None)
    assert err(exc) == (22, "invalid_request", "invalid_value")


@pytest.mark.parametrize("zone", ["Not/AZone", "../etc/passwd", "a" * 100, "", "UTC\n", "A B"])
def test_bad_zone(zone: str) -> None:
    with pytest.raises(InputError) as exc:
        compile_(flags(updated_since="2024-01-15T12:00:00Z"), time_zone=zone)
    assert err(exc)[:2] == (22, "invalid_request")


def test_timestamp_overflow_is_error() -> None:
    with pytest.raises(InputError) as exc:
        compile_(flags(updated_since="9999-12-31T23:59:59-23:59"), time_zone="Asia/Tokyo")
    assert err(exc) == (22, "invalid_request", "invalid_value")


# --- text --------------------------------------------------------------------------------------


def test_text_phrase_special_characters() -> None:
    out = compile_(flags(text="Fix [x] (y): a* b?"))
    assert out == '(text ~ "\\"Fix [x] (y): a* b?\\"")'


def test_text_escapes_quote_and_backslash() -> None:
    out = compile_(flags(text='a"b\\c'))
    # inner phrase: "a\"b\\c"; outer escapes the backslashes and quotes again
    assert out == '(text ~ "\\"a\\\\\\"b\\\\\\\\c\\"")'


def test_text_limits() -> None:
    compile_(flags(text="x" * 200))
    for bad in ("x" * 201, "", "a\nb"):
        with pytest.raises(InputError) as exc:
            compile_(flags(text=bad))
        assert err(exc) == (22, "invalid_request", "invalid_value")


# --- order by ----------------------------------------------------------------------------------


def test_order_by_forms() -> None:
    assert compile_(flags(project=("A",), order_by="rank")) == '(project in ("A")) ORDER BY rank'
    assert compile_(flags(project=("A",), order_by="updated:asc")).endswith("ORDER BY updated ASC")
    assert compile_(flags(project=("A",), order_by="created:desc")).endswith(
        "ORDER BY created DESC"
    )


def test_order_by_logical_names() -> None:
    f = flags(project=("A",), order_by="due_date:asc")
    assert compile_(f).endswith("ORDER BY duedate ASC")
    f = flags(project=("A",), order_by="estimate")
    assert compile_(f, order_clauses={"estimate": "cf[10001]"}).endswith("ORDER BY cf[10001]")
    with pytest.raises(InputError) as exc:
        compile_(f)
    assert err(exc) == (22, "invalid_request", "unknown_field")


def test_order_by_mapping_value_validated() -> None:
    with pytest.raises(InputError) as exc:
        compile_(
            flags(project=("A",), order_by="estimate"),
            order_clauses={"estimate": "x, project"},
        )
    assert err(exc) == (22, "invalid_request", "unknown_field")


@pytest.mark.parametrize(
    "bad",
    ["", ":asc", "a:", "a:up", "a:ASC", "1a", "a b", "a,b", 'a"', "a:asc:desc", "x" * 65, "a\n"],
)
def test_order_by_bad(bad: str) -> None:
    with pytest.raises(InputError) as exc:
        compile_(flags(project=("A",), order_by=bad))
    assert err(exc)[:2] == (22, "invalid_request")


# --- named queries and the scanner -------------------------------------------------------------


def test_named_query_wrapped_and_anded() -> None:
    out = compile_(flags(status=("Open",)), query="project = A OR project = B")
    assert out == '(project = A OR project = B) AND (status in ("Open"))'


def test_named_query_trailing_order_by_kept_or_replaced() -> None:
    q = "project = A OR project = B ORDER BY rank ASC"
    assert compile_(flags(status=("Open",)), query=q) == (
        '(project = A OR project = B) AND (status in ("Open")) ORDER BY rank ASC'
    )
    assert compile_(flags(status=("Open",), order_by="updated:asc"), query=q) == (
        '(project = A OR project = B) AND (status in ("Open")) ORDER BY updated ASC'
    )


def test_named_query_alone() -> None:
    assert compile_(query="project = A order by key") == "(project = A) ORDER BY key"


def test_named_query_only_order_by() -> None:
    assert compile_(flags(status=("Open",)), query="ORDER BY rank") == (
        '(status in ("Open")) ORDER BY rank'
    )


def test_named_query_order_by_cases_and_whitespace() -> None:
    assert sc.split_order_by("a = 1 Order\n\tBy rank") == ("a = 1 ", "rank")
    assert sc.split_order_by("(a = 1)ORDER BY rank") == ("(a = 1)", "rank")


def test_split_ignores_order_by_in_strings() -> None:
    q = 'summary ~ "x order by y" ORDER BY rank'
    assert sc.split_order_by(q) == ('summary ~ "x order by y" ', "rank")
    q = 'summary ~ \'x order by y\' AND a = "\\" order by z"'
    assert sc.split_order_by(q) == (q, None)


def test_split_ignores_escaped_quote_in_string() -> None:
    q = 'summary ~ "a \\" ORDER BY b"'
    assert sc.split_order_by(q) == (q, None)
    # an even number of backslashes closes the string
    q = 'summary ~ "a \\\\" ORDER BY b'
    assert sc.split_order_by(q) == ('summary ~ "a \\\\" ', "b")


def test_split_uses_last_top_level_order_by() -> None:
    assert sc.split_order_by("a = 1 ORDER BY x ORDER BY y") == ("a = 1 ORDER BY x ", "y")


def test_split_ignores_nested_order_by_and_word_parts() -> None:
    q = "(a = 1 ORDER BY x)"
    assert sc.split_order_by(q) == (q, None)
    for q in ("xORDER BY y", "a = 1 ORDER BYTE", "ORDER_BY x", "a = border by"):
        assert sc.split_order_by(q) == (q, None)


def test_split_nested_parentheses_and_brackets_in_strings() -> None:
    q = 'a in ("(", ")") AND (b = "((" ) ORDER BY "odd field" DESC'
    assert sc.split_order_by(q) == ('a in ("(", ")") AND (b = "((" ) ', '"odd field" DESC')


@pytest.mark.parametrize(
    "bad",
    [
        "status = Open) OR (project = SECRET",
        "(a = 1",
        "a = 1)",
        ")(",
        'a = "x',
        "a = 'x",
        'a = "x\\"',
        "a = 1 ORDER BY x)",
        "a = 1 ORDER BY",
        "a = 1 ORDER BY   ",
        "((a = 1)",
    ],
)
def test_scanner_rejects(bad: str) -> None:
    with pytest.raises(InputError) as exc:
        sc.split_order_by(bad)
    assert err(exc) == (22, "invalid_request", "invalid_query")


def test_payload_from_issue_exits_22_in_compile() -> None:
    with pytest.raises(InputError) as exc:
        compile_(flags(status=("Open",)), query="status = Open) OR (project = SECRET")
    assert err(exc) == (22, "invalid_request", "invalid_query")


def test_payload_in_order_tail() -> None:
    with pytest.raises(InputError) as exc:
        compile_(flags(status=("Open",)), query="a = 1 ORDER BY x) OR (b = 2")
    assert err(exc) == (22, "invalid_request", "invalid_query")


def test_named_query_control_characters_rejected() -> None:
    for bad in ["a = 1\x00", "a = 1\u2028OR b = 2", "a = 1 \u202e", "a\x0b= 1"]:
        with pytest.raises(InputError) as exc:
            compile_(query=bad)
        assert err(exc) == (22, "invalid_request", "invalid_query")


def test_named_query_multiline_allowed() -> None:
    assert compile_(query="a = 1\n  AND b = 2\n") == "(a = 1\n  AND b = 2)"


def test_length_cap_before_scanner(monkeypatch: pytest.MonkeyPatch) -> None:
    called = []
    monkeypatch.setattr(sc, "_scan", lambda *a, **k: called.append(1))
    for kw in ({"query": "a" * 4097}, {"raw_jql": "a" * 4097}):
        with pytest.raises(InputError) as exc:
            compile_(**kw)
        assert err(exc) == (22, "invalid_request", "invalid_query")
    assert not called
    assert len(compile_(query="a" * 4094)) == 4096
    assert compile_(raw_jql="a" * 4096) == "a" * 4096
    with pytest.raises(InputError):  # the wrapper's two parentheses push the whole over
        compile_(query="a" * 4096)


def test_compiled_whole_over_cap() -> None:
    q = "project = " + "A" * 4000
    with pytest.raises(InputError) as exc:
        compile_(flags(label=("x" * 200,)), query=q)
    assert err(exc) == (22, "invalid_request", "invalid_query")


def test_empty_search_needs_a_filter() -> None:
    with pytest.raises(InputError) as exc:
        compile_()
    assert err(exc) == (2, "usage", "missing_option")
    with pytest.raises(InputError) as exc:
        compile_(query="   ")
    assert err(exc) == (2, "usage", "missing_option")


# --- raw queries -------------------------------------------------------------------------------


def test_raw_stands_alone_unchanged() -> None:
    raw = "project = A ORDER BY rank"
    assert compile_(raw_jql=raw) == raw
    # not scanned: an unbalanced raw query is left to the parse endpoint
    assert compile_(raw_jql="a = 1) OR (b") == "a = 1) OR (b"


@pytest.mark.parametrize(
    "f",
    [
        flags(project=("A",)),
        flags(text="x"),
        flags(order_by="rank"),
        flags(not_label=("x",)),
        flags(updated_since="1d"),
        flags(sprint=("open",)),
    ],
)
def test_raw_with_structured_flag_is_invalid_combination(f: sc.SearchFlags) -> None:
    with pytest.raises(InputError) as exc:
        compile_(f, raw_jql="project = B")
    assert err(exc) == (2, "usage", "invalid_combination")


def test_raw_with_query_and_two_forms() -> None:
    with pytest.raises(InputError) as exc:
        compile_(raw_jql="a = 1", query="b = 2")
    assert err(exc) == (2, "usage", "invalid_combination")


def test_raw_control_characters() -> None:
    with pytest.raises(InputError) as exc:
        compile_(raw_jql="a = 1\x00")
    assert err(exc) == (22, "invalid_request", "invalid_query")


# --- lookup ------------------------------------------------------------------------------------


def test_lookup_named_query() -> None:
    assert sc.lookup_named_query({"mine": "a = 1"}, "mine", "main") == "a = 1"
    with pytest.raises(InputError) as exc:
        sc.lookup_named_query({"mine": "a = 1"}, "nope\x00", "main")
    assert err(exc) == (22, "invalid_request", "invalid_value")
    assert "\x00" not in exc.value.message


# --- adversarial and property tests ------------------------------------------------------------

NASTY = [
    'x" OR project = "SECRET',
    "x' OR 1=1 --",
    "\\",
    '\\"',
    '\\\\"',
    "ORDER BY rank",
    "order by x",
    ") OR (",
    "AND",
    "OR",
    "é\u0301😀",
    "a" * 256,
    "%00",
    "${x}",
    "x\\",
]


def _string_literals_ok(jql: str) -> bool:
    """True when double quotes in ``jql`` pair up under the backslash rule."""
    in_str = False
    esc = False
    for ch in jql:
        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
        elif ch == '"':
            in_str = True
    return not in_str


def _strip_strings(jql: str) -> str:
    """Replace every string literal (either quote, backslash escapes) by an empty pair."""
    out, quote, esc = [], None, False
    for ch in jql:
        if quote:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == quote:
                quote = None
        elif ch in "\"'":
            quote = ch
            out.append(ch * 2)
        else:
            out.append(ch)
    return "".join(out)


CLAUSE_FLAGS = [
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
]


def _random_value(rng: random.Random) -> str:
    kind = rng.randrange(5)
    if kind == 0:
        return "".join(rng.choice(string.printable) for _ in range(rng.randrange(0, 40)))
    if kind == 1:
        return "".join(chr(rng.randrange(0x20, 0x3000)) for _ in range(rng.randrange(0, 40)))
    if kind == 2:
        return rng.choice(NASTY)
    if kind == 3:
        return "".join(rng.choice("\"\\()' ORDERBY=,") for _ in range(rng.randrange(0, 30)))
    return rng.choice(["me", "none", "open", "future", "new", "done", "ABC-12", "7", "A_B"])


def test_property_every_flag_value_is_one_clause_or_an_error() -> None:
    rng = random.Random(1616)
    deadline = time.monotonic() + 20
    n = 0
    while time.monotonic() < deadline and n < 20000:
        n += 1
        flag = rng.choice(CLAUSE_FLAGS)
        values = tuple(_random_value(rng) for _ in range(rng.randrange(1, 4)))
        try:
            out = compile_(flags(**{flag: values}))
        except InputError as e:
            assert e.exit_code in (2, 22)
            continue
        assert out.startswith("(") and out.endswith(")")
        assert _string_literals_ok(out), (flag, values, out)
        skeleton = _strip_strings(out)
        depth = 0
        for i, ch in enumerate(skeleton):
            depth += ch == "("
            depth -= ch == ")"
            assert depth >= 0
            assert depth > 0 or i == len(skeleton) - 1, (flag, values, out)  # one top clause
        assert depth == 0
        assert "ORDER" not in skeleton.upper()
        assert len(out) <= 4096


def test_property_scanner_never_crashes_and_split_is_sound() -> None:
    rng = random.Random(4096)
    alphabet = ['"', "'", "\\", "(", ")", " ", "ORDER BY", "order  by", "a", "=", "\n", "x"]
    deadline = time.monotonic() + 15
    n = 0
    while time.monotonic() < deadline and n < 30000:
        n += 1
        q = "".join(rng.choice(alphabet) for _ in range(rng.randrange(0, 40)))
        try:
            where, tail = sc.split_order_by(q)
        except InputError as e:
            assert (e.exit_code, e.reason) == (22, "invalid_query")
            continue
        # an accepted query is balanced as a whole and each part is balanced alone
        for part in (where, tail or ""):
            depth = 0
            for ch in _strip_strings(part):
                depth += ch == "("
                depth -= ch == ")"
                assert depth >= 0, q
            assert depth == 0, q
        if tail is None:
            assert where == q


def test_wrapped_where_cannot_escape_for_any_accepted_query() -> None:
    """Whatever the scanner accepts, the wrapper's parentheses stay balanced and matched."""
    rng = random.Random(77)
    alphabet = ['"', "\\", "(", ")", " ", "a", "=", "OR", "x"]
    for _ in range(20000):
        q = "".join(rng.choice(alphabet) for _ in range(rng.randrange(1, 30)))
        try:
            out = compile_(flags(status=("Open",)), query=q)
        except InputError:
            continue
        if not sc.split_order_by(q)[0].strip():
            continue  # nothing to wrap
        skeleton = _strip_strings(out)
        depth = 0
        closed_at = None
        for i, ch in enumerate(skeleton):
            depth += ch == "("
            depth -= ch == ")"
            assert depth >= 0
            if depth == 0 and closed_at is None:
                closed_at = i
        assert depth == 0
        # the first top-level group closes exactly where the wrapper closes
        assert closed_at is not None
        assert skeleton.startswith("(") and skeleton[closed_at + 1 :].startswith(" AND (")


# --- time bounds -------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "q",
    [
        "(" * 4096,
        '"' * 4096,
        "\\" * 4096,
        "ORDER BY " * 450,
        " " * 4096,
        "ORDER" + " " * 4000 + "x",
        "a = " + "(" * 2000 + ")" * 2000,
    ],
)
def test_scanner_is_fast_on_hostile_input(q: str) -> None:
    start = time.monotonic()
    try:
        sc.split_order_by(q)
    except InputError:
        pass
    assert time.monotonic() - start < 1.0


def test_huge_inputs_fail_fast() -> None:
    start = time.monotonic()
    big = "a" * 10_000_000
    for f in (
        flags(status=(big,)),
        flags(text=big),
        flags(updated_since=big),
        flags(order_by=big),
        flags(project=(big,)),
        flags(assignee=(big,)),
        flags(key=(big,)),
    ):
        with pytest.raises(InputError):
            compile_(f)
    for kw in ({"query": big}, {"raw_jql": big}):
        with pytest.raises(InputError):
            compile_(flags(), **kw)
    assert time.monotonic() - start < 2.0
