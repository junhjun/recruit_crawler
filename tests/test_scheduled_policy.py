from __future__ import annotations
from dataclasses import asdict

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

import io
import hashlib
import unicodedata
import json
import tempfile
import os
from contextlib import redirect_stdout
from datetime import date
from unittest.mock import patch

from recruit_crawler.cli import main as cli_main
from recruit_crawler.config import load_config
from recruit_crawler.scheduled import (
    ScheduledRunRequest,
    _rollback_report,
    _write_gate_output,
    _canary_safe_text,
    run_scheduled_job,
)
from recruit_crawler.projection import false_report_artifact
from recruit_crawler.report_writer import ReportPublicationResultV1
from recruit_crawler.schemas import REPORT_ARTIFACT_SCHEMA_VERSION, RenderedReportV2
from recruit_crawler.storage import (
    export_recommendations,
    export_runs,
    persist_scheduled_run as persist_storage,
)

CONFIG = ROOT / "config" / "sample_config.json"


class ScheduledPolicyTests(unittest.TestCase):
    def _write_scheduled_config(self, tmp_path: Path) -> Path:
        raw = json.loads(CONFIG.read_text(encoding="utf-8"))
        fixture_path = tmp_path / "postings.json"
        raw["fixture_path"] = str(fixture_path)
        raw["output_dir"] = str(tmp_path / "reports")
        fixture_path.write_text((ROOT / "fixtures" / "postings.json").read_text(encoding="utf-8"), encoding="utf-8")
        config_path = tmp_path / "scheduled_config.json"
        config_path.write_text(json.dumps(raw), encoding="utf-8")
        return config_path

    def test_scheduled_service_missing_context_blocks_without_cli(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            raw = json.loads(CONFIG.read_text(encoding="utf-8"))
            raw["fixture_path"] = str(tmp_path / "postings.json")
            raw["output_dir"] = str(tmp_path / "reports")
            raw["profile"]["skills"] = []
            raw["profile"]["max_experience_years"] = 0
            (tmp_path / "postings.json").write_text(
                (ROOT / "fixtures" / "postings.json").read_text(encoding="utf-8"),
                encoding="utf-8",
            )
            config_path = tmp_path / "missing_service_config.json"
            config_path.write_text(json.dumps(raw), encoding="utf-8")
            gate_path = tmp_path / "missing_service_gate.json"
            result = run_scheduled_job(
                ScheduledRunRequest(
                    config=load_config(config_path, allow_real_sources=True),
                    run_date=date(2026, 6, 30),
                    quality_gate_output=gate_path,
                )
            )
            gate = json.loads(gate_path.read_text(encoding="utf-8"))
            report_exists = (tmp_path / "reports" / "recruiting-scheduled-run-2026-06-30.md").exists()

        self.assertEqual(result.exit_code, 1)
        self.assertFalse(report_exists)
        self.assertFalse(result.report_artifact.generated)
        self.assertEqual(gate["status"], "fail")
        self.assertEqual(gate["context_status"], "needs_context")
        self.assertTrue(
            any(
                item["message"] == "required user context is missing"
                or "missing required user context" in item["message"]
                for item in gate["findings"]
            )
        )
        self.assertIn("Scheduled run blocked", result.stdout_lines)

    def test_scheduled_service_source_policy_blocks_without_cli(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            raw = json.loads(CONFIG.read_text(encoding="utf-8"))
            raw["output_dir"] = str(tmp_path / "reports")
            raw["sources"][0]["access_mode"] = "manual"
            raw["sources"][0]["options"] = {"manual_postings": [{"title": "Manual"}]}
            config_path = tmp_path / "manual_service_config.json"
            config_path.write_text(json.dumps(raw), encoding="utf-8")
            gate_path = tmp_path / "manual_service_gate.json"
            result = run_scheduled_job(
                ScheduledRunRequest(
                    config=load_config(config_path, allow_real_sources=True),
                    run_date=date(2026, 6, 30),
                    quality_gate_output=gate_path,
                )
            )
            gate = json.loads(gate_path.read_text(encoding="utf-8"))

        self.assertEqual(result.exit_code, 1)
        self.assertFalse(gate.get("report", {}).get("generated", False))
        self.assertEqual(gate["status"], "fail")
        self.assertTrue(
            any(
                "scheduled-run source policy rejected enabled source" in item["message"]
                or item["message"] == "scheduled source policy failed"
                for item in gate["findings"]
            )
        )

    def test_preflight_configured_canary_is_redacted_from_legacy_gate_fields(self) -> None:
        configured = "Café Secret"
        source_id = unicodedata.normalize("NFD", configured).upper()
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            raw = json.loads(CONFIG.read_text(encoding="utf-8"))
            raw["output_dir"] = str(tmp_path / "reports")
            raw["profile"]["private_canaries"] = [configured]
            raw["sources"][0]["source_id"] = source_id
            raw["sources"][0]["access_mode"] = "manual"
            raw["sources"][0]["options"] = {"manual_postings": [{"title": "Manual"}]}
            config_path = tmp_path / "preflight-canary.json"
            config_path.write_text(json.dumps(raw), encoding="utf-8")
            gate_path = tmp_path / "gate.json"
            result = run_scheduled_job(
                ScheduledRunRequest(
                    config=load_config(config_path, allow_real_sources=True),
                    run_date=date(2026, 6, 30),
                    quality_gate_output=gate_path,
                    db_path=tmp_path / f"{configured}.sqlite3",
                )
            )
            gate = json.loads(gate_path.read_text(encoding="utf-8"))
            public_gate = unicodedata.normalize("NFC", gate_path.read_text(encoding="utf-8")).casefold()

        self.assertEqual(result.exit_code, 1)
        self.assertEqual(gate["status"], "fail")
        self.assertEqual(gate["source_policy"][0]["source_id"], "unknown-source")
        self.assertEqual(gate["db_path"]["name"], "[redacted]")
        self.assertTrue(any(item["source_id"] == "unknown-source" for item in gate["findings"]))
        self.assertNotIn(unicodedata.normalize("NFC", configured).casefold(), public_gate)
    def test_scheduled_run_missing_context_is_noninteractive_quality_failure(self) -> None:
        output = io.StringIO()
        with tempfile.TemporaryDirectory() as tmp, redirect_stdout(output), patch(
            "builtins.input",
            side_effect=AssertionError("scheduled-run must not prompt"),
        ):
            tmp_path = Path(tmp)
            config_path = self._write_scheduled_config(tmp_path)
            context_path = tmp_path / "partial.md"
            context_path.write_text("Roles: Systems Engineer\nLocations: Seoul\n", encoding="utf-8")
            gate_path = tmp_path / "scheduled_quality_gate.json"
            report_path = tmp_path / "reports" / "recruiting-scheduled-run-2026-06-30.md"

            exit_code = cli_main(
                [
                    "scheduled-run",
                    "--config",
                    str(config_path),
                    "--run-date",
                    "2026-06-30",
                    "--context-doc",
                    str(context_path),
                    "--quality-gate-output",
                    str(gate_path),
                ]
            )
            gate = json.loads(gate_path.read_text(encoding="utf-8"))

        self.assertEqual(exit_code, 1)
        self.assertFalse(report_path.exists())
        self.assertFalse(gate.get("report", {}).get("generated", False))
        self.assertEqual(gate["status"], "fail")
        self.assertEqual(gate["context_status"], "needs_context")
        self.assertTrue(
            any(
                item["message"] == "required user context is missing"
                or "missing required user context" in item["message"]
                for item in gate["findings"]
            )
        )
        self.assertNotIn("Supplemental context interview", output.getvalue())
        self.assertIn("Scheduled run blocked", output.getvalue())
        self.assertIn("Report written: not generated", output.getvalue())

    def test_scheduled_run_rejects_manual_enabled_source_policy(self) -> None:
        output = io.StringIO()
        with tempfile.TemporaryDirectory() as tmp, redirect_stdout(output):
            tmp_path = Path(tmp)
            raw = json.loads(CONFIG.read_text(encoding="utf-8"))
            raw["output_dir"] = str(tmp_path / "reports")
            raw["sources"][0]["access_mode"] = "manual"
            raw["sources"][0]["options"] = {"manual_postings": [{"title": "Manual"}]}
            config_path = tmp_path / "manual_config.json"
            config_path.write_text(json.dumps(raw), encoding="utf-8")
            gate_path = tmp_path / "scheduled_quality_gate.json"

            exit_code = cli_main(
                [
                    "scheduled-run",
                    "--config",
                    str(config_path),
                    "--run-date",
                    "2026-06-30",
                    "--quality-gate-output",
                    str(gate_path),
                ]
            )
            gate = json.loads(gate_path.read_text(encoding="utf-8"))

        self.assertEqual(exit_code, 1)
        self.assertFalse(gate.get("report", {}).get("generated", False))
        self.assertEqual(gate["status"], "fail")
        self.assertIn("Scheduled run blocked", output.getvalue())
        self.assertTrue(
            any(
                "scheduled-run source policy rejected enabled source" in item["message"]
                or item["message"] == "scheduled source policy failed"
                for item in gate["findings"]
            )
        )

    def test_scheduled_run_network_preflight_blocks_before_collection(self) -> None:
        output = io.StringIO()
        with tempfile.TemporaryDirectory() as tmp, redirect_stdout(output), patch(
            "recruit_crawler.scheduled.run_scheduled_run",
            side_effect=AssertionError("collection must not start when DNS is unavailable"),
        ), patch(
            "recruit_crawler.scheduled._resolve_domain",
            side_effect=OSError("nodename nor servname provided, or not known"),
        ):
            tmp_path = Path(tmp)
            raw = json.loads(CONFIG.read_text(encoding="utf-8"))
            raw["output_dir"] = str(tmp_path / "reports")
            raw["sources"][0]["source_id"] = "saramin"
            raw["sources"][0]["access_mode"] = "public_page"
            raw["sources"][0]["target_status"] = "enabled"
            raw["sources"][0]["target_lane"] = "public_http"
            raw["sources"][0]["automation_level"] = "no_human"
            raw["sources"][0]["tos_review_status"] = "pass"
            raw["sources"][0]["domains"] = ["jobs.example.test"]
            raw["sources"][0]["adapter_code_path"] = "src/recruit_crawler/sources/base.py::FixtureAdapter"
            raw["sources"][0]["test_refs"] = [
                "tests/test_scheduled_policy.py::test_scheduled_run_network_preflight_blocks_before_collection"
            ]
            raw["sources"][0]["docs_refs"] = ["docs/source_collection_matrix.md"]
            raw["sources"][0]["options"] = {
                "acquisition_strategy": "detail_only",
                "outer_strategy_approval": "not_probed",
            }
            config_path = tmp_path / "network_config.json"
            config_path.write_text(json.dumps(raw), encoding="utf-8")
            gate_path = tmp_path / "scheduled_quality_gate.json"

            exit_code = cli_main(
                [
                    "scheduled-run",
                    "--config",
                    str(config_path),
                    "--run-date",
                    "2026-06-30",
                    "--quality-gate-output",
                    str(gate_path),
                ]
            )
            gate = json.loads(gate_path.read_text(encoding="utf-8"))

        self.assertEqual(exit_code, 1)
        self.assertFalse(gate.get("report", {}).get("generated", False))
        self.assertEqual(gate["status"], "fail")
        self.assertTrue(
            any(
                "scheduled-run network preflight failed" in item["message"]
                or item["message"] == "scheduled network preflight failed"
                for item in gate["findings"]
            )
        )
        self.assertIn("Scheduled run blocked", output.getvalue())
        self.assertNotIn("jobs.example.test", json.dumps(gate, ensure_ascii=False))
        self.assertNotIn(
            "nodename nor servname provided, or not known",
            json.dumps(gate, ensure_ascii=False),
        )
        self.assertEqual(
            next(
                item["message"]
                for item in gate["findings"]
                if item["source_id"] == "saramin"
            ),
            "scheduled network preflight failed",
        )

    def test_scheduled_run_private_context_uses_privacy_exit_code(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            config_path = self._write_scheduled_config(tmp_path)
            context_path = tmp_path / "private.md"
            context_path.write_text("Skills: Python\nPRIVATE_PROFILE_CANARY", encoding="utf-8")

            with self.assertRaises(SystemExit) as cm:
                cli_main(
                    [
                        "scheduled-run",
                        "--config",
                        str(config_path),
                        "--context-doc",
                        str(context_path),
                        "--quality-gate-output",
                        str(tmp_path / "scheduled_quality_gate.json"),
                    ]
                )

        self.assertEqual(cm.exception.code, 3)
    def test_scheduled_private_canary_stays_out_of_report_gate_envelope_and_storage(self) -> None:
        private_canary = "PRIVATE_PROFILE_CANARY"
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            config_path = self._write_scheduled_config(tmp_path)
            raw = json.loads(config_path.read_text(encoding="utf-8"))
            raw["profile"]["private_canaries"] = [private_canary]
            config_path.write_text(json.dumps(raw), encoding="utf-8")
            config = load_config(config_path, allow_real_sources=True)
            gate_path = tmp_path / "private-gate.json"
            db_path = tmp_path / "private.sqlite3"
            persisted: list[object] = []

            def persist_and_capture(path, envelope, *, configured_canaries=()) -> None:
                persisted.append(envelope)
                persist_storage(path, envelope, configured_canaries=configured_canaries)

            with patch(
                "recruit_crawler.scheduled.persist_scheduled_run",
                side_effect=persist_and_capture,
            ):
                result = run_scheduled_job(
                    ScheduledRunRequest(
                        config=config,
                        run_date=date(2026, 6, 30),
                        quality_gate_output=gate_path,
                        db_path=db_path,
                    )
                )

            report_text = Path(result.report_artifact.path).read_text(encoding="utf-8")
            gate_text = gate_path.read_text(encoding="utf-8")
            stored_text = json.dumps(
                {
                    "runs": export_runs(db_path),
                    "recommendations": export_recommendations(db_path),
                },
                ensure_ascii=False,
            )
            envelope_text = json.dumps(asdict(persisted[0]), default=str, ensure_ascii=False)

        self.assertEqual(result.exit_code, 0)
        self.assertEqual(result.gate["status"], "pass")
        self.assertEqual(len(persisted), 1)
        for surface in (report_text, gate_text, envelope_text, stored_text):
            self.assertNotIn(private_canary, surface)
    def test_reserved_canary_collisions_never_escape_public_gate(self) -> None:
        for private_canary in (
            "redacted",
            "unknown-source",
            "scheduled public gate sanitization failed",
        ):
            with self.subTest(private_canary=private_canary), tempfile.TemporaryDirectory() as tmp:
                tmp_path = Path(tmp)
                config_path = self._write_scheduled_config(tmp_path)
                raw = json.loads(config_path.read_text(encoding="utf-8"))
                raw["profile"]["private_canaries"] = [private_canary]
                raw["sources"][0]["access_mode"] = "manual"
                raw["sources"][0]["options"] = {"manual_postings": [{"title": "Manual"}]}
                config_path.write_text(json.dumps(raw), encoding="utf-8")
                gate_path = tmp_path / "gate.json"
                result = run_scheduled_job(
                    ScheduledRunRequest(
                        config=load_config(config_path, allow_real_sources=True),
                        run_date=date(2026, 6, 30),
                        quality_gate_output=gate_path,
                    )
                )
                gate_text = gate_path.read_text(encoding="utf-8")

            self.assertEqual(result.exit_code, 1)
            self.assertEqual(result.gate["status"], "fail")
            self.assertNotIn(private_canary.casefold(), gate_text.casefold())
            self.assertNotIn(private_canary.casefold(), json.dumps(result.gate).casefold())
    def test_canary_safe_text_exhaustion_is_bounded_and_nonmatching(self) -> None:
        matcher = tuple(chr(codepoint) for codepoint in range(0xE000, 0xF900))
        safe = _canary_safe_text(chr(0xE000), matcher)
        self.assertEqual(safe, "")
        self.assertFalse(any(canary in safe for canary in matcher))
    def test_scheduled_configured_canary_blocks_publication_before_writer(self) -> None:
        private_canary = "arbitrary-configured-value"
        content = f"# {private_canary}\n".encode("utf-8")
        rendered = RenderedReportV2(
            schema_version=REPORT_ARTIFACT_SCHEMA_VERSION,
            markdown_bytes=content,
            content_sha256=hashlib.sha256(content).hexdigest(),
            byte_length=len(content),
        )
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            config_path = self._write_scheduled_config(tmp_path)
            raw = json.loads(config_path.read_text(encoding="utf-8"))
            raw["profile"]["private_canaries"] = [private_canary]
            config_path.write_text(json.dumps(raw), encoding="utf-8")
            report_path = tmp_path / "reports" / "recruiting-scheduled-run-2026-06-30.md"
            gate_path = tmp_path / "configured-canary-gate.json"
            with patch(
                "recruit_crawler.scheduled.render_report_v2",
                return_value=rendered,
            ), patch(
                "recruit_crawler.scheduled.persist_rendered_report"
            ) as publish, patch(
                "recruit_crawler.scheduled.persist_scheduled_run"
            ) as persist:
                result = run_scheduled_job(
                    ScheduledRunRequest(
                        config=load_config(config_path, allow_real_sources=True),
                        run_date=date(2026, 6, 30),
                        quality_gate_output=gate_path,
                        db_path=tmp_path / "configured-canary.sqlite3",
                    )
                )

        self.assertEqual(result.exit_code, 1)
        self.assertEqual(result.gate["status"], "fail")
        self.assertFalse(result.report_artifact.generated)
        self.assertFalse(report_path.exists())
        publish.assert_not_called()
        persist.assert_not_called()




    def test_scheduled_publication_failure_is_sanitized_and_emits_no_report(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            config_path = self._write_scheduled_config(tmp_path)
            gate_path = tmp_path / "publication-failure-gate.json"
            publication_failure = ReportPublicationResultV1(
                false_report_artifact(),
                "write_failed_pre_replace",
                "not_published",
            )
            with patch(
                "recruit_crawler.scheduled.persist_rendered_report",
                return_value=publication_failure,
            ):
                result = run_scheduled_job(
                    ScheduledRunRequest(
                        config=load_config(config_path, allow_real_sources=True),
                        run_date=date(2026, 6, 30),
                        quality_gate_output=gate_path,
                    )
                )
            gate_text = gate_path.read_text(encoding="utf-8")

        self.assertEqual(result.exit_code, 1)
        self.assertFalse(result.report_artifact.generated)
        self.assertIsNone(result.report_artifact.path)
        self.assertIsNone(result.report_artifact.rendered)
        self.assertEqual(result.gate["status"], "fail")
        self.assertNotIn("write_failed_pre_replace", gate_text)
    def test_gate_write_pre_replace_failure_preserves_existing_gate(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            gate_path = Path(tmp) / "gate.json"
            gate_path.write_bytes(b'{"status":"pass"}')
            with patch(
                "recruit_crawler.scheduled.os.replace",
                side_effect=OSError("replace unavailable"),
            ):
                written = _write_gate_output(
                    gate_path,
                    {"status": "fail", "findings": []},
                )
            remaining = gate_path.read_bytes()

        self.assertFalse(written)
        self.assertEqual(remaining, b'{"status":"pass"}')

    def test_gate_write_post_replace_sync_failure_keeps_published_gate(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            gate_path = Path(tmp) / "gate.json"
            with patch(
                "recruit_crawler.scheduled._fsync_directory",
                side_effect=OSError("directory sync uncertain"),
            ):
                written = _write_gate_output(
                    gate_path,
                    {"status": "fail", "findings": []},
                )
            published = json.loads(gate_path.read_text(encoding="utf-8"))

        self.assertFalse(written)
        self.assertEqual(published["status"], "fail")
    def test_interrupted_gate_replace_is_not_reported_as_written(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            gate_path = Path(tmp) / "gate.json"
            original_replace = os.replace

            def replace_then_interrupt(source, destination):
                original_replace(source, destination)
                raise TimeoutError("interrupted after replace")

            with patch(
                "recruit_crawler.scheduled.os.replace",
                side_effect=replace_then_interrupt,
            ):
                outcome = _write_gate_output(
                    gate_path,
                    {"status": "pass", "findings": []},
                )

            self.assertFalse(outcome)
            self.assertEqual(outcome.status, "uncertain")
            self.assertIsNotNone(outcome.reconciliation)
            self.assertEqual(outcome.reconciliation.result, "published")
            self.assertEqual(
                outcome.reconciliation.observed_identity,
                outcome.reconciliation.candidate_identity,
            )
            self.assertEqual(
                json.loads(gate_path.read_text()),
                {"status": "pass", "findings": []},
            )

    def test_pre_replace_publication_failure_preserves_existing_report(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            config_path = self._write_scheduled_config(tmp_path)
            report_path = tmp_path / "reports" / "recruiting-scheduled-run-2026-06-30.md"
            report_path.parent.mkdir()
            report_path.write_bytes(b"previous report")
            publication_failure = ReportPublicationResultV1(
                false_report_artifact(),
                "write_failed_pre_replace",
                "not_published",
            )
            with patch(
                "recruit_crawler.scheduled.persist_rendered_report",
                return_value=publication_failure,
            ), patch("recruit_crawler.scheduled._rollback_report") as rollback:
                result = run_scheduled_job(
                    ScheduledRunRequest(
                        config=load_config(config_path, allow_real_sources=True),
                        run_date=date(2026, 6, 30),
                        quality_gate_output=tmp_path / "gate.json",
                    )
                )

            self.assertEqual(report_path.read_bytes(), b"previous report")

        rollback.assert_not_called()
        self.assertEqual(result.publication_durability, "not_published")
        self.assertFalse(result.report_artifact.generated)

    def test_existing_report_capture_failure_blocks_publication_and_persistence(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            config_path = self._write_scheduled_config(tmp_path)
            report_path = (
                tmp_path / "reports" / "recruiting-scheduled-run-2026-06-30.md"
            ).resolve()
            report_path.parent.mkdir()
            previous_content = b"previous report"
            report_path.write_bytes(previous_content)
            gate_path = tmp_path / "capture-failure-gate.json"
            with patch(
                "recruit_crawler.scheduled._capture_report",
                return_value=(True, None),
            ) as capture, patch(
                "recruit_crawler.scheduled._publish_scheduled_report"
            ) as publish, patch(
                "recruit_crawler.scheduled._rollback_report"
            ) as rollback, patch(
                "recruit_crawler.scheduled.persist_scheduled_run"
            ) as persist:
                result = run_scheduled_job(
                    ScheduledRunRequest(
                        config=load_config(config_path, allow_real_sources=True),
                        run_date=date(2026, 6, 30),
                        quality_gate_output=gate_path,
                        db_path=tmp_path / "runs.sqlite3",
                    )
                )
            remaining_content = report_path.read_bytes()
            gate = json.loads(gate_path.read_text(encoding="utf-8"))

        capture.assert_called_once_with(report_path)
        publish.assert_not_called()
        rollback.assert_not_called()
        persist.assert_not_called()
        self.assertEqual(remaining_content, previous_content)
        self.assertEqual(result.exit_code, 1)
        self.assertEqual(result.gate["status"], "fail")
        self.assertFalse(result.report_artifact.generated)
        self.assertTrue(
            any(
                item["message"] == "scheduled report capture failed"
                for item in gate["findings"]
            )
        )
    def test_rollback_directory_sync_failure_is_indeterminate(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            report_path = Path(tmp) / "report.md"
            report_path.write_bytes(b"published report")
            with patch(
                "recruit_crawler.scheduled._fsync_directory",
                side_effect=OSError("directory sync failed"),
            ):
                rollback_ok = _rollback_report(report_path, (True, b"previous report"))

            self.assertEqual(report_path.read_bytes(), b"previous report")

        self.assertFalse(rollback_ok)
    def test_rollback_report_does_not_overwrite_or_delete_concurrent_report(self) -> None:
        import hashlib

        for previous in ((False, None), (True, b"previous report")):
            with self.subTest(previous_exists=previous[0]), tempfile.TemporaryDirectory() as tmp:
                report_path = Path(tmp) / "report.md"
                report_path.write_bytes(b"concurrent report")
                candidate_identity = hashlib.sha256(b"published candidate").hexdigest()

                rollback_ok = _rollback_report(
                    report_path,
                    previous,
                    candidate_identity,
                )

                self.assertFalse(rollback_ok)
                self.assertEqual(report_path.read_bytes(), b"concurrent report")

    def test_scheduled_nonpublished_or_indeterminate_publication_skips_db(self) -> None:
        for failure_code, durability in (
            ("write_failed_pre_replace", "not_published"),
            ("fsync_failed_post_replace", "indeterminate"),
        ):
            with self.subTest(durability=durability), tempfile.TemporaryDirectory() as tmp:
                tmp_path = Path(tmp)
                config_path = self._write_scheduled_config(tmp_path)
                publication_failure = ReportPublicationResultV1(
                    false_report_artifact(), failure_code, durability
                )
                with patch(
                    "recruit_crawler.scheduled.persist_rendered_report",
                    return_value=publication_failure,
                ), patch(
                    "recruit_crawler.scheduled.persist_scheduled_run",
                    return_value="pending-token",
                ) as persist_db:
                    result = run_scheduled_job(
                        ScheduledRunRequest(
                            config=load_config(config_path, allow_real_sources=True),
                            run_date=date(2026, 6, 30),
                            quality_gate_output=tmp_path / "gate.json",
                            db_path=tmp_path / "runs.sqlite3",
                        )
                    )

            self.assertEqual(result.exit_code, 1)
            self.assertEqual(result.publication_failure_code, failure_code)
            self.assertEqual(result.publication_durability, durability)
            if durability == "indeterminate":
                self.assertIn("indeterminate", " ".join(result.stdout_lines))
                self.assertNotIn("Report written: not generated", result.stdout_lines)
            persist_db.assert_called_once()

    def test_scheduled_warning_is_non_success(self) -> None:
        with tempfile.TemporaryDirectory() as tmp, patch(
            "recruit_crawler.scheduled.persist_scheduled_run"
        ) as persist_db:
            tmp_path = Path(tmp)
            config_path = self._write_scheduled_config(tmp_path)
            with patch(
                "recruit_crawler.scheduled._scheduled_gate",
                return_value={"status": "warning", "context_status": "complete"},
            ):
                result = run_scheduled_job(
                    ScheduledRunRequest(
                        config=load_config(config_path, allow_real_sources=True),
                        run_date=date(2026, 6, 30),
                        quality_gate_output=tmp_path / "warning-gate.json",
                        db_path=tmp_path / "warning.sqlite3",
                    )
                )

        self.assertEqual(result.exit_code, 1)
        persist_db.assert_not_called()
        self.assertIn("Scheduled run blocked", result.stdout_lines)
        self.assertNotIn("Scheduled run complete", result.stdout_lines)
        self.assertIn("Report written: not generated", result.stdout_lines)

    def test_scheduled_fail_gate_does_not_persist_or_claim_success(self) -> None:
        with tempfile.TemporaryDirectory() as tmp, patch(
            "recruit_crawler.scheduled.persist_scheduled_run"
        ) as persist_db:
            tmp_path = Path(tmp)
            config_path = self._write_scheduled_config(tmp_path)
            with patch(
                "recruit_crawler.scheduled._scheduled_gate",
                return_value={"status": "fail", "context_status": "complete"},
            ):
                result = run_scheduled_job(
                    ScheduledRunRequest(
                        config=load_config(config_path, allow_real_sources=True),
                        run_date=date(2026, 6, 30),
                        quality_gate_output=tmp_path / "fail-gate.json",
                        db_path=tmp_path / "fail.sqlite3",
                    )
                )

        self.assertEqual(result.exit_code, 1)
        persist_db.assert_not_called()
        self.assertIn("Scheduled run blocked", result.stdout_lines)
        self.assertNotIn("Scheduled run complete", result.stdout_lines)
        self.assertIn("Report written: not generated", result.stdout_lines)
    def test_scheduled_nonpass_gate_with_db_path_never_creates_success_record(self) -> None:
        for status in ("warning", "fail"):
            with self.subTest(status=status), tempfile.TemporaryDirectory() as tmp:
                tmp_path = Path(tmp)
                config_path = self._write_scheduled_config(tmp_path)
                db_path = tmp_path / f"{status}.sqlite3"
                with patch(
                    "recruit_crawler.scheduled._scheduled_gate",
                    return_value={"status": status, "context_status": "complete"},
                ):
                    result = run_scheduled_job(
                        ScheduledRunRequest(
                            config=load_config(config_path, allow_real_sources=True),
                            run_date=date(2026, 6, 30),
                            quality_gate_output=tmp_path / f"{status}-gate.json",
                            db_path=db_path,
                        )
                    )

                self.assertEqual(result.exit_code, 1)
                self.assertFalse(db_path.exists())
                self.assertIn("Scheduled run blocked", result.stdout_lines)
                self.assertNotIn("Scheduled run complete", result.stdout_lines)

    def test_scheduled_candidate_gate_failure_never_publishes_or_persists(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            config_path = self._write_scheduled_config(tmp_path)
            with patch(
                "recruit_crawler.scheduled._scheduled_gate",
                return_value={"status": "warning", "context_status": "complete"},
            ), patch(
                "recruit_crawler.scheduled.persist_rendered_report"
            ) as publish, patch(
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
        publish.assert_not_called()
        persist.assert_not_called()
        self.assertEqual(result.publication_durability, "not_published")
        self.assertIn("Report written: not generated", result.stdout_lines)
    def test_scheduled_collection_failure_is_sanitized_and_does_not_mutate_outputs(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            config_path = self._write_scheduled_config(tmp_path)
            report_path = (
                tmp_path / "reports" / "recruiting-scheduled-run-2026-06-30.md"
            )
            report_path.parent.mkdir()
            previous_report = b"previous report"
            report_path.write_bytes(previous_report)
            gate_path = tmp_path / "collection-failure-gate.json"
            db_path = tmp_path / "runs.sqlite3"
            with patch(
                "recruit_crawler.scheduled.run_scheduled_run",
                side_effect=RuntimeError("private /tmp/collection-secret"),
            ), patch("recruit_crawler.scheduled.persist_scheduled_run") as persist:
                result = run_scheduled_job(
                    ScheduledRunRequest(
                        config=load_config(config_path, allow_real_sources=True),
                        run_date=date(2026, 6, 30),
                        quality_gate_output=gate_path,
                        db_path=db_path,
                    )
                )
            gate_text = gate_path.read_text(encoding="utf-8")
            remaining_report = report_path.read_bytes()

        self.assertEqual(result.exit_code, 1)
        self.assertIsNone(result.result)
        self.assertFalse(result.report_artifact.generated)
        self.assertEqual(remaining_report, previous_report)
        self.assertFalse(db_path.exists())
        persist.assert_not_called()
        self.assertEqual(result.gate["status"], "fail")
        self.assertTrue(
            any(
                item["message"] == "scheduled runtime failure"
                for item in result.gate["findings"]
            )
        )
        self.assertNotIn("collection-secret", gate_text)
        self.assertNotIn("/tmp/", gate_text)
    def test_scheduled_render_failure_has_no_exception_details(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            config_path = self._write_scheduled_config(tmp_path)
            gate_path = tmp_path / "render-failure-gate.json"
            with patch(
                "recruit_crawler.scheduled.render_report_v2",
                side_effect=RuntimeError("private /tmp/report-secret"),
            ):
                result = run_scheduled_job(
                    ScheduledRunRequest(
                        config=load_config(config_path, allow_real_sources=True),
                        run_date=date(2026, 6, 30),
                        quality_gate_output=gate_path,
                    )
                )
            gate_text = gate_path.read_text(encoding="utf-8")

        self.assertEqual(result.exit_code, 1)
        self.assertFalse(result.report_artifact.generated)
        self.assertNotIn("report-secret", gate_text)


if __name__ == "__main__":
    unittest.main()
