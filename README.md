# android-mcp

本项目是一个本地 Windows MCP 服务，用于安全地检查、编辑、构建和验证 Kotlin/Android 工程。
实现按《Kotlin Android MCP 设计方案》组织，借鉴 Daofy 的插件注册、统一 action 分发、编辑保护、异步任务和结构化结果模式。

## 本地运行

```powershell
python -m android_mcp
```

如果尚未安装当前源码包，可在项目根目录执行：

```powershell
python -m pip install -e .
```

服务使用 stdio 传输。所有工具都返回 `success/data/error/meta` 结果信封；文件修改默认要求 `old_content`，并支持 `dry_run` 与历史备份。

## 当前能力

- `android_environment`：发现 JDK、SDK、ADB、Android Studio 和 Gradle Wrapper。
- `android_project`：发现模块、变体、受控任务和依赖概览。
- `android_file`：安全读取、搜索、替换、插入、删除、格式化、备份和 Kotlin import 管理。
- `android_build`：通过项目 Gradle Wrapper 异步执行受控构建任务。
- `android_device`：固定 action 的设备列表、安装、启动、停止、卸载、logcat 和截图。
- `android_task`：异步任务状态、结果、取消和长轮询。
- `android_kb`：项目源码的轻量本地检索索引。
- `get_coding_rules`、`tool_help`、`experience`：规范、帮助和本地经验库。

Tooling API bridge、UIAutomator 和向量引擎保留了清晰的扩展边界，尚未成为运行该 MVP 的必需依赖。
