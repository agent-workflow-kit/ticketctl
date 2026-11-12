# ticketctl

Normalized ticket access (Jira Cloud first; other providers later). Read, search, comment, transition, create and update tickets from a terminal or a script, with one confirm-by-default write flow.

## Status

Designed; not yet implemented. The normative contract is [docs/interface.md](docs/interface.md) (version 0.1.0-design); the decisions and live verification are in [docs/design.md](docs/design.md).

## Layer

L1 (primitive). Works fully standalone.

## What it does

- Reads tickets as a provider-neutral object (status with category, estimate, sprints, parent, links, a computed `blocked` flag, Markdown text) and searches them with named queries, structured filters or JQL, validating every query first so a typo is an error rather than an empty list.
- Writes (`transition`, `comment`, `create`, `update`, `sprint add|remove`) share one flow: a plan with a hash, `--dry-run`, one confirmation, per-kind hooks, a stale-ticket check and an audit record pair. There is no pending-confirmation store: a non-interactive write without `--yes` exits 13 and the calling tool records the pending confirmation.
- Text goes in as Markdown from a file or stdin and is converted to Atlassian's rich-text format; no wiki markup, mention or card is ever created from text.
- Field mappings (story points, acceptance criteria, sprint and so on), named queries and organization rules are config, never code.
- Stateless: a small cache, `audit.jsonl` and `log.jsonl`.

## Commands

Finalized in [docs/interface.md](docs/interface.md#commands).

```text
ticketctl whoami | get KEY... | search | comments KEY | transitions KEY | sprints | fields | sites | validate
ticketctl transition KEY --to STATUS | comment KEY --body-file F | create --title T --body-file F | update KEY --set k=v
ticketctl sprint add|remove KEY... --sprint open|next|ID
ticketctl export
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

## Exit codes

Shared codes from the design guide (0, 1, 2, 10, 11, 12, 13, 20, 21) plus 22 `invalid_request`. The full table with reasons is in [docs/interface.md](docs/interface.md#exit-codes).

## Documentation

- [awkit architecture](../awkit-docs/architecture.md) ([published](https://github.com/agent-workflow-kit/awkit-docs/blob/main/architecture.md))
- [awkit tool design guide](../awkit-docs/design-guide.md) ([published](https://github.com/agent-workflow-kit/awkit-docs/blob/main/design-guide.md))
- [interface](docs/interface.md), [design](docs/design.md)

## License

MIT — see LICENSE.
