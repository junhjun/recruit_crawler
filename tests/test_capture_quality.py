from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

import json
import tempfile
from datetime import date

from recruit_crawler.capture_import import build_capture_quality_gate, import_capture_files, select_capture_files


class CaptureQualityTests(unittest.TestCase):
    def test_capture_quality_gate_reports_privacy_and_import_categories(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            good = tmp_path / "good.json"
            bad = tmp_path / "bad.json"
            good.write_text(
                json.dumps(
                    {
                        "source_id": "saramin",
                        "captured_at": "2026-06-30T04:00:00Z",
                        "postings": [
                            {
                                "source_id": "saramin",
                                "source_url": "https://www.saramin.co.kr/zf_user/jobs/relay/view?rec_idx=2",
                                "source_posting_id": "2",
                                "title": "Public Contact Engineer",
                                "company": "Contact Co",
                                "location": "서울",
                                "skills": ["Python"],
                                "requirements": "자격요건 Python 문의 recruit@example.com",
                            },
                            {
                                "source_id": "saramin",
                                "source_url": "https://www.saramin.co.kr/zf_user/jobs/relay/view?rec_idx=3",
                                "source_posting_id": "3",
                                "title": "Image JD Engineer",
                                "company": "Image Co",
                                "location": "서울",
                                "skills": ["Python"],
                                "requirements": "",
                            },
                        ],
                    }
                ),
                encoding="utf-8",
            )
            bad.write_text("{not-json", encoding="utf-8")
            selection = select_capture_files(tmp_path, files=[good, bad], run_date=date(2026, 6, 30))
            imported = import_capture_files(selection.files)
            gate = build_capture_quality_gate(selection, imported)

        self.assertEqual(gate["status"], "fail")
        self.assertTrue(any(item["severity"] == "fail" and "invalid JSON" in item["message"] for item in gate["findings"]))
        self.assertTrue(any(item["category"] == "warning" and "public JD contact" in item["message"] for item in gate["privacy"]))
        self.assertEqual(gate["manual_review_items"][0]["flags"], ["본문 OCR 필요: 사람인 이미지형 JD 또는 DOM 텍스트 없음"])
        self.assertEqual(gate["source_mode_counts"], {"saramin": 2})

    def test_checked_in_chrome_capture_fixtures_cover_source_modes(self) -> None:
        fixture_paths = [
            ROOT / "fixtures" / "chrome_captures" / "linkedin_detail.json",
            ROOT / "fixtures" / "chrome_captures" / "saramin_image_only.json",
            ROOT / "fixtures" / "chrome_captures" / "jobkorea_detail.json",
        ]
        selection = select_capture_files(ROOT / "fixtures" / "chrome_captures", files=fixture_paths, run_date=date(2026, 6, 30))
        imported = import_capture_files(selection.files)
        gate = build_capture_quality_gate(selection, imported)

        self.assertEqual(gate["source_mode_counts"], {"jobkorea": 1, "linkedin": 1, "saramin": 1})
        self.assertEqual(gate["status"], "manual_review_required")
        self.assertTrue(gate["manual_review_items"])
        locations = {candidate.source_id: candidate.location for candidate in imported.candidates}
        self.assertEqual(locations["jobkorea"], "울산광역시 남구 옥현로 129")


if __name__ == "__main__":
    unittest.main()
