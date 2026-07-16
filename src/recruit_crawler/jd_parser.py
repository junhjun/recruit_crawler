from __future__ import annotations

import re
from datetime import date
from typing import Iterable, List, Optional, Tuple, Union

from .identity import (
    NormalizedCandidate,
    normalize_candidate,
)
from .schemas import CandidateV2, JDSnapshot, PostingCandidate, SnapshotV2


def parse_deadline(value: Optional[str]) -> Tuple[Optional[date], bool]:
    if not value:
        return None, True
    try:
        return date.fromisoformat(value), False
    except ValueError:
        return None, True


RawJdValue = Union[None, str, List[str]]


def _list(raw: RawJdValue) -> List[str]:
    if isinstance(raw, list):
        return [str(item).strip() for item in raw if str(item).strip()]
    if isinstance(raw, str):
        return [raw.strip()] if raw.strip() else []
    return []


def _minimum_experience_years(raw: RawJdValue) -> Optional[int]:
    for item in _list(raw):
        if "경력무관" in item or "신입" in item:
            continue
        match = re.search(r"경력\s*(\d+)\s*년", item)
        if match:
            return int(match.group(1))
        if "경력" in item:
            return 1
    return None


def parse_candidate(candidate: PostingCandidate) -> JDSnapshot:
    deadline, uncertain = parse_deadline(candidate.deadline_raw)
    raw = candidate.raw_jd
    return JDSnapshot(
        source_id=candidate.source_id,
        source_url=candidate.source_url,
        source_posting_id=candidate.source_posting_id,
        title=candidate.title,
        company=candidate.company,
        location=candidate.location,
        deadline_raw=candidate.deadline_raw,
        deadline=deadline,
        deadline_uncertain=uncertain,
        required_qualifications=_list(raw.get("required_qualifications")),
        preferred_qualifications=_list(raw.get("preferred_qualifications")),
        responsibilities=_list(raw.get("responsibilities")),
        company_info=_list(raw.get("company_info")),
        minimum_experience_years=_minimum_experience_years(raw.get("experience_tags")),
        manual_review_flags=_list(raw.get("manual_review_flags")),
    )


def parse_candidates(candidates: Iterable[PostingCandidate]) -> List[JDSnapshot]:
    return [parse_candidate(candidate) for candidate in candidates]
def parse_candidate_v2(
    candidate: PostingCandidate | CandidateV2,
    *,
    detail_quality: str = "verified",
) -> SnapshotV2:
    normalized = candidate if isinstance(candidate, CandidateV2) else normalize_candidate(candidate).candidate
    raw = {str(key): tuple(value) for key, value in normalized.raw_structured}
    deadline, uncertain = parse_deadline(normalized.deadline_raw)
    return SnapshotV2(
        source_id=normalized.source_id,
        canonical_url=normalized.source_url,
        source_posting_id=normalized.source_posting_id,
        title=normalized.title,
        company=normalized.company,
        location=normalized.location,
        deadline=deadline,
        deadline_uncertain=uncertain,
        required_qualifications=raw.get("required_qualifications", ()),
        preferred_qualifications=raw.get("preferred_qualifications", ()),
        responsibilities=raw.get("responsibilities", ()),
        company_info=raw.get("company_info", ()),
        experience_tags=raw.get("experience_tags", ()),
        manual_review_flags=raw.get("manual_review_flags", ()),
        detail_quality=detail_quality,
    )


def normalize_and_parse_candidate_v2(
    candidate: PostingCandidate | CandidateV2,
    *,
    detail_quality: str = "verified",
) -> tuple[NormalizedCandidate, SnapshotV2]:
    normalized = candidate if isinstance(candidate, CandidateV2) else normalize_candidate(candidate)
    return normalized, parse_candidate_v2(normalized.candidate, detail_quality=detail_quality)


def parse_candidates_v2(
    candidates: Iterable[PostingCandidate | CandidateV2],
    *,
    detail_quality: str = "verified",
) -> List[SnapshotV2]:
    return [parse_candidate_v2(candidate, detail_quality=detail_quality) for candidate in candidates]


parse_candidate_snapshot_v2 = parse_candidate_v2
parse_candidates_snapshot_v2 = parse_candidates_v2
