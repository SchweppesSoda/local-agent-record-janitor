# Agent automation protocol

`local-agent-record-janitor agent ...` is the stable, non-interactive surface
for an orchestrating Agent. It separates diagnosis, immutable authorization,
mutation, and verification so a timeout or crashed process cannot silently
turn into a duplicate delete.

## Commands

`agent doctor` is read-only. It checks that the target store is discoverable,
the scan is complete, and target-related clients can be classified. A client
from another `CODEX_HOME` is ignored only when both existing stores are proven
different with filesystem identity checks. Missing process metadata or an
identity error remains blocking.

`agent plan --operation purge --out PLAN.json` is also read-only with respect
to the target store. It never overwrites `PLAN.json`. The plan contains only
structured identities, action scopes, counts, fingerprints, and blockers—not
chat message bodies. The canonical SHA-256 is calculated over the JSON object
without its top-level `plan_sha256` field, using sorted keys and compact UTF-8
JSON.

One plan contains at most one mutation family and one immutable set of root
actions. The order is native conversation deletion, one legacy-index repair,
then Desktop state removal for one store. Completing one plan does not authorize
actions discovered by the next scan.

`agent apply` requires the exact plan path, exact `plan_sha256`, and an explicit
`--clients-closed` acknowledgement. It re-scans the full guarded scope and
requires the cleanup-plan fingerprint plus every selected action ID, target,
kind, affected scope, and snapshot fingerprint to match. It never selects from
newly discovered actions. An empty plan is not a permanent clean certificate:
apply re-scans and performs full-target verification before it may report
`complete`; a newly appeared problem blocks that old plan, and an incomplete
scan reports `unknown`.

Before any modifier is called, apply synchronously persists
`mutation_started=true` and a mutation-in-flight checkpoint. Therefore a crash
in the ambiguous window is recoverable without guessing: later apply calls
refuse to resend and return `unknown`.

`agent status` performs no mutation: it validates the bound plan, operation
state, journal, result, and mutation lock before reporting persisted evidence.
`agent verify` never calls a deletion or repair API; it takes the same operation
lock and checks both the frozen targets and the complete target store. The
default total verification budget is 180 seconds with bounded exponential
backoff; conclusive absent evidence or conclusive residuals return immediately.

## Persistent operation record

After an authorized apply, the target store contains:

```text
<CODEX_HOME>/.local-agent-record-janitor/operations/<operation-id>/
├── plan.json
├── events.jsonl
├── state.json
├── result.json
└── apply.lock        # only while a live apply owns the mutation gate
```

`events.jsonl` has strictly increasing sequence numbers. Every state, result,
and event is bound to the operation ID and plan hash. New files, replacements,
and directory creation are flushed durably before mutation can start, and
symlinks, junctions, reparse points, and non-regular journal leaves are rejected.
Reusing an operation ID with another plan is blocked. A lock left by a crashed
process is not automatically removed because its mutation outcome may be unknown.

Successful legacy-index and Desktop-state mutations retain their backup ID and
backup path in both `result.json` and the final journal event. The retained audit
payload is field-limited and never includes conversation message bodies.

## Result contract

Automation should branch on these fields, not translated prose:

- `goal_status`: `complete`, `completed_with_residuals`, `blocked`, or
  `unknown`;
- `goal_satisfied`: true only for `complete`;
- `modified`: true only when verification confirms at least one planned target
  changed;
- `mutation_started`: true once a modifier may have been called;
- `blockers[].blocker_code`: stable decision code with scope, severity,
  retryability, and remediation;
- `counts`: finding, issue-group, root-action, affected-thread, artifact,
  blocked-group, and legacy residual counts.

Cleanup authorization is decided from `cleanup_blocker_codes`, never by parsing
localized blocker prose. Missing, unknown, or malformed blocker codes fail
closed even if the display text happens to contain words such as “cascade”.

If `goal_status=unknown`, do not retry apply. Use status and verify. If verify
reports residuals, inspect them and create a new plan; do not edit the old plan
or widen its root-action list.
