from __future__ import annotations

import json
import os
import sqlite3
import stat
from dataclasses import asdict, dataclass
from pathlib import Path

from .codex_thread_context import parse_model_context_json
from .model_context import ContextExtractionError, ModelContextExtraction

_CACHE_VERSION = 2


@dataclass(frozen=True)  # noqa: SLOTS_OK - project supports Python 3.9.
class SqliteContextExtractionCache:
    path: Path

    def get(self, fingerprint: str) -> ModelContextExtraction | None:
        if not self.path.exists():
            return None
        self._secure_existing_file()
        try:
            with sqlite3.connect(self.path, timeout=30) as connection:
                self._ensure_schema(connection)
                row = connection.execute(
                    "SELECT extraction_json FROM model_context_cache WHERE fingerprint = ? AND version = ?",
                    (fingerprint, _CACHE_VERSION),
                ).fetchone()
        except sqlite3.DatabaseError:
            raise ContextExtractionError("model context cache could not be read") from None
        if row is None:
            return None
        return parse_model_context_json(row[0], source_text="")

    def set(self, fingerprint: str, extraction: ModelContextExtraction) -> None:
        try:
            self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            self._reject_unsafe_path()
            flags = os.O_CREAT | os.O_WRONLY | getattr(os, "O_NOFOLLOW", 0)
            descriptor = os.open(self.path, flags, 0o600)
            os.close(descriptor)
            self._secure_existing_file()
            with sqlite3.connect(self.path, timeout=30) as connection:
                self._ensure_schema(connection)
                connection.execute(
                    """INSERT INTO model_context_cache (fingerprint, version, extraction_json)
                    VALUES (?, ?, ?)
                    ON CONFLICT(fingerprint) DO UPDATE SET
                        version = excluded.version,
                        extraction_json = excluded.extraction_json""",
                    (
                        fingerprint,
                        _CACHE_VERSION,
                        json.dumps(
                            asdict(extraction),
                            ensure_ascii=False,
                            sort_keys=True,
                            separators=(",", ":"),
                        ),
                    ),
                )
        except (OSError, sqlite3.DatabaseError):
            raise ContextExtractionError("model context cache could not be written") from None

    @staticmethod
    def _ensure_schema(connection: sqlite3.Connection) -> None:
        connection.execute(
            """CREATE TABLE IF NOT EXISTS model_context_cache (
                fingerprint TEXT PRIMARY KEY,
                version INTEGER NOT NULL,
                extraction_json TEXT NOT NULL
            )"""
        )

    def _secure_existing_file(self) -> None:
        try:
            self._reject_unsafe_path()
            self.path.chmod(0o600)
        except OSError:
            raise ContextExtractionError("model context cache could not be secured") from None

    def _reject_unsafe_path(self) -> None:
        try:
            mode = self.path.lstat().st_mode
        except FileNotFoundError:
            return
        if stat.S_ISLNK(mode) or not stat.S_ISREG(mode):
            raise ContextExtractionError("model context cache path is unsafe")
