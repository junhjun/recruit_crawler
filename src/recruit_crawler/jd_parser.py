from __future__ import annotations

import re
from datetime import date
from typing import Iterable, List, Optional, Tuple, Union

from .schemas import JDSnapshot, PostingCandidate


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
