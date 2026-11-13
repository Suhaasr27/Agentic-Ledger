"""API: only commands + read-only queries. No route can create, edit or delete leads/events."""
import shutil
import uuid

import psycopg
import pytest
from fastapi.testclient import TestClient

from conftest import TEST_DSN
from ledger.api import create_app
from ledger.config import REPO_ROOT, Settings


@pytest.fixture
def client(tmp_path, conn):
    schema = conn.info.options.split("search_path=")[1].split()[0]
    data = tmp_path / "data"
    (data / "drop_1" / "leads" / "motor_renewal").mkdir(parents=True)
    (data / "drop_1" / "events").mkdir()
    (data / "drop_1" / "leads" / "motor_renewal" / "batch_001.csv").write_text(
        "customer_name,phone,policy_number\nAsha,9876543210,P1\nBad,123,P2\n")
    (data / "drop_1" / "events" / "events_0001.jsonl").write_text(
        '{"event_id":"e1","event_type":"call.initiated","workflow_id":"motor_renewal","call_id":"c1",'
        '"phone":"98765 43210","occurred_at":"2026-10-05T10:00:00+05:30","payload":{}}\n')
    cfg = Settings(database_url=TEST_DSN, hmac_key=b"test-hmac-key-0123456789abcdef",
                   fernet_key=__import__("cryptography.fernet", fromlist=["Fernet"]).Fernet.generate_key(),
                   schemas_dir=REPO_ROOT / "schemas", data_root=data, reports_dir=tmp_path / "reports",
                   db_schema=schema)
    return TestClient(create_app(cfg)), tmp_path


def test_ingest_is_idempotent_over_http(client):
    c, _ = client
    assert c.get("/drops").json() == [{"drop": "drop_1", "files": 2, "pending": 2}]
    first = c.post("/drops/drop_1/ingest").json()
    assert (first["ingested"], first["skipped"]) == (2, 0)
    second = c.post("/drops/drop_1/ingest").json()
    assert (second["ingested"], second["skipped"]) == (0, 2)
    assert c.get("/drops").json()[0]["pending"] == 0


def test_read_models_and_publish(client):
    c, tmp = client
    c.post("/drops/drop_1/ingest")
    assert c.get("/reports/data-quality").json()["lead_rows_rejected"] == 1
    leads = c.get("/reports/leads", params={"bucket": "in_progress_dialed"}).json()
    assert leads["total"] == 1 and leads["rows"][0]["phone_masked"] == "+91******3210"
    assert c.get("/reports/leads", params={"bucket": "bogus"}).status_code == 422
    assert c.get("/reports/rejections").json()[0]["reason"] == "invalid_phone"
    assert c.post("/reports/publish").json()["rows"]["lead_report"] == 1
    assert (tmp / "reports" / "lead_report.csv").exists()


def test_no_crud_surface_and_path_traversal_blocked(client):
    c, _ = client
    for method, path in [("delete", "/reports/leads"), ("put", "/reports/leads"), ("patch", "/reports/leads"),
                         ("post", "/reports/leads"), ("delete", "/drops/drop_1")]:
        assert getattr(c, method)(path).status_code in (404, 405), (method, path)  # no such route
    assert c.post("/drops/..%2Fetc/ingest").status_code in (404, 422)
    assert c.post("/drops/nope/ingest").status_code == 404
