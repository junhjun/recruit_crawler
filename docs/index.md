# Recruit Crawler 문서 인덱스

상태일: 2026-07-10

이 디렉터리는 사람이 읽는 운영/설계 문서를 보관합니다. 현재 기능 구현 현황의 단일 원장은 `status.md`이며, source 기계 판정 기준은 항상 `config/live_sources.sample.json`과 `source-status --json` 출력입니다.

## 먼저 읽을 문서

1. `status.md`
   - 현재 제품/기능/source/gap 상태판. `docs/status/features.json`과 source registry에서 `status-report`로 생성합니다.
2. `../AGENTS.md`
   - 에이전트용 핵심 하네스: 세션 부트스트랩, 구조 탐색, 파일 배치, GJC/TODO 갱신, 브라우저 검증 규칙.
3. `../TODO.md`
   - 앞으로 해야 할 작업만 담는 간결한 backlog.
4. `decisions.md`
   - 오래 유지할 제품/운영 결정만 짧게 기록합니다.
5. `source_collection_matrix.md`
   - 플랫폼별 `target_status`, `target_lane`, candidate lane, 증거/차단 사유 요약.
6. `source_search_logic.md`
   - 사이트별 검색 URL, discovery 방식, parser marker, fallback parser 상세.
7. `source_access_reviews.md`
   - source별 access/privacy/ToS 판단과 target enable/block 근거.
8. `model_context_extraction_report.md`
   - Codex thread 기반 structured context extraction의 현재 경계, privacy guard, persistent cache와 app-host integration 후속 과제.

## 보조 문서

- `chrome_extension_capture.md`
  - Chrome extension capture/import 경로. 현재 target enable 근거는 아니며, historical/manual import 및 회귀 검증용입니다.
- `source_access_review_template.md`
  - 신규 source를 검토할 때 복사해서 쓰는 템플릿.
- `source_reviews/jobkorea.md`
  - JobKorea 초기 source review 기록. 최신 기계 기준은 `config/live_sources.sample.json`과 `source_access_reviews.md`를 우선합니다.
- 완료 로그와 세션 요약은 별도 archive로 남기지 않습니다. 현재 상태는 `status.md`, 앞으로 할 일은 `../TODO.md`, 오래 유지할 결정은 `decisions.md`에만 둡니다.

## 현재 V1 target 상태

- Enabled `public_http`: JobKorea, Saramin, Wanted, Jumpit, Rallit
- Enabled `browser_automation`: RocketPunch
- Excluded/non-target: LinkedIn, Company careers

## 결과물 확인

생성 리포트는 Git ignore 대상인 `reports/` 아래에 생성됩니다.

```sh
PYTHONPATH=src python3 -m recruit_crawler.cli live-run \
  --config config/live_sources.sample.json \
  --context-doc path/to/resume.md \
  --context-doc path/to/portfolio.md \
  --quality-gate-output artifacts/live_quality_gate.json \
  --run-date 2026-07-01 \
  --print-report
```

`config.profile`은 fixture/default fallback입니다. 개인화된 실행은 `.txt`, `.md`, `.pdf`, `.docx` context 문서를 `--context-doc`으로 명시합니다. 이 옵션은 여러 번 반복할 수 있으며 resume, portfolio, 선호조건 메모를 합쳐 UserContext를 보강합니다.

`dry-run`은 interactive terminal 사용을 위해 누락 context를 보충 질문할 수 있습니다. `live-run`은 기본적으로 non-interactive이며 `--interview-missing-context`를 명시했을 때만 질문합니다. `scheduled-run`은 질문하지 않습니다.

Codex `예약됨` 대상 비대화형 실행은 `scheduled-run`을 사용합니다. 이 명령은 보충 interview를 실행하지 않고, 누락된 context를 quality gate의 `needs_context` 신호로 남깁니다.

```sh
PYTHONPATH=src python3 -m recruit_crawler.cli scheduled-run \
  --config config/live_sources.sample.json \
  --context-doc path/to/resume.md \
  --run-date 2026-07-01 \
  --output-dir reports/scheduled \
  --quality-gate-output artifacts/scheduled/latest_quality_gate.json
```

`needs_context`가 나오면 `context-doctor`로 부족한 필드만 질문받고 지속 사용 가능한 `personal_info/preferences.md`를 생성합니다.

```sh
PYTHONPATH=src python3 -m recruit_crawler.cli context-doctor \
  --config config/live_sources.sample.json \
  --context-doc path/to/resume.md \
  --output personal_info/preferences.md
```

현재 대표 리포트:

- `reports/recruiting-live-run-2026-07-01.md`
- `reports/recruiting-dry-run-2026-07-01.md`
