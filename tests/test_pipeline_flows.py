from __future__ import annotations

import json
import sys
import tempfile
import unittest
from dataclasses import asdict
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from recruit_crawler.config import ConfigError, load_config
from recruit_crawler.jd_parser import parse_deadline
from recruit_crawler.pipeline import run_dry_run

CONFIG = ROOT / "config" / "sample_config.json"


class PipelineFlowTests(unittest.TestCase):

    def test_fixture_e2e_generates_report_without_expired_postings(self) -> None:
        config = load_config(CONFIG)
        summary, report, ranked = run_dry_run(config, date(2026, 6, 30))

        self.assertTrue(summary.report_path.exists())
        self.assertEqual(summary.candidates_collected, 6)
        self.assertEqual(summary.duplicates_removed, 1)
        self.assertEqual(summary.expired_excluded, 1)
        self.assertEqual(summary.ranked_count, 4)
        self.assertIn("# 오늘의 채용 후보", report)
        self.assertIn("## 우선순위 표", report)
        self.assertIn("## 상세 메모", report)
        self.assertIn("https://jobs.example.test/apply-ml-engineer", report)
        self.assertNotIn("Expired ML Intern", report)
        self.assertNotIn("RAW_JD_CANARY_EXPIRED", report)
        self.assertEqual(len(ranked), 4)

    def test_recommendation_buckets_include_apply_hold_and_low_priority(self) -> None:
        config = load_config(CONFIG)
        _summary, _report, ranked = run_dry_run(config, date(2026, 6, 30))
        recommendations = {item.recommendation for item in ranked}

        self.assertIn("apply", recommendations)
        self.assertIn("hold", recommendations)
        self.assertIn("low_priority", recommendations)

    def test_report_excludes_raw_jd_and_private_profile_canaries(self) -> None:
        config = load_config(CONFIG)
        _summary, report, _ranked = run_dry_run(config, date(2026, 6, 30))

        forbidden = [
            "RAW_JD_CANARY_APPLY",
            "RAW_JD_CANARY_HOLD",
            "RAW_JD_CANARY_LOW",
            "RAW_JD_CANARY_AMBIGUOUS",
            "RAW_JD_CANARY_DUPLICATE",
            "PRIVATE_PROFILE_CANARY",
            "Ignore previous instructions",
        ]
        for value in forbidden:
            self.assertNotIn(value, report)

    def test_each_selected_posting_has_actionable_report_fields(self) -> None:
        config = load_config(CONFIG)
        _summary, report, ranked = run_dry_run(config, date(2026, 6, 30))

        self.assertEqual(len(ranked), config.top_n)
        for assessment in ranked:
            snapshot = assessment.snapshot
            self.assertIn(snapshot.source_url, report)
            self.assertIn(f"(`{assessment.recommendation}`)", report)
            self.assertIn(f"점수 **{assessment.score}**", report)
            self.assertIn("| 항목 | 내용 |", report)
            self.assertIn("- **맞는 부분**:", report)
            self.assertIn("- **리스크**:", report)
            self.assertIn("- **확인할 것**:", report)
            self.assertIn("- **지원 각도**:", report)

    def test_report_surface_text_is_korean(self) -> None:
        config = load_config(CONFIG)
        _summary, report, _ranked = run_dry_run(config, date(2026, 6, 30))

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

            summary, report, ranked = run_dry_run(config, date(2026, 6, 30))
            written_report = summary.report_path.read_text(encoding="utf-8")
            summary_payload = json.dumps(asdict(summary), default=str, ensure_ascii=False)
            ranked_payload = json.dumps([asdict(item) for item in ranked], default=str, ensure_ascii=False)

        for payload in (report, written_report, summary_payload, ranked_payload):
            self.assertNotIn(private_canary, payload)

    def test_unknown_deadline_is_uncertain_not_expired(self) -> None:
        parsed, uncertain = parse_deadline("not listed")

        self.assertIsNone(parsed)
        self.assertTrue(uncertain)

    def test_top_n_is_configurable(self) -> None:
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
            summary, _report, ranked = run_dry_run(config, date(2026, 6, 30))

        self.assertEqual(summary.ranked_count, 2)
        self.assertEqual(len(ranked), 2)

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
            summary, _report, ranked = run_dry_run(config, date(2026, 6, 30))

        self.assertEqual(summary.sources_attempted, ["fixture"])
        self.assertEqual(summary.source_metrics[0].source_id, "fixture")
        self.assertEqual(summary.source_metrics[0].candidate_count, summary.candidates_collected)
        self.assertEqual(len(ranked), raw["top_n"])
