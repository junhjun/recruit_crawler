from __future__ import annotations

"""Deterministic, public GateV2 construction.

The gate is deliberately downstream of the immutable pipeline result.  It never
collects, parses, ranks, or exposes the private fields carried by v2 records.
"""

from collections import Counter
from datetime import date
import hashlib
import json
import re
import unicodedata
from enum import Enum
from typing import Any, Iterable, Mapping

from .projection import project_pipeline_result
from .schemas import (
    DISPOSITION_SCHEMA_VERSION,
    GATE_SCHEMA_VERSION,
    GATE_V4_SCHEMA_VERSION,
    PIPELINE_RESULT_V4_SCHEMA_VERSION,
    PIPELINE_RESULT_SCHEMA_VERSION,
    REPORT_ARTIFACT_SCHEMA_VERSION,
    SCORE_SCHEMA_VERSION,
    PipelineResultV2,
    PipelineResultV4,
    SourceMetricV2,
    SourceMetricV4,
    ReportArtifactV2,
    SourceExecutionOutcomeV1,
    source_execution_outcome_v1_is_consistent,
    SOURCE_EXECUTION_OUTCOME_STATUSES_V1,
)


_SAFE_CODE = re.compile(r"[a-z0-9][a-z0-9_.-]{0,63}\Z")
_SAFE_SOURCE_CODE = re.compile(r"[a-z0-9][a-z0-9_-]{0,63}\Z")
_COMMAND_MODES = {"dry-run", "live-run", "scheduled-run", "capture-import", "replay"}
_CONTEXT_STATUSES = {"complete", "needs_context"}

_UNKNOWN_SOURCE_ID = "unknown-source"
# Only these strings can be emitted as finding messages.  In particular, raw
_DETAIL_WARNING_CODES = {
    "detail_url_invalid",
    "detail_fetch_failed",
    "detail_unverified",
    "detail_issue_invalid",
}
_COLLECTION_WARNING_CODES = {"collection_error", "collection_failed"}
# adapter errors, paths, URLs, and user context are never copied into a gate.
_MESSAGES = {
    "context": "required user context is missing",
    "source_identity": "source identity rejected",
    "source_not_attempted": "enabled source was not attempted",
    "source_zero_accepted": "enabled source accepted zero candidates",
    "capture_zero_accepted": "capture source accepted zero candidates",
    "scheduled_policy": "scheduled source policy failed",
    "scheduled_db": "scheduled database operation failed",
    "scheduled_network": "scheduled network preflight failed",
    "preflight": "scheduled preflight failed",
    "source_outcome_failed": "enabled source execution did not complete successfully",
    "source_outcome_inconsistent": "source execution outcome was internally inconsistent",
    "source_outcome_missing": "enabled source execution outcome was not recorded",
    "source_detail_warning": "source detail warning recorded",
    "source_collection_warning": "source collection warning recorded",
    "scheduled_report_rollback": "scheduled report rollback could not be confirmed",
    "runtime": "scheduled runtime failure",
    "live_report_render": "live report rendering failed",
    "live_report_candidate": "live report candidate failed validation",
    "live_preflight_deadline": "live preflight deadline exceeded",
    "live_collection": "live collection failed",
    "live_publication_unknown": "live report publication state unknown",
    "live_gate_indeterminate": "live quality gate output is indeterminate",
    "live_gate_rolled_back": "live quality gate output failed; report rolled back",
    "live_gate_publication_unknown": "live quality gate output failed; report publication state unknown",
    "schema": "pipeline schema versions are incompatible",
    "report_integrity": "report artifact integrity failed",
    "queue_parity": "report queue parity failed",
}
_RUNTIME_FAILURE_ALIASES = {
    "scheduled_policy_failure": "scheduled_policy",
    "scheduled_db_failure": "scheduled_db",
    "scheduled_db_finalize_failure": "scheduled_db",
    "scheduled_network_failure": "scheduled_network",
    "preflight_failure": "preflight",
    "report_rollback_failure": "scheduled_report_rollback",
    "scheduled_report_rollback_failure": "scheduled_report_rollback",
    "rollback_failure": "scheduled_report_rollback",
    "scheduled_publication_pending": "runtime",
    "scheduled_runtime_failure": "runtime",
    "scheduled_quality_gate_output_failure": "runtime",
    "scheduled_report_publication_uncertain": "runtime",
    "scheduled_persistence_uncertain": "scheduled_db",
    "live_report_render_failed": "live_report_render",
}


def _value(value: Any) -> Any:
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, date):
        return value.isoformat()
    return value


def _safe_code(value: Any) -> str | None:
    try:
        value = str(value)
    except Exception:
        return None
    if not _SAFE_CODE.fullmatch(value):
        return None
    lowered = value.casefold()
    if any(token in lowered for token in ("private", "canary", "secret", "raw", "opaque", "identity", "path", "full")):
        return None
    if "military" in lowered:
        return None
    return value

def _source_id_is_safe(value: Any) -> bool:
    safe = _safe_code(value)
    return safe is not None and _SAFE_SOURCE_CODE.fullmatch(safe) is not None

def _safe_source_id(value: Any) -> str:
    safe = _safe_code(value)
    return safe if safe is not None and _SAFE_SOURCE_CODE.fullmatch(safe) is not None else _UNKNOWN_SOURCE_ID


_PUBLIC_GATE_REASON_CODES = frozenset(
    {
        "dealbreaker",
        "education_ambiguous",
        "education_match",
        "education_mismatch",
        "education_unknown",
        "experience_ambiguous",
        "experience_mismatch",
        "experience_match",
        "experience_unknown",
        "invalid_candidate",
        "expired",
        "manual_flag",
        "manual_source",
    }
)


def _safe_codes(values: Iterable[Any]) -> list[str]:
    return sorted({code for value in values if (code := _safe_code(value))})


def _finding(code: str, source_id: str | None = None, severity: str = "fail") -> dict[str, Any]:
    return {
        "severity": severity,
        "source_id": _safe_source_id(source_id) if source_id is not None else None,
        "message": _MESSAGES[code],
    }


def _configured_canary_matcher(values: Iterable[str] | str) -> tuple[str, ...]:
    if isinstance(values, str):
        values = (values,)
    try:
        raw_values = tuple(values)
    except TypeError:
        return ()
    return tuple(
        unicodedata.normalize("NFC", value).casefold()
        for value in raw_values
        if type(value) is str and value
    )


def _matches_configured_canary(value: Any, configured_canaries: tuple[str, ...]) -> bool:
    return (
        isinstance(value, str)
        and any(
            canary and canary in unicodedata.normalize("NFC", value).casefold()
            for canary in configured_canaries
        )
    )
def _source_has_configured_canary(
    value: Any,
    configured_canaries: tuple[str, ...],
) -> bool:
    if isinstance(value, Mapping):
        get = value.get
    else:
        get = lambda name, default=None: getattr(value, name, default)
    if _matches_configured_canary(get("source_id", ""), configured_canaries):
        return True
    error_codes = get("error_codes", ())
    try:
        return any(
            _matches_configured_canary(code, configured_canaries)
            for code in error_codes
        )
    except TypeError:
        return False


def _guard_configured_canaries(
    value: Any,
    configured_canaries: tuple[str, ...],
    *,
    field_name: str | None = None,
) -> Any:
    if isinstance(value, Mapping):
        return {
            key: _guard_configured_canaries(
                item,
                configured_canaries,
                field_name=str(key),
            )
            for key, item in value.items()
        }
    if isinstance(value, (list, tuple)):
        values = (
            item
            for item in value
            if field_name != "error_codes"
            or not _matches_configured_canary(item, configured_canaries)
        )
        guarded = [
            _guard_configured_canaries(item, configured_canaries, field_name=field_name)
            for item in values
        ]
        return tuple(guarded) if isinstance(value, tuple) else guarded
    if field_name == "source_id" and _matches_configured_canary(
        value,
        configured_canaries,
    ):
        return _UNKNOWN_SOURCE_ID
    if isinstance(value, str) and _matches_configured_canary(
        value,
        configured_canaries,
    ):
        return ""
    return value


def _gate_contains_configured_canary(
    value: Any,
    configured_canaries: tuple[str, ...],
) -> bool:
    if isinstance(value, Mapping):
        return any(
            _gate_contains_configured_canary(item, configured_canaries)
            for item in value.values()
        )
    if isinstance(value, (list, tuple)):
        return any(
            _gate_contains_configured_canary(item, configured_canaries)
            for item in value
        )
    return _matches_configured_canary(value, configured_canaries)


def _safe_error_codes(
    values: Iterable[Any],
    configured_canaries: tuple[str, ...],
) -> list[str]:
    return _safe_codes(
        value
        for value in values
        if not _matches_configured_canary(value, configured_canaries)
    )


def _source_row(
    value: Any,
    *,
    configured_canaries: tuple[str, ...] = (),
) -> dict[str, Any]:
    """Convert a GateSourceV2 (or its public mapping) to its exact JSON shape."""
    if isinstance(value, Mapping):
        get = value.get
    else:
        get = lambda name, default=None: getattr(value, name, default)
    detail = get("detail_quality", ())
    if isinstance(detail, Mapping):
        detail_values = {
            "verified": int(detail.get("verified", 0)),
            "manual_only": int(detail.get("manual_only", 0)),
            "rejected": int(detail.get("rejected", 0)),
        }
    else:
        detail_values = {"verified": 0, "manual_only": 0, "rejected": 0}
        for item in detail or ():
            if isinstance(item, (tuple, list)) and len(item) == 2 and item[0] in detail_values:
                detail_values[str(item[0])] = int(item[1])
    raw_source_id = get("source_id", "")
    source_id = (
        _UNKNOWN_SOURCE_ID
        if _matches_configured_canary(raw_source_id, configured_canaries)
        else _safe_source_id(raw_source_id)
    )
    error_codes = _safe_error_codes(get("error_codes", ()), configured_canaries)
    return {
        "source_id": source_id,
        "attempted": bool(get("attempted", False)),
        "candidate_count": int(get("candidate_count", 0)),
        "source_rejected_count": int(get("source_rejected_count", 0)),
        "duplicate_count": int(get("duplicate_count", 0)),
        "normalized_changed_field_count": int(get("normalized_changed_field_count", 0)),
        "normalized_emptied_field_count": int(get("normalized_emptied_field_count", 0)),
        "detail_quality": detail_values,
        "error_count": len(error_codes),
        "error_codes": error_codes,
        "duration_ms": int(get("duration_ms", 0)),
    }


def _reason_counts(result: PipelineResultV2) -> dict[str, int]:
    counts: Counter[str] = Counter()
    for assessment in result.all_assessments:
        for item in assessment.eligibility:
            code = _safe_code(getattr(item, "reason_code", ""))
            if code in _PUBLIC_GATE_REASON_CODES:
                counts[code] += 1
    return {key: counts[key] for key in sorted(counts)}


def _artifact_report(
    result: PipelineResultV2,
    artifact: ReportArtifactV2 | Mapping[str, Any] | None,
    *,
    configured_canaries: tuple[str, ...] = (),
) -> tuple[dict[str, Any], bool, list[dict[str, Any]]]:
    empty = {
        "generated": False,
        "content_sha256": None,
        "byte_length": 0,
        "queue_parity": False,
    }
    if artifact is None:
        return empty, False, [_finding("report_integrity")]
    get = artifact.get if isinstance(artifact, Mapping) else lambda name, default=None: getattr(artifact, name, default)
    schema_version = get("schema_version")
    generated = get("generated")
    rendered = get("rendered")
    if generated is not True and generated is not False:
        return empty, False, [_finding("report_integrity")]
    if generated is False:
        return empty, False, [_finding("report_integrity")]
    if schema_version != REPORT_ARTIFACT_SCHEMA_VERSION or rendered is None:
        return {"generated": True, "content_sha256": None, "byte_length": 0, "queue_parity": False}, False, [_finding("report_integrity")]
    rget = rendered.get if isinstance(rendered, Mapping) else lambda name, default=None: getattr(rendered, name, default)
    raw = rget("markdown_bytes")
    if not isinstance(raw, bytes):
        return {"generated": True, "content_sha256": None, "byte_length": 0, "queue_parity": False}, False, [_finding("report_integrity")]
    supplied_hash = rget("content_sha256")
    supplied_length = rget("byte_length")
    actual_hash = hashlib.sha256(raw).hexdigest()
    valid = (
        rget("schema_version") == REPORT_ARTIFACT_SCHEMA_VERSION
        and isinstance(supplied_hash, str)
        and re.fullmatch(r"[0-9a-f]{64}", supplied_hash) is not None
        and supplied_hash == actual_hash
        and isinstance(supplied_length, int)
        and not isinstance(supplied_length, bool)
        and supplied_length == len(raw)
        and not _matches_configured_canary(
            raw.decode("utf-8", errors="replace"),
            configured_canaries,
        )
    )
    queue_parity = False
    if valid:
        try:
            from .summarizer import render_report_v2

            queue_parity = (
                render_report_v2(
                    result,
                    private_canaries=configured_canaries,
                ).markdown_bytes
                == raw
            )
        except Exception:
            queue_parity = False
    report = {
        "generated": True,
        "content_sha256": actual_hash if valid else None,
        "byte_length": len(raw) if valid else 0,
        "queue_parity": queue_parity,
    }
    findings: list[dict[str, Any]] = []
    if not valid:
        findings.append(_finding("report_integrity"))
    elif not queue_parity:
        findings.append(_finding("queue_parity"))
    return report, valid and queue_parity, findings


def build_gate_v2(
    result: PipelineResultV2,
    *,
    enabled_source_ids: Iterable[str] = (),
    configured_canaries: Iterable[str] | str = (),
    context_status: str = "complete",
    runtime_failures: Iterable[str] = (),
    runtime_context: Mapping[str, Any] | None = None,
    report_artifact: ReportArtifactV2 | Mapping[str, Any] | None = None,
    projection: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Build the ordered public GateV2 mapping from immutable inputs only."""
    if not isinstance(result, PipelineResultV2):
        raise TypeError("build_gate_v2 requires PipelineResultV2")
    runtime = dict(runtime_context or {})
    configured_canary_values = _configured_canary_matcher(configured_canaries)
    enabled_values = tuple(runtime.get("enabled_source_ids", enabled_source_ids))
    unsafe_enabled = any(
        not _source_id_is_safe(item)
        or _matches_configured_canary(item, configured_canary_values)
        for item in enabled_values
    )
    enabled = tuple(
        sorted(
            {
                (
                    _UNKNOWN_SOURCE_ID
                    if _matches_configured_canary(item, configured_canary_values)
                    else _safe_source_id(item)
                )
                for item in enabled_values
            }
        )
    )
    status_value = str(_value(runtime.get("context_status", context_status)))
    if status_value not in _CONTEXT_STATUSES:
        status_value = "needs_context"
    # The Gate is a result-only boundary.  Callers may pass a projection for
    # compatibility, but public Gate bytes are always re-derived here.
    projection = project_pipeline_result(result)
    gate_sources = tuple(projection.get("gate_sources", ()))
    unsafe_sources = any(
        not _source_id_is_safe(
            item.get("source_id", "")
            if isinstance(item, Mapping)
            else getattr(item, "source_id", "")
        )
        or _source_has_configured_canary(item, configured_canary_values)
        for item in gate_sources
    ) or any(
        _source_has_configured_canary(item, configured_canary_values)
        for item in result.source_metrics
    )
    source_rows = [
        _source_row(item, configured_canaries=configured_canary_values)
        for item in gate_sources
    ]
    source_rows.sort(key=lambda item: item["source_id"])
    by_source = {item["source_id"]: item for item in source_rows}
    command_mode = str(_value(result.command_mode))
    if command_mode not in _COMMAND_MODES:
        command_mode = "replay"
    findings: list[dict[str, Any]] = []
    if unsafe_enabled or unsafe_sources:
        findings.append(_finding("source_identity", _UNKNOWN_SOURCE_ID))
    if status_value != "complete":
        findings.append(_finding("context"))
    for source_id in enabled:
        row = by_source.get(source_id)
        if row is None or not row["attempted"]:
            findings.append(_finding("source_not_attempted", source_id))
        elif row["candidate_count"] == 0:
            code = "capture_zero_accepted" if command_mode == "capture-import" else "source_zero_accepted"
            findings.append(_finding(code, source_id, "warning" if command_mode == "capture-import" else "fail"))
    for row in source_rows:
        codes = set(row["error_codes"])
        if codes & _DETAIL_WARNING_CODES:
            findings.append(_finding("source_detail_warning", row["source_id"], "warning"))
        if codes & _COLLECTION_WARNING_CODES:
            findings.append(_finding("source_collection_warning", row["source_id"], "warning"))

    failure_codes: set[str] = set()
    unknown_runtime_failure = False
    for item in runtime_failures:
        code = str(item)
        mapped = _RUNTIME_FAILURE_ALIASES.get(code)
        if mapped is None:
            unknown_runtime_failure = True
        else:
            failure_codes.add(mapped)
    if unknown_runtime_failure:
        failure_codes.add("runtime")
    if runtime.get("scheduled_policy_failures"):
        failure_codes.add("scheduled_policy")
    if runtime.get("scheduled_db_failure"):
        failure_codes.add("scheduled_db")
    if runtime.get("scheduled_network_failure"):
        failure_codes.add("scheduled_network")
    if runtime.get("preflight"):
        failure_codes.add("preflight")
    for code in sorted(failure_codes):
        if code in _MESSAGES:
            findings.append(_finding(code))

    versions_ok = (
        result.schema_version == PIPELINE_RESULT_SCHEMA_VERSION
        and SCORE_SCHEMA_VERSION == 2
        and DISPOSITION_SCHEMA_VERSION == 2
    )
    if not versions_ok:
        findings.append(_finding("schema"))
    report, report_ok, report_findings = _artifact_report(
        result,
        report_artifact,
        configured_canaries=configured_canary_values,
    )
    findings.extend(report_findings)

    summary = dict(projection.get("summary", {}))
    eligibility_counts = _reason_counts(result)
    manual_counts = {
        str(key): int(value)
        for key, value in sorted(dict(projection.get("manual_reason_counts", {})).items())
        if _safe_code(key) in _PUBLIC_GATE_REASON_CODES
    }
    invariants = sorted(
        {
            "pipeline_schema_v2" if result.schema_version == PIPELINE_RESULT_SCHEMA_VERSION else "pipeline_schema_invalid",
            "score_schema_v2",
            "disposition_schema_v2",
            "source_rows_sorted",
            "reason_counts_sorted",
            "summary_projection_consistent",
            "report_artifact_valid" if report_ok else "report_artifact_unavailable",
            "queue_parity" if report.get("queue_parity") else "queue_parity_unavailable",
        }
    )
    findings.sort(key=lambda item: (item["severity"], item["source_id"] or "", item["message"]))
    status = "fail" if any(item["severity"] == "fail" for item in findings) else ("warning" if findings else "pass")
    gate = {
        "schema_version": GATE_SCHEMA_VERSION,
        "command_mode": command_mode,
        "run_date": result.run_date.isoformat(),
        "pipeline_schema_version": int(result.schema_version),
        "score_schema_version": SCORE_SCHEMA_VERSION,
        "disposition_schema_version": DISPOSITION_SCHEMA_VERSION,
        "status": status,
        "context_status": status_value,
        "report": report,
        "sources": source_rows,
        "summary": summary,
        "eligibility_reason_counts": eligibility_counts,
        "manual_reason_counts": manual_counts,
        "invariants": invariants,
        "findings": findings,
    }
    if _gate_contains_configured_canary(gate, configured_canary_values):
        findings.append(_finding("source_identity", _UNKNOWN_SOURCE_ID))
        findings.sort(key=lambda item: (item["severity"], item["source_id"] or "", item["message"]))
        gate["findings"] = findings
        gate["status"] = "fail"
    return _guard_configured_canaries(gate, configured_canary_values)
def _v4_outcome_row(value: Any) -> tuple[dict[str, Any], bool]:
    """Return a redacted outcome row and its contract validity."""

    get = value.get if isinstance(value, Mapping) else lambda name, default=None: getattr(value, name, default)
    source_id = get("source_id", _UNKNOWN_SOURCE_ID)
    safe_source = _safe_source_id(source_id)
    status = get("status")
    valid = source_execution_outcome_v1_is_consistent(value)
    if status not in {
        "success",
        "collection_error",
        "collection_failed",
        "source_timeout",
        "aggregate_budget_exhausted",
    }:
        status = "collection_error"
    error_code = get("error_code")
    if status == "success":
        error_code = None
    elif error_code not in {
        "collection_error",
        "collection_failed",
        "source_timeout",
        "aggregate_budget_exhausted",
    }:
        error_code = status
    # Timing is retained in the private V4 result/storage envelope, but the
    # public gate is deterministic and intentionally exposes no elapsed time.
    elapsed_ms = 0
    attempted = get("attempted", False)
    completed = get("completed", False)
    row = {
        "source_id": safe_source,
        "attempted": attempted if type(attempted) is bool else False,
        "completed": completed if type(completed) is bool else False,
        "status": status,
        "error_code": error_code,
        "elapsed_ms": elapsed_ms,
    }
    return row, valid


def _v4_legacy_result(result: PipelineResultV4) -> PipelineResultV2:
    outcome_by_source = {
        item.source_id: item for item in result.source_outcomes
    }

    def error_codes_for(item: SourceMetricV4) -> tuple[str, ...]:
        outcome = outcome_by_source.get(item.source_id)
        outcome_code = (
            getattr(outcome, "error_code", None)
            if outcome is not None
            else None
        )
        return tuple(
            sorted(
                {
                    *item.error_codes,
                    *(
                        (outcome_code,)
                        if outcome_code in _PUBLIC_SOURCE_ERROR_CODES
                        else ()
                    ),
                }
            )
        )

    metrics = tuple(
        SourceMetricV2(
            source_id=item.source_id,
            attempted=item.attempted,
            accepted_count=item.accepted_count,
            rejected_count=item.rejected_count,
            duplicate_count=item.duplicate_count,
            normalized_changed_field_count=item.normalized_changed_field_count,
            normalized_emptied_field_count=item.normalized_emptied_field_count,
            verified_count=item.verified_count,
            manual_only_count=item.manual_only_count,
            error_codes=error_codes_for(item),
            duration_ms=0,
        )
        for item in result.source_metrics
    )
    return PipelineResultV2(
        schema_version=PIPELINE_RESULT_SCHEMA_VERSION,
        command_mode=result.command_mode,
        run_date=result.run_date,
        all_assessments=result.all_assessments,
        source_metrics=metrics,
        duplicates_removed=result.duplicates_removed,
        collected_count=result.collected_count,
        source_rejected_count=result.source_rejected_count,
        top_n=result.top_n,
        manual_review_n=result.manual_review_n,
    )


def build_gate_v4(
    result: PipelineResultV4,
    *,
    enabled_source_ids: Iterable[str] = (),
    configured_canaries: Iterable[str] | str = (),
    context_status: str = "complete",
    runtime_failures: Iterable[str] = (),
    runtime_context: Mapping[str, Any] | None = None,
    report_artifact: ReportArtifactV2 | Mapping[str, Any] | None = None,
    projection: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Build a public Gate V4 and fail closed on outcome/axis mismatches."""

    if not isinstance(result, PipelineResultV4) or result.schema_version != PIPELINE_RESULT_V4_SCHEMA_VERSION:
        raise TypeError("build_gate_v4 requires PipelineResultV4")
    outcomes = tuple(result.source_outcomes)
    if len({getattr(item, "source_id", None) for item in outcomes}) != len(outcomes):
        raise ValueError("Gate V4 source outcome IDs are not unique")
    legacy = _v4_legacy_result(result)
    gate = build_gate_v2(
        legacy,
        enabled_source_ids=enabled_source_ids,
        configured_canaries=configured_canaries,
        context_status=context_status,
        runtime_failures=runtime_failures,
        runtime_context=runtime_context,
        report_artifact=report_artifact,
        projection=projection,
    )
    rows_valid = [_v4_outcome_row(item) for item in outcomes]
    rows = [row for row, _valid in rows_valid]
    rows.sort(key=lambda item: item["source_id"])
    enabled = {
        _safe_source_id(item)
        for item in tuple((runtime_context or {}).get("enabled_source_ids", enabled_source_ids))
    }
    outcome_objects = {getattr(item, "source_id", None): item for item in outcomes}
    outcome_by_source = {row["source_id"]: (row, valid) for row, valid in rows_valid}
    metric_by_source = {item.source_id: item for item in result.source_metrics}
    findings = list(gate["findings"])
    if set(outcome_by_source) != enabled or set(metric_by_source) != enabled:
        for source_id in sorted(set(outcome_by_source) ^ enabled):
            findings.append(_finding("source_outcome_inconsistent", source_id))
        for source_id in sorted(set(metric_by_source) ^ enabled):
            findings.append(_finding("source_outcome_inconsistent", source_id))
    for source_id in sorted(enabled):
        pair = outcome_by_source.get(source_id)
        if pair is None:
            findings.append(_finding("source_outcome_missing", source_id))
            continue
        row, valid = pair
        metric = metric_by_source.get(source_id)
        if not valid or metric is None:
            findings.append(_finding("source_outcome_inconsistent", source_id))
            continue
        if (
            not isinstance(metric, SourceMetricV4)
            or metric.outcome != outcome_objects.get(source_id)
            or metric.attempted != row["attempted"]
            or (
                set(metric.error_codes)
                & {
                    "collection_error",
                    "collection_failed",
                    "source_timeout",
                    "aggregate_budget_exhausted",
                }
                and row["status"] == "success"
            )
        ):
            findings.append(_finding("source_outcome_inconsistent", source_id))
        if (
            row["status"] != "success"
            or row["attempted"] is not True
            or row["completed"] is not True
            or row["error_code"] is not None
        ):
            findings.append(_finding("source_outcome_failed", source_id))
    for source_id, (_row, valid) in outcome_by_source.items():
        if not valid or not _source_id_is_safe(source_id):
            findings.append(_finding("source_outcome_inconsistent", _UNKNOWN_SOURCE_ID))
    findings.sort(key=lambda item: (item["severity"], item["source_id"] or "", item["message"]))
    gate["schema_version"] = GATE_V4_SCHEMA_VERSION
    gate["pipeline_schema_version"] = PIPELINE_RESULT_V4_SCHEMA_VERSION
    gate["invariants"] = sorted(
        {
            "pipeline_schema_v4",
            "score_schema_v2",
            "disposition_schema_v2",
            "source_rows_sorted",
            "reason_counts_sorted",
            "summary_projection_consistent",
            "report_artifact_valid"
            if gate["report"]["generated"]
            and gate["report"]["content_sha256"]
            and gate["report"]["queue_parity"]
            else "report_artifact_unavailable",
            "queue_parity" if gate["report"]["queue_parity"] else "queue_parity_unavailable",
            "source_outcomes_consistent" if not any(not valid for _row, valid in rows_valid) else "source_outcomes_invalid",
        }
    )
    gate["source_outcomes"] = rows
    gate["findings"] = findings
    gate["status"] = (
        "fail"
        if any(item["severity"] == "fail" for item in findings)
        else ("warning" if findings else "pass")
    )
    return _guard_configured_canaries(gate, _configured_canary_matcher(configured_canaries))


_V4_OUTCOME_KEYS = ("source_id", "attempted", "completed", "status", "error_code", "elapsed_ms")


def _canonical_gate_v4_mapping(gate: Mapping[str, Any]) -> Mapping[str, Any]:
    """Validate the exact public Gate V4 shape without accepting V2."""

    _exact_keys(gate, _PUBLIC_GATE_KEYS + ("source_outcomes",), "gate")
    if type(gate["schema_version"]) is not int or gate["schema_version"] != GATE_V4_SCHEMA_VERSION:
        raise ValueError("invalid GateV4 schema version")
    if gate["pipeline_schema_version"] != PIPELINE_RESULT_V4_SCHEMA_VERSION:
        raise ValueError("invalid PipelineResultV4 axis")
    outcomes = gate["source_outcomes"]
    if type(outcomes) is not list:
        raise ValueError("GateV4 source_outcomes must be a list")
    rows: list[dict[str, Any]] = []
    for index, item in enumerate(outcomes):
        _exact_keys(item, _V4_OUTCOME_KEYS, f"source_outcomes[{index}]")
        source_id = _exact_source_id(item["source_id"], f"source_outcomes[{index}].source_id")
        if type(item["attempted"]) is not bool or type(item["completed"]) is not bool:
            raise ValueError("GateV4 outcome flags are invalid")
        if item["status"] not in SOURCE_EXECUTION_OUTCOME_STATUSES_V1:
            raise ValueError("GateV4 outcome status is not allowlisted")
        if type(item["elapsed_ms"]) is not int or item["elapsed_ms"] < 0:
            raise ValueError("GateV4 outcome duration is invalid")
        if item["status"] == "success" and item["error_code"] is not None:
            raise ValueError("successful outcome contains an error code")
        if item["status"] != "success" and item["error_code"] != item["status"]:
            raise ValueError("failed outcome error code is inconsistent")
        rows.append({key: item[key] for key in _V4_OUTCOME_KEYS} | {"source_id": source_id})
    if [row["source_id"] for row in rows] != sorted({row["source_id"] for row in rows}):
        raise ValueError("GateV4 outcomes are not sorted or unique")
    source_ids: list[str] = []
    sources = gate["sources"]
    if type(sources) is not list:
        raise ValueError("GateV4 sources must be a list")
    for index, item in enumerate(sources):
        source = _canonical_source(item, index)
        source_ids.append(source["source_id"])
        if source["duration_ms"] != 0:
            raise ValueError("GateV4 source duration must be zero")
    outcome_ids = [row["source_id"] for row in rows]
    if source_ids != outcome_ids:
        raise ValueError("GateV4 source and outcome axes do not match")
    if any(row["elapsed_ms"] != 0 for row in rows):
        raise ValueError("GateV4 outcome duration must be zero")
    if gate["status"] == "pass" and (
        not rows
        or any(
            not row["attempted"]
            or not row["completed"]
            or row["status"] != "success"
            or row["error_code"] is not None
            for row in rows
        )
    ):
        raise ValueError("GateV4 pass contains an unsuccessful source outcome")
    base = dict(gate)
    base.pop("source_outcomes")
    base["schema_version"] = GATE_SCHEMA_VERSION
    base["pipeline_schema_version"] = PIPELINE_RESULT_SCHEMA_VERSION
    base["invariants"] = [
        "pipeline_schema_v2" if item == "pipeline_schema_v4" else item
        for item in base["invariants"]
        if item not in {"source_outcomes_consistent", "source_outcomes_invalid"}
    ]
    _canonical_gate_mapping(base)
    return dict(gate)


def canonical_gate_v4_bytes(gate: Mapping[str, Any]) -> bytes:
    return json.dumps(
        _canonical_gate_v4_mapping(gate),
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=False,
    ).encode("utf-8")


def gate_json_sha256_v4(gate: Mapping[str, Any]) -> str:
    return hashlib.sha256(canonical_gate_v4_bytes(gate)).hexdigest()


_PUBLIC_SOURCE_ERROR_CODES = frozenset(
    {
        "collection_error",
        "collection_failed",
        "source_timeout",
        "aggregate_budget_exhausted",
        "invalid_candidate",
        "detail_issue_invalid",
        "detail_url_invalid",
        "detail_fetch_failed",
        "detail_unverified",
    }
)
_PUBLIC_GATE_KEYS = (
    "schema_version",
    "command_mode",
    "run_date",
    "pipeline_schema_version",
    "score_schema_version",
    "disposition_schema_version",
    "status",
    "context_status",
    "report",
    "sources",
    "summary",
    "eligibility_reason_counts",
    "manual_reason_counts",
    "invariants",
    "findings",
)
_REPORT_KEYS = ("generated", "content_sha256", "byte_length", "queue_parity")
_SOURCE_KEYS = (
    "source_id",
    "attempted",
    "candidate_count",
    "source_rejected_count",
    "duplicate_count",
    "normalized_changed_field_count",
    "normalized_emptied_field_count",
    "detail_quality",
    "error_count",
    "error_codes",
    "duration_ms",
)
_DETAIL_KEYS = ("verified", "manual_only", "rejected")
_SUMMARY_KEYS = (
    "collected",
    "source_rejected",
    "source_accepted",
    "duplicates_removed",
    "deduplicated",
    "expired",
    "exclude",
    "manual_review_total",
    "apply_total",
    "hold_total",
    "low_priority_total",
    "actionable_total",
    "displayed_apply",
    "displayed_hold",
    "suppressed_apply",
    "suppressed_hold",
    "displayed_manual",
    "suppressed_manual",
)
_PUBLIC_INVARIANTS = frozenset(
    {
        "pipeline_schema_v2",
        "pipeline_schema_invalid",
        "score_schema_v2",
        "disposition_schema_v2",
        "source_rows_sorted",
        "reason_counts_sorted",
        "summary_projection_consistent",
        "report_artifact_valid",
        "report_artifact_unavailable",
        "queue_parity",
        "queue_parity_unavailable",
    }
)
_PUBLIC_SEVERITIES = frozenset({"fail", "warning"})
_PUBLIC_STATUSES = frozenset({"pass", "warning", "fail"})
_HASH_RE = re.compile(r"[0-9a-f]{64}\Z")


def _canonical_error(message: str) -> ValueError:
    return ValueError(f"invalid GateV2 mapping: {message}")


def _exact_keys(value: Any, expected: tuple[str, ...], field: str) -> None:
    if not isinstance(value, Mapping):
        raise _canonical_error(f"{field} must be a mapping")
    keys = tuple(value.keys())
    if any(type(key) is not str for key in keys) or set(keys) != set(expected):
        raise _canonical_error(f"{field} has an invalid shape")


def _exact_int(value: Any, field: str, *, minimum: int = 0) -> int:
    if type(value) is not int or value < minimum:
        raise _canonical_error(f"{field} must be a non-negative integer")
    return value


def _exact_source_id(value: Any, field: str) -> str:
    if type(value) is not str or not _source_id_is_safe(value):
        raise _canonical_error(f"{field} is not a controlled source ID")
    return value


def _canonical_report(value: Any) -> dict[str, Any]:
    _exact_keys(value, _REPORT_KEYS, "report")
    generated = value["generated"]
    if type(generated) is not bool:
        raise _canonical_error("report.generated must be a boolean")
    content_hash = value["content_sha256"]
    if content_hash is not None and (
        type(content_hash) is not str or _HASH_RE.fullmatch(content_hash) is None
    ):
        raise _canonical_error("report.content_sha256 is invalid")
    byte_length = _exact_int(value["byte_length"], "report.byte_length")
    queue_parity = value["queue_parity"]
    if type(queue_parity) is not bool:
        raise _canonical_error("report.queue_parity must be a boolean")
    if not generated and (content_hash is not None or byte_length != 0 or queue_parity):
        raise _canonical_error("report fields are inconsistent")
    if generated and content_hash is None and (byte_length != 0 or queue_parity):
        raise _canonical_error("report fields are inconsistent")
    return {key: value[key] for key in _REPORT_KEYS}


def _canonical_source(value: Any, index: int) -> dict[str, Any]:
    field = f"sources[{index}]"
    _exact_keys(value, _SOURCE_KEYS, field)
    source_id = _exact_source_id(value["source_id"], f"{field}.source_id")
    attempted = value["attempted"]
    if type(attempted) is not bool:
        raise _canonical_error(f"{field}.attempted must be a boolean")
    counts = (
        "candidate_count",
        "source_rejected_count",
        "duplicate_count",
        "normalized_changed_field_count",
        "normalized_emptied_field_count",
        "duration_ms",
    )
    normalized = {
        key: _exact_int(value[key], f"{field}.{key}")
        for key in counts
    }
    _exact_keys(value["detail_quality"], _DETAIL_KEYS, f"{field}.detail_quality")
    normalized["detail_quality"] = {
        key: _exact_int(
            value["detail_quality"][key],
            f"{field}.detail_quality.{key}",
        )
        for key in _DETAIL_KEYS
    }
    error_codes = value["error_codes"]
    if (
        type(error_codes) is not list
        or any(
            type(code) is not str or code not in _PUBLIC_SOURCE_ERROR_CODES
            for code in error_codes
        )
        or error_codes != sorted(set(error_codes))
    ):
        raise _canonical_error(f"{field}.error_codes is not allowlisted")
    error_count = _exact_int(value["error_count"], f"{field}.error_count")
    if error_count != len(error_codes):
        raise _canonical_error(f"{field}.error_count is inconsistent")
    normalized.update(
        {
            "source_id": source_id,
            "attempted": attempted,
            "error_count": error_count,
            "error_codes": list(error_codes),
        }
    )
    return {key: normalized[key] for key in _SOURCE_KEYS}


def _canonical_counts(value: Any, field: str, allowed: frozenset[str]) -> dict[str, int]:
    if not isinstance(value, Mapping):
        raise _canonical_error(f"{field} must be a mapping")
    if any(type(key) is not str or key not in allowed for key in value):
        raise _canonical_error(f"{field} contains a non-public key")
    return {
        key: _exact_int(item, f"{field}.{key}")
        for key, item in sorted(value.items())
    }


def _canonical_summary(value: Any) -> dict[str, int]:
    _exact_keys(value, _SUMMARY_KEYS, "summary")
    return {
        key: _exact_int(value[key], f"summary.{key}")
        for key in _SUMMARY_KEYS
    }


def _canonical_finding(value: Any, index: int) -> dict[str, Any]:
    field = f"findings[{index}]"
    _exact_keys(value, ("severity", "source_id", "message"), field)
    severity = value["severity"]
    if type(severity) is not str or severity not in _PUBLIC_SEVERITIES:
        raise _canonical_error(f"{field}.severity is not allowlisted")
    source_id = value["source_id"]
    if source_id is not None:
        source_id = _exact_source_id(source_id, f"{field}.source_id")
    message = value["message"]
    if type(message) is not str or message not in _MESSAGES.values():
        raise _canonical_error(f"{field}.message is not allowlisted")
    return {"severity": severity, "source_id": source_id, "message": message}


def _canonical_gate_mapping(gate: Mapping[str, Any]) -> Mapping[str, Any]:
    """Validate and copy only the exact public GateV2 JSON shape."""
    _exact_keys(gate, _PUBLIC_GATE_KEYS, "gate")
    schema_version = gate["schema_version"]
    if type(schema_version) is not int or schema_version != GATE_SCHEMA_VERSION:
        raise _canonical_error("schema_version is invalid")
    command_mode = gate["command_mode"]
    if type(command_mode) is not str or command_mode not in _COMMAND_MODES:
        raise _canonical_error("command_mode is invalid")
    run_date = gate["run_date"]
    if type(run_date) is not str:
        raise _canonical_error("run_date must be a string")
    try:
        parsed_date = date.fromisoformat(run_date)
    except ValueError:
        raise _canonical_error("run_date is not an ISO date") from None
    if parsed_date.isoformat() != run_date:
        raise _canonical_error("run_date is not canonical")
    for key in (
        "pipeline_schema_version",
        "score_schema_version",
        "disposition_schema_version",
    ):
        if type(gate[key]) is not int or gate[key] < 0:
            raise _canonical_error(f"{key} must be an integer")
    status = gate["status"]
    if type(status) is not str or status not in _PUBLIC_STATUSES:
        raise _canonical_error("status is invalid")
    context_status = gate["context_status"]
    if type(context_status) is not str or context_status not in _CONTEXT_STATUSES:
        raise _canonical_error("context_status is invalid")

    report = _canonical_report(gate["report"])
    sources = gate["sources"]
    if type(sources) is not list:
        raise _canonical_error("sources must be a list")
    normalized_sources = [
        _canonical_source(item, index)
        for index, item in enumerate(sources)
    ]
    if [item["source_id"] for item in normalized_sources] != sorted(
        item["source_id"] for item in normalized_sources
    ):
        raise _canonical_error("sources are not sorted")

    summary = _canonical_summary(gate["summary"])
    eligibility_counts = _canonical_counts(
        gate["eligibility_reason_counts"],
        "eligibility_reason_counts",
        _PUBLIC_GATE_REASON_CODES,
    )
    manual_counts = _canonical_counts(
        gate["manual_reason_counts"],
        "manual_reason_counts",
        _PUBLIC_GATE_REASON_CODES,
    )
    findings = gate["findings"]
    if type(findings) is not list:
        raise _canonical_error("findings must be a list")
    normalized_findings = [
        _canonical_finding(item, index)
        for index, item in enumerate(findings)
    ]
    invariants = gate["invariants"]
    if (
        type(invariants) is not list
        or any(
            type(item) is not str or item not in _PUBLIC_INVARIANTS
            for item in invariants
        )
        or invariants != sorted(set(invariants))
    ):
        raise _canonical_error("invariants are not allowlisted")
    _canonical_semantics(
        gate,
        report,
        normalized_sources,
        summary,
        eligibility_counts,
        manual_counts,
        list(invariants),
        normalized_findings,
    )
    return {
        "schema_version": schema_version,
        "command_mode": command_mode,
        "run_date": run_date,
        "pipeline_schema_version": gate["pipeline_schema_version"],
        "score_schema_version": gate["score_schema_version"],
        "disposition_schema_version": gate["disposition_schema_version"],
        "status": status,
        "context_status": context_status,
        "report": report,
        "sources": normalized_sources,
        "summary": summary,
        "eligibility_reason_counts": eligibility_counts,
        "manual_reason_counts": manual_counts,
        "invariants": list(invariants),
        "findings": normalized_findings,
    }
def _canonical_summary_consistent(
    summary: Mapping[str, int],
    sources: list[dict[str, Any]],
) -> bool:
    accepted = sum(item["candidate_count"] for item in sources)
    rejected = sum(item["source_rejected_count"] for item in sources)
    duplicates = sum(item["duplicate_count"] for item in sources)
    detail_verified = sum(item["detail_quality"]["verified"] for item in sources)
    detail_manual = sum(item["detail_quality"]["manual_only"] for item in sources)
    detail_rejected = sum(item["detail_quality"]["rejected"] for item in sources)
    return (
        summary["source_accepted"] == accepted
        and summary["source_rejected"] == rejected
        and summary["collected"] == accepted + rejected
        and summary["duplicates_removed"] == duplicates
        and summary["deduplicated"] == max(0, accepted - duplicates)
        and accepted - duplicates == detail_verified + detail_manual
        and rejected == detail_rejected
        and summary["actionable_total"] == summary["apply_total"] + summary["hold_total"]
        and summary["displayed_apply"] + summary["suppressed_apply"] == summary["apply_total"]
        and summary["displayed_hold"] + summary["suppressed_hold"] == summary["hold_total"]
        and summary["displayed_manual"] + summary["suppressed_manual"] == summary["manual_review_total"]
        and summary["displayed_manual"] <= summary["manual_review_total"]
    )


def _canonical_expected_invariants(
    gate: Mapping[str, Any],
    report: Mapping[str, Any],
) -> list[str]:
    report_valid = (
        report["generated"]
        and report["content_sha256"] is not None
        and report["queue_parity"]
    )
    return sorted(
        {
            "pipeline_schema_v2"
            if gate["pipeline_schema_version"] == PIPELINE_RESULT_SCHEMA_VERSION
            else "pipeline_schema_invalid",
            "score_schema_v2",
            "disposition_schema_v2",
            "source_rows_sorted",
            "reason_counts_sorted",
            "summary_projection_consistent",
            "report_artifact_valid" if report_valid else "report_artifact_unavailable",
            "queue_parity" if report["queue_parity"] else "queue_parity_unavailable",
        }
    )


def _canonical_semantics(
    gate: Mapping[str, Any],
    report: Mapping[str, Any],
    sources: list[dict[str, Any]],
    summary: Mapping[str, int],
    eligibility_counts: Mapping[str, int],
    manual_counts: Mapping[str, int],
    invariants: list[str],
    findings: list[dict[str, Any]],
) -> None:
    if len({item["source_id"] for item in sources}) != len(sources):
        raise _canonical_error("sources contain duplicate IDs")
    if gate["score_schema_version"] != SCORE_SCHEMA_VERSION:
        raise _canonical_error("score_schema_version is incompatible")
    if gate["disposition_schema_version"] != DISPOSITION_SCHEMA_VERSION:
        raise _canonical_error("disposition_schema_version is incompatible")
    expected_invariants = _canonical_expected_invariants(gate, report)
    if invariants != expected_invariants:
        raise _canonical_error("invariants contradict GateV2 fields")
    if not _canonical_summary_consistent(summary, sources):
        raise _canonical_error("summary contradicts sources")
    if any(count > summary["manual_review_total"] for count in manual_counts.values()):
        raise _canonical_error("manual reason counts contradict summary")
    if findings != sorted(
        findings,
        key=lambda item: (item["severity"], item["source_id"] or "", item["message"]),
    ):
        raise _canonical_error("findings are not sorted")
    if any(not item["attempted"] and item["candidate_count"] > 0 for item in sources):
        raise _canonical_error("source attempt state contradicts candidate count")
    fail_findings = any(item["severity"] == "fail" for item in findings)
    status = gate["status"]
    if status == "pass":
        if gate["command_mode"] == "scheduled-run" and (
            not sources
            or any(
                item["error_codes"]
                or not item["attempted"]
                or item["candidate_count"] == 0
                for item in sources
            )
        ):
            raise _canonical_error("scheduled pass contains incomplete source state")
        if (
            findings
            or gate["context_status"] != "complete"
            or gate["pipeline_schema_version"] != PIPELINE_RESULT_SCHEMA_VERSION
            or not report["generated"]
            or report["content_sha256"] is None
            or report["byte_length"] <= 0
            or not report["queue_parity"]
        ):
            raise _canonical_error("pass status contradicts GateV2 evidence")
    elif status == "warning":
        if not findings or fail_findings:
            raise _canonical_error("warning status contradicts findings")
    elif not fail_findings:
        raise _canonical_error("fail status has no fail finding")


def canonical_gate_bytes(gate: Mapping[str, Any]) -> bytes:
    """Encode GateV2 in its stated ordered canonical JSON form."""
    return json.dumps(
        _canonical_gate_mapping(gate),
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=False,
    ).encode("utf-8")


def gate_json_sha256(gate: Mapping[str, Any]) -> str:
    return hashlib.sha256(canonical_gate_bytes(gate)).hexdigest()


# Names used by the envelope/storage integration.
gate_bytes = canonical_gate_bytes
canonical_json_bytes = canonical_gate_bytes
gate_json_bytes = canonical_gate_bytes
canonical_bytes = canonical_gate_bytes
build_gate = build_gate_v2

__all__ = [
    "build_gate_v4",
    "canonical_gate_v4_bytes",
    "gate_json_sha256_v4",
    "build_gate_v2",
    "build_gate",
    "canonical_gate_bytes",
    "canonical_json_bytes",
    "gate_json_bytes",
    "canonical_bytes",
    "gate_bytes",
    "gate_json_sha256",
]
