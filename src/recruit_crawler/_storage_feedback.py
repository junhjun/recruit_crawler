from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, Optional, TypedDict

from ._storage_core import connect
from .user_context import _reject_private_text


class FeedbackRecord(TypedDict):
    event_id: str
    recommendation_id: str
    run_id: str
    posting_key: str
    source_id: str
    source_posting_id: str | None
    source_url: str | None
    verdict: str
    reason: str
    movement: str
    created_at: str


class UnknownRecommendationError(ValueError):
    def __init__(self, recommendation_id: str) -> None:
        super().__init__(f"unknown recommendation_id: {recommendation_id}")


class FeedbackConflictError(ValueError):
    pass


class PendingRecommendationError(UnknownRecommendationError):
    """Feedback cannot be attached while its run is pending publication."""
    pass


def add_feedback_event(
    db_path: Path,
    *,
    recommendation_id: str,
    verdict: str,
    reason: str,
    movement: str = "same",
    created_at: Optional[str] = None,
    configured_canaries: Iterable[str] = (),
) -> str:
    _reject_private_text(reason)
    try:
        canary_values = (configured_canaries,) if isinstance(configured_canaries, str) else tuple(configured_canaries)
    except TypeError:
        canary_values = configured_canaries
    from .storage import (
        _assert_configured_canaries,
        _assert_public_text,
        _configured_canary_matcher,
        _PUBLIC_FEEDBACK_MOVEMENTS,
        _PUBLIC_FEEDBACK_VERDICTS,
    )
    _assert_public_text(recommendation_id)
    _assert_public_text(reason)
    _assert_public_text(verdict)
    _assert_public_text(movement)
    if verdict not in _PUBLIC_FEEDBACK_VERDICTS:
        raise ValueError("invalid feedback verdict")
    if movement not in _PUBLIC_FEEDBACK_MOVEMENTS:
        raise ValueError("invalid feedback movement")
    created = created_at if created_at is not None else datetime.now(timezone.utc).isoformat()
    try:
        created = _canonical_feedback_timestamp(created)
    except (TypeError, ValueError):
        raise ValueError("created_at must be a canonical timezone-aware ISO timestamp") from None
    _assert_public_text(created)
    matcher = _configured_canary_matcher(canary_values)
    _assert_configured_canaries(
        {
            "recommendation_id": recommendation_id,
            "verdict": verdict,
            "reason": reason,
            "movement": movement,
            "created_at": created,
        },
        matcher,
    )
    event_id = _feedback_event_id(recommendation_id, verdict, reason, movement, created)
    from .storage import _pending_run_key
    connection = connect(Path(db_path), configured_canaries=canary_values)
    try:
        connection.execute("BEGIN IMMEDIATE")
        row = connection.execute("""
            SELECT recommendation_id, run_id, posting_key, source_id, source_posting_id, source_url
            FROM recommendations WHERE recommendation_id=?
        """, (recommendation_id,)).fetchone()
        if row is None:
            raise UnknownRecommendationError(recommendation_id)
        pending = connection.execute(
            "SELECT 1 FROM schema_metadata WHERE key = ?",
            (_pending_run_key(row["run_id"]),),
        ).fetchone()
        if pending is not None:
            raise PendingRecommendationError(recommendation_id)
        values = (
            event_id, row["recommendation_id"], row["run_id"], row["posting_key"], row["source_id"],
            row["source_posting_id"], row["source_url"], verdict, reason, movement, created, 3,
        )
        try:
            connection.execute("""
                INSERT INTO feedback_events(
                    event_id, recommendation_id, run_id, posting_key, source_id, source_posting_id,
                    source_url, verdict, reason, movement, created_at, record_schema_version
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
            """, values)
            connection.commit()
        except Exception as commit_error:
            try:
                connection.rollback()
            except Exception:
                raise commit_error
            try:
                existing = _find_committed_feedback_event(
                    Path(db_path), event_id, configured_canaries=canary_values,
                )
            except Exception:
                raise commit_error
            if existing is None:
                raise commit_error
            comparable = (
                "event_id", "recommendation_id", "run_id", "posting_key", "source_id",
                "source_posting_id", "source_url", "verdict", "reason", "movement",
                "created_at", "record_schema_version",
            )
            if any(existing[key] != value for key, value in zip(comparable, values)):
                raise FeedbackConflictError(f"conflicting feedback event: {event_id}")
        return event_id
    finally:
        connection.close()


def _find_committed_feedback_event(
    db_path: Path,
    event_id: str,
    *,
    configured_canaries: Iterable[str],
):
    connection = connect(db_path, configured_canaries=configured_canaries)
    try:
        connection.commit()
        existing = connection.execute(
            """
            SELECT feedback_events.*
            FROM feedback_events
            WHERE event_id=?
              AND NOT EXISTS (
                  SELECT 1 FROM schema_metadata
                  WHERE key = 'scheduled_run_pending:' || feedback_events.run_id
              )
            """,
            (event_id,),
        ).fetchone()
        connection.commit()
        return existing
    finally:
        connection.close()


def export_feedback_events(db_path: Path, *, configured_canaries: Iterable[str] = ()) -> list[FeedbackRecord]:
    from .storage import (
        StorageSchemaError,
        _PUBLIC_FEEDBACK_MOVEMENTS,
        _PUBLIC_FEEDBACK_VERDICTS,
        _export_canary_matcher,
        _assert_public_text,
        _validated_recommendation_export,
        _validated_run_context,
        _pending_run_ids,
    )

    matcher = _export_canary_matcher(configured_canaries)
    try:
        with connect(Path(db_path), configured_canaries=matcher) as connection:
            pending_run_ids = _pending_run_ids(connection)
            runs = _validated_run_context(connection)
            recommendation_rows = connection.execute("""
                SELECT recommendation_id, posting_key, run_id, source_id, source_url, source_posting_id,
                       title, company, location, deadline, score, recommendation, verdict,
                       matched_evidence_json, gaps_json, risks_json, final_disposition, reason_codes_json,
                       source_detail_quality, record_schema_version
                FROM recommendations
            """).fetchall()
            recommendations: dict[str, dict[str, object]] = {}
            recommendation_counts: dict[str, int] = {}
            for row in recommendation_rows:
                if row["run_id"] in pending_run_ids:
                    continue
                item = _validated_recommendation_export(row, runs)
                if item["recommendation_id"] in recommendations:
                    raise StorageSchemaError("database contains duplicate recommendations")
                recommendations[item["recommendation_id"]] = item
                run_id = item["run_id"]
                recommendation_counts[run_id] = recommendation_counts.get(run_id, 0) + 1
            if any(runs[run_id]["ranked_count"] != count for run_id, count in recommendation_counts.items()):
                raise StorageSchemaError("database recommendation count disagrees with run")
            if any(run_id not in recommendation_counts and run["ranked_count"] != 0 for run_id, run in runs.items()):
                raise StorageSchemaError("database recommendation count disagrees with run")
            rows = connection.execute("""
                SELECT event_id, recommendation_id, run_id, posting_key, source_id,
                       source_posting_id, source_url, verdict, reason, movement, created_at,
                       record_schema_version
                FROM feedback_events ORDER BY created_at ASC, event_id ASC
            """).fetchall()
            result: list[FeedbackRecord] = []
            for row in rows:
                item = dict(row)
                if item["run_id"] in pending_run_ids:
                    continue
                if item["record_schema_version"] != 3:
                    raise StorageSchemaError("database contains an invalid feedback record")
                rec = recommendations.get(item["recommendation_id"])
                if rec is None or item["run_id"] != rec["run_id"]:
                    raise StorageSchemaError("database contains orphan feedback")
                for key in ("event_id", "recommendation_id", "run_id", "posting_key", "source_id", "created_at"):
                    _assert_public_text(item[key])
                if item["posting_key"] != rec["posting_key"] or item["source_id"] != rec["source_id"]:
                    raise StorageSchemaError("database feedback identity does not match recommendation")
                if item["source_posting_id"] != rec["source_posting_id"] or item["source_url"] != rec["source_url"]:
                    raise StorageSchemaError("database feedback source does not match recommendation")
                if item["verdict"] not in _PUBLIC_FEEDBACK_VERDICTS:
                    raise StorageSchemaError("database contains an invalid feedback verdict")
                if item["movement"] not in _PUBLIC_FEEDBACK_MOVEMENTS:
                    raise StorageSchemaError("database contains an invalid feedback movement")
                _assert_public_text(item["verdict"])
                _assert_public_text(item["reason"])
                _assert_public_text(item["movement"])
                try:
                    created_at = _canonical_feedback_timestamp(item["created_at"])
                except (TypeError, ValueError):
                    raise StorageSchemaError("database contains an invalid feedback timestamp") from None
                expected_event_id = _feedback_event_id(
                    item["recommendation_id"], item["verdict"], item["reason"],
                    item["movement"], created_at,
                )
                if item["event_id"] != expected_event_id and not item["event_id"].startswith("legacy-"):
                    raise StorageSchemaError("database contains an invalid feedback identity")
                result.append({
                    "event_id": item["event_id"],
                    "recommendation_id": item["recommendation_id"],
                    "run_id": item["run_id"],
                    "posting_key": item["posting_key"],
                    "source_id": item["source_id"],
                    "source_posting_id": item["source_posting_id"],
                    "source_url": item["source_url"],
                    "verdict": item["verdict"],
                    "reason": item["reason"],
                    "movement": item["movement"],
                    "created_at": item["created_at"],
                })
            return result
    except StorageSchemaError:
        raise
    except Exception:
        raise StorageSchemaError("database contains unsafe or malformed public data") from None


def _feedback_event_id(recommendation_id: str, verdict: str, reason: str, movement: str, created_at: str) -> str:
    payload = json.dumps({
        "recommendation_id": recommendation_id, "verdict": verdict, "reason": reason,
        "movement": movement, "created_at": created_at,
    }, ensure_ascii=False, separators=(",", ":"), sort_keys=False)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:32]
def _canonical_feedback_timestamp(value: object) -> str:
    if type(value) is not str:
        raise ValueError
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        raise ValueError from None
    if parsed.tzinfo is None or parsed.utcoffset() is None or parsed.isoformat() != value:
        raise ValueError
    return value
