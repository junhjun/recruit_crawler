# 2026-07-01 User Context and Live Gate Archive

## 완료된 작업

- UserContext schema, FeedbackEvent, RelevanceCase, deterministic verdict contract 추가
- text/Markdown/PDF/DOCX context document parsing producer 추가
- 여러 `--context-doc` 입력을 merge하도록 CLI 지원
- context 문서에 누락된 필드가 있으면 CLI 보충 interview로 roles/skills/location/experience를 입력받도록 연결
- scoring을 `config.user_context` 기준으로 전환
- explicit deal-breaker는 exclude, missing context는 hold/interview 흐름으로 처리
- 30개 RelevanceCase seed labels와 deterministic evaluator 추가
- browser evidence CLI와 redacted transcript 생성 추가
- live-run source quality gate 추가
  - source별 attempted/candidate/error metrics 기록
  - enabled source가 0 candidates면 gate fail
  - `--quality-gate-output` JSON 출력
- company-careers 후보는 live candidate 0개라 `naver_careers`, `kakao_careers`, `line_careers`, `coupang_careers` 모두 deferred 유지
- `.github/PULL_REQUEST_TEMPLATE.md` 추가
- README/docs에 context input, repeated `--context-doc`, supplemental interview, live quality gate 사용법 반영

## 주요 커밋

- `37d81b8` Add user context relevance gates and browser evidence CLI
- `ee5acca` Add live-run source quality gate
- `17a026f` Wire user context document into CLI runs
- `b8d4ef7` Support multiple user context documents
- `23f849a` Add supplemental context interview to CLI
- `2f11e08` Improve supplemental interview prompt formatting

## 현재 사용 예

```sh
PYTHONPATH=src python3 -m recruit_crawler.cli live-run \
  --config config/live_sources.sample.json \
  --context-doc path/to/resume.md \
  --context-doc path/to/portfolio.md \
  --quality-gate-output artifacts/live_quality_gate.json \
  --run-date 2026-07-01 \
  --print-report
```

Context 문서에 필수 필드가 부족하면 CLI가 보충 질문을 실행하고, 답변을 UserContext에 merge한 뒤 scoring/ranking을 수행한다.

## 검증 이력

```sh
PYTHONPATH=src python3 -m unittest discover -s tests
# Ran 79 tests in 1.104s
# OK (skipped=1)

git diff --check
# 통과
```

Focused 검증:

```sh
PYTHONPATH=src python3 -m unittest tests.test_dry_run.UserContextCliTests tests.test_dry_run.SupplementalInterviewTests
# Ran 6 tests
# OK
```

## 상태 스냅샷

- Enabled sources: `saramin`, `jobkorea`, `wanted`, `jumpit`, `rallit`, `rocketpunch`
- Deferred company-careers: `company_careers`, `naver_careers`, `kakao_careers`, `line_careers`, `coupang_careers`
- Excluded: `linkedin`

## 남은 후속 작업

- company-careers source-specific adapter/parser 구현 및 non-zero live candidate evidence 확보
- RelevanceCase seed labels를 테스트 코드에서 fixture 파일로 분리
- source discovery/detail parser drift 샘플링 및 release checklist 유지
