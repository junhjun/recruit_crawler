from __future__ import annotations

import multiprocessing
import os
import signal
import time
import sys
import math
from dataclasses import dataclass, replace
from datetime import date
from typing import Any, Iterable, Optional

from .config import ConfigError, load_config
from .dedupe import dedupe_snapshots_v2
from .identity import (
    CandidateRejected,
    canonicalize_url,
    identity_basis,
    normalize_identifier,
    normalize_source_ids,
    normalize_source_id,
    posting_key,
    recommendation_id,
)
from .jd_parser import normalize_and_parse_candidate_v2
from .scorer import assess_snapshot_v2
from .schemas import (
    AppConfig,
    AssessmentV2,
    CandidateDetailIssueCodeV2,
    CandidateDetailIssueV2,
    CandidateV2,
    CommandMode,
    PipelineResultV2,
    PipelineResultV4,
    PostingCandidate,
    SourceMetricV2,
    SourceMetricV4,
    SourceExecutionOutcomeV1,
    CollectionResultV1,
    SOURCE_EXECUTION_OUTCOME_ERROR_CODES_V1,
    SOURCE_EXECUTION_OUTCOME_STATUSES_V1,
    source_execution_outcome_v1_is_consistent,
)
from .sources.base import build_source_adapter
from .user_context import missing_context_fields


LOCAL_ACCESS_MODES = {"fixture", "manual"}


def _assert_no_real_sources(config: AppConfig) -> None:
    blocked = [
        source.source_id
        for source in config.sources
        if source.enabled and source.access_mode not in LOCAL_ACCESS_MODES
    ]
    if blocked:
        raise ConfigError(
            "dry-run refuses real source adapters even when passed a preloaded config: "
            + ", ".join(blocked)
        )


def _safe_source_id(value: Any) -> str:
    try:
        from .identity import normalize_source_id

        return normalize_source_id(value)
    except (TypeError, ValueError):
        return str(value).strip().lower() or "unknown"

def _candidate_detail_quality(
    candidate: PostingCandidate | CandidateV2,
    source_quality: str,
) -> str:
    """Return ingress detail quality unless the source policy is stricter."""
    if source_quality == "manual_only":
        return "manual_only"
    raw_jd = getattr(candidate, "raw_jd", None)
    if not isinstance(raw_jd, dict):
        return "verified"
    detail_quality = raw_jd.get("detail_quality", "verified")
    return detail_quality if detail_quality in {"verified", "manual_only"} else "verified"
_DETAIL_ISSUE_CODES = frozenset(
    code.value for code in CandidateDetailIssueCodeV2
)
_SOURCE_ERROR_CODES = frozenset(
    {
        "collection_error",
        "collection_failed",
        "source_timeout",
        "aggregate_budget_exhausted",
        "invalid_candidate",
        *_DETAIL_ISSUE_CODES,
    }
)


def _source_error_code(value: Any) -> str:
    """Map caller-provided source errors to the fixed public vocabulary."""
    if not isinstance(value, str):
        return "collection_error"
    _prefix, separator, detail = value.partition(":")
    candidate = detail.strip() if separator else value.strip()
    return candidate if candidate in _SOURCE_ERROR_CODES else "collection_error"


def _candidate_identity_keys(candidate: CandidateV2) -> tuple[tuple[str, str, str], ...]:
    """Return the normalized identity basis used by deduplication."""
    basis = identity_basis(candidate)
    return (
        (
            str(basis["kind"]),
            str(basis["source_id"]),
            str(basis["value"]),
        ),
    )


def _normalize_detail_issue(issue: Any) -> tuple[str, tuple[str, str, str] | None] | None:
    """Normalize typed adapter findings to a fixed code and identity key."""
    code = getattr(issue, "code", None)
    code = getattr(code, "value", code)
    if str(code) not in _DETAIL_ISSUE_CODES:
        return None
    try:
        source_id = normalize_source_id(getattr(issue, "source_id", None))
    except (TypeError, ValueError):
        return (str(code), None)
    source_posting_id = normalize_identifier(getattr(issue, "source_posting_id", None))
    source_url = canonicalize_url(getattr(issue, "source_url", None))
    if source_posting_id:
        return str(code), ("source_posting_id", source_id, source_posting_id.casefold())
    if source_url:
        return str(code), ("canonical_url", source_id, source_url)
    return str(code), None


def _source_issue_id(issue: Any) -> str:
    return _safe_source_id(getattr(issue, "source_id", "unknown"))
def _assessment_order_key(item: AssessmentV2) -> tuple[str, str]:
    """Order assessments using public identity fields only."""
    return (
        item.source_id,
        item.source_posting_id or item.source_url or f"{item.title}\0{item.company}",
    )



def build_pipeline_result_v2(
    config: AppConfig,
    run_date: date,
    candidates: Iterable[PostingCandidate | CandidateV2],
    *,
    command_mode: str = CommandMode.DRY_RUN.value,
    candidate_detail_issues: Optional[Iterable[CandidateDetailIssueV2]] = None,
    run_id: Optional[str] = None,
    sources_attempted: Optional[Iterable[str]] = None,
    source_errors: Optional[Iterable[str]] = None,
    source_detail_quality: Optional[dict[str, str]] = None,
) -> PipelineResultV2:
    """Build one frozen v2 result from an already-collected candidate stream.

    This function performs no collection, ranking truncation, report writing,
    or gate construction. Every valid candidate is assessed after identity
    dedupe; rejected ingress records are represented only in source metrics.
    """
    if command_mode not in {mode.value for mode in CommandMode}:
        raise ValueError(f"unsupported command_mode: {command_mode}")
    raw_candidates = tuple(candidates)
    raw_errors = tuple(source_errors or ())
    attempted = set(normalize_source_ids(sources_attempted or ()))
    records: list[tuple[CandidateV2, Any]] = []
    metric_state: dict[str, dict[str, Any]] = {}
    rejected_count = 0

    def state(source_id: str) -> dict[str, Any]:
        return metric_state.setdefault(
            source_id,
            {
                "attempted": True,
                "accepted": 0,
                "rejected": 0,
                "duplicate": 0,
                "changed": 0,
                "emptied": 0,
                "verified": 0,
                "manual": 0,
                "errors": set(),
            },
        )
    detail_issues = tuple(candidate_detail_issues or ())
    normalized_issue_rows: list[tuple[str, str, tuple[str, str, str] | None]] = []
    for issue in detail_issues:
        normalized_issue = _normalize_detail_issue(issue)
        source_id = _source_issue_id(issue)
        if normalized_issue is None:
            state(source_id)["errors"].add("detail_issue_invalid")
            continue
        code, issue_key = normalized_issue
        normalized_issue_rows.append((source_id, code, issue_key))

    for raw_error in raw_errors:
        prefix, separator, _detail = (
            raw_error.partition(":") if isinstance(raw_error, str) else ("", "", "")
        )
        source_id = _safe_source_id(prefix) if separator else None
        code = _source_error_code(raw_error)
        targets = (source_id,) if source_id in attempted else tuple(attempted)
        for target in targets:
            state(target)["errors"].add(code)

    for candidate in raw_candidates:
        raw_source = getattr(candidate, "source_id", "unknown")
        source_hint = _safe_source_id(raw_source)
        try:
            source_quality = (source_detail_quality or {}).get(source_hint, "verified")
            normalized, snapshot = normalize_and_parse_candidate_v2(
                candidate,
                detail_quality=_candidate_detail_quality(candidate, source_quality),
            )
        except CandidateRejected:
            rejected_count += 1
            item = state(source_hint)
            item["rejected"] += 1
            item["errors"].add("invalid_candidate")
            continue
        except (TypeError, ValueError):
            rejected_count += 1
            item = state(source_hint)
            item["rejected"] += 1
            item["errors"].add("invalid_candidate")
            continue
        source_id = normalized.candidate.source_id
        item = state(source_id)
        item["accepted"] += 1
        item["changed"] += normalized.info.changed_fields
        item["emptied"] += normalized.info.emptied_fields
        records.append((normalized.candidate, snapshot))

    candidate_keys: dict[tuple[str, str, str], set[str]] = {}
    for candidate, _snapshot in records:
        for identity_key in _candidate_identity_keys(candidate):
            candidate_keys.setdefault(identity_key, set()).add(posting_key(candidate))
    issue_identities: dict[str, set[str]] = {}
    for source_id, code, issue_key in normalized_issue_rows:
        matched = candidate_keys.get(issue_key, set()) if issue_key is not None else set()
        if not matched:
            state(source_id)["errors"].add(code)
            continue
        issue_identities.setdefault(code, set()).update(matched)

    deduped, duplicates_removed = dedupe_snapshots_v2(records)
    survivor_ids = {id(candidate) for candidate, _snapshot in deduped}
    for candidate, _snapshot in records:
        if id(candidate) not in survivor_ids:
            state(candidate.source_id)["duplicate"] += 1

    issue_keys = set().union(*issue_identities.values()) if issue_identities else set()
    adjusted: list[tuple[CandidateV2, Any]] = []
    for candidate, snapshot in deduped:
        if posting_key(candidate) in issue_keys:
            snapshot = replace(snapshot, detail_quality="manual_only")
        item = state(candidate.source_id)
        if snapshot.detail_quality == "manual_only":
            item["manual"] += 1
        else:
            item["verified"] += 1
        adjusted.append((candidate, snapshot))
    deduped = adjusted

    effective_run_id = run_id or f"{command_mode}:{run_date.isoformat()}"
    assessments: list[AssessmentV2] = []
    for candidate, snapshot in deduped:
        key = posting_key(snapshot)
        assessments.append(
            assess_snapshot_v2(
                snapshot,
                config_or_context=config,
                recommendation_id=recommendation_id(snapshot, effective_run_id),
                posting_key=key,
                opaque_identity=key,
                run_date=run_date,
            )
        )
    assessments.sort(key=_assessment_order_key)

    all_source_ids = attempted | set(metric_state)
    metrics = []
    for source_id in sorted(all_source_ids):
        item = state(source_id)
        metrics.append(
            SourceMetricV2(
                source_id=source_id,
                attempted=bool(item["attempted"]),
                accepted_count=item["accepted"],
                rejected_count=item["rejected"],
                duplicate_count=item["duplicate"],
                normalized_changed_field_count=item["changed"],
                normalized_emptied_field_count=item["emptied"],
                verified_count=item["verified"],
                manual_only_count=item["manual"],
                error_codes=tuple(sorted(item["errors"])),
                duration_ms=0,
            )
        )
    return PipelineResultV2(
        schema_version=2,
        command_mode=command_mode,
        run_date=run_date,
        all_assessments=tuple(assessments),
        source_metrics=tuple(metrics),
        duplicates_removed=duplicates_removed,
        collected_count=len(raw_candidates),
        source_rejected_count=rejected_count,
        top_n=int(config.top_n),
        manual_review_n=int(config.manual_review_n),
    )
def _closed_source_outcome(value: Any) -> SourceExecutionOutcomeV1:
    """Copy only the allowlisted, closed execution outcome fields."""

    if (
        isinstance(value, SourceExecutionOutcomeV1)
        and source_execution_outcome_v1_is_consistent(value)
        and value.source_id == _safe_source_id(value.source_id)
    ):
        return value

    source_id = _safe_source_id(getattr(value, "source_id", "unknown-source"))
    attempted = getattr(value, "attempted", False)
    completed = getattr(value, "completed", False)
    status = getattr(value, "status", "collection_error")
    error_code = getattr(value, "error_code", None)
    elapsed_ms = getattr(value, "elapsed_ms", 0)
    if type(attempted) is not bool:
        attempted = False
    if type(completed) is not bool:
        completed = False
    if type(elapsed_ms) is not int or elapsed_ms < 0:
        elapsed_ms = 0
    if status not in SOURCE_EXECUTION_OUTCOME_STATUSES_V1:
        status = "collection_error"
    if status == "success":
        error_code = None
    elif error_code not in SOURCE_EXECUTION_OUTCOME_ERROR_CODES_V1:
        error_code = status
    return SourceExecutionOutcomeV1(
        source_id=source_id,
        attempted=attempted,
        completed=completed,
        status=status,
        error_code=error_code,
        elapsed_ms=elapsed_ms,
    )


def build_pipeline_result_v4(
    config: AppConfig,
    run_date: date,
    candidates: Iterable[PostingCandidate | CandidateV2],
    *,
    source_outcomes: Iterable[SourceExecutionOutcomeV1],
    command_mode: str = CommandMode.DRY_RUN.value,
    candidate_detail_issues: Optional[Iterable[CandidateDetailIssueV2]] = None,
    run_id: Optional[str] = None,
    sources_attempted: Optional[Iterable[str]] = None,
    source_errors: Optional[Iterable[str]] = None,
    source_detail_quality: Optional[dict[str, str]] = None,
) -> PipelineResultV4:
    """Build V4 without inferring any source execution outcome."""

    raw_outcomes = tuple(_closed_source_outcome(item) for item in source_outcomes)
    if len({item.source_id for item in raw_outcomes}) != len(raw_outcomes):
        raise ValueError("duplicate source execution outcomes")
    if sources_attempted is None:
        sources_attempted = tuple(item.source_id for item in raw_outcomes)
    result_v2 = build_pipeline_result_v2(
        config,
        run_date,
        candidates,
        command_mode=command_mode,
        candidate_detail_issues=candidate_detail_issues,
        run_id=run_id,
        sources_attempted=sources_attempted,
        source_errors=source_errors,
        source_detail_quality=source_detail_quality,
    )
    outcome_by_source = {item.source_id: item for item in raw_outcomes}
    metrics: list[SourceMetricV4] = []
    for metric in result_v2.source_metrics:
        outcome = outcome_by_source.get(metric.source_id)
        if outcome is None:
            raise ValueError("pipeline V4 source metrics/outcomes axis mismatch")
        error_codes = set(metric.error_codes)
        if outcome.status != "success":
            error_codes.add(outcome.error_code or outcome.status)
        metrics.append(
            SourceMetricV4(
                source_id=metric.source_id,
                attempted=metric.attempted,
                accepted_count=metric.accepted_count,
                rejected_count=metric.rejected_count,
                duplicate_count=metric.duplicate_count,
                normalized_changed_field_count=metric.normalized_changed_field_count,
                normalized_emptied_field_count=metric.normalized_emptied_field_count,
                verified_count=metric.verified_count,
                manual_only_count=metric.manual_only_count,
                error_codes=tuple(sorted(error_codes)),
                duration_ms=outcome.elapsed_ms,
                outcome=outcome,
            )
        )
    if set(outcome_by_source) != {metric.source_id for metric in result_v2.source_metrics}:
        raise ValueError("pipeline V4 source outcomes/metrics axis mismatch")
    return PipelineResultV4(
        schema_version=4,
        command_mode=result_v2.command_mode,
        run_date=result_v2.run_date,
        all_assessments=result_v2.all_assessments,
        source_metrics=tuple(metrics),
        duplicates_removed=result_v2.duplicates_removed,
        collected_count=result_v2.collected_count,
        source_rejected_count=result_v2.source_rejected_count,
        top_n=result_v2.top_n,
        manual_review_n=result_v2.manual_review_n,
        source_outcomes=raw_outcomes,
    )


def collection_result_v1(batch: Any) -> CollectionResultV1:
    """Project coordinator internals to a closed, payload-free result."""

    outcomes = tuple(_closed_source_outcome(item) for item in getattr(batch, "outcomes", ()))
    return CollectionResultV1(
        candidates=tuple(getattr(batch, "candidates", ())),
        detail_issues=tuple(getattr(batch, "issues", ())),
        source_outcomes=outcomes,
    )


def build_pipeline_result_v4_from_collection(
    config: AppConfig,
    run_date: date,
    batch: Any,
    *,
    command_mode: str = CommandMode.LIVE_RUN.value,
    run_id: Optional[str] = None,
) -> PipelineResultV4:
    projected = collection_result_v1(batch)
    return build_pipeline_result_v4(
        config,
        run_date,
        projected.candidates,
        source_outcomes=projected.source_outcomes,
        command_mode=command_mode,
        candidate_detail_issues=projected.detail_issues,
        sources_attempted=tuple(item.source_id for item in projected.source_outcomes),
        source_errors=tuple(
            f"{item.source_id}: {item.error_code}"
            for item in projected.source_outcomes
            if item.status != "success"
        ),
        run_id=run_id,
    )


# Short integration aliases.
build_pipeline_v2 = build_pipeline_result_v2
build_pipeline_result = build_pipeline_result_v2
build_v2_result = build_pipeline_result_v2
build_result_v2 = build_pipeline_result_v2
build_pipeline_v4 = build_pipeline_result_v4
build_result_v4 = build_pipeline_result_v4


DEFAULT_COLLECTION_BUDGET_SECONDS = 300.0
DEFAULT_SOURCE_BUDGET_SECONDS = 60.0
_COLLECTION_IPC_SCHEMA_VERSION = 1


class SourceCollectionStatus:
    SUCCESS = "success"
    COLLECTION_ERROR = "collection_error"
    COLLECTION_FAILED = "collection_failed"
    SOURCE_TIMEOUT = "source_timeout"
    AGGREGATE_BUDGET_EXHAUSTED = "aggregate_budget_exhausted"


@dataclass(frozen=True, slots=True)
class _SourceCollectionOutcome:
    source_id: str
    attempted: bool
    completed: bool
    status: str
    error_code: Optional[str]
    elapsed_ms: int
    candidates: tuple[PostingCandidate, ...] = ()
    issues: tuple[CandidateDetailIssueV2, ...] = ()
    errors: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class _CollectionBatch:
    outcomes: tuple[_SourceCollectionOutcome, ...]
    candidates: tuple[PostingCandidate, ...]
    issues: tuple[CandidateDetailIssueV2, ...]
    errors: tuple[str, ...]
    @property
    def source_outcomes(self) -> tuple[SourceExecutionOutcomeV1, ...]:
        """Closed outcome projection; no adapter payloads or exception text."""

        return tuple(_closed_source_outcome(item) for item in self.outcomes)


SourceCollectionOutcome = _SourceCollectionOutcome


def _source_worker(
    source: Any,
    fixture_path: Any,
    connection: Any,
    deadline: float,
    adapter_factory: Any,
) -> None:
    """Collect one source after a verified parent/worker PGID handshake."""
    handshake_complete = False
    try:
        os.setpgid(0, 0)
        worker_pid = os.getpid()
        worker_pgid = os.getpgid(worker_pid)
        if worker_pgid != worker_pid:
            raise RuntimeError("worker process group setup failed")
        connection.send(
            {
                "schema_version": _COLLECTION_IPC_SCHEMA_VERSION,
                "kind": "handshake",
                "status": "ready",
                "pid": worker_pid,
                "pgid": worker_pgid,
            }
        )
        command = connection.recv()
        if command != {
            "schema_version": _COLLECTION_IPC_SCHEMA_VERSION,
            "kind": "handshake",
            "command": "release",
        }:
            raise RuntimeError("worker handshake release failed")
        handshake_complete = True
    except Exception:
        if not handshake_complete:
            try:
                connection.send(
                    {
                        "schema_version": _COLLECTION_IPC_SCHEMA_VERSION,
                        "kind": "handshake",
                        "status": "error",
                        "pid": os.getpid(),
                        "pgid": None,
                    }
                )
            except (BrokenPipeError, EOFError, OSError):
                pass
        try:
            connection.close()
        except OSError:
            pass
        return

    try:
        factory = adapter_factory or build_source_adapter
        adapter = factory(source, fixture_path)
        set_deadline = getattr(adapter, "set_collection_deadline", None)
        if callable(set_deadline):
            set_deadline(deadline)
        candidates = tuple(adapter.collect())
        issues = tuple(getattr(adapter, "issues", ()) or ())
        errors = tuple(str(item) for item in (getattr(adapter, "errors", ()) or ()))
        payload = {
            "schema_version": _COLLECTION_IPC_SCHEMA_VERSION,
            "source_id": source.source_id,
            "status": SourceCollectionStatus.SUCCESS,
            "candidates": candidates,
            "issues": issues,
            "errors": errors,
        }
    except Exception as exc:
        status = (
            SourceCollectionStatus.SOURCE_TIMEOUT
            if exc.__class__.__name__ == "SourceBudgetExceeded"
            else (
                SourceCollectionStatus.COLLECTION_FAILED
                if isinstance(exc, RuntimeError)
                else SourceCollectionStatus.COLLECTION_ERROR
            )
        )
        payload = {
            "schema_version": _COLLECTION_IPC_SCHEMA_VERSION,
            "source_id": getattr(source, "source_id", ""),
            "status": status,
            "candidates": (),
            "issues": (),
            "errors": (),
        }
    try:
        connection.send(payload)
    except (BrokenPipeError, EOFError, OSError):
        pass
    finally:
        connection.close()


@dataclass(slots=True)
class _WorkerState:
    source: Any
    process: Any
    connection: Any
    started_at: float
    deadline: float
    pid: int = 0
    pgid: Optional[int] = None

class SourceCollectionCoordinator:
    """Run enabled source adapters in isolated POSIX process groups."""

    def __init__(
        self,
        config: AppConfig,
        *,
        total_budget_seconds: Optional[float] = None,
        per_source_budget_seconds: Optional[float] = None,
        source_budget_seconds: Optional[float] = None,
        per_source_cap_seconds: Optional[float] = None,
        adapter_factory: Any = None,
        monotonic: Any = time.monotonic,
        runtime_context: Any = None,
        hard_deadline: Optional[float] = None,
    ) -> None:
        if runtime_context is not None:
            context_monotonic = getattr(runtime_context, "monotonic", None)
            if callable(context_monotonic):
                monotonic = context_monotonic
            if hard_deadline is None:
                hard_deadline = getattr(
                    runtime_context,
                    "hard_deadline",
                    getattr(runtime_context, "deadline", None),
                )
        self.config = config
        self.total_budget_seconds = self._positive_budget(
            total_budget_seconds
            if total_budget_seconds is not None
            else getattr(config, "collection_budget_seconds", DEFAULT_COLLECTION_BUDGET_SECONDS),
            DEFAULT_COLLECTION_BUDGET_SECONDS,
        )
        self.per_source_budget_seconds = self._positive_budget(
            per_source_budget_seconds
            if per_source_budget_seconds is not None
            else source_budget_seconds,
            None,
        )
        if per_source_cap_seconds is not None:
            self.per_source_budget_seconds = self._positive_budget(per_source_cap_seconds, None)
        self.adapter_factory = adapter_factory
        self.monotonic = monotonic
        if hard_deadline is None:
            self.hard_deadline: Optional[float] = None
        else:
            try:
                parsed_deadline = float(hard_deadline)
            except (TypeError, ValueError):
                raise ValueError("hard deadline must be finite") from None
            if not math.isfinite(parsed_deadline):
                raise ValueError("hard deadline must be finite")
            self.hard_deadline = parsed_deadline

    @staticmethod
    def _positive_budget(value: Any, fallback: Optional[float]) -> Optional[float]:
        try:
            parsed = float(value)
        except (TypeError, ValueError):
            parsed = fallback
        if parsed is None:
            return None
        return parsed if parsed > 0 else fallback

    def _source_budget(self, source: Any) -> float:
        if self.per_source_budget_seconds is not None:
            return self.per_source_budget_seconds
        options = getattr(source, "options", {}) or {}
        for key in (
            "source_budget_seconds",
            "collection_budget_seconds",
            "collection_timeout_seconds",
            "budget_seconds",
            "source_cap_seconds",
            "per_source_cap_seconds",
        ):
            if key in options:
                value = self._positive_budget(options.get(key), None)
                if value is not None:
                    return value
        return DEFAULT_SOURCE_BUDGET_SECONDS

    @staticmethod
    def _outcome(
        source: Any,
        started_at: float,
        now: float,
        status: str,
        *,
        candidates: tuple[PostingCandidate, ...] = (),
        issues: tuple[CandidateDetailIssueV2, ...] = (),
        errors: tuple[str, ...] = (),
    ) -> _SourceCollectionOutcome:
        return _SourceCollectionOutcome(
            source_id=str(source.source_id),
            attempted=True,
            completed=status == SourceCollectionStatus.SUCCESS,
            status=status,
            error_code=None if status == SourceCollectionStatus.SUCCESS else status,
            elapsed_ms=max(0, int((now - started_at) * 1000)),
            candidates=candidates,
            issues=issues,
            errors=errors,
        )
    def _establish_handshake(
        self,
        state: _WorkerState,
        aggregate_deadline: float,
    ) -> bool:
        handshake_deadline = min(state.deadline, aggregate_deadline)
        while self.monotonic() < handshake_deadline:
            remaining = handshake_deadline - self.monotonic()
            if not state.connection.poll(min(0.02, max(0.0, remaining))):
                continue
            try:
                payload = state.connection.recv()
            except (EOFError, OSError):
                return False
            if not isinstance(payload, dict):
                return False
            if (
                payload.get("schema_version") != _COLLECTION_IPC_SCHEMA_VERSION
                or payload.get("kind") != "handshake"
                or payload.get("status") != "ready"
                or type(payload.get("pid")) is not int
                or type(payload.get("pgid")) is not int
                or payload["pid"] <= 0
                or payload["pgid"] <= 0
                or payload["pid"] != state.process.pid
                or payload["pgid"] != payload["pid"]
            ):
                return False
            try:
                observed_pgid = os.getpgid(payload["pid"])
            except (ProcessLookupError, PermissionError, OSError):
                return False
            if observed_pgid != payload["pgid"]:
                return False
            state.pid = payload["pid"]
            state.pgid = payload["pgid"]
            try:
                state.connection.send(
                    {
                        "schema_version": _COLLECTION_IPC_SCHEMA_VERSION,
                        "kind": "handshake",
                        "command": "release",
                    }
                )
            except (BrokenPipeError, EOFError, OSError):
                state.pgid = None
                return False
            return True
        return False

    @staticmethod
    def _group_exists(pgid: Optional[int]) -> bool:
        if type(pgid) is not int or pgid <= 0:
            return False
        try:
            os.killpg(pgid, 0)
        except ProcessLookupError:
            return False
        except PermissionError:
            return True
        except OSError:
            return False
        return True

    @staticmethod
    def _signal_state(state: _WorkerState, sig: signal.Signals) -> None:
        if type(state.pgid) is int and state.pgid > 0:
            try:
                os.killpg(state.pgid, sig)
            except (ProcessLookupError, PermissionError, OSError):
                pass
            return
        if type(state.pid) is int and state.pid > 0:
            try:
                os.kill(state.pid, sig)
            except (ProcessLookupError, PermissionError, OSError):
                pass

    @classmethod
    def _reap_processes(
        cls,
        states: Iterable[_WorkerState],
        *,
        timeout_seconds: float = 2.0,
        deadline: Optional[float] = None,
        monotonic: Any = time.monotonic,
    ) -> None:
        targets = list(states)
        started_at = monotonic()
        reap_deadline = started_at + max(0.0, timeout_seconds)
        if deadline is not None:
            reap_deadline = min(reap_deadline, deadline)
        for state in targets:
            process = state.process
            while process.is_alive():
                remaining = reap_deadline - monotonic()
                if remaining <= 0:
                    break
                process.join(timeout=min(0.05, remaining))
            if process.is_alive():
                try:
                    process.kill()
                except (AttributeError, ProcessLookupError, PermissionError, OSError):
                    pass
                remaining = reap_deadline - monotonic()
                if remaining > 0:
                    try:
                        process.join(timeout=min(0.2, remaining))
                    except (ProcessLookupError, OSError):
                        pass

    def _stop_processes(self, states: Iterable[_WorkerState]) -> None:
        targets = list(states)
        cleanup_started_at = self.monotonic()
        cleanup_deadline = cleanup_started_at + 4.0
        if self.hard_deadline is not None:
            cleanup_deadline = min(cleanup_deadline, self.hard_deadline)
        for state in targets:
            self._signal_state(state, signal.SIGTERM)
        grace_deadline = min(cleanup_started_at + 2.0, cleanup_deadline)
        now = cleanup_started_at
        while True:
            now = self.monotonic()
            if now >= grace_deadline:
                break
            if not any(
                state.process.is_alive() or self._group_exists(state.pgid)
                for state in targets
            ):
                break
            remaining = grace_deadline - now
            if remaining <= 0:
                break
            time.sleep(min(0.02, remaining))
        for state in targets:
            self._signal_state(state, signal.SIGKILL)
        remaining = max(0.0, cleanup_deadline - now)
        if remaining <= 0:
            return
        self._reap_processes(
            targets,
            timeout_seconds=remaining,
            deadline=cleanup_deadline,
            monotonic=self.monotonic,
        )

    @staticmethod
    def _validate_message(
        source: Any,
        payload: Any,
    ) -> tuple[str, tuple[PostingCandidate, ...], tuple[CandidateDetailIssueV2, ...], tuple[str, ...]]:
        if not isinstance(payload, dict) or type(payload.get("schema_version")) is not int:
            raise ValueError("invalid collection IPC envelope")
        if payload["schema_version"] != _COLLECTION_IPC_SCHEMA_VERSION:
            raise ValueError("unsupported collection IPC envelope")
        if payload.get("source_id") != source.source_id:
            raise ValueError("collection IPC source mismatch")
        status = payload.get("status")
        if status not in {
            SourceCollectionStatus.SUCCESS,
            SourceCollectionStatus.COLLECTION_ERROR,
            SourceCollectionStatus.COLLECTION_FAILED,
            SourceCollectionStatus.SOURCE_TIMEOUT,
        }:
            raise ValueError("invalid collection IPC status")
        candidates = payload.get("candidates")
        issues = payload.get("issues")
        errors = payload.get("errors")
        if not isinstance(candidates, (tuple, list)) or not isinstance(issues, (tuple, list)):
            raise ValueError("invalid collection IPC payload")
        if not isinstance(errors, (tuple, list)) or not all(isinstance(item, str) for item in errors):
            raise ValueError("invalid collection IPC errors")
        candidate_rows = tuple(candidates)
        issue_rows = tuple(issues)
        if status != SourceCollectionStatus.SUCCESS:
            if candidate_rows or issue_rows or errors:
                raise ValueError("failed collection IPC payload is not empty")
            return status, (), (), ()
        if not all(isinstance(item, PostingCandidate) for item in candidate_rows):
            raise ValueError("invalid collection IPC candidate")
        if any(item.source_id != source.source_id for item in candidate_rows):
            raise ValueError("collection IPC candidate source mismatch")
        if not all(isinstance(item, CandidateDetailIssueV2) for item in issue_rows):
            raise ValueError("invalid collection IPC issue")
        return status, candidate_rows, issue_rows, tuple(errors)

    def collect(self) -> _CollectionBatch:
        enabled_sources = tuple(source for source in self.config.sources if source.enabled)
        started = self.monotonic()
        aggregate_deadline = started + self.total_budget_seconds
        states: list[_WorkerState] = []
        outcomes: dict[str, _SourceCollectionOutcome] = {}
        context = None
        for source in enabled_sources:
            launch_time = self.monotonic()
            source_deadline = min(aggregate_deadline, launch_time + self._source_budget(source))
            start_method = (
                "spawn"
                if sys.platform == "darwin"
                and source.access_mode not in LOCAL_ACCESS_MODES
                else "fork"
            )
            context = multiprocessing.get_context(start_method)
            receiver, sender = context.Pipe(duplex=True)
            process = context.Process(
                target=_source_worker,
                args=(source, self.config.fixture_path, sender, source_deadline, self.adapter_factory),
            )
            try:
                process.start()
            except Exception:
                outcomes[source.source_id] = self._outcome(
                    source,
                    launch_time,
                    self.monotonic(),
                    SourceCollectionStatus.COLLECTION_ERROR,
                )
                receiver.close()
                sender.close()
                continue
            sender.close()
            state = _WorkerState(
                source,
                process,
                receiver,
                launch_time,
                source_deadline,
                pid=int(process.pid or 0),
            )
            if not self._establish_handshake(state, aggregate_deadline):
                outcomes[source.source_id] = self._outcome(
                    source,
                    launch_time,
                    self.monotonic(),
                    SourceCollectionStatus.COLLECTION_ERROR,
                )
                self._stop_processes([state])
                receiver.close()
                continue
            states.append(state)

        pending = list(states)
        while pending:
            now = self.monotonic()
            if now >= aggregate_deadline:
                for state in pending:
                    outcomes[state.source.source_id] = self._outcome(
                        state.source,
                        state.started_at,
                        now,
                        SourceCollectionStatus.AGGREGATE_BUDGET_EXHAUSTED,
                    )
                self._stop_processes(pending)
                for state in pending:
                    state.connection.close()
                pending.clear()
                break
            expired: list[_WorkerState] = []
            for state in pending:
                now = self.monotonic()
                if now >= state.deadline:
                    outcomes[state.source.source_id] = self._outcome(
                        state.source,
                        state.started_at,
                        now,
                        SourceCollectionStatus.SOURCE_TIMEOUT
                        if state.deadline < aggregate_deadline
                        else SourceCollectionStatus.AGGREGATE_BUDGET_EXHAUSTED,
                    )
                    expired.append(state)
                    continue
                if state.connection.poll(0):
                    message_needs_stop = False
                    try:
                        payload = state.connection.recv()
                        status, candidates, issues, errors = self._validate_message(state.source, payload)
                    except (EOFError, OSError, ValueError):
                        message_needs_stop = True
                        outcomes[state.source.source_id] = self._outcome(
                            state.source,
                            state.started_at,
                            self.monotonic(),
                            SourceCollectionStatus.COLLECTION_ERROR,
                        )
                    else:
                        received_at = self.monotonic()
                        message_needs_stop = status != SourceCollectionStatus.SUCCESS
                        if received_at > state.deadline:
                            outcomes[state.source.source_id] = self._outcome(
                                state.source,
                                state.started_at,
                                received_at,
                                SourceCollectionStatus.SOURCE_TIMEOUT
                                if state.deadline < aggregate_deadline
                                else SourceCollectionStatus.AGGREGATE_BUDGET_EXHAUSTED,
                            )
                            message_needs_stop = True
                        else:
                            outcomes[state.source.source_id] = self._outcome(
                                state.source,
                                state.started_at,
                                received_at,
                                status,
                                candidates=candidates,
                                issues=issues,
                                errors=errors,
                            )
                    if message_needs_stop:
                        self._stop_processes([state])
                    pending.remove(state)
                    state.connection.close()
                    self._reap_processes(
                        [state],
                        deadline=self.hard_deadline,
                        monotonic=self.monotonic,
                    )
                    continue
                if not state.process.is_alive():
                    outcomes[state.source.source_id] = self._outcome(
                        state.source,
                        state.started_at,
                        self.monotonic(),
                        SourceCollectionStatus.COLLECTION_ERROR,
                    )
                    self._stop_processes([state])
                    pending.remove(state)
                    state.connection.close()
            if expired:
                self._stop_processes(expired)
                for state in expired:
                    state.connection.close()
                    pending.remove(state)
            if pending:
                remaining = min(aggregate_deadline - self.monotonic(), 0.02)
                if remaining > 0:
                    time.sleep(remaining)

        ordered_outcomes = tuple(outcomes[source.source_id] for source in enabled_sources)
        collected: list[PostingCandidate] = []
        issues: list[CandidateDetailIssueV2] = []
        errors: list[str] = []
        for outcome in ordered_outcomes:
            collected.extend(outcome.candidates)
            issues.extend(outcome.issues)
            errors.extend(f"{outcome.source_id}: collection_error" for _ in outcome.errors)
            if outcome.status != SourceCollectionStatus.SUCCESS:
                errors.append(f"{outcome.source_id}: {outcome.status}")
        return _CollectionBatch(
            outcomes=ordered_outcomes,
            candidates=tuple(collected),
            issues=tuple(issues),
            errors=tuple(errors),
        )


CollectionCoordinator = SourceCollectionCoordinator
CollectionBatch = _CollectionBatch


def coordinate_source_collection(config: AppConfig, **kwargs: Any) -> _CollectionBatch:
    return SourceCollectionCoordinator(config, **kwargs).collect()
def coordinate_source_collection_v1(config: AppConfig, **kwargs: Any) -> CollectionResultV1:
    return collection_result_v1(coordinate_source_collection(config, **kwargs))


collect_enabled_sources_v1 = coordinate_source_collection_v1


collect_enabled_sources = coordinate_source_collection


def _normal_work_remaining(runtime_context: Any) -> float:
    remaining = getattr(runtime_context, "remaining", None)
    if callable(remaining):
        try:
            value = float(remaining())
        except (TypeError, ValueError):
            raise TimeoutError("normal work deadline is unavailable") from None
    else:
        deadline = getattr(runtime_context, "normal_work_deadline", None)
        monotonic = getattr(runtime_context, "monotonic", time.monotonic)
        if deadline is None or not callable(monotonic):
            raise TimeoutError("normal work deadline is unavailable")
        try:
            value = float(deadline) - float(monotonic())
        except (TypeError, ValueError):
            raise TimeoutError("normal work deadline is unavailable") from None
    if not math.isfinite(value):
        raise TimeoutError("normal work deadline is unavailable") from None
    return value


def _collect_and_build(
    config: AppConfig,
    run_date: date,
    *,
    command_mode: str,
    candidates: Optional[Iterable[PostingCandidate]] = None,
    sources_attempted: Optional[Iterable[str]] = None,
    source_errors: Optional[Iterable[str]] = None,
    candidate_detail_issues: Optional[Iterable[CandidateDetailIssueV2]] = None,
    runtime_context: Any = None,
    coordinator: Any = None,
) -> PipelineResultV2 | PipelineResultV4:
    if candidates is not None:
        detail_quality = {
            source.source_id: "manual_only"
            for source in config.sources
            if source.access_mode == "manual"
        }
        return build_pipeline_result_v2(
            config,
            run_date,
            candidates,
            command_mode=command_mode,
            sources_attempted=sources_attempted,
            source_errors=source_errors,
            source_detail_quality=detail_quality,
            candidate_detail_issues=candidate_detail_issues,
        )
    if runtime_context is not None:
        remaining = _normal_work_remaining(runtime_context)
        if remaining <= 0:
            raise TimeoutError("normal work deadline exceeded") from None
        if coordinator is None:
            configured_budget = getattr(
                config,
                "collection_budget_seconds",
                DEFAULT_COLLECTION_BUDGET_SECONDS,
            )
            try:
                configured_budget = float(configured_budget)
            except (TypeError, ValueError):
                configured_budget = DEFAULT_COLLECTION_BUDGET_SECONDS
            if not math.isfinite(configured_budget) or configured_budget <= 0:
                configured_budget = DEFAULT_COLLECTION_BUDGET_SECONDS
            coordinator = SourceCollectionCoordinator(
                config,
                total_budget_seconds=min(configured_budget, remaining),
                runtime_context=runtime_context,
            )
        elif isinstance(coordinator, SourceCollectionCoordinator):
            coordinator.total_budget_seconds = min(
                coordinator.total_budget_seconds,
                remaining,
            )
            context_monotonic = getattr(runtime_context, "monotonic", None)
            if callable(context_monotonic):
                coordinator.monotonic = context_monotonic
            context_deadline = getattr(
                runtime_context,
                "hard_deadline",
                getattr(runtime_context, "deadline", None),
            )
            if context_deadline is not None:
                coordinator.hard_deadline = float(context_deadline)
    if runtime_context is not None and _normal_work_remaining(runtime_context) <= 0:
        raise TimeoutError("normal work deadline exceeded") from None
    if coordinator is None:
        coordinator = SourceCollectionCoordinator(config)
    batch = coordinator.collect()
    for source, outcome in zip(
        (source for source in config.sources if source.enabled),
        batch.outcomes,
    ):
        if (
            outcome.status in {
                SourceCollectionStatus.COLLECTION_ERROR,
                SourceCollectionStatus.COLLECTION_FAILED,
            }
            and source.failure_mode == "fail_run"
        ):
            raise ConfigError(f"{source.source_id}: collection failed")
    detail_quality = {
        source.source_id: "manual_only"
        for source in config.sources
        if source.access_mode == "manual"
    }
    detail_issues: list[CandidateDetailIssueV2] = list(candidate_detail_issues or ())
    detail_issues.extend(batch.issues)
    errors = [str(error) for error in batch.errors]
    errors.extend(str(error) for error in (source_errors or ()))
    outcomes = tuple(batch.source_outcomes)
    if command_mode == CommandMode.DRY_RUN.value:
        return build_pipeline_result_v2(
            config,
            run_date,
            batch.candidates,
            command_mode=command_mode,
            sources_attempted=tuple(item.source_id for item in outcomes),
            source_errors=tuple(errors),
            source_detail_quality=detail_quality,
            candidate_detail_issues=detail_issues,
        )
    return build_pipeline_result_v4(
        config,
        run_date,
        batch.candidates,
        source_outcomes=outcomes,
        command_mode=command_mode,
        sources_attempted=tuple(item.source_id for item in outcomes),
        source_errors=tuple(errors),
        source_detail_quality=detail_quality,
        candidate_detail_issues=detail_issues,
    )


def run_dry_run(config: AppConfig, run_date: date) -> PipelineResultV2:
    _assert_no_real_sources(config)
    return _collect_and_build(config, run_date, command_mode=CommandMode.DRY_RUN.value)


def run_live_run(
    config: AppConfig,
    run_date: date,
    *,
    runtime_context: Any = None,
    coordinator: Any = None,
) -> PipelineResultV4:
    return _collect_and_build(
        config,
        run_date,
        command_mode=CommandMode.LIVE_RUN.value,
        runtime_context=runtime_context,
        coordinator=coordinator,
    )


def run_scheduled_run(
    config: AppConfig,
    run_date: date,
    *,
    runtime_context: Any = None,
    coordinator: Any = None,
) -> PipelineResultV4:
    return _collect_and_build(
        config,
        run_date,
        command_mode=CommandMode.SCHEDULED_RUN.value,
        runtime_context=runtime_context,
        coordinator=coordinator,
    )


def build_live_run_preflight_gate(run_date: date, config: AppConfig) -> dict[str, Any]:
    missing_fields = missing_context_fields(config.user_context)
    findings = []
    if missing_fields:
        findings.append({"severity": "fail", "source_id": None, "message": "live-run missing required user context: " + ", ".join(missing_fields)})
    return {
        "schema_version": 1,
        "command_mode": "live-run",
        "status": "fail" if findings else "pass",
        "run_date": run_date.isoformat(),
        "context_status": "needs_context" if missing_fields else "complete",
        "missing_context": missing_fields,
        "sources_attempted": [],
        "candidates_collected": 0,
        "sources": [],
        "findings": findings,
    }


def run_capture_import(
    config: AppConfig,
    run_date: date,
    candidates: Iterable[PostingCandidate],
    sources_attempted: list[str],
    source_errors: list[str],
) -> PipelineResultV2:
    return _collect_and_build(
        config,
        run_date,
        command_mode=CommandMode.CAPTURE_IMPORT.value,
        candidates=candidates,
        sources_attempted=sources_attempted,
        source_errors=source_errors,
    )


def run_dry_run_from_config(config_path, run_date: date):
    return run_dry_run(load_config(config_path), run_date)
