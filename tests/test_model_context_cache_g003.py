from __future__ import annotations

import sqlite3
import sys
import tempfile
import unittest
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from recruit_crawler.model_context import ModelContextExtraction
from recruit_crawler.model_context_cache import SqliteContextExtractionCache


@dataclass(slots=True)  # noqa: MUTABLE_OK - deterministic test clock advances between operations.
class MutableClock:
    timestamp: float

    def __call__(self) -> float:
        return self.timestamp


EXTRACTION = ModelContextExtraction(
    desired_roles=["ML Engineer"],
    skills=["Python"],
    preferred_locations=["Seoul"],
    max_experience_years=2,
)


class SqliteContextExtractionCacheG003Tests(unittest.TestCase):
    def test_get_does_not_slide_the_fixed_ttl(self) -> None:
        # Given: a cache entry with a deterministic 60-second lifetime.
        clock = MutableClock(timestamp=100.0)
        with tempfile.TemporaryDirectory() as tmp:
            cache = SqliteContextExtractionCache(
                Path(tmp) / "model-context-cache.sqlite3",
                ttl_seconds=60.0,
                clock=clock,
            )
            cache.set("fingerprint", EXTRACTION)

            # When: it is read before expiry, then read at its original expiry instant.
            clock.timestamp = 159.0
            before_expiry = cache.get("fingerprint")
            clock.timestamp = 160.0
            at_original_expiry = cache.get("fingerprint")

        # Then: the read did not extend the entry's original expiry.
        self.assertEqual(before_expiry, EXTRACTION)
        self.assertIsNone(at_original_expiry)

    def test_prune_expired_removes_only_expired_rows_including_boundary(self) -> None:
        # Given: entries expiring before, exactly at, and after the deterministic clock.
        clock = MutableClock(timestamp=100.0)
        with tempfile.TemporaryDirectory() as tmp:
            cache_path = Path(tmp) / "model-context-cache.sqlite3"
            cache = SqliteContextExtractionCache(cache_path, ttl_seconds=60.0, clock=clock)
            cache.set("expired", EXTRACTION)
            clock.timestamp = 160.0
            cache.set("boundary", EXTRACTION)
            clock.timestamp = 200.0
            cache.set("current", EXTRACTION)

            # When: cleanup runs at the exact expiry boundary of the middle entry.
            clock.timestamp = 220.0
            cache.prune_expired()
            with sqlite3.connect(cache_path) as connection:
                remaining = [
                    row[0]
                    for row in connection.execute(
                        "SELECT fingerprint FROM model_context_cache ORDER BY fingerprint"
                    )
                ]

        # Then: expired and boundary rows are removed, while a current row remains.
        self.assertEqual(remaining, ["current"])

    def test_prune_expired_removes_legacy_v1_row_without_timestamps(self) -> None:
        # Given: a database created by cache schema v1, whose row has no expiry metadata.
        clock = MutableClock(timestamp=100.0)
        with tempfile.TemporaryDirectory() as tmp:
            cache_path = Path(tmp) / "model-context-cache.sqlite3"
            with sqlite3.connect(cache_path) as connection:
                connection.execute(
                    """CREATE TABLE model_context_cache (
                        fingerprint TEXT PRIMARY KEY,
                        version INTEGER NOT NULL,
                        extraction_json TEXT NOT NULL
                    )"""
                )
                connection.execute(
                    """INSERT INTO model_context_cache (fingerprint, version, extraction_json)
                    VALUES (?, ?, ?)""",
                    ("legacy", 1, "{}"),
                )

            cache = SqliteContextExtractionCache(cache_path, ttl_seconds=60.0, clock=clock)

            # When: expiry cleanup migrates and evaluates the legacy row.
            cache.prune_expired()
            with sqlite3.connect(cache_path) as connection:
                remaining = connection.execute(
                    "SELECT fingerprint FROM model_context_cache"
                ).fetchall()

        # Then: a timestamp-less legacy row cannot survive indefinitely.
        self.assertEqual(remaining, [])
