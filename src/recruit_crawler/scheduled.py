from __future__ import annotations

import json
import socket
from dataclasses import dataclass, replace
from datetime import date
from pathlib import Path
from typing import Optional

from ._scheduled_contract import (
    GateFinding,
    ScheduledQualityGate,
    SourcePolicyRow,
    scheduled_preflight_gate,
    scheduled_quality_gate,
    scheduled_run_identity,
    scheduled_source_policy,
)
from .pipeline import build_live_run_quality_gate, run_scheduled_run
from .schemas import AppConfig, FitAssessment, RunSummary
from .storage import persist_scheduled_run
from .user_context import missing_context_fields


@dataclass(frozen=True)
class ScheduledRunRequest:
    config: AppConfig
    run_date: date
    quality_gate_output: Path
    output_dir: Optional[Path] = None
    db_path: Optional[Path] = None


@dataclass(frozen=True)
class ScheduledRunResult:
    exit_code: int
    gate: ScheduledQualityGate
    summary: Optional[RunSummary]
    ranked: tuple[FitAssessment, ...]
    quality_gate_output: Path

    @property
    def stdout_lines(self) -> tuple[str, ...]:
        lines = [
            "Scheduled run complete" if self.gate["report_generated"] else "Scheduled run blocked",
            f"Run date: {self.gate['run_date']}",
            f"Run id: {self.gate['run_identity']['run_id']}",
        ]
        if self.summary is not None:
            lines.append(f"Report written: {self.summary.report_path}")
        else:
            lines.append("Report written: not generated")
        lines.extend(
            [
                f"Quality gate status: {self.gate['status']}",
                f"Quality gate written: {self.quality_gate_output}",
            ]
        )
        if self.gate["missing_context"]:
            lines.append("Missing context: " + ", ".join(self.gate["missing_context"]))
        if self.gate["db_path"]:
            lines.append(
                "DB path accepted for future persistence: "
                f"{self.gate['db_path']['name']} ({self.gate['db_path']['path_hash'][:12]})"
            )
        return tuple(lines)


def _resolve_domain(domain: str) -> None:
    socket.getaddrinfo(domain, 443)


def _scheduled_network_findings(
    config: AppConfig,
    source_policy: list[SourcePolicyRow],
) -> list[GateFinding]:
    run_source_ids = {
        str(row["source_id"])
        for row in source_policy
        if row["scheduled_action"] == "run"
    }
    findings: list[GateFinding] = []
    checked_domains: set[str] = set()
    for source in config.sources:
        if source.source_id not in run_source_ids or source.access_mode == "fixture":
            continue
        for domain in source.domains:
            if domain in checked_domains:
                continue
            checked_domains.add(domain)
            try:
                _resolve_domain(domain)
            except OSError as exc:
                findings.append(
                    {
                        "severity": "fail",
                        "source_id": source.source_id,
                        "message": (
                            "scheduled-run network preflight failed before source collection: "
                            f"{domain}: {exc}. External DNS/network access is required; "
                            "in Codex sandbox, rerun this command with network approval."
                        ),
                    }
                )
                return findings
    return findings


def run_scheduled_job(request: ScheduledRunRequest) -> ScheduledRunResult:
    config = request.config
    if request.output_dir:
        config = replace(config, output_dir=request.output_dir.resolve())

    missing_fields = missing_context_fields(config.user_context)
    source_policy, policy_findings = scheduled_source_policy(config)
    network_findings = (
        []
        if missing_fields or policy_findings
        else _scheduled_network_findings(config, source_policy)
    )
    run_identity = scheduled_run_identity(config, request.run_date)

    if missing_fields or policy_findings or network_findings:
        gate = scheduled_quality_gate(
            scheduled_preflight_gate(request.run_date),
            missing_fields,
            request.db_path,
            source_policy,
            [*policy_findings, *network_findings],
            run_identity,
            report_generated=False,
        )
        summary = None
        ranked: list[FitAssessment] = []
    else:
        summary, _report, ranked = run_scheduled_run(config, request.run_date)
        gate = scheduled_quality_gate(
            build_live_run_quality_gate(summary, config),
            missing_fields,
            request.db_path,
            source_policy,
            policy_findings,
            run_identity,
            report_generated=True,
        )

    request.quality_gate_output.parent.mkdir(parents=True, exist_ok=True)
    request.quality_gate_output.write_text(json.dumps(gate, ensure_ascii=False, indent=2), encoding="utf-8")
    if request.db_path:
        persist_scheduled_run(request.db_path, gate=gate, summary=summary, ranked=ranked)

    return ScheduledRunResult(
        exit_code=1 if gate["status"] == "fail" else 0,
        gate=gate,
        summary=summary,
        ranked=tuple(ranked),
        quality_gate_output=request.quality_gate_output,
    )
