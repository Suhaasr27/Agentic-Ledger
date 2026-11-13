"""The value rules most likely to break: phones, dates, decimals, headers, timestamps."""
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

import pytest

from ledger.normalize import (IST, normalize_header, normalize_phone, parse_date, parse_decimal,
                              parse_enum, parse_timestamp)


@pytest.mark.parametrize("raw", [
    "9876543210",          # plain
    "+91 98765 43210",     # spaced
    "09876543210",         # trunk zero
    "91-98765-43210",      # dashed with country code
    "(+91) 9876543210",    # brackets
    "+919876543210",       # compact
    "  98765 43210  ",     # padding
])
def test_phone_all_six_generator_formats_normalise(raw):
    assert normalize_phone(raw) == "+919876543210"


@pytest.mark.parametrize("raw", [
    "987654321",           # 9 digits
    "98765432101",         # 11 digits not starting with 0
    "5876543210",          # bad prefix (must start 6-9)
    "022-2345-6789",       # landline: 0 + 2223456789 -> prefix 2
    "+1 415 555 0134",     # foreign
    "N/A", "", "abcdefghij",
    "919876543210 1",      # 13 digits
    "0919876543210",       # 0 then 91 + 10 digits = 13 digits: only ONE prefix is stripped
])
def test_phone_invalid(raw):
    assert normalize_phone(raw) is None


def test_phone_prefix_rules_are_exact():
    assert normalize_phone("911234567890") is None       # 12 digits w/ 91 -> 1234567890, starts with 1
    assert normalize_phone("916234567890") == "+916234567890"
    assert normalize_phone("0 6234567890") == "+916234567890"
    assert normalize_phone("6000000000") == "+916000000000"  # boundary prefix 6
    assert normalize_phone("٩٨٧٦٥٤٣٢١٠") is None            # non-ASCII digits are not digits
    assert normalize_phone(None) is None
    assert normalize_phone(9876543210) == "+919876543210"    # numeric JSON value


@pytest.mark.parametrize("raw, expected", [
    ("2026-12-01", date(2026, 12, 1)),
    ("01/12/2026", date(2026, 12, 1)),     # day-first, NOT Jan 12
    ("01-12-2026", date(2026, 12, 1)),
    ("01-Dec-2026", date(2026, 12, 1)),
    ("01-dEC-2026", date(2026, 12, 1)),    # month name case-insensitive
    ("29/02/2028", date(2028, 2, 29)),     # leap day
    ("  13/01/2026 ", date(2026, 1, 13)),
])
def test_dates_valid(raw, expected):
    assert parse_date(raw) == expected


@pytest.mark.parametrize("raw", [
    "31/02/2026", "29/02/2027", "2026-13-01", "2026-02-30", "12/13/2026",  # impossible (day-first!)
    "soon", "TBD", "", "01-Foo-2026", "01.12.2026", "2026/12/01", "1/2/26",
    "01/12-2026",                                                            # mixed separators
])
def test_dates_invalid(raw):
    assert parse_date(raw) is None


@pytest.mark.parametrize("raw, expected", [
    ("12345", "12345.00"), ("12345.5", "12345.50"), ("₹27,554", "27554.00"),
    ("Rs. 4200", "4200.00"), ("rs 4200", "4200.00"), ("RS.4200", "4200.00"),
    ("INR 12345.7", "12345.70"), ("inr12,345.67", "12345.67"), ("0", "0.00"),
    ("1,23,456.789", "123456.79"),
])
def test_decimal_valid(raw, expected):
    assert parse_decimal(raw) == Decimal(expected)


@pytest.mark.parametrize("raw", ["N/A", "-", "twelve thousand", "-5", "Rs.", "₹", "", "1.2.3", "12,34x", "$100"])
def test_decimal_invalid(raw):
    assert parse_decimal(raw) is None


def test_enum_case_insensitive_lowercase_storage():
    values = ("en", "hi")
    assert parse_enum("HI", values) == "hi"
    assert parse_enum(" en ", values) == "en"
    assert parse_enum("Hindi", values) is None


def test_header_normalisation():
    assert normalize_header("  Customer_Name ") == "customer name"
    assert normalize_header("Mobile__No") == "mobile no"
    assert normalize_header("POLICY \t No") == "policy no"
    assert normalize_header("Order ID") == normalize_header("order_id")


def test_timestamp_three_forms_naive_is_ist():
    utc = parse_timestamp("2026-10-05T04:30:00Z")
    offset = parse_timestamp("2026-10-05T10:00:00+05:30")
    naive = parse_timestamp("2026-10-05 10:00:00")
    assert utc == offset == naive
    assert naive.utcoffset() == timedelta(hours=5, minutes=30)
    assert parse_timestamp("garbage") is None and parse_timestamp(None) is None


def test_ist_date_boundary():
    late_utc = parse_timestamp("2026-10-05T19:00:00Z")  # 00:30 IST next day
    assert late_utc.astimezone(IST).date() == date(2026, 10, 6)
