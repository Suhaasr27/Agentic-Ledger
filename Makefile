# Usage: cp .env.example .env, fill the keys, then `make up install data ingest report`.
SHELL := /bin/bash
ENV   := set -a; [ -f .env ] && . ./.env; set +a;

up:        ## start Postgres
	docker compose up -d --wait db
install:   ## backend + frontend dependencies
	cd backend && pip install -e ".[dev]"
	cd frontend && npm install
keys:      ## print fresh keys for .env
	cd backend && python -m ledger keygen
data:      ## generate the small seed-42 dataset (not committed)
	python tools/generate_data.py --seed 42 --scale small --out data
ingest:    ## ingest both drops, in order (safe to re-run)
	$(ENV) cd backend && python -m ledger ingest ../data/drop_1 && python -m ledger ingest ../data/drop_2
report:    ## write the five report files to ./reports
	$(ENV) cd backend && python -m ledger report --out ../reports
test:      ## unit + database + oracle tests (needs Postgres: TEST_DATABASE_URL or DATABASE_URL)
	$(ENV) cd backend && python -m pytest -q
api:       ## FastAPI on :8000
	$(ENV) cd backend && python -m ledger serve
web:       ## Next.js dashboard on :3000
	cd frontend && npm run dev
