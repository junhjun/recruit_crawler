"""Public link, presentation, and capacity policy for rendered reports."""
from __future__ import annotations

import re
import unicodedata
from typing import Mapping, Optional
from urllib.parse import urlsplit

from .schemas import AssessmentV2

REPORT_LINK_POLICY_VERSION = 1
REPORT_TABLE_COLUMNS = ("순위", "판정", "공고", "회사", "지역", "마감", "사유", "링크")

MAX_VERIFIED_REPORT_URL_LENGTH = 2048
MAX_REPORT_ROWS = 1000
MAX_REPORT_RANK_DIGITS = len(str(MAX_REPORT_ROWS))
MAX_DEGRADATION_NOTICES = 64
MAX_DEGRADATION_NOTICE_BYTES = 512
MAX_REPORT_ROW_BYTES = 4096
# Title, summary, table headings and every permitted degradation notice.
MAX_REPORT_FIXED_BYTES = 256 + MAX_DEGRADATION_NOTICES * MAX_DEGRADATION_NOTICE_BYTES
MAX_REPORT_BYTES = MAX_REPORT_FIXED_BYTES + MAX_REPORT_ROWS * MAX_REPORT_ROW_BYTES
# Compatibility aliases for provisional report-policy callers.
REPORT_MAX_QUEUE_ROWS = MAX_REPORT_ROWS
REPORT_MAX_NOTICE_ROWS = MAX_DEGRADATION_NOTICES
REPORT_MAX_DOCUMENT_BYTES = MAX_REPORT_BYTES
REPORT_MAX_LINE_BYTES = MAX_REPORT_ROW_BYTES

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


def validate_report_queue_capacity(queue_length: int) -> None:
    if type(queue_length) is not int or queue_length < 0:
        raise ValueError("report queue count must be a non-negative integer")
    if queue_length > MAX_REPORT_ROWS:
        raise ValueError("report queue exceeds capacity")


def validate_degradation_notice_capacity(notice_count: int) -> None:
    if type(notice_count) is not int or notice_count < 0:
        raise ValueError("report notice count must be a non-negative integer")
    if notice_count > MAX_DEGRADATION_NOTICES:
        raise ValueError("report degradation notices exceed capacity")


def report_byte_budget(queue_length: int, notice_count: int) -> int:
    validate_report_queue_capacity(queue_length)
    validate_degradation_notice_capacity(notice_count)
    return 256 + notice_count * MAX_DEGRADATION_NOTICE_BYTES + queue_length * MAX_REPORT_ROW_BYTES


def _canonical_rallit_slug(raw_slug: str) -> Optional[str]:
    if not raw_slug:
        return None
    encoded = bytearray()
    index = 0
    while index < len(raw_slug):
        char = raw_slug[index]
        if char == "%":
            if index + 2 >= len(raw_slug) or not re.fullmatch(
                r"[0-9A-Fa-f]{2}", raw_slug[index + 1 : index + 3]
            ):
                return None
            encoded.append(int(raw_slug[index + 1 : index + 3], 16))
            index += 3
        else:
            encoded.extend(char.encode("utf-8"))
            index += 1
    try:
        slug = bytes(encoded).decode("utf-8")
    except UnicodeDecodeError:
        return None
    if unicodedata.normalize("NFC", slug) != slug or any(
        char in {".", "/", "\\", "%"} or unicodedata.category(char) == "Cc"
        for char in slug
    ):
        return None
    return "".join(
        char if char in _RALLIT_SLUG_SAFE else f"%{byte:02X}"
        for byte in slug.encode("utf-8")
        for char in (chr(byte),)
    )


def verified_link_url(
    command_mode: str,
    source_id: str,
    source_url: Optional[str],
    source_posting_id: Optional[str],
    source_detail_quality: str,
) -> Optional[str]:
    """Return a canonical clickable URL only for verified detail pages."""
    source_id = str(source_id).casefold()
    posting_id = str(source_posting_id) if source_posting_id is not None else ""
    if (
        command_mode not in _SAFE_COMMAND_MODES
        or source_detail_quality != "verified"
        or source_id not in _PUBLIC_SOURCE_IDS
        or not posting_id
        or not _POSTING_ID_RE.fullmatch(posting_id)
        or not isinstance(source_url, str)
    ):
        return None
    candidate = source_url.strip()
    if (
        candidate != source_url
        or len(candidate) > MAX_VERIFIED_REPORT_URL_LENGTH
        or any(ord(char) < 0x20 or ord(char) == 0x7F for char in candidate)
    ):
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
        or hostname.casefold() not in _SOURCE_HOSTS[source_id]
        or parsed.netloc.casefold() not in _SOURCE_HOSTS[source_id]
    ):
        return None
    if source_id == "fixture":
        path = parsed.path.removeprefix("/")
        if (
            not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", path)
            or any(token in path.casefold() for token in ("list", "search", "synthetic", "generic"))
            or parsed.query
            or path != posting_id
        ):
            return None
        return candidate
    if source_id == "saramin":
        if not posting_id.isdecimal():
            return None
        detail_path = "/zf_user/jobs/relay/view-detail"
        outer_path = "/zf_user/jobs/relay/view"
        canonical_detail = (
            "https://www.saramin.co.kr/zf_user/jobs/relay/view-detail"
            f"?rec_idx={posting_id}&rec_seq=0"
        )
        query = f"rec_idx={posting_id}&rec_seq=0"
        if parsed.path == outer_path and parsed.query == query:
            return candidate
        if parsed.path == detail_path and parsed.query in {"", query}:
            return canonical_detail
        return None
    if source_id == "rallit":
        if parsed.query or not _RALLIT_POSTING_ID_RE.fullmatch(posting_id):
            return None
        segments = parsed.path.split("/")
        if len(segments) not in {3, 4} or segments[:2] != ["", "positions"]:
            return None
        if segments[2] != posting_id:
            return None
        if len(segments) == 3:
            return candidate
        slug = _canonical_rallit_slug(segments[3])
        return f"https://www.rallit.com/positions/{posting_id}/{slug}" if slug else None
    patterns = {
        "jobkorea": r"/Recruit/GI_Read/(?P<id>\d+)",
        "wanted": r"/wd/(?P<id>\d+)",
        "jumpit": r"/position/(?P<id>\d+)",
        "rocketpunch": r"/en/jobs/(?P<id>\d+)",
    }
    match = re.fullmatch(patterns[source_id], parsed.path)
    return candidate if match and match.group("id") == posting_id and not parsed.query else None


def project_report_presentation(
    assessment: AssessmentV2, *, command_mode: str
) -> Mapping[str, str | None]:
    disposition = str(assessment.disposition)
    link_url = verified_link_url(
        command_mode,
        assessment.source_id,
        assessment.source_url,
        assessment.source_posting_id,
        assessment.detail_quality,
    )
    return {
        "label": _LABELS.get(disposition, "원문 확인 필요"),
        "explanation": _EXPLANATIONS.get(disposition, _EXPLANATIONS["manual_review"]),
        "link_url": link_url,
        "link_state": "원문 링크 확인됨" if link_url else "원문 링크 확인 필요",
    }


__all__ = [
    "MAX_DEGRADATION_NOTICE_BYTES",
    "MAX_DEGRADATION_NOTICES",
    "MAX_REPORT_BYTES",
    "MAX_REPORT_FIXED_BYTES",
    "MAX_REPORT_RANK_DIGITS",
    "MAX_REPORT_ROW_BYTES",
    "MAX_REPORT_ROWS",
    "MAX_VERIFIED_REPORT_URL_LENGTH",
    "REPORT_LINK_POLICY_VERSION",
    "REPORT_TABLE_COLUMNS",
    "project_report_presentation",
    "report_byte_budget",
    "validate_degradation_notice_capacity",
    "validate_report_queue_capacity",
    "verified_link_url",
]
