"""Controlled Gradle Wrapper execution and structured diagnostics."""

from __future__ import annotations

import hashlib
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
from .project_service import ProjectService
from .task_manager import TaskManager


class BuildService:
    def __init__(self, tasks: TaskManager, environment: EnvironmentService, config: ConfigManager | None = None) -> None:
        self.tasks = tasks
        self.environment = environment
        self.config = config or ConfigManager()
        self.policy = PathPolicy()
        self.projects = ProjectService()

    def handle(
        self,
        *,
        action: str | None,
        project_root: str | None = None,
        module: str = "app",
        variant: str = "debug",
        backend: str = "auto",
        tasks: list[str] | None = None,
        timeout_seconds: int = 1800,
        confirm_release: bool = False,
        connected: bool = False,
        **_: Any,
    ) -> dict[str, Any]:
        action = action or "assemble"
        root = self.policy.root(project_root)
        if action not in {"assemble", "bundle", "test", "lint", "check", "clean", "install"}:
            raise AndroidMcpError(f"android_build 不支持 action：{action}", code="unsupported_action")
        if backend not in {"auto", "wrapper", "tooling_api"}:
            raise AndroidMcpError(f"不支持的构建 backend：{backend}", code="invalid_backend")
        selected = tasks or [self._task_for(action, module, variant, connected=connected)]
        self._validate_tasks(root, selected)
        release = any("release" in task.lower() for task in selected)
        if release and not confirm_release:
            raise AndroidMcpError(
                "Release 构建需要显式 confirm_release=true。",
                code="release_confirmation_required",
                hint="确认不会泄露签名信息后再提交。",
            )
        if backend == "tooling_api":
            raise AndroidMcpError(
                "Tooling API bridge 尚未安装。",
                code="tooling_api_unavailable",
                hint="使用 backend=\"auto\" 或 backend=\"wrapper\" 通过项目 Gradle Wrapper 构建。",
            )
        wrapper = _wrapper(root)
        if not wrapper:
            raise AndroidMcpError("项目中未找到 gradlew.bat 或 gradlew。", code="wrapper_not_found")
        timeout_seconds = max(1, min(int(timeout_seconds), 24 * 60 * 60))
        dedupe_key = f"{root}|{' '.join(selected)}"
        record = self.tasks.submit(
            "android_build",
            lambda progress, cancelled: self._run_wrapper(
                root,
                wrapper,
                selected,
                timeout_seconds,
                backend,
                progress,
                cancelled,
            ),
            dedupe_key=dedupe_key,
        )
        return ok(
            {
                "task_id": record.task_id,
                "status": record.status.lower(),
                "dedupe_key": dedupe_key,
                "backend": "wrapper",
                "tasks": selected,
            },
            hint="构建已异步提交；使用 android_task(action=\"status\"/\"result\") 获取进度和结果。",
        )

    def _task_for(self, action: str, module: str, variant: str, *, connected: bool = False) -> str:
        if not re.fullmatch(r"[A-Za-z0-9_.\-/]+", module) or not re.fullmatch(r"[A-Za-z0-9_]+", variant):
            raise AndroidMcpError("module 或 variant 包含不安全字符。", code="invalid_task_name")
        capitalized = variant[:1].upper() + variant[1:]
        suffixes = {
            "assemble": f"assemble{capitalized}",
            "bundle": f"bundle{capitalized}",
            "test": f"test{capitalized}UnitTest",
            "lint": f"lint{capitalized}",
            "check": "check",
            "clean": "clean",
            "install": f"install{capitalized}",
        }
        if action == "test" and connected:
            suffixes["test"] = f"connected{capitalized}AndroidTest"
        return f":{module.strip(':')}:{suffixes[action]}"

    def _validate_tasks(self, root: Path, selected: list[str]) -> None:
        allowed = set(self.projects.tasks(root, None))
        pattern = re.compile(r"^:[A-Za-z0-9_.\-/]+:(assemble|bundle|test|lint|check|clean|install|connected)[A-Za-z0-9_]*$")
        for task in selected:
            if not isinstance(task, str) or not (task in allowed or pattern.fullmatch(task)):
                raise AndroidMcpError(
                    f"拒绝未受控的 Gradle 任务：{task}",
                    code="unsafe_gradle_task",
                    hint="先用 android_project(action=\"tasks\") 查看受控任务。",
                )

    def _run_wrapper(
        self,
        root: Path,
        wrapper: Path,
        selected: list[str],
        timeout_seconds: int,
        backend: str,
        progress: Any,
        cancelled: Any,
    ) -> dict[str, Any]:
        environment_result = self.environment.detect(str(root))
        environment = environment_result["data"]
        runtime = self.config.runtime_dir(root)
        runtime.mkdir(parents=True, exist_ok=True)
        timestamp = time.strftime("%Y%m%dT%H%M%S")
        stdout_path = runtime / f"build-{timestamp}.stdout.log"
        stderr_path = runtime / f"build-{timestamp}.stderr.log"
        command = _wrapper_command(wrapper, selected)
        env = os.environ.copy()
        if environment["jdk"]["home"]:
            env["JAVA_HOME"] = environment["jdk"]["home"]
            env["PATH"] = str(Path(environment["jdk"]["home"]) / "bin") + os.pathsep + env.get("PATH", "")
        if environment["sdk"]["root"]:
            env["ANDROID_SDK_ROOT"] = environment["sdk"]["root"]
            env["ANDROID_HOME"] = environment["sdk"]["root"]
        progress(current_step="configure", progress=5, total_steps=4, message="准备 Gradle Wrapper 环境")
        started = time.monotonic()
        stdout = ""
        stderr = ""
        exit_code: int | None = None
        try:
            process = subprocess.Popen(
                command,
                cwd=root,
                env=env,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
                errors="replace",
            )
            progress(current_step="execute", progress=20, message=f"执行 {' '.join(selected)}")
            while True:
                try:
                    out, err = process.communicate(timeout=1)
                    stdout += out or ""
                    stderr += err or ""
                    exit_code = process.returncode
                    break
                except subprocess.TimeoutExpired as exc:
                    stdout += _as_text(exc.output)
                    stderr += _as_text(exc.stderr)
                    if cancelled():
                        process.terminate()
                        try:
                            process.wait(timeout=5)
                        except subprocess.TimeoutExpired:
                            process.kill()
                            process.wait(timeout=5)
                        return {
                            "status": "cancelled",
                            "backend": backend,
                            "project_root": str(root),
                            "tasks": selected,
                            "exit_code": None,
                            "diagnostics": [],
                            "artifacts": [],
                            "logs": {"stdout_path": str(stdout_path), "stderr_path": str(stderr_path), "tail": _redact(stdout + stderr)[-4000:]},
                        }
                    elapsed = time.monotonic() - started
                    if elapsed > timeout_seconds:
                        process.terminate()
                        try:
                            process.wait(timeout=5)
                        except subprocess.TimeoutExpired:
                            process.kill()
                            process.wait(timeout=5)
                        raise AndroidMcpError(f"Gradle 构建超时（{timeout_seconds}s）。", code="build_timeout")
                    progress(current_step="execute", progress=min(85, 20 + elapsed / timeout_seconds * 60), message=f"Gradle 执行中（{int(elapsed)}s）")
        except FileNotFoundError as exc:
            raise AndroidMcpError(f"无法启动 Gradle Wrapper：{wrapper}", code="wrapper_launch_failed") from exc
        finally:
            stdout_path.write_text(_redact(stdout), encoding="utf-8")
            stderr_path.write_text(_redact(stderr), encoding="utf-8")
        progress(current_step="diagnose", progress=90, message="解析构建日志")
        diagnostics = _parse_diagnostics(stdout + "\n" + stderr)
        artifacts = _artifacts(root, selected)
        progress(current_step="package", progress=98, message="收集构建产物")
        return {
            "status": "completed" if exit_code == 0 else "failed",
            "backend": backend,
            "project_root": str(root),
            "tasks": selected,
            "exit_code": exit_code,
            "duration_seconds": round(time.monotonic() - started, 3),
            "diagnostics": diagnostics,
            "artifacts": artifacts,
            "logs": {"stdout_path": str(stdout_path), "stderr_path": str(stderr_path), "tail": _redact(stdout + stderr)[-4000:]},
        }


def _wrapper(root: Path) -> Path | None:
    for name in ("gradlew.bat", "gradlew"):
        candidate = root / name
        if candidate.is_file():
            return candidate
    return None


def _wrapper_command(wrapper: Path, tasks: list[str]) -> list[str]:
    args = [*tasks, "--console=plain"]
    if wrapper.suffix.lower() == ".bat":
        return [os.environ.get("COMSPEC", "cmd.exe"), "/d", "/c", str(wrapper), *args]
    return [str(wrapper), *args]


def _as_text(value: Any) -> str:
    if value is None:
        return ""
    return value.decode("utf-8", errors="replace") if isinstance(value, bytes) else str(value)


def _redact(value: str) -> str:
    return re.sub(
        r"(?i)(storePassword|keyPassword|password|token|api[_-]?key)\s*([=:])\s*([^\s,;]+)",
        lambda match: f"{match.group(1)}{match.group(2)}***",
        value,
    )


def _parse_diagnostics(log: str) -> list[dict[str, Any]]:
    diagnostics: list[dict[str, Any]] = []
    patterns = [
        re.compile(r"(?:e:|error:)?\s*(?P<file>[^\s:]+\.(?:kt|kts|java|xml)):(?P<line>\d+):(?P<column>\d+):\s*(?P<message>.+)", re.IGNORECASE),
        re.compile(r"(?P<file>[^\s:]+\.(?:kt|kts|java|xml)):(?P<line>\d+):\s*(?P<message>.+)", re.IGNORECASE),
    ]
    for line in log.splitlines():
        match = next((pattern.search(line) for pattern in patterns if pattern.search(line)), None)
        if match:
            data = match.groupdict()
            diagnostics.append(
                {
                    "file": data.get("file"),
                    "line": int(data["line"]) if data.get("line") else None,
                    "column": int(data["column"]) if data.get("column") else None,
                    "severity": "error" if "error" in line.lower() or line.lstrip().startswith("e:") else "warning",
                    "code": _diagnostic_code(data.get("message", "")),
                    "message": data.get("message", "").strip(),
                    "fix_hint": _fix_hint(data.get("message", "")),
                }
            )
    if "What went wrong" in log and not diagnostics:
        diagnostics.append({"file": None, "line": None, "column": None, "severity": "error", "code": "gradle_failure", "message": "Gradle task failed; see logs.tail。", "fix_hint": "先确认环境、依赖和任务名。"})
    return diagnostics[:100]


def _diagnostic_code(message: str) -> str:
    lowered = message.lower()
    if "unresolved reference" in lowered:
        return "kotlin_unresolved_reference"
    if "duplicate class" in lowered:
        return "duplicate_class"
    if "resource" in lowered and "not found" in lowered:
        return "aapt2_resource_missing"
    if "manifest" in lowered:
        return "manifest_merger"
    return "compile_error"


def _fix_hint(message: str) -> str | None:
    lowered = message.lower()
    if "unresolved reference" in lowered:
        return "检查 import、模块依赖和符号拼写；可用 android_kb 搜索现有 API。"
    if "resource" in lowered:
        return "检查 res 文件名、资源类型和引用模块。"
    return None


def _artifacts(root: Path, tasks: list[str]) -> list[dict[str, Any]]:
    module_names = {task.split(":")[1] for task in tasks if task.startswith(":") and len(task.split(":")) > 2}
    files: list[Path] = []
    for module in module_names:
        output = root / module / "build" / "outputs"
        if output.is_dir():
            files.extend(item for item in output.rglob("*") if item.is_file() and item.suffix.lower() in {".apk", ".aab"})
    result = []
    for path in sorted(files):
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        result.append({"path": str(path), "type": path.suffix.lower().lstrip("."), "module": path.parts[-4] if len(path.parts) >= 4 else None, "variant": path.parent.name, "size_bytes": path.stat().st_size, "sha256": digest})
    return result
