"""Unit tests for :func:`parser.timestamps.parse_timestamp`."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from parser.timestamps import parse_timestamp


def test_iso_with_z_suffix() -> None:
    assert parse_timestamp("2024-01-01T12:30:45Z") == datetime(
        2024, 1, 1, 12, 30, 45, tzinfo=timezone.utc
    )


def test_iso_with_explicit_offset() -> None:
    expected = datetime(
        2024, 1, 1, 12, 30, 45, tzinfo=timezone(timedelta(hours=3))
    )
    assert parse_timestamp("2024-01-01T12:30:45+03:00") == expected


def test_iso_space_separated_naive() -> None:
    result = parse_timestamp("2024-01-01 12:30:45")
    assert result == datetime(2024, 1, 1, 12, 30, 45)
    assert result.tzinfo is None


def test_iso_with_fractional_seconds() -> None:
    assert parse_timestamp("2024-01-01 12:30:45.123") == datetime(
        2024, 1, 1, 12, 30, 45, 123_000
    )


def test_syslog_yearless_is_none() -> None:
    # Syslog carries no year, so the stamp is incomplete: the parser reports it
    # as missing rather than completing it from anywhere.
    assert parse_timestamp("Jan 10 14:52:31") is None


def test_syslog_space_padded_day_is_none() -> None:
    assert parse_timestamp("Jan  1 14:52:31") is None


def test_yearless_never_becomes_year_1900() -> None:
    # Regression guard: ``strptime``'s default epoch must never leak out as a
    # real event time. 1900 would sort before every genuine timestamp.
    for value in ("Jan 10 14:52:31", "Jan  1 14:52:31", "Dec 31 23:59:59"):
        assert parse_timestamp(value) is None


def test_yearless_fractional_seconds_is_none() -> None:
    assert parse_timestamp("Jan 10 14:52:31.123") is None


def test_yearless_february_29_is_none_not_an_error() -> None:
    # Feb 29 exists only in a leap year, and 1900 was not one — under the old
    # behaviour this raised inside ``strptime``. It is simply incomplete.
    assert parse_timestamp("Feb 29 12:00:00") is None


def test_nestjs_us_locale_am() -> None:
    # NestJS' default logger: "MM/DD/YYYY, HH:MM:SS AM" (12-hour clock).
    assert parse_timestamp("07/22/2026, 10:15:30 AM") == datetime(
        2026, 7, 22, 10, 15, 30
    )


def test_nestjs_us_locale_pm_hour_offset() -> None:
    # 12-hour PM times are shifted into 24-hour form.
    assert parse_timestamp("07/22/2026, 01:05:09 PM") == datetime(
        2026, 7, 22, 13, 5, 9
    )


def test_mssql_two_digit_fractional_seconds() -> None:
    # SQL Server ERRORLOG stamps use centiseconds; ISO parsing handles them.
    assert parse_timestamp("2026-07-22 10:15:30.14") == datetime(
        2026, 7, 22, 10, 15, 30, 140_000
    )


def test_datetime_passthrough() -> None:
    dt = datetime(2024, 5, 5, 5, 5, 5)
    assert parse_timestamp(dt) is dt


@pytest.mark.parametrize(
    "value",
    [None, "", "   ", "not-a-date", "2024/01/01", "1704110445", "13:00"],
)
def test_invalid_or_unsupported_returns_none(value: object) -> None:
    assert parse_timestamp(value) is None


def test_never_raises_on_garbage() -> None:
    # Contract: deterministic and exception-free, whatever the input.
    for value in ("", "??", "\x00", "9999-99-99", 3.14, [], {}):
        assert parse_timestamp(value) is None


def test_deterministic() -> None:
    value = "2024-01-01T12:30:45Z"
    assert parse_timestamp(value) == parse_timestamp(value)
