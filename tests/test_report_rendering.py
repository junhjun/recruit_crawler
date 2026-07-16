from __future__ import annotations

from datetime import date
from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import patch

from recruit_crawler.schemas import REPORT_ARTIFACT_SCHEMA_VERSION
from recruit_crawler.projection import project_public_assessment
from recruit_crawler.summarizer import (
    ReportRenderError,
    render_report_v3,
)


def _result():
    return SimpleNamespace(run_date=date(2026, 7, 14), command_mode="scheduled-run")


def _projection(*, title="공고", company="회사", evidence=("근거",), reason=("manual_flag",)):
    return {
        "summary": {
            "collected": 1,
            "source_rejected": 0,
            "duplicates_removed": 0,
            "apply_total": 1,
            "hold_total": 0,
            "manual_review_total": 0,
            "low_priority_total": 0,
            "exclude": 0,
            "expired": 0,
            "displayed_apply": 1,
            "displayed_hold": 0,
            "displayed_manual": 0,
            "suppressed_apply": 0,
            "suppressed_hold": 0,
            "suppressed_manual": 0,
        },
        "action_queue": ({
            "final_disposition": "apply",
            "score": 91,
            "title": title,
            "company": company,
            "deadline": "2026-08-01",
            "source_url": "https://example.test/job",
            "matched_evidence": list(evidence),
            "reason_codes": list(reason),
        },),
        "manual_queue": (),
    }


class ReportRenderingTests(TestCase):
    def test_structured_fields_are_escaped_once_and_codes_are_not_public(self):
        with patch(
            "recruit_crawler.projection.project_pipeline_result",
            return_value=_projection(
                title="A & <B> [원문]",
                evidence=("경험 & 근거",),
            ),
        ):
            rendered = render_report_v3(_result())

        text = rendered.markdown_bytes.decode("utf-8")
        self.assertIn("A &amp; &lt;B&gt; \\[원문\\]", text)
        self.assertNotIn("&amp;amp;", text)
        self.assertIn("지원 추천", text)
        self.assertNotIn("apply", text)

    def test_dynamic_rows_stay_in_sections_before_exclusion_footer(self):
        projection = _projection()
        projection["summary"].update(
            manual_review_total=1,
            displayed_manual=1,
        )
        projection["manual_queue"] = (
            {
                "title": "수동 공고",
                "company": "수동 회사",
                "source_url": "https://example.test/manual",
                "reason_codes": ["manual_source"],
            },
        )
        with patch(
            "recruit_crawler.projection.project_pipeline_result",
            return_value=projection,
        ):
            rendered = render_report_v3(_result())

        text = rendered.markdown_bytes.decode("utf-8")
        lines = text.rstrip("\n").split("\n")
        action_header = lines.index("| --- | --- | --- | --- | --- | --- | --- | --- | --- |")
        manual_header = lines.index("| --- | --- | --- | --- |")
        exclusion = lines.index("## 제외")
        self.assertEqual(lines[action_header + 1].split("|")[2].strip(), "지원 추천")
        self.assertIn("공고", lines[action_header - 1])
        self.assertIn("회사", lines[action_header - 1])
        self.assertIn("링크", lines[action_header - 1])
        self.assertEqual(lines[manual_header + 1].split("|")[2].strip(), "원문 확인 필요")
        self.assertIn("수동 공고 - 수동 회사", text)
        self.assertLess(action_header, manual_header)
        self.assertLess(manual_header, exclusion)
        self.assertFalse(
            any(
                line.startswith("| 1 | 지원 추천") or line.startswith("| 1 | 원문 확인 필요")
                for line in lines[exclusion + 1 :]
            )
        )
        self.assertLessEqual(len(text), 2000)
        self.assertLessEqual(len(rendered.markdown_bytes), 122880)

    def test_verified_source_link_and_company_are_rendered_in_action_queue(self):
        projection = _projection(title="추천 공고", company="검증 회사")
        projection["action_queue"] = (
            {
                **projection["action_queue"][0],
                "source_id": "saramin",
                "source_url": (
                    "https://www.saramin.co.kr/zf_user/jobs/relay/"
                    "view-detail?rec_idx=123&rec_seq=0"
                ),
                "source_posting_id": "123",
                "source_detail_quality": "verified",
            },
        )
        with patch(
            "recruit_crawler.projection.project_pipeline_result",
            return_value=projection,
        ):
            rendered = render_report_v3(_result())

        text = rendered.markdown_bytes.decode("utf-8")
        self.assertIn("검증 회사", text)
        self.assertIn(
            "[열기](<https://www.saramin.co.kr/zf_user/jobs/relay/"
            "view-detail?rec_idx=123&rec_seq=0>)",
            text,
        )

    def test_unverified_source_link_is_not_rendered_as_clickable(self):
        projection = _projection()
        projection["action_queue"] = (
            {
                **projection["action_queue"][0],
                "source_id": "saramin",
                "source_url": "https://www.saramin.co.kr/jobs/list",
                "source_posting_id": "123",
                "source_detail_quality": "verified",
            },
        )
        with patch(
            "recruit_crawler.projection.project_pipeline_result",
            return_value=projection,
        ):
            rendered = render_report_v3(_result())

        text = rendered.markdown_bytes.decode("utf-8")
        self.assertNotIn("https://www.saramin.co.kr/jobs/list", text)
        self.assertIn("확인 필요", text)

    def test_long_fields_reduce_to_both_line_caps(self):
        long_text = "가" * 2000 + " & [원문]"
        with patch(
            "recruit_crawler.projection.project_pipeline_result",
            return_value=_projection(title=long_text, company=long_text, evidence=(long_text,)),
        ), patch("recruit_crawler.summarizer._REPORT_DOCUMENT_CODEPOINT_CAP", 1800), patch(
            "recruit_crawler.summarizer._REPORT_DOCUMENT_BYTE_CAP", 2500
        ):
            rendered = render_report_v3(_result())

        text = rendered.markdown_bytes.decode("utf-8")
        lines = text.rstrip("\n").split("\n")
        self.assertTrue(all(len(line) <= 480 for line in lines))
        self.assertTrue(all(len(line.encode("utf-8")) <= 768 for line in lines))
        self.assertLessEqual(len(text), 1800)
        self.assertLessEqual(len(rendered.markdown_bytes), 2500)

    def test_impossible_document_budget_returns_controlled_error(self):
        with patch(
            "recruit_crawler.projection.project_pipeline_result",
            return_value=_projection(),
        ), patch("recruit_crawler.summarizer._REPORT_DOCUMENT_CODEPOINT_CAP", 1), patch(
            "recruit_crawler.summarizer._REPORT_DOCUMENT_BYTE_CAP", 1
        ):
            with self.assertRaises(ReportRenderError):
                render_report_v3(_result())

    def test_projection_sanitizes_values_before_report_rendering(self):
        assessment = SimpleNamespace(
            recommendation_id="PRIVATE_PROFILE_CANARY",
            posting_key="RAW_JD_CANARY",
            source_id="fixture",
            source_url="https://jobs.example.test/PRIVATE_PROFILE_CANARY",
            source_posting_id="PRIVATE_PROFILE_CANARY",
            title="상세 설명 " * 30,
            company="RAW_JD_CANARY",
            location="군필 상세 정보",
            deadline=date(2026, 8, 1),
            score=91,
            disposition="apply",
            reason_codes=("manual_flag", "military_program_review", "PRIVATE_PROFILE_CANARY"),
            detail_quality="verified",
            matched_evidence=("필수 요건: Python", "PRIVATE_PROFILE_CANARY", "병역: 현역"),
        )
        projected = project_public_assessment(assessment)
        projection = _projection()
        projection["action_queue"] = (projected,)

        with patch(
            "recruit_crawler.projection.project_pipeline_result",
            return_value=projection,
        ):
            rendered = render_report_v3(_result())

        text = rendered.markdown_bytes.decode("utf-8")
        self.assertNotIn("상세 설명", text)
        self.assertIn("필수 요건 일치", text)
        for value in (
            "PRIVATE_PROFILE_CANARY",
            "RAW_JD_CANARY",
            "Ignore previous instructions",
            "군필 상세 정보",
            "military_program_review",
        ):
            self.assertNotIn(value, text)
        self.assertIn("manual_flag", str(projected["reason_codes"]))
    def test_unverified_assessment_url_is_omitted_from_public_projection(self):
        assessment = SimpleNamespace(
            source_id="wanted",
            source_url="https://www.wanted.co.kr/search?query=PRIVATE_PROFILE_CANARY",
            source_posting_id="123456",
            detail_quality="verified",
        )
        projected = project_public_assessment(assessment, command_mode="scheduled-run")
        self.assertIsNone(projected["source_url"])
    def test_rendered_schema_is_canonical(self):
        with patch(
            "recruit_crawler.projection.project_pipeline_result",
            return_value=_projection(),
        ):
            rendered = render_report_v3(_result())
        self.assertEqual(rendered.schema_version, REPORT_ARTIFACT_SCHEMA_VERSION)
        self.assertEqual(rendered.byte_length, len(rendered.markdown_bytes))
        self.assertTrue(rendered.markdown_bytes.endswith(b"\n"))
