"""Read-only GitHub source search for non-official implementation evidence.

GitHub results are deliberately kept separate from the curated official
catalog.  They can support an algorithm or implementation comparison, but
they must never satisfy an Android platform-contract or OEM-compatibility
requirement on their own.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import tempfile
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode, urlsplit
from urllib.request import Request, urlopen

from ..config import ConfigManager
from ..models import AndroidMcpError, now_iso


_API_BASE = "https://api.github.com"
_API_HOST = "api.github.com"
_REPOSITORY_RE = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
_DEFAULT_MAX_RESULTS = 10
_DEFAULT_MAX_FILE_BYTES = 180_000
_DEFAULT_TIMEOUT_SECONDS = 20


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


class GitHubSourceCatalog:
    """Search and cache public GitHub implementation sources."""

    def __init__(self, config: ConfigManager | None = None) -> None:
        self.config = config or ConfigManager()

    @property
    def index_path(self) -> Path:
        return self.config.runtime_dir() / "knowledge" / "github-index.json"

    def records(self) -> list[dict[str, Any]]:
        try:
            payload = json.loads(self.index_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return []
        records = payload.get("records", []) if isinstance(payload, dict) else []
        return [item for item in records if isinstance(item, dict)]

    def sources(self) -> list[dict[str, Any]]:
        """Return compact cached repository metadata without source contents."""

        result: list[dict[str, Any]] = []
        seen: set[str] = set()
        for record in self.records():
            repository = str(record.get("repository") or "")
            if not repository or repository in seen:
                continue
            seen.add(repository)
            result.append(
                {
                    "id": f"github:{repository}",
                    "source": "github",
                    "authority": "github",
                    "source_tier": "non_official",
                    "repository": repository,
                    "url": record.get("repository_url") or f"https://github.com/{repository}",
                    "license_ref": record.get("license_ref"),
                    "fetched_at": record.get("fetched_at"),
                }
            )
        return result

    def search(
        self,
        query: str,
        *,
        top_k: int = _DEFAULT_MAX_RESULTS,
        timeout_seconds: int | None = None,
        max_file_bytes: int | None = None,
    ) -> dict[str, Any]:
        query = str(query or "").strip()
        if not query:
            raise AndroidMcpError("GitHub 检索需要 query。", code="missing_query")
        if len(query) > 256:
            raise AndroidMcpError("GitHub 检索 query 不能超过 256 个字符。", code="invalid_query")

        settings = self._settings()
        if settings.get("enabled", True) is False:
            raise AndroidMcpError(
                "GitHub 知识库检索已禁用。",
                code="github_disabled",
                hint="在全局或项目配置的 github.enabled 中启用后重试。",
            )
        timeout = _bounded_int(
            timeout_seconds if timeout_seconds is not None else settings.get("timeout_seconds"),
            _DEFAULT_TIMEOUT_SECONDS,
            1,
            60,
        )
        max_bytes = _bounded_int(
            max_file_bytes if max_file_bytes is not None else settings.get("max_file_bytes"),
            _DEFAULT_MAX_FILE_BYTES,
            10_000,
            1_000_000,
        )
        limit = _bounded_int(top_k, _DEFAULT_MAX_RESULTS, 1, 20)
        token = self._token(settings)
        headers = self._headers(token)

        payload = self._request_json(
            "/search/code",
            {"q": query, "per_page": limit},
            headers=headers,
            timeout=timeout,
        )
        items = payload.get("items", []) if isinstance(payload, dict) else []
        if not isinstance(items, list):
            items = []

        records: list[dict[str, Any]] = []
        skipped: list[dict[str, str]] = []
        repository_cache: dict[str, dict[str, Any]] = {}
        for item in items[:limit]:
            if not isinstance(item, dict):
                continue
            repository_data = item.get("repository") if isinstance(item.get("repository"), dict) else {}
            repository = str(repository_data.get("full_name") or "").strip()
            path = str(item.get("path") or "").strip()
            if not _REPOSITORY_RE.fullmatch(repository) or not path or path.startswith("/"):
                skipped.append({"path": path, "reason": "invalid_repository_or_path"})
                continue

            metadata = repository_cache.get(repository)
            if metadata is None:
                metadata = dict(repository_data)
                if not metadata.get("default_branch"):
                    metadata.update(
                        self._request_json(
                            f"/repos/{_repository_path(repository)}",
                            {},
                            headers=headers,
                            timeout=timeout,
                        )
                    )
                repository_cache[repository] = metadata
            ref = str(metadata.get("default_branch") or "main")
            try:
                content_payload = self._request_json(
                    f"/repos/{_repository_path(repository)}/contents/{_path(path)}",
                    {"ref": ref},
                    headers=headers,
                    timeout=timeout,
                )
                content, blob_sha, truncated = _decode_content(content_payload, max_bytes)
            except AndroidMcpError as exc:
                skipped.append({"path": path, "reason": exc.code})
                continue
            if not content:
                skipped.append({"path": path, "reason": "empty_or_binary_content"})
                continue

            license_ref = _license_ref(metadata.get("license"))
            record = self._record(
                item=item,
                metadata=metadata,
                repository=repository,
                path=path,
                ref=ref,
                content=content,
                blob_sha=blob_sha,
                license_ref=license_ref,
                truncated=truncated,
            )
            records.append(record)

        self._save_records(records)
        return {
            "records": records,
            "searched": len(items),
            "fetched": len(records),
            "skipped": skipped,
            "authenticated": bool(token),
            "rate_limit_hint": "GitHub code search has a dedicated rate limit; set GITHUB_TOKEN for authenticated use.",
        }

    def read(self, source_id: str, locator: str | None = None, max_chars: int = 120_000) -> dict[str, Any]:
        candidates = [
            item
            for item in self.records()
            if item.get("source_id") == source_id or item.get("id") == source_id
        ]
        if not candidates and source_id.startswith("github:"):
            candidates = [item for item in self.records() if source_id == str(item.get("source_id"))]
        if locator:
            exact = [item for item in candidates if item.get("locator") == locator or item.get("id") == locator]
            candidates = exact or candidates
        if not candidates:
            raise AndroidMcpError(
                f"GitHub 来源尚未缓存：{source_id}",
                code="source_not_indexed",
                hint='先调用 android_kb(action="search", scope="github")。',
            )
        content = "\n\n".join(str(item.get("content", "")) for item in candidates)
        first = candidates[0]
        return {
            "source_id": source_id,
            "source": "github",
            "authority": "github",
            "source_tier": "non_official",
            "repository": first.get("repository"),
            "ref": first.get("ref"),
            "commit": first.get("commit"),
            "title": first.get("title"),
            "url": first.get("url"),
            "license_ref": first.get("license_ref"),
            "content": content[:max_chars],
            "truncated": len(content) > max_chars,
            "records": len(candidates),
            "locators": [item.get("locator") for item in candidates],
        }

    def _settings(self) -> dict[str, Any]:
        settings = self.config.load().get("github", {})
        return settings if isinstance(settings, dict) else {}

    @staticmethod
    def _token(settings: dict[str, Any]) -> str | None:
        token_env = str(settings.get("token_env") or "GITHUB_TOKEN")
        return os.environ.get(token_env) or os.environ.get("GH_TOKEN")

    @staticmethod
    def _headers(token: str | None, *, raw: bool = False) -> dict[str, str]:
        headers = {
            "Accept": "application/vnd.github.raw+json" if raw else "application/vnd.github+json",
            "User-Agent": "android-mcp-github-kb/0.4",
            "X-GitHub-Api-Version": "2022-11-28",
        }
        if token:
            headers["Authorization"] = f"Bearer {token}"
        return headers

    def _request_json(
        self,
        endpoint: str,
        params: dict[str, Any],
        *,
        headers: dict[str, str],
        timeout: int,
    ) -> dict[str, Any]:
        url = f"{_API_BASE}{endpoint}"
        query = urlencode({key: value for key, value in params.items() if value is not None})
        if query:
            url = f"{url}?{query}"
        request = Request(url, headers=headers, method="GET")
        try:
            with urlopen(request, timeout=timeout) as response:
                final_url = str(response.geturl() or url)
                parts = urlsplit(final_url)
                if parts.scheme != "https" or parts.hostname != _API_HOST:
                    raise AndroidMcpError("GitHub API 重定向到非允许地址。", code="github_redirect_blocked")
                raw = response.read(2_000_001)
        except HTTPError as exc:
            body = exc.read(1_000).decode("utf-8", errors="replace")
            if exc.code in {401, 403}:
                raise AndroidMcpError(
                    "GitHub API 拒绝了请求或触发速率限制。",
                    code="github_auth_or_rate_limited",
                    hint="配置 GITHUB_TOKEN 或 GH_TOKEN 后重试；Token 不会写入日志和证据。",
                ) from exc
            if exc.code == 404:
                raise AndroidMcpError("GitHub 仓库或文件不存在。", code="github_not_found", hint=body[:300]) from exc
            raise AndroidMcpError(f"GitHub API 请求失败：HTTP {exc.code}", code="github_api_error", hint=body[:300]) from exc
        except URLError as exc:
            raise AndroidMcpError("无法连接 GitHub API。", code="github_unavailable", hint=str(exc.reason)[:300]) from exc
        except TimeoutError as exc:
            raise AndroidMcpError("GitHub API 请求超时。", code="github_timeout") from exc
        if len(raw) > 2_000_000:
            raise AndroidMcpError("GitHub API 响应超过大小限制。", code="github_response_too_large")
        try:
            value = json.loads(raw.decode("utf-8", errors="replace"))
        except (TypeError, ValueError) as exc:
            raise AndroidMcpError("GitHub API 返回了无效 JSON。", code="github_invalid_response") from exc
        if not isinstance(value, dict):
            raise AndroidMcpError("GitHub API 返回格式不受支持。", code="github_invalid_response")
        return value

    def _save_records(self, records: list[dict[str, Any]]) -> None:
        if not records:
            return
        existing = {str(item.get("id")): item for item in self.records() if item.get("id")}
        for record in records:
            existing[str(record["id"])] = record
        ordered = list(existing.values())[-500:]
        _atomic_json_write(
            self.index_path,
            {
                "version": 1,
                "backend": "github_rest_contents",
                "built_at": now_iso(),
                "records": ordered,
            },
        )

    @staticmethod
    def _record(
        *,
        item: dict[str, Any],
        metadata: dict[str, Any],
        repository: str,
        path: str,
        ref: str,
        content: str,
        blob_sha: str | None,
        license_ref: str,
        truncated: bool,
    ) -> dict[str, Any]:
        content_hash = hashlib.sha256(content.encode("utf-8")).hexdigest()
        stable_sha = blob_sha or content_hash
        repository_url = f"https://github.com/{repository}"
        html_url = f"{repository_url}/blob/{quote(ref, safe='')}/{quote(path, safe='/')}"
        language = item.get("language") or metadata.get("language")
        tags = ["github", "open-source"]
        if language:
            tags.append(str(language).lower())
        return {
            "id": f"github:{repository}:{path}:{stable_sha}",
            "source_id": f"github:{repository}",
            "source": "github",
            "source_tier": "non_official",
            "authority": "github",
            "kind": "open_source",
            "title": f"{repository}/{path}",
            "url": html_url,
            "repository": repository,
            "repository_url": repository_url,
            "path": path,
            "locator": f"{repository}@{ref}:{path}",
            "content": content,
            "truncated": truncated,
            "version": ref,
            "ref": ref,
            "commit": blob_sha,
            "tags": tags,
            "license_ref": license_ref,
            "stars": metadata.get("stargazers_count"),
            "fork": bool(metadata.get("fork", False)),
            "archived": bool(metadata.get("archived", False)),
            "fetched_at": now_iso(),
            "content_hash": content_hash,
        }


def _repository_path(repository: str) -> str:
    if not _REPOSITORY_RE.fullmatch(repository):
        raise AndroidMcpError(f"GitHub 仓库标识无效：{repository}", code="github_invalid_repository")
    owner, name = repository.split("/", 1)
    return f"{quote(owner, safe='')}/{quote(name, safe='')}"


def _path(path: str) -> str:
    if not path or path.startswith("/") or "\x00" in path:
        raise AndroidMcpError("GitHub 文件路径无效。", code="github_invalid_path")
    return "/".join(quote(part, safe="") for part in path.split("/"))


def _decode_content(payload: dict[str, Any], max_bytes: int) -> tuple[str, str | None, bool]:
    if payload.get("type") != "file":
        raise AndroidMcpError("GitHub 搜索结果不是普通文件。", code="github_not_file")
    encoding = str(payload.get("encoding") or "")
    value = payload.get("content")
    if encoding != "base64" or not isinstance(value, str):
        raise AndroidMcpError("GitHub 文件内容不是可读取的 Base64 文件。", code="github_unsupported_content")
    try:
        raw = base64.b64decode("".join(value.split()), validate=False)
    except (ValueError, TypeError) as exc:
        raise AndroidMcpError("GitHub 文件内容解码失败。", code="github_invalid_content") from exc
    truncated = len(raw) > max_bytes
    if truncated:
        raw = raw[:max_bytes]
    if b"\x00" in raw:
        raise AndroidMcpError("跳过二进制 GitHub 文件。", code="github_binary_file")
    return raw.decode("utf-8", errors="replace"), str(payload.get("sha") or "") or None, truncated


def _license_ref(value: Any) -> str:
    if isinstance(value, dict):
        return str(value.get("spdx_id") or value.get("name") or "unknown")
    return "unknown"


def _bounded_int(value: Any, default: int, minimum: int, maximum: int) -> int:
    try:
        selected = default if value is None else int(value)
    except (TypeError, ValueError) as exc:
        raise AndroidMcpError("数值参数格式无效。", code="invalid_argument") from exc
    if not minimum <= selected <= maximum:
        raise AndroidMcpError(f"数值必须在 {minimum}-{maximum} 之间。", code="invalid_argument")
    return selected
