# Agent automation protocol

`local-agent-record-janitor agent ...` is the stable non-interactive surface
for an orchestrating Agent. Human and Agent commands share the same
`CleanupService`; neither command path owns a second scanner or planner.

## Commands

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

The plan contains structured identities, exact scopes, counts, fingerprints,
and blockers, but no chat message bodies. Its SHA-256 is calculated over the
canonical UTF-8 JSON object without the top-level `plan_sha256` field.

One plan authorizes one physical storage and one mutation family. Native
thread deletion, legacy-index cleanup, Desktop-state cleanup, exact relation
cleanup, and exact frontend-reference cleanup are separate batches. Completing
one batch never authorizes actions discovered by the next scan.

`agent apply` requires the exact plan, exact hash, and
`--clients-closed`. It performs:

1. one complete preflight scan;
2. action-local guards for only the approved rows, files, references, and
   relationship scope;
3. one complete final scan.

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

If `goal_status=unknown`, never retry apply. Run status and verify. If verify
reports residuals, inspect them and create a fresh plan; never edit an old plan
or widen its action list.
