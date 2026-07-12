from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import List

from .schemas import PostingCandidate


class CaptureImportError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class CaptureImportSelection:
    files: List[Path]
    run_date: date


@dataclass(frozen=True, slots=True)
class CaptureImportResult:
    candidates: List[PostingCandidate]
    sources_attempted: List[str]
    source_errors: List[str] = field(default_factory=list)
    skipped_files: List[str] = field(default_factory=list)
