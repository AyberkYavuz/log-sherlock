"""Regression tests over the real-world ``sample_logs/`` corpus.

These files were manually validated through LangGraph Studio and now serve as
the parser's regression suite. Every ``*.log`` file is run end-to-end through
:func:`parser.parser_node.parser_node`; the tests assert on the facts that
matter — parser selection, parser-metrics invariants, parsed-line counts,
timestamp normalization, and per-ecosystem level / logger / message / metadata
extraction — so a future change that quietly regresses a format fails loudly.

Tests deliberately assert on the *structured* result (levels, metadata dicts,
normalized timestamps), never on the exact wording of investigation notes, so
they stay robust to phrasing changes.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest

from parser.parser_node import parser_node

_SAMPLE_DIR = Path(__file__).resolve().parent.parent / "sample_logs"


def _run(name: str) -> dict:
    return parser_node({"raw_logs": (_SAMPLE_DIR / name).read_text()})


def _entries(name: str) -> list[dict]:
    return _run(name)["parsed_logs"]


def _corpus_files() -> list[str]:
    return sorted(p.name for p in _SAMPLE_DIR.glob("*.log"))


# Expected detected format for every corpus file. This table is the single
# source of truth for parser selection; the coverage test below fails if a new
# sample file is added without an entry here, forcing it into the regression
# suite automatically.
EXPECTED_FORMAT: dict[str, str] = {
    "java_spring_boot.log": "text",
    # Real logsherlock-benchmarks Spring Boot captures: one file per scenario,
    # ``TS LEVEL [thread] logger key=value ... message``.
    "java_spring_boot_NORMAL.text.log": "text",
    "java_spring_boot_INVALID_ORDER.text.log": "text",
    "java_spring_boot_OUT_OF_STOCK.text.log": "text",
    "java_spring_boot_PAYMENT_DECLINED.text.log": "text",
    "java_spring_boot_SHIPPING_DELAY.text.log": "text",
    # The same capture at volume (~7.8k lines), in both encodings.
    "java_spring_boot_large.text.log": "text",
    "java_spring_boot_large.json.log": "json",
    "java_spring_boot_json.log": "json",
    "typescript_pino_first_test.log": "json",
    "typescript_pino_recovery.log": "json",
    "postgresql.log": "text",
    "python_logs.log": "text",
    "simple.log": "text",
    "timestamps.log": "text",
    "fastapi.log": "text",
    # Real logsherlock-benchmarks FastAPI captures: timestamp-fronted Uvicorn.
    "fastapi_normal.log": "text",
    "fastapi_model_not_loaded.log": "text",
    "fastapi_inference_timeout.log": "text",
    "fastapi_recovery.log": "text",
    "nestjs_logger.log": "text",
    "mssql.log": "text",
    "json.log": "json",
    "typescript_pino.log": "json",
    "mixed_formats.log": "json",  # 1 JSON line beats the 0.1 text baseline
    "malformed.log": "json",  # 2 of 3 lines are JSON
}


def test_corpus_directory_exists() -> None:
    assert _SAMPLE_DIR.is_dir(), f"missing sample corpus at {_SAMPLE_DIR}"


def test_every_corpus_file_is_covered() -> None:
    # Adding a sample log without an EXPECTED_FORMAT entry (and thus without
    # regression coverage) fails here.
    assert set(_corpus_files()) == set(EXPECTED_FORMAT)


# ---------------------------------------------------------------------------
# Parser selection + metrics invariants hold for every corpus file.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("name", sorted(EXPECTED_FORMAT))
def test_format_detection(name: str) -> None:
    assert _run(name)["parser_metrics"]["detected_format"] == EXPECTED_FORMAT[name]


@pytest.mark.parametrize("name", sorted(EXPECTED_FORMAT))
def test_metrics_line_counts_are_consistent(name: str) -> None:
    metrics = _run(name)["parser_metrics"]
    raw = (_SAMPLE_DIR / name).read_text()
    total = len(raw.splitlines())
    non_blank = sum(1 for line in raw.splitlines() if line.strip())

    # The documented ParserMetrics invariant.
    assert metrics["total_lines"] == total
    assert (
        metrics["total_lines"]
        == metrics["blank_lines"] + metrics["parsed_lines"] + metrics["malformed_lines"]
    )
    # Every non-blank line is accounted for as either parsed or malformed.
    assert metrics["parsed_lines"] + metrics["malformed_lines"] == non_blank
    # Parsed-line count matches the number of emitted entries.
    assert metrics["parsed_lines"] == len(_run(name)["parsed_logs"])
    # Confidence stays within its documented range.
    assert 0.0 <= metrics["parser_confidence"] <= 1.0


@pytest.mark.parametrize("name", sorted(EXPECTED_FORMAT))
def test_text_files_never_report_malformed_lines(name: str) -> None:
    # The plain-text parser is a total function: every non-blank line yields an
    # entry, so a text-detected file has zero malformed lines.
    if EXPECTED_FORMAT[name] != "text":
        pytest.skip("only meaningful for text-detected files")
    assert _run(name)["parser_metrics"]["malformed_lines"] == 0


@pytest.mark.parametrize("name", sorted(EXPECTED_FORMAT))
def test_parsing_is_deterministic(name: str) -> None:
    assert _run(name) == _run(name)


@pytest.mark.parametrize("name", sorted(EXPECTED_FORMAT))
def test_every_entry_has_full_schema(name: str) -> None:
    for entry in _entries(name):
        assert set(entry) == {
            "line_number",
            "raw",
            "timestamp",
            "level",
            "logger",
            "message",
            "metadata",
        }
        assert isinstance(entry["metadata"], dict)
        assert entry["message"]  # never empty


# ---------------------------------------------------------------------------
# Spring Boot: logger + message + metadata (thread, pid).
# ---------------------------------------------------------------------------


def test_spring_boot_error_line_extracted() -> None:
    entry = next(e for e in _entries("java_spring_boot.log") if e["level"] == "ERROR")
    assert entry["logger"] == "c.logsherlock.repository.OrderRepository"
    assert entry["message"] == "Database timeout while fetching order id=1024"
    assert entry["metadata"] == {"thread": "nio-8080-exec-2", "pid": 12345}


def test_spring_boot_all_structured_lines_have_thread_and_pid() -> None:
    leveled = [e for e in _entries("java_spring_boot.log") if e["level"]]
    assert leveled, "expected Spring Boot lines to be recognised"
    for entry in leveled:
        assert entry["logger"] is not None
        assert entry["metadata"]["pid"] == 12345
        assert "thread" in entry["metadata"]


# ---------------------------------------------------------------------------
# Spring Boot (logsherlock-benchmarks): one file per scenario, each line
# ``TS LEVEL [thread] BenchmarkLoggerImpl key=value ... message``. The logger,
# the thread and every structured field must be lifted, and the trailing
# human-readable text must survive as the message.
# ---------------------------------------------------------------------------


# Scenario file -> (expected record count, expected WARN count).
_BENCHMARK_SPRING_FILES: dict[str, tuple[int, int]] = {
    "java_spring_boot_NORMAL.text.log": (10, 0),
    "java_spring_boot_INVALID_ORDER.text.log": (3, 1),
    "java_spring_boot_OUT_OF_STOCK.text.log": (6, 2),
    "java_spring_boot_PAYMENT_DECLINED.text.log": (9, 2),
    "java_spring_boot_SHIPPING_DELAY.text.log": (11, 1),
}

# Fields every benchmark line carries a value for (``orderId``, ``paymentId``
# and ``shipmentId`` are empty until the workflow reaches that stage).
_ALWAYS_PRESENT_FIELDS = (
    "application",
    "environment",
    "schemaVersion",
    "scenario",
    "reqId",
    "traceId",
    "customerId",
    "productId",
    "service",
    "component",
)


@pytest.mark.parametrize("name", sorted(_BENCHMARK_SPRING_FILES))
def test_benchmark_spring_all_records_parsed(name: str) -> None:
    expected_count, _ = _BENCHMARK_SPRING_FILES[name]
    assert len(_entries(name)) == expected_count


@pytest.mark.parametrize("name", sorted(_BENCHMARK_SPRING_FILES))
def test_benchmark_spring_logger_and_thread_extracted(name: str) -> None:
    for entry in _entries(name):
        assert entry["logger"] == "BenchmarkLoggerImpl"
        assert entry["metadata"]["thread"].startswith("http-nio-8080-exec-")


@pytest.mark.parametrize("name", sorted(_BENCHMARK_SPRING_FILES))
def test_benchmark_spring_structured_fields_extracted(name: str) -> None:
    scenario = name.removeprefix("java_spring_boot_").removesuffix(".text.log")
    for entry in _entries(name):
        metadata = entry["metadata"]
        for key in _ALWAYS_PRESENT_FIELDS:
            assert key in metadata, f"{key} missing from {metadata}"
        assert metadata["application"] == "logsherlock-order-service"
        assert metadata["environment"] == "benchmark"
        assert metadata["scenario"] == scenario


@pytest.mark.parametrize("name", sorted(_BENCHMARK_SPRING_FILES))
def test_benchmark_spring_message_excludes_structured_fields(name: str) -> None:
    for entry in _entries(name):
        message = entry["message"]
        # The message is the human-readable tail only: no field token, no
        # logger, no thread bracket leaking into it.
        for key in _ALWAYS_PRESENT_FIELDS:
            assert f"{key}=" not in message
        assert "BenchmarkLoggerImpl" not in message
        assert "http-nio-8080-exec-" not in message


@pytest.mark.parametrize("name", sorted(_BENCHMARK_SPRING_FILES))
def test_benchmark_spring_timestamps_are_parsed(name: str) -> None:
    for entry in _entries(name):
        assert isinstance(entry["timestamp"], datetime)
        assert entry["timestamp"].year == 2026
        assert entry["timestamp"].month == 8


@pytest.mark.parametrize("name", sorted(_BENCHMARK_SPRING_FILES))
def test_benchmark_spring_levels(name: str) -> None:
    _, expected_warns = _BENCHMARK_SPRING_FILES[name]
    levels = [e["level"] for e in _entries(name)]
    assert set(levels) <= {"INFO", "WARN"}
    assert levels.count("WARN") == expected_warns


def test_benchmark_spring_invalid_order_warning_line() -> None:
    entry = next(
        e for e in _entries("java_spring_boot_INVALID_ORDER.text.log")
        if e["level"] == "WARN"
    )
    assert entry["logger"] == "BenchmarkLoggerImpl"
    assert entry["message"] == (
        "Order ORDER-5001 rejected during validation: "
        "quantity must be greater than zero but was 0"
    )
    assert entry["metadata"] == {
        "thread": "http-nio-8080-exec-3",
        "application": "logsherlock-order-service",
        "environment": "benchmark",
        "schemaVersion": "1",
        "scenario": "INVALID_ORDER",
        "reqId": "REQ-1001",
        "traceId": "TRACE-1001",
        "orderId": "ORDER-5001",
        "customerId": "CUSTOMER-48",
        "productId": "PRODUCT-19",
        "service": "ORDER",
        "component": "VALIDATOR",
    }


def test_benchmark_spring_empty_fields_are_omitted_not_corrupting() -> None:
    # The first line of every scenario has ``orderId=`` empty; the fields that
    # follow it are still extracted.
    first = _entries("java_spring_boot_NORMAL.text.log")[0]
    assert "orderId" not in first["metadata"]
    assert first["metadata"]["customerId"] == "CUSTOMER-48"
    assert first["metadata"]["component"] == "API"
    # A later line, once the order exists, does carry it.
    assert _entries("java_spring_boot_NORMAL.text.log")[1]["metadata"]["orderId"] == (
        "ORDER-5001"
    )


# ---------------------------------------------------------------------------
# PostgreSQL: severity levels + clean message + metadata (pid, timezone).
# ---------------------------------------------------------------------------


def test_postgres_levels_and_metadata() -> None:
    entries = _entries("postgresql.log")
    by_level: dict[str, dict] = {}
    for entry in entries:
        if entry["level"] and entry["level"] not in by_level:
            by_level[entry["level"]] = entry

    for severity in ("LOG", "ERROR", "DETAIL", "HINT", "STATEMENT"):
        assert severity in by_level, f"{severity} not recognised"

    deadlock = by_level["ERROR"]
    assert deadlock["message"] == "deadlock detected"
    assert deadlock["metadata"] == {"pid": 12408, "timezone": "UTC"}

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


# ---------------------------------------------------------------------------
# FastAPI / Uvicorn: access lines, startup/shutdown, exceptions.
# ---------------------------------------------------------------------------


def _access_entries() -> list[dict]:
    return [e for e in _entries("fastapi.log") if e["logger"] == "uvicorn.access"]


def test_fastapi_access_line_full_extraction() -> None:
    entry = next(e for e in _access_entries() if e["metadata"]["method"] == "GET")
    assert entry["level"] == "INFO"
    assert entry["logger"] == "uvicorn.access"
    # Request line and status are combined into a readable message.
    assert entry["message"] == "GET /health HTTP/1.1 -> 200 OK"
    assert entry["metadata"] == {
        "client_ip": "127.0.0.1",
        "client_port": 53122,
        "method": "GET",
        "path": "/health",
        "status_code": 200,
    }
    # Numeric fields are stored as ints, not strings.
    assert isinstance(entry["metadata"]["status_code"], int)
    assert isinstance(entry["metadata"]["client_port"], int)


def test_fastapi_access_error_status() -> None:
    entry = next(e for e in _access_entries() if e["metadata"]["method"] == "POST")
    assert entry["metadata"]["status_code"] == 500
    assert entry["metadata"]["path"] == "/predict"
    assert entry["message"] == "POST /predict HTTP/1.1 -> 500 Internal Server Error"


def test_fastapi_startup_and_shutdown_lines() -> None:
    entries = _entries("fastapi.log")
    messages = {e["message"] for e in entries if e["level"] == "INFO"}
    assert "Started server process [24581]" in messages
    assert "Application startup complete." in messages
    assert "Application shutdown complete." in messages
    # These carry a level but no invented logger / metadata.
    startup = next(e for e in entries if e["message"] == "Application startup complete.")
    assert startup["level"] == "INFO"
    assert startup["logger"] is None
    assert startup["metadata"] == {}


def test_fastapi_exception_line_has_error_level() -> None:
    entry = next(
        e for e in _entries("fastapi.log")
        if e["message"] == "Exception in ASGI application"
    )
    assert entry["level"] == "ERROR"


def test_fastapi_stack_trace_lines_stay_separate() -> None:
    # The traceback is not merged into the ASGI error line; each frame is its
    # own plain-text entry (mirrors how Java stack traces are handled).
    messages = [e["message"] for e in _entries("fastapi.log")]
    assert "Traceback (most recent call last):" in messages
    assert any(m.startswith("RuntimeError: Model not loaded") for m in messages)


# ---------------------------------------------------------------------------
# FastAPI / Uvicorn (logsherlock-benchmarks): timestamp-fronted Uvicorn logs.
# Same Uvicorn layout as fastapi.log, but every line is prefixed with a
# timestamp. Access lines must still yield the uvicorn.access logger + full
# request metadata; app lines must still carry level + message.
# ---------------------------------------------------------------------------


def _timestamped_access_entries() -> list[dict]:
    return [e for e in _entries("fastapi_normal.log") if e["logger"] == "uvicorn.access"]


def test_benchmark_fastapi_timestamp_is_extracted() -> None:
    # Every parsed line carries the leading timestamp (unlike bare fastapi.log).
    entries = _entries("fastapi_normal.log")
    assert entries
    for entry in entries:
        assert isinstance(entry["timestamp"], datetime)
        assert entry["timestamp"].year == 2026


def test_benchmark_fastapi_access_line_full_extraction() -> None:
    entry = next(
        e for e in _timestamped_access_entries()
        if e["metadata"]["method"] == "GET" and e["metadata"]["path"] == "/health"
    )
    assert entry["timestamp"] == datetime(2026, 7, 27, 14, 2, 51)
    assert entry["level"] == "INFO"
    assert entry["logger"] == "uvicorn.access"
    # No reason phrase after the status code in the benchmark access lines.
    assert entry["message"] == "GET /health HTTP/1.1 -> 200"
    assert entry["metadata"] == {
        "client_ip": "127.0.0.1",
        "client_port": 50439,
        "method": "GET",
        "path": "/health",
        "status_code": 200,
    }
    assert isinstance(entry["metadata"]["status_code"], int)
    assert isinstance(entry["metadata"]["client_port"], int)


def test_benchmark_fastapi_app_line_has_level_but_no_logger() -> None:
    entry = next(
        e for e in _entries("fastapi_normal.log")
        if e["message"] == "Application startup complete."
    )
    assert entry["level"] == "INFO"
    assert entry["logger"] is None
    assert entry["metadata"] == {}
    assert isinstance(entry["timestamp"], datetime)


def test_benchmark_fastapi_error_line_extracted() -> None:
    entry = next(
        e for e in _entries("fastapi_model_not_loaded.log")
        if e["level"] == "ERROR"
    )
    assert entry["message"] == "Model not loaded: sentiment-v1"
    assert entry["logger"] is None
    assert isinstance(entry["timestamp"], datetime)


def test_benchmark_fastapi_stack_trace_lines_stay_separate() -> None:
    # As in fastapi.log, traceback frames are their own plain-text entries.
    messages = [e["message"] for e in _entries("fastapi_model_not_loaded.log")]
    assert "Traceback (most recent call last):" in messages
    assert any(m.startswith("RuntimeError: Model not loaded") for m in messages)


# ---------------------------------------------------------------------------
# NestJS: timestamp + pid + logger/component + level + clean message.
# ---------------------------------------------------------------------------


def test_nestjs_structured_lines() -> None:
    structured = [e for e in _entries("nestjs_logger.log") if e["metadata"].get("pid")]
    assert len(structured) == 3
    for entry in structured:
        assert entry["metadata"]["pid"] == 19452
        assert entry["level"] in {"LOG", "ERROR"}
        assert entry["logger"] in {"NestFactory", "InstanceLoader", "OrdersService"}
        assert isinstance(entry["timestamp"], datetime)
        assert entry["timestamp"].year == 2026


def test_nestjs_error_line_extraction() -> None:
    entry = next(e for e in _entries("nestjs_logger.log") if e["level"] == "ERROR")
    assert entry["logger"] == "OrdersService"
    assert entry["message"] == "Database timeout"
    assert entry["metadata"] == {"pid": 19452}


def test_nestjs_timestamp_normalization() -> None:
    first = _entries("nestjs_logger.log")[0]
    assert first["timestamp"] == datetime(2026, 7, 22, 10, 15, 30)


def test_nestjs_stack_trace_stays_separate() -> None:
    messages = [e["message"] for e in _entries("nestjs_logger.log")]
    # The stack frame is a distinct entry, not folded into the error line.
    assert any("OrdersRepository.findAll" in m for m in messages)


# ---------------------------------------------------------------------------
# SQL Server ERRORLOG: timestamp + level + message + metadata (spid, severity,
# state).
# ---------------------------------------------------------------------------


def test_mssql_error_line_metadata_and_level() -> None:
    entry = next(e for e in _entries("mssql.log") if e["level"] == "ERROR")
    assert entry["metadata"]["spid"] == 61
    assert entry["metadata"]["severity"] == 13
    assert entry["metadata"]["state"] == 51
    assert entry["metadata"]["error_number"] == 1205
    assert isinstance(entry["timestamp"], datetime)


def test_mssql_spid_extracted_on_non_error_lines() -> None:
    login = next(
        e for e in _entries("mssql.log") if e["message"].startswith("Login succeeded")
    )
    assert login["metadata"] == {"spid": 57}
    assert login["level"] is None


def test_mssql_server_lines_have_no_spid_or_level() -> None:
    entry = next(
        e for e in _entries("mssql.log")
        if e["message"].startswith("Microsoft SQL Server")
    )
    assert entry["level"] is None
    assert "spid" not in entry["metadata"]


def test_mssql_timestamp_normalization_with_centiseconds() -> None:
    first = _entries("mssql.log")[0]
    assert first["timestamp"] == datetime(2026, 7, 22, 10, 15, 30, 140_000)


# ---------------------------------------------------------------------------
# TypeScript (Pino): numeric level mapping + msg -> message + metadata.
# ---------------------------------------------------------------------------


def test_pino_numeric_levels_mapped_to_names() -> None:
    entries = _entries("typescript_pino.log")
    assert [e["level"] for e in entries] == ["INFO", "ERROR"]


def test_pino_msg_becomes_message() -> None:
    entries = _entries("typescript_pino.log")
    assert entries[0]["message"] == "Incoming request"
    assert entries[1]["message"] == "Request failed"


def test_pino_fields_flow_into_metadata() -> None:
    first = _entries("typescript_pino.log")[0]
    assert first["metadata"]["pid"] == 4123
    assert first["metadata"]["hostname"] == "api-prod-01"
    assert first["metadata"]["reqId"] == "req-101"
    assert first["metadata"]["method"] == "GET"
    assert first["metadata"]["url"] == "/orders"


def test_pino_unknown_nested_fields_are_preserved() -> None:
    # The nested ``err`` object is an unknown key and must survive intact.
    second = _entries("typescript_pino.log")[1]
    assert second["metadata"]["err"] == {"type": "Error", "message": "Database timeout"}


def test_pino_timestamp_is_normalized_and_aware() -> None:
    first = _entries("typescript_pino.log")[0]
    assert first["timestamp"] == datetime(
        2026, 7, 22, 10, 15, 30, 100_000, tzinfo=timezone.utc
    )
