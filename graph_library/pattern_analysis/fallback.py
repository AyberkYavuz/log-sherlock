"""The deterministic half of the Pattern Analysis Node.

This module answers the same question as the LLM pass — *"what about this
system's behaviour is abnormal?"* — with arithmetic only, and it is what the
node publishes when the model cannot be reached. It exists for the same reason
:mod:`graph_library.error_analysis.fingerprint` does: an unreachable provider
should cost an investigation its *interpretation*, not its findings.

What arithmetic can and cannot see is the whole design here. Three of the four
:data:`~graph_library.models.AnomalyCategory` values are decidable from the
inputs without judgement:

    * ``baseline_shift`` — the Timeline Node already located the error onset and
      the recovery, so a shift is a question about which milestones exist;
    * ``volume_spike`` — the peak bucket's error count against the mean of the
      series is a ratio, not an opinion;
    * ``metadata_clustering`` — one value holding most of a distribution is a
      share.

``logger_cascade`` is the exception and is reported conservatively: this pass
can see that component A's errors precede component B's, which is *consistent*
with propagation, and says exactly that rather than claiming causation. The
distinction is the same one the system prompt asks the model to respect, and
the fallback is held to it too.

Every string this module emits is a mechanical restatement of a count it
computed. Given the same statistics and timeline it always produces the same
summary, in the same order, with the same wording.
"""

from __future__ import annotations

from graph_library.models import (
    AnomalySeverity,
    PatternAnalysisResult,
    Statistics,
    SystemAnomaly,
    TimelineEvent,
)

#: Error-share thresholds for the three severity tiers, highest first. A share
#: is the fraction of *all* records at an error level, which is the same
#: denominator ``Statistics.severity.error_ratio`` uses.
SEVERITY_THRESHOLDS: tuple[tuple[float, AnomalySeverity], ...] = (
    (0.25, "critical"),
    (0.05, "warning"),
)

#: How far above the series mean a peak has to sit before it is a spike rather
#: than the top of a plateau. The same multiple the Timeline Node uses for onset
#: detection, so the two passes agree on what "breaking out of the baseline"
#: means.
SPIKE_RATIO = 2.0

#: The share of a metadata distribution one value must hold to count as a
#: concentration. High on purpose: metadata keys reach the distribution only
#: when they have at most 21 distinct values, so a merely-uneven split is the
#: normal case and not worth reporting.
DOMINANCE_RATIO = 0.7

#: How many metadata concentrations to report. Ordered by share, so the cap
#: drops the weakest.
MAX_METADATA_INSIGHTS = 5

#: How many loggers to name in a cascade. The timeline itself reports at most
#: three per bucket, and a sentence naming a dozen components is not a finding.
MAX_CASCADE_LOGGERS = 4


def severity_for_error_share(error_ratio: float) -> AnomalySeverity:
    """Map an error share onto a severity tier.

    Args:
        error_ratio: Errors as a fraction of all records, from
            ``Statistics.severity.error_ratio``.

    Returns:
        The highest tier whose threshold the share meets, or ``"info"``.
    """
    for threshold, severity in SEVERITY_THRESHOLDS:
        if error_ratio >= threshold:
            return severity
    return "info"


def _milestones(timeline: list[TimelineEvent]) -> dict[str, TimelineEvent]:
    """Index the milestone events by kind, keeping the first of each."""
    found: dict[str, TimelineEvent] = {}
    for event in timeline:
        if event.get("event_type") != "milestone":
            continue
        kind = event.get("milestone_kind")
        if kind and kind not in found:
            found[kind] = event
    return found


def _buckets(timeline: list[TimelineEvent]) -> list[TimelineEvent]:
    """The bucket events, in the order the Timeline Node emitted them."""
    return [event for event in timeline if event.get("event_type") == "bucket"]


def _error_logger_sequence(buckets: list[TimelineEvent]) -> list[str]:
    """Loggers named in error-carrying buckets, in first-appearance order.

    ``top_loggers`` is already ranked by error volume within its bucket, so
    reading them in bucket order gives the sequence in which components started
    failing — which is the only cascade evidence arithmetic has access to.
    """
    ordered: list[str] = []
    for bucket in buckets:
        if not int(bucket.get("error_count") or 0):
            continue
        for logger in bucket.get("top_loggers") or []:
            if logger not in ordered:
                ordered.append(logger)
    return ordered


def _baseline_shift(
    milestones: dict[str, TimelineEvent], severity: AnomalySeverity
) -> SystemAnomaly | None:
    """Report the onset, and whether the system came back from it."""
    onset = milestones.get("error_onset")
    if onset is None:
        return None

    recovery = milestones.get("recovery_onset")
    if recovery is None:
        description = (
            "Error volume broke out of its prior baseline at "
            f"{onset.get('timestamp')} and had not returned to it by the end of "
            "the window."
        )
    else:
        description = (
            "Error volume broke out of its prior baseline at "
            f"{onset.get('timestamp')} and returned to it at "
            f"{recovery.get('timestamp')}."
        )

    return SystemAnomaly(
        category="baseline_shift",
        # An unrecovered shift is worse than the dataset-wide share suggests:
        # the window ended while the system was still degraded.
        severity="critical" if recovery is None and severity != "info" else severity,
        description=description,
        affected_loggers=list(onset.get("top_loggers") or []),
        time_window=onset.get("timestamp"),
    )


def _volume_spike(
    milestones: dict[str, TimelineEvent],
    buckets: list[TimelineEvent],
    severity: AnomalySeverity,
) -> SystemAnomaly | None:
    """Report the peak, but only when it stands above the rest of the series."""
    peak = milestones.get("peak_error_volume")
    if peak is None or not buckets:
        return None

    counts = [int(bucket.get("error_count") or 0) for bucket in buckets]
    mean = sum(counts) / len(counts)
    peak_count = max(counts)

    # A flat series has no spike in it, however many errors it carries: that is
    # a sustained failure, which ``_baseline_shift`` is the right report for.
    if peak_count <= mean * SPIKE_RATIO:
        return None

    return SystemAnomaly(
        category="volume_spike",
        severity=severity,
        description=(
            f"Errors peaked at {peak_count} in the bucket starting "
            f"{peak.get('timestamp')}, against a series mean of {mean:.1f} "
            f"across {len(buckets)} bucket(s)."
        ),
        affected_loggers=list(peak.get("top_loggers") or []),
        time_window=peak.get("timestamp"),
    )


def _logger_cascade(
    buckets: list[TimelineEvent], severity: AnomalySeverity
) -> SystemAnomaly | None:
    """Report the order components started failing in — as sequence, not cause."""
    sequence = _error_logger_sequence(buckets)
    if len(sequence) < 2:
        return None

    named = sequence[:MAX_CASCADE_LOGGERS]
    trailing = (
        f", then {len(sequence) - len(named)} further component(s)"
        if len(sequence) > len(named)
        else ""
    )

    return SystemAnomaly(
        category="logger_cascade",
        severity=severity,
        description=(
            f"{len(sequence)} components logged errors, first appearing in this "
            f"order: {' -> '.join(named)}{trailing}. The ordering is consistent "
            "with propagation but does not establish it."
        ),
        affected_loggers=named,
        time_window=None,
    )


def _metadata_concentrations(
    statistics: Statistics,
) -> tuple[list[SystemAnomaly], list[str]]:
    """Find metadata dimensions where one value holds most of the records.

    Returns:
        An ``(anomalies, insights)`` pair. The insight lines restate each
        concentration as a share; the anomalies are the same finding in the
        structured form a consumer can filter on.
    """
    distributions = statistics.get("metadata_distributions") or {}

    found: list[tuple[float, str, object, int, int]] = []
    for key, rows in distributions.items():
        total = sum(int(row.get("count") or 0) for row in rows)
        if total <= 0 or len(rows) < 2:
            # A single-valued key is a constant, not a concentration — every
            # record in a run carrying one ``service`` name says nothing about
            # where failures landed.
            continue

        top = max(rows, key=lambda row: int(row.get("count") or 0))
        count = int(top.get("count") or 0)
        share = count / total
        if share >= DOMINANCE_RATIO:
            found.append((share, key, top.get("value"), count, total))

    # Strongest concentration first; the key name breaks ties so the output is
    # stable across runs regardless of dict ordering.
    found.sort(key=lambda item: (-item[0], item[1]))
    found = found[:MAX_METADATA_INSIGHTS]

    anomalies = [
        SystemAnomaly(
            category="metadata_clustering",
            severity="info",
            description=(
                f"Metadata field {key!r} is concentrated: {value!r} accounts "
                f"for {count} of {total} records ({share:.0%})."
            ),
            affected_loggers=[],
            time_window=None,
        )
        for share, key, value, count, total in found
    ]
    insights = [
        f"{key}={value!r} covers {share:.0%} of records ({count}/{total})."
        for share, key, value, count, total in found
    ]
    return anomalies, insights


def _cross_logger_correlations(buckets: list[TimelineEvent]) -> list[str]:
    """One sentence on the order components started failing in, if several did."""
    sequence = _error_logger_sequence(buckets)
    if len(sequence) < 2:
        return []

    named = sequence[:MAX_CASCADE_LOGGERS]
    return [
        f"Errors appeared first in {named[0]}, then in "
        f"{', '.join(named[1:])} — sequence only; no causal link was inferred."
    ]


def _synthesis(
    statistics: Statistics,
    timeline: list[TimelineEvent],
    anomalies: list[SystemAnomaly],
) -> str:
    """A mechanical narrative of the window, for the ``behavioral_synthesis``."""
    severity = statistics.get("severity") or {}
    coverage = statistics.get("timestamp_coverage") or {}
    buckets = _buckets(timeline)
    milestones = _milestones(timeline)

    error_count = int(severity.get("error_count") or 0)
    warning_count = int(severity.get("warning_count") or 0)
    error_ratio = float(severity.get("error_ratio") or 0.0)

    parts = [
        "Deterministic summary (no model reasoning was available).",
        f"The window carries {error_count} error-level and {warning_count} "
        f"warning-level record(s), an error share of {error_ratio:.1%}.",
    ]

    if coverage.get("earliest") and coverage.get("latest"):
        parts.append(
            f"Logs span {coverage['earliest']} to {coverage['latest']} across "
            f"{len(buckets)} time bucket(s)."
        )

    if "error_onset" in milestones:
        parts.append(
            "Errors broke out of their baseline at "
            f"{milestones['error_onset'].get('timestamp')}"
            + (
                " and recovered at "
                f"{milestones['recovery_onset'].get('timestamp')}."
                if "recovery_onset" in milestones
                else ", with no recovery observed before the logs end."
            )
        )
    elif error_count:
        parts.append("No distinct error onset was detected in the series.")

    parts.append(
        f"{len(anomalies)} anomaly/anomalies were derived arithmetically; "
        "no behavioral interpretation was performed."
        if anomalies
        else "No arithmetic anomaly threshold was crossed; no behavioral "
        "interpretation was performed."
    )

    return " ".join(parts)


def build_fallback_summary(
    statistics: Statistics | None,
    timeline: list[TimelineEvent] | None,
) -> PatternAnalysisResult:
    """Derive a pattern summary from the deterministic inputs alone.

    Never raises and never returns ``None``: a malformed or absent input yields
    an empty-but-well-formed result, because the caller reaching this function
    is already degrading and a second failure here would take the branch down
    entirely.

    Args:
        statistics: The Statistics Node's output, or ``None``.
        timeline: The Timeline Node's output, or ``None``.

    Returns:
        A :class:`~graph_library.models.PatternAnalysisResult` — the same type
        the LLM pass returns, so both paths publish the identical shape.
    """
    statistics = statistics or {}  # type: ignore[assignment]
    timeline = timeline or []

    severity_summary = statistics.get("severity") or {}
    severity = severity_for_error_share(
        float(severity_summary.get("error_ratio") or 0.0)
    )

    milestones = _milestones(timeline)
    buckets = _buckets(timeline)

    metadata_anomalies, metadata_insights = _metadata_concentrations(statistics)

    # Ordered most-diagnostic first: the shift says the system changed state,
    # the spike says how hard, the cascade says where it spread, and the
    # metadata concentration says along which dimension.
    anomalies = [
        anomaly
        for anomaly in (
            _baseline_shift(milestones, severity),
            _volume_spike(milestones, buckets, severity),
            _logger_cascade(buckets, severity),
        )
        if anomaly is not None
    ]
    anomalies.extend(metadata_anomalies)

    return PatternAnalysisResult(
        anomalies=anomalies,
        cross_logger_correlations=_cross_logger_correlations(buckets),
        metadata_insights=metadata_insights,
        behavioral_synthesis=_synthesis(statistics, timeline, anomalies),
    )
