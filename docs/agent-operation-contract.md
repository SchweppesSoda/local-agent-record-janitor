# Agent record operation contract

Use the non-interactive `records`/`delete`/`operation` surface for live records.
Do not drive human `clean`/`purge` prompts or use legacy `agent` commands as a
fallback when the operation API is unavailable.
For long inventory, deletion or verification commands, add `--progress` and
surface its stderr phase/count events while keeping stdout as the JSON result.

## Read for the selected task

- High-level inventory or deletion: [Commands](operation-cli.md#commands) and
  [Output](operation-cli.md#output), including its linked result contract.
  For official native stores, also read
  [Official Desktop inventory and completion](operation-cli.md#official-desktop-inventory-and-completion).
- Native local-environment registrations: also read
  [native project cleanup](native-project-cleanup.md); project IDs are not thread IDs.
- Existing legacy `agent` integrations: read [Commands](agent-automation.md#commands)
  and [Result contract](agent-automation.md#result-contract) instead of the high-level route.
- Recovery evidence or rollback copies: read
  [Operation evidence and receipts](agent-automation.md#operation-evidence-and-receipts)
  and [Temporary rollback copies](agent-automation.md#temporary-rollback-copies).

## Required agent workflow

1. Select one exact client and its physical stores. Official Codex/ChatGPT
   Desktop uses `native`/`codex-native` with the official native `CODEX_HOME`,
   never a Cindy/AionUI substitute. Inventory and reports expose metadata only;
   do not read or expose chat bodies.
2. Review plan targets, counts, blockers, progress groups and `plan_sha256`.
   Each immutable child batch owns one physical store and mutation family.
   `delete plan/run` performs preflight; `agent doctor` is optional. Structured
   blockers must be resolved; unsupported backends or unproven schemas remain inventory-only.
3. Apply only within the user's frozen authorization after the owning clients
   are closed. Never invent a hash or `--clients-closed` acknowledgement.
   Repeated scope selectors must match the full frozen scope. Within unchanged
   authorization, proceed without a second confirmation. Exact frontend-reference
   and relation writes require supported schemas, immutable row evidence,
   exact affected-row counts and post-write verification.
4. Cindy retained chat-row deletion requires explicit Cindy session IDs via
   `--record-id`; native SDK IDs do not authorize it. Project/all-projects
   cleanup retains its soft-deleted-only default.
5. Decide from `goal_status`, `goal_satisfied`, structured blockers and exit
   codes, never human message text. Only verified `complete` satisfies the goal.
6. If a mutation result is `unknown`, never repeat it: run `operation status`,
   then `operation verify` (legacy integrations use `agent status/verify`).
   Preserve the exact plan/state location. Never edit a frozen plan or journal,
   delete/bypass `apply.lock`, or discard unresolved recovery evidence.
   When a parent plan is missing, the supported native v1 child-only
   `operation status/verify` route can use the original child journal; see
   [the operation CLI recovery boundary](operation-cli.md#commands).
7. Further work after verification needs a fresh plan and must remain within
   the user's authorized scope. Newly discovered actions are not authorized by
   an old plan. Report residuals only from authoritative discovery evidence;
   otherwise report the capability boundary. `remote_delete=false` permits no remote writes.

Keep the record, or delete the whole verified record and every approved copy;
never offer `repair_index_path` or `quarantine_artifacts`. Shared-file rollback
copies are temporary and removed immediately after successful verification; receipts are
not backups. Recovery must prove the frozen state before completion or evidence removal.

The permanent `operations/.mutation.lock` serializes cooperating processes for
one trusted local root, from journal admission through mutation and result
publication. Verification and receipt cleanup use the same root lock. Never
delete or replace it. This root lock does not prove that an older writer holding
`apply.lock` has exited; that operation remains unknown even with a terminal
receipt. Unknown journals have no expiry. Missing or untrusted journal scope
blocks the whole affected root. Exact frozen IDs and artifact paths allow
independent targets and stores to proceed when their scopes do not overlap.
The qualified Orca v3/child-v2 startup boundary occupies the whole selected home:
another record in that home remains blocked while startup/teardown is unknown.
Its original named Job must be absent on the same machine and Windows session
before read-only recovery can publish a known result.

Legacy direct CLI/GUI and service writers share this admission gate, so they
cannot bypass an existing journal's unknown outcome. They do not create a new
operation journal themselves; durable recovery of a new direct-call timeout is
not provided by this gate. Use the journaled operation surface for that
guarantee. There is no global lock or replay guarantee across different native
roots, unproven bridges, or older versions that do not use the root mutex.
