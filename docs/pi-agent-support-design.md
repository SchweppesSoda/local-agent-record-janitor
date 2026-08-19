# Pi Agent 本地会话支持设计

状态：已实现并验收

## 目标与上游依据

本项目新增 Pi Agent 的本地会话清单与单文件删除能力，而不把 Pi 伪装成 Codex thread。Pi 上游将 session 存为 JSONL：默认位于 `~/.pi/agent/sessions/--<cwd>--/<timestamp>_<uuid>.jsonl`；首行是 `type: "session"` header，带会话 UUID、时间和 cwd。上游也明确说明 `/resume` 可删除 session，操作语义是移除该 `.jsonl` 文件。

- [Pi Session File Format](https://github.com/earendil-works/pi/blob/main/packages/coding-agent/docs/session-format.md)
- [Pi Settings](https://github.com/earendil-works/pi/blob/main/packages/coding-agent/docs/settings.md)
- [Pi environment variables](https://github.com/earendil-works/pi/blob/main/packages/coding-agent/docs/environment-variables.md)
- [Pi providers（OAuth token 默认存放在 auth.json）](https://github.com/earendil-works/pi/blob/main/packages/coding-agent/docs/providers.md)

## 配置优先级

Pi agent directory 从高到低：

1. `--pi-agent-dir PATH`；
2. `PI_CODING_AGENT_DIR`；
3. `~/.pi/agent`。

Pi session directory 从高到低：

1. `--pi-session-dir PATH`；
2. `PI_CODING_AGENT_SESSION_DIR`；
3. 合并后的 `settings.json` 的 `sessionDir`（项目设置覆盖全局设置）；
4. `<agentDir>/sessions`。

默认布局只枚举 `root/<project>/*.jsonl`；自定义 sessionDir 枚举 `root/*.jsonl`。仅为了兼容上游版本差异可有限检查两层目录，禁止无界递归。`--pi-agent-dir` 只影响 agentDir 的解析，不能越过已设置的 session directory。

Pi 的相对 `sessionDir` 依照上游 settings manager 相对于本次调用的 cwd 解释；CLI 将该 cwd 交给 `pi_sessions`，不会悄悄改为 agentDir。不存在的 session root 是空清单，不是读取失败；无法读取、越界或格式不安全才生成结构化 failure。

路径解析、文件边界检查和 header 解析统一由 `pi_sessions` 公共 API 完成；CLI 不自行猜测 Pi 文件名。实现只提取结构/展示所需元数据，消息正文不保留、不输出。

## 命令体验

`records --platform pi` 只构造 Pi catalog，显示 Pi ID、路径、时间、cwd、文件状态和结构化失败；不输出消息/思维/工具参数。默认或 `--platform all` 时继续输出 Codex catalog，同时在 JSON 顶层追加 `pi_sessions`、`pi_failures`、`pi_count`、`total_count`。

`delete --platform pi` 是唯一允许 Pi mutation 的入口。可用 `--session-id` 或 Pi action ID 选择一个会话；TTY 允许临时编号，但不允许 `all`。非 TTY 必须提供显式选择器、`--plan-fingerprint`、`--clients-closed` 和 `--yes`。TTY 需输入 `Pi 客户端已关闭并确认永久删除`。删除执行器来自 `pi_delete` 公共 API，负责删除前重验证和删除后确认。

`scan`、`clean` 只要显式带 `pi` 就报不支持；Pi 与任何其他 `--platform` 组合的 `delete` 也报错。这样不会把 Codex app-server `thread/delete` 误用于 Pi，或让 Pi 单文件删除绕过 Codex 的安全链路。

## 非目标

- 不配置、读取、复制、迁移或删除 Pi OAuth/API key；`auth.json` 永不读取或修改。`settings.json` 仅只读解析 `sessionDir`，绝不修改；`models.json` 与 extensions 不读取。
- 不调用 Pi provider、Codex OAuth、Pi RPC，亦不访问服务端历史、分享链接或导出内容。
- 只提取 header 和安全验证需要的最小结构/展示元数据；不保留、索引或输出消息正文。
- 不为 Pi 实现 `scan`/`clean` 的 Codex 完整性修复语义。

## 威胁模型与安全控制

操作者须控制 Pi session root 且在删除前关闭 Pi；本地恶意进程能在枚举和删除间替换文件时，任何纯用户态工具都无法完全消除风险。实现以目录边界、普通文件与 reparse/symlink 拒绝、精确 canonical path、header ID、`stat`/内容指纹和执行前再读取降低该风险。发现变化、损坏 header、越界路径、重复/歧义 selector 或读取失败一律停止。

删除是不可恢复的。UI 应反复说明只删除一份批准 JSONL、不修改 auth/settings（settings
仅只读解析 `sessionDir`），但该 JSONL 可能含聊天正文。Janitor 不为它创建备份，输出
也不得泄露正文。

## 验收矩阵

| 场景 | 期望 |
|---|---|
| `records --platform pi` | 仅 Pi catalog、无 Codex storage、无正文 |
| `records --json` 默认/all | 保留 Codex JSON 字段并增加 Pi 字段与总数 |
| `--pi-session-dir` | 覆盖环境变量、settings 的 sessionDir 与 agentDir 默认 sessions 路径 |
| `delete --platform pi --session-id ID` | 预览含稳定 action ID 和计划指纹 |
| TTY Pi 删除 | 编号选择、专用确认词、仅删除批准文件 |
| 非 TTY Pi 删除 | 缺 selector/fingerprint/closed/yes 任一即零修改 |
| `all` 或混合平台 delete | 拒绝且零修改 |
| 目标变更、活动写入、非普通文件 | 执行前 fail closed |
| `scan/clean --platform pi` | 清晰不支持错误，退出码非零 |
