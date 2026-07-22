"""Unit tests for :class:`parser.json_parser.JSONLinesParser`."""

from __future__ import annotations

import json

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
    assert entry.line_number == 1
    assert entry.timestamp == "2024-01-01T12:00:00Z"
    assert entry.level == "ERROR"  # normalized to upper-case
    assert entry.logger == "auth.service"
    assert entry.message == "login failed"
    assert entry.metadata == {"user_id": 42}


def test_field_aliases_are_recognized(parser: JSONLinesParser) -> None:
    raw = json.dumps({"ts": "t", "severity": "warn", "name": "svc", "msg": "hi"})
    entry = parser.parse_line(3, raw)
    assert entry is not None
    assert entry.timestamp == "t"
    assert entry.level == "WARN"
    assert entry.logger == "svc"
    assert entry.message == "hi"
    assert entry.metadata == {}


def test_alias_matching_is_case_insensitive(parser: JSONLinesParser) -> None:
    raw = json.dumps({"Timestamp": "t", "Level": "info", "Message": "hey"})
    entry = parser.parse_line(1, raw)
    assert entry is not None
    assert entry.timestamp == "t"
    assert entry.level == "INFO"
    assert entry.message == "hey"


def test_missing_fields_become_none(parser: JSONLinesParser) -> None:
    raw = json.dumps({"message": "only a message"})
    entry = parser.parse_line(1, raw)
    assert entry is not None
    assert entry.timestamp is None
    assert entry.level is None
    assert entry.logger is None
    assert entry.message == "only a message"


def test_no_message_field_falls_back_to_raw(parser: JSONLinesParser) -> None:
    raw = json.dumps({"level": "info", "code": 200})
    entry = parser.parse_line(1, raw)
    assert entry is not None
    assert entry.message == raw
    assert entry.metadata == {"code": 200}


def test_malformed_json_returns_none(parser: JSONLinesParser) -> None:
    assert parser.parse_line(1, "{not valid json") is None


def test_json_array_is_not_a_record(parser: JSONLinesParser) -> None:
    assert parser.parse_line(1, "[1, 2, 3]") is None


def test_json_scalar_is_not_a_record(parser: JSONLinesParser) -> None:
    assert parser.parse_line(1, "42") is None


def test_confidence_all_json(parser: JSONLinesParser) -> None:
    lines = ['{"a": 1}', '{"b": 2}']
    assert parser.confidence(lines) == 1.0


def test_confidence_partial(parser: JSONLinesParser) -> None:
    lines = ['{"a": 1}', "plain text", '{"b": 2}', "more text"]
    assert parser.confidence(lines) == 0.5


def test_confidence_empty_sample(parser: JSONLinesParser) -> None:
    assert parser.confidence([]) == 0.0
