from __future__ import annotations

from dataclasses import replace
from typing import Iterable, List, Tuple

from .schemas import AppConfig, RelevanceCase
from .scorer import score_snapshot
from .user_context import profile_from_context


def movement_for_verdict(verdict: str) -> str:
    if verdict == "include":
        return "up"
    if verdict == "exclude":
        return "down"
    return "same"


def evaluate_relevance_case(case: RelevanceCase, base_config: AppConfig) -> Tuple[bool, str]:
    config = replace(base_config, profile=profile_from_context(case.user_context), user_context=case.user_context)
    assessment = score_snapshot(case.snapshot, config)
    actual_movement = movement_for_verdict(assessment.verdict)
    verdict_ok = assessment.verdict == case.expected_verdict
    movement_ok = actual_movement == case.expected_movement
    ok = verdict_ok and movement_ok
    message = (
        f"{case.case_id}: expected verdict {case.expected_verdict}, "
        f"got {assessment.verdict}; expected movement {case.expected_movement}, "
        f"got {actual_movement}"
    )
    return ok, message


def evaluate_relevance_cases(cases: Iterable[RelevanceCase], base_config: AppConfig) -> List[str]:
    failures: List[str] = []
    for case in cases:
        ok, message = evaluate_relevance_case(case, base_config)
        if not ok:
            failures.append(message)
    return failures
