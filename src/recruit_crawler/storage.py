from __future__ import annotations

import hashlib
import re
import json
import sqlite3
from dataclasses import asdict, is_dataclass
from urllib.parse import urlsplit
from datetime import date, datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any, Iterable, Mapping, TypedDict

from ._storage_core import StorageSchemaError, connect, probe_run_transaction_state
from ._storage_feedback import add_feedback_event, export_feedback_events
from .user_context import UserContextImportError
from .user_context_documents import _reject_private_text
from .schemas import (
    PERSISTENCE_ENVELOPE_SCHEMA_VERSION,
    PUBLIC_SOURCE_IDS_V1,
    SOURCE_EXECUTION_OUTCOME_ERROR_CODES_V1,
    SOURCE_EXECUTION_OUTCOME_STATUSES_V1,
    SOURCE_OUTCOME_SCHEMA_VERSION,
)
from .report_policy import verified_link_url
from .report_writer import (
    _contains_unsafe_military_term,
    _normalize_for_matching,
    _normalized_canary_match,
)
_PUBLIC_DISPOSITIONS = {"expired", "exclude", "manual_review", "apply", "hold", "low_priority"}
_PUBLIC_RECOMMENDATION_VERDICTS = {"include", "hold", "exclude"}
_PUBLIC_FEEDBACK_VERDICTS = {
    "applied",
    "ignored",
    "hidden",
    "false_positive",
    "false_negative",
    "interesting",
    "not_relevant",
}
# Kept as the historical public vocabulary for callers that validate both
# recommendation and feedback records.  Feedback paths use the exact
# _PUBLIC_FEEDBACK_VERDICTS set above.
_PUBLIC_VERDICTS = _PUBLIC_DISPOSITIONS | _PUBLIC_RECOMMENDATION_VERDICTS | _PUBLIC_FEEDBACK_VERDICTS
_PUBLIC_SOURCE_IDS = PUBLIC_SOURCE_IDS_V1
_PUBLIC_OUTCOME_STATUSES = SOURCE_EXECUTION_OUTCOME_STATUSES_V1
_PUBLIC_OUTCOME_ERROR_CODES = SOURCE_EXECUTION_OUTCOME_ERROR_CODES_V1
_PUBLIC_FEEDBACK_MOVEMENTS = {"up", "down", "same"}


class RunIdentityRecord(TypedDict):
    command_mode: str
    run_date: str
    source_config_hash: str
    profile_config_hash: str
    run_id: str


class RunRecord(TypedDict):
    run_id: str
    command_mode: str
    run_date: str
    status: str
    context_status: str
    report_generated: int
    report_path: str | None
    candidates_collected: int
    ranked_count: int
    created_at: str
    updated_at: str


class RecommendationRecord(TypedDict):
    recommendation_id: str
    posting_key: str
    run_id: str
    source_id: str
    source_url: str | None
    source_posting_id: str | None
    title: str
    company: str
    location: str
    deadline: str | None
    score: int
    final_disposition: str
    reason_codes: list[str]
    source_detail_quality: str
    matched_evidence: list[str]
class SourceOutcomeRecord(TypedDict):
    run_id: str
    source_id: str
    attempted: int
    completed: int
    status: str
    error_code: str | None
    duration_ms: int
    outcome_schema_version: int


class EnvelopeValidationError(ValueError):
    pass

_PUBLIC_CODE_RE = re.compile(r"[a-z0-9][a-z0-9_.-]{0,63}\Z")
_PUBLIC_REASON_CODES = {
    "dealbreaker", "education_ambiguous", "education_mismatch",
    "education_unknown", "expired", "experience_ambiguous",
    "experience_mismatch", "experience_unknown", "invalid_candidate",
    "manual_flag", "manual_source",
}
_PUBLIC_EVIDENCE = {
    "필수 요건 일치",
    "담당 업무 관련성",
    "우대 요건 일치",
    "선호 직무 일치",
    "근무지 조건 일치",
}


_PENDING_RUN_KEY_PREFIX = "scheduled_run_pending:"


def persist_scheduled_run(
    db_path: Path,
    envelope: Any,
    *,
    configured_canaries: Iterable[str] = (),
) -> str | None:
    """Stage one validated envelope and return its owned pending token.

    The marker is deliberately retained until the caller has durably promoted
    the final file Gate. ``None`` means the identical run was already
    committed by an earlier invocation and this invocation owns no state.
    """
    value = _plain(envelope)
    matcher = _configured_canary_matcher(configured_canaries)
    _assert_configured_canaries(value, matcher)
    normalized = _validate_envelope(value)
    _assert_configured_canaries(normalized, matcher)
    run = normalized["run"]
    attempts = normalized["attempts"]
    outcomes = normalized["outcomes"]
    recommendations = normalized["recommendations"]
    gate_json = normalized["gate_json"]
    now = datetime.now(timezone.utc).isoformat()

    with connect(Path(db_path), configured_canaries=matcher) as connection:
        _pending_run_ids(connection)
        existing = connection.execute(
            "SELECT * FROM runs WHERE run_id = ?", (run["run_id"],)
        ).fetchone()
        pending_key = _pending_run_key(run["run_id"])
        if existing is not None:
            if not _stored_payload_matches(connection, existing, run, attempts, outcomes, recommendations, gate_json):
                raise EnvelopeValidationError(f"conflicting envelope for run_id: {run['run_id']}")
            pending = connection.execute(
                "SELECT value FROM schema_metadata WHERE key = ?", (pending_key,)
            ).fetchone()
            if pending is None:
                return None
            try:
                return _validate_pending_marker(
                    connection, run["run_id"], pending["value"], error_type=EnvelopeValidationError
                )
            except StorageSchemaError as exc:
                raise EnvelopeValidationError(str(exc)) from exc
        connection.execute("BEGIN IMMEDIATE")
        try:
            connection.execute("""
                INSERT INTO runs(
                    run_id, command_mode, run_date, source_config_hash, profile_config_hash,
                    status, context_status, report_generated, report_path, candidates_collected,
                    ranked_count, created_at, updated_at, record_schema_version,
                    pipeline_schema_version, score_schema_version, disposition_schema_version
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """, (
                run["run_id"], run["command_mode"], run["run_date"], run["source_config_hash"],
                run["profile_config_hash"], run["status"], run["context_status"], run["report_generated"],
                run["report_path"], run["candidates_collected"], run["ranked_count"], now, now, 3, 2, 2, 2,
            ))
            for attempt in attempts:
                connection.execute("""
                    INSERT INTO source_attempts(
                        run_id, source_id, attempted, candidate_count, error_count, errors_json,
                        accepted_count, rejected_count, duplicate_count, normalized_changed_field_count,
                        normalized_emptied_field_count, detail_json, error_codes_json, duration_ms
                    ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                """, (
                    run["run_id"], attempt["source_id"], attempt["attempted"], attempt["candidate_count"],
                    attempt["error_count"], attempt["error_codes_json"], attempt["accepted_count"],
                    attempt["rejected_count"], attempt["duplicate_count"], attempt["normalized_changed_field_count"],
                    attempt["normalized_emptied_field_count"], attempt["detail_json"], attempt["error_codes_json"],
                    attempt["duration_ms"],
                ))
            for outcome in outcomes:
                connection.execute("""
                    INSERT INTO source_outcomes(
                        run_id, source_id, attempted, completed, status, error_code,
                        duration_ms, outcome_schema_version
                    ) VALUES (?,?,?,?,?,?,?,?)
                """, (
                    run["run_id"], outcome["source_id"], outcome["attempted"], outcome["completed"],
                    outcome["status"], outcome["error_code"], outcome["duration_ms"],
                    outcome["outcome_schema_version"],
                ))
            for recommendation in recommendations:
                connection.execute("""
                    INSERT INTO recommendations(
                        recommendation_id, run_id, source_id, source_url, source_posting_id, title,
                        company, location, deadline, score, recommendation, verdict,
                        matched_evidence_json, gaps_json, risks_json, posting_key, final_disposition,
                        reason_codes_json, source_detail_quality, record_schema_version
                    ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                """, (
                    recommendation["recommendation_id"], run["run_id"], recommendation["source_id"],
                    recommendation["source_url"], recommendation["source_posting_id"], recommendation["title"],
                    recommendation["company"], recommendation["location"], recommendation["deadline"],
                    recommendation["score"], recommendation["final_disposition"], recommendation["verdict"],
                    recommendation["matched_evidence_json"], "[]", "[]", recommendation["posting_key"],
                    recommendation["final_disposition"], recommendation["reason_codes_json"],
                    recommendation["source_detail_quality"], 3,
                ))
            connection.execute("""
                INSERT INTO quality_gates(run_id, status, context_status, gate_json, updated_at)
                VALUES (?,?,?,?,?)
            """, (run["run_id"], run["status"], run["context_status"], gate_json, now))
            token = _pending_marker_token(run["run_id"], gate_json)
            connection.execute(
                "INSERT INTO schema_metadata(key, value) VALUES (?, ?)",
                (pending_key, token),
            )
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        return token


def _pending_run_key(run_id: str) -> str:
    return f"{_PENDING_RUN_KEY_PREFIX}{run_id}"


def _pending_marker_token(run_id: str, gate_json: str) -> str:
    try:
        canonical_gate = _json_canonical(json.loads(gate_json))
    except (TypeError, ValueError):
        raise StorageSchemaError("database contains an invalid pending run gate") from None
    return hashlib.sha256(
        (run_id + "\0" + canonical_gate).encode("utf-8")
    ).hexdigest()


def _validate_pending_marker(
    connection: sqlite3.Connection,
    run_id: str,
    token: Any,
    *,
    error_type: type[Exception] = StorageSchemaError,
) -> str:
    def invalid(message: str) -> None:
        raise error_type(message)

    if type(run_id) is not str or not run_id:
        invalid("invalid pending scheduled run marker")
    if type(token) is not str or len(token) != 64 or any(
        character not in "0123456789abcdef" for character in token
    ):
        invalid("invalid pending scheduled run marker")
    run = connection.execute(
        "SELECT * FROM runs WHERE run_id = ?", (run_id,)
    ).fetchone()
    gate = connection.execute(
        "SELECT gate_json FROM quality_gates WHERE run_id = ?", (run_id,)
    ).fetchone()
    if run is None or gate is None:
        invalid("database contains an orphan pending run marker")
    try:
        expected = _pending_marker_token(run_id, gate["gate_json"])
    except StorageSchemaError as exc:
        invalid(str(exc))
        return token
    if token != expected:
        invalid("database contains a mismatched pending run marker")
    return token


def finalize_scheduled_run(
    db_path: Path,
    run_id: str,
    token: str,
    *,
    expected_identity: Mapping[str, Any] | None = None,
    expected_versions: Mapping[str, Any] | None = None,
    expected_gate_json_sha256: str | None = None,
    expected_content_sha256: str | None = None,
    expected_token: str | None = None,
) -> bool:
    """Finalize only the exact pending state staged by this invocation."""
    path = Path(db_path)
    if not path.exists():
        return False
    try:
        with connect(path) as connection:
            connection.execute("BEGIN IMMEDIATE")
            marker = connection.execute(
                "SELECT value FROM schema_metadata WHERE key = ?",
                (_pending_run_key(run_id),),
            ).fetchone()
            if marker is None:
                connection.rollback()
                return False
            requested_token = expected_token if expected_token is not None else token
            _validate_pending_marker(connection, run_id, requested_token)
            if (
                expected_identity is not None
                or expected_versions is not None
                or expected_gate_json_sha256 is not None
                or expected_content_sha256 is not None
            ):
                state = probe_run_transaction_state(
                    connection,
                    run_id,
                    requested_token,
                    expected_identity=expected_identity,
                    expected_versions=expected_versions,
                    expected_gate_json_sha256=expected_gate_json_sha256,
                    expected_content_sha256=expected_content_sha256,
                    expected_token=requested_token,
                )
                if state != "pending":
                    connection.rollback()
                    return False
            connection.execute(
                "DELETE FROM schema_metadata WHERE key = ?",
                (_pending_run_key(run_id),),
            )
            connection.commit()
        return True
    except Exception:
        return False


def persistence_probe_expectations(envelope: Any) -> dict[str, Any]:
    """Derive the complete V4 identity used to reconcile a helper timeout."""
    normalized = _validate_envelope(_plain(envelope))
    run = normalized["run"]
    gate_json = normalized["gate_json"]
    try:
        parsed_gate = json.loads(gate_json)
    except (TypeError, ValueError):
        raise EnvelopeValidationError("invalid persistence gate identity") from None
    if (
        not isinstance(parsed_gate, dict)
        or type(parsed_gate.get("gate_json_sha256")) is not str
        or not re.fullmatch(r"[0-9a-f]{64}", parsed_gate["gate_json_sha256"])
    ):
        raise EnvelopeValidationError("invalid persistence gate identity")
    projection = parsed_gate.get("gate_projection") if isinstance(parsed_gate, dict) else None
    report = projection.get("report") if isinstance(projection, dict) else None
    content_sha256 = report.get("content_sha256") if isinstance(report, dict) else None
    if not isinstance(content_sha256, str) or not re.fullmatch(r"[0-9a-f]{64}", content_sha256):
        raise EnvelopeValidationError("invalid persistence report identity")
    return {
        "expected_identity": {
            key: run[key]
            for key in ("run_id", "command_mode", "run_date", "source_config_hash", "profile_config_hash")
        },
        "expected_versions": {
            "record_schema_version": 3,
            "pipeline_schema_version": 2,
            "score_schema_version": 2,
            "disposition_schema_version": 2,
        },
        "expected_gate_json_sha256": parsed_gate["gate_json_sha256"],
        "expected_content_sha256": content_sha256,
        "expected_token": _pending_marker_token(run["run_id"], gate_json),
    }


def scheduled_run_persistence_state(
    db_path: Path,
    run_id: str,
    *,
    expected_identity: Mapping[str, Any] | None = None,
    expected_versions: Mapping[str, Any] | None = None,
    expected_gate_json_sha256: str | None = None,
    expected_content_sha256: str | None = None,
    expected_token: str | None = None,
) -> str:
    """Return a reconciled V4 state; committed requires exact identities."""
    path = Path(db_path)
    if not path.exists():
        return "absent"
    try:
        with connect(path) as connection:
            marker = connection.execute(
                "SELECT value FROM schema_metadata WHERE key = ?",
                (_pending_run_key(run_id),),
            ).fetchone()
            token = marker["value"] if marker is not None else None
            return probe_run_transaction_state(
                connection,
                run_id,
                token,
                expected_identity=expected_identity,
                expected_versions=expected_versions,
                expected_gate_json_sha256=expected_gate_json_sha256,
                expected_content_sha256=expected_content_sha256,
                expected_token=expected_token,
            )
    except StorageSchemaError:
        raise
    except Exception as exc:
        raise StorageSchemaError("could not determine scheduled persistence state") from exc


def discard_scheduled_run(
    db_path: Path,
    run_id: str,
    token: str | None = None,
    *,
    expected_identity: Mapping[str, Any] | None = None,
    expected_versions: Mapping[str, Any] | None = None,
    expected_gate_json_sha256: str | None = None,
    expected_content_sha256: str | None = None,
    expected_token: str | None = None,
) -> bool:
    """Discard only an owned pending stage with matching V4 identity."""
    if not isinstance(token, str):
        return False
    path = Path(db_path)
    if not path.exists():
        return False
    try:
        with connect(path) as connection:
            connection.execute("BEGIN IMMEDIATE")
            marker = connection.execute(
                "SELECT value FROM schema_metadata WHERE key = ?",
                (_pending_run_key(run_id),),
            ).fetchone()
            if marker is None:
                connection.rollback()
                return False
            requested_token = expected_token if expected_token is not None else token
            _validate_pending_marker(connection, run_id, requested_token)
            if (
                expected_identity is not None
                or expected_versions is not None
                or expected_gate_json_sha256 is not None
                or expected_content_sha256 is not None
            ):
                state = probe_run_transaction_state(
                    connection,
                    run_id,
                    requested_token,
                    expected_identity=expected_identity,
                    expected_versions=expected_versions,
                    expected_gate_json_sha256=expected_gate_json_sha256,
                    expected_content_sha256=expected_content_sha256,
                    expected_token=requested_token,
                )
                if state != "pending":
                    connection.rollback()
                    return False
            connection.execute("DELETE FROM feedback_events WHERE run_id = ?", (run_id,))
            connection.execute("DELETE FROM recommendations WHERE run_id = ?", (run_id,))
            connection.execute("DELETE FROM source_attempts WHERE run_id = ?", (run_id,))
            connection.execute("DELETE FROM source_outcomes WHERE run_id = ?", (run_id,))
            connection.execute("DELETE FROM quality_gates WHERE run_id = ?", (run_id,))
            connection.execute("DELETE FROM runs WHERE run_id = ?", (run_id,))
            connection.execute(
                "DELETE FROM schema_metadata WHERE key = ?",
                (_pending_run_key(run_id),),
            )
            connection.commit()
        return True
    except Exception:
        return False



def export_runs(db_path: Path, *, configured_canaries: Iterable[str] = ()) -> list[RunRecord]:
    matcher = _export_canary_matcher(configured_canaries)
    try:
        with connect(Path(db_path), configured_canaries=matcher) as connection:
            return _validated_run_exports(connection)
    except StorageSchemaError:
        raise
    except Exception:
        raise StorageSchemaError("database contains unsafe or malformed public data") from None
def export_source_outcomes(
    db_path: Path,
    *,
    run_id: str | None = None,
    configured_canaries: Iterable[str] = (),
) -> list[SourceOutcomeRecord]:
    matcher = _export_canary_matcher(configured_canaries)
    try:
        with connect(Path(db_path), configured_canaries=matcher) as connection:
            pending = _pending_run_ids(connection)
            runs = _validated_run_context(connection)
            query = """
                SELECT run_id, source_id, attempted, completed, status, error_code,
                       duration_ms, outcome_schema_version
                FROM source_outcomes
            """
            parameters: tuple[Any, ...] = ()
            if run_id is not None:
                query += " WHERE run_id = ?"
                parameters = (run_id,)
            query += " ORDER BY run_id ASC, source_id ASC"
            result: list[SourceOutcomeRecord] = []
            for row in connection.execute(query, parameters):
                if row["run_id"] in pending:
                    continue
                if row["run_id"] not in runs:
                    raise StorageSchemaError("database contains an orphan source outcome")
                result.append(_validated_source_outcome_export(row))
            return result
    except StorageSchemaError:
        raise
    except Exception:
        raise StorageSchemaError("database contains unsafe or malformed public data") from None


def _validated_source_outcome_export(row: sqlite3.Row) -> SourceOutcomeRecord:
    if row["source_id"] not in _PUBLIC_SOURCE_IDS:
        raise StorageSchemaError("database contains an invalid source outcome source")
    if type(row["attempted"]) is not int or row["attempted"] not in (0, 1):
        raise StorageSchemaError("database contains an invalid source outcome state")
    if type(row["completed"]) is not int or row["completed"] not in (0, 1):
        raise StorageSchemaError("database contains an invalid source outcome state")
    if row["status"] not in _PUBLIC_OUTCOME_STATUSES:
        raise StorageSchemaError("database contains an invalid source outcome status")
    if row["error_code"] is not None and row["error_code"] not in _PUBLIC_OUTCOME_ERROR_CODES:
        raise StorageSchemaError("database contains an invalid source outcome error")
    if type(row["duration_ms"]) is not int or row["duration_ms"] < 0:
        raise StorageSchemaError("database contains an invalid source outcome duration")
    if row["outcome_schema_version"] != SOURCE_OUTCOME_SCHEMA_VERSION:
        raise StorageSchemaError("database contains an invalid source outcome version")
    if row["status"] == "success":
        valid = row["attempted"] == 1 and row["completed"] == 1 and row["error_code"] is None
    else:
        valid = row["attempted"] == 1 and row["completed"] == 0 and row["error_code"] == row["status"]
    if not valid:
        raise StorageSchemaError("database contains an invalid source outcome state")
    return {
        "run_id": row["run_id"],
        "source_id": row["source_id"],
        "attempted": row["attempted"],
        "completed": row["completed"],
        "status": row["status"],
        "error_code": row["error_code"],
        "duration_ms": row["duration_ms"],
        "outcome_schema_version": row["outcome_schema_version"],
    }


def export_recommendations(db_path: Path, *, configured_canaries: Iterable[str] = ()) -> list[RecommendationRecord]:
    matcher = _export_canary_matcher(configured_canaries)
    try:
        with connect(Path(db_path), configured_canaries=matcher) as connection:
            pending_run_ids = _pending_run_ids(connection)
            runs = _validated_run_context(connection)
            rows = connection.execute("""
                SELECT recommendation_id, posting_key, run_id, source_id, source_url, source_posting_id,
                       title, company, location, deadline, score, recommendation, verdict,
                       matched_evidence_json, gaps_json, risks_json, final_disposition, reason_codes_json,
                       source_detail_quality, record_schema_version
                FROM recommendations ORDER BY run_id ASC, score DESC, recommendation_id ASC
            """).fetchall()
            result: list[RecommendationRecord] = []
            counts: dict[str, int] = {}
            for row in rows:
                run_id = row["run_id"]
                if run_id in pending_run_ids:
                    continue
                counts[run_id] = counts.get(run_id, 0) + 1
                result.append(_validated_recommendation_export(row, runs))
            if any(runs[run_id]["ranked_count"] != count for run_id, count in counts.items()):
                raise StorageSchemaError("database recommendation count disagrees with run")
            if any(run_id not in counts and run["ranked_count"] != 0 for run_id, run in runs.items()):
                raise StorageSchemaError("database recommendation count disagrees with run")
            return result
    except StorageSchemaError:
        raise
    except Exception:
        raise StorageSchemaError("database contains unsafe or malformed public data") from None


def _export_canary_matcher(configured_canaries: Iterable[str]) -> tuple[str, ...]:
    try:
        return _configured_canary_matcher(configured_canaries)
    except Exception:
        raise StorageSchemaError("invalid configured canaries") from None


def _pending_run_ids(connection: sqlite3.Connection) -> set[str]:
    rows = connection.execute(
        "SELECT key, value FROM schema_metadata WHERE key LIKE ?",
        (_PENDING_RUN_KEY_PREFIX + "%",),
    ).fetchall()
    result: set[str] = set()
    for row in rows:
        key = row["key"]
        run_id = key[len(_PENDING_RUN_KEY_PREFIX):]
        if not run_id or key != _pending_run_key(run_id):
            raise StorageSchemaError("database contains an invalid pending run marker")
        _validate_pending_marker(connection, run_id, row["value"])
        result.add(run_id)
    return result


def _validated_run_context(connection: sqlite3.Connection) -> dict[str, dict[str, Any]]:
    pending_run_ids = _pending_run_ids(connection)
    run_rows = connection.execute("""
        SELECT run_id, command_mode, run_date, source_config_hash, profile_config_hash,
               status, context_status, report_generated, report_path, candidates_collected,
               ranked_count, created_at, updated_at, record_schema_version,
               pipeline_schema_version, score_schema_version, disposition_schema_version
        FROM runs
    """).fetchall()
    gate_rows = {
        row["run_id"]: row for row in connection.execute(
            "SELECT run_id, status, context_status, gate_json, updated_at FROM quality_gates"
        ).fetchall()
        if row["run_id"] not in pending_run_ids
    }
    result: dict[str, dict[str, Any]] = {}
    for row in run_rows:
        if row["run_id"] in pending_run_ids:
            continue
        item = dict(row)
        if item["run_id"] in result or item["record_schema_version"] != 3:
            raise StorageSchemaError("database contains unsafe or malformed public data")
        for key in (
            "run_id", "command_mode", "run_date", "source_config_hash", "profile_config_hash",
            "status", "context_status", "created_at", "updated_at",
        ):
            _assert_public_text(item[key])
        legacy = item["status"] == "legacy" and item["context_status"] == "unverified"
        current = item["command_mode"] == "scheduled-run" and item["status"] == "pass" and item["context_status"] == "complete"
        if not (legacy or current):
            raise StorageSchemaError("database does not contain a confirmed or quarantined run")
        try:
            parsed_date = date.fromisoformat(item["run_date"])
        except (TypeError, ValueError):
            raise StorageSchemaError("database contains an invalid run date") from None
        if parsed_date.isoformat() != item["run_date"]:
            raise StorageSchemaError("database contains an invalid run date")
        if type(item["report_generated"]) is not int or item["report_generated"] not in (0, 1):
            raise StorageSchemaError("database contains an invalid report state")
        if legacy and (item["report_generated"] != 0 or item["report_path"] is not None):
            raise StorageSchemaError("legacy run contains report evidence")
        if current and (item["report_generated"] != 1 or item["report_path"] is None):
            raise StorageSchemaError("passing run is missing generated report")
        if item["report_path"] is not None:
            if type(item["report_path"]) is not str or not item["report_path"] or item["report_path"].startswith("/"):
                raise StorageSchemaError("database contains an invalid report path")
            if ".." in item["report_path"].split("/"):
                raise StorageSchemaError("database contains an invalid report path")
            _assert_public_text(item["report_path"])
        for key in ("candidates_collected", "ranked_count"):
            if type(item[key]) is not int or isinstance(item[key], bool) or item[key] < 0:
                raise StorageSchemaError("database contains an invalid run count")
        if any(item[key] != 2 for key in ("pipeline_schema_version", "score_schema_version", "disposition_schema_version")):
            raise StorageSchemaError("database contains an invalid schema version")
        gate = gate_rows.get(item["run_id"])
        expected_gate = ("legacy", "unverified") if legacy else ("pass", "complete")
        if gate is None or (gate["status"], gate["context_status"]) != expected_gate:
            raise StorageSchemaError("database does not contain a matching quality gate")
        _assert_public_text(gate["run_id"])
        _assert_public_text(gate["updated_at"])
        _assert_public_text(gate["status"])
        _assert_public_text(gate["context_status"])
        _validate_gate_json(gate["gate_json"], item)
        result[item["run_id"]] = item
    if len(gate_rows) != len(result):
        raise StorageSchemaError("database contains an orphan quality gate")
    return result


def _validate_gate_json(value: Any, run: Mapping[str, Any] | None = None) -> None:
    if type(value) is not str or not value:
        raise StorageSchemaError("database contains an invalid quality gate")
    try:
        parsed = json.loads(value)
    except (TypeError, ValueError):
        raise StorageSchemaError("database contains an invalid quality gate") from None
    if not isinstance(parsed, dict):
        raise StorageSchemaError("database contains an invalid quality gate")
    _validate_public_json(parsed)
    if run is not None and run.get("status") == "legacy":
        if parsed != {} or run.get("report_generated") != 0 or run.get("report_path") is not None:
            raise StorageSchemaError("legacy gate contains retained evidence")
        return
    if not parsed or set(parsed) != {"gate_json_sha256", "gate_projection"} or value != _json_canonical(parsed):
        raise StorageSchemaError("database contains an invalid quality gate")
    digest = parsed["gate_json_sha256"]
    if type(digest) is not str or len(digest) != 64 or any(c not in "0123456789abcdef" for c in digest):
        raise StorageSchemaError("database contains an invalid quality gate digest")
    if run is not None:
        projection = parsed["gate_projection"]
        if not isinstance(projection, dict):
            raise StorageSchemaError("database contains an invalid quality gate")
        if set(projection) != {"schema_version", "command_mode", "run_date", "status", "context_status", "report", "sources", "summary"}:
            raise StorageSchemaError("database contains an invalid quality gate")
        if projection.get("schema_version") != 2:
            raise StorageSchemaError("database contains an invalid quality gate version")
        for key in ("command_mode", "run_date", "status", "context_status"):
            if projection.get(key) != run[key]:
                raise StorageSchemaError("database gate does not match run identity")
        report = projection["report"]
        if not isinstance(report, dict) or set(report) != {"generated", "content_sha256", "byte_length"}:
            raise StorageSchemaError("database contains an invalid quality gate report")
        if report.get("generated") is not True or report.get("generated") is not bool(run["report_generated"]):
            raise StorageSchemaError("database does not contain generated report evidence")
        content_sha256 = report["content_sha256"]
        if type(content_sha256) is not str or len(content_sha256) != 64 or any(c not in "0123456789abcdef" for c in content_sha256):
            raise StorageSchemaError("database contains an invalid report digest")
        if type(report["byte_length"]) is not int or isinstance(report["byte_length"], bool) or report["byte_length"] < 0:
            raise StorageSchemaError("database contains an invalid report length")


def _validate_public_json(value: Any) -> None:
    if type(value) is str:
        _assert_public_text(value)
    elif type(value) in (bool, int, float) or value is None:
        return
    elif isinstance(value, list):
        for item in value:
            _validate_public_json(item)
    elif isinstance(value, dict):
        for key, item in value.items():
            _assert_public_text(key)
            _validate_public_json(item)
    else:
        raise StorageSchemaError("database contains unsafe JSON")


def _validated_run_exports(connection: sqlite3.Connection) -> list[RunRecord]:
    runs = _validated_run_context(connection)
    result: list[RunRecord] = []
    for item in sorted(runs.values(), key=lambda value: (value["run_date"], value["updated_at"]), reverse=True):
        result.append({
            key: item[key] for key in (
                "run_id", "command_mode", "run_date", "status", "context_status",
                "report_generated", "report_path", "candidates_collected", "ranked_count",
                "created_at", "updated_at",
            )
        })
    return result


def _validated_recommendation_export(
    row: sqlite3.Row,
    runs: dict[str, dict[str, Any]],
) -> RecommendationRecord:
    item = dict(row)
    run_id = item["run_id"]
    if run_id not in runs or item["record_schema_version"] != 3:
        raise StorageSchemaError("database contains an orphan recommendation")
    legacy = runs[run_id]["status"] == "legacy"
    for key in ("recommendation_id", "posting_key", "title", "company", "location", "source_detail_quality"):
        _assert_public_text(item[key])
    _assert_public_code(item["source_id"])
    if item["source_detail_quality"] not in {"verified", "manual_only"}:
        raise StorageSchemaError("database contains invalid detail quality")
    for key in ("source_posting_id",):
        if item[key] is not None:
            _assert_public_text(item[key])
    if item["deadline"] is not None:
        if type(item["deadline"]) is not str:
            raise StorageSchemaError("database contains an invalid deadline")
        try:
            parsed_deadline = date.fromisoformat(item["deadline"])
        except ValueError:
            raise StorageSchemaError("database contains an invalid deadline") from None
        if parsed_deadline.isoformat() != item["deadline"]:
            raise StorageSchemaError("database contains an invalid deadline")
    if type(item["score"]) is not int or isinstance(item["score"], bool):
        raise StorageSchemaError("database contains an invalid score")
    final = item["final_disposition"]
    if final not in _PUBLIC_DISPOSITIONS or item["recommendation"] != final:
        raise StorageSchemaError("database contains an invalid recommendation disposition")
    expected_verdict = "include" if final == "apply" else "hold" if final == "hold" else "exclude"
    if item["verdict"] != expected_verdict or item["verdict"] not in _PUBLIC_RECOMMENDATION_VERDICTS:
        raise StorageSchemaError("database contains an invalid recommendation verdict")
    if legacy and (
        item["source_detail_quality"] != "manual_only"
        or item["source_url"] is not None
        or final != "manual_review"
        or item["recommendation"] != "manual_review"
        or item["verdict"] != "exclude"
    ):
        raise StorageSchemaError("legacy recommendation is not quarantined")
    if not isinstance(item["source_url"], (str, type(None))):
        raise StorageSchemaError("database contains an invalid source URL")
    expected_url = verified_link_url(
        "scheduled-run", item["source_id"], item["source_url"], item["source_posting_id"],
        item["source_detail_quality"],
    )
    if item["source_url"] != expected_url:
        raise StorageSchemaError("database contains an unverified source URL")
    if item["source_url"] is not None:
        _assert_public_url(item["source_url"])
    reason_codes = _export_json_list(item["reason_codes_json"], _PUBLIC_REASON_CODES)
    evidence = _export_json_list(item["matched_evidence_json"], _PUBLIC_EVIDENCE)
    if legacy and evidence:
        raise StorageSchemaError("legacy recommendation contains evidence")
    if _export_json_list(item["gaps_json"], None) or _export_json_list(item["risks_json"], None):
        raise StorageSchemaError("database contains private recommendation detail")
    if runs[run_id]["ranked_count"] < 0:
        raise StorageSchemaError("database contains an invalid ranked count")
    return {
        "recommendation_id": item["recommendation_id"],
        "posting_key": item["posting_key"],
        "run_id": run_id,
        "source_id": item["source_id"],
        "source_url": item["source_url"],
        "source_posting_id": item["source_posting_id"],
        "title": item["title"],
        "company": item["company"],
        "location": item["location"],
        "deadline": item["deadline"],
        "score": item["score"],
        "final_disposition": final,
        "reason_codes": reason_codes,
        "source_detail_quality": item["source_detail_quality"],
        "matched_evidence": evidence,
    }


def _export_json_list(value: Any, allowed: set[str] | None) -> list[Any]:
    if type(value) is not str:
        raise StorageSchemaError("database contains malformed JSON")
    try:
        parsed = json.loads(value)
    except (TypeError, ValueError):
        raise StorageSchemaError("database contains malformed JSON") from None
    if not isinstance(parsed, list):
        raise StorageSchemaError("database contains malformed JSON")
    if any(type(entry) is not str for entry in parsed):
        raise StorageSchemaError("database contains unsafe JSON")
    if allowed is not None and any(entry not in allowed for entry in parsed):
        raise StorageSchemaError("database contains unsafe public values")
    for entry in parsed:
        _assert_public_text(entry)
    return parsed


def _plain(value: Any) -> Any:
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, Path):
        return str(value)
    if is_dataclass(value):
        return {key: _plain(item) for key, item in asdict(value).items()}
    if isinstance(value, Mapping):
        return {str(key): _plain(item) for key, item in value.items()}
    if isinstance(value, tuple):
        if value and all(isinstance(item, tuple) and len(item) == 2 and isinstance(item[0], str) for item in value):
            return {item[0]: _plain(item[1]) for item in value}
        return [_plain(item) for item in value]
    if isinstance(value, list):
        return [_plain(item) for item in value]
    return value


def _validate_envelope(envelope: Any) -> dict[str, Any]:
    if not isinstance(envelope, dict):
        raise EnvelopeValidationError("envelope must be a mapping")
    required = {"schema_version", "run_identity", "report_artifact", "gate_status", "context_status",
                "gate_json_sha256", "summary", "source_metrics", "assessments", "source_outcomes"}
    if set(envelope) != required or envelope["schema_version"] != PERSISTENCE_ENVELOPE_SCHEMA_VERSION:
        raise EnvelopeValidationError("invalid persistence envelope v4 shape")
    identity = envelope["run_identity"]
    identity_keys = {"command_mode", "run_date", "source_config_hash", "profile_config_hash", "run_id"}
    if not isinstance(identity, dict) or set(identity) != identity_keys:
        raise EnvelopeValidationError("invalid run identity")
    _require_strings(identity, identity_keys)
    for key in identity_keys:
        _assert_public_text(identity[key])
    if identity["command_mode"] != "scheduled-run":
        raise EnvelopeValidationError("envelope command must be scheduled-run")
    try:
        parsed_run_date = date.fromisoformat(identity["run_date"])
    except ValueError as exc:
        raise EnvelopeValidationError("invalid run date") from exc
    if parsed_run_date.isoformat() != identity["run_date"]:
        raise EnvelopeValidationError("invalid run date")
    if envelope["gate_status"] != "pass" or envelope["context_status"] != "complete":
        raise EnvelopeValidationError("only a passing complete scheduled run may be persisted")
    artifact = envelope["report_artifact"]
    if not isinstance(artifact, dict) or set(artifact) != {"schema_version", "generated", "path", "rendered"}:
        raise EnvelopeValidationError("invalid report artifact")
    if artifact["schema_version"] != 2 or not isinstance(artifact["generated"], bool):
        raise EnvelopeValidationError("invalid report artifact version")
    if not isinstance(artifact["path"], (str, type(None))):
        raise EnvelopeValidationError("invalid report path")
    if artifact["generated"] and (not isinstance(artifact["path"], str) or not artifact["path"]):
        raise EnvelopeValidationError("generated report must have a path")
    if artifact["path"] is not None and (
        not isinstance(artifact["path"], str) or artifact["path"].startswith("/") or ".." in artifact["path"].split("/")
    ):
        raise EnvelopeValidationError("report path must not be a full/private path")
    rendered = artifact["rendered"]
    if not artifact["generated"]:
        if artifact["path"] is not None or rendered is not None:
            raise EnvelopeValidationError("false artifact must be empty")
    else:
        if not isinstance(rendered, dict) or set(rendered) != {"schema_version", "markdown_bytes", "content_sha256", "byte_length"}:
            raise EnvelopeValidationError("generated artifact is incomplete")
        if rendered["schema_version"] != 2 or type(rendered["markdown_bytes"]) is not bytes:
            raise EnvelopeValidationError("invalid rendered report")
        data = rendered["markdown_bytes"]
        if hashlib.sha256(data).hexdigest() != rendered["content_sha256"] or len(data) != rendered["byte_length"]:
            raise EnvelopeValidationError("rendered report hash/length mismatch")
        try:
            report_text = data.decode("utf-8", errors="strict")
        except UnicodeDecodeError as exc:
            raise EnvelopeValidationError("rendered report is not strict UTF-8") from exc
        if report_text.startswith("\ufeff") or not report_text.endswith("\n"):
            raise EnvelopeValidationError("rendered report is not public text")
        _assert_public_text(report_text)
    gate_hash = envelope["gate_json_sha256"]
    if not isinstance(gate_hash, str) or len(gate_hash) != 64 or any(c not in "0123456789abcdef" for c in gate_hash):
        raise EnvelopeValidationError("invalid gate JSON hash")

    summary = envelope["summary"]
    summary_keys = {"collected", "source_rejected", "source_accepted", "duplicates_removed", "deduplicated", "expired", "exclude",
                    "manual_review_total", "apply_total", "hold_total", "low_priority_total", "actionable_total", "displayed_apply",
                    "displayed_hold", "suppressed_apply", "suppressed_hold", "displayed_manual", "suppressed_manual"}
    if not isinstance(summary, dict) or set(summary) != summary_keys or any(not _nonnegative_int(summary[k]) for k in summary_keys):
        raise EnvelopeValidationError("invalid summary")
    if summary["collected"] != summary["source_accepted"] + summary["source_rejected"]:
        raise EnvelopeValidationError("collection summary disagrees with source counts")
    if summary["deduplicated"] != summary["source_accepted"] - summary["duplicates_removed"]:
        raise EnvelopeValidationError("deduplication summary disagrees with source counts")
    if summary["apply_total"] != summary["displayed_apply"] + summary["suppressed_apply"]:
        raise EnvelopeValidationError("apply display summary disagrees")
    if summary["hold_total"] != summary["displayed_hold"] + summary["suppressed_hold"]:
        raise EnvelopeValidationError("hold display summary disagrees")
    if summary["manual_review_total"] != summary["displayed_manual"] + summary["suppressed_manual"]:
        raise EnvelopeValidationError("manual display summary disagrees")

    sources = envelope["source_metrics"]
    if not isinstance(sources, list):
        raise EnvelopeValidationError("source metrics must be a list")
    attempts: list[dict[str, Any]] = []
    seen_sources: set[str] = set()
    for source in sources:
        expected = {"source_id", "attempted", "candidate_count", "source_rejected_count", "duplicate_count",
                    "normalized_changed_field_count", "normalized_emptied_field_count", "detail_quality", "error_count", "error_codes", "duration_ms"}
        if not isinstance(source, dict) or set(source) != expected:
            raise EnvelopeValidationError("invalid source metric")
        if not isinstance(source["source_id"], str) or not source["source_id"] or not isinstance(source["attempted"], bool):
            raise EnvelopeValidationError("invalid source metric identity")
        if source["source_id"] not in _PUBLIC_SOURCE_IDS:
            raise EnvelopeValidationError("unknown source metric identity")
        if not source["attempted"]:
            raise EnvelopeValidationError("passing envelope contains an unattempted source")
        if source["source_id"] in seen_sources:
            raise EnvelopeValidationError("invalid or duplicate source metric")
        seen_sources.add(source["source_id"])
        _assert_public_code(source["source_id"])
        count_keys = ("candidate_count", "source_rejected_count", "duplicate_count", "normalized_changed_field_count",
                      "normalized_emptied_field_count", "error_count", "duration_ms")
        if any(not _nonnegative_int(source[key]) for key in count_keys):
            raise EnvelopeValidationError("invalid source metric count")
        if not isinstance(source["error_codes"], list) or any(not isinstance(c, str) for c in source["error_codes"]):
            raise EnvelopeValidationError("invalid source errors")
        for code in source["error_codes"]:
            _assert_public_code(code)
        if source["error_count"] != len(source["error_codes"]) or source["error_codes"] != sorted(set(source["error_codes"])):
            raise EnvelopeValidationError("source error count/order mismatch")
        if source["error_codes"]:
            raise EnvelopeValidationError("passing gate cannot contain source errors")
        detail = source["detail_quality"]
        if not isinstance(detail, dict) or set(detail) != {"manual_only", "rejected", "verified"} or any(not _nonnegative_int(detail[k]) for k in detail):
            raise EnvelopeValidationError("invalid source detail quality")
        if source["duplicate_count"] > source["candidate_count"]:
            raise EnvelopeValidationError("source duplicate count exceeds accepted candidates")
        survivor_count = source["candidate_count"] - source["duplicate_count"]
        if detail["verified"] + detail["manual_only"] != survivor_count or detail["rejected"] != source["source_rejected_count"]:
            raise EnvelopeValidationError("source detail and counts disagree")
        attempts.append({
            "source_id": source["source_id"], "attempted": int(source["attempted"]), "candidate_count": source["candidate_count"],
            "error_count": source["error_count"], "accepted_count": source["candidate_count"], "rejected_count": source["source_rejected_count"],
            "duplicate_count": source["duplicate_count"], "normalized_changed_field_count": source["normalized_changed_field_count"],
            "normalized_emptied_field_count": source["normalized_emptied_field_count"],
            "detail_json": _json_canonical({"manual_only": detail["manual_only"], "rejected": detail["rejected"], "verified": detail["verified"]}),
            "errors_json": _json_canonical(source["error_codes"]), "error_codes_json": _json_canonical(source["error_codes"]), "duration_ms": source["duration_ms"],
        })
    if [source["source_id"] for source in sources] != sorted(seen_sources):
        raise EnvelopeValidationError("source metrics must be source-id ordered")
    if sum(a["candidate_count"] for a in attempts) != summary["source_accepted"] or sum(a["rejected_count"] for a in attempts) != summary["source_rejected"] or sum(a["duplicate_count"] for a in attempts) != summary["duplicates_removed"]:
        raise EnvelopeValidationError("source metrics and summary disagree")
    source_outcomes = envelope["source_outcomes"]
    if not isinstance(source_outcomes, list) or len(source_outcomes) != len(seen_sources):
        raise EnvelopeValidationError("invalid source outcomes")
    outcomes: list[dict[str, Any]] = []
    seen_outcomes: set[str] = set()
    for outcome in source_outcomes:
        expected_outcome = {
            "source_id", "attempted", "completed", "status", "error_code", "elapsed_ms",
        }
        if not isinstance(outcome, dict) or set(outcome) != expected_outcome:
            raise EnvelopeValidationError("invalid source outcome")
        source_id = outcome["source_id"]
        if (
            not isinstance(source_id, str) or not source_id
            or source_id in seen_outcomes or source_id not in seen_sources
            or source_id not in _PUBLIC_SOURCE_IDS
            or not isinstance(outcome["attempted"], bool)
            or not isinstance(outcome["completed"], bool)
            or outcome["status"] not in _PUBLIC_OUTCOME_STATUSES
            or outcome["error_code"] is not None and (
                not isinstance(outcome["error_code"], str)
                or outcome["error_code"] not in _PUBLIC_OUTCOME_ERROR_CODES
            )
            or not _nonnegative_int(outcome["elapsed_ms"])
        ):
            raise EnvelopeValidationError("invalid source outcome")
        if (
            outcome["status"] != "success"
            or not outcome["attempted"]
            or not outcome["completed"]
            or outcome["error_code"] is not None
        ):
            raise EnvelopeValidationError("passing envelope requires successful source outcomes")
        seen_outcomes.add(source_id)
        outcomes.append({
            "source_id": source_id,
            "attempted": int(outcome["attempted"]),
            "completed": int(outcome["completed"]),
            "status": outcome["status"],
            "error_code": outcome["error_code"],
            "duration_ms": outcome["elapsed_ms"],
            "outcome_schema_version": SOURCE_OUTCOME_SCHEMA_VERSION,
        })
    if seen_outcomes != seen_sources or [item["source_id"] for item in source_outcomes] != sorted(seen_outcomes):
        raise EnvelopeValidationError("source outcomes must be source-id ordered")
    metric_durations = {source["source_id"]: source["duration_ms"] for source in sources}
    if any(item["duration_ms"] != metric_durations[item["source_id"]] for item in outcomes):
        raise EnvelopeValidationError("source outcomes and metrics disagree")
    assessments = envelope["assessments"]
    if not isinstance(assessments, list):
        raise EnvelopeValidationError("assessments must be a list")
    assessment_detail_counts = {
        source_id: {"verified": 0, "manual_only": 0}
        for source_id in seen_sources
    }
    recommendations: list[dict[str, Any]] = []
    counts = {key: 0 for key in ("expired", "exclude", "manual_review", "apply", "hold", "low_priority")}
    seen_ids: set[str] = set()
    for assessment in assessments:
        expected_assessment = {
            "recommendation_id", "posting_key", "source_id", "source_url",
            "source_posting_id", "title", "company", "location", "deadline",
            "score", "final_disposition", "reason_codes", "source_detail_quality",
            "matched_evidence",
        }
        if not isinstance(assessment, dict) or set(assessment) != expected_assessment or assessment["recommendation_id"] in seen_ids:
            raise EnvelopeValidationError("invalid assessment identity")
        seen_ids.add(assessment["recommendation_id"])
        for key in ("recommendation_id", "posting_key", "source_id", "title", "company", "source_detail_quality"):
            if not isinstance(assessment[key], str) or not assessment[key]:
                raise EnvelopeValidationError("invalid assessment field")
        for key in ("recommendation_id", "posting_key", "source_id", "title", "company", "source_detail_quality"):
            _assert_public_text(assessment[key])
        if assessment["source_id"] not in _PUBLIC_SOURCE_IDS:
            raise EnvelopeValidationError("unknown assessment source")
        if assessment["source_id"] not in seen_sources:
            raise EnvelopeValidationError("assessment source is missing from source metrics")
        if assessment["source_url"] is not None:
            _assert_public_url(assessment["source_url"])
        if not isinstance(assessment["source_posting_id"], (str, type(None))) or not isinstance(assessment["score"], int):
            raise EnvelopeValidationError("invalid assessment scalar")
        if assessment["source_posting_id"] is not None and not assessment["source_posting_id"]:
            raise EnvelopeValidationError("invalid assessment scalar")
        if assessment["deadline"] is not None:
            if not isinstance(assessment["deadline"], str):
                raise EnvelopeValidationError("invalid assessment deadline")
            try:
                date.fromisoformat(assessment["deadline"])
            except ValueError as exc:
                raise EnvelopeValidationError("invalid assessment deadline") from exc
        disposition = assessment["final_disposition"]
        if disposition not in counts:
            raise EnvelopeValidationError("invalid disposition")
        if not isinstance(assessment["reason_codes"], list) or any(not isinstance(x, str) for x in assessment["reason_codes"]):
            raise EnvelopeValidationError("invalid reason codes")
        if any(code not in _PUBLIC_REASON_CODES for code in assessment["reason_codes"]):
            raise EnvelopeValidationError("invalid public reason code")
        if assessment["source_detail_quality"] not in {"verified", "manual_only"}:
            raise EnvelopeValidationError("invalid source detail quality")
        expected_source_url = verified_link_url(
            "scheduled-run",
            assessment["source_id"],
            assessment["source_url"],
            assessment["source_posting_id"],
            assessment["source_detail_quality"],
        )
        if assessment["source_url"] != expected_source_url:
            raise EnvelopeValidationError("assessment source URL is not source-bound")
        assessment_detail_counts[assessment["source_id"]][assessment["source_detail_quality"]] += 1
        if not isinstance(assessment["matched_evidence"], list) or any(not isinstance(x, str) for x in assessment["matched_evidence"]):
            raise EnvelopeValidationError("invalid evidence")
        for field in ("source_url", "source_posting_id", "title", "company", "location"):
            if assessment[field] is not None:
                _assert_public_text(assessment[field])
        for evidence in assessment["matched_evidence"]:
            if evidence not in _PUBLIC_EVIDENCE:
                raise EnvelopeValidationError("invalid public evidence")
            _assert_public_text(evidence)
        counts[disposition] += 1
        recommendations.append({
            "recommendation_id": assessment["recommendation_id"], "posting_key": assessment["posting_key"], "source_id": assessment["source_id"],
            "source_url": assessment["source_url"], "source_posting_id": assessment["source_posting_id"], "title": assessment["title"],
            "company": assessment["company"], "location": assessment["location"], "deadline": assessment["deadline"], "score": assessment["score"],
            "final_disposition": disposition, "recommendation": disposition,
            "verdict": "include" if disposition == "apply" else "hold" if disposition == "hold" else "exclude",
            "reason_codes_json": _json_canonical(assessment["reason_codes"]), "matched_evidence_json": _json_canonical(assessment["matched_evidence"]),
            "source_detail_quality": assessment["source_detail_quality"],
        })
    if len(assessments) != summary["deduplicated"]:
        raise EnvelopeValidationError("assessment count and summary disagree")
    for source in sources:
        detail = source["detail_quality"]
        observed = assessment_detail_counts[source["source_id"]]
        if detail["verified"] != observed["verified"] or detail["manual_only"] != observed["manual_only"]:
            raise EnvelopeValidationError("source detail and assessments disagree")
    expected_counts = {"expired": "expired", "exclude": "exclude", "manual_review": "manual_review_total", "apply": "apply_total", "hold": "hold_total", "low_priority": "low_priority_total"}
    if any(counts[key] != summary[field] for key, field in expected_counts.items()):
        raise EnvelopeValidationError("disposition counts and summary disagree")
    if summary["actionable_total"] != counts["apply"] + counts["hold"]:
        raise EnvelopeValidationError("actionable count and summary disagree")
    report = {
        "generated": artifact["generated"],
        "content_sha256": rendered["content_sha256"] if isinstance(rendered, dict) else None,
        "byte_length": rendered["byte_length"] if isinstance(rendered, dict) else 0,
    }
    # Timing is retained in the private attempt/outcome rows, but the
    # persisted public gate projection must remain deterministic across
    # retries of the same run.
    gate_sources = [
        {**source, "duration_ms": 0}
        for source in envelope["source_metrics"]
    ]
    gate_projection = {
        "schema_version": 2,
        "command_mode": identity["command_mode"],
        "run_date": identity["run_date"],
        "status": envelope["gate_status"],
        "context_status": envelope["context_status"],
        "report": report,
        "sources": gate_sources,
        "summary": summary,
    }
    gate_json = _json_canonical(
        {
            "gate_json_sha256": gate_hash,
            "gate_projection": gate_projection,
        }
    )
    return {
        "run": {
            **identity,
            "status": envelope["gate_status"],
            "context_status": envelope["context_status"],
            "report_generated": int(artifact["generated"]),
            "report_path": artifact["path"],
            "candidates_collected": summary["collected"],
            "ranked_count": len(recommendations),
        },
        "attempts": attempts,
        "outcomes": outcomes,
        "recommendations": recommendations,
        "gate_json": gate_json,
    }


def _stored_payload_matches(connection: sqlite3.Connection, row: sqlite3.Row, run: dict[str, Any], attempts: list[dict[str, Any]], outcomes: list[dict[str, Any]], recommendations: list[dict[str, Any]], gate_json: str) -> bool:
    for key in ("run_id", "command_mode", "run_date", "source_config_hash", "profile_config_hash", "status", "context_status", "report_generated", "report_path", "candidates_collected", "ranked_count"):
        if row[key] != run[key]:
            return False
    actual_attempts = [dict(r) for r in connection.execute("SELECT * FROM source_attempts WHERE run_id=? ORDER BY source_id", (run["run_id"],))]
    actual_outcomes = [dict(r) for r in connection.execute("SELECT * FROM source_outcomes WHERE run_id=? ORDER BY source_id", (run["run_id"],))]
    actual_recs = [dict(r) for r in connection.execute("SELECT * FROM recommendations WHERE run_id=? ORDER BY recommendation_id", (run["run_id"],))]
    expected_attempts = sorted(attempts, key=lambda x: x["source_id"])
    expected_recs = sorted(recommendations, key=lambda x: x["recommendation_id"])
    expected_outcomes = sorted(outcomes, key=lambda x: x["source_id"])
    if len(actual_attempts) != len(expected_attempts) or len(actual_outcomes) != len(expected_outcomes) or len(actual_recs) != len(expected_recs):
        return False
    for actual, expected in zip(actual_attempts, expected_attempts):
        for key in ("source_id", "attempted", "candidate_count", "error_count", "errors_json", "accepted_count", "rejected_count", "duplicate_count", "normalized_changed_field_count", "normalized_emptied_field_count", "detail_json", "error_codes_json"):
            if actual[key] != expected[key]:
                return False
    for actual, expected in zip(actual_outcomes, expected_outcomes):
        for key in ("source_id", "attempted", "completed", "status", "error_code", "outcome_schema_version"):
            if actual[key] != expected[key]:
                return False
    for actual, expected in zip(actual_recs, expected_recs):
        for key in ("recommendation_id", "source_id", "source_url", "source_posting_id", "title", "company", "location", "deadline", "score", "recommendation", "verdict", "matched_evidence_json", "posting_key", "final_disposition", "reason_codes_json", "source_detail_quality"):
            if actual[key] != expected[key]:
                return False
    gate = connection.execute("SELECT status, context_status, gate_json FROM quality_gates WHERE run_id=?", (run["run_id"],)).fetchone()
    return gate is not None and gate["status"] == run["status"] and gate["context_status"] == run["context_status"] and gate["gate_json"] == gate_json


def _json_canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=False)


def _json_array(value: str) -> list[Any]:
    try:
        parsed = json.loads(value)
    except (TypeError, ValueError):
        return []
    return parsed if isinstance(parsed, list) else []


def _require_strings(mapping: Mapping[str, Any], keys: set[str]) -> None:
    if any(not isinstance(mapping[key], str) or not mapping[key] for key in keys):
        raise EnvelopeValidationError("identity values must be nonempty strings")


def _nonnegative_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


def _assert_public_url(value: str) -> None:
    if type(value) is not str:
        raise EnvelopeValidationError("invalid source URL")
    parsed = urlsplit(value)
    segments = [item for item in parsed.path.split("/") if item]
    if (
        parsed.scheme != "https"
        or not parsed.netloc
        or parsed.username is not None
        or parsed.password is not None
        or not segments
        or parsed.fragment
    ):
        raise EnvelopeValidationError("invalid source URL")
    _assert_public_text(value)
def _assert_public_text(value: str) -> None:
    if type(value) is not str or not value:
        raise EnvelopeValidationError("unsafe public persistence text")
    try:
        _reject_private_text(value)
    except UserContextImportError as exc:
        raise EnvelopeValidationError("unsafe public persistence text") from exc
    normalized = _normalize_for_matching(value)
    if _contains_unsafe_military_term(value) or any(token in normalized for token in (
        "private_", "raw_", "raw jd", "raw-jd", "user_context", "opaque_identity", "canary",
        "profile:", "resume:", "desired roles:", "user context:", "military",
    )):
        raise EnvelopeValidationError("unsafe public persistence text")
    if any(ord(char) < 32 and char not in "\t\n\r" for char in value):
        raise EnvelopeValidationError("unsafe public persistence text")


def _configured_canary_matcher(
    configured_canaries: Iterable[str],
) -> tuple[str, ...]:
    try:
        values = (configured_canaries,) if isinstance(configured_canaries, str) else tuple(configured_canaries)
    except TypeError as exc:
        raise EnvelopeValidationError("invalid configured canaries") from exc
    if any(type(value) is not str or not value for value in values):
        raise EnvelopeValidationError("invalid configured canaries")
    return tuple(_normalize_for_matching(value) for value in values)


def _assert_configured_canaries(
    value: Any,
    matcher: tuple[str, ...],
) -> None:
    if type(value) is str:
        if _normalized_canary_match(value, matcher):
            raise EnvelopeValidationError("unsafe public persistence text")
        return
    if type(value) is bytes:
        try:
            decoded = value.decode("utf-8", errors="strict")
        except UnicodeDecodeError:
            return
        _assert_configured_canaries(decoded, matcher)
        return
    if isinstance(value, Mapping):
        for key, item in value.items():
            _assert_configured_canaries(key, matcher)
            _assert_configured_canaries(item, matcher)
        return
    if isinstance(value, (list, tuple)):
        for item in value:
            _assert_configured_canaries(item, matcher)


def _assert_public_code(value: str) -> None:
    if type(value) is not str or _PUBLIC_CODE_RE.fullmatch(value) is None:
        raise EnvelopeValidationError("unsafe public persistence code")
    lowered = value.casefold()
    if any(token in lowered for token in (
        "private", "profile", "raw", "canary", "secret", "opaque", "identity",
        "military", "internal",
    )):
        raise EnvelopeValidationError("unsafe public persistence code")
