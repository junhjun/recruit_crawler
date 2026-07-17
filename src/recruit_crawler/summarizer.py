"""Render the one public-safe recruiting report table."""
from __future__ import annotations

from hashlib import sha256
import unicodedata
from typing import Any, Iterable

from .report_policy import (
    MAX_DEGRADATION_NOTICE_BYTES,
    MAX_REPORT_RANK_DIGITS,
    MAX_REPORT_ROW_BYTES,
    REPORT_TABLE_COLUMNS,
    report_byte_budget,
    validate_degradation_notice_capacity,
    validate_report_queue_capacity,
    verified_link_url,
)
from .schemas import PipelineResultV2, REPORT_ARTIFACT_SCHEMA_VERSION, RenderedReportV2

_PUBLIC_SOURCE_ERROR_CODES = frozenset(
    {"collection_error", "collection_failed", "source_timeout", "aggregate_budget_exhausted"}
)
_PUBLIC_LABELS = {
    "apply": "지원 추천",
    "hold": "도전 지원",
    "manual_review": "원문 확인 필요",
    "low_priority": "제외",
    "exclude": "제외",
    "expired": "제외",
}
_PUBLIC_REASONS = {
    "manual_flag": "수동 확인 필요",
    "manual_source": "원문 확인 필요",
    "education_ambiguous": "학력 요건 확인 필요",
    "experience_ambiguous": "경력 요건 확인 필요",
    "education_unknown": "학력 정보 확인 필요",
    "experience_unknown": "경력 정보 확인 필요",
    "expired": "마감일 확인 필요",
    "dealbreaker": "지원 조건 확인 필요",
    "education_mismatch": "지원 조건 확인 필요",
    "experience_mismatch": "지원 조건 확인 필요",
}


class ReportRenderError(ValueError):
    """The complete public report cannot be rendered within policy limits."""


def _normalized(value: object) -> str:
    return " ".join(unicodedata.normalize("NFC", str(value)).replace("\r", "\n").split())


def _escape(value: object) -> str:
    replacements = {
        "\\": "\\\\",
        "&": "&amp;",
        "<": "&lt;",
        ">": "&gt;",
        "|": "\\|",
        "[": "\\[",
        "]": "\\]",
        "(": "\\(",
        ")": "\\)",
    }
    return "".join(replacements.get(char, char) for char in _normalized(value))


def _configured_canary(value: object, canaries: tuple[str, ...]) -> bool:
    if not isinstance(value, str):
        return False
    normalized = unicodedata.normalize("NFC", value).casefold()
    return any(canary and canary in normalized for canary in canaries)


def _safe_source_id(value: object, canaries: tuple[str, ...]) -> str | None:
    if not isinstance(value, str) or not value or len(value) > 64:
        return None
    if not value.replace("_", "").replace("-", "").isalnum():
        return None
    if _configured_canary(value, canaries):
        return None
    return value


def _safe_degradation_tokens(
    result: PipelineResultV2, gate_sources: Iterable[object], canaries: tuple[str, ...]
) -> tuple[tuple[str | None, str], ...]:
    outcomes = {getattr(item, "source_id", None): item for item in getattr(result, "source_outcomes", ())}
    notices: list[tuple[str | None, str]] = []
    for source in gate_sources:
        source_id = getattr(source, "source_id", None)
        outcome = outcomes.get(source_id)
        status = getattr(outcome, "status", "success")
        codes = set(
            code
            for code in getattr(source, "error_codes", ())
            if code in _PUBLIC_SOURCE_ERROR_CODES
        )
        outcome_code = getattr(outcome, "error_code", None)
        if outcome_code in _PUBLIC_SOURCE_ERROR_CODES:
            codes.add(outcome_code)
        zero_candidates = getattr(source, "candidate_count", 0) == 0
        if status == "success" and not codes and not zero_candidates:
            continue
        code = sorted(codes)[0] if codes else "수집 결과 없음" if zero_candidates else "수집 실패"
        source_token = _safe_source_id(source_id, canaries)
        source_bytes = len((source_token or "일부 소스").encode("utf-8"))
        code_bytes = len(code.encode("utf-8"))
        if source_bytes + code_bytes + 16 > MAX_DEGRADATION_NOTICE_BYTES:
            raise ReportRenderError("report degradation notice exceeds capacity")
        notices.append((source_token, code))
    return tuple(notices)


def _reason(item: dict[str, Any]) -> str:
    for code in item.get("reason_codes", ()):
        if code in _PUBLIC_REASONS:
            return _PUBLIC_REASONS[code]
    return "지원 조건 확인 필요"


def _row(
    rank: int, item: dict[str, Any], command_mode: str
) -> str:
    if len(str(rank)) > MAX_REPORT_RANK_DIGITS:
        raise ReportRenderError("report rank exceeds capacity")
    disposition = str(item.get("final_disposition", "manual_review"))
    url = verified_link_url(
        command_mode,
        item.get("source_id", ""),
        item.get("source_url"),
        item.get("source_posting_id"),
        item.get("source_detail_quality", "manual_only"),
    )
    link = f"[열기](<{url}>)" if url else "확인 필요"
    line = (
        f"| {rank} | {_escape(_PUBLIC_LABELS.get(disposition, '원문 확인 필요'))} | "
        f"{_escape(item.get('title') or '검토 필요 공고')} | "
        f"{_escape(item.get('company') or '확인 필요')} | "
        f"{_escape(item.get('location') or '확인 필요')} | "
        f"{_escape(item.get('deadline') or '확인 필요')} | {_escape(_reason(item))} | {link} |"
    )
    if len(line.encode("utf-8")) > MAX_REPORT_ROW_BYTES:
        raise ReportRenderError("report row exceeds capacity")
    return line


def _render_v2(result: PipelineResultV2, *, private_canaries=()) -> RenderedReportV2:
    from .projection import project_pipeline_result

    canary_values = (private_canaries,) if isinstance(private_canaries, str) else private_canaries
    canaries = tuple(
        unicodedata.normalize("NFC", value).casefold()
        for value in canary_values
        if isinstance(value, str) and value
    )
    projected = project_pipeline_result(result)
    queue = tuple(projected["report_queue"])
    notices = _safe_degradation_tokens(result, projected.get("gate_sources", ()), canaries)
    # Both counts and the whole budget are checked before a Markdown string is built.
    try:
        validate_report_queue_capacity(len(queue))
        validate_degradation_notice_capacity(len(notices))
        budget = report_byte_budget(len(queue), len(notices))
    except ValueError as exc:
        raise ReportRenderError(str(exc)) from exc

    summary = projected["summary"]
    lines = [
        f"# 채용 추천 리포트 — {_escape(result.run_date.isoformat())}",
        "",
        "## 한눈에 보기",
        f"- 수집: {summary.get('collected', 0)}",
        f"- 상세 거부: {summary.get('source_rejected', 0)}",
        f"- 중복 제거: {summary.get('duplicates_removed', 0)}",
        "",
        "## 지원/검토",
        "| " + " | ".join(REPORT_TABLE_COLUMNS) + " |",
        "| " + " | ".join("---" for _ in REPORT_TABLE_COLUMNS) + " |",
    ]
    command_mode = str(getattr(getattr(result, "command_mode", ""), "value", result.command_mode))
    for rank, item in enumerate(queue, start=1):
        lines.append(_row(rank, item, command_mode))
    if notices:
        lines.extend(("", "## 수집 저하 안내", "- 일부 활성 소스의 수집이 완료되지 않았습니다. Gate 상태는 fail입니다."))
        lines.extend(
            f"- 소스 `{_escape(source_id) if source_id else '일부 소스'}`: {_escape(code)}"
            for source_id, code in notices
        )
    markdown = "\n".join(lines) + "\n"
    encoded = markdown.encode("utf-8")
    if len(encoded) > budget:
        raise ReportRenderError("report exceeds capacity budget")
    return RenderedReportV2(
        schema_version=REPORT_ARTIFACT_SCHEMA_VERSION,
        markdown_bytes=encoded,
        content_sha256=sha256(encoded).hexdigest(),
        byte_length=len(encoded),
    )


def render_report_v3(result: PipelineResultV2, *, private_canaries=()) -> RenderedReportV2:
    return _render_v2(result, private_canaries=private_canaries)


render_report_v2 = render_report_v3
render_markdown_report_v2 = render_report_v3
render_pipeline_report = render_report_v3
render_report = render_report_v3
