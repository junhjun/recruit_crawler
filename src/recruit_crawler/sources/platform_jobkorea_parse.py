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
from .platform_shared import _clean_extracted_text, _clean_visible_text, _merged_options

def _merge_jobkorea_json_ld_detail(candidate: PostingCandidate, json_ld: PostingCandidate) -> PostingCandidate:
    raw_jd = dict(candidate.raw_jd)
    for key in ("responsibilities", "required_qualifications", "preferred_qualifications", "company_info"):
        merged = [*json_ld.raw_jd.get(key, []), *raw_jd.get(key, [])] if key == "responsibilities" else [*raw_jd.get(key, []), *json_ld.raw_jd.get(key, [])]
        if merged:
            raw_jd[key] = list(dict.fromkeys(value for value in merged if str(value).strip()))
    if candidate.raw_jd.get("experience_tags") and not raw_jd.get("experience_tags"):
        raw_jd["experience_tags"] = candidate.raw_jd["experience_tags"]
    return PostingCandidate(
        source_id=candidate.source_id,
        source_url=candidate.source_url,
        source_posting_id=candidate.source_posting_id or json_ld.source_posting_id,
        title=candidate.title or json_ld.title,
        company=candidate.company or json_ld.company,
        location=json_ld.location or candidate.location,
        deadline_raw=json_ld.deadline_raw or candidate.deadline_raw,
        collected_at=candidate.collected_at,
        raw_jd=raw_jd,
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


def _jobkorea_detail_snapshot(html: str) -> Dict[str, List[str]]:
    text = _clean_visible_text(html)
    responsibilities = _jobkorea_section_between(
        text,
        ("이런 업무를 해요", "주요업무", "담당업무"),
        ("이런 분들을 찾고 있어요", "자격요건", "지원자격", "우대사항", "근무지", "근무조건"),
    )
    required = _jobkorea_section_between(
        text,
        ("이런 분들을 찾고 있어요", "자격요건", "지원자격"),
        ("우대사항", "이런 분이면 더 좋아요", "근무지", "근무조건", "복리후생"),
    )
    preferred = _jobkorea_section_between(
        text,
        ("우대사항", "이런 분이면 더 좋아요"),
        ("근무지", "근무조건", "복리후생", "접수기간", "마감일"),
    )
    location = _first_match(text, r"근무지\s*주소\s*[:：]?\s*(.+?)(?:\s*지도보기|$)")
    output: Dict[str, List[str]] = {
        "responsibilities": [responsibilities] if responsibilities else [],
        "required_qualifications": [required] if required else [],
        "preferred_qualifications": [preferred] if preferred else [],
    }
    if location:
        output["location"] = [location]
    return output


def _jobkorea_section_between(text: str, starts: tuple[str, ...], stops: tuple[str, ...]) -> str:
    start_positions = [(text.find(marker), marker) for marker in starts if marker in text]
    if not start_positions:
        return ""
    start_index, marker = min(start_positions, key=lambda item: item[0])
    content_start = start_index + len(marker)
    stop_positions = [text.find(stop, content_start) for stop in stops if text.find(stop, content_start) != -1]
    content_end = min(stop_positions) if stop_positions else len(text)
    return text[content_start:content_end].strip(" :-–—·\n\t")


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
    return (
        _looks_like_location(value)
        or _looks_like_experience_tag(value)
        or "년↑" in value
        or "만원" in value
    )


def _looks_like_experience_tag(value: str) -> bool:
    return "경력" in value or "신입" in value


def _looks_like_required_experience_text(value: str) -> bool:
    return "경력" in value and "신입" not in value and "경력무관" not in value
