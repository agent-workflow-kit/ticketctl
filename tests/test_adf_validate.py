"""The ADF allowlist validator and the --format adf input path (interface.md, ADF allowlist)."""

from __future__ import annotations

import copy
import hashlib
import json
import random
import re
import time
from pathlib import Path
from typing import Any

import jsonschema
import pytest

from ticketctl import adf_validate
from ticketctl.adf_from_markdown import markdown_to_adf, text_chars
from ticketctl.adf_validate import (
    body_sha256,
    canonical_json,
    parse_adf_json,
    validate_adf,
)
from ticketctl.sanitize import InputError

ROOT = Path(__file__).resolve().parent.parent
SCHEMA = json.loads((ROOT / "schemas" / "adf.json").read_text(encoding="utf-8"))
Node = dict[str, Any]


def doc(*content: Any) -> Node:
    return {"type": "doc", "version": 1, "content": list(content)}


def para(*content: Any) -> Node:
    return {"type": "paragraph", "content": list(content)}


def txt(text: str, *marks: Node) -> Node:
    node: Node = {"type": "text", "text": text}
    if marks:
        node["marks"] = list(marks)
    return node


def link(href: Any) -> Node:
    return {"type": "link", "attrs": {"href": href}}


def mark(kind: str) -> Node:
    return {"type": kind}


def item(*content: Any) -> Node:
    return {"type": "listItem", "content": list(content)}


def bullets(*items: Any) -> Node:
    return {"type": "bulletList", "content": list(items)}


def quote(*content: Any) -> Node:
    return {"type": "blockquote", "content": list(content)}


def nested_lists(levels: int, leaf: Node | None = None) -> Node:
    node = bullets(item(leaf or para(txt("x"))))
    for _ in range(levels - 1):
        node = bullets(item(node))
    return node


def nested_quotes(levels: int) -> Node:
    node = quote(para(txt("x")))
    for _ in range(levels - 1):
        node = quote(node)
    return node


def refused(d: Any, reason: str = "node_not_allowed", **kw: Any) -> InputError:
    with pytest.raises(InputError) as info:
        validate_adf(d, **kw)
    exc = info.value
    assert (exc.exit_code, exc.code, exc.reason) == (22, "invalid_request", reason)
    return exc


def where(exc: InputError) -> str:
    assert exc.details is not None
    return exc.details["field"]


def field_of(d: Any) -> str:
    return where(refused(d))


# --- the accepted shapes --------------------------------------------------------------------


def test_every_allowed_node_and_mark_is_accepted() -> None:
    d = doc(
        {"type": "heading", "attrs": {"level": 6}, "content": [txt("h")]},
        para(
            txt("a", mark("strong"), mark("em")),
            {"type": "hardBreak"},
            txt("b", mark("code"), link("https://example.test/a?b=c#d")),
        ),
        {"type": "codeBlock", "attrs": {"language": "c++"}, "content": [txt("x = 1\n")]},
        {"type": "codeBlock"},
        {"type": "orderedList", "attrs": {"order": 9999}, "content": [item(para(txt("o")))]},
        {"type": "orderedList", "content": [item(para(txt("o")))]},
        bullets(item(para(txt("b")), nested_lists(2))),
        quote(para(), {"type": "heading", "attrs": {"level": 1}}),
    )
    assert validate_adf(d) is None


def test_the_empty_doc_is_structurally_valid() -> None:
    assert validate_adf(doc()) is None


def test_validation_does_not_modify_the_document() -> None:
    d = doc(para(txt("a", link("https://example.test/"))))
    before = copy.deepcopy(d)
    validate_adf(d)
    assert d == before


def test_converter_output_is_always_valid() -> None:
    rng = random.Random(21)
    pieces = [
        "# h\n",
        "- a\n  - b\n    1. c\n",
        "> q\n> > r\n",
        "```py\ncode\n```\n",
        "**b** _i_ `c` [l](https://example.test/x)\n",
        "line\\\nbreak  \nnext\n",
        "@mention [~accountid:1] ![i](https://example.test/i.png) <b>x</b>\n",
        "[bad](javascript:alert(1)) [u](https://a@b.test/)\n",
        "‮\x00 text\n",
        "1. one\n2. two\n",
    ]
    for _ in range(200):
        md = "\n".join(rng.choice(pieces) for _ in range(rng.randint(1, 8)))
        validate_adf(markdown_to_adf(md, body_chars=32767), body_chars=32767)


def test_five_nested_lists_are_depth_ten_and_a_paragraph_inside_is_accepted() -> None:
    d = doc(nested_lists(5))
    assert validate_adf(d) is None
    md = "- a\n  - b\n    - c\n      - d\n        - e\n          deep paragraph\n"
    validate_adf(markdown_to_adf(md))


def test_a_sixth_nested_list_is_refused_with_its_path() -> None:
    assert (
        field_of(doc(nested_lists(6)))
        == "content[0].content[0].content[0].content[0].content[0].content[0]"
        ".content[0].content[0].content[0].content[0].content[0]"
    )


def test_depth_counts_blockquotes_and_lists_together() -> None:
    validate_adf(doc(nested_quotes(10)))
    refused(doc(nested_quotes(11)))
    # 4 lists = depth 8, 2 quotes on top = 10, one more quote = 11
    inner = nested_lists(4)
    ok = doc(quote(quote(inner)))
    validate_adf(ok)
    refused(doc(quote(quote(quote(inner)))))
    # a list item under a quote under a list
    refused(doc(nested_lists(5, leaf=quote(para(txt("x"))))))


def test_deep_nesting_is_refused_iteratively_without_recursion_error() -> None:
    n = {"type": "paragraph"}
    for _ in range(200_000):
        n = {"type": "blockquote", "content": [n]}
    start = time.process_time()
    refused(doc(n))
    assert time.process_time() - start < 10


def test_exactly_five_thousand_nodes_pass_and_one_more_is_body_too_large() -> None:
    # doc + 4999 paragraphs = 5000 nodes
    validate_adf(doc(*[{"type": "paragraph"} for _ in range(4999)]))
    exc = refused(doc(*[{"type": "paragraph"} for _ in range(5000)]), "body_too_large")
    assert exc.details is None


def test_reserve_nodes_and_reserve_count_against_the_limits() -> None:
    d = doc(*[{"type": "paragraph"} for _ in range(4990)])
    validate_adf(d, reserve_nodes=9)
    refused(d, "body_too_large", reserve_nodes=10)
    t = doc(para(txt("a" * 10)))
    validate_adf(t, body_chars=10)
    refused(t, "body_too_large", body_chars=10, reserve=1)
    refused(doc(para(txt("a" * 11))), "body_too_large", body_chars=10)


def test_node_bomb_stops_early_and_is_fast() -> None:
    wide = doc(*[{"type": "paragraph"} for _ in range(1_000_000)])
    start = time.process_time()
    refused(wide, "body_too_large")
    assert time.process_time() - start < 10
    huge_marks = doc(para({"type": "text", "text": "a", "marks": [mark("strong")] * 1_000_000}))
    start = time.process_time()
    refused(huge_marks)
    assert time.process_time() - start < 10


def test_a_million_valid_children_are_refused_after_bounded_work(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The bound is the number of nodes ever queued (work done), not elapsed time: the unbounded
    # queue held a million entries, the bounded one never more than about the node limit.
    deepest = [0]
    real_queue = adf_validate._queue

    def watching(parent: Any, path: Any, depth: int, kind: str, stack: Any, room: int) -> None:
        real_queue(parent, path, depth, kind, stack, room)
        deepest[0] = max(deepest[0], len(stack))

    monkeypatch.setattr(adf_validate, "_queue", watching)
    wide = doc(*[para() for _ in range(1_000_000)])
    start = time.monotonic()
    refused(wide, "body_too_large")
    assert time.monotonic() - start < 60  # only a backstop
    assert 0 < deepest[0] <= adf_validate.MAX_NODES + 1


def test_a_wide_container_of_exactly_the_limit_is_accepted() -> None:
    validate_adf(doc(*[para() for _ in range(adf_validate.MAX_NODES - 1)]))
    refused(doc(*[para() for _ in range(adf_validate.MAX_NODES)]), "body_too_large")


def test_the_validator_agrees_with_the_converters_node_count() -> None:
    md = "\n\n".join("p" for _ in range(2400))  # 2400 paragraphs + 2400 text + doc = 4801
    d = markdown_to_adf(md)
    validate_adf(d)
    assert text_chars(d) == 2400


# --- refused nodes, marks and attributes ---------------------------------------------------


@pytest.mark.parametrize(
    "node",
    [
        {"type": "mention", "attrs": {"id": "x", "text": "@x"}},
        {"type": "media", "attrs": {"id": "x", "type": "file", "collection": "c"}},
        {"type": "mediaSingle", "content": []},
        {"type": "mediaGroup", "content": []},
        {"type": "inlineCard", "attrs": {"url": "https://example.test/"}},
        {"type": "blockCard", "attrs": {"url": "https://example.test/"}},
        {"type": "embedCard", "attrs": {"url": "https://example.test/", "layout": "wide"}},
        {"type": "emoji", "attrs": {"shortName": ":x:"}},
        {"type": "status", "attrs": {"text": "x", "color": "red"}},
        {"type": "panel", "attrs": {"panelType": "info"}, "content": [para()]},
        {"type": "table", "content": []},
        {"type": "rule"},
        {"type": "date", "attrs": {"timestamp": "1"}},
        {"type": "expand", "content": [para()]},
        {"type": "taskList", "content": []},
        {"type": "extension", "attrs": {}},
        {"type": "Paragraph"},
        {"type": "paragraph ", "content": []},
        {"type": ""},
        {"type": None},
        {"type": 5},
        {"type": ["paragraph"]},
        {"type": {"a": 1}},
        {},
        "paragraph",
        None,
        7,
        ["paragraph"],
    ],
)
def test_unknown_node_is_refused_with_its_path(node: Any) -> None:
    assert field_of(doc(para(), node)) == "content[1]"


@pytest.mark.parametrize(
    "bad",
    [
        {"type": "paragraph", "attrs": {}},
        {"type": "paragraph", "attrs": {"localId": "x"}},
        {"type": "paragraph", "marks": [mark("strong")]},
        {"type": "paragraph", "extra": 1},
        {"type": "paragraph", "content": [], "text": "x"},
        {"type": "heading", "attrs": {"level": 1, "id": "x"}},
        {"type": "heading", "attrs": {}},
        {"type": "heading"},
        {"type": "heading", "attrs": {"level": 1}, "marks": []},
        {"type": "bulletList", "attrs": {}, "content": [item(para())]},
        {"type": "bulletList", "content": []},
        {"type": "bulletList"},
        {"type": "orderedList", "attrs": {"order": 1, "type": "a"}, "content": [item(para())]},
        {"type": "orderedList", "content": []},
        {"type": "listItem", "content": [para()]},  # not inside a list
        {"type": "blockquote", "content": []},
        {"type": "blockquote"},
        {"type": "blockquote", "attrs": {}, "content": [para()]},
        {"type": "codeBlock", "attrs": {"language": "py", "x": 1}},
        {"type": "codeBlock", "attrs": {}, "content": [txt("x")], "extra": 1},
        {"type": "hardBreak"},  # hard break is inline only
        {"type": "text", "text": "x"},  # text is inline only
    ],
)
def test_extra_missing_or_misplaced_parts_in_a_block_are_refused(bad: Any) -> None:
    assert field_of(doc(para(), bad)).startswith("content[1]")


@pytest.mark.parametrize(
    "bad",
    [
        {"type": "text"},
        {"type": "text", "text": ""},
        {"type": "text", "text": 5},
        {"type": "text", "text": None},
        {"type": "text", "text": ["a"]},
        {"type": "text", "text": "a", "attrs": {}},
        {"type": "text", "text": "a", "extra": True},
        {"type": "text", "text": "a", "marks": {}},
        {"type": "text", "text": "a", "marks": "strong"},
        {"type": "text", "text": "a", "marks": [None]},
        {"type": "text", "text": "a", "marks": ["strong"]},
        {"type": "text", "text": "a", "marks": [{}]},
        {"type": "text", "text": "a", "marks": [{"type": "textColor", "attrs": {"color": "#f00"}}]},
        {"type": "text", "text": "a", "marks": [{"type": "underline"}]},
        {"type": "text", "text": "a", "marks": [{"type": "strike"}]},
        {"type": "text", "text": "a", "marks": [{"type": "annotation", "attrs": {"id": "x"}}]},
        {"type": "text", "text": "a", "marks": [{"type": "Strong"}]},
        {"type": "text", "text": "a", "marks": [{"type": ["strong"]}]},
        {"type": "text", "text": "a", "marks": [{"type": "strong", "attrs": {}}]},
        {"type": "text", "text": "a", "marks": [{"type": "strong", "x": 1}]},
        {"type": "text", "text": "a", "marks": [{"type": "link"}]},
        {"type": "text", "text": "a", "marks": [{"type": "link", "attrs": {}}]},
        {
            "type": "text",
            "text": "a",
            "marks": [{"type": "link", "attrs": {"href": "https://a.test/", "title": "t"}}],
        },
        {
            "type": "text",
            "text": "a",
            "marks": [{"type": "link", "attrs": {"href": "https://a.test/"}, "x": 1}],
        },
        {"type": "text", "text": "a", "marks": [{"type": "link", "attrs": []}]},
        {"type": "text", "text": "a", "marks": [mark("strong"), mark("strong")]},
        {"type": "text", "text": "a", "marks": [link("https://a.test/"), link("https://b.test/")]},
        {"type": "text", "text": "a", "marks": [mark("code"), mark("strong")]},
        {"type": "text", "text": "a", "marks": [mark("em"), mark("code")]},
        {"type": "hardBreak", "text": "x"},
        {"type": "hardBreak", "attrs": {}},
    ],
)
def test_bad_inline_nodes_and_marks_are_refused(bad: Any) -> None:
    assert field_of(doc(para(txt("ok"), bad))).startswith("content[0].content[1]")


def test_mark_path_names_the_mark_and_attribute() -> None:
    d = doc(para(txt("ok"), txt("a", mark("strong"), link("javascript:alert(1)"))))
    assert field_of(d) == "content[0].content[1].marks[1].attrs.href"
    d = doc(para(txt("a", {"type": "link", "attrs": {"href": "https://a.test/", "x": 1}})))
    assert field_of(d) == "content[0].content[0].marks[0].attrs.x"


def test_marks_in_a_code_block_are_refused() -> None:
    assert (
        field_of(doc({"type": "codeBlock", "content": [txt("x", mark("strong"))]}))
        == "content[0].content[0].marks"
    )
    refused(doc({"type": "codeBlock", "content": [para(txt("x"))]}))
    refused(doc({"type": "codeBlock", "content": [{"type": "hardBreak"}]}))


def test_an_empty_marks_list_is_accepted_like_the_schema() -> None:
    validate_adf(doc(para({"type": "text", "text": "a", "marks": []})))


@pytest.mark.parametrize(
    "href",
    [
        "javascript:alert(1)",
        "JaVaScRiPt:alert(1)",
        "data:text/html,<script>alert(1)</script>",
        "vbscript:x",
        "file:///etc/passwd",
        "ftp://example.test/",
        "mailto:a@example.test",
        "//example.test/",
        "/relative",
        "example.test",
        "https:example.test",
        "https:/example.test",
        "HTTPS://example.test/",
        "Http://example.test/",
        " https://example.test/",
        "\thttps://example.test/",
        "\x00https://example.test/",
        "https://example.test/ ",
        "https://example.test/a b",
        "https://example.test/\n",
        "https://example.test/\r",
        "https://example.test/\x00",
        "https://example.test/\x7f",
        "https://example.test/\x1b[31m",
        "https://example.test/‮",
        "https://example.test/​",
        "https://example.test/ ",
        "https://example.test/é",
        "https://éxample.test/",
        "https://user@example.test/",
        "https://user:pw@example.test/",
        "https://@example.test/",
        "http://u@example.test/",
        "https://",
        "http://",
        "",
        "https://" + "a" * 2001,
        "https://" + "a" * 5000,
        "javascript:" + "a" * 100,
        "https://example.test/﻿",
    ],
)
def test_unsafe_link_hrefs_are_refused_with_the_path_named(href: str) -> None:
    d = doc(para(txt("ok"), txt("a", link(href))))
    assert field_of(d) == "content[0].content[1].marks[0].attrs.href"


@pytest.mark.parametrize(
    "href", [5, None, True, ["https://a.test/"], {"u": "https://a.test/"}, 1.5]
)
def test_a_non_string_href_is_refused(href: Any) -> None:
    assert field_of(doc(para(txt("a", link(href))))).endswith("attrs.href")


@pytest.mark.parametrize(
    "href",
    [
        "https://example.test/",
        "http://example.test",
        "https://example.test:8443/a/b?c=d&e=f#g",
        "https://example.test/@name",
        "https://example.test/?u=a@b",
        "https://example.test/a%20b",
        "https://[::1]/",
        "https://" + "a" * 2000,
        "https://example.test/" + "a" * 1979,
    ],
)
def test_safe_link_hrefs_are_accepted(href: str) -> None:
    validate_adf(doc(para(txt("a", link(href)))))


def test_href_limit_is_the_schema_pattern_two_thousand_after_the_scheme() -> None:
    validate_adf(doc(para(txt("a", link("https://" + "a" * 2000)))))
    refused(doc(para(txt("a", link("https://" + "a" * 2001)))))


@pytest.mark.parametrize(
    "text",
    [
        "a\x00b",
        "a\x01b",
        "a\x1bb",
        "a\x7fb",
        "a\x9fb",
        "a\rb",
        "a\r\nb",
        "a‮b",
        "a‪b",
        "a⁦b",
        "a​b",
        "a‎b",
        "a﻿b",
        "a b",
        "a b",
        "a\ud800b",
        "a\U000e0001b",
        "ab",
        "\x00",
    ],
)
def test_control_and_bidirectional_text_is_refused_not_repaired(text: str) -> None:
    d = doc(para(txt("ok"), txt(text)))
    assert field_of(d) == "content[0].content[1].text"
    code = doc({"type": "codeBlock", "content": [txt(text)]})
    assert field_of(code) == "content[0].content[0].text"


def test_text_the_sanitizer_would_leave_alone_is_accepted() -> None:
    d = doc(
        para(txt("tab\there\nnew line, café, 日本語, \U0001f600, <U+202E> marker")),
        {"type": "codeBlock", "content": [txt("a\n\tb\n")]},
    )
    validate_adf(d)


def test_the_error_never_echoes_the_rejected_value() -> None:
    secret = "tok-0123456789abcdef"
    for bad in (
        doc(para(txt("x", link("javascript:" + secret)))),
        doc(para(txt("a\x00" + secret))),
        doc({"type": "mention_" + secret}),
    ):
        exc = refused(bad)
        assert secret not in exc.message
        assert secret not in json.dumps(exc.details)


def test_hostile_attribute_names_are_sanitized_and_capped_in_the_path() -> None:
    d = doc(
        para(
            txt(
                "a",
                {"type": "link", "attrs": {"href": "https://a.test/", "\x1b[2J‮" + "k" * 500: 1}},
            )
        )
    )
    path = where(refused(d))
    assert "\x1b" not in path and "‮" not in path
    assert len(path) < 200


@pytest.mark.parametrize(
    "attrs",
    [
        {"level": 0},
        {"level": 7},
        {"level": -1},
        {"level": True},
        {"level": False},
        {"level": 1.0},
        {"level": 2.5},
        {"level": "2"},
        {"level": None},
        {"level": [1]},
        {"level": 10**400},
        {"level": float("nan")},
        {"level": float("inf")},
    ],
)
def test_heading_level_must_be_an_int_one_to_six(attrs: Any) -> None:
    assert field_of(doc({"type": "heading", "attrs": attrs})).startswith("content[0].attrs")


@pytest.mark.parametrize("order", [0, -1, 10000, True, 1.0, "1", None, 10**400, float("nan")])
def test_ordered_list_order_must_be_an_int_one_to_9999(order: Any) -> None:
    d = doc({"type": "orderedList", "attrs": {"order": order}, "content": [item(para())]})
    assert field_of(d) == "content[0].attrs.order"


@pytest.mark.parametrize("language", ["", "a b", "a" * 33, "py\n", "pú", "<x>", 5, None, "a/b"])
def test_code_block_language_follows_the_pattern(language: Any) -> None:
    d = doc({"type": "codeBlock", "attrs": {"language": language}})
    assert field_of(d) == "content[0].attrs.language"


@pytest.mark.parametrize("language", ["py", "c++", "c#", "a.b_c-d", "a" * 32, "X1"])
def test_code_block_language_accepts_the_pattern(language: str) -> None:
    validate_adf(doc({"type": "codeBlock", "attrs": {"language": language}}))


@pytest.mark.parametrize(
    "bad",
    [
        None,
        [],
        "doc",
        5,
        {},
        {"type": "doc"},
        {"type": "doc", "version": 1},
        {"type": "doc", "content": []},
        {"type": "doc", "version": 2, "content": []},
        {"type": "doc", "version": True, "content": []},
        {"type": "doc", "version": 1.0, "content": []},
        {"type": "doc", "version": "1", "content": []},
        {"type": "doc", "version": 1, "content": {}},
        {"type": "doc", "version": 1, "content": ()},
        {"type": "doc", "version": 1, "content": "x"},
        {"type": "doc", "version": 1, "content": [], "extra": 1},
        {"type": "doc", "version": 1, "content": [], "attrs": {}},
        {"type": "paragraph", "content": []},
        {"type": "Doc", "version": 1, "content": []},
        {"version": 1, "content": []},
    ],
)
def test_the_document_root_is_strict(bad: Any) -> None:
    exc = refused(bad)
    assert where(exc).startswith("$") or where(exc) != ""


def test_the_root_path_is_named() -> None:
    assert where(refused(None)) == "$"
    assert where(refused(doc_with("version", 2))) == "version"
    assert where(refused({"type": "doc", "version": 1, "content": [], "x": 1})) == "x"


def doc_with(key: str, value: Any) -> Node:
    d = doc()
    d[key] = value
    return d


def test_non_string_keys_and_non_native_containers_are_refused() -> None:
    refused({"type": "doc", "version": 1, "content": [], 1: "x"})

    class D(dict):  # type: ignore[type-arg]
        pass

    refused(D(type="doc", version=1, content=[]))

    class L(list):  # type: ignore[type-arg]
        pass

    refused({"type": "doc", "version": 1, "content": L()})
    refused(doc(para(txt("a", {"type": "link", "attrs": {"href": "https://a.test/", 5: 1}}))))


def test_a_cyclic_document_is_refused_and_never_loops() -> None:
    d = doc()
    d["content"].append(d)
    refused(d)
    q = quote()
    q["content"].append(q)
    refused(doc(q))


def test_a_shared_subtree_counts_every_use() -> None:
    p = para(txt("x"))
    d = doc(*[p] * 2600)  # 1 + 2600 * 2 nodes
    refused(d, "body_too_large")


def test_never_raises_anything_but_input_error_on_random_json() -> None:
    rng = random.Random(1)
    atoms: list[Any] = [
        None,
        True,
        False,
        0,
        1,
        -1,
        10**30,
        1.5,
        "",
        "a",
        "doc",
        "text",
        "paragraph",
    ]

    def rand(depth: int) -> Any:
        r = rng.random()
        if depth > 5 or r < 0.4:
            return rng.choice(atoms)
        if r < 0.7:
            return [rand(depth + 1) for _ in range(rng.randint(0, 3))]
        keys = ["type", "version", "content", "text", "attrs", "marks", "href", "level"]
        return {rng.choice(keys): rand(depth + 1) for _ in range(rng.randint(0, 4))}

    for _ in range(3000):
        try:
            validate_adf(rand(0))
        except InputError:
            pass


# --- the differential check against schemas/adf.json ----------------------------------------


def schema_valid(d: Any) -> bool:
    return jsonschema.Draft202012Validator(SCHEMA).is_valid(d)


def test_the_schema_file_is_itself_a_valid_schema() -> None:
    jsonschema.Draft202012Validator.check_schema(SCHEMA)


def test_the_code_constants_match_the_schema_patterns() -> None:
    defs = SCHEMA["$defs"]
    href = defs["mark"]["oneOf"][1]["properties"]["attrs"]["properties"]["href"]["pattern"]
    assert href == "^" + adf_validate.HREF.pattern
    lang = defs["codeBlock"]["properties"]["attrs"]["properties"]["language"]["pattern"]
    assert lang == "^" + adf_validate.LANGUAGE.pattern
    nodes = {"doc"} | {
        d["properties"]["type"]["const"]
        for d in defs.values()
        if "const" in d.get("properties", {}).get("type", {})
    }
    assert nodes == set(adf_validate.NODE_TYPES)
    marks = set(defs["mark"]["oneOf"][0]["properties"]["type"]["enum"]) | {"link"}
    assert marks == set(adf_validate.MARK_TYPES)


_TEXTS = ["a", "word", "two words", "x\ny", "café", "<U+202E>"]
_CONTAINER_DEFS = {"bulletList", "orderedList", "blockquote"}
_HREFS = [
    "https://example.test/",
    "http://example.test/a?b#c",
    "https://example.test:80/x",
    "https://a",  # length 1 after the scheme
    "http://ab",  # length 2
    "https://" + "a" * 2000,  # the limit
    "https://" + "a" * 2001,  # one over (schema-invalid; filtered or mutated)
]
_LANGS = ["py", "c++", "a.b", "a", "a" * 32, "a" * 33]


def gen(ref: str, rng: random.Random, depth: int = 0) -> Any:
    """Generate a document from the schema (the generator reads the schema only)."""
    return _gen_schema(SCHEMA["$defs"][ref.split("/")[-1]], rng, depth)


def _gen_schema(s: Any, rng: random.Random, depth: int) -> Any:
    if "$ref" in s:
        return gen(s["$ref"], rng, depth)
    if "oneOf" in s:
        options = s["oneOf"]
        if depth > 5:  # stay finite: no more containers below this depth
            leaves = [o for o in options if o.get("$ref", "").split("/")[-1] not in _CONTAINER_DEFS]
            options = leaves or options
        return _gen_schema(rng.choice(options), rng, depth)
    if "const" in s:
        return s["const"]
    if "enum" in s:
        return rng.choice(s["enum"])
    t = s.get("type")
    if t == "object":
        props = s.get("properties", {})
        required = set(s.get("required", []))
        out: Node = {}
        for k, sub in props.items():
            if k in required or rng.random() < 0.6:
                out[k] = _gen_schema(sub, rng, depth + 1)
        return out
    if t == "array":
        lo = s.get("minItems", 0)
        n = rng.randint(lo, max(lo, 2 if depth < 4 else 1))
        return [_gen_schema(s["items"], rng, depth + 1) for _ in range(n)]
    if t == "integer":
        lo, hi = s["minimum"], s["maximum"]
        if rng.random() < 0.5:  # boundaries: 0/1/6/7 for levels, 0/1/9999/10000 for order
            return rng.choice([lo - 1, lo, hi, hi + 1])
        return rng.randint(lo, hi)
    if t == "string":
        pat = s.get("pattern", "")
        if pat.startswith("^(?!https?://"):
            return rng.choice(_HREFS)
        if pat:
            return rng.choice(_LANGS)
        return rng.choice(_TEXTS)
    raise AssertionError(f"generator does not know {s}")


def test_documents_generated_from_the_schema_are_accepted_by_the_code() -> None:
    rng = random.Random(7)
    checked = 0
    for _ in range(1200):
        d = gen("doc", rng)
        if not schema_valid(d):  # the generator ignores allOf mark constraints; filter
            continue
        validate_adf(d)
        checked += 1
    assert checked > 500


def mutate(d: Any, rng: random.Random) -> Any:
    d = copy.deepcopy(d)
    spots: list[tuple[Any, Any]] = []

    def walk(x: Any) -> None:
        if isinstance(x, dict):
            for k in list(x):
                spots.append((x, k))
                walk(x[k])
        elif isinstance(x, list):
            for i in range(len(x)):
                spots.append((x, i))
                walk(x[i])

    walk(d)
    if not spots:
        return d
    parent, key = rng.choice(spots)
    action = rng.choice(["delete", "extra", "type", "value", "dupe", "swap"])
    if action == "delete" and isinstance(parent, dict):
        del parent[key]
    elif action == "delete":
        del parent[key]
    elif action == "extra" and isinstance(parent, dict):
        parent[rng.choice(["x", "attrs", "marks", "content", "text", "localId"])] = rng.choice(
            [1, "a", {}, []]
        )
    elif action == "type":
        parent[key] = rng.choice([None, True, "a", [], {}, 0, -1, 7, 10000, "javascript:x"])
    elif action == "value" and isinstance(parent[key], str):
        parent[key] = rng.choice(
            [
                "mention",
                "media",
                "https://u@example.test/",
                "https://a.test/\n",
                "https://a.test\\.b.test/",
                "py\n",
                "a" * 33,
                "ftp://example.test/",
                "",
                "javascript:alert(1)",
                "textColor",
                "link",
                "strong",
                "code",
            ]
        )
    elif action == "dupe" and isinstance(parent, list):
        parent.insert(key, copy.deepcopy(parent[key]))
    elif action == "swap" and isinstance(parent, dict) and "type" in parent:
        parent["type"] = rng.choice(
            ["paragraph", "text", "listItem", "heading", "doc", "hardBreak"]
        )
    return d


def test_code_and_schema_accept_exactly_the_same_mutated_documents() -> None:
    rng = random.Random(11)
    agree = refused_n = 0
    for _ in range(2000):
        d = mutate(gen("doc", rng), rng)
        for _ in range(rng.randint(0, 2)):
            d = mutate(d, rng)
        try:
            validate_adf(d)
            ok = True
        except InputError:
            ok = False
        assert ok == schema_valid(d), json.dumps(d)
        agree += 1
        refused_n += not ok
    assert refused_n > 300 and agree - refused_n > 150


# --- the --format adf input path -------------------------------------------------------------


def dumps(d: Any) -> str:
    return json.dumps(d)


def parse_refused(text: str, reason: str, **kw: Any) -> InputError:
    with pytest.raises(InputError) as info:
        parse_adf_json(text, **kw)
    exc = info.value
    assert (exc.exit_code, exc.code, exc.reason) == (22, "invalid_request", reason)
    return exc


def test_a_prepared_document_is_parsed_and_validated() -> None:
    d = doc(para(txt("hello", mark("strong"))))
    assert parse_adf_json(dumps(d)) == d


def test_parse_refuses_what_validate_refuses_with_the_path() -> None:
    d = doc(para(txt("a", link("javascript:alert(1)"))))
    exc = parse_refused(dumps(d), "node_not_allowed")
    assert where(exc) == "content[0].content[0].marks[0].attrs.href"


def test_a_json_escaped_control_character_is_refused() -> None:
    parse_refused(
        '{"type":"doc","version":1,"content":[{"type":"paragraph","content":'
        '[{"type":"text","text":"a\\u0000b"}]}]}',
        "node_not_allowed",
    )
    parse_refused(
        '{"type":"doc","version":1,"content":[{"type":"paragraph","content":'
        '[{"type":"text","text":"a\\u202eb"}]}]}',
        "node_not_allowed",
    )
    parse_refused(
        '{"type":"doc","version":1,"content":[{"type":"paragraph","content":'
        '[{"type":"text","text":"a\\ud800b"}]}]}',
        "node_not_allowed",
    )


@pytest.mark.parametrize(
    "text",
    [
        "",
        " ",
        "{",
        "{'type': 'doc'}",
        '{"type":"doc","version":1,"content":[],}',
        '{"type":"doc","version":NaN,"content":[]}',
        '{"type":"doc","version":Infinity,"content":[]}',
        '{"type":"doc","version":-Infinity,"content":[]}',
        '{"type":"doc","version":1,"content":[]} trailing',
        '{"type":"doc","version":1,"content":[]}{"type":"doc"}',
        '{"type":"doc","type":"doc","version":1,"content":[]}',
        '{"type":"doc","version":1,"version":1,"content":[]}',
        '{"type":"doc","version":1,"content":[{"type":"paragraph","type":"mention"}]}',
        '{"type":"doc","version":1,"content":[{"type":"paragraph","content":'
        '[{"type":"text","text":"a","text":"b"}]}]}',
    ],
)
def test_malformed_json_nan_and_duplicate_keys_are_refused(text: str) -> None:
    parse_refused(text, "invalid_value")


@pytest.mark.parametrize("text", ["[]", "null", "5", '"doc"', "true", "{}"])
def test_valid_json_that_is_not_a_document_is_node_not_allowed(text: str) -> None:
    assert where(parse_refused(text, "node_not_allowed")) in (
        "$",
        "version",
        "type",
        "content",
    )


def test_duplicate_key_with_different_escape_is_still_a_duplicate() -> None:
    parse_refused('{"type":"doc","\\u0074ype":"x","version":1,"content":[]}', "invalid_value")


def test_a_huge_integer_literal_is_refused_not_an_unhandled_error() -> None:
    parse_refused(
        '{"type":"doc","version":1,"content":[],"n":' + "9" * 100_000 + "}", "invalid_value"
    )
    parse_refused(
        '{"type":"doc","version":' + "9" * 5000 + ',"content":[]}',
        "invalid_value",
    )


def test_a_huge_exponent_float_is_refused() -> None:
    parse_refused('{"type":"doc","version":1e999,"content":[]}', "invalid_value")
    parse_refused('{"type":"doc","version":1.0,"content":[]}', "node_not_allowed")


def test_deeply_nested_json_is_refused_without_recursion_error_and_fast() -> None:
    for text in ("[" * 100_000, '{"a":' * 20_000 + "1" + "}" * 20_000, "[" * 65 + "]" * 65):
        start = time.process_time()
        parse_refused(text, "invalid_value", body_chars=32767)
        assert time.process_time() - start < 10
    start = time.process_time()
    parse_refused("[" * 1_000_000, "body_too_large")
    assert time.process_time() - start < 10


def test_brackets_inside_strings_do_not_count_as_nesting() -> None:
    s = "[" * 5000 + "{" * 5000
    d = doc(para(txt(s)))
    assert parse_adf_json(dumps(d), body_chars=32767) == d
    escaped = doc(para(txt('"' * 3000 + "\\" * 3000 + "[" * 3000)))
    assert parse_adf_json(dumps(escaped), body_chars=32767) == escaped


def test_json_nesting_just_inside_the_guard_still_reaches_the_validator() -> None:
    parse_refused(dumps(doc(nested_quotes(11))), "node_not_allowed")
    parse_adf_json(dumps(doc(nested_quotes(10))))


def test_input_over_four_times_body_chars_is_body_too_large_before_parsing() -> None:
    parse_refused("[" * 41, "body_too_large", body_chars=10)
    parse_refused(" " * 41, "body_too_large", body_chars=10)


def test_text_over_the_body_limit_is_body_too_large() -> None:
    parse_refused(dumps(doc(para(txt("a" * 101)))), "body_too_large", body_chars=100)
    parse_refused(dumps(doc(para(txt("a" * 100)))), "body_too_large", body_chars=100, reserve=1)
    parse_adf_json(dumps(doc(para(txt("a" * 100)))), body_chars=100)


def test_a_document_with_no_visible_text_is_empty_body() -> None:
    parse_refused(dumps(doc()), "empty_body")
    parse_refused(dumps(doc(para())), "empty_body")
    parse_refused(dumps(doc(para(txt(" \n\t")))), "empty_body")
    parse_refused(
        dumps(doc({"type": "paragraph", "content": [{"type": "hardBreak"}]})), "empty_body"
    )
    parse_adf_json(dumps(doc(para(txt("<U+0000>")))))


def test_the_node_limit_applies_to_prepared_documents() -> None:
    parse_refused(dumps(doc(*[para(txt("x")) for _ in range(2500)])), "body_too_large")
    parse_refused(
        dumps(doc(*[para(txt("x")) for _ in range(2000)])), "body_too_large", reserve_nodes=1000
    )


def test_a_non_string_input_is_a_type_error() -> None:
    with pytest.raises(TypeError):
        parse_adf_json(b"{}")  # type: ignore[arg-type]


# --- the body hash --------------------------------------------------------------------------


def test_canonical_json_is_sorted_compact_utf8_unescaped() -> None:
    d = {"b": 1, "a": {"d": "café", "c": [1, 2]}}
    assert canonical_json(d) == '{"a":{"c":[1,2],"d":"café"},"b":1}'


def test_body_hash_is_sha256_of_canonical_json() -> None:
    d = doc(para(txt("café")))
    expected = hashlib.sha256(canonical_json(d).encode("utf-8")).hexdigest()
    assert body_sha256(d) == expected
    assert len(expected) == 64


def test_identical_markdown_and_adf_input_give_the_same_hash() -> None:
    md = (
        "# Title\n\nSome **bold** and `code` and [a link](https://example.test/x?y=1).\n\n"
        "- one\n- two\n  1. nested\n\n> quote\n\n```py\nx = 1\n```\n"
    )
    converted = markdown_to_adf(md)
    prepared = parse_adf_json(json.dumps(converted, indent=2, sort_keys=False, ensure_ascii=False))
    shuffled = parse_adf_json(json.dumps(converted, sort_keys=True))
    assert body_sha256(converted) == body_sha256(prepared) == body_sha256(shuffled)


def test_a_different_document_gives_a_different_hash() -> None:
    assert body_sha256(doc(para(txt("a")))) != body_sha256(doc(para(txt("b"))))


def test_hash_refuses_an_invalid_document() -> None:
    with pytest.raises(InputError):
        body_sha256(doc({"type": "mention"}))


# --- round trip through the renderer ---------------------------------------------------------


def test_markdown_to_adf_to_markdown_preserves_the_supported_dialect() -> None:
    from ticketctl.adf_render import render_adf

    md = (
        "# Title\n\nSome **bold**, _italic_ and `code` with [a link](https://example.test/x).\n\n"
        "- one\n- two\n\n1. first\n2. second\n\n> quoted\n\n```py\nx = 1\n```\n"
    )
    d = markdown_to_adf(md)
    validate_adf(d)
    text = render_adf(d).text
    for needle in (
        "# Title",
        "**bold**",
        "*italic*",
        "`code`",
        "a link (https://example.test/x)",
        "one",
        "two",
        "first",
        "second",
        "> quoted",
        "```py",
        "x = 1",
    ):
        assert needle in text, needle
    # the rendered text converts back to a valid document with the same words
    again = markdown_to_adf(text)
    validate_adf(again)
    words = lambda x: sorted(  # noqa: E731
        w for n in _texts(x) for w in n.replace("*", " ").replace("`", " ").split() if w.isalpha()
    )
    assert words(again) == words(d)


def _texts(d: Any) -> list[str]:
    out: list[str] = []
    stack = [d]
    while stack:
        n = stack.pop()
        if n.get("type") == "text":
            out.append(n["text"])
        stack.extend(n.get("content", ()))
    return out


# --- round 1 review findings ----------------------------------------------------------------


def test_a_str_subclass_with_a_lying_eq_is_not_a_doc_type() -> None:
    class Liar(str):
        __slots__ = ()

        def __eq__(self, other: object) -> bool:
            return True

        def __ne__(self, other: object) -> bool:
            return False

        __hash__ = str.__hash__

    d = {"type": Liar("mention"), "version": 1, "content": []}
    assert where(refused(d)) == "type"


def test_a_reserve_larger_than_the_budget_is_body_too_large_even_without_text() -> None:
    refused(doc(), "body_too_large", body_chars=10, reserve=11)
    validate_adf(doc(), body_chars=10, reserve=10)


def test_a_negative_body_chars_is_body_too_large_even_without_text() -> None:
    refused(doc(), "body_too_large", body_chars=-1)
    refused(doc(para()), "body_too_large", body_chars=-5, reserve=0)


def test_a_negative_reserve_or_reserve_nodes_is_a_value_error() -> None:
    with pytest.raises(ValueError):
        validate_adf(doc(), reserve=-1)
    with pytest.raises(ValueError):
        validate_adf(doc(), reserve_nodes=-1)


@pytest.mark.parametrize("href", ["http://a.test/\n", "https://a.test\n", "https://a.test/x\n"])
def test_a_trailing_newline_in_an_href_is_refused_by_code_and_schema(href: str) -> None:
    d = doc(para(txt("a", link(href))))
    refused(d)
    assert not schema_valid(d)


@pytest.mark.parametrize("language", ["py\n", "a\n"])
def test_a_trailing_newline_in_a_language_is_refused_by_code_and_schema(language: str) -> None:
    d = doc({"type": "codeBlock", "attrs": {"language": language}})
    refused(d)
    assert not schema_valid(d)


@pytest.mark.parametrize(
    "href",
    [
        "http://evil.example\\.trusted.example/x",
        "https://a.test/a\\b",
        "https://a.test\\",
        "http://\\a.test/",
    ],
)
def test_a_backslash_in_an_href_is_refused_by_code_and_schema(href: str) -> None:
    d = doc(para(txt("a", link(href))))
    refused(d)
    assert not schema_valid(d)


def test_the_converter_never_emits_an_href_with_a_backslash() -> None:
    for md in (
        "[t](http://evil.example\\\\.trusted.example/x)",
        "[t](<http://evil.example\\.trusted.example/x>)",
        "[t](http://a.test/p\\\\q)",
    ):
        d = markdown_to_adf(md)
        validate_adf(d)
        for p in d["content"]:
            for n in p.get("content", []):
                for m in n.get("marks", []):
                    if m["type"] == "link":
                        assert "\\" not in m["attrs"]["href"]


def test_the_converter_shares_the_validator_patterns() -> None:
    from ticketctl import adf_from_markdown

    assert adf_from_markdown._HREF is adf_validate.HREF
    assert adf_from_markdown._LANGUAGE is adf_validate.LANGUAGE


def test_the_first_violation_in_encounter_order_wins_over_the_node_limit() -> None:
    mention = {"type": "mention", "attrs": {"id": "x"}}
    d = doc(para(mention, *[txt("a")] * 6000))
    assert where(refused(d)) == "content[0].content[0]"
    wide = doc(*[{"type": "paragraph"}] * 6000)
    refused(wide, "body_too_large")
    bad_then_many = doc({"type": "mention"}, *[{"type": "paragraph"}] * 6000)
    assert where(refused(bad_then_many)) == "content[0]"


def _key_path(key: str) -> str:
    d = doc(para(txt("a", {"type": "link", "attrs": {"href": "https://a.test/", key: 1}})))
    return where(refused(d))


def test_a_capped_path_key_never_cuts_a_marker_in_half() -> None:
    for key in ("\x00" * 9, "\x00" * 100_000, "k" * 63 + "\x00", "k" * 100_000, "\x00" * 8 + "ab"):
        path = _key_path(key)
        shown = path.rsplit(".", 1)[-1]
        assert len(shown) <= 64
        assert re.fullmatch(r"(?:<U\+[0-9A-F]{4,6}>|[^<])*", shown) is not None, shown


@pytest.mark.parametrize("extra", [{"content": [{"type": "mention"}]}, {"content": []}])
def test_a_hard_break_refuses_content(extra: Node) -> None:
    d = doc(para({"type": "hardBreak", **extra}))
    assert where(refused(d)) == "content[0].content[0].content"


def test_a_text_node_refuses_content() -> None:
    d = doc(para({"type": "text", "text": "a", "content": [txt("b")]}))
    assert where(refused(d)) == "content[0].content[0].content"


def test_every_node_type_whose_key_table_lacks_content_refuses_content() -> None:
    checked = 0
    for t, (required, optional) in adf_validate._KEYS.items():
        if "content" in required | optional:
            continue
        checked += 1
        node: Node = {"type": t, "content": []}
        if t == "text":
            node["text"] = "a"
            d = doc(para(node))
        else:
            d = doc(para(node))
        assert where(refused(d)).endswith(".content"), t
    assert checked == 2  # text and hardBreak


_BLOCK_NODES: list[Node] = [
    {"type": "paragraph"},
    {"type": "heading", "attrs": {"level": 1}},
    bullets(item(para())),
    {"type": "orderedList", "content": [item(para())]},
    item(para()),
    quote(para()),
    {"type": "codeBlock"},
]


@pytest.mark.parametrize("block", _BLOCK_NODES, ids=lambda n: n["type"])
@pytest.mark.parametrize("parent", ["paragraph", "heading"])
def test_a_block_node_inside_inline_content_is_refused(block: Node, parent: str) -> None:
    holder: Node = {"type": parent, "content": [block]}
    if parent == "heading":
        holder["attrs"] = {"level": 2}
    assert where(refused(doc(holder))) == "content[0].content[0]"


def test_a_list_item_directly_inside_a_blockquote_is_refused() -> None:
    assert field_of(doc(quote(item(para())))) == "content[0].content[0]"


def test_a_paragraph_directly_inside_a_bullet_list_is_refused() -> None:
    assert field_of(doc(bullets(para()))) == "content[0].content[0]"


def test_json_nesting_of_exactly_the_limit_passes_the_guard_and_one_more_does_not() -> None:
    n = adf_validate.MAX_JSON_NESTING
    exc = parse_refused("[" * n + "]" * n, "node_not_allowed")
    assert where(exc) == "$"
    parse_refused("[" * (n + 1) + "]" * (n + 1), "invalid_value")


def test_a_dict_subclass_child_node_is_refused() -> None:
    class D(dict):  # type: ignore[type-arg]
        pass

    assert field_of(doc(D(type="paragraph"))) == "content[0]"
    assert field_of(doc(para(D(type="text", text="a")))) == "content[0].content[0]"


def test_tuple_marks_and_tuple_content_are_refused() -> None:
    assert field_of(doc(para({"type": "text", "text": "a", "marks": (mark("strong"),)}))) == (
        "content[0].content[0].marks"
    )
    assert field_of(doc({"type": "paragraph", "content": (txt("a"),)})) == "content[0].content"


def test_the_invalid_input_error_has_no_details_when_none_are_given() -> None:
    assert refused(doc(*[{"type": "paragraph"}] * 6000), "body_too_large").details is None
