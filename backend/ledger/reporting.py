"""Report generation: the five contract files, plus read-model queries shared with the API.

All aggregation happens in SQL (see sql/002_views.sql). This module only orders and formats.
Sorting uses ``COLLATE "C"`` so order is byte-wise (Python ``sorted`` order), never locale-dependent.
"""
from __future__ import annotations

import csv
import json
import os
import tempfile
from pathlib import Path
from typing import Any

import psycopg
from psycopg.rows import dict_row

BATCH_SUMMARY_COLS = ["workflow_id", "batch_id", "total_leads", "yet_to_reach_out",
                      "in_progress_telephony_failed", "in_progress_dialed", "completed"]
DAILY_COLS = ["workflow_id", "date", "total_calls", "connected_calls", "connectivity_rate", "success_leads"]
LEAD_COLS = ["workflow_id", "external_ref", "batch_id", "phone_masked", "bucket", "total_calls",
             "connected_calls", "last_call_at", "current_disposition", "ever_success"]
REJECTION_COLS = ["workflow_id", "batch_id", "row_number", "reason"]

DATA_QUALITY_KEYS = ["lead_rows_total", "lead_rows_rejected", "rejections_by_reason",
                     "duplicate_lead_rows_merged", "field_warnings", "events_total", "malformed_events",
                     "duplicate_events_dropped", "orphan_events", "unknown_disposition_events"]


def _rows(conn: psycopg.Connection, sql: str, params: tuple = ()) -> list[dict[str, Any]]:
    with conn.cursor(row_factory=dict_row) as cur:
        return cur.execute(sql, params).fetchall()


def batch_summary(conn: psycopg.Connection) -> list[dict[str, Any]]:
    return _rows(conn, f"""
        SELECT {", ".join(BATCH_SUMMARY_COLS)} FROM v_batch_summary
        ORDER BY workflow_id COLLATE "C", batch_id COLLATE "C"
    """)


def daily_overview(conn: psycopg.Connection) -> list[dict[str, Any]]:
    rows = _rows(conn, """
        SELECT workflow_id, call_date::text AS date, total_calls, connected_calls, success_leads
        FROM v_daily_overview ORDER BY workflow_id COLLATE "C", call_date
    """)
    for r in rows:
        r["connectivity_rate"] = f"{r['connected_calls'] / r['total_calls']:.4f}"
    return rows


def lead_report(conn: psycopg.Connection, *, workflow_id: str | None = None, bucket: str | None = None,
                limit: int | None = None, offset: int = 0) -> list[dict[str, Any]]:
    where, params = [], []
    if workflow_id:
        where.append("workflow_id = %s"); params.append(workflow_id)
    if bucket:
        where.append("bucket = %s"); params.append(bucket)
    sql = f"SELECT {', '.join(LEAD_COLS)} FROM v_lead_report"
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += ' ORDER BY workflow_id COLLATE "C", external_ref COLLATE "C", batch_id COLLATE "C", lead_id'
    if limit is not None:
        sql += " LIMIT %s OFFSET %s"; params += [limit, offset]
    return _rows(conn, sql, tuple(params))


def rejections(conn: psycopg.Connection) -> list[dict[str, Any]]:
    return _rows(conn, f"""
        SELECT {", ".join(REJECTION_COLS)} FROM v_rejections
        ORDER BY workflow_id COLLATE "C", batch_id COLLATE "C", row_number, file_id
    """)


def data_quality(conn: psycopg.Connection) -> dict[str, Any]:
    row = _rows(conn, "SELECT * FROM v_data_quality")[0]
    by_reason = {r["reason"]: r["n"] for r in
                 _rows(conn, 'SELECT reason, n FROM v_rejections_by_reason ORDER BY reason COLLATE "C"')}
    out: dict[str, Any] = {}
    for key in DATA_QUALITY_KEYS:
        out[key] = by_reason if key == "rejections_by_reason" else int(row[key])
    return out


# --------------------------------------------------------------------------- files
def _cell(value: Any) -> Any:
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    return value


def _write_atomic(path: Path, writer) -> None:
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.")
    try:
        with os.fdopen(fd, "w", newline="", encoding="utf-8") as fh:
            writer(fh)
        os.replace(tmp, path)
    except BaseException:
        os.unlink(tmp)
        raise


def _write_csv(path: Path, columns: list[str], rows: list[dict[str, Any]]) -> None:
    def writer(fh):
        out = csv.writer(fh, lineterminator="\n")
        out.writerow(columns)
        for row in rows:
            out.writerow([_cell(row[c]) for c in columns])
    _write_atomic(path, writer)


def write_reports(conn: psycopg.Connection, out_dir: Path) -> dict[str, int]:
    """Write the five contract files. Runs in one REPEATABLE READ snapshot so they are mutually consistent."""
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    conn.commit()  # make sure SET TRANSACTION below is the first statement of a fresh transaction
    with conn.transaction():
        conn.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY")
        batches, daily = batch_summary(conn), daily_overview(conn)
        leads, rejected, quality = lead_report(conn), rejections(conn), data_quality(conn)
    _write_csv(out_dir / "batch_summary.csv", BATCH_SUMMARY_COLS, batches)
    _write_csv(out_dir / "daily_overview.csv", DAILY_COLS, daily)
    _write_csv(out_dir / "lead_report.csv", LEAD_COLS, leads)
    _write_csv(out_dir / "rejections.csv", REJECTION_COLS, rejected)
    _write_atomic(out_dir / "data_quality.json",
                  lambda fh: (json.dump(quality, fh, indent=2, ensure_ascii=False), fh.write("\n")))
    return {"batch_summary": len(batches), "daily_overview": len(daily), "lead_report": len(leads),
            "rejections": len(rejected)}
