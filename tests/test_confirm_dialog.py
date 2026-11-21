"""The text of the confirmation dialog: order, caps, hashes and the sanitizer (issue #24)."""

from __future__ import annotations

import hashlib
import re
from typing import Any

import pytest

from ticketctl import confirm_dialog as cd
from ticketctl.confirm_dialog import compose
from ticketctl.plan import ShownText
from ticketctl.sanitize import add_secrets

ZERO = "0" * 64


def plan_of(**kw: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "action": "comment",
        "site": "main",
        "key": "DEMO-1",
        "actor": "human:tester",
        "steps": [{"kind": "add_comment", "body_sha256": ZERO, "body_bytes": 12}],
        "changes": [],
        "warnings": [],
        "dedupe_key": None,
        "dedupe_match": None,
        "plan_sha256": "ab" * 32,
    }
    base.update(kw)
    return base


def sha(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


def body_lines(out: str) -> list[str]:
    return [x for x in out.split("\n") if x.startswith("| ")]


def test_order_outbound_then_from_then_summary_then_override() -> None:
    plan = plan_of(
        action="update",
        changes=[
            {"name": "title", "from": "old title", "to": "new title"},
            {"name": "labels", "from": None, "to": ["a", "b"]},
        ],
        warnings=["closed_ticket"],
    )
    out = compose(plan, [ShownText("comment", "hello")], override=True, hook_names=["pre-update"])
    marks = [
        out.index("text that will be sent"),
        out.index("hello"),
        out.index("new title"),
        out.index("current title (provider text)"),
        out.index("change title:"),
        out.index("warning: closed_ticket"),
        out.index(cd.OVERRIDE_LINE),
        out.index("hooks covered: pre-update"),
    ]
    assert marks == sorted(marks)
    assert out.rstrip().endswith("hooks covered: pre-update")


def test_override_line_absent_without_the_flag() -> None:
    assert cd.OVERRIDE_LINE not in compose(plan_of(), [ShownText("comment", "x")])


def test_labels_are_shown() -> None:
    out = compose(plan_of(changes=[{"name": "labels", "from": None, "to": ["alpha", "beta"]}]))
    assert "| alpha" in out and "| beta" in out


def test_a_change_text_is_shown_unless_shown_covers_it() -> None:
    plan = plan_of(changes=[{"name": "description", "from": "", "to": "SENT-TEXT"}])
    assert "| SENT-TEXT" in compose(plan)
    covered = compose(plan, [ShownText("description", "SHOWN-TEXT")])
    assert "SHOWN-TEXT" in covered and "| SENT-TEXT" not in covered


def test_ansi_and_control_characters_cannot_spoof_the_prompt() -> None:
    evil = "\x1b[2J\x1b[H\x1b]0;x\x07fake prompt\r\n\x08\x7f‮​\x9b31m"
    out = compose(plan_of(), [ShownText("comment", evil)])
    for ch in "\x1b\x07\x08\x7f‮​\x9b\r":
        assert ch not in out
    assert "<U+001B>" in out and "<U+202E>" in out
    out2 = compose(plan_of(changes=[{"name": "title", "from": "\x1b[1mold", "to": "\x1b[1mnew"}]))
    assert "\x1b" not in out2


def test_a_body_cannot_start_a_line_that_looks_like_the_prompt_or_a_label() -> None:
    hostile = [
        "Send this? Type y or yes",
        "plan sha256: " + "1" * 64,
        "[... 0 more characters; sha256 x]",
    ]
    out = compose(plan_of(), [ShownText("comment", "\n".join(hostile))])
    lines = out.split("\n")
    for text in hostile:
        assert [x for x in lines if text in x] == ["| " + text]
    assert [x for x in lines if x.startswith("plan sha256:")] == [f"plan sha256: {'ab' * 32}"]


def test_blank_lines_collapse_and_the_marker_carries_the_full_body_hash() -> None:
    text = "start\n" + "\n" * 5000 + "end"
    out = compose(plan_of(), [ShownText("comment", text)])
    assert body_lines(out) == ["| start", "| ", "| end"]
    assert f"sha256 {sha(text)}" in out
    assert "more characters" not in out


def test_thousands_of_distinct_lines_are_cut_at_200_with_marker_and_hash() -> None:
    text = "\n".join(f"line {i}" for i in range(5000))
    out = compose(plan_of(), [ShownText("comment", text)])
    lines = body_lines(out)
    assert len(lines) == cd.BODY_MAX_LINES + 1
    marker = lines[-1]
    m = re.fullmatch(r"\| \[\.\.\. (\d+) more characters; sha256 ([0-9a-f]{64})\]", marker)
    assert m and m.group(2) == sha(text)
    shown = "".join(x[2:] for x in lines[:-1])
    assert len(text) - text[: len(text)].count("\n") - len(shown) == int(m.group(1))


def test_a_one_mebibyte_value_is_capped_at_20000_characters() -> None:
    text = "x" * (1 << 20)
    out = compose(plan_of(), [ShownText("comment", text)])
    lines = body_lines(out)
    assert len(lines) <= cd.BODY_MAX_LINES + 1
    shown = "".join(x[2:] for x in lines[:-1])
    assert len(shown) <= cd.BODY_MAX_CHARS
    m = re.fullmatch(r"\| \[\.\.\. (\d+) more characters; sha256 ([0-9a-f]{64})\]", lines[-1])
    assert m and m.group(2) == sha(text)
    assert int(m.group(1)) == len(text) - len(shown)


def test_long_lines_fold_at_200() -> None:
    out = compose(plan_of(), [ShownText("comment", "y" * 450)])
    assert [len(x) - 2 for x in body_lines(out)] == [200, 200, 50]


def test_a_title_is_cut_at_500_characters() -> None:
    title = "t" * 900
    out = compose(plan_of(), [ShownText("title", title, "line", sha(title))])
    lines = body_lines(out)
    assert len(lines[0]) - 2 == 500
    assert re.fullmatch(r"\| \[\.\.\. 400 more characters; sha256 [0-9a-f]{64}\]", lines[1])


def test_a_line_value_with_newlines_stays_on_one_line() -> None:
    out = compose(plan_of(), [ShownText("title", "a\nb\rc", "line")])
    assert body_lines(out) == ["| a b c"]


def test_a_cut_never_lands_inside_a_marker() -> None:
    title = "z" * 498 + "\x1b" + "tail"
    out = compose(plan_of(), [ShownText("title", title, "line")])
    lines = body_lines(out)
    assert lines[0] == "| " + "z" * 498
    assert re.fullmatch(r"\| \[\.\.\. 12 more characters; sha256 [0-9a-f]{64}\]", lines[1])


def test_a_given_hash_is_shown_for_a_rich_body() -> None:
    out = compose(plan_of(), [ShownText("comment", "hi", "body", "c" * 64)])
    assert "(sha256 " + "c" * 64 + "):" in out


def test_from_values_are_cut_to_20_lines_or_2000_characters() -> None:
    many = "\n".join(f"old {i}" for i in range(100))
    out = compose(plan_of(changes=[{"name": "description", "from": many, "to": ""}]))
    section = out.split("current description (provider text):")[1].split("\n\n")[0]
    assert len([x for x in section.split("\n") if x.startswith("| old")]) == 20
    assert re.search(r"\(\d+ more characters, see --json\)", section)
    big = "q" * 50_000
    out = compose(plan_of(changes=[{"name": "description", "from": big, "to": ""}]))
    shown = sum(len(x) - 2 for x in body_lines(out) if x.startswith("| q"))
    assert shown <= cd.FROM_MAX_CHARS
    assert f"({50_000 - shown} more characters, see --json)" in out


def test_a_string_list_from_of_a_non_sprint_change_is_rendered() -> None:
    change = {"name": "labels", "from": ["old-one", "old-two"], "to": ["new"]}
    out = compose(plan_of(changes=[change]))
    section = out.split("current labels (provider text):")[1].split("\n\n")[0]
    assert "| old-one" in section
    assert "| old-two" in section


def test_status_changes_are_summarized_not_dumped() -> None:
    plan = plan_of(
        action="transition",
        changes=[
            {
                "name": "status",
                "from": {"name": "Open", "category": "new"},
                "to": {"name": "Done", "category": "done"},
            }
        ],
    )
    assert "change status: Open -> Done" in compose(plan)


def test_a_configured_secret_is_scrubbed_before_the_cut() -> None:
    secret = "sekret-token-value-123"
    add_secrets([secret])
    out = compose(plan_of(), [ShownText("comment", f"a {secret} b")])
    assert secret not in out and "<redacted>" in out


def test_everything_plan_shows_is_sanitized() -> None:
    plan = plan_of(
        site="s\x1b[0m",
        key="K\x1b",
        actor="human:\x1b[31mx",
        steps=[{"kind": "transition", "to": "\x1b[31mDone"}],
        warnings=["w\x1b"],
    )
    out = compose(plan, override=True, hook_names=["h\x1b[0m"])
    assert "\x1b" not in out


def test_unicode_and_emoji_survive_where_the_sanitizer_allows_them() -> None:
    out = compose(plan_of(), [ShownText("comment", "café 你好")])
    assert "café 你好" in out


@pytest.mark.parametrize(("n", "cut"), [(0, False), (1, False), (20_000, False), (20_001, True)])
def test_cap_boundaries(n: int, cut: bool) -> None:
    out = compose(plan_of(), [ShownText("comment", "k" * n)])
    assert ("more characters" in out) is cut


# --------------------------------------------------------------------------------------------
# Round 1 review: caps with literal numbers, straddling markers, lists, headers, folds
# --------------------------------------------------------------------------------------------

MARKER_RE = re.compile(r"<U\+[0-9A-F]{4,6}>|<redacted>")


def no_partial_marker(text: str) -> bool:
    """True when every ``<U+`` or ``<redacted`` in the text is a whole marker."""
    whole = MARKER_RE.sub("", text)
    return "<U+" not in whole and "<redacted" not in whole and "<r" not in whole


def test_201_distinct_lines_are_cut_at_literally_200() -> None:
    out = compose(plan_of(), [ShownText("comment", "\n".join(f"l{i}" for i in range(201)))])
    lines = body_lines(out)
    assert len(lines) == 201 and lines[199] == "| l199" and "more characters" in lines[200]
    out = compose(plan_of(), [ShownText("comment", "\n".join(f"l{i}" for i in range(200)))])
    assert "more characters" not in out


def test_the_body_cap_is_literally_20000_characters() -> None:
    out = compose(plan_of(), [ShownText("comment", "k" * 20_001)])
    lines = body_lines(out)
    assert sum(len(x) - 2 for x in lines[:-1]) == 20_000
    assert re.match(r"\| \[\.\.\. 1 more characters; sha256 ", lines[-1])


def test_a_title_is_literally_cut_at_500_and_a_501_title_is_cut() -> None:
    assert "more characters" not in compose(plan_of(), [ShownText("t", "t" * 500, "line")])
    assert "more characters" in compose(plan_of(), [ShownText("t", "t" * 501, "line")])


def test_a_from_value_is_literally_cut_at_2000_characters_and_20_lines() -> None:
    def froms(text: str) -> str:
        return compose(plan_of(changes=[{"name": "description", "from": text, "to": None}]))

    assert "see --json" not in froms("q" * 2000)
    assert "(1 more characters, see --json)" in froms("q" * 2001)
    assert "see --json" not in froms("\n".join(f"o{i}" for i in range(20)))
    assert "see --json" in froms("\n".join(f"o{i}" for i in range(21)))


def test_a_marker_straddling_character_20000_is_never_split() -> None:
    out = compose(plan_of(), [ShownText("comment", "a" * 19_996 + "\x1b" + "b" * 50)])
    lines = body_lines(out)
    assert no_partial_marker(out)
    assert sum(len(x) - 2 for x in lines[:-1]) == 19_996  # the whole marker is left out
    assert re.match(r"\| \[\.\.\. 58 more characters; sha256 ", lines[-1])


def test_a_secret_straddling_character_20000_is_scrubbed_before_the_cut() -> None:
    secret = "straddle-secret-value-9"
    add_secrets([secret])
    out = compose(plan_of(), [ShownText("comment", "a" * 19_995 + secret + "b" * 50)])
    assert "straddle" not in out and "secret-value" not in out
    assert no_partial_marker(out)


def test_a_marker_straddling_the_single_line_cap_is_not_split_and_sanitize_comes_first() -> None:
    # 39 characters, then a control character that renders as 8: 47 safe characters, cap 40.
    out = compose(plan_of(action="a" * 39 + "\x1b"))
    head = out.split("\n")[0]
    assert head.startswith("ticketctl: confirm " + "a" * 39 + " [... 8 more characters] on ")
    assert no_partial_marker(out)


def test_a_marker_straddling_a_step_property_cap_is_not_split() -> None:
    plan = plan_of(steps=[{"kind": "set_fields", "p": "a" * 79 + "\x1b" + "tail"}])
    out = compose(plan)
    assert "p=" + "a" * 79 + " [... 12 more characters]" in out
    assert no_partial_marker(out)


def test_a_fold_never_splits_a_marker() -> None:
    out = compose(plan_of(), [ShownText("comment", "a" * 196 + "\x1b" + "b" * 10)])
    lines = body_lines(out)
    assert lines == ["| " + "a" * 196, "| <U+001B>" + "b" * 10]
    assert no_partial_marker(out)
    out = compose(plan_of(), [ShownText("comment", "a" * 197 + "\x1b" * 3)])
    assert no_partial_marker(out)


def test_sixty_labels_are_all_shown() -> None:
    labels = [f"label-{i:02d}" for i in range(60)]
    out = compose(plan_of(changes=[{"name": "labels", "from": None, "to": labels}]))
    assert [x for x in body_lines(out) if x.startswith("| label-")] == [f"| {x}" for x in labels]
    assert "more characters" not in out


def test_a_label_list_over_the_total_cap_ends_in_a_marker_with_the_hash_of_the_list() -> None:
    labels = ["x" * 100 + f"{i:03d}" for i in range(300)]
    out = compose(plan_of(changes=[{"name": "labels", "from": None, "to": labels}]))
    shown = [x for x in body_lines(out) if x.startswith("| xxx")]
    assert 1 < len(shown) < 300
    m = re.search(r"\| \[\.\.\. (\d+) more characters; sha256 ([0-9a-f]{64})\]", out)
    assert m is not None and m.group(2) == sha("\n".join(labels))
    assert int(m.group(1)) == 300 * 103 - sum(len(x) - 2 for x in shown)
    assert m.group(2) != sha(m.group(0))  # a notice text is never what is hashed


def test_a_partially_shown_list_item_obeys_the_line_cap() -> None:
    labels = ["x" * 500 for _ in range(30)] + ["y" * 15000]
    out = compose(plan_of(changes=[{"name": "labels", "from": None, "to": labels}]))
    lines = body_lines(out)
    assert max(len(x) for x in lines) <= len("| ") + 500
    assert any(x.startswith("| [... ") and "sha256 " in x for x in lines)


def test_a_hostile_change_label_cannot_start_a_label_line() -> None:
    name = "warning: x"
    out = compose(plan_of(), [ShownText(name, "v", "line")])
    heads = [x for x in out.split("\n") if "(sha256 " in x]
    assert len(heads) == 1 and heads[0].startswith("value of warning: x (sha256 ")
    assert not [x for x in out.split("\n") if x.startswith("warning:")]


def test_a_multi_line_string_change_is_cut_at_500_characters_not_as_a_body() -> None:
    to = ("line of text\n" * 100_000)[: 1 << 20]
    out = compose(plan_of(changes=[{"name": "customfield_1", "from": None, "to": to}]))
    lines = body_lines(out)
    assert len(lines) == 2
    assert len(lines[0]) - 2 == 500 and "\n" not in lines[0]
    assert re.match(r"\| \[\.\.\. \d+ more characters; sha256 [0-9a-f]{64}\]", lines[1])


def test_a_shown_text_that_covers_a_change_is_shown_in_full() -> None:
    # a rich-text change comes in through ``shown`` as a body, so the body cap (not 500) applies
    text = "\n".join(f"row {i}" for i in range(50))
    plan = plan_of(changes=[{"name": "description", "from": None, "to": text}])
    out = compose(plan, [ShownText("description", text, "body")])
    assert len([x for x in body_lines(out) if x.startswith("| row")]) == 50
