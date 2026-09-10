"""Every SQL statement this package issues, in one readable place.

Kept apart from the code that executes them for the same reason the prompt
modules are kept apart from the LLM nodes: the statements are the contract with
the storage layer, they are reviewed as a unit, and a schema change should be a
diff against one file rather than a hunt through connection handling.

The table is created by ``init_db.py`` rather than by the node. A node that
issued DDL would need elevated privileges on every run, and a typo in a report
would become a schema migration.

**Nothing in this module destroys data.** Every DDL statement below is guarded
with ``IF NOT EXISTS``, and there is deliberately no ``DROP``, no ``TRUNCATE``
and no sequence reset anywhere in the package: initialization is something a
deployment may run on every boot, so it has to be safe against a table that
already holds investigations. The only statement that changes a row is
:data:`UPSERT_SQL`, and it is keyed on an id the caller supplies.
"""

from __future__ import annotations

#: The one table this package reads or writes. Every statement below is
#: interpolated from this name rather than repeating the literal, so the table
#: cannot be renamed in one statement and not another.
TABLE_NAME = "investigations"

#: Whether the target table is already present. Scoped to ``public`` because
#: that is the schema the ``CREATE TABLE`` below lands in; an
#: ``investigations`` table in some other schema is a different table entirely.
#:
#: Used only to *report* whether a run created the table or found it — the
#: ``CREATE TABLE IF NOT EXISTS`` below is what actually makes the decision, so
#: there is no check-then-act race to lose.
TABLE_EXISTS_SQL = """
SELECT EXISTS (
    SELECT 1
    FROM information_schema.tables
    WHERE table_schema = 'public'
      AND table_name = %s
);
"""

#: The investigations schema.
#:
#: ``investigation_id`` is the caller's identifier rather than a generated
#: surrogate key, because it is what makes the write idempotent: re-running an
#: investigation must correct the stored row, not accumulate a second one.
#:
#: ``structured_report`` is ``JSONB`` rather than ``JSON`` — it is queried
#: (``->>'metadata'``, containment on ``ai_insights``) far more than it is
#: round-tripped, and only ``JSONB`` can be indexed. The four columns beside it
#: are deliberate duplication of values that also live inside that document:
#: they are what a dashboard filters and sorts on, and neither is authoritative
#: over the other because both are written from the same report in one
#: statement.
CREATE_TABLE_SQL = f"""
CREATE TABLE IF NOT EXISTS {TABLE_NAME} (
    investigation_id  VARCHAR(255) PRIMARY KEY,
    application_name  VARCHAR(255),
    confidence_score  INTEGER,
    analysis_mode     VARCHAR(50),
    llm_provider      VARCHAR(50),
    structured_report JSONB,
    created_at        TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
    updated_at        TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
);
"""

#: Whether a named index is present in the ``public`` schema. Like
#: :data:`TABLE_EXISTS_SQL`, this only decides which sentence gets logged; the
#: ``IF NOT EXISTS`` on the statement itself is what makes the create safe.
INDEX_EXISTS_SQL = """
SELECT EXISTS (
    SELECT 1
    FROM pg_indexes
    WHERE schemaname = 'public'
      AND tablename = %s
      AND indexname = %s
);
"""

#: The index behind the API's record list. Its column list and its sort
#: directions are copied deliberately from ``LIST_METADATA_SQL`` in
#: :mod:`backend.persistence.queries` — ``ORDER BY created_at DESC NULLS LAST,
#: investigation_id ASC`` — because an index only serves a paginated ordering
#: when it matches that ordering exactly, ``NULLS LAST`` included. Postgres can
#: read it backwards but not re-sort it for free, and the pager issues this sort
#: on every first paint.
LIST_ORDER_INDEX_NAME = "investigations_created_at_id_idx"

#: ``IF NOT EXISTS`` rather than a bare ``CREATE INDEX``, so initialization is
#: re-runnable. Deliberately *not* ``CONCURRENTLY``: that variant cannot run
#: inside a transaction block, and this statement shares one with the
#: ``CREATE TABLE`` above so a half-applied schema is not a state this script
#: can leave behind. The trade is a brief write lock on a table that is written
#: once per investigation.
CREATE_LIST_ORDER_INDEX_SQL = f"""
CREATE INDEX IF NOT EXISTS {LIST_ORDER_INDEX_NAME}
ON {TABLE_NAME} (created_at DESC NULLS LAST, investigation_id ASC);
"""

#: Every index this schema declares, as ``(name, statement)`` pairs so the
#: initializer can report each one by name. One entry, and the shortness is the
#: point: the primary key already indexes ``investigation_id``, which is what
#: the detail fetch and the delete both look a row up by, and nothing in this
#: repository filters on ``application_name``, ``analysis_mode`` or
#: ``llm_provider`` — the UI's search is client-side over rows it has already
#: loaded. An index that serves no statement costs every write and speeds up
#: nothing.
INDEX_DDL: tuple[tuple[str, str], ...] = (
    (LIST_ORDER_INDEX_NAME, CREATE_LIST_ORDER_INDEX_SQL),
)

#: How many investigations are stored. Read after initialization so the script
#: can report the number of rows it left untouched, which is the one line that
#: makes "non-destructive" checkable rather than merely claimed.
#:
#: :mod:`backend.persistence.queries` declares its own count for its own
#: contract; both interpolate :data:`TABLE_NAME`, so neither can drift onto a
#: different table.
COUNT_ROWS_SQL = f"SELECT count(*) FROM {TABLE_NAME};"

#: The node's single write. One statement rather than a SELECT-then-branch,
#: because the check-and-act version has a race between two graph runs finishing
#: at once and is three round trips where this is one.
#:
#: ``created_at`` is absent from both halves by design: it keeps its column
#: default on the first write and is left untouched by every later one, so the
#: row remembers when the investigation was first stored even after it is
#: re-run. ``updated_at`` is set to ``CURRENT_TIMESTAMP`` in the update branch
#: rather than to ``EXCLUDED.updated_at``, so the stored time is the server's
#: and not one derived from whatever clock the graph ran on.
UPSERT_SQL = f"""
INSERT INTO {TABLE_NAME} (
    investigation_id,
    application_name,
    confidence_score,
    analysis_mode,
    llm_provider,
    structured_report,
    updated_at
)
VALUES (%s, %s, %s, %s, %s, %s, CURRENT_TIMESTAMP)
ON CONFLICT (investigation_id) DO UPDATE SET
    application_name  = EXCLUDED.application_name,
    confidence_score  = EXCLUDED.confidence_score,
    analysis_mode     = EXCLUDED.analysis_mode,
    llm_provider      = EXCLUDED.llm_provider,
    structured_report = EXCLUDED.structured_report,
    updated_at        = CURRENT_TIMESTAMP;
"""

__all__ = [
    "COUNT_ROWS_SQL",
    "CREATE_LIST_ORDER_INDEX_SQL",
    "CREATE_TABLE_SQL",
    "INDEX_DDL",
    "INDEX_EXISTS_SQL",
    "LIST_ORDER_INDEX_NAME",
    "TABLE_EXISTS_SQL",
    "TABLE_NAME",
    "UPSERT_SQL",
]
