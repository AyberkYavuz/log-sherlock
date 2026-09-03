"""Every SQL statement this package issues, in one readable place.

Kept apart from the code that executes them for the same reason the prompt
modules are kept apart from the LLM nodes: the statements are the contract with
the storage layer, they are reviewed as a unit, and a schema change should be a
diff against one file rather than a hunt through connection handling.

The table is created by ``init_db.py`` rather than by the node. A node that
issued DDL would need elevated privileges on every run, and a typo in a report
would become a schema migration.
"""

from __future__ import annotations

#: The one table this package reads or writes. Every statement below is
#: interpolated from this name rather than repeating the literal, so the table
#: cannot be renamed in one statement and not another.
TABLE_NAME = "investigations"

#: Whether the target table is already present. Scoped to ``public`` because
#: that is the schema the ``CREATE TABLE`` below lands in; an ``investigations``
#: table in some other schema is a different table and must not be truncated.
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

#: Empty the table without dropping it. ``RESTART IDENTITY`` is absent on
#: purpose — there is no sequence to restart, since the primary key is supplied
#: by the caller — and so is ``CASCADE``: nothing references this table today,
#: and if something ever does, failing loudly is the correct answer to
#: "truncate this" rather than silently emptying the referencing table too.
TRUNCATE_TABLE_SQL = f"TRUNCATE TABLE {TABLE_NAME};"

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
    "CREATE_TABLE_SQL",
    "TABLE_EXISTS_SQL",
    "TABLE_NAME",
    "TRUNCATE_TABLE_SQL",
    "UPSERT_SQL",
]
