"""ADF to Markdown renderer and its budgets (docs/interface.md, Text conversion > Reading)."""

from __future__ import annotations

import random
import time
from typing import Any

import pytest

from ticketctl import adf_render
from ticketctl.adf_render import render_adf
from ticketctl.sanitize import sanitize_body


def doc(*content: Any) -> dict[str, Any]:
    return {"type": "doc", "version": 1, "content": list(content)}


def para(*content: Any) -> dict[str, Any]:
    return {"type": "paragraph", "content": list(content)}


def txt(text: str, *marks: dict[str, Any]) -> dict[str, Any]:
    node: dict[str, Any] = {"type": "text", "text": text}
    if marks:
        node["marks"] = list(marks)
    return node


def link(href: Any) -> dict[str, Any]:
    return {"type": "link", "attrs": {"href": href}}


def mark(kind: str) -> dict[str, Any]:
    return {"type": kind}


def md(*content: Any) -> str:
    return render_adf(doc(*content)).text


# ---- blocks ---------------------------------------------------------------------------------


def test_paragraphs_and_headings() -> None:
    out = md(
        {"type": "heading", "attrs": {"level": 2}, "content": [txt("Title")]},
        para(txt("one")),
        para(txt("two")),
    )
    assert out == "## Title\n\none\n\ntwo"


@pytest.mark.parametrize("level", [0, 9, "x", None, True, -3])
def test_heading_level_is_clamped(level: Any) -> None:
    out = md({"type": "heading", "attrs": {"level": level}, "content": [txt("T")]})
    assert out.startswith("#") and out.endswith(" T") and not out.startswith("#######")


def test_hard_break_is_a_newline() -> None:
    assert md(para(txt("a"), {"type": "hardBreak"}, txt("b"))) == "a\nb"


def test_rule_and_empty_doc() -> None:
    assert md({"type": "rule"}) == "---"
    assert md() == ""


def test_lists_nest_and_number() -> None:
    items = {
        "type": "orderedList",
        "attrs": {"order": 3},
        "content": [
            {"type": "listItem", "content": [para(txt("a"))]},
            {
                "type": "listItem",
                "content": [
                    para(txt("b")),
                    {
                        "type": "bulletList",
                        "content": [{"type": "listItem", "content": [para(txt("c"))]}],
                    },
                ],
            },
        ],
    }
    assert md(items) == "3. a\n4. b\n   - c"


def test_empty_list_item_keeps_its_marker() -> None:
    out = md({"type": "bulletList", "content": [{"type": "listItem", "content": []}]})
    assert out == "-"


def test_task_list() -> None:
    tasks = {
        "type": "taskList",
        "content": [
            {"type": "taskItem", "attrs": {"state": "TODO"}, "content": [txt("open")]},
            {"type": "taskItem", "attrs": {"state": "DONE"}, "content": [txt("shut")]},
        ],
    }
    assert md(tasks) == "- [ ] open\n- [x] shut"


def test_panel_is_a_block_quote_with_title_first() -> None:
    panel = {
        "type": "panel",
        "attrs": {"panelType": "info", "title": "Heads up"},
        "content": [para(txt("body")), para(txt("more"))],
    }
    assert md(panel) == "> Heads up\n> body\n>\n> more"


def test_expand_and_blockquote() -> None:
    expand = {"type": "expand", "attrs": {"title": "More"}, "content": [para(txt("x"))]}
    quote = {"type": "blockquote", "content": [para(txt("q"))]}
    assert md(expand, quote) == "> More\n> x\n\n> q"


def test_code_block_fence_outgrows_content_and_language_is_checked() -> None:
    block = {
        "type": "codeBlock",
        "attrs": {"language": "py"},
        "content": [txt("a ``` b\nc")],
    }
    assert md(block) == "````py\na ``` b\nc\n````"
    bad = {"type": "codeBlock", "attrs": {"language": "x\ny`"}, "content": [txt("z")]}
    assert md(bad) == "```\nz\n```"


def test_table_first_row_is_header_and_pipes_are_escaped() -> None:
    def cell(t: str) -> dict[str, Any]:
        return {"type": "tableCell", "content": [para(txt(t))]}

    table = {
        "type": "table",
        "content": [
            {"type": "tableRow", "content": [cell("h1"), cell("h2")]},
            {"type": "tableRow", "content": [cell("a|b"), cell("line"), cell("extra")]},
            {"type": "tableRow", "content": [cell("short")]},
        ],
    }
    assert md(table) == (
        "| h1 | h2 |  |\n| --- | --- | --- |\n| a\\|b | line | extra |\n| short |  |  |"
    )


def test_table_of_300_rows_renders_200() -> None:
    rows = [
        {"type": "tableRow", "content": [{"type": "tableCell", "content": [para(txt(str(i)))]}]}
        for i in range(300)
    ]
    result = render_adf(doc({"type": "table", "content": rows}), part="description")
    data_rows = [
        ln for ln in result.text.splitlines() if ln.startswith("| ") and not ln.startswith("| ---")
    ]
    assert len(data_rows) == 200
    assert result.incomplete == ("description",)
    assert result.reasons == ("table_rows",)
    assert "[100 more rows not shown]" in result.text


# ---- inline ---------------------------------------------------------------------------------


def test_marks() -> None:
    out = md(
        para(
            txt("b", mark("strong")),
            txt(" "),
            txt("i", mark("em")),
            txt(" "),
            txt("s", mark("strike")),
            txt(" "),
            txt("c", mark("code")),
            txt(" "),
            txt("u", mark("underline"), {"type": "textColor", "attrs": {"color": "#f00"}}),
        )
    )
    assert out == "**b** *i* ~~s~~ `c` u"


def test_mark_whitespace_stays_outside_the_markers() -> None:
    assert md(para(txt(" x ", mark("strong")))) == " **x** "
    assert md(para(txt("  ", mark("strong")))) == ""


def test_code_mark_with_backticks() -> None:
    assert md(para(txt("a`b", mark("code")))) == "``a`b``"
    assert md(para(txt("`", mark("code")))) == "`` ` ``"


def test_inline_nodes() -> None:
    out = md(
        para(
            {"type": "emoji", "attrs": {"shortName": ":smile:", "text": "X"}},
            txt(" "),
            {"type": "emoji", "attrs": {"shortName": ":wave:"}},
            txt(" "),
            {"type": "status", "attrs": {"text": "In Review", "color": "blue"}},
            txt(" "),
            {"type": "date", "attrs": {"timestamp": "1700000000000"}},
            txt(" "),
            {"type": "mention", "attrs": {"id": "abc", "text": "@Sam"}},
        )
    )
    assert out == "X :wave: In Review 2023-11-14 @Sam"


@pytest.mark.parametrize(
    "attrs", [{}, {"timestamp": "zz"}, {"timestamp": 1e300}, {"timestamp": None}]
)
def test_bad_date_never_raises(attrs: dict[str, Any]) -> None:
    assert md(para({"type": "date", "attrs": attrs})) == "[invalid date]"


# ---- links ----------------------------------------------------------------------------------


def test_link_with_different_text_shows_the_href() -> None:
    out = md(para(txt("docs", link("https://example.test/a"))))
    assert out == "docs (https://example.test/a)"


def test_link_text_equal_to_href_is_not_repeated() -> None:
    assert md(para(txt("https://example.test/", link("https://example.test/")))) == (
        "https://example.test/"
    )
    assert md(para(txt("a@example.test", link("mailto:a@example.test")))) == "a@example.test"


def test_adjacent_nodes_with_one_link_share_it() -> None:
    out = md(para(txt("a", link("https://e.test/")), txt("b", link("https://e.test/"), mark("em"))))
    assert out == "a*b* (https://e.test/)"


@pytest.mark.parametrize(
    "href",
    [
        "javascript:alert(1)",
        "JaVaScRiPt:alert(1)",
        " javascript:alert(1)",
        "java\tscript:alert(1)",
        "data:text/html,x",
        "vbscript:x",
        "/relative",
        "",
        "file:///etc/passwd",
    ],
)
def test_unsafe_links_are_removed(href: str) -> None:
    out = md(para(txt("click", link(href))))
    assert out == "[unsafe link removed]"


@pytest.mark.parametrize("href", [None, 5, ["https://x.test"], {}])
def test_malformed_href_drops_the_link_and_keeps_the_text(href: Any) -> None:
    assert md(para(txt("click", link(href)))) == "click"


def test_href_is_percent_encoded() -> None:
    out = md(para(txt("t", link("https://e.test/a b)c\x01d‮eé"))))
    assert out == "t (https://e.test/a%20b%29c%01d%E2%80%AEeé)"


def test_cards_render_the_url_under_the_same_rule() -> None:
    assert md({"type": "blockCard", "attrs": {"url": "https://e.test/x y"}}) == (
        "https://e.test/x%20y"
    )
    assert md({"type": "inlineCard", "attrs": {"url": "javascript:x"}}) == "[unsafe link removed]"
    assert md({"type": "embedCard", "attrs": {"data": {"url": "http://e.test/"}}}) == (
        "http://e.test/"
    )
    assert md({"type": "inlineCard", "attrs": {}}) == "[unsupported: inlineCard]"


# ---- media and unknown ----------------------------------------------------------------------


@pytest.mark.parametrize(
    ("ident", "shown"),
    [("abc-123", "abc-123"), ("a b", "?"), ("x" * 65, "?"), ("a\n", "?"), (5, "?"), (None, "?")],
)
def test_media_ids(ident: Any, shown: str) -> None:
    node = {"type": "mediaSingle", "content": [{"type": "media", "attrs": {"id": ident}}]}
    assert md(node) == f"[attachment: {shown}]"


def test_unknown_nodes() -> None:
    assert md({"type": "layoutSection", "content": [txt("a"), txt("b")]}) == (
        "[unsupported: layoutSection] ab"
    )
    assert md({"type": "evil\n<x>", "content": [txt("t")]}) == "[unsupported: unknown] t"
    assert md({"type": ["list"]}) == "[unsupported: unknown]"
    assert md({"type": "x" * 33}) == "[unsupported: unknown]"
    assert md({}) == "[unsupported: unknown]"
    assert render_adf(None).text == "[unsupported: unknown]"
    assert render_adf("str").text == "[unsupported: unknown]"


@pytest.mark.parametrize("bad", [None, 3, "s", [], {"a": 1}, True])
def test_malformed_shapes_never_raise(bad: Any) -> None:
    for node in (
        {"type": "paragraph", "content": bad, "attrs": bad},
        {"type": "text", "text": bad, "marks": bad},
        {"type": "table", "content": bad},
        {"type": "table", "content": [bad, {"type": "tableRow", "content": [bad]}]},
        {"type": "bulletList", "content": [bad]},
        {"type": "codeBlock", "content": [bad]},
        {"type": "doc", "content": [bad]},
        {"type": "media", "attrs": bad},
    ):
        assert isinstance(render_adf(node).text, str)


# ---- sanitizing and escaping ----------------------------------------------------------------


def test_control_and_bidi_characters_become_markers() -> None:
    out = md(para(txt("a\x1b[31m‮b\x00")))
    assert out == "a<U+001B>[31m<U+202E>b<U+0000>"
    assert sanitize_body(out) == out


def test_attrs_are_sanitized_too() -> None:
    out = md({"type": "panel", "attrs": {"title": "t‮\x07"}, "content": []})
    assert "<U+202E>" in out and "<U+0007>" in out


def test_text_is_not_escaped() -> None:
    assert md(para(txt("*a* [b](c) # <x> `y`"))) == "*a* [b](c) # <x> `y`"


# ---- budgets --------------------------------------------------------------------------------


def nested_quotes(depth: int) -> dict[str, Any]:
    node: dict[str, Any] = para(txt("core"))
    for _ in range(depth):
        node = {"type": "blockquote", "content": [node]}
    return doc(node)


def test_document_nested_60_deep_stops_at_the_depth_budget() -> None:
    result = render_adf(nested_quotes(60), part="description")
    assert "[too deep]" in result.text
    assert "core" not in result.text
    assert result.incomplete == ("description",)
    assert result.reasons == ("too_deep",)


def test_document_within_the_depth_budget_is_complete() -> None:
    result = render_adf(nested_quotes(40))
    assert "core" in result.text and result.incomplete == ()


def test_absurd_nesting_does_not_hit_the_recursion_limit() -> None:
    result = render_adf(nested_quotes(100_000))
    assert result.reasons == ("too_deep",)
    inline: Any = txt("x")
    for _ in range(100_000):
        inline = {"type": "unknownWrapper", "content": [inline]}
    assert render_adf(doc(para(inline))).reasons == ("too_deep",)


def test_cyclic_and_shared_structures_are_bounded() -> None:
    cyc: dict[str, Any] = {"type": "blockquote", "content": []}
    cyc["content"].append(cyc)
    assert "[too deep]" in render_adf(doc(cyc)).text
    shared = para(txt("x"))
    wide = doc(*[shared] * (adf_render.NODE_LIMIT * 2))
    result = render_adf(wide)
    assert result.reasons == ("too_many_nodes",) and result.text.endswith("[too large]")


def test_node_budget_counts_text_nodes() -> None:
    result = render_adf(doc(para(*[txt("a")] * (adf_render.NODE_LIMIT + 10))))
    assert result.reasons == ("too_many_nodes",)
    assert result.text.endswith("[too large]")


def test_output_beyond_one_mib_ends_with_too_large() -> None:
    big = doc(*[para(txt("x" * 100_000)) for _ in range(20)])
    result = render_adf(big, part="comments[0].body")
    assert len(result.text) <= adf_render.OUTPUT_LIMIT
    assert result.text.endswith("\n[too large]")
    assert result.incomplete == ("comments[0].body",)
    assert "too_large" in result.reasons


def test_one_huge_text_node_is_cut_not_dropped() -> None:
    result = render_adf(doc(para(txt("y" * (3 << 20)))))
    assert len(result.text) <= adf_render.OUTPUT_LIMIT
    assert result.text.startswith("yyyy") and result.text.endswith("[too large]")


def test_cut_never_splits_a_marker() -> None:
    result = render_adf(doc(para(txt("\x01" * (adf_render.OUTPUT_LIMIT)))))
    body = result.text.removesuffix("\n[too large]")
    assert body.endswith(">") and not body.endswith("<U+")
    assert sanitize_body(body) == body


def test_prefix_amplification_is_charged() -> None:
    node: Any = {"type": "paragraph", "content": [txt("x\n" * 200_000)]}
    for _ in range(40):
        node = {"type": "blockquote", "content": [node]}
    started = time.monotonic()
    result = render_adf(doc(node))
    assert time.monotonic() - started < 5
    assert len(result.text) <= adf_render.OUTPUT_LIMIT
    assert result.text.endswith("[too large]")


def test_exactly_at_the_limit_is_not_flagged() -> None:
    room = adf_render.OUTPUT_LIMIT - len("[too large]") - 1
    result = render_adf(doc(para(txt("z" * room))))
    assert result.incomplete == () and len(result.text) == room


# ---- property test --------------------------------------------------------------------------

TYPES = [
    "doc", "paragraph", "text", "heading", "bulletList", "orderedList", "listItem", "codeBlock",
    "blockquote", "hardBreak", "table", "tableRow", "tableCell", "tableHeader", "taskList",
    "taskItem", "panel", "expand", "nestedExpand", "rule", "mediaSingle", "media", "emoji",
    "status", "date", "mention", "inlineCard", "blockCard", "embedCard", "mystery", None, 7, [],
]  # fmt: skip
MARKS = ["strong", "em", "code", "strike", "link", "underline", None, 3]
STRINGS = [
    "", "a", "x y", "javascript:1", "https://e.test/a b)", "mailto:a@e.test", "\x00‮",
    "`" * 5, "*|*\n", "\U0001f600", "9" * 40, "abc-1",
]  # fmt: skip
VALUES = [*STRINGS, 0, 1, 6, 99, -1, 1.5, 1e300, True, None, [], {}]


def random_value(rng: random.Random, depth: int) -> Any:
    roll = rng.random()
    if depth <= 0 or roll < 0.25:
        return rng.choice(VALUES)
    if roll < 0.6:
        return random_node(rng, depth - 1)
    if roll < 0.8:
        return [random_value(rng, depth - 1) for _ in range(rng.randint(0, 4))]
    return {rng.choice(["a", "text", "level"]): random_value(rng, depth - 1)}


def random_node(rng: random.Random, depth: int) -> dict[str, Any]:
    node: dict[str, Any] = {}
    if rng.random() < 0.95:
        node["type"] = rng.choice(TYPES)
    if rng.random() < 0.7:
        node["content"] = [random_value(rng, depth) for _ in range(rng.randint(0, 5))]
    if rng.random() < 0.5:
        node["text"] = rng.choice(VALUES)
    if rng.random() < 0.5:
        keys = ["level", "order", "state", "language", "title", "id", "text", "url", "timestamp"]
        node["attrs"] = {rng.choice(keys): rng.choice(VALUES) for _ in range(rng.randint(0, 3))}
    if rng.random() < 0.4:
        node["marks"] = [
            {"type": rng.choice(MARKS), "attrs": {"href": rng.choice(VALUES)}}
            for _ in range(rng.randint(0, 3))
        ]
    return node


def test_random_trees_never_raise_and_stay_sanitized() -> None:
    rng = random.Random(20260101)
    started = time.monotonic()
    for _ in range(3000):
        tree = random_node(rng, rng.randint(1, 8))
        result = render_adf(tree)
        assert "internal" not in result.reasons, tree
        assert len(result.text) <= adf_render.OUTPUT_LIMIT
        assert sanitize_body(result.text) == result.text
    assert time.monotonic() - started < 30


def test_wide_document_is_fast() -> None:
    started = time.monotonic()
    render_adf(doc(*[para(txt("word " * 5, mark("strong"))) for _ in range(20_000)]))
    assert time.monotonic() - started < 5
