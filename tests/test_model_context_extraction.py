from __future__ import annotations

import json
import sys
import tempfile
import unittest
from dataclasses import replace
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from recruit_crawler.config import apply_context_documents, load_config
from recruit_crawler.model_context import (
    ContextExtractionError,
    ModelContextExtraction,
    context_fingerprint,
)
from recruit_crawler.pipeline import build_live_run_preflight_gate, run_dry_run
from recruit_crawler.user_context import parse_context_document, parse_context_document_with_extractor

CONFIG = ROOT / "config" / "sample_config.json"


class FakeContextExtractor:
    def __init__(self, extraction: ModelContextExtraction) -> None:
        self.extraction = extraction
        self.calls: list[str] = []
        self.texts: list[str] = []

    def extract(self, text: str, *, fingerprint: str) -> ModelContextExtraction:
        self.texts.append(text)
        self.calls.append(fingerprint)
        return self.extraction


class MemoryContextCache:
    def __init__(self) -> None:
        self.values: dict[str, ModelContextExtraction] = {}

    def get(self, fingerprint: str) -> ModelContextExtraction | None:
        return self.values.get(fingerprint)

    def set(self, fingerprint: str, extraction: ModelContextExtraction) -> None:
        self.values[fingerprint] = extraction


class FailingContextExtractor:
    def extract(self, text: str, *, fingerprint: str) -> ModelContextExtraction:
        raise ContextExtractionError("model unavailable")


class ModelContextExtractionTests(unittest.TestCase):
    def test_deterministic_bad_experience_year_is_ignored(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "resume.md"
            path.write_text(
                "Skills: Python, ML\nLocations: Seoul\nExperience: 6002018 years\n",
                encoding="utf-8",
            )

            context = parse_context_document(path)

        self.assertEqual(context.max_experience_years, 0)
        self.assertIn("max_experience_years", context.missing_context)

    def test_deterministic_parser_splits_role_and_skill_slashes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "preferences.md"
            path.write_text(
                "Roles: ML/AI Engineer\nSkills: Python/Django; PyTorch\nLocations: 원격/하이브리드 무관\nExperience: 2 years\n",
                encoding="utf-8",
            )

            context = parse_context_document(path)

        self.assertEqual(context.desired_roles, ["ML", "AI Engineer"])
        self.assertEqual(context.skills, ["Python", "Django", "PyTorch"])
        self.assertEqual(context.preferred_locations, ["원격/하이브리드 무관"])

    def test_model_style_extraction_merges_structured_fields_and_ignores_bad_years(self) -> None:
        extraction = ModelContextExtraction(
            desired_roles=[
                "ML Engineer",
                "AI Engineer",
                "Computer Vision Engineer",
                "AI Research Engineer",
            ],
            skills=[
                "Python",
                "PyTorch",
                "Deep Learning",
                "Machine Learning",
                "Computer Vision",
                "CLIP",
                "YOLOv8",
                "Stable Diffusion",
                "Dataset construction",
            ],
            preferred_locations=["Seoul", "서울", "Remote", "원격/하이브리드 무관", "Suwon"],
            max_experience_years=2,
            explicit_deal_breakers=["unpaid internship"],
            confidence=0.86,
        )
        extractor = FakeContextExtractor(extraction)

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "resume.md"
            path.write_text(
                "Education 2018-2024\nExperience: 6002018 years\nDeepfake detection with PyTorch\n",
                encoding="utf-8",
            )

            context = parse_context_document_with_extractor(path, extractor)

        self.assertEqual(context.max_experience_years, 2)
        self.assertEqual(context.missing_context, [])
        self.assertIn("Computer Vision Engineer", context.desired_roles)
        self.assertIn("Deep Learning", context.skills)
        self.assertEqual(context.provenance["max_experience_years"], "model_context.schema")
        self.assertEqual(len(extractor.calls), 1)

    def test_model_extraction_cache_uses_fingerprint_without_raw_text_storage(self) -> None:
        extraction = ModelContextExtraction(
            desired_roles=["ML Engineer"],
            skills=["Python", "PyTorch"],
            preferred_locations=["Seoul"],
            max_experience_years=2,
        )
        extractor = FakeContextExtractor(extraction)
        cache = MemoryContextCache()
        text = "Roles: ML Engineer\nSkills: Python\nLocations: Seoul\nExperience: 2 years\n"

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "context.md"
            path.write_text(text, encoding="utf-8")

            first = parse_context_document_with_extractor(path, extractor, cache=cache)
            second = parse_context_document_with_extractor(path, extractor, cache=cache)

        self.assertEqual(first, second)
        expected_fingerprint = context_fingerprint(extractor.texts[0])
        self.assertEqual(extractor.calls, [expected_fingerprint])
        self.assertEqual(set(cache.values), {expected_fingerprint})
        self.assertNotIn("Roles: ML Engineer", json.dumps(cache.values, default=str))

    def test_untrusted_extractor_value_is_rejected_before_cache_write(self) -> None:
        extraction = ModelContextExtraction(
            desired_roles=["ML Engineer"],
            skills=["Jane Doe"],
            preferred_locations=["Seoul"],
            max_experience_years=2,
        )
        extractor = FakeContextExtractor(extraction)
        cache = MemoryContextCache()

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "context.md"
            path.write_text("Name: Jane Doe\nSkills: Python\n", encoding="utf-8")

            context = parse_context_document_with_extractor(path, extractor, cache=cache)

        self.assertEqual(cache.values, {})
        self.assertEqual(context.skills, ["Python"])

    def test_untrusted_extractor_non_list_field_falls_back_before_cache_write(self) -> None:
        extraction = ModelContextExtraction(
            desired_roles=["ML Engineer"],
            skills="private source text",
            preferred_locations=["Seoul"],
            max_experience_years=2,
        )
        extractor = FakeContextExtractor(extraction)
        cache = MemoryContextCache()

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "context.md"
            path.write_text("Skills: Python\n", encoding="utf-8")

            context = parse_context_document_with_extractor(path, extractor, cache=cache)

        self.assertEqual(cache.values, {})
        self.assertEqual(context.skills, ["Python"])

    def test_apply_context_documents_with_extractor_uses_single_aggregate_prompt_for_multiple_docs(self) -> None:
        extraction = ModelContextExtraction(
            desired_roles=["ML Engineer"],
            skills=["Python", "PyTorch"],
            preferred_locations=["Seoul"],
            max_experience_years=2,
        )
        extractor = FakeContextExtractor(extraction)
        cache = MemoryContextCache()

        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            preferences = tmp_path / "preferences.md"
            resume = tmp_path / "resume.md"
            preferences.write_text(
                "Roles: ML Engineer\nSkills: Python\nLocations: Seoul\nExperience: 2 years\n",
                encoding="utf-8",
            )
            resume.write_text(
                "Project: recommender system with PyTorch and ranking models\n",
                encoding="utf-8",
            )

            config = apply_context_documents(
                load_config(CONFIG),
                [preferences, resume],
                extractor=extractor,
                cache=cache,
            )

        self.assertEqual(config.user_context.desired_roles, ["ML Engineer"])
        self.assertEqual(len(extractor.calls), 1)
        self.assertEqual(len(extractor.texts), 1)
        aggregate_text = extractor.texts[0]
        self.assertIn("context_document_1", aggregate_text)
        self.assertIn("context_document_2", aggregate_text)
        self.assertIn("Roles: ML Engineer", aggregate_text)
        self.assertIn("recommender system with PyTorch", aggregate_text)
        self.assertNotIn(str(tmp_path), aggregate_text)
        self.assertEqual(extractor.calls, [context_fingerprint(aggregate_text)])
        self.assertEqual(set(cache.values), set(extractor.calls))
        self.assertNotIn("Roles: ML Engineer", json.dumps(cache.values, default=str))

    def test_model_extractor_failure_falls_back_to_deterministic_missing_context(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "context.md"
            path.write_text(
                "Skills: Python\nLocations: Seoul\nExperience: 6002018 years\n",
                encoding="utf-8",
            )

            context = parse_context_document_with_extractor(path, FailingContextExtractor())

        self.assertIn("Python", context.skills)
        self.assertEqual(context.max_experience_years, 0)
        self.assertEqual(set(context.missing_context), {"desired_roles", "max_experience_years"})

    def test_aggregate_model_failure_falls_back_to_deterministic_merge_and_preserves_slash_rules(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            preferences = tmp_path / "preferences.md"
            resume = tmp_path / "resume.md"
            preferences.write_text(
                "Roles: ML/AI Engineer\nSkills: Python/Django\nLocations: 원격/하이브리드 무관\n",
                encoding="utf-8",
            )
            resume.write_text(
                "Skills: PyTorch\nExperience: 6002018 years\n",
                encoding="utf-8",
            )

            config = apply_context_documents(
                load_config(CONFIG),
                [preferences, resume],
                extractor=FailingContextExtractor(),
            )

        self.assertEqual(config.user_context.desired_roles, ["ML", "AI Engineer"])
        self.assertEqual(config.user_context.skills, ["Python", "Django", "PyTorch"])
        self.assertEqual(config.user_context.preferred_locations, ["원격/하이브리드 무관"])
        self.assertEqual(config.user_context.max_experience_years, 0)
        self.assertEqual(config.user_context.missing_context, ["max_experience_years"])

    def test_context_fingerprint_changes_with_schema_model_or_effort(self) -> None:
        text = "Roles: ML Engineer\nSkills: Python\n"
        base = context_fingerprint(text)

        self.assertNotEqual(base, context_fingerprint(text, schema_version="model-context-v2"))
        self.assertNotEqual(base, context_fingerprint(text, model_id="gpt-5.5"))
        self.assertNotEqual(base, context_fingerprint(text, effort="medium"))

    def test_private_canary_document_rejects_before_extractor_call(self) -> None:
        extractor = FakeContextExtractor(
            ModelContextExtraction(
                desired_roles=["ML Engineer"],
                skills=["Python"],
                preferred_locations=["Seoul"],
                max_experience_years=2,
            )
        )
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "private.md"
            path.write_text("Skills: Python\nPRIVATE_PROFILE_CANARY", encoding="utf-8")

            with self.assertRaisesRegex(ValueError, "private canary"):
                parse_context_document_with_extractor(path, extractor)

        self.assertEqual(extractor.calls, [])

    def test_private_canary_in_any_aggregate_document_rejects_before_extractor_call(self) -> None:
        extractor = FakeContextExtractor(
            ModelContextExtraction(
                desired_roles=["ML Engineer"],
                skills=["Python"],
                preferred_locations=["Seoul"],
                max_experience_years=2,
            )
        )
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            clean = tmp_path / "clean.md"
            private = tmp_path / "private.md"
            clean.write_text("Roles: ML Engineer\nSkills: Python\n", encoding="utf-8")
            private.write_text("Locations: Seoul\nPRIVATE_PROFILE_CANARY", encoding="utf-8")

            with self.assertRaisesRegex(ValueError, "private canary"):
                apply_context_documents(load_config(CONFIG), [clean, private], extractor=extractor)

        self.assertEqual(extractor.calls, [])
        self.assertEqual(extractor.texts, [])

    def test_partial_context_still_fails_closed_for_scheduled_gate(self) -> None:
        config = load_config(CONFIG)
        partial = replace(
            config.user_context,
            desired_roles=["ML Engineer"],
            skills=[],
            preferred_locations=["Seoul"],
            max_experience_years=0,
        )
        gate = build_live_run_preflight_gate(date(2026, 7, 10), replace(config, user_context=partial))

        self.assertEqual(gate["status"], "fail")
        self.assertEqual(gate["context_status"], "needs_context")
        self.assertEqual(set(gate["missing_context"]), {"skills", "max_experience_years"})

    def test_ranking_prefers_ml_fixture_with_model_context(self) -> None:
        extraction = ModelContextExtraction(
            desired_roles=["ML Engineer", "AI Engineer", "Computer Vision Engineer"],
            skills=[
                "Python",
                "PyTorch",
                "Machine Learning",
                "Deep Learning",
                "Computer Vision",
                "LLM",
            ],
            preferred_locations=["Seoul", "Remote"],
            max_experience_years=2,
            explicit_deal_breakers=["unpaid internship"],
        )
        extractor = FakeContextExtractor(extraction)

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

            config = apply_context_documents(
                load_config(config_path),
                [context_doc],
                extractor=extractor,
            )
            _summary, _report, ranked = run_dry_run(config, date(2026, 7, 10))

        self.assertEqual(ranked[0].snapshot.title, "ML Engineer, Recommendation Systems")
        self.assertGreater(ranked[0].score, 50)
