from __future__ import annotations

import re
import unicodedata
from typing import Any, Iterable, Optional, Tuple

from .schemas import (
    AppConfig,
    EligibilityResult,
    EligibilityState,
    EvidenceConfidence,
    Profile,
    RequirementEvidence,
    RequirementKind,
    RequirementModality,
    RequirementOperator,
    SnapshotV2,
    UserContext,
)

# The order is part of the v2 contract.  Do not combine fields before scanning.
_SCAN_FIELDS = (
    ("title", "title"),
    ("required_qualifications", "required_qualifications"),
    ("preferred_qualifications", "preferred_qualifications"),
    ("responsibilities", "responsibilities"),
    ("company_info", "company_info"),
    ("experience_tags", "experience_tags"),
)
_SENTENCE_SPLIT = re.compile(r"(?:\r\n|\r|\n)+|(?<=[.!?。！？])\s+")

EDUCATION_ALIASES = {
    "high_school": r"(?:고등학교|high\s*school)(?:\s*(?:졸업|graduate))?",
    "associate": r"(?:전문(?:학사|대)|associate(?:'s)?|2\s*-?\s*year\s+college)",
    "bachelor": r"(?:학사|대학교(?:\s*졸업)?|bachelor(?:'s)?)",
    "master": r"(?:석사|master(?:'s)?)",
    "doctorate": r"(?:박사|ph\.?\s*d\.?|doctorate)",
}
_EDUCATION_RE = re.compile("|".join(f"(?P<{key}>{value})" for key, value in EDUCATION_ALIASES.items()), re.I)
_EDUCATION_AT_LEAST_RE = re.compile(r"(?:이상|또는\s*이상|or\s+higher|or\s+above|minimum)", re.I)
_EDUCATION_JOINER_RE = re.compile(r"(?:\s*(?:/|,|·|및|또는|or|and|~|–|-)\s*)", re.I)
_EXPERIENCE_RANGE_RE = re.compile(
    r"(?<!\d)(?P<min>\d{1,2})\s*(?:년|years?)\s*(?:~|–|-|to)\s*"
    r"(?P<max>\d{1,2})\s*(?:년|years?)(?!\w)", re.I
)
_EXPERIENCE_AT_LEAST_RE = re.compile(
    r"(?<!\d)(?P<min>\d{1,2})\s*(?:년|years?)(?:\s*(?:이상|or\s+more|\+|↑))?(?!\w)",
    re.I,
)
_MILITARY_RE = re.compile(
    r"(?:군필|군\s*미필|병역\s*(?:특례|사항|이행|필)|보충역|산업기능요원|전문연구요원|"
    r"military\s+(?:service|exemption))",
    re.I,
)
_PREFERRED_CUE_RE = re.compile(r"(?:우대|preferred|nice\s+to\s+have|plus)", re.I)
_MANDATORY_CUE_RE = re.compile(r"(?:필수|required|must|mandatory)", re.I)

_EDUCATION_RANK = {
    "unknown": 0,
    "high_school": 1,
    "associate": 2,
    "bachelor": 3,
    "master": 4,
    "doctorate": 5,
}


def _normalize_text(value: str) -> str:
    value = unicodedata.normalize("NFC", str(value))
    return " ".join(value.split()).strip()


def _modality(source_field: str, sentence: str) -> str:
    preferred_cue = source_field == "preferred_qualifications" or bool(_PREFERRED_CUE_RE.search(sentence))
    mandatory_cue = bool(_MANDATORY_CUE_RE.search(sentence))
    if preferred_cue and mandatory_cue:
        return RequirementModality.AMBIGUOUS.value
    return RequirementModality.PREFERRED.value if preferred_cue else RequirementModality.MANDATORY.value


def _education_evidence(source_field: str, sentence: str, item_index: int, sentence_index: int) -> Optional[RequirementEvidence]:
    aliases = list(_EDUCATION_RE.finditer(sentence))
    if not aliases:
        return None
    # A joined education expression ("bachelor or master") is deliberately
    # ambiguous; retain the sentence, but never invent a single claim.
    joined = len(aliases) > 1
    tail = sentence[aliases[0].end() :]
    joined = joined or (
        bool(_EDUCATION_JOINER_RE.match(tail)) and bool(_EDUCATION_RE.search(tail))
    )
    at_least = (
        not joined
        and bool(re.match(r"\s*(?:이상|또는\s*이상|or\s+higher|or\s+above|minimum)\b", tail, re.I))
    )
    modality = _modality(source_field, sentence)
    if joined or modality == RequirementModality.AMBIGUOUS.value:
        modality = RequirementModality.AMBIGUOUS.value
    return RequirementEvidence(
        kind=RequirementKind.EDUCATION.value,
        operator=RequirementOperator.AT_LEAST.value if at_least else RequirementOperator.NONE.value,
        modality=modality,
        source_field=source_field,
        item_index=item_index,
        sentence_index=sentence_index,
        text=_normalize_text(sentence),
        confidence=(
            EvidenceConfidence.UNCERTAIN.value
            if modality == RequirementModality.AMBIGUOUS.value or joined
            else EvidenceConfidence.HIGH.value
        ),
    )


def _experience_evidence(source_field: str, sentence: str, item_index: int, sentence_index: int) -> Optional[RequirementEvidence]:
    range_match = _EXPERIENCE_RANGE_RE.search(sentence)
    minimum: Optional[int] = None
    operator = RequirementOperator.AT_LEAST.value
    if range_match:
        low, high = int(range_match.group("min")), int(range_match.group("max"))
        if not 0 <= low <= high <= 50:
            return None
        minimum = low
        operator = RequirementOperator.RANGE.value
    if minimum is None:
        for match in _EXPERIENCE_AT_LEAST_RE.finditer(sentence):
            if range_match and match.start() < range_match.end() and match.end() > range_match.start():
                continue
            candidate = int(match.group("min"))
            if 0 <= candidate <= 50:
                minimum = candidate
                break
    if minimum is None:
        return None
    modality = _modality(source_field, sentence)
    return RequirementEvidence(
        kind=RequirementKind.EXPERIENCE.value,
        operator=operator,
        modality=modality,
        source_field=source_field,
        item_index=item_index,
        sentence_index=sentence_index,
        text=_normalize_text(sentence),
        confidence=(
            EvidenceConfidence.UNCERTAIN.value
            if modality == RequirementModality.AMBIGUOUS.value
            else EvidenceConfidence.HIGH.value
        ),
    )


def _military_evidence(source_field: str, sentence: str, item_index: int, sentence_index: int) -> Optional[RequirementEvidence]:
    if not _MILITARY_RE.search(sentence):
        return None
    return RequirementEvidence(
        kind=RequirementKind.MILITARY_PROGRAM.value,
        operator=RequirementOperator.NONE.value,
        modality=_modality(source_field, sentence),
        source_field=source_field,
        item_index=item_index,
        sentence_index=sentence_index,
        text=_normalize_text(sentence),
        confidence=EvidenceConfidence.HIGH.value,
    )


def extract_requirement_evidence(snapshot: SnapshotV2) -> Tuple[RequirementEvidence, ...]:
    """Extract requirements in canonical six-field/item/sentence scan order."""
    evidence = []
    for source_field, attribute in _SCAN_FIELDS:
        values = getattr(snapshot, attribute, ())
        if isinstance(values, str):
            values = (values,)
        for item_index, item in enumerate(values):
            for sentence_index, sentence in enumerate(_SENTENCE_SPLIT.split(str(item))):
                sentence = _normalize_text(sentence)
                if not sentence:
                    continue
                # Pattern order is normative: education, experience, military.
                for found in (
                    _education_evidence(source_field, sentence, item_index, sentence_index),
                    _experience_evidence(source_field, sentence, item_index, sentence_index),
                    _military_evidence(source_field, sentence, item_index, sentence_index),
                ):
                    if found is not None:
                        evidence.append(found)
    return tuple(evidence)


def _profile_from(value: Any) -> Profile:
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
        return _profile_from(profile)
    return Profile([], [], [], 0)


def evaluate_eligibility(
    snapshot: SnapshotV2,
    config_or_context: Any,
    evidence: Optional[Iterable[RequirementEvidence]] = None,
) -> Tuple[EligibilityResult, ...]:
    """Evaluate extracted requirements using config-only education/experience claims."""
    profile = _profile_from(config_or_context)
    records = tuple(evidence) if evidence is not None else extract_requirement_evidence(snapshot)
    results = []
    for index, item in enumerate(records):
        reason = ""
        provenance = "none"
        state = EligibilityState.REVIEW_REQUIRED.value
        if item.kind == RequirementKind.MILITARY_PROGRAM.value:
            reason = "military_program_review"
        elif item.modality == RequirementModality.AMBIGUOUS.value:
            reason = f"{item.kind}_ambiguous"
        elif item.kind == RequirementKind.EDUCATION.value:
            aliases = list(_EDUCATION_RE.finditer(item.text))
            level = aliases[0].lastgroup if aliases else None
            claim = getattr(profile, "education_claim", None)
            if claim in (None, "unknown") or level is None:
                state, reason = EligibilityState.USER_UNKNOWN.value, "education_unknown"
            elif _EDUCATION_RANK.get(str(claim), 0) >= _EDUCATION_RANK.get(level, 0):
                state, reason, provenance = EligibilityState.MATCH.value, "education_match", "config"
            else:
                state, reason, provenance = EligibilityState.MISMATCH.value, "education_mismatch", "config"
        elif item.kind == RequirementKind.EXPERIENCE.value:
            maximum = int(getattr(profile, "max_experience_years", 0) or 0)
            match = _EXPERIENCE_RANGE_RE.search(item.text) or _EXPERIENCE_AT_LEAST_RE.search(item.text)
            minimum = int(match.group("min")) if match else None
            if maximum <= 0 or minimum is None:
                state, reason = EligibilityState.USER_UNKNOWN.value, "experience_unknown"
            elif maximum >= minimum:
                state, reason, provenance = EligibilityState.MATCH.value, "experience_match", "config"
            else:
                state, reason, provenance = EligibilityState.MISMATCH.value, "experience_mismatch", "config"
        results.append(
            EligibilityResult(
                requirement_index=index,
                state=state,
                reason_code=reason,
                claim_provenance=provenance,
                evidence=(item,),
            )
        )
    return tuple(results)


# Explicit aliases keep the integration seam discoverable without duplicating logic.
extract_requirements = extract_requirement_evidence
extract_requirements_v2 = extract_requirement_evidence
evaluate_eligibility_v2 = evaluate_eligibility
assess_eligibility = evaluate_eligibility
requirements_for_snapshot = extract_requirement_evidence
