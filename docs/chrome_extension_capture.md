# Chrome Extension Capture

This path is the practical V1 fallback collection route for supported public sources and reviewed manual/import targets.
It captures only job posting fields visible in the active Chrome tab and downloads a
local JSON file. It does not read cookies, session tokens, auth headers, local
storage, credentials, or unrelated account/profile data.

For the source-by-source collection method and verification matrix, see
`docs/source_collection_matrix.md`.

## Load The Extension

1. Open `chrome://extensions`.
2. Enable Developer mode.
3. Click "Load unpacked".
4. Select `browser_extension/` from this repository.

After changing files under `browser_extension/`, return to `chrome://extensions`
and click Reload on the unpacked extension before smoke-testing another capture.
The content script is passive on page load: it may inject the button and register
the extension command handler, but it must not capture, scroll, download, or
generate persisted diagnostics until the user clicks the button or popup command.

## Capture

1. Open a supported target page in Chrome: Saramin, JobKorea, Wanted, Rallit, or RocketPunch. LinkedIn remains available only for reviewed manual imports and historical regression coverage.
2. Use either capture entry point:
   - Click the in-page "Recruit Capture" button at the lower-right of the page.
   - Or click the "Recruit Capture" extension icon, then click "Capture visible postings".
3. Chrome downloads a JSON file under
   `Downloads/recruit-captures/YYYY-MM-DD/{source}/recruit-capture-{source}-{timestamp}.json`.
4. Import captured files into the scoring/report pipeline:
   - Latest dated spool: `PYTHONPATH=src python3 -m recruit_crawler.cli capture-import --latest`
   - Specific date: `PYTHONPATH=src python3 -m recruit_crawler.cli capture-import --date 2026-06-30`
   - Specific files: `PYTHONPATH=src python3 -m recruit_crawler.cli capture-import --file path/to/capture.json`
   Reports are written under `reports/` as `recruiting-capture-import-YYYY-MM-DD.md`.
5. Write a machine-readable quality gate for the exact captured files:
   - Latest dated spool: `PYTHONPATH=src python3 -m recruit_crawler.cli capture-quality-gate --latest --output artifacts/recruit_capture_results/quality_gate.json`
   - Specific date: `PYTHONPATH=src python3 -m recruit_crawler.cli capture-quality-gate --date 2026-06-30 --output artifacts/recruit_capture_results/quality_gate.json`
   - Specific files: `PYTHONPATH=src python3 -m recruit_crawler.cli capture-quality-gate --file path/to/capture.json --output artifacts/recruit_capture_results/quality_gate.json`

## Current Status

The extension exports visible result-card/detail data with these fields:

- `source_id`
- `source_url`
- `source_posting_id`
- `title`
- `company`
- `location`
- `deadline`
- `skills`
- `requirements`
- `captured_at`

Operational status after the reliability hardening pass:

- Static extension load is explicitly passive. Active capture and downloads run
  only through `recruit-capture:capture-visible-postings` after the user clicks
  the floating button or popup.
- Capture payloads include `extension_version` and `diagnostics` metadata:
  schema/source/mode, posting count, detail lengths, marker hits, extraction
  strategy, source-specific click-through counts, iframe status where applicable,
  warnings/errors, and manual review flags.
- `capture-quality-gate` produces structured `pass`, `pass_with_warnings`,
  `manual_review_required`, or `fail` output. Invalid JSON and credential/session
  findings fail; public JD contact emails/phones are warnings/manual review;
  Saramin image-only JDs are manual review required.

The older entries below are smoke evidence, not a substitute for a fresh quality
gate run against the current extension version.

Smoke-test results from Chrome on 2026-06-30:

- JobKorea search page: 80 visible postings captured. Current verified mode is
  result-card/search-page visible fields.
- Saramin search page: 64 visible postings captured. Current verified mode is
  result-card/search-page visible fields.
- Saramin text/iframe detail page:
  `Downloads/recruit-captures/2026-06-30/saramin/recruit-capture-saramin-2026-06-30T03-54-14-672Z.json`
  captured 1 posting with `capture_mode=current_detail`; `requirements` was
  2,911 characters and included `서비스 소개`, `모집부문`, `주요업무`, `자격요건`,
  `우대사항`, `LLM`, `RAG`, and `FastAPI`. The frame selector used title/company
  hints and did not capture the unrelated recommendation-frame company `두핸즈`.
- JobKorea detail page:
  `Downloads/recruit-captures/2026-06-30/jobkorea/recruit-capture-jobkorea-2026-06-30T04-04-24-339Z.json`
  captured 1 posting with `capture_mode=current_detail`; `requirements` was
  4,365 characters and included `플라잎`, `이런 업무를 해요`, `이런 분들을 찾고 있어요`,
  `Python`, and `Physical AI`.
- LinkedIn logged-in detail page:
  `Downloads/recruit-captures/2026-06-30/linkedin/recruit-capture-linkedin-2026-06-30T04-00-12-138Z.json`
  captured 1 posting with `capture_mode=current_detail`; the `requirements`
  field was 3,598 characters and included detail markers such as `채용공고 정보`,
  `General Summary`, and `Minimum Qualifications`.
- LinkedIn logged-in search results page:
  `Downloads/recruit-captures/2026-06-30/linkedin/recruit-capture-linkedin-2026-06-30T03-37-47-208Z.json`
  captured 7 visible postings with `capture_mode=visible_detail_clickthrough`;
  each row included detailed JD text in `requirements` with lengths from 688 to
  9,509 characters and detail markers such as `채용공고 설명`, `Key responsibilities`,
  `Minimum Qualifications`, or equivalent sections.
- Captured JSON did not include cookie, session, password, authorization, bearer,
  CSRF, or `li_at` strings in the smoke-test files.

## Known Limits

- LinkedIn capture support is retained only for ad hoc manual imports and
  historical regression coverage; LinkedIn is not a V1 target source.
- Saramin text detail pages and JobKorea detail pages are verified for detailed
  JD body capture through the in-page Chrome extension button. Same-origin frames
  are read through normal DOM access; cross-origin/inaccessible frames are not
  bypassed.
- Saramin image-only uploaded JD pages expose the JD body as an image rather than
  selectable DOM text. Those postings require OCR or manual review if the image
  text is needed for ranking.
- JobKorea detail-page location strings are normalized during `capture-import`;
  suffixes such as `마감일 ~7/28(화` are stripped, and `근무지 주소` from the JD body
  is preferred when present.
- `capture-import` skips empty captures, records invalid JSON and duplicate
  postings as source errors in the report, rejects sensitive posting fields such
  as session tokens, and imports only the normalized posting fields listed above.
