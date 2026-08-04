"""Filesystem-backed Android project discovery.

The optional Gradle Tooling API bridge can later replace individual methods.  The
MVP deliberately has a deterministic wrapper/filesystem fallback so discovery is
useful even when the bridge JAR is not installed.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from ..models import AndroidMcpError, ok
from ..paths import PathPolicy


class ProjectService:
    def __init__(self) -> None:
        self.policy = PathPolicy()

    def handle(self, *, action: str | None, project_root: str | None = None, module: str | None = None, **_: Any) -> dict[str, Any]:
        action = action or "discover"
        root = self.policy.root(project_root)
        if action in {"discover", "info"}:
            return ok(self.discover(root), hint="项目发现完成；可继续查询 variants、tasks 或执行 android_build。")
        if action == "modules":
            return ok({"project_root": str(root), "modules": self.modules(root)})
        if action == "variants":
            return ok({"project_root": str(root), "module": module or "app", "variants": self.variants(root, module or "app")})
        if action == "tasks":
            return ok({"project_root": str(root), "tasks": self.tasks(root, module)})
        if action == "dependencies":
            return ok({"project_root": str(root), "dependencies": self.dependencies(root, module)})
        if action == "sync":
            return ok({"project_root": str(root), "status": "filesystem_fallback", "diff": [], "message": "Tooling API bridge 未配置；已完成静态项目发现。"})
        if action == "diagnose":
            return ok(self.diagnose(root, module))
        raise AndroidMcpError(f"android_project 不支持 action：{action}", code="unsupported_action")

    def discover(self, root: Path) -> dict[str, Any]:
        settings = next((path for path in (root / "settings.gradle.kts", root / "settings.gradle") if path.is_file()), None)
        wrapper = next((path for path in (root / "gradlew.bat", root / "gradlew") if path.is_file()), None)
        return {
            "project_root": str(root),
            "settings_file": str(settings) if settings else None,
            "wrapper": str(wrapper) if wrapper else None,
            "modules": self.modules(root),
            "variants": {module: self.variants(root, module) for module in self.modules(root)},
            "tasks": self.tasks(root, None),
            "tooling_api": {"available": False, "reason": "bridge not installed"},
        }

    def modules(self, root: Path) -> list[str]:
        found: set[str] = set()
        for settings in (root / "settings.gradle.kts", root / "settings.gradle"):
            if not settings.is_file():
                continue
            content = settings.read_text(encoding="utf-8", errors="ignore")
            for match in re.finditer(r"include\s*\(([^)]*)\)|include\s+([^\n]+)", content):
                values = match.group(1) or match.group(2) or ""
                for value in re.findall(r"['\"](:[^'\"]+)['\"]", values):
                    found.add(value.lstrip(":").replace(":", "/"))
        for build_file in list(root.glob("*/build.gradle")) + list(root.glob("*/build.gradle.kts")):
            found.add(build_file.parent.name)
        if (root / "build.gradle").is_file() or (root / "build.gradle.kts").is_file():
            found.add(":root")
        return sorted(found)

    def variants(self, root: Path, module: str) -> list[dict[str, str]]:
        if module == ":root":
            return []
        path = root / module / "build.gradle.kts"
        if not path.is_file():
            path = root / module / "build.gradle"
        content = path.read_text(encoding="utf-8", errors="ignore") if path.is_file() else ""
        build_types = set(re.findall(r"buildTypes\s*\{([\s\S]*?)\n\s*\}", content))
        names = {"debug", "release"}
        if build_types:
            names.update(re.findall(r"^\s*(\w+)\s*\{", "\n".join(build_types), re.MULTILINE))
        flavors = re.findall(r"productFlavors\s*\{([\s\S]*?)\n\s*\}", content)
        flavor_names = re.findall(r"^\s*(\w+)\s*\{", "\n".join(flavors), re.MULTILINE) if flavors else []
        if not flavor_names:
            flavor_names = [""]
        return [{"name": f"{flavor}{variant}" if flavor else variant, "flavor": flavor, "build_type": variant} for flavor in flavor_names for variant in sorted(names)]

    def tasks(self, root: Path, module: str | None) -> list[str]:
        modules = [module] if module else [item for item in self.modules(root) if item != ":root"]
        tasks: set[str] = set()
        for name in modules:
            prefix = f":{name}:"
            for suffix in ("assembleDebug", "assembleRelease", "bundleDebug", "bundleRelease", "testDebugUnitTest", "testReleaseUnitTest", "connectedDebugAndroidTest", "connectedReleaseAndroidTest", "lintDebug", "lintRelease", "check", "clean", "installDebug"):
                tasks.add(prefix + suffix)
        tasks.update({"tasks", "help", "build", "clean"})
        return sorted(tasks)

    def dependencies(self, root: Path, module: str | None) -> list[dict[str, str]]:
        paths = []
        if module:
            paths.extend([root / module / "build.gradle.kts", root / module / "build.gradle"])
        else:
            paths.extend(list(root.glob("*/build.gradle")) + list(root.glob("*/build.gradle.kts")))
        result: list[dict[str, str]] = []
        seen: set[tuple[str, str]] = set()
        pattern = re.compile(r"\b(implementation|api|compileOnly|runtimeOnly|testImplementation|androidTestImplementation)\s*\(?\s*[\"']([^\"']+)[\"']\s*\)?")
        for path in paths:
            if not path.is_file():
                continue
            content = path.read_text(encoding="utf-8", errors="ignore")
            for configuration, coordinate in pattern.findall(content):
                key = configuration, coordinate
                if key not in seen:
                    seen.add(key)
                    result.append({"module": path.parent.name, "configuration": configuration, "coordinate": coordinate})
        return sorted(result, key=lambda item: (item["module"], item["configuration"], item["coordinate"]))

    def diagnose(self, root: Path, module: str | None) -> dict[str, Any]:
        missing = []
        if not (root / "settings.gradle.kts").is_file() and not (root / "settings.gradle").is_file():
            missing.append("settings.gradle(.kts)")
        if not (root / "gradlew.bat").is_file() and not (root / "gradlew").is_file():
            missing.append("gradlew(.bat)")
        if module and module not in self.modules(root):
            missing.append(f"module:{module}")
        return {"project_root": str(root), "status": "ok" if not missing else "needs_attention", "missing": missing, "module": module}
