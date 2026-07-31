# 给上游项目的修复建议

Janitor 用于修复已经形成的残留。长期正确方案仍是：创建 Codex 对话的平台负责完整执行删除生命周期，并为跨数据库/文件系统失败提供可重试状态。

## AionCore v0.1.55：已本地核验的删除顺序问题

我们对安装环境和官方 `v0.1.55` 源码进行了本地核验。该版本的对话删除路径存在一个确定的顺序问题：

1. `ConversationService::delete` 先执行 `conversation_repo.delete(user_id, id)`；
2. 随后执行 `acp_session_repo.delete_for_user(user_id, id)`；
3. 但 SQLite `delete_for_user` 的删除条件包含：

   ```sql
   EXISTS (
     SELECT 1
     FROM conversations c
     WHERE c.id = acp_session.conversation_id
       AND c.user_id = ?
   )
   ```

4. 主 conversation 已经被删掉，因此该 `EXISTS` 为假，ACP 行删除数为 `0`；
5. `false` 不是数据库错误，调用处也没有检查返回的布尔值，于是残留被静默接受。

官方源码：

- [`ConversationService::delete`](https://github.com/iOfficeAI/AionCore/blob/v0.1.55/crates/aionui-conversation/src/service.rs#L2278-L2319)
- [`SqliteAcpSessionRepository::delete_for_user`](https://github.com/iOfficeAI/AionCore/blob/v0.1.55/crates/aionui-db/src/repository/sqlite_acp_session.rs#L298-L310)

同一路径也没有看到对 Codex app-server `thread/delete` 的调用，因此即使修正 ACP 行顺序，Codex 对话仍可能留下。

### 建议修复

最小修复：

- 在删除 conversation 前删除 ACP 行；或
- 增加一个仅供“所有权已经验证”路径使用的 ACP 删除方法；或
- 建立正确的外键与 `ON DELETE CASCADE`，并补迁移测试；
- 检查 `rows_affected`，预期存在 ACP 行却删除 `0` 时返回错误。

完整修复：

1. 标记 conversation 为 deleting，停止并等待运行中的 agent。
2. 在任何删除前读取并持久化 `session_id`、Codex home 和 backend。
3. 调用 Codex 官方 `thread/delete`。
4. 在一个 SQLite 事务中删除 ACP 映射、消息与 conversation。
5. 若跨系统步骤失败，写入 durable deletion outbox，由后台安全重试。
6. 只有全部完成后才向 UI 广播最终 deleted。

Codex 删除无法与前端 SQLite 放进同一个原子事务，因此 durable outbox 比“尽力而为并只写 warning”可靠。

## Cindy：软删除与物理保留

Cindy `0.1.23` 的本地核验状态是：

- UI 删除后，session 仍以 `status='deleted'` 保留；
- 消息和 Codex 对话 ID 仍可能保留；
- 对应独立 Codex home 中的索引与 rollout 仍存在。

软删除本身可以是产品策略，但应明确：

- 保留期与用户可见的恢复入口；
- 何时清除消息正文；
- 何时调用 Codex `thread/delete`；
- 恢复 tombstone 时，如果 Codex 已经硬删除，应给出不可恢复提示；
- 隐私删除请求是否跳过保留期并执行物理删除。

建议使用带状态的删除作业，例如：

```text
active → deletion_requested → codex_deleted → frontend_purged
                         ↘ retry_required
```

每一步持久化，避免应用崩溃后只完成一半。

## 所有 Codex 前端的通用建议

- 删除前停止对话活动并等待写入结束。
- 使用官方 `thread/delete`，不要只删除前端 DB 行。
- 认识到该接口会同时硬删除由根对话创建的关联任务对话。
- 将对话 ID、Codex 数据目录、前端 owner 与删除请求绑定，避免跨保存位置删除。
- 对幂等重试做测试：rollout 已缺失应仍能收敛到成功状态。
- 删除后验证索引与 active/archived rollout 均不存在。
- 为 schema 升级、崩溃点、磁盘占满、文件被锁和 app-server 超时提供集成测试。
- 提供用户可见的“立即永久删除”与“软删除/保留期”语义，而不是让 UI 的“删除”含义不确定。

官方 [Codex app-server API 概览](https://learn.chatgpt.com/docs/app-server#api-overview) 定义了 `thread/delete`。前端必须按“活动/归档对话以及由该对话创建的关联任务对话都会被硬删除”的真实语义设计确认文案、恢复策略和失败重试。

Janitor 会把前端残留、Codex 对话和候选动作分开建模。清除前端残留引用目前只会结构化显示，不会随 Codex 对话删除自动执行；上游不能把 Janitor 的 `thread/delete` 当作前端数据库生命周期管理的替代品。
