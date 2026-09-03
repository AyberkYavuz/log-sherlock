"""Run the LogSherlock HTTP API.

    python3 backend.py

That is the whole interface. Every setting is optional and every default is a
working local development value, so the server starts against a local
PostgreSQL and the graph's own provider defaults with no configuration at all.

Configuration, all optional, all read from the environment (and therefore from
``.env``, which this script loads):

    ``API_HOST``               bind address           (default ``127.0.0.1``)
    ``API_PORT``               bind port              (default ``8010``)
    ``API_CORS_ORIGINS``       comma-separated list   (default: CRA + Vite)
    ``API_KEEP_ALIVE_TIMEOUT`` idle connection budget (default ``75`` seconds)
    ``API_GRAPH_TIMEOUT``      per-run deadline       (default ``900`` seconds)
    ``API_LOG_LEVEL``          uvicorn log level      (default ``info``)
    ``API_RELOAD``             auto-reload on edit    (default off)

The ``DB_*`` variables are read by :mod:`graph_library.write_to_db` and are the
same ones ``init_db.py`` and the ``write_to_db`` node use. Run ``init_db.py``
once before the first investigation, or the storage endpoints will report an
unavailable database against a table that does not exist yet.

**This script loads ``.env``; no module in ``backend/`` does.** That is the rule
the whole project holds — ``load_dotenv`` mutates ``os.environ`` for the entire
process, so a library that calls it injects every key in the file, provider
credentials included, into a process that deliberately did not set them.
Populating the environment is an entry point's job, and this is the entry point.

A note on the name. This file and the ``backend/`` package share one, and Python
resolves that in the package's favour: ``import backend`` always finds the
package. The collision is harmless because this file is only ever *executed*
(where it is ``__main__``) and never imported.
"""

from __future__ import annotations

import logging
import sys

import uvicorn

from backend import ApiSettings, create_app
from graph_library.write_to_db import load_env_file

#: The import string uvicorn needs in order to re-import the application on
#: every file change. Only used under ``API_RELOAD``: the reloader runs the
#: server in a child process, so it cannot be handed an already-constructed
#: application object. ``factory=True`` because ``create_app`` is a function.
APP_FACTORY_PATH = "backend.app:create_app"

EXIT_OK = 0
EXIT_FAILED = 1


def main() -> int:
    """Configure and run the API server.

    Returns:
        A process exit code. ``0`` on a clean shutdown, ``1`` when the server
        could not start — reported as one actionable sentence rather than as a
        traceback whose last frame is inside uvicorn.
    """
    load_env_file()
    settings = ApiSettings.from_env()

    logging.basicConfig(
        level=settings.log_level.upper(),
        format="%(asctime)s %(levelname)-8s %(name)s: %(message)s",
    )

    print(f"LogSherlock API starting on http://{settings.bind_target}")
    print(f"  Interactive docs: http://{settings.bind_target}/docs")
    print(f"  Health check:     http://{settings.bind_target}/api/health")
    print(f"  Allowed origins:  {', '.join(settings.cors_origins)}")

    try:
        uvicorn.run(
            # Under the reloader uvicorn needs an import string; otherwise the
            # application is built here so a construction failure surfaces now,
            # with a message, rather than inside a worker.
            APP_FACTORY_PATH if settings.reload else create_app(settings),
            host=settings.host,
            port=settings.port,
            reload=settings.reload,
            factory=settings.reload,
            log_level=settings.log_level,
            # Raised well above uvicorn's default 5 seconds because a single
            # ``POST /api/investigate`` runs the whole graph. Note this bounds
            # the *idle* period between requests, not a handler's runtime:
            # uvicorn imposes no ceiling on the latter, which is exactly what a
            # minutes-long analysis needs. The per-run deadline is the API's own
            # ``API_GRAPH_TIMEOUT``.
            timeout_keep_alive=settings.keep_alive_timeout,
        )
    except KeyboardInterrupt:  # pragma: no cover - interactive
        print("\nShutting down.")
        return EXIT_OK
    except OSError as exc:
        # Overwhelmingly "address already in use" — worth naming, because the
        # default traceback buries it under a socket call stack.
        print(f"\nFAILED to bind {settings.bind_target}: {exc}", file=sys.stderr)
        print(
            "Another process is probably using that port. Set API_PORT to a "
            "free one and try again.",
            file=sys.stderr,
        )
        return EXIT_FAILED
    except Exception as exc:  # noqa: BLE001 - reported, not propagated
        print(f"\nFAILED to start: {type(exc).__name__}: {exc}", file=sys.stderr)
        return EXIT_FAILED

    return EXIT_OK


if __name__ == "__main__":
    raise SystemExit(main())
