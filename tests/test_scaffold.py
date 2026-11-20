from __future__ import annotations

import ast
import importlib
import tomllib
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
PKG = ROOT / "src" / "ticketctl"

# The module map of docs/plan.md and docs/design.md (Modules).
MODULES = [
    "cli",
    "config",
    "gateway",
    "sanitize",
    "audit",
    "cache",
    "provider",
    "jira",
    "adf",
    "plan",
    "writeflow",
    "hooks",
    "dedupe",
]

# Imports that open a network connection; only gateway.py may use them.
NETWORK_MODULES = {"http", "socket", "ssl", "urllib", "requests", "httpx", "aiohttp"}


@pytest.mark.parametrize("name", MODULES)
def test_module_imports(name: str) -> None:
    importlib.import_module(f"ticketctl.{name}")


def test_import_package() -> None:
    import ticketctl

    assert ticketctl.__version__


def _imported_roots(path: Path) -> set[str]:
    roots: set[str] = set()
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(node, ast.Import):
            roots.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            roots.add(node.module.split(".")[0])
    return roots


def test_only_gateway_may_use_network_modules() -> None:
    offenders = {
        p.name: sorted(_imported_roots(p) & NETWORK_MODULES)
        for p in PKG.glob("*.py")
        if p.name != "gateway.py" and _imported_roots(p) & NETWORK_MODULES
    }
    assert offenders == {}


def test_network_guard_detects_an_offender(tmp_path: Path) -> None:
    bad = tmp_path / "bad.py"
    bad.write_text("import http.client\nfrom socket import socket\n", encoding="utf-8")
    assert _imported_roots(bad) & NETWORK_MODULES == {"http", "socket"}


def test_runtime_dependencies_are_only_pyyaml_and_pydantic() -> None:
    data = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    names = {
        d.split(">")[0].split("=")[0].split("<")[0].split("[")[0].strip().lower()
        for d in data["project"]["dependencies"]
    }
    assert names == {"pyyaml", "pydantic"}
