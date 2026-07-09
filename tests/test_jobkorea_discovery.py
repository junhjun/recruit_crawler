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


class JobKoreaDiscoveryTests(unittest.TestCase):
    def test_jobkorea_adapter_discovers_ai_jobs_from_api_html(self) -> None:
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
                "link_include_keywords": ["python"],
                "max_pages": 10,
                "require_robots": False,
                "explicit_automated_permission": True,
            },
        )
        adapter = JobKoreaAdapter(manifest)
        payload = {
            "html": """
            <a href="/Recruit/GI_Read/49333079?sc=323">AI 엔지니어 Python</a>
            <a href="/Recruit/GI_Read/49410271?sc=323">주방 보조</a>
            """
        }

        with patch.object(
            adapter,
            "_post_fetch",
            return_value=HttpResponse(
                "https://www.jobkorea.co.kr/recruit/ai-jobs/GetRecruitList",
                json.dumps(payload),
            ),
        ):
            urls = adapter.discover_urls()

        self.assertEqual(urls, ["https://www.jobkorea.co.kr/Recruit/GI_Read/49333079?sc=323"])

    def test_jobkorea_adapter_collects_api_keywords_as_snapshot_terms(self) -> None:
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
                "fetch_detail_pages": False,
                "delay_seconds": 0,
            },
        )
        adapter = JobKoreaAdapter(manifest)
        payload = {
            "html": """
            <li class="recruit-item">
              <a href="/Recruit/GI_Read/49333079?sc=323" class="recruit-link"
                 data-cname="Example AI" data-applyclosedt="2026-07-08 23:00:00">
                <div class="recruit-title"><h3 class="title">AI 엔지니어</h3></div>
                <ul class="keywords">
                  <li class="item primary">Python</li>
                  <li class="item primary">SQL</li>
                  <li class="item">서울 중구</li>
                </ul>
              </a>
            </li>
            </div></div>
            """
        }

        with patch.object(
            adapter,
            "_post_fetch",
            return_value=HttpResponse(
                "https://www.jobkorea.co.kr/recruit/ai-jobs/GetRecruitList",
                json.dumps(payload),
            ),
        ):
            candidates = adapter.collect()

        self.assertEqual(len(candidates), 1)
        self.assertEqual(candidates[0].company, "Example AI")
        self.assertEqual(candidates[0].deadline_raw, "2026-07-08")
        self.assertEqual(candidates[0].location, "서울 중구")
        self.assertIn("Python", candidates[0].raw_jd["required_qualifications"])
        self.assertNotIn("서울 중구", candidates[0].raw_jd["required_qualifications"])
        self.assertEqual(candidates[0].raw_jd["experience_tags"], [])

    def test_jobkorea_adapter_preserves_experience_tags(self) -> None:
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
                "fetch_detail_pages": False,
                "delay_seconds": 0,
            },
        )
        adapter = JobKoreaAdapter(manifest)
        payload = {
            "html": """
            <li class="recruit-item">
              <a href="/Recruit/GI_Read/49333079?sc=323" class="recruit-link"
                 data-cname="Example AI" data-applyclosedt="2026-07-08 23:00:00">
                <div class="recruit-title"><h3 class="title">AI 엔지니어</h3></div>
                <ul class="keywords">
                  <li class="item primary">Python</li>
                  <li class="item">경력1년↑</li>
                  <li class="item">서울 중구</li>
                </ul>
              </a>
            </li>
            </div></div>
            """
        }

        with patch.object(
            adapter,
            "_post_fetch",
            return_value=HttpResponse(
                "https://www.jobkorea.co.kr/recruit/ai-jobs/GetRecruitList",
                json.dumps(payload),
            ),
        ):
            candidates = adapter.collect()

        self.assertEqual(candidates[0].raw_jd["experience_tags"], ["경력1년↑"])
        self.assertNotIn("경력1년↑", candidates[0].raw_jd["required_qualifications"])


if __name__ == "__main__":
    unittest.main()
