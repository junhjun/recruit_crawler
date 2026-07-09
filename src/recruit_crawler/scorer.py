from __future__ import annotations

from datetime import date
from typing import Iterable, List, Sequence

from .schemas import AppConfig, FitAssessment, JDSnapshot, Profile, UserContext
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
