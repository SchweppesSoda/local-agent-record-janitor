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

The `native` client also supports exact stale local-environment registrations
through the independent `delete_native_project` family. See
[the supported schema and recovery contract](native-project-cleanup.md).

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

Cindy Codex plans include ordinary native conversations from the same
client-qualified inventory used by `records`, together with their frozen
frontend reference closure. Project selection carries the native project's
metadata to the paired reference actions. When selected roots include both a
parent and its descendants, the plan retains one encompassing native cascade
and its complete reference closure; it does not send overlapping deletion
requests. Descendant references remain bound to their own native IDs. A
child-only selection never implicitly selects its parent and remains subject
to the existing lineage guards.

If a verified native deletion leaves Cindy references, a fresh plan can clear
those exact references only when a complete inventory proves the native index,
rollouts, legacy index and descendants absent. Incomplete catalogs and unproven
row ownership remain blocked. Reappearing native records invalidate the plan.
Reference actions remove SDK references only. Deleting retained chat rows requires
explicit Cindy session IDs as described below.
Missing-parent subagents use the existing native orphan evidence and guards for
the same Cindy store rather than a synthetic manual-delete finding.

Cindy frontend references are checked during planning against the supported
upstream FTS trigger definitions and index layouts. Unknown triggers return a
`frontend_preflight_blocked` blocker before any native deletion is attempted.
See [Cindy storage contract](cindy-storage-contract.md) for pinned upstream
sources, the closed-client CJK fallback, and regression fixtures.

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
project/all-projects cleanup selects only `sessions.status='deleted'` rows.
An explicit `--record-id <Cindy-session-id>` selection also supports `active` and
`archived` rows when the user requests their permanent deletion. This selection
is frozen in the operation scope and in each row's `explicitly_selected` evidence;
a native SDK thread ID does not authorize deleting its Cindy chat row.
All approved IDs in one Cindy database are guarded as a set and deleted in one
transaction together with their supported message, FTS, embedding/vector, and
session-owned dependency rows. Status changes invalidate the frozen evidence.
Unselected retained rows and unproven schemas remain protected.

## Official Desktop inventory and completion

Select the official native `CODEX_HOME` explicitly when the caller itself runs
inside Cindy. `records --inspect-clients` adds read-only process ownership
evidence (PID, parent PID, executable and store relation); process names alone
do not identify which store is open. Apply and verify restore the frozen native
store when a plan is supplied and reject a conflicting explicit home.

Record output contains the Desktop catalog's UI display title when available,
its source, the full stable record ID, parent/descendant IDs and a snapshot ID.
Project and record filters restrict both displayed rows and grouped counts.
Temporary labels such as M14 belong to the inventory that produced them; resolve
them to full IDs before generating an immutable plan. Titles containing known
automatic-review history wrappers are displayed as `自动审查记录`; other long
display strings are bounded without changing identity fingerprints.

The lineage graph combines native spawn edges, structured source metadata and
top-level `parent_thread_id`/`thread_source` fields. Conflicting parents, roles
and cycles block deletion. An explicitly selected guardian whose parent is
absent can be deleted when its sole rollout matches its native index, the index
independently marks it as a subagent, and the rollout proves one top-level
parent. This exception does not enter automatic orphan cleanup and does not
allow an orphan with remaining descendants.

`delete apply` executes one immutable operation. Native `delete run` may create
fresh plans for supported Desktop residuals within the original frozen record
set, up to three rounds. It never resends an unknown native delete. Each round
has a new operation ID/hash; the run receipt retains every round and verifies
the entire original scope. Partial native results remain residuals, and exact
global-state references count even when the Desktop catalog row is gone.
An empty scan proves completion only for the exact successfully scanned store;
frontend databases additionally require the matching discovery family.

For a full explicit native thread ID, planning also probes exact JSON UI
references when both the native record and Desktop catalog row are absent.
These use `remove_desktop_state` with zero catalog rows and the same frozen
state fingerprints, closed-client guard, rollback and verification. A compatible
Desktop database is still required. Prefix, project and all-projects selectors
do not discover these catalog-free references; verification checks every frozen
descendant ID even if ordinary `records` output no longer lists it.

## Legacy commands

The older `agent doctor/plan/apply/status/verify` commands remain available
for integrations that need one exact `CODEX_HOME` and one mutation family.
They retain their one-store plan hash and explicit client-closed acknowledgement
requirements. They are not a fallback for `delete plan/apply/run`. If the
installed `OperationCoordinator` is unavailable, the new command returns
`operation_api_unavailable` and performs no write.
