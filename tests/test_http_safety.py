from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from unittest.mock import patch

from recruit_crawler.schemas import SourceManifest
from recruit_crawler.sources.http import HttpResponse, PublicJobsHttpAdapter, SourceAccessError, _SafeRedirectHandler


class HttpSafetyTests(unittest.TestCase):
    def test_public_jobs_adapter_extracts_json_ld_job_posting(self) -> None:
        manifest = SourceManifest(
            source_id="company_careers",
            enabled=True,
            access_mode="public_page",
            auth_required=False,
            tos_review_status="pass",
            domains=["example.com"],
            rate_limit="1 request / second",
            failure_mode="skip_source",
            allowed_persisted_fields=[],
            options={
                "start_urls": ["https://example.com/jobs/ml"],
                "require_robots": False,
                "explicit_automated_permission": True,
                "delay_seconds": 0,
            },
        )
        html = """
        <html><head>
          <script type="application/ld+json">
          {
            "@type": "JobPosting",
            "title": "Machine Learning Engineer",
            "url": "https://example.com/jobs/ml",
            "validThrough": "2026-08-01T00:00:00+09:00",
            "hiringOrganization": {"name": "Example AI"},
            "jobLocation": {"address": {"addressLocality": "Seoul"}},
            "qualifications": "Python and machine learning",
            "responsibilities": "Build ranking models"
          }
          </script>
        </head></html>
        """
        adapter = PublicJobsHttpAdapter(manifest)
        with patch.object(adapter, "_fetch", return_value=HttpResponse("https://example.com/jobs/ml", html)):
            candidates = adapter.collect()

        self.assertEqual(len(candidates), 1)
        self.assertEqual(candidates[0].title, "Machine Learning Engineer")
        self.assertEqual(candidates[0].company, "Example AI")
        self.assertEqual(candidates[0].deadline_raw, "2026-08-01")

    def test_public_jobs_adapter_filters_links_and_candidates_by_keywords(self) -> None:
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
                "search_urls": ["https://www.jobkorea.co.kr/recruit/ai-jobs"],
                "include_url_patterns": ["/Recruit/GI_Read"],
                "link_include_keywords": ["python", "데이터"],
                "candidate_include_keywords": ["python", "데이터"],
                "candidate_exclude_keywords": ["주방"],
                "require_robots": False,
                "explicit_automated_permission": True,
                "delay_seconds": 0,
            },
        )
        listing_html = """
        <a href="/Recruit/GI_Read/1">Python 데이터 엔지니어</a>
        <a href="/Recruit/GI_Read/2">주방 보조 모집</a>
        """
        job_html = """
        <html><head><title>Python 데이터 엔지니어 - Example</title>
        <meta name="description" content="Python과 데이터 파이프라인 업무"></head></html>
        """
        adapter = PublicJobsHttpAdapter(manifest)

        def fake_fetch(url: str) -> HttpResponse:
            if url.endswith("/recruit/ai-jobs"):
                return HttpResponse(url, listing_html)
            return HttpResponse(url, job_html)

        with patch.object(adapter, "_fetch", side_effect=fake_fetch):
            candidates = adapter.collect()

        self.assertEqual(len(candidates), 1)
        self.assertIn("Python", candidates[0].title)

    def test_http_adapter_blocks_off_domain_redirect_before_following(self) -> None:
        manifest = SourceManifest(
            source_id="company_careers",
            enabled=True,
            access_mode="public_page",
            auth_required=False,
            tos_review_status="pass",
            domains=["example.com"],
            rate_limit="1 request / second",
            failure_mode="skip_source",
            allowed_persisted_fields=[],
            options={},
        )
        adapter = PublicJobsHttpAdapter(manifest)
        handler = _SafeRedirectHandler(adapter)

        with self.assertRaises(SourceAccessError):
            handler.redirect_request(
                type("Req", (), {"full_url": "https://example.com/jobs/ml"})(),
                None,
                302,
                "Found",
                {},
                "https://evil.example/jobs/ml",
            )

    def test_disabling_robots_requires_explicit_permission(self) -> None:
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
                "start_urls": ["https://www.wanted.co.kr/wd/1"],
                "require_robots": False,
            },
        )
        adapter = PublicJobsHttpAdapter(manifest)

        with self.assertRaises(SourceAccessError):
            adapter.collect()


if __name__ == "__main__":
    unittest.main()
