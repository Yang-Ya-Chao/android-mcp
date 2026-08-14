"""Project and official Android knowledge base service.

The project corpus is intentionally exact and local.  The official corpus is a
curated, versioned allow-list maintained by :mod:`kb_catalog`.  Search results
carry stable source metadata so a caller can create an evidence record before it
changes code.
"""

from __future__ import annotations

import hashlib
import json
import re
import tempfile
from pathlib import Path
from typing import Any

from ..config import ConfigManager
from ..models import AndroidMcpError, now_iso, ok
from ..paths import PathPolicy
from .file_service import FileService
from .kb_catalog import OfficialSourceCatalog
from .task_manager import TaskManager


_SCOPES = {"all", "project", "official", "google", "aosp", "xiaomi"}
_PROJECT_SUFFIXES = {".kt", ".kts", ".java", ".xml", ".gradle", ".md"}
_EXCLUDED_PARTS = {".git", ".gradle", ".idea", "build", "captures", "__history"}


class KnowledgeBaseService:
    def __init__(self, tasks: TaskManager, config: ConfigManager | None = None) -> None:
        self.tasks = tasks
        self.config = config or ConfigManager()
        self.policy = PathPolicy()
        self.file_service = FileService()
        self.catalog = OfficialSourceCatalog(self.config)

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
        source_id: str | None = None,
        locator: str | None = None,
        source_ids: list[str] | None = None,
        scope: str = "all",
        api_level: int | None = None,
        target_sdk: int | None = None,
        vendor: str | None = None,
        os_name: str | None = None,
        require_citation: bool = False,
        evidence_id: str | None = None,
        **_: Any,
    ) -> dict[str, Any]:
        action = action or "search"
        root = self.policy.root(project_root)
        if action == "build":
            dedupe = f"{root}|kb-build"
            record = self.tasks.submit(
                "android_kb",
                lambda progress, cancelled: self._build(root, progress, cancelled, rebuild),
                dedupe_key=dedupe,
            )
            return ok(
                {"task_id": record.task_id, "status": record.status.lower()},
                hint="项目源码索引已异步提交，请使用 android_task(action=\"result\")。",
            )
        if action == "sync_sources":
            selected = sorted(set(source_ids or []))
            dedupe = "official-kb-sync|" + ",".join(selected or ["all"])
            record = self.tasks.submit(
                "android_kb_sync",
                lambda progress, cancelled: self.catalog.sync(source_ids=selected or None),
                dedupe_key=dedupe,
            )
            return ok(
                {"task_id": record.task_id, "status": record.status.lower(), "source_ids": selected},
                hint="官方资料同步已异步提交，请使用 android_task(action=\"result\")。",
            )
        if action == "catalog":
            return ok({"version": 1, "sources": self.catalog.sources()})
        if action == "stats":
            return ok(self.stats(root))
        if action == "search":
            if not query:
                raise AndroidMcpError("android_kb search 需要 query。", code="missing_query")
            return ok(
                self.search(
                    root,
                    query,
                    search_type,
                    top_k,
                    scope=scope,
                    api_level=api_level,
                    target_sdk=target_sdk,
                    vendor=vendor,
                    os_name=os_name,
                    require_citation=require_citation,
                )
            )
        if action in {"read", "read_source"}:
            if source_id:
                return ok(self.catalog.read(source_id, locator))
            if not file_path:
                raise AndroidMcpError("android_kb read 需要 file_path 或 source_id。", code="missing_file_path")
            return ok(self.read(root, file_path))
        if action == "verify":
            if not evidence_id:
                raise AndroidMcpError("android_kb verify 需要 evidence_id。", code="missing_evidence_id")
            return ok(self.verify(root, evidence_id))
        raise AndroidMcpError(f"android_kb 不支持 action：{action}", code="unsupported_action")

    def _index_path(self, root: Path) -> Path:
        return self.config.runtime_dir(root) / "kb-index.json"

    def _evidence_path(self, root: Path) -> Path:
        return self.config.runtime_dir(root) / "knowledge" / "evidence.json"

    def _build(self, root: Path, progress: Any, cancelled: Any, rebuild: bool) -> dict[str, Any]:
        runtime = self.config.runtime_dir(root)
        runtime.mkdir(parents=True, exist_ok=True)
        files = self._source_files(root)
        records: list[dict[str, Any]] = []
        total = max(1, len(files))
        for index, path in enumerate(files, 1):
            if cancelled():
                return {"status": "cancelled", "indexed": len(records)}
            try:
                content = path.read_text(encoding="utf-8", errors="replace")
                stat = path.stat()
            except OSError:
                continue
            relative = str(path.relative_to(root)).replace("\\", "/")
            records.append(
                {
                    "id": f"project:{relative}:{_sha256(content)[:12]}",
                    "source": "project",
                    "authority": "project",
                    "kind": "source",
                    "path": relative,
                    "title": path.name,
                    "locator": f"{relative}#file",
                    "content": content[:200_000],
                    "truncated": len(content) > 200_000,
                    "suffix": path.suffix.lower(),
                    "mtime_ns": stat.st_mtime_ns,
                    "size": stat.st_size,
                    "line_count": len(content.splitlines()),
                    "content_hash": _sha256(content),
                    "fetched_at": now_iso(),
                }
            )
            if index == 1 or index % 25 == 0 or index == total:
                progress(
                    current_step="index",
                    progress=index / total * 100,
                    total_steps=1,
                    message=f"索引 {index}/{total} 个项目文件",
                )
        payload = {
            "version": 2,
            "backend": "hybrid_lexical",
            "built_at": now_iso(),
            "root": str(root),
            "records": records,
            "rebuild": bool(rebuild),
        }
        _atomic_json_write(self._index_path(root), payload)
        return {
            "status": "completed",
            "indexed": len(records),
            "backend": payload["backend"],
            "path": str(self._index_path(root)),
        }

    def stats(self, root: Path) -> dict[str, Any]:
        payload = self._load(root)
        project_records = payload.get("records", []) if payload else []
        official_records = self.catalog.records()
        evidence = self._load_evidence(root)
        return {
            "project_root": str(root),
            "backend": payload.get("backend", "not_built") if payload else "not_built",
            "indexed_files": len(project_records),
            "official_records": len(official_records),
            "official_index_path": str(self.catalog.index_path),
            "evidence_records": len(evidence),
            "built_at": payload.get("built_at") if payload else None,
            "official_built_at": self.catalog.load_index().get("built_at"),
            "fresh": self._is_fresh(root, project_records),
        }

    def search(
        self,
        root: Path,
        query: str,
        search_type: str,
        top_k: int,
        *,
        scope: str = "all",
        api_level: int | None = None,
        target_sdk: int | None = None,
        vendor: str | None = None,
        os_name: str | None = None,
        require_citation: bool = False,
    ) -> dict[str, Any]:
        if scope not in _SCOPES:
            raise AndroidMcpError(
                f"不支持的 scope：{scope}",
                code="invalid_scope",
                hint=f"可用 scope：{', '.join(sorted(_SCOPES))}",
            )
        if search_type not in {"all", "path", "class", "function", "record"}:
            raise AndroidMcpError(f"不支持的 search_type：{search_type}", code="invalid_search_type")

        payload = self._load(root)
        if scope in {"all", "project"} and not payload:
            self._build(root, lambda **_: None, lambda: False, False)
            payload = self._load(root)

        candidates: list[dict[str, Any]] = []
        if scope in {"all", "project"} and payload:
            candidates.extend(payload.get("records", []))
        if scope != "project":
            candidates.extend(self.catalog.records())

        tokens = [
            token.lower()
            for token in re.findall(r"[A-Za-z_][A-Za-z0-9_.:$-]*|[\u4e00-\u9fff]+", query)
            if token
        ]
        results: list[dict[str, Any]] = []
        for record in candidates:
            if not self._scope_matches(record, scope):
                continue
            if not self._context_matches(record, api_level, target_sdk, vendor, os_name):
                continue
            content = str(record.get("content", ""))
            path = str(record.get("path", ""))
            title = str(record.get("title", ""))
            if not content:
                continue
            if record.get("source") == "project":
                if search_type == "path" and not any(token in path.lower() for token in tokens):
                    continue
                if search_type == "class" and not re.search(r"\b(class|interface|object|enum)\b", content):
                    continue
                if search_type == "function" and not re.search(r"\b(fun|function|def)\b", content):
                    continue
                if search_type == "record" and not re.search(r"\b(data\s+class|record|sealed\s+class)\b", content):
                    continue
            haystack = " ".join(
                [
                    path,
                    title,
                    str(record.get("locator", "")),
                    " ".join(str(item) for item in record.get("tags", [])),
                    content,
                ]
            ).lower()
            score = sum(haystack.count(token) for token in tokens)
            score += sum(path.lower().count(token) * 5 for token in tokens)
            score += sum(title.lower().count(token) * 4 for token in tokens)
            if query.lower() in haystack:
                score += 10
            if score <= 0:
                continue
            results.append(self._result_record(record, score, tokens))

        results.sort(key=lambda item: (-item["score"], item.get("authority", ""), item.get("id", "")))
        results = results[: max(1, min(int(top_k), 100))]
        authoritative = any(item.get("source") == "official" for item in results)
        if require_citation and not authoritative:
            raise AndroidMcpError(
                "没有找到可引用的官方或 AOSP 依据。",
                code="evidence_insufficient",
                hint="先同步官方来源，或降低查询范围后重新检索；不要凭猜测修改代码。",
            )
        evidence_id = self._save_evidence(
            root,
            query=query,
            scope=scope,
            api_level=api_level,
            target_sdk=target_sdk,
            vendor=vendor,
            os_name=os_name,
            results=results,
        ) if results else None
        return {
            "query": query,
            "scope": scope,
            "search_type": search_type,
            "backend": "hybrid_lexical",
            "results": results,
            "result_count": len(results),
            "evidence_id": evidence_id,
            "authoritative": authoritative,
            "project_fresh": self._is_fresh(root, payload.get("records", [])) if payload else False,
            "official_indexed": bool(self.catalog.records()),
        }

    def read(self, root: Path, file_path: str) -> dict[str, Any]:
        result = self.file_service.read(project_root=str(root), file_path=file_path)
        return {
            "path": file_path,
            "content": result["data"]["content"],
            "hint": "完整项目源码请继续使用 android_file(action=\"read\")。",
        }

    def verify(self, root: Path, evidence_id: str) -> dict[str, Any]:
        evidence = self.get_evidence(root, evidence_id)
        current_ids = {str(item.get("id")) for item in self.catalog.records()}
        project_payload = self._load(root)
        project_fresh = self._is_fresh(root, project_payload.get("records", [])) if project_payload else False
        invalid_sources: list[str] = []
        for source in evidence.get("sources", []):
            if source.get("source") == "official" and source.get("id") not in current_ids:
                invalid_sources.append(str(source.get("id")))
        verified = not invalid_sources and (project_fresh or not evidence.get("has_project_source"))
        return {
            "evidence_id": evidence_id,
            "verified": verified,
            "project_fresh": project_fresh,
            "invalid_sources": invalid_sources,
            "created_at": evidence.get("created_at"),
            "sources": evidence.get("sources", []),
        }

    def get_evidence(self, root: Path, evidence_id: str) -> dict[str, Any]:
        record = next((item for item in self._load_evidence(root) if item.get("evidence_id") == evidence_id), None)
        if not record:
            raise AndroidMcpError(f"证据记录不存在：{evidence_id}", code="evidence_not_found")
        return record

    def _result_record(self, record: dict[str, Any], score: int, tokens: list[str]) -> dict[str, Any]:
        content = str(record.get("content", ""))
        snippet = _snippet(content, tokens)
        return {
            "id": record.get("id"),
            "source": record.get("source"),
            "authority": record.get("authority"),
            "kind": record.get("kind"),
            "title": record.get("title"),
            "url": record.get("url"),
            "path": record.get("path"),
            "locator": record.get("locator"),
            "score": score,
            "snippet": snippet[:800],
            "version": record.get("version"),
            "api_level": record.get("api_level"),
            "os": record.get("os"),
            "updated_at": record.get("updated_at"),
            "fetched_at": record.get("fetched_at"),
            "content_hash": record.get("content_hash"),
            "license_ref": record.get("license_ref"),
        }

    def _save_evidence(
        self,
        root: Path,
        *,
        query: str,
        scope: str,
        api_level: int | None,
        target_sdk: int | None,
        vendor: str | None,
        os_name: str | None,
        results: list[dict[str, Any]],
    ) -> str:
        evidence_id = f"ev_{_sha256(f'{root}|{query}|{now_iso()}')[:16]}"
        sources = [
            {
                "id": item.get("id"),
                "source": item.get("source"),
                "authority": item.get("authority"),
                "title": item.get("title"),
                "url": item.get("url"),
                "path": item.get("path"),
                "locator": item.get("locator"),
                "content_hash": item.get("content_hash"),
            }
            for item in results
        ]
        record = {
            "evidence_id": evidence_id,
            "created_at": now_iso(),
            "project_root": str(root),
            "query": query,
            "scope": scope,
            "api_level": api_level,
            "target_sdk": target_sdk,
            "vendor": vendor,
            "os": os_name,
            "result_ids": [item.get("id") for item in results],
            "has_project_source": any(item.get("source") == "project" for item in results),
            "has_official_source": any(item.get("source") == "official" for item in results),
            "sources": sources,
        }
        records = self._load_evidence(root)
        records = [item for item in records if item.get("evidence_id") != evidence_id]
        records.append(record)
        _atomic_json_write(self._evidence_path(root), {"version": 1, "records": records[-200:]})
        return evidence_id

    def _load_evidence(self, root: Path) -> list[dict[str, Any]]:
        try:
            value = json.loads(self._evidence_path(root).read_text(encoding="utf-8"))
            records = value.get("records", []) if isinstance(value, dict) else []
            return [item for item in records if isinstance(item, dict)]
        except (OSError, ValueError):
            return []

    def _load(self, root: Path) -> dict[str, Any] | None:
        path = self._index_path(root)
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
            return payload if isinstance(payload, dict) else None
        except (OSError, ValueError):
            return None

    def _source_files(self, root: Path) -> list[Path]:
        result: list[Path] = []
        for path in root.rglob("*"):
            if not path.is_file() or path.suffix.lower() not in _PROJECT_SUFFIXES:
                continue
            if {part.lower() for part in path.relative_to(root).parts} & _EXCLUDED_PARTS:
                continue
            result.append(path)
            if len(result) >= 2_000:
                break
        return sorted(result)

    @staticmethod
    def _scope_matches(record: dict[str, Any], scope: str) -> bool:
        if scope == "all":
            return True
        if scope == "project":
            return record.get("source") == "project"
        if scope == "official":
            return record.get("source") == "official"
        return record.get("authority") == scope

    @staticmethod
    def _context_matches(
        record: dict[str, Any],
        api_level: int | None,
        target_sdk: int | None,
        vendor: str | None,
        os_name: str | None,
    ) -> bool:
        if api_level is not None and record.get("api_level") not in {None, api_level}:
            return False
        if target_sdk is not None and record.get("target_sdk") not in {None, target_sdk}:
            return False
        if vendor and vendor.lower() == "xiaomi" and record.get("source") == "official":
            return record.get("authority") == "xiaomi"
        if os_name and record.get("os") and os_name.lower() not in str(record.get("os")).lower():
            return False
        return True

    def _is_fresh(self, root: Path, records: list[dict[str, Any]]) -> bool:
        if not records:
            return False
        for record in records:
            path = root / str(record.get("path", ""))
            if not path.is_file() or path.stat().st_mtime_ns != record.get("mtime_ns"):
                return False
            try:
                content = path.read_text(encoding="utf-8", errors="replace")
            except OSError:
                return False
            if record.get("content_hash") and _sha256(content) != record.get("content_hash"):
                return False
        return True


def _snippet(content: str, tokens: list[str]) -> str:
    lines = content.splitlines() or [content]
    for index, line in enumerate(lines):
        if any(token in line.lower() for token in tokens):
            low = max(0, index - 1)
            high = min(len(lines), index + 2)
            return "\n".join(lines[low:high]).strip()
    return content[:800].strip()


def _sha256(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _atomic_json_write(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = tempfile.NamedTemporaryFile(
        mode="w",
        encoding="utf-8",
        newline="\n",
        prefix=f".{path.name}.",
        suffix=".tmp",
        dir=path.parent,
        delete=False,
    )
    temporary_path = Path(temporary.name)
    try:
        with temporary:
            json.dump(payload, temporary, ensure_ascii=False, indent=2)
            temporary.write("\n")
            temporary.flush()
        temporary_path.replace(path)
    except Exception:
        try:
            temporary_path.unlink(missing_ok=True)
        except OSError:
            pass
        raise
