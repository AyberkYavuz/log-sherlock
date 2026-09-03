"""Every SQL statement the API issues, in one reviewable place.

The same split :mod:`graph_library.write_to_db.queries` makes, for the same
reason: the statements are the contract with the storage layer and should be
diffable as a unit.

This module is the *read and delete* half of that contract. The write half —
``CREATE TABLE`` and the idempotent ``INSERT ... ON CONFLICT`` — belongs to
:mod:`graph_library.write_to_db` and is not duplicated here; the API never
writes a row directly, it invokes the graph and lets the ``write_to_db`` node
do it.

:data:`~graph_library.write_to_db.queries.TABLE_NAME` is imported rather than
respelled, so the API and the node cannot end up pointed at two different
tables.
"""

from __future__ import annotations

from graph_library.write_to_db import TABLE_NAME

#: The columns the record list returns, in the order the statement selects them
#: and the order rows are unpacked in. Declared once and interpolated into the
#: SQL below so the projection and the unpacking cannot drift — a reordered
#: ``SELECT`` would otherwise silently swap two same-typed values.
#:
#: ``structured_report`` is conspicuously absent, and that is the point: it is
#: megabytes per row, the list is what a UI renders on first paint, and a
#: ``SELECT *`` here would pull the entire corpus of every stored investigation
#: over the wire to show ten table rows.
METADATA_COLUMNS: tuple[str, ...] = (
    "investigation_id",
    "application_name",
    "confidence_score",
    "analysis_mode",
    "llm_provider",
    "created_at",
    "updated_at",
)

#: How many investigations are stored. Issued alongside the page query so the
#: client can size its pager; a separate statement rather than a window function
#: because ``count(*) OVER ()`` returns nothing at all for a page past the end,
#: which is exactly when the total is most needed.
COUNT_INVESTIGATIONS_SQL = f"SELECT count(*) FROM {TABLE_NAME};"

#: One page of the record list, newest first.
#:
#: ``NULLS LAST`` because ``created_at`` is nullable: a row written before the
#: column had its default would otherwise sort *ahead* of every real
#: investigation under Postgres' descending default, putting the least
#: informative row at the top of the first page.
#:
#: ``investigation_id`` is the tiebreaker, and it is load-bearing rather than
#: tidy: ``created_at`` has a shared ``CURRENT_TIMESTAMP`` per transaction, so
#: rows written together tie, and an unstable sort under ``LIMIT``/``OFFSET``
#: lets the same row appear on two pages while another appears on none.
LIST_METADATA_SQL = f"""
SELECT {", ".join(METADATA_COLUMNS)}
FROM {TABLE_NAME}
ORDER BY created_at DESC NULLS LAST, investigation_id ASC
LIMIT %s OFFSET %s;
"""

#: One stored report. The id is selected back rather than echoed from the
#: parameter so the response reports what the database actually holds.
SELECT_INVESTIGATION_SQL = f"""
SELECT investigation_id, structured_report
FROM {TABLE_NAME}
WHERE investigation_id = %s;
"""

#: Remove one investigation. ``cursor.rowcount`` afterwards is what
#: distinguishes a delete from a no-op, which is the difference between a 200
#: and a 404 — no separate existence check, and therefore no window between the
#: check and the delete in which another request removes the row.
DELETE_INVESTIGATION_SQL = f"DELETE FROM {TABLE_NAME} WHERE investigation_id = %s;"

__all__ = [
    "COUNT_INVESTIGATIONS_SQL",
    "DELETE_INVESTIGATION_SQL",
    "LIST_METADATA_SQL",
    "METADATA_COLUMNS",
    "SELECT_INVESTIGATION_SQL",
    "TABLE_NAME",
]
