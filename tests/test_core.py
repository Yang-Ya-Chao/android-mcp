from __future__ import annotations

import asyncio
import base64
import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from android_mcp.models import AndroidMcpError
from android_mcp.server import _safe_call, create_server
from android_mcp.services.build_service import _artifacts
from android_mcp.services.device_service import (
    DeviceService,
    _encode_input_text,
    _keycode,
    _matching_nodes,
    _node_from_element,
    _parse_bounds,
    _validated_url,
)
from android_mcp.services.edit_guard import EditGuard
from android_mcp.services.file_service import FileService
from android_mcp.services.task_manager import TaskManager
from unittest.mock import patch
from subprocess import CompletedProcess
from xml.etree import ElementTree as ET


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

    def test_imports_preserve_comments_between_imports(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "Main.kt"
            source.write_text('package demo\n\nimport a.b.C\n// keep me\nimport d.e.F\n', encoding="utf-8")
            service = FileService(EditGuard())

            added = service.edit(
                project_root=str(root),
                file_path="Main.kt",
                action="imports",
                imports=["kotlin.io.println"],
                uses_action="add",
                dry_run=False,
            )
            self.assertTrue(added["success"])
            content = source.read_text(encoding="utf-8")
            self.assertIn("import kotlin.io.println", content)
            self.assertIn("// keep me", content)
            self.assertEqual(content.count("import "), 3)

            removed = service.edit(
                project_root=str(root),
                file_path="Main.kt",
                action="imports",
                imports=["a.b.C"],
                uses_action="remove",
                dry_run=False,
            )
            self.assertTrue(removed["success"])
            content = source.read_text(encoding="utf-8")
            self.assertNotIn("import a.b.C", content)
            self.assertIn("// keep me", content)
            self.assertIn("import d.e.F", content)

    def test_artifacts_module_inference(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            apk = root / "app" / "build" / "outputs" / "apk" / "debug" / "x.apk"
            apk.parent.mkdir(parents=True)
            apk.write_bytes(b"PK\x03\x04")
            artifacts = _artifacts(root, [":app:assembleDebug"])
            self.assertEqual(len(artifacts), 1)
            self.assertEqual(artifacts[0]["module"], "app")
            self.assertEqual(artifacts[0]["variant"], "debug")

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


class DeviceAutomationTests(unittest.TestCase):
    def test_ui_nodes_and_selector_matching(self) -> None:
        element = ET.fromstring(
            '<node class="android.widget.Button" text="登录" content-desc="sign in" '
            'resource-id="demo:id/login" bounds="[10,20][110,80]" clickable="true" enabled="true" />'
        )
        node = _node_from_element(element)
        self.assertEqual(_parse_bounds(node["bounds"]), (10, 20, 110, 80))
        self.assertTrue(node["interactive"])
        self.assertEqual(node["center"], {"x": 60, "y": 50})
        self.assertEqual(len(_matching_nodes([node], "登", "text", "contains")), 1)
        self.assertEqual(len(_matching_nodes([node], "demo:id/login", "resource_id", "equals")), 1)
        self.assertEqual(len(_matching_nodes([node], "login", "resource_id", "equals")), 1)

    def test_input_and_key_allowlists(self) -> None:
        self.assertEqual(_encode_input_text("hello world%"), "hello%sworld%25")
        self.assertEqual(_keycode("KEYCODE_BACK"), "KEYCODE_BACK")
        with self.assertRaises(AndroidMcpError):
            _keycode("KEYCODE_UNKNOWN")

    def test_sequence_validation_and_fixed_input_command(self) -> None:
        manager = TaskManager(max_workers=1)
        try:
            service = DeviceService(manager, object())
            service._validate_interaction_request(
                "run_sequence",
                {
                    "steps": [
                        {"action": "tap", "x": 10, "y": 20},
                        {"action": "input_text", "text": "hello"},
                        {"action": "press", "key": "ENTER"},
                        {"action": "assert_text", "text": "完成"},
                    ],
                    "max_steps": 50,
                },
            )
            with patch("android_mcp.services.device_service._run") as run:
                run.return_value = CompletedProcess(["adb"], 0, "", "")
                result = service._perform_interaction(
                    "adb",
                    "serial",
                    Path.cwd(),
                    "input_text",
                    {"text": "hello world"},
                    lambda: False,
                )
                self.assertEqual(result["exit_code"], 0)
                self.assertEqual(run.call_args.args[0][-1], "hello%sworld")
        finally:
            manager._executor.shutdown(wait=True)

    def test_directional_swipe_and_sequence_validation(self) -> None:
        service = DeviceService(object(), object())
        with patch.object(service, "_screen_size", return_value={"width": 1000, "height": 2000}):
            with patch.object(service, "_run_input", return_value={"exit_code": 0}) as run_input:
                service._perform_interaction(
                    "adb",
                    "serial",
                    Path.cwd(),
                    "swipe",
                    {"direction": "up", "distance": 500, "duration_ms": 700},
                    lambda: False,
                )
        command = run_input.call_args.args[0]
        self.assertEqual(command[-5:], ["500", "1000", "500", "500", "700"])

        service._validate_interaction_request(
            "run_sequence",
            {
                "steps": [
                    {"action": "double_tap", "x": 10, "y": 20},
                    {"action": "swipe", "direction": "left", "x": 100, "y": 200, "distance": 50},
                    {"action": "open_url", "url": "https://example.com/docs"},
                    {"action": "set_orientation", "orientation": "landscape"},
                    {"action": "package_intents", "package_name": "com.example.app"},
                    {"action": "input_text", "text": "你好", "submit": True},
                ],
                "max_steps": 50,
            },
        )
        with self.assertRaises(AndroidMcpError) as unsafe_url:
            _validated_url("javascript:alert(1)")
        self.assertEqual(unsafe_url.exception.code, "unsafe_url")

    def test_unicode_input_uses_optional_devicekit_without_shell(self) -> None:
        service = DeviceService(object(), object())
        responses = [
            CompletedProcess(["adb"], 0, "package:com.mobilenext.devicekit\n", ""),
            CompletedProcess(["adb"], 0, "", ""),
            CompletedProcess(["adb"], 0, "", ""),
            CompletedProcess(["adb"], 0, "", ""),
        ]
        with patch("android_mcp.services.device_service._run", side_effect=responses) as run:
            result = service._perform_interaction(
                "adb",
                "serial",
                Path.cwd(),
                "input_text",
                {"text": "你好"},
                lambda: False,
            )
        self.assertEqual(result["unicode_mode"], "devicekit")
        commands = [call.args[0] for call in run.call_args_list]
        self.assertEqual(commands[0][-1], "com.mobilenext.devicekit")
        self.assertIn("devicekit.clipboard.set", commands[1])
        self.assertEqual(commands[1][commands[1].index("text") + 1], base64.b64encode("你好".encode()).decode())
        self.assertEqual(commands[2][-1], "KEYCODE_PASTE")
        self.assertIn("devicekit.clipboard.clear", commands[3])

    def test_installed_package_listing_is_fixed_action(self) -> None:
        service = DeviceService(object(), object())
        with patch("android_mcp.services.device_service._run") as run:
            run.return_value = CompletedProcess(
                ["adb"],
                0,
                "package:com.example.app\npackage:com.example.app\npackage:com.example.tools\n",
                "",
            )
            result = service._list_packages("adb", "serial")
        self.assertEqual(result["packages"], ["com.example.app", "com.example.tools"])
        self.assertEqual(run.call_args.args[0][-3:], ["pm", "list", "packages"])

    def test_screenshot_can_return_embedded_image(self) -> None:
        service = DeviceService(object(), object())
        with TemporaryDirectory() as directory:
            with patch("android_mcp.services.device_service.subprocess.run") as run:
                run.return_value = CompletedProcess(["adb"], 0, b"png-bytes", b"")
                result = service._screenshot("adb", "serial", Path(directory), name="test", include_image=True)
            self.assertEqual(result["mime_type"], "image/png")
            self.assertEqual(result["image_base64"], base64.b64encode(b"png-bytes").decode())
            self.assertTrue(Path(result["path"]).is_file())


class ServerTests(unittest.TestCase):
    def test_safe_call_allows_device_screenshot_name(self) -> None:
        class Registry:
            def dispatch(self, tool_name: str, *, action: str | None, **kwargs: object) -> dict[str, object]:
                return {"tool_name": tool_name, "action": action, "name": kwargs["name"]}

        result = _safe_call(Registry(), "android_device", action="screenshot", name="home")
        self.assertEqual(result["name"], "home")

    def test_mcp_surface_and_help(self) -> None:
        server, _ = create_server()

        async def call() -> tuple[list[str], str]:
            tools = await server.list_tools()
            result = await server.call_tool("tool_help", {"tool_name": "android_file", "action": "replace"})
            if hasattr(result, "content"):
                text = result.content[0].text
            else:
                block = result[0][0] if isinstance(result, tuple) and result and isinstance(result[0], list) else result[0]
                text = getattr(block, "text", block.get("text", "") if isinstance(block, dict) else str(block))
            return [tool.name for tool in tools], text

        names, text = asyncio.run(call())
        self.assertIn("android_file", names)
        payload = json.loads(text)
        self.assertTrue(payload["success"])
        self.assertEqual(payload["data"]["action"], "replace")


if __name__ == "__main__":
    unittest.main()
