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
from .platform_rocketpunch_detail import (
    _looks_like_rocketpunch_card_text,
    _rocketpunch_card_company,
    _rocketpunch_card_location,
    _rocketpunch_card_snippet,
    _rocketpunch_card_title,
    _rocketpunch_deadline,
    _rocketpunch_detail_text,
    _rocketpunch_experience_tags,
    _rocketpunch_posting_id,
    _rocketpunch_skill_terms,
    _rocketpunch_source_url,
    _rocketpunch_synthetic_id,
)

def _rocketpunch_listing_blocks(html: str) -> List[str]:
    blocks: List[str] = []
    marker_pattern = re.compile(
        r'<(?:article|li|div)\b(?:[^>]*class="[^"]*(?:listing-card|job-card|job-item|company-list)[^"]*"[^>]*|[^>]*data-index="\d+"[^>]*)>',
        re.I,
    )
    matches = list(marker_pattern.finditer(html))
    for index, match in enumerate(matches):
        end = matches[index + 1].start() if index + 1 < len(matches) else len(html)
        block = html[match.start() : end]
        text = _clean_visible_text(block)
        if _rocketpunch_card_title(block, text):
            blocks.append(block)
    if blocks:
        return blocks
    return [
        f"<div>{line}</div>"
        for line in _clean_visible_text(html).split(" Product Manager ")
        if _looks_like_rocketpunch_card_text(line)
    ]


def _candidate_from_rocketpunch_card(
    source_id: str,
    listing_url: str,
    block: str,
    index: int,
) -> PostingCandidate:
    text = _clean_visible_text(block)
    title = _rocketpunch_card_title(block, text)
    company = _rocketpunch_card_company(block, text, title)
    snippet = _rocketpunch_card_snippet(text, title, company)
    location = _rocketpunch_card_location(text)
    skills = _rocketpunch_skill_terms(text)
    posting_id = _rocketpunch_posting_id(block) or _rocketpunch_synthetic_id(index, company, title)
    source_url = _rocketpunch_source_url(listing_url, posting_id)
    return PostingCandidate(
        source_id=source_id,
        source_url=source_url,
        source_posting_id=posting_id,
        title=title,
        company=company or "rocketpunch",
        location=location,
        deadline_raw=None,
        collected_at=datetime.now(timezone.utc),
        raw_jd={
            "required_qualifications": skills or [snippet],
            "preferred_qualifications": [],
            "responsibilities": [snippet or title],
            "company_info": [company] if company else [],
            "experience_tags": _rocketpunch_experience_tags(text),
        },
    )


def _merge_rocketpunch_detail(candidate: PostingCandidate, html: str) -> PostingCandidate:
    text = _clean_visible_text(html)
    detail = _rocketpunch_detail_text(text, candidate.title)
    if not detail:
        return candidate
    raw_jd = dict(candidate.raw_jd)
    responsibilities = _section_between(detail, ["Responsibilities"], ["Qualifications", "Preferred Qualifications", "Benefits", "Process", "Notes"])
    qualifications = _section_between(detail, ["Qualifications"], ["Preferred Qualifications", "Benefits", "Process", "Notes"])
    preferred = _section_between(detail, ["Preferred Qualifications"], ["Benefits", "Process", "Notes"])
    team_info = _section_between(detail, ["Team Introduction"], ["Responsibilities", "Qualifications", "Preferred Qualifications"])
    if responsibilities:
        raw_jd["responsibilities"] = [_clean_extracted_text(responsibilities)]
    if qualifications:
        raw_jd["required_qualifications"] = [_clean_extracted_text(qualifications), *_rocketpunch_skill_terms(qualifications)]
    if preferred:
        raw_jd["preferred_qualifications"] = [_clean_extracted_text(preferred)]
    if team_info:
        raw_jd["company_info"] = [_clean_extracted_text(team_info)]
    experience_tags = _rocketpunch_experience_tags(detail)
    if experience_tags:
        raw_jd["experience_tags"] = experience_tags
    deadline = _rocketpunch_deadline(detail)
    return PostingCandidate(
        source_id=candidate.source_id,
        source_url=candidate.source_url,
        source_posting_id=candidate.source_posting_id,
        title=candidate.title,
        company=candidate.company,
        location=candidate.location,
        deadline_raw=deadline or candidate.deadline_raw,
        collected_at=candidate.collected_at,
        raw_jd=raw_jd,
    )
