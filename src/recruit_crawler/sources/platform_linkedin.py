from __future__ import annotations

from dataclasses import replace
import csv
import json
import shutil
import subprocess
import os
import re
from datetime import datetime, timezone
from html import unescape
from pathlib import Path
from typing import Any, Dict, List, Optional
from urllib.parse import parse_qs, urljoin, urlparse


from ..schemas import PostingCandidate, SourceManifest
from .http import PublicJobsHttpAdapter, SourceAccessError, _contains_any, _date_prefix
from .platform_shared import (
    _candidate_from_manual_record,
    _filter_candidates,
    _has_manual_records,
    _manual_records,
    _merged_options,
    _records_from_file,
)


class LinkedInAdapter(PublicJobsHttpAdapter):
    def __init__(self, manifest: SourceManifest):
        super().__init__(
            _merged_options(
                manifest,
                {
                    "include_url_patterns": [r"/jobs/view/"],
                    "exclude_url_patterns": [r"/login", r"/signup"],
                    "max_pages": 20,
                    "delay_seconds": 1,
                    "require_robots": True,
                    "approved_partner_access": False,
                },
            )
        )

    def _validate_access(self) -> None:
        super()._validate_access()
        if not self.options.get("approved_partner_access"):
            raise SourceAccessError("LinkedIn requires approved partner/API access.")
        if not self.options.get("approved_authenticated_flow"):
            raise SourceAccessError("LinkedIn requires an approved authenticated/API flow.")

    def collect(self) -> List[PostingCandidate]:
        if _has_manual_records(self.options):
            return _filter_candidates(
                [_candidate_from_manual_record(self.manifest.source_id, record) for record in _manual_records(self.options)],
                self,
            )
        partner_payload = self.options.get("partner_payload_path") or self.options.get("api_response_path")
        if partner_payload:
            self._validate_access()
            records = _records_from_file(Path(str(partner_payload)))
            return _filter_candidates(
                [_candidate_from_manual_record(self.manifest.source_id, record) for record in records],
                self,
            )
        self._validate_access()
        raise SourceAccessError(
            "LinkedIn live API fetching is not implemented without a concrete approved partner API payload. "
            "Configure partner_payload_path/api_response_path or manual_export_path."
        )
