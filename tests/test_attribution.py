"""The attribution paragraph, the comment property and the actor rules (issue #22)."""

from __future__ import annotations

import hashlib
import json

import pytest

from ticketctl.adf_from_markdown import text_chars
from ticketctl.adf_validate import canonical_json, validate_adf
from ticketctl.attribution import (
    META_PROPERTY,
    RichText,
    actor_kind,
    attribution_text,
    comment_meta,
    default_actor,
    prepare_rich_text,
)
from ticketctl.config import Attribution
from ticketctl.sanitize import InputError, sanitize_body

TEMPLATE = Attribution().template
AUTO = "auto:unknown"
HUMAN = "human:tester"


def prep(body: str | None, *, actor: str = AUTO, kind: str = "comment", **kw: object) -> RichText:
    args: dict[str, object] = {"actor": actor, "kind": kind, "template": TEMPLATE, "enabled": True}
    args.update(kw)
    return prepare_rich_text(body, **args)  # type: ignore[arg-type]


def D(out: RichText) -> dict:
    assert out.doc is not None
    return out.doc


def first_paragraph_text(doc: dict) -> str:
    para = doc["content"][0]
    assert para["type"] == "paragraph"
    assert len(para["content"]) == 1 and "marks" not in para["content"][0]
    return para["content"][0]["text"]


def refused(exc: pytest.ExceptionInfo[InputError], exit_code: int, reason: str) -> None:
    assert exc.value.exit_code == exit_code
    assert exc.value.reason == reason


# --- the paragraph ------------------------------------------------------------------------


def test_auto_comment_starts_with_the_attribution_paragraph() -> None:
    out = prep("hello **world**")
    assert first_paragraph_text(D(out)) == TEMPLATE
    assert D(out)["content"][1]["content"][0]["text"] == "hello "
    assert out.attributed is True


def test_attribution_cannot_be_turned_off_for_auto() -> None:
    out = prep("hi", enabled=False)
    assert first_paragraph_text(D(out)) == TEMPLATE


def test_human_gets_it_only_when_enabled() -> None:
    assert first_paragraph_text(D(prep("hi", actor=HUMAN))) == TEMPLATE
    off = prep("hi", actor=HUMAN, enabled=False)
    assert off.attributed is False
    assert D(off)["content"][0]["content"][0]["text"] == "hi"


def test_empty_auto_comment_is_empty_body_before_the_attribution() -> None:
    for body in ("", "   \n\t", None):
        with pytest.raises(InputError) as exc:
            prep(body)
        refused(exc, 22, "empty_body")


def test_empty_human_comment_is_empty_body() -> None:
    with pytest.raises(InputError) as exc:
        prep("", actor=HUMAN)
    refused(exc, 22, "empty_body")


def test_auto_create_without_body_is_the_attribution_alone() -> None:
    out = prep(None, kind="description")
    assert out.doc is not None
    assert [n["type"] for n in D(out)["content"]] == ["paragraph"]
    assert first_paragraph_text(D(out)) == TEMPLATE


def test_human_create_without_body_omits_the_description() -> None:
    out = prep(None, kind="description", actor=HUMAN)
    assert out.doc is None and out.sha256 is None and out.attributed is False
    assert prep(None, kind="description", actor=HUMAN, enabled=False).doc is None


def test_human_create_with_attribution_enabled_and_no_body_still_omits_it() -> None:
    # "a human: create with no body omits the description" holds whatever attribution.enabled is.
    assert prep(None, kind="description", actor=HUMAN, enabled=True).doc is None


def test_description_with_body_has_attribution_first() -> None:
    out = prep("# Title\n\ntext", kind="description")
    assert first_paragraph_text(D(out)) == TEMPLATE
    assert D(out)["content"][1]["type"] == "heading"


def test_hash_is_of_the_final_adf_including_the_paragraph() -> None:
    out = prep("body")
    expected = hashlib.sha256(canonical_json(D(out)).encode()).hexdigest()
    assert out.sha256 == expected
    other = prep("body", template="Another line.")
    assert other.sha256 != out.sha256


def test_final_document_validates_without_a_reserve() -> None:
    out = prep("- a\n- b\n\n> q\n\n```py\ncode\n```")
    validate_adf(D(out))


def test_input_markdown_cannot_replace_or_precede_the_paragraph() -> None:
    out = prep(TEMPLATE)
    assert [first_paragraph_text(D(out)), D(out)["content"][1]["content"][0]["text"]] == [
        TEMPLATE,
        TEMPLATE,
    ]
    assert len(D(out)["content"]) == 2


# --- hostile templates -------------------------------------------------------------------


@pytest.mark.parametrize(
    "template",
    [
        "AI ‮ drafted ⁦x",  # bidi
        "zero​width \x1b[31m red",  # Cf and escape (config forbids ASCII control already)
        "tab\there\r\nnewline",
        "[link](https://example.test/) and ![img](https://example.test/a.png)",
        "[~accountid:abc123] @everyone {code}x{code} h2. t",
        '{"type":"mention","attrs":{"id":"1"}}',
        "<script>alert(1)</script> javascript:alert(1)",
        "**bold** _it_ `code`",
        "x" * 300,
        "\U0001f600" * 300,
    ],
)
def test_hostile_template_stays_one_plain_allowed_text_node(template: str) -> None:
    out = prep("body", template=template)
    para = D(out)["content"][0]
    assert para["type"] == "paragraph" and set(para) == {"type", "content"}
    (node,) = para["content"]
    assert set(node) == {"type", "text"}
    text = node["text"]
    assert text == sanitize_body(text)
    assert "\n" not in text and "\r" not in text and "\t" not in text
    validate_adf(D(out))
    assert D(out)["content"][1]["content"][0]["text"] == "body"


def test_template_markup_is_literal_not_converted() -> None:
    text = attribution_text("[a](https://example.test/) **b**")
    assert text == "[a](https://example.test/) **b**"


def test_invisible_template_is_refused() -> None:
    for template in ("   ", "\t \t"):
        with pytest.raises(InputError) as exc:
            attribution_text(template)
        refused(exc, 2, "config_invalid")


@pytest.mark.parametrize("template", ["\u200b", "\x00\x01", "\u200b \x00 \u202e", " \u2060 "])
def test_template_of_only_markers_is_refused(template: str) -> None:
    with pytest.raises(InputError) as exc:
        attribution_text(template)
    refused(exc, 2, "config_invalid")


def test_template_with_text_and_markers_is_valid() -> None:
    assert attribution_text("hi\u200b") == "hi<U+200B>"


def test_template_is_stripped() -> None:
    assert attribution_text("  hi  ") == "hi"


# --- limits -------------------------------------------------------------------------------


def test_attribution_counts_against_body_chars() -> None:
    room = 1000 - len(TEMPLATE)
    ok = prep("a" * room, body_chars=1000)
    assert text_chars(D(ok)) == 1000
    with pytest.raises(InputError) as exc:
        prep("a" * (room + 1), body_chars=1000)
    refused(exc, 22, "body_too_large")


def test_attribution_counts_against_the_node_limit() -> None:
    # n paragraphs of one text node are 2n nodes plus the doc; the attribution adds 2 more.
    body = "\n\n".join("p" for _ in range(2499))
    with pytest.raises(InputError) as exc:
        prep(body)
    refused(exc, 22, "body_too_large")
    ok = prep("\n\n".join("p" for _ in range(2498)))
    validate_adf(D(ok))


def test_marker_expanded_template_counts_by_its_real_length() -> None:
    template = "a" + "‮" * 100  # one letter and 100 markers of 8 chars
    out = prep("a" * 10, template=template, body_chars=1000)
    assert text_chars(D(out)) == 811
    with pytest.raises(InputError) as exc:
        prep("a" * 200, template=template, body_chars=1000)
    refused(exc, 22, "body_too_large")


@pytest.mark.parametrize("fmt", ["markdown", "adf"])
def test_body_chars_is_forwarded_above_the_default(fmt: str) -> None:
    n = 30500
    body = "a" * n if fmt == "markdown" else adf("a" * n)
    out = prep(body, fmt=fmt, body_chars=32767)
    assert text_chars(D(out)) == n + len(TEMPLATE)
    with pytest.raises(InputError) as exc:
        prep("a" * n if fmt == "markdown" else adf("a" * n), fmt=fmt)
    refused(exc, 22, "body_too_large")


@pytest.mark.parametrize("fmt", ["markdown", "adf"])
def test_body_over_the_raised_limit_is_too_large(fmt: str) -> None:
    n = 32767 - len(TEMPLATE) + 1
    body = "a" * n if fmt == "markdown" else adf("a" * n)
    with pytest.raises(InputError) as exc:
        prep(body, fmt=fmt, body_chars=32767)
    refused(exc, 22, "body_too_large")


def test_reserve_is_forwarded_to_both_converters(monkeypatch: pytest.MonkeyPatch) -> None:
    import ticketctl.attribution as mod

    seen: list[tuple[str, int, int]] = []
    real_md, real_adf = mod.markdown_to_adf, mod.parse_adf_json

    def spy_md(text: str, **kw: int) -> dict:
        seen.append(("md", kw["reserve"], kw["reserve_nodes"]))
        return real_md(text, **kw)

    def spy_adf(text: str, **kw: int) -> dict:
        seen.append(("adf", kw["reserve"], kw["reserve_nodes"]))
        return real_adf(text, **kw)

    monkeypatch.setattr(mod, "markdown_to_adf", spy_md)
    monkeypatch.setattr(mod, "parse_adf_json", spy_adf)
    prep("x")
    prep(adf(), fmt="adf")
    assert seen == [("md", len(TEMPLATE), 2), ("adf", len(TEMPLATE), 2)]


def test_no_reserve_without_attribution(monkeypatch: pytest.MonkeyPatch) -> None:
    import ticketctl.attribution as mod

    seen: list[tuple[int, int]] = []
    real = mod.markdown_to_adf

    def spy(text: str, **kw: int) -> dict:
        seen.append((kw["reserve"], kw["reserve_nodes"]))
        return real(text, **kw)

    monkeypatch.setattr(mod, "markdown_to_adf", spy)
    prep("x", actor=HUMAN, enabled=False)
    assert seen == [(0, 0)]


def test_description_only_attribution_over_a_tiny_limit_is_too_large() -> None:
    with pytest.raises(InputError) as exc:
        prep(None, kind="description", body_chars=10)
    refused(exc, 22, "body_too_large")


# --- --format adf --------------------------------------------------------------------------


def adf(text: str = "prepared") -> str:
    para = {"type": "paragraph", "content": [{"type": "text", "text": text}]}
    return json.dumps({"type": "doc", "version": 1, "content": [para]})


def test_adf_input_gets_the_paragraph_first() -> None:
    out = prep(adf(), fmt="adf")
    assert first_paragraph_text(D(out)) == TEMPLATE
    assert D(out)["content"][1]["content"][0]["text"] == "prepared"
    validate_adf(D(out))


def test_adf_input_empty_is_empty_body_before_the_attribution() -> None:
    with pytest.raises(InputError) as exc:
        prep(adf("  "), fmt="adf")
    refused(exc, 22, "empty_body")


def test_adf_input_is_validated_before_the_paragraph_is_added() -> None:
    bad = json.dumps(
        {
            "type": "doc",
            "version": 1,
            "content": [{"type": "paragraph", "content": [{"type": "mention", "attrs": {}}]}],
        }
    )
    with pytest.raises(InputError) as exc:
        prep(bad, fmt="adf")
    refused(exc, 22, "node_not_allowed")


def test_adf_input_leaves_room_for_the_paragraph() -> None:
    room = 1000 - len(TEMPLATE)
    prep(adf("a" * room), fmt="adf", body_chars=1000)
    with pytest.raises(InputError) as exc:
        prep(adf("a" * (room + 1)), fmt="adf", body_chars=1000)
    refused(exc, 22, "body_too_large")


def test_adf_input_does_not_mutate_or_share_the_input() -> None:
    text = adf()
    out = prep(text, fmt="adf")
    assert json.loads(text)["content"][0]["content"][0]["text"] == "prepared"
    assert len(D(out)["content"]) == 2


def test_adf_none_body_for_a_comment_is_empty_body() -> None:
    with pytest.raises(InputError) as exc:
        prep(None, fmt="adf")
    refused(exc, 22, "empty_body")


# --- the field path ----------------------------------------------------------------------


@pytest.mark.parametrize("body", ["text", "", None])
def test_auto_rich_text_field_is_invalid_value(body: str | None) -> None:
    with pytest.raises(InputError) as exc:
        prep(body, kind="field")
    refused(exc, 22, "invalid_value")
    assert "rich text by an automatic actor carries no attribution" in str(exc.value)


def test_human_rich_text_field_has_no_attribution() -> None:
    out = prep("plain *text*", kind="field", actor=HUMAN)
    assert out.attributed is False
    assert D(out)["content"][0]["content"][0]["text"] == "plain "
    validate_adf(D(out))


def test_human_field_with_adf_has_no_attribution() -> None:
    out = prep(adf(), kind="field", actor=HUMAN, fmt="adf")
    assert len(D(out)["content"]) == 1


def test_human_empty_field_is_empty_body() -> None:
    with pytest.raises(InputError) as exc:
        prep("", kind="field", actor=HUMAN)
    refused(exc, 22, "empty_body")


# --- argument errors ---------------------------------------------------------------------


def test_actor_kind_refuses_non_strings() -> None:
    for actor in (None, 5):
        with pytest.raises(ValueError):
            actor_kind(actor)  # type: ignore[arg-type]


def test_bad_arguments_are_type_or_value_errors() -> None:
    with pytest.raises(ValueError):
        prep("x", kind="title")
    with pytest.raises(ValueError):
        prep("x", actor="root")
    with pytest.raises(ValueError):
        prep("x", actor="auto:")
    with pytest.raises(ValueError):
        prep("x", fmt="html")
    with pytest.raises(TypeError):
        prep(b"x")  # type: ignore[arg-type]


# --- the comment property --------------------------------------------------------------------


def test_meta_shape_without_a_key() -> None:
    out = prep("hello")
    meta = comment_meta(AUTO, out)
    assert META_PROPERTY == "ticketctl.meta"
    assert meta == {"v": 1, "actor_kind": "auto", "body_sha256": out.sha256}
    assert "key_sha256" not in meta


def test_meta_with_a_key_hashes_the_key_alone() -> None:
    out = prep("hello", actor=HUMAN)
    meta = comment_meta(HUMAN, out, dedupe_key="run-1:step/2")
    assert meta["actor_kind"] == "human"
    assert meta["key_sha256"] == hashlib.sha256(b"run-1:step/2").hexdigest()
    assert meta["body_sha256"] == out.sha256


def test_meta_refuses_a_key_outside_the_grammar() -> None:
    out = prep("hello")
    for key in ("", "a b", "x" * 129, "k\n", "é"):
        with pytest.raises(InputError) as exc:
            comment_meta(AUTO, out, dedupe_key=key)
        refused(exc, 2, "invalid_argument")


def test_meta_refuses_a_bad_actor() -> None:
    out = prep("hello")
    for actor in ("root", "auto:", None, 5):
        with pytest.raises(ValueError):
            comment_meta(actor, out)  # type: ignore[arg-type]


def test_meta_auto_actor_needs_an_attributed_document() -> None:
    plain = prep("hello", actor=HUMAN, enabled=False)
    assert plain.attributed is False
    with pytest.raises(ValueError):
        comment_meta(AUTO, plain)


def test_meta_needs_a_document() -> None:
    empty = prep(None, kind="description", actor=HUMAN)
    assert empty.doc is None
    for actor in (HUMAN, AUTO):
        with pytest.raises(ValueError):
            comment_meta(actor, empty)


def test_meta_human_actor_with_unattributed_text_is_fine() -> None:
    plain = prep("hello", actor=HUMAN, enabled=False)
    assert comment_meta(HUMAN, plain)["body_sha256"] == plain.sha256


def test_meta_does_not_apply_the_default_size_limit() -> None:
    out = prep("a" * 31000, actor=HUMAN, body_chars=32767)
    assert comment_meta(HUMAN, out)["body_sha256"] == out.sha256


def test_meta_serializes_to_plain_json() -> None:
    out = prep("hello")
    assert json.loads(json.dumps(comment_meta(AUTO, out, dedupe_key="k"))) == comment_meta(
        AUTO, out, dedupe_key="k"
    )


# --- default actor -------------------------------------------------------------------------


def test_explicit_actor_always_wins() -> None:
    assert default_actor("auto:bot", me_name="me", stderr_tty=True, tty_openable=True) == (
        "auto:bot",
        "flag",
    )
    assert default_actor("human:x", me_name="me", stderr_tty=False, tty_openable=False) == (
        "human:x",
        "flag",
    )


def test_tty_default_is_human_me_name() -> None:
    assert default_actor(None, me_name="jo", stderr_tty=True, tty_openable=True) == (
        "human:jo",
        "tty",
    )


@pytest.mark.parametrize("stderr_tty,tty_openable", [(False, True), (True, False), (False, False)])
def test_no_terminal_is_auto_unknown(stderr_tty: bool, tty_openable: bool) -> None:
    got = default_actor(None, me_name="jo", stderr_tty=stderr_tty, tty_openable=tty_openable)
    assert got == ("auto:unknown", "default")


@pytest.mark.parametrize("name", [None, 5, "", "a b", "x" * 65, "‮"])
def test_unusable_me_name_falls_back_to_auto(name: object) -> None:
    got = default_actor(
        None,
        me_name=name,  # type: ignore[arg-type]
        stderr_tty=True,
        tty_openable=True,
    )
    assert got == ("auto:unknown", "default")


def test_hostile_explicit_actor_is_refused() -> None:
    for actor in ("human:", "human:a b", "root", "human:‮", "auto:x\n"):
        with pytest.raises(ValueError):
            default_actor(actor, me_name=None, stderr_tty=False, tty_openable=False)
