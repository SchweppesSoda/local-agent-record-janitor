# Adapter 贡献指南

接入一个客户端不等于接入一个引擎。客户端可以运行多个引擎、使用多个 profile，
也可以与独立 CLI 共用存储。适配器只提供已验证的发现和归属证据；写入必须交给对应
引擎或 frontend mutation family 的独立执行器。不能因为新客户端也使用 Codex，
就继承 Cindy 的目录、引用、进程归属或删除规则。

## 通用接入契约

| 角色 | 必须提供的证据 | 不得推导的结论 |
|---|---|---|
| 客户端发现与引用 | client/profile、数据库或状态文件、原始 backend、current/history/restore 引用及稳定定位 | 无当前 UI 引用不等于孤儿；显示 ID 不能跨数据库去重 |
| 引擎清单 | storage-qualified native identity、完整性、关系类型、文件/index/manifest、逐记录 blockers | 同名 ID 不等于同一记录；有 parent 不等于级联删除 |
| 运行归属 | 真实写入方、owner process root、进程树、host/runtime | 窗口关闭不等于后台 writer 已停止；native root 不等于客户端 profile root |
| 执行与验证 | 注册的 mutation family、冻结范围/指纹、漂移检查、未知结果恢复和最终验证 | 能盘点不等于能删除；单个路径消失不等于所有副本或引用清空 |

本库已有接口包括 `list_sessions`/`snapshot_sessions`、`native_catalog_for`、
`registered_capability` 与 `owner_process_root`。`inventory_engines` 声明默认需要
检查的原生后端，避免前端引用全删后漏掉原生残留。多个 profile 共用 native root 时，
可以通过 `native_catalog_group` 合并引用证据后一次构建清单。共享 root 中无该客户端
归属证据的独立会话不能归入该客户端的项目删除范围。

默认发现仍由显式代码注册，尚不是可动态加载任意客户端的插件系统。
下面的 Codex `Finding` 接口是既有 compatibility adapter，不是所有引擎的必选接口。

### 现有组合与兼容基线

| 客户端与引擎 | 已有写入路径 | 必要限制 |
|---|---|---|
| Cindy / Codex | 原生 `thread/delete`、精确 current/history 引用、软删除 session 行 | 正常原生父链保留；独立批次及关闭、范围重验证仍必需 |
| Cindy / Pi | 精确 JSONL、精确引用、软删除 session 行 | 每个 profile 单独定位；`parentSession` 不级联 |
| Cindy / Claude | session manifest、精确引用、软删除 session 行 | 共享 root 聚合所有 profile 引用；无 Cindy 归属的独立 session 不纳入 Cindy 删除范围 |
| AionUI / Codex | 精确 ACP 引用及支持 schema 的孤立 project 行 | 原生写入仍须独立证明归属和 writer；未知 schema 只读 |
| AionUI / Pi、Claude | 精确 ACP 引用 | 原生 root 未证明，不提供 native writer |
| native / Codex、Pi、Claude | 对应引擎专用 writer | 无前端行的独立记录正常；每个物理 root 分别批准 |
| Orca / Codex | journal 引用与已证明 homes 的原生清单；限定 Windows 原生删除 | 静态能力关闭，精确 v3 operation 逐目标资格见下文；未知来源保持 incomplete |
| Orca / Claude、其他引擎 | 只读引用与来源错误 | 本机 Claude account root 仅用于精确保护，不提供 Orca native catalog/writer |
| Herdr / Codex、Claude、Pi、未知 backend | 持久化 current/restore；显式 live metadata | rootless；全部 writer 归属未知，全部写入及自身 verify 关闭 |
| WorkBuddy / WorkBuddy | 独立 SQLite、精确会话 artifacts 和 UI 引用 | 仅支持已识别 5.6.2 本地终态范围，Windows writer 覆盖及完整冻结证据；未知副本只读 |
| 千问办公 / QwenWork CN | 主/子会话元数据及已知 SDK 文件位置 | 已核实 1.2.5 CN schema；草稿/恢复/共享 SDK 范围未闭合，写入和 verify 关闭 |
| QoderWork CN | 主/子会话元数据及已知 SDK 文件位置 | 已核实 0.9.18 CN schema；不与 Qoder IDE/CLI 混用，写入和 verify 关闭 |
| Paseo / 内置及未知 provider | 当前/旧格式 agent 快照、归档与恢复引用 | 固定源码的只读注册清单；无 native root/完整 writer 证明，写入和 verify 关闭 |
| 未识别 backend | 清单 | 原始名称保留，不继承已知引擎 writer |

兼容回归使用[固定 v1 样本](../tests/fixtures/operation_v1.json)及
[持久化 reader 测试](../tests/test_plan_compatibility.py)，覆盖未执行、mutation
已经开始、unknown、partial 和终态回执。路径由测试重定位到临时存储；生产 reader
保留旧计划原文与 hash，status/verify 不重发 mutation。新的成功场景使用独立临时
存储，不能用一个 fresh operation 的成功代替旧 unknown operation 的收口。
固定 child plan/state/receipt 同时重绑定全部存储引用，使用新的 coordinator 实例执行
status/verify 并验证 apply 不重发；兼容 reader 不修改旧授权或 hash。

### 类型化清单入口

`client_contracts.py` 提供 `ClientDescriptor`、`ClientAdapter`、`ReferenceSnapshot`、
`ClientReference` 和 `RelationEvidence`。`describe_client()` 的 client 是选择依据；
只读客户端可以仅提供真实元数据来源与引用，不需要继承 `FrontendAdapter` 或伪造
`database`、`codex_home`。旧适配器继续通过 facade 提供相同信息。来源错误与原生
catalog 错误分别保留。

descriptor 必须在构造本机路径和 `StoreKey` 前检查 host/path namespace；当前仅接受
local。远端引用可以保留不透明 locator，但不能携带本机 native identity，也不会
触发本机 catalog。未证明 native root 的引用输出 `unverified`；已精确限定 engine、
store、ID（Pi 还需文件路径）的引用才能绑定已存在的 native 清单项。共享 Claude
root 由现有 catalog 提供真实绑定，descriptor 不把候选 `claude-home` 当成共享 root。

`capability_limit_for()` 表达 adapter/profile 的实现上限；当前 schema、归属和记录
blocker 继续收紧每个目标。engine context 的 capability 是汇总，不能覆盖具体目标的
限制。新引用和关系字段是清单展示投影，既有身份、approval payload 和冻结计划 hash
格式继续由原写入契约负责；这些投影本身不扩大写入范围。

能力上限贯穿旧 scan/purge、manual delete、服务执行和 operation。现有 mutation
family 在实际 writer 分派前按精确 metadata source 或 native store 再检查；前端
引用按冻结的真实 backend 证据检查全部受影响引擎，不使用 Codex 默认值放宽 Pi/Claude
限制。明确选择只读目标返回 blocked，独立可写 profile 保留原计划行为。

候选 adapter 与保护 adapter 分开：只有所选客户端的 adapters 提供 native/catalog
候选，全部已知保护 adapter 继续约束对应 store。`SessionCatalog.active_adapters` 和
`ManualDeletePlan.active_adapters` 是运行期绑定，不进入旧 JSON、approval 或 hash；
plan/apply、fresh rebind、terminal、manual/GUI 和直接服务执行均保留这些保护。
`build_session_catalog(..., guard_adapters=...)` 不把 guard-only home 加入候选；同一已选
home 的旧前端行仍可作为原有审批证据。纯类型化只读客户端走公共清单，明确选择删除
返回 `client_capability_limit`，不要求假的 `scan()` 或 native home。

`TargetedReferenceGuard` 当前为 Codex native store 刷新类型化引用，只检查精确 store
和已批准的 ID/后代，
不重建全库或发现新的授权目标。descriptor 的 stores 与 snapshot 中的 qualified 引用
或 `SourceFailure.store` 都可提供相关性；裸 ID、远端和 rootless 引用不补默认 root。
current/history/restore 的持久化引用阻止相关删除；只有证据完整且生命周期明确为
deleted 的 current/history 引用可释放，unknown/restorable 不据此释放。明确限定 store
的来源错误只影响该 store，未限定 store 的 profile 覆盖缺口仍影响该 profile 的选择；
`--record-id` 不会隐藏相关来源失败。
Pi/Claude 继续由既有专用引用/manifest 检查和公共能力上限保护，尚未引入同样的类型化
fresh-reference 执行契约。

新的 adapter `verify=False` 表示其自身尚无完整终验契约，仍允许旧 native v1 operation
执行只读 status/verify。恢复只依据原冻结范围与当前可信事实；证据不足保持 unknown，
不会重发 mutation 或改写原计划。

### 文件别名与运行观察

`file_alias_evidence.py` 的 `probe_file_aliases()` 接受已发现的本机路径与明确 roots，
提供 `FileAliasSnapshot`/`FileAliasEvidence`：观察时间、词法/解析路径、readlink、
device/file ID、nlink、已知路径与错误。host/namespace 在构造 `Path` 前检查；UNC、
foreign-OS、范围外及断链 locator 保持不透明或 incomplete。只允许范围内的 leaf
symlink，目录链接和身份变化不能视为完整观察；不读取内容或另行发现副本。
`readlink` 保留系统返回的原始目标；Windows 本机盘符的 `\\?\` 路径仅在 root
两种拼写的目录身份一致时共享范围边界，UNC/设备路径仍不跟随。

`collect_client_file_aliases()` 仅提取选中 native 清单项的 rollout、Pi JSONL 或 Claude
manifest 文件。`records --client ...` 将结果作为独立 `file_aliases` 输出，不加入旧
target、snapshot_id 或 v1 approval/hash。相同 file ID 只关联观察证据，不合并不同
`StoreKey`。普通/extended/8.3 spelling 不重复算目录项；nlink 匹配仅描述观察时点的
已知 hardlink 名称，`alias_coverage_complete` 始终为 false，不证明所有 symlink/copy
已发现。若以后用这些新证据改变 writer 授权，必须按计划兼容规则另行版本演进。

`collect_shared_store_file_aliases()` 为选中目标与 `contexts.native_records` 精确匹配
的 Codex store 提供独立 `shared_store_file_aliases`。每个逻辑 store 只观察固定的
`state_5.sqlite`、`state_5.sqlite-wal/-shm/-journal` 和 `session_index.jsonl`，一次
聚合探测关联已选路径中的硬链接证据；输出保留 store 的 backend/kind/词法路径、
`shared_native_store` 范围与 database/wal/shm/rollback_journal/index 角色。
共享文件不成为记录的专属 artifact，不改变 record count、groups、能力、旧
`file_aliases` 字段或 snapshot/plan hash。仅前端引用、rootless/未知引擎或没有匹配
native catalog 的目标不会据 locator 探测原生存储。

探测前验证普通 root/父链；`omit_initially_missing=True` 仅在初次 leaf lstat 缺失且
父链重检成功时省略可选文件。断链、已有文件后续消失、权限失败、非普通文件、
目录链接和身份变化仍是 incomplete。目录缓存、访问集合和范围比较使用严格词法
字符串，避免 Windows 大小写敏感目录被 `Path` 相等规则合并；物理别名仍须实际文件
证据。该投影不读内容、不枚举其他文件或读取配置，`alias_coverage_complete=false`、
`sqlite_home_and_api_storage_coverage=not_probed`；观察成功不证明外部 SQLite home、
副本或实际 API 修改范围完整。

`inspect_client_ownership()` 和 `--inspect-clients` 使用 descriptor 的真实 owner root，
不从 native root 补造归属。`probe_complete` 表示枚举/探测成功，`coverage_complete`
仅在支持的本机 Windows owner、Codex engine 和具备必要 metadata 的已注册进程范围
内成立；仍不证明任意其他 writer 已停止。collector 当前只枚举 Codex/ChatGPT/AionUI/
Cindy 四类 exe，复用 Cindy 的真实进程树/profile 证据。未知 owner、Pi/Claude runtime、
非 Windows 或失败返回 unknown；明确的相关运行进程仍可返回 false。输出不含 command
line。该观察不替代现有关闭 ack，也不修改既有 writer 的关闭检查。

Herdr 的显式检查复用 adapter 同次缓存的 metadata API 观察，见下文；不使用上述
四类进程的空枚举证明 Herdr 关闭。运行投影在 `snapshot_id` 之外输出，不改变旧审批。

### 同 root 的 mutation 协调

`mutation_guard.py` 在同一受信本地物理 root 的既有 `operations` 下读取可信的
legacy/child journal。unknown 占用被冻结的 native/frontend ID 和精确文件路径共同
定位；更换 operation ID 或 plan path 不绕过占用。文件路径按原始 root anchor 的
词法相对位置映射到已证明的物理 root，保留已消失路径，不重新 resolve 目标文件。
未知 family、缺失路径/状态或损坏 journal 保守阻止相应 root。

永久 `.mutation.lock` 使用 OS 跨进程互斥，固定多 root 顺序，覆盖占用检查、持久化
checkpoint、writer 和终态发布；verify 及 receipt 清理也持同一锁。旧 `apply.lock`
仍意味着结果未知，新根锁不能证明旧 writer 已退出；不自动删锁或给 unknown 设置
TTL。已知未尝试的 blocked 可恢复，verify 确认 partial 后可为真实残留生成新计划。

manual、GUI 和直接服务入口使用相同 gate，阻止绕过已有 journal 的 unknown；这些
入口不因此新增持久化 journal，其新超时结果没有完整 operation 恢复保证。协调范围
仅限既有受信本地 root，不承诺不同 native roots、未证明桥接或未使用该锁的旧版本
之间全局互斥。接入方仍须提供完整 frozen footprint 和既有关闭、归属、终验契约。

### 身份、错误与能力

- 原生身份至少包含 engine、规范化 store 和完整 native ID；Pi 还包含精确 JSONL 路径。
- 前端绑定身份包含 client、数据库、前端 ID、engine、native target、引用种类和历史
  boundary。`cindy:ID` 只是兼容显示名；不能据此合并不同 profile 或 current/history。
- 所有 profile 都必须进入清单与重验证；异常不得被吞成空清单。可归属的错误限制到其
  store；无法定位的 builder 异常阻止该次操作，不能复用旧清单冒充成功。
- 原始 backend 未被客户端适配器验证时，只能 inventory-only。Cindy 用
  `unsupported:<原始 agent_kind>` 保留未知值，避免公共引擎别名意外恢复删除能力。
- `healthy`、`unreferenced` 和 `cleanup_eligible` 分别是分类、引用状态、可选择性；
  都不能替代 plan/apply 的范围和删除授权。
- 当前 host/path 模型只适用于本地执行。接入 WSL/SSH/远端存储前必须显式建模 host、
  路径命名空间和执行端，不能把远端路径直接交给本机 `Path`。

### 关系语义

| 证据 | 盘点含义 | 删除范围 |
|---|---|---|
| Codex spawn/source parent | 同 store 的父链和后代图 | 依据实际 runtime 的 thread/delete 契约冻结后代，并验证结果 |
| Pi `parentSession` | 分支来源文件路径 | 各 JSONL 独立批准；删除父文件不自动删除子文件 |
| Claude session 下 `subagents/` | session 专属 manifest 成员 | 与已批准 session manifest 一起处理；不把消息 `parentUuid` 当另一个 session |
| 前端 parent/pane/layout 关系 | UI、恢复或运行组织关系 | 没有已验证生命周期契约时只展示，不扩大 native 删除范围 |

### 接入验收

每个新客户端与引擎组合都要有临时存储用例覆盖：多 profile、同名 native/前端 ID、
跨引擎同 ID、无前端行但原生存在、current/history/restore 的交叉引用、共享 root、
不完整清单、未知 backend/别名、原生独立会话、并发漂移及 unknown 不重发。
同时验证完整副本范围、关系语义、运行方检查、精确选择、schema 变化和跨进程恢复。
没有 writer/运行归属/验证证据的组合保持 inventory-only；已知引擎名不能跳过验收。

## WorkBuddy：独立本地会话

WorkBuddy 不继承 Claude/Codex 身份或存储规则。其 `ClientAdapter` 使用独立
`workbuddy` client、engine 和 `(profile root, session UUID)` 身份；不同 profile 的
同一个 UUID 是两个目标。只有所选客户端或显式 `--workbuddy-root` 才发现该存储，
默认 root 为 `WORKBUDDY_CONFIG_DIR` 或 `~/.workbuddy`，显式 roots 替换默认值。
重复 roots 按规范化物理定位去重，不作为其他客户端的全局保护来源。

支持 schema 固定为 WorkBuddy 5.6.2 的 `workbuddy.db`，完整表、列、索引和 trigger
集合必须匹配[随包 schema 资源](../src/local_agent_record_janitor/workbuddy_schema_562.json)。
未知 schema、损坏或缺失数据库、链接路径、超出有界发现范围，以及无法解释的
会话来源都产生结构化错误。SQLite 用 URI `mode=ro` 盘点；只查询白名单 ID、cwd、
归属、状态和时间字段。共享库内容以临时 SQLite snapshot 的规范化文件指纹守护，
不把 title、prompt、配置内容或消息 cell 转为计划证据。

`delete_workbuddy_session` 可删除 terminal、`transport='local'` 且 origin 为 local
或空的已证明记录。项目/all-projects 默认只选 `deleted_at > 0` 的软删除行；完整
`--record-id` 可明确选择仍保留的终态会话，ID 前缀不匹配。全部批准 ID 在同一个
物理 root 中组成一个 batch，精确删除 `sessions` 和可选 `session_usage` 行，
清理下列已识别的记录专属文件与结构化 ID 引用：

- `projects/<cwd-slug>/<sid>.jsonl`、`<sid>.meta.json` 和验证为空的 `<sid>.quickask`；
- 同一目录已有 `<sid>.jsonl` 对应的 `<sid>.file-rollback.ndjson`，仅接受下面的 v1 元数据格式；
- `artifact-index/<sid>.json`、`file-tree-manifests/<sid>.json`、`media-index/<sid>.json`；
- `<user_id>/sidebar-list-snapshot.json` 的 `items[].id`，以及当前/旧版
  `storage/user-<uid>[-<environment>]/[global/]conversations.json` 的 `pinned[].id`。

独占 transcript 只流式 hash，不解析聊天内容；共享 JSON 只输出 ID、状态等 scalar 元数据，
重复 keys、未知结构或嵌套 metadata 会阻止重写。其他会话、配置、账户、插件、skills、
memory、workspace/session 工作产物、自动化定义和共享 content-addressed blobs 保留。
这些索引的删除不保证其可能引用的共享附件被清除。

已安装 5.6.2 的 `resources/app.asar.unpacked/cli/dist/codebuddy-lite-wb.mjs` 中，
`getFileBackupRecordPath` 将 transcript 文件名主干映射为 `.file-rollback.ndjson`；
`FileBackupRecordStore` 追加 `{v:1, requestId:string, commitSeq:integer}` 元数据。
writer 只在顶层 UUID transcript 与相邻 sidecar 配对、每行恰好包含上述键且通过
有界格式验证时删除该 sidecar。计划只保留格式、条数和文件指纹，不输出 requestId；
未知版本、重复键、额外键或缺失 transcript 均阻止删除。session 子目录和 flat
subagent 副本的完整闭包未证明；`agent-*`/parent 会话范围保持只读。
非空 `media-index-workspace` 和未理解的
根 `session-artifacts.json` 阻止该 root 的删除。关联的 edge-sync SQLite 会话映射、
cloud/未知 transport/origin 和自动化依赖阻止受影响目标。

`remove_workbuddy_ui_reference` 独立清理完整 ID 的本地置顶引用。它要求该 ID 没有
session/usage 行、会话 artifact、sidebar 引用、同步关联或自动化依赖，且引用只属于
一个用户和环境的当前 `global/conversations.json` 及其已知迁移来源。此规则来自
5.6.2 `app.asar` 的 `main/conversations.js`：`PinnedConversationsStore.setTop`/`reorder`
仅修改本地存储，当前 global 来源与旧环境/用户来源存在迁移关系。所有已发现的
匹配引用都冻结并精确移除，保留其他 ID 的 `groupKey` 和顺序；跨用户/环境引用
保持阻断。此动作不会 dispatch 原生 SQL DELETE，结果区分 `removed_ui_only_ids`
与 `deleted_session_ids`。仅置顶引用必须显式按完整 ID 选择，并与会话删除分别建立
新计划；混合选择返回 `workbuddy_mixed_mutation_families`。

其他 UI-only/孤立文件 ID 保留为 `unverified`；all-projects 报覆盖缺口，独立本地
exact-ID 不因无关 UI-only ID 被扩为全 root 删除。`remote_delete=false`，不发送
云端请求或声称云端已空。

执行需真实 `--clients-closed` 声明，并在 Windows 上用 bounded CIM metadata 核验
WorkBuddy、已知 vendor/supervisor/CLI、按安装路径或配置参数归属的 runtime、其
后代进程及 runtime registry PID。命令文本只提及 WorkBuddy 不证明 shell 是 writer；
缺字段、超时或无法归属保持 unknown。registry endpoint 不输出、不连接。

冻结证据包含整共享 DB/UI 的 before 与批准 batch 的确定 after 指纹、精确 rows、
文件集合、身份/hash 和受影响数量。获得 SQLite writer lock 后再次核验完整库指纹；
任何已选/未选内容漂移、链接或新增副本都会阻止写入。同库共享路径进入 mutation
footprint，unknown 会阻止另一个 SID 在该库上写入，但不扩大已批准 ID，也不阻止
独立 root。只为共享 DB/JSON 建短期 rollback，不保留 transcript 备份。

部分 unlink 或 DB commit 后 JSON/file 失败保持 unknown。恢复只读，不重发删除或
盲目恢复库；只有证明完整 before/after 才清理临时副本。缺失 backup 不能以“IDs 已
不存在”代替完整冻结 after 指纹。已经可信终验并清除 rollback 的 child 才可用精确
SID 残留复核，后续另一个合法会话删除不改变原有完成结论。相关契约与临时 fixture
见 [operation CLI](operation-cli.md#workbuddy-local-sessions)和
[WorkBuddy 回归](../tests/test_workbuddy.py)。

## Orca 与 Herdr 的接入边界

### Orca：已接入只读清单与保护

`OrcaAdapter` 是独立的类型化 metadata adapter，通过 `records --client orca`
使用公共清单。首版依据固定上游 revision
`efbf651c7bb2eec778daf1844f8228e70809ec9f`，接受
`agent-session-journal.db` 的 SQLite `user_version=4` 和 record `schemaVersion=2`。
验证 schema 与读取 metadata 位于同一读事务；只查询
`agent_session_records`、`agent_session_tabs` 和 `agent_session_store_meta`，拒绝用
VIEW 冒充这些表。依据：[数据库 schema](https://github.com/stablyai/orca/blob/efbf651c7bb2eec778daf1844f8228e70809ec9f/src/main/native-chat/agent-session-journal/journal-database-schema.ts)、
[record 契约](https://github.com/stablyai/orca/blob/efbf651c7bb2eec778daf1844f8228e70809ec9f/src/shared/agent-session-record.ts)。

每个合法 record 的 handle chain 全部保留，最后一项为 current，其余为 history；
record 不依赖当前 tab 是否存在。`session_tabs_recorded` 缺失时显式报告覆盖未知，
已记录的空 tabs 不清除 record 引用，也不证明进程已停。
Orca 的 fork/history 是 frontend 元数据，不变成原生 parent 边；正常 Codex 子记录
仍按原生父链证据分类。依据：[handle chain](https://github.com/stablyai/orca/blob/efbf651c7bb2eec778daf1844f8228e70809ec9f/src/shared/agent-session-provider-handle.ts)、
[tab 恢复状态](https://github.com/stablyai/orca/blob/efbf651c7bb2eec778daf1844f8228e70809ec9f/src/main/runtime/agent-session-record-rows.ts)。

发现限于默认、`ORCA_USER_DATA_PATH`、显式 `--orca-root`，以及已知 native home 的
精确 `codex-accounts/<id>/home` 布局和可观察 marker。账号归属还要求普通 marker 内容
与 ID 一致、canonical containment、普通 home/sessions 目录且不落入 system home。
runtime home 必须由已知 profile 中本机 `accountHome` 明确关联，目录名称不足以证明。
host 必须为 `local`、WSL distro 必须为空，foreign-OS/相对/远端 locator 不交给本机
catalog。未知或失败引用保留 opaque locator 与来源错误，不补默认 home。
依据：[默认路径](https://github.com/stablyai/orca/blob/efbf651c7bb2eec778daf1844f8228e70809ec9f/src/main/codex/codex-home-paths.ts)、
[账号归属](https://github.com/stablyai/orca/blob/efbf651c7bb2eec778daf1844f8228e70809ec9f/src/main/codex-accounts/host-codex-managed-home-ownership.ts)。

多个已证明 Codex homes 分别盘点并保留 store-qualified identity，多个 profile 共享
catalog pass。Orca 清单不读取 Codex Desktop 私有 sqlite/global-state；不同逻辑 store
不会因同 ID 或 hardlink 合并。文件别名仅为观察证据，尚无完整桥接图。本机合法
`CLAUDE_CONFIG_DIR` selector 仅用于精确 root 的只读保护，不遍历该 root 或声称 Orca
已支持 Claude 原生清单。静态 Orca 能力仍关闭；只有下述高层 operation 精确组合可获得
原生删除资格，frontend/remote 写入不开放。

已知但未实现的恢复来源只检查 presence 并报告 incomplete：退休的
`agent-sessions/agent-sessions.json[.bak]`、root/profile 下 `orca-data.json[.bak.1..5]`、
`profile-state.db` 及其 `-wal/-shm/-journal`、`agent-hooks/last-status.json`。
`agent-hooks` 目录存在时还报告 namespace 覆盖未实现；`orca-runtime.json` 存在时
报告运行验证未覆盖。独立合法 journal 引用继续展示；不读取这些恢复 blob、hook 正文、
runtime auth/transport 或 `journal_rows` 聊天内容。journal 的 record JSON 可以包含
`launchArgs`/options，reader 仅校验其形状和大小，不输出或执行这些内容；投影不包含
argv、options、凭据或原始 record JSON。不启动产品、socket、迁移、bridge 或同步 helper。

数据库及已知侧文件先检查普通文件和目录；SQL 使用 `mode=ro`、`query_only` 及读事务，
包含当前 WAL 中的引用。SQLite 的内部读锁可能创建或更新 WAL/SHM 侧文件，因此不承诺
文件系统零写入；不在读取后清理侧文件，也不以 `immutable` 忽略活跃 WAL。

已知保护 profile 与候选 adapter 分开；选择 native、Pi/Claude 或某个 Orca profile
不会丢掉其他已知来源，也不会把 guard-only home 增加为候选。写前刷新有界 metadata，
精确 store 错误只收紧相应目标。需要保留这些来源的新 top-level plan 使用 v2，将全部
已知 profile roots 冻结到 hash 内的 `guard_sources`；fresh/same-process apply 与残留
run 轮次并集保留 frozen/current/显式来源。旧 v1 不补字段或重算 hash；缺新保护来源
证据的未执行 batch 要求重新计划，unknown 仍只通过 status/verify 恢复。详见
[operation 协议](operation-cli.md)。

默认/env/显式路径和可信 marker 之外的 custom、搬移、dev/E2E、去 marker 账号、未证明
runtime、WSL/远端及桥接副本不具备全局发现保证。持久化 lease 或进程名字缺失不能证明
writer 已停。当前运行观察不提供全盘 writer 证明，macOS/Linux 和 Orca 产品实机运行
归属尚未验收。

### 千问办公与 QoderWork：CN 本地会话清理

`OfficeAdapter` 以独立 `qwenwork` / `qoderwork` 客户端和引擎接入公共 records，
共享盘点与删除基础设施，分别校验整套表、索引、外键和 FTS trigger schema。登记依据为
2026-10-04 静态解包的官方 Windows 包：QwenWorkCN 1.2.5
（SHA-256 `95a4a35cb4517fd145913233c171c211d00a84b4f2c84be7685db64fd6afc3c8`）与
QoderWork CN 0.9.18（`085f668322572cca2c600f9a96e567f2f926f9830a78809966d9cc7c2e99242d`）。
产品入口见[千问办公](https://help.aliyun.com/zh/qwenwork/desktop-usage)和
[QoderWork](https://docs.qoder.cn/qoderwork/product-overview/what-is-qoderwork-cn)。
国际版、其他版本和相似产品名不自动继承该资格。

Windows 的数据库分别位于 `%APPDATA%/QwenWorkCN/data/agents.db` 与
`%APPDATA%/QoderWork CN/data/agents.db`。发现同产品 stable/dev/canary profile 时
分别列出；`--qwenwork-root` / `--qoderwork-root` 可重复指定。SDK 根分别为
`~/.qwenworkcn`、`~/.qoderworkcn`，也可通过对应 `--*-sdk-root` 显式指定。
同产品多个 profile 共享 SDK 根，不能从一个 profile 的会话 ID 推断 SDK 独占归属。

公开记录身份是 `(client, profile, chats.id)`；`sub_chats.session_id` 才是 SDK UUID。
只读查询保留时间、项目、子会话、规范化 messages 表的计数及云映射/任务日志/提醒
计数，不读取 name、消息 parts、searchable_text、legacy messages 或凭据配置。
`inventory_scope=database_metadata_and_known_sdk_paths` 仅表示该范围的读取情况。
SDK 文件仅按 UUID 在有界目录枚举位置，不读取正文，不将相似名称当作另一产品记录。
未知 schema、损坏关联、链接目录、读取失败或预算耗尽保持 blocked；其它已读取
profile/记录继续显示，不将错误折叠为零记录。

已支持的本地会话沿现有 `plan → apply → status/verify` 执行。完整 ID 选择可以包含
未软删除聊天；项目范围默认只选软删除聊天。数据库事务删除主/子行、关联消息、FTS
投影和独占 nudge，保留项目与其他聊天，并更新 Qwen sidebarTaskLayout。SDK 批次包括
主转录、已证明归属的 subagent、session-memory、file-history、工具/图片缓存和会话
日志；Qwen 还处理逐子会话 shell/sandbox 输出和 sensitive-vault 条目，保留主密钥。
文件身份、完整清单和哈希必须在执行前仍与计划相符。

Chromium 数据库同时检查 profile 顶层与 `Partitions/main/Local Storage/leveldb`。
显式安装的 Node/classic-level 固定依赖在私有副本上制定计划；验证物理文件、逻辑
投影及运行时代码后，才对原库同步写入并冷读取核验。清理逐聊天草稿、置顶、打开的
子会话、已识别的布局与恢复上下文，同时更新 Chromium origin 元数据。无逐聊天
归属的 `agent-input-history`、模型偏好、预览设置和登录数据保留。未知格式或校验
失败不降级为仅删 SQLite。安装步骤见[Office CLI](operation-cli.md#office-local-sessions)。

profile 与 SDK 分成独立物理根批次，同时冻结并检查旧 unknown 操作。SDK 批次经
验证完成后才释放 profile 批次；每次修改前持久记录 mutation checkpoint，部分失败
保持 unknown，不自动重发。冷 status/verify 从原计划还原根目录，检查所选记录及
未知操作的保留数据投影。执行要求办公主进程、helper、SDK CLI/worker 及其子进程
已退出；不能从缺少主窗口推断数据目录已停止写入。

同产品其他 profile 的 SDK UUID 引用参与冻结；共享 UUID、未选 fork、云端映射、
选中会话关联的自动化/import/ACP 恢复引用、未知 schema 和不确定文件归属逐项
阻断。云端映射不授权远程删除。用户文档、工作区、输出、登录信息、共享 memory、
voice history 和全局日志不属于单条记录清理范围。支持依据是上述 CN schema 和
临时数据上的真实 SQLite/LevelDB 操作，不代表其他发行版本或真实客户端重启已验收。

### Orca：精确原生删除的限定组合

原生删除组合限 Windows、本机 journal4/record2、明确 managed account home、
显式选择的 Codex 原生记录及其必须后代。未被完整前端操作覆盖的 current/history/restore
引用或 bridge 仍阻挡；runtime home、Claude/Pi、远端写入继续关闭。
调用 `delete plan --client orca --record-id ID --engine codex` 时，协调器逐目标冻结并核查
资格；品牌、目录名和 marker 不独立授予写权限。

选择普通本机 structured-chat 的 frontend session ID（可加 `orca:` 前缀），并显式
指定 `--codex-bin` 后，可生成包含原生链及 `delete_orca_frontend` 的完整计划。
适配依据为 Orca 固定源码
[`efbf651c`](https://github.com/stablyai/orca/tree/efbf651c7bb2eec778daf1844f8228e70809ec9f)。
完整操作先删除 current/history 对应的原生会话，再清理共享 journal、旧 JSON
records/tabs、根及各 profile 的 `orca-data.json`、bak/export、profile-state v3
数据库和已识别 backup、属于所选会话的旧单会话 journal。界面分组、选中项、pane
引用同步调整；设置、自动化、无关会话和远端命名空间保留。操作防重放 ledger 保留
已消费身份，所选成功结果转为不含会话内容的 unknown tombstone。

计划冻结完整文件集合、身份、前后哈希、profile ID、版本及原生依赖；native child
均经同一操作的持久回执验收后才派发前端 child。原生已完成、前端未开始时可冷续接，
不重启已完成的原生子批次；前端写入中断后只允许只读复核全部批准 after 状态。
只有前端残留时也必须锁定并检查其原生 home 的未决操作和完整残留，不能借空数据库
跳过 rollout 或旧 index。

hooks 根、已知生产/开发 namespace、status、authority、spool 和临时写入副本全部
核查并冻结；与所选会话有关的 TUI 数据、未知 recovery/schema、无法证明的归属
保持阻挡。runtime metadata 与 daemon lock 只作进程身份输入，不能凭文件不存在
认定停止。前端需要关闭 Orca 及相关后台进程；探测失败阻挡，执行中每次写入前重查。

固定 API 为真实 `codex-cli 0.160.0` Windows 原生 exe，SHA-256 为
`fdda5fa3cf3fb3d000b876720742857676293e4315e4b045fae6f8bd7e866d1d`。
依据为官方 `rust-v0.160.0` 的 Windows standalone 包及固定源码
[`a956835d`](https://github.com/openai/codex/tree/a956835d020762cb2b570053af06f643a11c0ecc)。
相同版本字符串不替代文件哈希；先前登记的同版本 binary 不继承本次资格。
要求 `state_5.sqlite` 的 SQLx 58 项及四个辅助 DB 各 2 项 migration/schema 与登记元数据
一致，backfill 为已完成单行，rollout migration 两表为空；缺 state DB、空 DB、旧/未知
schema 均不授予 writer，缺辅助 DB 仅允许固定运行时创建。所有共享 DB/index、已知
rollout 必须 plain、nlink=1；已知别名、managed marker、父目录和配置来源纳入冻结。

target evidence/runtime policy v2 同时验收空启动目录及带受限配置、模拟凭据、已有
启动产物的 managed home。`config.toml` 仅接受有界的模型/推理/审批/沙箱/认证偏好、
更新开关与项目 trust 标记；未知 key、provider/profile/hooks/MCP 表及存储重定向阻挡。
配置只冻结哈希；`auth.json`、`.credentials.json`、`credentials.json` 仅冻结身份和
哈希，不解析或输出值。固定 runtime 强制 ephemeral auth，不加载这些凭据。
`environments.toml`、Windows system config/requirements 及已知相邻 package manifest
必须不存在，祖先目录身份可证明；不读取这些配置文件。

已有 startup 产物仅允许固定 bundle 的逐文件内容哈希、合法 installation UUID、
已登记目录及 plain、单链接、空的协调锁；已有活动 arg0 bucket 阻挡。允许的新建
副作用限已登记的辅助 SQLite family、bundled system skills、installation ID、
精确 arg0 shim/锁、thread coordination lock 和 `.sqlite-maintenance.lock`。
未知叶、内容漂移、链接或目录替换阻挡。

原生启动会清理跨 thread 的过期日志，因此已有 `logs_2.sqlite.logs` 必须为空。
未登记的 `thread_history_1.sqlite`、`memories_v2_1.sqlite`、
`agent_message_board_1.sqlite` 及 WAL/SHM/journal 必须不存在；原生 index 的固定
替换路径 `session_index.jsonl.tmp` 也必须不存在。
所选 thread 的 `memories_1.sqlite.stage1_outputs` 不得含 `selected_for_phase2` 标记，
避免原生删除触发未授权的全局 memory consolidation job；不限制无关 thread 的标记。
rollout 文件名须包含有效时间戳与批准 UUID；sessions/archived_sessions 的有界枚举
不得发现压缩 `.jsonl.zst` 副本。以上条件在执行前重查；“用过的 home”不表示任意
业务状态都已取得写入资格。

专用运行时使用完整 allowlist 环境与全新 TEMP cwd，不继承 storage override、认证、
Git 配置/credential helper；采用固定 strict-config 覆盖并禁用 plugins/remote plugin/
apps/hooks/skill 搜索依赖安装等启动同步。早期默认启动曾实际尝试 GitHub `ls-remote`；
最终禁同步策略不能用 loopback proxy 拒绝本身代替验收。Windows Job 在 native spawn
前建立，parent 为唯一 Job handle owner；父进程崩溃会回收整个子树。
该固定版本的 remote control feature 已移除，隔离环境使用实际启动门槛
`CODEX_INTERNAL_APP_SERVER_REMOTE_CONTROL_DISABLED=1` 关闭 transport 的认证与连接循环，
并把此设置纳入 invocation policy；不依赖已经无效的 feature 开关。

关闭资格使用真实 Toolhelp PID/父 PID/image name/birth metadata，拒绝已知 Orca 主进程、
目标相关 lease owner 及其已知子链，保留探测错误；不采集全局 argv。无关 Codex/
ChatGPT 实例不扩大这个目标保护范围，显式 `--clients-closed` 仍必需。只读冷恢复要求
原机器与 Windows 登录 session、原 named Job 不存在，直接核查冻结目标残留，不重启
binary。协议版本与 unknown 占用见 [operation 协议](operation-cli.md)。

固定 binary 隔离验收入口仅创建合成 TEMP stores，无真实 profile、凭据或聊天正文：

```powershell
$env:PYTHONPATH = 'src'
python -m tests.orca_binary_acceptance --binary 'C:\absolute\pinned\codex.exe'
python -m tests.orca_configured_acceptance --binary 'C:\absolute\pinned\codex.exe'
python -m tests.orca_fullflow_acceptance --binary 'C:\absolute\pinned\codex.exe'
python -m tests.orca_fullflow_acceptance --binary 'C:\absolute\pinned\codex.exe' --scenario cold-continue
# 独立检查延迟后台维护，不发送删除请求：
python -m tests.orca_configured_acceptance --binary 'C:\absolute\pinned\codex.exe' --maintenance-warm-seconds 65
```

入口先进入完整隔离环境的新 Python worker，再执行公共 v3 plan/cold apply/status/verify；
结果保留在输出的 TEMP 路径。它只接受登记 SHA 和限定资格，不是 live cleanup 命令。
配置验收还从真实启动生成 schema/skills，冻结合成配置及不合法的模拟凭据，验证
执行前漂移阻断、父子删除、范围外哨兵保持，以及三个独立进程的 apply/status/verify。
延长维护检查允许运行时生成自身日志，并验证下一次启动仍被空日志条件阻挡；
不删除这些日志来制造可写资格。

### Herdr：已接入持久化只读引用

`HerdrAdapter` 是独立的类型化 metadata adapter，使用公共 `records --client herdr`
入口，接受固定上游 `d6b40d4edd550ccea081f089605a64314f8c8b27` 的持久化
`version:3`，并验证真实 serde layout（`Pane`/`Split`）及 workspace/tab/pane 必需字段。
遍历全部 tabs/panes，包括非当前项；普通只有 cwd 的 pane 不制造原生引用。
依据：[snapshot schema](https://github.com/herdrdev/herdr/blob/d6b40d4edd550ccea081f089605a64314f8c8b27/src/persist/snapshot.rs)、
[agent session](https://github.com/herdrdev/herdr/blob/d6b40d4edd550ccea081f089605a64314f8c8b27/src/agent_resume.rs)。

发现限于显式 `--herdr-root` 或 release/dev 默认配置目录：优先
`XDG_CONFIG_HOME`，Windows 其次为 APPDATA/USERPROFILE，其余为 HOME/.config，缺失时
使用系统临时目录。显式路径就是 config root，不再追加产品名；相对、foreign-OS、UNC
或远端 locator 不作为本机 root。`HERDR_CONFIG_PATH`、`HERDR_HOME`、`XDG_STATE_HOME`
不提供 session root。只枚举 root 和 `sessions/<name>` 下的已知来源，保留 session 名
精确拼写；发现本身不连接或启动 server。依据：
[配置路径](https://github.com/herdrdev/herdr/blob/d6b40d4edd550ccea081f089605a64314f8c8b27/src/config/io.rs)、
[session 路径与名称](https://github.com/herdrdev/herdr/blob/d6b40d4edd550ccea081f089605a64314f8c8b27/src/session.rs)。

`session.json` 作为 current 来源；同级 `session-snapshots` 和 `session-backups` 中
上游 `session-<39 位 u128 timestamp>-<pid>-<sequence>.json` 文件作为 restore 来源，
其内容是原始 snapshot，没有额外 wrapper。当前文件为空仍盘点独立恢复文件。
同 ID 的每个 source/session/pane 保留独立 binding；恢复、历史布局不成为原生 parent 边。
坏 JSON/UTF-8、未知版本、未识别的恢复名称和读取失败保留精确 `SourceFailure`，
独立有效来源仍展示。`.pending` 是未发布文件，不读取；手工命名恢复文件不在解析范围内。
每个枚举目录最多 256 项、snapshot 最多 4 MiB，超限报告 incomplete；symlink/reparse
目录或 metadata 文件拒绝跟随。依据：
[恢复文件](https://github.com/herdrdev/herdr/blob/d6b40d4edd550ccea081f089605a64314f8c8b27/src/persist/writer.rs)。

`agent_session` 只投影 source/agent/kind/value 与来源定位。Codex/Claude/Pi ID、Pi path
和 cwd 都不证明 native root，所有引用保持 `native_record=None`，descriptor 不声明
native stores 或调用默认 native catalog；同 ID 的无关原生记录不受这些 rootless 引用影响。
未知 backend 保留原文和规范化只读 engine，写入及 verify 能力均关闭。
不读取 `session-history.json` 终端正文；snapshot 中的 argv/private 字段可能被 JSON
reader 读到，但不输出或执行，也不由 argv 补造 native ID。`agent_resume`/`launch_argv`
等独立恢复语义未实现时明确报告覆盖不足。

普通盘点保留 `live_metadata_not_probed`，不查询端点。只有明确选择 Herdr 的
`records --inspect-clients` 才读取各已知 default/named session 的 `herdr.sock` metadata API。
每次连接只发送一个 JSON 行请求：先 `ping`，再另连 `session.snapshot`，`params={}`；
只接受 protocol22 和一致的 version，验证 public ID、交叉关联、layout 与 pane/tab 计数。
已知 `0.9.3` 及合法后缀仅提供兼容观察，不证明二进制 SHA；其他 base version 即使
shape 可读，也保留引用并报告 `live_version_unverified`。依据：
[wire protocol](https://github.com/herdrdev/herdr/blob/d6b40d4edd550ccea081f089605a64314f8c8b27/src/protocol/wire.rs)、
[session snapshot](https://github.com/herdrdev/herdr/blob/d6b40d4edd550ccea081f089605a64314f8c8b27/src/app/api/session.rs)。

Unix 使用本机同用户 socket；Windows 使用有界 overlapped named-pipe I/O，检查 marker
与 pipe peer PID，超时取消并收口 handle。路径 namespace 在本机 I/O 前校验，父链和
endpoint/marker 在查询前后核对；变化报告 incomplete，不延用该 live snapshot。
极端取消未确认时报告 incomplete 并暂停后续 probe，最多保留一份必要 OVERLAPPED
存储，待后续 probe 确认完成后释放；不留下后台 reader 线程或子进程。
pipe 名按固定 interprocess 2.4.2 原样拼接 `\\.\pipe\` 与 socket locator，保留已知显式/
默认路径的原始斜杠拼写。canonical 相同不证明所有 pipe 别名相同，不搜索任意别名；
`HERDR_SOCKET_PATH` override 尚不支持，明确报告覆盖不足。每 endpoint 的两请求共享
350 ms deadline，每 profile 共用 2 s、8 MiB 响应预算；pong 最多 16 KiB、snapshot 最多
2 MiB，预算耗尽的 session 保持 unprobed。依据：
[IPC identity](https://github.com/herdrdev/herdr/blob/d6b40d4edd550ccea081f089605a64314f8c8b27/src/ipc.rs)、
[Windows namespace](https://github.com/herdrdev/herdr/blob/d6b40d4edd550ccea081f089605a64314f8c8b27/src/platform/windows.rs)。

live 来源记录独立 CURRENT/LIVE binding，持久化 current 保持 unknown、restore 保持
restorable；两者不按同数值 pane ID 合并，live 的 public ID 会重映射。pane 和 agent 的
同一 public identity 引用不重复计数，冲突值分别保留并报告错误。`reference_values_match`
仅比较同 profile/session 的 current 与 live 引用值及数量，不证明 pane 对应、同代实例
或原子一致。失联、版本变化、覆盖缺口或差异不会清空持久化/恢复引用。

`client_ownership` 从同次 adapter 缓存输出，不发第二轮查询。`probe_complete` 仅表示
metadata 查询完整，`coverage_complete` 始终 false，scope 明确为 profile/session。
有效 Pong 即使后续 snapshot 失败也保留 `clients_closed=false`；没有有效响应则保持
unknown，永不由缺 socket 推断 true。`detached_daemon_observed` 是 Pong 中严格的
布尔值或 unknown，表示服务启动时自报的 detached daemon 状态，依据
[上游进程检测](https://github.com/herdrdev/herdr/blob/d6b40d4edd550ccea081f089605a64314f8c8b27/src/api/server.rs#L61)。
它不证明当前附着客户端数量、OS 进程停止或 native 归属；`attached_clients` 仍为 null。
清单保留 `runtime_writer_coverage_unknown`（未探测时为 `live_metadata_not_probed`），
这些限制的 `blocks_delete=true`、`blocks_inventory=false`。`metadata_scope` 区分
persisted 与 persisted_and_live；完整读取请求范围内的已支持元数据时，records 返回
退出码 `0` 和 `goal_status=complete`，不表示可以清理。读取失败、未知 schema/argv、
transport 失败及 live/persisted 冲突仍使清单 blocked。API 不发送
mutation 命令，不启动/attach/restore，也不查询 `agent.get`、终端正文或进程命令行。
JSON 中的 private 字段可能被读取，但不投影或执行；server 自身可能写请求日志或处理
已有 title 事件，不能承诺产品内存/文件系统零副作用。

所有 native/frontend/remote 写入及 Herdr 自身 verify 关闭；选择
Herdr 的 delete plan/run 结构化 blocked，无授权动作的 apply/status/verify 保留该结果。
rootless Herdr 不进入 Orca `guard_sources`，也不改变旧 v1 hash 或只读恢复契约。
合成临时来源已通过 Windows/macOS/Linux × Python 3.10/3.12 的实际 CI，包含
Windows 临时 named pipe 与 macOS/Linux 临时 Unix socket。全部 runtime writer
归属及产品实机验收仍未完成，平台测试通过不授予删除能力。

后续改造的阶段、模块范围与验收门槛见[多客户端施工方案](cleanup-refactor-plan.md)。
该方案中的待实施能力不改变本页的当前支持边界。

### Paseo：agent 注册记录只读盘点

固定依据为上游 `0.11.0-beta.3`、commit
[`05b074764dd1be4b7b04c7ab403ebeb88393aee3`](https://github.com/getpaseo/paseo/tree/05b074764dd1be4b7b04c7ab403ebeb88393aee3)。
`paseo` 是独立客户端身份；记录 ID 属于 Paseo profile，不能替代提供者的 native ID。
仅选中该客户端或显式传入 `--paseo-root` 才发现其根目录，默认 `PASEO_HOME` 或
`~/.paseo`，不扫描其他主机或连接 daemon。相同物理 profile 的已证明路径别名合并，
链接/reparse 根和文件在读取前拒绝。

[AgentStorage](https://github.com/getpaseo/paseo/blob/05b074764dd1be4b7b04c7ab403ebeb88393aee3/packages/server/src/server/agent/agent-storage.ts)
保存没有独立版本字段的 JSON，读取 `agents/*.json` 与 `agents/*/*.json`；新记录位于
cwd 编码的项目目录。reader 检查已知字段和有界元数据形状，不把缺失或损坏的目录解释为
零记录成功。未发布的 `.tmp` 不读取，更深目录报告未覆盖。单文件上限 4 MiB、总读取
预算 64 MiB（最多额外一个检测越界的字节）、目录项上限 20,000；单个文件读取期间的
身份、大小、mtime 或扫描到的目录变化会使观察不完整。这不是跨文件事务快照，也不能
证明完成观察后文件不再变化。

输出采用明确字段清单：Paseo ID、provider、cwd、workspaceId、时间、保存的 status、
archivedAt、internal、daemon owner ID、固定 parent-agent 标签、恢复 ID 及快照来源/指纹。
`persistence` 和 `runtimeInfo` 的引用分别保留。Codex/Claude 等已核实 provider 的
标量 `nativeHandle` 为 ID，Pi/omp 的为不透明 session 文件路径；不会打开该文件或
推断 native root。未知 provider 原名保留在元数据中，引擎投影为 `unsupported:*`；
未知或对象类型的 nativeHandle 只报告存在与未解析状态。JSON 中的 title、任意 labels、
config、provider metadata、runtime extra 和 lastError 不进入输出；原生转录不读取。
同 profile 重复 agent ID 保留每个快照与引用，并报告冲突，不采用上游缓存的最后覆盖值。

所有 capability 的删除与 verify 标志关闭，`delete plan/run/apply`、`operation status/verify`
不会把只读结果升级为删除完成。`records --inspect-clients` 只说明持久元数据观察范围，
`clients_closed=null`、`coverage_complete=false`；保存的 `closed` 状态不是进程关闭证据。

当前完整性只覆盖 `persisted_agent_registry`，不包含 schedules、project/workspace 注册表、
原生 provider 历史、daemon 内存、客户端缓存或所有子代理关系。Codex/Claude 的 native root
取决于启动环境及 provider 配置，快照中的 ID/cwd 不足以绑定它。Paseo 的
[删除请求](https://github.com/getpaseo/paseo/blob/05b074764dd1be4b7b04c7ab403ebeb88393aee3/packages/server/src/server/session.ts#L3161)
通过缓存写入屏障、关闭运行时及排空队列后移除注册快照；该动作不是通用原生历史删除，
错误也可能只记录到日志后继续发送删除事件。直接 unlink 则可能被内存缓存写回。
归档会调用 provider 生命周期并可能级联子代理；桌面退出还可能保留后台 daemon，
监督进程可能重启 worker。后续 writer 必须明确批准副本/调度与父子引用范围、证明全部
相关写入者停止并提供持久 journal/只读恢复，不能用归档或删除事件代替终验。

## 既有 Codex Finding adapter

Codex compatibility adapter 将外部平台的删除状态转换为保守的 `Finding`，不能直接
删除前端或 Codex 数据。Codex Desktop 私有宿主状态由独立、版本探测的实现负责。

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
    "needs_quarantine": False,  # legacy compatibility: manual review needed
    "cleanup_blocked_reason": None,
}
```

这些字段描述的是“当前这条 Finding”而不是平台的永久能力。它们是计划生成器的证据输入，不会直接成为 CLI 删除目标。存在活跃引用、冲突证据或不完整关系时，应将 `cleanable` 设为 `False` 并给出阻断理由。

Adapter 不负责决定最终风险或动作，也永远不直接写库。核心层会按
`(storage_id, full_thread_id)` 聚合全部 Observation，枚举列表记录和内容文件，读取完整
关联任务范围，并生成整条记录删除、精确关系边清理、精确 frontend reference 清理、
旧索引/Desktop 残留清理或 `keep`。共享数据库写入由独立 writer 在重新验证 adapter
提供的 schema、行定位和完整指纹后执行。`repair_index_path` 和
`quarantine_artifacts` 不再生成；旧 `needs_quarantine` 字段只表示需要人工身份复核。

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
