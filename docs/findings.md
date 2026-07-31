# Finding、Observation 与候选动作

Finding 是 adapter 的兼容证据格式，描述一个不一致状态，不等同于“可以自动删除”。计划生成器会把同一保存位置和完整对话 ID 的 Finding 聚合成 Observation，再根据当前列表记录、全部内容文件、关联任务范围、活跃引用和扫描状态生成 CandidateAction。

所有问题都会显示候选动作。当前只有 `delete_conversation`（删除整条对话）执行器可用；其他动作会保留在结构化输出中，并明确标为不可用。

## 前端残留

### AionUI orphan mapping

条件：

- `acp_session.conversation_id` 找不到对应 `conversations.id`；
- `session_id` 非空；
- agent metadata 明确 backend 为 Codex，或 rollout originator 明确为 `aionui-session`；
- Codex 索引或 rollout 至少存在一项。

典型原因是 AionUI 删除了主对话，但映射清理失败，而且未调用 Codex 硬删除。

### Cindy soft-deleted session

条件：

- `sessions.agent_kind='codex'`；
- `sessions.status='deleted'`；
- `sdk_session_id` 非空；
- rollout originator 不与 Cindy 冲突；
- Codex 索引或 rollout至少存在一项。

这里的“孤立”是从可见 UI 的角度描述。Cindy 数据库仍保留 tombstone 和可能的消息行；Janitor 不删除它们。

## Codex 原生完整性

### `index_missing_rollout`

`state_5.sqlite` 存在对话列表记录，但记录指定的内容文件不存在，也未在活动/归档目录中找到同 ID 内容文件。

这是此前匿名化复现中的“只有列表记录、没有内容”。如果官方 `thread/delete` 能处理，计划可能提供删除整条对话动作。

Codex `0.144.6` 隔离验收已确认这种状态可由官方接口收敛，无需直接修改 `state_5.sqlite`。

### `rollout_missing_index`

活动或归档目录中存在有效 `session_meta`，但对话列表记录不存在。

Codex `0.144.6` 隔离验收已确认官方接口能够删除合成的有效 rollout-only 对话。实现仍须验证有效 metadata、唯一 ID 和冲突证据；通过这些检查后才可提供删除动作，否则应显示隔离或保留等候选决定。

### `index_rollout_path_mismatch`

对话列表记录中的 `rollout_path` 与实际扫描到的同 ID 内容文件路径不一致。

可能是归档/移动中断、旧路径残留或重复 rollout。计划会显示路径修复、删除和保留等候选动作。路径修复执行器尚未实现；当每个文件的对话身份和当前精确范围均可验证时，整条对话删除可作为 `high` 风险动作逐项明确选择。任何已知文件残留都会使结果成为 `partial`。

### `orphaned_subagent_thread`

对话有明确 subagent 来源证据，但可识别父对话的列表记录和内容文件都不存在。

这对应此前匿名化复现中的“孤立关联任务对话日志”。仅仅出现在子目录、名字相似或工作目录相同都不构成该 Finding。

即使分类成立，也只有父对话记录和内容文件全无、关系边不是 open、且完整关联范围可读取等严格条件同时成立时，才会提供删除候选。删除影响必须显示该对话创建的其他关联任务对话；执行前重新读取的范围必须与用户已批准范围完全一致。

### `residual_spawn_edge`

对话关联记录的一端缺失。

计划始终会显示 `remove_broken_relation` 候选动作，但当前不可执行。若关联记录指向的子对话仍有身份精确可验证的列表记录或内容文件，且 originator/source/parent 等来源证据无冲突，计划还可显示必须逐项选择的 `high` 风险 `delete_conversation`。该动作删除这条子对话及其精确批准的关联任务范围，不是单独删除关系边；子对话无现存本地数据、来源冲突或范围不精确时不得提供该授权。

### `legacy_index_only`

某些旧版 Codex 可能还使用不同索引格式。能够识别但不能由当前安全路径处理的旧索引，应标记为 unsupported，而不是直接修改。

## 常用字段

| 字段 | 含义 |
|---|---|
| `platform` | `aionui`、`cindy` 或 `native` |
| `platform_session_id` | 前端会话 ID；原生 Finding 可能使用对话自身标识 |
| `thread_id` | 完整 Codex 对话 ID |
| `reason` | 面向人的判断理由 |
| `platform_db` | 证据来源数据库；原生 Finding 可能为空或指向 state DB |
| `codex_home` | 该对话所属 Codex 数据目录 |
| `rollout` | 首行 metadata 与实际路径，不含聊天正文 |
| `codex_indexed` | 当前索引是否存在 |
| `details.finding_type` | 原生完整性分类 |
| `details.thread_delete_supported` | 兼容字段：adapter 是否认为官方 API 可处理该证据 |
| `details.cleanable` | 兼容字段：adapter 的类别特有安全条件是否满足 |
| `details.cleanup_blocked_reason` | 不能自动删除时的具体阻断理由 |
| `details.needs_quarantine` | 是否应人工复核/未来隔离，而不是自动删除 |

这两个兼容能力字段仍会被读取，但不再是 CLI 的唯一分区依据。计划动作还必须通过身份、当前状态、影响范围、活跃引用和扫描完整性检查。

## 问题到动作

| 问题 | 结构化候选动作 | 当前执行状态 |
|---|---|---|
| 前端已删除、Codex 对话仍存在 | `delete_conversation`、`remove_frontend_reference`、`keep` | 仅整条对话删除可在安全条件满足时执行 |
| 列表记录存在、内容文件不存在 | `delete_conversation`、`keep` | 删除通常为 `low` |
| 内容文件存在、列表记录不存在 | `delete_conversation`、`keep` | 删除为 `high`，必须明确选择 |
| 多份内容文件 | `quarantine_artifacts`、`delete_conversation`、`keep` | 隔离未实现；身份和范围可验证时可逐项选择 `high` 删除 |
| 列表路径错位 | `repair_index_path`、`delete_conversation`、`keep` | 修复未实现；身份和范围可验证时可逐项选择 `high` 删除 |
| 无效关联记录 | `remove_broken_relation`、可选 `delete_conversation`、`keep` | 单独关系修复未实现；记录指向的子对话身份、来源与精确范围均可验证时，可逐项选择 `high` 风险整条删除 |
| 旧聚合索引 | `repair_legacy_index`、`keep` | 修复未实现，不能把聚合项当对话 ID |

CandidateAction 的稳定 action ID、风险、可用性、不可用原因、ActionImpact 和快照指纹都进入 JSON。自动化应使用完整 action ID，不应持久化交互编号。
