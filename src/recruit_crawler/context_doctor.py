from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Sequence

from .config import apply_context_documents
from .schemas import AppConfig, UserContext
from .user_context import merge_supplemental_answers, missing_context_fields

_PREFERENCE_PROMPTS = {
    "desired_roles": "어떤 직무명/역할을 우선 지원 대상으로 볼까요?",
    "skills": "평가에 반드시 반영할 핵심 기술 스택은 무엇인가요?",
    "preferred_locations": "선호 근무지 또는 원격/하이브리드 조건은 무엇인가요?",
    "max_experience_years": "지원 가능한 최대 요구 경력 연차는 몇 년인가요?",
    "explicit_deal_breakers": "절대 제외하거나 피하고 싶은 조건은 무엇인가요?",
}


@dataclass(frozen=True, slots=True)
class ContextDoctorRequest:
    config: AppConfig
    context_docs: Sequence[Path]
    output: Path


@dataclass(frozen=True, slots=True)
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
        filled_fields = [field for field in self.missing_before if field not in self.missing_after]
        if filled_fields:
            lines.append("Filled context fields: " + ", ".join(filled_fields))
        if self.missing_after:
            lines.append("Still missing context: " + ", ".join(self.missing_after))
        return tuple(lines)


def run_context_doctor(request: ContextDoctorRequest, answers: Dict[str, str]) -> ContextDoctorResult:
    existing_preferences = _existing_preferences(request.output)
    context = _effective_context(request, existing_preferences)
    missing_before = missing_context_fields(context)
    updated = merge_supplemental_answers(context, answers) if answers else context
    missing_after = missing_context_fields(updated)
    request.output.parent.mkdir(parents=True, exist_ok=True)
    baseline = _preserved_existing_preferences(existing_preferences, _context_without_output(request))
    preferences = merge_supplemental_answers(baseline, answers) if answers else baseline
    request.output.write_text(render_preferences_markdown(preferences), encoding="utf-8")
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
    ]
    if context.desired_roles:
        lines.append(f"Roles: {_join_values(context.desired_roles)}")
    if context.skills:
        lines.append(f"Skills: {_join_values(context.skills)}")
    if context.preferred_locations:
        lines.append(f"Locations: {_join_values(context.preferred_locations)}")
    if context.max_experience_years > 0:
        lines.append(f"Experience: {_experience_text(context.max_experience_years)}")
    if context.explicit_deal_breakers:
        lines.append(f"Deal breakers: {_join_values(context.explicit_deal_breakers)}")
    lines.append("")
    return "\n".join(lines)


def _existing_preferences(output: Path) -> UserContext:
    if output.exists():
        return _parse_preferences_markdown(output)
    return UserContext(desired_roles=[], skills=[], preferred_locations=[])


def _parse_preferences_markdown(path: Path) -> UserContext:
    desired_roles: List[str] = []
    skills: List[str] = []
    preferred_locations: List[str] = []
    max_experience_years = 0
    explicit_deal_breakers: List[str] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if ":" not in line:
            continue
        key, value = line.split(":", 1)
        values = _split_preferences_values(value)
        normalized_key = key.strip().lower()
        if normalized_key == "roles":
            desired_roles = values
        if normalized_key == "skills":
            skills = values
        if normalized_key == "locations":
            preferred_locations = values
        if normalized_key == "experience":
            max_experience_years = _parse_experience_years(value)
        if normalized_key == "deal breakers":
            explicit_deal_breakers = values
    return UserContext(
        desired_roles=desired_roles,
        skills=skills,
        preferred_locations=preferred_locations,
        max_experience_years=max_experience_years,
        explicit_deal_breakers=explicit_deal_breakers,
    )


def _split_preferences_values(value: str) -> List[str]:
    return [item.strip() for item in value.split(",") if item.strip()]


def _parse_experience_years(value: str) -> int:
    text = value.strip()
    if not text:
        return 0
    return int(text.split()[0])


def _preserved_existing_preferences(existing: UserContext, context: UserContext) -> UserContext:
    return UserContext(
        desired_roles=list(existing.desired_roles),
        skills=[] if context.skills else list(existing.skills),
        preferred_locations=list(existing.preferred_locations),
        max_experience_years=0 if context.max_experience_years > 0 else existing.max_experience_years,
        explicit_deal_breakers=list(existing.explicit_deal_breakers),
    )


def effective_context_for_doctor(request: ContextDoctorRequest) -> UserContext:
    return _effective_context(request, _existing_preferences(request.output))


def context_doctor_question_fields(request: ContextDoctorRequest) -> List[str]:
    existing = _existing_preferences(request.output)
    fields: List[str] = []
    if not existing.desired_roles:
        fields.append("desired_roles")
    if not existing.skills:
        fields.append("skills")
    if not existing.preferred_locations:
        fields.append("preferred_locations")
    if existing.max_experience_years <= 0:
        fields.append("max_experience_years")
    if not existing.explicit_deal_breakers:
        fields.append("explicit_deal_breakers")
    return fields


def context_doctor_question(field: str) -> str:
    return _PREFERENCE_PROMPTS[field]


def _effective_context(request: ContextDoctorRequest, existing_preferences: UserContext) -> UserContext:
    context = _context_without_output(request)
    if request.output.exists():
        return merge_supplemental_answers(context, _answers_from_preferences(existing_preferences))
    return context


def _context_without_output(request: ContextDoctorRequest) -> UserContext:
    if not request.context_docs:
        return request.config.user_context
    return apply_context_documents(request.config, request.context_docs).user_context


def _answers_from_preferences(context: UserContext) -> Dict[str, str]:
    answers: Dict[str, str] = {}
    if context.desired_roles:
        answers["desired_roles"] = _join_values(context.desired_roles)
    if context.skills:
        answers["skills"] = _join_values(context.skills)
    if context.preferred_locations:
        answers["preferred_locations"] = _join_values(context.preferred_locations)
    if context.max_experience_years > 0:
        answers["max_experience_years"] = str(context.max_experience_years)
    return answers


def _join_values(values: Sequence[str]) -> str:
    return ", ".join(values)


def _experience_text(max_experience_years: int) -> str:
    if max_experience_years <= 0:
        return ""
    return f"{max_experience_years} years"
