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
and follow its task-specific reading routes and execution constraints.

## 文档归属

- README（含子目录）说明当前功能、用法、必要限制和文档入口，不追加执行流水或测试通过数量。
- 普通代码、配置、文档修改及常规验证由 Git 提交说明、PR 或 CI 记录；不为每次任务新建维护记录、目录或索引。
- 长期有效的设计、兼容性结论和操作方法更新已有专题文档；版本历史沿用已有 CHANGELOG。只有 Git/CI 无法还原、后续仍需排障或恢复的外部证据，才补充必要记录。
- 纯文档修改检查链接和 diff，不运行部署或业务全量测试。
