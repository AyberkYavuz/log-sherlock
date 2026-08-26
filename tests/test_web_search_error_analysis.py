"""Tests for the optional web-search path (``graph_library/web_search/`` + the two passes).

Everything here runs offline. Two seams are stubbed and nothing else:

    * ``graph_library.error_analysis.node.iter_error_analysis_llms`` — a fake chat model that
      answers the decision pass and the analysis pass differently, dispatching
      on the structured-output schema it is handed, and recording every prompt
      it was given so a test can assert what the model actually saw;
    * ``graph_library.web_search.node.run_web_search`` — canned snippets, so the graph-level
      tests exercise the real node, the real router and the real prompt
      builder without touching Tavily.

Below that, the client's own tests drive :func:`run_web_search` with a fake
client object, so the relevance floor and the failure handling are covered
against the response shape Tavily really returns.

The conventions asserted here:

    * the flag is off by default and off means *nothing happens* — no decision
      call, no search node, no change to the output;
    * an ordinary error payload costs one extra call and stops there, because
      the decision pass is expected to answer "no";
    * the loop runs at most one lap, guaranteed by ``search_context`` always
      being written as a list — the failure paths are what prove this, since
      they are the ones tempted to leave it unset;
    * fanning back in still works: ``recommendation`` runs exactly once whether
      or not the error-analysis branch took the detour.
"""

from __future__ import annotations

import json
import re
from typing import Any

import pytest

from graph_library.error_analysis import (
    build_analysis_prompt,
    build_error_summary,
    error_analysis_node,
)
from graph_library.error_analysis.node import build_search_decision_prompt, decide_search_queries
from graph import compile_graph, route_after_error_analysis
from graph_library.models import (
    MAX_SEARCH_QUERIES,
    LLMErrorAnalysisResult,
    LLMErrorSignatureEvaluation,
    LLMSearchDecision,
)
from graph_library.parser.parser_node import parser_node
from graph_library.web_search import MIN_RELEVANCE_SCORE, format_result, run_web_search
from graph_library.web_search.client import SNIPPET_MAX_LENGTH, build_client
from graph_library.web_search.node import web_search_node

# ===========================================================================
# Log payloads
# ===========================================================================

#: The everyday kind. A senior engineer needs no documentation for these, so
#: the decision pass is expected to ask for no queries.
COMMON_LOGS = "\n".join(
    [
        '{"level":50,"time":1722250761422,"name":"db","msg":"Connection refused:'
        ' could not connect to database at 10.0.0.5:5432"}',
        '{"level":50,"time":1722250761423,"name":"api","msg":"Request 8f2c failed:'
        ' upstream error"}',
        '{"level":50,"time":1722250761424,"name":"api","msg":"Request 91ab failed:'
        ' upstream error"}',
    ]
)

#: A synthetic vendor code that exists nowhere. Exactly the shape the decision
#: pass is meant to catch, and — see ``test_the_niche_code_survives_masking`` —
#: one the fingerprinting pass leaves intact for it to read.
NICHE_CODE = "ERR_X99_VIRTUAL_BUFFER_OVERFLOW_MEM_LOCK"

NICHE_LOGS = "\n".join(
    [
        '{"level":50,"time":1722250761422,"name":"vbuf","msg":"'
        f'{NICHE_CODE}: virtual buffer lock could not be acquired"}}',
        '{"level":50,"time":1722250761423,"name":"api","msg":"Request 8f2c failed:'
        ' upstream error"}',
    ]
)

#: What the stubbed search brings back for the niche code.
SNIPPET = (
    f"[query: {NICHE_CODE} virtual buffer]\n"
    "VBuf runtime error reference — https://example.invalid/vbuf/errors\n"
    f"{NICHE_CODE} is raised when the virtual buffer allocator cannot take the "
    "memory lock, usually because the host ran out of hugepages."
)


def _parsed(raw_logs: str) -> list[dict[str, Any]]:
    """Run the real parser, so these tests see the records the node will."""
    return parser_node({"raw_logs": raw_logs})["parsed_logs"]


def _signatures(raw_logs: str) -> list[dict[str, Any]]:
    """The deterministic signatures for a payload, via the real pipeline."""
    return build_error_summary(_parsed(raw_logs))[0]["signatures"]


# ===========================================================================
# Test doubles
# ===========================================================================


class _Recorder:
    """Every structured call the node made, in order."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, str, str]] = []

    def add(self, schema_name: str, messages: Any) -> None:
        system, human = (str(content) for _role, content in messages)
        self.calls.append((schema_name, system, human))

    @property
    def schemas(self) -> list[str]:
        return [schema for schema, _system, _human in self.calls]

    def human_prompt(self, schema_name: str) -> str:
        """The human turn of the (single) call made with ``schema_name``."""
        matches = [human for schema, _s, human in self.calls if schema == schema_name]
        assert len(matches) == 1, f"expected 1 {schema_name} call, got {len(matches)}"
        return matches[0]


def _analysis_from_prompt(prompt: str) -> LLMErrorAnalysisResult:
    """Answer the analysis pass using only what the prompt actually contained.

    Deriving the response from the prompt — rather than hard-coding one — is
    what lets a test tell whether the retrieved documentation reached pass 2:
    the summary says so only when the reference section was really there.
    """
    ids = sorted(set(re.findall(r"ERR_\d+", prompt)))
    cited = "EXTERNAL REFERENCE MATERIAL" in prompt

    return LLMErrorAnalysisResult(
        primary_error_signature_id=ids[0] if ids else None,
        cascading_impact_summary=(
            "The retrieved documentation identifies the allocator as the trigger."
            if cited
            else "The first error triggered the rest."
        ),
        evaluations=[
            LLMErrorSignatureEvaluation(
                signature_id=signature_id,
                is_root_cause_candidate=(index == 0),
                explanation=(
                    "Explained with help from the hugepages documentation."
                    if cited and index == 0
                    else "Downstream of the first error."
                ),
            )
            for index, signature_id in enumerate(ids)
        ],
    )


class _FakeStructuredLLM:
    def __init__(self, schema: Any, decision: Any, recorder: _Recorder) -> None:
        self._schema = schema
        self._decision = decision
        self._recorder = recorder

    def invoke(self, messages: Any) -> Any:
        self._recorder.add(self._schema.__name__, messages)
        if self._schema is LLMSearchDecision:
            if isinstance(self._decision, Exception):
                raise self._decision
            return self._decision
        return _analysis_from_prompt(messages[-1][1])


class _FakeLLM:
    """A chat model that answers each pass according to the schema it is given."""

    def __init__(self, decision: Any, recorder: _Recorder) -> None:
        self._decision = decision
        self._recorder = recorder

    def with_structured_output(self, schema: Any, **_: Any) -> _FakeStructuredLLM:
        return _FakeStructuredLLM(schema, self._decision, self._recorder)


def _install_llm(
    monkeypatch: pytest.MonkeyPatch, *, queries: list[str] | Exception
) -> _Recorder:
    """Point the error-analysis node at a fake model.

    Args:
        queries: What the decision pass should answer with — a list of query
            strings, or an exception for it to raise.
    """
    recorder = _Recorder()
    decision = (
        queries
        if isinstance(queries, Exception)
        else LLMSearchDecision(queries=queries, reasoning="because")
    )
    fake = _FakeLLM(decision, recorder)

    def factory(provider: str = "openai", mode: str = "standard", **_: Any) -> Any:
        yield "fake-model", fake

    monkeypatch.setattr("graph_library.error_analysis.node.iter_error_analysis_llms", factory)
    return recorder


def _install_search(
    monkeypatch: pytest.MonkeyPatch,
    *,
    snippets: list[str] | None = None,
    notes: list[str] | None = None,
    error: Exception | None = None,
) -> list[list[str]]:
    """Stub the Tavily round trip; return the list that records its calls."""
    calls: list[list[str]] = []

    def fake_run(queries: list[str], **_: Any) -> tuple[list[str], list[str]]:
        calls.append(list(queries))
        if error is not None:
            raise error
        return list(snippets or []), list(notes or [])

    monkeypatch.setattr("graph_library.web_search.node.run_web_search", fake_run)
    return calls


class _FakeTavily:
    """Stands in for ``TavilyClient``, returning canned payloads per query."""

    def __init__(self, payloads: dict[str, Any]) -> None:
        self._payloads = payloads
        self.queries: list[str] = []
        self.kwargs: list[dict[str, Any]] = []

    def search(self, query: str, **kwargs: Any) -> Any:
        self.queries.append(query)
        self.kwargs.append(kwargs)
        payload = self._payloads[query]
        if isinstance(payload, Exception):
            raise payload
        return payload


def _tavily_result(score: float | None, url: str = "https://example.invalid/a") -> dict:
    """One entry shaped like a real Tavily result."""
    result: dict[str, Any] = {
        "title": "Some documentation page",
        "url": url,
        "content": "The allocator could not take the lock.",
    }
    if score is not None:
        result["score"] = score
    return result


# ===========================================================================
# The three headline scenarios, driven through the compiled graph
# ===========================================================================


def test_web_search_disabled(monkeypatch: pytest.MonkeyPatch) -> None:
    """Off by default: no decision call, no search node, analysis unchanged."""
    recorder = _install_llm(monkeypatch, queries=["should never be asked for"])
    searches = _install_search(monkeypatch, snippets=[SNIPPET])

    final = compile_graph().invoke(
        {
            "application_name": "checkout",
            "raw_logs": NICHE_LOGS,
            "enable_web_search": False,
        }
    )

    # The web search node never ran, and neither did the pass that precedes it:
    # with the flag off the node makes exactly the one call it always made.
    assert searches == []
    assert "web_search" not in final["completed_stages"]
    assert recorder.schemas == ["LLMErrorAnalysisResult"]

    # ...and the analysis is complete and unenriched.
    assert "error_analysis" in final["completed_stages"]
    assert final["error_summary"]["primary_error_signature_id"] == "ERR_001"
    assert "EXTERNAL REFERENCE MATERIAL" not in recorder.human_prompt(
        "LLMErrorAnalysisResult"
    )


def test_web_search_omitted_from_state_behaves_as_disabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The flag is opt-in, so its absence must mean off rather than unset."""
    recorder = _install_llm(monkeypatch, queries=["should never be asked for"])
    searches = _install_search(monkeypatch, snippets=[SNIPPET])

    final = compile_graph().invoke(
        {"application_name": "checkout", "raw_logs": NICHE_LOGS}
    )

    assert searches == []
    assert recorder.schemas == ["LLMErrorAnalysisResult"]
    assert "error_analysis" in final["completed_stages"]


def test_standard_error_skips_search(monkeypatch: pytest.MonkeyPatch) -> None:
    """Enabled, but a common payload: the decision pass declines and stops."""
    recorder = _install_llm(monkeypatch, queries=[])
    searches = _install_search(monkeypatch, snippets=[SNIPPET])

    final = compile_graph().invoke(
        {
            "application_name": "checkout",
            "raw_logs": COMMON_LOGS,
            "enable_web_search": True,
        }
    )

    # The decision ran and cost one call; the search did not run at all.
    assert recorder.schemas == ["LLMSearchDecision", "LLMErrorAnalysisResult"]
    assert searches == []
    assert "web_search" not in final["completed_stages"]

    # "Decided, and the answer was no" — an empty list, never None, which is
    # what tells the router the loop is over.
    assert final["search_context"] == []
    assert not final.get("search_queries")

    # The analysis itself is untouched by the detour not being taken.
    assert final["error_summary"]["primary_error_signature_id"] == "ERR_001"
    assert "EXTERNAL REFERENCE MATERIAL" not in recorder.human_prompt(
        "LLMErrorAnalysisResult"
    )


def test_niche_error_triggers_search(monkeypatch: pytest.MonkeyPatch) -> None:
    """Enabled, and an obscure code: decide, search, then analyze with context."""
    query = f"{NICHE_CODE} virtual buffer"
    recorder = _install_llm(monkeypatch, queries=[query])
    searches = _install_search(monkeypatch, snippets=[SNIPPET])

    final = compile_graph().invoke(
        {
            "application_name": "checkout",
            "raw_logs": NICHE_LOGS,
            "enable_web_search": True,
        }
    )

    # 1. The decision pass produced queries, and they reached the search node
    #    through graph state rather than a direct call.
    assert searches == [[query]]
    assert final["search_queries"] == [query]

    # 2. The web search node ran, via the package, and wrote its findings.
    assert "web_search" in final["completed_stages"]
    assert final["search_context"] == [SNIPPET]

    # 3. Both passes ran, in order, and the second one saw the documentation.
    assert recorder.schemas == ["LLMSearchDecision", "LLMErrorAnalysisResult"]
    analysis_prompt = recorder.human_prompt("LLMErrorAnalysisResult")
    assert "EXTERNAL REFERENCE MATERIAL" in analysis_prompt
    assert "hugepages" in analysis_prompt
    # The decision pass, by contrast, is asked before anything is retrieved.
    assert "EXTERNAL REFERENCE MATERIAL" not in recorder.human_prompt(
        "LLMSearchDecision"
    )

    # 4. The retrieved context shows up in the published explanation.
    summary = final["error_summary"]
    assert "retrieved documentation" in summary["cascading_impact_summary"]
    assert "hugepages" in summary["signatures"][0]["explanation"]

    # 5. The detour is reported rather than silent.
    assert any(
        "Web search: ran 1 query" in note for note in final["investigation_notes"]
    )


# ===========================================================================
# Fan-in and loop bounds — what the routing change could plausibly break
# ===========================================================================


@pytest.mark.parametrize(
    ("raw_logs", "queries", "expected_stages"),
    [
        (COMMON_LOGS, [], 0),
        (NICHE_LOGS, ["a niche query"], 1),
    ],
    ids=["without-detour", "with-detour"],
)
def test_recommendation_runs_once_whether_or_not_the_branch_loops(
    monkeypatch: pytest.MonkeyPatch,
    raw_logs: str,
    queries: list[str],
    expected_stages: int,
) -> None:
    # The failure this guards against is silent and specific to the
    # conditional edge: LangGraph treats it as a trigger separate from the
    # plain edges, so without ``defer`` the join fires early and
    # ``recommendation`` runs twice — once on a state with no error_summary.
    _install_llm(monkeypatch, queries=queries)
    _install_search(monkeypatch, snippets=[SNIPPET])

    final = compile_graph().invoke(
        {"raw_logs": raw_logs, "enable_web_search": True}
    )
    stages = final["completed_stages"]

    assert stages.count("recommendation") == 1
    assert stages.count("report_generator") == 1
    assert stages.count("error_analysis") == 1
    assert stages.count("web_search") == expected_stages
    # Every analysis stage still fans in, the detour notwithstanding.
    for stage in ("error_analysis", "statistics", "timeline", "pattern_analysis"):
        assert stage in stages


def test_the_loop_runs_at_most_one_lap(monkeypatch: pytest.MonkeyPatch) -> None:
    # ``search_queries`` stays in state after the search, so the only thing
    # stopping a second lap is ``search_context`` no longer being None. This
    # asserts that with the search returning nothing at all — the case where a
    # node might be tempted to leave the field unset.
    _install_llm(monkeypatch, queries=["a niche query"])
    searches = _install_search(monkeypatch, snippets=[])

    final = compile_graph().invoke(
        {"raw_logs": NICHE_LOGS, "enable_web_search": True}
    )

    assert len(searches) == 1
    assert final["completed_stages"].count("web_search") == 1
    assert final["search_context"] == []
    assert final["error_summary"]["primary_error_signature_id"] == "ERR_001"


def test_a_failed_search_still_completes_the_investigation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_llm(monkeypatch, queries=["a niche query"])
    _install_search(monkeypatch, error=RuntimeError("TAVILY_API_KEY is not set"))

    final = compile_graph().invoke(
        {"raw_logs": NICHE_LOGS, "enable_web_search": True}
    )

    assert final["search_context"] == []
    assert final["error_summary"]["primary_error_signature_id"] == "ERR_001"
    assert final["completed_stages"].count("recommendation") == 1
    assert any(
        "Web search: unavailable" in note for note in final["investigation_notes"]
    )


# ===========================================================================
# The router in isolation
# ===========================================================================


@pytest.mark.parametrize(
    ("state", "expected"),
    [
        ({}, "recommendation"),
        ({"search_queries": [], "search_context": None}, "recommendation"),
        ({"search_queries": ["q"], "search_context": None}, "web_search"),
        ({"search_queries": ["q"], "search_context": []}, "recommendation"),
        ({"search_queries": ["q"], "search_context": ["snippet"]}, "recommendation"),
    ],
    ids=["empty", "no-queries", "asked", "answered-empty", "answered"],
)
def test_router_sends_work_to_web_search_exactly_once(
    state: dict[str, Any], expected: str
) -> None:
    assert route_after_error_analysis(state) == expected


# ===========================================================================
# The two passes, at the node level
# ===========================================================================


def test_decision_pass_returns_only_the_queries(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Pass 1 has not finished the node's work, so it must claim neither the
    # completed stage nor a summary — the router is about to send it away and
    # it will be back.
    _install_llm(monkeypatch, queries=["a niche query"])

    delta = error_analysis_node(
        {"parsed_logs": _parsed(NICHE_LOGS), "enable_web_search": True}
    )

    assert delta == {"search_queries": ["a niche query"]}


def test_fingerprint_notes_are_not_duplicated_across_the_two_passes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # ``investigation_notes`` uses an additive reducer, so a note emitted by
    # both passes would appear twice in the final report.
    _install_llm(monkeypatch, queries=["a niche query"])
    _install_search(monkeypatch, snippets=[SNIPPET])

    final = compile_graph().invoke(
        {"raw_logs": NICHE_LOGS, "enable_web_search": True}
    )

    fingerprint_notes = [
        note
        for note in final["investigation_notes"]
        if note.startswith("Error analysis: fingerprinted")
    ]
    assert len(fingerprint_notes) == 1


def test_a_failed_decision_falls_through_to_the_analysis(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # An optional enhancement must not be able to break an investigation that
    # would otherwise have succeeded, so a broken decision call degrades to the
    # behaviour of the flag being off.
    recorder = _install_llm(monkeypatch, queries=RuntimeError("decision exploded"))

    delta = error_analysis_node(
        {"parsed_logs": _parsed(NICHE_LOGS), "enable_web_search": True}
    )

    assert recorder.schemas == ["LLMSearchDecision", "LLMErrorAnalysisResult"]
    assert delta["search_context"] == []
    assert delta["completed_stages"] == ["error_analysis"]
    assert delta["error_summary"]["primary_error_signature_id"] == "ERR_001"


def test_decision_pass_is_skipped_when_there_is_nothing_to_analyze(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # No signatures means no report to enrich; searching would be a round trip
    # spent decorating an empty summary.
    recorder = _install_llm(monkeypatch, queries=["should never be asked for"])

    delta = error_analysis_node({"parsed_logs": [], "enable_web_search": True})

    assert recorder.calls == []
    assert delta["error_summary"]["signatures"] == []
    assert delta["completed_stages"] == ["error_analysis"]


def test_decision_pass_enforces_the_query_cap(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The cap reaches the model as a schema description, which is a request;
    # this is what makes it a limit.
    too_many = [f"query {index}" for index in range(MAX_SEARCH_QUERIES + 4)]
    _install_llm(monkeypatch, queries=too_many)

    queries = decide_search_queries("openai", "standard", _signatures(NICHE_LOGS))

    assert queries == too_many[:MAX_SEARCH_QUERIES]


def test_decision_pass_discards_blank_queries(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_llm(monkeypatch, queries=["  ", "", "  real query  "])

    assert decide_search_queries("openai", "standard", _signatures(NICHE_LOGS)) == [
        "real query"
    ]


# ===========================================================================
# Prompts
# ===========================================================================


def test_the_niche_code_survives_masking() -> None:
    # The whole decision pass rests on the model being able to read the odd
    # code; a masking rule that ate the digits would defeat it silently.
    templates = [signature["template"] for signature in _signatures(NICHE_LOGS)]
    assert any(NICHE_CODE in template for template in templates)


def test_analysis_prompt_is_unchanged_without_context() -> None:
    signatures = _signatures(NICHE_LOGS)

    assert build_analysis_prompt(signatures) == build_analysis_prompt(
        signatures, search_context=None
    )
    # An empty list is the "we looked and found nothing" case, which has
    # nothing to add to a prompt either.
    assert build_analysis_prompt(signatures) == build_analysis_prompt(
        signatures, search_context=[]
    )


def test_analysis_prompt_fences_off_the_reference_material() -> None:
    prompt = build_analysis_prompt(
        _signatures(NICHE_LOGS), search_context=[SNIPPET, "second snippet"]
    )

    assert "EXTERNAL REFERENCE MATERIAL" in prompt
    assert "[1] " in prompt and "[2] " in prompt
    assert "hugepages" in prompt
    # The snippets must not be able to pass themselves off as findings.
    assert "not as evidence" in prompt
    assert prompt.index("EXTERNAL REFERENCE MATERIAL") > prompt.index("signature_id")


def test_decision_prompt_shows_wording_and_withholds_the_arithmetic() -> None:
    prompt = build_search_decision_prompt(
        _signatures(NICHE_LOGS), application_name="checkout"
    )

    assert "checkout" in prompt
    assert NICHE_CODE in prompt
    # Counts and timings are evidence for causation, which is pass 2's
    # question, not this one's.
    payload = json.loads(prompt[prompt.index("[") : prompt.rindex("]") + 1])
    assert set(payload[0]) == {"signature_id", "template", "severity", "loggers"}


# ===========================================================================
# The web search node
# ===========================================================================


def test_node_publishes_snippets_and_says_what_it_did(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_search(monkeypatch, snippets=[SNIPPET], notes=["a client note"])

    delta = web_search_node({"search_queries": ["one query"]})

    assert delta["search_context"] == [SNIPPET]
    assert delta["completed_stages"] == ["web_search"]
    assert delta["investigation_notes"][0] == (
        "Web search: ran 1 query and retrieved 1 relevant snippet."
    )
    assert "a client note" in delta["investigation_notes"]


@pytest.mark.parametrize(
    "failure",
    [
        RuntimeError("Web search is enabled but TAVILY_API_KEY is not set."),
        ImportError("Web search requires the tavily-python package."),
        OSError("network is unreachable"),
    ],
    ids=["no-credential", "no-package", "network-down"],
)
def test_node_always_writes_a_list_however_it_fails(
    monkeypatch: pytest.MonkeyPatch, failure: Exception
) -> None:
    # This is the loop's termination condition, so it is asserted against every
    # failure mode rather than a representative one.
    _install_search(monkeypatch, error=failure)

    delta = web_search_node({"search_queries": ["one query"]})

    assert delta["search_context"] == []
    assert delta["completed_stages"] == ["web_search"]
    assert any("Web search: unavailable" in n for n in delta["investigation_notes"])


def test_node_writes_a_list_even_with_nothing_to_search_for() -> None:
    delta = web_search_node({})

    assert delta["search_context"] == []
    assert delta["completed_stages"] == ["web_search"]


# ===========================================================================
# The Tavily client
# ===========================================================================


def test_client_drops_results_below_the_relevance_floor() -> None:
    # Calibrated against the live API: a query with a real answer scores ~0.65,
    # while a query for an invented code still returns three pages at 0.12-0.35.
    # Passing those on would invite an explanation sourced from the wrong error.
    client = _FakeTavily(
        {
            "q": {
                "results": [
                    _tavily_result(0.65, "https://example.invalid/good"),
                    _tavily_result(0.21, "https://example.invalid/noise"),
                ]
            }
        }
    )

    snippets, notes = run_web_search(["q"], client=client)

    assert len(snippets) == 1
    assert "good" in snippets[0]
    assert notes == []


def test_client_reports_a_query_that_found_nothing_relevant() -> None:
    client = _FakeTavily({"q": {"results": [_tavily_result(0.1)]}})

    snippets, notes = run_web_search(["q"], client=client)

    assert snippets == []
    assert len(notes) == 1
    assert str(MIN_RELEVANCE_SCORE) in notes[0]


def test_client_keeps_a_result_that_carries_no_score() -> None:
    # The floor rejects what Tavily itself rates poorly; an absent rating is
    # not a poor one.
    client = _FakeTavily({"q": {"results": [_tavily_result(None)]}})

    snippets, _ = run_web_search(["q"], client=client)

    assert len(snippets) == 1


def test_one_failing_query_does_not_cancel_the_others() -> None:
    client = _FakeTavily(
        {
            "dead": OSError("connection reset"),
            "live": {"results": [_tavily_result(0.7)]},
        }
    )

    snippets, notes = run_web_search(["dead", "live"], client=client)

    assert len(snippets) == 1
    assert any("'dead' failed" in note for note in notes)


def test_client_deduplicates_and_trims_queries() -> None:
    client = _FakeTavily({"q": {"results": []}})

    run_web_search(["q", " q ", "", "   "], client=client)

    assert client.queries == ["q"]


def test_client_runs_a_basic_search_with_a_timeout() -> None:
    client = _FakeTavily({"q": {"results": []}})

    run_web_search(["q"], client=client)

    assert client.kwargs[0]["search_depth"] == "basic"
    assert client.kwargs[0]["max_results"] == 3
    assert client.kwargs[0]["timeout"] > 0


def test_no_queries_means_no_client_is_ever_built() -> None:
    # Reached before ``build_client``, so this must hold with no credential in
    # the environment at all.
    assert run_web_search([]) == ([], [])
    assert run_web_search(["", "  "]) == ([], [])


def test_a_snippet_carries_its_query_title_url_and_excerpt() -> None:
    snippet = format_result("why did it break", _tavily_result(0.7))

    assert "[query: why did it break]" in snippet
    assert "Some documentation page" in snippet
    assert "https://example.invalid/a" in snippet
    assert "could not take the lock" in snippet


def test_a_long_page_is_truncated() -> None:
    snippet = format_result("q", {"title": "t", "url": "u", "content": "x" * 5000})

    assert len(snippet) < SNIPPET_MAX_LENGTH + 200
    assert snippet.endswith("...")


def test_building_a_client_without_a_credential_is_an_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Raised rather than returning a client that would fail on every query with
    # a less obvious message. The node is what turns this into a warning.
    monkeypatch.delenv("TAVILY_API_KEY", raising=False)

    with pytest.raises(RuntimeError, match="TAVILY_API_KEY is not set"):
        build_client()
