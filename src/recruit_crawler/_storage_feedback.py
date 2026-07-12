from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Optional, TypedDict

from ._storage_core import connect, stable_digest
from .user_context import _reject_private_text


class FeedbackRecord(TypedDict):
    event_id: str
    recommendation_id: str
    run_id: str
    posting_key: str
    source_id: str
    source_url: str
    source_posting_id: str | None
    verdict: str
    reason: str
    movement: str
    created_at: str


class UnknownRecommendationError(ValueError):
    def __init__(self, recommendation_id: str) -> None:
        super().__init__(f"unknown recommendation_id: {recommendation_id}")


def add_feedback_event(
    db_path: Path,
    *,
    recommendation_id: str,
    verdict: str,
    reason: str,
    movement: str = "same",
    created_at: Optional[str] = None,
) -> str:
    _reject_private_text(reason)
    created = created_at or datetime.now(timezone.utc).isoformat()
    with connect(db_path) as connection:
        row = connection.execute(
            """
            SELECT run_id, source_id, source_posting_id, source_url
            FROM recommendations
            WHERE recommendation_id = ?
            """,
            (recommendation_id,),
        ).fetchone()
        if row is None:
            raise UnknownRecommendationError(recommendation_id)
        posting_key = _posting_key(row["source_id"], row["source_url"], row["source_posting_id"])
        event_id = _feedback_event_id(recommendation_id, verdict, reason, movement, created)
        connection.execute(
            """
            INSERT OR REPLACE INTO feedback_events(
                event_id, recommendation_id, run_id, posting_key, source_id,
                source_posting_id, source_url, verdict, reason, movement, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                event_id,
                recommendation_id,
                row["run_id"],
                posting_key,
                row["source_id"],
                row["source_posting_id"],
                row["source_url"],
                verdict,
                reason,
                movement,
                created,
            ),
        )
        connection.commit()
        return event_id


def export_feedback_events(db_path: Path) -> list[FeedbackRecord]:
    with connect(db_path) as connection:
        rows = connection.execute(
            """
            SELECT event_id, recommendation_id, run_id, posting_key, source_id,
                   source_posting_id, source_url, verdict, reason, movement, created_at
            FROM feedback_events
            ORDER BY created_at ASC, event_id ASC
            """
        ).fetchall()
        return [dict(row) for row in rows]


def _posting_key(source_id: str, source_url: str, source_posting_id: Optional[str]) -> str:
    return stable_digest(
        {
            "source_id": source_id,
            "source_url": source_url,
            "source_posting_id": source_posting_id,
        }
    )[:32]


def _feedback_event_id(recommendation_id: str, verdict: str, reason: str, movement: str, created_at: str) -> str:
    return stable_digest(
        {
            "recommendation_id": recommendation_id,
            "verdict": verdict,
            "reason": reason,
            "movement": movement,
            "created_at": created_at,
        }
    )[:32]
