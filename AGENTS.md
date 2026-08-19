# AGENTS.md — local-agent-record-janitor

This repository provides a non-interactive cleanup protocol specifically for
software agents. Use it instead of driving the human `clean`/`purge` prompts.

## Required agent workflow

1. Work on one exact `CODEX_HOME` at a time. For official OpenAI Codex and
   ChatGPT Desktop, use `--platform native` and the official native
   `CODEX_HOME`; do not substitute a Cindy or AionUI store.
2. Run `agent doctor` and resolve every structured blocker.
3. Run `agent plan --operation purge --out ...`. A plan authorizes at most one
   immutable mutation batch. Review its target, counts, blockers, root actions,
   affected thread IDs, and `plan_sha256` without reading or exposing chat
   bodies. Treat an empty plan as a snapshot only; `apply` still re-scans the
   complete target before reporting success.
4. Call `agent apply` only when the cleanup is within the user's authorization
   and the clients for that exact store are closed. Pass the plan's exact
   `plan_sha256`; never invent or assume `--clients-closed`.
5. Treat `goal_status`, `goal_satisfied`, structured blockers, and the exit code
   as authoritative. Do not decide from human message text.
6. If apply returns `unknown`, never repeat it. Run `agent status`, then
   `agent verify`. A repeated apply is intentionally prevented from sending a
   second deletion.
7. After a verified batch completes, create a fresh plan. Newly discovered
   actions are never absorbed into an old authorization. Continue only while
   the user's authorized scope still covers the new batch.

```powershell
local-agent-record-janitor agent doctor `
  --platform native --codex-home 'D:\exact\CODEX_HOME'

local-agent-record-janitor agent plan --operation purge `
  --platform native --codex-home 'D:\exact\CODEX_HOME' `
  --out '.\janitor-plan.json'

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
See `docs/agent-automation.md` for the JSON and recovery contract.
