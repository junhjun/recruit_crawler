# 모델 기반 personal_info context 추출 리팩토링 보고서

## 요약

현재 `personal_info` 문서를 채용공고 ranking에 쓰는 경로는 동작하지만, 핵심 입력 품질이 낮다. 기존 deterministic parser는 `Roles:`, `Skills:`, `Locations:`, `Deal breakers:` 같은 명시 라인과 작은 키워드 목록에 의존한다. 실제 CV/PDF에서는 세부 연구·프로젝트·기술 스택을 거의 구조화하지 못했고, OCR/날짜 노이즈를 경력 연차로 오인해 `6002018` 같은 값을 만들었다.

GPT-5.5 수준의 모델이 문서 텍스트를 읽어 구조화한다고 가정하면 훨씬 유용한 context가 나온다. 모델식 추출 시뮬레이션은 `ML Engineer`, `Computer Vision Engineer`, `PyTorch`, `CLIP`, `YOLOv8`, `Stable Diffusion`, `Deepfake detection`, `Dataset construction`, `max_experience_years=2` 같은 ranking에 직접 쓰기 좋은 신호를 만들었다. fake extractor 기반 회귀 테스트에서는 ML fixture가 50점 초과로 올라가 1순위가 되었고, synthetic CLI dry-run에서도 `ML Engineer, Recommendation Systems`가 1순위로 유지됐다.

추천 방향은 **single aggregated prompt + strict schema + deterministic fail-closed fallback + fingerprint cache**다. 실제로 파싱할 `personal_info` 문서 수가 적고, 전체 이력서/포트폴리오/선호조건 맥락을 한 번에 보는 편이 role·skill·location·경력 상한을 더 일관되게 뽑는다. 문서별 extraction이나 chunked map-reduce는 문서가 매우 길어지거나 모델 입력 한도를 넘는 경우의 fallback으로 두는 것이 낫다.

## 현재 상황 요약

현재 기능은 “동작하는 deterministic personalization” 단계다. `--context-doc`와 scheduled/live quality gate는 이미 있으므로, 리팩토링의 목표는 ranking engine 전체를 LLM으로 바꾸는 것이 아니라 **사용자 context 입력 품질을 모델로 보강하되, 실패 시 기존 deterministic gate가 안전하게 막는 구조**로 바꾸는 것이다.

## 현재 구현과 관찰 결과

현재 경로:

1. CLI가 `--context-doc` 문서를 받는다.
2. `parse_context_document()`가 `.txt`, `.md`, `.pdf`, `.docx` 텍스트를 읽는다.
3. `_section_values()`가 명시 섹션을 추출한다.
4. 명시 skills가 없으면 작은 keyword fallback을 쓴다.
5. `_infer_max_experience()`가 `N years` 또는 `N년` 패턴의 최대값을 경력 상한으로 삼는다.
6. `score_snapshot()`이 JD 구조 필드와 skills/location/role을 substring matching으로 비교한다.

관찰된 deterministic 추출 결과:

| 입력 | 추출 결과 | 문제 |
| --- | --- | --- |
| `preferences.md` | role `ML/AI Engineer`, skill `ML`, location `서울`, `원격/하이브리드 무거나` | 경력 상한 없음, location 오타 보정 없음 |
| 이력서/포트폴리오 PDF | skill `Data`, max experience `23` | role/location 누락, 날짜/숫자 오인 |
| 석사 CV PDF | `Python`, `Data` | 연구/비전/딥페이크/모델링 신호 누락 |
| 학부 CV PDF | `Python`, `ML`, `Data`, max experience `6002018` | OCR/기간 노이즈를 경력으로 오인 |

기존 parser의 가장 위험한 결함은 경력 숫자 추론이었다. 이번 리팩토링에서 deterministic 추론은 `1..20` 범위만 경력으로 인정하도록 fail-closed 처리했다.

## GPT 기반 추출 방식 선택지

### 1. Single aggregated prompting

여러 `personal_info` 문서를 순서대로 하나의 bundle prompt로 합친 뒤 JSON schema로 한 번만 뽑는다.

장점:

- 구현이 가장 단순하다.
- 문서 수가 적은 현재 사용 패턴에서는 비용과 latency가 낮다.
- 전체 맥락을 한 번에 보므로 role/skill/location 간 일관성이 좋다.
- prompt와 schema를 고정하면 테스트 fixture로 회귀 검증하기 쉽다.
- fingerprint cache를 붙이면 문서 bundle이 바뀐 경우에만 재호출한다.

단점:

- 긴 PDF나 OCR 깨짐이 심한 문서에서는 누락·과대요약 위험이 있다.
- 문서 하나만 바뀌어도 bundle fingerprint가 바뀌므로 전체를 다시 호출한다.

### 2. 문서별 schema extraction 후 deterministic merge

각 context document를 독립적으로 모델 추출하고, 결과를 `UserContext`로 변환한 뒤 merge한다.

장점:

- resume, portfolio, preferences의 provenance를 분리할 수 있다.
- 특정 문서 실패 시 전체 context를 버리지 않고 fail-closed 처리하기 쉽다.
- `personal_info/preferences.md`처럼 사람이 고친 파일을 우선하거나 보강하기 쉽다.

단점:

- 문서 수만큼 호출이 늘 수 있다.
- merge 정책이 필요하다. 예: `max_experience_years`는 최대가 아니라 “합리 범위 내 명시값 우선”이어야 한다.

현재 판단:

- 이전 리팩토링의 후보였지만, 현재 전제에서는 기본값으로 과하다.
- provenance 분리는 장점이지만, 비용·복잡도·merge policy가 늘어난다.
- single aggregated prompt가 실패하거나 입력 한도에 걸릴 때만 fallback 후보로 둔다.

### 3. Chunked map-reduce

긴 PDF를 chunk로 나누어 추출한 뒤 최종 reducer가 중복 제거·우선순위·confidence를 정한다.

장점:

- 긴 포트폴리오나 논문 목록에서 coverage가 좋다.
- OCR이 일부 깨져도 다른 chunk가 보완할 수 있다.

단점:

- 비용/latency가 가장 높다.
- reducer prompt와 merge test가 필요하다.
- 현재 프로젝트 규모에서는 첫 도입으로는 과하다.

### 4. Cache-first incremental extraction

문서 텍스트 fingerprint를 만들고, 동일 fingerprint는 저장된 structured extraction을 재사용한다.

장점:

- Codex scheduled run에서 매일 같은 CV를 다시 모델 호출하지 않는다.
- raw text를 저장하지 않고 fingerprint와 구조화 결과만 저장할 수 있다.
- 비용과 latency를 크게 낮춘다.

단점:

- cache invalidation 정책이 필요하다.
- schema version, model id, effort도 fingerprint key에 포함하는 것이 더 정확하다.

## 비용, latency, privacy, 재현성, 장애 모드 고려

이 환경에서는 외부 OpenAI API key와 `openai` 패키지가 없어 실제 API별 비용/latency benchmark는 수행하지 못했다. 아래는 현재 로컬 테스트와 모델 추출의 일반적 동작 특성에 기반한 설계 판단이다.

| 선택 | 비용 | 품질 | 추천 용도 |
| --- | ---: | ---: | --- |
| deterministic only | 최저 | 낮음 | fallback, privacy fail-closed |
| single aggregated prompt, medium effort | 낮음~중간 | 높음 | 기본 scheduled run |
| single prompt, high effort | 중간 | 높음 | 초기 onboarding, schema drift 점검 |
| chunked map-reduce | 높음 | 최고 가능 | 긴 포트폴리오, 품질 진단 batch |

Tradeoff:

- 비용: 매일 scheduled run에서는 같은 문서를 반복 호출하지 않도록 fingerprint cache가 필수다.
- latency: single aggregated prompt는 호출 1회라 가장 단순하고 빠르다. 긴 PDF에서는 chunked extraction이 더 안정적일 수 있다.
- privacy: raw personal text는 repo, report, cache에 저장하지 않는다. private canary를 모델 호출 전에 검사하고, aggregate는 delimiter가 아닌 JSON string으로 prompt에 넣는다. 모델 출력은 field별 item 수·길이·single-line 제약과 긴 verbatim source passage 차단을 통과해야 cache나 report로 이동할 수 있다.
- 재현성: 모델 결과는 deterministic ranking으로 들어가기 전에 schema와 range guard를 통과해야 한다.
- 장애 모드: 모델 미사용, cache miss 후 호출 실패, 불완전 JSON, 비현실적 경력 숫자는 모두 missing context로 남기고 quality gate가 fail-closed해야 한다.

## GPT-5.5 effort별 기대값

아래 평가는 **실측과 추론을 구분**한다. 이번 repo 검증에서 실측한 것은 fake extractor 기반 schema 흐름, fingerprint cache, deterministic bad-year guard, ranking movement다. 실제 GPT-5.5 low/medium/high/xhigh API 비교는 이 환경에 API key와 SDK가 없어 수행하지 못했다.

| effort | 기대 결과 | 비용/latency | 판단 근거 |
| --- | --- | ---: | --- |
| low | 명시 섹션과 짧은 preferences 문서는 충분할 수 있으나, CV/PDF의 암묵적 role·skill 추론은 누락될 가능성이 있다. | 낮음 | 추론 |
| medium | schema field 추출, 노이즈 제거, 한국어/영어 혼합 normalization의 균형점으로 적합하다. | 중간 | 추론 + fake schema test로 architecture 적합성 확인 |
| high | 긴 포트폴리오와 연구 설명에서 더 넓은 skill/role recall을 기대할 수 있다. 과추출 noise는 deterministic merge가 제어해야 한다. | 높음 | 추론 |
| xhigh | chunk reduce, 모순 해결, provenance 설명에는 유리할 수 있으나 매일 scheduled 기본값으로는 비용 대비 이득이 불확실하다. | 최고 | 추론 |

결론적으로 기본값은 **single aggregated prompt + medium effort + 엄격 JSON schema + cache**가 합리적이다. high/xhigh는 매일 실행이 아니라 초기 personal_info onboarding, 실패 분석, 또는 사용자가 수동 재추출을 요청할 때 쓰는 편이 낫다.

## 이전 방식과 새 방식 비교

| 항목 | 이전 후보: 문서별 모델 추출 | 현재 선택: single aggregated prompt |
| --- | --- | --- |
| 모델 호출 수 | 문서 수만큼 호출 | 문서 bundle당 1회 |
| context 품질 | 문서 간 맥락은 merge 정책에 의존 | 전체 맥락을 한 번에 보고 일관된 schema 생성 |
| cache key | 문서별 fingerprint | 전체 bundle fingerprint |
| 비용/latency | 문서 수에 비례 | 보통 1회 호출 |
| 구현 복잡도 | 문서별 실패/merge/provenance 정책 필요 | prompt builder와 schema parser 중심 |
| failure fallback | 실패 문서만 deterministic fallback 가능 | 모델 실패 시 전체 bundle을 deterministic merge로 fallback |
| privacy guard | 각 문서별 호출 전 검사 | 모든 문서를 먼저 검사하고 하나라도 private marker가 있으면 모델 호출 없음 |

현재 repo 구현은 `apply_context_documents(..., extractor=...)`에서 모든 context document를 하나의 aggregate bundle로 합친다. `CodexThreadContextExtractor`는 bundle을 strict JSON 지시와 함께 새 Codex thread의 initial prompt로 한 번만 보내고, 응답을 읽어 schema로 검증한 직후 thread를 archive한다. extractor가 없거나 thread 생성·읽기·JSON 검증이 실패하면 기존 deterministic 문서별 parser와 merge를 그대로 사용한다.

## 추천 아키텍처

1. `ModelContextExtraction` schema를 둔다.
2. 모델 호출자는 `ContextExtractor` protocol로 감싸고, Codex app 연동은 `CodexThreadRunner`의 create/read/archive 세 동작만 주입한다.
3. `CodexThreadContextExtractor`가 model id와 effort를 thread 생성에 전달하고 strict JSON을 `ModelContextExtraction`으로 파싱한다.
4. 성공과 처리 가능한 실패 모두 생성된 임시 thread의 archive를 시도한다.
5. 테스트와 offline run은 fake extractor 또는 fake thread runner를 사용한다.
6. 추출 결과는 `UserContext`로 변환한다.
7. `max_experience_years`는 `1..20` 범위만 인정한다.
8. schema version, model id, effort를 포함한 full SHA-256 fingerprint로 cache key를 만들고 raw text는 저장하지 않는다.
9. app host는 `ContextExtractionRuntime`으로 extractor와 `SqliteContextExtractionCache`를 정상 CLI context-loading 경로에 주입한다.
10. structured cache는 fingerprint와 검증된 schema 결과만 권한 제한 SQLite transaction으로 저장한다.
11. scheduled run은 context가 부족하면 기존처럼 `needs_context`로 실패한다.

Python 코드가 Codex app 도구를 직접 호출할 수 없으므로 app 소유 integration layer가 `CodexThreadRunner`를 구현한다. 이 경계는 Codex의 `create_thread(initial prompt)`, 완료 대기 후 final assistant JSON을 반환하는 `read_thread`, `set_thread_archived(true)` 의미에 맞춰져 있으며 OpenAI API key나 `openai` SDK를 사용하지 않는다. app host는 이 runner로 만든 extractor와 persistent structured cache를 `ContextExtractionRuntime`으로 `load_config_with_context()`에 주입한다. repo unit test는 live model에 의존하지 않고 동일 lifecycle과 CLI context-loading hook을 fake runner로 검증한다.

## 리팩토링 계획

1. `model_context.py`에 schema/protocol/fingerprint를 둔다.
2. `parse_context_documents_with_extractor()`를 추가해 여러 context document를 하나의 prompt bundle로 합친다.
3. `apply_context_documents(..., extractor=..., cache=...)`는 extractor가 있을 때 single aggregated prompt 경로를 사용하고, extractor가 없을 때 기존 deterministic merge를 유지한다.
4. deterministic `_infer_max_experience()`는 비현실적 숫자를 무시한다.
5. ranking은 당장 semantic scorer로 바꾸지 않고, 입력 context 품질 개선이 점수 변화로 이어지는지 먼저 고정한다.
6. `CodexThreadContextExtractor`와 injectable runner boundary를 추가하고 strict JSON/lifecycle을 검증한다.
7. 이후 단계에서 persistent schema-versioned cache와 feedback 반영 ranking을 추가한다.

## 테스트 결과 요약

새 focused test는 다음을 검증한다.

- `6002018 years` 같은 OCR 노이즈는 deterministic 경력 상한으로 쓰지 않는다.
- fake model extractor가 구조화한 role/skill/location/experience/deal breaker는 `UserContext`로 변환된다.
- 여러 문서가 있어도 model extractor는 aggregate prompt 1개로 한 번만 호출된다.
- cache miss에서는 Codex thread가 정확히 하나 생성되고, strict JSON 응답이 `ModelContextExtraction`으로 변환된다.
- 성공, read 실패, JSON schema 실패 뒤 생성된 임시 thread를 archive한다.
- 예상 밖 read 예외에도 `finally`에서 archive를 시도하고, archive 실패는 raw adapter detail 없이 fail-closed 처리한다.
- context bundle은 JSON string으로 encode해 delimiter closure를 데이터로 취급한다.
- 긴 원문 passage를 model output이 되돌리면 cache 저장 전에 거부하고 deterministic fallback을 사용한다.
- unchanged aggregate cache hit에서는 새 thread를 만들지 않는다.
- 새 cache instance를 쓰는 다음 실행도 structured JSON cache를 읽어 thread를 다시 만들지 않는다.
- cache는 raw text 없이 aggregate fingerprint 기반으로 extractor 재호출을 막는다.
- raw personal text는 cache나 생성 report에 남지 않는다.
- fingerprint는 schema version, model id, effort가 바뀌면 달라진다.
- private canary 문서는 extractor 호출 전에 차단된다.
- extractor 실패는 deterministic parser로 fallback되고 부족한 context는 그대로 missing으로 남는다.
- context가 여전히 부족하면 live preflight gate는 fail-closed 한다.
- 모델식 context는 ML fixture ranking을 의도한 방향으로 올린다.

실행 명령:

```bash
PYTHONPATH=src python3 -m unittest tests.test_codex_thread_context tests.test_codex_thread_security tests.test_codex_thread_runtime_wiring tests.test_model_context_extraction
PYTHONPATH=src python3 -m unittest tests.test_model_context_extraction tests.test_user_context tests.test_user_context_cli tests.test_live_context_cli tests.test_scheduled_policy
PYTHONPATH=src python3 -m unittest discover -s tests
PYTHONPATH=src python3 -m recruit_crawler.cli dry-run --config config/sample_config.json --run-date 2026-07-10 --context-doc fixtures/user_context/context.md --print-report
PYTHONPATH=src python3 -m recruit_crawler.cli live-run --config /tmp/recruit-crawler-single-aggregated-live/live_sources.json --run-date 2026-07-10 --quality-gate-output /tmp/recruit-crawler-single-aggregated-live/gate.json --print-report
```

결과:

- Codex thread/model context focused tests: 30 tests passed.
- context/scheduled focused regression: 54 tests passed, 1 skipped.
- full unittest suite: 151 tests passed, 1 skipped.
- Codex app manual QA: synthetic context를 initial prompt 하나로 보낸 disposable thread가 strict JSON을 반환했고, 응답을 읽은 직후 thread를 archive했다.
- dry-run manual QA: `ML Engineer, Recommendation Systems`가 `70점 hold`로 1순위.
- live-run QA: enabled source 6개에서 후보 86개를 수집했고 quality gate는 `pass`, context status는 `complete`.

## 남은 일

- app-owned `CodexThreadRunner` implementation: Codex app host가 create/read/wait/final-response/archive 도구 호출을 세 protocol method에 연결해야 한다. repo의 CLI context-loading hook과 cache 주입 경계는 구현되어 있다.
- thread timeout/retry와 privacy-safe 운영 logging 정책: raw prompt/response를 기록하지 않는 host 정책으로 별도 확정한다.
- structured cache retention/cleanup policy: host는 `personal_info/` 아래 cache 경로와 보존 기간을 정해야 한다.
- ranking 고도화: 현재는 여전히 substring scorer다. 입력 context가 좋아지면 개선되지만, JD와 개인 프로젝트 간 의미적 전이 판단은 아직 하지 않는다.
- feedback history 반영: TODO에 남아 있는 deterministic relevance evaluation 연동이 다음 ranking 품질 개선 단계다.
