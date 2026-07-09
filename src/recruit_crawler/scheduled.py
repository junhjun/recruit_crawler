from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass, replace
from datetime import date
from pathlib import Path
from typing import Optional, TypedDict, Union

from .pipeline import build_live_run_quality_gate, run_scheduled_run
from .schemas import AppConfig, FitAssessment, RunSummary
from .storage import persist_scheduled_run
from .user_context import missing_context_fields


SCHEDULED_TARGET_LANES = {"public_http", "browser_automation"}
SCHEDULED_ALLOWED_ACCESS_MODES = {"public_page, browser_automation", "public_page", "browser_automation"}
SCHEDULED_PROHIBITED_OPTION_KEYS = {
    "approved_api_access",
    "manual_postings",
    "manual_export_path",
    "user_operated_chrome_extension",
    "user_operated_browser_use",
    "ocr_required",
    "manual_review_flags",
    "partner_payload",
}


JsonScalar = Union[str, int, float, bool, None]
JsonValue = Union[JsonScalar, list["JsonValue"], dict[str, "JsonValue"]]


class GateFinding(TypedDict):
    severity: str
    source_id: str | None
    message: str


class SourcePolicyRow(TypedDict):
    source_id: str
    enabled: bool
    scheduled_action: str
    access_mode: str
    target_status: str
    target_lane: str | None
    automation_level: str
    auth_required: bool
    prohibited_options: list[str]


class RunIdentity(TypedDict):
    command_mode: str
    run_date: str
    source_config_hash: str
    profile_config_hash: str
    run_id: str


class DbPathMetadata(TypedDict):
    provided: bool
    name: str
    path_hash: str


class ScheduledQualityGate(TypedDict, total=False):
    schema_version: int
    command_mode: str
    run_date: str
    status: str
    context_status: str
    missing_context: list[str]
    db_path: DbPathMetadata | None
    report_generated: bool
    sources_attempted: list[str]
    candidates_collected: int
    sources: list[dict[str, JsonValue]]
    source_policy: list[SourcePolicyRow]
    run_identity: RunIdentity
    findings: list[GateFinding]


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


def run_scheduled_job(request: ScheduledRunRequest) -> ScheduledRunResult:
    config = request.config
    if request.output_dir:
        config = replace(config, output_dir=request.output_dir.resolve())

    missing_fields = missing_context_fields(config.user_context)
    source_policy, policy_findings = scheduled_source_policy(config)
    run_identity = scheduled_run_identity(config, request.run_date)

    if missing_fields or policy_findings:
        gate = scheduled_quality_gate(
            scheduled_preflight_gate(request.run_date),
            missing_fields,
            request.db_path,
            source_policy,
            policy_findings,
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


def _truthy_option(value: JsonValue) -> bool:
    if value is None or value is False or value == "":
        return False
    if isinstance(value, (list, tuple, dict, set)):
        return bool(value)
    return True


def _hash_json(value: JsonValue) -> str:
    payload = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def scheduled_run_identity(config: AppConfig, run_date: date) -> RunIdentity:
    source_config_hash = _hash_json([asdict(source) for source in config.sources])
    profile_config_hash = _hash_json(
        {
            "top_n": config.top_n,
            "thresholds": asdict(config.thresholds),
            "scoring_weights": asdict(config.scoring_weights),
            "user_context": {
                "desired_roles": config.user_context.desired_roles,
                "skills": config.user_context.skills,
                "preferred_locations": config.user_context.preferred_locations,
                "max_experience_years": config.user_context.max_experience_years,
                "explicit_deal_breakers": config.user_context.explicit_deal_breakers,
                "missing_context": config.user_context.missing_context,
            },
        }
    )
    identity_input = {
        "command_mode": "scheduled-run",
        "run_date": run_date.isoformat(),
        "source_config_hash": source_config_hash,
        "profile_config_hash": profile_config_hash,
    }
    return {
        **identity_input,
        "run_id": _hash_json(identity_input)[:24],
    }


def scheduled_source_policy(config: AppConfig) -> tuple[list[SourcePolicyRow], list[GateFinding]]:
    rows: list[SourcePolicyRow] = []
    findings: list[GateFinding] = []
    for source in config.sources:
        prohibited = [
            key
            for key in sorted(SCHEDULED_PROHIBITED_OPTION_KEYS)
            if _truthy_option(source.options.get(key))
        ]
        if source.access_mode == "fixture":
            allowed_to_run = source.enabled and not source.auth_required and not prohibited
        else:
            allowed_to_run = (
                source.enabled
                and source.target_status == "enabled"
                and source.target_lane in SCHEDULED_TARGET_LANES
                and source.automation_level == "no_human"
                and source.access_mode in SCHEDULED_ALLOWED_ACCESS_MODES
                and not source.auth_required
                and not prohibited
            )
        row = {
            "source_id": source.source_id,
            "enabled": source.enabled,
            "scheduled_action": "run" if allowed_to_run else "skip",
            "access_mode": source.access_mode,
            "target_status": source.target_status,
            "target_lane": source.target_lane,
            "automation_level": source.automation_level,
            "auth_required": source.auth_required,
            "prohibited_options": prohibited,
        }
        rows.append(row)
        if source.enabled and not allowed_to_run:
            reasons = []
            if source.auth_required:
                reasons.append("auth_required")
            if source.target_status != "enabled" and source.access_mode != "fixture":
                reasons.append(f"target_status={source.target_status}")
            if source.target_lane not in SCHEDULED_TARGET_LANES and source.access_mode != "fixture":
                reasons.append(f"target_lane={source.target_lane}")
            if source.automation_level != "no_human" and source.access_mode != "fixture":
                reasons.append(f"automation_level={source.automation_level}")
            if source.access_mode not in SCHEDULED_ALLOWED_ACCESS_MODES and source.access_mode != "fixture":
                reasons.append(f"access_mode={source.access_mode}")
            reasons.extend(f"option:{key}" for key in prohibited)
            findings.append(
                {
                    "severity": "fail",
                    "source_id": source.source_id,
                    "message": "scheduled-run source policy rejected enabled source: " + ", ".join(reasons),
                }
            )
    return rows, findings


def scheduled_preflight_gate(run_date: date) -> ScheduledQualityGate:
    return {
        "schema_version": 1,
        "status": "pass",
        "run_date": run_date.isoformat(),
        "sources_attempted": [],
        "candidates_collected": 0,
        "sources": [],
        "findings": [],
    }


def scheduled_db_path_metadata(db_path: Optional[Path]) -> DbPathMetadata | None:
    if db_path is None:
        return None
    text = str(db_path)
    return {
        "provided": True,
        "name": db_path.name,
        "path_hash": hashlib.sha256(text.encode("utf-8")).hexdigest(),
    }


def scheduled_quality_gate(
    live_gate: ScheduledQualityGate,
    missing_fields: list[str],
    db_path: Optional[Path],
    source_policy: list[SourcePolicyRow],
    policy_findings: list[GateFinding],
    run_identity: RunIdentity,
    *,
    report_generated: bool,
) -> ScheduledQualityGate:
    findings = [*live_gate["findings"], *policy_findings]
    if missing_fields:
        findings.append(
            {
                "severity": "fail",
                "source_id": None,
                "message": "scheduled-run missing required user context: " + ", ".join(missing_fields),
            }
        )
    status = "fail" if any(item["severity"] == "fail" for item in findings) else "pass"
    return {
        **live_gate,
        "schema_version": 1,
        "command_mode": "scheduled-run",
        "status": status,
        "context_status": "needs_context" if missing_fields else "complete",
        "missing_context": missing_fields,
        "db_path": scheduled_db_path_metadata(db_path),
        "report_generated": report_generated,
        "source_policy": source_policy,
        "run_identity": run_identity,
        "findings": findings,
    }
