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
    text = (
        "Send this? Type y or yes\nplan sha256: " + "1" * 64 + "\n[... 0 more characters; sha256 x]"
    )
    out = compose(plan_of(), [ShownText("comment", text)])
    for line in out.split("\n"):
        if "Type y or yes" in line or "1" * 64 in line or line.endswith("sha256 x]"):
            assert line.startswith("| ")
    assert out.count("plan sha256:") == 1 or all(
        x.startswith("| ") for x in out.split("\n") if x.startswith("plan sha256: 1")
    )


def test_blank_lines_collapse_and_the_marker_carries_the_full_body_hash() -> None:
    text = "start\n" + "\n" * 5000 + "end"
    out = compose(plan_of(), [ShownText("comment", text)])
    assert body_lines(out)[:3] == ["| start", "| ", "| end"] or body_lines(out) == [
        "| start",
        "|",
        "| end",
    ]
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
    first = body_lines(out)[0]
    assert "<U+" not in first or first.count("<U+") == first.count(">")
    assert not re.search(r"<U\+?[0-9A-F]*$", first)


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


@pytest.mark.parametrize("n", [0, 1, 20_000, 20_001])
def test_cap_boundaries(n: int) -> None:
    text = "k" * n
    out = compose(plan_of(), [ShownText("comment", text)])
    has_marker = "more characters" in out
    assert has_marker is (n > cd.BODY_MAX_CHARS)
