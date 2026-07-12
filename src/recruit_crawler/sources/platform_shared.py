from __future__ import annotations

from dataclasses import replace
import csv
import json
import shutil
import subprocess
import os
import re
from datetime import datetime, timezone
from html import unescape
from pathlib import Path
from typing import Any, Dict, List, Optional
from urllib.parse import parse_qs, urljoin, urlparse


from ..schemas import PostingCandidate, SourceManifest
from .http import PublicJobsHttpAdapter, SourceAccessError, _contains_any, _date_prefix


def _merged_options(manifest: SourceManifest, defaults: Dict[str, Any]) -> SourceManifest:
    merged = dict(defaults)
    merged.update(manifest.options)
    return replace(manifest, options=merged)


class CompanyCareersAdapter(PublicJobsHttpAdapter):
    def __init__(self, manifest: SourceManifest):
        super().__init__(
            _merged_options(
                manifest,
                {
                    "include_url_patterns": [r"job", r"career", r"recruit", r"position"],
                    "exclude_url_patterns": [r"login", r"signup", r"privacy"],
                    "max_pages": 20,
                    "delay_seconds": 1,
                    "require_robots": True,
                },
            )
        )


def _first_match(text: str, pattern: str) -> str:
    match = re.search(pattern, text, re.S)
    return unescape(match.group(1)).strip() if match else ""


def _strip_tags(text: str) -> str:
    return _clean_extracted_text(re.sub(r"<[^>]+>", " ", text))


def _clean_visible_text(html: str) -> str:
    without_script = re.sub(r"<(script|style)[^>]*>.*?</\1>", " ", html, flags=re.I | re.S)
    with_breaks = re.sub(r"</?(br|p|div|li|section|article|h[1-6]|tr|td|th)[^>]*>", " ", without_script, flags=re.I)
    return _clean_extracted_text(re.sub(r"<[^>]+>", " ", with_breaks))


def _clean_extracted_text(text: str) -> str:
    cleaned = unescape(text)
    cleaned = re.sub(r"@(?:-webkit-)?keyframes\s+[^{\s]+{.*?}", " ", cleaned, flags=re.I | re.S)
    cleaned = re.sub(r"\.[A-Za-z0-9_-]+\{[^{}]*(?:\{[^{}]*\}[^{}]*)*\}", " ", cleaned)
    cleaned = re.sub(r"\b(?:display|align-items|justify-content|box-shadow|pointer-events|white-space|overflow|font-family|animation|opacity|color|height|width):[^;}]+[;}]", " ", cleaned)
    cleaned = re.sub(r"(?<!\w)[0-9A-Fa-f]{6};", " ", cleaned)
    return " ".join(cleaned.split())
def _looks_like_location(value: str) -> bool:
    return any(
        token in value
        for token in (
            "서울",
            "경기",
            "인천",
            "부산",
            "대전",
            "대구",
            "광주",
            "울산",
            "세종",
            "Remote",
            "Seoul",
            "remote",
        )
    )


def _looks_like_non_skill_tag(value: str) -> bool:
    return _looks_like_location(value) or "년↑" in value or "만원" in value
def _og_title(html: str) -> str:
    return _meta_content(html, "og:title")


def _og_description_company(html: str) -> str:
    description = _meta_content(html, "og:description")
    return description.split(" - ", 1)[0].strip() if " - " in description else ""


def _meta_content(html: str, property_name: str) -> str:
    match = re.search(
        rf'<meta\s+property="{re.escape(property_name)}"\s+content="([^"]*)"',
        html,
        re.I,
    )
    return unescape(match.group(1)).strip() if match else ""


def _has_manual_records(options: Dict[str, Any]) -> bool:
    return bool(options.get("manual_postings") or options.get("manual_export_path"))


def _manual_records(options: Dict[str, Any]) -> List[Dict[str, Any]]:
    inline = options.get("manual_postings")
    records: List[Dict[str, Any]] = []
    if isinstance(inline, list):
        records.extend(item for item in inline if isinstance(item, dict))
    export_path = options.get("manual_export_path")
    if export_path:
        records.extend(_records_from_file(Path(str(export_path))))
    return records


def _records_from_file(path: Path) -> List[Dict[str, Any]]:
    if path.suffix.lower() == ".csv":
        with path.open(newline="", encoding="utf-8") as handle:
            return [dict(row) for row in csv.DictReader(handle)]
    payload = json.loads(path.read_text(encoding="utf-8"))
    if isinstance(payload, list):
        return [item for item in payload if isinstance(item, dict)]
    if isinstance(payload, dict):
        for key in ("postings", "jobs", "elements", "data", "included"):
            value = payload.get(key)
            if isinstance(value, list):
                return [item for item in value if isinstance(item, dict)]
        return [payload]
    return []


def _candidate_from_manual_record(source_id: str, record: Dict[str, Any]) -> PostingCandidate:
    source_url = str(
        _first_present(record, "source_url", "url", "job_url", "apply_url", "link")
        or f"manual://{source_id}/{_first_present(record, 'id', 'job_id', 'source_posting_id') or 'unknown'}"
    )
    title = str(_first_present(record, "title", "job_title", "position", "name") or "")
    company = str(_first_present(record, "company", "company_name", "organization", "employer") or source_id)
    location = str(_first_present(record, "location", "address", "workplace") or "")
    deadline = _date_prefix(_first_present(record, "deadline", "deadline_raw", "validThrough", "expires_at"))
    required = _as_text_list(_first_present(record, "required_qualifications", "requirements", "description", "summary"))
    preferred = _as_text_list(_first_present(record, "preferred_qualifications", "preferred", "nice_to_have"))
    responsibilities = _as_text_list(_first_present(record, "responsibilities", "duties", "tasks"))
    skills = _as_text_list(_first_present(record, "skills", "tech_stacks", "tags"))
    experience = _as_text_list(_first_present(record, "experience_tags", "experience", "career"))
    return PostingCandidate(
        source_id=source_id,
        source_url=source_url,
        source_posting_id=str(_first_present(record, "source_posting_id", "id", "job_id") or ""),
        title=title,
        company=company,
        location=location,
        deadline_raw=deadline,
        collected_at=datetime.now(timezone.utc),
        raw_jd={
            "required_qualifications": [*required, *skills],
            "preferred_qualifications": preferred,
            "responsibilities": responsibilities or required,
            "company_info": _as_text_list(_first_present(record, "company_info", "company_description")),
            "experience_tags": experience,
        },
    )


def _filter_candidates(candidates: List[PostingCandidate], adapter: PublicJobsHttpAdapter) -> List[PostingCandidate]:
    return [
        candidate
        for candidate in candidates
        if candidate.title.strip() and adapter._candidate_matches_keywords(candidate)
    ]


def _option_secret(options: Dict[str, Any], value_key: str, env_key: str) -> str:
    direct = str(options.get(value_key, "")).strip()
    if direct:
        return direct
    env_name = str(options.get(env_key, "")).strip()
    return os.environ.get(env_name, "").strip() if env_name else ""


def _first_present(record: Dict[str, Any], *keys: str) -> Any:
    for key in keys:
        value = record.get(key)
        if value not in (None, ""):
            return value
    return None


def _as_text_list(value: Any) -> List[str]:
    if value is None or value == "":
        return []
    if isinstance(value, list):
        output: List[str] = []
        for item in value:
            output.extend(_as_text_list(item))
        return output
    if isinstance(value, dict):
        return _as_text_list(value.get("name") or value.get("title") or value.get("value"))
    return [str(value)]


def _dig(value: Dict[str, Any], *path: str) -> Any:
    current: Any = value
    for key in path:
        if not isinstance(current, dict):
            return None
        current = current.get(key)
    return current


def _section_between(text: str, starts: List[str], ends: List[str]) -> str:
    start_positions = [(text.find(start), start) for start in starts if text.find(start) >= 0]
    if not start_positions:
        return ""
    start_index, start_token = min(start_positions, key=lambda item: item[0])
    content_start = start_index + len(start_token)
    end_positions = [text.find(end, content_start) for end in ends if text.find(end, content_start) >= 0]
    content_end = min(end_positions) if end_positions else len(text)
    return text[content_start:content_end].strip()
