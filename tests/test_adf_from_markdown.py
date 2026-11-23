"""Markdown to ADF converter (docs/interface.md, Text conversion > Writing)."""

from __future__ import annotations

import json
import random
import re
import time
from pathlib import Path
from typing import Any

import pytest

from ticketctl import sanitize
from ticketctl.adf_from_markdown import markdown_to_adf, text_chars
from ticketctl.adf_validate import validate_adf

ROOT = Path(__file__).resolve().parent.parent


def check(doc: dict[str, Any]) -> int:
    """The real allowlist (``validate_adf``); returns the node count."""
    validate_adf(doc, body_chars=32767)
    count = 0
    stack = [doc]
    while stack:
        node = stack.pop()
        count += 1
        stack.extend(node.get("content", ()))
    return count


def conv(md: str, **kw: Any) -> dict[str, Any]:
    doc = markdown_to_adf(md, **kw)
    check(doc)
    return doc


def para(*content: dict[str, Any]) -> dict[str, Any]:
    return {"type": "paragraph", "content": list(content)}


def t(text: str, *marks: str, href: str | None = None) -> dict[str, Any]:
    node: dict[str, Any] = {"type": "text", "text": text}
    ms: list[dict[str, Any]] = [{"type": m} for m in marks]
    if href is not None:
        ms.append({"type": "link", "attrs": {"href": href}})
    if ms:
        node["marks"] = ms
    return node


def body(*blocks: dict[str, Any]) -> dict[str, Any]:
    return {"type": "doc", "version": 1, "content": list(blocks)}


def all_text(doc: dict[str, Any]) -> str:
    out: list[str] = []

    def walk(n: Any) -> None:
        if isinstance(n, dict):
            if n.get("type") == "text":
                out.append(n["text"])
            for c in n.get("content", []):
                walk(c)

    walk(doc)
    return "".join(out)


def container_depth(doc: dict[str, Any]) -> int:
    """The depth measure of docs/interface.md: list, listItem and blockquote nodes only."""

    def walk(n: dict[str, Any]) -> int:
        here = 1 if n["type"] in ("bulletList", "orderedList", "listItem", "blockquote") else 0
        return here + max((walk(c) for c in n.get("content", [])), default=0)

    return walk(doc)


def link_hrefs(doc: dict[str, Any]) -> list[str]:
    out: list[str] = []

    def walk(n: Any) -> None:
        for m in n.get("marks", []):
            if m["type"] == "link":
                out.append(m["attrs"]["href"])
        for c in n.get("content", []):
            walk(c)

    walk(doc)
    return out


def types(doc: dict[str, Any]) -> set[str]:
    seen: set[str] = set()

    def walk(n: Any) -> None:
        if isinstance(n, dict):
            seen.add(n["type"])
            for c in n.get("content", []):
                walk(c)
            for m in n.get("marks", []):
                seen.add("mark:" + m["type"])

    walk(doc)
    return seen


# --- the dialect ----------------------------------------------------------------------------


def test_paragraphs_and_soft_breaks() -> None:
    assert conv("one\ntwo\n\nthree") == body(para(t("one two")), para(t("three")))


def test_hard_breaks_backslash_and_two_spaces() -> None:
    assert conv("a\\\nb") == body(para(t("a"), {"type": "hardBreak"}, t("b")))
    assert conv("a  \nb") == body(para(t("a"), {"type": "hardBreak"}, t("b")))
    assert conv("a \nb") == body(para(t("a b")))
    assert conv("a\\") == body(para(t("a\\")))
    assert conv("a  ") == body(para(t("a")))


def test_atx_headings() -> None:
    doc = conv("# One\n###### Six ##\n####### seven\n#hashtag")
    assert doc["content"][0] == {
        "type": "heading",
        "attrs": {"level": 1},
        "content": [t("One")],
    }
    assert doc["content"][1]["attrs"] == {"level": 6}
    assert doc["content"][1]["content"] == [t("Six")]
    assert doc["content"][2] == para(t("####### seven #hashtag"))
    assert conv("a\n#") == body(para(t("a")), {"type": "heading", "attrs": {"level": 1}})


def test_setext_underline_is_literal() -> None:
    assert conv("Title\n===") == body(para(t("Title ===")))


def test_inline_marks() -> None:
    doc = conv("a **b** _c_ `d` *e* __f__ ***g***")
    assert doc == body(
        para(
            t("a "),
            t("b", "strong"),
            t(" "),
            t("c", "em"),
            t(" "),
            t("d", "code"),
            t(" "),
            t("e", "em"),
            t(" "),
            t("f", "strong"),
            t(" "),
            t("g", "strong", "em"),
        )
    )


def test_nested_marks_and_intraword_underscore() -> None:
    assert conv("**a *b* c**") == body(
        para(t("a ", "strong"), t("b", "strong", "em"), t(" c", "strong"))
    )
    assert conv("snake_case_name") == body(para(t("snake_case_name")))


def test_unmatched_delimiters_stay_literal() -> None:
    assert conv("**a") == body(para(t("**a")))
    assert conv("a * b * c") == body(para(t("a * b * c")))
    assert conv("`a") == body(para(t("`a")))


def test_code_span_rules() -> None:
    assert conv("`` a ` b ``") == body(para(t("a ` b", "code")))
    assert conv("`*not em*`") == body(para(t("*not em*", "code")))
    assert conv("**`x`**") == body(para(t("x", "code")))


def test_backslash_escapes() -> None:
    assert conv(r"\*a\* \# \[x\](http://a.example)") == body(para(t("*a* # [x](http://a.example)")))


def test_links() -> None:
    doc = conv("see [the **doc**](https://a.example/p?q=1#f) now")
    assert doc == body(
        para(
            t("see "),
            t("the ", href="https://a.example/p?q=1#f"),
            t("doc", "strong", href="https://a.example/p?q=1#f"),
            t(" now"),
        )
    )
    assert conv("[a](HTTP://a.example/)") == body(para(t("a", href="http://a.example/")))
    assert conv("[a](<https://a.example/x y>)") == body(
        para(t("a", href="https://a.example/x%20y"))
    )
    assert conv("[a](https://a.example/(b))") == body(para(t("a", href="https://a.example/(b)")))


def test_link_text_never_nests_links() -> None:
    doc = conv("[a [b](https://b.example) c](https://c.example)")
    assert doc == body(
        para(t("[a "), t("b", href="https://b.example"), t(" c](https://c.example)"))
    )


@pytest.mark.parametrize(
    "target",
    [
        "javascript:alert(1)",
        "JaVaScRiPt:alert(1)",
        "data:text/html;base64,AAAA",
        "vbscript:x",
        "file:///etc/passwd",
        "mailto:a@b.example",
        "ftp://a.example/",
        "//a.example/",
        "/relative",
        "https:a.example",
        "http://",
        "https://user:pw@a.example/",
        "https://user@a.example/",
        "https://a.example/ x",
        "https://a.example/<U+202E>",
        "https://" + "a" * 2001,
        "https://[::1",
        "http://good.example＠evil.example/",
        "http://evil.com\\@good.com",
        "http://evil.com\\\\good.com/",
        "http://good.example/\\<U+202E>",
        "<http://a.example/x\ty>",
        "<http://a.example/x\u00a0y>",
        "http://:80/",
        "http://é:abc/",
        "http://é:²",
        "http://x.co:99999999999",
        "http://x.co:",
        "http://[:]",
        "http://[::1]x",
        "http://xn--/",
        "http://a%41.example/",
        "http://faß.de/",
        "http://u@" + "ü" * 70 + ".example/",
        "http://a.example:8a/",
    ],
)
def test_unsafe_or_unsupported_link_targets_stay_literal(target: str) -> None:
    md = f"[x]({target})"
    doc = conv(md)
    assert "mark:link" not in types(doc)
    assert "[x](" in all_text(doc)


def test_href_length_boundary() -> None:
    ok = "https://" + "a" * 2000
    assert "mark:link" in types(conv(f"[x]({ok})"))
    assert "mark:link" not in types(conv(f"[x]({ok}a)"))


def test_non_ascii_path_is_percent_encoded_and_host_uses_idna() -> None:
    doc = conv("[x](https://bücher.example/café?q=é#é)")
    href = doc["content"][0]["content"][0]["marks"][0]["attrs"]["href"]
    assert href == "https://xn--bcher-kva.example/caf%C3%A9?q=%C3%A9#%C3%A9"


def test_unencodable_host_is_invalid_value() -> None:
    with pytest.raises(sanitize.InputError) as err:
        markdown_to_adf("[x](https://" + "ü" * 70 + ".example/)")
    assert (err.value.exit_code, err.value.code, err.value.reason) == (
        22,
        "invalid_request",
        "invalid_value",
    )


def test_quote_characters_in_href_are_encoded() -> None:
    doc = conv('[x](https://a.example/"q"`{a}|^)')
    href = doc["content"][0]["content"][0]["marks"][0]["attrs"]["href"]
    assert not set(href) & set('"<>`{}|^ ')


def test_images_are_literal() -> None:
    doc = conv("![alt](https://a.example/i.png)")
    assert "mark:link" not in types(doc)
    assert all_text(doc) == "![alt](https://a.example/i.png)"


def test_fenced_code() -> None:
    doc = conv("```python\nx = 1\n\n  y\n```\nafter")
    assert doc["content"][0] == {
        "type": "codeBlock",
        "attrs": {"language": "python"},
        "content": [t("x = 1\n\n  y")],
    }
    assert doc["content"][1] == para(t("after"))


def test_fence_variants() -> None:
    assert conv("```\nx\n```")["content"][0] == {"type": "codeBlock", "content": [t("x")]}
    assert conv("~~~c++ extra words\nx\n~~~")["content"][0]["attrs"] == {"language": "c++"}
    assert conv("x\n\n```\n```")["content"][1] == {"type": "codeBlock"}
    # a bad language word is dropped, the block stays
    doc = conv("```" + "a" * 33 + "\nx\n```")
    assert doc["content"][0] == {"type": "codeBlock", "content": [t("x")]}
    # a longer fence needs a closing fence at least as long
    doc = conv("````\n```\nx\n````")
    assert doc["content"][0]["content"] == [t("```\nx")]
    # a backtick in a backtick fence's info string makes it not a fence
    assert conv("```a`b\nx")["content"][0] == para(t("```a`b x"))


def test_unterminated_fence_runs_to_the_end() -> None:
    doc = conv("```\na\nb")
    assert doc == body({"type": "codeBlock", "content": [t("a\nb")]})


def test_markdown_inside_code_is_literal() -> None:
    doc = conv("```\n# h\n**b** [a](https://a.example)\n```")
    assert types(doc) == {"doc", "codeBlock", "text"}


def test_lists() -> None:
    doc = conv("- a\n- b\n\n  more\n- c")
    lst = doc["content"][0]
    assert lst["type"] == "bulletList" and len(lst["content"]) == 3
    assert lst["content"][1]["content"] == [para(t("b")), para(t("more"))]
    doc = conv("3. a\n4) b\n5. c")
    assert [b["type"] for b in doc["content"]] == ["orderedList"] * 3
    assert doc["content"][0]["attrs"] == {"order": 3}
    assert "attrs" not in conv("1. a")["content"][0]  # order omitted when it is 1


def test_ordered_start_bounds() -> None:
    assert conv("9999. a")["content"][0]["type"] == "orderedList"
    for md in ("10000. a", "0. a", "1234567890. a"):
        assert conv(md)["content"][0]["type"] == "paragraph", md


def test_list_nesting() -> None:
    doc = conv("- a\n  - b\n    1. c\n- d")
    outer = doc["content"][0]
    assert [i["content"][0] for i in outer["content"]] == [para(t("a")), para(t("d"))]
    inner = outer["content"][0]["content"][1]
    assert inner["type"] == "bulletList"
    assert inner["content"][0]["content"][1]["type"] == "orderedList"


def test_list_nesting_limit_is_five() -> None:
    md = "\n".join("  " * i + f"- l{i}" for i in range(8))
    doc = conv(md)
    depth = 0
    node = doc["content"][0]
    while node["type"] == "bulletList":
        depth += 1
        sub = [b for b in node["content"][0]["content"] if b["type"] == "bulletList"]
        if not sub:
            break
        node = sub[0]
    assert depth == 5
    assert "- l5" in all_text(doc)  # the sixth level is literal text


def test_empty_list_item_and_changed_marker() -> None:
    doc = conv("-\n- a")
    assert doc["content"][0]["content"][0]["content"] == [{"type": "paragraph"}]
    assert [b["type"] for b in conv("- a\n* b\n+ c")["content"]] == ["bulletList"] * 3


def test_list_markers_need_a_space_and_hr_is_literal() -> None:
    assert conv("-a") == body(para(t("-a")))
    assert conv("---") == body(para(t("---")))
    assert conv("* * *") == body(para(t("* * *")))
    assert conv("___") == body(para(t("___")))


def test_list_interrupting_a_paragraph() -> None:
    assert [b["type"] for b in conv("text\n- a")["content"]] == ["paragraph", "bulletList"]
    assert [b["type"] for b in conv("text\n1. a")["content"]] == ["paragraph", "orderedList"]
    assert [b["type"] for b in conv("text\n2. a")["content"]] == ["paragraph"]


def test_blockquotes() -> None:
    doc = conv("> a\n> b\n>\n> - c\n\nafter")
    q = doc["content"][0]
    assert q["type"] == "blockquote"
    assert [b["type"] for b in q["content"]] == ["paragraph", "bulletList"]
    assert doc["content"][1] == para(t("after"))
    assert conv(">x")["content"][0]["content"] == [para(t("x"))]
    assert conv("x\n\n>")["content"][1]["content"] == [{"type": "paragraph"}]


def test_blockquote_nesting_is_bounded() -> None:
    doc = conv(">" * 40 + " deep")
    assert "deep" in all_text(doc)
    assert ">" in all_text(doc)  # past the limit the markers are literal


def test_headings_inside_containers() -> None:
    doc = conv("- # h\n> ## q")
    assert doc["content"][0]["content"][0]["content"][0]["type"] == "heading"
    assert doc["content"][1]["content"][0]["attrs"] == {"level": 2}


# --- stays literal --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("md", "text"),
    [
        ("[~accountid:abc]", "[~accountid:abc]"),
        ("[~accountid:abc] please look", "[~accountid:abc] please look"),
        ("@someone", "@someone"),
        ("{code}x{code}", "{code}x{code}"),
        ("{code:java}x{code}", "{code:java}x{code}"),
        ("h2. Title", "h2. Title"),
        ("h1.Title", "h1.Title"),
        ("{panel}x{panel}", "{panel}x{panel}"),
        ("||a||b||\n|c|d|", "||a||b|| |c|d|"),
        ("| a | b |\n|---|---|\n| 1 | 2 |", "| a | b | |---|---| | 1 | 2 |"),
        ("<b>bold</b> <script>alert(1)</script>", "<b>bold</b> <script>alert(1)</script>"),
        ("<!-- c -->", "<!-- c -->"),
        ("&amp; &#60;", "&amp; &#60;"),
        ("<https://a.example/>", "<https://a.example/>"),
        ("https://a.example/bare", "https://a.example/bare"),
        ("~~strike~~", "~~strike~~"),
        ("!(https://a.example/i.png)", "!(https://a.example/i.png)"),
        ("[ ] todo", "[ ] todo"),
        (":smile: [x]", ":smile: [x]"),
        ("!{noformat}", "!{noformat}"),
        ("    indented\n    code", "indented code"),
    ],
)
def test_other_forms_stay_literal(md: str, text: str) -> None:
    doc = conv(md)
    assert types(doc) <= {"doc", "paragraph", "text"}  # no marks, no other nodes
    assert all_text(doc) == text


def test_task_list_and_wiki_items_become_plain_text() -> None:
    doc = conv("- [ ] todo\n- [x] done")
    assert types(doc) == {"doc", "bulletList", "listItem", "paragraph", "text"}
    assert all_text(doc) == "[ ] todo[x] done"


def test_star_pair_is_italic_not_wiki_bold() -> None:
    assert conv("*x*") == body(para(t("x", "em")))
    assert conv("a * x * b") == body(para(t("a * x * b")))


# --- sanitizing, empty and size -------------------------------------------------------------


def test_control_and_bidi_characters_are_marked_before_conversion() -> None:
    doc = conv("a‮b\x00c‍ `⁦x`\n```\n‮\n```")
    text = all_text(doc)
    for ch in ("‮", "\x00", "‍", "⁦"):
        assert ch not in text
    assert "<U+202E>" in text and "<U+0000>" in text and "<U+2066>" in text


def test_crlf_and_cr_are_newlines() -> None:
    assert conv("a\r\n\r\nb\rc") == body(para(t("a")), para(t("b c")))


def test_converter_does_not_scrub_secrets() -> None:
    sanitize.configure_scrubber(["supersecretvalue123"])
    try:
        # the converter does not scrub (that is ensure_no_secret's job): it must not crash
        assert "supersecretvalue123" in all_text(conv("supersecretvalue123"))
    finally:
        sanitize.configure_scrubber([])


@pytest.mark.parametrize("md", ["", " ", "\n\n", "\t \r\n"])
def test_whitespace_only_body_is_empty_body(md: str) -> None:
    with pytest.raises(sanitize.InputError) as err:
        markdown_to_adf(md)
    assert (err.value.exit_code, err.value.code, err.value.reason) == (
        22,
        "invalid_request",
        "empty_body",
    )


def test_body_without_text_nodes_is_empty() -> None:
    for md in ("```\n```", ">", "-", "#"):
        with pytest.raises(sanitize.InputError) as err:
            markdown_to_adf(md)
        assert err.value.reason == "empty_body", md


def test_text_chars_sums_all_text_nodes() -> None:
    doc = conv("ab **cd**\n\n- e\n\n```\nfg\n```")
    assert text_chars(doc) == len("ab ") + 2 + 1 + 2


def test_size_cap_is_inclusive_and_never_truncates() -> None:
    assert text_chars(conv("a" * 100, body_chars=100)) == 100
    with pytest.raises(sanitize.InputError) as err:
        markdown_to_adf("a" * 101, body_chars=100)
    assert (err.value.exit_code, err.value.code, err.value.reason) == (
        22,
        "invalid_request",
        "body_too_large",
    )


def test_size_counts_the_sanitized_text_and_the_reserve() -> None:
    # a control character grows to an eight character marker before it is measured
    assert text_chars(conv("\x01" * 5, body_chars=40)) == 40
    with pytest.raises(sanitize.InputError) as err:
        markdown_to_adf("\x01" * 6, body_chars=40)
    assert err.value.reason == "body_too_large"
    # the attribution paragraph (added by a later step) is reserved
    assert text_chars(conv("a" * 90, body_chars=100, reserve=10)) == 90
    with pytest.raises(sanitize.InputError) as err:
        markdown_to_adf("a" * 91, body_chars=100, reserve=10)
    assert err.value.reason == "body_too_large"


def test_size_is_measured_after_conversion_not_on_the_source() -> None:
    # 30 source characters of markup, 3 text characters
    assert text_chars(conv("**a** **b** **c**" + " " * 0, body_chars=5)) == 5


def test_huge_input_is_refused_quickly() -> None:
    start = time.monotonic()
    with pytest.raises(sanitize.InputError) as err:
        markdown_to_adf("a" * 5_000_000)
    assert err.value.reason == "body_too_large"
    assert time.monotonic() - start < 2


def test_node_count_limit() -> None:
    md = "a\\\n" * 5200 + "z"
    with pytest.raises(sanitize.InputError) as err:
        markdown_to_adf(md, body_chars=32767)
    assert err.value.reason == "body_too_large"


def test_output_is_json_and_deterministic() -> None:
    md = "# h\n\n- a **b** [c](https://c.example)\n\n```x\ny\n```"
    a = json.dumps(markdown_to_adf(md), sort_keys=True)
    assert a == json.dumps(markdown_to_adf(md), sort_keys=True)


def test_input_is_not_mutated_and_type_is_checked() -> None:
    with pytest.raises(TypeError):
        markdown_to_adf(b"x")  # type: ignore[arg-type]


# --- hostile input --------------------------------------------------------------------------

HOSTILE = [
    "- " * 5000,
    "> " * 5000 + "x",
    ">" * 100000 + "x",
    ("  " * 50 + "- x\n") * 200,
    "[" * 20000,
    "[a](" * 5000,
    "[a](https://x.example/" * 3000,
    "](" * 20000,
    "*" * 60000,
    "*a " * 20000,
    "**a *b " * 8000,
    "_a*" * 20000,
    "`" * 50001,
    "`a``b```c" * 5000,
    "![" * 20000,
    "[a]" * 20000 + "(",
    "\\" * 50001,
    "```\n" * 20000,
    "```" * 20000,
    "# " * 20000,
    "1. " * 10000,
    "\n" * 100000,
    " " * 100000 + "x",
    "\t" * 50000 + "- x",
    "a  \n" * 20000,
    "<" * 50000,
    "&" * 50000,
    "[x](" + "(" * 50000,
    "[x](<" + "a" * 50000,
    "[x](https://a.example/" + "\\" * 30000,
    "[x](https://a.example/" + "(a" * 20000 + ")" * 20000 + ")",
    "‮" * 10000,
    "- a\n" + "  " * 1 + "b\n" * 20000,
]


@pytest.mark.parametrize("md", HOSTILE, ids=range(len(HOSTILE)))
def test_hostile_inputs_finish_quickly_and_stay_in_the_allowlist(md: str) -> None:
    start = time.monotonic()
    try:
        doc = markdown_to_adf(md, body_chars=32767)
    except sanitize.InputError as exc:
        assert exc.reason in {"body_too_large", "empty_body", "invalid_value"}
    else:
        check(doc)
    assert time.monotonic() - start < 3


def test_deep_nesting_does_not_hit_the_recursion_limit() -> None:
    doc = conv("\n".join("  " * i + "- x" for i in range(200)) + "\n" + "> " * 200 + "q")
    assert "x" in all_text(doc)


# --- property tests -------------------------------------------------------------------------

ATOMS = [
    "a",
    "b c",
    " ",
    "  ",
    "\n",
    "\n\n",
    "\t",
    "*",
    "**",
    "_",
    "__",
    "`",
    "```",
    "~~~",
    "[",
    "]",
    "(",
    ")",
    "![",
    "](",
    "http://a.example/",
    "https://bücher.example/é",
    "javascript:x",
    "<",
    ">",
    "> ",
    "- ",
    "1. ",
    "# ",
    "###### ",
    "\\",
    "\\\n",
    "@",
    "[~accountid:x]",
    "{code}",
    "h2. ",
    "<b>",
    "&amp;",
    "|",
    "---",
    "‮",
    "\x00",
    "‍",
    "é",
    "\U0001f600",
    "    ",
    "  - ",
    "    - ",
]


@pytest.mark.parametrize("seed", range(30))
def test_random_markdown_never_yields_a_disallowed_node_and_never_raises(seed: int) -> None:
    rng = random.Random(seed)
    for _ in range(40):
        md = "".join(rng.choice(ATOMS) for _ in range(rng.randint(0, 120)))
        try:
            doc = markdown_to_adf(md, body_chars=32767)
        except sanitize.InputError as exc:
            assert exc.reason in {"empty_body", "invalid_value"}, md
            continue
        check(doc)
        for banned in ("mention", "inlineCard", "media", "panel", "status", "table", "emoji"):
            assert banned not in types(doc)
        assert text_chars(doc) <= 32767


# --- the wiki-markup API is never used ------------------------------------------------------


def test_no_wiki_markup_api_anywhere_in_the_code() -> None:
    # The write path is v3 ADF only; the v2 endpoints take wiki markup strings.
    for path in (ROOT / "src").rglob("*.py"):
        src = path.read_text(encoding="utf-8")
        assert "/rest/api/2" not in src, path
        assert "rest/api/latest" not in src, path
    table = (ROOT / "docs" / "interface.md").read_text(encoding="utf-8")
    endpoints = table.split("### Jira endpoints", 1)[1].split("The old `POST", 1)[0]
    assert "/rest/api/3/" in endpoints
    assert "/rest/api/2" not in endpoints
    assert "renderedBody" not in endpoints


# --- review round 1: raw code text, reserve, links, depth, limits ---------------------------


def code_text(doc: dict[str, Any]) -> str:
    out: list[str] = []

    def walk(n: Any) -> None:
        if n["type"] == "codeBlock":
            out.extend(c["text"] for c in n.get("content", []))
        for c in n.get("content", []):
            walk(c)

    walk(doc)
    return "\n".join(out)


def test_tabs_inside_fenced_code_are_kept() -> None:
    assert code_text(conv("```\n\tx\n\t\ty\n```")) == "\tx\n\t\ty"
    assert code_text(conv("> ```\n> \tx\n> ```")) == "\tx"
    assert code_text(conv("- ```\n  \tx\n  ```")) == "\tx"
    assert code_text(conv("- a\n\n  ```\n  \tx\n  ```")) == "\tx"


def test_tab_straddling_a_container_indent_becomes_spaces() -> None:
    # the tab spans columns 0 to 4; the list item takes 2 of them
    assert code_text(conv("- a\n\n  ```\n\tx\n  ```")) == "  x"


def test_leading_tab_is_four_columns_for_structure() -> None:
    doc = conv("\t- a")  # indented 4 columns: not a list marker
    assert types(doc) == {"doc", "paragraph", "text"}
    assert all_text(doc) == "- a"
    assert conv("- a\n\t- b")["content"][0]["content"][0]["content"][1]["type"] == "bulletList"


def test_reserve_nodes_counts_toward_the_node_limit() -> None:
    md = "a\\\n" * 2000 + "z"  # about 4002 nodes
    nodes = check(conv(md, body_chars=32767))
    free = 5000 - nodes
    assert markdown_to_adf(md, body_chars=32767, reserve_nodes=free)
    with pytest.raises(sanitize.InputError) as err:
        markdown_to_adf(md, body_chars=32767, reserve_nodes=free + 1)
    assert err.value.reason == "body_too_large"


@pytest.mark.parametrize("kw", [{"reserve": -1}, {"reserve_nodes": -1}])
def test_negative_reserve_is_a_value_error(kw: dict[str, int]) -> None:
    with pytest.raises(ValueError):
        markdown_to_adf("a", **kw)


@pytest.mark.parametrize(
    "md",
    [
        "[](http://a.example)",
        "[ ](http://a.example)",
        "[ ](http://a.example)",
        "[` `](http://a.example)",
    ],
)
def test_link_without_visible_text_stays_literal(md: str) -> None:
    doc = conv(md)
    assert "mark:link" not in types(doc)
    assert all_text(doc).startswith("[")
    assert re.search(r"\(http://a\.example\)$", all_text(doc))


def test_link_whose_text_is_a_sanitizer_marker_is_a_link() -> None:
    doc = conv("[​](http://a.example)")
    assert link_hrefs(doc) == ["http://a.example"]
    assert all_text(doc) == "<U+200B>"


def test_nbsp_only_line_is_not_a_blank_line() -> None:
    doc = conv("a\n \nb")
    assert doc == body(para(t("a   b")))


def test_nbsp_only_body_is_empty() -> None:
    with pytest.raises(sanitize.InputError) as err:
        markdown_to_adf(" \n ")
    assert err.value.reason == "empty_body"


def test_whitespace_only_line_inside_code_in_a_list_item_keeps_its_content() -> None:
    doc = conv("- ```\n    x\n      \n    y\n  ```")
    assert code_text(doc) == "  x\n    \n  y"
    short = conv("- ```\n  x\n \n  y\n  ```")  # shorter than the item indent: empty line
    assert code_text(short) == "x\n\ny"


def test_body_of_only_sanitizer_markers_is_text() -> None:
    doc = conv("​")
    assert all_text(doc) == "<U+200B>"


def test_container_depth_measure_for_nested_lists() -> None:
    md = "\n".join("  " * i + f"- l{i}" for i in range(8))
    assert container_depth(conv(md)) == 10  # five lists and five list items


def test_container_depth_measure_for_mixed_quotes_and_lists() -> None:
    doc = conv("> - > - > x")  # quote, list, item, quote, list, item, quote
    assert container_depth(doc) == 7
    deeper = conv("> - > - > - > x")  # the sixth container is literal
    assert container_depth(deeper) == 7
    assert "- > x" in all_text(deeper)


def test_input_cap_is_four_times_body_chars() -> None:
    cap = 50
    assert text_chars(conv("a" + "\n" * (4 * cap - 1), body_chars=cap)) == 1
    with pytest.raises(sanitize.InputError) as err:
        markdown_to_adf("a" + "\n" * (4 * cap), body_chars=cap)
    assert err.value.reason == "body_too_large"


def test_unescaped_parens_in_destinations() -> None:
    assert link_hrefs(conv(r"[x](http://a.example/p\)q)")) == ["http://a.example/p)q"]
    assert link_hrefs(conv(r"[x](http://a.example/p\(q)")) == ["http://a.example/p(q"]


def test_fence_indent_of_four_or_more_is_not_a_fence() -> None:
    assert "codeBlock" not in types(conv("    ```\nx"))
    doc = conv("```\na\n    ```\nb")  # an indented fence line does not close the block
    assert code_text(doc) == "a\n    ```\nb"


def test_host_with_special_characters_is_percent_encoded() -> None:
    assert link_hrefs(conv('[x](http://a"b.example/)')) == ["http://a%22b.example/"]


def test_host_and_port_forms_that_link() -> None:
    cases = {
        "http://é.example/": "http://xn--9ca.example/",
        "http://é.example:8080/x": "http://xn--9ca.example:8080/x",
        "http://xn--bcher-kva.example/": "http://xn--bcher-kva.example/",
        "http://[::1]:80/": "http://[::1]:80/",
        "http://x.co:65535/": "http://x.co:65535/",
    }
    for target, href in cases.items():
        assert link_hrefs(conv(f"[x]({target})")) == [href], target


def test_userinfo_with_unencodable_host_is_literal_not_an_error() -> None:
    doc = conv("[x](http://u@" + "ü" * 70 + ".example/)")
    assert "mark:link" not in types(doc)


def test_very_long_destination_stays_literal_and_fast() -> None:
    start = time.monotonic()
    doc = conv("[x](http://a.example/" + "a" * 5000 + ")")
    assert "mark:link" not in types(doc)
    md = ("[x](http://a.example/" + "a" * 3000) * 8
    assert "mark:link" not in types(conv(md))
    assert time.monotonic() - start < 2


def test_many_images_before_links_is_not_quadratic() -> None:
    md = "![" * 30000 + "[a](http://a)" * 4615
    start = time.monotonic()
    try:
        check(markdown_to_adf(md, body_chars=32767))
    except sanitize.InputError as exc:
        assert exc.reason == "body_too_large"
    assert time.monotonic() - start < 2


def test_emphasis_token_soup_is_not_superlinear(monkeypatch: pytest.MonkeyPatch) -> None:
    # The work is the number of links followed along the delimiter list (``prev`` reads), which
    # is deterministic for a seeded input; the bound is linear in the token count. A quadratic
    # scan reads it on the order of tokens squared, and stops at the cap instead of running on.
    from ticketctl import adf_from_markdown as mod

    reads = [0]
    cap = 10 * 65000  # about 108 000 reads happen today
    stored = mod._Delim.prev  # the slot descriptor of the real class

    def get_prev(self: Any) -> Any:
        reads[0] += 1
        if reads[0] > cap:
            raise AssertionError(f"more than {cap} delimiter links followed: superlinear")
        return stored.__get__(self)

    class Counting(mod._Delim):
        __slots__ = ()
        prev = property(get_prev, lambda self, value: stored.__set__(self, value))

    monkeypatch.setattr(mod, "_Delim", Counting)
    rng = random.Random(7)
    md = "".join(rng.choice(["***", "[", "_a", "](", "*"]) for _ in range(65000))
    start = time.monotonic()
    try:
        check(markdown_to_adf(md, body_chars=32767))
    except sanitize.InputError as exc:
        assert exc.reason == "body_too_large"
    assert time.monotonic() - start < 60  # only a backstop; the bound is the read count
    assert reads[0] > 0


def test_converter_patterns_match_the_validators() -> None:
    from ticketctl import adf_from_markdown as mod
    from ticketctl import adf_validate

    assert mod._HREF.pattern == adf_validate.HREF.pattern
    assert mod._LANGUAGE.pattern == adf_validate.LANGUAGE.pattern
    assert mod.MAX_NODES == adf_validate.MAX_NODES
    assert mod.MAX_NEST * 2 == adf_validate.MAX_DEPTH
    assert mod.DEFAULT_BODY_CHARS == adf_validate.DEFAULT_BODY_CHARS
