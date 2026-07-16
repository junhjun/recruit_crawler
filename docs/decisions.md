# Decisions

This file keeps only durable product and operating decisions. It is not a work log.

## Progress Tracking

Decision: Do not use `docs/archive/` for progress tracking.

Reason: This project is small enough that archive files add management overhead. Progress should stay visible through `TODO.md` and the generated `docs/status.md`.

Current rule:

- `TODO.md` contains future work only.
- `docs/status.md` shows current capability, gaps, and next work.
- `docs/status/features.json` is the source for generated status.
- This file records only long-lived product or operating decisions.

## Non-Target Sources

Decision: Keep LinkedIn automatic collection and company-careers expansion out of the current target scope.

Reason: LinkedIn has auth/session/privacy risk, and company-specific career pages would add source-specific parser work before the core local recruiting workflow is stable.

## V2 Deterministic Recommendation Contract

Decision: Use one immutable `PipelineResultV2` as the source for public projection, Korean report bytes, GateV2, scheduled persistence envelope, and feedback identity.

Reason: Keeping ranking, rendering, quality checks, and persistence on one deterministic result prevents early-truncation drift and prevents raw JD, private context, military evidence details, and opaque identity material from crossing public boundaries.

Current rule:
- `top_n` and `manual_review_n` limit only rendered projection queues, never terminal assessments.
- LinkedIn is manual capture-only and excluded from automatic source collection.
- Storage is forward-only v3; valid v3 databases are read-only during initialization and scheduled persistence accepts only a validated v3 envelope.
## V3 Opportunity-First Report Contract

Decision: Rank near matches instead of excluding them for ordinary experience gaps, and expose only Korean action states in reports.

Reason: A strict perfect-match filter hid realistic applications. Users need prioritized opportunities with clear tradeoffs, not internal classifier codes.

Current rule:
- A one-year mandatory experience gap is a penalized `도전 지원` candidate; larger or ambiguous numeric gaps are `원문 확인 필요`, not automatic exclusions.
- Reports use `지원 추천`, `도전 지원`, `원문 확인 필요`, and `제외`; raw codes, raw JD, private context, and military details remain non-public.
- Clickable links require a verified canonical per-posting URL. Generic listing/search links are shown as link verification needed.
- A Gate PASS requires a validated published report; report integrity or partial-source warnings cannot silently pass.
## Jumpit Sitemap Failure Policy
Decision: Keep Jumpit sitemap timeout handling fail-closed and defer a public-listing fallback.
Reason: The current failure is an external sitemap response delay; an unreviewed fallback could silently omit postings or weaken link and privacy safety.
Current rule:
- A Jumpit sitemap timeout remains a visible `collection_error`; a validated partial live report may publish successful-source results with a visible source-failure notice, while Gate remains `fail`.
- Reconsider a fallback only after persistent unavailability and a separate public endpoint, robots/TOS, parser, fixture, and live-evidence review.
