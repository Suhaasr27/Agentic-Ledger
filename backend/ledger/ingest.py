"""Ingestion: lead files -> ``leads`` (merge), event files -> ``events`` (append-only ledger).

Idempotency
    Every file is identified by the sha256 of its bytes. A file is parsed outside the
    transaction, then written inside ONE transaction that first claims its manifest row in
    ``ingested_files`` (unique on kind/workflow/sha256). A re-run hits the unique key, writes
    nothing, and reports the file as skipped. A crash mid-file rolls everything back.

Incrementality
    Nothing is "linked" or "resolved" at ingest time. Events are stored as they arrive and
    joined to leads at read time (002_views.sql), so late-arriving leads need no reprocessing.
"""
from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from pathlib import Path

import psycopg

from .events import ParsedEvent, parse_event_line
from .leads import LeadFileParse, parse_lead_file
from .pii import PiiProtector
from .schema import Catalog, LeadSchema

_DROP_NAME = re.compile(r"^[A-Za-z0-9_.-]+$")


@dataclass
class FileResult:
    kind: str  # leads | events
    workflow_id: str
    file: str
    status: str  # ingested | skipped
    records_total: int = 0
    records_rejected: int = 0
    records_accepted: int = 0


@dataclass
class DropResult:
    drop: str
    files: list[FileResult] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    @property
    def ingested(self) -> int:
        return sum(f.status == "ingested" for f in self.files)

    @property
    def skipped(self) -> int:
        return sum(f.status == "skipped" for f in self.files)


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def validate_drop_name(name: str) -> str:
    if not _DROP_NAME.fullmatch(name) or name in {".", ".."}:
        raise ValueError(f"invalid drop name {name!r}")
    return name


class Ingestor:
    def __init__(self, conn: psycopg.Connection, catalog: Catalog, pii: PiiProtector) -> None:
        self.conn = conn
        self.catalog = catalog
        self.pii = pii

    # ------------------------------------------------------------------ public
    def ingest_drop(self, drop_dir: Path) -> DropResult:
        drop_dir = Path(drop_dir)
        if not drop_dir.is_dir():
            raise FileNotFoundError(f"drop directory not found: {drop_dir}")
        result = DropResult(drop=drop_dir.name)
        self.sync_catalog()

        # Spec order: files in filename order. Leads first, then events (order is irrelevant to
        # the outcome, which is derived at read time, but leads-first keeps the manifest readable).
        leads_root = drop_dir / "leads"
        for wf_dir in sorted(p for p in leads_root.glob("*") if p.is_dir()):
            schema = self.catalog.schemas.get(wf_dir.name)
            if schema is None:
                result.warnings.append(f"skipped leads/{wf_dir.name}: no schema for this workflow")
                continue
            for path in sorted(wf_dir.glob("*.csv")):
                result.files.append(self._ingest_lead_file(drop_dir.name, schema, path))
        for path in sorted((drop_dir / "events").glob("*.jsonl")):
            result.files.append(self._ingest_event_file(drop_dir.name, path))
        return result

    def sync_catalog(self) -> None:
        """Mirror schemas + disposition catalogue into the DB (the views join on them)."""
        with self.conn.transaction():
            for wf, schema in self.catalog.schemas.items():
                self.conn.execute(
                    "INSERT INTO workflows (workflow_id, schema) VALUES (%s, %s) "
                    "ON CONFLICT (workflow_id) DO UPDATE SET schema = EXCLUDED.schema",
                    (wf, json.dumps(schema.raw)),
                )
            for d in self.catalog.dispositions:
                self.conn.execute(
                    "INSERT INTO dispositions (workflow_id, code, label, is_terminal, is_success) "
                    "VALUES (%s, %s, %s, %s, %s) ON CONFLICT (workflow_id, code) DO UPDATE SET "
                    "label = EXCLUDED.label, is_terminal = EXCLUDED.is_terminal, is_success = EXCLUDED.is_success",
                    (d.workflow_id, d.code, d.label, d.is_terminal, d.is_success),
                )

    # ------------------------------------------------------------------ leads
    def _ingest_lead_file(self, drop: str, schema: LeadSchema, path: Path) -> FileResult:
        sha = sha256_file(path)
        parsed = parse_lead_file(path, schema)  # pure; outside the transaction
        result = FileResult("leads", schema.workflow_id, f"{drop}/{path.name}", "skipped",
                            parsed.records_total, len(parsed.rejections), parsed.records_accepted)
        with self.conn.transaction():
            self._lock()
            file_id = self._claim_file(
                kind="leads", workflow_id=schema.workflow_id, batch_id=path.stem, drop=drop,
                name=path.name, sha=sha, total=parsed.records_total,
                rejected=len(parsed.rejections), accepted=parsed.records_accepted,
                warnings=parsed.field_warnings,
            )
            if file_id is None:
                return result
            self._write_rejections(file_id, parsed)
            self._upsert_leads(file_id, schema, path.stem, parsed)
        result.status = "ingested"
        return result

    def _write_rejections(self, file_id: int, parsed: LeadFileParse) -> None:
        with self.conn.cursor() as cur:
            cur.executemany(
                "INSERT INTO lead_rejections (file_id, row_number, reason) VALUES (%s, %s, %s)",
                [(file_id, r.row_number, r.reason) for r in parsed.rejections],
            )

    def _upsert_leads(self, file_id: int, schema: LeadSchema, batch_id: str, parsed: LeadFileParse) -> None:
        if not parsed.leads:
            return
        pii_names = {f.name for f in schema.fields if f.pii}
        ref_name = schema.reference_field.name
        phone_name = schema.phone_field.name
        rows = []
        for order, (phone, lead) in enumerate(parsed.leads.items()):
            attrs = {k: v for k, v in lead.fields.items() if k not in pii_names and k != ref_name}
            pii = {k: self.pii.encrypt(v) for k, v in lead.fields.items() if k in pii_names}
            rows.append((
                order, schema.workflow_id, self.pii.phone_hash(phone), self.pii.phone_last4(phone),
                lead.fields.get(ref_name), json.dumps(attrs, sort_keys=True), json.dumps(pii, sort_keys=True),
            ))
        assert all(phone_name in lead.fields for lead in parsed.leads.values())

        with self.conn.cursor() as cur:
            cur.execute(
                "CREATE TEMP TABLE _stage_leads (ord int, workflow_id text, phone_hash bytea, "
                "phone_last4 text, external_ref text, attrs jsonb, pii jsonb) ON COMMIT DROP"
            )
            with cur.copy("COPY _stage_leads FROM STDIN") as copy:
                for row in rows:
                    copy.write_row(row)
            # First valid row creates the lead (batch_id is set once and never touched again);
            # later rows overwrite only non-null fields: jsonb `||` replaces just the keys present.
            cur.execute(
                """
                INSERT INTO leads (workflow_id, phone_hash, phone_last4, batch_id, external_ref,
                                   attrs, pii, first_file_id)
                SELECT workflow_id, phone_hash, phone_last4, %s, external_ref, attrs, pii, %s
                FROM _stage_leads ORDER BY ord
                ON CONFLICT (workflow_id, phone_hash) DO UPDATE SET
                    external_ref = coalesce(EXCLUDED.external_ref, leads.external_ref),
                    attrs        = leads.attrs || EXCLUDED.attrs,
                    pii          = leads.pii   || EXCLUDED.pii,
                    updated_at   = now()
                """,
                (batch_id, file_id),
            )

    # ------------------------------------------------------------------ events
    def _ingest_event_file(self, drop: str, path: Path) -> FileResult:
        sha = sha256_file(path)
        events: list[ParsedEvent] = []
        total = malformed = 0
        with path.open(encoding="utf-8", errors="replace") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                total += 1
                event = parse_event_line(line, self.pii.phone_hash)
                if event is None:
                    malformed += 1
                else:
                    events.append(event)
        result = FileResult("events", "", f"{drop}/{path.name}", "skipped", total, malformed, len(events))
        with self.conn.transaction():
            self._lock()
            file_id = self._claim_file(
                kind="events", workflow_id="", batch_id=None, drop=drop, name=path.name, sha=sha,
                total=total, rejected=malformed, accepted=len(events), warnings=0,
            )
            if file_id is None:
                return result
            self._append_events(file_id, events)
        result.status = "ingested"
        return result

    def _append_events(self, file_id: int, events: list[ParsedEvent]) -> None:
        if not events:
            return
        with self.conn.cursor() as cur:
            cur.execute(
                "CREATE TEMP TABLE _stage_events (ord int, event_id text, workflow_id text, event_type text, "
                "call_id text, occurred_at timestamptz, phone_hash bytea, payload jsonb) ON COMMIT DROP"
            )
            with cur.copy("COPY _stage_events FROM STDIN") as copy:
                for order, e in enumerate(events):
                    copy.write_row((order, e.event_id, e.workflow_id, e.event_type, e.call_id,
                                    e.occurred_at.isoformat(), e.phone_hash, json.dumps(e.payload)))
            # First occurrence wins inside the file; ON CONFLICT drops ids already in the ledger
            # (earlier file or earlier drop). Surviving rows are appended in file order.
            cur.execute(
                """
                INSERT INTO events (event_id, workflow_id, event_type, call_id, occurred_at,
                                    phone_hash, payload, file_id)
                SELECT event_id, workflow_id, event_type, call_id, occurred_at, phone_hash, payload, %s
                FROM (SELECT DISTINCT ON (event_id) * FROM _stage_events ORDER BY event_id, ord) first_seen
                ORDER BY ord
                ON CONFLICT (event_id) DO NOTHING
                """,
                (file_id,),
            )

    # ------------------------------------------------------------------ shared
    def _lock(self) -> None:
        self.conn.execute("SELECT pg_advisory_xact_lock(hashtext('ledger-ingest'))")

    def _claim_file(self, *, kind: str, workflow_id: str, batch_id: str | None, drop: str, name: str,
                    sha: str, total: int, rejected: int, accepted: int, warnings: int) -> int | None:
        """Insert the manifest row; ``None`` means these exact bytes were already ingested."""
        row = self.conn.execute(
            """
            INSERT INTO ingested_files (kind, workflow_id, batch_id, drop_name, file_name, sha256,
                                        records_total, records_rejected, records_accepted, field_warnings)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (kind, workflow_id, sha256) DO NOTHING
            RETURNING file_id
            """,
            (kind, workflow_id, batch_id, drop, name, sha, total, rejected, accepted, warnings),
        ).fetchone()
        return row[0] if row else None
