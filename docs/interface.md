# ticketctl: interface

Status: 0.1.0-design. Written before implementation (architecture §18.1). This document is the single normative source for ticketctl's rules; `docs/design.md`, the README and the architecture link to its headings and never restate its tables. The `--json` shapes, hook payloads, config, audit and log records are specified in [schemas/](../schemas/).

ticketctl is normalized ticket access for one ticket provider at a time. Jira Cloud is the only provider in 0.x. It reads tickets, searches them, lists transitions, comments, and performs writes (transition, comment, create, update, sprint add and remove) through one confirm-by-default write flow. It is stateless: it keeps a small cache and its audit and request logs, and no database.

## Concepts

### Identifiers

A ticket is identified by its key (`KEY-123`: an upper-case project key, a hyphen and a number). Input is case-insensitive and printed upper-case, because Jira resolves lower-case and numeric ids to the canonical key. A ticket may also be given as a browse URL (`<site url>/browse/KEY-123`) whose host matches a configured site; any other host is refused (`unknown_site`, exit 22) before anything is sent. `--json` output always uses the key plus a `site` field, never a URL as the identity.

Keys are not unique across sites. A key is resolved to a site by [Site selection](#site-selection); a command never guesses.

### Site selection

A command picks its site in this order, and uses the first rule that selects exactly one site:

1. `--site <name>`.
2. The one site whose `projects` list holds the key's project prefix (for commands that take a key). A project claimed by several sites, or by none while several sites are configured, exits 21 (`ambiguous_site`) listing the candidates.
3. `default_site`.
4. The only configured site.

With no site selected a command exits 2. ticketctl contacts only the selected site's host, and only hosts named in config. A search or create without a key selects by `--site`, then `default_site`, then the only site.

### Human actions

A write is confirm-by-default. A human is a caller with stdin and stderr attached to a TTY, `--actor` not starting with `auto:`, and no `--yes`. The actor defaults to `human:<me.name>` at a TTY and `auto:unknown` otherwise. These rules are advisory: a same-user process can claim `--actor human:<user>`; the audit record carries `actor_source` and `stdin_tty` so the claim is checkable. `--override-escalation` is human-only (exit 2 otherwise).

### Unknown values

Unknown values are `null`, and a list that was cut off says so in `incomplete`. A consumer treats `null` as the safe answer: `blocked.value: null` is not "not blocked", `assignee: null` means unassigned or unknown according to `incomplete`, and a done-ness question is answered by `status.category`, never by `resolution` (a done ticket can have none).

## Normalized objects

Provider-neutral. No `customfield_N` name appears in any output: only mapped and `extra` fields do (see [Field mapping](#field-mapping)). Fields marked untrusted carry `x-untrusted` in the schemas; fencing remains the consumer's job, but ticketctl has already replaced control and invisible characters with visible markers (see [Sanitizing](#sanitizing)).

### Ticket object

`schemas/cli/get.json`. `get` and `search` print this shape; `search` fills only the requested `--fields`.

| Field | Type | Notes |
|---|---|---|
| `key`, `site`, `url`, `id` | string | `id` is the provider's own id. |
| `type` | `{name, subtask}` | |
| `title` | string | untrusted |
| `description` | string or null | untrusted; Markdown rendered from the provider's rich text. `description_adf` only with `--include-adf`. |
| `status` | `{name, category}` | `category` is `new`, `indeterminate` or `done`. |
| `resolution` | string or null | |
| `assignee`, `reporter` | `{account_id, display_name}` or null | no email address is ever printed. |
| `priority` | `{name, rank}` or null | `rank` is the provider's position, lower is higher. |
| `estimate` | number or null | the mapped `estimate` field. |
| `acceptance_criteria` | string or null | untrusted Markdown. |
| `labels` | string[] | untrusted |
| `sprints` | `{id, name, state, start, end}[]` | `state` is `future`, `active` or `closed`; times UTC. |
| `parent` | `{key, type, title}` or null | `title` only when the provider embeds it. `epic` mirrors `parent` when the site distinguishes them. |
| `links` | `{type, direction, key, status_category, summary}[]` | `direction` is `inward` or `outward`; `type` is the link type's name. `summary` is untrusted. |
| `blocked` | `{value, by}` | `value` is `true`, `false` or `null`; `by` lists the keys. See [Blocked](#blocked). |
| `due_date` | date or null | |
| `created`, `updated`, `resolved` | UTC ISO 8601 with `Z`, or null | the provider's offsets (for example `-0600`, without a colon) are parsed and normalized. |
| `comments` | `{items, total, truncated}` | only with `--comments N`; untrusted bodies. |
| `attachments` | `{id, name, size, mime}[]` | `name` is untrusted. |
| `fields` | object | the configured `extra` fields, converted by schema. |
| `incomplete` | string[] | parts that were cut off or unavailable (`links`, `comments`, `attachments`, ...). |

#### Blocked

`blocked.value` is `true` when any `Blocks` link points at an issue whose `status_category` is not `done` (a missing category counts as not done and the key is listed in `by`). It is `false` only when the links were fetched in full and none qualifies, and `null` when the links were not fetched or were cut off (`incomplete` names `links`). It is computed from the links embedded in the same read; no extra calls.

### Comment object

`schemas/cli/comments.json`: `id`, `author` (`{account_id, display_name}`), `own` (true when the author is the resolved `me`), `created`, `updated`, `body` (untrusted Markdown), `dedupe_key_sha256` (the stored property's hash when present, else null). Listed oldest first by default; `--newest-first` reverses.

### Transition object

`schemas/cli/transitions.json`: `id`, `name`, `to` (`{name, category}`), `required_fields` (`{name, kind, allowed_values?}[]`), `has_screen`. Transitions are global on the supported provider (the same ids apply from every status), but ticketctl always reads the live list for the ticket and never relies on that.

### Sprint object

`schemas/cli/sprints.json`: `id`, `name`, `state`, `start`, `end`, `board`.

### Search result

`schemas/cli/search.json`: `{tickets, truncated, next, count}`. `truncated` is true when `--limit` cut the list; `next` is a resumable cursor when the provider gave one; `count` is present only with `--count`.

### Field mapping

Logical field names are fixed: `estimate`, `acceptance_criteria`, `sprint`, `parent`, `epic`, `labels`, `priority`, `assignee`, `resolution`, `due_date`. Each site maps a logical name to a provider field by `{name}`, `{id}` or `{schema}` under `sites.<site>.fields`, and `fields.extra.<name>` adds more fields to the output. With no mapping, ticketctl discovers the sprint field by its schema and the estimate field by the story-point schemas, and caches the field list ([Files](#files)). Several candidates for one logical field exit 21 (`ambiguous_field`) listing them unless config picks one; a mapping that names a missing field is `invalid_config` (exit 2) at load for `validate`, and exit 22 `unknown_field` when a command needs it. Values are converted by the field's schema: rich text to Markdown, an option to its string, a user to an account object, an array to a list, a number to a number.

## Sanitizing

Every string from the provider that ticketctl prints (titles, descriptions, comment bodies, labels, names, link summaries, error messages from the provider) passes one sanitizer: control and invisible characters (C0 and C1 controls except newline and tab in bodies, bidirectional overrides, zero-width characters, tag characters and unpaired surrogates) are replaced by visible markers of the form `<U+XXXX>`, and text longer than a field's cap is cut with `truncated` recorded in `incomplete`. ticketctl does not escape Markdown or fence text; that is the consumer's job (architecture §13.2 rule 1). A key, account id or other identifier that fails its grammar is rejected, not sanitized.

## Text conversion

### Reading

The provider's rich text (ADF) is rendered to Markdown. Mentions render as `@name`, inline cards as their URL, media as `[attachment: <id>]`, and every unknown node type as `[unsupported: <type>]` followed by any plain text inside it; the renderer never raises on unknown input and bounds nesting depth (exceeding it renders `[too deep]`). A link whose real `href` differs from its visible text renders as `text (href)`. Only `http`, `https` and `mailto` links keep their target; any other scheme renders as text with `[unsafe link removed]`. Text content is sanitized as above.

### Writing

Bodies are Markdown from `--body-file <path>` or `-` (stdin). A short `--body <text>` exists for humans and is treated as untrusted like the file form. ticketctl converts the Markdown to ADF with its own converter, supporting paragraphs, headings, bullet and numbered lists (nesting bounded), code blocks (with an optional language word), block quotes, inline code, bold, italic, and links whose target is `http` or `https`. Everything else, including `@` mentions, `[~accountid:...]`, Jira wiki markup, raw HTML and images, stays literal text in ADF text nodes, which carry no markup. The converter never creates mention, inline-card, media, panel or status nodes. `--format adf` accepts a prepared ADF document and validates it against the same node and mark allowlist; a document with any other node is refused (exit 22, `reason: node_not_allowed`). The provider's wiki-markup string API is never used, because it parses `*x*`, `h2.`, `#` lists, `{code}` and `[~accountid:...]` out of a body.

A body longer than `limits.body_chars` (default 30000 characters of Markdown source; the provider refuses a comment of 32768 or more characters of text with `CONTENT_LIMIT_EXCEEDED`) is refused with exit 22 (`body_too_large`); it is never truncated or split into several comments silently. An empty body is refused (`empty_body`). The provider does not sanitize written ADF (it stores `javascript:` links and control characters as given), so the converter's link-scheme allowlist and the control-character replacement are the only defenses. A control or invisible character in a body is replaced as in Sanitizing before conversion.

### Attribution

With `attribution.enabled` (default true) a comment or a created ticket's description ends with a footer line from `attribution.template` (plain text; no mentions or links). For an `auto:` actor the footer is mandatory and cannot be turned off; for a human actor it can. The footer is part of the body the human confirmed.

## Invoking the provider

All HTTP goes through `src/ticketctl/gateway.py`, a stdlib client. Requests use the selected site's URL, HTTP basic authentication with the site's email and token (the only place the token is placed, as the `Authorization` header), TLS verification on, no redirect to another host (a cross-host redirect is a failure), and a fixed `User-Agent`. The provider API used is Jira Cloud REST v3 plus the agile API; the endpoints are listed in [Jira endpoints](#jira-endpoints). The token never appears in argv, environment of a child, logs, audit, output or hook payloads.

### Jira endpoints

| Purpose | Endpoint |
|---|---|
| identity | `GET /rest/api/3/myself` |
| read | `GET /rest/api/3/issue/{key}?fields=...&expand=...` (always with `fields`) |
| search | `POST /rest/api/3/search/jql` (`nextPageToken` paging), `POST /rest/api/3/search/approximate-count`, `POST /rest/api/3/jql/parse` |
| comments | `GET /rest/api/3/issue/{key}/comment` (`orderBy`, `startAt`, `expand=properties`), `POST` the same path (the create body may carry `properties`), comment properties read under `/rest/api/3/comment/{id}/properties/{key}` |
| transitions | `GET /rest/api/3/issue/{key}/transitions?expand=transitions.fields`, `POST` the same path |
| edit | `PUT /rest/api/3/issue/{key}`, `GET /rest/api/3/issue/{key}/editmeta` |
| create | `POST /rest/api/3/issue`, `GET /rest/api/3/issue/createmeta/{project}/issuetypes[/{id}]` |
| fields | `GET /rest/api/3/field`, `GET /rest/api/3/resolution` |
| sprints | `GET /rest/agile/1.0/board`, `.../board/{id}/sprint`, `GET /rest/agile/1.0/sprint/{id}`, `POST /rest/agile/1.0/sprint/{id}/issue`, `POST /rest/agile/1.0/backlog/issue` |

The old `POST /rest/api/3/search` is not used (it returns 410 on current Jira Cloud).

## Reliability

- **Timeouts.** `timeouts.connect` (default 10 s) and `timeouts.read` (default 30 s) per site; a whole-command `--timeout` (default 120 s) bounds paging and retries; on expiry the command exits 12 `timeout`.
- **Reads** are retried up to 3 times with exponential backoff and jitter on connection failures and on 429, 502, 503 and 504, honoring `Retry-After` up to `limits.retry_after_max` (default 60 s).
- **Writes** are retried only when the request provably was not processed: a connection or TLS failure before the request was sent, or a 429. They are never retried after a timeout, a 5xx or a dropped connection once the request may have been sent; the command then exits 12 `outcome_unknown` with `retryable: true`.
- **Re-running a write** is the caller's retry. A `comment` or `create` re-run with the same `--dedupe-key` returns `already_posted` or `reused`; a `transition` or `update` re-reads first and reports `changed: false` if it already happened.
- **Pacing.** Within one command, when `X-RateLimit-Remaining` falls below 10% of `X-RateLimit-Limit` ticketctl sleeps briefly between requests; the values appear in `--verbose` output and in the error envelope. There is no cross-process limiter.
- **Caps.** One `search` makes at most 20 requests (validation, count and page loop) and returns at most 500 tickets.
- **Version drift.** Unknown fields in a response are ignored, a 410 or 404 on a documented endpoint exits 12 `provider_changed`, and the audit and log never depend on a response shape.

## Write flow

Every write (`transition`, `comment`, `create`, `update`, `sprint add|remove`) follows one order.

1. **Resolve.** Select the site, read the ticket (and `editmeta`, `createmeta` or the transitions list as the action needs), and apply config preconditions. Failures here are exit 22 (`invalid_request`, with a `reason` and the offending field or option), 21 or 20.
2. **Plan.** Build the plan object (below) and its `plan_sha256`.
3. **`--dry-run`** prints the plan and stops. It needs no `--yes`, takes no lock, calls no hook and writes nothing, not even audit.
4. **Confirm.** At a TTY the human sees the plan and confirms once (default no). Non-interactive without `--yes` exits 13 with `reason: confirmation_required` and nothing sent; the caller records the pending confirmation.
5. **`pre-` hook.** The hook for the action runs (see [Hooks](#hooks)); a veto exits 11, an escalation exits 13 with `reason: escalated` and the hook name.
6. **Stale check.** With `--yes` the caller must pass `--expect-plan <plan_sha256>` (exit 2 without it, so automation names what it was shown); with a TTY confirmation the check is implicit. ticketctl re-reads the ticket and recomputes the plan; a different hash exits 20 `stale_ticket`, with the new plan in the error details.
7. **Audit and send.** Append the `attempted` audit record (failure to append exits 1 and sends nothing), perform the provider calls in order, append the outcome record, run the `post-` hook, and print the result.

`--yes` never overrides a refusal (exit 20).

### Plan object

`schemas/cli/plan.json`: `action`, `site`, `key` (null for create), `actor`, `steps` (an ordered list of provider calls in plain terms: `{kind: assign|transition|set_fields|add_comment|create|sprint_add|sprint_remove, ...}`), `changes` (per field: `name`, `from`, `to`; rich-text values as byte size and sha256 only in the canonical hash input, shown in full to the human), `warnings`, `dedupe_key`, `plan_sha256`. The hash covers the canonical JSON of the action, site, key, the current values of every field the plan changes (so a changed ticket changes the hash), the steps, the sha256 of each body, and the dedupe key; it excludes the actor and timestamps.

### Result object

`schemas/cli/result.json`: `action`, `site`, `key`, `changed` (bool), `result` (`ok`, `partial` or `unchanged`), `steps` (each with `status`: `done`, `skipped`, `failed`), `already_posted`, `reused`, `comment_id`, `attempt_id`, `decision_ids`. `partial: true` means an earlier step was applied and a later one failed; nothing is rolled back and the result names what was applied.

### Transition order

`transition KEY --to <status>` resolves the transition from the live list by target status name, case-insensitively. No match is exit 20 `no_such_transition` listing the available targets; several matches exit 21 `ambiguous_transition`. `--transition-id <id>` bypasses the name match. A ticket already in the target status exits 0 with `changed: false` and no write. If the target's category is `done`, a resolution is required: `--resolution <name>` or `sites.<site>.transitions.default_resolution` (exit 22 `needs_resolution` otherwise), validated against the transition's allowed values (and, when they are not listed, against the provider's resolution list). The provider accepts a done transition with no resolution and leaves it empty, so ticketctl never sends one without it.

When the provider requires an assignee for the transition and the ticket has none (the provider's error is a 400 that the transition body cannot satisfy), the plan includes an explicit assign step first: `--assign me|<account>` or `sites.<site>.transitions.assign_on_start: me`. Without either, the pre-check exits 20 `needs_assignee` before sending. The assign and the transition are two calls and are not atomic; the result reports `partial` if the second fails. Other required transition fields are passed with `--field name=value` and validated against the transition's field list.

### Dedupe

Both `comment` and `create` accept `--dedupe-key <string>` (a short identifier chosen by the caller, up to 128 bytes, never secret). The provider has no idempotency, so ticketctl records the key on the object it creates.

- **Comment.** The key's sha256 is stored as the comment property `ticketctl.dedupe` (a JSON object `{"v": 1, "key_sha256": "..."}`), sent in the same request that creates the comment, so the comment and its property are written together. A re-run lists the ticket's comments with `expand=properties` and looks for the property among the author's own comments; a match returns `already_posted: true` with the existing comment id and posts nothing. A comment posted without a key is never deduplicated. The property key is fixed by ticketctl (the provider does not check key characters, so ticketctl never builds a key from caller text).
- **Create.** The key is stored as a label `tc-<first 16 hex characters of the key's sha256>` set in the create request. A re-run searches the site for that label first (`labels = "tc-..."`, in the target project) and returns the existing ticket with `reused: true`. A label became searchable about one second after it was set in the spike; the window between the search and the create (that index lag, and two concurrent creates) can duplicate a ticket, a documented residual risk. Issue properties and comment properties are not searchable (a JQL property clause returns no issues and no error), so they are never used as a lookup key.

A dedupe key is never reused for different content: the stored hash covers the key only, so a caller that wants a new comment uses a new key.

## Commands

All commands accept the [global flags](#global-flags) where meaningful. Every command that outputs data supports `--json`.

### `whoami`

Prints the selected site, the resolved account id and display name (from `me.jira` or the cached `/myself`). `--refresh` ignores the cache. Schema `schemas/cli/whoami.json`.

### `get KEY...`

Reads one or more tickets (up to 50 per call, one request each, issued with a small concurrency of 4). Options: `--fields a,b` (limits the output and the provider request), `--comments N` (the latest N, default 0), `--include-adf`, `--expand links|attachments|changelog`. A missing ticket in a multi-key call is reported under `errors` with exit 10 only if every key was missing; otherwise exit 0 with the failures listed and `incomplete`. A single missing ticket exits 10. Schema `schemas/cli/get.json`.

### `search`

Filters in three forms, combined with AND:

1. `--query <name>`: a named query from `sites.<site>.queries.<name>` (a JQL string in the user's own config).
2. Structured flags: `--project`, `--status-category new|indeterminate|done`, `--status`, `--assignee me|none|<account>`, `--sprint open|future|<id>`, `--label`, `--updated-since <ts|duration>`, `--parent <key>`, `--type`, each repeatable where it makes sense. ticketctl quotes and escapes the values; a caller never builds JQL text from text it did not write.
3. `--jql-file <path>|-` (and a short `--jql <text>` for humans), treated as untrusted.

Also `--order-by <field>[:asc|:desc]` (including `rank`), `--limit N` (default 50, hard cap 500), `--fields`, `--count`, `--comments N`. The compiled JQL is first validated through the provider's parse endpoint with strict validation; a syntax error, an unknown field, an unknown status or option value or an unknown function exits 22 `invalid_query` naming the offending token, because the provider otherwise answers a semantic error with an empty result. An empty result is therefore always a real zero. Paging is internal; `truncated` says whether `--limit` cut the list. Default output fields: `key`, `title`, `status`, `assignee`, `priority`, `estimate`, `sprints`, `parent`, `updated`, `labels`, `type`. With `fields=*all` the provider silently returns 100 per page, so ticketctl never asks for it. Schema `schemas/cli/search.json`.

### `comments KEY`

Lists comments (`--limit`, `--since <ts>`, `--newest-first`, `--own`), paged with `startAt`. Schema `schemas/cli/comments.json`.

### `transitions KEY`

Lists the transitions available now, with required fields. Schema `schemas/cli/transitions.json`.

### `sprints`

`sprints list [--state active|future|closed] [--board <id|name>]` and `sprints show <id>`. The board comes from `--board`, else `sites.<site>.board`, else the one scrum board of the project; several boards exit 21 `ambiguous_board`. Schema `schemas/cli/sprints.json`.

### `fields`

`fields list` prints the discovered fields with their schema and the logical mapping; `fields check` validates the configured mappings against them (exit 22 on a problem); `fields refresh` replaces the cache. Schema `schemas/cli/fields.json`.

### `sites`

Prints the configured sites, their `projects` and the site a key would resolve to (`sites resolve KEY`). No network. Schema `schemas/cli/sites.json`.

### `validate`

`validate` checks the configuration (exit 2 on invalid config, naming the key). `validate --probe` also makes read-only calls: it authenticates, finds the mapped fields and (with `--query <name>` or all queries) parses each named query. Schema `schemas/cli/validate.json`.

### `transition KEY`

See [Transition order](#transition-order). Options: `--to <status>`, `--transition-id`, `--resolution`, `--assign me|<account>`, `--field name=value` (repeatable).

### `comment KEY`

`--body-file <path>|-` (or `--body`), `--format markdown|adf`, `--dedupe-key`. The comment is posted as the authenticated account.

### `create`

`--project <key>` (or the site's only `projects` entry), `--type <name>`, `--title <text>` or `--title-file`, `--body-file`, `--parent <key>`, `--assign me|none|<account>`, `--label` (repeatable), `--set name=value` (repeatable), `--dedupe-key`. The type is validated against createmeta; required fields the project demands that are not supplied exit 22 `missing_required_fields` listing them. The result carries the new key and `reused: true` when a dedupe match was found.

### `update KEY`

`--title`, `--assign me|none|<account>`, `--add-label`, `--remove-label`, `--set name=value` (repeatable). `--set name=@file|-` reads a rich-text value from a file or stdin. Values are converted by the field's schema; an unknown or non-editable field exits 22 `unknown_field` or `not_editable` before anything is sent. Setting a value the ticket already has is a no-op (`changed: false`).

### `sprint add|remove KEY...`

`--sprint open|next|<id>`. `open` is the board's one active sprint; `next` is the nearest future sprint; several candidates exit 21. Only active and future sprints are targets; a closed sprint is refused (exit 20 `sprint_closed`). `remove` moves the tickets to the backlog.

### `export`

Prints audit records as JSON lines: `--since <ts>`, `--after-seq <n>`, `--to <path>` (appended is never done; an existing file is refused). Each record carries `seq`.

## Global flags

`--json`, `--config <path>`, `--site <name>`, `--verbose`, `--quiet`, `--timeout <seconds>`, `--dry-run` (writes), `--yes` (writes; needs `--expect-plan` for automation), `--expect-plan <sha256>`, `--actor <string>`, `--decision-id <id>` (repeatable), `--override-escalation[=<scope>]` (human-only; the scope is `ticketctl`), `--hook <name>=<command>` (repeatable, in addition to configured hooks), `--version`. A value that can carry untrusted text is accepted as a file or stdin, never only as argv (design guide §4.1): bodies, titles, JQL and `--set` rich-text values.

## Exit codes

| Code | Meaning |
|---|---|
| 0 | success (including `changed: false`, `already_posted`, `reused`) |
| 1 | unexpected or internal error (including a failed audit append) |
| 2 | usage error, invalid config, `--yes` without `--expect-plan` from automation, no site selected |
| 10 | not found (a ticket, a sprint, a board) |
| 11 | vetoed by a pre-hook |
| 12 | failed for a reportable reason: `auth_required`, `bad_credentials`, `forbidden`, `rejected` (the provider refused the write), `unavailable`, `timeout`, `outcome_unknown`, `rate_limited`, `provider_changed` |
| 13 | needs a human: `confirmation_required`, `escalated` |
| 20 | refused: `stale_ticket`, `no_such_transition`, `needs_assignee`, `sprint_closed`, `already_exists` |
| 21 | ambiguous: `ambiguous_site`, `ambiguous_field`, `ambiguous_transition`, `ambiguous_board`, `ambiguous_sprint` |
| 22 | `invalid_request`: the input failed ticketctl's own validation before anything was sent |

Exit 22 carries a `reason`: `invalid_query`, `unknown_field`, `not_editable`, `invalid_value`, `needs_resolution`, `missing_required_fields`, `node_not_allowed`, `body_too_large`, `empty_body`, `unknown_site`, `invalid_key`.

## Output by exit code

With `--json`, exit 0 prints the command's object; a failure prints the error envelope on stdout (and the message on stderr). Without `--json`, human text goes to stdout and errors to stderr.

### Error envelope

`schemas/cli/error.json`: `{"error": {"code", "message", "reason", "retryable", "retry_after_s", "outcome_unknown", "site", "key", "details"}}`. `details` carries the structured part: candidates for an ambiguity, the available targets for `no_such_transition`, the offending field for exit 22, the new plan for `stale_ticket`, the hook name for a veto or escalation, the provider's `errorMessages` and `errors` (sanitized, untrusted) for `rejected`. The provider answers errors in three body shapes (`errorMessages` with `errors`, `{"error": ...}` and plain text); the gateway maps all of them to this envelope.

## Hooks

Per kind of write, one `pre-` and one `post-` hook: `pre-transition`, `post-transition`, `pre-comment`, `post-comment`, `pre-create`, `post-create`, `pre-update`, `post-update` (including assign and label changes), `pre-sprint`, `post-sprint`. Reads have no hooks. Hooks follow the design guide §8 (own process group, JSON on stdin, no templates, timeout default 30 s, environment minus `token_env`).

- **`pre-`** runs after the plan and the confirmation and before the first provider write (and not for `--dry-run`). Exit 13 escalates (the command exits 13 `escalated`, naming the hook; a caller confirming later passes `--override-escalation=ticketctl`, which overrides only ticketctl's hooks); any other non-zero exit vetoes (exit 11, the hook's stderr as the reason, sanitized).
- **`post-`** runs once after the outcome is known (`ok`, `failed` or `partial`), best-effort, never fatal, `async: true` optional.
- **Payload** (`schemas/hooks/<hook>.json`): `tool`, `hook`, `object_type` (`ticket`), `object_id` (the key, or null for `pre-create`), `site`, `project`, `actor`, `decision_ids`, `plan` (action, current and target status and category, the names of the fields changed and never their values, the resolution, whether an assign step is included, `plan_sha256`, `dedupe_key`), `body` (byte size and sha256 of each body, never text), and for `post-` also `result`, the provider ids written and `changed`. There is no `repo` and no `labels` (ticketctl has no repository notion and stores no objects). Hook payloads carry no ticket text: no title, description, comment or label text. A policy that needs text reads it with `get`.
- **Environment:** `TICKETCTL_HOOK`, `TICKETCTL_ID` (the key, or empty for create), `TICKETCTL_OBJECT_TYPE=ticket`.
- A returned `decision_id` is recorded in the step's audit records.

## Config

`~/.config/ticketctl/config.yaml`, drop-ins `config.d/*.yaml`, `--config` overrides (design guide §5). Schema [`schemas/config.json`](../schemas/config.json); every key is documented with its default in `config.example.yaml`. Invalid config fails fast naming the key.

| Key | Default | Meaning |
|---|---|---|
| `sites.<name>.provider` | `jira` | the only value in 0.x |
| `sites.<name>.url` | required | `https` site URL (`allow_http` for a local test server only) |
| `sites.<name>.email`, `email_env` | required, one of | the account email; not a secret |
| `sites.<name>.token_env` | required | name of the environment variable holding the API token |
| `sites.<name>.projects` | `[]` | project keys this site owns (site selection) |
| `sites.<name>.board` | none | board id or name for sprint commands |
| `sites.<name>.fields` | `{}` | logical field mappings and `extra` |
| `sites.<name>.queries` | `{}` | named JQL queries |
| `sites.<name>.transitions.default_resolution` | none | resolution used for done targets |
| `sites.<name>.transitions.assign_on_start` | none | `me` to add an assign step when needed |
| `sites.<name>.cache_ttl` | `24h` | field-list and identity cache lifetime |
| `sites.<name>.timeouts.connect`, `.read` | `10s`, `30s` | per site |
| `default_site` | none | used when no other rule selects |
| `me` | `{name: <OS user>}` | `{name, jira}` (the account id) |
| `attribution.enabled`, `.template` | true, a plain footer | see Attribution |
| `limits.body_chars` | `30000` | body size cap in characters |
| `limits.retry_after_max` | `60s` | longest `Retry-After` honored |
| `hooks` | `{}` | per hook, ordered `{cmd, timeout, async}` lists |

Per-repo overrides do not exist: ticketctl has no repository notion, and its settings are scoped by site (the position `repos:` holds elsewhere). Structured config (`sites`, `hooks`) cannot be set from environment variables.

### Credentials

The token comes only from the environment variable named by `token_env`. A missing or empty variable exits 12 `auth_required` naming the variable. A 401 exits 12 `bad_credentials`; a 403 on a write exits 12 `forbidden`. There is no token command and no stored login. Hooks and child processes get the environment minus every configured `token_env` variable. The token is never written to state, logs, audit, output or hook payloads. Callers that run ticketctl inside a Claude session must list the token variable in claude-session's `secret_env`.

## Environment variables

Top-level scalar config values accept `TICKETCTL_<KEY>` (`TICKETCTL_TIMEOUT`, `TICKETCTL_DEFAULT_SITE`, ...); structured keys do not. Hook variables are listed under Hooks. The token variable's name is whatever `token_env` says.

## Labels

None: ticketctl creates no long-lived objects and stores none. (The `tc-` label it adds to a created ticket is provider data, described under Dedupe.)

## TUI

None in 0.x.

## Files

Under `~/.local/state/ticketctl/` (respecting `XDG_STATE_HOME`; the same path on macOS):

- `cache/<site>/fields.json`, `cache/<site>/me.json`: replaced atomically, refetched when missing, corrupt or older than `cache_ttl`; never holds credentials.
- `audit.jsonl`: public, append-only (schema `schemas/audit.json`), kept forever.
- `log.jsonl`: public request log (schema `schemas/log.json`), rotated at 10 MiB keeping 3 files.

Appends take an `flock`. There is no database, no `cron` command and no `reconcile` or `prune`: nothing needs maintenance.

## Audit and log

**Audit.** Every write appends two records sharing an `attempt_id`: `result: attempted` before the first provider call, and the outcome after. A record with no outcome means the process died in between. If the `attempted` append fails, nothing is sent and the command exits 1. A multi-step write (an assign and a transition) has one record pair per provider call, sharing the command's `attempt_id` and a `step` number. Fields: `seq`, `ts`, `actor`, `actor_source`, `stdin_tty`, `decision_id` (a list), `target` (`{site, key}`), `action`, `inputs` (`plan_sha256`, body sha256 and byte size, `dedupe_key`, the config hash), `result` (`ok`, `failed`, `partial`, `attempted`), `provider_ids` and `changed`. No body text, no titles, no secrets.

**Log.** One line per provider call: `ts`, `site`, `method`, a path template (`/issue/{key}`, no values), `status`, `duration_ms`, `retries`, `rate_limit_remaining`. No headers, no bodies. A failed log write warns and never fails the command.

## Upgrades

The interface version is semver; before 1.0 a breaking change is a `feat` whose title names the break. The cache has a `schema_version`; an unknown version is discarded and refetched. `audit.jsonl` and `log.jsonl` gain fields additively only.

## Residual risks

- A provider validator can still reject a write after the pre-check passes (`rejected`, exit 12).
- Two concurrent creates with the same dedupe key can both succeed (index lag and the search-then-create window).
- `plan_sha256` guards against a changed ticket, not against a hostile same-user process: the `actor` claim is not authenticated.
- Jira has no optimistic locking: a write that changes a field another writer changed in the last milliseconds is last write wins per field, and the stale check narrows but does not close that window.
- An assign plus a transition is not atomic.
- Per-user transition filtering, 429 behavior and scoped API tokens were not verified live; the retry rules are conservative because of that.
- Ticket text is untrusted; ticketctl sanitizes control characters but does not fence.

## Platform notes

Pure standard library; Linux, WSL and macOS behave the same. Python 3.12 and 3.13. Timestamps are normalized to UTC; local time is never printed. There is no sandbox or process work in ticketctl.
