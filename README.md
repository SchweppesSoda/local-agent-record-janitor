# Local Agent Record Janitor / 本地 Agent 记录清理器

`local-agent-record-janitor` 是本项目统一使用的仓库名、发行包名和 CLI 命令；Python
模块名统一为 `local_agent_record_janitor`。它是一个本地、保守的多引擎 Agent 记录检查
和清理工具：处理 Codex thread、Pi Agent session 和 Claude Code session，也能精确清除
Cindy/AionUI 中已经失效的 frontend reference；前端映射不是另一份 native record。

### 命名迁移

这次统一命名是一次破坏性迁移：现有可编辑安装需要先卸载先前发行包，再安装
`local-agent-record-janitor`；脚本和集成应改用 `local-agent-record-janitor` 命令以及
`local_agent_record_janitor` Python 模块。改名前版本生成的旧版聚合索引备份，应继续
使用生成该备份的对应版本执行还原。

它主要处理两类问题：

1. AionUI、Cindy 等 Agent 前端已经删除了自己的对话或会话，但它引用的 Codex
   thread、列表记录或 rollout 内容文件仍留在磁盘。
2. Codex 自身的 thread 列表记录、rollout 内容文件和 thread 关联关系不一致，例如
   “只有列表记录没有内容”“只有内容没有列表记录”或孤立的关联任务 thread 日志。

此外，`records --platform pi|claude` 可只读列出 Pi Agent/Claude Code 的本地 JSONL 会话，并区分 standalone 与 Cindy profile storage。`delete --platform pi` 只删除精确批准的单个 Pi JSONL；`delete --platform claude` 只删除批准 manifest 中的该 session transcript 副本和 session 专属辅助目录。两者均不保留、输出或上传消息正文。

项目目前处于 **Alpha**。`scan` 和 `records` 始终只读。`records` 会列出正常、异常
以及仍被 Cindy/AionUI 引用的 Codex thread；`delete` 允许用户逐项选择其中任意仍有
本地 Codex 数据、且根或级联子 thread 均没有 live frontend reference 的 thread 永久
删除；live reference 会 fail closed 并使动作不可选。`clean` 继续只处理扫描发现的异常，
不会把正常记录混入保守批量清理路径。逐项修改命令要求明确目标；专用 `purge` 命令
则以 `--yes --clients-closed` 作为整批授权，只纳入当前完整扫描中可执行的 Codex 异常
动作。两条路径都会在每次修改前重新扫描和重验证。

Codex `delete` 的目标是 `(CODEX_HOME, thread_id)`，原生 thread 删除优先调用官方
Codex `thread/delete`。这个动作不会顺带修改 Cindy/AionUI 数据库；失效的 frontend
reference 会成为另一份独立授权、独立事务和独立验证的精确清理动作。

Codex Desktop 还可能维护一层宿主目录/UI 状态。它不是公开 app-server 数据契约；
本工具只通过结构探测读取，并把“原生 thread 已不存在、但 `host_id='local'` 的宿主
目录行仍存在”报告为 `desktop_state_orphan`。这类记录不能再次发送给
`thread/delete`。只有单独选择 `remove_desktop_state` 高风险动作，或通过专用 `purge`
整批授权，并在关闭相关客户端、通过完整状态指纹重验证、创建临时回滚副本后，工具才
精确删除该目录行及 JSON 中的结构化精确 ID 引用；提示历史正文中仅仅包含该 ID 的
普通字符串会保留。验证成功后临时副本立即删除，不形成长期备份。

### 术语与身份边界

| 层 | 本项目中的含义 |
|---|---|
| Frontend namespace | Cindy/AionUI 自己的 owner、数据库、对话或会话 ID，以及它们到原生记录的 frontend reference/mapping |
| Harness runtime | 实际运行的 Codex、Pi Agent 或 Claude Code harness、二进制路径和版本 |
| Native store | `CODEX_HOME`、Pi `session_root` 或 Claude Code config root |
| Native record | Codex 的 `(codex_home, thread_id)`、Pi 的 storage-qualified JSONL session、Claude Code 的 config-qualified session manifest |

跨引擎叙述统一使用 **local agent record / 原生记录**。Codex 对象严格称为
**thread**，Pi Agent 与 Claude Code 对象称为 **session**；Cindy/AionUI 行称为
**frontend reference**。认证来源、登录状态和账号绑定只可作为诊断事实，不参与
删除目标身份，也不能代替 native store 与 native record 的精确限定；它们至多说明
某个 harness 如何获得运行授权。

在本机 Cindy `0.1.27` 的限定观察中，登录前后 local/owner 数据库 namespace 会变化，
但 bundled Codex app-server 与 Cindy `codex-home` 不变；Cindy app login 与 OpenAI
Provider auth 是两个正交轴。本工具不会据登录或认证状态推断 native store，也不会把
owner 数据库的出现当作已证明存在跨设备记录同步。

## 为什么需要它

Codex 的本地 thread 不是一个文件，而是至少包含：

- `state_5.sqlite` 中的 thread 列表记录与关联数据；
- `sessions/` 或 `archived_sessions/` 中的 rollout JSONL；
- Codex Desktop 的可选宿主目录/UI 状态（当前结构探测到时）；
- 对第三方前端而言，前端数据库中还会保存一层“frontend ID → Codex thread ID”引用。

任意一层单独删除，都可能留下无法从界面管理的记录。一次匿名化的本机复现中，我们
先发现了 **9 条有列表记录但内容文件已不存在的记录**，随后又识别出 **58 条失去
有效父 thread 关系的关联任务 thread 日志**。这些数字只是问题背景，不是检测规则，
也不会被硬编码。

后来还分别确认：

- AionUI 删除前端对话后，Codex thread 可能仍然存在；
- Cindy 将前端会话标记为 `deleted` 后，Codex thread 和 rollout 内容文件仍可能保留。

在隔离的临时 `CODEX_HOME` 中，我们还用 Codex `0.144.6` 验收了官方删除行为：

- index-only（`threads` 行存在、rollout 缺失）可由 `thread/delete` 清除，无需手改 SQLite；
- 合成的有效 rollout-only（rollout 存在、`threads` 行缺失）也可由 `thread/delete` 清除并返回 `{}`；
- 一个完全不存在、既无索引也无 rollout 的 ID 返回 `-32600` / `no rollout found`，不会被误报为成功。

这说明官方接口具备修复部分不一致状态的能力；是否自动调用仍取决于 Janitor 对来源、父子关系和冲突证据的安全判断。

当前 OpenAI 官方 App Server 文档进一步明确：`thread/delete` 永久删除活动或归档
thread 及其 spawned descendants，并在成功返回前移除现有 rollout 和关联原生
metadata；rollout 已缺失时按已删除处理。Desktop 私有宿主目录不在公开协议的字段或
存储结构中，因此 Janitor 将其作为独立、版本探测的残留层，不把私有表结构声称为
OpenAI 稳定 API。

## 支持范围

| 范围 | 检查依据 | 当前候选动作 |
|---|---|---|
| AionUI | 前端对话已不存在，但 `acp_session` reference 仍在；并要求 Codex backend/originator 证据 | Codex thread 与精确映射分成独立动作；映射仅以主键或重验证的 `rowid + 完整行指纹` 删除 |
| Cindy | 已失效的当前 `sdk_session_id`，或历史 `agent_switch` 中的 `fromSdkSessionId` | 当前引用只清空该字段；历史引用只移除绑定消息 ID 与内容哈希的该 JSON 字段 |
| Pi Agent | standalone 及每个 Cindy `<profile>/pi-agent-home/sessions` 的有界 JSONL 盘点 | 逐个精确删除可选 JSONL；live Cindy current/historical 引用阻止删除 |
| Claude Code | effective config root 及可确定归属的 Cindy `claude-home`/默认 root | 逐 session 删除精确 manifest；共享配置、memory/history/index 保留 |
| Codex：index-only | thread 列表记录存在，但 rollout 内容文件不存在 | 删除整个 thread，通常为 `low` |
| Codex：rollout-only | rollout 内容文件存在，但 thread 列表记录不存在 | 删除整个 thread，属于 `high`，必须明确选择 |
| Codex：重复内容文件 | 同一 thread ID 有多份可验证 rollout 内容文件 | 保留，或把全部已确认副本作为整条 thread 的 `high` 风险删除范围；不提供隔离动作 |
| Codex：路径错位 | thread 列表路径与实际 rollout 内容文件不一致，但文件身份可验证 | 保留，或删除整条 thread；不提供猜测性的路径修复 |
| Codex：孤立关联任务 thread | 明确 subagent 证据，父 thread 的列表记录和 rollout 内容文件均缺失 | 删除整个 thread；必须展示它创建的其他关联任务 thread |
| Codex：残留关联记录 | thread 关联记录的一端不存在 | 按 parent、child、status、schema 与完整行指纹精确删除一条关系边；不唯一或仍 open 时阻止该条 |
| Codex：旧版聚合索引残留 | `session_index.jsonl` 中的 ID 经严格 SQLite 与活动/归档 rollout 清单证明已无 live thread | 精确移除已证明失效的原始整行；写入前临时备份，验证成功后立即删除 |
| Codex Desktop：宿主状态孤儿 | `local_thread_catalog` 中 `host_id='local'` 的精确 ID 仍存在，但 `state_5.sqlite` 和有效 rollout 均不存在 | `remove_desktop_state`；客户端关闭、完整指纹、SQLite/JSON 原子写入和临时回滚保护 |

所有发现都会成为 Observation，并显示一个或多个 CandidateAction。可执行 mutation
包括整条 Codex thread 删除、旧索引残留清除、Desktop 宿主残留清除、精确关系边删除、
AionUI/Cindy 引用清理，以及 Pi/Claude 的精确 session 删除。`repair_index_path` 和
`quarantine_artifacts` 只保留旧 JSON 枚举兼容，不再生成或提供；异常记录只能保留，
或在身份与完整范围均可验证时删除整条记录。

## 平台与 Shell

这是 Python 3.10+ CLI，不需要额外编写或安装 `.cmd`、`.bat`、`.sh` 包装脚本。PowerShell、`cmd.exe` 和 macOS/Linux 的 sh 只是不同入口；扫描、计划、官方 `thread/delete` 和删除后验证都由同一套 Python 实现完成。

支持边界：

- native 扫描可在 Windows、macOS 和 Linux 使用，前提是 Codex 使用标准 `~/.codex` 数据布局，且可信的 `codex` 可执行文件位于 `PATH`；
- AionUI 和 Cindy 的默认数据库、数据目录及捆绑 Codex 自动发现目前主要针对 Windows；
- 其他平台上的第三方前端路径尚无公开 CLI 支持承诺，不应依赖隐藏参数或内部程序接口建立自动化。
- Pi Agent 在 Windows、macOS 和 Linux 均使用其公开的本地会话布局；默认目录为 `~/.pi/agent`，会话目录为 `~/.pi/agent/sessions`。会话目录的优先级为 `--pi-session-dir`、`PI_CODING_AGENT_SESSION_DIR`、合并后的项目/全局 `settings.json` 的 `sessionDir`、再到 `<agentDir>/sessions`；agent 目录本身按 `--pi-agent-dir`、`PI_CODING_AGENT_DIR`、`~/.pi/agent` 解析。`--pi-agent-dir` 不会覆盖明确的 session directory。
- Claude Code 的普通 `~/.claude`/`CLAUDE_CONFIG_DIR` 清单和逐项删除可在 Windows、macOS 和 Linux 使用；Cindy profile 的自动发现仍受上一条 Windows 边界约束。

安装后通常使用 `local-agent-record-janitor` 命令。如果 pip 安装的 console script 尚未进入 `PATH`，所有示例都可以改用 `python -m local_agent_record_janitor`（macOS/Linux 常用 `python3 -m local_agent_record_janitor`）。

### Windows PowerShell

```powershell
git clone https://github.com/SchweppesSoda/local-agent-record-janitor.git
Set-Location local-agent-record-janitor
python -m pip install -e .

local-agent-record-janitor scan --platform native
local-agent-record-janitor clean --platform native --thread-id 0198abcd
local-agent-record-janitor clean --platform native --thread-id 0198abcd --yes

# PATH fallback
python -m local_agent_record_janitor scan --platform native
```

### Windows cmd.exe

```bat
git clone https://github.com/SchweppesSoda/local-agent-record-janitor.git
cd /d local-agent-record-janitor
python -m pip install -e .

local-agent-record-janitor scan --platform native
local-agent-record-janitor clean --platform native --thread-id 0198abcd
local-agent-record-janitor clean --platform native --thread-id 0198abcd --yes

rem PATH fallback
python -m local_agent_record_janitor scan --platform native
```

### macOS / Linux sh

```sh
git clone https://github.com/SchweppesSoda/local-agent-record-janitor.git
cd local-agent-record-janitor
python3 -m pip install -e .

local-agent-record-janitor scan --platform native
local-agent-record-janitor clean --platform native --thread-id 0198abcd
local-agent-record-janitor clean --platform native --thread-id 0198abcd --yes

# PATH fallback
python3 -m local_agent_record_janitor scan --platform native
```

## 快速开始

先关闭 AionUI、Cindy 和正在使用同一 `CODEX_HOME` 的 Codex 客户端，再扫描：

```powershell
local-agent-record-janitor scan
```

如果目标是一次清理原生 Codex、Cindy 和 AionUI 中当前所有**可执行异常残留**，在
完全退出 Codex/ChatGPT Desktop、Cindy 和 AionUI 后，从外部 PowerShell 运行：

```powershell
local-agent-record-janitor purge --yes --clients-closed
```

`purge` 不会删除正常对话，也不会触碰 Pi 或 Claude Code 会话。它按物理存储和 mutation
kind 拆成不可变批次，依次处理 thread、旧索引、Desktop 状态、关系边和前端引用；每批
绑定当次完整计划指纹，执行前完整重扫，执行中只做定点 guard，执行后再完整扫描。
共享 SQLite/JSON 写入前只创建临时回滚副本，验证成功后立即删除。任何完整扫描失败、
计划漂移或执行错误都会停止后续批次；受阻动作保留并计数，并以退出码 `3` 和
`goal_satisfied=false` 明确表示目标未完成，
不会再显示为成功。可用 `--platform native|cindy|aionui` 把候选范围缩小到单一
来源，但所有已发现前端仍参与 live-reference 安全检查。

由 Agent 执行时使用专用的非交互协议，不要模拟人类提示：

```powershell
local-agent-record-janitor agent doctor --platform native --codex-home 'D:\exact\CODEX_HOME'
local-agent-record-janitor agent plan --operation purge --platform native `
  --codex-home 'D:\exact\CODEX_HOME'
local-agent-record-janitor agent apply --plan '<plan 输出中的 plan_path>' `
  --authorized-plan-sha256 '<plan-sha256>' --clients-closed
local-agent-record-janitor agent status --operation-id '<operation-id>' `
  --codex-home 'D:\exact\CODEX_HOME'
local-agent-record-janitor agent verify --operation-id '<operation-id>' `
  --codex-home 'D:\exact\CODEX_HOME'
```

Agent 命令只输出 JSON、不读取 stdin；计划只授权一个不可变动作批次。省略 `--out`
时，计划写入用户状态目录而不是项目根目录；显式指定的计划文件由调用方管理。`apply`
在触发修改前持久化 mutation gate，结果为 `unknown` 时会拒绝重复发送删除，只能通过
`status`/`verify` 收口。已知终态只保留最长 7 天、无正文的最小回执。完整约定见 [AGENTS.md](AGENTS.md) 和
[Agent automation protocol](docs/agent-automation.md)。

列出全部正常/异常 Codex thread 及 Cindy/AionUI frontend reference：

```powershell
local-agent-record-janitor records
local-agent-record-janitor records --platform cindy --limit 50
local-agent-record-janitor records --thread-id 0198abcd
local-agent-record-janitor records --json
```

`records` 只读聚合 `state_5.sqlite`、活动/归档 rollout、旧索引、可探测的 Codex
Desktop 宿主目录以及 frontend reference。人类输出受 `--limit` 限制；JSON 始终
完整，不受 `--limit` 截断。未分配
Codex thread ID 的前端记录也会显示，但不能删除；已知 Cindy profile 即使只剩独立
`codex-home`、前端数据库已经移除，也仍会作为 native store 纳入默认清单。

Pi session 使用独立清单，不会伪装成 Codex native store。只查看 Pi：

```powershell
local-agent-record-janitor records --platform pi
local-agent-record-janitor records --platform pi --pi-agent-dir "$HOME/.pi/agent"
local-agent-record-janitor records --platform pi --pi-session-dir 'D:\PiSessions' --json
```

默认或 `--platform all` 的 `records --json` 保留既有 Codex `records`、`errors` 和 `count` 字段，并另外包含 `pi_sessions`、`pi_failures`、`pi_count` 与 `total_count`。Pi 只提取结构/展示所需元数据：会话 ID、精确文件路径与状态、版本、时间、cwd、父/子会话关系、会话名称、provider/model 以及是否使用 OpenAI Codex；不保留或输出 message、thinking、tool 参数或聊天正文。默认布局只扫描 `root/<project>/*.jsonl`，自定义 sessionDir 扫描 `root/*.jsonl`；兼容模式最多检查两层，绝不无界递归。

Claude Code session 使用独立清单：

```powershell
local-agent-record-janitor records --platform claude
local-agent-record-janitor records --platform claude --claude-config-dir "$HOME/.claude" --json
local-agent-record-janitor delete --platform claude --session-id 完整UUID --json
```

Claude config root 按 `--claude-config-dir`、`CLAUDE_CONFIG_DIR`、`~/.claude` 解析。默认/all JSON 另含 `claude_sessions`、`claude_failures`、`claude_count`，`total_count = count + pi_count + claude_count`。Claude Code 当前没有本地逐 session 官方删除命令；官方 `claude project purge` 是项目级 purge，不能用于本工具的逐项选择。因此本工具执行精确文件 manifest 删除：除 transcript 和旧式 `<root>/<session-id>` 路径外，还支持当前的 `debug/<session-id>.txt` 与严格匹配的 `todos/<session-id>-agent-<safe-token>.json` 普通文件。相似前缀、其他 session、credentials、settings、plugins、skills、agents、commands、project memory、`CLAUDE.md`、stats cache 和共享 history/index 均保留；这不等于清空整个 Claude 配置。

来源视图有明确边界：`--platform cindy` 显示 Cindy 专用 `codex-home` 中的全部
Codex thread 及其 frontend reference，即使 SQLite reference 已经消失；
`--platform aionui` 因与原生 Codex 共享 native store，只显示有明确 AionUI reference
的 thread 和未映射行；`--platform native` 显示原生 `CODEX_HOME` 的完整底层清单；
默认 `all` 显示三者并集。`delete` 使用同样的可选目标视图，但安全盘点始终读取相关
native store 的完整状态，并把所有已发现的 Cindy/AionUI adapter 保留为 frontend
reference guard，不会因为显示过滤而放宽删除检查。

TTY 中逐项选择任意记录永久删除：

```powershell
local-agent-record-janitor delete
```

输入编号或范围（例如 `1,3-4`）；`delete` 不接受 `all`。Codex 最终计划会展示完整
根 thread ID、所有级联关联任务 thread、绝对数据目录、计划指纹和不会被删除的
Cindy/AionUI frontend reference。输入专用确认句 `客户端已关闭并确认永久删除` 后，
工具会重建清单、精确比较批准指纹，再调用官方接口。

非 TTY/自动化必须先明确目标做一次预览；预览退出码为 `2`，JSON 中包含所选范围的 `plan_fingerprint`：

```powershell
local-agent-record-janitor delete --thread-id 0198abcd --json

local-agent-record-janitor delete --action-id 完整动作ID `
  --plan-fingerprint 完整所选计划指纹 `
  --clients-closed --yes --json
```

非 TTY 执行缺少明确的 `--thread-id`/完整 `--action-id`、匹配的计划指纹、
`--clients-closed` 或 `--yes` 中任一条件都会停止且零修改。`--yes` 不能代替目标
选择；执行前清单、级联范围、native artifact 或 frontend reference 快照发生变化也会停止。

Pi 删除使用 Pi 的公开语义：永久删除一份精确批准的本地 `.jsonl`，不会调用 Codex `thread/delete`，不会读取或修改 `auth.json`，也不会修改 `settings.json` 或任何服务端历史。为解析 session 目录优先级，工具只读 `settings.json` 的 `sessionDir`。它与 Codex 删除不能混用：

```powershell
# 先生成只读 JSON 预览，获得 session_id/action_id 和 plan_fingerprint
local-agent-record-janitor delete --platform pi --session-id 完整Pi会话ID --json

# 非 TTY 执行：必须绑定预览指纹并声明 Pi 客户端已关闭
local-agent-record-janitor delete --platform pi --session-id 完整Pi会话ID `
  --plan-fingerprint 完整所选计划指纹 --clients-closed --yes --json
```

TTY 可在 Pi 清单显示后输入一个临时编号；不支持 `all`。默认还须输入 `Pi 客户端已关闭并确认永久删除`；只有同时显式提供 `--yes`、明确目标和 `--clients-closed` 才会跳过该提示。若继承的 `PI_SESSION_FILE` 标记目标为活动会话，或文件状态在预览后变化、目标不是常规 `.jsonl` 会话文件，命令会停止；其他仍在运行的 Pi 进程需由操作者通过 `--clients-closed` 或 TTY 确认保证已关闭。工具不会为 Pi session 创建备份。

仅扫描一个来源：

```powershell
local-agent-record-janitor scan --platform aionui
local-agent-record-janitor scan --platform cindy
local-agent-record-janitor scan --platform native
```

输出机器可读 JSON：

```powershell
local-agent-record-janitor scan --platform all --json
```

按完整 Codex thread ID（`thread_id`）或唯一前缀缩小范围：

```powershell
local-agent-record-janitor scan --thread-id 0198abcd
```

交互查看所有问题。输出按保存位置列出候选目标；每个目标展示 thread 名称、项目与
完整 cwd、完整 thread ID、根/级联关系、子代理名称/角色/路径、父 thread ID、
索引/归档/originator、元数据来源与冲突。每个候选动作都有临时编号：

```powershell
local-agent-record-janitor clean
```

可输入 `1,3-5` 明确选择动作；输入 `all` 只会选择允许批量纳入的 `low` 风险动作。标记为“必须逐项选择”的动作即使是 `low` 风险，也不会被 `all` 纳入。临时编号只在本次扫描和当前进程有效，不能保存给自动化使用。

TTY 交互会在同一进程中完整展示最终计划。Codex thread 硬删除要求确认词
`确认删除`；旧索引修复要求先关闭相关客户端并输入 `客户端已关闭并确认修复`。
确认后立即重新扫描并执行。输入其他内容或遇到 EOF 都会取消且零修改。`--yes`
只跳过最终提示，不会跳过编号/目标选择及其他安全条件。

按完整 Codex thread ID（`thread_id`）或唯一前缀选择单个目标；TTY 仍会展示最终计划并要求确认：

```powershell
local-agent-record-janitor clean --thread-id 0198abcd
```

自动化应先读取计划 JSON，再使用其中的完整稳定 action ID：

```powershell
local-agent-record-janitor clean --json
local-agent-record-janitor clean --action-id delete_conversation-完整动作ID
```

确认目标和影响范围无误后才执行：

```powershell
local-agent-record-janitor clean --thread-id 0198abcd --yes
local-agent-record-janitor clean --action-id delete_conversation-完整动作ID --yes
```

非 TTY 执行 `review` 或 `high` 风险动作时，还必须把刚才计划 JSON 的完整 `plan_fingerprint` 原样传回；执行前的全局计划或所选动作范围发生变化都会停止：

```powershell
local-agent-record-janitor clean --action-id delete_conversation-完整动作ID `
  --plan-fingerprint 完整计划指纹 --yes
```

旧版聚合索引是文件资源，不是虚构的 Codex thread ID，不能用 `--thread-id` 选择、
不能被 `all` 纳入，也不能与 thread 删除混跑。关闭使用同一数据目录的 Codex、
AionUI 和 Cindy 后，可用计划中的兼容 action kind `repair_legacy_index` 精确移除已证明
失效的原始整行：

```powershell
local-agent-record-janitor clean --platform native --json
local-agent-record-janitor clean --action-id repair_legacy_index-完整动作ID `
  --plan-fingerprint 完整计划指纹 --clients-closed --yes
```

写入前会创建临时回滚副本；写入和重新读取均验证成功后立即删除。0.2.0 不提供
`restore-legacy-index` 或长期备份库。执行结果为 `partial`/`unknown` 或自动回滚失败时，
副本才暂时保留，等待 `verify` 收口。

原生记录已经消失、但 Codex Desktop 侧栏/目录状态仍残留时，先完全退出
Codex/ChatGPT Desktop、AionUI、Cindy，再从外部 PowerShell 获取高风险动作和计划
指纹，并保持这些客户端关闭直到执行完成。关闭客户端可能触发最后一次状态写入，
所以不要在关闭前复制计划指纹：

```powershell
local-agent-record-janitor clean --platform native --json

local-agent-record-janitor clean `
  --action-id remove_desktop_state-完整动作ID `
  --plan-fingerprint 完整计划指纹 `
  --clients-closed --yes --json
```

执行会先创建 Desktop catalog 与全局状态文件的一致临时回滚副本。任何写入或执行后
验证失败都会尝试自动还原；成功写入或成功自动回滚后副本立即删除。只有结果仍不明或
回滚失败时才暂时保留，并在错误中报告精确路径。

非 TTY 中，`clean` 不传 `--yes` 时只预览计划且不会修改数据；有可执行动作进入计划时以退出码 `2` 表示“需要显式确认”。只有 stdin 和 stdout 同时为 TTY 时才会进入上述同进程选择与确认流程。`--yes` 不能替代 `--thread-id`、完整 `--action-id` 或当前进程中的交互编号，也不能跳过删除前重验证。无人值守执行没有选择器时会被拒绝；无人值守预览没有选择器时最多默认计划允许批量纳入的 `low` 风险删除动作，不会纳入标记为“必须逐项选择”的动作。

如果默认 `~/.codex` 不是目标保存位置，应明确目录后重新扫描，让计划为该 StorageLocation 生成动作；无法确定归属时不会自动删除：

```powershell
$env:CODEX_HOME = 'D:\CodexData'
local-agent-record-janitor scan --platform native

local-agent-record-janitor scan --platform native --codex-home 'D:\CodexData'
local-agent-record-janitor clean --platform native --codex-home 'D:\CodexData' `
  --codex-bin 'C:\path\to\codex.exe' --thread-id 0198abcd
```

`--codex-home` 表示本次命令显式扫描的一个 Codex 数据目录；自动发现多个位置时仍按不同 StorageLocation 展示和选择，不能只凭相同 ID 合并。

若多个扫描来源为同一数据目录提供了不同的 Codex 可执行文件，工具不会任选一个执行，而会阻止该目录的清理。请核对后用 `--codex-bin PATH` 明确指定；显式路径必须是现存普通文件，否则命令会在扫描前报错。

## 删除是怎样完成的

本工具不会用手写 SQL 代替官方 Codex thread 删除。可安全处理的原生 Codex thread
会按各自的 `CODEX_HOME` 启动对应 harness runtime：

```text
initialize → initialized → thread/delete → 删除后验证
```

官方 `thread/delete` 会硬删除活动或已归档 thread，并一同删除由该 thread 创建的
关联任务 thread；rollout 已缺失时会按“已删除”处理。删除后验证还会检查可探测的
Codex Desktop 宿主目录/UI 精确引用；它们仍存在时结果为 `partial`，不会误报完整
删除。详见
[Codex app-server API](https://developers.openai.com/codex/app-server/)。

Pi 没有对应的 Codex app-server 删除 API。Pi 上游将会话保存为 `sessions/` 下的单个 JSONL，且 `/resume` 的删除操作也是删除该文件；本工具仅在批准后删除该精确文件。依据：[Pi Session File Format](https://github.com/earendil-works/pi/blob/main/packages/coding-agent/docs/session-format.md)。

这也意味着：

- 删除不可撤销；
- 删除计划会列出根 thread，以及将一同删除的关联任务 thread 数量和完整 ID；
- 每个删除根目标都有精确批准范围，包括关联任务 thread ID、列表中存在的 thread ID、
  当前存在的 rollout 内容文件路径，以及每个文件的身份、来源和 `stat` 状态指纹；
  指纹覆盖 metadata thread ID、originator、source、工作目录、时间戳、活动/归档状态、
  规范化路径、大小和纳秒修改时间；
- 启动 app-server 后、任何删除请求前会再次捕获完整范围；每个根目标在发送请求的
  最后一刻还会重查 native record 身份范围和全部活跃 frontend reference，漂移会停止后续请求；
- 经验证的重复文件或路径错位可以作为 `high` 风险整个 thread 删除逐项选择；不提供
  路径修复或可恢复隔离，删除后任何已知残留都会使结果成为 `partial`；
- 残留关联记录仅在记录指向的子 thread 仍有精确可验证的列表记录或 rollout 内容文件、
  来源无冲突时，才可提供窄范围 `high` 风险整个 thread 删除；这会删除该子 thread
  及其批准的关联任务范围。若只剩无效关系边，则 `remove_broken_relation` 只在
  parent、child、status、schema 与完整行指纹仍完全匹配时删除该一行；
- AionUI/Cindy 引用和关系边写入按一个物理数据库拆批，要求客户端关闭、schema 白名单、
  事务、精确影响行数、写后重读及临时回滚保护；
- Cindy 的 `deleted` 是前端软删除状态，但一旦对应 Codex thread 被硬删除，就不能靠
  恢复 Cindy 状态找回 Codex 正文。

## JSON 输出

`scan --json` 除 Finding 外还包含 storage-qualified 的 Codex thread 摘要目录：

```json
{
  "findings": [],
  "conversations": [],
  "errors": [],
  "count": 0
}
```

`clean --json` 在计划阶段包含保存位置、唯一 Codex thread 摘要目录、每个删除动作的
派生影响视图、Observation、候选动作、风险、影响、阻断原因、action ID 和快照指纹，例如：

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

最终确认和执行结果会显示完整 Codex thread ID；JSON 始终保留完整 ID、action ID 和
绝对路径。同一 ID 位于不同 Codex 数据目录时是不同目标，不会静默合并。绝对路径、
工作目录、thread/session ID 与错误信息可能泄露用户名、项目名或目录结构，分享前请脱敏。

## 退出码

| 退出码 | 含义 |
|---:|---|
| `0` | 扫描/记录清单成功，或所选删除/精确残留清理成功 |
| `1` | 扫描、选择、重验证、执行或执行后验证发生错误 |
| `2` | 非 TTY 的修改命令未传 `--yes`，仅预览计划，没有修改 |

“扫描到问题”本身不是命令失败，因此正常扫描仍返回 `0`。自动化应读取 JSON 的 `count` 和 `errors`，不要只看退出码。

## 重要限制

- 当前只识别有明确证据的 AionUI/Cindy/Codex/Pi/Claude 状态；数据库 schema 或 session 格式变化可能导致来源暂时不可用。
- Codex Desktop 的 catalog/全局状态是未公开的宿主实现细节。存在但结构不兼容、发现多个候选 catalog、非 `local` host、原生证据重新出现或快照漂移时一律阻止修改；没有探测到该层不代表 OpenAI 承诺它不存在。
- Finding 是 adapter 证据格式，不等于删除目标。计划生成器会把它聚合为 Observation，并根据完整当前状态生成 CandidateAction。
- 兼容期仍读取 adapter 的能力证据，但它们不是 CLI 的唯一分区依据；冲突、活跃引用、范围不明或状态读取失败都会阻止动作。
- rollout 扫描只读取首行 `session_meta`，不会解析或上传聊天正文。
- `scan` 是时间点快照。扫描后重新打开、恢复或继续某个 Codex thread 会使结果过期；
  清理前必须重新扫描。
- 不跨不同 `CODEX_HOME` 合并状态。同一个 thread ID 在不同保存位置下被视为不同目标。
- 路径错位和重复文件不应通过手工“删一个文件”解决；工具只允许保留或删除整条已确认记录。旧版聚合索引只移除已证明无 live 会话的原始整行；无效关系边只删除指纹完全匹配的一行。两者都要求审批快照、客户端关闭、原子/事务写入、写后验证和临时回滚保护。
- 扫描失败按 Codex 数据目录归属，只阻止受影响位置；无法归属到保存位置的错误按 fail closed 处理。
- 工具不删除 AionUI/Cindy 的对话正文或软删除记录；它只删除精确 AionUI 孤立映射、
  清空精确 Cindy `sdk_session_id`，或从绑定消息 ID 和内容哈希的 `agent_switch` JSON
  中移除 `fromSdkSessionId`，其他字段和消息行保持不变。
- `scan` 与 `clean` 目前不处理 Pi/Claude；显式传入对应 platform 会返回不支持错误，而 `--platform all` 保持 Codex 原有行为。

更多说明：

- [检测模型与安全边界](docs/design.md)
- [Finding 类型](docs/findings.md)
- [Adapter 贡献指南](docs/adapters.md)
- [给上游项目的修复建议](docs/upstream-fixes.md)
- [全量记录与选择性删除设计及 review](docs/selective-record-management-design.md)
- [Pi Agent 支持设计](docs/pi-agent-support-design.md)
- [多引擎本地 Agent 记录清理设计](docs/multi-engine-record-cleanup-design.md)
- [安全政策与操作清单](SECURITY.md)

## 开发

```powershell
python -m unittest discover -s tests -v
```

贡献新 adapter 前，请先阅读 [docs/adapters.md](docs/adapters.md)。任何扩大自动删除范围的修改，都必须同时提供误删反例测试。

## License

[MIT](LICENSE)
