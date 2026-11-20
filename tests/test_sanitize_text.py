"""Sanitizer, truncation and output scrub (docs/interface.md, Sanitizing)."""

from __future__ import annotations

import base64
import json
import unicodedata
import urllib.parse
from collections.abc import Iterator

import pytest

from ticketctl import sanitize

# Written out independently of the implementation's constants, from the interface text.
EXPLICIT = (
    {0x034F, 0x115F, 0x1160, 0x16FE4, 0x17B4, 0x17B5, 0x2800, 0x3164, 0xFFA0, 0xFFFC}
    | set(range(0x180B, 0x1810))
    | set(range(0xFE00, 0xFE10))
    | set(range(0xE0100, 0xE01F0))
)
CATEGORIES = {"Cc", "Cf", "Cs", "Co", "Cn", "Zl", "Zp"}


def marker(cp: int) -> str:
    return f"<U+{cp:04X}>"


@pytest.fixture(autouse=True)
def _clean_scrubber() -> Iterator[None]:
    sanitize.configure_scrubber([])
    yield
    sanitize.configure_scrubber([])


def _expected(cp: int, *, body: bool) -> str:
    ch = chr(cp)
    if cp == 0x0D:
        return "\n" if body else " "  # a lone CR is a newline first
    if cp in (0x0A, 0x09):
        return ch if body else " "
    if cp in EXPLICIT or unicodedata.category(ch) in CATEGORIES:
        return marker(cp)
    return ch


@pytest.mark.parametrize("body", [False, True])
def test_whole_code_space_matches_explicit_list_and_running_unicode_table(body: bool) -> None:
    # CR is excluded from the joined string: CRLF folding makes a run of chars context dependent.
    cps = [cp for cp in range(0x110000) if cp != 0x0D]
    text = "".join(chr(cp) for cp in cps)
    expected = "".join(_expected(cp, body=body) for cp in cps)
    fn = sanitize.sanitize_body if body else sanitize.sanitize_line
    assert fn(text) == expected


@pytest.mark.parametrize("body", [False, True])
def test_every_code_point_alone(body: bool) -> None:
    fn = sanitize.sanitize_body if body else sanitize.sanitize_line
    for cp in list(range(0x300)) + sorted(EXPLICIT) + [0xD800, 0xDFFF, 0x10FFFF, 0xE000, 0x2028]:
        assert fn(chr(cp)) == _expected(cp, body=body), hex(cp)


def test_explicit_list_is_not_empty_of_non_category_members() -> None:
    # Guards the test above: several explicit code points are not Cc/Cf/Cs/Co/Cn/Zl/Zp.
    extra = [cp for cp in EXPLICIT if unicodedata.category(chr(cp)) not in CATEGORIES]
    assert 0x2800 in extra and 0xFE00 in extra and 0x3164 in extra


def test_marker_width_for_astral_and_bmp() -> None:
    assert sanitize.sanitize_line("\x1b") == "<U+001B>"
    assert sanitize.sanitize_line("\U000e0001") == "<U+E0001>"
    assert sanitize.sanitize_line("\U0010ffff") == "<U+10FFFF>"


def test_ansi_escape_and_osc_are_neutralized() -> None:
    hostile = "\x1b[2J\x1b]0;title\x07\x9b31m\x00x"
    out = sanitize.sanitize_line(hostile)
    assert "\x1b" not in out and "\x07" not in out and "\x9b" not in out and "\x00" not in out
    assert out == "<U+001B>[2J<U+001B>]0;title<U+0007><U+009B>31m<U+0000>x"


def test_bidi_controls_and_invisibles_are_replaced_but_letters_are_not() -> None:
    assert sanitize.sanitize_line("a‮b⁦c​d﻿") == ("a<U+202E>b<U+2066>c<U+200B>d<U+FEFF>")
    # Strong RTL letters are left alone (documented residual risk).
    assert sanitize.sanitize_line("אב") == "אב"
    assert sanitize.sanitize_line("  ") == "<U+2028><U+2029>"


def test_no_unicode_normalization() -> None:
    nfd = "é"
    nfc = "é"
    assert sanitize.sanitize_line(nfd) == nfd
    assert sanitize.sanitize_line(nfc) == nfc
    assert sanitize.sanitize_line("ﬁ") == "ﬁ"  # compatibility ligature kept


def test_marker_in_input_is_not_escaped() -> None:
    assert sanitize.sanitize_line("<U+0041>") == "<U+0041>"


def test_single_line_folds_whitespace_controls() -> None:
    assert sanitize.sanitize_line("a\nb\tc\rd") == "a b c d"
    assert sanitize.sanitize_line("a\r\nb") == "a b"
    assert sanitize.sanitize_line("a\n\nb") == "a  b"


def test_body_keeps_newline_and_tab_and_normalizes_cr() -> None:
    assert sanitize.sanitize_body("a\r\nb\rc\nd\te") == "a\nb\nc\nd\te"
    assert sanitize.sanitize_body("a\r\r\nb") == "a\n\nb"
    assert sanitize.sanitize_body("a\x0bb\x0cc\x85d") == "a<U+000B>b<U+000C>c<U+0085>d"


def test_sanitizer_is_idempotent_on_its_own_output() -> None:
    raw = "x\x1b[0m‮\ud800y\U000e0100"
    once = sanitize.sanitize_line(raw)
    assert sanitize.sanitize_line(once) == once


def test_plain_text_and_empty_pass_through() -> None:
    assert sanitize.sanitize_line("") == ""
    assert sanitize.sanitize_body("") == ""
    text = "Plain ASCII, accents é, CJK 中文, emoji \U0001f600 ok"
    assert sanitize.sanitize_line(text) == text


def test_lone_surrogate_is_replaced() -> None:
    assert sanitize.sanitize_line("a\udc80b") == "a<U+DC80>b"


def test_large_input_is_fast_enough() -> None:
    text = ("café 中文 \x1b[0m\n" * 100_000)[:1_000_000]
    out = sanitize.sanitize_body(text)
    assert "\x1b" not in out
    assert len(out) > len(text)


@pytest.mark.parametrize("non_str", [None, 5, b"x"])
def test_non_string_is_a_type_error(non_str: object) -> None:
    with pytest.raises(TypeError):
        sanitize.sanitize_line(non_str)  # type: ignore[arg-type]


# --- truncation ----------------------------------------------------------------------------


def test_cap_text_short_is_unchanged() -> None:
    assert sanitize.cap_text("abc", 3) == ("abc", False)
    assert sanitize.cap_text("", 0) == ("", False)


def test_cap_text_long_ends_with_marker_and_fits_the_cap() -> None:
    out, cut = sanitize.cap_text("a" * 100, 50)
    assert cut is True
    assert out.endswith("<truncated>")
    assert len(out) == 50
    assert out == "a" * 39 + "<truncated>"


def test_cap_text_cuts_at_a_code_point_boundary() -> None:
    out, cut = sanitize.cap_text("\U0001f600" * 40, 20)
    assert cut and out == "\U0001f600" * 9 + "<truncated>"


def test_cap_text_never_splits_a_u_marker() -> None:
    text = sanitize.sanitize_line("ab" + "\x1b" * 10)
    for cap in range(12, 40):
        out, cut = sanitize.cap_text(text, cap)
        if not cut:
            continue
        body = out[: -len("<truncated>")]
        assert body.count("<") == body.count(">"), (cap, out)
        assert len(out) <= cap


def test_cap_smaller_than_marker_is_just_the_marker_prefix() -> None:
    out, cut = sanitize.cap_text("a" * 100, 5)
    assert cut and len(out) <= 5


def test_field_helper_sanitizes_then_caps() -> None:
    out, cut = sanitize.sanitize_field("x‮" * 100, cap=30)
    assert cut and len(out) <= 30 and out.endswith("<truncated>")
    out, cut = sanitize.sanitize_field("a\nb", cap=30, body=True)
    assert (out, cut) == ("a\nb", False)
    out, cut = sanitize.sanitize_field("a\nb", cap=30)
    assert (out, cut) == ("a b", False)


def test_documented_caps() -> None:
    assert sanitize.CAPS == {
        "title": 4096,
        "summary": 4096,
        "body": 1_000_000,
        "label": 256,
        "name": 256,
        "attachment_name": 1024,
    }


# --- output scrub --------------------------------------------------------------------------

TOKEN = "tok-SENTINEL-0123456789"


def _basic(email: str, token: str) -> str:
    return base64.b64encode(f"{email}:{token}".encode()).decode()


def test_scrub_replaces_token_every_occurrence() -> None:
    sanitize.configure_scrubber([TOKEN])
    assert sanitize.scrub(f"a {TOKEN} b {TOKEN}") == "a <redacted> b <redacted>"


def test_scrub_replaces_basic_header_value() -> None:
    value = _basic("user@example.test", TOKEN)
    sanitize.configure_scrubber([TOKEN], basic_values=[value])
    assert sanitize.scrub(f"Authorization: Basic {value}") == "Authorization: Basic <redacted>"


def test_basic_value_helper_matches_the_gateway_formula() -> None:
    assert sanitize.basic_auth_value("user@example.test", TOKEN) == _basic(
        "user@example.test", TOKEN
    )
    # Both values are stripped of surrounding whitespace before use.
    assert sanitize.basic_auth_value(" user@example.test\n", f" {TOKEN}\n") == _basic(
        "user@example.test", TOKEN
    )


def test_short_secrets_are_ignored() -> None:
    sanitize.configure_scrubber(["1234567", "", " "])
    assert sanitize.scrub("1234567 and 1234567") == "1234567 and 1234567"


def test_eight_character_secret_is_scrubbed() -> None:
    sanitize.configure_scrubber(["12345678"])
    assert sanitize.scrub("x12345678y") == "x<redacted>y"


def test_longer_secret_wins_over_its_prefix() -> None:
    sanitize.configure_scrubber(["abcdefgh", "abcdefghijkl"])
    assert sanitize.scrub("abcdefghijkl") == "<redacted>"


def test_scrub_with_regex_metacharacters_in_secret() -> None:
    sanitize.configure_scrubber(["a.b*c+d?(e)[f]"])
    assert sanitize.scrub("x a.b*c+d?(e)[f] y aXb") == "x <redacted> y aXb"


def test_render_is_sanitize_then_scrub() -> None:
    sanitize.configure_scrubber([TOKEN])
    out = sanitize.render(f"bad\x1b[0m {TOKEN}\nnext")
    assert out == "bad<U+001B>[0m <redacted> next"
    out = sanitize.render(f"{TOKEN}\nnext", body=True)
    assert out == "<redacted>\nnext"


def test_secret_with_a_control_char_is_scrubbed_in_its_sanitized_form() -> None:
    secret = "abcd\x1befgh"
    sanitize.configure_scrubber([secret])
    assert sanitize.render(f"x {secret} y") == "x <redacted> y"


def test_token_echoed_in_a_provider_error_body_is_redacted() -> None:
    sanitize.configure_scrubber([TOKEN])
    body = f'{{"errorMessages": ["bad credentials {TOKEN}"]}}'
    assert TOKEN not in sanitize.render(body)


def test_token_in_a_traceback_is_redacted() -> None:
    sanitize.configure_scrubber([TOKEN])
    try:
        raise RuntimeError(f"boom {TOKEN}")
    except RuntimeError as exc:
        text = sanitize.format_traceback(exc)
    assert TOKEN not in text
    assert "<redacted>" in text
    assert "RuntimeError" in text
    assert "\n" in text  # a traceback is multi-line


def test_token_in_a_chained_traceback_is_redacted() -> None:
    sanitize.configure_scrubber([TOKEN])
    try:
        try:
            raise ValueError(f"inner {TOKEN}")
        except ValueError as inner:
            raise RuntimeError("outer") from inner
    except RuntimeError as exc:
        assert exc.__cause__ is not None
        text = sanitize.format_traceback(exc)
    assert TOKEN not in text
    assert "ValueError: inner <redacted>" in text
    assert "RuntimeError: outer" in text


def test_configure_replaces_previous_secrets() -> None:
    sanitize.configure_scrubber([TOKEN])
    sanitize.configure_scrubber([])
    assert sanitize.scrub(TOKEN) == TOKEN


def test_scrub_without_configuration_is_identity() -> None:
    assert sanitize.scrub("anything") == "anything"


# --- review round 1: stripped, encoded and overlapping secrets; scrub before cap -----------

SECRET = "QZXV-secret-0123"


@pytest.mark.parametrize("raw", [f"{TOKEN}\n", f"  {TOKEN}  ", f"\t{TOKEN}\r\n"])
def test_whitespace_around_a_configured_secret_is_stripped(raw: str) -> None:
    sanitize.configure_scrubber([raw], basic_values=[])
    assert sanitize.scrub(f"{TOKEN}.") == "<redacted>."
    assert sanitize.scrub(f"end {TOKEN}") == "end <redacted>"
    assert sanitize.render(f"x {TOKEN} y") == "x <redacted> y"


def test_minimum_length_applies_to_the_stripped_secret() -> None:
    sanitize.configure_scrubber(["  1234567  \n"])
    assert sanitize.scrub("1234567") == "1234567"
    sanitize.configure_scrubber(["  12345678  "])
    assert sanitize.scrub("1234567890") == "<redacted>90"


def test_ensure_no_secret_uses_the_stripped_secret() -> None:
    with pytest.raises(sanitize.InputError) as info:
        sanitize.ensure_no_secret(f"a {TOKEN}.", secrets=[f"{TOKEN}\n"])
    assert info.value.reason == "secret_in_body"
    with pytest.raises(sanitize.InputError):
        sanitize.ensure_no_secret(f"a {TOKEN}", secrets=[f"  {TOKEN} "])
    sanitize.ensure_no_secret("a 1234567", secrets=["  1234567\n"])
    sanitize.configure_scrubber([f"{TOKEN}\n"])
    with pytest.raises(sanitize.InputError):
        sanitize.ensure_no_secret(f"x {TOKEN}")


def test_secret_straddling_the_cap_leaves_no_prefix() -> None:
    sanitize.configure_scrubber([SECRET])
    text = "a" * 20 + SECRET + "b" * 20
    for cap in range(12, 60):
        out, _ = sanitize.sanitize_field(text, cap=cap)
        assert SECRET[:4] not in out, (cap, out)
        assert len(out) <= cap
        body = out[: -len("<truncated>")] if out.endswith("<truncated>") else out
        assert body.count("<") == body.count(">"), (cap, out)


def test_basic_header_value_straddling_the_cap_leaves_no_prefix() -> None:
    value = _basic("user@example.test", TOKEN)
    sanitize.configure_scrubber([TOKEN], basic_values=[value])
    text = "a" * 20 + value + "b" * 20
    for cap in range(12, 80):
        out, _ = sanitize.sanitize_field(text, cap=cap, body=True)
        assert value[:4] not in out, (cap, out)


def test_cap_text_never_splits_a_redacted_marker() -> None:
    text = "ab" + sanitize.REDACTED * 3
    for cap in range(12, 40):
        out, cut = sanitize.cap_text(text, cap)
        if not cut:
            continue
        body = out[: -len("<truncated>")]
        assert body.count("<") == body.count(">"), (cap, out)


def test_encoded_forms_of_a_secret_are_scrubbed() -> None:
    secret = 'pa ss/w"o\\rd+&é'
    sanitize.configure_scrubber([secret])
    forms = [
        urllib.parse.quote(secret, safe=""),
        urllib.parse.quote(secret),
        json.dumps(secret)[1:-1],
        secret,
    ]
    for form in forms:
        assert sanitize.scrub(f"x {form} y") == "x <redacted> y", form


def test_slash_secret_is_matched_in_its_percent_encoded_form() -> None:
    sanitize.configure_scrubber(["abc/defg"])  # 8 chars; quote(safe="") makes %2F (10 chars)
    assert sanitize.scrub("abc%2Fdefg") == "<redacted>"
    with pytest.raises(sanitize.InputError):
        sanitize.ensure_no_secret("abc%2Fdefg", secrets=["abc/defg"])


def test_overlapping_secrets_leave_no_partial() -> None:
    sanitize.configure_scrubber(["12345678abc", "5678abcdefg"])
    assert sanitize.scrub("12345678abcdefg") == "<redacted>"
    assert sanitize.scrub("x 12345678abcdefg y") == "x <redacted> y"


def test_adjacent_secrets_merge_into_one_marker() -> None:
    sanitize.configure_scrubber(["aaaaaaaa", "bbbbbbbb"])
    assert sanitize.scrub("aaaaaaaabbbbbbbb") == "<redacted>"
    assert sanitize.scrub("aaaaaaaa bbbbbbbb") == "<redacted> <redacted>"
