# AGENTS.md

## 탐색 기준

```text
.
├── README.md              # public project overview
├── AGENTS.md              # agent harness
├── TODO.md                # 앞으로 해야 할 일만
├── docs/status.md          # 현재 기능 구현 현황의 단일 상태판
├── src/recruit_crawler/   # CLI, pipeline, parser, registry, source adapters
├── tests/                 # unittest 검증
├── fixtures/              # parser/import 회귀 샘플
├── config/                # sample/live source 설정
├── docs/                  # 운영·source access 문서
│   └── status/            # 상태판 생성용 feature ledger
├── docs/decisions.md       # 오래 유지할 제품/운영 결정만 짧게 기록
└── browser_extension/     # 수동 캡처/import 보조 도구
```

- 새 파일은 위 구조 중 가장 가까운 기존 디렉토리에 둔다.
- 불필요한 새 최상위 디렉토리, 중복 문서, 임시 산출물은 만들지 않는다.
- `README.md`는 허황되지 않은 영어 project overview만 둔다. 에이전트 하네스는 `AGENTS.md`, 사람용 backlog는 `TODO.md`에 둔다.
- `docs/status.md`는 현재 기능 구현 현황의 단일 상태판이다. 직접 장문 편집하지 말고 `docs/status/features.json`을 갱신한 뒤 `status-report`로 재생성한다.
- 진행상황 파악의 SSOT는 역할별로 나뉜다: 기능 상태는 `docs/status/features.json`, 사람이 볼 상태판은 생성물 `docs/status.md`, 미래 backlog는 `TODO.md`, 오래 유지할 결정은 `docs/decisions.md`, 런타임 세션 상태는 `.gjc/_session-*/`다.


## 세션 부트스트랩

새 GJC 세션이나 "진행상황 파악" 요청은 토큰 절약을 위해 먼저 짧은 하네스만 실행한다.

1. `PYTHONPATH=src python3 -m recruit_crawler.cli status-report --brief` — tracking ok/stale, 현재 기능 카운트, 열린 TODO 수, non-done 기능, 다음 TODO 일부
2. `tracking: stale`이거나 brief만으로 판단이 안 될 때만 `docs/status.md`를 읽는다.
3. 실제 backlog 문구나 우선순위가 필요할 때만 `TODO.md`를 읽는다.
4. 중요한 제품/운영 결정의 이유가 필요할 때만 `docs/decisions.md`를 읽는다.
5. `.gjc/_session-*/`는 workflow 복구가 필요할 때만 읽고, 일반 진행상황 파악에는 쓰지 않는다.

`status-report --brief`는 최종 판단이 아니라 토큰 절약용 1차 라우터다. `recommended_next`가 policy/no-op 성격이면 바로 구현하지 말고 다음 actionable TODO나 사용자의 현재 요청을 우선한다.

## Live 실행 하네스

`live-run`, `scheduled-run`처럼 실제 외부 채용 source나 browser automation을 쓰는 시연·검증 요청은 sandbox 안에서 먼저 실행하지 않는다. Codex 기본 sandbox는 외부 DNS/network가 막혀 있으므로, live source 수집을 확인해야 하는 명령은 처음부터 `sandbox_permissions="require_escalated"`로 실행해 network/browser access 승인을 요청한다.

- 적용 대상: `recruit_crawler.cli live-run`, `recruit_crawler.cli scheduled-run`, live source를 여는 browser automation 검증
- 비적용 대상: `status-report --brief`, `status-report --check`, fixture 기반 `dry-run`, unittest, `source-status`의 registry-only 확인
- live 시연 산출물은 사용자가 명시하지 않으면 `/tmp/recruit-crawler-*` 아래에 쓴다. repo 안의 `reports/`, `artifacts/`, `personal_info/recruit.sqlite3`는 사용자가 요청한 경우에만 갱신한다.
- sandbox에서 같은 live 명령을 먼저 실행해 DNS 실패를 만든 뒤 재실행하지 않는다. sandbox 실패가 필요한 검증은 network preflight/fail-fast 테스트를 명시적으로 요청받았을 때만 한다.

## 변경 완료 게이트

기능 추가, 삭제, 상태 변경이 있으면 다음 순서로 닫는다.

1. `docs/status/features.json` 갱신
2. `PYTHONPATH=src python3 -m recruit_crawler.cli status-report`로 `docs/status.md` 재생성
3. 완료 항목은 `TODO.md`에서 제거한다
4. 오래 유지할 제품/운영 결정이 바뀌었으면 `docs/decisions.md`에 짧게 추가/수정한다
5. `PYTHONPATH=src python3 -m recruit_crawler.cli status-report --check`
6. 관련 focused tests 또는 full `PYTHONPATH=src python3 -m unittest discover -s tests`
7. `git diff --check`

## 진행상황 기록

- `TODO.md`는 앞으로 해야 할 일만 담는다. 완료 항목은 제거하고 완료 기록 파일은 만들지 않는다.
- `docs/status.md`는 현재 되는 것, 아직 안 되는 것, 다음 추천만 보여주는 생성물이다.
- `docs/decisions.md`는 오래 유지할 제품/운영 결정만 짧게 담는다. 작업 완료 로그나 세션 요약을 넣지 않는다.
- `docs/archive/`는 사용하지 않는다. 새 archive 파일이나 폴더를 만들지 않는다.
- `.gjc/`는 로컬 런타임 상태다. 일반 진행상황 파악에는 쓰지 않는다.

## GitHub / PR 규칙

- TBD

## 브라우저 검증 하네스

- 자동 검증은 기본 headless browser 또는 테스트 전용 임시 `user_data_dir`만 사용한다.
- 사용자의 실사용 Chrome 프로필은 사용자가 그 턴에서 명시 요청한 수동 검증에만 연결한다.
- 내가 연 브라우저/탭만 닫는다. 외부 Chrome 프로세스는 종료하거나 `kill`하지 않는다.
- Chrome/Chromium crash, `Target closed`, disconnect, protocol error, 예기치 않은 종료가 나오면 즉시 반복을 멈추고 실패 URL·동작·artifact를 기록한 뒤 원인을 분석한다.
- 브라우저 검증 완료 주장은 transcript artifact(open/navigate/evaluate와 관찰 결과)가 있을 때만 한다. 스크린샷이나 기억만으로 완료 처리하지 않는다.
- user-operated Chrome extension/browser-use/OCR/수동 검토는 fallback/manual evidence다. `enabled` target 근거는 `public_http` 또는 no-human `browser_automation` 증거만 인정한다.
