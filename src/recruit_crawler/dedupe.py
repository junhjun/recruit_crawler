from __future__ import annotations

import re
from typing import Iterable, List, Tuple

from .schemas import JDSnapshot


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
