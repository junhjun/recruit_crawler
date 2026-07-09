from __future__ import annotations

import hashlib
import json
import sqlite3
from collections.abc import Mapping
from pathlib import Path

SCHEMA_VERSION = 1


def connect(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(path)
    connection.execute("PRAGMA foreign_keys = ON")
    connection.row_factory = sqlite3.Row
    initialize(connection)
    return connection


def initialize(connection: sqlite3.Connection) -> None:
    connection.executescript(
        """
        CREATE TABLE IF NOT EXISTS schema_metadata (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS runs (
            run_id TEXT PRIMARY KEY,
            command_mode TEXT NOT NULL,
            run_date TEXT NOT NULL,
            source_config_hash TEXT NOT NULL,
            profile_config_hash TEXT NOT NULL,
            status TEXT NOT NULL,
            context_status TEXT NOT NULL,
            report_generated INTEGER NOT NULL,
            report_path TEXT,
            candidates_collected INTEGER NOT NULL DEFAULT 0,
            ranked_count INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS source_attempts (
            run_id TEXT NOT NULL,
            source_id TEXT NOT NULL,
            attempted INTEGER NOT NULL,
            candidate_count INTEGER NOT NULL,
            error_count INTEGER NOT NULL,
            errors_json TEXT NOT NULL,
            PRIMARY KEY (run_id, source_id),
            FOREIGN KEY (run_id) REFERENCES runs(run_id) ON DELETE CASCADE
        );
        CREATE TABLE IF NOT EXISTS recommendations (
            recommendation_id TEXT PRIMARY KEY,
            run_id TEXT NOT NULL,
            source_id TEXT NOT NULL,
            source_url TEXT NOT NULL,
            source_posting_id TEXT,
            title TEXT NOT NULL,
            company TEXT NOT NULL,
            location TEXT NOT NULL,
            deadline TEXT,
            score INTEGER NOT NULL,
            recommendation TEXT NOT NULL,
            verdict TEXT NOT NULL,
            matched_evidence_json TEXT NOT NULL,
            gaps_json TEXT NOT NULL,
            risks_json TEXT NOT NULL,
            FOREIGN KEY (run_id) REFERENCES runs(run_id) ON DELETE CASCADE
        );
        CREATE TABLE IF NOT EXISTS quality_gates (
            run_id TEXT PRIMARY KEY,
            status TEXT NOT NULL,
            context_status TEXT NOT NULL,
            gate_json TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            FOREIGN KEY (run_id) REFERENCES runs(run_id) ON DELETE CASCADE
        );
        CREATE TABLE IF NOT EXISTS feedback_events (
            event_id TEXT PRIMARY KEY,
            recommendation_id TEXT NOT NULL,
            run_id TEXT NOT NULL,
            posting_key TEXT NOT NULL,
            source_id TEXT NOT NULL,
            source_posting_id TEXT,
            source_url TEXT NOT NULL,
            verdict TEXT NOT NULL,
            reason TEXT NOT NULL,
            movement TEXT NOT NULL,
            created_at TEXT NOT NULL,
            FOREIGN KEY (recommendation_id) REFERENCES recommendations(recommendation_id) ON DELETE CASCADE,
            FOREIGN KEY (run_id) REFERENCES runs(run_id) ON DELETE CASCADE
        );
        """
    )
    _ensure_feedback_columns(connection)
    connection.execute(
        "INSERT OR REPLACE INTO schema_metadata(key, value) VALUES (?, ?)",
        ("schema_version", str(SCHEMA_VERSION)),
    )
    connection.commit()


def stable_digest(value: Mapping[str, str | None]) -> str:
    payload = json.dumps(value, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _ensure_feedback_columns(connection: sqlite3.Connection) -> None:
    existing = {
        row["name"]
        for row in connection.execute("PRAGMA table_info(feedback_events)").fetchall()
    }
    migrations = {
        "posting_key": "ALTER TABLE feedback_events ADD COLUMN posting_key TEXT NOT NULL DEFAULT ''",
        "source_id": "ALTER TABLE feedback_events ADD COLUMN source_id TEXT NOT NULL DEFAULT ''",
        "source_posting_id": "ALTER TABLE feedback_events ADD COLUMN source_posting_id TEXT",
        "source_url": "ALTER TABLE feedback_events ADD COLUMN source_url TEXT NOT NULL DEFAULT ''",
    }
    for column, statement in migrations.items():
        if column not in existing:
            connection.execute(statement)
