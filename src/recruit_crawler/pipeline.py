from __future__ import annotations

from datetime import date
from typing import Iterable, Tuple

from .config import ConfigError, load_config
from .dedupe import dedupe_snapshots
from .jd_parser import parse_candidates
from .report_writer import write_report
from .schemas import AppConfig, FitAssessment, PostingCandidate, RunSummary
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


def _rank_candidates(
    config: AppConfig,
    run_date: date,
    candidates: Iterable[PostingCandidate],
    sources_attempted: list[str],
    source_errors: list[str],
    *,
    report_slug: str,
) -> Tuple[RunSummary, str, list[FitAssessment]]:
    candidates = list(candidates)
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
    for source in enabled_sources:
        adapter = build_source_adapter(source, config.fixture_path)
        try:
            candidates.extend(adapter.collect())
            source_errors.extend(
                f"{source.source_id}: {error}"
                for error in getattr(adapter, "errors", [])
            )
        except Exception as exc:
            if source.failure_mode == "fail_run":
                raise
            source_errors.append(f"{source.source_id}: {exc}")
    return _rank_candidates(
        config,
        run_date,
        candidates,
        sources_attempted,
        source_errors,
        report_slug=report_slug,
    )


def run_dry_run(config: AppConfig, run_date: date) -> Tuple[RunSummary, str, list[FitAssessment]]:
    _assert_no_real_sources(config)
    return _run_pipeline(config, run_date, report_slug="recruiting-dry-run")


def run_live_run(config: AppConfig, run_date: date) -> Tuple[RunSummary, str, list[FitAssessment]]:
    return _run_pipeline(config, run_date, report_slug="recruiting-live-run")


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
