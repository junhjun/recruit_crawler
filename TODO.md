# Recruit Crawler TODO

마지막 업데이트: 2026-07-09

## 원칙

- 이 문서는 앞으로 해야 할 일만 적는다.
- 완료 기록 파일은 만들지 않는다.
- 현재 상태는 `docs/status.md`, 오래 유지할 결정은 `docs/decisions.md`에 둔다.

## Roadmap backlog

### Phase 1 — Source reliability maintenance

- [ ] JobKorea JSON-LD fallback으로 수집한 공고의 상세 JD 품질을 계속 샘플링
- [ ] Rallit/Jumpit/RocketPunch 상세 parser shape 변경 감지용 fixture를 주기적으로 갱신
- [ ] Source registry/docs/config/test refs 일치 여부를 release 전 체크리스트로 유지

### Phase 2 — Daily-use feedback and ranking quality

- [ ] Feed stored feedback into deterministic relevance evaluation without live external LLM defaults
- [ ] Add regression cases for false negatives, false positives, and user deal-breaker drift

### Phase 3 — Personal history and customization

- [ ] Expand scheduled history queries for source health, recommendation changes, personal_info coverage, and filter-rule effects
- [ ] Define Codex thread timeout/retry and privacy-safe host logging policy without raw prompt or response text
- [ ] Define retention and cleanup policy for the structured model-context cache

### Phase 4 — Codex scheduled product loop

- [ ] Add source-health maintenance command that samples enabled source parsers and flags zero-candidate or shape-drift failures
- [ ] Define artifact retention and cleanup policy for reports, evidence transcripts, DB rows, and feedback history

### Phase 5 — Reusable distribution

- [ ] Add privacy-first onboarding docs for personal_info, local storage, allowed persisted fields, and excluded/non-target sources
- [ ] Defer web UI/service mode until scheduled runner, persistence, feedback ingestion, customization, and source health gates are stable
