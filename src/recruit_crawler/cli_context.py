from __future__ import annotations

import argparse
from dataclasses import dataclass
from typing import TYPE_CHECKING, Optional

from .config import ConfigError, apply_context_documents, apply_supplemental_answers, load_config
from .context_doctor import ContextDoctorRequest, context_doctor_question, context_doctor_question_fields
from .schemas import AppConfig
from .user_context import missing_context_fields, supplemental_questions

if TYPE_CHECKING:
    from .model_context import ContextExtractionCache, ContextExtractor


@dataclass(frozen=True)  # noqa: SLOTS_OK - project supports Python 3.9.
class ContextExtractionRuntime:
    extractor: "ContextExtractor"
    cache: "ContextExtractionCache"


def apply_supplemental_interview(config: AppConfig) -> AppConfig:
    missing_fields = missing_context_fields(config.user_context)
    if not missing_fields:
        return config
    questions = supplemental_questions(config.user_context)
    answers: dict[str, str] = {}
    print("Supplemental context interview:")
    for field, question in zip(missing_fields, questions):
        try:
            answer = input(f"- {question}\n> ")
        except EOFError as exc:
            raise ConfigError(f"missing context requires supplemental answer for {field}") from exc
        if answer.strip():
            answers[field] = answer.strip()
    if not answers:
        return config
    return apply_supplemental_answers(config, answers)


def load_config_with_context(
    args: argparse.Namespace,
    *,
    allow_real_sources: bool,
    interview: bool,
    model_context: Optional[ContextExtractionRuntime] = None,
) -> AppConfig:
    config = load_config(args.config, allow_real_sources=allow_real_sources)
    if args.context_doc:
        if model_context is None:
            config = apply_context_documents(config, args.context_doc)
        else:
            config = apply_context_documents(
                config,
                args.context_doc,
                extractor=model_context.extractor,
                cache=model_context.cache,
            )
    if interview:
        config = apply_supplemental_interview(config)
    return config


def context_doctor_answers(args: argparse.Namespace) -> dict[str, str]:
    config = load_config(args.config, allow_real_sources=True)
    request = ContextDoctorRequest(
        config=config,
        context_docs=args.context_doc or [],
        output=args.output,
    )
    question_fields = context_doctor_question_fields(request)
    if not question_fields:
        return {}
    print("Context onboarding interview:")
    answers: dict[str, str] = {}
    for field in question_fields:
        question = context_doctor_question(field)
        try:
            answer = input(f"- {question}\n> ")
        except EOFError as exc:
            raise ConfigError(f"missing context requires supplemental answer for {field}") from exc
        if answer.strip():
            answers[field] = answer.strip()
    return answers
