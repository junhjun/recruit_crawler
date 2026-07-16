from __future__ import annotations

from pathlib import Path
from typing import Any, List, Protocol

from ..schemas import CandidateDetailIssueV2, PostingCandidate, SourceManifest


class SourceAdapterConfigurationError(ValueError):
    pass


class SourceAdapter(Protocol):
    manifest: SourceManifest
    issues: List[CandidateDetailIssueV2]

    def collect(self) -> List[PostingCandidate]:
        """Return normalized candidates without performing work outside the adapter."""


def initialize_source_adapter(adapter: Any) -> None:
    """Initialize the adapter-bound typed candidate issue transport."""
    adapter.issues = []


class LocalJsonSourceAdapter:
    def __init__(self, manifest: SourceManifest, path: Path):
        if manifest.access_mode not in {"fixture", "manual"}:
            raise SourceAdapterConfigurationError(
                f"unsupported local source access mode: {manifest.access_mode}"
            )
        self.manifest = manifest
        self.path = path
        initialize_source_adapter(self)

    def collect(self) -> List[PostingCandidate]:
        from .fixture import load_fixture_postings

        return load_fixture_postings(self.path)


def build_source_adapter(manifest: SourceManifest, fixture_path: Path) -> SourceAdapter:
    if manifest.access_mode in {"fixture", "manual"}:
        return LocalJsonSourceAdapter(manifest, fixture_path)
    if manifest.access_mode in {"public_page", "feed", "api", "browser_automation"}:
        from .platforms import PLATFORM_ADAPTERS

        adapter_class = PLATFORM_ADAPTERS.get(manifest.source_id)
        if adapter_class:
            adapter = adapter_class(manifest)
            initialize_source_adapter(adapter)
            return adapter

        from .http import PublicJobsHttpAdapter

        adapter = PublicJobsHttpAdapter(manifest)
        initialize_source_adapter(adapter)
        return adapter
    raise SourceAdapterConfigurationError(f"unsupported source access mode: {manifest.access_mode}")
