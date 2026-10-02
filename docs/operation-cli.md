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
records --client <native|codex-native|cindy|aionui|pi|claude|orca|herdr> [--project <selector> ...]
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

Orca is inventory-only. `records --client orca [--orca-root PATH ...]` reads the
supported local journal and proven native homes. Selecting Orca for delete
returns structured `blocked`/capability-unavailable evidence, including when
the plan has no executable actions. Status and verify preserve that blocked
result; an empty unauthorized action set is not successful cleanup.

Plans that discover Orca protection profiles use `larj.operation-plan.v2`.
The required `guard_sources` list contains each canonical local profile root,
client, host and path namespace, and is included in the approval hash. These
locators are separate from mutation storages and never create a journal or lock
under an Orca profile. Apply, residual run rounds and verify restore frozen
profiles even when the caller omits or changes `--orca-root`. They also retain
current default/env and explicitly supplied protections; those guards do not
expand the selected catalog or approved targets. A required source that fails
to read cannot become an empty guard set. Store-qualified failures affect the
matching approved store; unscoped coverage failure of a required profile blocks
dispatch. Only bounded product metadata is refreshed, without another native
planner pass.

Existing v1 plans keep their original fields, bytes and hash. If an unattempted
v1 operation now requires these new protection locators, apply returns
`missing_guard_source_evidence` and requires a new top-level plan. Started or
ambiguous child journals remain `unknown` before any such gate; their native
status/verify diagnosis remains available without resending mutation. A new
adapter's `verify=false` does not revoke that legacy read-only diagnosis.

Herdr is persisted-only and inventory-only. `records --client herdr
[--herdr-root PATH ...]` reads snapshot schema3 current and recognized recovery
files across default/named sessions. Explicit roots replace default candidate
profiles. Rootless IDs and Pi paths do not prove native ownership or join a
native catalog; project cwd is not a native root. Even valid persisted sources
retain `live_metadata_not_probed`: records returns exit code `1` and
`goal_status=blocked` while exposing the available references. Source errors
remain scoped to the selected profile/source. Herdr does not add Orca
`guard_sources`; all native/frontend/remote mutation and its own verify
capabilities are false. Delete plan/run returns structured capability blockers;
apply/status/verify preserve a blocked plan with no authorized actions instead
of interpreting the empty action set as successful cleanup. See
[the source and coverage limits](adapters.md#herdr已接入持久化只读引用).

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
unverified
```

Codex child records do not need their own frontend reference to be `healthy`.
The complete catalog resolves their parent chain within the same native store
before project or record filtering. Each ancestor must have a native index row
or a verifiable rollout; a frontend reference or graph-only placeholder is not
proof that the parent exists. In third-party client scope, unreferenced roots retain `orphan_native`, as do
children whose immediate native parent is confirmed missing. Unknown or
conflicting parent chains are `broken_relation`, not confirmed orphans.
Normal standalone Codex/Pi/Claude records are `healthy` without frontend
references. Unknown backends or unverified native ownership are `unverified`.
Unknown backends remain inventory-only; unverified native roots cannot grant a
native writer, even when a separate exact frontend writer is supported.
Consumers must tolerate additional inventory
classifications and must never treat a classification as write authorization.

Record metadata includes `is_subagent`, `parent_thread_ids`,
`descendant_thread_ids`, and `lineage_status` (`root`, `known`, `missing_parent`,
`unknown`, or `conflict`). Parent references are not copied into a child's
`frontend_reference_ids`. Normal manual-record plans use the same classification;
explicit anomaly findings retain their anomaly classification. Classification
is inventory metadata, not deletion authorization: exact scope, descendant
closure, parent protection, and frozen-plan checks still apply.

`frontend_reference_ids` retains compatibility display IDs;
`frontend_binding_keys` qualifies each binding by client, database, frontend ID,
engine, native target, reference kind and historical boundary. Matching, grouping
and record selection use these qualified bindings. Native catalog errors and
per-record blockers remain visible through the client projection. Every selected
profile participates in initial inventory and revalidation. Cindy's default
inventory also probes supported native engines with no remaining frontend rows.

Pi `parentSession` is fork provenance, not a recursive deletion rule. Claude
subagent files are members of a session manifest. Neither relationship inherits
Codex's descendant deletion semantics. See [adapter contracts](adapters.md).

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

Cindy frontend-session deletion defaults to `sessions.status='deleted'` rows.
The narrow explicit-selection exception is an `active` row whose
`sdk_session_id` is SQL NULL: a full frontend session ID supplied through
`--record-id` may freeze it with `explicit_unbound_active=true`. Project-wide
and all-project scans do not opt into this exception. Apply and recovery bind
the exact status, row, dependencies and NULL native binding; a new binding or
status change blocks deletion.
All approved IDs in one Cindy database are guarded as a set and deleted in one
transaction together with their supported message, FTS, embedding/vector, and
session-owned dependency rows. Other `active` rows, `archived`, and unproven schemas are
inventory/protection evidence, not deletion candidates.

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

## Legacy commands

The older `agent doctor/plan/apply/status/verify` commands remain available
for integrations that need one exact `CODEX_HOME` and one mutation family.
They retain their one-store plan hash and explicit client-closed acknowledgement
requirements. They are not a fallback for `delete plan/apply/run`. If the
installed `OperationCoordinator` is unavailable, the new command returns
`operation_api_unavailable` and performs no write.

Legacy agent v1 plans cannot freeze Orca protection locators. New plans and
unattempted applies that discover them return `missing_guard_source_evidence`
and direct callers to the high-level v2 surface. Existing agent status/verify
remain read-only, and unknown apply is still refused before scanning guards.
