from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from unittest.mock import patch

from recruit_crawler.schemas import SourceManifest
from recruit_crawler.sources.platforms import RocketPunchBrowserAutomationAdapter


class RocketPunchAdapterTests(unittest.TestCase):
    def test_rocketpunch_browser_automation_parses_listing_cards_without_detail_links(self) -> None:
        listing_url = "https://www.rocketpunch.com/en/jobs"
        manifest = SourceManifest(
            source_id="rocketpunch",
            enabled=True,
            access_mode="browser_automation",
            auth_required=False,
            tos_review_status="unknown",
            domains=["www.rocketpunch.com"],
            rate_limit="browser automation with user-directed notice override",
            failure_mode="skip_source",
            allowed_persisted_fields=[],
            options={
                "search_urls": [listing_url],
                "candidate_include_keywords": ["python", "data", "backend", "fintech"],
                "policy_override_mode": "user_directed_ignore",
                "policy_override_reason": "User directed RocketPunch browser automation despite source notice.",
                "policy_override_acknowledges_source_notice": True,
                "max_pages": 10,
                "delay_seconds": 0,
                "fetch_detail_pages": False,
            },
        )
        dom_html = """
        <html><body>
          <div class="listing-card">
            <a href="/en/jobs/158927?list=true">detail</a>
            <span class="company-name">페이데이터</span>
            <h2 class="job-title">DataOps Engineer</h2>
            <p>Python SQL Airflow data pipeline operations</p>
            <span>Seoul</span>
          </div>
          <div class="listing-card">
            <span class="company-name">핀테크랩</span>
            <h2 class="job-title">Fintech Project Manager</h2>
            <p>Fintech AI backend platform coordination</p>
          </div>
          <div class="listing-card">
            <span class="company-name">디자인랩</span>
            <h2 class="job-title">Product Designer</h2>
            <p>Design system and marketing assets</p>
          </div>
        </body></html>
        """
        adapter = RocketPunchBrowserAutomationAdapter(manifest)

        with patch.object(adapter, "_dump_dom", return_value=dom_html):
            candidates = adapter.collect()

        self.assertEqual(len(candidates), 2)
        first = candidates[0]
        self.assertEqual(first.source_id, "rocketpunch")
        self.assertEqual(first.source_url, "https://www.rocketpunch.com/en/jobs?selectedJobId=158927")
        self.assertEqual(first.source_posting_id, "158927")
        self.assertEqual(first.title, "DataOps Engineer")
        self.assertEqual(first.company, "페이데이터")
        self.assertIn("Python", first.raw_jd["required_qualifications"])
        self.assertIn("data pipeline", first.raw_jd["responsibilities"][0])
        self.assertIn("Seoul", first.location)

    def test_rocketpunch_browser_automation_enriches_selected_job_detail(self) -> None:
        listing_url = "https://www.rocketpunch.com/en/jobs"
        manifest = SourceManifest(
            source_id="rocketpunch",
            enabled=True,
            access_mode="browser_automation",
            auth_required=False,
            tos_review_status="unknown",
            domains=["www.rocketpunch.com"],
            rate_limit="browser automation with user-directed notice override",
            failure_mode="skip_source",
            allowed_persisted_fields=[],
            options={
                "search_urls": [listing_url],
                "candidate_include_keywords": ["python"],
                "policy_override_mode": "user_directed_ignore",
                "policy_override_reason": "User directed RocketPunch browser automation despite source notice.",
                "policy_override_acknowledges_source_notice": True,
                "delay_seconds": 0,
            },
        )
        listing_dom = """
        <div data-index="0">
          <a href="/en/jobs/158927?list=true">
            <p class="textStyle_Body.BodyS c_foregrounds.neutral.secondary">Playtica</p>
            <p class="textStyle_Body.BodyM_Bold c_foregrounds.neutral.primary">IT Specialist (BackEnd)</p>
            <p>Python, Java, Node.js</p>
          </a>
        </div>
        """
        detail_dom = (ROOT / "fixtures" / "chrome_captures" / "rocketpunch_selected_job_158927.html").read_text(
            encoding="utf-8"
        )
        adapter = RocketPunchBrowserAutomationAdapter(manifest)

        with patch.object(adapter, "_dump_dom", side_effect=[listing_dom, detail_dom]):
            candidates = adapter.collect()

        self.assertEqual(len(candidates), 1)
        candidate = candidates[0]
        self.assertEqual(candidate.source_url, "https://www.rocketpunch.com/en/jobs?selectedJobId=158927")
        self.assertEqual(candidate.deadline_raw, "2026-07-31")
        self.assertIn("unmanned/self-service", candidate.raw_jd["responsibilities"][0])
        self.assertIn("Django", candidate.raw_jd["required_qualifications"][0])
        self.assertIn("payment systems", candidate.raw_jd["preferred_qualifications"][0])


if __name__ == "__main__":
    unittest.main()
