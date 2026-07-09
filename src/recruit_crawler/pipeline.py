from __future__ import annotations

from datetime import date
from typing import Any, Iterable, Optional, Tuple

from .config import ConfigError, load_config
from .dedupe import dedupe_snapshots
from .jd_parser import parse_candidates
from .report_writer import write_report
from .schemas import AppConfig, FitAssessment, PostingCandidate, RunSummary, SourceRunMetric
from .scorer import exceeds_experience_limit, is_expired, rank_snapshots
from .sources.base import build_source_adapter
from .summarizer import render_markdown_report


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


def _source_metrics_from_candidates(
    sources_attempted: list[str],
    candidates: list[PostingCandidate],
    source_errors: list[str],
) -> list[SourceRunMetric]:
    metrics = []
    for source_id in sources_attempted:
        errors = [error for error in source_errors if error.startswith(f"{source_id}:")]
        metrics.append(
            SourceRunMetric(
                source_id=source_id,
                attempted=True,
                candidate_count=sum(1 for candidate in candidates if candidate.source_id == source_id),
                error_count=len(errors),
                errors=errors,
            )
        )
    return metrics


def _rank_candidates(
    config: AppConfig,
    run_date: date,
    candidates: Iterable[PostingCandidate],
    sources_attempted: list[str],
    source_errors: list[str],
    *,
    report_slug: str,
    source_metrics: Optional[list[SourceRunMetric]] = None,
) -> Tuple[RunSummary, str, list[FitAssessment]]:
    candidates = list(candidates)
    source_metrics = source_metrics or _source_metrics_from_candidates(sources_attempted, candidates, source_errors)
    snapshots = parse_candidates(candidates)
    deduped, duplicates_removed = dedupe_snapshots(snapshots)
    experience_eligible = [
        snapshot for snapshot in deduped if not exceeds_experience_limit(snapshot, config)
    ]
    experience_excluded = len(deduped) - len(experience_eligible)
    active = [snapshot for snapshot in experience_eligible if not is_expired(snapshot, run_date)]
    expired_excluded = len(experience_eligible) - len(active)
    ranked = rank_snapshots(active, config)[: config.top_n]

    placeholder_summary = RunSummary(
        run_date=run_date,
        sources_attempted=sources_attempted,
        source_errors=source_errors,
        candidates_collected=len(candidates),
        duplicates_removed=duplicates_removed,
        experience_excluded=experience_excluded,
        expired_excluded=expired_excluded,
        ranked_count=len(ranked),
        report_path=config.output_dir / "pending.md",
        source_metrics=source_metrics,
    )
    content = render_markdown_report(placeholder_summary, ranked)
    report_path = write_report(config.output_dir, run_date, content, report_slug=report_slug)
    summary = RunSummary(
        run_date=run_date,
        sources_attempted=sources_attempted,
        source_errors=source_errors,
        candidates_collected=len(candidates),
        duplicates_removed=duplicates_removed,
        experience_excluded=experience_excluded,
        expired_excluded=expired_excluded,
        ranked_count=len(ranked),
        report_path=report_path,
        source_metrics=source_metrics,
    )
    content = render_markdown_report(summary, ranked)
    report_path.write_text(content, encoding="utf-8")
    return summary, content, ranked


def _run_pipeline(
    config: AppConfig,
    run_date: date,
    *,
    report_slug: str,
) -> Tuple[RunSummary, str, list[FitAssessment]]:
    enabled_sources = [source for source in config.sources if source.enabled]
    sources_attempted = [source.source_id for source in enabled_sources]
    candidates = []
    source_errors = []
    source_metrics = []
    for source in enabled_sources:
        adapter = build_source_adapter(source, config.fixture_path)
        source_candidates = []
        errors = []
        try:
            source_candidates = adapter.collect()
            candidates.extend(source_candidates)
            errors = [
                f"{source.source_id}: {error}"
                for error in getattr(adapter, "errors", [])
            ]
            source_errors.extend(errors)
        except Exception as exc:
            if source.failure_mode == "fail_run":
                raise

            errors = [f"{source.source_id}: {exc}"]
            source_errors.extend(errors)
        source_metrics.append(
            SourceRunMetric(
                source_id=source.source_id,
                attempted=True,
                candidate_count=len(source_candidates),
                error_count=len(errors),
                errors=errors,
            )
        )
    return _rank_candidates(
        config,
        run_date,
        candidates,
        sources_attempted,
        source_errors,
        report_slug=report_slug,
        source_metrics=source_metrics,
    )


def run_dry_run(config: AppConfig, run_date: date) -> Tuple[RunSummary, str, list[FitAssessment]]:
    _assert_no_real_sources(config)
    return _run_pipeline(config, run_date, report_slug="recruiting-dry-run")


def run_live_run(config: AppConfig, run_date: date) -> Tuple[RunSummary, str, list[FitAssessment]]:
    return _run_pipeline(config, run_date, report_slug="recruiting-live-run")


def run_scheduled_run(config: AppConfig, run_date: date) -> Tuple[RunSummary, str, list[FitAssessment]]:
    return _run_pipeline(config, run_date, report_slug="recruiting-scheduled-run")


def build_live_run_quality_gate(summary: RunSummary, config: AppConfig) -> dict[str, Any]:
    enabled_source_ids = {source.source_id for source in config.sources if source.enabled}
    findings = []
    source_rows = []
    for metric in summary.source_metrics:
        enabled = metric.source_id in enabled_source_ids
        row = {
            "source_id": metric.source_id,
            "enabled": enabled,
            "attempted": metric.attempted,
            "candidate_count": metric.candidate_count,
            "error_count": metric.error_count,
            "errors": metric.errors,
        }
        source_rows.append(row)
        if enabled and metric.attempted and metric.candidate_count == 0:
            findings.append(
                {
                    "severity": "fail",
                    "source_id": metric.source_id,
                    "message": f"enabled source {metric.source_id} collected 0 candidates",
                }
            )
        for error in metric.errors:
            findings.append(
                {
                    "severity": "warning",
                    "source_id": metric.source_id,
                    "message": error,
                }
            )

    missing_attempts = enabled_source_ids - {metric.source_id for metric in summary.source_metrics}
    for source_id in sorted(missing_attempts):
        findings.append(
            {
                "severity": "fail",
                "source_id": source_id,
                "message": f"enabled source {source_id} was not attempted",
            }
        )

    status = "fail" if any(item["severity"] == "fail" for item in findings) else "pass"
    return {
        "schema_version": 1,
        "status": status,
        "run_date": summary.run_date.isoformat(),
        "sources_attempted": summary.sources_attempted,
        "candidates_collected": summary.candidates_collected,
        "sources": source_rows,
        "findings": findings,
    }


def run_capture_import(
    config: AppConfig,
    run_date: date,
    candidates: Iterable[PostingCandidate],
    sources_attempted: list[str],
    source_errors: list[str],
) -> Tuple[RunSummary, str, list[FitAssessment]]:
    return _rank_candidates(
        config,
        run_date,
        candidates,
        sources_attempted,
        source_errors,
        report_slug="recruiting-capture-import",
    )


def run_dry_run_from_config(config_path, run_date: date):
    return run_dry_run(load_config(config_path), run_date)
