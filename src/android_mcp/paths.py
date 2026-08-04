"""Project-root and sensitive-path policy."""

from __future__ import annotations

import os
from pathlib import Path

from .models import AndroidMcpError


PROTECTED_NAMES = {
    "local.properties",
    "gradle.properties",
    "settings.properties",
    ".env",
    "keystore.properties",
    "signing.properties",
}
PROTECTED_DIRECTORIES = {".git", ".gradle", ".idea", "build", "captures", "__history"}
PROTECTED_SUFFIXES = {".jks", ".keystore", ".p12", ".pfx", ".pem", ".key"}
PROTECTED_TOKENS = {"secret", "secrets", "password", "credentials", "apikey", "api_key", "token"}


class PathPolicy:
    """Keep every user-addressable file inside one explicit project root."""

    def root(self, project_root: str | os.PathLike[str] | None = None) -> Path:
        candidate = Path(project_root or os.getcwd()).expanduser()
        try:
            resolved = candidate.resolve(strict=True)
        except OSError as exc:
            raise AndroidMcpError(f"项目根目录不存在或不可访问：{candidate}", code="invalid_project_root") from exc
        if not resolved.is_dir():
            raise AndroidMcpError(f"项目根路径不是目录：{resolved}", code="invalid_project_root")
        return resolved

    def file(
        self,
        project_root: str | os.PathLike[str] | None,
        file_path: str | os.PathLike[str],
        *,
        allow_missing: bool = False,
        allow_artifact: bool = False,
        allow_directory: bool = False,
    ) -> Path:
        root = self.root(project_root)
        raw = os.fspath(file_path)
        if "\x00" in raw:
            raise AndroidMcpError("文件路径包含 null 字节。", code="invalid_path")
        candidate = Path(raw).expanduser()
        if not candidate.is_absolute():
            candidate = root / candidate
        try:
            # Resolve without requiring existence so a missing file returns the
            # actionable file_not_found code instead of an opaque OS error.
            resolved = candidate.resolve(strict=False)
        except OSError as exc:
            raise AndroidMcpError(f"文件路径不可访问：{candidate}", code="invalid_path") from exc
        try:
            resolved.relative_to(root)
        except ValueError as exc:
            raise AndroidMcpError(
                f"拒绝访问项目根目录之外的路径：{resolved}",
                code="path_escape",
                hint=f"文件必须位于 {root} 内。",
            ) from exc
        relative_parts = {part.lower() for part in resolved.relative_to(root).parts[:-1]}
        basename = resolved.name.lower()
        if not allow_artifact and relative_parts & {name.lower() for name in PROTECTED_DIRECTORIES}:
            raise AndroidMcpError(
                f"拒绝访问受保护目录：{resolved}",
                code="protected_path",
                hint="请使用 android_build/android_task 查看构建产物或构建日志。",
            )
        if basename in {name.lower() for name in PROTECTED_NAMES}:
            raise AndroidMcpError(
                f"拒绝修改或读取敏感配置文件：{resolved.name}",
                code="sensitive_file",
                hint="环境检测会按需读取 local.properties，但 android_file 不开放该文件。",
            )
        if not allow_artifact and resolved.suffix.lower() in PROTECTED_SUFFIXES:
            raise AndroidMcpError(f"拒绝访问密钥文件：{resolved.name}", code="sensitive_file")
        stem_tokens = set(resolved.stem.lower().replace("-", "_").split("_"))
        if stem_tokens & PROTECTED_TOKENS:
            raise AndroidMcpError(f"拒绝访问疑似敏感文件：{resolved.name}", code="sensitive_file")
        if not allow_missing and not resolved.is_file() and not (allow_directory and resolved.is_dir()):
            raise AndroidMcpError(f"文件不存在：{resolved}", code="file_not_found")
        return resolved

    def is_inside(self, root: Path, candidate: Path) -> bool:
        try:
            candidate.resolve().relative_to(root.resolve())
            return True
        except ValueError:
            return False
