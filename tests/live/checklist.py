"""The live-verification checklist: every unverified fact, and the live test that probes it.

The source is issue #3 (the checklist) and the "unverified provider behavior" notes the review
cycles left on it and on #37 and #38. Each entry names the live test (a function of
``test_live_read.py`` or ``test_live_write.py``) that observes the fact, or says why no live test
can: a human decision, another account, another machine or a fault a real site cannot be made to
produce. The runbook table (docs/live-verification.md) is rendered from this module and a test
keeps the two equal.

Nothing here claims a fact is verified: a probe only records what one run observed.
"""

from __future__ import annotations

from dataclasses import dataclass

PROBED = "probed"
INDIRECT = "indirect"  # observed only through the normalized output of the CLI
HUMAN = "human"  # needs a human decision, another account, another machine or a fault


@dataclass(frozen=True)
class Item:
    id: str
    source: str
    fact: str
    kind: str
    probes: tuple[str, ...] = ()
    why: str = ""  # for HUMAN: what is needed; for INDIRECT: what the CLI hides
    needs: str = ""  # a site feature or setting the probe needs (the test skips without it)


R = "test_live_read.py"
W = "test_live_write.py"

ITEMS: tuple[Item, ...] = (
    Item(
        "L01",
        "#3",
        "A missing key and a hidden key: the HTTP status of each (404 or 403) and the "
        "CLI error code, so a consumer can treat only not_found as gone",
        PROBED,
        ("test_get_missing_and_hidden_status",),
        needs="TICKETCTL_LIVE_HIDDEN_TICKET and a missing key",
    ),
    Item(
        "L02",
        "#3, #69",
        "A moved ticket: the old key resolves to the canonical key, "
        "requested_key keeps the typed key",
        PROBED,
        ("test_get_moved_ticket",),
        needs="TICKETCTL_LIVE_MOVED_TICKET",
    ),
    Item(
        "L03",
        "#3",
        "An archived ticket",
        HUMAN,
        why="needs a ticket the site archived: name it "
        "with TICKETCTL_LIVE_HIDDEN_TICKET and compare with L01 (the site decides the status)",
    ),
    Item(
        "L04",
        "#3",
        "The statusCategory names the --status-category flag compiles to "
        "(To Do, In Progress, Done)",
        PROBED,
        ("test_status_category_names",),
    ),
    Item(
        "L05",
        "#3, #10",
        "The order of the provider's priority list (rank order)",
        PROBED,
        ("test_priority_order",),
    ),
    Item(
        "L06",
        "#3",
        "The phrase compile of --text against live search",
        PROBED,
        ("test_text_and_scanner_samples",),
    ),
    Item(
        "L07",
        "#3, #57",
        "A backslash outside a quoted string and the single-quote handling "
        "match the scanner's assumptions",
        INDIRECT,
        ("test_text_and_scanner_samples",),
        why="the ticketctl scanner refuses a backslash outside a quoted string before Jira sees it (exit 22), so only a backslash inside a phrase and the single-quote handling are observed",
    ),
    Item(
        "L08",
        "#3, #57, #17",
        "The incremental refresh combination: a named query, "
        "--updated-since, --order-by updated:asc and --cursor paging",
        INDIRECT,
        ("test_incremental_refresh_combination",),
        why="no named query is run (the suite has none to name): the run covers the flag combination only, observed through the printed order and the cursor",
    ),
    Item(
        "L09",
        "#3, #57",
        "How Jira reads instants either side of a daylight-saving fall-back fold",
        HUMAN,
        why="needs a site user time zone with a fold on a known day and tickets updated "
        "inside it: a manual JQL experiment, not a repeatable suite",
    ),
    Item(
        "L10",
        "#17",
        "search/jql answers {issues, nextPageToken}, the token only while more "
        "exist; maxResults 100 is honored with an explicit field list; a token stays valid "
        "for a whole paging run",
        INDIRECT,
        ("test_search_multi_page_cursor",),
        why="the CLI hides the raw response: the page size and the token are seen only as ticket counts, the next cursor and the request count",
        needs="at least 150 tickets visible in the probe project",
    ),
    Item(
        "L11",
        "#17",
        "A rejected page token is a 400 (cursor_expired on a first page)",
        PROBED,
        ("test_search_rejected_cursor",),
        needs="the multi-page search of L10",
    ),
    Item(
        "L12",
        "#17",
        "An empty page may or may not carry a token",
        HUMAN,
        why="cannot be provoked on demand; the multi-page run records any empty page it meets",
    ),
    Item(
        "L13",
        "#17",
        "search/approximate-count answers {count} and accepts an ORDER BY tail",
        PROBED,
        ("test_search_count_with_order_by",),
    ),
    Item(
        "L14",
        "#17, #10",
        "jql/parse?validation=strict reports every semantic error the search "
        "would turn into an empty 200",
        PROBED,
        ("test_search_semantic_jql_is_caught_by_parse",),
    ),
    Item(
        "L15",
        "#10",
        "The wording of every JQL error message and the parse response beyond errors",
        INDIRECT,
        ("test_search_semantic_jql_is_caught_by_parse",),
        why="the CLI prints the provider message sanitized; the raw response needs a raw call",
    ),
    Item(
        "L16",
        "#65",
        "validate --probe: the parse endpoint answers 200 with queries[].errors; "
        "validation=strict and a one-element queries array are accepted; a fresh /myself needs "
        "no extra permission",
        PROBED,
        ("test_validate_probe",),
        needs="named queries in the user's config for the parse part",
    ),
    Item(
        "L17",
        "#11",
        "GET /myself answers 200 with string accountId, displayName and timeZone",
        PROBED,
        ("test_whoami_shape",),
    ),
    Item(
        "L18",
        "#58, #10",
        "The 401 body for a wrong token, and that a wrong token is bad_credentials",
        INDIRECT,
        ("test_wrong_token_is_a_401",),
        why="the CLI prints the error code and the HTTP status, not the 401 body",
        needs="TICKETCTL_LIVE_TOKEN_ENV (the name of the token variable of the site)",
    ),
    Item(
        "L19",
        "#10",
        "The 403 body for a bearer header, the 410 body, and Basic auth with a wrong "
        "email but a valid token",
        HUMAN,
        why="needs hand-built requests (a bearer header, the old search endpoint, an altered "
        "email): ticketctl never sends them",
    ),
    Item(
        "L20",
        "#3, #58, #10",
        "429 behavior, the Retry-After format and the rate-limit headers",
        HUMAN,
        ("test_passive_request_log",),
        why="a real 429 cannot be provoked politely; the passive log probe records whether the "
        "rate-limit header was present and any 429 the run met",
    ),
    Item(
        "L21",
        "#58",
        "CONNECT-tunnel behavior through a real corporate proxy (407, error pages)",
        HUMAN,
        why="needs a proxied network: run the read suite there with HTTPS_PROXY set",
    ),
    Item(
        "L22",
        "#3",
        "Scoped API tokens",
        HUMAN,
        why="needs a second config with a scoped token: run the read suite with "
        "TICKETCTL_LIVE_CONFIG pointing at it and compare",
    ),
    Item(
        "L23",
        "#3",
        "Per-user transition filtering",
        HUMAN,
        why="needs a second account with other permissions: run the read suite as it",
    ),
    Item(
        "L24",
        "#3",
        "Comment visibility on a customer-facing (service management) project",
        HUMAN,
        why="needs such a project and an agent and a customer account",
    ),
    Item(
        "L25",
        "#64",
        "GET /field, /priority and /resolution answer plain arrays; system fields "
        "have custom:false and their ids; the sprint field schema ends :gh-sprint; option and "
        "user value shapes; the epic-link schema",
        INDIRECT,
        ("test_fields_check_and_list",),
        why="the CLI prints the mapping, not the raw field list",
    ),
    Item(
        "L26",
        "#67",
        "Normalization: the parent's hierarchyLevel, link ends, a field not on "
        "the screen omitted, sprint value fields, epic link, attachment fields, a summary-only "
        "fields request",
        INDIRECT,
        ("test_get_default_shape", "test_get_fields_summary_only"),
        why="the run records the incomplete list and the shape of the normalized ticket",
    ),
    Item(
        "L27",
        "#69, #18",
        "GET /issue/{key} honors fields; the comment endpoint honors "
        "orderBy and maxResults; expand=properties embeds comment properties; total counts "
        "every comment",
        INDIRECT,
        ("test_get_default_shape", "test_comments_shape"),
        why="the CLI hides which parameters the provider honored: only the printed list, its order, the own-comment properties and the total are observed",
        needs="a ticket with comments (TICKETCTL_LIVE_TICKET)",
    ),
    Item(
        "L28",
        "#18",
        "expand=transitions.fields gives required fields with name, schema.type and "
        "allowedValues; hasScreen is present on every transition",
        PROBED,
        ("test_transitions_shape",),
    ),
    Item(
        "L29",
        "#18",
        "The agile shapes (values, isLast, originBoardId, startDate, endDate, board "
        "type and name); the sprint endpoint accepts a single state; board paging",
        PROBED,
        ("test_sprints_list_and_show",),
        needs="TICKETCTL_LIVE_BOARD",
    ),
    Item(
        "L30",
        "#18",
        "A 404 on /board/{id}/sprint means a missing board; a 400 means a board without sprints",
        PROBED,
        ("test_board_errors",),
        needs="TICKETCTL_LIVE_KANBAN_BOARD for the 400",
    ),
    Item(
        "L31",
        "#39",
        "Whether the platform reads work on macOS (paths, files, prompt)",
        HUMAN,
        why="the macOS checklist (#39): another machine",
    ),
    Item(
        "L32",
        "#73, #52",
        "export --to and the input-file reader on macOS and APFS, a real "
        "separate writer process, a real full disk",
        HUMAN,
        why="another machine and a manual setup (#39)",
    ),
    Item(
        "W01",
        "#3, #31, #84",
        "The label index lag after a create with the label in the body "
        "(the dedupe lag guard is a bound until measured)",
        PROBED,
        ("test_create_with_dedupe_key_and_the_label_lag",),
    ),
    Item(
        "W02",
        "#3, #31",
        "An explicit unassigned value in a create body leaves the ticket unassigned",
        PROBED,
        ("test_create_explicit_unassigned",),
        needs="a ticket cap of 2 or 3 (TICKETCTL_LIVE_MAX_TICKETS) only when the site refuses the "
        "unassigned value: the retry is a second create, and with a cap of 1 the run skips instead",
    ),
    Item(
        "W03",
        "#31",
        "createmeta shapes (issueTypes or values, fields as list or object, "
        "paging) and the POST /issue response carrying a usable key",
        INDIRECT,
        ("test_create_with_dedupe_key_and_the_label_lag",),
        why="a create that works proves the shapes are accepted, not which one the site sent; "
        "at a cap of 1 after a W02 refusal the create is not retried and this shape finding stays "
        "unobserved",
    ),
    Item(
        "W04",
        "#31",
        "A site that refuses labels on create answers 400 with a labels field error",
        HUMAN,
        why="needs a project that refuses labels: a site setting the suite cannot change",
    ),
    Item(
        "W05",
        "#31, #3",
        "The parent rejection shape (Sub-task without a parent, Task under a "
        "Task: 400 with errors.parent)",
        HUMAN,
        why="already verified by spike D (V-29); a live run would have to create a failing "
        "ticket: left to the human if wanted",
    ),
    Item(
        "W06",
        "#3, #23",
        "The order of add and remove within one update (labels)",
        INDIRECT,
        ("test_update_title_labels_order",),
        why="one update adds one label and removes another and the result is read back; the same label in both is refused by ticketctl (exit 22), so the add-versus-remove order for one label is not probed",
    ),
    Item(
        "W07",
        "#66, #23",
        "The unchanged check by the body hash of the current ADF may produce a "
        "spurious update (the provider adds localId and the like)",
        PROBED,
        ("test_update_description_is_unchanged_on_rerun",),
    ),
    Item(
        "W08",
        "#66",
        "Payload shapes (option, priority, user, null clears), the datetime format, "
        "ADF accepted in description and the transition comment, editmeta and createmeta keyed "
        "by field id",
        INDIRECT,
        ("test_update_title_labels_order", "test_transition_with_assign"),
        why="only the fields the suite sets are exercised; the rest needs site-specific fields",
    ),
    Item(
        "W09",
        "#23, #72",
        "PUT on the assignee, POST on transitions, POST on comment: request "
        "and response shapes accepted; the 4xx body shapes",
        INDIRECT,
        ("test_transition_with_assign", "test_comment_with_key"),
        why="the write succeeding proves the shape; a refusal needs a deliberately bad write",
    ),
    Item(
        "W10",
        "#33, #63, #3",
        "A keyed comment: ticketctl.meta accepted as a property in the "
        "comment create request; the comment list is read-your-writes; expand=properties "
        "returns the own comment's property",
        PROBED,
        ("test_comment_with_key",),
    ),
    Item(
        "W11",
        "#63, #3",
        "Comment property size and key limits, the text limit of descriptions "
        "and rich-text custom fields, an empty text node, a total text limit across ADF nodes, "
        "a 300 KB body",
        HUMAN,
        why="needs oversized writes; the live suite never deletes a comment, so this is a human decision "
        "(a description on a test ticket is the safer place)",
    ),
    Item(
        "W12",
        "#34",
        "Adding a ticket that is already in an open or future sprint to a second "
        "one moves it (plan assumes a move, warning sprint_moved) rather than appending",
        PROBED,
        ("test_sprint_add_move_and_remove",),
        needs="a board with two open or future sprints",
    ),
    Item(
        "W13",
        "#34",
        "sprint remove sends the ticket to the backlog: with several open sprints",
        PROBED,
        ("test_sprint_add_move_and_remove",),
        needs="a board with two open or future sprints",
    ),
    Item(
        "W14",
        "#3, #34",
        "Closed-sprint membership under the backlog call, and moving a ticket "
        "out of a closed sprint",
        HUMAN,
        why="needs a ticket whose only sprint entries are closed; a sprint cannot be closed "
        "and reopened by the suite: a human sets one up and runs sprint remove on it",
    ),
    Item(
        "W15",
        "#34",
        "What a 400 and a 404 mean on the sprint add and backlog calls",
        PROBED,
        ("test_sprint_errors",),
        needs="a board",
    ),
    Item(
        "W16",
        "#34",
        "sprint add without --board on a site with several boards is ambiguous_board",
        HUMAN,
        why="the user's config usually names a board; the human runs a dry-run sprint add "
        "against a config with none",
    ),
    Item(
        "W17",
        "#26, #72",
        "What Jira does after a 5xx, a reset or a drop on a write; whether a "
        "write 429 carries Retry-After and never applies; a 2xx with an unusable body",
        HUMAN,
        why="faults a real site cannot be made to produce on demand",
    ),
    Item(
        "W18",
        "#10",
        "The 400 body of an unknown transition id and of a missing resolution, the "
        "ADF-required 400 for a plain string, the label, summary, parent and notifyUsers texts",
        HUMAN,
        why="ticketctl validates these first, so the provider is never asked: needs raw requests",
    ),
    Item(
        "W19",
        "#3",
        "Whether a done transition from a non-assigned sub-task needs a parent; "
        "the transition order (assign then done)",
        INDIRECT,
        ("test_transition_with_assign",),
        why="the suite closes tickets with the normal flow; a sub-task needs a parent ticket",
    ),
    Item(
        "W20",
        "#38",
        "The closer: every created ticket ends done with the won't-do resolution",
        PROBED,
        ("test_every_created_ticket_is_closed",),
    ),
    Item(
        "W21",
        "#43",
        "comment-edit and comment-delete: the single-comment read embeds properties under "
        "expand=properties; PUT of a comment accepts body and properties and rewrites the "
        "ticketctl.meta property; PUT without visibility on a restricted comment, and whether an "
        "internal-only comment reads as jsdPublic false (ticketctl refuses both, so only a raw "
        "request shows what the provider does); DELETE removes the "
        "comment; a non-author PUT or DELETE is refused",
        HUMAN,
        why="the write suite posts exactly one comment per test ticket and a delete would break "
        "that count: a human runs comment-edit and comment-delete on a comment of their own "
        "test ticket and reads the result with comments",
    ),
    Item(
        "W22",
        "#41",
        "link add and link remove: inwardIssue of POST issueLink is the ticket that gets the "
        "outward phrase (inwardIssue blocks outwardIssue), so --direction outward reads back as "
        "outward on the ticket; the create answers 201 with an empty body; each ticket's "
        "issuelinks carries the link with the same id; DELETE issueLink/{id} answers 204; a "
        "duplicate link, and tickets that may not be linked, are refused or accepted as the "
        "docs say",
        HUMAN,
        why="the write suite creates no links and cannot undo one on a ticket it did not make: a "
        "human runs link add with --direction outward between two of their own test tickets, "
        "reads both with get --fields links, then runs link remove",
    ),
    Item(
        "W23",
        "#42",
        "attachment-add and attachment-download: POST /issue/{key}/attachments with one `file` "
        "part and X-Atlassian-Token: no-check is accepted and answers a list with the new "
        "attachment (id, filename, size); a name with a non-ASCII character is stored as sent; a "
        "second upload of the same name is kept as a second attachment (the content check "
        "relies on that and on the issue read listing the first one at once); the issue read "
        "lists `attachment` with id, filename, size and mimeType; GET "
        "/attachment/content/{id}?redirect=false answers the bytes themselves, with no redirect, "
        "and a length equal to the listed size; the provider's own size limit and the answer "
        "when it is exceeded (a 4xx ticketctl maps to `rejected`)",
        HUMAN,
        why="an upload cannot be deleted by ticketctl and the write suite creates no "
        "attachments: a human runs attachment-add twice on a test ticket (the second run must be "
        "unchanged), then attachment-download of the printed id and compares the bytes, and "
        "tries a file just over the site's limit if it is below 10 MiB",
    ),
    Item(
        "W24",
        "#48",
        "resolution-backfill: the edit screen (editmeta) of a done ticket that has no resolution "
        "lists `resolution` with the `set` operation (when it does not, ticketctl refuses with "
        "not_editable and the site's screen or workflow has to change); the shape of its "
        "`allowedValues` (entries with `name`); PUT /issue/{key} with fields.resolution "
        "{name} answers 204 and the value is stored and readable on a done ticket; resolutiondate "
        "follows or stays; the status does not change; a workflow with a post function that "
        "clears or requires the resolution on that status refuses or overwrites the value (a "
        "4xx ticketctl maps to `rejected`, or a value that reads back empty)",
        HUMAN,
        why="the write suite closes its tickets through the transition with a resolution and "
        "never leaves a done ticket without one: a human closes one test ticket in the site's "
        "workflow without a resolution (or finds one), runs resolution-backfill --dry-run, "
        "applies it with --yes --expect-plan, reads the ticket with get, and runs it again "
        "(the second run must be unchanged)",
    ),
)


def by_id() -> dict[str, Item]:
    return {i.id: i for i in ITEMS}


def render_table() -> str:
    """The markdown table of the runbook, one row per item (the doc embeds exactly this text)."""
    rows = ["| Id | Source | Fact | Probe | Needs |", "|---|---|---|---|---|"]
    for i in ITEMS:
        if i.kind == HUMAN:
            probe = "needs a human decision or another environment: " + i.why
            if i.probes:
                probe += " (passive: `" + "`, `".join(i.probes) + "`)"
        else:
            probe = "`" + "`, `".join(i.probes) + "`"
            if i.kind == INDIRECT:
                probe += " (indirect: " + i.why + ")"
        rows.append(f"| {i.id} | {i.source} | {i.fact} | {probe} | {i.needs or 'none'} |")
    return "\n".join(rows)
