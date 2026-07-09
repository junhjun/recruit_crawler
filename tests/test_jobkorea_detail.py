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


class JobKoreaDetailTests(unittest.TestCase):
    def test_jobkorea_adapter_enriches_api_cards_with_public_detail_body(self) -> None:
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
                "delay_seconds": 0,
            },
        )
        adapter = JobKoreaAdapter(manifest)
        payload = {
            "html": """
            <li class="recruit-item">
              <a href="/Recruit/GI_Read/49476607?sc=323" class="recruit-link"
                 data-cname="플라잎" data-applyclosedt="2026-07-28 23:00:00">
                <div class="recruit-title"><h3 class="title">Forward Deployed 엔지니어</h3></div>
                <ul class="keywords">
                  <li class="item primary">Python</li>
                  <li class="item">울산 남구</li>
                </ul>
              </a>
            </li>
            """
        }
        detail_html = """
        <html><body>
          <h2>이런 업무를 해요</h2>
          <p>Python AI 시스템 개발과 고객 자동화 작업 분석</p>
          <h2>이런 분들을 찾고 있어요</h2>
          <p>PyTorch 경험 및 문제 해결 능력</p>
          <h2>우대사항</h2>
          <p>Physical AI 프로젝트 경험</p>
          <p>근무지 주소 : 울산광역시 남구 옥현로 129 지도보기</p>
        </body></html>
        """

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
            return_value=HttpResponse("https://www.jobkorea.co.kr/Recruit/GI_Read/49476607", detail_html),
        ):
            candidates = adapter.collect()

        self.assertEqual(len(candidates), 1)
        self.assertIn("고객 자동화", candidates[0].raw_jd["responsibilities"][0])
        self.assertIn("PyTorch", candidates[0].raw_jd["required_qualifications"][0])
        self.assertIn("Physical AI", candidates[0].raw_jd["preferred_qualifications"][0])
        self.assertEqual(candidates[0].location, "울산광역시 남구 옥현로 129")

    def test_jobkorea_adapter_uses_json_ld_when_detail_sections_are_absent(self) -> None:
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
              <a href="/Recruit/GI_Read/49333079?sc=323" class="recruit-link"
                 data-cname="Example AI" data-applyclosedt="2026-07-08 23:00:00">
                <div class="recruit-title"><h3 class="title">AI 엔지니어</h3></div>
                <ul class="keywords">
                  <li class="item primary">Python</li>
                  <li class="item">서울 중구</li>
                </ul>
              </a>
            </li>
            """
        }
        detail_html = """
        <html><head><title>Example AI 채용</title></head><body>
          <script type="application/ld+json">
          {
            "@context": "https://schema.org",
            "@type": "JobPosting",
            "title": "AI 엔지니어",
            "description": "Python 기반 LLM 서비스 개발자를 채용합니다.",
            "validThrough": "2026-07-08T23:59",
            "hiringOrganization": {"@type": "Organization", "name": "Example AI"},
            "jobLocation": {
              "@type": "Place",
              "address": {"@type": "PostalAddress", "streetAddress": "서울 중구 세종대로"}
            },
            "identifier": {"@type": "PropertyValue", "name": "JobKorea", "value": "49333079"},
            "url": "https://www.jobkorea.co.kr/Recruit/GI_Read/49333079"
          }
          </script>
          <p>섹션 마커 없는 Next page shell</p>
        </body></html>
        """

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
            return_value=HttpResponse("https://www.jobkorea.co.kr/Recruit/GI_Read/49333079", detail_html),
        ):
            candidates = adapter.collect()

        self.assertEqual(len(candidates), 1)
        self.assertIn("Python 기반 LLM", candidates[0].raw_jd["responsibilities"][0])
        self.assertEqual(candidates[0].location, "서울 중구 세종대로")
        self.assertEqual(candidates[0].deadline_raw, "2026-07-08")
        self.assertEqual(adapter.errors, [])

    def test_jobkorea_json_ld_graph_detail_quality_sample(self) -> None:
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
              <a href="/Recruit/GI_Read/50000001?sc=323" class="recruit-link"
                 data-cname="Graph AI" data-applyclosedt="2026-08-01 23:00:00">
                <div class="recruit-title"><h3 class="title">Python ML 엔지니어</h3></div>
                <ul class="keywords"><li class="item primary">Python</li></ul>
              </a>
            </li>
            """
        }
        detail_html = """
        <html><body>
          <script type="application/ld+json">
          {
            "@context": "https://schema.org",
            "@graph": [
              {"@type": "BreadcrumbList", "name": "ignored"},
              {
                "@type": "JobPosting",
                "title": "Python ML 엔지니어",
                "description": "<p>Python 모델 서빙과 ML pipeline 개발</p>",
                "validThrough": "2026-08-01T23:59:59+09:00",
                "hiringOrganization": {"@type": "Organization", "name": "Graph AI", "description": "AI product team"},
                "jobLocation": {"@type": "Place", "address": {"streetAddress": "서울 강남구 테헤란로 1"}}
              }
            ]
          }
          </script>
          <p>섹션 marker 없는 상세 shell</p>
        </body></html>
        """

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
            return_value=HttpResponse("https://www.jobkorea.co.kr/Recruit/GI_Read/50000001", detail_html),
        ):
            candidates = adapter.collect()

        self.assertEqual(len(candidates), 1)
        self.assertIn("ML pipeline", candidates[0].raw_jd["responsibilities"][0])
        self.assertIn("AI product team", candidates[0].raw_jd["company_info"])
        self.assertEqual(candidates[0].location, "서울 강남구 테헤란로 1")
        self.assertEqual(candidates[0].deadline_raw, "2026-08-01")


if __name__ == "__main__":
    unittest.main()
