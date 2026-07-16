from __future__ import annotations

import re
from typing import Optional

from .schemas import (
    PipelineResultV2,
    RenderedReportV2,
    REPORT_ARTIFACT_SCHEMA_VERSION,
)


_REPORT_LINE_CODEPOINT_CAP = 480
_REPORT_LINE_BYTE_CAP = 768
_REPORT_DOCUMENT_CODEPOINT_CAP = 2000
_REPORT_DOCUMENT_BYTE_CAP = 122880

_MANUAL_REASON_ORDER = (
    "manual_flag",
    "manual_source",
    "education_ambiguous",
    "experience_ambiguous",
    "military_program_review",
    "education_unknown",
    "experience_unknown",
)
_PUBLIC_DISPOSITION_LABELS = {
    "apply": "지원 추천",
    "hold": "도전 지원",
    "manual_review": "원문 확인 필요",
    "low_priority": "제외",
    "exclude": "제외",
    "expired": "제외",
}
_PUBLIC_REASON_LABELS = {
    "manual_flag": "수동 확인 필요",
    "manual_source": "원문 확인 필요",
    "education_ambiguous": "학력 요건 확인 필요",
    "experience_ambiguous": "경력 요건 확인 필요",
    "military_program_review": "지원 자격 확인 필요",
    "education_unknown": "학력 정보 확인 필요",
    "experience_unknown": "경력 정보 확인 필요",
}

class ReportRenderError(ValueError):
    """The bounded public report cannot be rendered safely."""


def _manual_report_reason(reason_codes) -> str:
    reasons = set(str(code) for code in reason_codes)
    for reason in _MANUAL_REASON_ORDER:
        if reason in reasons:
            return _PUBLIC_REASON_LABELS[reason]
    return "원문 확인 필요"


def _report_normalize(value: object) -> str:
    import unicodedata

    text = unicodedata.normalize("NFC", str(value))
    return " ".join(text.replace("\r\n", "\n").replace("\r", "\n").split())


def _report_escape(value: object) -> str:
    """Escape one normalized source field exactly once for Markdown."""
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
    return "".join(replacements.get(char, char) for char in _report_normalize(value))


def _report_truncate(value: object, limit: int) -> str:
    text = _report_normalize(value)
    if limit <= 0:
        return ""
    if len(text) <= limit:
        return text
    if limit == 1:
        return "…"
    return text[: limit - 1] + "…"


def _report_line_ok(line: str) -> bool:
    return (
        len(line) <= _REPORT_LINE_CODEPOINT_CAP
        and len(line.encode("utf-8")) <= _REPORT_LINE_BYTE_CAP
    )


def _report_document_ok(lines: list[str]) -> bool:
    content = "\n".join(lines) + "\n"
    return (
        len(content) <= _REPORT_DOCUMENT_CODEPOINT_CAP
        and len(content.encode("utf-8")) <= _REPORT_DOCUMENT_BYTE_CAP
    )
_REPORT_SAFE_SOURCE_ID_RE = re.compile(r"[a-z0-9][a-z0-9_-]{0,63}\Z")
_REPORT_SOURCE_ERROR_CODES = frozenset(
    {
        "collection_error",
        "collection_failed",
        "source_timeout",
        "aggregate_budget_exhausted",
    }
)


def _report_source_id(value: object, configured_canaries=()) -> str | None:
    if not isinstance(value, str) or not _REPORT_SAFE_SOURCE_ID_RE.fullmatch(value):
        return None
    lowered = value.casefold()
    if any(
        token in lowered
        for token in ("private", "canary", "secret", "raw", "military", "path")
    ):
        return None
    import unicodedata

    normalized = tuple(
        unicodedata.normalize("NFC", canary).casefold()
        for canary in (
            configured_canaries
            if not isinstance(configured_canaries, str)
            else (configured_canaries,)
        )
        if isinstance(canary, str) and canary
    )
    if any(canary in unicodedata.normalize("NFC", value).casefold() for canary in normalized):
        return None
    return value


def _report_source_degradation(
    result: PipelineResultV2,
    gate_sources,
    configured_canaries=(),
) -> tuple[list[tuple[str | None, str]], bool]:
    import unicodedata

    normalized = tuple(
        unicodedata.normalize("NFC", canary).casefold()
        for canary in (
            configured_canaries
            if not isinstance(configured_canaries, str)
            else (configured_canaries,)
        )
        if isinstance(canary, str) and canary
    )

    def contaminated(value: object) -> bool:
        return isinstance(value, str) and any(
            canary in unicodedata.normalize("NFC", value).casefold()
            for canary in normalized
        )

    outcomes = {
        getattr(item, "source_id", None): item
        for item in getattr(result, "source_outcomes", ())
    }
    rows: list[tuple[str | None, str]] = []
    degraded = False
    for source in gate_sources:
        source_id = _report_source_id(
            getattr(source, "source_id", None),
            configured_canaries,
        )
        error_codes = {
            code
            for code in getattr(source, "error_codes", ())
            if code in _REPORT_SOURCE_ERROR_CODES and not contaminated(code)
        }
        outcome = outcomes.get(getattr(source, "source_id", None))
        outcome_status = getattr(outcome, "status", "success")
        outcome_code = getattr(outcome, "error_code", None)
        if outcome_status != "success":
            degraded = True
        if outcome_code in _REPORT_SOURCE_ERROR_CODES and not contaminated(outcome_code):
            error_codes.add(outcome_code)
        zero_candidates = getattr(source, "candidate_count", 0) == 0
        if not error_codes and not zero_candidates and outcome_status == "success":
            continue
        degraded = True
        detail = (
            ", ".join(sorted(error_codes))
            if error_codes
            else "수집 결과 없음"
            if zero_candidates
            else "수집 실패"
        )
        rows.append((source_id, "" if contaminated(detail) else detail))
    return rows, degraded


def _fit_row_values(build, fields: dict[str, str], priorities: tuple[str, ...]) -> dict[str, str]:
    values = dict(fields)
    line = build(values)
    for field in priorities:
        if _report_line_ok(line):
            break
        current = values[field]
        low, high = 0, len(current)
        best = ""
        while low <= high:
            middle = (low + high) // 2
            trial = dict(values)
            trial[field] = current[:middle]
            if _report_line_ok(build(trial)):
                best = trial[field]
                high = middle - 1
            else:
                low = middle + 1
        values[field] = best
        line = build(values)
    if not _report_line_ok(line):
        raise ReportRenderError("report row exceeds the public line caps")
    return values


def _shrink_row(build, fields: dict[str, str], priorities: tuple[str, ...]) -> str:
    """Use finite binary searches over raw fields to satisfy both line caps."""
    return build(_fit_row_values(build, fields, priorities))


def _shrink_document(
    fixed_lines: list[str],
    rows: list[tuple[object, dict[str, str], tuple[str, ...]]],
    *,
    row_positions: Optional[list[int]] = None,
) -> list[str]:
    """Reduce raw fields with a finite monotonic budget for both document caps."""
    if row_positions is None:
        row_positions = [len(fixed_lines)] * len(rows)
    if len(row_positions) != len(rows) or any(
        position < 0 or position > len(fixed_lines) for position in row_positions
    ):
        raise ReportRenderError("invalid report row position")

    values = [
        _fit_row_values(build, fields, priorities)
        for build, fields, priorities in rows
    ]

    def render_lines() -> list[str]:
        grouped_rows: dict[int, list[str]] = {}
        for index, position in enumerate(row_positions):
            grouped_rows.setdefault(position, []).append(rows[index][0](values[index]))
        lines: list[str] = []
        for position in range(len(fixed_lines) + 1):
            lines.extend(grouped_rows.get(position, []))
            if position < len(fixed_lines):
                lines.append(fixed_lines[position])
        return lines

    max_steps = sum(len(value) for fields in values for value in fields.values()) + 1
    for _step in range(max_steps):
        lines = render_lines()
        if _report_document_ok(lines):
            return lines
        candidates = [
            (len(value), index, field)
            for index, (_build, _fields, priorities) in enumerate(rows)
            for field in priorities
            for value in (values[index][field],)
            if value
        ]
        if not candidates:
            break
        _length, index, field = max(candidates)
        current = values[index][field]
        # Prefix reduction is monotonic in source length and strictly consumes
        # the finite budget, regardless of codepoint/UTF-8 byte expansion.
        values[index][field] = _report_truncate(current, max(0, len(current) - max(1, len(current) // 2)))
    lines = render_lines()
    if not _report_document_ok(lines):
        raise ReportRenderError("report exceeds document caps")
    return lines


def _render_v2(
    result: PipelineResultV2,
    *,
    private_canaries=(),
) -> RenderedReportV2:
    from hashlib import sha256
    from .projection import project_pipeline_result
    from .report_policy import verified_link_url

    projected = project_pipeline_result(result)
    command_mode = getattr(getattr(result, "command_mode", ""), "value", getattr(result, "command_mode", ""))
    def _verified_queue_url(item: dict[str, object]) -> str:
        link = verified_link_url(
            str(command_mode),
            item.get("source_id", ""),
            item.get("source_url"),
            item.get("source_posting_id"),
            item.get("source_detail_quality", "manual_only"),
        )
        return _report_normalize(link or "")
    summary = projected["summary"]
    fixed_lines = [
        f"# 채용 추천 리포트 — {_report_escape(result.run_date.isoformat())}",
        "",
        "## 한눈에 보기",
        f"- 수집: {summary['collected']}",
        f"- 상세 거부: {summary['source_rejected']}",
        f"- 중복 제거: {summary['duplicates_removed']}",
        f"- 지원 추천: {summary['apply_total']}",
        f"- 도전 지원: {summary['hold_total']}",
        f"- 원문 확인 필요: {summary['manual_review_total']}",
        f"- 제외: {summary['low_priority_total'] + summary['exclude'] + summary['expired']}",
        f"- 표시: 지원 추천 {summary['displayed_apply']}, 도전 지원 {summary['displayed_hold']}, 원문 확인 필요 {summary['displayed_manual']}",
    ]
    source_degradations, has_source_degradation = _report_source_degradation(
        result,
        projected.get("gate_sources", ()),
        private_canaries,
    )
    if has_source_degradation:
        fixed_lines.extend(
            [
                "",
                "## 수집 저하 안내",
                "- 일부 활성 소스의 수집이 완료되지 않았습니다. Gate 상태는 fail입니다.",
            ]
        )
        for source_id, detail in source_degradations:
            label = (
                f"소스 `{_report_escape(source_id)}`"
                if source_id is not None
                else "일부 소스"
            )
            fixed_lines.append(f"- {label}: {_report_escape(detail)}")
    fixed_lines.extend(
        [
            "",
            "## 지원/검토",
            "| 순위 | 판정 | 점수 | 공고 | 회사 | 마감 | 근거 | 사유 | 링크 |",
            "| --- | --- | --- | --- | --- | --- | --- | --- | --- |",
        ]
    )
    action_row_position = len(fixed_lines)
    rows: list[tuple[object, dict[str, str], tuple[str, ...]]] = []
    row_positions: list[int] = []
    for rank, item in enumerate(projected["action_queue"], start=1):
        disposition = str(item["final_disposition"])
        title = _report_truncate(item.get("title", ""), 160)
        company = _report_truncate(item.get("company", ""), 120)
        evidence = _report_truncate(", ".join(str(x) for x in item["matched_evidence"]) or "-", 160)
        reason = _report_truncate(
            ", ".join(_PUBLIC_REASON_LABELS.get(str(x), "지원 조건 확인 필요") for x in item["reason_codes"]) or "-",
            96,
        )
        deadline = _report_normalize(item.get("deadline") or "확인 필요")
        url = _verified_queue_url(item)

        def build_action(
            values,
            *,
            rank=rank,
            disposition=disposition,
            score=item["score"],
            deadline=deadline,
            url=url,
        ):
            posting = _report_escape(values["posting"])
            source_link = f"[열기](<{url}>)" if url else "확인 필요"
            return (
                f"| {rank} | {_report_escape(_PUBLIC_DISPOSITION_LABELS.get(disposition, '원문 확인 필요'))} "
                f"| {score} | {posting} | {_report_escape(values['company'])} | "
                f"{_report_escape(deadline)} | {_report_escape(values['evidence'])} | "
                f"{_report_escape(values['reason'])} | {source_link} |"
            )

        rows.append(
            (
                build_action,
                {
                    "posting": title,
                    "company": company,
                    "evidence": evidence,
                    "reason": reason,
                },
                ("posting", "company", "evidence", "reason"),
            )
        )
        row_positions.append(action_row_position)

    if not rows:
        fixed_lines.append("| - | 없음 | - | - | - | - | - | - | - |")

    if summary["manual_review_total"]:
        fixed_lines.extend(
            [
                "",
                "## 원문 확인 필요",
                "| 순위 | 사유 | 공고 | 링크 |",
                "| --- | --- | --- | --- |",
            ]
        )
        manual_row_position = len(fixed_lines)
        if projected["manual_queue"]:
            for rank, item in enumerate(projected["manual_queue"], start=1):
                title = _report_truncate(
                    f"{item.get('title', '')} - {item.get('company', '')}".strip(" -"), 160
                )
                reason = _manual_report_reason(item["reason_codes"])
                url = _verified_queue_url(item)
                fields = {"title": title, "reason": reason}

                def build_manual(values, *, rank=rank, url=url):
                    link = f"[열기](<{url}>)" if url else "확인 필요"
                    return (
                        f"| {rank} | {_report_escape(values['reason'])} | "
                        f"{_report_escape(values['title'])} | {link} |"
                    )

                rows.append((build_manual, fields, ("title", "reason")))
                row_positions.append(manual_row_position)
        else:
            fixed_lines.append("| - | 없음 | - | - |")

    fixed_lines.extend(
        [
            "",
            "## 제외",
            f"- 표시하지 않은 지원 추천/도전 지원: {summary['suppressed_apply'] + summary['suppressed_hold']}",
            f"- 표시하지 않은 원문 확인 필요: {summary['suppressed_manual']}",
            "- 낮은 우선순위 상세와 원문은 저장하거나 출력하지 않습니다.",
        ]
    )

    # Rows are rendered from raw structured fields and escaped exactly once.
    lines = _shrink_document(fixed_lines, rows, row_positions=row_positions)
    if any(not _report_line_ok(line) for line in lines):
        raise ReportRenderError("report contains an overlong fixed line")
    content = "\n".join(lines) + "\n"
    encoded = content.encode("utf-8")
    if not content.endswith("\n") or not _report_document_ok(lines):
        raise ReportRenderError("report exceeds document caps")
    rendered = RenderedReportV2(
        schema_version=REPORT_ARTIFACT_SCHEMA_VERSION,
        markdown_bytes=encoded,
        content_sha256=sha256(encoded).hexdigest(),
        byte_length=len(encoded),
    )
    if rendered.byte_length != len(rendered.markdown_bytes):
        raise ReportRenderError("report byte-length invariant failed")
    return rendered


def render_report_v3(
    result: PipelineResultV2,
    *,
    private_canaries=(),
) -> RenderedReportV2:
    """Render a bounded Korean-only V3 report into canonical UTF-8 bytes."""
    return _render_v2(result, private_canaries=private_canaries)


def render_report_v2(
    result: PipelineResultV2,
    *,
    private_canaries=(),
) -> RenderedReportV2:
    """Compatibility name routed through the validated V3 renderer."""
    return render_report_v3(result, private_canaries=private_canaries)


render_markdown_report_v2 = render_report_v3
render_pipeline_report = render_report_v3
render_report = render_report_v3