from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

import json
import tempfile
from datetime import date

from recruit_crawler.capture_import import import_capture_files, select_capture_files
from recruit_crawler.config import load_config
from recruit_crawler.pipeline import run_capture_import

CONFIG = ROOT / "config" / "sample_config.json"


class CaptureImportTests(unittest.TestCase):
    def _write_config(self, tmp_path: Path) -> Path:
        raw = json.loads(CONFIG.read_text(encoding="utf-8"))
        raw["output_dir"] = str(tmp_path / "reports")
        config_path = tmp_path / "config.json"
        config_path.write_text(json.dumps(raw), encoding="utf-8")
        return config_path

    def test_capture_import_maps_mixed_sources_and_generates_report(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            day_dir = tmp_path / "spool" / "2026-06-30"
            for source in ("linkedin", "saramin", "jobkorea"):
                (day_dir / source).mkdir(parents=True)
            captures = [
                {
                    "source_id": "linkedin",
                    "captured_at": "2026-06-30T04:00:00Z",
                    "postings": [
                        {
                            "source_id": "linkedin",
                            "source_url": "https://www.linkedin.com/jobs/view/4432928554/",
                            "source_posting_id": "4432928554",
                            "title": "LinkedIn Data Engineer",
                            "company": "LinkedIn Partner Co",
                            "location": "서울 서울",
                            "deadline": "",
                            "skills": ["Python", "SQL"],
                            "requirements": "Minimum Qualifications Python Programming Deep learning with PyTorch",
                            "captured_at": "2026-06-30T04:00:00Z",
                            "unexpected_private_note": "SHOULD_NOT_LEAK",
                        }
                    ],
                },
                {
                    "source_id": "saramin",
                    "captured_at": "2026-06-30T04:01:00Z",
                    "postings": [
                        {
                            "source_id": "saramin",
                            "source_url": "https://www.saramin.co.kr/zf_user/jobs/relay/view?rec_idx=54106686",
                            "source_posting_id": "54106686",
                            "title": "AI 엔지니어 채용",
                            "company": "레플리",
                            "location": "서울 강남구",
                            "deadline": "",
                            "skills": ["신입", "AI", "Python"],
                            "requirements": "주요업무 LLM 에이전트 및 RAG 시스템 설계 자격요건 Python FastAPI 마감일 : 2026년 07월 07일",
                            "captured_at": "2026-06-30T04:01:00Z",
                        }
                    ],
                },
                {
                    "source_id": "jobkorea",
                    "captured_at": "2026-06-30T04:02:00Z",
                    "postings": [
                        {
                            "source_id": "jobkorea",
                            "source_url": "https://www.jobkorea.co.kr/Recruit/GI_Read/49476607?sc=322",
                            "source_posting_id": "49476607",
                            "title": "[울산]Forward Deployed 엔지니어",
                            "company": "플라잎",
                            "location": "울산 남구 마감일 ~7/28(화",
                            "deadline": "",
                            "skills": ["AI", "Python"],
                            "requirements": "이런 업무를 해요 고객 자동화 작업 분석 이런 분들을 찾고 있어요 PyTorch Jax 근무지 주소 : 울산광역시 남구 옥현로 129 지도보기 이 기간동안 모집해요 ~ 2026.07.28(화)",
                            "captured_at": "2026-06-30T04:02:00Z",
                        },
                        {
                            "source_id": "jobkorea",
                            "source_url": "https://www.jobkorea.co.kr/Recruit/GI_Read/49476607?sc=322",
                            "source_posting_id": "49476607",
                            "title": "Duplicate",
                            "company": "플라잎",
                            "location": "울산 남구",
                            "skills": ["Python"],
                            "requirements": "duplicate",
                        },
                    ],
                },
                {"source_id": "saramin", "captured_at": "2026-06-30T04:03:00Z", "postings": []},
            ]
            for index, capture in enumerate(captures):
                source = capture["source_id"]
                (day_dir / source / f"capture-{index}.json").write_text(json.dumps(capture), encoding="utf-8")
            (day_dir / "jobkorea" / "invalid.json").write_text("{not-json", encoding="utf-8")

            selection = select_capture_files(tmp_path / "spool", run_date=date(2026, 6, 30))
            imported = import_capture_files(selection.files)
            config = load_config(self._write_config(tmp_path))
            summary, report, ranked = run_capture_import(
                config,
                selection.run_date,
                imported.candidates,
                imported.sources_attempted,
                imported.source_errors,
            )

        self.assertEqual(summary.candidates_collected, 3)
        self.assertEqual(summary.duplicates_removed, 0)
        self.assertEqual(summary.ranked_count, 3)
        self.assertEqual(imported.sources_attempted, ["jobkorea", "linkedin", "saramin"])
        self.assertTrue(any("invalid JSON" in error for error in summary.source_errors))
        self.assertTrue(any("empty postings" in error for error in summary.source_errors))
        self.assertTrue(any("duplicate posting jobkorea:49476607" in error for error in summary.source_errors))
        self.assertIn("LinkedIn Data Engineer", report)
        self.assertIn("AI 엔지니어 채용", report)
        self.assertIn("[울산]Forward Deployed 엔지니어", report)
        self.assertIn("울산광역시 남구 옥현로 129", report)
        self.assertIn("2026-07-28", report)
        self.assertNotIn("울산 남구 마감일", report)
        self.assertNotIn("SHOULD_NOT_LEAK", report)
        self.assertIn("PyTorch", report)
        self.assertIn("FastAPI", report)
        self.assertEqual({item.snapshot.source_id for item in ranked}, {"linkedin", "saramin", "jobkorea"})

    def test_capture_import_rejects_sensitive_posting_fields(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "capture.json"
            path.write_text(
                json.dumps(
                    {
                        "source_id": "linkedin",
                        "postings": [
                            {
                                "source_id": "linkedin",
                                "source_url": "https://www.linkedin.com/jobs/view/1",
                                "title": "Data Engineer",
                                "company": "Example",
                                "session_token": "secret",
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )

            imported = import_capture_files([path])

        self.assertEqual(imported.candidates, [])
        self.assertTrue(any("sensitive field" in error for error in imported.source_errors))

    def test_capture_import_rejects_credential_like_posting_text(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "capture.json"
            path.write_text(
                json.dumps(
                    {
                        "source_id": "linkedin",
                        "postings": [
                            {
                                "source_id": "linkedin",
                                "source_url": "https://www.linkedin.com/jobs/view/1",
                                "title": "Data Engineer",
                                "company": "Example",
                                "requirements": "Use Authorization: Bearer PRIVATE_CAPTURE_TOKEN",
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )

            imported = import_capture_files([path])

        self.assertEqual(imported.candidates, [])
        self.assertTrue(any("credential-like value" in error for error in imported.source_errors))

    def test_saramin_image_only_capture_is_marked_for_manual_ocr_review(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            path = tmp_path / "capture.json"
            path.write_text(
                json.dumps(
                    {
                        "source_id": "saramin",
                        "postings": [
                            {
                                "source_id": "saramin",
                                "source_url": "https://www.saramin.co.kr/zf_user/jobs/relay/view?rec_idx=1",
                                "source_posting_id": "1",
                                "title": "Image JD Engineer",
                                "company": "Image Co",
                                "location": "서울",
                                "skills": ["Python"],
                                "requirements": "",
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )
            imported = import_capture_files([path])
            raw = json.loads(CONFIG.read_text(encoding="utf-8"))
            raw["output_dir"] = str(tmp_path / "reports")
            config_path = tmp_path / "config.json"
            config_path.write_text(json.dumps(raw), encoding="utf-8")
            config = load_config(config_path)
            _summary, report, ranked = run_capture_import(
                config,
                date(2026, 6, 30),
                imported.candidates,
                imported.sources_attempted,
                imported.source_errors,
            )

        self.assertEqual(ranked[0].snapshot.manual_review_flags, ["본문 OCR 필요: 사람인 이미지형 JD 또는 DOM 텍스트 없음"])
        self.assertIn("본문 OCR 필요", report)
        self.assertIn("본문 이미지/OCR 필요 상태를 수동 검토했나요?", report)


if __name__ == "__main__":
    unittest.main()
