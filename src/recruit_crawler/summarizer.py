from __future__ import annotations

from collections import Counter
from typing import List, Optional

from .schemas import FitAssessment, RunSummary


RECOMMENDATION_LABELS = {
    "apply": "지원 추천",
    "hold": "보류",
    "low_priority": "낮은 우선순위",
}


def _join(items: List[str], fallback: str = "기재 없음") -> str:
    return ", ".join(items) if items else fallback


def _compact(items: List[str], fallback: str, limit: int = 2) -> str:
    if not items:
        return fallback
    selected = items[:limit]
    suffix = f" 외 {len(items) - limit}개" if len(items) > limit else ""
    return ", ".join(selected) + suffix


def _table_cell(value: str) -> str:
    return value.replace("|", "\\|").replace("\n", " ")


def _deadline_text(assessment: FitAssessment) -> str:
    deadline = assessment.snapshot.deadline
    if deadline:
        return deadline.isoformat()
    return "확인 필요"


def _experience_text(minimum_years: Optional[int]) -> str:
    if minimum_years is None:
        return "신입/경력무관 또는 확인 필요"
    return f"{minimum_years}년 이상"


def _summary_table(assessments: List[FitAssessment]) -> List[str]:
    lines = [
        "| 순위 | 판정 | 점수 | 공고 | 마감 | 바로 볼 이유 |",
        "| --- | --- | ---: | --- | --- | --- |",
    ]
    for index, assessment in enumerate(assessments, start=1):
        snapshot = assessment.snapshot
        recommendation_label = RECOMMENDATION_LABELS.get(
            assessment.recommendation,
            assessment.recommendation,
        )
        reason = _compact(
            assessment.matched_evidence,
            "강한 매칭 신호 없음",
        )
        title = f"[{snapshot.title} - {snapshot.company}]({snapshot.source_url})"
        lines.append(
            "| "
            + " | ".join(
                [
                    str(index),
                    _table_cell(f"{recommendation_label} (`{assessment.recommendation}`)"),
                    str(assessment.score),
                    _table_cell(title),
                    _table_cell(_deadline_text(assessment)),
                    _table_cell(reason),
                ]
            )
            + " |"
        )
    return lines


def render_markdown_report(summary: RunSummary, assessments: List[FitAssessment]) -> str:
    bucket_counts = Counter(assessment.recommendation for assessment in assessments)
    lines = [
        "# 오늘의 채용 후보",
        "",
        f"> {summary.run_date.isoformat()} 기준, 원문을 열기 전에 지원 우선순위를 판단하기 위한 요약입니다.",
        "",
        "## 한눈에 보기",
        "",
        f"- **지원 추천**: {bucket_counts.get('apply', 0)}개",
        f"- **보류**: {bucket_counts.get('hold', 0)}개",
        f"- **낮은 우선순위**: {bucket_counts.get('low_priority', 0)}개",
        f"- 수집 {summary.candidates_collected}개 -> 중복 제외 {summary.duplicates_removed}개 -> 경력 초과 제외 {summary.experience_excluded}개 -> 마감 제외 {summary.expired_excluded}개 -> 최종 {summary.ranked_count}개",
        f"- 확인한 소스: {', '.join(summary.sources_attempted) or '없음'}",
    ]
    if summary.source_errors:
        lines.append(f"- 소스 오류: {len(summary.source_errors)}개")
        lines.extend(f"  - {error}" for error in summary.source_errors)
    lines.append("")

    if not assessments:
        lines.append("진행 가능한 후보 공고가 없습니다.")
        lines.append("")
        return "\n".join(lines)

    lines.extend(
        [
            "## 우선순위 표",
            "",
            *_summary_table(assessments),
            "",
            "## 상세 메모",
            "",
        ]
    )

    for index, assessment in enumerate(assessments, start=1):
        snapshot = assessment.snapshot
        recommendation_label = RECOMMENDATION_LABELS.get(
            assessment.recommendation,
            assessment.recommendation,
        )
        lines.extend(
            [
                f"### {index}. {recommendation_label} · {snapshot.title}",
                "",
                f"**{snapshot.company}** · {snapshot.location or '확인 필요'} · 마감 {_deadline_text(assessment)} · 점수 **{assessment.score}** · `{assessment.recommendation}`",
                "",
                f"[원문 보기]({snapshot.source_url})",
                "",
                "| 항목 | 내용 |",
                "| --- | --- |",
                f"| 필수 요건 | {_table_cell(_join(snapshot.required_qualifications))} |",
                f"| 우대 요건 | {_table_cell(_join(snapshot.preferred_qualifications))} |",
                f"| 최소 경력 | {_table_cell(_experience_text(snapshot.minimum_experience_years))} |",
                f"| 담당 업무 | {_table_cell(_join(snapshot.responsibilities))} |",
                f"| 회사 정보 | {_table_cell(_join(snapshot.company_info))} |",
                f"| 검토 상태 | {_table_cell(_join(snapshot.manual_review_flags, '자동 파싱 가능'))} |",
                "",
                f"- **맞는 부분**: {_compact(assessment.matched_evidence, '강한 프로필 매칭 신호가 없습니다', 3)}",
                f"- **걸리는 부분**: {_compact(assessment.gaps, '주요 필수 요건 공백은 확인되지 않았습니다', 3)}",
                f"- **리스크**: {_compact(assessment.risks, '구조화된 항목 기준 큰 위험 신호는 없습니다', 2)}",
                f"- **확인할 것**: {_compact(assessment.verification_questions, '추가 확인 질문 없음', 2)}",
                f"- **지원 각도**: {assessment.positioning_seed}",
                "",
            ]
        )

    return "\n".join(lines)
