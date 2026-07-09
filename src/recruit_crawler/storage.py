from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, Optional, TypedDict, Union

from ._storage_core import SCHEMA_VERSION, connect, initialize, stable_digest
from ._storage_feedback import add_feedback_event, export_feedback_events
from .schemas import FitAssessment, RunSummary


JsonScalar = Union[str, int, float, bool, None]
JsonValue = Union[JsonScalar, list["JsonValue"], dict[str, "JsonValue"]]


class RunIdentityRecord(TypedDict):
    command_mode: str
    run_date: str
    source_config_hash: str
    profile_config_hash: str
    run_id: str


class StorageGate(TypedDict, total=False):
    run_identity: RunIdentityRecord
    status: str
    context_status: str
    report_generated: bool
    candidates_collected: int
    sources: list[dict[str, JsonValue]]


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
    run_id: str
    source_id: str
    source_url: str
    source_posting_id: str | None
    title: str
    company: str
    location: str
    deadline: str | None
    score: int
    recommendation: str
    verdict: str


def persist_scheduled_run(
    db_path: Path,
    *,
    gate: StorageGate,
    summary: Optional[RunSummary],
    ranked: Iterable[FitAssessment],
) -> None:
    now = datetime.now(timezone.utc).isoformat()
    identity = gate["run_identity"]
    run_id = identity["run_id"]
    report_path = str(summary.report_path) if summary else None
    ranked_items = list(ranked)
    with connect(db_path) as connection:
        connection.execute(
            """
            INSERT INTO runs(
                run_id, command_mode, run_date, source_config_hash, profile_config_hash,
                status, context_status, report_generated, report_path, candidates_collected,
                ranked_count, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(run_id) DO UPDATE SET
                status=excluded.status,
                context_status=excluded.context_status,
                report_generated=excluded.report_generated,
                report_path=excluded.report_path,
                candidates_collected=excluded.candidates_collected,
                ranked_count=excluded.ranked_count,
                updated_at=excluded.updated_at
            """,
            (
                run_id,
                identity["command_mode"],
                identity["run_date"],
                identity["source_config_hash"],
                identity["profile_config_hash"],
                gate["status"],
                gate["context_status"],
                1 if gate["report_generated"] else 0,
                report_path,
                int(gate.get("candidates_collected", 0)),
                len(ranked_items),
                now,
                now,
            ),
        )
        connection.execute("DELETE FROM source_attempts WHERE run_id = ?", (run_id,))
        for source in gate.get("sources", []):
            connection.execute(
                """
                INSERT INTO source_attempts(
                    run_id, source_id, attempted, candidate_count, error_count, errors_json
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    run_id,
                    source["source_id"],
                    1 if source.get("attempted") else 0,
                    int(source.get("candidate_count", 0)),
                    int(source.get("error_count", 0)),
                    json.dumps(source.get("errors", []), ensure_ascii=False),
                ),
            )
        connection.execute("DELETE FROM recommendations WHERE run_id = ?", (run_id,))
        for item in ranked_items:
            snapshot = item.snapshot
            recommendation_id = _recommendation_id(run_id, snapshot.source_id, snapshot.source_url, snapshot.source_posting_id)
            connection.execute(
                """
                INSERT INTO recommendations(
                    recommendation_id, run_id, source_id, source_url, source_posting_id,
                    title, company, location, deadline, score, recommendation, verdict,
                    matched_evidence_json, gaps_json, risks_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    recommendation_id,
                    run_id,
                    snapshot.source_id,
                    snapshot.source_url,
                    snapshot.source_posting_id,
                    snapshot.title,
                    snapshot.company,
                    snapshot.location,
                    snapshot.deadline.isoformat() if snapshot.deadline else None,
                    item.score,
                    item.recommendation,
                    item.verdict,
                    json.dumps(item.matched_evidence, ensure_ascii=False),
                    json.dumps(item.gaps, ensure_ascii=False),
                    json.dumps(item.risks, ensure_ascii=False),
                ),
            )
        connection.execute(
            """
            INSERT INTO quality_gates(run_id, status, context_status, gate_json, updated_at)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(run_id) DO UPDATE SET
                status=excluded.status,
                context_status=excluded.context_status,
                gate_json=excluded.gate_json,
                updated_at=excluded.updated_at
            """,
            (
                run_id,
                gate["status"],
                gate["context_status"],
                json.dumps(gate, ensure_ascii=False, sort_keys=True),
                now,
            ),
        )
        connection.commit()


def export_runs(db_path: Path) -> list[RunRecord]:
    with connect(db_path) as connection:
        rows = connection.execute(
            """
            SELECT run_id, command_mode, run_date, status, context_status, report_generated,
                   report_path, candidates_collected, ranked_count, created_at, updated_at
            FROM runs
            ORDER BY run_date DESC, updated_at DESC
            """
        ).fetchall()
        return [dict(row) for row in rows]


def export_recommendations(db_path: Path) -> list[RecommendationRecord]:
    with connect(db_path) as connection:
        rows = connection.execute(
            """
            SELECT recommendation_id, run_id, source_id, source_url, source_posting_id,
                   title, company, location, deadline, score, recommendation, verdict
            FROM recommendations
            ORDER BY run_id ASC, score DESC, recommendation_id ASC
            """
        ).fetchall()
        return [dict(row) for row in rows]


def _recommendation_id(run_id: str, source_id: str, source_url: str, source_posting_id: Optional[str]) -> str:
    return stable_digest(
        {
            "run_id": run_id,
            "source_id": source_id,
            "source_url": source_url,
            "source_posting_id": source_posting_id,
        }
    )[:32]
