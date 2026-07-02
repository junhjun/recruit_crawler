# Recruit Crawler 문서 인덱스

상태일: 2026-07-01

이 디렉터리는 사람이 읽는 운영/설계 문서를 보관합니다. 기계 판정 기준은 항상 `config/live_sources.sample.json`과 `source-status --json` 출력입니다.

## 먼저 읽을 문서

1. `../AGENTS.md`
   - 에이전트용 핵심 하네스: 구조 탐색, 파일 배치, GJC/TODO 갱신, 브라우저 검증 규칙.
2. `../TODO.md`
   - 앞으로 해야 할 작업만 담는 간결한 backlog.
3. `archive/`
   - 완료된 작업, 상태 스냅샷, 검증 이력 보관.
4. `source_collection_matrix.md`
   - 플랫폼별 `target_status`, `target_lane`, candidate lane, 증거/차단 사유 요약.
5. `source_search_logic.md`
   - 사이트별 검색 URL, discovery 방식, parser marker, fallback parser 상세.
6. `source_access_reviews.md`
   - source별 access/privacy/ToS 판단과 target enable/block 근거.

## 보조 문서

- `chrome_extension_capture.md`
  - Chrome extension capture/import 경로. 현재 target enable 근거는 아니며, historical/manual import 및 회귀 검증용입니다.
- `source_access_review_template.md`
  - 신규 source를 검토할 때 복사해서 쓰는 템플릿.
- `source_reviews/jobkorea.md`
  - JobKorea 초기 source review 기록. 최신 기계 기준은 `config/live_sources.sample.json`과 `source_access_reviews.md`를 우선합니다.

## 현재 V1 target 상태

- Enabled `public_http`: JobKorea, Saramin, Wanted, Jumpit, Rallit
- Enabled `browser_automation`: RocketPunch
- Excluded: LinkedIn
- Deferred: Company careers

## 결과물 확인

생성 리포트는 Git ignore 대상인 `reports/` 아래에 생성됩니다.

```sh
PYTHONPATH=src python3 -m recruit_crawler.cli live-run \
  --config config/live_sources.sample.json \
  --context-doc path/to/resume.md \
  --quality-gate-output artifacts/live_quality_gate.json \
  --run-date 2026-07-01 \
  --print-report
```

`config.profile`은 fixture/default fallback입니다. 개인화된 실행은 `.txt`, `.md`, `.pdf`, `.docx` context 문서를 `--context-doc`으로 명시합니다.

현재 대표 리포트:

- `reports/recruiting-live-run-2026-07-01.md`
- `reports/recruiting-dry-run-2026-07-01.md`
