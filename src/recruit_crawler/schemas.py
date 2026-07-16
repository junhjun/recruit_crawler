from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from enum import Enum
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple


PIPELINE_RESULT_SCHEMA_VERSION = 2
PIPELINE_RESULT_V4_SCHEMA_VERSION = 4
GATE_V4_SCHEMA_VERSION = 4
PIPELINE_RESULT_SCHEMA_VERSION_V4 = PIPELINE_RESULT_V4_SCHEMA_VERSION
GATE_SCHEMA_VERSION_V4 = GATE_V4_SCHEMA_VERSION
SCORE_SCHEMA_VERSION = 2
DISPOSITION_SCHEMA_VERSION = 2
GATE_SCHEMA_VERSION = 2
PERSISTENCE_ENVELOPE_SCHEMA_VERSION = 4
STORAGE_SCHEMA_VERSION = 4
SOURCE_OUTCOME_SCHEMA_VERSION = 1
CORPUS_SCHEMA_VERSION = 2
PUBLIC_SOURCE_IDS_V1 = frozenset(
    {"fixture", "saramin", "jobkorea", "wanted", "jumpit", "rallit", "rocketpunch"}
)
SOURCE_EXECUTION_OUTCOME_STATUSES_V1 = frozenset(
    {"success", "collection_error", "collection_failed", "source_timeout", "aggregate_budget_exhausted"}
)
SOURCE_EXECUTION_OUTCOME_ERROR_CODES_V1 = frozenset(
    {"collection_error", "collection_failed", "source_timeout", "aggregate_budget_exhausted"}
)
REPORT_ARTIFACT_SCHEMA_VERSION = 2
REFERENCE_ORACLE_VERSION = 1


class _StringEnum(str, Enum):
    """String-valued enums keep legacy string comparisons working."""


class CommandMode(_StringEnum):
    DRY_RUN = "dry-run"
    LIVE_RUN = "live-run"
    SCHEDULED_RUN = "scheduled-run"
    CAPTURE_IMPORT = "capture-import"
    REPLAY = "replay"


class ContextStatus(_StringEnum):
    COMPLETE = "complete"
    NEEDS_CONTEXT = "needs_context"


class DetailQuality(_StringEnum):
    VERIFIED = "verified"
    MANUAL_ONLY = "manual_only"
    REJECTED = "rejected"


class Disposition(_StringEnum):
    APPLY = "apply"
    HOLD = "hold"
    MANUAL_REVIEW = "manual_review"
    LOW_PRIORITY = "low_priority"
    EXCLUDE = "exclude"
    EXPIRED = "expired"


class GateStatus(_StringEnum):
    PASS = "pass"
    WARNING = "warning"
    FAIL = "fail"


class RequirementKind(_StringEnum):
    EDUCATION = "education"
    EXPERIENCE = "experience"
    MILITARY_PROGRAM = "military_program"


class RequirementOperator(_StringEnum):
    AT_LEAST = "at_least"
    RANGE = "range"
    NONE = "none"


class RequirementModality(_StringEnum):
    MANDATORY = "mandatory"
    PREFERRED = "preferred"
    AMBIGUOUS = "ambiguous"


class SourceField(_StringEnum):
    TITLE = "title"
    REQUIRED_QUALIFICATIONS = "required_qualifications"
    PREFERRED_QUALIFICATIONS = "preferred_qualifications"
    RESPONSIBILITIES = "responsibilities"
    COMPANY_INFO = "company_info"
    EXPERIENCE_TAGS = "experience_tags"


class EvidenceConfidence(_StringEnum):
    HIGH = "high"
    UNCERTAIN = "uncertain"


class EligibilityState(_StringEnum):
    NOT_APPLICABLE = "not_applicable"
    MATCH = "match"
    MISMATCH = "mismatch"
    USER_UNKNOWN = "user_unknown"
    REVIEW_REQUIRED = "review_required"


class ClaimProvenance(_StringEnum):
    CONFIG = "config"
    NONE = "none"


# Short aliases match the terminology used in the v2 specification.
Command = CommandMode
Operator = RequirementOperator
Modality = RequirementModality
Confidence = EvidenceConfidence
Provenance = ClaimProvenance


FrozenMap = Tuple[Tuple[str, Any], ...]


def _freeze(value: Any) -> Any:
    """Recursively remove mutable containers from v2 public records."""
    if isinstance(value, dict):
        return tuple((str(key), _freeze(item)) for key, item in value.items())
    if isinstance(value, (list, tuple)):
        return tuple(_freeze(item) for item in value)
    if isinstance(value, set):
        return tuple(sorted((_freeze(item) for item in value), key=repr))
    return value


class CandidateDetailIssueCodeV2(_StringEnum):
    DETAIL_URL_INVALID = "detail_url_invalid"
    DETAIL_FETCH_FAILED = "detail_fetch_failed"
    DETAIL_UNVERIFIED = "detail_unverified"


@dataclass(frozen=True, slots=True)
class CandidateDetailIssueV2:
    source_id: str
    source_url: str
    source_posting_id: Optional[str]
    code: CandidateDetailIssueCodeV2


@dataclass(frozen=True, slots=True)
class CandidateV2:
    source_id: str
    source_url: str
    source_posting_id: Optional[str]
    title: str
    company: str
    location: str
    deadline_raw: Optional[str]
    collected_at: datetime
    raw_structured: Tuple[Tuple[str, Any], ...]

    def __post_init__(self) -> None:
        object.__setattr__(self, "raw_structured", _freeze(self.raw_structured))


@dataclass(frozen=True, slots=True)
class SnapshotV2:
    source_id: str
    canonical_url: str
    source_posting_id: Optional[str]
    title: str
    company: str
    location: str
    deadline: Optional[date]
    deadline_uncertain: bool
    required_qualifications: Tuple[str, ...]
    preferred_qualifications: Tuple[str, ...]
    responsibilities: Tuple[str, ...]
    company_info: Tuple[str, ...]
    experience_tags: Tuple[str, ...]
    manual_review_flags: Tuple[str, ...]
    detail_quality: str

    def __post_init__(self) -> None:
        for name in (
            "required_qualifications",
            "preferred_qualifications",
            "responsibilities",
            "company_info",
            "experience_tags",
            "manual_review_flags",
        ):
            object.__setattr__(self, name, tuple(getattr(self, name)))


@dataclass(frozen=True, slots=True)
class RequirementEvidence:
    kind: str
    operator: str
    modality: str
    source_field: str
    item_index: int
    sentence_index: int
    text: str
    confidence: str


@dataclass(frozen=True, slots=True)
class EligibilityResult:
    requirement_index: int
    state: str
    reason_code: str
    claim_provenance: str
    evidence: Tuple[RequirementEvidence, ...]

    def __post_init__(self) -> None:
        object.__setattr__(self, "evidence", tuple(self.evidence))


@dataclass(frozen=True, slots=True)
class ScoreBreakdownV2:
    required_numerator: int
    required_denominator: int
    required_score: int
    responsibilities_numerator: int
    responsibilities_denominator: int
    responsibilities_score: int
    role_numerator: int
    role_denominator: int
    role_score: int
    preferred_numerator: int
    preferred_denominator: int
    preferred_score: int
    location_numerator: int
    location_denominator: int
    location_score: int
    raw_score: int
    score: int


@dataclass(frozen=True, slots=True)
class AssessmentV2:
    recommendation_id: str
    posting_key: str
    source_id: str
    source_url: str
    source_posting_id: Optional[str]
    title: str
    company: str
    location: str
    deadline: Optional[date]
    deadline_uncertain: bool
    score: int
    score_breakdown: ScoreBreakdownV2
    disposition: str
    reason_codes: Tuple[str, ...]
    detail_quality: str
    matched_evidence: Tuple[str, ...]
    eligibility: Tuple[EligibilityResult, ...]
    manual_review_flags: Tuple[str, ...]
    opaque_identity: str

    def __post_init__(self) -> None:
        for name in ("reason_codes", "matched_evidence", "eligibility", "manual_review_flags"):
            object.__setattr__(self, name, tuple(getattr(self, name)))


@dataclass(frozen=True, slots=True)
class SourceMetricV2:
    source_id: str
    attempted: bool
    accepted_count: int
    rejected_count: int
    duplicate_count: int
    normalized_changed_field_count: int
    normalized_emptied_field_count: int
    verified_count: int
    manual_only_count: int
    error_codes: Tuple[str, ...]
    duration_ms: int

    def __post_init__(self) -> None:
        object.__setattr__(self, "error_codes", tuple(self.error_codes))


@dataclass(frozen=True, slots=True)
class GateSourceV2:
    source_id: str
    attempted: bool
    candidate_count: int
    source_rejected_count: int
    duplicate_count: int
    normalized_changed_field_count: int
    normalized_emptied_field_count: int
    detail_quality: FrozenMap
    error_count: int
    error_codes: Tuple[str, ...]
    duration_ms: int

    def __post_init__(self) -> None:
        object.__setattr__(self, "detail_quality", _freeze(self.detail_quality))
        object.__setattr__(self, "error_codes", tuple(self.error_codes))

@dataclass(frozen=True, slots=True)
class GateSourceV4(GateSourceV2):
    """Gate V4 source row bound to a closed execution outcome."""

    outcome: Optional["SourceExecutionOutcomeV1"] = None


@dataclass(frozen=True, slots=True)
class GateV4:
    """Typed identity contract for a public Gate V4 record."""

    schema_version: int
    pipeline_schema_version: int
    status: str
    context_status: str
    source_outcomes: Tuple["SourceExecutionOutcomeV1", ...]



@dataclass(frozen=True, slots=True)
class SourceMetricV4(SourceMetricV2):
    """Pipeline V4 source metrics bound to one closed execution outcome."""

    outcome: Optional["SourceExecutionOutcomeV1"] = None


@dataclass(frozen=True, slots=True)
class PipelineResultV2:
    schema_version: int
    command_mode: str
    run_date: date
    all_assessments: Tuple[AssessmentV2, ...]
    source_metrics: Tuple[SourceMetricV2, ...]
    duplicates_removed: int
    collected_count: int
    source_rejected_count: int
    top_n: int
    manual_review_n: int

    def __post_init__(self) -> None:
        object.__setattr__(self, "all_assessments", tuple(self.all_assessments))
        object.__setattr__(self, "source_metrics", tuple(self.source_metrics))


@dataclass(frozen=True, slots=True)
class PipelineResultV4(PipelineResultV2):
    """Immutable pipeline V4 result with explicit closed source outcomes."""

    schema_version: int
    source_metrics: Tuple[SourceMetricV4, ...]
    source_outcomes: Tuple["SourceExecutionOutcomeV1", ...]

    def __post_init__(self) -> None:
        PipelineResultV2.__post_init__(self)
        object.__setattr__(self, "source_outcomes", tuple(self.source_outcomes))


@dataclass(frozen=True, slots=True)
class RenderedReportV2:
    schema_version: int
    markdown_bytes: bytes
    content_sha256: str
    byte_length: int

@dataclass(frozen=True, slots=True)
class ReportArtifactV2:
    schema_version: int
    generated: bool
    path: Optional[str]
    rendered: Optional[RenderedReportV2]


@dataclass(frozen=True, slots=True)
class PersistenceEnvelopeV3:
    schema_version: int
    run_identity: FrozenMap
    report_artifact: ReportArtifactV2
    gate_status: str
    context_status: str
    gate_json_sha256: Optional[str]
    summary: FrozenMap
    source_metrics: Tuple[GateSourceV2, ...]
    assessments: Tuple[AssessmentV2, ...]

    def __post_init__(self) -> None:
        object.__setattr__(self, "run_identity", _freeze(self.run_identity))
        object.__setattr__(self, "summary", _freeze(self.summary))
        object.__setattr__(self, "source_metrics", tuple(self.source_metrics))
        object.__setattr__(self, "assessments", tuple(self.assessments))
@dataclass(frozen=True, slots=True)
class SourceExecutionOutcomeV1:
    source_id: str
    attempted: bool
    completed: bool
    status: str
    error_code: Optional[str]
    elapsed_ms: int


def source_execution_outcome_v1_is_consistent(value: Any) -> bool:
    """Return whether an outcome uses only the closed V1 vocabulary."""

    status = getattr(value, "status", None)
    error_code = getattr(value, "error_code", None)
    attempted = getattr(value, "attempted", None)
    completed = getattr(value, "completed", None)
    elapsed_ms = getattr(value, "elapsed_ms", None)
    source_id = getattr(value, "source_id", None)
    if (
        type(source_id) is not str
        or not source_id
        or type(attempted) is not bool
        or type(completed) is not bool
        or type(elapsed_ms) is not int
        or elapsed_ms < 0
        or status not in SOURCE_EXECUTION_OUTCOME_STATUSES_V1
    ):
        return False
    if status == "success":
        return attempted and completed and error_code is None
    return attempted and not completed and error_code == status


@dataclass(frozen=True, slots=True)
class PersistenceEnvelopeV4:
    schema_version: int
    run_identity: FrozenMap
    report_artifact: ReportArtifactV2
    gate_status: str
    context_status: str
    gate_json_sha256: Optional[str]
    summary: FrozenMap
    source_metrics: Tuple[GateSourceV2, ...]
    assessments: Tuple[AssessmentV2, ...]
    source_outcomes: Tuple[SourceExecutionOutcomeV1, ...]

    def __post_init__(self) -> None:
        object.__setattr__(self, "run_identity", _freeze(self.run_identity))
        object.__setattr__(self, "summary", _freeze(self.summary))
        object.__setattr__(self, "source_metrics", tuple(self.source_metrics))
        object.__setattr__(self, "assessments", tuple(self.assessments))
        object.__setattr__(self, "source_outcomes", tuple(self.source_outcomes))


@dataclass(frozen=True, slots=True)
class Thresholds:
    apply: int = 75
    hold: int = 50


@dataclass(frozen=True, slots=True)
class ScoringWeights:
    # v2 dimensions are required/responsibilities/role/preferred/location.
    required: int = 40
    responsibilities: int = 20
    role: int = 20
    preferred: int = 10
    location: int = 10
    company: int = 0  # legacy scorer compatibility; excluded from v2 validation


@dataclass(frozen=True, slots=True)
class Profile:
    desired_roles: List[str]
    skills: List[str]
    preferred_locations: List[str]
    max_experience_years: int = 0
    exclusions: List[str] = field(default_factory=list)
    private_canaries: List[str] = field(default_factory=list)
    education_claim: Optional[str] = None

@dataclass(frozen=True, slots=True)
class UserContext:
    desired_roles: List[str]
    skills: List[str]
    preferred_locations: List[str]
    max_experience_years: int = 0
    explicit_deal_breakers: List[str] = field(default_factory=list)
    missing_context: List[str] = field(default_factory=list)
    provenance: Dict[str, str] = field(default_factory=dict)
    private_canaries: List[str] = field(default_factory=list)


@dataclass(frozen=True, slots=True)
class FeedbackEvent:
    posting_id: str
    verdict: str
    reason: str
    created_at: datetime
    movement: str = "same"


@dataclass(frozen=True, slots=True)
class RelevanceCase:
    case_id: str
    user_context: UserContext
    snapshot: "JDSnapshot"
    expected_verdict: str
    expected_movement: str = "same"
    rationale: str = ""


@dataclass(frozen=True, slots=True)
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


@dataclass(frozen=True, slots=True)
class AppConfig:
    top_n: int
    output_dir: Path
    fixture_path: Path
    delivery_mode: str
    thresholds: Thresholds
    scoring_weights: ScoringWeights
    profile: Profile
    user_context: UserContext
    sources: List[SourceManifest]
    manual_review_n: int = 5
    scoring_schema_version: int = 1

    @property
    def weights(self) -> ScoringWeights:
        """v2 spelling retained without breaking legacy callers."""
        return self.scoring_weights


@dataclass(frozen=True, slots=True)
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


@dataclass(frozen=True, slots=True)
class CollectionResultV1:
    """Collection-only coordinator result with closed, public outcomes."""

    candidates: Tuple[PostingCandidate, ...]
    detail_issues: Tuple[CandidateDetailIssueV2, ...]
    source_outcomes: Tuple[SourceExecutionOutcomeV1, ...]

    def __post_init__(self) -> None:
        object.__setattr__(self, "candidates", tuple(self.candidates))
        object.__setattr__(self, "detail_issues", tuple(self.detail_issues))
        object.__setattr__(self, "source_outcomes", tuple(self.source_outcomes))


@dataclass(frozen=True, slots=True)
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


@dataclass(frozen=True, slots=True)
class FitAssessment:
    snapshot: JDSnapshot
    score: int
    recommendation: str
    matched_evidence: List[str]
    gaps: List[str]
    risks: List[str]
    verification_questions: List[str]
    positioning_seed: str

    verdict: str = ""
    missing_context_signals: List[str] = field(default_factory=list)
    deal_breaker_hits: List[str] = field(default_factory=list)

@dataclass(frozen=True, slots=True)
class SourceRunMetric:
    source_id: str
    attempted: bool
    candidate_count: int
    error_count: int = 0
    errors: List[str] = field(default_factory=list)


@dataclass(frozen=True, slots=True)
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
    source_metrics: List[SourceRunMetric] = field(default_factory=list)
