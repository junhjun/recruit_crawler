from __future__ import annotations

import json
import sys
import tempfile
import unittest
from dataclasses import asdict
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from recruit_crawler.capture_import import import_capture_files, select_capture_files
from recruit_crawler.config import load_config
from recruit_crawler.gate import build_gate_v2
from recruit_crawler.pipeline import run_capture_import
from recruit_crawler.projection import project_pipeline_result
from recruit_crawler.report_writer import persist_rendered_report
from recruit_crawler.schemas import PipelineResultV2
from recruit_crawler.summarizer import render_report_v2

CONFIG = ROOT / "config" / "sample_config.json"


def _materialize(result: PipelineResultV2, config, slug: str = "capture-import"):
    projection = project_pipeline_result(result)
    rendered = render_report_v2(result)
    publication = persist_rendered_report(
        config.output_dir,
        result.run_date,
        rendered,
        report_slug=slug,
    )
    artifact = publication.artifact
    report = artifact.rendered.markdown_bytes.decode("utf-8") if artifact.rendered is not None else ""
    gate = build_gate_v2(
        result,
        enabled_source_ids=(source.source_id for source in config.sources if source.enabled),
        projection=projection,
        report_artifact=artifact,
    )
    return projection, report, publication, gate


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
            result = run_capture_import(
                config,
                selection.run_date,
                imported.candidates,
                imported.sources_attempted,
                imported.source_errors,
            )
            projection, report, publication, gate = _materialize(result, config, "mixed-sources")

        self.assertIsInstance(result, PipelineResultV2)
        self.assertEqual(result.collected_count, 3)
        self.assertEqual(result.duplicates_removed, 0)
        self.assertEqual(len(projection["assessments"]), 3)
        self.assertEqual(imported.sources_attempted, ["jobkorea", "linkedin", "saramin"])
        self.assertTrue(any("invalid JSON" in error for error in imported.source_errors))
        self.assertTrue(any("empty postings" in error for error in imported.source_errors))
        self.assertTrue(any("duplicate posting jobkorea:49476607" in error for error in imported.source_errors))
        self.assertIsNone(publication.failure_code)
        self.assertEqual(publication.durability, "published")
        self.assertTrue(publication.artifact.generated)
        self.assertTrue(gate["report"]["queue_parity"])
        self.assertIn("LinkedIn Data Engineer", report)
        public_titles = {item["title"] for item in projection["assessments"]}
        self.assertIn("AI 엔지니어 채용", public_titles)
        self.assertIn("[울산]Forward Deployed 엔지니어", public_titles)
        self.assertTrue(
            any(item["location"] == "울산광역시 남구 옥현로 129" for item in projection["assessments"])
        )
        self.assertTrue(any(item["deadline"] == "2026-07-28" for item in projection["assessments"]))
        self.assertIn("AI 엔지니어 채용", report)
        self.assertNotIn("울산 남구 마감일", report)
        self.assertNotIn("SHOULD_NOT_LEAK", report)
        self.assertNotIn("필수 요건 일치", report)
        self.assertNotIn("FastAPI", report)
        self.assertEqual({item["source_id"] for item in projection["assessments"]}, {"linkedin", "saramin", "jobkorea"})
        public_payload = json.dumps({"projection": projection, "artifact": asdict(publication.artifact), "gate": gate}, default=str, ensure_ascii=False)
        self.assertNotIn("SHOULD_NOT_LEAK", public_payload)

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
            result = run_capture_import(
                config,
                date(2026, 6, 30),
                imported.candidates,
                imported.sources_attempted,
                imported.source_errors,
            )
            projection, report, publication, gate = _materialize(result, config, "image-only")

        self.assertEqual(len(projection["manual_queue"]), 1)
        manual_item = projection["manual_queue"][0]
        self.assertEqual(
            set(manual_item),
            {
                "recommendation_id",
                "posting_key",
                "source_id",
                "source_url",
                "source_posting_id",
                "title",
                "company",
                "location",
                "deadline",
                "score",
                "final_disposition",
                "reason_codes",
                "source_detail_quality",
                "matched_evidence",
            },
        )
        self.assertEqual(set(manual_item["reason_codes"]), {"manual_flag"})
        self.assertEqual(manual_item["final_disposition"], "manual_review")
        self.assertNotIn("manual_review_flags", manual_item)
        public_payload = json.dumps(
            {"projection": projection, "gate": gate},
            default=str,
            ensure_ascii=False,
        ) + report
        self.assertNotIn("OCR", public_payload)
        self.assertNotIn("manual_review_flags", public_payload)
        self.assertNotIn("본문 OCR 필요", public_payload)
        self.assertTrue(gate["report"]["queue_parity"])
        self.assertIsNone(publication.failure_code)
        self.assertEqual(publication.durability, "published")
        self.assertTrue(publication.artifact.generated)


if __name__ == "__main__":
    unittest.main()
