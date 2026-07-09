from __future__ import annotations

import argparse
from collections.abc import Callable
from pathlib import Path
from typing import Optional

from .cli_handlers import (
    handle_browser_evidence,
    handle_capture_import,
    handle_capture_quality_gate,
    handle_context_doctor,
    handle_dry_run,
    handle_feedback_add,
    handle_feedback_export,
    handle_live_run,
    handle_scheduled_history,
    handle_scheduled_run,
    handle_source_status,
    handle_status_report,
)


CommandHandler = Callable[[argparse.Namespace, argparse.ArgumentParser], int]

_COMMAND_HANDLERS: dict[str, CommandHandler] = {
    "dry-run": handle_dry_run,
    "live-run": handle_live_run,
    "scheduled-run": handle_scheduled_run,
    "scheduled-history": handle_scheduled_history,
    "feedback-add": handle_feedback_add,
    "feedback-export": handle_feedback_export,
    "source-status": handle_source_status,
    "capture-import": handle_capture_import,
    "capture-quality-gate": handle_capture_quality_gate,
    "browser-evidence": handle_browser_evidence,
    "context-doctor": handle_context_doctor,
    "status-report": handle_status_report,
}


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
    scheduled_run = subparsers.add_parser("scheduled-run", help="run non-interactive daily report for Codex Scheduled")
    scheduled_run.add_argument("--config", type=Path, default=Path("config/live_sources.sample.json"))
    scheduled_run.add_argument("--run-date", help="YYYY-MM-DD date used for deterministic deadline checks")
    scheduled_run.add_argument("--context-doc", type=Path, action="append", help="personal context document; repeat for multiple .txt, .md, .pdf, or .docx inputs")
    scheduled_run.add_argument("--output-dir", type=Path, help="directory for scheduled Markdown reports")
    scheduled_run.add_argument("--quality-gate-output", type=Path, required=True, help="write scheduled-run quality gate JSON")
    scheduled_run.add_argument("--db-path", type=Path, help="optional future SQLite persistence path; accepted for contract stability")
    scheduled_history = subparsers.add_parser("scheduled-history", help="export persisted scheduled-run history")
    scheduled_history.add_argument("--db-path", type=Path, required=True)
    scheduled_history.add_argument("--json", action="store_true", help="print machine-readable run history")
    feedback = subparsers.add_parser("feedback-add", help="record feedback for a persisted recommendation")
    feedback.add_argument("--db-path", type=Path, required=True)
    feedback.add_argument("--recommendation-id", required=True)
    feedback.add_argument("--verdict", required=True, choices=["applied", "ignored", "hidden", "false_positive", "false_negative", "interesting", "not_relevant"])
    feedback.add_argument("--reason", required=True)
    feedback.add_argument("--movement", default="same", choices=["up", "down", "same"])
    feedback.add_argument("--created-at", help="ISO timestamp for deterministic imports/tests")
    feedback_export = subparsers.add_parser("feedback-export", help="export persisted feedback events")
    feedback_export.add_argument("--db-path", type=Path, required=True)
    feedback_export.add_argument("--json", action="store_true", help="print machine-readable feedback events")
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
    context_doctor = subparsers.add_parser("context-doctor", help="interview for missing user context and write preferences Markdown")
    context_doctor.add_argument("--config", type=Path, default=Path("config/live_sources.sample.json"))
    context_doctor.add_argument("--context-doc", type=Path, action="append", help="existing context document; repeat for resume, portfolio, and preferences")
    context_doctor.add_argument("--output", type=Path, required=True, help="path to write persistent preferences Markdown")
    status_report = subparsers.add_parser("status-report", help="write or check the current feature status ledger")
    status_report.add_argument("--config", type=Path, default=Path("config/live_sources.sample.json"))
    status_report.add_argument("--features", type=Path, default=Path("docs/status/features.json"))
    status_report.add_argument("--output", type=Path, default=Path("docs/status.md"))
    status_report.add_argument("--todo", type=Path, default=Path("TODO.md"))
    status_report.add_argument("--check", action="store_true", help="fail if the generated status report differs from --output")
    status_report.add_argument("--brief", action="store_true", help="print a token-minimal progress brief without regenerating docs/status.md")
    return parser


def main(argv: Optional[list[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    handler = _COMMAND_HANDLERS.get(args.command)
    if handler is None:
        parser.error(f"unknown command: {args.command}")
        return 2
    return handler(args, parser)


if __name__ == "__main__":
    raise SystemExit(main())
