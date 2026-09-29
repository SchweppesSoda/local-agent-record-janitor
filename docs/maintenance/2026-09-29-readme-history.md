# 从 README 迁出的历史记录

整理日期：2026-09-29。下文保留迁出前的观察与验证结果；日期、版本、待办和完成状态均沿用原文，不代表本次重新测试或当前现场状态。片段中的“当前”“上文”等表述属于原文语境。

来源仓库：`local-agent-record-janitor`；整理前提交：`8336facdbaffc3010a5fd341b6717d6d9b6d14c1`。

## Cindy 登录与存储观察

来源：`README.md`。

在本机 Cindy `0.1.27` 的限定观察中，登录前后 local/owner 数据库 namespace 会变化，
但 bundled Codex app-server 与 Cindy `codex-home` 不变；Cindy app login 与 OpenAI
Provider auth 是两个正交轴。本工具不会据登录或认证状态推断 native store，也不会把
owner 数据库的出现当作已证明存在跨设备记录同步。

## 初始残留复现与官方删除接口试验

来源：`README.md`。

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
