from __future__ import annotations

import base64
import json
import os
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch
from urllib.parse import urlsplit

from android_mcp.models import AndroidMcpError
from android_mcp.services.edit_guard import EditGuard
from android_mcp.services.file_service import FileService
from android_mcp.services.github_catalog import GitHubSourceCatalog
from android_mcp.services.kb_catalog import OfficialSourceCatalog, _parse_html
from android_mcp.services.kb_service import KnowledgeBaseService
from android_mcp.services.rule_engine import RuleEngine
from android_mcp.services.task_manager import TaskManager


class _FakeResponse:
    def __init__(self, payload: dict[str, object], url: str) -> None:
        self.payload = payload
        self.url = url

    def __enter__(self) -> "_FakeResponse":
        return self

    def __exit__(self, *_: object) -> None:
        return None

    def geturl(self) -> str:
        return self.url

    def read(self, _limit: int = -1) -> bytes:
        return json.dumps(self.payload).encode("utf-8")


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

    def test_github_evidence_supports_algorithms_but_not_platform_contracts(self) -> None:
        with TemporaryDirectory() as runtime, TemporaryDirectory() as directory:
            with patch.dict(
                os.environ,
                {"LOCALAPPDATA": runtime, "GITHUB_TOKEN": "test-token"},
                clear=False,
            ):
                root = Path(directory)
                (root / "Algorithm.kt").write_text("package demo\n", encoding="utf-8")
                content = "fun binarySearch(values: IntArray, target: Int): Int = -1\n"
                search_payload = {
                    "total_count": 1,
                    "items": [
                        {
                            "path": "src/Algorithm.kt",
                            "repository": {
                                "full_name": "octocat/Hello-World",
                                "default_branch": "main",
                                "stargazers_count": 42,
                                "fork": False,
                                "archived": False,
                                "license": {"spdx_id": "MIT"},
                            },
                            "language": "Kotlin",
                        }
                    ],
                }
                content_payload = {
                    "type": "file",
                    "encoding": "base64",
                    "content": base64.b64encode(content.encode("utf-8")).decode("ascii"),
                    "sha": "blob-sha-1",
                }

                def fake_urlopen(request: object, timeout: int = 0) -> _FakeResponse:
                    del timeout
                    url = str(getattr(request, "full_url", ""))
                    path = urlsplit(url).path
                    if path == "/search/code":
                        return _FakeResponse(search_payload, url)
                    if path == "/repos/octocat/Hello-World/contents/src/Algorithm.kt":
                        return _FakeResponse(content_payload, url)
                    raise AssertionError(f"unexpected GitHub endpoint: {url}")

                with patch("android_mcp.services.github_catalog.urlopen", side_effect=fake_urlopen):
                    catalog = GitHubSourceCatalog()
                    direct = catalog.search("algorithm language:Kotlin", top_k=1)
                    self.assertEqual(direct["fetched"], 1)
                    record = direct["records"][0]
                    self.assertEqual(record["source"], "github")
                    self.assertEqual(record["source_tier"], "non_official")
                    self.assertEqual(record["repository"], "octocat/Hello-World")
                    self.assertEqual(record["ref"], "main")
                    self.assertEqual(record["commit"], "blob-sha-1")
                    self.assertEqual(record["license_ref"], "MIT")
                    self.assertNotIn("test-token", catalog.index_path.read_text(encoding="utf-8"))
                    self.assertIn("binarySearch", catalog.read(record["id"])["content"])

                    tasks = TaskManager(max_workers=1)
                    try:
                        kb = KnowledgeBaseService(tasks)
                        result = kb.handle(
                            action="github_search",
                            project_root=str(root),
                            query="binary search algorithm",
                            require_citation=True,
                        )
                        data = result["data"]
                        self.assertFalse(data["authoritative"])
                        self.assertTrue(data["has_github_source"])
                        self.assertTrue(data["citation_available"])
                        evidence_id = data["evidence_id"]
                        self.assertTrue(kb.verify(root, evidence_id)["verified"])

                        engine = RuleEngine(kb)
                        allowed = engine.validate_write(
                            project_root=str(root),
                            file_path="Algorithm.kt",
                            action="replace",
                            evidence_ids=[evidence_id],
                            change_type="algorithm",
                            change_reason="compare an open-source algorithm implementation",
                        )
                        self.assertTrue(allowed["allowed"])
                        self.assertTrue(allowed["has_github_source"])
                        with self.assertRaises(AndroidMcpError) as official_required:
                            engine.validate_write(
                                project_root=str(root),
                                file_path="Algorithm.kt",
                                action="replace",
                                evidence_ids=[evidence_id],
                                change_type="api",
                                change_reason="change Android API behavior",
                            )
                        self.assertEqual(official_required.exception.code, "official_evidence_required")
                    finally:
                        tasks._executor.shutdown(wait=True)


if __name__ == "__main__":
    unittest.main()
