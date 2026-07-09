from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

import io
import json
import tempfile
from contextlib import redirect_stdout

from recruit_crawler.browser_evidence import _redact, build_browser_evidence
from recruit_crawler.cli import main as cli_main
from recruit_crawler.config import load_config


class ChromeExtensionBoundaryTests(unittest.TestCase):
    def test_static_content_script_load_is_passive(self) -> None:
        content = (ROOT / "browser_extension" / "content.js").read_text(encoding="utf-8")

        self.assertIn('const CAPTURE_COMMAND = "recruit-capture:capture-visible-postings";', content)
        self.assertIn("registerCaptureCommandHandler();", content)
        self.assertIn("injectCaptureButton();", content)
        self.assertNotIn("return captureVisiblePostings();", content)
        self.assertLess(content.rfind("registerCaptureCommandHandler();"), content.rfind("injectCaptureButton();"))
        self.assertNotIn("window.addEventListener(\"message\"", content)
        self.assertNotIn("postMessage({", content)

    def test_popup_uses_explicit_capture_command(self) -> None:
        popup = (ROOT / "browser_extension" / "popup.js").read_text(encoding="utf-8")

        self.assertIn('"recruit-capture:capture-visible-postings"', popup)
        self.assertIn("chrome.tabs.sendMessage", popup)
        self.assertIn("frameId: 0", popup)
        self.assertNotIn('type: "recruit-capture:download"', popup)
        self.assertNotIn("const [{ result }]", popup)

    def test_capture_payload_includes_diagnostics_and_download_proof(self) -> None:
        content = (ROOT / "browser_extension" / "content.js").read_text(encoding="utf-8")
        background = (ROOT / "browser_extension" / "background.js").read_text(encoding="utf-8")

        self.assertIn('const EXTENSION_VERSION = "0.1.0";', content)
        self.assertIn("function withCaptureDiagnostics", content)
        self.assertIn("extension_version: EXTENSION_VERSION", content)
        self.assertIn("detail_length: requirements.length", content)
        self.assertIn("marker_hit: sourceDetailMarkerPattern().test(requirements)", content)
        self.assertIn('extraction_strategy: "linkedin_visible_detail_clickthrough"', content)
        self.assertIn("clickthrough:", content)
        self.assertIn("iframe_status: \"same_origin_dom_only\"", content)
        self.assertIn("filename", background)
        self.assertIn("sendResponse({ ok: true, ...download })", background)

class BrowserEvidenceCliTests(unittest.TestCase):
    def test_browser_evidence_fixture_writes_allowed_fields_without_dom_leakage(self) -> None:
        output = io.StringIO()
        with tempfile.TemporaryDirectory() as tmp, redirect_stdout(output):
            exit_code = cli_main(
                [
                    "browser-evidence",
                    "--config",
                    str(ROOT / "config" / "live_sources.sample.json"),
                    "--source-id",
                    "rocketpunch",
                    "--fixture-html",
                    str(ROOT / "fixtures" / "browser_evidence" / "rocketpunch_listing.html"),
                    "--output",
                    str(Path(tmp) / "rocketpunch_fixture.json"),
                ]
            )
            transcript = json.loads((Path(tmp) / "rocketpunch_fixture.json").read_text(encoding="utf-8"))

        self.assertEqual(exit_code, 0)
        self.assertEqual(transcript["schema_version"], 1)
        self.assertEqual(transcript["command_mode"], "fixture")
        self.assertIn("dom_sha256", transcript)
        self.assertNotIn("dom", transcript)
        self.assertEqual(transcript["privacy_findings"], [])
        self.assertTrue(transcript["filterability"]["stable_posting_url"])

    def test_browser_evidence_private_target_fails(self) -> None:
        config = load_config(ROOT / "config" / "live_sources.sample.json", allow_real_sources=True)
        rocketpunch = next(source for source in config.sources if source.source_id == "rocketpunch")

        transcript = build_browser_evidence(rocketpunch, target_url="https://www.rocketpunch.com/private?session=secret")

        self.assertEqual(transcript["exit_code"], 1)
        self.assertTrue(transcript["errors"])

    def test_browser_evidence_redacts_private_markers_case_insensitively(self) -> None:
        self.assertEqual(
            _redact("https://jobs.example.test/apply?Session=abc&ACCESS_TOKEN=def"),
            "https://jobs.example.test/apply?[REDACTED]abc&[REDACTED]def",
        )


if __name__ == "__main__":
    unittest.main()
