# ticketctl: implementation plan

Implements [interface.md](interface.md) and [design.md](design.md). The plan is staged; each stage's issues are expanded into bite-sized test-first steps when picked up. The interface is the contract: when a rule is unclear, change the interface first, then the code.

## Conventions for every stage

- **Gateway boundary.** All HTTP goes through `src/ticketctl/gateway.py`. Tests replace it with a fake provider server (an in-process HTTP server under `tests/`) that serves synthetic fixtures. Fixtures are built from the shapes the live checks verified and never copy a real response; no real site name, project key, field id, account or ticket text enters the repo.
- **Fake quirk switches.** The fake provider has a switch for each verified quirk (design.md, Live verification): silent-empty semantic JQL, the page cap with all fields, a done transition accepted without a resolution, the assignee validator on transitions out of the backlog, the 410 on the old search, offsets without a colon, ADF node variety, the three error body shapes, rate-limit headers, a label visible after a delay, a slow or dropped response.
- **Pinned sibling schemas.** ticketctl calls no sibling tool, so nothing is pinned; consumers pin ticketctl's schemas, not the reverse.
- **Process and live test split.** Unit and integration tests run offline in CI. Tests that start `ticketctl` as a process (exit codes, `/dev/tty` confirmation through a pty, hooks, locks, `flock`) are ordinary tests. Real-site tests are marked `live`, deselected by default, never run in CI, and need `TICKETCTL_LIVE=1`.
- **Golden files.** Every `--json` output and hook payload has a golden file validated against `schemas/`.
- **Spike regressions.** Every live-verified fact in design.md becomes a test or a `live` check (see each stage).
- **`make verify`** (ruff, pyright, pytest, markdown lint) passes before every commit.
- **Schemas are authoritative for shapes.** The private generator is not in the repo; after the first implementation stage the schemas are maintained in the repo by hand and checked by tests against the interface's enums.

## Module map

| Module | Stage | Responsibility |
|---|---|---|
| `cli` | 0 | argparse subcommands, global flags, exit codes, human and `--json` output |
| `config` | 0 | load and validate config (Pydantic), drop-ins, env overrides for top-level scalars, loader checks |
| `gateway` | 0 | the only HTTP boundary: requests, auth header, timeouts, retry policy, pacing, redirect and proxy rules |
| `sanitize` | 0 | the one sanitizer and the identifier grammars |
| `audit` | 0 | `audit.jsonl` and `log.jsonl` appends with `flock`, rotation, `export` |
| `cache` | 1 | field list and identity cache with atomic replace |
| `provider`, `jira` | 1 | the provider interface and the Jira implementation (endpoints, field discovery, JQL compile and parse check, transitions, sprints) |
| `adf` | 1, 2 | ADF to Markdown renderer (stage 1), Markdown to ADF converter and the allowlist (stage 2) |
| `plan`, `writeflow` | 3 | plan building, canonical hash, the write order, partial results |
| `hooks` | 3 | hook execution (process groups, timeouts, payloads) |
| `dedupe` | 4 | the dedupe lock and the lookups for comment and create |

## Stage 0: foundation

**Goal**: A package that loads config, makes one safe HTTP call through the gateway, prints errors as the envelope and writes the logs.
**Success Criteria**: `ticketctl --version`, `validate`, `sites` and `whoami` work against the fake provider; config errors name the key; exit codes follow the table; the sanitizer matches the specified classes; the gateway enforces host, redirect, TLS, size and proxy rules; token never appears in any output.
**Tests**: config loader (drop-ins, env scalars, grammars, `url` rules, `allow_http` loopback only); gateway (timeouts, retry table, 429 and `Retry-After`, cross-host redirect fails, response caps, proxy CONNECT, no header echo with `--verbose` and `TICKETCTL_DEBUG`); sanitizer over all of Unicode categories against the specified classes; identifier grammars; log and audit append with `flock`, rotation; golden files for `whoami`, `sites`, `validate`, the error envelope; token-leak test (token in env never in stdout, stderr, log, audit, cache).
**Status**: Not Started

## Stage 1: reads

**Goal**: Every read command works against the fake provider with the normalized ticket object.
**Success Criteria**: `get`, `search`, `comments`, `transitions`, `sprints`, `fields` and `sites resolve` print the schemas' shapes; a semantic JQL error is exit 22 before any search; paging with `--cursor` and the 500 cap; `blocked` computed from the same read; ADF rendered with the safety rules.
**Tests**: search compilation (structured flags, named queries, ORDER BY wrapping with the quote-aware balance rule, bypass attempts including `x) OR (...`); parse-endpoint strictness; field discovery by schema and the ambiguity refusals; timestamp normalization (offsets without a colon, user time zone for absolute dates); renderer budgets (depth, nodes, output size) and unknown nodes; blocked tri-state; multi-key `get`; cache TTL and corruption; spike regressions V-2 to V-6, V-13, V-14, V-22.
**Status**: Not Started

## Stage 2: text conversion

**Goal**: Markdown becomes ADF safely and attribution is applied on every rich-text path.
**Success Criteria**: the converter never emits a mention, card or media node and only `http` and `https` links; `--format adf` is validated by the allowlist including link schemes; size and emptiness rules enforced; the attribution paragraph is applied for `auto:` actors on every rich-text write path.
**Tests**: property tests (random Markdown never yields a disallowed node, never raises); `[~accountid:...]`, `{code}`, `h2.`, `javascript:` and control or bidi characters stay literal or are replaced; allowlist bypass attempts; round trip with the renderer; spike regressions V-5, V-21.
**Status**: Not Started

## Stage 3: the write flow

**Goal**: `transition`, `comment` and `update` follow the one flow with plan, `--dry-run`, confirmation, hooks, stale check, audit and partial results.
**Success Criteria**: the eight-step order holds; idempotency is evaluated before validation errors and before the plan hash comparison; a re-run after a lost response is `unchanged`; `partial` and `outcome_unknown` exit 12 with the result in the envelope; confirmation reads `/dev/tty`, a decline is exit 20 `declined`; `--yes` needs `--expect-plan` and, for `comment`, a dedupe key; hooks run after the confirmation; the audit has an `attempted` and an outcome record per provider call.
**Tests**: each step of the flow against the fake provider with each quirk switch; the assign step rule (deterministic, the provider's 400 is `rejected` with the hint); done transitions always carry a resolution; hash scope (a plan under `auto:` confirmed under `human:` matches; a takeover by another assignee is `stale_ticket`); veto, escalation and `--override-escalation`; hooks with a timeout and a process group; payloads carry no ticket text; audit append failure sends nothing; pty tests for the prompt; spike regressions V-7 to V-11.
**Status**: Not Started

## Stage 4: create, dedupe and sprints

**Goal**: `create`, `sprint add|remove` and the dedupe mechanisms.
**Success Criteria**: `create` is one provider call with labels, assignee and description in the body; the dedupe lock covers lookup and create only and the lag guard works; `reused` and `already_exists` follow the checks (reporter, type, title); `comment --dedupe-key` stores and finds the property; `sprint add|remove` takes one key and the guards hold.
**Tests**: lookup and lag simulation in the fake (a label visible after a delay); concurrent creates with one key (two processes, one lock); a planted label with a different reporter is not reused; forged comment properties from another account are ignored; parent and issue type errors; sprint membership with closed, active and future sprints; spike regressions V-9, V-12, V-19, V-20, V-24 to V-33.
**Status**: Not Started

## Stage 5: reliability and operations

**Goal**: The retry, pacing and failure rules, `export` and the log rotation hold under stress.
**Success Criteria**: reads retry as specified; a write retries only when provably unprocessed; rate-limit pacing works within one command; `export` resumes with `--after-seq`; the error mapping table covers every documented status.
**Tests**: failure injection in the fake (connection reset before and after send, 429, 5xx, timeouts, truncated bodies); HTTP status mapping per endpoint; `export` options and refusals (`--to` creates new files only with mode 0600); log rotation under concurrent appenders.
**Status**: Not Started

## Stage 6: live suite, platforms and release readiness

**Goal**: Everything verified against a real site and on macOS; docs complete.
**Success Criteria**: the `live` read suite passes against a real site; the `live` write suite creates at most three titled test tickets in a project the user's config names and closes each as done with a "won't do" resolution in a `finally` (about 60 requests); the macOS checklist is done; README, config example and schemas match the code; a cron-style run from a minimal environment works with a configured `PATH`.
**Tests**: the live suites described above (`TICKETCTL_LIVE=1`); the facts still to verify (per-user transition filtering, 429 behavior, scoped tokens, moved or archived tickets, hidden-ticket 404 versus 403) become live checks or recorded findings; a macOS manual checklist (real read, XDG paths, `/dev/tty` prompt).
**Status**: Not Started

## Version roadmap

- **0.x**: stages 0 to 6, shipped as `feat` releases while the interface may still change (a breaking change before 1.0 is a `feat` whose title names the break).
- **1.0** when: the live read and write suites pass against a real site; the macOS checklist is done; ticket-queue and ticket-work have exercised `search`, `get`, `transition`, `update`, `comment` and `sprint` against the schemas in their own integration tests; the interface has been stable for one release.
- **1.x** (additive): link create and delete; attachment download and upload; comment edit and delete; bulk writes with per-ticket plans; a `browse` TUI; changelog as a first-class object; a `before_confirm` hook placement and `--dry-run --run-hooks` (hook candidates from review); a resolution backfill for done tickets with none; a read-only deployment mode.
- **2.0**: a second provider behind the internal provider boundary, only if a real need appears.
