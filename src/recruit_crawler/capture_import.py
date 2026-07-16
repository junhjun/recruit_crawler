from __future__ import annotations

import re
from dataclasses import replace
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Iterable, List, Optional, Sequence

from .capture_import_models import CaptureImportError, CaptureImportResult, CaptureImportSelection
from .capture_import_parsing import CREDENTIAL_VALUE_RE, load_capture_file
from .schemas import PipelineResultV2, PostingCandidate

def suppress_capture_report_links(result: PipelineResultV2) -> PipelineResultV2:
    """Remove capture URLs from the transient report input."""
    return replace(
        result,
        all_assessments=tuple(
            replace(assessment, source_url="")
            for assessment in result.all_assessments
        ),
    )

PUBLIC_CONTACT_RE = re.compile(r"[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}|\b0\d{1,2}[-.\s]?\d{3,4}[-.\s]?\d{4}\b")


def select_capture_files(spool_dir: Path, *, run_date: Optional[date] = None, latest: bool = False, files: Optional[Sequence[Path]] = None) -> CaptureImportSelection:
    if files:
        return CaptureImportSelection(_dedupe_paths([path.expanduser().resolve() for path in files]), run_date or date.today())
    root = spool_dir.expanduser().resolve()
    if run_date and latest:
        raise CaptureImportError("--date and --latest cannot be used together")
    selected_date = run_date or (_latest_capture_date(root) if latest else date.today())
    day_dir = root / selected_date.isoformat()
    if not day_dir.exists():
        raise CaptureImportError(f"capture date directory not found: {day_dir}")
    selected = sorted(path for path in day_dir.rglob("*.json") if path.is_file())
    if not selected:
        raise CaptureImportError(f"no capture JSON files found under: {day_dir}")
    return CaptureImportSelection(_dedupe_paths(selected), selected_date)


def import_capture_files(paths: Iterable[Path]) -> CaptureImportResult:
    candidates: List[PostingCandidate] = []
    errors: List[str] = []
    skipped: List[str] = []
    seen_candidates: set[tuple[str, str]] = set()
    sources: set[str] = set()
    for path in _dedupe_paths([Path(path) for path in paths]):
        try:
            file_candidates = load_capture_file(path)
        except CaptureImportError as exc:
            errors.append(f"{path}: {exc}")
            continue
        if not file_candidates:
            skipped.append(f"{path}: empty postings")
            continue
        for candidate in file_candidates:
            sources.add(candidate.source_id)
            key = (candidate.source_id, candidate.source_posting_id or candidate.source_url)
            if key in seen_candidates:
                skipped.append(f"{path}: duplicate posting {key[0]}:{key[1]}")
                continue
            seen_candidates.add(key)
            candidates.append(candidate)
    return CaptureImportResult(candidates=candidates, sources_attempted=sorted(sources), source_errors=errors + skipped, skipped_files=skipped)


def build_capture_quality_gate(selection: CaptureImportSelection, imported: CaptureImportResult) -> dict[str, Any]:
    findings = [{"severity": "fail" if _is_blocking_import_error(error) else "warning", "message": error} for error in imported.source_errors]
    privacy = _privacy_findings(imported.candidates)
    manual_review_items = [{"source_id": candidate.source_id, "source_posting_id": candidate.source_posting_id, "source_url": candidate.source_url, "flags": list(candidate.raw_jd.get("manual_review_flags", []))} for candidate in imported.candidates if candidate.raw_jd.get("manual_review_flags")]
    source_mode_counts: dict[str, int] = {}
    for candidate in imported.candidates:
        source_mode_counts[candidate.source_id] = source_mode_counts.get(candidate.source_id, 0) + 1
    has_failures = any(item["severity"] == "fail" for item in findings) or any(item["category"] == "fail" for item in privacy)
    status = "fail" if has_failures else "manual_review_required" if manual_review_items else "pass_with_warnings" if findings or privacy else "pass"
    return {"status": status, "checked_at": datetime.now(timezone.utc).isoformat(), "selected_date": selection.run_date.isoformat(), "input_files": [str(path) for path in selection.files], "sources_attempted": imported.sources_attempted, "source_mode_counts": source_mode_counts, "candidates_collected": len(imported.candidates), "findings": findings, "privacy": privacy, "manual_review_items": manual_review_items}


def _is_blocking_import_error(error: str) -> bool:
    return any(token in error for token in ("invalid JSON", "must be an object", "must be a list", "requires source_id/source_url/title/company", "sensitive field", "credential-like value"))


def _privacy_findings(candidates: Iterable[PostingCandidate]) -> list[dict[str, Any]]:
    findings: list[dict[str, Any]] = []
    for candidate in candidates:
        text = " ".join(str(value) for value in [candidate.title, candidate.company, candidate.location, *candidate.raw_jd.get("required_qualifications", []), *candidate.raw_jd.get("preferred_qualifications", []), *candidate.raw_jd.get("responsibilities", []), *candidate.raw_jd.get("company_info", [])])
        if CREDENTIAL_VALUE_RE.search(text):
            findings.append({"category": "fail", "source_id": candidate.source_id, "source_posting_id": candidate.source_posting_id, "message": "credential/session/auth-like value detected in imported posting text"})
        if PUBLIC_CONTACT_RE.search(text):
            findings.append({"category": "warning", "source_id": candidate.source_id, "source_posting_id": candidate.source_posting_id, "message": "public JD contact email/phone detected; keep as warning/manual review or redact by policy"})
    return findings


def _latest_capture_date(root: Path) -> date:
    if not root.exists():
        raise CaptureImportError(f"capture spool directory not found: {root}")
    dates: list[date] = []
    for child in root.iterdir():
        if child.is_dir():
            try:
                dates.append(date.fromisoformat(child.name))
            except ValueError:
                continue
    if not dates:
        raise CaptureImportError(f"no YYYY-MM-DD capture directories found under: {root}")
    return max(dates)


def _dedupe_paths(paths: Sequence[Path]) -> List[Path]:
    selected: List[Path] = []
    for path in paths:
        resolved = path.expanduser().resolve()
        if resolved not in selected:
            selected.append(resolved)
    return selected
