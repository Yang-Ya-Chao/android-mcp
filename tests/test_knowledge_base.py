from __future__ import annotations

import json
import os
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from android_mcp.models import AndroidMcpError
from android_mcp.services.edit_guard import EditGuard
from android_mcp.services.file_service import FileService
from android_mcp.services.kb_catalog import OfficialSourceCatalog, _parse_html
from android_mcp.services.kb_service import KnowledgeBaseService
from android_mcp.services.rule_engine import RuleEngine
from android_mcp.services.task_manager import TaskManager


class KnowledgeBaseTests(unittest.TestCase):
    def test_catalog_is_curated_and_html_keeps_source_locator(self) -> None:
        catalog = OfficialSourceCatalog()
        source_ids = {item["id"] for item in catalog.sources()}
        self.assertIn("google.android.api", source_ids)
        self.assertIn("xiaomi.hyperos.android15", source_ids)
        parsed = _parse_html(
            "<html><title>Android API</title><h2>Permissions</h2>"
            "<p>Request permission at runtime.</p></html>"
        )
        self.assertEqual(parsed["title"], "Android API")
        self.assertEqual(parsed["blocks"][0]["heading"], "Permissions")
        self.assertIn("runtime", parsed["text"])

    def test_project_search_creates_verifiable_evidence(self) -> None:
        with TemporaryDirectory() as runtime, TemporaryDirectory() as directory:
            with patch.dict(os.environ, {"LOCALAPPDATA": runtime}, clear=False):
                root = Path(directory)
                (root / "Main.kt").write_text(
                    "package demo\n\nclass MainActivity\n",
                    encoding="utf-8",
                )
                tasks = TaskManager(max_workers=1)
                try:
                    kb = KnowledgeBaseService(tasks)
                    build = kb._build(root, lambda **_: None, lambda: False, False)
                    self.assertEqual(build["status"], "completed")
                    result = kb.handle(
                        action="search",
                        project_root=str(root),
                        query="MainActivity",
                        scope="project",
                    )
                    data = result["data"]
                    self.assertTrue(data["evidence_id"])
                    self.assertEqual(data["results"][0]["source"], "project")
                    verification = kb.verify(root, data["evidence_id"])
                    self.assertTrue(verification["verified"])
                    (root / "Main.kt").write_text(
                        "package demo\n\nclass ChangedActivity\n",
                        encoding="utf-8",
                    )
                    self.assertFalse(kb.verify(root, data["evidence_id"])["verified"])
                finally:
                    tasks._executor.shutdown(wait=True)

    def test_official_evidence_gates_sensitive_write(self) -> None:
        with TemporaryDirectory() as runtime, TemporaryDirectory() as directory:
            with patch.dict(os.environ, {"LOCALAPPDATA": runtime}, clear=False):
                root = Path(directory)
                source = root / "Main.kt"
                source.write_text("package demo\n\nfun main() = Unit\n", encoding="utf-8")
                tasks = TaskManager(max_workers=1)
                try:
                    kb = KnowledgeBaseService(tasks)
                    official_id = "google.android.api:test:1"
                    content = "Android 13 notification permission must be requested at runtime."
                    kb.catalog.index_path.parent.mkdir(parents=True, exist_ok=True)
                    kb.catalog.index_path.write_text(
                        json.dumps(
                            {
                                "version": 1,
                                "records": [
                                    {
                                        "id": official_id,
                                        "source_id": "google.android.api",
                                        "source": "official",
                                        "authority": "google",
                                        "kind": "api_reference",
                                        "title": "Android API reference",
                                        "url": "https://developer.android.com/reference/",
                                        "locator": "notification-permission",
                                        "content": content,
                                        "api_level": 33,
                                        "content_hash": "test-hash",
                                    }
                                ],
                            }
                        ),
                        encoding="utf-8",
                    )
                    result = kb.handle(
                        action="search",
                        project_root=str(root),
                        query="notification permission",
                        scope="official",
                        api_level=33,
                        require_citation=True,
                    )
                    data = result["data"]
                    self.assertTrue(data["authoritative"])
                    evidence_id = data["evidence_id"]
                    self.assertTrue(kb.verify(root, evidence_id)["verified"])

                    engine = RuleEngine(kb)
                    with self.assertRaises(AndroidMcpError) as missing:
                        engine.validate_write(
                            project_root=str(root),
                            file_path="Main.kt",
                            action="replace",
                            evidence_ids=None,
                            change_type="api",
                            change_reason="add runtime permission",
                        )
                    self.assertEqual(missing.exception.code, "knowledge_required")
                    with self.assertRaises(AndroidMcpError) as format_bypass:
                        engine.validate_write(
                            project_root=str(root),
                            file_path="Main.kt",
                            action="replace",
                            evidence_ids=None,
                            change_type="format",
                            change_reason="pretend formatting",
                        )
                    self.assertEqual(format_bypass.exception.code, "invalid_change_type")

                    file_service = FileService(EditGuard(), engine)
                    preview = file_service.edit(
                        project_root=str(root),
                        file_path="Main.kt",
                        action="replace",
                        edits=[
                            {
                                "start_line": 3,
                                "end_line": 3,
                                "old_content": "fun main() = Unit",
                                "content": "fun main() = requestNotificationPermission()",
                            }
                        ],
                        evidence_ids=[evidence_id],
                        change_type="api",
                        dry_run=True,
                    )
                    self.assertTrue(preview["success"])
                    self.assertTrue(preview["data"]["rule_check"]["has_official_source"])
                finally:
                    tasks._executor.shutdown(wait=True)


if __name__ == "__main__":
    unittest.main()
