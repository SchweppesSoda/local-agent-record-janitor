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

### 文件别名与运行观察

`file_alias_evidence.py` 的 `probe_file_aliases()` 接受已发现的本机路径与明确 roots，
提供 `FileAliasSnapshot`/`FileAliasEvidence`：观察时间、词法/解析路径、readlink、
device/file ID、nlink、已知路径与错误。host/namespace 在构造 `Path` 前检查；UNC、
foreign-OS、范围外及断链 locator 保持不透明或 incomplete。只允许范围内的 leaf
symlink，目录链接和身份变化不能视为完整观察；不读取内容或另行发现副本。

`collect_client_file_aliases()` 仅提取选中 native 清单项的 rollout、Pi JSONL 或 Claude
manifest 文件。`records --client ...` 将结果作为独立 `file_aliases` 输出，不加入旧
target、snapshot_id 或 v1 approval/hash。相同 file ID 只关联观察证据，不合并不同
`StoreKey`。普通/extended/8.3 spelling 不重复算目录项；nlink 匹配仅描述观察时点的
已知 hardlink 名称，`alias_coverage_complete` 始终为 false，不证明所有 symlink/copy
已发现。若以后用这些新证据改变 writer 授权，必须按计划兼容规则另行版本演进。

`inspect_client_ownership()` 和 `--inspect-clients` 使用 descriptor 的真实 owner root，
不从 native root 补造归属。`probe_complete` 表示枚举/探测成功，`coverage_complete`
仅在支持的本机 Windows owner、Codex engine 和具备必要 metadata 的已注册进程范围
内成立；仍不证明任意其他 writer 已停止。collector 当前只枚举 Codex/ChatGPT/AionUI/
Cindy 四类 exe，复用 Cindy 的真实进程树/profile 证据。未知 owner、Pi/Claude runtime、
非 Windows 或失败返回 unknown；明确的相关运行进程仍可返回 false。输出不含 command
line。该观察不替代现有关闭 ack，也不修改既有 writer 的关闭检查。

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

## Orca 与 Herdr 的接入边界

当前没有 Orca 或 Herdr 专用 adapter/writer，也没有其本地实机兼容性验证。
以下是根据上游公开实现得到的接入要求，不是支持声明。

- **Orca**：上游枚举按账号隔离的 Codex homes，WSL homes 另走自己的发现路径；
  session bridge 的链接实现优先 hardlink，失败后尝试 symlink。因此需要同时保留逻辑
  store 身份和物理文件别名证据，检查所有受影响链接及 frontend 引用，不能按账号或单
  一目录推导完整删除范围。本库已有部分链接保护，尚未建模 Orca 的完整桥接图。
  依据：[account home discovery](https://github.com/stablyai/orca/blob/main/src/main/codex/codex-account-home-discovery.ts)、
  [session link](https://github.com/stablyai/orca/blob/main/src/main/codex/codex-session-link.ts)。
- **Herdr**：detach 后 server、pane 和 agent 可以继续运行；`session.json` 和 layout
  snapshots 保留 native session 恢复引用。因此应识别 server/session/host 和真实 writer，
  盘点恢复引用；关闭 pane 的 API 不能当作删除原生 conversation 的协议。
  依据：[session state](https://herdr.dev/docs/session-state/)、
  [socket API](https://herdr.dev/docs/socket-api/)。

接入顺序是先确认真实版本和 schema，提供只读归属/引用清单，再验证 native writer 与
关闭检查，最后开放精确删除及恢复。公共身份、错误与关系契约先保持一致；只有出现
真实可复用实现时再提取 registry，避免为未验证的产品增加空壳支持。

后续改造的阶段、模块范围与验收门槛见[多客户端施工方案](cleanup-refactor-plan.md)。
该方案中的待实施能力不改变本页的当前支持边界。

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
