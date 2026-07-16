"""Transient V3 report presentation and link policy.

This module is deliberately separate from persisted assessments.  It translates
runtime dispositions to Korean reader-facing labels and only emits a source
link when the source/detail provenance is safe and verified.
"""
from __future__ import annotations

from typing import Mapping, Optional
from urllib.parse import urlsplit
import unicodedata
import re

from .schemas import AssessmentV2

REPORT_LINK_POLICY_VERSION = 1

# Source IDs that are part of the approved public discovery lane.  Capture-only
# and excluded sources are intentionally absent; unknown IDs fail closed.
_PUBLIC_SOURCE_IDS = frozenset(
    {"fixture", "saramin", "jobkorea", "wanted", "jumpit", "rallit", "rocketpunch"}
)
_SAFE_COMMAND_MODES = frozenset({"dry-run", "live-run", "scheduled-run", "replay"})
_SOURCE_HOSTS = {
    "fixture": frozenset({"jobs.example.test"}),
    "saramin": frozenset({"www.saramin.co.kr"}),
    "jobkorea": frozenset({"www.jobkorea.co.kr"}),
    "wanted": frozenset({"www.wanted.co.kr"}),
    "jumpit": frozenset({"jumpit.saramin.co.kr"}),
    "rallit": frozenset({"www.rallit.com"}),
    "rocketpunch": frozenset({"rocketpunch.com", "www.rocketpunch.com"}),
}
_POSTING_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
_RALLIT_POSTING_ID_RE = re.compile(r"^[0-9]+$")
_RALLIT_SLUG_SAFE = frozenset(
    "-~!$&'()*+,;=:@ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789"
)
_GENERIC_FIXTURE_PATHS = frozenset(
    {"en", "gi_read", "jobs", "list", "position", "positions", "recruit", "relay", "search", "view", "wd"}
)

_LABELS = {
    "apply": "지원 추천",
    "hold": "도전 지원",
    "manual_review": "원문 확인 필요",
    "exclude": "제외",
    "expired": "제외",
    "low_priority": "제외",
}
_EXPLANATIONS = {
    "apply": "핵심 조건과 프로필 신호가 맞아 지원을 권합니다.",
    "hold": "일부 조건을 확인한 뒤 도전 지원을 검토합니다.",
    "manual_review": "자동 판정만으로 확정하기 어려워 원문 확인이 필요합니다.",
    "exclude": "현재 기준으로 우선순위에서 제외합니다.",
    "expired": "마감일이 지나 현재 지원 우선순위에서 제외합니다.",
    "low_priority": "현재 기준으로 우선순위가 낮아 제외합니다.",
}


def _canonical_rallit_slug(raw_slug: str) -> Optional[str]:
    """Decode and canonicalize one Rallit slug path segment."""
    if not raw_slug:
        return None
    encoded = bytearray()
    index = 0
    while index < len(raw_slug):
        char = raw_slug[index]
        if char == "%":
            if index + 2 >= len(raw_slug):
                return None
            escape = raw_slug[index + 1 : index + 3]
            if not re.fullmatch(r"[0-9A-Fa-f]{2}", escape):
                return None
            encoded.append(int(escape, 16))
            index += 3
            continue
        try:
            encoded.extend(char.encode("utf-8"))
        except UnicodeEncodeError:
            return None
        index += 1

    try:
        slug = bytes(encoded).decode("utf-8")
    except UnicodeDecodeError:
        return None
    if unicodedata.normalize("NFC", slug) != slug:
        return None
    if any(
        char in {".", "/", "\\", "%"} or unicodedata.category(char) == "Cc"
        for char in slug
    ):
        return None

    canonical = []
    for byte in slug.encode("utf-8"):
        char = chr(byte)
        canonical.append(char if char in _RALLIT_SLUG_SAFE else f"%{byte:02X}")
    return "".join(canonical)


def verified_link_url(
    command_mode: str,
    source_id: str,
    source_url: Optional[str],
    source_posting_id: Optional[str],
    source_detail_quality: str,
) -> Optional[str]:
    """Return a clickable canonical URL only for verified public details."""
    normalized_source_id = str(source_id).casefold()
    posting_id = str(source_posting_id) if source_posting_id is not None else ""
    if command_mode not in _SAFE_COMMAND_MODES or source_detail_quality != "verified":
        return None
    if normalized_source_id not in _PUBLIC_SOURCE_IDS or not posting_id:
        return None
    if not _POSTING_ID_RE.fullmatch(posting_id) or not isinstance(source_url, str):
        return None
    candidate = source_url.strip()
    if candidate != source_url or len(candidate) > 2048:
        return None
    if any(ord(char) < 0x20 or ord(char) == 0x7F for char in candidate):
        return None
    try:
        parsed = urlsplit(candidate)
        hostname = parsed.hostname
        port = parsed.port
    except ValueError:
        return None
    if (
        parsed.scheme != "https"
        or not hostname
        or parsed.username
        or parsed.password
        or port is not None
        or parsed.fragment
        or hostname.casefold() not in _SOURCE_HOSTS[normalized_source_id]
        or parsed.netloc.casefold() not in _SOURCE_HOSTS[normalized_source_id]
    ):
        return None

    if normalized_source_id == "fixture":
        path = parsed.path.removeprefix("/")
        if (
            not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", path)
            or path.casefold() in _GENERIC_FIXTURE_PATHS
            or path.casefold().startswith(("synthetic-", "listing-"))
            or parsed.query
            or path != posting_id
        ):
            return None
        return candidate

    if normalized_source_id == "saramin":
        if not posting_id.isdecimal():
            return None
        canonical_detail = (
            f"https://www.saramin.co.kr/zf_user/jobs/relay/view-detail"
            f"?rec_idx={posting_id}&rec_seq=0"
        )
        canonical_outer = (
            f"https://www.saramin.co.kr/zf_user/jobs/relay/view"
            f"?rec_idx={posting_id}&rec_seq=0"
        )
        if parsed.path == "/zf_user/jobs/relay/view-detail":
            if parsed.query in {"", f"rec_idx={posting_id}&rec_seq=0"}:
                return canonical_detail
            return None
        if parsed.path == "/zf_user/jobs/relay/view":
            return canonical_outer if candidate == canonical_outer else None
        return None

    if normalized_source_id == "rallit":
        if "?" in candidate or "#" in candidate:
            return None
        if not _RALLIT_POSTING_ID_RE.fullmatch(posting_id):
            return None
        segments = parsed.path.split("/")
        if len(segments) not in {3, 4} or segments[:2] != ["", "positions"]:
            return None
        if not _RALLIT_POSTING_ID_RE.fullmatch(segments[2]) or segments[2] != posting_id:
            return None
        if len(segments) == 3:
            return candidate
        canonical_slug = _canonical_rallit_slug(segments[3])
        if canonical_slug is None:
            return None
        return f"https://www.rallit.com/positions/{posting_id}/{canonical_slug}"

    path_patterns = {
        "jobkorea": r"/Recruit/GI_Read/(?P<id>\d+)",
        "wanted": r"/wd/(?P<id>\d+)",
        "jumpit": r"/position/(?P<id>\d+)",
        "rocketpunch": r"/en/jobs/(?P<id>\d+)",
    }
    match = re.fullmatch(path_patterns[normalized_source_id], parsed.path)
    if match is None or match.group("id") != posting_id or parsed.query:
        return None
    return candidate


def project_report_presentation(
    assessment: AssessmentV2, *, command_mode: str
) -> Mapping[str, str | None]:
    """Project one assessment into the transient Korean report surface."""
    disposition = str(assessment.disposition)
    label = _LABELS.get(disposition, "원문 확인 필요")
    explanation = _EXPLANATIONS.get(disposition, _EXPLANATIONS["manual_review"])
    link_url = verified_link_url(
        command_mode,
        assessment.source_id,
        assessment.source_url,
        assessment.source_posting_id,
        assessment.detail_quality,
    )
    return {
        "label": label,
        "explanation": explanation,
        "link_url": link_url,
        "link_state": "원문 링크 확인됨" if link_url else "원문 링크 확인 필요",
    }


__all__ = [
    "REPORT_LINK_POLICY_VERSION",
    "project_report_presentation",
    "verified_link_url",
]
