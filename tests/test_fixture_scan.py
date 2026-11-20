"""Repository scan: nothing real in tests/ (issue #10, CLAUDE.md rules).

The scan guards against accidents, not adversarial obfuscation. It cannot know a real site name, so
it enforces the synthetic conventions instead: a host is loopback or on a short list of known
synthetic hosts, a project key is on the synthetic list, an account id is not shaped like a real
one, and a custom field id is one the fixtures actually use. Anything else fails with the file and
line, so new fixture data has to be added to the lists on purpose (and reviewed). Every non-binary
text file under tests/ is scanned, this one included; a line that holds a deliberately offending
sample carries the marker ``scan: allow`` and only that line is exempt.
"""

from __future__ import annotations

import re
from collections.abc import Iterator
from pathlib import Path

import pytest

TESTS = Path(__file__).resolve().parent
MARKER = "scan: allow"

# Hosts the fixtures may use, exact and lower case. Add one here only when it is invented.
HOSTS_OK = {
    "localhost",
    "example.test",
    "api.example.test",
    "x.example.test",
    "example.com",
    "example.net",
    "example.org",
    "example.atlassian.net",  # the documentation spelling used by the config tests
    "json-schema.org",  # the $schema URL of the schemas
    "a.test",
    "o.test",
    "p.test",
    "x.test",
    "up.test",
    "low.test",
    "other.test",
    "evil.test",
    "proxy.test",
    "-bad.test",
    "127.0.0.1.evil.test",
    "localhost.evil.test",
}
# Single-label fragments that hostile-input tests leave when a space or a non-ASCII character cuts
# a synthetic host short ("exa mple.test"); the rest of that host is on the list above.
FRAGMENTS_OK = {"a", "api", "exa", "pro", "ex", "exam", "bad"}
LOOPBACK = re.compile(r"^(127\.\d+\.\d+\.\d+|0\.0\.0\.0|::\d?|10\.0\.0\.\d+)$")
# Project key prefixes the fixtures may use. Add one here only when it is invented.
PROJECTS = {
    "DEMO",
    "AB",
    "ABC",
    "BC",
    "C9",
    "Z9",
    "SENTINEL",
    "TEST",
    "FAKE",
    "PROJECT",
    "ZZNOPE",
    "AB_C9",
}
# Whole tokens shaped like a key that are not one (a hyphenated standard or constant).
NOT_KEYS = {"UTF-8", "UTF-16", "UTF-32", "SHA-1", "SHA-256", "SHA-512", "ISO-8601", "CP-1252"}
# Synthetic custom field ids: exactly the ones the fixtures use.
FIELD_OK = re.compile(r"customfield_(1|9000[1-6])\b")

URL = re.compile(r"https?://([^/\s\"'\\)>]+)", re.I)
KEY = re.compile(r"(?<![A-Za-z0-9_])([A-Z][A-Z0-9_]+)-(\d{1,7})(?![A-Za-z0-9_-])")
PROJECT_VALUE = re.compile(
    r"""(?<![A-Za-z0-9_])["']?project["']?\s*(?:=|:|\bin\b\s*\()\s*["']?([A-Z][A-Z0-9_]+)\b"""
)
FIELD = re.compile(r"customfield_[0-9]+")
# Shapes of real Atlassian account ids: 24 hex characters with a letter among them, or
# <digits>:<uuid>.
ACCOUNT = re.compile(
    r"(?<![0-9a-f])(?=[0-9a-f]*[a-f])[0-9a-f]{24}(?![0-9a-f])"
    r"|\b\d{4,8}:[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b",
    re.I,
)
CLOUD_HOSTS = re.compile(r"[A-Za-z0-9-]+\.(atlassian\.net|jira\.com|atlassian\.com)", re.I)
# A scheme-less name that ends in a top-level domain real sites use.
BARE_DOMAIN = re.compile(
    r"(?<![A-Za-z0-9_.@/-])((?:[A-Za-z0-9-]+\.)+(?:com|net|org|io|dev|app|cloud|corp|internal|lan"
    r"|local|intranet|edu|gov))(?![A-Za-z0-9_-])",
    re.I,
)


def files(root: Path = TESTS) -> Iterator[tuple[Path, str]]:
    """Every text file under a tree (binary and non-UTF-8 files and bytecode are skipped)."""
    for path in sorted(root.rglob("*")):
        if not path.is_file() or "__pycache__" in path.parts:
            continue
        data = path.read_bytes()
        if b"\0" in data:
            continue
        try:
            yield path, data.decode("utf-8")
        except UnicodeDecodeError:
            continue


def host_ok(host: str) -> bool:
    low = host.lower()
    return low in HOSTS_OK or low in FRAGMENTS_OK or LOOPBACK.match(low) is not None


def scan(text: str) -> list[tuple[int, str]]:
    """The offending (line, what) pairs of one text."""
    bad: list[tuple[int, str]] = []
    for number, line in enumerate(text.splitlines(), 1):
        if MARKER in line:
            continue
        for m in URL.finditer(line):
            host = m.group(1).rsplit("@", 1)[-1]
            if host.startswith("["):
                host = host[1:].split("]")[0]
            else:
                host = re.match(r"[A-Za-z0-9.-]*", host).group()  # type: ignore[union-attr]
            if host.startswith(":") or host == "":
                continue  # an unfilled template such as http://{host}, or a bare port
            if not host_ok(host):
                bad.append((number, f"host {host}"))
        for m in CLOUD_HOSTS.finditer(line):
            if m.group(0).lower() not in HOSTS_OK:
                bad.append((number, f"hosted site {m.group(0)}"))
        for m in BARE_DOMAIN.finditer(line):
            if not host_ok(m.group(1)) and not m.group(1).lower().endswith(".example.test"):
                bad.append((number, f"domain {m.group(1)}"))
        for m in KEY.finditer(line):
            if m.group(1) not in PROJECTS and m.group(0) not in NOT_KEYS:
                bad.append((number, f"project key {m.group(0)}"))
        for m in PROJECT_VALUE.finditer(line):
            if m.group(1) not in PROJECTS:
                bad.append((number, f"project value {m.group(1)}"))
        for m in FIELD.finditer(line):
            if not FIELD_OK.fullmatch(m.group(0)):
                bad.append((number, f"custom field id {m.group(0)}"))
        for m in ACCOUNT.finditer(line):
            bad.append((number, f"account id {m.group(0)}"))
    return bad


def test_tests_hold_no_real_site_project_account_or_field() -> None:
    found = [
        f"{path.relative_to(TESTS.parent)}:{line}: {what}"
        for path, text in files()
        for line, what in scan(text)
    ]
    assert found == [], "non-synthetic data in tests/:\n" + "\n".join(found)


def test_the_scan_covers_every_text_file_including_itself() -> None:
    names = {p.name for p, _ in files()}
    assert {"fake_provider.py", "not-found.json", "test_fixture_scan.py", ".gitkeep"} <= names


def test_the_scan_skips_binary_and_non_utf8_files_and_ignores_extensions(tmp_path: Path) -> None:
    (tmp_path / "a.TXT").write_text("text", encoding="utf-8")
    (tmp_path / "noext").write_text("text", encoding="utf-8")
    (tmp_path / "bin.dat").write_bytes(b"a\0b")
    (tmp_path / "latin.md").write_bytes(b"caf\xe9")
    (tmp_path / "__pycache__").mkdir()
    (tmp_path / "__pycache__" / "x.py").write_text("text", encoding="utf-8")
    assert [p.name for p, _ in files(tmp_path)] == ["a.TXT", "noext"]


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        (
            "https://acme-corp.atlassian.net/browse/DEMO-1",  # scan: allow
            ["host acme-corp", "hosted site"],  # scan: allow
        ),
        ("see https://example.test/x and http://127.0.0.1:8080/y", []),
        ("http://user:pw@127.0.0.1:1/", []),
        ("http://[::1]:8080/", []),
        (
            "https://jira.internal.corp/x",  # scan: allow
            ["host jira.internal.corp"],  # scan: allow
        ),
        (
            "https://foo.example/x",  # scan: allow
            ["host foo.example"],  # scan: allow
        ),
        (
            "https://other.example.test/x",  # scan: allow
            ["host other.example.test"],  # scan: allow
        ),
        (
            "https://intranet/x",  # scan: allow
            ["host intranet"],  # scan: allow
        ),
        ("https://localhost:8080/ and http://LOCALHOST/", []),
        (
            "mail me at ops.corp or see wiki.acme.io",  # scan: allow
            ["domain ops.corp", "domain wiki.acme.io"],  # scan: allow
        ),
        ("fine: example.test api.example.test json-schema.org", []),
        (
            "PROJ-123 and DEMO-5 and UTF-8 and SHA-256",  # scan: allow
            ["project key PROJ-123"],  # scan: allow
        ),
        (
            "MY_PROJ-7 is flagged too",  # scan: allow
            ["project key MY_PROJ-7"],  # scan: allow
        ),
        (
            "project = XYZ and project in (DEMO, ABC)",  # scan: allow
            ["project value XYZ"],  # scan: allow
        ),
        (
            '{"project": "XYZ"} and project: QQQ',  # scan: allow
            ["project value XYZ", "project value QQQ"],  # scan: allow
        ),
        ("project = PROJECT and project = fields", []),
        (
            "customfield_10016 customfield_90001",  # scan: allow
            ["custom field id customfield_10016"],  # scan: allow
        ),
        (
            "customfield_10001 customfield_90007",  # scan: allow
            ["custom field id", "custom field id"],  # scan: allow
        ),
        (
            "5b10a2844c20165700ede21g 5b10a2844c20165700ede21f",  # scan: allow
            ["account id 5b10a2844c20165700ede21f"],  # scan: allow
        ),
        (
            "5B10A2844C20165700EDE21F",  # scan: allow
            ["account id"],  # scan: allow
        ),
        (
            "557058:f58131cb-b67d-43c7-b30d-6b58d40bd077",  # scan: allow
            ["account id 557058"],  # scan: allow
        ),
        (
            "1234:f58131cb-b67d-43c7-b30d-6b58d40bd077",  # scan: allow
            ["account id 1234"],  # scan: allow
        ),
        ("acct-0001 and a sha " + "ab" * 32 + " and 000000000000000000000001", []),
    ],
)
def test_the_scan_flags_what_it_should(text: str, expected: list[str]) -> None:
    got = [what for _, what in scan(text)]
    assert len(got) == len(expected), got
    for item, want in zip(got, expected, strict=True):
        assert item.startswith(want)


def test_the_marker_exempts_its_own_line_only() -> None:
    first = "https:" + "//acme-corp" + ".atlassian" + ".net/x"
    sample = first + "\n" + first + "  # " + MARKER + "\n"
    assert [line for line, _ in scan(sample)] == [1, 1]
