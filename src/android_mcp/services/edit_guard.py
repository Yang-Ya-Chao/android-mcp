"""Lazy edit guard for changes made outside the MCP editor."""

from __future__ import annotations

import hashlib
import threading
import time
from pathlib import Path
from typing import Any

from ..models import AndroidMcpError


def fingerprint(path: Path) -> str:
    try:
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        return digest
    except OSError:
        return "<missing>"


class EditGuard:
    def __init__(self, mode: str = "warn", ttl_seconds: int = 900) -> None:
        self.mode = mode if mode in {"warn", "strict"} else "warn"
        self.ttl_seconds = ttl_seconds
        self._known: dict[str, tuple[str, float]] = {}
        self._external: dict[str, dict[str, Any]] = {}
        self._lock = threading.RLock()

    def observe(self, path: Path) -> None:
        with self._lock:
            key = str(path)
            self._known[key] = (fingerprint(path), time.time())
            self._external.pop(key, None)

    def authorize(self, path: Path) -> None:
        self.observe(path)

    def check(self, path: Path, *, for_write: bool = False) -> None:
        with self._lock:
            key = str(path)
            current = fingerprint(path)
            previous = self._known.get(key)
            if previous and previous[0] != current:
                self._external[key] = {
                    "path": key,
                    "detected_at": time.time(),
                    "previous_fingerprint": previous[0],
                    "current_fingerprint": current,
                }
            if for_write and self.mode == "strict" and key in self._external:
                raise AndroidMcpError(
                    f"检测到未授权的外部修改：{path}",
                    code="external_edit_detected",
                    hint="先使用 android_file(action=\"read\") 检查外部变更，确认后再写入。",
                )

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            return {
                "enabled": True,
                "mode": self.mode,
                "tracked_files": len(self._known),
                "external_modifications": list(self._external.values()),
            }
