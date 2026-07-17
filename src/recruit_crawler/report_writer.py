from __future__ import annotations
from collections.abc import Mapping
import hashlib
import math
import time
import os
import re
import tempfile
import unicodedata
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Callable, Iterable, Literal, Optional

from .projection import false_report_artifact
from .report_policy import (
    MAX_DEGRADATION_NOTICE_BYTES,
    MAX_DEGRADATION_NOTICES,
    MAX_REPORT_ROW_BYTES,
    MAX_REPORT_ROWS,
    report_byte_budget,
)
from .schemas import REPORT_ARTIFACT_SCHEMA_VERSION, ReportArtifactV2, RenderedReportV2


@dataclass(frozen=True, slots=True)
class RuntimeBudgetV1:
    """Validated service budget split into normal work and cleanup time."""

    total_seconds: float = 300.0
    source_seconds: float = 60.0
    dns_preflight_seconds: float = 8.0
    output_lock_seconds: float = 5.0
    cleanup_seconds: float = 15.0

    def __post_init__(self) -> None:
        values = (
            self.total_seconds,
            self.source_seconds,
            self.dns_preflight_seconds,
            self.output_lock_seconds,
            self.cleanup_seconds,
        )
        if any(isinstance(value, bool) or not isinstance(value, (int, float)) for value in values):
            raise ValueError("runtime budget values must be numbers")
        if any(not math.isfinite(float(value)) or float(value) <= 0 for value in values):
            raise ValueError("runtime budget values must be positive and finite")
        if self.cleanup_seconds >= self.total_seconds:
            raise ValueError("cleanup budget must be less than total budget")


@dataclass(frozen=True, slots=True)
class RuntimeContext:
    """Immutable service-owned monotonic deadline context.

    ``deadline`` remains the historical hard deadline alias.  New service code
    uses ``normal_work_deadline`` for all ordinary work and ``hard_deadline``
    only for bounded cleanup.
    """

    started_at: float
    deadline: float | None = None
    monotonic: Callable[[], float] = time.monotonic
    command_mode: str = ""
    budget: RuntimeBudgetV1 = RuntimeBudgetV1()
    normal_work_deadline: float | None = None
    hard_deadline: float | None = None

    def __post_init__(self) -> None:
        hard_deadline = self.hard_deadline
        if hard_deadline is None:
            hard_deadline = self.deadline
        if hard_deadline is None:
            hard_deadline = self.started_at + self.budget.total_seconds
        normal_work_deadline = self.normal_work_deadline
        if normal_work_deadline is None:
            normal_work_deadline = (
                self.started_at + self.budget.total_seconds - self.budget.cleanup_seconds
                if self.deadline is None
                else hard_deadline
            )
        object.__setattr__(self, "hard_deadline", float(hard_deadline))
        object.__setattr__(self, "normal_work_deadline", float(normal_work_deadline))
        object.__setattr__(self, "deadline", float(hard_deadline))

    @classmethod
    def start(
        cls,
        *,
        timeout_seconds: float = 300.0,
        total_seconds: float | None = None,
        source_seconds: float = 60.0,
        dns_preflight_seconds: float = 8.0,
        output_lock_seconds: float = 5.0,
        cleanup_seconds: float = 15.0,
        command_mode: str = "",
        runtime_budget: RuntimeBudgetV1 | Mapping[str, object] | None = None,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> "RuntimeContext":
        if runtime_budget is None:
            budget = RuntimeBudgetV1(
                total_seconds=timeout_seconds if total_seconds is None else total_seconds,
                source_seconds=source_seconds,
                dns_preflight_seconds=dns_preflight_seconds,
                output_lock_seconds=output_lock_seconds,
                cleanup_seconds=cleanup_seconds,
            )
        elif isinstance(runtime_budget, RuntimeBudgetV1):
            budget = runtime_budget
        elif isinstance(runtime_budget, Mapping):
            budget = RuntimeBudgetV1(**dict(runtime_budget))
        else:
            raise ValueError("runtime_budget must be a mapping or RuntimeBudgetV1")
        started_at = monotonic()
        hard_deadline = started_at + budget.total_seconds
        return cls(
            started_at,
            hard_deadline,
            monotonic,
            command_mode,
            budget,
            started_at + budget.total_seconds - budget.cleanup_seconds,
            hard_deadline,
        )

    def remaining(self) -> float:
        """Remaining normal-work budget for collection and ordinary I/O."""
        return max(0.0, self.normal_work_deadline - self.monotonic())

    def hard_remaining(self) -> float:
        return max(0.0, self.hard_deadline - self.monotonic())

    @property
    def expired(self) -> bool:
        return self.remaining() <= 0.0

    @property
    def hard_expired(self) -> bool:
        return self.hard_remaining() <= 0.0

    @property
    def normal_work_expired(self) -> bool:
        return self.expired

    def allows_normal_work(self) -> bool:
        return not self.expired

_UNSAFE_REPORT_MARKER_RE = re.compile(
    r"(?:"
    r"\b(?:raw|private|canary|internal|military|army|veteran)(?:[_ -][a-z0-9]+)*\b|"
    r"\braw(?:[\s_-]+(?:jd|profile|resume|cv|data|text|source))+\b|"
    r"(?:private|raw)_[a-z0-9_]*canary\b|"
    r"\b(?:user[_ -]?context|user[_ -]?state|opaque[_ -]?identity|"
    r"explicit[_ -]?deal[_ -]?breakers|private[_ -]?canaries|"
    r"desired[_ -]?roles|preferred[_ -]?locations|max[_ -]?experience[_ -]?years|"
    r"provenance)\b|"
    r"(?:profile|resume)\s*:|"
    r"ignore\s+previous\s+instructions|system\s+prompt|developer\s+message|"
    r"access[_ -]?token|session[_ -]?token"
    r")",
    re.IGNORECASE,
)
_UNSAFE_MILITARY_TERM_RE = re.compile(
    r"(?:"
    r"군\s*(?:필|미필|복무|면제)|군대|군사|병역(?:\s*특례)?|"
    r"보충역|산업기능요원|전문연구요원|현역|예비역|대체\s*복무"
    r")"
)


def _normalize_for_matching(value: str) -> str:
    return unicodedata.normalize("NFC", value).casefold()


def _normalized_canary_match(value: str, normalized_canaries: Iterable[str]) -> bool:
    normalized_value = _normalize_for_matching(value)
    return any(canary and canary in normalized_value for canary in normalized_canaries)


def _contains_unsafe_military_term(value: str) -> bool:
    return bool(_UNSAFE_MILITARY_TERM_RE.search(_normalize_for_matching(value)))

PublicationFailureCode = Literal[
    "render_failed",
    "artifact_invalid",
    "write_failed_pre_replace",
    "fsync_failed_post_replace",
    "runtime_deadline_exceeded",
]
PublicationDurability = Literal["not_published", "published", "indeterminate"]
_ALLOWED_FAILURE_CODES = {
    "render_failed",
    "artifact_invalid",
    "write_failed_pre_replace",
    "fsync_failed_post_replace",
    "runtime_deadline_exceeded",
}
_ALLOWED_DURABILITY = {"not_published", "published", "indeterminate"}
_WRITE_PHASES = {
    "preimage_captured",
    "candidate_staged",
    "replaced",
    "directory_synced",
    "interrupted",
    "unknown",
}
WriteDurablePhase = Literal[
    "preimage_captured",
    "candidate_staged",
    "replaced",
    "directory_synced",
    "interrupted",
    "unknown",
]


@dataclass(frozen=True, slots=True)
class WriteReconciliationV1:
    """Evidence-backed outcome of one atomic destination write."""

    preimage_identity: str
    candidate_identity: str
    durable_phase: WriteDurablePhase
    observed_identity: str
    result: PublicationDurability

    def __post_init__(self) -> None:
        if self.durable_phase not in _WRITE_PHASES:
            raise ValueError("unsupported write durability phase")
        if self.result not in _ALLOWED_DURABILITY:
            raise ValueError("unsupported write reconciliation result")
        for name in (
            "preimage_identity",
            "candidate_identity",
            "observed_identity",
        ):
            if not isinstance(getattr(self, name), str) or not getattr(self, name):
                raise ValueError(f"{name} must be a non-empty identity")


ReportWriteReconciliationV1 = WriteReconciliationV1
GateWriteReconciliationV1 = WriteReconciliationV1


def _file_identity(path: Path) -> str:
    try:
        if not path.exists():
            return "absent"
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except (OSError, ValueError):
        return "unknown"


def reconcile_write(
    path: Path,
    *,
    preimage_identity: str,
    candidate_identity: str,
    durable_phase: WriteDurablePhase,
    indeterminate: bool = False,
) -> WriteReconciliationV1:
    """Reconcile a write without replacing bytes that cannot be identified."""
    observed_identity = _file_identity(path)
    if indeterminate:
        result: PublicationDurability = "indeterminate"
    elif observed_identity == candidate_identity:
        result = "published"
    elif observed_identity == preimage_identity:
        result = "not_published"
    else:
        result = "indeterminate"
    return WriteReconciliationV1(
        preimage_identity,
        candidate_identity,
        durable_phase,
        observed_identity,
        result,
    )
@dataclass(frozen=True, slots=True)
class ReportPublicationResultV1:
    artifact: ReportArtifactV2
    failure_code: Optional[PublicationFailureCode]
    durability: PublicationDurability
    reconciliation: WriteReconciliationV1 | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.artifact, ReportArtifactV2):
            raise TypeError("artifact must be ReportArtifactV2")
        if self.failure_code is not None and self.failure_code not in _ALLOWED_FAILURE_CODES:
            raise ValueError("unsupported publication failure code")
        if self.durability not in _ALLOWED_DURABILITY:
            raise ValueError("unsupported publication durability")
        if self.reconciliation is not None and self.reconciliation.result != self.durability:
            raise ValueError("publication durability does not match reconciliation")
        if self.failure_code is None:
            if self.artifact.generated and self.durability != "published":
                raise ValueError("generated success must be published")
            if not self.artifact.generated and self.durability != "not_published":
                raise ValueError("blocked success must not be published")
        if self.failure_code is not None and self.artifact.generated:
            raise ValueError("failed publication cannot expose an artifact")


def _result(
    artifact: ReportArtifactV2,
    failure_code: Optional[PublicationFailureCode],
    durability: PublicationDurability,
    reconciliation: WriteReconciliationV1 | None = None,
) -> ReportPublicationResultV1:
    return ReportPublicationResultV1(
        artifact,
        failure_code,
        durability,
        reconciliation,
    )


def _failure(code: PublicationFailureCode, durability: PublicationDurability) -> ReportPublicationResultV1:
    return _result(false_report_artifact(), code, durability)


def validate_rendered_report(
    rendered: object,
    *,
    private_canaries: Iterable[str] = (),
) -> None:
    """Validate immutable rendered bytes before touching the destination."""
    if not isinstance(rendered, RenderedReportV2):
        raise ValueError("expected RenderedReportV2")
    if rendered.schema_version != REPORT_ARTIFACT_SCHEMA_VERSION:
        raise ValueError("unsupported rendered report schema")
    if type(rendered.markdown_bytes) is not bytes:
        raise ValueError("rendered markdown must be bytes")
    if rendered.byte_length != len(rendered.markdown_bytes):
        raise ValueError("rendered byte length does not match markdown bytes")
    if hashlib.sha256(rendered.markdown_bytes).hexdigest() != rendered.content_sha256:
        raise ValueError("rendered content hash does not match markdown bytes")
    try:
        content = rendered.markdown_bytes.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ValueError("rendered markdown is not UTF-8") from exc
    normalized_canaries = tuple(
        _normalize_for_matching(canary)
        for canary in (private_canaries if not isinstance(private_canaries, str) else (private_canaries,))
        if isinstance(canary, str) and canary
    )
    if _normalized_canary_match(content, normalized_canaries):
        raise ValueError("rendered report contains configured private canary")
    if _contains_unsafe_military_term(content):
        raise ValueError("rendered report contains unsafe public marker")
    if _UNSAFE_REPORT_MARKER_RE.search(content):
        raise ValueError("rendered report contains unsafe public marker")
    if not content.endswith("\n"):
        raise ValueError("rendered markdown must end with LF")
    lines = content[:-1].split("\n")
    table_rows = 0
    in_table = False
    notice_lines: list[str] = []
    in_notices = False
    for line in lines:
        if line.startswith("## 수집 저하 안내"):
            in_notices = True
            in_table = False
            continue
        if in_notices:
            if line.startswith("- 소스 "):
                notice_lines.append(line)
            continue
        if line.startswith("| "):
            if line.startswith("| ---"):
                in_table = True
                continue
            if in_table:
                table_rows += 1
    if table_rows > MAX_REPORT_ROWS:
        raise ValueError("rendered report table exceeds capacity")
    if any(len(line.encode("utf-8")) > MAX_REPORT_ROW_BYTES for line in lines if line.startswith("| ")):
        raise ValueError("rendered report row exceeds capacity")
    if len(notice_lines) > MAX_DEGRADATION_NOTICES:
        raise ValueError("rendered degradation notices exceed capacity")
    if any(len(line.encode("utf-8")) > MAX_DEGRADATION_NOTICE_BYTES for line in notice_lines):
        raise ValueError("rendered degradation notice exceeds capacity")
    if len(rendered.markdown_bytes) > report_byte_budget(table_rows, len(notice_lines)):
        raise ValueError("rendered report exceeds capacity budget")


def _destination(output_dir: Path, run_date: date, report_slug: str) -> Path:
    if not isinstance(report_slug, str) or not report_slug:
        raise ValueError("report slug is required")
    if Path(report_slug).name != report_slug or report_slug in {".", ".."}:
        raise ValueError("report slug must be a basename")
    return Path(output_dir) / f"{report_slug}-{run_date.isoformat()}.md"


def publish_report(
    output_dir: Path,
    run_date: date,
    rendered: Optional[RenderedReportV2],
    *,
    report_slug: str,
    generated: bool = True,
    private_canaries: Iterable[str] = (),
    runtime_context: RuntimeContext | None = None,
) -> ReportPublicationResultV1:
    """Validate and atomically publish a rendered report."""
    if runtime_context is not None and runtime_context.expired:
        return _failure("runtime_deadline_exceeded", "not_published")
    if not generated:
        if rendered is not None:
            return _failure("artifact_invalid", "not_published")
        return _result(false_report_artifact(), None, "not_published")
    try:
        validate_rendered_report(rendered, private_canaries=private_canaries)
        path = _destination(output_dir, run_date, report_slug)
    except Exception:
        return _failure("artifact_invalid", "not_published")

    candidate_identity = rendered.content_sha256
    preimage_identity = _file_identity(path)
    output_path = Path(output_dir)
    temp_path: Optional[Path] = None
    candidate = ReportArtifactV2(
        schema_version=REPORT_ARTIFACT_SCHEMA_VERSION,
        generated=True,
        path=str(path),
        rendered=rendered,
    )

    def reconcile_result(
        failure_code: PublicationFailureCode,
        phase: str,
        *,
        indeterminate: bool = False,
        interrupted: bool = False,
    ) -> ReportPublicationResultV1:
        reconciliation = reconcile_write(
            path,
            preimage_identity=preimage_identity,
            candidate_identity=candidate_identity,
            durable_phase=phase,
            indeterminate=indeterminate,
        )
        if reconciliation.result == "published":
            if interrupted:
                return _result(
                    false_report_artifact(),
                    failure_code,
                    "published",
                    reconciliation,
                )
            return _result(candidate, None, "published", reconciliation)
        return _result(
            false_report_artifact(),
            failure_code,
            reconciliation.result,
            reconciliation,
        )

    try:
        if runtime_context is not None and runtime_context.expired:
            return reconcile_result(
                "runtime_deadline_exceeded",
                "interrupted",
                interrupted=True,
            )
        output_path.mkdir(parents=True, exist_ok=True)
        fd, temp_name = tempfile.mkstemp(
            prefix=f".{path.name}.",
            suffix=".tmp",
            dir=str(output_path),
        )
        temp_path = Path(temp_name)
        with os.fdopen(fd, "wb") as handle:
            handle.write(rendered.markdown_bytes)
            handle.flush()
            os.fsync(handle.fileno())
    except (TimeoutError, InterruptedError):
        if temp_path is not None:
            try:
                temp_path.unlink()
            except OSError:
                pass
        if runtime_context is not None:
            return reconcile_result(
                "runtime_deadline_exceeded",
                "interrupted",
                interrupted=True,
            )
        return reconcile_result("write_failed_pre_replace", "preimage_captured")
    except Exception:
        if temp_path is not None:
            try:
                temp_path.unlink()
            except OSError:
                pass
        return reconcile_result("write_failed_pre_replace", "preimage_captured")

    if runtime_context is not None and runtime_context.expired:
        if temp_path is not None:
            try:
                temp_path.unlink()
            except OSError:
                pass
        return reconcile_result(
            "runtime_deadline_exceeded",
            "interrupted",
            interrupted=True,
        )

    try:
        os.replace(temp_path, path)
        temp_path = None
    except (TimeoutError, InterruptedError):
        if temp_path is not None:
            try:
                temp_path.unlink()
            except OSError:
                pass
        if runtime_context is not None or _file_identity(path) == candidate_identity:
            return reconcile_result(
                "runtime_deadline_exceeded",
                "interrupted",
                interrupted=True,
            )
        return reconcile_result("write_failed_pre_replace", "candidate_staged")
    except Exception:
        if temp_path is not None:
            try:
                temp_path.unlink()
            except OSError:
                pass
        return reconcile_result("write_failed_pre_replace", "candidate_staged")

    if runtime_context is not None and runtime_context.expired:
        return reconcile_result(
            "runtime_deadline_exceeded",
            "interrupted",
            interrupted=True,
        )

    try:
        directory_fd = os.open(output_path, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    except Exception:
        return reconcile_result(
            "fsync_failed_post_replace",
            "replaced",
            indeterminate=True,
        )
    if runtime_context is not None and runtime_context.expired:
        return reconcile_result(
            "runtime_deadline_exceeded",
            "interrupted",
            interrupted=True,
        )

    reconciliation = reconcile_write(
        path,
        preimage_identity=preimage_identity,
        candidate_identity=candidate_identity,
        durable_phase="directory_synced",
    )
    if reconciliation.result != "published":
        return _result(
            false_report_artifact(),
            "runtime_deadline_exceeded"
            if runtime_context is not None and runtime_context.expired
            else "fsync_failed_post_replace",
            reconciliation.result,
            reconciliation,
        )
    return _result(candidate, None, "published", reconciliation)


def persist_rendered_report(
    output_dir: Path,
    run_date: date,
    rendered: Optional[RenderedReportV2],
    *,
    report_slug: str,
    generated: bool = True,
    private_canaries: Iterable[str] = (),
    runtime_context: RuntimeContext | None = None,
) -> ReportPublicationResultV1:
    """Validated publication entry point retained for pending integrations."""
    return publish_report(
        output_dir,
        run_date,
        rendered,
        report_slug=report_slug,
        generated=generated,
        private_canaries=private_canaries,
        runtime_context=runtime_context,
    )


def write_report(
    output_dir: Path,
    run_date: date,
    content: Optional[RenderedReportV2],
    *,
    report_slug: str,
    generated: bool = True,
    private_canaries: Iterable[str] = (),
    runtime_context: RuntimeContext | None = None,
) -> ReportPublicationResultV1:
    """Publish only a validated RenderedReportV2; raw text is never accepted."""
    return publish_report(
        output_dir,
        run_date,
        content,
        report_slug=report_slug,
        generated=generated,
        private_canaries=private_canaries,
        runtime_context=runtime_context,
    )


def no_report_artifact() -> ReportArtifactV2:
    return false_report_artifact()


__all__ = [
    "PublicationDurability",
    "PublicationFailureCode",
    "ReportPublicationResultV1",
    "WriteReconciliationV1",
    "WriteDurablePhase",
    "ReportWriteReconciliationV1",
    "GateWriteReconciliationV1",
    "reconcile_write",
    "RuntimeContext",
    "RuntimeBudgetV1",
    "no_report_artifact",
    "persist_rendered_report",
    "publish_report",
    "validate_rendered_report",
    "write_report",
]
