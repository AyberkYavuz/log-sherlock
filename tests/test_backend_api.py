"""Tests for the LogSherlock HTTP API (``backend/``).

Everything here runs offline. No PostgreSQL, no LLM provider, no compiled
graph: the application is built through
:func:`~backend.app.create_app` with a substitute
:class:`~backend.factories.ServiceFactory`, which is the single seam the whole
object graph hangs off. That is the point of the abstraction rather than a
convenience — the doubles below implement the same ABCs the production classes
do, so a test that passes here is a test against the real interfaces.

The conventions asserted:

    * every endpoint's success path returns exactly the documented contract,
      and the record list never carries ``structured_report``;
    * a payload that does not validate is a 422 naming the field, a missing
      record is a 404, and an unreachable database is a 503 — each in the same
      ``{"detail": ...}`` envelope;
    * ``POST /api/investigate`` is a 200 even when the graph could not persist:
      ``db_persisted`` carries the outcome and ``investigation_notes`` carries
      the reason, because that pair is what the UI's two scenarios branch on;
    * an id is generated when the caller supplies none, and a supplied one is
      never replaced;
    * the overloaded service entry points dispatch on their call shape, and
      refuse a combination that matches neither overload;
    * CORS is configured for the two React development origins.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from typing import Any, override

import pytest
from fastapi.testclient import TestClient

from backend import ApiSettings, create_app
from backend.errors import RepositoryError
from backend.factories import GraphFactory, ServiceFactory
from backend.persistence import (
    InvestigationMetadataRow,
    InvestigationPage,
    InvestigationRepository,
    StoredInvestigation,
)
from backend.schemas import MAX_LIMIT, InvestigateRequest
from backend.services import (
    GENERATED_ID_CHARS,
    GENERATED_ID_PREFIX,
    DefaultInvestigationService,
    GraphRunnerService,
    InvestigationService,
    LangGraphRunnerService,
    generate_investigation_id,
)

# ---------------------------------------------------------------------------
# Fixture data
# ---------------------------------------------------------------------------

#: A stored report shaped exactly like the ``StructuredInvestigationReport``
#: the graph writes — four sections, partitioned by provenance. Trimmed in
#: depth, not in breadth: the detail endpoint's whole contract is that it
#: returns the document verbatim, so every top-level section has to be present
#: for that assertion to mean anything.
SAMPLE_REPORT: dict[str, Any] = {
    "metadata": {
        "application_name": "checkout-api",
        "investigation_timestamp": "2026-01-01T11:00:00+00:00",
        "analysis_mode": "standard",
        "llm_provider": "openai",
        "confidence_score": 90,
        "parser_metrics": {"detected_format": "json", "total_lines": 100},
    },
    "synthesis": {
        "root_cause": "The payment client lost its database connection.",
        "executive_summary": "At 10:05 the payment client began refusing...",
        "investigation_notes": ["Parser: 3 malformed lines were skipped."],
    },
    "deterministic_outputs": {
        "statistics": {"severity": {"error_count": 12}},
        "timeline": [{"event_type": "milestone", "milestone_kind": "first_error"}],
    },
    "ai_insights": {
        "error_summary": {"primary_error_signature_id": "ERR_001"},
        "pattern_summary": {"anomalies": []},
    },
}

_CREATED = datetime(2026, 1, 1, 10, 0, tzinfo=timezone.utc)


def _row(
    investigation_id: str,
    *,
    application_name: str | None = "checkout-api",
    confidence_score: int | None = 90,
    minute: int = 0,
) -> InvestigationMetadataRow:
    """Build one metadata row as the repository would return it."""
    stamp = _CREATED.replace(minute=minute)
    return {
        "investigation_id": investigation_id,
        "application_name": application_name,
        "confidence_score": confidence_score,
        "analysis_mode": "standard",
        "llm_provider": "openai",
        "created_at": stamp,
        "updated_at": stamp,
    }


# ---------------------------------------------------------------------------
# Test doubles — real implementations of the production ABCs
# ---------------------------------------------------------------------------


class MemoryInvestigationRepository(InvestigationRepository):
    """An in-memory :class:`InvestigationRepository`.

    A full implementation rather than a mock: it holds rows, honours
    ``limit``/``offset`` and reports ``rowcount`` semantics on delete, so the
    service's pagination arithmetic and its not-found handling are exercised
    against behaviour rather than against a recorded call.
    """

    def __init__(
        self,
        rows: list[InvestigationMetadataRow] | None = None,
        reports: dict[str, dict[str, Any]] | None = None,
    ) -> None:
        self.rows = list(rows or [])
        self.reports = dict(reports or {})
        #: Every ``(limit, offset)`` the service asked for, so a test can assert
        #: the offset was computed from the page rather than guessed.
        self.page_calls: list[tuple[int, int]] = []

    @override
    def fetch_page(self, *, limit: int, offset: int) -> InvestigationPage:
        self.page_calls.append((limit, offset))
        return InvestigationPage(
            rows=self.rows[offset : offset + limit], total=len(self.rows)
        )

    @override
    def fetch_one(self, investigation_id: str) -> StoredInvestigation | None:
        if investigation_id not in self.reports:
            return None
        return StoredInvestigation(
            investigation_id=investigation_id,
            structured_report=self.reports[investigation_id],
        )

    @override
    def delete(self, investigation_id: str) -> bool:
        if investigation_id not in self.reports:
            return False
        del self.reports[investigation_id]
        self.rows = [r for r in self.rows if r["investigation_id"] != investigation_id]
        return True


class UnreachableRepository(InvestigationRepository):
    """A repository whose every method reports the database is down.

    Mirrors what :class:`~backend.persistence.PostgresInvestigationRepository`
    does with a driver failure — it raises
    :class:`~backend.errors.RepositoryError`, never a ``psycopg2`` exception —
    so the 503 path is tested at the same boundary production crosses.
    """

    _FAILURE = "Could not reach the investigations database (OperationalError)."

    @override
    def fetch_page(self, *, limit: int, offset: int) -> InvestigationPage:
        raise RepositoryError(self._FAILURE)

    @override
    def fetch_one(self, investigation_id: str) -> StoredInvestigation | None:
        raise RepositoryError(self._FAILURE)

    @override
    def delete(self, investigation_id: str) -> bool:
        raise RepositoryError(self._FAILURE)


class RecordingGraph:
    """A stand-in for the compiled graph.

    Satisfies :class:`~backend.services.CompiledGraph` — one ``ainvoke``
    method — which is exactly why that protocol is one method wide.
    """

    def __init__(
        self,
        final_state: dict[str, Any] | None = None,
        *,
        raises: Exception | None = None,
        delay: float = 0.0,
    ) -> None:
        self.final_state = final_state if final_state is not None else {
            "db_persisted": True,
            "investigation_notes": ["Successfully persisted investigation."],
            "completed_stages": ["parser", "write_to_db"],
        }
        self.raises = raises
        self.delay = delay
        #: Every input state the runner built, so a test can assert what the
        #: graph was actually asked to analyze.
        self.invocations: list[dict[str, Any]] = []

    async def ainvoke(self, state: dict[str, Any], /, *_a: Any, **_k: Any) -> dict[str, Any]:
        self.invocations.append(state)
        if self.delay:
            await asyncio.sleep(self.delay)
        if self.raises is not None:
            raise self.raises
        return self.final_state


class StubGraphFactory(GraphFactory):
    """Hands out one :class:`RecordingGraph`, never compiling anything."""

    def __init__(self, graph: RecordingGraph | None = None) -> None:
        self.graph = graph or RecordingGraph()

    @override
    def get_graph(self) -> Any:
        return self.graph


class FailingGraphFactory(GraphFactory):
    """A factory that cannot build a graph at all."""

    @override
    def get_graph(self) -> Any:
        raise ImportError("langgraph is not installed")


class StubServiceFactory(ServiceFactory):
    """The single seam: supplies whichever services a test wants wired in."""

    def __init__(
        self,
        investigation_service: InvestigationService,
        graph_runner_service: GraphRunnerService,
    ) -> None:
        self._investigation_service = investigation_service
        self._graph_runner_service = graph_runner_service

    @override
    def create_investigation_service(self) -> InvestigationService:
        return self._investigation_service

    @override
    def create_graph_runner_service(self) -> GraphRunnerService:
        return self._graph_runner_service


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def repository() -> MemoryInvestigationRepository:
    """A repository holding two investigations, one of them with a report."""
    return MemoryInvestigationRepository(
        rows=[_row("inv-graph-aaaa", minute=1), _row("inv-graph-bbbb", minute=0)],
        reports={"inv-graph-aaaa": SAMPLE_REPORT},
    )


@pytest.fixture
def graph() -> RecordingGraph:
    """A graph that succeeds and records what it was asked to run."""
    return RecordingGraph()


def build_client(
    repository: InvestigationRepository,
    graph: RecordingGraph | None = None,
    *,
    graph_factory: GraphFactory | None = None,
    graph_timeout: float = 30.0,
    raise_server_exceptions: bool = True,
) -> TestClient:
    """Assemble an application over the given doubles.

    Everything below the factory is the *production* class: the real
    :class:`~backend.services.DefaultInvestigationService`, the real
    :class:`~backend.services.LangGraphRunnerService`, the real routes,
    middleware and exception handlers. Only the two leaves — where the database
    and the graph would be — are substituted.
    """
    settings = ApiSettings(graph_timeout=graph_timeout)
    factory = StubServiceFactory(
        DefaultInvestigationService(repository),
        LangGraphRunnerService(
            graph_factory or StubGraphFactory(graph or RecordingGraph()),
            timeout=graph_timeout,
        ),
    )
    return TestClient(
        create_app(settings, factory),
        raise_server_exceptions=raise_server_exceptions,
    )


@pytest.fixture
def client(
    repository: MemoryInvestigationRepository, graph: RecordingGraph
) -> TestClient:
    """The default application: in-memory storage, a graph that succeeds."""
    return build_client(repository, graph)


# ---------------------------------------------------------------------------
# GET /api/health
# ---------------------------------------------------------------------------


def test_health_returns_the_documented_body(client: TestClient) -> None:
    response = client.get("/api/health")

    assert response.status_code == 200
    assert response.json() == {"status": "ok", "message": "Backend is running"}


def test_health_does_not_touch_storage() -> None:
    # A liveness probe that fails when Postgres is down would take the API out
    # of a load balancer over a dependency it does not need.
    client = build_client(UnreachableRepository())

    assert client.get("/api/health").status_code == 200


# ---------------------------------------------------------------------------
# POST /api/investigate
# ---------------------------------------------------------------------------


def test_investigate_returns_the_id_and_the_persistence_flag(
    client: TestClient,
) -> None:
    response = client.post(
        "/api/investigate",
        json={
            "application_name": "checkout-api",
            "raw_logs": "ERROR boom\nINFO fine\n",
            "investigation_id": "inv-graph-fixed",
        },
    )

    assert response.status_code == 200
    body = response.json()
    assert body["investigation_id"] == "inv-graph-fixed"
    assert body["db_persisted"] is True
    assert body["investigation_notes"] == [
        "Successfully persisted investigation."
    ]


def test_investigate_passes_the_request_through_as_graph_input(
    client: TestClient, graph: RecordingGraph
) -> None:
    client.post(
        "/api/investigate",
        json={
            "application_name": "orders",
            "raw_logs": "ERROR boom",
            "analysis_mode": "deep",
            "llm_provider": "Claude",
            "enable_web_search": True,
            "investigation_id": "inv-graph-abcd",
        },
    )

    (state,) = graph.invocations
    assert state["application_name"] == "orders"
    assert state["raw_logs"] == "ERROR boom"
    assert state["analysis_mode"] == "deep"
    # Passed through unnormalized on purpose: the graph's own
    # ``normalize_provider`` resolves ``Claude`` to ``anthropic``, and a second
    # opinion in this layer would be the one that decides which providers exist.
    assert state["llm_provider"] == "Claude"
    assert state["enable_web_search"] is True
    assert state["investigation_id"] == "inv-graph-abcd"


def test_investigate_supplies_an_investigation_timestamp(
    client: TestClient, graph: RecordingGraph
) -> None:
    # The API *is* the caller the graph's ``investigation_timestamp`` documents,
    # so it records when the run started. Left unset, every stored report would
    # carry an empty timestamp in its metadata.
    client.post(
        "/api/investigate",
        json={"application_name": "orders", "raw_logs": "ERROR boom"},
    )

    (state,) = graph.invocations
    stamp = datetime.fromisoformat(state["investigation_timestamp"])
    assert stamp.tzinfo is not None


def test_investigate_writes_only_input_state_keys(
    client: TestClient, graph: RecordingGraph
) -> None:
    # Seeding a working-region field would change which pass a node thinks it
    # is running — ``search_context`` is the load-bearing example.
    client.post(
        "/api/investigate",
        json={"application_name": "orders", "raw_logs": "ERROR boom"},
    )

    (state,) = graph.invocations
    assert set(state) == {
        "application_name",
        "investigation_id",
        "raw_logs",
        "investigation_timestamp",
        "analysis_mode",
        "llm_provider",
        "enable_web_search",
    }


def test_investigate_generates_an_id_when_none_is_supplied(
    client: TestClient, graph: RecordingGraph
) -> None:
    response = client.post(
        "/api/investigate",
        json={"application_name": "orders", "raw_logs": "ERROR boom"},
    )

    generated = response.json()["investigation_id"]
    assert generated.startswith(GENERATED_ID_PREFIX)
    assert len(generated) == len(GENERATED_ID_PREFIX) + GENERATED_ID_CHARS
    # The graph must be invoked under the same key the response reports, or the
    # client cannot fetch back what it just created.
    assert graph.invocations[0]["investigation_id"] == generated


def test_investigate_applies_the_documented_defaults(
    client: TestClient, graph: RecordingGraph
) -> None:
    client.post(
        "/api/investigate",
        json={"application_name": "orders", "raw_logs": "ERROR boom"},
    )

    (state,) = graph.invocations
    assert state["analysis_mode"] == "standard"
    assert state["llm_provider"] == "openai"
    assert state["enable_web_search"] is False


def test_investigate_reports_a_failed_write_as_a_successful_request() -> None:
    # Scenario B: the analysis ran, the storage did not. That is a 200 carrying
    # bad news, and the notes are what the UI puts in its warning toast.
    graph = RecordingGraph(
        {
            "db_persisted": False,
            "investigation_notes": [
                "Write to DB: could not persist investigation to PostgreSQL."
            ],
        }
    )
    client = build_client(MemoryInvestigationRepository(), graph)

    response = client.post(
        "/api/investigate",
        json={"application_name": "orders", "raw_logs": "ERROR boom"},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["db_persisted"] is False
    assert "could not persist" in body["investigation_notes"][0]


def test_investigate_tolerates_a_final_state_missing_every_optional_key() -> None:
    # By the time the graph returns, the investigation is complete. A KeyError
    # here would turn a finished run into a 500 reporting that nothing was done.
    client = build_client(MemoryInvestigationRepository(), RecordingGraph({}))

    response = client.post(
        "/api/investigate",
        json={"application_name": "orders", "raw_logs": "ERROR boom"},
    )

    assert response.status_code == 200
    assert response.json()["db_persisted"] is False
    assert response.json()["investigation_notes"] == []


@pytest.mark.parametrize(
    ("payload", "field"),
    [
        ({"raw_logs": "ERROR boom"}, "application_name"),
        ({"application_name": "orders"}, "raw_logs"),
        ({"application_name": "", "raw_logs": "x"}, "application_name"),
        ({"application_name": "orders", "raw_logs": ""}, "raw_logs"),
        (
            {"application_name": "orders", "raw_logs": "x", "investigation_id": ""},
            "investigation_id",
        ),
        (
            {
                "application_name": "orders",
                "raw_logs": "x",
                "investigation_id": "i" * 256,
            },
            "investigation_id",
        ),
        (
            {"application_name": "orders", "raw_logs": "x", "enable_web_search": "nope"},
            "enable_web_search",
        ),
    ],
)
def test_investigate_rejects_an_invalid_body(
    client: TestClient, payload: dict[str, Any], field: str
) -> None:
    response = client.post("/api/investigate", json=payload)

    assert response.status_code == 422
    assert field in str(response.json()["detail"])


def test_investigate_rejects_an_unknown_field(client: TestClient) -> None:
    # ``extra="forbid"`` turns a client-side typo into a 422 naming the key,
    # rather than a silently ignored field and a run that used defaults the
    # caller did not intend.
    response = client.post(
        "/api/investigate",
        json={
            "application_name": "orders",
            "raw_logs": "x",
            "analysis_modes": "deep",
        },
    )

    assert response.status_code == 422
    assert "analysis_modes" in str(response.json()["detail"])


def test_investigate_reports_a_timeout_as_504() -> None:
    client = build_client(
        MemoryInvestigationRepository(),
        RecordingGraph(delay=0.5),
        graph_timeout=0.05,
    )

    response = client.post(
        "/api/investigate",
        json={"application_name": "orders", "raw_logs": "ERROR boom"},
    )

    assert response.status_code == 504
    assert "did not finish" in response.json()["detail"]


def test_investigate_reports_an_unbuildable_graph_as_500() -> None:
    client = build_client(
        MemoryInvestigationRepository(), graph_factory=FailingGraphFactory()
    )

    response = client.post(
        "/api/investigate",
        json={"application_name": "orders", "raw_logs": "ERROR boom"},
    )

    assert response.status_code == 500
    assert "could not be started" in response.json()["detail"]


def test_investigate_reports_a_raising_graph_as_500() -> None:
    client = build_client(
        MemoryInvestigationRepository(),
        RecordingGraph(raises=RuntimeError("superstep exploded")),
    )

    response = client.post(
        "/api/investigate",
        json={"application_name": "orders", "raw_logs": "ERROR boom"},
    )

    assert response.status_code == 500
    detail = response.json()["detail"]
    assert "pipeline failed" in detail
    assert "superstep exploded" in detail


# ---------------------------------------------------------------------------
# POST /api/investigations  (the record list)
# ---------------------------------------------------------------------------


def test_list_returns_a_page_of_metadata(client: TestClient) -> None:
    response = client.post("/api/investigations", json={"page": 1, "limit": 10})

    assert response.status_code == 200
    body = response.json()
    assert body["total"] == 2
    assert body["page"] == 1
    assert body["limit"] == 10
    assert body["total_pages"] == 1
    assert [item["investigation_id"] for item in body["items"]] == [
        "inv-graph-aaaa",
        "inv-graph-bbbb",
    ]


def test_list_never_carries_the_structured_report(client: TestClient) -> None:
    # The whole reason the list and the detail endpoints are separate: a stored
    # report runs to megabytes and this is what a UI renders on first paint.
    response = client.post("/api/investigations", json={})

    for item in response.json()["items"]:
        assert "structured_report" not in item
        assert set(item) == {
            "investigation_id",
            "application_name",
            "confidence_score",
            "analysis_mode",
            "llm_provider",
            "created_at",
            "updated_at",
        }


def test_list_defaults_when_the_body_is_empty_or_absent(client: TestClient) -> None:
    for response in (
        client.post("/api/investigations", json={}),
        client.post("/api/investigations"),
    ):
        assert response.status_code == 200
        assert response.json()["page"] == 1
        assert response.json()["limit"] == 10


def test_list_computes_the_offset_from_the_page(
    client: TestClient, repository: MemoryInvestigationRepository
) -> None:
    client.post("/api/investigations", json={"page": 3, "limit": 5})

    assert repository.page_calls == [(5, 10)]


def test_list_rounds_total_pages_up(client: TestClient) -> None:
    response = client.post("/api/investigations", json={"page": 1, "limit": 1})

    body = response.json()
    assert body["total"] == 2
    assert body["total_pages"] == 2
    assert len(body["items"]) == 1


def test_list_reports_zero_pages_for_an_empty_table() -> None:
    # ``0`` rather than ``1``, so a client can test the field directly instead
    # of special-casing "one page that happens to contain nothing".
    client = build_client(MemoryInvestigationRepository())

    body = client.post("/api/investigations", json={}).json()
    assert body == {"items": [], "total": 0, "page": 1, "limit": 10, "total_pages": 0}


def test_list_serves_an_out_of_range_page_with_the_real_total(
    client: TestClient,
) -> None:
    # What lets a client recover from asking for a page that no longer exists.
    body = client.post("/api/investigations", json={"page": 99, "limit": 10}).json()

    assert body["items"] == []
    assert body["total"] == 2


def test_list_preserves_a_null_confidence_score() -> None:
    # ``None`` means "not measured" and ``0`` means "measured as zero". The
    # distinction survives all the way to the client.
    client = build_client(
        MemoryInvestigationRepository(rows=[_row("inv-x", confidence_score=None)])
    )

    (item,) = client.post("/api/investigations", json={}).json()["items"]
    assert item["confidence_score"] is None


@pytest.mark.parametrize(
    "payload",
    [
        {"page": 0},
        {"page": -1},
        {"limit": 0},
        {"limit": MAX_LIMIT + 1},
        {"page": "first"},
        {"pages": 2},
    ],
)
def test_list_rejects_invalid_pagination(
    client: TestClient, payload: dict[str, Any]
) -> None:
    assert client.post("/api/investigations", json=payload).status_code == 422


def test_list_reports_an_unreachable_database_as_503() -> None:
    client = build_client(UnreachableRepository())

    response = client.post("/api/investigations", json={})

    assert response.status_code == 503
    assert "database" in response.json()["detail"]


# ---------------------------------------------------------------------------
# POST /api/investigations/{id}  (the record detail)
# ---------------------------------------------------------------------------


def test_detail_returns_the_report_verbatim(client: TestClient) -> None:
    response = client.post("/api/investigations/inv-graph-aaaa", json={})

    assert response.status_code == 200
    body = response.json()
    assert body["investigation_id"] == "inv-graph-aaaa"
    # Verbatim: nothing summarized, reshaped or dropped on the way out.
    assert body["structured_report"] == SAMPLE_REPORT


def test_detail_carries_all_four_provenance_sections(client: TestClient) -> None:
    report = client.post("/api/investigations/inv-graph-aaaa", json={}).json()[
        "structured_report"
    ]

    assert set(report) == {
        "metadata",
        "synthesis",
        "deterministic_outputs",
        "ai_insights",
    }


def test_detail_accepts_an_absent_body(client: TestClient) -> None:
    assert client.post("/api/investigations/inv-graph-aaaa").status_code == 200


def test_detail_returns_404_for_an_unknown_id(client: TestClient) -> None:
    response = client.post("/api/investigations/does-not-exist", json={})

    assert response.status_code == 404
    assert response.json() == {"detail": "Investigation not found"}


def test_detail_returns_404_for_a_row_that_was_listed_but_has_no_report(
    client: TestClient,
) -> None:
    # ``inv-graph-bbbb`` is in the list fixture but has no stored report, which
    # in this double means no row — the same 404 the real repository produces
    # for an id that is not there.
    assert client.post("/api/investigations/inv-graph-bbbb", json={}).status_code == 404


def test_detail_rejects_an_over_long_id(client: TestClient) -> None:
    response = client.post(f"/api/investigations/{'i' * 256}", json={})

    assert response.status_code == 422


def test_detail_reports_an_unreachable_database_as_503() -> None:
    client = build_client(UnreachableRepository())

    assert client.post("/api/investigations/anything", json={}).status_code == 503


# ---------------------------------------------------------------------------
# DELETE /api/investigations/{id}
# ---------------------------------------------------------------------------


def test_delete_removes_the_record(
    client: TestClient, repository: MemoryInvestigationRepository
) -> None:
    response = client.delete("/api/investigations/inv-graph-aaaa")

    assert response.status_code == 200
    assert response.json() == {
        "status": "deleted",
        "investigation_id": "inv-graph-aaaa",
    }
    assert "inv-graph-aaaa" not in repository.reports


def test_delete_is_reflected_in_the_list(client: TestClient) -> None:
    client.delete("/api/investigations/inv-graph-aaaa")

    body = client.post("/api/investigations", json={}).json()
    assert body["total"] == 1
    assert [item["investigation_id"] for item in body["items"]] == ["inv-graph-bbbb"]


def test_delete_returns_404_for_an_unknown_id(client: TestClient) -> None:
    response = client.delete("/api/investigations/does-not-exist")

    assert response.status_code == 404
    assert response.json() == {"detail": "Investigation not found"}


def test_delete_is_not_idempotent_by_design(client: TestClient) -> None:
    # The second delete is a 404 rather than a second 200: a client removing
    # something it could not see is looking at a stale list, and saying so is
    # what prompts the refresh.
    assert client.delete("/api/investigations/inv-graph-aaaa").status_code == 200
    assert client.delete("/api/investigations/inv-graph-aaaa").status_code == 404


def test_delete_reports_an_unreachable_database_as_503() -> None:
    client = build_client(UnreachableRepository())

    assert client.delete("/api/investigations/anything").status_code == 503


# ---------------------------------------------------------------------------
# Error envelope
# ---------------------------------------------------------------------------


def test_every_error_uses_the_same_detail_envelope(client: TestClient) -> None:
    responses = [
        client.post("/api/investigations/nope", json={}),          # 404
        client.post("/api/investigations", json={"page": 0}),      # 422
        client.get("/api/no-such-endpoint"),                       # 404 (routing)
    ]

    for response in responses:
        assert response.status_code >= 400
        assert "detail" in response.json()


def test_an_unexpected_exception_becomes_a_500_without_a_traceback() -> None:
    # A stack trace in an HTTP body names file paths and library versions and
    # is useless to the browser that receives it. It belongs in the log.
    class ExplodingRepository(MemoryInvestigationRepository):
        @override
        def fetch_page(self, *, limit: int, offset: int) -> InvestigationPage:
            raise ValueError("something nobody anticipated")

    client = build_client(ExplodingRepository(), raise_server_exceptions=False)

    response = client.post("/api/investigations", json={})

    assert response.status_code == 500
    detail = response.json()["detail"]
    assert "Internal server error" in detail
    assert "something nobody anticipated" not in detail


# ---------------------------------------------------------------------------
# CORS
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "origin", ["http://localhost:3000", "http://localhost:5173"]
)
def test_cors_allows_the_react_development_origins(
    client: TestClient, origin: str
) -> None:
    response = client.options(
        "/api/investigations",
        headers={
            "Origin": origin,
            "Access-Control-Request-Method": "POST",
            "Access-Control-Request-Headers": "content-type",
        },
    )

    assert response.status_code == 200
    assert response.headers["access-control-allow-origin"] == origin
    assert "POST" in response.headers["access-control-allow-methods"]


def test_cors_echoes_the_origin_on_a_real_request(client: TestClient) -> None:
    response = client.get(
        "/api/health", headers={"Origin": "http://localhost:5173"}
    )

    assert response.headers["access-control-allow-origin"] == "http://localhost:5173"
    assert response.headers["access-control-allow-credentials"] == "true"


def test_cors_does_not_advertise_an_unknown_origin(client: TestClient) -> None:
    response = client.get(
        "/api/health", headers={"Origin": "http://evil.example.com"}
    )

    # The request still succeeds — CORS is enforced by the browser, not the
    # server — but the header that would authorize it is absent.
    assert response.status_code == 200
    assert "access-control-allow-origin" not in response.headers


# ---------------------------------------------------------------------------
# Service layer: the overloaded entry points
# ---------------------------------------------------------------------------


def test_fetch_dispatches_on_its_call_shape(
    repository: MemoryInvestigationRepository,
) -> None:
    service = DefaultInvestigationService(repository)

    detail = service.fetch("inv-graph-aaaa")
    page = service.fetch(page=1, limit=10)

    # Same name, two call shapes, two response types — which is the whole
    # reason ``typing.overload`` is applied here rather than a union return.
    assert detail.structured_report == SAMPLE_REPORT
    assert page.total == 2


def test_fetch_defaults_to_the_first_page(
    repository: MemoryInvestigationRepository,
) -> None:
    page = DefaultInvestigationService(repository).fetch()

    assert (page.page, page.limit) == (1, 10)


def test_fetch_refuses_a_call_matching_neither_overload(
    repository: MemoryInvestigationRepository,
) -> None:
    service = DefaultInvestigationService(repository)

    with pytest.raises(TypeError, match="not both"):
        service.fetch("inv-graph-aaaa", page=2)  # type: ignore[call-overload]


def test_execute_accepts_a_request_model_or_keyword_fields() -> None:
    graph = RecordingGraph()
    service = LangGraphRunnerService(StubGraphFactory(graph))

    from_model = asyncio.run(
        service.execute(
            InvestigateRequest(application_name="a", raw_logs="ERROR boom")
        )
    )
    from_fields = asyncio.run(
        service.execute(application_name="a", raw_logs="ERROR boom")
    )

    assert from_model.db_persisted is True
    assert from_fields.db_persisted is True
    assert len(graph.invocations) == 2


def test_execute_validates_keyword_fields_through_the_same_model() -> None:
    # The keyword form must not be a way around ``min_length`` on ``raw_logs``.
    service = LangGraphRunnerService(StubGraphFactory())

    with pytest.raises(Exception) as caught:
        asyncio.run(service.execute(application_name="a", raw_logs=""))

    assert "raw_logs" in str(caught.value)


def test_execute_refuses_a_call_matching_neither_overload() -> None:
    service = LangGraphRunnerService(StubGraphFactory())
    request = InvestigateRequest(application_name="a", raw_logs="x")

    with pytest.raises(TypeError, match="not both"):
        asyncio.run(service.execute(request, application_name="b"))  # type: ignore[call-overload]

    with pytest.raises(TypeError, match="requires"):
        asyncio.run(service.execute())  # type: ignore[call-overload]


def test_execute_never_replaces_a_supplied_id() -> None:
    service = LangGraphRunnerService(StubGraphFactory())

    response = asyncio.run(
        service.execute(
            application_name="a", raw_logs="x", investigation_id="chosen-by-caller"
        )
    )

    assert response.investigation_id == "chosen-by-caller"


def test_generated_ids_use_the_documented_format() -> None:
    generated = {generate_investigation_id() for _ in range(50)}

    for value in generated:
        assert value.startswith(GENERATED_ID_PREFIX)
        assert len(value) == len(GENERATED_ID_PREFIX) + GENERATED_ID_CHARS
    # Not a uniqueness guarantee — 4 hex characters is 65,536 values and
    # collisions are expected at scale (see GENERATED_ID_CHARS) — but 50 draws
    # colliding would mean the generator is not random at all.
    assert len(generated) > 40


# ---------------------------------------------------------------------------
# Architecture
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "interface",
    [
        InvestigationRepository,
        InvestigationService,
        GraphRunnerService,
        GraphFactory,
        ServiceFactory,
    ],
)
def test_the_interfaces_cannot_be_instantiated(interface: type) -> None:
    # Each is an ABC with at least one abstract method, so a partial
    # implementation fails at construction rather than at the first call.
    with pytest.raises(TypeError):
        interface()  # type: ignore[abstract]


def test_the_openapi_schema_documents_every_endpoint(client: TestClient) -> None:
    paths = client.get("/openapi.json").json()["paths"]

    assert set(paths) == {
        "/api/health",
        "/api/investigate",
        "/api/investigations",
        "/api/investigations/{investigation_id}",
    }
    assert set(paths["/api/investigations/{investigation_id}"]) == {"post", "delete"}
