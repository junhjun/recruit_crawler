from __future__ import annotations

import argparse
from contextlib import ExitStack
import inspect
import json
import socket
import threading
import sys
import time
from datetime import date
from pathlib import Path
from typing import Any

from .cli_context import load_config_with_context
from .config import ConfigError
from .gate import build_gate_v4, canonical_gate_v4_bytes
from .pipeline import build_live_run_preflight_gate, run_live_run
from .projection import false_report_artifact, project_pipeline_result
from .report_writer import (
    ReportPublicationResultV1,
    RuntimeContext,
    persist_rendered_report,
    validate_rendered_report,
)
from .schemas import PipelineResultV4, ReportArtifactV2
from .summarizer import render_report_v2
from .scheduled import (
    _gate_outcome_is_durable,
    _rollback_report,
    _write_gate_output_at_service_boundary,
    _scheduled_output_locks,
)

def _report_path(config, run_date: date) -> Path:
    return Path(config.output_dir) / f"recruiting-live-run-{run_date.isoformat()}.md"


def _capture_report_preimage(path: Path) -> tuple[bool, bytes | None]:
    try:
        if not path.exists():
            return False, None
        return True, path.read_bytes()
    except OSError as exc:
        raise OSError("live report preimage unreadable") from exc

def _build_live_gate(
    result: PipelineResultV4,
    *,
    enabled_source_ids,
    context_status: str,
    runtime_failures=(),
    report_artifact=None,
    projection=None,
    configured_canaries=(),
) -> dict[str, Any]:
    if not isinstance(result, PipelineResultV4):
        raise TypeError("live service boundary requires PipelineResultV4")
    gate = build_gate_v4(
        result,
        enabled_source_ids=tuple(enabled_source_ids),
        context_status=context_status,
        runtime_failures=runtime_failures,
        report_artifact=report_artifact,
        projection=projection,
        configured_canaries=configured_canaries,
    )
    canonical_gate_v4_bytes(gate)
    return gate

def _render_live_report(
    result: PipelineResultV4,
    *,
    private_canaries=(),
    runtime_context: RuntimeContext | None = None,
) -> tuple[Any | None, ReportPublicationResultV1]:
    if runtime_context is not None and runtime_context.expired:
        return None, ReportPublicationResultV1(
            false_report_artifact(), "runtime_deadline_exceeded", "not_published"
        )
    try:
        rendered = render_report_v2(result, private_canaries=private_canaries)
        validate_rendered_report(rendered, private_canaries=private_canaries)
    except Exception:
        return None, ReportPublicationResultV1(
            false_report_artifact(), "render_failed", "not_published"
        )
    return rendered, ReportPublicationResultV1(
        false_report_artifact(), None, "not_published"
    )


def _publish_live_report(
    config, run_date: date, result: PipelineResultV4, rendered=None, *,
    private_canaries=(),
    runtime_context: RuntimeContext | None = None,
) -> ReportPublicationResultV1:
    if rendered is None:
        rendered, failed = _render_live_report(
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
        report_slug="recruiting-live-run",
        private_canaries=private_canaries,
        runtime_context=runtime_context,
    )


def _configured_private_canaries(config) -> tuple[str, ...]:
    return tuple(
        dict.fromkeys(
            canary
            for canary in (
                *getattr(getattr(config, "profile", None), "private_canaries", ()),
                *getattr(getattr(config, "user_context", None), "private_canaries", ()),
            )
            if isinstance(canary, str) and canary
        )
    )


def _live_network_findings(
    config, runtime_context: RuntimeContext
) -> list[dict[str, object]]:
    checked: set[str] = set()
    for source in config.sources:
        if not source.enabled or source.access_mode == "fixture":
            continue
        for domain in source.domains:
            if domain in checked:
                continue
            checked.add(domain)
            if runtime_context.expired:
                return [
                    {
                        "severity": "fail",
                        "source_id": None,
                        "message": "live network preflight failed",
                    }
                ]
            try:
                errors: list[Exception] = []

                def resolve() -> None:
                    try:
                        socket.getaddrinfo(domain, 443)
                    except Exception as exc:
                        errors.append(exc)

                worker = threading.Thread(target=resolve, daemon=True)
                worker.start()
                worker.join(
                    min(runtime_context.remaining(), runtime_context.hard_remaining())
                )
                if worker.is_alive():
                    return [
                        {
                            "severity": "fail",
                            "source_id": None,
                            "message": "live network preflight interrupted",
                        }
                    ]
                if errors:
                    raise errors[0]
            except OSError:
                return [
                    {
                        "severity": "fail",
                        "source_id": None,
                        "message": "live network preflight failed",
                    }
                ]
    return []


def _run_live_run_at_service_boundary(
    config,
    run_date: date,
    *,
    coordinator=None,
    runtime_context: RuntimeContext | None = None,
) -> PipelineResultV4:
    target = getattr(run_live_run, "side_effect", None)
    if not callable(target):
        target = run_live_run
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
    result = run_live_run(config, run_date, **kwargs)
    if not isinstance(result, PipelineResultV4):
        raise TypeError("live service boundary requires PipelineResultV4")
    return result


def _live_failure_gate(preflight: dict[str, Any], message: str) -> dict[str, Any]:
    return {
        **preflight,
        "status": "fail",
        "findings": [
            *preflight.get("findings", ()),
            {"severity": "fail", "source_id": None, "message": message},
        ],
    }

_PARTIAL_SOURCE_FAILURE_MESSAGES = frozenset(
    {
        "enabled source accepted zero candidates",
        "enabled source execution did not complete successfully",
    }
)


def _candidate_gate_allows_partial_publication(gate: dict[str, Any]) -> bool:
    """Allow only source-degraded failures to publish a validated report."""
    failures = [
        finding
        for finding in gate.get("findings", ())
        if finding.get("severity") == "fail"
    ]
    return all(
        finding.get("source_id")
        and finding.get("message") in _PARTIAL_SOURCE_FAILURE_MESSAGES
        for finding in failures
    )


def handle_live_run(
    args: argparse.Namespace,
    parser: argparse.ArgumentParser,
    *,
    coordinator=None,
    runtime_context: RuntimeContext | None = None,
) -> int:
    runtime_context = runtime_context or RuntimeContext.start(
        command_mode="live-run",
        monotonic=time.monotonic,
    )
    output_locks = ExitStack()
    try:
        run_date = _parse_date(args.run_date)
        config = load_config_with_context(
            args,
            allow_real_sources=True,
            interview=args.interview_missing_context,
        )
        configured_canaries = _configured_private_canaries(config)
        preflight = build_live_run_preflight_gate(run_date, config)
        network_findings = (
            []
            if preflight["status"] == "fail"
            else _live_network_findings(config, runtime_context)
        )
        if network_findings:
            preflight = {
                **preflight,
                "status": "fail",
                "findings": [*preflight.get("findings", ()), *network_findings],
            }
        if runtime_context.expired and preflight["status"] != "fail":
            preflight = _live_failure_gate(preflight, "live preflight deadline exceeded")
        artifact = false_report_artifact()
        report_path = _report_path(config, run_date)
        report_preimage = None
        preimage_error = False
        report_published = False
        report_unknown = False
        gate_output_written = False
        if args.quality_gate_output:
            output_locks.enter_context(
                _scheduled_output_locks(
                    (report_path, args.quality_gate_output),
                    runtime_context=runtime_context,
                )
            )
            try:
                report_preimage = _capture_report_preimage(report_path)
            except OSError:
                preimage_error = True
        gate = preflight
        result = None
        if preflight["status"] != "fail":
            try:
                result = _run_live_run_at_service_boundary(
                    config,
                    run_date,
                    coordinator=coordinator,
                    runtime_context=runtime_context,
                )
            except Exception:
                gate = _live_failure_gate(preflight, "live collection failed")
            else:
                projection = project_pipeline_result(result)
                rendered, render_publication = _render_live_report(
                    result,
                    private_canaries=configured_canaries,
                    runtime_context=runtime_context,
                )
                candidate = (
                    ReportArtifactV2(
                        schema_version=2,
                        generated=True,
                        path=str(
                            Path(config.output_dir)
                            / f"recruiting-live-run-{run_date.isoformat()}.md"
                        ),
                        rendered=rendered,
                    )
                    if rendered is not None
                    else false_report_artifact()
                )
                enabled_source_ids = (
                    source.source_id
                    for source in config.sources
                    if source.enabled
                )
                candidate_gate = _build_live_gate(
                    result,
                    enabled_source_ids=enabled_source_ids,
                    context_status=preflight["context_status"],
                    report_artifact=candidate,
                    projection=projection,
                    configured_canaries=configured_canaries,
                )
                if preimage_error:
                    rendered = None
                    candidate_gate = _build_live_gate(
                        result,
                        enabled_source_ids=(
                            source.source_id for source in config.sources if source.enabled
                        ),
                        context_status=preflight["context_status"],
                        runtime_failures=("live_report_preimage_unreadable",),
                        report_artifact=false_report_artifact(),
                        projection=projection,
                        configured_canaries=configured_canaries,
                    )
                if rendered is None and not preimage_error:
                    candidate_gate = _build_live_gate(
                        result,
                        enabled_source_ids=(
                            source.source_id
                            for source in config.sources
                            if source.enabled
                        ),
                        context_status=preflight["context_status"],
                        runtime_failures=("live_report_render_failed",),
                        report_artifact=candidate,
                        projection=projection,
                        configured_canaries=configured_canaries,
                    )
                if rendered is None:
                    gate = candidate_gate
                elif not _candidate_gate_allows_partial_publication(candidate_gate):
                    gate = _build_live_gate(
                        result,
                        enabled_source_ids=(
                            source.source_id
                            for source in config.sources
                            if source.enabled
                        ),
                        context_status=preflight["context_status"],
                        report_artifact=false_report_artifact(),
                        projection=projection,
                        configured_canaries=configured_canaries,
                    )
                else:
                    publication = _publish_live_report(
                        config,
                        run_date,
                        result,
                        rendered,
                        private_canaries=configured_canaries,
                        runtime_context=runtime_context,
                    )
                    if publication.durability == "published":
                        report_published = True
                        artifact = publication.artifact
                        gate = _build_live_gate(
                            result,
                            enabled_source_ids=(
                                source.source_id
                                for source in config.sources
                                if source.enabled
                            ),
                            context_status=preflight["context_status"],
                            report_artifact=artifact,
                            projection=projection,
                            configured_canaries=configured_canaries,
                        )
                    else:
                        artifact = false_report_artifact()
                        gate = _build_live_gate(
                            result,
                            enabled_source_ids=(
                                source.source_id
                                for source in config.sources
                                if source.enabled
                            ),
                            context_status=preflight["context_status"],
                            runtime_failures=("live_report_publication_failed",),
                            report_artifact=artifact,
                            projection=projection,
                            configured_canaries=configured_canaries,
                        )
        if args.quality_gate_output:
            gate_outcome = _write_gate_output_at_service_boundary(
                args.quality_gate_output,
                gate,
                configured_canaries=configured_canaries,
                runtime_context=runtime_context,
            )
            if _gate_outcome_is_durable(gate_outcome):
                gate_output_written = True
            elif getattr(gate_outcome, "status", None) == "uncertain":
                # The candidate Gate may already be visible. Keep its matching
                # report and make the transaction explicitly indeterminate.
                report_unknown = report_published
                gate = _live_failure_gate(
                    gate, "live quality gate output is indeterminate"
                )
            else:
                rollback_ok = True
                if report_published:
                    expected_identity = (
                        artifact.rendered.content_sha256
                        if artifact.rendered is not None
                        else None
                    )
                    rollback_ok = _rollback_report(
                        report_path,
                        report_preimage,
                        expected_identity,
                        runtime_context=runtime_context,
                    )
                    artifact = false_report_artifact()
                    report_published = False
                if rollback_ok:
                    gate = _live_failure_gate(
                        gate, "live quality gate output failed; report rolled back"
                    )
                else:
                    report_unknown = True
                    gate = _live_failure_gate(
                        gate,
                        "live quality gate output failed; report publication state unknown",
                    )
        output_locks.close()
    except (ConfigError, ValueError, FileNotFoundError, OSError) as exc:
        output_locks.close()
        parser.error(str(exc))
        return 2

    diagnostic = sys.stderr if args.print_report else sys.stdout
    if args.print_report and artifact.generated and artifact.rendered is not None:
        stream = getattr(sys.stdout, "buffer", None)
        if stream is not None:
            stream.write(artifact.rendered.markdown_bytes)
            stream.flush()
        else:
            sys.stdout.write(artifact.rendered.markdown_bytes.decode("utf-8"))
            sys.stdout.flush()
    if result is None:
        print("Live run blocked", file=diagnostic)
        print("Report written: not generated", file=diagnostic)
    elif report_unknown:
        print("Live run failed", file=diagnostic)
        print("Report written: publication state unknown", file=diagnostic)
    elif not artifact.generated:
        print("Live run failed", file=diagnostic)
        print("Report written: not generated", file=diagnostic)
    else:
        print(f"Report written: {artifact.path}", file=diagnostic)
    print(f"Live-run quality gate status: {gate['status']}", file=diagnostic)
    if args.quality_gate_output:
        if gate_output_written:
            print(
                f"Live-run quality gate written: {args.quality_gate_output}",
                file=diagnostic,
            )
        else:
            print("Live-run quality gate written: not generated", file=diagnostic)
    return 1 if gate["status"] != "pass" else 0


def _parse_date(value: str | None) -> date:
    if not value:
        return date.today()
    return date.fromisoformat(value)
