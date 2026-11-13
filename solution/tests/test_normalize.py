import pytest

from ledger.normalize import (normalize_header, normalize_phone, parse_date, parse_decimal, parse_enum,
                              parse_timestamp)


@pytest.mark.parametrize("raw", [
    "9876543210", "+91 98765 43210", "09876543210", "91-98765-43210", "(+91) 9876543210",
    "+919876543210", " 98765 43210 ", "6000000000", "7123456789", "8123456789"])
def test_phone_valid_formats(raw):
    assert normalize_phone(raw) == "+91" + "".join(c for c in raw if c.isdigit())[-10:]


@pytest.mark.parametrize("raw", [
    "", "N/A", "987654321", "5876543210", "022-2345-6789", "+1 415 555 0134", "98765432101",
    "919876543210 9", "0098765432100", "+91 5876543210", "91 98765 4321"])
def test_phone_invalid(raw):
    assert normalize_phone(raw) is None


def test_phone_prefix_rules_are_exact():
    assert normalize_phone("919876543210") == "+919876543210"      # 12 digits starting 91
    assert normalize_phone("09876543210") == "+919876543210"       # 11 digits starting 0
    assert normalize_phone("91987654321") is None                  # 11 digits starting 9: not a 0-prefix


def test_phone_ten_digits_starting_91_is_just_a_ten_digit_number():
    assert normalize_phone("9123456789") == "+919123456789"


@pytest.mark.parametrize("raw,expected", [
    ("2026-12-01", "2026-12-01"), ("01/12/2026", "2026-12-01"), ("01-12-2026", "2026-12-01"),
    ("01-Dec-2026", "2026-12-01"), ("01-dec-2026", "2026-12-01"), ("01-DEC-2026", "2026-12-01"),
    ("1/2/2026", "2026-02-01"), ("29/02/2028", "2028-02-29")])
def test_dates_valid_and_day_first(raw, expected):
    assert parse_date(raw) == expected


@pytest.mark.parametrize("raw", [
    "31/02/2026", "2026-13-01", "soon", "TBD", "", "29/02/2027", "01/13/2026", "2026/12/01",
    "01-Foo-2026", "01/12-2026", "12/2026"])
def test_dates_invalid(raw):
    assert parse_date(raw) is None


@pytest.mark.parametrize("raw,expected", [
    ("1234", "1234.00"), ("1234.5", "1234.50"), ("₹12,000", "12000.00"), ("Rs. 500", "500.00"),
    ("rs 500", "500.00"), ("RS.500", "500.00"), ("INR 1234.5", "1234.50"), ("inr1,234.567", "1234.57"),
    ("0", "0.00"), ("1,00,000", "100000.00"), ("0.005", "0.01")])
def test_decimal_valid(raw, expected):
    assert parse_decimal(raw) == expected


@pytest.mark.parametrize("raw", ["N/A", "-", "twelve thousand", "-5", "₹-5", "", "USD 5", "1.2.3"])
def test_decimal_invalid(raw):
    assert parse_decimal(raw) is None


def test_enum_case_insensitive_stored_lowercase():
    assert parse_enum("HI", ["en", "hi"]) == "hi"
    assert parse_enum(" en ", ["en", "hi"]) == "en"
    assert parse_enum("Hindi", ["en", "hi"]) is None


def test_header_normalisation():
    assert normalize_header("  Policy_No ") == "policy no"
    assert normalize_header("customer__name") == "customer name"
    assert normalize_header("Mobile   No") == "mobile no"
    assert normalize_header("Order ID") == normalize_header("order_id")


def test_timestamps_three_forms():
    utc = parse_timestamp("2026-10-03T04:31:00Z")
    assert parse_timestamp("2026-10-03T10:01:00+05:30") == utc
    assert parse_timestamp("2026-10-03 10:01:00") == utc          # naive means IST
    assert parse_timestamp("2026-10-03T10:01:00+00:00") == utc + 19800
    assert parse_timestamp("garbage") is None and parse_timestamp(None) is None
