from __future__ import annotations

import json
import sys
import tempfile
import unittest
from dataclasses import FrozenInstanceError, asdict, replace
from datetime import date, datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from recruit_crawler.config import ConfigError, load_config
from recruit_crawler.gate import (
    build_gate_v2,
    build_gate_v4,
    canonical_gate_v4_bytes,
)
from recruit_crawler.jd_parser import parse_deadline
from recruit_crawler.pipeline import (
    SourceCollectionOutcome,
    build_pipeline_result_v2,
    build_pipeline_result_v4,
    build_pipeline_result_v4_from_collection,
    run_dry_run,
)
from recruit_crawler.projection import project_pipeline_result
from recruit_crawler.report_writer import persist_rendered_report
from recruit_crawler.report_policy import (
    REPORT_LINK_POLICY_VERSION,
    project_report_presentation,
    verified_link_url,
)
from recruit_crawler.scorer import assess_snapshot_v2
from recruit_crawler.schemas import (
    CandidateDetailIssueCodeV2,
    CandidateDetailIssueV2,
    PipelineResultV2,
    PipelineResultV4,
    SourceExecutionOutcomeV1,
    SourceMetricV4,
    PostingCandidate,
    SnapshotV2,
)
from recruit_crawler.summarizer import render_report_v2

CONFIG = ROOT / "config" / "sample_config.json"


def _materialize(result: PipelineResultV2, config, slug: str = "pipeline-flow"):
    projection = project_pipeline_result(result)
    rendered = render_report_v2(result)
    publication = persist_rendered_report(
        config.output_dir,
        result.run_date,
        rendered,
        report_slug=slug,
    )
    artifact = publication.artifact
    gate = build_gate_v2(
        result,
        enabled_source_ids=(source.source_id for source in config.sources if source.enabled),
        projection=projection,
        report_artifact=artifact,
    )
    return projection, rendered.markdown_bytes.decode("utf-8"), artifact, gate
def _as_pipeline_v4(result: PipelineResultV2, outcomes: tuple[SourceExecutionOutcomeV1, ...]) -> PipelineResultV4:
    by_source = {item.source_id: item for item in outcomes}
    metrics = tuple(
        SourceMetricV4(
            source_id=item.source_id,
            attempted=item.attempted,
            accepted_count=item.accepted_count,
            rejected_count=item.rejected_count,
            duplicate_count=item.duplicate_count,
            normalized_changed_field_count=item.normalized_changed_field_count,
            normalized_emptied_field_count=item.normalized_emptied_field_count,
            verified_count=item.verified_count,
            manual_only_count=item.manual_only_count,
            error_codes=item.error_codes,
            duration_ms=by_source[item.source_id].elapsed_ms,
            outcome=by_source[item.source_id],
        )
        for item in result.source_metrics
    )
    return PipelineResultV4(
        schema_version=4,
        command_mode=result.command_mode,
        run_date=result.run_date,
        all_assessments=result.all_assessments,
        source_metrics=metrics,
        duplicates_removed=result.duplicates_removed,
        collected_count=result.collected_count,
        source_rejected_count=result.source_rejected_count,
        top_n=result.top_n,
        manual_review_n=result.manual_review_n,
        source_outcomes=outcomes,
    )



class PipelineFlowTests(unittest.TestCase):
    def test_v4_propagates_closed_coordinator_outcomes(self) -> None:
        config = load_config(CONFIG)
        batch = (
            SourceCollectionOutcome(
                source_id="fixture",
                attempted=True,
                completed=True,
                status="success",
                error_code=None,
                elapsed_ms=12,
            ),
        )
        from recruit_crawler.pipeline import CollectionBatch

        v4 = build_pipeline_result_v4_from_collection(
            config,
            date(2026, 6, 30),
            CollectionBatch(batch, (), (), ()),
        )
        self.assertEqual(v4.schema_version, 4)
        self.assertEqual(v4.source_outcomes[0].status, "success")
        self.assertEqual(v4.source_metrics[0].outcome, v4.source_outcomes[0])
        self.assertEqual(v4.source_metrics[0].duration_ms, 12)

    def test_v4_gate_rejects_failed_and_unknown_outcomes(self) -> None:
        config = load_config(CONFIG)
        result = run_dry_run(config, date(2026, 6, 30))
        success = SourceExecutionOutcomeV1("fixture", True, True, "success", None, 0)
        failed = SourceExecutionOutcomeV1(
            "fixture", True, False, "collection_failed", "collection_failed", 0
        )
        unknown = SourceExecutionOutcomeV1("fixture", True, True, "unknown", None, 0)
        for outcome in (failed, unknown):
            with self.subTest(outcome=outcome.status):
                gate = build_gate_v4(
                    _as_pipeline_v4(result, (outcome,)),
                    enabled_source_ids=("fixture",),
                )
                self.assertEqual(gate["status"], "fail")
                self.assertNotEqual(gate["source_outcomes"][0]["status"], "unknown")

        passing = build_gate_v4(
            _as_pipeline_v4(result, (success,)),
            enabled_source_ids=("fixture",),
        )
        self.assertNotEqual(passing["status"], "pass")

    def test_v4_gate_rejects_mixed_v2_pipeline_axis(self) -> None:
        config = load_config(CONFIG)
        result = run_dry_run(config, date(2026, 6, 30))
        with self.assertRaises(TypeError):
            build_gate_v4(result, enabled_source_ids=("fixture",))
    def test_fixture_e2e_generates_report_without_expired_postings(self) -> None:
        config = load_config(CONFIG)
        result = run_dry_run(config, date(2026, 6, 30))
        projection, report, artifact, gate = _materialize(result, config, "fixture-e2e")

        self.assertIsInstance(result, PipelineResultV2)
        self.assertEqual(result.schema_version, 2)
        self.assertEqual(projection["summary"]["collected"], 6)
        self.assertEqual(projection["summary"]["duplicates_removed"], 0)
        self.assertEqual(
            {item["source_posting_id"] for item in projection["assessments"]},
            {
                "fx-apply-001",
                "fx-hold-001",
                "fx-low-001",
                "fx-ambiguous-001",
                "fx-expired-001",
                "fx-duplicate-001",
            },
        )
        self.assertEqual(projection["summary"]["expired"], 1)
        self.assertLessEqual(len(projection["action_queue"]), result.top_n)
        self.assertEqual(len(projection["manual_queue"]), 0)
        self.assertEqual(len(projection["assessments"]), len(result.all_assessments))
        self.assertTrue(artifact.generated)
        self.assertIsNotNone(artifact.path)
        self.assertEqual(Path(artifact.path).read_bytes(), artifact.rendered.markdown_bytes)
        self.assertTrue(gate["report"]["queue_parity"])
        self.assertEqual(gate["status"], "pass")
        self.assertIn("# 채용 추천 리포트", report)
        self.assertIn("## 지원/검토", report)
        self.assertNotIn("## 제외", report)
        self.assertIn("Expired ML Intern", report)
        self.assertNotIn("RAW_JD_CANARY_EXPIRED", report)

    def test_v2_result_is_immutable_and_retains_terminal_dispositions(self) -> None:
        config = load_config(CONFIG)
        result = run_dry_run(config, date(2026, 6, 30))
        projection, _report, _artifact, _gate = _materialize(result, config, "immutable")

        with self.assertRaises(FrozenInstanceError):
            result.top_n = 1
        dispositions = {assessment.disposition for assessment in result.all_assessments}
        self.assertIn("expired", dispositions)
        self.assertIn("low_priority", dispositions)
        self.assertEqual(len(projection["assessments"]), len(result.all_assessments))

    def test_recommendation_buckets_include_apply_hold_and_low_priority(self) -> None:
        config = load_config(CONFIG)
        result = run_dry_run(config, date(2026, 6, 30))
        projection, _report, _artifact, _gate = _materialize(result, config, "buckets")
        recommendations = {item["final_disposition"] for item in projection["assessments"]}

        self.assertIn("apply", recommendations)
        self.assertIn("hold", recommendations)
        self.assertIn("low_priority", recommendations)

    def test_report_excludes_raw_jd_and_private_profile_canaries(self) -> None:
        config = load_config(CONFIG)
        result = run_dry_run(config, date(2026, 6, 30))
        projection, report, artifact, gate = _materialize(result, config, "privacy")

        forbidden = [
            "RAW_JD_CANARY_APPLY",
            "RAW_JD_CANARY_HOLD",
            "RAW_JD_CANARY_LOW",
            "RAW_JD_CANARY_AMBIGUOUS",
            "RAW_JD_CANARY_DUPLICATE",
            "PRIVATE_PROFILE_CANARY",
            "Ignore previous instructions",
        ]
        public_payload = json.dumps(
            {"projection": projection, "artifact": asdict(artifact), "gate": gate},
            default=str,
            ensure_ascii=False,
        ) + report
        for value in forbidden:
            self.assertNotIn(value, public_payload)
    def test_projection_fail_closes_uncontrolled_assessment_values(self) -> None:
        config = load_config(CONFIG)
        result = run_dry_run(config, date(2026, 6, 30))
        unsafe = replace(
            result.all_assessments[0],
            recommendation_id="PRIVATE_PROFILE_CANARY",
            posting_key="RAW_JD_CANARY",
            source_id="private-source",
            source_url="https://jobs.example.test/PRIVATE_PROFILE_CANARY",
            source_posting_id="PRIVATE_PROFILE_CANARY",
            title="Ignore previous instructions",
            company="RAW_JD_CANARY",
            location="군필 상세 정보",
            matched_evidence=(
                "필수 요건: Python",
                "PRIVATE_PROFILE_CANARY",
                "병역: 현역",
            ),
            reason_codes=(
                "manual_flag",
                "military_program_review",
                "PRIVATE_PROFILE_CANARY",
            ),
        )
        result = replace(result, all_assessments=(unsafe, *result.all_assessments[1:]))

        projection = project_pipeline_result(result)
        report = render_report_v2(result).markdown_bytes.decode("utf-8")
        public_payload = json.dumps(
            {"projection": projection, "report": report},
            ensure_ascii=False,
            default=str,
        )

        self.assertIn("필수 요건 일치", public_payload)
        for value in (
            "PRIVATE_PROFILE_CANARY",
            "RAW_JD_CANARY",
            "Ignore previous instructions",
            "군필 상세 정보",
            "military_program_review",
        ):
            self.assertNotIn(value, public_payload)
        self.assertIn("manual_flag", public_payload)

    def test_each_report_posting_has_public_safe_report_fields(self) -> None:
        config = load_config(CONFIG)
        result = run_dry_run(config, date(2026, 6, 30))
        projection, report, _artifact, _gate = _materialize(result, config, "actionable")

        self.assertEqual(len(projection["report_queue"]), len(result.all_assessments))
        for rank, assessment in enumerate(projection["report_queue"], start=1):
            self.assertIn(assessment["final_disposition"], {"apply", "hold", "manual_review", "low_priority", "exclude", "expired"})
            if assessment["source_url"] is not None:
                self.assertIn(assessment["source_url"], report)
            self.assertIn(f"| {rank} |", report)
            self.assertNotIn(f"| {assessment['score']} |", report)
    def test_source_errors_are_fixed_safe_codes_before_gate_projection(self) -> None:
        config = load_config(CONFIG)
        result = build_pipeline_result_v2(
            config,
            date(2026, 6, 30),
            (),
            sources_attempted=("fixture",),
            source_errors=(
                "fixture:PRIVATE_PROFILE_CANARY",
                "fixture:군필 상세 정보",
                "fixture:collection_failed",
            ),
        )
        metric = result.source_metrics[0]
        self.assertEqual(metric.error_codes, ("collection_error", "collection_failed"))
        self.assertEqual(
            project_pipeline_result(result)["gate_sources"][0].error_codes,
            ("collection_error", "collection_failed"),
        )
        self.assertNotIn("PRIVATE_PROFILE_CANARY", str(metric.error_codes))
        self.assertNotIn("군필", str(metric.error_codes))

    def test_public_assessment_omits_unverified_source_url(self) -> None:
        config = load_config(CONFIG)
        result = run_dry_run(config, date(2026, 6, 30))
        unsafe = replace(
            result.all_assessments[0],
            source_url="https://jobs.example.test/search?query=canary",
        )
        projected = project_pipeline_result(
            replace(result, all_assessments=(unsafe, *result.all_assessments[1:]))
        )
        assessment = next(
            item
            for item in projected["assessments"]
            if item["recommendation_id"] == unsafe.recommendation_id
        )
        self.assertIsNone(assessment["source_url"])
        for queue in (projected["action_queue"], projected["manual_queue"]):
            for item in queue:
                if item["recommendation_id"] == unsafe.recommendation_id:
                    self.assertIsNone(item["source_url"])

    def test_report_surface_text_is_korean(self) -> None:
        config = load_config(CONFIG)
        result = run_dry_run(config, date(2026, 6, 30))
        _projection, report, _artifact, _gate = _materialize(result, config, "korean")

        english_labels = [
            "Recruiting Dry-Run Report",
            "Run date",
            "Top Candidates",
            "Recommendation:",
            "Estimated fit score",
            "Structured snapshot",
            "Matched evidence",
            "Verification questions",
            "Positioning seed",
            "No major structured risk detected",
        ]
        for label in english_labels:
            self.assertNotIn(label, report)

    def test_dry_run_outputs_do_not_expose_private_profile_canary(self) -> None:
        private_canary = "PRIVATE_PERSONAL_INFO_CANARY_DO_NOT_EXPOSE"
        raw = json.loads(CONFIG.read_text(encoding="utf-8"))
        raw["profile"]["private_canaries"] = [private_canary]
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            config_path = tmp_path / "config.json"
            raw["fixture_path"] = str(ROOT / "fixtures" / "postings.json")
            raw["output_dir"] = str(tmp_path / "reports")
            config_path.write_text(json.dumps(raw), encoding="utf-8")
            config = load_config(config_path)

            result = run_dry_run(config, date(2026, 6, 30))
            projection, report, artifact, gate = _materialize(result, config, "private")
            written_report = Path(artifact.path).read_text(encoding="utf-8")
            public_payload = json.dumps(
                {"projection": projection, "artifact": asdict(artifact), "gate": gate},
                default=str,
                ensure_ascii=False,
            )

        for payload in (report, written_report, public_payload):
            self.assertNotIn(private_canary, payload)

    def test_unknown_deadline_is_uncertain_not_expired(self) -> None:
        parsed, uncertain = parse_deadline("not listed")

        self.assertIsNone(parsed)
        self.assertTrue(uncertain)

    def test_top_n_is_applied_only_in_projection(self) -> None:
        raw = json.loads(CONFIG.read_text(encoding="utf-8"))
        raw["top_n"] = 2
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            config_dir = tmp_path / "config"
            config_dir.mkdir()
            config_path = config_dir / "sample_config.json"
            raw["fixture_path"] = str(ROOT / "fixtures" / "postings.json")
            raw["output_dir"] = str(tmp_path / "reports")
            config_path.write_text(json.dumps(raw), encoding="utf-8")

            config = load_config(config_path)
            result = run_dry_run(config, date(2026, 6, 30))
            projection, _report, _artifact, _gate = _materialize(result, config, "top-n")

        self.assertEqual(result.top_n, 2)
        self.assertGreater(len(result.all_assessments), 2)
        self.assertEqual(len(projection["assessments"]), len(result.all_assessments))
        self.assertLessEqual(len(projection["action_queue"]), result.top_n)
        self.assertEqual(projection["summary"]["suppressed_apply"], projection["summary"]["apply_total"] - projection["summary"]["displayed_apply"])
        self.assertEqual(projection["summary"]["suppressed_hold"], projection["summary"]["hold_total"] - projection["summary"]["displayed_hold"])

    def test_real_source_is_blocked_for_dry_run(self) -> None:
        raw = json.loads(CONFIG.read_text(encoding="utf-8"))
        raw["sources"].append(
            {
                "source_id": "real_example",
                "enabled": True,
                "access_mode": "public_page",
                "auth_required": False,
                "tos_review_status": "unknown",
                "domains": ["example.com"],
                "rate_limit": "unknown",
                "failure_mode": "skip_source",
                "allowed_persisted_fields": [],
            }
        )
        with tempfile.TemporaryDirectory() as tmp:
            config_path = Path(tmp) / "config.json"
            config_path.write_text(json.dumps(raw), encoding="utf-8")
            with self.assertRaises(ConfigError):
                load_config(config_path)

    def test_dry_run_rejects_preloaded_real_source_config(self) -> None:
        config = load_config(ROOT / "config" / "live_sources.sample.json", allow_real_sources=True)

        with self.assertRaises(ConfigError):
            run_dry_run(config, date(2026, 6, 30))

    def test_manual_local_source_mode_is_allowed_without_real_adapter_enablement(self) -> None:
        raw = json.loads(CONFIG.read_text(encoding="utf-8"))
        raw["sources"][0]["access_mode"] = "manual"
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            config_path = tmp_path / "config.json"
            raw["fixture_path"] = str(ROOT / "fixtures" / "postings.json")
            raw["output_dir"] = str(tmp_path / "reports")
            config_path.write_text(json.dumps(raw), encoding="utf-8")
            config = load_config(config_path)
            result = run_dry_run(config, date(2026, 6, 30))
            projection, _report, _artifact, _gate = _materialize(result, config, "manual")

        self.assertEqual(result.source_metrics[0].source_id, "fixture")
        self.assertEqual(result.source_metrics[0].accepted_count, result.collected_count)
        self.assertEqual(projection["gate_sources"][0].source_id, "fixture")
        self.assertLessEqual(len(projection["action_queue"]), raw["top_n"])


    def test_v3_experience_gap_penalty_and_nonterminal_precedence(self) -> None:
        config = load_config(CONFIG)
        snapshot = SnapshotV2(
            source_id="fixture",
            canonical_url="https://jobs.example.test/v3-gap",
            source_posting_id="v3-gap",
            title="ML Engineer",
            company="Example",
            location="Seoul",
            deadline=date(2026, 12, 31),
            deadline_uncertain=False,
            required_qualifications=("경력 3년 이상",),
            preferred_qualifications=(),
            responsibilities=(),
            company_info=(),
            experience_tags=(),
            manual_review_flags=(),
            detail_quality="verified",
        )
        assessment = assess_snapshot_v2(snapshot, config_or_context=config, run_date=date(2026, 7, 1))
        self.assertEqual(assessment.score, max(0, assessment.score_breakdown.raw_score - 10))
        self.assertEqual(assessment.disposition, "hold")

        larger_gap = SnapshotV2(
            **{
                **asdict(snapshot),
                "canonical_url": "https://jobs.example.test/v3-large-gap",
                "source_posting_id": "v3-large-gap",
                "required_qualifications": ("경력 4년 이상",),
            }
        )
        larger = assess_snapshot_v2(larger_gap, config_or_context=config, run_date=date(2026, 7, 1))
        self.assertEqual(larger.disposition, "hold")
        self.assertEqual(larger.score, larger.score_breakdown.raw_score)
        expired_gap = SnapshotV2(
            **{
                **asdict(larger_gap),
                "canonical_url": "https://jobs.example.test/v3-expired-gap",
                "source_posting_id": "v3-expired-gap",
                "deadline": date(2026, 6, 30),
            }
        )
        expired = assess_snapshot_v2(expired_gap, config_or_context=config, run_date=date(2026, 7, 1))
        self.assertEqual(expired.disposition, "hold")

        boundary = SnapshotV2(
            **{
                **asdict(snapshot),
                "canonical_url": "https://jobs.example.test/v3-boundary",
                "source_posting_id": "v3-boundary",
                "required_qualifications": ("경력 2년 이상",),
            }
        )
        at_boundary = assess_snapshot_v2(boundary, config_or_context=config, run_date=date(2026, 7, 1))
        self.assertNotEqual(at_boundary.disposition, "hold")
        self.assertEqual(at_boundary.score, at_boundary.score_breakdown.raw_score)

    def test_v3_zero_experience_profile_numeric_gaps_are_actionable(self) -> None:
        config = load_config(CONFIG)
        zero_config = replace(
            config,
            profile=replace(config.profile, max_experience_years=0),
            user_context=replace(config.user_context, max_experience_years=0),
        )
        one_year = SnapshotV2(
            source_id="fixture",
            canonical_url="https://jobs.example.test/v3-zero-gap",
            source_posting_id="v3-zero-gap",
            title="ML Engineer",
            company="Example",
            location="Seoul",
            deadline=date(2026, 12, 31),
            deadline_uncertain=False,
            required_qualifications=("경력 1년 이상",),
            preferred_qualifications=(),
            responsibilities=(),
            company_info=(),
            experience_tags=(),
            manual_review_flags=(),
            detail_quality="verified",
        )
        assessment = assess_snapshot_v2(
            one_year,
            config_or_context=zero_config,
            run_date=date(2026, 7, 1),
        )
        self.assertEqual(assessment.score, max(0, assessment.score_breakdown.raw_score - 10))
        self.assertEqual(assessment.disposition, "hold")

        larger_gap = replace(
            one_year,
            canonical_url="https://jobs.example.test/v3-zero-large-gap",
            source_posting_id="v3-zero-large-gap",
            required_qualifications=("경력 3년 이상",),
        )
        larger = assess_snapshot_v2(
            larger_gap,
            config_or_context=zero_config,
            run_date=date(2026, 7, 1),
        )
        self.assertEqual(larger.disposition, "hold")
        self.assertEqual(larger.score, larger.score_breakdown.raw_score)
    def test_v3_experience_ranges_use_lower_endpoint_for_korean_and_english(self) -> None:
        config = load_config(CONFIG)
        four_year_config = replace(
            config,
            profile=replace(config.profile, max_experience_years=4),
            user_context=replace(config.user_context, max_experience_years=4),
        )
        two_year_config = replace(
            config,
            profile=replace(config.profile, max_experience_years=2),
            user_context=replace(config.user_context, max_experience_years=2),
        )
        base = SnapshotV2(
            source_id="fixture",
            canonical_url="https://jobs.example.test/v3-range",
            source_posting_id="v3-range",
            title="ML Engineer",
            company="Example",
            location="Seoul",
            deadline=date(2026, 12, 31),
            deadline_uncertain=False,
            required_qualifications=(),
            preferred_qualifications=(),
            responsibilities=(),
            company_info=(),
            experience_tags=(),
            manual_review_flags=(),
            detail_quality="verified",
        )

        for suffix, requirement in (
            ("ko", "경력 3~5년"),
            ("en", "3-5 years experience"),
            ("ko-compact-minimum", "경력 3년이상"),
            ("ko-compact-range", "경력 3~5년이상"),
        ):
            assessment = assess_snapshot_v2(
                replace(
                    base,
                    canonical_url=f"{base.canonical_url}-{suffix}",
                    source_posting_id=f"{base.source_posting_id}-{suffix}",
                    required_qualifications=(requirement,),
                ),
                config_or_context=four_year_config,
                run_date=date(2026, 7, 1),
            )
            self.assertEqual(assessment.score, assessment.score_breakdown.raw_score)
            self.assertNotEqual(assessment.disposition, "hold")

        true_minimum = assess_snapshot_v2(
            replace(
                base,
                canonical_url=f"{base.canonical_url}-minimum",
                source_posting_id=f"{base.source_posting_id}-minimum",
                required_qualifications=("경력 5년 이상",),
            ),
            config_or_context=four_year_config,
            run_date=date(2026, 7, 1),
        )
        self.assertEqual(
            true_minimum.score,
            max(0, true_minimum.score_breakdown.raw_score - 10),
        )
        self.assertEqual(true_minimum.disposition, "hold")

        for suffix, requirement in (
            ("compact-minimum-gap", "경력 3년이상"),
            ("compact-range-gap", "경력 3~5년이상"),
        ):
            assessment = assess_snapshot_v2(
                replace(
                    base,
                    canonical_url=f"{base.canonical_url}-{suffix}",
                    source_posting_id=f"{base.source_posting_id}-{suffix}",
                    required_qualifications=(requirement,),
                ),
                config_or_context=two_year_config,
                run_date=date(2026, 7, 1),
            )
            self.assertEqual(assessment.disposition, "hold")
            self.assertNotEqual(assessment.disposition, "apply")

    def test_v3_public_evidence_filters_broadened_military_terms(self) -> None:
        config = load_config(CONFIG)
        base = SnapshotV2(
            source_id="fixture",
            canonical_url="https://jobs.example.test/v3-public-military",
            source_posting_id="v3-public-military",
            title="ML Engineer",
            company="Example",
            location="Seoul",
            deadline=date(2026, 12, 31),
            deadline_uncertain=False,
            required_qualifications=(),
            preferred_qualifications=(),
            responsibilities=(),
            company_info=(),
            experience_tags=(),
            manual_review_flags=(),
            detail_quality="verified",
        )
        for suffix, requirement in (
            ("army", "Army Python"),
            ("exemption", "군 면제 Python"),
            ("alternative", "대체복무 Python"),
            ("service", "군 복무 Python"),
        ):
            assessment = assess_snapshot_v2(
                replace(
                    base,
                    canonical_url=f"{base.canonical_url}-{suffix}",
                    source_posting_id=f"{base.source_posting_id}-{suffix}",
                    required_qualifications=(requirement,),
                ),
                config_or_context=config,
                run_date=date(2026, 7, 1),
            )
            self.assertFalse(
                any(requirement in evidence for evidence in assessment.matched_evidence)
            )

    def test_v3_report_policy_is_korean_and_fail_closed_for_capture(self) -> None:
        config = load_config(CONFIG)
        result = run_dry_run(config, date(2026, 6, 30))
        assessment = result.all_assessments[0]
        presentation = project_report_presentation(assessment, command_mode="scheduled-run")
        self.assertEqual(REPORT_LINK_POLICY_VERSION, 1)
        self.assertIn(presentation["label"], {"지원 추천", "도전 지원", "원문 확인 필요", "제외"})
        self.assertNotIn("experience_mismatch", str(presentation))
        self.assertNotIn("verified", str(presentation).casefold())
        self.assertEqual(presentation["link_state"], "원문 링크 확인 필요")
        self.assertEqual(
            verified_link_url(
                "capture-import",
                assessment.source_id,
                assessment.source_url,
                assessment.source_posting_id,
                assessment.detail_quality,
            ),
            None,
        )
        self.assertIsNone(
            verified_link_url(
                "scheduled-run",
                "unknown-source",
                "https://jobs.example.test/unsafe",
                "x",
                "verified",
            )
        )
        self.assertIsNone(
            verified_link_url(
                "scheduled-run",
                assessment.source_id,
                assessment.source_url,
                assessment.source_posting_id,
                "manual_only",
            )
        )
        valid_links = (
            ("saramin", "https://www.saramin.co.kr/zf_user/jobs/relay/view-detail?rec_idx=54106686&rec_seq=0", "54106686"),
            ("jobkorea", "https://www.jobkorea.co.kr/Recruit/GI_Read/49476607", "49476607"),
            ("wanted", "https://www.wanted.co.kr/wd/123456", "123456"),
            ("jumpit", "https://jumpit.saramin.co.kr/position/54308479", "54308479"),
            ("rallit", "https://www.rallit.com/positions/987", "987"),
            ("fixture", "https://jobs.example.test/fx-apply-001", "fx-apply-001"),
            ("rocketpunch", "https://www.rocketpunch.com/en/jobs/158927", "158927"),
        )
        for source_id, source_url, posting_id in valid_links:
            with self.subTest(source_id=source_id):
                self.assertEqual(
                    verified_link_url("scheduled-run", source_id, source_url, posting_id, "verified"),
                    source_url,
                )
        self.assertEqual(
            verified_link_url(
                "scheduled-run",
                "saramin",
                "https://www.saramin.co.kr/zf_user/jobs/relay/view-detail",
                "54106686",
                "verified",
            ),
            "https://www.saramin.co.kr/zf_user/jobs/relay/view-detail?rec_idx=54106686&rec_seq=0",
        )
        invalid_links = (
            ("jobkorea", "https://www.jobkorea.co.kr/recruit/joblist", "49476607"),
            ("wanted", "https://www.wanted.co.kr/search?query=python", "123456"),
            ("rocketpunch", "https://www.rocketpunch.com/en/jobs?selectedJobId=158927", "158927"),
            ("rocketpunch", "https://www.rocketpunch.com/en/jobs/158928", "158927"),
            ("fixture", "https://jobs.example.test/fx-apply-001", "fx-hold-001"),
            ("fixture", "https://jobs.example.test/apply-ml-engineer", "fx-apply-001"),
            ("saramin", "https://www.saramin.co.kr/zf_user/jobs/relay/view-detail?rec_idx=54106686&rec_seq=0&next=1", "54106686"),
        )
        for source_id, source_url, posting_id in invalid_links:
            with self.subTest(source_id=source_id, source_url=source_url):
                self.assertIsNone(
                    verified_link_url("scheduled-run", source_id, source_url, posting_id, "verified")
                )
    def test_canonical_gate_v4_is_fail_closed_on_v4_axis_and_timing(self) -> None:
        config = load_config(CONFIG)
        result = run_dry_run(config, date(2026, 6, 30))
        _projection, _report, artifact, _v2_gate = _materialize(
            result, config, "canonical-v4"
        )
        outcome = SourceExecutionOutcomeV1(
            "fixture", True, True, "success", None, 9
        )
        gate = build_gate_v4(
            _as_pipeline_v4(result, (outcome,)),
            enabled_source_ids=("fixture",),
            report_artifact=artifact,
        )
        self.assertEqual(gate["status"], "pass")
        self.assertEqual(gate["sources"][0]["duration_ms"], 0)
        self.assertEqual(gate["source_outcomes"][0]["elapsed_ms"], 0)
        canonical_gate_v4_bytes(gate)

        with self.assertRaises(ValueError):
            canonical_gate_v4_bytes(_v2_gate)
        for forged in (
            {**gate, "source_outcomes": []},
            {**gate, "sources": []},
            {
                **gate,
                "source_outcomes": [
                    *gate["source_outcomes"],
                    {
                        **gate["source_outcomes"][0],
                        "source_id": "other",
                    },
                ],
            },
            {
                **gate,
                "source_outcomes": [
                    *gate["source_outcomes"],
                    dict(gate["source_outcomes"][0]),
                ],
            },
            {
                **gate,
                "sources": [
                    {**gate["sources"][0], "duration_ms": 1},
                ],
            },
            {
                **gate,
                "source_outcomes": [
                    {**gate["source_outcomes"][0], "elapsed_ms": 1},
                ],
            },
        ):
            with self.subTest(forged=forged):
                with self.assertRaises(ValueError):
                    canonical_gate_v4_bytes(forged)
if __name__ == "__main__":
    unittest.main()
