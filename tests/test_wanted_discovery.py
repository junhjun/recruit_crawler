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
from recruit_crawler.sources.platforms import WantedAdapter


SEARCH_URL = "https://www.wanted.co.kr/search?query=python&tab=position"
DETAIL_URL = "https://www.wanted.co.kr/wd/371139"
DETAIL_HTML = """
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


def _manifest(*, link_keywords: bool = False) -> SourceManifest:
    options = {
        "search_urls": [SEARCH_URL],
        "candidate_include_keywords": ["python"],
        "require_robots": False,
        "explicit_automated_permission": True,
        "delay_seconds": 0,
    }
    if link_keywords:
        options["link_include_keywords"] = ["python"]
    return SourceManifest(
        source_id="wanted",
        enabled=True,
        access_mode="public_page",
        auth_required=False,
        tos_review_status="pass",
        domains=["www.wanted.co.kr"],
        rate_limit="1 request / second",
        failure_mode="skip_source",
        allowed_persisted_fields=[],
        options=options,
    )


class WantedDiscoveryTests(unittest.TestCase):
    def test_wanted_adapter_discovers_next_data_wd_ids_from_listing(self) -> None:
        adapter = WantedAdapter(_manifest(link_keywords=True))
        list_html = """
        <script id="__NEXT_DATA__" type="application/json">
        {"props":{"pageProps":{"positions":[
          {"id":371139,"position":"Python 자동화 개발자"},
          {"id":222222,"position":"주방 보조"}
        ]}}}
        </script>
        """

        def fake_fetch(url: str) -> HttpResponse:
            if url == SEARCH_URL:
                return HttpResponse(url, list_html)
            self.assertEqual(url, DETAIL_URL)
            return HttpResponse(url, DETAIL_HTML)

        with patch.object(adapter, "_fetch", side_effect=fake_fetch):
            candidates = adapter.collect()

        self.assertEqual(len(candidates), 1)
        self.assertEqual(candidates[0].source_url, DETAIL_URL)
        self.assertEqual(candidates[0].source_posting_id, "371139")

    def test_wanted_adapter_falls_back_to_public_search_api_when_listing_is_skeleton(self) -> None:
        adapter = WantedAdapter(_manifest())
        list_html = """
        <html><body>
        <li class="JobCardSkeleton_container__2500s" aria-hidden="true"></li>
        <script id="__NEXT_DATA__" type="application/json">{"props":{"pageProps":{}}}</script>
        </body></html>
        """
        api_payload = {"data": [{"id": 371139, "position": "Python 자동화 개발자"}]}

        def fake_fetch(url: str) -> HttpResponse:
            if url == SEARCH_URL:
                return HttpResponse(url, list_html)
            self.assertEqual(url, DETAIL_URL)
            return HttpResponse(url, DETAIL_HTML)

        with (
            patch.object(adapter, "_fetch", side_effect=fake_fetch),
            patch.object(
                adapter,
                "_get_fetch",
                return_value=HttpResponse(
                    "https://www.wanted.co.kr/api/chaos/search/v1/position?query=python",
                    json.dumps(api_payload),
                ),
            ) as get_fetch,
        ):
            candidates = adapter.collect()

        get_fetch.assert_called_once_with(
            "https://www.wanted.co.kr/api/chaos/search/v1/position",
            {"query": "python", "limit": 20},
        )
        self.assertEqual(len(candidates), 1)
        self.assertEqual(candidates[0].source_url, DETAIL_URL)
        self.assertEqual(candidates[0].source_posting_id, "371139")


if __name__ == "__main__":
    unittest.main()
