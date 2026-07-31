# Security Policy

本项目处理本地聊天记录、数据库和不可恢复的删除操作。安全目标首先是**避免误删**，其次才是回收残留数据。

## 支持状态

项目目前处于 Alpha。发布前应以仓库最新 release 说明为准；不要把未发布分支直接用于无人值守清理。

## 威胁模型

本工具假设：

- 操作者拥有所扫描账户与数据目录的权限；
- AionUI、Cindy、Codex 二进制和本地数据库不是由攻击者控制的；
- `PATH`、`CODEX_HOME`、`APPDATA`、`LOCALAPPDATA`、`COMSPEC` 等环境变量可信；
- 本机没有恶意进程在扫描和删除之间替换数据库、rollout 或 Codex 可执行文件。

若这些条件不成立，请只使用隔离环境中的 `scan`，不要在 TTY 中确认删除，也不要执行带 `--yes` 的清理。

## 删除语义

`clean` 只对用户明确选择、删除前重验证通过的 `delete_conversation` 动作调用 Codex 官方 `thread/delete`。TTY 用户输入确认词后可在同一进程执行；`--yes` 只是跳过这一步确认提示。这是硬删除：

- 活动对话和归档对话都可被删除；
- 由该对话创建的关联任务对话也会一起删除；
- 缺失 rollout 被视为已删除；
- 成功后不能由本工具恢复。

因此，一个根对话目标不等于磁盘上只删除一个 JSONL。计划必须列出完整影响范围；执行前应备份整个相关 `CODEX_HOME`，而不只是计划中显示的单个文件。

对于 Cindy，前端数据库中的 `status='deleted'` 只是软删除证据。清除对应 Codex 对话后，即使手工恢复 Cindy 的状态，也无法恢复已经硬删除的 Codex 历史。

## 安全操作清单

执行永久删除前：

1. 完全退出 AionUI、Cindy、Codex Desktop/CLI，以及使用同一 `CODEX_HOME` 的后台进程。
2. 备份相关前端数据库与完整 `CODEX_HOME`。
3. 先执行 `scan --json`，检查错误属于哪些保存位置；无法归属的错误会阻止全部动作。
4. 查看 `clean --json` 中的 Observation、CandidateAction、风险、影响、阻断理由、会话摘要目录和快照指纹。
5. 使用 `--thread-id` 从单条唯一目标开始，或把完整 `--action-id` 用作自动化选择器。
6. 核对保存位置、完整对话 ID、会话名称、项目与 cwd、子代理名称/角色/路径、父关系、列表记录、内容文件，以及将一同删除的关联任务对话 ID。
7. 确认没有活跃前端引用，并备份完整保存位置。
8. 在 TTY 中输入明确确认词 `确认删除`，或对已显式选择的目标使用 `--yes`；非 TTY 的 `review/high` 还必须提供刚审核的 `--plan-fingerprint`。程序仍会在删除前重新扫描并比较动作快照与精确范围。
9. 删除后再次扫描，并在对应前端确认仍存的对话可以正常打开。

如果前端或 Codex 在扫描后被重新打开，放弃旧计划并重新扫描。

若默认保存位置不正确，应设置 `CODEX_HOME`，或用公开的 `--codex-home PATH` 与匹配的 `--codex-bin PATH` 重新扫描。无法安全确定归属时保持数据，不会仅凭 ID 猜测目录后删除。

## 选择与风险

交互输出按保存位置和 `low`、`review`、`high`、`blocked` 风险分类。编号和范围（如 `1,3-5`）只在当前进程和本次扫描内有效；输入 `all` 只选择 `low` 风险动作。`review` 和 `high` 必须由用户明确点名并在最终计划中复核。

只有 stdin 和 stdout 同时为 TTY 时才会提示编号和最终确认；任一输出重定向都会退回纯预览，避免在操作者看不到计划时执行。最终计划显示完整 action ID。输入 `确认删除` 后在同一进程继续；取消或 EOF 保证零修改。

自动化不得保存临时编号，应使用计划 JSON 中的完整 action ID。非交互 `review/high` 审批必须同时绑定完整计划指纹。`--yes` 只跳过最终确认提示，不会选择目标、放宽风险、忽略扫描失败或绕过快照与范围重验证。

旧版聚合索引修复是独立的文件资源操作：它不能通过 `--thread-id` 选择、不会进入 `all`、不能与会话删除混跑。非交互修复还要求 `--clients-closed`；TTY 使用独立确认词。严格清单、审批指纹、每个原始行的哈希、预期输出哈希、独占锁、持久备份/清单和原子替换缺一不可。

`keep` 是有效的 no-op 决定。不可用或尚未实现的修复动作可以查看，但不会进入 mutation plan。

## 隐私

工具设计为本地运行，不读取 rollout 正文；rollout 检测只解析首行 `session_meta`。但是以下内容仍属于敏感信息：

- 完整对话/会话 ID；
- 绝对路径、用户名与工作目录；
- 前端数据库位置；
- originator、agent 类型和父子关系；
- Codex stderr 与扫描错误；
- 备份的 SQLite/JSONL，其中可能含完整聊天内容、工具输出和文件路径。

`--json` 输出适合本地自动化，不适合未经检查地上传到 issue、日志服务或聊天窗口。提交 bug 时请：

- 将用户目录改写为 `%USERPROFILE%`；
- 将对话/会话 ID 替换为稳定但虚构的 ID；
- 删除客户名、仓库名和工作目录；
- 不附带原始数据库或 rollout；
- 使用最小化、合成的复现 fixture。

## 数据完整性边界

本项目坚持以下约束：

- 扫描前端 SQLite 时使用只读连接；
- 不直接修改 Codex `state_5.sqlite` 或日志表；旧版 `session_index.jsonl` 仅允许通过下述严格、可恢复的专用修复器替换；
- 不直接删除 rollout JSONL；
- 不把损坏或不兼容数据库等同于“没有残留”后继续自动删除；
- 不把仅凭文件名、目录日期或对话 ID 形状得到的猜测作为删除依据；
- rollout-only 只有在有效 metadata、唯一 ID、精确范围已批准且无冲突时才提供可执行删除动作；
- 身份可验证的重复文件和路径错位可以提供 `high` 风险整条对话删除，但只能逐项明确选择；隔离和路径修复动作仍不可执行；
- 残留关联记录只有在其指向的同 target 子对话仍有身份精确可验证的本地数据、无来源冲突且精确范围获批时，才能提供 `high` 风险整条对话删除；这不是单独关系修复，`remove_broken_relation` 仍不可执行；
- 按 `(storage_id, full_thread_id, action_kind)` 识别动作，避免跨保存位置混淆；
- 修复列表路径、清除关系、隔离文件和清除前端引用等动作当前只显示，不会执行。

旧索引专用修复器只删除同时不在严格只读 SQLite 快照、活动 rollout 和归档 rollout 中出现的有效 ID 整行。遍历、首行 metadata、SQLite/schema、UTF-8、路径边界或稳定文件状态有任何歧义即停止。它拒绝 symlink/junction/reparse point、非普通文件、hard link 和越界路径；替换前写入私有 backup ID 目录、原文件、prepared manifest 并同步落盘。还原必须指定 backup ID，且当前文件哈希仍是该备份记录的修复后哈希；还原前也会备份当前版本。

如果官方 `thread/delete` 无法安全处理某个状态，应报告并保留数据，而不是降级为直接文件/SQLite 删除。

删除后结果必须是 `deleted`、`not_deleted`、`partial` 或 `unknown`。协议错误和超时不能代替验证：若实际范围已全部消失，应报告 `deleted` 并保留请求警告；若只消失一部分，应报告 `partial`。

## 发布安全门槛

任何可执行永久删除的版本都必须用自动化测试证明：

- 可定位到 Codex 数据目录的扫描错误只阻止该位置；无法定位的错误按 fail closed 阻止全部动作；
- 同一 Codex 数据目录发现多个不同的可执行文件候选时，不选择第一个，而是阻止该目录清理，直到用户用现存普通文件形式的 `--codex-bin PATH` 明确指定；
- `needs_quarantine=true` 时绝不调用 `thread/delete`；`details.thread_delete_supported` 不为 `true` 时原则上也不得调用。唯一窄例外只适用于该字段明确为 `false`、同 target 的 `duplicate_rollout`、`index_rollout_path_mismatch`，或关联记录指向的子对话仍有精确本地数据的 `residual_spawn_edge`：用户已逐项选择 HIGH 整条对话删除，同 target Observation 已进入 `approved_integrity_deletes`，每个 root 都有 `expected_scopes` 精确范围并通过启动后重验证。residual 授权还必须确认无 source/identity 冲突且不能授权单独关系修复。字段缺失、其他类型、其他 target 和关联任务对话均不得借此绕过；
- 同一对话 ID 仍被任一活跃前端会话引用时，软删除/残留映射不能触发删除；
- rollout originator 与前端 backend 证据冲突时 fail closed；
- 相同对话 ID 出现在多个 `CODEX_HOME` 时，选择器不会静默扩大到多个删除目标；
- 已知关联任务对话仍被前端活跃引用时，根对话删除被阻止；
- 每个删除根目标都有精确批准范围：关联任务对话 ID、列表中存在的对话 ID、当前存在的活动/归档内容文件路径，以及逐文件的身份、来源和 `stat` 状态指纹；
- app-server 启动后、任何删除请求前捕获的精确范围与已批准范围不一致时，整组发送零个删除请求；
- 每次 `thread/delete` 前重新扫描全部 live-reference guard；app-server 启动后新出现的 AionUI/Cindy 活跃引用必须停止当前及后续请求；
- 审批元数据指纹同时绑定规范化展示字段和 DB/rollout 的原始身份证据哈希，不能因另一来源仍给出相同父 ID 而忽略 source/thread_source 的丢失；
- 重复文件、路径错位和残留记录所指子对话的 HIGH 整条删除只允许对同一 target 的对应 Observation 做窄授权，不得授权关联任务或其他完整性类别；source/identity 冲突、子对话无现存本地数据或仅请求单独关系修复时不得授权；
- app-server 启动后若内容文件的 metadata 对话 ID、originator、source、工作目录、时间戳、活动/归档状态、规范化路径、大小或纳秒修改时间变化，指纹比较会阻止整组删除；
- 删除后同时验证索引和所有已知 active/archived rollout，不因重复 ID 覆盖而漏检；
- 删除结果区分 `deleted`、`not_deleted`、`partial`、`unknown`，请求错误不能跳过验证；
- 损坏、被锁或 schema 不兼容的数据库产生显式扫描错误，而不是“零发现”。

这些不是“增强功能”，而是防止真实聊天记录误删的发布阻断条件。

## 竞态与误删风险

扫描与删除之间存在时间检查/使用（TOCTOU）窗口。尤其要注意：

- 前端可能在扫描后恢复或重新关联软删除会话；
- 后台 agent 可能仍在写 rollout；
- 内容文件可能在路径和对话 ID 不变时改变来源 metadata 或文件状态；
- 对话关联关系可能在扫描后新增；
- SQLite WAL 中可能存在尚未被当前快照观察到的更新；
- `thread/delete` 删除根对话时会一同删除由该对话创建的关联任务对话。

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

不要在 SQLite 仍有写入进程时只复制主 `.db` 文件。恢复时也应先关闭所有使用这些目录的进程，并恢复为一个一致的完整快照。

旧索引修复器生成的局部备份位于对应 `CODEX_HOME/.codex-session-janitor/legacy-index-backups/<backup-id>/`。它用于精确撤销该次旧索引替换，不能代替执行会话硬删除前对完整 `CODEX_HOME` 的外部备份。

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
