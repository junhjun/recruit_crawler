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

from recruit_crawler.cli import main as cli_main
from recruit_crawler.config import load_config
from recruit_crawler.scheduled import ScheduledRunRequest, run_scheduled_job

CONFIG = ROOT / "config" / "sample_config.json"


class ScheduledRunCliTests(unittest.TestCase):
    def _write_scheduled_config(self, tmp_path: Path) -> Path:
        raw = json.loads(CONFIG.read_text(encoding="utf-8"))
        fixture_path = tmp_path / "postings.json"
        raw["fixture_path"] = str(fixture_path)
        raw["output_dir"] = str(tmp_path / "reports")
        fixture_path.write_text((ROOT / "fixtures" / "postings.json").read_text(encoding="utf-8"), encoding="utf-8")
        config_path = tmp_path / "scheduled_config.json"
        config_path.write_text(json.dumps(raw), encoding="utf-8")
        return config_path

    def test_scheduled_run_writes_contract_quality_gate(self) -> None:
        output = io.StringIO()
        with tempfile.TemporaryDirectory() as tmp, redirect_stdout(output):
            tmp_path = Path(tmp)
            config_path = self._write_scheduled_config(tmp_path)
            gate_path = tmp_path / "scheduled_quality_gate.json"
            db_path = tmp_path / "recruit.sqlite3"

            exit_code = cli_main(
                [
                    "scheduled-run",
                    "--config",
                    str(config_path),
                    "--run-date",
                    "2026-06-30",
                    "--output-dir",
                    str(tmp_path / "scheduled_reports"),
                    "--quality-gate-output",
                    str(gate_path),
                    "--db-path",
                    str(db_path),
                ]
            )
            gate = json.loads(gate_path.read_text(encoding="utf-8"))

        self.assertEqual(exit_code, 0)
        self.assertEqual(gate["command_mode"], "scheduled-run")
        self.assertEqual(gate["status"], "pass")
        self.assertEqual(gate["context_status"], "complete")
        self.assertEqual(gate["missing_context"], [])
        self.assertEqual(gate["db_path"]["name"], db_path.name)
        self.assertEqual(len(gate["db_path"]["path_hash"]), 64)
        self.assertTrue(gate["report_generated"])
        self.assertTrue(any(row["scheduled_action"] == "run" for row in gate["source_policy"]))
        self.assertEqual(len(gate["run_identity"]["run_id"]), 24)
        self.assertEqual(gate["run_identity"]["command_mode"], "scheduled-run")
        self.assertEqual(gate["run_identity"]["run_date"], "2026-06-30")
        self.assertIn("Scheduled run complete", output.getvalue())
        self.assertIn("Quality gate status: pass", output.getvalue())
        self.assertIn("recruiting-scheduled-run-2026-06-30.md", output.getvalue())
        self.assertIn(db_path.name, output.getvalue())
        self.assertNotIn(str(db_path), output.getvalue())

    def test_scheduled_service_runs_without_cli_argparse(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            config_path = self._write_scheduled_config(tmp_path)
            gate_path = tmp_path / "service_quality_gate.json"
            db_path = tmp_path / "service.sqlite3"
            result = run_scheduled_job(
                ScheduledRunRequest(
                    config=load_config(config_path, allow_real_sources=True),
                    run_date=date(2026, 6, 30),
                    quality_gate_output=gate_path,
                    output_dir=tmp_path / "service_reports",
                    db_path=db_path,
                )
            )
            gate = json.loads(gate_path.read_text(encoding="utf-8"))
            db_exists = db_path.exists()

        self.assertEqual(result.exit_code, 0)
        self.assertEqual(result.gate["run_identity"], gate["run_identity"])
        self.assertEqual(gate["command_mode"], "scheduled-run")
        self.assertEqual(gate["status"], "pass")
        self.assertTrue(gate["report_generated"])
        self.assertTrue(db_exists)
        self.assertIn("Scheduled run complete", result.stdout_lines)
        self.assertTrue(any(line.startswith("Quality gate written: ") for line in result.stdout_lines))

    def test_scheduled_run_rerun_reuses_stable_identity_and_artifacts(self) -> None:
        output = io.StringIO()
        with tempfile.TemporaryDirectory() as tmp, redirect_stdout(output):
            tmp_path = Path(tmp)
            config_path = self._write_scheduled_config(tmp_path)
            gate_path = tmp_path / "scheduled_quality_gate.json"
            output_dir = tmp_path / "scheduled_reports"
            argv = [
                "scheduled-run",
                "--config",
                str(config_path),
                "--run-date",
                "2026-06-30",
                "--output-dir",
                str(output_dir),
                "--quality-gate-output",
                str(gate_path),
            ]

            first_exit = cli_main(argv)
            first_gate = json.loads(gate_path.read_text(encoding="utf-8"))
            report_path = output_dir / "recruiting-scheduled-run-2026-06-30.md"
            first_report_mtime = report_path.stat().st_mtime_ns

            second_exit = cli_main(argv)
            second_gate = json.loads(gate_path.read_text(encoding="utf-8"))
            report_exists = report_path.exists()
            second_report_mtime = report_path.stat().st_mtime_ns

        self.assertEqual(first_exit, 0)
        self.assertEqual(second_exit, 0)
        self.assertEqual(first_gate["run_identity"], second_gate["run_identity"])
        self.assertEqual(first_gate["run_identity"]["run_id"], second_gate["run_identity"]["run_id"])
        self.assertTrue(report_exists)
        self.assertGreaterEqual(second_report_mtime, first_report_mtime)
        self.assertEqual(first_gate["sources_attempted"], second_gate["sources_attempted"])


if __name__ == "__main__":
    unittest.main()
