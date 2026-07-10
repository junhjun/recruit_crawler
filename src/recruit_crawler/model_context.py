from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Optional, Protocol, Sequence, runtime_checkable

from .schemas import UserContext
from .user_context import (
    MAX_REASONABLE_EXPERIENCE_YEARS,
    _context_from_text,
    _read_document_text,
    _reject_private_text,
    missing_context_fields,
)


class ContextExtractionError(ValueError):
    pass


@dataclass(frozen=True)  # noqa: SLOTS_OK - project supports Python 3.9.
class ModelContextExtraction:
    desired_roles: list[str]
    skills: list[str]
    preferred_locations: list[str]
    max_experience_years: int
    explicit_deal_breakers: list[str] = field(default_factory=list)
    confidence: float = 0.0


class ContextExtractor(Protocol):
    def extract(self, text: str, *, fingerprint: str) -> ModelContextExtraction:
        ...


@runtime_checkable
class ContextFingerprintProvider(Protocol):
    def cache_fingerprint(self, text: str) -> str:
        ...


class ContextExtractionCache(Protocol):
    def get(self, fingerprint: str) -> Optional[ModelContextExtraction]:
        ...

    def set(self, fingerprint: str, extraction: ModelContextExtraction) -> None:
        ...


def context_fingerprint(
    text: str,
    *,
    schema_version: str = "model-context-v1",
    model_id: str = "unspecified",
    effort: str = "unspecified",
) -> str:
    normalized = "\n".join(line.rstrip() for line in text.splitlines()).strip()
    payload = json.dumps(
        {
            "schema_version": schema_version,
            "model_id": model_id,
            "effort": effort,
            "text": normalized,
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def context_from_model_payload(payload: ModelContextExtraction, *, provenance: str) -> UserContext:
    if not isinstance(payload, ModelContextExtraction):
        raise ContextExtractionError("model context extraction must match ModelContextExtraction")
    max_experience_years = (
        payload.max_experience_years
        if 0 < payload.max_experience_years <= MAX_REASONABLE_EXPERIENCE_YEARS
        else 0
    )
    context = UserContext(
        desired_roles=_unique_nonempty(payload.desired_roles),
        skills=_unique_nonempty(payload.skills),
        preferred_locations=_unique_nonempty(payload.preferred_locations),
        max_experience_years=max_experience_years,
        explicit_deal_breakers=_unique_nonempty(payload.explicit_deal_breakers),
        provenance={
            "desired_roles": provenance,
            "skills": provenance,
            "preferred_locations": provenance,
            "max_experience_years": provenance,
            "explicit_deal_breakers": provenance,
        },
    )
    return UserContext(
        desired_roles=context.desired_roles,
        skills=context.skills,
        preferred_locations=context.preferred_locations,
        max_experience_years=context.max_experience_years,
        explicit_deal_breakers=context.explicit_deal_breakers,
        missing_context=missing_context_fields(context),
        provenance=context.provenance,
        private_canaries=context.private_canaries,
    )


def parse_context_document_with_extractor(
    path: Path,
    extractor: ContextExtractor,
    *,
    cache: Optional[ContextExtractionCache] = None,
) -> UserContext:
    return parse_context_documents_with_extractor([path], extractor, cache=cache)


def parse_context_documents_with_extractor(
    paths: Sequence[Path],
    extractor: ContextExtractor,
    *,
    cache: Optional[ContextExtractionCache] = None,
) -> UserContext:
    documents = [_read_context_document(path, index) for index, path in enumerate(paths, start=1)]
    aggregate_text = "\n\n".join(documents)
    fingerprint = (
        extractor.cache_fingerprint(aggregate_text)
        if isinstance(extractor, ContextFingerprintProvider)
        else context_fingerprint(aggregate_text)
    )
    try:
        extraction = cache.get(fingerprint) if cache is not None else None
        if extraction is None:
            extraction = extractor.extract(aggregate_text, fingerprint=fingerprint)
            from .codex_thread_context import validate_model_context_extraction

            extraction = validate_model_context_extraction(extraction, source_text=aggregate_text)
            if cache is not None:
                cache.set(fingerprint, extraction)
        return context_from_model_payload(extraction, provenance="model_context.schema")
    except ContextExtractionError:
        contexts = [_context_from_text(document) for document in documents]
        context = _merge_model_fallback_contexts(contexts)
        return replace(context, missing_context=missing_context_fields(context))


def _read_context_document(path: Path, index: int) -> str:
    text = _read_document_text(path)
    _reject_private_text(text)
    suffix = path.suffix.lower() or "<none>"
    return f"--- context_document_{index} type={suffix} ---\n{text.strip()}"


def _merge_model_fallback_contexts(contexts: Sequence[UserContext]) -> UserContext:
    from .user_context import merge_user_contexts

    return merge_user_contexts(contexts)


def _unique_nonempty(values: Sequence[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for value in values:
        normalized = " ".join(value.split())
        key = normalized.casefold()
        if normalized and key not in seen:
            result.append(normalized)
            seen.add(key)
    return result
