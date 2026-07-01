from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import List

from ..schemas import PostingCandidate


def load_fixture_postings(path: Path) -> List[PostingCandidate]:
    items = json.loads(path.read_text(encoding="utf-8"))
    collected_at = datetime.now(timezone.utc)
    return [
        PostingCandidate(
            source_id=str(item["source_id"]),
            source_url=str(item["source_url"]),
            source_posting_id=item.get("source_posting_id"),
            title=str(item["title"]),
            company=str(item["company"]),
            location=str(item.get("location", "")),
            deadline_raw=item.get("deadline"),
            collected_at=collected_at,
            raw_jd=dict(item.get("raw_jd", {})),
        )
        for item in items
    ]
