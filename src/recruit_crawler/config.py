from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path
from typing import TYPE_CHECKING, Any, Dict, Optional, Sequence

from .schemas import AppConfig, Profile, ScoringWeights, SourceManifest, Thresholds
from .user_context import (
    context_from_profile,
    merge_supplemental_answers,
    merge_user_contexts,
    parse_context_document,
    profile_from_context,
)
from .source_registry import SourceRegistryError, validate_source_registry

if TYPE_CHECKING:
    from .model_context import ContextExtractionCache, ContextExtractor


class ConfigError(ValueError):
    pass
EDUCATION_CLAIMS = frozenset({"unknown", "high_school", "associate", "bachelor", "master", "doctorate"})


def _as_int(value: Any, label: str) -> int:
    if isinstance(value, bool):
        raise ConfigError(f"{label} must be an integer")
    try:
        return int(value)
    except (TypeError, ValueError) as exc:
        raise ConfigError(f"{label} must be an integer") from exc


def _require_mapping(data: Any, label: str) -> Dict[str, Any]:
    if not isinstance(data, dict):
        raise ConfigError(f"{label} must be an object")
    return data
def _validate_saramin_acquisition(source: Dict[str, Any]) -> None:
    source_id = str(source.get("source_id", "")).strip()
    options = source.get("options", {})
    if not isinstance(options, dict):
        raise ConfigError(f"{source_id or 'source'} options must be an object")
    has_strategy_fields = "acquisition_strategy" in options or "outer_strategy_approval" in options
    if source_id.casefold() != "saramin":
        if has_strategy_fields:
            raise ConfigError("acquisition_strategy is only supported for 사람인")
        return
    enabled_public = bool(source.get("enabled", False)) and str(source.get("access_mode", "")) in {
        "public_page",
        "feed",
    }
    if not enabled_public:
        return
    strategy = options.get("acquisition_strategy")
    approval = options.get("outer_strategy_approval")
    if not isinstance(strategy, str) or strategy not in {"detail_only", "outer_only"}:
        raise ConfigError(
            "사람인 enabled public source requires acquisition_strategy detail_only or outer_only"
        )
    if not isinstance(approval, str) or approval not in {"not_probed", "approved"}:
        raise ConfigError(
            "사람인 enabled public source requires outer_strategy_approval not_probed or approved"
        )
    expected_approval = "not_probed" if strategy == "detail_only" else "approved"
    if approval != expected_approval:
        raise ConfigError(
            f"사람인 acquisition strategy {strategy} requires outer_strategy_approval {expected_approval}"
        )
    if "detail_urls" in options and "outer_urls" in options:
        raise ConfigError("사람인 acquisition config cannot define both detail_urls and outer_urls")




def load_config(path: Path, *, allow_real_sources: bool = False) -> AppConfig:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ConfigError(f"invalid JSON config: {exc}") from exc

    data = _require_mapping(raw, "config")
    top_n = _as_int(data.get("top_n", 5), "top_n")
    if top_n <= 0:
        raise ConfigError("top_n must be greater than zero")
    manual_review_n = _as_int(data.get("manual_review_n", 5), "manual_review_n")
    if manual_review_n < 0:
        raise ConfigError("manual_review_n must be non-negative")

    thresholds_raw = _require_mapping(data.get("thresholds", {}), "thresholds")
    thresholds = Thresholds(
        apply=_as_int(thresholds_raw.get("apply", 75), "thresholds.apply"),
        hold=_as_int(thresholds_raw.get("hold", 50), "thresholds.hold"),
    )
    if thresholds.apply <= thresholds.hold:
        raise ConfigError("thresholds.apply must be greater than thresholds.hold")

    schema_value = data.get("scoring_schema_version")
    if schema_value is None:
        scoring_schema_version = 2 if "weights" in data else 1
    else:
        scoring_schema_version = _as_int(schema_value, "scoring_schema_version")
    if scoring_schema_version not in {1, 2}:
        raise ConfigError("scoring_schema_version must be 1 or 2")

    weights_key = "weights" if "weights" in data else "scoring_weights"
    weights_raw = _require_mapping(data.get(weights_key, {}), weights_key)
    if scoring_schema_version == 2:
        expected_weight_keys = {"required", "responsibilities", "role", "preferred", "location"}
        actual_weight_keys = set(weights_raw)
        missing = sorted(expected_weight_keys - actual_weight_keys)
        unknown = sorted(actual_weight_keys - expected_weight_keys)
        if missing or unknown:
            details = []
            if missing:
                details.append("missing: " + ", ".join(missing))
            if unknown:
                details.append("unknown: " + ", ".join(unknown))
            raise ConfigError("v2 weights keys are invalid (" + "; ".join(details) + ")")
        v2_values = {
            key: _as_int(weights_raw[key], f"{weights_key}.{key}")
            for key in expected_weight_keys
        }
        if any(value < 0 for value in v2_values.values()):
            raise ConfigError("v2 weights values must be non-negative")
        if sum(v2_values.values()) != 100:
            raise ConfigError("v2 weights must total 100")
        weights = ScoringWeights(
            required=v2_values["required"],
            responsibilities=v2_values["responsibilities"],
            role=v2_values["role"],
            preferred=v2_values["preferred"],
            location=v2_values["location"],
            company=0,
        )
    else:
        weights = ScoringWeights(
            required=_as_int(weights_raw.get("required", 45), f"{weights_key}.required"),
            preferred=_as_int(weights_raw.get("preferred", 20), f"{weights_key}.preferred"),
            responsibilities=_as_int(
                weights_raw.get("responsibilities", 15), f"{weights_key}.responsibilities"
            ),
            company=_as_int(weights_raw.get("company", 10), f"{weights_key}.company"),
            location=_as_int(weights_raw.get("location", 10), f"{weights_key}.location"),
            role=0,
        )
        weight_values = (
            weights.required,
            weights.preferred,
            weights.responsibilities,
            weights.company,
            weights.location,
        )
        if any(value < 0 for value in weight_values):
            raise ConfigError("scoring_weights values must be non-negative")
        if sum(weight_values) <= 0:
            raise ConfigError("scoring_weights must include at least one positive value")

    profile_raw = _require_mapping(data.get("profile", {}), "profile")
    advanced_raw = _require_mapping(data.get("advanced", {}), "advanced")
    education_claim = advanced_raw.get("education_claim")
    if education_claim is not None:
        education_claim = str(education_claim)
        if education_claim not in EDUCATION_CLAIMS:
            raise ConfigError(
                "advanced.education_claim must be one of: " + ", ".join(sorted(EDUCATION_CLAIMS))
            )
    profile = Profile(
        desired_roles=list(profile_raw.get("desired_roles", [])),
        skills=list(profile_raw.get("skills", [])),
        preferred_locations=list(profile_raw.get("preferred_locations", [])),
        max_experience_years=_as_int(
            profile_raw.get("max_experience_years", 0), "profile.max_experience_years"
        ),
        exclusions=list(profile_raw.get("exclusions", [])),
        private_canaries=list(profile_raw.get("private_canaries", [])),
        education_claim=education_claim,
    )

    sources_raw = data.get("sources", [])
    if not isinstance(sources_raw, list):
        raise ConfigError("sources must be an array")
    if any(not isinstance(source, dict) for source in sources_raw):
        raise ConfigError("each source must be an object")
    has_registry_fields = any(
        "target_status" in source
        or "target_lane" in source
        or str(source.get("source_id", "")).casefold() == "linkedin"
        for source in sources_raw
    )
    sources = []
    for source in sources_raw:
        _validate_saramin_acquisition(source)
        target_lane = source.get("target_lane")
        sources.append(
            SourceManifest(
                source_id=str(source["source_id"]),
                enabled=bool(source.get("enabled", False)),
                access_mode=str(source.get("access_mode", "")),
                auth_required=bool(source.get("auth_required", False)),
                tos_review_status=str(source.get("tos_review_status", "unknown")),
                domains=list(source.get("domains", [])),
                rate_limit=str(source.get("rate_limit", "")),
                failure_mode=str(source.get("failure_mode", "skip_source")),
                allowed_persisted_fields=list(source.get("allowed_persisted_fields", [])),
                display_name=str(source.get("display_name", source["source_id"])),
                v1_role=str(source.get("v1_role", "")),
                target_status=str(source.get("target_status", "enabled" if source.get("enabled", False) else "deferred")),
                maintenance_status=str(source.get("maintenance_status", "active" if source.get("enabled", False) else "watch")),
                target_lane=None if target_lane is None else str(target_lane),
                candidate_lanes=list(source.get("candidate_lanes", [])),
                automation_level=str(source.get("automation_level", "no_human" if source.get("enabled", False) else "unknown")),
                status_reason=str(source.get("status_reason", "")),
                evidence=list(source.get("evidence", [])),
                blockers=list(source.get("blockers", [])),
                next_action=str(source.get("next_action", "")),
                adapter_code_path=str(source.get("adapter_code_path", "")),
                test_refs=list(source.get("test_refs", [])),
                docs_refs=list(source.get("docs_refs", [])),
                options=dict(source.get("options", {})),
            )
        )
    if has_registry_fields or (
        allow_real_sources
        and any(
            source.enabled and source.access_mode not in {"fixture", "manual"}
            for source in sources
        )
    ):
        try:
            validate_source_registry(sources)
        except SourceRegistryError as exc:
            raise ConfigError(str(exc)) from exc
    local_access_modes = {"fixture", "manual"}
    if not any(source.enabled and (allow_real_sources or source.access_mode in local_access_modes) for source in sources):
        if allow_real_sources:
            raise ConfigError("live-run requires at least one enabled source")
        raise ConfigError("dry-run requires at least one enabled fixture or manual source")
    blocked = [
        source.source_id
        for source in sources
        if source.enabled and not allow_real_sources and source.access_mode not in local_access_modes
    ]
    if blocked:
        raise ConfigError(
            "real source adapters are disabled for no-network dry-run: "
            + ", ".join(blocked)
        )

    base = path.parent.parent if path.parent.name == "config" else Path.cwd()
    return AppConfig(
        top_n=top_n,
        output_dir=(base / data.get("output_dir", "reports")).resolve(),
        fixture_path=(base / data.get("fixture_path", "fixtures/postings.json")).resolve(),
        delivery_mode=str(data.get("delivery_mode", "markdown_local")),
        thresholds=thresholds,
        scoring_weights=weights,
        profile=profile,
        user_context=context_from_profile(profile),
        sources=sources,
        manual_review_n=manual_review_n,
        scoring_schema_version=scoring_schema_version,
    )


def apply_context_documents(
    config: AppConfig,
    paths: Sequence[Path],
    *,
    extractor: Optional["ContextExtractor"] = None,
    cache: Optional["ContextExtractionCache"] = None,
) -> AppConfig:
    if extractor is not None:
        from .model_context import parse_context_documents_with_extractor

        context = parse_context_documents_with_extractor(paths, extractor, cache=cache)
    else:
        contexts = [parse_context_document(path) for path in paths]
        context = merge_user_contexts(contexts)
    return replace(config, profile=profile_from_context(context), user_context=context)


def apply_context_document(
    config: AppConfig,
    path: Path,
    *,
    extractor: Optional["ContextExtractor"] = None,
    cache: Optional["ContextExtractionCache"] = None,
) -> AppConfig:
    return apply_context_documents(config, [path], extractor=extractor, cache=cache)


def apply_supplemental_answers(config: AppConfig, answers: Dict[str, str]) -> AppConfig:
    context = merge_supplemental_answers(config.user_context, answers)
    return replace(config, profile=profile_from_context(context), user_context=context)
