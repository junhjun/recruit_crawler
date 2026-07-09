from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from recruit_crawler.config import ConfigError, load_config
from recruit_crawler.scheduled import scheduled_source_policy

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
COMPANY_CAREERS_SOURCE_IDS = {
    "company_careers",
    "naver_careers",
    "kakao_careers",
    "line_careers",
    "coupang_careers",
}


class SourcePolicyTests(unittest.TestCase):
    def _live_config(self) -> dict:
        return json.loads((ROOT / "config" / "live_sources.sample.json").read_text(encoding="utf-8"))

    def _write_temp_config(self, raw: dict, tmp_path: Path) -> Path:
        config_path = tmp_path / "live_sources.json"
        config_path.write_text(json.dumps(raw), encoding="utf-8")
        return config_path

    def test_source_registry_rejects_non_target_target_enablement(self) -> None:
        blocked_options = [
            ("saramin", {"access_mode": "api", "options": {"approved_api_access": True}}),
            ("wanted", {"options": {"manual_postings": [{"title": "Manual"}]}}),
            ("wanted", {"options": {"manual_export_path": "wanted.csv"}}),
            ("wanted", {"options": {"user_operated_chrome_extension": True}}),
            ("wanted", {"options": {"user_operated_browser_use": True}}),
            ("saramin", {"options": {"ocr_required": True}}),
            ("saramin", {"options": {"manual_review_flags": ["본문 OCR 필요"]}}),
            ("linkedin", {"options": {"approved_partner_access": True, "partner_payload_path": "jobs.json"}}),
        ]
        for source_id, override in blocked_options:
            raw = self._live_config()
            for source in raw["sources"]:
                if source["source_id"] == source_id:
                    source.update(
                        {
                            "enabled": True,
                            "target_status": "enabled",
                            "target_lane": "public_http",
                            "automation_level": "no_human",
                            "tos_review_status": "pass",
                            "adapter_code_path": "src/recruit_crawler/sources/platforms.py::Adapter",
                            "test_refs": [
                                "tests/test_saramin_adapter.py::test_saramin_adapter_collects_public_detail_body_without_api"
                            ],
                            "docs_refs": ["docs/source_collection_matrix.md"],
                        }
                    )
                    source.update({key: value for key, value in override.items() if key != "options"})
                    source["options"].update(override.get("options", {}))
            with tempfile.TemporaryDirectory() as tmp:
                with self.assertRaises(ConfigError, msg=source_id):
                    load_config(self._write_temp_config(raw, Path(tmp)), allow_real_sources=True)

    def test_scheduled_source_policy_keeps_excluded_sources_skipped(self) -> None:
        config = load_config(ROOT / "config" / "live_sources.sample.json", allow_real_sources=True)

        rows, findings = scheduled_source_policy(config)
        by_id = {row["source_id"]: row for row in rows}

        self.assertEqual(findings, [])
        for source_id in sorted(ENABLED_SOURCE_IDS):
            self.assertEqual(by_id[source_id]["scheduled_action"], "run")
            self.assertEqual(by_id[source_id]["target_status"], "enabled")
        for source_id in EXCLUDED_SOURCE_IDS:
            self.assertEqual(by_id[source_id]["scheduled_action"], "skip")
            self.assertEqual(by_id[source_id]["target_status"], "excluded")
            manifest = next(source for source in config.sources if source.source_id == source_id)
            self.assertEqual(manifest.allowed_persisted_fields, [])

    def test_registry_allows_user_directed_policy_override_for_browser_automation(self) -> None:
        raw = self._live_config()
        for source in raw["sources"]:
            if source["source_id"] == "rocketpunch":
                source.update(
                    {
                        "enabled": True,
                        "access_mode": "browser_automation",
                        "target_status": "enabled",
                        "target_lane": "browser_automation",
                        "automation_level": "no_human",
                        "tos_review_status": "unknown",
                        "adapter_code_path": "src/recruit_crawler/sources/platforms.py::RocketPunchBrowserAutomationAdapter",
                        "test_refs": [
                            "tests/test_rocketpunch_adapter.py::test_rocketpunch_browser_automation_parses_listing_cards_without_detail_links"
                        ],
                        "docs_refs": ["docs/source_search_logic.md"],
                    }
                )
                source["options"].update(
                    {
                        "policy_override_mode": "user_directed_ignore",
                        "policy_override_reason": "User directed RocketPunch no-human browser automation despite source notice.",
                        "policy_override_acknowledges_source_notice": True,
                    }
                )

        with tempfile.TemporaryDirectory() as tmp:
            config = load_config(self._write_temp_config(raw, Path(tmp)), allow_real_sources=True)

        rocketpunch = next(source for source in config.sources if source.source_id == "rocketpunch")
        self.assertEqual(rocketpunch.target_lane, "browser_automation")
        self.assertEqual(rocketpunch.options["policy_override_mode"], "user_directed_ignore")

    def test_policy_override_requires_explicit_acknowledgement(self) -> None:
        raw = self._live_config()
        for source in raw["sources"]:
            if source["source_id"] == "rocketpunch":
                source.update(
                    {
                        "enabled": True,
                        "access_mode": "browser_automation",
                        "target_status": "enabled",
                        "target_lane": "browser_automation",
                        "automation_level": "no_human",
                        "tos_review_status": "unknown",
                        "adapter_code_path": "src/recruit_crawler/sources/platforms.py::RocketPunchBrowserAutomationAdapter",
                        "test_refs": [
                            "tests/test_rocketpunch_adapter.py::test_rocketpunch_browser_automation_parses_listing_cards_without_detail_links"
                        ],
                        "docs_refs": ["docs/source_search_logic.md"],
                    }
                )
                source["options"].update(
                    {
                        "policy_override_mode": "user_directed_ignore",
                        "policy_override_reason": "User directed RocketPunch no-human browser automation despite source notice.",
                        "policy_override_acknowledges_source_notice": False,
                    }
                )

        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaisesRegex(ConfigError, "requires passed source review"):
                load_config(self._write_temp_config(raw, Path(tmp)), allow_real_sources=True)

    def test_company_careers_sources_are_parked_with_excluded_targets(self) -> None:
        config = load_config(ROOT / "config" / "live_sources.sample.json", allow_real_sources=True)
        parked = [source for source in config.sources if source.source_id in COMPANY_CAREERS_SOURCE_IDS]

        self.assertEqual({source.source_id for source in parked}, COMPANY_CAREERS_SOURCE_IDS)
        for source in parked:
            self.assertFalse(source.enabled)
            self.assertIsNone(source.target_lane)
            self.assertEqual(source.target_status, "excluded")
            self.assertEqual(source.automation_level, "excluded")
            self.assertTrue(source.blockers)
            self.assertFalse(source.auth_required)
