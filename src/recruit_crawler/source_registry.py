from __future__ import annotations

from typing import Any, Dict, Iterable, List, Optional

from .schemas import SourceManifest

TARGET_LANES = {"public_http", "browser_automation"}
TARGET_STATUSES = {"enabled", "blocked", "deferred", "excluded"}
MAINTENANCE_STATUSES = {"active", "watch", "blocked", "excluded"}
TARGET_ALLOWED_FIELDS = {
    "source_id",
    "source_url",
    "title",
    "company",
    "location",
    "deadline",
    "structured_snapshot",
}
NON_TARGET_OPTION_KEYS = {
    "approved_api_access": "API access cannot enable a target source",
    "approved_partner_access": "partner payload access cannot enable a target source",
    "approved_authenticated_flow": "authenticated partner flow cannot enable a target source",
    "manual_export_path": "manual export cannot enable a target source",
    "partner_payload_path": "partner payload cannot enable a target source",
    "manual_postings": "manual postings cannot enable a target source",
    "user_operated_chrome_extension": "user-operated Chrome extension cannot enable a target source",
    "user_operated_browser_use": "user-operated browser-use cannot enable a target source",
    "ocr_required": "OCR/manual review cannot enable a target source",
    "manual_review_required": "manual review cannot enable a target source",
    "manual_review_flags": "manual review flags cannot enable a target source",
}

POLICY_OVERRIDE_MODE = "user_directed_ignore"


class SourceRegistryError(ValueError):
    pass


def _field_list(values: Iterable[str]) -> List[str]:
    return [str(value) for value in values]


def validate_source_registry(sources: Iterable[SourceManifest]) -> None:
    for source in sources:
        _validate_source(source)


def _validate_source(source: SourceManifest) -> None:
    if source.target_status not in TARGET_STATUSES:
        raise SourceRegistryError(f"{source.source_id}: invalid target_status: {source.target_status}")
    if source.maintenance_status not in MAINTENANCE_STATUSES:
        raise SourceRegistryError(f"{source.source_id}: invalid maintenance_status: {source.maintenance_status}")
    if source.target_lane == "":
        raise SourceRegistryError(f"{source.source_id}: target_lane cannot be empty string")
    if source.target_lane is not None and source.target_lane not in TARGET_LANES:
        raise SourceRegistryError(f"{source.source_id}: invalid target_lane: {source.target_lane}")
    invalid_candidate_lanes = [lane for lane in source.candidate_lanes if lane not in TARGET_LANES]
    if invalid_candidate_lanes:
        raise SourceRegistryError(
            f"{source.source_id}: invalid candidate_lanes: {', '.join(invalid_candidate_lanes)}"
        )
    if source.target_status in {"blocked", "deferred", "excluded"} and source.target_lane is not None:
        raise SourceRegistryError(f"{source.source_id}: {source.target_status} sources must use target_lane null")
    if source.enabled:
        _validate_enabled_source(source)
    elif source.target_status == "enabled":
        raise SourceRegistryError(f"{source.source_id}: target_status enabled requires enabled=true")


def _validate_enabled_source(source: SourceManifest) -> None:
    if source.target_status != "enabled":
        raise SourceRegistryError(f"{source.source_id}: enabled source requires target_status=enabled")
    if source.target_lane not in TARGET_LANES:
        raise SourceRegistryError(f"{source.source_id}: enabled source requires a target_lane")
    if source.automation_level != "no_human":
        raise SourceRegistryError(f"{source.source_id}: enabled target requires automation_level=no_human")
    if source.tos_review_status != "pass" and not _has_policy_override(source):
        raise SourceRegistryError(f"{source.source_id}: enabled target requires passed source review")
    if source.access_mode == "api":
        raise SourceRegistryError(f"{source.source_id}: API access_mode cannot enable a target source")
    disallowed_fields = set(source.allowed_persisted_fields) - TARGET_ALLOWED_FIELDS
    if disallowed_fields:
        raise SourceRegistryError(
            f"{source.source_id}: enabled target persists disallowed fields: {', '.join(sorted(disallowed_fields))}"
        )
    for field_name in ("adapter_code_path", "test_refs", "docs_refs"):
        value = getattr(source, field_name)
        if not value:
            raise SourceRegistryError(f"{source.source_id}: enabled target requires {field_name}")
    if source.target_lane == "public_http" and source.access_mode not in {"public_page", "feed"}:
        raise SourceRegistryError(f"{source.source_id}: public_http target requires public_page or feed access_mode")
    if source.target_lane == "browser_automation" and source.access_mode != "browser_automation":
        raise SourceRegistryError(f"{source.source_id}: browser_automation target requires browser_automation access_mode")
    for key, message in NON_TARGET_OPTION_KEYS.items():
        if _is_truthy_non_target_option(source.options.get(key)):
            raise SourceRegistryError(f"{source.source_id}: {message}")
    if _has_policy_override(source) and source.target_lane != "browser_automation":
        raise SourceRegistryError(f"{source.source_id}: policy override is only allowed for browser_automation targets")
    if _has_policy_override(source) and source.access_mode != "browser_automation":
        raise SourceRegistryError(f"{source.source_id}: policy override requires browser_automation access_mode")


def _is_truthy_non_target_option(value: Any) -> bool:
    if value is None or value is False or value == "":
        return False
    if isinstance(value, (list, dict, tuple, set)):
        return bool(value)
    return bool(value)


def _has_policy_override(source: SourceManifest) -> bool:
    if source.options.get("policy_override_mode") != POLICY_OVERRIDE_MODE:
        return False
    reason = str(source.options.get("policy_override_reason", "")).strip()
    acknowledged = source.options.get("policy_override_acknowledges_source_notice") is True
    return bool(reason and acknowledged)


def source_status_rows(sources: Iterable[SourceManifest]) -> List[Dict[str, Any]]:
    return [source_status_row(source) for source in sources]


def source_status_row(source: SourceManifest) -> Dict[str, Any]:
    return {
        "source_id": source.source_id,
        "display_name": source.display_name,
        "enabled": source.enabled,
        "v1_role": source.v1_role,
        "target_status": source.target_status,
        "maintenance_status": source.maintenance_status,
        "target_lane": source.target_lane,
        "candidate_lanes": _field_list(source.candidate_lanes),
        "automation_level": source.automation_level,
        "status_reason": source.status_reason,
        "evidence": _field_list(source.evidence),
        "blockers": _field_list(source.blockers),
        "next_action": source.next_action,
        "adapter_code_path": source.adapter_code_path,
        "test_refs": _field_list(source.test_refs),
        "docs_refs": _field_list(source.docs_refs),
        "policy_override_mode": source.options.get("policy_override_mode"),
    }
