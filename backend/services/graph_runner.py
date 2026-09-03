"""Running the LangGraph pipeline behind ``POST /api/investigate``.

This is the only module in the backend that knows the graph exists. It builds
the input state, invokes the compiled graph, and reads three values back out of
the final state; everything else about the pipeline — eight nodes, four LLM
calls, an optional web search and a database write — is the engine's business.

``graph.py`` is not modified and not reimplemented. The graph object arrives
through a :class:`~backend.factories.GraphFactory`, so this service never
compiles anything itself and a test can hand it a stub.

Three decisions shape the implementation:

    * **The invocation is awaited, not blocked on.** ``ainvoke`` lets LangGraph
      run the sync node functions on worker threads while the event loop stays
      free, which matters because this endpoint holds a connection open for as
      long as the slowest provider call takes. A synchronous ``invoke`` in an
      ``async def`` would stall every other request on the process for minutes.
    * **The run has a deadline.** Every node degrades rather than raises, so a
      wedged provider socket does not surface as an error — it surfaces as a
      request that never returns. :class:`~backend.errors.GraphTimeoutError` is
      the only thing standing between that and a permanently pinned worker.
    * **A failed run is not a failed request, unless the graph itself failed.**
      ``db_persisted: False`` is a *successful* 200 carrying bad news, because
      the investigation ran and its notes explain why it was not stored. Only an
      exception escaping the graph is a 5xx.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from abc import ABC, abstractmethod
from datetime import datetime, timezone
from typing import Any, Protocol, overload, override

from ..errors import GraphExecutionError, GraphTimeoutError
from ..schemas import InvestigateRequest, InvestigateResponse

logger = logging.getLogger(__name__)

#: Prefix on an id this API generated rather than received, matching the
#: convention :mod:`graph_library.write_to_db.node` uses so an operator reading
#: a stored row can tell a key their system chose from one that was invented for
#: them.
GENERATED_ID_PREFIX = "inv-graph-"

#: Characters of the UUID kept after the prefix.
#:
#: Four is the specified format and it is a deliberately small keyspace — 65,536
#: values — so collisions are not remote: at a few hundred stored
#: investigations the birthday bound makes one likely. Because the write is an
#: idempotent upsert keyed on this id, a collision *overwrites* the earlier
#: investigation rather than failing. Callers that need durable, non-colliding
#: keys should supply ``investigation_id`` themselves, which is never replaced.
GENERATED_ID_CHARS = 4


class CompiledGraph(Protocol):
    """The slice of a compiled LangGraph this service actually uses.

    A ``Protocol`` rather than an import of
    :class:`~langgraph.graph.state.CompiledStateGraph`, so the service depends
    on the one method it calls instead of on LangGraph's class hierarchy — which
    is also what makes a two-line stub a valid graph in a test.
    """

    async def ainvoke(self, input: dict[str, Any], /, *args: Any, **kwargs: Any) -> dict[str, Any]:
        """Run the graph to completion and return the final state."""
        ...


def generate_investigation_id() -> str:
    """Mint an id for a request that supplied none.

    Format is ``inv-graph-<4 hex chars>``, from :func:`uuid.uuid4`. See
    :data:`GENERATED_ID_CHARS` for the collision trade-off this accepts.

    Generating rather than refusing is what makes persistence the default: a UI
    with no id field would otherwise produce complete investigations that are
    never stored.
    """
    return f"{GENERATED_ID_PREFIX}{str(uuid.uuid4())[:GENERATED_ID_CHARS]}"


class GraphRunnerService(ABC):
    """Run one investigation through the LogSherlock graph."""

    @overload
    async def execute(
        self, request: InvestigateRequest, /
    ) -> InvestigateResponse:
        """Run the graph from a validated request model."""

    @overload
    async def execute(
        self,
        /,
        *,
        application_name: str,
        raw_logs: str,
        analysis_mode: str = ...,
        llm_provider: str = ...,
        enable_web_search: bool = ...,
        investigation_id: str | None = ...,
    ) -> InvestigateResponse:
        """Run the graph from loose keyword fields."""

    @abstractmethod
    async def execute(
        self,
        request: InvestigateRequest | None = None,
        /,
        **fields: Any,
    ) -> InvestigateResponse:
        """Run one investigation.

        Two call shapes, because two kinds of caller need this. A route handler
        already holds a validated :class:`~backend.schemas.InvestigateRequest`
        and passes it whole; a script, a test or a future scheduled job holds
        loose values and should not have to import a request model to run the
        pipeline. The keyword form validates through the same model, so neither
        path can skip a constraint the other enforces.

        Args:
            request: Positional-only. A complete, validated request.
            **fields: The request's fields, when no model is passed.

        Returns:
            The investigation id, whether it was persisted, and the notes every
            node recorded.

        Raises:
            GraphTimeoutError: If the run exceeded the configured deadline.
            GraphExecutionError: If the graph could not be built or run.
            TypeError: If both a request and keyword fields are supplied, or
                neither.
        """


class LangGraphRunnerService(GraphRunnerService):
    """The implementation, over the compiled LogSherlock graph."""

    def __init__(
        self,
        graph_factory: GraphFactoryProtocol,
        *,
        timeout: float | None = None,
    ) -> None:
        """Bind the service to a source of compiled graphs.

        Args:
            graph_factory: Supplies the compiled graph. A factory rather than
                the graph itself so compilation is deferred to the first
                invocation and then cached — building it at import time would
                make every ``import backend`` pay for the whole node stack,
                including pandas.
            timeout: Seconds a single run may take. ``None`` or a
                non-positive value disables the deadline.
        """
        self._graph_factory = graph_factory
        self._timeout = timeout if timeout and timeout > 0 else None

    @overload
    async def execute(
        self, request: InvestigateRequest, /
    ) -> InvestigateResponse: ...

    @overload
    async def execute(
        self,
        /,
        *,
        application_name: str,
        raw_logs: str,
        analysis_mode: str = ...,
        llm_provider: str = ...,
        enable_web_search: bool = ...,
        investigation_id: str | None = ...,
    ) -> InvestigateResponse: ...

    @override
    async def execute(
        self,
        request: InvestigateRequest | None = None,
        /,
        **fields: Any,
    ) -> InvestigateResponse:
        request = self._resolve_request(request, fields)

        # Generated here rather than left to the ``write_to_db`` node, because
        # the response has to carry the key the report was stored under and the
        # node reports its own generated id only in a note. Supplying one always
        # means the API and the database never disagree about the primary key.
        investigation_id = request.investigation_id or generate_investigation_id()
        if request.investigation_id is None:
            logger.info(
                "No investigation_id supplied; generated %r", investigation_id
            )

        state = self._build_input_state(request, investigation_id)
        logger.info(
            "Invoking the graph: investigation_id=%s application=%s provider=%s "
            "mode=%s web_search=%s raw_log_chars=%d timeout=%s",
            investigation_id,
            request.application_name,
            request.llm_provider,
            request.analysis_mode,
            request.enable_web_search,
            len(request.raw_logs),
            self._timeout or "none",
        )

        final_state = await self._invoke(state, investigation_id)
        return self._build_response(final_state, investigation_id)

    # -- internals ----------------------------------------------------------

    @staticmethod
    def _resolve_request(
        request: InvestigateRequest | None, fields: dict[str, Any]
    ) -> InvestigateRequest:
        """Collapse the two call shapes onto one validated request.

        The keyword form is validated through the same model rather than used
        raw, so a caller that bypasses HTTP cannot bypass ``min_length`` on
        ``raw_logs`` either.
        """
        if request is not None and fields:
            raise TypeError(
                "execute() takes either an InvestigateRequest or keyword "
                "fields, not both"
            )
        if request is not None:
            return request
        if not fields:
            raise TypeError(
                "execute() requires an InvestigateRequest or the request's "
                "keyword fields"
            )
        return InvestigateRequest(**fields)

    @staticmethod
    def _build_input_state(
        request: InvestigateRequest, investigation_id: str
    ) -> dict[str, Any]:
        """Render the request as the graph's INPUT region.

        Only input keys are written. The working and output regions belong to
        the nodes, and seeding one from here — ``search_context``, say — would
        change which pass the error-analysis node thinks it is running.
        """
        return {
            "application_name": request.application_name,
            "investigation_id": investigation_id,
            "raw_logs": request.raw_logs,
            # The API *is* the caller the graph's ``investigation_timestamp``
            # documents, and it knows when the run started, so recording it here
            # is reporting a fact rather than inventing one. Left unset, every
            # stored report would carry an empty timestamp in its metadata.
            "investigation_timestamp": datetime.now(timezone.utc).isoformat(),
            "analysis_mode": request.analysis_mode,
            "llm_provider": request.llm_provider,
            "enable_web_search": request.enable_web_search,
        }

    async def _invoke(
        self, state: dict[str, Any], investigation_id: str
    ) -> dict[str, Any]:
        """Run the graph under the configured deadline."""
        try:
            graph = self._graph_factory.get_graph()
        except Exception as exc:  # noqa: BLE001 - reported as a 500, not a crash
            logger.error("Could not build the LogSherlock graph", exc_info=True)
            raise GraphExecutionError(
                f"The analysis pipeline could not be started "
                f"({type(exc).__name__}: {exc})."
            ) from exc

        try:
            if self._timeout is None:
                return await graph.ainvoke(state)
            async with asyncio.timeout(self._timeout):
                return await graph.ainvoke(state)
        except TimeoutError as exc:
            # ``asyncio.timeout`` raises the builtin ``TimeoutError`` on 3.11+.
            logger.error(
                "Graph invocation for %s exceeded %.0fs",
                investigation_id,
                self._timeout or 0.0,
                exc_info=True,
            )
            raise GraphTimeoutError(
                f"The analysis did not finish within {self._timeout:.0f} "
                "seconds. Try a smaller log payload, a faster analysis_mode, "
                "or raise API_GRAPH_TIMEOUT."
            ) from exc
        except Exception as exc:  # noqa: BLE001 - every node degrades; this is the rest
            logger.error(
                "Graph invocation for %s failed", investigation_id, exc_info=True
            )
            raise GraphExecutionError(
                f"The analysis pipeline failed ({type(exc).__name__}: {exc})."
            ) from exc

    @staticmethod
    def _build_response(
        final_state: dict[str, Any], investigation_id: str
    ) -> InvestigateResponse:
        """Read the three values the client needs out of the final state.

        Read defensively even though the topology guarantees ``write_to_db``
        ran: by this point the investigation is complete and possibly stored,
        and a ``KeyError`` here would turn a finished run into a 500 that tells
        the client nothing was done.
        """
        state = final_state if isinstance(final_state, dict) else {}
        persisted = bool(state.get("db_persisted"))
        notes = [str(note) for note in (state.get("investigation_notes") or [])]

        logger.info(
            "Graph complete: investigation_id=%s db_persisted=%s stages=%s "
            "notes=%d",
            investigation_id,
            persisted,
            state.get("completed_stages"),
            len(notes),
        )
        return InvestigateResponse(
            investigation_id=investigation_id,
            db_persisted=persisted,
            investigation_notes=notes,
        )


class GraphFactoryProtocol(Protocol):
    """The one method :class:`LangGraphRunnerService` needs from a factory.

    Declared here rather than imported from :mod:`backend.factories` so the
    dependency arrow runs one way: factories know about services, services do
    not know about factories.
    """

    def get_graph(self) -> CompiledGraph:
        """Return the compiled graph, building it on first use."""
        ...


__all__ = [
    "GENERATED_ID_CHARS",
    "GENERATED_ID_PREFIX",
    "CompiledGraph",
    "GraphFactoryProtocol",
    "GraphRunnerService",
    "LangGraphRunnerService",
    "generate_investigation_id",
]
