<div align="center">

# Recruit Crawler

<strong>Local recruiting-source crawler and ranking pipeline.</strong><br />
Collect public job postings, parse structured JD fields, score them against a local user context, and write Korean Markdown reports.

</div>

<hr />

## What is this?

Recruit Crawler is a local Python CLI for reviewing recruiting postings with less manual JD reading.

It focuses on:

- public or no-human source collection paths;
- structured JD parsing with fixture-backed regression tests;
- local scoring and Korean Markdown reports;
- explicit source registry status for enabled, deferred, blocked, and excluded sources.

Sensitive personal inputs and generated reports stay local and are git-ignored.

## Quick start

Run the no-network fixture pipeline:

```sh
PYTHONPATH=src python3 -m recruit_crawler.cli dry-run \
  --config config/sample_config.json \
  --run-date 2026-07-01 \
  --print-report
```

Run with a personal context document instead of the sample profile:

```sh
PYTHONPATH=src python3 -m recruit_crawler.cli dry-run \
  --config config/sample_config.json \
  --context-doc path/to/resume.md \
  --run-date 2026-07-01 \
  --print-report
```

`--context-doc` accepts `.txt`, `.md`, `.pdf`, and `.docx` files. `config.profile` is a fallback/default for fixture runs; personalized runs should pass an explicit context document.

Check source registry status:

```sh
PYTHONPATH=src python3 -m recruit_crawler.cli source-status \
  --config config/live_sources.sample.json
```

Run tests:

```sh
PYTHONPATH=src python3 -m unittest discover -s tests
```

## Current source status

- Enabled `public_http`: JobKorea, Saramin, Wanted, Jumpit, Rallit
- Enabled `browser_automation`: RocketPunch
- Deferred: Company careers
- Excluded: LinkedIn

## Project docs

- `AGENTS.md` — agent harness and repository rules
- `TODO.md` — active backlog only
- `docs/index.md` — documentation index
- `docs/archive/` — completed work, state snapshots, and verification history

## Scope guard

- `dry-run` does not use the network.
- `personal_info/` is ignored and is not read by the dry-run pipeline.
- Reports are written under ignored `reports/`.
- Personal context documents are read only when passed with `--context-doc`; parser fixtures under `fixtures/user_context/` are not product defaults.
- Raw JD bodies and browser profile artifacts should not be committed.
