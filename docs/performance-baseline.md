# Apply 性能基线

本文件只记录匿名性能数据，不记录 thread ID、聊天正文或用户目录。

## 2026-08-19 基线

- 场景：单一 Codex 物理存储中的 53 个根删除动作。
- 总耗时：901.777 秒。
- 平均耗时：约 17.015 秒/动作。
- 已确认瓶颈：执行前已有一次完整快照，但逐 action guard 又调用完整
  adapter 扫描和完整计划构建，形成 `N × 全库扫描`。

## 重构验收目标

- 默认 planner 对每个 store 只做一次 catalog pass。
- healthy/native 路径已测为计划一次、终验一次两次 full catalog pass。
- 每个 action 只读取与其物理存储、记录身份、活动引用和关联范围有关的证据；action
  loop 不重建完整 catalog/plan/frontend。
- anomaly scanner 可有来源特定读取；两次 full catalog pass 不对所有分类作统一承诺。
  测试直接统计各路径的扫描调用次数。
