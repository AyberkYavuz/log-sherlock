"""Tests for non-destructive, idempotent database initialization.

The property under test is a *negative* one — initialization must not lose a
row — and a negative property needs a database that can actually lose rows to
be a real test. Two levels are used, and both matter:

    * **A fake PostgreSQL** (:class:`FakeDatabase`) that holds rows, honours
      ``IF NOT EXISTS`` the way the server does, and **raises** if a
      destructive statement ever reaches it. Every test runs here, offline, in
      milliseconds, with no server to install. It is wired in by patching
      ``psycopg2.connect``, which is the lowest possible seam: everything above
      it — :func:`connect`, the :func:`connection` context manager, the cursor
      handling, the transaction — is the production code path.
    * **A real PostgreSQL**, in the one test at the bottom, skipped unless a
      server happens to be reachable. The fake proves the logic; the live test
      proves the SQL, which no double can do.

``psycopg2`` itself is only needed for its ``Json`` adapter, so the module
skips rather than fails where the driver is absent.
"""

from __future__ import annotations

import os
import re
import uuid
from typing import Any

import pytest

psycopg2 = pytest.importorskip("psycopg2")

from graph_library.write_to_db import (  # noqa: E402 - after the driver skip
    INDEX_DDL,
    LIST_ORDER_INDEX_NAME,
    TABLE_NAME,
    DatabaseConfig,
    initialize_database,
    queries,
    upsert_investigation,
)
from init_db import init_db  # noqa: E402 - after the driver skip

# ---------------------------------------------------------------------------
# A PostgreSQL stand-in that can lose data, so a test can prove it did not
# ---------------------------------------------------------------------------

#: Statements this schema must never issue. Checked by the fake at execute
#: time rather than only by a static scan, so a destructive statement composed
#: at runtime — an f-string, a helper, a future migration path — is caught too.
DESTRUCTIVE = re.compile(
    r"\b(DROP|TRUNCATE|DELETE\s+FROM|ALTER\s+TABLE|RESTART\s+IDENTITY|"
    r"SETVAL|CREATE\s+OR\s+REPLACE)\b"
)

#: The columns the tests read back, in the order the fake returns them.
READBACK_COLUMNS = (
    "investigation_id",
    "application_name",
    "confidence_score",
    "analysis_mode",
    "llm_provider",
    "structured_report",
)

#: The tests' own re-query. Deliberately spelled here rather than imported:
#: this is the *verification* statement, and a verification that reuses the
#: statement under test would pass on a table that was never written to.
SELECT_ALL_SQL = (
    f"SELECT {', '.join(READBACK_COLUMNS)} FROM {TABLE_NAME} "
    "ORDER BY investigation_id;"
)


def _normalize(statement: str) -> str:
    """Collapse whitespace and drop the trailing semicolon, for matching."""
    return " ".join(statement.split()).rstrip(";").upper()


class FakeDatabase:
    """Server-side state: which relations exist, and what rows they hold.

    Outlives any one connection, which is what lets a test open a connection,
    insert rows, close it, and then run initialization over a *fresh*
    connection against the same data — the sequence a real deployment
    performs.
    """

    def __init__(self) -> None:
        self.table_exists = False
        self.indexes: set[str] = set()
        self.rows: dict[str, dict[str, Any]] = {}
        #: Every statement any connection executed, normalized. The audit trail
        #: the destructive-statement tests read.
        self.statements: list[str] = []
        self.connections: list[FakeConnection] = []
        #: Set by a test to make one matching statement fail, so the failure
        #: paths of the connection lifecycle can be exercised.
        self.fail_on: str | None = None


class FakeCursor:
    """Enough of a ``psycopg2`` cursor to run this package's statements."""

    def __init__(self, server: FakeDatabase) -> None:
        self._server = server
        self._result: list[tuple[Any, ...]] = []
        self.closed = False

    # -- the context manager the production code uses -----------------------
    def __enter__(self) -> FakeCursor:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def close(self) -> None:
        self.closed = True

    # -- statement dispatch --------------------------------------------------
    def execute(self, statement: str, params: tuple[Any, ...] | None = None) -> None:
        if self.closed:  # pragma: no cover - a guard, not a path
            raise RuntimeError("cursor already closed")

        sql = _normalize(statement)
        self._server.statements.append(sql)

        if DESTRUCTIVE.search(sql):
            raise AssertionError(
                f"a destructive statement reached the database: {statement!r}"
            )

        if self._server.fail_on and self._server.fail_on in sql:
            raise RuntimeError(f"simulated failure executing: {sql[:40]}")

        if sql.startswith("SELECT EXISTS") and "INFORMATION_SCHEMA.TABLES" in sql:
            assert params is not None
            self._result = [(self._server.table_exists and params[0] == TABLE_NAME,)]
            return

        if sql.startswith("SELECT EXISTS") and "PG_INDEXES" in sql:
            assert params is not None
            self._result = [(params[1] in self._server.indexes,)]
            return

        if sql.startswith("CREATE TABLE"):
            # The real server errors on a second unguarded CREATE. Modelling
            # that is the whole point: it is what makes an unguarded statement
            # fail the idempotence tests instead of passing them.
            if "IF NOT EXISTS" not in sql and self._server.table_exists:
                raise RuntimeError(f'relation "{TABLE_NAME}" already exists')
            self._server.table_exists = True
            return

        if sql.startswith("CREATE INDEX") or sql.startswith("CREATE UNIQUE INDEX"):
            name = self._index_name(sql)
            if "IF NOT EXISTS" not in sql and name in self._server.indexes:
                raise RuntimeError(f'relation "{name}" already exists')
            self._server.indexes.add(name)
            return

        if sql.startswith("SELECT COUNT(*)"):
            self._require_table()
            self._result = [(len(self._server.rows),)]
            return

        if sql.startswith("INSERT INTO"):
            self._require_table()
            assert params is not None
            self._upsert(params)
            return

        if sql.startswith("SELECT") and "FROM " + TABLE_NAME.upper() in sql:
            self._require_table()
            self._result = [
                tuple(row[column] for column in READBACK_COLUMNS)
                for _, row in sorted(self._server.rows.items())
            ]
            return

        raise AssertionError(f"the fake database does not implement: {statement!r}")

    @staticmethod
    def _index_name(sql: str) -> str:
        match = re.search(r"CREATE (?:UNIQUE )?INDEX (?:IF NOT EXISTS )?(\w+)", sql)
        assert match, f"could not read an index name out of {sql!r}"
        return match.group(1).lower()

    def _require_table(self) -> None:
        if not self._server.table_exists:
            raise RuntimeError(f'relation "{TABLE_NAME}" does not exist')

    def _upsert(self, params: tuple[Any, ...]) -> None:
        """Apply ``UPSERT_SQL``'s parameters, in its declared column order."""
        key = params[0]
        existing = self._server.rows.get(key, {})
        self._server.rows[key] = {
            "investigation_id": key,
            "application_name": params[1],
            "confidence_score": params[2],
            "analysis_mode": params[3],
            "llm_provider": params[4],
            # ``psycopg2.extras.Json`` wraps the dict; unwrap it so a test can
            # compare against the value it passed in.
            "structured_report": getattr(params[5], "adapted", params[5]),
            # ``created_at`` survives an upsert, exactly as the real statement
            # arranges by omitting it from both halves.
            "created_at": existing.get("created_at", f"t{len(self._server.rows)}"),
        }

    def fetchone(self) -> tuple[Any, ...] | None:
        return self._result[0] if self._result else None

    def fetchall(self) -> list[tuple[Any, ...]]:
        return list(self._result)


class FakeConnection:
    """A connection that records its own lifecycle, so a leak is assertable."""

    def __init__(self, server: FakeDatabase, **kwargs: Any) -> None:
        self._server = server
        self.kwargs = kwargs
        self.cursors: list[FakeCursor] = []
        self.commits = 0
        self.rollbacks = 0
        self.closed = False

    def cursor(self) -> FakeCursor:
        cursor = FakeCursor(self._server)
        self.cursors.append(cursor)
        return cursor

    def commit(self) -> None:
        self.commits += 1

    def rollback(self) -> None:
        self.rollbacks += 1

    def close(self) -> None:
        self.closed = True


@pytest.fixture
def server(monkeypatch: pytest.MonkeyPatch) -> FakeDatabase:
    """A fake PostgreSQL wired in where the driver would be."""
    fake = FakeDatabase()

    def fake_connect(**kwargs: Any) -> FakeConnection:
        conn = FakeConnection(fake, **kwargs)
        fake.connections.append(conn)
        return conn

    monkeypatch.setattr(psycopg2, "connect", fake_connect)
    return fake


@pytest.fixture
def config() -> DatabaseConfig:
    """Connection settings the fake ignores but the production path requires."""
    return DatabaseConfig(
        host="localhost",
        port=5432,
        dbname="postgres",
        user="postgres",
        password="",
        connect_timeout=5,
    )


def store_dummy_rows(config: DatabaseConfig, count: int = 3) -> list[dict[str, Any]]:
    """Insert ``count`` investigations through the production write path."""
    written: list[dict[str, Any]] = []
    for index in range(count):
        row = {
            "investigation_id": f"inv-dummy-{index:03d}",
            "application_name": f"payment-service-{index}",
            "confidence_score": None if index == 1 else 70 + index,
            "analysis_mode": "standard",
            "llm_provider": "local",
            "structured_report": {"synthesis": {"root_cause": f"cause {index}"}},
        }
        upsert_investigation(config, **row)
        written.append(row)
    return written


def read_back(config: DatabaseConfig) -> list[dict[str, Any]]:
    """Re-query every stored investigation over a fresh connection."""
    from graph_library.write_to_db import connection

    with connection(config) as conn, conn.cursor() as cursor:
        cursor.execute(SELECT_ALL_SQL)
        rows = cursor.fetchall()
    return [dict(zip(READBACK_COLUMNS, row, strict=True)) for row in rows]


# ---------------------------------------------------------------------------
# Creation, and creating nothing the second time
# ---------------------------------------------------------------------------


def test_the_first_run_creates_the_table_and_every_index(
    server: FakeDatabase, config: DatabaseConfig
) -> None:
    result = init_db(config)

    assert server.table_exists is True
    assert server.indexes == {name for name, _ in INDEX_DDL}
    assert result.table_created is True
    assert result.indexes_created == tuple(name for name, _ in INDEX_DDL)
    assert result.indexes_present == ()
    assert result.preserved_rows == 0
    assert result.changed is True


def test_a_second_run_creates_nothing_and_raises_nothing(
    server: FakeDatabase, config: DatabaseConfig
) -> None:
    init_db(config)
    result = init_db(config)

    assert result.table_created is False
    assert result.indexes_created == ()
    assert result.indexes_present == tuple(name for name, _ in INDEX_DDL)
    assert result.changed is False


def test_initialization_is_callable_many_times_in_a_row(
    server: FakeDatabase, config: DatabaseConfig
) -> None:
    """Five sequential runs, which is the deployment-on-every-boot case."""
    for _ in range(5):
        init_db(config)

    assert server.table_exists is True
    assert server.indexes == {name for name, _ in INDEX_DDL}
    # One connection per call, every one of them closed.
    assert len(server.connections) == 5
    assert all(conn.closed for conn in server.connections)


def test_a_missing_index_is_added_to_an_existing_table(
    server: FakeDatabase, config: DatabaseConfig
) -> None:
    """The upgrade path: a table created by an earlier release, no index yet."""
    server.table_exists = True

    result = init_db(config)

    assert result.table_created is False
    assert result.indexes_created == tuple(name for name, _ in INDEX_DDL)


# ---------------------------------------------------------------------------
# Data preservation — the point of the exercise
# ---------------------------------------------------------------------------


def test_dummy_rows_survive_initialization(
    server: FakeDatabase, config: DatabaseConfig
) -> None:
    """Insert, initialize, re-query: every record and every field intact."""
    init_db(config)
    written = store_dummy_rows(config)

    result = init_db(config)

    stored = read_back(config)
    assert stored == written
    assert result.preserved_rows == len(written)
    assert result.table_created is False


def test_repeated_initialization_preserves_every_row(
    server: FakeDatabase, config: DatabaseConfig
) -> None:
    """Three runs back to back, each reporting the same untouched rows."""
    init_db(config)
    written = store_dummy_rows(config, count=4)

    for _ in range(3):
        result = init_db(config)
        assert result.preserved_rows == 4

    assert read_back(config) == written


def test_a_null_confidence_score_is_not_disturbed(
    server: FakeDatabase, config: DatabaseConfig
) -> None:
    """``None`` means "not measured" and must not become ``0`` in passing."""
    init_db(config)
    store_dummy_rows(config)

    init_db(config)

    scores = {row["investigation_id"]: row["confidence_score"] for row in read_back(config)}
    assert scores["inv-dummy-001"] is None


def test_initialization_of_a_populated_table_issues_no_destructive_statement(
    server: FakeDatabase, config: DatabaseConfig
) -> None:
    """The audit trail, read back statement by statement.

    The fake also raises on a destructive statement, so this assertion is the
    belt to that braces — it fails with the offending SQL in the message
    rather than with an ``AssertionError`` from inside a cursor.
    """
    init_db(config)
    store_dummy_rows(config)
    server.statements.clear()

    init_db(config)

    offenders = [sql for sql in server.statements if DESTRUCTIVE.search(sql)]
    assert offenders == []
    assert server.statements, "initialization issued no statements at all"


# ---------------------------------------------------------------------------
# The statements themselves
# ---------------------------------------------------------------------------


def test_every_ddl_statement_is_guarded_with_if_not_exists() -> None:
    assert "IF NOT EXISTS" in _normalize(queries.CREATE_TABLE_SQL)
    for name, statement in INDEX_DDL:
        assert "IF NOT EXISTS" in _normalize(statement), name


def test_the_package_declares_no_destructive_statement() -> None:
    """No module-level SQL in the package may destroy anything.

    Including the name that used to: ``TRUNCATE_TABLE_SQL`` is gone from
    ``queries`` and from the package's public surface, so an old caller fails
    at import rather than silently finding a statement that no longer means
    what it did.
    """
    import graph_library.write_to_db as package

    for module in (queries, package):
        assert not hasattr(module, "TRUNCATE_TABLE_SQL")
        for name in getattr(module, "__all__", ()):
            value = getattr(module, name)
            if isinstance(value, str) and DESTRUCTIVE.search(_normalize(value)):
                pytest.fail(f"{module.__name__}.{name} is destructive: {value!r}")


def test_the_list_index_matches_the_ordering_the_api_pages_by() -> None:
    """An index that does not match the ``ORDER BY`` serves no query at all."""
    from backend.persistence.queries import LIST_METADATA_SQL

    ordering = "CREATED_AT DESC NULLS LAST, INVESTIGATION_ID ASC"
    assert ordering in _normalize(LIST_METADATA_SQL)
    assert ordering in _normalize(queries.CREATE_LIST_ORDER_INDEX_SQL)
    assert LIST_ORDER_INDEX_NAME in {name for name, _ in INDEX_DDL}


# ---------------------------------------------------------------------------
# Connection lifecycle
# ---------------------------------------------------------------------------


def test_the_connection_and_cursor_are_closed_and_committed_once(
    server: FakeDatabase, config: DatabaseConfig
) -> None:
    init_db(config)

    assert len(server.connections) == 1
    conn = server.connections[0]
    assert conn.closed is True
    assert conn.commits == 1
    assert conn.rollbacks == 0
    assert conn.cursors and all(cursor.closed for cursor in conn.cursors)


def test_a_failing_statement_rolls_back_and_still_closes_the_connection(
    server: FakeDatabase, config: DatabaseConfig
) -> None:
    """A leaked session is the failure mode a long-lived process pays for."""
    server.fail_on = "CREATE INDEX"

    with pytest.raises(RuntimeError, match="simulated failure"):
        init_db(config)

    conn = server.connections[0]
    assert conn.closed is True
    assert conn.rollbacks == 1
    assert conn.commits == 0
    assert all(cursor.closed for cursor in conn.cursors)


def test_no_session_is_left_open_across_many_runs(
    server: FakeDatabase, config: DatabaseConfig
) -> None:
    for _ in range(10):
        init_db(config)

    assert len(server.connections) == 10
    assert [conn.closed for conn in server.connections] == [True] * 10


# ---------------------------------------------------------------------------
# Wiring
# ---------------------------------------------------------------------------


def test_init_db_reads_the_environment_when_given_no_config(
    server: FakeDatabase, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("DB_HOST", "db.example.internal")
    monkeypatch.setenv("DB_NAME", "sherlock")

    init_db()

    assert server.connections[0].kwargs["host"] == "db.example.internal"
    assert server.connections[0].kwargs["dbname"] == "sherlock"


def test_init_db_delegates_to_the_shared_implementation(
    server: FakeDatabase, config: DatabaseConfig
) -> None:
    """``init_db`` is a thin wrapper, not a second copy of the DDL.

    Asserted by equality once the schema is settled: from that point both
    functions must report the same "nothing to do" result, which they cannot do
    if either is issuing statements the other does not.
    """
    init_db(config)

    assert init_db(config) == initialize_database(config)


def test_the_structured_log_reports_start_verification_and_completion(
    server: FakeDatabase, config: DatabaseConfig, capsys: pytest.CaptureFixture[str]
) -> None:
    """The three lines a deployment log needs, each carrying the prefix."""
    init_db(config)
    capsys.readouterr()

    init_db(config)
    lines = [
        line for line in capsys.readouterr().out.splitlines() if "[LogSherlock DB]" in line
    ]
    blob = "\n".join(lines).lower()

    assert "non-destructive" in blob
    assert f"checking for table '{TABLE_NAME}'" in blob
    assert "verified" in blob
    assert "initialization complete" in blob
    assert "row(s) preserved" in blob


# ---------------------------------------------------------------------------
# The same guarantee against a real server, when there is one
# ---------------------------------------------------------------------------


def _live_config() -> DatabaseConfig | None:
    """Settings for a reachable PostgreSQL, or ``None``.

    Opt-in through the environment the rest of the project already uses. The
    probe is a real connection because "reachable" is not something ``DB_HOST``
    can tell you.
    """
    if not os.getenv("LOGSHERLOCK_TEST_LIVE_DB"):
        return None
    config = DatabaseConfig.from_env()
    try:
        conn = psycopg2.connect(**config.connection_kwargs())
    except Exception:  # noqa: BLE001 - absence is the normal case
        return None
    conn.close()
    return config


@pytest.mark.skipif(
    _live_config() is None,
    reason="set LOGSHERLOCK_TEST_LIVE_DB=1 with a reachable PostgreSQL to run this",
)
def test_a_real_row_survives_initialization_against_live_postgres() -> None:
    """Insert, initialize twice, re-query — against actual PostgreSQL.

    Uses a uniquely-keyed row and removes only that row afterwards, so the
    test is safe to run against a database that holds real investigations.
    """
    from graph_library.write_to_db import connection

    config = _live_config()
    assert config is not None

    init_db(config)

    marker = f"inv-livetest-{uuid.uuid4().hex[:12]}"
    report = {"synthesis": {"root_cause": "live initialization test"}}
    upsert_investigation(
        config,
        investigation_id=marker,
        application_name="init-db-live-test",
        confidence_score=42,
        analysis_mode="fast",
        llm_provider="local",
        structured_report=report,
    )

    try:
        with connection(config) as conn, conn.cursor() as cursor:
            cursor.execute(queries.COUNT_ROWS_SQL)
            before = cursor.fetchone()[0]

        first = init_db(config)
        second = init_db(config)

        with connection(config) as conn, conn.cursor() as cursor:
            cursor.execute(
                f"SELECT application_name, confidence_score, structured_report "
                f"FROM {TABLE_NAME} WHERE investigation_id = %s;",
                (marker,),
            )
            row = cursor.fetchone()
            cursor.execute(queries.COUNT_ROWS_SQL)
            after = cursor.fetchone()[0]

        assert row is not None, "initialization removed the row it must preserve"
        assert row[0] == "init-db-live-test"
        assert row[1] == 42
        assert row[2] == report
        assert after == before
        assert first.table_created is False and second.table_created is False
        assert first.preserved_rows == before
    finally:
        # The test's own row, by exact id. Nothing else is touched.
        with connection(config) as conn, conn.cursor() as cursor:
            cursor.execute(
                f"DELETE FROM {TABLE_NAME} WHERE investigation_id = %s;", (marker,)
            )
