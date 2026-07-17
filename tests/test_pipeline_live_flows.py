from __future__ import annotations

import hashlib
import io
import json
import unicodedata
import sys
import tempfile
import unittest
from dataclasses import replace
from contextlib import redirect_stderr, redirect_stdout
from datetime import date
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from recruit_crawler.cli import main as cli_main
from recruit_crawler.config import ConfigError, load_config
from recruit_crawler.gate import build_gate_v2
from recruit_crawler.gate import build_gate_v4, canonical_gate_v4_bytes
from recruit_crawler.live_run import (
    _publish_live_report,
    _run_live_run_at_service_boundary,
)
from recruit_crawler.pipeline import (
    CollectionBatch,
    build_pipeline_result_v2,
    build_pipeline_result_v4,
    run_live_run,
)
from recruit_crawler.projection import project_pipeline_result
from recruit_crawler.projection import false_report_artifact
from recruit_crawler.report_writer import (
    ReportPublicationResultV1,
    RuntimeContext,
    persist_rendered_report,
)
from recruit_crawler.schemas import (
    PipelineResultV2,
    PipelineResultV4,
    PostingCandidate,
    RenderedReportV2,
    SourceExecutionOutcomeV1,
)
from recruit_crawler.summarizer import render_report_v2
from recruit_crawler.report_policy import MAX_REPORT_ROWS
from recruit_crawler.sources.http import SourceBudgetExceeded

CONFIG = ROOT / "config" / "sample_config.json"


class AdapterCollectionError(RuntimeError):
    pass


def _materialize(result: PipelineResultV2, config, slug: str = "live-flow"):
    projection = project_pipeline_result(result)
    rendered = render_report_v2(result)
    publication = persist_rendered_report(
        config.output_dir,
        result.run_date,
        rendered,
        report_slug=slug,
    )
    artifact = publication.artifact
    report = artifact.rendered.markdown_bytes.decode("utf-8") if artifact.rendered is not None else ""
    gate = build_gate_v2(
        result,
        enabled_source_ids=(source.source_id for source in config.sources if source.enabled),
        projection=projection,
        report_artifact=artifact,
    )
    return projection, report, publication, gate


class PipelineLiveFlowTests(unittest.TestCase):
    def test_live_service_passes_one_immutable_context_to_collection(self) -> None:
        class Clock:
            now = 100.0

            def __call__(self) -> float:
                return self.now

        clock = Clock()
        context = RuntimeContext.start(
            total_seconds=20,
            cleanup_seconds=5,
            command_mode="live-run",
            monotonic=clock,
        )
        received: list[RuntimeContext] = []
        config = load_config(CONFIG, allow_real_sources=True)
        collected = build_pipeline_result_v4(
            config,
            date(2026, 6, 30),
            (),
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
            command_mode="live-run",
        )

        def collect(config, run_date, *, coordinator, runtime_context):
            received.append(runtime_context)
            return collected

        with patch("recruit_crawler.live_run.run_live_run", side_effect=collect):
            result = _run_live_run_at_service_boundary(
                config,
                date(2026, 6, 30),
                coordinator=object(),
                runtime_context=context,
            )

        self.assertIs(result, collected)
        self.assertEqual(received, [context])
        self.assertEqual(context.normal_work_deadline, 115.0)
        self.assertEqual(context.hard_deadline, 120.0)
    def test_live_service_boundary_rejects_v2_result(self) -> None:
        config = load_config(CONFIG, allow_real_sources=True)
        legacy = build_pipeline_result_v2(
            config,
            date(2026, 6, 30),
            (),
            command_mode="live-run",
            sources_attempted=("fixture",),
        )
        with patch("recruit_crawler.live_run.run_live_run", return_value=legacy):
            with self.assertRaisesRegex(
                TypeError,
                "live service boundary requires PipelineResultV4",
            ):
                _run_live_run_at_service_boundary(
                    config,
                    date(2026, 6, 30),
                )
    def test_live_pipeline_clips_collection_budget_and_fails_closed(self) -> None:
        config = load_config(CONFIG, allow_real_sources=True)

        class Clock:
            now = 100.0

            def __call__(self) -> float:
                return self.now

        clock = Clock()
        context = RuntimeContext.start(
            total_seconds=20,
            cleanup_seconds=5,
            command_mode="live-run",
            monotonic=clock,
        )
        clock.now = 103.0
        with patch("recruit_crawler.pipeline.SourceCollectionCoordinator") as coordinator_type:
            coordinator_type.return_value.collect.return_value = CollectionBatch((), (), (), ())
            result = run_live_run(
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
                run_live_run(
                    config,
                    date(2026, 6, 30),
                    runtime_context=context,
                )
        coordinator_type.assert_not_called()

    def test_live_report_write_is_rejected_after_normal_deadline(self) -> None:
        class Clock:
            now = 10.0

            def __call__(self) -> float:
                return self.now

        clock = Clock()
        context = RuntimeContext.start(
            total_seconds=10,
            cleanup_seconds=2,
            monotonic=clock,
        )
        clock.now = 20.0
        rendered_bytes = b"# report\n"
        rendered = RenderedReportV2(
            schema_version=2,
            markdown_bytes=rendered_bytes,
            content_sha256=hashlib.sha256(rendered_bytes).hexdigest(),
            byte_length=len(rendered_bytes),
        )
        with tempfile.TemporaryDirectory() as tmp:
            publication = persist_rendered_report(
                Path(tmp),
                date(2026, 6, 30),
                rendered,
                report_slug="deadline",
                runtime_context=context,
            )
            self.assertEqual(publication.failure_code, "runtime_deadline_exceeded")
            self.assertFalse((Path(tmp) / "deadline-2026-06-30.md").exists())
    def test_live_v4_capacity_failure_skips_builders_and_publication(self) -> None:
        raw = json.loads(CONFIG.read_text(encoding="utf-8"))
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            raw["fixture_path"] = str(tmp_path / "postings.json")
            raw["output_dir"] = str(tmp_path / "reports")
            config_path = tmp_path / "config.json"
            config_path.write_text(json.dumps(raw), encoding="utf-8")
            (tmp_path / "postings.json").write_text("[]", encoding="utf-8")
            config = load_config(config_path, allow_real_sources=True)
            result = run_live_run(config, date(2026, 6, 30))
            actual_projection = project_pipeline_result(result)
            overflow_projection = {
                **actual_projection,
                "report_queue": tuple({} for _ in range(MAX_REPORT_ROWS + 1)),
            }
            with (
                patch(
                    "recruit_crawler.projection.project_pipeline_result",
                    return_value=overflow_projection,
                ),
                patch("recruit_crawler.summarizer._escape") as escape,
                patch("recruit_crawler.summarizer._row") as render_row,
            ):
                publication = _publish_live_report(config, result.run_date, result)

            gate = build_gate_v4(
                result,
                enabled_source_ids=("fixture",),
                projection=actual_projection,
                report_artifact=publication.artifact,
            )
            report_path = (
                Path(config.output_dir)
                / f"recruiting-live-run-{result.run_date.isoformat()}.md"
            )
            self.assertFalse(publication.artifact.generated)
            self.assertEqual(publication.failure_code, "render_failed")
            self.assertFalse(report_path.exists())
            self.assertEqual(gate["status"], "fail")
            escape.assert_not_called()
            render_row.assert_not_called()
    def test_live_run_does_not_publish_adapter_exception_details(self) -> None:
        class FailingAdapter:
            def collect(self) -> list[PostingCandidate]:
                raise AdapterCollectionError("PRIVATE_ADAPTER_FAILURE_DO_NOT_PUBLISH")

        raw = json.loads(CONFIG.read_text(encoding="utf-8"))
        raw["sources"][0]["failure_mode"] = "skip_source"
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            config_path = tmp_path / "config.json"
            raw["output_dir"] = str(tmp_path / "reports")
            config_path.write_text(json.dumps(raw), encoding="utf-8")
            config = load_config(config_path, allow_real_sources=True)
            with patch("recruit_crawler.pipeline.build_source_adapter", return_value=FailingAdapter()):
                result = run_live_run(config, date(2026, 6, 30))
            projection, report, publication, gate = _materialize(result, config, "source-failure")

        self.assertIsInstance(result, PipelineResultV4)
        self.assertEqual(result.source_metrics[0].error_codes, ("collection_failed",))
        self.assertEqual(projection["gate_sources"][0].error_codes, ("collection_failed",))
        self.assertNotIn("PRIVATE_ADAPTER_FAILURE_DO_NOT_PUBLISH", report)
        self.assertNotIn("PRIVATE_ADAPTER_FAILURE_DO_NOT_PUBLISH", json.dumps(gate, ensure_ascii=False))
        self.assertIsNone(publication.failure_code)
        self.assertEqual(publication.durability, "published")
        self.assertTrue(publication.artifact.generated)
        self.assertEqual(publication.artifact.rendered.content_sha256, gate["report"]["content_sha256"])

    def test_live_run_fail_run_redacts_adapter_exception_details(self) -> None:
        class FailingAdapter:
            def collect(self) -> list[PostingCandidate]:
                raise AdapterCollectionError("PRIVATE_FAIL_RUN_DETAIL_DO_NOT_PUBLISH")

        raw = json.loads(CONFIG.read_text(encoding="utf-8"))
        raw["sources"][0]["failure_mode"] = "fail_run"
        with tempfile.TemporaryDirectory() as tmp:
            config_path = Path(tmp) / "config.json"
            config_path.write_text(json.dumps(raw), encoding="utf-8")
            config = load_config(config_path, allow_real_sources=True)
            with patch("recruit_crawler.pipeline.build_source_adapter", return_value=FailingAdapter()):
                with self.assertRaisesRegex(ConfigError, "fixture: collection failed") as caught:
                    run_live_run(config, date(2026, 6, 30))

        self.assertNotIn("PRIVATE_FAIL_RUN_DETAIL_DO_NOT_PUBLISH", str(caught.exception))

    def test_live_config_rejects_company_careers_collection(self) -> None:
        raw = json.loads(CONFIG.read_text(encoding="utf-8"))
        raw["sources"] = [
            {
                "source_id": "company_careers",
                "enabled": True,
                "access_mode": "public_page",
                "auth_required": False,
                "tos_review_status": "pass",
                "domains": ["example.com"],
                "rate_limit": "1 request / second",
                "failure_mode": "skip_source",
                "allowed_persisted_fields": [],
                "options": {"start_urls": ["https://example.com/careers/ml"], "require_robots": False},
            }
        ]
        with tempfile.TemporaryDirectory() as tmp:
            config_path = Path(tmp) / "config.json"
            config_path.write_text(json.dumps(raw), encoding="utf-8")
            with self.assertRaisesRegex(ConfigError, "company-careers collection"):
                load_config(config_path, allow_real_sources=True)

    def test_live_run_records_source_level_candidate_metrics(self) -> None:
        raw = json.loads(CONFIG.read_text(encoding="utf-8"))
        postings = [
            {
                "source_id": "fixture",
                "source_url": "https://jobs.example.test/source-metric",
                "source_posting_id": "source-metric",
                "title": "ML Engineer",
                "company": "Metric Co",
                "location": "Seoul",
                "deadline": "2026-07-10",
                "raw_jd": {
                    "required_qualifications": ["Python", "machine learning"],
                    "preferred_qualifications": ["PyTorch"],
                    "responsibilities": ["Build ML systems"],
                    "company_info": ["AI team"],
                    "experience_tags": ["경력무관"],
                },
            }
        ]
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            config_path = tmp_path / "config.json"
            fixture_path = tmp_path / "postings.json"
            raw["fixture_path"] = str(fixture_path)
            raw["output_dir"] = str(tmp_path / "reports")
            config_path.write_text(json.dumps(raw), encoding="utf-8")
            fixture_path.write_text(json.dumps(postings), encoding="utf-8")
            config = load_config(config_path, allow_real_sources=True)
            result = run_live_run(config, date(2026, 6, 30))
            projection, _report, _publication, gate = _materialize(result, config, "metrics")

        self.assertEqual(len(result.source_metrics), 1)
        metric = result.source_metrics[0]
        self.assertEqual(metric.source_id, "fixture")
        self.assertTrue(metric.attempted)
        self.assertEqual(metric.accepted_count, 1)
        self.assertEqual(metric.error_codes, ())
        self.assertEqual(projection["gate_sources"][0].candidate_count, 1)
        self.assertEqual(gate["sources"][0]["candidate_count"], 1)

    def test_live_run_quality_gate_fails_enabled_source_with_zero_candidates(self) -> None:
        raw = json.loads(CONFIG.read_text(encoding="utf-8"))
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            config_path = tmp_path / "config.json"
            fixture_path = tmp_path / "postings.json"
            raw["fixture_path"] = str(fixture_path)
            raw["output_dir"] = str(tmp_path / "reports")
            config_path.write_text(json.dumps(raw), encoding="utf-8")
            fixture_path.write_text("[]", encoding="utf-8")
            config = load_config(config_path, allow_real_sources=True)
            result = run_live_run(config, date(2026, 6, 30))
            _projection, _report, _publication, gate = _materialize(result, config, "zero-candidates")

        self.assertEqual(gate["status"], "fail")
        self.assertEqual(gate["sources"][0]["source_id"], "fixture")
        self.assertEqual(gate["sources"][0]["candidate_count"], 0)
        self.assertTrue(any("enabled source accepted zero candidates" in finding["message"] for finding in gate["findings"]))

    def test_live_run_cli_writes_quality_gate_json(self) -> None:
        raw = json.loads(CONFIG.read_text(encoding="utf-8"))
        output = io.StringIO()
        with tempfile.TemporaryDirectory() as tmp, redirect_stdout(output):
            tmp_path = Path(tmp)
            config_path = tmp_path / "config.json"
            fixture_path = tmp_path / "postings.json"
            gate_path = tmp_path / "live_quality_gate.json"
            report_path = tmp_path / "reports" / "recruiting-live-run-2026-06-30.md"
            raw["fixture_path"] = str(fixture_path)
            raw["output_dir"] = str(tmp_path / "reports")
            config_path.write_text(json.dumps(raw), encoding="utf-8")
            fixture_path.write_text("[]", encoding="utf-8")

            exit_code = cli_main(
                [
                    "live-run",
                    "--config",
                    str(config_path),
                    "--run-date",
                    "2026-06-30",
                    "--quality-gate-output",
                    str(gate_path),
                ]
            )

            gate = json.loads(gate_path.read_text(encoding="utf-8"))
            report = report_path.read_text(encoding="utf-8")

            report_exists = report_path.exists()
        self.assertEqual(exit_code, 1)
        self.assertEqual(gate["status"], "fail")
        self.assertIn("Live-run quality gate status: fail", output.getvalue())
        self.assertTrue(report_exists)
        self.assertIn("## 수집 저하 안내", report)
        self.assertIn("소스 `fixture`", report)
        self.assertNotIn("PRIVATE_ADAPTER_EXCEPTION", report)
        self.assertEqual(
            gate["report"]["content_sha256"],
            hashlib.sha256(report.encode("utf-8")).hexdigest(),
        )

    def test_live_run_cli_publishes_partial_report_for_collection_error(self) -> None:
        class FailingAdapter:
            def collect(self) -> list[PostingCandidate]:
                raise AdapterCollectionError("PRIVATE_ADAPTER_EXCEPTION")

        raw = json.loads(CONFIG.read_text(encoding="utf-8"))
        raw["sources"][0]["failure_mode"] = "skip_source"
        output = io.StringIO()
        with tempfile.TemporaryDirectory() as tmp, redirect_stdout(output):
            tmp_path = Path(tmp)
            config_path = tmp_path / "config.json"
            gate_path = tmp_path / "live_quality_gate.json"
            raw["output_dir"] = str(tmp_path / "reports")
            config_path.write_text(json.dumps(raw), encoding="utf-8")
            with patch(
                "recruit_crawler.pipeline.build_source_adapter",
                return_value=FailingAdapter(),
            ):
                exit_code = cli_main(
                    [
                        "live-run",
                        "--config",
                        str(config_path),
                        "--run-date",
                        "2026-06-30",
                        "--quality-gate-output",
                        str(gate_path),
                    ]
                )
            gate = json.loads(gate_path.read_text(encoding="utf-8"))
            report_path = tmp_path / "reports" / "recruiting-live-run-2026-06-30.md"
            report = report_path.read_text(encoding="utf-8")

        self.assertEqual(exit_code, 1)
        self.assertEqual(gate["status"], "fail")
        self.assertIn("## 수집 저하 안내", report)
        self.assertIn("소스 `fixture`", report)
        self.assertNotIn("PRIVATE_ADAPTER_EXCEPTION", report)
        self.assertNotIn("PRIVATE_ADAPTER_EXCEPTION", json.dumps(gate, ensure_ascii=False))

    def test_live_run_cli_publishes_partial_report_for_source_timeout(self) -> None:
        class TimedOutAdapter:
            def collect(self) -> list[PostingCandidate]:
                raise SourceBudgetExceeded("source collection budget exhausted")

        raw = json.loads(CONFIG.read_text(encoding="utf-8"))
        raw["sources"][0]["failure_mode"] = "skip_source"
        output = io.StringIO()
        with tempfile.TemporaryDirectory() as tmp, redirect_stdout(output):
            tmp_path = Path(tmp)
            config_path = tmp_path / "config.json"
            gate_path = tmp_path / "live_quality_gate.json"
            raw["output_dir"] = str(tmp_path / "reports")
            config_path.write_text(json.dumps(raw), encoding="utf-8")
            with patch(
                "recruit_crawler.pipeline.build_source_adapter",
                return_value=TimedOutAdapter(),
            ):
                exit_code = cli_main(
                    [
                        "live-run",
                        "--config",
                        str(config_path),
                        "--run-date",
                        "2026-06-30",
                        "--quality-gate-output",
                        str(gate_path),
                    ]
                )
            gate = json.loads(gate_path.read_text(encoding="utf-8"))
            report_path = tmp_path / "reports" / "recruiting-live-run-2026-06-30.md"
            report = report_path.read_text(encoding="utf-8")

        self.assertEqual(exit_code, 1)
        self.assertEqual(gate["status"], "fail")
        self.assertTrue(gate["report"]["queue_parity"])
        self.assertTrue(canonical_gate_v4_bytes(gate))
        self.assertTrue(gate["report"]["generated"])
        self.assertEqual(gate["source_outcomes"][0]["status"], "source_timeout")
        self.assertIn("## 수집 저하 안내", report)
        self.assertIn("source_timeout", report)

    def test_live_gate_preserves_candidate_rejection_reason(self) -> None:
        raw = json.loads(CONFIG.read_text(encoding="utf-8"))
        output = io.StringIO()
        with tempfile.TemporaryDirectory() as tmp, redirect_stdout(output):
            tmp_path = Path(tmp)
            config_path = tmp_path / "config.json"
            gate_path = tmp_path / "live_quality_gate.json"
            raw["output_dir"] = str(tmp_path / "reports")
            config_path.write_text(json.dumps(raw), encoding="utf-8")

            with patch(
                "recruit_crawler.live_run._candidate_gate_allows_partial_publication",
                return_value=False,
            ):
                exit_code = cli_main(
                    [
                        "live-run",
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
        self.assertFalse(gate["report"]["generated"])
        self.assertIn(
            {
                "severity": "fail",
                "source_id": None,
                "message": "live report candidate failed validation",
            },
            gate["findings"],
        )
    def test_live_run_holds_one_year_over_profile_limit(self) -> None:
        raw = json.loads(CONFIG.read_text(encoding="utf-8"))
        raw["top_n"] = 5
        raw["profile"]["max_experience_years"] = 2
        raw["sources"] = [
            {
                "source_id": "fixture",
                "enabled": True,
                "access_mode": "fixture",
                "auth_required": False,
                "tos_review_status": "not_required",
                "domains": [],
                "rate_limit": "none",
                "failure_mode": "skip_source",
                "allowed_persisted_fields": [],
            }
        ]
        postings = [
            {
                "source_id": "fixture",
                "source_url": "https://jobs.example.test/new-grad",
                "source_posting_id": "new-grad",
                "title": "New Grad AI Engineer",
                "company": "Example AI",
                "location": "Seoul",
                "deadline": "2026-07-10",
                "raw_jd": {
                    "required_qualifications": ["Python", "machine learning"],
                    "preferred_qualifications": ["PyTorch"],
                    "responsibilities": ["Build ML systems"],
                    "company_info": ["AI team"],
                    "experience_tags": ["경력무관"],
                },
            },
            {
                "source_id": "fixture",
                "source_url": "https://jobs.example.test/one-year",
                "source_posting_id": "one-year",
                "title": "AI Engineer 1 Year",
                "company": "Example AI",
                "location": "Seoul",
                "deadline": "2026-07-10",
                "raw_jd": {
                    "required_qualifications": ["Python", "machine learning"],
                    "preferred_qualifications": ["PyTorch"],
                    "responsibilities": ["Build ML systems"],
                    "company_info": ["AI team"],
                    "experience_tags": ["경력1년↑"],
                },
            },
            {
                "source_id": "fixture",
                "source_url": "https://jobs.example.test/three-year",
                "source_posting_id": "three-year",
                "title": "AI Engineer 3 Years",
                "company": "Example AI",
                "location": "Seoul",
                "deadline": "2026-07-10",
                "raw_jd": {
                    "required_qualifications": ["Python", "machine learning"],
                    "preferred_qualifications": ["PyTorch"],
                    "responsibilities": ["Build ML systems"],
                    "company_info": ["AI team"],
                    "experience_tags": ["경력3년↑"],
                },
            },
        ]
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            config_path = tmp_path / "config.json"
            fixture_path = tmp_path / "postings.json"
            raw["fixture_path"] = str(fixture_path)
            raw["output_dir"] = str(tmp_path / "reports")
            config_path.write_text(json.dumps(raw), encoding="utf-8")
            fixture_path.write_text(json.dumps(postings), encoding="utf-8")
            config = load_config(config_path, allow_real_sources=True)
            result = run_live_run(config, date(2026, 6, 30))
            projection, report, publication, gate = _materialize(result, config, "experience")

        self.assertIsNone(publication.failure_code)
        self.assertEqual(publication.durability, "published")
        self.assertTrue(publication.artifact.generated)
        self.assertEqual(projection["summary"]["exclude"], 0)
        self.assertEqual(projection["summary"]["hold_total"], 1)
        self.assertEqual(projection["summary"]["manual_review_total"], 0)
        self.assertLessEqual(len(projection["action_queue"]), result.top_n)
        hold_items = [
            item for item in projection["action_queue"] if item["final_disposition"] == "hold"
        ]
        self.assertEqual([item["title"] for item in hold_items], ["AI Engineer 3 Years"])
        three_year = next(
            assessment
            for assessment in result.all_assessments
            if assessment.title == "AI Engineer 3 Years"
        )
        self.assertEqual(
            three_year.score,
            max(0, three_year.score_breakdown.raw_score - 10),
        )
        self.assertEqual(three_year.disposition, "hold")
        self.assertNotIn("manual_source", three_year.reason_codes)
        self.assertIn("New Grad AI Engineer", report)
        self.assertIn("AI Engineer 1 Year", report)
        self.assertIn("AI Engineer 3 Years", report)
        self.assertTrue(gate["report"]["queue_parity"])

    def test_partial_publication_failure_never_exposes_a_report(self) -> None:
        raw = json.loads(CONFIG.read_text(encoding="utf-8"))
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            config_path = tmp_path / "config.json"
            gate_path = tmp_path / "live_quality_gate.json"
            report_path = tmp_path / "reports" / "recruiting-live-run-2026-06-30.md"
            raw["fixture_path"] = str(tmp_path / "postings.json")
            raw["output_dir"] = str(tmp_path / "reports")
            config_path.write_text(json.dumps(raw), encoding="utf-8")
            (tmp_path / "postings.json").write_text("[]", encoding="utf-8")
            for durability, failure_code in (
                ("indeterminate", "fsync_failed_post_replace"),
                ("not_published", "write_failed_pre_replace"),
            ):
                with self.subTest(durability=durability), patch(
                    "recruit_crawler.live_run.persist_rendered_report",
                    return_value=ReportPublicationResultV1(
                        false_report_artifact(),
                        failure_code,
                        durability,
                    ),
                ):
                    output = io.StringIO()
                    with redirect_stdout(output), redirect_stderr(output):
                        exit_code = cli_main(
                            [
                                "live-run",
                                "--config",
                                str(config_path),
                                "--run-date",
                                "2026-06-30",
                                "--quality-gate-output",
                                str(gate_path),
                                "--print-report",
                            ]
                        )
                    gate = json.loads(gate_path.read_text(encoding="utf-8"))

                self.assertEqual(exit_code, 1)
                self.assertEqual(gate["status"], "fail")
                self.assertFalse(gate["report"]["generated"])
                self.assertFalse(report_path.exists())
                self.assertIn("Report written: not generated", output.getvalue())
                self.assertNotIn("# 채용 추천 리포트", output.getvalue())

    def test_indeterminate_replace_keeps_attributable_candidate_and_fails_gate(self) -> None:
        raw = json.loads(CONFIG.read_text(encoding="utf-8"))
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            config_path = tmp_path / "config.json"
            gate_path = tmp_path / "live_quality_gate.json"
            report_path = tmp_path / "reports" / "recruiting-live-run-2026-06-30.md"
            raw["fixture_path"] = str(tmp_path / "postings.json")
            raw["output_dir"] = str(tmp_path / "reports")
            config_path.write_text(json.dumps(raw), encoding="utf-8")
            (tmp_path / "postings.json").write_text("[]", encoding="utf-8")

            def leave_candidate(output_dir, run_date, rendered, **kwargs):
                report_path.parent.mkdir(parents=True, exist_ok=True)
                report_path.write_bytes(rendered.markdown_bytes)
                return ReportPublicationResultV1(
                    false_report_artifact(), "fsync_failed_post_replace", "indeterminate"
                )

            with patch(
                "recruit_crawler.live_run.persist_rendered_report",
                side_effect=leave_candidate,
            ), patch(
                "recruit_crawler.live_run._rollback_report",
                return_value=False,
            ):
                output = io.StringIO()
                with redirect_stdout(output), redirect_stderr(output):
                    exit_code = cli_main(
                        [
                            "live-run", "--config", str(config_path),
                            "--run-date", "2026-06-30",
                            "--quality-gate-output", str(gate_path),
                        ]
                    )
            gate = json.loads(gate_path.read_text(encoding="utf-8"))
            candidate_sha = hashlib.sha256(report_path.read_bytes()).hexdigest()

        self.assertEqual(exit_code, 1)
        self.assertEqual(gate["status"], "fail")
        self.assertTrue(gate["report"]["generated"])
        self.assertEqual(
            gate["report"]["content_sha256"],
            candidate_sha,
        )
        self.assertIn("publication state unknown", output.getvalue())
        self.assertIn("Report written: publication state unknown", output.getvalue())
    def test_partial_report_redacts_normalized_casefolded_canary(self) -> None:
        config = load_config(CONFIG)
        canary = "Café"
        normalized_canary = unicodedata.normalize("NFC", canary).casefold()
        base = build_pipeline_result_v4(
            config,
            date(2026, 6, 30),
            (),
            source_outcomes=(
                SourceExecutionOutcomeV1(
                    source_id="fixture",
                    attempted=True,
                    completed=True,
                    status="collection_error",
                    error_code="collection_error",
                    elapsed_ms=17,
                ),
            ),
            command_mode="live-run",
        )
        contaminated_source_id = "fixture-CAFE\u0301"
        contaminated_error_code = f"COLLECTION_FAILED_{canary.swapcase()}"

        for field in ("source_id", "collection_error_code"):
            with self.subTest(field=field):
                outcome = base.source_outcomes[0]
                metric = base.source_metrics[0]
                if field == "source_id":
                    outcome = replace(outcome, source_id=contaminated_source_id)
                    metric = replace(
                        metric,
                        source_id=contaminated_source_id,
                        outcome=outcome,
                    )
                    enabled_source_ids = (contaminated_source_id,)
                else:
                    metric = replace(
                        metric,
                        error_codes=("collection_error", contaminated_error_code),
                    )
                    enabled_source_ids = ("fixture",)
                result = replace(
                    base,
                    source_metrics=(metric,),
                    source_outcomes=(outcome,),
                )
                rendered = render_report_v2(result, private_canaries=(canary,))
                with tempfile.TemporaryDirectory() as tmp:
                    publication = persist_rendered_report(
                        Path(tmp),
                        result.run_date,
                        rendered,
                        report_slug="canary-partial",
                        private_canaries=(canary,),
                    )
                    gate = build_gate_v4(
                        result,
                        enabled_source_ids=enabled_source_ids,
                        configured_canaries=(canary,),
                        report_artifact=publication.artifact,
                    )
                    report = publication.artifact.rendered.markdown_bytes.decode("utf-8")

                public_payload = json.dumps(gate, ensure_ascii=False)
                self.assertTrue(publication.artifact.generated)
                self.assertIn("## 수집 저하 안내", report)
                self.assertNotIn(normalized_canary, unicodedata.normalize("NFC", report).casefold())
                self.assertNotIn(
                    normalized_canary,
                    unicodedata.normalize("NFC", public_payload).casefold(),
                )

    def test_published_partial_report_binds_gate_hash_axes_and_public_timing(self) -> None:
        class FailingAdapter:
            def collect(self) -> list[PostingCandidate]:
                raise AdapterCollectionError("PRIVATE_PARTIAL_FAILURE")

        raw = json.loads(CONFIG.read_text(encoding="utf-8"))
        raw["sources"][0]["failure_mode"] = "skip_source"
        output = io.StringIO()
        with tempfile.TemporaryDirectory() as tmp, redirect_stdout(output):
            tmp_path = Path(tmp)
            config_path = tmp_path / "config.json"
            gate_path = tmp_path / "live_quality_gate.json"
            raw["output_dir"] = str(tmp_path / "reports")
            config_path.write_text(json.dumps(raw), encoding="utf-8")
            with patch(
                "recruit_crawler.pipeline.build_source_adapter",
                return_value=FailingAdapter(),
            ):
                exit_code = cli_main(
                    [
                        "live-run",
                        "--config",
                        str(config_path),
                        "--run-date",
                        "2026-06-30",
                        "--quality-gate-output",
                        str(gate_path),
                    ]
                )
            gate = json.loads(gate_path.read_text(encoding="utf-8"))
            report_path = tmp_path / "reports" / "recruiting-live-run-2026-06-30.md"
            report_bytes = report_path.read_bytes()

        self.assertEqual(exit_code, 1)
        self.assertEqual(gate["status"], "fail")
        self.assertTrue(gate["report"]["generated"])
        self.assertEqual(
            gate["report"]["content_sha256"],
            hashlib.sha256(report_bytes).hexdigest(),
        )
        self.assertEqual(gate["report"]["byte_length"], len(report_bytes))
        source_ids = [row["source_id"] for row in gate["sources"]]
        outcome_ids = [row["source_id"] for row in gate["source_outcomes"]]
        self.assertEqual(source_ids, outcome_ids)
        self.assertTrue(source_ids)
        self.assertTrue(all(row["duration_ms"] == 0 for row in gate["sources"]))
        self.assertTrue(all(row["elapsed_ms"] == 0 for row in gate["source_outcomes"]))

    def test_live_gate_output_failure_rolls_back_partial_report(self) -> None:
        raw = json.loads(CONFIG.read_text(encoding="utf-8"))
        output = io.StringIO()
        with tempfile.TemporaryDirectory() as tmp, redirect_stdout(output):
            tmp_path = Path(tmp)
            config_path = tmp_path / "config.json"
            gate_path = tmp_path / "live_quality_gate.json"
            report_path = tmp_path / "reports" / "recruiting-live-run-2026-06-30.md"
            raw["fixture_path"] = str(tmp_path / "postings.json")
            raw["output_dir"] = str(tmp_path / "reports")
            config_path.write_text(json.dumps(raw), encoding="utf-8")
            (tmp_path / "postings.json").write_text("[]", encoding="utf-8")
            failed_gate_write = type("Outcome", (), {"successful": False})()
            with patch(
                "recruit_crawler.live_run._write_gate_output_at_service_boundary",
                return_value=failed_gate_write,
            ):
                exit_code = cli_main(
                    [
                        "live-run",
                        "--config",
                        str(config_path),
                        "--run-date",
                        "2026-06-30",
                        "--quality-gate-output",
                        str(gate_path),
                    ]
                )
            report_exists = report_path.exists()
            gate_exists = gate_path.exists()
        self.assertEqual(exit_code, 1)
        self.assertFalse(report_exists)
        self.assertFalse(gate_exists)
        self.assertIn("Report written: not generated", output.getvalue())

    def test_live_gate_failure_reports_unknown_when_rollback_is_unconfirmed(self) -> None:
        raw = json.loads(CONFIG.read_text(encoding="utf-8"))
        output = io.StringIO()
        with tempfile.TemporaryDirectory() as tmp, redirect_stdout(output):
            tmp_path = Path(tmp)
            config_path = tmp_path / "config.json"
            gate_path = tmp_path / "live_quality_gate.json"
            report_path = tmp_path / "reports" / "recruiting-live-run-2026-06-30.md"
            raw["fixture_path"] = str(tmp_path / "postings.json")
            raw["output_dir"] = str(tmp_path / "reports")
            config_path.write_text(json.dumps(raw), encoding="utf-8")
            (tmp_path / "postings.json").write_text("[]", encoding="utf-8")
            failed_gate_write = type("Outcome", (), {"successful": False})()
            with (
                patch(
                    "recruit_crawler.live_run._write_gate_output_at_service_boundary",
                    return_value=failed_gate_write,
                ),
                patch("recruit_crawler.live_run._rollback_report", return_value=False),
            ):
                exit_code = cli_main(
                    [
                        "live-run",
                        "--config",
                        str(config_path),
                        "--run-date",
                        "2026-06-30",
                        "--quality-gate-output",
                        str(gate_path),
                    ]
                )
            report_exists = report_path.exists()
            gate_exists = gate_path.exists()
        self.assertEqual(exit_code, 1)
        self.assertTrue(report_exists)
        self.assertFalse(gate_exists)
        self.assertIn("Report written: publication state unknown", output.getvalue())
if __name__ == "__main__":
    unittest.main()
