# AGENTS.md — local-agent-record-janitor

This repository provides a non-interactive cleanup protocol specifically for
software agents. Use it instead of driving the human `clean`/`purge` prompts.
The high-level `records`/`delete`/`operation` commands are the preferred
multi-project surface; the older `agent` commands remain a one-store,
one-mutation-family compatibility surface.

## Required agent workflow

1. Work on one exact client selection at a time. For official OpenAI Codex and
   ChatGPT Desktop, use client `native`/`codex-native` and the official native
   `CODEX_HOME`; do not substitute a Cindy or AionUI store.
2. `delete plan` and `delete run` perform their own read-only preflight.
   `agent doctor` is an optional diagnostic and is not a prerequisite for the
   high-level operation path. If a diagnostic or preflight reports a
   structured blocker, the operation remains blocked until that blocker is
   resolved.
3. A high-level plan covers one selected client and may contain multiple
   immutable child batches. Each child batch still targets exactly one
   physical store and one mutation family. Review target, counts, blockers,
   progress groups, and `plan_sha256` without reading or exposing chat bodies.
4. Apply only within the user's frozen authorization and after the owning
   clients are closed. The operation store binds the plan hash internally;
   callers must not invent a hash or a `--clients-closed` acknowledgement.
   If an apply request repeats a client/project/engine/record scope, the core
   must compare it with the frozen plan in full; a mismatched selector blocks
   the operation rather than silently narrowing or widening it.
5. Treat `goal_status`, `goal_satisfied`, structured blockers, and the exit code
   as authoritative. Do not decide from human message text.
6. If any mutation result is `unknown`, never repeat it. Run
   `operation status`, then `operation verify`. A repeated apply is
   intentionally prevented from sending a second deletion.
7. After a verified operation completes, create a fresh plan. Newly discovered
   actions are never absorbed into an old authorization. Continue only while
   the user's authorized client/project scope still covers the new operation.

The legacy `agent` workflow below keeps its stricter one-store contract for
existing integrations. Its `doctor`/`plan`/`apply` commands are not an
additional execution path for the high-level operation API.

```powershell
local-agent-record-janitor records --client native [--project SELECTOR]

local-agent-record-janitor delete plan --client native --all-projects `
  --out .\operation-plan.json
local-agent-record-janitor delete apply --operation-id '<operation-id>' `
  --plan .\operation-plan.json --clients-closed
local-agent-record-janitor delete run --client cindy --project '<project-id>' `
  --clients-closed

local-agent-record-janitor operation status --operation-id '<operation-id>'
local-agent-record-janitor operation verify --operation-id '<operation-id>'
```

The high-level operation output is grouped by project, engine, and physical
location and contains metadata only. It uses the stable classifications
`healthy`, `orphan_native`, `orphan_frontend`, `orphan_project`,
`broken_relation`, `stale_index`, `partial_remote`, `corrupt_unreadable`, and
`unknown_operation`. These are stable classifications, not a promise that
every adapter discovers or deletes every class. Unsupported backends and
unproven schemas are inventory-only. AionUI orphan project/conversations rows
are executable only when the adapter proves the supported schema, immutable
row evidence, and zero `acp_session` references; all other schemas remain
inventory-only. When an adapter supplies authoritative remote discovery
evidence, `remote_delete=false` reports those residuals without performing
remote writes; without that evidence, report the capability boundary only and
make no residual claim.

```powershell
local-agent-record-janitor agent doctor `
  --platform native --codex-home 'D:\exact\CODEX_HOME'

local-agent-record-janitor agent plan --operation purge `
  --platform native --codex-home 'D:\exact\CODEX_HOME'

local-agent-record-janitor agent apply `
  --plan '.\janitor-plan.json' `
  --authorized-plan-sha256 '<exact-plan-sha256>' `
  --clients-closed

local-agent-record-janitor agent status `
  --operation-id '<operation-id>' --codex-home 'D:\exact\CODEX_HOME'

local-agent-record-janitor agent verify `
  --operation-id '<operation-id>' --codex-home 'D:\exact\CODEX_HOME' `
  --verify-timeout 180
```

All agent subcommands emit JSON only and never read stdin. Exit codes are:

- `0`: the read-only command succeeded, or the frozen cleanup goal is verified
  complete;
- `1`: the result is unknown or could not be trusted;
- `3`: the goal is blocked or completed with residuals;
- `2` is reserved for human confirmation flows and is never an agent result.

Operation evidence is stored under
`<CODEX_HOME>/.local-agent-record-janitor/operations/<operation-id>/`. Do not
delete an `apply.lock`, edit a plan, or repair an operation journal in place.
An `unknown` result retains the detailed recovery evidence. A known terminal
result is compacted to a body-free `receipt.json`; the receipt expires after at
most seven days and must never be treated as a backup. See
`docs/agent-automation.md` for the JSON and verification contract.

Shared SQLite/JSON mutations use temporary rollback copies only. Exact frontend
reference and relation-edge actions require a closed owning client, a supported
schema, immutable row evidence, an exact affected-row count, and post-write
verification. Successful verification deletes the temporary copy immediately.
Never offer `repair_index_path` or `quarantine_artifacts`: keep the record, or
delete the whole verified record and every approved copy.
