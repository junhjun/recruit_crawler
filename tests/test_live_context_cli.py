from __future__ import annotations

import io
import json
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from recruit_crawler.cli import main as cli_main

CONFIG = ROOT / "config" / "sample_config.json"


def write_missing_context_config(tmp_path: Path) -> Path:
    raw = json.loads(CONFIG.read_text(encoding="utf-8"))
    fixture_path = tmp_path / "postings.json"
    raw["fixture_path"] = str(fixture_path)
    raw["output_dir"] = str(tmp_path / "reports")
    raw["profile"] = {
        "desired_roles": [],
        "skills": [],
        "preferred_locations": [],
        "max_experience_years": 0,
        "exclusions": [],
    }
    fixture_path.write_text((ROOT / "fixtures" / "postings.json").read_text(encoding="utf-8"), encoding="utf-8")
    config_path = tmp_path / "config.json"
    config_path.write_text(json.dumps(raw), encoding="utf-8")
    return config_path


class LiveContextCliTests(unittest.TestCase):
    def test_live_run_missing_context_is_noninteractive_quality_failure(self) -> None:
        output = io.StringIO()
        with tempfile.TemporaryDirectory() as tmp, redirect_stdout(output), patch(
            "builtins.input",
            side_effect=AssertionError("live-run must not prompt without explicit interview flag"),
        ):
            tmp_path = Path(tmp)
            config_path = write_missing_context_config(tmp_path)
            gate_path = tmp_path / "live_quality_gate.json"

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
        self.assertEqual(gate["context_status"], "needs_context")
        self.assertTrue(gate["findings"])
        self.assertEqual(set(gate["findings"][0]), {"severity", "source_id", "message"})
        self.assertEqual(gate["findings"][0]["severity"], "fail")
        self.assertEqual(gate["sources_attempted"], [])
        self.assertNotIn("Supplemental context interview", output.getvalue())
        self.assertIn("Live run blocked", output.getvalue())

    def test_live_run_interview_flag_fills_missing_context(self) -> None:
        output = io.StringIO()
        with tempfile.TemporaryDirectory() as tmp, redirect_stdout(output), patch(
            "builtins.input",
            side_effect=["Python, SQL", "2"],
        ):
            tmp_path = Path(tmp)
            config_path = write_missing_context_config(tmp_path)
            context_path = tmp_path / "partial.md"
            context_path.write_text("Roles: Systems Engineer\nLocations: Seoul\n", encoding="utf-8")
            gate_path = tmp_path / "live_quality_gate.json"

            exit_code = cli_main(
                [
                    "live-run",
                    "--config",
                    str(config_path),
                    "--run-date",
                    "2026-06-30",
                    "--context-doc",
                    str(context_path),
                    "--interview-missing-context",
                    "--quality-gate-output",
                    str(gate_path),
                ]
            )
            gate = json.loads(gate_path.read_text(encoding="utf-8"))

        self.assertEqual(exit_code, 0)
        self.assertEqual(gate["status"], "pass")
        self.assertEqual(gate["context_status"], "complete")
        self.assertNotIn("missing_context", gate)
        self.assertEqual(gate["findings"], [])
        self.assertIn("Supplemental context interview:", output.getvalue())

    def test_live_run_print_report_matches_persisted_utf8_bytes(self) -> None:
        output = io.StringIO()
        diagnostics = io.StringIO()
        with tempfile.TemporaryDirectory() as tmp, redirect_stdout(output), redirect_stderr(diagnostics):
            tmp_path = Path(tmp)
            config_path = write_missing_context_config(tmp_path)
            context_path = tmp_path / "complete.md"
            context_path.write_text(
                "Roles: ML Engineer\nSkills: Python, machine learning\n"
                "Locations: Seoul\nExperience: 2 years\n",
                encoding="utf-8",
            )

            exit_code = cli_main(
                [
                    "live-run",
                    "--config",
                    str(config_path),
                    "--run-date",
                    "2026-06-30",
                    "--context-doc",
                    str(context_path),
                    "--print-report",
                ]
            )
            report_line = next(
                line for line in diagnostics.getvalue().splitlines() if line.startswith("Report written: ")
            )
            report_path = Path(report_line.partition(":")[2].strip())
            report_bytes = report_path.read_bytes()

        self.assertEqual(exit_code, 0)
        self.assertEqual(output.getvalue().encode("utf-8"), report_bytes)
        self.assertTrue(report_bytes.endswith(b"\n"))
        self.assertIn("Report written:", diagnostics.getvalue())
        self.assertIn("Live-run quality gate status: pass", diagnostics.getvalue())


if __name__ == "__main__":
    unittest.main()
