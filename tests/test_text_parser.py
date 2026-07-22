"""Unit tests for :class:`parser.text_parser.PlainTextParser`."""

from __future__ import annotations

import pytest

from parser.text_parser import PlainTextParser


@pytest.fixture
def parser() -> PlainTextParser:
    return PlainTextParser()


def test_iso_timestamp_bracket_level_message(parser: PlainTextParser) -> None:
    entry = parser.parse_line(1, "2024-01-01T12:00:00Z [ERROR] disk full")
    assert entry is not None
    assert entry.timestamp == "2024-01-01T12:00:00Z"
    assert entry.level == "ERROR"
    assert entry.message == "disk full"


def test_timestamp_level_logger_colon_message(parser: PlainTextParser) -> None:
    entry = parser.parse_line(2, "2024-01-01 12:00:00 INFO auth.service: user logged in")
    assert entry is not None
    assert entry.timestamp == "2024-01-01 12:00:00"
    assert entry.level == "INFO"
    assert entry.logger == "auth.service"
    assert entry.message == "user logged in"


def test_dashed_layout(parser: PlainTextParser) -> None:
    entry = parser.parse_line(1, "2024-01-01 12:00:00 - db.pool - WARNING - slow query")
    assert entry is not None
    assert entry.timestamp == "2024-01-01 12:00:00"
    assert entry.logger == "db.pool"
    assert entry.level == "WARNING"
    assert entry.message == "slow query"


def test_python_logging_default(parser: PlainTextParser) -> None:
    entry = parser.parse_line(1, "ERROR:root:something broke")
    assert entry is not None
    assert entry.level == "ERROR"
    assert entry.logger == "root"
    assert entry.message == "something broke"


def test_level_only(parser: PlainTextParser) -> None:
    entry = parser.parse_line(1, "DEBUG starting up")
    assert entry is not None
    assert entry.level == "DEBUG"
    assert entry.timestamp is None
    assert entry.message == "starting up"


def test_timestamp_without_level(parser: PlainTextParser) -> None:
    entry = parser.parse_line(1, "2024-01-01T12:00:00Z heartbeat ok")
    assert entry is not None
    assert entry.timestamp == "2024-01-01T12:00:00Z"
    assert entry.level is None
    assert entry.message == "heartbeat ok"


def test_unstructured_line_keeps_whole_message(parser: PlainTextParser) -> None:
    entry = parser.parse_line(7, "just some free-form text without structure")
    assert entry is not None
    assert entry.timestamp is None
    assert entry.level is None
    assert entry.logger is None
    assert entry.message == "just some free-form text without structure"
    assert entry.line_number == 7


def test_level_is_uppercased(parser: PlainTextParser) -> None:
    entry = parser.parse_line(1, "warn something odd happened")
    assert entry is not None
    assert entry.level == "WARN"


def test_raw_is_preserved_verbatim(parser: PlainTextParser) -> None:
    raw = "  2024-01-01T12:00:00Z [INFO] padded  "
    entry = parser.parse_line(1, raw)
    assert entry is not None
    assert entry.raw == raw


def test_confidence_is_low_baseline(parser: PlainTextParser) -> None:
    assert parser.confidence(["anything"]) == pytest.approx(0.1)


def test_confidence_zero_for_empty(parser: PlainTextParser) -> None:
    assert parser.confidence([]) == 0.0
