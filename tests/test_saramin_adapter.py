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
from recruit_crawler.sources.platforms import SaraminAdapter


class SaraminAdapterTests(unittest.TestCase):
    def test_saramin_adapter_collects_from_official_api_payload(self) -> None:
        manifest = SourceManifest(
            source_id="saramin",
            enabled=True,
            access_mode="api",
            auth_required=False,
            tos_review_status="pass",
            domains=["oapi.saramin.co.kr"],
            rate_limit="official API quota",
            failure_mode="skip_source",
            allowed_persisted_fields=[],
            options={
                "api_url": "https://oapi.saramin.co.kr/job-search",
                "access_key": "test-key",
                "approved_api_access": True,
                "require_robots": False,
                "candidate_include_keywords": ["python"],
                "delay_seconds": 0,
            },
        )
        adapter = SaraminAdapter(manifest)
        payload = {
            "jobs": {
                "job": [
                    {
                        "id": "saramin-1",
                        "url": "https://www.saramin.co.kr/zf_user/jobs/relay/view?rec_idx=1",
                        "expiration-date": "2026-07-31T23:59:59+09:00",
                        "company": {"detail": {"name": "Saramin AI"}},
                        "position": {
                            "title": "Python AI Engineer",
                            "location": {"name": "서울"},
                            "job-code": {"name": "AI·데이터"},
                            "experience-level": {"name": "경력 2년 이상"},
                        },
                    }
                ]
            }
        }

        with patch.object(
            adapter,
            "_get_fetch",
            return_value=HttpResponse("https://oapi.saramin.co.kr/job-search", json.dumps(payload)),
        ):
            candidates = adapter.collect()

        self.assertEqual(len(candidates), 1)
        self.assertEqual(candidates[0].source_id, "saramin")
        self.assertEqual(candidates[0].title, "Python AI Engineer")
        self.assertEqual(candidates[0].company, "Saramin AI")
        self.assertEqual(candidates[0].deadline_raw, "2026-07-31")
        self.assertEqual(candidates[0].raw_jd["experience_tags"], ["경력 2년 이상"])

    def test_saramin_adapter_collects_public_detail_body_without_api(self) -> None:
        detail_url = "https://www.saramin.co.kr/zf_user/jobs/relay/view-detail?rec_idx=54106686&rec_seq=0"
        manifest = SourceManifest(
            source_id="saramin",
            enabled=True,
            access_mode="public_page",
            auth_required=False,
            tos_review_status="pass",
            domains=["www.saramin.co.kr"],
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
        adapter = SaraminAdapter(manifest)
        detail_html = """
        <html><body>
        AI 엔지니어 채용
        사용 기술 Python FastAPI Redis AWS Docker
        주요업무
        LLM 에이전트 및 RAG 시스템 설계 및 개발
        자격요건
        신입 또는 관련 경력 1년 이상 Python 비동기 API 설계 경험
        우대사항
        Redis, Elasticsearch 운영 경험
        마감일 및 근무지
        마감일 : 2026년 07월 07일
        근무지
        - 서울 강남구 선릉로93길 40
        </body></html>
        """

        with patch.object(adapter, "_fetch", return_value=HttpResponse(detail_url, detail_html)):
            candidates = adapter.collect()

        self.assertEqual(len(candidates), 1)
        self.assertEqual(candidates[0].source_id, "saramin")
        self.assertEqual(candidates[0].source_posting_id, "54106686")
        self.assertEqual(candidates[0].title, "AI 엔지니어 채용")
        self.assertIn("LLM 에이전트", candidates[0].raw_jd["responsibilities"][0])
        self.assertIn("Python", candidates[0].raw_jd["required_qualifications"][0])
        self.assertIn("FastAPI", candidates[0].raw_jd["required_qualifications"])
        self.assertIn("Elasticsearch", candidates[0].raw_jd["preferred_qualifications"][0])
        self.assertIn("2026년 07월 07일", candidates[0].deadline_raw)
        self.assertIn("서울 강남구", candidates[0].location)

    def test_saramin_adapter_discovers_public_relay_detail_urls_from_listing(self) -> None:
        search_url = "https://www.saramin.co.kr/zf_user/search/recruit?searchword=python"
        detail_url = "https://www.saramin.co.kr/zf_user/jobs/relay/view-detail?rec_idx=54106686&rec_seq=0"
        manifest = SourceManifest(
            source_id="saramin",
            enabled=True,
            access_mode="public_page",
            auth_required=False,
            tos_review_status="pass",
            domains=["www.saramin.co.kr"],
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
        <a href="/zf_user/jobs/relay/view?rec_idx=54106686&rec_seq=0">Python AI 엔지니어</a>
        <a href="/zf_user/jobs/relay/view?rec_idx=11111111&rec_seq=0">주방 보조</a>
        """
        detail_html = """
        <html><body>
        Python AI 엔지니어
        주요업무
        Python 기반 추천 시스템 개발
        자격요건
        Python API 개발 경험
        우대사항
        LLM 서비스 경험
        마감일 및 근무지
        근무지
        - 서울 강남구
        </body></html>
        """
        adapter = SaraminAdapter(manifest)

        def fake_fetch(url: str) -> HttpResponse:
            if url == search_url:
                return HttpResponse(url, list_html)
            self.assertEqual(url, detail_url)
            return HttpResponse(url, detail_html)

        with patch.object(adapter, "_fetch", side_effect=fake_fetch):
            candidates = adapter.collect()

        self.assertEqual(len(candidates), 1)
        self.assertEqual(candidates[0].source_url, detail_url)
        self.assertEqual(candidates[0].source_posting_id, "54106686")
        self.assertIn("추천 시스템", candidates[0].raw_jd["responsibilities"][0])


if __name__ == "__main__":
    unittest.main()
