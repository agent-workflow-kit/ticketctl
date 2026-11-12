# ticketctl

Normalized ticket access (Jira Cloud first; other providers later). Read, search, comment, transition, create and update tickets from a terminal or a script, with one confirm-by-default write flow.

## Status

Designed; not yet implemented. The normative contract is [docs/interface.md](docs/interface.md) (version 0.1.0-design); the decisions and live verification are in [docs/design.md](docs/design.md).

## Layer

L1 (primitive). Works fully standalone.

## What it does

- Reads tickets as a provider-neutral object (status with category, estimate, sprints, parent, links, a computed `blocked` flag, Markdown text) and searches them with named queries, structured filters or JQL, validating every query first so a typo is an error rather than an empty list.
- Writes (`transition`, `comment`, `create`, `update`, `sprint add|remove`) share one flow: an idempotency check first, a plan with a hash, `--dry-run`, one confirmation (a decline is exit 20), per-kind hooks, a lock and stale-ticket check and an audit record pair. There is no pending-confirmation store: a non-interactive write without `--yes` exits 13 and the calling tool records the pending confirmation.
- Text goes in as Markdown from a file or stdin and is converted to Atlassian's rich-text format; no wiki markup, mention or card is ever created from text.
- Field mappings (story points, acceptance criteria, sprint and so on), named queries and organization rules are config, never code.
- Stateless: a small cache, `audit.jsonl` and `log.jsonl`.

## Commands

Finalized in [docs/interface.md](docs/interface.md#commands).

```text
ticketctl whoami | get KEY... | search | comments KEY | transitions KEY | sprints | fields | sites | validate
ticketctl transition KEY --to STATUS | comment KEY --body-file F | create --title T --body-file F | update KEY --set k=v
ticketctl sprint add KEY --sprint open|next|ID | sprint remove KEY
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

- Jira Cloud REST API v3 and the agile API: reached directly through ticketctl's own standard-library client (no other tool or CLI is needed).
- Python packages: `pyyaml`, `pydantic`.

## Install

```sh
uv tool install git+https://github.com/agent-workflow-kit/ticketctl
```

## Configuration

`~/.config/ticketctl/config.yaml` (drop-ins in `config.d/`). See [config.example.yaml](config.example.yaml) and [docs/interface.md](docs/interface.md#config). Credentials come only from the environment variable named by `token_env`; a missing one fails with a message naming it.

## Hooks

One `pre-` and one `post-` hook per kind of write; a `pre-` hook runs after the confirmation and can veto (exit 11) or escalate (exit 13). Payload fields and rules are in [docs/interface.md](docs/interface.md#hooks).

| Kind | Hooks |
|---|---|
| transition | `pre-transition`, `post-transition` |
| comment | `pre-comment`, `post-comment` |
| create | `pre-create`, `post-create` |
| update | `pre-update`, `post-update` |
| sprint add and remove | `pre-sprint`, `post-sprint` |

## Environment

- `TICKETCTL_TIMEOUT` and `TICKETCTL_DEFAULT_SITE` set the two top-level scalar config keys; nested keys cannot come from the environment.
- The API token is read only from the variable named by `token_env` (and the email from `email_env` when used).
- Hooks receive `TICKETCTL_HOOK`, `TICKETCTL_ID` and `TICKETCTL_OBJECT_TYPE`; they are hook variables, not config keys. `TICKETCTL_DEBUG` is a debug switch (a traceback for exit 1) and `TICKETCTL_LIVE` is the live-test switch.

## Exit codes

Shared codes from the design guide (0, 1, 2, 10, 11, 12, 13, 20, 21) plus 22 `invalid_request`. The full table is in [docs/interface.md](docs/interface.md#exit-codes). Reasons: exit 2 `human_only`, `yes_requires_expect_plan`, `dedupe_key_required`, `stdin_conflict`, `invalid_combination`, `missing_option`, `invalid_argument`, `config_invalid`, `no_sites`, `account_mismatch`; exit 20 equal to the code (`stale_ticket`, `precondition_failed`, `no_such_transition`, `sprint_closed`, `already_exists`, `declined`); exit 22 `invalid_query`, `unknown_field`, `not_editable`, `invalid_value`, `needs_resolution`, `missing_required_fields`, `node_not_allowed`, `body_too_large`, `empty_body`, `unknown_site`, `invalid_key`, `mixed_sites`, `invalid_file`, `secret_in_body`, `cursor_expired`. A failure after an earlier step landed is exit 12 `partial` with the applied steps in `details.result`.

## Documentation

- [awkit architecture](../awkit-docs/architecture.md) ([published](https://github.com/agent-workflow-kit/awkit-docs/blob/main/architecture.md))
- [awkit tool design guide](../awkit-docs/design-guide.md) ([published](https://github.com/agent-workflow-kit/awkit-docs/blob/main/design-guide.md))
- [interface](docs/interface.md), [design](docs/design.md)

## License

MIT — see LICENSE.
