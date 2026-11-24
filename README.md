# ticketctl

Normalized ticket access (Jira Cloud first; other providers later). Read, search, comment, transition, create and update tickets from a terminal or a script, with one confirm-by-default write flow.

## Status

Every command below is implemented. The live suites against a real site, the macOS checklist and a cron-style minimal-environment run are the remaining release-readiness work (Stage 6 of [docs/plan.md](docs/plan.md)). The normative contract is [docs/interface.md](docs/interface.md); the decisions and live verification are in [docs/design.md](docs/design.md).

## Layer

L1 (primitive). Works fully standalone.

## What it does

- Reads tickets as a provider-neutral object (status with category, estimate, sprints, parent, links, a computed `blocked` flag, Markdown text) and searches them with named queries, structured filters or a raw JQL query that stands alone, validating every query first so a typo is an error rather than an empty list.
- Writes (`transition`, `comment`, `comment-edit`, `comment-delete`, `attachment-add`, `create`, `update`, `sprint add|remove`, `link add|remove`, `resolution-backfill`; `link remove` deletes any link of the named type, direction and ticket, whoever made it) share one flow: an idempotency check first, a plan with a hash, `--dry-run`, one confirmation (a decline is exit 20), per-kind hooks, a lock and stale-ticket check and an audit record pair. There is no pending-confirmation store: a non-interactive write without `--yes` exits 13 and the calling tool records the pending confirmation.
- `resolution-backfill` refuses with `not_editable` on sites whose edit screen omits Resolution; this is unverified live (W24).
- Text goes in as Markdown from a file or stdin and is converted to Atlassian's rich-text format; no wiki markup, mention or card is ever created from text.
- Field mappings (story points, acceptance criteria, sprint and so on), named queries and organization rules are config, never code.
- Stateless: a small cache, `audit.jsonl` and `log.jsonl`.

## Commands

Finalized in [docs/interface.md](docs/interface.md#commands).

```text
ticketctl whoami | get KEY... | search | browse | comments KEY | changelog KEY | transitions KEY | sprints | sprints resolve open|next | fields | sites | validate
ticketctl transition KEY --to STATUS | comment KEY --body-file F | comment-edit KEY ID --body-file F | comment-delete KEY ID | attachment-add KEY --file F | create --title T --body-file F | update KEY --set k=v
ticketctl sprint add KEY --sprint open|next|ID | sprint remove KEY
ticketctl link add KEY --type NAME --direction inward|outward --other KEY | link remove KEY (same options)
ticketctl attachment-download KEY ID --to PATH
ticketctl resolution-backfill KEY [--resolution NAME]
ticketctl export
```

## Quick start

```sh
uv tool install git+https://github.com/agent-workflow-kit/ticketctl
mkdir -p ~/.config/ticketctl && cp config.example.yaml ~/.config/ticketctl/config.yaml
export JIRA_API_TOKEN=...
ticketctl whoami
ticketctl search --query my_open --json
ticketctl transition ABC-123 --to Done --dry-run
```

## Dependencies

- Jira Cloud REST API v3 and the agile API: reached directly through ticketctl's own client built on the standard library (no other tool or CLI is needed).
- Python packages: `pyyaml`, `pydantic`.

## Install

```sh
uv tool install git+https://github.com/agent-workflow-kit/ticketctl
```

## Configuration

`~/.config/ticketctl/config.yaml` (drop-ins in `config.d/`). See [config.example.yaml](config.example.yaml) and [docs/interface.md](docs/interface.md#config). Credentials come only from the environment variable named by `token_env`; a missing one fails with a message naming it.

## Hooks

One `pre-` and one `post-` hook per kind of write; a `pre-` hook runs after the confirmation (or before it, with `placement: before_confirm` in its config entry) and can veto (exit 11) or escalate (exit 13); `--dry-run --run-hooks` runs the `pre-` hooks without writing. Payload fields and rules are in [docs/interface.md](docs/interface.md#hooks).

| Kind | Hooks |
|---|---|
| transition | `pre-transition`, `post-transition` |
| comment | `pre-comment`, `post-comment` |
| comment edit | `pre-comment-edit`, `post-comment-edit` |
| comment delete | `pre-comment-delete`, `post-comment-delete` |
| attachment add | `pre-attachment-add`, `post-attachment-add` |
| resolution backfill | `pre-resolution-backfill`, `post-resolution-backfill` |
| create | `pre-create`, `post-create` |
| update | `pre-update`, `post-update` |
| sprint add and remove | `pre-sprint`, `post-sprint` |
| link add and remove | `pre-link`, `post-link` |

## Environment

- `TICKETCTL_TIMEOUT`, `TICKETCTL_DEFAULT_SITE` and `TICKETCTL_READ_ONLY` set the three top-level scalar config keys (`TICKETCTL_READ_ONLY=1` turns read-only mode on and nothing in the same process turns it off (an accident guard, not a security boundary), see [Read-only mode](docs/interface.md#read-only-mode)); nested keys cannot come from the environment.
- The API token is read only from the variable named by `token_env` (and the email from `email_env` when used).
- Hooks receive `TICKETCTL_HOOK`, `TICKETCTL_ID` and `TICKETCTL_OBJECT_TYPE` (and `TICKETCTL_DRY_RUN=1` under `--dry-run --run-hooks`); they are hook variables, not config keys. `TICKETCTL_DEBUG` is a debug switch (a traceback for exit 1) and `TICKETCTL_LIVE` is the live-test switch.

## Exit codes

Shared codes from the design guide (0, 1, 2, 10, 11, 12, 13, 20, 21) plus 22 `invalid_request`. The full table is in [docs/interface.md](docs/interface.md#exit-codes). Reasons: exit 2 `human_only`, `yes_requires_expect_plan`, `dedupe_key_required`, `stdin_conflict`, `invalid_combination`, `missing_option`, `invalid_argument`, `config_invalid`, `no_sites`, `account_mismatch`; exit 20 equal to the code (`stale_ticket`, `precondition_failed`, `no_such_transition`, `sprint_closed`, `already_exists`, `declined`, `read_only`, `not_ticketctl_comment`, `restricted_comment`, `human_comment`); exit 12 `unavailable` with `locked`; exit 1 `internal` with `audit_failed` or `hooks_unavailable`; exit 22 `invalid_query`, `unknown_field`, `not_editable`, `invalid_value`, `needs_resolution`, `missing_required_fields`, `node_not_allowed`, `body_too_large`, `empty_body`, `unknown_site`, `invalid_key`, `mixed_sites`, `invalid_file`, `secret_in_body`, `cursor_invalid`, `cursor_expired`. A failure after an earlier step landed is exit 12 `partial` with the applied steps in `details.result`.

## Documentation

- [awkit architecture](../awkit-docs/architecture.md) ([published](https://github.com/agent-workflow-kit/awkit-docs/blob/main/architecture.md))
- [awkit tool design guide](../awkit-docs/design-guide.md) ([published](https://github.com/agent-workflow-kit/awkit-docs/blob/main/design-guide.md))
- [interface](docs/interface.md), [design](docs/design.md), [live verification runbook](docs/live-verification.md)

## License

MIT — see LICENSE.
