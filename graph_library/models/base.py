"""Base classes shared by every LLM structured-output schema.

The ``LLM*`` response schemas in this package are Pydantic models for exactly
one reason: they are handed to ``ChatModel.with_structured_output()`` as the
response schema for a node's call. That makes them a transport contract with a
provider rather than graph state, and it means they all inherit the same
failure mode when a provider deviates from that contract.

:class:`_ToolArgumentEnvelope` is the repair for the one deviation observed so
far. It lives here, rather than beside any single node's schemas, because the
deviation belongs to the *provider*, not to the node — a schema that is bound as
a tool call needs the repair regardless of which node binds it.

This module holds no field definitions and imports nothing from the rest of
``graph_library``, so any model module may import it without risking a cycle.
"""

from __future__ import annotations

import logging
from typing import Any

from pydantic import BaseModel, model_validator

logger = logging.getLogger(__name__)

__all__ = ["_ToolArgumentEnvelope"]


class _ToolArgumentEnvelope(BaseModel):
    """Base class for a schema bound as a provider's tool-call response.

    Structured output is a tool call underneath, and the arguments of that call
    are supposed to *be* the schema. Anthropic's and DeepSeek's current
    generations sometimes return them nested one level deeper instead, in a
    single wrapper key::

        {"content": {"primary_error_signature_id": ..., "evaluations": [...]}}

    LangChain hands those arguments to the schema verbatim
    (``PydanticToolsParser`` calls ``Schema(**tool_call["args"])``), so the
    wrapper is what Pydantic sees, and every required field reads as missing::

        ValidationError: 3 validation errors for LLMErrorAnalysisResult
        primary_error_signature_id
          Field required [type=missing, input_value={'content': {'primary_err...}}]

    Unwrapping here rather than at the call site is what makes the repair
    universal: it runs wherever the schema is validated — inside the provider's
    own parser, where the node never sees the payload at all, as well as in the
    node's own ``model_validate`` — and it covers every provider rather than the
    one that was observed misbehaving.

    A schema whose fields all have defaults would otherwise fail *silently*
    rather than loudly:
    :class:`~graph_library.models.error_analysis.LLMSearchDecision` accepts
    ``LLMSearchDecision(content={...})`` without complaint under Pydantic's
    default ``extra="ignore"``, discards the wrapped queries and reports that
    the model asked for no search. That is the more dangerous half of this bug,
    and the reason every node's response schema inherits from here rather than
    only the ones seen to trip over it.
    """

    @model_validator(mode="before")
    @classmethod
    def _unwrap_tool_arguments(cls, data: Any) -> Any:
        """Peel a wrapper key off tool-call arguments, when there plainly is one.

        Deliberately conservative — three conditions must hold together, so a
        well-formed payload is never touched and an ambiguous one is passed
        through to fail loudly at validation rather than being guessed at:

            * no top-level key names a field of this schema (the payload is not
              already the right shape, possibly with extras);
            * exactly one top-level value is a dict that *does* name at least
              one field (the wrapper key itself is not inspected, since
              ``content`` is only the spelling seen so far);
            * only that one candidate exists.
        """
        if not isinstance(data, dict) or any(key in cls.model_fields for key in data):
            return data

        wrapped = [
            (key, value)
            for key, value in data.items()
            if isinstance(value, dict)
            and any(field in cls.model_fields for field in value)
        ]
        if len(wrapped) != 1:
            return data

        key, payload = wrapped[0]
        # Logged rather than silently repaired: the model deviated from the
        # schema contract it was given, which is worth knowing about when a
        # report reads oddly, and worth noticing if it ever becomes the norm.
        logger.warning(
            "%s arrived wrapped in a %r key; unwrapping the %d-key payload "
            "inside it",
            cls.__name__,
            key,
            len(payload),
        )
        return payload
