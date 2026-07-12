from __future__ import annotations

import sys
import unittest
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from recruit_crawler.jd_parser import parse_candidate
from recruit_crawler.schemas import PostingCandidate


class JdParserTests(unittest.TestCase):
    def test_parse_candidate_discards_non_list_jd_sections(self) -> None:
        candidate = PostingCandidate(
            source_id="fixture",
            source_url="https://jobs.example.test/1",
            source_posting_id="1",
            title="ML Engineer",
            company="Example",
            location="Seoul",
            deadline_raw=None,
            collected_at=datetime(2026, 7, 12, tzinfo=timezone.utc),
            raw_jd={"required_qualifications": {"secret": "PRIVATE_RAW_JD_DO_NOT_PUBLISH"}},
        )

        snapshot = parse_candidate(candidate)

        self.assertEqual(snapshot.required_qualifications, [])
