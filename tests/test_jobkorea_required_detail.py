from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

import json
from unittest.mock import patch

from recruit_crawler.schemas import SourceManifest
from recruit_crawler.sources.http import HttpResponse
from recruit_crawler.sources.platforms import JobKoreaAdapter


class JobKoreaRequiredDetailTests(unittest.TestCase):
    def test_jobkorea_required_detail_body_drops_card_only_candidates(self) -> None:
        manifest = SourceManifest(
            source_id="jobkorea",
            enabled=True,
            access_mode="public_page",
            auth_required=False,
            tos_review_status="pass",
            domains=["www.jobkorea.co.kr"],
            rate_limit="1 request / second",
            failure_mode="skip_source",
            allowed_persisted_fields=[],
            options={
                "api_url": "https://www.jobkorea.co.kr/recruit/ai-jobs/GetRecruitList",
                "candidate_include_keywords": ["python"],
                "max_pages": 10,
                "require_robots": False,
                "explicit_automated_permission": True,
                "require_detail_body": True,
                "delay_seconds": 0,
            },
        )
        adapter = JobKoreaAdapter(manifest)
        payload = {
            "html": """
            <li class="recruit-item">
              <a href="/Recruit/GI_Read/49476607?sc=323" class="recruit-link"
                 data-cname="플라잎" data-applyclosedt="2026-07-28 23:00:00">
                <div class="recruit-title"><h3 class="title">Python 엔지니어</h3></div>
              </a>
            </li>
            """
        }

        with patch.object(
            adapter,
            "_post_fetch",
            return_value=HttpResponse(
                "https://www.jobkorea.co.kr/recruit/ai-jobs/GetRecruitList",
                json.dumps(payload),
            ),
        ), patch.object(
            adapter,
            "_fetch",
            return_value=HttpResponse("https://www.jobkorea.co.kr/Recruit/GI_Read/49476607", "<html><body>카드만 있음</body></html>"),
        ):
            candidates = adapter.collect()

        self.assertEqual(candidates, [])
        self.assertTrue(any("detail body sections not found" in error for error in adapter.errors))

    def test_jobkorea_required_detail_body_rejects_empty_listing_fallback(self) -> None:
        manifest = SourceManifest(
            source_id="jobkorea",
            enabled=True,
            access_mode="public_page",
            auth_required=False,
            tos_review_status="pass",
            domains=["www.jobkorea.co.kr"],
            rate_limit="1 request / second",
            failure_mode="skip_source",
            allowed_persisted_fields=[],
            options={
                "api_url": "https://www.jobkorea.co.kr/recruit/ai-jobs/GetRecruitList",
                "search_urls": ["https://www.jobkorea.co.kr/recruit/ai-jobs"],
                "candidate_include_keywords": ["python"],
                "max_pages": 10,
                "require_robots": False,
                "explicit_automated_permission": True,
                "require_detail_body": True,
                "delay_seconds": 0,
            },
        )
        adapter = JobKoreaAdapter(manifest)

        with patch.object(
            adapter,
            "_post_fetch",
            return_value=HttpResponse(
                "https://www.jobkorea.co.kr/recruit/ai-jobs/GetRecruitList",
                json.dumps({"html": ""}),
            ),
        ), patch.object(adapter, "_fetch") as fetch:
            candidates = adapter.collect()

        fetch.assert_not_called()
        self.assertEqual(candidates, [])
        self.assertEqual(adapter.errors, ["JobKorea listing API HTML was empty"])


if __name__ == "__main__":
    unittest.main()
