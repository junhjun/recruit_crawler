from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path
from typing import Any, Dict, List, Optional


@dataclass(frozen=True)
class Thresholds:
    apply: int = 75
    hold: int = 50


@dataclass(frozen=True)
class ScoringWeights:
    required: int = 45
    preferred: int = 20
    responsibilities: int = 15
    company: int = 10
    location: int = 10


@dataclass(frozen=True)
class Profile:
    desired_roles: List[str]
    skills: List[str]
    preferred_locations: List[str]
    max_experience_years: int = 0
    exclusions: List[str] = field(default_factory=list)
    private_canaries: List[str] = field(default_factory=list)


@dataclass(frozen=True)
class SourceManifest:
    source_id: str
    enabled: bool
    access_mode: str
    auth_required: bool
    tos_review_status: str
    domains: List[str]
    rate_limit: str
    failure_mode: str
    allowed_persisted_fields: List[str]
    display_name: str = ""
    v1_role: str = ""
    target_status: str = "deferred"
    maintenance_status: str = "watch"
    target_lane: Optional[str] = None
    candidate_lanes: List[str] = field(default_factory=list)
    automation_level: str = "unknown"
    status_reason: str = ""
    evidence: List[str] = field(default_factory=list)
    blockers: List[str] = field(default_factory=list)
    next_action: str = ""
    adapter_code_path: str = ""
    test_refs: List[str] = field(default_factory=list)
    docs_refs: List[str] = field(default_factory=list)
    options: Dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class AppConfig:
    top_n: int
    output_dir: Path
    fixture_path: Path
    delivery_mode: str
    thresholds: Thresholds
    scoring_weights: ScoringWeights
    profile: Profile
    sources: List[SourceManifest]


@dataclass(frozen=True)
class PostingCandidate:
    source_id: str
    source_url: str
    source_posting_id: Optional[str]
    title: str
    company: str
    location: str
    deadline_raw: Optional[str]
    collected_at: datetime
    raw_jd: Dict[str, Any]


@dataclass(frozen=True)
class JDSnapshot:
    source_id: str
    source_url: str
    source_posting_id: Optional[str]
    title: str
    company: str
    location: str
    deadline_raw: Optional[str]
    deadline: Optional[date]
    deadline_uncertain: bool
    required_qualifications: List[str]
    preferred_qualifications: List[str]
    responsibilities: List[str]
    company_info: List[str]
    minimum_experience_years: Optional[int] = None
    manual_review_flags: List[str] = field(default_factory=list)


@dataclass(frozen=True)
class FitAssessment:
    snapshot: JDSnapshot
    score: int
    recommendation: str
    matched_evidence: List[str]
    gaps: List[str]
    risks: List[str]
    verification_questions: List[str]
    positioning_seed: str


@dataclass(frozen=True)
class RunSummary:
    run_date: date
    sources_attempted: List[str]
    source_errors: List[str]
    candidates_collected: int
    duplicates_removed: int
    experience_excluded: int
    expired_excluded: int
    ranked_count: int
    report_path: Path
