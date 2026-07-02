from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path
from typing import Any, Dict, Sequence

from .schemas import AppConfig, Profile, ScoringWeights, SourceManifest, Thresholds
from .user_context import context_from_profile, merge_user_contexts, parse_context_document, profile_from_context
from .source_registry import SourceRegistryError, validate_source_registry


class ConfigError(ValueError):
    pass


def _require_mapping(data: Any, label: str) -> Dict[str, Any]:
    if not isinstance(data, dict):
        raise ConfigError(f"{label} must be an object")
    return data


def load_config(path: Path, *, allow_real_sources: bool = False) -> AppConfig:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ConfigError(f"invalid JSON config: {exc}") from exc

    data = _require_mapping(raw, "config")
    top_n = int(data.get("top_n", 5))
    if top_n <= 0:
        raise ConfigError("top_n must be greater than zero")

    thresholds_raw = _require_mapping(data.get("thresholds", {}), "thresholds")
    thresholds = Thresholds(
        apply=int(thresholds_raw.get("apply", 75)),
        hold=int(thresholds_raw.get("hold", 50)),
    )
    if thresholds.apply <= thresholds.hold:
        raise ConfigError("thresholds.apply must be greater than thresholds.hold")

    weights_raw = _require_mapping(data.get("scoring_weights", {}), "scoring_weights")
    weights = ScoringWeights(
        required=int(weights_raw.get("required", 45)),
        preferred=int(weights_raw.get("preferred", 20)),
        responsibilities=int(weights_raw.get("responsibilities", 15)),
        company=int(weights_raw.get("company", 10)),
        location=int(weights_raw.get("location", 10)),
    )

    profile_raw = _require_mapping(data.get("profile", {}), "profile")
    profile = Profile(
        desired_roles=list(profile_raw.get("desired_roles", [])),
        skills=list(profile_raw.get("skills", [])),
        preferred_locations=list(profile_raw.get("preferred_locations", [])),
        max_experience_years=int(profile_raw.get("max_experience_years", 0)),
        exclusions=list(profile_raw.get("exclusions", [])),
        private_canaries=list(profile_raw.get("private_canaries", [])),
    )

    has_registry_fields = any("target_status" in source or "target_lane" in source for source in data.get("sources", []))
    sources = []
    for source in data.get("sources", []):
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
    if has_registry_fields:
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
    )


def apply_context_documents(config: AppConfig, paths: Sequence[Path]) -> AppConfig:
    contexts = [parse_context_document(path) for path in paths]
    context = merge_user_contexts(contexts)
    return replace(config, profile=profile_from_context(context), user_context=context)


def apply_context_document(config: AppConfig, path: Path) -> AppConfig:
    return apply_context_documents(config, [path])
