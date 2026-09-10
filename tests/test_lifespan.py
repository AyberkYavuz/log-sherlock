"""Tests for the application's startup and shutdown hook.

The hook has one job beyond logging: verify the investigations schema before
the first request is served. Four properties are worth pinning, and each of
them is a thing that would otherwise be discovered in production:

    * it runs, against the same database the repositories were built for;
    * it does **not** run when the application is wired to test doubles, which
      is what keeps the other backend tests offline;
    * a failure aborts startup with the traceback in the log rather than
      leaving a process that answers every storage endpoint with a 503;
    * it runs off the event loop, so a hung connection does not take the
      signal handling down with it.

``TestClient`` runs the lifespan only when it is used as a context manager,
which is exactly the distinction between "built the application" and "booted
it" — and is why the offline suite in ``test_backend_api.py``, which uses a
bare client, never enters this code path at all.
"""

from __future__ import annotations

import logging
import threading
from typing import Any, override

import pytest
from fastapi.testclient import TestClient

from backend import ApiSettings, create_app
from backend.app import LIFESPAN_LOG_PREFIX
from backend.factories import (
    DefaultServiceFactory,
    GraphFactory,
    PostgresRepositoryFactory,
    ServiceFactory,
)
from backend.services import GraphRunnerService, InvestigationService
from graph_library.write_to_db import DatabaseConfig, SchemaInitResult

#: The database the production-shaped factory is pointed at. Never connected
#: to: :func:`backend.app.initialize_database` is substituted in every test
#: below, and the one test that wants a failure raises rather than dialling.
CONFIG = DatabaseConfig(
    host="db.example.internal",
    port=5432,
    dbname="sherlock",
    user="postgres",
    password="",
    connect_timeout=5,
)

#: What a successful verification of an existing schema looks like.
VERIFIED = SchemaInitResult(
    table_created=False,
    indexes_created=(),
    indexes_present=("investigations_created_at_id_idx",),
    preserved_rows=27,
)


class StubGraphFactory(GraphFactory):
    """Hands out an object that is never invoked — no graph is compiled."""

    @override
    def get_graph(self) -> Any:
        return object()


class StubServiceFactory(ServiceFactory):
    """Test wiring: no repository factory, and therefore no database."""

    @override
    def create_investigation_service(self) -> InvestigationService:
        raise AssertionError("no request in these tests reaches a service")

    @override
    def create_graph_runner_service(self) -> GraphRunnerService:
        raise AssertionError("no request in these tests reaches a service")


class SchemaInitRecorder:
    """Stands in for ``initialize_database`` and remembers how it was called."""

    def __init__(self, *, error: Exception | None = None) -> None:
        self.calls: list[DatabaseConfig] = []
        self.threads: list[str] = []
        self._error = error

    def __call__(self, config: DatabaseConfig) -> SchemaInitResult:
        self.calls.append(config)
        self.threads.append(threading.current_thread().name)
        if self._error is not None:
            raise self._error
        return VERIFIED


@pytest.fixture
def recorder(monkeypatch: pytest.MonkeyPatch) -> SchemaInitRecorder:
    """A substituted initializer, patched where the hook looks it up."""
    stub = SchemaInitRecorder()
    monkeypatch.setattr("backend.app.initialize_database", stub)
    return stub


def production_app(**factory_kwargs: Any):
    """An application wired the way a deployment wires it.

    Real :class:`DefaultServiceFactory` over a real
    :class:`PostgresRepositoryFactory` — which is what the hook's production
    check looks for — with only the graph stubbed, so no pipeline is compiled
    for a test about startup.
    """
    factory = DefaultServiceFactory(
        settings=ApiSettings(),
        repository_factory=PostgresRepositoryFactory(CONFIG),
        graph_factory=StubGraphFactory(),
        **factory_kwargs,
    )
    return create_app(ApiSettings(), factory)


# ---------------------------------------------------------------------------
# The hook runs
# ---------------------------------------------------------------------------


def test_the_lifespan_initializes_the_schema_on_boot(
    recorder: SchemaInitRecorder,
) -> None:
    with TestClient(production_app()):
        pass

    assert len(recorder.calls) == 1


def test_it_initializes_the_database_the_repositories_were_built_for(
    recorder: SchemaInitRecorder,
) -> None:
    """Not a second read of the environment, which could name another server."""
    with TestClient(production_app()):
        pass

    assert recorder.calls == [CONFIG]


def test_startup_completes_cleanly_and_the_api_serves(
    recorder: SchemaInitRecorder,
) -> None:
    """The boot finishes, and the application answers inside the context."""
    with TestClient(production_app()) as client:
        response = client.get("/api/health")

    assert response.status_code == 200
    assert response.json() == {"status": "ok", "message": "Backend is running"}
    assert len(recorder.calls) == 1


def test_the_schema_is_verified_before_the_first_request_is_served(
    recorder: SchemaInitRecorder,
) -> None:
    """Ordering, not just occurrence — a late check would serve 503s first."""
    with TestClient(production_app()) as client:
        assert len(recorder.calls) == 1  # already done, before any request
        client.get("/api/health")

    assert len(recorder.calls) == 1  # and not repeated per request


def test_each_boot_verifies_once(recorder: SchemaInitRecorder) -> None:
    app = production_app()

    for _ in range(3):
        with TestClient(app):
            pass

    assert len(recorder.calls) == 3


def test_initialization_runs_off_the_event_loop(
    recorder: SchemaInitRecorder,
) -> None:
    """Blocking driver I/O belongs in a worker thread, not on the loop."""
    with TestClient(production_app()):
        pass

    assert recorder.threads
    assert recorder.threads[0] != threading.main_thread().name


# ---------------------------------------------------------------------------
# ... and does not run where there is no database
# ---------------------------------------------------------------------------


def test_test_wiring_does_not_touch_a_database(
    recorder: SchemaInitRecorder, caplog: pytest.LogCaptureFixture
) -> None:
    """The guard that keeps the rest of the backend suite offline."""
    caplog.set_level(logging.INFO, logger="backend.app")

    with TestClient(create_app(ApiSettings(), StubServiceFactory())) as client:
        assert client.get("/api/health").status_code == 200

    assert recorder.calls == []
    assert "schema verification skipped" in caplog.text


def test_building_the_application_verifies_nothing(
    recorder: SchemaInitRecorder,
) -> None:
    """Construction reads no environment and opens no socket; boot does."""
    production_app()

    assert recorder.calls == []


# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------


def test_the_hook_logs_start_and_completion(
    recorder: SchemaInitRecorder, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.INFO, logger="backend.app")

    with TestClient(production_app()):
        pass

    messages = [record.getMessage() for record in caplog.records]
    prefixed = [m for m in messages if LIFESPAN_LOG_PREFIX in m]

    assert any("Starting automated database schema verification" in m for m in prefixed)
    assert any("schema verification complete" in m.lower() for m in prefixed)
    assert any(CONFIG.target in m for m in prefixed)
    # The summary the initializer returned is carried through verbatim, so the
    # startup log states how many rows the verification preserved.
    assert any("27 row(s) preserved" in m for m in messages)


def test_the_hook_logs_a_shutdown_message(
    recorder: SchemaInitRecorder, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.INFO, logger="backend.app")

    with TestClient(production_app()):
        pass

    assert any(
        LIFESPAN_LOG_PREFIX in record.getMessage()
        and "shutting down" in record.getMessage()
        for record in caplog.records
    )


def test_the_startup_log_never_carries_the_password(
    recorder: SchemaInitRecorder, caplog: pytest.LogCaptureFixture
) -> None:
    """``DatabaseConfig.target`` has no representation for a credential."""
    caplog.set_level(logging.INFO, logger="backend.app")
    secret = "correct-horse-battery-staple"
    factory = DefaultServiceFactory(
        settings=ApiSettings(),
        repository_factory=PostgresRepositoryFactory(CONFIG._replace(password=secret)),
        graph_factory=StubGraphFactory(),
    )

    with TestClient(create_app(ApiSettings(), factory)):
        pass

    assert secret not in caplog.text


# ---------------------------------------------------------------------------
# Failure
# ---------------------------------------------------------------------------


@pytest.fixture
def failing_recorder(monkeypatch: pytest.MonkeyPatch) -> SchemaInitRecorder:
    stub = SchemaInitRecorder(
        error=RuntimeError("could not connect to server: Connection refused")
    )
    monkeypatch.setattr("backend.app.initialize_database", stub)
    return stub


def test_a_failed_initialization_aborts_startup(
    failing_recorder: SchemaInitRecorder,
) -> None:
    with pytest.raises(RuntimeError, match="Connection refused"):
        with TestClient(production_app()):
            pytest.fail("startup should not have completed")


def test_a_failed_initialization_logs_the_stack_trace(
    failing_recorder: SchemaInitRecorder, caplog: pytest.LogCaptureFixture
) -> None:
    """A container log is the only diagnostic available at boot."""
    caplog.set_level(logging.ERROR, logger="backend.app")

    with pytest.raises(RuntimeError):
        with TestClient(production_app()):
            pass

    failures = [r for r in caplog.records if r.levelno >= logging.ERROR]
    assert failures, "the failure was not logged at all"
    record = failures[0]

    assert LIFESPAN_LOG_PREFIX in record.getMessage()
    assert "FAILED" in record.getMessage()
    assert CONFIG.target in record.getMessage()
    # ``logger.exception``, not ``logger.error`` — the traceback is the point.
    assert record.exc_info is not None
    assert "Traceback" in caplog.text
    assert "Connection refused" in caplog.text


def test_a_failed_initialization_names_the_variables_to_check(
    failing_recorder: SchemaInitRecorder, caplog: pytest.LogCaptureFixture
) -> None:
    """The message is the fix, not just the fact."""
    caplog.set_level(logging.ERROR, logger="backend.app")

    with pytest.raises(RuntimeError):
        with TestClient(production_app()):
            pass

    assert "DB_HOST" in caplog.text
    assert "will not start" in caplog.text
