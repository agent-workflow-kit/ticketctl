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
| D-1 | Own client over the Python standard library (`http.client`), behind `gateway.py`. No wrapped Jira CLI and no dependence on any user-installed script. | No dependency, no Node or PATH problem under cron, and full control of the provider's quirks (silent semantic JQL errors, the 100-per-page cap with all fields, three error body shapes, ambiguous custom fields) and of the connect-versus-send distinction that write safety needs. See [Invoking the provider](interface.md#invoking-the-provider). |
| D-2 | One internal provider boundary, no plugin system: operations return ticketctl's normalized objects; Jira is the only implementation; `sites.<name>.provider` names it. A later provider is new code behind the same objects. | Keeps the `--json` contract provider-neutral (see [Ticket object](interface.md#ticket-object)) without building a framework for a non-goal (architecture §19.2). |
| D-3 | Three ways to select tickets (named queries in config, structured flags compiled to JQL by ticketctl, raw JQL from a file or stdin), every search validated through the provider's parse endpoint first. A trailing `ORDER BY` of a named or raw query is split off and replaced by `--order-by`, the rest is parenthesized and combined with the structured flags by `AND`. | Spike A: a semantic JQL error returns an empty success; callers never build JQL from text they did not write. The combination rule exists because a named query with an `ORDER BY` (the documented example has one) otherwise yields a syntax error, and an `OR` inside a raw query would change precedence. See [`search`](interface.md#search) and [Search compilation](interface.md#search-compilation). |
| D-4 | One write flow: resolve, plan, `--dry-run`, confirm, `pre-` hooks, a fresh stale check against the held plan hash (`--expect-plan`), audit-and-send. Idempotency is evaluated first: a plan with no remaining step is `unchanged` before any prompt, hook, audit record or hash comparison. See [Write flow](interface.md#write-flow). | Jira has no dry-run and no idempotency; the plan hash makes the confirmation specific and the stale check refuses a ticket that changed in something the plan reads. The order is fixed by the roadmap hook row and forgectl's merge order: the stale check sits after the hooks because it is the one nearest the write (the hash is also compared early, when the plan is built), and the `pre-` hook runs after the confirmation so an expensive gate does not run before every prompt or for confirmations that are later rejected. A hook placed before the confirmation is a hook feature candidate. |
| D-5 | Transitions are selected by target status, not id; a done target always sends a resolution; the assignee step is in the plan by rule (the ticket is unassigned and `--assign` is given, or `assign_on_start: me` is set and the target is not done), never guessed; a provider 400 without such a step is exit 12 `rejected` with `details.reason_hint`. See [Transition order](interface.md#transition-order). | Spike B: transitions are global, a done transition without a resolution succeeds silently, and leaving the backlog fails with a validator that neither the transitions list nor the edit metadata reveals and that the transition call cannot satisfy. The requirement is only discoverable by attempting, so the plan assigns by rule and the provider's refusal is reported with a hint decided from plan facts, never from message text (which is locale and workflow dependent). |
| D-6 | Logical field names mapped per site by name, id or schema, discovered by schema when unmapped (the legacy points field has the generic `float` schema and is found only by its exact name), ambiguity refused lazily (only when the requested output or a write needs the field). See [Field mapping](interface.md#field-mapping). | Spike A: two story-point fields exist and only one carries data; acceptance criteria are findable only by name; refusing ambiguity on every read would break `get --fields key,title`. |
| D-7 | v3 and ADF only; own Markdown-to-ADF converter that never creates mention, card or media nodes; one `validate_adf()` allowlist ends both input forms (Markdown and `--format adf`); the wiki-markup string API is never used. See [Text conversion](interface.md#text-conversion) and [ADF allowlist](interface.md#adf-allowlist). | Spike B: a v2 string is parsed as wiki markup (`*x*`, `h2.`, `#` lists, `{code}`, `[~accountid:...]`). Spike C: Jira stores a `javascript:` link as sent, so ticketctl is the only filter. |
| D-8 | `sites` map, basic authentication with `token_env`, one total site-selection table (flag, browse URL, project prefix, default, the only site), only configured hosts are contacted; the account for `me` is always the live or cached `/myself` account and `me.jira` and `sites.<name>.account_id` are only expectations. See [Site selection](interface.md#site-selection) and [Credentials](interface.md#credentials). | Several sites are plausible; a key is never sent to a site that was not selected; account ids are per organization, so a global id cannot be trusted. |
| D-9 | Per-kind `pre-` and `post-` hooks with payloads that carry no ticket text but do carry ids, names and the assignee, type, priority and sprint facts. See [Hooks](interface.md#hooks). | Lets gate policies and notifications attach to each write kind; gate policies match on names and ids, so those facts are in the payload. |
| D-10 | Command surface as in [Commands](interface.md#commands); links and attachments are read (metadata) in 0.x; creating or deleting links, attachment download and upload, comment edit and delete, watchers, bulk writes (including several keys for `sprint`) and a TUI are left to 1.x. | Smallest surface that serves the three consumers. |
| D-11 | Reads retry; a write retries only when the request provably was not processed; an ambiguous write exits 12 `outcome_unknown` and the caller re-runs (with a dedupe key for `comment` and `create`). See [Reliability](interface.md#reliability). | Jira has no write idempotency. |
| D-12 | Stateless: a small cache plus `audit.jsonl` and `log.jsonl`; no `state.db`, no cron, no reconcile. See [Files](interface.md#files). | Jira holds the truth; pending confirmations belong to the caller. |
| D-13 | A provider-neutral ticket object with `null` meaning unknown, `blocked` computed from the same read (direction relative to the ticket, link types configurable), done-ness from the status category only, and a `--fields` vocabulary so that unrequested properties are neither fetched nor printed. See [Ticket object](interface.md#ticket-object). | The contract three consumers pin. |
| D-14 | Tests through the gateway with a fake provider and synthetic fixtures; an opt-in `live` suite that closes its own test tickets. See Test strategy below. | Nothing private may enter the repo. |
| D-15 | Dedupe keys are bound to content and required for non-interactive writes: a stored hash of the body (comment) or of type and title (create) detects a reused key with other content (`already_exists`); `comment` and `create` with `--yes` require `--dedupe-key` (exit 2, `dedupe_key_required`). See [Dedupe](interface.md#dedupe). | A replayed `--yes --expect-plan` of a comment or create has no ticket state in its hash, so a confirmation confirmed twice, a cron overlap, a dashboard double click or an `outcome_unknown` re-run would post twice. A key is cheap and makes a re-run always safe. The lookup also guards against planted labels, comment floods and concurrent runs (own-author filter, bounded scan, per-key lock). |
| D-16 | A failure after an earlier step landed is the error envelope with exit 12 `partial` and the full result under `details.result`; there is no second result path and no automatic rollback. See [Result object](interface.md#result-object) and [Error envelope](interface.md#error-envelope). | A caller must be able to see that the assignee changed although the transition failed; one printed shape for every failure keeps consumers simple. |
| D-17 | The plan hash covers the caller's intent plus only the facts the plan reads, and an own assign step never invalidates it. See [Plan object](interface.md#plan-object). | A hash of the whole pre-write state would make a retry after a partial look stale; a hash of nothing would miss a human takeover during a confirmation that can wait days. `--if-status` is the one explicit precondition (restore flows). |
| D-18 | `sprint add|remove` takes exactly one key; `remove` moves the ticket to the backlog and so drops every sprint. See [`sprint add|remove KEY`](interface.md#sprint-addremove-key). | One plan, one result, one audit pair and one hook pair per command; spike B7 shows the backlog endpoint removes every sprint. |
| D-19 | The confirmation reads `/dev/tty`, not stdin, and a run is interactive only with a TTY, a `human:` actor and no `--yes`; `--override-escalation` is human-only by actor claim and works with `--yes`. See [Human actions](interface.md#human-actions). | Bodies arrive on stdin, so a stdin-based definition would make a person non-interactive; the dashboard needs `--yes --actor human:<user>`, so "human-only" cannot mean "no `--yes`". |
| D-20 | The trust boundary is the user's credentials: ticketctl does not restrict `--config`, `--hook`, `--actor` or the environment; every audit record carries the config, hook and token-variable facts so a bypass is visible. See [Trust boundary](interface.md#trust-boundary). | The caller controls arguments and environment, so no in-process flag is a boundary (a `read_only` mode was declined for the same reason). Guide 5 defines `--config` and tests need it. Constrained deployments wrap ticketctl. |
| D-21 | One sanitizer rule (Unicode categories plus a short list) applied at the output layer to every string that leaves the process, whatever its origin. See [Sanitizing](interface.md#sanitizing). | Errors echo caller and file strings, so sanitizing only provider text leaves escape sequences reachable. No normalization is applied, because consumers compare provider names by equality. |
| D-22 | Gateway invariants: URLs are built only from the site URL and path templates, every redirect fails, no `Accept-Encoding`, a hard response cap, a JSON depth guard, `--verbose` never enables protocol debug output, proxies (`HTTPS_PROXY`, `NO_PROXY`) are honored through a CONNECT tunnel. See [Invoking the provider](interface.md#invoking-the-provider). | Credentials are never re-sent to another host; TLS stays end to end; ignoring proxies would break users behind one, and proxy and CA variables fall inside the trust boundary of D-20. |
| D-23 | The attribution line is the first paragraph of the body, mandatory for an `auto:` actor, and ticketctl's marker is a comment property (`ticketctl.meta`) instead of hidden text. See [Attribution](interface.md#attribution). | ADF has no hidden text; architecture §14 places the line first so no untrusted text can hide it, and Jira collapses long comments. A property lets an awkit-authored comment be told from a human one on the same account. |
| D-24 | A gate's decision lives in the gate's own log: ticketctl audits only writes that reached the provider boundary; a veto, escalation, stale or precondition refusal and `confirmation_required` write no audit record and return `decision_ids`, `hook` and `plan` in the envelope. See [Audit and log](interface.md#audit-and-log). | gatectl records every decision; duplicating refusals in the audit would blur "what ticketctl did" with "what was stopped". The caller records what it needs from the envelope. |

## Deviations from the guide

- The confirmation reads `/dev/tty` (forgectl reads stdin), so a stdin body does not remove the human (D-19).
- The marker for awkit-authored ticket comments is a comment property, not the fixed HTML marker, because ADF has no hidden text (D-23).
- One attribution template serves both actor kinds (architecture §14 names two); `attribution.enabled` applies to `human:` actors only.
- A refusal's `reason` equals its `code` for exit 20 (guide 4.2 asks for a reason; the code is the reason).
- The audit `result` vocabulary includes `unknown` for an ambiguous outcome.
- Environment overrides are limited to top-level scalars (`TICKETCTL_TIMEOUT`, `TICKETCTL_DEFAULT_SITE`), a narrowing of guide 5.
- `--field` (transition) and `--set` (create, update) keep their names and share one grammar; `assign_on_start` keeps its name.

## Resolved open topics

Architecture §20 listed four open topics for ticketctl. Provider interface: D-2. JQL and filter config: D-3. Write safety flow details: D-4, D-5 and [Dedupe](interface.md#dedupe). Own client versus invoking user-installed tools: D-1. The seeded text in this file's earlier version (status "not yet designed") is superseded.

## Live verification

Spikes ran against a real Jira Cloud site on 2026-11-11 and 2026-11-12 (private records: A read-only, B writes on three throwaway tickets that were closed as done with a "won't do" resolution, C targeted checks). The results below are the facts the design relies on; every one becomes a regression test or a `live` test in the plan.

| # | Fact | Where it bites |
|---|---|---|
| V-1 | Basic authentication (email and token) works on the site URL; a bearer header with an API token is refused (403); a wrong token is a 401 with a plain-text body. | Credentials |
| V-2 | The old `POST /search` returns 410; `/search/jql` pages with `nextPageToken`, has no total (a separate approximate-count call exists), and with all fields silently returns 100 per page. The cursor is therefore the only way past one page; it is exposed as `search --cursor`. | `search` |
| V-3 | A semantic JQL error (unknown field, status value or function) returns 200 with no issues; a syntax error is a 400. The parse endpoint reports them. | `search` validation |
| V-4 | A default issue read is about 31 KB with 555 field keys; with `fields=` about 1.7 KB. | `get --fields` |
| V-5 | v3 descriptions and comments are ADF; v2 returns wiki-markup strings and a v2 write parses wiki markup. | Text conversion |
| V-6 | Two story-point fields exist (a legacy float field with data, a newer estimate field without); the legacy one has the generic `float` schema (many other fields do too), so only its exact name `Story Points` identifies it; the sprint field is identifiable by schema; acceptance criteria only by name and must be written as ADF. | Field mapping |
| V-7 | Transitions are the same ids from every status; a done target needs a resolution (five allowed values); a sub-task transition also needs a parent; the API accepts a done transition without a resolution. | Transition order |
| V-8 | Every transition out of the backlog failed with a 400 until the ticket had an assignee, and the transition body cannot set one. The validator appears in neither the transitions list nor the edit metadata; only the 400 reveals it, with locale-dependent text. | Transition order |
| V-9 | There is no write idempotency: an identical create made a duplicate ticket; identical comments are not deduplicated. | Dedupe |
| V-10 | Updates return 204 with no body; re-sending an unchanged value is a no-op; a failed multi-field update applies nothing; an unknown or non-screen field is a 400 with `errors.<field>`. | `update` |
| V-11 | There is no optimistic locking: a stale write of a different field keeps the other writer's change (last write wins per field). | Residual risks |
| V-12 | Comment properties and issue properties can be written and fetched but not searched. | Dedupe |
| V-13 | Timestamps carry the user's time zone with an offset that has no colon (`-0600`); sprint dates are UTC with `Z`. | Normalization |
| V-14 | `statusCategory` is `new`, `indeterminate` or `done`; some done-category tickets have no resolution; "blocked" can be computed from the links embedded in one read, where blocked-by is the inward side of the `Blocks` link type. | `blocked`, done-ness |
| V-15 | Every response carries rate-limit headers (`X-RateLimit-Limit`, `X-RateLimit-Remaining`, `Ratelimit`, `Ratelimit-Policy`); no `Retry-After` and no 429 were seen. | Reliability |
| V-16 | Errors come in three body shapes: `errorMessages` with `errors`, `{"error": ...}` and plain text; a missing, malformed or hidden key is the same 404 with the same text, so no `gone` or `hidden` distinction exists. | Error envelope |
| V-17 | The agile API works with the same token; sprint membership can be added and removed (204 both ways); the sprint field is an array of objects; remove goes through the backlog endpoint and removes every sprint. | `sprint` |
| V-18 | `notifyUsers=false` returns 403 for a non-admin; a label containing a space is a 400; a label can be up to 255 characters; adding an existing label or removing an absent one is a 204 no-op. | Update |
| V-19 | A comment create body accepts `properties`, written with the comment; they read back by `/comment/{id}/properties/{key}` and `expand=properties`. A property value can be up to 32,768 bytes and a key 255 bytes; the provider does not check key characters. | Dedupe |
| V-20 | A label is searchable about one second after it is set, with or without a project clause; issue and comment properties are silently unsearchable. | Dedupe |
| V-21 | Written ADF is stored as given: `javascript:` links, a 2000-character href, control and bidirectional characters are all accepted and not rewritten; a comment of 32,768 or more characters of text is a 400 `CONTENT_LIMIT_EXCEEDED`. | Text conversion |
| V-22 | The parse endpoint always answers 200; with strict or warn validation a syntax error, an unknown field, status value or function appear as strings in `errors`; with validation none only syntax errors do. | `search` validation |
| V-23 | Closed (done) tickets accept label, comment and property writes; no editable restriction appeared. | Writes |
| V-24 | The create screen does not offer `labels` and `labels` was never sent in a create body; labels are writable by edit but absent from the edit metadata. | `create`, dedupe labels |
| V-25 | A search embeds only the first 20 comments of each ticket, so comments are read with their own endpoint. | `get --comments` |

Still to verify (conservative defaults apply): per-user transition filtering, 429 behavior, scoped API tokens, moved or archived tickets (a moved key can differ from the requested one) and the order of the provider's priority list, the 404-versus-403 behavior for a hidden ticket, labels in a `POST /issue` body (spike D), comment visibility on a customer-facing (service management) project, the text limits of descriptions and rich-text custom fields, an empty text node, a total text limit across nodes, and the order of add and remove within one update. Spike C (V-19 to V-23) settled the dedupe mechanisms, the body cap and the parse endpoint's use. Not tested: a 300 KB body.

## Modules

| Module | Responsibility |
|---|---|
| `cli` | argparse subcommands, global flags, exit codes, human and `--json` output; the sanitizer runs here, at the output layer |
| `config` | load and validate config (Pydantic), drop-ins, env overrides for top-level scalars; the loader checks (`default_site` names a site, `email` xor `email_env`, `queries` and `extra` names collide with no logical name, url, duration and board shapes, `me` and `account_id` shapes) that JSON Schema cannot express, all failing as `invalid_config` naming the key |
| `gateway` | the only HTTP boundary: request, auth header, connect phase, timeouts, retry policy, rate-limit pacing, redirect refusal, response cap, proxy tunnel |
| `provider` | the internal provider interface and normalized objects |
| `jira` | the Jira implementation: endpoint calls, field discovery, JQL compile and parse check, transitions, sprints |
| `adf` | ADF to Markdown renderer (with budgets) and Markdown to ADF converter; the one `validate_adf()` allowlist |
| `sanitize` | the one sanitizer rule (applied at the output layer) and the identifier grammars with the `segment()` path helper |
| `plan` | plan building, canonical hash, idempotency reduction, stale check |
| `writeflow` | the write order: resolve, confirm, hooks, audit, send, partial results |
| `hooks` | hook execution (process groups, timeouts, payloads) |
| `audit` | `audit.jsonl` and `log.jsonl` appends with `flock`, rotation, `export` |
| `cache` | field list, priority list and identity cache with atomic replace, keyed by site, url and email hash |

Dependencies: `pyyaml` (config; the standard library cannot read YAML) and `pydantic` (validation), both required by the design guide. No HTTP library: `http.client` through the gateway.

## Data model

There is no database. State is the cache, the audit log, the request log and the dedupe lock files ([Files](interface.md#files)). All durable truth is in the provider; ticketctl derives everything else per call.

## Concurrency

ticketctl takes `flock` around appends to `audit.jsonl` and `log.jsonl` (rotation included) and, for a write that has a dedupe key, a per-key lock from the lookup to the outcome record; the cache is replaced by rename. Two simultaneous writes to the same ticket from two processes are two independent plans: the plan hash narrows the window (an own assign step never invalidates it), and the provider's last-write-wins per field is a documented residual risk. The dedupe lock covers one host only; cross-host concurrency is a residual risk.

## Error handling

Every provider failure maps to the error envelope with a stable `code`, `reason` and the `retryable`, `outcome_unknown` and `retry_after_s` fields; the exit codes, codes and reasons are one table ([Exit codes](interface.md#exit-codes), [Error envelope](interface.md#error-envelope)). A failure after a successful earlier step is exit 12 `partial` with the applied steps in `details.result`, never an automatic rollback (D-16). A failed `attempted` audit append before sending refuses the write; a failed outcome append after a send only warns. HTTP statuses map per endpoint kind ([Reliability](interface.md#reliability)).

## Consumer flows (non-normative)

How ticket-work uses the primitives; ticketctl itself knows nothing about its callers.

- Start: `sprint add KEY --sprint open`, then `transition KEY --to <status> --assign me`. Two commands, two hashes; because a hash covers only what its plan reads, applying one does not stale the other, so both can be dry-run before the confirmation.
- Restore after a failed launch: `transition KEY --to <previous status> --if-status <started status>`, then `update KEY --assign none|<previous>` and `sprint add|remove` as needed. A human's newer move refuses with `precondition_failed`; the assignee and sprint set by the start are not undone by a transition. Reopening from done is not supported in 0.x.

## Test strategy

- Unit and integration tests run through `gateway.py` against a fake provider server (in `tests/`), with synthetic fixtures that follow the verified shapes and never copy a real response. The fake has switches for the verified quirks (silent-empty semantic JQL, the page cap with all fields, a done transition without a resolution, the assignee requirement, the 410 on the old search, `-0600` offsets, ADF node variety, errors in all three body shapes, a 429 with `Retry-After`, a connection dropped after the headers).
- Golden files for every `--json` output (one schema per command) and hook payload, validated against `schemas/`.
- Spike regression tests: no wiki markup is ever sent; a done transition always carries a resolution; a semantic JQL error is caught before the search; `[~accountid:x]` in a body stays literal; a changed ticket gives `stale_ticket`; a write is never retried after an ambiguous failure.
- Write-flow tests: a re-run after a landed transition with the old hash gives `unchanged`; a re-run after the assign landed and the transition failed sends only the transition with the same hash; an assign then a transition 400 gives exit 12 `partial` with `details.result.steps` `[done, failed]`, and a create then a sprint add failure likewise; a takeover by another assignee gives `stale_ticket`; an actor class change gives `stale_ticket`; an `--if-status` mismatch exits 20; a reused dedupe key with other content gives `already_exists`; `--yes` without `--dedupe-key` on `comment` and `create` exits 2; unknown outcome, a failed outcome append and a failed `attempted` append at step 2 are covered.
- Search tests: the `ORDER BY` scanner (quotes, escapes, `order by` inside a string literal), precedence with `OR`, the time zone conversion of `--updated-since`, and a property test that every structured flag value yields exactly one clause or an error.
- Property tests: the Markdown converter never emits a mention, card or media node and never raises; link schemes (`javascript:`, `data:`, userinfo), depth, node count and mark attributes are refused by `validate_adf()`; the ADF renderer never raises on unknown nodes and respects its depth, node and size budgets.
- Sanitizer test: the whole code space is enumerated against the rule. Token test: every command runs with `--verbose` against the fake with a sentinel token, and stdout, stderr, log and audit are searched for the token and its base64 form.
- `live` tests (deselected by default, never in CI): read-only checks against any ticket, and a write suite that creates at most three titled test tickets in a project the user's config names and closes each as done with a "won't do" resolution in a `finally`, about 60 requests. The live suite never touches a ticket it did not create; live comments cannot be deleted, so it posts at most one comment per test ticket. Opt-in through `TICKETCTL_LIVE=1`.
- `make verify` (ruff, pyright, pytest, markdown lint) runs before every commit.

## Residual risks

The user-facing list is [Residual risks](interface.md#residual-risks); the design-level points are these. The human claim (`human:`) is advisory: a same-user process can state it, and the audit records `actor_source`, `stdin_tty` and the override scope so the claim is checkable. A driven caller can create new files with `export --to`. The dedupe lock is per host. A create whose labels cannot go in the create body has a window between its two steps. Plan and error text carry provider text and are untrusted for consumers.

## Platforms

Pure standard library; Linux, WSL and macOS behave the same. A macOS checklist covers a real read and a path check. CI runs Python 3.12 and 3.13 on Linux.

## Architecture constraints this design adds

See `awkit-docs/architecture.md` §5.1 and §20 (v1.19): stateless; per-kind hooks; ticket-text-free payloads; every provider write audited with an attempted and an outcome record; idempotency first with an exit 12 `partial` for partial failures; no pending store; allowing ticketctl allows everything it can do with the user's credentials (§13.1).
