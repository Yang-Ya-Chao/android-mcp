"""Safe Kotlin/Gradle/XML file operations.

This module is the Android equivalent of Daofy's file tool.  It intentionally
keeps the edit protocol line-oriented and explicit so an AI client can preview,
validate and roll back each change.
"""

from __future__ import annotations

import difflib
import codecs
import hashlib
import os
import re
import shutil
import tempfile
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

from ..models import AndroidMcpError, ok
from ..paths import PathPolicy
from ..utils.kotlin_normalize import normalize_code, normalize_text
from .edit_guard import EditGuard, fingerprint


class RWLock:
    """A small multi-reader/single-writer lock for one file."""

    def __init__(self) -> None:
        self._condition = threading.Condition(threading.Lock())
        self._readers = 0
        self._writer = False

    @contextmanager
    def read(self) -> Iterator[None]:
        with self._condition:
            while self._writer:
                self._condition.wait()
            self._readers += 1
        try:
            yield
        finally:
            with self._condition:
                self._readers -= 1
                if self._readers == 0:
                    self._condition.notify_all()

    @contextmanager
    def write(self) -> Iterator[None]:
        with self._condition:
            while self._writer or self._readers:
                self._condition.wait()
            self._writer = True
        try:
            yield
        finally:
            with self._condition:
                self._writer = False
                self._condition.notify_all()


class FileService:
    def __init__(self, guard: EditGuard | None = None) -> None:
        self.policy = PathPolicy()
        self.guard = guard or EditGuard()
        self._locks: dict[str, RWLock] = {}
        self._locks_guard = threading.Lock()
        self._dirty: set[str] = set()

    def _lock_for(self, path: Path) -> RWLock:
        key = str(path)
        with self._locks_guard:
            return self._locks.setdefault(key, RWLock())

    def read(
        self,
        *,
        project_root: str | None,
        file_path: str,
        start_line: int | None = None,
        end_line: int | None = None,
        show_line_numbers: bool = True,
    ) -> dict[str, Any]:
        path = self.policy.file(project_root, file_path)
        with self._lock_for(path).read():
            self.guard.check(path)
            text, encoding = _read_text(path)
            self.guard.observe(path)
        lines = text.splitlines(keepends=True)
        first = max(1, start_line or 1)
        last = min(len(lines), end_line or len(lines))
        if first > last and lines:
            raise AndroidMcpError("start_line 不能大于 end_line。", code="invalid_line_range")
        selected = lines[first - 1 : last] if lines else []
        output = "".join(
            f"{number:>6}: {line}" if show_line_numbers else line
            for number, line in enumerate(selected, first)
        )
        self._dirty.discard(str(path))
        return ok(
            {
                "path": str(path),
                "relative_path": str(path.relative_to(self.policy.root(project_root))),
                "encoding": encoding,
                "line_count": len(lines),
                "start_line": first if lines else 0,
                "end_line": last if lines else 0,
                "content": output,
                "sha256": _sha256_text(text),
                "dirty": False,
            },
            hint="如需修改，请保留目标行的原文作为 old_content。",
        )

    def grep(
        self,
        *,
        project_root: str | None,
        pattern: str,
        file_path: str | None = None,
        include: str | None = None,
        context: int = 0,
        count: int = 50,
    ) -> dict[str, Any]:
        if not pattern:
            raise AndroidMcpError("grep 的 pattern 不能为空。", code="invalid_pattern")
        root = self.policy.root(project_root)
        base = self.policy.file(project_root, file_path, allow_artifact=False, allow_directory=True) if file_path else root
        if file_path and base.is_file():
            candidates = [base]
        else:
            candidates = [p for p in base.rglob("*") if p.is_file()]
        regex = re.compile(pattern, re.IGNORECASE)
        results: list[dict[str, Any]] = []
        for path in candidates:
            if len(results) >= count:
                break
            if not self._is_searchable(path, root, include):
                continue
            try:
                text, _ = _read_text(path)
            except (OSError, UnicodeError):
                continue
            lines = text.splitlines()
            for index, line in enumerate(lines):
                if not regex.search(line):
                    continue
                low = max(0, index - max(0, context))
                high = min(len(lines), index + max(0, context) + 1)
                results.append(
                    {
                        "path": str(path.relative_to(root)),
                        "line": index + 1,
                        "match": line,
                        "context": lines[low:high],
                    }
                )
                if len(results) >= count:
                    break
        return ok({"pattern": pattern, "count": len(results), "matches": results})

    def edit(
        self,
        *,
        action: str,
        project_root: str | None,
        file_path: str,
        edits: list[dict[str, Any]] | None = None,
        dry_run: bool = True,
        backup: bool = True,
        allow_dirty: bool = False,
        auto_format: bool = False,
        backup_action: str | None = None,
        version: int | None = None,
        imports: list[str] | None = None,
        import_name: str | None = None,
        uses_action: str | None = None,
        from_encoding: str | None = None,
        to_encoding: str | None = None,
        manifest_operation: str | None = None,
        manifest_target: str = "application",
        attribute_name: str | None = None,
        attribute_value: str | None = None,
        dependency: str | None = None,
        configuration: str = "implementation",
        dependencies_action: str = "add",
    ) -> dict[str, Any]:
        if action == "backup":
            return self.backup(
                project_root=project_root,
                file_path=file_path,
                backup_action=backup_action or "list",
                version=version,
            )
        path = self.policy.file(project_root, file_path, allow_missing=action == "write")
        lock = self._lock_for(path)
        with lock.write():
            if path.exists():
                self.guard.check(path, for_write=True)
                original, encoding = _read_text(path)
            else:
                original, encoding = "", "utf-8"
            requested_action = action
            if action == "encode":
                return self._encode(
                    path,
                    dry_run=dry_run,
                    backup=backup,
                    allow_dirty=allow_dirty,
                    from_encoding=from_encoding,
                    to_encoding=to_encoding,
                )
            dependency_warnings: list[str] = []
            if action == "format":
                target = _format_text(original)
                edits = [{"start_line": 1, "end_line": max(1, len(original.splitlines())), "old_content": original, "content": target}]
            elif action == "imports":
                target = _manage_imports(original, imports or ([import_name] if import_name else []), uses_action or "add")
                edits = [{"start_line": 1, "end_line": max(1, len(original.splitlines())), "old_content": original, "content": target}]
                action = "write"
            elif action == "manifest":
                target = _edit_manifest(original, manifest_operation or "add_permission", manifest_target, attribute_name, attribute_value)
                edits = [{"start_line": 1, "end_line": max(1, len(original.splitlines())), "old_content": original, "content": target}]
                action = "write"
            elif action == "dependencies":
                target, dependency_warnings = _edit_dependencies(original, dependency, configuration, dependencies_action, path.suffix.lower())
                edits = [{"start_line": 1, "end_line": max(1, len(original.splitlines())), "old_content": original, "content": target}]
                action = "write"
            elif action in {"write", "replace", "insert", "delete"}:
                edits = edits or []
                if not edits:
                    raise AndroidMcpError("修改操作需要 edits。", code="missing_edits")
            else:
                raise AndroidMcpError(f"android_file 不支持 action：{action}", code="unsupported_action")
            if path.exists() and not _edits_have_old_content(edits or []):
                raise AndroidMcpError(
                    "修改现有文件时每个 edit 都必须提供 old_content。",
                    code="missing_old_content",
                    hint="先 read 目标行，使用原文作为 old_content；仅新增文件可省略。",
                )
            if path.exists() and str(path) in self._dirty and not allow_dirty and not _edits_have_old_content(edits or []):
                raise AndroidMcpError(
                    f"文件仍处于 dirty 状态：{path}",
                    code="dirty_file",
                    hint="先重新 read，或为每个 edit 提供 old_content；确认外部改动后才使用 allow_dirty=true。",
                )
            effective_edits = [dict(edit) for edit in (edits or [])]
            if action == "insert":
                for edit in effective_edits:
                    edit.setdefault("position", "after")
            updated, report = _apply_edits(original, effective_edits, path.suffix.lower())
            diff = _unified_diff(original, updated, path.name)
            response = {
                "path": str(path),
                "action": requested_action,
                "dry_run": dry_run,
                "changed": original != updated,
                "diff": diff,
                "offset_report": report,
                "encoding": encoding,
                "before_sha256": _sha256_text(original),
                "after_sha256": _sha256_text(updated),
            }
            if dependency_warnings:
                response["dependency_warnings"] = dependency_warnings
            if dry_run:
                return ok(response, hint="这是预览结果；设置 dry_run=false 才会落盘。")
            if not path.exists() and action != "write":
                raise AndroidMcpError("只有 write 可以创建新文件。", code="file_not_found")
            backup_path = _create_backup(path) if backup and path.exists() else None
            try:
                _atomic_write(path, updated, encoding)
            except OSError as exc:
                raise AndroidMcpError(f"原子写入失败：{path} ({exc})", code="write_failed") from exc
            self._dirty.add(str(path))
            self.guard.authorize(path)
            response["backup_path"] = str(backup_path) if backup_path else None
            response["dirty"] = True
            if auto_format:
                response["format_note"] = "已写入；如需项目级 ktlint/spotless 格式化，请调用 android_build 或配置 formatter 任务。"
            return ok(response, hint="写入完成后请重新调用 android_file(action=\"read\") 校验内容。")

    def _encode(
        self,
        path: Path,
        *,
        dry_run: bool,
        backup: bool,
        allow_dirty: bool,
        from_encoding: str | None,
        to_encoding: str | None,
    ) -> dict[str, Any]:
        if str(path) in self._dirty and not allow_dirty:
            raise AndroidMcpError(
                f"文件仍处于 dirty 状态：{path}",
                code="dirty_file",
                hint="先重新 read，再执行 encode；或显式使用 allow_dirty=true。",
            )
        raw = path.read_bytes()
        _, detected_encoding = _read_text(path)
        source_encoding = from_encoding or detected_encoding
        target_encoding = to_encoding or detected_encoding
        try:
            codecs.lookup(source_encoding)
            codecs.lookup(target_encoding)
            text = raw.decode(source_encoding)
            encoded = text.encode(target_encoding)
        except (LookupError, UnicodeError) as exc:
            raise AndroidMcpError(f"编码转换失败：{source_encoding} -> {target_encoding}", code="encoding_error") from exc
        response = {
            "path": str(path),
            "action": "encode",
            "dry_run": dry_run,
            "changed": raw != encoded,
            "from_encoding": source_encoding,
            "to_encoding": target_encoding,
            "before_sha256": hashlib.sha256(raw).hexdigest(),
            "after_sha256": hashlib.sha256(encoded).hexdigest(),
        }
        if dry_run:
            return ok(response, hint="这是编码转换预览；设置 dry_run=false 才会落盘。")
        backup_path = _create_backup(path) if backup else None
        try:
            _atomic_write_bytes(path, encoded)
        except OSError as exc:
            raise AndroidMcpError(f"编码转换写入失败：{path} ({exc})", code="write_failed") from exc
        self._dirty.add(str(path))
        self.guard.authorize(path)
        response["backup_path"] = str(backup_path) if backup_path else None
        response["dirty"] = True
        return ok(response, hint="编码转换完成后请重新调用 android_file(action=\"read\")。")

    def backup(
        self,
        *,
        project_root: str | None,
        file_path: str,
        backup_action: str,
        version: int | None = None,
    ) -> dict[str, Any]:
        path = self.policy.file(project_root, file_path)
        history = path.parent / "__history"
        if backup_action == "create":
            created = _create_backup(path)
            return ok({"path": str(path), "backup_path": str(created)})
        backups = sorted(history.glob(f"{path.name}.*.bak"), reverse=True) if history.is_dir() else []
        if backup_action == "list":
            return ok({"path": str(path), "backups": [str(item) for item in backups]})
        if backup_action == "restore":
            if not backups:
                raise AndroidMcpError("没有可恢复的备份。", code="backup_not_found")
            selected = backups[version - 1] if version and 0 < version <= len(backups) else backups[0]
            _, encoding = _read_text(path)
            backup_text, _ = _read_text(selected)
            _atomic_write(path, backup_text, encoding)
            self._dirty.add(str(path))
            self.guard.authorize(path)
            return ok({"path": str(path), "restored_from": str(selected), "dirty": True})
        raise AndroidMcpError(f"未知 backup_action：{backup_action}", code="unsupported_backup_action")

    def _is_searchable(self, path: Path, root: Path, include: str | None) -> bool:
        try:
            relative = path.relative_to(root)
        except ValueError:
            return False
        parts = {part.lower() for part in relative.parts}
        if parts & {".git", ".gradle", ".idea", "build", "captures", "__history"}:
            return False
        if include and not path.match(include):
            return False
        return path.suffix.lower() in {".kt", ".kts", ".java", ".xml", ".gradle", ".properties", ".md"}


def _read_text(path: Path) -> tuple[str, str]:
    data = path.read_bytes()
    candidates: list[tuple[str, bytes]] = []
    if data.startswith(b"\xef\xbb\xbf"):
        candidates.append(("utf-8-sig", data))
    elif data.startswith((b"\xff\xfe", b"\xfe\xff")):
        candidates.append(("utf-16", data))
    candidates.extend([("utf-8", data), ("gb18030", data), ("cp1252", data)])
    for encoding, raw in candidates:
        try:
            return raw.decode(encoding), encoding
        except UnicodeDecodeError:
            continue
    raise AndroidMcpError(f"无法识别文件编码：{path}", code="encoding_error")


def _atomic_write(path: Path, text: str, encoding: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding=encoding, newline="") as stream:
            stream.write(text)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    except Exception:
        try:
            os.unlink(temporary)
        except OSError:
            pass
        raise


def _atomic_write_bytes(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    except Exception:
        try:
            os.unlink(temporary)
        except OSError:
            pass
        raise


def _create_backup(path: Path) -> Path:
    history = path.parent / "__history"
    history.mkdir(parents=True, exist_ok=True)
    timestamp = time.strftime("%Y%m%dT%H%M%S", time.localtime())
    digest = fingerprint(path)[:10]
    target = history / f"{path.name}.{timestamp}.{digest}.bak"
    counter = 1
    while target.exists():
        target = history / f"{path.name}.{timestamp}.{digest}.{counter}.bak"
        counter += 1
    shutil.copy2(path, target)
    return target


def _sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _normalize_for_path(value: str, suffix: str) -> str:
    return normalize_code(value) if suffix in {".kt", ".kts", ".java", ".gradle"} else normalize_text(value)


def _edits_have_old_content(edits: list[dict[str, Any]]) -> bool:
    return bool(edits) and all("old_content" in edit and edit.get("old_content") is not None for edit in edits)


def _apply_edits(original: str, edits: list[dict[str, Any]], suffix: str) -> tuple[str, list[dict[str, Any]]]:
    newline = "\r\n" if "\r\n" in original else "\n"
    original_lines = original.splitlines(keepends=True)
    if not original_lines and original:
        original_lines = [original]
    working = list(original_lines)
    reports: list[dict[str, Any]] = []
    ordered = sorted(enumerate(edits), key=lambda pair: (int(pair[1].get("start_line", 0)), pair[0]), reverse=True)
    for edit_index, edit in ordered:
        try:
            start = int(edit["start_line"])
        except (KeyError, TypeError, ValueError) as exc:
            raise AndroidMcpError("每个 edit 都必须包含有效 start_line。", code="invalid_edit") from exc
        end = int(edit.get("end_line", start))
        if start < 1 or end < 0 or end < start - 1 or start > len(working) + 1:
            raise AndroidMcpError(f"edit 行号范围无效：{start}-{end}", code="invalid_line_range")
        if "old_content" in edit and edit.get("old_content") is not None:
            if start <= len(working):
                actual = "".join(working[start - 1 : min(end, len(working))])
            else:
                actual = ""
            expected = str(edit["old_content"])
            if _normalize_for_path(actual, suffix) != _normalize_for_path(expected, suffix):
                raise AndroidMcpError(
                    f"old_content 校验失败（行 {start}-{end}）。",
                    code="stale_edit",
                    hint=f"expected={expected[:160]!r}; actual={actual[:160]!r}。请重新 read 后重算 edits。",
                )
        if "content" in edit:
            replacement = str(edit.get("content") or "")
        elif "new_text" in edit:
            replacement = str(edit.get("new_text") or "")
        else:
            replacement = ""
        replacement_lines = _new_lines(replacement, newline, original.endswith(("\n", "\r\n")), end < len(working))
        position = edit.get("position", "replace")
        if position in {"before", "after"}:
            anchor = max(0, min(len(working), start - 1 + (1 if position == "after" else 0)))
            working[anchor:anchor] = replacement_lines
            actual_end = anchor + len(replacement_lines)
        elif end == start - 1:
            working[start - 1 : start - 1] = replacement_lines
            actual_end = start - 1 + len(replacement_lines)
        else:
            working[start - 1 : min(end, len(working))] = replacement_lines
            actual_end = start - 1 + len(replacement_lines)
        reports.append({"edit_index": edit_index, "original_start": start, "original_end": end, "result_end": actual_end})
    reports.reverse()
    return "".join(working), reports


def _new_lines(value: str, newline: str, original_ends_with_newline: bool, not_at_eof: bool) -> list[str]:
    if not value:
        return []
    normalized = value.replace("\r\n", "\n").replace("\r", "\n")
    if newline != "\n":
        normalized = normalized.replace("\n", newline)
    lines = normalized.splitlines(keepends=True)
    if lines and not lines[-1].endswith(("\n", "\r")) and (original_ends_with_newline or not_at_eof):
        lines[-1] += newline
    return lines


def _unified_diff(before: str, after: str, name: str) -> str:
    return "".join(
        difflib.unified_diff(
            before.splitlines(keepends=True),
            after.splitlines(keepends=True),
            fromfile=f"{name} (before)",
            tofile=f"{name} (after)",
        )
    )


def _format_text(value: str) -> str:
    newline = "\r\n" if "\r\n" in value else "\n"
    lines = [line.rstrip() for line in value.replace("\r\n", "\n").replace("\r", "\n").split("\n")]
    while lines and lines[-1] == "":
        lines.pop()
    return newline.join(lines) + (newline if lines else "")


def _manage_imports(original: str, requested: list[str], action: str) -> str:
    if action not in {"add", "remove"}:
        raise AndroidMcpError(f"imports 的 uses_action 必须是 add/remove：{action}", code="invalid_import_action")
    lines = original.splitlines(keepends=True)
    import_indexes = [index for index, line in enumerate(lines) if re.match(r"^\s*import\s+\S+", line)]
    existing = {re.match(r"^\s*import\s+(.+?)\s*$", lines[index].rstrip("\r\n")).group(1) for index in import_indexes}
    normalized_requested = {item.strip().removeprefix("import ").strip() for item in requested if item and item.strip()}
    if action == "remove":
        remaining = {
            item
            for item in existing
            if item not in normalized_requested and item.split(" as ", 1)[0].strip() not in normalized_requested
        }
    else:
        remaining = existing | normalized_requested
    indent = ""
    if import_indexes:
        indent = re.match(r"^\s*", lines[import_indexes[0]]).group(0)
        first = min(import_indexes)
        last = max(import_indexes)
        block = [f"{indent}import {item}\n" for item in sorted(remaining)]
        if lines[last].endswith("\r\n"):
            block = [line.replace("\n", "\r\n") for line in block]
        lines[first : last + 1] = block
        return "".join(lines)
    if action == "remove" or not normalized_requested:
        return original
    insert_at = 0
    for index, line in enumerate(lines):
        if line.lstrip().startswith("package "):
            insert_at = index + 1
            break
    newline = "\r\n" if "\r\n" in original else "\n"
    block = [f"import {item}{newline}" for item in sorted(normalized_requested)]
    lines[insert_at:insert_at] = ([newline] if insert_at and lines[insert_at - 1].strip() else []) + block
    return "".join(lines)


def _edit_manifest(original: str, operation: str, target: str, attribute_name: str | None, attribute_value: str | None) -> str:
    """Apply small line-preserving Manifest changes without reserializing XML."""

    if not original.lstrip().startswith("<"):
        raise AndroidMcpError("Manifest 不是有效 XML 文本。", code="invalid_manifest")
    newline = "\r\n" if "\r\n" in original else "\n"
    lines = original.splitlines(keepends=True)
    name = (attribute_value or attribute_name or target or "").strip()
    if operation in {"add_permission", "permission_add"}:
        if not name:
            raise AndroidMcpError("add_permission 需要 attribute_value 作为权限名。", code="missing_manifest_value")
        if any(re.search(rf"android:name\s*=\s*[\"']{re.escape(name)}[\"']", line) and "uses-permission" in line for line in lines):
            return original
        insert_at = next((index for index, line in enumerate(lines) if re.search(r"<application\b", line)), len(lines) - 1)
        indent = re.match(r"\s*", lines[insert_at]).group(0) if lines else "    "
        lines.insert(insert_at, f'{indent}<uses-permission android:name="{name}" />{newline}')
        return "".join(lines)
    if operation in {"remove_permission", "permission_remove"}:
        if not name:
            raise AndroidMcpError("remove_permission 需要 attribute_value 作为权限名。", code="missing_manifest_value")
        return "".join(line for line in lines if not ("uses-permission" in line and re.search(rf"android:name\s*=\s*[\"']{re.escape(name)}[\"']", line)))
    if operation in {"add_activity", "activity_add"}:
        if not name:
            raise AndroidMcpError("add_activity 需要 attribute_value 作为 Activity 名称。", code="missing_manifest_value")
        if any("<activity" in line and re.search(rf"android:name\s*=\s*[\"']{re.escape(name)}[\"']", line) for line in lines):
            return original
        close_index = next((index for index, line in enumerate(lines) if re.search(r"</application>", line)), None)
        if close_index is None:
            raise AndroidMcpError("Manifest 中未找到 application 结束标签。", code="invalid_manifest")
        app_indent = next((re.match(r"\s*", line).group(0) for line in lines if re.search(r"<application\b", line)), "    ")
        lines.insert(close_index, f'{app_indent}    <activity android:name="{name}" />{newline}')
        return "".join(lines)
    if operation in {"remove_activity", "activity_remove"}:
        if not name:
            raise AndroidMcpError("remove_activity 需要 attribute_value 作为 Activity 名称。", code="missing_manifest_value")
        pattern = re.compile(rf"^\s*<activity\b[^>]*android:name\s*=\s*[\"']{re.escape(name)}[\"'][^>]*/>\s*(?:\r?\n)?$")
        return "".join(line for line in lines if not pattern.match(line))
    if operation in {"set_attribute", "attribute_set"}:
        if not attribute_name or attribute_value is None:
            raise AndroidMcpError("set_attribute 需要 attribute_name 和 attribute_value。", code="missing_manifest_attribute")
        tag = target if target in {"manifest", "application", "activity"} else "application"
        tag_pattern = re.compile(rf"^(\s*<\s*{re.escape(tag)}\b)([^>]*)(/?>.*)$")
        attr_pattern = re.compile(rf"\s+{re.escape(attribute_name)}\s*=\s*[\"'][^\"']*[\"']")
        for index, line in enumerate(lines):
            match = tag_pattern.match(line)
            if not match:
                continue
            attrs = match.group(2)
            if attr_pattern.search(attrs):
                attrs = attr_pattern.sub(f' {attribute_name}="{attribute_value}"', attrs)
            else:
                attrs += f' {attribute_name}="{attribute_value}"'
            lines[index] = f"{match.group(1)}{attrs}{match.group(3)}"
            return "".join(lines)
        raise AndroidMcpError(f"Manifest 中未找到 {tag} 标签。", code="manifest_target_not_found")
    raise AndroidMcpError(f"不支持的 manifest_operation：{operation}", code="unsupported_manifest_operation")


def _edit_dependencies(original: str, dependency: str | None, configuration: str, action: str, suffix: str) -> tuple[str, list[str]]:
    if not dependency:
        raise AndroidMcpError("dependencies 需要 dependency 坐标。", code="missing_dependency")
    if not re.fullmatch(r"[A-Za-z][A-Za-z0-9_.-]*", configuration):
        raise AndroidMcpError(f"不安全的依赖 configuration：{configuration}", code="invalid_dependency_configuration")
    if action not in {"add", "remove"}:
        raise AndroidMcpError(f"dependencies_action 必须是 add/remove：{action}", code="invalid_dependency_action")
    newline = "\r\n" if "\r\n" in original else "\n"
    coordinate_pattern = re.escape(dependency)
    declaration = re.compile(rf"\b{re.escape(configuration)}\s*\(?\s*[\"']{coordinate_pattern}[\"']\s*\)?")
    if action == "remove":
        lines = [line for line in original.splitlines(keepends=True) if not declaration.search(line)]
        return "".join(lines), []
    if declaration.search(original):
        return original, _dependency_warnings(original)
    match = re.search(r"(?m)^(\s*dependencies\s*\{)([\s\S]*?)(^\s*\})", original)
    if not match:
        raise AndroidMcpError("Gradle 文件中未找到 dependencies { ... } 块。", code="dependencies_block_not_found")
    body = match.group(2)
    indent = "    "
    body_lines = body.splitlines(keepends=True)
    existing_indent = next((re.match(r"\s*", line).group(0) for line in body_lines if line.strip()), None)
    if existing_indent:
        indent = existing_indent
    line = f'{indent}{configuration}("{dependency}"){newline}'
    new_body = body + ("" if not body or body.endswith(("\n", "\r")) else newline) + line
    updated = original[: match.start(2)] + new_body + original[match.end(2) :]
    return updated, _dependency_warnings(updated)


def _dependency_warnings(content: str) -> list[str]:
    coordinates = re.findall(r"\b(?:implementation|api|compileOnly|runtimeOnly|testImplementation|androidTestImplementation)\s*\(?\s*[\"']([^\"']+)[\"']", content)
    versions: dict[str, set[str]] = {}
    for coordinate in coordinates:
        parts = coordinate.split(":")
        if len(parts) >= 3:
            versions.setdefault(":".join(parts[:2]), set()).add(parts[2])
    return [f"依赖版本不一致：{group} -> {', '.join(sorted(values))}" for group, values in versions.items() if len(values) > 1]
