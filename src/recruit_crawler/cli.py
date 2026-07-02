from __future__ import annotations

import argparse
import json
from datetime import date
from pathlib import Path
from typing import Optional

from .capture_import import (
    CaptureImportError,
    build_capture_quality_gate,
    import_capture_files,
    select_capture_files,
)
from .browser_evidence import build_browser_evidence, write_browser_evidence
from .config import ConfigError, apply_context_documents, apply_supplemental_answers, load_config
from .source_registry import source_status_rows
from .pipeline import build_live_run_quality_gate, run_capture_import, run_dry_run, run_live_run


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


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="recruit-crawler")
    subparsers = parser.add_subparsers(dest="command", required=True)
    dry_run = subparsers.add_parser("dry-run", help="run fixture-only pipeline without network access")
    dry_run.add_argument("--config", type=Path, default=Path("config/sample_config.json"))
    dry_run.add_argument("--run-date", help="YYYY-MM-DD date used for deterministic deadline checks")
    dry_run.add_argument("--print-report", action="store_true", help="print generated Markdown to stdout")
    dry_run.add_argument("--context-doc", type=Path, action="append", help="personal context document; repeat for multiple .txt, .md, .pdf, or .docx inputs")
    live_run = subparsers.add_parser("live-run", help="run enabled reviewed real-source adapters")
    live_run.add_argument("--config", type=Path, default=Path("config/live_sources.sample.json"))
    live_run.add_argument("--run-date", help="YYYY-MM-DD date used for deterministic deadline checks")
    live_run.add_argument("--print-report", action="store_true", help="print generated Markdown to stdout")
    live_run.add_argument("--quality-gate-output", type=Path, help="write live-run source quality gate JSON")
    live_run.add_argument("--context-doc", type=Path, action="append", help="personal context document; repeat for multiple .txt, .md, .pdf, or .docx inputs")
    source_status = subparsers.add_parser("source-status", help="print source registry status without network access")
    source_status.add_argument("--config", type=Path, default=Path("config/live_sources.sample.json"))
    source_status.add_argument("--json", action="store_true", help="print machine-readable registry rows")
    capture_import = subparsers.add_parser("capture-import", help="import Chrome extension capture JSON files")
    capture_import.add_argument("--config", type=Path, default=Path("config/sample_config.json"))
    capture_import.add_argument("--spool-dir", type=Path, default=Path("~/Downloads/recruit-captures"))
    capture_import.add_argument("--date", dest="capture_date", help="YYYY-MM-DD capture directory to import")
    capture_import.add_argument("--latest", action="store_true", help="import the latest YYYY-MM-DD capture directory")
    capture_import.add_argument("--file", dest="files", type=Path, action="append", help="specific capture JSON file; repeatable")
    capture_import.add_argument("--run-date", help="YYYY-MM-DD date used for deterministic deadline checks")
    capture_import.add_argument("--print-report", action="store_true", help="print generated Markdown to stdout")
    capture_gate = subparsers.add_parser("capture-quality-gate", help="validate Chrome capture JSON and write quality gate JSON")
    capture_gate.add_argument("--spool-dir", type=Path, default=Path("~/Downloads/recruit-captures"))
    capture_gate.add_argument("--date", dest="capture_date", help="YYYY-MM-DD capture directory to validate")
    capture_gate.add_argument("--latest", action="store_true", help="validate the latest YYYY-MM-DD capture directory")
    capture_gate.add_argument("--file", dest="files", type=Path, action="append", help="specific capture JSON file; repeatable")
    capture_gate.add_argument("--output", type=Path, required=True, help="path to write quality gate JSON")
    browser_evidence = subparsers.add_parser("browser-evidence", help="capture Chrome/Chromium DOM evidence transcript")
    browser_evidence.add_argument("--config", type=Path, default=Path("config/live_sources.sample.json"))
    browser_evidence.add_argument("--source-id", required=True)
    browser_evidence.add_argument("--target-url")
    browser_evidence.add_argument("--fixture-html", type=Path)
    browser_evidence.add_argument("--output", type=Path, required=True)
    return parser


def main(argv: Optional[list[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    if args.command == "dry-run":
        try:
            config = load_config(args.config)
            if args.context_doc:
                config = apply_context_documents(config, args.context_doc)
                config = _apply_supplemental_interview(config)
            summary, report, _ranked = run_dry_run(config, _parse_date(args.run_date))
        except (ConfigError, ValueError, FileNotFoundError) as exc:
            parser.error(str(exc))
            return 2
        print(f"Report written: {summary.report_path}")
        if args.print_report:
            print(report)
        return 0

    if args.command == "live-run":
        try:
            config = load_config(args.config, allow_real_sources=True)
            if args.context_doc:
                config = apply_context_documents(config, args.context_doc)
                config = _apply_supplemental_interview(config)
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
    if args.command == "source-status":
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

    if args.command == "capture-import":
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

    if args.command == "capture-quality-gate":
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

    if args.command == "browser-evidence":
        try:
            config = load_config(args.config, allow_real_sources=True)
            manifest = next((source for source in config.sources if source.source_id == args.source_id), None)
            if manifest is None:
                raise ConfigError(f"unknown source_id: {args.source_id}")
            transcript = build_browser_evidence(
                manifest,
                fixture_html=args.fixture_html,
                target_url=args.target_url,
            )
            write_browser_evidence(transcript, args.output)
        except (ConfigError, ValueError, FileNotFoundError) as exc:
            parser.error(str(exc))
            return 2
        print(f"Browser evidence written: {args.output}")
        print(f"Browser evidence status: {'passed' if transcript['exit_code'] == 0 else 'failed'}")
        return int(transcript["exit_code"])
    parser.error(f"unknown command: {args.command}")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
