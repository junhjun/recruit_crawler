from __future__ import annotations

from datetime import date
from decimal import Decimal, ROUND_HALF_UP
import re
from typing import Any, Iterable, List, Mapping, Optional, Sequence, Tuple

from .schemas import (
    AppConfig,
    AssessmentV2,
    EligibilityResult,
    EligibilityState,
    FitAssessment,
    JDSnapshot,
    Profile,
    RequirementKind,
    ScoreBreakdownV2,
    SnapshotV2,
    UserContext,
)
from .user_context import missing_context_fields, profile_from_context


def is_expired(snapshot: JDSnapshot, run_date: date) -> bool:
    return snapshot.deadline is not None and snapshot.deadline < run_date


def exceeds_experience_limit(snapshot: JDSnapshot, config: AppConfig) -> bool:
    minimum = snapshot.minimum_experience_years
    if minimum is None:
        return False
    return minimum > config.user_context.max_experience_years


def _contains_skill(text: str, skill: str) -> bool:
    return skill.lower() in text.lower()


def _match_terms(items: Sequence[str], profile: Profile) -> List[str]:
    matches: List[str] = []
    for item in items:
        for skill in profile.skills:
            if _contains_skill(item, skill):
                matches.append(item)
                break
    return matches


def _location_matches(location: str, preferred_locations: Sequence[str]) -> bool:
    normalized_location = location.lower()
    for preferred in preferred_locations:
        normalized_preferred = preferred.lower()
        if normalized_preferred in normalized_location:
            return True
        if normalized_preferred == "seoul" and "서울" in location:
            return True
        if normalized_preferred == "remote" and "재택" in location:
            return True
    return False


def _role_matches(title: str, desired_roles: Sequence[str]) -> bool:
    normalized_title = title.lower()
    for role in desired_roles:
        normalized_role = role.lower().strip()
        if not normalized_role:
            continue
        if normalized_role in normalized_title:
            return True
        tokens = [token for token in normalized_role.replace("/", " ").split() if token]
        if tokens and all(token in normalized_title for token in tokens):
            return True
    return False


def _ratio(matches: Sequence[str], total: int) -> float:
    if total == 0:
        return 0.5
    return len(matches) / total


def _verdict_from_recommendation(recommendation: str) -> str:
    if recommendation == "apply":
        return "include"
    if recommendation == "hold":
        return "hold"
    return "exclude"


def _snapshot_text(snapshot: JDSnapshot) -> str:
    return " ".join(
        [
            snapshot.title,
            snapshot.company,
            snapshot.location,
            *snapshot.required_qualifications,
            *snapshot.preferred_qualifications,
            *snapshot.responsibilities,
            *snapshot.company_info,
            *snapshot.manual_review_flags,
        ]
    ).lower()


def deal_breaker_hits(snapshot: JDSnapshot, context: UserContext) -> List[str]:
    text = _snapshot_text(snapshot)
    return [item for item in context.explicit_deal_breakers if item and item.lower() in text]

def _recommendation(score: int, config: AppConfig) -> str:
    if score >= config.thresholds.apply:
        return "apply"
    if score >= config.thresholds.hold:
        return "hold"
    return "low_priority"


def score_snapshot(snapshot: JDSnapshot, config: AppConfig) -> FitAssessment:
    context = config.user_context
    profile = profile_from_context(context)
    required_matches = _match_terms(snapshot.required_qualifications, profile)
    preferred_matches = _match_terms(snapshot.preferred_qualifications, profile)
    responsibility_matches = _match_terms(snapshot.responsibilities, profile)
    company_matches = _match_terms(snapshot.company_info, profile)
    location_match = _location_matches(snapshot.location, profile.preferred_locations)
    role_match = _role_matches(snapshot.title, profile.desired_roles)
    missing_context_signals = missing_context_fields(context)
    deal_breakers = deal_breaker_hits(snapshot, context)

    weights = config.scoring_weights
    score = round(
        weights.required * _ratio(required_matches, len(snapshot.required_qualifications))
        + weights.preferred * _ratio(preferred_matches, len(snapshot.preferred_qualifications))
        + weights.responsibilities * _ratio(responsibility_matches, len(snapshot.responsibilities))
        + weights.company * _ratio(company_matches, len(snapshot.company_info))
        + (weights.location if location_match else 0)
        + (5 if role_match else 0)
        - (10 if profile.desired_roles and not role_match else 0)
    )
    score = max(0, min(100, int(score)))

    matched_evidence = []
    for label, matches in (
        ("필수 요건", required_matches),
        ("우대 요건", preferred_matches),
        ("담당 업무", responsibility_matches),
        ("회사 정보", company_matches),
    ):
        matched_evidence.extend(f"{label}: {match}" for match in matches[:3])
    if location_match:
        matched_evidence.append(f"근무지: {snapshot.location}")
    if role_match:
        matched_evidence.append(f"선호 직무: {snapshot.title}")
    if not matched_evidence:
        matched_evidence.append("구조화된 항목에서 강한 프로필 매칭 신호가 없습니다")

    gaps = [
        item
        for item in snapshot.required_qualifications
        if item not in required_matches
    ][:3]
    risks: List[str] = []
    risks.extend(snapshot.manual_review_flags)
    if snapshot.deadline_uncertain:
        risks.append("마감일이 없거나 해석되지 않습니다. 지원 전 확인이 필요합니다")
    if gaps:
        risks.append("필수 요건 공백이 서류 평가에 영향을 줄 수 있습니다")
    if snapshot.location and not location_match:
        risks.append(f"선호 근무지가 아닙니다: {snapshot.location}")
    if profile.desired_roles and not role_match:
        risks.append("선호 직무명과 직접 일치하지 않습니다: " + ", ".join(profile.desired_roles[:3]))

    if missing_context_signals:
        risks.append("사용자 맥락이 부족해 보류 판단이 필요합니다: " + ", ".join(missing_context_signals))
    if deal_breakers:
        risks.append("명시적 제외 조건과 충돌합니다: " + ", ".join(deal_breakers))
    verification_questions = []
    if snapshot.deadline_uncertain:
        verification_questions.append("정확한 지원 마감일은 언제인가요?")
    if gaps:
        verification_questions.append("부족한 필수 요건이 대체 경험으로 보완 가능한가요?")
    if snapshot.manual_review_flags:
        verification_questions.append("본문 이미지/OCR 필요 상태를 수동 검토했나요?")
    if not verification_questions:
        verification_questions.append("채용팀이 매칭된 기술을 서류 평가에서 중요하게 보나요?")

    positioning_seed = (
        f"{', '.join(matched_evidence[:2])} 중심으로 지원 포지셔닝"
        if matched_evidence
        else "가장 가까운 관련 프로젝트 경험 중심으로 지원 포지셔닝"
    )

    recommendation = _recommendation(score, config)
    verdict = _verdict_from_recommendation(recommendation)
    if missing_context_signals and verdict == "exclude":
        recommendation = "hold"
        verdict = "hold"
    if deal_breakers:
        recommendation = "low_priority"
        verdict = "exclude"

    return FitAssessment(
        snapshot=snapshot,
        score=score,
        recommendation=recommendation,
        matched_evidence=matched_evidence,
        gaps=gaps,
        risks=risks or ["구조화된 항목 기준 큰 위험 신호는 없습니다"],
        verification_questions=verification_questions,
        positioning_seed=positioning_seed,
        verdict=verdict,
        missing_context_signals=missing_context_signals,
        deal_breaker_hits=deal_breakers,
    )


def rank_snapshots(snapshots: Iterable[JDSnapshot], config: AppConfig) -> List[FitAssessment]:
    assessments = [score_snapshot(snapshot, config) for snapshot in snapshots]
    return sorted(
        assessments,
        key=lambda item: (
            item.score,
            1 if item.snapshot.deadline_uncertain else 0,
            item.snapshot.title,
        ),
        reverse=True,
    )
_V2_WEIGHTS = {
    "required": Decimal("40"),
    "responsibilities": Decimal("20"),
    "role": Decimal("20"),
    "preferred": Decimal("10"),
    "location": Decimal("10"),
}
_EXPERIENCE_NUMBER_RE = re.compile(
    r"(?<!\d)(?P<minimum>\d{1,2})\s*(?:년|years?)(?:\s*(?:이상|or\s+more|\+|↑))?(?!\w)",
    re.IGNORECASE,
)
_EXPERIENCE_RANGE_NUMBER_RE = re.compile(
    r"(?<!\d)(?P<minimum>\d{1,2})\s*(?:년|years?)?\s*(?:~|–|-|to)\s*"
    r"\d{1,2}\s*(?:년|years?)(?:\s*(?:이상|or\s+more|\+|↑))?(?!\w)",
    re.IGNORECASE,
)
_MILITARY_PUBLIC_RE = re.compile(
    r"(?:군\s*(?:필|미필|복무|면제)|군대|군사|병역|대체복무|보충역|산업기능요원|전문연구요원|"
    r"현역|예비역|military|army|veteran)",
    re.IGNORECASE,
)


def _v2_profile(value: Any) -> Profile:
    if isinstance(value, AppConfig):
        return value.profile
    if isinstance(value, Profile):
        return value
    if isinstance(value, UserContext):
        return Profile(
            desired_roles=list(value.desired_roles),
            skills=list(value.skills),
            preferred_locations=list(value.preferred_locations),
            max_experience_years=value.max_experience_years,
            exclusions=list(value.explicit_deal_breakers),
            private_canaries=list(value.private_canaries),
        )
    profile = getattr(value, "profile", None)
    if profile is not None:
        return _v2_profile(profile)
    return Profile([], [], [], 0)
def _mandatory_experience_gap(
    eligibility: Sequence[EligibilityResult],
    profile: Profile,
) -> int:
    """Return the largest positive gap from numeric mandatory requirements."""
    maximum = int(getattr(profile, "max_experience_years", 0) or 0)
    if maximum < 0:
        return 0
    gaps: list[int] = []
    for result in eligibility:
        if result.state != EligibilityState.MISMATCH.value and not (
            maximum == 0 and result.state == EligibilityState.USER_UNKNOWN.value
        ):
            continue
        for evidence in result.evidence:
            if (
                evidence.kind != RequirementKind.EXPERIENCE.value
                or evidence.modality != "mandatory"
                or evidence.confidence != "high"
            ):
                continue
            match = _EXPERIENCE_RANGE_NUMBER_RE.search(evidence.text)
            if match is None:
                match = _EXPERIENCE_NUMBER_RE.search(evidence.text)
            if match is not None:
                gaps.append(max(0, int(match.group("minimum")) - maximum))
    return max(gaps, default=0)

def mandatory_experience_gap(
    eligibility: Sequence[EligibilityResult],
    config_or_context: Any,
) -> int:
    """Public deterministic helper for the V3 mandatory experience rule."""
    return _mandatory_experience_gap(tuple(eligibility), _v2_profile(config_or_context))




def _v2_normalize(value: str) -> str:
    return " ".join(str(value).casefold().split()).strip()


def _v2_excluded_items(
    eligibility: Sequence[EligibilityResult],
) -> Mapping[str, frozenset[int]]:
    excluded = {}
    for result in eligibility:
        for evidence in result.evidence:
            if evidence.kind in {
                RequirementKind.EDUCATION.value,
                RequirementKind.EXPERIENCE.value,
                RequirementKind.MILITARY_PROGRAM.value,
            }:
                excluded.setdefault(evidence.source_field, set()).add(evidence.item_index)
    return {field: frozenset(indices) for field, indices in excluded.items()}


def _v2_skill_matches(items: Sequence[str], field: str, skills: Sequence[str], excluded: Mapping[str, frozenset[int]]) -> Tuple[int, int, List[str]]:
    ignored = excluded.get(field, frozenset())
    usable = [(index, item) for index, item in enumerate(items) if index not in ignored]
    normalized_skills = tuple(_v2_normalize(skill) for skill in skills if _v2_normalize(skill))
    matches = [
        item for _, item in usable
        if any(skill in _v2_normalize(item) for skill in normalized_skills)
    ]
    return len(matches), len(usable), matches


def _v2_role_match(title: str, roles: Sequence[str]) -> bool:
    normalized_title = _v2_normalize(title)
    for role in roles:
        normalized_role = _v2_normalize(role)
        if not normalized_role:
            continue
        if normalized_role in normalized_title:
            return True
        tokens = [token for token in normalized_role.split("/") if token.strip()]
        if tokens and all(token.strip() in normalized_title for token in tokens):
            return True
    return False


def _v2_location_match(location: str, locations: Sequence[str]) -> bool:
    normalized_location = _v2_normalize(location)
    for preferred in locations:
        normalized = _v2_normalize(preferred)
        if not normalized:
            continue
        if normalized in normalized_location:
            return True
        if normalized == "seoul" and "서울" in location:
            return True
        if normalized == "remote" and "재택" in location:
            return True
    return False


def _v2_component(weight: Decimal, numerator: int, denominator: int) -> Tuple[int, Decimal]:
    if denominator <= 0:
        return 0, Decimal("0")
    exact = weight * Decimal(numerator) / Decimal(denominator)
    return int(exact.quantize(Decimal("1"), rounding=ROUND_HALF_UP)), exact


def score_snapshot_v2(
    snapshot: SnapshotV2,
    eligibility: Optional[Sequence[EligibilityResult]] = None,
    config_or_context: Any = None,
    *,
    config: Any = None,
    context: Any = None,
) -> ScoreBreakdownV2:
    """Return a deterministic v2 score; eligibility items are excluded from skill dimensions."""
    # Accept both natural integration orders: (snapshot, eligibility, config) and
    # (snapshot, config, eligibility), while keeping the operation pure.
    if isinstance(eligibility, (AppConfig, Profile, UserContext)):
        eligibility, config_or_context = config_or_context, eligibility
    if config is not None:
        config_or_context = config
    if context is not None:
        config_or_context = context
    records = tuple(eligibility or ())
    profile = _v2_profile(config_or_context)
    excluded = _v2_excluded_items(records)
    req_num, req_den, _ = _v2_skill_matches(
        snapshot.required_qualifications, "required_qualifications", profile.skills, excluded
    )
    resp_num, resp_den, _ = _v2_skill_matches(
        snapshot.responsibilities, "responsibilities", profile.skills, excluded
    )
    pref_num, pref_den, _ = _v2_skill_matches(
        snapshot.preferred_qualifications, "preferred_qualifications", profile.skills, excluded
    )
    role_den = 1 if profile.desired_roles else 0
    role_num = int(_v2_role_match(snapshot.title, profile.desired_roles)) if role_den else 0
    location_den = 1 if profile.preferred_locations else 0
    location_num = int(_v2_location_match(snapshot.location, profile.preferred_locations)) if location_den else 0

    req_score, req_exact = _v2_component(_V2_WEIGHTS["required"], req_num, req_den)
    resp_score, resp_exact = _v2_component(_V2_WEIGHTS["responsibilities"], resp_num, resp_den)
    role_score, role_exact = _v2_component(_V2_WEIGHTS["role"], role_num, role_den)
    pref_score, pref_exact = _v2_component(_V2_WEIGHTS["preferred"], pref_num, pref_den)
    loc_score, loc_exact = _v2_component(_V2_WEIGHTS["location"], location_num, location_den)
    raw_exact = req_exact + resp_exact + role_exact + pref_exact + loc_exact
    raw_score = int(raw_exact.quantize(Decimal("1"), rounding=ROUND_HALF_UP))
    gap = _mandatory_experience_gap(records, profile)
    score = max(0, min(100, raw_score - (10 if gap == 1 else 0)))
    return ScoreBreakdownV2(
        required_numerator=req_num,
        required_denominator=req_den,
        required_score=req_score,
        responsibilities_numerator=resp_num,
        responsibilities_denominator=resp_den,
        responsibilities_score=resp_score,
        role_numerator=role_num,
        role_denominator=role_den,
        role_score=role_score,
        preferred_numerator=pref_num,
        preferred_denominator=pref_den,
        preferred_score=pref_score,
        location_numerator=location_num,
        location_denominator=location_den,
        location_score=loc_score,
        raw_score=raw_score,
        score=score,
    )


def _v2_public_item(item: str, profile: Profile) -> bool:
    normalized = _v2_normalize(item)
    if not normalized or _MILITARY_PUBLIC_RE.search(item):
        return False
    return not any(_v2_normalize(canary) and _v2_normalize(canary) in normalized for canary in profile.private_canaries)


def _v2_matched_evidence(snapshot: SnapshotV2, profile: Profile, eligibility: Sequence[EligibilityResult]) -> Tuple[str, ...]:
    excluded = _v2_excluded_items(eligibility)
    evidence: List[str] = []
    for label, field, items in (
        ("필수 요건", "required_qualifications", snapshot.required_qualifications),
        ("담당 업무", "responsibilities", snapshot.responsibilities),
        ("우대 요건", "preferred_qualifications", snapshot.preferred_qualifications),
    ):
        _, _, matches = _v2_skill_matches(items, field, profile.skills, excluded)
        evidence.extend(
            f"{label}: {item}"
            for item in matches[:3]
            if _v2_public_item(item, profile)
        )
    if profile.desired_roles and _v2_role_match(snapshot.title, profile.desired_roles) and _v2_public_item(snapshot.title, profile):
        evidence.append(f"선호 직무: {snapshot.title}")
    if profile.preferred_locations and _v2_location_match(snapshot.location, profile.preferred_locations) and _v2_public_item(snapshot.location, profile):
        evidence.append(f"근무지: {snapshot.location}")
    return tuple(evidence)


def _v2_reason_codes(
    snapshot: SnapshotV2,
    eligibility: Sequence[EligibilityResult],
    profile: Profile,
    run_date: date,
) -> Tuple[str, ...]:
    codes: List[str] = []
    if snapshot.detail_quality == "rejected" or not snapshot.title.strip() or not snapshot.source_id.strip():
        codes.append("invalid_candidate")
    if snapshot.deadline is not None and snapshot.deadline < run_date:
        codes.append("expired")
    searchable = " ".join(
        (
            snapshot.title,
            snapshot.company,
            snapshot.location,
            *snapshot.required_qualifications,
            *snapshot.preferred_qualifications,
            *snapshot.responsibilities,
            *snapshot.company_info,
            *snapshot.experience_tags,
        )
    )
    if any(
        _v2_normalize(term) and _v2_normalize(term) in _v2_normalize(searchable)
        for term in getattr(profile, "exclusions", ())
    ):
        codes.append("dealbreaker")
    mismatches = [
        item.reason_code
        for item in eligibility
        if item.state == EligibilityState.MISMATCH.value
        and any(evidence.modality != "preferred" for evidence in item.evidence)
    ]
    codes.extend(dict.fromkeys(mismatches))
    if snapshot.manual_review_flags:
        codes.append("manual_flag")
    if snapshot.detail_quality == "manual_only":
        codes.append("manual_source")
    priority = (
        "education_ambiguous",
        "experience_ambiguous",
        "military_program_review",
        "education_unknown",
        "experience_unknown",
    )
    found = {item.reason_code for item in eligibility}
    codes.extend(code for code in priority if code in found)
    return tuple(dict.fromkeys(codes))


def assess_snapshot_v2(
    snapshot: SnapshotV2,
    eligibility: Optional[Sequence[EligibilityResult]] = None,
    config_or_context: Any = None,
    *,
    recommendation_id: str = "",
    posting_key: str = "",
    opaque_identity: str = "",
    run_date: Optional[date] = None,
    config: Any = None,
    context: Any = None,
) -> AssessmentV2:
    """Build a terminal v2 assessment without persisting raw or private source material."""
    if isinstance(eligibility, (AppConfig, Profile, UserContext)):
        eligibility, config_or_context = config_or_context, eligibility
    if config is not None:
        config_or_context = config
    if context is not None:
        config_or_context = context
    from .eligibility import evaluate_eligibility

    records = tuple(evaluate_eligibility(snapshot, config_or_context) if eligibility is None else eligibility)
    profile = _v2_profile(config_or_context)
    breakdown = score_snapshot_v2(snapshot, records, config_or_context)
    today = run_date or date.today()
    reasons = _v2_reason_codes(snapshot, records, profile, today)
    thresholds = getattr(config_or_context, "thresholds", None)
    apply_at = int(getattr(thresholds, "apply", 75))
    hold_at = int(getattr(thresholds, "hold", 50))
    experience_gap = mandatory_experience_gap(records, profile)
    if "invalid_candidate" in reasons:
        disposition = "exclude"
    elif experience_gap > 0:
        # Owner-confirmed numeric gaps are always actionable and never excluded.
        disposition = "hold"
    elif "expired" in reasons:
        disposition = "expired"
    elif "dealbreaker" in reasons:
        disposition = "exclude"
    elif any(code in reasons for code in (
        "manual_flag", "manual_source", "education_ambiguous", "experience_ambiguous",
        "military_program_review", "education_unknown", "experience_unknown",
    )):
        disposition = "manual_review"
    elif breakdown.score >= apply_at:
        disposition = "apply"
    elif breakdown.score >= hold_at:
        disposition = "hold"
    else:
        disposition = "low_priority"
    safe_flags = tuple(
        flag for flag in snapshot.manual_review_flags
        if _v2_public_item(flag, profile)
    )
    return AssessmentV2(
        recommendation_id=recommendation_id,
        posting_key=posting_key,
        source_id=snapshot.source_id,
        source_url=snapshot.canonical_url,
        source_posting_id=snapshot.source_posting_id,
        title=snapshot.title,
        company=snapshot.company,
        location=snapshot.location,
        deadline=snapshot.deadline,
        deadline_uncertain=snapshot.deadline_uncertain,
        score=breakdown.score,
        score_breakdown=breakdown,
        disposition=disposition,
        reason_codes=reasons,
        detail_quality=snapshot.detail_quality,
        matched_evidence=_v2_matched_evidence(snapshot, profile, records),
        eligibility=records,
        manual_review_flags=safe_flags,
        opaque_identity=opaque_identity,
    )


# Integration aliases retained as pure v2 entry points.
score_v2 = score_snapshot_v2
assess_v2 = assess_snapshot_v2
rank_snapshot_v2 = assess_snapshot_v2
score_assessment_v2 = assess_snapshot_v2
