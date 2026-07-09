from __future__ import annotations

import io
import json
import sys
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from recruit_crawler.cli import main as cli_main
from recruit_crawler.config import load_config
from recruit_crawler.source_registry import source_status_rows

SOURCE_STATUS_ROW_KEYS = {
    "source_id",
    "display_name",
    "enabled",
    "v1_role",
    "target_status",
    "maintenance_status",
    "target_lane",
    "candidate_lanes",
    "automation_level",
    "status_reason",
    "evidence",
    "blockers",
    "next_action",
    "adapter_code_path",
    "test_refs",
    "docs_refs",
    "policy_override_mode",
}
EXPECTED_SOURCE_IDS = {
    "company_careers",
    "saramin",
    "jobkorea",
    "wanted",
    "jumpit",
    "rallit",
    "rocketpunch",
    "linkedin",
    "naver_careers",
    "kakao_careers",
    "line_careers",
    "coupang_careers",
}
ENABLED_SOURCE_IDS = {"jobkorea", "jumpit", "rallit", "rocketpunch", "saramin", "wanted"}
EXCLUDED_SOURCE_IDS = EXPECTED_SOURCE_IDS - ENABLED_SOURCE_IDS


class SourceStatusRowsTests(unittest.TestCase):
    def test_source_status_rows_have_stable_shape_for_status_surfaces(self) -> None:
        config = load_config(ROOT / "config" / "live_sources.sample.json", allow_real_sources=True)

        rows = source_status_rows(config.sources)

        self.assertEqual([row["source_id"] for row in rows], [source.source_id for source in config.sources])
        by_id = {row["source_id"]: row for row in rows}
        self.assertEqual(set(by_id), EXPECTED_SOURCE_IDS)
        for row in rows:
            with self.subTest(source_id=row["source_id"]):
                self.assertEqual(set(row), SOURCE_STATUS_ROW_KEYS)
                self.assertIsInstance(row["source_id"], str)
                self.assertIsInstance(row["display_name"], str)
                self.assertIsInstance(row["enabled"], bool)
                self.assertIn(row["target_status"], {"enabled", "blocked", "deferred", "excluded"})
                self.assertIn(row["maintenance_status"], {"active", "watch", "blocked", "excluded"})
                self.assertIn(row["target_lane"], {"public_http", "browser_automation", None})
                for list_field in ("candidate_lanes", "evidence", "blockers", "test_refs", "docs_refs"):
                    self.assertIsInstance(row[list_field], list)
                    self.assertTrue(all(isinstance(item, str) for item in row[list_field]), list_field)
        for source_id in ENABLED_SOURCE_IDS:
            self.assertTrue(by_id[source_id]["enabled"], source_id)
            self.assertTrue(by_id[source_id]["adapter_code_path"], source_id)
            self.assertTrue(by_id[source_id]["test_refs"], source_id)
            self.assertTrue(by_id[source_id]["docs_refs"], source_id)
        for source_id in EXCLUDED_SOURCE_IDS:
            self.assertFalse(by_id[source_id]["enabled"], source_id)
            self.assertIsNone(by_id[source_id]["target_lane"], source_id)
            self.assertEqual(by_id[source_id]["target_status"], "excluded")


class SourceStatusCliTests(unittest.TestCase):
    def test_source_status_json_outputs_registry_without_network_or_adapter_construction(self) -> None:
        stdout = io.StringIO()
        with patch("recruit_crawler.sources.base.build_source_adapter") as build_adapter:
            with redirect_stdout(stdout):
                exit_code = cli_main(
                    [
                        "source-status",
                        "--config",
                        str(ROOT / "config" / "live_sources.sample.json"),
                        "--json",
                    ]
                )

        self.assertEqual(exit_code, 0)
        build_adapter.assert_not_called()
        payload = json.loads(stdout.getvalue())
        by_id = {source["source_id"]: source for source in payload["sources"]}
        self.assertEqual(set(by_id), EXPECTED_SOURCE_IDS)
        self.assertEqual(payload["sources"][-1]["source_id"], "coupang_careers")
        for source in by_id.values():
            self.assertEqual(set(source), SOURCE_STATUS_ROW_KEYS)
            self.assertIn(source["target_lane"], {"public_http", "browser_automation", None})
        self.assertEqual(by_id["jumpit"]["target_lane"], "public_http")
        self.assertEqual(by_id["rallit"]["target_lane"], "public_http")
        self.assertEqual(by_id["jobkorea"]["target_lane"], "public_http")
        self.assertEqual(by_id["saramin"]["target_lane"], "public_http")
        self.assertEqual(by_id["wanted"]["target_lane"], "public_http")
        self.assertEqual(by_id["rocketpunch"]["target_lane"], "browser_automation")
        for source_id in EXCLUDED_SOURCE_IDS:
            self.assertIsNone(by_id[source_id]["target_lane"])
            self.assertEqual(by_id[source_id]["target_status"], "excluded")
            self.assertFalse(by_id[source_id]["enabled"])
        self.assertEqual(by_id["rocketpunch"]["target_status"], "enabled")
