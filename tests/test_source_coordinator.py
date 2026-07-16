from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
import time
from unittest.mock import patch
import signal
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
import unittest

from recruit_crawler.pipeline import (
    SourceCollectionCoordinator,
    SourceCollectionStatus,
)
from recruit_crawler.schemas import PostingCandidate, SourceManifest
from recruit_crawler.sources.http import PublicJobsHttpAdapter, SourceBudgetExceeded
import recruit_crawler.pipeline as pipeline_module


def _manifest(source_id: str, *, options: dict | None = None) -> SourceManifest:
    return SourceManifest(
        source_id=source_id,
        enabled=True,
        access_mode="fixture",
        auth_required=False,
        tos_review_status="pass",
        domains=["example.com"],
        rate_limit="local",
        failure_mode="skip_source",
        allowed_persisted_fields=[],
        options=options or {},
    )


def _candidate(source_id: str, title: str) -> PostingCandidate:
    return PostingCandidate(
        source_id=source_id,
        source_url=f"https://example.com/{source_id}",
        source_posting_id=source_id,
        title=title,
        company="Example",
        location="Remote",
        deadline_raw=None,
        collected_at=datetime.now(timezone.utc),
        raw_jd={"responsibilities": ["build"]},
    )


class _FastAdapter:
    def __init__(self, source, _fixture_path):
        self.source = source
        self.errors = []
        self.issues = []

    def collect(self):
        return [_candidate(self.source.source_id, self.source.source_id)]


class _SlowAdapter(_FastAdapter):
    def collect(self):
        time.sleep(0.5)
        return super().collect()


def _fast_factory(source, fixture_path):
    return _FastAdapter(source, fixture_path)


def _slow_factory(source, fixture_path):
    return _SlowAdapter(source, fixture_path)


class SourceCoordinatorTests(unittest.TestCase):
    def test_candidates_merge_in_manifest_order_not_completion_order(self):
        config = SimpleNamespace(
            fixture_path=Path("."),
            sources=[_manifest("first"), _manifest("second")],
        )
        batch = SourceCollectionCoordinator(
            config,
            total_budget_seconds=2,
            per_source_budget_seconds=1,
            adapter_factory=_fast_factory,
        ).collect()

        self.assertEqual([row.source_id for row in batch.outcomes], ["first", "second"])
        self.assertEqual([row.source_id for row in batch.candidates], ["first", "second"])
        self.assertTrue(all(row.status == SourceCollectionStatus.SUCCESS for row in batch.outcomes))

    def test_source_cap_produces_typed_timeout(self):
        config = SimpleNamespace(fixture_path=Path("."), sources=[_manifest("slow")])
        batch = SourceCollectionCoordinator(
            config,
            total_budget_seconds=2,
            per_source_budget_seconds=0.05,
            adapter_factory=_slow_factory,
        ).collect()

        self.assertEqual(batch.outcomes[0].status, SourceCollectionStatus.SOURCE_TIMEOUT)
        self.assertFalse(batch.outcomes[0].completed)
    def test_handshake_setup_failure_is_collection_error(self):
        config = SimpleNamespace(fixture_path=Path("."), sources=[_manifest("setup")])
        with patch("recruit_crawler.pipeline.os.setpgid", side_effect=PermissionError):
            batch = SourceCollectionCoordinator(
                config,
                total_budget_seconds=1,
                per_source_budget_seconds=0.5,
                adapter_factory=_fast_factory,
            ).collect()

        self.assertEqual(batch.outcomes[0].status, SourceCollectionStatus.COLLECTION_ERROR)
        self.assertFalse(batch.outcomes[0].completed)

    def test_cleanup_targets_verified_pgid_with_term_then_kill(self):
        class _FakeProcess:
            pid = 41

            def __init__(self):
                self.alive = True
                self.join_timeouts = []

            def is_alive(self):
                return self.alive

            def join(self, timeout=None):
                self.join_timeouts.append(timeout)
                self.alive = False

            def kill(self):
                self.alive = False

        process = _FakeProcess()
        state = pipeline_module._WorkerState(
            _manifest("group"),
            process,
            None,
            0.0,
            10.0,
            pid=41,
            pgid=900,
        )
        signals = []
        group_alive = True

        def _killpg(pgid, sig):
            nonlocal group_alive
            if sig == 0:
                if not group_alive:
                    raise ProcessLookupError
                return
            signals.append((pgid, sig))
            if sig == signal.SIGKILL:
                group_alive = False

        coordinator = SourceCollectionCoordinator(
            SimpleNamespace(fixture_path=Path("."), sources=[]),
            monotonic=iter((0.0, 3.0, 3.0)).__next__,
            hard_deadline=1.0,
        )
        with patch("recruit_crawler.pipeline.os.killpg", side_effect=_killpg):
            coordinator._stop_processes([state])

        self.assertEqual(
            signals,
            [(900, signal.SIGTERM), (900, signal.SIGKILL)],
        )
        self.assertNotIn((41, signal.SIGTERM), signals)
        self.assertNotIn((41, signal.SIGKILL), signals)

    def test_ipc_validation_rejects_wrong_source_and_candidate_type(self):
        source = _manifest("source")
        with self.assertRaises(ValueError):
            SourceCollectionCoordinator._validate_message(
                source,
                {
                    "schema_version": 1,
                    "source_id": "other",
                    "status": SourceCollectionStatus.SUCCESS,
                    "candidates": (),
                    "issues": (),
                    "errors": (),
                },
            )
        with self.assertRaises(ValueError):
            SourceCollectionCoordinator._validate_message(
                source,
                {
                    "schema_version": 1,
                    "source_id": "source",
                    "status": SourceCollectionStatus.SUCCESS,
                    "candidates": ("not-a-candidate",),
                    "issues": (),
                    "errors": (),
                },
            )

    def test_http_deadline_is_checked_before_request(self):
        adapter = PublicJobsHttpAdapter(_manifest("http", options={"require_robots": False}))
        adapter.set_collection_deadline(time.monotonic() - 1)

        with self.assertRaises(SourceBudgetExceeded):
            adapter._fetch("https://example.com/jobs")
