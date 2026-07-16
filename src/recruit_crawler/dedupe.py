from __future__ import annotations

import hashlib
import re
from typing import Iterable, List, Tuple

from .identity import posting_key, snapshot_bytes, tie_breaker
from .schemas import CandidateV2, JDSnapshot, SnapshotV2


def _normalize(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", value.lower()).strip()


def dedupe_snapshots(snapshots: Iterable[JDSnapshot]) -> Tuple[List[JDSnapshot], int]:
    seen_urls = set()
    seen_title_company = set()
    kept: List[JDSnapshot] = []
    removed = 0

    for snapshot in snapshots:
        url_key = snapshot.source_url.strip().lower()
        title_company_key = (_normalize(snapshot.title), _normalize(snapshot.company))
        if url_key in seen_urls or title_company_key in seen_title_company:
            removed += 1
            continue
        seen_urls.add(url_key)
        seen_title_company.add(title_company_key)
        kept.append(snapshot)

    return kept, removed
def dedupe_snapshots_v2(
    records: Iterable[tuple[CandidateV2, SnapshotV2] | SnapshotV2],
) -> Tuple[List[tuple[CandidateV2, SnapshotV2] | SnapshotV2], int]:
    """Keep one deterministic survivor for each posting-v3 identity."""
    grouped: dict[str, list[tuple[CandidateV2, SnapshotV2] | SnapshotV2]] = {}
    for record in records:
        snapshot = record[1] if isinstance(record, tuple) else record
        grouped.setdefault(posting_key(snapshot), []).append(record)

    kept: list[tuple[CandidateV2, SnapshotV2] | SnapshotV2] = []
    for records_for_key in grouped.values():
        if len(records_for_key) == 1:
            kept.append(records_for_key[0])
            continue

        def sort_key(record: tuple[CandidateV2, SnapshotV2] | SnapshotV2) -> tuple:
            if isinstance(record, tuple):
                return tie_breaker(record[0], record[1])
            # Snapshot-only callers have no CandidateV2 bytes; canonical public
            # fields remain a deterministic last-resort ordering.
            return (
                {"verified": 0, "manual_only": 1, "rejected": 2}.get(record.detail_quality, 2),
                int(bool(record.manual_review_flags)),
                int(record.deadline is None or record.deadline_uncertain),
                -sum(
                    len(getattr(record, field))
                    for field in (
                        "required_qualifications",
                        "preferred_qualifications",
                        "responsibilities",
                        "company_info",
                        "experience_tags",
                    )
                ),
                hashlib.sha256(snapshot_bytes(record)).hexdigest(),
                snapshot_bytes(record),
            )

        kept.append(min(records_for_key, key=sort_key))
    return kept, sum(len(items) - 1 for items in grouped.values() if len(items) > 1)


dedupe_snapshot_v2 = dedupe_snapshots_v2
dedupe_candidates_v2 = dedupe_snapshots_v2
dedupe_v2 = dedupe_snapshots_v2
