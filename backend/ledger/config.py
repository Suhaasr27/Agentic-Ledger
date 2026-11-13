"""Runtime settings, read from the environment. Secrets are never defaulted: fail closed."""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]


class ConfigError(RuntimeError):
    pass


@dataclass(frozen=True)
class Settings:
    database_url: str
    hmac_key: bytes
    fernet_key: bytes
    schemas_dir: Path
    data_root: Path
    reports_dir: Path
    db_schema: str | None = None  # pins search_path; used to isolate test runs

    @classmethod
    def from_env(cls) -> "Settings":
        missing = [k for k in ("LEDGER_HMAC_KEY", "LEDGER_FERNET_KEY") if not os.environ.get(k)]
        if missing:
            raise ConfigError(
                f"missing {', '.join(missing)}. Generate keys with `python -m ledger keygen` "
                "and export them (see .env.example)."
            )
        return cls(
            database_url=os.environ.get("DATABASE_URL", "postgresql://postgres:postgres@localhost:5432/ledger"),
            hmac_key=os.environ["LEDGER_HMAC_KEY"].encode(),
            fernet_key=os.environ["LEDGER_FERNET_KEY"].encode(),
            schemas_dir=Path(os.environ.get("LEDGER_SCHEMAS_DIR", REPO_ROOT / "schemas")),
            data_root=Path(os.environ.get("LEDGER_DATA_ROOT", REPO_ROOT / "data")),
            reports_dir=Path(os.environ.get("LEDGER_REPORTS_DIR", REPO_ROOT / "reports")),
            db_schema=os.environ.get("LEDGER_DB_SCHEMA") or None,
        )
