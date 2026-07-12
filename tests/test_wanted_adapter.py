from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from unittest.mock import patch

from recruit_crawler.schemas import SourceManifest
from recruit_crawler.sources.http import HttpResponse
from recruit_crawler.sources.platforms import WantedAdapter


class WantedAdapterTests(unittest.TestCase):
    def test_wanted_adapter_collects_user_provided_manual_postings(self) -> None:
        manifest = SourceManifest(
            source_id="wanted",
            enabled=True,
            access_mode="api",
            auth_required=False,
            tos_review_status="pass",
            domains=["www.wanted.co.kr"],
            rate_limit="manual export",
            failure_mode="skip_source",
            allowed_persisted_fields=[],
            options={
                "manual_postings": [
                    {
                        "url": "https://www.wanted.co.kr/wd/123",
                        "title": "Wanted Python ML Engineer",
                        "company": "Wanted AI",
                        "location": "Seoul",
                        "deadline": "2026-08-01",
                        "skills": ["Python", "LLM"],
                        "requirements": "Build machine learning products",
                        "experience": "경력 1년 이상",
                    }
                ],
                "candidate_include_keywords": ["python"],
                "delay_seconds": 0,
            },
        )
        candidates = WantedAdapter(manifest).collect()

        self.assertEqual(len(candidates), 1)
        self.assertEqual(candidates[0].source_id, "wanted")
        self.assertEqual(candidates[0].title, "Wanted Python ML Engineer")
        self.assertIn("LLM", candidates[0].raw_jd["required_qualifications"])

    def test_wanted_adapter_collects_public_detail_body_without_manual_payload(self) -> None:
        detail_url = "https://www.wanted.co.kr/wd/371139"
        manifest = SourceManifest(
            source_id="wanted",
            enabled=True,
            access_mode="public_page",
            auth_required=False,
            tos_review_status="pass",
            domains=["www.wanted.co.kr"],
            rate_limit="1 request / second",
            failure_mode="skip_source",
            allowed_persisted_fields=[],
            options={
                "detail_urls": [detail_url],
                "candidate_include_keywords": ["python"],
                "require_robots": False,
                "explicit_automated_permission": True,
                "delay_seconds": 0,
            },
        )
        adapter = WantedAdapter(manifest)
        detail_html = """
        <html><head><meta property="og:title" content="[엑스보스] Python 워크플로우 자동화 개발자 채용 공고 | 원티드"></head><body>
        엑스보스∙서울 강서구∙경력 1-5년
        Python 워크플로우 자동화 개발자
        포지션 상세
        주요업무
        Python 워크플로우 설계와 API GUI 하이브리드 자동화 개발
        자격요건
        Python 개발 2-5년 또는 Selenium Playwright Requests asyncio 경험
        기술 스택 • 툴
        Git MySQL JSON Python Excel PostgreSQL SQLite
        마감일
        2026.07.25
        근무지역
        서울 강서구 마곡동 799-6
        본 채용정보는 원티드랩의 동의없이 무단전재
        </body></html>
        """

        with patch.object(adapter, "_fetch", return_value=HttpResponse(detail_url, detail_html)):
            candidates = adapter.collect()

        self.assertEqual(len(candidates), 1)
        self.assertEqual(candidates[0].source_id, "wanted")
        self.assertEqual(candidates[0].source_posting_id, "371139")
        self.assertEqual(candidates[0].title, "Python 워크플로우 자동화 개발자")
        self.assertEqual(candidates[0].company, "엑스보스")
        self.assertIn("Python 워크플로우", candidates[0].raw_jd["responsibilities"][0])
        self.assertIn("Selenium", candidates[0].raw_jd["required_qualifications"][0])
        self.assertIn("PostgreSQL", candidates[0].raw_jd["required_qualifications"])
        self.assertEqual(candidates[0].deadline_raw, "2026.07.25")
        self.assertIn("서울 강서구", candidates[0].location)

    def test_wanted_adapter_discovers_public_wd_urls_from_listing(self) -> None:
        search_url = "https://www.wanted.co.kr/search?query=python&tab=position"
        detail_url = "https://www.wanted.co.kr/wd/371139"
        manifest = SourceManifest(
            source_id="wanted",
            enabled=True,
            access_mode="public_page",
            auth_required=False,
            tos_review_status="pass",
            domains=["www.wanted.co.kr"],
            rate_limit="1 request / second",
            failure_mode="skip_source",
            allowed_persisted_fields=[],
            options={
                "search_urls": [search_url],
                "link_include_keywords": ["python"],
                "candidate_include_keywords": ["python"],
                "require_robots": False,
                "explicit_automated_permission": True,
                "delay_seconds": 0,
            },
        )
        list_html = """
        <a href="/wd/371139">Python 워크플로우 자동화 개발자</a>
        <a href="/company/123">회사 소개</a>
        """
        detail_html = """
        <html><head><meta property="og:title" content="[엑스보스] Python 자동화 개발자 채용 공고 | 원티드"></head><body>
        엑스보스∙서울 강서구∙경력 1-5년
        Python 자동화 개발자
        주요업무
        Python 데이터 수집 자동화 개발
        자격요건
        Python Requests Playwright 경험
        마감일
        2026.07.25
        근무지역
        서울 강서구
        </body></html>
        """
        adapter = WantedAdapter(manifest)

        def fake_fetch(url: str) -> HttpResponse:
            if url == search_url:
                return HttpResponse(url, list_html)
            self.assertEqual(url, detail_url)
            return HttpResponse(url, detail_html)

        with patch.object(adapter, "_fetch", side_effect=fake_fetch):
            candidates = adapter.collect()

        self.assertEqual(len(candidates), 1)
        self.assertEqual(candidates[0].source_url, detail_url)
        self.assertEqual(candidates[0].source_posting_id, "371139")
        self.assertIn("데이터 수집", candidates[0].raw_jd["responsibilities"][0])

if __name__ == "__main__":
    unittest.main()
