from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

import io
import sqlite3
from dataclasses import replace
import json
import tempfile
from contextlib import contextmanager, redirect_stdout
from datetime import date
from unittest.mock import patch

from recruit_crawler.cli import main as cli_main
from recruit_crawler.gate import (
    build_gate_v2,
    canonical_gate_bytes,
    canonical_gate_v4_bytes,
)
from recruit_crawler.config import load_config
from recruit_crawler.scheduled import (
    ScheduledRunRequest,
    _ScheduledRecoveryState,
    _recover_locked_scheduled_failure,
    _report_lock_path,
    _run_scheduled_run_at_service_boundary,
    _scheduled_output_locks,
    _write_gate_output,
    run_scheduled_job,
)
from recruit_crawler.report_writer import RuntimeContext
from recruit_crawler.pipeline import CollectionBatch, build_pipeline_result_v2, run_scheduled_run
from recruit_crawler.storage import export_recommendations, export_runs
from recruit_crawler.schemas import PersistenceEnvelopeV4, PipelineResultV4

CONFIG = ROOT / "config" / "sample_config.json"


class ScheduledRunCliTests(unittest.TestCase):
    def test_scheduled_service_passes_one_immutable_context_to_collection(self) -> None:
        class Clock:
            now = 100.0

            def __call__(self) -> float:
                return self.now

        clock = Clock()
        context = RuntimeContext.start(
            total_seconds=20,
            cleanup_seconds=5,
            command_mode="scheduled-run",
            monotonic=clock,
        )
        received: list[RuntimeContext] = []
        collected = object()

        def collect(config, run_date, *, coordinator, runtime_context):
            received.append(runtime_context)
            return collected

        with patch("recruit_crawler.scheduled.run_scheduled_run", side_effect=collect):
            with self.assertRaisesRegex(
                TypeError,
                "scheduled service boundary requires PipelineResultV4",
            ):
                _run_scheduled_run_at_service_boundary(
                    object(),
                    date(2026, 6, 30),
                    coordinator=object(),
                    runtime_context=context,
                )
        self.assertEqual(received, [context])
        self.assertEqual(context.normal_work_deadline, 115.0)
        self.assertEqual(context.hard_deadline, 120.0)
    def test_scheduled_pipeline_clips_collection_budget_and_fails_closed(self) -> None:
        config = load_config(CONFIG, allow_real_sources=True)

        class Clock:
            now = 100.0

            def __call__(self) -> float:
                return self.now

        clock = Clock()
        context = RuntimeContext.start(
            total_seconds=20,
            cleanup_seconds=5,
            command_mode="scheduled-run",
            monotonic=clock,
        )
        clock.now = 103.0
        with patch("recruit_crawler.pipeline.SourceCollectionCoordinator") as coordinator_type:
            coordinator_type.return_value.collect.return_value = CollectionBatch((), (), (), ())
            result = run_scheduled_run(
                config,
                date(2026, 6, 30),
                runtime_context=context,
            )

        self.assertIsInstance(result, PipelineResultV4)
        coordinator_type.assert_called_once_with(
            config,
            total_budget_seconds=12.0,
            runtime_context=context,
        )

        clock.now = 115.0
        with patch("recruit_crawler.pipeline.SourceCollectionCoordinator") as coordinator_type:
            with self.assertRaisesRegex(TimeoutError, "normal work deadline exceeded"):
                run_scheduled_run(
                    config,
                    date(2026, 6, 30),
                    runtime_context=context,
                )
        coordinator_type.assert_not_called()
    def test_scheduled_service_rejects_collection_after_normal_deadline(self) -> None:
        class Clock:
            now = 10.0

            def __call__(self) -> float:
                return self.now

        clock = Clock()
        context = RuntimeContext.start(
            total_seconds=10,
            cleanup_seconds=2,
            command_mode="scheduled-run",
            monotonic=clock,
        )
        clock.now = 20.0
        with tempfile.TemporaryDirectory() as tmp, patch(
            "recruit_crawler.scheduled.run_scheduled_run"
        ) as collect, patch(
            "recruit_crawler.scheduled.persist_scheduled_run"
        ) as persist:
            tmp_path = Path(tmp)
            config_path = self._write_scheduled_config(tmp_path)
            result = run_scheduled_job(
                ScheduledRunRequest(
                    config=load_config(config_path, allow_real_sources=True),
                    run_date=date(2026, 6, 30),
                    quality_gate_output=tmp_path / "gate.json",
                    runtime_context=context,
                )
            )

        self.assertEqual(result.exit_code, 1)
        collect.assert_not_called()
        persist.assert_not_called()
        self.assertFalse((tmp_path / "gate.json").exists())
    def _write_scheduled_config(self, tmp_path: Path) -> Path:
        raw = json.loads(CONFIG.read_text(encoding="utf-8"))
        fixture_path = tmp_path / "postings.json"
        raw["fixture_path"] = str(fixture_path)
        raw["output_dir"] = str(tmp_path / "reports")
        fixture_path.write_text((ROOT / "fixtures" / "postings.json").read_text(encoding="utf-8"), encoding="utf-8")
        config_path = tmp_path / "scheduled_config.json"
        config_path.write_text(json.dumps(raw), encoding="utf-8")
        return config_path

    def test_scheduled_run_writes_contract_quality_gate(self) -> None:
        output = io.StringIO()
        with tempfile.TemporaryDirectory() as tmp, redirect_stdout(output):
            tmp_path = Path(tmp)
            config_path = self._write_scheduled_config(tmp_path)
            gate_path = tmp_path / "scheduled_quality_gate.json"
            db_path = tmp_path / "recruit.sqlite3"

            exit_code = cli_main(
                [
                    "scheduled-run",
                    "--config",
                    str(config_path),
                    "--run-date",
                    "2026-06-30",
                    "--output-dir",
                    str(tmp_path / "scheduled_reports"),
                    "--quality-gate-output",
                    str(gate_path),
                    "--db-path",
                    str(db_path),
                ]
            )
            gate = json.loads(gate_path.read_text(encoding="utf-8"))

        self.assertEqual(exit_code, 0)
        self.assertEqual(set(gate), {
            "schema_version",
            "command_mode",
            "run_date",
            "pipeline_schema_version",
            "score_schema_version",
            "disposition_schema_version",
            "status",
            "context_status",
            "report",
            "sources",
            "summary",
            "eligibility_reason_counts",
            "manual_reason_counts",
            "invariants",
            "findings",
            "source_outcomes",
        })
        self.assertEqual(gate["schema_version"], 4)
        self.assertEqual(gate["command_mode"], "scheduled-run")
        self.assertEqual(gate["pipeline_schema_version"], 4)
        self.assertEqual(gate["score_schema_version"], 2)
        self.assertEqual(gate["disposition_schema_version"], 2)
        self.assertEqual(gate["status"], "pass")
        self.assertEqual(gate["context_status"], "complete")
        self.assertTrue(gate["report"]["generated"])
        self.assertEqual(len(gate["sources"]), 1)
        self.assertEqual(gate["sources"][0]["source_id"], "fixture")
        self.assertEqual(len(gate["sources"][0]["detail_quality"]), 3)
        self.assertIn("pipeline_schema_v4", gate["invariants"])
        self.assertEqual(len(gate["run_date"]), 10)
        self.assertIn("Scheduled run complete", output.getvalue())
        self.assertIn("Quality gate status: pass", output.getvalue())
        self.assertIn("recruiting-scheduled-run-2026-06-30.md", output.getvalue())

    def test_scheduled_gate_reason_counts_suppress_military_codes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            config_path = self._write_scheduled_config(tmp_path)
            result = run_scheduled_job(
                ScheduledRunRequest(
                    config=load_config(config_path, allow_real_sources=True),
                    run_date=date(2026, 6, 30),
                    quality_gate_output=tmp_path / "gate.json",
                )
            )

        for field in ("eligibility_reason_counts", "manual_reason_counts"):
            self.assertFalse(
                any("military" in code.casefold() for code in result.gate[field])
            )
        self.assertNotIn("military", json.dumps(result.gate, ensure_ascii=False).casefold())
    def test_gate_source_and_finding_boundaries_fail_closed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            config_path = self._write_scheduled_config(tmp_path)
            scheduled = run_scheduled_job(
                ScheduledRunRequest(
                    config=load_config(config_path, allow_real_sources=True),
                    run_date=date(2026, 6, 30),
                    quality_gate_output=tmp_path / "gate.json",
                )
            )

        unsafe_metric = replace(
            scheduled.result.source_metrics[0],
            source_id="PRIVATE_PROFILE_CANARY",
        )
        unsafe_result = replace(
            scheduled.result,
            source_metrics=(unsafe_metric,),
        )
        gate = build_gate_v2(
            unsafe_result,
            enabled_source_ids=("fixture", "military-source", "<<invalid>>"),
            report_artifact=scheduled.report_artifact,
        )
        gate_bytes = canonical_gate_bytes(gate)
        public_gate = gate_bytes.decode("utf-8")

        for rejected in ("PRIVATE_PROFILE_CANARY", "military-source", "<<invalid>>"):
            self.assertNotIn(rejected, public_gate)
        self.assertIn('"source_id":"unknown-source"', public_gate)
        self.assertEqual(gate["sources"][0]["source_id"], "unknown-source")
        self.assertTrue(
            any(
                finding["source_id"] == "unknown-source"
                for finding in gate["findings"]
            )
        )
        self.assertEqual(gate["status"], "fail")
    def test_scheduled_gate_rejects_configured_canary_source_id(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            config_path = self._write_scheduled_config(tmp_path)
            scheduled = run_scheduled_job(
                ScheduledRunRequest(
                    config=load_config(config_path, allow_real_sources=True),
                    run_date=date(2026, 6, 30),
                    quality_gate_output=tmp_path / "gate.json",
                )
            )

        canary = "violet-lattice-731"
        canary_metric = replace(
            scheduled.result.source_metrics[0],
            source_id=canary,
        )
        canary_result = replace(
            scheduled.result,
            source_metrics=(canary_metric,),
        )
        gate = build_gate_v2(
            canary_result,
            enabled_source_ids=(canary,),
            configured_canaries=(canary,),
            report_artifact=scheduled.report_artifact,
        )
        gate_bytes = canonical_gate_bytes(gate)

        self.assertNotIn(canary.encode("utf-8"), gate_bytes)
        self.assertEqual(gate["sources"][0]["source_id"], "unknown-source")
        self.assertTrue(
            any(
                finding["source_id"] == "unknown-source"
                for finding in gate["findings"]
            )
        )
        self.assertEqual(gate["status"], "fail")
    def test_scheduled_gate_rejects_configured_canary_error_code(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            config_path = self._write_scheduled_config(tmp_path)
            scheduled = run_scheduled_job(
                ScheduledRunRequest(
                    config=load_config(config_path, allow_real_sources=True),
                    run_date=date(2026, 6, 30),
                    quality_gate_output=tmp_path / "gate.json",
                )
            )

        canary = "collection_error"
        canary_metric = replace(
            scheduled.result.source_metrics[0],
            error_codes=("detail_fetch_failed", canary),
        )
        canary_result = replace(
            scheduled.result,
            source_metrics=(canary_metric,),
        )
        gate = build_gate_v2(
            canary_result,
            enabled_source_ids=("fixture",),
            configured_canaries=(canary,),
            report_artifact=scheduled.report_artifact,
        )
        gate_bytes = canonical_gate_bytes(gate)

        self.assertNotIn(canary.encode("utf-8"), gate_bytes)
        self.assertEqual(gate["sources"][0]["error_codes"], ["detail_fetch_failed"])
        self.assertEqual(gate["sources"][0]["error_count"], 1)
        self.assertTrue(
            any(
                finding["message"] == "source identity rejected"
                and finding["source_id"] == "unknown-source"
                for finding in gate["findings"]
            )
        )
        self.assertEqual(gate["status"], "fail")
    def test_unknown_runtime_failure_is_generic_and_fails_gate(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            config_path = self._write_scheduled_config(tmp_path)
            scheduled = run_scheduled_job(
                ScheduledRunRequest(
                    config=load_config(config_path, allow_real_sources=True),
                    run_date=date(2026, 6, 30),
                    quality_gate_output=tmp_path / "gate.json",
                )
            )

        gate = build_gate_v2(
            scheduled.result,
            enabled_source_ids=("fixture",),
            runtime_failures=("unrecognized_runtime_failure",),
            report_artifact=scheduled.report_artifact,
        )
        self.assertEqual(gate["status"], "fail")
        self.assertIn(
            "scheduled runtime failure",
            [finding["message"] for finding in gate["findings"]],
        )
        self.assertNotIn("unrecognized_runtime_failure", json.dumps(gate))
        with self.assertRaises(ValueError):
            canonical_gate_v4_bytes(gate)

    def test_canonical_gate_rejects_semantic_forged_pass(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            config_path = self._write_scheduled_config(tmp_path)
            scheduled = run_scheduled_job(
                ScheduledRunRequest(
                    config=load_config(config_path, allow_real_sources=True),
                    run_date=date(2026, 6, 30),
                    quality_gate_output=tmp_path / "gate.json",
                )
            )

        base = scheduled.gate
        finding = {
            "severity": "fail",
            "source_id": None,
            "message": "pipeline schema versions are incompatible",
        }
        source = dict(base["sources"][0])
        source["attempted"] = False
        source_with_error = dict(base["sources"][0])
        source_with_error["error_codes"] = ["detail_unverified"]
        source_with_error["error_count"] = 1
        zero_source = dict(base["sources"][0])
        zero_source.update(
            {
                "attempted": False,
                "candidate_count": 0,
                "source_rejected_count": 0,
                "duplicate_count": 0,
                "detail_quality": {
                    "verified": 0,
                    "manual_only": 0,
                    "rejected": 0,
                },
            }
        )
        zero_summary = {
            **base["summary"],
            "collected": 0,
            "source_rejected": 0,
            "source_accepted": 0,
            "duplicates_removed": 0,
            "deduplicated": 0,
        }
        cases = (
            {**base, "findings": [finding]},
            {**base, "pipeline_schema_version": 1},
            {
                **base,
                "report": {**base["report"], "queue_parity": False},
            },
            {**base, "invariants": base["invariants"][:-1]},
            {**base, "sources": [source]},
            {**base, "sources": [source_with_error]},
            {**base, "sources": [zero_source], "summary": zero_summary},
            {**base, "sources": [], "summary": zero_summary},
            {
                **base,
                "summary": {
                    **base["summary"],
                    "collected": base["summary"]["collected"] + 1,
                },
            },
        )
        for forged in cases:
            with self.subTest(forged=forged):
                with self.assertRaises(ValueError):
                    canonical_gate_v4_bytes(forged)
    def test_canonical_gate_rejects_untrusted_mapping_shape_and_text(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            config_path = self._write_scheduled_config(tmp_path)
            scheduled = run_scheduled_job(
                ScheduledRunRequest(
                    config=load_config(config_path, allow_real_sources=True),
                    run_date=date(2026, 6, 30),
                    quality_gate_output=tmp_path / "gate.json",
                )
            )

        base_gate = scheduled.gate
        cases = (
            {
                **base_gate,
                "findings": [
                    {
                        "severity": "fail",
                        "source_id": None,
                        "message": "raw private military canary payload",
                    }
                ],
            },
            {
                **base_gate,
                "findings": [
                    {
                        "severity": "critical",
                        "source_id": None,
                        "message": "source identity rejected",
                    }
                ],
            },
            {
                **base_gate,
                "raw_private_canary": "PRIVATE_PROFILE_CANARY",
            },
        )
        for malicious_gate in cases:
            with self.subTest(malicious_gate=malicious_gate):
                with self.assertRaises(ValueError):
                    canonical_gate_v4_bytes(malicious_gate)

    def test_canonical_gate_rejects_uncontrolled_source_mapping(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            config_path = self._write_scheduled_config(tmp_path)
            scheduled = run_scheduled_job(
                ScheduledRunRequest(
                    config=load_config(config_path, allow_real_sources=True),
                    run_date=date(2026, 6, 30),
                    quality_gate_output=tmp_path / "gate.json",
                )
            )

        source = dict(scheduled.gate["sources"][0])
        source["source_id"] = "PRIVATE_PROFILE_CANARY"
        malicious_gate = {
            **scheduled.gate,
            "sources": [source],
        }
        with self.assertRaises(ValueError):
            canonical_gate_v4_bytes(malicious_gate)
    def test_quality_gate_report_path_collision_preserves_existing_report(self) -> None:
        with tempfile.TemporaryDirectory() as tmp, patch(
            "recruit_crawler.scheduled.persist_rendered_report"
        ) as publish:
            tmp_path = Path(tmp)
            config_path = self._write_scheduled_config(tmp_path)
            report_path = (
                tmp_path / "reports" / "recruiting-scheduled-run-2026-06-30.md"
            ).resolve()
            report_path.parent.mkdir()
            report_path.write_bytes(b"previous report")
            result = run_scheduled_job(
                ScheduledRunRequest(
                    config=load_config(config_path, allow_real_sources=True),
                    run_date=date(2026, 6, 30),
                    quality_gate_output=report_path,
                )
            )
            remaining_content = report_path.read_bytes()

        self.assertEqual(result.exit_code, 1)
        self.assertEqual(result.gate["status"], "fail")
        self.assertEqual(remaining_content, b"previous report")
        self.assertFalse(result.report_artifact.generated)
        self.assertIn(
            "scheduled report and quality gate paths collide",
            [item["message"] for item in result.gate["findings"]],
        )
        self.assertIn(
            "Quality gate not written: report and quality gate paths collide",
            result.stdout_lines,
        )
        publish.assert_not_called()
    def test_scheduled_service_runs_without_cli_argparse(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            config_path = self._write_scheduled_config(tmp_path)
            gate_path = tmp_path / "service_quality_gate.json"
            db_path = tmp_path / "service.sqlite3"
            result = run_scheduled_job(
                ScheduledRunRequest(
                    config=load_config(config_path, allow_real_sources=True),
                    run_date=date(2026, 6, 30),
                    quality_gate_output=gate_path,
                    output_dir=tmp_path / "service_reports",
                    db_path=db_path,
                )
            )
            gate = json.loads(gate_path.read_text(encoding="utf-8"))
            db_exists = db_path.exists()
            with sqlite3.connect(db_path) as connection:
                persisted_outcomes = connection.execute(
                    "SELECT source_id, attempted, completed, status, error_code, duration_ms "
                    "FROM source_outcomes ORDER BY source_id"
                ).fetchall()

        self.assertEqual(result.exit_code, 0)
        self.assertEqual(result.gate, gate)
        self.assertIsInstance(result.result, PipelineResultV4)
        self.assertEqual(gate["schema_version"], 4)
        self.assertEqual(gate["source_outcomes"], [
            {
                "source_id": outcome.source_id,
                "attempted": outcome.attempted,
                "completed": outcome.completed,
                "status": outcome.status,
                "error_code": outcome.error_code,
                "elapsed_ms": 0,
            }
            for outcome in result.result.source_outcomes
        ])
        self.assertEqual(
            persisted_outcomes,
            [
                (
                    outcome.source_id,
                    int(outcome.attempted),
                    int(outcome.completed),
                    outcome.status,
                    outcome.error_code,
                    outcome.elapsed_ms,
                )
                for outcome in result.result.source_outcomes
            ],
        )
        self.assertEqual(gate["command_mode"], "scheduled-run")
        self.assertEqual(gate["status"], "pass")
        self.assertTrue(gate["report"]["generated"])
        self.assertTrue(db_exists)
        self.assertIn("Scheduled run complete", result.stdout_lines)
        self.assertTrue(any(line.startswith("Quality gate written: ") for line in result.stdout_lines))
    def test_scheduled_service_is_sole_v3_envelope_writer(self) -> None:
        with tempfile.TemporaryDirectory() as tmp, patch(
            "recruit_crawler.scheduled.persist_scheduled_run",
            return_value="owned-pending-token",
        ) as persist, patch(
            "recruit_crawler.scheduled.finalize_scheduled_run",
            return_value=True,
        ):
            tmp_path = Path(tmp)
            config_path = self._write_scheduled_config(tmp_path)
            result = run_scheduled_job(
                ScheduledRunRequest(
                    config=load_config(config_path, allow_real_sources=True),
                    run_date=date(2026, 6, 30),
                    quality_gate_output=tmp_path / "gate.json",
                    output_dir=tmp_path / "reports",
                    db_path=tmp_path / "recruit.sqlite3",
                )
            )

        self.assertEqual(result.exit_code, 0)
        persist.assert_called_once()
        envelope = persist.call_args.args[1]
        self.assertIsInstance(envelope, PersistenceEnvelopeV4)
        self.assertEqual(envelope.schema_version, 4)
        self.assertEqual(dict(envelope.run_identity)["run_id"], result.run_identity["run_id"])
        self.assertEqual(envelope.gate_status, "pass")
        self.assertIsInstance(result.result, PipelineResultV4)
        self.assertEqual(
            envelope.source_outcomes,
            result.result.source_outcomes,
        )

    def test_unknown_exception_after_report_publication_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp, patch(
            "recruit_crawler.scheduled._persistence_envelope",
            side_effect=RuntimeError("PRIVATE_JD /secret/path"),
        ):
            tmp_path = Path(tmp)
            config_path = self._write_scheduled_config(tmp_path)
            gate_path = tmp_path / "gate.json"
            db_path = tmp_path / "runs.sqlite3"
            result = run_scheduled_job(
                ScheduledRunRequest(
                    config=load_config(config_path, allow_real_sources=True),
                    run_date=date(2026, 6, 30),
                    quality_gate_output=gate_path,
                    db_path=db_path,
                )
            )
            gate_payload = gate_path.read_text(encoding="utf-8")
            report_path = (
                tmp_path
                / "reports"
                / "recruiting-scheduled-run-2026-06-30.md"
            )

        self.assertEqual(result.exit_code, 1)
        self.assertEqual(result.gate["status"], "fail")
        self.assertIn("scheduled runtime failure", gate_payload)
        self.assertNotIn("PRIVATE_JD", gate_payload)
        self.assertNotIn("/secret/path", gate_payload)
        self.assertFalse(report_path.exists())
        self.assertFalse(db_path.exists())
        self.assertEqual(result.publication_durability, "not_published")

    def test_unknown_exception_before_report_publication_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp, patch(
            "recruit_crawler.scheduled.project_pipeline_result",
            side_effect=RuntimeError("PRIVATE_PROFILE /secret/path"),
        ):
            tmp_path = Path(tmp)
            config_path = self._write_scheduled_config(tmp_path)
            gate_path = tmp_path / "gate.json"
            result = run_scheduled_job(
                ScheduledRunRequest(
                    config=load_config(config_path, allow_real_sources=True),
                    run_date=date(2026, 6, 30),
                    quality_gate_output=gate_path,
                )
            )
            gate_payload = gate_path.read_text(encoding="utf-8")
            report_path = (
                tmp_path
                / "reports"
                / "recruiting-scheduled-run-2026-06-30.md"
            )

        self.assertEqual(result.exit_code, 1)
        self.assertEqual(result.gate["status"], "fail")
        self.assertIn("scheduled runtime failure", gate_payload)
        self.assertNotIn("PRIVATE_PROFILE", gate_payload)
        self.assertNotIn("/secret/path", gate_payload)
        self.assertFalse(report_path.exists())
        self.assertEqual(result.publication_durability, "not_published")

    def test_finalize_failure_cannot_leave_durable_pass_gate(self) -> None:
        with tempfile.TemporaryDirectory() as tmp, patch(
            "recruit_crawler.scheduled._write_gate_output",
            side_effect=(True, True),
        ), patch(
            "recruit_crawler.scheduled.persist_scheduled_run",
            return_value="owned-pending-token",
        ), patch(
            "recruit_crawler.scheduled.finalize_scheduled_run",
            return_value=False,
        ):
            tmp_path = Path(tmp)
            config_path = self._write_scheduled_config(tmp_path)
            gate_path = tmp_path / "gate.json"
            result = run_scheduled_job(
                ScheduledRunRequest(
                    config=load_config(config_path, allow_real_sources=True),
                    run_date=date(2026, 6, 30),
                    quality_gate_output=gate_path,
                    db_path=tmp_path / "runs.sqlite3",
                )
            )
            persisted_gate = json.loads(gate_path.read_text(encoding="utf-8"))

        self.assertEqual(result.exit_code, 1)
        self.assertEqual(result.gate["status"], "fail")
        self.assertEqual(persisted_gate["status"], "fail")
        self.assertNotEqual(persisted_gate.get("status"), "pass")
        self.assertIn(
            "scheduled database operation failed",
            json.dumps(persisted_gate),
        )
    def test_finalize_exception_cannot_leave_durable_pass_gate(self) -> None:
        with tempfile.TemporaryDirectory() as tmp, patch(
            "recruit_crawler.scheduled.persist_scheduled_run",
            return_value="owned-pending-token",
        ), patch(
            "recruit_crawler.scheduled.finalize_scheduled_run",
            side_effect=OSError("finalization unavailable"),
        ):
            tmp_path = Path(tmp)
            config_path = self._write_scheduled_config(tmp_path)
            gate_path = tmp_path / "gate.json"
            result = run_scheduled_job(
                ScheduledRunRequest(
                    config=load_config(config_path, allow_real_sources=True),
                    run_date=date(2026, 6, 30),
                    quality_gate_output=gate_path,
                    db_path=tmp_path / "runs.sqlite3",
                )
            )
            persisted_gate = json.loads(gate_path.read_text(encoding="utf-8"))
            report_path = (
                tmp_path
                / "reports"
                / "recruiting-scheduled-run-2026-06-30.md"
            )

        self.assertEqual(result.exit_code, 1)
        self.assertEqual(result.gate["status"], "fail")
        self.assertEqual(persisted_gate["status"], "fail")
        self.assertFalse(report_path.exists())
        self.assertNotEqual(persisted_gate.get("status"), "pass")

    def test_failed_final_and_fallback_gate_writes_never_publish_pass(self) -> None:
        with tempfile.TemporaryDirectory() as tmp, patch(
            "recruit_crawler.scheduled._write_gate_output",
            side_effect=(True, False, False),
        ), patch(
            "recruit_crawler.scheduled._write_gate_payload",
            return_value=False,
        ):
            tmp_path = Path(tmp)
            config_path = self._write_scheduled_config(tmp_path)
            gate_path = tmp_path / "gate.json"
            result = run_scheduled_job(
                ScheduledRunRequest(
                    config=load_config(config_path, allow_real_sources=True),
                    run_date=date(2026, 6, 30),
                    quality_gate_output=gate_path,
                )
            )

        self.assertEqual(result.exit_code, 1)
        self.assertEqual(result.gate["status"], "fail")
        self.assertFalse(result.report_artifact.generated)
        self.assertFalse(
            gate_path.exists()
            and json.loads(gate_path.read_text(encoding="utf-8")).get("status")
            == "pass"
        )

    def test_post_replace_pass_gate_uncertainty_preserves_matching_dependencies(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            config_path = self._write_scheduled_config(tmp_path)
            gate_path = tmp_path / "gate.json"
            db_path = tmp_path / "runs.sqlite3"
            sync_calls = 0

            def uncertain_after_replace(path: Path) -> None:
                nonlocal sync_calls
                if path != gate_path.parent:
                    return
                sync_calls += 1
                if sync_calls >= 2:
                    raise OSError("directory durability uncertain")

            with patch(
                "recruit_crawler.scheduled._fsync_directory",
                side_effect=uncertain_after_replace,
            ):
                result = run_scheduled_job(
                    ScheduledRunRequest(
                        config=load_config(config_path, allow_real_sources=True),
                        run_date=date(2026, 6, 30),
                        quality_gate_output=gate_path,
                        db_path=db_path,
                    )
                )
            report_path = (
                tmp_path
                / "reports"
                / "recruiting-scheduled-run-2026-06-30.md"
            )

            self.assertEqual(result.exit_code, 1)
            self.assertEqual(result.gate["status"], "fail")
            self.assertTrue(report_path.exists())
            with sqlite3.connect(db_path) as connection:
                self.assertEqual(connection.execute("SELECT COUNT(*) FROM runs").fetchone()[0], 1)
                self.assertIsNone(
                    connection.execute(
                        "SELECT value FROM schema_metadata "
                        "WHERE key LIKE 'scheduled_run_pending:%'"
                    ).fetchone()
                )
            if gate_path.exists():
                persisted_gate = json.loads(gate_path.read_text(encoding="utf-8"))
                if persisted_gate.get("status") == "pass":
                    self.assertTrue(report_path.exists())
    def test_scheduled_service_db_failure_fails_gate_without_exposing_error(self) -> None:
        with tempfile.TemporaryDirectory() as tmp, patch(
            "recruit_crawler.scheduled.persist_scheduled_run",
            side_effect=OSError("PRIVATE_DB_CANARY"),
        ):
            tmp_path = Path(tmp)
            config_path = self._write_scheduled_config(tmp_path)
            result = run_scheduled_job(
                ScheduledRunRequest(
                    config=load_config(config_path, allow_real_sources=True),
                    run_date=date(2026, 6, 30),
                    quality_gate_output=tmp_path / "gate.json",
                    output_dir=tmp_path / "reports",
                    db_path=tmp_path / "recruit.sqlite3",
                )
            )

        self.assertEqual(result.exit_code, 1)
        self.assertIsInstance(result.result, PipelineResultV4)
        self.assertEqual(result.gate["schema_version"], 4)
        self.assertEqual(result.gate["status"], "fail")
        self.assertTrue(any(item["message"] == "scheduled database operation failed" for item in result.gate["findings"]))
        self.assertNotIn("PRIVATE_DB_CANARY", json.dumps(result.gate, ensure_ascii=False))
        self.assertFalse(result.report_artifact.generated)
        self.assertEqual(result.publication_durability, "not_published")
        self.assertIn("Report written: not generated", result.stdout_lines)

    def test_scheduled_gate_output_failure_rolls_back_report_before_db(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            config_path = self._write_scheduled_config(tmp_path)
            report_path = tmp_path / "reports" / "recruiting-scheduled-run-2026-06-30.md"
            with patch(
                "recruit_crawler.scheduled._write_gate_output",
                return_value=False,
            ), patch(
                "recruit_crawler.scheduled.persist_scheduled_run"
            ) as persist:
                result = run_scheduled_job(
                    ScheduledRunRequest(
                        config=load_config(config_path, allow_real_sources=True),
                        run_date=date(2026, 6, 30),
                        quality_gate_output=tmp_path / "gate.json",
                        db_path=tmp_path / "runs.sqlite3",
                    )
                )

        self.assertEqual(result.exit_code, 1)
        self.assertFalse(report_path.exists())
        persist.assert_not_called()
        self.assertEqual(result.gate["status"], "fail")
        self.assertIn("scheduled quality gate output failed", json.dumps(result.gate))
    def test_scheduled_failed_rollback_retains_artifact_and_indeterminate_durability(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            config_path = self._write_scheduled_config(tmp_path)
            with patch(
                "recruit_crawler.scheduled._write_gate_output",
                return_value=False,
            ), patch(
                "recruit_crawler.scheduled._rollback_report",
                return_value=False,
            ), patch(
                "recruit_crawler.scheduled.persist_scheduled_run"
            ) as persist:
                result = run_scheduled_job(
                    ScheduledRunRequest(
                        config=load_config(config_path, allow_real_sources=True),
                        run_date=date(2026, 6, 30),
                        quality_gate_output=tmp_path / "gate.json",
                        db_path=tmp_path / "runs.sqlite3",
                    )
                )

        self.assertEqual(result.exit_code, 1)
        self.assertFalse(result.report_artifact.generated)
        self.assertEqual(result.publication_durability, "not_published")
        self.assertEqual(result.gate["status"], "fail")
        self.assertIn("scheduled quality gate output failed", json.dumps(result.gate))
        self.assertIn("Report written: not generated", result.stdout_lines)
        persist.assert_not_called()
    def test_gate_post_replace_fsync_failure_restores_nonpass_provisional_gate(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            config_path = self._write_scheduled_config(tmp_path)
            gate_path = tmp_path / "gate.json"
            calls = 0

            def fail_final_gate_fsync(path: Path) -> None:
                nonlocal calls
                calls += 1
                if calls == 2:
                    raise OSError("directory sync uncertain")

            with patch(
                "recruit_crawler.scheduled._fsync_directory",
                side_effect=fail_final_gate_fsync,
            ):
                result = run_scheduled_job(
                    ScheduledRunRequest(
                        config=load_config(config_path, allow_real_sources=True),
                        run_date=date(2026, 6, 30),
                        quality_gate_output=gate_path,
                    )
                )

            persisted_gate = json.loads(gate_path.read_text(encoding="utf-8"))

        self.assertEqual(result.exit_code, 1)
        self.assertEqual(result.gate["status"], "fail")
        self.assertEqual(persisted_gate["status"], "fail")
        self.assertNotEqual(persisted_gate.get("status"), "pass")
        self.assertTrue(
            any(
                item["message"] == "scheduled quality gate output failed"
                for item in result.gate["findings"]
            )
        )
    def test_final_gate_promotion_failure_after_persistence_is_non_success(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            config_path = self._write_scheduled_config(tmp_path)
            gate_writes: list[dict[str, object]] = []

            def gate_write(path, gate, *, configured_canaries=()):
                gate_writes.append(dict(gate))
                return len(gate_writes) == 1

            with patch(
                "recruit_crawler.scheduled._write_gate_output",
                side_effect=gate_write,
            ) as write_gate, patch(
                "recruit_crawler.scheduled.persist_scheduled_run",
                return_value="owned-pending-token",
            ) as persist:
                result = run_scheduled_job(
                    ScheduledRunRequest(
                        config=load_config(config_path, allow_real_sources=True),
                        run_date=date(2026, 6, 30),
                        quality_gate_output=tmp_path / "gate.json",
                        db_path=tmp_path / "runs.sqlite3",
                    )
                )

        self.assertGreaterEqual(len(gate_writes), 2)
        self.assertEqual(persist.call_count, 1)
        self.assertEqual(result.exit_code, 1)
        self.assertEqual(result.gate["status"], "fail")
        self.assertTrue(
            any(
                finding["message"] == "scheduled quality gate output failed"
                for finding in result.gate["findings"]
            )
        )
    def test_final_gate_fsync_failure_never_persists_pass_data(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            config_path = self._write_scheduled_config(tmp_path)
            gate_path = tmp_path / "gate.json"
            db_path = tmp_path / "runs.sqlite3"
            calls = 0

            def fail_final_gate_fsync(path: Path) -> None:
                nonlocal calls
                calls += 1
                if calls == 2:
                    raise OSError("final gate directory sync uncertain")

            with patch(
                "recruit_crawler.scheduled._fsync_directory",
                side_effect=fail_final_gate_fsync,
            ):
                result = run_scheduled_job(
                    ScheduledRunRequest(
                        config=load_config(config_path, allow_real_sources=True),
                        run_date=date(2026, 6, 30),
                        quality_gate_output=gate_path,
                        db_path=db_path,
                    )
                )

            self.assertEqual(result.exit_code, 1)
            self.assertEqual(result.gate["status"], "fail")
            self.assertFalse(result.report_artifact.generated)
            self.assertTrue(db_path.exists())
            with sqlite3.connect(db_path) as connection:
                self.assertEqual(
                    connection.execute("SELECT COUNT(*) FROM runs").fetchone()[0], 0
                )
                self.assertEqual(
                    connection.execute("SELECT COUNT(*) FROM quality_gates").fetchone()[0], 0
                )
                self.assertEqual(
                    connection.execute("SELECT COUNT(*) FROM recommendations").fetchone()[0],
                    0,
                )
                self.assertIsNone(
                    connection.execute(
                        "SELECT value FROM schema_metadata WHERE key LIKE 'scheduled_run_pending:%'"
                    ).fetchone()
                )
            self.assertEqual(len(export_runs(db_path)), 0)
            self.assertEqual(len(export_recommendations(db_path)), 0)

    def test_exhausted_hard_deadline_recovery_is_indeterminate(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            config_path = self._write_scheduled_config(tmp_path)

            class Clock:
                def __call__(self) -> float:
                    return 100.0

            context = RuntimeContext(
                started_at=0.0,
                deadline=10.0,
                monotonic=Clock(),
            )
            request = ScheduledRunRequest(
                config=load_config(config_path, allow_real_sources=True),
                run_date=date(2026, 6, 30),
                quality_gate_output=tmp_path / "gate.json",
            )
            result = _recover_locked_scheduled_failure(
                request,
                _ScheduledRecoveryState(
                    tmp_path / "report.md",
                    (False, None),
                    report_publication_started=True,
                ),
                {"run_id": "run-1", "run_date": "2026-06-30"},
                (),
                runtime_context=context,
            )

        self.assertEqual(result.exit_code, 1)
        self.assertEqual(result.publication_durability, "indeterminate")
        self.assertFalse((tmp_path / "gate.json").exists())

    def test_gate_writer_sanitizes_private_profile_military_and_canary_values(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            gate_path = Path(tmp) / "gate.json"
            gate = {
                "status": "pass",
                "raw_jd": "private profile details",
                "military_detail": "군필",
                "source_id": "violet-lattice-731",
            }
            outcome = _write_gate_output(
                gate_path,
                gate,
                configured_canaries=("violet-lattice-731",),
            )
            payload = json.loads(gate_path.read_text(encoding="utf-8"))

        self.assertEqual(outcome.status, "written")
        self.assertEqual(payload["status"], "fail")
        self.assertNotIn("raw_jd", json.dumps(payload, ensure_ascii=False))
        self.assertNotIn("private", json.dumps(payload, ensure_ascii=False).casefold())
        self.assertNotIn("military", json.dumps(payload, ensure_ascii=False).casefold())
        self.assertNotIn("군필", json.dumps(payload, ensure_ascii=False))
        self.assertNotIn("violet-lattice-731", json.dumps(payload, ensure_ascii=False))
    def test_scheduled_output_locks_are_sorted_and_unique_by_identity(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            report_path = tmp_path / "z-reports" / "report.md"
            gate_path = tmp_path / "a-gates" / "gate.json"
            events: list[tuple[str, Path]] = []

            @contextmanager
            def record_lock(path: Path):
                events.append(("enter", path))
                try:
                    yield
                finally:
                    events.append(("exit", path))

            with patch(
                "recruit_crawler.scheduled._report_advisory_lock",
                side_effect=record_lock,
            ):
                with _scheduled_output_locks(
                    (report_path, gate_path, report_path.resolve())
                ):
                    pass

            expected = sorted(
                {report_path.resolve(), gate_path.resolve()},
                key=lambda path: str(_report_lock_path(path)),
            )

        self.assertEqual(
            events,
            [
                *[("enter", path) for path in expected],
                *[("exit", path) for path in reversed(expected)],
            ],
        )

    def test_same_candidate_rerun_does_not_republish_or_ambiguously_rollback(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            config_path = self._write_scheduled_config(tmp_path)
            config = load_config(config_path, allow_real_sources=True)
            gate_path = tmp_path / "gate.json"
            first = run_scheduled_job(
                ScheduledRunRequest(
                    config=config,
                    run_date=date(2026, 6, 30),
                    quality_gate_output=gate_path,
                )
            )
            report_path = Path(first.report_artifact.path)
            report_before = report_path.read_bytes()
            with patch(
                "recruit_crawler.scheduled._publish_scheduled_report"
            ) as publish:
                second = run_scheduled_job(
                    ScheduledRunRequest(
                        config=config,
                        run_date=date(2026, 6, 30),
                        quality_gate_output=gate_path,
                    )
                )

            report_after = report_path.read_bytes()

        self.assertEqual(first.exit_code, 0)
        self.assertEqual(second.exit_code, 0)
        publish.assert_not_called()
        self.assertEqual(report_before, report_after)
        self.assertEqual(
            first.gate["report"]["content_sha256"],
            second.gate["report"]["content_sha256"],
        )
    def test_scheduled_run_rerun_reuses_stable_identity_and_artifacts(self) -> None:
        output = io.StringIO()
        with tempfile.TemporaryDirectory() as tmp, redirect_stdout(output):
            tmp_path = Path(tmp)
            config_path = self._write_scheduled_config(tmp_path)
            gate_path = tmp_path / "scheduled_quality_gate.json"
            output_dir = tmp_path / "scheduled_reports"
            argv = [
                "scheduled-run",
                "--config",
                str(config_path),
                "--run-date",
                "2026-06-30",
                "--output-dir",
                str(output_dir),
                "--quality-gate-output",
                str(gate_path),
            ]

            first_exit = cli_main(argv)
            first_gate = json.loads(gate_path.read_text(encoding="utf-8"))
            report_path = output_dir / "recruiting-scheduled-run-2026-06-30.md"
            first_report_mtime = report_path.stat().st_mtime_ns

            second_exit = cli_main(argv)
            second_gate = json.loads(gate_path.read_text(encoding="utf-8"))
            report_exists = report_path.exists()
            second_report_mtime = report_path.stat().st_mtime_ns

        self.assertEqual(first_exit, 0)
        self.assertEqual(second_exit, 0)
        self.assertEqual(first_gate, second_gate)
        self.assertEqual(first_gate["run_date"], "2026-06-30")
        self.assertEqual(first_gate["report"]["content_sha256"], second_gate["report"]["content_sha256"])
        self.assertTrue(report_exists)
        self.assertGreaterEqual(second_report_mtime, first_report_mtime)


if __name__ == "__main__":
    unittest.main()
