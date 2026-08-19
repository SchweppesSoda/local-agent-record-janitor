# 本地 Agent 记录清理模型与安全边界

本文档描述 0.2.0 的当前实现。项目只负责找出失效或用户明确选择的本地 Agent
记录，展示精确影响，永久删除，再确认目标已消失。它不是恢复平台、长期备份库、
rollout 隔离区或自动数据修复器。

## 统一执行架构

```text
Driver
  → StoreSnapshot
  → Planner
  → AuthorizedAction
  → Guard
  → Executor
  → Verifier
```

人类入口 `scan/records/delete/clean/purge` 和 Agent 入口
`agent doctor/plan/apply/status/verify` 都调用同一个 `CleanupService`。CLI 负责参数、
确认和输出，不拥有另一套扫描或删除规则。

核心类型分工：

- `StorageRef`：一个精确物理存储；
- `RecordRef`：该存储中的一条逻辑记录；
- `Evidence`：不含聊天正文的身份、状态、schema 和指纹证据；
- `Action`：稳定 action ID、目标、影响与 mutation family；
- `GuardToken`：执行前重验证所得的不可变授权绑定；
- `Result`：结构化状态、计数、blocker code 与验证结果。

兼容 facade 继续输出既有 JSON 字段和 `delete_conversation`、
`repair_legacy_index` 等 action kind。自由文本只用于显示，不能参与授权决定。

## 身份与物理边界

```text
Frontend namespace -- reference/mapping --> Native record
Harness runtime -- operates on --> Native store -- contains --> Native record
```

Codex thread 的身份是 `(CODEX_HOME, thread_id)`；Pi session 和 Claude session
还绑定各自的 session/config root。相同 ID 出现在不同物理存储时必须拆成不同目标。
认证来源、账号、登录状态和前端 owner 只属于诊断信息，不能替代物理存储身份。

一份授权计划只处理一个物理存储和一种 mutation family。前一批完成后必须重新扫描，
新发现的动作不会被吸收到旧计划中。

## 快照与计划

一次完整快照聚合：

- Codex SQLite 列表、活动/归档 rollout 首行 metadata 和旧索引；
- 精确关系边与完整关联任务范围；
- Codex Desktop 可探测的 local catalog/UI 结构化引用；
- AionUI/Cindy 当前与历史 frontend reference；
- Pi/Claude storage-qualified session manifest；
- 读取失败、进程归属和 identity conflict。

扫描只读取判定和显示所需的最小结构化字段，不读取或输出聊天正文。Planner 为每个
action 生成完整目标、影响范围、schema/行/文件指纹、稳定 ID 和 blocker code。
活动引用、读取失败、身份冲突或无法唯一定位只阻止受影响目标。

## 问题与动作

| 问题 | 当前动作 |
|---|---|
| 前端已删除、底层会话仍在 | 删除整条底层会话；前端残留作为下一独立动作 |
| index-only、rollout-only | 删除整条 Codex thread |
| 重复 rollout、路径错位 | 保留，或删除整条 thread 及全部已确认副本 |
| 孤立关联任务 thread | 展示完整级联范围后删除 |
| 只剩无效关系边 | 精确删除一条已批准关系行 |
| 只剩旧索引/Desktop 状态 | 精确清除残留 |
| 只剩 Cindy/AionUI 映射 | 精确清除该映射 |
| Pi/Claude session | 删除精确文件或 manifest |
| 同一 ID 位于多个存储 | 每个物理存储分别选择 |
| 活动引用、读取失败、身份冲突 | 阻止该目标并返回稳定原因 |

`repair_index_path` 和 `quarantine_artifacts` 不再生成或提供。对应枚举只为读取旧
JSON 保留；当前决定只有“保留”或“删除整条已验证记录”。

## 精确写入器

### Codex thread

原生 thread 删除调用匹配目标 `CODEX_HOME` 的官方 `thread/delete`。同一批只启动
一个 app-server。若官方 API 已删除 thread 但留下已批准 rollout，只有在路径位于目标
store、是唯一链接的普通文件、thread ID 与完整指纹仍匹配且已无 index/活动引用时，
才能作为同一动作的残留清除。任何未知残留使结果成为 `partial` 或 `unknown`。

### AionUI

只删除孤立 `acp_session` 行。当前 schema 使用完整主键；无主键的旧 schema 仅接受
执行前重新验证的 `rowid + 完整行指纹`。无法唯一定位、schema 漂移、存在 trigger
或影响行数不是 1 时阻止该条。

### Cindy

- 当前引用：只把目标 session 的 `sdk_session_id` 清为 `NULL`；
- 历史引用：只从绑定消息 ID、消息行指纹和原始内容哈希的结构化
  `agent_switch` JSON 中移除 `fromSdkSessionId`。

历史写入保留消息行和其他 JSON 字段；重复 key、内容漂移或字段值不符都会阻止。

### Codex 关系边

只删除 parent、child、status、schema fingerprint 与完整 row fingerprint 全部匹配的
一条 `thread_spawn_edges` 行。open edge、重复身份行、未知 schema 或不完整证据都
fail closed。

### 旧索引与 Desktop 状态

旧索引只移除已证明没有 live thread 的原始整行。Desktop 写入只删除
`host_id='local'` 的精确 catalog 行和结构化精确 ID 引用；正文中的相同字符串不参与
匹配。

所有共享 SQLite/JSON 写入都要求：客户端关闭、schema 白名单、事务或原子替换、
影响范围等于预期、写后重新读取，以及临时回滚副本。

## 客户端归属

`ClientInspector` 由服务注入，默认单元测试不读取真实宿主进程。生产检查综合可执行
文件、父子进程和物理 store 归属。已证明属于官方 Codex 的进程不阻止独立 Cindy
store；Cindy 进程也不阻止已证明独立的官方 store。无法证明归属时才 fail closed。

## 执行、验证与性能

`plan` 做一次完整快照；`apply` 做一次完整预检和一次完整终检。N 个 action
之间只重查本 action 涉及的数据库行、文件、活动引用和关联范围：

```text
一次完整扫描 + N 次定点检查 + 一次完整终检
```

中途漂移立即停止剩余动作，不在每个 action 前重扫全库。性能测试直接统计 full-scan
调用次数；100 个 action 仍必须只有两次完整扫描。

执行结果采用 `deleted/not_deleted/partial/unknown` 或 Agent
`complete/completed_with_residuals/blocked/unknown`。API 成功响应不能代替磁盘、
数据库和完整目标终检。

## 回滚副本与 operation 数据

Codex/Pi/Claude 会话和独立 rollout 永久删除，不创建备份。共享 SQLite、旧索引或
Desktop JSON 写入前创建临时回滚副本：

- 写入并验证成功：立即删除；
- 自动回滚成功：立即删除；
- partial、unknown 或回滚失败：暂时保留，直到 verify 收口。

执行中或结果未知时保留详细 operation journal。已知终态压缩为不含正文的最小
`receipt.json`，只记录 plan hash、action 状态、blocker code、计数和时间；最长
七天后自动清除。回执不是备份，项目不提供 recover 命令。

## 关键不变量

- 所有计划、回执和输出都不含聊天正文；
- 不跨物理存储合并目标；
- schema、身份、范围或客户端归属不确定时只阻止相关目标；
- `--yes` 不能代替目标选择、客户端关闭或当前计划指纹；
- 同一动作在结果不明时不得重复发送；
- 删除成功必须由重新读取和完整终检证明；
- 不以节省空间为理由降低证据门槛。
