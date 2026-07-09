from __future__ import annotations

from pathlib import Path
from typing import Sequence

from ._status_report_model import FeatureLedger, FeatureRecord, JsonValue, SourceRow, feature_records, string_list, text_field


def render_status_report(
    *,
    feature_ledger: FeatureLedger,
    source_rows: Sequence[SourceRow],
    todo_path: Path,
) -> str:
    features = feature_records(feature_ledger)
    lines: list[str] = []
    lines.append("# Recruit Crawler Status")
    lines.append("")
    lines.append(f"상태일: {feature_ledger.get('updated_at', 'unknown')}")
    lines.append("")
    lines.append("## 제품 한 줄 정의")
    lines.append("")
    lines.append(str(feature_ledger.get("product_summary", "")))
    lines.append("")
    lines.extend(_feature_summary_section(features))
    lines.append("")
    lines.extend(_source_status_section(source_rows))
    lines.append("")
    lines.extend(_gaps_section(features))
    lines.append("")
    lines.extend(_next_work_section(features, todo_path))
    lines.append("")
    lines.append("## 운영 규칙")
    lines.append("")
    lines.append("- 이 문서는 `docs/status/features.json`과 source registry에서 생성되는 현재 상태판입니다.")
    lines.append("- 기능 추가/삭제/상태 변경 시 `features.json`을 먼저 갱신한 뒤 `status-report`로 이 파일을 재생성합니다.")
    lines.append("- `TODO.md`는 앞으로 할 일만 담고, 중요한 제품 결정은 `docs/decisions.md`에 짧게 기록합니다.")
    lines.append("")
    return "\n".join(lines)


def _feature_summary_section(features: Sequence[FeatureRecord]) -> list[str]:
    lines = ["## 기능 구현 현황", ""]
    counts: dict[str, int] = {}
    for feature in features:
        status = text_field(feature, "status")
        counts[status] = counts.get(status, 0) + 1
    ordered_counts = ", ".join(f"{status}: {counts[status]}" for status in sorted(counts))
    lines.append(f"총 {len(features)}개 기능 — {ordered_counts}")
    lines.append("")
    lines.append("| 기능 | 상태 | 범주 | 사용자 가치 | 진입점 | 검증 |")
    lines.append("| --- | --- | --- | --- | --- | --- |")
    for feature in features:
        entrypoints = _join_code(string_list(feature, "entrypoints"), "없음")
        tests = _join_short_refs(string_list(feature, "test_refs"), "없음")
        lines.append(
            "| "
            + " | ".join(
                [
                    _cell(text_field(feature, "name")),
                    f"`{_cell(text_field(feature, 'status'))}`",
                    _cell(text_field(feature, "category")),
                    _cell(text_field(feature, "user_value")),
                    entrypoints,
                    tests,
                ]
            )
            + " |"
        )
    return lines


def _source_status_section(source_rows: Sequence[SourceRow]) -> list[str]:
    lines = ["## Source 상태", ""]
    lines.append("| Source | 상태 | Lane | Automation | Blocker / 다음 작업 |")
    lines.append("| --- | --- | --- | --- | --- |")
    for row in source_rows:
        lane = row.get("target_lane") if row.get("target_lane") is not None else "null"
        blockers = string_list(row, "blockers")
        note = "; ".join(blockers) if blockers else text_field(row, "next_action", "없음")
        lines.append(
            f"| {_cell(row.get('display_name') or row.get('source_id'))} "
            f"| `{_cell(row.get('target_status'))}` "
            f"| `{_cell(lane)}` "
            f"| {_cell(row.get('automation_level'))} "
            f"| {_cell(note)} |"
        )
    return lines


def _gaps_section(features: Sequence[FeatureRecord]) -> list[str]:
    lines = ["## 부족한 것", ""]
    gaps = [feature for feature in features if text_field(feature, "status") != "done"]
    if not gaps:
        return lines + ["현재 `done`이 아닌 기능이 없습니다."]
    lines.append("| 기능 | 상태 | 영향 / 차단 사유 | 다음 작업 |")
    lines.append("| --- | --- | --- | --- |")
    for feature in gaps:
        blockers = string_list(feature, "blockers")
        blocker_text = "; ".join(blockers) if blockers else "명시된 blocker 없음"
        next_action = text_field(feature, "next_action") or "정의 필요"
        lines.append(
            f"| {_cell(text_field(feature, 'name'))} | `{_cell(text_field(feature, 'status'))}` "
            f"| {_cell(blocker_text)} | {_cell(next_action)} |"
        )
    return lines


def _next_work_section(features: Sequence[FeatureRecord], todo_path: Path) -> list[str]:
    lines = ["## 다음 작업", ""]
    status_actions = [
        feature for feature in features if text_field(feature, "status") != "done" and text_field(feature, "next_action")
    ]
    if status_actions:
        lines.append("### Status ledger 기준")
        lines.append("")
        for feature in status_actions:
            lines.append(f"- **{text_field(feature, 'name')}**: {text_field(feature, 'next_action')}")
        lines.append("")
    lines.append("### TODO.md 기준")
    lines.append("")
    if not todo_path.exists():
        lines.append("- `TODO.md` 없음")
        return lines
    todos = open_todo_items(todo_path)
    if not todos:
        lines.append("- 열린 TODO 항목 없음")
    else:
        lines.extend(f"- {item}" for item in todos)
    return lines


def open_todo_items(path: Path) -> list[str]:
    items: list[str] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if stripped.startswith("- [ ] "):
            items.append(stripped[6:])
    return items


def _join_code(values: Sequence[str], fallback: str) -> str:
    if not values:
        return fallback
    return "<br />".join(f"`{_cell(value)}`" for value in values)


def _join_short_refs(values: Sequence[str], fallback: str) -> str:
    if not values:
        return fallback
    shortened = []
    for value in values[:2]:
        shortened.append(f"`{_cell(value.split('::')[-1])}`")
    if len(values) > 2:
        shortened.append(f"+{len(values) - 2}")
    return "<br />".join(shortened)


def _cell(value: JsonValue) -> str:
    return str(value).replace("|", "\\|").replace("\n", " ")
