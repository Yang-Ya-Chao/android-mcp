"""Environment discovery for Android/Gradle projects."""

from __future__ import annotations

import os
import re
import shutil
import subprocess
from pathlib import Path
from typing import Any

from ..config import ConfigManager
from ..models import AndroidMcpError, ok
from ..paths import PathPolicy


class EnvironmentService:
    def __init__(self, config: ConfigManager | None = None) -> None:
        self.config = config or ConfigManager()
        self.policy = PathPolicy()

    def detect(self, project_root: str | None = None) -> dict[str, Any]:
        root = self.policy.root(project_root) if project_root else None
        gradle_properties = _read_properties(root / "gradle.properties") if root and (root / "gradle.properties").is_file() else {}
        local_properties = _read_properties(root / "local.properties") if root and (root / "local.properties").is_file() else {}
        java_home = self._find_java_home(root, gradle_properties)
        sdk_root = self._find_sdk_root(root, local_properties)
        adb = _find_executable(
            [
                sdk_root / "platform-tools" / _exe("adb") if sdk_root else Path(""),
                Path(shutil.which("adb") or ""),
            ]
        )
        wrapper = _find_executable([root / "gradlew.bat" if root else Path(""), root / "gradlew" if root else Path("")])
        studio = self._find_android_studio()
        build_tools = _latest_child(sdk_root / "build-tools" if sdk_root else None)
        gradle_version = _wrapper_version(root) if root else None
        agp_version, kotlin_version = _script_versions(root) if root else (None, None)
        result = {
            "project_root": str(root) if root else None,
            "jdk": {
                "home": str(java_home) if java_home else None,
                "java": str(java_home / "bin" / _exe("java")) if java_home else None,
                "version": _command_version(java_home / "bin" / _exe("java")) if java_home else None,
                "source": _candidate_source(java_home, root, "jdk"),
                "found": bool(java_home),
            },
            "sdk": {
                "root": str(sdk_root) if sdk_root else None,
                "source": _candidate_source(sdk_root, root, "sdk"),
                "found": bool(sdk_root),
                "build_tools": str(build_tools) if build_tools else None,
            },
            "adb": {
                "path": str(adb) if adb else None,
                "version": _command_version(adb, ["version"]) if adb else None,
                "found": bool(adb),
            },
            "android_studio": {"home": str(studio) if studio else None, "found": bool(studio)},
            "gradle_wrapper": {
                "path": str(wrapper) if wrapper else None,
                "version": gradle_version,
                "found": bool(wrapper),
            },
            "agp": {"version": agp_version, "found": bool(agp_version)},
            "kotlin": {"version": kotlin_version, "found": bool(kotlin_version)},
            "missing": [],
            "warnings": [],
        }
        for key, label in (("jdk", "JDK"), ("sdk", "Android SDK"), ("adb", "ADB"), ("gradle_wrapper", "Gradle Wrapper")):
            if not result[key]["found"]:
                result["missing"].append(label)
        if root and not (root / "settings.gradle.kts").exists() and not (root / "settings.gradle").exists():
            result["warnings"].append("未找到 settings.gradle 或 settings.gradle.kts。")
        if java_home and studio and not os.environ.get("JAVA_HOME"):
            result["warnings"].append("JAVA_HOME 未设置，构建时将注入检测到的 JDK。")
        try:
            self.config.cache_environment(result)
            result["cached"] = True
        except AndroidMcpError as exc:
            result["cached"] = False
            result["warnings"].append(exc.message)
        return ok(result, hint="环境检测完成后可调用 android_project(action=\"discover\")。")

    def doctor(self, project_root: str | None = None) -> dict[str, Any]:
        detected = self.detect(project_root)
        data = detected["data"]
        checks = []
        for label in ("jdk", "sdk", "adb", "gradle_wrapper"):
            item = data[label]
            checks.append({"name": label, "status": "ok" if item["found"] else "missing", "details": item})
        compatible = not data["missing"]
        return ok(
            {"status": "ok" if compatible else "needs_attention", "checks": checks, "missing": data["missing"], "warnings": data["warnings"]},
            hint="缺失项请先修复；如果只是没有环境变量，确认路径后可直接重试构建。",
        )

    def _find_java_home(self, root: Path | None, properties: dict[str, str]) -> Path | None:
        candidates: list[Path] = []
        configured = properties.get("org.gradle.java.home")
        if configured:
            candidates.append(Path(configured).expanduser())
        if os.environ.get("JAVA_HOME"):
            candidates.append(Path(os.environ["JAVA_HOME"]).expanduser())
        studio = self._find_android_studio()
        if studio:
            candidates.append(studio / "jbr")
        candidates.extend(
            [
                Path(os.environ.get("LOCALAPPDATA", "")) / "Programs" / "Android Studio" / "jbr",
                Path(os.environ.get("ProgramFiles", "C:\\Program Files")) / "Android" / "Android Studio" / "jbr",
            ]
        )
        java = shutil.which("java")
        if java:
            candidates.append(Path(java).resolve().parent.parent)
        return _find_directory_with(candidates, "bin", _exe("java"))

    def _find_sdk_root(self, root: Path | None, properties: dict[str, str]) -> Path | None:
        candidates: list[Path] = []
        configured = properties.get("sdk.dir")
        if configured:
            candidates.append(Path(configured.replace("\\:", ":")).expanduser())
        for variable in ("ANDROID_SDK_ROOT", "ANDROID_HOME"):
            if os.environ.get(variable):
                candidates.append(Path(os.environ[variable]).expanduser())
        candidates.extend(
            [
                Path(os.environ.get("LOCALAPPDATA", "")) / "Android" / "Sdk",
                Path.home() / "AppData" / "Local" / "Android" / "Sdk",
            ]
        )
        return _find_directory_with(candidates, "platform-tools", _exe("adb"))

    def _find_android_studio(self) -> Path | None:
        candidates = [
            Path(os.environ.get("LOCALAPPDATA", "")) / "Programs" / "Android Studio",
            Path(os.environ.get("ProgramFiles", "C:\\Program Files")) / "Android" / "Android Studio",
            Path(os.environ.get("ProgramFiles(x86)", "C:\\Program Files (x86)")) / "Android" / "Android Studio",
        ]
        return _find_directory(candidates)


def _exe(name: str) -> str:
    return f"{name}.exe" if os.name == "nt" else name


def _find_directory(candidates: list[Path]) -> Path | None:
    seen: set[str] = set()
    for candidate in candidates:
        if not str(candidate) or str(candidate) in seen:
            continue
        seen.add(str(candidate))
        try:
            resolved = candidate.expanduser().resolve()
        except OSError:
            continue
        if resolved.is_dir():
            return resolved
    return None


def _find_directory_with(candidates: list[Path], child_dir: str, executable: str) -> Path | None:
    for candidate in candidates:
        resolved = _find_directory([candidate])
        if resolved and (resolved / child_dir / executable).is_file():
            return resolved
    return None


def _find_executable(candidates: list[Path]) -> Path | None:
    for candidate in candidates:
        if str(candidate) and candidate.is_file():
            return candidate.resolve()
    return None


def _latest_child(directory: Path | None) -> Path | None:
    if not directory or not directory.is_dir():
        return None
    children = [item for item in directory.iterdir() if item.is_dir()]
    return sorted(children, key=lambda item: item.name)[-1] if children else None


def _read_properties(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    try:
        for raw in path.read_text(encoding="utf-8").splitlines():
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            values[key.strip()] = value.strip()
    except (OSError, UnicodeError):
        pass
    return values


def _wrapper_version(root: Path) -> str | None:
    properties = root / "gradle" / "wrapper" / "gradle-wrapper.properties"
    if not properties.is_file():
        return None
    match = re.search(r"gradle-([0-9][^/-]*)-(?:bin|all)\.zip", properties.read_text(encoding="utf-8", errors="ignore"))
    return match.group(1) if match else None


def _script_versions(root: Path) -> tuple[str | None, str | None]:
    agp = kotlin = None
    for path in list(root.glob("*.gradle")) + list(root.glob("*.gradle.kts")) + list(root.glob("**/build.gradle")) + list(root.glob("**/build.gradle.kts")):
        try:
            content = path.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        agp_match = re.search(r"com\.android\.tools\.build:gradle:([\w.+-]+)|id\([\"']com\.android\.(?:application|library)[\"']\)\s+version\s+[\"']([^\"']+)", content)
        kotlin_match = re.search(r"id\([\"']org\.jetbrains\.kotlin(?:\.android)?[\"']\)\s+version\s+[\"']([^\"']+)|kotlin\([\"']android[\"']\)\s+version\s+[\"']([^\"']+)", content)
        if agp_match:
            agp = agp_match.group(1) or agp_match.group(2)
        if kotlin_match:
            kotlin = kotlin_match.group(1) or kotlin_match.group(2)
    return agp, kotlin


def _command_version(executable: Path, args: list[str] | None = None) -> str | None:
    try:
        completed = subprocess.run([str(executable), *(args or ["-version"])], capture_output=True, text=True, timeout=5, check=False)
    except (OSError, subprocess.SubprocessError):
        return None
    output = f"{completed.stdout}\n{completed.stderr}".strip()
    return output.splitlines()[0][:200] if output else None


def _candidate_source(candidate: Path | None, root: Path | None, kind: str) -> str | None:
    if not candidate:
        return None
    if kind == "jdk" and os.environ.get("JAVA_HOME") and candidate == Path(os.environ["JAVA_HOME"]).expanduser().resolve():
        return "JAVA_HOME"
    if kind == "sdk" and os.environ.get("ANDROID_SDK_ROOT") and candidate == Path(os.environ["ANDROID_SDK_ROOT"]).expanduser().resolve():
        return "ANDROID_SDK_ROOT"
    if root and kind == "sdk" and (root / "local.properties").is_file():
        return "local.properties"
    return "discovered"
