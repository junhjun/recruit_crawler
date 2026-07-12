from __future__ import annotations

import json
import os
import sqlite3
import stat
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Callable

from .codex_thread_context import parse_model_context_json
from .model_context import ContextExtractionError, ModelContextExtraction

_CACHE_VERSION = 1
_DEFAULT_TTL_SECONDS = 24 * 60 * 60


@dataclass(frozen=True)  # noqa: SLOTS_OK - project supports Python 3.9.
class SqliteContextExtractionCache:
    path: Path
    ttl_seconds: float = _DEFAULT_TTL_SECONDS
    clock: Callable[[], float] = time.time

    def get(self, fingerprint: str) -> ModelContextExtraction | None:
        if not self.path.exists():
            return None
        self._secure_existing_file()
        try:
            with sqlite3.connect(self.path, timeout=30) as connection:
                self._ensure_schema(connection)
                row = connection.execute(
                    """SELECT extraction_json, expires_at FROM model_context_cache
                    WHERE fingerprint = ? AND version = ?""",
                    (fingerprint, _CACHE_VERSION),
                ).fetchone()
                if row is not None and (row[1] is None or row[1] <= self.clock()):
                    connection.execute(
                        "DELETE FROM model_context_cache WHERE fingerprint = ?",
                        (fingerprint,),
                    )
                    return None
        except sqlite3.DatabaseError:
            raise ContextExtractionError("model context cache could not be read") from None
        if row is None:
            return None
        return parse_model_context_json(row[0], source_text="")

    def set(self, fingerprint: str, extraction: ModelContextExtraction) -> None:
        try:
            self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            descriptor = os.open(
                self.path,
                os.O_CREAT | os.O_WRONLY | getattr(os, "O_NOFOLLOW", 0),
                0o600,
            )
            try:
                if not stat.S_ISREG(os.fstat(descriptor).st_mode):
                    raise OSError("model context cache is not a regular file")
            finally:
                os.close(descriptor)
            self.path.chmod(0o600)
            created_at = self.clock()
            expires_at = created_at + self.ttl_seconds
            with sqlite3.connect(self.path, timeout=30) as connection:
                self._ensure_schema(connection)
                connection.execute(
                    """INSERT INTO model_context_cache (
                        fingerprint, version, extraction_json, created_at, expires_at
                    ) VALUES (?, ?, ?, ?, ?)
                    ON CONFLICT(fingerprint) DO UPDATE SET
                        version = excluded.version,
                        extraction_json = excluded.extraction_json,
                        created_at = excluded.created_at,
                        expires_at = excluded.expires_at""",
                    (
                        fingerprint,
                        _CACHE_VERSION,
                        json.dumps(
                            asdict(extraction),
                            ensure_ascii=False,
                            sort_keys=True,
                            separators=(",", ":"),
                        ),
                        created_at,
                        expires_at,
                    ),
                )
        except (OSError, sqlite3.DatabaseError):
            raise ContextExtractionError("model context cache could not be written") from None

    def prune_expired(self) -> int:
        if not self.path.exists():
            return 0
        self._secure_existing_file()
        try:
            with sqlite3.connect(self.path, timeout=30) as connection:
                self._ensure_schema(connection)
                deleted = connection.execute(
                    """DELETE FROM model_context_cache
                    WHERE expires_at IS NULL OR expires_at <= ?""",
                    (self.clock(),),
                ).rowcount
        except sqlite3.DatabaseError:
            raise ContextExtractionError("model context cache could not be pruned") from None
        return deleted

    @staticmethod
    def _ensure_schema(connection: sqlite3.Connection) -> None:
        connection.execute(
            """CREATE TABLE IF NOT EXISTS model_context_cache (
                fingerprint TEXT PRIMARY KEY,
                version INTEGER NOT NULL,
                extraction_json TEXT NOT NULL,
                created_at REAL NOT NULL,
                expires_at REAL NOT NULL
            )"""
        )
        columns = {
            row[1]
            for row in connection.execute("PRAGMA table_info(model_context_cache)")
        }
        if "created_at" not in columns:
            connection.execute("ALTER TABLE model_context_cache ADD COLUMN created_at REAL")
        if "expires_at" not in columns:
            connection.execute("ALTER TABLE model_context_cache ADD COLUMN expires_at REAL")

    def _secure_existing_file(self) -> None:
        try:
            if not stat.S_ISREG(os.lstat(self.path).st_mode):
                raise OSError("model context cache is not a regular file")
            self.path.chmod(0o600)
        except OSError:
            raise ContextExtractionError("model context cache could not be secured") from None
