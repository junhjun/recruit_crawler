from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Sequence

from .config import apply_context_documents
from .schemas import AppConfig, UserContext
from .user_context import merge_supplemental_answers, missing_context_fields


@dataclass(frozen=True)
class ContextDoctorRequest:
    config: AppConfig
    context_docs: Sequence[Path]
    output: Path


@dataclass(frozen=True)
class ContextDoctorResult:
    output: Path
    missing_before: List[str]
    missing_after: List[str]

    @property
    def exit_code(self) -> int:
        if self.missing_after:
            return 1
        return 0

    @property
    def stdout_lines(self) -> tuple[str, ...]:
        lines = [
            f"Context preferences written: {self.output}",
            "Context status: complete" if not self.missing_after else "Context status: needs_context",
        ]
        if self.missing_before:
            lines.append("Filled context fields: " + ", ".join(self.missing_before))
        if self.missing_after:
            lines.append("Still missing context: " + ", ".join(self.missing_after))
        return tuple(lines)


def run_context_doctor(request: ContextDoctorRequest, answers: Dict[str, str]) -> ContextDoctorResult:
    context = _effective_context(request.config, request.context_docs, request.output)
    missing_before = missing_context_fields(context)
    updated = merge_supplemental_answers(context, answers) if answers else context
    missing_after = missing_context_fields(updated)
    request.output.parent.mkdir(parents=True, exist_ok=True)
    request.output.write_text(render_preferences_markdown(updated), encoding="utf-8")
    return ContextDoctorResult(
        output=request.output,
        missing_before=missing_before,
        missing_after=missing_after,
    )


def render_preferences_markdown(context: UserContext) -> str:
    lines = [
        "# Recruiting Preferences",
        "",
        "Edit this file when your job-search preferences change.",
        "",
        f"Roles: {_join_values(context.desired_roles)}",
        f"Skills: {_join_values(context.skills)}",
        f"Locations: {_join_values(context.preferred_locations)}",
        f"Experience: {_experience_text(context.max_experience_years)}",
        f"Deal breakers: {_join_values(context.explicit_deal_breakers)}",
        "",
    ]
    return "\n".join(lines)


def _effective_context(config: AppConfig, context_docs: Sequence[Path], output: Path) -> UserContext:
    paths = _context_paths_with_existing_output(context_docs, output)
    if not paths:
        return config.user_context
    return apply_context_documents(config, paths).user_context


def _context_paths_with_existing_output(context_docs: Sequence[Path], output: Path) -> List[Path]:
    paths = list(context_docs)
    if output.exists() and output not in paths:
        paths.append(output)
    return paths


def _join_values(values: Sequence[str]) -> str:
    return ", ".join(values)


def _experience_text(max_experience_years: int) -> str:
    if max_experience_years <= 0:
        return ""
    return f"{max_experience_years} years"
