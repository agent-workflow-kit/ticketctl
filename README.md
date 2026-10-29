# ticketctl

Normalized ticket access (Jira first; other providers later).

## Status

Pre-design: interface not yet specified; see [docs/design.md](docs/design.md).

## Layer

L1 (primitive).

## What it does

- Gets and searches tickets, transitions them, comments, creates and updates them, and offers sprint helpers.
- Writes support `--dry-run` and are confirm-by-default. There is no pending-confirmation store: a non-interactive write without `--yes` exits 13 without acting, and the calling tool records the pending confirmation.
- Field mappings (story points, acceptance criteria, sprint field and so on) are config, never code.

## Indicative commands

Indicative, finalized in [docs/interface.md](docs/interface.md).

```text
  ticketctl get
  ticketctl search
  ticketctl transition
  ticketctl comment --body-file
  ticketctl create --body-file
  ticketctl update
  ticketctl sprint helpers
```

## Dependencies

- Jira REST API: binding: TBD (A/B/C), decided in this tool's design session

## Install

```sh
uv tool install git+https://github.com/agent-workflow-kit/ticketctl
```

## Documentation

- [awkit architecture](../awkit-docs/architecture.md) ([published](https://github.com/agent-workflow-kit/awkit-docs/blob/main/architecture.md))
- [awkit tool design guide](../awkit-docs/design-guide.md) ([published](https://github.com/agent-workflow-kit/awkit-docs/blob/main/design-guide.md))

## License

MIT — see LICENSE.
