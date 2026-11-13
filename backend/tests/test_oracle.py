"""End-to-end: generate data with the official generator on fresh seeds, run the real CLI-level
pipeline (drop_1 then drop_2), and diff all five files against the independent oracle."""
import csv
import json
import shutil
import subprocess
import sys
import uuid

import pytest

import oracle
from conftest import TEST_DSN, World
from ledger import db, reporting
from ledger.config import REPO_ROOT

GENERATOR = REPO_ROOT / "tools" / "generate_data.py"


def _read_csv(path):
    with path.open(newline="", encoding="utf-8") as fh:
        rows = list(csv.reader(fh))
    return rows[0], rows[1:]


@pytest.fixture(scope="module")
def generated(tmp_path_factory):
    def make(seed: int):
        out = tmp_path_factory.mktemp(f"seed{seed}")
        subprocess.run([sys.executable, str(GENERATOR), "--seed", str(seed), "--scale", "small", "--out", str(out)],
                       check=True, capture_output=True)
        return out
    return make


@pytest.mark.parametrize("seed", [7, 2026])
def test_reports_match_independent_oracle_on_fresh_seed(seed, generated, conn, catalog, pii, tmp_path):
    data = generated(seed)
    world = World(conn, catalog, pii, data)
    world.drop("drop_1")
    world.drop("drop_2")
    out = tmp_path / "reports"
    reporting.write_reports(conn, out)

    expected = oracle.run([data / "drop_1", data / "drop_2"], catalog)
    for name in ["batch_summary.csv", "daily_overview.csv", "lead_report.csv", "rejections.csv"]:
        header, rows = _read_csv(out / name)
        assert rows == expected[name], name
        assert rows, f"{name} unexpectedly empty"
    assert json.loads((out / "data_quality.json").read_text()) == expected["data_quality.json"]


def test_incremental_equals_all_at_once_and_reingest_is_noop_on_generated_data(generated, conn, catalog, pii, tmp_path):
    import psycopg
    data = generated(99)
    world = World(conn, catalog, pii, data)
    world.drop("drop_1")
    world.drop("drop_2")
    assert world.drop("drop_1").ingested == 0 and world.drop("drop_2").ingested == 0
    inc = tmp_path / "inc"
    reporting.write_reports(conn, inc)

    # one-shot: both drops merged into a single directory (same files, same sort order)
    merged = tmp_path / "all"
    for src in ("drop_1", "drop_2"):
        shutil.copytree(data / src, merged, dirs_exist_ok=True)
    schema = f"t_{uuid.uuid4().hex[:10]}"
    admin = psycopg.connect(TEST_DSN, autocommit=True)
    admin.execute(f"CREATE SCHEMA {schema}")
    try:
        other = db.connect(TEST_DSN, schema=schema)
        db.migrate(other)
        from ledger.ingest import Ingestor
        Ingestor(other, catalog, pii).ingest_drop(merged)
        one = tmp_path / "one"
        reporting.write_reports(other, one)
        other.close()
    finally:
        admin.execute(f"DROP SCHEMA {schema} CASCADE")
        admin.close()
    for name in ["batch_summary.csv", "daily_overview.csv", "lead_report.csv", "rejections.csv", "data_quality.json"]:
        assert (inc / name).read_bytes() == (one / name).read_bytes(), name


def test_reports_follow_the_output_contract(generated, conn, catalog, pii, tmp_path):
    data = generated(5)
    world = World(conn, catalog, pii, data)
    world.drop("drop_1"); world.drop("drop_2")
    out = tmp_path / "r"
    reporting.write_reports(conn, out)
    assert _read_csv(out / "batch_summary.csv")[0] == reporting.BATCH_SUMMARY_COLS
    assert _read_csv(out / "daily_overview.csv")[0] == reporting.DAILY_COLS
    assert _read_csv(out / "lead_report.csv")[0] == reporting.LEAD_COLS
    assert _read_csv(out / "rejections.csv")[0] == reporting.REJECTION_COLS
    _, daily = _read_csv(out / "daily_overview.csv")
    assert all(len(r[4].split(".")[1]) == 4 for r in daily)           # exactly 4 decimals
    _, leads = _read_csv(out / "lead_report.csv")
    assert all(r[3].startswith("+91******") and len(r[3]) == 13 for r in leads)
    assert all(r[9] in ("true", "false") for r in leads)
    assert list(json.loads((out / "data_quality.json").read_text())) == reporting.DATA_QUALITY_KEYS
    assert b"\r" not in (out / "lead_report.csv").read_bytes()
