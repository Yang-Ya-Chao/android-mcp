"""Lightweight project knowledge base with a future vector-backend seam."""

from __future__ import annotations

import json
import re
import time
from pathlib import Path
from typing import Any

from ..config import ConfigManager
from ..models import AndroidMcpError, ok
from ..paths import PathPolicy
from .file_service import FileService
from .task_manager import TaskManager


class KnowledgeBaseService:
    def __init__(self, tasks: TaskManager, config: ConfigManager | None = None) -> None:
        self.tasks = tasks
        self.config = config or ConfigManager()
        self.policy = PathPolicy()
        self.file_service = FileService()

    def handle(
        self,
        *,
        action: str | None,
        project_root: str | None = None,
        query: str | None = None,
        search_type: str = "all",
        top_k: int = 20,
        rebuild: bool = False,
        file_path: str | None = None,
        **_: Any,
    ) -> dict[str, Any]:
        action = action or "search"
        root = self.policy.root(project_root)
        if action == "build":
            dedupe = f"{root}|kb-build"
            record = self.tasks.submit("android_kb", lambda progress, cancelled: self._build(root, progress, cancelled, rebuild), dedupe_key=dedupe)
            return ok({"task_id": record.task_id, "status": record.status.lower()}, hint="知识库构建已异步提交。")
        if action == "stats":
            return ok(self.stats(root))
        if action == "search":
            if not query:
                raise AndroidMcpError("android_kb search 需要 query。", code="missing_query")
            return ok(self.search(root, query, search_type, top_k))
        if action == "read":
            if not file_path:
                raise AndroidMcpError("android_kb read 需要 file_path。", code="missing_file_path")
            return ok(self.read(root, file_path))
        raise AndroidMcpError(f"android_kb 不支持 action：{action}", code="unsupported_action")

    def _index_path(self, root: Path) -> Path:
        return self.config.runtime_dir(root) / "kb-index.json"

    def _build(self, root: Path, progress: Any, cancelled: Any, rebuild: bool) -> dict[str, Any]:
        runtime = self.config.runtime_dir(root)
        runtime.mkdir(parents=True, exist_ok=True)
        files = self._source_files(root)
        records = []
        total = max(1, len(files))
        for index, path in enumerate(files, 1):
            if cancelled():
                return {"status": "cancelled", "indexed": len(records)}
            try:
                content = path.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            relative = str(path.relative_to(root)).replace("\\", "/")
            records.append({"path": relative, "suffix": path.suffix.lower(), "mtime_ns": path.stat().st_mtime_ns, "size": path.stat().st_size, "content": content[:200_000]})
            if index == 1 or index % 25 == 0 or index == total:
                progress(current_step="index", progress=index / total * 100, total_steps=1, message=f"索引 {index}/{total} 个文件")
        payload = {"version": 1, "backend": "lexical_fallback", "built_at": time.time(), "root": str(root), "records": records}
        self._index_path(root).write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        return {"status": "completed", "indexed": len(records), "backend": "lexical_fallback", "path": str(self._index_path(root))}

    def stats(self, root: Path) -> dict[str, Any]:
        payload = self._load(root)
        records = payload.get("records", []) if payload else []
        return {"project_root": str(root), "backend": payload.get("backend", "not_built") if payload else "not_built", "indexed_files": len(records), "built_at": payload.get("built_at") if payload else None, "fresh": self._is_fresh(root, records)}

    def search(self, root: Path, query: str, search_type: str, top_k: int) -> dict[str, Any]:
        if search_type not in {"all", "path", "class", "function", "record"}:
            raise AndroidMcpError(f"不支持的 search_type：{search_type}", code="invalid_search_type")
        payload = self._load(root)
        if not payload:
            self._build(root, lambda **_: None, lambda: False, False)
            payload = self._load(root)
        tokens = [token.lower() for token in re.findall(r"[A-Za-z_][A-Za-z0-9_.-]*|[\u4e00-\u9fff]+", query) if token]
        results: list[dict[str, Any]] = []
        for record in payload.get("records", []):
            path = record["path"]
            content = record.get("content", "")
            if search_type == "path" and not any(token in path.lower() for token in tokens):
                continue
            if search_type == "class" and not re.search(r"\b(class|interface|object|enum)\b", content):
                continue
            if search_type == "function" and not re.search(r"\b(fun|function|def)\b", content):
                continue
            if search_type == "record" and not re.search(r"\b(data\s+class|record|sealed\s+class)\b", content):
                continue
            score = sum(path.lower().count(token) * 5 + content.lower().count(token) for token in tokens)
            if score <= 0:
                continue
            match_line = next((line for line in content.splitlines() if any(token in line.lower() for token in tokens)), "")
            results.append({"path": path, "score": score, "snippet": match_line[:500], "source": "project"})
        results.sort(key=lambda item: (-item["score"], item["path"]))
        return {"query": query, "search_type": search_type, "backend": payload.get("backend", "lexical_fallback"), "results": results[: max(1, min(int(top_k), 100))], "fresh": self._is_fresh(root, payload.get("records", []))}

    def read(self, root: Path, file_path: str) -> dict[str, Any]:
        result = self.file_service.read(project_root=str(root), file_path=file_path)
        return {"path": file_path, "content": result["data"]["content"], "hint": "完整源码请继续使用 android_file(action=\"read\")。"}

    def _load(self, root: Path) -> dict[str, Any] | None:
        path = self._index_path(root)
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
            return payload if isinstance(payload, dict) else None
        except (OSError, ValueError):
            return None

    def _source_files(self, root: Path) -> list[Path]:
        result = []
        for path in root.rglob("*"):
            if not path.is_file() or path.suffix.lower() not in {".kt", ".kts", ".java", ".xml", ".gradle", ".md"}:
                continue
            if {part.lower() for part in path.relative_to(root).parts} & {".git", ".gradle", ".idea", "build", "captures", "__history"}:
                continue
            result.append(path)
            if len(result) >= 2000:
                break
        return sorted(result)

    def _is_fresh(self, root: Path, records: list[dict[str, Any]]) -> bool:
        if not records:
            return False
        for record in records:
            path = root / record["path"]
            if not path.is_file() or path.stat().st_mtime_ns != record.get("mtime_ns"):
                return False
        return True
