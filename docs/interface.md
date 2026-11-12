# ticketctl: interface

Status: 0.1.0-design. Written before implementation (architecture §18.1). This document is the single normative source for ticketctl's rules; `docs/design.md`, the README and the architecture link to its headings and never restate its tables. The `--json` shapes, hook payloads, config, audit and log records are specified in [schemas/](../schemas/).

ticketctl is normalized ticket access for one ticket provider at a time. Jira Cloud is the only provider in 0.x. It reads tickets, searches them, lists transitions, comments, and performs writes (`transition`, `comment`, `create`, `update`, sprint add and remove) through one confirm-by-default write flow. It is stateless: it keeps a small cache and its audit and request logs, and no database.

## Concepts

### Identifiers

A ticket is identified by its key (`KEY-123`: an upper-case project key, a hyphen and a number). Input is case-insensitive and printed upper-case, because Jira resolves lower-case and numeric ids to the canonical key. A ticket may also be given as a browse URL; the rules are in [Site selection](#site-selection). `--json` output always uses the key plus a `site` field, never a URL as the identity. A ticket whose canonical key differs from the requested one (a moved ticket) carries `requested_key`; write results and audit use the canonical key.

Keys are not unique across sites. A key is resolved to a site by [Site selection](#site-selection); a command never guesses.

Every identifier that reaches a URL path, JSON, the audit or a hook payload has one grammar, and every grammar is matched with `fullmatch` (a trailing newline never passes). A ticket key, project key, site name or browse URL that fails its grammar exits 22 (`invalid_key`, `invalid_value` or `unknown_site`); the shared flag grammars (actor, decision id, dedupe key, numeric ids, durations, integers) exit 2 `invalid_argument` (see [Commands](#commands)); an identifier in a provider response is dropped from its object and named in `incomplete`. Every URL path segment is built by one `segment()` helper that checks the grammar and percent-encodes with no safe characters, and every query parameter by one `query()` helper (urlencode, no safe characters). A `urlsplit` `ValueError` is exit 22 `invalid_value`. Cache entries are validated on load and discarded on a mismatch.

| Identifier | Grammar |
|---|---|
| ticket key | `^[A-Z][A-Z0-9_]*-[0-9]+$` |
| project key | `^[A-Z][A-Z0-9_]*$` |
| site name | `^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$` |
| provider ids (comment, field, property) | `^[A-Za-z0-9_.:-]{1,64}$` |
| numeric ids (transition, sprint, board) | `^[0-9]{1,18}$` |
| custom field id | `customfield_[0-9]+` |
| account id | `^[A-Za-z0-9:_.-]{1,128}$` |
| decision id and dedupe key | `^[A-Za-z0-9._:@/-]{1,128}$` |
| actor | `^(human\|auto):[A-Za-z0-9._@-]{1,64}$` |
| user time zone (`/myself`) | `^[A-Za-z0-9_+/-]{1,64}$`; a cache entry that fails it is discarded |

Property keys are fixed by ticketctl and never built from caller text. `me.name` (and so the default actor) is normalized to the actor pattern at config load (disallowed characters become `_`, truncated to 64); an `--actor` that fails the pattern exits 2 `invalid_argument`.

### Site selection

A command picks its site with one table. The inputs are tried in this order and the first that selects a site wins:

| Order | Input | Result |
|---|---|---|
| 1 | `--site <name>` | an unknown name exits 22 `unknown_site` |
| 2 | a browse URL | the URL's lower-cased host (and port) must equal a configured site's, else exit 22 `unknown_site`; more than one configured site with that host and port exits 21 `ambiguous_site` listing them (`--site` resolves); a differing `--site` exits 22 `invalid_value` ("url and --site disagree") |
| 3 | a key, or `--project`, or the `create` project | the sites whose `projects` hold the project key: exactly one selects it, several exit 21 `ambiguous_site` listing them, none falls through |
| 4 | `default_site` | selects it |
| 5 | the only configured site | selects it |
| 6 | nothing selects | several sites exit 21 `ambiguous_site` listing the names; zero configured sites exit 2 (`invalid_config`, `reason: no_sites`) |

A browse URL must have the scheme `https` (`http` only for an `allow_http` site), no userinfo, and a path matching `^/browse/<KEY>/?$`, compared after `urlsplit`, never by substring. Its query and fragment are ignored (a pasted link often carries `?focusedCommentId=` or `#comment-1`); only scheme, host, port and path are checked, and a board or other URL form is not accepted. A command with several keys needs one site: keys, `--project` values or `--key` values that select different sites exit 22 `mixed_sites`, and `--site` forces one site for all keys. A key from an unclaimed project goes to `default_site` or the only site by design; a user who wants a refusal claims all projects. ticketctl contacts only the selected site's host, and only hosts named in config. `sites resolve KEY` prints the exit 21 envelope with the candidates when ambiguous.

### Human actions

A write is confirm-by-default. A run is interactive when stderr is a TTY, `/dev/tty` can be opened, the actor is not `auto:` and `--yes` is not given. The answer is read from `/dev/tty`, never from stdin, so a piped body does not remove the human. The default actor is `human:<me.name>` when `/dev/tty` can be opened and stderr is a TTY, else `auto:unknown`; an explicit `--actor` always wins. `--body-file -` (or any `-` source) with stdin a TTY exits 2 `invalid_argument`. `actor_source` in the audit is `flag` (an explicit `--actor`), `tty` (the default became `human:` because of the controlling terminal) or `default` (the `auto:unknown` fallback). A dry-run and an apply may use different actors; the actor is not part of the plan hash, so a plan made under `auto:` can be confirmed with `--actor human:<user> --yes --expect-plan <hash>`.

The confirmation dialog is written to stderr through the output sanitizer, in this order: every outbound text value in full (title, description, comment body, `--set` and `--field` string values, labels), with the body rendered through the reader's Markdown renderer from the final ADF so every link shows as `text (href)` and the attribution paragraph shows; then any `from` value of a change (provider-owned text), cut to 20 lines and 2000 characters with a "(N more characters, see --json)" marker; then the one-line-per-change summary and the warnings directly above the prompt. Outbound text is bounded by `limits.body_chars`, so showing it in full is safe; `--dry-run --json` carries full values. Pending input on `/dev/tty` is discarded before the prompt (`termios.tcflush(fd, TCIFLUSH)`); only a typed `y` or `yes` (case-insensitive, trimmed) accepts, and anything else, an empty line, EOF and Ctrl-C decline. `--quiet` does not suppress the dialog. A decline is exit 20 `declined` (`retryable` false, `details.plan`) with no audit record, no hook and no pending record: it is a refusal by the human, not exit 13 (which means a human must still decide), so the caller stops.

`--yes` always requires `--expect-plan` from every caller (exit 2, `reason: yes_requires_expect_plan`); a caller takes the hash from the exit 13 envelope or a dry-run. With `comment` and `create`, `--yes` also requires `--dedupe-key` (exit 2, `reason: dedupe_key_required`); an interactive human may omit it. `--dry-run` skips these two checks (not `human_only`), so `--dry-run --yes` prints the plan and is the discovery route; every other flag-combination rule still runs first.

`--override-escalation[=ticketctl]` is human-only: it requires `--actor human:<user>` or no `--actor` with the default actor `human:`, and `--yes` may accompany it. With an `auto:` actor it exits 2 (`reason: human_only`). The claim is advisory (a same-user process can say `--actor human:x`); the audit records `actor_source`, `stdin_tty` and `inputs.override_escalation` (the scope, else null), so the claim is checkable. The flag overrides the escalation of every hook ticketctl ran (configured and `--hook`), never a veto. With the flag an interactive run prompts once and an escalation is then overridden; without it an interactive escalation exits 13 `escalated` and the message names the flag to re-run with. `--decision-id` values are caller-supplied and advisory like the actor.

### Trust boundary

Allowing ticketctl in a session allows everything ticketctl can do with the user's credentials. Every control (confirmation, `--yes --expect-plan`, `pre-` hooks, the human claim, the mandatory attribution, the host list) is chosen by the caller through `--config`, `XDG_CONFIG_HOME`, `XDG_STATE_HOME` and `HOME` (which redirect config and state), `--hook`, `--actor`, `--yes`, `attribution.enabled` and the environment, so "mandatory" and "human-only" are enforced only when the caller cannot choose arguments, environment and config. A caller that can run ticketctl can name a config with another host and a token variable of its choice and so send the token there, skip hooks (`--config` with no hooks), post as the user without the attribution line, run shell commands through `--hook`, pass a dry-run's own hash to `--yes`, and post any file the user can read with `--body-file` or `@file`. `HTTPS_PROXY` and CA variables are part of the same environment. The confirmation is not a boundary against a caller that can run ticketctl with a pty (`script`, `pty.fork` answer the prompt). The only constrained deployments are a wrapper that allows reads only (every write subcommand rejected, a fixed `--config`, a scrubbed environment) or writes executed outside the session, where the token lives. `--config` is not restricted to the user's config directory (the design guide defines it and tests need it). The audit is evidence for a cooperative caller and for writes, not a tamper-evident record (architecture non-goal): a read with a foreign `--config` is not audited, though it is logged with its host unless the state directory was redirected (see [Audit and log](#audit-and-log)). There is no read-only mode inside ticketctl: it would not be a boundary.

### Unknown values

Unknown values are `null`, and a list that was cut off says so in `incomplete`. A consumer treats `null` as the safe answer: `blocked.value: null` is not "not blocked", `assignee: null` means unassigned or unknown according to `incomplete`, and a done-ness question is answered by `status.category`, never by `resolution` (a done ticket can have none).

## Normalized objects

Provider-neutral. No `customfield_N` name appears in any output except `fields list` output and the sanitized keys of a provider's `details.field_errors`: only mapped and `extra` fields do (see [Field mapping](#field-mapping)). Fields marked untrusted carry `x-untrusted` in the schemas; fencing remains the consumer's job, but ticketctl has already replaced control and invisible characters with visible markers (see [Sanitizing](#sanitizing)). Untrusted: title, description, `description_adf`, acceptance criteria, labels, link and parent summaries, comment bodies, attachment names, account display names, sprint names, extra string fields, plan `changes` values, error `message`, and `details.errorMessages`, `field_errors`, `errors` and `hook_stderr`. Status, type, priority and resolution names are workflow configuration, not ticket text, and are not marked (they still pass the sanitizer).

### Ticket object

`get` prints `{tickets, errors}` (see [`get`](#get-key)) and `search` prints the [Search result](#search-result); both hold the ticket shape of `schemas/defs.json#ticket`. `--fields` takes a comma list of the property names below and only property names (`fields` selects the configured extras, `comments` needs `--comments N` and `description_adf` needs `--include-adf`, otherwise exit 22 `invalid_value`); `key` and `site` are always present, an unknown name exits 22 `unknown_field`, and the provider request is built from the same list, so unrequested properties are neither fetched nor printed. `blocked` pulls `links` and `epic` pulls `parent`. `get` returns every property except `comments` (only with `--comments N`) and `description_adf` (only with `--include-adf`). `search` returns `key`, `title`, `status`, `assignee`, `priority`, `estimate`, `sprints`, `parent`, `updated`, `labels`, `type` by default; `due_date`, `created`, `links`, `blocked` and `epic` are requested by name.

| Property | Type | Notes |
|---|---|---|
| `key`, `site`, `url`, `id` | string | `id` is the provider's own id; `url` is the browse URL. |
| `requested_key` | string | present only when the canonical key differs from the requested one (a moved ticket). |
| `type` | `{name, subtask}` | |
| `title` | string | untrusted |
| `description` | string or null | untrusted; Markdown rendered from the provider's rich text. `description_adf` only with `--include-adf`: an opaque, untrusted object. |
| `status` | `{name, category}` | `category` is `new`, `indeterminate` or `done`. |
| `resolution` | string or null | |
| `assignee`, `reporter` | `{account_id, display_name}` or null | no email address is ever printed. `account_id` is null for an account with no id (deleted, app or anonymized users), and the display name is then `unknown user`. |
| `priority` | `{name, rank}` or null | `rank` is the zero-based position in the provider's priority list (cached, see [Files](#files)); "Not Set" is a real entry with a rank; a name not in the list has `rank: null`; `priority` is null only when the issue's field is null. |
| `estimate` | number or null | the mapped `estimate` field; null with `incomplete: ["estimate"]` when it is ambiguous (see [Field mapping](#field-mapping)). |
| `acceptance_criteria` | string or null | untrusted Markdown; null without a mapping. |
| `labels` | string[] | untrusted; labels matching `^tc-[0-9a-f]{16}$` (the dedupe labels) are never listed. |
| `sprints` | `{id, name, state, start, end}[]` | `state` is `future`, `active` or `closed`; times UTC. |
| `parent` | `{key, type, title}` or null | the issue's parent; `title` only when the provider embeds it. |
| `epic` | `{key, type, title}` or null | set only when the parent's type is an epic (hierarchy level), else null; for a sub-task null (no extra read). The mapped `epic` field fills it only on sites where the parent is not the epic. |
| `links` | `{type, direction, key, status_category, summary}[]` | `type` is the link type's name. `direction` is relative to this ticket: `inward` means this ticket is the target of the link type's inward phrase (for `Blocks`, "is blocked by"), `outward` means it is the source ("blocks"). `status_category` and `summary` may be null; `summary` is untrusted. |
| `blocked` | `{value, by}` | `value` is `true`, `false` or `null`; `by` lists the keys. See [Blocked](#blocked). |
| `due_date` | date or null | `YYYY-MM-DD`. |
| `created`, `updated`, `resolved` | UTC ISO 8601 with `Z` to the second, or null | the provider's offsets (for example `-0600`, without a colon) are parsed and normalized and its milliseconds are dropped. |
| `comments` | `{items, total, truncated}` | only with `--comments N`; untrusted bodies. |
| `attachments` | `{id, name, size, mime}[]` | metadata only; `name` is untrusted. |
| `fields` | object | the configured `extra` fields, converted by schema. |
| `incomplete` | string[] | parts that were cut off or unavailable (`links`, `comments`, `attachments`, `<part>.truncated`, `<part>.invalid_id`, `links.unreadable`, `fields.<name>`, ...). |

Text longer than a field's cap is cut at a code-point boundary, ends with the marker `<truncated>`, and the part is named `<field>.truncated` in `incomplete`; a comment object carries `truncated` (boolean). Caps: title 4096, parent and link summaries 4096, body fields 1,000,000, label 256, names 256, attachment names 1024. One ticket that fails to parse or convert becomes a stub with `incomplete`, never aborting a `search` or a multi-key `get`. `get --comments N` costs one extra `/comment?orderBy=-created` call per ticket and returns the latest N (at most 50), oldest first.

#### Blocked

`blocked.value` is `true` when any `inward` link of a type listed in `sites.<name>.blocking_link_types` (default `["Blocks"]`, matched by link type name, case-insensitively) has an other end whose `status_category` is not `done`, even when the list was cut off (`true` wins over `null`). A null category counts as not done: the key is listed in `by` and `incomplete` gains `links.unreadable`. Otherwise it is `false` only when links were requested, none was cut off and none qualifies, and `null` when `links` was not requested or was cut off. It is computed from the links embedded in the same read; no extra calls.

### Comment object

`schemas/cli/comments.json`: `id`, `author` (`{account_id, display_name}`), `own` (true when the author is the resolved `me`), `created`, `updated` (equal to `created` when never edited), `body` (untrusted Markdown), `truncated`, `dedupe_key_sha256` and `body_sha256` (from the ticketctl property when present, else null), `authored_by_ticketctl` (true when the property is present on an own comment) and `actor_kind` (`auto`, `human` or null). Listed oldest first by default; `--newest-first` reverses.

### Transition object

`schemas/cli/transitions.json`: `id`, `name`, `to` (`{name, category}`), `required_fields` (`{name, kind, allowed_values?}[]`, where `kind` is the provider's field type name, a free string of at most 64 characters), `has_screen`. Transitions are global on the supported provider (the same ids apply from every status), but ticketctl always reads the live list for the ticket and never relies on that.

### Sprint object

`schemas/cli/sprints-list.json`: `id`, `name`, `state` (`future`, `active` or `closed`), `start`, `end` (null for a future sprint), `board` (the origin board id, an integer; a listing by board may show sprints whose origin differs). The selector word `open` means state `active`; `sprints list --state` takes the state names.

### Search result

`schemas/cli/search.json`: `{tickets, truncated, next, count}`. `truncated` is true whenever more matches exist than were returned (`--limit`, the 500 cap or the request cap stopped the loop) and `next` is then set; `next` is the opaque cursor that `--cursor` accepts, else null; `count` is approximate (from the approximate-count endpoint) and present only with `--count`.

### Field mapping

Logical field names are fixed: `estimate`, `acceptance_criteria`, `sprint`, `parent`, `epic`, `labels`, `priority`, `assignee`, `resolution`, `due_date`. Each site maps a logical name to a provider field by `{name}`, `{id}` or `{schema}` under `sites.<site>.fields`, and `fields.extra.<name>` adds more fields to the output. The schema string is `schema.custom` (the `type` for system fields).

- **Discovery without a mapping.** `sprint` is the field whose `schema.custom` ends `:gh-sprint`. `estimate` is the union of fields whose `schema.custom` ends `:jsw-story-points` and fields with `schema.custom` `…customfieldtypes:float` and the exact name `Story Points` (the legacy points field has only the generic float schema). `epic` uses `:gh-epic-link`. `acceptance_criteria` has no default: null and no error unless mapped, and `incomplete` names it only when requested. The field list is cached ([Files](#files)).
- **Ambiguity is lazy.** More than one candidate for a logical field exits 21 `ambiguous_field` only when the requested output or a write needs it. A read that requests it (default `get` includes `estimate`) prints the property as null with `incomplete: ["estimate"]` and warns; a write to it exits 21.
- **Names for `--set` and `--field`.** `--set` and `--field` never accept `assignee`, `sprint`, `resolution`, `labels` or `title` (each has a flag or a command); `description` is accepted on `update` only (on `create` the body is `--body-file`); `parent` is accepted except on `create` (which has `--parent`). A refused name, or a name given twice, exits 2 `invalid_combination` and the message names the flag or command. The other names resolve in order: the logical names left, the `extra` names, then a unique exact display name from the metadata (case-insensitive), the metadata being `editmeta` for `update`, the transition's field list for `--field` and `createmeta` for `create`. A `customfield_N` id is refused (`unknown_field`) unless mapped in config; an ambiguous display name exits 21 `ambiguous_field`.
- **Missing.** A mapping that names a missing field is reported as the `missing` problem by `fields check` and `validate --probe` (both exit 0), and a command that needs it exits 22 `unknown_field`.
- **Conversion by schema.** Rich text (ADF) to Markdown, an option to its string, a user to an account object, an array to a list, a number to a number, date and datetime to ISO strings, labels to a list, an array of objects to a list of their `name` or `value`. Any other schema type gives null with `incomplete: ["fields.<name>"]`.

#### Value grammar

`--set name=value` and `--field name=value` split at the first `=`. The value is inline text, `@path` (the whole file; one trailing newline is stripped for scalar types) or `-` (stdin). Mismatched forms exit 22 `invalid_value`. Allowed values are checked when the metadata lists them, else the provider decides.

| Schema type | Accepted value |
|---|---|
| string | as given |
| number | `^-?[0-9]+(\.[0-9]+)?$` |
| date | `YYYY-MM-DD` |
| datetime | RFC 3339 with `Z` or an offset |
| option | the option name, matched case-insensitively against the allowed values (several matches or none: exit 22 `invalid_value` listing candidates) |
| array of option or string | a JSON array of strings, or a bare value meaning a one-element list |
| user | `me`, `none` or an account id |
| rich text | only `@path` or `-`, converted like a body (an inline value is exit 22 `invalid_value`) |

An empty value (`name=`) clears an optional field (null; an empty list for arrays). For an `auto:` actor a rich-text value is refused (see [Attribution](#attribution)).

## Sanitizing

Every string that leaves the process (`--json` values, stderr, `--verbose` output, hook stderr relayed in an envelope, `export`) and every outbound string value (title, label, field string, body text node) passes one sanitizer, whatever its origin: provider text, caller input echoed in an error, file paths. It replaces by `<U+XXXX>` every code point in the categories Cc (except `\n` and `\t` in body-class fields), Cf, Cs, Co, Cn, Zl and Zp, plus U+034F, U+115F, U+1160, U+16FE4, U+17B4, U+17B5, U+180B to U+180F, U+2800, U+3164, U+FE00 to U+FE0F, U+FFA0, U+E0100 to U+E01EF and U+FFFC. The body-class fields are a closed list: descriptions, comment bodies, acceptance criteria, rendered ADF and rich-text `changes[].from` and `to`; everything else, including `hook_stderr`, extra string fields and error text, is single-line. A lone `\r` and CRLF are first normalized to `\n` in body-class fields; in single-line fields `\n`, `\r` and `\t` become one space. No Unicode normalization is applied (it would change provider names that consumers compare by equality). The `<U+XXXX>` marker is not escaped in input, so a consumer must not read a marker as proof of anything. Over-long text is cut as described under [Ticket object](#ticket-object). ticketctl does not escape Markdown or fence text; that is the consumer's job (architecture §13.2 rule 1). A key, account id or other identifier that fails its grammar is rejected, and the rejection message prints the sanitized value cut to 64 characters. The rule is category based, so joiner and variation-selector sequences (emoji, Persian text that needs U+200C) are shown and written with markers (see [Residual risks](#residual-risks)).

## Text conversion

### Reading

The provider's rich text (ADF) is rendered to Markdown with one list of rules:

- Blocks: `table` as pipe-table rows (header from the first row, at most 200 rows, cells flattened), `taskList` and `taskItem` as `- [ ]` and `- [x]`, `hardBreak` as a newline, `rule` as `---`, `panel`, `expand` and `nestedExpand` as a block quote with the title first.
- Inline: `emoji` from `attrs.text` else `:shortName:`, `status` and `date` as their text, `mention` as `@name`, marks `strong`, `em`, `code` and `strike` as Markdown; other marks are dropped.
- Links: a link whose real `href` differs from its text renders `text (href)` with the href percent-encoded for `)`, spaces and control characters. `inlineCard`, `blockCard` and `embedCard` render the URL under the same rule. Only `http`, `https` and `mailto` keep their target; any other scheme renders `[unsafe link removed]`.
- Media nodes render `[attachment: <id>]` when the id matches `^[A-Za-z0-9-]{1,64}$`, else `[attachment: ?]`. An unknown node renders `[unsupported: <type>]` (the type when it matches `^[A-Za-z]{1,32}$`, else `unknown`) followed by its plain text.
- Budgets: depth 50 (`[too deep]`), 50,000 nodes and 1 MiB of output (`[too large]`, and `incomplete` names the part). The renderer never raises. Text is not escaped (a literal `*` and a bold mark look the same); the consumer fences.

### Writing

Bodies are Markdown from `--body-file <path>` or `-` (stdin). `comment` also takes a short `--body <text>` for humans, treated as untrusted like the file form. ticketctl converts the Markdown to ADF with its own converter. The dialect is a CommonMark subset: paragraphs, ATX headings, bullet and ordered lists (nesting 5), fenced code blocks (with a language word), block quotes, inline code, bold, italic, `[text](http(s)://...)` links, and backslash or two-space hard breaks. Tables, task lists, strikethrough, images, autolinks, raw HTML, `@` mentions, `[~accountid:...]` and Jira wiki markup stay literal text in ADF text nodes. The converter never creates mention, inline-card, media, panel or status nodes, and mark spans never nest links. `--format adf` accepts a prepared ADF document. Every rich-text value from any path (a body, a description, a `--set` or `--field` value) ends in one function `validate_adf()` against the [ADF allowlist](#adf-allowlist). The provider's wiki-markup string API is never used, because it parses `*x*`, `h2.`, `#` lists, `{code}` and `[~accountid:...]` out of a body.

The size is measured on the final document: the sum of the characters of all text nodes of the ADF that will be sent, after sanitizing, after the attribution line and after conversion, must be at most `limits.body_chars` (default 30000, schema maximum 32767, the comment limit verified for a single text node; the total across nodes is assumed, and the same cap applies to descriptions and rich-text custom fields, whose limit is unmeasured). Over is exit 22 `body_too_large`, never truncated or split into several comments; an empty body (no text, judged before the attribution paragraph is added) is `empty_body`. The hash of a body (plan, hook payload, audit, dedupe `body_sha256`) is the sha256 of the final ADF as canonical JSON (sorted keys, separators `,` and `:`, UTF-8, no ASCII escaping), for Markdown and `--format adf` alike. The provider does not sanitize written ADF (it stores `javascript:` links and control characters as given), so the allowlist and the sanitizer of [Sanitizing](#sanitizing) are the only defenses.

#### ADF allowlist

`schemas/adf.json` is the JSON Schema of the allowlist. Allowed nodes: `doc` (version 1), `paragraph`, `text` (non-empty), `heading` (`level` 1 to 6), `bulletList`, `orderedList` (`order` an integer 1 to 9999), `listItem`, `codeBlock` (`language` matching `^[A-Za-z0-9+#._-]{1,32}$`), `blockquote`, `hardBreak`. Allowed marks: `strong`, `em`, `code`, and `link` (`href` matching `^(?!https?://[^/?#]*@)https?://[\x21-\x7e]{1,2000}$` with `fullmatch`: printable ASCII only, userinfo refused, no other attributes). The converter percent-encodes non-ASCII path characters and converts a non-ASCII host with the `idna` codec (a failure is exit 22 `invalid_value`); homograph hosts are a residual risk. Every other node, mark, attribute or attribute value is refused and never repaired (exit 22 `node_not_allowed`, `details.field` naming the path). Limits that the schema cannot express: depth 10 (list nesting 5) and 5000 nodes. Text nodes pass the sanitizer.

### Attribution

Every comment ticketctl posts carries the comment property `ticketctl.meta` with `{"v": 1, "actor_kind": "auto"|"human", "body_sha256": ..., "key_sha256"?: ...}` (the key part only when a dedupe key was given), written in the create request itself. ADF has no hidden text, so awkit-authored ticket comments are detected by this property on the user's own comments, a recorded deviation from the design guide's fixed marker. A comment posted through a transition `Comment` field carries no property and no dedupe.

With `attribution.enabled` (default true) a comment or a created ticket's description starts with a first paragraph from `attribution.template` (plain text; no mentions or links), so no untrusted text can hide it. For an `auto:` actor it is mandatory and cannot be turned off; for a `human:` actor it is optional. The single template says "Drafted with AI assistance" for both actor kinds and so does not state that a human accepted the post (the audit `actor` and `actor_source` do). The actor string is the only test, and the claim is advisory (see [Trust boundary](#trust-boundary)). The line is part of the body the human confirmed. One rich-text rule for an `auto:` actor: the only rich-text write paths are a comment body and a created ticket's description (`--body-file`); a rich-text value from `--set` or `--field` is exit 22 `invalid_value` ("rich text by an automatic actor carries no attribution") on `update`, `create` and `transition` alike. An `auto:` create always gets the attribution paragraph as its first description paragraph, with the body after it when `--body-file` is given and alone when it is not; a `human:` create with no body omits the description. For a `human:` actor a rich-text `--set` or `--field` value is converted and checked by `validate_adf()` with no attribution.

## Invoking the provider

All HTTP goes through `src/ticketctl/gateway.py`, a stdlib client, under these invariants:

- `http.client` with one connection per write and an explicit `connect()` phase; a failure after the request was started is ambiguous; no connection reuse for writes.
- HTTP basic authentication with the site's email and token (the only place the token is placed, as the `Authorization` header; both values are stripped of surrounding whitespace), TLS verification on, and a fixed `User-Agent`. The token never appears in argv, the environment of a child, logs, audit, output or hook payloads.
- URLs are built only as `<site url> + path template`; an absolute URL from a response (`self`, `next`, attachment `content`) is never followed.
- Every 3xx is exit 12 `provider_changed`: no redirect is followed and credentials are never re-sent.
- No `Accept-Encoding` header is sent, so there is no decompression; a response is read through a hard cap of 16 MiB (and `Content-Length` is checked), and a larger one is `provider_changed`.
- JSON parsing runs behind a string-aware, iterative depth guard at depth 400 (a deeper document is `provider_changed` for the whole response, a residual for rich-text searches and comment pages; default `search` fields and single-key reads are unaffected); the renderer's own depth budget of 50 gives per-ticket stubs below that.
- `--verbose` never sets an `http.client` debug level and never prints request headers.
- `HTTPS_PROXY`, `https_proxy` and `NO_PROXY` are honored through a CONNECT tunnel (TLS stays end to end); the destination is still only the configured host.

The provider API used is Jira Cloud REST v3 plus the agile API. Every endpoint ticketctl calls is in the table below.

### Jira endpoints

| Purpose | Endpoint |
|---|---|
| identity | `GET /rest/api/3/myself` |
| read | `GET /rest/api/3/issue/{key}?fields=...&expand=...` (always with `fields`) |
| search | `POST /rest/api/3/search/jql` (`nextPageToken` paging), `POST /rest/api/3/search/approximate-count`, `POST /rest/api/3/jql/parse` |
| comments | `GET /rest/api/3/issue/{key}/comment` (`orderBy`, `startAt`, `expand=properties`), `POST` the same path (the create body carries `properties`) |
| transitions | `GET /rest/api/3/issue/{key}/transitions?expand=transitions.fields`, `POST` the same path |
| assign | `PUT /rest/api/3/issue/{key}/assignee` |
| edit | `PUT /rest/api/3/issue/{key}`, `GET /rest/api/3/issue/{key}/editmeta` |
| create | `POST /rest/api/3/issue`, `GET /rest/api/3/issue/createmeta/{project}/issuetypes[/{id}]` |
| fields | `GET /rest/api/3/field`, `GET /rest/api/3/resolution`, `GET /rest/api/3/priority` |
| sprints | `GET /rest/agile/1.0/board`, `GET /rest/agile/1.0/board?projectKeyOrId=`, `.../board/{id}/sprint`, `GET /rest/agile/1.0/sprint/{id}`, `POST /rest/agile/1.0/sprint/{id}/issue`, `POST /rest/agile/1.0/backlog/issue` |

The old `POST /rest/api/3/search` is not used (it returns 410 on current Jira Cloud).

## Reliability

- **Timeouts.** `timeouts.connect` (default 10 s) and `timeouts.read` (default 30 s) per site. The whole-command `--timeout` (default 120 s) covers provider work only (reads, retries, sends); the prompt and hook runs are excluded (hooks have their own timeouts). The deadline is checked before each step. On expiry before any write was sent, or on a read, the command exits 12 `timeout`; once a write request may have been sent, expiry is `outcome_unknown` (or `partial` when an earlier step landed), never `timeout`.
- **Retries.** Reads retry up to 3 times (4 attempts) on a connection failure before a response and on 429, 502, 503 and 504; writes retry up to 2 times only on a connection or TLS failure before any request byte was sent and on 429, and never after any byte of the request may have been sent (the command then exits 12 `outcome_unknown`). Every sleep is `min(0.5 s * 2^n, 8 s)` with full jitter; a `Retry-After` replaces it and is honored when it is at most `limits.retry_after_max` and the remaining command time allows it, else the command ends 12 `rate_limited` with `retry_after_s` and no wait. All sleeping counts against `--timeout`.
- **Re-running a write** is the caller's retry. A `comment` or `create` re-run with the same `--dedupe-key` returns `already_posted` or `reused`; a `transition` or `update` re-run reports `unchanged` if it already happened ([Plan object](#plan-object), rule 1). `outcome_unknown` is retryable only by these recipes: it is `retryable: false` for a `comment` or `create` without a dedupe key, and its message says "re-run only with --dedupe-key".
- **Pacing.** When `X-RateLimit-Remaining` falls below 10% of `X-RateLimit-Limit`, ticketctl sleeps 1 s before the next request of the same command; the values appear in `--verbose` output and in the error envelope. There is no cross-process limiter.
- **Caps.** One `search` returns at most 500 tickets with pages of `min(remaining, 100)` and an explicit `fields` list (never `*all`, which silently returns 100 per page), at most 5 page requests plus the parse call and the count. `get` takes at most 50 keys, `comments --limit` defaults to 50 (max 200), `get --comments N` at most 50, `sprints list --limit` defaults to 50 (max 500).
- **Version drift.** Unknown fields in a response are ignored, and the audit and log never depend on a response shape.

### HTTP status mapping

| Response | Exit and code |
|---|---|
| 404 on an endpoint whose path names an object (issue, comment, sprint, board, an issue's transitions) | 10 `not_found`; a missing, malformed or hidden key look identical, so the message says "missing or no permission" |
| 404 or any 410 on a collection endpoint (`search/jql`, `jql/parse`, `approximate-count`, `myself`, `field`, `priority`, `resolution`, the `board` listing) | 12 `provider_changed` |
| 401 | 12 `bad_credentials` |
| 403 on any call | 12 `forbidden` |
| 400 on a write | 12 `rejected`, the sanitized provider `errorMessages` and `errors` in `details` |
| 400 on `search/jql` | 22 `invalid_query` |
| 429 after retries | 12 `rate_limited` (see Retries above) |
| 502, 503, 504 and other 5xx after retries | on reads 12 `unavailable`, on writes 12 `outcome_unknown` |
| any other 4xx on a write (409, 413, 415, 422, ...) | 12 `rejected` (nothing applied) |
| any other 4xx on a read, and 400 on a read other than `search/jql` | 12 `provider_changed` |
| a write whose connection never opened, after retries | 12 `unavailable` (retryable, nothing sent) |

## Write flow

Every write (`transition`, `comment`, `create`, `update`, `sprint add|remove`) follows one order.

1. **Prepare** (no provider call). Argv grammars (exit 2), input files, Markdown conversion, `validate_adf()`, `secret_in_body`, size and `empty_body` (judged before the attribution paragraph is added), then the attribution paragraph is inserted and `attempt_id` is generated (16 random hex characters, used by every record and payload of the command).
2. **Resolve.** Select the site; for a write take a fresh `/myself` (this also refreshes `me.json`; reads use the cache) and apply `account_mismatch` (`auth_required` arises at the first call); read the ticket and the lists the action needs (`editmeta`, `createmeta`, the transitions); resolve names (`unknown_field`, `ambiguous_*`); run the dedupe lookup without a lock.
3. **Reduce and check.** Reduce the plan to its remaining steps ([Plan object](#plan-object), rule 1): none left is `unchanged` (exit 0, nothing else runs). Then `--if-status` (rule 4), then the provider-context checks for the steps that remain (`no_such_transition`, `ambiguous_transition`, `needs_resolution`, `missing_required_fields`, `not_editable`, `sprint_closed`), then build the plan and its hash and compare `--expect-plan` when given (rule 3; no comparison without it).
4. **`--dry-run`** prints the plan and stops. An empty `steps` is the plan of an `unchanged` run; a caller records no pending confirmation for it.
5. **Confirm** ([Human actions](#human-actions)). An interactive run shows the dialog and confirms once (default no). A non-interactive run without `--yes` exits 13 `confirmation_required` with nothing sent. The dialog tells the human that `pre-` hooks run after the answer; a decline is exit 20 `declined`.
6. **`pre-` hooks** run (see [Hooks](#hooks)); a veto exits 11, an escalation exits 13 `escalated`.
7. **Lock and fresh check.** With a dedupe key take the bucket lock ([Dedupe](#dedupe)), then re-read and rebuild the plan; for `comment` and `create` the fresh check is the dedupe lookup repeated under the lock. A hash difference from the held plan is exit 20 `stale_ticket` (rule 3), and no remaining steps is `unchanged`.
8. **Audit and send.** Append the `attempted` audit record, perform the provider calls in order, append the outcome record, run the `post-` hook, release the lock and print the result.

The order is fixed: the `pre-` hook runs after the confirmation, so a hook can veto or escalate a plan the human accepted; the human sees the veto as exit 11 with the hook's reason. The stale check stays after the hooks because it is the one nearest the write. `--yes` never overrides a refusal (exit 20).

- **No-ops never prompt.** A no-op (rule 1, including `already_posted` and `reused`) prompts nothing, runs no hook and writes no audit record; the request log still has its lines.
- **Post hooks.** A `post-` hook runs once, only when an `attempted` record was written, with `ok`, `failed`, `partial` or `unknown`; never for a refusal, a veto, `confirmation_required`, a no-op or an exit 22.
- **Gate decisions are not audited.** The audit holds only writes that reached the provider boundary (attempted plus outcome). A veto (11), an escalation (13), a stale or precondition refusal (20), a decline (20 `declined`) and `confirmation_required` write no audit record: the gate's own log is the record of its decision, and the envelope returns `decision_ids` and `hook` so the caller can record them. A `decision_id` is recorded in the audit records of the steps that follow.
- **`--dry-run` edges.** `--dry-run` takes precedence over `--yes` (see [Human actions](#human-actions)), ignores `--expect-plan`, does the reads (including dedupe lookups, so a hit prints a plan with no steps and a `dedupe_match`), reports a refusal with its exit code and envelope (an exit 20, 21 or 22 is shown, not hidden), runs no hook, takes no lock and writes no audit. "Writes nothing" means no provider write, no audit and no hook; cache refreshes and request log lines still happen.
- **Failure shape.** A failure prints the error envelope and nothing else (see [Result object](#result-object)); every failure after an `attempted` record carries `details.attempt_id`.

### Plan object

`schemas/cli/plan.json`: `action`, `site`, `key` (null for create), `actor`, `steps`, `changes`, `warnings`, `dedupe_key`, `dedupe_match` (the provider id or key of the object a dedupe lookup found, else null) and `plan_sha256`. The action names are `transition`, `comment`, `create`, `update`, `sprint_add` and `sprint_remove` (hooks use `sprint` for the last two; `plan.action` tells them apart). `steps` is an ordered list of provider calls in plain terms, `{kind: assign|transition|set_fields|add_comment|create|sprint_add|sprint_remove, ...}`, each a closed object with only the properties that apply to its kind. An empty `steps` array is `unchanged`.

`changes[].name` is `status`, `resolution`, `assignee`, `sprints`, `labels`, `title`, `description`, or a logical or `extra` field name; for `status`, `from` and `to` are `{name, category}`. A sprint remove lists its effect in `changes` too; a `comment` has no change entry (the body is in `steps` as hash and size). `from` and `to` are untrusted JSON (rich text in full for the human, as hashes only in the hash input). The `tc-` dedupe label is not listed.

`warnings` is an array of codes from one closed enum:

| Code | Meaning |
|---|---|
| `closed_ticket` | the ticket is in a done status |
| `assign_added` | the plan includes an assign step |
| `other_sprints_dropped` | a sprint remove drops more than one open or future sprint |
| `sprint_moved` | a sprint add moves the ticket out of another sprint |
| `body_near_limit` | the body is over 90% of `limits.body_chars` |
| `duplicate_dedupe_matches` | several own tickets matched a create dedupe label |
| `assign_skipped` | `--assign` was not applied because the ticket has an assignee |
| `audit_incomplete` | a result only: an outcome record could not be written |

The plan is built, hashed and applied by one rule:

1. **Idempotency first.** After resolve, the plan is reduced to its remaining steps. A plan with none (the intended end state already holds) is `unchanged`: exit 0, no prompt, no hook, no audit record, before any hash comparison and whatever `--expect-plan` says, and before every check that only matters when a step remains. The end state per action: transition, the current status name equals the target (case-insensitive; with `--transition-id` the listed transition's `to` is compared, and an id absent from the live list is `no_such_transition`); update, canonical equality of every named field; sprint add and remove, membership ([`sprint add|remove KEY`](#sprint-addremove-key)); comment and create, a dedupe hit. Canonical comparison, for both this test and the facts: rich text as rendered Markdown (the converted ADF rendered by the reader's renderer, so re-serialization does not matter), numbers as decimals, dates as ISO strings, options by name, lists and labels as sorted sets, users by account id.
2. **The hash.** `plan_sha256` is the sha256 of the canonical JSON (sorted keys, no whitespace, UTF-8) of `{v: 1, action, site, key (the project for create), intent, facts}`. `intent` is what the caller asked for, resolved: the target status and the resolved transition id (not the typed `--to`), the resolution, the effective assign target (`me`, `none`, an account id or null, after `--assign` and `assign_on_start`, whether or not a step results), field names with value hashes, body hashes, labels, the resolved sprint id and the dedupe key. `facts` is only what the plan reads: for a transition, the current status name and the assignee when that assignee is neither none, nor the resolved `me`, nor the requested assign target; for an update, the current values of the fields it changes (for label operations, whether each named label is present); for sprint add and remove, the ticket's current open and future sprint ids; for comment and create, none. Steps are derived from intent plus facts and are not hashed, so an assign step that appears or disappears between plan and apply runs as the plan said (it was in the confirmed intent). The actor is not hashed: a body that carries the attribution line already differs in its hash.
3. **Two comparisons.** The hash is compared when the plan is built (against `--expect-plan`, before any hook runs) and again after the hooks, from a fresh read just before the first write (write-flow step 7). A difference is exit 20 `stale_ticket` with `details.plan`; a fresh read that now shows no remaining steps is `unchanged` (rule 1).
4. **`--if-status`.** `transition` accepts `--if-status <name>`: when the ticket's current status is not that name (case-insensitive) the command exits 20 `precondition_failed` (`details.current_status`, `details.expected_status`). It is evaluated after rule 1 and before the provider-context checks and the hash, and is the only precondition flag (the assignee guard is in the hash).

### Result object

`schemas/cli/result.json`, printed on exit 0: `{action, site, key, url, changed, result, steps, already_posted, reused, comment_id, attempt_id, decision_ids, warnings}`. `result` is `ok` (a provider write happened) or `unchanged` (nothing was sent: already in the target state, `already_posted`, `reused`); `changed` is `result == ok`; `already_posted` and `reused` are required booleans; `comment_id` is null except for `comment` (and then it is the existing id for `already_posted`); `url` is the ticket's browse URL; `attempt_id` is null when nothing was audited (`unchanged`); `warnings` holds the plan warnings that apply. A no-op has `steps: []` and `decision_ids` holding the flag ids; `reused` carries the existing key. Each step has `status` `done`, `skipped` or `failed`; `skipped` means in the plan but not run because an earlier step failed.

`partial` is printed only inside `details.result` of an exit 12 envelope with `code: partial`. A command whose earlier step landed and a later one failed (definitely, or unknown) exits 12 `partial`; `details.result` holds the full result object (`result: partial`, the per-step statuses, `attempt_id`, `decision_ids`), and the failing step's own error is copied into `retryable`, `retry_after_s` and `outcome_unknown` of the envelope (`outcome_unknown: true` when the failing step is unknown) and into `result.steps[i].code`. A failure with nothing applied keeps its own code (`rejected`, `outcome_unknown`, ...) and has no `details.result`. Nothing is rolled back.

The three `result` vocabularies are different on purpose:

| Where | Values |
|---|---|
| result object | `ok`, `unchanged` (and `partial` only inside an envelope) |
| audit record, per provider call | `attempted`, `ok`, `failed`, `partial`, `unknown` |
| post-hook payload, per command | `ok`, `failed`, `partial`, `unknown` |

### Transition order

`transition KEY --to <status>` resolves the transition from the live list by target status name, case-insensitively. No match is exit 20 `no_such_transition` listing the available targets; several matches exit 21 `ambiguous_transition`; these checks run only while a step remains (a re-run after a landed transition is `unchanged`, never exit 20). `--transition-id <id>` bypasses the name match. A ticket already in the target status is `unchanged`, unless `--assign` was given and the ticket is unassigned: the assign step alone then remains (result action `transition`, one step). If the target's category is `done`, a resolution is required while a step remains: `--resolution <name>` or `sites.<site>.transitions.default_resolution` (exit 22 `needs_resolution` otherwise), validated against the transition's allowed values (and, when they are not listed, against the provider's resolution list). `--resolution` with a non-done target is exit 22 `invalid_value`, and is ignored for an `unchanged` result. A done ticket reached by another route (workflow automation) with no resolution is `unchanged`: ticketctl never backfills a resolution in 0.x. Transitions out of a done status are not special-cased: the live list decides and `closed_ticket` warns. The provider accepts a done transition with no resolution and leaves it empty, so ticketctl never sends one without it. A transition whose required fields are not supplied is exit 22 `missing_required_fields` with `details.required_fields`, as for `create`.

`--assign me|<account>` on `transition` means "assign if unassigned": the assign step is in the plan exactly when the ticket is unassigned at plan time and either `--assign` was given, or `sites.<site>.transitions.assign_on_start: me` is set, the target's category is not `done` and a transition step remains. When `--assign` was given and the ticket already has an assignee the step is skipped and the plan warns `assign_skipped`. The step is explicit, shown in the dry-run and audited. Otherwise the transition is sent as is and the plan holds no assign step. The requirement for an assignee is a workflow validator that neither the transitions list nor `editmeta` shows (a Sub-task's done transition may need one), so it cannot be pre-checked: a provider 400 on the transition call is exit 12 `rejected` with nothing applied, and `details.reason_hint: needs_assignee` is added when the ticket was unassigned and the plan had no assign step (decided from the plan facts, never from the message text). The assign and the transition are two calls and are not atomic; a failure of the second is exit 12 `partial`. Other required transition fields are passed with `--field name=value` and validated against the transition's field list; a sub-task transition that needs a parent takes `--field parent=KEY`.

### Dedupe

`comment` and `create` accept `--dedupe-key <string>` (a short identifier chosen by the caller, grammar in [Identifiers](#identifiers), never secret); `--yes` requires it ([Human actions](#human-actions)). The provider has no idempotency, so ticketctl records the key on the object it creates. The lookup in resolve is lock-free and feeds the plan and the dry-run, so a hit gives an empty plan (`unchanged`) and a human is never asked to confirm a post that will be skipped; under the lock (write-flow step 7) it is repeated as the fresh check.

- **Comment.** The key is stored in the `ticketctl.meta` property ([Attribution](#attribution)) of the comment as `key_sha256`, the sha256 of the key string alone (UTF-8), beside `body_sha256` of the final ADF. The lookup lists the ticket's comments newest first with `expand=properties`, keeps those whose `author.accountId` equals the fresh `/myself` account of the write flow (not `me.jira`), parses the property defensively (only an object with `v == 1` and a 64-hex `key_sha256` counts; anything else is "no marker") and scans at most 1000 comments. The key found with the same `body_sha256` is `already_posted` (exit 0); with a different one it is exit 20 `already_exists` with `details.existing_id`, and nothing is posted (a re-run under a different attribution template or setting also finds a different hash: the safe direction). If the key is not found and more comments exist, the command exits 12 `lookup_incomplete` rather than assuming absence; it is not retryable, an automatic caller reports it and only a human may post without a key. A comment posted without a key is never deduplicated.
- **Create.** The key is stored as a label `tc-<first 16 hex characters of sha256(site \0 project \0 key)>`. The label and the `--label` values go in `fields.labels` of the create request, so `create` is always one provider call and `partial` cannot occur on it. `labels` is exempt from the `unknown_field` and `not_editable` pre-check against `editmeta` and `createmeta` (the create body accepts it although createmeta does not list it). A 400 with a `labels` error from a site's create screen is exit 12 `rejected` with the provider's `details.field_errors`, and the message says to create without a dedupe key (there is no fallback). The lookup searches `project = "<P>" AND labels = "tc-..." AND reporter = "<account id from /myself>"`. The found ticket's issue type and exact title (sha256 compared) must match the plan: a match is `reused` and nothing else runs, a mismatch is exit 20 `already_exists` with `details.existing_key`. A key is bound to type and title only: a reused key with a different description, parent or labels is `reused`. Several own matches (a race) return the oldest by key with the warning `duplicate_dedupe_matches`. Issue and comment properties are not searchable, so they are never a lookup key for tickets. A `--label` matching `^tc-[0-9a-f]{16}$` (the reserved pattern) is exit 22 `invalid_value` on `create` and `update`.
- **Lock.** The lock is taken at write-flow step 7 (after the hooks, never across the prompt) and held to the outcome record. Locks are a fixed set of 256 bucket files `state/locks/<first two hex of sha256(site \0 project-or-key \0 dedupe key)>` taken with `flock`, waiting up to the command timeout, then exit 12 `unavailable` with `reason: locked`; a collision only serializes unrelated writers and nothing accumulates. On release the holder updates the file's modification time. Lag guard: when a lock is acquired and its file was modified within the last 5 s, a `create` lookup that finds nothing repeats once after 2 s before concluding absent (the label index lags a create by about a second). This covers a waiting run and a quick re-run on one host; a re-run from another host is a residual risk. Comments need no guard (their list is read-your-writes).

The caller owns key hygiene: a new body needs a new key.

## Commands

All commands accept the [global flags](#global-flags) where meaningful. Every command that outputs data supports `--json`; with `--json` anywhere in argv an argparse failure also prints the error envelope (the parser is wrapped and the message is sanitized). Write-only flags (`--yes`, `--dry-run`, `--expect-plan`, `--actor`, `--decision-id`, `--override-escalation`, `--hook`, `--if-status`) on a read command exit 2 `invalid_argument`. A flag that fails argparse (type, choice, required, mutual exclusion) and every shared flag grammar (actor, decision id, dedupe key, numeric ids, durations, integers) exit 2 `invalid_argument`; violations of "exactly one of" rules and `--verbose` with `--quiet` exit 2 `invalid_combination`. Values that feed site selection or the provider path (ticket keys, project keys, site names, browse URLs) and values judged with config, provider or object context exit 22.

### `whoami`

Prints `{site, account_id, display_name, source}` for the selected site: the `/myself` account (cached in `me.json`, or fresh with `--refresh`), `source` being `cache` or `provider`. Schema `schemas/cli/whoami.json`.

### `get KEY...`

Reads up to 50 distinct keys (a duplicate is read once; the 51st distinct key exits 22 `invalid_value`); every key's grammar is validated up front and any failure exits 22 `invalid_key`; all keys must select one site. Options: `--fields a,b`, `--comments N`, `--include-adf`. Output is `{tickets, errors}`: `tickets` in input order, `errors` a list of `{key, error}` where `error` is the full envelope body for that key (`not_found`, `forbidden`, any other). Every key readable: exit 0. Some succeeded: exit 0 with `errors` non-empty (a consumer must read `errors`). None succeeded: the exit of the first error in input order (10 when all were not found) with the error envelope alone and `details.errors` holding the per-key list (the shape of `get.json`). A single key keeps the single-key behavior (a missing ticket exits 10 with the envelope). Reads are issued with concurrency 4 and order is restored before printing. Schema `schemas/cli/get.json`.

### `search`

The filter is a query form plus structured flags. The query form is one of `--query <name>` (a named query from `sites.<site>.queries.<name>`, a JQL string in the user's own config; an unknown name is exit 22 `invalid_value` naming the query and the site), `--jql-file <path>|-` or a short `--jql <text>` for humans; the file and text forms are untrusted. Structured flags combine with it as described in [Search compilation](#search-compilation):

| Flag | Meaning | Repeats |
|---|---|---|
| `--project` | project key | OR |
| `--status`, `--status-category new\|indeterminate\|done` | status name, category (`new` compiles to `"To Do"`, `indeterminate` to `"In Progress"`, `done` to `"Done"`, the `statusCategory` names) | OR |
| `--assignee me\|none\|<account>` | `--assignee none --assignee me` is the "unassigned or mine" pool | OR |
| `--sprint open\|future\|<id>` | sprint selector | OR |
| `--label` | label | AND (all present) |
| `--parent <key>`, `--type`, `--priority` | parent key, issue type, priority | OR |
| `--key` | a batch of keys (`key in (...)`) | OR |
| `--not-status`, `--not-label`, `--not-parent`, `--not-type` | exclusions; each excludes every listed value | all values excluded |
| `--updated-since`, `--created-since` | a time ([Search compilation](#search-compilation)) | last wins |
| `--text <string>` or `--text-file <path>\|-` | one clause `text ~ "<phrase>"` (a quoted phrase, see [Search compilation](#search-compilation)), at most 200 characters, never from argv alone | one |

Different flags are ANDed. Everything else (components, due-date filters, board filters) is a named query or `--jql-file` in the caller's own trusted config; ticketctl's quoting guarantee covers the structured flags only, and board-based pools are named queries (`filter = <id>`). `--board` on `search` is a 1.x candidate.

Also `--order-by <field>[:asc|:desc]` (including `rank`), `--limit N` (default 50, at most 500, at least 1; `--limit 0` only with `--count`; else exit 22 `invalid_value`), `--fields`, `--count` and `--cursor <token>`. The cursor is opaque (`base64url` of `{v: 1, jql_sha256, page_token}`) and valid only with the same compiled JQL; one that does not decode, does not match the compiled JQL or whose provider token the provider rejected exits 22 `cursor_expired` ("restart without --cursor"). `--updated-since` plus `--cursor` are the documented incremental pool refresh; a ticket leaving a pool is not reported (the consumer re-reads claimed keys with `get`). There is no `--comments` on `search`. Schema `schemas/cli/search.json`.

#### Search compilation

The query form is split at its last top-level `ORDER BY` by a small scanner that skips double- and single-quoted strings with backslash escapes; the tail after the keyword is the query's order clause. The scanner first proves the where-part balanced (quote-aware, parenthesis depth never negative, ending at zero, no open quote), else exit 22 `invalid_query` ("unbalanced parentheses or quote"), because a where-part such as `status = Open) OR (project = X` would escape the AND-ed flags. Only then is the where-part wrapped in parentheses, each structured flag becomes one parenthesized clause, all are joined with `AND`, and the order clause is `--order-by` when given (it replaces the query's), else the query's. The result is always validated through the provider's parse endpoint with strict validation: a mis-split, a syntax error, an unknown field, status, option value or function exits 22 `invalid_query` naming the offending token, because the provider otherwise answers a semantic error with an empty result. An empty result is therefore always a real zero. A 400 from `search/jql` (for example "unbounded JQL queries are not allowed") is also `invalid_query`, with the provider's sanitized text in `details.errorMessages`.

Each structured flag compiles to exactly one clause or an exit 22. Quoted strings are double-quoted with `\` then `"` escaped; a value with control characters or longer than 256 characters is `invalid_value`. Unquoted tokens have grammars:

| Token | Grammar and compilation |
|---|---|
| duration | `^[0-9]{1,4}[mhdw]$`, compiled `-<n><unit>` |
| timestamp | the time grammar below |
| sprint selector | `open` (compiled `openSprints()`), `future` (`futureSprints()`), or digits (the id) |
| `--status-category` | `new`, `indeterminate` or `done`, compiled to the quoted category name |
| `--assignee` | `me` is `currentUser()`, `none` is `EMPTY`, an account id is quoted and matched against `account_id` |
| `--text` | the text has `\` then `"` escaped and is wrapped in inner double quotes, and the whole is escaped and quoted again: a Lucene phrase inside the JQL string, so `[ ] { } ( ) ^ ~ * ? : + - & \| !` in a title are literal. It is a tokenized, punctuation-insensitive phrase match, so results can be near matches and the caller compares titles when it needs equality |
| `--order-by` | `<field>[:asc\|:desc]`, field `^[A-Za-z][A-Za-z0-9_]{0,63}$` (JQL clause names, `rank` included) or a logical field name mapped to its clause |

One time grammar serves `--updated-since`, `--created-since`, `comments --since` and `export --since`: an RFC 3339 timestamp with `Z` or an offset, a date `YYYY-MM-DD`, or a duration `<n>m|h|d|w`. In `search`, timestamps compile into the JQL minute format in the site user's time zone, taken from `/myself.timeZone` (stored in `me.json`; absolute JQL dates are read in that zone), rounded down to the minute; durations compile to relative JQL. For `comments` and `export` a bare date means 00:00 UTC. The sprint states are `future`, `active` and `closed`; the selectors are `open` (the `active` state), `future` (search only), `next` (`sprint add` only) or an id.

### `comments KEY`

Lists comments (`--limit` default 50, max 200; `--since <ts>`, which filters on `created`; `--newest-first`; `--own`, which needs the resolved account), paged with `startAt`. The scan reads at most 1000 comments (the dedupe cap): `--limit N` with the default order returns the oldest N matching comments, with `--newest-first` the newest N (`get --comments N` is the newest N, oldest first). Output `{key, site, items, total, truncated}`: `total` is the number of comments on the ticket before the `--since` and `--own` filters; `truncated` is true when the limit or the cap stopped the scan with more matching comments beyond. Schema `schemas/cli/comments.json`.

### `transitions KEY`

Lists the transitions available now, with required fields: `{key, site, transitions}`. Schema `schemas/cli/transitions.json`.

### `sprints`

`sprints list [--state future|active|closed] [--board <id|name>] [--limit N]` and `sprints show <id>`. Without a key the board is `--board`, else `sites.<site>.board`, else the scrum boards of `--project`, else of the site's only project, else exit 2 `missing_option`. `--board <name>` is matched case-insensitively and exactly against the boards of the site (at most 5 pages of 50); a digits-only value is an id; several matches, or several scrum boards for a derived project board, exit 21 `ambiguous_board`; no board is exit 10 `not_found` with `details.option: board`. `list` returns the provider's order by ascending id within the pages read, reading pages of 50 until `--limit` is reached (default 50, max 500), as `{site, sprints}`; a consumer wanting the latest uses `--state`. `show` returns `{site, sprint}`. Schemas `schemas/cli/sprints-list.json` and `schemas/cli/sprints-show.json`.

### `fields`

`fields list` prints the discovered fields (the one command that prints provider field ids) with their schema and the logical mapping; `fields check` validates the configured mappings against them; `fields refresh` replaces the cache. `fields check` prints its problems (`missing`, `ambiguous` and `type_mismatch`, the last for a mapped field whose schema is not one the logical field accepts: `estimate` a number, `sprint` an array of sprint objects, `acceptance_criteria` a string or rich text, `due_date` a date) as data and exits 0 whenever it ran (an empty list when the mappings are sound); an offline invalid config exits 2. Schemas `schemas/cli/fields-list.json`, `schemas/cli/fields-check.json` and `schemas/cli/fields-refresh.json`.

### `sites`

Prints the configured sites and their `projects`; `sites resolve KEY` prints `{key, site, rule}`, where `rule` is `site_flag`, `url`, `project`, `default_site` or `only_site` (see [Site selection](#site-selection)). Offline: no network and no credentials are read. Schemas `schemas/cli/sites.json` and `schemas/cli/sites-resolve.json`.

### `validate`

`validate` checks the configuration offline (shape only, plus the loader checks under [Config](#config); exit 2 on invalid config, naming the key) and prints `{valid, probe, sites?}`. `validate --probe` also makes read-only calls: it authenticates, compares the account with any configured expectation, runs the same routine as `fields check` and parses every named query, and prints the problems per site under `sites[].problems`; it exits 0 whenever it ran, `valid` meaning only that the configuration is valid. Schema `schemas/cli/validate.json`.

### `transition KEY`

See [Transition order](#transition-order). Exactly one of `--to <status>` and `--transition-id`; `--resolution`; `--assign me|<account>` (assigns only an unassigned ticket; plan warning `assign_skipped` otherwise); `--field name=value|@file|-` (repeatable, the grammar of `--set`; `resolution` and the other dedicated names are refused with exit 2 `invalid_combination`); `--if-status <name>`. See [Value grammar](#value-grammar).

### `comment KEY`

Exactly one of `--body <text>` and `--body-file <path>|-`; `--format markdown|adf` (`adf` only with `--body-file`); `--dedupe-key`. The comment is posted as the authenticated account.

### `create`

`--project <key>` (or the site's only `projects` entry, used only after the site was selected by the other rules; with several entries exit 2 `missing_option`), `--type <name>` (validated against createmeta; an unknown type is exit 22 `invalid_value` with the valid types in `details.candidates`), `--title <text>` or `--title-file` (at most 255 characters, else exit 22 `invalid_value`), `--body-file` only (there is no short `--body`, and `--set description` is refused), `--parent <key>` (a sub-task needs it), `--assign me|none|<account>` (part of the create body, so there is no assign step; without it the provider default applies; `none` sends an explicit unassigned value), `--label` (repeatable; no whitespace, at most 255 characters; a reserved dedupe pattern is refused, see [Dedupe](#dedupe)), `--set name=value|@file|-` (repeatable; priority, estimate and due date go through it with logical names; see [Value grammar](#value-grammar)), `--dedupe-key`. `create` is one provider call and has no `--sprint`: run `sprint add` on the returned key. Required fields that are not supplied exit 22 `missing_required_fields` with `details.required_fields` (`[{name, kind, allowed_values?}]`; fields that have a default value or that ticketctl supplies are not missing); `create --dry-run` with the type and without the values is the discovery route. The result carries the new key and `reused: true` when a dedupe match was found.

### `update KEY`

`--title <text>` or `--title-file` (at most 255 characters), `--assign me|none|<account>` (always assigns; `none` unassigns; never the provider's automatic assignment), `--add-label`, `--remove-label` (the same label in both exits 22 `invalid_value`; no whitespace, at most 255 characters; a reserved dedupe pattern is refused), `--set name=value|@file|-` (repeatable; `description` is set with `--set description=@file`, there is no `--body-file` on `update`; see [Value grammar](#value-grammar)). With no change flag the command exits 2 (`reason: missing_option`). Values are converted by the field's schema; an unknown or non-editable field exits 22 `unknown_field` or `not_editable` before anything is sent. Setting a value the ticket already has is `unchanged`. An update with several change kinds runs `set_fields` first (the step that can fail on field values) and `assign` second, so a failure of the second is exit 12 `partial`; label operations ride in the same `set_fields` request as the other fields, using the provider's `update` verbs.

### `sprint add|remove KEY`

Takes exactly one key; callers loop. One plan, one result, one audit pair and one hook pair per command; multi-key writes are a 1.x candidate.

- `sprint add KEY --sprint open|next|<id>`: already in that sprint is `unchanged`; in another future or active sprint the plan shows `from` and `to` and warns `sprint_moved` (the provider moves it); a closed target exits 20 `sprint_closed`; a provider refusal (a sub-task, a ticket outside the board) is `rejected`. `open` is the board's one `active` sprint (several active exit 21 `ambiguous_sprint`); `next` is the future sprint with the earliest non-null `start`, else the lowest id (never ambiguous). Sprint membership for the plan, the hash facts and the end-state test means the ticket's `future` and `active` sprints only; closed sprints are history and ignored.
- `sprint remove KEY` takes no `--sprint` (exit 2 `invalid_combination` if given): it moves the ticket to the backlog, which for the verified case (one active sprint) removed it from that sprint; the membership counted is `future` and `active`. The plan lists the open and future sprints dropped in `changes` and warns `other_sprints_dropped` when more than one; a ticket with no open or future sprint is `unchanged`. Moving a ticket out of a closed sprint is not claimed (unverified).
- Board: `--board <id|name>` (also on `sprint add`), else `sites.<site>.board`, else the scrum boards the ticket's project is on. One is used; none exits 10 `not_found` with `details.option: board`; several, or a name matching several boards, exit 21 `ambiguous_board` (matching as under [`sprints`](#sprints)).

### `export`

Prints audit records as JSON lines on stdout: `--since <ts>` (bare dates are UTC) and `--after-seq <n>` (ANDed), `--to <path>`. With `--to` the file is created per [Input files](#input-files) and an existing file exits 20 `already_exists`; without it nothing is created. `--json` has no extra effect. A line that is not a JSON object with `v == 1` is skipped with one warning; other lines are not validated against the schema, so records written by a later minor version pass. Each record carries `seq`. Schema `schemas/cli/export.json` (each line is one audit record).

## Global flags

`--json`, `--config <path>`, `--site <name>`, `--verbose`, `--quiet`, `--timeout <duration>` (the duration grammar, 1 s to 24 h), `--dry-run` (writes), `--yes` (writes; always needs `--expect-plan`), `--expect-plan <sha256>`, `--actor <string>`, `--decision-id <id>` (repeatable), `--override-escalation[=<scope>]` (human-only, see [Human actions](#human-actions); the scope is `ticketctl`), `--hook <name>=<command>` (repeatable, in addition to configured hooks), `--version`. `--quiet` suppresses non-error stderr (not the confirmation dialog); `--verbose` prints extra diagnostics (rate limits, retries) on stderr and never headers; both together exit 2 `invalid_combination`. A value that can carry untrusted text is accepted as a file or stdin, never only as argv (design guide §4.1): bodies, titles, JQL, text filters and `--set` rich-text values. At most one `-` source per command (exit 2 `stdin_conflict`). `--config <file>` loads only that file (no drop-ins). A time setting takes the duration grammar under [Config](#config).

### Input files

One reader serves every file argument (`--body-file`, `--title-file`, `--jql-file`, `--text-file`, `@file`). It opens with `O_RDONLY|O_NONBLOCK|O_NOFOLLOW|O_CLOEXEC`, requires `fstat` to show a regular file (so a FIFO never blocks), then clears non-blocking; it refuses a path under `/proc`, `/sys` or `/dev`, judged on the opened descriptor's resolved path (`/proc/self/fd/N` on Linux, `F_GETPATH` on macOS, `realpath` as the fallback; stdin `-` is the exception). It reads at most `4 * limits.body_chars + 1` bytes for bodies and 1 MiB for the others, and decodes strict UTF-8. A failure is exit 22 `invalid_file`. Any other file the user can read is readable this way (see [Trust boundary](#trust-boundary)). Before any send, an outbound body, title, JQL, label or field value that contains the exact value of any configured token variable (a value of at least 8 characters) is exit 22 `secret_in_body`; the message never echoes the value or its position. `export --to` opens with `O_WRONLY|O_CREAT|O_EXCL|O_NOFOLLOW` and mode 0600 and requires the parent to be a directory owned by the caller. The cache writes (temporary file plus rename) and the state directory use `O_NOFOLLOW` and ownership checks. A driven caller can still create new files with `export --to`.

## Exit codes

One table of exit, `code`, `reason` and the caller's action. `retryable` is true for `timeout`, `unavailable`, `rate_limited` and `outcome_unknown` (the last only by the re-run recipes under [Reliability](#reliability): false for a `comment` or `create` without a dedupe key), false for everything else, `lookup_incomplete` included; `partial` copies the failing step's value. `retry_after_s` is set for `rate_limited` and `unavailable` when the provider sent `Retry-After`. `reason` is required (non-null) for exits 2, 20 and 22, and null otherwise except where listed.

| Exit | `code` | `reason` | Caller action |
|---|---|---|---|
| 0 | none | none | success, including `unchanged`, `already_posted` and `reused` |
| 1 | `internal` | `audit_failed` when the `attempted` append failed and nothing was sent, else null | report; do not retry blindly |
| 2 | `usage` | `human_only`, `yes_requires_expect_plan`, `dedupe_key_required`, `stdin_conflict`, `invalid_combination`, `missing_option`, `invalid_argument` | fix the invocation |
| 2 | `invalid_config` | `config_invalid`, `no_sites`, `account_mismatch` | fix the config |
| 10 | `not_found` | null (a ticket, sprint or board) | the object is missing or hidden |
| 11 | `vetoed` | null (the hook in `details`) | report the hook's reason |
| 12 | `auth_required`, `bad_credentials`, `forbidden`, `rejected`, `unavailable` (reason `locked` possible), `timeout`, `outcome_unknown`, `rate_limited`, `provider_changed`, `partial`, `lookup_incomplete` | null | by code: `outcome_unknown` re-run per Reliability (find the `unknown` audit record by `details.attempt_id`); `partial` read `details.result`; `rejected` read `details.errorMessages`, `details.field_errors` and any `reason_hint`; `lookup_incomplete` report, only a human may post without a key |
| 13 | `confirmation_required`, `escalated` | null | store `details.plan`, `plan_sha256`, `decision_ids` and the hook; a human confirms later |
| 20 | `stale_ticket`, `precondition_failed`, `no_such_transition`, `sprint_closed`, `already_exists`, `declined` | equal to the code | `stale_ticket`: re-plan with `--dry-run`, never retry as is; `already_exists`: pick a new key or keep the existing id; `declined`: the human said no, stop and record nothing |
| 21 | `ambiguous_site`, `ambiguous_field`, `ambiguous_transition`, `ambiguous_board`, `ambiguous_sprint` | null (candidates in `details.candidates`) | name one candidate |
| 22 | `invalid_request` | `invalid_query`, `unknown_field`, `not_editable`, `invalid_value`, `needs_resolution`, `missing_required_fields`, `node_not_allowed`, `body_too_large`, `empty_body`, `unknown_site`, `invalid_key`, `mixed_sites`, `invalid_file`, `secret_in_body`, `cursor_expired` | the input failed ticketctl's own validation; fix it |

`needs_resolution` is exit 22: the plan needs one more input. Exit 2 is for a flag or shared flag grammar that fails (argparse, actor, decision id, dedupe key, numeric ids, durations, integers); exit 22 is for a ticket, project or site identifier, a browse URL, or a value judged with config, provider or object context.

## Output by exit code

With `--json`, exit 0 prints the command's object (a result or, with `--dry-run`, a plan); a failure prints the error envelope on stdout (and the message on stderr). Without `--json`, human text goes to stdout and errors to stderr. The stderr message has no schema.

### Error envelope

`schemas/cli/error.json`: `{"error": {"code", "message", "reason", "retryable", "retry_after_s", "outcome_unknown", "site", "key", "details"}}`. `details` keys are named once; which appear depends on the code: `attempt_id` (on every failure after an `attempted` audit record was written), `candidates` (also the valid issue types for an unknown `--type`), `available_targets`, `field`, `fields`, `option`, `token`, `required_fields`, `plan`, `result`, `hook`, `escalated_hooks`, `hook_stderr`, `decision_ids`, `existing_id`, `existing_key`, `current_status`, `expected_status`, `reason_hint`, `field_errors`, `errors`, `errorMessages`, `rate_limit`, `token_env`. `confirmation_required`, `escalated`, `vetoed` and `declined` carry `details.plan` (the full plan with `plan_sha256`); the first three also carry `details.decision_ids` (every id returned by hooks and given with `--decision-id`), and the last two of those also carry `details.hook` (the hook that escalated or vetoed; `details.escalated_hooks` lists several) and `details.hook_stderr` (sanitized, at most 4096 bytes, untrusted). The caller stores the hash and later passes `--yes --expect-plan` with `--override-escalation=ticketctl` for an escalation; no second dry-run is needed. `stale_ticket` carries the new plan. `details.errorMessages` and `details.field_errors` are the provider's (sanitized, untrusted); `details.errors` is only the `get` per-key list of `{key, error}` with the full error body, the shape of `get.json`. The provider answers errors in three body shapes (`errorMessages` with `errors`, `{"error": ...}` and plain text) and the gateway maps all of them to this envelope. `details.reason_hint` is `needs_assignee` (see [Transition order](#transition-order)).

## Hooks

Per kind of write, one `pre-` and one `post-` hook: `pre-transition`, `post-transition`, `pre-comment`, `post-comment`, `pre-create`, `post-create`, `pre-update`, `post-update` (including assign and label changes), `pre-sprint`, `post-sprint`. Reads have no hooks. Hooks follow the design guide §8 (own process group, JSON on stdin, no templates, timeout default 30 s). Hooks run in the caller's working directory.

- **Order.** Configured hooks run in list order, then `--hook` ones. Exit 0 continues. Exit 13 escalates and the remaining hooks still run (a later veto wins). Any other non-zero exit vetoes at once (later hooks do not run): exit 11, the hook's sanitized stderr in `details.hook_stderr`. A timeout is a veto and the hook's process group is signaled.
- **`pre-`** runs at write-flow step 6, after the plan and the confirmation and before the first provider write (not for `--dry-run`). stdout may be a JSON object with `decision_id` (a string or a list): every id returned, also by an escalating or vetoing hook, is returned in `details.decision_ids` of that exit and recorded in the audit records of the steps that follow. An id that fails the decision-id grammar, or one beyond 16 in all, is dropped with one stderr warning and is never a veto. `--override-escalation=ticketctl` overrides the escalation of every hook ticketctl ran, never a veto ([Human actions](#human-actions)). Hook stdout is not validated. The `attempt_id` in a `pre-` payload is provisional: no audit record follows a veto, a stale refusal or a decline.
- **`post-`** runs after the outcome record, once per attempted command (see [Write flow](#write-flow)), best-effort, never fatal, `async: true` optional. There is no event id and no at-least-once delivery.
- **Payload** (`schemas/hooks/<hook>.json`): `tool`, `hook`, `object_type` (`ticket`), `object_id` (the key; null for `pre-create` and for a `post-create` that created nothing), `site`, `project` (the key prefix or the create project), `actor`, `attempt_id`, `decision_ids`, `plan`, `body`. `plan` is per hook family: `action`, `steps` (the kinds), `current_status` and `target_status` (a transition has both; create has neither; the other actions have only `current_status`), `fields_changed` (names), `resolution`, `assign` (transition and update: the assign step's target `me`, `none` or an account id, null when the plan has no assign step), `plan_sha256`, `dedupe_key`, as the family needs. `body` is the array of `{sha256, bytes}`, one for every rich-text value (the body or description first, then `--set` and `--field` values in argv order). Ticket hooks (all but create) add `ticket` `{type, subtask, status, priority, assignee_account_id, assignee_is_me}` (names and ids only: status, type and priority names are workflow configuration, not ticket text). Sprint hooks add `sprints`, a non-empty list of `{id, state}` (the target for add, the dropped open and future sprints for remove). Create hooks add `create` `{type, parent, assign}` (`parent` is a key or null). `post-` hooks add `result` (`ok`, `failed`, `partial` or `unknown`: `partial` when any earlier step landed and a later one failed or is unknown, else the single outcome), `error` (`{code, reason}` or null), `steps` (`[{kind, status}]`), `provider_ids` (the comment id, the created ticket id, else the ticket id) and `changed` (boolean, null for `unknown`). There is no `repo`, so every ticketctl gate decision is recorded in gatectl's scope `*` and per-project policy uses the `project` field in rules; there is no `labels`. Payloads carry no ticket text: no title, description, comment or label text; a policy that needs text reads it with `get`. The payload is built from the resolve read (write-flow steps 2 and 3) and may predate a long confirmation.
- **Environment.** The tool's environment minus every configured `token_env` and `email_env` variable, plus `TICKETCTL_HOOK`, `TICKETCTL_ID` (equal to `object_id`: empty only for `pre-create` and for a `post-create` that created nothing) and `TICKETCTL_OBJECT_TYPE=ticket`.

## Config

`~/.config/ticketctl/config.yaml`, drop-ins `config.d/*.yaml`, `--config` overrides (design guide §5). Schema [`schemas/config.json`](../schemas/config.json); every key is documented with its default in `config.example.yaml`. Invalid config fails fast naming the key. The default location reads `config.yaml` then `config.d/*.yaml` in lexical order: maps merge, `hooks` lists append, other lists replace; the merged document is validated. `--config <file>` loads only that file.

One duration grammar serves every time setting (config, flags, hook timeouts): `<integer>(ms|s|m|h|d)` or a bare integer meaning seconds. `0` is allowed only for `cache_ttl` (no cache) and `retry_after_max` (never wait); hook timeouts are whole seconds from 1 to 600; `--timeout` and the `timeout` key run from 1 s to 24 h.

| Key | Default | Meaning |
|---|---|---|
| `sites.<name>.provider` | `jira` | the only value in 0.x |
| `sites.<name>.url` | required | `https` URL with a non-empty host, no userinfo, query or fragment, an optional context path; the host is lower-cased and port and path are compared with the same normalization everywhere |
| `sites.<name>.allow_http` | `false` | accept an `http` URL; only for loopback hosts (`127.0.0.1`, `::1`, `localhost`) |
| `sites.<name>.email`, `email_env` | required, exactly one | the account email; not a secret |
| `sites.<name>.token_env` | required | name of the environment variable holding the API token |
| `sites.<name>.account_id` | none | the account id this site's `me` is expected to be; overrides `me.jira` for the site |
| `sites.<name>.projects` | `[]` | project keys this site owns (site selection) |
| `sites.<name>.board` | none | board for sprint commands: an integer is an id, a string a name (names can repeat: `ambiguous_board`) |
| `sites.<name>.fields` | `{}` | logical field mappings and `extra` (names lower snake and not a logical name) |
| `sites.<name>.queries` | `{}` | named JQL queries (names lower snake, no collision with a logical name) |
| `sites.<name>.blocking_link_types` | `["Blocks"]` | link type names that make a ticket blocked |
| `sites.<name>.transitions.default_resolution` | none | resolution used for done targets |
| `sites.<name>.transitions.assign_on_start` | none | `me`: assign `me` when a transition applies to an unassigned ticket whose target is not `done` |
| `sites.<name>.cache_ttl` | `24h` | field-list and identity cache lifetime |
| `sites.<name>.timeouts.connect`, `.read` | `10s`, `30s` | per site |
| `default_site` | none | used when no other rule selects; must name a configured site |
| `timeout` | `120s` | whole-command timeout |
| `me` | `{name: <OS user>}` | `{name, jira}`: `jira` is an expectation only; the account for a write (`me`, `own`, `--assign me`, dedupe) is a fresh `/myself`, and reads and `whoami` use the cached one (24 h, `--refresh`) |
| `attribution.enabled` | `true` | see [Attribution](#attribution) |
| `attribution.template` | `Drafted with AI assistance and posted via ticketctl.` | plain text, 1 to 300 characters, no control characters; one template for both actor kinds |
| `limits.body_chars` | `30000` | body size cap in characters (maximum 32767; verified for a single text node, the total across nodes is assumed) |
| `limits.retry_after_max` | `60s` | longest `Retry-After` honored; also bounded by the remaining command time |
| `hooks` | `{}` | per hook (one of the ten names), ordered `{cmd, timeout, async}` lists; `async` only for `post-` hooks |

Loader checks that JSON Schema cannot express, all `invalid_config` naming the key: `default_site` names a configured site; a project key claimed by two sites is allowed (selection then needs `--site`); `me` and `account_id` shapes; `queries` and `extra` names collide with no logical name; `email` xor `email_env`; the `url` rules above; and hook timeouts that are whole seconds from 1 to 600. Configuration errors name the key path and the constraint, never the input value or a source line. An `email_env` or `token_env` variable that is unset or empty is exit 12 `auth_required` naming it, only for commands that call the provider (`sites` and `validate` without `--probe` never read them). A configured account expectation (`me.jira` or `account_id`) that differs from `/myself` makes a write exit 2 (`invalid_config`, `reason: account_mismatch`) before anything is sent; for reads it is a warning, and `validate --probe` reports it.

Per-repo overrides do not exist: ticketctl has no repository notion, and its settings are scoped by site (the position `repos:` holds elsewhere).

### Credentials

The token comes only from the environment variable named by `token_env`. A missing or empty variable exits 12 `auth_required` naming the variable. A 401 exits 12 `bad_credentials`; a 403 on any call exits 12 `forbidden`. There is no token command and no stored login. Hooks and child processes get the environment minus every configured `token_env` and `email_env` variable. The token is never written to state, logs, audit, output or hook payloads, and error text never echoes it: proxy URLs are printed as `host:port`, and the top-level handler prints the exception class and a fixed message for exit 1, with a traceback only when `TICKETCTL_DEBUG` is set. Callers that run ticketctl inside a Claude session must list the token variable in claude-session's `secret_env`.

## Environment variables

Only the top-level scalars `timeout` and `default_site` accept `TICKETCTL_<KEY>` (`TICKETCTL_TIMEOUT`, `TICKETCTL_DEFAULT_SITE`), a deliberate narrowing of the design guide's nested form; nested environment keys are ignored with a `validate` warning. `TICKETCTL_HOOK`, `TICKETCTL_ID` and `TICKETCTL_OBJECT_TYPE` are hook variables, not config keys, `TICKETCTL_DEBUG` is a debug switch (a traceback for exit 1) and `TICKETCTL_LIVE` is the live-test switch. The token variable's name is whatever `token_env` says; `HTTPS_PROXY` and `NO_PROXY` are honored ([Invoking the provider](#invoking-the-provider)).

## Labels

None: ticketctl creates no long-lived objects and stores none. (The `tc-` label it adds to a created ticket is provider data, described under Dedupe.)

## TUI

None in 0.x.

## Files

Under `~/.local/state/ticketctl/` (respecting `XDG_STATE_HOME`; the same path on macOS). The directory is `0700` and files are `0600`, opened with `O_APPEND|O_NOFOLLOW|O_CREAT`; a directory or file not owned by the current uid is exit 1 `internal`.

- `cache/<site>/fields.json`, `cache/<site>/me.json`, `cache/<site>/priorities.json`: replaced atomically, refetched when missing, corrupt or older than `cache_ttl`; never holds credentials. Every file stores `{schema_version, site, url, email_sha256, fetched_at, ...}` and is discarded on any mismatch of url or email hash; `me.json` also stores `time_zone`. `priorities.json` holds the provider's priority list in order.
- `locks/`: a `0700` directory of 256 bucket lock files for dedupe, never removed; nothing accumulates ([Dedupe](#dedupe)).
- `audit.jsonl`: public, append-only (schema `schemas/audit.json`), kept forever.
- `log.jsonl`: public request log (schema `schemas/log.json`), rotated at 10 MiB keeping `log.jsonl.1` to `.3` under the same flock.

Appends take an `flock`. There is no database, no `cron` command and no `reconcile` or `prune`: nothing needs maintenance.

## Audit and log

**Audit.** One record pair per provider write call (GETs are not records): `result: attempted` before the call and the outcome after. `step` numbers the write calls of the command from 1, `step_kind` is the plan step kind, and `attempt_id` is shared by the command and is in `details.attempt_id` of every failure envelope after the `attempted` record. `result` is `attempted`, `ok`, `failed` (a definite refusal or error, `changed` false), `partial` (a definite failure after an earlier step of the same command landed, `changed` false) or `unknown` (a request that may have been sent with no answer: timeout, dropped connection, 5xx on a write; `changed` null whether or not an earlier step landed); `changed` is null for `attempted` and `unknown` and a boolean otherwise; `reason` is the error code of a failed, partial or unknown outcome. The command-level exit 12 `partial` with `outcome_unknown: true` is derived from the steps. The outcome of `create` fills `target.key` (null on the `attempted` record and when the create failed). A record with no outcome means ticketctl died or could not write it.

If the outcome append fails after a send, ticketctl warns on stderr, prints the result, exits with the real outcome, and the result `warnings` has `audit_incomplete`. If the `attempted` append of a step fails, that step is not sent: with nothing landed the command exits 1 `internal` (`reason: audit_failed`); with an earlier step landed it exits 12 `partial` whose failed step carries `code: internal`.

Fields: `v` (integer 1), `seq`, `ts`, `attempt_id`, `step`, `step_kind`, `actor`, `actor_source`, `stdin_tty`, `decision_id` (a list: hook-returned and flag ids both), `target` (`{site, key}`), `action`, `inputs`, `result`, `provider_ids`, `changed`, `reason`. `inputs` holds `plan_sha256`, `bodies` (sha256 and byte size), `dedupe_key`, `override_escalation`, `config` (`{path (null for the default location), files_sha256 (the loaded files), sha256 (the merged document)}`), `site_host` (the host and port requested), `hooks_sha256` (the effective hook list, configured plus `--hook`), `cli_hooks` (the `--hook` names) and `token_env` (the variable name, never a value), as cheap evidence of a bypass. No body text, no titles, no secrets. `seq` is assigned under the flock as 1 plus the last record's `seq` read from the file tail. `fsync` follows each `attempted` append.

**Log.** One line per provider call: `v`, `ts`, `site`, `host` (the host and port requested), `config_sha256` (the merged config), `method`, the full path template (`/rest/api/3/issue/{key}`, `/rest/agile/1.0/sprint/{id}/issue`, `{property}` for a property key; no values), `status`, `duration_ms`, `retries`, `rate_limit_remaining`. No headers, no bodies. A failed log write warns and never fails the command. `audit.jsonl` and `log.jsonl` are written with `ensure_ascii=True`; under the flock, if the last byte of the file is not `\n` a newline is written first, and if the tail line does not parse, `seq` is the maximum found by scanning the file.

## Upgrades

The interface version is semver; before 1.0 a breaking change is a `feat` whose title names the break. The cache has a `schema_version`; an unknown version is discarded and refetched. `audit.jsonl` and `log.jsonl` carry `v` and gain fields additively only (a minor release); the schemas are closed for the producer's tests and a consumer tolerates unknown properties.

## Residual risks

- A provider validator can still reject a write the plan did not predict (`rejected`, exit 12), for example a required assignee.
- Two concurrent creates with the same dedupe key on different hosts can both succeed; same-host runs are serialized by the bucket lock and the lag guard, and a gateway 5xx on a comment may still be committing when the re-run looks.
- `plan_sha256` guards against a changed ticket, not against a hostile same-user process: the `actor` claim is not authenticated, and the controls are the caller's to choose (see [Trust boundary](#trust-boundary)): a dry-run then `--yes` with its own hash, `--config` with no hooks, `--hook`, a pty answering the prompt, and `HTTPS_PROXY` or CA variables are same-user bypasses. The audit is evidence for a cooperative caller, not a tamper-evident record.
- Jira has no optimistic locking: a write that changes a field another writer changed in the last milliseconds is last write wins per field, and the hash narrows but does not close that window. A human takeover between a plan and its apply is seen only for the facts the plan reads.
- An assign plus a transition is not atomic. An assign step that appears between plan and apply (the assignee was cleared) runs as the plan said; it was in the confirmed intent. Two keyless `--yes` runs of a `transition` or `update` that overlap can both send; benign for idempotent end states.
- The sanitizer is category based: emoji sequences (a heart with U+FE0F, joiner sequences) and text that needs U+200C (Persian) are shown and written with visible markers. Homograph hosts in links are not detected.
- A page of rich-text search or comment results with one ticket nested deeper than 400 levels is `provider_changed` for the whole page; default `search` fields and single-key reads are unaffected.
- Closed-sprint membership under the backlog call, and moving a ticket out of a closed sprint, are unverified.
- Plan `from` and `to` and every error echo carry provider text: fence it like ticket text (echoes are sanitized and capped).
- An automatic comment on a customer-facing (service management) project might be customer-visible; this is unverified.
- A driven caller can create new files with `export --to`.
- Per-user transition filtering, 429 behavior, scoped API tokens, moved or archived tickets and a hidden ticket's status code were not verified live; the retry rules are conservative because of that.
- Ticket text is untrusted; ticketctl sanitizes control characters but does not fence.

## Platform notes

Pure standard library; Linux, WSL and macOS behave the same. Python 3.12 and 3.13. Timestamps are normalized to UTC; local time is never printed. There is no sandbox or process work in ticketctl.
