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
from .platform_shared import _merged_options


def _sitemap_locations(xml: str) -> List[str]:
    return [
        unescape(match).strip()
        for match in re.findall(r"<loc>(.*?)</loc>", xml, re.S)
        if match.strip()
    ]


def _react_string_field(html: str, field: str) -> str:
    escaped_pattern = rf'\\"{re.escape(field)}\\"\s*:\s*\\"(?P<value>(?:\\\\.|[^\\"])*)\\"'
    raw_pattern = rf'"{re.escape(field)}"\s*:\s*"(?P<value>(?:\\.|[^"])*)"'
    match = re.search(escaped_pattern, html) or re.search(raw_pattern, html)
    if not match:
        return ""
    return _decode_json_string(match.group("value"))


def _react_position_title(html: str) -> str:
    escaped = re.search(r'\\"title\\"\s*:\s*\\"(?P<value>(?:\\\\.|[^\\"])*)\\"\s*,\s*\\"companyName\\"', html)
    raw = re.search(r'"title"\s*:\s*"(?P<value>(?:\\.|[^"])*)"\s*,\s*"companyName"', html)
    match = escaped or raw
    return _decode_json_string(match.group("value")) if match else ""


def _react_int_field(html: str, field: str) -> int | None:
    match = re.search(rf'\\"{re.escape(field)}\\"\s*:\s*(?P<value>\d+)', html) or re.search(
        rf'"{re.escape(field)}"\s*:\s*(?P<value>\d+)', html
    )
    return int(match.group("value")) if match else None


def _react_bool_field(html: str, field: str) -> bool:
    match = re.search(rf'\\"{re.escape(field)}\\"\s*:\s*(?P<value>true|false)', html) or re.search(
        rf'"{re.escape(field)}"\s*:\s*(?P<value>true|false)', html
    )
    return bool(match and match.group("value") == "true")


def _react_stack_values(html: str) -> List[str]:
    values = [
        _decode_json_string(match)
        for match in re.findall(r'\\"stack\\"\s*:\s*\\"((?:\\\\.|[^\\"])*)\\"', html)
    ]
    values.extend(
        _decode_json_string(match)
        for match in re.findall(r'"stack"\s*:\s*"((?:\\.|[^"])*)"', html)
    )
    return list(dict.fromkeys(value for value in values if value))


def _decode_json_string(value: str) -> str:
    try:
        return json.loads(f'"{value}"').strip()
    except json.JSONDecodeError:
        return unescape(value).strip()

class JumpitAdapter(PublicJobsHttpAdapter):
    def __init__(self, manifest: SourceManifest):
        super().__init__(
            _merged_options(
                manifest,
                {
                    "sitemap_urls": [
                        "https://jumpit.saramin.co.kr/sitemap/sitemap_position_view_1.xml"
                    ],
                    "include_url_patterns": [r"/position/\d+"],
                    "exclude_url_patterns": [
                        r"/resumes",
                        r"/resume/",
                        r"/myjumpit",
                        r"/applications-status/",
                        r"/auth/",
                    ],
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
                        "반도체",
                        "fpga",
                        "asic",
                        "verilog",
                        "디자인",
                    ],
                    "max_pages": 25,
                    "delay_seconds": 1,
                    "require_robots": True,
                },
            )
        )

    def discover_urls(self) -> List[str]:
        self._validate_access()
        urls: List[str] = []
        for sitemap_url in self.options.get("sitemap_urls", []):
            response = self._fetch(str(sitemap_url))
            urls.extend(_sitemap_locations(response.text))
            self._sleep()
        return self._filter_sitemap_urls(urls)[: self.max_pages]

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
            candidate = self._candidate_from_jumpit_page(response.url, response.text)
            if candidate.title.strip() and self._candidate_matches_keywords(candidate):
                candidates.append(candidate)
            self._sleep()
        return candidates

    def _filter_sitemap_urls(self, urls: List[str]) -> List[str]:
        include_patterns = [
            re.compile(pattern)
            for pattern in self.options.get("include_url_patterns", [r"/position/\d+"])
        ]
        exclude_patterns = [
            re.compile(pattern)
            for pattern in self.options.get("exclude_url_patterns", [])
        ]
        filtered = []
        for url in urls:
            if any(pattern.search(url) for pattern in exclude_patterns):
                continue
            if any(pattern.search(url) for pattern in include_patterns):
                filtered.append(url)
        return list(dict.fromkeys(filtered))

    def _candidate_from_jumpit_page(self, source_url: str, html: str) -> PostingCandidate:
        title = _react_position_title(html) or _og_title(html).replace("점핏 | ", "")
        company = _react_string_field(html, "companyName") or _og_description_company(html) or "jumpit"
        location = _react_string_field(html, "location")
        deadline = _date_prefix(_react_string_field(html, "closedAt"))
        responsibilities = _react_string_field(html, "responsibility")
        qualifications = _react_string_field(html, "qualifications")
        preferred = _react_string_field(html, "preferredRequirements")
        service_info = _react_string_field(html, "serviceInfo")
        tech_stacks = _react_stack_values(html)
        min_career = _react_int_field(html, "minCareer")
        experience_tags = []
        if _react_bool_field(html, "newcomer"):
            experience_tags.append("신입")
        if min_career is not None:
            experience_tags.append(f"경력{min_career}년↑")
        return PostingCandidate(
            source_id=self.manifest.source_id,
            source_url=source_url,
            source_posting_id=source_url.rstrip("/").rsplit("/", 1)[-1],
            title=title,
            company=company,
            location=location,
            deadline_raw=deadline,
            collected_at=datetime.now(timezone.utc),
            raw_jd={
                "required_qualifications": [qualifications, *tech_stacks],
                "preferred_qualifications": [preferred],
                "responsibilities": [responsibilities],
                "company_info": [service_info],
                "experience_tags": experience_tags,
            },
        )
