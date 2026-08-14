"""Curated official-source catalog and bounded document ingestion.

The catalog is deliberately allow-listed.  The service never accepts an arbitrary
URL from an MCP caller, which keeps synchronization auditable and prevents the KB
from becoming an unrestricted web fetch proxy.
"""

from __future__ import annotations

import hashlib
import json
import re
import tempfile
from html.parser import HTMLParser
from importlib import resources
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen
from urllib.parse import urlsplit

from ..config import ConfigManager
from ..models import AndroidMcpError, now_iso


_HEADING_TAGS = {f"h{index}" for index in range(1, 7)}
_BLOCK_TAGS = {
    "address",
    "article",
    "blockquote",
    "br",
    "dd",
    "div",
    "dl",
    "dt",
    "li",
    "p",
    "pre",
    "section",
    "table",
    "td",
    "th",
    "tr",
    "ul",
    "ol",
}
_SKIP_TAGS = {"script", "style", "noscript", "svg", "canvas", "nav", "footer", "form"}


class _HtmlDocumentParser(HTMLParser):
    """Extract readable blocks while retaining headings for source locators."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.title_parts: list[str] = []
        self.blocks: list[dict[str, str]] = []
        self.heading = ""
        self._current: list[str] = []
        self._heading_parts: list[str] = []
        self._in_title = False
        self._in_heading = False
        self._skip_depth = 0
        self._pre_depth = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        tag = tag.lower()
        if tag in _SKIP_TAGS:
            self._skip_depth += 1
            return
        if self._skip_depth:
            return
        if tag == "title":
            self._in_title = True
        if tag in _HEADING_TAGS:
            self._flush()
            self._in_heading = True
            self._heading_parts = []
        if tag == "pre":
            self._pre_depth += 1
        if tag in _BLOCK_TAGS and tag not in _HEADING_TAGS:
            self._flush()

    def handle_endtag(self, tag: str) -> None:
        tag = tag.lower()
        if tag in _SKIP_TAGS:
            self._skip_depth = max(0, self._skip_depth - 1)
            return
        if self._skip_depth:
            return
        if tag == "title":
            self._in_title = False
        if tag in _HEADING_TAGS and self._in_heading:
            value = _clean_text("".join(self._heading_parts))
            if value:
                self.heading = value
            self._in_heading = False
            self._heading_parts = []
        if tag == "pre":
            self._pre_depth = max(0, self._pre_depth - 1)
        if tag in _BLOCK_TAGS or tag in _HEADING_TAGS:
            self._flush()

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.handle_starttag(tag, attrs)
        self.handle_endtag(tag)

    def handle_data(self, data: str) -> None:
        if self._skip_depth:
            return
        if self._in_title:
            self.title_parts.append(data)
        elif self._in_heading:
            self._heading_parts.append(data)
        else:
            self._current.append(data)

    def finish(self) -> dict[str, Any]:
        self._flush()
        return {
            "title": _clean_text("".join(self.title_parts)),
            "blocks": self.blocks,
            "text": "\n\n".join(item["text"] for item in self.blocks),
        }

    def _flush(self) -> None:
        if not self._current:
            return
        raw = "".join(self._current)
        self._current = []
        if self._pre_depth:
            text = re.sub(r"\n{3,}", "\n\n", raw).strip()
        else:
            text = _clean_text(raw)
        if text:
            self.blocks.append({"heading": self.heading, "text": text})


def _clean_text(value: str) -> str:
    value = value.replace("\xa0", " ")
    value = re.sub(r"[ \t\r\n]+", " ", value)
    return value.strip()


def _extract_updated_at(text: str) -> str | None:
    match = re.search(
        r"(?:更新时间|last\s+updated|updated)\s*[:：]?\s*"
        r"(\d{4}[-/]\d{1,2}[-/]\d{1,2}(?:[ T]\d{1,2}:\d{2}(?::\d{2})?)?)",
        text,
        re.IGNORECASE,
    )
    return match.group(1).replace("/", "-") if match else None


def _parse_html(html: str) -> dict[str, Any]:
    parser = _HtmlDocumentParser()
    parser.feed(html)
    parser.close()
    result = parser.finish()
    if not result["blocks"]:
        fallback = re.sub(r"<[^>]+>", " ", html)
        text = _clean_text(fallback)
        if text:
            result["blocks"] = [{"heading": "", "text": text}]
            result["text"] = text
    result["updated_at"] = _extract_updated_at(result["text"])
    return result


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


class OfficialSourceCatalog:
    """Load, synchronize and read the curated official documentation corpus."""

    def __init__(self, config: ConfigManager | None = None) -> None:
        self.config = config or ConfigManager()
        self._sources = self._load_sources()

    @property
    def index_path(self) -> Path:
        return self.config.runtime_dir() / "knowledge" / "official-index.json"

    def sources(self) -> list[dict[str, Any]]:
        return [dict(source) for source in self._sources]

    def get_source(self, source_id: str) -> dict[str, Any]:
        source = next((item for item in self._sources if item.get("id") == source_id), None)
        if not source:
            raise AndroidMcpError(f"官方来源不存在：{source_id}", code="source_not_found")
        return dict(source)

    def load_index(self) -> dict[str, Any]:
        try:
            value = json.loads(self.index_path.read_text(encoding="utf-8"))
            return value if isinstance(value, dict) else {"version": 1, "records": []}
        except (OSError, ValueError):
            return {"version": 1, "records": []}

    def records(self) -> list[dict[str, Any]]:
        value = self.load_index().get("records", [])
        return [item for item in value if isinstance(item, dict)]

    def sync(
        self,
        *,
        source_ids: list[str] | None = None,
        timeout_seconds: int = 20,
        max_bytes: int = 2_000_000,
    ) -> dict[str, Any]:
        selected_ids = set(source_ids or [str(item["id"]) for item in self._sources])
        unknown = selected_ids - {str(item["id"]) for item in self._sources}
        if unknown:
            raise AndroidMcpError(
                f"官方来源不在白名单：{', '.join(sorted(unknown))}",
                code="source_not_found",
            )

        previous = self.records()
        retained = [item for item in previous if item.get("source_id") not in selected_ids]
        synced: list[str] = []
        skipped: list[str] = []
        errors: list[dict[str, str]] = []
        new_records: list[dict[str, Any]] = []

        for source in self._sources:
            source_id = str(source["id"])
            if source_id not in selected_ids:
                continue
            if source.get("kind") == "source_repository":
                new_records.append(self._repository_record(source))
                skipped.append(source_id)
                continue
            try:
                new_records.extend(self._fetch_document(source, timeout_seconds, max_bytes))
                synced.append(source_id)
            except (HTTPError, URLError, TimeoutError, OSError, UnicodeError, ValueError) as exc:
                errors.append({"source_id": source_id, "error": str(exc)[:300]})
                retained.extend(item for item in previous if item.get("source_id") == source_id)

        payload = {
            "version": 1,
            "backend": "curated_lexical",
            "built_at": now_iso(),
            "records": retained + new_records,
            "errors": errors,
        }
        _atomic_json_write(self.index_path, payload)
        return {
            "status": "completed" if not errors else "partial",
            "index_path": str(self.index_path),
            "synced": synced,
            "metadata_only": skipped,
            "errors": errors,
            "records": len(payload["records"]),
        }

    def read(self, source_id: str, locator: str | None = None, max_chars: int = 120_000) -> dict[str, Any]:
        source = self.get_source(source_id)
        candidates = [item for item in self.records() if item.get("source_id") == source_id]
        if locator:
            exact = [item for item in candidates if item.get("locator") == locator or item.get("id") == locator]
            candidates = exact or candidates
        if not candidates:
            raise AndroidMcpError(
                f"来源尚未同步：{source_id}",
                code="source_not_indexed",
                hint="先调用 android_kb(action=\"sync_sources\")。",
            )
        content = "\n\n".join(str(item.get("content", "")) for item in candidates)
        return {
            "source_id": source_id,
            "authority": source.get("authority"),
            "title": source.get("title"),
            "url": source.get("url"),
            "content": content[:max_chars],
            "truncated": len(content) > max_chars,
            "records": len(candidates),
            "locators": [item.get("locator") for item in candidates],
        }

    def _load_sources(self) -> list[dict[str, Any]]:
        resource = resources.files("android_mcp").joinpath("resources", "official_sources.json")
        try:
            payload = json.loads(resource.read_text(encoding="utf-8"))
        except (FileNotFoundError, OSError, ValueError) as exc:
            raise AndroidMcpError("官方来源目录损坏", code="source_catalog_invalid") from exc
        sources = payload.get("sources") if isinstance(payload, dict) else None
        if not isinstance(sources, list) or not sources:
            raise AndroidMcpError("官方来源目录为空", code="source_catalog_invalid")
        return [item for item in sources if isinstance(item, dict) and item.get("id") and item.get("url")]

    def _fetch_document(self, source: dict[str, Any], timeout_seconds: int, max_bytes: int) -> list[dict[str, Any]]:
        url = str(source["url"])
        if not url.startswith("https://"):
            raise ValueError(f"仅允许 HTTPS 来源：{url}")
        request = Request(
            url,
            headers={
                "Accept": "text/html,application/xhtml+xml,text/plain;q=0.9,*/*;q=0.1",
                "User-Agent": "android-mcp-official-kb/0.3 (+local development tool)",
            },
        )
        with urlopen(request, timeout=max(1, int(timeout_seconds))) as response:
            final_url = str(response.geturl() or url)
            initial_host = urlsplit(url).hostname
            final_parts = urlsplit(final_url)
            if final_parts.scheme != "https" or final_parts.hostname != initial_host:
                raise ValueError(f"来源重定向到非白名单地址：{final_url}")
            raw = response.read(max_bytes + 1)
            if len(raw) > max_bytes:
                raise ValueError(f"文档超过大小限制 {max_bytes} bytes")
            encoding = response.headers.get_content_charset() or "utf-8"
            html = raw.decode(encoding, errors="replace")
            last_modified = response.headers.get("Last-Modified")
        parsed = _parse_html(html)
        source_id = str(source["id"])
        source_hash = hashlib.sha256(raw).hexdigest()
        title = parsed.get("title") or str(source.get("title") or source_id)
        blocks = parsed.get("blocks") or [{"heading": "", "text": parsed.get("text", "")}]
        records: list[dict[str, Any]] = []
        buffer: list[str] = []
        buffer_heading = ""
        chunk_number = 0

        def flush() -> None:
            nonlocal buffer, buffer_heading, chunk_number
            content = "\n\n".join(buffer).strip()
            if not content:
                return
            chunk_number += 1
            locator = buffer_heading.strip()
            if not locator or len(locator) > 120:
                locator = f"chunk-{chunk_number}"
            records.append(
                {
                    "id": f"{source_id}:{source_hash[:12]}:{chunk_number}",
                    "source_id": source_id,
                    "source": "official",
                    "authority": source.get("authority"),
                    "kind": source.get("kind"),
                    "title": title,
                    "url": url,
                    # Headings from CMS pages can contain embedded examples or
                    # generated text.  Keep citations readable and bounded;
                    # the URL plus content hash remains the stable identity.
                    "locator": locator,
                    "content": content,
                    "version": source.get("ref") or source.get("api_level"),
                    "api_level": source.get("api_level"),
                    "os": source.get("os"),
                    "tags": source.get("tags", []),
                    "updated_at": parsed.get("updated_at"),
                    "last_modified": last_modified,
                    "fetched_at": now_iso(),
                    "content_hash": source_hash,
                    "license_ref": source.get("license_ref"),
                }
            )
            buffer = []
            buffer_heading = ""

        for block in blocks:
            text = str(block.get("text", "")).strip()
            heading = str(block.get("heading", "")).strip()
            if not text:
                continue
            if heading and heading != buffer_heading and buffer:
                flush()
            buffer_heading = heading or buffer_heading
            if sum(len(item) for item in buffer) + len(text) > 6_000 and buffer:
                flush()
                buffer_heading = heading
            buffer.append(text)
        flush()
        return records

    @staticmethod
    def _repository_record(source: dict[str, Any]) -> dict[str, Any]:
        source_id = str(source["id"])
        content = f"{source.get('title', source_id)}\n{source['url']}\n" + " ".join(source.get("tags", []))
        content_hash = hashlib.sha256(content.encode("utf-8")).hexdigest()
        return {
            "id": f"{source_id}:{content_hash[:12]}:metadata",
            "source_id": source_id,
            "source": "official",
            "authority": source.get("authority"),
            "kind": source.get("kind"),
            "title": source.get("title", source_id),
            "url": source.get("url"),
            "locator": "repository",
            "content": content,
            "version": source.get("ref"),
            "tags": source.get("tags", []),
            "fetched_at": now_iso(),
            "content_hash": content_hash,
            "license_ref": source.get("license_ref"),
        }
