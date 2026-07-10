from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from recruit_crawler.schemas import SourceManifest
from recruit_crawler.sources.http import HttpResponse
from recruit_crawler.sources.platforms import WantedAdapter, _wanted_detail_urls_from_search_api


class WantedPolicyTests(unittest.TestCase):
    def test_skeleton_listing_uses_reviewed_public_search_endpoint(self) -> None:
        search_url = "https://www.wanted.co.kr/search?query=python&tab=position"
        api_url = "https://www.wanted.co.kr/api/chaos/search/v1/position"
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
                "api_url": api_url,
                "candidate_include_keywords": ["python"],
                "require_robots": False,
                "explicit_automated_permission": True,
                "delay_seconds": 0,
            },
        )
        adapter = WantedAdapter(manifest)
        skeleton = '<li class="JobCardSkeleton_container__2500s" aria-hidden="true"></li>'
        api_payload = {"data": [{"id": 371139, "position": "Python automation developer"}]}
        detail_html = """
        <html><head><meta property="og:title" content="[Wanted] Python automation developer 채용 공고 | 원티드"></head><body>
        Wanted∙Seoul∙경력 1-2년
        Python automation developer
        주요업무
        Python data automation
        자격요건
        Python experience
        </body></html>
        """

        with (
            patch.object(
                adapter,
                "_fetch",
                side_effect=[HttpResponse(search_url, skeleton), HttpResponse(detail_url, detail_html)],
            ),
            patch.object(adapter, "_get_fetch", return_value=HttpResponse(api_url, json.dumps(api_payload))) as get_fetch,
        ):
            candidates = adapter.collect()

        get_fetch.assert_called_once_with(api_url, {"query": "python", "limit": 20})
        self.assertEqual([candidate.source_url for candidate in candidates], [detail_url])

    def test_public_search_api_ignores_non_numeric_position_ids(self) -> None:
        payload = {
            "data": [
                {"id": 371139},
                {"id": "371140"},
                {"id": "../login"},
                {"id": True},
                {"id": None},
            ]
        }

        urls = _wanted_detail_urls_from_search_api(json.dumps(payload))

        self.assertEqual(
            urls,
            [
                "https://www.wanted.co.kr/wd/371139",
                "https://www.wanted.co.kr/wd/371140",
            ],
        )


if __name__ == "__main__":
    unittest.main()
