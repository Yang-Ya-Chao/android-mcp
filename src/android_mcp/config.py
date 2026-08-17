"""Three-layer configuration and runtime state paths."""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
from pathlib import Path
from typing import Any

from .models import AndroidMcpError


def _json_load(path: Path, default: dict[str, Any]) -> dict[str, Any]:
    try:
        with path.open("r", encoding="utf-8") as stream:
            value = json.load(stream)
        return value if isinstance(value, dict) else dict(default)
    except (OSError, ValueError, TypeError):
        return dict(default)


def _atomic_json_write(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as stream:
            json.dump(value, stream, ensure_ascii=False, indent=2)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    except Exception:
        try:
            os.unlink(temporary)
        except OSError:
            pass
        raise


class ConfigManager:
    """Load global, project and runtime configuration without failing startup."""

    def __init__(self) -> None:
        configured = os.environ.get("ANDROID_MCP_CONFIG")
        self.global_path = Path(configured).expanduser() if configured else Path.home() / ".android-mcp" / "config.json"

    def project_config_path(self, project_root: Path) -> Path:
        return project_root / ".androidmcp" / "project.json"

    def runtime_dir(self, project_root: Path | None = None) -> Path:
        base = Path(os.environ.get("LOCALAPPDATA", Path.home() / ".local")) / "android-mcp" / "state"
        if project_root is None:
            return base / "global"
        # Windows may expose the same temporary/project directory through an
        # 8.3 path and a fully expanded path.  Normalize before hashing so the
        # project index and evidence store cannot split into two runtimes.
        normalized_root = Path(project_root).expanduser().resolve()
        digest = hashlib.sha256(str(normalized_root).encode("utf-8")).hexdigest()[:16]
        return base / digest

    def load(self, project_root: Path | None = None) -> dict[str, Any]:
        global_config = _json_load(self.global_path, {})
        project_config = _json_load(self.project_config_path(project_root), {}) if project_root else {}
        merged: dict[str, Any] = {
            "version": 1,
            "edit_guard": {"mode": os.environ.get("ANDROID_EDIT_GUARD", "warn")},
            "allowed_project_roots": [],
            "build": {"timeout_seconds": 1800},
            "github": {
                "enabled": True,
                "token_env": "GITHUB_TOKEN",
                "timeout_seconds": 20,
                "max_file_bytes": 180_000,
            },
            "update": {
                "repository": "Yang-Ya-Chao/android-mcp",
                "branch": "main",
                "timeout_seconds": 20,
                "install_spec": "git+https://github.com/Yang-Ya-Chao/android-mcp.git@main",
            },
            "environment": {},
        }
        _deep_merge(merged, global_config)
        _deep_merge(merged, project_config)
        mode = str(os.environ.get("ANDROID_EDIT_GUARD") or merged.get("edit_guard", {}).get("mode", "warn")).lower()
        if mode not in {"warn", "strict"}:
            mode = "warn"
        merged.setdefault("edit_guard", {})["mode"] = mode
        return merged

    def save_global(self, config: dict[str, Any]) -> None:
        try:
            _atomic_json_write(self.global_path, config)
        except OSError as exc:
            raise AndroidMcpError(
                f"无法保存全局配置：{self.global_path} ({exc})",
                code="config_write_failed",
                hint="检查用户目录权限，或设置 ANDROID_MCP_CONFIG 指向可写路径。",
            ) from exc

    def cache_environment(self, environment: dict[str, Any]) -> None:
        config = _json_load(self.global_path, {"version": 1})
        config["environment"] = environment
        self.save_global(config)


def _deep_merge(target: dict[str, Any], source: dict[str, Any]) -> None:
    for key, value in source.items():
        if isinstance(value, dict) and isinstance(target.get(key), dict):
            _deep_merge(target[key], value)
        else:
            target[key] = value
