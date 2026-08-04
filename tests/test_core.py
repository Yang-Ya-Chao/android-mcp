from __future__ import annotations

import asyncio
import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from android_mcp.models import AndroidMcpError
from android_mcp.server import create_server
from android_mcp.services.edit_guard import EditGuard
from android_mcp.services.file_service import FileService
from android_mcp.services.task_manager import TaskManager


class FileServiceTests(unittest.TestCase):
    def test_preview_apply_backup_and_stale_guard(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "Main.kt"
            source.write_text('package demo\n\nfun greet() = "hi"\n', encoding="utf-8")
            service = FileService(EditGuard())

            preview = service.edit(
                project_root=str(root),
                file_path="Main.kt",
                action="replace",
                edits=[{"start_line":3, "end_line":3, "old_content":'fun greet() = "hi"', "content":'fun greet() = "hello"'}],
                dry_run=True,
            )
            self.assertTrue(preview["success"])
            self.assertTrue(preview["data"]["diff"])
            self.assertEqual(source.read_text(encoding="utf-8"), 'package demo\n\nfun greet() = "hi"\n')

            applied = service.edit(
                project_root=str(root),
                file_path="Main.kt",
                action="replace",
                edits=[{"start_line":3, "end_line":3, "old_content":'fun greet() = "hi"', "content":'fun greet() = "hello"'}],
                dry_run=False,
            )
            self.assertTrue(applied["success"])
            self.assertIn("hello", source.read_text(encoding="utf-8"))
            self.assertTrue((root / "__history").is_dir())

            with self.assertRaises(AndroidMcpError) as stale_error:
                service.edit(
                    project_root=str(root),
                    file_path="Main.kt",
                    action="replace",
                    edits=[{"start_line":3, "end_line":3, "old_content": "fun other()", "content": "fun next()"}],
                    dry_run=True,
                )
            self.assertEqual(stale_error.exception.code, "stale_edit")

            service.read(project_root=str(root), file_path="Main.kt")
            imports = service.edit(
                project_root=str(root),
                file_path="Main.kt",
                action="imports",
                imports=["kotlin.io.println"],
                uses_action="add",
                dry_run=False,
            )
            self.assertTrue(imports["success"])
            self.assertIn("import kotlin.io.println", source.read_text(encoding="utf-8"))

    def test_protected_paths_are_rejected(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "local.properties").write_text("sdk.dir=x", encoding="utf-8")
            service = FileService()
            with self.assertRaises(AndroidMcpError) as protected_error:
                service.read(project_root=str(root), file_path="local.properties")
            self.assertEqual(protected_error.exception.code, "sensitive_file")


class TaskTests(unittest.TestCase):
    def test_deduplication_and_result(self) -> None:
        manager = TaskManager(max_workers=1)
        try:
            first = manager.submit("test", lambda progress, cancelled: {"value": 1}, dedupe_key="same")
            second = manager.submit("test", lambda progress, cancelled: {"value": 2}, dedupe_key="same")
            self.assertEqual(first.task_id, second.task_id)
            result = manager.wait(first.task_id, 5)
            self.assertEqual(result["data"]["status"], "completed")
            full = manager.get(first.task_id, include_result=True)
            self.assertEqual(full["data"]["result"], {"value": 1})
        finally:
            manager._executor.shutdown(wait=True)


class ServerTests(unittest.TestCase):
    def test_mcp_surface_and_help(self) -> None:
        server, _ = create_server()

        async def call() -> tuple[list[str], str]:
            tools = await server.list_tools()
            result = await server.call_tool("tool_help", {"tool_name": "android_file", "action": "replace"})
            return [tool.name for tool in tools], result.content[0].text

        names, text = asyncio.run(call())
        self.assertIn("android_file", names)
        payload = json.loads(text)
        self.assertTrue(payload["success"])
        self.assertEqual(payload["data"]["action"], "replace")


if __name__ == "__main__":
    unittest.main()
