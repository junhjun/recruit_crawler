from __future__ import annotations

import argparse
import json
from datetime import date
from typing import Optional

from .browser_evidence import build_browser_evidence, write_browser_evidence
from .capture_import import CaptureImportError, build_capture_quality_gate, import_capture_files, select_capture_files
from .config import ConfigError, apply_context_documents, apply_supplemental_answers, load_config
from .pipeline import build_live_run_quality_gate, run_capture_import, run_dry_run, run_live_run
from .scheduled import ScheduledRunRequest, run_scheduled_job
from .source_registry import source_status_rows
from .status_report import StatusReportError, build_progress_brief, check_status_report, write_status_report
from .storage import add_feedback_event, export_feedback_events, export_recommendations, export_runs
from .user_context import UserContextImportError


def _parse_date(value: Optional[str]) -> date:
    if not value:
        return date.today()
    return date.fromisoformat(value)


def _apply_supplemental_interview(config):
    from .user_context import missing_context_fields, supplemental_questions

    missing_fields = missing_context_fields(config.user_context)
    if not missing_fields:
        return config
    questions = supplemental_questions(config.user_context)
    answers = {}
    print("Supplemental context interview:")
    for field, question in zip(missing_fields, questions):
        try:
            answer = input(f"- {question}\n> ")
        except EOFError as exc:
            raise ConfigError(f"missing context requires supplemental answer for {field}") from exc
        if answer.strip():
            answers[field] = answer.strip()
    if not answers:
        return config
    return apply_supplemental_answers(config, answers)


def _load_config_with_context(args: argparse.Namespace, *, allow_real_sources: bool, interview: bool):
    config = load_config(args.config, allow_real_sources=allow_real_sources)
    if args.context_doc:
        config = apply_context_documents(config, args.context_doc)
        if interview:
            config = _apply_supplemental_interview(config)
    return config


def handle_dry_run(args: argparse.Namespace, parser: argparse.ArgumentParser) -> int:
    try:
        config = _load_config_with_context(args, allow_real_sources=False, interview=True)
        summary, report, _ranked = run_dry_run(config, _parse_date(args.run_date))
    except (ConfigError, ValueError, FileNotFoundError) as exc:
        parser.error(str(exc))
        return 2
    print(f"Report written: {summary.report_path}")
    if args.print_report:
        print(report)
    return 0


def handle_live_run(args: argparse.Namespace, parser: argparse.ArgumentParser) -> int:
    try:
        config = _load_config_with_context(args, allow_real_sources=True, interview=True)
        summary, report, _ranked = run_live_run(config, _parse_date(args.run_date))
        gate = build_live_run_quality_gate(summary, config)
        if args.quality_gate_output:
            args.quality_gate_output.parent.mkdir(parents=True, exist_ok=True)
            args.quality_gate_output.write_text(json.dumps(gate, ensure_ascii=False, indent=2), encoding="utf-8")
    except (ConfigError, ValueError, FileNotFoundError) as exc:
        parser.error(str(exc))
        return 2
    print(f"Report written: {summary.report_path}")
    print(f"Live-run quality gate status: {gate['status']}")
    if args.quality_gate_output:
        print(f"Live-run quality gate written: {args.quality_gate_output}")
    if args.print_report:
        print(report)
    return 1 if gate["status"] == "fail" else 0


def handle_scheduled_run(args: argparse.Namespace, parser: argparse.ArgumentParser) -> int:
    try:
        config = _load_config_with_context(args, allow_real_sources=True, interview=False)
        result = run_scheduled_job(
            ScheduledRunRequest(
                config=config,
                run_date=_parse_date(args.run_date),
                quality_gate_output=args.quality_gate_output,
                output_dir=args.output_dir,
                db_path=args.db_path,
            )
        )
    except UserContextImportError as exc:
        parser.exit(3, f"{parser.prog}: privacy error: {exc}\n")
    except (ConfigError, ValueError, FileNotFoundError) as exc:
        parser.error(str(exc))
        return 2
    for line in result.stdout_lines:
        print(line)
    return result.exit_code


def handle_scheduled_history(args: argparse.Namespace, parser: argparse.ArgumentParser) -> int:
    try:
        runs = export_runs(args.db_path)
        recommendations = export_recommendations(args.db_path)
    except (ValueError, FileNotFoundError) as exc:
        parser.error(str(exc))
        return 2
    if args.json:
        print(json.dumps({"runs": runs, "recommendations": recommendations}, ensure_ascii=False, indent=2))
    else:
        for run in runs:
            print(f"{run['run_date']} {run['run_id']} {run['status']} ranked={run['ranked_count']} report={run['report_path']}")
        for recommendation in recommendations:
            print(
                f"recommendation {recommendation['recommendation_id']} "
                f"{recommendation['run_id']} {recommendation['recommendation']} "
                f"score={recommendation['score']} title={recommendation['title']}"
            )
    return 0


def handle_feedback_add(args: argparse.Namespace, parser: argparse.ArgumentParser) -> int:
    try:
        event_id = add_feedback_event(
            args.db_path,
            recommendation_id=args.recommendation_id,
            verdict=args.verdict,
            reason=args.reason,
            movement=args.movement,
            created_at=args.created_at,
        )
    except UserContextImportError as exc:
        parser.exit(3, f"{parser.prog}: privacy error: {exc}\n")
    except (ValueError, FileNotFoundError) as exc:
        parser.error(str(exc))
        return 2
    print(f"Feedback recorded: {event_id}")
    return 0


def handle_feedback_export(args: argparse.Namespace, parser: argparse.ArgumentParser) -> int:
    try:
        events = export_feedback_events(args.db_path)
    except (ValueError, FileNotFoundError) as exc:
        parser.error(str(exc))
        return 2
    if args.json:
        print(json.dumps({"feedback": events}, ensure_ascii=False, indent=2))
    else:
        for event in events:
            print(f"{event['created_at']} {event['recommendation_id']} {event['verdict']} movement={event['movement']}")
    return 0


def handle_source_status(args: argparse.Namespace, parser: argparse.ArgumentParser) -> int:
    try:
        config = load_config(args.config, allow_real_sources=True)
        rows = source_status_rows(config.sources)
    except (ConfigError, ValueError, FileNotFoundError) as exc:
        parser.error(str(exc))
        return 2
    if args.json:
        print(json.dumps({"sources": rows}, ensure_ascii=False, indent=2))
    else:
        for row in rows:
            lane = row["target_lane"] if row["target_lane"] is not None else "null"
            print(f"{row['source_id']}: {row['target_status']} / {lane} / enabled={row['enabled']}")
    return 0


def handle_capture_import(args: argparse.Namespace, parser: argparse.ArgumentParser) -> int:
    try:
        config = load_config(args.config)
        selection = select_capture_files(
            args.spool_dir,
            run_date=_parse_date(args.capture_date) if args.capture_date else None,
            latest=args.latest,
            files=args.files,
        )
        imported = import_capture_files(selection.files)
        summary, report, _ranked = run_capture_import(
            config,
            _parse_date(args.run_date) if args.run_date else selection.run_date,
            imported.candidates,
            imported.sources_attempted,
            imported.source_errors,
        )
    except (ConfigError, CaptureImportError, ValueError, FileNotFoundError) as exc:
        parser.error(str(exc))
        return 2
    print(f"Imported capture files: {len(selection.files)}")
    print(f"Imported candidates: {summary.candidates_collected}")
    print(f"Report written: {summary.report_path}")
    if args.print_report:
        print(report)
    return 0


def handle_capture_quality_gate(args: argparse.Namespace, parser: argparse.ArgumentParser) -> int:
    try:
        selection = select_capture_files(
            args.spool_dir,
            run_date=_parse_date(args.capture_date) if args.capture_date else None,
            latest=args.latest,
            files=args.files,
        )
        imported = import_capture_files(selection.files)
        gate = build_capture_quality_gate(selection, imported)
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(gate, ensure_ascii=False, indent=2), encoding="utf-8")
    except (CaptureImportError, ValueError, FileNotFoundError) as exc:
        parser.error(str(exc))
        return 2
    print(f"Quality gate written: {args.output}")
    print(f"Quality gate status: {gate['status']}")
    return 0


def handle_browser_evidence(args: argparse.Namespace, parser: argparse.ArgumentParser) -> int:
    try:
        config = load_config(args.config, allow_real_sources=True)
        manifest = next((source for source in config.sources if source.source_id == args.source_id), None)
        if manifest is None:
            raise ConfigError(f"unknown source_id: {args.source_id}")
        transcript = build_browser_evidence(manifest, fixture_html=args.fixture_html, target_url=args.target_url)
        write_browser_evidence(transcript, args.output)
    except (ConfigError, ValueError, FileNotFoundError) as exc:
        parser.error(str(exc))
        return 2
    print(f"Browser evidence written: {args.output}")
    print(f"Browser evidence status: {'passed' if transcript['exit_code'] == 0 else 'failed'}")
    return int(transcript["exit_code"])


def handle_status_report(args: argparse.Namespace, parser: argparse.ArgumentParser) -> int:
    try:
        if args.brief:
            brief = build_progress_brief(
                features_path=args.features,
                todo_path=args.todo,
                config_path=args.config,
                output_path=args.output,
            )
            print(brief.text)
            return 0
        if args.check:
            result = check_status_report(
                config_path=args.config,
                features_path=args.features,
                output_path=args.output,
                todo_path=args.todo,
            )
            print(result.message)
            return 0 if result.ok else 1
        write_status_report(
            config_path=args.config,
            features_path=args.features,
            output_path=args.output,
            todo_path=args.todo,
        )
    except (ConfigError, StatusReportError, ValueError, FileNotFoundError) as exc:
        parser.error(str(exc))
        return 2
    print(f"Status report written: {args.output}")
    return 0
