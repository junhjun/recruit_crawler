from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Dict, List, TypedDict, Union

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from recruit_crawler.config import ConfigError, load_config

EXPECTED_SOURCE_IDS = {
    "company_careers",
    "saramin",
    "jobkorea",
    "wanted",
    "jumpit",
    "rallit",
    "rocketpunch",
    "linkedin",
    "naver_careers",
    "kakao_careers",
    "line_careers",
    "coupang_careers",
}
ENABLED_SOURCE_IDS = {"jobkorea", "jumpit", "rallit", "rocketpunch", "saramin", "wanted"}
EXCLUDED_SOURCE_IDS = EXPECTED_SOURCE_IDS - ENABLED_SOURCE_IDS
JsonScalar = Union[str, int, float, bool, None]
JsonValue = Union[JsonScalar, List["JsonValue"], Dict[str, "JsonValue"]]


class RawSource(TypedDict, total=False):
    source_id: str
    enabled: bool
    target_lane: str | None
    test_refs: List[str]
    options: Dict[str, JsonValue]


class RawConfig(TypedDict):
    sources: List[RawSource]


class SourceRegistryTests(unittest.TestCase):
    def _live_config(self) -> RawConfig:
        return json.loads((ROOT / "config" / "live_sources.sample.json").read_text(encoding="utf-8"))

    def _write_temp_config(self, raw: RawConfig, tmp_path: Path) -> Path:
        config_path = tmp_path / "live_sources.json"
        config_path.write_text(json.dumps(raw), encoding="utf-8")
        return config_path

    def test_source_registry_loads_expected_statuses_and_lanes(self) -> None:
        config = load_config(ROOT / "config" / "live_sources.sample.json", allow_real_sources=True)
        by_id = {source.source_id: source for source in config.sources}

        self.assertEqual(set(by_id), EXPECTED_SOURCE_IDS)
        self.assertEqual(by_id["jumpit"].target_lane, "public_http")
        self.assertEqual(by_id["jumpit"].target_status, "enabled")
        self.assertEqual(by_id["rallit"].target_lane, "public_http")
        self.assertEqual(by_id["rallit"].target_status, "enabled")
        self.assertEqual(by_id["jobkorea"].target_status, "enabled")
        self.assertEqual(by_id["jobkorea"].target_lane, "public_http")
        self.assertEqual(by_id["saramin"].target_status, "enabled")
        self.assertEqual(by_id["saramin"].target_lane, "public_http")
        self.assertEqual(by_id["wanted"].target_status, "enabled")
        self.assertEqual(by_id["wanted"].target_lane, "public_http")
        self.assertEqual(by_id["rocketpunch"].target_status, "enabled")
        self.assertEqual(by_id["rocketpunch"].target_lane, "browser_automation")
        self.assertEqual(by_id["linkedin"].target_status, "excluded")
        for source_id in EXCLUDED_SOURCE_IDS:
            self.assertEqual(by_id[source_id].target_status, "excluded")
            self.assertIsNone(by_id[source_id].target_lane)

    def test_source_registry_rejects_empty_target_lane(self) -> None:
        raw = self._live_config()
        raw["sources"][0]["target_lane"] = ""
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaisesRegex(ConfigError, "target_lane cannot be empty string"):
                load_config(self._write_temp_config(raw, Path(tmp)), allow_real_sources=True)

    def test_enabled_registry_sources_require_code_tests_and_docs_refs(self) -> None:
        raw = self._live_config()
        for source in raw["sources"]:
            if source["source_id"] == "jumpit":
                source["test_refs"] = []
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaisesRegex(ConfigError, "requires test_refs"):
                load_config(self._write_temp_config(raw, Path(tmp)), allow_real_sources=True)

    def test_registry_docs_refs_exist_for_every_source(self) -> None:
        config = load_config(ROOT / "config" / "live_sources.sample.json", allow_real_sources=True)

        for source in config.sources:
            for docs_ref in source.docs_refs:
                self.assertTrue((ROOT / docs_ref).exists(), f"{source.source_id}: {docs_ref}")

    def test_registry_code_test_and_docs_refs_align_with_focused_modules(self) -> None:
        config = load_config(ROOT / "config" / "live_sources.sample.json", allow_real_sources=True)

        for source in config.sources:
            if source.adapter_code_path:
                adapter_path, _, adapter_symbol = source.adapter_code_path.partition("::")
                adapter_file = ROOT / adapter_path
                self.assertTrue(adapter_file.exists(), f"{source.source_id}: {source.adapter_code_path}")
                if adapter_symbol:
                    self.assertIn(adapter_symbol, adapter_file.read_text(encoding="utf-8"), source.source_id)
            for docs_ref in source.docs_refs:
                self.assertTrue((ROOT / docs_ref).exists(), f"{source.source_id}: {docs_ref}")
            for test_ref in source.test_refs:
                test_path, _, test_name = test_ref.partition("::")
                test_file = ROOT / test_path
                self.assertTrue(test_path.startswith("tests/test_"), f"{source.source_id}: {test_ref}")
                self.assertNotIn(test_path, {"tests/test_dry_run.py", "tests/test_repository_harness.py"}, test_ref)
                self.assertTrue(test_name.startswith("test_"), f"{source.source_id}: {test_ref}")
                self.assertTrue(test_file.exists(), f"{source.source_id}: {test_ref}")
                test_text = test_file.read_text(encoding="utf-8")
                self.assertIn(f"def {test_name}(", test_text, f"{source.source_id}: {test_ref}")
            if source.target_status == "enabled":
                self.assertTrue(source.adapter_code_path, source.source_id)
                self.assertTrue(source.test_refs, source.source_id)
                self.assertTrue(source.docs_refs, source.source_id)
