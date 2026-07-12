from __future__ import annotations

import json
import re
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, List, Optional, Sequence
from urllib.parse import urlparse

from .capture_import_models import CaptureImportError
from .schemas import PostingCandidate

SENSITIVE_KEY_RE = re.compile(r"(cookie|token|secret|password|authorization|credential|session|email|phone|resume|personal)", re.I)
LOCATION_SUFFIX_RE = re.compile(r"\s*(?:마감일|마감|D[-+]?\d+|~\s*\d{1,2}/\d{1,2}).*$")
CREDENTIAL_VALUE_RE = re.compile(r"(bearer\s+[a-z0-9._-]+|li_at=|csrf|session[_-]?id|auth[_-]?token)", re.I)
TECH_TERMS = ["Python", "PyTorch", "TensorFlow", "JAX", "LLM", "RAG", "FastAPI", "SQL", "Redis", "Elasticsearch", "Docker", "Kubernetes", "AWS", "GCP", "Azure", "Pandas", "NumPy", "Machine Learning", "Deep Learning", "Reinforcement Learning", "MLOps"]


def load_capture_file(path: Path) -> List[PostingCandidate]:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise CaptureImportError("file not found") from exc
    except json.JSONDecodeError as exc:
        raise CaptureImportError(f"invalid JSON: {exc.msg}") from exc
    if not isinstance(raw, dict):
        raise CaptureImportError("capture root must be an object")
    postings = raw.get("postings")
    if not isinstance(postings, list):
        raise CaptureImportError("capture postings must be a list")
    source_id = clean_scalar(raw.get("source_id"))
    captured_at = parse_datetime(raw.get("captured_at"))
    candidates: List[PostingCandidate] = []
    for index, posting in enumerate(postings, start=1):
        if not isinstance(posting, dict):
            raise CaptureImportError(f"posting #{index} must be an object")
        candidates.append(candidate_from_capture_posting(posting, source_id, captured_at))
    return candidates


def candidate_from_capture_posting(posting: dict[str, Any], capture_source_id: str, capture_collected_at: datetime) -> PostingCandidate:
    for key in posting:
        if SENSITIVE_KEY_RE.search(str(key)):
            raise CaptureImportError(f"sensitive field is not importable: {key}")
    if any(contains_credential_like_value(value) for value in posting.values()):
        raise CaptureImportError("credential-like value is not importable")
    source_id = clean_scalar(posting.get("source_id")) or capture_source_id or source_from_url(posting.get("source_url"))
    source_url = clean_scalar(posting.get("source_url"))
    title = clean_scalar(posting.get("title"))
    company = clean_scalar(posting.get("company"))
    if not source_id or not source_url or not title or not company:
        raise CaptureImportError("posting requires source_id/source_url/title/company")
    requirements = clean_scalar(posting.get("requirements"))
    skills = clean_list(posting.get("skills"))
    return PostingCandidate(source_id=source_id, source_url=source_url, source_posting_id=clean_scalar(posting.get("source_posting_id")) or id_from_url(source_url), title=title, company=company, location=normalize_location(clean_scalar(posting.get("location")), source_id, requirements), deadline_raw=normalize_deadline(clean_scalar(posting.get("deadline")), requirements), collected_at=parse_datetime(posting.get("captured_at"), fallback=capture_collected_at), raw_jd={"required_qualifications": unique_nonempty([*skills, *extract_tech_terms(requirements), *requirement_lines(requirements)]), "preferred_qualifications": unique_nonempty(section_lines(requirements, ["우대사항", "이런 분이면 더 좋아요", "Preferred"])), "responsibilities": unique_nonempty(section_lines(requirements, ["주요업무", "이런 업무", "General Summary", "Responsibilities"])), "company_info": unique_nonempty(section_lines(requirements, ["회사 소개", "서비스 소개", "Company", "회사정보"])[:3]) or [company], "experience_tags": unique_nonempty([item for item in [*skills, *requirement_lines(requirements)] if "경력" in item or "신입" in item]), "manual_review_flags": ["본문 OCR 필요: 사람인 이미지형 JD 또는 DOM 텍스트 없음"] if source_id == "saramin" and not requirements else []})


def clean_scalar(value: Any) -> str:
    return " ".join(str(value).split()).strip() if value is not None else ""


def contains_credential_like_value(value: Any) -> bool:
    if isinstance(value, str):
        return CREDENTIAL_VALUE_RE.search(value) is not None
    if isinstance(value, list):
        return any(contains_credential_like_value(item) for item in value)
    if isinstance(value, dict):
        return any(contains_credential_like_value(item) for item in value.values())
    return False


def clean_list(value: Any) -> List[str]:
    return [clean_scalar(item) for item in value if clean_scalar(item)] if isinstance(value, list) else []


def parse_datetime(value: Any, fallback: Optional[datetime] = None) -> datetime:
    text = clean_scalar(value)
    if text:
        try:
            parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
            return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
        except ValueError:
            return fallback or datetime.now(timezone.utc)
    return fallback or datetime.now(timezone.utc)


def normalize_location(value: str, source_id: str, requirements: str) -> str:
    cleaned = LOCATION_SUFFIX_RE.sub("", value).strip()
    if source_id == "jobkorea":
        match = re.search(r"근무지(?:\s*주소)?\s*[:：]\s*([^\nㆍ]+)", requirements)
        if match:
            extracted = clean_scalar(re.split(r"지도보기|함께하면|이 기간동안|합류 여정|지원 시", match.group(1), maxsplit=1)[0])
            if extracted:
                return extracted
    return cleaned


def normalize_deadline(value: str, requirements: str) -> Optional[str]:
    for text in (value, requirements):
        parsed = extract_date(text)
        if parsed:
            return parsed.isoformat()
    return value or None


def extract_date(text: str) -> Optional[date]:
    if not text:
        return None
    match = re.search(r"(20\d{2})[.\-/년\s]+(\d{1,2})[.\-/월\s]+(\d{1,2})", text)
    if match:
        return date(int(match.group(1)), int(match.group(2)), int(match.group(3)))
    match = re.search(r"~\s*(\d{1,2})/(\d{1,2})", text)
    if match:
        today = date.today()
        return date(today.year, int(match.group(1)), int(match.group(2)))
    return None


def extract_tech_terms(text: str) -> List[str]:
    normalized = text.lower()
    return [term for term in TECH_TERMS if term.lower() in normalized]


def requirement_lines(text: str) -> List[str]:
    lines: List[str] = []
    for line in re.split(r"[\n•ㆍ]+", text):
        item = clean_scalar(line)
        if item:
            lines.append(truncate(item, 180))
        if len(lines) >= 8:
            break
    return lines or [truncate(text, 240)] if text else lines


def section_lines(text: str, headings: Sequence[str]) -> List[str]:
    lowered = text.lower()
    start = next((found for heading in headings if (found := lowered.find(heading.lower())) >= 0), -1)
    if start < 0:
        return []
    lines = requirement_lines(text[start : start + 1200])
    return lines[1:5] or lines[:3]


def unique_nonempty(items: Sequence[str]) -> List[str]:
    result: List[str] = []
    for item in items:
        cleaned = clean_scalar(item)
        if cleaned and cleaned not in result:
            result.append(cleaned)
    return result


def truncate(value: str, limit: int) -> str:
    return value if len(value) <= limit else value[: limit - 1].rstrip() + "…"


def source_from_url(value: Any) -> str:
    host = urlparse(clean_scalar(value)).netloc.lower()
    return next((source for source in ("saramin", "jobkorea", "wanted", "jumpit", "rallit", "rocketpunch") if source in host), host.split(".")[0] if host else "")


def id_from_url(value: str) -> Optional[str]:
    path = urlparse(value).path.rstrip("/")
    return path.rsplit("/", 1)[-1] or None
