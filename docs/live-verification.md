# ticketctl: live verification runbook

Status: the suites exist; nothing in this repository has been verified live by them yet. A run only records what it saw; a human turns an observation into a dated fact in [design.md](design.md#live-findings-log).

The live suites settle the facts that the fake provider (`tests/fake_provider.py`, its `UNVERIFIED` list) and the code only assume. They are two `live`-marked pytest modules under `tests/live/`, run by hand against your own site, never in CI:

- `tests/live/test_live_read.py` (issue #37) reads and writes nothing.
- `tests/live/test_live_write.py` (issue #38) creates at most three titled test tickets, posts one comment and closes every ticket it created.

Both drive the `ticketctl` command line as a subprocess, like a real consumer, and make no HTTP request of their own. The harness logic (skipping, redaction, the request budget, the closer) is tested in normal CI against the fake provider by `tests/live/test_harness.py`.

## Read this before the write suite

The write suite changes a real site.

- A comment cannot be deleted. The suite posts exactly one, on one of its own test tickets, and refuses a second one on any ticket.
- A ticket cannot be deleted either, only closed. The suite creates at most three, titled `ticketctl live test <run id> ...`, and closes each as done with a "won't do" resolution in a `finally` (the module fixture `write_world`), also when a test fails, the request cap stops the run or you interrupt it. A ticket the closer could not close is printed at the end ("TEST TICKETS LEFT OPEN") so you can close it by hand.
- It writes only tickets it created (the key a create returned, and after a create whose answer was lost, the tickets that carry this run's title marker).
- Sprint tests move one of its own tickets into a future sprint of the board you name and back to the backlog. Use a board and sprints you may disturb.
- The suite refuses to run the writes unless `TICKETCTL_LIVE=1` and `TICKETCTL_LIVE_WRITE=1` are both set and `TICKETCTL_LIVE_PROJECT` names a test project. Use a project made for testing: the tickets, labels and the comment stay in it.

## Prerequisites

1. A checkout with the dev environment (`uv sync`); the suites run `python -m ticketctl` from it.
2. A working ticketctl configuration for your site, in the default place (`~/.config/ticketctl/config.yaml`) or a file named by `TICKETCTL_LIVE_CONFIG`: the site URL, the account email, `token_env` naming the variable that holds your API token, and that variable set in your shell. The suites read nothing else about your site.
3. For the write suite: a project where test tickets are welcome, whose create screen needs nothing ticketctl cannot supply (it fills the summary, project, issue type, reporter and labels; `create --dry-run` names the required fields it cannot).
4. For the sprint probes: a scrum board with at least one future sprint (two to check move versus append).

The suites never default to anything real: every probe target comes from an environment variable below, and a probe whose variable is missing is skipped with a clear reason, never failed.

## Environment

Nothing here is read from the repository. Set the variables in your shell; the values are masked in the findings files.

| Variable | Needed by | Meaning |
|---|---|---|
| `TICKETCTL_LIVE=1` | both | turns the live suites on (also refused when `CI` is set) |
| `TICKETCTL_LIVE_WRITE=1` | write | second switch: you know the write suite creates tickets and a comment |
| `TICKETCTL_LIVE_PROJECT` | write (read: optional) | the test project key; the read suite uses it, else the project of `TICKETCTL_LIVE_TICKET`, for the search probes |
| `TICKETCTL_LIVE_SITE` | optional | the site name in your config (`--site`), when the config holds several |
| `TICKETCTL_LIVE_CONFIG` | optional | a config file (`--config`) instead of the default location |
| `TICKETCTL_LIVE_TICKET` | read | a ticket with comments you can read (`get`, `comments`, `transitions`) |
| `TICKETCTL_LIVE_MOVED_TICKET` | read, optional | the old key of a ticket that was moved to another project |
| `TICKETCTL_LIVE_HIDDEN_TICKET` | read, optional | a key that exists but that your account cannot see |
| `TICKETCTL_LIVE_MISSING_TICKET` | read, optional | a key that does not exist (default: the probe project with number 999999999) |
| `TICKETCTL_LIVE_BOARD` | both, optional | a scrum board id for the sprint probes |
| `TICKETCTL_LIVE_KANBAN_BOARD` | read, optional | a board id without sprints (for the 400 probe) |
| `TICKETCTL_LIVE_TOKEN_ENV` | both | the name (not the value) of the variable that holds your site token (the `token_env` of your config): the child process gets that variable and the wrong-token probe overrides it; a reserved name (`HOME`, `PATH`, `XDG_*`, `TICKETCTL_*`) is refused |
| `TICKETCTL_LIVE_PASS_ENV` | optional | comma separated names of other variables the child needs (for example the `email_env` of your config); the values are masked in the findings |
| `TICKETCTL_LIVE_TYPE` | write, optional | issue type for the created tickets (default `Task`) |
| `TICKETCTL_LIVE_RESOLUTION` | write, optional | the closing resolution (default `Won't Do`) |
| `TICKETCTL_LIVE_DONE_STATUS` | write, optional | the done status name (default `Done`) |
| `TICKETCTL_LIVE_FINDINGS_DIR` | optional | where findings go (default `live-findings/` in the checkout, git-ignored) |

The suites give every run a private state directory (`XDG_STATE_HOME`), so a run starts with a cold cache, its request log holds exactly its own requests, and your real state, audit log and locks are untouched. Your configuration directory is used as it is. The `ticketctl` child process gets a minimal environment: `PATH`, `HOME`, `LANG`, `LC_*`, `TZ`, `XDG_*`, `TICKETCTL_*`, the proxy and CA bundle variables, and the variables named by `TICKETCTL_LIVE_TOKEN_ENV` and `TICKETCTL_LIVE_PASS_ENV`; nothing else of your environment reaches it. The read suite also refuses `--yes` and any command whose request log shows a write, and the write suite's runner refuses a create beyond three, or a comment the ledger did not approve (one per ticket), before any request is sent.

## Commands

Read suite:

```sh
TICKETCTL_LIVE=1 TICKETCTL_LIVE_TICKET=<a ticket> TICKETCTL_LIVE_PROJECT=<project> \
  uv run pytest -m live tests/live/test_live_read.py -v
```

Write suite (read the section above first):

```sh
TICKETCTL_LIVE=1 TICKETCTL_LIVE_WRITE=1 TICKETCTL_LIVE_PROJECT=<test project> \
  TICKETCTL_LIVE_BOARD=<board id> \
  uv run pytest -m live tests/live/test_live_write.py -v
```

`-m live` is needed because the default run deselects the marker. Without the variables, `uv run pytest -m live` skips every live test and says why; plain `uv run pytest` does not collect them. The `-s` flag shows the progress lines; the findings summary and the findings file path print at the end of each module either way.

## Request budget

Requests are counted from ticketctl's own request log (`log.jsonl`: one line per provider call, plus its retries), read from the run's private state directory. The runner checks the count before each command (it does not start one at or over the cap) and after it (a command that went over the cap aborts the suite). The module fixtures then close the test tickets with a reserve above the cap, so cleanup is never starved.

- Write suite: about 110 requests for a full run against the fake provider with one future sprint (measured by `test_the_write_suite_runs_against_the_fake_and_closes_everything`; a site with two future sprints, a slow label index or retries costs a few more), cap 130, cleanup reserve 25. The issue's estimate of about 60 did not account for the plan-then-apply protocol: every write is a `--dry-run` (reads only: the runner asserts it sent no write) followed by the applied plan with `--expect-plan`, and each of the two commands re-reads the ticket, the account and the transitions. One test ticket serves every probe to keep the count down (a second ticket would add about twenty requests); the three-ticket ceiling stays as a guard. Typical costs: a create 10, a transition 10, an update 6 to 8, a comment 9, a sprint add 9, the close 10.
- Read suite: about 40 requests against the fake with the optional probes skipped, up to about 55 on a site with the optional probes and more than 150 tickets, cap 70, reserve 25.

A run that stops at the cap exits with code 3 and says "the live suite hit its request cap". The cap is a constant in `tests/live/conftest.py`.

## Reading the findings file

Each module writes `findings-<suite>-<UTC time>.json` and a `.md` twin under `live-findings/` (mode 0600) and prints a summary at the end. A finding has the checklist id, a title, a status and the observed data.

- `observed`: what the run saw, with no comparison.
- `differs`: the real behavior is not what the fake provider and the code assume (the finding's `expected` says what was assumed). Act on these first.
- `skipped`: the probe needed a site feature or variable that was absent.
- `error`: the probe could not tell.

Findings hold shapes, counts, status codes and codes, not ticket text. Every string is also masked by patterns: emails, URLs and hosts, ticket keys, custom field ids (`customfield_` followed by digits, and the `cf[...]` form), account ids, long opaque strings and credential headers, plus the literal values you named in the `TICKETCTL_LIVE_*` variables (site, config, project, tickets, boards, the key and pass variable names, and a type, resolution or done status other than the defaults) and the values of the variables named by `TICKETCTL_LIVE_TOKEN_ENV` and `TICKETCTL_LIVE_PASS_ENV` and of any variable whose name looks like a credential. A pattern can miss free text, so read the file before sharing it.

To record a result, follow [Live findings log](design.md#live-findings-log): one dated line per settled fact in `docs/design.md`, `docs/interface.md` first when a rule changes, and the `UNVERIFIED` constant of `tests/fake_provider.py` updated (delete the line a finding settles, or change the fake's default and add a test). The label index lag finding (W01) also decides the dedupe lag guard bound (D-36).

## What the probes check, and what they cannot

The table maps every item of the checklist in issue #3 and of the review notes on issues #3, #37 and #38 to the live test that observes it, or says why none can. It is rendered from `tests/live/checklist.py` and a test keeps the two equal. Kinds:

- A test name alone: the test observes the fact directly.
- "indirect": the test sees the fact only through ticketctl's normalized output, which hides the raw response.
- "needs a human decision or another environment": no repeatable live test can settle it (another account, another machine, a fault a site cannot produce on demand, or an oversized write that cannot be undone). Where a test records related data passively it is named.

<!-- checklist:begin -->
| Id | Source | Fact | Probe | Needs |
|---|---|---|---|---|
| L01 | #3 | A missing key and a hidden key: the HTTP status of each (404 or 403) and the CLI error code, so a consumer can treat only not_found as gone | `test_get_missing_and_hidden_status` | TICKETCTL_LIVE_HIDDEN_TICKET and a missing key |
| L02 | #3, #69 | A moved ticket: the old key resolves to the canonical key, requested_key keeps the typed key | `test_get_moved_ticket` | TICKETCTL_LIVE_MOVED_TICKET |
| L03 | #3 | An archived ticket | needs a human decision or another environment: needs a ticket the site archived: name it with TICKETCTL_LIVE_HIDDEN_TICKET and compare with L01 (the site decides the status) | none |
| L04 | #3 | The statusCategory names the --status-category flag compiles to (To Do, In Progress, Done) | `test_status_category_names` | none |
| L05 | #3, #10 | The order of the provider's priority list (rank order) | `test_priority_order` | none |
| L06 | #3 | The phrase compile of --text against live search | `test_text_and_scanner_samples` | none |
| L07 | #3, #57 | A backslash outside a quoted string and the single-quote handling match the scanner's assumptions | `test_text_and_scanner_samples` (indirect: the ticketctl scanner refuses a backslash outside a quoted string before Jira sees it (exit 22), so only a backslash inside a phrase and the single-quote handling are observed) | none |
| L08 | #3, #57, #17 | The incremental refresh combination: a named query, --updated-since, --order-by updated:asc and --cursor paging | `test_incremental_refresh_combination` (indirect: no named query is run (the suite has none to name): the run covers the flag combination only, observed through the printed order and the cursor) | none |
| L09 | #3, #57 | How Jira reads instants either side of a daylight-saving fall-back fold | needs a human decision or another environment: needs a site user time zone with a fold on a known day and tickets updated inside it: a manual JQL experiment, not a repeatable suite | none |
| L10 | #17 | search/jql answers {issues, nextPageToken}, the token only while more exist; maxResults 100 is honored with an explicit field list; a token stays valid for a whole paging run | `test_search_multi_page_cursor` (indirect: the CLI hides the raw response: the page size and the token are seen only as ticket counts, the next cursor and the request count) | at least 150 tickets visible in the probe project |
| L11 | #17 | A rejected page token is a 400 (cursor_expired on a first page) | `test_search_rejected_cursor` | the multi-page search of L10 |
| L12 | #17 | An empty page may or may not carry a token | needs a human decision or another environment: cannot be provoked on demand; the multi-page run records any empty page it meets | none |
| L13 | #17 | search/approximate-count answers {count} and accepts an ORDER BY tail | `test_search_count_with_order_by` | none |
| L14 | #17, #10 | jql/parse?validation=strict reports every semantic error the search would turn into an empty 200 | `test_search_semantic_jql_is_caught_by_parse` | none |
| L15 | #10 | The wording of every JQL error message and the parse response beyond errors | `test_search_semantic_jql_is_caught_by_parse` (indirect: the CLI prints the provider message sanitized; the raw response needs a raw call) | none |
| L16 | #65 | validate --probe: the parse endpoint answers 200 with queries[].errors; validation=strict and a one-element queries array are accepted; a fresh /myself needs no extra permission | `test_validate_probe` | named queries in the user's config for the parse part |
| L17 | #11 | GET /myself answers 200 with string accountId, displayName and timeZone | `test_whoami_shape` | none |
| L18 | #58, #10 | The 401 body for a wrong token, and that a wrong token is bad_credentials | `test_wrong_token_is_a_401` (indirect: the CLI prints the error code and the HTTP status, not the 401 body) | TICKETCTL_LIVE_TOKEN_ENV (the name of the token variable of the site) |
| L19 | #10 | The 403 body for a bearer header, the 410 body, and Basic auth with a wrong email but a valid token | needs a human decision or another environment: needs hand-built requests (a bearer header, the old search endpoint, an altered email): ticketctl never sends them | none |
| L20 | #3, #58, #10 | 429 behavior, the Retry-After format and the rate-limit headers | needs a human decision or another environment: a real 429 cannot be provoked politely; the passive log probe records whether the rate-limit header was present and any 429 the run met (passive: `test_passive_request_log`) | none |
| L21 | #58 | CONNECT-tunnel behavior through a real corporate proxy (407, error pages) | needs a human decision or another environment: needs a proxied network: run the read suite there with HTTPS_PROXY set | none |
| L22 | #3 | Scoped API tokens | needs a human decision or another environment: needs a second config with a scoped token: run the read suite with TICKETCTL_LIVE_CONFIG pointing at it and compare | none |
| L23 | #3 | Per-user transition filtering | needs a human decision or another environment: needs a second account with other permissions: run the read suite as it | none |
| L24 | #3 | Comment visibility on a customer-facing (service management) project | needs a human decision or another environment: needs such a project and an agent and a customer account | none |
| L25 | #64 | GET /field, /priority and /resolution answer plain arrays; system fields have custom:false and their ids; the sprint field schema ends :gh-sprint; option and user value shapes; the epic-link schema | `test_fields_check_and_list` (indirect: the CLI prints the mapping, not the raw field list) | none |
| L26 | #67 | Normalization: the parent's hierarchyLevel, link ends, a field not on the screen omitted, sprint value fields, epic link, attachment fields, a summary-only fields request | `test_get_default_shape`, `test_get_fields_summary_only` (indirect: the run records the incomplete list and the shape of the normalized ticket) | none |
| L27 | #69, #18 | GET /issue/{key} honors fields; the comment endpoint honors orderBy and maxResults; expand=properties embeds comment properties; total counts every comment | `test_get_default_shape`, `test_comments_shape` (indirect: the CLI hides which parameters the provider honored: only the printed list, its order, the own-comment properties and the total are observed) | a ticket with comments (TICKETCTL_LIVE_TICKET) |
| L28 | #18 | expand=transitions.fields gives required fields with name, schema.type and allowedValues; hasScreen is present on every transition | `test_transitions_shape` | none |
| L29 | #18 | The agile shapes (values, isLast, originBoardId, startDate, endDate, board type and name); the sprint endpoint accepts a single state; board paging | `test_sprints_list_and_show` | TICKETCTL_LIVE_BOARD |
| L30 | #18 | A 404 on /board/{id}/sprint means a missing board; a 400 means a board without sprints | `test_board_errors` | TICKETCTL_LIVE_KANBAN_BOARD for the 400 |
| L31 | #39 | Whether the platform reads work on macOS (paths, files, prompt) | needs a human decision or another environment: the macOS checklist (#39): another machine | none |
| L32 | #73, #52 | export --to and the input-file reader on macOS and APFS, a real separate writer process, a real full disk | needs a human decision or another environment: another machine and a manual setup (#39) | none |
| W01 | #3, #31, #84 | The label index lag after a create with the label in the body (the dedupe lag guard is a bound until measured) | `test_create_with_dedupe_key_and_the_label_lag` | none |
| W02 | #3, #31 | An explicit unassigned value in a create body leaves the ticket unassigned | `test_create_explicit_unassigned` | none |
| W03 | #31 | createmeta shapes (issueTypes or values, fields as list or object, paging) and the POST /issue response carrying a usable key | `test_create_with_dedupe_key_and_the_label_lag` (indirect: a create that works proves the shapes are accepted, not which one the site sent) | none |
| W04 | #31 | A site that refuses labels on create answers 400 with a labels field error | needs a human decision or another environment: needs a project that refuses labels: a site setting the suite cannot change | none |
| W05 | #31, #3 | The parent rejection shape (Sub-task without a parent, Task under a Task: 400 with errors.parent) | needs a human decision or another environment: already verified by spike D (V-29); a live run would have to create a failing ticket: left to the human if wanted | none |
| W06 | #3, #23 | The order of add and remove within one update (labels) | `test_update_title_labels_order` (indirect: one update adds one label and removes another and the result is read back; the same label in both is refused by ticketctl (exit 22), so the add-versus-remove order for one label is not probed) | none |
| W07 | #66, #23 | The unchanged check by the body hash of the current ADF may produce a spurious update (the provider adds localId and the like) | `test_update_description_is_unchanged_on_rerun` | none |
| W08 | #66 | Payload shapes (option, priority, user, null clears), the datetime format, ADF accepted in description and the transition comment, editmeta and createmeta keyed by field id | `test_update_title_labels_order`, `test_transition_with_assign` (indirect: only the fields the suite sets are exercised; the rest needs site-specific fields) | none |
| W09 | #23, #72 | PUT on the assignee, POST on transitions, POST on comment: request and response shapes accepted; the 4xx body shapes | `test_transition_with_assign`, `test_comment_with_key` (indirect: the write succeeding proves the shape; a refusal needs a deliberately bad write) | none |
| W10 | #33, #63, #3 | A keyed comment: ticketctl.meta accepted as a property in the comment create request; the comment list is read-your-writes; expand=properties returns the own comment's property | `test_comment_with_key` | none |
| W11 | #63, #3 | Comment property size and key limits, the text limit of descriptions and rich-text custom fields, an empty text node, a total text limit across ADF nodes, a 300 KB body | needs a human decision or another environment: needs oversized writes; a comment cannot be deleted, so this is a human decision (a description on a test ticket is the safer place) | none |
| W12 | #34 | Adding a ticket that is already in an open or future sprint to a second one moves it (plan assumes a move, warning sprint_moved) rather than appending | `test_sprint_add_move_and_remove` | a board with two open or future sprints |
| W13 | #34 | sprint remove sends the ticket to the backlog: with several open sprints | `test_sprint_add_move_and_remove` | a board with two open or future sprints |
| W14 | #3, #34 | Closed-sprint membership under the backlog call, and moving a ticket out of a closed sprint | needs a human decision or another environment: needs a ticket whose only sprint entries are closed; a sprint cannot be closed and reopened by the suite: a human sets one up and runs sprint remove on it | none |
| W15 | #34 | What a 400 and a 404 mean on the sprint add and backlog calls | `test_sprint_errors` | a board |
| W16 | #34 | sprint add without --board on a site with several boards is ambiguous_board | needs a human decision or another environment: the user's config usually names a board; the human runs a dry-run sprint add against a config with none | none |
| W17 | #26, #72 | What Jira does after a 5xx, a reset or a drop on a write; whether a write 429 carries Retry-After and never applies; a 2xx with an unusable body | needs a human decision or another environment: faults a real site cannot be made to produce on demand | none |
| W18 | #10 | The 400 body of an unknown transition id and of a missing resolution, the ADF-required 400 for a plain string, the label, summary, parent and notifyUsers texts | needs a human decision or another environment: ticketctl validates these first, so the provider is never asked: needs raw requests | none |
| W19 | #3 | Whether a done transition from a non-assigned sub-task needs a parent; the transition order (assign then done) | `test_transition_with_assign` (indirect: the suite closes tickets with the normal flow; a sub-task needs a parent ticket) | none |
| W20 | #38 | The closer: every created ticket ends done with the won't-do resolution | `test_every_created_ticket_is_closed` | none |
<!-- checklist:end -->

## Probes that depend on the site

A probe whose precondition is missing skips with the reason in the pytest report:

- No `TICKETCTL_LIVE_TICKET`, `..._MOVED_TICKET`, `..._HIDDEN_TICKET`, `..._BOARD`, `..._KANBAN_BOARD` or `..._TOKEN_ENV`: the probes that need them skip.
- A project with fewer than 101 visible tickets: the multi-page search records the count and skips; with 150 or fewer, the cursor probes skip too.
- A site with no board, or a board with no future sprint: the sprint writes skip; with one future sprint only the move-versus-append probe is recorded as skipped.
- A create screen with required fields ticketctl cannot supply: the write suite skips its create probes (and then has nothing to probe on tickets); choose another type or project.
- A workflow with no non-done transition free of required fields: the transition probe skips.

## If something goes wrong

- A test fails with a ticketctl exit code or error code: the interface was broken or the site refused; the message names the command and code (never a value). Re-run the one test with `-k`.
- The suite stops with "request cap": nothing is wrong with your site; read the per-command costs in the verbose output and raise the cap only on purpose.
- "TEST TICKETS LEFT OPEN" at the end: close those keys by hand (transition them to done with the won't-do resolution); nothing else was created.
- Interrupted with Ctrl-C: the module finalizer still closes the tickets; if the terminal was killed, find them by the title prefix `ticketctl live test` in the test project.
