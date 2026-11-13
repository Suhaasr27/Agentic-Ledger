# AI_USAGE

## Tools

Claude (Anthropic) wrote the code, tests and documentation in this repository in a single working session, running
commands in a sandbox with a local PostgreSQL 16. I (the submitter) supplied the assignment, the data generator, the
schemas and the tech stack, and directed the work.

> Edit this section to reflect your own involvement before you submit: what you reviewed, changed, or re-ran yourself.

## What the AI did

- Read the assignment, the glossary, both lead schemas, `dispositions.csv` and the generator before designing anything.
- Designed the schema and the "derive at read time" architecture, and wrote the pure rule modules, SQL migrations/views,
  ingest, reporting, CLI, FastAPI service and Next.js dashboard.
- Wrote the tests, including an independent oracle (a second, in-memory implementation of the spec) used to diff the SQL
  read model on seeds the pipeline was never tuned on.

## How the output was checked (not just trusted)

- Ran the generator, ingested both drops, and confirmed lead `MOT/2026/100000` produces the exact `last_call_at` example in the spec.
- 111 automated tests pass; the oracle comparison runs on four fresh seeds; an incremental run is compared byte-for-byte
  with a one-shot run; re-ingest is asserted to change no row, including `updated_at`.
- Timed the `--scale large` dataset end to end (about 63 s).
- A smoke test of the live API and the Next.js proxy, including a path-traversal attempt.
- One test expectation was wrong on first run (a route that does not exist returns 404, not 405). The test was corrected, not the code.

## Known limits of that verification

- The oracle reuses the leaf value rules (phone/date/decimal parsing and header mapping) from the pipeline, so a mistake in a
  rule would appear in both. Those rules have their own direct tests, written from the spec text.
- Not verified on Python 3.11 specifically (3.13 was available), nor with docker compose (a local PostgreSQL 16 was used instead).
- The oracle was not run on the large dataset.
- Where the spec was ambiguous the AI chose a reading; every such choice is listed in `SUBMISSION.md` so a reviewer can overrule it.
