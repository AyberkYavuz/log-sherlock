"""The Write to DB Node — the graph's persistence step.

The last node in the topology and the only one that leaves the process. It
takes the ``structured_report`` that ``prepare_output`` assembled and writes it
to PostgreSQL, keyed by ``investigation_id`` so that re-running an investigation
under the same id corrects the stored row rather than accumulating a second one.

``investigation_id`` is optional. When the caller supplies none — which is the
normal case for a LangGraph Studio run, where there is no natural place to type
a primary key — one is generated and the fact is recorded in
``investigation_notes``. Persisting is therefore the default rather than
something a caller has to opt into, at the cost of idempotence on exactly the
runs that never asked for it. See :func:`generate_investigation_id`.

It has no analysis to contribute and deliberately publishes none. Three fields
leave this node — ``db_persisted``, one investigation note and its
``completed_stages`` entry — and in particular it no longer writes
``structured_report``. The stub it replaces returned an empty dict there, which
(the field having no reducer, and this node running immediately after the one
that fills it) overwrote the finished report in the final state. That is fixed
by omission: a node that does not own a field must not return it.

**It degrades rather than fails, and the bar is higher here than elsewhere.**
Every other node degrades to protect the run; this one degrades to protect a
run that is already *complete*. By the time it executes, the entire
investigation has been parsed, analyzed, synthesized and scored, so an
unreachable database must cost the storage and nothing else. Every failure path
therefore returns the same shape as the happy one, with ``db_persisted: False``
and the reason recorded in ``investigation_notes``. Nothing raises out of
:func:`write_to_db_node`.

``completed_stages`` gains ``"write_to_db"`` on *every* path, including the
failure paths. The channel records which stages ran, not which succeeded — the
note and the flag are what carry the outcome — and a node that omitted itself
on failure would make a degraded run indistinguishable from a truncated one.
"""

from __future__ import annotations

import logging
import uuid
from typing import Any

from .config import DatabaseConfig
from .db import announce, upsert_investigation

logger = logging.getLogger(__name__)

#: Columns lifted out of the report into their own SQL columns, so a dashboard
#: can filter and sort without opening the JSONB document. Each is looked up in
#: ``metadata`` first, then ``synthesis``, then the top level of graph state —
#: see :func:`_read_attribute`.
RELATIONAL_ATTRIBUTES: tuple[str, ...] = (
    "application_name",
    "confidence_score",
    "analysis_mode",
    "llm_provider",
)

#: Written to ``application_name`` when neither the report nor the state names
#: one. The column is nullable, but a readable placeholder beats a ``NULL``
#: that a dashboard has to special-case in every query.
UNKNOWN_APPLICATION = "unknown"

#: The maximum length of the ``investigation_id`` column. Checked here rather
#: than left to the server so an over-long id is reported as the input problem
#: it is, instead of as a failed write halfway through the persistence step.
MAX_INVESTIGATION_ID_CHARS = 255

#: Where a driver's error message is cut when it is quoted into a note.
#: ``psycopg2`` reports a refused connection over four lines, once per address
#: family it tried, and an investigation note is a single line in a report a
#: human reads. The full text, newlines and all, is in the logged traceback.
MAX_REASON_CHARS = 200

#: Prefix on an id this node generated rather than received. Visible on purpose:
#: an operator reading a stored row can tell at a glance whether the key is one
#: their system chose — and can therefore correlate it with something — or one
#: this run invented, which correlates with nothing outside the table.
GENERATED_ID_PREFIX = "inv-graph-"

#: Hex characters of the UUID kept after the prefix. Eight gives roughly four
#: billion values, which is far beyond any collision risk for a table of
#: investigations, and keeps the id short enough to paste into a query by hand.
#: The trade is deliberate: this id exists to be unique within one table, not to
#: be globally unique, and a caller who needs the latter supplies their own.
GENERATED_ID_HEX_CHARS = 8


def generate_investigation_id() -> str:
    """Mint an id for a run whose caller supplied none.

    Format is ``inv-graph-<8 hex chars>``, drawn from :func:`uuid.uuid4`.

    Generating rather than refusing is what makes persistence the default: a
    LangGraph Studio run has no natural place to type a primary key, and a
    complete investigation that goes unstored because of that is a worse outcome
    than a row whose key nobody chose. The trade-off is real and is recorded in
    ``investigation_notes`` on every run that takes it — see
    :data:`GENERATED_ID_PREFIX`. A generated id is *not* idempotent across runs:
    re-running the same logs mints a new key and stores a second row, where a
    caller-supplied id would have corrected the first.

    Returns:
        A new id, always within :data:`MAX_INVESTIGATION_ID_CHARS`.
    """
    return f"{GENERATED_ID_PREFIX}{uuid.uuid4().hex[:GENERATED_ID_HEX_CHARS]}"


def _reason(exc: Exception) -> str:
    """Render an exception as one bounded line, for a note or a log line."""
    message = " ".join(str(exc).split())
    if len(message) > MAX_REASON_CHARS:
        message = f"{message[: MAX_REASON_CHARS - 1]}…"
    return f"{type(exc).__name__}: {message}" if message else type(exc).__name__


def _delta(
    *,
    persisted: bool,
    notes: list[str],
) -> dict[str, Any]:
    """Build the node's partial state delta. One shape, every path.

    Args:
        persisted: Whether the row reached PostgreSQL.
        notes: The lines this run contributes to ``investigation_notes``, in
            the order they should be read. A run that both generated an id and
            stored a row contributes two: what it decided, then what it did.

    Returns:
        The delta. ``investigation_notes`` and ``completed_stages`` use
        additive reducers in the graph, so returning only this node's own
        contribution is correct.
    """
    return {
        "db_persisted": persisted,
        "investigation_notes": list(notes),
        "completed_stages": ["write_to_db"],
    }


def _read_attribute(
    name: str,
    report: dict[str, Any],
    state: dict[str, Any],
) -> Any:
    """Find one relational attribute wherever this run happens to carry it.

    Three sources, in descending order of authority. ``metadata`` is where
    ``prepare_output`` records the run's identity, and it holds the
    *normalized* provider and mode — ``anthropic`` where the caller typed
    ``Claude`` — which is what makes the stored row a reproducibility record
    rather than a transcript of the form. ``synthesis`` is read next because
    the report's two text sections are a plausible future home for a value that
    moves; today it holds none of these and the lookup simply misses. Graph
    state is the last resort, and covers a report assembled by an older release
    or truncated by a partial run.

    Args:
        name: The attribute to find.
        report: The structured report.
        state: The graph state.

    Returns:
        The first value found, or ``None``. A ``None`` *stored* in one of the
        sections is skipped rather than returned, so an explicitly-null
        ``confidence_score`` in the metadata still falls through to the state.
    """
    metadata = report.get("metadata")
    synthesis = report.get("synthesis")

    for source in (metadata, synthesis, state):
        if isinstance(source, dict) and source.get(name) is not None:
            return source[name]
    return None


def _coerce_score(value: Any) -> int | None:
    """Read the confidence score as an ``INTEGER``, or store nothing.

    ``None`` rather than ``0`` for anything unusable: the column is nullable
    precisely so that "not measured" and "measured as zero" stay distinguishable
    in the stored row, and collapsing them here would misreport a degraded run
    as a maximally uncertain one.
    """
    if isinstance(value, bool) or value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        logger.warning("confidence_score %r is not an integer; storing NULL", value)
        return None


def _text(value: Any, default: str = "") -> str:
    """Read a value destined for a ``VARCHAR`` column, defensively."""
    return value if isinstance(value, str) and value else default


def write_to_db_node(state: dict[str, Any]) -> dict[str, Any]:
    """Persist the completed investigation to PostgreSQL.

    Args:
        state: The LogSherlock graph state. Reads ``structured_report`` (the
            artifact to store) and ``investigation_id`` (the primary key).
            The id is optional: when it is absent, null or blank one is
            generated, so a run that never thought about persistence is still
            persisted. Also reads ``application_name``, ``confidence_score``,
            ``analysis_mode`` and ``llm_provider`` as fallbacks for the
            relational columns when the report does not carry them. Treated as
            read-only throughout — a generated id is reported in the notes
            rather than written back to ``investigation_id``, since this node
            does not own that field.

    Returns:
        A partial state delta containing:

            * ``db_persisted`` — ``True`` only when a row reached the database,
            * ``investigation_notes`` — one line saying what happened, preceded
              by a second one when the id was generated,
            * ``completed_stages`` — ``["write_to_db"]``, on every path.

        No other state field is touched, and no exception escapes.
    """
    report = state.get("structured_report")
    investigation_id = state.get("investigation_id")

    # Collected as the run proceeds rather than built at each return, because
    # the id decision is made before the outcome is known and both belong in
    # the same delta, in that order.
    notes: list[str] = []

    # -- input validation ---------------------------------------------------
    # Checked before the driver is imported and before a socket is opened: a
    # run with nothing to store must not spend a connection discovering that.
    if not isinstance(report, dict) or not report:
        announce("No structured report in state; nothing to persist")
        return _delta(
            persisted=False,
            notes=[
                "Write to DB: no structured report was available in state, so "
                "nothing was persisted. The investigation itself is unaffected; "
                "only its storage was skipped."
            ],
        )

    # -- the primary key ----------------------------------------------------
    # Absent, null, non-string or whitespace all mean the same thing here —
    # nobody chose a key — and all take the same path. Only an id the caller
    # genuinely supplied and that cannot be stored is an error.
    if isinstance(investigation_id, str) and investigation_id.strip():
        investigation_id = investigation_id.strip()

        if len(investigation_id) > MAX_INVESTIGATION_ID_CHARS:
            announce(
                f"investigation_id is {len(investigation_id)} characters; too long"
            )
            return _delta(
                persisted=False,
                notes=[
                    f"Write to DB: the investigation_id is "
                    f"{len(investigation_id)} characters long and the column "
                    f"holds at most {MAX_INVESTIGATION_ID_CHARS}, so the report "
                    "was not persisted."
                ],
            )
    else:
        investigation_id = generate_investigation_id()
        announce(f"No investigation_id supplied; generated {investigation_id}")
        notes.append(
            f"Write to DB: no investigation_id was supplied, so "
            f"{investigation_id} was generated for this run. Supply an id in "
            "the input state to make re-runs update the same row instead of "
            "storing a new one."
        )

    # -- payload unpacking --------------------------------------------------
    attributes = {
        name: _read_attribute(name, report, state) for name in RELATIONAL_ATTRIBUTES
    }
    application_name = _text(attributes["application_name"], UNKNOWN_APPLICATION)
    confidence_score = _coerce_score(attributes["confidence_score"])
    analysis_mode = _text(attributes["analysis_mode"])
    llm_provider = _text(attributes["llm_provider"])

    # Read from the environment as it stands. This node deliberately does *not*
    # load ``.env`` itself, and that is a correctness requirement rather than a
    # style choice: ``load_dotenv`` mutates ``os.environ`` for the whole
    # process, so a node calling it would inject every key in the file —
    # provider credentials, ``LANGSMITH_TRACING`` — into a process that had
    # deliberately not set them. Under a test suite that is the difference
    # between a hermetic run and one that makes real, billed API calls from the
    # next node that looks for a key. Populating the environment belongs to the
    # entry point: ``langgraph.json`` does it with ``"env": ".env"``, and
    # ``init_db.py`` calls :func:`~graph_library.write_to_db.db.load_env_file`
    # explicitly. Every other node in this graph reads credentials the same way.
    config = DatabaseConfig.from_env()

    logger.info(
        "Write to DB node starting: investigation_id=%s application=%s "
        "target=%s confidence=%s mode=%s provider=%s report_sections=%s",
        investigation_id,
        application_name,
        config.target,
        confidence_score,
        analysis_mode or "<unset>",
        llm_provider or "<unset>",
        sorted(report),
    )

    # -- the write ----------------------------------------------------------
    try:
        upsert_investigation(
            config,
            investigation_id=investigation_id,
            application_name=application_name,
            confidence_score=confidence_score,
            analysis_mode=analysis_mode,
            llm_provider=llm_provider,
            structured_report=report,
        )
    except Exception as exc:  # noqa: BLE001 - a stored failure beats a dead run
        # The investigation is already complete at this point; only its storage
        # failed. Logged with a traceback because the note is a summary and the
        # cause is usually in the driver's exception chain.
        logger.error(
            "Failed to persist investigation %s to %s; the run is unaffected "
            "and continues",
            investigation_id,
            config.target,
            exc_info=True,
        )
        reason = _reason(exc)
        announce(
            f"FAILED to persist investigation {investigation_id} to "
            f"{config.target} ({reason})"
        )
        return _delta(
            persisted=False,
            notes=[
                *notes,
                f"Write to DB: could not persist investigation "
                f"{investigation_id} to PostgreSQL at {config.target} "
                f"({reason}). The investigation completed normally and every "
                "finding in this state is intact; only storage failed.",
            ],
        )

    announce(f"Successfully persisted investigation {investigation_id}")
    logger.info(
        "Write to DB node complete: investigation_id=%s target=%s",
        investigation_id,
        config.target,
    )
    return _delta(
        persisted=True,
        notes=[
            *notes,
            f"Successfully persisted investigation {investigation_id} to "
            "PostgreSQL database.",
        ],
    )


__all__ = ["write_to_db_node"]
