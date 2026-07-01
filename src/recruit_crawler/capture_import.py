from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Iterable, List, Optional, Sequence
from urllib.parse import urlparse

from .schemas import PostingCandidate


class CaptureImportError(ValueError):
    pass


@dataclass(frozen=True)
class CaptureImportSelection:
    files: List[Path]
    run_date: date


@dataclass(frozen=True)
class CaptureImportResult:
    candidates: List[PostingCandidate]
    sources_attempted: List[str]
    source_errors: List[str] = field(default_factory=list)
    skipped_files: List[str] = field(default_factory=list)


SENSITIVE_KEY_RE = re.compile(r"(cookie|token|secret|password|authorization|credential|session|email|phone|resume|personal)", re.I)
LOCATION_SUFFIX_RE = re.compile(r"\s*(?:마감일|마감|D[-+]?\d+|~\s*\d{1,2}/\d{1,2}).*$")
CREDENTIAL_VALUE_RE = re.compile(r"(bearer\s+[a-z0-9._-]+|li_at=|csrf|session[_-]?id|auth[_-]?token)", re.I)
PUBLIC_CONTACT_RE = re.compile(r"[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}|\b0\d{1,2}[-.\s]?\d{3,4}[-.\s]?\d{4}\b")
TECH_TERMS = [
    "Python",
    "PyTorch",
    "TensorFlow",
    "JAX",
    "LLM",
    "RAG",
    "FastAPI",
    "SQL",
    "Redis",
    "Elasticsearch",
    "Docker",
    "Kubernetes",
    "AWS",
    "GCP",
    "Azure",
    "Pandas",
    "NumPy",
    "Machine Learning",
    "Deep Learning",
    "Reinforcement Learning",
    "MLOps",
]


def select_capture_files(
    spool_dir: Path,
    *,
    run_date: Optional[date] = None,
    latest: bool = False,
    files: Optional[Sequence[Path]] = None,
) -> CaptureImportSelection:
    if files:
        selected = [path.expanduser().resolve() for path in files]
        selected_date = run_date or date.today()
        return CaptureImportSelection(_dedupe_paths(selected), selected_date)

    root = spool_dir.expanduser().resolve()
    if run_date and latest:
        raise CaptureImportError("--date and --latest cannot be used together")
    if run_date:
        selected_date = run_date
    else:
        selected_date = _latest_capture_date(root) if latest else date.today()

    day_dir = root / selected_date.isoformat()
    if not day_dir.exists():
        raise CaptureImportError(f"capture date directory not found: {day_dir}")
    selected = sorted(path for path in day_dir.rglob("*.json") if path.is_file())
    if not selected:
        raise CaptureImportError(f"no capture JSON files found under: {day_dir}")
    return CaptureImportSelection(_dedupe_paths(selected), selected_date)


def import_capture_files(paths: Iterable[Path]) -> CaptureImportResult:
    candidates: List[PostingCandidate] = []
    errors: List[str] = []
    skipped: List[str] = []
    seen_candidates: set[tuple[str, str]] = set()
    sources: set[str] = set()

    for path in _dedupe_paths([Path(path) for path in paths]):
        try:
            file_candidates = _load_capture_file(path)
        except CaptureImportError as exc:
            errors.append(f"{path}: {exc}")
            continue
        if not file_candidates:
            skipped.append(f"{path}: empty postings")
            continue
        for candidate in file_candidates:
            sources.add(candidate.source_id)
            key = (candidate.source_id, candidate.source_posting_id or candidate.source_url)
            if key in seen_candidates:
                skipped.append(f"{path}: duplicate posting {key[0]}:{key[1]}")
                continue
            seen_candidates.add(key)
            candidates.append(candidate)

    return CaptureImportResult(
        candidates=candidates,
        sources_attempted=sorted(sources),
        source_errors=errors + skipped,
        skipped_files=skipped,
    )


def build_capture_quality_gate(
    selection: CaptureImportSelection,
    imported: CaptureImportResult,
) -> dict[str, Any]:
    findings = []
    for error in imported.source_errors:
        severity = "fail" if _is_blocking_import_error(error) else "warning"
        findings.append({"severity": severity, "message": error})

    privacy = _privacy_findings(imported.candidates)
    manual_review_items = [
        {
            "source_id": candidate.source_id,
            "source_posting_id": candidate.source_posting_id,
            "source_url": candidate.source_url,
            "flags": list(candidate.raw_jd.get("manual_review_flags", [])),
        }
        for candidate in imported.candidates
        if candidate.raw_jd.get("manual_review_flags")
    ]
    source_mode_counts: dict[str, int] = {}
    for candidate in imported.candidates:
        source_mode_counts[candidate.source_id] = source_mode_counts.get(candidate.source_id, 0) + 1

    has_failures = any(item["severity"] == "fail" for item in findings)
    has_failures = has_failures or any(item["category"] == "fail" for item in privacy)
    status = "fail" if has_failures else ("manual_review_required" if manual_review_items else ("pass_with_warnings" if findings or privacy else "pass"))

    return {
        "status": status,
        "checked_at": datetime.now(timezone.utc).isoformat(),
        "selected_date": selection.run_date.isoformat(),
        "input_files": [str(path) for path in selection.files],
        "sources_attempted": imported.sources_attempted,
        "source_mode_counts": source_mode_counts,
        "candidates_collected": len(imported.candidates),
        "findings": findings,
        "privacy": privacy,
        "manual_review_items": manual_review_items,
    }


def _is_blocking_import_error(error: str) -> bool:
    return any(
        token in error
        for token in (
            "invalid JSON",
            "must be an object",
            "must be a list",
            "requires source_id/source_url/title/company",
            "sensitive field",
        )
    )


def _privacy_findings(candidates: Iterable[PostingCandidate]) -> list[dict[str, Any]]:
    findings: list[dict[str, Any]] = []
    for candidate in candidates:
        text = " ".join(
            str(value)
            for value in [
                candidate.title,
                candidate.company,
                candidate.location,
                *candidate.raw_jd.get("required_qualifications", []),
                *candidate.raw_jd.get("preferred_qualifications", []),
                *candidate.raw_jd.get("responsibilities", []),
                *candidate.raw_jd.get("company_info", []),
            ]
        )
        if CREDENTIAL_VALUE_RE.search(text):
            findings.append(
                {
                    "category": "fail",
                    "source_id": candidate.source_id,
                    "source_posting_id": candidate.source_posting_id,
                    "message": "credential/session/auth-like value detected in imported posting text",
                }
            )
        if PUBLIC_CONTACT_RE.search(text):
            findings.append(
                {
                    "category": "warning",
                    "source_id": candidate.source_id,
                    "source_posting_id": candidate.source_posting_id,
                    "message": "public JD contact email/phone detected; keep as warning/manual review or redact by policy",
                }
            )
    return findings

def _latest_capture_date(root: Path) -> date:
    if not root.exists():
        raise CaptureImportError(f"capture spool directory not found: {root}")
    dates = []
    for child in root.iterdir():
        if child.is_dir():
            try:
                dates.append(date.fromisoformat(child.name))
            except ValueError:
                continue
    if not dates:
        raise CaptureImportError(f"no YYYY-MM-DD capture directories found under: {root}")
    return max(dates)


def _dedupe_paths(paths: Sequence[Path]) -> List[Path]:
    selected: List[Path] = []
    seen: set[Path] = set()
    for path in paths:
        resolved = path.expanduser().resolve()
        if resolved in seen:
            continue
        seen.add(resolved)
        selected.append(resolved)
    return selected


def _load_capture_file(path: Path) -> List[PostingCandidate]:
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
    source_id = _clean_scalar(raw.get("source_id"))
    captured_at = _parse_datetime(raw.get("captured_at"))
    candidates = []
    for index, posting in enumerate(postings, start=1):
        if not isinstance(posting, dict):
            raise CaptureImportError(f"posting #{index} must be an object")
        candidates.append(_candidate_from_capture_posting(posting, source_id, captured_at))
    return candidates


def _candidate_from_capture_posting(
    posting: dict[str, Any],
    capture_source_id: str,
    capture_collected_at: datetime,
) -> PostingCandidate:
    for key in posting:
        if SENSITIVE_KEY_RE.search(str(key)):
            raise CaptureImportError(f"sensitive field is not importable: {key}")

    source_id = _clean_scalar(posting.get("source_id")) or capture_source_id or _source_from_url(posting.get("source_url"))
    source_url = _clean_scalar(posting.get("source_url"))
    title = _clean_scalar(posting.get("title"))
    company = _clean_scalar(posting.get("company"))
    if not source_id or not source_url or not title or not company:
        raise CaptureImportError("posting requires source_id/source_url/title/company")

    requirements = _clean_scalar(posting.get("requirements"))
    skills = _clean_list(posting.get("skills"))
    location = _normalize_location(_clean_scalar(posting.get("location")), source_id, requirements)
    deadline = _normalize_deadline(_clean_scalar(posting.get("deadline")), requirements)
    collected_at = _parse_datetime(posting.get("captured_at"), fallback=capture_collected_at)

    extracted_terms = _extract_tech_terms(requirements)
    required = _unique_nonempty([*skills, *extracted_terms, *_requirement_lines(requirements)])
    responsibilities = _unique_nonempty(_section_lines(requirements, ["주요업무", "이런 업무", "General Summary", "Responsibilities"]))
    preferred = _unique_nonempty(_section_lines(requirements, ["우대사항", "이런 분이면 더 좋아요", "Preferred"]))
    company_info = _unique_nonempty(_section_lines(requirements, ["회사 소개", "서비스 소개", "Company", "회사정보"])[:3])
    experience_tags = _unique_nonempty([item for item in [*skills, *_requirement_lines(requirements)] if "경력" in item or "신입" in item])
    manual_review_flags = []
    if source_id == "saramin" and not requirements:
        manual_review_flags.append("본문 OCR 필요: 사람인 이미지형 JD 또는 DOM 텍스트 없음")

    return PostingCandidate(
        source_id=source_id,
        source_url=source_url,
        source_posting_id=_clean_scalar(posting.get("source_posting_id")) or _id_from_url(source_url),
        title=title,
        company=company,
        location=location,
        deadline_raw=deadline,
        collected_at=collected_at,
        raw_jd={
            "required_qualifications": required,
            "preferred_qualifications": preferred,
            "responsibilities": responsibilities,
            "company_info": company_info or [company],
            "experience_tags": experience_tags,
            "manual_review_flags": manual_review_flags,
        },
    )


def _clean_scalar(value: Any) -> str:
    if value is None:
        return ""
    return " ".join(str(value).split()).strip()


def _clean_list(value: Any) -> List[str]:
    if not isinstance(value, list):
        return []
    return [_clean_scalar(item) for item in value if _clean_scalar(item)]


def _parse_datetime(value: Any, fallback: Optional[datetime] = None) -> datetime:
    text = _clean_scalar(value)
    if text:
        try:
            parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
            return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
        except ValueError:
            pass
    return fallback or datetime.now(timezone.utc)


def _normalize_location(value: str, source_id: str, requirements: str) -> str:
    cleaned = LOCATION_SUFFIX_RE.sub("", value).strip()
    if source_id == "jobkorea":
        match = re.search(r"근무지(?:\s*주소)?\s*[:：]\s*([^\nㆍ]+)", requirements)
        if match:
            extracted = _clean_scalar(
                re.split(
                    r"지도보기|함께하면|이 기간동안|합류 여정|지원 시",
                    match.group(1),
                    maxsplit=1,
                )[0]
            )
            if extracted:
                return extracted
    return cleaned


def _normalize_deadline(value: str, requirements: str) -> Optional[str]:
    for text in (value, requirements):
        parsed = _extract_date(text)
        if parsed:
            return parsed.isoformat()
    return value or None


def _extract_date(text: str) -> Optional[date]:
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


def _extract_tech_terms(text: str) -> List[str]:
    normalized = text.lower()
    found = []
    for term in TECH_TERMS:
        if term.lower() in normalized:
            found.append(term)
    return found

def _requirement_lines(text: str) -> List[str]:
    lines = []
    for line in re.split(r"[\n•ㆍ]+", text):
        item = _clean_scalar(line)
        if item:
            lines.append(_truncate(item, 180))
        if len(lines) >= 8:
            break
    if not lines and text:
        lines.append(_truncate(text, 240))
    return lines


def _section_lines(text: str, headings: Sequence[str]) -> List[str]:
    if not text:
        return []
    lowered = text.lower()
    start = -1
    for heading in headings:
        start = lowered.find(heading.lower())
        if start >= 0:
            break
    if start < 0:
        return []
    section = text[start : start + 1200]
    return _requirement_lines(section)[1:5] or _requirement_lines(section)[:3]


def _unique_nonempty(items: Iterable[str]) -> List[str]:
    result = []
    seen = set()
    for item in items:
        cleaned = _clean_scalar(item)
        if not cleaned or cleaned in seen:
            continue
        seen.add(cleaned)
        result.append(cleaned)
    return result


def _truncate(value: str, limit: int) -> str:
    return value if len(value) <= limit else value[: limit - 1].rstrip() + "…"


def _source_from_url(value: Any) -> str:
    host = urlparse(_clean_scalar(value)).netloc.lower()
    if "saramin" in host:
        return "saramin"
    if "jobkorea" in host:
        return "jobkorea"
    if "linkedin" in host:
        return "linkedin"
    return host.split(".")[-2] if "." in host else host


def _id_from_url(source_url: str) -> Optional[str]:
    matches = re.findall(r"\d{5,}", source_url)
    if matches:
        return matches[-1]
    return None
