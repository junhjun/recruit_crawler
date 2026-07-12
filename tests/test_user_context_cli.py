from __future__ import annotations

import io
import json
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from datetime import date
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from recruit_crawler.cli import main as cli_main
from recruit_crawler.config import apply_context_document, load_config
from recruit_crawler.pipeline import run_dry_run
from recruit_crawler.schemas import UserContext
from recruit_crawler.user_context import UserContextImportError, merge_supplemental_answers, supplemental_questions

CONFIG = ROOT / "config" / "sample_config.json"


class UserContextCliTests(unittest.TestCase):
    def _write_two_posting_config(self, tmp_path: Path) -> Path:
        raw = json.loads(CONFIG.read_text(encoding="utf-8"))
        fixture_path = tmp_path / "postings.json"
        raw["fixture_path"] = str(fixture_path)
        raw["output_dir"] = str(tmp_path / "reports")
        raw["top_n"] = 2
        fixture_path.write_text(
            json.dumps(
                [
                    {
                        "source_id": "fixture",
                        "source_url": "https://jobs.example.test/python",
                        "source_posting_id": "python",
                        "title": "Python ML Engineer",
                        "company": "Python Co",
                        "location": "Seoul",
                        "deadline": "2026-07-10",
                        "raw_jd": {
                            "required_qualifications": ["Python", "machine learning"],
                            "preferred_qualifications": ["PyTorch"],
                            "responsibilities": ["Build Python models"],
                            "company_info": ["AI team"],
                            "experience_tags": ["경력무관"],
                        },
                    },
                    {
                        "source_id": "fixture",
                        "source_url": "https://jobs.example.test/rust",
                        "source_posting_id": "rust",
                        "title": "Rust Systems Engineer",
                        "company": "Rust Co",
                        "location": "Seoul",
                        "deadline": "2026-07-10",
                        "raw_jd": {
                            "required_qualifications": ["Rust", "distributed systems"],
                            "preferred_qualifications": ["Tokio"],
                            "responsibilities": ["Build Rust services"],
                            "company_info": ["Infrastructure team"],
                            "experience_tags": ["경력무관"],
                        },
                    },
                ]
            ),
            encoding="utf-8",
        )
        config_path = tmp_path / "config.json"
        config_path.write_text(json.dumps(raw), encoding="utf-8")
        return config_path

    def test_context_document_replaces_config_profile_for_scoring(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            config_path = self._write_two_posting_config(tmp_path)
            context_path = tmp_path / "context.md"
            context_path.write_text(
                "Roles: Systems Engineer\nSkills: Rust, distributed systems, Tokio\nLocations: Seoul\nExperience: 2 years\n",
                encoding="utf-8",
            )

            config = apply_context_document(load_config(config_path), context_path)
            _summary, _report, ranked = run_dry_run(config, date(2026, 6, 30))

        self.assertEqual(config.user_context.skills, ["Rust", "distributed systems", "Tokio"])
        self.assertEqual(ranked[0].snapshot.title, "Rust Systems Engineer")

    def test_dry_run_context_doc_cli_applies_personalized_context(self) -> None:
        output = io.StringIO()
        with tempfile.TemporaryDirectory() as tmp, redirect_stdout(output):
            tmp_path = Path(tmp)
            config_path = self._write_two_posting_config(tmp_path)
            context_path = tmp_path / "context.md"
            context_path.write_text(
                "Roles: Systems Engineer\nSkills: Rust, distributed systems, Tokio\nLocations: Seoul\nExperience: 2 years\n",
                encoding="utf-8",
            )

            exit_code = cli_main(
                [
                    "dry-run",
                    "--config",
                    str(config_path),
                    "--run-date",
                    "2026-06-30",
                    "--context-doc",
                    str(context_path),
                    "--print-report",
                ]
            )

        report = output.getvalue()
        self.assertEqual(exit_code, 0)
        self.assertLess(report.index("Rust Systems Engineer"), report.index("Python ML Engineer"))

    def test_dry_run_context_doc_cli_merges_multiple_personal_inputs(self) -> None:
        output = io.StringIO()
        with tempfile.TemporaryDirectory() as tmp, redirect_stdout(output):
            tmp_path = Path(tmp)
            config_path = self._write_two_posting_config(tmp_path)
            resume_path = tmp_path / "resume.md"
            portfolio_path = tmp_path / "portfolio.md"
            resume_path.write_text(
                "Roles: Systems Engineer\nSkills: Rust\nLocations: Seoul\nExperience: 2 years\n",
                encoding="utf-8",
            )
            portfolio_path.write_text(
                "Skills: distributed systems, Tokio\nLocations: Remote\n",
                encoding="utf-8",
            )

            exit_code = cli_main(
                [
                    "dry-run",
                    "--config",
                    str(config_path),
                    "--run-date",
                    "2026-06-30",
                    "--context-doc",
                    str(resume_path),
                    "--context-doc",
                    str(portfolio_path),
                    "--print-report",
                ]
            )

        report = output.getvalue()
        self.assertEqual(exit_code, 0)
        self.assertLess(report.index("Rust Systems Engineer"), report.index("Python ML Engineer"))

    def test_context_doc_cli_interviews_for_missing_context(self) -> None:
        output = io.StringIO()
        with tempfile.TemporaryDirectory() as tmp, redirect_stdout(output), patch(
            "builtins.input",
            side_effect=["Rust, distributed systems, Tokio", "2"],
        ):
            tmp_path = Path(tmp)
            config_path = self._write_two_posting_config(tmp_path)
            context_path = tmp_path / "partial.md"
            context_path.write_text(
                "Roles: Systems Engineer\nLocations: Seoul\n",
                encoding="utf-8",
            )

            exit_code = cli_main(
                [
                    "dry-run",
                    "--config",
                    str(config_path),
                    "--run-date",
                    "2026-06-30",
                    "--context-doc",
                    str(context_path),
                    "--print-report",
                ]
            )

        report = output.getvalue()
        self.assertEqual(exit_code, 0)
        self.assertIn("Supplemental context interview:", report)
        self.assertLess(report.index("Rust Systems Engineer"), report.index("Python ML Engineer"))

    def test_context_doc_cli_fails_closed_on_private_canary(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            config_path = self._write_two_posting_config(tmp_path)
            context_path = tmp_path / "private.md"
            context_path.write_text("Skills: Python\nPRIVATE_PROFILE_CANARY", encoding="utf-8")

            with self.assertRaises(SystemExit) as cm:
                cli_main(
                    [
                        "dry-run",
                        "--config",
                        str(config_path),
                        "--context-doc",
                        str(context_path),
                    ]
                )

        self.assertEqual(cm.exception.code, 2)

    def test_scheduled_run_with_context_doctor_output_has_complete_context(self) -> None:
        with tempfile.TemporaryDirectory() as tmp, patch(
            "builtins.input",
            side_effect=["AI Engineer", "Python, SQL", "Seoul", "2", ""],
        ):
            tmp_path = Path(tmp)
            config_path = self._write_two_posting_config(tmp_path)
            resume_path = tmp_path / "resume.md"
            preferences_path = tmp_path / "preferences.md"
            gate_path = tmp_path / "gate.json"
            resume_path.write_text(
                "Skills: Python, SQL\nExperience: 2 years\n",
                encoding="utf-8",
            )

            doctor_exit = cli_main(
                [
                    "context-doctor",
                    "--config",
                    str(config_path),
                    "--context-doc",
                    str(resume_path),
                    "--output",
                    str(preferences_path),
                ]
            )
            scheduled_exit = cli_main(
                [
                    "scheduled-run",
                    "--config",
                    str(config_path),
                    "--context-doc",
                    str(resume_path),
                    "--context-doc",
                    str(preferences_path),
                    "--run-date",
                    "2026-06-30",
                    "--quality-gate-output",
                    str(gate_path),
                ]
            )
            gate = json.loads(gate_path.read_text(encoding="utf-8"))

        self.assertEqual(doctor_exit, 0)
        self.assertEqual(scheduled_exit, 0)
        self.assertEqual(gate["context_status"], "complete")
        self.assertEqual(gate["missing_context"], [])


class SupplementalInterviewTests(unittest.TestCase):
    def test_missing_context_generates_questions_and_answers_merge(self) -> None:
        context = UserContext(desired_roles=[], skills=[], preferred_locations=[], max_experience_years=0)

        questions = supplemental_questions(context)
        merged = merge_supplemental_answers(
            context,
            {
                "desired_roles": "ML Engineer",
                "skills": "Python, SQL",
                "preferred_locations": "Seoul",
                "max_experience_years": "2",
            },
        )

        self.assertGreaterEqual(len(questions), 4)
        self.assertEqual(merged.skills, ["Python", "SQL"])
        self.assertEqual(merged.provenance["skills"], "supplemental_interview")

    def test_invalid_experience_answer_is_rejected_at_the_interview_boundary(self) -> None:
        context = UserContext(desired_roles=[], skills=[], preferred_locations=[], max_experience_years=0)

        with self.assertRaisesRegex(UserContextImportError, "maximum experience"):
            merge_supplemental_answers(context, {"max_experience_years": "999"})
