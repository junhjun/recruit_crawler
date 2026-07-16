from __future__ import annotations

import json
import re
import sys
import time
import unicodedata
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence
from urllib.request import Request, urlopen

from .sources.platform_saramin import (
    _clean_visible_text,
    _saramin_detail_company,
    _saramin_json_ld_values,
    _section_between,
)

OUTER_URL = "https://www.saramin.co.kr/zf_user/jobs/relay/view?rec_idx={rec_idx}&rec_seq=0"
DETAIL_URL = "https://www.saramin.co.kr/zf_user/jobs/relay/view-detail?rec_idx={rec_idx}&rec_seq=0"
OUTPUT_PREFIX = "/tmp/recruit-crawler-saramin-probe-"
SUMMARY_FILENAME = "summary.json"
SUMMARY_SCHEMA_VERSION = 1
_PAIR_COUNTERS = (
    "pairs_requested",
    "pairs_outer_sufficient",
    "outer_fetch_failed",
    "detail_fetch_failed",
    "redirect_or_endpoint_mismatch",
    "title_mismatch",
    "company_missing_or_placeholder_or_mismatch",
    "responsibilities_mismatch",
    "required_mismatch",
    "preferred_mismatch",
)
_PLACEHOLDER_COMPANIES = frozenset({"", "회사명 확인 필요", "saramin", "사람인", "사람인 채용"})
_TITLE_HEADER_RE = re.compile(
    r"<p\b[^>]*class\s*=\s*['\"][^'\"]*\bjob-header__title\b[^'\"]*['\"][^>]*>(.*?)</p>",
    re.I | re.S,
)
_META_RE = re.compile(r"<meta\b[^>]*>", re.I)


class ProbeAuthorizationError(ValueError):
    """The diagnostic command was not registered with its exact authorization."""


@dataclass(frozen=True, slots=True)
class ProbeRequest:
    rec_indices: tuple[str, str, str]
    output_dir: Path


@dataclass(frozen=True, slots=True)
class ProbeResponse:
    final_url: str
    text: str


@dataclass(frozen=True, slots=True)
class _Document:
    title: str
    company: str
    responsibilities: str
    required: str
    preferred: str


def _bad(message: str) -> ProbeAuthorizationError:
    return ProbeAuthorizationError(message)


def _validate_output_dir(value: str) -> Path:
    if not re.fullmatch(r"/tmp/recruit-crawler-saramin-probe-[^/\r\n]+", value):
        raise _bad("--output-dir must match /tmp/recruit-crawler-saramin-probe-*")
    return Path(value)


def parse_probe_args(argv: Sequence[str]) -> ProbeRequest:
    """Parse and authorize the deliberately narrow diagnostic command arguments."""
    authorized = 0
    rec_indices: list[str] = []
    output_dir: Path | None = None
    index = 0
    values = list(argv)
    while index < len(values):
        token = values[index]
        if token == "--authorized-live-probe":
            authorized += 1
            index += 1
            continue
        if token == "--rec-idx":
            if index + 1 >= len(values):
                raise _bad("--rec-idx requires an ID")
            rec_idx = values[index + 1]
            if not (1 <= len(rec_idx) <= 12 and rec_idx.isascii() and rec_idx.isdecimal()):
                raise _bad("--rec-idx must be an ASCII decimal ID of 1-12 digits")
            normalized = str(int(rec_idx))
            if normalized in rec_indices:
                raise _bad("--rec-idx values must be distinct")
            rec_indices.append(normalized)
            index += 2
            continue
        if token == "--output-dir":
            if index + 1 >= len(values):
                raise _bad("--output-dir requires a path")
            if output_dir is not None:
                raise _bad("--output-dir may be supplied only once")
            output_dir = _validate_output_dir(values[index + 1])
            index += 2
            continue
        raise _bad("unapproved argument")
    if authorized != 1:
        raise _bad("--authorized-live-probe is required exactly once")
    if len(rec_indices) != 3:
        raise _bad("exactly three distinct --rec-idx values are required")
    if output_dir is None:
        raise _bad("--output-dir is required")
    return ProbeRequest((rec_indices[0], rec_indices[1], rec_indices[2]), output_dir)


def _normalize(value: object) -> str:
    return " ".join(unicodedata.normalize("NFC", str(value)).split()).casefold()


def _tokens(value: object) -> set[str]:
    return set(re.findall(r"[^\W_]+", _normalize(value), flags=re.UNICODE))


def _strip_tags(value: str) -> str:
    return re.sub(r"<[^>]+>", " ", value)


def _json_ld_title(html: str) -> str:
    for payload in _saramin_json_ld_values(html):
        queue = [payload]
        while queue:
            value = queue.pop(0)
            if isinstance(value, Mapping):
                title = value.get("title")
                if isinstance(title, str) and title.strip():
                    return title
                queue.extend(value.values())
            elif isinstance(value, list):
                queue.extend(value)
    return ""


def _meta_content(html: str, key: str) -> str:
    for tag in _META_RE.findall(html):
        property_match = re.search(r"\b(?:property|name)\s*=\s*['\"]([^'\"]+)['\"]", tag, re.I)
        if property_match and property_match.group(1).strip().casefold() == key.casefold():
            content_match = re.search(r"\bcontent\s*=\s*['\"]([^'\"]*)['\"]", tag, re.I)
            if content_match:
                return content_match.group(1)
    return ""


def _title(html: str, text: str) -> str:
    for value in (
        *(_strip_tags(item) for item in _TITLE_HEADER_RE.findall(html)),
        _meta_content(html, "og:title"),
        _json_ld_title(html),
    ):
        value = " ".join(value.split())
        if value:
            return value
    return next((line.strip() for line in text.splitlines() if line.strip()), "")


def _parse_document(html: str) -> _Document:
    text = _clean_visible_text(html)
    return _Document(
        title=_title(html, text),
        company=_saramin_detail_company(html),
        responsibilities=_section_between(
            text,
            ["주요업무", "담당업무"],
            ["자격요건", "지원자격", "우대사항", "마감일 및 근무지", "복지 및 혜택"],
        ),
        required=_section_between(
            text,
            ["자격요건", "지원자격"],
            ["우대사항", "마감일 및 근무지", "복지 및 혜택", "채용절차"],
        ),
        preferred=_section_between(
            text,
            ["우대사항"],
            ["마감일 및 근무지", "복지 및 혜택", "채용절차"],
        ),
    )


def _response_parts(response: Any) -> ProbeResponse:
    if isinstance(response, ProbeResponse):
        return response
    if isinstance(response, Mapping):
        final_url = response.get("final_url", response.get("url"))
        text = response.get("text", response.get("body", ""))
    elif isinstance(response, tuple) and len(response) == 2:
        final_url, text = response
    else:
        final_url = getattr(response, "final_url", getattr(response, "url", None))
        text = getattr(response, "text", getattr(response, "body", ""))
    if not isinstance(final_url, str) or not isinstance(text, str):
        raise ValueError("invalid probe response")
    return ProbeResponse(final_url, text)


def _default_fetch(url: str) -> ProbeResponse:
    request = Request(url, headers={"User-Agent": "recruit-crawler/saramin-strategy-probe"})
    with urlopen(request, timeout=8) as response:
        body = response.read().decode("utf-8", errors="replace")
        return ProbeResponse(response.geturl(), body)


def _pair_counter_defaults() -> dict[str, int]:
    return {name: 0 for name in _PAIR_COUNTERS}


def _company_sufficient(outer: str, detail: str) -> bool:
    outer_norm = _normalize(outer)
    detail_norm = _normalize(detail)
    return bool(outer_norm and detail_norm and outer_norm == detail_norm and outer_norm not in _PLACEHOLDER_COMPANIES)


def _section_sufficient(outer: str, detail: str) -> bool:
    return bool(_normalize(outer) and _normalize(detail) and _tokens(outer) & _tokens(detail))


def run_probe(
    request: ProbeRequest,
    *,
    fetch: Callable[[str], Any] = _default_fetch,
    clock: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
) -> dict[str, Any]:
    """Run at most three sequential outer/detail pairs and return fixed redacted fields."""
    started = clock()
    counters = _pair_counter_defaults()
    counters["pairs_requested"] = 3
    previous_start: float | None = None

    def start_request() -> bool:
        nonlocal previous_start
        if clock() - started >= 50.0:
            return False
        if previous_start is not None:
            wait = 1.0 - (clock() - previous_start)
            if wait > 0:
                sleep(wait)
        previous_start = clock()
        return True

    for rec_idx in request.rec_indices:
        outer_url = OUTER_URL.format(rec_idx=rec_idx)
        detail_url = DETAIL_URL.format(rec_idx=rec_idx)
        outer_response: ProbeResponse | None = None
        detail_response: ProbeResponse | None = None
        if start_request():
            try:
                outer_response = _response_parts(fetch(outer_url))
            except Exception:
                counters["outer_fetch_failed"] += 1
        else:
            counters["outer_fetch_failed"] += 1
        if start_request():
            try:
                detail_response = _response_parts(fetch(detail_url))
            except Exception:
                counters["detail_fetch_failed"] += 1
        else:
            counters["detail_fetch_failed"] += 1
        if outer_response is None or detail_response is None:
            continue
        if outer_response.final_url != outer_url or detail_response.final_url != detail_url:
            counters["redirect_or_endpoint_mismatch"] += 1
            continue
        try:
            outer = _parse_document(outer_response.text)
            detail = _parse_document(detail_response.text)
        except Exception:
            counters["title_mismatch"] += 1
            counters["company_missing_or_placeholder_or_mismatch"] += 1
            counters["responsibilities_mismatch"] += 1
            counters["required_mismatch"] += 1
            counters["preferred_mismatch"] += 1
            continue
        sufficient = True
        if not (
            _normalize(outer.title)
            and _normalize(detail.title)
            and len(_normalize(outer.title)) <= 120
            and len(_normalize(detail.title)) <= 120
            and _normalize(outer.title) == _normalize(detail.title)
        ):
            counters["title_mismatch"] += 1
            sufficient = False
        if not _company_sufficient(outer.company, detail.company):
            counters["company_missing_or_placeholder_or_mismatch"] += 1
            sufficient = False
        for name, outer_value, detail_value in (
            ("responsibilities_mismatch", outer.responsibilities, detail.responsibilities),
            ("required_mismatch", outer.required, detail.required),
            ("preferred_mismatch", outer.preferred, detail.preferred),
        ):
            if not _section_sufficient(outer_value, detail_value):
                counters[name] += 1
                sufficient = False
        if sufficient:
            counters["pairs_outer_sufficient"] += 1
    elapsed_ms = max(0, int(round((clock() - started) * 1000)))
    decision = "outer_only" if counters["pairs_outer_sufficient"] == 3 and all(
        counters[name] == 0 for name in _PAIR_COUNTERS[2:]
    ) else "detail_only"
    return {
        "schema_version": SUMMARY_SCHEMA_VERSION,
        "counters": counters,
        "decision": decision,
        "elapsed_ms": elapsed_ms,
    }


def write_summary(output_dir: Path, summary: Mapping[str, Any]) -> Path:
    output_dir.mkdir(parents=True, exist_ok=True)
    path = output_dir / SUMMARY_FILENAME
    redacted = {
        "schema_version": SUMMARY_SCHEMA_VERSION,
        "counters": {name: int(summary.get("counters", {}).get(name, 0)) for name in _PAIR_COUNTERS},
        "decision": summary.get("decision", "detail_only") if summary.get("decision") in {"outer_only", "detail_only"} else "detail_only",
        "elapsed_ms": max(0, int(summary.get("elapsed_ms", 0))),
    }
    path.write_text(json.dumps(redacted, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return path


def main(
    argv: Sequence[str] | None = None,
    *,
    fetch: Callable[[str], Any] = _default_fetch,
    clock: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
) -> int:
    try:
        request = parse_probe_args(list(sys.argv[1:] if argv is None else argv))
    except ProbeAuthorizationError as exc:
        print(f"saramin-strategy-probe: {exc}", file=sys.stderr)
        return 64
    try:
        summary = run_probe(request, fetch=fetch, clock=clock, sleep=sleep)
        write_summary(request.output_dir, summary)
    except Exception:
        failure_summary = {
            "schema_version": SUMMARY_SCHEMA_VERSION,
            "counters": _pair_counter_defaults(),
            "decision": "detail_only",
            "elapsed_ms": 0,
        }
        try:
            write_summary(request.output_dir, failure_summary)
        except Exception:
            pass
        print("saramin-strategy-probe: internal failure", file=sys.stderr)
        return 2
    return 0 if summary["decision"] == "outer_only" else 1
