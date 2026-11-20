"""Identifier grammars, segment() and query() (docs/interface.md, Identifiers)."""

from __future__ import annotations

import pytest

from ticketctl import sanitize as s

VALID = {
    "ticket_key": ["ABC-1", "A-1", "AB_C9-12345", "Z9-0"],
    "project_key": ["ABC", "A", "A1_B2"],
    "site_name": ["main", "A", "a-b_c", "x" * 64, "9lives"],
    "provider_id": ["10001", "a.b:c_d-e", "x" * 64],
    "numeric_id": ["0", "1", "9" * 18],
    "custom_field_id": ["customfield_1", "customfield_90002"],
    "account_id": ["abc:123-def_g.h", "x" * 128],
    "decision_id": ["a", "a.b:c@d/e-f_g", "x" * 128],
    "dedupe_key": ["run-2024/01@host:1", "x" * 128],
    "actor": ["human:alice", "auto:bot.1@x-y_z", "auto:" + "x" * 64],
    "time_zone": ["UTC", "America/Port-au-Prince", "Etc/GMT+5", "x" * 64],
}

INVALID = {
    "ticket_key": [
        "",
        "abc-1",
        "ABC-",
        "ABC",
        "-1",
        "1ABC-1",
        "ABC-1-2",
        "ABC-01 ",
        " ABC-1",
        "AB C-1",
        "ABC-１",
        "ＡBC-1",
        "ABC-1/..",
        "ABC-1;",
        "ABC-1\x00",
    ],
    "project_key": ["", "abc", "1A", "A-B", "A B", "Ａ"],
    "site_name": ["", "-a", "_a", "a b", "a.b", "x" * 65, "a/b", "é"],
    "provider_id": ["", "a b", "x" * 65, "a/b", "a?b", "é", "..%2f"],
    "numeric_id": ["", "-1", "1.0", "1" * 19, "١", "0x10", "1 "],
    "custom_field_id": [
        "",
        "customfield_",
        "customfield_x",
        "Customfield_1",
        "customfield_1a",
        "customfield_١",
        "xcustomfield_1",
    ],
    "account_id": ["", "a b", "x" * 129, "a/b", "a@b"],
    "decision_id": ["", "a b", "x" * 129, "a#b", "a\\b", "a?b"],
    "dedupe_key": ["", "a b", "x" * 129, "a'b", 'a"b'],
    "actor": [
        "",
        "human:",
        "bot:x",
        "Human:x",
        "human:a b",
        "auto:" + "x" * 65,
        "human:a/b",
        "human",
        ":x",
    ],
    "time_zone": ["", "a b", "x" * 65, "A..B?", "a%2f"],
}


def test_every_identifier_has_a_grammar() -> None:
    assert set(VALID) == set(INVALID) == set(s.GRAMMARS)


@pytest.mark.parametrize(("name", "value"), [(n, v) for n, vs in VALID.items() for v in vs])
def test_valid_values_match(name: str, value: str) -> None:
    assert s.GRAMMARS[name].matches(value)
    assert s.GRAMMARS[name].check(value) == value


@pytest.mark.parametrize(("name", "value"), [(n, v) for n, vs in INVALID.items() for v in vs])
def test_invalid_values_do_not_match(name: str, value: str) -> None:
    assert not s.GRAMMARS[name].matches(value)
    with pytest.raises(s.InputError):
        s.GRAMMARS[name].check(value)


@pytest.mark.parametrize("name", sorted(VALID))
@pytest.mark.parametrize("suffix", ["\n", "\r\n", "\n\n", " ", "\x00", " "])
def test_trailing_junk_is_never_accepted_fullmatch(name: str, suffix: str) -> None:
    for good in VALID[name]:
        if suffix == " " and name == "":  # pragma: no cover
            continue
        assert not s.GRAMMARS[name].matches(good + suffix), (name, good, suffix)
        assert not s.GRAMMARS[name].matches(suffix + good), (name, good, suffix)


def test_abc_1_newline_is_rejected_by_every_grammar() -> None:
    for name, grammar in s.GRAMMARS.items():
        for good in VALID[name]:
            assert not grammar.matches(good + "\n"), name
    assert not s.GRAMMARS["ticket_key"].matches("ABC-1\n")


def test_non_string_never_matches() -> None:
    for grammar in s.GRAMMARS.values():
        assert not grammar.matches(None)  # type: ignore[arg-type]
        assert not grammar.matches(5)  # type: ignore[arg-type]
        assert not grammar.matches(b"ABC-1")  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("name", "exit_code", "code", "reason"),
    [
        ("ticket_key", 22, "invalid_request", "invalid_key"),
        ("project_key", 22, "invalid_request", "invalid_key"),
        ("site_name", 22, "invalid_request", "unknown_site"),
        ("custom_field_id", 22, "invalid_request", "invalid_value"),
        ("numeric_id", 2, "usage", "invalid_argument"),
        ("provider_id", 2, "usage", "invalid_argument"),
        ("account_id", 2, "usage", "invalid_argument"),
        ("decision_id", 2, "usage", "invalid_argument"),
        ("dedupe_key", 2, "usage", "invalid_argument"),
        ("actor", 2, "usage", "invalid_argument"),
    ],
)
def test_failure_exit_codes(name: str, exit_code: int, code: str, reason: str) -> None:
    with pytest.raises(s.InputError) as info:
        s.GRAMMARS[name].check("!!bad!!")
    assert (info.value.exit_code, info.value.code, info.value.reason) == (exit_code, code, reason)


def test_rejection_message_is_sanitized_and_cut_to_64() -> None:
    with pytest.raises(s.InputError) as info:
        s.GRAMMARS["ticket_key"].check("\x1b[31m" + "x" * 500)
    msg = str(info.value)
    assert "\x1b" not in msg
    assert "<U+001B>" in msg
    assert "x" * 100 not in msg
    assert info.value.message == msg


def test_rejection_message_is_scrubbed() -> None:
    token = "tok-SENTINEL-0123456789"
    s.configure_scrubber([token])
    try:
        with pytest.raises(s.InputError) as info:
            s.GRAMMARS["ticket_key"].check(token)
        assert token not in str(info.value)
    finally:
        s.configure_scrubber([])


# --- ticket key normalization --------------------------------------------------------------


def test_normalize_ticket_key_uppercases_ascii() -> None:
    assert s.normalize_ticket_key("abc-12") == "ABC-12"
    assert s.normalize_ticket_key("ABC-12") == "ABC-12"


@pytest.mark.parametrize(
    "bad",
    ["abc-1\n", "ſbc-1", "ıbc-1", "abßc-1", "ǰ-1", "abc-1 ", "", "abc"],
)
def test_normalize_ticket_key_rejects_non_ascii_case_tricks(bad: str) -> None:
    # str.upper() turns the long s and the German sharp s into ASCII letters; that must not pass.
    with pytest.raises(s.InputError) as info:
        s.normalize_ticket_key(bad)
    assert info.value.reason == "invalid_key"


# --- segment() and query() -----------------------------------------------------------------


def test_segment_checks_the_grammar_and_encodes_with_no_safe_characters() -> None:
    assert s.segment("ticket_key", "ABC-1") == "ABC-1"
    assert s.segment("provider_id", "a.b:c_d-e") == "a.b%3Ac_d-e"
    assert s.segment("decision_id", "a/b@c") == "a%2Fb%40c"


def test_segment_refuses_values_failing_the_grammar() -> None:
    for name, bad in [
        ("ticket_key", "../x"),
        ("ticket_key", "ABC-1\n"),
        ("numeric_id", "1/2"),
        ("provider_id", "a/b"),
    ]:
        with pytest.raises(s.InputError):
            s.segment(name, bad)


def test_segment_never_emits_a_slash_or_dot_segment_for_any_grammar() -> None:
    for name in s.GRAMMARS:
        for bad in ["/", "..", ".", "a/b", "%2f", "?", "#", "\\"]:
            try:
                out = s.segment(name, bad)
            except s.InputError:
                continue
            assert "/" not in out and out not in {".", ".."}, (name, bad, out)


@pytest.mark.parametrize("dots", [".", ".."])
def test_segment_refuses_dot_segments_that_the_grammars_allow(dots: str) -> None:
    # The provider-id grammars allow dots and quote() does not encode them: path traversal.
    assert s.GRAMMARS["provider_id"].matches(dots)
    with pytest.raises(s.InputError):
        s.segment("provider_id", dots)
    assert s.segment("provider_id", "a..b") == "a..b"


def test_segment_unknown_grammar_is_a_key_error() -> None:
    with pytest.raises(KeyError):
        s.segment("nope", "x")


def test_query_encodes_with_no_safe_characters() -> None:
    assert s.query({"a": "b c", "d": "e&f=g"}) == "a=b%20c&d=e%26f%3Dg"
    assert s.query([("k", "/"), ("k", "~")]) == "k=%2F&k=~"
    assert s.query({"n": 5, "t": True}) == "n=5&t=True"
    assert s.query({"jql": 'x = "y" OR z'}) == "jql=x%20%3D%20%22y%22%20OR%20z"
    assert s.query({}) == ""


def test_query_encodes_keys_and_unicode_and_newlines() -> None:
    assert s.query({"a b": "é\n"}) == "a%20b=%C3%A9%0A"
    assert s.query({"q": "a+b"}) == "q=a%2Bb"


def test_query_with_a_lone_surrogate_is_an_input_error_not_a_crash() -> None:
    with pytest.raises(s.InputError) as info:
        s.query({"q": "\ud800"})
    assert info.value.reason == "invalid_value"


# --- urlsplit -------------------------------------------------------------------------------


def test_split_url_wraps_value_errors_as_invalid_value() -> None:
    with pytest.raises(s.InputError) as info:
        s.split_url("https://[::1")
    assert (info.value.exit_code, info.value.reason) == (22, "invalid_value")
    with pytest.raises(s.InputError):
        s.split_url("http://[bad]x/")


def test_split_url_returns_the_parts() -> None:
    parts = s.split_url("https://example.test/browse/ABC-1?x=1#f")
    assert (parts.scheme, parts.netloc, parts.path) == ("https", "example.test", "/browse/ABC-1")


def test_browse_path_grammar_is_fullmatch() -> None:
    assert s.browse_key("/browse/ABC-1") == "ABC-1"
    assert s.browse_key("/browse/ABC-1/") == "ABC-1"
    for bad in [
        "/browse/ABC-1\n",
        "/browse/abc-1",
        "/browse/ABC-1//",
        "/x/browse/ABC-1",
        "/browse/ABC-1/extra",
        "/browse/",
        "/browse/ABC-1%0a",
        "",
    ]:
        assert s.browse_key(bad) is None, bad


@pytest.mark.parametrize(
    "bad",
    [
        "https://example.test/browse/ABC-1\n",
        "https://example.test/\nbrowse/ABC-1",
        "https://example.test/\tbrowse/ABC-1",
        "https://example.test/\rbrowse",
        "\x01 https://example.test/",
        " https://example.test/",
        "https://example.test/a b",
        "https://example.test/\x7f",
        "https://example.test/\x85",
        "https://example.test/\u00a0x",
        "https://example.test/\u2028x",
        "https://example.test/\u2029x",
        "https://example.test/\u3000x",
    ],
)
def test_split_url_refuses_whitespace_and_control_characters(bad: str) -> None:
    with pytest.raises(s.InputError) as info:
        s.split_url(bad)
    assert (info.value.exit_code, info.value.code, info.value.reason) == (
        22,
        "invalid_request",
        "invalid_value",
    )


def test_ticket_and_project_keys_are_at_most_64_characters() -> None:
    ok_key = "A" * 62 + "-1"
    assert len(ok_key) == 64
    assert s.GRAMMARS["ticket_key"].matches(ok_key)
    assert not s.GRAMMARS["ticket_key"].matches("A" * 63 + "-1")
    assert s.GRAMMARS["project_key"].matches("A" * 64)
    assert not s.GRAMMARS["project_key"].matches("A" * 65)
    assert s.normalize_ticket_key(ok_key.lower()) == ok_key
    with pytest.raises(s.InputError):
        s.normalize_ticket_key("a" * 63 + "-1")


def test_segment_refuses_dot_segments_with_the_grammars_own_failure() -> None:
    for name in ("provider_id", "account_id"):
        g = s.GRAMMARS[name]
        for dots in (".", ".."):
            with pytest.raises(s.InputError) as info:
                s.segment(name, dots)
            assert (info.value.exit_code, info.value.code, info.value.reason) == (
                g.exit_code,
                g.code,
                g.reason,
            )
