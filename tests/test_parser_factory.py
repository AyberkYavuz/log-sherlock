"""Unit tests for format detection in :mod:`parser.parser_factory`."""

from __future__ import annotations

from parser.json_parser import JSONLinesParser
from parser.parser_factory import sample_lines, select_parser
from parser.text_parser import PlainTextParser


def test_selects_json_for_json_lines() -> None:
    lines = ['{"a": 1}', '{"b": 2}', '{"c": 3}']
    assert isinstance(select_parser(lines), JSONLinesParser)


def test_selects_text_for_plain_lines() -> None:
    lines = ["INFO starting", "ERROR crashed", "plain text"]
    assert isinstance(select_parser(lines), PlainTextParser)


def test_mostly_text_with_a_little_json_selects_text() -> None:
    # One JSON line out of many => confidence well below the text baseline.
    lines = ['{"a": 1}'] + ["plain line"] * 20
    assert isinstance(select_parser(lines), PlainTextParser)


def test_majority_json_selects_json() -> None:
    lines = ['{"a": 1}'] * 8 + ["plain line", "another plain line"]
    assert isinstance(select_parser(lines), JSONLinesParser)


def test_empty_input_defaults_to_text() -> None:
    assert isinstance(select_parser([]), PlainTextParser)


def test_sample_lines_skips_blanks_and_strips() -> None:
    lines = ["", "  ", "  hello  ", "world"]
    assert sample_lines(lines) == ["hello", "world"]


def test_sample_lines_respects_limit() -> None:
    lines = [f"line {i}" for i in range(100)]
    assert len(sample_lines(lines, limit=10)) == 10
