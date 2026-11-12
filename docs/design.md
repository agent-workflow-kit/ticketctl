# ticketctl: design

Status: designed 2026-11-12 (interface 0.1.0-design). The normative rules are in [interface.md](interface.md); this document records the decisions, the live verification behind them and the internal structure, and links to interface headings instead of restating them.

## Role and layer

Normalized ticket access (Jira Cloud first; other providers later). Layer 1 (primitive). Works fully standalone (architecture §2, C2): no harness, bus, shared library or other awkit tool is needed.

## Uses

| Dependency | Binding | Notes |
|---|---|---|
| Jira Cloud REST API v3 and the agile API | none (direct, stdlib HTTP client in `gateway.py`) | the only external boundary; see [Invoking the provider](interface.md#invoking-the-provider) |

ticketctl calls no other awkit tool. Its known consumers are ticket-queue, ticket-create and ticket-work (interface registry).

## Decisions

| # | Decision | Rationale |
|---|---|---|
| D-1 | Own client over the Python standard library (`urllib`), behind `gateway.py`. No wrapped Jira CLI and no dependence on any user-installed script. | No dependency, no Node or PATH problem under cron, and full control of the provider's quirks (silent semantic JQL errors, the 100-per-page cap with all fields, three error body shapes, ambiguous custom fields). See [Invoking the provider](interface.md#invoking-the-provider). |
| D-2 | One internal provider boundary, no plugin system: operations return ticketctl's normalized objects; Jira is the only implementation; `sites.<name>.provider` names it. A later provider is new code behind the same objects. | Keeps the `--json` contract provider-neutral (see [Ticket object](interface.md#ticket-object)) without building a framework for a non-goal (architecture §19.2). |
| D-3 | Three ways to select tickets (named queries in config, structured flags compiled to JQL by ticketctl, raw JQL from a file or stdin), every search validated through the provider's parse endpoint first. | Spike A: a semantic JQL error returns an empty success; callers never build JQL from text they did not write. See [`search`](interface.md#search). |
| D-4 | One write flow: resolve, plan, `--dry-run`, confirm, `pre-` hook, stale check (`--expect-plan`), audit-and-send. See [Write flow](interface.md#write-flow). | Jira has no dry-run and no idempotency; the plan hash makes the confirmation specific and the stale check refuses a changed ticket. |
| D-5 | Transitions are selected by target status, not id; a done target always sends a resolution; an assignee requirement is an explicit plan step. See [Transition order](interface.md#transition-order). | Spike B: transitions are global, a done transition without a resolution succeeds silently, and leaving the backlog fails without an assignee that the transition call cannot set. |
| D-6 | Logical field names mapped per site by name, id or schema, discovered by schema when unmapped, ambiguity refused. See [Field mapping](interface.md#field-mapping). | Spike A: two story-point fields exist and only one carries data; acceptance criteria are findable only by name. |
| D-7 | v3 and ADF only; own Markdown-to-ADF converter that never creates mention, card or media nodes; the wiki-markup string API is never used. See [Text conversion](interface.md#text-conversion). | Spike B: a v2 string is parsed as wiki markup (`*x*`, `h2.`, `#` lists, `{code}`, `[~accountid:...]`). |
| D-8 | `sites` map, basic authentication with `token_env`, site selection by flag, project prefix, default, then the only site; only configured hosts are contacted. See [Site selection](interface.md#site-selection) and [Credentials](interface.md#credentials). | Several sites are plausible; a key is never sent to a site that was not selected. |
| D-9 | Per-kind `pre-` and `post-` hooks with payloads that carry no ticket text. See [Hooks](interface.md#hooks). | Lets gate policies and notifications attach to each write kind. |
| D-10 | Command surface as in [Commands](interface.md#commands); links, attachments, comment edit and delete, watchers, bulk writes and a TUI are left to 1.x. | Smallest surface that serves the three consumers. |
| D-11 | Reads retry; a write retries only when the request provably was not processed; an ambiguous write exits 12 `outcome_unknown` and the caller re-runs with a dedupe key. See [Reliability](interface.md#reliability). | Jira has no write idempotency. |
| D-12 | Stateless: a small cache plus `audit.jsonl` and `log.jsonl`; no `state.db`, no cron, no reconcile. See [Files](interface.md#files). | Jira holds the truth; pending confirmations belong to the caller. |
| D-13 | A provider-neutral ticket object with `null` meaning unknown, `blocked` computed from the same read, and done-ness from the status category only. See [Ticket object](interface.md#ticket-object). | The contract three consumers pin. |
| D-14 | Tests through the gateway with a fake provider and synthetic fixtures; an opt-in `live` suite that closes its own test tickets. See Test strategy below. | Nothing private may enter the repo. |

## Resolved open topics

Architecture §20 listed four open topics for ticketctl. Provider interface: D-2. JQL and filter config: D-3. Write safety flow details: D-4, D-5 and [Dedupe](interface.md#dedupe). Own client versus invoking user-installed tools: D-1. The seeded text in this file's earlier version (status "not yet designed") is superseded.

## Live verification

Spikes ran against a real Jira Cloud site on 2026-11-11 and 2026-11-12 (private records: A read-only, B writes on three throwaway tickets that were closed as done with a "won't do" resolution, C targeted checks). The results below are the facts the design relies on; every one becomes a regression test or a `live` test in the plan.

| # | Fact | Where it bites |
|---|---|---|
| V-1 | Basic authentication (email and token) works on the site URL; a bearer header with an API token is refused (403); a wrong token is a 401 with a plain-text body. | Credentials |
| V-2 | The old `POST /search` returns 410; `/search/jql` pages with `nextPageToken`, has no total (a separate approximate-count call exists), and with all fields silently returns 100 per page. | `search` |
| V-3 | A semantic JQL error (unknown field, status value or function) returns 200 with no issues; a syntax error is a 400. The parse endpoint reports them. | `search` validation |
| V-4 | A default issue read is about 31 KB with 555 field keys; with `fields=` about 1.7 KB. | `get --fields` |
| V-5 | v3 descriptions and comments are ADF; v2 returns wiki-markup strings and a v2 write parses wiki markup. | Text conversion |
| V-6 | Two story-point fields exist (a legacy float field with data, a newer estimate field without); the sprint field is identifiable by schema; acceptance criteria only by name and must be written as ADF. | Field mapping |
| V-7 | Transitions are the same ids from every status; a done target needs a resolution (five allowed values); a sub-task transition also needs a parent; the API accepts a done transition without a resolution. | Transition order |
| V-8 | Every transition out of the backlog failed with a 400 until the ticket had an assignee, and the transition body cannot set one. | Transition order |
| V-9 | There is no write idempotency: an identical create made a duplicate ticket; identical comments are not deduplicated. | Dedupe |
| V-10 | Updates return 204 with no body; re-sending an unchanged value is a no-op; a failed multi-field update applies nothing; an unknown or non-screen field is a 400 with `errors.<field>`. | `update` |
| V-11 | There is no optimistic locking: a stale write of a different field keeps the other writer's change (last write wins per field). | Residual risks |
| V-12 | Comment properties and issue properties can be written and fetched but not searched. | Dedupe |
| V-13 | Timestamps carry the user's time zone with an offset that has no colon (`-0600`); sprint dates are UTC with `Z`. | Normalization |
| V-14 | `statusCategory` is `new`, `indeterminate` or `done`; some done-category tickets have no resolution; "blocked" can be computed from the links embedded in one read. | `blocked`, done-ness |
| V-15 | Every response carries rate-limit headers (`X-RateLimit-Limit`, `X-RateLimit-Remaining`, `Ratelimit`, `Ratelimit-Policy`); no `Retry-After` and no 429 were seen. | Reliability |
| V-16 | Errors come in three body shapes: `errorMessages` with `errors`, `{"error": ...}` and plain text; a missing, malformed or hidden key is a 404. | Error envelope |
| V-17 | The agile API works with the same token; sprint membership can be added and removed (204 both ways); the sprint field is an array of objects. | `sprint` |
| V-18 | `notifyUsers=false` returns 403 for a non-admin; a label containing a space is a 400; a label can be up to 255 characters; adding an existing label or removing an absent one is a 204 no-op. | Update |
| V-19 | A comment create body accepts `properties`, written with the comment; they read back by `/comment/{id}/properties/{key}` and `expand=properties`. A property value can be up to 32,768 bytes and a key 255 bytes; the provider does not check key characters. | Dedupe |
| V-20 | A label is searchable about one second after it is set, with or without a project clause; issue and comment properties are silently unsearchable. | Dedupe |
| V-21 | Written ADF is stored as given: `javascript:` links, a 2000-character href, control and bidirectional characters are all accepted and not rewritten; a comment of 32,768 or more characters of text is a 400 `CONTENT_LIMIT_EXCEEDED`. | Text conversion |
| V-22 | The parse endpoint always answers 200; with strict or warn validation a syntax error, an unknown field, status value or function appear as strings in `errors`; with validation none only syntax errors do. | `search` validation |
| V-23 | Closed (done) tickets accept label, comment and property writes; no editable restriction appeared. | Writes |

Still to verify (conservative defaults apply): per-user transition filtering, 429 behavior, scoped API tokens, moved or archived tickets, and the 404-versus-403 behavior for a hidden ticket. Spike C (V-19 to V-23) settled the dedupe mechanisms, the body cap and the parse endpoint's use. Not tested: a 300 KB body, a total text limit across nodes, an empty text node and add-then-remove ordering within one update.

## Modules

| Module | Responsibility |
|---|---|
| `cli` | argparse subcommands, global flags, exit codes, human and `--json` output |
| `config` | load and validate config (Pydantic), drop-ins, env overrides for top-level scalars |
| `gateway` | the only HTTP boundary: request, auth header, timeouts, retry policy, rate-limit pacing, redirect refusal |
| `provider` | the internal provider interface and normalized objects |
| `jira` | the Jira implementation: endpoint calls, field discovery, JQL compile and parse check, transitions, sprints |
| `adf` | ADF to Markdown renderer and Markdown to ADF converter, node allowlist |
| `sanitize` | the one sanitizer for provider strings and identifier grammars |
| `plan` | plan building, canonical hash, stale check |
| `writeflow` | the write order: resolve, confirm, hooks, audit, send, partial results |
| `hooks` | hook execution (process groups, timeouts, payloads) |
| `audit` | `audit.jsonl` and `log.jsonl` appends with `flock`, rotation, `export` |
| `cache` | field list and identity cache with atomic replace |

Dependencies: `pyyaml` (config; the standard library cannot read YAML) and `pydantic` (validation), both required by the design guide. No HTTP library: `urllib` through the gateway.

## Data model

There is no database. State is the cache, the audit log and the request log ([Files](interface.md#files)). All durable truth is in the provider; ticketctl derives everything else per call.

## Concurrency

ticketctl takes no locks other than `flock` around appends to `audit.jsonl` and `log.jsonl`; the cache is replaced by rename. Two simultaneous writes to the same ticket from two processes are two independent plans: the stale check narrows the window, and the provider's last-write-wins per field is a documented residual risk.

## Error handling

Every provider failure maps to the error envelope with a stable `code`, `reason` and the `retryable`, `outcome_unknown` and `retry_after_s` fields ([Error envelope](interface.md#error-envelope)). A failure after a successful earlier step is `partial`, never an automatic rollback. A failed audit append before sending refuses the write (exit 1).

## Test strategy

- Unit and integration tests run through `gateway.py` against a fake provider server (in `tests/`), with synthetic fixtures that follow the verified shapes and never copy a real response. The fake has switches for the verified quirks (silent-empty semantic JQL, the page cap with all fields, a done transition without a resolution, the assignee requirement, the 410 on the old search, `-0600` offsets, ADF node variety, errors in all three body shapes).
- Golden files for every `--json` output and hook payload, validated against `schemas/`.
- Spike regression tests: no wiki markup is ever sent; a done transition always carries a resolution; a semantic JQL error is caught before the search; `[~accountid:x]` in a body stays literal; a changed ticket gives `stale_ticket`; a write is never retried after an ambiguous failure.
- Property tests: the Markdown converter never emits a mention, card or media node and never raises; the ADF renderer never raises on unknown nodes and bounds depth.
- `live` tests (deselected by default, never in CI): read-only checks against any ticket, and a write suite that creates at most three titled test tickets in a project the user's config names and closes each as done with a "won't do" resolution in a `finally`, about 60 requests. Opt-in through `TICKETCTL_LIVE=1`.
- `make verify` (ruff, pyright, pytest, markdown lint) runs before every commit.

## Platforms

Pure standard library; Linux, WSL and macOS behave the same. A macOS checklist covers a real read and a path check. CI runs Python 3.12 and 3.13 on Linux.

## Architecture constraints this design adds

See `awkit-docs/architecture.md` §5.1 and §20 (v1.19): stateless; per-kind hooks; ticket-text-free payloads; every write audited with an attempted and an outcome record; no pending store.
