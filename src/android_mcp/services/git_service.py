"""Read-only Git information with no arbitrary command surface."""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any

from ..models import AndroidMcpError, ok
from ..paths import PathPolicy


class GitService:
    def __init__(self) -> None:
        self.policy = PathPolicy()

    def handle(self, *, action: str | None, project_root: str | None = None, limit: int = 20, **_: Any) -> dict[str, Any]:
        root = self.policy.root(project_root)
        if action == "git_status":
            command = ["git", "status", "--short", "--branch"]
        elif action == "git_log":
            command = ["git", "log", "-n", str(max(1, min(int(limit), 100))), "--oneline", "--decorate"]
        elif action == "git_diff":
            command = ["git", "diff", "--stat"]
        else:
            raise AndroidMcpError(f"code_hosting 不支持 action：{action}", code="unsupported_action")
        try:
            completed = subprocess.run(command, cwd=root, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=30, check=False)
        except OSError as exc:
            raise AndroidMcpError(f"Git 执行失败：{exc}", code="git_failed") from exc
        return ok({"project_root": str(root), "action": action, "exit_code": completed.returncode, "stdout": completed.stdout[-10000:], "stderr": completed.stderr[-2000:]})
