"""Value conversion by schema for --set and --field (issue #27)."""

from __future__ import annotations

import io
from pathlib import Path
from typing import Any

import pytest

from ticketctl import sanitize, setconvert
from ticketctl.errors import TicketctlError
from ticketctl.fieldmap import FieldSchema, ProviderField
from ticketctl.setspec import Assignment, MetaField, Resolved, Source, parse_assignment

NS = "com.atlassian.jira.plugin.system.customfieldtypes"


class Stdin:
    def __init__(self, data: bytes, tty: bool = False) -> None:
        self.buffer = io.BytesIO(data)
        self._tty = tty

    def isatty(self) -> bool:
        return self._tty


def ctx(**kw: Any) -> setconvert.ValueContext:
    kw.setdefault("actor", "human:tester")
    kw.setdefault("template", "Posted by an agent")
    kw.setdefault("attribution_enabled", True)
    return setconvert.ValueContext(**kw)


def resolved(
    text: str,
    type_: str = "string",
    *,
    fid: str = "customfield_90002",
    custom: str | None = None,
    items: str | None = None,
    allowed: tuple[str, ...] | None = None,
    required: bool = False,
) -> Resolved:
    schema = FieldSchema(type_, items, custom)
    field = ProviderField(fid, "Thing", fid.startswith("customfield"), schema)
    a = parse_assignment("thing=" + text)
    return Resolved(a, field, MetaField(field, required=required, allowed=allowed))


def value(text: str, type_: str = "string", **kw: Any) -> Any:
    c = kw.pop("context", None) or ctx()
    return setconvert.convert_assignment(resolved(text, type_, **kw), c).payload


def refused(text: str, type_: str = "string", **kw: Any) -> TicketctlError:
    c = kw.pop("context", None) or ctx()
    with pytest.raises(TicketctlError) as exc:
        setconvert.convert_assignment(resolved(text, type_, **kw), c)
    assert exc.value.code == "invalid_request" and exc.value.reason == "invalid_value"
    return exc.value


# ---- scalars -----------------------------------------------------------------------------------


def test_string_as_given() -> None:
    assert value("hello world = x") == "hello world = x"


@pytest.mark.parametrize("text", ["a\nb", "a\tb", "a‮b", "a\x07b", "a" * 4097])
def test_string_refuses_control_and_oversize(text: str) -> None:
    refused(text)


@pytest.mark.parametrize(
    ("text", "expected"),
    [("0", 0), ("-12", -12), ("3.5", 3.5), ("-0.25", -0.25), ("007", 7)],
)
def test_number(text: str, expected: Any) -> None:
    got = value(text, "number")
    assert got == expected and type(got) is type(expected)


@pytest.mark.parametrize(
    "text",
    ["", " 1", "1 ", "1e3", "+1", ".5", "5.", "1,5", "NaN", "inf", "１", "1_0"]
    + ["9" * 40, "1." + "1" * 40, "9223372036854775809"],
)
def test_number_refuses(text: str) -> None:
    if text == "":
        return
    refused(text, "number")


def test_date_and_datetime() -> None:
    assert value("2024-02-29", "date") == "2024-02-29"
    refused("2023-02-29", "date")
    refused("2024-2-9", "date")
    refused("2024-02-29T00:00:00Z", "date")
    assert value("2024-01-02T03:04:05Z", "datetime") == "2024-01-02T03:04:05.000+0000"
    assert value("2024-01-02T03:04:05.5+02:00", "datetime") == "2024-01-02T01:04:05.500+0000"
    for bad in (
        "2024-01-02",
        "2024-01-02T03:04:05",
        "2024-01-02T03:04:05+0200",
        "2024-13-02T03:04:05Z",
    ):
        refused(bad, "datetime")


def test_text_refusals_never_echo_the_value() -> None:
    err = refused("TOPSECRETVALUE", "number")
    assert "TOPSECRETVALUE" not in err.message


def test_configured_secret_in_a_value_is_refused() -> None:
    sanitize.configure_scrubber(["abcdefgh-secret-token"])
    try:
        with pytest.raises(TicketctlError) as exc:
            setconvert.convert_assignment(resolved("x abcdefgh-secret-token"), ctx())
        assert exc.value.reason == "secret_in_body"
        assert "abcdefgh" not in exc.value.message
    finally:
        sanitize.configure_scrubber([])


# ---- clearing ----------------------------------------------------------------------------------


@pytest.mark.parametrize("type_", ["string", "number", "date", "datetime", "option", "user"])
def test_empty_clears(type_: str) -> None:
    fv = setconvert.convert_assignment(resolved("", type_), ctx())
    assert fv.payload is None and fv.cleared


def test_empty_array_is_an_empty_list() -> None:
    fv = setconvert.convert_assignment(resolved("", "array", items="string"), ctx())
    assert fv.payload == [] and fv.cleared


def test_required_field_cannot_be_cleared() -> None:
    refused("", "string", required=True)


# ---- options -----------------------------------------------------------------------------------


def test_option_matches_case_insensitively_to_the_canonical_name() -> None:
    assert value("large", "option", allowed=("Small", "Large")) == {"value": "Large"}


def test_option_none_matching_lists_candidates() -> None:
    err = refused("huge", "option", allowed=("Small", "Large"))
    assert err.details == {"candidates": ["Large", "Small"]}


def test_option_several_matches_list_candidates() -> None:
    err = refused("a", "option", allowed=("A", "a", "B"))
    assert err.details == {"candidates": ["A", "a"]}


def test_option_without_listed_values_goes_to_the_provider() -> None:
    assert value("Whatever", "option") == {"value": "Whatever"}


def test_priority_uses_name() -> None:
    assert value("high", "priority", fid="priority", allowed=("High",)) == {"name": "High"}


def test_candidates_are_capped() -> None:
    allowed = tuple(f"Opt{i:03d}" for i in range(200))
    err = refused("zzz", "option", allowed=allowed)
    assert err.details is not None and len(err.details["candidates"]) == 50


# ---- arrays ------------------------------------------------------------------------------------


def test_array_bare_value_is_one_element() -> None:
    assert value("one", "array", items="string") == ["one"]


def test_array_json_list() -> None:
    assert value('["a", "b c"]', "array", items="string") == ["a", "b c"]


@pytest.mark.parametrize("text", ["[1]", '["a", null]', '[""]', "[", "[[]]", '["a\\nb"]'])
def test_array_refuses_bad_json_lists(text: str) -> None:
    refused(text, "array", items="string")


def test_array_deep_nesting_does_not_crash() -> None:
    refused("[" * 100_000 + "]" * 100_000, "array", items="string")


def test_array_item_cap() -> None:
    refused("[" + ",".join(['"a"'] * 1001) + "]", "array", items="string")


def test_array_of_option_matches_each_item() -> None:
    got = value('["small", "LARGE"]', "array", items="option", allowed=("Small", "Large"))
    assert got == [{"value": "Small"}, {"value": "Large"}]
    refused('["small", "nope"]', "array", items="option", allowed=("Small", "Large"))


def test_array_of_component_uses_name() -> None:
    assert value("core", "array", items="component") == [{"name": "core"}]


def test_array_of_unsupported_items_is_refused() -> None:
    refused("x", "array", items="issuelinks")


# ---- users -------------------------------------------------------------------------------------


def test_user_account_id_none_and_me() -> None:
    assert value("acct-1", "user") == {"accountId": "acct-1"}
    assert value("none", "user") is None
    assert value("me", "user", context=ctx(me_account_id=lambda: "acct-me")) == {
        "accountId": "acct-me"
    }


def test_user_me_without_identity_is_a_caller_bug() -> None:
    with pytest.raises(ValueError):
        setconvert.convert_assignment(resolved("me", "user"), ctx())


@pytest.mark.parametrize("text", ["has space", "a/b", "x" * 129, "<script>", "a\nb"])
def test_user_refuses_bad_account_ids(text: str) -> None:
    refused(text, "user")


# ---- types that are not supported --------------------------------------------------------------


@pytest.mark.parametrize("type_", ["issuelink", "project", "attachment", "any", "resolution"])
def test_unsupported_types_are_invalid_value(type_: str) -> None:
    refused("x", type_)


def test_missing_schema_is_invalid_value() -> None:
    r = resolved("x")
    field = ProviderField("customfield_90002", "Thing", True, None)
    bare = Resolved(r.assignment, field, MetaField(field))
    with pytest.raises(TicketctlError) as exc:
        setconvert.convert_assignment(bare, ctx())
    assert exc.value.reason == "invalid_value"


# ---- files and stdin ---------------------------------------------------------------------------


def with_source(r: Resolved, source: Source) -> Resolved:
    return Resolved(Assignment(r.assignment.name, source), r.field, r.meta)


def test_file_value_strips_one_trailing_newline_for_scalars(tmp_path: Path) -> None:
    p = tmp_path / "v.txt"
    p.write_text("3.5\n")
    r = with_source(resolved("", "number"), Source("file", str(p)))
    assert setconvert.convert_assignment(r, ctx()).payload == 3.5
    p.write_text("a b\n\n")
    r = with_source(resolved("", "string"), Source("file", str(p)))
    refused_text = setconvert.convert_assignment
    with pytest.raises(TicketctlError):
        refused_text(r, ctx())  # the second newline stays and a string holds no newline


def test_file_crlf_is_one_newline(tmp_path: Path) -> None:
    p = tmp_path / "v.txt"
    p.write_bytes(b"hello\r\n")
    r = with_source(resolved("", "string"), Source("file", str(p)))
    assert setconvert.convert_assignment(r, ctx()).payload == "hello"


def test_empty_file_does_not_clear(tmp_path: Path) -> None:
    p = tmp_path / "v.txt"
    p.write_text("\n")
    r = with_source(resolved("", "string"), Source("file", str(p)))
    with pytest.raises(TicketctlError) as exc:
        setconvert.convert_assignment(r, ctx())
    assert exc.value.reason == "invalid_value"


def test_missing_and_special_files_are_invalid_file(tmp_path: Path) -> None:
    for path in (str(tmp_path / "nope"), "/dev/null", str(tmp_path)):
        r = with_source(resolved("", "string"), Source("file", path))
        with pytest.raises(TicketctlError) as exc:
            setconvert.convert_assignment(r, ctx())
        assert exc.value.reason == "invalid_file"


def test_symlink_file_is_refused(tmp_path: Path) -> None:
    target = tmp_path / "t"
    target.write_text("x")
    link = tmp_path / "l"
    link.symlink_to(target)
    r = with_source(resolved("", "string"), Source("file", str(link)))
    with pytest.raises(TicketctlError) as exc:
        setconvert.convert_assignment(r, ctx())
    assert exc.value.reason == "invalid_file"


def test_at_dash_is_a_file_named_dash_not_stdin() -> None:
    a = parse_assignment("thing=@-")
    assert a.source == Source("file", "-")
    r = Resolved(a, resolved("").field, resolved("").meta)
    with pytest.raises(TicketctlError) as exc:
        setconvert.convert_assignment(r, ctx(stdin=Stdin(b"stdin text")))
    assert exc.value.reason == "invalid_file"


def test_stdin_value() -> None:
    r = with_source(resolved("", "string"), Source("stdin"))
    assert setconvert.convert_assignment(r, ctx(stdin=Stdin(b"from stdin\n"))).payload == (
        "from stdin"
    )


def test_stdin_on_a_terminal_is_refused() -> None:
    r = with_source(resolved("", "string"), Source("stdin"))
    with pytest.raises(TicketctlError) as exc:
        setconvert.convert_assignment(r, ctx(stdin=Stdin(b"x", tty=True)))
    assert (exc.value.code, exc.value.reason) == ("usage", "invalid_argument")


def test_stdin_invalid_utf8_and_oversize_are_refused() -> None:
    r = with_source(resolved("", "string"), Source("stdin"))
    for data in (b"\xff\xfe", b"a" * (2**20 + 5)):
        with pytest.raises(TicketctlError) as exc:
            setconvert.convert_assignment(r, ctx(stdin=Stdin(data)))
        assert exc.value.reason == "invalid_file"


# ---- rich text ---------------------------------------------------------------------------------


def rich(text: str = "", **kw: Any) -> Resolved:
    return resolved(text, "string", fid="description", **kw)


def test_rich_text_inline_is_invalid_value() -> None:
    with pytest.raises(TicketctlError) as exc:
        setconvert.convert_assignment(rich("some **text**"), ctx())
    assert exc.value.reason == "invalid_value"


def test_rich_text_from_a_file_becomes_adf_without_attribution(tmp_path: Path) -> None:
    p = tmp_path / "d.md"
    p.write_text("Hello **world**\n")
    fv = setconvert.convert_assignment(with_source(rich(), Source("file", str(p))), ctx())
    assert fv.rich is not None and fv.payload == fv.rich.doc and not fv.rich.attributed
    assert fv.payload["type"] == "doc"
    assert [n["type"] for n in fv.payload["content"]] == ["paragraph"]


def test_rich_text_from_stdin() -> None:
    fv = setconvert.convert_assignment(
        with_source(rich(), Source("stdin")), ctx(stdin=Stdin(b"# Title\n\ntext\n"))
    )
    assert fv.rich is not None and fv.rich.sha256


def test_rich_text_by_an_auto_actor_is_refused(tmp_path: Path) -> None:
    p = tmp_path / "d.md"
    p.write_text("text")
    r = with_source(rich(), Source("file", str(p)))
    with pytest.raises(TicketctlError) as exc:
        setconvert.convert_assignment(r, ctx(actor="auto:bot"))
    assert exc.value.reason == "invalid_value"


def test_rich_text_empty_body_is_refused(tmp_path: Path) -> None:
    p = tmp_path / "d.md"
    p.write_text("   \n")
    r = with_source(rich(), Source("file", str(p)))
    with pytest.raises(TicketctlError) as exc:
        setconvert.convert_assignment(r, ctx())
    assert exc.value.reason in ("empty_body", "invalid_value")


def test_rich_text_empty_inline_value_clears() -> None:
    fv = setconvert.convert_assignment(rich(""), ctx())
    assert fv.payload is None and fv.cleared and fv.rich is None


def test_textarea_custom_field_and_comment_are_rich_text() -> None:
    cases: list[dict[str, Any]] = [
        {"fid": "customfield_90004", "custom": f"{NS}:textarea"},
        {"fid": "comment"},
    ]
    for kw in cases:
        with pytest.raises(TicketctlError) as exc:
            setconvert.convert_assignment(resolved("inline", "string", **kw), ctx())
        assert exc.value.reason == "invalid_value"
    # a plain text field is not rich text
    assert value("inline", "string", fid="customfield_90004", custom=f"{NS}:textfield") == "inline"


def test_repeated_rich_text_is_unchanged(tmp_path: Path) -> None:
    p = tmp_path / "d.md"
    p.write_text("same **text**\n")
    r = with_source(rich(), Source("file", str(p)))
    first = setconvert.convert_assignment(r, ctx())
    again = setconvert.convert_assignment(r, ctx())
    assert first.rich is not None and again.rich is not None
    assert setconvert.rich_unchanged(first.payload, again.rich)
    p.write_text("other text\n")
    changed = setconvert.convert_assignment(r, ctx())
    assert changed.rich is not None
    assert not setconvert.rich_unchanged(first.payload, changed.rich)


@pytest.mark.parametrize("current", [None, "text", 5, [], {}, {"type": "doc"}, {"a": {"b": {}}}])
def test_rich_unchanged_is_false_for_unusable_current_values(current: Any, tmp_path: Path) -> None:
    p = tmp_path / "d.md"
    p.write_text("x")
    fv = setconvert.convert_assignment(with_source(rich(), Source("file", str(p))), ctx())
    assert fv.rich is not None
    assert setconvert.rich_unchanged(current, fv.rich) is False


# ---- convert_all -------------------------------------------------------------------------------


def test_convert_all_keeps_order_and_reads_stdin_once(tmp_path: Path) -> None:
    items = [
        with_source(resolved("", "string"), Source("stdin")),
        resolved("4", "number", fid="customfield_90003"),
    ]
    out = setconvert.convert_all(items, ctx(stdin=Stdin(b"text")))
    assert [o.payload for o in out] == ["text", 4]


# ---- labels ------------------------------------------------------------------------------------


@pytest.mark.parametrize("label", ["a", "tc-xyz", "TC-0123456789abcdef", "x" * 255, "ünï"])
def test_valid_labels(label: str) -> None:
    assert setconvert.validate_label(label) == label


@pytest.mark.parametrize(
    "label",
    ["has space", "tab\there", "line\nbreak", "", "x" * 256, "nb sp", "z​w"]
    + ["tc-0123456789abcdef", "a b"],
)
def test_invalid_labels(label: str) -> None:
    with pytest.raises(TicketctlError) as exc:
        setconvert.validate_label(label)
    assert (exc.value.code, exc.value.reason) == ("invalid_request", "invalid_value")
    assert exc.value.exit == 22
