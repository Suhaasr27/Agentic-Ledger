from __future__ import annotations

import json
import os
import uuid
from pathlib import Path

import psycopg
import pytest
from cryptography.fernet import Fernet

from ledger import db
from ledger.config import REPO_ROOT
from ledger.ingest import Ingestor
from ledger.pii import PiiProtector
from ledger.schema import Catalog

TEST_DSN = os.environ.get("TEST_DATABASE_URL") or os.environ.get("DATABASE_URL")


@pytest.fixture(scope="session")
def catalog() -> Catalog:
    return Catalog.load(REPO_ROOT / "schemas")


@pytest.fixture(scope="session")
def pii() -> PiiProtector:
    return PiiProtector(b"test-hmac-key-0123456789abcdef", Fernet.generate_key())


@pytest.fixture
def conn():
    """A connection pinned to a throwaway schema, fully migrated. Skips if no Postgres is reachable."""
    if not TEST_DSN:
        pytest.skip("set TEST_DATABASE_URL (or DATABASE_URL) to run database tests")
    schema = f"t_{uuid.uuid4().hex[:12]}"
    try:
        admin = psycopg.connect(TEST_DSN, autocommit=True)
    except psycopg.OperationalError as exc:
        pytest.skip(f"postgres not reachable: {exc}")
    admin.execute(f"CREATE SCHEMA {schema}")
    c = db.connect(TEST_DSN, schema=schema)
    db.migrate(c)
    yield c
    c.close()
    admin.execute(f"DROP SCHEMA {schema} CASCADE")
    admin.close()


@pytest.fixture
def make_world(catalog, pii, tmp_path):
    """Factory: ``world = make_world(conn)``; ``world.drop('drop_1', leads=..., events=...)``."""
    def factory(connection):
        return World(connection, catalog, pii, tmp_path / uuid.uuid4().hex[:6])
    return factory


class World:
    """Writes tiny drops to disk and ingests them through the real pipeline."""

    def __init__(self, conn, catalog, pii, root: Path) -> None:
        self.conn, self.catalog, self.pii, self.root = conn, catalog, pii, root

    def drop(self, name: str, leads: dict | None = None, events: dict | list | None = None):
        """leads: {(workflow, batch): [row dicts]}; events: list of dicts/raw lines, or {file_name: [...]}."""
        base = self.root / name
        for (wf, batch), rows in (leads or {}).items():
            path = base / "leads" / wf / f"{batch}.csv"
            path.parent.mkdir(parents=True, exist_ok=True)
            header = list(rows[0].keys())
            lines = [",".join(header)] + [",".join(str(r.get(h, "")) for h in header) for r in rows]
            path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        if events:
            files = events if isinstance(events, dict) else {"events_0001.jsonl": events}
            for fname, evs in files.items():
                path = base / "events" / fname
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text("\n".join(e if isinstance(e, str) else json.dumps(e) for e in evs) + "\n",
                                encoding="utf-8")
        base.mkdir(parents=True, exist_ok=True)
        return Ingestor(self.conn, self.catalog, self.pii).ingest_drop(base)


def lead(name="Asha Rao", phone="9876543210", ref="POL-1", **extra):
    return {"customer_name": name, "phone": phone, "policy_number": ref, **extra}


_seq = iter(range(1, 10**9))


def ev(event_type, call="call_1", phone="9876543210", at="2026-10-05T10:00:00+05:30",
       wf="motor_renewal", event_id=None, **payload):
    e = {"event_id": event_id or f"evt_{next(_seq):08d}", "event_type": event_type, "workflow_id": wf,
         "call_id": call, "occurred_at": at, "payload": payload}
    if phone is not None:
        e["phone"] = phone
    return e
