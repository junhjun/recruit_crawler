# Recruit Crawler Status

상태일: 2026-07-17

## 제품 한 줄 정의

Codex 예약됨에 등록하도록 설계된 local-first recruiting report service: collect reviewed no-human sources, score against user-owned personal_info/filter rules, persist feedback/history, and generate Korean recommendation reports.

## 기능 구현 현황

총 21개 기능 — deferred: 1, done: 16, excluded: 2, partial: 2

| 기능 | 상태 | 범주 | 사용자 가치 | 진입점 | 검증 |
| --- | --- | --- | --- | --- | --- |
| Fixture dry-run | `done` | pipeline | 네트워크 없이 샘플 채용공고 리포트를 생성한다. | `recruit-crawler dry-run` | `test_fixture_e2e_generates_report_without_expired_postings` |
| Live-run pipeline | `done` | pipeline | 검토된 실제 source adapter에서 후보를 수집하고 랭킹 리포트를 만든다. | `recruit-crawler live-run` | `test_live_run_records_source_level_candidate_metrics` |
| Source registry and status CLI | `done` | source | source별 enabled/deferred/excluded, lane, evidence, blocker를 기계적으로 확인한다. | `recruit-crawler source-status` | `test_source_registry_loads_expected_statuses_and_lanes`<br />`test_source_status_json_outputs_registry_without_network_or_adapter_construction` |
| Core platform source adapters | `done` | source | JobKorea, 사람인, Wanted, Jumpit, Rallit, RocketPunch에서 no-human target 후보를 제한 시간 안에 수집한다. | `recruit-crawler live-run` | `test_known_platforms_use_platform_specific_adapters`<br />`test_jobkorea_adapter_discovers_ai_jobs_from_api_html`<br />+10 |
| Company careers collection | `excluded` | source | 회사별 채용 페이지에서 공고를 수집한다. | 없음 | 없음 |
| LinkedIn automatic collection | `excluded` | source | LinkedIn 공고를 자동 수집한다. | 없음 | `test_linkedin_adapter_requires_explicit_approved_access` |
| JD parsing | `done` | scoring | 공고 후보를 title/company/location/deadline/requirements/responsibilities 등 구조화된 snapshot으로 변환한다. | `internal parse_candidates` | `test_unknown_deadline_is_uncertain_not_expired`<br />`test_each_selected_posting_has_actionable_report_fields` |
| Scoring and ranking | `done` | scoring | 사용자 context와 JD를 비교해 apply/hold/low_priority/exclude 추천을 산출한다. | `internal rank_snapshots` | `test_recommendation_buckets_include_apply_hold_and_low_priority`<br />`test_live_run_holds_one_year_over_profile_limit`<br />+1 |
| Korean Markdown report | `done` | reporting | 랭킹 결과를 한국어 Markdown 리포트로 저장하고 raw/private marker를 노출하지 않는다. | `--print-report`<br />`reports/*.md` | `test_report_surface_text_is_korean`<br />`test_report_excludes_raw_jd_and_private_profile_canaries` |
| Context document import | `done` | user_context | app host가 주입한 disposable Codex thread로 이력서/포트폴리오/선호조건을 구조화하고 실패 시 deterministic context로 안전하게 복구한다. | `--context-doc` | `test_plaintext_context_imports_user_context`<br />`test_dry_run_context_doc_cli_merges_multiple_personal_inputs`<br />+16 |
| Supplemental context interview | `done` | user_context | 명시적으로 요청한 terminal live-run이나 context-doctor에서 필수 필드 부족분만 CLI 질문으로 보강한다. | `live-run --interview-missing-context`<br />`recruit-crawler context-doctor` | `test_context_doc_cli_interviews_for_missing_context`<br />`test_live_run_interview_flag_fills_missing_context`<br />+1 |
| Context doctor preferences file | `done` | user_context | 역할·기술·근무지·경력·제외조건을 명시적으로 질문해 지속 사용 가능한 personal_info/preferences.md를 만든다. | `recruit-crawler context-doctor` | `test_context_doctor_writes_only_interview_preferences_and_preserves_korean_locations`<br />`test_scheduled_run_with_context_doctor_output_has_complete_context` |
| Privacy and persisted-field boundaries | `done` | privacy | private canary, raw JD marker, auth/session/private target 정보를 저장하거나 리포트하지 않도록 차단한다. | `config allowed_persisted_fields`<br />`browser-evidence`<br />`capture-import` | `test_private_canary_document_fails_closed`<br />`test_capture_import_rejects_sensitive_posting_fields`<br />+1 |
| Live-run quality gate | `done` | quality_gate | context 부족, enabled source zero-candidate, source 오류를 JSON gate로 실패/경고 표면화한다. | `live-run --quality-gate-output` | `test_live_run_missing_context_is_noninteractive_quality_failure`<br />`test_live_run_quality_gate_fails_enabled_source_with_zero_candidates`<br />+1 |
| Chrome extension capture/import fallback | `partial` | browser_capture | 수동 브라우저 캡처 JSON을 import해 리포트와 quality gate를 만들 수 있다. | `browser extension popup`<br />`recruit-crawler capture-import`<br />`recruit-crawler capture-quality-gate` | `test_capture_import_maps_mixed_sources_and_generates_report`<br />`test_capture_quality_gate_reports_privacy_and_import_categories` |
| Browser evidence transcript | `done` | quality_gate | 브라우저 DOM evidence를 허용 필드만 남긴 transcript로 생성해 source proof를 남긴다. | `recruit-crawler browser-evidence` | `test_browser_evidence_fixture_writes_allowed_fields_without_dom_leakage`<br />`test_browser_evidence_redacts_private_markers_case_insensitively` |
| Feedback learning loop | `done` | scoring | 사용자 피드백으로 추천 품질을 개선한다. | `recruit-crawler feedback-add`<br />`recruit-crawler feedback-export` | `test_feedback_add_records_event_for_persisted_recommendation`<br />`test_feedback_add_rejects_private_reason_canary`<br />+2 |
| Persistent DB/history | `partial` | reporting | 매일 실행 결과, 후보, 점수, 피드백, 품질 게이트 이력을 저장하고 재사용한다. | `recruit-crawler scheduled-run --db-path`<br />`recruit-crawler scheduled-history` | `test_scheduled_run_persists_history_without_duplicate_rows`<br />`test_feedback_add_records_event_for_persisted_recommendation`<br />+1 |
| Scheduler/recurring run | `done` | pipeline | Codex/cron 같은 외부 스케줄러에 나중에 연결할 수 있도록 검증된 비대화형 CLI/service contract와 deterministic artifacts를 제공한다. | `recruit-crawler scheduled-run` | `test_scheduled_run_writes_contract_quality_gate`<br />`test_scheduled_run_missing_context_is_noninteractive_quality_failure`<br />+5 |
| User customization contract | `done` | user_context | 사용자 소유 context 문서와 config 필터/가중치로 개인화된 스케줄 리포트를 만든다. | `config/sample_config.json`<br />`config/live_sources.sample.json`<br />`--context-doc`<br />`recruit-crawler scheduled-run` | `test_config_profile_creates_user_context_contract`<br />`test_explicit_deal_breaker_excludes_without_raw_leakage`<br />+5 |
| Codex scheduled onboarding and packaging | `deferred` | service_productization | 다른 사용자도 personal_info와 필터링 규칙을 넣어 scheduler-compatible CLI/service contract를 로컬에서 실행할 수 있다. | `README.md Codex 예약됨 onboarding`<br />`recruit-crawler scheduled-run`<br />`recruit-crawler feedback-add`<br />`recruit-crawler scheduled-history` | `test_scheduled_run_writes_contract_quality_gate`<br />`test_feedback_add_records_event_for_persisted_recommendation`<br />+1 |

## Source 상태

| Source | 상태 | Lane | Automation | Blocker / 다음 작업 |
| --- | --- | --- | --- | --- |
| Saramin | `enabled` | `public_http` | no_human | 목록/detail shape drift 시 fixture/parser test를 갱신합니다. |
| JobKorea | `enabled` | `public_http` | no_human | 상세 페이지 shape 변경 시 detail-body parser fixture를 갱신합니다. |
| Wanted | `enabled` | `public_http` | no_human | 검색/detail shape drift 시 fixture/parser test를 갱신합니다. |
| Jumpit | `enabled` | `public_http` | no_human | 페이지 shape 변경 시 fixture를 갱신합니다. |
| Rallit | `enabled` | `public_http` | no_human | 페이지 shape 변경 시 fixture를 갱신합니다. |
| RocketPunch | `enabled` | `browser_automation` | no_human | detail anchor가 계속 노출되지 않으면 listing URL + synthetic posting id 방식의 카드 parser 품질을 샘플링합니다. |
| LinkedIn | `excluded` | `null` | human | automatic collection prohibited; auth/session/privacy risk |
| Company careers | `excluded` | `null` | excluded | near-term product roadmap excludes company-careers expansion; source-specific parsers require separate future review |
| NAVER Careers | `excluded` | `null` | excluded | near-term product roadmap excludes company-careers expansion; source-specific parsers require separate future review |
| Kakao Careers | `excluded` | `null` | excluded | near-term product roadmap excludes company-careers expansion; source-specific parsers require separate future review |
| LINE Careers | `excluded` | `null` | excluded | near-term product roadmap excludes company-careers expansion; source-specific parsers require separate future review |
| Coupang Careers | `excluded` | `null` | excluded | near-term product roadmap excludes company-careers expansion; source-specific parsers require separate future review |

## 부족한 것

| 기능 | 상태 | 영향 / 차단 사유 | 다음 작업 |
| --- | --- | --- | --- |
| Company careers collection | `excluded` | Near-term service goal prioritizes Codex scheduled usage over company-careers expansion; Company careers require separate source-specific future review | Park with LinkedIn-like non-target sources; revisit only after scheduled service, persistence, feedback, and customization are stable. |
| LinkedIn automatic collection | `excluded` | V1 target에서 제외; auth/session/privacy risk; direct scraping/API/partner payload excluded | Do not include in V1 automatic collection. |
| Chrome extension capture/import fallback | `partial` | manual/user-operated capture is fallback evidence, not target completion evidence | Keep as historical/manual fallback and regression fixture path; do not count as target enablement. |
| Persistent DB/history | `partial` | History query surface is minimal and does not yet expose recommendation-change analysis | Expand history queries after customization and packaging stabilize. |
| Codex scheduled onboarding and packaging | `deferred` | Web UI/service mode is outside the current scheduled-run CLI product scope; Source-health maintenance, persistence/history, feedback ingestion, and customization gates must stabilize first | Defer web UI/service mode until the scheduled runner, persistence, feedback ingestion, customization, and source health gates are stable. |

## 다음 작업

### Status ledger 기준

- **Company careers collection**: Park with LinkedIn-like non-target sources; revisit only after scheduled service, persistence, feedback, and customization are stable.
- **LinkedIn automatic collection**: Do not include in V1 automatic collection.
- **Chrome extension capture/import fallback**: Keep as historical/manual fallback and regression fixture path; do not count as target enablement.
- **Persistent DB/history**: Expand history queries after customization and packaging stabilize.
- **Codex scheduled onboarding and packaging**: Defer web UI/service mode until the scheduled runner, persistence, feedback ingestion, customization, and source health gates are stable.

### TODO.md 기준

- JobKorea JSON-LD fallback으로 수집한 공고의 상세 JD 품질을 계속 샘플링
- Rallit/Jumpit/RocketPunch 상세 parser shape 변경 감지용 fixture를 주기적으로 갱신
- Evaluate a Jumpit public-listing fallback only after persistent sitemap unavailability and a separate public endpoint, robots/TOS, parser, fixture, and live-evidence review
- Source registry/docs/config/test refs 일치 여부를 release 전 체크리스트로 유지
- Feed stored feedback into deterministic relevance evaluation without live external LLM defaults
- Add regression cases for false negatives, false positives, and user deal-breaker drift
- Expand scheduled history queries for source health, recommendation changes, personal_info coverage, and filter-rule effects
- Define Codex thread timeout/retry and privacy-safe host logging policy without raw prompt or response text
- Define retention and cleanup policy for the structured model-context cache
- Add source-health maintenance command that samples enabled source parsers and flags zero-candidate or shape-drift failures
- Define artifact retention and cleanup policy for reports, evidence transcripts, DB rows, and feedback history
- Add privacy-first onboarding docs for personal_info, local storage, allowed persisted fields, and excluded/non-target sources
- Defer web UI/service mode until scheduled runner, persistence, feedback ingestion, customization, and source health gates are stable

## 운영 규칙

- 이 문서는 `docs/status/features.json`과 source registry에서 생성되는 현재 상태판입니다.
- 기능 추가/삭제/상태 변경 시 `features.json`을 먼저 갱신한 뒤 `status-report`로 이 파일을 재생성합니다.
- `TODO.md`는 앞으로 할 일만 담고, 중요한 제품 결정은 `docs/decisions.md`에 짧게 기록합니다.
