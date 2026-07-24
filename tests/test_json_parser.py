"""Unit tests for :class:`parser.json_parser.JSONLinesParser`."""

from __future__ import annotations

import json
from datetime import datetime, timezone

import pytest

from parser.json_parser import JSONLinesParser


@pytest.fixture
def parser() -> JSONLinesParser:
    return JSONLinesParser()


def test_parses_full_record(parser: JSONLinesParser) -> None:
    raw = json.dumps(
        {
            "timestamp": "2024-01-01T12:00:00Z",
            "level": "error",
            "logger": "auth.service",
            "message": "login failed",
            "user_id": 42,
        }
    )
    entry = parser.parse_line(1, raw)
    assert entry is not None
    assert entry["line_number"] == 1
    # Timestamp is normalized to a datetime, not left as a string.
    assert entry["timestamp"] == datetime(2024, 1, 1, 12, 0, 0, tzinfo=timezone.utc)
    assert entry["level"] == "ERROR"  # normalized to upper-case
    assert entry["logger"] == "auth.service"
    assert entry["message"] == "login failed"
    assert entry["metadata"] == {"user_id": 42}


def test_output_is_a_plain_dict(parser: JSONLinesParser) -> None:
    entry = parser.parse_line(1, json.dumps({"message": "hi"}))
    assert isinstance(entry, dict)
    assert set(entry) == {
        "line_number",
        "raw",
        "timestamp",
        "level",
        "logger",
        "message",
        "metadata",
    }


def test_field_aliases_are_recognized(parser: JSONLinesParser) -> None:
    raw = json.dumps(
        {"ts": "2024-01-01 00:00:00", "severity": "warn", "name": "svc", "msg": "hi"}
    )
    entry = parser.parse_line(3, raw)
    assert entry is not None
    assert entry["timestamp"] == datetime(2024, 1, 1, 0, 0, 0)
    assert entry["level"] == "WARN"
    assert entry["logger"] == "svc"
    assert entry["message"] == "hi"
    assert entry["metadata"] == {}


def test_alias_matching_is_case_insensitive(parser: JSONLinesParser) -> None:
    raw = json.dumps(
        {"Timestamp": "2024-01-01 00:00:00", "Level": "info", "Message": "hey"}
    )
    entry = parser.parse_line(1, raw)
    assert entry is not None
    assert entry["timestamp"] == datetime(2024, 1, 1, 0, 0, 0)
    assert entry["level"] == "INFO"
    assert entry["message"] == "hey"


def test_missing_fields_become_none(parser: JSONLinesParser) -> None:
    entry = parser.parse_line(1, json.dumps({"message": "only a message"}))
    assert entry is not None
    assert entry["timestamp"] is None
    assert entry["level"] is None
    assert entry["logger"] is None
    assert entry["message"] == "only a message"


def test_unparseable_timestamp_becomes_none(parser: JSONLinesParser) -> None:
    entry = parser.parse_line(1, json.dumps({"timestamp": "not-a-date", "message": "x"}))
    assert entry is not None
    assert entry["timestamp"] is None


def test_no_message_field_falls_back_to_raw(parser: JSONLinesParser) -> None:
    raw = json.dumps({"level": "info", "code": 200})
    entry = parser.parse_line(1, raw)
    assert entry is not None
    assert entry["message"] == raw
    assert entry["metadata"] == {"code": 200}


def test_malformed_json_returns_none(parser: JSONLinesParser) -> None:
    assert parser.parse_line(1, "{not valid json") is None


def test_json_array_is_not_a_record(parser: JSONLinesParser) -> None:
    assert parser.parse_line(1, "[1, 2, 3]") is None


def test_json_scalar_is_not_a_record(parser: JSONLinesParser) -> None:
    assert parser.parse_line(1, "42") is None


# ---------------------------------------------------------------------------
# Pino / Bunyan numeric levels
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("numeric", "name"),
    [(10, "TRACE"), (20, "DEBUG"), (30, "INFO"), (40, "WARN"), (50, "ERROR"), (60, "FATAL")],
)
def test_pino_numeric_levels_mapped(
    parser: JSONLinesParser, numeric: int, name: str
) -> None:
    entry = parser.parse_line(1, json.dumps({"level": numeric, "msg": "hi"}))
    assert entry is not None
    assert entry["level"] == name


def test_unknown_numeric_level_preserved_verbatim(parser: JSONLinesParser) -> None:
    # A numeric level outside the Pino scale is not guessed at — kept as-is.
    entry = parser.parse_line(1, json.dumps({"level": 7, "msg": "hi"}))
    assert entry is not None
    assert entry["level"] == "7"


def test_named_level_still_normalizes(parser: JSONLinesParser) -> None:
    entry = parser.parse_line(1, json.dumps({"level": "warn", "msg": "hi"}))
    assert entry is not None
    assert entry["level"] == "WARN"


def test_pino_msg_and_metadata(parser: JSONLinesParser) -> None:
    raw = json.dumps(
        {
            "level": 30,
            "time": "2026-07-22T10:15:30.100Z",
            "pid": 4123,
            "hostname": "api-prod-01",
            "reqId": "req-101",
            "msg": "Incoming request",
            "method": "GET",
            "url": "/orders",
        }
    )
    entry = parser.parse_line(1, raw)
    assert entry is not None
    assert entry["level"] == "INFO"
    assert entry["message"] == "Incoming request"
    assert entry["logger"] is None
    assert entry["metadata"] == {
        "pid": 4123,
        "hostname": "api-prod-01",
        "reqId": "req-101",
        "method": "GET",
        "url": "/orders",
    }


def test_confidence_all_json(parser: JSONLinesParser) -> None:
    assert parser.confidence(['{"a": 1}', '{"b": 2}']) == 1.0


def test_confidence_partial(parser: JSONLinesParser) -> None:
    lines = ['{"a": 1}', "plain text", '{"b": 2}', "more text"]
    assert parser.confidence(lines) == 0.5


def test_confidence_empty_sample(parser: JSONLinesParser) -> None:
    assert parser.confidence([]) == 0.0
