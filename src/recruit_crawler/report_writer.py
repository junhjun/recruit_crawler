from __future__ import annotations

from datetime import date
from pathlib import Path


def write_report(output_dir: Path, run_date: date, content: str, *, report_slug: str) -> Path:
    output_dir.mkdir(parents=True, exist_ok=True)
    path = output_dir / f"{report_slug}-{run_date.isoformat()}.md"
    path.write_text(content, encoding="utf-8")
    return path
