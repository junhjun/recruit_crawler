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


def _rendered(content: bytes = b"# report\n") -> RenderedReportV2:
    return RenderedReportV2(
        schema_version=REPORT_ARTIFACT_SCHEMA_VERSION,
        markdown_bytes=content,
        content_sha256=hashlib.sha256(content).hexdigest(),
        byte_length=len(content),
    )


class ReportWriterTests(TestCase):
    def test_publication_result_is_typed_and_atomically_published(self):
        with TemporaryDirectory() as directory:
            result = publish_report(
                Path(directory), date(2026, 7, 14), _rendered(), report_slug="daily"
            )
            self.assertIsInstance(result, ReportPublicationResultV1)
            self.assertIsNone(result.failure_code)
            self.assertEqual(result.durability, "published")
            self.assertTrue(result.artifact.generated)
            self.assertEqual(Path(result.artifact.path).read_bytes(), b"# report\n")

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
            self.assertEqual(target.read_bytes(), b"# report\n")
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
            self.assertEqual(result.durability, "published")
            self.assertFalse(result.artifact.generated)
            self.assertIsNotNone(result.reconciliation)
            self.assertEqual(
                result.reconciliation.observed_identity,
                result.reconciliation.candidate_identity,
            )
            self.assertEqual(target.read_bytes(), b"# report\n")

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
