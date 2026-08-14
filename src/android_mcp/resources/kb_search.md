# 知识库检索规范

## 强制流程

1. 开发任务开始时调用 `android_environment(action="detect")` 和 `android_project(action="discover")`，确认 JDK、SDK、Gradle、模块和目标设备。
2. 用 `android_kb(action="search", scope="project")` 检索当前项目源码、Gradle、Manifest 和既有测试，先理解项目约定。
3. 用 `android_kb(action="search", scope="official")` 检索 Google/AOSP；涉及小米设备、HyperOS、权限策略、后台、通知、多窗口或大屏时，再用 `vendor="xiaomi"` 检索 Xiaomi 官方资料。
4. 对 API、兼容性、依赖、Manifest、权限、后台和设备行为等变更设置 `require_citation=true`，保存返回的 `evidence_id`。
5. 只有在检索结果足以支撑方案后，才调用 `android_file`；把 `evidence_ids` 传给写入动作。

## 查询建议

- 用业务概念和 API 名称组合查询，例如 `notification permission Android 13`、`WorkManager background`、`HyperOS autostart`。
- 用 `api_level`、`target_sdk`、`vendor`、`os_name` 缩小上下文；不确定版本时先查项目 `compileSdk`、`targetSdk` 和设备系统版本。
- 需要源码依据时检索 `scope=project`；需要平台契约时检索 `scope=official`；两者冲突时优先记录版本、适用条件和测试证据，不要臆测。
- 结果不足时先 `android_kb(action="sync_sources")`，再重新检索。同步失败不得伪造引用。

## 来源优先级

Google Android 开发者文档、Android API Reference、AOSP/AndroidX 源码与 CDD 优先；小米资料用于 HyperOS/OEM 行为和小米设备适配；项目源码和测试用于本项目约定。搜索引擎摘要、论坛和未经验证的博客只能作为线索，不能作为官方敏感变更的唯一依据。
