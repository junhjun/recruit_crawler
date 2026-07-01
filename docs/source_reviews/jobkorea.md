# JobKorea Source Access Review

Status: `draft_review_only`
Checked date: 2026-06-30
Candidate priority: first real connector candidate

## Source

- Source ID: `jobkorea`
- Domains: `www.jobkorea.co.kr`
- Access mode under consideration: public job listing and public posting pages
- Auth required: no for public pages under consideration; authenticated/member areas are out of scope
- Rate limit: unknown; default proposal is conservative manual smoke testing only before any unattended use
- Terms or robots review status: unknown, not passed
- Allowed persisted fields under consideration:
  - source ID
  - source URL
  - source posting ID if visible in URL or page metadata
  - title
  - company
  - location
  - deadline
  - structured JD snapshot summary
- Failure mode: skip source and continue ranking other source outputs

## Evidence Observed

`https://www.jobkorea.co.kr/robots.txt` was reachable during review. It listed public job-listing and posting paths such as `/recruit/joblist` and `/Recruit/GI_Read` as allowed for general rules, while blocking login, user, account, company-management, and some search paths. It also contained explicit restrictions for AI/LLM crawlers and known web scrapers.

This is not a ToS approval. `robots.txt` is treated as one access signal only, not as sufficient permission to run an unattended connector.

## Decision

- Status: `fail_pending_review`
- Rationale: candidate is plausible for manual public-page smoke review, but legal/ToS status, anti-bot behavior, rate limits, and field-level persistence constraints are not yet approved.
- Next safe step: create a manual smoke-test checklist using one or two user-opened public URLs, then decide whether to implement a disabled adapter fixture around saved HTML.

## Connector Guardrails

Any future `jobkorea` adapter must remain disabled by default until this review changes to `pass`.

Required before enablement:

- Confirm ToS and acceptable-use constraints.
- Confirm no login/session/authenticated pages are needed.
- Confirm public page selectors using saved HTML fixtures first.
- Add rate limiting and source-specific error handling.
- Add tests proving the adapter cannot run in unattended mode while `tos_review_status != "pass"`.
- Persist only normalized structured fields, never raw full JD text.
