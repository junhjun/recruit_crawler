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
from urllib.parse import parse_qs, parse_qsl, urljoin, urlparse


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

    def _validate_access(self) -> None:
        try:
            super()._validate_access()
        except SourceAccessError as exc:
            raise SourceAccessError(str(exc).replace("saramin", "사람인")) from exc

    def collect(self) -> List[PostingCandidate]:
        if _has_manual_records(self.options):
            return _filter_candidates(
                [_candidate_from_manual_record(self.manifest.source_id, record) for record in _manual_records(self.options)],
                self,
            )
        if self.manifest.access_mode not in {"public_page", "feed"}:
            raise SourceAccessError("사람인 acquisition requires public page access.")
        strategy = str(self.options.get("acquisition_strategy", "")).strip()
        approval = str(self.options.get("outer_strategy_approval", "")).strip()
        if strategy == "detail_only" and approval == "not_probed":
            return self._collect_detail_only()
        if strategy == "outer_only" and approval == "approved":
            return self._collect_outer_only()
        raise SourceAccessError(
            "사람인 acquisition strategy is invalid; use detail_only/not_probed or outer_only/approved."
        )

    def _seed_strategy_urls(self, strategy: str) -> List[str]:
        if self.options.get("search_urls") or self.options.get("start_urls"):
            urls = self._seed_urls()
        else:
            urls = [str(value) for value in self.options.get("detail_urls", [])]
        canonical: List[str] = []
        seen = set()
        for url in urls:
            try:
                normalized = (
                    _saramin_detail_url(url)
                    if strategy == "detail_only"
                    else _saramin_outer_url(url)
                )
            except SourceAccessError as exc:
                if self.manifest.failure_mode == "fail_run":
                    raise
                self.errors.append(f"{url}: {exc}")
                continue
            if normalized not in seen:
                seen.add(normalized)
                canonical.append(normalized)
        return canonical

    def _collect_detail_only(self) -> List[PostingCandidate]:
        self._validate_access()
        urls = self._seed_strategy_urls("detail_only")
        candidates: List[PostingCandidate] = []
        for url in urls[: self.max_pages]:
            try:
                response = self._fetch(url)
                _require_saramin_endpoint(response.url, url)
            except (HTTPException, OSError, SourceAccessError) as exc:
                if self.manifest.failure_mode == "fail_run":
                    raise
                self.errors.append(f"{url}: {exc}")
                continue
            candidate = _candidate_from_saramin_detail(url, response.text)
            if candidate is not None:
                candidates.append(candidate)
            self._sleep()
        return _filter_candidates(candidates[: self.max_pages], self)

    def _collect_outer_only(self) -> List[PostingCandidate]:
        self._validate_access()
        urls = self._seed_strategy_urls("outer_only")
        candidates: List[PostingCandidate] = []
        for url in urls[: self.max_pages]:
            try:
                response = self._fetch(url)
                _require_saramin_endpoint(response.url, url)
            except (HTTPException, OSError, SourceAccessError) as exc:
                if self.manifest.failure_mode == "fail_run":
                    raise
                self.errors.append(f"{url}: {exc}")
                continue
            candidate = _candidate_from_saramin_outer(url, response.text)
            if candidate is not None:
                candidates.append(candidate)
            self._sleep()
        return _filter_candidates(candidates[: self.max_pages], self)

    def discover_urls(self) -> List[str]:
        urls = super().discover_urls()
        normalize = (
            _saramin_listing_outer_url
            if str(self.options.get("acquisition_strategy", "")).strip() == "outer_only"
            else _saramin_listing_detail_url
        )
        canonical = []
        for url in urls:
            try:
                candidate = normalize(url)
            except SourceAccessError as exc:
                self.errors.append(f"{url}: {exc}")
                continue
            if candidate not in canonical:
                canonical.append(candidate)
        return canonical[: self.max_pages]

    def _discover_urls_from_listing(
        self,
        base_url: str,
        html: str,
        parser: Any,
    ) -> List[str]:
        return _saramin_detail_urls_from_listing(html, self.link_include_keywords)


_SARAMIN_HOSTNAME = "www.saramin.co.kr"
_SARAMIN_CANONICAL_HOST = f"https://{_SARAMIN_HOSTNAME}"
_SARAMIN_DETAIL_PATH = "/zf_user/jobs/relay/view-detail"
_SARAMIN_OUTER_PATH = "/zf_user/jobs/relay/view"
_SARAMIN_UNKNOWN_COMPANY = "회사명 확인 필요"
_SARAMIN_PLACEHOLDER_COMPANIES = frozenset(
    {"", "empty", "회사명 확인 필요", "saramin", "사람인", "사람인 채용"}
)


def _require_saramin_endpoint(response_url: str, expected_url: str) -> None:
    if response_url != expected_url:
        raise SourceAccessError("사람인 response endpoint mismatch.")
def _candidate_from_saramin_job(item: Dict[str, Any]) -> PostingCandidate:
    position = item.get("position") if isinstance(item.get("position"), dict) else {}
    company_data = item.get("company") if isinstance(item.get("company"), dict) else {}
    detail = company_data.get("detail") if isinstance(company_data.get("detail"), dict) else {}
    title = str(_dig(position, "title") or item.get("title") or "")
    company_value = _dig(detail, "name") or (item.get("company") if isinstance(item.get("company"), str) else "")
    company = str(company_value or _SARAMIN_UNKNOWN_COMPANY)
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


def _saramin_rec_idx(url: str, *, allow_listing_query: bool = False) -> tuple[str, str]:
    if not isinstance(url, str) or not url or any(char.isspace() for char in url):
        raise SourceAccessError("사람인 URL is malformed.")
    parsed = urlparse(url)
    try:
        hostname = parsed.hostname
        port = parsed.port
    except ValueError as exc:
        raise SourceAccessError("사람인 URL is malformed.") from exc
    if (
        parsed.scheme != "https"
        or hostname is None
        or hostname.casefold() != _SARAMIN_HOSTNAME
        or port is not None
        or parsed.username is not None
        or parsed.password is not None
        or parsed.fragment
        or parsed.params
        or parsed.path not in {_SARAMIN_DETAIL_PATH, _SARAMIN_OUTER_PATH}
    ):
        raise SourceAccessError("사람인 URL is not a trusted job URL.")
    if allow_listing_query:
        query_items = parse_qsl(parsed.query, keep_blank_values=True)
        rec_idx_values = [value for key, value in query_items if key == "rec_idx"]
        rec_seq_values = [value for key, value in query_items if key == "rec_seq"]
        if (
            len(rec_idx_values) != 1
            or not re.fullmatch(r"[1-9]\d*", rec_idx_values[0])
            or any(value != "0" for value in rec_seq_values)
        ):
            raise SourceAccessError("사람인 URL has an ambiguous or malformed rec_idx.")
        return parsed.path, rec_idx_values[0]
    if not re.fullmatch(
        r"(?:rec_idx=[1-9]\d*(?:&rec_seq=0)?|rec_seq=0&rec_idx=[1-9]\d*)",
        parsed.query,
    ):
        raise SourceAccessError("사람인 URL has an ambiguous or malformed rec_idx.")
    query = parse_qs(parsed.query, keep_blank_values=True)
    rec_idx_values = query.get("rec_idx", [])
    if len(rec_idx_values) != 1 or not re.fullmatch(r"[1-9]\d*", rec_idx_values[0]):
        raise SourceAccessError("사람인 URL has an ambiguous or malformed rec_idx.")
    return parsed.path, rec_idx_values[0]


def _saramin_detail_url(url: str) -> str:
    _path, rec_idx = _saramin_rec_idx(url)
    return f"{_SARAMIN_CANONICAL_HOST}{_SARAMIN_DETAIL_PATH}?rec_idx={rec_idx}&rec_seq=0"


def _saramin_outer_url(url: str) -> str:
    _path, rec_idx = _saramin_rec_idx(url)
    return f"{_SARAMIN_CANONICAL_HOST}{_SARAMIN_OUTER_PATH}?rec_idx={rec_idx}&rec_seq=0"
def _saramin_listing_detail_url(url: str) -> str:
    _path, rec_idx = _saramin_rec_idx(url, allow_listing_query=True)
    return f"{_SARAMIN_CANONICAL_HOST}{_SARAMIN_DETAIL_PATH}?rec_idx={rec_idx}&rec_seq=0"


def _saramin_listing_outer_url(url: str) -> str:
    _path, rec_idx = _saramin_rec_idx(url, allow_listing_query=True)
    return f"{_SARAMIN_CANONICAL_HOST}{_SARAMIN_OUTER_PATH}?rec_idx={rec_idx}&rec_seq=0"



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
    canonical_url = _saramin_detail_url(source_url)
    title = _saramin_detail_title(text, html) or "채용 공고"
    rec_idx = _first_match(canonical_url, r"[?&]rec_idx=(\d+)")
    company = _saramin_detail_company(html)
    skills = _saramin_skill_terms(text)
    deadline = _first_match(text, r"마감일\s*[:：]?\s*(?:~\s*)?(\d{4}년\s*\d{2}월\s*\d{2}일|\d{4}[.-]\d{1,2}[.-]\d{1,2}|채용시)")
    location = _first_match(text, r"근무지\s+-\s*(.+?)(?:복지 및 혜택|채용절차|$)")
    return PostingCandidate(
        source_id="saramin",
        source_url=canonical_url,
        source_posting_id=rec_idx or None,
        title=title,
        company=company,
        location=location,
        deadline_raw=deadline or None,
        collected_at=datetime.now(timezone.utc),
        raw_jd={
            "required_qualifications": [required, *skills] if required else skills,
            "preferred_qualifications": [preferred] if preferred else [],
            "responsibilities": [responsibilities] if responsibilities else [],
            "company_info": [company] if company != _SARAMIN_UNKNOWN_COMPANY else [],
            "experience_tags": _as_text_list(_first_match(text, r"(신입|경력\s*\d+년\s*이상|경력\s*\d+~\d+년|경력무관)")),
        },
    )
def _candidate_from_saramin_outer(source_url: str, html: str) -> Optional[PostingCandidate]:
    canonical_url = _saramin_outer_url(source_url)
    text = _clean_visible_text(html)
    responsibilities = _section_between(
        text,
        ["주요업무", "담당업무"],
        ["자격요건", "지원자격", "우대사항", "마감일 및 근무지", "복지 및 혜택"],
    )
    required = _section_between(
        text,
        ["자격요건", "지원자격"],
        ["우대사항", "마감일 및 근무지", "복지 및 혜택", "채용절차"],
    )
    preferred = _section_between(
        text,
        ["우대사항"],
        ["마감일 및 근무지", "복지 및 혜택", "채용절차"],
    )
    if not all((responsibilities, required, preferred)):
        return None
    detail_candidate = _candidate_from_saramin_detail(_saramin_detail_url(canonical_url), html)
    if detail_candidate is None:
        return None
    if (
        not detail_candidate.title.strip()
        or detail_candidate.title == "채용 공고"
        or detail_candidate.company.strip().casefold() in _SARAMIN_PLACEHOLDER_COMPANIES
    ):
        return None
    return replace(detail_candidate, source_url=canonical_url)

def _saramin_header_titles(html: str) -> List[str]:
    values = []
    for value in re.findall(
        r"<p\b[^>]*class\s*=\s*['\"][^'\"]*\bjob-header__title\b[^'\"]*['\"][^>]*>(.*?)</p>",
        html,
        flags=re.I | re.S,
    ):
        clean = _bounded_saramin_text(re.sub(r"<[^>]+>", " ", value))
        if clean:
            values.append(clean)
    return values


def _saramin_detail_title(text: str, html: str = "") -> str:
    for value in (
        *_saramin_header_titles(html),
        _saramin_meta_content(html, "og:title"),
        _saramin_json_ld_title(html),
    ):
        if value:
            return _bounded_saramin_text(value)
    for marker in ("서비스 소개", "모집부문 / 상세내용", "사용 기술", "주요업무"):
        if marker in text:
            return _bounded_saramin_text(text.split(marker, 1)[0])
    return _bounded_saramin_text(_first_non_empty_line(text))


_SARAMIN_TITLE_LIMIT = 120


def _clean_extracted_text(value: object) -> str:
    return " ".join(unescape(str(value)).split())


def _bounded_saramin_text(value: str, limit: int = _SARAMIN_TITLE_LIMIT) -> str:
    return _clean_extracted_text(value)[:limit].rstrip()


def _saramin_meta_content(html: str, key: str) -> str:
    for tag in re.findall(r"<meta\b[^>]*>", html, flags=re.I):
        match = re.search(r"\b(?:property|name)\s*=\s*['\"]([^'\"]+)['\"]", tag, flags=re.I)
        if match and match.group(1).strip().lower() == key.lower():
            content = re.search(r"\bcontent\s*=\s*['\"]([^'\"]*)['\"]", tag, flags=re.I)
            if content:
                return unescape(content.group(1)).strip()
    return ""


def _saramin_json_ld_values(html: str) -> List[Any]:
    values: List[Any] = []
    for script in re.findall(
        r"<script\b[^>]*type\s*=\s*['\"]application/ld\+json['\"][^>]*>(.*?)</script>",
        html,
        flags=re.I | re.S,
    ):
        try:
            values.append(json.loads(unescape(script)))
        except (TypeError, ValueError):
            continue
    return values


def _saramin_json_ld_title(html: str) -> str:
    for payload in _saramin_json_ld_values(html):
        stack = [payload]
        while stack:
            value = stack.pop(0)
            if isinstance(value, dict):
                title = value.get("title")
                if isinstance(title, str) and title.strip():
                    return title
                stack.extend(value.values())
            elif isinstance(value, list):
                stack.extend(value)
    return ""


def _saramin_company_value(value: Any) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, dict):
        name = value.get("name")
        return name if isinstance(name, str) else ""
    return ""


def _is_saramin_source_name(value: str) -> bool:
    return value.strip().casefold() in {"saramin", "사람인", "사람인 채용"}


def _saramin_detail_company(html: str) -> str:
    for title in _saramin_header_titles(html):
        match = re.match(r"^\[([^\]]{1,80})\]", title)
        if match:
            company = _bounded_saramin_text(match.group(1))
            if company and not _is_saramin_source_name(company):
                return company
    for payload in _saramin_json_ld_values(html):
        stack = [payload]
        while stack:
            value = stack.pop(0)
            if isinstance(value, dict):
                for key in ("hiringOrganization", "employer", "organization", "company"):
                    company = _bounded_saramin_text(_saramin_company_value(value.get(key)))
                    if company and not _is_saramin_source_name(company):
                        return company
                for key in ("companyName", "company_name"):
                    company = _bounded_saramin_text(str(value.get(key) or ""))
                    if company and not _is_saramin_source_name(company):
                        return company
                stack.extend(value.values())
            elif isinstance(value, list):
                stack.extend(value)
    for key in ("og:company", "company", "company_name", "author", "og:site_name"):
        company = _bounded_saramin_text(_saramin_meta_content(html, key))
        if company and not _is_saramin_source_name(company):
            return company
    description = _saramin_meta_content(html, "og:description")
    if " - " in description:
        company = _bounded_saramin_text(description.split(" - ", 1)[0])
        if company and not _is_saramin_source_name(company):
            return company
    return _SARAMIN_UNKNOWN_COMPANY



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
