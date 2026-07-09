from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from unittest.mock import patch

from recruit_crawler.schemas import SourceManifest
from recruit_crawler.sources.http import HttpResponse
from recruit_crawler.sources.platforms import JumpitAdapter


class JumpitAdapterTests(unittest.TestCase):
    def test_jumpit_adapter_collects_jobs_from_sitemap_and_react_data(self) -> None:
        manifest = SourceManifest(
            source_id="jumpit",
            enabled=True,
            access_mode="public_page",
            auth_required=False,
            tos_review_status="pass",
            domains=["jumpit.saramin.co.kr"],
            rate_limit="1 request / second",
            failure_mode="skip_source",
            allowed_persisted_fields=[],
            options={
                "sitemap_urls": ["https://jumpit.saramin.co.kr/sitemap/sitemap_position_view_1.xml"],
                "candidate_include_keywords": ["python"],
                "max_pages": 5,
                "require_robots": False,
                "explicit_automated_permission": True,
                "delay_seconds": 0,
            },
        )
        sitemap_xml = """
        <urlset><url><loc>https://jumpit.saramin.co.kr/position/54308479</loc></url></urlset>
        """
        page_html = r'''
        <script>
        self.__next_f.push([1,"{\"data\":{\"NOTICE\":[{\"title\":\"[당첨자 발표] 훈련과정 인증 이벤트\"}]}}"])
        self.__next_f.push([1,"{\"data\":{\"id\":54308479,\"title\":\"AI Platform Engineer\",\"companyName\":\"Example AI\",\"techStacks\":[{\"stack\":\"Python\"},{\"stack\":\"PyTorch\"}],\"responsibility\":\"Build ML serving systems\",\"qualifications\":\"Python and machine learning\",\"preferredRequirements\":\"LLM experience\",\"serviceInfo\":\"AI products\",\"newcomer\":false,\"minCareer\":2,\"closedAt\":\"2026-07-29 23:59:59\",\"location\":\"서울 강남구\"},\"dataUpdateCount\":1}"])
        </script>
        '''
        adapter = JumpitAdapter(manifest)

        def fake_fetch(url: str) -> HttpResponse:
            if url.endswith("sitemap_position_view_1.xml"):
                return HttpResponse(url, sitemap_xml)
            return HttpResponse(url, page_html)

        with patch.object(adapter, "_fetch", side_effect=fake_fetch):
            candidates = adapter.collect()

        self.assertEqual(len(candidates), 1)
        self.assertEqual(candidates[0].title, "AI Platform Engineer")
        self.assertEqual(candidates[0].company, "Example AI")
        self.assertEqual(candidates[0].deadline_raw, "2026-07-29")
        self.assertEqual(candidates[0].location, "서울 강남구")
        self.assertIn("Python", candidates[0].raw_jd["required_qualifications"])
        self.assertEqual(candidates[0].raw_jd["experience_tags"], ["경력2년↑"])

    def test_jumpit_adapter_tolerates_spaced_next_payload_shape(self) -> None:
        manifest = SourceManifest(
            source_id="jumpit",
            enabled=True,
            access_mode="public_page",
            auth_required=False,
            tos_review_status="pass",
            domains=["jumpit.saramin.co.kr"],
            rate_limit="1 request / second",
            failure_mode="skip_source",
            allowed_persisted_fields=[],
            options={
                "sitemap_urls": ["https://jumpit.saramin.co.kr/sitemap/sitemap_position_view_1.xml"],
                "candidate_include_keywords": ["python"],
                "require_robots": False,
                "explicit_automated_permission": True,
                "delay_seconds": 0,
            },
        )
        sitemap_xml = "<urlset><url><loc>https://jumpit.saramin.co.kr/position/54308479</loc></url></urlset>"
        page_html = '''
        <script>
        self.__next_f.push([1, '{"data": {"title": "AI Platform Engineer", "companyName": "Example AI",
          "techStacks": [{"stack": "Python"}, {"stack": "PyTorch"}],
          "responsibility": "Build Python services", "qualifications": "Python ML",
          "preferredRequirements": "LLM", "serviceInfo": "AI products",
          "newcomer": true, "minCareer": 1, "closedAt": "2026-07-29 23:59:59",
          "location": "서울 강남구"}}'])
        </script>
        '''
        adapter = JumpitAdapter(manifest)

        with patch.object(
            adapter,
            "_fetch",
            side_effect=[
                HttpResponse("https://jumpit.saramin.co.kr/sitemap/sitemap_position_view_1.xml", sitemap_xml),
                HttpResponse("https://jumpit.saramin.co.kr/position/54308479", page_html),
            ],
        ):
            candidates = adapter.collect()

        self.assertEqual(len(candidates), 1)
        self.assertEqual(candidates[0].title, "AI Platform Engineer")
        self.assertIn("PyTorch", candidates[0].raw_jd["required_qualifications"])
        self.assertEqual(candidates[0].raw_jd["experience_tags"], ["신입", "경력1년↑"])


if __name__ == "__main__":
    unittest.main()
