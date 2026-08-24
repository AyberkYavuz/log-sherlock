"""A mock OpenAI-compatible server for exercising the ``local`` provider.

The Error Analysis Node's ``local`` provider targets any server that speaks the
OpenAI chat-completions protocol (vLLM, Ollama, LM Studio). This module is the
smallest such server that satisfies the node, so the full path — factory →
``ChatOpenAI`` → HTTP → structured-output parsing → merge — can be exercised
without an API key, a network or a GPU.

The response is **derived from the request**, not canned: the handler reads the
``signature_id`` values out of the prompt and returns one evaluation per
signature, nominating the last one listed as the root cause. The node sends its
signatures ranked by descending count, so that is the lowest-volume signature —
the one the system prompt's own heuristic points at. Deriving the response makes
this a real test of the node's merge logic (ids must line up, every signature
must be covered) rather than a fixed blob that would pass even if the node sent
the wrong batch. It performs no reasoning and is not a model.

Both transports the protocol defines are served, because the client picks
between them and both are reached in practice:

    * a single JSON ``chat.completion`` body when ``stream`` is false — the
      shape a plain ``.invoke()`` gets;
    * ``text/event-stream`` chunks when ``stream`` is true — the shape
      LangGraph asks for whenever a run subscribes to token streaming, which
      LangGraph Studio does on every graph run.

Serving only the first is *silently* wrong: the client's SSE decoder finds no
events in a JSON body, ends the stream having consumed zero chunks, and the
OpenAI SDK then trips a bare ``assert`` on its own empty snapshot. That reaches
the node as ``AssertionError`` with no message and no hint of where it came
from. See :func:`iter_completion_chunks`.

Nothing in this module raises on a malformed request. An exception inside the
ASGI app propagates out of the transport, through the OpenAI client and into
the node's ``except Exception`` fallback, where it surfaces as an unexplained
"LLM reasoning unavailable" note rather than a test failure that points here.
Missing or unexpected fields are therefore read defensively and answered with a
well-formed response.

Run it standalone::

    python -m uvicorn tests.mock_local_llm:app --port 8000

then point the node at it::

    LOCAL_LLM_BASE_URL=http://127.0.0.1:8000/v1

Or use it in-process, without a socket, via :func:`make_transport` — see
``tests/test_error_analysis.py``.
"""

from __future__ import annotations

import asyncio
import json
import re
from collections.abc import Iterator
from typing import Any

import httpx
from fastapi import FastAPI, Response
from fastapi.responses import JSONResponse, StreamingResponse

app = FastAPI(title="LogSherlock mock local LLM")

#: Pulls the signature ids out of the JSON batch the node renders into the
#: prompt. Deliberately a regex over the raw text: the mock stands in for a
#: model, which likewise only ever sees a string.
_SIGNATURE_ID_PATTERN = re.compile(r'"signature_id":\s*"([^"]+)"')

#: Model id echoed back in the response envelope.
MODEL_NAME = "mock-local-llm"

#: How many pieces a streamed payload is cut into. More than one on purpose:
#: a single-chunk stream would not exercise the client's accumulator, which is
#: the half of the streaming path most likely to break.
STREAM_CHUNK_COUNT = 4


def extract_signature_ids(messages: list[dict[str, Any]]) -> list[str]:
    """Read the signature ids out of a chat-completions message list.

    Args:
        messages: The ``messages`` array from the request body.

    Returns:
        The ids in prompt order, de-duplicated. Empty when the prompt carries
        none, which the caller renders as a "no signatures" response.
    """
    text = "\n".join(
        message["content"]
        for message in messages
        # Content is a list for multimodal turns and absent for tool results;
        # neither carries a prompt, and neither may crash the handler.
        if isinstance(message, dict) and isinstance(message.get("content"), str)
    )
    seen: dict[str, None] = {}
    for signature_id in _SIGNATURE_ID_PATTERN.findall(text):
        seen.setdefault(signature_id, None)
    return list(seen)


def build_analysis_payload(signature_ids: list[str]) -> dict[str, Any]:
    """Build a response body matching :class:`~models.LLMErrorAnalysisResult`.

    Every id gets an evaluation, so the node's "did not evaluate" note stays
    silent and a genuine coverage gap in the node would show up. The last id —
    the lowest-volume signature, since the node ranks by descending count — is
    nominated as the root cause; the rest are marked as downstream fallout.

    Args:
        signature_ids: The ids read out of the prompt, in prompt order.

    Returns:
        A plain dict with the schema's three fields. Keys and types match
        exactly; the caller serializes it. With no ids the root cause is
        ``None``, which is the schema's way of saying "cannot single one out".
    """
    if not signature_ids:
        return {
            "primary_error_signature_id": None,
            "cascading_impact_summary": (
                "No error signatures were supplied, so there is no cascade to "
                "describe."
            ),
            "evaluations": [],
        }

    primary = signature_ids[-1]
    secondary_count = len(signature_ids) - 1

    return {
        "primary_error_signature_id": primary,
        "cascading_impact_summary": (
            f"{primary} is the lowest-volume failure in the batch and is "
            f"nominated as the trigger; the remaining {secondary_count} "
            f"signature(s) are treated as downstream fallout from it."
        ),
        "evaluations": [
            {
                "signature_id": signature_id,
                "is_root_cause_candidate": signature_id == primary,
                "explanation": (
                    f"{signature_id} is nominated as the root cause: it is the "
                    "lowest-volume signature in the batch."
                    if signature_id == primary
                    else f"{signature_id} is a downstream consequence of {primary}."
                ),
            }
            for signature_id in signature_ids
        ],
    }


def resolve_tool_name(body: dict[str, Any]) -> str | None:
    """Return the tool the request asked to be called, if it sent any.

    The name is echoed from the request rather than hardcoded: the client
    matches the returned call by name against the schema it sent, so a guess
    that drifts from the schema's title silently drops the call.

    Args:
        body: The decoded chat-completions request.

    Returns:
        The first tool's function name, or ``None`` when the request carries no
        ``tools`` — which is the case for the ``json_schema`` path.
    """
    tools = body.get("tools") or []
    if not isinstance(tools, list) or not tools or not isinstance(tools[0], dict):
        return None
    function = tools[0].get("function")
    if not isinstance(function, dict):
        return None
    name = function.get("name")
    return name if isinstance(name, str) and name else "LLMErrorAnalysisResult"


def _serialized_payload(body: dict[str, Any]) -> str:
    """Render the analysis payload for this request as a JSON string.

    A *string*, in both the content and the tool-arguments position: the wire
    format for tool arguments is text, and a raw object there fails the
    client's parse.
    """
    return json.dumps(build_analysis_payload(extract_signature_ids(body.get("messages") or [])))


def _split_evenly(text: str, parts: int) -> list[str]:
    """Cut ``text`` into ``parts`` non-empty pieces, as a token stream would."""
    if parts < 2 or len(text) < parts:
        return [text]
    size = -(-len(text) // parts)  # ceiling, so the pieces cover the whole text
    return [text[start : start + size] for start in range(0, len(text), size)]


def build_completion_response(body: dict[str, Any]) -> dict[str, Any]:
    """Wrap the analysis payload in a non-streaming chat-completion envelope.

    Both structured-output methods are served from the same payload, because
    which one is used is the client's choice, not the server's:
    ``with_structured_output`` currently sends ``response_format:
    {"type": "json_schema"}`` and reads ``message.content``, but a client
    configured with ``method="function_calling"`` sends ``tools`` and reads
    ``message.tool_calls`` instead.

    Args:
        body: The decoded chat-completions request.

    Returns:
        The response envelope, ready to be serialized.
    """
    serialized = _serialized_payload(body)
    tool_name = resolve_tool_name(body)

    if tool_name is not None:
        message: dict[str, Any] = {
            "role": "assistant",
            # Null alongside a tool call, as a real provider sends it.
            "content": None,
            "tool_calls": [
                {
                    "id": "call_mock_0",
                    "type": "function",
                    "function": {"name": tool_name, "arguments": serialized},
                }
            ],
        }
    else:
        message = {"role": "assistant", "content": serialized}

    return {
        "id": "chatcmpl-mock-123",
        "object": "chat.completion",
        "created": 1700000000,
        "model": body.get("model") or MODEL_NAME,
        "choices": [
            {
                "index": 0,
                "message": message,
                "finish_reason": "tool_calls" if tool_name is not None else "stop",
            }
        ],
        "usage": {
            "prompt_tokens": 100,
            "completion_tokens": 50,
            "total_tokens": 150,
        },
    }


def build_completion_chunks(body: dict[str, Any]) -> list[dict[str, Any]]:
    """Build the ``chat.completion.chunk`` sequence for a streaming request.

    The same payload as :func:`build_completion_response`, delivered the way a
    streaming server delivers it: an opening chunk that declares the role, one
    chunk per slice of the payload, and a terminal chunk carrying the
    ``finish_reason`` and no delta. Clients accumulate these into a single
    message, so the concatenated slices must reproduce the payload exactly.

    Args:
        body: The decoded chat-completions request.

    Returns:
        The chunks in wire order, excluding the ``[DONE]`` sentinel.
    """
    serialized = _serialized_payload(body)
    tool_name = resolve_tool_name(body)
    pieces = _split_evenly(serialized, STREAM_CHUNK_COUNT)

    def envelope(delta: dict[str, Any], finish_reason: str | None) -> dict[str, Any]:
        return {
            "id": "chatcmpl-mock-123",
            "object": "chat.completion.chunk",
            "created": 1700000000,
            "model": body.get("model") or MODEL_NAME,
            "choices": [{"index": 0, "delta": delta, "finish_reason": finish_reason}],
        }

    if tool_name is not None:
        # The id, type and name arrive once, on the opening tool-call delta;
        # every later delta carries only more argument text, addressed by the
        # same ``index``. Repeating the name would append it to itself.
        chunks = [
            envelope(
                {
                    "role": "assistant",
                    "tool_calls": [
                        {
                            "index": 0,
                            "id": "call_mock_0",
                            "type": "function",
                            "function": {"name": tool_name, "arguments": ""},
                        }
                    ],
                },
                None,
            )
        ]
        chunks += [
            envelope(
                {"tool_calls": [{"index": 0, "function": {"arguments": piece}}]}, None
            )
            for piece in pieces
        ]
        chunks.append(envelope({}, "tool_calls"))
    else:
        chunks = [envelope({"role": "assistant", "content": ""}, None)]
        chunks += [envelope({"content": piece}, None) for piece in pieces]
        chunks.append(envelope({}, "stop"))

    # Only sent when asked for; an unsolicited usage chunk is a protocol
    # violation that some clients reject.
    stream_options = body.get("stream_options")
    if isinstance(stream_options, dict) and stream_options.get("include_usage"):
        usage_chunk = envelope({}, None)
        usage_chunk["choices"] = []
        usage_chunk["usage"] = {
            "prompt_tokens": 100,
            "completion_tokens": 50,
            "total_tokens": 150,
        }
        chunks.append(usage_chunk)

    return chunks


def iter_completion_chunks(body: dict[str, Any]) -> Iterator[str]:
    """Render the streaming response as server-sent events.

    Each event is ``data: <json>`` followed by a blank line, and the stream is
    closed by the ``[DONE]`` sentinel. Getting this framing wrong does not
    produce an error the client can report: its SSE decoder simply finds no
    events, the stream ends having yielded nothing, and the OpenAI SDK asserts
    on its own un-initialized snapshot — surfacing as a bare ``AssertionError``
    with no message, four libraries away from the cause.

    Args:
        body: The decoded chat-completions request.

    Yields:
        The encoded events, in wire order.
    """
    for chunk in build_completion_chunks(body):
        yield f"data: {json.dumps(chunk)}\n\n"
    yield "data: [DONE]\n\n"


@app.post("/v1/chat/completions")
async def chat_completions(body: dict[str, Any]) -> Response:
    """The one endpoint the ``local`` provider needs.

    Honours ``stream``. A client that asked for events and got a JSON body
    fails in a way that names neither this server nor the request that caused
    it, so the branch is not optional.
    """
    if body.get("stream"):
        return StreamingResponse(
            iter_completion_chunks(body),
            media_type="text/event-stream",
        )
    return JSONResponse(build_completion_response(body))


@app.get("/v1/models")
async def list_models() -> JSONResponse:
    """Advertise the served model; some clients probe this on startup."""
    return JSONResponse(
        {
            "object": "list",
            "data": [{"id": MODEL_NAME, "object": "model", "owned_by": "logsherlock"}],
        }
    )


class SyncASGITransport(httpx.BaseTransport):
    """Serve an ASGI app to a *synchronous* httpx client, in-process.

    ``httpx.ASGITransport`` only implements the async half of the transport
    protocol, but the OpenAI SDK that ``ChatOpenAI`` wraps drives a sync
    ``httpx.Client``. This adapter runs each request through the ASGI app on a
    private event loop and hands the result back synchronously, which is what
    lets a test exercise the real client — real request building, real response
    parsing — with no socket and nothing to tear down.
    """

    def __init__(self, asgi_app: Any) -> None:
        self._app = asgi_app

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        body = request.read()

        async def call() -> httpx.Response:
            transport = httpx.ASGITransport(app=self._app)
            async with httpx.AsyncClient(
                transport=transport, base_url="http://mock"
            ) as client:
                return await client.request(
                    request.method,
                    request.url,
                    content=body,
                    headers={
                        key: value
                        for key, value in request.headers.items()
                        # Dropped because httpx recomputes them for the inner
                        # request; forwarding the outer values would conflict.
                        if key.lower() not in {"content-length", "host"}
                    },
                )

        response = asyncio.run(call())
        return httpx.Response(
            response.status_code,
            content=response.content,
            headers={"content-type": response.headers.get("content-type", "application/json")},
            request=request,
        )


def make_transport() -> httpx.BaseTransport:
    """Return a sync httpx transport that serves this app in-process.

    Pass the resulting client to the factory::

        import httpx
        client = httpx.Client(transport=make_transport(), base_url="http://mock")
        llm = get_error_analysis_llm("local", "fast", http_client=client)
    """
    return SyncASGITransport(app)


if __name__ == "__main__":  # pragma: no cover - manual entry point
    import uvicorn

    uvicorn.run(app, host="127.0.0.1", port=8000)
