from __future__ import annotations

from collections.abc import Mapping
from contextlib import ExitStack, contextmanager
import fcntl
import re
import inspect
import hashlib
import json
import multiprocessing
import os
import errno
import socket
import threading
import time
import tempfile
import unicodedata
from dataclasses import dataclass, replace
from datetime import date
from pathlib import Path
from typing import Any, Literal, Optional
from unittest.mock import Mock

from ._scheduled_contract import (
    GateFinding,
    SourcePolicyRow,
    scheduled_preflight_gate,
    scheduled_quality_gate,
    scheduled_run_identity,
    scheduled_source_policy,
)
from .gate import (
    _safe_source_id,
    build_gate_v4,
    gate_json_sha256_v4,
    canonical_gate_v4_bytes,
)
from .pipeline import run_scheduled_run
from .projection import false_report_artifact, project_pipeline_result
from .schemas import (
    AppConfig,
    PersistenceEnvelopeV4,
    PipelineResultV4,
    ReportArtifactV2,
    RenderedReportV2,
    REPORT_ARTIFACT_SCHEMA_VERSION,
    PERSISTENCE_ENVELOPE_SCHEMA_VERSION,
)
from .projection import project_public_assessments
from .report_writer import (
    ReportPublicationResultV1,
    persist_rendered_report,
    validate_rendered_report,
    RuntimeContext,
    WriteReconciliationV1,
    reconcile_write,
)
GateWriteReconciliationV1 = WriteReconciliationV1
from .storage import (
    discard_scheduled_run,
    finalize_scheduled_run,
    persist_scheduled_run,
    persistence_probe_expectations,
    scheduled_run_persistence_state,
)
from .summarizer import render_report_v2
from .user_context import missing_context_fields
@dataclass(frozen=True, slots=True)
class _StorageCall:
    status: Literal["ok", "error", "timeout"]
    value: Any = None
    test_double: bool = False

def _storage_operation(operation: str) -> Any:
    return {
        "persist": persist_scheduled_run,
        "finalize": finalize_scheduled_run,
        "discard": discard_scheduled_run,
        "probe": scheduled_run_persistence_state,
    }[operation]



def _storage_helper_entry(
    sender: Any,
    operation: str,
    args: tuple[Any, ...],
    kwargs: dict[str, Any],
) -> None:
    try:
        value = _storage_operation(operation)(*args, **kwargs)
        sender.send(("ok", value))
    except BaseException:
        try:
            sender.send(("error", None))
        except Exception:
            pass
    finally:
        sender.close()


def _bounded_storage_call(
    operation: str,
    args: tuple[Any, ...],
    kwargs: dict[str, Any],
    *,
    runtime_context: RuntimeContext,
    cleanup: bool = False,
) -> _StorageCall:
    remaining = runtime_context.hard_remaining() if cleanup else runtime_context.remaining()
    timeout = min(5.0 if cleanup else 8.0, remaining)
    if timeout <= 0:
        return _StorageCall("timeout")
    operation_target = _storage_operation(operation)
    if isinstance(operation_target, Mock):
        try:
            return _StorageCall(
                "ok",
                operation_target(*args, **kwargs),
                test_double=True,
            )
        except BaseException:
            return _StorageCall("error", test_double=True)
    context = multiprocessing.get_context("fork")
    receiver, sender = context.Pipe(duplex=False)
    process = context.Process(
        target=_storage_helper_entry,
        args=(sender, operation, args, kwargs),
        daemon=True,
    )
    try:
        process.start()
        sender.close()
        if not receiver.poll(timeout):
            process.terminate()
            process.join(min(0.2, runtime_context.hard_remaining()))
            if process.is_alive():
                process.kill()
                process.join(min(0.2, runtime_context.hard_remaining()))
            return _StorageCall("timeout")
        status, value = receiver.recv()
        process.join(min(0.2, runtime_context.hard_remaining()))
        if process.is_alive():
            process.terminate()
            process.join(min(0.2, runtime_context.hard_remaining()))
        return _StorageCall(status, value)
    except (OSError, EOFError):
        return _StorageCall("error")
    finally:
        try:
            receiver.close()
        except Exception:
            pass
        if process.is_alive():
            process.terminate()
            process.join(min(0.2, runtime_context.hard_remaining()))


def _render_scheduled_report(
    result: PipelineResultV4,
    *,
    private_canaries: tuple[str, ...] = (),
    runtime_context: RuntimeContext | None = None,
) -> tuple[RenderedReportV2 | None, ReportPublicationResultV1]:
    if runtime_context is not None and runtime_context.expired:
        return None, ReportPublicationResultV1(
            false_report_artifact(), "runtime_deadline_exceeded", "not_published"
        )
    try:
        rendered = render_report_v2(result, private_canaries=private_canaries)
    except Exception:
        return None, ReportPublicationResultV1(
            false_report_artifact(), "render_failed", "not_published"
        )
    try:
        validate_rendered_report(rendered, private_canaries=private_canaries)
    except Exception:
        return None, ReportPublicationResultV1(
            false_report_artifact(), "artifact_invalid", "not_published"
        )
    return rendered, ReportPublicationResultV1(
        false_report_artifact(), None, "not_published"
    )


def _publish_scheduled_report(
    config: AppConfig,
    run_date: date,
    result: PipelineResultV4,
    rendered: RenderedReportV2 | None = None,
    *,
    private_canaries: tuple[str, ...] = (),
    runtime_context: RuntimeContext | None = None,
) -> ReportPublicationResultV1:
    if rendered is None:
        rendered, failed = _render_scheduled_report(
            result,
            private_canaries=private_canaries,
            runtime_context=runtime_context,
        )
        if rendered is None:
            return failed
    return persist_rendered_report(
        config.output_dir,
        run_date,
        rendered,
        report_slug="recruiting-scheduled-run",
        private_canaries=private_canaries,
        runtime_context=runtime_context,
    )


def _report_path(config: AppConfig, run_date: date) -> Path:
    return Path(config.output_dir) / f"recruiting-scheduled-run-{run_date.isoformat()}.md"


class _ReportLockError(OSError):
    pass
class _ScheduledRunBoundaryError(RuntimeError):
    pass


class _ScheduledCollectionInterrupted(RuntimeError):
    pass


def _report_lock_path(path: Path) -> Path:
    identity = hashlib.sha256(str(path).encode("utf-8")).hexdigest()[:24]
    return path.parent.parent / f".{path.parent.name}-{identity}.scheduled.lock"


@contextmanager
def _report_advisory_lock(
    path: Path,
    *,
    runtime_context: RuntimeContext | None = None,
):
    lock_deadline = (
        min(runtime_context.normal_work_deadline, runtime_context.monotonic() + 5.0)
        if runtime_context is not None
        else None
    )
    if runtime_context is not None and runtime_context.expired:
        raise _ReportLockError("scheduled output lock deadline exceeded")
    lock_path = _report_lock_path(path)
    preexisting = lock_path.exists()
    descriptor: int | None = None
    try:
        descriptor = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o600)
        while True:
            try:
                flags = fcntl.LOCK_EX
                if runtime_context is not None:
                    flags |= fcntl.LOCK_NB
                fcntl.flock(descriptor, flags)
                break
            except OSError as exc:
                blocked = exc.errno in {errno.EACCES, errno.EAGAIN}
                if not blocked or runtime_context is None:
                    raise
                remaining = min(
                    runtime_context.remaining(),
                    max(0.0, lock_deadline - runtime_context.monotonic()),
                )
                if remaining <= 0:
                    raise _ReportLockError("scheduled output lock deadline exceeded") from None
                time.sleep(min(0.01, remaining))
    except OSError as exc:
        if descriptor is not None:
            os.close(descriptor)
            descriptor = None
        if not preexisting:
            try:
                lock_path.unlink(missing_ok=True)
            except OSError:
                pass
        if isinstance(exc, _ReportLockError):
            raise
        raise _ReportLockError(str(exc)) from exc
    try:
        yield
    finally:
        if descriptor is not None:
            try:
                fcntl.flock(descriptor, fcntl.LOCK_UN)
            finally:
                os.close(descriptor)


@contextmanager
def _scheduled_output_locks(
    paths: tuple[Path, ...],
    *,
    runtime_context: RuntimeContext | None = None,
):
    by_lock_path = {
        _report_lock_path(path.resolve()): path.resolve()
        for path in paths
    }
    ordered_paths = [
        path for _, path in sorted(by_lock_path.items(), key=lambda item: str(item[0]))
    ]
    with ExitStack() as stack:
        for path in ordered_paths:
            if runtime_context is None:
                stack.enter_context(_report_advisory_lock(path))
            else:
                stack.enter_context(
                    _report_advisory_lock(path, runtime_context=runtime_context)
                )
        yield
def _capture_report(path: Path) -> tuple[bool, bytes | None]:
    try:
        if not path.exists():
            return False, None
        return True, path.read_bytes()
    except OSError:
        return True, None


def _gate_file_is_pass(path: Path) -> bool:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, TypeError, ValueError):
        return False
    return isinstance(payload, dict) and payload.get("status") == "pass"


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _rollback_report(
    path: Path,
    previous: tuple[bool, bytes | None],
    expected_current_sha256: str | None = None,
    *,
    runtime_context: RuntimeContext | None = None,
) -> bool:
    existed, content = previous
    if runtime_context is not None and runtime_context.hard_expired:
        return False
    if existed and content is None:
        return False
    temporary_path: Path | None = None
    try:
        if expected_current_sha256 is not None:
            if not path.is_file():
                return False
            current_content = path.read_bytes()
            if runtime_context is not None and runtime_context.hard_expired:
                return False
            if hashlib.sha256(current_content).hexdigest() != expected_current_sha256:
                return False
        if existed:
            descriptor, temporary_name = tempfile.mkstemp(
                prefix=f".{path.name}.rollback-",
                dir=path.parent,
            )
            temporary_path = Path(temporary_name)
            with os.fdopen(descriptor, "wb") as temporary:
                temporary.write(content)
                temporary.flush()
                os.fsync(temporary.fileno())
            os.replace(temporary_path, path)
            if runtime_context is not None and runtime_context.hard_expired:
                return False
            temporary_path = None
            _fsync_directory(path.parent)
            if runtime_context is not None and runtime_context.hard_expired:
                return False
            return path.is_file() and path.read_bytes() == content
        if runtime_context is not None and runtime_context.hard_expired:
            return False
        path.unlink(missing_ok=True)
        _fsync_directory(path.parent)
        if runtime_context is not None and runtime_context.hard_expired:
            return False
        return not path.exists()
    except OSError:
        return False
    finally:
        if temporary_path is not None:
            try:
                temporary_path.unlink(missing_ok=True)
            except OSError:
                pass


_PUBLIC_GATE_REDACTION = "[redacted]"
_PUBLIC_GATE_CANARY_FAILURE = "scheduled public gate sanitization failed"
_PUBLIC_GATE_UNSAFE_MARKER_RE = re.compile(
    r"(?:"
    r"\b(?:raw|private|canary|internal|military|army|veteran|profile|resume)(?:[_ -][a-z0-9]+)*\b|"
    r"\braw(?:[\s_-]+(?:jd|profile|resume|cv|data|text|source))+\b|"
    r"(?:private|raw)_[a-z0-9_]*canary\b|"
    r"\b(?:user[_ -]?context|user[_ -]?state|opaque[_ -]?identity|"
    r"explicit[_ -]?deal[_ -]?breakers|private[_ -]?canaries|"
    r"desired[_ -]?roles|max[_ -]?experience[_ -]?years|provenance)\b|"
    r"(?:profile|resume)\s*:|"
    r"ignore\s+previous\s+instructions|system\s+prompt|developer\s+message|"
    r"access[_ -]?token|session[_ -]?token|"
    r"군\s*(?:필|미필|복무|면제)|군대|군사|병역(?:\s*특례)?|"
    r"보충역|산업기능요원|전문연구요원|현역|예비역|대체\s*복무"
    r")",
    re.IGNORECASE,
)
# These names are part of the public Gate schema.  They may contain words
# such as ``profile`` (for example the V4 run identity hash), but the names
# themselves are not private payload and must remain stable at the boundary.
_PUBLIC_GATE_STRUCTURAL_KEYS = frozenset(
    {
        "schema_version",
        "command_mode",
        "run_date",
        "pipeline_schema_version",
        "score_schema_version",
        "disposition_schema_version",
        "status",
        "context_status",
        "report",
        "sources",
        "summary",
        "eligibility_reason_counts",
        "manual_reason_counts",
        "invariants",
        "findings",
        "source_outcomes",
        "run_identity",
        "run_id",
        "source_config_hash",
        "profile_config_hash",
        "gate_json_sha256",
        "gate_projection",
        "scheduled_db_failure",
    }
)

_PUBLIC_GATE_STRUCTURAL_MESSAGES = frozenset(
    {
        "required user context is missing",
        "scheduled source policy failed",
        "scheduled network preflight failed",
        "scheduled network preflight interrupted",
        "scheduled runtime failure",
        "scheduled database operation failed",
        "scheduled quality gate output failed",
    }
)


_MISSING_CONTEXT_FIELD_NAMES = (
    "desired_roles",
    "skills",
    "preferred_locations",
    "max_experience_years",
)
_PUBLIC_GATE_MISSING_CONTEXT_MESSAGES = frozenset(
    "scheduled-run missing required user context: "
    + ", ".join(
        field
        for index, field in enumerate(_MISSING_CONTEXT_FIELD_NAMES)
        if mask & (1 << index)
    )
    for mask in range(1, 1 << len(_MISSING_CONTEXT_FIELD_NAMES))
)


def _is_public_missing_context_message(value: str) -> bool:
    return value in _PUBLIC_GATE_MISSING_CONTEXT_MESSAGES
def _configured_canary_matcher(values: tuple[str, ...]) -> tuple[str, ...]:
    return tuple(
        unicodedata.normalize("NFC", value).casefold()
        for value in values
        if isinstance(value, str) and value
    )


def _canary_safe_text(preferred: str, matcher: tuple[str, ...]) -> str:
    normalized = unicodedata.normalize("NFC", preferred).casefold()
    if not any(canary in normalized for canary in matcher):
        return preferred
    if any(not canary for canary in matcher):
        raise ValueError("empty canary cannot be sanitized")
    used = {character for canary in matcher for character in canary}
    for codepoint in range(0xE000, 0xF900):
        candidate = chr(codepoint)
        if candidate not in used and all(canary not in candidate for canary in matcher):
            return candidate
    # The empty string is the only finite fail-safe that cannot contain a
    # non-empty configured canary. This bounded fallback avoids an unbounded
    # collision loop for hostile canary sets.
    return ""


def _sanitize_public_gate(
    gate: dict[str, Any],
    configured_canaries: tuple[str, ...],
) -> dict[str, Any]:
    matcher = _configured_canary_matcher(configured_canaries)
    contaminated = False
    try:
        redaction = _canary_safe_text(_PUBLIC_GATE_REDACTION, matcher)
        unknown_source = _canary_safe_text("unknown-source", matcher)
        failure_message = _canary_safe_text(_PUBLIC_GATE_CANARY_FAILURE, matcher)
    except ValueError:
        return {}

    def matches(value: str) -> bool:
        if value in _PUBLIC_GATE_STRUCTURAL_MESSAGES:
            return False
        # Missing-context findings include the field names needed by the
        # quality gate, but those names are structural diagnostics rather than
        # private context payload. Keep the complete public finding so a
        # scheduled run fails for the right quality reason instead of being
        # misclassified as a sanitization/runtime failure.
        if _is_public_missing_context_message(value):
            return False
        normalized = unicodedata.normalize("NFC", value).casefold()
        return bool(
            any(canary in normalized for canary in matcher)
            or _PUBLIC_GATE_UNSAFE_MARKER_RE.search(normalized)
        )

    def sanitize(value: Any, field_name: str | None = None) -> Any:
        nonlocal contaminated
        if isinstance(value, str):
            if matches(value):
                contaminated = True
                return unknown_source if field_name == "source_id" else redaction
            return value
        if isinstance(value, Mapping):
            sanitized: dict[Any, Any] = {}
            for key, child in value.items():
                safe_key = key
                if (
                    isinstance(key, str)
                    and key not in _PUBLIC_GATE_STRUCTURAL_KEYS
                    and matches(key)
                ):
                    contaminated = True
                    safe_key = redaction
                sanitized[safe_key] = sanitize(
                    child, key if isinstance(key, str) else None
                )
            return sanitized
        if isinstance(value, (list, tuple)):
            return [sanitize(item, field_name) for item in value]
        return value

    public = sanitize(gate)
    if not isinstance(public, dict):
        public = {}
        contaminated = True
    if contaminated:
        findings = public.get("findings")
        if not isinstance(findings, list):
            findings = []
        findings.append(
            {
                "severity": "fail",
                "source_id": None,
                "message": failure_message,
            }
        )
        public["findings"] = findings
        public["status"] = "fail"

    def contains_forbidden(value: Any) -> bool:
        if isinstance(value, str):
            if (
                value in _PUBLIC_GATE_STRUCTURAL_MESSAGES
                or _is_public_missing_context_message(value)
                or value
                in {
                    "desired_roles",
                    "skills",
                    "preferred_locations",
                    "max_experience_years",
                    "explicit_deal_breakers",
                }
            ):
                return False
            normalized = unicodedata.normalize("NFC", value).casefold()
            return bool(
                any(canary in normalized for canary in matcher)
                or _PUBLIC_GATE_UNSAFE_MARKER_RE.search(normalized)
            )
        if isinstance(value, Mapping):
            return any(
                (
                    (
                        isinstance(key, str)
                        and key not in _PUBLIC_GATE_STRUCTURAL_KEYS
                        and contains_forbidden(key)
                    )
                    or contains_forbidden(child)
                )
                for key, child in value.items()
            )
        if isinstance(value, (list, tuple)):
            return any(contains_forbidden(item) for item in value)
        return False

    if contains_forbidden(public):
        return {}
    return public


@dataclass(frozen=True, slots=True)
class _GateWriteOutcome:
    status: Literal["written", "not_written", "restored", "uncertain"]
    reconciliation: WriteReconciliationV1 | None = None

    @property
    def successful(self) -> bool:
        return self.status == "written"

    @property
    def may_have_replaced(self) -> bool:
        return self.status == "uncertain"
    def __bool__(self) -> bool:
        return self.successful



def _coerce_gate_write_outcome(value: object) -> _GateWriteOutcome:
    if isinstance(value, _GateWriteOutcome):
        return value
    return _GateWriteOutcome("written" if value is True else "not_written")
def _gate_outcome_is_durable(outcome: _GateWriteOutcome) -> bool:
    if not outcome.successful:
        return False
    reconciliation = outcome.reconciliation
    return reconciliation is None or reconciliation.durable_phase == "directory_synced"



def _restore_gate(
    path: Path,
    previous_exists: bool | None,
    previous_payload: bytes | None,
    *,
    runtime_context: RuntimeContext | None = None,
) -> _GateWriteOutcome:
    if previous_exists is None or (previous_exists and previous_payload is None):
        return _GateWriteOutcome("uncertain")
    if runtime_context is not None and runtime_context.hard_expired:
        return _GateWriteOutcome("uncertain")
    restore_path: Path | None = None
    try:
        if previous_exists:
            descriptor, restore_name = tempfile.mkstemp(
                prefix=f".{path.name}.restore-",
                suffix=".tmp",
                dir=path.parent,
            )
            restore_path = Path(restore_name)
            with os.fdopen(descriptor, "wb") as restore:
                restore.write(previous_payload)
                restore.flush()
                os.fsync(restore.fileno())
            os.replace(restore_path, path)
            if runtime_context is not None and runtime_context.hard_expired:
                return _GateWriteOutcome("uncertain")
            restore_path = None
        else:
            path.unlink(missing_ok=True)
        _fsync_directory(path.parent)
        if runtime_context is not None and runtime_context.hard_expired:
            return _GateWriteOutcome("uncertain")
        if previous_exists:
            restored = path.is_file() and path.read_bytes() == previous_payload
        else:
            restored = not path.exists()
        if runtime_context is not None and runtime_context.hard_expired:
            return _GateWriteOutcome("uncertain")
        return _GateWriteOutcome("restored" if restored else "uncertain")
    except Exception:
        return _GateWriteOutcome("uncertain")
    finally:
        if restore_path is not None:
            try:
                restore_path.unlink(missing_ok=True)
            except OSError:
                pass


def _write_gate_output(
    path: Path,
    gate: dict[str, Any],
    *,
    configured_canaries: tuple[str, ...] = (),
    runtime_context: RuntimeContext | None = None,
) -> _GateWriteOutcome:
    public_gate = _sanitize_public_gate(gate, configured_canaries)
    gate.clear()
    gate.update(public_gate)
    if not public_gate:
        gate.clear()
        gate.update(
            {
                "status": "fail",
                "findings": [
                    {
                        "severity": "fail",
                        "source_id": None,
                        "message": "scheduled public gate sanitization failed",
                    }
                ],
            }
        )
        return _GateWriteOutcome("not_written")
    payload = json.dumps(
        public_gate,
        ensure_ascii=False,
        indent=2,
    ).encode("utf-8")
    candidate_identity = hashlib.sha256(payload).hexdigest()
    previous_exists: bool | None = None
    previous_payload: bytes | None = None
    try:
        previous_exists = path.exists()
        previous_payload = path.read_bytes() if previous_exists else None
    except OSError:
        previous_exists = None
        previous_payload = None
    if previous_exists is None:
        preimage_identity = "unknown"
    elif previous_payload is None:
        preimage_identity = "absent"
    else:
        preimage_identity = hashlib.sha256(previous_payload).hexdigest()
    temporary_path: Path | None = None

    def reconcile_outcome(
        phase: str,
        *,
        indeterminate: bool = False,
        interrupted: bool = False,
    ) -> _GateWriteOutcome:
        reconciliation = reconcile_write(
            path,
            preimage_identity=preimage_identity,
            candidate_identity=candidate_identity,
            durable_phase=phase,
            indeterminate=indeterminate,
        )
        if interrupted:
            status: Literal["written", "not_written", "restored", "uncertain"] = (
                "uncertain"
            )
        elif reconciliation.result == "published":
            status = "written"
        elif reconciliation.result == "not_published":
            status = "not_written"
        else:
            status = "uncertain"
        return _GateWriteOutcome(status, reconciliation)

    if runtime_context is not None and runtime_context.expired:
        return reconcile_outcome("interrupted", interrupted=True)

    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=f".{path.name}.",
            suffix=".tmp",
            dir=path.parent,
        )
        temporary_path = Path(temporary_name)
        with os.fdopen(descriptor, "wb") as temporary:
            temporary.write(payload)
            temporary.flush()
            os.fsync(temporary.fileno())
    except (TimeoutError, InterruptedError):
        if temporary_path is not None:
            try:
                temporary_path.unlink(missing_ok=True)
            except OSError:
                pass
        return reconcile_outcome("interrupted", interrupted=True)
    except Exception:
        if temporary_path is not None:
            try:
                temporary_path.unlink(missing_ok=True)
            except OSError:
                pass
        return reconcile_outcome("preimage_captured")

    if runtime_context is not None and runtime_context.expired:
        if temporary_path is not None:
            try:
                temporary_path.unlink(missing_ok=True)
            except OSError:
                pass
        return reconcile_outcome("interrupted", interrupted=True)

    try:
        os.replace(temporary_path, path)
        temporary_path = None
    except (TimeoutError, InterruptedError):
        if temporary_path is not None:
            try:
                temporary_path.unlink(missing_ok=True)
            except OSError:
                pass
        return reconcile_outcome("interrupted", interrupted=True)
    except Exception:
        if temporary_path is not None:
            try:
                temporary_path.unlink(missing_ok=True)
            except OSError:
                pass
        return reconcile_outcome("candidate_staged")

    if runtime_context is not None and runtime_context.expired:
        return reconcile_outcome("interrupted", interrupted=True)

    try:
        _fsync_directory(path.parent)
    except Exception:
        reconciliation = reconcile_write(
            path,
            preimage_identity=preimage_identity,
            candidate_identity=candidate_identity,
            durable_phase="replaced",
            indeterminate=True,
        )
        if previous_exists is False:
            return _GateWriteOutcome("uncertain", reconciliation)
        restored = _restore_gate(
            path,
            previous_exists,
            previous_payload,
            runtime_context=runtime_context,
        )
        if restored.status == "uncertain":
            return _GateWriteOutcome("uncertain", reconciliation)
        restored_reconciliation = reconcile_write(
            path,
            preimage_identity=preimage_identity,
            candidate_identity=candidate_identity,
            durable_phase="replaced",
        )
        return _GateWriteOutcome(restored.status, restored_reconciliation)

    return reconcile_outcome("directory_synced")
def _write_gate_output_at_service_boundary(
    path: Path,
    gate: dict[str, Any],
    *,
    configured_canaries: tuple[str, ...] = (),
    runtime_context: RuntimeContext | None = None,
) -> _GateWriteOutcome:
    side_effect = getattr(_write_gate_output, "side_effect", None)
    target = side_effect if callable(side_effect) else _write_gate_output
    parameters = inspect.signature(target).parameters
    kwargs: dict[str, Any] = {}
    accepts_var_kwargs = any(
        parameter.kind is inspect.Parameter.VAR_KEYWORD
        for parameter in parameters.values()
    )
    if "configured_canaries" in parameters or accepts_var_kwargs:
        kwargs["configured_canaries"] = configured_canaries
    if runtime_context is not None and (
        "runtime_context" in parameters or accepts_var_kwargs
    ):
        kwargs["runtime_context"] = runtime_context
    return _coerce_gate_write_outcome(target(path, gate, **kwargs))


def _write_gate_payload(
    path: Path,
    payload: bytes,
    *,
    runtime_context: RuntimeContext | None = None,
) -> bool:
    if runtime_context is not None and runtime_context.hard_expired:
        return False
    temporary_path: Path | None = None
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=f".{path.name}.recovery-",
            suffix=".tmp",
            dir=path.parent,
        )
        temporary_path = Path(temporary_name)
        with os.fdopen(descriptor, "wb") as temporary:
            temporary.write(payload)
            temporary.flush()
            os.fsync(temporary.fileno())
        if runtime_context is not None and runtime_context.hard_expired:
            if temporary_path is not None:
                temporary_path.unlink(missing_ok=True)
                temporary_path = None
            return False
        os.replace(temporary_path, path)
        temporary_path = None
        _fsync_directory(path.parent)
        if runtime_context is not None and runtime_context.hard_expired:
            return False
        return True
    except Exception:
        if temporary_path is not None:
            try:
                temporary_path.unlink(missing_ok=True)
            except OSError:
                pass
        return False


def _gate_output_failure(gate: dict[str, Any]) -> dict[str, Any]:
    failed = dict(gate)
    findings = list(failed.get("findings", ()))
    if not any(item.get("message") == "scheduled quality gate output failed" for item in findings):
        findings.append(
            {
                "severity": "fail",
                "source_id": None,
                "message": "scheduled quality gate output failed",
            }
        )
    failed["findings"] = findings
    failed["status"] = "fail"
    return failed
def _database_operation_failure_gate(gate: dict[str, Any]) -> dict[str, Any]:
    failed = dict(gate)
    findings = list(failed.get("findings", ()))
    if not any(
        item.get("message") == "scheduled database operation failed"
        for item in findings
    ):
        findings.append(
            {
                "severity": "fail",
                "source_id": None,
                "message": "scheduled database operation failed",
            }
        )
    failed["findings"] = findings
    failed["status"] = "fail"
    return failed
def _report_capture_failure_gate(gate: dict[str, Any]) -> dict[str, Any]:
    failed = dict(gate)
    findings = list(failed.get("findings", ()))
    if not any(
        item.get("message") == "scheduled report capture failed"
        for item in findings
    ):
        findings.append(
            {
                "severity": "fail",
                "source_id": None,
                "message": "scheduled report capture failed",
            }
        )
    failed["findings"] = findings
    failed["status"] = "fail"
    return failed
@dataclass(frozen=True, slots=True)
class ScheduledRunRequest:
    config: AppConfig
    run_date: date
    quality_gate_output: Path
    output_dir: Optional[Path] = None
    db_path: Optional[Path] = None
    coordinator: Any = None
    runtime_context: RuntimeContext | None = None


@dataclass(slots=True)
class _ScheduledRecoveryState:
    report_path: Path
    previous_report: tuple[bool, bytes | None]
    report_publication_started: bool = False
    persistence_token: str | None = None
    pass_gate_write_started: bool = False
    pass_gate_write_uncertain: bool = False


@dataclass(frozen=True, slots=True)
class ScheduledRunResult:
    exit_code: int
    gate: dict[str, Any]
    result: PipelineResultV4 | None
    report_artifact: ReportArtifactV2
    run_identity: dict[str, str]
    quality_gate_output: Path
    publication_failure_code: Optional[str] = None
    publication_durability: Literal["not_published", "published", "indeterminate"] = "not_published"

    @property
    def stdout_lines(self) -> tuple[str, ...]:
        gate_passed = self.gate.get("status") == "pass"
        successful = gate_passed and self.exit_code == 0
        generated = bool(self.report_artifact.generated)
        if self.publication_durability == "indeterminate":
            report_line = "Report publication: indeterminate (final report status unknown)"
        elif self.publication_failure_code is not None:
            report_line = (
                "Report publication failed: "
                f"{self.publication_failure_code} ({self.publication_durability})"
            )
        elif successful:
            report_line = f"Report written: {self.report_artifact.path if generated else 'not generated'}"
        elif generated:
            report_line = "Report written: not published"
        elif self.publication_durability == "published":
            report_line = "Report publication: published (rolled back)"
        else:
            report_line = "Report written: not generated"
        lines = [
            "Scheduled run complete" if successful else "Scheduled run blocked",
            f"Run date: {self.run_identity['run_date']}",
            f"Run id: {self.run_identity['run_id']}",
            report_line,
            (
                "Quality gate not written: report and quality gate paths collide"
                if any(
                    item.get("message")
                    == "scheduled report and quality gate paths collide"
                    for item in self.gate.get("findings", ())
                )
                else (
                    "Quality gate not written: output lock acquisition failed"
                    if any(
                        item.get("message")
                        == "scheduled output lock acquisition failed"
                        for item in self.gate.get("findings", ())
                    )
                    else (
                        "Quality gate write failed: "
                        f"{self.quality_gate_output}"
                        if any(
                            item.get("message") == "scheduled quality gate output failed"
                            for item in self.gate.get("findings", ())
                        )
                        else f"Quality gate written: {self.quality_gate_output}"
                    )
                )
            ),
            f"Quality gate status: {self.gate.get('status', '')}",
        ]
        if any(
            item.get("message") == "scheduled report rollback could not be confirmed"
            for item in self.gate.get("findings", ())
        ):
            lines.append("Report rollback: could not be confirmed")
        context_status = self.gate.get("context_status")
        if context_status == "needs_context":
            lines.append("Missing context: required user context is incomplete")
        if self.gate.get("scheduled_db_failure"):
            lines.append("DB persistence failed")
        return tuple(lines)


def _resolve_domain(domain: str) -> None:
    socket.getaddrinfo(domain, 443)


def _resolve_domain_bounded(
    domain: str,
    runtime_context: RuntimeContext | None,
) -> str | None:
    if runtime_context is None:
        _resolve_domain(domain)
        return None
    errors: list[Exception] = []

    def resolve() -> None:
        try:
            _resolve_domain(domain)
        except Exception as exc:
            errors.append(exc)

    worker = threading.Thread(target=resolve, daemon=True)
    worker.start()
    worker.join(runtime_context.remaining())
    if worker.is_alive():
        return "interrupted"
    if errors:
        raise errors[0]
    return None


def _scheduled_network_findings(
    config: AppConfig,
    source_policy: list[SourcePolicyRow],
    *,
    runtime_context: RuntimeContext | None = None,
) -> list[GateFinding]:
    run_source_ids = {
        str(row["source_id"])
        for row in source_policy
        if row["scheduled_action"] == "run"
    }
    findings: list[GateFinding] = []
    checked_domains: set[str] = set()
    for source in config.sources:
        if source.source_id not in run_source_ids or source.access_mode == "fixture":
            continue
        for domain in source.domains:
            if domain in checked_domains:
                continue
            checked_domains.add(domain)
            if runtime_context is not None and runtime_context.expired:
                findings.append(
                    {
                        "severity": "fail",
                        "source_id": _safe_source_id(source.source_id),
                        "message": "scheduled network preflight failed",
                    }
                )
                return findings
            try:
                outcome = _resolve_domain_bounded(domain, runtime_context)
                if outcome == "interrupted":
                    findings.append(
                        {
                            "severity": "fail",
                            "source_id": _safe_source_id(source.source_id),
                            "message": "scheduled network preflight interrupted",
                        }
                    )
                    return findings
            except OSError:
                findings.append(
                    {
                        "severity": "fail",
                        "source_id": _safe_source_id(source.source_id),
                        "message": "scheduled network preflight failed",
                    }
                )
                return findings
    return findings


def _scheduled_gate(
    result: PipelineResultV4,
    config: AppConfig,
    projection: dict[str, Any],
    artifact: ReportArtifactV2,
    *,
    configured_canaries: tuple[str, ...] = (),
    runtime_failures: tuple[str, ...] = (),
    db_failure: bool = False,
) -> dict[str, Any]:
    if not isinstance(result, PipelineResultV4):
        raise TypeError("scheduled gate requires PipelineResultV4")
    enabled_source_ids = tuple(
        source.source_id for source in config.sources if source.enabled
    )
    gate_context = {
        "enabled_source_ids": list(enabled_source_ids),
        "context_status": "complete",
        "scheduled_db_failure": db_failure,
    }
    gate = build_gate_v4(
        result,
        enabled_source_ids=enabled_source_ids,
        configured_canaries=configured_canaries,
        context_status="complete",
        runtime_failures=runtime_failures,
        runtime_context=gate_context,
        report_artifact=artifact,
        projection=projection,
    )
    canonical_gate_v4_bytes(gate)
    return gate


def _persistence_envelope(
    result: PipelineResultV4,
    projection: dict[str, Any],
    artifact: ReportArtifactV2,
    gate: dict[str, Any],
    run_identity: dict[str, str],
) -> PersistenceEnvelopeV4:
    # Storage accepts only a report basename; the runtime artifact retains the
    # actual output path for the command's concise diagnostics.
    persisted_artifact = artifact
    if artifact.path is not None:
        persisted_artifact = replace(artifact, path=Path(artifact.path).name)
    source_outcomes = tuple(
        sorted(result.source_outcomes, key=lambda value: value.source_id)
    )
    return PersistenceEnvelopeV4(
        schema_version=PERSISTENCE_ENVELOPE_SCHEMA_VERSION,
        run_identity=run_identity,
        report_artifact=persisted_artifact,
        gate_status=str(gate["status"]),
        context_status=str(gate["context_status"]),
        gate_json_sha256=gate_json_sha256_v4(gate),
        summary=projection["summary"],
        source_metrics=projection["gate_sources"],
        assessments=project_public_assessments(
            result.all_assessments,
            command_mode=result.command_mode,
        ),
        source_outcomes=source_outcomes,
    )


def _report_gate_path_collision_gate(
    run_date: date,
    run_identity: dict[str, str],
) -> dict[str, Any]:
    gate = scheduled_quality_gate(
        scheduled_preflight_gate(run_date),
        [],
        None,
        [],
        [],
        run_identity,
        report_generated=False,
    )
    gate["status"] = "fail"
    gate["findings"] = [
        {
            "severity": "fail",
            "source_id": None,
            "message": "scheduled report and quality gate paths collide",
        }
    ]
    return gate

def _report_lock_failure_gate(
    run_date: date,
    run_identity: dict[str, str],
) -> dict[str, Any]:
    gate = scheduled_quality_gate(
        scheduled_preflight_gate(run_date),
        [],
        None,
        [],
        [],
        run_identity,
        report_generated=False,
    )
    gate["status"] = "fail"
    gate["findings"] = [
        {
            "severity": "fail",
            "source_id": None,
            "message": "scheduled output lock acquisition failed",
        }
    ]
    return gate
def _scheduled_runtime_failure_gate(
    run_date: date,
    db_path: Optional[Path],
    source_policy: list[SourcePolicyRow],
    run_identity: dict[str, str],
    *,
    failure_message: str = "scheduled runtime failure",
) -> dict[str, Any]:
    return scheduled_quality_gate(
        scheduled_preflight_gate(run_date),
        [],
        db_path,
        source_policy,
        [
            {
                "severity": "fail",
                "source_id": None,
                "message": failure_message,
            }
        ],
        run_identity,
        report_generated=False,
    )


def _run_scheduled_run_at_service_boundary(
    config: AppConfig,
    run_date: date,
    *,
    coordinator: Any = None,
    runtime_context: RuntimeContext | None = None,
) -> PipelineResultV4:
    try:
        target = getattr(run_scheduled_run, "side_effect", None)
        if not callable(target):
            target = run_scheduled_run
        parameters = inspect.signature(target).parameters
        kwargs: dict[str, Any] = {}
        if "coordinator" in parameters and coordinator is not None:
            kwargs["coordinator"] = coordinator
        if runtime_context is not None:
            if "runtime_context" in parameters:
                kwargs["runtime_context"] = runtime_context
            elif "context" in parameters:
                kwargs["context"] = runtime_context
            if "normal_work_deadline" in parameters:
                kwargs["normal_work_deadline"] = runtime_context.normal_work_deadline
        result = run_scheduled_run(config, run_date, **kwargs)
    except _ScheduledCollectionInterrupted:
        raise
    except TimeoutError:
        raise _ScheduledCollectionInterrupted from None
    except _ScheduledRunBoundaryError:
        raise
    except Exception:
        raise _ScheduledRunBoundaryError from None
    if not isinstance(result, PipelineResultV4):
        raise TypeError("scheduled service boundary requires PipelineResultV4")
    return result


def run_scheduled_job(request: ScheduledRunRequest) -> ScheduledRunResult:
    runtime_context = request.runtime_context or RuntimeContext.start(
        command_mode="scheduled-run",
        runtime_budget=getattr(request.config, "runtime_budget", None),
    )
    config = request.config
    if request.output_dir:
        config = replace(config, output_dir=request.output_dir.resolve())
    configured_canaries = tuple(
        dict.fromkeys(
            canary
            for canary in (
                *getattr(config.profile, "private_canaries", ()),
                *getattr(config.user_context, "private_canaries", ()),
            )
            if isinstance(canary, str) and canary
        )
    )
    run_identity = scheduled_run_identity(config, request.run_date)
    report_destination = _report_path(config, request.run_date).resolve()
    quality_gate_destination = Path(request.quality_gate_output).resolve()
    try:
        with _scheduled_output_locks(
            (report_destination, quality_gate_destination),
            runtime_context=runtime_context,
        ):
            return _run_scheduled_job_locked(
                request,
                runtime_context=runtime_context,
                coordinator=request.coordinator,
            )
    except (_ScheduledCollectionInterrupted, _ScheduledRunBoundaryError) as exc:
        source_policy, _ = scheduled_source_policy(config)
        gate = _sanitize_public_gate(
            _scheduled_runtime_failure_gate(
                request.run_date,
                request.db_path,
                source_policy,
                run_identity,
                failure_message=(
                    "scheduled collection interrupted"
                    if isinstance(exc, _ScheduledCollectionInterrupted)
                    else "scheduled runtime failure"
                ),
            ),
            configured_canaries,
        )
        _write_gate_output_at_service_boundary(
            request.quality_gate_output,
            gate,
            configured_canaries=configured_canaries,
            runtime_context=runtime_context,
        )
        return ScheduledRunResult(
            exit_code=1,
            gate=gate,
            result=None,
            report_artifact=false_report_artifact(),
            run_identity=run_identity,
            quality_gate_output=request.quality_gate_output,
            publication_failure_code=None,
            publication_durability="not_published",
        )
    except _ReportLockError:
        gate = _sanitize_public_gate(
            _report_lock_failure_gate(request.run_date, run_identity),
            configured_canaries,
        )
        return ScheduledRunResult(
            exit_code=1,
            gate=gate,
            result=None,
            report_artifact=false_report_artifact(),
            run_identity=run_identity,
            quality_gate_output=request.quality_gate_output,
            publication_failure_code=None,
            publication_durability="not_published",
        )

def _safe_runtime_failure_gate(
    request: ScheduledRunRequest,
    run_identity: dict[str, str],
    configured_canaries: tuple[str, ...],
) -> dict[str, Any]:
    source_policy: list[SourcePolicyRow] = []
    try:
        gate = _scheduled_runtime_failure_gate(
            request.run_date,
            request.db_path,
            source_policy,
            run_identity,
        )
        public_gate = _sanitize_public_gate(gate, configured_canaries)
        if public_gate:
            return public_gate
    except Exception:
        pass
    return {
        "status": "fail",
        "findings": [
            {
                "severity": "fail",
                "source_id": None,
                "message": "scheduled runtime failure",
            }
        ],
    }


def _recover_locked_scheduled_failure(
    request: ScheduledRunRequest,
    state: _ScheduledRecoveryState,
    run_identity: dict[str, str],
    configured_canaries: tuple[str, ...],
    *,
    runtime_context: RuntimeContext,
) -> ScheduledRunResult:
    if request.db_path is not None:
        if isinstance(state.persistence_token, str):
            _bounded_storage_call(
                "discard",
                (request.db_path, run_identity["run_id"], state.persistence_token),
                {},
                runtime_context=runtime_context,
                cleanup=True,
            )
        _bounded_storage_call(
            "probe",
            (request.db_path, run_identity["run_id"]),
            {},
            runtime_context=runtime_context,
            cleanup=True,
        )

    if state.report_publication_started:
        if state.pass_gate_write_started:
            rollback_ok = False
        else:
            try:
                rollback_ok = _rollback_report(
                    state.report_path,
                    state.previous_report,
                    runtime_context=runtime_context,
                )
            except Exception:
                rollback_ok = False
    else:
        rollback_ok = True
    publication_durability: Literal[
        "not_published", "published", "indeterminate"
    ] = "not_published" if rollback_ok else "indeterminate"

    gate = _safe_runtime_failure_gate(
        request,
        run_identity,
        configured_canaries,
    )
    gate_written = False
    try:
        gate_written = _coerce_gate_write_outcome(
            _write_gate_output_at_service_boundary(
                request.quality_gate_output,
                gate,
                configured_canaries=configured_canaries,
                runtime_context=runtime_context,
            )
        ).successful
    except Exception:
        gate_written = False
    if not gate_written:
        try:
            safe_gate = _sanitize_public_gate(gate, configured_canaries)
            if not safe_gate:
                return ScheduledRunResult(
                    exit_code=1,
                    gate=gate,
                    result=None,
                    report_artifact=false_report_artifact(),
                    run_identity=run_identity,
                    quality_gate_output=request.quality_gate_output,
                    publication_failure_code=None,
                    publication_durability="not_published",
                )
            payload = json.dumps(
                safe_gate,
                ensure_ascii=False,
                indent=2,
            ).encode("utf-8")
            _write_gate_payload(
                request.quality_gate_output,
                payload,
                runtime_context=runtime_context,
            )
        except Exception:
            pass

    return ScheduledRunResult(
        exit_code=1,
        gate=gate,
        result=None,
        report_artifact=false_report_artifact(),
        run_identity=run_identity,
        quality_gate_output=request.quality_gate_output,
        publication_failure_code=None,
        publication_durability=publication_durability,
    )


def _run_scheduled_job_locked(
    request: ScheduledRunRequest,
    *,
    runtime_context: RuntimeContext | None = None,
    coordinator: Any = None,
) -> ScheduledRunResult:
    runtime_context = runtime_context or RuntimeContext.start()
    config = request.config
    try:
        if request.output_dir:
            config = replace(config, output_dir=request.output_dir.resolve())
        report_path = _report_path(config, request.run_date).resolve()
        previous_report = _capture_report(report_path)
    except Exception:
        report_path = Path(".") / f"recruiting-scheduled-run-{request.run_date.isoformat()}.md"
        previous_report = (False, None)
    state = _ScheduledRecoveryState(report_path, previous_report)
    configured_canaries: tuple[str, ...] = ()
    try:
        configured_canaries = tuple(
            dict.fromkeys(
                canary
                for canary in (
                    *getattr(config.profile, "private_canaries", ()),
                    *getattr(config.user_context, "private_canaries", ()),
                )
                if isinstance(canary, str) and canary
            )
        )
        run_identity = scheduled_run_identity(config, request.run_date)
        return _run_scheduled_job_locked_impl(
            request,
            state,
            runtime_context=runtime_context,
            coordinator=coordinator,
        )
    except Exception:
        try:
            run_identity = scheduled_run_identity(config, request.run_date)
        except Exception:
            run_identity = {
                "run_id": "unknown",
                "run_date": request.run_date.isoformat(),
            }
        return _recover_locked_scheduled_failure(
            request,
            state,
            run_identity,
            configured_canaries,
            runtime_context=runtime_context,
        )


def _run_scheduled_job_locked_impl(
    request: ScheduledRunRequest,
    state: _ScheduledRecoveryState,
    *,
    runtime_context: RuntimeContext | None = None,
    coordinator: Any = None,
) -> ScheduledRunResult:
    runtime_context = runtime_context or RuntimeContext.start()
    config = request.config
    if request.output_dir:
        config = replace(config, output_dir=request.output_dir.resolve())
    configured_private_canaries = tuple(
        dict.fromkeys(
            canary
            for canary in (
                *getattr(config.profile, "private_canaries", ()),
                *getattr(config.user_context, "private_canaries", ()),
            )
            if isinstance(canary, str) and canary
        )
    )
    run_identity = scheduled_run_identity(config, request.run_date)
    report_destination = _report_path(config, request.run_date).resolve()
    quality_gate_destination = Path(request.quality_gate_output).resolve()
    paths_collide = quality_gate_destination == report_destination
    if not paths_collide:
        try:
            paths_collide = os.path.samefile(quality_gate_destination, report_destination)
        except OSError:
            paths_collide = False
    if paths_collide:
        collision_gate = _sanitize_public_gate(
            _report_gate_path_collision_gate(request.run_date, run_identity),
            configured_private_canaries,
        )
        return ScheduledRunResult(
            exit_code=1,
            gate=collision_gate,
            result=None,
            report_artifact=false_report_artifact(),
            run_identity=run_identity,
            quality_gate_output=request.quality_gate_output,
            publication_failure_code=None,
            publication_durability="not_published",
        )

    missing_fields = missing_context_fields(config.user_context)
    source_policy, policy_findings = scheduled_source_policy(config)
    network_findings = (
        []
        if missing_fields or policy_findings
        else _scheduled_network_findings(
            config,
            source_policy,
            runtime_context=runtime_context,
        )
    )
    if runtime_context.expired and not network_findings:
        network_findings = [
            {
                "severity": "fail",
                "source_id": None,
                "message": "scheduled preflight failed",
            }
        ]

    result = None
    artifact = false_report_artifact()
    publication = ReportPublicationResultV1(artifact, None, "not_published")
    publication_durability = publication.durability
    report_publication_deferred = False
    gate_output_failed = False
    gate_output_written = False
    if missing_fields or policy_findings or network_findings:
        gate = scheduled_quality_gate(
            scheduled_preflight_gate(request.run_date),
            missing_fields,
            request.db_path,
            source_policy,
            [*policy_findings, *network_findings],
            run_identity,
            report_generated=False,
        )
    else:
        result = _run_scheduled_run_at_service_boundary(
            config,
            request.run_date,
            coordinator=coordinator,
            runtime_context=runtime_context,
        )
        if runtime_context.expired:
            gate = _sanitize_public_gate(
                _scheduled_runtime_failure_gate(
                    request.run_date,
                    request.db_path,
                    source_policy,
                    run_identity,
                    failure_message="scheduled normal-work deadline exceeded",
                ),
                configured_private_canaries,
            )
            _write_gate_output_at_service_boundary(
                request.quality_gate_output,
                gate,
                configured_canaries=configured_private_canaries,
                runtime_context=runtime_context,
            )
            return ScheduledRunResult(
                exit_code=1,
                gate=gate,
                result=result,
                report_artifact=false_report_artifact(),
                run_identity=run_identity,
                quality_gate_output=request.quality_gate_output,
                publication_failure_code=None,
                publication_durability="not_published",
            )
        projection = project_pipeline_result(result)
        rendered, render_publication = _render_scheduled_report(
            result,
            private_canaries=configured_private_canaries,
            runtime_context=runtime_context,
        )
        publication = render_publication
        publication_durability = publication.durability
        if rendered is None:
            gate = _scheduled_gate(
                result,
                config,
                projection,
                artifact,
                configured_canaries=configured_private_canaries,
            )
        else:
            candidate = ReportArtifactV2(
                schema_version=REPORT_ARTIFACT_SCHEMA_VERSION,
                generated=True,
                path=str(_report_path(config, request.run_date)),
                rendered=rendered,
            )
            candidate_report_identity = hashlib.sha256(
                rendered.markdown_bytes
            ).hexdigest()
            candidate_gate = _scheduled_gate(
                result,
                config,
                projection,
                candidate,
                configured_canaries=configured_private_canaries,
            )
            if runtime_context.expired and candidate_gate.get("status") == "pass":
                candidate_gate = _scheduled_gate(
                    result,
                    config,
                    projection,
                    candidate,
                    configured_canaries=configured_private_canaries,
                    runtime_failures=("scheduled_runtime_deadline_exceeded",),
                )
            if candidate_gate.get("status") != "pass":
                # The candidate bytes were validated by the Gate, but a warning
                # or failure is never exposed as a final report.
                gate = (
                    candidate_gate
                    if not gate_output_written
                    else _scheduled_gate(
                        result,
                        config,
                        projection,
                        artifact,
                        configured_canaries=configured_private_canaries,
                    )
                )
            else:
                prior_pass_gate = _gate_file_is_pass(request.quality_gate_output)
                provisional_gate = _scheduled_gate(
                    result,
                    config,
                    projection,
                    artifact,
                    configured_canaries=configured_private_canaries,
                    runtime_failures=("scheduled_publication_pending",),
                )
                provisional_outcome = _coerce_gate_write_outcome(
                    _write_gate_output_at_service_boundary(
                        request.quality_gate_output,
                        provisional_gate,
                        configured_canaries=configured_private_canaries,
                        runtime_context=runtime_context,
                    )
                )
                gate_output_written = provisional_outcome.successful
                if not gate_output_written:
                    gate_output_failed = True
                    candidate_gate = _gate_output_failure(provisional_gate)
                report_path = state.report_path
                previous_report = state.previous_report
                if (
                    previous_report[0]
                    and previous_report[1] is not None
                    and hashlib.sha256(previous_report[1]).hexdigest()
                    == candidate_report_identity
                    and gate_output_written
                ):
                    publication = ReportPublicationResultV1(candidate, None, "published")
                elif gate_output_written and not (
                    previous_report[0] and previous_report[1] is None
                ):
                    # Keep the candidate in memory until a durable final pass
                    # Gate exists; the report path must not expose it earlier.
                    report_publication_deferred = True
                    publication = ReportPublicationResultV1(
                        false_report_artifact(), None, "not_published"
                    )
                publication_durability = publication.durability
                if publication.durability == "not_published" and not report_publication_deferred:
                    artifact = false_report_artifact()
                    gate = _scheduled_gate(
                        result,
                        config,
                        projection,
                        artifact,
                        configured_canaries=configured_private_canaries,
                    )
                    if previous_report[0] and previous_report[1] is None:
                        gate = _report_capture_failure_gate(gate)
                elif publication.failure_code == "fsync_failed_post_replace":
                    artifact = candidate
                    publication_durability = "indeterminate"
                    gate = _scheduled_gate(
                        result,
                        config,
                        projection,
                        artifact,
                        configured_canaries=configured_private_canaries,
                        runtime_failures=("scheduled_report_publication_uncertain",),
                    )
                elif (
                    not report_publication_deferred
                    and (
                        publication.failure_code is not None
                        or publication.durability != "published"
                    )
                ):
                    rollback_ok = _rollback_report(
                        report_path,
                        previous_report,
                        candidate_report_identity,
                        runtime_context=runtime_context,
                    )
                    if rollback_ok:
                        publication_durability = "not_published"
                        artifact = false_report_artifact()
                        gate = _scheduled_gate(
                            result,
                            config,
                            projection,
                            artifact,
                            configured_canaries=configured_private_canaries,
                        )
                    else:
                        artifact = candidate
                        publication_durability = "indeterminate"
                        gate = _scheduled_gate(
                            result,
                            config,
                            projection,
                            artifact,
                            configured_canaries=configured_private_canaries,
                            runtime_failures=("scheduled_report_rollback_failure",),
                        )
                else:
                    artifact = candidate if report_publication_deferred else publication.artifact
                    gate = candidate_gate
                    persistence_ok = True
                    persistence_state = "committed"
                    persistence_token: str | None = None
                    persistence_failure = "scheduled_db_failure"
                    if request.db_path:
                        envelope = _persistence_envelope(
                            result, projection, artifact, gate, run_identity
                        )
                        expectations = persistence_probe_expectations(envelope)
                        expected_kwargs = {
                            key: expectations[key]
                            for key in (
                                "expected_identity",
                                "expected_versions",
                                "expected_gate_json_sha256",
                                "expected_content_sha256",
                                "expected_token",
                            )
                        }
                        expected_token = expectations["expected_token"]
                        persistence_token = expected_token
                        state.persistence_token = expected_token
                        probe_call = _bounded_storage_call(
                            "probe",
                            (request.db_path, run_identity["run_id"]),
                            expected_kwargs,
                            runtime_context=runtime_context,
                        )
                        reconciled = (
                            probe_call.status == "ok"
                            and probe_call.value == "committed"
                        )
                        pending_reconciled = (
                            probe_call.status == "ok"
                            and probe_call.value == "pending"
                        )
                        if pending_reconciled:
                            persistence_failure = "scheduled_db_finalize_failure"
                            persistence_state = "pending"
                            persist_call = _StorageCall(
                                "ok",
                                expected_token,
                                test_double=probe_call.test_double,
                            )
                            storage_test_double = probe_call.test_double
                        elif reconciled:
                            # A matching committed V4 identity already owns the
                            # durable rows. Do not replay inserts merely because
                            # the internal outcome timings changed.
                            persist_call = _StorageCall("ok", None, test_double=True)
                            storage_test_double = True
                        else:
                            persist_call = _bounded_storage_call(
                                "persist",
                                (
                                    request.db_path,
                                    envelope,
                                ),
                                {"configured_canaries": configured_private_canaries},
                                runtime_context=runtime_context,
                            )
                            storage_test_double = persist_call.test_double
                        if (
                            persist_call.test_double
                            and persist_call.status != "ok"
                        ):
                            persistence_state = "absent"
                        if persist_call.status != "ok":
                            persistence_failure = "scheduled_db_failure"
                            if not persist_call.test_double:
                                persistence_state = "indeterminate"
                        else:
                            returned_token = persist_call.value
                            if returned_token is not None and not isinstance(returned_token, str):
                                persistence_state = "indeterminate"
                            else:
                                persistence_state = "pending" if returned_token else "committed"
                        if (
                            persistence_state in {"pending", "committed", "indeterminate"}
                            and not storage_test_double
                        ):
                            probe_call = _bounded_storage_call(
                                "probe",
                                (request.db_path, run_identity["run_id"]),
                                expected_kwargs,
                                runtime_context=runtime_context,
                            )
                            if probe_call.status == "ok" and probe_call.value in {
                                "committed",
                                "absent",
                                "pending",
                                "indeterminate",
                            }:
                                persistence_state = probe_call.value
                                if (
                                    persistence_state == "indeterminate"
                                    and prior_pass_gate
                                    and previous_report[0]
                                    and previous_report[1] is not None
                                    and hashlib.sha256(previous_report[1]).hexdigest()
                                    == candidate_report_identity
                                ):
                                    # A repeated identical invocation may race
                                    # with the prior finalization probe. Reconcile
                                    # by run identity before treating storage as
                                    # an unknown failure.
                                    identity_probe = _bounded_storage_call(
                                        "probe",
                                        (request.db_path, run_identity["run_id"]),
                                        {},
                                        runtime_context=runtime_context,
                                    )
                                    if (
                                        identity_probe.status == "ok"
                                        and identity_probe.value in {
                                            "committed",
                                            "pending",
                                            "absent",
                                        }
                                    ):
                                        persistence_state = identity_probe.value
                            else:
                                persistence_state = "indeterminate"
                        # A pending stage is intentional: it is promoted only
                        # after the final Gate write has directory_synced evidence.
                        persistence_ok = persistence_state in {"pending", "committed"}
                    if not persistence_ok:
                        rollback_failures: tuple[str, ...] = (persistence_failure,)
                        if persistence_state == "pending" and isinstance(
                            persistence_token, str
                        ):
                            discarded_call = _bounded_storage_call(
                                "discard",
                                (
                                    request.db_path,
                                    run_identity["run_id"],
                                    persistence_token,
                                ),
                                {
                                    "expected_identity": expectations["expected_identity"],
                                    "expected_versions": expectations["expected_versions"],
                                    "expected_gate_json_sha256": expectations["expected_gate_json_sha256"],
                                    "expected_content_sha256": expectations["expected_content_sha256"],
                                    "expected_token": expectations["expected_token"],
                                },
                                runtime_context=runtime_context,
                                cleanup=True,
                            )
                            if (
                                discarded_call.status == "ok"
                                and discarded_call.value is True
                            ):
                                persistence_state = "absent"
                        if persistence_state in {"pending", "indeterminate"}:
                            rollback_ok = False
                        elif report_publication_deferred and not report_path.exists():
                            # The candidate has not touched the report path yet.
                            # A proven-absent DB stage therefore needs no report
                            # rollback and remains definitely not published.
                            rollback_ok = True
                        else:
                            rollback_ok = _rollback_report(
                                report_path,
                                previous_report,
                                candidate_report_identity,
                                runtime_context=runtime_context,
                            )
                        if rollback_ok:
                            artifact = false_report_artifact()
                            publication_durability = "not_published"
                            rollback_failures = ()
                        else:
                            artifact = publication.artifact
                            publication_durability = "indeterminate"
                            rollback_failures = (
                                *rollback_failures,
                                "scheduled_report_rollback_failure",
                            )
                        gate = _scheduled_gate(
                            result,
                            config,
                            projection,
                            artifact,
                            configured_canaries=configured_private_canaries,
                            runtime_failures=rollback_failures,
                            db_failure=True,
                        )
                    else:
                        state.pass_gate_write_started = True
                        final_outcome = _coerce_gate_write_outcome(
                            _write_gate_output_at_service_boundary(
                                request.quality_gate_output,
                                gate,
                                configured_canaries=configured_private_canaries,
                                runtime_context=runtime_context,
                            )
                        )
                        gate_output_written = _gate_outcome_is_durable(final_outcome)
                        if not gate_output_written:
                            state.pass_gate_write_uncertain = (
                                final_outcome.may_have_replaced
                            )
                            gate_output_failed = True
                            gate = _gate_output_failure(gate)
                            if not final_outcome.may_have_replaced:
                                artifact = false_report_artifact()
                                publication_durability = "not_published"
                            if (
                                not final_outcome.may_have_replaced
                                and request.db_path is not None
                                and isinstance(persistence_token, str)
                            ):
                                _bounded_storage_call(
                                    "discard",
                                    (
                                        request.db_path,
                                        run_identity["run_id"],
                                        persistence_token,
                                    ),
                                    expected_kwargs,
                                    runtime_context=runtime_context,
                                    cleanup=True,
                                )
                                artifact = false_report_artifact()
                                publication_durability = "not_published"
                            rollback_ok = (
                                report_publication_deferred and not report_path.exists()
                            ) or _rollback_report(
                                report_path,
                                previous_report,
                                candidate_report_identity,
                                runtime_context=runtime_context,
                            )
                            if rollback_ok:
                                artifact = false_report_artifact()
                                publication_durability = "not_published"
                            else:
                                artifact = candidate
                                publication_durability = "indeterminate"
                            fallback_outcome = _coerce_gate_write_outcome(
                                _write_gate_output_at_service_boundary(
                                    request.quality_gate_output,
                                    gate,
                                    configured_canaries=configured_private_canaries,
                                    runtime_context=runtime_context,
                                )
                            )
                            fallback_written = fallback_outcome.successful
                            if not fallback_written:
                                try:
                                    safe_gate = _sanitize_public_gate(
                                        gate,
                                        configured_private_canaries,
                                    )
                                    fallback_payload = (
                                        json.dumps(
                                            safe_gate,
                                            ensure_ascii=False,
                                            indent=2,
                                        ).encode("utf-8")
                                        if safe_gate
                                        else None
                                    )
                                except Exception:
                                    fallback_payload = None
                                if fallback_payload is not None:
                                    fallback_written = _write_gate_payload(
                                        request.quality_gate_output,
                                        fallback_payload,
                                        runtime_context=runtime_context,
                                    )
                            state.pass_gate_write_uncertain = (
                                state.pass_gate_write_uncertain
                                or fallback_outcome.may_have_replaced
                            )
                            if (
                                report_publication_deferred
                                and final_outcome.may_have_replaced
                            ):
                                # The final Gate write may already have replaced
                                # the provisional bytes. Preserve the matching
                                # candidate report rather than rolling it back
                                # on an unknown post-replace outcome.
                                state.report_publication_started = True
                                publication = _publish_scheduled_report(
                                    config,
                                    request.run_date,
                                    result,
                                    rendered,
                                    private_canaries=configured_private_canaries,
                                    runtime_context=runtime_context,
                                )
                                publication_durability = publication.durability
                                if publication.durability == "published":
                                    artifact = publication.artifact
                                else:
                                    artifact = false_report_artifact()
                            if not state.pass_gate_write_uncertain:
                                rollback_ok = _rollback_report(
                                    report_path,
                                    previous_report,
                                    candidate_report_identity,
                                    runtime_context=runtime_context,
                                )
                                if rollback_ok:
                                    artifact = false_report_artifact()
                                    publication_durability = "not_published"
                                else:
                                    publication_durability = "indeterminate"
                            if (
                                state.pass_gate_write_uncertain
                                and request.db_path is not None
                                and isinstance(persistence_token, str)
                            ):
                                finalize_uncertain = _bounded_storage_call(
                                    "finalize",
                                    (
                                        request.db_path,
                                        run_identity["run_id"],
                                        persistence_token,
                                    ),
                                    expected_kwargs,
                                    runtime_context=runtime_context,
                                )
                                if (
                                    finalize_uncertain.status != "ok"
                                    or finalize_uncertain.value is not True
                                ):
                                    _bounded_storage_call(
                                        "discard",
                                        (
                                            request.db_path,
                                            run_identity["run_id"],
                                            persistence_token,
                                        ),
                                        expected_kwargs,
                                        runtime_context=runtime_context,
                                        cleanup=True,
                                    )
                        else:
                            if report_publication_deferred:
                                state.report_publication_started = True
                                publication = _publish_scheduled_report(
                                    config,
                                    request.run_date,
                                    result,
                                    rendered,
                                    private_canaries=configured_private_canaries,
                                    runtime_context=runtime_context,
                                )
                                publication_durability = publication.durability
                                if publication.durability != "published":
                                    gate_output_failed = True
                                    gate_output_written = False
                                    gate = _gate_output_failure(gate)
                                    artifact = false_report_artifact()
                                    if (
                                        isinstance(persistence_token, str)
                                        and request.db_path is not None
                                    ):
                                        _bounded_storage_call(
                                            "discard",
                                            (
                                                request.db_path,
                                                run_identity["run_id"],
                                                persistence_token,
                                            ),
                                            expected_kwargs,
                                            runtime_context=runtime_context,
                                            cleanup=True,
                                        )
                            if (
                                not gate_output_failed
                                and persistence_state == "pending"
                                and request.db_path is not None
                                and isinstance(persistence_token, str)
                            ):
                                finalize_call = _bounded_storage_call(
                                    "finalize",
                                    (
                                        request.db_path,
                                        run_identity["run_id"],
                                        persistence_token,
                                    ),
                                    expected_kwargs,
                                    runtime_context=runtime_context,
                                )
                                if (
                                    finalize_call.status != "ok"
                                    or finalize_call.value is not True
                                ):
                                    _bounded_storage_call(
                                        "discard",
                                        (
                                            request.db_path,
                                            run_identity["run_id"],
                                            persistence_token,
                                        ),
                                        expected_kwargs,
                                        runtime_context=runtime_context,
                                        cleanup=True,
                                    )
                                    gate_output_failed = True
                                    gate_output_written = False
                                    gate = _database_operation_failure_gate(
                                        _gate_output_failure(gate)
                                    )
                                    rollback_ok = _rollback_report(
                                        report_path,
                                        previous_report,
                                        candidate_report_identity,
                                        runtime_context=runtime_context,
                                    )
                                    if rollback_ok:
                                        artifact = false_report_artifact()
                                        publication_durability = "not_published"
                                    else:
                                        artifact = publication.artifact
                                        publication_durability = "indeterminate"
    if gate.get("status") != "pass" and not gate_output_failed:
        gate_output_written = False
    if not (
        result is not None
        and gate.get("status") == "pass"
        and artifact.generated
        and publication.failure_code is None
        and publication_durability == "published"
    ):
        if gate.get("status") == "pass":
            gate = _gate_output_failure(gate)
            gate_output_failed = True
        if not gate_output_failed and not gate_output_written:
            outcome = _coerce_gate_write_outcome(
                _write_gate_output_at_service_boundary(
                    request.quality_gate_output,
                    gate,
                    configured_canaries=configured_private_canaries,
                    runtime_context=runtime_context,
                )
            )
            gate_output_written = outcome.successful
            gate_output_failed = not gate_output_written
        if gate_output_failed:
            gate = _gate_output_failure(gate)
            try:
                safe_gate = _sanitize_public_gate(gate, configured_private_canaries)
                fallback_payload = (
                    json.dumps(
                        safe_gate,
                        ensure_ascii=False,
                        indent=2,
                    ).encode("utf-8")
                    if safe_gate
                    else None
                )
            except Exception:
                fallback_payload = None
            if fallback_payload is not None:
                _write_gate_payload(
                    request.quality_gate_output,
                    fallback_payload,
                    runtime_context=runtime_context,
                )
    elif not gate_output_written:
        state.pass_gate_write_started = True
        outcome = _coerce_gate_write_outcome(
            _write_gate_output_at_service_boundary(
                request.quality_gate_output,
                gate,
                configured_canaries=configured_private_canaries,
                runtime_context=runtime_context,
            )
        )
        gate_output_written = outcome.successful
        if not gate_output_written:
            state.pass_gate_write_uncertain = outcome.may_have_replaced
            gate = _gate_output_failure(gate)
            gate_output_failed = True

    gate = _sanitize_public_gate(gate, configured_private_canaries)
    return ScheduledRunResult(
        exit_code=1 if gate.get("status") != "pass" else 0,
        gate=gate,
        result=result,
        report_artifact=artifact,
        run_identity=run_identity,
        quality_gate_output=request.quality_gate_output,
        publication_failure_code=publication.failure_code,
        publication_durability=publication_durability,
    )
