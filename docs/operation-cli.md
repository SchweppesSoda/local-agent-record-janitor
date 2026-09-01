# Operation CLI contract

The operation CLI is the multi-project, body-free interface for local record
cleanup. It is a thin dispatcher: scanning, immutable planning, child-batch
partitioning, journal recovery, mutation, and verification belong to the
`OperationCoordinator`, which delegates storage work to `CleanupService`. The
CLI does not maintain a second scanner or mutation implementation. The
implemented deletion paths currently cover healthy/native records with a
frozen frontend closure, Cindy Pi/Claude sessions, and Cindy frontend sessions
whose exact row status is `deleted`. AionUI orphan
project/conversations rows are executable only for the probed supported schema
with immutable row evidence and zero `acp_session` references; other schemas
remain `inventory_only`.

## Commands

Use one client per operation. A plan/run selects exactly one scope mode:

```text
records --client <native|codex-native|cindy|aionui|pi|claude> [--project <selector> ...]
delete plan --client <client> (--project <selector> ... | --all-projects | --record-id <id> ...)
delete apply --operation-id <id> [--plan <plan.json>] [--clients-closed]
delete run --client <client> (--project <selector> ... | --all-projects | --record-id <id> ...)
operation status --operation-id <id> [--operation-home <path>] [--plan <plan.json>]
operation verify --operation-id <id> [--operation-home <path>] [--plan <plan.json>] [--verify-timeout <seconds>]
```

`--engine` may further limit a client scope. A project selector is resolved by
the core using a normalized working directory, authoritative project ID, or a
unique project name. Same-name projects at different paths remain ambiguous;
records without project evidence are not absorbed by name.

When `delete plan` receives `--out PATH`, pass that exact file as `delete apply
--plan PATH` and as `operation status/verify --plan PATH`. The plan path is
optional only when the coordinator can resolve the operation's default state
location by operation ID. `operation_home` and `codex_home` are state-root
selectors for status/verify; they are not plan files and must not be substituted
for `--plan`.

`delete plan` creates one immutable top-level operation. Its child batches are
the write boundary: each child targets one physical store and one mutation
family. The core freezes the target/reference closure supported by the
selected adapters before mutation and uses targeted guards while applying.
Terminal verification is path-dependent; for healthy/native records the
measured contract is one planner catalog pass plus one terminal catalog pass.
The default planner performs one pass per store and the action loop does not
rebuild a complete catalog/plan/frontend. Anomaly adapters can require
source-specific reads, so this two-pass measurement is not a promise for every
classification. A successful `delete run` performs the same plan/apply
sequence without pausing for an interactive review. Within an unchanged
authorized scope, the Agent may proceed from plan to apply without asking for
a second confirmation.

The operation store owns the operation ID and plan hash binding. `apply` must
not infer a changed plan or resend an ambiguous native request. If a mutation
is `unknown`, stop and use `operation status` followed by `operation verify`;
never invoke `delete apply` again for that operation.

## Output

JSON output and human summaries contain metadata only: IDs, stores, paths,
counts, classifications, blockers, and progress grouped by project, engine,
and physical location. Chat messages, prompts, transcripts, and response
content are not read into the report or saved in operation evidence.

The stable classification vocabulary is:

```text
healthy
orphan_native
orphan_frontend
orphan_project
broken_relation
stale_index
partial_remote
corrupt_unreadable
unknown_operation
```

An unsupported AionUI backend is still inventoried and reported as
`inventory_only`/`unsupported`; the operation is not marked deleted. An
unreadable unrelated store is a warning, while an unreadable target or an
unprovable shared reference blocks only the affected child batch. AionUI
orphan project/conversations rows are executable only for the probed supported
schema with immutable row evidence and zero `acp_session` references; other
schemas remain `inventory_only`. When
`remote_delete=false`, residuals are reported only when the adapter provides
authoritative discovery evidence; otherwise the output states the capability
boundary without claiming residuals, and nothing is deleted. Stale-index and broken-relation discovery remains in the
anomaly scan and is not guaranteed to appear as a uniformly executable
`records` target for every client.

Cindy frontend-session deletion is deliberately narrower than "not active":
only `sessions.status='deleted'` rows enter `delete_frontend_session` batches.
All approved IDs in one Cindy database are guarded as a set and deleted in one
transaction together with their supported message, FTS, embedding/vector, and
session-owned dependency rows. `active`, `archived`, and unproven schemas are
inventory/protection evidence, not deletion candidates.

## Compatibility boundary

The older `agent doctor/plan/apply/status/verify` commands remain available
for integrations that need one exact `CODEX_HOME` and one mutation family.
They retain their one-store plan hash and explicit client-closed acknowledgement
requirements. They are not a fallback for `delete plan/apply/run`. If the
installed `OperationCoordinator` is unavailable, the new command returns
`operation_api_unavailable` and performs no write.
