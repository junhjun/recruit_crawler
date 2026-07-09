from __future__ import annotations

from datetime import datetime
from dataclasses import replace
import json
from pathlib import Path
from typing import Iterable, List, Mapping, Tuple, TypedDict, Union

from .schemas import AppConfig, FeedbackEvent, JDSnapshot, RelevanceCase, UserContext
from .scorer import score_snapshot
from .user_context import profile_from_context


class RelevanceCaseLoadError(ValueError):
    pass


JsonScalar = Union[str, int, float, bool, None]
JsonValue = Union[JsonScalar, list["JsonValue"], dict[str, "JsonValue"]]
JsonObject = Mapping[str, JsonValue]


class FeedbackRecordInput(TypedDict, total=False):
    posting_key: str
    recommendation_id: str
    posting_id: str
    verdict: str
    reason: str
    movement: str
    created_at: str


def load_relevance_cases(path: Path, base_config: AppConfig) -> List[RelevanceCase]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise RelevanceCaseLoadError(f"invalid relevance case fixture: {path}") from exc
    if not isinstance(payload, dict) or not isinstance(payload.get("cases"), list):
        raise RelevanceCaseLoadError("relevance case fixture requires a cases array")
    return [_case_from_mapping(item, base_config) for item in payload["cases"]]


def _json_mapping(value: JsonValue, message: str) -> JsonObject:
    if not isinstance(value, dict):
        raise RelevanceCaseLoadError(message)
    return value


def _json_list(value: JsonValue) -> list[JsonValue]:
    return value if isinstance(value, list) else []


def _json_object_items(value: JsonValue) -> JsonObject:
    return value if isinstance(value, dict) else {}


def _optional_int(value: JsonValue) -> int | None:
    return int(value) if value is not None else None


def _case_from_mapping(item: JsonValue, base_config: AppConfig) -> RelevanceCase:
    record = _json_mapping(item, "each relevance case must be an object")
    context = _context_from_mapping(record.get("context"), base_config)
    snapshot = _snapshot_from_mapping(record.get("snapshot"))
    return RelevanceCase(
        case_id=str(record["case_id"]),
        user_context=context,
        snapshot=snapshot,
        expected_verdict=str(record["expected_verdict"]),
        expected_movement=str(record.get("expected_movement", "same")),
        rationale=str(record.get("rationale", "")),
    )


def _context_from_mapping(value: JsonValue, base_config: AppConfig) -> UserContext:
    if value == "sample_config":
        return base_config.user_context
    record = _json_mapping(value, "case context must be an object or sample_config")
    provenance = _json_object_items(record.get("provenance"))
    return UserContext(
        desired_roles=[str(item) for item in _json_list(record.get("desired_roles"))],
        skills=[str(item) for item in _json_list(record.get("skills"))],
        preferred_locations=[str(item) for item in _json_list(record.get("preferred_locations"))],
        max_experience_years=int(record.get("max_experience_years", 0)),
        explicit_deal_breakers=[str(item) for item in _json_list(record.get("explicit_deal_breakers"))],
        missing_context=[str(item) for item in _json_list(record.get("missing_context"))],
        provenance={str(key): str(inner_value) for key, inner_value in provenance.items()},
        private_canaries=[str(item) for item in _json_list(record.get("private_canaries"))],
    )


def _snapshot_from_mapping(value: JsonValue) -> JDSnapshot:
    record = _json_mapping(value, "case snapshot must be an object")
    return JDSnapshot(
        source_id=str(record.get("source_id", "fixture")),
        source_url=str(record["source_url"]),
        source_posting_id=str(record["source_posting_id"]) if record.get("source_posting_id") is not None else None,
        title=str(record["title"]),
        company=str(record.get("company", "")),
        location=str(record.get("location", "")),
        deadline_raw=str(record["deadline_raw"]) if record.get("deadline_raw") is not None else None,
        deadline=None,
        deadline_uncertain=bool(record.get("deadline_uncertain", False)),
        required_qualifications=[str(item) for item in _json_list(record.get("required_qualifications"))],
        preferred_qualifications=[str(item) for item in _json_list(record.get("preferred_qualifications"))],
        responsibilities=[str(item) for item in _json_list(record.get("responsibilities"))],
        company_info=[str(item) for item in _json_list(record.get("company_info"))],
        minimum_experience_years=_optional_int(record.get("minimum_experience_years")),
        manual_review_flags=[str(item) for item in _json_list(record.get("manual_review_flags"))],
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


def feedback_events_from_records(records: Iterable[FeedbackRecordInput]) -> List[FeedbackEvent]:
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
