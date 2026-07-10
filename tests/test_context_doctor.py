from __future__ import annotations

import io
import json
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from recruit_crawler.cli import main as cli_main

CONFIG = ROOT / "config" / "sample_config.json"


def _write_two_posting_config(tmp_path: Path) -> Path:
    raw = json.loads(CONFIG.read_text(encoding="utf-8"))
    fixture_path = tmp_path / "postings.json"
    raw["fixture_path"] = str(fixture_path)
    raw["output_dir"] = str(tmp_path / "reports")
    raw["top_n"] = 2
    fixture_path.write_text("[]", encoding="utf-8")
    config_path = tmp_path / "config.json"
    config_path.write_text(json.dumps(raw), encoding="utf-8")
    return config_path


class ContextDoctorTests(unittest.TestCase):
    def test_context_doctor_writes_only_interview_preferences_and_preserves_korean_locations(self) -> None:
        output = io.StringIO()
        with tempfile.TemporaryDirectory() as tmp, redirect_stdout(output), patch(
            "builtins.input",
            side_effect=[
                "ML Engineer, AI Engineer",
                "Python, PyTorch",
                "서울, 판교, 원격/하이브리드 상관없음",
                "3",
                "",
            ],
        ):
            tmp_path = Path(tmp)
            config_path = _write_two_posting_config(tmp_path)
            resume_path = tmp_path / "resume.md"
            preferences_path = tmp_path / "preferences.md"
            resume_path.write_text(
                "Skills: Python, SQL\nExperience: 2 years\n",
                encoding="utf-8",
            )

            exit_code = cli_main(
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
            preferences = preferences_path.read_text(encoding="utf-8")

        self.assertEqual(exit_code, 0)
        stdout = output.getvalue()
        self.assertIn("Context preferences written:", stdout)
        self.assertIn("Roles: ML Engineer, AI Engineer", preferences)
        self.assertIn("Skills: Python, PyTorch", preferences)
        self.assertIn("Locations: 서울, 판교, 원격/하이브리드 상관없음", preferences)
        self.assertIn("Experience: 3 years", preferences)
        self.assertNotIn("Skills: Python, SQL", preferences)
        self.assertNotIn("Deal breakers:", preferences)

    def test_context_doctor_preserves_existing_preferences_when_only_location_is_missing(self) -> None:
        output = io.StringIO()
        with tempfile.TemporaryDirectory() as tmp, redirect_stdout(output), patch(
            "builtins.input",
            side_effect=["서울, 판교", ""],
        ):
            tmp_path = Path(tmp)
            config_path = _write_two_posting_config(tmp_path)
            resume_path = tmp_path / "resume.md"
            preferences_path = tmp_path / "preferences.md"
            resume_path.write_text(
                "Skills: Python, SQL\nExperience: 2 years\n",
                encoding="utf-8",
            )
            preferences_path.write_text(
                "Roles: AI Engineer\n"
                "Skills: Python, SQL\n"
                "Experience: 2 years\n",
                encoding="utf-8",
            )

            exit_code = cli_main(
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
            preferences = preferences_path.read_text(encoding="utf-8")

        self.assertEqual(exit_code, 0)
        self.assertIn("Context preferences written:", output.getvalue())
        self.assertIn("Roles: AI Engineer", preferences)
        self.assertIn("Locations: 서울, 판교", preferences)
        self.assertNotIn("Skills:", preferences)
        self.assertNotIn("Experience:", preferences)

    def test_context_doctor_removes_redundant_inferred_fields_from_existing_preferences(self) -> None:
        output = io.StringIO()
        with tempfile.TemporaryDirectory() as tmp, redirect_stdout(output), patch(
            "builtins.input",
            side_effect=["", "", "", ""],
        ):
            tmp_path = Path(tmp)
            config_path = _write_two_posting_config(tmp_path)
            resume_path = tmp_path / "resume.md"
            preferences_path = tmp_path / "preferences.md"
            resume_path.write_text(
                "Skills: Python, SQL\nExperience: 2 years\n",
                encoding="utf-8",
            )
            preferences_path.write_text(
                "Roles: AI Engineer\n"
                "Skills: Python, SQL\n"
                "Locations: 서울, 판교\n"
                "Experience: 2 years\n",
                encoding="utf-8",
            )

            exit_code = cli_main(
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
            preferences = preferences_path.read_text(encoding="utf-8")

        self.assertEqual(exit_code, 0)
        self.assertIn("Context preferences written:", output.getvalue())
        self.assertIn("Roles: AI Engineer", preferences)
        self.assertIn("Locations: 서울, 판교", preferences)
        self.assertNotIn("Skills:", preferences)
        self.assertNotIn("Experience:", preferences)

    def test_context_doctor_does_not_infer_skills_from_existing_preference_roles(self) -> None:
        output = io.StringIO()
        with tempfile.TemporaryDirectory() as tmp, redirect_stdout(output), patch(
            "builtins.input",
            side_effect=["", "", "", ""],
        ):
            tmp_path = Path(tmp)
            config_path = _write_two_posting_config(tmp_path)
            context_path = tmp_path / "context.md"
            preferences_path = tmp_path / "preferences.md"
            context_path.write_text(
                "Locations: 서울\nExperience: 2 years\n",
                encoding="utf-8",
            )
            preferences_path.write_text(
                "Roles: ML Engineer\n",
                encoding="utf-8",
            )

            exit_code = cli_main(
                [
                    "context-doctor",
                    "--config",
                    str(config_path),
                    "--context-doc",
                    str(context_path),
                    "--output",
                    str(preferences_path),
                ]
            )
            preferences = preferences_path.read_text(encoding="utf-8")

        self.assertEqual(exit_code, 1)
        self.assertIn("Context preferences written:", output.getvalue())
        self.assertIn("Context status: needs_context", output.getvalue())
        self.assertIn("Still missing context: skills", output.getvalue())
        self.assertNotIn("Filled context fields: skills", output.getvalue())
        self.assertIn("Roles: ML Engineer", preferences)
        self.assertNotIn("Skills:", preferences)
