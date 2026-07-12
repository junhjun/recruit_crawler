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
    _dig,
    _filter_candidates,
    _first_match,
    _has_manual_records,
    _manual_records,
    _merged_options,
    _option_secret,
    _section_between,
)


class SaraminAdapter(PublicJobsHttpAdapter):
    def __init__(self, manifest: SourceManifest):
        super().__init__(
            _merged_options(
                manifest,
                {
                    "include_url_patterns": [
                        r"/zf_user/jobs/relay/view",
                        r"/zf_user/jobs/view",
                    ],
                    "exclude_url_patterns": [r"login", r"join", r"apply"],
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
                candidate = _candidate_from_saramin_detail(response.url, response.text)
                if candidate is not None:
                    candidates.append(candidate)
                self._sleep()
            return _filter_candidates(candidates[: self.max_pages], self)

        self._validate_access()
        access_key = _option_secret(self.options, "access_key", "access_key_env")
        if not access_key:
            raise SourceAccessError("Saramin official API collection requires access_key or access_key_env.")
        endpoint = str(self.options.get("api_url", "https://oapi.saramin.co.kr/job-search"))
        params = {
            "keywords": str(self.options.get("keywords", "python ai")),
            "count": self.max_pages,
            "start": int(self.options.get("start", 0)),
            "job_mid_cd": str(self.options.get("job_mid_cd", "")),
            "fields": str(self.options.get("fields", "")),
            "format": "json",
        }
        params.update(dict(self.options.get("api_params", {})))
        params = {key: value for key, value in params.items() if value not in ("", None)}
        response = self._get_fetch(
            endpoint,
            params,
            headers={"Access-Key": access_key, "access-key": access_key},
        )
        candidates = [_candidate_from_saramin_job(item) for item in _saramin_jobs(response.text)]
        return _filter_candidates(candidates[: self.max_pages], self)

    def discover_urls(self) -> List[str]:
        urls = super().discover_urls()
        return [_saramin_detail_url(url) for url in urls][: self.max_pages]

    def _discover_urls_from_listing(
        self,
        base_url: str,
        html: str,
        parser: Any,
    ) -> List[str]:
        return _saramin_detail_urls_from_listing(html, self.link_include_keywords)


def _candidate_from_saramin_job(item: Dict[str, Any]) -> PostingCandidate:
    position = item.get("position") if isinstance(item.get("position"), dict) else {}
    company_data = item.get("company") if isinstance(item.get("company"), dict) else {}
    detail = company_data.get("detail") if isinstance(company_data.get("detail"), dict) else {}
    title = str(_dig(position, "title") or item.get("title") or "")
    company = str(_dig(detail, "name") or item.get("company") or "saramin")
    source_url = str(item.get("url") or _dig(position, "url") or "")
    location = str(_dig(position, "location", "name") or item.get("location") or "")
    deadline = _date_prefix(item.get("expiration-date") or item.get("expiration_date"))
    required = _as_text_list(_dig(position, "job-type", "name"))
    required.extend(_as_text_list(_dig(position, "industry", "name")))
    required.extend(_as_text_list(_dig(position, "job-code", "name")))
    experience = _as_text_list(_dig(position, "experience-level", "name"))
    return PostingCandidate(
        source_id="saramin",
        source_url=source_url,
        source_posting_id=str(item.get("id") or item.get("rec_idx") or ""),
        title=title,
        company=company,
        location=location,
        deadline_raw=deadline,
        collected_at=datetime.now(timezone.utc),
        raw_jd={
            "required_qualifications": required,
            "preferred_qualifications": [],
            "responsibilities": [title],
            "company_info": [company],
            "experience_tags": experience,
        },
    )

def _saramin_detail_url(url: str) -> str:
    if "/zf_user/jobs/relay/view-detail" in url:
        return url
    rec_idx = _first_match(url, r"[?&]rec_idx=(\d+)")
    if rec_idx and "/zf_user/jobs/relay/view" in url:
        return f"https://www.saramin.co.kr/zf_user/jobs/relay/view-detail?rec_idx={rec_idx}&rec_seq=0"
    return url


def _saramin_detail_urls_from_listing(html: str, link_include_keywords: List[str]) -> List[str]:
    urls: List[str] = []
    for block in re.findall(r"\{[^{}]*rec_idx[^{}]*\}", html, re.S):
        rec_idx = _first_match(block, r'"rec_idx"\s*:\s*"?(\d+)"?') or _first_match(block, r"'rec_idx'\s*:\s*'?(\d+)'?")
        if not rec_idx:
            continue
        if link_include_keywords and not _contains_any(block, link_include_keywords):
            continue
        urls.append(f"https://www.saramin.co.kr/zf_user/jobs/relay/view-detail?rec_idx={rec_idx}&rec_seq=0")
    return list(dict.fromkeys(urls))


def _candidate_from_saramin_detail(source_url: str, html: str) -> Optional[PostingCandidate]:
    text = _clean_visible_text(html)
    responsibilities = _section_between(text, ["주요업무", "담당업무"], ["자격요건", "지원자격", "우대사항", "마감일 및 근무지", "복지 및 혜택"])
    required = _section_between(text, ["자격요건", "지원자격"], ["우대사항", "마감일 및 근무지", "복지 및 혜택", "채용절차"])
    preferred = _section_between(text, ["우대사항"], ["마감일 및 근무지", "복지 및 혜택", "채용절차"])
    if not any((responsibilities, required, preferred)):
        return None
    title = _saramin_detail_title(text) or "saramin"
    rec_idx = _first_match(source_url, r"rec_idx=(\d+)")
    skills = _saramin_skill_terms(text)
    deadline = _first_match(text, r"마감일\s*[:：]?\s*(?:~\s*)?(\d{4}년\s*\d{2}월\s*\d{2}일|\d{4}[.-]\d{1,2}[.-]\d{1,2}|채용시)")
    location = _first_match(text, r"근무지\s+-\s*(.+?)(?:복지 및 혜택|채용절차|$)")
    return PostingCandidate(
        source_id="saramin",
        source_url=source_url,
        source_posting_id=rec_idx or None,
        title=title,
        company="saramin",
        location=location,
        deadline_raw=deadline or None,
        collected_at=datetime.now(timezone.utc),
        raw_jd={
            "required_qualifications": [required, *skills] if required else skills,
            "preferred_qualifications": [preferred] if preferred else [],
            "responsibilities": [responsibilities] if responsibilities else [],
            "company_info": [],
            "experience_tags": _as_text_list(_first_match(text, r"(신입|경력\s*\d+년\s*이상|경력\s*\d+~\d+년|경력무관)")),
        },
    )

def _saramin_detail_title(text: str) -> str:
    for marker in ("서비스 소개", "모집부문 / 상세내용", "사용 기술", "주요업무"):
        if marker in text:
            return text.split(marker, 1)[0].strip()
    return _first_non_empty_line(text)


def _first_non_empty_line(text: str) -> str:
    for line in text.splitlines():
        line = line.strip()
        if line:
            return line
    return ""


def _saramin_skill_terms(text: str) -> List[str]:
    known = ["Python", "FastAPI", "RAG", "LLM", "Redis", "Elasticsearch", "AWS", "Docker", "PyTorch", "TensorFlow"]
    return [skill for skill in known if skill.lower() in text.lower()]


def _saramin_jobs(text: str) -> List[Dict[str, Any]]:
    payload = json.loads(text)
    jobs = payload.get("jobs") if isinstance(payload, dict) else None
    if isinstance(jobs, dict):
        job = jobs.get("job")
        if isinstance(job, list):
            return [item for item in job if isinstance(item, dict)]
        if isinstance(job, dict):
            return [job]
    if isinstance(payload, dict) and isinstance(payload.get("job"), list):
        return [item for item in payload["job"] if isinstance(item, dict)]
    return []
