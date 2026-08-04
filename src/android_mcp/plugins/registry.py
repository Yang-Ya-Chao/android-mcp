"""Default plugin assembly."""

from __future__ import annotations

from typing import Any

from ..models import ToolDefinition
from ..services.file_service import FileService
from ..services.task_manager import TaskManager
from .base import AndroidPlugin, PluginRegistry


def create_registry(services: dict[str, Any]) -> PluginRegistry:
    registry = PluginRegistry()
    file_service: FileService = services["file"]
    task_manager: TaskManager = services["tasks"]

    registry.register(
        AndroidPlugin(
            name="android",
            description="Kotlin/Android project operations",
            definitions=[
                ToolDefinition(
                    name="android_environment",
                    plugin="android",
                    description="发现 JDK、Android SDK、ADB、Android Studio 与项目 Gradle Wrapper。",
                    actions=("detect", "doctor", "check"),
                    handler=services["environment_handler"],
                ),
                ToolDefinition(
                    name="android_project",
                    plugin="android",
                    description="发现 Android 工程、模块、变体、受控 Gradle 任务与依赖。",
                    actions=("discover", "info", "modules", "variants", "tasks", "dependencies", "sync", "diagnose"),
                    handler=services["project_handler"],
                ),
                ToolDefinition(
                    name="android_file",
                    plugin="android",
                    description="安全读取、搜索和编辑 Kotlin/Gradle/XML 文件，支持 diff、old_content、备份和 import 管理。",
                    actions=("read", "grep", "write", "replace", "insert", "delete", "format", "backup", "encode", "imports", "manifest", "dependencies"),
                    extensions=(".kt", ".kts", ".java", ".xml", ".gradle", ".gradle.kts"),
                    handler=services["file_handler"],
                ),
                ToolDefinition(
                    name="android_build",
                    plugin="android",
                    description="通过项目 Gradle Wrapper 异步执行受控 assemble、bundle、test、lint、check、clean 和 install。",
                    actions=("assemble", "bundle", "test", "lint", "check", "clean", "install"),
                    handler=services["build_handler"],
                ),
                ToolDefinition(
                    name="android_device",
                    plugin="android",
                    description="使用固定 action 操作 ADB 设备：列表、安装、启动、停止、卸载、logcat、截图。",
                    actions=("list", "install", "launch", "stop", "uninstall", "logcat", "screenshot", "clear_log"),
                    handler=services["device_handler"],
                ),
            ],
        )
    )
    registry.register(
        AndroidPlugin(
            name="core",
            description="MCP core services",
            definitions=[
                ToolDefinition("android_task", "核心异步任务状态、结果、取消与列表。", "core", ("list", "status", "result", "cancel"), handler=services["task_handler"]),
                ToolDefinition("android_kb", "项目 Kotlin/XML 轻量索引与检索。", "core", ("search", "stats", "build", "read"), handler=services["kb_handler"]),
                ToolDefinition("get_coding_rules", "按章节返回 Android/Kotlin 编码规则。", "core", (), handler=services["rules_handler"]),
                ToolDefinition("tool_help", "按工具与 action 返回参数说明和示例。", "core", (), handler=services["help_handler"]),
                ToolDefinition("experience", "保存、检索和维护本地问题解决经验。", "core", ("save", "search", "get", "list", "prune"), handler=services["experience_handler"]),
                ToolDefinition("code_hosting", "只读 Git 状态、日志和 diff 的安全入口。", "core", ("git_status", "git_log", "git_diff"), handler=services["git_handler"]),
                ToolDefinition("android_mcp_update", "检查当前服务版本；更新由安装器负责。", "core", ("version", "check"), handler=services["update_handler"]),
            ],
        )
    )
    return registry
