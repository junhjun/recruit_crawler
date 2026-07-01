# Recruit Crawler TODO

마지막 업데이트: 2026-07-01

## 원칙

- 이 문서는 앞으로 해야 할 일만 적는다.
- 완료된 작업, 상태 스냅샷, 검증 이력은 `docs/archive/`로 옮긴다.

## 다음 작업

- [ ] Saramin/Wanted 검색/목록 discovery를 `detail_urls` 직접 지정에서 no-human public discovery로 확장
- [ ] JobKorea JSON-LD fallback으로 수집한 공고의 상세 JD 품질을 계속 샘플링
- [ ] Rallit/Jumpit 상세 parser shape 변경 감지용 fixture를 주기적으로 갱신
- [ ] Source registry/docs/config/test refs 일치 여부를 release 전 체크리스트로 유지
- [ ] Company careers를 도메인별 `public_http` target으로 확장할 후보 회사를 선정
- [ ] 첫 GitHub baseline push 이후 PR/TDD/reviewer/branch protection 규칙을 정리
- [ ] 반복 브라우저 검증이 필요해지면 headless/임시 프로필만 쓰는 전용 browser evidence harness를 설계
