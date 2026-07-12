from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from html import unescape
from html.parser import HTMLParser
from typing import Any, Dict, Iterable, List, Optional
from urllib.parse import urljoin, urlparse

from ..schemas import PostingCandidate


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


class HttpHtmlParsingMixin:
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
        flattened.extend(_flatten_value(value))
    return flattened


def _flatten_value(value: Any) -> List[str]:
    if isinstance(value, list):
        return [str(item) for item in value]
    if isinstance(value, dict):
        return _flatten_values(value.values())
    if value is None:
        return []
    return [str(value)]


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
