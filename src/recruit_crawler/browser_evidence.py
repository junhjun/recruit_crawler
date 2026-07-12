from __future__ import annotations

import hashlib
import json
import re
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Protocol, runtime_checkable
from urllib.parse import urlparse

from .schemas import SourceManifest
from .sources.base import build_source_adapter
from .sources.http import SourceAccessError

_ALLOWED_TRANSCRIPT_KEYS = {
    "schema_version",
    "captured_at",
    "source_id",
    "target_url",
    "final_url",
    "target_lane",
    "browser_binary_kind",
    "command_mode",
    "exit_code",
    "timed_out",
    "dom_sha256",
    "parser_class",
    "posting_count",
    "filterability",
    "allowed_candidate_summaries",
    "privacy_findings",
    "errors",
}
_PRIVATE_MARKERS = ("PRIVATE_", "RAW_JD_CANARY", "Ignore previous instructions", "session=", "access_token=")


@runtime_checkable
class BrowserDomAdapter(Protocol):
    def _dump_dom(self, target: str) -> str:
        ...


def build_browser_evidence(
    manifest: SourceManifest,
    *,
    fixture_html: Optional[Path] = None,
    target_url: Optional[str] = None,
) -> Dict[str, Any]:
    errors: List[str] = []
    target = target_url or _default_target_url(manifest)
    command_mode = "fixture" if fixture_html else "chrome_dump_dom"
    options = dict(manifest.options)
    if fixture_html is not None:
        options["browser_capture_fixture_path"] = str(fixture_html)
        if target:
            options.setdefault("search_urls", [target])
    evidence_manifest = SourceManifest(**{**asdict(manifest), "options": options})
    dom = ""
    candidates = []
    exit_code = 0
    parser_class = ""
    try:
        _reject_private_target(target)
        adapter = build_source_adapter(evidence_manifest, Path("fixtures/postings.json"))
        parser_class = type(adapter).__name__
        if isinstance(adapter, BrowserDomAdapter) and target:
            dom = adapter._dump_dom(target)
        elif fixture_html is not None:
            dom = fixture_html.read_text(encoding="utf-8")
        candidates = adapter.collect()
        errors.extend(getattr(adapter, "errors", []))
    except (OSError, SourceAccessError, UnicodeError, ValueError) as exc:
        exit_code = 1
        errors.append(_redact(str(exc)))
    privacy_findings = _privacy_findings(dom)
    if privacy_findings:
        exit_code = 1
    transcript = {
        "schema_version": 1,
        "captured_at": datetime.now(timezone.utc).isoformat(),
        "source_id": manifest.source_id,
        "target_url": target,
        "final_url": target,
        "target_lane": manifest.target_lane,
        "browser_binary_kind": "Chrome/Chromium" if command_mode == "chrome_dump_dom" else "fixture",
        "command_mode": command_mode,
        "exit_code": exit_code,
        "timed_out": False,
        "dom_sha256": hashlib.sha256(dom.encode("utf-8")).hexdigest() if dom else "",
        "parser_class": parser_class,
        "posting_count": len(candidates),
        "filterability": _filterability(candidates),
        "allowed_candidate_summaries": [
            {
                "title": _redact(candidate.title),
                "company": _redact(candidate.company),
                "location": _redact(candidate.location),
                "source_url": _redact(candidate.source_url),
            }
            for candidate in candidates[:5]
        ],
        "privacy_findings": privacy_findings,
        "errors": errors,
    }
    return {key: transcript[key] for key in _ALLOWED_TRANSCRIPT_KEYS}


def write_browser_evidence(transcript: Dict[str, Any], output: Path) -> None:
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(transcript, ensure_ascii=False, indent=2), encoding="utf-8")


def _default_target_url(manifest: SourceManifest) -> str:
    urls = manifest.options.get("search_urls") or manifest.options.get("start_urls") or manifest.options.get("detail_urls") or []
    return str(urls[0]) if urls else f"https://{manifest.domains[0]}/" if manifest.domains else ""


def _reject_private_target(url: str) -> None:
    parsed = urlparse(url)
    lowered = url.lower()
    if parsed.username or parsed.password or "session" in lowered or "token" in lowered or "private" in parsed.path.lower():
        raise SourceAccessError("browser evidence target must not contain auth/session/private material")


def _privacy_findings(text: str) -> List[str]:
    findings = []
    for marker in _PRIVATE_MARKERS:
        if marker.lower() in text.lower():
            findings.append(f"redacted-private-marker:{marker.rstrip('=')}")
    return findings


def _redact_marker(value: str, marker: str) -> str:
    return re.sub(re.escape(marker), "[REDACTED]", value, flags=re.I)


def _redact(value: str) -> str:
    redacted = value
    for marker in _PRIVATE_MARKERS:
        redacted = _redact_marker(redacted, marker)
    return redacted[:500]


def _filterability(candidates: List[Any]) -> Dict[str, bool]:
    raw_items = [candidate.raw_jd for candidate in candidates]
    return {
        "role_title": any(candidate.title for candidate in candidates),
        "skills_requirements": any(item.get("required_qualifications") or item.get("preferred_qualifications") for item in raw_items),
        "responsibilities": any(item.get("responsibilities") for item in raw_items),
        "seniority_experience": any(item.get("experience_tags") for item in raw_items),
        "location": any(candidate.location for candidate in candidates),
        "stable_posting_url": any(candidate.source_url.startswith("http") for candidate in candidates),
    }
