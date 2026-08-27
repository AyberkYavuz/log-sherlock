"""Unit tests for the Error Analysis Node (``graph_library/error_analysis/``).

Entries are built with :func:`_entry`, which mirrors the ``ParsedLogEntry``
schema the parser emits, so these tests exercise the node against exactly the
shape it receives in the graph. The fixture-driven tests go further and run the
*real* parser over files in ``sample_logs/``, so a change to either half of the
pipeline shows up here.

Documented conventions asserted here:

    * grouping is driven by the masked ``template`` alone — two records that
      differ only in ids, addresses or counts collapse into one signature,
      and two records at different severities never do;
    * a multi-line record (a Python traceback, a JVM stack) is collated into
      the error line that introduced it, never left as free-floating entries
      and never turned into signatures of its own;
    * a payload with no hard errors falls back to warnings and *says so*, and
      the top-25 cap reports what it dropped — neither is silent;
    * the deterministic pass never sets ``is_root_cause_candidate`` or
      ``explanation``; those come only from the LLM, and when the LLM is
      unavailable the counted findings are still published.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Iterator
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import httpx
import pytest

import graph_library.error_analysis.llm_factory
from graph_library import error_analysis
from graph_library.error_analysis import (
    ERROR_SEVERITIES,
    MAX_SIGNATURES_FOR_LLM,
    NO_ERRORS_NOTE,
    SAMPLE_MESSAGE_LIMIT,
    WARNING_SEVERITIES,
    anthropic_supports_temperature,
    build_analysis_prompt,
    build_error_summary,
    collate_message,
    discover_models,
    error_analysis_node,
    get_error_analysis_llm,
    is_continuation,
    is_model_unavailable,
    iter_error_analysis_llms,
    mask_message,
    normalize_mode,
    normalize_provider,
    resolve_model_candidates,
    resolve_model_name,
    select_error_entries,
    structured_output_kwargs,
    supports_temperature,
)
from graph_library.error_analysis.llm_factory import (
    ANTHROPIC_MAX_TOKENS,
    MODEL_FALLBACKS,
    MODEL_TIERS,
    STRUCTURED_OUTPUT_OVERRIDES,
    TEMPERATURE,
    clear_model_discovery_cache,
)
from graph_library.models import LLMErrorAnalysisResult, LLMErrorSignatureEvaluation, ParsedLogEntry
from graph_library.parser.parser_node import parser_node
from tests.mock_local_llm import (
    build_analysis_payload,
    build_completion_chunks,
    build_completion_response,
    extract_signature_ids,
    iter_completion_chunks,
    make_transport,
)

_SAMPLE_DIR = Path(__file__).resolve().parent.parent / "sample_logs"

BASE = datetime(2024, 1, 1, 12, 0, 0, tzinfo=timezone.utc)

EXPECTED_SIGNATURE_KEYS = {
    "signature_id",
    "template",
    "severity",
    "count",
    "first_seen",
    "last_seen",
    "loggers",
    "sample_messages",
    "is_root_cause_candidate",
    "explanation",
}

EXPECTED_SUMMARY_KEYS = {
    "total_errors_analyzed",
    "unique_signatures_found",
    "primary_error_signature_id",
    "signatures",
    "cascading_impact_summary",
}


def _entry(
    line_number: int = 1,
    *,
    message: str = "boom",
    level: str | None = "ERROR",
    logger: str | None = None,
    timestamp: datetime | None = None,
    raw: str | None = None,
) -> ParsedLogEntry:
    """Build a ``ParsedLogEntry`` with the same shape the parser produces."""
    return {
        "line_number": line_number,
        "raw": raw if raw is not None else message,
        "timestamp": timestamp,
        "level": level,
        "logger": logger,
        "message": message,
        "metadata": {},
    }


def _parse_sample(name: str) -> list[ParsedLogEntry]:
    """Run the real parser over a corpus file and return its entries."""
    raw = (_SAMPLE_DIR / name).read_text()
    return parser_node({"raw_logs": raw})["parsed_logs"]


# ===========================================================================
# Parameter masking
# ===========================================================================


@pytest.mark.parametrize(
    ("message", "expected"),
    [
        # IPv4, with and without a port.
        ("Failed connection to 192.168.1.10:5432", "Failed connection to <IP>:<PORT>"),
        ("Connection refused by 10.0.0.1", "Connection refused by <IP>"),
        # IPv6, in each of its forms.
        ("peer fe80::1ff:fe23:4567:890a unreachable", "peer <IP> unreachable"),
        ("bound to ::1 refused", "bound to <IP> refused"),
        ("listen [fe80::1]:8080 failed", "listen [<IP>]:<NUM> failed"),
        ("route 2001:db8::/32 down", "route <IP> down"),
        (
            "peer 2001:0db8:85a3:0000:0000:8a2e:0370:7334 lost",
            "peer <IP> lost",
        ),
        # UUID / GUID.
        (
            "Request 550e8400-e29b-41d4-a716-446655440000 failed",
            "Request <UUID> failed",
        ),
        # Memory address vs. short hex literal — different placeholders.
        ("Segfault at 0x7f8e4c2a1b30 in worker", "Segfault at <ADDR> in worker"),
        ("exited with code 0xFF", "exited with code <HEX>"),
        # Hash digests, length-anchored.
        (f"checksum {'a' * 64} mismatch", "checksum <HEX> mismatch"),
        (f"blob {'b' * 40} missing", "blob <HEX> missing"),
        (f"etag {'c' * 32} stale", "etag <HEX> stale"),
        # Timestamps collapse to a single placeholder, not one per component.
        ("failed at 2026-08-10T16:54:41.865", "failed at <NUM>"),
        ("failed at 2026-08-10 16:54:41", "failed at <NUM>"),
        # Numbers, including ids glued to an identifier by a separator.
        ("Order ORDER-5001 rejected", "Order ORDER-<NUM> rejected"),
        ("retry 3 of 5", "retry <NUM> of <NUM>"),
        ("timeout after 30.5s", "timeout after <NUM>s"),
        ("allocated 512MB", "allocated <NUM>MB"),
        ("processed 1,048,576 records", "processed <NUM> records"),
        # Identity, not parameters: digits fused into a word survive.
        ("sha256 utf8 http2 model v1 ok", "sha256 utf8 http2 model v1 ok"),
        # Whitespace is normalized so formatting cannot split a group.
        ("too    many\n  spaces", "too many spaces"),
    ],
)
def test_mask_message_replaces_variable_tokens(message: str, expected: str) -> None:
    assert mask_message(message) == expected


@pytest.mark.parametrize(
    "message",
    [
        # A "::" scope operator is not an IPv6 address. Stack traces are full
        # of these, and corrupting them would corrupt the templates built from
        # the traces they appear in.
        "std::vector<int> allocation failed",
        "at com.example.Service::run failed",
        "Namespace App::Http::Controllers missing",
        "Ruby constant Foo::Bar::Baz not found",
        "ERROR: connection: refused",
    ],
)
def test_masking_leaves_scope_operators_alone(message: str) -> None:
    assert "<IP>" not in mask_message(message)


def test_masking_collapses_records_differing_only_in_parameters() -> None:
    first = mask_message("Failed connection to 192.168.1.10:5432 after 3 retries")
    second = mask_message("Failed connection to 10.2.9.44:6543 after 17 retries")
    assert first == second == "Failed connection to <IP>:<PORT> after <NUM> retries"


def test_masking_keeps_genuinely_different_errors_apart() -> None:
    assert mask_message("Connection refused by 10.0.0.1") != mask_message(
        "Permission denied for 10.0.0.1"
    )


def test_masking_is_bounded_for_pathological_messages() -> None:
    # A megabyte-long line must not be fingerprinted in full.
    template = mask_message("x" * 10_000)
    assert len(template) <= 2000


# ===========================================================================
# Severity filtering and the warning fallback
# ===========================================================================


def test_only_error_severities_are_selected() -> None:
    logs = [
        _entry(1, level="INFO", message="started"),
        _entry(2, level="ERROR", message="failed"),
        _entry(3, level="DEBUG", message="detail"),
        _entry(4, level="CRITICAL", message="worse"),
    ]
    selected, used_fallback = select_error_entries(logs)

    assert [entry["message"] for entry, _ in selected] == ["failed", "worse"]
    assert used_fallback is False


@pytest.mark.parametrize("level", sorted(ERROR_SEVERITIES))
def test_every_documented_error_severity_is_recognized(level: str) -> None:
    selected, used_fallback = select_error_entries([_entry(1, level=level)])
    assert len(selected) == 1
    assert used_fallback is False


def test_level_matching_is_case_insensitive() -> None:
    selected, _ = select_error_entries([_entry(1, level="error")])
    assert len(selected) == 1


def test_falls_back_to_warnings_only_when_no_hard_errors_exist() -> None:
    logs = [_entry(1, level="INFO"), _entry(2, level="WARN", message="low stock")]
    selected, used_fallback = select_error_entries(logs)

    assert [entry["message"] for entry, _ in selected] == ["low stock"]
    assert used_fallback is True


def test_warnings_are_not_mixed_into_a_payload_that_has_real_errors() -> None:
    # Three real errors must not be diluted by a hundred warnings.
    logs = [_entry(index, level="WARN", message=f"warn {index}") for index in range(100)]
    logs.append(_entry(200, level="ERROR", message="the real failure"))

    selected, used_fallback = select_error_entries(logs)

    assert [entry["message"] for entry, _ in selected] == ["the real failure"]
    assert used_fallback is False


@pytest.mark.parametrize("level", sorted(WARNING_SEVERITIES))
def test_every_documented_warning_severity_is_recognized(level: str) -> None:
    selected, used_fallback = select_error_entries([_entry(1, level=level)])
    assert len(selected) == 1
    assert used_fallback is True


def test_empty_payload_yields_empty_summary_and_a_note() -> None:
    summary, notes = build_error_summary([])

    assert summary == {
        "total_errors_analyzed": 0,
        "unique_signatures_found": 0,
        "primary_error_signature_id": None,
        "signatures": [],
        "cascading_impact_summary": "",
    }
    assert notes == [NO_ERRORS_NOTE]


def test_payload_with_no_errors_or_warnings_yields_empty_summary() -> None:
    summary, notes = build_error_summary(
        [_entry(1, level="INFO"), _entry(2, level="DEBUG")]
    )

    assert summary["signatures"] == []
    assert notes == [NO_ERRORS_NOTE]


# ===========================================================================
# Traceback collation
# ===========================================================================


def test_traceback_lines_are_recognized_as_continuations() -> None:
    for text in (
        "Traceback (most recent call last):",
        'File "/app/api.py", line 30, in predict',
        "^^^^^^^^^^",
        "RuntimeError: Model not loaded",
        "at com.example.Service.run(Service.java:42)",
        "Caused by: java.sql.SQLException",
        "... 12 more",
    ):
        assert is_continuation(_entry(1, message=text, level=None)), text


def test_a_levelled_line_is_never_swallowed_as_a_continuation() -> None:
    # However traceback-shaped it looks, a line the parser gave a level to is a
    # record in its own right.
    assert not is_continuation(
        _entry(1, message="RuntimeError: Model not loaded", level="ERROR")
    )


def test_continuations_are_collated_into_the_preceding_error() -> None:
    logs = [
        _entry(1, level="ERROR", message="Model not loaded: sentiment-v1"),
        _entry(2, level=None, message="Traceback (most recent call last):"),
        _entry(3, level=None, message='File "/app/api.py", line 30, in predict'),
        _entry(4, level=None, message="RuntimeError: Model not loaded: sentiment-v1"),
        _entry(5, level="INFO", message="request finished"),
    ]
    selected, _ = select_error_entries(logs)

    assert len(selected) == 1, "the traceback must not become a record of its own"
    _, message = selected[0]
    assert message.startswith("Model not loaded: sentiment-v1")
    assert "RuntimeError: Model not loaded: sentiment-v1" in message


def test_collation_stops_at_a_distant_line_number() -> None:
    # A traceback-shaped line hundreds of lines later belongs to nothing here.
    logs = [
        _entry(1, level="ERROR", message="first failure"),
        _entry(900, level=None, message="Caused by: java.sql.SQLException"),
    ]
    selected, _ = select_error_entries(logs)

    assert len(selected) == 1
    assert selected[0][1] == "first failure"


def test_collate_message_joins_parts_with_newlines() -> None:
    message = collate_message(
        _entry(1, message="boom"),
        [_entry(2, message="Traceback (most recent call last):", level=None)],
    )
    assert message == "boom\nTraceback (most recent call last):"


# ===========================================================================
# Grouping and signature construction
# ===========================================================================


def test_identical_templates_collapse_into_one_signature() -> None:
    logs = [
        _entry(1, message="Failed connection to 10.0.0.1:5432"),
        _entry(2, message="Failed connection to 10.0.0.2:5432"),
        _entry(3, message="Failed connection to 172.16.4.9:6543"),
    ]
    summary, _ = build_error_summary(logs)

    assert summary["unique_signatures_found"] == 1
    assert summary["total_errors_analyzed"] == 3
    assert summary["signatures"][0]["count"] == 3
    assert summary["signatures"][0]["template"] == "Failed connection to <IP>:<PORT>"


def test_different_severities_never_share_a_signature() -> None:
    logs = [
        _entry(1, level="ERROR", message="disk full"),
        _entry(2, level="CRITICAL", message="disk full"),
    ]
    summary, _ = build_error_summary(logs)

    assert summary["unique_signatures_found"] == 2
    assert {s["severity"] for s in summary["signatures"]} == {"ERROR", "CRITICAL"}


def test_signatures_are_ranked_by_descending_count_and_ided_in_that_order() -> None:
    logs = [_entry(index, message="rare failure") for index in range(1, 3)]
    logs += [_entry(index, message="common failure") for index in range(10, 20)]
    summary, _ = build_error_summary(logs)

    ids = [signature["signature_id"] for signature in summary["signatures"]]
    counts = [signature["count"] for signature in summary["signatures"]]

    assert ids == ["ERR_001", "ERR_002"]
    assert counts == [10, 2]
    assert summary["signatures"][0]["template"] == "common failure"


def test_signature_ids_are_zero_padded_to_three_digits() -> None:
    # Distinguished by a letter, not a digit — a trailing number would be
    # masked and the three would correctly collapse into one signature.
    logs = [
        _entry(index, message=f"failure kind {chr(96 + index)}") for index in range(1, 4)
    ]
    summary, _ = build_error_summary(logs)
    assert [s["signature_id"] for s in summary["signatures"]] == [
        "ERR_001",
        "ERR_002",
        "ERR_003",
    ]


def test_signature_carries_every_documented_key() -> None:
    summary, _ = build_error_summary([_entry(1)])
    assert set(summary["signatures"][0]) == EXPECTED_SIGNATURE_KEYS
    assert set(summary) == EXPECTED_SUMMARY_KEYS


def test_deterministic_pass_leaves_llm_fields_at_their_defaults() -> None:
    summary, _ = build_error_summary([_entry(1)])
    signature = summary["signatures"][0]

    assert signature["is_root_cause_candidate"] is False
    assert signature["explanation"] == ""
    assert summary["primary_error_signature_id"] is None
    assert summary["cascading_impact_summary"] == ""


def test_loggers_are_unique_sorted_and_never_invented() -> None:
    logs = [
        _entry(1, message="boom", logger="payment"),
        _entry(2, message="boom", logger="booking"),
        _entry(3, message="boom", logger="payment"),
        _entry(4, message="boom", logger=None),
    ]
    summary, _ = build_error_summary(logs)

    # No "UNKNOWN" placeholder for the entry that had no logger.
    assert summary["signatures"][0]["loggers"] == ["booking", "payment"]


def test_first_and_last_seen_use_timestamps_when_available() -> None:
    logs = [
        _entry(1, message="boom", timestamp=BASE + timedelta(minutes=5)),
        _entry(2, message="boom", timestamp=BASE),
        _entry(3, message="boom", timestamp=BASE + timedelta(minutes=9)),
    ]
    summary, _ = build_error_summary(logs)
    signature = summary["signatures"][0]

    assert signature["first_seen"] == BASE.isoformat()
    assert signature["last_seen"] == (BASE + timedelta(minutes=9)).isoformat()


def test_first_and_last_seen_fall_back_to_line_numbers() -> None:
    logs = [
        _entry(81, message="boom", timestamp=None),
        _entry(93, message="boom", timestamp=None),
    ]
    summary, _ = build_error_summary(logs)
    signature = summary["signatures"][0]

    assert signature["first_seen"] == "line 81"
    assert signature["last_seen"] == "line 93"


def test_naive_and_aware_timestamps_can_coexist_in_one_group() -> None:
    # A payload can mix a zoned application log with a bare one; comparing them
    # must not raise.
    logs = [
        _entry(1, message="boom", timestamp=datetime(2024, 1, 1, 12, 0)),
        _entry(2, message="boom", timestamp=BASE + timedelta(hours=1)),
    ]
    summary, _ = build_error_summary(logs)
    assert summary["signatures"][0]["count"] == 2


def test_sample_messages_are_unmasked_and_capped() -> None:
    logs = [_entry(index, message=f"Failed to reach 10.0.0.{index}") for index in range(1, 6)]
    summary, _ = build_error_summary(logs)
    samples = summary["signatures"][0]["sample_messages"]

    assert len(samples) == SAMPLE_MESSAGE_LIMIT
    assert samples[0] == "Failed to reach 10.0.0.1"
    assert "<IP>" not in samples[0], "samples must show the real values"


def test_long_sample_messages_are_truncated_visibly() -> None:
    summary, _ = build_error_summary([_entry(1, message="y" * 5000)])
    sample = summary["signatures"][0]["sample_messages"][0]

    assert len(sample) <= 1200
    assert sample.endswith("...")


# ===========================================================================
# Volume management
# ===========================================================================


def test_signatures_sent_to_the_llm_are_capped_and_the_rest_reported() -> None:
    # 30 distinct templates, each with a distinct count so ranking is total.
    logs: list[ParsedLogEntry] = []
    line = 1
    for kind in range(30):
        for _ in range(30 - kind):
            logs.append(_entry(line, message=f"failure kind {chr(97 + kind)}"))
            line += 1

    summary, notes = build_error_summary(logs)

    assert len(summary["signatures"]) == MAX_SIGNATURES_FOR_LLM
    # The totals still describe the whole payload, not just what was submitted.
    assert summary["unique_signatures_found"] == 30
    assert summary["total_errors_analyzed"] == len(logs)
    assert any("omitted from LLM analysis" in note for note in notes)
    assert any("top 25 by volume" in note for note in notes)


def test_nothing_is_reported_as_omitted_when_everything_fits() -> None:
    summary, notes = build_error_summary([_entry(1), _entry(2, message="other")])
    assert not any("omitted" in note for note in notes)


def test_warning_fallback_is_stated_in_the_notes() -> None:
    _, notes = build_error_summary([_entry(1, level="WARN", message="low stock")])
    assert any("WARN/WARNING entries were analyzed instead" in note for note in notes)


def test_error_path_does_not_claim_a_warning_fallback() -> None:
    _, notes = build_error_summary([_entry(1, level="ERROR")])
    assert not any("WARN/WARNING" in note for note in notes)
    assert any("error-level" in note for note in notes)


def test_fingerprinting_is_deterministic() -> None:
    logs = [
        _entry(1, message="Failed connection to 10.0.0.1:5432"),
        _entry(2, message="Order ORDER-5001 rejected"),
        _entry(3, message="Failed connection to 10.0.0.9:5432"),
    ]
    assert build_error_summary(logs) == build_error_summary(logs)


# ===========================================================================
# Fixture: Python / ML stack traces (fastapi_recovery.log)
# ===========================================================================


def test_fastapi_recovery_collates_tracebacks_into_one_signature() -> None:
    summary, notes = build_error_summary(_parse_sample("fastapi_recovery.log"))

    # Every ERROR in this file is the same failure repeated; the interleaved
    # traceback lines must not inflate the signature count.
    assert summary["unique_signatures_found"] == 1
    signature = summary["signatures"][0]
    assert signature["severity"] == "ERROR"
    assert signature["count"] == summary["total_errors_analyzed"] > 1
    assert signature["template"].startswith("Model not loaded: sentiment-v1")


def test_fastapi_recovery_template_retains_the_exception_type() -> None:
    # The terminal traceback line is the most diagnostic part of the record —
    # collation exists so it reaches the LLM attached to its error.
    summary, _ = build_error_summary(_parse_sample("fastapi_recovery.log"))
    template = summary["signatures"][0]["template"]

    assert "Traceback (most recent call last):" in template
    assert "RuntimeError: Model not loaded: sentiment-v1" in template


def test_fastapi_recovery_masks_traceback_line_numbers() -> None:
    summary, _ = build_error_summary(_parse_sample("fastapi_recovery.log"))
    template = summary["signatures"][0]["template"]

    # "line 30" / "line 46" are the variable part of a frame reference.
    assert "line <NUM>" in template
    assert "line 30" not in template


def test_fastapi_recovery_samples_keep_the_real_traceback() -> None:
    summary, _ = build_error_summary(_parse_sample("fastapi_recovery.log"))
    sample = summary["signatures"][0]["sample_messages"][0]

    assert "Traceback (most recent call last):" in sample
    assert "line 30" in sample, "samples are unmasked"


def test_fastapi_recovery_signature_is_timestamped() -> None:
    summary, _ = build_error_summary(_parse_sample("fastapi_recovery.log"))
    signature = summary["signatures"][0]

    assert signature["first_seen"] is not None
    assert signature["first_seen"].startswith("2026-07-27T")
    assert signature["last_seen"] >= signature["first_seen"]


def test_dynamic_memory_addresses_are_masked_in_a_python_traceback() -> None:
    # fastapi_recovery.log carries no pointer values, so the address rule is
    # asserted on the traceback shape it *would* produce.
    logs = [
        _entry(1, level="ERROR", message="Segmentation fault in worker"),
        _entry(2, level=None, message="Traceback (most recent call last):"),
        _entry(
            3,
            level=None,
            message='File "/app/infer.py", line 88, in run',
        ),
        _entry(
            4,
            level=None,
            message="MemoryError: cannot allocate tensor at 0x7f8e4c2a1b30",
        ),
    ]
    other = [
        _entry(10, level="ERROR", message="Segmentation fault in worker"),
        _entry(11, level=None, message="Traceback (most recent call last):"),
        _entry(12, level=None, message='File "/app/infer.py", line 88, in run'),
        _entry(
            13,
            level=None,
            message="MemoryError: cannot allocate tensor at 0x7fa19b004c80",
        ),
    ]
    summary, _ = build_error_summary(logs + other)

    # Two crashes at different addresses are one failure class.
    assert summary["unique_signatures_found"] == 1
    assert "<ADDR>" in summary["signatures"][0]["template"]
    assert summary["signatures"][0]["count"] == 2


# ===========================================================================
# Fixture: Node.js / JSON Pino (typescript_pino_recovery.log)
# ===========================================================================


def test_pino_recovery_extracts_json_levels_and_messages() -> None:
    logs = _parse_sample("typescript_pino_recovery.log")
    summary, _ = build_error_summary(logs)

    # Pino writes numeric levels (50 = error); the parser normalizes them, and
    # this node must see the normalized spelling.
    assert summary["total_errors_analyzed"] > 0
    assert {s["severity"] for s in summary["signatures"]} == {"ERROR"}

    templates = {s["template"] for s in summary["signatures"]}
    # Grouping keys off the JSON "msg" field, not the raw JSON line — otherwise
    # every record would be unique thanks to its reqId.
    assert "Payment provider unavailable" in templates
    assert not any(template.startswith("{") for template in templates)


def test_pino_recovery_collapses_per_request_noise() -> None:
    logs = _parse_sample("typescript_pino_recovery.log")
    summary, _ = build_error_summary(logs)

    # Each error line carries a unique reqId/bookingId; without masking and
    # grouping there would be one signature per request.
    assert summary["unique_signatures_found"] < summary["total_errors_analyzed"] / 10
    assert all("<UUID>" not in s["template"] for s in summary["signatures"]), (
        "the reqId lives in JSON metadata, not the message"
    )


def test_pino_recovery_preserves_the_component_as_a_logger() -> None:
    logs = _parse_sample("typescript_pino_recovery.log")
    summary, _ = build_error_summary(logs)

    payment = next(
        s for s in summary["signatures"] if s["template"] == "Payment provider unavailable"
    )
    assert payment["loggers"] == ["payment"]


def test_pino_recovery_timestamps_are_iso_and_ordered() -> None:
    logs = _parse_sample("typescript_pino_recovery.log")
    summary, _ = build_error_summary(logs)

    for signature in summary["signatures"]:
        assert signature["first_seen"].startswith("2026-07-29T")
        assert signature["last_seen"] >= signature["first_seen"]


# ===========================================================================
# Fixture: Java Spring Boot, high volume (java_spring_boot_large.text.log)
# ===========================================================================


def test_java_large_collapses_high_volume_into_few_templates() -> None:
    logs = _parse_sample("java_spring_boot_large.text.log")
    summary, notes = build_error_summary(logs)

    # ~7.8k lines, 1.5k of them at a failure severity, collapsing to a handful
    # of distinct templates. This is the whole point of the deterministic pass.
    assert summary["total_errors_analyzed"] >= 1000
    assert summary["unique_signatures_found"] <= MAX_SIGNATURES_FOR_LLM
    assert summary["unique_signatures_found"] < 20
    assert len(summary["signatures"]) == summary["unique_signatures_found"]


def test_java_large_uses_the_warning_fallback_and_says_so() -> None:
    logs = _parse_sample("java_spring_boot_large.text.log")
    summary, notes = build_error_summary(logs)

    # This corpus file logs its failures at WARN — there is not a single
    # ERROR/FATAL line — so the fallback must engage and be reported.
    assert {s["severity"] for s in summary["signatures"]} == {"WARN"}
    assert any("WARN/WARNING entries were analyzed instead" in note for note in notes)


def test_java_large_masks_per_order_identifiers() -> None:
    logs = _parse_sample("java_spring_boot_large.text.log")
    summary, _ = build_error_summary(logs)

    templates = [s["template"] for s in summary["signatures"]]
    # Every record names a distinct ORDER-#### / PRODUCT-## / quantity; masking
    # those is what turns hundreds of lines into one signature.
    assert any("<NUM>" in template for template in templates)
    assert all("ORDER-5001" not in template for template in templates)

    top = summary["signatures"][0]
    assert top["count"] > 100
    assert top["loggers"] == ["BenchmarkLoggerImpl"]


def test_java_large_totals_account_for_every_selected_record() -> None:
    logs = _parse_sample("java_spring_boot_large.text.log")
    summary, _ = build_error_summary(logs)

    assert sum(s["count"] for s in summary["signatures"]) == summary[
        "total_errors_analyzed"
    ]


def test_java_large_signatures_are_ranked_by_volume() -> None:
    logs = _parse_sample("java_spring_boot_large.text.log")
    summary, _ = build_error_summary(logs)

    counts = [signature["count"] for signature in summary["signatures"]]
    assert counts == sorted(counts, reverse=True)


# ===========================================================================
# LLM provider factory
# ===========================================================================


@pytest.fixture
def provider_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Give every provider a dummy credential so clients can be constructed."""
    for variable in (
        "OPENAI_API_KEY",
        "ANTHROPIC_API_KEY",
        "GEMINI_API_KEY",
        "DEEPSEEK_API_KEY",
        "LOCAL_LLM_API_KEY",
    ):
        monkeypatch.setenv(variable, "test-key")


@pytest.mark.parametrize(
    ("provider", "expected_class"),
    [
        ("openai", "ChatOpenAI"),
        ("anthropic", "ChatAnthropic"),
        ("gemini", "ChatGoogleGenerativeAI"),
        # DeepSeek and local speak the OpenAI wire protocol, so they are built
        # on the same client with a redirected base_url.
        ("deepseek", "ChatOpenAI"),
        ("local", "ChatOpenAI"),
    ],
)
def test_factory_instantiates_the_right_client_for_each_provider(
    provider: str, expected_class: str, provider_env: None
) -> None:
    pytest.importorskip(
        {
            "anthropic": "langchain_anthropic",
            "gemini": "langchain_google_genai",
        }.get(provider, "langchain_openai")
    )
    llm = get_error_analysis_llm(provider, "standard")
    assert type(llm).__name__ == expected_class


@pytest.mark.parametrize("provider", sorted(MODEL_TIERS))
@pytest.mark.parametrize("mode", ["fast", "standard", "deep"])
def test_factory_resolves_the_documented_model_for_every_tier(
    provider: str, mode: str
) -> None:
    assert resolve_model_name(provider, mode) == MODEL_TIERS[provider][mode]


def test_factory_routing_table_matches_the_specification() -> None:
    assert MODEL_TIERS["openai"] == {
        "fast": "gpt-4o-mini",
        "standard": "gpt-4o",
        "deep": "o3-mini",
    }
    assert MODEL_TIERS["anthropic"] == {
        "fast": "claude-haiku-4-5",
        "standard": "claude-sonnet-5",
        "deep": "claude-opus-5",
    }
    assert MODEL_TIERS["gemini"] == {
        "fast": "gemini-3.6-flash",
        "standard": "gemini-pro-latest",
        "deep": "gemini-pro-latest",
    }
    assert MODEL_TIERS["deepseek"] == {
        "fast": "deepseek-v4-flash",
        "standard": "deepseek-v4-flash",
        "deep": "deepseek-v4-pro",
    }


@pytest.mark.parametrize("provider", ["openai", "deepseek", "local"])
@pytest.mark.parametrize("mode", ["fast", "standard", "deep"])
def test_every_provider_is_deterministic_where_the_model_allows_it(
    provider: str, mode: str, provider_env: None
) -> None:
    # Root-cause attribution must be reproducible; sampling would break that.
    # OpenAI's reasoning models fix sampling server-side and reject the
    # parameter, so there the setting is necessarily absent rather than 0.0.
    llm = get_error_analysis_llm(provider, mode)

    if provider == "openai" and not supports_temperature(resolve_model_name(provider, mode)):
        assert llm.temperature is None
    else:
        assert llm.temperature == TEMPERATURE == 0.0


@pytest.mark.parametrize(
    ("model", "supported"),
    [
        ("gpt-4o", True),
        ("gpt-4o-mini", True),
        ("gpt-4.1", True),
        # The ``o`` series and GPT-5 fix sampling server-side.
        ("o1", False),
        ("o1-preview", False),
        ("o3", False),
        ("o3-mini", False),
        ("o4-mini-2025-04-16", False),
        ("gpt-5", False),
        ("gpt-5-mini", False),
        # Named like a reasoning model but is not one: the family token is
        # ``omni``, not ``o`` followed by digits.
        ("omni-moderation-latest", True),
    ],
)
def test_supports_temperature_classifies_the_model_families(
    model: str, supported: bool
) -> None:
    assert supports_temperature(model) is supported


def test_deep_mode_routes_to_a_model_that_rejects_temperature(
    provider_env: None,
) -> None:
    # If the routing table ever moves ``deep`` onto a non-reasoning model this
    # fails, flagging that the conditional below is no longer exercised.
    assert not supports_temperature(resolve_model_name("openai", "deep"))
    assert get_error_analysis_llm("openai", "deep").temperature is None


def test_an_explicit_temperature_override_still_wins(provider_env: None) -> None:
    assert get_error_analysis_llm("openai", "deep", temperature=0.7).temperature == 0.7


@pytest.mark.parametrize(
    ("mode", "expected"),
    [
        ("fast", "claude-haiku-4-5"),
        ("standard", "claude-sonnet-5"),
        ("deep", "claude-opus-5"),
    ],
)
def test_anthropic_tiers_resolve_to_live_model_identifiers(
    mode: str, expected: str
) -> None:
    # Regression guard for the 404 that took the Anthropic branch out entirely.
    # Anthropic serves no ``-latest`` aliases, and the Claude 3.5 ids this table
    # used to name are retired — both answer with ``404 not_found_error``.
    model = resolve_model_name("anthropic", mode)

    assert model == expected
    assert "-latest" not in model
    assert not model.startswith("claude-3-")


@pytest.mark.parametrize(
    ("model", "supported"),
    [
        # Pre-4.6 generation: still accepts the sampling parameters.
        ("claude-haiku-4-5", True),
        # 4.6 and later removed them; sending one is a 400, not a no-op.
        ("claude-sonnet-5", False),
        ("claude-opus-5", False),
        # Unknown ids default to the safe direction.
        ("claude-some-future-model", False),
    ],
)
def test_anthropic_supports_temperature_classifies_the_generations(
    model: str, supported: bool
) -> None:
    assert anthropic_supports_temperature(model) is supported


@pytest.mark.parametrize("mode", ["fast", "standard", "deep"])
def test_anthropic_only_sends_temperature_where_the_model_accepts_it(
    mode: str, provider_env: None
) -> None:
    pytest.importorskip("langchain_anthropic")
    model = resolve_model_name("anthropic", mode)

    llm = get_error_analysis_llm("anthropic", mode)

    if anthropic_supports_temperature(model):
        assert llm.temperature == TEMPERATURE == 0.0
    else:
        # Omitted rather than zeroed: Claude 4.6+ rejects the parameter, so
        # sending 0.0 would trade the old 404 for a 400.
        assert llm.temperature is None


@pytest.mark.parametrize("mode", ["fast", "standard", "deep"])
def test_anthropic_raises_the_default_output_ceiling(
    mode: str, provider_env: None
) -> None:
    # langchain-anthropic defaults to 1024, which truncates a batched response
    # covering up to MAX_SIGNATURES_FOR_LLM signatures.
    pytest.importorskip("langchain_anthropic")
    assert ANTHROPIC_MAX_TOKENS > 1024
    assert get_error_analysis_llm("anthropic", mode).max_tokens == ANTHROPIC_MAX_TOKENS


@pytest.mark.parametrize(
    ("supplied", "expected"),
    [
        # The misspelling from the bug report, plus the vendor names users
        # reach for instead of the canonical id.
        ("geminni", "gemini"),
        ("google", "gemini"),
        ("google_genai", "gemini"),
        ("google-genai", "gemini"),
        ("Google GenAI", "gemini"),
        ("  GEMINI  ", "gemini"),
        ("claude", "anthropic"),
        ("ChatGPT", "openai"),
        ("deep-seek", "deepseek"),
        # Already canonical, and the not-supplied cases.
        ("openai", "openai"),
        ("", "openai"),
        (None, "openai"),
        # Unmapped names pass through so the eventual error quotes what the
        # caller actually typed.
        ("wingdings", "wingdings"),
    ],
)
def test_normalize_provider_folds_case_padding_and_aliases(
    supplied: str | None, expected: str
) -> None:
    assert normalize_provider(supplied) == expected


@pytest.mark.parametrize(
    ("supplied", "expected"),
    [("FAST", "fast"), ("  deep ", "deep"), ("", "standard"), (None, "standard")],
)
def test_normalize_mode_folds_case_and_padding(
    supplied: str | None, expected: str
) -> None:
    assert normalize_mode(supplied) == expected


def test_a_misspelled_provider_still_resolves_a_model() -> None:
    # The bug: "geminni" reached the factory verbatim and blew up before any
    # model id was involved.
    assert resolve_model_name("geminni", "fast") == MODEL_TIERS["gemini"]["fast"]
    assert resolve_model_name(" GOOGLE ", "FAST") == MODEL_TIERS["gemini"]["fast"]


@pytest.mark.parametrize(
    ("mode", "expected"),
    [
        ("fast", "gemini-3.6-flash"),
        ("standard", "gemini-pro-latest"),
        ("deep", "gemini-pro-latest"),
    ],
)
def test_gemini_tiers_resolve_to_live_model_identifiers(
    mode: str, expected: str
) -> None:
    # Regression guard for the 404. The 1.5 generation is gone outright and 2.5
    # answers "no longer available to new users" — both verified against
    # generateContent with a live key.
    model = resolve_model_name("gemini", mode)

    assert model == expected
    assert not model.startswith(("gemini-1.5", "gemini-2.5"))


@pytest.mark.parametrize("provider", ["gemini", "deepseek"])
@pytest.mark.parametrize("mode", ["fast", "standard", "deep"])
def test_candidates_lead_with_the_tier_and_then_widen(
    provider: str, mode: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Discovery is stubbed out: this asserts the ordering contract, not what
    # any particular key happens to be able to reach.
    monkeypatch.setattr(
        "graph_library.error_analysis.llm_factory.discover_models", lambda *_, **__: ()
    )

    candidates = resolve_model_candidates(provider, mode)

    assert candidates[0] == resolve_model_name(provider, mode)
    assert len(candidates) == len(set(candidates)), "candidates must be de-duplicated"
    for alternate in MODEL_FALLBACKS[provider][mode]:
        assert alternate in candidates


def test_discovered_models_rank_behind_the_verified_fallbacks(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # A listing proves a model exists, not that this key may call it: Gemini
    # lists gemini-2.5-flash and then refuses it at generateContent. So a
    # discovered id must never displace a hand-verified one.
    monkeypatch.setattr(
        "graph_library.error_analysis.llm_factory.discover_models",
        lambda *_, **__: ("gemini-2.5-flash", "gemini-9.9-flash"),
    )

    candidates = resolve_model_candidates("gemini", "fast")
    verified = MODEL_FALLBACKS["gemini"]["fast"]

    assert candidates.index("gemini-2.5-flash") > candidates.index(verified[0])
    # Newer generations first among the discovered tail.
    assert candidates.index("gemini-9.9-flash") < candidates.index("gemini-2.5-flash")


def test_discovery_excludes_models_that_cannot_do_this_job(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "graph_library.error_analysis.llm_factory.discover_models",
        lambda *_, **__: (
            "gemini-2.5-flash-preview-tts",
            "gemini-3.1-flash-image",
            "gemma-4-31b-it",
            "gemini-4.0-flash",
        ),
    )

    candidates = resolve_model_candidates("gemini", "fast")

    assert "gemini-4.0-flash" in candidates
    for unusable in ("gemini-2.5-flash-preview-tts", "gemini-3.1-flash-image", "gemma-4-31b-it"):
        assert unusable not in candidates


def test_discovery_is_cached_and_never_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[str] = []

    def exploding_backend() -> tuple[str, ...]:
        calls.append("called")
        raise RuntimeError("listing endpoint is down")

    monkeypatch.setitem(
        error_analysis.llm_factory._DISCOVERY_BACKENDS, "gemini", exploding_backend
    )
    clear_model_discovery_cache()

    # Swallowed, not raised: discovery only widens an already-usable list.
    assert discover_models("gemini") == ()
    assert discover_models("gemini") == ()
    assert calls == ["called"], "a failed listing must be cached, not retried"


def test_discovery_is_skipped_for_providers_without_a_listing_backend() -> None:
    clear_model_discovery_cache()
    for provider in ("openai", "anthropic", "local"):
        assert discover_models(provider) == ()


@pytest.mark.parametrize(
    ("exc", "recoverable"),
    [
        # The exact shape langchain-google-genai raises: no status attribute,
        # everything in the message.
        (
            RuntimeError(
                "Error calling model 'gemini-2.5-flash' (NOT_FOUND): 404 NOT_FOUND. "
                "{'error': {'code': 404, 'message': 'This model models/gemini-2.5-flash "
                "is no longer available to new users.'}}"
            ),
            True,
        ),
        (RuntimeError("models/gemini-1.5-pro is not found for API version v1beta"), True),
        (RuntimeError("The model `deepseek-v4-flash` does not exist"), True),
        (RuntimeError("model_not_found"), True),
        # Not a model-identity problem: retrying elsewhere burns quota and
        # fails the same way.
        (RuntimeError("401 invalid api key"), False),
        (RuntimeError("429 rate limit exceeded"), False),
        (TimeoutError("read timed out"), False),
    ],
)
def test_is_model_unavailable_only_claims_model_identity_failures(
    exc: Exception, recoverable: bool
) -> None:
    assert is_model_unavailable(exc) is recoverable


def test_is_model_unavailable_unwraps_the_cause_chain() -> None:
    inner = RuntimeError("404 NOT_FOUND")
    outer = RuntimeError("Error calling model")
    outer.__cause__ = inner

    assert is_model_unavailable(outer) is True


def test_is_model_unavailable_survives_a_self_referential_cause() -> None:
    looping = RuntimeError("boom")
    looping.__cause__ = looping

    assert is_model_unavailable(looping) is False


def test_iter_yields_a_client_per_candidate_lazily(
    monkeypatch: pytest.MonkeyPatch, provider_env: None
) -> None:
    built: list[str] = []
    monkeypatch.setattr(
        "graph_library.error_analysis.llm_factory.resolve_model_candidates",
        lambda *_: ["first", "second", "third"],
    )
    monkeypatch.setattr(
        "graph_library.error_analysis.llm_factory.get_error_analysis_llm",
        lambda *_, model=None, **__: built.append(model) or object(),
    )

    iterator = iter_error_analysis_llms("openai", "fast")
    model, _ = next(iterator)

    assert model == "first"
    assert built == ["first"], "a healthy run must build exactly one client"

    assert [name for name, _ in iterator] == ["second", "third"]


def test_an_explicit_anthropic_override_still_wins(provider_env: None) -> None:
    pytest.importorskip("langchain_anthropic")
    llm = get_error_analysis_llm("anthropic", "deep", max_tokens=256, temperature=0.7)

    assert llm.max_tokens == 256
    assert llm.temperature == 0.7


class _CapturingTransport(httpx.BaseTransport):
    """Records the request body and replies with a minimal valid completion."""

    def __init__(self) -> None:
        self.body: dict[str, Any] = {}

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        self.body = json.loads(request.read())
        return httpx.Response(
            200,
            json={
                "id": "chatcmpl-capture",
                "object": "chat.completion",
                "created": 0,
                "model": self.body.get("model", "captured"),
                "choices": [
                    {
                        "index": 0,
                        "message": {
                            "role": "assistant",
                            "content": json.dumps(
                                {
                                    "primary_error_signature_id": None,
                                    "cascading_impact_summary": "captured",
                                    "evaluations": [],
                                }
                            ),
                        },
                        "finish_reason": "stop",
                    }
                ],
            },
            request=request,
        )


@pytest.mark.parametrize(
    ("mode", "expects_temperature"),
    [("fast", True), ("standard", True), ("deep", False)],
)
def test_temperature_reaches_the_wire_only_for_models_that_accept_it(
    mode: str, expects_temperature: bool, provider_env: None
) -> None:
    # Asserted on the serialized request, not on the client object: the live
    # 400 ("Unsupported parameter: 'temperature'") was about what was sent, and
    # a client attribute can be set without being transmitted, or vice versa.
    transport = _CapturingTransport()
    llm = get_error_analysis_llm(
        "openai",
        mode,
        http_client=httpx.Client(transport=transport, base_url="https://api.openai.com/v1"),
    )
    llm.with_structured_output(LLMErrorAnalysisResult).invoke([("human", "hi")])

    assert transport.body["model"] == resolve_model_name("openai", mode)
    assert ("temperature" in transport.body) is expects_temperature
    if expects_temperature:
        assert transport.body["temperature"] == 0.0


def test_deepseek_targets_its_own_endpoint(provider_env: None) -> None:
    llm = get_error_analysis_llm("deepseek", "standard")
    assert str(llm.openai_api_base) == "https://api.deepseek.com"
    assert llm.model_name == "deepseek-v4-flash"


def test_deep_mode_selects_the_deepseek_pro_model(provider_env: None) -> None:
    assert get_error_analysis_llm("deepseek", "deep").model_name == "deepseek-v4-pro"


# ===========================================================================
# Structured output per provider
#
# Two live DeepSeek 400s are pinned here. ``json_schema`` — what
# langchain-openai sends by default — is answered with "This response_format
# type is unavailable now"; switching to tool calling then forces the tool,
# which the thinking-mode deepseek-v4 models answer with "Thinking mode does
# not support this tool_choice". Either one costs the node its whole root-cause
# pass.
# ===========================================================================


def test_deepseek_asks_for_an_unforced_tool_call() -> None:
    expected = {"method": "function_calling", "tool_choice": "auto"}

    assert structured_output_kwargs("deepseek") == expected
    # Aliases fold first, so a user who typed "deep-seek" gets the same fix.
    assert structured_output_kwargs("Deep-Seek") == expected


def test_the_override_table_is_never_handed_out_to_be_mutated() -> None:
    kwargs = structured_output_kwargs("deepseek")
    kwargs["method"] = "json_mode"

    assert STRUCTURED_OUTPUT_OVERRIDES["deepseek"]["method"] == "function_calling"


@pytest.mark.parametrize("provider", ["openai", "anthropic", "gemini", "local"])
def test_other_providers_keep_their_own_structured_output_default(
    provider: str,
) -> None:
    # Empty, not ``{"method": "json_schema"}``: each LangChain integration picks
    # its own default and this module has no reason to freeze that choice.
    assert structured_output_kwargs(provider) == {}


def test_only_documented_exceptions_override_structured_output() -> None:
    assert set(STRUCTURED_OUTPUT_OVERRIDES) == {"deepseek"}


def test_deepseek_sends_an_unforced_tool_and_no_response_format(
    provider_env: None,
) -> None:
    # Asserted on the serialized request because that is where both live 400s
    # came from: the schema must travel as a tool definition, the tool must not
    # be forced, and ``response_format`` must not appear at all.
    transport = _CapturingTransport()
    llm = get_error_analysis_llm(
        "deepseek",
        "standard",
        http_client=httpx.Client(
            transport=transport, base_url="https://api.deepseek.com"
        ),
    )
    llm.with_structured_output(
        LLMErrorAnalysisResult, **structured_output_kwargs("deepseek")
    ).invoke([("human", "hi")])

    assert "response_format" not in transport.body
    assert [tool["function"]["name"] for tool in transport.body["tools"]] == [
        "LLMErrorAnalysisResult"
    ]
    assert transport.body["tool_choice"] == "auto"


def test_openai_still_sends_a_json_schema_response_format(provider_env: None) -> None:
    # The counterpart guard: the DeepSeek fix must not have been applied
    # globally, since json_schema is the stricter contract where it works.
    transport = _CapturingTransport()
    llm = get_error_analysis_llm(
        "openai",
        "standard",
        http_client=httpx.Client(
            transport=transport, base_url="https://api.openai.com/v1"
        ),
    )
    llm.with_structured_output(
        LLMErrorAnalysisResult, **structured_output_kwargs("openai")
    ).invoke([("human", "hi")])

    assert transport.body["response_format"]["type"] == "json_schema"


def test_local_provider_reads_its_endpoint_from_the_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("LOCAL_LLM_BASE_URL", "http://192.168.1.50:9000/v1")
    monkeypatch.setenv("LOCAL_LLM_MODEL_NAME", "qwen2.5-coder-32b")
    monkeypatch.setenv("LOCAL_LLM_API_KEY", "local-secret")

    llm = get_error_analysis_llm("local", "standard")

    assert llm.model_name == "qwen2.5-coder-32b"
    assert str(llm.openai_api_base) == "http://192.168.1.50:9000/v1"


def test_local_provider_falls_back_to_documented_defaults(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    for variable in ("LOCAL_LLM_BASE_URL", "LOCAL_LLM_MODEL_NAME", "LOCAL_LLM_API_KEY"):
        monkeypatch.delenv(variable, raising=False)

    llm = get_error_analysis_llm("local", "fast")

    assert llm.model_name == "llama-3.3-70b-instruct"
    assert str(llm.openai_api_base) == "http://127.0.0.1:8000/v1"


def test_factory_rejects_an_unknown_provider() -> None:
    with pytest.raises(ValueError, match="Unsupported LLM provider"):
        get_error_analysis_llm("cohere", "standard")


def test_factory_rejects_an_unknown_mode() -> None:
    with pytest.raises(ValueError, match="Unsupported analysis mode"):
        get_error_analysis_llm("openai", "turbo")


# ===========================================================================
# Prompt construction
# ===========================================================================


def test_prompt_carries_every_signature_and_its_counts() -> None:
    summary, _ = build_error_summary(
        [_entry(1, message="a"), _entry(2, message="a"), _entry(3, message="b")]
    )
    prompt = build_analysis_prompt(summary["signatures"], application_name="checkout")

    assert "checkout" in prompt
    for signature in summary["signatures"]:
        assert signature["signature_id"] in prompt
        assert signature["template"] in prompt


def test_prompt_omits_the_fields_the_model_is_meant_to_produce() -> None:
    summary, _ = build_error_summary([_entry(1)])
    prompt = build_analysis_prompt(summary["signatures"])

    # Showing the model empty defaults would only invite it to echo them back.
    assert "is_root_cause_candidate" not in prompt
    assert "explanation" not in prompt


def test_prompt_survives_signatures_without_timestamps() -> None:
    summary, _ = build_error_summary([_entry(1, timestamp=None)])
    assert "line 1" in build_analysis_prompt(summary["signatures"])


# ===========================================================================
# Node behaviour with a stubbed model
# ===========================================================================


class _FakeStructuredLLM:
    """Stands in for ``llm.with_structured_output(...)``."""

    def __init__(self, result: Any, recorder: list[Any] | None = None) -> None:
        self._result = result
        self._recorder = recorder if recorder is not None else []

    def invoke(self, messages: Any) -> Any:
        self._recorder.append(messages)
        if isinstance(self._result, Exception):
            raise self._result
        return self._result


class _FakeLLM:
    """Stands in for the chat model the factory returns."""

    def __init__(self, result: Any, recorder: list[Any] | None = None) -> None:
        self._result = result
        self.recorder = recorder if recorder is not None else []
        self.structured_schema: Any = None
        self.structured_kwargs: dict[str, Any] = {}

    def with_structured_output(self, schema: Any, **kwargs: Any) -> _FakeStructuredLLM:
        self.structured_schema = schema
        self.structured_kwargs = kwargs
        return _FakeStructuredLLM(self._result, self.recorder)


def _install_fake_llm(
    monkeypatch: pytest.MonkeyPatch, result: Any
) -> tuple[_FakeLLM, list[tuple[str, str]]]:
    """Point the node at a fake model and record the (provider, mode) it asked for."""
    calls: list[tuple[str, str]] = []
    fake = _FakeLLM(result)

    def factory(
        provider: str = "openai", mode: str = "standard", **_: Any
    ) -> Iterator[tuple[str, _FakeLLM]]:
        calls.append((provider, mode))
        yield "fake-model", fake

    monkeypatch.setattr("graph_library.error_analysis.node.iter_error_analysis_llms", factory)
    return fake, calls


def _result(
    *,
    primary: str | None = "ERR_001",
    evaluations: list[tuple[str, bool, str]] | None = None,
    summary: str = "The primary failure cascaded downstream.",
) -> LLMErrorAnalysisResult:
    return LLMErrorAnalysisResult(
        primary_error_signature_id=primary,
        cascading_impact_summary=summary,
        evaluations=[
            LLMErrorSignatureEvaluation(
                signature_id=signature_id,
                is_root_cause_candidate=is_root,
                explanation=explanation,
            )
            for signature_id, is_root, explanation in (evaluations or [])
        ],
    )


def test_node_merges_llm_evaluations_into_the_signatures(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    logs = [
        _entry(1, message="Payment provider unavailable"),
        _entry(2, message="Booking request failed"),
        _entry(3, message="Booking request failed"),
    ]
    _install_fake_llm(
        monkeypatch,
        _result(
            primary="ERR_002",
            evaluations=[
                ("ERR_001", False, "Downstream effect of the provider outage."),
                ("ERR_002", True, "The payment provider stopped responding."),
            ],
        ),
    )

    delta = error_analysis_node({"parsed_logs": logs})
    summary = delta["error_summary"]

    assert delta["completed_stages"] == ["error_analysis"]
    assert summary["primary_error_signature_id"] == "ERR_002"
    assert summary["cascading_impact_summary"] == (
        "The primary failure cascaded downstream."
    )

    by_id = {s["signature_id"]: s for s in summary["signatures"]}
    assert by_id["ERR_002"]["is_root_cause_candidate"] is True
    assert by_id["ERR_002"]["explanation"] == "The payment provider stopped responding."
    assert by_id["ERR_001"]["is_root_cause_candidate"] is False


def test_node_never_lets_the_llm_overwrite_deterministic_fields(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    logs = [_entry(1, message="boom"), _entry(2, message="boom")]
    _install_fake_llm(
        monkeypatch, _result(evaluations=[("ERR_001", True, "because")])
    )

    before, _ = build_error_summary(logs)
    after = error_analysis_node({"parsed_logs": logs})["error_summary"]

    assert after["signatures"][0]["count"] == before["signatures"][0]["count"] == 2
    assert after["signatures"][0]["template"] == before["signatures"][0]["template"]
    assert after["total_errors_analyzed"] == before["total_errors_analyzed"]


def test_node_uses_the_schema_and_the_requested_provider(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake, calls = _install_fake_llm(monkeypatch, _result(evaluations=[]))

    error_analysis_node(
        {
            "parsed_logs": [_entry(1)],
            "llm_provider": "anthropic",
            "analysis_mode": "deep",
        }
    )

    assert calls == [("anthropic", "deep")]
    assert fake.structured_schema is LLMErrorAnalysisResult
    assert fake.structured_kwargs == {}


def test_node_requests_an_unforced_tool_call_for_deepseek(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Without this the whole LLM pass is lost to a 400 and the node publishes
    # deterministic signatures only.
    fake, _ = _install_fake_llm(monkeypatch, _result(evaluations=[]))

    error_analysis_node({"parsed_logs": [_entry(1)], "llm_provider": "deep-seek"})

    assert fake.structured_kwargs == {
        "method": "function_calling",
        "tool_choice": "auto",
    }


def test_node_degrades_when_the_model_skips_the_tool_call(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # An unforced tool call may simply not happen; the parser returns None
    # rather than raising, which would otherwise reach the merge as a bare
    # ValidationError against None and take the whole node down.
    _install_fake_llm(monkeypatch, None)

    delta = error_analysis_node(
        {"parsed_logs": [_entry(1)], "llm_provider": "deepseek"}
    )

    assert delta["error_summary"]["signatures"][0]["count"] == 1
    assert delta["completed_stages"] == ["error_analysis"]
    degraded = [
        note
        for note in delta["investigation_notes"]
        if "LLM reasoning unavailable" in note
    ]
    assert len(degraded) == 1
    assert "no structured response" in degraded[0]


class _RaisingLLM:
    """A chat model whose invocation always fails."""

    def __init__(self, error: Exception) -> None:
        self._error = error

    def with_structured_output(self, schema: Any, **_: Any) -> Any:
        return self

    def invoke(self, _messages: Any) -> Any:
        raise self._error


def _install_candidate_chain(
    monkeypatch: pytest.MonkeyPatch, chain: list[tuple[str, Any]]
) -> list[str]:
    """Point the node at an explicit ``(model, client)`` fallback chain."""
    attempted: list[str] = []

    def factory(provider: str = "openai", mode: str = "standard", **_: Any) -> Any:
        for model, llm in chain:
            attempted.append(model)
            yield model, llm

    monkeypatch.setattr("graph_library.error_analysis.node.iter_error_analysis_llms", factory)
    return attempted


def test_node_falls_back_when_the_tier_model_is_retired(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The reported failure: the tier's model 404s. Before, that emptied
    # cascading_impact_summary; now the next candidate answers.
    dead = _RaisingLLM(
        RuntimeError(
            "Error calling model 'gemini-2.5-flash' (NOT_FOUND): 404 NOT_FOUND. "
            "This model is no longer available to new users."
        )
    )
    live = _FakeLLM(_result(evaluations=[("ERR_001", True, "root cause")]))
    attempted = _install_candidate_chain(
        monkeypatch, [("gemini-2.5-flash", dead), ("gemini-3.6-flash", live)]
    )

    delta = error_analysis_node(
        {"parsed_logs": [_entry(1)], "llm_provider": "geminni", "analysis_mode": "fast"}
    )

    assert attempted == ["gemini-2.5-flash", "gemini-3.6-flash"]
    assert delta["error_summary"]["cascading_impact_summary"] != ""
    assert delta["error_summary"]["primary_error_signature_id"] == "ERR_001"


def test_node_records_which_model_actually_answered(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # A silent substitution would make the report unreproducible.
    dead = _RaisingLLM(RuntimeError("404 NOT_FOUND"))
    live = _FakeLLM(_result(evaluations=[]))
    _install_candidate_chain(
        monkeypatch, [("gemini-pro-latest", dead), ("gemini-flash-latest", live)]
    )

    notes = error_analysis_node(
        {"parsed_logs": [_entry(1)], "llm_provider": "gemini", "analysis_mode": "deep"}
    )["investigation_notes"]

    substitution = [note for note in notes if "unavailable" in note]
    assert len(substitution) == 1
    assert "gemini-pro-latest" in substitution[0]
    assert "gemini-flash-latest" in substitution[0]


def test_node_adds_no_note_when_the_first_candidate_works(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_candidate_chain(
        monkeypatch, [("gemini-3.6-flash", _FakeLLM(_result(evaluations=[])))]
    )

    notes = error_analysis_node({"parsed_logs": [_entry(1)]})["investigation_notes"]

    assert not [note for note in notes if "unavailable" in note]


def test_node_does_not_retry_a_credential_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Retrying an expired key against another model burns a request and fails
    # identically, so the chain must stop at the first candidate.
    live = _FakeLLM(_result(evaluations=[]))
    attempted = _install_candidate_chain(
        monkeypatch,
        [
            ("gemini-3.6-flash", _RaisingLLM(RuntimeError("401 invalid api key"))),
            ("gemini-flash-latest", live),
        ],
    )

    delta = error_analysis_node(
        {"parsed_logs": [_entry(1)], "llm_provider": "gemini"}
    )

    assert attempted == ["gemini-3.6-flash"]
    assert delta["error_summary"]["cascading_impact_summary"] == ""
    assert any("401 invalid api key" in note for note in delta["investigation_notes"])


def test_node_reports_the_requested_model_when_every_candidate_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The tier's own failure is the actionable one; the rest are consequences
    # of this node's retrying.
    _install_candidate_chain(
        monkeypatch,
        [
            ("gemini-3.6-flash", _RaisingLLM(RuntimeError("404 gemini-3.6-flash gone"))),
            ("gemini-flash-latest", _RaisingLLM(RuntimeError("404 fallback gone"))),
        ],
    )

    notes = error_analysis_node(
        {"parsed_logs": [_entry(1)], "llm_provider": "gemini"}
    )["investigation_notes"]

    assert any("gemini-3.6-flash gone" in note for note in notes)
    assert not any("fallback gone" in note for note in notes)


def test_node_normalizes_a_misspelled_provider_before_dispatch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, calls = _install_fake_llm(monkeypatch, _result(evaluations=[]))

    error_analysis_node(
        {
            "parsed_logs": [_entry(1)],
            "llm_provider": "  Geminni ",
            "analysis_mode": "FAST",
        }
    )

    assert calls == [("gemini", "fast")]


def test_node_defaults_to_openai_standard(monkeypatch: pytest.MonkeyPatch) -> None:
    _, calls = _install_fake_llm(monkeypatch, _result(evaluations=[]))
    error_analysis_node({"parsed_logs": [_entry(1)]})
    assert calls == [("openai", "standard")]


def test_node_makes_exactly_one_batched_call(monkeypatch: pytest.MonkeyPatch) -> None:
    logs = [_entry(index, message=f"failure {chr(96 + index)}") for index in range(1, 9)]
    fake, _ = _install_fake_llm(monkeypatch, _result(primary=None, evaluations=[]))

    summary = error_analysis_node({"parsed_logs": logs})["error_summary"]

    assert summary["unique_signatures_found"] == 8
    assert len(fake.recorder) == 1, "all signatures must go in one prompt"


def test_node_discards_evaluations_for_unknown_signature_ids(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_fake_llm(
        monkeypatch,
        _result(
            primary="ERR_001",
            evaluations=[
                ("ERR_001", True, "real"),
                ("ERR_999", True, "hallucinated"),
            ],
        ),
    )

    delta = error_analysis_node({"parsed_logs": [_entry(1)]})

    assert len(delta["error_summary"]["signatures"]) == 1
    assert any("ERR_999" in note for note in delta["investigation_notes"])


def test_node_drops_a_primary_id_that_matches_no_signature(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_fake_llm(
        monkeypatch,
        _result(primary="ERR_404", evaluations=[("ERR_001", False, "x")]),
    )

    delta = error_analysis_node({"parsed_logs": [_entry(1)]})

    assert delta["error_summary"]["primary_error_signature_id"] is None
    assert any("ERR_404" in note for note in delta["investigation_notes"])


def test_node_reports_signatures_the_model_did_not_evaluate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    logs = [_entry(1, message="a"), _entry(2, message="b")]
    _install_fake_llm(
        monkeypatch, _result(primary=None, evaluations=[("ERR_001", True, "x")])
    )

    delta = error_analysis_node({"parsed_logs": logs})

    assert any("did not evaluate" in note for note in delta["investigation_notes"])
    unevaluated = delta["error_summary"]["signatures"][1]
    assert unevaluated["explanation"] == ""
    assert unevaluated["is_root_cause_candidate"] is False


def test_node_accepts_a_plain_dict_from_structured_output(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Some providers/methods yield a dict rather than the Pydantic instance.
    _install_fake_llm(
        monkeypatch,
        {
            "primary_error_signature_id": "ERR_001",
            "cascading_impact_summary": "cascade",
            "evaluations": [
                {
                    "signature_id": "ERR_001",
                    "is_root_cause_candidate": True,
                    "explanation": "root",
                }
            ],
        },
    )

    summary = error_analysis_node({"parsed_logs": [_entry(1)]})["error_summary"]

    assert summary["primary_error_signature_id"] == "ERR_001"
    assert summary["signatures"][0]["explanation"] == "root"


def test_node_publishes_deterministic_findings_when_the_llm_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    logs = [_entry(1, message="boom"), _entry(2, message="boom")]
    _install_fake_llm(monkeypatch, RuntimeError("provider unreachable"))

    delta = error_analysis_node({"parsed_logs": logs})
    summary = delta["error_summary"]

    # The counted findings are exactly as accurate as before the call failed.
    assert summary["signatures"][0]["count"] == 2
    assert summary["primary_error_signature_id"] is None
    assert summary["cascading_impact_summary"] == ""
    assert delta["completed_stages"] == ["error_analysis"]
    assert any(
        "LLM reasoning unavailable" in note and "provider unreachable" in note
        for note in delta["investigation_notes"]
    )


def test_node_skips_the_llm_entirely_when_there_are_no_errors(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake, calls = _install_fake_llm(monkeypatch, _result())

    delta = error_analysis_node({"parsed_logs": [_entry(1, level="INFO")]})

    assert calls == [], "no signatures means nothing to reason about"
    assert delta["error_summary"]["signatures"] == []
    assert delta["investigation_notes"] == [NO_ERRORS_NOTE]
    assert delta["completed_stages"] == ["error_analysis"]


def test_node_handles_a_missing_parsed_logs_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_fake_llm(monkeypatch, _result())
    delta = error_analysis_node({})
    assert delta["error_summary"]["total_errors_analyzed"] == 0


def test_node_returns_only_the_keys_it_owns(monkeypatch: pytest.MonkeyPatch) -> None:
    _install_fake_llm(monkeypatch, _result(evaluations=[("ERR_001", True, "x")]))
    delta = error_analysis_node({"parsed_logs": [_entry(1)]})

    # Three sibling nodes write in the same superstep; this node must not touch
    # anything outside its own channels.
    assert set(delta) == {"error_summary", "investigation_notes", "completed_stages"}


def test_node_does_not_mutate_the_incoming_state(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_fake_llm(monkeypatch, _result(evaluations=[("ERR_001", True, "x")]))
    logs = [_entry(1)]
    state = {"parsed_logs": logs}

    error_analysis_node(state)

    assert state == {"parsed_logs": logs}
    assert logs[0]["message"] == "boom"


# ===========================================================================
# The mock local LLM server
# ===========================================================================


def test_mock_extracts_signature_ids_from_the_prompt() -> None:
    messages = [
        {"role": "system", "content": "instructions"},
        {"role": "user", "content": '[{"signature_id": "ERR_001"}, {"signature_id": "ERR_002"}]'},
    ]
    assert extract_signature_ids(messages) == ["ERR_001", "ERR_002"]


def test_mock_payload_conforms_to_the_structured_output_schema() -> None:
    payload = build_analysis_payload(["ERR_001", "ERR_002", "ERR_003"])
    parsed = LLMErrorAnalysisResult.model_validate(payload)

    assert parsed.primary_error_signature_id == "ERR_003"
    assert [e.signature_id for e in parsed.evaluations] == [
        "ERR_001",
        "ERR_002",
        "ERR_003",
    ]
    assert [e.is_root_cause_candidate for e in parsed.evaluations] == [
        False,
        False,
        True,
    ]


def test_mock_handles_a_prompt_with_no_signatures() -> None:
    parsed = LLMErrorAnalysisResult.model_validate(build_analysis_payload([]))
    assert parsed.primary_error_signature_id is None
    assert parsed.evaluations == []


def test_mock_serves_json_content_by_default() -> None:
    response = build_completion_response(
        {"messages": [{"role": "user", "content": '"signature_id": "ERR_001"'}]}
    )
    message = response["choices"][0]["message"]

    assert response["object"] == "chat.completion"
    assert message["content"] is not None
    assert "ERR_001" in message["content"]


def test_mock_serves_a_tool_call_when_the_request_uses_function_calling() -> None:
    response = build_completion_response(
        {
            "messages": [{"role": "user", "content": '"signature_id": "ERR_001"'}],
            "tools": [{"function": {"name": "LLMErrorAnalysisResult"}}],
        }
    )
    message = response["choices"][0]["message"]

    assert response["choices"][0]["finish_reason"] == "tool_calls"
    assert message["tool_calls"][0]["function"]["name"] == "LLMErrorAnalysisResult"
    assert "ERR_001" in message["tool_calls"][0]["function"]["arguments"]


# ===========================================================================
# The mock's streaming transport
#
# LangGraph sets ``stream: true`` on every chat model in a run that subscribes
# to token streaming, which is what LangGraph Studio does. A mock that answers
# such a request with a plain JSON body does not fail cleanly: the client's SSE
# decoder finds no events, the OpenAI SDK asserts on its empty snapshot, and
# the node reports a bare ``AssertionError``. These tests pin the framing.
# ===========================================================================


def _streaming_body(**extra: Any) -> dict[str, Any]:
    return {
        "model": "mock-local-llm",
        "stream": True,
        "messages": [
            {
                "role": "user",
                "content": '[{"signature_id": "ERR_001"}, {"signature_id": "ERR_002"}]',
            }
        ],
        **extra,
    }


def test_streamed_chunks_reassemble_into_the_unstreamed_payload() -> None:
    body = _streaming_body()
    chunks = build_completion_chunks(body)
    content = "".join(
        chunk["choices"][0]["delta"].get("content", "")
        for chunk in chunks
        if chunk["choices"]
    )

    # A client accumulates the deltas; the result must be byte-identical to
    # what the non-streaming branch would have returned.
    assert content == build_completion_response(body)["choices"][0]["message"]["content"]
    assert LLMErrorAnalysisResult.model_validate_json(content).primary_error_signature_id == (
        "ERR_002"
    )


def test_streamed_payload_arrives_in_more_than_one_chunk() -> None:
    # A single-chunk stream would leave the client's accumulator untested.
    chunks = build_completion_chunks(_streaming_body())
    carrying_content = [
        chunk for chunk in chunks if chunk["choices"][0]["delta"].get("content")
    ]
    assert len(carrying_content) > 1


def test_streamed_chunks_open_with_a_role_and_close_with_a_finish_reason() -> None:
    chunks = build_completion_chunks(_streaming_body())

    assert all(chunk["object"] == "chat.completion.chunk" for chunk in chunks)
    assert chunks[0]["choices"][0]["delta"]["role"] == "assistant"
    assert chunks[-1]["choices"][0]["finish_reason"] == "stop"
    # Exactly one terminal chunk: a second would end the stream early.
    assert [c["choices"][0]["finish_reason"] for c in chunks].count("stop") == 1


def test_streamed_tool_call_declares_itself_once_then_streams_arguments() -> None:
    chunks = build_completion_chunks(
        _streaming_body(tools=[{"function": {"name": "LLMErrorAnalysisResult"}}])
    )
    calls = [
        chunk["choices"][0]["delta"]["tool_calls"][0]
        for chunk in chunks
        if chunk["choices"] and "tool_calls" in chunk["choices"][0]["delta"]
    ]

    # The id, type and name arrive once; repeating them would concatenate the
    # name with itself in the client's accumulator.
    assert [call for call in calls if "id" in call] == [calls[0]]
    assert calls[0]["function"]["name"] == "LLMErrorAnalysisResult"
    assert all(call["index"] == 0 for call in calls)

    arguments = "".join(call["function"].get("arguments", "") for call in calls)
    assert LLMErrorAnalysisResult.model_validate_json(arguments)
    assert chunks[-1]["choices"][0]["finish_reason"] == "tool_calls"


def test_stream_is_framed_as_server_sent_events() -> None:
    events = list(iter_completion_chunks(_streaming_body()))

    assert all(event.startswith("data: ") for event in events)
    # A blank line terminates each event; without it the decoder buffers
    # forever and the stream yields nothing.
    assert all(event.endswith("\n\n") for event in events)
    assert events[-1] == "data: [DONE]\n\n"
    for event in events[:-1]:
        json.loads(event.removeprefix("data: "))


def test_usage_is_streamed_only_when_the_client_asks_for_it() -> None:
    without = build_completion_chunks(_streaming_body())
    with_usage = build_completion_chunks(
        _streaming_body(stream_options={"include_usage": True})
    )

    assert not any("usage" in chunk for chunk in without)
    assert with_usage[-1]["usage"]["total_tokens"] == 150
    # The usage chunk carries no choices, so it cannot be mistaken for content.
    assert with_usage[-1]["choices"] == []


# ===========================================================================
# End-to-end against the local provider
# ===========================================================================


@pytest.fixture
def local_llm_client(monkeypatch: pytest.MonkeyPatch) -> httpx.Client:
    """A real ``httpx.Client`` wired to the in-process mock server."""
    monkeypatch.setenv("LOCAL_LLM_BASE_URL", "http://mock/v1")
    monkeypatch.setenv("LOCAL_LLM_API_KEY", "test-key")
    monkeypatch.setenv("LOCAL_LLM_MODEL_NAME", "mock-local-llm")
    return httpx.Client(transport=make_transport(), base_url="http://mock")


def test_local_provider_round_trips_through_the_mock_server(
    local_llm_client: httpx.Client,
) -> None:
    # Exercises the real path: factory -> ChatOpenAI -> HTTP -> structured
    # output parsing, with no network and no API key.
    llm = get_error_analysis_llm("local", "fast", http_client=local_llm_client)
    structured = llm.with_structured_output(LLMErrorAnalysisResult)

    result = structured.invoke(
        [
            ("system", "instructions"),
            ("human", '[{"signature_id": "ERR_001"}, {"signature_id": "ERR_002"}]'),
        ]
    )

    assert isinstance(result, LLMErrorAnalysisResult)
    assert result.primary_error_signature_id == "ERR_002"
    assert len(result.evaluations) == 2


def test_node_runs_end_to_end_against_the_local_provider(
    monkeypatch: pytest.MonkeyPatch, local_llm_client: httpx.Client
) -> None:
    real_factory = iter_error_analysis_llms

    def factory(provider: str = "openai", mode: str = "standard", **kwargs: Any) -> Any:
        return real_factory(provider, mode, http_client=local_llm_client, **kwargs)

    monkeypatch.setattr("graph_library.error_analysis.node.iter_error_analysis_llms", factory)

    logs = _parse_sample("typescript_pino_recovery.log")
    delta = error_analysis_node(
        {
            "parsed_logs": logs,
            "llm_provider": "local",
            "analysis_mode": "fast",
            "application_name": "booking-benchmark",
        }
    )
    summary = delta["error_summary"]

    assert delta["completed_stages"] == ["error_analysis"]
    # The mock nominates the last (lowest-volume) signature, and the node must
    # have merged that verdict onto the matching signature.
    expected_primary = summary["signatures"][-1]["signature_id"]
    assert summary["primary_error_signature_id"] == expected_primary
    assert summary["cascading_impact_summary"] != ""

    by_id = {s["signature_id"]: s for s in summary["signatures"]}
    assert by_id[expected_primary]["is_root_cause_candidate"] is True
    assert all(s["explanation"] != "" for s in summary["signatures"])
    # Deterministic fields survived the round trip untouched.
    assert sum(s["count"] for s in summary["signatures"]) == summary[
        "total_errors_analyzed"
    ]


# ===========================================================================
# End-to-end with streaming enabled — the LangGraph Studio condition
#
# A run that subscribes to token streaming makes LangChain take its ``_stream``
# branch, so the node's ``.invoke()`` becomes a streaming HTTP request without
# the node asking for one. Everything below is the same node, the same mock and
# the same assertions as above, over that second transport.
# ===========================================================================


@pytest.fixture
def streaming_local_llm(
    monkeypatch: pytest.MonkeyPatch, local_llm_client: httpx.Client
) -> None:
    """Point the node at the mock with streaming forced on."""
    real_factory = iter_error_analysis_llms

    def factory(provider: str = "openai", mode: str = "standard", **kwargs: Any) -> Any:
        return real_factory(
            provider, mode, http_client=local_llm_client, streaming=True, **kwargs
        )

    monkeypatch.setattr("graph_library.error_analysis.node.iter_error_analysis_llms", factory)

    # The graph-level test below also runs ``pattern_analysis``, which is a
    # second LLM node reading the same ``llm_provider: "local"`` from state.
    # Left alone it would build a real client against the default local base
    # URL and spend the test's time failing to connect to it. The mock answers
    # every request with the error-analysis payload regardless of the schema it
    # was handed, so pointing this node at it would only trade a connection
    # error for a validation error; a local stub is what keeps the run offline.
    def pattern_factory(*_args: Any, **_kwargs: Any) -> Any:
        yield "mock-local-llm", _StubPatternLLM()

    monkeypatch.setattr(
        "graph_library.pattern_analysis.node.iter_error_analysis_llms", pattern_factory
    )


class _StubPatternLLM:
    """Answers the pattern-analysis schema, so the graph run stays offline."""

    def with_structured_output(self, schema: Any, **_: Any) -> Any:
        return self

    def invoke(self, _messages: Any) -> Any:
        from graph_library.models import PatternAnalysisResult

        return PatternAnalysisResult(behavioral_synthesis="Nothing notable.")


@pytest.mark.parametrize("method", ["json_schema", "function_calling"])
def test_streaming_structured_output_round_trips(
    method: str, local_llm_client: httpx.Client
) -> None:
    llm = get_error_analysis_llm(
        "local", "fast", http_client=local_llm_client, streaming=True
    )
    structured = llm.with_structured_output(LLMErrorAnalysisResult, method=method)

    result = structured.invoke(
        [
            ("system", "instructions"),
            ("human", '[{"signature_id": "ERR_001"}, {"signature_id": "ERR_002"}]'),
        ]
    )

    assert isinstance(result, LLMErrorAnalysisResult)
    assert result.primary_error_signature_id == "ERR_002"
    assert len(result.evaluations) == 2


def test_node_runs_end_to_end_against_a_streaming_local_provider(
    streaming_local_llm: None,
) -> None:
    logs = _parse_sample("typescript_pino_recovery.log")
    delta = error_analysis_node(
        {
            "parsed_logs": logs,
            "llm_provider": "local",
            "analysis_mode": "fast",
            "application_name": "booking-benchmark",
        }
    )
    summary = delta["error_summary"]

    # The failure this guards against degrades silently: the node catches the
    # exception and publishes deterministic-only findings, so the note is the
    # only place it shows.
    assert not any(
        "LLM reasoning unavailable" in note for note in delta["investigation_notes"]
    )
    assert summary["primary_error_signature_id"] == summary["signatures"][-1][
        "signature_id"
    ]
    assert summary["cascading_impact_summary"] != ""
    assert all(s["explanation"] != "" for s in summary["signatures"])


def test_graph_run_with_token_streaming_reaches_the_llm(
    streaming_local_llm: None,
) -> None:
    """Run the compiled graph the way LangGraph Studio runs it.

    Studio subscribes to token streaming on every run, which is what flips the
    chat model onto its streaming branch. Driving the whole graph — rather than
    the node alone — is what makes this a faithful reproduction: nothing in the
    node or the test asks for a stream, the runtime does.
    """
    from graph import compile_graph

    raw_logs = "\n".join(
        [
            '{"level":50,"time":1722250761422,"name":"payment","msg":"Payment provider unavailable"}',
            '{"level":50,"time":1722250761423,"name":"booking","msg":"Booking request failed"}',
            '{"level":50,"time":1722250761424,"name":"booking","msg":"Booking request failed"}',
        ]
    )

    async def run() -> dict[str, Any]:
        final: dict[str, Any] = {}
        async for mode, payload in compile_graph().astream(
            {
                "application_name": "booking",
                "raw_logs": raw_logs,
                "llm_provider": "local",
                "analysis_mode": "fast",
            },
            stream_mode=["values", "messages"],
        ):
            if mode == "values":
                final = payload
        return final

    state = asyncio.run(run())
    summary = state["error_summary"]

    # Scoped to this node's note. Driving the whole graph also runs
    # ``pattern_analysis``, which is a second LLM node with its own degradation
    # note in the same wording; an unqualified substring match would be
    # satisfied — or here, tripped — by a node this test says nothing about.
    assert not any(
        "Error analysis: LLM reasoning unavailable" in note
        for note in state["investigation_notes"]
    )
    assert summary["cascading_impact_summary"] != ""
    assert summary["primary_error_signature_id"] is not None
