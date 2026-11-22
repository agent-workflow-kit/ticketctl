"""Parity of docs/interface.md, the schemas, the code, README.md and config.example.yaml (#40).

``docs/interface.md`` is the normative source. Every check below reads the Markdown by heading and
table header (``tests/docparse.py``), never by line number, and compares it with the schemas, the
code constants and the other documents. A difference fails with a message that names the missing
and the extra values. Each check takes a ``Repo`` bundle, so the mutation tests at the end can hand
it a changed copy of one document or schema and expect the failure: they prove each check can fail.
"""

from __future__ import annotations

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

ROOT = Path(__file__).resolve().parent.parent
NO_DEFAULT = object()
# Schemas that belong to no single command: shared output shapes.
SHARED_CLI = {"plan", "result", "error"}


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
    t = dp.table(dp.section(md, "Exit codes"), "Exit", "`code`", "`reason`")
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
        body, r"(Shared codes from the design guide .*? `invalid_request`)", "status list"
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


# --- enums --------------------------------------------------------------------------------------


def check_plan_enums(r: Repo) -> None:
    body = dp.section(r.interface, "Plan object")
    warns = dp.table(body, "Code", "Meaning")
    doc_warnings = [dp.spans(row[0])[0] for row in warns.rows]
    dp.assert_same(
        "interface warnings", doc_warnings, "defs.json warning_code", r.enum("warning_code")
    )
    dp.assert_same("interface warnings", doc_warnings, "plan.WARNINGS", plan.WARNINGS)
    kinds = dp.capture(body, r"\{kind: ([a-z_|]+)", "the step kinds").split("|")
    dp.assert_same("interface step kinds", kinds, "defs.json step_kind", r.enum("step_kind"))
    dp.assert_same("interface step kinds", kinds, "plan.STEP_KINDS", plan.STEP_KINDS)
    dp.assert_same("interface step kinds", kinds, "audit.STEP_KINDS", audit.STEP_KINDS)
    sentence = dp.capture(body, r"The action names are (.*?) \(hooks use", "the action names")
    actions = dp.names(sentence)
    dp.assert_same("interface actions", actions, "defs.json action", r.enum("action"))
    dp.assert_same("interface actions", actions, "plan.ACTIONS", plan.ACTIONS)


def doc_hook_names(md: str) -> list[str]:
    first = next(
        (line for line in dp.section(md, "Hooks").splitlines() if line.strip()),
        "",
    )
    return dp.names(first, r"(?:pre|post)-[a-z]+")


def check_hook_names(r: Repo) -> None:
    doc = doc_hook_names(r.interface)
    dp.assert_same("interface hooks", doc, "defs.json hook_name", r.enum("hook_name"))
    dp.assert_same("interface hooks", doc, "config.HOOK_NAMES", config.HOOK_NAMES)
    dp.assert_same("interface hooks", doc, "audit.HOOK_NAMES", audit.HOOK_NAMES)
    dp.assert_same("interface hooks", doc, "schemas/hooks files", r.hook_stems())
    schema_hooks = r.schema("config.json")["properties"]["hooks"]["properties"]
    dp.assert_same("interface hooks", doc, "config.json hooks properties", schema_hooks)
    kind_table = dp.table(dp.section(r.readme, "Hooks"), "Kind", "Hooks")
    readme_hooks = [n for row in kind_table.rows for n in dp.names(row[1], r"(?:pre|post)-[a-z]+")]
    dp.assert_same("interface hooks", doc, "README hooks table", readme_hooks)
    for hook in doc:  # each hook schema names itself
        payload = r.schema(f"hooks/{hook}.json")
        if payload["properties"]["hook"] != {"const": hook}:
            raise AssertionError(f"schemas/hooks/{hook}.json does not name itself in `hook`")


def check_result_vocabularies(r: Repo) -> None:
    t = dp.table(dp.section(r.interface, "Result object"), "Where", "Values")
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
        r"depends on the code: (.*?)\.\s+`confirmation_required`",
        "the details keys",
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
    fields = dp.capture(audit_doc, r"Fields: (.*?)\. `inputs` holds", "the audit fields")
    schema = r.schema("audit.json")["properties"]
    dp.assert_same(
        "interface audit fields",
        dp.names(dp.without_parens(fields)),
        "audit.json properties",
        schema,
    )
    inputs = dp.capture(audit_doc, r"`inputs` holds (.*?), as cheap evidence", "the audit inputs")
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
    stems = {"-".join(path) for path in REGISTRY}
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
    t = dp.table(dp.section(md, "Config"), "Key", "Default", "Meaning")
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


def _states_default(text: str, key: str, default: Any) -> bool:
    """Whether the comment block right above ``key:`` says ``Default: <default>``."""
    lines = text.splitlines()
    for i, line in enumerate(lines):
        if re.match(rf"^\s*{re.escape(key)}:", line):
            block: list[str] = []
            j = i - 1
            while j >= 0 and lines[j].lstrip().startswith("#"):
                block.append(lines[j])
                j -= 1
            shown = yaml.safe_dump(default, default_flow_style=True, width=1000).strip()
            shown = shown.removesuffix("\n...").strip()
            return any(re.search(rf"Default: {re.escape(shown)}(?!\w)", b, re.I) for b in block)
    return False


def check_example(r: Repo) -> None:
    schema = r.schema("config.json")
    paths = schema_paths(schema, {"hooks"})
    parsed = yaml.safe_load(r.example)
    if not isinstance(parsed, dict):
        raise AssertionError("config.example.yaml is not a mapping")
    problems: list[str] = []
    for path, default in sorted(paths.items()):
        segs = path.split(".")
        named = segs[:-1] if segs[-1] == "<name>" else segs
        commented = re.search(rf"^\s*#\s*{re.escape(named[-1])}:", r.example, re.M)
        here = _present(parsed, named)
        if not here and not commented:
            problems.append(f"{path}: not in the example (not even commented out)")
        elif default is not NO_DEFAULT:
            if not here:
                problems.append(f"{path}: has the default {default!r} but is only commented out")
            elif _duration(_value(parsed, named)) != _duration(default) and not (
                isinstance(default, list | dict) and _states_default(r.example, named[-1], default)
            ):
                problems.append(
                    f"{path}: the example shows {_value(parsed, named)!r}, "
                    f"the default is {default!r} (a list or map may differ only if the comment "
                    "above says 'Default: ...')"
                )
    for hook in re.findall(r"^#\s{3}((?:pre|post)-[a-z]+):", r.example, re.M):
        if hook not in r.enum("hook_name"):
            problems.append(f"hooks.{hook}: not a hook name")
    if problems:
        raise AssertionError("config.example.yaml: " + "; ".join(problems))
    validator = Draft202012Validator(schema, format_checker=FormatChecker())
    errors = sorted(validator.iter_errors(parsed), key=lambda e: list(e.path))
    if errors:
        raise AssertionError(f"config.example.yaml fails config.json: {errors[0].message[:200]}")


# --- environment variables ----------------------------------------------------------------------


def check_environment(r: Repo) -> None:
    pattern = r"TICKETCTL_[A-Z_]+"
    doc = set(dp.names(dp.section(r.interface, "Environment variables"), pattern))
    readme = set(dp.names(dp.section(r.readme, "Environment"), pattern))
    dp.assert_same("interface environment variables", doc, "README environment variables", readme)
    code = {f"TICKETCTL_{n}" for n in (*config.ENV_SCALARS, *config.ENV_NON_CONFIG)}
    dp.assert_same("interface environment variables", doc, "config.ENV_*", code)


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
        "`sprint_add` and `sprint_remove` (hooks use",
        "`sprint_remove` (hooks use",
        "sprint_add",
    ),
    ("hook_names", "`pre-sprint`, `post-sprint`. Reads", "`pre-sprint`. Reads", "post-sprint"),
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
