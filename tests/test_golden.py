"""The golden-file harness and the goldens that exist so far (issue #10)."""

from __future__ import annotations

import json
import shutil
from pathlib import Path
from typing import Any

import pytest
from jsonschema import ValidationError

from tests import golden
from ticketctl.errors import TicketctlError

# --- the goldens of this repository ---------------------------------------------------------


def test_the_repository_tree_is_consistent() -> None:
    assert golden.check_tree() == []


def test_every_golden_file_validates_against_its_schema() -> None:
    for path in golden.golden_files():
        rel = golden.schema_for(path)
        golden.validator(rel).validate(json.loads(path.read_text(encoding="utf-8")))


ERRORS: dict[str, TicketctlError] = {
    "rate-limited": TicketctlError(
        "rate_limited",
        "the provider is rate limiting this client",
        site="main",
        retry_after_s=30,
        details={"rate_limit": {"limit": 1000, "remaining": 0}},
    ),
    "not-found": TicketctlError(
        "not_found", "the ticket is missing or you have no permission", site="main", key="DEMO-1"
    ),
    "usage": TicketctlError("usage", "--to is required", reason="missing_option"),
    "outcome-unknown": TicketctlError(
        "outcome_unknown",
        "the connection dropped after the request was sent",
        site="main",
        key="DEMO-2",
        details={"attempt_id": "a" * 26},
    ),
    "provider-text": TicketctlError(
        "rejected",
        "the provider refused the write",
        site="main",
        key="DEMO-1",
        details={"errorMessages": ["Assignee is required to progress this issue."]},
    ),
}


@pytest.mark.parametrize("case", sorted(ERRORS))
def test_error_envelope_goldens(case: str) -> None:
    golden.assert_golden("cli", "error", case, ERRORS[case].envelope())


# --- the harness catches what it promises -----------------------------------------------------


@pytest.fixture
def trees(tmp_path: Path) -> tuple[Path, Path]:
    schemas = tmp_path / "schemas"
    shutil.copytree(golden.SCHEMAS, schemas)
    goldens = tmp_path / "golden"
    (goldens / "cli" / "error").mkdir(parents=True)
    shutil.copy(golden.GOLDEN / "cli" / "error" / "not-found.json", goldens / "cli" / "error")
    golden.write_manifest(schemas, goldens)
    return schemas, goldens


def pending_for(schemas: Path) -> dict[str, int]:
    return {rel: 1 for rel in golden.schema_files(schemas) if rel != "cli/error.json"}


def test_a_consistent_tree_has_no_problems(trees: tuple[Path, Path]) -> None:
    schemas, goldens = trees
    assert golden.check_tree(schemas, goldens, pending_for(schemas)) == []


def test_a_golden_that_fails_its_schema_is_a_problem(trees: tuple[Path, Path]) -> None:
    schemas, goldens = trees
    path = goldens / "cli" / "error" / "not-found.json"
    doc: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
    doc["error"]["retryable"] = "yes"
    path.write_text(json.dumps(doc), encoding="utf-8")
    problems = golden.check_tree(schemas, goldens, pending_for(schemas))
    assert any("fails cli/error.json" in p for p in problems)


def test_a_golden_with_an_extra_member_is_a_problem(trees: tuple[Path, Path]) -> None:
    schemas, goldens = trees
    path = goldens / "cli" / "error" / "not-found.json"
    doc = json.loads(path.read_text(encoding="utf-8"))
    doc["extra"] = 1
    path.write_text(json.dumps(doc), encoding="utf-8")
    assert golden.check_tree(schemas, goldens, pending_for(schemas))


def test_a_schema_change_without_a_golden_update_is_a_problem(trees: tuple[Path, Path]) -> None:
    schemas, goldens = trees
    path = schemas / "cli" / "error.json"
    doc = json.loads(path.read_text(encoding="utf-8"))
    doc["description"] = "changed, and still valid for the golden"
    path.write_text(json.dumps(doc), encoding="utf-8")
    problems = golden.check_tree(schemas, goldens, pending_for(schemas))
    assert any("changed since its goldens were last reviewed" in p for p in problems)
    golden.write_manifest(schemas, goldens)
    assert golden.check_tree(schemas, goldens, pending_for(schemas)) == []


def test_a_change_in_a_referenced_file_is_a_problem(trees: tuple[Path, Path]) -> None:
    schemas, goldens = trees
    path = schemas / "defs.json"
    doc = json.loads(path.read_text(encoding="utf-8"))
    doc["$defs"]["site_name"]["description"] = "changed"
    path.write_text(json.dumps(doc), encoding="utf-8")
    assert any(
        "changed since" in p for p in golden.check_tree(schemas, goldens, pending_for(schemas))
    )


def test_formatting_alone_is_not_a_schema_change(trees: tuple[Path, Path]) -> None:
    schemas, goldens = trees
    path = schemas / "cli" / "error.json"
    path.write_text(json.dumps(json.loads(path.read_text(encoding="utf-8"))), encoding="utf-8")
    assert golden.check_tree(schemas, goldens, pending_for(schemas)) == []


def test_a_schema_with_no_golden_and_no_pending_entry_is_a_problem(
    trees: tuple[Path, Path],
) -> None:
    schemas, goldens = trees
    pending = pending_for(schemas)
    del pending["cli/get.json"]
    assert any(
        "cli/get.json: no golden file" in p for p in golden.check_tree(schemas, goldens, pending)
    )


def test_a_stale_or_unknown_pending_entry_is_a_problem(trees: tuple[Path, Path]) -> None:
    schemas, goldens = trees
    pending = pending_for(schemas) | {"cli/error.json": 5, "cli/nosuch.json": 5}
    problems = golden.check_tree(schemas, goldens, pending)
    assert any("remove it from PENDING" in p for p in problems)
    assert any("no such schema" in p for p in problems)


def test_a_golden_outside_the_layout_or_without_a_schema_is_a_problem(
    trees: tuple[Path, Path],
) -> None:
    schemas, goldens = trees
    (goldens / "cli" / "nosuch").mkdir()
    (goldens / "cli" / "nosuch" / "x.json").write_text("{}", encoding="utf-8")
    (goldens / "stray.json").write_text("{}", encoding="utf-8")
    problems = golden.check_tree(schemas, goldens, pending_for(schemas))
    assert any("no schema cli/nosuch.json" in p for p in problems)
    assert any("outside the layout" in p for p in problems)


def test_a_manifest_entry_without_a_golden_is_a_problem(trees: tuple[Path, Path]) -> None:
    schemas, goldens = trees
    manifest = goldens / golden.MANIFEST
    doc = json.loads(manifest.read_text(encoding="utf-8"))
    doc["cli/get.json"] = "0" * 64
    manifest.write_text(json.dumps(doc), encoding="utf-8")
    assert any(
        "has no golden file" in p for p in golden.check_tree(schemas, goldens, pending_for(schemas))
    )


def test_assert_golden_rejects_output_that_violates_the_schema(trees: tuple[Path, Path]) -> None:
    schemas, goldens = trees
    with pytest.raises(ValidationError):
        golden.assert_golden(
            "cli", "error", "not-found", {"error": 1}, schemas=schemas, golden_dir=goldens
        )


def test_assert_golden_rejects_a_changed_output(trees: tuple[Path, Path]) -> None:
    schemas, goldens = trees
    envelope = ERRORS["not-found"].envelope()
    golden.assert_golden("cli", "error", "not-found", envelope, schemas=schemas, golden_dir=goldens)
    envelope["error"]["message"] = "a different message"
    with pytest.raises(AssertionError, match="differs"):
        golden.assert_golden(
            "cli", "error", "not-found", envelope, schemas=schemas, golden_dir=goldens
        )


def test_assert_golden_names_a_missing_golden_file(trees: tuple[Path, Path]) -> None:
    schemas, goldens = trees
    with pytest.raises(AssertionError, match="missing golden file"):
        golden.assert_golden(
            "cli",
            "error",
            "no-such-case",
            ERRORS["usage"].envelope(),
            schemas=schemas,
            golden_dir=goldens,
        )


def test_update_mode_writes_the_file_and_the_manifest(
    trees: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    schemas, goldens = trees
    monkeypatch.setenv(golden.UPDATE_ENV, "1")
    golden.assert_golden(
        "cli", "error", "usage", ERRORS["usage"].envelope(), schemas=schemas, golden_dir=goldens
    )
    monkeypatch.delenv(golden.UPDATE_ENV)
    assert (goldens / "cli" / "error" / "usage.json").is_file()
    assert golden.check_tree(schemas, goldens, pending_for(schemas)) == []
    golden.assert_golden(
        "cli", "error", "usage", ERRORS["usage"].envelope(), schemas=schemas, golden_dir=goldens
    )


@pytest.mark.parametrize(
    ("kind", "name", "case"),
    [("cli", "../x", "a"), ("cli", "error", "A b"), ("nope", "error", "a")],
)
def test_golden_names_are_restricted(kind: str, name: str, case: str) -> None:
    with pytest.raises(ValueError):
        golden.assert_golden(kind, name, case, {})


def test_the_pending_list_names_real_schemas_and_an_owner() -> None:
    assert set(golden.PENDING) <= set(golden.schema_files())
    assert all(isinstance(n, int) and n > 0 for n in golden.PENDING.values())


# --- review round 1: update mode, CI, formats, strict compare, stray files ------------------


def _tiny_tree(root: Path) -> tuple[Path, Path]:
    """Two golden-backed schemas (a, b) in a throwaway tree, with goldens and a fresh manifest."""
    schemas = root / "schemas"
    (schemas / "cli").mkdir(parents=True)
    for name in ("a", "b"):
        doc = {
            "$schema": "https://json-schema.org/draft/2020-12/schema",
            "type": "object",
            "properties": {"n": {}},
            "description": "first",
        }
        (schemas / "cli" / f"{name}.json").write_text(json.dumps(doc), encoding="utf-8")
    goldens = root / "golden"
    for name in ("a", "b"):
        (goldens / "cli" / name).mkdir(parents=True)
        (goldens / "cli" / name / "x.json").write_text('{"n": 1}\n', encoding="utf-8")
    golden.write_manifest(schemas, goldens)
    return schemas, goldens


def _change(schemas: Path, name: str) -> None:
    path = schemas / "cli" / f"{name}.json"
    doc = json.loads(path.read_text(encoding="utf-8"))
    doc["description"] = "second"
    path.write_text(json.dumps(doc), encoding="utf-8")


def test_updating_one_schema_does_not_bless_the_stale_digest_of_another(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    schemas, goldens = _tiny_tree(tmp_path)
    assert golden.check_tree(schemas, goldens, {}) == []
    _change(schemas, "a")
    _change(schemas, "b")
    monkeypatch.delenv("CI", raising=False)
    monkeypatch.setenv(golden.UPDATE_ENV, "1")
    golden.assert_golden("cli", "a", "x", {"n": 1}, schemas=schemas, golden_dir=goldens)
    monkeypatch.delenv(golden.UPDATE_ENV)
    problems = golden.check_tree(schemas, goldens, {})
    assert len(problems) == 1 and problems[0].startswith("cli/b.json: the schema")
    # updating the second one clears it
    monkeypatch.setenv(golden.UPDATE_ENV, "1")
    golden.assert_golden("cli", "b", "x", {"n": 1}, schemas=schemas, golden_dir=goldens)
    monkeypatch.delenv(golden.UPDATE_ENV)
    assert golden.check_tree(schemas, goldens, {}) == []


def test_update_mode_is_refused_when_ci_is_set(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(golden.UPDATE_ENV, "1")
    monkeypatch.setenv("CI", "")
    with pytest.raises(RuntimeError, match="CI"):
        golden.updating()
    monkeypatch.delenv("CI")
    assert golden.updating() is True
    monkeypatch.delenv(golden.UPDATE_ENV)
    monkeypatch.setenv("CI", "true")
    assert golden.updating() is False  # CI alone is fine: it only compares


def test_assert_golden_does_not_write_in_ci(
    trees: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    schemas, goldens = trees
    monkeypatch.setenv(golden.UPDATE_ENV, "1")
    monkeypatch.setenv("CI", "1")
    with pytest.raises(RuntimeError):
        golden.assert_golden(
            "cli", "error", "usage", ERRORS["usage"].envelope(), schemas=schemas, golden_dir=goldens
        )
    assert not (goldens / "cli" / "error" / "usage.json").exists()


def test_a_uri_format_is_enforced(tmp_path: Path) -> None:
    schemas = tmp_path / "schemas"
    (schemas / "cli").mkdir(parents=True)
    doc = {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "type": "object",
        "properties": {"url": {"type": "string", "format": "uri"}},
    }
    (schemas / "cli" / "u.json").write_text(json.dumps(doc), encoding="utf-8")
    check = golden.validator("cli/u.json", schemas)
    for good in ("https://example.test/x", "http://127.0.0.1:8/p?q=1"):
        check.validate({"url": good})
    for bad in ("not a url", "/relative/path", "https://", "ftp:/no-host", "", "http://a b/c"):
        with pytest.raises(ValidationError):
            check.validate({"url": bad})


def test_golden_comparison_is_type_strict() -> None:
    assert golden._canonical({"a": 1}) != golden._canonical({"a": True})
    assert golden._canonical({"a": 1}) != golden._canonical({"a": 1.0})
    assert golden._canonical({"a": 1, "b": 2}) == golden._canonical({"b": 2, "a": 1})


def test_assert_golden_rejects_an_integer_written_as_a_float(trees: tuple[Path, Path]) -> None:
    schemas, goldens = trees
    envelope = ERRORS["rate-limited"].envelope()
    (goldens / "cli" / "error" / "rate-limited.json").write_text(
        json.dumps(envelope), encoding="utf-8"
    )
    golden.assert_golden(
        "cli", "error", "rate-limited", envelope, schemas=schemas, golden_dir=goldens
    )
    changed = json.loads(json.dumps(envelope))
    changed["error"]["details"]["rate_limit"]["limit"] = 1000.0  # still an integer to the schema
    with pytest.raises(AssertionError, match="differs"):
        golden.assert_golden(
            "cli", "error", "rate-limited", changed, schemas=schemas, golden_dir=goldens
        )


@pytest.mark.parametrize("name", ["README.md", "x.JSON", "Upper.json", "a_b.json", "notes.txt"])
def test_a_stray_file_under_the_golden_tree_is_a_problem(
    trees: tuple[Path, Path], name: str
) -> None:
    schemas, goldens = trees
    (goldens / "cli" / "error" / name).write_text("x", encoding="utf-8")
    problems = golden.check_tree(schemas, goldens, pending_for(schemas))
    assert any(name in p and "not a golden file name" in p for p in problems)


def test_the_manifest_and_well_named_goldens_are_not_stray(trees: tuple[Path, Path]) -> None:
    _, goldens = trees
    assert golden.stray_files(goldens) == []
