from __future__ import annotations

from datetime import datetime
from dataclasses import replace
import json
from pathlib import Path
from typing import Any, Iterable, List, Mapping, Tuple

from .schemas import AppConfig, FeedbackEvent, JDSnapshot, RelevanceCase, UserContext
from .scorer import score_snapshot
from .user_context import profile_from_context


class RelevanceCaseLoadError(ValueError):
    pass


def load_relevance_cases(path: Path, base_config: AppConfig) -> List[RelevanceCase]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise RelevanceCaseLoadError(f"invalid relevance case fixture: {path}") from exc
    if not isinstance(payload, dict) or not isinstance(payload.get("cases"), list):
        raise RelevanceCaseLoadError("relevance case fixture requires a cases array")
    return [_case_from_mapping(item, base_config) for item in payload["cases"]]


def _case_from_mapping(item: Any, base_config: AppConfig) -> RelevanceCase:
    if not isinstance(item, dict):
        raise RelevanceCaseLoadError("each relevance case must be an object")
    context = _context_from_mapping(item.get("context"), base_config)
    snapshot = _snapshot_from_mapping(item.get("snapshot"))
    return RelevanceCase(
        case_id=str(item["case_id"]),
        user_context=context,
        snapshot=snapshot,
        expected_verdict=str(item["expected_verdict"]),
        expected_movement=str(item.get("expected_movement", "same")),
        rationale=str(item.get("rationale", "")),
    )


def _context_from_mapping(value: Any, base_config: AppConfig) -> UserContext:
    if value == "sample_config":
        return base_config.user_context
    if not isinstance(value, dict):
        raise RelevanceCaseLoadError("case context must be an object or sample_config")
    return UserContext(
        desired_roles=[str(item) for item in value.get("desired_roles", [])],
        skills=[str(item) for item in value.get("skills", [])],
        preferred_locations=[str(item) for item in value.get("preferred_locations", [])],
        max_experience_years=int(value.get("max_experience_years", 0)),
        explicit_deal_breakers=[str(item) for item in value.get("explicit_deal_breakers", [])],
        missing_context=[str(item) for item in value.get("missing_context", [])],
        provenance={str(key): str(inner_value) for key, inner_value in value.get("provenance", {}).items()},
        private_canaries=[str(item) for item in value.get("private_canaries", [])],
    )


def _snapshot_from_mapping(value: Any) -> JDSnapshot:
    if not isinstance(value, Mapping):
        raise RelevanceCaseLoadError("case snapshot must be an object")
    return JDSnapshot(
        source_id=str(value.get("source_id", "fixture")),
        source_url=str(value["source_url"]),
        source_posting_id=str(value["source_posting_id"]) if value.get("source_posting_id") is not None else None,
        title=str(value["title"]),
        company=str(value.get("company", "")),
        location=str(value.get("location", "")),
        deadline_raw=value.get("deadline_raw"),
        deadline=None,
        deadline_uncertain=bool(value.get("deadline_uncertain", False)),
        required_qualifications=[str(item) for item in value.get("required_qualifications", [])],
        preferred_qualifications=[str(item) for item in value.get("preferred_qualifications", [])],
        responsibilities=[str(item) for item in value.get("responsibilities", [])],
        company_info=[str(item) for item in value.get("company_info", [])],
        minimum_experience_years=value.get("minimum_experience_years"),
        manual_review_flags=[str(item) for item in value.get("manual_review_flags", [])],
    )


def movement_for_verdict(verdict: str) -> str:
    if verdict == "include":
        return "up"
    if verdict == "exclude":
        return "down"
    return "same"


def evaluate_relevance_case(
    case: RelevanceCase,
    base_config: AppConfig,
    feedback_events: Iterable[FeedbackEvent] = (),
) -> Tuple[bool, str]:
    config = replace(base_config, profile=profile_from_context(case.user_context), user_context=case.user_context)
    assessment = score_snapshot(case.snapshot, config)
    feedback_index = feedback_movement_index(feedback_events)
    actual_movement = feedback_index.get(posting_key_for_snapshot(case.snapshot), movement_for_verdict(assessment.verdict))
    verdict_ok = assessment.verdict == case.expected_verdict
    movement_ok = actual_movement == case.expected_movement
    ok = verdict_ok and movement_ok
    message = (
        f"{case.case_id}: expected verdict {case.expected_verdict}, "
        f"got {assessment.verdict}; expected movement {case.expected_movement}, "
        f"got {actual_movement}"
    )
    return ok, message


def evaluate_relevance_cases(
    cases: Iterable[RelevanceCase],
    base_config: AppConfig,
    feedback_events: Iterable[FeedbackEvent] = (),
) -> List[str]:
    feedback_list = list(feedback_events)
    failures: List[str] = []
    for case in cases:
        ok, message = evaluate_relevance_case(case, base_config, feedback_list)
        if not ok:
            failures.append(message)
    return failures


def feedback_events_from_records(records: Iterable[Mapping[str, Any]]) -> List[FeedbackEvent]:
    events: List[FeedbackEvent] = []
    for record in records:
        created_at = record.get("created_at")
        events.append(
            FeedbackEvent(
                posting_id=str(record.get("posting_key") or record.get("recommendation_id") or record.get("posting_id")),
                verdict=str(record["verdict"]),
                reason=str(record.get("reason", "")),
                movement=str(record.get("movement", "same")),
                created_at=datetime.fromisoformat(str(created_at)) if created_at else datetime.now(),
            )
        )
    return events


def posting_key_for_snapshot(snapshot: JDSnapshot) -> str:
    import hashlib

    payload = json.dumps(
        {
            "source_id": snapshot.source_id,
            "source_url": snapshot.source_url,
            "source_posting_id": snapshot.source_posting_id,
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:32]


def feedback_movement_index(events: Iterable[FeedbackEvent]) -> dict[str, str]:
    return {event.posting_id: event.movement for event in events}
