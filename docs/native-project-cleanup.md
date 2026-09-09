# Native local-environment registration cleanup

ChatGPT Desktop's local Environments page can retain a project after its root
directory and native project have disappeared. The `native` client now exposes
these registrations through `records` and the existing high-level operation
protocol. This is a separate `delete_native_project` mutation family; no
`thread/delete` request is sent and no cloud project is deleted.

## Selection and evidence

Use the official native `CODEX_HOME`, never a Cindy/AionUI substitute. Select a
complete project ID with `--record-id`, or a unique project name/path with
`--project`. Partial project IDs are not accepted as record selectors. Two
registrations with the same name remain ambiguous. `--all-projects` includes
eligible orphan registrations; existing directories remain inventory-only and
are not automatically removed. Review the frozen action list before applying.

The supported registration schema has `id`, `name`, `rootPaths` and optional
`createdAt`/`updatedAt`. Copies must agree. Each root must be an absolute local
path that is missing on an accessible volume, with no symlink/reparse ancestor.
Read-only native database checks must prove the absence of matching project IDs,
mapped IDs, root paths and descendant working directories. Unavailable or
unproven database schemas do not count as absence.

Only these keys may be removed, in the main global-state file and its existing
`.bak` copy:

- `local-projects[project_id]`;
- the boolean `sidebar-project-expanded-v1-chatgpt:<id>` or
  `sidebar-project-expanded-v1-codex:<id>` atom;
- the selected ID in `app-server-project-id-by-legacy-project-id-by-host`, for
  the exact `local:<CODEX_HOME>` host only.

Any other reference to the project, root or mapped ID blocks deletion. Unknown
registration fields, duplicate JSON keys, linked state files and differing
copies are inventory-only or structured scan errors. This adapter does not
guess new UI schemas or remove prompt text. Output contains project metadata,
file hashes and counts only, never global-state content or chat bodies.

## Apply and recovery

One batch owns one native home and can contain several exact project IDs. The
plan binds hashes for both named files, including absence of `.bak`. Apply
checks that evidence again, checks owning clients, and checks paths and native
references immediately before writing. It removes only approved keys, verifies
the expected count and the complete resulting JSON payload, and deletes its
temporary rollback files after success. JSON whitespace may be normalized;
unrelated keys and values are preserved.

A failed write restores the originals only when the current files still match
what this writer wrote. A conflicting change causes `unknown`; the writer keeps
rollback copies plus a body-free `.larj-project-recovery-*` manifest. These files
are recovery evidence, not a backup product. Never retry an unknown apply.

`operation verify` checks frozen project and mapped-ID markers even if only a
side-panel reference remains and discovery can no longer create a project
action. When recovery evidence exists, it first proves the before/after hashes
against the frozen authorization and the rollback data. It releases temporary
copies only for a fully verified completed write or fully restored rollback.
Mixed files, missing approved files or unrelated changes remain unknown: a file
replaced with `{}` must not be reported as successful cleanup. Verification
never repairs the live configuration; only verified temporary evidence may be
removed. A fresh plan is required for any newly discovered work.

## Usage

```powershell
local-agent-record-janitor records --client native --json
local-agent-record-janitor delete plan --client native --record-id '<complete-project-id>' --out .\environment-plan.json
local-agent-record-janitor delete apply --operation-id '<operation-id>' --plan .\environment-plan.json --clients-closed
local-agent-record-janitor operation status --operation-id '<operation-id>' --plan .\environment-plan.json
local-agent-record-janitor operation verify --operation-id '<operation-id>' --plan .\environment-plan.json
```

Only pass `--clients-closed` after confirming the owning client is closed. The
core also checks process ownership, freezes the authorization and prevents a
second deletion after an unknown result.
