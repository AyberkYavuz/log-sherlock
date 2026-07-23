"""Regression tests over the real-world ``sample_logs/`` corpus.

These files were manually validated through LangGraph Studio and now serve as
the parser's regression suite. The tests run each file end-to-end through
:func:`parser.parser_node.parser_node` and assert on format detection plus the
specific extraction improvements (Spring Boot, PostgreSQL, Python logging),
so a future change that quietly regresses one of these formats fails loudly.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from parser.parser_node import parser_node

_SAMPLE_DIR = Path(__file__).resolve().parent.parent / "sample_logs"


def _run(name: str) -> dict:
    return parser_node({"raw_logs": (_SAMPLE_DIR / name).read_text()})


def _entries(name: str) -> list[dict]:
    return _run(name)["parsed_logs"]


def test_corpus_directory_exists() -> None:
    assert _SAMPLE_DIR.is_dir(), f"missing sample corpus at {_SAMPLE_DIR}"


# ---------------------------------------------------------------------------
# Format detection is stable for every corpus file.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("name", "expected_format"),
    [
        ("java_spring_boot.log", "text"),
        ("postgresql.log", "text"),
        ("python_logs.log", "text"),
        ("simple.log", "text"),
        ("timestamps.log", "text"),
        ("json.log", "json"),
    ],
)
def test_format_detection(name: str, expected_format: str) -> None:
    assert _run(name)["parser_metrics"]["detected_format"] == expected_format


def test_no_file_raises_or_crashes() -> None:
    # Robustness: every corpus file parses without an exception and yields a
    # well-formed metrics dict.
    for path in _SAMPLE_DIR.glob("*.log"):
        metrics = parser_node({"raw_logs": path.read_text()})["parser_metrics"]
        assert (
            metrics["total_lines"]
            == metrics["blank_lines"]
            + metrics["parsed_lines"]
            + metrics["malformed_lines"]
        )


# ---------------------------------------------------------------------------
# Spring Boot: logger + message + metadata (thread, pid).
# ---------------------------------------------------------------------------


def test_spring_boot_error_line_extracted() -> None:
    entry = next(
        e for e in _entries("java_spring_boot.log")
        if e["level"] == "ERROR"
    )
    assert entry["logger"] == "c.logsherlock.repository.OrderRepository"
    assert entry["message"] == "Database timeout while fetching order id=1024"
    assert entry["metadata"] == {"thread": "nio-8080-exec-2", "pid": 12345}


def test_spring_boot_all_structured_lines_have_thread_and_pid() -> None:
    # Every genuine Spring log line (one carrying a level) exposes thread + pid;
    # stack-trace continuation lines have no level and are left as plain text.
    leveled = [e for e in _entries("java_spring_boot.log") if e["level"]]
    assert leveled, "expected Spring Boot lines to be recognised"
    for entry in leveled:
        assert entry["logger"] is not None
        assert entry["metadata"]["pid"] == 12345
        assert "thread" in entry["metadata"]


# ---------------------------------------------------------------------------
# PostgreSQL: severity levels + clean message + metadata (pid, timezone).
# ---------------------------------------------------------------------------


def test_postgres_levels_and_metadata() -> None:
    entries = _entries("postgresql.log")
    by_level: dict[str, dict] = {}
    for entry in entries:
        if entry["level"] and entry["level"] not in by_level:
            by_level[entry["level"]] = entry

    # The distinctive PostgreSQL severities are all recognised.
    for severity in ("LOG", "ERROR", "DETAIL", "HINT", "STATEMENT"):
        assert severity in by_level, f"{severity} not recognised"

    deadlock = by_level["ERROR"]
    assert deadlock["message"] == "deadlock detected"
    assert deadlock["metadata"] == {"pid": 12408, "timezone": "UTC"}

    # Message is clean: no leftover timezone / pid / severity prefix.
    for entry in entries:
        if entry["level"] in {"LOG", "ERROR", "DETAIL", "HINT"}:
            assert not entry["message"].startswith("UTC")
            assert "[12408]" not in entry["message"]


# ---------------------------------------------------------------------------
# Python logging: logger + message.
# ---------------------------------------------------------------------------


def test_python_logging_logger_and_message() -> None:
    entries = _entries("python_logs.log")
    assert entries
    for entry in entries:
        assert entry["logger"] == "root"
        assert entry["level"] in {"INFO", "ERROR"}
        assert entry["message"].startswith(("Processing order", "Database timeout"))
        assert entry["metadata"] == {}
