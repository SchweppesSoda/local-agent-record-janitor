# 多引擎本地 Agent 记录清理设计

> 历史说明：本文是多引擎清单与选择性删除的实施基线。0.2.0 已把 Pi/Claude 纳入
> 统一 CleanupService，并增加独立授权的 AionUI/Cindy 精确引用清理；当前安全边界以
> [design.md](design.md) 为准。

状态：实施基线

## 1. 目标

在现有 Codex 清单、选择性删除和通用 Pi 文件删除能力上，补齐多引擎 local agent
record 管理：

- 独立 Pi 与 Cindy 管理的 Pi 会话区分；
- Cindy 当前、停放及仍可用于历史 fork 的 native record frontend reference 保护；
- 独立 Claude Code 与 Cindy 管理的 Claude Code 会话清单和选择性删除；
- Codex thread、Pi JSONL、Claude Code JSONL 三种目标使用不同执行器，禁止按一个
  `session_id` 模型混删。

本阶段不直接删除或改写 Cindy 会话行。Cindy SQLite 只作为归属、生命周期和删除
保护证据。

### 1.1 规范术语

- 跨引擎对象称为 **local agent record / 原生记录**，不统称 session；
- Codex 对象称为 **thread**，其协议和稳定 ID 是 `thread/*` 与 `thread_id`；
- Pi Agent 与 Claude Code 对象称为 **session**；
- Cindy/AionUI 数据库行称为 **frontend reference/mapping**，它们只描述前端对象到
  原生记录的引用，不是原生记录副本；
- `sessions/`、`session_meta`、`sdk_session_id` 等既有文件、协议或外部 schema 名称
  按原名引用，不从其名称推导统一领域模型。

### 1.2 分层模型

每条记录的发现、展示和删除必须保持以下四层，不得把登录账号、前端 ID、harness
或存储目录折叠成一个 session 概念：

```text
Frontend namespace -- frontend reference/mapping --> Native record
Harness runtime -- operates on --> Native store -- contains --> Native record

Frontend namespace: Cindy/AionUI owner + database + frontend conversation/session ID
Harness runtime:    engine + executable path + version
Native store:       CODEX_HOME | Pi session_root | Claude config root
Native record:      Codex thread | Pi session JSONL | Claude session manifest
```

认证来源、登录状态、OAuth/API 凭据归属和前端 owner 可以作为诊断信息展示，用于解释
为什么某个 harness 能运行或获得授权；它们不进入 record identity、删除身份、
action ID 或审批 fingerprint，也不能
代替 native store 与 native record 的精确限定。

## 2. 后端限定身份

所有原生目标必须带存储和后端限定：

```text
Codex:      (codex_home, thread_id)
Pi:         (session_root, canonical_jsonl_path, header_session_id)
Claude Code:(claude_config_dir, session_id, transcript_paths[])
```

展示层可以同时显示 native、Cindy、AionUI 等来源，但不得用 frontend namespace 或
来源名称代替后端身份。
同一个 Claude Code session ID 在多个 project 目录出现时合并为一个目标，计划中保留
每个精确文件路径；不同 `CLAUDE_CONFIG_DIR` 中的同 ID 永不合并。

## 3. Cindy 引用提取

新增统一、只读的 Cindy frontend reference 提取层，读取：

1. `sessions.agent_kind/sdk_session_id` 当前绑定；
2. `messages.role='agent_switch'` 中的
   `fromAgentKind/fromSdkSessionId` 历史绑定。

DB kind 映射为：`codex -> codex`、`pi -> pi`、`cc -> claude`。frontend reference
至少带：

- Cindy 数据库、profile 根和 Cindy session ID；
- session 状态、工作目录和更新时间；
- 后端、原生 ID/路径、current 或 agent_switch 引用种类；
- boundary ID/时间/rewind 信息；
- 是否来自未删除 Cindy session。

为保证引擎切回和历史 fork，不只保护最近一次 parked 绑定。第一版保守保护所有仍属于
未删除 Cindy session 的可解析 `agent_switch` 原生引用。数据库存在但无法读取、必要
schema 不兼容或 switch JSON 隐藏了无法判定的引用时，相关 storage 的删除全部阻止。

## 4. 存储发现与归属

### 4.1 Pi

- 独立 Pi 沿用参数、环境变量、settings 和 `~/.pi/agent/sessions` 优先级；
- 对每个已发现 Cindy profile 增加 `<profile>/pi-agent-home/sessions`；
- 文件位于 Cindy root 只表示“Cindy 候选 storage”，真正归属和 live 保护仍由 DB
  引用决定；
- 同时安装独立 Pi 和 Cindy 时分别建 catalog，不能用一个 root 覆盖另一个。

### 4.2 Claude Code

- 显式 `--claude-config-dir` 优先，否则使用 `CLAUDE_CONFIG_DIR`，再否则
  `~/.claude`；
- 额外发现已有的 Cindy dev/profile `<profile>/claude-home`，但生产 Cindy 常与
  独立 Claude Code 共用默认 config root；
- 枚举 `<config>/projects/<project>/<session-id>.jsonl`，有界读取结构元数据，
  内容只流式计算指纹，不保留或输出正文；
- 用 Cindy DB 的 `cc` 引用装饰匹配 session ID。无 Cindy 引用才显示为 standalone；
  同一目标可有多条 Cindy current/switch 引用。

## 5. 删除分类

每个 Pi session、Claude Code session 和 Codex thread 统一显示以下结论之一：

| 分类 | 是否可选物理删除 | 含义 |
|---|---:|---|
| `deleted_frontend_reference` | 是 | 仅被已删除 Cindy 会话引用 |
| `unreferenced` | 是，高风险提示 | 没有任何 Cindy frontend reference 的原生记录 |
| `live_current_reference` | 否 | 未删除 Cindy 会话当前正在使用 |
| `live_historical_reference` | 否 | 未删除 Cindy 会话仍保留切回或历史 fork 引用 |
| `frontend_only` | 无物理目标 | DB 有 frontend reference，但原生文件/thread 已不存在 |
| `inventory_incomplete` | 否 | DB、路径、schema 或文件无法完整验证 |

可选只表示计划允许用户逐项批准，不表示自动删除。继续禁止 `all` 和隐式批量选择。

## 6. Claude Code 删除范围

Claude Code 当前没有逐 session 的官方本地删除命令；官方 `claude project purge`
按整个项目删除，不能用于本项目的逐项选择。因此使用独立文件执行器，删除前后均重建
catalog。

一个 Claude action 可以包含：

- 每个 project 目录中匹配的 `<session-id>.jsonl`；
- 对应 `<project>/<session-id>/` 下的 `subagents/`、`tool-results/` 等 session
  专属内容；
- `<config>/file-history/<session-id>/`、`tasks/<session-id>/`、
  `debug/<session-id>/`、`session-env/<session-id>/` 和旧 `todos/<session-id>/`
  等精确 session 专属目录（仅存在时）。
- 当前布局的普通文件 `debug/<session-id>.txt`；
- 旧 TodoWrite 普通文件
  `todos/<canonical-session-uuid>-agent-<nonempty-safe-token>.json`，文件名必须完整
  匹配；其他 session、相似前缀、空 token、unsafe token 和备份后缀均视为共享内容。

目录清单必须绑定规范路径、类型、stat、文件大小和 SHA-256 manifest。拒绝 reparse、
symlink、越界、未知节点、重复路径和枚举变化。每个 action 删除前再次比较完整 manifest，
删除后验证所有批准路径均不存在。

以下共享或身份数据永不修改：

- `.credentials.json`、设置、插件、skills、agents、commands；
- project memory、`CLAUDE.md`、`.claude.json`；
- `stats-cache.json`、共享 prompt history 和无法稳定限定到单 session 的 cache/index。

输出必须说明这些共享记录被保留，不能把“主 transcript 已删除”误报为清空整个
Claude 配置。

## 7. 执行器与并发安全

- Codex 仅调用官方 app-server `thread/delete`；
- Pi 仅精确删除获批 JSONL；
- Claude Code 仅删除获批 session manifest；
- 三种后端的 action ID、计划 fingerprint 和确认文案不同，删除命令不得混选；认证或
  登录状态的变化不得改变记录身份规则；
- 非 TTY 必须同时提供明确 selector、刚预览得到的 fingerprint、
  `--clients-closed` 和 `--yes`；TTY 默认要求后端专用永久删除确认句，只有操作者
  同时显式提供 `--yes`、明确 selector 和 `--clients-closed` 时才跳过该提示；
- 首个 mutation 前以及每个 action 前重读 Cindy frontend reference。任何目标新增未删除 Cindy
  引用、文件写入、路径变化或 catalog failure 都停止；
- 删除失败后逐项报告 `deleted`、`not_deleted` 或 `unknown`，不得继续猜测。

## 8. CLI 兼容

- 保留现有 `records/delete --platform pi`；catalog 同时显示 standalone 与 Cindy
  Pi，并明确 storage/profile/引用；
- 新增 `records/delete --platform claude`、`--session-id` 和 Claude 专用计划输出；
- 默认/all records 聚合 Codex thread、Pi session、Claude Code session，JSON 使用
  独立顶层字段，避免类型混淆；
- `scan/clean` 暂不扩展 Pi/Claude 完整性修复语义；显式指定时清晰拒绝；
- `--platform cindy` 的 Codex 视图补齐 parked/historical Codex 引用；Pi/Claude
  仍通过各自后端视图删除。

## 9. 验收矩阵

- 独立 Pi 与 Cindy Pi 同时存在，清单不合并、不漏扫；
- Cindy Pi/Codex/Claude current、parked、多次切换和历史 fork 引用均阻止误删；
- 已删除 Cindy session 的原生内容可以逐项选择；
- 共享默认 `~/.claude` 中，Cindy `cc` 引用会正确装饰目标，其他目标保持 standalone；
- Claude 同 ID 多 project 副本形成一个精确 action；
- Claude 主 transcript、subagents、tool-results 和 session 专属辅助目录完整删除；
- 凭证、设置、插件、memory、shared history/stat cache 字节级不变；
- DB/schema/JSON/权限/reparse/manifest/并发漂移任一异常均零修改；
- TTY、非 TTY、JSON、action/session selector、计划重放和混合后端拒绝均有测试；
- 全量既有测试继续通过。
