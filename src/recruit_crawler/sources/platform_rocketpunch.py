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
from ..schemas import (
    CandidateDetailIssueCodeV2,
    CandidateDetailIssueV2,
    PostingCandidate,
    SourceManifest,
)
from .http import PublicJobsHttpAdapter, SourceAccessError, SourceBudgetExceeded, _contains_any, _date_prefix
from .base import initialize_source_adapter
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
from .platform_rocketpunch_detail import (
    _rocketpunch_detail_text,
    _rocketpunch_is_direct_detail_url,
)
from .platform_rocketpunch_cards import (
    _candidate_from_rocketpunch_card,
    _merge_rocketpunch_detail,
    _rocketpunch_listing_blocks,
)
class BrowserChildLifecycleError(SourceAccessError):
    """A browser child could not be safely attached to the worker lifecycle."""


class BrowserChildRegistryV1:
    """Track browser children that must remain in the worker's process group."""

    def __init__(self, worker_pgid: Optional[int] = None) -> None:
        if worker_pgid is None:
            worker_pgid = self._read_pgid(os.getpid())
        if type(worker_pgid) is not int or worker_pgid <= 0:
            raise BrowserChildLifecycleError("worker process group could not be verified")
        self.worker_pgid = worker_pgid
        self._handles: Dict[int, Any] = {}

    @staticmethod
    def _read_pgid(pid: int) -> int:
        getpgid = getattr(os, "getpgid", None)
        if not callable(getpgid):
            raise BrowserChildLifecycleError("process-group verification is unavailable")
        try:
            pgid = getpgid(pid)
        except (AttributeError, OSError, PermissionError, ProcessLookupError) as exc:
            raise BrowserChildLifecycleError("process-group verification failed") from exc
        if type(pgid) is not int or pgid <= 0:
            raise BrowserChildLifecycleError("process-group verification returned an invalid value")
        return pgid

    def register(self, process: Any) -> None:
        pid = getattr(process, "pid", None)
        if type(pid) is not int or pid <= 0:
            self._terminate_and_reap(process)
            raise BrowserChildLifecycleError("browser process registration failed")
        self._handles[pid] = process
        try:
            child_pgid = self._read_pgid(pid)
        except BrowserChildLifecycleError:
            self._handles.pop(pid, None)
            self._terminate_and_reap(process)
            raise
        if child_pgid != self.worker_pgid:
            self._handles.pop(pid, None)
            self._terminate_and_reap(process)
            raise BrowserChildLifecycleError("browser process group does not match worker")

    def reap(self, process: Any) -> None:
        self._reap(process)
        pid = getattr(process, "pid", None)
        if type(pid) is int:
            self._handles.pop(pid, None)

    def terminate_and_reap(self) -> None:
        handles = list(self._handles.values())
        for process in handles:
            self._terminate(process)
        for process in handles:
            self._reap(process)
            pid = getattr(process, "pid", None)
            if type(pid) is int:
                self._handles.pop(pid, None)

    @staticmethod
    def _terminate(process: Any) -> None:
        try:
            terminate = getattr(process, "terminate")
            terminate()
        except (AttributeError, OSError, PermissionError, ProcessLookupError):
            pass

    @staticmethod
    def _reap(process: Any) -> None:
        wait = getattr(process, "wait", None)
        if not callable(wait):
            return
        try:
            wait(timeout=2.0)
        except subprocess.TimeoutExpired:
            try:
                kill = getattr(process, "kill")
                kill()
            except (AttributeError, OSError, PermissionError, ProcessLookupError):
                pass
            try:
                wait(timeout=2.0)
            except (AttributeError, OSError, PermissionError, ProcessLookupError, subprocess.TimeoutExpired):
                pass
        except (AttributeError, OSError, PermissionError, ProcessLookupError, TypeError):
            pass

    @classmethod
    def _terminate_and_reap(cls, process: Any) -> None:
        cls._terminate(process)
        cls._reap(process)


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
        initialize_source_adapter(self)
        self._browser_registry = BrowserChildRegistryV1()

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
        try:
            self._validate_access()
            candidates: List[PostingCandidate] = []
            for url in self._search_urls():
                self._validate_url(url)
                try:
                    html = self._dump_dom(url)
                except BrowserChildLifecycleError:
                    raise
                except SourceBudgetExceeded:
                    raise
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
        finally:
            self._browser_registry.terminate_and_reap()

    def _dump_dom(self, url: str) -> str:
        self._ensure_budget()
        fixture_path = self.options.get("browser_capture_fixture_path")
        if fixture_path:
            self._ensure_budget()
            return Path(str(fixture_path)).read_text(encoding="utf-8")
        try:
            browser_binary = self._browser_binary()
        except BrowserChildLifecycleError:
            raise
        except BaseException as exc:
            raise BrowserChildLifecycleError("browser binary launch failed") from exc
        timeout = float(self.options.get("browser_timeout_seconds", 45))
        remaining = self._remaining_budget()
        if remaining is not None:
            timeout = min(timeout, remaining)
        try:
            process = subprocess.Popen(
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
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
        except BrowserChildLifecycleError:
            raise
        except BaseException as exc:
            raise BrowserChildLifecycleError("browser launch failed") from exc
        try:
            self._browser_registry.register(process)
            stdout, stderr = process.communicate(timeout=timeout)
            self._browser_registry.reap(process)
        except BrowserChildLifecycleError:
            raise
        except BaseException as exc:
            self._browser_registry.terminate_and_reap()
            raise BrowserChildLifecycleError("browser child execution failed") from exc
        if process.returncode != 0:
            raise BrowserChildLifecycleError(
                (stderr or "").strip() or f"browser exited with {process.returncode}"
            )
        return stdout or ""

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

    def _record_detail_issue(
        self,
        candidate: PostingCandidate,
        code: CandidateDetailIssueCodeV2,
    ) -> None:
        self.issues.append(
            CandidateDetailIssueV2(
                source_id=candidate.source_id,
                source_url=candidate.source_url,
                source_posting_id=candidate.source_posting_id,
                code=code,
            )
        )

    def _enrich_detail_candidates(self, candidates: List[PostingCandidate]) -> List[PostingCandidate]:
        try:
            if not bool(self.options.get("fetch_detail_pages", True)):
                for candidate in candidates:
                    issue_code = (
                        CandidateDetailIssueCodeV2.DETAIL_UNVERIFIED
                        if _rocketpunch_is_direct_detail_url(candidate.source_url)
                        else CandidateDetailIssueCodeV2.DETAIL_URL_INVALID
                    )
                    self._record_detail_issue(candidate, issue_code)
                return candidates

            enriched: List[PostingCandidate] = []
            max_detail_pages = int(self.options.get("max_detail_pages", len(candidates)))
            detail_attempts = 0
            for candidate in candidates:
                if not _rocketpunch_is_direct_detail_url(candidate.source_url):
                    self._record_detail_issue(
                        candidate,
                        CandidateDetailIssueCodeV2.DETAIL_URL_INVALID,
                    )
                    enriched.append(candidate)
                    continue
                if detail_attempts >= max_detail_pages:
                    self._record_detail_issue(
                        candidate,
                        CandidateDetailIssueCodeV2.DETAIL_UNVERIFIED,
                    )
                    enriched.append(candidate)
                    continue
                detail_attempts += 1
                try:
                    html = self._dump_dom(candidate.source_url)
                except BrowserChildLifecycleError:
                    raise
                except SourceBudgetExceeded:
                    raise
                except (OSError, SourceAccessError, subprocess.TimeoutExpired) as exc:
                    if self.manifest.failure_mode == "fail_run":
                        raise
                    self.errors.append(f"{candidate.source_url}: {exc}")
                    self._record_detail_issue(
                        candidate,
                        CandidateDetailIssueCodeV2.DETAIL_FETCH_FAILED,
                    )
                    enriched.append(candidate)
                    continue
                enriched_candidate = _merge_rocketpunch_detail(candidate, html)
                if not _rocketpunch_detail_text(_clean_visible_text(html), candidate.title):
                    self._record_detail_issue(
                        candidate,
                        CandidateDetailIssueCodeV2.DETAIL_UNVERIFIED,
                    )
                enriched.append(enriched_candidate)
                self._sleep()
            return enriched
        finally:
            self._browser_registry.terminate_and_reap()