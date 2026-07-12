from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from recruit_crawler._status_report_model import FeatureLedgerShapeError, feature_records
from recruit_crawler._storage_feedback import UnknownRecommendationError
from recruit_crawler.schemas import Thresholds
from recruit_crawler.scheduled import ScheduledRunRequest


class CoreModelAuditTests(unittest.TestCase):
    def test_value_models_use_slots_without_changing_constructor_contracts(self) -> None:
        # Given: representative core value models
        threshold = Thresholds()

        # When: inspecting their instance layout
        threshold_slots = threshold.__slots__

        # Then: the models do not allocate a per-instance attribute dictionary
        self.assertNotIn("__dict__", threshold_slots)
        self.assertIn("config", ScheduledRunRequest.__slots__)

    def test_feature_records_raise_typed_shape_error_for_invalid_ledger(self) -> None:
        # Given: a ledger boundary that was not parsed into a feature list
        ledger = {"status_date": "2026-07-12", "features": "invalid"}

        # When / Then: the shape error is domain-specific and preserves the diagnostic
        with self.assertRaisesRegex(FeatureLedgerShapeError, "load_feature_ledger"):
            feature_records(ledger)

    def test_unknown_recommendation_error_preserves_cli_diagnostic(self) -> None:
        # Given: an absent recommendation identifier
        error = UnknownRecommendationError("missing-recommendation")

        # When: the boundary renders its error
        diagnostic = str(error)

        # Then: callers retain the actionable identifier in the existing message contract
        self.assertEqual(diagnostic, "unknown recommendation_id: missing-recommendation")


if __name__ == "__main__":
    unittest.main()
