"""Fixed-action ADB device operations and safe UI automation."""

from __future__ import annotations

import json
import re
import subprocess
import time
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any

from ..config import ConfigManager
from ..models import AndroidMcpError, ok
from ..paths import PathPolicy
from .environment import EnvironmentService
from .task_manager import TaskManager


INTERACTION_ACTIONS = {
    "tap",
    "long_press",
    "swipe",
    "input_text",
    "press",
    "wait",
    "run_sequence",
}
SEQUENCE_ACTIONS = INTERACTION_ACTIONS | {
    "assert_text",
    "screenshot",
    "screen_size",
    "ui_dump",
    "wait_for",
}
SELECTOR_TYPES = {"text", "content_desc", "resource_id", "class_name", "package"}
MATCH_MODES = {"contains", "equals"}
ALLOWED_KEYS = {
    "BACK": "KEYCODE_BACK",
    "HOME": "KEYCODE_HOME",
    "ENTER": "KEYCODE_ENTER",
    "TAB": "KEYCODE_TAB",
    "ESCAPE": "KEYCODE_ESCAPE",
    "SPACE": "KEYCODE_SPACE",
    "DEL": "KEYCODE_DEL",
    "DELETE": "KEYCODE_FORWARD_DEL",
    "DPAD_UP": "KEYCODE_DPAD_UP",
    "DPAD_DOWN": "KEYCODE_DPAD_DOWN",
    "DPAD_LEFT": "KEYCODE_DPAD_LEFT",
    "DPAD_RIGHT": "KEYCODE_DPAD_RIGHT",
    "DPAD_CENTER": "KEYCODE_DPAD_CENTER",
    "APP_SWITCH": "KEYCODE_APP_SWITCH",
    "MENU": "KEYCODE_MENU",
    "VOLUME_UP": "KEYCODE_VOLUME_UP",
    "VOLUME_DOWN": "KEYCODE_VOLUME_DOWN",
    "POWER": "KEYCODE_POWER",
}
UI_DUMP_PATH = "/sdcard/android-mcp-window.xml"


class DeviceService:
    def __init__(self, tasks: TaskManager, environment: EnvironmentService, config: ConfigManager | None = None) -> None:
        self.tasks = tasks
        self.environment = environment
        self.config = config or ConfigManager()
        self.policy = PathPolicy()

    def handle(
        self,
        *,
        action: str | None,
        project_root: str | None = None,
        serial: str | None = None,
        apk_path: str | None = None,
        package_name: str | None = None,
        activity: str | None = None,
        confirm: bool = False,
        lines: int = 200,
        x: int | None = None,
        y: int | None = None,
        x2: int | None = None,
        y2: int | None = None,
        duration_ms: int | None = None,
        text: str | None = None,
        key: str | None = None,
        selector: str | None = None,
        selector_type: str = "text",
        match: str = "contains",
        timeout_ms: int = 5000,
        poll_interval_ms: int = 250,
        wait_ms: int = 500,
        steps: list[dict[str, Any]] | None = None,
        max_steps: int = 50,
        screenshot_each_step: bool = False,
        include_xml: bool = False,
        name: str | None = None,
        **_: Any,
    ) -> dict[str, Any]:
        action = action or "list"
        if action == "list":
            return ok(self.list_devices(project_root))

        environment = self.environment.detect(project_root)["data"]
        adb = environment["adb"]["path"]
        if not adb:
            raise AndroidMcpError("未找到 adb。", code="adb_not_found", hint='先运行 android_environment(action="doctor")。')

        if action == "logcat":
            selected = self._select_serial(adb, serial)
            return ok(self._logcat(adb, selected, lines))
        if action == "screenshot":
            selected = self._select_serial(adb, serial)
            root = self.policy.root(project_root)
            return ok(self._screenshot(adb, selected, root, name=name))
        if action == "screen_size":
            selected = self._select_serial(adb, serial)
            return ok(self._screen_size(adb, selected))
        if action == "ui_dump":
            selected = self._select_serial(adb, serial)
            return ok(self._ui_snapshot(adb, selected, include_xml=include_xml))
        if action == "assert_text":
            selected = self._select_serial(adb, serial)
            self._validate_interaction_request(action, {"text": text, "selector": selector, "selector_type": selector_type, "match": match})
            snapshot = self._ui_snapshot(adb, selected)
            return ok(self._assert_text(snapshot, text=text, selector=selector, selector_type=selector_type, match=match))
        if action == "wait_for":
            selected = self._select_serial(adb, serial)
            self._validate_interaction_request(
                action,
                {
                    "text": text,
                    "selector": selector,
                    "selector_type": selector_type,
                    "match": match,
                    "timeout_ms": timeout_ms,
                    "poll_interval_ms": poll_interval_ms,
                },
            )
            return ok(
                self._wait_for(
                    adb,
                    selected,
                    text=text,
                    selector=selector,
                    selector_type=selector_type,
                    match=match,
                    timeout_ms=timeout_ms,
                    poll_interval_ms=poll_interval_ms,
                )
            )

        if action in INTERACTION_ACTIONS:
            selected = self._select_serial(adb, serial)
            root = self.policy.root(project_root)
            request = {
                "x": x,
                "y": y,
                "x2": x2,
                "y2": y2,
                "duration_ms": duration_ms,
                "text": text,
                "key": key,
                "selector": selector,
                "selector_type": selector_type,
                "match": match,
                "timeout_ms": timeout_ms,
                "poll_interval_ms": poll_interval_ms,
                "wait_ms": wait_ms,
                "steps": steps,
                "max_steps": max_steps,
                "screenshot_each_step": screenshot_each_step,
                "include_xml": include_xml,
                "name": name,
            }
            self._validate_interaction_request(action, request)
            dedupe = f"{root}|device|{selected}|{action}|{_stable_request(request)}"
            record = self.tasks.submit(
                "android_device",
                lambda progress, cancelled: self._run_interaction(
                    adb,
                    selected,
                    root,
                    action,
                    request,
                    progress,
                    cancelled,
                ),
                dedupe_key=dedupe,
            )
            return ok(
                {
                    "task_id": record.task_id,
                    "status": record.status.lower(),
                    "serial": selected,
                    "action": action,
                },
                hint="设备交互已异步提交；使用 android_task 查询结果。",
            )

        if action not in {"install", "launch", "stop", "uninstall", "clear_log"}:
            raise AndroidMcpError(f"android_device 不支持 action：{action}", code="unsupported_action")
        if action in {"uninstall", "clear_log"} and not confirm:
            raise AndroidMcpError(f"{action} 需要显式 confirm=true。", code="confirmation_required")
        root = self.policy.root(project_root)
        if action == "install":
            apk = self._apk_path(root, apk_path)
            if not package_name:
                package_name = _infer_package(root)
        else:
            apk = None
        if action in {"launch", "stop", "uninstall"} and not package_name:
            raise AndroidMcpError(f"{action} 需要 package_name。", code="missing_package")
        selected = self._select_serial(adb, serial)
        dedupe = f"{root}|device|{selected}|{action}|{package_name or apk}"
        record = self.tasks.submit(
            "android_device",
            lambda progress, cancelled: self._run_mutation(adb, selected, action, apk, package_name, activity, progress, cancelled),
            dedupe_key=dedupe,
        )
        return ok(
            {"task_id": record.task_id, "status": record.status.lower(), "serial": selected, "action": action},
            hint="设备操作已异步提交；使用 android_task 查询结果。",
        )

    def list_devices(self, project_root: str | None) -> dict[str, Any]:
        environment = self.environment.detect(project_root)["data"]
        adb = environment["adb"]["path"]
        if not adb:
            return {"devices": [], "adb": None, "message": "未找到 adb。"}
        completed = _run([adb, "devices", "-l"])
        devices = _parse_devices(completed.stdout)
        return {"adb": adb, "devices": devices, "exit_code": completed.returncode, "stderr": completed.stderr[-1000:]}

    def _select_serial(self, adb: str, serial: str | None) -> str:
        completed = _run([adb, "devices", "-l"])
        devices = _parse_devices(completed.stdout)
        if serial:
            selected = next((item for item in devices if item["serial"] == serial), None)
            if not selected:
                raise AndroidMcpError(f"找不到设备：{serial}", code="device_not_found")
        elif len(devices) != 1:
            if not devices:
                raise AndroidMcpError("没有可用 Android 设备。", code="no_device", hint="启动模拟器或连接设备后重试。")
            raise AndroidMcpError("检测到多个设备，必须指定 serial。", code="multiple_devices")
        else:
            selected = devices[0]
        if selected["state"] != "device":
            raise AndroidMcpError(f"设备不在线：{selected['serial']}（{selected['state']}）", code="device_offline")
        return selected["serial"]

    def _apk_path(self, root: Path, apk_path: str | None) -> Path:
        if not apk_path:
            candidates = sorted((root / "app" / "build" / "outputs").glob("apk/**/*.apk")) if (root / "app" / "build" / "outputs").is_dir() else []
            if not candidates:
                raise AndroidMcpError('未找到 APK，请先执行 android_build(action="assemble")。', code="apk_not_found")
            return candidates[-1].resolve()
        candidate = Path(apk_path).expanduser().resolve()
        try:
            candidate.relative_to(root)
        except ValueError as exc:
            raise AndroidMcpError("APK 必须位于项目目录内。", code="path_escape") from exc
        if candidate.suffix.lower() != ".apk" or not candidate.is_file():
            raise AndroidMcpError(f"APK 不存在或格式不正确：{candidate}", code="invalid_apk")
        return candidate

    def _run_mutation(
        self,
        adb: str,
        serial: str,
        action: str,
        apk: Path | None,
        package_name: str | None,
        activity: str | None,
        progress: Any,
        cancelled: Any,
    ) -> dict[str, Any]:
        progress(current_step="prepare", progress=10, total_steps=2, message="准备设备操作")
        if cancelled():
            return {"status": "cancelled"}
        prefix = [adb, "-s", serial]
        if action == "install":
            command = [*prefix, "install", "-r", str(apk)]
        elif action == "launch":
            component = f"{package_name}/{activity}" if activity else None
            command = [*prefix, "shell", "am", "start", "-n", component] if component else [*prefix, "shell", "monkey", "-p", package_name or "", "1"]
        elif action == "stop":
            command = [*prefix, "shell", "am", "force-stop", package_name or ""]
        elif action == "uninstall":
            command = [*prefix, "uninstall", package_name or ""]
        else:
            command = [*prefix, "logcat", "-c"]
        completed = _run(command, timeout=300)
        progress(current_step="complete", progress=100, message="设备操作完成")
        return {
            "status": "completed" if completed.returncode == 0 else "failed",
            "action": action,
            "serial": serial,
            "exit_code": completed.returncode,
            "stdout": _redact(completed.stdout)[-4000:],
            "stderr": _redact(completed.stderr)[-2000:],
        }

    def _run_interaction(
        self,
        adb: str,
        serial: str,
        root: Path,
        action: str,
        request: dict[str, Any],
        progress: Any,
        cancelled: Any,
    ) -> dict[str, Any]:
        if action == "run_sequence":
            sequence = request.get("steps") or []
            results: list[dict[str, Any]] = []
            total = len(sequence)
            for index, step in enumerate(sequence, start=1):
                if cancelled():
                    return {"status": "cancelled", "serial": serial, "completed_steps": len(results), "steps": results}
                progress(
                    current_step=f"step_{index}",
                    progress=(index - 1) * 100 / total,
                    total_steps=total,
                    message=f"执行第 {index}/{total} 步",
                )
                step_action = str(step["action"])
                result = self._perform_interaction(adb, serial, root, step_action, step, cancelled)
                if request.get("screenshot_each_step") and step_action != "screenshot":
                    result["screenshot"] = self._screenshot(adb, serial, root, name=f"step-{index}")
                results.append({"index": index, "action": step_action, "result": result})
            progress(current_step="complete", progress=100, total_steps=total, message="自动化流程完成")
            return {"status": "completed", "serial": serial, "completed_steps": total, "steps": results}

        progress(current_step=action, progress=10, total_steps=1, message=f"执行设备交互：{action}")
        result = self._perform_interaction(adb, serial, root, action, request, cancelled)
        progress(current_step="complete", progress=100, total_steps=1, message="设备交互完成")
        return {"status": "completed", "serial": serial, "action": action, "result": result}

    def _perform_interaction(
        self,
        adb: str,
        serial: str,
        root: Path,
        action: str,
        request: dict[str, Any],
        cancelled: Any,
    ) -> dict[str, Any]:
        if cancelled():
            return {"status": "cancelled"}
        prefix = [adb, "-s", serial]
        if action in {"tap", "long_press"}:
            point = self._resolve_point(adb, serial, request)
            duration = _bounded_int(request.get("duration_ms"), 800 if action == "long_press" else 0, 0, 10000, "duration_ms")
            command = [*prefix, "shell", "input", "tap", str(point[0]), str(point[1])]
            if action == "long_press":
                command = [*prefix, "shell", "input", "swipe", str(point[0]), str(point[1]), str(point[0]), str(point[1]), str(duration)]
            return self._run_input(command, action)
        if action == "swipe":
            start = self._validated_point(adb, serial, request.get("x"), request.get("y"))
            end = self._validated_point(adb, serial, request.get("x2"), request.get("y2"))
            duration = _bounded_int(request.get("duration_ms"), 300, 50, 10000, "duration_ms")
            command = [*prefix, "shell", "input", "swipe", str(start[0]), str(start[1]), str(end[0]), str(end[1]), str(duration)]
            return self._run_input(command, action)
        if action == "input_text":
            value = str(request.get("text") or "")
            return self._run_input([*prefix, "shell", "input", "text", _encode_input_text(value)], action)
        if action == "press":
            return self._run_input([*prefix, "shell", "input", "keyevent", _keycode(request.get("key"))], action)
        if action == "wait":
            wait_ms = _bounded_int(request.get("wait_ms"), 500, 0, 60000, "wait_ms")
            _sleep_with_cancel(wait_ms / 1000, cancelled)
            return {"wait_ms": wait_ms}
        if action == "screenshot":
            return self._screenshot(adb, serial, root, name=request.get("name"))
        if action == "screen_size":
            return self._screen_size(adb, serial)
        if action == "ui_dump":
            return self._ui_snapshot(adb, serial, include_xml=bool(request.get("include_xml")))
        if action == "assert_text":
            snapshot = self._ui_snapshot(adb, serial)
            return self._assert_text(
                snapshot,
                text=request.get("text"),
                selector=request.get("selector"),
                selector_type=request.get("selector_type", "text"),
                match=request.get("match", "contains"),
            )
        if action == "wait_for":
            return self._wait_for(
                adb,
                serial,
                text=request.get("text"),
                selector=request.get("selector"),
                selector_type=request.get("selector_type", "text"),
                match=request.get("match", "contains"),
                timeout_ms=request.get("timeout_ms", 5000),
                poll_interval_ms=request.get("poll_interval_ms", 250),
                cancelled=cancelled,
            )
        raise AndroidMcpError(f"不支持的设备交互：{action}", code="unsupported_action")

    def _validate_interaction_request(self, action: str, request: dict[str, Any]) -> None:
        if action == "run_sequence":
            steps = request.get("steps")
            if not isinstance(steps, list) or not steps:
                raise AndroidMcpError("run_sequence 需要非空 steps。", code="missing_steps")
            max_steps = _bounded_int(request.get("max_steps"), 50, 1, 100, "max_steps")
            if len(steps) > max_steps:
                raise AndroidMcpError(f"步骤数不能超过 {max_steps}。", code="too_many_steps")
            for step in steps:
                if not isinstance(step, dict) or not step.get("action"):
                    raise AndroidMcpError("每个自动化步骤都必须包含 action。", code="invalid_step")
                step_action = str(step["action"])
                if step_action not in SEQUENCE_ACTIONS or step_action == "run_sequence":
                    raise AndroidMcpError(f"流程不支持步骤 action：{step_action}", code="unsupported_action")
                self._validate_interaction_request(step_action, step)
            return
        if action not in SEQUENCE_ACTIONS:
            raise AndroidMcpError(f"不支持的设备交互：{action}", code="unsupported_action")
        selector_type = str(request.get("selector_type") or "text")
        if selector_type not in SELECTOR_TYPES:
            raise AndroidMcpError(f"不支持的 selector_type：{selector_type}", code="invalid_selector")
        match = str(request.get("match") or "contains")
        if match not in MATCH_MODES:
            raise AndroidMcpError(f"不支持的 match：{match}", code="invalid_match")
        if action in {"tap", "long_press"}:
            has_selector = bool(request.get("selector"))
            has_coordinates = request.get("x") is not None or request.get("y") is not None
            if has_selector == has_coordinates:
                raise AndroidMcpError("tap/long_press 必须二选一提供 selector 或 x/y。", code="invalid_coordinates")
            if has_coordinates:
                _as_int(request.get("x"), "x")
                _as_int(request.get("y"), "y")
            if action == "long_press":
                _bounded_int(request.get("duration_ms"), 800, 100, 10000, "duration_ms")
        elif action == "swipe":
            for field in ("x", "y", "x2", "y2"):
                _as_int(request.get(field), field)
            _bounded_int(request.get("duration_ms"), 300, 50, 10000, "duration_ms")
        elif action == "input_text":
            value = request.get("text")
            if not isinstance(value, str) or not value or len(value) > 2048 or "\x00" in value or "\r" in value or "\n" in value:
                raise AndroidMcpError("input_text 的 text 必须是 1-2048 个不含换行/null 的字符。", code="invalid_text")
        elif action == "press":
            _keycode(request.get("key"))
        elif action == "wait":
            _bounded_int(request.get("wait_ms"), 500, 0, 60000, "wait_ms")
        elif action in {"assert_text", "wait_for"}:
            if not request.get("text") and not request.get("selector"):
                raise AndroidMcpError(f"{action} 需要 text 或 selector。", code="missing_selector")
            if action == "wait_for":
                _bounded_int(request.get("timeout_ms"), 5000, 100, 60000, "timeout_ms")
                _bounded_int(request.get("poll_interval_ms"), 250, 50, 2000, "poll_interval_ms")

    def _resolve_point(self, adb: str, serial: str, request: dict[str, Any]) -> tuple[int, int]:
        if request.get("selector"):
            snapshot = self._ui_snapshot(adb, serial)
            nodes = _matching_nodes(snapshot["nodes"], request["selector"], request.get("selector_type", "text"), request.get("match", "contains"))
            if not nodes:
                raise AndroidMcpError(f"找不到 UI 节点：{request['selector']}", code="ui_node_not_found")
            bounds = _parse_bounds(nodes[0].get("bounds", ""))
            if not bounds:
                raise AndroidMcpError("匹配到的 UI 节点没有有效 bounds。", code="ui_bounds_missing")
            return self._validated_point(adb, serial, (bounds[0] + bounds[2]) // 2, (bounds[1] + bounds[3]) // 2)
        return self._validated_point(adb, serial, request.get("x"), request.get("y"))

    def _validated_point(self, adb: str, serial: str, x: Any, y: Any) -> tuple[int, int]:
        point = (_as_int(x, "x"), _as_int(y, "y"))
        size = self._screen_size(adb, serial)
        if not (0 <= point[0] < size["width"] and 0 <= point[1] < size["height"]):
            raise AndroidMcpError(
                f"坐标超出屏幕范围：({point[0]}, {point[1]})，屏幕为 {size['width']}x{size['height']}。",
                code="coordinate_out_of_bounds",
            )
        return point

    def _run_input(self, command: list[str], action: str) -> dict[str, Any]:
        completed = _run(command, timeout=30)
        result = {
            "action": action,
            "exit_code": completed.returncode,
            "stdout": _redact(completed.stdout)[-1000:],
            "stderr": _redact(completed.stderr)[-1000:],
        }
        if completed.returncode != 0:
            raise AndroidMcpError(f"设备交互失败：{action}", code="device_action_failed", hint=result["stderr"] or result["stdout"])
        return result

    def _screen_size(self, adb: str, serial: str) -> dict[str, Any]:
        completed = _run([adb, "-s", serial, "shell", "wm", "size"])
        sizes = re.findall(r"(\d+)x(\d+)", completed.stdout)
        if completed.returncode != 0 or not sizes:
            raise AndroidMcpError("无法读取设备屏幕尺寸。", code="screen_size_failed", hint=_redact(completed.stderr or completed.stdout)[-1000:])
        width, height = (int(value) for value in sizes[-1])
        rotation_result = _run([adb, "-s", serial, "shell", "settings", "get", "system", "user_rotation"])
        rotation_match = re.search(r"\b[0-3]\b", rotation_result.stdout)
        return {
            "serial": serial,
            "width": width,
            "height": height,
            "rotation": int(rotation_match.group(0)) if rotation_match else None,
            "raw": _redact(completed.stdout).strip(),
        }

    def _ui_snapshot(self, adb: str, serial: str, *, include_xml: bool = False) -> dict[str, Any]:
        prefix = [adb, "-s", serial]
        dumped = _run([*prefix, "shell", "uiautomator", "dump", UI_DUMP_PATH], timeout=30)
        xml_result = _run([*prefix, "shell", "cat", UI_DUMP_PATH], timeout=30)
        _run([*prefix, "shell", "rm", "-f", UI_DUMP_PATH], timeout=30)
        xml = xml_result.stdout
        if dumped.returncode != 0 or xml_result.returncode != 0 or "<hierarchy" not in xml:
            hint = _redact(xml_result.stderr or dumped.stderr or dumped.stdout)[-1000:]
            raise AndroidMcpError("无法读取 UIAutomator 层级。", code="ui_dump_failed", hint=hint)
        try:
            root = ET.fromstring(xml)
        except ET.ParseError as exc:
            raise AndroidMcpError("设备返回的 UI 层级不是有效 XML。", code="ui_dump_failed") from exc
        nodes: list[dict[str, Any]] = []
        for element in root.iter():
            node = _node_from_element(element)
            if node:
                nodes.append(node)
        result: dict[str, Any] = {"serial": serial, "node_count": len(nodes), "nodes": nodes[:500]}
        if len(nodes) > 500:
            result["truncated"] = True
        if include_xml:
            result["xml"] = xml[:200000]
        return result

    def _assert_text(
        self,
        snapshot: dict[str, Any],
        *,
        text: str | None,
        selector: str | None,
        selector_type: str,
        match: str,
    ) -> dict[str, Any]:
        value = selector or text
        if not value:
            raise AndroidMcpError("断言需要 text 或 selector。", code="missing_selector")
        nodes = _matching_nodes(snapshot.get("nodes", []), value, selector_type if selector else "text", match)
        if not nodes:
            raise AndroidMcpError(f"UI 断言失败，未找到：{value}", code="ui_assertion_failed")
        return {"serial": snapshot["serial"], "matched": len(nodes), "nodes": nodes[:20], "value": value, "match": match}

    def _wait_for(
        self,
        adb: str,
        serial: str,
        *,
        text: str | None,
        selector: str | None,
        selector_type: str,
        match: str,
        timeout_ms: int,
        poll_interval_ms: int,
        cancelled: Any | None = None,
    ) -> dict[str, Any]:
        value = selector or text
        if not value:
            raise AndroidMcpError("等待需要 text 或 selector。", code="missing_selector")
        timeout = _bounded_int(timeout_ms, 5000, 100, 60000, "timeout_ms") / 1000
        interval = _bounded_int(poll_interval_ms, 250, 50, 2000, "poll_interval_ms") / 1000
        started = time.monotonic()
        attempts = 0
        while True:
            if cancelled and cancelled():
                return {"status": "cancelled", "value": value, "attempts": attempts}
            attempts += 1
            snapshot = self._ui_snapshot(adb, serial)
            nodes = _matching_nodes(snapshot.get("nodes", []), value, selector_type if selector else "text", match)
            if nodes:
                return {
                    "serial": serial,
                    "value": value,
                    "matched": len(nodes),
                    "nodes": nodes[:20],
                    "attempts": attempts,
                    "elapsed_ms": round((time.monotonic() - started) * 1000),
                }
            elapsed = time.monotonic() - started
            if elapsed >= timeout:
                break
            _sleep_with_cancel(min(interval, timeout - elapsed), cancelled)
        raise AndroidMcpError(f"等待 UI 节点超时：{value}", code="ui_wait_timeout")

    def _logcat(self, adb: str, serial: str, lines: int) -> dict[str, Any]:
        lines = max(1, min(int(lines), 5000))
        completed = _run([adb, "-s", serial, "logcat", "-d", "-t", str(lines)], timeout=30)
        return {"serial": serial, "exit_code": completed.returncode, "content": _redact(completed.stdout), "stderr": completed.stderr[-1000:]}

    def _screenshot(self, adb: str, serial: str, root: Path, *, name: str | None = None) -> dict[str, Any]:
        try:
            completed = subprocess.run([adb, "-s", serial, "exec-out", "screencap", "-p"], capture_output=True, timeout=30, check=False)
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise AndroidMcpError("截图失败。", code="screenshot_failed") from exc
        if completed.returncode != 0 or not completed.stdout:
            raise AndroidMcpError(f"截图失败：{completed.stderr.decode(errors='replace')[-1000:]}", code="screenshot_failed")
        runtime = self.config.runtime_dir(root) / "screenshots"
        runtime.mkdir(parents=True, exist_ok=True)
        suffix = ""
        if name:
            safe_name = re.sub(r"[^A-Za-z0-9_.-]+", "-", str(name)).strip("-")[:80]
            suffix = f"-{safe_name}" if safe_name else ""
        target = runtime / f"screenshot-{time.strftime('%Y%m%dT%H%M%S')}-{time.time_ns() % 1000000:06d}{suffix}.png"
        target.write_bytes(completed.stdout)
        return {"serial": serial, "path": str(target), "size_bytes": target.stat().st_size}


def _run(command: list[str], timeout: int = 30) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(command, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=timeout, check=False)
    except FileNotFoundError as exc:
        raise AndroidMcpError("找不到 adb。", code="adb_not_found") from exc
    except subprocess.TimeoutExpired as exc:
        raise AndroidMcpError("ADB 操作超时。", code="adb_timeout") from exc


def _parse_devices(output: str) -> list[dict[str, str]]:
    devices = []
    for line in output.splitlines()[1:]:
        line = line.strip()
        if not line:
            continue
        fields = line.split()
        if len(fields) >= 2:
            devices.append({"serial": fields[0], "state": fields[1], "details": " ".join(fields[2:])})
    return devices


def _infer_package(root: Path) -> str | None:
    for path in (root / "app" / "build.gradle.kts", root / "app" / "build.gradle"):
        if not path.is_file():
            continue
        content = path.read_text(encoding="utf-8", errors="ignore")
        match = re.search(r"applicationId\s*(?:=|\()\s*[\"']([^\"']+)", content)
        if match:
            return match.group(1)
    return None


def _redact(value: str) -> str:
    return re.sub(r"(?i)(password|token|api[_-]?key)\s*([=:])\s*([^\s,;]+)", r"\1\2***", value)


def _as_int(value: Any, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise AndroidMcpError(f"{name} 必须是整数。", code="invalid_argument")
    return value


def _bounded_int(value: Any, default: int, minimum: int, maximum: int, name: str) -> int:
    selected = default if value is None else _as_int(value, name)
    if not minimum <= selected <= maximum:
        raise AndroidMcpError(f"{name} 必须在 {minimum}-{maximum} 之间。", code="invalid_argument")
    return selected


def _keycode(value: Any) -> str:
    if not isinstance(value, str) or not value.strip():
        raise AndroidMcpError("press 需要 key。", code="missing_key")
    normalized = value.strip().upper()
    if normalized.startswith("KEYCODE_"):
        normalized = normalized.removeprefix("KEYCODE_")
    if normalized not in ALLOWED_KEYS:
        raise AndroidMcpError(f"不允许的按键：{value}", code="unsupported_key", hint=f"可用按键：{', '.join(sorted(ALLOWED_KEYS))}")
    return ALLOWED_KEYS[normalized]


def _encode_input_text(value: str) -> str:
    if not isinstance(value, str) or not value or "\x00" in value or "\r" in value or "\n" in value:
        raise AndroidMcpError("输入文本不能为空，且不能包含换行或 null。", code="invalid_text")
    # input text uses %s for a space. Keep the argument as one argv item so
    # the MCP server never invokes a shell or evaluates user-provided syntax.
    return value.replace("%", "%25").replace(" ", "%s")


def _parse_bounds(value: str) -> tuple[int, int, int, int] | None:
    match = re.fullmatch(r"\[(\d+),(\d+)\]\[(\d+),(\d+)\]", value.strip())
    return tuple(int(item) for item in match.groups()) if match else None


def _node_from_element(element: ET.Element) -> dict[str, Any]:
    attributes = element.attrib
    node = {
        "class_name": attributes.get("class", ""),
        "package": attributes.get("package", ""),
        "text": attributes.get("text", ""),
        "content_desc": attributes.get("content-desc", ""),
        "resource_id": attributes.get("resource-id", ""),
        "bounds": attributes.get("bounds", ""),
        "clickable": attributes.get("clickable", "false").lower() == "true",
        "enabled": attributes.get("enabled", "false").lower() == "true",
        "scrollable": attributes.get("scrollable", "false").lower() == "true",
    }
    return node if any(node[key] for key in ("class_name", "package", "text", "content_desc", "resource_id", "bounds")) else {}


def _matching_nodes(nodes: list[dict[str, Any]], value: str, selector_type: str, match: str) -> list[dict[str, Any]]:
    if selector_type not in SELECTOR_TYPES or match not in MATCH_MODES:
        return []
    target = str(value)
    matched = []
    for node in nodes:
        current = str(node.get(selector_type, ""))
        is_match = current == target if match == "equals" else target in current
        if is_match:
            matched.append(node)
    return matched


def _sleep_with_cancel(seconds: float, cancelled: Any | None = None) -> None:
    deadline = time.monotonic() + max(0.0, seconds)
    while True:
        if cancelled and cancelled():
            return
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return
        time.sleep(min(0.1, remaining))


def _stable_request(request: dict[str, Any]) -> str:
    return json.dumps(request, ensure_ascii=False, sort_keys=True, default=str, separators=(",", ":"))
