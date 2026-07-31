# Codex Session Janitor

`codex-session-janitor` 是一个本地、保守的 Codex 会话一致性检查与清理工具。它处理两类问题：

1. AionUI、Cindy 等外部 Agent 前端已经删除了对话，但其调用 Codex 产生的对话、列表记录或内容文件仍留在磁盘。
2. Codex 自身的列表记录、内容文件和对话关联关系不一致，例如“只有列表记录没有内容”“只有内容没有列表记录”或孤立的关联任务对话日志。

项目目前处于 **Alpha**。`scan` 始终只读。`clean` 会先生成基于动作的结构化计划；只有用户明确选择目标、计划通过执行前重验证，并在 TTY 中输入专用确认词或使用受约束的 `--yes`，才会执行会话硬删除或带备份的旧索引修复。

## 为什么需要它

Codex 的本地会话不是一个文件，而是至少包含：

- `state_5.sqlite` 中的对话列表记录与关联数据；
- `sessions/` 或 `archived_sessions/` 中的 rollout JSONL；
- 对第三方前端而言，前端数据库中还会保存一层“前端会话 ID ↔ Codex 对话 ID”映射。

任意一层单独删除，都可能留下无法从界面管理的记录。一次匿名化的本机复现中，我们先发现了 **9 条有列表记录但内容文件已不存在的记录**，随后又识别出 **58 条失去有效父对话关系的关联任务对话日志**。这些数字只是问题背景，不是检测规则，也不会被硬编码。

后来还分别确认：

- AionUI 删除对话后，Codex 对话可能仍然存在；
- Cindy 将会话标记为 `deleted` 后，Codex 对话和内容文件仍可能保留。

在隔离的临时 `CODEX_HOME` 中，我们还用 Codex `0.144.6` 验收了官方删除行为：

- index-only（`threads` 行存在、rollout 缺失）可由 `thread/delete` 清除，无需手改 SQLite；
- 合成的有效 rollout-only（rollout 存在、`threads` 行缺失）也可由 `thread/delete` 清除并返回 `{}`；
- 一个完全不存在、既无索引也无 rollout 的 ID 返回 `-32600` / `no rollout found`，不会被误报为成功。

这说明官方接口具备修复部分不一致状态的能力；是否自动调用仍取决于 Janitor 对来源、父子关系和冲突证据的安全判断。

## 支持范围

| 范围 | 检查依据 | 当前候选动作 |
|---|---|---|
| AionUI | 对话已不存在，但 `acp_session` 映射仍在；并要求 Codex backend/originator 证据 | 删除整条 Codex 对话；前端引用清理会结构化显示但尚不可执行 |
| Cindy | `agent_kind='codex'`、`status='deleted'` 且存在 `sdk_session_id` | 删除整条 Codex 对话；前端引用清理会结构化显示但尚不可执行 |
| Codex：index-only | 对话列表记录存在，但内容文件不存在 | 删除整条对话，通常为 `low` |
| Codex：rollout-only | 内容文件存在，但对话列表记录不存在 | 删除整条对话，属于 `high`，必须明确选择 |
| Codex：重复内容文件 | 同一 ID 有多份可验证内容文件 | 隔离动作尚未实现；删除整条对话为 `high`，只能逐项明确选择并批准精确范围 |
| Codex：路径错位 | 列表路径与实际内容文件不一致，但文件身份可验证 | 路径修复尚未实现；删除整条对话为 `high`，只能逐项明确选择并批准精确范围 |
| Codex：孤立关联任务对话 | 明确 subagent 证据，父对话的列表记录和内容文件均缺失 | 删除整条对话；必须展示它创建的其他关联任务对话 |
| Codex：残留关联记录 | 对话关联记录的一端不存在 | 单独清除关联记录尚未实现；若记录指向的子对话仍有身份精确可验证的本地数据且无来源冲突，可逐项选择 `high` 风险删除该子对话 |
| Codex：旧版聚合索引残留 | `session_index.jsonl` 中的 ID 经严格 SQLite 与活动/归档 rollout 清单证明已无 live 会话 | 修复文件；`high` 风险、逐项选择、先持久备份，可按 backup ID 受保护还原 |

所有发现都会成为 Observation，并显示一个或多个 CandidateAction。当前可执行的是 `delete_conversation`（删除整条对话）和严格受保护的 `repair_legacy_index`；修复列表路径、隔离文件、清除关系或前端引用等动作仍只结构化显示，并附带不可用原因。

## 平台与 Shell

这是 Python 3.10+ CLI，不需要额外编写或安装 `.cmd`、`.bat`、`.sh` 包装脚本。PowerShell、`cmd.exe` 和 macOS/Linux 的 sh 只是不同入口；扫描、计划、官方 `thread/delete` 和删除后验证都由同一套 Python 实现完成。

支持边界：

- native 扫描可在 Windows、macOS 和 Linux 使用，前提是 Codex 使用标准 `~/.codex` 数据布局，且可信的 `codex` 可执行文件位于 `PATH`；
- AionUI 和 Cindy 的默认数据库、数据目录及捆绑 Codex 自动发现目前主要针对 Windows；
- 其他平台上的第三方前端路径尚无公开 CLI 支持承诺，不应依赖隐藏参数或内部程序接口建立自动化。

安装后通常使用 `codex-session-janitor` 命令。如果 pip 安装的 console script 尚未进入 `PATH`，所有示例都可以改用 `python -m codex_session_janitor`（macOS/Linux 常用 `python3 -m codex_session_janitor`）。

### Windows PowerShell

```powershell
git clone https://github.com/SchweppesSoda/codex-session-janitor.git
Set-Location codex-session-janitor
python -m pip install -e .

codex-session-janitor scan --platform native
codex-session-janitor clean --platform native --thread-id 0198abcd
codex-session-janitor clean --platform native --thread-id 0198abcd --yes

# PATH fallback
python -m codex_session_janitor scan --platform native
```

### Windows cmd.exe

```bat
git clone https://github.com/SchweppesSoda/codex-session-janitor.git
cd /d codex-session-janitor
python -m pip install -e .

codex-session-janitor scan --platform native
codex-session-janitor clean --platform native --thread-id 0198abcd
codex-session-janitor clean --platform native --thread-id 0198abcd --yes

rem PATH fallback
python -m codex_session_janitor scan --platform native
```

### macOS / Linux sh

```sh
git clone https://github.com/SchweppesSoda/codex-session-janitor.git
cd codex-session-janitor
python3 -m pip install -e .

codex-session-janitor scan --platform native
codex-session-janitor clean --platform native --thread-id 0198abcd
codex-session-janitor clean --platform native --thread-id 0198abcd --yes

# PATH fallback
python3 -m codex_session_janitor scan --platform native
```

## 快速开始

先关闭 AionUI、Cindy 和正在使用同一 `CODEX_HOME` 的 Codex 客户端，再扫描：

```powershell
codex-session-janitor scan
```

仅扫描一个来源：

```powershell
codex-session-janitor scan --platform aionui
codex-session-janitor scan --platform cindy
codex-session-janitor scan --platform native
```

输出机器可读 JSON：

```powershell
codex-session-janitor scan --platform all --json
```

按完整对话 ID（`thread_id`）或唯一前缀缩小范围：

```powershell
codex-session-janitor scan --thread-id 0198abcd
```

交互查看所有问题。输出按保存位置列出候选目标；每个目标展示会话名称、项目与完整 cwd、完整 ID、根/级联关系、子代理名称/角色/路径、父 ID、索引/归档/originator、元数据来源与冲突。每个候选动作都有临时编号：

```powershell
codex-session-janitor clean
```

可输入 `1,3-5` 明确选择动作；输入 `all` 只会选择允许批量纳入的 `low` 风险动作。标记为“必须逐项选择”的动作即使是 `low` 风险，也不会被 `all` 纳入。临时编号只在本次扫描和当前进程有效，不能保存给自动化使用。

TTY 交互会在同一进程中完整展示最终计划。会话硬删除要求确认词 `确认删除`；旧索引修复要求先关闭相关客户端并输入 `客户端已关闭并确认修复`。确认后立即重新扫描并执行。输入其他内容或遇到 EOF 都会取消且零修改。`--yes` 只跳过最终提示，不会跳过编号/目标选择及其他安全条件。

按完整对话 ID（`thread_id`）或唯一前缀选择单个目标；TTY 仍会展示最终计划并要求确认：

```powershell
codex-session-janitor clean --thread-id 0198abcd
```

自动化应先读取计划 JSON，再使用其中的完整稳定 action ID：

```powershell
codex-session-janitor clean --json
codex-session-janitor clean --action-id delete_conversation-完整动作ID
```

确认备份、目标和影响范围无误后才执行：

```powershell
codex-session-janitor clean --thread-id 0198abcd --yes
codex-session-janitor clean --action-id delete_conversation-完整动作ID --yes
```

非 TTY 执行 `review` 或 `high` 风险动作时，还必须把刚才计划 JSON 的完整 `plan_fingerprint` 原样传回；执行前的全局计划或所选动作范围发生变化都会停止：

```powershell
codex-session-janitor clean --action-id delete_conversation-完整动作ID `
  --plan-fingerprint 完整计划指纹 --yes
```

旧版聚合索引是文件资源，不是虚构的会话 ID，不能用 `--thread-id` 选择、不能被 `all` 纳入，也不能与会话删除混跑。关闭使用同一数据目录的 Codex、AionUI 和 Cindy 后，可用计划中的修复 action ID 执行；结果会返回 backup ID：

```powershell
codex-session-janitor clean --platform native --json
codex-session-janitor clean --action-id repair_legacy_index-完整动作ID `
  --plan-fingerprint 完整计划指纹 --clients-closed --yes

codex-session-janitor restore-legacy-index --codex-home 'D:\CodexData' `
  --backup-id 完整备份ID --clients-closed --yes
```

还原只有在当前索引哈希仍等于该次修复产生的哈希时才会进行；还原本身也会先创建新的备份，避免覆盖后续合法变化。

非 TTY 中，`clean` 不传 `--yes` 时只预览计划且不会修改数据；有可执行动作进入计划时以退出码 `2` 表示“需要显式确认”。只有 stdin 和 stdout 同时为 TTY 时才会进入上述同进程选择与确认流程。`--yes` 不能替代 `--thread-id`、完整 `--action-id` 或当前进程中的交互编号，也不能跳过删除前重验证。无人值守执行没有选择器时会被拒绝；无人值守预览没有选择器时最多默认计划允许批量纳入的 `low` 风险删除动作，不会纳入标记为“必须逐项选择”的动作。

如果默认 `~/.codex` 不是目标保存位置，应明确目录后重新扫描，让计划为该 StorageLocation 生成动作；无法确定归属时不会自动删除：

```powershell
$env:CODEX_HOME = 'D:\CodexData'
codex-session-janitor scan --platform native

codex-session-janitor scan --platform native --codex-home 'D:\CodexData'
codex-session-janitor clean --platform native --codex-home 'D:\CodexData' `
  --codex-bin 'C:\path\to\codex.exe' --thread-id 0198abcd
```

`--codex-home` 表示本次命令显式扫描的一个 Codex 数据目录；自动发现多个位置时仍按不同 StorageLocation 展示和选择，不能只凭相同 ID 合并。

若多个扫描来源为同一数据目录提供了不同的 Codex 可执行文件，工具不会任选一个执行，而会阻止该目录的清理。请核对后用 `--codex-bin PATH` 明确指定；显式路径必须是现存普通文件，否则命令会在扫描前报错。

## 删除是怎样完成的

本工具不会直接 `DELETE` Codex SQLite 行，也不会直接删除 rollout JSONL。可安全处理的对话会按各自的 `CODEX_HOME` 启动对应 Codex：

```text
initialize → initialized → thread/delete → 删除后验证
```

官方 `thread/delete` 会硬删除活动或已归档对话，并一同删除由该对话创建的关联任务对话；rollout 已缺失时会按“已删除”处理。详见 [Codex app-server API 概览](https://learn.chatgpt.com/docs/app-server#api-overview)。

这也意味着：

- 删除不可撤销；
- 删除计划会列出根对话，以及将一同删除的关联任务对话数量和完整 ID；
- 每个删除根目标都有精确批准范围，包括关联任务对话 ID、列表中存在的对话 ID、当前存在的内容文件路径，以及每个文件的身份、来源和 `stat` 状态指纹；指纹覆盖 metadata 对话 ID、originator、source、工作目录、时间戳、活动/归档状态、规范化路径、大小和纳秒修改时间；
- 启动 app-server 后、任何删除请求前会再次捕获完整范围；每个根目标在发送请求的最后一刻还会重查 native 身份范围和全部前端活跃引用，漂移会停止后续请求；
- 经验证的重复文件或路径错位可以作为 `high` 风险整条对话删除逐项选择；修复和隔离动作仍未实现，删除后任何已知残留都会使结果成为 `partial`；
- 残留关联记录仅在记录指向的子对话仍有精确可验证的列表记录或内容文件、来源无冲突时，才可提供窄范围 `high` 风险整条对话删除；这会删除该子对话及其批准的关联任务范围，不是单独删除关系边。若子对话本身已不存在，只能选择尚未实现的 `remove_broken_relation`；
- Cindy 的 `deleted` 是前端软删除状态，但一旦对应 Codex 对话被硬删除，就不能靠恢复 Cindy 状态找回 Codex 正文。

## JSON 输出

`scan --json` 除 Finding 外还包含 storage-qualified 的会话摘要目录：

```json
{
  "findings": [],
  "conversations": [],
  "errors": [],
  "count": 0
}
```

`clean --json` 在计划阶段包含保存位置、唯一会话摘要目录、每个删除动作的派生影响视图、Observation、候选动作、风险、影响、阻断原因、action ID 和快照指纹，例如：

```json
{
  "storages": [],
  "conversations": [],
  "action_conversation_views": [],
  "observations": [],
  "actions": [],
  "selected_action_ids": [],
  "errors": []
}
```

执行结果使用 `deleted`、`not_deleted`、`partial`、`unknown` 四种状态。请求报错但磁盘和列表验证证明范围内数据均已消失时，仍报告 `deleted` 并保留请求警告；只消失一部分时报告 `partial`。

最终确认和执行结果会显示完整对话 ID；JSON 始终保留完整 ID、action ID 和绝对路径。同一 ID 位于不同 Codex 数据目录时是不同目标，不会静默合并。绝对路径、工作目录、会话 ID 与错误信息可能泄露用户名、项目名或目录结构，分享前请脱敏。

## 退出码

| 退出码 | 含义 |
|---:|---|
| `0` | 扫描成功，或所选删除、旧索引修复/还原成功 |
| `1` | 扫描、选择、重验证、执行或执行后验证发生错误 |
| `2` | 非 TTY 的修改命令未传 `--yes`，仅预览计划，没有修改 |

“扫描到问题”本身不是命令失败，因此正常扫描仍返回 `0`。自动化应读取 JSON 的 `count` 和 `errors`，不要只看退出码。

## 重要限制

- 当前只识别有明确证据的 AionUI/Cindy/Codex 状态；数据库 schema 变化可能导致 adapter 暂时不可用。
- Finding 是 adapter 证据格式，不等于删除目标。计划生成器会把它聚合为 Observation，并根据完整当前状态生成 CandidateAction。
- 兼容期仍读取 adapter 的能力证据，但它们不是 CLI 的唯一分区依据；冲突、活跃引用、范围不明或状态读取失败都会阻止动作。
- rollout 扫描只读取首行 `session_meta`，不会解析或上传聊天正文。
- `scan` 是时间点快照。扫描后重新打开、恢复或继续某个会话会使结果过期；清理前必须重新扫描。
- 不跨不同 `CODEX_HOME` 合并状态。同一个对话 ID 在不同保存位置下被视为不同目标。
- 路径错位、重复文件和残留关联记录不应通过手工“删一个文件”解决；单独关系修复等动作目前只显示、不执行。唯一直接文件替换例外是旧版聚合索引：严格清单只移除已证明无 live 会话的原始整行，并要求独占锁、审批快照、备份清单、原子替换和受保护还原。
- 扫描失败按 Codex 数据目录归属，只阻止受影响位置；无法归属到保存位置的错误按 fail closed 处理。
- 工具不清理 AionUI/Cindy 自身的消息表或软删除记录；它只处理经确认的 Codex 侧残留。

更多说明：

- [检测模型与安全边界](docs/design.md)
- [Finding 类型](docs/findings.md)
- [Adapter 贡献指南](docs/adapters.md)
- [给上游项目的修复建议](docs/upstream-fixes.md)
- [安全政策与操作清单](SECURITY.md)

## 开发

```powershell
python -m unittest discover -s tests -v
```

贡献新 adapter 前，请先阅读 [docs/adapters.md](docs/adapters.md)。任何扩大自动删除范围的修改，都必须同时提供误删反例测试。

## License

[MIT](LICENSE)
