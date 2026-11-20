"""Repository scan: nothing real in tests/ (issue #10, CLAUDE.md rules).

The scan cannot know a real site name, so it enforces the synthetic conventions instead: a host is
loopback or in a reserved test domain, a project key is on the synthetic list, an account id is not
shaped like a real one, and a custom field id is in the synthetic range or on a short reviewed list.
Anything else fails with the file and line, so new fixture data has to be added to the lists on
purpose (and reviewed).
"""

from __future__ import annotations

import re
from collections.abc import Iterator
from pathlib import Path

import pytest

TESTS = Path(__file__).resolve().parent
SELF = Path(__file__).resolve()

# Reserved for documentation and tests (RFC 2606, RFC 6761) and loopback.
HOST_OK = re.compile(
    r"^(localhost|127\.\d+\.\d+\.\d+|0\.0\.0\.0|::\d?|bad|10\.0\.0\.\d+|"
    r"(.+\.)?(test|example|invalid|localhost)|(.+\.)?example\.(com|net|org))$",
    re.I,
)
# Project key prefixes the fixtures may use. Add one here only when it is invented.
PROJECTS = {"DEMO", "AB", "ABC", "BC", "C9", "Z9", "SENTINEL", "TEST", "FAKE"}
# Tokens shaped like a key that are not one (a hyphenated standard or constant).
NOT_KEYS = {"UTF", "SHA", "ISO", "ASCII", "IPV", "RFC", "HTTP", "TLS", "SSL", "CP", "LATIN"}
# Synthetic custom field ids: the fake's range, plus ids already used by other test files.
FIELD_OK = re.compile(r"customfield_(9\d{4}|1|10001|10042)\b")

# Account-id-shaped values already used as grammar test inputs; reviewed, not real accounts.
ACCOUNT_OK = {"5b10ac8d82e05b22cc7d4ef5"}

URL = re.compile(r"https?://([^/\s\"'\\)>]+)", re.I)
KEY = re.compile(r"(?<![A-Za-z0-9_])([A-Z][A-Z0-9]{1,9})-(\d{1,7})(?![A-Za-z0-9_-])")
FIELD = re.compile(r"customfield_[0-9]+")
# Shapes of real Atlassian account ids: 24 hex characters, or <digits>:<uuid>.
ACCOUNT = re.compile(
    r"(?<![0-9a-fA-F])[0-9a-f]{24}(?![0-9a-fA-F])|\b\d{5,7}:[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-"
    r"[0-9a-f]{4}-[0-9a-f]{12}\b"
)
CLOUD_HOSTS = re.compile(r"[A-Za-z0-9-]+\.(atlassian\.net|jira\.com|atlassian\.com)", re.I)


def files() -> Iterator[Path]:
    for path in sorted(TESTS.rglob("*")):
        if (
            path.is_file()
            and path.suffix in {".py", ".json", ".md", ".txt", ".yaml", ".yml"}
            and "__pycache__" not in path.parts
            and path.resolve() != SELF
        ):
            yield path


def scan(text: str) -> list[tuple[int, str]]:
    """The offending (line, what) pairs of one text."""
    bad: list[tuple[int, str]] = []
    for number, line in enumerate(text.splitlines(), 1):
        for m in URL.finditer(line):
            host = m.group(1).rsplit("@", 1)[-1]
            if host.startswith("["):
                host = host[1:].split("]")[0]
            else:
                host = re.match(r"[A-Za-z0-9.-]*", host).group()  # type: ignore[union-attr]
            # A single label is a broken fragment of a hostile-input test ("exa mple.test").
            if "." not in host and not host.startswith(":") and host.lower() != "localhost":
                if ":" not in host:
                    continue
            if host.lower() == "example.atlassian.net":
                continue  # the documentation spelling used by the config tests
            if not HOST_OK.match(host):
                bad.append((number, f"host {host}"))
        for m in CLOUD_HOSTS.finditer(line):
            if m.group(0).lower() != "example.atlassian.net":
                bad.append((number, f"hosted site {m.group(0)}"))
        for m in KEY.finditer(line):
            prefix = m.group(1)
            if prefix not in PROJECTS and prefix not in NOT_KEYS:
                bad.append((number, f"project key {m.group(0)}"))
        for m in FIELD.finditer(line):
            if not FIELD_OK.fullmatch(m.group(0)):
                bad.append((number, f"custom field id {m.group(0)}"))
        for m in ACCOUNT.finditer(line):
            if m.group(0) in ACCOUNT_OK:
                continue
            bad.append((number, f"account id {m.group(0)}"))
    return bad


def test_tests_hold_no_real_site_project_account_or_field() -> None:
    found = [
        f"{path.relative_to(TESTS.parent)}:{line}: {what}"
        for path in files()
        for line, what in scan(path.read_text(encoding="utf-8", errors="replace"))
    ]
    assert found == [], "non-synthetic data in tests/:\n" + "\n".join(found)


def test_the_scan_covers_python_and_json_files() -> None:
    names = {p.name for p in files()}
    assert "fake_provider.py" in names and "not-found.json" in names


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("https://acme-corp.atlassian.net/browse/DEMO-1", ["host acme-corp", "hosted site"]),
        ("see https://example.test/x and http://127.0.0.1:8080/y", []),
        ("http://user:pw@127.0.0.1:1/", []),
        ("http://[::1]:8080/", []),
        ("https://jira.internal.corp/x", ["host jira.internal.corp"]),
        ("PROJ-123 and DEMO-5 and UTF-8 and SHA-256", ["project key PROJ-123"]),
        ("customfield_10016 customfield_90001", ["custom field id customfield_10016"]),
        ("5b10a2844c20165700ede21g 5b10a2844c20165700ede21f", ["account id 5b10a2844c20165700ede21f"]),
        ("557058:f58131cb-b67d-43c7-b30d-6b58d40bd077", ["account id 557058"]),
        ("acct-0001 and a sha " + "ab" * 32, []),
    ],
)
def test_the_scan_flags_what_it_should(text: str, expected: list[str]) -> None:
    got = [what for _, what in scan(text)]
    assert len(got) == len(expected)
    for item, want in zip(got, expected, strict=True):
        assert item.startswith(want)
