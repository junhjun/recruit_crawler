from __future__ import annotations

import io
import json
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from recruit_crawler.cli import main as cli_main
from recruit_crawler.config import load_config
from recruit_crawler.pipeline import build_live_run_quality_gate, run_live_run

CONFIG = ROOT / "config" / "sample_config.json"


class PipelineLiveFlowTests(unittest.TestCase):
    def test_live_config_can_load_reviewed_real_sources(self) -> None:
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
            config = load_config(config_path, allow_real_sources=True)

        self.assertEqual(config.sources[0].source_id, "company_careers")

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
            summary, _report, _ranked = run_live_run(config, date(2026, 6, 30))

        self.assertEqual(len(summary.source_metrics), 1)
        self.assertEqual(summary.source_metrics[0].source_id, "fixture")
        self.assertTrue(summary.source_metrics[0].attempted)
        self.assertEqual(summary.source_metrics[0].candidate_count, 1)
        self.assertEqual(summary.source_metrics[0].error_count, 0)

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
            summary, _report, _ranked = run_live_run(config, date(2026, 6, 30))
            gate = build_live_run_quality_gate(summary, config)

        self.assertEqual(gate["status"], "fail")
        self.assertEqual(gate["sources"][0]["source_id"], "fixture")
        self.assertEqual(gate["sources"][0]["candidate_count"], 0)
        self.assertTrue(any("enabled source fixture collected 0 candidates" in finding["message"] for finding in gate["findings"]))

    def test_live_run_cli_writes_quality_gate_json(self) -> None:
        raw = json.loads(CONFIG.read_text(encoding="utf-8"))
        output = io.StringIO()
        with tempfile.TemporaryDirectory() as tmp, redirect_stdout(output):
            tmp_path = Path(tmp)
            config_path = tmp_path / "config.json"
            fixture_path = tmp_path / "postings.json"
            gate_path = tmp_path / "live_quality_gate.json"
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

        self.assertEqual(exit_code, 1)
        self.assertEqual(gate["status"], "fail")
        self.assertIn("Live-run quality gate status: fail", output.getvalue())

    def test_live_run_allows_two_years_and_filters_above_profile_limit(self) -> None:
        raw = json.loads(CONFIG.read_text(encoding="utf-8"))
        raw["top_n"] = 5
        raw["profile"]["max_experience_years"] = 2
        raw["sources"] = [
            {
                "source_id": "fixture",
                "enabled": True,
                "access_mode": "manual",
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
            summary, report, ranked = run_live_run(config, date(2026, 6, 30))

        self.assertEqual(summary.experience_excluded, 1)
        self.assertEqual(len(ranked), 2)
        self.assertIn("New Grad AI Engineer", report)
        self.assertIn("AI Engineer 1 Year", report)
        self.assertNotIn("AI Engineer 3 Years", report)
