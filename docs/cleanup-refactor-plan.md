# 0.2.0 清理核心重构

状态：已实施，等待发布矩阵验收。

## 目标

项目只负责：

> 找出失效或用户明确选择的本地 Agent 记录，展示精确影响，永久删除，然后确认删干净。

非目标包括数据恢复平台、长期备份库、rollout 隔离区、路径修复器，以及根据不完整
证据自动推测关联关系。重复 rollout 或路径错位只允许保留，或删除整条记录和全部已
批准副本。旧的 `repair_index_path`、`quarantine_artifacts` 枚举仅用于读取旧 JSON，
不会出现在新计划中。

## 架构结果

```text
Driver → StoreSnapshot → Planner → AuthorizedAction → Guard → Executor → Verifier
```

- 人类 CLI 与 Agent CLI 共用唯一 `CleanupService`；
- `StorageRef/RecordRef/Evidence/Action/GuardToken/Result` 明确分离身份、证据、
  授权和结果；
- 一份计划只授权一个物理存储和一种 mutation family；
- blocker code 和类型化状态决定是否允许执行，自由文本只负责显示；
- `ClientInspector` 按可执行文件、进程关系和物理 store 归属判断，未知才 fail closed。

## 已实施动作

| Mutation family | 精确语义 |
|---|---|
| `delete_conversation` | 官方 `thread/delete`，同批单 app-server；仅对严格匹配的 API 残留 rollout 做受限清除 |
| `remove_broken_relation` | 删除 parent、child、status、schema 与行指纹完全匹配的一条关系边 |
| `repair_legacy_index` | 兼容 action kind；实际只删除已证明失效的原始索引整行 |
| `remove_desktop_state` | 删除精确 local catalog 行和结构化 UI 引用 |
| `remove_frontend_reference` | AionUI 精确行删除；Cindy 精确字段清理 |
| `delete_pi_session` | 删除一份 storage-qualified Pi JSONL |
| `delete_claude_session` | 删除一份批准的 Claude manifest |

AionUI 无主键旧 schema 只接受重验证的 `rowid + 完整行指纹`。Cindy 当前引用只清空
`sdk_session_id`；历史引用只移除绑定消息 ID 和内容哈希的
`agent_switch.fromSdkSessionId`，其他 JSON 字段和消息行保持不变。

## 回滚与 operation 生命周期

- 会话、独立 rollout、Pi JSONL 和 Claude manifest 永久删除，不创建备份；
- 共享 SQLite、旧索引与 Desktop JSON 使用临时回滚副本；
- 成功写入或成功自动回滚后立即删除副本；
- partial、unknown 或回滚失败时暂时保留，等待 verify；
- 结果未知时保留详细 journal；
- 已知终态压缩成无正文最小回执，最长保留七天；
- 没有公开 recover/restore 命令。

## 性能约束

`plan` 一次完整快照；`apply` 一次完整预检、N 次 action-local guard 和一次完整终检。
同一 Codex 删除批次只启动一个 app-server。发现漂移后停止剩余动作，不重新扫描整个
store。100 个 action 的性能测试必须固定为两次 full scan。

## 分阶段提交

1. 基线稳定：修正 store 归属、blocker code、测试与匿名指标；
2. 统一类型和服务：引入 typed core 与 `CleanupService`，保留兼容 facade；
3. Codex 执行路径：合并重复编排、单 app-server、定点 guard、逐 action 状态；
4. 前端引用：AionUI/Cindy 精确写入器与回滚测试；
5. Pi/Claude：接入统一计划和 operation 状态并保持 storage 隔离；
6. 旧架构收口：精确关系写入器、最小回执、临时备份生命周期、依赖方向测试、
   文档与版本 0.2.0。

每个阶段是独立提交并要求当时测试全绿，可单独回滚。

## 发布验收

- Windows、macOS、Linux × Python 3.10/3.12 测试矩阵通过；
- 默认单元测试不读取真实宿主进程；
- 所有确定且精确的问题有删除动作，不确定项只返回明确 blocker；
- AionUI/Cindy 只改变批准行或字段；
- 100 个 action 的 apply 只有两次完整扫描；
- 崩溃/超时后的未知动作不会重复发送；
- 成功后没有长期备份、隔离文件或完整 journal；
- 新扫描显示真实零目标或剩余未授权目标；
- 计划、回执和输出不含聊天正文；
- Windows 中文输出和既有 PowerShell/CMD/sh 调用方式继续有效。
