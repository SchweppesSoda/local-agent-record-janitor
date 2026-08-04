# 全量记录清单与选择性删除设计

状态：方案 review 已通过，可进入实现

## 1. 目标

在现有“扫描异常并保守清理”的能力之外，增加一条独立的人工管理路径：

- 列出指定 Codex 数据目录中的正常与异常对话；
- 将 Cindy、AionUI 中的 Codex 会话映射关联到对应 Codex 对话；
- 允许用户逐项选择任意仍有 Codex 本地数据的对话并永久删除；
- 保留现有 scan/clean 的异常修复语义和低风险批量规则，不把正常记录混进 clean。

这里的删除目标是 storage-qualified Codex thread，即
(codex_home, thread_id)。Cindy/AionUI 数据库中的行是来源与引用证据，
第一阶段只读展示，不直接改写第三方数据库。执行后若第三方仍保留引用，
输出必须明确报告该引用被保留以及可能形成前端孤立映射。

## 2. 上游接入方式研究

### 2.1 Codex

Codex app-server 是官方本地协议入口。完成 initialize/initialized 握手后：

- thread/list 可分页列出活动或归档的已存储 thread；
- thread/read 可只读取得一个 thread 摘要；
- thread/delete 永久删除活动或归档 thread，并级联删除它创建的后代 thread。

清单不能只依赖 thread/list：它默认筛选交互来源，而且仅靠正常索引会漏掉
rollout-only、legacy-index-only 等本项目需要展示的记录。因此清单使用本地
SQLite、rollout 与旧索引的只读并集；实际删除仍只调用官方 thread/delete。

官方资料：

- https://developers.openai.com/codex/app-server/

### 2.2 Cindy

当前 Cindy 不再通过旧 Codex SDK 管理会话，而是以绝对二进制路径启动
codex app-server，通过 stdio NDJSON 调用 thread/start、thread/resume 等方法。
一个 Cindy maker session 对应一个 Codex thread。

Cindy 本地 SQLite 的 sessions 表保存：

- sessions.id：Cindy 会话 ID；
- sessions.sdk_session_id：Codex thread ID；
- sessions.agent_kind='codex'：Codex 后端；
- sessions.status：active、archived、deleted 等 Cindy 生命周期状态；
- title、working_dir、created_at、updated_at 等展示信息。

当前桌面端为不同用户打开 cindy-<userId>.db；旧品牌还可能存在
xdt-maker-<userId>.db 或较早固定文件。当前 Cindy 使用独立
<userData>/codex-home。Global/CN/Dev/旧品牌的 Windows userData 候选分别是
CindyGlobal、Cindy、CindyDev、xdt-maker。

主要上游证据：

- https://github.com/makecindy/cindy/blob/main/packages/maker-core/src/agents/codex/app-server/stdioTransport.ts
- https://github.com/makecindy/cindy/blob/main/packages/maker-core/src/agents/codex/app-server/host.ts
- https://github.com/makecindy/cindy/blob/main/apps/desktop/src/main/maker-host/session-storage.ts
- https://github.com/makecindy/cindy/blob/main/apps/desktop/src/main/localDb/index.ts
- https://github.com/makecindy/cindy/blob/main/packages/maker-shared/src/brandIdentity.ts

### 2.3 AionUI

当前 AionUI 桌面端启动捆绑的 AionCore。AionCore 作为 ACP client 启动受管的
Codex ACP adapter；当前迁移将内置 Codex adapter 固定到
@agentclientprotocol/codex-acp@1.1.2。外部 codex CLI 负责自身认证与配置。

AionCore SQLite 的 acp_session 表保存：

- conversation_id：AionUI 对话 ID；
- agent_backend='codex'：当前 schema 中的直接后端标识；
- agent_id/agent_source：Agent 定义；
- session_id：ACP 下游分配并用于恢复的 Codex session/thread ID；
- session_status、last_active_at：前端生命周期与时间。

Codex ACP adapter 的 newSession 将 thread/start 返回的 response.thread.id
原样作为 ACP sessionId，resumeSession 又把该 sessionId 原样传给
thread/resume。因此 AionCore 持久化的 session_id 对内置 Codex backend
就是 Codex thread ID，不是只能展示而无法反查的另一层随机 ID。

当前数据库位于 <Electron userData>/aionui/aionui.db。较老版本曾使用其他文件名，
包括本项目原默认值 aionui-backend.db。当前 schema 可直接读取
acp_session.agent_backend；旧 schema 才需要通过 agent_metadata.backend 关联，
且关联键在版本间可能是 agent_metadata.id 或 agent_metadata.agent_id。

主要上游证据：

- https://github.com/iOfficeAI/AionUi/wiki/ACP-Setup
- https://github.com/iOfficeAI/AionUi/blob/main/packages/desktop/src/process/utils/utils.ts
- https://github.com/iOfficeAI/AionUi/blob/main/packages/desktop/src/process/services/database/runLegacyDatabaseMigrations.ts
- https://github.com/iOfficeAI/AionCore/blob/main/crates/aionui-db/migrations/001_initial_schema.sql
- https://github.com/iOfficeAI/AionCore/blob/main/crates/aionui-db/migrations/020_update_codex_acp_package_scope.sql
- https://github.com/iOfficeAI/AionCore/blob/main/crates/aionui-db/src/models/acp_session.rs
- https://github.com/agentclientprotocol/codex-acp/blob/main/src/CodexAcpClient.ts

接入链路：

~~~text
Cindy session ── sdk_session_id ──> Cindy codex-home / Codex thread

AionUI conversation
  └─ AionCore acp_session.session_id
       └─ managed Codex ACP adapter
            └─ native CODEX_HOME / Codex thread
~~~

## 3. 用户接口

增加两个独立子命令：

~~~text
local-agent-record-janitor records [通用来源/路径参数]
local-agent-record-janitor delete  [通用来源/路径参数] [选择与确认参数]
~~~

records 始终只读：

- 默认聚合 native、Cindy、AionUI；
- 支持 --platform、--thread-id、--limit 和 --json；
- 人类输出按 Codex 数据目录分组，显示名称、项目、完整 ID、状态、来源、
  Cindy/AionUI 引用、索引/rollout/归档信息和级联后代；
- JSON 输出完整、稳定且不受 --limit 截断。

平台视图中，Cindy 当前使用独立 codex-home，因此该 storage 中未保留 SQLite
映射的残留 thread 仍归入 Cindy；AionUI 与原生 Codex 共享 home，只有明确的
AionUI 前端映射才能用于平台归属，避免把全部原生记录误标成 AionUI。

delete 是高风险人工管理：

- TTY 且无选择器时先显示同一清单，再接受编号/范围；
- 不支持 all，任何正常记录都必须逐项选择；
- 支持完整稳定 action ID，thread ID 或唯一前缀；
- --yes 只跳过最终提示，不能代替目标选择；
- 非 TTY 执行必须同时提供明确目标、刚才预览得到的 plan fingerprint、
  --clients-closed 和 --yes；
- TTY 执行要求输入专用确认句“客户端已关闭并确认永久删除”；
- 只预览不修改时沿用退出码 2；执行或验证失败返回 1。

## 4. 数据模型与公共接口

新增 inventory.py，提供：

- FrontendSessionRecord：平台行的只读快照；
- ManagedConversation：一个 (codex_home, thread_id) 目标及其 Codex 摘要、
  artifact、前端引用、后代、二进制提示与 blocker；
- SessionCatalog.unmapped_frontend_sessions：session/thread ID 为空或无效、
  因而不能关联到 Codex thread 的前端记录；它们仍会被 records 展示，但不能
  生成删除 action；
- InventoryFailure：单一来源的失败，不吞掉其余来源结果；
- SessionCatalog：完整记录和错误，可序列化；
- build_session_catalog(adapters)：构造只读清单；
- select_managed_conversations(...)：按 action ID/thread selector 唯一选择。

FrontendAdapter 增加非抽象 list_sessions()，默认返回空列表，避免破坏现有
第三方/测试 adapter。CindyAdapter 与 AionUIAdapter 实现它；native adapter
由 Codex 本地并集自然产生记录。

新增 manual_delete.py，提供：

- ManualDeleteAction：稳定 action ID、根目标、完整 affected thread 集合、
  前端引用提示、精确原生 scope；
- ManualDeletePlan：actions、errors；with_selected_actions(...) 生成只包含
  明确选择根目标的批准计划及 plan fingerprint；
- build_manual_delete_plan(catalog)；
- execute_manual_delete(actions, ...)：转换为现有 cleaner 的 Finding 和
  ExpectedDeletionScope，复用 app-server 握手、级联检查、精确 scope 比对、
  删除后四态验证。

稳定 action ID 只绑定规范化 codex_home 与完整 thread_id。路径规范化采用
expanduser + absolute + normpath + normcase，并在可解析时保留 resolved path，
避免同一 Windows 路径仅因大小写或分隔符生成两个目标。plan fingerprint
还绑定所选根及后代、ConversationSummary fingerprint、索引 ID、
rollout 路径及内容/stat fingerprint、全部前端引用快照、二进制提示。前端引用
快照至少绑定平台、数据库绝对路径、平台会话 ID、thread ID、backend、status、
updated/last-active 时间和是否 live；标题只用于显示，不作为唯一身份。

## 5. 清单算法

1. 对每个 adapter 只读取得全部 Codex 前端记录，保留 active、archived、
   deleted、其他未知状态，以及 session/thread ID 尚未分配的记录。
2. 按 codex_home 分组，并读取以下并集：
   - state_5.sqlite threads；
   - sessions 与 archived_sessions 下可验证的 session_meta；
   - session_index.jsonl；
   - 前端映射中的非空 thread ID。
3. 对并集调用现有 ConversationSummary 合并逻辑，只读取 rollout 第一行，
   不扫描聊天正文。
4. 将前端行关联到相同 (codex_home, thread_id)，相同 thread 不重复显示。
5. 读取完整 spawn edge/rollout source 关系并计算传递后代集合。
6. 仅有前端引用、已无任何 Codex artifact 的行仍进入清单，但标记为
   frontend-only，不生成 thread/delete action。legacy index 本身也不足以证明
   thread/delete 可执行；只有 SQLite thread 行或可验证 rollout 至少存在一项，
   才能生成 action。
7. session/thread ID 未分配的前端记录进入独立 unmapped 列表。
8. 同一 thread ID 位于不同 codex_home 时始终是不同目标。
9. 单一 Codex home 的 state DB、rollout 身份或级联关系读取失败时，records
   仍报告可取得的数据与结构化错误，但该 home 的全部 delete action 都标记为
   不可执行；不能以不完整清单继续删除。
10. 无法完整取得级联后代证据时显示 cascade_unknown，并阻止生成可执行 action。

## 6. 当前与旧版本发现

自动发现优先兼容当前布局，同时保留显式路径覆盖：

- AionUI：优先 aionui/aionui.db，并读取存在的旧 aionui-backend.db；
- Cindy：检查 CindyGlobal、Cindy、CindyDev、xdt-maker，读取匹配
  cindy-*.db、xdt-maker-*.db 和已知早期固定数据库；忽略 wal/shm/backup；
- Cindy 的每个 profile 使用同目录 codex-home；
- 若已知 Cindy profile 的数据库已移除但 codex-home 仍存在，仍将该 storage
  纳入原生清单，只是不再声称有可读取的前端映射；
- AionUI 默认关联显式 CODEX_HOME 或 ~/.codex；
- 多个数据库或保存位置分别保留来源，不以 thread ID 跨目录合并。

AionUI adapter 的现有异常扫描也同步改为动态 schema 解析，支持当前
acp_session.agent_backend + agent_metadata.id，以及旧 metadata 关联形式。

## 7. 删除执行

1. 构造完整 catalog 和删除 action。
2. 用户明确选择根目标。
3. 展示最终计划：每个根、所有级联后代、前端引用保留提示、完整路径与 ID。
4. 生成绑定所选范围的 plan fingerprint。
5. 要求客户端关闭确认；未确认则零修改。
6. 执行前重建 catalog，并对所选 action 与 fingerprint 做精确比对。
7. 拒绝根/后代互相包含或多个根共享后代的计划。
8. 按 codex_home 选择唯一可信 Codex 二进制。
9. 复用 cleaner，在 app-server 启动后、首个请求前再次捕获原生 scope；
   每个请求前用 manual delete 专用 validator 精确比较前端引用快照。它不会
   调用 clean 的“发现 live 引用即阻止异常修复”validator，因为此命令正是允许
   用户在关闭客户端并逐项确认后删除正常且仍被引用的 Codex thread。
10. 调用 thread/delete。
11. 验证根与批准后代的 SQLite、活动/归档 rollout 是否全部消失，报告
    deleted、not_deleted、partial 或 unknown。
12. 输出仍保留的 Cindy/AionUI 行；本功能不把它们误报为已删除。

## 8. 并行实现边界

- 子任务 A：inventory 模型、adapter 全量读取、当前/旧路径与 schema 兼容、
  对应单元测试。
- 子任务 B：manual delete plan、fingerprint、cleaner 适配与对抗性测试。
- 子任务 C：records/delete CLI、人类/JSON 输出、README 与 CLI 测试。

三个子任务以本文件声明的公共接口为契约，尽量不交叉修改同一文件。主代理负责
接口整合、全量测试、真实临时目录验收和最终安全 review。

## 9. 方案 review

Review 日期：2026-07-31。

Review 门槛：

1. 需求覆盖：正常/异常 Codex thread 与 Cindy/AionUI 映射均可见、可逐项选。
2. 当前上游兼容：接入链路、数据库布局和当前/旧 schema 均有源码依据。
3. 只读完整性：清单不启动删除、不读聊天正文、不因单一失败静默漏项。
4. 删除安全：永久性、级联、前端残留、并发漂移和自动化误选均 fail-closed。
5. 可测试与可并行：公共接口明确，三块实现可用隔离 fixture 独立验证。

初版 review 发现并修正了五个问题：

- 初版未展示尚未分配 session ID 的前端记录；加入 unmapped 清单。
- 初版未明确 legacy-index-only 是否可删除；修订为不可执行。
- 初版未规定级联/schema 清单不完整时的 home 级阻断；加入 storage fail-closed。
- 初版的 plan fingerprint 在全量 catalog 与所选范围之间语义不清；改为选择后
  的批准计划 fingerprint。
- 初版未区分 clean 的 live-reference blocker 与人工正常记录删除；规定专用
  精确快照 validator，同时保留客户端关闭和逐项确认。

Review 结论：五项门槛全部通过。实现不得扩展为直接 DELETE 第三方 SQLite 行，
不得加入 all 批量删除，也不得在 cascade_unknown 或 storage inventory error
存在时绕过阻断。

## 10. 实施后验收 review

主代理在三个子任务冻结后独立 review 了整合结果，额外发现并修正四类问题：

- records 与 delete 曾生成不同的 action ID；统一为直接复用
  ManagedConversation.action_id，并增加跨命令真实类型测试。
- delete 的 JSON 执行路径曾可能连续输出预览和结果两个 JSON 文档；改为每次调用
  只输出一个完整文档，并覆盖预览、错误和成功路径。
- 单平台来源过滤曾同时裁掉删除 guard；改为“视图过滤”和“完整安全盘点”分离，
  delete 始终保留全部已发现 adapter，只有可选目标按平台过滤。
- 可选文件探测和 rollout 递归遍历曾可能把权限/路径错误当成“不存在”；改为
  严格 stat + 带 onerror 的遍历，删除后验证也先重建完整清单。任何无法验证的
  状态均阻止请求或返回 unknown。

最终独立验收包含真实临时 CODEX_HOME 的只读快照比较、跨命令 action ID、
平台视图、级联重叠、计划/前端漂移、非法路径与权限错误测试。全量结果为
全量共 295 项，其中 294 项通过，1 项因平台条件跳过。实施后 review 结论：通过。
