# Adapter 贡献指南

Adapter 用于把一个外部 Agent 平台的删除状态转换为保守的 Codex `Finding`。它不能直接删除前端或 Codex 数据。

## 最低要求

一个可合并的 adapter 必须：

1. 明确平台数据库和该平台使用的 `CODEX_HOME`，不得假设所有前端共用 `~/.codex`。
2. 用 SQLite `mode=ro` 或等价只读方式访问数据库。
3. 只读取识别关系所需字段，不读取消息正文。
4. 同时提供“平台明确是 Codex”的证据与对话 ID。
5. 在 rollout 存在时校验 `session_meta.originator`，对冲突证据 fail closed。
6. 只返回仍有 Codex artifact 的 Finding。
7. schema 缺失、数据库损坏或被锁时返回可观察错误，而不是把它解释为“零残留”。
8. 不修改平台数据库，不删除文件，不直接修改 Codex SQLite。
9. 为每条规则提供正例、反例、schema 演进和特殊字符测试。
10. 显式设置兼容能力字段 `thread_delete_supported` 与 `cleanable`，并提供具体阻断理由；计划生成器仍会独立检查当前状态和影响范围。

## 建议接口

```python
class ExampleAdapter(FrontendAdapter):
    name = "example"

    def scan(self) -> list[Finding]:
        ...
```

构造参数至少包括：

- `database: Path`
- `codex_home: Path`
- 可选 `codex_bin_hint: Path`

Finding 的 `details` 应包含平台状态、originator 证据和 adapter 判断所需的非正文信息。不要把消息、prompt、工具输出或环境变量放进 JSON。

建议所有 adapter 使用统一能力字段：

```python
details = {
    "thread_delete_supported": True,
    "cleanable": True,
    "needs_quarantine": False,
    "cleanup_blocked_reason": None,
}
```

这些字段描述的是“当前这条 Finding”而不是平台的永久能力。它们是计划生成器的证据输入，不会直接成为 CLI 删除目标。存在活跃引用、冲突证据或不完整关系时，应将 `cleanable` 设为 `False` 并给出阻断理由。

Adapter 不负责决定最终风险或动作。核心层会按 `(storage_id, full_thread_id)` 聚合全部 Observation，枚举列表记录和内容文件，读取完整关联任务范围，并生成 `delete_conversation`、修复、隔离、清除引用或 `keep` 等 CandidateAction。当前可执行整条对话删除；旧聚合索引还可通过独立的文件级修复器处理，其他修复和隔离动作仍只展示。

## 判定模板

建议按以下顺序：

1. 数据库/目录是否存在；
2. schema 是否兼容；
3. 平台行是否明确处于已删除或不可见状态；
4. agent/backend 是否明确为 Codex；
5. 对话 ID 是否非空；
6. Codex index/rollout 是否存在；
7. rollout originator 是否与平台一致；
8. 是否存在反证，例如同一对话仍被另一个活跃会话引用；
9. 才生成 Finding。

最后一项很重要：同一个 Codex 对话可能被平台内多个对象引用。实现必须优先查询活跃引用，不能因为发现一个 tombstone 就把仍在使用的对话提供为可执行删除动作。

扫描异常应携带受影响的 `codex_home`，使计划能把失败限制到对应 StorageLocation。只有确实无法归属保存位置的错误才应成为计划级错误并阻止所有动作。

## 时间与年龄

平台时间戳单位可能是秒、毫秒或 ISO 8601。adapter 应明确转换，不要用数值大小猜测后直接参与自动删除。

未来若加入 `--min-age`：

- 以平台删除时间为首选；
- 缺少删除时间时不得用文件 mtime 代替强证据；
- 系统时钟回拨应导致拒绝删除；
- 年龄只是一层保护，不会把弱证据变成强证据。

## Codex 二进制发现

前端可能捆绑特定版本 Codex。adapter 可以给出 `codex_bin_hint`，但核心层必须：

- 验证是普通文件；
- 不通过 shell 拼接用户输入；
- 设置正确的 `CODEX_HOME`；
- 在输出中展示实际使用的二进制；
- 找不到可信二进制时停止清理，不能降级为直接删文件。

## 测试矩阵

每个 adapter 至少应有：

- 已删除 + 正确 Codex 证据 → Finding；
- 活跃会话 → 不报告；
- 非 Codex agent → 不报告；
- originator 冲突 → 不报告；
- 空/null 对话 ID → 不报告；
- 只有索引 → 按能力标志处理；
- 只有 rollout → 按能力标志处理；
- 同一对话仍有活跃引用 → 保留 Observation，并阻止删除动作；
- 缺表、列变化、损坏 DB、锁冲突 → 显式错误；
- 对话 ID 含 SQL 特殊字符 → 参数绑定且不破坏表；
- 不同 `CODEX_HOME` 下相同 ID → 两个独立目标，旧 `--thread-id` 选择器必须拒绝歧义；
- 重复 rollout / 路径冲突 → 只有身份和精确范围均可验证时才提供 `high` 风险整条对话删除，且必须逐项明确选择；否则阻止。
- schema 不兼容的关联表 → 显式错误，不能解释成没有关联任务对话。

测试必须使用合成 fixture，不能提交真实用户数据库或 rollout。

## 提交说明

PR 应说明：

- 平台与已验证版本；
- 默认数据库及 Codex home 发现路径；
- 删除/软删除状态的确切语义；
- 反误删查询；
- 是否支持官方 `thread/delete`；
- 已知 schema 变体；
- 完整测试矩阵。

若平台本身可修复删除流程，也请同时向上游报告。Janitor 是修复遗留状态的工具，不应成为前端跳过正确生命周期管理的理由。
