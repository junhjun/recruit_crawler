from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

import io
import json
import sqlite3
import tempfile
from contextlib import redirect_stderr, redirect_stdout

from recruit_crawler.cli import main as cli_main
from recruit_crawler.relevance import feedback_events_from_records, feedback_movement_index

CONFIG = ROOT / "config" / "sample_config.json"


class FeedbackCliTests(unittest.TestCase):
    def _write_scheduled_config(self, tmp_path: Path) -> Path:
        raw = json.loads(CONFIG.read_text(encoding="utf-8"))
        fixture_path = tmp_path / "postings.json"
        raw["fixture_path"] = str(fixture_path)
        raw["output_dir"] = str(tmp_path / "reports")
        fixture_path.write_text((ROOT / "fixtures" / "postings.json").read_text(encoding="utf-8"), encoding="utf-8")
        config_path = tmp_path / "scheduled_config.json"
        config_path.write_text(json.dumps(raw), encoding="utf-8")
        return config_path

    def test_scheduled_run_persists_history_without_duplicate_rows(self) -> None:
        output = io.StringIO()
        with tempfile.TemporaryDirectory() as tmp, redirect_stdout(output):
            tmp_path = Path(tmp)
            config_path = self._write_scheduled_config(tmp_path)
            gate_path = tmp_path / "scheduled_quality_gate.json"
            output_dir = tmp_path / "scheduled_reports"
            db_path = tmp_path / "recruit.sqlite3"
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
                "--db-path",
                str(db_path),
            ]

            first_exit = cli_main(argv)
            second_exit = cli_main(argv)
            with sqlite3.connect(db_path) as connection:
                run_count = connection.execute("SELECT COUNT(*) FROM runs").fetchone()[0]
                source_count = connection.execute("SELECT COUNT(*) FROM source_attempts").fetchone()[0]
                recommendation_count = connection.execute("SELECT COUNT(*) FROM recommendations").fetchone()[0]
                gate_count = connection.execute("SELECT COUNT(*) FROM quality_gates").fetchone()[0]
                schema_version = connection.execute(
                    "SELECT value FROM schema_metadata WHERE key = 'schema_version'"
                ).fetchone()[0]
                persisted_run = connection.execute(
                    "SELECT status, context_status, report_generated, ranked_count FROM runs"
                ).fetchone()
            history_output = io.StringIO()
            with redirect_stdout(history_output):
                history_exit = cli_main(["scheduled-history", "--db-path", str(db_path), "--json"])
            history = json.loads(history_output.getvalue())

        self.assertEqual(first_exit, 0)
        self.assertEqual(second_exit, 0)
        self.assertEqual(history_exit, 0)
        self.assertEqual(run_count, 1)
        self.assertEqual(gate_count, 1)
        self.assertEqual(source_count, 1)
        self.assertGreater(recommendation_count, 0)
        self.assertEqual(schema_version, "4")
        self.assertEqual(tuple(persisted_run[:3]), ("pass", "complete", 1))
        self.assertEqual(persisted_run[3], recommendation_count)
        self.assertEqual(len(history["runs"]), 1)
        self.assertEqual(len(history["recommendations"]), recommendation_count)
        recommendation = history["recommendations"][0]
        for field in (
            "recommendation_id",
            "posting_key",
            "run_id",
            "source_id",
            "source_url",
            "source_posting_id",
            "title",
            "company",
            "location",
            "deadline",
            "score",
            "final_disposition",
            "reason_codes",
            "source_detail_quality",
        ):
            self.assertIn(field, recommendation)
        self.assertNotIn("opaque_identity", recommendation)
        self.assertNotIn("raw_structured", recommendation)
        self.assertNotIn("raw_jd", recommendation)
        self.assertNotIn("PRIVATE_PROFILE_CANARY", json.dumps(history, ensure_ascii=False))

    def test_feedback_add_records_event_for_persisted_recommendation(self) -> None:
        output = io.StringIO()
        with tempfile.TemporaryDirectory() as tmp, redirect_stdout(output):
            tmp_path = Path(tmp)
            config_path = self._write_scheduled_config(tmp_path)
            gate_path = tmp_path / "scheduled_quality_gate.json"
            db_path = tmp_path / "recruit.sqlite3"

            cli_main(
                [
                    "scheduled-run",
                    "--config",
                    str(config_path),
                    "--run-date",
                    "2026-06-30",
                    "--quality-gate-output",
                    str(gate_path),
                    "--db-path",
                    str(db_path),
                ]
            )
            history_output = io.StringIO()
            with redirect_stdout(history_output):
                history_exit = cli_main(["scheduled-history", "--db-path", str(db_path), "--json"])
            history = json.loads(history_output.getvalue())
            recommendation_id = history["recommendations"][0]["recommendation_id"]

            feedback_exit = cli_main(
                [
                    "feedback-add",
                    "--db-path",
                    str(db_path),
                    "--recommendation-id",
                    recommendation_id,
                    "--verdict",
                    "interesting",
                    "--reason",
                    "Good fit for daily review",
                    "--movement",
                    "up",
                    "--created-at",
                    "2026-07-02T00:00:00+00:00",
                ]
            )
            feedback_output = io.StringIO()
            with redirect_stdout(feedback_output):
                export_exit = cli_main(["feedback-export", "--db-path", str(db_path), "--json"])
            payload = json.loads(feedback_output.getvalue())

        self.assertEqual(feedback_exit, 0)
        self.assertEqual(history_exit, 0)
        self.assertEqual(export_exit, 0)
        self.assertEqual(len(payload["feedback"]), 1)
        event = payload["feedback"][0]
        self.assertEqual(event["recommendation_id"], recommendation_id)
        self.assertEqual(event["verdict"], "interesting")
        self.assertEqual(event["movement"], "up")
        self.assertEqual(event["reason"], "Good fit for daily review")
        feedback_events = feedback_events_from_records(payload["feedback"])
        self.assertEqual(event["posting_key"], feedback_events[0].posting_id)
        self.assertEqual(event["source_id"], "fixture")
        self.assertIsNone(event["source_url"])
        movement_index = feedback_movement_index(feedback_events)
        self.assertEqual(movement_index[event["posting_key"]], "up")

    def test_feedback_add_rejects_private_reason_canary(self) -> None:
        private_reason = "PRIVATE_PROFILE_CANARY should not persist"
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            config_path = self._write_scheduled_config(tmp_path)
            gate_path = tmp_path / "scheduled_quality_gate.json"
            db_path = tmp_path / "recruit.sqlite3"

            stdout = io.StringIO()
            stderr = io.StringIO()
            with redirect_stdout(stdout):
                cli_main(
                    [
                        "scheduled-run",
                        "--config",
                        str(config_path),
                        "--run-date",
                        "2026-06-30",
                        "--quality-gate-output",
                        str(gate_path),
                        "--db-path",
                        str(db_path),
                    ]
                )
            with sqlite3.connect(db_path) as connection:
                recommendation_id = connection.execute(
                    "SELECT recommendation_id FROM recommendations ORDER BY score DESC LIMIT 1"
                ).fetchone()[0]

            with redirect_stdout(stdout), redirect_stderr(stderr), self.assertRaises(SystemExit) as cm:
                cli_main(
                    [
                        "feedback-add",
                        "--db-path",
                        str(db_path),
                        "--recommendation-id",
                        recommendation_id,
                        "--verdict",
                        "not_relevant",
                        "--reason",
                        private_reason,
                        "--movement",
                        "down",
                    ]
                )
            feedback_output = io.StringIO()
            with redirect_stdout(feedback_output):
                export_exit = cli_main(["feedback-export", "--db-path", str(db_path), "--json"])
            payload = json.loads(feedback_output.getvalue())
            with sqlite3.connect(db_path) as connection:
                feedback_count = connection.execute("SELECT COUNT(*) FROM feedback_events").fetchone()[0]

        self.assertEqual(cm.exception.code, 3)
        self.assertNotIn(private_reason, stdout.getvalue())
        self.assertNotIn(private_reason, stderr.getvalue())
        self.assertEqual(export_exit, 0)
        self.assertEqual(payload["feedback"], [])
        self.assertEqual(feedback_count, 0)

    def test_feedback_add_unknown_recommendation_exits_without_exported_events(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            db_path = tmp_path / "recruit.sqlite3"
            stderr = io.StringIO()

            with redirect_stderr(stderr), self.assertRaises(SystemExit) as cm:
                cli_main(
                    [
                        "feedback-add",
                        "--db-path",
                        str(db_path),
                        "--recommendation-id",
                        "missing-recommendation",
                        "--verdict",
                        "not_relevant",
                        "--reason",
                        "Not a match",
                    ]
                )
            feedback_output = io.StringIO()
            with redirect_stdout(feedback_output):
                export_exit = cli_main(["feedback-export", "--db-path", str(db_path), "--json"])
            payload = json.loads(feedback_output.getvalue())

        self.assertEqual(cm.exception.code, 2)
        self.assertIn("unknown recommendation_id: missing-recommendation", stderr.getvalue())
        self.assertEqual(export_exit, 0)
        self.assertEqual(payload["feedback"], [])


if __name__ == "__main__":
    unittest.main()
