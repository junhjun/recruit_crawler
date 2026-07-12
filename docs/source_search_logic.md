# 사이트별 검색/수집 로직

상태일: 2026-07-01

이 문서는 `live-run`에서 source별로 어떤 공개 경로를 사용해 후보 URL을 찾고, 어떤 parser로 JD를 구조화하는지 설명합니다. 기계 판정 기준은 `config/live_sources.sample.json`과 `source-status --json`입니다.

## 공통 규칙

- 사람 개입 없는 `no_human` 경로만 target completion으로 인정합니다.
- `target_lane`은 `public_http`, `browser_automation`, `null`만 사용합니다.
- API/partner payload, manual export, user-operated Chrome extension/browser-use, OCR, 수동 검토 결과는 enabled target 근거가 아닙니다.
- Source notice/policy gate는 기본적으로 target enable 조건입니다. 단, 사용자가 명시적으로 정책 차단을 무시하라고 지시한 경우 harness는 `policy_override_mode: user_directed_ignore`를 가진 `browser_automation` target만 예외적으로 허용합니다. 이 override는 `policy_override_reason`과 `policy_override_acknowledges_source_notice: true`가 모두 있어야 하며, `public_http`나 API/manual/user-operated/OCR 경로에는 적용되지 않습니다.
- `PublicJobsHttpAdapter` 공통 동작:
  - `domains` allowlist 밖 URL은 거부합니다.
  - `require_robots=true`이면 robots.txt 허용 여부를 확인합니다.
  - `search_urls` 또는 source별 endpoint에서 anchor/detail URL을 발견합니다.
  - `include_url_patterns`와 `exclude_url_patterns`로 URL을 필터링합니다.
  - `link_include_keywords`가 있으면 링크 URL+텍스트에 키워드가 있는 링크만 남깁니다.
  - 최종 candidate는 `candidate_include_keywords` / `candidate_exclude_keywords`로 한 번 더 필터링합니다.

## JobKorea

- Adapter: `src/recruit_crawler/sources/platforms.py::JobKoreaAdapter`
- Config: `config/live_sources.sample.json`의 `source_id=jobkorea`
- Lane/status: `enabled / public_http`
- 검색/목록 경로:
  - POST `https://www.jobkorea.co.kr/recruit/ai-jobs/GetRecruitList`
  - params: `pageNo`, `pageSize`, `keyword`, `orderType`, `api_params`
  - JSON payload의 `html` fragment에서 `<li class="recruit-item">` block을 읽습니다.
  - `/Recruit/GI_Read/...` 상세 URL, 제목, 회사명, 마감일, 목록 태그를 추출합니다.
- 상세 parser:
  - 우선 공개 상세 HTML의 visible section marker를 사용합니다.
  - 책임: `이런 업무를 해요`, `주요업무`, `담당업무`
  - 자격: `이런 분들을 찾고 있어요`, `자격요건`, `지원자격`
  - 우대: `우대사항`, `이런 분이면 더 좋아요`
  - location은 `근무지 주소` 주변 텍스트에서 추출합니다.
  - 섹션 marker가 없는 Next.js 상세 shell은 `application/ld+json`의 schema.org `JobPosting`을 fallback으로 사용합니다.
  - JSON-LD는 단일 `JobPosting`과 `@graph` 내부 `JobPosting` 모두 품질 샘플 fixture로 검증합니다.
- 품질 규칙:
  - `require_detail_body=true`이면 visible section 또는 JSON-LD JobPosting description 중 하나가 있어야 candidate를 유지합니다.
  - detail fetch 실패는 source error로 기록하고 해당 candidate는 건너뜁니다.

## Saramin

- Adapter: `src/recruit_crawler/sources/platforms.py::SaraminAdapter`
- Lane/status: `enabled / public_http`
- 검색/상세 경로:
  - `search_urls`의 공개 검색/목록 HTML에서 `/zf_user/jobs/relay/view?...rec_idx=...` anchor 또는 script payload의 `rec_idx`를 찾습니다.
  - 발견된 relay view URL은 `/zf_user/jobs/relay/view-detail?rec_idx=...&rec_seq=0` 공개 detail iframe URL로 정규화합니다.
  - `search_urls`/`start_urls`가 없을 때만 직접 지정된 `detail_urls`를 fallback seed로 사용합니다.
  - 공식 API, 이미지 OCR, 수동 capture는 target 근거로 쓰지 않습니다.
- 상세 parser:
  - visible text에서 `주요업무`/`담당업무`, `자격요건`/`지원자격`, `우대사항`, `마감일 및 근무지` marker를 사용합니다.
  - title/company/location/deadline/skill terms를 공개 detail body에서 추출합니다.

## Wanted

- Adapter: `src/recruit_crawler/sources/platforms.py::WantedAdapter`
- Lane/status: `enabled / public_http`
- 검색/상세 경로:
  - `search_urls`의 공개 검색/목록 HTML에서 `/wd/...` detail anchor 또는 Next data의 position id를 찾습니다.
  - 검색 HTML이 skeleton만 반환하면 `api_url` 또는 기본 공개 `/api/chaos/search/v1/position` endpoint에서 position id를 찾습니다.
  - `search_urls`/`start_urls`가 없을 때만 직접 지정된 `detail_urls`를 fallback seed로 사용합니다.
  - no-human browser automation 검증 증거는 유지하지만 target parser는 auth/session/private profile 없이 public HTTP만 사용합니다.
- 상세 parser:
  - `주요업무`, `자격요건`, `우대사항`, `혜택 및 복지`, `기술 스택`, `근무지역` marker를 사용합니다.
  - title/company/location/skill terms를 공개 detail HTML에서 추출합니다.
- 경계:
  - auth/session/private profile 없이 공개 `/wd` URL만 사용합니다.

## Jumpit

- Adapter: `src/recruit_crawler/sources/platforms.py::JumpitAdapter`
- Lane/status: `enabled / public_http`
- 검색/상세 경로:
  - 공개 sitemap/position 경로에서 `/position/...` URL을 찾습니다.
  - 상세 페이지의 React/HTML payload에서 title/company/location/deadline/JD sections를 추출합니다.
- Parser:
  - 주요업무, 자격요건, 우대사항, 기술스택, 회사 정보, 경력 tag를 구조화합니다.
  - Next.js payload의 JSON spacing/escaping drift는 parser fixture로 검증합니다.

## Rallit

- Adapter: `src/recruit_crawler/sources/platforms.py::RallitAdapter`
- Lane/status: `enabled / public_http`
- 검색/상세 경로:
  - `https://www.rallit.com`에서 `/positions/<id>` anchor를 찾습니다.
  - `/apply`, `/auth`, `/my`, `/resume` 경로는 제외합니다.
- 상세 parser:
  - public detail HTML visible text에서 title/company/location/deadline을 추출합니다.
  - `주요업무`/`합류하면 하게 될 업무`, `자격요건`/`지원자격`, `우대사항`, `어떤 곳인가요?`, `경력` marker를 사용합니다.
  - embedded CSS/style text는 visible-text cleanup에서 제거하며 alternate section marker drift를 fixture로 검증합니다.
- 주의:
  - Rallit Next/Mantine 스타일 문자열이 본문에 섞일 수 있어 parser cleanup test가 있습니다.

## RocketPunch

- Adapter: `src/recruit_crawler/sources/platforms.py::RocketPunchBrowserAutomationAdapter`
- Lane/status: `enabled / browser_automation`
- 검색/목록 경로:
  - `https://www.rocketpunch.com/en/jobs`를 no-human headless Chrome/Chromium `--dump-dom` browser automation으로 로드합니다.
  - 이번 세션 정책에 따라 `policy_override_mode: user_directed_ignore`, `policy_override_reason`, `policy_override_acknowledges_source_notice: true`를 모두 둔 browser automation target만 허용합니다.
  - API/partner/manual/user-operated/OCR 경로는 여전히 target 근거가 아닙니다.
- Listing-card parser:
  - DOM의 `listing-card`, `job-card`, `job-item`, `company-list`, `data-index` block을 card 후보로 분리합니다.
  - card 내부 `job-title`/heading/`BodyM_Bold`에서 title을, `company-name`/company class/`BodyS secondary`에서 company를 추출합니다.
  - card href가 있으면 `/en/jobs?selectedJobId=<id>` URL로 정규화합니다. 실제 browser QA에서 이 URL은 같은 jobs list 화면이지만 우측/상세 pane에 선택된 JD 본문을 엽니다.
  - 선택 URL을 다시 no-human browser automation으로 로드해 `Responsibilities`, `Qualifications`, `Preferred Qualifications`, deadline을 detail pane에서 보강합니다.
  - href가 없으면 listing URL과 card 순번+company+title 기반 synthetic id를 fallback으로 사용합니다.
  - snippet, visible skill terms, location, experience tag를 구조화한 뒤 `candidate_include_keywords`/`candidate_exclude_keywords`로 필터링합니다.
- 주의:
  - source notice override는 RocketPunch browser automation target에만 적용됩니다. public HTTP, API/manual/user-operated/OCR에는 적용하지 않습니다.

## LinkedIn

- Adapter: `LinkedInAdapter`는 guardrail만 유지
- Lane/status: `excluded / null`
- 이유:
  - V1 target에서 제외합니다.
  - scraping/API/partner payload/auth/session flow는 target이 아닙니다.

## Company careers

- Adapter: no near-term target adapter
- Lane/status: `excluded / null`
- 판단:
  - Company careers 계열(`company_careers`, NAVER/Kakao/LINE/Coupang careers)은 Codex 예약됨 daily report service가 안정화될 때까지 LinkedIn과 같은 후순위/비대상 source로 둡니다.
  - 회사별 source-specific parser 확장은 scheduled runner, persistence, feedback, customization이 안정화된 뒤 별도 계획으로 재검토합니다.
  - manual/user-operated/OCR/API/partner path는 target enable 근거가 아닙니다.
