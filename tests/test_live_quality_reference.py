from __future__ import annotations

import hashlib
import json
import sys
import unittest
from datetime import datetime
from pathlib import Path
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from recruit_crawler.gate import build_gate_v2, canonical_gate_bytes
from recruit_crawler.identity import identity_basis, posting_key
from recruit_crawler.pipeline import build_pipeline_result_v2
from recruit_crawler.projection import project_pipeline_result, project_public_assessments
from recruit_crawler.schemas import (
    AppConfig,
    CandidateV2,
    PersistenceEnvelopeV3,
    PostingCandidate,
    Profile,
    ReportArtifactV2,
    ScoringWeights,
    SourceManifest,
    Thresholds,
    UserContext,
)
from recruit_crawler.report_policy import project_report_presentation, verified_link_url
from recruit_crawler.summarizer import render_report_v2


class LiveQualityReferenceTests(unittest.TestCase):
    """Execute the public V3 runtime against independently authored literals."""

    def _fixture(self, name: str) -> dict:
        with (ROOT / "fixtures" / name).open(encoding="utf-8") as handle:
            return json.load(handle)

    def _runs(self):
        for name in ("live_quality_tuning_v2.json", "live_quality_holdout_v2.json"):
            corpus = self._fixture(name)
            self.assertEqual(corpus["schema_version"], 2)
            self.assertEqual(corpus["oracle_version"], 1)
            self.assertIsInstance(corpus["runs"], list)
            for run in corpus["runs"]:
                yield name, run

    @staticmethod
    def _clock(run: dict) -> datetime:
        return datetime.fromisoformat(run["clock"].replace("Z", "+00:00"))

    def _config(self, fixture_name: str, run: dict) -> AppConfig:
        context = run["context"]
        source = run["sources"][0]
        source_host = urlsplit(source["source_url"]).hostname or ""
        user_context = UserContext(
            desired_roles=list(context["desired_roles"]),
            skills=list(context["skills"]),
            preferred_locations=list(context["preferred_locations"]),
            max_experience_years=int(context["max_experience_years"]),
            explicit_deal_breakers=list(context["explicit_deal_breakers"]),
            provenance={},
        )
        profile = Profile(
            desired_roles=list(context["desired_roles"]),
            skills=list(context["skills"]),
            preferred_locations=list(context["preferred_locations"]),
            max_experience_years=int(context["max_experience_years"]),
            exclusions=list(context["explicit_deal_breakers"]),
            education_claim=context["education_claim"],
        )
        manifest = SourceManifest(
            source_id=source["source_id"],
            enabled=bool(source["enabled"]),
            access_mode=source["access_mode"],
            auth_required=False,
            tos_review_status="fixture",
            domains=[source_host],
            rate_limit="fixture",
            failure_mode="skip_source",
            allowed_persisted_fields=list(source["allowed_persisted_fields"]),
        )
        config = run["config"]
        return AppConfig(
            top_n=int(config["top_n"]),
            output_dir=ROOT / "reports",
            fixture_path=ROOT / "fixtures" / fixture_name,
            delivery_mode="dry-run",
            thresholds=Thresholds(**config["thresholds"]),
            scoring_weights=ScoringWeights(**config["weights"]),
            profile=profile,
            user_context=user_context,
            sources=[manifest],
            manual_review_n=int(config["manual_review_n"]),
            scoring_schema_version=int(config["scoring_schema_version"]),
        )

    def _candidate(self, posting: dict, collected_at: datetime) -> PostingCandidate:
        return PostingCandidate(
            source_id=posting["source_id"],
            source_url=posting["source_url"],
            source_posting_id=posting["source_posting_id"],
            title=posting["title"],
            company=posting["company"],
            location=posting["location"],
            deadline_raw=posting["deadline"],
            collected_at=collected_at,
            raw_jd=dict(posting["structured_snapshot"]),
        )

    def _execute(self, fixture_name: str, run: dict) -> dict:
        collected_at = self._clock(run)
        config = self._config(fixture_name, run)
        candidates = tuple(self._candidate(posting, collected_at) for posting in run["postings"])
        result = build_pipeline_result_v2(
            config,
            collected_at.date(),
            candidates,
            run_id=run["run_id"],
            sources_attempted=(source["source_id"] for source in run["sources"] if source["enabled"]),
        )
        projection = project_pipeline_result(result)
        rendered = render_report_v2(result)
        artifact = ReportArtifactV2(
            schema_version=2,
            generated=True,
            path=None,
            rendered=rendered,
        )
        gate = build_gate_v2(
            result,
            enabled_source_ids=(source["source_id"] for source in run["sources"] if source["enabled"]),
            context_status="complete",
            projection=projection,
            report_artifact=artifact,
        )
        envelope = PersistenceEnvelopeV3(
            schema_version=3,
            run_identity={
                "run_id": run["run_id"],
                "run_date": collected_at.date().isoformat(),
                "command_mode": result.command_mode,
            },
            report_artifact=artifact,
            gate_status=gate["status"],
            context_status=gate["context_status"],
            gate_json_sha256=hashlib.sha256(canonical_gate_bytes(gate)).hexdigest(),
            summary=projection["summary"],
            source_metrics=projection["gate_sources"],
            assessments=project_public_assessments(result.all_assessments),
        )
        identity_inputs = {
            posting["source_posting_id"].upper(): posting
            for posting in run["postings"]
            if posting["source_posting_id"].upper() in run["expected"]["identity_projection"]
        }
        identities = {}
        for case_id, posting in identity_inputs.items():
            candidate = CandidateV2(
                source_id=posting["source_id"],
                source_url=posting["source_url"],
                source_posting_id=posting["source_posting_id"],
                title=posting["title"],
                company=posting["company"],
                location=posting["location"],
                deadline_raw=posting["deadline"],
                collected_at=collected_at,
                raw_structured=tuple(posting["structured_snapshot"].items()),
            )
            identities[case_id] = {
                "basis": dict(identity_basis(candidate)),
                "posting_key": posting_key(candidate),
            }
        return {
            "result": result,
            "projection": projection,
            "rendered": rendered,
            "gate": gate,
            "envelope": envelope,
            "identities": identities,
        }

    def test_roots_runs_and_privacy_allowlist_are_literal(self) -> None:
        for name, run in self._runs():
            with self.subTest(fixture=name, run_id=run["run_id"]):
                self.assertIn(run["run_id"], {"tuning-primary", "tuning-unknown-education", "holdout-primary"})
                self.assertEqual(run["clock"], "2026-07-14T00:00:00Z")
                self.assertEqual(run["duration_ms"], 0)
                self.assertIsNone(run["inheritance"])
                self.assertEqual(set(run["config"]), {"top_n", "manual_review_n", "scoring_schema_version", "weights", "thresholds"})
                self.assertEqual(run["config"]["weights"], {"required": 40, "responsibilities": 20, "role": 20, "preferred": 10, "location": 10})
                self.assertEqual(run["config"]["thresholds"], {"apply": 75, "hold": 50})
                self.assertEqual(run["context"]["skills"], ["Python", "PyTorch"])
                self.assertEqual(run["context"]["preferred_locations"], ["Seoul"])
                self.assertEqual(run["context"]["max_experience_years"], 2)
                self.assertEqual(run["context"]["explicit_deal_breakers"], ["unpaid internship"])
                self.assertEqual(len(run["sources"]), 1)
                source = run["sources"][0]
                self.assertEqual(source["source_id"], "fixture")
                self.assertTrue(source["enabled"])
                self.assertEqual(source["access_mode"], "fixture")
                self.assertEqual(source["source_url"], "https://jobs.example.test")
                self.assertEqual(source["duration_ms"], 0)
                self.assertEqual(source["allowed_persisted_fields"], [
                    "source_id", "source_url", "source_posting_id", "title", "company",
                    "location", "deadline", "structured_snapshot",
                ])
                for posting in run["postings"]:
                    self.assertEqual(set(posting), {"source_id", "source_url", "source_posting_id", "title", "company", "location", "deadline", "structured_snapshot"})
                    self.assertEqual(posting["source_id"], "fixture")
                    self.assertTrue(posting["source_url"].startswith("https://jobs.example.test/"))
                    self.assertNotIn("raw_jd", posting)
                    snapshot = posting["structured_snapshot"]
                    self.assertEqual(set(snapshot), {"required_qualifications", "preferred_qualifications", "responsibilities", "company_info", "experience_tags", "manual_review_flags", "detail_quality"})
                    self.assertIn(snapshot["detail_quality"], {"verified", "manual_only"})
                    self.assertNotIn("private", json.dumps(posting).casefold())
                    self.assertNotIn("canary", json.dumps(posting).casefold())

    @staticmethod
    def _metric_dict(metric) -> dict:
        return {
            "source_id": metric.source_id,
            "attempted": metric.attempted,
            "accepted_count": metric.accepted_count,
            "rejected_count": metric.rejected_count,
            "duplicate_count": metric.duplicate_count,
            "normalized_changed_field_count": metric.normalized_changed_field_count,
            "normalized_emptied_field_count": metric.normalized_emptied_field_count,
            "verified_count": metric.verified_count,
            "manual_only_count": metric.manual_only_count,
            "error_codes": list(metric.error_codes),
            "duration_ms": metric.duration_ms,
        }

    @staticmethod
    def _ids(items) -> list[str]:
        return [str(item["source_posting_id"]).upper() for item in items]

    def test_runtime_terminal_maps_scores_and_reasons_against_literals(self) -> None:
        for fixture_name, run in self._runs():
            with self.subTest(fixture=fixture_name, run_id=run["run_id"]):
                expected = run["expected"]
                projection = self._execute(fixture_name, run)["projection"]
                actual_assessments = {
                    str(item["source_posting_id"]).upper(): item
                    for item in projection["assessments"]
                }
                expected_absent = {
                    case_id
                    for case_id, disposition in expected["terminal_dispositions"].items()
                    if disposition in {"duplicate", "rejected"}
                }
                expected_assessed = set(expected["terminal_dispositions"]) - expected_absent
                self.assertEqual(set(actual_assessments), expected_assessed)
                self.assertEqual(
                    {case_id: item["final_disposition"] for case_id, item in actual_assessments.items()},
                    {case_id: expected["terminal_dispositions"][case_id] for case_id in expected_assessed},
                )
                self.assertEqual(
                    {case_id: item["score"] for case_id, item in actual_assessments.items()},
                    {case_id: expected["scores"][case_id] for case_id in expected_assessed},
                )
                self.assertEqual(
                    {case_id: item["reason_codes"] for case_id, item in actual_assessments.items()},
                    {case_id: expected["reason_codes"][case_id] for case_id in expected_assessed},
                )

    def test_runtime_queues_summary_and_metrics_against_literals(self) -> None:
        for fixture_name, run in self._runs():
            with self.subTest(fixture=fixture_name, run_id=run["run_id"]):
                expected = run["expected"]
                executed = self._execute(fixture_name, run)
                result = executed["result"]
                projection = executed["projection"]
                actual_assessments = {
                    str(item["source_posting_id"]).upper(): item
                    for item in projection["assessments"]
                }
                self.assertEqual(
                    [item["source_posting_id"].upper() for item in projection["action_queue"]],
                    expected["displayed_actionable_ids"],
                )
                self.assertEqual(
                    [item["source_posting_id"].upper() for item in projection["manual_queue"]],
                    expected["displayed_manual_ids"],
                )
                actual_actionable = [
                    case_id for case_id, item in actual_assessments.items()
                    if item["final_disposition"] in {"apply", "hold"}
                ]
                actual_manual = [
                    case_id for case_id, item in actual_assessments.items()
                    if item["final_disposition"] == "manual_review"
                ]
                self.assertEqual(actual_actionable, expected["actionable_ids"])
                self.assertEqual(actual_manual, expected["manual_ids"])
                self.assertEqual(
                    set(actual_manual) - set(self._ids(projection["manual_queue"])),
                    set(expected["suppressed_ids"]),
                )
                self.assertEqual(projection["summary"], expected["summary"])
                self.assertEqual(len(result.source_metrics), len(expected["source_metrics"]))
                self.assertEqual(
                    [metric for metric in map(self._metric_dict, result.source_metrics)],
                    expected["source_metrics"],
                )
                for metric in result.source_metrics:
                    survivor_assessments = [
                        item for item in projection["assessments"]
                        if item["source_id"] == metric.source_id
                    ]
                    self.assertEqual(
                        metric.verified_count,
                        sum(item["source_detail_quality"] == "verified" for item in survivor_assessments),
                    )
                    self.assertEqual(
                        metric.manual_only_count,
                        sum(item["source_detail_quality"] == "manual_only" for item in survivor_assessments),
                    )

    def test_runtime_render_bytes_contract(self) -> None:
        for fixture_name, run in self._runs():
            with self.subTest(fixture=fixture_name, run_id=run["run_id"]):
                expected_report = run["expected"]["report_bytes"].encode("utf-8")
                self.assertTrue(expected_report.startswith(b"# "))
                self.assertIn("## 한눈에 보기".encode("utf-8"), expected_report)
                self.assertIn("## 지원/검토".encode("utf-8"), expected_report)
                self.assertIn("## 제외".encode("utf-8"), expected_report)
                self.assertNotIn("## 제외 요약".encode("utf-8"), expected_report)
                self.assertNotIn("## 수동 검토".encode("utf-8"), expected_report)
                self.assertNotIn(b"(`apply`)", expected_report)
                self.assertNotIn(b"(`hold`)", expected_report)
                self.assertTrue(expected_report.endswith(b"\n"))
                self.assertEqual(expected_report.count("## 원문 확인 필요".encode("utf-8")), 1 if run["expected"]["summary"]["manual_review_total"] else 0)
                compact_report = expected_report.count(b"\n") == 1
                if compact_report:
                    self.fail(f"{run['run_id']}: fixture_freeze_required_report_bytes")
                rendered = self._execute(fixture_name, run)["rendered"]
                self.assertEqual(rendered.markdown_bytes, expected_report)

    def test_runtime_gate_map_contract(self) -> None:
        for fixture_name, run in self._runs():
            with self.subTest(fixture=fixture_name, run_id=run["run_id"]):
                expected = run["expected"]
                gate = self._execute(fixture_name, run)["gate"]
                actual_gate_projection = {
                    key: gate.get(key)
                    for key in expected["gate"]
                }
                self.assertEqual(actual_gate_projection, expected["gate"])

    def test_runtime_gate_bytes_contract(self) -> None:
        for fixture_name, run in self._runs():
            with self.subTest(fixture=fixture_name, run_id=run["run_id"]):
                expected = run["expected"]
                expected_gate_bytes = expected["gate_bytes"].encode("utf-8")
                expected_gate_wire = json.loads(expected_gate_bytes.decode("utf-8"))
                complete_gate_keys = {
                    "schema_version", "command_mode", "run_date", "pipeline_schema_version",
                    "score_schema_version", "disposition_schema_version", "status",
                    "context_status", "report", "sources", "summary",
                    "eligibility_reason_counts", "manual_reason_counts", "invariants", "findings",
                }
                self.assertEqual(set(expected_gate_wire), complete_gate_keys)
                self.assertEqual(
                    {key: expected_gate_wire[key] for key in expected["gate"]},
                    expected["gate"],
                )
                gate = self._execute(fixture_name, run)["gate"]
                self.assertEqual(canonical_gate_bytes(gate), expected_gate_bytes)

    def test_runtime_identity_and_envelope_contract(self) -> None:
        for fixture_name, run in self._runs():
            with self.subTest(fixture=fixture_name, run_id=run["run_id"]):
                expected = run["expected"]
                executed = self._execute(fixture_name, run)
                for case_id, basis in expected["identity_projection"].items():
                    self.assertEqual(executed["identities"][case_id]["basis"], basis)
                    actual_assessment = next(
                        item for item in executed["projection"]["assessments"]
                        if item["source_posting_id"].upper() == case_id
                    )
                    self.assertEqual(
                        actual_assessment["posting_key"],
                        executed["identities"][case_id]["posting_key"],
                    )

                envelope = executed["envelope"]
                expected_envelope = expected["envelope_projection"]
                actual_envelope = {
                    "schema_version": envelope.schema_version,
                    "gate_status": envelope.gate_status,
                    "context_status": envelope.context_status,
                    "summary": dict(envelope.summary),
                }
                self.assertEqual(actual_envelope, expected_envelope)

    def test_report_presentation_is_korean_and_source_bound(self) -> None:
        fixture_name, run = next(self._runs())
        executed = self._execute(fixture_name, run)
        assessment = executed["result"].all_assessments[0]
        presentation = project_report_presentation(assessment, command_mode="scheduled-run")
        self.assertNotIn("verified", str(presentation).casefold())
        self.assertEqual(presentation["label"], "지원 추천")
        self.assertEqual(presentation["link_state"], "원문 링크 확인됨")
        self.assertEqual(presentation["link_url"], assessment.source_url)
        self.assertIsNone(
            verified_link_url(
                "scheduled-run",
                "fixture",
                "https://jobs.example.test/search",
                "synthetic-1",
                "verified",
            )
        )


if __name__ == "__main__":
    unittest.main()