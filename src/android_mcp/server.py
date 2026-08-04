"""MCP entry point and service wiring."""

from __future__ import annotations

import json
import logging
import sys
import time
from typing import Any

from mcp.server.mcpserver import MCPServer

from . import __version__
from .config import ConfigManager
from .models import AndroidMcpError, fail, ok
from .plugins.registry import create_registry
from .rules import get_rules
from .services.build_service import BuildService
from .services.device_service import DeviceService
from .services.edit_guard import EditGuard
from .services.environment import EnvironmentService
from .services.experience_service import ExperienceService
from .services.file_service import FileService
from .services.git_service import GitService
from .services.help_service import HelpService
from .services.kb_service import KnowledgeBaseService
from .services.project_service import ProjectService
from .services.task_manager import TaskManager


LOGGER = logging.getLogger("android_mcp")
STARTED_AT = time.time()


def _safe_call(registry: Any, name: str, *, action: str | None = None, **kwargs: Any) -> dict[str, Any]:
    try:
        return registry.dispatch(name, action=action, **kwargs)
    except AndroidMcpError as exc:
        return fail(exc)
    except (ValueError, KeyError) as exc:
        return fail(str(exc), code="invalid_request")
    except Exception as exc:  # pragma: no cover - MCP boundary must never crash the session
        LOGGER.exception("tool %s failed", name)
        return fail("工具执行失败。", code="internal_error", hint=str(exc))


def create_server() -> tuple[MCPServer, dict[str, Any]]:
    config = ConfigManager()
    task_manager = TaskManager()
    global_config = config.load()
    guard = EditGuard(mode=str(global_config.get("edit_guard", {}).get("mode", "warn")))
    file_service = FileService(guard)
    environment = EnvironmentService(config)
    project_service = ProjectService()
    build_service = BuildService(task_manager, environment, config)
    device_service = DeviceService(task_manager, environment, config)
    kb_service = KnowledgeBaseService(task_manager, config)
    experience_service = ExperienceService(config)
    help_service = HelpService()
    git_service = GitService()

    registry_ref: dict[str, Any] = {}
    services: dict[str, Any] = {
        "file": file_service,
        "tasks": task_manager,
        "environment_handler": lambda *, action=None, project_root=None, **kwargs: _environment_handler(environment, action, project_root, **kwargs),
        "project_handler": project_service.handle,
        "file_handler": lambda *, action=None, **kwargs: _file_handler(file_service, action, **kwargs),
        "build_handler": build_service.handle,
        "device_handler": device_service.handle,
        "task_handler": lambda *, action=None, **kwargs: _task_handler(task_manager, action, **kwargs),
        "kb_handler": kb_service.handle,
        "rules_handler": lambda **kwargs: get_rules(**kwargs),
        "help_handler": lambda **kwargs: help_service.handle(definitions=registry_ref["registry"].definitions(), **kwargs),
        "experience_handler": experience_service.handle,
        "git_handler": git_service.handle,
        "update_handler": _update_handler,
    }
    # The help handler needs the final registry, but the closure is evaluated only
    # after registration has completed.
    registry = create_registry(services)
    registry_ref["registry"] = registry
    services["registry"] = registry

    mcp = MCPServer(
        name="android-mcp",
        version=__version__,
        description="安全的 Kotlin/Android 本地 MCP 服务",
        instructions=(
            "android-mcp: Kotlin/Gradle/XML 文件必须使用 android_file；编码和构建前先 get_coding_rules；"
            "复杂工具调用前先 tool_help(tool_name, action)。"
        ),
    )

    @mcp.tool(name="android_environment", description="发现 JDK、SDK、ADB、Android Studio 与 Gradle Wrapper。", structured_output=True)
    def android_environment(action: str = "detect", project_root: str | None = None) -> dict[str, Any]:
        return _safe_call(registry, "android_environment", action=action, project_root=project_root)

    @mcp.tool(name="android_project", description="发现 Android 工程、模块、变体、任务、依赖与诊断。", structured_output=True)
    def android_project(action: str = "discover", project_root: str | None = None, module: str | None = None) -> dict[str, Any]:
        return _safe_call(registry, "android_project", action=action, project_root=project_root, module=module)

    @mcp.tool(name="android_file", description="安全读取、搜索和编辑 Kotlin/Gradle/XML 文件。", structured_output=True)
    def android_file(
        action: str = "read",
        project_root: str | None = None,
        file_path: str | None = None,
        start_line: int | None = None,
        end_line: int | None = None,
        show_line_numbers: bool = True,
        pattern: str | None = None,
        include: str | None = None,
        context: int = 0,
        count: int = 50,
        edits: list[dict[str, Any]] | None = None,
        dry_run: bool = True,
        backup: bool = True,
        allow_dirty: bool = False,
        auto_format: bool = False,
        backup_action: str | None = None,
        version: int | None = None,
        imports: list[str] | None = None,
        import_name: str | None = None,
        uses_action: str | None = None,
        from_encoding: str | None = None,
        to_encoding: str | None = None,
        manifest_operation: str | None = None,
        manifest_target: str = "application",
        attribute_name: str | None = None,
        attribute_value: str | None = None,
        dependency: str | None = None,
        configuration: str = "implementation",
        dependencies_action: str = "add",
    ) -> dict[str, Any]:
        return _safe_call(
            registry,
            "android_file",
            action=action,
            project_root=project_root,
            file_path=file_path,
            start_line=start_line,
            end_line=end_line,
            show_line_numbers=show_line_numbers,
            pattern=pattern,
            include=include,
            context=context,
            count=count,
            edits=edits,
            dry_run=dry_run,
            backup=backup,
            allow_dirty=allow_dirty,
            auto_format=auto_format,
            backup_action=backup_action,
            version=version,
            imports=imports,
            import_name=import_name,
            uses_action=uses_action,
            from_encoding=from_encoding,
            to_encoding=to_encoding,
            manifest_operation=manifest_operation,
            manifest_target=manifest_target,
            attribute_name=attribute_name,
            attribute_value=attribute_value,
            dependency=dependency,
            configuration=configuration,
            dependencies_action=dependencies_action,
        )

    @mcp.tool(name="android_build", description="通过项目 Gradle Wrapper 异步执行受控构建任务。", structured_output=True)
    def android_build(
        action: str = "assemble",
        project_root: str | None = None,
        module: str = "app",
        variant: str = "debug",
        backend: str = "auto",
        tasks: list[str] | None = None,
        timeout_seconds: int = 1800,
        confirm_release: bool = False,
        connected: bool = False,
    ) -> dict[str, Any]:
        return _safe_call(registry, "android_build", action=action, project_root=project_root, module=module, variant=variant, backend=backend, tasks=tasks, timeout_seconds=timeout_seconds, confirm_release=confirm_release, connected=connected)

    @mcp.tool(name="android_device", description="使用固定 action 操作 ADB 设备。", structured_output=True)
    def android_device(
        action: str = "list",
        project_root: str | None = None,
        serial: str | None = None,
        apk_path: str | None = None,
        package_name: str | None = None,
        activity: str | None = None,
        confirm: bool = False,
        lines: int = 200,
    ) -> dict[str, Any]:
        return _safe_call(registry, "android_device", action=action, project_root=project_root, serial=serial, apk_path=apk_path, package_name=package_name, activity=activity, confirm=confirm, lines=lines)

    @mcp.tool(name="android_task", description="查询、长轮询、获取结果或取消异步任务。", structured_output=True)
    def android_task(action: str = "list", task_id: str | None = None, long_poll_seconds: float = 0.0, task_type: str | None = None, limit: int = 50) -> dict[str, Any]:
        return _safe_call(registry, "android_task", action=action, task_id=task_id, long_poll_seconds=long_poll_seconds, task_type=task_type, limit=limit)

    @mcp.tool(name="android_kb", description="项目 Kotlin/XML 轻量检索知识库。", structured_output=True)
    def android_kb(action: str = "search", project_root: str | None = None, query: str | None = None, search_type: str = "all", top_k: int = 20, rebuild: bool = False, file_path: str | None = None) -> dict[str, Any]:
        return _safe_call(registry, "android_kb", action=action, project_root=project_root, query=query, search_type=search_type, top_k=top_k, rebuild=rebuild, file_path=file_path)

    @mcp.tool(name="get_coding_rules", description="按章节返回 Android/Kotlin 编码规则。", structured_output=True)
    def get_coding_rules(section: str | None = None, language: str = "kotlin") -> dict[str, Any]:
        return _safe_call(registry, "get_coding_rules", section=section, language=language)

    @mcp.tool(name="tool_help", description="按工具和 action 返回参数说明与示例。", structured_output=True)
    def tool_help(tool_name: str | None = None, action: str | None = None) -> dict[str, Any]:
        return _safe_call(registry, "tool_help", action=action, tool_name=tool_name)

    @mcp.tool(name="experience", description="保存、检索和维护本地问题解决经验。", structured_output=True)
    def experience(
        action: str = "list",
        problem: str | None = None,
        solution: str | None = None,
        tags: list[str] | None = None,
        tools_used: list[str] | None = None,
        experience_id: str | None = None,
        query: str | None = None,
        limit: int = 20,
    ) -> dict[str, Any]:
        return _safe_call(registry, "experience", action=action, problem=problem, solution=solution, tags=tags, tools_used=tools_used, experience_id=experience_id, query=query, limit=limit)

    @mcp.tool(name="code_hosting", description="只读 Git 状态、日志和 diff。", structured_output=True)
    def code_hosting(action: str = "git_status", project_root: str | None = None, limit: int = 20) -> dict[str, Any]:
        return _safe_call(registry, "code_hosting", action=action, project_root=project_root, limit=limit)

    @mcp.tool(name="android_mcp_update", description="检查当前服务版本；更新由安装器负责。", structured_output=True)
    def android_mcp_update(action: str = "version") -> dict[str, Any]:
        return _safe_call(registry, "android_mcp_update", action=action)

    @mcp.resource("android://resources")
    def resources_index() -> str:
        return json.dumps({"resources": ["android://health", "android://coding-rules", "android://troubleshooting", "android://device-testing"], "prompts": ["android-build-workflow", "android-test-plan", "android-failure-recover"]}, ensure_ascii=False)

    @mcp.resource("android://health")
    def health() -> str:
        return json.dumps({"version": __version__, "uptime_seconds": round(time.time() - STARTED_AT, 3), "tools": [definition.name for definition in registry.definitions()], "edit_guard": guard.snapshot()}, ensure_ascii=False)

    @mcp.resource("android://coding-rules")
    def coding_rules_resource() -> str:
        return json.dumps(get_rules()["data"], ensure_ascii=False)

    @mcp.resource("android://troubleshooting")
    def troubleshooting_resource() -> str:
        return _resource_text("troubleshooting.md")

    @mcp.resource("android://device-testing")
    def device_testing_resource() -> str:
        return _resource_text("device-testing.md")

    @mcp.prompt(name="android-build-workflow", description="构建、诊断、修复和验证 Android 工程。")
    def android_build_workflow(project_root: str = "") -> str:
        return f"请按顺序执行 android_environment detect、android_project discover、android_build assemble，并在失败时阅读 diagnostics。项目根：{project_root}"

    @mcp.prompt(name="android-test-plan", description="生成 Android 测试计划。")
    def android_test_plan(project_root: str = "") -> str:
        return f"请先用 android_kb 搜索入口类和测试，再规划单元测试、Lint 与设备验证。项目根：{project_root}"

    @mcp.prompt(name="android-failure-recover", description="按错误分类恢复构建。")
    def android_failure_recover(error: str = "") -> str:
        return f"请将错误归类为环境、Gradle、Kotlin、AAPT2、Manifest、D8/R8 或依赖问题，给出最小修复并重试。错误：{error}"

    return mcp, {"registry": registry, "guard": guard, "tasks": task_manager}


def _environment_handler(environment: EnvironmentService, action: str | None, project_root: str | None, **_: Any) -> dict[str, Any]:
    if action in {None, "detect", "check"}:
        return environment.detect(project_root)
    if action == "doctor":
        return environment.doctor(project_root)
    raise AndroidMcpError(f"android_environment 不支持 action：{action}", code="unsupported_action")


def _file_handler(file_service: FileService, action: str | None, **kwargs: Any) -> dict[str, Any]:
    project_root = kwargs.get("project_root")
    file_path = kwargs.get("file_path")
    if not file_path and action != "grep":
        raise AndroidMcpError("android_file 需要 file_path。", code="missing_file_path")
    if action == "read":
        return file_service.read(project_root=project_root, file_path=file_path, start_line=kwargs.get("start_line"), end_line=kwargs.get("end_line"), show_line_numbers=kwargs.get("show_line_numbers", True))
    if action == "grep":
        if not kwargs.get("pattern"):
            raise AndroidMcpError("grep 需要 pattern。", code="missing_pattern")
        return file_service.grep(project_root=project_root, file_path=file_path if file_path else None, pattern=kwargs["pattern"], include=kwargs.get("include"), context=kwargs.get("context", 0), count=kwargs.get("count", 50))
    edit_kwargs = {
        "project_root": project_root,
        "file_path": file_path,
        "edits": kwargs.get("edits"),
        "dry_run": kwargs.get("dry_run", True),
        "backup": kwargs.get("backup", True),
        "allow_dirty": kwargs.get("allow_dirty", False),
        "auto_format": kwargs.get("auto_format", False),
        "backup_action": kwargs.get("backup_action"),
        "version": kwargs.get("version"),
        "imports": kwargs.get("imports"),
        "import_name": kwargs.get("import_name"),
        "uses_action": kwargs.get("uses_action"),
        "from_encoding": kwargs.get("from_encoding"),
        "to_encoding": kwargs.get("to_encoding"),
        "manifest_operation": kwargs.get("manifest_operation"),
        "manifest_target": kwargs.get("manifest_target", "application"),
        "attribute_name": kwargs.get("attribute_name"),
        "attribute_value": kwargs.get("attribute_value"),
        "dependency": kwargs.get("dependency"),
        "configuration": kwargs.get("configuration", "implementation"),
        "dependencies_action": kwargs.get("dependencies_action", "add"),
    }
    return file_service.edit(action=action or "read", **edit_kwargs)


def _task_handler(task_manager: TaskManager, action: str | None, **kwargs: Any) -> dict[str, Any]:
    action = action or "list"
    if action == "list":
        return task_manager.list(task_type=kwargs.get("task_type"), limit=kwargs.get("limit", 50))
    task_id = kwargs.get("task_id")
    if not task_id:
        raise AndroidMcpError(f"android_task {action} 需要 task_id。", code="missing_task_id")
    if action == "status":
        return task_manager.wait(task_id, kwargs.get("long_poll_seconds", 0)) if kwargs.get("long_poll_seconds", 0) else task_manager.get(task_id)
    if action == "result":
        return task_manager.get(task_id, include_result=True)
    if action == "cancel":
        return task_manager.cancel(task_id)
    raise AndroidMcpError(f"android_task 不支持 action：{action}", code="unsupported_action")


def _update_handler(*, action: str | None = None, **_: Any) -> dict[str, Any]:
    if action == "version" or action is None:
        return ok({"current_version": __version__, "update_available": False})
    if action == "check":
        return ok({"current_version": __version__, "update_available": False, "message": "未配置远程更新源。"})
    raise AndroidMcpError("更新由安装器负责；当前只支持 version/check。", code="update_not_available")


def _resource_text(name: str) -> str:
    from importlib import resources

    try:
        return resources.files("android_mcp").joinpath("resources", name).read_text(encoding="utf-8")
    except (FileNotFoundError, ModuleNotFoundError, OSError):
        return "资源暂不可用。"


def main() -> None:
    logging.basicConfig(stream=sys.stderr, level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    mcp, _ = create_server()
    mcp.run("stdio")
