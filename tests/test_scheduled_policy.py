from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

import io
import json
import tempfile
from contextlib import redirect_stdout
from datetime import date
from unittest.mock import patch

from recruit_crawler.cli import main as cli_main
from recruit_crawler.config import load_config
from recruit_crawler.scheduled import ScheduledRunRequest, run_scheduled_job

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
        self.assertFalse(gate["report_generated"])
        self.assertEqual(gate["context_status"], "needs_context")
        self.assertEqual(set(gate["missing_context"]), {"skills", "max_experience_years"})
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
        self.assertFalse(gate["report_generated"])
        self.assertEqual(gate["status"], "fail")
        self.assertEqual(gate["source_policy"][0]["scheduled_action"], "skip")
        self.assertEqual(gate["source_policy"][0]["prohibited_options"], ["manual_postings"])
        self.assertTrue(
            any("scheduled-run source policy rejected enabled source" in item["message"] for item in gate["findings"])
        )

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
        self.assertFalse(gate["report_generated"])
        self.assertEqual(gate["status"], "fail")
        self.assertEqual(gate["context_status"], "needs_context")
        self.assertEqual(set(gate["missing_context"]), {"skills", "max_experience_years"})
        self.assertNotIn("Supplemental context interview", output.getvalue())
        self.assertIn("Scheduled run blocked", output.getvalue())
        self.assertIn("Report written: not generated", output.getvalue())
        self.assertIn("Missing context: skills, max_experience_years", output.getvalue())

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
        self.assertFalse(gate["report_generated"])
        self.assertEqual(gate["status"], "fail")
        self.assertIn("Scheduled run blocked", output.getvalue())
        self.assertEqual(gate["source_policy"][0]["access_mode"], "manual")
        self.assertEqual(gate["source_policy"][0]["prohibited_options"], ["manual_postings"])
        self.assertTrue(
            any("scheduled-run source policy rejected enabled source" in item["message"] for item in gate["findings"])
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


if __name__ == "__main__":
    unittest.main()
