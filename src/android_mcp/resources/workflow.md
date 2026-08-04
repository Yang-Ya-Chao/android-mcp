# Android MCP 工作流

1. 先用 `android_environment(action="detect")` 确认 JDK、SDK、ADB 和 Wrapper。
2. 需要改动 Kotlin、Gradle 或 XML 时使用 `android_file`，先 `dry_run=true` 再写入。
3. 写入现有文件时为每个 edit 提供 `old_content`，写入后重新读取文件。
4. 构建使用 `android_build`，失败时先查看结构化 diagnostics 和日志尾部。
