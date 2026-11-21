"""Stub write actions for the engine tests (issue #23): a transition, a comment and a description
update against the fake provider. They stand in for the commands of #28 to #31; the engine knows no
action, so the stubs prove it.

Everything is synthetic. The stubs implement only what the engine's contract needs: idempotency
first in ``reduce``, ``--if-status`` in ``check``, hashed intent and facts, and steps that call the
fake through the guarded gateway.
"""

from __future__ import annotations

import hashlib
from collections.abc import Callable
from typing import Any

from tests.fake_provider import TRANSITIONS
from ticketctl import attribution, dedupe
from ticketctl.errors import TicketctlError
from ticketctl.plan import Planned, Step
from ticketctl.sanitize import query, segment
from ticketctl.writeflow import (
    Context,
    PrepareContext,
    StepResult,
    WriteGateway,
    body_info,
    raise_for_write,
    rich_changed,
)

TRANSITION_IDS = {name: tid for tid, (_n, name) in TRANSITIONS.items()}


def _issue_path(key: str) -> str:
    return f"/rest/api/3/issue/{segment('ticket_key', key)}"


def read_issue(ctx: Context, key: str, fields: str) -> dict[str, Any]:
    path = _issue_path(key)
    res = ctx.gw.request(
        "GET", path, template="/rest/api/3/issue/{key}", query=query({"fields": fields})
    )
    assert isinstance(res.data, dict)
    return res.data["fields"]


class StubTransition:
    """``transition KEY --to <status> [--assign me] [--if-status s]``."""

    name = "transition"
    yes_needs_dedupe_key = False
    dedupe_key: str | None = None

    def __init__(
        self,
        key: str,
        to: str,
        *,
        assign_me: bool = False,
        if_status: str | None = None,
        transition_id: str | None = None,
    ) -> None:
        self.key = key
        self.to = to
        self.assign_me = assign_me
        self.if_status = if_status
        self.transition_id = transition_id or TRANSITION_IDS.get(to, "99")
        self.prepared = False
        self.checked = 0

    def prepare(self, pc: PrepareContext) -> None:
        self.prepared = True

    def reduce(self, ctx: Context, *, fresh: bool) -> Planned:
        fields = read_issue(ctx, self.key, "status,assignee")
        status = fields["status"]["name"]
        assignee = (fields.get("assignee") or {}).get("accountId")
        steps: list[Step] = []
        wants_assign = self.assign_me and assignee is None
        if wants_assign:
            steps.append(self._assign(ctx.me.account_id))
        if status.lower() != self.to.lower():
            steps.append(self._transition())
        # The hash facts: the status, and the assignee only when it is another account.
        other = assignee if assignee not in (None, ctx.me.account_id) else None
        facts: dict[str, Any] = {"status": status.lower(), "assignee": other}
        return Planned(
            steps=steps,
            intent={
                "to": self.to.lower(),
                "transition_id": self.transition_id,
                "assign": "me" if self.assign_me else None,
            },
            facts=facts,
            changes=[
                {
                    "name": "status",
                    "from": {"name": status, "category": None},
                    "to": {"name": self.to, "category": None},
                }
            ],
            key=self.key,
            warnings=["assign_added"] if wants_assign else [],
        )

    def check(self, ctx: Context, planned: Planned) -> None:
        self.checked += 1
        if self.if_status is not None:
            current = planned.facts["status"]
            if current != self.if_status.lower():
                raise TicketctlError(
                    "precondition_failed",
                    "the ticket is not in the expected status",
                    details={"current_status": current, "expected_status": self.if_status},
                )

    def _assign(self, account: str) -> Step:
        def send(gw: WriteGateway) -> StepResult:
            raise_for_write(
                gw.request(
                    "PUT",
                    f"{_issue_path(self.key)}/assignee",
                    template="/rest/api/3/issue/{key}/assignee",
                    body={"accountId": account},
                ),
                object_path=True,
            )
            return StepResult([])

        return Step("assign", send, {"account": "me"})

    def _transition(self) -> Step:
        def send(gw: WriteGateway) -> StepResult:
            raise_for_write(
                gw.request(
                    "POST",
                    f"{_issue_path(self.key)}/transitions",
                    template="/rest/api/3/issue/{key}/transitions",
                    body={"transition": {"id": self.transition_id}},
                ),
                object_path=True,
            )
            return StepResult([])

        return Step("transition", send, {"transition_id": self.transition_id, "to": self.to})


class StubComment:
    """``comment KEY --body ... [--dedupe-key k]`` with the property and a minimal lookup."""

    name = "comment"
    yes_needs_dedupe_key = True

    def __init__(
        self,
        key: str,
        body: str | None,
        *,
        fmt: str = "markdown",
        dedupe_key: str | None = None,
        on_prepare: Callable[[], None] | None = None,
    ) -> None:
        self.key = key
        self.body = body
        self.fmt: Any = fmt
        self.dedupe_key = dedupe_key
        self.rich: attribution.RichText | None = None
        self.on_prepare = on_prepare

    def prepare(self, pc: PrepareContext) -> None:
        if self.on_prepare:
            self.on_prepare()
        self.rich = pc.prepare_rich(self.body, kind="comment", fmt=self.fmt)
        pc.check_secrets(self.dedupe_key or "")

    def reduce(self, ctx: Context, *, fresh: bool) -> Planned:
        rich = self.rich
        assert rich is not None and rich.doc is not None
        info = body_info(rich)
        found = self._lookup(ctx, rich) if self.dedupe_key else None
        steps = [] if found else [self._add(ctx, rich, info)]
        return Planned(
            steps=steps,
            intent={
                "bodies": [info["sha256"]],
                "dedupe_key": self.dedupe_key,
            },
            changes=[],
            key=self.key,
            dedupe_key=self.dedupe_key,
            dedupe_match=found,
            bodies=[info],
            already_posted=found is not None,
            comment_id=found,
        )

    def check(self, ctx: Context, planned: Planned) -> None:
        return None

    def _lookup(self, ctx: Context, rich: attribution.RichText) -> str | None:
        res = ctx.gw.request(
            "GET",
            f"{_issue_path(self.key)}/comment",
            template="/rest/api/3/issue/{key}/comment",
            query=query({"orderBy": "-created", "expand": "properties", "maxResults": 100}),
        )
        wanted = hashlib.sha256((self.dedupe_key or "").encode()).hexdigest()
        data: Any = res.data
        for c in data.get("comments", []):
            if (c.get("author") or {}).get("accountId") != ctx.me.account_id:
                continue
            for prop in c.get("properties") or []:
                value = prop.get("value") if isinstance(prop, dict) else None
                if isinstance(value, dict) and value.get("key_sha256") == wanted:
                    return str(c["id"])
        return None

    def _add(self, ctx: Context, rich: attribution.RichText, info: dict[str, Any]) -> Step:
        meta = attribution.comment_meta(ctx.actor, rich, dedupe_key=self.dedupe_key)

        def send(gw: WriteGateway) -> StepResult:
            res = gw.request(
                "POST",
                f"{_issue_path(self.key)}/comment",
                template="/rest/api/3/issue/{key}/comment",
                body={
                    "body": rich.doc,
                    "properties": [{"key": attribution.META_PROPERTY, "value": meta}],
                },
            )
            raise_for_write(res, object_path=True)
            data: Any = res.data
            return StepResult([str(data["id"])], comment_id=str(data["id"]))

        return Step(
            "add_comment", send, {"body_sha256": info["sha256"], "body_bytes": info["bytes"]}
        )


class StubDescription:
    """``update KEY --set description=...``: the rich-text unchanged test (``rich_changed``)."""

    name = "update"
    yes_needs_dedupe_key = False
    dedupe_key: str | None = None

    def __init__(self, key: str, body: str) -> None:
        self.key = key
        self.body = body
        self.rich: attribution.RichText | None = None

    def prepare(self, pc: PrepareContext) -> None:
        self.rich = pc.prepare_rich(self.body, kind="field")

    def reduce(self, ctx: Context, *, fresh: bool) -> Planned:
        rich = self.rich
        assert rich is not None and rich.doc is not None
        info = body_info(rich)
        current = read_issue(ctx, self.key, "description").get("description")
        steps = []
        if rich_changed(current, rich):
            steps.append(self._set(rich, info))
        return Planned(
            steps=steps,
            intent={"fields": {"description": info["sha256"]}},
            facts={"description": None if current is None else _sha(current)},
            changes=[{"name": "description", "from": None, "to": "rendered markdown"}],
            key=self.key,
            bodies=[info],
        )

    def check(self, ctx: Context, planned: Planned) -> None:
        return None

    def _set(self, rich: attribution.RichText, info: dict[str, Any]) -> Step:
        def send(gw: WriteGateway) -> StepResult:
            raise_for_write(
                gw.request(
                    "PUT",
                    _issue_path(self.key),
                    template="/rest/api/3/issue/{key}",
                    body={"fields": {"description": rich.doc}},
                ),
                object_path=True,
            )
            return StepResult([])

        return Step("set_fields", send, {"fields": ["description"]})


def _sha(doc: Any) -> str:
    from ticketctl.adf_validate import canonical_json

    return hashlib.sha256(canonical_json(doc).encode()).hexdigest()


class StubCreate:
    """``create --project P --title T [--dedupe-key k]``: the label lookup and the lag guard.

    Stands in for the create command of #31: one provider call carrying the ``tc-`` label, a
    lock-free lookup in resolve and the guarded lookup under the lock (``reduce(fresh=True)``).
    ``clock`` is the guard's clock (tests pass a virtual one), ``on_send`` runs just before the
    request (a test can raise there), ``lookups`` counts the label searches.
    """

    name = "create"
    yes_needs_dedupe_key = False

    def __init__(
        self,
        title: str,
        *,
        dedupe_key: str | None,
        project: str = "DEMO",
        issue_type: str = "Task",
        clock: dedupe.Clock | None = None,
        on_send: Callable[[], None] | None = None,
    ) -> None:
        self.title = title
        self.project = project
        self.issue_type = issue_type
        self.dedupe_key = dedupe_key
        self.clock = clock
        self.on_send = on_send
        self.lookups = 0

    def prepare(self, pc: PrepareContext) -> None:
        return None

    def label(self, site: str) -> str:
        raw = f"{site}\0{self.project}\0{self.dedupe_key}".encode()
        return "tc-" + hashlib.sha256(raw).hexdigest()[:16]

    def _lookup(self, ctx: Context) -> str | None:
        self.lookups += 1
        jql = (
            f'project = "{self.project}" AND labels = "{self.label(ctx.site)}" '
            f'AND reporter = "{ctx.me.account_id}"'
        )
        res = ctx.gw.request(
            "POST",
            "/rest/api/3/search/jql",
            template="/rest/api/3/search/jql",
            body={"jql": jql, "fields": ["summary", "labels"], "maxResults": 50},
        )
        data: Any = res.data
        keys = sorted(str(i["key"]) for i in data.get("issues", []))
        return keys[0] if keys else None

    def reduce(self, ctx: Context, *, fresh: bool) -> Planned:
        found: str | None = None
        if self.dedupe_key is not None:
            if fresh:
                found = dedupe.guarded_lookup(
                    ctx,
                    scope=self.project,
                    dedupe_key=self.dedupe_key,
                    lookup=lambda: self._lookup(ctx),
                    clock=self.clock,
                )
            else:
                found = self._lookup(ctx)
        steps = [] if found else [self._create(ctx)]
        return Planned(
            steps=steps,
            intent={
                "project": self.project,
                "type": self.issue_type,
                "title": self.title,
                "dedupe_key": self.dedupe_key,
            },
            key=None,
            hash_key=self.project,
            dedupe_key=self.dedupe_key,
            dedupe_match=found,
            reused=found is not None,
            existing_key=found,
        )

    def check(self, ctx: Context, planned: Planned) -> None:
        return None

    def _create(self, ctx: Context) -> Step:
        labels = [self.label(ctx.site)] if self.dedupe_key is not None else []

        def send(gw: WriteGateway) -> StepResult:
            if self.on_send is not None:
                self.on_send()
            res = gw.request(
                "POST",
                "/rest/api/3/issue",
                template="/rest/api/3/issue",
                body={
                    "fields": {
                        "project": {"key": self.project},
                        "issuetype": {"name": self.issue_type},
                        "summary": self.title,
                        "labels": labels,
                    }
                },
            )
            raise_for_write(res, object_path=False)
            data: Any = res.data
            return StepResult([str(data["key"])], key=str(data["key"]))

        return Step("create", send, {"project": self.project, "type": self.issue_type})
