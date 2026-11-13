"""Command line: ``python -m ledger {keygen,migrate,ingest,report,serve}``."""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

from cryptography.fernet import Fernet

from . import db, reporting
from .config import ConfigError, Settings
from .ingest import Ingestor
from .pii import PiiProtector
from .schema import Catalog


def _open(settings: Settings):
    conn = db.connect(settings.database_url)
    db.migrate(conn)
    return conn


def cmd_keygen(_: argparse.Namespace) -> int:
    import secrets
    print(f"export LEDGER_HMAC_KEY={secrets.token_hex(32)}")
    print(f"export LEDGER_FERNET_KEY={Fernet.generate_key().decode()}")
    return 0


def cmd_migrate(_: argparse.Namespace) -> int:
    settings = Settings.from_env()
    with db.connect(settings.database_url) as conn:
        applied = db.migrate(conn)
    print("applied:", ", ".join(applied) if applied else "nothing (up to date)")
    return 0


def cmd_ingest(args: argparse.Namespace) -> int:
    settings = Settings.from_env()
    catalog = Catalog.load(settings.schemas_dir)
    pii = PiiProtector(settings.hmac_key, settings.fernet_key)
    with _open(settings) as conn:
        result = Ingestor(conn, catalog, pii).ingest_drop(Path(args.drop))
    for f in result.files:
        print(f"{f.status:9} {f.kind:6} {f.workflow_id or '-':16} {f.file:34} "
              f"rows={f.records_total} rejected={f.records_rejected} accepted={f.records_accepted}")
    for w in result.warnings:
        print("warning:", w, file=sys.stderr)
    print(f"{result.drop}: {result.ingested} ingested, {result.skipped} skipped (already ingested)")
    return 0


def cmd_report(args: argparse.Namespace) -> int:
    settings = Settings.from_env()
    out = Path(args.out)
    with _open(settings) as conn:
        counts = reporting.write_reports(conn, out)
    print(f"wrote 5 files to {out}/ ", counts)
    return 0


def cmd_serve(args: argparse.Namespace) -> int:
    import uvicorn
    uvicorn.run("ledger.api:create_app", factory=True, host=args.host, port=args.port)
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="ledger", description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("keygen", help="print fresh PII keys").set_defaults(func=cmd_keygen)
    sub.add_parser("migrate", help="apply database migrations").set_defaults(func=cmd_migrate)
    p = sub.add_parser("ingest", help="ingest one drop directory (safe to re-run)")
    p.add_argument("drop", help="e.g. data/drop_1")
    p.set_defaults(func=cmd_ingest)
    p = sub.add_parser("report", help="write the five report files")
    p.add_argument("--out", required=True, help="output directory")
    p.set_defaults(func=cmd_report)
    p = sub.add_parser("serve", help="run the FastAPI service")
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=8000)
    p.set_defaults(func=cmd_serve)
    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except (ConfigError, FileNotFoundError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
