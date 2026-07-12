# Source Access Reviews

상태일: 2026-07-01. 이 문서는 crawler 운영 게이트 기록이며 법률 자문이 아닙니다. Source별 target status/lane의 기계 기준은 `config/live_sources.sample.json`과 `source-status --json`입니다.

공통 원칙:

- Target workflow는 사람 개입 없는 자동 수집만 허용합니다.
- API/partner payload/manual export/manual postings/user-operated Chrome extension/user-operated browser-use/OCR 수동 검토는 target enable 근거가 아닙니다.
- `blocked`, `deferred`, `excluded` source는 `target_lane: null`이어야 합니다.

## 회사 공식 채용 페이지

- Source ID: `company_careers`
- Domains: `config/live_sources.sample.json`의 회사별 allowlist
- Access mode: public page
- Auth required: no
- target_status: `excluded`
- target_lane: `null`
- candidate_lanes: []
- Rate limit: not scheduled
- Allowed persisted fields: none while excluded
- Failure mode: not part of target workflow

Decision: 회사별 careers 수집은 Codex 예약됨 기반 개인 채용 리포트 서비스가 안정화될 때까지 LinkedIn과 같은 후순위/비대상 계열로 둡니다.

## Company-careers V1 gate

Four distinct company-careers candidates were previously reviewed for an expansion gate. The roadmap pivot now excludes them from near-term target completion so implementation can focus on Codex 예약됨 daily execution, feedback, persistence, customization, and reusable distribution.

| Candidate | Source ID | Decision | Target lane | Evidence |
| --- | --- | --- | --- | --- |
| NAVER Careers | `naver_careers` | excluded | `null` | Public careers pages are reachable without login, but source-specific parsing is no longer near-term roadmap work. Revisit only after scheduled service stabilization. |
| Kakao Careers | `kakao_careers` | excluded | `null` | Public careers shell is reachable without login, but source-specific SPA/API/detail parsing is no longer near-term roadmap work. Revisit only after scheduled service stabilization. |
| LINE Careers | `line_careers` | excluded | `null` | Fallback candidate parked with other company-careers expansion sources. |
| Coupang Careers | `coupang_careers` | excluded | `null` | Fallback candidate parked with other company-careers expansion sources. |

Decision: keep all company-careers candidates excluded from the near-term target workflow. No manual/user-operated/OCR/API/partner path is used for target source count, and company-specific source expansion requires a separate future plan.
## 사람인

- Source ID: `saramin`
- Domains: `www.saramin.co.kr`
- Access mode: public page
- Auth required: no for public detail iframe pages; user session/auth flows are not target
- target_status: `enabled`
- target_lane: `public_http`
- candidate_lanes: []
- Rate limit: 1 request / second after review
- Allowed persisted fields: source URL, title, company, location, deadline, structured snapshot
- Failure mode: skip source

Decision: 공식 API는 target workflow에서 제외합니다. 이미지형 JD/OCR 및 user-operated Chrome capture도 target completion이 아닙니다. 공개 검색/목록에서 anchor 또는 script payload의 relay view URL을 찾고 공개 relay detail iframe URL로 정규화해 인증 없이 노출되는 JD 본문 섹션을 수집하며, `SaraminAdapter` public-page parser/discovery test와 no-human browser automation iframe evidence로 검증했습니다.

## 잡코리아

- Source ID: `jobkorea`
- Domains: `www.jobkorea.co.kr`
- Access mode: public page
- Auth required: no for public postings; personalized/session flows are not target
- target_status: `enabled`
- target_lane: `public_http`
- candidate_lanes: []
- Rate limit: 1 request / second after review
- Allowed persisted fields: source URL, title, company, location, deadline, structured snapshot
- Failure mode: skip source

Decision: public HTTP 목록 API HTML에서 상세 URL을 찾고, 공개 상세 페이지에서 JD 본문 섹션(`이런 업무를 해요`, `이런 분들을 찾고 있어요`, `우대사항`) 또는 schema.org JSON-LD `JobPosting`(단일 객체와 `@graph`)을 추출하는 parser test를 추가했습니다. Registry 기준상 `public_http` target으로 enable합니다.

## 점핏

- Source ID: `jumpit`
- Domains: `jumpit.saramin.co.kr`
- Access mode: public page
- Auth required: no for public postings
- target_status: `enabled`
- target_lane: `public_http`
- candidate_lanes: []
- Rate limit: 1 request / second after review
- Terms or robots review status: public position/sitemap paths checked on 2026-06-30
- Allowed persisted fields: source URL, title, company, location, deadline, structured snapshot
- Failure mode: skip source

Decision: live sample에서 enabled target입니다. Adapter는 public position sitemap에서 시작해 public posting page만 fetch하며 Next.js payload spacing/escaping drift fixture로 parser를 검증합니다.

## 원티드

- Source ID: `wanted`
- Domains: `www.wanted.co.kr`
- Access mode: public page
- Auth required: no for public `/wd/` detail pages; user session/auth flows are not target
- target_status: `enabled`
- target_lane: `public_http`
- candidate_lanes: []
- Rate limit: 1 request / second after review
- Terms or robots review status: public detail/search browser automation checked without login/session; manual/export/user capture excluded
- Allowed persisted fields: source URL, title, company, location, deadline, structured snapshot
- Failure mode: skip source

Decision: manual/export/user capture fallback은 target completion이 아닙니다. 공개 검색/목록에서 anchor, Next data, 또는 공개 search position API의 `/wd/` detail 링크를 찾고, 공개 detail HTML은 `WantedAdapter` public-page parser/discovery test로 JD 본문을 수집합니다. no-human browser automation 검증 증거는 유지하지만 target path는 auth/session/private profile 없이 public HTTP만 사용합니다.

## LinkedIn

- Source ID: `linkedin`
- Domains: `www.linkedin.com`
- Access mode: excluded
- Auth required: live site usage generally requires auth/session, therefore not target
- target_status: `excluded`
- target_lane: `null`
- candidate_lanes: []
- Rate limit: not applicable for V1 target
- Terms or robots review status: blocked without express permission
- Allowed persisted fields: none while excluded; historical/manual fixtures are non-target regression evidence only
- Failure mode: skip source

Decision: V1 target collection에서 제외합니다. 기존 fixture/import code는 historical/manual regression coverage입니다. Direct scraping, API, partner payload는 target이 아닙니다.

## 랠릿

- Source ID: `rallit`
- Domains: `www.rallit.com`
- Access mode: public page
- Auth required: no for public posting pages observed
- target_status: `enabled`
- target_lane: `public_http`
- candidate_lanes: []
- Rate limit: 1 request / second after adapter implementation
- Terms or robots review status: pass for public `/positions` review; auth/apply/my/resume-sensitive paths excluded
- Allowed persisted fields: source URL, title, company, location, deadline, structured snapshot
- Failure mode: skip source

Decision: live sample에서 enabled target입니다. Public `/positions`와 detail pages가 job card와 detailed JD text를 노출하며 fixture-backed parser assertion, embedded CSS cleanup assertion, alternate section marker drift assertion이 있습니다.

## 로켓펀치

- Source ID: `rocketpunch`
- Domains: `www.rocketpunch.com`
- Access mode: browser automation
- Auth required: no; target path must not require user session
- target_status: `enabled`
- target_lane: `browser_automation`
- candidate_lanes: []
- Rate limit: one no-human headless browser jobs page load plus selected detail pane loads per run
- Terms or robots review status: source notice remains acknowledged; this session explicitly enables only `policy_override_mode=user_directed_ignore` browser automation with `policy_override_reason` and `policy_override_acknowledges_source_notice: true`
- Allowed persisted fields: source URL, title, company, location, deadline, structured snapshot
- Failure mode: skip source

Decision: Manual/export/user-visible capture, API/partner, public HTTP, and OCR remain non-target. User-directed policy override enables only no-human browser automation for the jobs page. Browser QA confirmed RocketPunch links do not leave the list shell; clicking a card changes the URL to `/en/jobs?selectedJobId=<id>` and opens a detail pane containing Responsibilities/Qualifications/deadline. `RocketPunchBrowserAutomationAdapter` therefore normalizes card hrefs to selectedJobId URLs and reloads those pages to enrich listing-card candidates. If no card href is present, it falls back to listing URL plus a synthetic posting id.
