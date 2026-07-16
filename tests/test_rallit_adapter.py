from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

import json
from unittest.mock import patch

from recruit_crawler.schemas import SourceManifest
from recruit_crawler.report_policy import verified_link_url
from recruit_crawler.sources.http import HttpResponse
from recruit_crawler.sources.platforms import RallitAdapter


class RallitAdapterTests(unittest.TestCase):
    def test_rallit_adapter_collects_jobs_from_public_positions(self) -> None:
        manifest = SourceManifest(
            source_id="rallit",
            enabled=True,
            access_mode="public_page",
            auth_required=False,
            tos_review_status="pass",
            domains=["www.rallit.com"],
            rate_limit="1 request / second",
            failure_mode="skip_source",
            allowed_persisted_fields=[],
            options={
                "search_urls": ["https://www.rallit.com"],
                "candidate_include_keywords": ["python"],
                "max_pages": 5,
                "require_robots": False,
                "explicit_automated_permission": True,
                "delay_seconds": 0,
            },
        )
        list_html = """
        <a href="/positions/1797/%EC%A3%BC%EC%8B%9D%ED%9A%8C%EC%82%AC-%EC%9C%A0%EB%8B%88%EC%9C%A0%EB%8B%88-ai-%EA%B0%9C%EB%B0%9C%EC%9E%90-%EC%B1%84%EC%9A%A9">
          주식회사 유니유니 AI 개발자 Python
        </a>
        """
        detail_html = """
        <html><head>
          <meta property="og:title" content="주식회사 유니유니 [주식회사 유니유니] ‘AI 개발자’ 채용 채용 - 랠릿" />
        </head><body>
          <h1>[주식회사 유니유니] ‘AI 개발자’ 채용</h1>
          <section>주식회사 유니유니, 어떤 곳인가요? 프라이버시 테크 AI 스타트업입니다.</section>
          <section>[주식회사 유니유니] ‘AI 개발자’ 채용, 어떤 일을 하나요?</section>
          #인공지능(AI) #컴퓨터 비전 #Python
          <h3>주요업무</h3>
          <p>SAVVY 서비스에 사용중인 딥러닝 모델의 개선 방안 연구 및 테스트 진행</p>
          <h3>자격요건</h3>
          <p>Python을 이용한 개발 경험 및 PyTorch 숙련도를 보유하신 분</p>
          <h3>우대사항</h3>
          <p>컴퓨터 비전 분야 실무 경험</p>
          <h3>혜택 및 복지</h3>
          <p>교육비 지원</p>
          <p>근무 지역 경기 성남시 분당구 판교로289번길 20</p>
          <p>경력 미들 (4~8년)</p>
          <p>최소 연봉 회사 내규에 따름</p>
          <p>마감일 채용 시 마감</p>
          <p>회사명 주식회사 유니유니 7 지원하기</p>
        </body></html>
        """
        adapter = RallitAdapter(manifest)

        def fake_fetch(url: str) -> HttpResponse:
            if url == "https://www.rallit.com":
                return HttpResponse(url, list_html)
            return HttpResponse(url, detail_html)

        with patch.object(adapter, "_fetch", side_effect=fake_fetch):
            candidates = adapter.collect()

        self.assertEqual(len(candidates), 1)
        candidate = candidates[0]
        self.assertEqual(candidate.source_id, "rallit")
        self.assertEqual(candidate.source_posting_id, "1797")
        self.assertEqual(candidate.title, "[주식회사 유니유니] ‘AI 개발자’ 채용")
        self.assertEqual(candidate.company, "주식회사 유니유니")
        self.assertEqual(candidate.deadline_raw, "채용시")
        self.assertIn("경기 성남시", candidate.location)
        self.assertIn("Python", candidate.raw_jd["required_qualifications"])
        self.assertIn("딥러닝 모델", candidate.raw_jd["responsibilities"][0])
        self.assertEqual(candidate.raw_jd["experience_tags"], ["미들 (4~8년)"])

    def test_rallit_adapter_strips_embedded_css_from_public_detail(self) -> None:
        manifest = SourceManifest(
            source_id="rallit",
            enabled=True,
            access_mode="public_page",
            auth_required=False,
            tos_review_status="pass",
            domains=["www.rallit.com"],
            rate_limit="1 request / second",
            failure_mode="skip_source",
            allowed_persisted_fields=[],
            options={
                "search_urls": ["https://www.rallit.com"],
                "candidate_include_keywords": ["python"],
                "require_robots": False,
                "explicit_automated_permission": True,
                "delay_seconds": 0,
            },
        )
        list_html = '<a href="/positions/3333/python-backend">Python 백엔드</a>'
        detail_html = """
        <html><body>
          <style>.mantine-9qoqdi{display:flex;color:#00CCAA;}@keyframes animation-1a410py{from{opacity:0.6;}to{opacity:0;}}</style>
          <h1>Python 백엔드 개발자</h1>
          <section>누아, 어떤 곳인가요? 인공지능 여행 기술 기업입니다.</section>
          <h3>주요업무</h3>
          <p>예약 관리 시스템 개발</p>
          <h3>자격요건</h3>
          <p>Python API 개발 경험</p>
          <h3>우대사항</h3>
          <p>여행 플랫폼 연동 경험</p>
          <p>회사명 누아 1 지원하기</p>
        </body></html>
        """
        adapter = RallitAdapter(manifest)

        with patch.object(
            adapter,
            "_fetch",
            side_effect=[
                HttpResponse("https://www.rallit.com", list_html),
                HttpResponse("https://www.rallit.com/positions/3333/python-backend", detail_html),
            ],
        ):
            candidates = adapter.collect()

        self.assertEqual(len(candidates), 1)
        serialized = json.dumps(candidates[0].raw_jd, ensure_ascii=False)
        self.assertIn("Python API 개발", serialized)
        self.assertNotIn("mantine", serialized)
        self.assertNotIn("keyframes", serialized)
        self.assertNotIn("display:flex", serialized)

    def test_rallit_adapter_tolerates_alternate_section_markers(self) -> None:
        manifest = SourceManifest(
            source_id="rallit",
            enabled=True,
            access_mode="public_page",
            auth_required=False,
            tos_review_status="pass",
            domains=["www.rallit.com"],
            rate_limit="1 request / second",
            failure_mode="skip_source",
            allowed_persisted_fields=[],
            options={
                "search_urls": ["https://www.rallit.com"],
                "candidate_include_keywords": ["python"],
                "require_robots": False,
                "explicit_automated_permission": True,
                "delay_seconds": 0,
            },
        )
        list_html = '<a href="/positions/4444/python-platform">Python 플랫폼</a>'
        detail_html = """
        <html><head><meta property="og:title" content="테크랩 [테크랩] Python 플랫폼 개발자 채용 - 랠릿" /></head><body>
          <h1>[테크랩] Python 플랫폼 개발자</h1>
          <section>테크랩, 어떤 곳인가요? AI 플랫폼 기업입니다.</section>
          <h3>합류하면 하게 될 업무</h3>
          <p>Python 기반 데이터 플랫폼을 개발합니다.</p>
          <h3>지원자격</h3>
          <p>Python API와 SQL 경험</p>
          <h3>우대사항</h3>
          <p>LLM 서비스 경험</p>
          <p>근무 지역 서울 강남구</p>
          <p>경력 주니어 (1~3년)</p>
          <p>마감일 2026.07.31</p>
          <p>회사명 테크랩 3 지원하기</p>
        </body></html>
        """
        adapter = RallitAdapter(manifest)

        with patch.object(
            adapter,
            "_fetch",
            side_effect=[
                HttpResponse("https://www.rallit.com", list_html),
                HttpResponse("https://www.rallit.com/positions/4444/python-platform", detail_html),
            ],
        ):
            candidates = adapter.collect()

        self.assertEqual(len(candidates), 1)
        self.assertIn("데이터 플랫폼", candidates[0].raw_jd["responsibilities"][0])
        self.assertIn("Python API", candidates[0].raw_jd["required_qualifications"][0])
        self.assertEqual(candidates[0].deadline_raw, "2026-07-31")


    def test_verified_rallit_links_canonicalize_unicode_slugs_and_reject_unsafe_forms(self) -> None:
        encoded_slug = (
            "%EC%A3%BC%EC%8B%9D%ED%9A%8C%EC%82%AC-%EC%9C%A0%EB%8B%88%EC%9C%A0%EB%8B%88-"
            "ai-%EA%B0%9C%EB%B0%9C%EC%9E%90-%EC%B1%84%EC%9A%A9"
        )
        self.assertEqual(
            verified_link_url(
                "scheduled-run",
                "rallit",
                f"https://www.rallit.com/positions/1797/{encoded_slug.lower()}",
                "1797",
                "verified",
            ),
            f"https://www.rallit.com/positions/1797/{encoded_slug}",
        )
        self.assertEqual(
            verified_link_url(
                "scheduled-run",
                "rallit",
                "https://www.rallit.com/positions/1797/주식회사-유니유니-ai-개발자-채용",
                "1797",
                "verified",
            ),
            f"https://www.rallit.com/positions/1797/{encoded_slug}",
        )
        self.assertEqual(
            verified_link_url(
                "scheduled-run",
                "rallit",
                "https://www.rallit.com/positions/1797",
                "1797",
                "verified",
            ),
            "https://www.rallit.com/positions/1797",
        )

        unsafe_urls = (
            "https://www.rallit.com/positions/1797/bad%",
            "https://www.rallit.com/positions/1797/bad%G0",
            "https://www.rallit.com/positions/1797/bad%2Fslug",
            "https://www.rallit.com/positions/1797/bad/slug",
            "https://www.rallit.com/positions/1797/bad.dot",
            "https://www.rallit.com/positions/1797/bad%2Edot",
            "https://www.rallit.com/positions/1797/bad\\slug",
            "https://www.rallit.com/positions/1797/bad%5Cslug",
            "https://www.rallit.com/positions/1797/bad%25slug",
            "https://www.rallit.com/positions/1797/bad%00slug",
            "https://www.rallit.com/positions/1797/bad%FFslug",
            "https://www.rallit.com/positions/1797/e\u0301",
            "https://www.rallit.com/positions/1797/",
            "https://www.rallit.com/positions/1797/bad/",
            "https://www.rallit.com/positions/1797/bad/extra",
            "https://www.rallit.com/positions/1797/bad?query=1",
            "https://www.rallit.com/positions/1797/bad#fragment",
            "https://user@www.rallit.com/positions/1797/bad",
            "https://www.rallit.com:443/positions/1797/bad",
            "http://www.rallit.com/positions/1797/bad",
            "https://rallit.com/positions/1797/bad",
            "https://www.rallit.com/positions/1798/bad",
        )
        for source_url in unsafe_urls:
            with self.subTest(source_url=source_url):
                self.assertIsNone(
                    verified_link_url(
                        "scheduled-run",
                        "rallit",
                        source_url,
                        "1797",
                        "verified",
                    )
                )


if __name__ == "__main__":
    unittest.main()
