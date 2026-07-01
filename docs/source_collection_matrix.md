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
| Jumpit | Core | `enabled` | `public_http` | [] | 공개 sitemap/detail page adapter가 no-human target 기준을 만족합니다. | `tests/test_dry_run.py::test_jumpit_adapter_collects_jobs_from_sitemap_and_react_data`, `tests/test_dry_run.py::test_jumpit_adapter_tolerates_spaced_next_payload_shape`; adapter `JumpitAdapter`; `/position/`, `/sitemap/` 공개 경로 검토 | 페이지 shape 변경 시 fixture/parser 갱신 |
| Rallit | Core | `enabled` | `public_http` | [] | public `/positions` 및 detail page adapter가 no-human target 기준을 만족합니다. | `tests/test_dry_run.py::test_rallit_adapter_collects_jobs_from_public_positions`, `tests/test_dry_run.py::test_rallit_adapter_strips_embedded_css_from_public_detail`, `tests/test_dry_run.py::test_rallit_adapter_tolerates_alternate_section_markers`; adapter `RallitAdapter`; public detail JD text 증거 | 페이지 shape 변경 시 fixture/parser 갱신 |
| JobKorea | Core | `enabled` | `public_http` | [] | public HTTP 목록 API HTML에서 상세 URL을 찾고 공개 상세 페이지의 섹션 또는 JSON-LD `JobPosting`을 추출합니다. | `tests/test_dry_run.py::test_jobkorea_adapter_discovers_ai_jobs_from_api_html`, `tests/test_dry_run.py::test_jobkorea_adapter_enriches_api_cards_with_public_detail_body`, `tests/test_dry_run.py::test_jobkorea_adapter_uses_json_ld_when_detail_sections_are_absent`, `tests/test_dry_run.py::test_jobkorea_json_ld_graph_detail_quality_sample`; adapter `JobKoreaAdapter` | 상세 페이지 shape 변경 시 detail-body/parser fixture를 갱신합니다. |
| Saramin | Core | `enabled` | `public_http` | [] | 공개 검색/목록에서 relay detail URL을 발견하고 detail iframe URL로 정규화해 JD 본문 섹션을 직접 가져옵니다. | `tests/test_dry_run.py::test_saramin_adapter_collects_public_detail_body_without_api`, `tests/test_dry_run.py::test_saramin_adapter_discovers_public_relay_detail_urls_from_listing`, `artifacts/ultragoal_g002/saramin_browser_automation_iframe_details.json`; adapter `SaraminAdapter` | 목록/detail shape 변경 시 fixture/parser 갱신 |
| Wanted | Core | `enabled` | `public_http` | [] | 공개 검색/목록에서 `/wd/` detail 링크를 발견하고 detail HTML을 public HTTP parser로 수집합니다. | `tests/test_dry_run.py::test_wanted_adapter_collects_public_detail_body_without_manual_payload`, `tests/test_dry_run.py::test_wanted_adapter_discovers_public_wd_urls_from_listing`, `artifacts/ultragoal_g003/wanted_browser_transcript.json`; adapter `WantedAdapter` | 검색/detail shape 변경 시 fixture/parser 갱신 |
| RocketPunch | Core | `enabled` | `browser_automation` | [] | 사용자 지시에 따른 `user_directed_ignore` override를 명시하고 no-human headless browser automation으로 jobs list DOM을 수집합니다. RocketPunch의 공고 URL은 별도 detail page가 아니라 `/en/jobs?selectedJobId=<id>`가 같은 list 화면의 detail pane을 여는 구조라, card href를 selectedJobId URL로 정규화하고 detail pane을 재로드해 JD 본문을 보강합니다. | `tests/test_dry_run.py::test_rocketpunch_browser_automation_parses_listing_cards_without_detail_links`, `tests/test_dry_run.py::test_rocketpunch_browser_automation_enriches_selected_job_detail`, `fixtures/chrome_captures/rocketpunch_selected_job_158927.html`; adapter `RocketPunchBrowserAutomationAdapter`; source notice acknowledgement + policy override | 카드/detail pane DOM shape 변경 시 parser fixture와 live 샘플을 갱신합니다. |
| LinkedIn | Excluded | `excluded` | `null` | [] | V1 target에서 제외합니다. 직접 scraping/API/partner payload는 target이 아닙니다. | `fixtures/chrome_captures/linkedin_detail.json`은 historical/manual fixture only; auth/session/privacy risk | V1 자동 수집 대상에 포함하지 않음 |
| Company careers | Expansion | `deferred` | `null` | [`public_http`] | 회사별 도메인 단위 public HTTP 검토가 필요합니다. | per-domain allowlist, robots/access review, fixture/parser test 미완료 | 회사별 source review와 fixture/test 추가 후 개별 enable |

## V1 Completion Rule

V1 완료는 pipeline이 데이터를 ingest할 수 있다는 뜻이 아닙니다. Completion은 source registry에서 `target_status=enabled`, `automation_level=no_human`, 유효한 `target_lane`, adapter/test/docs refs를 모두 통과해야 합니다.

현재 V1 target enabled source는 `jobkorea`, `saramin`, `wanted`, `jumpit`, `rallit`, `rocketpunch`입니다. `company_careers`는 후보/보류 상태이며, `linkedin`은 제외입니다.

`browser_automation` 후보는 보류 라벨이 아닙니다. `public_http`가 실패한 source는 세션/개인정보/anti-bot 리스크가 없는 no-human 브라우저 자동화가 안정적으로 성공하거나, 안전하게 불가능하다는 증거가 남을 때까지 실험 대상입니다.

## Privacy Boundary

수집 경로가 저장할 수 있는 필드는 채용 공고 필드로 제한합니다.

- source ID / source URL / source posting ID
- title, company, location, deadline 또는 posted date
- visible skills/tags
- ranking에 필요한 JD text 또는 visible snippet
- capture timestamp

쿠키, 세션 토큰, auth header, local storage, 비밀번호, private message, inbox, resume/profile private data, unrelated account content는 저장하면 안 됩니다.
