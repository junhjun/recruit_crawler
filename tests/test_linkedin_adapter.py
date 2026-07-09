from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

import json
import tempfile

from recruit_crawler.schemas import SourceManifest
from recruit_crawler.sources.http import PublicJobsHttpAdapter, SourceAccessError
from recruit_crawler.sources.platforms import LinkedInAdapter


class LinkedInAdapterTests(unittest.TestCase):
    def test_linkedin_adapter_requires_explicit_approved_access(self) -> None:
        manifest = SourceManifest(
            source_id="linkedin",
            enabled=True,
            access_mode="api",
            auth_required=True,
            tos_review_status="pass",
            domains=["www.linkedin.com"],
            rate_limit="approved partner/API access only",
            failure_mode="skip_source",
            allowed_persisted_fields=[],
            options={"approved_partner_access": False},
        )
        adapter = PublicJobsHttpAdapter(manifest)

        with self.assertRaises(SourceAccessError):
            adapter.collect()

    def test_linkedin_adapter_collects_approved_partner_payload_path(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            payload_path = Path(tmp) / "linkedin_jobs.json"
            payload_path.write_text(
                json.dumps(
                    {
                        "jobs": [
                            {
                                "job_id": "li-1",
                                "job_url": "https://www.linkedin.com/jobs/view/1",
                                "job_title": "LinkedIn Data Engineer",
                                "company_name": "LinkedIn Partner Co",
                                "location": "Remote",
                                "deadline": "2026-08-15",
                                "skills": ["Python", "SQL", "data pipeline"],
                                "experience": "경력무관",
                            }
                        ]
                    }
                ),
                encoding="utf-8",
            )
            manifest = SourceManifest(
                source_id="linkedin",
                enabled=True,
                access_mode="api",
                auth_required=True,
                tos_review_status="pass",
                domains=["www.linkedin.com"],
                rate_limit="approved partner/API access only",
                failure_mode="skip_source",
                allowed_persisted_fields=[],
                options={
                    "approved_partner_access": True,
                    "approved_authenticated_flow": True,
                    "partner_payload_path": str(payload_path),
                    "candidate_include_keywords": ["python"],
                    "delay_seconds": 0,
                },
            )
            candidates = LinkedInAdapter(manifest).collect()

        self.assertEqual(len(candidates), 1)
        self.assertEqual(candidates[0].source_id, "linkedin")
        self.assertEqual(candidates[0].title, "LinkedIn Data Engineer")
        self.assertIn("data pipeline", candidates[0].raw_jd["required_qualifications"])

    def test_linkedin_partner_payload_requires_approved_access(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            payload_path = Path(tmp) / "linkedin_jobs.json"
            payload_path.write_text(
                json.dumps(
                    [
                        {
                            "job_url": "https://www.linkedin.com/jobs/view/1",
                            "job_title": "LinkedIn Data Engineer",
                            "company_name": "LinkedIn Partner Co",
                            "skills": ["Python"],
                        }
                    ]
                ),
                encoding="utf-8",
            )
            manifest = SourceManifest(
                source_id="linkedin",
                enabled=True,
                access_mode="api",
                auth_required=True,
                tos_review_status="pass",
                domains=["www.linkedin.com"],
                rate_limit="approved partner/API access only",
                failure_mode="skip_source",
                allowed_persisted_fields=[],
                options={
                    "approved_partner_access": False,
                    "approved_authenticated_flow": False,
                    "partner_payload_path": str(payload_path),
                },
            )

            with self.assertRaises(SourceAccessError):
                LinkedInAdapter(manifest).collect()


if __name__ == "__main__":
    unittest.main()
