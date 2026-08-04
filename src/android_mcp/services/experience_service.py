"""Local problem/solution experience store."""

from __future__ import annotations

import json
import time
import uuid
from pathlib import Path
from typing import Any

from ..config import ConfigManager
from ..models import AndroidMcpError, ok


class ExperienceService:
    def __init__(self, config: ConfigManager | None = None) -> None:
        self.config = config or ConfigManager()

    def handle(
        self,
        *,
        action: str | None,
        problem: str | None = None,
        solution: str | None = None,
        tags: list[str] | None = None,
        tools_used: list[str] | None = None,
        experience_id: str | None = None,
        query: str | None = None,
        limit: int = 20,
        **_: Any,
    ) -> dict[str, Any]:
        action = action or "list"
        records = self._load()
        if action == "save":
            if not problem or not solution:
                raise AndroidMcpError("experience save 需要 problem 和 solution。", code="missing_experience_fields")
            record = {"id": f"exp_{uuid.uuid4().hex[:12]}", "problem": problem, "solution": solution, "tags": tags or [], "tools_used": tools_used or [], "timestamp": time.time()}
            records.append(record)
            self._save(records)
            return ok(record)
        if action == "get":
            record = next((item for item in records if item.get("id") == experience_id), None)
            if not record:
                raise AndroidMcpError(f"经验不存在：{experience_id}", code="experience_not_found")
            return ok(record)
        if action == "list":
            return ok({"experiences": sorted(records, key=lambda item: item.get("timestamp", 0), reverse=True)[: max(1, min(limit, 200))], "count": len(records)})
        if action == "search":
            if not query:
                raise AndroidMcpError("experience search 需要 query。", code="missing_query")
            tokens = [token.lower() for token in query.split() if token]
            scored = []
            for item in records:
                haystack = " ".join([item.get("problem", ""), item.get("solution", ""), " ".join(item.get("tags", []))]).lower()
                score = sum(haystack.count(token) for token in tokens)
                if score:
                    scored.append((score, item))
            scored.sort(key=lambda pair: (-pair[0], -pair[1].get("timestamp", 0)))
            return ok({"query": query, "results": [{"score": score, **item} for score, item in scored[: max(1, min(limit, 100))]]})
        if action == "prune":
            before = len(records)
            records = _deduplicate(records)
            self._save(records)
            return ok({"before": before, "after": len(records), "removed": before - len(records)})
        raise AndroidMcpError(f"experience 不支持 action：{action}", code="unsupported_action")

    def _path(self) -> Path:
        path = self.config.runtime_dir() / "experiences.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        return path

    def _load(self) -> list[dict[str, Any]]:
        try:
            value = json.loads(self._path().read_text(encoding="utf-8"))
            return value if isinstance(value, list) else []
        except (OSError, ValueError):
            return []

    def _save(self, records: list[dict[str, Any]]) -> None:
        self._path().write_text(json.dumps(records, ensure_ascii=False, indent=2), encoding="utf-8")


def _deduplicate(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    for item in sorted(records, key=lambda value: value.get("timestamp", 0), reverse=True):
        key = (item.get("problem", "").strip().lower(), item.get("solution", "").strip().lower())
        if key in seen:
            continue
        seen.add(key)
        result.append(item)
    return result
