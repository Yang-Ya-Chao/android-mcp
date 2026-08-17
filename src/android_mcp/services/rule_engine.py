"""Server-side coding gates for evidence-first Android changes."""

from __future__ import annotations

import json
import threading
from pathlib import Path
from typing import Any

from ..config import ConfigManager
from ..models import AndroidMcpError, now_iso


class RuleEngine:
    """Validate that Android code changes are backed by a fresh KB search."""

    OFFICIAL_CHANGE_TYPES = {
        "api",
        "compatibility",
        "dependency",
        "manifest",
        "permission",
        "background",
        "device",
    }
    NON_OFFICIAL_CHANGE_TYPES = {
        "algorithm",
        "implementation",
        "code",
        "test",
        "docs",
    }
    _audit_lock = threading.Lock()

    def __init__(self, knowledge_base: Any, config: ConfigManager | None = None) -> None:
        self.knowledge_base = knowledge_base
        self.config = config or ConfigManager()

    def validate_write(
        self,
        *,
        project_root: str | None,
        file_path: str,
        action: str,
        evidence_ids: list[str] | None,
        change_type: str | None,
        change_reason: str | None,
        vendor: str | None = None,
        api_level: int | None = None,
        target_sdk: int | None = None,
    ) -> dict[str, Any]:
        normalized_ids = _normalize_ids(evidence_ids)
        normalized_type = self.infer_change_type(action, change_type)
        if normalized_type == "format" and action != "format":
            raise AndroidMcpError(
                "只有 android_file(action=\"format\") 可以声明 format 变更。",
                code="invalid_change_type",
                hint="代码替换、写入或依赖修改仍需提供 evidence_ids。",
            )
        if normalized_type == "format" and not normalized_ids:
            result = {
                "allowed": True,
                "change_type": normalized_type,
                "evidence_ids": [],
                "reason": change_reason or "format-only change",
            }
            self._audit(project_root, file_path, action, result)
            return result
        if not normalized_ids:
            raise AndroidMcpError(
                "代码写入前必须先检索知识库并提供 evidence_ids。",
                code="knowledge_required",
                hint=(
                    "先调用 android_kb(action=\"search\", require_citation=true)，"
                    "再把返回的 evidence_id 传给 android_file。"
                ),
            )

        root = self.knowledge_base.policy.root(project_root)
        evidence_records: list[dict[str, Any]] = []
        for evidence_id in normalized_ids:
            evidence = self.knowledge_base.get_evidence(root, evidence_id)
            verification = self.knowledge_base.verify(root, evidence_id)
            if not verification.get("verified"):
                raise AndroidMcpError(
                    f"证据已过期或无法验证：{evidence_id}",
                    code="evidence_stale",
                    hint="重新构建项目索引或重新同步官方资料后再写入。",
                )
            evidence_records.append(evidence)

        has_official = any(item.get("has_official_source") for item in evidence_records)
        has_github = any(
            item.get("has_github_source")
            or any(source.get("source") == "github" for source in item.get("sources", []))
            for item in evidence_records
        )
        requires_official = normalized_type in self.OFFICIAL_CHANGE_TYPES or (vendor or "").lower() == "xiaomi"
        if requires_official and not has_official:
            raise AndroidMcpError(
                f"{normalized_type} 变更必须有 Google/AOSP/Xiaomi 官方依据。",
                code="official_evidence_required",
                hint="使用 scope=official 或 vendor=xiaomi 重新检索，并保留对应版本信息。",
            )

        authorities = sorted(
            {
                str(source.get("authority"))
                for evidence in evidence_records
                for source in evidence.get("sources", [])
                if source.get("authority")
            }
        )
        result = {
            "allowed": True,
            "change_type": normalized_type,
            "evidence_ids": normalized_ids,
            "authorities": authorities,
            "has_official_source": has_official,
            "has_github_source": has_github,
            "has_non_official_source": has_github,
            "requires_official": requires_official,
            "context": {
                "vendor": vendor,
                "api_level": api_level,
                "target_sdk": target_sdk,
            },
            "reason": change_reason or "evidence-backed Android change",
        }
        self._audit(project_root, file_path, action, result)
        return result

    @classmethod
    def infer_change_type(cls, action: str, change_type: str | None) -> str:
        if change_type:
            value = change_type.strip().lower().replace("-", "_")
            allowed = cls.OFFICIAL_CHANGE_TYPES | cls.NON_OFFICIAL_CHANGE_TYPES | {"format"}
            if value not in allowed:
                raise AndroidMcpError(f"不支持的 change_type：{change_type}", code="invalid_change_type")
            return value
        return {
            "manifest": "manifest",
            "dependencies": "dependency",
            "format": "format",
            "encode": "code",
        }.get(action, "code")

    def _audit(self, project_root: str | None, file_path: str, action: str, result: dict[str, Any]) -> None:
        root = self.knowledge_base.policy.root(project_root)
        path = self.config.runtime_dir(root) / "knowledge" / "rule-audit.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        event = {
            "timestamp": now_iso(),
            "project_root": str(root),
            "file_path": file_path,
            "action": action,
            "result": result,
        }
        with self._audit_lock:
            with path.open("a", encoding="utf-8", newline="\n") as stream:
                stream.write(json.dumps(event, ensure_ascii=False) + "\n")


def _normalize_ids(evidence_ids: list[str] | None) -> list[str]:
    if not evidence_ids:
        return []
    result: list[str] = []
    for value in evidence_ids:
        if value and value not in result:
            result.append(str(value))
    return result
