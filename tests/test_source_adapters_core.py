from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from recruit_crawler.schemas import SourceManifest
from recruit_crawler.sources.base import build_source_adapter
from recruit_crawler.sources.platforms import (
    CompanyCareersAdapter,
    JobKoreaAdapter,
    JumpitAdapter,
    LinkedInAdapter,
    RallitAdapter,
    RocketPunchBrowserAutomationAdapter,
    SaraminAdapter,
    WantedAdapter,
    known_platform_ids,
)


class SourceAdapterRegistryTests(unittest.TestCase):
    def test_known_platforms_use_platform_specific_adapters(self) -> None:
        expected = {
            "company_careers": CompanyCareersAdapter,
            "jumpit": JumpitAdapter,
            "saramin": SaraminAdapter,
            "jobkorea": JobKoreaAdapter,
            "wanted": WantedAdapter,
            "linkedin": LinkedInAdapter,
            "rallit": RallitAdapter,
            "rocketpunch": RocketPunchBrowserAutomationAdapter,
        }
        for source_id, adapter_class in expected.items():
            manifest = SourceManifest(
                source_id=source_id,
                enabled=True,
                access_mode="public_page" if source_id != "linkedin" else "api",
                auth_required=source_id == "linkedin",
                tos_review_status="pass",
                domains=["example.com"],
                rate_limit="1 request / second",
                failure_mode="skip_source",
                allowed_persisted_fields=[],
                options={},
            )

            adapter = build_source_adapter(manifest, ROOT / "fixtures" / "postings.json")

            self.assertIsInstance(adapter, adapter_class)

        self.assertEqual(known_platform_ids(), sorted(expected))

    def test_build_source_adapters_initializes_empty_typed_issue_lists(self) -> None:
        source_ids = [
            "company_careers",
            "jumpit",
            "saramin",
            "jobkorea",
            "wanted",
            "linkedin",
            "rallit",
            "rocketpunch",
        ]
        for source_id in source_ids:
            with self.subTest(source_id=source_id):
                manifest = SourceManifest(
                    source_id=source_id,
                    enabled=True,
                    access_mode="public_page" if source_id != "linkedin" else "api",
                    auth_required=source_id == "linkedin",
                    tos_review_status="pass",
                    domains=["example.com"],
                    rate_limit="1 request / second",
                    failure_mode="skip_source",
                    allowed_persisted_fields=[],
                    options={},
                )

                adapter = build_source_adapter(manifest, ROOT / "fixtures" / "postings.json")

                self.assertIsInstance(adapter.issues, list)
                self.assertEqual(adapter.issues, [])


if __name__ == "__main__":
    unittest.main()
