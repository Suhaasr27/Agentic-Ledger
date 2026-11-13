# Interaction Ledger & Reporting Pipeline

Ingests messy lead CSVs and call-event JSONL into PostgreSQL, keeps an **append-only interaction ledger**,
and produces the five client reports exactly as specified. Python 3.11+, FastAPI, Next.js/React, PostgreSQL.

```
 lead CSVs ──► parse + validate (pure) ──┐                       ┌─► v_batch_summary ─┐
                                         ├─► Postgres ──► SQL ───┤   v_daily_overview ├─► 5 report files
 event JSONL ─► parse (pure) ────────────┘   views                │   v_lead_report    │   FastAPI ─► Next.js
   (phones HMAC-hashed before they touch the DB)                    └─► v_data_quality ──┘
```

## Run it, step by step

**Prerequisites:** Python 3.11+, Node 20+, Docker (or any PostgreSQL 14+).

```bash
# 1. Start Postgres
docker compose up -d --wait db

# 2. Install
cd backend && pip install -e ".[dev]" && cd ..
cd frontend && npm install && cd ..

# 3. Create PII keys (two secrets, never committed) and save them in .env
cp .env.example .env
(cd backend && python -m ledger keygen)      # paste the two printed values into .env
set -a; . ./.env; set +a                     # load them into this shell

# 4. Generate data (not committed; a different seed works identically)
python tools/generate_data.py --seed 42 --scale small --out data
#   optional stretch dataset:  python tools/generate_data.py --seed 42 --scale large --out data_large

# 5. Ingest, drop by drop, then report
cd backend
python -m ledger ingest ../data/drop_1
python -m ledger ingest ../data/drop_2
python -m ledger ingest ../data/drop_2       # re-run: every file reported "skipped", nothing changes
python -m ledger report --out ../reports     # 5 files
```

The first `ingest` creates the tables (migrations run automatically). The repo ships with
`assignment-a-data/schemas/` copied to `schemas/`; point `LEDGER_SCHEMAS_DIR` elsewhere if you keep them there.

`make up install data ingest report` does the same thing once `.env` exists.

### Tests

```bash
export TEST_DATABASE_URL=postgresql://postgres:postgres@localhost:5432/ledger
cd backend && python -m pytest -q          # DB tests run in throwaway schemas; they skip if no Postgres
```

| File | What it proves |
|---|---|
| `test_normalize.py` | phone (all six formats + edge cases), day-first dates, decimals, headers, timestamps, IST boundary |
| `test_leads_events.py` | rejection order, warnings only on kept rows, blank rows & row numbers, aliases/reordered columns, in-file merge, malformed events |
| `test_pipeline.py` | effective disposition (override > set, latest wins, unknown ignored), **bucket logic**, daily rollups, orphan re-linking, dedupe across drops, **idempotent re-ingest**, **incremental = all-at-once**, append-only trigger, **no plaintext PII in any table** |
| `test_oracle.py` | an independent in-memory implementation of the spec, diffed against the pipeline on **fresh seeds** (7, 2026, 99, 5) |
| `test_api.py` | commands are idempotent over HTTP, no write/delete routes exist, path traversal blocked |

### API and dashboard

```bash
cd backend && python -m ledger serve        # http://127.0.0.1:8000  (docs at /docs)
cd frontend && npm run dev                  # http://localhost:3000  (proxies /api/* to the API)
```

The API is deliberately **not CRUD**. There are two commands and a set of read-only queries:

| Kind | Endpoint | Purpose |
|---|---|---|
| command | `POST /drops/{name}/ingest` | ingest a drop (idempotent) |
| command | `POST /reports/publish` | write the five contract files |
| query | `GET /drops` | drops on disk, and how many files are still pending |
| query | `GET /reports/{batch-summary,daily-overview,data-quality,rejections}` | derived read models |
| query | `GET /reports/leads?workflow_id=&bucket=&limit=&offset=` | paged lead report (masked phones) |

There is no route that creates, edits or deletes a lead, event or report row.

## Layout

```
backend/ledger/   normalize.py   pure value rules (phone, date, decimal, header, timestamp)
                  schema.py      workflow schemas + disposition catalogue
                  leads.py       CSV -> validated, merged leads         (pure)
                  events.py      JSON line -> event                      (pure)
                  pii.py         HMAC pseudonyms + Fernet encryption
                  ingest.py      transactional, sha256-idempotent writes
                  reporting.py   ordering/formatting of the 5 files
                  api.py cli.py config.py db.py
backend/sql/      001_schema.sql (tables, constraints, append-only trigger)  002_views.sql (all derivations)
frontend/         Next.js dashboard
schemas/          workflow schemas + dispositions.csv        tools/  data generator
SUBMISSION.md     decisions, ambiguities, trade-offs         AI_USAGE.md
```
