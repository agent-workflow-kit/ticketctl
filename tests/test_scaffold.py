from __future__ import annotations

import ast
import importlib
import importlib.metadata
import re
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
    "search_compile",
    "audit",
    "state",
    "cache",
    "provider",
    "jira",
    "fieldmap",
    "fieldload",
    "fieldconv",
    "timestamps",
    "normalize",
    "adf_render",
    "adf_from_markdown",
    "adf_validate",
    "attribution",
    "plan",
    "writeflow",
    "hooks",
    "dedupe",
    "setspec",
    "setconvert",
]

# Modules that open a connection or start a process; only gateway.py may use them.
DENIED_MODULES = {
    "http",
    "socket",
    "ssl",
    "urllib",
    "requests",
    "httpx",
    "aiohttp",
    "asyncio",
    "selectors",
    "smtplib",
    "ftplib",
    "imaplib",
    "poplib",
    "nntplib",
    "telnetlib",
    "xmlrpc",
    "socketserver",
    "urllib3",
    "websockets",
    "webbrowser",
    "multiprocessing",
    "subprocess",
}
OS_PROCESS_NAMES = ("system", "popen", "exec", "spawn", "posix_spawn")
GATEWAY = PKG / "gateway.py"


@pytest.mark.parametrize("name", MODULES)
def test_module_imports(name: str) -> None:
    importlib.import_module(f"ticketctl.{name}")


def test_import_package() -> None:
    import ticketctl

    assert ticketctl.__version__ == importlib.metadata.version("ticketctl")


def test_version_falls_back_when_metadata_is_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    import ticketctl

    def missing(name: str) -> str:
        raise importlib.metadata.PackageNotFoundError(name)

    monkeypatch.setattr(importlib.metadata, "version", missing)
    try:
        importlib.reload(ticketctl)
        assert ticketctl.__version__ == "0+unknown"
    finally:
        monkeypatch.undo()
        importlib.reload(ticketctl)
    assert ticketctl.__version__ == importlib.metadata.version("ticketctl")


def _is_os_process(name: str) -> bool:
    return name.startswith(OS_PROCESS_NAMES)


def _denied(dotted: str) -> bool:
    # urllib.parse is pure string handling (no connection); the rest of urllib is not.
    if dotted == "urllib.parse" or dotted.startswith("urllib.parse."):
        return False
    return dotted.split(".")[0] in DENIED_MODULES


def _violations(source: str) -> set[str]:
    found: set[str] = set()
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import):
            for alias in node.names:
                root = alias.name.split(".")[0]
                if _denied(alias.name):
                    found.add(f"import {root}")
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            root = node.module.split(".")[0]
            if _denied(node.module):
                found.add(f"import {root}")
            if root == "os":
                found.update(f"os.{a.name}" for a in node.names if _is_os_process(a.name))
        elif (
            isinstance(node, ast.Attribute)
            and isinstance(node.value, ast.Name)
            and node.value.id == "os"
            and _is_os_process(node.attr)
        ):
            found.add(f"os.{node.attr}")
        elif isinstance(node, ast.Call):
            func = node.func
            if isinstance(func, ast.Name) and func.id in {"__import__", "import_module"}:
                found.add(f"call {func.id}")
            elif isinstance(func, ast.Attribute) and func.attr == "import_module":
                found.add("call import_module")
    return found


def _scanned() -> list[Path]:
    return sorted(p for p in PKG.rglob("*.py") if p != GATEWAY)


def _offenders(files: list[Path], base: Path) -> dict[str, list[str]]:
    return {
        p.relative_to(base).as_posix(): sorted(v)
        for p in files
        if (v := _violations(p.read_text(encoding="utf-8")))
    }


def test_guard_scans_the_whole_package() -> None:
    names = {p.relative_to(PKG).as_posix() for p in _scanned()}
    assert names
    assert GATEWAY.exists()
    assert {f"{m}.py" for m in MODULES if m != "gateway"} <= names


def test_only_gateway_may_use_network_or_process_apis() -> None:
    assert _offenders(_scanned(), PKG) == {}


@pytest.mark.parametrize(
    ("source", "expected"),
    [
        ("import http.client", {"import http"}),
        ("from socket import socket", {"import socket"}),
        ("import subprocess as sp", {"import subprocess"}),
        ("import asyncio", {"import asyncio"}),
        ("try:\n    import requests\nexcept ImportError:\n    pass", {"import requests"}),
        ("def f():\n    import urllib.request", {"import urllib"}),
        ("import urllib.parse\nfrom urllib.parse import quote", set()),
        ("from urllib import parse", {"import urllib"}),
        ("from urllib.request import urlopen", {"import urllib"}),
        ("if True:\n    from xmlrpc.client import ServerProxy", {"import xmlrpc"}),
        ("__import__('socket')", {"call __import__"}),
        ("import importlib\nimportlib.import_module('x')", {"call import_module"}),
        ("from importlib import import_module\nimport_module('x')", {"call import_module"}),
        ("import os\nos.system('x')", {"os.system"}),
        ("import os\nos.execvp('x', [])", {"os.execvp"}),
        ("from os import system", {"os.system"}),
        ("from os import popen, getcwd", {"os.popen"}),
    ],
)
def test_guard_detects_forms(source: str, expected: set[str]) -> None:
    assert _violations(source) == expected


def test_guard_ignores_harmless_code() -> None:
    assert _violations("import os, importlib, builtins\nos.getcwd()\n") == set()


def test_guard_flags_subpackage_files(tmp_path: Path) -> None:
    (tmp_path / "sub" / "deep").mkdir(parents=True)
    (tmp_path / "sub" / "deep" / "probe.py").write_text("import socket\n", encoding="utf-8")
    files = sorted(tmp_path.rglob("*.py"))
    assert _offenders(files, tmp_path) == {"sub/deep/probe.py": ["import socket"]}


def _pyproject() -> dict:
    return tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))


def test_runtime_dependencies_are_only_pyyaml_and_pydantic() -> None:
    project = _pyproject()["project"]
    names = set()
    for requirement in project["dependencies"]:
        match = re.match(r"^[A-Za-z0-9._-]+", requirement.strip())
        assert match, requirement
        names.add(match.group(0).lower())
    assert names == {"pyyaml", "pydantic"}
    assert "optional-dependencies" not in project
