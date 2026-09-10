# =============================================================================
# LogSherlock — the Python half of the stack
# =============================================================================
# Two services are built from this one file, as two targets over a shared
# ``base``:
#
#     docker build --target backend  -t logsherlock-backend .
#     docker build --target mock-llm -t logsherlock-mock-llm .
#
# One file rather than two, and that is a deliberate choice worth stating. The
# API and the mock provider are the *same source tree* — ``tests/mock_local_llm.py``
# imports ``graph_library.env_files`` and answers the same Pydantic schemas the
# graph's nodes send — and they need the same interpreter and the same
# dependency set. Two Dockerfiles would install that set twice, cache it twice,
# and drift apart the first time one of them was edited. What actually differs
# between the two services is one ``CMD``, so one ``CMD`` is what differs here.
#
# The env files are deliberately *not* copied in; see ``.dockerignore``.
# Configuration arrives from Compose at run time, which is the case
# ``graph_library/env_files.py`` documents as "an orchestrator or a secrets
# manager provides it".
# =============================================================================

# -----------------------------------------------------------------------------
# base — the interpreter, the dependencies and the source, shared by both
# -----------------------------------------------------------------------------
FROM python:3.12-slim AS base

# ``PYTHONUNBUFFERED`` is the one that matters operationally. Both entry points
# report their configuration on stdout *before* logging is configured — the
# ``[Config]`` line, the startup banner, ``[LogSherlock DB]`` — and stdout is
# block-buffered whenever it is not a terminal, which is every container. Without
# this, those lines sit in a buffer while uvicorn's stderr streams past them and
# ``docker compose logs`` shows the banner only when the process exits.
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

# The dependency layer, kept separate from the source layer so that editing a
# node does not reinstall pandas. ``pip install .`` needs the packages present
# to resolve ``[tool.setuptools.packages.find]``, so the manifest cannot be
# installed entirely on its own — but copying the manifest plus the two package
# trees, and *then* the rest, still keeps the expensive layer cached across
# every change to a root script, a test or a sample log.
#
# Extras: every provider, because ``llm_factory`` imports them lazily and an
# image that ships only one would fail on a run the API happily accepts. Plus
# ``dev``, which is not only about tests — ``graph_library/stats/aggregations.py``
# imports pandas at run time, and ``httpx`` is what the mock's client path uses.
COPY pyproject.toml README.md LICENSE ./
COPY graph_library/ ./graph_library/
COPY backend/ ./backend/
# ``rm -rf build *.egg-info`` in the same layer, and it is not housekeeping.
# setuptools builds the wheel *in tree*, so ``pip install .`` leaves a verbatim
# second copy of every module under ``/app/build/lib/`` — the same reason
# ``build/`` is in the project's ``.gitignore``. Left in place it ships a stale
# duplicate of the whole source tree in the image, and
# ``tests/test_models_architecture.py`` catches it exactly as designed: six of
# its checks fail on "expected 1 definition, found 2". Removing it in the same
# ``RUN`` matters too — a separate layer would delete the files from the
# filesystem while the layer holding them still travels with the image.
RUN pip install --no-cache-dir ".[openai,anthropic,gemini,search,dev]" \
    && rm -rf build *.egg-info

# The rest of the tree: the root entry points, the graph module, the mock
# provider and the log corpus the mock and the tests read.
COPY graph.py backend.py init_db.py langgraph.json ./
COPY tests/ ./tests/
COPY sample_logs/ ./sample_logs/

# Nothing here needs to write to the image, and a process that cannot write to
# its own source tree cannot be persuaded to. ``--system`` because this account
# is a service identity, not a login.
RUN useradd --system --create-home --shell /usr/sbin/nologin appuser \
    && chown -R appuser:appuser /app
USER appuser

# -----------------------------------------------------------------------------
# backend — the FastAPI application
# -----------------------------------------------------------------------------
FROM base AS backend

# Documentation for a reader and for ``docker inspect``; publishing is Compose's
# job. The bind address is not baked in: ``API_HOST`` must be ``0.0.0.0`` in a
# container (the code's default is loopback, deliberately, because it holds a
# database credential), and Compose sets it. A default here would hide that
# decision inside an image layer.
EXPOSE 8010

# The project's own entry point rather than a bare ``uvicorn`` invocation. It is
# what resolves and *reports* the environment file, prints the banner naming the
# resolved host, port, origins and database, and translates a failed bind into
# one actionable sentence. Calling uvicorn directly would skip all of it and
# silently pick different defaults for the keep-alive budget.
CMD ["python3", "backend.py"]

# -----------------------------------------------------------------------------
# mock-llm — the offline OpenAI-compatible provider
# -----------------------------------------------------------------------------
FROM base AS mock-llm

# It lives under ``tests/`` and is still a runtime component rather than a test
# fixture: it is how the ``local`` provider is served with no API key and no
# network, which is what makes this stack demonstrable offline. ``fastapi`` and
# ``uvicorn`` are core dependencies of the project for exactly this reason.
EXPOSE 8000

CMD ["python3", "tests/mock_local_llm.py"]
