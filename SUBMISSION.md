# SUBMISSION

## What I built

A pipeline that ingests lead CSVs and call-event JSONL into PostgreSQL, keeps an append-only interaction ledger,
and derives the five contract reports in SQL. A thin FastAPI service (two commands, read-only queries, no CRUD)
and a Next.js dashboard sit on top. Run instructions are in `README.md`.

**Verified:** 111 tests pass (Python 3.13 / PostgreSQL 16 in my environment; the code targets 3.11+ and uses nothing newer,
but I did not run it on 3.11 itself). The reports match an independent in-memory oracle on four fresh seeds (5, 7, 99, 2026),
and an incremental run is byte-identical to a one-shot run.

## Core design in five decisions

1. **The ledger stores facts; everything else is derived.** `events` is insert-only (a trigger rejects UPDATE, DELETE
   and TRUNCATE). Whether an event is linked or an orphan, which call it belongs to, what the effective disposition is,
   which bucket a lead is in: none of this is stored. It is computed by SQL views at read time. This is the reason
   "drop_1 then drop_2" equals "everything at once": there is no resolve-on-ingest step whose order could matter, and an
   orphan from drop_1 links itself the moment its lead arrives in drop_2.
2. **Idempotency by content hash, enforced by the database.** Each file's sha256 is claimed in `ingested_files`
   (`UNIQUE (kind, workflow_id, sha256)`) inside the same transaction that writes its rows. A re-run hits the unique key and
   writes nothing; a crash mid-file rolls the whole file back. Duplicate/rejected/warning counts are derived from immutable
   per-file counters plus the final table sizes (for example `duplicates = valid rows - leads`), so they are exact however
   the drops were split and need no mutable counters.
3. **PII: pseudonymise to match, encrypt to keep.** The phone is stored as `HMAC-SHA256(key, "+91XXXXXXXXXX")`
   (`phone_hash`, 32 bytes, unique with workflow). Events carry only that hash, and the raw phone is hashed inside the parser,
   so plaintext never reaches the database or logs. A keyed HMAC is used instead of a plain hash because there are only ~4 billion
   Indian mobile numbers, which would make an unkeyed SHA-256 trivially reversible. Names and the phone itself are
   encrypted with Fernet under a second key, so the app can still dial or display them; a database dump alone reveals nothing.
   Reports show `+91******1234` only (last four digits are stored in a separate column). A test dumps every table to JSON and
   asserts no name or phone fragment appears.
4. **Lead merge in the database, not in application memory.** The first valid row creates a lead and fixes its `batch_id` forever.
   Later rows overwrite only non-null fields. Non-PII fields live in a `jsonb` column whose null values are simply absent, so
   `attrs = leads.attrs || EXCLUDED.attrs` is exactly "last non-null write wins". PII fields are encrypted per field in a
   second jsonb map merged the same way. Defaults (`preferred_language -> en`) are applied after merging by the
   `v_lead_profile` view.
5. **All report aggregation is in SQL** (`002_views.sql`); Python only orders and formats. Sorting uses `COLLATE "C"` so output
   is byte-ordered and does not depend on the database locale.

## Schema (see `backend/sql/001_schema.sql`)

| Table | Role |
|---|---|
| `workflows`, `dispositions` | the schema JSON and `dispositions.csv`, mirrored so views can join on them |
| `ingested_files` | immutable manifest: sha256 idempotency key, drop/file/batch, per-file counters; CHECK constraints tie counters together |
| `leads` | current state per `(workflow_id, phone_hash)`; `attrs`, `pii` jsonb; `batch_id` of first valid row |
| `lead_rejections` | `(file_id, row_number, reason)`; the reason is a code, never row content, so no PII |
| `events` | the append-only ledger; `event_id` PK; **no FK on workflow_id or lead** so unlinkable events can be stored and linked later |

## Ambiguities and the calls I made

| # | Situation | Decision |
|---|---|---|
| 1 | Event with a bad structure beyond the two cases listed (no `event_type`, unparseable `occurred_at`) | Counted as **malformed** and skipped. A timestamp-less event cannot be ordered or dated, so it cannot enter the ledger. The generator never produces these. |
| 2 | Is an override beaten by a *later-stamped* set? | No. Any valid override beats any set; among overrides the latest by `occurred_at` wins. Reading of "latest override ... if no override, latest set". |
| 3 | Override with an unknown code | Ignored and counted as unknown; the call falls back to its `set` disposition. |
| 4 | Ties on `occurred_at` | Broken by `event_id`, never by ingest order, so results cannot depend on how drops were split. Ties on `initiated_at` between calls break on `call_id`. |
| 5 | Event that is both an orphan and an unknown disposition | Counted only as an orphan (the spec's processing order puts the orphan check first). |
| 6 | Events of a call with no `call.initiated` | Linked (so not orphans) but the call is not counted anywhere, per the spec. |
| 7 | `rejections_by_reason` | Lists only reasons that occurred, sorted by key. |
| 8 | A batch whose rows were all rejected or all duplicates | No row in `batch_summary`, which lists batches that own at least one lead. |
| 9 | Header clashes (two columns map to one field) | First column wins. Header normalisation is exactly as written: lower-case, trim, collapse. |
| 10 | Date strings | Day/month may be 1 or 2 digits (`1/2/2026`), year must be 4. `12/13/2026` is invalid because dates are day-first. |
| 11 | Decimal rounding | Half-up on the decimal string (not float). The generator's data never needs it. |
| 12 | Same file bytes uploaded again under another name, or an edited file | Identical bytes = already ingested (skipped). Changed bytes = a new file that is ingested, which is the right behaviour for a corrected upload, but its rows would count as merged duplicates. |
| 13 | Two leads with the same `external_ref` | Allowed; identity is the phone. Ties in `lead_report` sort by batch then creation order. |
| 14 | `last_call_at` precision | Truncated to whole seconds in IST. |
| 15 | Override that unexpectedly carries a phone | Linked through its `call_id` only, as the spec says. |
| 16 | Ingest order | Drops are processed in the order ingested, files in filename order, as specified. Ingesting drop_2 before drop_1 would change merge winners, by design of the spec. |

## Performance (stretch)

`--scale large` (132,903 leads, ~633k event lines), on this sandbox, single process, Postgres on the same machine:

| Step | Time |
|---|---|
| ingest drop_1 (66 files) | 15.5 s |
| ingest drop_2 (67 files) | 13.9 s |
| report (all five files, 132,903-row lead report) | 33.8 s |
| **total** | **~63 s** |

Ingest uses `COPY` into a temp table and one set-based `INSERT ... ON CONFLICT` per file. The report is the slower part
because each view re-derives `v_event_lead`; the next optimisation is to materialise `v_calls` and `v_call_disposition`
into temp tables once per report run. I verified correctness against the oracle on small seeds only, not on the large set.

## Truly incremental processing: trade-off

Ingest already never re-processes a file. What it does not do is cache report results: every report recomputes from the
ledger. That is deliberate. Caching aggregates would mean invalidating them when a late lead arrives (an old orphan call
suddenly counting toward a past date), which is exactly the class of bug the spec is warning about. At 100k leads/day I would
keep the derived-at-read design and add a materialised `v_calls`, refreshed after each ingest, before reaching for
incremental aggregates.

## Production at 100k leads/day (short note)

- **Scheduling:** an orchestrator (Airflow, Dagster or a plain cron plus a lock) runs `ingest <drop>` when a drop lands and
  `report` after it; both are safe to retry. The advisory lock already serialises concurrent ingests.
- **Failures:** per-file transactions mean a failed run leaves no partial file; re-run and finished files are skipped.
  Alert on rejected-row rate, malformed events, and orphan count rising rather than falling (a lead upload may be missing).
- **Backfills:** re-run old drops in their original order; the sha256 manifest makes this free for already-seen files. A
  schema or disposition-catalogue change needs no backfill at all because reports are derived.
- **PII ops:** keys in a secrets manager; rotating the Fernet key is a re-encrypt of `leads.pii`; rotating the HMAC key means
  re-hashing leads and events, so plan it as a migration. Revoke UPDATE/DELETE on `events` from the app role as a second
  layer behind the trigger. Partition `events` by month once it passes a few hundred million rows.

## What I would do next

1. Materialise calls per report run (target: report under 10 s on the large set) and run the oracle on the large dataset.
2. Add an `events_quarantine` audit view with the reason per orphan (unknown phone vs invalid phone vs unknown call), for
   operations teams.
3. A CI job that runs the generator on a new random seed each build and diffs against the oracle.
4. Authentication on the API (it currently assumes a trusted network) and an allow-list for the drop directory.
