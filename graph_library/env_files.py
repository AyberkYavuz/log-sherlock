"""Which environment file a service loads, and saying so out loud.

Every LogSherlock entry point — ``backend.py``, ``init_db.py``,
``tests/mock_local_llm.py`` — has to answer the same question before it does
anything else: *which* environment file am I about to read? Getting that wrong
is the most expensive kind of silent failure in this stack, because nothing
fails. The service starts, binds a port, connects to *a* database and reports
success against the wrong configuration entirely.

This module is the single answer to that question on the Python side, and the
rule it implements is mirrored — deliberately, in a comment that names this
file — by ``frontend/vite.config.ts`` for the browser bundle.

The rule, in priority order:

    1. ``ENV_FILE`` names a file explicitly. It is honoured verbatim, and a
       value that names nothing is reported as the error it is rather than
       quietly falling back. Falling back there is the dangerous case: an
       operator who asked for ``.env.docker`` and silently got ``.env`` would be
       running a container against a developer's laptop credentials.
    2. A container indicator is present *and* ``.env.docker`` exists. Inside a
       container the compose file usually supplies the environment directly, so
       this is a convenience rather than the main path.
    3. ``.env``, the local default.
    4. Nothing. The environment is used exactly as the shell, the orchestrator
       or the secrets manager supplied it — which is a perfectly good way to run
       and is reported as such rather than as a failure.

``ENV_FILE`` is deliberately *not* a key inside any of the env files. It selects
which file to read, so a value written inside one could only be read after the
decision it was supposed to inform had already been made.

Nothing here imports another module of this project, and nothing here raises.
The loader reports and returns; a missing file, an unreadable file or a missing
``python-dotenv`` all leave the environment as it was and say so. This module is
imported by an entry point before that entry point has configured logging, so
every message goes to stdout as well as to the logger — the same reasoning
``graph_library.write_to_db.db.announce`` documents, and for the same reason:
a configuration line that only a configured log pipeline can see is invisible
exactly when it is needed.
"""

from __future__ import annotations

import logging
import os
import re
from pathlib import Path
from typing import NamedTuple

logger = logging.getLogger(__name__)

#: The variable that names an environment file explicitly. Read from the real
#: environment, never from a file — see the module docstring.
ENV_FILE_VAR = "ENV_FILE"

#: The local default, and the containerized override.
LOCAL_ENV_FILE = ".env"
DOCKER_ENV_FILE = ".env.docker"

#: Variables whose mere presence means "this process is inside a container".
#: ``container`` is what podman and systemd-nspawn set;
#: ``KUBERNETES_SERVICE_HOST`` is injected into every pod;
#: ``DOCKER_CONTAINER`` is the one a compose file can set by hand when the
#: others do not apply.
CONTAINER_ENV_VARS: tuple[str, ...] = (
    "DOCKER_CONTAINER",
    "KUBERNETES_SERVICE_HOST",
    "container",
)

#: The file Docker creates inside every container it starts. Checked as well as
#: the variables above, because it needs no cooperation from the compose file.
CONTAINER_MARKER_PATH = Path("/.dockerenv")

#: Prefix on every line this module emits, so one ``grep`` isolates the
#: configuration decision from the rest of a service's startup output.
LOG_PREFIX = "[Config]"

#: How far up from the working directory to look for a named env file. Enough
#: to cover running a script from ``tests/`` or ``frontend/``, bounded so a
#: process started at ``/`` does not walk the whole filesystem.
MAX_PARENT_SEARCH_DEPTH = 4

#: One ``KEY=VALUE`` assignment. Used only to *count* the keys in a file, so the
#: report can say how much was loaded without depending on ``python-dotenv``
#: being installed to produce the number.
_ASSIGNMENT = re.compile(r"^\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=")


class EnvFileResolution(NamedTuple):
    """What a service decided to read, and what came of it.

    Attributes:
        name: The file's name as it was asked for (``".env"``,
            ``".env.docker"``, or whatever ``ENV_FILE`` held). ``""`` when no
            file was in play at all.
        path: The absolute path that was found, or ``None`` when nothing was.
        selected_by: Which rule chose it — ``"ENV_FILE"``, ``"container"``,
            ``"default"`` or ``"none"``. This is the field that makes a
            surprising choice explicable after the fact.
        exists: Whether the file was actually there.
        loaded: Whether its contents reached ``os.environ``. ``False`` with
            ``exists=True`` means ``python-dotenv`` was missing or the file
            could not be read.
        key_count: Assignments found in the file, or ``0``.
        container: Whether a container indicator was detected, regardless of
            which file was chosen.
        detail: The one-line human sentence, already composed. This is what
            gets printed and logged.
    """

    name: str
    path: Path | None
    selected_by: str
    exists: bool
    loaded: bool
    key_count: int
    container: bool
    detail: str


#: The result of the last :func:`load_env_file` call in this process, so a
#: component that starts later — the API's lifespan hook, say — can report what
#: was actually loaded instead of re-deriving a guess.
_LOADED: EnvFileResolution | None = None


def loaded_env_file() -> EnvFileResolution | None:
    """The last resolution :func:`load_env_file` produced, or ``None``.

    ``None`` means no entry point in this process has loaded a file yet, which
    is the normal state under pytest and inside a library import.
    """
    return _LOADED


def container_indicator() -> str | None:
    """Why this process looks containerized, or ``None`` if it does not.

    Returns:
        A short phrase naming the evidence — suitable for dropping into a log
        line — or ``None``. The evidence is named rather than reduced to a
        boolean because "we picked the Docker file" is a claim an operator will
        eventually want to check.
    """
    for variable in CONTAINER_ENV_VARS:
        if (os.getenv(variable) or "").strip():
            return f"{variable} is set"
    try:
        if CONTAINER_MARKER_PATH.exists():
            return f"{CONTAINER_MARKER_PATH} exists"
    except OSError:  # pragma: no cover - a sandbox may refuse the stat
        return None
    return None


def _repo_root() -> Path:
    """The repository root, inferred from this file's own location."""
    return Path(__file__).resolve().parent.parent


def search_paths(start: Path | None = None) -> list[Path]:
    """Directories to look in, nearest first.

    The working directory and a bounded walk up from it, then the repository
    root. The walk is what lets ``python3 tests/mock_local_llm.py`` and
    ``npm run dev`` from ``frontend/`` find the same file the backend does; the
    repository root is the backstop for a process started somewhere else
    entirely.
    """
    origin = (start or Path.cwd()).resolve()
    candidates = [origin, *list(origin.parents)[:MAX_PARENT_SEARCH_DEPTH], _repo_root()]

    seen: set[Path] = set()
    ordered: list[Path] = []
    for candidate in candidates:
        if candidate not in seen:
            seen.add(candidate)
            ordered.append(candidate)
    return ordered


def find_env_file(name: str, start: Path | None = None) -> Path | None:
    """Locate ``name``, or return ``None``.

    An absolute path is taken at its word. A relative one is looked for in each
    of :func:`search_paths`, nearest first.
    """
    candidate = Path(name).expanduser()
    if candidate.is_absolute():
        return candidate if candidate.is_file() else None

    for directory in search_paths(start):
        resolved = directory / candidate
        if resolved.is_file():
            return resolved
    return None


def _count_keys(path: Path) -> int:
    """Assignments in a file, or ``0`` if it cannot be read."""
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return 0
    return sum(1 for line in text.splitlines() if _ASSIGNMENT.match(line))


def _relative(path: Path) -> str:
    """Render a path relative to the repository root when it sits under it.

    Absolute paths in a startup banner are noise when they all share a prefix,
    and load-bearing when they do not — so the prefix is dropped only when it is
    the one the reader already knows.
    """
    try:
        return str(path.relative_to(_repo_root()))
    except ValueError:
        return str(path)


def resolve_env_file(start: Path | None = None) -> EnvFileResolution:
    """Decide which environment file to read. No side effects.

    Separated from :func:`load_env_file` so the decision can be inspected — by
    a test, or by a diagnostic that wants to report the choice without making
    it. See the module docstring for the rule and its rationale.

    Args:
        start: Where to begin searching. Defaults to the working directory.

    Returns:
        A resolution whose ``loaded`` is always ``False`` — this function
        chooses, it does not load.
    """
    indicator = container_indicator()
    containerized = indicator is not None

    # -- 1. an explicit request ---------------------------------------------
    explicit = (os.getenv(ENV_FILE_VAR) or "").strip()
    if explicit:
        path = find_env_file(explicit, start)
        if path is None:
            looked_in = ", ".join(_relative(p) or "." for p in search_paths(start))
            return EnvFileResolution(
                name=explicit,
                path=None,
                selected_by="ENV_FILE",
                exists=False,
                loaded=False,
                key_count=0,
                container=containerized,
                detail=(
                    f"{ENV_FILE_VAR}={explicit!r} was requested but no such file "
                    f"exists (looked in: {looked_in}). Environment left exactly "
                    "as supplied; nothing was loaded."
                ),
            )
        return EnvFileResolution(
            name=explicit,
            path=path,
            selected_by="ENV_FILE",
            exists=True,
            loaded=False,
            key_count=_count_keys(path),
            container=containerized,
            detail="",
        )

    # -- 2. a container, with a file meant for one --------------------------
    if containerized:
        docker_path = find_env_file(DOCKER_ENV_FILE, start)
        if docker_path is not None:
            return EnvFileResolution(
                name=DOCKER_ENV_FILE,
                path=docker_path,
                selected_by="container",
                exists=True,
                loaded=False,
                key_count=_count_keys(docker_path),
                container=True,
                detail="",
            )

    # -- 3. the local default -----------------------------------------------
    local_path = find_env_file(LOCAL_ENV_FILE, start)
    if local_path is not None:
        return EnvFileResolution(
            name=LOCAL_ENV_FILE,
            path=local_path,
            selected_by="default",
            exists=True,
            loaded=False,
            key_count=_count_keys(local_path),
            container=containerized,
            detail="",
        )

    # -- 4. nothing, which is a valid way to run ----------------------------
    looked_in = ", ".join(_relative(p) or "." for p in search_paths(start))
    wanted = (
        f"{DOCKER_ENV_FILE} or {LOCAL_ENV_FILE}" if containerized else LOCAL_ENV_FILE
    )
    return EnvFileResolution(
        name="",
        path=None,
        selected_by="none",
        exists=False,
        loaded=False,
        key_count=0,
        container=containerized,
        detail=(
            f"No environment file found (looked for {wanted} in: {looked_in}). "
            "Reading the environment exactly as supplied, which is normal when "
            "an orchestrator or a secrets manager provides it."
        ),
    )


def _selection_reason(resolution: EnvFileResolution) -> str:
    """Why this file, phrased for the end of a sentence."""
    if resolution.selected_by == "ENV_FILE":
        return f"selected by {ENV_FILE_VAR}"
    if resolution.selected_by == "container":
        indicator = container_indicator() or "a container indicator"
        return f"selected because {indicator}"
    if resolution.container:
        # Containerized, but the Docker file was not there to be chosen. Worth
        # saying: it is the combination most likely to be a mistake.
        return (
            f"selected by default — {DOCKER_ENV_FILE} was not found even though "
            f"{container_indicator() or 'a container indicator'}"
        )
    return f"selected by default — no {ENV_FILE_VAR} override, not containerized"


def _report(message: str, *, warning: bool = False) -> None:
    """Send one line to the logger and to stdout.

    Both, deliberately. An entry point calls this *before* it configures
    logging, so a logger-only line would be dropped by the root logger's
    default handling; and stdout is what a terminal, a container log and
    LangGraph Server all show directly. ``flush`` because stdout is
    block-buffered whenever it is not a terminal, which is every case where the
    line matters most.
    """
    line = f"{LOG_PREFIX} {message}"

    # The logger is used only once something is actually listening. Without a
    # configured handler, ``logging.lastResort`` writes WARNING and above
    # straight to stderr, which would print every warning twice — once there and
    # once below — in exactly the case that matters most: an entry point
    # reporting a bad ``ENV_FILE`` before it has configured logging. Checking
    # for handlers keeps the stdout line the single source of that message
    # locally, while a deployment that configures logging first still captures
    # it in its own pipeline.
    if logger.hasHandlers():
        logger.warning("%s", line) if warning else logger.info("%s", line)

    print(line, flush=True)


def load_env_file(
    start: Path | None = None,
    *,
    announce: bool = True,
    override: bool = False,
) -> EnvFileResolution:
    """Resolve an environment file, load it, and say which one it was.

    Never raises. Every failure — no file, no ``python-dotenv``, an unreadable
    file — is reported and returned, because a service that can start from the
    environment it already has should not be stopped by the absence of a
    convenience.

    Args:
        start: Where to begin searching. Defaults to the working directory.
        announce: Emit the report to stdout and the logger. Set ``False`` for a
            caller that wants the resolution without the output.
        override: Let file values replace variables already in the environment.
            Defaults to ``False``, which is what keeps a real credential
            exported by a shell or injected by an orchestrator from being
            clobbered by a checked-in placeholder.

    Returns:
        The resolution, with ``loaded`` telling the caller whether anything
        actually reached ``os.environ``.
    """
    global _LOADED

    resolution = resolve_env_file(start)

    if resolution.path is None:
        # Nothing to load: either an explicit request that named nothing, or no
        # file at all. Both already carry their own sentence.
        if announce:
            _report(resolution.detail, warning=resolution.selected_by == "ENV_FILE")
        _LOADED = resolution
        return resolution

    try:
        from dotenv import load_dotenv
    except ImportError:
        resolution = resolution._replace(
            detail=(
                f"python-dotenv is not installed, so {_relative(resolution.path)} "
                f"was NOT loaded ({resolution.key_count} keys skipped). Install it "
                "with: pip install python-dotenv"
            )
        )
        if announce:
            _report(resolution.detail, warning=True)
        _LOADED = resolution
        return resolution

    try:
        load_dotenv(resolution.path, override=override)
    except Exception as exc:  # noqa: BLE001 - the environment may already be complete
        resolution = resolution._replace(
            detail=(
                f"could not read {_relative(resolution.path)} "
                f"({type(exc).__name__}: {exc}). Environment left as supplied."
            )
        )
        if announce:
            _report(resolution.detail, warning=True)
        _LOADED = resolution
        return resolution

    resolution = resolution._replace(
        loaded=True,
        detail=(
            f"Sourced environment variables from {_relative(resolution.path)} "
            f"({resolution.key_count} keys, {_selection_reason(resolution)})"
        ),
    )
    if announce:
        _report(resolution.detail)
    _LOADED = resolution
    return resolution


__all__ = [
    "CONTAINER_ENV_VARS",
    "CONTAINER_MARKER_PATH",
    "DOCKER_ENV_FILE",
    "ENV_FILE_VAR",
    "LOCAL_ENV_FILE",
    "LOG_PREFIX",
    "EnvFileResolution",
    "container_indicator",
    "find_env_file",
    "load_env_file",
    "loaded_env_file",
    "resolve_env_file",
    "search_paths",
]
