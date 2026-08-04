# Security Policy

Local Agent Record Janitor / 本地 Agent 记录清理器处理本地聊天记录、数据库和不可
恢复的删除操作。跨引擎对象称为原生记录：Codex 使用 thread，Pi Agent 与 Claude Code
使用 session；Cindy/AionUI 数据库行是只读 frontend reference。安全目标首先是
**避免误删**，其次才是回收残留数据。仓库、发行包和命令统一使用
`local-agent-record-janitor`，Python 模块统一使用 `local_agent_record_janitor`。

## 支持状态

项目目前处于 Alpha。发布前应以仓库最新 release 说明为准；不要把未发布分支直接用于无人值守清理。

## 威胁模型

本工具假设：

- 操作者拥有所扫描账户与数据目录的权限；
- AionUI、Cindy、Codex 二进制和本地数据库不是由攻击者控制的；
- `PATH`、`CODEX_HOME`、`APPDATA`、`LOCALAPPDATA`、`COMSPEC` 等环境变量可信；
- 本机没有恶意进程在扫描和删除之间替换数据库、rollout 或 Codex 可执行文件。
- Pi 的 `sessions/` 目录、会话 JSONL 路径和 Pi Agent 进程不受攻击者控制；`--pi-agent-dir`/`--pi-session-dir` 指向操作者预期的本地目录。
- Claude Code config root、project transcript、session 专属辅助目录和 Cindy 引用数据库不受攻击者控制；`--claude-config-dir` 指向操作者预期的本地目录。

若这些条件不成立，请只使用隔离环境中的 `scan`，不要在 TTY 中确认删除，也不要执行带 `--yes` 的清理。

## 身份与删除语义

这些实体不是一条线性身份链。关系是：

```text
Frontend namespace -- reference/mapping --> Native record
Harness runtime -- operates on --> Native store -- contains --> Native record
```

前端 owner、数据库和前端对话/会话 ID 只提供 reference；harness 二进制和版本决定由谁
执行；native store 决定读取哪个 `CODEX_HOME`、Pi session root 或 Claude config root；
最终目标必须是存储限定的 Codex thread 或 Pi/Claude session。认证来源、登录状态和
账号绑定至多绑定或授权 harness，只可用于诊断，不参与 record identity、删除身份、
action ID 或 fingerprint。

`clean` 只对用户明确选择、删除前重验证通过的 `delete_conversation` 动作调用 Codex 官方 `thread/delete`。TTY 用户输入确认词后可在同一进程执行；`--yes` 只是跳过这一步确认提示。这是硬删除：

- 活动 thread 和归档 thread 都可被删除；
- 由该 thread 创建的关联任务 thread 也会一起删除；
- 缺失 rollout 被视为已删除；
- 成功后不能由本工具恢复。

因此，一个根 thread 目标不等于磁盘上只删除一个 JSONL。计划必须列出完整影响范围；
执行前应备份整个相关 `CODEX_HOME`，而不只是计划中显示的单个文件。

对于 Cindy，前端数据库中的 `status='deleted'` 只是软删除 frontend reference 证据。
清除对应 Codex thread 后，即使手工恢复 Cindy 的状态，也无法恢复已经硬删除的 Codex 历史。

Pi 删除是另一条独立路径：只可删除清单和计划中完全匹配的一份普通 `.jsonl` 会话文件，绝不递归删除目录，绝不按文件名模糊匹配。它只提取 session 的结构与展示元数据，消息正文不保留或输出；`auth.json` 永不读取或修改；`settings.json` 仅为解析 `sessionDir` 而只读，绝不修改；`models.json` 与 extensions 不读取。Pi OAuth token、登录状态和远端服务端历史不在本项目的信任边界内。

Claude Code 删除也是独立路径。Claude Code 当前没有本地逐 session 官方删除命令；官方 `claude project purge` 只能按项目 purge，不用于逐 session 选择。本工具只删除批准 manifest 中的精确 transcript 副本和 session 专属路径，包括当前布局的 `debug/<session-id>.txt`，以及文件名严格满足 canonical session UUID、`-agent-`、非空安全 token 和 `.json` 的旧 TodoWrite 文件。相似前缀、非普通文件、link/reparse 和未知节点不会被模糊纳入；盘点不完整时删除被阻止。删除前后均重建 catalog 并比较完整 manifest。credentials、settings、plugins、skills、agents、commands、project memory、`CLAUDE.md`、`.claude.json`、stats cache 及共享 prompt history/index 永不修改。删除 transcript 不等于清空整个 Claude config root。

## 安全操作清单

执行永久删除前：

1. 完全退出 AionUI、Cindy、Codex Desktop/CLI，以及使用同一 `CODEX_HOME` 的后台进程；删除 Pi/Claude 前也退出可能写入相关 session storage 的客户端和后台进程。
2. 备份相关前端数据库与完整 `CODEX_HOME`。
3. 先执行 `scan --json`，检查错误属于哪些保存位置；无法归属的错误会阻止全部动作。
4. 查看 `clean --json` 中的 Observation、CandidateAction、风险、影响、阻断理由、Codex thread 摘要目录和快照指纹。
5. 使用 `--thread-id` 从单条唯一目标开始，或把完整 `--action-id` 用作自动化选择器。
6. 核对 native store、完整 Codex thread ID、thread 名称、项目与 cwd、子代理名称/角色/路径、父关系、列表记录、rollout 内容文件，以及将一同删除的关联任务 thread ID。
7. 确认没有活跃 frontend reference，并备份完整 native store。
8. 在 TTY 中输入明确确认词 `确认删除`，或对已显式选择的目标使用 `--yes`；非 TTY 的 `review/high` 还必须提供刚审核的 `--plan-fingerprint`。程序仍会在删除前重新扫描并比较动作快照与精确范围。
9. 删除后再次扫描，并在对应前端确认仍存的对话可以正常打开。

如果前端或 Codex 在扫描后被重新打开，放弃旧计划并重新扫描。

若默认保存位置不正确，应设置 `CODEX_HOME`，或用公开的 `--codex-home PATH` 与匹配的 `--codex-bin PATH` 重新扫描。无法安全确定归属时保持数据，不会仅凭 ID 猜测目录后删除。

## 选择与风险

交互输出按保存位置和 `low`、`review`、`high`、`blocked` 风险分类。编号和范围（如 `1,3-5`）只在当前进程和本次扫描内有效；输入 `all` 只选择 `low` 风险动作。`review` 和 `high` 必须由用户明确点名并在最终计划中复核。

只有 stdin 和 stdout 同时为 TTY 时才会提示编号和最终确认；任一输出重定向都会退回纯预览，避免在操作者看不到计划时执行。最终计划显示完整 action ID。输入 `确认删除` 后在同一进程继续；取消或 EOF 保证零修改。

自动化不得保存临时编号，应使用计划 JSON 中的完整 action ID。非交互 `review/high` 审批必须同时绑定完整计划指纹。`--yes` 只跳过最终确认提示，不会选择目标、放宽风险、忽略扫描失败或绕过快照与范围重验证。

旧版聚合索引修复是独立的文件资源操作：它不能通过 `--thread-id` 选择、不会进入
`all`、不能与 Codex thread 删除混跑。非交互修复还要求 `--clients-closed`；TTY 使用
独立确认词。严格清单、审批指纹、每个原始行的哈希、预期输出哈希、独占锁、持久
备份/清单和原子替换缺一不可。

`keep` 是有效的 no-op 决定。不可用或尚未实现的修复动作可以查看，但不会进入 mutation plan。

## 隐私

工具设计为本地运行，不读取 rollout 正文；rollout 检测只解析首行 `session_meta`。但是以下内容仍属于敏感信息：

- 完整 thread/session ID；
- 绝对路径、用户名与工作目录；
- 前端数据库位置；
- originator、agent 类型和父子关系；
- Codex stderr 与扫描错误；
- 备份的 SQLite/JSONL，其中可能含完整聊天内容、工具输出和文件路径。

`--json` 输出适合本地自动化，不适合未经检查地上传到 issue、日志服务或聊天窗口。提交 bug 时请：

- 将用户目录改写为 `%USERPROFILE%`；
- 将 thread/session ID 替换为稳定但虚构的 ID；
- 删除客户名、仓库名和工作目录；
- 不附带原始数据库或 rollout；
- 使用最小化、合成的复现 fixture。

## 数据完整性边界

本项目坚持以下约束：

- 扫描前端 SQLite 时使用只读连接；
- 不直接修改 Codex `state_5.sqlite` 或日志表；旧版 `session_index.jsonl` 仅允许通过下述严格、可恢复的专用修复器替换；
- 不直接删除 rollout JSONL；
- 不把损坏或不兼容数据库等同于“没有残留”后继续自动删除；
- 不把仅凭文件名、目录日期或 thread ID 形状得到的猜测作为删除依据；
- rollout-only 只有在有效 metadata、唯一 ID、精确范围已批准且无冲突时才提供可执行删除动作；
- 身份可验证的重复文件和路径错位可以提供 `high` 风险整个 Codex thread 删除，但只能逐项明确选择；隔离和路径修复动作仍不可执行；
- 残留关联记录只有在其指向的同 target 子 thread 仍有身份精确可验证的本地数据、无来源冲突且精确范围获批时，才能提供 `high` 风险整个 thread 删除；这不是单独关系修复，`remove_broken_relation` 仍不可执行；
- 按 `(storage_id, full_thread_id, action_kind)` 识别动作，避免跨保存位置混淆；
- 修复列表路径、清除关系、隔离文件和清除 frontend reference 等动作当前只显示，不会执行。

旧索引专用修复器只删除同时不在严格只读 SQLite 快照、活动 rollout 和归档 rollout 中出现的有效 ID 整行。遍历、首行 metadata、SQLite/schema、UTF-8、路径边界或稳定文件状态有任何歧义即停止。它拒绝 symlink/junction/reparse point、非普通文件、hard link 和越界路径；替换前写入私有 backup ID 目录、原文件、prepared manifest 并同步落盘。还原必须指定 backup ID，且当前文件哈希仍是该备份记录的修复后哈希；还原前也会备份当前版本。

如果官方 `thread/delete` 无法安全处理某个状态，应报告并保留数据，而不是降级为直接文件/SQLite 删除。

Pi 的直接文件删除是经上游公开会话格式确认的专用例外：只允许 `delete --platform pi`，只针对已解析 header、位于批准 session root 内、无 symlink/reparse-point 风险且删除前 `stat`/哈希仍匹配的普通 JSONL。任何活动会话、路径边界、文件身份或 TOCTOU 证据不完整都必须 fail closed。发布前应在 Windows 与 POSIX 合成 fixture 中覆盖：拒绝 `all`、拒绝 Pi 与其他平台混合、拒绝 auth/settings、拒绝文件替换/重写，并证明删除后仅指定 JSONL 消失。

Claude 直接文件删除只允许 `delete --platform claude`，并绑定 `(config root, session ID, exact manifest)`。它拒绝 symlink/junction/reparse、越界、未知节点、重复路径、manifest 变化和 Cindy live current/historical 引用。非 TTY 执行 Pi/Claude 删除均必须同时提供显式 selector、当前预览指纹、`--clients-closed` 和 `--yes`。

删除后结果必须是 `deleted`、`not_deleted`、`partial` 或 `unknown`。协议错误和超时不能代替验证：若实际范围已全部消失，应报告 `deleted` 并保留请求警告；若只消失一部分，应报告 `partial`。

## 发布安全门槛

任何可执行永久删除的版本都必须用自动化测试证明：

- 可定位到 Codex 数据目录的扫描错误只阻止该位置；无法定位的错误按 fail closed 阻止全部动作；
- 同一 Codex 数据目录发现多个不同的可执行文件候选时，不选择第一个，而是阻止该目录清理，直到用户用现存普通文件形式的 `--codex-bin PATH` 明确指定；
- `needs_quarantine=true` 时绝不调用 `thread/delete`；`details.thread_delete_supported` 不为 `true` 时原则上也不得调用。唯一窄例外只适用于该字段明确为 `false`、同 target 的 `duplicate_rollout`、`index_rollout_path_mismatch`，或关联记录指向的子 thread 仍有精确本地数据的 `residual_spawn_edge`：用户已逐项选择 HIGH 整个 thread 删除，同 target Observation 已进入 `approved_integrity_deletes`，每个 root 都有 `expected_scopes` 精确范围并通过启动后重验证。residual 授权还必须确认无 source/identity 冲突且不能授权单独关系修复。字段缺失、其他类型、其他 target 和关联任务 thread 均不得借此绕过；
- 同一 thread ID 仍被任一活跃 frontend reference 引用时，软删除/reference 残留不能触发删除；
- rollout originator 与前端 backend 证据冲突时 fail closed；
- 相同 thread ID 出现在多个 `CODEX_HOME` 时，选择器不会静默扩大到多个删除目标；
- 已知关联任务 thread 仍被活跃 frontend reference 引用时，根 thread 删除被阻止；
- 每个删除根目标都有精确批准范围：关联任务 thread ID、列表中存在的 thread ID、当前存在的活动/归档 rollout 路径，以及逐文件的身份、来源和 `stat` 状态指纹；
- app-server 启动后、任何删除请求前捕获的精确范围与已批准范围不一致时，整组发送零个删除请求；
- 每次 `thread/delete` 前重新扫描全部 live-reference guard；app-server 启动后新出现的 AionUI/Cindy 活跃引用必须停止当前及后续请求；
- 审批元数据指纹同时绑定规范化展示字段和 DB/rollout 的原始身份证据哈希，不能因另一来源仍给出相同父 ID 而忽略 source/thread_source 的丢失；
- 重复文件、路径错位和残留记录所指子 thread 的 HIGH 整个 thread 删除只允许对同一
  target 的对应 Observation 做窄授权，不得授权关联任务 thread 或其他完整性类别；
  source/identity 冲突、子 thread 无现存本地数据或仅请求单独关系修复时不得授权；
- app-server 启动后若 rollout 的 metadata thread ID、originator、source、工作目录、时间戳、活动/归档状态、规范化路径、大小或纳秒修改时间变化，指纹比较会阻止整组删除；
- 删除后同时验证索引和所有已知 active/archived rollout，不因重复 ID 覆盖而漏检；
- 删除结果区分 `deleted`、`not_deleted`、`partial`、`unknown`，请求错误不能跳过验证；
- 损坏、被锁或 schema 不兼容的数据库产生显式扫描错误，而不是“零发现”。

这些不是“增强功能”，而是防止真实聊天记录误删的发布阻断条件。

## 竞态与误删风险

扫描与删除之间存在时间检查/使用（TOCTOU）窗口。尤其要注意：

- 前端可能在扫描后恢复或重新关联软删除会话；
- 后台 agent 可能仍在写 rollout；
- Pi 可能在会话正在写入时追加 JSONL，或在预览后重建/替换目标文件；
- Claude Code 可能在预览后追加 transcript、新增副本/辅助路径，或 Cindy 可能新增 current/historical 引用；
- rollout 可能在路径和 thread ID 不变时改变来源 metadata 或文件状态；
- thread 关联关系可能在扫描后新增；
- SQLite WAL 中可能存在尚未被当前快照观察到的更新；
- `thread/delete` 删除根 thread 时会一同删除由该 thread 创建的关联任务 thread。

关闭相关前端只能降低风险，不能替代删除前重验证。`--yes` 不能替代显式目标选择：无人值守执行必须提供完整、唯一的 `--thread-id` 或完整 `--action-id`。在运行中进程检测和审计日志等保护完成前，不建议设置计划任务自动执行。

## 可执行文件发现

清理会启动找到的 Codex `app-server`，并将目标目录作为 `CODEX_HOME` 传入。不要：

- 在不可信 `PATH` 下以管理员身份运行；
- 把 `--codex-bin`（若当前版本提供）指向未知程序；
- 从其他用户可写目录加载 `codex.exe`、`.cmd` 或 `.bat`；
- 在未验证签名/来源时使用前端捆绑的 Codex。

优先使用已安装的官方 Codex，或对应前端随包提供且已核验来源的版本。

## 备份与恢复

推荐在前端关闭后复制：

- AionUI 数据目录中的 `aionui-backend.db` 及相关 `-wal`/`-shm`（若存在）；
- Cindy 的数据库和独立 `codex-home`；
- 标准 Codex 的完整 `.codex` 目录。
- Pi 的完整 session root（通常为 `~/.pi/agent/sessions`）；不要以删除为目的复制或暴露 `~/.pi/agent/auth.json`。

不要在 SQLite 仍有写入进程时只复制主 `.db` 文件。恢复时也应先关闭所有使用这些目录的进程，并恢复为一个一致的完整快照。

旧索引修复器生成的局部备份位于对应 `CODEX_HOME/.local-agent-record-janitor/legacy-index-backups/<backup-id>/`。它用于精确撤销该次旧索引替换，不能代替执行会话硬删除前对完整 `CODEX_HOME` 的外部备份。

## 报告漏洞

请不要在公开 issue 中提交真实聊天内容、数据库、rollout、用户名或绝对路径。

报告应至少包含：

- 受影响版本与操作系统；
- 使用的命令（敏感路径脱敏）；
- 预期与实际结果；
- 最小化合成 fixture；
- 是否发生了实际误删；
- 如果涉及路径边界，说明是否使用 junction/symlink。

在仓库启用私密漏洞报告后，请优先使用 GitHub Security Advisory 的私密报告功能。启用前，可创建不含敏感信息的 issue，请求维护者提供私密联系渠道。
