"""Compact tool/action help, loaded on demand."""

from __future__ import annotations

from typing import Any, Iterable

from ..models import AndroidMcpError, ok


class HelpService:
    def handle(self, *, tool_name: str | None = None, action: str | None = None, definitions: Iterable[Any] = (), **_: Any) -> dict[str, Any]:
        definitions = list(definitions)
        definition = next((item for item in definitions if item.name == tool_name), None) if tool_name else None
        if tool_name and not definition:
            raise AndroidMcpError(f"工具不存在：{tool_name}", code="tool_not_found")
        if not definition:
            return ok({"tools": [{"name": item.name, "description": item.description, "actions": list(item.actions)} for item in definitions]})
        actions = list(definition.actions)
        if action and actions and action not in actions:
            raise AndroidMcpError(f"工具 {tool_name} 不支持 action：{action}", code="unsupported_action", hint=f"可用 action：{', '.join(actions)}")
        examples = {
            "android_environment": {"action": action or "detect", "project_root": "D:/Android/example"},
            "android_project": {"action": action or "discover", "project_root": "D:/Android/example"},
            "android_file": {"action": action or "replace", "project_root": "D:/Android/example", "file_path": "app/src/main/java/example/MainActivity.kt", "edits": [{"start_line": 10, "end_line": 10, "old_content": "旧行", "content": "新行"}], "dry_run": True},
            "android_build": {"action": action or "assemble", "project_root": "D:/Android/example", "module": "app", "variant": "debug"},
            "android_device": {"action": action or "list", "project_root": "D:/Android/example", "serial": "emulator-5554"},
            "android_task": {"action": action or "status", "task_id": "task_xxx"},
            "android_kb": {"action": action or "search", "project_root": "D:/Android/example", "query": "MainActivity"},
        }
        return ok({"tool_name": tool_name, "action": action, "description": definition.description, "plugin": definition.plugin, "actions": actions, "extensions": list(definition.extensions), "example": examples.get(tool_name)})
