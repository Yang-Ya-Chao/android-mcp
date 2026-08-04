"""Fixed-action ADB device operations."""

from __future__ import annotations

import os
import re
import subprocess
import time
from pathlib import Path
from typing import Any

from ..config import ConfigManager
from ..models import AndroidMcpError, ok
from ..paths import PathPolicy
from .environment import EnvironmentService
from .task_manager import TaskManager


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
        **_: Any,
    ) -> dict[str, Any]:
        action = action or "list"
        if action == "list":
            return ok(self.list_devices(project_root))
        environment = self.environment.detect(project_root)["data"]
        adb = environment["adb"]["path"]
        if not adb:
            raise AndroidMcpError("未找到 adb。", code="adb_not_found", hint="先运行 android_environment(action=\"doctor\")。")
        if action in {"logcat", "screenshot"}:
            selected = self._select_serial(adb, serial)
            if action == "logcat":
                return ok(self._logcat(adb, selected, lines))
            root = self.policy.root(project_root)
            return ok(self._screenshot(adb, selected, root))
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
        return ok({"task_id": record.task_id, "status": record.status.lower(), "serial": selected, "action": action}, hint="设备操作已异步提交；使用 android_task 查询结果。")

    def list_devices(self, project_root: str | None) -> dict[str, Any]:
        environment = self.environment.detect(project_root)["data"]
        adb = environment["adb"]["path"]
        if not adb:
            return {"devices": [], "adb": None, "message": "未找到 adb。"}
        completed = _run([adb, "devices", "-l"])
        devices = []
        for line in completed.stdout.splitlines()[1:]:
            line = line.strip()
            if not line:
                continue
            fields = line.split()
            if len(fields) >= 2:
                devices.append({"serial": fields[0], "state": fields[1], "details": " ".join(fields[2:])})
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
                raise AndroidMcpError("未找到 APK，请先执行 android_build(action=\"assemble\")。", code="apk_not_found")
            return candidates[-1].resolve()
        candidate = Path(apk_path).expanduser().resolve()
        try:
            candidate.relative_to(root)
        except ValueError as exc:
            raise AndroidMcpError("APK 必须位于项目目录内。", code="path_escape") from exc
        if candidate.suffix.lower() != ".apk" or not candidate.is_file():
            raise AndroidMcpError(f"APK 不存在或格式不正确：{candidate}", code="invalid_apk")
        return candidate

    def _run_mutation(self, adb: str, serial: str, action: str, apk: Path | None, package_name: str | None, activity: str | None, progress: Any, cancelled: Any) -> dict[str, Any]:
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
        return {"status": "completed" if completed.returncode == 0 else "failed", "action": action, "serial": serial, "exit_code": completed.returncode, "stdout": _redact(completed.stdout)[-4000:], "stderr": _redact(completed.stderr)[-2000:]}

    def _logcat(self, adb: str, serial: str, lines: int) -> dict[str, Any]:
        lines = max(1, min(int(lines), 5000))
        completed = _run([adb, "-s", serial, "logcat", "-d", "-t", str(lines)], timeout=30)
        return {"serial": serial, "exit_code": completed.returncode, "content": _redact(completed.stdout), "stderr": completed.stderr[-1000:]}

    def _screenshot(self, adb: str, serial: str, root: Path) -> dict[str, Any]:
        completed = subprocess.run([adb, "-s", serial, "exec-out", "screencap", "-p"], capture_output=True, timeout=30, check=False)
        if completed.returncode != 0 or not completed.stdout:
            raise AndroidMcpError(f"截图失败：{completed.stderr.decode(errors='replace')[-1000:]}", code="screenshot_failed")
        runtime = self.config.runtime_dir(root) / "screenshots"
        runtime.mkdir(parents=True, exist_ok=True)
        target = runtime / f"screenshot-{time.strftime('%Y%m%dT%H%M%S')}.png"
        target.write_bytes(completed.stdout)
        return {"serial": serial, "path": str(target), "size_bytes": target.stat().st_size}


def _run(command: list[str], timeout: int = 30) -> subprocess.CompletedProcess[str]:
    return subprocess.run(command, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=timeout, check=False)


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
