"""The search cursor: encode, decode and the validation rules (issue #17).

The cursor is untrusted input: every decoding failure is exit 22 ``cursor_invalid``, never exit 1,
and the message never echoes the cursor.
"""

from __future__ import annotations

import base64
import json
from typing import Any

import pytest

from ticketctl.errors import TicketctlError
from ticketctl.search_cursor import (
    MAX_CURSOR,
    MAX_TOKEN,
    Cursor,
    check,
    decode,
    encode,
    jql_digest,
    valid_token,
)

DIGEST = jql_digest("project = DEMO ORDER BY key")


def b64(raw: bytes | str) -> str:
    data = raw.encode() if isinstance(raw, str) else raw
    return base64.urlsafe_b64encode(data).decode().rstrip("=")


def doc(**over: Any) -> dict[str, Any]:
    base: dict[str, Any] = {"v": 1, "site": "main", "jql_sha256": DIGEST, "page_token": "tok-1"}
    base.update(over)
    return base


def cursor_of(document: Any) -> str:
    return b64(json.dumps(document, sort_keys=True, separators=(",", ":")))


def refused(text: str) -> TicketctlError:
    with pytest.raises(TicketctlError) as info:
        decode(text)
    err = info.value
    assert (err.exit, err.code, err.reason) == (22, "invalid_request", "cursor_invalid")
    assert len(text) < 8 or text not in err.message
    return err


def test_round_trip() -> None:
    text = encode("main", DIGEST, "tok-1")
    assert decode(text) == Cursor("main", DIGEST, "tok-1")
    assert text == text.strip() and "=" not in text and len(text) <= MAX_CURSOR


def test_the_digest_is_sha256_hex_of_the_compiled_query() -> None:
    assert len(DIGEST) == 64 and DIGEST == DIGEST.lower()
    assert jql_digest("a") != jql_digest("a ")


def test_encoded_shape_is_documented() -> None:
    raw = base64.urlsafe_b64decode(encode("main", DIGEST, "t") + "==")
    assert json.loads(raw) == {"v": 1, "site": "main", "jql_sha256": DIGEST, "page_token": "t"}


def test_the_widest_token_with_escapes_still_fits() -> None:
    token = '"\\' * (MAX_TOKEN // 2)
    assert len(token) == MAX_TOKEN
    assert decode(encode("s" * 64, DIGEST, token)).page_token == token


@pytest.mark.parametrize(
    "token", ["", "x" * (MAX_TOKEN + 1), "café", "a\nb", "a\x7fb", "a\x00b", "١", None, 5]
)
def test_unusable_tokens_are_not_valid(token: Any) -> None:
    assert not valid_token(token)


def test_a_provider_token_we_cannot_encode_is_provider_changed() -> None:
    with pytest.raises(TicketctlError) as info:
        encode("main", DIGEST, "café")
    assert info.value.code == "provider_changed"


def test_over_4096_bytes() -> None:
    refused("A" * (MAX_CURSOR + 1))


def test_exactly_at_the_cap_is_judged_on_content() -> None:
    refused("A" * MAX_CURSOR)  # not a document, but refused for that, not for length


def test_nested_100000_deep() -> None:
    refused(b64("[" * 100_000))  # also over the size cap
    refused(b64("[" * 3000 + "]" * 3000))  # inside the size cap, over the depth cap


def test_depth_over_four_is_refused_before_parsing() -> None:
    refused(cursor_of(doc(page_token=[[[[["x"]]]]])))
    refused(cursor_of({"a": {"a": {"a": {"a": {"a": 1}}}}}))


def test_brackets_inside_strings_do_not_count_as_depth() -> None:
    assert decode(cursor_of(doc(page_token="[[[[[[[[[[")))


@pytest.mark.parametrize(
    "text",
    [
        "",
        "!!!!",
        "a b",
        "café",  # non-ASCII
        "١٢",  # non-ASCII digits
        "AAAA\n",
        "A",  # a lone sextet
    ],
)
def test_garbage_text(text: str) -> None:
    refused(text)


def test_padding_is_tolerated_only_in_its_canonical_form() -> None:
    text = encode("main", DIGEST, "tok-1")
    assert decode(text + "=" * (-len(text) % 4)).page_token == "tok-1"
    refused(text + "=" * 3)
    refused(text + "=" * (-len(text) % 4) + "=")


def test_non_canonical_trailing_bits_are_refused() -> None:
    text = encode("main", DIGEST, "tok-1")
    alphabet = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_"
    last = alphabet.index(text[-1])
    tampered = text[:-1] + alphabet[last ^ 1]
    if len(text) % 4 in (2, 3):  # only then are there unused bits to flip
        refused(tampered)


@pytest.mark.parametrize(
    "document",
    [
        None,
        1,
        "x",
        [],
        {},
        doc(v=2),
        doc(v=0),
        doc(v=True),
        doc(v=1.0),
        doc(v="1"),
        doc(site=None),
        doc(site=5),
        doc(jql_sha256=5),
        doc(page_token=None),
        doc(page_token=""),
        doc(page_token="x" * (MAX_TOKEN + 1)),
        doc(page_token="café"),
        doc(page_token="a\nb"),
        doc(page_token={"a": 1}),
        {**doc(), "extra": 1},
        {k: v for k, v in doc().items() if k != "site"},
    ],
    ids=lambda d: json.dumps(d)[:50],
)
def test_bad_documents(document: Any) -> None:
    refused(cursor_of(document))


def test_duplicate_keys_are_refused() -> None:
    raw = f'{{"v":1,"v":1,"site":"main","jql_sha256":"{DIGEST}","page_token":"t"}}'
    refused(b64(raw))


@pytest.mark.parametrize("raw", ["NaN", '{"v":NaN}', '{"v":1', b"\xff\xfe", "﻿{}", "{" * 3])
def test_bad_json(raw: Any) -> None:
    refused(b64(raw))


def test_check_site_and_digest() -> None:
    c = decode(encode("main", DIGEST, "tok-1"))
    check(c, "main", DIGEST)
    for site, digest in (("other", DIGEST), ("main", jql_digest("project = FAKE"))):
        with pytest.raises(TicketctlError) as info:
            check(c, site, digest)
        assert (info.value.exit, info.value.reason) == (22, "cursor_invalid")


def test_a_digest_of_the_wrong_shape_never_matches() -> None:
    c = decode(cursor_of(doc(jql_sha256="x")))
    with pytest.raises(TicketctlError):
        check(c, "main", DIGEST)


def test_the_message_never_echoes_the_cursor() -> None:
    marker = "ZZZSECRETZZZ"
    err = refused(b64(json.dumps(doc(page_token=marker, v=9))))
    assert marker not in err.message and marker not in str(err.details)


def test_a_respelled_but_valid_document_is_not_canonical() -> None:
    assert decode(encode("main", DIGEST, "tok-1")).page_token == "tok-1"
    spaced = json.dumps(doc(), sort_keys=True)  # a space after each separator
    reordered = json.dumps(doc(), separators=(",", ":"))  # insertion order, not sorted
    escaped = '{"jql_sha256":"' + DIGEST + '","page_token":"tok-1","site":"m\\u0061in","v":1}'
    compact_sorted = json.dumps(doc(), sort_keys=True, separators=(",", ":"))
    for text in (spaced, reordered, escaped):
        assert json.loads(text) == doc() and text != compact_sorted
        refused(b64(text))
    assert decode(b64(compact_sorted)) == Cursor("main", DIGEST, "tok-1")


def test_the_size_cap_alone_refuses_a_long_valid_cursor() -> None:
    site = "s" * 4000
    document = {"v": 1, "site": site, "jql_sha256": DIGEST, "page_token": "tok-1"}
    text = b64(json.dumps(document, sort_keys=True, separators=(",", ":")))
    assert len(text) > MAX_CURSOR
    refused(text)
    # the same document under the cap decodes: only the length refuses the long one
    short = b64(json.dumps({**document, "site": "s" * 100}, sort_keys=True, separators=(",", ":")))
    assert len(short) <= MAX_CURSOR and decode(short).site == "s" * 100


def test_the_length_is_judged_before_any_byte_encoding(monkeypatch: pytest.MonkeyPatch) -> None:
    class Spy(str):
        __slots__ = ()

        def encode(self, *args: Any, **kw: Any) -> bytes:
            raise AssertionError("encoded before the length check")

    refused_text = Spy("A" * (MAX_CURSOR + 1))
    with pytest.raises(TicketctlError) as info:
        decode(refused_text)
    assert info.value.reason == "cursor_invalid"


def test_the_depth_guard_alone_refuses_a_deep_cursor(monkeypatch: pytest.MonkeyPatch) -> None:
    from ticketctl import search_cursor

    def parser_must_not_run(*args: Any, **kw: Any) -> Any:
        raise AssertionError("the JSON parser ran on a document over the depth cap")

    monkeypatch.setattr(search_cursor.json, "loads", parser_must_not_run)
    refused(cursor_of(doc(page_token=[[[[["x"]]]]])))


def test_the_parser_is_given_a_hook_that_rejects_json_constants(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A constant (NaN, Infinity) always lands in a field the shape checks refuse anyway, so no
    cursor makes the hook the only defect; the hook is checked through the arguments instead."""
    from ticketctl import search_cursor

    seen: dict[str, Any] = {}
    real = json.loads

    def spy(text: str, **kw: Any) -> Any:
        seen.update(kw)
        return real(text, **kw)

    monkeypatch.setattr(search_cursor.json, "loads", spy)
    decode(encode("main", DIGEST, "tok-1"))
    for constant in ("NaN", "Infinity", "-Infinity"):
        with pytest.raises(ValueError):
            seen["parse_constant"](constant)
