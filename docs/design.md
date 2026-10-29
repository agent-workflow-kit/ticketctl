# ticketctl: design

Status: Seeded from awkit architecture v1.4; not yet designed.

## Role and layer

Normalized ticket access (Jira first; other providers later). Layer 1 (primitive). Must work fully standalone (architecture §2, C2).

## Responsibilities

From architecture §5.1:

Normalized ticket access. Jira first; other providers later.

- Commands: `get`, `search`, `transition`, `comment --body-file`, `create --body-file`, `update`, `sprint` helpers.
- Uses: Jira REST API.
- Notes: Writes support `--dry-run` and are confirm-by-default. ticketctl has no pending-confirmation store: a non-interactive write without `--yes` exits 13 without acting, and the calling tool records the pending confirmation (section 12.4). Field mappings (story points, acceptance criteria, sprint field and so on) are config, never code (C1).

## Uses

- Jira REST API: binding: TBD (A/B/C), decided in this tool's design session

## Architecture sections to read

Section numbers refer to `../awkit-docs/architecture.md` (v1.4).

- §2: hard constraints, especially C1 (no project keys, custom-field IDs or hostnames in code).
- §6: conventions summary.
- §12.1: public defaults (ticket transitions: show and confirm; ticket creation: always confirm).
- §12.4: confirmations, holds and `--yes` (ticketctl writes are confirm-by-default primitives without a pending store).
- §13.2: prompt injection: transport of bodies via `--body-file`; trust tiers (rule 7).
- §13.3: credentials (`token_env`).
- §14: audit (transition, comment, create, update are external actions).
- §20: this tool's constraints and open topics.

## Constraints (decided)

From architecture §20: none.

## Open topics

From architecture §20:

- provider interface for future sources
- JQL/filter config
- write safety flow details
- own client vs invoking user-installed tools

## Decisions

| Decision | Rationale | Date |
|---|---|---|

## Questions for the design session

1. What provider interface lets future ticket sources plug in after Jira?
2. How are JQL and other filters expressed in config?
3. What are the details of the write safety flow (dry-run output, confirmation prompt, exit 13 without a TTY)?
4. Does ticketctl ship its own HTTP client, or invoke user-installed tools?
