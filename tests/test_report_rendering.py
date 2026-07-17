from datetime import date
import unicodedata
from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import patch

from recruit_crawler.report_policy import (
    MAX_DEGRADATION_NOTICES,
    MAX_REPORT_ROWS,
    REPORT_TABLE_COLUMNS,
    verified_link_url,
)
from recruit_crawler.summarizer import ReportRenderError, render_report_v3


def result(payload):
    return SimpleNamespace(
        run_date=date(2026, 7, 14), command_mode="scheduled-run", **payload
    )


def projection(queue=(), gate_sources=()):
    return {
        "summary": {
            "collected": len(queue),
            "apply_total": 1,
            "hold_total": 0,
            "manual_review_total": 0,
            "exclude": 0,
            "expired": 0,
            "low_priority_total": 0,
        },
        "report_queue": queue,
        "action_queue": queue,
        "manual_queue": (),
        "gate_sources": gate_sources,
    }


def row(**overrides):
    return {
        "final_disposition": "apply",
        "title": "공고",
        "company": "회사",
        "location": "서울",
        "deadline": "2026-08-01",
        "reason_codes": (),
        **overrides,
    }


class ReportRenderingTests(TestCase):
    def _render(
        self, queue=(), gate_sources=(), result_payload=None, private_canaries=()
    ):
        payload = result_payload or {}
        with patch(
            "recruit_crawler.projection.project_pipeline_result",
            return_value=projection(queue, gate_sources),
        ):
            return render_report_v3(
                result(payload), private_canaries=private_canaries
            ).markdown_bytes.decode("utf-8")

    def test_exact_eight_column_table_and_no_score_or_evidence(self):
        text = self._render((row(score=99, matched_evidence=("secret",)),))

        self.assertIn("| " + " | ".join(REPORT_TABLE_COLUMNS) + " |", text)
        self.assertNotIn("99", text)
        self.assertNotIn("secret", text)

    def test_all_public_labels_appear_once_in_table_rows(self):
        queue = (
            row(final_disposition="apply", title="안전 | 제목\n줄바꿈"),
            row(final_disposition="hold"),
            row(final_disposition="manual_review"),
            row(final_disposition="exclude"),
        )
        text = self._render(queue)
        lines = text.splitlines()
        header = "| " + " | ".join(REPORT_TABLE_COLUMNS) + " |"
        table_start = lines.index(header)
        data_rows = []
        for line in lines[table_start + 2 :]:
            if not line.startswith("|"):
                break
            data_rows.append(line)

        self.assertEqual(lines.count(header), 1)
        self.assertEqual(len(data_rows), 4)
        labels = [line.split("|")[2].strip() for line in data_rows]
        self.assertEqual(labels, ["지원 추천", "도전 지원", "원문 확인 필요", "제외"])

    def test_safe_link_only(self):
        item = row(
            source_id="saramin",
            source_posting_id="123",
            source_detail_quality="verified",
            source_url=(
                "https://www.saramin.co.kr/zf_user/jobs/relay/"
                "view-detail?rec_idx=123&rec_seq=0"
            ),
        )
        text = self._render((item,))

        self.assertIn(
            "[열기](<https://www.saramin.co.kr/zf_user/jobs/relay/"
            "view-detail?rec_idx=123&rec_seq=0>)",
            text,
        )
    def test_configured_canaries_use_safe_row_fallbacks_and_hide_links(self):
        canary = "Café Private"
        item = row(
            title=unicodedata.normalize("NFD", canary),
            company=canary.upper(),
            location=canary.swapcase(),
            deadline=canary,
            source_id="fixture",
            source_posting_id="fixture-canary",
            source_detail_quality="verified",
            source_url=f"https://jobs.example.test/{canary}",
        )

        text = self._render((item,), result_payload={}, private_canaries=(canary,))
        self.assertNotIn(canary.casefold(), unicodedata.normalize("NFC", text).casefold())
        self.assertIn("검토 필요 공고", text)
        self.assertIn("확인 필요", text)

    def test_non_detail_or_hostile_links_are_not_clickable(self):
        invalid_links = (
            ("https://jobs.example.test/list-001", "list-001", "verified"),
            ("https://jobs.example.test/search-001", "search-001", "verified"),
            ("https://jobs.example.test/generic-001", "generic-001", "verified"),
            ("https://jobs.example.test/fx-apply-001", "fx-hold-001", "verified"),
            ("https://jobs.example.test/fx-apply-001", "fx-apply-001", "manual_only"),
            ("http://jobs.example.test/fx-apply-001", "fx-apply-001", "verified"),
            ("https://user@jobs.example.test/fx-apply-001", "fx-apply-001", "verified"),
            ("https://jobs.example.test:443/fx-apply-001", "fx-apply-001", "verified"),
            ("https://jobs.example.test/fx-apply-001#fragment", "fx-apply-001", "verified"),
            ("https://jobs.example.test/fx-apply-001?query=1", "fx-apply-001", "verified"),
            ("https://unapproved.example/fx-apply-001", "fx-apply-001", "verified"),
            ("https://jobs.example.test/fx-apply-001\x01", "fx-apply-001", "verified"),
        )
        for source_url, posting_id, quality in invalid_links:
            with self.subTest(source_url=source_url, quality=quality):
                self.assertIsNone(
                    verified_link_url(
                        "scheduled-run",
                        "fixture",
                        source_url,
                        posting_id,
                        quality,
                    )
                )
    def test_hostile_urls_are_non_clickable_in_rendered_rows(self):
        for source_url in (
            "javascript:alert(1)",
            "https://unapproved.example/fx-apply-001",
            "https://jobs.example.test/fx-apply-001#injected",
            "https://jobs.example.test/fx-apply-001\x01",
        ):
            with self.subTest(source_url=source_url):
                text = self._render(
                    (
                        row(
                            source_id="fixture",
                            source_url=source_url,
                            source_posting_id="fx-apply-001",
                            source_detail_quality="verified",
                        ),
                    )
                )
                self.assertNotIn("[열기]", text)
                self.assertIn("확인 필요", text)

    def test_queue_capacity_fails_before_markdown_builders_run(self):
        queue = tuple(row(title=str(index)) for index in range(MAX_REPORT_ROWS + 1))
        with (
            patch(
                "recruit_crawler.projection.project_pipeline_result",
                return_value=projection(queue),
            ),
            patch("recruit_crawler.summarizer._escape") as escape,
            patch("recruit_crawler.summarizer._row") as render_row,
            self.assertRaisesRegex(ReportRenderError, "queue exceeds capacity"),
        ):
            render_report_v3(result({}))

        escape.assert_not_called()
        render_row.assert_not_called()

    def test_notice_capacity_fails_before_markdown_builders_run(self):
        gate_sources = tuple(
            SimpleNamespace(
                source_id=f"fixture-{index}",
                candidate_count=0,
                error_codes=(),
            )
            for index in range(MAX_DEGRADATION_NOTICES + 1)
        )
        with (
            patch(
                "recruit_crawler.projection.project_pipeline_result",
                return_value=projection((), gate_sources),
            ),
            patch("recruit_crawler.summarizer._escape") as escape,
            patch("recruit_crawler.summarizer._row") as render_row,
            self.assertRaisesRegex(ReportRenderError, "notices exceed capacity"),
        ):
            render_report_v3(result({}))

        escape.assert_not_called()
        render_row.assert_not_called()
