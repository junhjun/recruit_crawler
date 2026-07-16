from __future__ import annotations

import sys
import unittest
from pathlib import Path
import tempfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

import json
from unittest.mock import patch
from recruit_crawler.config import ConfigError, _validate_saramin_acquisition

from recruit_crawler.schemas import SourceManifest
from recruit_crawler.saramin_strategy_probe import (
    ProbeRequest,
    ProbeResponse,
    main as saramin_strategy_probe_main,
    run_probe,
    write_summary,
)
from recruit_crawler.sources.http import HttpResponse, SourceAccessError
from recruit_crawler.sources.platforms import SaraminAdapter
from recruit_crawler.sources.platform_saramin import _saramin_detail_url


class SaraminAdapterTests(unittest.TestCase):
    def test_saramin_adapter_rejects_api_fallback(self) -> None:
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
            options={"approved_api_access": True},
        )
        adapter = SaraminAdapter(manifest)
        with patch.object(adapter, "_get_fetch") as api_fetch:
            with self.assertRaisesRegex(SourceAccessError, "사람인"):
                adapter.collect()
        api_fetch.assert_not_called()

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
                "detail_urls": [detail_url, detail_url],
                "acquisition_strategy": "detail_only",
                "outer_strategy_approval": "not_probed",
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

        with patch.object(adapter, "_fetch", return_value=HttpResponse(detail_url, detail_html)) as fetch:
            candidates = adapter.collect()

        self.assertEqual(fetch.call_count, 1)
        self.assertEqual(len(candidates), 1)
        self.assertEqual(candidates[0].source_url, detail_url)
        self.assertEqual(candidates[0].source_id, "saramin")
        self.assertEqual(candidates[0].company, "회사명 확인 필요")
        self.assertEqual(candidates[0].source_posting_id, "54106686")
        self.assertEqual(candidates[0].title, "AI 엔지니어 채용")
        self.assertIn("LLM 에이전트", candidates[0].raw_jd["responsibilities"][0])
        self.assertIn("Python", candidates[0].raw_jd["required_qualifications"][0])
        self.assertIn("FastAPI", candidates[0].raw_jd["required_qualifications"])
        self.assertIn("Elasticsearch", candidates[0].raw_jd["preferred_qualifications"][0])
        self.assertIn("2026년 07월 07일", candidates[0].deadline_raw)
        self.assertIn("서울 강남구", candidates[0].location)


    def test_saramin_detail_rejects_final_endpoint_mismatch(self) -> None:
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
                "acquisition_strategy": "detail_only",
                "outer_strategy_approval": "not_probed",
                "candidate_include_keywords": ["python"],
                "require_robots": False,
                "explicit_automated_permission": True,
                "delay_seconds": 0,
            },
        )
        adapter = SaraminAdapter(manifest)
        detail_html = """
        <html><head>
        <script type="application/ld+json">
        {"@type":"JobPosting","title":"Python AI 엔지니어",
         "hiringOrganization":{"@type":"Organization","name":"Example AI"}}
        </script>
        </head><body>
        Python AI 엔지니어
        주요업무
        Python 기반 추천 시스템 개발
        자격요건
        Python API 개발 경험
        </body></html>
        """
        redirected_url = "https://www.saramin.co.kr/zf_user/jobs/view?rec_idx=54106686"
        with patch.object(
            adapter,
            "_fetch",
            return_value=HttpResponse(redirected_url, detail_html),
        ) as fetch:
            candidates = adapter.collect()

        self.assertEqual(len(candidates), 0)
        self.assertEqual(fetch.call_count, 1)
        self.assertEqual(len(adapter.errors), 1)
    def test_saramin_outer_requires_company_and_all_jd_sections(self) -> None:
        outer_url = "https://www.saramin.co.kr/zf_user/jobs/relay/view?rec_idx=54106686&rec_seq=0"
        options = {
            "detail_urls": [outer_url],
            "acquisition_strategy": "outer_only",
            "outer_strategy_approval": "approved",
            "candidate_include_keywords": ["python"],
            "require_robots": False,
            "explicit_automated_permission": True,
            "delay_seconds": 0,
        }
        base_html = """
        <html><head>
        <script type="application/ld+json">
        {"@type":"JobPosting","title":"Python AI 엔지니어",
         "hiringOrganization":{"@type":"Organization","name":"Example AI"}}
        </script>
        </head><body>
        Python AI 엔지니어
        주요업무
        Python 기반 추천 시스템 개발
        자격요건
        Python API 개발 경험
        우대사항
        LLM 서비스 경험
        </body></html>
        """
        cases = (
            (base_html, True),
            (base_html.replace("Example AI", "사람인"), False),
            (base_html.replace("우대사항\n        LLM 서비스 경험", ""), False),
        )
        for html, accepted in cases:
            with self.subTest(accepted=accepted):
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
                    options=options,
                )
                adapter = SaraminAdapter(manifest)
                with patch.object(
                    adapter, "_fetch", return_value=HttpResponse(outer_url, html)
                ):
                    candidates = adapter.collect()
                self.assertEqual(bool(candidates), accepted)
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
                "acquisition_strategy": "detail_only",
                "outer_strategy_approval": "not_probed",
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

    def test_saramin_adapter_discovers_script_embedded_relay_ids_from_listing(self) -> None:
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
                "acquisition_strategy": "detail_only",
                "outer_strategy_approval": "not_probed",
                "link_include_keywords": ["python"],
                "candidate_include_keywords": ["python"],
                "require_robots": False,
                "explicit_automated_permission": True,
                "delay_seconds": 0,
            },
        )
        list_html = """
        <script>
        window.recruitList = [
          {"rec_idx": "54106686", "title": "Python AI 엔지니어"},
          {"rec_idx": "11111111", "title": "주방 보조"}
        ];
        </script>
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


    def test_strategy_config_matrix_is_exclusive(self) -> None:
        base = {
            "source_id": "saramin",
            "enabled": True,
            "access_mode": "public_page",
            "options": {},
        }
        for strategy, approval in (("detail_only", "not_probed"), ("outer_only", "approved")):
            source = {**base, "options": {
                "acquisition_strategy": strategy,
                "outer_strategy_approval": approval,
            }}
            _validate_saramin_acquisition(source)
        invalid = (
            {},
            {"acquisition_strategy": "unknown", "outer_strategy_approval": "not_probed"},
            {"acquisition_strategy": "detail_only", "outer_strategy_approval": "approved"},
            {"acquisition_strategy": "outer_only", "outer_strategy_approval": "not_probed"},
        )
        for options in invalid:
            with self.subTest(options=options), self.assertRaises(ConfigError):
                _validate_saramin_acquisition({**base, "options": options})
        with self.assertRaises(ConfigError):
            _validate_saramin_acquisition({
                **base,
                "source_id": "wanted",
                "options": {"acquisition_strategy": "detail_only"},
            })
        with self.assertRaises(ConfigError):
            _validate_saramin_acquisition({
                **base,
                "options": {
                    "acquisition_strategy": "detail_only",
                    "outer_strategy_approval": "not_probed",
                    "detail_urls": ["https://www.saramin.co.kr/zf_user/jobs/relay/view-detail?rec_idx=1"],
                    "outer_urls": ["https://www.saramin.co.kr/zf_user/jobs/relay/view?rec_idx=1"],
                },
            })

    def test_saramin_canonicalization_rejects_untrusted_and_ambiguous_urls(self) -> None:
        invalid_urls = (
            "https://evil.example/zf_user/jobs/relay/view?rec_idx=1",
            "https://www.saramin.co.kr.evil/zf_user/jobs/relay/view?rec_idx=1",
            "https://www.saramin.co.kr/zf_user/jobs/relay/view?rec_idx=1&rec_idx=2",
            "https://www.saramin.co.kr/zf_user/jobs/relay/view?rec_idx=abc",
            "https://www.saramin.co.kr/zf_user/jobs/relay/view?rec_idx=0",
            "https://www.saramin.co.kr/zf_user/jobs/relay/view?rec_idx=1&next=2",
        )
        for url in invalid_urls:
            with self.subTest(url=url), self.assertRaises(SourceAccessError):
                _saramin_detail_url(url)
    def test_strategy_probe_fixture_selects_outer_only_and_redacts_summary(self) -> None:
        html = """
        <html><body>
        <p class="job-header__title">[Example AI] Python Engineer</p>
        주요업무
        Python API 개발
        자격요건
        Python API 개발 경험
        우대사항
        Python 오픈소스 경험
        </body></html>
        """

        def fetch(url: str) -> ProbeResponse:
            return ProbeResponse(url, html)

        request = ProbeRequest(("1", "2", "3"), Path("/tmp/recruit-crawler-saramin-probe-fixture"))
        summary = run_probe(request, fetch=fetch, clock=lambda: 0.0, sleep=lambda _: None)
        self.assertEqual(summary["decision"], "outer_only")
        self.assertEqual(summary["counters"]["pairs_outer_sufficient"], 3)

        with tempfile.TemporaryDirectory(prefix="recruit-crawler-saramin-probe-") as tmp:
            output = write_summary(Path(tmp), summary)
            payload = json.loads(output.read_text(encoding="utf-8"))
        self.assertEqual(set(payload), {"schema_version", "counters", "decision", "elapsed_ms"})
        self.assertNotIn("Example AI", json.dumps(payload, ensure_ascii=False))
        self.assertNotIn("Python API", json.dumps(payload, ensure_ascii=False))

    def test_strategy_probe_rejects_unauthorized_args_without_fetch(self) -> None:
        calls: list[str] = []
        with tempfile.TemporaryDirectory(prefix="recruit-crawler-saramin-probe-") as tmp:
            output_dir = Path(tmp) / "nested"
            exit_code = saramin_strategy_probe_main(
                [
                    "--rec-idx",
                    "1",
                    "--rec-idx",
                    "2",
                    "--rec-idx",
                    "3",
                    "--output-dir",
                    str(output_dir),
                ],
                fetch=lambda url: calls.append(url),
            )
        self.assertEqual(exit_code, 64)
        self.assertEqual(calls, [])
        self.assertFalse(output_dir.exists())
if __name__ == "__main__":
    unittest.main()
