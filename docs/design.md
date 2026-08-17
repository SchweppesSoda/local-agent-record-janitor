# 本地 Agent 记录检测模型与安全边界

本文档描述 Local Agent Record Janitor 的核心模型。跨引擎对象称为 local agent
record；Codex 对象称为 thread，Pi Agent 和 Claude Code 对象称为 session，
Cindy/AionUI 行称为 frontend reference/mapping。

## 核心模型

Janitor 把“发现问题”和“执行删除”分开：

```text
Finding（adapter 证据）
  → Observation（发现的问题）
  → TargetRef（native store + 完整 Codex thread ID）
  → ConversationSummary（兼容类型名；内容是 thread 名称、项目、父子代理关系和证据指纹）
  → CandidateAction（候选动作、风险、影响、可用性）
  → CleanupPlan（用户明确选择的动作）
  → ActionResult（验证后的结果）
```

本节当前对应 Codex 完整性清理子系统。Finding 在兼容期仍是 adapter 输出格式，但
不等于删除目标。稳定动作身份是
`(storage_id, full_thread_id, action_kind)`；同一 ID 出现在不同 Codex 数据目录时必须是不同目标。

每个 StorageLocation 保存稳定 ID、用户可读标签、规范化绝对路径、可选 Codex 可执行文件提示、扫描状态和仅影响该位置的错误。

## 分层状态

```text
Frontend namespace -- frontend reference/mapping --> Native record
Harness runtime -- operates on --> Native store -- contains --> Native record

Frontend namespace: Cindy/AionUI owner + database + frontend conversation/session ID
Harness runtime:    Codex executable path + version
Native store:       CODEX_HOME
Native record:      Codex thread (thread_id)
                    ├── state_5.sqlite：thread 列表记录和关联记录
                    └── sessions|archived_sessions：rollout 内容文件
Optional host state: Codex Desktop local catalog + structured global UI state
```

扫描只读取前端 SQLite 的最小必要字段、rollout 首行 `session_meta`、必要的 Codex
thread 列表记录和关联记录，不读取聊天正文，也不修改文件或数据库。frontend
reference 只是归属、生命周期和 live guard 证据，不能作为原生 thread 的替代身份。

认证来源、登录状态、OAuth/API 凭据归属和 frontend owner 只属于诊断信息。它们不参与
删除目标、action ID 或 fingerprint，不得代替 `CODEX_HOME + thread_id`。harness runtime
也不是身份；认证至多绑定或授权 harness。同一 harness 可以访问不同 native store，
不同 harness 也可能访问同一路径，执行前必须同时确认可信 runtime 与目标 store。

## 计划生成

计划生成器按 `(storage_id, full_thread_id)` 聚合 Finding，同时保留全部 Observation。它会：

1. 枚举活动和归档目录中的全部同 ID 内容文件；
2. 读取由根 thread 创建的完整关联任务 thread 闭包；
3. 收集列表记录、活跃 frontend reference 和扫描错误；
4. 从列表数据库、rollout 首行和旧索引名称构建统一 thread 目录，显示根 thread 和
   完整关联任务 thread 的名称、完整项目目录、Git 来源、父 thread、子代理昵称/角色/
   路径及元数据冲突；
5. 为每个候选动作计算风险、影响、稳定 action ID 和快照指纹，并为当前每个内容文件计算身份、来源和 `stat` 状态指纹；
6. 把未实现的修复动作结构化显示，并给出不可用原因。

快照覆盖 native store、完整 thread ID、问题类型、列表存在状态、已知 rollout 路径、
关联任务 thread ID、活跃 frontend reference 状态、thread 元数据证据和逐文件状态指纹。
thread 元数据指纹绑定规范化展示字段以及实际数据库行、rollout `session_meta`、旧索引
名称的原始证据哈希；逐文件指纹覆盖 metadata thread ID、originator、source、工作目录、
时间戳、活动/归档状态、规范化路径、大小和纳秒修改时间。任何范围或身份无法精确生成
都会阻止删除。当前可执行的是 `delete_conversation`（为 JSON v1 保留的 action kind，
语义是删除整个 Codex thread）和独立的 `repair_legacy_index`；修复列表路径、清除无效
关系、隔离文件、清除第三方 frontend reference 等动作不会降级成直接 SQLite 或任意
文件操作。唯一额外可执行动作是 `remove_desktop_state`：它只处理原生证据已经为空的
Codex Desktop local 宿主残留，属于独立 `high` 风险修复器，并绑定 catalog 行、完整
JSON 哈希、结构化精确引用、客户端关闭声明和一致备份。

风险定义：

- `low`：没有可恢复聊天内容，例如只有列表记录；
- `review`：仍有聊天内容，但前端已经删除或关系明确孤立；
- `high`：内容文件不在列表、重复、路径错位或需要内部修复；
- `blocked`：身份冲突、范围无法计算、状态读取失败或计划已经变化。

交互编号和范围（如 `1,3-5`）是当前进程内的临时选择。`all` 只选择 `low` 风险动作；自动化必须使用完整 action ID 或计划 JSON。同一 target 同时选择 `keep` 和任何其他动作属于矛盾计划，必须拒绝。

## 扫描失败的作用域

带 Codex 数据目录信息的扫描失败只把对应 StorageLocation 标为失败，并阻止该位置的动作。一个独立目录读取失败，不会无条件阻止其他已完整读取的位置。

无法归属到保存位置的错误会进入计划级错误并按 fail closed 处理。关系表存在但 schema
不兼容属于读取失败，不能解释成“关联任务 thread 为零”。

## 执行与重验证

```text
扫描并生成计划
  → 用户明确选择动作
  → 展示根 thread 及关联任务 thread 的名称、项目、完整目录、子代理身份和 frontend reference 残留影响
  → TTY 输入明确确认词，或 --yes 跳过该提示
  → 重新扫描并比较 action ID + 快照指纹
  → 按保存位置启动对应 Codex app-server
  → 捕获关联任务 thread ID、列表中存在的 thread ID、当前 rollout 路径、thread 元数据及逐文件身份/来源/stat 指纹
  → 与每个 root 的已批准精确范围完全比较
  → 每次请求前再次扫描所有活动前端并验证动作快照
  → thread/delete
  → 无论请求成功、错误或超时都执行磁盘、列表和可探测 Desktop 宿主状态验证
```

只有 stdin 和 stdout 同时为 TTY 时才进入编号及最终确认流程。最终删除计划显示 action
ID，并完整展开每个根 thread 和将被级联删除的关联任务 thread；跨项目目录时会给出
醒目警告。用户输入 `确认删除` 后在同一进程继续，取消或 EOF 零修改。`--yes` 只跳过
最终确认提示，不能替代目标选择，也不能跳过重验证。非 TTY 的 `review/high` 风险动作
还必须提供当前完整 `--plan-fingerprint`。无人值守执行没有 `--thread-id` 或完整
`--action-id` 时会被拒绝。完整 ID 或前缀跨保存位置匹配多个目标时也会拒绝，不能
静默扩大范围。

`repair_legacy_index` 使用独立确认词 `客户端已关闭并确认修复`，非交互模式同时要求 `--clients-closed` 和计划指纹。它只能单独执行；`restore-legacy-index` 也要求客户端关闭，并仅在当前文件哈希仍等于该备份对应的修复结果时恢复。修复和还原都会先为将被覆盖的当前版本再创建一份可验证备份。

`remove_desktop_state` 也只能单独执行。它在每次写入前重新证明目标没有原生 index 或
有效 rollout，且恰好存在一条 `host_id='local'` 的已批准 catalog 行；发现多个 catalog、
schema 变化、全局状态哈希漂移或客户端仍运行都会停止。SQLite 和 JSON 修改共用一次
备份清单；写入后只要任一精确引用仍存在，就尝试自动还原全部宿主状态。

身份和文件范围均可验证的重复文件或路径错位允许作为 `high` 风险整个 thread 删除逐项
选择。残留关联记录只有在其指向的同 target 子 thread 仍有身份精确可验证的本地数据、
无 source/identity 冲突时，才可提供 `high` 风险整个 thread 删除。CLI 只对同 target
的 `duplicate_rollout`、`index_rollout_path_mismatch` 或该精确 `residual_spawn_edge`
Observation 传递窄授权；不能授权其他类型或关联任务 thread。residual 删除会删除这条
子 thread 及批准的关联任务范围，不是关系边修复；若子 thread 已不存在，只能显示尚未
实现的 `remove_broken_relation`。隔离、路径修复和关系修复执行器仍未实现。

官方接口优于直接操作存储，因为 Codex 自己负责活动/归档路径、列表记录，以及由该
thread 创建的关联任务 thread 的一致删除。API 语义见
[Codex app-server API 概览](https://learn.chatgpt.com/docs/app-server#api-overview)。

执行结果有四态：

- `deleted`：计划范围内所有已知目标均不存在；
- `not_deleted`：目标明确仍完整存在；
- `partial`：只有部分列表记录、文件或关联任务 thread 消失；
- `unknown`：无法完成可靠验证。

请求报错但验证证明全部消失时仍是 `deleted`，同时保留请求警告。

## 关键不变量

- 第三方前端数据库只读；不直接修改原生 `state_5.sqlite`，不直接删除 rollout 内容
  文件。写入例外只有经独立审批和备份保护的旧聚合索引整体替换，以及原生证据为空时
  精确限定的 Codex Desktop 私有宿主状态修复。
- 所有问题都显示候选动作；不可执行不等于不显示。
- 活跃引用、身份冲突、范围不明和不完整读取都会阻止相关删除动作。
- 不跨 Codex 数据目录合并目标；技术路径和完整 ID 在 JSON 中保真。
- 同一数据目录的可执行文件候选不一致时，规划层和执行层都 fail closed；用户必须用现存普通文件形式的 `--codex-bin PATH` 消除歧义。
- 用户明确选择的 `keep` 是成功的 no-op，不是执行失败。
- 删除计划、thread 名称/项目/子代理元数据、关联任务范围或活动 frontend reference
  漂移时停止执行；每个 API 请求前都重新检查。
- 删除请求的返回值不能替代删除后验证。
- Desktop catalog/UI 精确引用仍存在时，原生删除结果为 `partial`，不得用 app-server
  成功响应覆盖宿主残留证据。
- 不以“回收空间”为理由降低证据门槛。

## 已知设计风险

- TOCTOU：前端恢复、后台写入或新增关联记录会使旧计划失效。
- schema 演进：数据库不兼容必须显式失败，不能静默扩大匹配。
- 重复 ID 或多份内容文件：不能选择“最后扫描到的文件”作为依据；必须枚举并批准全部当前文件，残留时报告 `partial`。
- junction、symlink 和外部路径：未来隔离或修复执行器必须验证真实路径边界，并提供可恢复备份和审计。
