# Agent automation protocol

This reference covers legacy `local-agent-record-janitor agent ...` integrations
and shared result/recovery semantics. Human and Agent commands share the same
`CleanupService`; neither command path owns a second scanner or planner.

For the preferred `records`/`delete`/`operation` interface, use
[Operation CLI contract](operation-cli.md). Select required reading through the
[operation contract](agent-operation-contract.md#read-for-the-selected-task).

## Commands

These commands target one physical store and one mutation family. Native
local-environment registrations require the high-level
[native project cleanup](native-project-cleanup.md) path.

`agent doctor` is read-only. It checks the exact target store, scan
completeness, and client ownership. A process using another physical store does
not block this store when executable, parent/child process, and filesystem
identity evidence prove the separation. Unknown ownership fails closed.

`agent plan --operation purge` is also read-only with respect to the target
store. `--out PLAN.json` is optional:

- an explicit path is caller-managed and is never overwritten;
- without `--out`, the plan is written below the user's platform state
  directory, never the project root;
- the command returns the exact `plan_path` and `plan_sha256`.

Legacy agent v1 does not serialize new Orca protection sources. When planning
or applying an unattempted mutation with such a known source, it returns
`missing_guard_source_evidence`; use [the high-level operation
plan](operation-cli.md) instead. Existing v1 documents are never enriched or
rehashed at runtime. Started/ambiguous operations are diagnosed through the
original status/verify contract, and no new guard or schema condition permits
resending an unknown mutation. Metadata readers use read-only SQL; SQLite WAL
read locks may still create or update SQLite-owned side files, which are never
cleaned up by the inventory command.

The qualified Orca Windows native combination uses top-level v3 and child v2.
Legacy `agent`/direct/manual/GUI calls lack its exact frozen target/runtime proof
and remain blocked. Startup is a durable mutation boundary with root-wide
coordination; read-only recovery requires the original named Job's absence on
the same machine and Windows session. It reads frozen native artifacts directly
even when today's capability is closed. See [the precise operation
contract](operation-cli.md) and [admission limits](adapters.md#orca精确原生删除的限定组合).

For one native store, repeat `--thread-id ID_OR_UNIQUE_PREFIX` to freeze only
actions whose root thread identity matches every supplied selector. Any missing
or ambiguous selector blocks the plan instead of falling back to the unscoped
purge batch. The selected actions must still form one physical-store mutation
family; later families require a fresh plan.

The plan contains structured identities, exact scopes, counts, fingerprints,
and blockers, but no chat message bodies. Its SHA-256 is calculated over the
canonical UTF-8 JSON object without the top-level `plan_sha256` field.

One plan authorizes one physical storage and one mutation family. Native
thread deletion, legacy-index cleanup, Desktop-state cleanup, exact relation
cleanup, and exact frontend-reference cleanup are separate batches. Completing
one batch never authorizes actions discovered by the next scan.

`agent apply` requires the exact plan, exact hash, and
`--clients-closed`. Where the selected path supports complete catalog
verification, it performs:

1. one complete preflight scan;
2. action-local guards for only the approved rows, files, references, and
   relationship scope;
3. one complete final scan.

The no-action-loop-full-rebuild rule applies to all supported paths. The
default planner uses one catalog pass per store; healthy/native records have
the measured two-pass plan-plus-terminal result. Anomaly sources such as
stale-index or broken-relation detection may perform source-specific reads,
so the two-pass figure is not a universal promise.

For a Codex deletion batch, one app-server handles all approved actions.
Per-action guards do not repeat a complete store scan. Any drift stops the
remaining actions instead of silently widening the old plan.

An empty plan is only a snapshot. Apply still re-scans the full target before
it may return `complete`; a new problem blocks the old plan and an incomplete
scan returns `unknown`.

Before a modifier can run, apply durably persists
`mutation_started=true` and an in-flight checkpoint. If the process stops in
the ambiguous window, another apply must not resend the mutation.

`agent status` is read-only and reports trusted persisted evidence.
`agent verify` never calls a deletion or repair API; it verifies the frozen
targets and then the complete target. The default total verification budget is
180 seconds with bounded backoff. Conclusive absence or residual evidence
returns immediately.

## Operation evidence and receipts

While an apply is executing, or while its result is unknown, the target store
may contain:

```text
<CODEX_HOME>/.local-agent-record-janitor/operations/<operation-id>/
├── plan.json
├── events.jsonl
├── state.json
├── result.json       # when a result document was reached
└── apply.lock        # only while a process owns the mutation gate
```

Every document is bound to the operation ID and plan hash. Sequence numbers are
strictly increasing, durable writes are flushed before mutation, and symlinks,
junctions, reparse points, non-regular leaves, and reused operation IDs fail
closed. A lock left after a crash is not deleted or bypassed.

The parent `operations/.mutation.lock` is a permanent OS lock file shared by
cooperating processes. Root locks are acquired in a fixed physical-root order,
before per-operation `apply.lock`; they cover admission, durable checkpoints,
writers, verification facts and terminal publication. Result/receipt readers
also hold the root lock when finishing compaction or expiring a receipt, so
status cannot delete a newer journal that reuses an expired operation ID.
`mutation_root_locked` means the root is busy; retry status after the active
operation finishes. Neither lock file is automatically replaced or removed
to recover from an unknown mutation.

Before dispatch, all trusted legacy and child journals under that root are
checked for overlapping frozen IDs and lexical artifact paths. A new operation
ID or plan path cannot evade `store_mutation_outcome_unknown`. Missing state,
malformed evidence or an unproved action footprint blocks the affected root.
A remaining old `apply.lock` stays unknown even beside a complete receipt;
the new OS mutex cannot prove an older writer has stopped.

When the result is known—`complete`, `blocked`, or
`completed_with_residuals`—the detailed plan, events, state, and result are
replaced by one compact, body-free receipt:

```text
<operation-id>/
└── receipt.json
```

The receipt stores only the operation/plan binding, action statuses, compact
blocker codes, counts, verification booleans, and timestamps. It is not a
backup and cannot restore deleted data. `status` and `verify` can use it to
resolve a caller timeout without repeating a mutation. It expires after at
most seven days; expiry cleanup removes only a trusted directory containing
that one receipt.

An `unknown` result keeps the detailed evidence because it is still needed to
verify the outcome. It is not compacted until verification reaches a known
terminal state.
Unknown journals have no TTL. Conclusive verification of a known partial result
allows a new plan for the actual residuals. A child blocked before any mutation
can resume once its blocker is resolved, preserving its original authorization.

The shared gate also protects direct CLI/GUI and service entry points against
existing journaled unknown outcomes. Those direct calls do not acquire a new
durable operation journal. Cross-process timeout recovery applies to the
journaled operation and legacy agent surfaces, within their trusted local
roots; different roots, unproven file bridges and older non-cooperating writers
are outside that guarantee. After mutation may have begun, a lock-exit or
publication failure returns one `unknown` JSON result and preserves any
already established modification and mutation-started facts.

## Temporary rollback copies

Codex/Pi/Claude records and standalone rollout files are permanently deleted
without backup. A shared SQLite database, legacy index, or Desktop JSON file
uses a temporary rollback copy so an exact write cannot damage neighboring
records.

- successful write and verification: delete the temporary copy immediately;
- successful automatic rollback: delete the temporary copy immediately;
- partial/unknown result or failed rollback: retain it temporarily and report
  its exact path until verification resolves the operation.

No public recovery command or long-term backup repository is part of the Agent
protocol.

## Result contract

Legacy `agent` subcommands emit JSON only and never read stdin. Their exit codes
are `0` for read-only success or verified completion, `1` for unknown/untrusted
results, and `3` for blocked goals or residuals. Exit code `2` is reserved for
human confirmation flows and is never an agent result.

Automation branches on structured fields, never translated prose:

- `goal_status`: `complete`, `completed_with_residuals`, `blocked`, or
  `unknown`;
- `goal_satisfied`: true only for `complete`;
- `modified`: true only after verification confirms a planned change;
- `mutation_started`: true once a modifier may have been called;
- `blockers[].blocker_code`: stable decision code;
- `counts`: structured finding/action/residual counts.

Authorization uses `cleanup_blocker_codes`, not human text. Missing, unknown,
or malformed codes fail closed.

`complete` applies to the frozen paths, rows, manifest members and approved
references. Existing Pi/Claude v1 writers do not prove that undiscovered aliases,
external copies or records in another logical store are absent. The independent
`records --client ...` file-alias and process-owner projections are observations;
they do not change the inventory snapshot ID, plan hash or writer authorization.
Successful process enumeration is not complete runtime coverage: an unsupported
owner, engine or platform remains unknown even when no named process is found.

If `goal_status=unknown`, never retry apply. Run status and verify. If verify
reports residuals, inspect them and create a fresh plan; never edit an old plan
or widen its action list.
