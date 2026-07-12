from __future__ import annotations

import json
import sys
import unittest
from dataclasses import FrozenInstanceError, asdict, is_dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from recruit_crawler.codex_thread_context import (
    CodexThreadContextExtractor,
    CodexThreadError,
    CodexThreadId,
    CodexThreadOperationEvent,
)
from recruit_crawler.model_context import ContextExtractionError


def _response() -> str:
    return json.dumps(
        {
            "desired_roles": ["ML Engineer"],
            "skills": ["Python"],
            "preferred_locations": ["Seoul"],
            "max_experience_years": 2,
            "explicit_deal_breakers": [],
            "confidence": 0.8,
        }
    )


class ScriptedRunner:
    def __init__(
        self,
        *,
        read_results: list[str | CodexThreadError | TimeoutError] | None = None,
        create_fails: bool = False,
        archive_failures: int = 0,
    ) -> None:
        self.read_results = list(read_results or [_response()])
        self.create_fails = create_fails
        self.archive_failures = archive_failures
        self.created_prompts: list[str] = []
        self.read_thread_ids: list[CodexThreadId] = []
        self.archived_thread_ids: list[CodexThreadId] = []
        self.timeouts: dict[str, list[float]] = {"create": [], "read": [], "archive": []}

    def create_thread(
        self,
        prompt: str,
        *,
        model_id: str,
        effort: str,
        timeout_seconds: float,
    ) -> CodexThreadId:
        self.created_prompts.append(prompt)
        self.timeouts["create"].append(timeout_seconds)
        if self.create_fails:
            raise CodexThreadError(operation="create")
        return CodexThreadId("thread-private")

    def read_thread(self, thread_id: CodexThreadId, *, timeout_seconds: float) -> str:
        self.read_thread_ids.append(thread_id)
        self.timeouts["read"].append(timeout_seconds)
        result = self.read_results.pop(0)
        if isinstance(result, (CodexThreadError, TimeoutError)):
            raise result
        return result

    def archive_thread(self, thread_id: CodexThreadId, *, timeout_seconds: float) -> None:
        self.archived_thread_ids.append(thread_id)
        self.timeouts["archive"].append(timeout_seconds)
        if self.archive_failures > 0:
            self.archive_failures -= 1
            raise CodexThreadError(operation="archive")


class CodexThreadContextG002Tests(unittest.TestCase):
    def test_extractor_rejects_non_positive_retry_or_timeout_configuration(self) -> None:
        runner = ScriptedRunner()

        with self.assertRaisesRegex(ValueError, "max_attempts"):
            CodexThreadContextExtractor(runner=runner, max_attempts=0)
        with self.assertRaisesRegex(ValueError, "operation_timeout_seconds"):
            CodexThreadContextExtractor(runner=runner, operation_timeout_seconds=0)

    def test_extractor_retries_one_transient_read_on_the_same_thread_then_archives(self) -> None:
        runner = ScriptedRunner(read_results=[CodexThreadError(operation="read"), _response()])
        extractor = CodexThreadContextExtractor(runner=runner, max_attempts=2)

        extraction = extractor.extract("aggregate private context", fingerprint="abc123")

        self.assertEqual(extraction.desired_roles, ["ML Engineer"])
        self.assertEqual(len(runner.created_prompts), 1)
        self.assertEqual(
            runner.read_thread_ids,
            [CodexThreadId("thread-private"), CodexThreadId("thread-private")],
        )
        self.assertEqual(runner.archived_thread_ids, [CodexThreadId("thread-private")])

    def test_extractor_does_not_retry_thread_creation_after_a_transient_error(self) -> None:
        runner = ScriptedRunner(create_fails=True)
        extractor = CodexThreadContextExtractor(runner=runner, max_attempts=2)

        with self.assertRaisesRegex(ContextExtractionError, "creation failed"):
            extractor.extract("aggregate private context", fingerprint="abc123")

        self.assertEqual(len(runner.created_prompts), 1)
        self.assertEqual(runner.read_thread_ids, [])
        self.assertEqual(runner.archived_thread_ids, [])

    def test_extractor_forwards_one_operation_timeout_to_create_read_and_archive(self) -> None:
        runner = ScriptedRunner()
        extractor = CodexThreadContextExtractor(runner=runner, operation_timeout_seconds=7.5)

        extraction = extractor.extract("aggregate private context", fingerprint="abc123")

        self.assertEqual(extraction.skills, ["Python"])
        self.assertEqual(runner.timeouts, {"create": [7.5], "read": [7.5], "archive": [7.5]})

    def test_extractor_retries_one_transient_archive_on_the_same_thread(self) -> None:
        runner = ScriptedRunner(archive_failures=1)
        extractor = CodexThreadContextExtractor(runner=runner, max_attempts=2)

        extraction = extractor.extract("aggregate private context", fingerprint="abc123")

        self.assertEqual(extraction.preferred_locations, ["Seoul"])
        self.assertEqual(runner.read_thread_ids, [CodexThreadId("thread-private")])
        self.assertEqual(
            runner.archived_thread_ids,
            [CodexThreadId("thread-private"), CodexThreadId("thread-private")],
        )

    def test_extractor_emits_only_frozen_allowlisted_events_without_private_values(self) -> None:
        class PrivateTransientReadError(CodexThreadError):
            def __str__(self) -> str:
                return "PRIVATE_RUNNER_DETAIL_DO_NOT_LOG"

        private_prompt = "PRIVATE_PROMPT_CANARY_DO_NOT_LOG"
        runner = ScriptedRunner(
            read_results=[PrivateTransientReadError(operation="read"), _response()],
        )
        events: list[CodexThreadOperationEvent] = []
        extractor = CodexThreadContextExtractor(runner=runner, event_sink=events.append, max_attempts=2)

        extraction = extractor.extract(private_prompt, fingerprint="abc123")

        self.assertEqual(extraction.desired_roles, ["ML Engineer"])
        self.assertEqual(len(events), 4)
        for event in events:
            payload = asdict(event)
            self.assertTrue(is_dataclass(event))
            self.assertEqual(
                set(payload),
                {"operation", "attempt", "outcome", "error_class", "duration_ms"},
            )
            self.assertIn(payload["operation"], {"create", "read", "archive"})
            self.assertIn(payload["attempt"], {1, 2})
            self.assertIn(payload["outcome"], {"success", "failure"})
            self.assertIn(payload["error_class"], {None, "transient"})
            self.assertGreaterEqual(payload["duration_ms"], 0)
            with self.assertRaises(FrozenInstanceError):
                event.__setattr__("operation", "read")
        serialized_events = json.dumps([asdict(event) for event in events])
        self.assertNotIn(private_prompt, serialized_events)
        self.assertNotIn("PRIVATE_RUNNER_DETAIL_DO_NOT_LOG", serialized_events)
        self.assertNotIn("thread-private", serialized_events)

    def test_extractor_retries_timeout_without_exposing_private_values_in_events(self) -> None:
        private_prompt = "PRIVATE_PROMPT_CANARY_DO_NOT_LOG"
        private_timeout_detail = "PRIVATE_TIMEOUT_CANARY_DO_NOT_LOG"
        runner = ScriptedRunner(read_results=[TimeoutError(private_timeout_detail), _response()])
        events: list[CodexThreadOperationEvent] = []
        extractor = CodexThreadContextExtractor(
            runner=runner,
            max_attempts=2,
            event_sink=events.append,
        )

        extraction = extractor.extract(private_prompt, fingerprint="abc123")

        self.assertEqual(extraction.desired_roles, ["ML Engineer"])
        self.assertEqual(len(runner.created_prompts), 1)
        self.assertEqual(
            runner.read_thread_ids,
            [CodexThreadId("thread-private"), CodexThreadId("thread-private")],
        )
        self.assertEqual(runner.archived_thread_ids, [CodexThreadId("thread-private")])
        serialized_events = json.dumps([asdict(event) for event in events])
        self.assertNotIn(private_prompt, serialized_events)
        self.assertNotIn(private_timeout_detail, serialized_events)
