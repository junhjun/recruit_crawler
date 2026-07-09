from __future__ import annotations

import json
from pathlib import Path
from typing import Iterable

from ._status_report_model import (
    FeatureLedger,
    JsonValue,
    ProgressBrief,
    StatusReportCheck,
    feature_records,
    string_list,
    text_field,
)
from ._status_report_render import open_todo_items, render_status_report
from .config import load_config
from .source_registry import source_status_rows

VALID_FEATURE_STATUSES = {"done", "partial", "in_progress", "deferred", "blocked", "excluded", "not_started"}
NON_DONE_STATUSES = VALID_FEATURE_STATUSES - {"done"}


class StatusReportError(ValueError):
    pass


def load_feature_ledger(path: Path) -> FeatureLedger:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise StatusReportError(f"invalid feature ledger JSON: {path}") from exc
    if not isinstance(data, dict):
        raise StatusReportError("feature ledger must be a JSON object")
    features = data.get("features")
    if not isinstance(features, list) or not features:
        raise StatusReportError("feature ledger requires a non-empty features array")
    for feature in features:
        _validate_feature(feature)
    return data


def _validate_feature(feature: JsonValue) -> None:
    if not isinstance(feature, dict):
        raise StatusReportError("each feature must be an object")
    required = [
        "feature_id",
        "name",
        "category",
        "status",
        "user_value",
        "entrypoints",
        "code_refs",
        "test_refs",
        "docs_refs",
        "blockers",
        "next_action",
    ]
    missing = [field for field in required if field not in feature]
    feature_id = str(feature.get("feature_id", "<unknown>"))
    if missing:
        raise StatusReportError(f"feature {feature_id} missing fields: {', '.join(missing)}")
    status = feature["status"]
    if not isinstance(status, str) or status not in VALID_FEATURE_STATUSES:
        raise StatusReportError(f"feature {feature_id} has invalid status: {status}")
    for field in ("entrypoints", "code_refs", "test_refs", "docs_refs", "blockers"):
        if not isinstance(feature[field], list):
            raise StatusReportError(f"feature {feature_id} field {field} must be an array")
    if status == "done" and not feature["test_refs"]:
        raise StatusReportError(f"done feature {feature_id} requires test_refs")
    if status in NON_DONE_STATUSES and not (feature["blockers"] or feature["next_action"]):
        raise StatusReportError(f"non-done feature {feature_id} requires blockers or next_action")


def build_progress_brief(
    *,
    features_path: Path,
    todo_path: Path,
    config_path: Path | None = None,
    output_path: Path | None = None,
    max_items: int = 6,
) -> ProgressBrief:
    feature_ledger = load_feature_ledger(features_path)
    features = feature_records(feature_ledger)
    counts: dict[str, int] = {}
    for feature in features:
        status = text_field(feature, "status")
        counts[status] = counts.get(status, 0) + 1
    ordered_counts = ", ".join(f"{status}={counts[status]}" for status in sorted(counts))
    non_done = [feature for feature in features if text_field(feature, "status") != "done"]
    todos = open_todo_items(todo_path) if todo_path.exists() else []
    status_check: StatusReportCheck | None = None
    if config_path is not None and output_path is not None:
        status_check = check_status_report(
            config_path=config_path,
            features_path=features_path,
            output_path=output_path,
            todo_path=todo_path,
        )
    recommended_next = todos[0] if todos else next(
        (
            text_field(feature, "next_action") or "; ".join(string_list(feature, "blockers"))
            for feature in non_done
            if text_field(feature, "next_action") or string_list(feature, "blockers")
        ),
        "열린 다음 작업 없음",
    )

    lines: list[str] = [
        f"status_date: {feature_ledger.get('updated_at', 'unknown')}",
        f"features: total={len(features)}; {ordered_counts}",
        f"open_todos: {len(todos)}",
        f"non_done: {len(non_done)}",
    ]
    if status_check is not None:
        lines.append(f"tracking: {'ok' if status_check.ok else 'stale'} — {status_check.message}")
    lines.append(f"recommended_next: {recommended_next}")
    for feature in non_done[:max_items]:
        action = text_field(feature, "next_action") or "; ".join(string_list(feature, "blockers")) or "정의 필요"
        lines.append(f"- {text_field(feature, 'status')}: {text_field(feature, 'name')} — {action}")
    if len(non_done) > max_items:
        lines.append(f"- ... {len(non_done) - max_items} more non-done features")

    lines.append(f"next_todos: {min(len(todos), max_items)} shown")
    for item in todos[:max_items]:
        lines.append(f"- {item}")
    if len(todos) > max_items:
        lines.append(f"- ... {len(todos) - max_items} more TODO items")

    lines.append("verify: PYTHONPATH=src python3 -m recruit_crawler.cli status-report --check")
    return ProgressBrief(tuple(lines))


def write_status_report(*, config_path: Path, features_path: Path, output_path: Path, todo_path: Path) -> str:
    content = build_status_report(
        config_path=config_path,
        features_path=features_path,
        todo_path=todo_path,
    )
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(content, encoding="utf-8")
    return content


def build_status_report(*, config_path: Path, features_path: Path, todo_path: Path) -> str:
    feature_ledger = load_feature_ledger(features_path)
    config = load_config(config_path, allow_real_sources=True)
    return render_status_report(
        feature_ledger=feature_ledger,
        source_rows=[dict(row) for row in source_status_rows(config.sources)],
        todo_path=todo_path,
    )


def check_status_report(*, config_path: Path, features_path: Path, output_path: Path, todo_path: Path) -> StatusReportCheck:
    expected = build_status_report(
        config_path=config_path,
        features_path=features_path,
        todo_path=todo_path,
    )
    if not output_path.exists():
        return StatusReportCheck(False, f"missing status report: {output_path}")
    actual = output_path.read_text(encoding="utf-8")
    if actual != expected:
        return StatusReportCheck(False, f"status report is stale; regenerate {output_path}")
    return StatusReportCheck(True, "status report is current")


def iter_feature_refs(feature_ledger: FeatureLedger, fields: Iterable[str]) -> Iterable[tuple[str, str, str]]:
    for feature in feature_records(feature_ledger):
        for field in fields:
            for ref in string_list(feature, field):
                yield text_field(feature, "feature_id"), field, ref
