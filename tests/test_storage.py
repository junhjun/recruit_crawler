from __future__ import annotations

import sys
import tempfile
import unittest
from datetime import date, datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from recruit_crawler.schemas import FitAssessment, JDSnapshot, RunSummary, SourceRunMetric
from recruit_crawler.storage import (
    add_feedback_event,
    export_feedback_events,
    export_recommendations,
    export_runs,
    persist_scheduled_run,
    StorageGate,
)


def _gate(*, status: str = "pass", report_generated: bool = True) -> StorageGate:
    return {
        "schema_version": 1,
        "command_mode": "scheduled-run",
        "run_date": "2026-07-01",
        "status": status,
        "context_status": "complete" if status == "pass" else "needs_context",
        "missing_context": [] if status == "pass" else ["skills"],
        "db_path": {"provided": True, "name": "recruit.sqlite3", "path_hash": "a" * 64},
        "report_generated": report_generated,
        "sources_attempted": ["fixture"] if report_generated else [],
        "candidates_collected": 1 if report_generated else 0,
        "sources": [
            {
                "source_id": "fixture",
                "attempted": True,
                "candidate_count": 1,
                "error_count": 0,
                "errors": [],
            }
        ]
        if report_generated
        else [],
        "source_policy": [
            {
                "source_id": "fixture",
                "enabled": True,
                "scheduled_action": "run",
                "access_mode": "fixture",
                "target_status": "enabled",
                "target_lane": None,
                "automation_level": "no_human",
                "auth_required": False,
                "prohibited_options": [],
            }
        ],
        "run_identity": {
            "command_mode": "scheduled-run",
            "run_date": "2026-07-01",
            "source_config_hash": "source-hash",
            "profile_config_hash": "profile-hash",
            "run_id": "stable-run-id",
        },
        "findings": []
        if status == "pass"
        else [
            {
                "severity": "fail",
                "source_id": None,
                "message": "scheduled-run missing required user context: skills",
            }
        ],
    }


def _summary(report_path: Path) -> RunSummary:
    return RunSummary(
        run_date=date(2026, 7, 1),
        sources_attempted=["fixture"],
        source_errors=[],
        candidates_collected=1,
        duplicates_removed=0,
        experience_excluded=0,
        expired_excluded=0,
        ranked_count=1,
        report_path=report_path,
        source_metrics=[SourceRunMetric(source_id="fixture", attempted=True, candidate_count=1)],
    )


def _assessment(*, source_url: str = "https://example.test/jobs/1") -> FitAssessment:
    snapshot = JDSnapshot(
        source_id="fixture",
        source_url=source_url,
        source_posting_id="posting-1",
        title="Backend Engineer",
        company="Example",
        location="Seoul",
        deadline_raw=None,
        deadline=None,
        deadline_uncertain=False,
        required_qualifications=["Python"],
        preferred_qualifications=["SQLite"],
        responsibilities=["Build pipelines"],
        company_info=["B2B"],
    )
    return FitAssessment(
        snapshot=snapshot,
        score=88,
        recommendation="apply",
        matched_evidence=["Python"],
        gaps=[],
        risks=[],
        verification_questions=[],
        positioning_seed="Python pipeline work",
        verdict="apply",
    )


class StoragePersistenceTests(unittest.TestCase):
    def test_persist_scheduled_run_overwrites_children_and_removes_orphan_feedback(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            db_path = tmp_path / "recruit.sqlite3"

            persist_scheduled_run(
                db_path,
                gate=_gate(),
                summary=_summary(tmp_path / "report.md"),
                ranked=[_assessment()],
            )
            recommendation_id = export_recommendations(db_path)[0]["recommendation_id"]
            add_feedback_event(
                db_path,
                recommendation_id=recommendation_id,
                verdict="interesting",
                reason="Useful role",
                movement="up",
                created_at="2026-07-02T00:00:00+00:00",
            )

            persist_scheduled_run(
                db_path,
                gate=_gate(status="fail", report_generated=False),
                summary=None,
                ranked=[],
            )
            runs = export_runs(db_path)
            recommendations = export_recommendations(db_path)
            feedback = export_feedback_events(db_path)

        self.assertEqual(len(runs), 1)
        self.assertEqual(runs[0]["status"], "fail")
        self.assertEqual(runs[0]["report_generated"], 0)
        self.assertEqual(runs[0]["ranked_count"], 0)
        self.assertEqual(recommendations, [])
        self.assertEqual(feedback, [])

    def test_persist_scheduled_run_replaces_recommendations_for_same_run_identity(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            db_path = tmp_path / "recruit.sqlite3"

            persist_scheduled_run(
                db_path,
                gate=_gate(),
                summary=_summary(tmp_path / "report.md"),
                ranked=[_assessment(source_url="https://example.test/jobs/1")],
            )
            first_recommendation_id = export_recommendations(db_path)[0]["recommendation_id"]

            persist_scheduled_run(
                db_path,
                gate=_gate(),
                summary=_summary(tmp_path / "report-updated.md"),
                ranked=[_assessment(source_url="https://example.test/jobs/2")],
            )
            runs = export_runs(db_path)
            recommendations = export_recommendations(db_path)

        self.assertEqual(len(runs), 1)
        self.assertEqual(runs[0]["report_path"], str(tmp_path / "report-updated.md"))
        self.assertEqual(len(recommendations), 1)
        self.assertNotEqual(recommendations[0]["recommendation_id"], first_recommendation_id)

    def test_feedback_event_export_is_idempotent_for_same_created_at_payload(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            db_path = tmp_path / "recruit.sqlite3"

            persist_scheduled_run(
                db_path,
                gate=_gate(),
                summary=_summary(tmp_path / "report.md"),
                ranked=[_assessment()],
            )
            recommendation_id = export_recommendations(db_path)[0]["recommendation_id"]
            created_at = datetime(2026, 7, 2, tzinfo=timezone.utc).isoformat()

            first_event_id = add_feedback_event(
                db_path,
                recommendation_id=recommendation_id,
                verdict="interesting",
                reason="Useful role",
                movement="up",
                created_at=created_at,
            )
            second_event_id = add_feedback_event(
                db_path,
                recommendation_id=recommendation_id,
                verdict="interesting",
                reason="Useful role",
                movement="up",
                created_at=created_at,
            )
            feedback = export_feedback_events(db_path)

        self.assertEqual(first_event_id, second_event_id)
        self.assertEqual(len(feedback), 1)
        self.assertEqual(feedback[0]["event_id"], first_event_id)


if __name__ == "__main__":
    unittest.main()
