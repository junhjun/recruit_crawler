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
from .platform_shared import (
    _as_text_list,
    _clean_extracted_text,
    _clean_visible_text,
    _filter_candidates,
    _first_match,
    _looks_like_location,
    _merged_options,
    _section_between,
    _strip_tags,
)

def _rocketpunch_detail_text(text: str, title: str) -> str:
    marker = f"List {title}"
    start = text.find(marker)
    if start < 0:
        start = text.rfind(title)
    if start < 0:
        return ""
    return text[start:]


def _rocketpunch_deadline(text: str) -> Optional[str]:
    match = re.search(r"You can apply until ([A-Za-z]+) (\d{1,2}), (\d{4})", text)
    if not match:
        return None
    months = {
        "january": "01",
        "february": "02",
        "march": "03",
        "april": "04",
        "may": "05",
        "june": "06",
        "july": "07",
        "august": "08",
        "september": "09",
        "october": "10",
        "november": "11",
        "december": "12",
    }
    month = months.get(match.group(1).lower())
    if not month:
        return None
    return f"{match.group(3)}-{month}-{int(match.group(2)):02d}"


def _rocketpunch_posting_id(block: str) -> str:
    return _first_match(block, r'href="/en/jobs/(\d+)(?:\?[^"]*)?"') or _first_match(
        block,
        r"selectedJobId=(\d+)",
    )


def _rocketpunch_source_url(listing_url: str, posting_id: str) -> str:
    if posting_id.startswith("listing-"):
        return listing_url
    return urljoin(listing_url, f"/en/jobs?selectedJobId={posting_id}")

def _rocketpunch_card_title(block: str, text: str) -> str:
    for pattern in (
        r'<p[^>]*class="[^"]*BodyM_Bold[^"]*"[^>]*>(.*?)</p>',
        r'<(?:h1|h2|h3|strong)[^>]*class="[^"]*(?:title|job)[^"]*"[^>]*>(.*?)</(?:h1|h2|h3|strong)>',
        r'<(?:h1|h2|h3)[^>]*>(.*?)</(?:h1|h2|h3)>',
        r'data-title="([^"]+)"',
    ):
        value = _first_match(block, pattern)
        if value:
            return _strip_tags(value)
    for line in _rocketpunch_lines(text):
        if not _rocketpunch_is_noise_line(line) and not _looks_like_location(line):
            return line
    return ""


def _rocketpunch_card_company(block: str, text: str, title: str) -> str:
    for pattern in (
        r'<p[^>]*class="[^"]*BodyS[^"]*secondary[^"]*"[^>]*>(.*?)</p>',
        r'<[^>]*class="[^"]*(?:company|organization|name)[^"]*"[^>]*>(.*?)</[^>]+>',
        r'data-company="([^"]+)"',
    ):
        value = _first_match(block, pattern)
        if value:
            return _strip_tags(value)
    lines = _rocketpunch_lines(text)
    if title in lines:
        index = lines.index(title)
        if index > 0 and not _rocketpunch_is_noise_line(lines[index - 1]):
            return lines[index - 1]
    return ""


def _rocketpunch_card_snippet(text: str, title: str, company: str) -> str:
    ignored = {title, company, "quick apply", "apply", "view"}
    lines = [
        line
        for line in _rocketpunch_lines(text)
        if line.lower() not in ignored and not _rocketpunch_is_noise_line(line)
    ]
    return " ".join(lines[:6])[:600]


def _rocketpunch_card_location(text: str) -> str:
    return next((line for line in _rocketpunch_lines(text) if _looks_like_location(line)), "")


def _rocketpunch_experience_tags(text: str) -> List[str]:
    return _as_text_list(
        _first_match(text, r"(신입|경력\s*\d+년\s*이상|경력\s*\d+~\d+년|경력무관|[0-9]+\+?\s*years?)")
    )


def _rocketpunch_skill_terms(text: str) -> List[str]:
    known = [
        "Python",
        "SQL",
        "PyTorch",
        "TensorFlow",
        "LLM",
        "RAG",
        "Kubernetes",
        "AWS",
        "FastAPI",
        "Django",
        "React",
        "Node.js",
        "DataOps",
        "Fintech",
        "AI",
        "Machine Learning",
        "Backend",
        "인공지능",
        "머신러닝",
        "딥러닝",
        "데이터",
        "백엔드",
    ]
    normalized = text.lower()
    return [skill for skill in known if skill.lower() in normalized]


def _rocketpunch_synthetic_id(index: int, company: str, title: str) -> str:
    slug = re.sub(r"[^a-z0-9가-힣]+", "-", f"{company}-{title}".lower()).strip("-")
    return f"listing-{index}-{slug[:80] or 'unknown'}"


def _rocketpunch_lines(text: str) -> List[str]:
    return [line.strip("•·-–— ") for line in re.split(r"\s{2,}|\n", text) if line.strip("•·-–— ")]


def _rocketpunch_is_noise_line(line: str) -> bool:
    normalized = line.lower()
    return normalized in {
        "job",
        "jobs",
        "company",
        "companies",
        "apply",
        "quick apply",
        "view",
    } or normalized.startswith("we prohibit and reject")


def _looks_like_rocketpunch_card_text(text: str) -> bool:
    return any(keyword in text.lower() for keyword in ("python", "data", "backend", "ai", "fintech"))
