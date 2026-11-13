"""FastAPI service. Deliberately NOT a CRUD API: there is no way to create, edit or delete a lead,
event or report row over HTTP. The surface is two *commands* (ingest a drop, publish reports)
and read-only *queries* over the derived read models.
"""
from __future__ import annotations

from contextlib import contextmanager
from dataclasses import asdict
from pathlib import Path
from typing import Iterator, Literal

from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware

from . import db, reporting
from .config import Settings
from .ingest import Ingestor, sha256_file, validate_drop_name
from .pii import PiiProtector
from .schema import Catalog

Bucket = Literal["yet_to_reach_out", "in_progress_telephony_failed", "in_progress_dialed", "completed"]


def create_app(settings: Settings | None = None) -> FastAPI:
    cfg = settings or Settings.from_env()
    catalog = Catalog.load(cfg.schemas_dir)
    pii = PiiProtector(cfg.hmac_key, cfg.fernet_key)

    app = FastAPI(title="Interaction Ledger", version="0.1.0")
    app.add_middleware(CORSMiddleware, allow_origins=["http://localhost:3000"], allow_methods=["GET", "POST"],
                       allow_headers=["*"])

    @contextmanager
    def connection() -> Iterator:
        conn = db.connect(cfg.database_url, schema=cfg.db_schema)
        try:
            db.migrate(conn)
            yield conn
        finally:
            conn.close()

    def drop_path(name: str) -> Path:
        try:
            validate_drop_name(name)
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc
        path = (cfg.data_root / name).resolve()
        if cfg.data_root.resolve() not in path.parents or not path.is_dir():
            raise HTTPException(404, f"drop {name!r} not found")
        return path

    # ------------------------------------------------------------------ queries
    @app.get("/health")
    def health() -> dict:
        with connection() as conn:
            conn.execute("SELECT 1")
        return {"status": "ok"}

    @app.get("/drops")
    def drops() -> list[dict]:
        """Drops on disk and how many of their files the ledger has already absorbed."""
        out = []
        with connection() as conn:
            known = {(r[0], r[1]) for r in conn.execute("SELECT kind, sha256 FROM ingested_files")}
            for d in sorted(p for p in cfg.data_root.glob("*") if p.is_dir()) if cfg.data_root.is_dir() else []:
                files = [*sorted(d.glob("leads/*/*.csv")), *sorted(d.glob("events/*.jsonl"))]
                pending = sum((("leads" if f.suffix == ".csv" else "events"), sha256_file(f)) not in known
                              for f in files)
                out.append({"drop": d.name, "files": len(files), "pending": pending})
        return out

    @app.get("/reports/data-quality")
    def get_data_quality() -> dict:
        with connection() as conn:
            return reporting.data_quality(conn)

    @app.get("/reports/batch-summary")
    def get_batch_summary() -> list[dict]:
        with connection() as conn:
            return reporting.batch_summary(conn)

    @app.get("/reports/daily-overview")
    def get_daily_overview() -> list[dict]:
        with connection() as conn:
            return reporting.daily_overview(conn)

    @app.get("/reports/rejections")
    def get_rejections() -> list[dict]:
        with connection() as conn:
            return reporting.rejections(conn)

    @app.get("/reports/leads")
    def get_leads(workflow_id: str | None = None, bucket: Bucket | None = None,
                  limit: int = Query(50, ge=1, le=500), offset: int = Query(0, ge=0)) -> dict:
        with connection() as conn:
            rows = reporting.lead_report(conn, workflow_id=workflow_id, bucket=bucket, limit=limit, offset=offset)
            total = conn.execute(
                "SELECT count(*) FROM v_lead_report WHERE (%s::text IS NULL OR workflow_id = %s) "
                "AND (%s::text IS NULL OR bucket = %s)", (workflow_id, workflow_id, bucket, bucket)).fetchone()[0]
        return {"total": total, "limit": limit, "offset": offset, "rows": rows}

    # ------------------------------------------------------------------ commands
    @app.post("/drops/{name}/ingest")
    def ingest_drop(name: str) -> dict:
        """Ingest a drop. Idempotent: files already ingested are reported as skipped."""
        path = drop_path(name)
        with connection() as conn:
            result = Ingestor(conn, catalog, pii).ingest_drop(path)
        return {"drop": result.drop, "ingested": result.ingested, "skipped": result.skipped,
                "warnings": result.warnings, "files": [asdict(f) for f in result.files]}

    @app.post("/reports/publish")
    def publish_reports() -> dict:
        """Write the five contract files to the configured reports directory."""
        with connection() as conn:
            counts = reporting.write_reports(conn, cfg.reports_dir)
        return {"directory": str(cfg.reports_dir), "rows": counts}

    return app
