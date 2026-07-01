# 2026-07-01 Initial Baseline Archive

## 완료된 작업

- 기본 dry-run 파이프라인 구현
- Chrome 캡처/import 경로 구현
- source registry / `source-status` 하네스 구현
- target lane/status를 `public_http | browser_automation | null`, `enabled | blocked | deferred | excluded`로 표준화
- Jumpit/Rallit을 no-human `public_http` target으로 유지
- LinkedIn을 V1 target에서 제외
- JobKorea public HTTP detail-body proof 재검증 및 `public_http` target enable
- Saramin public HTTP detail iframe proof 검증 및 `public_http` target enable
- Wanted no-human browser automation/detail proof 검증 및 `public_http` target enable
- RocketPunch를 `policy_override_mode=user_directed_ignore` no-human `browser_automation` target으로 enable
- Repository 문서 오케스트레이션: `AGENTS.md`를 핵심 하네스로 정리하고 `README.md` 내용을 `AGENTS.md`/`TODO.md`/`docs/index.md`로 이관
- 불필요한 런타임 흔적 정리 원칙 확정: GJC는 `.gjc/`, 과거 `.omx/` 흔적은 보존 대상에서 제외

## Baseline 상태

- Enabled target: `jobkorea`, `saramin`, `wanted`, `jumpit`, `rallit`, `rocketpunch`
- Deferred: `company_careers`
- Excluded: `linkedin`

## 검증 기준

```sh
PYTHONPATH=src python3 -m unittest discover -s tests
PYTHONPATH=src python3 -m recruit_crawler.cli source-status --config config/live_sources.sample.json
PYTHONPATH=src python3 -m recruit_crawler.cli live-run --config config/live_sources.sample.json --run-date 2026-07-01
```

## 운영 결정

- `TODO.md`는 앞으로 해야 할 일만 유지한다.
- 완료 항목, 상태 스냅샷, 검증 이력은 `docs/archive/`에 보관한다.
- Initial baseline 이후 `README.md`는 미니멀한 영어 project overview로 다시 둔다.
