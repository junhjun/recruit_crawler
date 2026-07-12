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
from .platform_shared import (
    _as_text_list,
    _clean_extracted_text,
    _clean_visible_text,
    _filter_candidates,
    _first_match,
    _looks_like_location,
    _merged_options,
    _section_between,
    _strip_tags,
)
from .platform_rocketpunch_cards import (
    _candidate_from_rocketpunch_card,
    _merge_rocketpunch_detail,
    _rocketpunch_listing_blocks,
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
            except (OSError, SourceAccessError, subprocess.TimeoutExpired) as exc:
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
            except (OSError, SourceAccessError, subprocess.TimeoutExpired) as exc:
                if self.manifest.failure_mode == "fail_run":
                    raise
                self.errors.append(f"{candidate.source_url}: {exc}")
                enriched.append(candidate)
                continue
            enriched.append(_merge_rocketpunch_detail(candidate, html))
            self._sleep()
        return enriched
