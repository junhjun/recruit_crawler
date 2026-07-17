from __future__ import annotations

"""Pure public projection and report-input helpers for pipeline v2.

This module deliberately accepts only frozen v2 records and returns ordinary
public mappings.  Raw structured data, eligibility evidence, and the opaque
sort key never cross this boundary.
"""

from collections import Counter
from datetime import date
import re
from typing import Any, Iterable, Mapping, Sequence
from .report_policy import project_report_presentation, verified_link_url

from .schemas import (
    AssessmentV2,
    GateSourceV2,
    PipelineResultV2,
    ReportArtifactV2,
    RenderedReportV2,
    REPORT_ARTIFACT_SCHEMA_VERSION,
)


_UNSAFE_PUBLIC_RE = re.compile(
    r"(?:\bprivate(?:[\s_-]+[a-z0-9]+)*\b|\bcanary\b|"
    r"\braw(?:[\s_-]+(?:jd|profile|resume|cv|data|text|source))+\b|"
    r"(?:private|raw)_[a-z0-9_]*canary\b|"
    r"\bpersonal(?:[\s_-]*(?:info|data|profile))?\b|"
    r"ignore\s+previous\s+instructions|system\s+prompt|developer\s+message|"
    r"access[_ -]?token|session[_ -]?token)",
    re.I,
)
_MILITARY_RE = re.compile(
    r"(?:군\s*(?:필|미필|복무)|군대|군사|병역|보충역|산업기능요원|전문연구요원|"
    r"현역|예비역|military|army|veteran)",
    re.I,
)
_SAFE_CODE_RE = re.compile(r"[a-z0-9][a-z0-9_.-]{0,63}\Z")
_SAFE_REASON_CODES = frozenset(
    {
        "invalid_candidate",
        "expired",
        "dealbreaker",
        "education_mismatch",
        "experience_mismatch",
        "manual_flag",
        "manual_source",
        "education_ambiguous",
        "experience_ambiguous",
        "education_unknown",
        "experience_unknown",
    }
)
_PUBLIC_SOURCE_ERROR_CODES = frozenset(
    {
        "collection_error",
        "collection_failed",
        "invalid_candidate",
        "detail_issue_invalid",
        "detail_url_invalid",
        "detail_fetch_failed",
        "detail_unverified",
    }
)
_PUBLIC_EVIDENCE_LABELS = (
    ("필수 요건:", "필수 요건 일치"),
    ("담당 업무:", "담당 업무 관련성"),
    ("우대 요건:", "우대 요건 일치"),
    ("선호 직무:", "선호 직무 일치"),
    ("근무지:", "근무지 조건 일치"),
)
_REASON_CODE_MAP = {"military_program_review": "manual_flag"}
_SAFE_DISPOSITIONS = frozenset(
    {"apply", "hold", "manual_review", "low_priority", "exclude", "expired"}
)
_MANUAL_DISPOSITION = "manual_review"
_ACTION_DISPOSITIONS = {"apply", "hold"}
_MANUAL_REASONS = (
    "manual_flag",
    "manual_source",
    "education_ambiguous",
    "experience_ambiguous",
    "military_program_review",
    "education_unknown",
    "experience_unknown",
)
_REPORT_LABEL_PRIORITY = {
    "apply": 0,
    "hold": 1,
    "manual_review": 2,
    "low_priority": 3,
    "exclude": 4,
    "expired": 5,
}


def _iso(value: Any) -> Any:
    if value is None or isinstance(value, date):
        return value.isoformat() if isinstance(value, date) else None
    return None


def _public_text(value: Any, *, fallback: str, maximum_length: int = 120) -> str:
    """Fail closed for uncontrolled text at the first public boundary."""
    if (
        not isinstance(value, str)
        or not value.strip()
        or len(value) > maximum_length
        or _UNSAFE_PUBLIC_RE.search(value)
        or _MILITARY_RE.search(value)
    ):
        return fallback
    return value


def _public_values(value: Any) -> tuple[Any, ...]:
    if value is None or isinstance(value, (str, bytes, bytearray)):
        return ()
    try:
        return tuple(value)
    except TypeError:
        return ()


def _public_evidence(value: Any) -> tuple[str, ...]:
    """Replace scorer evidence excerpts with bounded public categories."""
    projected = []
    for item in _public_values(value):
        text = _public_text(item, fallback="", maximum_length=320)
        label = next(
            (
                candidate
                for prefix, candidate in _PUBLIC_EVIDENCE_LABELS
                if text.startswith(prefix)
            ),
            "",
        )
        if label and label not in projected:
            projected.append(label)
    return tuple(projected)


def _public_reason_codes(value: Any) -> tuple[str, ...]:
    projected = []
    for item in _public_values(value):
        if not isinstance(item, str):
            continue
        code = _REASON_CODE_MAP.get(item, item)
        if code not in _SAFE_REASON_CODES or not _SAFE_CODE_RE.fullmatch(code):
            continue
        if code not in projected:
            projected.append(code)
    return tuple(projected)


def _public_identifier(value: Any, *, fallback: str = "") -> str:
    return _public_text(value, fallback=fallback)


def _public_error_codes(value: Any) -> tuple[str, ...]:
    """Revalidate source metric errors against the fixed public vocabulary."""
    return tuple(
        sorted(
            {
                item
                for item in _public_values(value)
                if isinstance(item, str) and item in _PUBLIC_SOURCE_ERROR_CODES
            }
        )
    )


def project_public_assessment(
    assessment: AssessmentV2,
    *,
    command_mode: str | None = None,
) -> dict[str, Any]:
    """Return the exact allowlisted public shape for one assessment."""
    source_detail_quality = getattr(assessment, "detail_quality", "manual_only")
    projected = {
        "recommendation_id": _public_identifier(
            getattr(assessment, "recommendation_id", ""),
        ),
        "posting_key": _public_identifier(getattr(assessment, "posting_key", "")),
        "source_id": _public_identifier(
            getattr(assessment, "source_id", ""),
            fallback="unknown-source",
        ),
        "source_url": None,
        "source_posting_id": (
            _public_identifier(getattr(assessment, "source_posting_id", ""))
            or None
        ),
        "title": _public_text(
            getattr(assessment, "title", ""),
            fallback="검토 필요 공고",
        ),
        "company": _public_text(
            getattr(assessment, "company", ""),
            fallback="확인 필요",
            maximum_length=80,
        ),
        "location": _public_text(
            getattr(assessment, "location", ""),
            fallback="확인 필요",
            maximum_length=80,
        ),
        "deadline": _iso(getattr(assessment, "deadline", None)),
        "score": (
            getattr(assessment, "score", 0)
            if isinstance(getattr(assessment, "score", 0), int)
            and not isinstance(getattr(assessment, "score", 0), bool)
            else 0
        ),
        "final_disposition": (
            getattr(assessment, "disposition", _MANUAL_DISPOSITION)
            if getattr(assessment, "disposition", _MANUAL_DISPOSITION)
            in _SAFE_DISPOSITIONS
            else _MANUAL_DISPOSITION
        ),
        "reason_codes": list(
            _public_reason_codes(getattr(assessment, "reason_codes", ()))
        ),
        "source_detail_quality": (
            source_detail_quality
            if source_detail_quality in {"verified", "manual_only", "rejected"}
            else "manual_only"
        ),
        "matched_evidence": list(
            _public_evidence(getattr(assessment, "matched_evidence", ()))
        ),
    }
    mode = getattr(command_mode, "value", command_mode)
    link_url = verified_link_url(
        str(mode) if mode is not None else "",
        getattr(assessment, "source_id", ""),
        getattr(assessment, "source_url", None),
        getattr(assessment, "source_posting_id", None),
        source_detail_quality,
    )
    if link_url:
        projected["source_url"] = link_url
    return projected


project_assessment_v2 = project_public_assessment


def project_public_assessments(
    assessments: Iterable[AssessmentV2],
    *,
    command_mode: str | None = None,
) -> tuple[dict[str, Any], ...]:
    """Project every assessment without filtering or early truncation."""
    return tuple(
        project_public_assessment(item, command_mode=command_mode)
        for item in assessments
    )


def _sort_key(assessment: AssessmentV2) -> tuple[Any, ...]:
    unknown = assessment.deadline is None or assessment.deadline_uncertain
    deadline = assessment.deadline.isoformat() if assessment.deadline else ""
    return (
        -int(assessment.score),
        int(unknown),
        deadline,
        assessment.source_id,
        assessment.source_posting_id
        or assessment.source_url
        or f"{assessment.title}\0{assessment.company}",
    )




def _reason_counts(assessments: Iterable[AssessmentV2]) -> dict[str, int]:
    counts: Counter[str] = Counter()
    for assessment in assessments:
        counts.update(_public_reason_codes(getattr(assessment, "reason_codes", ())))
    return {key: counts[key] for key in sorted(counts)}


def _manual_reason_counts(assessments: Iterable[AssessmentV2]) -> dict[str, int]:
    counts: Counter[str] = Counter()
    for assessment in assessments:
        if getattr(assessment, "disposition", None) != _MANUAL_DISPOSITION:
            continue
        for code in _public_reason_codes(getattr(assessment, "reason_codes", ())):
            if code in _MANUAL_REASONS:
                counts[code] += 1
    return {key: counts[key] for key in sorted(counts)}


def _summary(result: PipelineResultV2, assessments: Sequence[AssessmentV2]) -> dict[str, int]:
    by_disposition = Counter(item.disposition for item in assessments)
    accepted = sum(int(metric.accepted_count) for metric in result.source_metrics)
    actionable = by_disposition["apply"] + by_disposition["hold"]
    return {
        "collected": int(result.collected_count),
        "source_rejected": int(result.source_rejected_count),
        "source_accepted": accepted,
        "duplicates_removed": int(result.duplicates_removed),
        "deduplicated": max(0, accepted - int(result.duplicates_removed)),
        "expired": by_disposition["expired"],
        "exclude": by_disposition["exclude"],
        "manual_review_total": by_disposition["manual_review"],
        "apply_total": by_disposition["apply"],
        "hold_total": by_disposition["hold"],
        "low_priority_total": by_disposition["low_priority"],
        "actionable_total": actionable,
        "displayed_apply": by_disposition["apply"],
        "displayed_hold": by_disposition["hold"],
        "suppressed_apply": 0,
        "suppressed_hold": 0,
        "displayed_manual": by_disposition["manual_review"],
        "suppressed_manual": 0,
    }


def project_gate_sources(result: PipelineResultV2) -> tuple[GateSourceV2, ...]:
    """Convert source metrics to the sole allowlisted Gate-source shape."""
    projected = []
    metrics = getattr(result, "source_metrics", result)
    for metric in sorted(metrics, key=lambda item: item.source_id):
        errors = _public_error_codes(getattr(metric, "error_codes", ()))
        projected.append(
            GateSourceV2(
                source_id=metric.source_id,
                attempted=bool(metric.attempted),
                candidate_count=int(metric.accepted_count),
                source_rejected_count=int(metric.rejected_count),
                duplicate_count=int(metric.duplicate_count),
                normalized_changed_field_count=int(metric.normalized_changed_field_count),
                normalized_emptied_field_count=int(metric.normalized_emptied_field_count),
                detail_quality=(
                    ("verified", int(metric.verified_count)),
                    ("manual_only", int(metric.manual_only_count)),
                    ("rejected", int(metric.rejected_count)),
                ),
                error_count=len(errors),
                error_codes=errors,
                duration_ms=int(metric.duration_ms),
            )
        )
    return tuple(projected)
gate_source_projections = project_gate_sources


def summarize_pipeline_result(result: PipelineResultV2) -> dict[str, int]:
    return _summary(result, tuple(result.all_assessments))


summary_for_result = summarize_pipeline_result
def _report_queue_item(
    assessment: AssessmentV2,
    *,
    command_mode: str,
) -> dict[str, Any]:
    item = project_public_assessment(assessment, command_mode=command_mode)
    presentation = project_report_presentation(assessment, command_mode=command_mode)
    # Queue mappings are transient report inputs.  Keep the persisted
    # assessment allowlist unchanged, while ensuring report writers cannot
    # turn an unverified or disallowed URL into a clickable link.
    link_url = presentation["link_url"]
    if link_url:
        item["source_url"] = link_url
    else:
        item["source_url"] = None
    return item


def _report_sort_key(item: Mapping[str, Any]) -> tuple[Any, ...]:
    disposition = str(item.get("final_disposition", _MANUAL_DISPOSITION))
    score = item.get("score", 0)
    if type(score) is not int:
        score = 0
    deadline = item.get("deadline")
    return (
        _REPORT_LABEL_PRIORITY.get(disposition, _REPORT_LABEL_PRIORITY[_MANUAL_DISPOSITION]),
        -score,
        deadline is None,
        deadline or "",
        item.get("recommendation_id") or "unknown-recommendation",
        item.get("posting_key") or "unknown-posting",
    )

def project_pipeline_result(result: PipelineResultV2) -> dict[str, Any]:
    """Build reusable public projections with report ordering independent of input."""
    assessments = tuple(result.all_assessments)
    command_mode = getattr(result.command_mode, "value", result.command_mode)
    public = project_public_assessments(assessments, command_mode=str(command_mode))
    report_queue = tuple(
        sorted(
            (
                _report_queue_item(item, command_mode=str(command_mode))
                for item in assessments
            ),
            key=_report_sort_key,
        )
    )
    keys = tuple(_report_sort_key(item) for item in report_queue)
    if len(set(keys)) != len(keys):
        raise ValueError("public report ordering collision")
    # Legacy consumers retain the historical score-first slice shape.  Reports
    # consume only the complete report_queue above.
    legacy_ordered = tuple(sorted(assessments, key=_sort_key))
    action_queue = tuple(
        _report_queue_item(item, command_mode=str(command_mode))
        for item in legacy_ordered
        if item.disposition in _ACTION_DISPOSITIONS
    )[: max(int(result.top_n), 0)]
    manual_queue = tuple(
        _report_queue_item(item, command_mode=str(command_mode))
        for item in legacy_ordered
        if item.disposition == _MANUAL_DISPOSITION
    )[: max(int(result.manual_review_n), 0)]
    return {
        "assessments": public,
        "report_queue": report_queue,
        "action_queue": action_queue,
        "manual_queue": manual_queue,
        "summary": _summary(result, assessments),
        "reason_counts": _reason_counts(assessments),
        "manual_reason_counts": _manual_reason_counts(assessments),
        "gate_sources": project_gate_sources(result),
    }


build_projection = project_pipeline_result
project_result_v2 = project_pipeline_result


def false_report_artifact() -> ReportArtifactV2:
    """Return the explicit no-report artifact used by failed/blocked runs."""
    return ReportArtifactV2(
        schema_version=REPORT_ARTIFACT_SCHEMA_VERSION,
        generated=False,
        path=None,
        rendered=None,
    )


__all__ = [
    "build_projection",
    "false_report_artifact",
    "gate_source_projections",
    "project_assessment_v2",
    "project_gate_sources",
    "project_pipeline_result",
    "project_public_assessment",
    "project_public_assessments",
    "project_result_v2",
    "summarize_pipeline_result",
    "summary_for_result",
]
