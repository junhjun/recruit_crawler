from __future__ import annotations

import argparse
import json
from datetime import date

from .cli_context import load_config_with_context
from .config import ConfigError
from .pipeline import build_live_run_preflight_gate, build_live_run_quality_gate, run_live_run


def handle_live_run(args: argparse.Namespace, parser: argparse.ArgumentParser) -> int:
    try:
        run_date = _parse_date(args.run_date)
        config = load_config_with_context(
            args,
            allow_real_sources=True,
            interview=args.interview_missing_context,
        )
        gate = build_live_run_preflight_gate(run_date, config)
        summary = None
        report = ""
        if gate["status"] != "fail":
            summary, report, _ranked = run_live_run(config, run_date)
            gate = build_live_run_quality_gate(summary, config)
        if args.quality_gate_output:
            args.quality_gate_output.parent.mkdir(parents=True, exist_ok=True)
            args.quality_gate_output.write_text(json.dumps(gate, ensure_ascii=False, indent=2), encoding="utf-8")
    except (ConfigError, ValueError, FileNotFoundError) as exc:
        parser.error(str(exc))
        return 2
    if summary is None:
        print("Live run blocked")
        print("Report written: not generated")
    else:
        print(f"Report written: {summary.report_path}")
    print(f"Live-run quality gate status: {gate['status']}")
    if args.quality_gate_output:
        print(f"Live-run quality gate written: {args.quality_gate_output}")
    if args.print_report and summary is not None:
        print(report)
    return 1 if gate["status"] == "fail" else 0


def _parse_date(value: str | None) -> date:
    if not value:
        return date.today()
    return date.fromisoformat(value)
