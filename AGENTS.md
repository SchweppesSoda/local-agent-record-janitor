# local-agent-record-janitor maintenance

This repository implements record cleanup. Editing its code/documentation is
distinct from executing deletion against a user's live stores.

## Source work

- Use the existing `main` checkout with one writer. Inspect branch, upstream,
  worktree and existing changes first; preserve unrelated work. A new branch
  or worktree requires the user's isolation/parallel-work request.
- Complete requested reversible local edits, relevant checks and scoped local
  commits without pausing after a first draft. Search affected APIs and direct
  consumers first; expand for shared protocol changes or demonstrated risk.
- Documentation needs link/diff checks. Code changes use relevant existing
  tests with temporary stores; shared deletion/authorization changes need the
  corresponding protocol regression coverage. Never use live cleanup as a test.
- Report commit, validation and unresolved limits. Push/release/live cleanup
  follows the user's existing authorization; prepare local work before asking
  for any missing external or destructive authorization.

## Live record operations

Only when asked to inspect/delete actual records, read [operation contract](docs/agent-operation-contract.md)
and the required sections of [protocol documentation](docs/agent-automation.md).
Use the non-interactive `records`/`delete`/`operation` surface. Select the exact
client/store, honor the frozen plan/hash/scope and closed-client requirements,
and never repeat an unknown mutation. Recover through status/verify. Unsupported
schemas remain inventory-only. Expose metadata only, never chat bodies.
