from __future__ import annotations

import re
from dataclasses import replace
from pathlib import Path
from typing import Dict, Iterable, List, Sequence, Tuple

from .schemas import Profile, UserContext

_REQUIRED_FIELDS = ("desired_roles", "skills", "preferred_locations")
_PRIVATE_PATTERNS = (
    re.compile(r"PRIVATE_[A-Z0-9_]*CANARY", re.I),
    re.compile(r"RAW_[A-Z0-9_]*CANARY", re.I),
    re.compile(r"Ignore previous instructions", re.I),
)


class UserContextImportError(ValueError):
    pass


def context_from_profile(profile: Profile) -> UserContext:
    return UserContext(
        desired_roles=list(profile.desired_roles),
        skills=list(profile.skills),
        preferred_locations=list(profile.preferred_locations),
        max_experience_years=profile.max_experience_years,
        explicit_deal_breakers=list(profile.exclusions),
        private_canaries=list(profile.private_canaries),
        provenance={
            "desired_roles": "config.profile.desired_roles",
            "skills": "config.profile.skills",
            "preferred_locations": "config.profile.preferred_locations",
            "max_experience_years": "config.profile.max_experience_years",
            "explicit_deal_breakers": "config.profile.exclusions",
        },
    )


def profile_from_context(context: UserContext) -> Profile:
    return Profile(
        desired_roles=list(context.desired_roles),
        skills=list(context.skills),
        preferred_locations=list(context.preferred_locations),
        max_experience_years=context.max_experience_years,
        exclusions=list(context.explicit_deal_breakers),
        private_canaries=list(context.private_canaries),
    )


def missing_context_fields(context: UserContext) -> List[str]:
    missing: List[str] = []
    if not context.desired_roles:
        missing.append("desired_roles")
    if not context.skills:
        missing.append("skills")
    if not context.preferred_locations:
        missing.append("preferred_locations")
    if context.max_experience_years <= 0:
        missing.append("max_experience_years")
    return missing


def supplemental_questions(context: UserContext) -> List[str]:
    prompts = {
        "desired_roles": "어떤 직무명/역할을 우선 지원 대상으로 볼까요?",
        "skills": "평가에 반드시 반영할 핵심 기술 스택은 무엇인가요?",
        "preferred_locations": "선호 근무지 또는 원격/하이브리드 조건은 무엇인가요?",
        "max_experience_years": "지원 가능한 최대 요구 경력 연차는 몇 년인가요?",
    }
    return [prompts[field] for field in missing_context_fields(context)]


def merge_supplemental_answers(context: UserContext, answers: Dict[str, str]) -> UserContext:
    def split_values(value: str) -> List[str]:
        return [item.strip() for item in re.split(r"[,\n]", value) if item.strip()]

    updates = {
        "desired_roles": list(context.desired_roles),
        "skills": list(context.skills),
        "preferred_locations": list(context.preferred_locations),
        "max_experience_years": context.max_experience_years,
    }
    provenance = dict(context.provenance)
    if answers.get("desired_roles"):
        updates["desired_roles"] = split_values(answers["desired_roles"])
        provenance["desired_roles"] = "supplemental_interview"
    if answers.get("skills"):
        updates["skills"] = split_values(answers["skills"])
        provenance["skills"] = "supplemental_interview"
    if answers.get("preferred_locations"):
        updates["preferred_locations"] = split_values(answers["preferred_locations"])
        provenance["preferred_locations"] = "supplemental_interview"
    if answers.get("max_experience_years"):
        updates["max_experience_years"] = int(answers["max_experience_years"])
        provenance["max_experience_years"] = "supplemental_interview"
    return replace(
        context,
        desired_roles=updates["desired_roles"],
        skills=updates["skills"],
        preferred_locations=updates["preferred_locations"],
        max_experience_years=updates["max_experience_years"],
        missing_context=missing_context_fields(
            UserContext(
                desired_roles=updates["desired_roles"],
                skills=updates["skills"],
                preferred_locations=updates["preferred_locations"],
                max_experience_years=updates["max_experience_years"],
            )
        ),
        provenance=provenance,
    )


def parse_context_document(path: Path) -> UserContext:
    text = _read_document_text(path)
    _reject_private_text(text)
    context = _context_from_text(text)
    return replace(context, missing_context=missing_context_fields(context))


def _read_document_text(path: Path) -> str:
    suffix = path.suffix.lower()
    if suffix in {".txt", ".md"}:
        return path.read_text(encoding="utf-8")
    if suffix == ".pdf":
        try:
            from pypdf import PdfReader
        except Exception as exc:  # pragma: no cover - dependency import guard
            raise UserContextImportError("pypdf is required for PDF context import") from exc
        try:
            reader = PdfReader(str(path))
            if reader.is_encrypted:
                raise UserContextImportError("encrypted PDF context documents are not supported")
            text = "\n".join(page.extract_text() or "" for page in reader.pages)
        except UserContextImportError:
            raise
        except Exception as exc:
            raise UserContextImportError("PDF context document could not be parsed") from exc
        if not text.strip():
            raise UserContextImportError("PDF context document did not contain extractable text")
        return text
    if suffix == ".docx":
        try:
            from docx import Document
        except Exception as exc:  # pragma: no cover - dependency import guard
            raise UserContextImportError("python-docx is required for DOCX context import") from exc
        try:
            doc = Document(str(path))
            text = "\n".join(paragraph.text for paragraph in doc.paragraphs)
        except Exception as exc:
            raise UserContextImportError("DOCX context document could not be parsed") from exc
        if not text.strip():
            raise UserContextImportError("DOCX context document did not contain extractable text")
        return text
    raise UserContextImportError(f"unsupported context document type: {suffix or '<none>'}")


def _reject_private_text(text: str) -> None:
    for pattern in _PRIVATE_PATTERNS:
        if pattern.search(text):
            raise UserContextImportError("context document contains private canary text")


def _context_from_text(text: str) -> UserContext:
    sections = _section_values(text)
    skills = sections.get("skills", []) or _infer_keywords(text)
    context = UserContext(
        desired_roles=sections.get("roles", []),
        skills=skills,
        preferred_locations=sections.get("locations", []),
        max_experience_years=_infer_max_experience(text),
        explicit_deal_breakers=sections.get("deal_breakers", []),
        provenance={
            "desired_roles": "document.roles",
            "skills": "document.skills",
            "preferred_locations": "document.locations",
            "max_experience_years": "document.experience",
            "explicit_deal_breakers": "document.deal_breakers",
        },
    )
    return context


def _section_values(text: str) -> Dict[str, List[str]]:
    aliases = {
        "roles": ("roles", "desired roles", "직무", "희망 직무"),
        "skills": ("skills", "기술", "스킬"),
        "locations": ("locations", "preferred locations", "근무지", "선호 근무지"),
        "deal_breakers": ("deal breakers", "exclusions", "제외", "딜브레이커"),
    }
    result: Dict[str, List[str]] = {}
    for line in text.splitlines():
        if ":" not in line:
            continue
        key, value = line.split(":", 1)
        normalized_key = key.strip().lower()
        for field, names in aliases.items():
            if normalized_key in names:
                result[field] = [item.strip() for item in re.split(r"[,;/]", value) if item.strip()]
    return result


def _infer_keywords(text: str) -> List[str]:
    known = ["Python", "Machine Learning", "ML", "LLM", "Django", "FastAPI", "SQL", "Data", "검색", "추천"]
    lowered = text.lower()
    return [keyword for keyword in known if keyword.lower() in lowered]


def _infer_max_experience(text: str) -> int:
    matches = [int(value) for value in re.findall(r"(\d+)\s*(?:years?|년)", text, flags=re.I)]
    return max(matches) if matches else 0
