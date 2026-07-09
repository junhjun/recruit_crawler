from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from recruit_crawler.config import ConfigError, load_config
from recruit_crawler.relevance import (
    evaluate_relevance_cases,
    feedback_events_from_records,
    load_relevance_cases,
    posting_key_for_snapshot,
)
from recruit_crawler.scorer import score_snapshot
from recruit_crawler.schemas import JDSnapshot

CONFIG = ROOT / "config" / "sample_config.json"


class ScoringConfigTests(unittest.TestCase):

    def test_scoring_weights_must_be_non_negative_and_useful(self) -> None:
        raw = json.loads(CONFIG.read_text(encoding="utf-8"))
        with tempfile.TemporaryDirectory() as tmp:
            config_path = Path(tmp) / "config.json"
            raw["scoring_weights"]["required"] = -1
            config_path.write_text(json.dumps(raw), encoding="utf-8")
            with self.assertRaisesRegex(ConfigError, "scoring_weights values must be non-negative"):
                load_config(config_path)

            raw["scoring_weights"] = {
                "required": 0,
                "preferred": 0,
                "responsibilities": 0,
                "company": 0,
                "location": 0,
            }
            config_path.write_text(json.dumps(raw), encoding="utf-8")
            with self.assertRaisesRegex(ConfigError, "scoring_weights must include at least one positive value"):
                load_config(config_path)

    def test_config_ranking_weights_affect_scores_deterministically(self) -> None:
        raw = json.loads(CONFIG.read_text(encoding="utf-8"))
        raw["scoring_weights"] = {
            "required": 100,
            "preferred": 0,
            "responsibilities": 0,
            "company": 0,
            "location": 0,
        }
        snapshot = JDSnapshot(
            source_id="fixture",
            source_url="https://jobs.example.test/weighted",
            source_posting_id="weighted",
            title="ML Engineer",
            company="Example",
            location="Busan",
            deadline_raw=None,
            deadline=None,
            deadline_uncertain=False,
            required_qualifications=["Python"],
            preferred_qualifications=[],
            responsibilities=[],
            company_info=[],
        )
        with tempfile.TemporaryDirectory() as tmp:
            config_path = Path(tmp) / "config.json"
            config_path.write_text(json.dumps(raw), encoding="utf-8")
            required_weight_config = load_config(config_path)
            raw["scoring_weights"] = {
                "required": 0,
                "preferred": 0,
                "responsibilities": 0,
                "company": 0,
                "location": 100,
            }
            config_path.write_text(json.dumps(raw), encoding="utf-8")
            location_weight_config = load_config(config_path)

        self.assertEqual(score_snapshot(snapshot, required_weight_config).score, 100)
        self.assertEqual(score_snapshot(snapshot, location_weight_config).score, 5)

    def test_preferred_role_titles_affect_scores_deterministically(self) -> None:
        config = load_config(CONFIG)
        matching = JDSnapshot(
            source_id="fixture",
            source_url="https://jobs.example.test/ml",
            source_posting_id="ml",
            title="ML Engineer",
            company="Example",
            location="Seoul",
            deadline_raw=None,
            deadline=None,
            deadline_uncertain=False,
            required_qualifications=["Python"],
            preferred_qualifications=[],
            responsibilities=[],
            company_info=[],
        )
        non_matching = JDSnapshot(
            source_id="fixture",
            source_url="https://jobs.example.test/frontend",
            source_posting_id="frontend",
            title="Frontend Engineer",
            company="Example",
            location="Seoul",
            deadline_raw=None,
            deadline=None,
            deadline_uncertain=False,
            required_qualifications=["Python"],
            preferred_qualifications=[],
            responsibilities=[],
            company_info=[],
        )

        self.assertGreater(score_snapshot(matching, config).score, score_snapshot(non_matching, config).score)

class RelevanceCaseEvaluationTests(unittest.TestCase):
    def test_thirty_seed_relevance_cases_evaluate_deterministically(self) -> None:
        config = load_config(CONFIG)
        cases = load_relevance_cases(ROOT / "fixtures" / "relevance_cases.json", config)

        failures = evaluate_relevance_cases(cases, config)

        self.assertEqual(len(cases), 30)
        self.assertEqual(failures, [])

    def test_feedback_history_can_drive_deterministic_relevance_movement(self) -> None:
        config = load_config(CONFIG)
        cases = load_relevance_cases(ROOT / "fixtures" / "relevance_cases.json", config)
        feedback_events = feedback_events_from_records(
            [
                {
                    "posting_key": posting_key_for_snapshot(cases[0].snapshot),
                    "verdict": "not_relevant",
                    "reason": "Daily feedback marked this as noise",
                    "movement": "down",
                    "created_at": "2026-07-02T00:00:00+00:00",
                }
            ]
        )

        failures = evaluate_relevance_cases([cases[0]], config, feedback_events)

        self.assertEqual(len(failures), 1)
        self.assertIn("expected movement up, got down", failures[0])
