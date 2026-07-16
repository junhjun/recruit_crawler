from __future__ import annotations

from datetime import date, datetime, timezone
import hashlib
import json
import sqlite3
from collections.abc import Mapping
from pathlib import Path
from typing import Any
from .schemas import (
    PUBLIC_SOURCE_IDS_V1,
    SOURCE_EXECUTION_OUTCOME_ERROR_CODES_V1,
    SOURCE_EXECUTION_OUTCOME_STATUSES_V1,
)
from typing import Iterable


SCHEMA_VERSION = 4
SCHEMA_SIGNATURE = "storage-v4"

_TABLES = {"schema_metadata", "runs", "source_attempts", "recommendations", "quality_gates", "feedback_events", "source_outcomes"}
_V3_TABLES = _TABLES - {"source_outcomes"}
_OUTCOME_COLUMNS = {
    "run_id", "source_id", "attempted", "completed", "status", "error_code",
    "duration_ms", "outcome_schema_version",
}
_GUARD_TRIGGERS = {
    "schema_metadata_no_downgrade_insert",
    "schema_metadata_no_downgrade_update",
    "schema_metadata_no_downgrade_delete",
}
_INDEXES = {"idx_recommendations_run", "idx_feedback_recommendation"}


_RUN_COLUMNS = {
    "run_id", "command_mode", "run_date", "source_config_hash", "profile_config_hash",
    "status", "context_status", "report_generated", "report_path", "candidates_collected",
    "ranked_count", "created_at", "updated_at", "record_schema_version",
    "pipeline_schema_version", "score_schema_version", "disposition_schema_version",
}
_ATTEMPT_COLUMNS = {
    "run_id", "source_id", "attempted", "candidate_count", "error_count", "errors_json",
    "accepted_count", "rejected_count", "duplicate_count", "normalized_changed_field_count",
    "normalized_emptied_field_count", "detail_json", "error_codes_json", "duration_ms",
}
_REC_COLUMNS = {
    "recommendation_id", "run_id", "source_id", "source_url", "source_posting_id", "title",
    "company", "location", "deadline", "score", "recommendation", "verdict",
    "matched_evidence_json", "gaps_json", "risks_json", "posting_key", "final_disposition",
    "reason_codes_json", "source_detail_quality", "record_schema_version",
}
_GATE_COLUMNS = {"run_id", "status", "context_status", "gate_json", "updated_at"}
_FEEDBACK_COLUMNS = {
    "event_id", "recommendation_id", "run_id", "posting_key", "source_id", "source_posting_id",
    "source_url", "verdict", "reason", "movement", "created_at", "record_schema_version",
}
_LEGACY_RUN_COLUMNS = _RUN_COLUMNS - {
    "record_schema_version", "pipeline_schema_version", "score_schema_version", "disposition_schema_version"
}
_LEGACY_ATTEMPT_COLUMNS = {"run_id", "source_id", "attempted", "candidate_count", "error_count", "errors_json"}
_LEGACY_REC_COLUMNS = {
    "recommendation_id", "run_id", "source_id", "source_url", "source_posting_id", "title",
    "company", "location", "deadline", "score", "recommendation", "verdict",
    "matched_evidence_json", "gaps_json", "risks_json",
}
_LEGACY_FEEDBACK_COLUMNS = {"event_id", "recommendation_id", "run_id", "verdict", "reason", "movement", "created_at"}
_CURRENT_FEEDBACK_COLUMNS = _LEGACY_FEEDBACK_COLUMNS | {
    "posting_key", "source_id", "source_posting_id", "source_url"
}
_PROBE_IDENTITY_COLUMNS = (
    "run_id", "command_mode", "run_date", "source_config_hash", "profile_config_hash",
)
_PROBE_VERSION_COLUMNS = (
    "record_schema_version", "pipeline_schema_version", "score_schema_version",
    "disposition_schema_version",
)


class StorageSchemaError(ValueError):
    """The database is not a supported storage schema and was not changed."""


def probe_run_transaction_state(
    connection: sqlite3.Connection,
    run_id: str,
    token: str | None = None,
    *,
    expected_identity: Mapping[str, Any] | None = None,
    expected_versions: Mapping[str, Any] | None = None,
    expected_gate_json_sha256: str | None = None,
    expected_content_sha256: str | None = None,
    expected_token: str | None = None,
) -> str:
    if type(run_id) is not str or not run_id:
        return "indeterminate"

    def expected_matches(row: tuple[Any, ...]) -> bool:
        if expected_identity is None or expected_versions is None:
            return False
        if set(expected_identity) != set(_PROBE_IDENTITY_COLUMNS):
            return False
        if set(expected_versions) != set(_PROBE_VERSION_COLUMNS):
            return False
        values = dict(zip(_PROBE_IDENTITY_COLUMNS + _PROBE_VERSION_COLUMNS, row))
        if any(expected_identity[key] != values[key] for key in _PROBE_IDENTITY_COLUMNS):
            return False
        if any(expected_versions[key] != values[key] for key in _PROBE_VERSION_COLUMNS):
            return False
        gate = connection.execute(
            "SELECT gate_json FROM quality_gates WHERE run_id=?", (run_id,)
        ).fetchone()
        if gate is None:
            return False
        try:
            parsed_gate = json.loads(gate[0])
        except (TypeError, ValueError):
            return False
        if not isinstance(parsed_gate, dict):
            return False
        if expected_gate_json_sha256 is not None:
            if parsed_gate.get("gate_json_sha256") != expected_gate_json_sha256:
                return False
        if expected_content_sha256 is not None:
            projection = parsed_gate.get("gate_projection")
            report = projection.get("report") if isinstance(projection, dict) else None
            if not isinstance(report, dict) or report.get("content_sha256") != expected_content_sha256:
                return False
        return True

    def marker_token(gate_json: Any) -> str | None:
        if type(gate_json) is not str:
            return None
        try:
            parsed_gate = json.loads(gate_json)
            canonical_gate = json.dumps(
                parsed_gate, ensure_ascii=False, separators=(",", ":"), sort_keys=False
            )
        except (TypeError, ValueError):
            return None
        return hashlib.sha256(
            (run_id + "\0" + canonical_gate).encode("utf-8")
        ).hexdigest()

    try:
        run = connection.execute(
            "SELECT run_id, command_mode, run_date, source_config_hash, profile_config_hash, "
            "record_schema_version, pipeline_schema_version, score_schema_version, disposition_schema_version "
            "FROM runs WHERE run_id=?",
            (run_id,),
        ).fetchone()
        if run is None:
            marker = connection.execute(
                "SELECT value FROM schema_metadata WHERE key=?",
                (f"scheduled_run_pending:{run_id}",),
            ).fetchone()
            return "indeterminate" if marker is not None else "absent"
        has_expectations = any(
            value is not None for value in (
                expected_identity, expected_versions, expected_gate_json_sha256,
                expected_content_sha256, expected_token,
            )
        )
        if has_expectations and not expected_matches(run):
            return "indeterminate"
        marker = connection.execute(
            "SELECT value FROM schema_metadata WHERE key=?",
            (f"scheduled_run_pending:{run_id}",),
        ).fetchone()
        gate = connection.execute(
            "SELECT gate_json FROM quality_gates WHERE run_id=?", (run_id,)
        ).fetchone()
        if gate is None:
            return "indeterminate"
        requested_token = expected_token if expected_token is not None else token
        if marker is not None:
            if requested_token is None or marker[0] != requested_token:
                return "indeterminate"
            return "pending"
        if requested_token is not None and marker_token(gate["gate_json"]) != requested_token:
            return "indeterminate"
        return "committed" if not has_expectations or expected_matches(run) else "indeterminate"
    except Exception:
        return "indeterminate"

def connect(path: Path, *, configured_canaries: Iterable[str] = ()) -> sqlite3.Connection:
    path = Path(path)
    if not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(path)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    initialize(connection, configured_canaries=configured_canaries)
    return connection


def initialize(connection: sqlite3.Connection, *, configured_canaries: Iterable[str] = ()) -> None:
    if connection.row_factory is None:
        connection.row_factory = sqlite3.Row
    classification = _classify(connection)
    if classification == "fresh":
        _create_v4(connection)
        connection.commit()
        return
    if classification == "valid-v3":
        _assert_v3_configured_canaries(connection, configured_canaries)
        _assert_v3_consistency(connection)
        _migrate_v3_to_v4(connection)
        return
    if classification == "valid-v4":
        _assert_v4_configured_canaries(connection, configured_canaries)
        _assert_v4_consistency(connection)
        return
    if classification.startswith("legacy"):
        _migrate_legacy(connection, configured_canaries=configured_canaries)
        _migrate_v3_to_v4(connection)
        return
    raise StorageSchemaError(f"unsupported storage schema: {classification}")


def _classify(connection: sqlite3.Connection) -> str:
    names = {
        row["name"] for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type IN ('table','view','trigger','index')"
        ) if not row["name"].startswith("sqlite_")
    }
    tables = {name for name in names if connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)
    ).fetchone()}
    if not tables:
        return "fresh" if not names else "unknown"
    if not tables.issubset(_TABLES) or "schema_metadata" not in tables:
        return "unknown"
    metadata = _columns(connection, "schema_metadata")
    if metadata != {"key", "value"}:
        return "malformed"
    rows = connection.execute("SELECT key, value FROM schema_metadata").fetchall()
    values = {row["key"]: row["value"] for row in rows}
    marker = values.get("schema_version")
    if marker is not None:
        try:
            version = int(marker)
        except (TypeError, ValueError):
            return "malformed"
        if version > SCHEMA_VERSION:
            return "higher"
    else:
        version = None
    if version == 4:
        if values.get("schema_signature") != SCHEMA_SIGNATURE or values.get("persistence_envelope_schema_version") != "4":
            return "mismatched"
        return "valid-v4" if _is_v4(connection) else "mismatched"
    if version == 3:
        if values.get("schema_signature") != "storage-v3" or values.get("persistence_envelope_schema_version") != "3":
            return "mismatched"
        return "valid-v3" if _is_v3(connection) else "mismatched"
    if names != tables:
        return "unknown"
    legacy = _legacy_shape(connection)
    if legacy is None:
        return "malformed" if version in (1, 2) else "unknown"
    if version is None:
        return f"legacy-v{legacy}"
    if version in (1, 2):
        return "legacy-current-v1" if version == 1 and _columns(connection, "feedback_events") == _CURRENT_FEEDBACK_COLUMNS else f"legacy-v{version}"
    return "mismatched"


def _columns(connection: sqlite3.Connection, table: str) -> set[str]:
    return {row["name"] for row in connection.execute(f"PRAGMA table_info({table})")}


def _legacy_shape(connection: sqlite3.Connection) -> int | None:
    required = {"runs": _LEGACY_RUN_COLUMNS, "source_attempts": _LEGACY_ATTEMPT_COLUMNS,
                "recommendations": _LEGACY_REC_COLUMNS, "quality_gates": _GATE_COLUMNS}
    if any(_columns(connection, table) != columns for table, columns in required.items()):
        return None
    feedback = _columns(connection, "feedback_events")
    if feedback == _LEGACY_FEEDBACK_COLUMNS:
        return 1
    if feedback == _CURRENT_FEEDBACK_COLUMNS:
        return 2
    return None


def _is_v3(connection: sqlite3.Connection) -> bool:
    expected = {
        "runs": _RUN_COLUMNS, "source_attempts": _ATTEMPT_COLUMNS, "recommendations": _REC_COLUMNS,
        "quality_gates": _GATE_COLUMNS, "feedback_events": _FEEDBACK_COLUMNS,
    }
    names = {
        row["name"] for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type IN ('table','view','trigger','index')"
        ) if not row["name"].startswith("sqlite_")
    }
    if names != _V3_TABLES | _GUARD_TRIGGERS | _INDEXES:
        return False
    if any(_columns(connection, table) != columns for table, columns in expected.items()):
        return False
    # A marker without the v3 FK topology is not a v3 database.
    for table, parent in (("source_attempts", "runs"), ("recommendations", "runs"),
                          ("quality_gates", "runs"), ("feedback_events", "runs"),
                          ("feedback_events", "recommendations")):
        if not any(row["table"] == parent and row["on_delete"].upper() == "RESTRICT"
                   for row in connection.execute(f"PRAGMA foreign_key_list({table})")):
            return False
    return True
def _is_v4(connection: sqlite3.Connection) -> bool:
    if not _is_v3_shape_without_names(connection):
        return False
    return _columns(connection, "source_outcomes") == _OUTCOME_COLUMNS


def _is_v3_shape_without_names(connection: sqlite3.Connection) -> bool:
    expected = {
        "runs": _RUN_COLUMNS, "source_attempts": _ATTEMPT_COLUMNS, "recommendations": _REC_COLUMNS,
        "quality_gates": _GATE_COLUMNS, "feedback_events": _FEEDBACK_COLUMNS,
    }
    names = {
        row["name"] for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type IN ('table','view','trigger','index')"
        ) if not row["name"].startswith("sqlite_")
    }
    if names != _V3_TABLES | _GUARD_TRIGGERS | _INDEXES | {"source_outcomes"}:
        return False
    if any(_columns(connection, table) != columns for table, columns in expected.items()):
        return False
    if not any(
        row["table"] == "runs" and row["on_delete"].upper() == "RESTRICT"
        for row in connection.execute("PRAGMA foreign_key_list(source_outcomes)")
    ):
        return False
    for table, parent in (("source_attempts", "runs"), ("recommendations", "runs"),
                          ("quality_gates", "runs"), ("feedback_events", "runs"),
                          ("feedback_events", "recommendations")):
        if not any(
            row["table"] == parent and row["on_delete"].upper() == "RESTRICT"
            for row in connection.execute(f"PRAGMA foreign_key_list({table})")
        ):
            return False
    return True


def _create_v3(connection: sqlite3.Connection, *, table_suffix: str = "") -> None:
    s = table_suffix
    statements = (
        """
        CREATE TABLE schema_metadata (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
        )
        """,
        f"""
        CREATE TABLE runs{s} (
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
            updated_at TEXT NOT NULL,
            record_schema_version INTEGER NOT NULL,
            pipeline_schema_version INTEGER NOT NULL,
            score_schema_version INTEGER NOT NULL,
            disposition_schema_version INTEGER NOT NULL
        )
        """,
        f"""
        CREATE TABLE source_attempts{s} (
            run_id TEXT NOT NULL,
            source_id TEXT NOT NULL,
            attempted INTEGER NOT NULL,
            candidate_count INTEGER NOT NULL,
            error_count INTEGER NOT NULL,
            errors_json TEXT NOT NULL,
            accepted_count INTEGER NOT NULL,
            rejected_count INTEGER NOT NULL,
            duplicate_count INTEGER NOT NULL,
            normalized_changed_field_count INTEGER NOT NULL,
            normalized_emptied_field_count INTEGER NOT NULL,
            detail_json TEXT NOT NULL,
            error_codes_json TEXT NOT NULL,
            duration_ms INTEGER NOT NULL,
            PRIMARY KEY (run_id, source_id),
            FOREIGN KEY (run_id) REFERENCES runs(run_id) ON DELETE RESTRICT
        )
        """,
        f"""
        CREATE TABLE recommendations{s} (
            recommendation_id TEXT PRIMARY KEY,
            run_id TEXT NOT NULL,
            source_id TEXT NOT NULL,
            source_url TEXT,
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
            posting_key TEXT NOT NULL,
            final_disposition TEXT NOT NULL,
            reason_codes_json TEXT NOT NULL,
            source_detail_quality TEXT NOT NULL,
            record_schema_version INTEGER NOT NULL,
            FOREIGN KEY (run_id) REFERENCES runs(run_id) ON DELETE RESTRICT
        )
        """,
        f"""
        CREATE TABLE quality_gates{s} (
            run_id TEXT PRIMARY KEY,
            status TEXT NOT NULL,
            context_status TEXT NOT NULL,
            gate_json TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            FOREIGN KEY (run_id) REFERENCES runs(run_id) ON DELETE RESTRICT
        )
        """,
        f"""
        CREATE TABLE feedback_events{s} (
            event_id TEXT PRIMARY KEY,
            recommendation_id TEXT NOT NULL,
            run_id TEXT NOT NULL,
            posting_key TEXT NOT NULL,
            source_id TEXT NOT NULL,
            source_posting_id TEXT,
            source_url TEXT,
            verdict TEXT NOT NULL,
            reason TEXT NOT NULL,
            movement TEXT NOT NULL,
            created_at TEXT NOT NULL,
            record_schema_version INTEGER NOT NULL,
            FOREIGN KEY (recommendation_id) REFERENCES recommendations(recommendation_id) ON DELETE RESTRICT,
            FOREIGN KEY (run_id) REFERENCES runs(run_id) ON DELETE RESTRICT
        )
        """,
        f"CREATE INDEX IF NOT EXISTS idx_recommendations_run ON recommendations{s}(run_id)",
        f"CREATE INDEX IF NOT EXISTS idx_feedback_recommendation ON feedback_events{s}(recommendation_id)",
    )
    for statement in statements:
        connection.execute(statement)
    if not s:
        connection.execute("INSERT INTO schema_metadata(key,value) VALUES (?,?)", ("schema_version", "3"))
        connection.execute("INSERT INTO schema_metadata(key,value) VALUES (?,?)", ("schema_signature", "storage-v3"))
        connection.execute("INSERT INTO schema_metadata(key,value) VALUES (?,?)", ("persistence_envelope_schema_version", "3"))
        _install_guards(connection)
def _create_v4(connection: sqlite3.Connection) -> None:
    _create_v3(connection)
    connection.execute("""
        CREATE TABLE source_outcomes (
            run_id TEXT NOT NULL,
            source_id TEXT NOT NULL,
            attempted INTEGER NOT NULL,
            completed INTEGER NOT NULL,
            status TEXT NOT NULL,
            error_code TEXT,
            duration_ms INTEGER NOT NULL,
            outcome_schema_version INTEGER NOT NULL,
            PRIMARY KEY (run_id, source_id),
            FOREIGN KEY (run_id) REFERENCES runs(run_id) ON DELETE RESTRICT
        )
    """)
    connection.execute(
        "UPDATE schema_metadata SET value='4' WHERE key='schema_version'"
    )
    connection.execute(
        "UPDATE schema_metadata SET value=? WHERE key='schema_signature'",
        (SCHEMA_SIGNATURE,),
    )
    connection.execute(
        "UPDATE schema_metadata SET value='4' WHERE key='persistence_envelope_schema_version'"
    )


def _migrate_v3_to_v4(connection: sqlite3.Connection) -> None:
    try:
        connection.execute("BEGIN IMMEDIATE")
        if _classify(connection) == "valid-v4":
            connection.commit()
            return
        if _classify(connection) != "valid-v3":
            raise StorageSchemaError("unsupported storage schema for v3 migration")
        connection.execute("""
            CREATE TABLE source_outcomes (
                run_id TEXT NOT NULL,
                source_id TEXT NOT NULL,
                attempted INTEGER NOT NULL,
                completed INTEGER NOT NULL,
                status TEXT NOT NULL,
                error_code TEXT,
                duration_ms INTEGER NOT NULL,
                outcome_schema_version INTEGER NOT NULL,
                PRIMARY KEY (run_id, source_id),
                FOREIGN KEY (run_id) REFERENCES runs(run_id) ON DELETE RESTRICT
            )
        """)
        # V3 attempts contain metrics, not execution outcomes.  Preserve the
        # V3 rows and leave the V4 outcome table empty rather than inferring
        # success, timeout, or failure from incomplete legacy observations.
        for row in connection.execute(
            "SELECT source_id, attempted, error_codes_json, duration_ms FROM source_attempts"
        ):
            try:
                errors = json.loads(row["error_codes_json"])
            except (TypeError, ValueError):
                raise ValueError from None
            if not isinstance(errors, list) or any(
                type(code) is not str for code in errors
            ):
                raise ValueError
            from .storage import _assert_public_code
            for code in errors:
                _assert_public_code(code)
            attempted = row["attempted"]
            duration_ms = row["duration_ms"]
            if type(attempted) is not int or attempted not in (0, 1):
                raise ValueError
            if type(duration_ms) is not int or duration_ms < 0:
                raise ValueError
            if type(row["source_id"]) is not str or not row["source_id"]:
                raise ValueError
            _assert_public_code(row["source_id"])
        connection.execute("UPDATE schema_metadata SET value='4' WHERE key='schema_version'")
        connection.execute(
            "UPDATE schema_metadata SET value=? WHERE key='schema_signature'",
            (SCHEMA_SIGNATURE,),
        )
        connection.execute(
            "UPDATE schema_metadata SET value='4' WHERE key='persistence_envelope_schema_version'"
        )
        if _classify(connection) != "valid-v4":
            raise StorageSchemaError("v3 migration produced an invalid schema")
        connection.commit()
    except StorageSchemaError:
        connection.rollback()
        raise
    except Exception:
        connection.rollback()
        raise StorageSchemaError("v3 migration failed atomically") from None


def _install_guards(connection: sqlite3.Connection) -> None:
    statements = (
        """
        CREATE TRIGGER schema_metadata_no_downgrade_insert
        BEFORE INSERT ON schema_metadata
        WHEN NEW.key='schema_version' AND EXISTS (
            SELECT 1 FROM schema_metadata WHERE key='schema_version' AND CAST(value AS INTEGER) >= 3
        )
        BEGIN SELECT RAISE(ABORT, 'storage schema downgrade or replacement'); END
        """,
        """
        CREATE TRIGGER schema_metadata_no_downgrade_update
        BEFORE UPDATE ON schema_metadata
        WHEN OLD.key='schema_version' AND CAST(OLD.value AS INTEGER) >= 3
             AND (NEW.key <> OLD.key OR CAST(NEW.value AS INTEGER) < 3)
        BEGIN SELECT RAISE(ABORT, 'storage schema downgrade or rename'); END
        """,
        """
        CREATE TRIGGER schema_metadata_no_downgrade_delete
        BEFORE DELETE ON schema_metadata
        WHEN OLD.key='schema_version' AND CAST(OLD.value AS INTEGER) >= 3
        BEGIN SELECT RAISE(ABORT, 'storage schema marker deletion'); END
        """,
    )
    for statement in statements:
        connection.execute(statement)


def _migrate_legacy(connection: sqlite3.Connection, *, configured_canaries: Iterable[str] = ()) -> None:
    tables = ("runs", "source_attempts", "recommendations", "quality_gates", "feedback_events")
    connection.execute("PRAGMA foreign_keys=OFF")
    try:
        connection.execute("BEGIN IMMEDIATE")
    except Exception:
        connection.execute("PRAGMA foreign_keys=ON")
        raise
    try:
        snapshots: dict[str, list[dict[str, Any]]] = {
            table: [dict(row) for row in connection.execute(f"SELECT * FROM {table}").fetchall()] for table in tables
        }
        from .storage import (
            _PUBLIC_EVIDENCE,
            _PUBLIC_FEEDBACK_MOVEMENTS,
            _PUBLIC_FEEDBACK_VERDICTS,
            _assert_configured_canaries,
            _assert_public_code,
            _assert_public_text,
            _assert_public_url,
            _configured_canary_matcher,
        )

        matcher = _configured_canary_matcher(configured_canaries)
        _assert_configured_canaries(snapshots, matcher)

        def text(value: Any, *, optional: bool = False) -> str | None:
            if optional and value is None:
                return None
            if type(value) is not str or (not optional and not value):
                raise ValueError
            _assert_public_text(value)
            return value

        def code(value: Any) -> str:
            value = text(value)
            assert value is not None
            _assert_public_code(value)
            return value

        def integer(value: Any, *, nonnegative: bool = True) -> int:
            if type(value) is not int or (nonnegative and value < 0):
                raise ValueError
            return value

        def json_value(value: Any, *, kind: type) -> Any:
            if type(value) is not str:
                raise ValueError
            parsed = json.loads(value)
            if not isinstance(parsed, kind):
                raise ValueError
            return parsed

        def safe_gate(value: Any) -> None:
            if type(value) is str:
                _assert_public_text(value)
            elif isinstance(value, dict):
                for key, item in value.items():
                    text(key)
                    safe_gate(item)
            elif isinstance(value, list):
                for item in value:
                    safe_gate(item)
            elif value is None or type(value) in (bool, int, float):
                return
            else:
                raise ValueError

        safe: dict[str, list[dict[str, Any]]] = {table: [] for table in tables}
        for row in snapshots["runs"]:
            for key in ("run_id", "command_mode", "run_date", "source_config_hash", "profile_config_hash",
                        "status", "context_status", "created_at", "updated_at"):
                text(row[key])
            if row["command_mode"] not in {"scheduled-run", "live-run", "dry-run", "replay"}:
                raise ValueError
            try:
                parsed_date = date.fromisoformat(row["run_date"])
            except (TypeError, ValueError):
                raise ValueError from None
            if parsed_date.isoformat() != row["run_date"]:
                raise ValueError
            if type(row["report_generated"]) is not int or row["report_generated"] not in (0, 1):
                raise ValueError
            for key in ("candidates_collected", "ranked_count"):
                integer(row[key])
            if row.get("report_path") is not None:
                text(row["report_path"])
            safe["runs"].append(row)
        run_ids = {row["run_id"] for row in safe["runs"]}
        if any(row["run_id"] not in run_ids for row in snapshots["source_attempts"]):
            raise ValueError
        if any(row["run_id"] not in run_ids for row in snapshots["recommendations"]):
            raise ValueError
        if any(row["run_id"] not in run_ids for row in snapshots["quality_gates"]):
            raise ValueError
        if any(row["run_id"] not in run_ids for row in snapshots["feedback_events"]):
            raise ValueError

        for row in snapshots["source_attempts"]:
            text(row["run_id"])
            code(row["source_id"])
            for key in ("attempted", "candidate_count", "error_count"):
                integer(row[key])
            errors = json_value(row["errors_json"], kind=list)
            if any(type(item) is not str for item in errors):
                raise ValueError
            errors = [code(item) for item in errors]
            if row["error_count"] != len(errors) or errors != sorted(set(errors)):
                raise ValueError
            safe["source_attempts"].append({
                **row,
                "_errors": errors,
                "_detail": json.dumps({"manual_only": row["candidate_count"], "rejected": 0, "verified": 0}, separators=(",", ":")),
            })

        rec_map: dict[str, tuple[str, dict[str, Any], str | None]] = {}
        rec_ids: set[str] = set()
        for row in snapshots["recommendations"]:
            for key in ("recommendation_id", "run_id", "title", "company", "location"):
                text(row[key])
            code(row["source_id"])
            rec_id = row["recommendation_id"]
            if rec_id in rec_ids:
                raise ValueError
            rec_ids.add(rec_id)
            source_posting_id = text(row.get("source_posting_id"), optional=True)
            deadline = text(row.get("deadline"), optional=True)
            if deadline is not None:
                try:
                    parsed_deadline = date.fromisoformat(deadline)
                except (TypeError, ValueError):
                    raise ValueError from None
                if parsed_deadline.isoformat() != deadline:
                    raise ValueError
            integer(row["score"], nonnegative=False)
            recommendation = code(row["recommendation"])
            verdict = code(row["verdict"])
            if recommendation not in {"apply", "hold", "manual_review", "low_priority", "exclude", "expired", "include", "interesting"}:
                raise ValueError
            if verdict not in {"apply", "hold", "manual_review", "low_priority", "exclude", "expired", "include", "interesting"}:
                raise ValueError
            evidence = json_value(row["matched_evidence_json"], kind=list)
            if any(type(item) is not str or item not in _PUBLIC_EVIDENCE for item in evidence):
                raise ValueError
            for item in evidence:
                _assert_public_text(item)
            gaps = json_value(row["gaps_json"], kind=list)
            risks = json_value(row["risks_json"], kind=list)
            if gaps or risks:
                raise ValueError
            # Legacy source URLs and detail claims have no independently validated provenance.
            source_url = None
            final = "manual_review"
            posting_key = _posting_key(row["source_id"], source_url, source_posting_id,
                                       row["title"], row["company"], rec_id)
            projected = {
                **row,
                "_source_posting_id": source_posting_id,
                "_deadline": deadline,
                "_source_url": source_url,
                "_evidence": [],
                "_final": final,
                "_posting_key": posting_key,
            }
            safe["recommendations"].append(projected)
            rec_map[rec_id] = (posting_key, projected, source_url)

        for row in snapshots["quality_gates"]:
            text(row["run_id"])
            status = code(row["status"])
            context_status = code(row["context_status"])
            if type(row["gate_json"]) is not str:
                raise ValueError
            gate = json.loads(row["gate_json"])
            if not isinstance(gate, dict):
                raise ValueError
            safe_gate(gate)
            text(row["updated_at"])
            safe["quality_gates"].append({
                **row, "_status": "legacy", "_context_status": "unverified",
                # Legacy gate payloads are never copied into the public V3 projection.
                "_gate_json": "{}",
            })

        for row in snapshots["feedback_events"]:
            rec_id = row["recommendation_id"]
            if rec_id not in rec_map:
                raise ValueError
            if row["run_id"] != rec_map[rec_id][1]["run_id"]:
                raise ValueError
            for key in ("event_id", "recommendation_id", "run_id", "created_at"):
                text(row[key])
            verdict = text(row["verdict"])
            movement = text(row["movement"])
            if verdict is None or movement is None:
                raise ValueError
            if verdict not in _PUBLIC_FEEDBACK_VERDICTS or movement not in _PUBLIC_FEEDBACK_MOVEMENTS:
                raise ValueError
            reason = text(row["reason"])
            assert reason is not None
            created_at = row["created_at"]
            try:
                parsed_date = date.fromisoformat(created_at)
            except ValueError:
                parsed_date = None
            if parsed_date is not None and parsed_date.isoformat() == created_at:
                created_at = datetime.combine(parsed_date, datetime.min.time(), timezone.utc).isoformat()
            else:
                try:
                    parsed_timestamp = datetime.fromisoformat(created_at)
                except ValueError:
                    raise ValueError from None
                if parsed_timestamp.tzinfo is None or parsed_timestamp.utcoffset() is None:
                    raise ValueError
                created_at = parsed_timestamp.isoformat()
            safe["feedback_events"].append({
                **row, "_created_at": created_at, "_verdict": verdict,
                "_movement": movement, "_reason": reason,
            })

    except Exception:
        try:
            connection.rollback()
        finally:
            connection.execute("PRAGMA foreign_keys=ON")
        raise StorageSchemaError("legacy database contains unsafe or malformed public data") from None

    try:
        for table in tables:
            connection.execute(f"ALTER TABLE {table} RENAME TO {table}_legacy")
        connection.execute("DROP TABLE schema_metadata")
        _create_v3(connection)
        for row in safe["runs"]:
            connection.execute("""INSERT INTO runs VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""", (
                row["run_id"], row["command_mode"], row["run_date"], row["source_config_hash"], row["profile_config_hash"],
                "legacy", "unverified", 0, None, row["candidates_collected"],
                row["ranked_count"], row["created_at"], row["updated_at"], 3, 2, 2, 2))
        for row in safe["source_attempts"]:
            errors = row["_errors"]
            connection.execute("""INSERT INTO source_attempts VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""", (
                row["run_id"], row["source_id"], row["attempted"], row["candidate_count"], row["error_count"],
                json.dumps(errors, ensure_ascii=False, separators=(",", ":")), row["candidate_count"],
                0, 0, 0, 0, row["_detail"], json.dumps(errors, ensure_ascii=False, separators=(",", ":")), 0))
        for row in safe["recommendations"]:
            connection.execute("""INSERT INTO recommendations VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""", (
                row["recommendation_id"], row["run_id"], row["source_id"], row["_source_url"],
                row["_source_posting_id"], row["title"], row["company"], row["location"], row["_deadline"],
                row["score"], row["_final"], "exclude", json.dumps(row["_evidence"], ensure_ascii=False, separators=(",", ":")),
                "[]", "[]", row["_posting_key"], row["_final"], "[]", "manual_only", 3))
        for row in safe["quality_gates"]:
            connection.execute("INSERT INTO quality_gates VALUES (?,?,?,?,?)", (
                row["run_id"], row["_status"], row["_context_status"], row["_gate_json"], row["updated_at"]))
        for row in safe["feedback_events"]:
            posting_key, rec, source_url = rec_map[row["recommendation_id"]]
            connection.execute("""INSERT INTO feedback_events VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""", (
                row["event_id"], row["recommendation_id"], row["run_id"], posting_key,
                rec["source_id"], rec["_source_posting_id"], source_url, row["_verdict"],
                row["_reason"], row["_movement"], row["_created_at"], 3))
        for table in tables:
            connection.execute(f"DROP TABLE {table}_legacy")
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.execute("PRAGMA foreign_keys=ON")
        if connection.execute("PRAGMA foreign_keys").fetchone()[0] != 1:
            raise StorageSchemaError("could not restore foreign key enforcement")


def _assert_v3_configured_canaries(connection: sqlite3.Connection, configured_canaries: Iterable[str]) -> None:
    try:
        from .storage import _assert_configured_canaries, _configured_canary_matcher
        matcher = _configured_canary_matcher(configured_canaries)
        for table in ("runs", "source_attempts", "recommendations", "quality_gates", "feedback_events"):
            for row in connection.execute(f"SELECT * FROM {table}"):
                _assert_configured_canaries(tuple(row), matcher)
    except Exception:
        raise StorageSchemaError("configured canary found in public storage") from None
def _assert_v4_configured_canaries(connection: sqlite3.Connection, configured_canaries: Iterable[str]) -> None:
    _assert_v3_configured_canaries(connection, configured_canaries)
    try:
        from .storage import _assert_configured_canaries, _configured_canary_matcher
        matcher = _configured_canary_matcher(configured_canaries)
        for row in connection.execute("SELECT * FROM source_outcomes"):
            _assert_configured_canaries(tuple(row), matcher)
    except Exception:
        raise StorageSchemaError("configured canary found in public storage") from None


def _assert_v4_consistency(connection: sqlite3.Connection) -> None:
    _assert_v3_consistency(connection)
    for row in connection.execute(
        "SELECT source_id, attempted, completed, status, error_code, duration_ms, outcome_schema_version "
        "FROM source_outcomes"
    ):
        if row["source_id"] not in PUBLIC_SOURCE_IDS_V1:
            raise StorageSchemaError("database contains an invalid source outcome source")
        if any(type(row[key]) is not int or row[key] not in (0, 1) for key in ("attempted", "completed")):
            raise StorageSchemaError("database contains an invalid source outcome state")
        if type(row["duration_ms"]) is not int or row["duration_ms"] < 0:
            raise StorageSchemaError("database contains an invalid source outcome duration")
        if row["outcome_schema_version"] != 1:
            raise StorageSchemaError("database contains an invalid source outcome version")
        if row["status"] not in SOURCE_EXECUTION_OUTCOME_STATUSES_V1:
            raise StorageSchemaError("database contains an invalid source outcome status")
        if row["error_code"] is not None:
            if row["error_code"] not in SOURCE_EXECUTION_OUTCOME_ERROR_CODES_V1:
                raise StorageSchemaError("database contains an invalid source outcome error")
        if row["status"] == "success":
            valid = row["attempted"] == 1 and row["completed"] == 1 and row["error_code"] is None
        else:
            valid = row["attempted"] == 1 and row["completed"] == 0 and row["error_code"] == row["status"]
        if not valid:
            raise StorageSchemaError("database contains an invalid source outcome state")


def _json_list(value: Any) -> list[Any]:
    if not value:
        return []
    try:
        parsed = json.loads(value)
    except (TypeError, ValueError):
        return []
    return parsed if isinstance(parsed, list) else []




def _final_disposition(recommendation: str, verdict: str) -> str:
    value = recommendation or verdict
    if value in {"apply", "hold", "manual_review", "low_priority", "exclude", "expired"}:
        return value
    if value in {"include", "interesting"}:
        return "apply"
    return "exclude"


def _posting_key(source_id: str, source_url: str | None, source_posting_id: str | None,
                 title: str = "", company: str = "", recommendation_id: str | None = None) -> str:
    if source_posting_id:
        basis = {"kind": "source_posting_id", "source_id": source_id, "value": source_posting_id}
    elif source_url:
        basis = {"kind": "canonical_url", "source_id": source_id, "value": source_url}
    elif recommendation_id:
        basis = {"kind": "legacy_recommendation", "source_id": source_id, "value": recommendation_id}
    else:
        basis = {"kind": "title_company", "source_id": source_id, "value": f"{title}\0{company}"}
    payload = json.dumps(basis, ensure_ascii=False, separators=(",", ":"), sort_keys=False)
    return hashlib.sha256(("posting-v3\0" + payload).encode("utf-8")).hexdigest()[:32]


def stable_digest(value: Mapping[str, str | None]) -> str:
    payload = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()
def _assert_v3_consistency(connection: sqlite3.Connection) -> None:
    for row in connection.execute("SELECT report_generated, report_path FROM runs"):
        report_generated = row["report_generated"]
        if type(report_generated) is not int or report_generated not in (0, 1):
            raise StorageSchemaError("database contains an invalid report state")
        if (report_generated == 1) != (row["report_path"] is not None):
            raise StorageSchemaError("database report state does not match report path")
