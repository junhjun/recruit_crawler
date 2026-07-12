from __future__ import annotations

import json
import re
from typing import Dict, Final, List, Tuple, Union

from .model_context import ContextExtractionError, ModelContextExtraction
from .user_context import MAX_REASONABLE_EXPERIENCE_YEARS

JsonValue = Union[None, bool, int, float, str, List["JsonValue"], Dict[str, "JsonValue"]]
_EXPECTED_FIELDS = {"desired_roles", "skills", "preferred_locations", "max_experience_years", "explicit_deal_breakers", "confidence"}
_FIELD_LIMITS: Final[Dict[str, Tuple[int, int]]] = {"desired_roles": (20, 80), "skills": (80, 80), "preferred_locations": (20, 80), "explicit_deal_breakers": (30, 160)}
_MAX_VERBATIM_CHARS: Final = 32
_SENSITIVE_OUTPUT_PATTERNS: Final = (re.compile(r"\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b", re.I), re.compile(r"\bhttps?://\S+", re.I), re.compile(r"\b(?:\+?\d[\d .()-]{7,}\d)\b"), re.compile(r"\b(?:PRIVATE|RAW)_[A-Z0-9_]*CANARY\b", re.I))


def parse_model_context_json(response: str, *, source_text: str) -> ModelContextExtraction:
    try:
        payload: JsonValue = json.loads(response, object_pairs_hook=_strict_object)
    except json.JSONDecodeError as exc:
        raise ContextExtractionError("Codex thread response was not valid JSON") from exc
    if not isinstance(payload, dict) or set(payload) != _EXPECTED_FIELDS:
        raise ContextExtractionError("Codex thread response did not match the strict schema")
    desired_roles = _string_list(payload["desired_roles"], field="desired_roles", source_text=source_text)
    skills = _string_list(payload["skills"], field="skills", source_text=source_text)
    preferred_locations = _string_list(payload["preferred_locations"], field="preferred_locations", source_text=source_text)
    explicit_deal_breakers = _string_list(payload["explicit_deal_breakers"], field="explicit_deal_breakers", source_text=source_text)
    _reject_cross_field_passages(desired_roles + skills + preferred_locations + explicit_deal_breakers, source_text=source_text)
    max_experience_years = payload["max_experience_years"]
    confidence = payload["confidence"]
    if isinstance(max_experience_years, bool) or not isinstance(max_experience_years, int):
        raise ContextExtractionError("max_experience_years must be an integer")
    if not 0 <= max_experience_years <= MAX_REASONABLE_EXPERIENCE_YEARS:
        raise ContextExtractionError("max_experience_years was outside the supported range")
    if isinstance(confidence, bool) or not isinstance(confidence, (int, float)):
        raise ContextExtractionError("confidence must be a number")
    if not 0.0 <= float(confidence) <= 1.0:
        raise ContextExtractionError("confidence must be between 0 and 1")
    return ModelContextExtraction(desired_roles=desired_roles, skills=skills, preferred_locations=preferred_locations, max_experience_years=max_experience_years, explicit_deal_breakers=explicit_deal_breakers, confidence=float(confidence))


def _string_list(value: JsonValue, *, field: str, source_text: str) -> list[str]:
    if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
        raise ContextExtractionError(f"{field} must be an array of strings")
    max_items, max_chars = _FIELD_LIMITS[field]
    if len(value) > max_items:
        raise ContextExtractionError(f"{field} contained too many items")
    source_normalized = _passage_key(source_text)
    result: list[str] = []
    for item in value:
        normalized = " ".join(item.split())
        if not normalized or "\n" in item or "\r" in item or len(normalized) > max_chars:
            raise ContextExtractionError(f"{field} contained a non-atomic value")
        normalized_key = _passage_key(normalized)
        if len(normalized_key) >= _MAX_VERBATIM_CHARS and normalized_key in source_normalized:
            raise ContextExtractionError(f"{field} contained a verbatim source passage")
        if any(pattern.search(normalized) for pattern in _SENSITIVE_OUTPUT_PATTERNS):
            raise ContextExtractionError(f"{field} contained sensitive source data")
        result.append(normalized)
    combined = _passage_key(" ".join(result))
    if len(combined) >= _MAX_VERBATIM_CHARS and combined in source_normalized:
        raise ContextExtractionError(f"{field} contained a split verbatim source passage")
    return result


def _strict_object(pairs: List[Tuple[str, JsonValue]]) -> Dict[str, JsonValue]:
    result: Dict[str, JsonValue] = {}
    for key, value in pairs:
        if key in result:
            raise ContextExtractionError("Codex thread response contained duplicate JSON fields")
        result[key] = value
    return result


def _passage_key(value: str) -> str:
    return " ".join(re.sub(r"[^\w]+", " ", value.casefold()).split())


def _reject_cross_field_passages(values: List[str], *, source_text: str) -> None:
    source_key = _passage_key(source_text)
    matches = [(start, start + len(value_key)) for value in values if (value_key := _passage_key(value)) for start in range(len(source_key)) if source_key.startswith(value_key, start)]
    ends_by_start: Dict[int, List[int]] = {}
    for start, end in matches:
        ends_by_start.setdefault(start, []).append(end)
    for start, end in matches:
        pending = [(start, end, 1)]
        while pending:
            chain_start, chain_end, chain_items = pending.pop()
            if chain_items >= 2 and len(source_key[chain_start:chain_end]) >= _MAX_VERBATIM_CHARS:
                raise ContextExtractionError("model output contained a cross-field source passage")
            next_start = chain_end + 1 if source_key[chain_end : chain_end + 1] == " " else chain_end
            pending.extend((chain_start, next_end, chain_items + 1) for next_end in ends_by_start.get(next_start, []))
