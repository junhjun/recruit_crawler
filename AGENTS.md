# AGENTS.md

## 탐색 기준

```text
.
├── src/recruit_crawler/   # CLI, pipeline, parser, registry, source adapters
├── tests/                 # unittest 검증
├── fixtures/              # parser/import 회귀 샘플
├── config/                # sample/live source 설정
├── docs/                  # 운영·source access 문서
│   └── archive/           # 완료 작업, 상태 스냅샷, 검증 이력
├── browser_extension/     # 수동 캡처/import 보조 도구
└── TODO.md                # 앞으로 해야 할 일만
```

- 새 파일은 위 구조 중 가장 가까운 기존 디렉토리에 둔다.
- 불필요한 새 최상위 디렉토리, 중복 문서, 임시 산출물은 만들지 않는다.
- `README.md`는 두지 않는다. 에이전트 하네스는 이 파일, 사람용 진행상황은 `TODO.md`에 둔다.

## GJC 작업 기록

- `TODO.md`는 앞으로 해야 할 일만 담는다. 완료 항목, 상태 스냅샷, 검증 이력은 `docs/archive/`로 옮긴다.
- GJC workflow나 큰 작업을 마치면 같은 턴에 `TODO.md`에서 완료 항목을 제거하고, 완료 요약을 `docs/archive/YYYY-MM-DD-<topic>.md`에 기록한다.
- `.gjc/`는 로컬 런타임 상태다. 완료 요약은 `.gjc/`에 묻어두지 말고 `docs/archive/`로 승격한다.

## GitHub / PR 규칙

- TBD

## 브라우저 검증 하네스

- 자동 검증은 기본 headless browser 또는 테스트 전용 임시 `user_data_dir`만 사용한다.
- 사용자의 실사용 Chrome 프로필은 사용자가 그 턴에서 명시 요청한 수동 검증에만 연결한다.
- 내가 연 브라우저/탭만 닫는다. 외부 Chrome 프로세스는 종료하거나 `kill`하지 않는다.
- Chrome/Chromium crash, `Target closed`, disconnect, protocol error, 예기치 않은 종료가 나오면 즉시 반복을 멈추고 실패 URL·동작·artifact를 기록한 뒤 원인을 분석한다.
- 브라우저 검증 완료 주장은 transcript artifact(open/navigate/evaluate와 관찰 결과)가 있을 때만 한다. 스크린샷이나 기억만으로 완료 처리하지 않는다.
- user-operated Chrome extension/browser-use/OCR/수동 검토는 fallback/manual evidence다. `enabled` target 근거는 `public_http` 또는 no-human `browser_automation` 증거만 인정한다.
