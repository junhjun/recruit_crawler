# Source Collection Matrix

상태일: 2026-07-01

이 문서는 source별 target workflow 상태를 사람이 읽는 한국어 기준으로 정리합니다. 기계 판정의 기준은 `config/live_sources.sample.json`과 `source-status --json`입니다.

사이트별 검색 URL, discovery 방식, parser marker는 `docs/source_search_logic.md`에 별도로 정리합니다.

## Target 용어

- `target_lane`: `public_http`, `browser_automation`, JSON `null`만 허용합니다.
- `target_status`: `enabled`, `blocked`, `deferred`, `excluded`만 허용합니다.
- `enabled`: 사람 개입 없는 target 수집이 검증되어 `live-run` 대상입니다.
- `deferred`: 후보이지만 public HTTP 또는 no-human browser automation 증거가 부족합니다.
- `blocked`: anti-bot/auth/session/privacy 등으로 안전한 target 경로가 아직 없습니다.
- `excluded`: V1 target에서 제외합니다.

API, partner payload, manual export, manual postings, user-operated Chrome extension/browser-use, OCR/수동 검토는 fallback 또는 과거 증거일 뿐 target completion을 만족하지 않습니다.

## Matrix

| Source | V1 role | target_status | target_lane | candidate_lanes | 현재 판단 | 증거/차단 사유 | 다음 작업 |
| --- | --- | --- | --- | --- | --- | --- | --- |
| Jumpit | Core | `enabled` | `public_http` | [] | 공개 sitemap/detail page adapter가 no-human target 기준을 만족합니다. | `tests/test_jumpit_adapter.py::test_jumpit_adapter_collects_jobs_from_sitemap_and_react_data`, `tests/test_jumpit_adapter.py::test_jumpit_adapter_tolerates_spaced_next_payload_shape`; adapter `JumpitAdapter`; `/position/`, `/sitemap/` 공개 경로 검토 | 페이지 shape 변경 시 fixture/parser 갱신 |
| Rallit | Core | `enabled` | `public_http` | [] | public `/positions` 및 detail page adapter가 no-human target 기준을 만족합니다. | `tests/test_rallit_adapter.py::test_rallit_adapter_collects_jobs_from_public_positions`, `tests/test_rallit_adapter.py::test_rallit_adapter_strips_embedded_css_from_public_detail`, `tests/test_rallit_adapter.py::test_rallit_adapter_tolerates_alternate_section_markers`; adapter `RallitAdapter`; public detail JD text 증거 | 페이지 shape 변경 시 fixture/parser 갱신 |
| JobKorea | Core | `enabled` | `public_http` | [] | public HTTP 목록 API HTML에서 상세 URL을 찾고 공개 상세 페이지의 섹션 또는 JSON-LD `JobPosting`을 추출합니다. | `tests/test_jobkorea_discovery.py::test_jobkorea_adapter_discovers_ai_jobs_from_api_html`, `tests/test_jobkorea_detail.py::test_jobkorea_adapter_enriches_api_cards_with_public_detail_body`, `tests/test_jobkorea_detail.py::test_jobkorea_adapter_uses_json_ld_when_detail_sections_are_absent`, `tests/test_jobkorea_detail.py::test_jobkorea_json_ld_graph_detail_quality_sample`; adapter `JobKoreaAdapter` | 상세 페이지 shape 변경 시 detail-body/parser fixture를 갱신합니다. |
| Saramin | Core | `enabled` | `public_http` | [] | 공개 검색/목록에서 anchor 또는 script payload의 relay detail URL을 발견하고 detail iframe URL로 정규화해 JD 본문 섹션을 직접 가져옵니다. | `tests/test_saramin_adapter.py::test_saramin_adapter_collects_public_detail_body_without_api`, `tests/test_saramin_adapter.py::test_saramin_adapter_discovers_public_relay_detail_urls_from_listing`, `tests/test_saramin_adapter.py::test_saramin_adapter_discovers_script_embedded_relay_ids_from_listing`, `artifacts/ultragoal_g002/saramin_browser_automation_iframe_details.json`; adapter `SaraminAdapter` | 목록/detail shape 변경 시 fixture/parser 갱신 |
| Wanted | Core | `enabled` | `public_http` | [] | 공개 검색/목록에서 anchor, Next data, 또는 공개 search position API의 `/wd/` detail 링크를 발견하고 detail HTML을 public HTTP parser로 수집합니다. | `tests/test_wanted_adapter.py::test_wanted_adapter_collects_public_detail_body_without_manual_payload`, `tests/test_wanted_adapter.py::test_wanted_adapter_discovers_public_wd_urls_from_listing`, `tests/test_wanted_discovery.py::test_wanted_adapter_discovers_next_data_wd_ids_from_listing`, `tests/test_wanted_discovery.py::test_wanted_adapter_falls_back_to_public_search_api_when_listing_is_skeleton`, `artifacts/ultragoal_g003/wanted_browser_transcript.json`; adapter `WantedAdapter` | 검색/detail shape 변경 시 fixture/parser 갱신 |
| RocketPunch | Core | `enabled` | `browser_automation` | [] | 사용자 지시에 따른 `user_directed_ignore` override를 명시하고 no-human headless browser automation으로 jobs list DOM을 수집합니다. 직접 `/en/jobs/<id>` href만 유지·fetch해 JD 본문을 보강합니다. 직접 href가 없을 때 합성하는 `selectedJobId` URL은 candidate 식별용이며 detail fetch 대상이 아니므로 listing candidate로 남깁니다. | `tests/test_rocketpunch_adapter.py::test_rocketpunch_browser_automation_parses_listing_cards_without_detail_links`, `tests/test_rocketpunch_adapter.py::test_rocketpunch_browser_automation_enriches_direct_detail`, `tests/test_rocketpunch_adapter.py::test_invalid_listing_search_and_synthetic_urls_never_fetch_detail`, `fixtures/chrome_captures/rocketpunch_selected_job_158927.html`; adapter `RocketPunchBrowserAutomationAdapter`; source notice acknowledgement + policy override | 카드/detail pane DOM shape 변경 시 parser fixture와 live 샘플을 갱신합니다. |
| NAVER Careers | Company careers parked | `excluded` | `null` | [] | Codex 예약됨 기반 개인 채용 리포트 서비스가 안정화될 때까지 LinkedIn과 같은 후순위/비대상 계열로 둡니다. | `docs/source_access_reviews.md#company-careers-v1-gate`, `config/live_sources.sample.json`; source-specific parsing is future expansion only | scheduled service 안정화 이후 별도 계획으로 재검토 |
| Kakao Careers | Company careers parked | `excluded` | `null` | [] | Codex 예약됨 기반 개인 채용 리포트 서비스가 안정화될 때까지 LinkedIn과 같은 후순위/비대상 계열로 둡니다. | `docs/source_access_reviews.md#company-careers-v1-gate`, `config/live_sources.sample.json`; source-specific SPA/API/detail parsing is future expansion only | scheduled service 안정화 이후 별도 계획으로 재검토 |
| LINE Careers | Company careers parked | `excluded` | `null` | [] | Company careers fallback도 near-term target completion에서 제외합니다. | `docs/source_access_reviews.md#company-careers-v1-gate` | scheduled service 안정화 이후 별도 계획으로 재검토 |
| Coupang Careers | Company careers parked | `excluded` | `null` | [] | Company careers fallback도 near-term target completion에서 제외합니다. | `docs/source_access_reviews.md#company-careers-v1-gate` | scheduled service 안정화 이후 별도 계획으로 재검토 |
| LinkedIn | Excluded | `excluded` | `null` | [] | V1 target에서 제외합니다. 직접 scraping/API/partner payload는 target이 아닙니다. | `fixtures/chrome_captures/linkedin_detail.json`은 historical/manual fixture only; auth/session/privacy risk | V1 자동 수집 대상에 포함하지 않음 |
| Company careers | Parked expansion | `excluded` | `null` | [] | Aggregate placeholder only. Reviewed concrete company-careers candidates are parked with LinkedIn-like non-target sources for the near-term roadmap. | per-domain rows above | Do not count this placeholder as an enabled source |

## V1 Completion Rule

V1 완료는 pipeline이 데이터를 ingest할 수 있다는 뜻이 아닙니다. Completion은 source registry에서 `target_status=enabled`, `automation_level=no_human`, 유효한 `target_lane`, adapter/test/docs refs를 모두 통과해야 합니다.

현재 V1 target enabled source는 `jobkorea`, `saramin`, `wanted`, `jumpit`, `rallit`, `rocketpunch`입니다. `linkedin`, `company_careers`, `naver_careers`, `kakao_careers`, `line_careers`, `coupang_careers`는 near-term target에서 제외입니다.

`live-run`은 source별 candidate count를 quality gate로 남겨야 합니다. `enabled` source가 0 candidates를 수집하면 gate status는 `fail`이며, 해당 source는 fixture/parser/live evidence를 보강하거나 `deferred`로 되돌려야 합니다.

`browser_automation` 후보는 보류 라벨이 아닙니다. `public_http`가 실패한 source는 세션/개인정보/anti-bot 리스크가 없는 no-human 브라우저 자동화가 안정적으로 성공하거나, 안전하게 불가능하다는 증거가 남을 때까지 실험 대상입니다.

## Privacy Boundary

수집 경로가 저장할 수 있는 필드는 채용 공고 필드로 제한합니다.

- source ID / source URL / source posting ID
- title, company, location, deadline 또는 posted date
- visible skills/tags
- ranking에 필요한 JD text 또는 visible snippet
- capture timestamp

쿠키, 세션 토큰, auth header, local storage, 비밀번호, private message, inbox, resume/profile private data, unrelated account content는 저장하면 안 됩니다.
