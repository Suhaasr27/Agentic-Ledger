"""Derived facts and pipeline guarantees, through the real ingest code and a real Postgres."""
import json

import psycopg
import pytest

from conftest import ev, lead
from ledger import reporting

MR = "motor_renewal"


def _lead_row(conn, ref="POL-1"):
    rows = reporting.lead_report(conn)
    return next(r for r in rows if r["external_ref"] == ref)


def _call(call, phone="9876543210", start="2026-10-05T10:00:00+05:30"):
    return [ev("call.initiated", call=call, phone=phone, at=start)]


# ------------------------------------------------------------------ effective disposition
@pytest.mark.parametrize("name, dispositions, expected", [
    ("latest set wins", [("set", "callback_requested", "10:05"), ("set", "payment_link_sent", "10:09")],
     "payment_link_sent"),
    ("set order is by time not arrival", [("set", "payment_link_sent", "10:09"), ("set", "callback_requested", "10:05")],
     "payment_link_sent"),
    ("override beats set", [("set", "renewed", "10:05"), ("override", "not_interested", "11:00")], "not_interested"),
    ("override wins even if stamped earlier", [("set", "renewed", "10:30"), ("override", "not_interested", "10:10")],
     "not_interested"),
    ("latest override wins", [("override", "dnd", "11:00"), ("override", "wrong_number", "12:00")], "wrong_number"),
    ("unknown override is ignored, falls back to set",
     [("set", "payment_link_sent", "10:05"), ("override", "made_up", "11:00")], "payment_link_sent"),
    ("unknown set leaves no disposition", [("set", "RENEWD", "10:05")], None),
])
def test_effective_disposition(conn, make_world, name, dispositions, expected):
    events = _call("c1")
    for kind, code, hhmm in dispositions:
        events.append(ev(f"disposition.{kind}", call="c1", at=f"2026-10-05T{hhmm}:00+05:30",
                         phone=None if kind == "override" else "9876543210", disposition_code=code))
    make_world(conn).drop("d1", leads={(MR, "batch_001"): [lead()]}, events=events)
    assert _lead_row(conn)["current_disposition"] == expected, name


def test_override_never_edits_the_original_event(conn, make_world):
    make_world(conn).drop("d1", leads={(MR, "batch_001"): [lead()]}, events=[
        *_call("c1"), ev("disposition.set", call="c1", disposition_code="renewed"),
        ev("disposition.override", call="c1", phone=None, disposition_code="not_interested",
           at="2026-10-06T10:00:00+05:30", overridden_by="sup", reason="qa")])
    codes = [r[0] for r in conn.execute(
        "SELECT payload->>'disposition_code' FROM events WHERE event_type LIKE 'disposition.%' ORDER BY event_id")]
    assert sorted(codes) == ["not_interested", "renewed"]  # both survive in the ledger
    row = _lead_row(conn)
    assert row["ever_success"] is False and row["current_disposition"] == "not_interested"


def test_current_disposition_is_from_most_recent_call_that_has_one(conn, make_world):
    events = [
        *_call("c1", start="2026-10-05T10:00:00+05:30"),
        ev("disposition.set", call="c1", disposition_code="callback_requested", at="2026-10-05T10:05:00+05:30"),
        *_call("c2", start="2026-10-06T10:00:00+05:30"),       # newer call, no disposition at all
    ]
    make_world(conn).drop("d1", leads={(MR, "batch_001"): [lead()]}, events=events)
    row = _lead_row(conn)
    assert row["total_calls"] == 2 and row["current_disposition"] == "callback_requested"


# ------------------------------------------------------------------ buckets
@pytest.mark.parametrize("name, build, bucket", [
    ("no calls", lambda: [], "yet_to_reach_out"),
    ("one dialed call", lambda: _call("c1"), "in_progress_dialed"),
    ("only telephony failures", lambda: [*_call("c1"), ev("call.telephony_failed", call="c1", reason="x"),
                                         *_call("c2", start="2026-10-06T10:00:00+05:30"),
                                         ev("call.telephony_failed", call="c2", reason="x",
                                            at="2026-10-06T10:00:05+05:30")],
     "in_progress_telephony_failed"),
    ("one failed + one dialed is NOT all failed", lambda: [*_call("c1"), ev("call.telephony_failed", call="c1"),
                                                         *_call("c2", start="2026-10-06T10:00:00+05:30")],
     "in_progress_dialed"),
    ("terminal disposition", lambda: [*_call("c1"), ev("call.connected", call="c1"),
                                      ev("disposition.set", call="c1", disposition_code="not_interested")],
     "completed"),
    ("non-terminal disposition stays in progress", lambda: [*_call("c1"), ev("call.connected", call="c1"),
                                                           ev("disposition.set", call="c1",
                                                              disposition_code="callback_requested")],
     "in_progress_dialed"),
    ("completed beats telephony_failed", lambda: [*_call("c1"), ev("call.telephony_failed", call="c1"),
                                                  ev("disposition.set", call="c1", disposition_code="dnd")],
     "completed"),
    ("override can make a call terminal", lambda: [*_call("c1"), ev("disposition.set", call="c1",
                                                                     disposition_code="callback_requested"),
                                                   ev("disposition.override", call="c1", phone=None,
                                                      disposition_code="dnd", at="2026-10-06T10:00:00+05:30")],
     "completed"),
    ("override can reopen a lead", lambda: [*_call("c1"), ev("disposition.set", call="c1", disposition_code="dnd"),
                                            ev("disposition.override", call="c1", phone=None,
                                               disposition_code="callback_requested",
                                               at="2026-10-06T10:00:00+05:30")],
     "in_progress_dialed"),
    ("events without call.initiated do not make a call", lambda: [ev("call.connected", call="c1")],
     "yet_to_reach_out"),
])
def test_bucket_logic(conn, make_world, name, build, bucket):
    events = build()
    make_world(conn).drop("d1", leads={(MR, "batch_001"): [lead()]}, events=events or None)
    assert _lead_row(conn)["bucket"] == bucket, name


def test_batch_summary_counts_buckets_per_batch(conn, make_world):
    leads = [lead(phone="9876543210", ref="A"), lead(phone="9876543211", ref="B"), lead(phone="9876543212", ref="C")]
    events = [*_call("c1", phone="9876543210"),
              ev("disposition.set", call="c1", phone="9876543210", disposition_code="renewed"),
              *_call("c2", phone="9876543211")]
    make_world(conn).drop("d1", leads={(MR, "batch_007"): leads}, events=events)
    (row,) = reporting.batch_summary(conn)
    assert row == {"workflow_id": MR, "batch_id": "batch_007", "total_leads": 3, "yet_to_reach_out": 1,
                   "in_progress_telephony_failed": 0, "in_progress_dialed": 1, "completed": 1}


# ------------------------------------------------------------------ daily overview
def test_daily_overview_uses_ist_date_of_initiation_and_distinct_success_leads(conn, make_world):
    events = [
        # 2026-10-05T19:00Z is 00:30 IST on the 6th
        ev("call.initiated", call="c1", at="2026-10-05T19:00:00Z"), ev("call.connected", call="c1", at="2026-10-05T19:00:09Z"),
        ev("disposition.set", call="c1", disposition_code="renewed", at="2026-10-05T19:05:00Z"),
        # two successful calls the same day for the SAME lead count once
        ev("call.initiated", call="c2", at="2026-10-06 10:00:00"),
        ev("disposition.set", call="c2", disposition_code="renewed", at="2026-10-06 10:05:00"),
        ev("call.initiated", call="c3", at="2026-10-06 11:00:00"),
    ]
    make_world(conn).drop("d1", leads={(MR, "batch_001"): [lead()]}, events=events)
    (row,) = reporting.daily_overview(conn)
    assert row == {"workflow_id": MR, "date": "2026-10-06", "total_calls": 3, "connected_calls": 1,
                   "success_leads": 1, "connectivity_rate": "0.3333"}


# ------------------------------------------------------------------ linking, orphans, ordering
def test_orphans_resolve_when_their_lead_arrives_in_a_later_drop(conn, make_world):
    world = make_world(conn)
    events = [*_call("c1"), ev("call.connected", call="c1"),
              ev("disposition.override", call="c1", phone=None, disposition_code="dnd", at="2026-10-06T10:00:00+05:30")]
    world.drop("drop_1", events=events)                      # events first, lead not uploaded yet
    assert reporting.data_quality(conn)["orphan_events"] == 3
    assert reporting.lead_report(conn) == []

    world.drop("drop_2", leads={(MR, "batch_001"): [lead()]})  # lead arrives later
    assert reporting.data_quality(conn)["orphan_events"] == 0
    row = _lead_row(conn)
    assert (row["total_calls"], row["connected_calls"], row["bucket"]) == (1, 1, "completed")  # override linked too


def test_unlinkable_events_stay_out_of_every_report(conn, make_world):
    events = [*_call("c1", phone="9000000001"),                       # number never uploaded
              *_call("c2", phone="12345"),                            # invalid phone
              ev("disposition.override", call="ghost", phone=None, disposition_code="dnd"),  # unknown call
              ev("call.initiated", call="c3", wf="no_such_workflow")]  # unknown workflow
    make_world(conn).drop("d1", leads={(MR, "batch_001"): [lead()]}, events=events)
    assert reporting.data_quality(conn)["orphan_events"] == 4
    assert reporting.daily_overview(conn) == []
    assert _lead_row(conn)["total_calls"] == 0


def test_orphan_takes_precedence_over_unknown_disposition(conn, make_world):
    events = [ev("disposition.set", call="c1", phone="9000000001", disposition_code="nope"),   # orphan
              *_call("c2"), ev("disposition.set", call="c2", disposition_code="nope")]          # linked, unknown
    make_world(conn).drop("d1", leads={(MR, "batch_001"): [lead()]}, events=events)
    dq = reporting.data_quality(conn)
    assert (dq["orphan_events"], dq["unknown_disposition_events"]) == (1, 1)


def test_event_before_initiated_does_not_matter_and_is_not_a_call(conn, make_world):
    events = [ev("call.connected", call="c1", at="2026-10-05T10:00:05+05:30"),   # arrives before initiated
              ev("call.initiated", call="c1")]
    make_world(conn).drop("d1", leads={(MR, "batch_001"): [lead()]}, events=events)
    assert _lead_row(conn)["connected_calls"] == 1


# ------------------------------------------------------------------ dedupe & data quality counters
def test_event_dedupe_within_file_and_across_drops_and_malformed_counts(conn, make_world):
    world = make_world(conn)
    initiated = ev("call.initiated", call="c1", event_id="evt_dup")
    world.drop("drop_1", leads={(MR, "batch_001"): [lead()]},
               events=[initiated, initiated, '{"event_id": "evt_trunc', ev("call.connected", call="c1")])
    world.drop("drop_2", events=[initiated, ev("call.initiated", call="c2", at="2026-10-06T10:00:00+05:30")])
    dq = reporting.data_quality(conn)
    assert dq["events_total"] == 6 and dq["malformed_events"] == 1 and dq["duplicate_events_dropped"] == 2
    assert _lead_row(conn)["total_calls"] == 2


def test_lead_counters_rejections_and_merges(conn, make_world):
    rows = [lead(ref="A"), lead(ref="A2", name="Asha R"),                # duplicate identity -> merged
            lead(ref="B", phone="12345"), lead(name="", phone="9876543299", ref="C"),
            lead(ref="D", phone="9876543298", policy_expiry="31/02/2026")]  # warning only
    for r in rows:
        r.setdefault("policy_expiry", "")
    make_world(conn).drop("d1", leads={(MR, "batch_001"): rows})
    dq = reporting.data_quality(conn)
    assert (dq["lead_rows_total"], dq["lead_rows_rejected"], dq["duplicate_lead_rows_merged"],
            dq["field_warnings"]) == (5, 2, 1, 1)
    assert dq["rejections_by_reason"] == {"invalid_phone": 1, "missing_required:customer_name": 1}
    assert reporting.rejections(conn) == [
        {"workflow_id": MR, "batch_id": "batch_001", "row_number": 3, "reason": "invalid_phone"},
        {"workflow_id": MR, "batch_id": "batch_001", "row_number": 4, "reason": "missing_required:customer_name"}]


def test_merge_across_batches_keeps_first_batch_and_applies_defaults_last(conn, make_world):
    world = make_world(conn)
    world.drop("d1", leads={(MR, "batch_001"): [lead(name="Asha", ref="P1", premium_amount="1000")]})
    world.drop("d2", leads={(MR, "batch_003"): [{"Insured Name": "Asha Rao", "Mobile No": "+91 98765 43210",
                                                  "Policy No": "P1-NEW", "Language": "HI", "Premium": ""}]})
    row = _lead_row(conn, "P1-NEW")
    assert row["batch_id"] == "batch_001"                       # batch never changes
    attrs, = conn.execute("SELECT attrs FROM v_lead_profile").fetchone()
    assert attrs["preferred_language"] == "hi" and attrs["premium_amount"] == "1000.00"
    # a lead that never states a language gets the schema default
    world.drop("d3", leads={(MR, "batch_004"): [lead(phone="9876543277", ref="P9")]})
    default_attrs, = conn.execute(
        "SELECT attrs FROM v_lead_profile WHERE external_ref = 'P9'").fetchone()
    assert default_attrs["preferred_language"] == "en"


# ------------------------------------------------------------------ idempotency & incrementality
def _snapshot(conn):
    tables = ["ingested_files", "leads", "lead_rejections", "events"]
    snap = {t: conn.execute(f"SELECT count(*) FROM {t}").fetchone()[0] for t in tables}
    snap["leads_state"] = conn.execute("SELECT attrs::text, pii::text, updated_at FROM leads ORDER BY lead_id").fetchall()
    snap["reports"] = (reporting.batch_summary(conn), reporting.daily_overview(conn),
                       reporting.lead_report(conn), reporting.rejections(conn), reporting.data_quality(conn))
    return snap


def test_reingesting_a_drop_changes_nothing(conn, make_world):
    world = make_world(conn)
    leads = {(MR, "batch_001"): [lead(), lead(ref="X", name="Dup")]}
    events = [*_call("c1"), ev("disposition.set", call="c1", disposition_code="renewed"), "garbage"]
    first = world.drop("drop_1", leads=leads, events=events)
    assert first.ingested == 2 and first.skipped == 0
    before = _snapshot(conn)
    again = world.drop("drop_1")  # same files on disk, ingested again
    assert again.ingested == 0 and again.skipped == 2
    assert _snapshot(conn) == before   # including updated_at: no row was even touched


def test_incremental_equals_all_at_once(make_world, conn, catalog, pii, tmp_path):
    import psycopg as pg
    from ledger import db
    from conftest import TEST_DSN

    leads1 = {(MR, "batch_001"): [lead(ref="A"), lead(phone="9876543211", ref="B")]}
    leads2 = {(MR, "batch_003"): [lead(ref="A-v2", name="Asha Two"), lead(phone="9876543212", ref="C")]}
    c1 = [*_call("c1"), ev("call.connected", call="c1"), ev("disposition.set", call="c1", disposition_code="renewed"),
          *_call("c9", phone="9876543212", start="2026-10-05T12:00:00+05:30")]  # orphan until drop_2
    c2 = [ev("disposition.override", call="c1", phone=None, disposition_code="dnd", at="2026-10-07T10:00:00+05:30"),
          *_call("c2", phone="9876543211", start="2026-10-07T12:00:00+05:30"), c1[0]]  # c1[0] repeated across drops

    world_inc = make_world(conn)
    world_inc.drop("drop_1", leads=leads1, events=c1)
    world_inc.drop("drop_2", leads=leads2, events={"events_1001.jsonl": c2})
    incremental = _snapshot(conn)["reports"]

    schema = "t_oneshot"
    admin = pg.connect(TEST_DSN, autocommit=True)
    admin.execute(f"DROP SCHEMA IF EXISTS {schema} CASCADE"); admin.execute(f"CREATE SCHEMA {schema}")
    try:
        other = db.connect(TEST_DSN, schema=schema); db.migrate(other)
        world_all = make_world(other)
        # everything in ONE drop: same files, same filename order
        from conftest import World
        merged_events = {"events_0001.jsonl": c1, "events_1001.jsonl": c2}
        world_all.drop("all", leads={**leads1, **leads2}, events=merged_events)
        oneshot = _snapshot(other)["reports"]
        other.close()
    finally:
        admin.execute(f"DROP SCHEMA {schema} CASCADE"); admin.close()
    assert incremental == oneshot


# ------------------------------------------------------------------ ledger + PII guarantees
def test_ledger_is_append_only(conn, make_world):
    make_world(conn).drop("d1", leads={(MR, "batch_001"): [lead()]}, events=_call("c1"))
    for stmt in ("UPDATE events SET event_type = 'x'", "DELETE FROM events", "TRUNCATE events"):
        with pytest.raises(psycopg.errors.IntegrityError, match="append-only"):
            conn.execute(stmt)
        conn.rollback()
    assert conn.execute("SELECT count(*) FROM events").fetchone()[0] == 1


def test_no_plaintext_pii_anywhere_in_the_database(conn, make_world, pii):
    make_world(conn).drop("d1", leads={(MR, "batch_001"): [lead(name="Zorblax Unusualname", phone="98765 43210")]},
                          events=[*_call("c1", phone="+91-98765-43210")])
    dump = ""
    for table in ("leads", "events", "ingested_files", "lead_rejections"):
        dump += json.dumps([r[0] for r in conn.execute(f"SELECT to_jsonb(t)::text FROM {table} t")])
    assert "Zorblax" not in dump and "9876543210" not in dump and "98765" not in dump
    # recoverable only with the key
    token, = conn.execute("SELECT pii->>'customer_name' FROM leads").fetchone()
    assert pii.decrypt(token) == "Zorblax Unusualname"
    phone_token, = conn.execute("SELECT pii->>'phone' FROM leads").fetchone()
    assert pii.decrypt(phone_token) == "+919876543210"
    # the same phone, written differently in a lead file and in an event, produces the same pseudonym
    lead_hash, = conn.execute("SELECT phone_hash FROM leads").fetchone()
    event_hash, = conn.execute("SELECT phone_hash FROM events").fetchone()
    assert lead_hash == event_hash == pii.phone_hash("+919876543210")


def test_reports_contain_only_masked_phones(conn, make_world, tmp_path):
    make_world(conn).drop("d1", leads={(MR, "batch_001"): [lead(phone="9876543210")]})
    out = tmp_path / "rep"
    reporting.write_reports(conn, out)
    text = (out / "lead_report.csv").read_text()
    assert "+91******3210" in text and "9876543210" not in text and "Asha" not in text
    assert sorted(p.name for p in out.iterdir()) == ["batch_summary.csv", "daily_overview.csv",
                                                     "data_quality.json", "lead_report.csv", "rejections.csv"]
