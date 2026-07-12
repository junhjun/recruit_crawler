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
from .platform_shared import _clean_extracted_text, _clean_visible_text, _merged_options
from .platform_jobkorea_parse import (
    _first_match,
    _jobkorea_detail_snapshot,
    _looks_like_experience_tag,
    _looks_like_location,
    _looks_like_non_skill_tag,
    _looks_like_required_experience_text,
    _merge_jobkorea_json_ld_detail,
    _strip_tags,
)


class JobKoreaAdapter(PublicJobsHttpAdapter):
    def __init__(self, manifest: SourceManifest):
        super().__init__(
            _merged_options(
                manifest,
                {
                    "include_url_patterns": [
                        r"/Recruit/GI_Read",
                        r"/recruit/joblist",
                    ],
                    "exclude_url_patterns": [r"Login", r"Register", r"Apply"],
                    "link_include_keywords": [
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
                        "조리",
                        "주방",
                        "지게차",
                        "물류 알바",
                        "매장",
                        "판매",
                    ],
                    "max_pages": 20,
                    "delay_seconds": 1,
                    "require_robots": True,
                },
            )
        )

    def collect(self) -> List[PostingCandidate]:
        self._validate_access()
        html = self._fetch_api_html()
        if not html:
            if self.options.get("require_detail_body"):
                self.errors.append("JobKorea listing API HTML was empty")
                return []
            return super().collect()
        candidates = self._candidates_from_api_html(html)
        enriched: List[PostingCandidate] = []
        for candidate in candidates[: self.max_pages]:
            enriched_candidate = self._enrich_candidate_from_detail(candidate)
            if enriched_candidate is not None:
                enriched.append(enriched_candidate)
        return [candidate for candidate in enriched if candidate.title.strip() and self._candidate_matches_keywords(candidate)]

    def discover_urls(self) -> List[str]:
        self._validate_access()
        html = self._fetch_api_html()
        if not html:
            return super().discover_urls()
        parser = self._parse_html(html)
        urls = self._filter_job_links("https://www.jobkorea.co.kr/recruit/ai-jobs", parser.anchors)
        return [urljoin("https://www.jobkorea.co.kr", url) for url in urls][: self.max_pages]

    def _fetch_api_html(self) -> str:
        endpoint = str(
            self.options.get(
                "api_url",
                "https://www.jobkorea.co.kr/recruit/ai-jobs/GetRecruitList",
            )
        )
        params = {
            "pageNo": 1,
            "pageSize": self.max_pages,
            "keyword": str(self.options.get("keyword", "python")),
            "orderType": str(self.options.get("order_type", "1")),
        }
        params.update(dict(self.options.get("api_params", {})))
        response = self._post_fetch(endpoint, params)
        self._sleep()
        try:
            payload = json.loads(response.text)
        except json.JSONDecodeError:
            return ""
        return str(payload.get("html", ""))

    def _candidates_from_api_html(self, html: str) -> List[PostingCandidate]:
        candidates: List[PostingCandidate] = []
        for block in re.split(r'<li class="recruit-item[^"]*"', html)[1:]:
            link_match = re.search(r'href="(?P<href>/Recruit/GI_Read/[^"]+)"', block)
            title_match = re.search(r'<h3 class="title">(?P<title>.*?)</h3>', block, re.S)
            if not link_match or not title_match:
                continue
            company = _first_match(block, r'data-cname="([^"]+)"')
            deadline = _first_match(block, r'data-applyclosedt="(\d{4}-\d{2}-\d{2})')
            keywords = [
                _strip_tags(match)
                for match in re.findall(r'<li class="item[^"]*">(.*?)</li>', block, re.S)
            ]
            keywords = [keyword for keyword in keywords if keyword and keyword != "합격축하금"]
            location = next((item for item in keywords if _looks_like_location(item)), "")
            experience_tags = [keyword for keyword in keywords if _looks_like_experience_tag(keyword)]
            technical_keywords = [keyword for keyword in keywords if not _looks_like_non_skill_tag(keyword)]
            title = _strip_tags(title_match.group("title"))
            if _looks_like_required_experience_text(title):
                experience_tags.append(title)
            source_url = urljoin("https://www.jobkorea.co.kr", link_match.group("href")).split("?")[0]
            candidates.append(
                PostingCandidate(
                    source_id=self.manifest.source_id,
                    source_url=source_url,
                    source_posting_id=source_url.rsplit("/", 1)[-1],
                    title=title,
                    company=company or "jobkorea",
                    location=location,
                    deadline_raw=deadline,
                    collected_at=datetime.now(timezone.utc),
                    raw_jd={
                        "required_qualifications": technical_keywords,
                        "preferred_qualifications": technical_keywords,
                        "responsibilities": [title],
                        "company_info": [company] if company else [],
                        "experience_tags": experience_tags,
                    },
                )
            )
        return candidates

    def _enrich_candidate_from_detail(self, candidate: PostingCandidate) -> Optional[PostingCandidate]:
        if not bool(self.options.get("fetch_detail_pages", True)):
            return candidate
        try:
            response = self._fetch(candidate.source_url)
            self._sleep()
        except (HTTPException, OSError, SourceAccessError) as exc:
            if self.manifest.failure_mode == "fail_run":
                raise
            self.errors.append(f"{candidate.source_url}: {exc}")
            return None if self.options.get("require_detail_body") else candidate
        detail = _jobkorea_detail_snapshot(response.text)
        json_ld_candidates = self._candidates_from_json_ld(response.url, self._parse_html(response.text))
        if not any(detail.get(key) for key in ("responsibilities", "required_qualifications", "preferred_qualifications")):
            if json_ld_candidates:
                return _merge_jobkorea_json_ld_detail(candidate, json_ld_candidates[0])
            self.errors.append(f"{candidate.source_url}: detail body sections not found")
            return None if self.options.get("require_detail_body") else candidate
        raw_jd = dict(candidate.raw_jd)
        for key in ("responsibilities", "required_qualifications", "preferred_qualifications"):
            value = detail.get(key)
            if value:
                raw_jd[key] = value
        location_values = detail.get("location", [])
        location = location_values[0] if location_values else candidate.location
        if json_ld_candidates:
            json_ld = json_ld_candidates[0]
            raw_jd.setdefault("company_info", json_ld.raw_jd.get("company_info", []))
            if not location:
                location = json_ld.location
            deadline_raw = json_ld.deadline_raw or candidate.deadline_raw
        else:
            deadline_raw = candidate.deadline_raw
        return PostingCandidate(
            source_id=candidate.source_id,
            source_url=candidate.source_url,
            source_posting_id=candidate.source_posting_id,
            title=candidate.title,
            company=candidate.company,
            location=location,
            deadline_raw=deadline_raw,
            collected_at=candidate.collected_at,
            raw_jd=raw_jd,
        )
