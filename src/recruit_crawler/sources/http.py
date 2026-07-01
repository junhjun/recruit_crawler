from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from html import unescape
from html.parser import HTMLParser
from typing import Any, Dict, Iterable, List, Optional
from urllib.parse import urljoin, urlparse
from urllib.parse import urlencode
from urllib.request import HTTPRedirectHandler, Request, build_opener
from urllib.robotparser import RobotFileParser

from ..schemas import PostingCandidate, SourceManifest


DEFAULT_USER_AGENT = "recruit-crawler/0.1 (+local personal recruiting report)"


class SourceAccessError(RuntimeError):
    pass


@dataclass(frozen=True)
class HttpResponse:
    url: str
    text: str


class _SafeRedirectHandler(HTTPRedirectHandler):
    def __init__(self, adapter: "PublicJobsHttpAdapter"):
        self.adapter = adapter

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # type: ignore[no-untyped-def]
        absolute = urljoin(req.full_url, newurl)
        self.adapter._validate_url(absolute)
        return super().redirect_request(req, fp, code, msg, headers, absolute)


class AnchorAndScriptParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.anchors: List[tuple[str, str]] = []
        self.json_ld_scripts: List[str] = []
        self.title_parts: List[str] = []
        self.meta_description = ""
        self._current_href: Optional[str] = None
        self._current_anchor_text: List[str] = []
        self._in_json_ld = False
        self._json_ld_parts: List[str] = []
        self._in_title = False

    def handle_starttag(self, tag: str, attrs: List[tuple[str, Optional[str]]]) -> None:
        attrs_map = {key.lower(): value or "" for key, value in attrs}
        if tag == "a" and attrs_map.get("href"):
            self._current_href = attrs_map["href"]
            self._current_anchor_text = []
        if tag == "script" and attrs_map.get("type", "").lower() == "application/ld+json":
            self._in_json_ld = True
            self._json_ld_parts = []
        if tag == "title":
            self._in_title = True
        if tag == "meta":
            name = attrs_map.get("name", "").lower()
            prop = attrs_map.get("property", "").lower()
            if name == "description" or prop == "og:description":
                self.meta_description = attrs_map.get("content", "")

    def handle_endtag(self, tag: str) -> None:
        if tag == "a" and self._current_href:
            text = " ".join("".join(self._current_anchor_text).split())
            self.anchors.append((self._current_href, unescape(text)))
            self._current_href = None
            self._current_anchor_text = []
        if tag == "script" and self._in_json_ld:
            self.json_ld_scripts.append("".join(self._json_ld_parts))
            self._in_json_ld = False
            self._json_ld_parts = []
        if tag == "title":
            self._in_title = False

    def handle_data(self, data: str) -> None:
        if self._current_href is not None:
            self._current_anchor_text.append(data)
        if self._in_json_ld:
            self._json_ld_parts.append(data)
        if self._in_title:
            self.title_parts.append(data)


class PublicJobsHttpAdapter:
    """Collect public job pages after a source review enables the manifest."""

    def __init__(self, manifest: SourceManifest):
        self.manifest = manifest
        self.options = manifest.options
        self.errors: List[str] = []
        self.user_agent = str(self.options.get("user_agent", DEFAULT_USER_AGENT))
        self.timeout_seconds = float(self.options.get("timeout_seconds", 12))
        self.fetch_retries = int(self.options.get("fetch_retries", 0))
        self.delay_seconds = float(self.options.get("delay_seconds", 1))
        self.max_pages = int(self.options.get("max_pages", 25))
        self.require_robots = bool(self.options.get("require_robots", True))
        self.allowed_domains = set(manifest.domains)
        self.link_include_keywords = _normalized_keywords(self.options.get("link_include_keywords", []))
        self.candidate_include_keywords = _normalized_keywords(
            self.options.get("candidate_include_keywords", [])
        )
        self.candidate_exclude_keywords = _normalized_keywords(
            self.options.get("candidate_exclude_keywords", [])
        )

    def collect(self) -> List[PostingCandidate]:
        self._validate_access()
        urls = self._seed_urls()
        candidates: List[PostingCandidate] = []
        seen_urls = set()
        for url in urls[: self.max_pages]:
            if url in seen_urls:
                continue
            seen_urls.add(url)
            try:
                response = self._fetch(url)
            except Exception as exc:
                if self.manifest.failure_mode == "fail_run":
                    raise
                self.errors.append(f"{url}: {exc}")
                continue
            parser = self._parse_html(response.text)
            page_candidates = self._candidates_from_json_ld(response.url, parser)
            if not page_candidates:
                page_candidates = [self._candidate_from_page(response.url, parser)]
            candidates.extend(page_candidates)
            self._sleep()
        return [
            candidate
            for candidate in candidates
            if candidate.title.strip() and self._candidate_matches_keywords(candidate)
        ]

    def discover_urls(self) -> List[str]:
        self._validate_access()
        discovered: List[str] = []
        for url in self._search_urls():
            response = self._fetch(url)
            parser = self._parse_html(response.text)
            discovered.extend(self._filter_job_links(response.url, parser.anchors))
            self._sleep()
        return _dedupe(discovered)[: self.max_pages]

    def _seed_urls(self) -> List[str]:
        explicit = [str(url) for url in self.options.get("start_urls", [])]
        if explicit:
            return explicit
        return self.discover_urls()

    def _search_urls(self) -> List[str]:
        return [str(url) for url in self.options.get("search_urls", [])]

    def _validate_access(self) -> None:
        if self.manifest.auth_required and not self.options.get("approved_authenticated_flow"):
            raise SourceAccessError(f"{self.manifest.source_id} requires approved authenticated access.")
        if self.manifest.tos_review_status != "pass":
            raise SourceAccessError(f"{self.manifest.source_id} source review has not passed.")
        if not self.manifest.domains:
            raise SourceAccessError(f"{self.manifest.source_id} must declare allowed domains.")
        if (
            not self.require_robots
            and not self.options.get("explicit_automated_permission")
            and not self.options.get("approved_api_access")
        ):
            raise SourceAccessError(
                f"{self.manifest.source_id} requires explicit automated-access permission "
                "when robots.txt checks are disabled."
            )

    def _fetch(self, url: str) -> HttpResponse:
        self._validate_url(url)
        if self.require_robots:
            robots_allowed = self._robots_allows(url)
            if robots_allowed is None:
                raise SourceAccessError(f"robots.txt could not be checked: {url}")
            if not robots_allowed:
                raise SourceAccessError(f"robots.txt does not allow fetching: {url}")
        request = Request(url, headers={"User-Agent": self.user_agent})
        with self._open_request(request) as response:
            charset = response.headers.get_content_charset() or "utf-8"
            body = response.read().decode(charset, errors="replace")
            return HttpResponse(url=response.geturl(), text=body)

    def _post_fetch(self, url: str, data: Dict[str, Any]) -> HttpResponse:
        self._validate_url(url)
        if self.require_robots:
            robots_allowed = self._robots_allows(url)
            if robots_allowed is None:
                raise SourceAccessError(f"robots.txt could not be checked: {url}")
            if not robots_allowed:
                raise SourceAccessError(f"robots.txt does not allow fetching: {url}")
        encoded = urlencode(data, doseq=True).encode("utf-8")
        request = Request(
            url,
            data=encoded,
            headers={
                "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
                "User-Agent": self.user_agent,
            },
        )
        with self._open_request(request) as response:
            charset = response.headers.get_content_charset() or "utf-8"
            body = response.read().decode(charset, errors="replace")
            return HttpResponse(url=response.geturl(), text=body)

    def _get_fetch(
        self,
        url: str,
        params: Dict[str, Any],
        *,
        headers: Optional[Dict[str, str]] = None,
    ) -> HttpResponse:
        self._validate_url(url)
        if self.require_robots:
            robots_allowed = self._robots_allows(url)
            if robots_allowed is None:
                raise SourceAccessError(f"robots.txt could not be checked: {url}")
            if not robots_allowed:
                raise SourceAccessError(f"robots.txt does not allow fetching: {url}")
        query = urlencode(params, doseq=True)
        separator = "&" if "?" in url else "?"
        request_url = f"{url}{separator}{query}" if query else url
        request_headers = {"User-Agent": self.user_agent}
        request_headers.update(headers or {})
        request = Request(request_url, headers=request_headers)
        with self._open_request(request) as response:
            charset = response.headers.get_content_charset() or "utf-8"
            body = response.read().decode(charset, errors="replace")
            return HttpResponse(url=response.geturl(), text=body)

    def _open_request(self, request: Request):
        opener = build_opener(_SafeRedirectHandler(self))
        last_error: Optional[Exception] = None
        for _attempt in range(self.fetch_retries + 1):
            try:
                return opener.open(request, timeout=self.timeout_seconds)
            except Exception as exc:
                last_error = exc
        assert last_error is not None
        raise last_error

    def _validate_url(self, url: str) -> None:
        parsed = urlparse(url)
        if parsed.scheme not in {"http", "https"}:
            raise SourceAccessError(f"unsupported URL scheme: {url}")
        if parsed.netloc not in self.allowed_domains:
            raise SourceAccessError(f"URL domain is not allowlisted: {url}")

    def _robots_allows(self, url: str) -> Optional[bool]:
        parsed = urlparse(url)
        robots_url = f"{parsed.scheme}://{parsed.netloc}/robots.txt"
        parser = RobotFileParser()
        try:
            request = Request(robots_url, headers={"User-Agent": self.user_agent})
            with self._open_request(request) as response:
                charset = response.headers.get_content_charset() or "utf-8"
                body = response.read().decode(charset, errors="replace")
            parser.parse(body.splitlines())
        except Exception:
            return None
        return parser.can_fetch(self.user_agent, url)

    def _parse_html(self, html: str) -> AnchorAndScriptParser:
        parser = AnchorAndScriptParser()
        parser.feed(html)
        return parser

    def _filter_job_links(self, base_url: str, anchors: Iterable[tuple[str, str]]) -> List[str]:
        include_patterns = [
            re.compile(pattern)
            for pattern in self.options.get("include_url_patterns", [r"job", r"recruit", r"career"])
        ]
        exclude_patterns = [
            re.compile(pattern)
            for pattern in self.options.get("exclude_url_patterns", [r"login", r"signup"])
        ]
        urls = []
        for href, text in anchors:
            absolute = urljoin(base_url, href)
            parsed = urlparse(absolute)
            if parsed.netloc not in self.allowed_domains:
                continue
            if any(pattern.search(absolute) for pattern in exclude_patterns):
                continue
            link_text = f"{absolute} {text}"
            if self.link_include_keywords and not _contains_any(link_text, self.link_include_keywords):
                continue
            if any(pattern.search(absolute) for pattern in include_patterns):
                urls.append(absolute)
        return _dedupe(urls)

    def _candidates_from_json_ld(
        self,
        source_url: str,
        parser: AnchorAndScriptParser,
    ) -> List[PostingCandidate]:
        candidates = []
        for script in parser.json_ld_scripts:
            for item in _json_ld_items(script):
                if item.get("@type") != "JobPosting":
                    continue
                candidates.append(self._candidate_from_job_posting(source_url, item))
        return candidates

    def _candidate_from_job_posting(
        self,
        source_url: str,
        item: Dict[str, Any],
    ) -> PostingCandidate:
        org = item.get("hiringOrganization") or {}
        location = _location_text(item.get("jobLocation"))
        description = _clean_html(str(item.get("description", "")))
        return PostingCandidate(
            source_id=self.manifest.source_id,
            source_url=str(item.get("url") or source_url),
            source_posting_id=str(item.get("identifier") or item.get("directApply") or ""),
            title=str(item.get("title") or ""),
            company=str(org.get("name") or self.manifest.source_id),
            location=location,
            deadline_raw=_date_prefix(item.get("validThrough")),
            collected_at=datetime.now(timezone.utc),
            raw_jd={
                "required_qualifications": _string_list(item.get("qualifications")),
                "preferred_qualifications": _string_list(item.get("experienceRequirements")),
                "responsibilities": _string_list(item.get("responsibilities")) or [description],
                "company_info": _string_list(org.get("description")) or [description[:240]],
            },
        )

    def _candidate_from_page(
        self,
        source_url: str,
        parser: AnchorAndScriptParser,
    ) -> PostingCandidate:
        title = " ".join("".join(parser.title_parts).split())
        description = " ".join(parser.meta_description.split())
        return PostingCandidate(
            source_id=self.manifest.source_id,
            source_url=source_url,
            source_posting_id=None,
            title=title or source_url,
            company=str(self.options.get("default_company", self.manifest.source_id)),
            location=str(self.options.get("default_location", "")),
            deadline_raw=None,
            collected_at=datetime.now(timezone.utc),
            raw_jd={
                "responsibilities": [description] if description else [],
                "company_info": [description] if description else [],
            },
        )

    def _candidate_matches_keywords(self, candidate: PostingCandidate) -> bool:
        searchable = " ".join(
            [
                candidate.title,
                candidate.company,
                candidate.location,
                " ".join(_flatten_values(candidate.raw_jd.values())),
            ]
        )
        if self.candidate_exclude_keywords and _contains_any(searchable, self.candidate_exclude_keywords):
            return False
        if self.candidate_include_keywords and not _contains_any(searchable, self.candidate_include_keywords):
            return False
        return True

    def _sleep(self) -> None:
        if self.delay_seconds > 0:
            time.sleep(self.delay_seconds)


def _dedupe(values: Iterable[str]) -> List[str]:
    seen = set()
    output = []
    for value in values:
        if value not in seen:
            seen.add(value)
            output.append(value)
    return output


def _normalized_keywords(values: Any) -> List[str]:
    if not isinstance(values, list):
        return []
    return [str(value).lower() for value in values if str(value).strip()]


def _contains_any(text: str, keywords: List[str]) -> bool:
    normalized = text.lower()
    return any(keyword in normalized for keyword in keywords)


def _flatten_values(values: Iterable[Any]) -> List[str]:
    flattened: List[str] = []
    for value in values:
        if isinstance(value, list):
            flattened.extend(str(item) for item in value)
        elif isinstance(value, dict):
            flattened.extend(_flatten_values(value.values()))
        elif value is not None:
            flattened.append(str(value))
    return flattened


def _json_ld_items(script: str) -> List[Dict[str, Any]]:
    try:
        parsed = json.loads(script)
    except json.JSONDecodeError:
        return []
    if isinstance(parsed, dict) and isinstance(parsed.get("@graph"), list):
        return [item for item in parsed["@graph"] if isinstance(item, dict)]
    if isinstance(parsed, dict):
        return [parsed]
    if isinstance(parsed, list):
        return [item for item in parsed if isinstance(item, dict)]
    return []


def _clean_html(value: str) -> str:
    without_tags = re.sub(r"<[^>]+>", " ", value)
    return " ".join(unescape(without_tags).split())


def _string_list(value: Any) -> List[str]:
    if value is None:
        return []
    if isinstance(value, list):
        return [_clean_html(str(item)) for item in value if _clean_html(str(item))]
    cleaned = _clean_html(str(value))
    return [cleaned] if cleaned else []


def _date_prefix(value: Any) -> Optional[str]:
    if not value:
        return None
    text = str(value)
    match = re.match(r"\d{4}-\d{2}-\d{2}", text)
    return match.group(0) if match else text


def _location_text(value: Any) -> str:
    if isinstance(value, list):
        return ", ".join(_location_text(item) for item in value if _location_text(item))
    if not isinstance(value, dict):
        return str(value or "")
    address = value.get("address") or {}
    if isinstance(address, dict):
        parts = [
            address.get("streetAddress"),
            address.get("addressLocality"),
            address.get("addressRegion"),
            address.get("addressCountry"),
        ]
        return ", ".join(str(part) for part in parts if part)
    return str(address)
