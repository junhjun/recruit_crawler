from __future__ import annotations

import hashlib
import os
from datetime import date
from pathlib import Path
import unicodedata
from tempfile import TemporaryDirectory
from unittest import TestCase
from unittest.mock import patch

from recruit_crawler.report_writer import (
    ReportPublicationResultV1,
    publish_report,
    write_report,
)
from recruit_crawler.schemas import (
    REPORT_ARTIFACT_SCHEMA_VERSION,
    RenderedReportV2,
)
from recruit_crawler.report_policy import (
    MAX_DEGRADATION_NOTICES,
    MAX_REPORT_BYTES,
    MAX_REPORT_ROWS,
)


_VALID_REPORT = (
    "# 채용 추천 리포트 — 2026-07-14\n\n"
    "## 한눈에 보기\n"
    "- 수집: 0\n"
    "- 상세 거부: 0\n"
    "- 중복 제거: 0\n\n"
    "## 지원/검토\n"
    "| 순위 | 판정 | 공고 | 회사 | 지역 | 마감 | 사유 | 링크 |\n"
    "| --- | --- | --- | --- | --- | --- | --- | --- |\n"
).encode("utf-8")


def _rendered(content: bytes = _VALID_REPORT) -> RenderedReportV2:
    return RenderedReportV2(
        schema_version=REPORT_ARTIFACT_SCHEMA_VERSION,
        markdown_bytes=content,
        content_sha256=hashlib.sha256(content).hexdigest(),
        byte_length=len(content),
    )


class ReportWriterTests(TestCase):
    def _assert_rejected_before_replace(self, content: bytes) -> None:
        with TemporaryDirectory() as directory:
            target = Path(directory) / "daily-2026-07-14.md"
            target.write_bytes(b"old\n")
            result = publish_report(
                Path(directory), date(2026, 7, 14), _rendered(content), report_slug="daily"
            )

            self.assertEqual(result.failure_code, "artifact_invalid")
            self.assertEqual(result.durability, "not_published")
            self.assertFalse(result.artifact.generated)
            self.assertEqual(target.read_bytes(), b"old\n")

    def test_malformed_or_extra_table_is_rejected_before_replace(self):
        malformed = _VALID_REPORT + b"\n| hidden | table | must | not | pass | writer | check | now |\n"
        self._assert_rejected_before_replace(malformed)
    def test_canonical_grammar_rejects_extra_prose_unknown_bullets_and_noncontiguous_ranks(self):
        row_one = "| 1 | 지원 추천 | 공고 | 회사 | 서울 | 확인 필요 | 사유 | 확인 필요 |\n".encode("utf-8")
        row_three = "| 3 | 지원 추천 | 공고 | 회사 | 서울 | 확인 필요 | 사유 | 확인 필요 |\n".encode("utf-8")
        for suffix in (
            "\n추가 설명\n".encode("utf-8"),
            "\n- 알 수 없는 안내\n".encode("utf-8"),
            row_one + row_three,
        ):
            with self.subTest(suffix=suffix):
                self._assert_rejected_before_replace(_VALID_REPORT + suffix)
    def test_writer_rejects_overbound_summary_and_empty_degradation_block(self):
        oversized_summary = _VALID_REPORT.replace(
            b"- \xec\x88\x98\xec\xa7\x91: 0",
            b"- \xec\x88\x98\xec\xa7\x91: 100000000000000000000",
        )
        empty_degradation = _VALID_REPORT + (
            "\n## 수집 저하 안내\n"
            "- 일부 활성 소스의 수집이 완료되지 않았습니다. Gate 상태는 fail입니다.\n"
        ).encode("utf-8")

        self._assert_rejected_before_replace(oversized_summary)
        self._assert_rejected_before_replace(empty_degradation)
    def test_writer_rejects_autolinks_and_html_in_every_table_cell(self):
        for title, link in (
            ("<https://evil.example/jobs/1>", "확인 필요"),
            ('<a href="https://evil.example/jobs/1">open</a>', "확인 필요"),
            ("\\\\[공고](https://evil.example/jobs/1)", "확인 필요"),
            ("[\\]](javascript:alert(1))", "확인 필요"),
            ("공고", "<https://evil.example/jobs/1>"),
            ("공고", '<a href="https://evil.example/jobs/1">open</a>'),
        ):
            row = (
                f"| 1 | 지원 추천 | {title} | 회사 | 서울 | 확인 필요 | 사유 | {link} |\n"
            ).encode("utf-8")
            with self.subTest(title=title, link=link):
                self._assert_rejected_before_replace(_VALID_REPORT + row)

    def test_injected_row_notice_and_total_capacity_are_rejected_before_replace(self):
        row = "| 1 | 지원 추천 | 공고 | 회사 | 서울 | 확인 필요 | 사유 | 확인 필요 |\n".encode("utf-8")
        over_rows = _VALID_REPORT + row * (MAX_REPORT_ROWS + 1)
        notices = "## 수집 저하 안내\n".encode("utf-8") + (
            "- 소스 `fixture`: collection_failed\n".encode("utf-8")
            * (MAX_DEGRADATION_NOTICES + 1)
        )
        over_total = _VALID_REPORT + b"x" * (MAX_REPORT_BYTES + 1)

        for content in (over_rows, _VALID_REPORT + notices, over_total):
            with self.subTest(byte_length=len(content)):
                self._assert_rejected_before_replace(content)
    def test_publication_result_is_typed_and_atomically_published(self):
        with TemporaryDirectory() as directory:
            result = publish_report(
                Path(directory), date(2026, 7, 14), _rendered(), report_slug="daily"
            )
            self.assertIsInstance(result, ReportPublicationResultV1)
            self.assertIsNone(result.failure_code)
            self.assertEqual(result.durability, "published")
            self.assertTrue(result.artifact.generated)
            self.assertEqual(Path(result.artifact.path).read_bytes(), _VALID_REPORT)
    def test_canonical_writer_accepts_escaped_table_cells(self):
        content = _VALID_REPORT + (
            "| 1 | 지원 추천 | 안전 \\| 제목 | 회사 | 서울 | 확인 필요 | 사유 | 확인 필요 |\n"
        ).encode("utf-8")
        with TemporaryDirectory() as directory:
            result = publish_report(
                Path(directory), date(2026, 7, 14), _rendered(content), report_slug="daily"
            )

        self.assertTrue(result.artifact.generated)
        self.assertEqual(result.durability, "published")

    def test_raw_text_is_rejected_without_creating_a_target(self):
        with TemporaryDirectory() as directory:
            result = write_report(
                Path(directory), date(2026, 7, 14), "raw text", report_slug="daily"
            )
            self.assertEqual(result.failure_code, "artifact_invalid")
            self.assertEqual(result.durability, "not_published")
            self.assertFalse(result.artifact.generated)
            self.assertFalse((Path(directory) / "daily-2026-07-14.md").exists())

    def test_invalid_artifact_is_rejected_before_destination_changes(self):
        with TemporaryDirectory() as directory:
            target = Path(directory) / "daily-2026-07-14.md"
            target.write_bytes(b"old\n")
            invalid = _rendered()
            invalid = RenderedReportV2(
                schema_version=invalid.schema_version,
                markdown_bytes=invalid.markdown_bytes,
                content_sha256="0" * 64,
                byte_length=invalid.byte_length,
            )
            result = publish_report(
                Path(directory), date(2026, 7, 14), invalid, report_slug="daily"
            )
            self.assertEqual(result.failure_code, "artifact_invalid")
            self.assertEqual(result.durability, "not_published")
            self.assertEqual(target.read_bytes(), b"old\n")
    def test_correctly_hashed_unsafe_report_is_rejected_before_replace(self):
        with TemporaryDirectory() as directory:
            target = Path(directory) / "daily-2026-07-14.md"
            target.write_bytes(b"old\n")
            unsafe = _rendered(b"# PRIVATE_PROFILE_CANARY\n")
            result = publish_report(
                Path(directory),
                date(2026, 7, 14),
                unsafe,
                report_slug="daily",
            )
            self.assertEqual(result.failure_code, "artifact_invalid")
            self.assertEqual(result.durability, "not_published")
            self.assertEqual(target.read_bytes(), b"old\n")

    def test_configured_canary_is_rejected_even_without_a_generic_marker(self):
        with TemporaryDirectory() as directory:
            target = Path(directory) / "daily-2026-07-14.md"
            target.write_bytes(b"old\n")
            unsafe = _rendered(b"# arbitrary-configured-value\n")
            result = publish_report(
                Path(directory),
                date(2026, 7, 14),
                unsafe,
                report_slug="daily",
                private_canaries=("arbitrary-configured-value",),
            )
            self.assertEqual(result.failure_code, "artifact_invalid")
            self.assertEqual(result.durability, "not_published")
            self.assertEqual(target.read_bytes(), b"old\n")
    def test_nfc_nfd_equivalent_configured_canary_is_rejected(self):
        nfc_canary = "Café Secret"
        nfd_canary = unicodedata.normalize("NFD", nfc_canary)
        for configured, body in ((nfc_canary, nfd_canary), (nfd_canary, nfc_canary)):
            with self.subTest(configured=configured is nfc_canary), TemporaryDirectory() as directory:
                content = f"# {body.upper()}\n".encode("utf-8")
                unsafe = _rendered(content)
                self.assertEqual(unsafe.content_sha256, hashlib.sha256(content).hexdigest())
                result = publish_report(
                    Path(directory),
                    date(2026, 7, 14),
                    unsafe,
                    report_slug="daily",
                    private_canaries=(configured,),
                )
                self.assertEqual(result.failure_code, "artifact_invalid")
                self.assertEqual(result.durability, "not_published")

    def test_compact_military_terms_are_rejected_before_publish(self):
        for expression in ("군복무", "군 면제", "대체복무", "병역특례"):
            with self.subTest(expression=expression), TemporaryDirectory() as directory:
                result = publish_report(
                    Path(directory),
                    date(2026, 7, 14),
                    _rendered(f"# {expression}\n".encode("utf-8")),
                    report_slug="daily",
                )
                self.assertEqual(result.failure_code, "artifact_invalid")
                self.assertEqual(result.durability, "not_published")
                self.assertFalse(
                    (Path(directory) / "daily-2026-07-14.md").exists()
                )

    def test_unicode_casefold_configured_canary_is_rejected(self):
        configured_canary = "Straße Secret"
        content = "# STRASSE SECRET\n".encode("utf-8")
        with TemporaryDirectory() as directory:
            result = publish_report(
                Path(directory),
                date(2026, 7, 14),
                _rendered(content),
                report_slug="daily",
                private_canaries=(configured_canary,),
            )
        self.assertEqual(result.failure_code, "artifact_invalid")
        self.assertEqual(result.durability, "not_published")
    def test_replace_failure_preserves_existing_output(self):
        with TemporaryDirectory() as directory:
            target = Path(directory) / "daily-2026-07-14.md"
            target.write_bytes(b"old\n")
            with patch(
                "recruit_crawler.report_writer.os.replace",
                side_effect=OSError("replace unavailable"),
            ):
                result = publish_report(
                    Path(directory), date(2026, 7, 14), _rendered(), report_slug="daily"
                )

            self.assertEqual(result.failure_code, "write_failed_pre_replace")
            self.assertEqual(result.durability, "not_published")
            self.assertFalse(result.artifact.generated)
            self.assertEqual(target.read_bytes(), b"old\n")
    def test_pre_replace_fsync_failure_preserves_existing_output(self):
        with TemporaryDirectory() as directory:
            target = Path(directory) / "daily-2026-07-14.md"
            target.write_bytes(b"old\n")
            with patch(
                "recruit_crawler.report_writer.os.fsync",
                side_effect=OSError("file fsync unavailable"),
            ):
                result = publish_report(
                    Path(directory), date(2026, 7, 14), _rendered(), report_slug="daily"
                )

            self.assertEqual(result.failure_code, "write_failed_pre_replace")
            self.assertEqual(result.durability, "not_published")
            self.assertFalse(result.artifact.generated)
            self.assertEqual(target.read_bytes(), b"old\n")
    def test_post_replace_directory_fsync_is_indeterminate(self):
        with TemporaryDirectory() as directory:
            target = Path(directory) / "daily-2026-07-14.md"
            target.write_bytes(b"old\n")
            with patch(
                "recruit_crawler.report_writer.os.fsync",
                side_effect=(None, OSError("directory fsync unavailable")),
            ):
                result = publish_report(
                    Path(directory), date(2026, 7, 14), _rendered(), report_slug="daily"
                )
            self.assertEqual(result.failure_code, "fsync_failed_post_replace")
            self.assertEqual(result.durability, "indeterminate")
            self.assertEqual(target.read_bytes(), _VALID_REPORT)
    def test_interrupted_replace_reconciles_candidate_without_claiming_success(self):
        with TemporaryDirectory() as directory:
            target = Path(directory) / "daily-2026-07-14.md"
            target.write_bytes(b"old\n")
            original_replace = os.replace

            def replace_then_interrupt(source, destination):
                original_replace(source, destination)
                raise TimeoutError("interrupted after replace")

            with patch(
                "recruit_crawler.report_writer.os.replace",
                side_effect=replace_then_interrupt,
            ):
                result = publish_report(
                    Path(directory),
                    date(2026, 7, 14),
                    _rendered(),
                    report_slug="daily",
                )

            self.assertEqual(result.failure_code, "runtime_deadline_exceeded")
            self.assertEqual(result.durability, "indeterminate")
            self.assertFalse(result.artifact.generated)
            self.assertIsNotNone(result.reconciliation)
            self.assertEqual(
                result.reconciliation.observed_identity,
                result.reconciliation.candidate_identity,
            )
            self.assertEqual(target.read_bytes(), _VALID_REPORT)

    def test_interrupted_replace_before_bytes_change_is_not_published(self):
        with TemporaryDirectory() as directory:
            target = Path(directory) / "daily-2026-07-14.md"
            target.write_bytes(b"old\n")
            with patch(
                "recruit_crawler.report_writer.os.replace",
                side_effect=TimeoutError("interrupted before replace"),
            ):
                result = publish_report(
                    Path(directory),
                    date(2026, 7, 14),
                    _rendered(),
                    report_slug="daily",
                )

            self.assertEqual(result.failure_code, "write_failed_pre_replace")
            self.assertEqual(result.durability, "not_published")
            self.assertEqual(target.read_bytes(), b"old\n")
            self.assertEqual(
                result.reconciliation.observed_identity,
                result.reconciliation.preimage_identity,
            )
    def test_publish_rejects_unsafe_or_noncanonical_markdown_links(self):
        row = "| 1 | 지원 추천 | [공고](javascript:alert(1)) | 회사 | 서울 | 확인 필요 | 사유 | 확인 필요 |\n".encode()
        self._assert_rejected_before_replace(_VALID_REPORT + row)
        for url in (
            "https://evil.example/jobs/1",
            "https://www.jobkorea.co.kr/Recruit/GI_Read/1?x=1",
        ):
            row = f"| 1 | 지원 추천 | 공고 | 회사 | 서울 | 확인 필요 | 사유 | [열기](<{url}>) |\n".encode()
            self._assert_rejected_before_replace(_VALID_REPORT + row)

    def test_publish_accepts_canonical_verified_detail_link(self):
        url = "https://www.jobkorea.co.kr/Recruit/GI_Read/123"
        row = f"| 1 | 지원 추천 | 공고 | 회사 | 서울 | 확인 필요 | 사유 | [열기](<{url}>) |\n".encode()
        with TemporaryDirectory() as directory:
            result = publish_report(
                Path(directory), date(2026, 7, 14), _rendered(_VALID_REPORT + row), report_slug="daily"
            )
        self.assertIsNone(result.failure_code)
        self.assertTrue(result.artifact.generated)
