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
                except Exception as exc:
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
        except Exception as exc:
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
        if isinstance(position_id, bool) or not str(position_id).isdigit():
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
                except Exception as exc:
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
            except Exception as exc:
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
            except Exception as exc:
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


class RocketPunchBrowserAutomationAdapter(PublicJobsHttpAdapter):
    def __init__(self, manifest: SourceManifest):
        super().__init__(
            _merged_options(
                manifest,
                {
                    "search_urls": ["https://www.rocketpunch.com/en/jobs"],
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
                        "백엔드",
                        "backend",
                    ],
                    "candidate_exclude_keywords": [
                        "designer",
                        "design",
                        "marketing",
                        "sales",
                        "영업",
                        "마케팅",
                        "디자인",
                    ],
                    "max_pages": 20,
                    "delay_seconds": 0,
                    "browser_timeout_seconds": 45,
                },
            )
        )

    def _validate_access(self) -> None:
        if self.manifest.auth_required:
            raise SourceAccessError("RocketPunch browser automation must not require authentication.")
        if self.manifest.access_mode != "browser_automation":
            raise SourceAccessError("RocketPunch requires browser_automation access_mode.")
        if not self.manifest.domains:
            raise SourceAccessError("rocketpunch must declare allowed domains.")
        if self.options.get("policy_override_mode") != "user_directed_ignore":
            raise SourceAccessError("RocketPunch browser automation requires user_directed_ignore policy override.")
        if not str(self.options.get("policy_override_reason", "")).strip():
            raise SourceAccessError("RocketPunch browser automation requires policy_override_reason.")
        if self.options.get("policy_override_acknowledges_source_notice") is not True:
            raise SourceAccessError("RocketPunch browser automation requires source notice acknowledgement.")

    def collect(self) -> List[PostingCandidate]:
        self._validate_access()
        candidates: List[PostingCandidate] = []
        for url in self._search_urls():
            self._validate_url(url)
            try:
                html = self._dump_dom(url)
            except Exception as exc:
                if self.manifest.failure_mode == "fail_run":
                    raise
                self.errors.append(f"{url}: {exc}")
                continue
            candidates.extend(self._candidates_from_listing_dom(url, html))
            self._sleep()
            if len(candidates) >= self.max_pages:
                break
        filtered = _filter_candidates(candidates[: self.max_pages], self)
        return self._enrich_detail_candidates(filtered)

    def _dump_dom(self, url: str) -> str:
        fixture_path = self.options.get("browser_capture_fixture_path")
        if fixture_path:
            return Path(str(fixture_path)).read_text(encoding="utf-8")
        browser_binary = self._browser_binary()
        timeout = float(self.options.get("browser_timeout_seconds", 45))
        completed = subprocess.run(
            [
                browser_binary,
                "--headless=new",
                "--disable-gpu",
                "--no-first-run",
                "--no-default-browser-check",
                "--disable-extensions",
                "--disable-background-networking",
                f"--virtual-time-budget={int(float(self.options.get('browser_virtual_time_budget_ms', 15000)))}",
                "--dump-dom",
                url,
            ],
            check=False,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        if completed.returncode != 0:
            raise SourceAccessError(completed.stderr.strip() or f"browser exited with {completed.returncode}")
        return completed.stdout

    def _browser_binary(self) -> str:
        configured = str(self.options.get("browser_binary", "")).strip()
        candidates = [
            configured,
            os.environ.get("ROCKETPUNCH_BROWSER_BINARY", ""),
            "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
            "/Applications/Chromium.app/Contents/MacOS/Chromium",
            shutil.which("google-chrome") or "",
            shutil.which("chromium") or "",
            shutil.which("chromium-browser") or "",
        ]
        for candidate in candidates:
            if candidate and Path(candidate).exists():
                return candidate
        raise SourceAccessError("Chrome/Chromium binary not found for RocketPunch browser automation.")

    def _candidates_from_listing_dom(self, listing_url: str, html: str) -> List[PostingCandidate]:
        blocks = _rocketpunch_listing_blocks(html)
        candidates = [
            _candidate_from_rocketpunch_card(self.manifest.source_id, listing_url, block, index)
            for index, block in enumerate(blocks, start=1)
        ]
        return [candidate for candidate in candidates if candidate.title.strip()]

    def _enrich_detail_candidates(self, candidates: List[PostingCandidate]) -> List[PostingCandidate]:
        if not bool(self.options.get("fetch_detail_pages", True)):
            return candidates
        enriched: List[PostingCandidate] = []
        max_detail_pages = int(self.options.get("max_detail_pages", len(candidates)))
        for index, candidate in enumerate(candidates):
            if index >= max_detail_pages:
                enriched.append(candidate)
                continue
            try:
                html = self._dump_dom(candidate.source_url)
            except Exception as exc:
                if self.manifest.failure_mode == "fail_run":
                    raise
                self.errors.append(f"{candidate.source_url}: {exc}")
                enriched.append(candidate)
                continue
            enriched.append(_merge_rocketpunch_detail(candidate, html))
            self._sleep()
        return enriched



class LinkedInAdapter(PublicJobsHttpAdapter):
    def __init__(self, manifest: SourceManifest):
        super().__init__(
            _merged_options(
                manifest,
                {
                    "include_url_patterns": [r"/jobs/view/"],
                    "exclude_url_patterns": [r"/login", r"/signup"],
                    "max_pages": 20,
                    "delay_seconds": 1,
                    "require_robots": True,
                    "approved_partner_access": False,
                },
            )
        )

    def _validate_access(self) -> None:
        super()._validate_access()
        if not self.options.get("approved_partner_access"):
            raise SourceAccessError("LinkedIn requires approved partner/API access.")
        if not self.options.get("approved_authenticated_flow"):
            raise SourceAccessError("LinkedIn requires an approved authenticated/API flow.")

    def collect(self) -> List[PostingCandidate]:
        if _has_manual_records(self.options):
            return _filter_candidates(
                [_candidate_from_manual_record(self.manifest.source_id, record) for record in _manual_records(self.options)],
                self,
            )
        partner_payload = self.options.get("partner_payload_path") or self.options.get("api_response_path")
        if partner_payload:
            self._validate_access()
            records = _records_from_file(Path(str(partner_payload)))
            return _filter_candidates(
                [_candidate_from_manual_record(self.manifest.source_id, record) for record in records],
                self,
            )
        self._validate_access()
        raise SourceAccessError(
            "LinkedIn live API fetching is not implemented without a concrete approved partner API payload. "
            "Configure partner_payload_path/api_response_path or manual_export_path."
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


def _rocketpunch_listing_blocks(html: str) -> List[str]:
    blocks: List[str] = []
    marker_pattern = re.compile(
        r'<(?:article|li|div)\b(?:[^>]*class="[^"]*(?:listing-card|job-card|job-item|company-list)[^"]*"[^>]*|[^>]*data-index="\d+"[^>]*)>',
        re.I,
    )
    matches = list(marker_pattern.finditer(html))
    for index, match in enumerate(matches):
        end = matches[index + 1].start() if index + 1 < len(matches) else len(html)
        block = html[match.start() : end]
        text = _clean_visible_text(block)
        if _rocketpunch_card_title(block, text):
            blocks.append(block)
    if blocks:
        return blocks
    return [
        f"<div>{line}</div>"
        for line in _clean_visible_text(html).split(" Product Manager ")
        if _looks_like_rocketpunch_card_text(line)
    ]


def _candidate_from_rocketpunch_card(
    source_id: str,
    listing_url: str,
    block: str,
    index: int,
) -> PostingCandidate:
    text = _clean_visible_text(block)
    title = _rocketpunch_card_title(block, text)
    company = _rocketpunch_card_company(block, text, title)
    snippet = _rocketpunch_card_snippet(text, title, company)
    location = _rocketpunch_card_location(text)
    skills = _rocketpunch_skill_terms(text)
    posting_id = _rocketpunch_posting_id(block) or _rocketpunch_synthetic_id(index, company, title)
    source_url = _rocketpunch_source_url(listing_url, posting_id)
    return PostingCandidate(
        source_id=source_id,
        source_url=source_url,
        source_posting_id=posting_id,
        title=title,
        company=company or "rocketpunch",
        location=location,
        deadline_raw=None,
        collected_at=datetime.now(timezone.utc),
        raw_jd={
            "required_qualifications": skills or [snippet],
            "preferred_qualifications": [],
            "responsibilities": [snippet or title],
            "company_info": [company] if company else [],
            "experience_tags": _rocketpunch_experience_tags(text),
        },
    )


def _merge_rocketpunch_detail(candidate: PostingCandidate, html: str) -> PostingCandidate:
    text = _clean_visible_text(html)
    detail = _rocketpunch_detail_text(text, candidate.title)
    if not detail:
        return candidate
    raw_jd = dict(candidate.raw_jd)
    responsibilities = _section_between(detail, ["Responsibilities"], ["Qualifications", "Preferred Qualifications", "Benefits", "Process", "Notes"])
    qualifications = _section_between(detail, ["Qualifications"], ["Preferred Qualifications", "Benefits", "Process", "Notes"])
    preferred = _section_between(detail, ["Preferred Qualifications"], ["Benefits", "Process", "Notes"])
    team_info = _section_between(detail, ["Team Introduction"], ["Responsibilities", "Qualifications", "Preferred Qualifications"])
    if responsibilities:
        raw_jd["responsibilities"] = [_clean_extracted_text(responsibilities)]
    if qualifications:
        raw_jd["required_qualifications"] = [_clean_extracted_text(qualifications), *_rocketpunch_skill_terms(qualifications)]
    if preferred:
        raw_jd["preferred_qualifications"] = [_clean_extracted_text(preferred)]
    if team_info:
        raw_jd["company_info"] = [_clean_extracted_text(team_info)]
    experience_tags = _rocketpunch_experience_tags(detail)
    if experience_tags:
        raw_jd["experience_tags"] = experience_tags
    deadline = _rocketpunch_deadline(detail)
    return PostingCandidate(
        source_id=candidate.source_id,
        source_url=candidate.source_url,
        source_posting_id=candidate.source_posting_id,
        title=candidate.title,
        company=candidate.company,
        location=candidate.location,
        deadline_raw=deadline or candidate.deadline_raw,
        collected_at=candidate.collected_at,
        raw_jd=raw_jd,
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
def _section_between(text: str, starts: List[str], ends: List[str]) -> str:
    start_positions = [(text.find(start), start) for start in starts if text.find(start) >= 0]
    if not start_positions:
        return ""
    start_index, start_token = min(start_positions, key=lambda item: item[0])
    content_start = start_index + len(start_token)
    end_positions = [text.find(end, content_start) for end in ends if text.find(end, content_start) >= 0]
    content_end = min(end_positions) if end_positions else len(text)
    return text[content_start:content_end].strip()


PLATFORM_ADAPTERS = {
    "company_careers": CompanyCareersAdapter,
    "jumpit": JumpitAdapter,
    "rallit": RallitAdapter,
    "saramin": SaraminAdapter,
    "jobkorea": JobKoreaAdapter,
    "wanted": WantedAdapter,
    "rocketpunch": RocketPunchBrowserAutomationAdapter,
    "linkedin": LinkedInAdapter,
}


def known_platform_ids() -> List[str]:
    return sorted(PLATFORM_ADAPTERS)
