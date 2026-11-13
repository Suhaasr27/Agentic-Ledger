"""Connection helper and a tiny forward-only migration runner."""
from __future__ import annotations

from pathlib import Path

import psycopg

SQL_DIR = Path(__file__).resolve().parents[1] / "sql"


def connect(dsn: str, schema: str | None = None) -> psycopg.Connection:
    """Open a connection. ``schema`` pins ``search_path`` (used to isolate test runs)."""
    options = f"-c search_path={schema}" if schema else None
    return psycopg.connect(dsn, options=options)


def migrate(conn: psycopg.Connection) -> list[str]:
    """Apply sql/*.sql in order, each exactly once. Returns the names applied this run."""
    applied: list[str] = []
    with conn.transaction():
        conn.execute("SELECT pg_advisory_xact_lock(hashtext('ledger-migrate'))")
        conn.execute(
            "CREATE TABLE IF NOT EXISTS schema_migrations "
            "(name text PRIMARY KEY, applied_at timestamptz NOT NULL DEFAULT now())"
        )
        done = {r[0] for r in conn.execute("SELECT name FROM schema_migrations")}
        for path in sorted(SQL_DIR.glob("*.sql")):
            if path.name in done:
                continue
            conn.execute(path.read_text(encoding="utf-8"))
            conn.execute("INSERT INTO schema_migrations (name) VALUES (%s)", (path.name,))
            applied.append(path.name)
    return applied
