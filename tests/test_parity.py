"""Parity of docs/interface.md, the schemas, the code, README.md and config.example.yaml (#40).

``docs/interface.md`` is the normative source. Every check below reads the Markdown by heading and
table header (``tests/docparse.py``), never by line number, and compares it with the schemas, the
code constants and the other documents. A difference fails with a message that names the missing
and the extra values. Each check takes a ``Repo`` bundle, so the mutation tests at the end can hand
it a changed copy of one document or schema and expect the failure: they prove each check can fail.
"""

from __future__ import annotations

import argparse
import copy
import json
import re
import shutil
from collections.abc import Callable
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

import pytest
import yaml
from jsonschema import Draft202012Validator, FormatChecker

from tests import docparse as dp
from tests import golden
from ticketctl import audit, config, plan
from ticketctl.commands import REGISTRY
from ticketctl.errors import CODES, DETAILS_KEYS
from ticketctl.flags import build_global_parser

ROOT = Path(__file__).resolve().parent.parent
INTERFACE_FILE = "docs/interface.md"
README_FILE = "README.md"
EXAMPLE_FILE = "config.example.yaml"
NO_DEFAULT = object()
# Schemas that belong to no single command: shared output shapes.
SHARED_CLI = {"plan", "result", "error"}
# Commands that print nothing (a full-screen view): no `--json` shape, so no schema and no golden.
INTERACTIVE = {"browse"}


@dataclass(frozen=True)
class Repo:
    interface: str
    readme: str
    example: str
    schemas: Path
    golden: Path

    def schema(self, rel: str) -> Any:
        return json.loads((self.schemas / rel).read_text(encoding="utf-8"))

    def defs(self) -> dict[str, Any]:
        return self.schema("defs.json")["$defs"]

    def enum(self, name: str) -> list[str]:
        return self.defs()[name]["enum"]

    def cli_stems(self) -> set[str]:
        return {p.stem for p in (self.schemas / "cli").glob("*.json")}

    def hook_stems(self) -> set[str]:
        return {p.stem for p in (self.schemas / "hooks").glob("*.json")}


def real_repo() -> Repo:
    return Repo(
        interface=(ROOT / "docs" / "interface.md").read_text(encoding="utf-8"),
        readme=(ROOT / "README.md").read_text(encoding="utf-8"),
        example=(ROOT / "config.example.yaml").read_text(encoding="utf-8"),
        schemas=golden.SCHEMAS,
        golden=golden.GOLDEN,
    )


@pytest.fixture(scope="module")
def repo() -> Repo:
    return real_repo()


# --- exit codes, reasons ------------------------------------------------------------------------


def doc_exit_table(md: str) -> dict[str, tuple[int, frozenset[str]]]:
    """`code` -> (exit, reasons) from the Exit codes table."""
    t = dp.table(
        dp.section(md, "Exit codes"), "Exit", "`code`", "`reason`", where="section Exit codes"
    )
    out: dict[str, tuple[int, frozenset[str]]] = {}
    for row in t.rows:
        if not row[0].isdigit():
            raise dp.ParityError(f"Exit codes table: {row[0]!r} is not an exit status")
        status = int(row[0])
        if status == 0:
            continue
        pairs = re.findall(r"`([a-z_]+)`(?: \(reason `([a-z_]+)`)?", row[1])
        reason_cell = row[2]
        shared: set[str] = set()
        if not reason_cell.startswith(("equal to the code", "null", "none")):
            shared = set(re.findall(r"(?<!the )`([a-z_]+)`", reason_cell))
            if shared and len(pairs) != 1:
                raise dp.ParityError(f"exit {status}: reasons listed for several codes")
        for code, own in pairs:
            reasons = set(shared)
            if own:
                reasons.add(own)
            if reason_cell.startswith("equal to the code"):
                reasons.add(code)
            out[code] = (status, frozenset(reasons))
    return out


def check_exit_codes(r: Repo) -> None:
    doc = doc_exit_table(r.interface)
    codes = set(doc)
    reasons = {x for _, rs in doc.values() for x in rs}
    dp.assert_same("interface exit-code table", codes, "defs.json error_code", r.enum("error_code"))
    dp.assert_same("interface reasons", reasons, "defs.json error_reason", r.enum("error_reason"))
    dp.assert_same("interface exit-code table", codes, "audit.ERROR_CODES", audit.ERROR_CODES)
    table = {c: (s.exit, s.reasons) for c, s in CODES.items()}
    mismatched = sorted(c for c in set(doc) | set(table) if doc.get(c) != table.get(c))
    if mismatched:
        detail = {c: (doc.get(c), table.get(c)) for c in mismatched}
        raise AssertionError(f"exit and reasons of interface vs errors.CODES differ: {detail}")


def check_readme_exit_codes(r: Repo) -> None:
    doc = doc_exit_table(r.interface)
    body = dp.section(r.readme, "Exit codes")
    sentence = dp.capture(
        body,
        r"(?i)((?:the )?shared codes from the design guide .*? `invalid_request`)",
        "the sentence 'Shared codes from the design guide (...) plus ... `invalid_request`' "
        "(section Exit codes)",
        README_FILE,
    )
    shown = {int(x) for x in re.findall(r"\d+", sentence)}
    dp.assert_same(
        "README exit statuses",
        map(str, shown),
        "interface exit statuses",
        map(str, {0} | {s for s, _ in doc.values()}),
    )
    reasons = set(r.enum("error_reason"))
    codes = set(r.enum("error_code"))
    seen = set(dp.names(body))
    dp.assert_same("README reasons", seen & reasons, "defs.json error_reason", reasons)
    dp.assert_same("README unknown names", seen - reasons - codes, "nothing", [])
    # The grouping: each `exit N` group names only codes and reasons that have status N.
    statuses: dict[str, set[int]] = {}
    for code, (status, rs) in doc.items():
        for name in (code, *rs):
            statuses.setdefault(name, set()).add(status)
    parts = re.split(r"\bexit (\d+)\b", body)
    wrong = [
        f"{name} is under exit {parts[i]} in the README, the interface has it under "
        f"{sorted(statuses[name])}"
        for i in range(1, len(parts), 2)
        for name in dp.names(parts[i + 1])
        if name in statuses and int(parts[i]) not in statuses[name]
    ]
    if wrong:
        raise AssertionError("README exit-code grouping differs: " + "; ".join(wrong))


# --- enums --------------------------------------------------------------------------------------


def check_plan_enums(r: Repo) -> None:
    body = dp.section(r.interface, "Plan object")
    warns = dp.table(body, "Code", "Meaning", where="section Plan object")
    doc_warnings = [dp.spans(row[0])[0] for row in warns.rows]
    dp.assert_same(
        "interface warnings", doc_warnings, "defs.json warning_code", r.enum("warning_code")
    )
    dp.assert_same("interface warnings", doc_warnings, "plan.WARNINGS", plan.WARNINGS)
    kinds = dp.capture(body, r"\{kind: ([a-z_|]+)", "the step kinds").split("|")
    dp.assert_same("interface step kinds", kinds, "defs.json step_kind", r.enum("step_kind"))
    dp.assert_same("interface step kinds", kinds, "plan.STEP_KINDS", plan.STEP_KINDS)
    dp.assert_same("interface step kinds", kinds, "audit.STEP_KINDS", audit.STEP_KINDS)
    sentence = dp.capture(
        body,
        r"(?i)(?:the |these )?action names are (.*?) \((?:the )?hooks use",
        "the sentence 'The action names are ... (hooks use ...)' (section Plan object)",
        INTERFACE_FILE,
    )
    actions = dp.names(sentence)
    dp.assert_same("interface actions", actions, "defs.json action", r.enum("action"))
    dp.assert_same("interface actions", actions, "plan.ACTIONS", plan.ACTIONS)


def doc_hook_names(md: str) -> list[str]:
    first = next(
        (line for line in dp.section(md, "Hooks").splitlines() if line.strip()),
        "",
    )
    return dp.names(first, r"(?:pre|post)-[a-z]+(?:-[a-z]+)*")


def check_hook_names(r: Repo) -> None:
    doc = doc_hook_names(r.interface)
    dp.assert_same("interface hooks", doc, "defs.json hook_name", r.enum("hook_name"))
    dp.assert_same("interface hooks", doc, "config.HOOK_NAMES", config.HOOK_NAMES)
    dp.assert_same("interface hooks", doc, "audit.HOOK_NAMES", audit.HOOK_NAMES)
    dp.assert_same("interface hooks", doc, "schemas/hooks files", r.hook_stems())
    schema_hooks = r.schema("config.json")["properties"]["hooks"]["properties"]
    dp.assert_same("interface hooks", doc, "config.json hooks properties", schema_hooks)
    kind_table = dp.table(
        dp.section(r.readme, "Hooks"), "Kind", "Hooks", where="README section Hooks"
    )
    readme_hooks = [
        n for row in kind_table.rows for n in dp.names(row[1], r"(?:pre|post)-[a-z]+(?:-[a-z]+)*")
    ]
    dp.assert_same("interface hooks", doc, "README hooks table", readme_hooks)
    for hook in doc:  # each hook schema names itself
        payload = r.schema(f"hooks/{hook}.json")
        if payload["properties"]["hook"] != {"const": hook}:
            raise AssertionError(f"schemas/hooks/{hook}.json does not name itself in `hook`")


def check_result_vocabularies(r: Repo) -> None:
    t = dp.table(
        dp.section(r.interface, "Result object"), "Where", "Values", where="section Result object"
    )
    rows = {row[0]: dp.names(row[1]) for row in t.rows}
    result_doc = rows["result object"]
    dp.assert_same(
        "interface result values",
        result_doc,
        "defs.json result",
        r.defs()["result"]["properties"]["result"]["enum"],
    )
    audit_schema = r.schema("audit.json")["properties"]["result"]["enum"]
    dp.assert_same(
        "interface audit results",
        rows["audit record, per provider call"],
        "audit.json result",
        audit_schema,
    )
    dp.assert_same(
        "interface audit results",
        rows["audit record, per provider call"],
        "audit.RESULTS",
        audit.RESULTS,
    )
    post = rows["post-hook payload, per command"]
    for hook in (h for h in doc_hook_names(r.interface) if h.startswith("post-")):
        enum = r.schema(f"hooks/{hook}.json")["properties"]["result"]["enum"]
        dp.assert_same(
            "interface post-hook results", post, f"schemas/hooks/{hook}.json result", enum
        )
    source = dp.without_parens(
        dp.capture(
            dp.section(r.interface, "Human actions"),
            r"`actor_source` in the audit is (.*?)\. ",
            "actor_source",
        )
    )
    dp.assert_same(
        "interface actor_source",
        dp.names(source),
        "audit.json actor_source",
        r.schema("audit.json")["properties"]["actor_source"]["enum"],
    )
    dp.assert_same(
        "interface actor_source", dp.names(source), "audit.ACTOR_SOURCES", audit.ACTOR_SOURCES
    )


# --- object shapes ------------------------------------------------------------------------------


def check_object_keys(r: Repo) -> None:
    defs = r.defs()
    plan_text = dp.capture(
        dp.section(r.interface, "Plan object"),
        r"`schemas/cli/plan\.json`: (.*?)\. ",
        "the plan keys",
    )
    dp.assert_same(
        "interface plan keys",
        dp.names(dp.without_parens(plan_text)),
        "defs.json plan",
        defs["plan"]["properties"],
    )
    result_text = dp.capture(
        dp.section(r.interface, "Result object"),
        r"printed on exit 0: `\{(.*?)\}`",
        "the result keys",
    )
    dp.assert_same(
        "interface result keys",
        [k.strip() for k in result_text.split(",")],
        "defs.json result",
        defs["result"]["properties"],
    )
    envelope = dp.capture(
        dp.section(r.interface, "Error envelope"), r'`\{"error": \{(.*?)\}\}`', "the envelope keys"
    )
    dp.assert_same(
        "interface envelope keys",
        re.findall(r'"(\w+)"', envelope),
        "defs.json error_body",
        defs["error_body"]["properties"],
    )
    details = dp.capture(
        dp.section(r.interface, "Error envelope"),
        r"depends on (?:the )?code: (.*?)\.\s+`confirmation_required`",
        "the sentence '... depends on the code: ... `confirmation_required`' (Error envelope)",
        INTERFACE_FILE,
    )
    keys = dp.names(dp.without_parens(details), r"[A-Za-z_]+")
    dp.assert_same(
        "interface details keys",
        keys,
        "defs.json error_details",
        defs["error_details"]["properties"],
    )
    dp.assert_same("interface details keys", keys, "errors.DETAILS_KEYS", DETAILS_KEYS)
    audit_doc = dp.section(r.interface, "Audit and log")
    fields = dp.capture(
        audit_doc,
        r"Fields: (.*?)\. `inputs` (?:holds|contains)",
        "the sentence 'Fields: ... `inputs` holds' (section Audit and log)",
        INTERFACE_FILE,
    )
    schema = r.schema("audit.json")["properties"]
    dp.assert_same(
        "interface audit fields",
        dp.names(dp.without_parens(fields)),
        "audit.json properties",
        schema,
    )
    inputs = dp.capture(
        audit_doc,
        r"`inputs` (?:holds|contains) (.*?), as cheap evidence",
        "the sentence '`inputs` holds ..., as cheap evidence' (section Audit and log)",
        INTERFACE_FILE,
    )
    dp.assert_same(
        "interface audit inputs",
        dp.names(dp.without_parens(inputs)),
        "audit.json inputs",
        schema["inputs"]["properties"],
    )


# --- commands, schemas, goldens -----------------------------------------------------------------


def check_commands(r: Repo) -> None:
    doc_tops = {title.split()[0] for title in dp.subheadings(r.interface, "Commands", 3)}
    registry_tops = {path[0] for path in REGISTRY}
    dp.assert_same("interface command headings", doc_tops, "the command registry", registry_tops)
    stems = {"-".join(path) for path in REGISTRY} - INTERACTIVE
    dp.assert_same(
        "schemas/cli (commands)", r.cli_stems() - SHARED_CLI, "the command registry", stems
    )
    missing = SHARED_CLI - r.cli_stems()
    if missing:
        raise AssertionError(f"schemas/cli lacks the shared schemas {sorted(missing)}")
    block = dp.capture(
        dp.section(r.readme, "Commands"), r"```text\n(.*?)```", "the README command block"
    )
    heads: set[str] = set()
    for line in block.splitlines():
        for part in line.removeprefix("ticketctl ").split(" | "):
            if part.strip():
                heads.add(part.split()[0])
    # `sprint add ... | sprint remove` is one `sprint` group; the README names its words.
    dp.assert_same("README commands", heads, "interface command headings", doc_tops)
    referenced = set(re.findall(r"schemas/cli/([a-z-]+)\.json", r.interface))
    gone = referenced - r.cli_stems()
    if gone:
        raise AssertionError(f"interface names schemas that do not exist: {sorted(gone)}")


def check_goldens(r: Repo) -> None:
    problems: list[str] = []
    for stem in sorted(r.cli_stems()):
        if not list((r.golden / "cli" / stem).glob("*.json")):
            problems.append(f"schemas/cli/{stem}.json has no golden file")
    for stem in sorted(r.hook_stems()):
        has = list((r.golden / "hooks" / stem).glob("*.json"))
        if not has:
            problems.append(f"schemas/hooks/{stem}.json has no golden file")
    if problems:
        raise AssertionError("; ".join(problems))


# --- config -------------------------------------------------------------------------------------

# Objects the interface table lists as one row.
TABLE_LEAVES = {"fields", "queries", "hooks", "me"}


def _resolve(node: dict[str, Any], defs: dict[str, Any]) -> dict[str, Any]:
    if "$ref" in node:
        target = defs[node["$ref"].rsplit("/", 1)[1]]
        return {**target, **{k: v for k, v in node.items() if k != "$ref"}}
    return node


def schema_paths(schema: dict[str, Any], stop: set[str]) -> dict[str, Any]:
    """Dotted path -> default (``NO_DEFAULT``) of every leaf; a map of objects uses ``<name>``."""
    defs = schema["$defs"]
    out: dict[str, Any] = {}

    def walk(node: dict[str, Any], path: list[str]) -> None:
        node = _resolve(node, defs)
        props = node.get("properties")
        extra = node.get("additionalProperties")
        if props and not (path and path[-1] in stop):
            for key, child in props.items():
                walk(child, [*path, key])
        elif (
            isinstance(extra, dict)
            and _resolve(extra, defs).get("properties")
            and not (path and path[-1] in stop)
        ):
            walk(extra, [*path, "<name>"])
        else:
            out[".".join(path)] = node.get("default", NO_DEFAULT)

    walk(schema, [])
    return out


def _duration(value: Any) -> Any:
    if isinstance(value, str) and (m := re.fullmatch(r"(\d+)(ms|s|m|h|d)?", value)):
        unit = {"ms": 0.001, "s": 1, "m": 60, "h": 3600, "d": 86400, None: 1}[m.group(2)]
        return int(m.group(1)) * unit
    return value


def doc_config_table(md: str) -> dict[str, Any]:
    """Config key -> default (``NO_DEFAULT`` when the row names none) from the Config table."""
    t = dp.table(dp.section(md, "Config"), "Key", "Default", "Meaning", where="section Config")
    out: dict[str, Any] = {}
    for row in t.rows:
        keys = dp.spans(row[0])
        first = keys[0]
        prefix = first.rsplit(".", 1)[0] + "." if "." in first else ""
        full = [first] + [prefix + k.lstrip(".") for k in keys[1:]]
        defaults = dp.spans(row[1])
        if len(full) > 1 and len(defaults) == len(full):
            pairs = list(zip(full, defaults, strict=True))
        else:
            pairs = [(k, defaults[0] if defaults and len(full) == 1 else None) for k in full]
        for key, text in pairs:
            value: Any = NO_DEFAULT
            if text is not None and not text.startswith(("{n", "<")):
                try:
                    value = json.loads(text)
                except ValueError:
                    value = text
            out[key] = value
    return out


def check_config_table(r: Repo) -> None:
    schema = r.schema("config.json")
    leaves = schema_paths(schema, TABLE_LEAVES)
    doc = doc_config_table(r.interface)
    dp.assert_same("interface Config table", doc, "config.json keys", leaves)
    bad = [
        f"{key}: interface default {doc[key]!r}, schema default "
        f"{None if leaves[key] is NO_DEFAULT else leaves[key]!r}"
        for key in sorted(doc)
        if doc[key] is not NO_DEFAULT
        and (leaves[key] is NO_DEFAULT or _duration(doc[key]) != _duration(leaves[key]))
    ]
    if bad:
        raise AssertionError("defaults differ: " + "; ".join(bad))
    withdefault = [k for k in leaves if leaves[k] is not NO_DEFAULT and doc[k] is NO_DEFAULT]
    if withdefault:
        raise AssertionError(
            f"the schema has a default the interface does not state: {withdefault}"
        )


def _present(tree: Any, segs: list[str]) -> bool:
    if not segs:
        return True
    if not isinstance(tree, dict):
        return False
    if segs[0] == "<name>":
        return any(_present(v, segs[1:]) for v in tree.values())
    return segs[0] in tree and _present(tree[segs[0]], segs[1:])


def _value(tree: Any, segs: list[str]) -> Any:
    for seg in segs:
        tree = next(iter(tree.values())) if seg == "<name>" else tree[seg]
    return tree


# The example shows two commented hooks (a pre- and a post- one).
EXPECTED_COMMENTED_HOOKS = 2
_COMMENT = re.compile(r"^(\s*)# ?(.*)$")
_SAMPLE_KEY = re.compile(r"\s*(?:-\s+)?([\w-]+):(?:\s|$)")
_DEFAULT_COMMENT = re.compile(r"Default: (.+?)(?:\.(?:\s|$)| \(|$)")
_KEY_LINE = re.compile(r"^(\s*)([\w.<>-]+):(?:\s|$)")


def _schema_names(schema: Any) -> set[str]:
    """Every property name and enum string of the schema: the keys a commented sample may use."""
    out: set[str] = set(config.HOOK_NAMES)

    def walk(node: Any) -> None:
        if isinstance(node, dict):
            for key, value in node.items():
                if key == "properties" and isinstance(value, dict):
                    out.update(value)
                if key == "enum" and isinstance(value, list):
                    out.update(v for v in value if isinstance(v, str))
                walk(value)
        elif isinstance(node, list):
            for item in node:
                walk(item)

    walk(schema)
    return out


def _sample_key(body: str, known: set[str]) -> str | None:
    """The key of a commented sample line (text after ``# ``), or None for a prose comment."""
    match = _SAMPLE_KEY.match(body)
    if not match:
        return None
    key = match.group(1)
    if key in known or re.fullmatch(r"(?:pre|post)-[\w-]+", key):
        return key
    return None


def uncomment_samples(text: str, known: set[str], skip: tuple[str, ...] = ()) -> str:
    """``text`` with the commented sample lines uncommented (``# `` removed, indent kept)."""
    out: list[str] = []
    for line in text.splitlines():
        match = _COMMENT.match(line)
        key = _sample_key(match.group(2), known) if match else None
        if match and key is not None and key not in skip:
            out.append(match.group(1) + match.group(2))
        else:
            out.append(line)
    return "\n".join(out) + "\n"


def _load_mapping(text: str, what: str) -> dict[str, Any]:
    try:
        loaded = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise AssertionError(f"{what} is not valid YAML: {str(exc)[:200]}") from None
    if not isinstance(loaded, dict):
        raise AssertionError(f"{what} is not a mapping")
    return loaded


def _key_lines(lines: list[str]) -> dict[tuple[str, ...], int]:
    """Dotted key path (as a tuple) -> line index of every real (not commented) key line."""
    stack: list[tuple[int, str]] = []
    out: dict[tuple[str, ...], int] = {}
    for i, line in enumerate(lines):
        if not line.strip() or line.lstrip().startswith(("#", "-")):
            continue
        match = _KEY_LINE.match(line)
        if not match:
            continue
        indent = len(match.group(1))
        while stack and stack[-1][0] >= indent:
            stack.pop()
        stack.append((indent, match.group(2)))
        out.setdefault(tuple(key for _, key in stack), i)
    return out


def _line_of(keys: dict[tuple[str, ...], int], segs: list[str]) -> int | None:
    for path, index in keys.items():
        if len(path) == len(segs) and all(
            s in ("<name>", p) for s, p in zip(segs, path, strict=True)
        ):
            return index
    return None


def _stated_defaults(lines: list[str], index: int, known: set[str]) -> list[str]:
    """The ``Default: X`` values in the prose comment block directly above line ``index``."""
    block: list[str] = []
    j = index - 1
    while j >= 0:
        match = _COMMENT.match(lines[j])
        if not match or _sample_key(match.group(2), known) is not None:
            break
        block.append(match.group(2).strip())
        j -= 1
    return _DEFAULT_COMMENT.findall(" ".join(reversed(block)))


def _same_default(text: str, default: Any) -> bool:
    try:
        shown = yaml.safe_load(text)
    except yaml.YAMLError:
        return False
    return bool(_duration(shown) == _duration(default))


def check_example(r: Repo) -> None:
    schema = r.schema("config.json")
    paths = schema_paths(schema, {"hooks"})
    known = _schema_names(schema)
    lines = r.example.splitlines()
    keys = _key_lines(lines)
    parsed = _load_mapping(r.example, EXAMPLE_FILE)
    sampled = _load_mapping(uncomment_samples(r.example, known), f"{EXAMPLE_FILE} (samples)")
    problems: list[str] = []
    for path, default in sorted(paths.items()):
        segs = path.split(".")
        named = segs[:-1] if segs[-1] == "<name>" else segs
        if not _present(sampled, named):
            problems.append(f"{path}: not in the example (not even commented out)")
        elif default is not NO_DEFAULT:
            if not _present(parsed, named):
                problems.append(f"{path}: has the default {default!r} but is only commented out")
                continue
            shown = _value(parsed, named)
            row = _line_of(keys, named)
            stated = _stated_defaults(lines, row, known) if row is not None else []
            if _duration(shown) != _duration(default) and not (
                isinstance(default, list | dict) and any(_same_default(s, default) for s in stated)
            ):
                problems.append(
                    f"{path}: the example shows {shown!r}, "
                    f"the default is {default!r} (a list or map may differ only if the comment "
                    "above says 'Default: ...')"
                )
            problems.extend(
                f"{path}: the comment above says 'Default: {s}', the schema default is {default!r}"
                for s in stated
                if not _same_default(s, default)
            )
    hooks = sampled.get("hooks")
    found = list(hooks) if isinstance(hooks, dict) else []
    if len(found) < EXPECTED_COMMENTED_HOOKS:
        problems.append(
            f"expected {EXPECTED_COMMENTED_HOOKS} commented hooks, found {len(found)}: {found}"
        )
    problems.extend(
        f"hooks.{hook}: not a hook name" for hook in found if hook not in config.HOOK_NAMES
    )
    if problems:
        raise AssertionError(f"{EXAMPLE_FILE}: " + "; ".join(problems))
    validator = Draft202012Validator(schema, format_checker=FormatChecker())
    errors = sorted(validator.iter_errors(parsed), key=lambda e: list(e.path))
    if errors:
        raise AssertionError(f"{EXAMPLE_FILE} fails config.json: {errors[0].message[:200]}")
    # The commented samples must be valid too; `email_env` is the alternative to `email`.
    samples = _load_mapping(
        uncomment_samples(r.example, known, skip=("email_env",)), f"{EXAMPLE_FILE} (samples)"
    )
    errors = sorted(validator.iter_errors(samples), key=lambda e: list(e.path))
    if errors:
        raise AssertionError(
            f"{EXAMPLE_FILE} with its commented samples uncommented fails config.json: "
            f"{errors[0].message[:200]}"
        )


# --- environment variables ----------------------------------------------------------------------


def check_environment(r: Repo) -> None:
    pattern = r"TICKETCTL_[A-Z_]+"
    doc = set(dp.names(dp.section(r.interface, "Environment variables"), pattern))
    readme = set(dp.names(dp.section(r.readme, "Environment"), pattern))
    dp.assert_same("interface environment variables", doc, "README environment variables", readme)
    code = {f"TICKETCTL_{n}" for n in (*config.ENV_SCALARS, *config.ENV_NON_CONFIG)}
    dp.assert_same("interface environment variables", doc, "config.ENV_*", code)


# --- flags --------------------------------------------------------------------------------------

MIN_README_EXAMPLES = 3
_FLAG_NAME = re.compile(r"--[a-z][a-z0-9-]*")
_CHOICES = re.compile(r"[A-Za-z]\w*(?:\|[A-Za-z]\w*)+")
_README_DEFAULT = re.compile(
    r"(?i)\bdefault\s+`?([a-z_][a-z0-9_.]*)`?\s*(?:is\s+|of\s+|=\s*)?`?(\d+(?:ms|s|m|h|d)?)`?(?![\w.])"
)


def global_flags() -> dict[str, argparse.Action]:
    """Option string -> action of the global flag parser."""
    return dict(build_global_parser()._option_string_actions)  # noqa: SLF001


def command_flags(path: tuple[str, ...]) -> dict[str, argparse.Action]:
    """Option string -> action of one registry command's own flags."""
    parser = argparse.ArgumentParser(add_help=False)
    REGISTRY[path].add_arguments(parser)
    return {opt: a for a in parser._actions for opt in a.option_strings}  # noqa: SLF001


def _flag_name(span: str) -> str | None:
    match = _FLAG_NAME.match(span)
    return match.group(0) if match else None


def _fence_split(md: str) -> tuple[list[str], list[str]]:
    """The lines inside fenced blocks and the lines outside them."""
    inside: list[str] = []
    outside: list[str] = []
    fence: str | None = None
    for line in md.splitlines():
        marker = re.match(r"^[ \t]*(`{3,}|~{3,})", line)
        if marker:
            fence = None if fence == marker.group(1)[0] else marker.group(1)[0]
        elif fence is not None:
            inside.append(line)
        else:
            outside.append(line)
    return inside, outside


def _example_command(tokens: list[str], line: str) -> tuple[str, ...]:
    glob = global_flags()
    i = 0
    while i < len(tokens) and tokens[i].startswith("-"):
        action = glob.get(tokens[i])
        takes_value = action is not None and action.nargs != 0 and "=" not in tokens[i]
        i += 2 if takes_value else 1
    tokens = tokens[i:]
    if not tokens:
        return ()  # only global flags (`--version`, `--help`)
    for size in (2, 1):
        if tuple(tokens[:size]) in REGISTRY:
            return tuple(tokens[:size])
    if any(path[0] == tokens[0] for path in REGISTRY):
        return ()  # a group word such as `sprints`: only the global flags apply
    raise dp.ParityError(f"README example {line!r}: {tokens[0]!r} is not a command")


def _choices_of(action: argparse.Action) -> set[str] | None:
    if action.choices is not None:
        return {str(c) for c in action.choices}
    if isinstance(action.metavar, str) and "|" in action.metavar:
        return set(action.metavar.split("|"))
    return None


def check_readme_flags(r: Repo) -> None:
    """README flags exist, shown choices match the parser, a stated numeric default is true."""
    glob = global_flags()
    every = set(glob)
    for path in REGISTRY:
        every |= set(command_flags(path))
    inside, outside = _fence_split(r.readme)
    problems: list[str] = []
    checked = 0
    for line in inside:
        text = line.strip().removeprefix("$ ").strip()
        if not text.startswith("ticketctl "):
            continue
        checked += 1
        for part in text.removeprefix("ticketctl ").split(" | "):
            tokens = part.split()
            if not tokens:
                continue
            path = _example_command(tokens, text)
            flags = {**glob, **(command_flags(path) if path else {})}
            where = " ".join(path) or "ticketctl"
            for i, token in enumerate(tokens):
                if not token.startswith("--"):
                    continue
                name = token.partition("=")[0]
                action = flags.get(name)
                if action is None:
                    problems.append(f"{text!r}: {name} is not a flag of {where} or a global flag")
                    continue
                following = tokens[i + 1] if i + 1 < len(tokens) else ""
                if _CHOICES.fullmatch(following):
                    shown, known = set(following.split("|")), _choices_of(action)
                    if known is None:
                        problems.append(
                            f"{text!r}: {name} shows {following} but the parser has no choices"
                        )
                    elif shown != known:
                        problems.append(
                            f"{text!r}: {name} shows {sorted(shown)}, "
                            f"the parser has {sorted(known)}"
                        )
    if checked < MIN_README_EXAMPLES:
        raise dp.ParityError(
            f"README: checked {checked} fenced `ticketctl` example lines, expected at least "
            f"{MIN_README_EXAMPLES}"
        )
    prose = "\n".join(outside)
    for span in dp.spans(prose):
        name = _flag_name(span)
        if name is not None and span.startswith("--") and name not in every:
            problems.append(f"README names {name}, which no command or global option has")
    schema = r.schema("config.json")
    leaves = schema_paths(schema, TABLE_LEAVES)
    for key, value in _README_DEFAULT.findall(prose):
        want = {d for p, d in leaves.items() if p.rsplit(".", 1)[-1] == key.rsplit(".", 1)[-1]}
        if not want:
            problems.append(f"README states a default for {key!r}, which is not a config key")
        elif any(d is NO_DEFAULT or _duration(d) != _duration(value) for d in want):
            problems.append(
                f"README states the default {key} {value}, the schema default is {want}"
            )
    if problems:
        raise AssertionError(f"{README_FILE} flags and defaults: " + "; ".join(problems))


def check_global_flags(r: Repo) -> None:
    body = dp.section(r.interface, "Global flags")
    paragraph = next((line for line in body.splitlines() if line.strip()), "")
    listed = dp.capture(
        paragraph,
        r"^(.*?`--version`)",
        "the flag list that ends with `--version` (first paragraph of section Global flags)",
        INTERFACE_FILE,
    )
    names = {n for span in dp.spans(listed) if span.startswith("--") and (n := _flag_name(span))}
    dp.assert_same("interface Global flags", names, "the global argparse options", global_flags())


CHECKS: dict[str, Callable[[Repo], None]] = {
    "exit_codes": check_exit_codes,
    "readme_exit_codes": check_readme_exit_codes,
    "plan_enums": check_plan_enums,
    "hook_names": check_hook_names,
    "result_vocabularies": check_result_vocabularies,
    "object_keys": check_object_keys,
    "commands": check_commands,
    "goldens": check_goldens,
    "config_table": check_config_table,
    "example": check_example,
    "environment": check_environment,
    "readme_flags": check_readme_flags,
    "global_flags": check_global_flags,
}


@pytest.mark.parametrize("name", sorted(CHECKS))
def test_parity(name: str, repo: Repo) -> None:
    CHECKS[name](repo)


def test_every_config_key_of_the_schema_is_in_the_example_with_its_default(repo: Repo) -> None:
    """The acceptance criterion by name: the check also runs under ``test_parity[example]``."""
    check_example(repo)


# --- the parser reads what it promises ----------------------------------------------------------


def test_sections_ignore_headings_inside_fences() -> None:
    md = "# A\n```\n## not a heading\n```\n## B\ntext\n"
    assert [s.title for s in dp.sections(md)] == ["A", "B"]


def test_section_reports_a_missing_or_repeated_heading() -> None:
    with pytest.raises(dp.ParityError, match="'Nope'"):
        dp.section("# A\n", "Nope")
    with pytest.raises(dp.ParityError, match="found 2"):
        dp.section("# A\n# A\n", "A")


def test_table_cells_keep_escaped_and_quoted_pipes() -> None:
    md = "| a | b |\n|---|---|\n| `x\\|y` | z \\| w |\n"
    t = dp.table(md, "a", "b")
    assert t.rows == (("`x|y`", "z | w"),)


def test_table_header_must_match() -> None:
    with pytest.raises(dp.ParityError, match="header"):
        dp.table("| a | b |\n|---|---|\n| 1 | 2 |\n", "a", "c")


def test_a_blank_line_inside_a_table_is_an_error() -> None:
    md = "## Sec\n\n| a | b |\n|---|---|\n| 1 | 2 |\n\n| 3 | 4 |\n"
    with pytest.raises(
        dp.ParityError, match=r"table in section Sec continues after a blank line: line 7"
    ):
        dp.tables(md, "section Sec")


def test_a_second_table_after_a_gap_is_not_a_continuation() -> None:
    md = "| a | b |\n|---|---|\n| 1 | 2 |\n\nText.\n\n| c | d |\n|---|---|\n| 3 | 4 |\n"
    assert [t.header for t in dp.tables(md)] == [("a", "b"), ("c", "d")]


def test_a_row_after_a_heading_is_not_a_continuation() -> None:
    md = "| a | b |\n|---|---|\n| 1 | 2 |\n\n## Next\n| x | y |\n"
    assert [t.rows for t in dp.tables(md)] == [(("1", "2"),)]


def test_assert_same_names_the_difference() -> None:
    with pytest.raises(
        AssertionError,
        match=r"in left but not in right: \['x'\].*in right but not in left: \['y'\]",
    ):
        dp.assert_same("left", ["a", "x"], "right", ["a", "y"])
    with pytest.raises(AssertionError, match="twice"):
        dp.assert_same("left", ["a", "a"], "right", ["a"])


# --- the checks can fail (mutation proofs) ------------------------------------------------------


def mutate_text(text: str, old: str, new: str) -> str:
    assert text.count(old) == 1, f"the mutation anchor must occur exactly once: {old!r}"
    return text.replace(old, new)


def schema_copy(r: Repo, tmp: Path) -> Repo:
    target = tmp / "schemas"
    shutil.copytree(r.schemas, target)
    return replace(r, schemas=target)


def edit_json(r: Repo, rel: str, edit: Callable[[Any], None]) -> None:
    path = r.schemas / rel
    doc = json.loads(path.read_text(encoding="utf-8"))
    edit(doc)
    path.write_text(json.dumps(doc), encoding="utf-8")


def _drop(seq: list[str], value: str) -> None:
    seq.remove(value)


def _defs_enum(name: str, edit: Callable[[list[str]], None]) -> Callable[[Any], None]:
    return lambda doc: edit(doc["$defs"][name]["enum"])


# (check, what is changed, message the failure must contain)
DOC_MUTATIONS = [
    ("exit_codes", "| 21 | `ambiguous_site`, ", "| 21 | ", "ambiguous_site"),
    (
        "exit_codes",
        "| 13 | `confirmation_required`",
        "| 14 | `confirmation_required`",
        "confirmation_required",
    ),
    ("exit_codes", "`locked`", "`lockedx`", "locked"),
    ("exit_codes", "| `usage` | `human_only`", "| `usage` | `human_onlyx`", "human_only"),
    ("plan_enums", "| `closed_ticket` |", "| `closed_ticketx` |", "closed_ticket"),
    ("plan_enums", "{kind: assign|transition|", "{kind: assign|", "transition"),
    (
        "plan_enums",
        "`sprint_add`, `sprint_remove`, `link_add`",
        "`sprint_remove`, `link_add`",
        "sprint_add",
    ),
    (
        "plan_enums",
        "`link_add`, `link_remove`, `comment_edit`, `comment_delete` and `attachment_add` (hooks",
        "`link_add`, `comment_edit`, `comment_delete` and `attachment_add` (hooks",
        "link_remove",
    ),
    (
        "plan_enums",
        "`link_remove`, `comment_edit`",
        "`link_remove`, `comment_editx`",
        "comment_edit",
    ),
    (
        "plan_enums",
        "|add_link|remove_link|edit_comment|delete_comment|add_attachment, ...}",
        "|add_link|edit_comment|delete_comment|add_attachment, ...}",
        "remove_link",
    ),
    (
        "hook_names",
        "`pre-sprint`, `post-sprint`, `pre-link`",
        "`pre-sprint`, `pre-link`",
        "post-sprint",
    ),
    ("hook_names", "`pre-link`, `post-link` (", "`pre-link` (", "post-link"),
    (
        "result_vocabularies",
        "| `attempted`, `ok`, `failed`, `partial`, `unknown` |",
        "| `attempted`, `ok`, `failed`, `unknown` |",
        "partial",
    ),
    (
        "result_vocabularies",
        "`ok`, `failed`, `partial`, `unknown` |\n\n",
        "`ok`, `failed`, `unknown` |\n\n",
        "partial",
    ),
    (
        "object_keys",
        "`dedupe_match` (the provider id",
        "`dedupe_matchx` (the provider id",
        "dedupe_match",
    ),
    ("object_keys", "`rate_limit`, `token_env`.", "`token_env`.", "rate_limit"),
    ("object_keys", "`stdin_tty`, `decision_id`", "`decision_id`", "stdin_tty"),
    ("commands", "### `export`", "### `exportx`", "export"),
    (
        "config_table",
        "| `limits.body_chars` | `30000` |",
        "| `limits.body_chars` | `30001` |",
        "limits.body_chars",
    ),
    (
        "config_table",
        "| `sites.<name>.cache_ttl` | `24h` |",
        "| `sites.<name>.cache_ttl` | `48h` |",
        "cache_ttl",
    ),
    ("config_table", "| `default_site` | none |", "| `default_sitex` | none |", "default_site"),
    (
        "environment",
        "`TICKETCTL_LIVE` is the live-test switch",
        "`TICKETCTL_LIVEX` is the live-test switch",
        "TICKETCTL_LIVE",
    ),
    ("global_flags", "`--verbose`, `--quiet`, `--timeout", "`--verbose`, `--timeout", "--quiet"),
    (
        "global_flags",
        "in addition to configured hooks), `--version`.",
        "in addition to configured hooks), `--extra`, `--version`.",
        "--extra",
    ),
]


@pytest.mark.parametrize(
    ("check", "old", "new", "expected"),
    DOC_MUTATIONS,
    ids=[f"{c}:{o[:28]}" for c, o, _, _ in DOC_MUTATIONS],
)
def test_a_changed_interface_fails_its_check(
    repo: Repo, check: str, old: str, new: str, expected: str
) -> None:
    CHECKS[check](repo)
    changed = replace(repo, interface=mutate_text(repo.interface, old, new))
    with pytest.raises(AssertionError, match=re.escape(expected)):
        CHECKS[check](changed)


SCHEMA_MUTATIONS: list[tuple[str, str, Callable[[Any], None] | None, str]] = [
    (
        "exit_codes",
        "defs.json",
        _defs_enum("error_code", lambda e: _drop(e, "lookup_incomplete")),
        "lookup_incomplete",
    ),
    (
        "exit_codes",
        "defs.json",
        _defs_enum("error_reason", lambda e: e.append("brand_new")),
        "brand_new",
    ),
    (
        "exit_codes",
        "defs.json",
        _defs_enum("error_reason", lambda e: _drop(e, "empty_body")),
        "empty_body",
    ),
    (
        "plan_enums",
        "defs.json",
        _defs_enum("warning_code", lambda e: _drop(e, "sprint_moved")),
        "sprint_moved",
    ),
    ("plan_enums", "defs.json", _defs_enum("step_kind", lambda e: e.append("delete")), "delete"),
    ("plan_enums", "defs.json", _defs_enum("action", lambda e: _drop(e, "update")), "update"),
    (
        "hook_names",
        "defs.json",
        _defs_enum("hook_name", lambda e: _drop(e, "post-create")),
        "post-create",
    ),
    (
        "hook_names",
        "config.json",
        lambda d: d["properties"]["hooks"]["properties"].pop("pre-update"),
        "pre-update",
    ),
    (
        "hook_names",
        "hooks/post-comment.json",
        lambda d: d["properties"]["hook"].update(const="post-commentx"),
        "post-comment",
    ),
    (
        "result_vocabularies",
        "audit.json",
        lambda d: d["properties"]["result"]["enum"].remove("unknown"),
        "unknown",
    ),
    (
        "result_vocabularies",
        "hooks/post-update.json",
        lambda d: d["properties"]["result"]["enum"].append("skipped"),
        "skipped",
    ),
    (
        "result_vocabularies",
        "audit.json",
        lambda d: d["properties"]["actor_source"]["enum"].remove("tty"),
        "tty",
    ),
    (
        "object_keys",
        "defs.json",
        lambda d: d["$defs"]["plan"]["properties"].pop("dedupe_key"),
        "dedupe_key",
    ),
    (
        "object_keys",
        "defs.json",
        lambda d: d["$defs"]["error_details"]["properties"].pop("existing_key"),
        "existing_key",
    ),
    (
        "object_keys",
        "audit.json",
        lambda d: d["properties"]["inputs"]["properties"].pop("site_host"),
        "site_host",
    ),
    ("commands", "cli/whoami.json", None, "whoami"),  # None: the file is deleted
    (
        "config_table",
        "config.json",
        lambda d: d["properties"]["limits"]["properties"]["body_chars"].update(default=29999),
        "body_chars",
    ),
    (
        "config_table",
        "config.json",
        lambda d: d["properties"].update(extra_key={"type": "string"}),
        "extra_key",
    ),
    (
        "example",
        "config.json",
        lambda d: d["properties"]["limits"]["properties"].update(
            new_limit={"type": "integer", "default": 5}
        ),
        "new_limit",
    ),
    (
        "example",
        "config.json",
        lambda d: d["$defs"]["site"]["properties"]["provider"].update(default="other"),
        "provider",
    ),
]


@pytest.mark.parametrize(
    ("check", "rel", "edit", "expected"),
    SCHEMA_MUTATIONS,
    ids=[f"{c}:{rel}:{e}" for c, rel, _, e in SCHEMA_MUTATIONS],
)
def test_a_changed_schema_fails_its_check(
    repo: Repo,
    tmp_path: Path,
    check: str,
    rel: str,
    edit: Callable[[Any], None] | None,
    expected: str,
) -> None:
    changed = schema_copy(repo, tmp_path)
    if edit is None:
        (changed.schemas / rel).unlink()
    else:
        edit_json(changed, rel, edit)
    with pytest.raises(AssertionError, match=re.escape(expected)):
        CHECKS[check](changed)


EXAMPLE_MUTATIONS = [
    ("    cache_ttl: 24h\n", "    cache_ttl: 12h\n", "cache_ttl"),
    ("    allow_http: false\n", "", "allow_http"),
    ("  retry_after_max: 60s\n", "", "retry_after_max"),
    ("    token_env: JIRA_API_TOKEN\n", "    tokenenv: JIRA_API_TOKEN\n", "token_env"),
    ("    timeouts:\n      connect: 10s\n", "    timeouts:\n      connect: 11s\n", "connect"),
    # a stale `Default:` comment fails even when the shown value equals the real default
    ("cache. Default: 24h.", "cache. Default: 48h.", "cache_ttl"),
    ("Default: 60s.", "Default: 90s.", "retry_after_max"),
    # a leaf deleted from its own block is not covered by the same name elsewhere
    ("me:\n  name: me\n", "me:\n", "me.name"),
    # commented hooks: a wrong name, and fewer than expected
    ("#   pre-transition:\n", "#   pre-transitionx:\n", "pre-transitionx"),
    (
        '# hooks:\n#   pre-transition:\n#     - cmd: "my-policy-check --stdin-json"\n'
        "#       timeout: 30\n#   post-comment:\n"
        '#     - cmd: "my-notifier --stdin-json"\n#       async: true\n',
        "",
        "commented hooks",
    ),
    # a commented sample that breaks the schema (a mapping names exactly one of name, id, schema)
    (
        "      #   name: Acceptance Criteria\n",
        "      #   name: Acceptance Criteria\n      #   id: acceptance\n",
        "commented samples",
    ),
]


@pytest.mark.parametrize(("old", "new", "expected"), EXAMPLE_MUTATIONS)
def test_a_changed_example_fails_the_example_check(
    repo: Repo, old: str, new: str, expected: str
) -> None:
    changed = replace(repo, example=mutate_text(repo.example, old, new))
    with pytest.raises(AssertionError, match=re.escape(expected)):
        check_example(changed)


README_MUTATIONS = [
    ("exit_codes_readme", "`locked`", "`lockedx`", "locked"),
    ("hook_names", "| `pre-sprint`, `post-sprint` |", "| `pre-sprint` |", "post-sprint"),
    ("commands", "ticketctl export", "ticketctl exportx", "export"),
    (
        "environment",
        "`TICKETCTL_LIVE` is the live-test switch",
        "`TICKETCTL_LIVEX` is the live-test switch",
        "TICKETCTL_LIVE",
    ),
    ("readme_flags", "transition KEY --to STATUS", "transition KEY --too STATUS", "--too"),
    ("readme_flags", "--sprint open|next|ID", "--sprint open|next|latest|ID", "--sprint"),
    ("readme_flags", "--query my_open --json", "--query my_open --jsonx", "--jsonx"),
    ("readme_flags", "`--dry-run`", "`--dry-runx`", "--dry-runx"),
    ("readme_flags", "Stateless: a small", "Stateless (default timeout 300s): a small", "timeout"),
    (
        "readme_flags",
        "Stateless: a small",
        "Stateless (default nothing_here 5): a small",
        "nothing_here",
    ),
    # `locked` belongs under exit 12, not exit 22
    (
        "exit_codes_readme",
        "exit 12 `unavailable` with `locked`",
        "exit 12 `unavailable`; exit 22 `locked`",
        "locked is under exit 22",
    ),
]


@pytest.mark.parametrize(("check", "old", "new", "expected"), README_MUTATIONS)
def test_a_changed_readme_fails_its_check(
    repo: Repo, check: str, old: str, new: str, expected: str
) -> None:
    name = {"exit_codes_readme": "readme_exit_codes"}.get(check, check)
    CHECKS[name](repo)
    changed = replace(repo, readme=mutate_text(repo.readme, old, new))
    with pytest.raises(AssertionError, match=re.escape(expected)):
        CHECKS[name](changed)


def _fenced(*lines: str) -> str:
    return "```\n" + "\n".join(lines) + "\n```\n"


def test_a_readme_with_too_few_fenced_examples_fails_the_flag_check(repo: Repo) -> None:
    changed = replace(repo, readme=_fenced("ticketctl whoami", "ticketctl fields"))
    with pytest.raises(AssertionError, match="at least 3"):
        check_readme_flags(changed)


def test_prompt_prefixed_examples_are_still_checked(repo: Repo) -> None:
    lines = ["$ ticketctl whoami", "  $ ticketctl fields", "$ ticketctl sites --nope"]
    with pytest.raises(AssertionError, match="--nope"):
        check_readme_flags(replace(repo, readme=_fenced(*lines)))


def test_global_flags_before_the_command_are_skipped(repo: Repo) -> None:
    lines = [
        "ticketctl --json search --query my_open",
        "ticketctl --site main --timeout 5s whoami",
        "ticketctl --version",
    ]
    check_readme_flags(replace(repo, readme=_fenced(*lines)))


def test_a_global_only_example_is_checked_against_global_flags_only(repo: Repo) -> None:
    lines = ["ticketctl whoami", "ticketctl fields", "ticketctl sites", "ticketctl --versionx"]
    with pytest.raises(AssertionError, match="--versionx"):
        check_readme_flags(replace(repo, readme=_fenced(*lines)))


WORDING_VARIANTS = [
    ("plan_enums", "The action names are ", "These action names are "),
    ("plan_enums", "(hooks use", "(the hooks use"),
    ("object_keys", "depends on the code:", "depends on code:"),
    ("object_keys", "`inputs` holds", "`inputs` contains"),
]


@pytest.mark.parametrize(("check", "old", "new"), WORDING_VARIANTS)
def test_a_reworded_interface_sentence_still_parses(
    repo: Repo, check: str, old: str, new: str
) -> None:
    CHECKS[check](replace(repo, interface=mutate_text(repo.interface, old, new)))


def test_a_missing_interface_sentence_names_the_sentence_and_the_file(repo: Repo) -> None:
    changed = replace(
        repo,
        interface=mutate_text(repo.interface, "The action names are ", "Names of the actions: "),
    )
    with pytest.raises(dp.ParityError, match=r"action names are .*docs/interface\.md"):
        check_plan_enums(changed)


def test_a_missing_readme_sentence_names_the_sentence_and_the_file(repo: Repo) -> None:
    changed = replace(
        repo, readme=mutate_text(repo.readme, "Shared codes from the design guide", "Codes")
    )
    with pytest.raises(dp.ParityError, match=r"Shared codes from the design guide.*README\.md"):
        check_readme_exit_codes(changed)


def test_the_readme_sentence_tolerates_a_leading_the_and_case(repo: Repo) -> None:
    changed = replace(
        repo,
        readme=mutate_text(
            repo.readme,
            "Shared codes from the design guide",
            "The shared codes from the Design Guide",
        ),
    )
    check_readme_exit_codes(changed)


def test_the_commented_samples_are_uncommented_and_valid(repo: Repo) -> None:
    known = _schema_names(repo.schema("config.json"))
    samples = yaml.safe_load(uncomment_samples(repo.example, known, skip=("email_env",)))
    assert sorted(samples["hooks"]) == ["post-comment", "pre-transition"]
    assert "acceptance_criteria" in samples["sites"]["main"]["fields"]
    Draft202012Validator(repo.schema("config.json")).validate(samples)


def test_a_command_without_a_golden_file_fails(repo: Repo, tmp_path: Path) -> None:
    target = tmp_path / "golden"
    shutil.copytree(repo.golden, target)
    shutil.rmtree(target / "cli" / "sprint-add")
    with pytest.raises(AssertionError, match="sprint-add"):
        check_goldens(replace(repo, golden=target))


def test_a_schema_without_a_command_fails(repo: Repo, tmp_path: Path) -> None:
    changed = schema_copy(repo, tmp_path)
    shutil.copy(changed.schemas / "cli" / "whoami.json", changed.schemas / "cli" / "orphan.json")
    with pytest.raises(AssertionError, match="orphan"):
        check_commands(changed)


def test_the_example_must_still_validate(repo: Repo) -> None:
    changed = replace(
        repo, example=mutate_text(repo.example, "    provider: jira\n", "    provider: other\n")
    )
    with pytest.raises(AssertionError, match="fails config.json|provider"):
        check_example(changed)


def test_schema_paths_do_not_alias_the_loaded_schema(repo: Repo) -> None:
    schema = repo.schema("config.json")
    before = copy.deepcopy(schema)
    schema_paths(schema, TABLE_LEAVES)
    assert schema == before
