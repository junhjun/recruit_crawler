from __future__ import annotations

import io
import json
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from recruit_crawler.cli import main as cli_main
from recruit_crawler.status_report import build_progress_brief, check_status_report, iter_feature_refs, load_feature_ledger, write_status_report

KNOWN_ENTRYPOINT_PREFIXES = (
    "recruit-crawler ",
    "--",
    "internal ",
    "browser extension ",
    "live-run ",
)
KNOWN_ENTRYPOINTS = {
    "browser-evidence",
    "capture-import",
    "config allowed_persisted_fields",
}


class RepositoryHarnessTests(unittest.TestCase):
    def test_todo_contains_only_open_backlog_items(self) -> None:
        todo = (ROOT / "TODO.md").read_text(encoding="utf-8")

        self.assertNotIn("- [x]", todo, "TODO.md should contain only future work")

    def test_status_report_is_current(self) -> None:
        result = check_status_report(
            config_path=ROOT / "config" / "live_sources.sample.json",
            features_path=ROOT / "docs" / "status" / "features.json",
            output_path=ROOT / "docs" / "status.md",
            todo_path=ROOT / "TODO.md",
        )

        self.assertTrue(result.ok, result.message)

    def test_status_report_write_refreshes_feature_ledger_date(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            features_path = tmp_path / "features.json"
            output_path = tmp_path / "status.md"
            source_features_path = ROOT / "docs" / "status" / "features.json"
            ledger = json.loads(source_features_path.read_text(encoding="utf-8"))
            ledger["updated_at"] = "1999-01-01"
            features_path.write_text(json.dumps(ledger, ensure_ascii=False), encoding="utf-8")

            content = write_status_report(
                config_path=ROOT / "config" / "live_sources.sample.json",
                features_path=features_path,
                output_path=output_path,
                todo_path=ROOT / "TODO.md",
            )

            refreshed = json.loads(features_path.read_text(encoding="utf-8"))
            self.assertNotEqual(refreshed["updated_at"], "1999-01-01")
            self.assertIn(f"상태일: {refreshed['updated_at']}", content)
            self.assertEqual(output_path.read_text(encoding="utf-8"), content)

    def test_progress_brief_is_token_minimal_status_harness(self) -> None:
        brief = build_progress_brief(
            features_path=ROOT / "docs" / "status" / "features.json",
            todo_path=ROOT / "TODO.md",
            max_items=2,
        )

        self.assertLessEqual(len(brief.lines), 14)
        self.assertIn("features: total=", brief.text)
        self.assertIn("open_todos:", brief.text)
        self.assertIn("verify: PYTHONPATH=src python3 -m recruit_crawler.cli status-report --check", brief.text)
        self.assertNotIn("archive", brief.text.lower())

    def test_status_report_brief_cli_prints_concise_progress(self) -> None:
        output = io.StringIO()

        with redirect_stdout(output):
            exit_code = cli_main(["status-report", "--brief"])

        self.assertEqual(exit_code, 0)
        text = output.getvalue()
        self.assertIn("features: total=", text)
        self.assertIn("next_todos:", text)
        self.assertNotIn("| 기능 | 상태 |", text)

    def test_feature_status_refs_resolve_to_files_symbols_or_cli_commands(self) -> None:
        ledger = load_feature_ledger(ROOT / "docs" / "status" / "features.json")

        for feature_id, field, ref in iter_feature_refs(ledger, ("code_refs", "test_refs", "docs_refs")):
            with self.subTest(feature_id=feature_id, field=field, ref=ref):
                if field == "test_refs":
                    path_text, symbol = ref.split("::", 1)
                    self.assertTrue(path_text.startswith("tests/test_"), ref)
                    self.assertTrue(symbol.startswith("test_"), ref)
                    path = ROOT / path_text
                    self.assertTrue(path.exists(), ref)
                    self.assertIn(f"def {symbol}(", path.read_text(encoding="utf-8"), ref)
                    continue
                path_text, _, symbol = ref.partition("::")
                path = ROOT / path_text
                self.assertTrue(path.exists(), ref)
                if symbol:
                    self.assertIn(symbol, path.read_text(encoding="utf-8"), ref)

    def test_feature_entrypoints_are_files_or_intentional_command_refs(self) -> None:
        ledger = load_feature_ledger(ROOT / "docs" / "status" / "features.json")

        for feature in ledger["features"]:
            for entrypoint in feature["entrypoints"]:
                with self.subTest(feature_id=feature["feature_id"], entrypoint=entrypoint):
                    if any(entrypoint.startswith(prefix) for prefix in KNOWN_ENTRYPOINT_PREFIXES):
                        continue
                    if entrypoint in KNOWN_ENTRYPOINTS:
                        continue
                    if "*" in entrypoint:
                        self.assertTrue(entrypoint.startswith("reports/"), entrypoint)
                        continue
                    first_token = entrypoint.split(" ", 1)[0]
                    if (ROOT / first_token).exists():
                        continue
                    self.assertTrue((ROOT / entrypoint).exists(), entrypoint)

    def test_setup_checks_require_live_run_quality_gate(self) -> None:
        readme = (ROOT / "README.md").read_text(encoding="utf-8")

        self.assertIn("recruit_crawler.cli live-run", readme)
        self.assertIn("--config config/live_sources.sample.json", readme)
        self.assertIn("--quality-gate-output artifacts/scheduled/live_quality_gate.json", readme)
