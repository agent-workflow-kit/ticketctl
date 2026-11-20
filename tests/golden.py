"""The golden-file harness (issue #10).

Layout: ``tests/golden/cli/<command>/<case>.json`` is validated against
``schemas/cli/<command>.json``
and ``tests/golden/hooks/<hook>/<case>.json`` against ``schemas/hooks/<hook>.json``; ``<command>``
and ``<hook>`` are the schema's file stem. A test produces a document and calls
``assert_golden("cli", "whoami", "basic", doc)``: the document must satisfy the schema and equal the
golden file, compared type-strictly (``True`` is not ``1``, ``1.0`` is not ``1``).
``TICKETCTL_UPDATE_GOLDEN=1`` writes the file (after the schema check) instead of comparing, and
refreshes only the ``tests/golden/MANIFEST.json`` digest of the schema it just wrote a golden for,
so a stale digest of any other schema keeps failing. Update mode refuses to run when ``CI`` is set.

A schema is validated with a format checker, so ``format: uri`` is enforced.

Three guards run in ``tests/test_golden.py`` through ``check_tree``:

- every golden file validates against its schema, so a golden that fails its schema fails the run;
- ``MANIFEST.json`` records a digest of each schema that has a golden (the schema and every file it
  references, parsed and re-serialized, so formatting does not matter); a schema change without a
  golden update makes the digest differ and fails the run until the goldens are reviewed and the
  manifest is refreshed;
- every schema under ``schemas/cli`` and ``schemas/hooks`` has a golden or is listed in ``PENDING``
  with the issue that owns its command; a pending entry that gets a golden, or names no schema,
  fails, so the list can only shrink;
- no stray file sits under ``tests/golden`` (every file is ``<name>.json`` of lower case letters,
  digits and hyphens, apart from the manifest).
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from jsonschema import Draft202012Validator, FormatChecker
from referencing import Registry, Resource

ROOT = Path(__file__).resolve().parent.parent
SCHEMAS = ROOT / "schemas"
GOLDEN = Path(__file__).resolve().parent / "golden"
MANIFEST = "MANIFEST.json"
UPDATE_ENV = "TICKETCTL_UPDATE_GOLDEN"
KINDS = ("cli", "hooks")

# Schemas whose command or hook is not implemented yet, with the owning issue. Remove an entry in
# the PR that adds the first golden file for it.
PENDING: dict[str, int] = {
    "cli/comments.json": 18,
    "cli/transitions.json": 18,
    "cli/sprints-list.json": 18,
    "cli/sprints-show.json": 18,
    "cli/plan.json": 23,
    "cli/result.json": 23,
    "cli/transition.json": 28,
    "cli/comment.json": 29,
    "cli/update.json": 30,
    "cli/create.json": 31,
    "cli/sprint-add.json": 34,
    "cli/sprint-remove.json": 34,
    "cli/export.json": 36,
    **{
        f"hooks/{when}-{kind}.json": 25
        for when in ("pre", "post")
        for kind in ("comment", "create", "sprint", "transition", "update")
    },
}


NAME = re.compile(r"[a-z0-9-]+")


def updating() -> bool:
    """Whether this run rewrites goldens. Never allowed in CI, where a golden must not change."""
    if os.environ.get(UPDATE_ENV) != "1":
        return False
    if "CI" in os.environ:
        raise RuntimeError(
            f"{UPDATE_ENV}=1 is refused when CI is set: goldens are updated locally and reviewed"
        )
    return True


def _load(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def registry(schemas: Path = SCHEMAS) -> Registry:
    resources = []
    for path in schemas.rglob("*.json"):
        doc = _load(path)
        uri = (
            "ticketctl/" + path.relative_to(schemas).as_posix() if "$id" not in doc else doc["$id"]
        )
        resources.append((uri, Resource.from_contents(doc)))
        resources.append((path.resolve().as_uri(), Resource.from_contents(doc)))
    return Registry().with_resources(resources)


def _is_uri(value: object) -> bool:
    """A small stdlib ``uri`` check (no extra dependency): a scheme, a host, no whitespace."""
    if not isinstance(value, str):
        return True  # format applies to strings only
    if not value or re.search(r"\s", value):
        return False
    try:
        parts = urlsplit(value)
    except ValueError:
        return False
    return bool(parts.scheme) and bool(parts.netloc)


def format_checker() -> FormatChecker:
    checker = FormatChecker()
    checker.checks("uri")(_is_uri)
    return checker


def validator(rel: str, schemas: Path = SCHEMAS) -> Draft202012Validator:
    return Draft202012Validator(
        _load(schemas / rel), registry=registry(schemas), format_checker=format_checker()
    )


def schema_files(schemas: Path = SCHEMAS) -> list[str]:
    return sorted(
        p.relative_to(schemas).as_posix() for kind in KINDS for p in (schemas / kind).glob("*.json")
    )


def _refs(path: Path) -> list[Path]:
    text = path.read_text(encoding="utf-8")
    found = {m.group(1) for m in re.finditer(r'"\$ref"\s*:\s*"([^"#]+)(?:#[^"]*)?"', text)}
    return sorted((path.parent / name).resolve() for name in found)


def digest(rel: str, schemas: Path = SCHEMAS) -> str:
    """Digest of a schema and, transitively, every schema file it references."""
    seen: dict[Path, None] = {}
    stack = [(schemas / rel).resolve()]
    while stack:
        path = stack.pop()
        if path in seen:
            continue
        seen[path] = None
        stack.extend(_refs(path))
    h = hashlib.sha256()
    for path in sorted(seen, key=lambda p: p.as_posix()):
        canonical = json.dumps(_load(path), sort_keys=True, separators=(",", ":"))
        h.update(path.name.encode() + b"\0" + canonical.encode() + b"\0")
    return h.hexdigest()


def schema_for(golden: Path, golden_dir: Path = GOLDEN) -> str:
    """``cli/<command>.json`` or ``hooks/<hook>.json`` for a golden file's path."""
    parts = golden.relative_to(golden_dir).parts
    if len(parts) != 3 or parts[0] not in KINDS or not parts[2].endswith(".json"):
        raise ValueError(f"golden file outside the layout <kind>/<name>/<case>.json: {golden}")
    return f"{parts[0]}/{parts[1]}.json"


def golden_files(golden_dir: Path = GOLDEN) -> list[Path]:
    return sorted(p for p in golden_dir.rglob("*.json") if p.name != MANIFEST)


def _text(doc: Any) -> str:
    return json.dumps(doc, indent=2, ensure_ascii=False) + "\n"


def _canonical(doc: Any) -> str:
    """A form in which ``True``, ``1`` and ``1.0`` differ, and key order does not matter."""
    return json.dumps(doc, sort_keys=True, ensure_ascii=False)


def stray_files(golden_dir: Path = GOLDEN) -> list[Path]:
    """Files under the golden tree that are not ``<name>.json`` (the manifest aside)."""
    return sorted(
        p
        for p in golden_dir.rglob("*")
        if p.is_file()
        and not (p.parent == golden_dir and p.name == MANIFEST)
        and not (NAME.fullmatch(p.stem) and p.suffix == ".json")
    )


def check_tree(
    schemas: Path = SCHEMAS,
    golden_dir: Path = GOLDEN,
    pending: dict[str, int] | None = None,
) -> list[str]:
    """Every problem found in a schema and golden tree (empty when it is consistent)."""
    pending = PENDING if pending is None else pending
    problems: list[str] = []
    have: set[str] = set()
    for stray in stray_files(golden_dir):
        problems.append(f"{stray.relative_to(golden_dir)}: not a golden file name (<name>.json)")
    for path in golden_files(golden_dir):
        try:
            rel = schema_for(path, golden_dir)
        except ValueError as exc:
            problems.append(str(exc))
            continue
        if not (schemas / rel).is_file():
            problems.append(f"{path.relative_to(golden_dir)}: no schema {rel}")
            continue
        have.add(rel)
        try:
            doc = _load(path)
        except ValueError as exc:
            problems.append(f"{path.relative_to(golden_dir)}: not JSON ({exc})")
            continue
        errors = sorted(validator(rel, schemas).iter_errors(doc), key=lambda e: list(e.path))
        for err in errors[:3]:
            where = "/".join(str(p) for p in err.path) or "(root)"
            problems.append(
                f"{path.relative_to(golden_dir)}: fails {rel} at {where}: {err.message[:200]}"
            )
    schemas_all = set(schema_files(schemas))
    for rel in sorted(schemas_all - have - set(pending)):
        problems.append(f"{rel}: no golden file and not listed in PENDING")
    for rel in sorted(have & set(pending)):
        problems.append(f"{rel}: has a golden file; remove it from PENDING")
    for rel in sorted(set(pending) - schemas_all):
        problems.append(f"{rel}: listed in PENDING but no such schema")
    manifest_path = golden_dir / MANIFEST
    recorded: dict[str, str] = _load(manifest_path) if manifest_path.is_file() else {}
    for rel in sorted(have):
        now = digest(rel, schemas)
        if rel not in recorded:
            problems.append(f"{rel}: not in {MANIFEST}; run with {UPDATE_ENV}=1")
        elif recorded[rel] != now:
            problems.append(
                f"{rel}: the schema (or one it references) changed since its goldens were last "
                f"reviewed; update the golden files and run with {UPDATE_ENV}=1"
            )
    for rel in sorted(set(recorded) - have):
        problems.append(f"{rel}: in {MANIFEST} but has no golden file")
    return problems


def write_manifest(schemas: Path = SCHEMAS, golden_dir: Path = GOLDEN) -> None:
    """Record the digest of every schema that has a golden: a full refresh after a review.

    ``assert_golden`` never calls this; update mode refreshes one entry (``update_manifest``).
    """
    have = sorted({schema_for(p, golden_dir) for p in golden_files(golden_dir)})
    manifest = {rel: digest(rel, schemas) for rel in have}
    (golden_dir / MANIFEST).write_text(_text(manifest), encoding="utf-8")


def update_manifest(rel: str, schemas: Path = SCHEMAS, golden_dir: Path = GOLDEN) -> None:
    """Refresh only the digest of ``rel``; every other entry keeps its recorded value."""
    path = golden_dir / MANIFEST
    manifest: dict[str, str] = _load(path) if path.is_file() else {}
    manifest[rel] = digest(rel, schemas)
    path.write_text(_text(dict(sorted(manifest.items()))), encoding="utf-8")


def assert_golden(
    kind: str,
    name: str,
    case: str,
    actual: Any,
    *,
    schemas: Path = SCHEMAS,
    golden_dir: Path = GOLDEN,
) -> None:
    """The document satisfies the schema and equals ``golden/<kind>/<name>/<case>.json``."""
    rel = f"{kind}/{name}.json"
    if (
        kind not in KINDS
        or not re.fullmatch(r"[a-z0-9-]+", name)
        or not re.fullmatch(r"[a-z0-9-]+", case)
    ):
        raise ValueError(f"bad golden name {kind}/{name}/{case}")
    # Round-trip so a non-JSON value (a tuple, a set) cannot pass for its JSON form.
    doc = json.loads(json.dumps(actual))
    validator(rel, schemas).validate(doc)
    path = golden_dir / kind / name / f"{case}.json"
    if updating():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(_text(doc), encoding="utf-8")
        update_manifest(rel, schemas, golden_dir)
        return
    if not path.is_file():
        raise AssertionError(f"missing golden file {path}; create it with {UPDATE_ENV}=1")
    expected = _load(path)
    assert _canonical(doc) == _canonical(expected), (
        f"output differs from {path.relative_to(golden_dir.parent)}"
    )
