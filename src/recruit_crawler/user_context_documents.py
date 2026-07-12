from __future__ import annotations

import re
from dataclasses import replace
from pathlib import Path
from typing import Dict, List

from .schemas import UserContext
from .user_context import MAX_REASONABLE_EXPERIENCE_YEARS, UserContextImportError, missing_context_fields

_PRIVATE_PATTERNS = (re.compile(r"PRIVATE_[A-Z0-9_]*CANARY", re.I), re.compile(r"RAW_[A-Z0-9_]*CANARY", re.I), re.compile(r"Ignore previous instructions", re.I))


def parse_context_document(path: Path) -> UserContext:
    text = _read_document_text(path)
    _reject_private_text(text)
    context = _context_from_text(text)
    return replace(context, missing_context=missing_context_fields(context))


def _reject_private_text(text: str) -> None:
    if any(pattern.search(text) for pattern in _PRIVATE_PATTERNS):
        raise UserContextImportError("context document contains private canary text")


def _read_document_text(path: Path) -> str:
    suffix = path.suffix.lower()
    if suffix in {".txt", ".md"}:
        return path.read_text(encoding="utf-8")
    if suffix == ".pdf":
        try:
            from pypdf import PdfReader
        except ImportError as exc:
            raise UserContextImportError("pypdf is required for PDF context import") from exc
        try:
            reader = PdfReader(str(path))
            if reader.is_encrypted:
                raise UserContextImportError("encrypted PDF context documents are not supported")
            text = "\n".join(page.extract_text() or "" for page in reader.pages)
        except (OSError, ValueError) as exc:
            raise UserContextImportError("PDF context document could not be parsed") from exc
        if not text.strip():
            raise UserContextImportError("PDF context document did not contain extractable text")
        return text
    if suffix == ".docx":
        try:
            from docx import Document
        except ImportError as exc:
            raise UserContextImportError("python-docx is required for DOCX context import") from exc
        try:
            text = "\n".join(paragraph.text for paragraph in Document(str(path)).paragraphs)
        except (OSError, ValueError) as exc:
            raise UserContextImportError("DOCX context document could not be parsed") from exc
        if not text.strip():
            raise UserContextImportError("DOCX context document did not contain extractable text")
        return text
    raise UserContextImportError(f"unsupported context document type: {suffix or '<none>'}")


def _context_from_text(text: str) -> UserContext:
    sections = _section_values(text)
    return UserContext(desired_roles=sections.get("roles", []), skills=sections.get("skills", []) or _infer_keywords(text), preferred_locations=sections.get("locations", []), max_experience_years=_infer_max_experience(text), explicit_deal_breakers=sections.get("deal_breakers", []), provenance={"desired_roles": "document.roles", "skills": "document.skills", "preferred_locations": "document.locations", "max_experience_years": "document.experience", "explicit_deal_breakers": "document.deal_breakers"})


def _section_values(text: str) -> Dict[str, List[str]]:
    aliases = {"roles": ("roles", "desired roles", "직무", "희망 직무"), "skills": ("skills", "기술", "스킬"), "locations": ("locations", "preferred locations", "근무지", "선호 근무지"), "deal_breakers": ("deal breakers", "exclusions", "제외", "딜브레이커")}
    result: Dict[str, List[str]] = {}
    for line in text.splitlines():
        if ":" not in line:
            continue
        key, value = line.split(":", 1)
        normalized_key = key.strip().lower()
        for field, names in aliases.items():
            if normalized_key in names:
                separator = r"[,;/]" if field in {"roles", "skills", "deal_breakers"} else r"[,;]"
                result[field] = [item.strip() for item in re.split(separator, value) if item.strip()]
    return result


def _infer_keywords(text: str) -> List[str]:
    known = ["Python", "Machine Learning", "ML", "LLM", "Django", "FastAPI", "SQL", "Data", "검색", "추천"]
    lowered = text.lower()
    return [keyword for keyword in known if keyword.lower() in lowered]


def _infer_max_experience(text: str) -> int:
    matches = [int(value) for value in re.findall(r"(\d+)\s*(?:years?|년)", text, flags=re.I)]
    reasonable_matches = [value for value in matches if 0 < value <= MAX_REASONABLE_EXPERIENCE_YEARS]
    return max(reasonable_matches) if reasonable_matches else 0
