from __future__ import annotations

"""Runtime-only canonicalization and identity helpers for pipeline v2.

No value returned by this module is a persistence or reporting contract.  In
particular, candidate bytes and identity bases must stay inside the pipeline.
"""

import hashlib
import html
import ipaddress
import json
import re
import unicodedata
from dataclasses import dataclass
from datetime import date, datetime
from typing import Any, Iterable, Mapping, Optional, Sequence
from urllib.parse import urlsplit

from .schemas import CandidateV2, SnapshotV2

_SOURCE_ID_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")
_SAFE_PATH = frozenset("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789/:%-._~!$&'()*+,;=@")
_HEX = frozenset("0123456789abcdefABCDEF")
_TEXT_ESCAPE_PUNCTUATION = r"""!#$%&()*+,-./:;<=>?@[\]^_`{|}~"""
_TEXT_ESCAPES = {
    **{char: char for char in _TEXT_ESCAPE_PUNCTUATION},
    "n": "\n",
    "r": "\r",
    "t": "\t",
    '"': '"',
    "'": "'",
}
_RAW_FIELDS = frozenset(
    {
        "required_qualifications",
        "preferred_qualifications",
        "responsibilities",
        "company_info",
        "experience_tags",
        "manual_review_flags",
    }
)


class IdentityError(ValueError):
    """Raised when an ingress value cannot be canonicalized safely."""


class CandidateRejected(IdentityError):
    """Raised when a candidate cannot enter the v2 pipeline."""


@dataclass(frozen=True, slots=True)
class NormalizationInfo:
    changed_fields: int = 0
    emptied_fields: int = 0
    error_codes: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class NormalizedCandidate:
    candidate: CandidateV2
    info: NormalizationInfo


def _text(value: Any) -> str:
    value = html.unescape(str(value))
    # Decode only a single escape introducer.  A literal ``\\n`` remains
    # literal, rather than being turned into a line break on a second pass.
    # Source pages also commonly protect visible ASCII punctuation (for
    # example, ``\[주니어\]`` and ``\, 서울``); those escapes are not part of
    # the public posting text.
    output: list[str] = []
    index = 0
    while index < len(value):
        char = value[index]
        if char == "\\" and index + 1 < len(value) and value[index + 1] in _TEXT_ESCAPES:
            slash_count = 0
            cursor = index
            while cursor >= 0 and value[cursor] == "\\":
                slash_count += 1
                cursor -= 1
            if slash_count % 2:
                output.append(_TEXT_ESCAPES[value[index + 1]])
                index += 2
                continue
        output.append(char)
        index += 1
    return unicodedata.normalize("NFC", "".join(output))


def normalize_scalar(value: Any, *, allow_none: bool = True) -> Optional[str]:
    if value is None:
        return None if allow_none else ""
    if isinstance(value, (dict, set, bytes)):
        raise IdentityError("scalar value must be text")
    normalized = " ".join(_text(value).split()).strip()
    return normalized or (None if allow_none else "")


def normalize_list(value: Any) -> tuple[str, ...]:
    if value is None:
        return ()
    if isinstance(value, (str, int, float)):
        values: Iterable[Any] = (value,)
    elif isinstance(value, (list, tuple)):
        values = value
    else:
        raise IdentityError("list value must be text or a list")
    return tuple(item for raw in values if (item := normalize_scalar(raw)) is not None)


def normalize_source_id(value: Any) -> str:
    normalized = normalize_scalar(value, allow_none=False)
    # Source IDs are intentionally ASCII-only after normalization; accepting
    # a transliterated or codec-derived spelling would create a new identity.
    if not normalized or not normalized.isascii() or not _SOURCE_ID_RE.fullmatch(normalized.lower()):
        raise IdentityError("invalid source_id")
    return normalized.lower()
def normalize_source_ids(values: Iterable[Any]) -> tuple[str, ...]:
    normalized = tuple(normalize_source_id(value) for value in values)
    if len(set(normalized)) != len(normalized):
        raise IdentityError("duplicate normalized source_id")
    return normalized




def normalize_identifier(value: Any) -> Optional[str]:
    if isinstance(value, str) and any(ord(char) < 32 or ord(char) == 127 for char in value):
        return None
    normalized = normalize_scalar(value)
    if normalized is None:
        return None
    if (
        len(normalized) > 256
        or any(ord(char) < 32 or ord(char) == 127 for char in normalized)
    ):
        return None
    return normalized


def _idna_host(host: str) -> str:
    try:
        import idna  # type: ignore
    except ImportError:
        # Do not use Python's legacy IDNA codec as a fallback.
        if not host.isascii():
            raise IdentityError("unicode host requires UTS-46 IDNA support")
        return host.lower()
    try:
        return idna.encode(host, uts46=True, std3_rules=True).decode("ascii").lower()
    except (idna.IDNAError, UnicodeError) as exc:
        raise IdentityError("invalid URL host") from exc


def canonicalize_url(value: Any) -> Optional[str]:
    if not isinstance(value, str) or not value or len(value) > 2048:
        return None
    if any(char.isspace() or ord(char) < 32 or ord(char) == 127 for char in value):
        return None
    if any(value[index] == "%" and (index + 2 >= len(value) or value[index + 1] not in _HEX or value[index + 2] not in _HEX) for index in range(len(value))):
        return None
    try:
        parsed = urlsplit(value)
        if parsed.scheme.lower() != "https" or not parsed.netloc or parsed.username is not None or parsed.password is not None:
            return None
        host = parsed.hostname
        if not host:
            return None
        port = parsed.port
    except (ValueError, UnicodeError):
        return None
    if port is not None and not 1 <= port <= 65535:
        return None
    try:
        ip = ipaddress.ip_address(host)
        host_text = f"[{ip.compressed}]" if ip.version == 6 else ip.compressed
    except ValueError:
        # Bracketed values which are not IPv6 are rejected by urlsplit above;
        # DNS labels are validated by the UTS-46 implementation when present.
        try:
            host_text = _idna_host(host)
        except IdentityError:
            return None
        if len(host_text) > 253 or any(not label or len(label) > 63 for label in host_text.split(".")):
            return None
        if any(label.startswith("-") or label.endswith("-") for label in host_text.split(".")):
            return None
        if re.fullmatch(r"\d+(?:\.\d+){3}", host):
            return None
    path = parsed.path or "/"
    if any(char not in _SAFE_PATH and not (char == "%") for char in path):
        return None
    path = re.sub(r"/{2,}", "/", path)
    path = re.sub(r"%([0-9a-fA-F]{2})", lambda match: "%" + match.group(1).upper(), path)
    if len(path) > 1:
        path = path.rstrip("/") or "/"
    port_text = "" if port in (None, 443) else f":{port}"
    return f"https://{host_text}{port_text}{path}"


def _raw_structured(raw: Mapping[str, Any]) -> tuple[tuple[str, Any], ...]:
    result: list[tuple[str, Any]] = []
    for key in sorted(_RAW_FIELDS):
        if key not in raw:
            continue
        values = normalize_list(raw[key])
        result.append((key, values))
    return tuple(result)


def normalize_candidate(candidate: Any) -> NormalizedCandidate:
    source_id = normalize_source_id(getattr(candidate, "source_id", None))
    source_url = canonicalize_url(getattr(candidate, "source_url", None))
    if source_url is None:
        raise CandidateRejected("invalid_source_url")
    source_posting_id = normalize_identifier(getattr(candidate, "source_posting_id", None))
    title = normalize_scalar(getattr(candidate, "title", None), allow_none=False) or ""
    company = normalize_scalar(getattr(candidate, "company", None), allow_none=False) or ""
    location = normalize_scalar(getattr(candidate, "location", None), allow_none=False) or ""
    deadline_raw = normalize_scalar(getattr(candidate, "deadline_raw", None))
    if not title or not company:
        raise CandidateRejected("invalid_candidate")
    raw = getattr(candidate, "raw_jd", None)
    if not isinstance(raw, Mapping):
        raise CandidateRejected("invalid_raw_structured")
    try:
        collected_at = getattr(candidate, "collected_at")
        if not isinstance(collected_at, datetime):
            raise TypeError
        structured = _raw_structured(raw)
    except (TypeError, IdentityError) as exc:
        raise CandidateRejected("invalid_candidate") from exc
    changed = 0
    emptied = 0
    for original_value, normalized_value in (
        (getattr(candidate, "source_id", None), source_id),
        (getattr(candidate, "source_url", None), source_url),
        (getattr(candidate, "source_posting_id", None), source_posting_id),
        (getattr(candidate, "title", None), title),
        (getattr(candidate, "company", None), company),
        (getattr(candidate, "location", None), location),
        (getattr(candidate, "deadline_raw", None), deadline_raw),
    ):
        if original_value != normalized_value:
            changed += 1
        if original_value not in (None, "") and normalized_value in (None, ""):
            emptied += 1
    for key, normalized_values in structured:
        original_value = raw[key]
        comparable = tuple(original_value) if isinstance(original_value, (list, tuple)) else (original_value,)
        if comparable != normalized_values:
            changed += 1
        if comparable and not normalized_values:
            emptied += 1
    result = CandidateV2(source_id, source_url, source_posting_id, title, company, location, deadline_raw, collected_at, structured)
    return NormalizedCandidate(result, NormalizationInfo(changed, emptied, ()))


def candidate_bytes(candidate: CandidateV2) -> bytes:
    return canonical_bytes(
        {
            "source_id": candidate.source_id,
            "source_url": candidate.source_url,
            "source_posting_id": candidate.source_posting_id,
            "title": candidate.title,
            "company": candidate.company,
            "location": candidate.location,
            "deadline_raw": candidate.deadline_raw,
            "collected_at": candidate.collected_at,
            "raw_structured": candidate.raw_structured,
        }
    )


def snapshot_bytes(snapshot: SnapshotV2) -> bytes:
    return canonical_bytes(
        {
            "source_id": snapshot.source_id,
            "canonical_url": snapshot.canonical_url,
            "source_posting_id": snapshot.source_posting_id,
            "title": snapshot.title,
            "company": snapshot.company,
            "location": snapshot.location,
            "deadline": snapshot.deadline,
            "deadline_uncertain": snapshot.deadline_uncertain,
            "required_qualifications": snapshot.required_qualifications,
            "preferred_qualifications": snapshot.preferred_qualifications,
            "responsibilities": snapshot.responsibilities,
            "company_info": snapshot.company_info,
            "experience_tags": snapshot.experience_tags,
            "manual_review_flags": snapshot.manual_review_flags,
            "detail_quality": snapshot.detail_quality,
        }
    )

def _jsonable(value: Any) -> Any:
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, Mapping):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if isinstance(value, (tuple, list)):
        return [_jsonable(item) for item in value]
    if hasattr(value, "value") and isinstance(value.value, str):
        return value.value
    return value


def canonical_bytes(value: Any) -> bytes:
    return json.dumps(_jsonable(value), ensure_ascii=False, separators=(",", ":"), sort_keys=False).encode("utf-8")


def identity_basis(value: CandidateV2 | SnapshotV2) -> Mapping[str, str]:
    if isinstance(value, CandidateV2):
        source_id, posting_id, url, title, company = value.source_id, value.source_posting_id, value.source_url, value.title, value.company
    else:
        source_id, posting_id, url, title, company = value.source_id, value.source_posting_id, value.canonical_url, value.title, value.company
    if posting_id:
        return {"kind": "source_posting_id", "source_id": source_id, "value": posting_id.casefold()}
    if url:
        return {"kind": "canonical_url", "source_id": source_id, "value": url}
    collapsed = "".join(" " if unicodedata.category(char)[0] in "PSC" else char for char in f"{title} {company}")
    collapsed = " ".join(collapsed.casefold().split()).strip()
    return {"kind": "title_company", "source_id": source_id, "value": collapsed}


def posting_key(value: CandidateV2 | SnapshotV2) -> str:
    return hashlib.sha256(b"posting-v3\0" + canonical_bytes(identity_basis(value))).hexdigest()[:32]


def recommendation_id(value: CandidateV2 | SnapshotV2, run_id: str) -> str:
    if not isinstance(run_id, str) or not run_id:
        raise IdentityError("run_id is required")
    return hashlib.sha256(b"recommendation-v3\0" + run_id.encode("utf-8") + b"\0" + canonical_bytes(identity_basis(value))).hexdigest()[:32]


def tie_breaker(candidate: CandidateV2, snapshot: SnapshotV2) -> tuple[Any, ...]:
    detail_rank = {"verified": 0, "manual_only": 1, "rejected": 2}.get(snapshot.detail_quality, 2)
    manual_rank = int(bool(snapshot.manual_review_flags))
    deadline_rank = int(snapshot.deadline is None or snapshot.deadline_uncertain)
    structured_count = sum(len(getattr(snapshot, field)) for field in ("required_qualifications", "preferred_qualifications", "responsibilities", "company_info", "experience_tags"))
    encoded = candidate_bytes(candidate)
    return (detail_rank, manual_rank, deadline_rank, -structured_count, hashlib.sha256(encoded).hexdigest(), encoded)


# Explicit aliases make the integration surface discoverable without keeping
# separate implementations.
canonical_url = canonicalize_url
normalize_url = canonicalize_url
posting_key_v3 = posting_key
recommendation_id_v3 = recommendation_id
candidate_identity_basis = identity_basis
candidate_tie_breaker = tie_breaker
normalize_candidate_v2 = normalize_candidate
normalize_ingress_candidate = normalize_candidate
canonicalize_source_id = normalize_source_id
canonicalize_identifier = normalize_identifier
posting_identity_basis = identity_basis
posting_key_for_snapshot = posting_key
recommendation_id_for_snapshot = recommendation_id
deterministic_candidate_tie_breaker = tie_breaker
dedupe_identity_basis = identity_basis
snapshot_canonical_bytes = snapshot_bytes
