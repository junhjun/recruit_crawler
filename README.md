<div align="center">

# Recruit Crawler

<strong>Local recruiting-source crawler and ranking pipeline.</strong><br />
Collect public job postings, parse structured JD fields, score them against a local user context, and write Korean Markdown reports.

</div>

<hr />

## What is this?

Recruit Crawler is a local Python CLI for reviewing recruiting postings with less manual JD reading. The near-term product target is a daily Codex app `예약됨` command: run reviewed no-human sources, score them against user-owned personal context and filtering rules, persist feedback/history, and write Korean recommendation reports.

It focuses on:

- public or no-human source collection paths;
- structured JD parsing with fixture-backed regression tests;
- local scoring, feedback collection, and Korean Markdown reports;
- explicit source registry status for enabled and excluded/non-target sources;
- privacy-bounded personal inputs that stay local.

## Quick start

Run the no-network fixture pipeline:

```sh
PYTHONPATH=src python3 -m recruit_crawler.cli dry-run \
  --config config/sample_config.json \
  --run-date 2026-07-01 \
  --print-report
```

Run with personal context documents instead of the sample profile:

```sh
PYTHONPATH=src python3 -m recruit_crawler.cli dry-run \
  --config config/sample_config.json \
  --context-doc path/to/resume.md \
  --context-doc path/to/portfolio.md \
  --run-date 2026-07-01 \
  --print-report
```

`--context-doc` accepts `.txt`, `.md`, `.pdf`, and `.docx` files and can be repeated for multiple inputs such as resume + portfolio + preference notes. `config.profile` is a fallback/default for fixture runs; personalized runs should pass explicit context documents.

When supplied context documents are missing required fields, the CLI starts a supplemental interview and asks only for the missing role, skill, location, or experience fields before scoring.

Check source registry status:

```sh
PYTHONPATH=src python3 -m recruit_crawler.cli source-status \
  --config config/live_sources.sample.json
```

Run the non-interactive scheduled CLI/service contract intended for later external schedulers such as Codex app `예약됨` or cron:

```sh
PYTHONPATH=src python3 -m recruit_crawler.cli scheduled-run \
  --config config/live_sources.sample.json \
  --context-doc path/to/resume.md \
  --context-doc path/to/preferences.md \
  --run-date 2026-07-01 \
  --output-dir reports/scheduled \
  --quality-gate-output artifacts/scheduled/latest_quality_gate.json
```

`scheduled-run` never prompts for missing context. Missing role, skill, location, or experience fields are written as `needs_context` quality-gate findings so an external scheduler can fail visibly instead of hanging. Its quality gate also includes a stable `run_identity.run_id` derived from command mode, run date, source config hash, and profile/filter hash so same-date reruns reuse deterministic report and gate artifacts.

If a scheduled run reports `context_status: needs_context`, run `context-doctor` once from the Codex app or terminal to fill only the missing fields and persist them as an editable Markdown preferences document:

```sh
PYTHONPATH=src python3 -m recruit_crawler.cli context-doctor \
  --config config/live_sources.sample.json \
  --context-doc personal_info/resume.md \
  --context-doc personal_info/portfolio.md \
  --output personal_info/preferences.md
```

Then include `personal_info/preferences.md` in future scheduled runs with another `--context-doc`. The generated file uses simple `Roles:`, `Skills:`, `Locations:`, `Experience:`, and `Deal breakers:` lines so it can be edited later without touching source code.

When `--db-path` is supplied, `scheduled-run` stores the run, source metrics, recommendations, and quality gate in local SQLite. The gate and stdout expose only the DB filename plus a path hash, not the raw local path.

```sh
PYTHONPATH=src python3 -m recruit_crawler.cli scheduled-history \
  --db-path personal_info/recruit.sqlite3 \
  --json
```

## Codex `예약됨` onboarding

Use this repository as a local-first scheduled report service. Install dependencies once, keep user-owned private files outside git under `personal_info/`, and validate the scheduler-compatible CLI/service contract from the repository root. Actual Codex `예약됨`, cron, launchd, or other external scheduler registration is a later integration step and is not claimed as verified by this repo smoke test.

### Required local files

- `config/live_sources.sample.json` or a copied user config such as `personal_info/live_sources.json`.
- One or more user-owned context documents passed with repeated `--context-doc`; accepted suffixes are `.txt`, `.md`, `.pdf`, and `.docx`.
- A writable report directory such as `reports/scheduled/`.
- A writable quality-gate path such as `artifacts/scheduled/latest_quality_gate.json`.
- A local SQLite DB path such as `personal_info/recruit.sqlite3` for scheduled history and feedback.
- Optional persistent preferences at `personal_info/preferences.md`, generated with `context-doctor` when resume/portfolio documents do not contain explicit roles, locations, or experience bounds.

Do not put raw resumes, cookies, browser profiles, API tokens, private canaries, or session exports in committed files. Sample configs are templates only and do not contain private personal data.

### Scheduler-compatible command

Use this command from the repository root when wiring a future scheduler, replacing only the `personal_info/*` paths with local user-owned files:

```sh
PYTHONPATH=src python3 -m recruit_crawler.cli scheduled-run \
  --config personal_info/live_sources.json \
  --context-doc personal_info/resume.md \
  --context-doc personal_info/preferences.md \
  --run-date "$(date +%F)" \
  --output-dir reports/scheduled \
  --quality-gate-output artifacts/scheduled/latest_quality_gate.json \
  --db-path personal_info/recruit.sqlite3
```

Expected outputs:

- `reports/scheduled/recruiting-scheduled-run-YYYY-MM-DD.md` — Korean recommendation report.
- `artifacts/scheduled/latest_quality_gate.json` — pass/fail gate with source health, context status, deterministic run identity, DB path hash, and report status.
- `personal_info/recruit.sqlite3` — local run history, source attempts, recommendations, quality gates, and feedback events.

### Daily feedback commands

After reviewing a report, export recommendation IDs from `scheduled-history --json` and record local feedback:

```sh
PYTHONPATH=src python3 -m recruit_crawler.cli scheduled-history \
  --db-path personal_info/recruit.sqlite3 \
  --json
PYTHONPATH=src python3 -m recruit_crawler.cli feedback-add \
  --db-path personal_info/recruit.sqlite3 \
  --recommendation-id RECOMMENDATION_ID \
  --verdict interesting \
  --reason "worth reviewing tomorrow" \
  --movement up
PYTHONPATH=src python3 -m recruit_crawler.cli feedback-export \
  --db-path personal_info/recruit.sqlite3 \
  --json
```

The `scheduled-history --json` payload includes both `runs` and `recommendations`; use a recommendation object's `recommendation_id` for `feedback-add`.

Allowed feedback verdicts are `applied`, `ignored`, `hidden`, `false_positive`, `false_negative`, `interesting`, and `not_relevant`. Feedback reasons are local, but private canary markers are rejected before persistence.

### Setup checks

Install dependencies and run these checks before registering or after updating the repo:

```sh
python3 -m pip install -e .
PYTHONPATH=src python3 -m recruit_crawler.cli status-report --check
PYTHONPATH=src python3 -m recruit_crawler.cli source-status --config config/live_sources.sample.json --json
PYTHONPATH=src python3 -m recruit_crawler.cli dry-run --config config/sample_config.json --run-date 2026-07-01 --print-report
PYTHONPATH=src python3 -m recruit_crawler.cli live-run --config config/live_sources.sample.json --run-date "$(date +%F)" --quality-gate-output artifacts/scheduled/live_quality_gate.json
PYTHONPATH=src python3 -m recruit_crawler.cli scheduled-run --config config/sample_config.json --run-date 2026-07-01 --output-dir reports/scheduled --quality-gate-output artifacts/scheduled/setup_quality_gate.json --db-path personal_info/recruit.sqlite3
PYTHONPATH=src python3 -m unittest discover -s tests
```

### Troubleshooting and update flow

- `context_status: needs_context`: run `context-doctor` to generate or update `personal_info/preferences.md`, then pass that file to `scheduled-run`; scheduled mode will not prompt.
- `source_policy` failure: disable non-target, manual, API, OCR, authenticated, partner-payload, or user-operated sources for scheduled mode.
- Zero-candidate/parser drift failure: inspect the source row in the quality gate and keep the report on hold until the adapter fixture/parser is updated.
- Privacy failure exit code `3`: remove private canary/session/token text from context or feedback reason inputs.
- Updating: pull the repo, review config changes, keep `personal_info/` untouched, run the setup checks above, then let the next scheduled run reuse the deterministic DB/report paths.

Run tests:

```sh
PYTHONPATH=src python3 -m unittest discover -s tests
```

## Current source status

- Enabled `public_http`: JobKorea, Saramin, Wanted, Jumpit, Rallit
- Enabled `browser_automation`: RocketPunch
- Excluded/non-target: LinkedIn, Company careers

## Project docs

- `AGENTS.md` — agent harness and repository rules
- `docs/status.md` — current feature/source/gap status board
- `TODO.md` — active backlog only
- `docs/index.md` — documentation index
- `docs/decisions.md` — durable product and operating decisions

## Scope guard

- `dry-run` does not use the network.
- `personal_info/` is ignored and is not read by the dry-run pipeline.
- Reports are written under ignored `reports/`.
- Personal context documents are read only when passed with `--context-doc`; parser fixtures under `fixtures/user_context/` are not product defaults.
- Raw JD bodies and browser profile artifacts should not be committed.
