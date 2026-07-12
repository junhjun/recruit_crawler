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
from http.client import HTTPException
from pathlib import Path
from typing import Any, Dict, List, Optional
from urllib.parse import parse_qs, urljoin, urlparse


from ..schemas import PostingCandidate, SourceManifest
from .http import PublicJobsHttpAdapter, SourceAccessError, _contains_any, _date_prefix
from .platform_shared import (
    _clean_extracted_text,
    _clean_visible_text,
    _first_match,
    _looks_like_location,
    _merged_options,
    _og_title,
    _section_between,
    _strip_tags,
)


class RallitAdapter(PublicJobsHttpAdapter):
    def __init__(self, manifest: SourceManifest):
        super().__init__(
            _merged_options(
                manifest,
                {
                    "search_urls": ["https://www.rallit.com"],
                    "include_url_patterns": [r"/positions/\d+"],
                    "exclude_url_patterns": [r"/apply", r"/auth", r"/my", r"/resume"],
                    "candidate_include_keywords": [
                        "ai",
                        "인공지능",
                        "머신러닝",
                        "machine learning",
                        "ml",
                        "데이터",
                        "data",
                        "python",
                        "파이썬",
                        "llm",
                        "딥러닝",
                        "추천",
                        "검색",
                        "백엔드",
                    ],
                    "candidate_exclude_keywords": [
                        "디자인",
                        "마케팅",
                        "영업",
                        "사업개발",
                    ],
                    "max_pages": 20,
                    "delay_seconds": 1,
                    "require_robots": True,
                },
            )
        )

    def collect(self) -> List[PostingCandidate]:
        self._validate_access()
        candidates: List[PostingCandidate] = []
        for url in self.discover_urls():
            try:
                response = self._fetch(url)
            except (HTTPException, OSError, SourceAccessError) as exc:
                if self.manifest.failure_mode == "fail_run":
                    raise
                self.errors.append(f"{url}: {exc}")
                continue
            candidate = self._candidate_from_rallit_page(response.url, response.text)
            if candidate.title.strip() and self._candidate_matches_keywords(candidate):
                candidates.append(candidate)
            self._sleep()
        return candidates

    def _candidate_from_rallit_page(self, source_url: str, html: str) -> PostingCandidate:
        text = _clean_visible_text(html)
        main_text = _rallit_main_text(text)
        title = _rallit_title(html, main_text)
        company = _rallit_company(html, main_text)
        location = _rallit_location(main_text)
        deadline = _rallit_deadline(main_text)
        skills = _rallit_skills(main_text)
        responsibilities = _clean_extracted_text(_section_between(main_text, ["주요업무", "합류하면 하게 될 업무", "어떤 일을 하나요?"], ["자격요건", "지원자격", "우대사항", "혜택 및 복지"]))
        qualifications = _clean_extracted_text(_section_between(main_text, ["자격요건", "지원자격"], ["우대사항", "혜택 및 복지", "채용 절차"]))
        preferred = _clean_extracted_text(_section_between(main_text, ["우대사항"], ["혜택 및 복지", "채용 절차"]))
        company_info = _clean_extracted_text(_section_between(main_text, ["어떤 곳인가요?"], ["어떤 일을 하나요?", "합류하면 하게 될 업무", "주요업무"]))
        experience = _section_between(main_text, ["경력"], ["최소 연봉", "마감일", "회사명"])
        posting_id = _first_match(source_url, r"/positions/(\d+)")
        return PostingCandidate(
            source_id=self.manifest.source_id,
            source_url=source_url,
            source_posting_id=posting_id or source_url.rstrip("/").rsplit("/", 1)[-1],
            title=title,
            company=company or "rallit",
            location=location,
            deadline_raw=deadline,
            collected_at=datetime.now(timezone.utc),
            raw_jd={
                "required_qualifications": [qualifications, *skills],
                "preferred_qualifications": [preferred],
                "responsibilities": [responsibilities],
                "company_info": [company_info],
                "experience_tags": [experience] if experience else [],
            },
        )


def _rallit_title(html: str, text: str) -> str:
    title = _og_title(html)
    if title:
        title = re.sub(r"\s+채용\s+-\s+랠릿$", "", title).strip()
        title = re.sub(r"^.+?\s+(\[.+)$", r"\1", title).strip()
        return title
    h1 = _first_match(html, r"<h1[^>]*>(.*?)</h1>")
    if h1:
        return _strip_tags(h1)
    match = re.search(r"#\s+(.+?)\s+(?:코드 리뷰|주요업무|.+어떤 곳인가요\?)", text)
    return match.group(1).strip() if match else ""


def _rallit_company(html: str, text: str) -> str:
    match = re.search(r"회사명\s+(.{1,80}?)\s+(?:\d+\s+)?지원하기", text)
    if match:
        return match.group(1).strip()
    title = _og_title(html)
    if title:
        return title.split(" ", 1)[0].strip()
    return ""


def _rallit_location(text: str) -> str:
    match = re.search(r"근무\s*지역\s+(.{1,100}?)(?:\s+\S+\s+경력|\s+경력|\s+최소 연봉|\s+마감일|\s+회사명)", text)
    if match:
        return match.group(1).strip()
    return ""


def _rallit_deadline(text: str) -> Optional[str]:
    match = re.search(r"마감일\s+(\d{4}[.-]\d{1,2}[.-]\d{1,2})", text)
    if match:
        return match.group(1).replace(".", "-")
    if re.search(r"마감일\s+채용\s*시\s*마감", text):
        return "채용시"
    return None


def _rallit_main_text(text: str) -> str:
    stop_markers = [
        "비슷한 채용 공고",
        "새 로그 작성",
        "기업 서비스",
        "©Rallit",
    ]
    stop_positions = [text.find(marker) for marker in stop_markers if text.find(marker) >= 0]
    return text[: min(stop_positions)].strip() if stop_positions else text


def _rallit_skills(text: str) -> List[str]:
    skill_scope = text.split("주요업무", 1)[0]
    hashtag_values = re.findall(r"#\s*([^#]+?)(?=\s*#|\s+주요업무|\s+자격요건|\s+우대사항|$)", skill_scope)
    cleaned = [value.strip() for value in hashtag_values if value.strip()]
    if cleaned:
        return list(dict.fromkeys(cleaned))
    known = ["Python", "SQL", "PyTorch", "TensorFlow", "LLM", "인공지능", "머신러닝", "딥러닝"]
    return [skill for skill in known if skill.lower() in text.lower()]
