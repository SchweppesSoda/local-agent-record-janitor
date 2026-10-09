# Cindy storage contract

This implementation is checked against [makecindy/cindy at
8b512ba5396f83a242838d172dbd7afb19183265](https://github.com/makecindy/cindy/tree/8b512ba5396f83a242838d172dbd7afb19183265).
It does not infer compatibility from the installed application version alone.

## Storage and selection

Cindy's frontend SQLite database, dedicated `codex-home`, `pi-agent-home`, and
Claude store remain separate physical stores. The official Codex home is never
substituted. The native catalog supplies project and parent/descendant evidence
to both inventory and planning, including records without frontend references.
Existing Windows normal/extended path aliases match only after the shared path
identity helper proves they refer to the same object. A full path is not a prefix
selector; unique display-name prefixes remain a convenience.

Native parent/descendant deletions are coalesced into non-overlapping frozen
cascades. Frontend references are released only for their own positively verified
native outcome. An unknown native result still requires status/verify and never
permits a repeated request. A failure to capture the native scope before any
request is classified as blocked by the operation API, with the actual metadata
error propagated to its result and child checkpoint. The legacy verifier retains
its `unknown` classification for an unprovable scope; `preflight_blocked=true`
distinguishes that from an ambiguous mutation.

## Frontend schema proof

The [upstream schema](https://github.com/makecindy/cindy/blob/8b512ba5396f83a242838d172dbd7afb19183265/apps/desktop/src/main/localDb/schema.ts)
uses a standalone FTS5 index and, in newer migrations, stable message-to-FTS row
IDs. `cindy_schema.py` owns the supported complete trigger definitions and index
shape checks shared by reference cleanup and session cleanup.

Supported persistent trigger generations:

| Migration | Index behavior |
| --- | --- |
| [0017](https://github.com/makecindy/cindy/blob/8b512ba5396f83a242838d172dbd7afb19183265/apps/desktop/drizzle/0017_add_messages_fts.sql) | Index non-rewound messages by message ID |
| [0066](https://github.com/makecindy/cindy/blob/8b512ba5396f83a242838d172dbd7afb19183265/apps/desktop/drizzle/scripts/0066_slim_messages_fts.ts) | Restrict indexed message roles |
| [0095](https://github.com/makecindy/cindy/blob/8b512ba5396f83a242838d172dbd7afb19183265/apps/desktop/drizzle/scripts/0095_scope_messages_fts_update_trigger.ts) | Restrict update-trigger execution |
| [0096](https://github.com/makecindy/cindy/blob/8b512ba5396f83a242838d172dbd7afb19183265/apps/desktop/drizzle/scripts/0096_stabilize_messages_fts_rows.ts) | Stable `messages_fts_rows` mapping |
| [0100](https://github.com/makecindy/cindy/blob/8b512ba5396f83a242838d172dbd7afb19183265/apps/desktop/drizzle/scripts/0100_segment_messages_fts_cjk.ts) | Persistent fallback for connections without Cindy's temporary CJK triggers |

The CJK tokenizer and temporary triggers belong to Cindy's connection. Janitor
does not install or simulate them. The closed-client operation uses the upstream
persistent fallback, changes only `agent_switch` reference metadata, and deletes
selected rows. Unrelated indexed message content is preserved.

The optional [0034 rewind trigger](https://github.com/makecindy/cindy/blob/8b512ba5396f83a242838d172dbd7afb19183265/apps/desktop/drizzle/0034_add_chat_embedding_vec.sql)
is also matched by its complete definition. It only runs on `UPDATE OF rewind_at`,
which neither content-reference cleanup nor session deletion performs. Its
presence does not require activating vector cleanup during reference updates.

Whole-session deletion also supports migration `0122_auto_review_projections`
as shipped in Cindy 0.1.99. Its installed SQL and script excerpts, with source
hashes, are pinned in `tests/fixtures/cindy/auto_review_0122.sql`. All four
auto-review triggers and the complete projection-table definition must match;
additional triggers on that table remain blocked. Message deletion invalidates
only projections whose session or lead is selected, and both foreign keys cascade
when those sessions are deleted. The plan fingerprints the affected cache rows,
checks them again before writing, and verifies both reference columns are empty
after deletion. Unrelated projections remain unchanged. Reference-only cleanup
does not yet enable this trigger generation.

On Windows, session cleanup discovers Cindy's bundled SQLite vector extension
under the per-user installation or the standard Program Files installation roots.
An explicit extension path takes precedence; missing or non-file candidates do
not bypass the schema checks.

Trigger names alone are insufficient: complete normalized SQL fingerprints must
match one supported generation. Unknown triggers, altered index layouts, or
triggers on the index dependencies block deletion. Reference plans run a set
guard once per database before returning ready. Apply repeats its exact guards
under the write transaction. Each business statement must affect exactly one
approved row; FTS shadow writes do not count as extra business rows.

Whole-session deletion still requires `status=deleted` or the exact explicit
frontend session ID authorization described in [operation-cli.md](operation-cli.md).
Project/all-projects selection does not silently add retained frontend rows.
Session deletion validates its trigger contract while freezing evidence and
again before execution. A guard failure before mutation does not restore a
backup over an unchanged database.

## Regression evidence

`tests/fixtures/cindy/fts_*.sql` contains attributed SQL excerpts from the pinned
upstream migrations. Tests instantiate temporary SQLite databases, exercise
history-reference removal and complete session deletion, and check neighboring
sessions, messages, search results and stable FTS rows. Modified same-name
triggers and index dependency triggers must fail before the first write.

Additional operation tests cover normal/extended Windows paths, project records
without frontend rows, visible subagent lineage, frontend preflight blockers,
native startup drift, and mixed native outcomes. Live cleanup is never a test.
