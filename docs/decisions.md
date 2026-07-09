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
