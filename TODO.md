# Recruit Crawler TODO

마지막 업데이트: 2026-07-01

## 원칙

- 이 문서는 앞으로 해야 할 일만 적는다.
- 완료된 작업, 상태 스냅샷, 검증 이력은 `docs/archive/`로 옮긴다.

## V1 gate ledger

- [x] UserContext schema and deterministic verdict contract
- [x] CV/PDF/DOCX document parsing producer
- [x] Supplemental interview for missing context
- [x] RelevanceCase seed labels and deterministic evaluation
- [x] Two company-careers source candidate gate
- [ ] Implement source-specific company-careers parser with non-zero live candidates
- [x] Headless browser evidence CLI
- [x] Docs/config/tests consolidation

## Follow-up backlog

- [ ] Company careers: NAVER/Kakao/LINE/Coupang은 후보 검토만 완료했고 live 후보 0개라 deferred 유지. 나중에 source-specific parser와 non-zero live evidence를 만든 뒤 enable
- [ ] Saramin/Wanted 검색/목록 discovery를 `detail_urls` 직접 지정에서 no-human public discovery로 확장
- [ ] JobKorea JSON-LD fallback으로 수집한 공고의 상세 JD 품질을 계속 샘플링
- [ ] Rallit/Jumpit 상세 parser shape 변경 감지용 fixture를 주기적으로 갱신
- [ ] Source registry/docs/config/test refs 일치 여부를 release 전 체크리스트로 유지
