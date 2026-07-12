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
    _as_text_list,
    _candidate_from_manual_record,
    _clean_visible_text,
    _filter_candidates,
    _first_match,
    _has_manual_records,
    _manual_records,
    _merged_options,
    _og_description_company,
    _og_title,
    _section_between,
)


def _candidate_from_wanted_detail(source_url: str, html: str) -> Optional[PostingCandidate]:
    text = _clean_visible_text(html)
    responsibilities = _section_between(text, ["주요업무"], ["자격요건", "우대사항", "혜택 및 복지", "기술 스택", "마감일"])
    required = _section_between(text, ["자격요건"], ["우대사항", "혜택 및 복지", "기술 스택", "마감일", "근무지역"])
    preferred = _section_between(text, ["우대사항"], ["혜택 및 복지", "기술 스택", "마감일", "근무지역"])
    if not any((responsibilities, required, preferred)):
        return None
    title = _wanted_detail_title(text, html)
    company = _wanted_detail_company(text, html)
    location = _first_match(text, r"근무지역\s+(.+?)(?:\s+[가-힣A-Za-z0-9()]+기업소개|본 채용정보|지원하기|$)")
    deadline = _first_match(text, r"마감일\s+(\d{4}[.-]\d{1,2}[.-]\d{1,2}|상시|채용시)")
    skills = _wanted_skill_terms(text)
    return PostingCandidate(
        source_id="wanted",
        source_url=source_url,
        source_posting_id=_first_match(source_url, r"/wd/(\d+)") or None,
        title=title,
        company=company or "wanted",
        location=location,
        deadline_raw=deadline or None,
        collected_at=datetime.now(timezone.utc),
        raw_jd={
            "required_qualifications": [required, *skills] if required else skills,
            "preferred_qualifications": [preferred] if preferred else [],
            "responsibilities": [responsibilities] if responsibilities else [],
            "company_info": [company] if company else [],
            "experience_tags": _as_text_list(_first_match(text, r"(신입-경력\s*\d+년|경력\s*\d+-\d+년|경력\s*\d+년\s*이상|경력 무관|신입)")),
        },
    )


def _wanted_detail_title(text: str, html: str) -> str:
    title = _og_title(html)
    if "] " in title and " 채용 공고" in title:
        return title.split("] ", 1)[1].split(" 채용 공고", 1)[0].strip()
    match = re.search(r"경력\s*[\d무관신입~\\- ]+년?\s+(.+?)\s+합격보상", text)
    return match.group(1).strip() if match else title.split("|", 1)[0].strip()


def _wanted_detail_company(text: str, html: str) -> str:
    title = _og_title(html)
    match = re.match(r"\[([^\]]+)\]", title)
    if match:
        return match.group(1).strip()
    match = re.search(r"기업 서비스\s+([^∙]+)∙", text)
    return match.group(1).strip() if match else ""


def _wanted_skill_terms(text: str) -> List[str]:
    known = ["Python", "FastAPI", "RAG", "LLM", "Git", "MySQL", "JSON", "Excel", "PostgreSQL", "SQLite", "Selenium", "Playwright"]
    return [skill for skill in known if skill.lower() in text.lower()]


def _wanted_detail_urls_from_listing(html: str, link_include_keywords: List[str]) -> List[str]:
    urls: List[str] = []
    for block in re.findall(r"\{[^{}]*\"id\"\s*:\s*\d+[^{}]*\}", html, re.S):
        position_id = _first_match(block, r'"id"\s*:\s*(\d+)')
        if not position_id:
            continue
        if link_include_keywords and not _contains_any(block, link_include_keywords):
            continue
        urls.append(f"https://www.wanted.co.kr/wd/{position_id}")
    return list(dict.fromkeys(urls))


def _wanted_search_query(options: Dict[str, Any]) -> str:
    direct = str(options.get("keyword") or options.get("query") or "").strip()
    if direct:
        return direct
    for url in options.get("search_urls", []):
        parsed = urlparse(str(url))
        query_values = parse_qs(parsed.query).get("query", [])
        if query_values and query_values[0].strip():
            return query_values[0].strip()
    return ""


def _wanted_detail_urls_from_search_api(text: str) -> List[str]:
    try:
        payload = json.loads(text)
    except json.JSONDecodeError:
        return []
    data = payload.get("data") if isinstance(payload, dict) else []
    if not isinstance(data, list):
        return []
    urls: List[str] = []
    for item in data:
        if not isinstance(item, dict):
            continue
        position_id = item.get("id")
        if position_id in (None, ""):
            continue
        urls.append(f"https://www.wanted.co.kr/wd/{position_id}")
    return list(dict.fromkeys(urls))


class WantedAdapter(PublicJobsHttpAdapter):
    def __init__(self, manifest: SourceManifest):
        super().__init__(
            _merged_options(
                manifest,
                {
                    "include_url_patterns": [r"/wd/"],
                    "exclude_url_patterns": [r"login", r"signup"],
                    "max_pages": 20,
                    "delay_seconds": 1,
                    "require_robots": True,
                },
            )
        )

    def collect(self) -> List[PostingCandidate]:
        if _has_manual_records(self.options):
            return _filter_candidates(
                [_candidate_from_manual_record(self.manifest.source_id, record) for record in _manual_records(self.options)],
                self,
            )
        if self.manifest.access_mode == "public_page" and (
            self.options.get("detail_urls") or self.options.get("search_urls") or self.options.get("start_urls")
        ):
            self._validate_access()
            urls = self._seed_urls() if self.options.get("search_urls") or self.options.get("start_urls") else [
                str(value) for value in self.options.get("detail_urls", [])
            ]
            candidates = []
            for url in urls[: self.max_pages]:
                try:
                    response = self._fetch(url)
                except (HTTPException, OSError, SourceAccessError) as exc:
                    if self.manifest.failure_mode == "fail_run":
                        raise
                    self.errors.append(f"{url}: {exc}")
                    continue
                candidate = _candidate_from_wanted_detail(response.url, response.text)
                if candidate is not None:
                    candidates.append(candidate)
                self._sleep()
            return _filter_candidates(candidates[: self.max_pages], self)
        if not self.options.get("explicit_automated_permission"):
            raise SourceAccessError(
                "Wanted direct collection is blocked unless explicit automated-access permission "
                "or a reviewed public detail_urls/search_urls path is configured."
            )
        return super().collect()

    def discover_urls(self) -> List[str]:
        urls = super().discover_urls()
        if urls:
            return urls[: self.max_pages]
        return self._discover_urls_from_public_search_api()[: self.max_pages]

    def _discover_urls_from_listing(
        self,
        base_url: str,
        html: str,
        parser: Any,
    ) -> List[str]:
        return _wanted_detail_urls_from_listing(html, self.link_include_keywords)

    def _discover_urls_from_public_search_api(self) -> List[str]:
        query = _wanted_search_query(self.options)
        if not query:
            return []
        endpoint = str(
            self.options.get(
                "api_url",
                "https://www.wanted.co.kr/api/chaos/search/v1/position",
            )
        )
        params = {"query": query, "limit": self.max_pages}
        params.update(dict(self.options.get("api_params", {})))
        response = self._get_fetch(endpoint, params)
        return _wanted_detail_urls_from_search_api(response.text)
