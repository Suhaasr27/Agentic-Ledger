"""Row validation, header mapping, row numbering, merging and event-line parsing (no database)."""
from pathlib import Path

from ledger.events import parse_event_line
from ledger.leads import Accepted, Rejected, parse_lead_file, validate_row


def test_rejection_walks_schema_order_first_failure_wins(catalog):
    schema = catalog.schemas["motor_renewal"]
    # name missing AND phone invalid -> name comes first in the schema
    assert validate_row(schema, {"customer_name": "", "phone": "123", "policy_number": "P"}) == \
        Rejected("missing_required:customer_name")
    # phone invalid AND policy missing -> phone comes first
    assert validate_row(schema, {"customer_name": "A", "phone": "123", "policy_number": ""}) == Rejected("invalid_phone")
    assert validate_row(schema, {"customer_name": "A", "phone": "", "policy_number": "P"}) == \
        Rejected("missing_required:phone")
    assert validate_row(schema, {"customer_name": "A", "phone": "9876543210", "policy_number": "  "}) == \
        Rejected("missing_required:policy_number")


def test_optional_invalid_becomes_null_with_one_warning_each(catalog):
    schema = catalog.schemas["motor_renewal"]
    out = validate_row(schema, {"customer_name": "A", "phone": "9876543210", "policy_number": "P",
                                "policy_expiry": "31/02/2026", "premium_amount": "N/A",
                                "preferred_language": "Hindi"})
    assert isinstance(out, Accepted)
    assert out.warnings == 3
    assert set(out.values) == {"customer_name", "phone", "policy_number"}


def test_optional_absent_is_not_a_warning(catalog):
    out = validate_row(catalog.schemas["motor_renewal"],
                       {"customer_name": "A", "phone": "9876543210", "policy_number": "P", "premium_amount": " "})
    assert out.warnings == 0


def test_rejected_rows_contribute_no_warnings(tmp_path: Path, catalog):
    f = tmp_path / "batch_001.csv"
    f.write_text("customer_name,phone,policy_number,policy_expiry\n"
                 "A,bad-phone,P1,not-a-date\n"   # rejected: its bad date must NOT be counted
                 "B,9876543210,P2,not-a-date\n")  # accepted: one warning
    parsed = parse_lead_file(f, catalog.schemas["motor_renewal"])
    assert (parsed.records_total, len(parsed.rejections), parsed.field_warnings) == (2, 1, 1)


def test_headers_aliases_extra_columns_blank_rows_and_row_numbers(tmp_path: Path, catalog):
    f = tmp_path / "batch_003.csv"
    f.write_text(
        "Language,Insured Name,Policy_No,MOBILE NO,agent_notes\r\n"  # renamed, reordered, extra column
        "hi,Asha,P1,98765 43210,VIP\r\n"
        ",,,,\r\n"                                                   # blank row: skipped, not counted
        "   ,  ,,,\r\n"                                              # whitespace-only: blank too
        "en,Ravi,,9876543211,\r\n"                                   # row 2 (not 4): missing policy
        "en,Meena,P3,12345,\r\n",                                    # row 3: invalid phone
        encoding="utf-8")
    parsed = parse_lead_file(f, catalog.schemas["motor_renewal"])
    assert parsed.records_total == 3
    assert [(r.row_number, r.reason) for r in parsed.rejections] == [
        (2, "missing_required:policy_number"), (3, "invalid_phone")]
    assert parsed.leads["+919876543210"].fields["preferred_language"] == "hi"


def test_utf8_bom_and_short_rows(tmp_path: Path, catalog):
    f = tmp_path / "batch_001.csv"
    f.write_bytes("﻿customer_name,phone,policy_number\nAsha,9876543210\n".encode())  # short row, BOM
    parsed = parse_lead_file(f, catalog.schemas["motor_renewal"])
    assert parsed.records_total == 1 and parsed.rejections[0].reason == "missing_required:policy_number"


def test_in_file_duplicates_merge_last_non_null_wins(tmp_path: Path, catalog):
    f = tmp_path / "batch_001.csv"
    f.write_text("customer_name,phone,policy_number,premium_amount,preferred_language\n"
                 "Asha,9876543210,P1,1000,hi\n"
                 "Asha R,+91 98765 43210,P1,,ta\n"       # blank premium must NOT erase 1000
                 "Asha R3,09876543210,P1,,\n"            # blank language must NOT erase 'ta'
                 )
    parsed = parse_lead_file(f, catalog.schemas["motor_renewal"])
    assert parsed.records_accepted == 3 and len(parsed.leads) == 1   # 1 lead + 2 merged duplicates
    merged = parsed.leads["+919876543210"].fields
    assert merged["customer_name"] == "Asha R3"        # last write wins
    assert merged["preferred_language"] == "ta"
    assert merged["premium_amount"] == "1000.00"       # null never overwrites


def test_event_line_malformed_variants():
    h = lambda p: b"x"
    for bad in ['{"event_id": "e1", "event_ty', "not json", "[1, 2]", '{"event_type": "call.initiated"}',
                '{"event_id": "", "event_type": "call.initiated", "occurred_at": "2026-10-05T10:00:00Z"}',
                '{"event_id": "e1", "event_type": "call.initiated", "occurred_at": "yesterday"}']:
        assert parse_event_line(bad, h) is None


def test_event_line_ok_and_phone_never_survives():
    seen = []
    ev = parse_event_line(
        '{"event_id":"e1","event_type":"call.initiated","workflow_id":"w","call_id":"c","phone":"91-98765-43210",'
        '"occurred_at":"2026-10-05 10:00:00","payload":{}}', lambda p: seen.append(p) or b"H")
    assert seen == ["+919876543210"] and ev.phone_hash == b"H"
    assert "9876543210" not in repr(ev)
    assert ev.occurred_at.utcoffset().total_seconds() == 19800  # naive -> IST
    override = parse_event_line('{"event_id":"e2","event_type":"disposition.override","call_id":"c",'
                                '"occurred_at":"2026-10-05T10:00:00Z","payload":{"disposition_code":"x"}}',
                                lambda p: b"H")
    assert override.phone_hash is None
