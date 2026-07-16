from __future__ import annotations

import json
import sys
import tempfile
import unittest
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from recruit_crawler.config import apply_context_documents, load_config
from recruit_crawler.model_context import ModelContextExtraction
from recruit_crawler.pipeline import run_dry_run
from recruit_crawler.projection import project_pipeline_result

CONFIG = ROOT / "config" / "sample_config.json"


class RankingContextExtractor:
    def __init__(self, extraction: ModelContextExtraction) -> None:
        self.extraction = extraction

    def extract(self, text: str, *, fingerprint: str) -> ModelContextExtraction:
        del text, fingerprint
        return self.extraction


class ModelContextRankingTests(unittest.TestCase):
    def test_ranking_prefers_ml_fixture_with_model_context(self) -> None:
        # Given: a complete model extraction and the deterministic postings fixture.
        extraction = ModelContextExtraction(
            desired_roles=["ML Engineer", "AI Engineer", "Computer Vision Engineer"],
            skills=["Python", "PyTorch", "Machine Learning", "Deep Learning", "Computer Vision", "LLM"],
            preferred_locations=["Seoul", "Remote"],
            max_experience_years=2,
            explicit_deal_breakers=["unpaid internship"],
        )

        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            context_doc = tmp_path / "resume.md"
            context_doc.write_text(
                "Resume text with noisy year 6002018 years and model projects.\n",
                encoding="utf-8",
            )
            raw = json.loads(CONFIG.read_text(encoding="utf-8"))
            raw["output_dir"] = str(tmp_path / "reports")
            raw["fixture_path"] = str(ROOT / "fixtures" / "postings.json")
            config_path = tmp_path / "config.json"
            config_path.write_text(json.dumps(raw), encoding="utf-8")

            # When: the model-derived context drives the fixture dry run.
            config = apply_context_documents(
                load_config(config_path),
                [context_doc],
                extractor=RankingContextExtractor(extraction),
            )
            result = run_dry_run(config, date(2026, 7, 10))
            ranked = project_pipeline_result(result)["action_queue"]

        # Then: the most relevant ML opportunity ranks first.
        self.assertEqual(ranked[0]["title"], "ML Engineer, Recommendation Systems")
        self.assertGreater(ranked[0]["score"], 50)
