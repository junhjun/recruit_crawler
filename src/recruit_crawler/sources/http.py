from __future__ import annotations

import time
from dataclasses import dataclass
from http.client import HTTPException
from typing import Any, Dict, List, Optional
from urllib.parse import urljoin, urlparse
from urllib.parse import urlencode
from urllib.request import HTTPRedirectHandler, Request, build_opener
from urllib.robotparser import RobotFileParser

from ..schemas import PostingCandidate, SourceManifest
from .http_html import (
    AnchorAndScriptParser,
    HttpHtmlParsingMixin,
    _contains_any,
    _date_prefix,
    _dedupe,
    _flatten_values,
    _normalized_keywords,
)


DEFAULT_USER_AGENT = "recruit-crawler/0.1 (+local personal recruiting report)"


class SourceAccessError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class HttpResponse:
    url: str
    text: str


class _SafeRedirectHandler(HTTPRedirectHandler):
    def __init__(self, adapter: "PublicJobsHttpAdapter"):
        self.adapter = adapter

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        absolute = urljoin(req.full_url, newurl)
        self.adapter._validate_url(absolute)
        return super().redirect_request(req, fp, code, msg, headers, absolute)


class PublicJobsHttpAdapter(HttpHtmlParsingMixin):
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
            except (HTTPException, OSError, SourceAccessError) as exc:
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
            discovered.extend(self._discover_urls_from_listing(response.url, response.text, parser))
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
        last_error: Optional[HTTPException | OSError | SourceAccessError] = None
        for _attempt in range(self.fetch_retries + 1):
            try:
                return opener.open(request, timeout=self.timeout_seconds)
            except (HTTPException, OSError, SourceAccessError) as exc:
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
        except (HTTPException, OSError, SourceAccessError):
            return None
        return parser.can_fetch(self.user_agent, url)

    def _discover_urls_from_listing(
        self,
        base_url: str,
        html: str,
        parser: AnchorAndScriptParser,
    ) -> List[str]:
        return []

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
