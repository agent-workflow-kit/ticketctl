"""The text of the confirmation dialog (docs/interface.md, Human actions). Pure, no I/O.

``compose`` turns a plan and the outbound texts into the lines the human reads before the prompt:

1. every outbound text value (``Planned.shown`` first, then every string ``to`` of a change that
   no ``shown`` entry covers, so no text can be sent without being shown), capped;
2. every ``from`` value of a change (provider-owned text), cut to 20 lines and 2000 characters;
3. the steps, the one-line-per-change summary and the warnings;
4. when ``--override-escalation`` is on, the override line and the hook names it covers.

Every string passes the one sanitizer and the secret scrub (``sanitize.render``) before it is
cut, so a control, bidi or invisible character is a visible ``<U+XXXX>`` marker and a secret never
straddles a cut. Every line of a text value starts with ``| ``: a ticket text cannot start a line
that looks like a label, a marker or the prompt. The dialog caps only what it displays; the sent
text and the hashes are unchanged.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Sequence
from typing import Any

from .plan import ShownText
from .sanitize import render

BODY_MAX_LINES = 200
BODY_MAX_CHARS = 20_000
LINE_MAX_CHARS = 500
FOLD_AT = 200
FROM_MAX_LINES = 20
FROM_MAX_CHARS = 2_000
LIST_MAX_ITEMS = 50
BRIEF_CHARS = 80
STEP_CHARS = 200

OVERRIDE_LINE = (
    "escalation override is ON: hooks that ask for review will be overridden by this answer"
)
PREFIX = "| "
_MARKER = re.compile(r"<U\+[0-9A-F]{4,6}>|<redacted>")


def digest(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8", "surrogatepass")).hexdigest()


def _cut_point(text: str, cap: int) -> int:
    """``cap``, moved back so the cut never lands inside a ``<U+XXXX>`` or ``<redacted>`` marker."""
    lo = max(0, cap - 16)
    for m in _MARKER.finditer(text, lo, cap + 16):
        if m.start() < cap < m.end():
            return m.start()
    return cap


def _single(text: str, cap: int) -> str:
    """A one-line, sanitized and scrubbed value cut to ``cap`` characters."""
    safe = render(str(text), body=False)
    if len(safe) <= cap:
        return safe
    at = _cut_point(safe, cap)
    return f"{safe[:at]} [... {len(safe) - at} more characters]"


def _lines(text: str) -> list[str]:
    """Sanitized lines: tabs widened, blank runs collapsed to one, long lines folded at 200."""
    safe = render(text, body=True).replace("\t", "    ")
    out: list[str] = []
    blank = True  # leading blank lines are dropped
    for raw in safe.split("\n"):
        line = raw.rstrip()
        if not line.strip():
            if not blank:
                out.append("")
            blank = True
            continue
        blank = False
        if len(line) <= FOLD_AT:
            out.append(line)
        else:
            out.extend(line[i : i + FOLD_AT] for i in range(0, len(line), FOLD_AT))
    while out and out[-1] == "":
        out.pop()
    return out


def _capped_lines(text: str, *, max_lines: int, max_chars: int) -> tuple[list[str], int]:
    """The lines that fit both caps and the number of characters left out."""
    lines = _lines(text)
    total = sum(len(x) for x in lines)  # characters of text; line breaks do not count
    kept: list[str] = []
    used = 0
    for line in lines:
        if len(kept) >= max_lines:
            break
        if used + len(line) <= max_chars:
            kept.append(line)
            used += len(line)
            continue
        at = _cut_point(line, max_chars - used) if max_chars > used else 0
        if at > 0:
            kept.append(line[:at])
            used += at
        break
    return kept, total - used


def _block(label: str, text: str, *, kind: str, sha: str) -> list[str]:
    header = f"{_single(label, 80)} (sha256 {sha}):"
    if kind == "line":
        safe = render(text, body=False)
        if len(safe) <= LINE_MAX_CHARS:
            return [header, PREFIX + safe]
        at = _cut_point(safe, LINE_MAX_CHARS)
        return [
            header,
            PREFIX + safe[:at],
            f"{PREFIX}[... {len(safe) - at} more characters; sha256 {sha}]",
        ]
    kept, left = _capped_lines(text, max_lines=BODY_MAX_LINES, max_chars=BODY_MAX_CHARS)
    out = [header, *(PREFIX + line for line in kept)]
    if not kept:
        out.append(PREFIX + "(empty)")
    if left > 0:
        out.append(f"{PREFIX}[... {left} more characters; sha256 {sha}]")
    return out


def _from_block(name: str, text: str) -> list[str]:
    kept, left = _capped_lines(text, max_lines=FROM_MAX_LINES, max_chars=FROM_MAX_CHARS)
    out = [f"current {_single(name, 80)} (provider text):", *(PREFIX + x for x in kept)]
    if not kept:
        out.append(PREFIX + "(empty)")
    if left > 0:
        out.append(f"{PREFIX}({left} more characters, see --json)")
    return out


def _brief(value: Any) -> str:
    if value is None:
        return "none"
    if isinstance(value, str):
        return f"text of {len(value)} characters"
    if isinstance(value, dict) and isinstance(value.get("name"), str):
        return _single(value["name"], BRIEF_CHARS)
    if isinstance(value, list):
        return f"{len(value)} item(s)"
    try:
        return _single(json.dumps(value, ensure_ascii=False, sort_keys=True), BRIEF_CHARS)
    except (TypeError, ValueError):
        return "(value)"


def _is_str_list(value: Any) -> bool:
    return isinstance(value, list) and bool(value) and all(isinstance(v, str) for v in value)


def _outbound(plan: dict[str, Any], shown: Sequence[ShownText]) -> list[ShownText]:
    out = list(shown)
    covered = {s.label for s in out}
    for change in plan.get("changes") or []:
        name = str(change.get("name"))
        to = change.get("to")
        if name in covered:
            continue
        if isinstance(to, str):
            out.append(ShownText(name, to, "line" if "\n" not in to else "body"))
        elif _is_str_list(to):
            items = to[:LIST_MAX_ITEMS]
            out.extend(ShownText(f"{name}[{i}]", v, "line") for i, v in enumerate(items))
            if len(to) > LIST_MAX_ITEMS:
                out.append(
                    ShownText(
                        f"{name}", f"({len(to) - LIST_MAX_ITEMS} more items, see --json)", "line"
                    )
                )
    return out


def compose(
    plan: dict[str, Any],
    shown: Sequence[ShownText] = (),
    *,
    override: bool = False,
    hook_names: Sequence[str] = (),
) -> str:
    """The dialog text (no trailing newline); the prompt is written after it."""
    out: list[str] = []
    key = plan.get("key")
    action, site = _single(plan.get("action", "?"), 40), _single(plan.get("site", "?"), 80)
    head = f"ticketctl: confirm {action} on {site}"
    if key:
        head += f" {_single(key, 80)}"
    out.append(head)
    out.append(f"actor: {_single(plan.get('actor', '?'), 120)}")
    out.append(f"plan sha256: {_single(plan.get('plan_sha256', '?'), 64)}")

    outbound = _outbound(plan, shown)
    if outbound:
        out.append("")
        out.append("text that will be sent:")
        for item in outbound:
            sha = item.sha256 or digest(item.text)
            out.extend(_block(item.label, item.text, kind=item.kind, sha=sha))

    froms = [
        (str(c.get("name")), c["from"])
        for c in plan.get("changes") or []
        if isinstance(c.get("from"), str)
    ]
    if froms:
        out.append("")
        for name, text in froms:
            out.extend(_from_block(name, text))

    out.append("")
    steps = plan.get("steps") or []
    for i, step in enumerate(steps, 1):
        props = " ".join(
            f"{_single(k, 40)}={_brief_prop(v)}" for k, v in step.items() if k != "kind"
        )
        out.append(_single(f"step {i}: {step.get('kind')} {props}".rstrip(), STEP_CHARS))
    for change in plan.get("changes") or []:
        old, new = _brief(change.get("from")), _brief(change.get("to"))
        out.append(
            _single(
                f"change {change.get('name')}: {old} -> {new}",
                STEP_CHARS,
            )
        )
    for code in plan.get("warnings") or []:
        out.append(f"warning: {_single(code, 60)}")
    out.append("pre- hooks run after your answer and can still stop the write")
    if override:
        out.append(OVERRIDE_LINE)
        names = list(dict.fromkeys(hook_names))
        out.append("hooks covered: " + (", ".join(_single(n, 80) for n in names) or "none"))
    return "\n".join(out)


def _brief_prop(value: Any) -> str:
    if isinstance(value, (list, dict)):
        try:
            return _single(json.dumps(value, ensure_ascii=False, sort_keys=True), BRIEF_CHARS)
        except (TypeError, ValueError):
            return "(value)"
    return _single(str(value), BRIEF_CHARS)
