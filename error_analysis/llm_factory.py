"""Chat-model construction for the Error Analysis Node.

One function, :func:`get_error_analysis_llm`, maps a ``(provider, mode)`` pair
to a configured LangChain chat model. Isolating that here keeps two concerns out
of :mod:`error_analysis.node`: which model a tier actually resolves to, and how
each vendor's client wants to be constructed.

Three rules hold for every provider:

    * **``temperature=0.0`` wherever the model accepts it.** This node
      classifies and attributes errors; the same log should yield the same
      verdict twice in a row. Sampling would make the output unreproducible for
      no benefit. Two model families are documented exceptions, both because
      they *reject* the parameter rather than ignore it: OpenAI's reasoning
      models (see :func:`supports_temperature`) and Anthropic's current
      generation (see :func:`anthropic_supports_temperature`).
    * **Credentials come from the environment**, never from graph state, so an
      API key cannot leak into a report or a checkpoint.
    * **Provider SDKs are imported lazily**, inside the branch that needs them.
      Only the provider actually in use has to be installed — analyzing logs
      with OpenAI does not require the Anthropic or Google packages to be
      present.

Both OpenAI-compatible providers (DeepSeek and ``local``) are built on
``ChatOpenAI`` with a redirected ``base_url``; that is the wire protocol they
speak, not an assumption that they are OpenAI. Where that assumption would
break — DeepSeek serves tool calling but not ``response_format:
{"type": "json_schema"}`` — :func:`structured_output_kwargs` names the
difference, since the client is shared but the endpoint's capabilities are not.

Every construction is logged at ``INFO`` with the resolved model id and the
full parameter payload (API keys reduced to ``<set>``/``<unset>``). A wrong
model id fails at the provider with a bare ``404``, which says nothing about
which tier asked for it; the log line does.

Model ids rot. Vendors retire a generation and every pinned id in
:data:`MODEL_TIERS` starts answering ``404``, which this node can only report
as "LLM reasoning unavailable". Three layers guard against that, in increasing
order of desperation:

    * :data:`MODEL_FALLBACKS` — hand-verified alternates per tier;
    * :func:`discover_models` — a cached, best-effort listing of what the
      configured key can actually reach;
    * :func:`iter_error_analysis_llms` — walks those candidates so a caller can
      retry the *invocation*, which is the only place a bad model id shows up.
      No provider validates the id at construction time.

A listing is not an oracle: Gemini returns ``gemini-2.5-flash`` from
``models.list()`` and then refuses it at ``generateContent``. Discovery is
therefore ordered last, behind ids that have been verified by hand.
"""

from __future__ import annotations

import logging
import os
import re
from collections.abc import Iterator
from typing import TYPE_CHECKING, Any

from models import AnalysisMode, LLMProvider

if TYPE_CHECKING:  # pragma: no cover - typing only
    from langchain_core.language_models.chat_models import BaseChatModel

logger = logging.getLogger(__name__)

#: Model tiers per provider, keyed by analysis mode. Kept as data rather than
#: branches so the full routing table is readable at a glance and a new tier is
#: a one-line edit.
#:
#: Where a provider's ``standard`` and ``deep`` tiers name the same model, that
#: is deliberate: the vendor has no distinct higher-reasoning model that suits
#: this task, and silently downgrading ``deep`` is better than pointing it at a
#: model that does not exist.
MODEL_TIERS: dict[str, dict[str, str]] = {
    "openai": {
        "fast": "gpt-4o-mini",
        "standard": "gpt-4o",
        "deep": "o3-mini",
    },
    # Anthropic publishes no ``-latest`` aliases on the API: every id here is a
    # concrete model string, verified against ``GET /v1/models``. The Claude 3.5
    # generation this table used to name — ``claude-3-5-haiku-*`` and
    # ``claude-3-5-sonnet-*``, alias or dated — has been retired and answers
    # every request with ``404 not_found_error``.
    "anthropic": {
        "fast": "claude-haiku-4-5",
        "standard": "claude-sonnet-5",
        "deep": "claude-opus-5",
    },
    # ``gemini-pro-latest`` is an alias rather than a dated id because Google
    # currently ships no stable concrete Pro: every other Pro in
    # ``models.list()`` is a ``-preview`` or an image variant, and pinning a
    # preview invites exactly the retirement this table already suffered.
    # The 1.5 generation is gone entirely and 2.5 answers "no longer available
    # to new users" — see :func:`discover_models` for why neither is detectable
    # from the listing alone.
    "gemini": {
        "fast": "gemini-3.6-flash",
        "standard": "gemini-pro-latest",
        "deep": "gemini-pro-latest",
    },
    # Verified against the live ``/models`` listing and against
    # ``/chat/completions``: both ids answer, and both think before answering,
    # which is what :data:`STRUCTURED_OUTPUT_OVERRIDES` has to accommodate.
    # They do not match the ids DeepSeek documents, so
    # :data:`MODEL_FALLBACKS` still carries ``deepseek-chat`` and
    # ``deepseek-reasoner`` as alternates — neither is in the listing, but both
    # answer when called by name.
    "deepseek": {
        "fast": "deepseek-v4-flash",
        "standard": "deepseek-v4-flash",
        "deep": "deepseek-v4-pro",
    },
}

#: Alternates tried, in order, when a tier's model turns out to be unavailable.
#: Every Gemini entry was verified against ``generateContent`` with a live key;
#: the DeepSeek entries are the ids DeepSeek documents. These sit *ahead* of
#: anything :func:`discover_models` returns because they are known-good, where
#: a listing is only known-to-exist.
MODEL_FALLBACKS: dict[str, dict[str, tuple[str, ...]]] = {
    "gemini": {
        "fast": ("gemini-flash-latest", "gemini-3.7-flash", "gemini-pro-latest"),
        "standard": ("gemini-flash-latest", "gemini-3.6-flash"),
        "deep": ("gemini-flash-latest", "gemini-3.6-flash"),
    },
    "deepseek": {
        "fast": ("deepseek-chat",),
        "standard": ("deepseek-chat",),
        "deep": ("deepseek-reasoner", "deepseek-chat"),
    },
}

#: Applied when ``provider`` or ``mode`` is omitted from the graph state.
DEFAULT_PROVIDER: LLMProvider = "openai"
DEFAULT_MODE: AnalysisMode = "standard"

#: Spellings that resolve to a canonical provider. ``llm_provider`` arrives as
#: free text — typed into LangGraph Studio, read from a config file — and
#: nothing between the form field and this module validates it, so a provider
#: the user plainly meant should not fail on capitalisation or a doubled
#: letter. Keys are matched after :func:`normalize_provider` lowercases and
#: folds ``-``/space to ``_``, which is why no cased or hyphenated variants
#: appear here.
#:
#: The vendor-name entries (``google``, ``claude``) are the names users
#: actually reach for; the misspellings are the ones observed in practice. This
#: is a convenience layer, not a spell-checker — an unrecognised string still
#: raises with the list of supported providers.
PROVIDER_ALIASES: dict[str, str] = {
    "google": "gemini",
    "google_ai": "gemini",
    "google_genai": "gemini",
    "google_gemini": "gemini",
    "genai": "gemini",
    "geminni": "gemini",
    "gemeni": "gemini",
    "gemnini": "gemini",
    "claude": "anthropic",
    "antropic": "anthropic",
    "gpt": "openai",
    "chatgpt": "openai",
    "open_ai": "openai",
    "deep_seek": "deepseek",
}


def normalize_provider(provider: str | None) -> str:
    """Fold a free-text provider name onto its canonical spelling.

    Args:
        provider: Whatever arrived in graph state — possibly padded,
            capitalised, hyphenated or misspelled. ``None`` and the empty
            string mean "not supplied".

    Returns:
        The canonical provider id, or :data:`DEFAULT_PROVIDER` when nothing was
        supplied. An unrecognised name is returned normalized but unmapped, so
        the error it eventually raises names what the caller actually typed.
    """
    if provider is None:
        return DEFAULT_PROVIDER

    folded = str(provider).strip().lower().replace("-", "_").replace(" ", "_")
    if not folded:
        return DEFAULT_PROVIDER

    canonical = PROVIDER_ALIASES.get(folded, folded)
    if canonical != str(provider):
        logger.info(
            "Normalized llm_provider %r to %r", provider, canonical
        )
    return canonical


def normalize_mode(mode: str | None) -> str:
    """Fold a free-text analysis mode onto its canonical spelling.

    Case and padding only — unlike :func:`normalize_provider` there is no alias
    table, because ``fast``/``standard``/``deep`` have no competing vendor
    names to be confused with.
    """
    if mode is None:
        return DEFAULT_MODE

    folded = str(mode).strip().lower()
    if not folded:
        return DEFAULT_MODE

    if folded != str(mode):
        logger.info("Normalized analysis_mode %r to %r", mode, folded)
    return folded

#: DeepSeek's OpenAI-compatible endpoint.
DEEPSEEK_BASE_URL = "https://api.deepseek.com"

#: Fallbacks for the ``local`` provider, each overridable by the matching
#: environment variable. The defaults target a vLLM/LM Studio style server on
#: localhost — and the mock in ``tests/mock_local_llm.py``.
DEFAULT_LOCAL_MODEL = "llama-3.3-70b-instruct"
DEFAULT_LOCAL_BASE_URL = "http://127.0.0.1:8000/v1"
#: Local servers usually ignore the key but the OpenAI client refuses to send
#: an empty one, hence a non-empty placeholder rather than "".
DEFAULT_LOCAL_API_KEY = "cant-be-empty"

TEMPERATURE = 0.0

#: Anthropic's client has no server-side default for ``max_tokens`` and
#: langchain-anthropic falls back to 1024, which is not enough here: the node
#: asks for one evaluation per signature and up to 25 signatures are batched
#: into a single call. On the models that think before answering, reasoning is
#: billed against this same ceiling, so a low value truncates the structured
#: response mid-object rather than degrading gracefully.
ANTHROPIC_MAX_TOKENS = 8192

#: OpenAI's reasoning families — the ``o`` series and GPT-5. They fix sampling
#: server-side and *reject* ``temperature`` outright rather than ignoring it:
#: ``400 unsupported_parameter``. Matched on the leading family token so dated
#: and sized variants (``o3-mini``, ``o4-mini-2025-04-16``) are covered without
#: enumerating every release.
_REASONING_MODEL_PATTERN = re.compile(r"^(o\d+|gpt-5)(-|$)")

#: The Anthropic models that still accept ``temperature``. Deliberately an
#: allowlist rather than a pattern: Anthropic removed the sampling parameters
#: with the Claude 4.6 generation and every model released since rejects them
#: (``400 'temperature' is deprecated for this model``), so *not* sending the
#: parameter is the safe default for anything not named here.
ANTHROPIC_TEMPERATURE_MODELS: frozenset[str] = frozenset({"claude-haiku-4-5"})

#: Keyword arguments a provider needs passed to ``with_structured_output()``.
#: Only the exceptions are listed; every provider absent from this table keeps
#: its LangChain default, which is what its own package's maintainers test
#: against.
#:
#: DeepSeek is the one entry, and it needs both keys, because being
#: OpenAI-*compatible* is not the same as being OpenAI. Each key answers a
#: distinct ``400`` from ``api.deepseek.com``, verified against the live
#: endpoint:
#:
#:     * ``method`` — ``ChatOpenAI.with_structured_output`` defaults to
#:       ``"json_schema"``, which puts ``response_format: {"type":
#:       "json_schema", ...}`` on the wire. Every DeepSeek model rejects it with
#:       ``This response_format type is unavailable now``. Tool calling is what
#:       DeepSeek does serve, so ``"function_calling"`` is the method that
#:       works.
#:     * ``tool_choice`` — with ``function_calling``, langchain-openai then
#:       *forces* the single bound tool (``tool_choice: "<tool name>"``). The
#:       ``deepseek-v4`` tier models think before answering and reject a forced
#:       choice with ``Thinking mode does not support this tool_choice``;
#:       ``"auto"`` is the only setting they accept. ``deepseek-chat`` — the
#:       :data:`MODEL_FALLBACKS` alternate — accepts either, so one value
#:       covers the whole candidate chain.
#:
#: Both failures are total rather than partial: the node catches them, publishes
#: deterministic signatures only, and the entire root-cause pass is lost. That
#: is why these are pinned here rather than discovered per run.
#:
#: ``"auto"`` costs the guarantee that a tool call comes back at all, which is
#: why :func:`~error_analysis.node._invoke_with_model_fallback` treats an empty
#: response as a failure with its own note instead of letting it surface as a
#: validation error.
STRUCTURED_OUTPUT_OVERRIDES: dict[str, dict[str, Any]] = {
    "deepseek": {"method": "function_calling", "tool_choice": "auto"},
}

#: Constructor arguments whose value is a credential. Logged as a presence flag
#: so a run is debuggable — "the key was empty" is a common cause of a provider
#: rejection — without the key itself reaching a log file.
_SECRET_KWARGS: frozenset[str] = frozenset({"api_key", "google_api_key"})

#: Seconds to wait on a model-listing call. Discovery runs at most once per
#: provider per process and only widens an already-curated candidate list, so a
#: slow or unreachable listing endpoint must not hold up the analysis.
MODEL_DISCOVERY_TIMEOUT = 10.0

#: Substrings that mark a listed model as wrong for this node. The listings are
#: whole-catalogue: Gemini's includes text-to-speech, image, robotics and music
#: models that advertise ``generateContent`` but cannot answer a structured
#: root-cause question.
_DISCOVERY_EXCLUDE: tuple[str, ...] = (
    "tts",
    "image",
    "vision",
    "embedding",
    "aqa",
    "robotics",
    "lyria",
    "veo",
    "imagen",
    "banana",
    "computer-use",
    "deep-research",
    "gemma",
)

#: Which discovered models suit which tier, most-preferred keyword first. A
#: discovered model is only considered if its id contains one of these.
_DISCOVERY_KEYWORDS: dict[str, dict[str, tuple[str, ...]]] = {
    "gemini": {
        "fast": ("flash",),
        "standard": ("pro", "flash"),
        "deep": ("pro", "flash"),
    },
    "deepseek": {
        "fast": ("chat",),
        "standard": ("chat",),
        "deep": ("reasoner", "chat"),
    },
}

#: Per-process memo for :func:`discover_models`, keyed by canonical provider. A
#: failed lookup caches the empty tuple deliberately: a missing key or an
#: offline endpoint will not fix itself mid-run, and retrying the listing on
#: every invocation would add the latency this cache exists to avoid.
_DISCOVERY_CACHE: dict[str, tuple[str, ...]] = {}


def clear_model_discovery_cache() -> None:
    """Drop every memoized listing. For tests and long-lived processes."""
    _DISCOVERY_CACHE.clear()
    logger.debug("Model discovery cache cleared")


def supports_temperature(model: str) -> bool:
    """Whether ``temperature`` may be sent to this OpenAI model.

    The distinction is not cosmetic. Sending ``temperature`` to a reasoning
    model fails the whole request, which is how ``deep`` mode — the only tier
    that routes to one — lost its LLM pass entirely while ``fast`` and
    ``standard`` kept working.

    Args:
        model: An OpenAI model identifier.

    Returns:
        ``False`` for the reasoning families, ``True`` for everything else.
        Only consulted for the ``openai`` provider: the other
        OpenAI-compatible endpoints (DeepSeek, ``local``) serve their own
        models and accept the parameter.
    """
    return _REASONING_MODEL_PATTERN.match(model) is None


def anthropic_supports_temperature(model: str) -> bool:
    """Whether ``temperature`` may be sent to this Anthropic model.

    The Anthropic counterpart to :func:`supports_temperature`, and the same
    class of failure: Claude 4.6 and later removed the sampling parameters, and
    sending one fails the whole request with ``400 'temperature' is deprecated
    for this model`` — the node would fall back to deterministic signatures
    with no root-cause evaluation, exactly as it did under the ``404``.

    Args:
        model: An Anthropic model identifier.

    Returns:
        ``True`` only for the models in
        :data:`ANTHROPIC_TEMPERATURE_MODELS`; ``False`` otherwise. The
        conservative direction is deliberate — a new Anthropic model is far
        likelier to reject the parameter than to accept it.
    """
    return model in ANTHROPIC_TEMPERATURE_MODELS


def structured_output_kwargs(provider: str) -> dict[str, Any]:
    """The ``with_structured_output()`` keyword arguments for a provider.

    The third provider quirk in this module, and the same shape as the other
    two: a provider rejects the default outright, so the default has to be
    replaced rather than sent and refused. See
    :data:`STRUCTURED_OUTPUT_OVERRIDES` for why DeepSeek is currently the only
    entry and what each of its keys fixes.

    Returned as kwargs rather than as a method string so the common case stays
    *untouched*: a provider with no override gets an empty dict and therefore
    whatever its own LangChain package defaults to, which is not the same thing
    as passing ``method="json_schema"`` explicitly — the Gemini and Anthropic
    integrations pick their own default, and naming one here would freeze a
    choice this module has no reason to make.

    Args:
        provider: A canonical provider id, or any spelling
            :func:`normalize_provider` accepts.

    Returns:
        A fresh dict — safe for the caller to mutate — of arguments to splat
        into ``with_structured_output()``, empty for a provider that needs no
        override.
    """
    provider = normalize_provider(provider)

    overrides = STRUCTURED_OUTPUT_OVERRIDES.get(provider)
    if not overrides:
        return {}

    logger.debug(
        "Provider %s requires structured-output overrides: %s", provider, overrides
    )
    return dict(overrides)


def _discover_gemini_models() -> tuple[str, ...]:
    """List the Gemini models this ``GEMINI_API_KEY`` can call."""
    from google import genai  # imported lazily, like every other provider SDK

    client = genai.Client(api_key=os.getenv("GEMINI_API_KEY"))
    return tuple(
        # The listing prefixes every id with ``models/``; the chat clients want
        # it without.
        model.name.removeprefix("models/")
        for model in client.models.list()
        if "generateContent" in (getattr(model, "supported_actions", None) or ())
    )


def _discover_deepseek_models() -> tuple[str, ...]:
    """List the DeepSeek models this ``DEEPSEEK_API_KEY`` can call."""
    from openai import OpenAI  # DeepSeek speaks the OpenAI wire protocol

    client = OpenAI(
        api_key=os.getenv("DEEPSEEK_API_KEY"),
        base_url=DEEPSEEK_BASE_URL,
        timeout=MODEL_DISCOVERY_TIMEOUT,
    )
    return tuple(model.id for model in client.models.list().data)


_DISCOVERY_BACKENDS = {
    "gemini": _discover_gemini_models,
    "deepseek": _discover_deepseek_models,
}


def discover_models(provider: str, *, refresh: bool = False) -> tuple[str, ...]:
    """Ask a provider which models the configured key can reach.

    Memoized per process — see :data:`_DISCOVERY_CACHE`. Never raises: a
    missing SDK, an absent key or an unreachable endpoint yields an empty
    tuple, because discovery only ever *widens* the candidate list built by
    :func:`resolve_model_candidates` and must not be able to break a run that
    the curated entries would have handled.

    A listing says a model *exists*, not that this key may *call* it. Gemini is
    the proof: ``gemini-2.5-flash`` is returned by ``models.list()`` and still
    answers ``generateContent`` with ``404 ... no longer available to new
    users``. Discovery is therefore a last-resort widener, ordered behind
    :data:`MODEL_FALLBACKS`, and never a source of truth on its own.

    Args:
        provider: A canonical provider id. Providers with no listing backend
            (``openai``, ``anthropic``, ``local``) return an empty tuple.
        refresh: Bypass and repopulate the cache.

    Returns:
        Model ids in the order the provider listed them, unfiltered.
    """
    provider = normalize_provider(provider)

    if not refresh and provider in _DISCOVERY_CACHE:
        return _DISCOVERY_CACHE[provider]

    backend = _DISCOVERY_BACKENDS.get(provider)
    if backend is None:
        _DISCOVERY_CACHE[provider] = ()
        return ()

    try:
        discovered = backend()
    except Exception:  # noqa: BLE001 - discovery is strictly best-effort
        logger.warning(
            "Model discovery failed for provider=%s; continuing with the "
            "curated candidate list",
            provider,
            exc_info=True,
        )
        discovered = ()
    else:
        logger.info(
            "Discovered %d callable model(s) for provider=%s", len(discovered), provider
        )
        logger.debug("provider=%s listing: %s", provider, ", ".join(discovered))

    _DISCOVERY_CACHE[provider] = discovered
    return discovered


def _discovered_candidates(provider: str, mode: str) -> list[str]:
    """The discovered models that plausibly suit ``mode``, best first."""
    keywords = _DISCOVERY_KEYWORDS.get(provider, {}).get(mode)
    if not keywords:
        return []

    usable = [
        model
        for model in discover_models(provider)
        if not any(marker in model.lower() for marker in _DISCOVERY_EXCLUDE)
    ]

    ranked: list[str] = []
    for keyword in keywords:
        # Sorted descending so a newer generation outranks an older one:
        # ``gemini-3.7-flash`` before ``gemini-3.6-flash`` before ``2.5``.
        ranked.extend(
            sorted(
                (model for model in usable if keyword in model.lower() and model not in ranked),
                reverse=True,
            )
        )
    return ranked


def resolve_model_candidates(provider: str, mode: str) -> list[str]:
    """Every model worth trying for a tier, in the order to try them.

    Three sources, deliberately ordered by how much they are trusted:

        1. the tier's model from :data:`MODEL_TIERS` — what the operator asked
           for, and the only entry used when everything is healthy;
        2. :data:`MODEL_FALLBACKS` — hand-verified alternates;
        3. whatever :func:`discover_models` turns up — models known to exist
           but not known to be callable.

    Args:
        provider: A canonical provider id.
        mode: One of :data:`~models.AnalysisMode`.

    Returns:
        A de-duplicated list, never empty — its first entry is always
        :func:`resolve_model_name`.

    Raises:
        ValueError: If ``provider`` or ``mode`` is not recognized.
    """
    provider = normalize_provider(provider)
    mode = normalize_mode(mode)

    candidates = [resolve_model_name(provider, mode)]
    for model in (
        *MODEL_FALLBACKS.get(provider, {}).get(mode, ()),
        *_discovered_candidates(provider, mode),
    ):
        if model not in candidates:
            candidates.append(model)

    logger.debug(
        "Candidate models for provider=%s mode=%s: %s",
        provider,
        mode,
        ", ".join(candidates),
    )
    return candidates


#: Text that marks a provider error as "this model is not available to you",
#: as opposed to a transport, quota or credential failure. Matched
#: case-insensitively against every exception in the ``__cause__`` chain,
#: because each SDK wraps the underlying 404 in its own class: Google raises
#: ``ChatGoogleGenerativeAIError`` with no status attribute at all, while the
#: OpenAI and Anthropic clients raise a typed ``NotFoundError``.
_MODEL_UNAVAILABLE_MARKERS: tuple[str, ...] = (
    "not_found",
    "not found",
    "no longer available",
    "does not exist",
    "model_not_found",
    "invalid model",
    "unknown model",
    "is not supported for generatecontent",
)


def is_model_unavailable(exc: BaseException) -> bool:
    """Whether ``exc`` means "wrong model", and so is worth a retry elsewhere.

    Distinguishing this from an expired key or a rate limit matters: retrying
    those against a different model burns quota and still fails. Only a
    model-identity failure is recoverable by swapping the model.

    Args:
        exc: The exception raised by a chat-model invocation.

    Returns:
        ``True`` if ``exc`` — or anything it wraps — reports a 404 or names the
        model as unavailable.
    """
    seen: set[int] = set()
    current: BaseException | None = exc

    while current is not None and id(current) not in seen:
        seen.add(id(current))

        for attribute in ("status_code", "code", "status"):
            if getattr(current, attribute, None) in (404, "404", "NOT_FOUND"):
                return True

        text = str(current).lower()
        if any(marker in text for marker in _MODEL_UNAVAILABLE_MARKERS):
            return True

        current = current.__cause__ or current.__context__

    return False


def _log_client_config(
    provider: str, mode: str, model_name: str, kwargs: dict[str, Any]
) -> None:
    """Record the exact payload a provider client is about to be built with."""
    redacted = {
        key: ("<set>" if value else "<unset>") if key in _SECRET_KWARGS else value
        for key, value in kwargs.items()
    }
    logger.info(
        "Building error-analysis LLM: provider=%s mode=%s model=%s params=%s",
        provider,
        mode,
        model_name,
        redacted,
    )


def resolve_model_name(provider: str, mode: str) -> str:
    """Return the model id a ``(provider, mode)`` pair selects.

    Exposed separately from :func:`get_error_analysis_llm` so the routing table
    can be asserted without constructing a client or holding credentials.

    Args:
        provider: One of :data:`~models.LLMProvider`.
        mode: One of :data:`~models.AnalysisMode`.

    Returns:
        The provider-specific model identifier. For ``"local"`` this is
        ``LOCAL_LLM_MODEL_NAME`` from the environment, since the served model
        is the operator's choice and cannot be known here.

    Raises:
        ValueError: If ``provider`` or ``mode`` is not recognized.
    """
    provider = normalize_provider(provider)
    mode = normalize_mode(mode)

    if provider == "local":
        model = os.getenv("LOCAL_LLM_MODEL_NAME") or DEFAULT_LOCAL_MODEL
        logger.debug(
            "Resolved local model %s (from %s)",
            model,
            "LOCAL_LLM_MODEL_NAME" if os.getenv("LOCAL_LLM_MODEL_NAME") else "default",
        )
        return model

    tiers = MODEL_TIERS.get(provider)
    if tiers is None:
        supported = ", ".join([*sorted(MODEL_TIERS), "local"])
        logger.error(
            "Unknown LLM provider %r; supported providers: %s", provider, supported
        )
        raise ValueError(
            f"Unsupported LLM provider {provider!r}. Supported providers: {supported}."
        )

    model = tiers.get(mode)
    if model is None:
        logger.error(
            "Unknown analysis mode %r for provider %r; supported modes: %s",
            mode,
            provider,
            ", ".join(sorted(tiers)),
        )
        raise ValueError(
            f"Unsupported analysis mode {mode!r} for provider {provider!r}. "
            f"Supported modes: {', '.join(sorted(tiers))}."
        )

    logger.debug("Resolved provider=%s mode=%s to model=%s", provider, mode, model)
    return model


def get_error_analysis_llm(
    provider: str = DEFAULT_PROVIDER,
    mode: str = DEFAULT_MODE,
    *,
    model: str | None = None,
    **overrides: Any,
) -> BaseChatModel:
    """Build the chat model the Error Analysis Node should reason with.

    Args:
        provider: Which vendor to call — ``"openai"``, ``"anthropic"``,
            ``"gemini"``, ``"deepseek"`` or ``"local"``. Normalized and
            de-aliased by :func:`normalize_provider`.
        mode: Which tier to use — ``"fast"``, ``"standard"`` or ``"deep"``.
        model: Use this model id instead of the one ``mode`` selects. How
            :func:`iter_error_analysis_llms` walks a fallback chain without
            having to reach into each provider branch.
        **overrides: Extra keyword arguments forwarded verbatim to the
            underlying client (``timeout``, ``max_retries``, ...). Anything
            passed here wins over this function's defaults.

    Returns:
        A configured chat model, ready for ``.with_structured_output()``. It
        runs at ``temperature=0.0`` unless the model rejects the parameter, in
        which case it is omitted rather than sent and refused.

        Construction contacts nothing — no provider validates the model id
        here, so a dead id builds cleanly and only fails on invocation. That is
        why the fallback chain lives in :func:`iter_error_analysis_llms` and
        not in a ``try`` around this call.

    Raises:
        ValueError: If ``provider`` or ``mode`` is not recognized.
        ImportError: If the selected provider's LangChain package is not
            installed. The message names the package to install.
    """
    provider = normalize_provider(provider)
    mode = normalize_mode(mode)
    model_name = model or resolve_model_name(provider, mode)

    if provider == "openai":
        chat_openai = _import_chat_openai()
        # Every branch assembles a dict rather than passing keywords directly:
        # ``temperature`` is conditional on two of them, an explicit override
        # then replaces a default instead of colliding with it, and the
        # assembled payload is what gets logged.
        kwargs: dict[str, Any] = {
            "model": model_name,
            "api_key": os.getenv("OPENAI_API_KEY"),
        }
        if supports_temperature(model_name):
            kwargs["temperature"] = TEMPERATURE
        kwargs.update(overrides)
        _log_client_config(provider, mode, model_name, kwargs)
        return chat_openai(**kwargs)

    if provider == "anthropic":
        try:
            from langchain_anthropic import ChatAnthropic
        except ImportError as exc:  # pragma: no cover - depends on environment
            logger.error("langchain-anthropic is not installed", exc_info=True)
            raise ImportError(
                "The 'anthropic' provider requires the langchain-anthropic "
                "package. Install it with: pip install langchain-anthropic"
            ) from exc
        kwargs = {
            "model": model_name,
            "api_key": os.getenv("ANTHROPIC_API_KEY"),
            "max_tokens": ANTHROPIC_MAX_TOKENS,
        }
        if anthropic_supports_temperature(model_name):
            kwargs["temperature"] = TEMPERATURE
        kwargs.update(overrides)
        _log_client_config(provider, mode, model_name, kwargs)
        return ChatAnthropic(**kwargs)

    if provider == "gemini":
        try:
            from langchain_google_genai import ChatGoogleGenerativeAI
        except ImportError as exc:  # pragma: no cover - depends on environment
            logger.error("langchain-google-genai is not installed", exc_info=True)
            raise ImportError(
                "The 'gemini' provider requires the langchain-google-genai "
                "package. Install it with: pip install langchain-google-genai"
            ) from exc
        # Reads GEMINI_API_KEY specifically; the package would otherwise fall
        # back to GOOGLE_API_KEY, which is not the variable this project
        # documents in .env.example.
        kwargs = {
            "model": model_name,
            "temperature": TEMPERATURE,
            "google_api_key": os.getenv("GEMINI_API_KEY"),
        }
        kwargs.update(overrides)
        _log_client_config(provider, mode, model_name, kwargs)
        return ChatGoogleGenerativeAI(**kwargs)

    if provider == "deepseek":
        chat_openai = _import_chat_openai()
        kwargs = {
            "model": model_name,
            "temperature": TEMPERATURE,
            "api_key": os.getenv("DEEPSEEK_API_KEY"),
            "base_url": DEEPSEEK_BASE_URL,
        }
        kwargs.update(overrides)
        _log_client_config(provider, mode, model_name, kwargs)
        return chat_openai(**kwargs)

    if provider == "local":
        chat_openai = _import_chat_openai()
        kwargs = {
            "model": model_name,
            "temperature": TEMPERATURE,
            "api_key": os.getenv("LOCAL_LLM_API_KEY") or DEFAULT_LOCAL_API_KEY,
            "base_url": os.getenv("LOCAL_LLM_BASE_URL") or DEFAULT_LOCAL_BASE_URL,
        }
        kwargs.update(overrides)
        _log_client_config(provider, mode, model_name, kwargs)
        return chat_openai(**kwargs)

    # resolve_model_name has already rejected unknown providers, so reaching
    # here means a provider was added to MODEL_TIERS without a branch above.
    logger.error("No client branch implemented for provider %r", provider)
    raise ValueError(f"Unsupported LLM provider {provider!r}.")


def iter_error_analysis_llms(
    provider: str = DEFAULT_PROVIDER,
    mode: str = DEFAULT_MODE,
    **overrides: Any,
) -> Iterator[tuple[str, BaseChatModel]]:
    """Yield ``(model_id, client)`` for each candidate, best first.

    The caller invokes the first client and, if it fails with something
    :func:`is_model_unavailable` recognises, asks for the next. Splitting it
    this way keeps *which models exist* here and *what to ask them* in
    :mod:`error_analysis.node`.

    Clients are built lazily — one per iteration — so a healthy run constructs
    exactly one and the discovery listing behind
    :func:`resolve_model_candidates` is the only extra cost.

    Args:
        provider: Which vendor to call.
        mode: Which tier to use.
        **overrides: Forwarded to :func:`get_error_analysis_llm`.

    Yields:
        A resolved model id and a client configured for it.

    Raises:
        ValueError: If ``provider`` or ``mode`` is not recognized.
        ImportError: If the provider's LangChain package is not installed.
    """
    provider = normalize_provider(provider)
    mode = normalize_mode(mode)

    for position, model in enumerate(resolve_model_candidates(provider, mode)):
        if position:
            logger.warning(
                "Falling back to model %s (candidate %d) for provider=%s mode=%s",
                model,
                position + 1,
                provider,
                mode,
            )
        yield model, get_error_analysis_llm(provider, mode, model=model, **overrides)


def _import_chat_openai() -> Any:
    """Import ``ChatOpenAI``, the client shared by three of the five providers."""
    try:
        from langchain_openai import ChatOpenAI
    except ImportError as exc:  # pragma: no cover - depends on environment
        raise ImportError(
            "This provider requires the langchain-openai package. Install it "
            "with: pip install langchain-openai"
        ) from exc
    return ChatOpenAI
