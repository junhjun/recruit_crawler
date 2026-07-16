from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Dict, List, TypedDict, Union

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from recruit_crawler.config import ConfigError, load_config
from recruit_crawler.cli import build_parser, main as cli_main
from recruit_crawler.scheduled import scheduled_source_policy
from recruit_crawler.report_policy import verified_link_url
from recruit_crawler.source_registry import CAPTURE_ONLY_LINKEDIN

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
JsonScalar = Union[str, int, float, bool, None]
JsonValue = Union[JsonScalar, List["JsonValue"], Dict[str, "JsonValue"]]


class RawSource(TypedDict, total=False):
    source_id: str
    enabled: bool
    access_mode: str
    target_status: str
    target_lane: str | None
    automation_level: str
    tos_review_status: str
    adapter_code_path: str
    test_refs: List[str]
    docs_refs: List[str]
    options: Dict[str, JsonValue]


class RawConfig(TypedDict):
    sources: List[RawSource]


class SourcePolicyTests(unittest.TestCase):
    def _live_config(self) -> RawConfig:
        return json.loads((ROOT / "config" / "live_sources.sample.json").read_text(encoding="utf-8"))

    def _write_temp_config(self, raw: RawConfig, tmp_path: Path) -> Path:
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
            expected_fields = (
                CAPTURE_ONLY_LINKEDIN.allowed_persisted_fields
                if source_id == "linkedin"
                else []
            )
            self.assertEqual(manifest.allowed_persisted_fields, expected_fields)

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
    def test_company_careers_enablement_is_rejected(self) -> None:
        raw = self._live_config()
        company = next(
            source for source in raw["sources"] if source["source_id"] == "company_careers"
        )
        company.update(
            {
                "enabled": True,
                "target_status": "enabled",
                "maintenance_status": "active",
                "target_lane": "public_http",
                "automation_level": "no_human",
            }
        )
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaisesRegex(ConfigError, "company-careers collection"):
                load_config(self._write_temp_config(raw, Path(tmp)), allow_real_sources=True)

    def test_legacy_shaped_live_source_is_rejected(self) -> None:
        raw: RawConfig = {
            "sources": [
                {
                    "source_id": "legacy_live",
                    "enabled": True,
                    "access_mode": "public_page",
                }
            ]
        }
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(ConfigError):
                load_config(self._write_temp_config(raw, Path(tmp)), allow_real_sources=True)
    def test_live_registry_rejects_noncanonical_and_unapproved_source_ids(self) -> None:
        for source_id in ("linkedin ", "company_careers ", "new_company_careers"):
            with self.subTest(source_id=source_id):
                raw = self._live_config()
                source = raw["sources"][0]
                source.update(
                    {
                        "source_id": source_id,
                        "enabled": True,
                        "target_status": "enabled",
                        "maintenance_status": "active",
                        "target_lane": "public_http",
                        "automation_level": "no_human",
                        "tos_review_status": "pass",
                    }
                )
                with tempfile.TemporaryDirectory() as tmp:
                    with self.assertRaises(ConfigError):
                        load_config(
                            self._write_temp_config(raw, Path(tmp)), allow_real_sources=True
                        )
    def test_saramin_outer_report_links_require_exact_canonical_endpoint(self) -> None:
        outer_url = "https://www.saramin.co.kr/zf_user/jobs/relay/view?rec_idx=54106686&rec_seq=0"
        self.assertEqual(
            verified_link_url(
                "scheduled-run", "saramin", outer_url, "54106686", "verified"
            ),
            outer_url,
        )
        invalid_links = (
            "https://www.saramin.co.kr/zf_user/jobs/relay/view",
            "https://www.saramin.co.kr/zf_user/jobs/relay/view?rec_idx=54106687&rec_seq=0",
            "https://www.saramin.co.kr/zf_user/jobs/relay/view?rec_idx=54106686&rec_seq=0&next=1",
            "http://www.saramin.co.kr/zf_user/jobs/relay/view?rec_idx=54106686&rec_seq=0",
            "https://www.saramin.co.kr.evil/zf_user/jobs/relay/view?rec_idx=54106686&rec_seq=0",
        )
        for source_url in invalid_links:
            with self.subTest(source_url=source_url):
                self.assertIsNone(
                    verified_link_url(
                        "scheduled-run", "saramin", source_url, "54106686", "verified"
                    )
                )
        self.assertEqual(
            verified_link_url(
                "scheduled-run",
                "saramin",
                "https://www.saramin.co.kr/zf_user/jobs/relay/view-detail",
                "54106686",
                "verified",
            ),
            "https://www.saramin.co.kr/zf_user/jobs/relay/view-detail?rec_idx=54106686&rec_seq=0",
        )
    def test_saramin_probe_registration_keeps_arguments_under_diagnostic_handler(self) -> None:
        args = build_parser().parse_args(
            [
                "saramin-strategy-probe",
                "--authorized-live-probe",
                "--rec-idx",
                "1",
                "--rec-idx",
                "2",
                "--rec-idx",
                "3",
                "--output-dir",
                "/tmp/recruit-crawler-saramin-probe-test",
            ]
        )
        self.assertEqual(args.command, "saramin-strategy-probe")
        self.assertTrue(args.authorized_live_probe)
        self.assertEqual(args.rec_idx, ["1", "2", "3"])
        self.assertEqual(cli_main(["saramin-strategy-probe", "--rec-idx", "1", "--rec-idx", "2", "--rec-idx", "3", "--output-dir", "/tmp/not-approved"]), 64)
