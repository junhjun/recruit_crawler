from __future__ import annotations

from dataclasses import replace
import hashlib
import json
import sqlite3
import threading
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from recruit_crawler.schemas import (
    GateSourceV2,
    PersistenceEnvelopeV4,
    PersistenceEnvelopeV3,
    SourceExecutionOutcomeV1,
    ReportArtifactV2,
    RenderedReportV2,
)
from recruit_crawler._storage_core import probe_run_transaction_state
from recruit_crawler.storage import (
    EnvelopeValidationError,
    StorageSchemaError,
    add_feedback_event,
    export_feedback_events,
    export_recommendations,
    export_source_outcomes,
    export_runs,
    finalize_scheduled_run,
    persist_scheduled_run as _stage_scheduled_run,
    scheduled_run_persistence_state,
)


def persist_scheduled_run(db_path: Path, envelope, **kwargs):
    token = _stage_scheduled_run(db_path, envelope, **kwargs)
    if token is not None:
        assert finalize_scheduled_run(db_path, dict(envelope.run_identity)["run_id"], token)
    return token


RUN_ID = "run-v3-20260701"


def _envelope(*, run_id: str = RUN_ID, score: int = 88) -> PersistenceEnvelopeV4:
    report = b"# report\n"
    source = GateSourceV2(
        source_id="fixture",
        attempted=True,
        candidate_count=1,
        source_rejected_count=0,
        duplicate_count=0,
        normalized_changed_field_count=0,
        normalized_emptied_field_count=0,
        detail_quality={"manual_only": 0, "rejected": 0, "verified": 1},
        error_count=0,
        error_codes=(),
        duration_ms=0,
    )
    summary = {
        "collected": 1,
        "source_rejected": 0,
        "source_accepted": 1,
        "duplicates_removed": 0,
        "deduplicated": 1,
        "expired": 0,
        "exclude": 0,
        "manual_review_total": 0,
        "apply_total": 1,
        "hold_total": 0,
        "low_priority_total": 0,
        "actionable_total": 1,
        "displayed_apply": 1,
        "displayed_hold": 0,
        "suppressed_apply": 0,
        "suppressed_hold": 0,
        "displayed_manual": 0,
        "suppressed_manual": 0,
    }
    return PersistenceEnvelopeV4(
        schema_version=4,
        run_identity={
            "command_mode": "scheduled-run",
            "run_date": "2026-07-01",
            "source_config_hash": "source-hash",
            "profile_config_hash": "profile-hash",
            "run_id": run_id,
        },
        report_artifact=ReportArtifactV2(
            schema_version=2,
            generated=True,
            path="report.md",
            rendered=RenderedReportV2(
                schema_version=2,
                markdown_bytes=report,
                content_sha256=hashlib.sha256(report).hexdigest(),
                byte_length=len(report),
            ),
        ),
        gate_status="pass",
        context_status="complete",
        gate_json_sha256="a" * 64,
        summary=summary,
        source_metrics=(source,),
        assessments=(
            {
                "recommendation_id": "recommendation-v3-1",
                "posting_key": "posting-v3-1",
                "source_id": "fixture",
                "source_url": "https://jobs.example.test/posting-1",
                "source_posting_id": "posting-1",
                "title": "Backend Engineer",
                "company": "Example",
                "location": "Seoul",
                "deadline": "2026-12-31",
                "score": score,
                "final_disposition": "apply",
                "reason_codes": [],
                "source_detail_quality": "verified",
                "matched_evidence": ["필수 요건 일치"],
            },
        ),
        source_outcomes=(
            SourceExecutionOutcomeV1(
                source_id="fixture",
                attempted=True,
                completed=True,
                status="success",
                error_code=None,
                elapsed_ms=0,
            ),
        ),
    )


def _legacy_database(
    path: Path,
    *,
    marker: str | None = None,
    current_feedback: bool = False,
    source_id: str = "fixture",
    source_url: str = "https://jobs.example.test/posting-1",
    source_posting_id: str | None = "posting-1",
) -> None:
    with sqlite3.connect(path) as connection:
        connection.executescript(
            """
            CREATE TABLE schema_metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL);
            CREATE TABLE runs (
                run_id TEXT PRIMARY KEY, command_mode TEXT NOT NULL, run_date TEXT NOT NULL,
                source_config_hash TEXT NOT NULL, profile_config_hash TEXT NOT NULL,
                status TEXT NOT NULL, context_status TEXT NOT NULL, report_generated INTEGER NOT NULL,
                report_path TEXT, candidates_collected INTEGER NOT NULL, ranked_count INTEGER NOT NULL,
                created_at TEXT NOT NULL, updated_at TEXT NOT NULL
            );
            CREATE TABLE source_attempts (
                run_id TEXT NOT NULL, source_id TEXT NOT NULL, attempted INTEGER NOT NULL,
                candidate_count INTEGER NOT NULL, error_count INTEGER NOT NULL, errors_json TEXT NOT NULL,
                PRIMARY KEY (run_id, source_id)
            );
            CREATE TABLE recommendations (
                recommendation_id TEXT PRIMARY KEY, run_id TEXT NOT NULL, source_id TEXT NOT NULL,
                source_url TEXT, source_posting_id TEXT, title TEXT NOT NULL, company TEXT NOT NULL,
                location TEXT NOT NULL, deadline TEXT, score INTEGER NOT NULL, recommendation TEXT NOT NULL,
                verdict TEXT NOT NULL, matched_evidence_json TEXT NOT NULL, gaps_json TEXT NOT NULL,
                risks_json TEXT NOT NULL
            );
            CREATE TABLE quality_gates (
                run_id TEXT PRIMARY KEY, status TEXT NOT NULL, context_status TEXT NOT NULL,
                gate_json TEXT NOT NULL, updated_at TEXT NOT NULL
            );
        """
        )
        if current_feedback:
            connection.execute(
                """CREATE TABLE feedback_events (
                    event_id TEXT PRIMARY KEY, recommendation_id TEXT NOT NULL, run_id TEXT NOT NULL,
                    verdict TEXT NOT NULL, reason TEXT NOT NULL, movement TEXT NOT NULL, created_at TEXT NOT NULL,
                    posting_key TEXT, source_id TEXT, source_posting_id TEXT, source_url TEXT
                )"""
            )
        else:
            connection.execute(
                """CREATE TABLE feedback_events (
                    event_id TEXT PRIMARY KEY, recommendation_id TEXT NOT NULL, run_id TEXT NOT NULL,
                    verdict TEXT NOT NULL, reason TEXT NOT NULL, movement TEXT NOT NULL, created_at TEXT NOT NULL
                )"""
            )
        if marker is not None:
            connection.execute("INSERT INTO schema_metadata(key, value) VALUES ('schema_version', ?)", (marker,))
        connection.execute(
            """INSERT INTO runs VALUES
            ('legacy-run', 'scheduled-run', '2026-07-01', 'source-hash', 'profile-hash',
             'pass', 'complete', 1, 'report.md', 1, 1, 'created', 'updated')"""
        )
        connection.execute("INSERT INTO source_attempts VALUES ('legacy-run', ?, 1, 1, 0, '[]')", (source_id,))
        connection.execute(
            """INSERT INTO recommendations VALUES
            ('legacy-rec', 'legacy-run', ?, ?, ?,
             'Backend Engineer', 'Example', 'Seoul', '2026-12-31', 88, 'apply', 'include',
             '[\"필수 요건 일치\"]', '[]', '[]')""",
            (source_id, source_url, source_posting_id),
        )
        connection.execute("INSERT INTO quality_gates VALUES ('legacy-run', 'pass', 'complete', '{}', 'updated')")
        if current_feedback:
            connection.execute(
                """INSERT INTO feedback_events VALUES
                ('legacy-event', 'legacy-rec', 'legacy-run', 'interesting', 'continuity',
                 'up', '2026-07-02', 'old-key', 'old-source', 'old-posting', 'https://example.test/old')"""
            )
        else:
            connection.execute(
                """INSERT INTO feedback_events VALUES
                ('legacy-event', 'legacy-rec', 'legacy-run', 'interesting', 'continuity', 'up', '2026-07-02')"""
            )
        connection.commit()
def _demote_v4_to_v3(path: Path) -> None:
    with sqlite3.connect(path) as connection:
        connection.execute("DROP TABLE source_outcomes")
        connection.execute("UPDATE schema_metadata SET value='3' WHERE key='schema_version'")
        connection.execute("UPDATE schema_metadata SET value='storage-v3' WHERE key='schema_signature'")
        connection.execute("UPDATE schema_metadata SET value='3' WHERE key='persistence_envelope_schema_version'")
        connection.commit()



class StoragePersistenceTests(unittest.TestCase):
    def test_fresh_database_accepts_only_v4_envelope_and_persists_public_projection(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "recruit.sqlite3"
            persist_scheduled_run(db_path, _envelope())
            with sqlite3.connect(db_path) as connection:
                metadata = dict(connection.execute("SELECT key, value FROM schema_metadata"))
                versions = connection.execute(
                    "SELECT record_schema_version, pipeline_schema_version, score_schema_version, disposition_schema_version FROM runs"
                ).fetchone()
                recommendation_columns = {
                    row[1] for row in connection.execute("PRAGMA table_info(recommendations)")
                }

            runs = export_runs(db_path)
            recommendations = export_recommendations(db_path)

        self.assertEqual(metadata, {
            "schema_version": "4",
            "schema_signature": "storage-v4",
            "persistence_envelope_schema_version": "4",
        })
        self.assertEqual(tuple(versions), (3, 2, 2, 2))
        self.assertIn("final_disposition", recommendation_columns)
        self.assertEqual(runs[0]["run_id"], RUN_ID)
        self.assertEqual(runs[0]["report_path"], "report.md")
        self.assertEqual(recommendations[0]["final_disposition"], "apply")
        self.assertEqual(recommendations[0]["source_url"], "https://jobs.example.test/posting-1")
        self.assertNotIn("opaque_identity", recommendations[0])
        with self.assertRaises(EnvelopeValidationError):
            persist_scheduled_run(db_path, {"schema_version": 1})
    def test_new_v3_envelope_write_is_rejected(self) -> None:
        envelope = _envelope()
        legacy = PersistenceEnvelopeV3(
            schema_version=3,
            run_identity=dict(envelope.run_identity),
            report_artifact=envelope.report_artifact,
            gate_status=envelope.gate_status,
            context_status=envelope.context_status,
            gate_json_sha256=envelope.gate_json_sha256,
            summary=dict(envelope.summary),
            source_metrics=envelope.source_metrics,
            assessments=envelope.assessments,
        )
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(EnvelopeValidationError):
                persist_scheduled_run(Path(tmp) / "v3-write.sqlite3", legacy)
    def test_v4_pass_requires_allowlisted_consistent_success_outcomes(self) -> None:
        envelope = _envelope()
        invalid_outcomes = (
            replace(
                envelope.source_outcomes[0],
                status="collection_error",
                completed=False,
                error_code="collection_error",
            ),
            replace(envelope.source_outcomes[0], error_code="PRIVATE_PROFILE_CANARY"),
            replace(envelope.source_outcomes[0], source_id="unknown-source"),
        )
        invalid_envelopes = tuple(
            replace(envelope, source_outcomes=(outcome,))
            for outcome in invalid_outcomes
        ) + (
            replace(
                envelope,
                source_metrics=(replace(envelope.source_metrics[0], source_id="unknown-source"),),
            ),
        )
        for index, invalid in enumerate(invalid_envelopes):
            with self.subTest(index=index), tempfile.TemporaryDirectory() as tmp:
                with self.assertRaises(EnvelopeValidationError):
                    persist_scheduled_run(Path(tmp) / f"invalid-outcome-{index}.sqlite3", invalid)


    def test_v3_survivor_detail_metrics_allow_duplicates(self) -> None:
        envelope = _envelope()
        source = replace(
            envelope.source_metrics[0],
            candidate_count=2,
            duplicate_count=1,
            detail_quality={"manual_only": 0, "rejected": 0, "verified": 1},
        )
        summary = dict(envelope.summary)
        summary.update(
            collected=2,
            source_accepted=2,
            duplicates_removed=1,
            deduplicated=1,
        )
        survivor_envelope = replace(envelope, source_metrics=(source,), summary=summary)
        with tempfile.TemporaryDirectory() as tmp:
            persist_scheduled_run(Path(tmp) / "survivors.sqlite3", survivor_envelope)

    def test_v3_rejects_unsafe_survivor_detail_combinations(self) -> None:
        envelope = _envelope()
        duplicate_overflow = replace(
            envelope.source_metrics[0],
            candidate_count=1,
            duplicate_count=2,
            detail_quality={"manual_only": 0, "rejected": 0, "verified": 0},
        )
        detail_overflow = replace(
            envelope.source_metrics[0],
            candidate_count=2,
            duplicate_count=1,
            detail_quality={"manual_only": 0, "rejected": 0, "verified": 2},
        )
        invalid_cases = (
            replace(envelope, source_metrics=(duplicate_overflow,)),
            replace(
                envelope,
                source_metrics=(detail_overflow,),
                summary={
                    **dict(envelope.summary),
                    "collected": 2,
                    "source_accepted": 2,
                    "duplicates_removed": 1,
                },
            ),
        )
        for invalid in invalid_cases:
            with self.subTest(source=invalid.source_metrics[0]):
                with tempfile.TemporaryDirectory() as tmp:
                    with self.assertRaises(EnvelopeValidationError):
                        persist_scheduled_run(Path(tmp) / "invalid-survivors.sqlite3", invalid)
    def test_v3_assessment_uses_only_public_projection_names(self) -> None:
        envelope = _envelope()
        assessment = dict(envelope.assessments[0])
        assessment["disposition"] = assessment.pop("final_disposition")
        assessment["detail_quality"] = assessment.pop("source_detail_quality")
        legacy_shape = replace(envelope, assessments=(assessment,))
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(EnvelopeValidationError):
                persist_scheduled_run(Path(tmp) / "legacy-envelope.sqlite3", legacy_shape)
    def test_storage_rejects_private_canary_without_creating_database_or_leaking_error(self) -> None:
        envelope = _envelope()
        unsafe_assessment = dict(envelope.assessments[0])
        unsafe_assessment["matched_evidence"] = ["PRIVATE_PROFILE_CANARY"]
        unsafe = replace(envelope, assessments=(unsafe_assessment,))

        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "private.sqlite3"
            with self.assertRaises(EnvelopeValidationError) as caught:
                persist_scheduled_run(db_path, unsafe)

            self.assertFalse(db_path.exists())

        self.assertNotIn("PRIVATE_PROFILE_CANARY", str(caught.exception))
    def test_storage_rejects_configured_canary_without_creating_database(self) -> None:
        configured_canary = "violet-lattice-731"
        envelope = _envelope()
        assessment = dict(envelope.assessments[0])
        assessment["title"] = f"Backend Engineer {configured_canary}"
        unsafe = replace(envelope, assessments=(assessment,))
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "configured-canary.sqlite3"
            with self.assertRaises(EnvelopeValidationError) as caught:
                persist_scheduled_run(
                    db_path,
                    unsafe,
                    configured_canaries=(configured_canary,),
                )
            self.assertFalse(db_path.exists())
        self.assertNotIn(configured_canary, str(caught.exception))

    def test_storage_rejects_configured_canary_in_report_bytes_without_database(self) -> None:
        configured_canary = "violet-lattice-731"
        envelope = _envelope()
        rendered = envelope.report_artifact.rendered
        assert rendered is not None
        report = f"# {configured_canary}\n".encode("utf-8")
        unsafe_rendered = replace(
            rendered,
            markdown_bytes=report,
            content_sha256=hashlib.sha256(report).hexdigest(),
            byte_length=len(report),
        )
        unsafe = replace(
            envelope,
            report_artifact=replace(envelope.report_artifact, rendered=unsafe_rendered),
        )
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "configured-canary-report.sqlite3"
            with self.assertRaises(EnvelopeValidationError):
                persist_scheduled_run(
                    db_path,
                    unsafe,
                    configured_canaries=(configured_canary,),
                )
            self.assertFalse(db_path.exists())
    def test_storage_rejects_unicode_casefold_canary_in_strict_utf8_bytes(self) -> None:
        configured_canary = "Straße Secret"
        envelope = _envelope()
        rendered = envelope.report_artifact.rendered
        assert rendered is not None
        report = b"# STRASSE SECRET\n"
        unsafe_rendered = replace(
            rendered,
            markdown_bytes=report,
            content_sha256=hashlib.sha256(report).hexdigest(),
            byte_length=len(report),
        )
        unsafe = replace(
            envelope,
            report_artifact=replace(envelope.report_artifact, rendered=unsafe_rendered),
        )
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "unicode-casefold.sqlite3"
            with self.assertRaises(EnvelopeValidationError) as caught:
                persist_scheduled_run(
                    db_path,
                    unsafe,
                    configured_canaries=(configured_canary,),
                )
            self.assertFalse(db_path.exists())
        self.assertNotIn(configured_canary, str(caught.exception))

    def test_storage_rejects_compact_military_terms_in_report_bytes(self) -> None:
        envelope = _envelope()
        report_artifact = envelope.report_artifact
        rendered = report_artifact.rendered
        assert rendered is not None
        for expression in ("군복무", "군 면제", "대체복무", "병역특례"):
            with self.subTest(expression=expression), tempfile.TemporaryDirectory() as tmp:
                report = f"# {expression}\n".encode("utf-8")
                unsafe_rendered = replace(
                    rendered,
                    markdown_bytes=report,
                    content_sha256=hashlib.sha256(report).hexdigest(),
                    byte_length=len(report),
                )
                unsafe = replace(
                    envelope,
                    report_artifact=replace(
                        report_artifact,
                        rendered=unsafe_rendered,
                    ),
                )
                db_path = Path(tmp) / "military.sqlite3"
                with self.assertRaises(EnvelopeValidationError):
                    persist_scheduled_run(db_path, unsafe)
                self.assertFalse(db_path.exists())
    def test_storage_rejects_non_scheduled_failed_or_incomplete_envelopes_before_db_creation(self) -> None:
        envelope = _envelope()
        cases = (
            replace(envelope, run_identity={**dict(envelope.run_identity), "command_mode": "live-run"}),
            replace(envelope, gate_status="warning"),
            replace(envelope, gate_status="fail"),
            replace(envelope, context_status="needs_context"),
        )
        for index, invalid in enumerate(cases):
            with self.subTest(index=index):
                with tempfile.TemporaryDirectory() as tmp:
                    db_path = Path(tmp) / "invalid-state.sqlite3"
                    with self.assertRaises(EnvelopeValidationError):
                        persist_scheduled_run(db_path, invalid)
                    self.assertFalse(db_path.exists())

    def test_storage_rejects_non_utf8_or_unsafe_report_bytes_before_db_creation(self) -> None:
        envelope = _envelope()
        report_artifact = envelope.report_artifact
        rendered = report_artifact.rendered
        assert rendered is not None
        cases = (
            b"\xff\n",
            b"# PRIVATE_PROFILE_CANARY\n",
            "# 군필\n".encode("utf-8"),
            b"# raw_jd\n",
        )
        for index, report in enumerate(cases):
            with self.subTest(index=index):
                unsafe_rendered = replace(
                    rendered,
                    markdown_bytes=report,
                    content_sha256=hashlib.sha256(report).hexdigest(),
                    byte_length=len(report),
                )
                unsafe = replace(
                    envelope,
                    report_artifact=replace(report_artifact, rendered=unsafe_rendered),
                )
                with tempfile.TemporaryDirectory() as tmp:
                    db_path = Path(tmp) / "unsafe-report.sqlite3"
                    with self.assertRaises(EnvelopeValidationError):
                        persist_scheduled_run(db_path, unsafe)
                    self.assertFalse(db_path.exists())
        unsafe_rendered = replace(rendered, markdown_bytes="# text\n")
        unsafe = replace(envelope, report_artifact=replace(report_artifact, rendered=unsafe_rendered))
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "text-report.sqlite3"
            with self.assertRaises(EnvelopeValidationError):
                persist_scheduled_run(db_path, unsafe)
            self.assertFalse(db_path.exists())

    def test_storage_rejects_unverified_source_urls(self) -> None:
        envelope = _envelope()
        cases = (
            ("forged-posting", "https://jobs.example.test/posting-2"),
            ("wrong-host", "https://evil.example.test/posting-1"),
            ("generic", "https://jobs.example.test"),
        )
        for name, source_url in cases:
            with self.subTest(name=name):
                assessment = dict(envelope.assessments[0])
                assessment["source_url"] = source_url
                unsafe = replace(envelope, assessments=(assessment,))
                with tempfile.TemporaryDirectory() as tmp:
                    db_path = Path(tmp) / f"{name}-url.sqlite3"
                    with self.assertRaises(EnvelopeValidationError):
                        persist_scheduled_run(db_path, unsafe)
                    self.assertFalse(db_path.exists())
    def test_storage_rejects_internal_and_military_reason_codes(self) -> None:
        envelope = _envelope()
        for code in ("military_program_review", "internal_reason"):
            with self.subTest(code=code):
                assessment = dict(envelope.assessments[0])
                assessment["reason_codes"] = [code]
                unsafe = replace(envelope, assessments=(assessment,))
                with tempfile.TemporaryDirectory() as tmp:
                    db_path = Path(tmp) / "unsafe-reason.sqlite3"
                    with self.assertRaises(EnvelopeValidationError):
                        persist_scheduled_run(db_path, unsafe)
                    self.assertFalse(db_path.exists())
    def test_persistence_retains_gate_digest_with_projection_snapshot(self) -> None:
        envelope = _envelope()
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "gate-snapshot.sqlite3"
            persist_scheduled_run(db_path, envelope)
            with sqlite3.connect(db_path) as connection:
                stored = connection.execute(
                    "SELECT gate_json FROM quality_gates WHERE run_id=?", (RUN_ID,)
                ).fetchone()[0]

        snapshot = json.loads(stored)
        self.assertEqual(snapshot["gate_json_sha256"], envelope.gate_json_sha256)
        self.assertEqual(snapshot["gate_projection"]["status"], "pass")

    def test_valid_v3_open_is_read_only_and_repeated_envelope_is_idempotent(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "recruit.sqlite3"
            envelope = _envelope()
            persist_scheduled_run(db_path, envelope)
            with sqlite3.connect(db_path) as connection:
                before = tuple(connection.execute("SELECT key, value FROM schema_metadata ORDER BY key"))
                before_updated = connection.execute("SELECT updated_at FROM runs").fetchone()[0]
            persist_scheduled_run(db_path, envelope)
            with sqlite3.connect(db_path) as connection:
                after = tuple(connection.execute("SELECT key, value FROM schema_metadata ORDER BY key"))
                after_updated = connection.execute("SELECT updated_at FROM runs").fetchone()[0]
                run_count = connection.execute("SELECT COUNT(*) FROM runs").fetchone()[0]
                recommendation_count = connection.execute("SELECT COUNT(*) FROM recommendations").fetchone()[0]
            with self.assertRaises(EnvelopeValidationError):
                persist_scheduled_run(db_path, _envelope(score=87))

        self.assertEqual(before, after)
        self.assertEqual(before_updated, after_updated)
        self.assertEqual(run_count, 1)
        self.assertEqual(recommendation_count, 1)

    def test_higher_schema_is_forward_only_and_does_not_mutate(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "higher.sqlite3"
            with sqlite3.connect(db_path) as connection:
                connection.execute("CREATE TABLE schema_metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
                connection.execute("INSERT INTO schema_metadata VALUES ('schema_version', '4')")
                connection.commit()
            with self.assertRaises(StorageSchemaError):
                export_runs(db_path)
            with sqlite3.connect(db_path) as connection:
                marker = connection.execute("SELECT value FROM schema_metadata WHERE key='schema_version'").fetchone()[0]
                table_count = connection.execute(
                    "SELECT COUNT(*) FROM sqlite_master WHERE type='table' AND name='runs'"
                ).fetchone()[0]

        self.assertEqual(marker, "4")
        self.assertEqual(table_count, 0)
    def test_unknown_view_is_not_treated_as_fresh(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "unknown.sqlite3"
            with sqlite3.connect(db_path) as connection:
                connection.execute("CREATE VIEW unexpected AS SELECT 1 AS value")
                connection.commit()
            with self.assertRaises(StorageSchemaError):
                export_runs(db_path)
            with sqlite3.connect(db_path) as connection:
                self.assertEqual(
                    connection.execute(
                        "SELECT COUNT(*) FROM sqlite_master WHERE type='table' AND name='runs'"
                    ).fetchone()[0],
                    0,
                )

    def test_v3_missing_guard_trigger_is_rejected_without_mutation(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "missing-trigger.sqlite3"
            persist_scheduled_run(db_path, _envelope())
            with sqlite3.connect(db_path) as connection:
                connection.execute("DROP TRIGGER schema_metadata_no_downgrade_delete")
                connection.commit()
            with self.assertRaises(StorageSchemaError):
                export_runs(db_path)
    def test_v3_unknown_index_is_rejected_without_mutation(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "unknown-index.sqlite3"
            persist_scheduled_run(db_path, _envelope())
            with sqlite3.connect(db_path) as connection:
                connection.execute("CREATE INDEX unexpected_index ON runs(run_date)")
                connection.commit()
            with self.assertRaises(StorageSchemaError):
                export_runs(db_path)

    def test_markerless_v1_migration_preserves_feedback_continuity(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "legacy.sqlite3"
            _legacy_database(db_path)
            runs = export_runs(db_path)
            recommendations = export_recommendations(db_path)
            feedback = export_feedback_events(db_path)
            with sqlite3.connect(db_path) as connection:
                metadata = dict(connection.execute("SELECT key, value FROM schema_metadata"))
                feedback_columns = {row[1] for row in connection.execute("PRAGMA table_info(feedback_events)")}
        self.assertEqual(metadata["schema_version"], "4")
        self.assertEqual(runs[0]["report_generated"], 0)
        self.assertIsNone(runs[0]["report_path"])
        self.assertEqual(runs[0]["status"], "legacy")
        self.assertEqual(runs[0]["context_status"], "unverified")
        self.assertIsNone(recommendations[0]["source_url"])
        self.assertEqual(recommendations[0]["source_detail_quality"], "manual_only")
        self.assertEqual(recommendations[0]["final_disposition"], "manual_review")
        self.assertEqual(feedback[0]["recommendation_id"], "legacy-rec")
        self.assertEqual(feedback[0]["posting_key"], recommendations[0]["posting_key"])
        self.assertEqual(feedback[0]["source_id"], "fixture")
        self.assertIsNone(feedback[0]["source_url"])
        self.assertIn("record_schema_version", feedback_columns)

    def test_marked_legacy_v1_and_v2_migrate_without_downgrade(self) -> None:
        for marker, current_feedback in (("1", False), ("2", True)):
            with self.subTest(marker=marker):
                with tempfile.TemporaryDirectory() as tmp:
                    db_path = Path(tmp) / "legacy.sqlite3"
                    _legacy_database(db_path, marker=marker, current_feedback=current_feedback)
                    export_runs(db_path)
                    recommendations = export_recommendations(db_path)
                    feedback = export_feedback_events(db_path)
                    with sqlite3.connect(db_path) as connection:
                        self.assertEqual(
                            connection.execute("SELECT value FROM schema_metadata WHERE key='schema_version'").fetchone()[0],
                            "4",
                        )
                        self.assertEqual(
                            connection.execute("SELECT record_schema_version FROM recommendations").fetchone()[0], 3
                        )
                    self.assertEqual(feedback[0]["posting_key"], recommendations[0]["posting_key"])
                    self.assertEqual(feedback[0]["source_id"], "fixture")
                    self.assertIsNone(feedback[0]["source_url"])

    def test_legacy_migration_nulls_unverified_links_and_keeps_identity(self) -> None:
        cases = (
            ("fixture", "https://jobs.example.test/other-posting"),
            ("unknown-source", "https://jobs.example.test/posting-1"),
        )
        for source_id, source_url in cases:
            with self.subTest(source_id=source_id):
                with tempfile.TemporaryDirectory() as tmp:
                    db_path = Path(tmp) / "invalid.sqlite3"
                    _legacy_database(db_path, source_id=source_id, source_url=source_url)
                    recommendations = export_recommendations(db_path)
                    feedback = export_feedback_events(db_path)

                self.assertIsNone(recommendations[0]["source_url"])
                self.assertIsNone(feedback[0]["source_url"])
                self.assertEqual(feedback[0]["recommendation_id"], recommendations[0]["recommendation_id"])
                self.assertEqual(feedback[0]["posting_key"], recommendations[0]["posting_key"])

    def test_legacy_migration_does_not_use_raw_url_as_sole_identity(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            first_path = Path(tmp) / "first.sqlite3"
            second_path = Path(tmp) / "second.sqlite3"
            _legacy_database(
                first_path,
                source_url="https://jobs.example.test/private-first",
                source_posting_id=None,
            )
            _legacy_database(
                second_path,
                source_url="https://jobs.example.test/private-second",
                source_posting_id=None,
            )
            first = export_recommendations(first_path)[0]
            second = export_recommendations(second_path)[0]
            first_feedback = export_feedback_events(first_path)[0]
            second_feedback = export_feedback_events(second_path)[0]

        self.assertIsNone(first["source_url"])
        self.assertIsNone(second["source_url"])
        self.assertIsNone(first_feedback["source_url"])
        self.assertIsNone(second_feedback["source_url"])
        self.assertEqual(first["posting_key"], second["posting_key"])
        self.assertEqual(first_feedback["posting_key"], first["posting_key"])
        self.assertEqual(second_feedback["posting_key"], second["posting_key"])
    def test_legacy_migration_rejects_unsafe_rows_atomically(self) -> None:
        cases = (
            ("private", "UPDATE recommendations SET title='PRIVATE_PROFILE_CANARY'"),
            ("military", "UPDATE recommendations SET title='군필 복무 정보'"),
            ("raw-gaps", "UPDATE recommendations SET gaps_json='[\"raw jd\"]'"),
        )
        for name, statement in cases:
            with self.subTest(name=name), tempfile.TemporaryDirectory() as tmp:
                db_path = Path(tmp) / f"{name}.sqlite3"
                _legacy_database(db_path)
                with sqlite3.connect(db_path) as connection:
                    connection.execute(statement)
                    connection.commit()
                with self.assertRaises(StorageSchemaError):
                    export_recommendations(db_path)
                with sqlite3.connect(db_path) as connection:
                    tables = {
                        row[0] for row in connection.execute(
                            "SELECT name FROM sqlite_master WHERE type='table'"
                        )
                    }
                self.assertIn("recommendations", tables)
                self.assertNotIn("recommendations_legacy", tables)

    def test_configured_canary_blocks_legacy_migration_atomically(self) -> None:
        configured_canary = "violet-lattice-731"
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "configured-canary.sqlite3"
            _legacy_database(db_path)
            with sqlite3.connect(db_path) as connection:
                connection.execute(
                    "UPDATE recommendations SET title=?", (configured_canary,)
                )
                connection.commit()
            with self.assertRaises(StorageSchemaError):
                export_recommendations(db_path, configured_canaries=(configured_canary,))
            with sqlite3.connect(db_path) as connection:
                tables = {
                    row[0] for row in connection.execute(
                        "SELECT name FROM sqlite_master WHERE type='table'"
                    )
                }
            self.assertIn("recommendations", tables)
            self.assertNotIn("recommendations_legacy", tables)
    def test_legacy_orphan_feedback_is_rejected_without_migration(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "orphan.sqlite3"
            _legacy_database(db_path)
            with sqlite3.connect(db_path) as connection:
                connection.execute("UPDATE feedback_events SET recommendation_id='missing'")
                connection.commit()
            with self.assertRaises(StorageSchemaError):
                export_runs(db_path)
            with sqlite3.connect(db_path) as connection:
                tables = {
                    row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")
                }
                self.assertIn("recommendations", tables)
                self.assertNotIn("recommendations_legacy", tables)

    def test_legacy_invalid_feedback_enum_is_rejected_without_migration(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "invalid-feedback.sqlite3"
            _legacy_database(db_path)
            with sqlite3.connect(db_path) as connection:
                connection.execute("UPDATE feedback_events SET movement='sideways'")
                connection.commit()
            with self.assertRaises(StorageSchemaError):
                export_runs(db_path)
            with sqlite3.connect(db_path) as connection:
                tables = {
                    row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")
                }
            self.assertIn("feedback_events", tables)
            self.assertNotIn("feedback_events_legacy", tables)
    def test_legacy_recommendation_verdicts_are_rejected_without_migration(self) -> None:
        for verdict in ("include", "hold", "exclude"):
            with self.subTest(verdict=verdict), tempfile.TemporaryDirectory() as tmp:
                db_path = Path(tmp) / "recommendation-verdict.sqlite3"
                _legacy_database(db_path)
                with sqlite3.connect(db_path) as connection:
                    connection.execute("UPDATE feedback_events SET verdict=?", (verdict,))
                    connection.commit()
                with self.assertRaises(StorageSchemaError):
                    export_runs(db_path)
                with sqlite3.connect(db_path) as connection:
                    tables = {
                        row[0] for row in connection.execute(
                            "SELECT name FROM sqlite_master WHERE type='table'"
                        )
                    }
                    stored_verdict = connection.execute(
                        "SELECT verdict FROM feedback_events"
                    ).fetchone()[0]
                self.assertIn("feedback_events", tables)
                self.assertNotIn("feedback_events_legacy", tables)
                self.assertEqual(stored_verdict, verdict)
    def test_feedback_survives_idempotent_persistence(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "feedback.sqlite3"
            envelope = _envelope()
            persist_scheduled_run(db_path, envelope)
            recommendation_id = export_recommendations(db_path)[0]["recommendation_id"]
            event_id = add_feedback_event(
                db_path,
                recommendation_id=recommendation_id,
                verdict="interesting",
                reason="Useful role",
                movement="up",
                created_at="2026-07-02T00:00:00+00:00",
            )
            persist_scheduled_run(db_path, envelope)
            feedback = export_feedback_events(db_path)

        self.assertEqual(len(feedback), 1)
        self.assertEqual(feedback[0]["event_id"], event_id)
        self.assertEqual(feedback[0]["run_id"], RUN_ID)
        self.assertEqual(feedback[0]["posting_key"], "posting-v3-1")
    def test_feedback_rejects_unsafe_timestamp_and_enums_without_mutation(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "feedback-validation.sqlite3"
            persist_scheduled_run(db_path, _envelope())
            cases = (
                {"created_at": "2026-07-02"},
                {
                    "created_at": "2026-07-02T00:00:00+00:00",
                    "configured_canaries": ("2026-07-02T00:00:00+00:00",),
                },
                {"verdict": "not-a-verdict"},
                *({"verdict": verdict} for verdict in (
                    "expired",
                    "exclude",
                    "manual_review",
                    "apply",
                    "hold",
                    "low_priority",
                    "include",
                )),
                {"movement": "sideways"},
            )
            for overrides in cases:
                with self.subTest(overrides=overrides):
                    kwargs = {
                        "recommendation_id": "recommendation-v3-1",
                        "verdict": "interesting",
                        "reason": "Useful role",
                        "movement": "same",
                        "created_at": "2026-07-02T00:00:00+00:00",
                    }
                    kwargs.update(overrides)
                    with self.assertRaises(ValueError):
                        add_feedback_event(db_path, **kwargs)
                    with sqlite3.connect(db_path) as connection:
                        self.assertEqual(connection.execute("SELECT COUNT(*) FROM feedback_events").fetchone()[0], 0)

    def test_v3_export_rejects_sqlite_tampered_recommendation_title(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "tampered-title.sqlite3"
            persist_scheduled_run(db_path, _envelope())
            with sqlite3.connect(db_path) as connection:
                connection.execute(
                    "UPDATE recommendations SET title='PRIVATE_PROFILE_CANARY' WHERE recommendation_id=?",
                    ("recommendation-v3-1",),
                )
                connection.commit()
            with self.assertRaises(StorageSchemaError):
                export_recommendations(db_path)

    def test_v3_export_rejects_sqlite_forged_recommendation_url(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "tampered-url.sqlite3"
            persist_scheduled_run(db_path, _envelope())
            with sqlite3.connect(db_path) as connection:
                connection.execute(
                    "UPDATE recommendations SET source_url='https://evil.example/forged' WHERE recommendation_id=?",
                    ("recommendation-v3-1",),
                )
                connection.commit()
            with self.assertRaises(StorageSchemaError):
                export_recommendations(db_path)

    def test_v3_export_rejects_sqlite_tampered_feedback_and_run(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "tampered-feedback.sqlite3"
            persist_scheduled_run(db_path, _envelope())
            recommendation_id = export_recommendations(db_path)[0]["recommendation_id"]
            add_feedback_event(
                db_path,
                recommendation_id=recommendation_id,
                verdict="interesting",
                reason="Useful role",
                movement="up",
                created_at="2026-07-02T00:00:00+00:00",
            )
            with sqlite3.connect(db_path) as connection:
                connection.execute("UPDATE feedback_events SET reason='PRIVATE_REASON_CANARY'")
                connection.commit()
            with self.assertRaises(StorageSchemaError):
                export_feedback_events(db_path)

            with sqlite3.connect(db_path) as connection:
                connection.execute("UPDATE feedback_events SET reason='Useful role'")
                connection.execute("UPDATE runs SET status='warning' WHERE run_id=?", (RUN_ID,))
                connection.commit()
            with self.assertRaises(StorageSchemaError):
                export_runs(db_path)
    def test_v3_export_rejects_recommendation_verdict_leaks_in_feedback(self) -> None:
        recommendation_only = (
            "expired",
            "exclude",
            "manual_review",
            "apply",
            "hold",
            "low_priority",
            "include",
        )
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "feedback-verdict.sqlite3"
            persist_scheduled_run(db_path, _envelope())
            add_feedback_event(
                db_path,
                recommendation_id="recommendation-v3-1",
                verdict="interesting",
                reason="Useful role",
                created_at="2026-07-02T00:00:00+00:00",
            )
            for verdict in recommendation_only:
                with self.subTest(verdict=verdict):
                    with sqlite3.connect(db_path) as connection:
                        connection.execute("UPDATE feedback_events SET verdict=?", (verdict,))
                        connection.commit()
                    with self.assertRaises(StorageSchemaError):
                        export_feedback_events(db_path)
                    with sqlite3.connect(db_path) as connection:
                        connection.execute(
                            "UPDATE feedback_events SET verdict='interesting'"
                        )
                        connection.commit()
    def test_legacy_migration_rolls_back_after_v3_data_insertion_failure(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "legacy-fault.sqlite3"
            _legacy_database(db_path)
            tables = ("runs", "source_attempts", "recommendations", "quality_gates", "feedback_events")
            with sqlite3.connect(db_path) as connection:
                before_schema = tuple(connection.execute(
                    "SELECT type, name, tbl_name, sql FROM sqlite_master "
                    "WHERE name NOT LIKE 'sqlite_%' ORDER BY type, name"
                ))
                before_rows = {
                    table: tuple(connection.execute(f"SELECT * FROM {table}"))
                    for table in tables
                }

            original_connect = sqlite3.connect

            class FailAfterFeedbackInsert(sqlite3.Connection):
                def execute(self, sql, parameters=()):
                    cursor = super().execute(sql, parameters)
                    if sql.lstrip().upper().startswith("INSERT INTO FEEDBACK_EVENTS"):
                        raise RuntimeError("injected migration failure")
                    return cursor

            with patch(
                "recruit_crawler._storage_core.sqlite3.connect",
                side_effect=lambda *args, **kwargs: original_connect(
                    *args, factory=FailAfterFeedbackInsert, **kwargs
                ),
            ):
                with self.assertRaises(StorageSchemaError):
                    export_recommendations(db_path)

            with sqlite3.connect(db_path) as connection:
                after_schema = tuple(connection.execute(
                    "SELECT type, name, tbl_name, sql FROM sqlite_master "
                    "WHERE name NOT LIKE 'sqlite_%' ORDER BY type, name"
                ))
                after_rows = {
                    table: tuple(connection.execute(f"SELECT * FROM {table}"))
                    for table in tables
                }
                migrated_tables = {
                    row[0] for row in connection.execute(
                        "SELECT name FROM sqlite_master WHERE type='table'"
                    )
                }

        self.assertEqual(after_schema, before_schema)
        self.assertEqual(after_rows, before_rows)
        self.assertIn("recommendations", migrated_tables)
        self.assertNotIn("recommendations_legacy", migrated_tables)
    def test_legacy_migration_locks_snapshot_against_concurrent_writer(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "legacy-race.sqlite3"
            _legacy_database(db_path)
            snapshot_ready = threading.Event()
            release_snapshot = threading.Event()
            writer_attempted = threading.Event()
            writer_errors: list[Exception] = []
            migration_errors: list[Exception] = []
            original_connect = sqlite3.connect

            class WriterConnection(sqlite3.Connection):
                def execute(self, sql, parameters=()):
                    if sql.lstrip().upper().startswith("INSERT INTO RUNS"):
                        writer_attempted.set()
                    return super().execute(sql, parameters)

            original_assert = __import__(
                "recruit_crawler.storage", fromlist=["_assert_configured_canaries"]
            )._assert_configured_canaries

            def pause_after_snapshot(value, matcher):
                snapshot_ready.set()
                if not release_snapshot.wait(timeout=5):
                    raise RuntimeError("snapshot synchronization timed out")
                return original_assert(value, matcher)

            def migrate() -> None:
                try:
                    export_recommendations(db_path)
                except Exception as error:
                    migration_errors.append(error)

            def writer() -> None:
                try:
                    with original_connect(db_path, timeout=5, factory=WriterConnection) as connection:
                        connection.execute(
                            "INSERT INTO runs VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                            (
                                "concurrent-run", "scheduled-run", "2026-07-02", "source-hash",
                                "profile-hash", "pass", "complete", 1, None, 0, 0, "created", "updated",
                            ),
                        )
                        connection.commit()
                except Exception as error:
                    writer_errors.append(error)

            with patch(
                "recruit_crawler.storage._assert_configured_canaries",
                side_effect=pause_after_snapshot,
            ):
                migration_thread = threading.Thread(target=migrate)
                migration_thread.start()
                try:
                    self.assertTrue(snapshot_ready.wait(timeout=5))
                    writer_thread = threading.Thread(target=writer)
                    writer_thread.start()
                    self.assertTrue(writer_attempted.wait(timeout=5))
                    release_snapshot.set()
                    migration_thread.join(timeout=5)
                    writer_thread.join(timeout=5)
                finally:
                    release_snapshot.set()
                    migration_thread.join(timeout=5)
                    if "writer_thread" in locals():
                        writer_thread.join(timeout=5)

            self.assertEqual(migration_errors, [])
            self.assertEqual(len(writer_errors), 1)
            with sqlite3.connect(db_path) as connection:
                self.assertEqual(connection.execute("SELECT COUNT(*) FROM runs").fetchone()[0], 1)
                self.assertEqual(connection.execute("SELECT run_id FROM runs").fetchone()[0], "legacy-run")
    def test_feedback_commit_failure_does_not_report_uncommitted_event(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "feedback-commit-fault.sqlite3"
            persist_scheduled_run(db_path, _envelope())
            recommendation_id = export_recommendations(db_path)[0]["recommendation_id"]

            original_connect = sqlite3.connect

            class FailFeedbackCommit(sqlite3.Connection):
                fail_next_commit = False

                def execute(self, sql, parameters=()):
                    cursor = super().execute(sql, parameters)
                    if sql.lstrip().upper().startswith("INSERT INTO FEEDBACK_EVENTS"):
                        type(self).fail_next_commit = True
                    return cursor

                def commit(self):
                    if type(self).fail_next_commit:
                        type(self).fail_next_commit = False
                        raise sqlite3.OperationalError("injected commit failure")
                    return super().commit()

            with patch(
                "recruit_crawler._storage_core.sqlite3.connect",
                side_effect=lambda *args, **kwargs: original_connect(
                    *args, factory=FailFeedbackCommit, **kwargs
                ),
            ):
                with self.assertRaises(sqlite3.OperationalError):
                    add_feedback_event(
                        db_path,
                        recommendation_id=recommendation_id,
                        verdict="interesting",
                        reason="Useful role",
                        movement="up",
                        created_at="2026-07-02T00:00:00+00:00",
                    )

            self.assertEqual(export_feedback_events(db_path), [])
    def test_v3_schema_guards_reject_marker_replace_downgrade_and_delete(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "guards.sqlite3"
            persist_scheduled_run(db_path, _envelope())
            with sqlite3.connect(db_path) as connection:
                with self.assertRaises(sqlite3.IntegrityError):
                    connection.execute("INSERT INTO schema_metadata(key, value) VALUES ('schema_version', '3')")
                with self.assertRaises(sqlite3.IntegrityError):
                    connection.execute("UPDATE schema_metadata SET value='2' WHERE key='schema_version'")
                with self.assertRaises(sqlite3.IntegrityError):
                    connection.execute("UPDATE schema_metadata SET key='renamed' WHERE key='schema_version'")
                with self.assertRaises(sqlite3.IntegrityError):
                    connection.execute("DELETE FROM schema_metadata WHERE key='schema_version'")
    def test_pending_stage_is_hidden_and_rejects_feedback_until_finalized(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "pending.sqlite3"
            envelope = _envelope()
            token = _stage_scheduled_run(db_path, envelope)
            self.assertIsInstance(token, str)
            self.assertEqual(export_runs(db_path), [])
            self.assertEqual(export_recommendations(db_path), [])
            with self.assertRaises(ValueError):
                add_feedback_event(
                    db_path,
                    recommendation_id="recommendation-v3-1",
                    verdict="interesting",
                    reason="pending",
                )
            self.assertTrue(
                finalize_scheduled_run(db_path, RUN_ID, token)
            )
            self.assertEqual(len(export_runs(db_path)), 1)
            self.assertEqual(len(export_recommendations(db_path)), 1)

    def test_idempotent_committed_rerun_preserves_feedback(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "idempotent.sqlite3"
            envelope = _envelope()
            persist_scheduled_run(db_path, envelope)
            recommendation_id = export_recommendations(db_path)[0]["recommendation_id"]
            add_feedback_event(
                db_path,
                recommendation_id=recommendation_id,
                verdict="interesting",
                reason="keep",
                created_at="2026-07-02T00:00:00+00:00",
            )
            self.assertIsNone(_stage_scheduled_run(db_path, envelope))
            self.assertEqual(len(export_feedback_events(db_path)), 1)


    def test_v3_migration_success_is_idempotent_without_fabricated_outcomes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "v3.sqlite3"
            persist_scheduled_run(db_path, _envelope())
            _demote_v4_to_v3(db_path)
            first = export_source_outcomes(db_path)
            second = export_source_outcomes(db_path)
            with sqlite3.connect(db_path) as connection:
                marker = dict(connection.execute("SELECT key, value FROM schema_metadata"))
                outcome_count = connection.execute(
                    "SELECT COUNT(*) FROM source_outcomes"
                ).fetchone()[0]
            self.assertEqual(first, [])
            self.assertEqual(second, [])
            self.assertEqual(marker["schema_version"], "4")
            self.assertEqual(outcome_count, 0)

    def test_v3_migration_rolls_back_on_unsafe_outcome_projection(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "v3-rollback.sqlite3"
            persist_scheduled_run(db_path, _envelope())
            _demote_v4_to_v3(db_path)
            with sqlite3.connect(db_path) as connection:
                connection.execute(
                    "UPDATE source_attempts SET error_codes_json='[\"PRIVATE_PROFILE_CANARY\"]'"
                )
                connection.commit()
            with self.assertRaises(StorageSchemaError):
                export_runs(db_path)
            with sqlite3.connect(db_path) as connection:
                self.assertEqual(
                    connection.execute("SELECT value FROM schema_metadata WHERE key='schema_version'").fetchone()[0],
                    "3",
                )
                self.assertEqual(
                    connection.execute(
                        "SELECT COUNT(*) FROM sqlite_master WHERE type='table' AND name='source_outcomes'"
                    ).fetchone()[0],
                    0,
                )

    def test_mixed_v3_v4_schema_is_rejected_without_mutation(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "mixed.sqlite3"
            persist_scheduled_run(db_path, _envelope())
            with sqlite3.connect(db_path) as connection:
                connection.execute("UPDATE schema_metadata SET value='3' WHERE key='schema_version'")
                connection.execute("UPDATE schema_metadata SET value='storage-v3' WHERE key='schema_signature'")
                connection.execute("UPDATE schema_metadata SET value='3' WHERE key='persistence_envelope_schema_version'")
                connection.commit()
            with self.assertRaises(StorageSchemaError):
                export_runs(db_path)
            with sqlite3.connect(db_path) as connection:
                self.assertEqual(
                    connection.execute("SELECT value FROM schema_metadata WHERE key='schema_version'").fetchone()[0],
                    "3",
                )

    def test_probe_database_lock_fails_closed_as_indeterminate(self) -> None:
        class BlockedConnection:
            def execute(self, *args, **kwargs):
                raise sqlite3.OperationalError("database is locked")

        self.assertEqual(
            probe_run_transaction_state(
                BlockedConnection(),
                RUN_ID,
                "0" * 64,
                expected_identity={"run_id": RUN_ID},
                expected_versions={},
                expected_token="0" * 64,
            ),
            "indeterminate",
        )
    def test_v4_probe_roundtrip_reports_pending_and_committed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "probe.sqlite3"
            envelope = _envelope()
            token = _stage_scheduled_run(db_path, envelope)
            expected_identity = dict(envelope.run_identity)
            expected_versions = {
                "record_schema_version": 3,
                "pipeline_schema_version": 2,
                "score_schema_version": 2,
                "disposition_schema_version": 2,
            }
            expected_gate_hash = envelope.gate_json_sha256
            expected_content_hash = envelope.report_artifact.rendered.content_sha256
            self.assertIsInstance(expected_content_hash, str)
            assert expected_content_hash is not None
            with sqlite3.connect(db_path) as connection:
                self.assertEqual(
                    probe_run_transaction_state(connection, RUN_ID, token),
                    "pending",
                )
                self.assertEqual(
                    probe_run_transaction_state(
                        connection,
                        RUN_ID,
                        "0" * 64,
                        expected_identity=expected_identity,
                        expected_versions=expected_versions,
                        expected_gate_json_sha256=expected_gate_hash,
                        expected_content_sha256=expected_content_hash,
                        expected_token="0" * 64,
                    ),
                    "indeterminate",
                )
            assert token is not None
            self.assertFalse(
                finalize_scheduled_run(
                    db_path,
                    RUN_ID,
                    token,
                    expected_identity={**expected_identity, "run_date": "2026-07-02"},
                    expected_versions=expected_versions,
                    expected_gate_json_sha256=expected_gate_hash,
                    expected_content_sha256=expected_content_hash,
                    expected_token=token,
                )
            )
            self.assertTrue(finalize_scheduled_run(db_path, RUN_ID, token))
            with sqlite3.connect(db_path) as connection:
                connection.row_factory = sqlite3.Row
                self.assertEqual(
                    probe_run_transaction_state(
                        connection,
                        RUN_ID,
                        token,
                        expected_identity=expected_identity,
                        expected_versions=expected_versions,
                        expected_gate_json_sha256=expected_gate_hash,
                        expected_content_sha256=expected_content_hash,
                    ),
                    "committed",
                )
                self.assertEqual(
                    probe_run_transaction_state(
                        connection,
                        RUN_ID,
                        "0" * 64,
                        expected_identity=expected_identity,
                        expected_versions=expected_versions,
                        expected_gate_json_sha256=expected_gate_hash,
                        expected_content_sha256=expected_content_hash,
                        expected_token="0" * 64,
                    ),
                    "indeterminate",
                )
                self.assertEqual(
                    probe_run_transaction_state(
                        connection,
                        RUN_ID,
                        token,
                        expected_identity={**expected_identity, "run_date": "2026-07-02"},
                        expected_versions=expected_versions,
                    ),
                    "indeterminate",
                )
                self.assertEqual(
                    probe_run_transaction_state(
                        connection,
                        RUN_ID,
                        token,
                        expected_identity=expected_identity,
                        expected_versions=expected_versions,
                        expected_gate_json_sha256=expected_gate_hash,
                        expected_content_sha256="b" * 64,
                        expected_token=token,
                    ),
                    "indeterminate",
                )
            self.assertEqual(
                scheduled_run_persistence_state(
                    db_path,
                    RUN_ID,
                    expected_identity=expected_identity,
                    expected_versions=expected_versions,
                    expected_gate_json_sha256=expected_gate_hash,
                    expected_content_sha256=expected_content_hash,
                    expected_token=token,
                ),
                "committed",
            )
            self.assertEqual(
                scheduled_run_persistence_state(
                    db_path,
                    RUN_ID,
                    expected_identity=expected_identity,
                    expected_versions={**expected_versions, "pipeline_schema_version": 4},
                ),
                "indeterminate",
            )

if __name__ == "__main__":
    unittest.main()
