from __future__ import annotations

import argparse
import json
import sys
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from recruit_crawler.cli_context import ContextExtractionRuntime, load_config_with_context
from recruit_crawler.codex_thread_context import CodexThreadContextExtractor, CodexThreadId
from recruit_crawler.model_context import ContextExtractionError, ModelContextExtraction
from recruit_crawler.model_context_cache import SqliteContextExtractionCache

CONFIG = ROOT / "config" / "sample_config.json"


class RecordingRunner:
    def __init__(self) -> None:
        self.created_prompts: list[str] = []
        self.archived: list[CodexThreadId] = []

    def create_thread(self, prompt: str, *, model_id: str, effort: str) -> CodexThreadId:
        self.created_prompts.append(prompt)
        return CodexThreadId("runtime-thread")

    def read_thread(self, thread_id: CodexThreadId) -> str:
        return json.dumps(
            {
                "desired_roles": ["ML Engineer"],
                "skills": ["Python"],
                "preferred_locations": ["Seoul"],
                "max_experience_years": 2,
                "explicit_deal_breakers": [],
                "confidence": 0.9,
            }
        )

    def archive_thread(self, thread_id: CodexThreadId) -> None:
        self.archived.append(thread_id)


class CodexThreadRuntimeWiringTests(unittest.TestCase):
    def test_cli_context_hook_reuses_persistent_structured_cache(self) -> None:
        raw_marker = "private source sentence must never enter cache"

        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            context_path = tmp_path / "resume.md"
            context_path.write_text(
                "Roles: ML Engineer\nSkills: Python\nLocations: Seoul\nExperience: 2 years\n"
                f"Private note: {raw_marker}.\n",
                encoding="utf-8",
            )
            cache_path = tmp_path / "personal_info" / "model-context-cache.sqlite3"
            args = argparse.Namespace(config=CONFIG, context_doc=[context_path])

            first_runner = RecordingRunner()
            first = load_config_with_context(
                args,
                allow_real_sources=False,
                interview=False,
                model_context=ContextExtractionRuntime(
                    extractor=CodexThreadContextExtractor(runner=first_runner),
                    cache=SqliteContextExtractionCache(cache_path),
                ),
            )
            second_runner = RecordingRunner()
            second = load_config_with_context(
                args,
                allow_real_sources=False,
                interview=False,
                model_context=ContextExtractionRuntime(
                    extractor=CodexThreadContextExtractor(runner=second_runner),
                    cache=SqliteContextExtractionCache(cache_path),
                ),
            )
            persisted = cache_path.read_bytes()

        self.assertEqual(first.user_context, second.user_context)
        self.assertEqual(first.user_context.desired_roles, ["ML Engineer"])
        self.assertEqual(len(first_runner.created_prompts), 1)
        self.assertEqual(first_runner.archived, [CodexThreadId("runtime-thread")])
        self.assertEqual(second_runner.created_prompts, [])
        self.assertNotIn(raw_marker.encode(), persisted)
        self.assertNotIn(b"Private note", persisted)

    def test_structured_cache_serializes_concurrent_writers_and_repairs_permissions(self) -> None:
        extraction = ModelContextExtraction(
            desired_roles=["ML Engineer"],
            skills=["Python"],
            preferred_locations=["Seoul"],
            max_experience_years=2,
        )

        with tempfile.TemporaryDirectory() as tmp:
            cache_path = Path(tmp) / "model-context-cache.sqlite3"
            cache = SqliteContextExtractionCache(cache_path)
            with ThreadPoolExecutor(max_workers=8) as pool:
                list(pool.map(lambda index: cache.set(f"fingerprint-{index}", extraction), range(20)))
            cache_path.chmod(0o644)
            values = [cache.get(f"fingerprint-{index}") for index in range(20)]
            mode = cache_path.stat().st_mode & 0o777

        self.assertEqual(values, [extraction] * 20)
        self.assertEqual(mode, 0o600)

    def test_cache_permission_failure_uses_privacy_safe_extraction_error(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cache_path = Path(tmp) / "model-context-cache.sqlite3"
            cache_path.write_bytes(b"")
            cache = SqliteContextExtractionCache(cache_path)

            with patch.object(Path, "chmod", side_effect=PermissionError("private path")):
                with self.assertRaisesRegex(ContextExtractionError, "could not be secured") as caught:
                    cache.get("fingerprint")

        self.assertIsNone(caught.exception.__cause__)
        self.assertNotIn("private path", str(caught.exception))

    def test_cache_rejects_symlink_path(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            target = tmp_path / "target.sqlite3"
            target.write_bytes(b"")
            cache_path = tmp_path / "model-context-cache.sqlite3"
            cache_path.symlink_to(target)
            cache = SqliteContextExtractionCache(cache_path)

            with self.assertRaisesRegex(ContextExtractionError, "path is unsafe"):
                cache.get("fingerprint")
