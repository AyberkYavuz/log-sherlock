"""Unit tests for the :func:`parser.parser_node.parser_node` graph node."""

from __future__ import annotations

import json

from parser.parser_node import parser_node

EXPECTED_KEYS = {"parsed_logs", "investigation_notes", "completed_stages"}


def _notes_text(result: dict) -> str:
    """Join all investigation notes into one lowercased string for matching."""
    return " ".join(result["investigation_notes"]).lower()


def test_return_shape_and_completed_stage() -> None:
    result = parser_node({"raw_logs": "INFO hello"})
    assert set(result) == EXPECTED_KEYS
    assert result["completed_stages"] == ["parser"]


def test_does_not_touch_other_state_fields() -> None:
    result = parser_node({"raw_logs": "INFO hello", "application_name": "svc"})
    assert "application_name" not in result


def test_empty_string_input() -> None:
    result = parser_node({"raw_logs": ""})
    assert result["parsed_logs"] == []
    assert result["completed_stages"] == ["parser"]
    assert "empty input" in _notes_text(result)


def test_missing_raw_logs_key() -> None:
    result = parser_node({})
    assert result["parsed_logs"] == []
    assert "empty input" in _notes_text(result)


def test_whitespace_only_input() -> None:
    result = parser_node({"raw_logs": "\n   \n\t\n"})
    assert result["parsed_logs"] == []


def test_json_lines_end_to_end() -> None:
    raw = "\n".join(
        [
            json.dumps({"timestamp": "t1", "level": "info", "message": "a"}),
            json.dumps({"timestamp": "t2", "level": "error", "message": "b"}),
        ]
    )
    result = parser_node({"raw_logs": raw})
    logs = result["parsed_logs"]
    assert len(logs) == 2
    assert logs[0]["level"] == "INFO"
    assert logs[1]["message"] == "b"
    assert "json" in _notes_text(result)
    # Entries are plain dicts with the full normalized schema.
    assert set(logs[0]) == {
        "line_number",
        "raw",
        "timestamp",
        "level",
        "logger",
        "message",
        "metadata",
    }


def test_plain_text_end_to_end() -> None:
    raw = "2024-01-01T12:00:00Z [ERROR] boom\n2024-01-01T12:00:01Z [INFO] ok"
    result = parser_node({"raw_logs": raw})
    logs = result["parsed_logs"]
    assert len(logs) == 2
    assert logs[0]["level"] == "ERROR"
    assert "text" in _notes_text(result)


def test_malformed_lines_are_skipped_and_counted() -> None:
    raw = "\n".join(
        [
            json.dumps({"message": "good one"}),
            "{broken json",
            json.dumps({"message": "good two"}),
            "also { not json",
        ]
    )
    result = parser_node({"raw_logs": raw})
    assert len(result["parsed_logs"]) == 2
    assert "skipped 2 malformed lines" in _notes_text(result)


def test_blank_lines_are_not_counted_as_malformed() -> None:
    raw = json.dumps({"message": "hi"}) + "\n\n\n" + json.dumps({"message": "bye"})
    result = parser_node({"raw_logs": raw})
    assert len(result["parsed_logs"]) == 2
    assert "malformed" not in _notes_text(result)


def test_line_numbers_reflect_original_position() -> None:
    # Blank line 2 is skipped, but line numbering of real entries is preserved.
    raw = json.dumps({"message": "first"}) + "\n\n" + json.dumps({"message": "third"})
    result = parser_node({"raw_logs": raw})
    line_numbers = [entry["line_number"] for entry in result["parsed_logs"]]
    assert line_numbers == [1, 3]


def test_missing_timestamps_are_reported() -> None:
    raw = "\n".join(
        [
            json.dumps({"message": "no ts here"}),
            json.dumps({"timestamp": "t", "message": "has ts"}),
        ]
    )
    result = parser_node({"raw_logs": raw})
    assert "1 entry is missing a timestamp" in _notes_text(result)


def test_all_missing_timestamps_plural_note() -> None:
    raw = "\n".join([json.dumps({"message": "a"}), json.dumps({"message": "b"})])
    result = parser_node({"raw_logs": raw})
    assert "2 entries are missing a timestamp" in _notes_text(result)


def test_missing_levels_yield_none() -> None:
    raw = json.dumps({"message": "no level"})
    result = parser_node({"raw_logs": raw})
    assert result["parsed_logs"][0]["level"] is None


def test_mixed_valid_and_invalid_json() -> None:
    raw = "\n".join(
        [
            json.dumps({"level": "info", "message": "valid"}),
            "garbage line that is not json",
            json.dumps({"level": "error", "message": "also valid"}),
        ]
    )
    result = parser_node({"raw_logs": raw})
    # Majority is valid JSON, so JSON parser is chosen and the text line drops.
    assert "json" in _notes_text(result)
    assert len(result["parsed_logs"]) == 2
    assert "skipped 1 malformed line" in _notes_text(result)


def test_determinism_same_input_same_output() -> None:
    raw = "\n".join(json.dumps({"message": f"m{i}"}) for i in range(5))
    assert parser_node({"raw_logs": raw}) == parser_node({"raw_logs": raw})


def test_crlf_line_endings() -> None:
    raw = json.dumps({"message": "a"}) + "\r\n" + json.dumps({"message": "b"})
    result = parser_node({"raw_logs": raw})
    assert len(result["parsed_logs"]) == 2
