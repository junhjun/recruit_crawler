from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from recruit_crawler.codex_thread_context import (
    CodexThreadContextExtractor,
    CodexThreadId,
    parse_model_context_json,
)
from recruit_crawler.model_context import ContextExtractionError


class UnexpectedRunnerError(RuntimeError):
    pass


class FailingRunner:
    def create_thread(self, prompt: str, *, model_id: str, effort: str) -> CodexThreadId:
        return CodexThreadId("thread-private")

    def read_thread(self, thread_id: CodexThreadId) -> str:
        raise UnexpectedRunnerError("RAW-READ-PRIVATE")

    def archive_thread(self, thread_id: CodexThreadId) -> None:
        raise UnexpectedRunnerError("RAW-ARCHIVE-PRIVATE")


class CodexThreadSecurityTests(unittest.TestCase):
    def test_combined_read_and_archive_failure_is_sanitized(self) -> None:
        extractor = CodexThreadContextExtractor(runner=FailingRunner())

        with self.assertRaisesRegex(
            ContextExtractionError,
            "extraction and archive failed",
        ) as caught:
            extractor.extract("private source", fingerprint="abc123")

        self.assertIsNone(caught.exception.__cause__)
        self.assertNotIn("PRIVATE", repr(caught.exception))

    def test_duplicate_json_field_is_rejected(self) -> None:
        response = (
            '{"desired_roles":[],"skills":["first"],"skills":["second"],'
            '"preferred_locations":[],"max_experience_years":0,'
            '"explicit_deal_breakers":[],"confidence":0.5}'
        )

        with self.assertRaisesRegex(ContextExtractionError, "duplicate JSON fields"):
            parse_model_context_json(response, source_text="Skills: first")

    def test_out_of_range_experience_is_rejected_before_cache_boundary(self) -> None:
        response = json.dumps(
            {
                "desired_roles": [],
                "skills": [],
                "preferred_locations": [],
                "max_experience_years": 999999,
                "explicit_deal_breakers": [],
                "confidence": 0.5,
            }
        )

        with self.assertRaisesRegex(ContextExtractionError, "supported range"):
            parse_model_context_json(response, source_text="Experience: 999999 years")

    def test_short_pii_and_split_source_passages_are_rejected(self) -> None:
        pii_response = json.dumps(
            {
                "desired_roles": [],
                "skills": ["private@example.com"],
                "preferred_locations": [],
                "max_experience_years": 0,
                "explicit_deal_breakers": [],
                "confidence": 0.5,
            }
        )
        split_response = json.dumps(
            {
                "desired_roles": [],
                "skills": ["confidential research", "prototype access phrase"],
                "preferred_locations": [],
                "max_experience_years": 0,
                "explicit_deal_breakers": [],
                "confidence": 0.5,
            }
        )

        with self.assertRaisesRegex(ContextExtractionError, "sensitive source data"):
            parse_model_context_json(pii_response, source_text="Email: private@example.com")
        with self.assertRaisesRegex(ContextExtractionError, "split verbatim source passage"):
            parse_model_context_json(
                split_response,
                source_text="confidential research, prototype access phrase",
            )

    def test_three_field_source_passage_is_rejected(self) -> None:
        response = json.dumps(
            {
                "desired_roles": ["alpha bravo"],
                "skills": ["charlie delta"],
                "preferred_locations": ["echo foxtrot"],
                "max_experience_years": 0,
                "explicit_deal_breakers": [],
                "confidence": 0.5,
            }
        )

        with self.assertRaisesRegex(ContextExtractionError, "cross-field source passage"):
            parse_model_context_json(
                response,
                source_text="alpha bravo, charlie delta; echo foxtrot",
            )

    def test_overlapping_short_match_cannot_hide_source_passage(self) -> None:
        response = json.dumps(
            {
                "desired_roles": ["alpha bravo"],
                "skills": ["bravo"],
                "preferred_locations": ["charlie delta echo golf"],
                "max_experience_years": 0,
                "explicit_deal_breakers": [],
                "confidence": 0.5,
            }
        )

        with self.assertRaisesRegex(ContextExtractionError, "cross-field source passage"):
            parse_model_context_json(
                response,
                source_text="alpha bravo charlie delta echo golf",
            )
