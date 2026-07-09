from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from recruit_crawler.schemas import SourceManifest
from recruit_crawler.sources.base import build_source_adapter
from recruit_crawler.sources.platforms import (
    JobKoreaAdapter,
    JumpitAdapter,
    LinkedInAdapter,
    RallitAdapter,
    RocketPunchBrowserAutomationAdapter,
    SaraminAdapter,
    WantedAdapter,
)


class SourceAdapterRegistryTests(unittest.TestCase):
    def test_known_platforms_use_platform_specific_adapters(self) -> None:
        expected = {
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


if __name__ == "__main__":
    unittest.main()
