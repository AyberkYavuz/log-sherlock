"""Unit tests for :class:`parser.text_parser.PlainTextParser`."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from parser.text_parser import PlainTextParser


@pytest.fixture
def parser() -> PlainTextParser:
    return PlainTextParser()


def test_iso_timestamp_bracket_level_message(parser: PlainTextParser) -> None:
    entry = parser.parse_line(1, "2024-01-01T12:00:00Z [ERROR] disk full")
    assert entry is not None
    assert entry["timestamp"] == datetime(2024, 1, 1, 12, 0, 0, tzinfo=timezone.utc)
    assert entry["level"] == "ERROR"
    assert entry["message"] == "disk full"


def test_timestamp_level_logger_colon_message(parser: PlainTextParser) -> None:
    entry = parser.parse_line(2, "2024-01-01 12:00:00 INFO auth.service: user logged in")
    assert entry is not None
    assert entry["timestamp"] == datetime(2024, 1, 1, 12, 0, 0)
    assert entry["level"] == "INFO"
    assert entry["logger"] == "auth.service"
    assert entry["message"] == "user logged in"


def test_dashed_layout(parser: PlainTextParser) -> None:
    entry = parser.parse_line(1, "2024-01-01 12:00:00 - db.pool - WARNING - slow query")
    assert entry is not None
    assert entry["timestamp"] == datetime(2024, 1, 1, 12, 0, 0)
    assert entry["logger"] == "db.pool"
    assert entry["level"] == "WARNING"
    assert entry["message"] == "slow query"


def test_python_logging_default(parser: PlainTextParser) -> None:
    entry = parser.parse_line(1, "ERROR:root:something broke")
    assert entry is not None
    assert entry["level"] == "ERROR"
    assert entry["logger"] == "root"
    assert entry["message"] == "something broke"


def test_level_only(parser: PlainTextParser) -> None:
    entry = parser.parse_line(1, "DEBUG starting up")
    assert entry is not None
    assert entry["level"] == "DEBUG"
    assert entry["timestamp"] is None
    assert entry["message"] == "starting up"


def test_timestamp_without_level(parser: PlainTextParser) -> None:
    entry = parser.parse_line(1, "2024-01-01T12:00:00Z heartbeat ok")
    assert entry is not None
    assert entry["timestamp"] == datetime(2024, 1, 1, 12, 0, 0, tzinfo=timezone.utc)
    assert entry["level"] is None
    assert entry["message"] == "heartbeat ok"


def test_syslog_timestamp(parser: PlainTextParser) -> None:
    entry = parser.parse_line(1, "Jan 10 14:52:31 sshd accepted connection")
    assert entry is not None
    # Syslog has no year -> stdlib default year 1900 (documented limitation).
    assert entry["timestamp"] == datetime(1900, 1, 10, 14, 52, 31)


def test_unstructured_line_keeps_whole_message(parser: PlainTextParser) -> None:
    entry = parser.parse_line(7, "just some free-form text without structure")
    assert entry is not None
    assert entry["timestamp"] is None
    assert entry["level"] is None
    assert entry["logger"] is None
    assert entry["message"] == "just some free-form text without structure"
    assert entry["line_number"] == 7


def test_level_is_uppercased(parser: PlainTextParser) -> None:
    entry = parser.parse_line(1, "warn something odd happened")
    assert entry is not None
    assert entry["level"] == "WARN"


def test_raw_is_preserved_verbatim(parser: PlainTextParser) -> None:
    raw = "  2024-01-01T12:00:00Z [INFO] padded  "
    entry = parser.parse_line(1, raw)
    assert entry is not None
    assert entry["raw"] == raw


def test_confidence_is_low_baseline(parser: PlainTextParser) -> None:
    assert parser.confidence(["anything"]) == pytest.approx(0.1)


def test_confidence_zero_for_empty(parser: PlainTextParser) -> None:
    assert parser.confidence([]) == 0.0


# ---------------------------------------------------------------------------
# Spring Boot pattern
# ---------------------------------------------------------------------------


def test_spring_boot_full_extraction(parser: PlainTextParser) -> None:
    line = (
        "2026-07-22 10:15:33.210 ERROR 12345 --- [nio-8080-exec-2] "
        "c.logsherlock.repository.OrderRepository : "
        "Database timeout while fetching order id=1024"
    )
    entry = parser.parse_line(1, line)
    assert entry is not None
    assert entry["timestamp"] == datetime(2026, 7, 22, 10, 15, 33, 210000)
    assert entry["level"] == "ERROR"
    assert entry["logger"] == "c.logsherlock.repository.OrderRepository"
    assert entry["message"] == "Database timeout while fetching order id=1024"
    assert entry["metadata"] == {"thread": "nio-8080-exec-2", "pid": 12345}


def test_spring_boot_padded_thread_is_trimmed(parser: PlainTextParser) -> None:
    # Spring aligns the thread column with padding; the value is just the name.
    line = (
        "2026-07-22 10:15:30.123  INFO 12345 --- [           main] "
        "c.logsherlock.Application          : Starting LogSherlockApplication"
    )
    entry = parser.parse_line(1, line)
    assert entry is not None
    assert entry["level"] == "INFO"
    assert entry["logger"] == "c.logsherlock.Application"
    assert entry["message"] == "Starting LogSherlockApplication"
    assert entry["metadata"] == {"thread": "main", "pid": 12345}


def test_spring_boot_pid_is_int(parser: PlainTextParser) -> None:
    line = (
        "2026-07-22 10:15:32.002  WARN 12345 --- [nio-8080-exec-1] "
        "c.logsherlock.service.OrderService : Slow query"
    )
    entry = parser.parse_line(1, line)
    assert entry is not None
    assert entry["metadata"]["pid"] == 12345
    assert isinstance(entry["metadata"]["pid"], int)


# ---------------------------------------------------------------------------
# PostgreSQL pattern
# ---------------------------------------------------------------------------


def test_postgres_error_extraction(parser: PlainTextParser) -> None:
    entry = parser.parse_line(1, "2026-07-22 10:16:15 UTC [12408] ERROR:  deadlock detected")
    assert entry is not None
    assert entry["timestamp"] == datetime(2026, 7, 22, 10, 16, 15)
    assert entry["level"] == "ERROR"
    assert entry["logger"] is None
    assert entry["message"] == "deadlock detected"
    assert entry["metadata"] == {"pid": 12408, "timezone": "UTC"}


@pytest.mark.parametrize(
    "severity",
    ["LOG", "DETAIL", "HINT", "STATEMENT", "WARNING", "FATAL", "PANIC"],
)
def test_postgres_severity_levels_recognised(
    parser: PlainTextParser, severity: str
) -> None:
    entry = parser.parse_line(1, f"2026-07-22 10:16:15 UTC [12408] {severity}:  something happened")
    assert entry is not None
    assert entry["level"] == severity
    assert entry["message"] == "something happened"
    assert entry["metadata"] == {"pid": 12408, "timezone": "UTC"}


# ---------------------------------------------------------------------------
# Python logging (must not regress)
# ---------------------------------------------------------------------------


def test_python_logging_logger_and_message(parser: PlainTextParser) -> None:
    entry = parser.parse_line(1, "INFO:root:Processing order 42")
    assert entry is not None
    assert entry["level"] == "INFO"
    assert entry["logger"] == "root"
    assert entry["message"] == "Processing order 42"
    assert entry["metadata"] == {}


# ---------------------------------------------------------------------------
# FastAPI / Uvicorn pattern
# ---------------------------------------------------------------------------


def test_uvicorn_access_line(parser: PlainTextParser) -> None:
    line = 'INFO:     127.0.0.1:53122 - "GET /health HTTP/1.1" 200 OK'
    entry = parser.parse_line(1, line)
    assert entry is not None
    assert entry["level"] == "INFO"
    assert entry["logger"] == "uvicorn.access"
    assert entry["message"] == "GET /health HTTP/1.1 -> 200 OK"
    assert entry["metadata"] == {
        "client_ip": "127.0.0.1",
        "client_port": 53122,
        "method": "GET",
        "path": "/health",
        "status_code": 200,
    }


def test_uvicorn_access_multiword_reason(parser: PlainTextParser) -> None:
    line = 'INFO:     127.0.0.1:53124 - "POST /predict HTTP/1.1" 500 Internal Server Error'
    entry = parser.parse_line(1, line)
    assert entry is not None
    assert entry["message"] == "POST /predict HTTP/1.1 -> 500 Internal Server Error"
    assert entry["metadata"]["status_code"] == 500


def test_uvicorn_level_prefixed_line(parser: PlainTextParser) -> None:
    entry = parser.parse_line(1, "INFO:     Application startup complete.")
    assert entry is not None
    assert entry["level"] == "INFO"
    assert entry["logger"] is None
    assert entry["message"] == "Application startup complete."
    assert entry["metadata"] == {}


def test_uvicorn_timestamped_access_line(parser: PlainTextParser) -> None:
    # Timestamp-fronted access log (logsherlock-benchmarks shape): the leading
    # timestamp is lifted while logger/message/metadata match the bare form.
    line = '2026-07-27 14:02:51 INFO:     127.0.0.1:50439 - "GET /health HTTP/1.1" 200'
    entry = parser.parse_line(1, line)
    assert entry is not None
    assert entry["timestamp"] == datetime(2026, 7, 27, 14, 2, 51)
    assert entry["level"] == "INFO"
    assert entry["logger"] == "uvicorn.access"
    # No reason phrase after the status code here — message ends at the code.
    assert entry["message"] == "GET /health HTTP/1.1 -> 200"
    assert entry["metadata"] == {
        "client_ip": "127.0.0.1",
        "client_port": 50439,
        "method": "GET",
        "path": "/health",
        "status_code": 200,
    }


def test_uvicorn_timestamped_level_prefixed_line(parser: PlainTextParser) -> None:
    line = "2026-07-27 14:02:52 INFO:     Prediction request completed"
    entry = parser.parse_line(1, line)
    assert entry is not None
    assert entry["timestamp"] == datetime(2026, 7, 27, 14, 2, 52)
    assert entry["level"] == "INFO"
    assert entry["logger"] is None
    assert entry["message"] == "Prediction request completed"
    assert entry["metadata"] == {}


def test_uvicorn_timestamped_error_line(parser: PlainTextParser) -> None:
    # ERROR uses wider padding after the colon; the level is still recognised.
    line = "2026-07-27 14:11:26 ERROR:    Model not loaded: sentiment-v1"
    entry = parser.parse_line(1, line)
    assert entry is not None
    assert entry["timestamp"] == datetime(2026, 7, 27, 14, 11, 26)
    assert entry["level"] == "ERROR"
    assert entry["logger"] is None
    assert entry["message"] == "Model not loaded: sentiment-v1"


# ---------------------------------------------------------------------------
# NestJS pattern
# ---------------------------------------------------------------------------


def test_nestjs_error_line(parser: PlainTextParser) -> None:
    line = "[Nest] 19452  - 07/22/2026, 10:15:33 AM   ERROR [OrdersService] Database timeout"
    entry = parser.parse_line(1, line)
    assert entry is not None
    assert entry["timestamp"] == datetime(2026, 7, 22, 10, 15, 33)
    assert entry["level"] == "ERROR"
    assert entry["logger"] == "OrdersService"
    assert entry["message"] == "Database timeout"
    assert entry["metadata"] == {"pid": 19452}


def test_nestjs_log_level(parser: PlainTextParser) -> None:
    line = "[Nest] 19452  - 07/22/2026, 10:15:30 AM     LOG [NestFactory] Starting Nest application..."
    entry = parser.parse_line(1, line)
    assert entry is not None
    assert entry["level"] == "LOG"
    assert entry["logger"] == "NestFactory"
    assert entry["message"] == "Starting Nest application..."


# ---------------------------------------------------------------------------
# SQL Server ERRORLOG pattern
# ---------------------------------------------------------------------------


def test_mssql_error_header(parser: PlainTextParser) -> None:
    entry = parser.parse_line(1, "2026-07-22 10:16:08.73 spid61 Error: 1205, Severity: 13, State: 51.")
    assert entry is not None
    assert entry["timestamp"] == datetime(2026, 7, 22, 10, 16, 8, 730_000)
    assert entry["level"] == "ERROR"
    assert entry["message"] == "Error: 1205, Severity: 13, State: 51."
    assert entry["metadata"] == {
        "spid": 61,
        "error_number": 1205,
        "severity": 13,
        "state": 51,
    }


def test_mssql_spid_line_without_error(parser: PlainTextParser) -> None:
    entry = parser.parse_line(1, "2026-07-22 10:16:04.17 spid57      Login succeeded for user 'sa'.")
    assert entry is not None
    assert entry["level"] is None
    assert entry["message"] == "Login succeeded for user 'sa'."
    assert entry["metadata"] == {"spid": 57}


def test_mssql_server_line(parser: PlainTextParser) -> None:
    entry = parser.parse_line(1, "2026-07-22 10:15:30.14 Server      Microsoft SQL Server 2025 starting.")
    assert entry is not None
    assert entry["level"] is None
    assert entry["logger"] is None
    assert entry["message"] == "Microsoft SQL Server 2025 starting."
    assert entry["metadata"] == {}


# ---------------------------------------------------------------------------
# Generic patterns keep empty metadata (no invented values)
# ---------------------------------------------------------------------------


def test_generic_pattern_has_empty_metadata(parser: PlainTextParser) -> None:
    entry = parser.parse_line(1, "2024-01-01 12:00:00 INFO auth.service: user logged in")
    assert entry is not None
    assert entry["logger"] == "auth.service"
    assert entry["metadata"] == {}
