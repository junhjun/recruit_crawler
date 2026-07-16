from __future__ import annotations

import sys
import subprocess
import unittest
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from unittest.mock import call, patch

from recruit_crawler.schemas import (
    CandidateDetailIssueCodeV2,
    CandidateDetailIssueV2,
    PostingCandidate,
    SourceManifest,
)
from recruit_crawler.sources.http import SourceAccessError
from recruit_crawler.sources.platforms import RocketPunchBrowserAutomationAdapter
from recruit_crawler.sources.platform_rocketpunch import (
    BrowserChildLifecycleError,
    BrowserChildRegistryV1,
)

class _FakeBrowserProcess:
    def __init__(self, events=None, *, communicate_error=None, returncode=0):
        self.pid = 40123
        self.returncode = returncode
        self.stdout = ""
        self.stderr = ""
        self.events = events if events is not None else []
        self.communicate_error = communicate_error
        self.terminate_calls = 0
        self.wait_calls = 0
        self.kill_calls = 0

    def communicate(self, *, timeout):
        self.events.append(("communicate", timeout))
        if self.communicate_error is not None:
            raise self.communicate_error
        return "<html></html>", ""

    def terminate(self):
        self.terminate_calls += 1
        self.events.append("terminate")

    def wait(self, *, timeout):
        self.wait_calls += 1
        self.events.append(("wait", timeout))

    def kill(self):
        self.kill_calls += 1
        self.events.append("kill")


class RocketPunchAdapterTests(unittest.TestCase):

    def test_rocketpunch_browser_automation_parses_listing_cards_without_detail_links(self) -> None:
        listing_url = "https://www.rocketpunch.com/en/jobs"
        manifest = SourceManifest(
            source_id="rocketpunch",
            enabled=True,
            access_mode="browser_automation",
            auth_required=False,
            tos_review_status="unknown",
            domains=["www.rocketpunch.com"],
            rate_limit="browser automation with user-directed notice override",
            failure_mode="skip_source",
            allowed_persisted_fields=[],
            options={
                "search_urls": [listing_url],
                "candidate_include_keywords": ["python", "data", "backend", "fintech"],
                "policy_override_mode": "user_directed_ignore",
                "policy_override_reason": "User directed RocketPunch browser automation despite source notice.",
                "policy_override_acknowledges_source_notice": True,
                "max_pages": 10,
                "delay_seconds": 0,
                "fetch_detail_pages": False,
            },
        )
        dom_html = """
        <html><body>
          <div class="listing-card">
            <a href="/en/jobs/158927?list=true">detail</a>
            <span class="company-name">페이데이터</span>
            <h2 class="job-title">DataOps Engineer</h2>
            <p>Python SQL Airflow data pipeline operations</p>
            <span>Seoul</span>
          </div>
          <div class="listing-card">
            <span class="company-name">핀테크랩</span>
            <h2 class="job-title">Fintech Project Manager</h2>
            <p>Fintech AI backend platform coordination</p>
          </div>
          <div class="listing-card">
            <span class="company-name">디자인랩</span>
            <h2 class="job-title">Product Designer</h2>
            <p>Design system and marketing assets</p>
          </div>
        </body></html>
        """
        adapter = RocketPunchBrowserAutomationAdapter(manifest)

        with patch.object(adapter, "_dump_dom", return_value=dom_html):
            candidates = adapter.collect()

        self.assertEqual(len(candidates), 2)
        first = candidates[0]
        self.assertEqual(first.source_id, "rocketpunch")
        self.assertEqual(first.source_url, "https://www.rocketpunch.com/en/jobs/158927?list=true")
        self.assertEqual(first.source_posting_id, "158927")
        self.assertEqual(first.title, "DataOps Engineer")
        self.assertEqual(first.company, "페이데이터")
        self.assertIn("Python", first.raw_jd["required_qualifications"])
        self.assertIn("data pipeline", first.raw_jd["responsibilities"][0])
        self.assertIn("Seoul", first.location)

    def test_rocketpunch_browser_automation_enriches_direct_detail(self) -> None:
        listing_url = "https://www.rocketpunch.com/en/jobs"
        manifest = SourceManifest(
            source_id="rocketpunch",
            enabled=True,
            access_mode="browser_automation",
            auth_required=False,
            tos_review_status="unknown",
            domains=["www.rocketpunch.com"],
            rate_limit="browser automation with user-directed notice override",
            failure_mode="skip_source",
            allowed_persisted_fields=[],
            options={
                "search_urls": [listing_url],
                "candidate_include_keywords": ["python"],
                "policy_override_mode": "user_directed_ignore",
                "policy_override_reason": "User directed RocketPunch browser automation despite source notice.",
                "policy_override_acknowledges_source_notice": True,
                "delay_seconds": 0,
            },
        )
        listing_dom = """
        <div data-index="0">
          <a href="/en/jobs/158927?list=true">
            <p class="textStyle_Body.BodyS c_foregrounds.neutral.secondary">Playtica</p>
            <p class="textStyle_Body.BodyM_Bold c_foregrounds.neutral.primary">IT Specialist (BackEnd)</p>
            <p>Python, Java, Node.js</p>
          </a>
        </div>
        """
        detail_dom = (ROOT / "fixtures" / "chrome_captures" / "rocketpunch_selected_job_158927.html").read_text(
            encoding="utf-8"
        )
        adapter = RocketPunchBrowserAutomationAdapter(manifest)

        with patch.object(adapter, "_dump_dom", side_effect=[listing_dom, detail_dom]):
            candidates = adapter.collect()

        self.assertEqual(len(candidates), 1)
        candidate = candidates[0]
        self.assertEqual(candidate.source_url, "https://www.rocketpunch.com/en/jobs/158927?list=true")
        self.assertEqual(candidate.deadline_raw, "2026-07-31")
        self.assertIn("unmanned/self-service", candidate.raw_jd["responsibilities"][0])
        self.assertIn("Django", candidate.raw_jd["required_qualifications"][0])
        self.assertIn("payment systems", candidate.raw_jd["preferred_qualifications"][0])

    @staticmethod
    def _candidate(source_url: str, source_posting_id: str = "158927") -> PostingCandidate:
        return PostingCandidate(
            source_id="rocketpunch",
            source_url=source_url,
            source_posting_id=source_posting_id,
            title="IT Specialist (BackEnd)",
            company="Playtica",
            location="Seoul",
            deadline_raw=None,
            collected_at=datetime.now(timezone.utc),
            raw_jd={
                "required_qualifications": ["Python"],
                "preferred_qualifications": [],
                "responsibilities": ["Build backend services"],
                "company_info": ["Playtica"],
                "experience_tags": [],
            },
        )

    @staticmethod
    def _manifest(**options: object) -> SourceManifest:
        values = {
            "search_urls": [],
            "candidate_include_keywords": ["python"],
            "policy_override_mode": "user_directed_ignore",
            "policy_override_reason": "User directed RocketPunch browser automation despite source notice.",
            "policy_override_acknowledges_source_notice": True,
            "delay_seconds": 0,
        }
        values.update(options)
        return SourceManifest(
            source_id="rocketpunch",
            enabled=True,
            access_mode="browser_automation",
            auth_required=False,
            tos_review_status="unknown",
            domains=["www.rocketpunch.com"],
            rate_limit="browser automation with user-directed notice override",
            failure_mode="skip_source",
            allowed_persisted_fields=[],
            options=values,
        )

    def test_direct_detail_link_is_the_only_candidate_url_fetched(self) -> None:
        listing_url = "https://www.rocketpunch.com/en/jobs"
        listing_dom = """
        <div class="listing-card">
          <a href="/en/jobs/158927">
            <span class="company-name">Playtica</span>
            <h2 class="job-title">IT Specialist (BackEnd)</h2>
            <p>Python, Java, Node.js</p>
          </a>
        </div>
        """
        detail_dom = (ROOT / "fixtures" / "chrome_captures" / "rocketpunch_selected_job_158927.html").read_text(
            encoding="utf-8"
        )
        adapter = RocketPunchBrowserAutomationAdapter(
            self._manifest(search_urls=[listing_url], max_pages=10)
        )

        with patch.object(adapter, "_dump_dom", side_effect=[listing_dom, detail_dom]) as dump_dom:
            candidates = adapter.collect()

        self.assertEqual([candidate.source_url for candidate in candidates], ["https://www.rocketpunch.com/en/jobs/158927"])
        dump_dom.assert_has_calls(
            [call(listing_url), call("https://www.rocketpunch.com/en/jobs/158927")]
        )
        self.assertEqual(dump_dom.call_count, 2)
        self.assertEqual(adapter.issues, [])

    def test_invalid_listing_search_and_synthetic_urls_never_fetch_detail(self) -> None:
        candidates = [
            self._candidate("https://www.rocketpunch.com/en/jobs", "158927"),
            self._candidate("https://www.rocketpunch.com/en/jobs?selectedJobId=158927", "158927"),
            self._candidate("https://www.rocketpunch.com/en/jobs?query=python", "158927"),
            self._candidate("https://www.rocketpunch.com/en/jobs", "listing-1-it-specialist-backend"),
        ]
        adapter = RocketPunchBrowserAutomationAdapter(self._manifest())

        with patch.object(adapter, "_dump_dom") as dump_dom:
            enriched = adapter._enrich_detail_candidates(candidates)

        self.assertEqual(enriched, candidates)
        dump_dom.assert_not_called()
        self.assertEqual(
            [
                (issue.source_posting_id, issue.code)
                for issue in adapter.issues
            ],
            [
                ("158927", CandidateDetailIssueCodeV2.DETAIL_URL_INVALID),
                ("158927", CandidateDetailIssueCodeV2.DETAIL_URL_INVALID),
                ("158927", CandidateDetailIssueCodeV2.DETAIL_URL_INVALID),
                ("listing-1-it-specialist-backend", CandidateDetailIssueCodeV2.DETAIL_URL_INVALID),
            ],
        )
        self.assertTrue(all(isinstance(issue, CandidateDetailIssueV2) for issue in adapter.issues))
    def test_disabled_detail_fetch_marks_direct_candidates_unverified(self) -> None:
        direct = self._candidate("https://www.rocketpunch.com/en/jobs/158927")
        invalid = self._candidate("https://www.rocketpunch.com/en/jobs?selectedJobId=158927")
        adapter = RocketPunchBrowserAutomationAdapter(
            self._manifest(fetch_detail_pages=False)
        )

        with patch.object(adapter, "_dump_dom") as dump_dom:
            enriched = adapter._enrich_detail_candidates([direct, invalid])

        self.assertEqual(enriched, [direct, invalid])
        dump_dom.assert_not_called()
        self.assertEqual(
            [(issue.source_url, issue.code) for issue in adapter.issues],
            [
                (direct.source_url, CandidateDetailIssueCodeV2.DETAIL_UNVERIFIED),
                (invalid.source_url, CandidateDetailIssueCodeV2.DETAIL_URL_INVALID),
            ],
        )

    def test_detail_budget_counts_direct_attempts_not_listing_indexes(self) -> None:
        invalid = self._candidate("https://www.rocketpunch.com/en/jobs?selectedJobId=158927")
        first_direct = self._candidate("https://www.rocketpunch.com/en/jobs/158927")
        second_direct = self._candidate("https://www.rocketpunch.com/en/jobs/158928")
        detail_dom = (ROOT / "fixtures" / "chrome_captures" / "rocketpunch_selected_job_158927.html").read_text(
            encoding="utf-8"
        )
        adapter = RocketPunchBrowserAutomationAdapter(
            self._manifest(max_detail_pages=1)
        )

        with patch.object(adapter, "_dump_dom", return_value=detail_dom) as dump_dom:
            enriched = adapter._enrich_detail_candidates(
                [invalid, first_direct, second_direct]
            )

        dump_dom.assert_called_once_with(first_direct.source_url)
        self.assertEqual(enriched[0], invalid)
        self.assertEqual(enriched[1].deadline_raw, "2026-07-31")
        self.assertEqual(enriched[2], second_direct)
        self.assertEqual(
            [(issue.source_url, issue.code) for issue in adapter.issues],
            [
                (invalid.source_url, CandidateDetailIssueCodeV2.DETAIL_URL_INVALID),
                (second_direct.source_url, CandidateDetailIssueCodeV2.DETAIL_UNVERIFIED),
            ],
        )

    def test_detail_failures_use_closed_typed_codes(self) -> None:
        candidate = self._candidate("https://www.rocketpunch.com/en/jobs/158927")
        detail_dom = "<html><body><h1>Different posting</h1></body></html>"

        fetch_failed_adapter = RocketPunchBrowserAutomationAdapter(self._manifest())
        with patch.object(
            fetch_failed_adapter,
            "_dump_dom",
            side_effect=SourceAccessError("offline"),
        ):
            self.assertEqual(
                fetch_failed_adapter._enrich_detail_candidates([candidate]),
                [candidate],
            )
        self.assertEqual(
            fetch_failed_adapter.issues[0].code,
            CandidateDetailIssueCodeV2.DETAIL_FETCH_FAILED,
        )

        unverified_adapter = RocketPunchBrowserAutomationAdapter(self._manifest())
        with patch.object(unverified_adapter, "_dump_dom", return_value=detail_dom):
            self.assertEqual(
                unverified_adapter._enrich_detail_candidates([candidate]),
                [candidate],
            )
        self.assertEqual(
            unverified_adapter.issues[0].code,
            CandidateDetailIssueCodeV2.DETAIL_UNVERIFIED,
        )

    def test_mixed_candidates_isolated_when_one_detail_url_is_invalid(self) -> None:
        direct = self._candidate("https://www.rocketpunch.com/en/jobs/158927")
        invalid = self._candidate("https://www.rocketpunch.com/en/jobs?selectedJobId=158927")
        detail_dom = (ROOT / "fixtures" / "chrome_captures" / "rocketpunch_selected_job_158927.html").read_text(
            encoding="utf-8"
        )
        adapter = RocketPunchBrowserAutomationAdapter(self._manifest())

        with patch.object(adapter, "_dump_dom", return_value=detail_dom) as dump_dom:
            enriched = adapter._enrich_detail_candidates([direct, invalid])

        self.assertEqual(dump_dom.call_count, 1)
        dump_dom.assert_called_once_with(direct.source_url)
        self.assertEqual(enriched[0].deadline_raw, "2026-07-31")
        self.assertEqual(enriched[1], invalid)
        self.assertEqual(
            [(issue.source_url, issue.code) for issue in adapter.issues],
            [(invalid.source_url, CandidateDetailIssueCodeV2.DETAIL_URL_INVALID)],
        )

    def test_mixed_candidates_isolated_when_one_detail_fetch_fails(self) -> None:
        failed = self._candidate("https://www.rocketpunch.com/en/jobs/158927", "failed")
        valid = self._candidate("https://www.rocketpunch.com/en/jobs/158928", "valid")
        detail_dom = (ROOT / "fixtures" / "chrome_captures" / "rocketpunch_selected_job_158927.html").read_text(
            encoding="utf-8"
        )
        adapter = RocketPunchBrowserAutomationAdapter(self._manifest())

        with patch.object(
            adapter,
            "_dump_dom",
            side_effect=[SourceAccessError("private detail failure"), detail_dom],
        ) as dump_dom:
            enriched = adapter._enrich_detail_candidates([failed, valid])

        self.assertEqual(dump_dom.call_count, 2)
        self.assertEqual(enriched[0], failed)
        self.assertEqual(enriched[1].deadline_raw, "2026-07-31")
        self.assertEqual(
            [(issue.source_posting_id, issue.code) for issue in adapter.issues],
            [("failed", CandidateDetailIssueCodeV2.DETAIL_FETCH_FAILED)],
        )

    def test_browser_launch_inherits_worker_group_and_registers_before_navigation(self) -> None:
        adapter = RocketPunchBrowserAutomationAdapter(
            self._manifest(search_urls=["https://www.rocketpunch.com/en/jobs"])
        )
        events = []
        process = _FakeBrowserProcess(events)
        original_register = adapter._browser_registry.register

        def register(handle):
            events.append("register")
            original_register(handle)

        with patch.object(adapter, "_browser_binary", return_value="/tmp/chrome"), patch(
            "recruit_crawler.sources.platform_rocketpunch.subprocess.Popen",
            return_value=process,
        ) as popen, patch(
            "recruit_crawler.sources.platform_rocketpunch.os.getpgid",
            return_value=adapter._browser_registry.worker_pgid,
        ), patch.object(adapter._browser_registry, "register", side_effect=register):
            self.assertEqual(adapter._dump_dom("https://www.rocketpunch.com/en/jobs"), "<html></html>")

        self.assertEqual(events[0], "register")
        self.assertEqual(events[1][0], "communicate")
        kwargs = popen.call_args.kwargs
        self.assertNotIn("start_new_session", kwargs)
        self.assertNotIn("process_group", kwargs)
        self.assertNotIn("preexec_fn", kwargs)
        self.assertIs(kwargs["stdout"], subprocess.PIPE)
        self.assertIs(kwargs["stderr"], subprocess.PIPE)

    def test_browser_registration_rejects_mismatched_process_group_and_reaps(self) -> None:
        registry = BrowserChildRegistryV1(worker_pgid=700)
        process = _FakeBrowserProcess()
        with patch(
            "recruit_crawler.sources.platform_rocketpunch.os.getpgid",
            return_value=701,
        ):
            with self.assertRaises(BrowserChildLifecycleError):
                registry.register(process)

        self.assertEqual(process.terminate_calls, 1)
        self.assertEqual(process.wait_calls, 1)
        self.assertEqual(registry._handles, {})

    def test_browser_execution_failure_is_reaped_and_not_reported_as_success(self) -> None:
        process = _FakeBrowserProcess(
            communicate_error=subprocess.TimeoutExpired(["chrome"], 1)
        )
        adapter = RocketPunchBrowserAutomationAdapter(
            self._manifest(search_urls=["https://www.rocketpunch.com/en/jobs"])
        )
        with patch.object(adapter, "_browser_binary", return_value="/tmp/chrome"), patch(
            "recruit_crawler.sources.platform_rocketpunch.subprocess.Popen",
            return_value=process,
        ), patch(
            "recruit_crawler.sources.platform_rocketpunch.os.getpgid",
            return_value=adapter._browser_registry.worker_pgid,
        ):
            with self.assertRaises(BrowserChildLifecycleError):
                adapter._dump_dom("https://www.rocketpunch.com/en/jobs")

        self.assertEqual(process.terminate_calls, 1)
        self.assertEqual(process.wait_calls, 1)
if __name__ == "__main__":
    unittest.main()
