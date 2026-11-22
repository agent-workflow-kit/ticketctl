"""Harness of the live suites (issues #37 and #38): environment, budget, runner, recorder, closer.

Everything a live test needs lives here so that the harness logic is proved in normal CI against the
fake provider (``tests/live/test_harness.py``) without any real site. The live tests drive the
``ticketctl`` command line as a subprocess, like a real consumer; they import nothing from the
``ticketctl`` package and make no HTTP request themselves.

Nothing real is named here: the site, the project, the tickets, the board and the probe targets all
come from the user's own ticketctl configuration and from environment variables (see
``docs/live-verification.md``). There are no defaults that point at anything.
"""

from __future__ import annotations

import json
import re
import secrets
import subprocess
import sys
import time
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

# ---- environment ---------------------------------------------------------------------------------

ENV_LIVE = "TICKETCTL_LIVE"
ENV_WRITE = "TICKETCTL_LIVE_WRITE"
ENV_PROJECT = "TICKETCTL_LIVE_PROJECT"
ENV_TICKET = "TICKETCTL_LIVE_TICKET"
ENV_MOVED = "TICKETCTL_LIVE_MOVED_TICKET"
ENV_HIDDEN = "TICKETCTL_LIVE_HIDDEN_TICKET"
ENV_MISSING = "TICKETCTL_LIVE_MISSING_TICKET"
ENV_BOARD = "TICKETCTL_LIVE_BOARD"
ENV_KANBAN = "TICKETCTL_LIVE_KANBAN_BOARD"
ENV_SITE = "TICKETCTL_LIVE_SITE"
ENV_CONFIG = "TICKETCTL_LIVE_CONFIG"
ENV_TYPE = "TICKETCTL_LIVE_TYPE"
ENV_RESOLUTION = "TICKETCTL_LIVE_RESOLUTION"
ENV_DONE_STATUS = "TICKETCTL_LIVE_DONE_STATUS"
ENV_TOKEN_ENV = "TICKETCTL_LIVE_TOKEN_ENV"
ENV_FINDINGS = "TICKETCTL_LIVE_FINDINGS_DIR"

PROJECT_RE = re.compile(r"[A-Za-z][A-Za-z0-9_]{0,31}")
KEY_RE = re.compile(r"[A-Za-z][A-Za-z0-9_]{0,31}-[0-9]{1,9}")
SITE_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}")
BOARD_RE = re.compile(r"[0-9]{1,18}")
ENV_NAME_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]{0,127}")

DEFAULT_RESOLUTION = "Won't Do"
DEFAULT_DONE_STATUS = "Done"
DEFAULT_TYPE = "Task"
MAX_TEST_TICKETS = 3
MAX_COMMENTS_PER_TICKET = 1


def skip_reason(env: Mapping[str, str], *, write: bool) -> str | None:
    """Why a live suite must not run now, else None. Pure: the caller passes the environment."""
    if env.get(ENV_LIVE) != "1":
        return f"live suites run only with {ENV_LIVE}=1 (never in CI)"
    if env.get("CI"):
        return "CI is set: the live suites never run in CI"
    if not write:
        return None
    if env.get(ENV_WRITE) != "1":
        return (
            f"the write suite needs {ENV_WRITE}=1 as well: it creates tickets and posts "
            "a comment that cannot be undone"
        )
    project = env.get(ENV_PROJECT, "")
    if not PROJECT_RE.fullmatch(project):
        return f"the write suite needs {ENV_PROJECT} naming a test project (a project key)"
    return None


@dataclass(frozen=True)
class LiveEnv:
    """The probe targets, all from the environment; empty means "not given"."""

    site: str = ""
    config: str = ""
    project: str = ""
    ticket: str = ""
    moved_ticket: str = ""
    hidden_ticket: str = ""
    missing_ticket: str = ""
    board: str = ""
    kanban_board: str = ""
    issue_type: str = DEFAULT_TYPE
    resolution: str = DEFAULT_RESOLUTION
    done_status: str = DEFAULT_DONE_STATUS
    token_env: str = ""

    @classmethod
    def from_env(cls, env: Mapping[str, str]) -> LiveEnv:
        def get(name: str, pattern: re.Pattern[str] | None = None) -> str:
            value = env.get(name, "").strip()
            if value and pattern is not None and not pattern.fullmatch(value):
                raise ValueError(f"{name} is not a valid value")
            return value

        return cls(
            site=get(ENV_SITE, SITE_RE),
            config=get(ENV_CONFIG),
            project=get(ENV_PROJECT, PROJECT_RE),
            ticket=get(ENV_TICKET, KEY_RE),
            moved_ticket=get(ENV_MOVED, KEY_RE),
            hidden_ticket=get(ENV_HIDDEN, KEY_RE),
            missing_ticket=get(ENV_MISSING, KEY_RE),
            board=get(ENV_BOARD, BOARD_RE),
            kanban_board=get(ENV_KANBAN, BOARD_RE),
            issue_type=get(ENV_TYPE) or DEFAULT_TYPE,
            resolution=get(ENV_RESOLUTION) or DEFAULT_RESOLUTION,
            done_status=get(ENV_DONE_STATUS) or DEFAULT_DONE_STATUS,
            token_env=get(ENV_TOKEN_ENV, ENV_NAME_RE),
        )

    def project_key(self) -> str:
        """The project to probe: the named one, else the one the named ticket belongs to."""
        if self.project:
            return self.project
        return self.ticket.rsplit("-", 1)[0] if self.ticket else ""

    def literals(self) -> list[str]:
        """Every value the user named: masked in findings wherever it shows up."""
        values = [
            self.site,
            self.project,
            self.ticket,
            self.moved_ticket,
            self.hidden_ticket,
            self.missing_ticket,
            self.project_key(),
            self.config,
        ]
        return [v for v in values if v]


# ---- redaction -----------------------------------------------------------------------------------

_EMAIL = re.compile(r"[A-Za-z0-9._%+'-]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)+")
_URL = re.compile(r"[A-Za-z][A-Za-z0-9+.-]*://[^\s\"'<>)\]]+")
_HOST = re.compile(
    r"(?<![A-Za-z0-9_.@/-])(?:[A-Za-z0-9-]+\.)+"
    r"(?:com|net|org|io|dev|app|cloud|corp|internal|lan|local|intranet|edu|gov|test|example)"
    r"(?![A-Za-z0-9_-])",
    re.I,
)
_TICKET = re.compile(r"(?<![A-Za-z0-9_])[A-Z][A-Z0-9_]+-\d{1,9}(?![A-Za-z0-9_-])")
_ACCOUNT = re.compile(
    r"(?<![0-9A-Fa-f])(?=[0-9a-f]*[a-f])[0-9a-f]{24}(?![0-9A-Fa-f])"
    r"|\b\d{4,8}:[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b",
    re.I,
)
_AUTH = re.compile(r"\b(?:Basic|Bearer)\s+[A-Za-z0-9._~+/=-]{6,}", re.I)
_OPAQUE = re.compile(r"(?<![A-Za-z0-9_-])[A-Za-z0-9_+/=-]{32,}(?![A-Za-z0-9_-])")
_MASK_NAME_RE = re.compile(r"TOKEN|SECRET|PASSWORD|PASSWD|API_KEY|CREDENTIAL", re.I)
MASK = "<redacted>"


def masked_env_values(env: Mapping[str, str]) -> list[str]:
    """Values of the environment variables whose names look like credentials (never printed)."""
    return [v for k, v in env.items() if _MASK_NAME_RE.search(k) and len(v) >= 8]


class Redactor:
    """Masks what must never reach a findings file: credentials, emails, hosts, keys, accounts."""

    def __init__(self, literals: Iterable[str] = ()) -> None:
        self._literals = sorted({v for v in literals if len(v) >= 3}, key=len, reverse=True)

    def text(self, value: str) -> str:
        for literal in self._literals:
            value = value.replace(literal, MASK)
        value = _AUTH.sub(MASK, value)
        value = _URL.sub("<url>", value)
        value = _EMAIL.sub("<email>", value)
        value = _HOST.sub("<host>", value)
        value = _TICKET.sub("<KEY>", value)
        value = _ACCOUNT.sub("<account>", value)
        return _OPAQUE.sub("<opaque>", value)

    def any(self, value: Any) -> Any:
        if isinstance(value, str):
            return self.text(value)
        if isinstance(value, Mapping):
            return {self.text(str(k)): self.any(v) for k, v in value.items()}
        if isinstance(value, list | tuple | set | frozenset):
            items = sorted(value, key=str) if isinstance(value, set | frozenset) else value
            return [self.any(v) for v in items]
        if value is None or isinstance(value, bool | int | float):
            return value
        return self.text(str(value))


def shape(value: Any, *, depth: int = 4) -> Any:
    """A value reduced to its structure: types for leaves, no text, so it cannot leak data."""
    if depth <= 0:
        return "..."
    if isinstance(value, Mapping):
        return {str(k): shape(v, depth=depth - 1) for k, v in sorted(value.items())}
    if isinstance(value, list):
        return [shape(value[0], depth=depth - 1)] if value else []
    if value is None:
        return "null"
    return type(value).__name__


# ---- findings ------------------------------------------------------------------------------------

STATUSES = ("observed", "differs", "skipped", "error")


@dataclass
class Finding:
    fact: str  # a checklist id (see tests/live/checklist.py)
    title: str
    status: str
    observed: Any
    expected: str = ""


@dataclass
class Recorder:
    """Collects findings, redacts them and writes a dated file under a git-ignored directory."""

    suite: str
    directory: Path
    redactor: Redactor
    clock: Callable[[], datetime] = lambda: datetime.now(UTC)
    findings: list[Finding] = field(default_factory=list)
    requests: int = 0

    def record(
        self, fact: str, title: str, observed: Any, *, status: str = "observed", expected: str = ""
    ) -> None:
        if status not in STATUSES:
            raise ValueError(f"unknown finding status {status!r}")
        self.findings.append(Finding(fact, title, status, observed, expected))

    def differs(self, fact: str, title: str, observed: Any, expected: str) -> None:
        """The real behavior is not what the fake provider (and the code) assumed."""
        self.record(fact, title, observed, status="differs", expected=expected)

    def document(self) -> dict[str, Any]:
        now = self.clock()
        doc = {
            "suite": self.suite,
            "date": now.strftime("%Y-%m-%d"),
            "time": now.strftime("%H:%M:%SZ"),
            "requests": self.requests,
            "note": (
                "observations of one run, not verified facts: a human reviews each one and "
                "records it in docs/design.md (Live verification) as a dated fact"
            ),
            "findings": [vars(f).copy() for f in self.findings],
        }
        return self.redactor.any(doc)

    def markdown(self, doc: dict[str, Any]) -> str:
        lines = [
            f"# Live findings: {doc['suite']} suite, {doc['date']}",
            "",
            doc["note"],
            "",
            f"Requests counted from the request log: {doc['requests']}.",
            "",
        ]
        for f in doc["findings"]:
            lines.append(f"## {f['fact']}: {f['title']} ({f['status']})")
            lines.append("")
            if f["expected"]:
                lines.append(f"Expected (fake provider and code): {f['expected']}")
                lines.append("")
            lines += ["```json", json.dumps(f["observed"], indent=2, sort_keys=True), "```", ""]
        return "\n".join(lines)

    def summary(self) -> str:
        counts = {s: sum(1 for f in self.findings if f.status == s) for s in STATUSES}
        head = f"live {self.suite} findings: " + ", ".join(f"{n} {s}" for s, n in counts.items())
        rows = [f"  [{f.status}] {f.fact}: {self.redactor.text(f.title)}" for f in self.findings]
        return "\n".join([head, *rows])

    def write(self) -> Path:
        doc = self.document()
        self.directory.mkdir(parents=True, exist_ok=True)
        stem = f"findings-{self.suite}-{self.clock().strftime('%Y%m%dT%H%M%SZ')}"
        path = self.directory / f"{stem}.json"
        path.write_text(json.dumps(doc, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        (self.directory / f"{stem}.md").write_text(self.markdown(doc) + "\n", encoding="utf-8")
        for p in (path, path.with_suffix(".md")):
            p.chmod(0o600)
        return path


def default_findings_dir(env: Mapping[str, str]) -> Path:
    """``TICKETCTL_LIVE_FINDINGS_DIR``, else ``live-findings`` at the repository root (ignored)."""
    given = env.get(ENV_FINDINGS)
    return Path(given) if given else Path(__file__).resolve().parents[2] / "live-findings"


# ---- request budget ------------------------------------------------------------------------------


class BudgetExceeded(RuntimeError):
    """The request cap was reached: the suite stops (cleanup still runs, within its reserve)."""


class Budget:
    """Counts the provider requests ticketctl made, from its own request log.

    ``ticketctl`` writes one ``log.jsonl`` line per provider call (method, path template, status,
    ``retries``) under ``$XDG_STATE_HOME/ticketctl``. The live suites give every run a private state
    directory, so the log holds exactly this run's calls: a line counts ``1 + retries`` requests.
    The suite stops before a command when the limit is reached and after a command that went over
    it. Cleanup commands are allowed a reserve on top of the limit so test tickets are still closed.
    """

    def __init__(self, state_home: Path, limit: int, reserve: int = 20) -> None:
        self.log = state_home / "ticketctl" / "log.jsonl"
        self.limit = limit
        self.reserve = reserve
        self.aborted = False

    def records(self) -> list[dict[str, Any]]:
        try:
            text = self.log.read_text(encoding="utf-8")
        except FileNotFoundError:
            return []
        out: list[dict[str, Any]] = []
        for line in text.splitlines():
            try:
                rec = json.loads(line)
            except ValueError:
                rec = {}
            out.append(rec if isinstance(rec, dict) else {})
        return out

    @staticmethod
    def weight(rec: Mapping[str, Any]) -> int:
        retries = rec.get("retries")
        return 1 + (retries if isinstance(retries, int) and retries > 0 else 0)

    def used(self) -> int:
        return sum(self.weight(r) for r in self.records())

    def check_before(self, *, cleanup: bool = False) -> None:
        ceiling = self.limit + (self.reserve if cleanup else 0)
        used = self.used()
        if used >= ceiling:
            self.aborted = True
            raise BudgetExceeded(f"request budget reached before a command: {used} of {ceiling}")

    def check_after(self, *, cleanup: bool = False) -> None:
        ceiling = self.limit + (self.reserve if cleanup else 0)
        used = self.used()
        if used > ceiling:
            self.aborted = True
            raise BudgetExceeded(f"request budget exceeded: {used} requests, cap {ceiling}")
        if used >= self.limit and not cleanup:
            self.aborted = True  # the suite is out of requests: only cleanup may continue


# ---- the command runner --------------------------------------------------------------------------


# POST endpoints that only read (search and the JQL parse check): not writes.
READ_POSTS = ("/search/jql", "/search/approximate-count", "/jql/parse")


def is_write(rec: Mapping[str, Any]) -> bool:
    if rec.get("method") not in ("POST", "PUT", "DELETE"):
        return False
    path = str(rec.get("path", ""))
    return not (rec.get("method") == "POST" and path.endswith(READ_POSTS))


@dataclass
class Result:
    argv: tuple[str, ...]
    code: int
    doc: Any
    stdout: str
    stderr: str
    seconds: float
    calls: list[dict[str, Any]]  # the request-log records this command added

    @property
    def requests(self) -> int:
        return sum(Budget.weight(r) for r in self.calls)

    @property
    def writes(self) -> int:
        """Provider calls that change data: a POST to a query endpoint is a read."""
        return sum(1 for r in self.calls if is_write(r))

    @property
    def error(self) -> dict[str, Any]:
        err = self.doc.get("error") if isinstance(self.doc, dict) else None
        return err if isinstance(err, dict) else {}

    @property
    def statuses(self) -> list[int | None]:
        return [r.get("status") for r in self.calls]


class LiveCli:
    """Runs ``python -m ticketctl`` with a private state directory and counts every request."""

    def __init__(
        self,
        *,
        env: Mapping[str, str],
        state_home: Path,
        budget: Budget,
        config: str = "",
        site: str = "",
        timeout: float = 180.0,
        python: str | None = None,
    ) -> None:
        self.base_env = dict(env)
        self.state_home = state_home
        self.budget = budget
        self.config = config
        self.site = site
        self.timeout = timeout
        self.python = python or sys.executable
        self.history: list[Result] = []

    def command(self, argv: Iterable[str]) -> list[str]:
        cmd = [self.python, "-m", "ticketctl"]
        if self.config:
            cmd += ["--config", self.config]
        if self.site:
            cmd += ["--site", self.site]
        return [*cmd, "--json", *argv]

    def run(
        self,
        *argv: str,
        cleanup: bool = False,
        extra_env: Mapping[str, str] | None = None,
        stdin: str | None = None,
    ) -> Result:
        self.budget.check_before(cleanup=cleanup)
        before = len(self.budget.records())
        env = {**self.base_env, "XDG_STATE_HOME": str(self.state_home), **(extra_env or {})}
        started = time.monotonic()
        done = subprocess.run(  # noqa: S603 - the interpreter running the tests, fixed module
            self.command(argv),
            env=env,
            input=stdin if stdin is not None else "",
            capture_output=True,
            text=True,
            timeout=self.timeout,
            check=False,
        )
        seconds = time.monotonic() - started
        try:
            doc = json.loads(done.stdout) if done.stdout.strip() else None
        except ValueError:
            doc = None
        result = Result(
            tuple(argv),
            done.returncode,
            doc,
            done.stdout,
            done.stderr,
            seconds,
            self.budget.records()[before:],
        )
        self.history.append(result)
        self.budget.check_after(cleanup=cleanup)
        return result

    def apply(
        self, *argv: str, pre: tuple[str, ...] = (), cleanup: bool = False
    ) -> tuple[Result, Result | None]:
        """Plan with ``--dry-run`` and, when the plan is good, apply that exact plan.

        Returns (the dry run, the apply or None). A dry run must send no write: the request log of
        the dry run is checked here, so every live write test also proves that.
        """
        plan = self.run(*pre, "--dry-run", *argv, cleanup=cleanup)
        if plan.writes:
            raise AssertionError(f"a --dry-run sent {plan.writes} write request(s): {argv[:2]}")
        digest = plan.doc.get("plan_sha256") if isinstance(plan.doc, dict) else None
        if plan.code != 0 or not isinstance(digest, str):
            return plan, None
        done = self.run(*pre, "--yes", "--expect-plan", digest, *argv, cleanup=cleanup)
        return plan, done

    def statuses_by_kind(self) -> dict[str, int]:
        """Every HTTP status seen in this run's log, counted (for the passive rate-limit finding)."""
        counts: dict[str, int] = {}
        for rec in self.budget.records():
            key = str(rec.get("status"))
            counts[key] = counts.get(key, 0) + self.budget.weight(rec)
        return counts

    def rate_limit_values(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for rec in self.budget.records():
            key = "absent" if rec.get("rate_limit_remaining") is None else "present"
            counts[key] = counts.get(key, 0) + 1
        return counts


# ---- the ledger and the closer -------------------------------------------------------------------


class TooManyTickets(RuntimeError):
    """The write suite never creates more than three test tickets."""


class TooManyComments(RuntimeError):
    """The write suite never posts more than one comment per test ticket."""


def run_marker(rng: Callable[[int], str] = secrets.token_hex) -> str:
    """A fresh id for the titles of this run's tickets (the only tickets the suite may touch)."""
    return rng(4)


TITLE_PREFIX = "ticketctl live test"


@dataclass
class Ledger:
    """The test tickets of a run: created, then always closed as done with a "won't do" resolution.

    The closer touches only keys the run itself created (recorded here the moment a create
    returned a key) and, after a create whose answer was lost, the tickets that carry this run's
    title marker (found with one search, and only those whose title starts with the marker).
    """

    cli: LiveCli
    env: LiveEnv
    marker: str
    created: list[str] = field(default_factory=list)
    names: dict[str, str] = field(default_factory=dict)  # a test's name for a ticket -> its key
    comments: dict[str, int] = field(default_factory=dict)
    unknown_create: bool = False
    closed: dict[str, str] = field(default_factory=dict)  # key -> "closed" | "already" | error
    left_open: list[str] = field(default_factory=list)

    @property
    def title_stem(self) -> str:
        return f"{TITLE_PREFIX} {self.marker}"

    def title(self, label: str) -> str:
        return f"{self.title_stem} {label}"

    def add(self, key: str, name: str = "") -> None:
        if not KEY_RE.fullmatch(key):
            raise ValueError("not a ticket key")
        if name:
            self.names[name] = key
        if key in self.created:
            return
        if len(self.created) >= MAX_TEST_TICKETS:
            self.created.append(key)  # it is this run's ticket: the closer still closes it
            raise TooManyTickets(f"more than {MAX_TEST_TICKETS} test tickets were created")
        self.created.append(key)

    def note_comment(self, key: str) -> None:
        self.comments[key] = self.comments.get(key, 0) + 1
        if self.comments[key] > MAX_COMMENTS_PER_TICKET:
            raise TooManyComments("a second comment on one test ticket is refused")

    def may_comment(self, key: str) -> bool:
        return self.comments.get(key, 0) < MAX_COMMENTS_PER_TICKET

    def _sweep(self) -> None:
        """After an unknown create: find this run's tickets by title (the only fallback search)."""
        project = self.env.project_key()
        if not project:
            return
        found = self.cli.run(
            "search", "--project", project, "--text", self.title_stem, "--fields", "title",
            "--limit", "20", cleanup=True,
        )  # fmt: skip
        tickets = found.doc.get("tickets") if isinstance(found.doc, dict) else None
        for t in tickets or []:
            if (
                isinstance(t, dict)
                and isinstance(t.get("key"), str)
                and str(t.get("title", "")).startswith(self.title_stem)
                and t["key"] not in self.created
                and KEY_RE.fullmatch(t["key"])
            ):
                self.created.append(t["key"])

    def close_one(self, key: str) -> str:
        """Close one ticket as done with the won't-do resolution; the outcome as a short word."""
        # --assign me: the provider's validator refuses a done transition for an unassigned ticket
        # coming out of the backlog (design.md, the transition order), and assigns only when empty.
        args = (
            "transition", key, "--to", self.env.done_status,
            "--resolution", self.env.resolution, "--assign", "me",
        )  # fmt: skip
        plan, done = self.cli.apply(*args, cleanup=True)
        shown = done or plan
        if done is not None and done.code == 0:
            result = done.doc.get("result") if isinstance(done.doc, dict) else None
            return "already" if result == "unchanged" else "closed"
        return f"failed: exit {shown.code} {shown.error.get('code')}"

    def close_all(self) -> dict[str, str]:
        """Close every ticket of this run, never raising: a failure is recorded and printed."""
        if self.unknown_create:
            try:
                self._sweep()
            except Exception as exc:  # noqa: BLE001 - cleanup must go on
                self.left_open.append(f"sweep failed: {type(exc).__name__}")
        for key in list(self.created):
            if key in self.closed:
                continue
            try:
                self.closed[key] = self.close_one(key)
            except Exception as exc:  # noqa: BLE001 - one failure must not stop the others
                self.closed[key] = f"failed: {type(exc).__name__}"
        return dict(self.closed)

    def still_open(self) -> list[str]:
        return [k for k, v in self.closed.items() if v.startswith("failed")] + [
            k for k in self.created if k not in self.closed
        ]
