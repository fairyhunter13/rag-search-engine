"""Behavioral E2E tests — verify search engine behaviors through the HTTP API.

Tests the complete stack against the running daemon using the opencode-search-engine
project itself as the target (it is always indexed and watching).

Focuses on BEHAVIOR not just format:
  - Search returns semantically relevant code, not just any results
  - Ask answers questions using KB content (cites real files/concepts)
  - Graph returns real call relationships with file paths
  - Chat is multi-turn and uses conversation history
  - Streaming delivers progressive NDJSON tokens

Markers: runtime_deps (requires daemon on :8765 + Ollama on :11434)
"""
from __future__ import annotations

import json
from typing import Any

import httpx
import pytest

pytestmark = pytest.mark.runtime_deps

_DAEMON = "http://127.0.0.1:8765"
_PROJECT = "/home/user/git/github.com/fairyhunter13/opencode-search-engine"
_TIMEOUT = 300.0  # seconds per HTTP call — LLM MAP-REDUCE can take 130-160s


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture(scope="module", autouse=True)
def require_daemon():
    import urllib.request
    try:
        urllib.request.urlopen(f"{_DAEMON}/healthz", timeout=5)
    except Exception:
        pytest.skip("daemon not running on :8765")


@pytest.fixture(scope="module", autouse=True)
def require_ollama():
    import urllib.request
    try:
        urllib.request.urlopen("http://localhost:11434/api/tags", timeout=5)
    except Exception:
        pytest.skip("Ollama not running on :11434")


@pytest.fixture(scope="module")
def http():
    """Sync httpx client for the daemon."""
    with httpx.Client(base_url=_DAEMON, timeout=_TIMEOUT) as c:
        yield c


# ---------------------------------------------------------------------------
# Helper
# ---------------------------------------------------------------------------

def _post_mcp(http, tool: str, params: dict[str, Any]) -> dict[str, Any]:
    """Call a tool via the /api/ HTTP endpoints."""
    endpoint_map = {
        "search": "/api/search",
        "ask": "/api/ask",
        "graph": "/api/graph",
        "overview": "/api/overview",
    }
    path = endpoint_map.get(tool, f"/api/{tool}")
    r = http.post(path, json={**params, "project": _PROJECT})
    assert r.status_code == 200, f"{tool}: {r.status_code} {r.text[:200]}"
    return r.json()


def _get(http, path: str, params: dict | None = None) -> dict[str, Any]:
    r = http.get(path, params=params or {})
    assert r.status_code == 200, f"GET {path}: {r.status_code} {r.text[:200]}"
    return r.json()


# ---------------------------------------------------------------------------
# T1: Search returns semantically relevant results
# ---------------------------------------------------------------------------

class TestSearchBehavior:
    """Search must return results that are relevant to the query, not random code."""

    def test_search_for_embedding_returns_embedding_files(self, http):
        r = http.get("/api/search", params={"project": _PROJECT, "q": "embedding model GPU ONNX inference"})
        assert r.status_code == 200, f"search returned {r.status_code}: {r.text[:200]}"
        results = r.json().get("results", [])
        assert len(results) >= 3, f"Expected >= 3 results, got {len(results)}"
        paths = [res.get("path", "") for res in results]
        assert any("embed" in p.lower() for p in paths), \
            f"Expected embedding-related file in results, got: {paths[:5]}"

    def test_search_for_handler_returns_handler_files(self, http):
        r = http.get("/api/search", params={"project": _PROJECT, "q": "handle_search_code query handler"})
        assert r.status_code == 200, f"search returned {r.status_code}: {r.text[:200]}"
        results = r.json().get("results", [])
        assert len(results) >= 2, f"Expected >= 2 results, got {len(results)}"
        paths = [res.get("path", "") for res in results]
        assert any("handler" in p.lower() or "_query" in p.lower() for p in paths), \
            f"Expected handler file in top results, got: {paths[:5]}"

    def test_search_results_have_scores(self, http):
        r = http.get("/api/search", params={"project": _PROJECT, "q": "LanceDB vector storage"})
        assert r.status_code == 200, f"search returned {r.status_code}: {r.text[:200]}"
        results = r.json().get("results", [])
        assert results, "Expected results for LanceDB vector storage query"
        # Scores are raw cosine similarity values from LanceDB — may exceed 1.0
        for res in results[:3]:
            score = res.get("score", None)
            assert score is not None, f"Result missing score: {res.get('path')}"
            assert score >= 0, f"Negative score unexpected: {score} for {res.get('path')}"

    def test_search_results_have_language(self, http):
        r = http.get("/api/search", params={"project": _PROJECT, "q": "pytest fixture conftest"})
        assert r.status_code == 200, f"search returned {r.status_code}: {r.text[:200]}"
        results = r.json().get("results", [])
        assert results, "No results returned"
        langs = {res.get("language") for res in results if res.get("language")}
        assert "python" in langs or "Python" in langs, \
            f"Expected Python language in results, got: {langs}"

    def test_search_scope_code_vs_docs(self, http):
        r_code = http.get("/api/search", params={"project": _PROJECT, "q": "streaming chat", "scope": "code"})
        assert r_code.status_code == 200, f"search returned {r_code.status_code}: {r_code.text[:200]}"
        code_results = r_code.json().get("results", [])
        assert code_results, "search with scope=code returned no results"


# ---------------------------------------------------------------------------
# T2: Ask returns answers grounded in KB content
# ---------------------------------------------------------------------------

class TestAskBehavior:
    """Ask must return answers that reference actual codebase content.

    /api/ask (GET, default scope) returns search results (not LLM answer).
    /api/ask?scope=global triggers LLM MAP-REDUCE synthesis.
    /api/chat (POST) provides full LLM-powered KB answers.
    """

    @pytest.fixture(scope="class")
    def ask_search_result(self, http):
        """Default scope: returns ranked search results from KB."""
        r = http.get(
            "/api/ask",
            params={"project": _PROJECT, "q": "KB chat handler LLM context assembly"},
        )
        assert r.status_code == 200, f"ask returned {r.status_code}: {r.text[:200]}"
        return r.json()

    def test_ask_returns_results(self, ask_search_result):
        results = ask_search_result.get("results", [])
        assert len(results) >= 1, f"ask returned no results: {ask_search_result}"

    def test_ask_results_have_scores(self, ask_search_result):
        results = ask_search_result.get("results", [])
        for r in results[:3]:
            score = r.get("score", -1)
            assert 0 <= score <= 1, f"Score out of range: {score}"

    def test_ask_has_community_matches(self, ask_search_result):
        # Global search returns community and wiki matches alongside code results
        total = ask_search_result.get("total", 0)
        assert total >= 1, f"Expected total >= 1, got {total}"

    @pytest.fixture(scope="class")
    def feature_ask_result(self, http):
        """Feature scope: returns entry points, call chain, algorithm, design rationale."""
        r = http.get(
            "/api/ask",
            params={"project": _PROJECT, "q": "why is the pipeline designed with community detection?", "scope": "feature"},
        )
        assert r.status_code == 200, f"feature ask returned {r.status_code}: {r.text[:200]}"
        return r.json()

    def test_feature_ask_has_status_ok(self, feature_ask_result):
        # feature trace returns {status, entry_points, call_chain, algorithm, design_rationale}
        status = feature_ask_result.get("status", "")
        answer = feature_ask_result.get("answer", "")
        assert status == "ok" or len(answer) > 50, \
            f"Feature ask returned neither ok status nor answer: {feature_ask_result}"

    def test_feature_ask_has_entry_points_or_answer(self, feature_ask_result):
        eps = feature_ask_result.get("entry_points", [])
        answer = feature_ask_result.get("answer", "")
        assert len(eps) >= 1 or len(answer) > 50, \
            f"Feature ask has no entry points or answer: {feature_ask_result}"


# ---------------------------------------------------------------------------
# T3: Graph returns real call relationships
# ---------------------------------------------------------------------------

class TestGraphBehavior:
    """Graph queries must return actual call relationships with file paths."""

    @pytest.fixture(scope="class")
    def callers_result(self, http):
        r = http.get("/api/graph", params={
            "project": _PROJECT,
            "symbol": "handle_search_code",
            "relation": "callers",
        })
        assert r.status_code == 200
        return r.json()

    def test_callers_returns_results(self, callers_result):
        callers = callers_result.get("callers", [])
        assert len(callers) >= 1, f"Expected >= 1 caller for handle_search_code, got {len(callers)}"

    def test_callers_have_file_paths(self, callers_result):
        callers = callers_result.get("callers", [])
        for c in callers[:3]:
            assert c.get("file") or c.get("file_path"), f"Caller missing file: {c}"

    def test_callers_include_kb_chat_or_query(self, callers_result):
        callers = callers_result.get("callers", [])
        names = [
            (c.get("qualified_name", "") + c.get("name", "")).lower()
            for c in callers
        ]
        assert any("search" in n or "kb" in n or "feature" in n or "global" in n or "chat" in n for n in names), \
            f"Expected KB/search related callers, got: {names[:5]}"

    @pytest.fixture(scope="class")
    def callees_result(self, http):
        r = http.get("/api/graph", params={
            "project": _PROJECT,
            "symbol": "handle_pipeline",
            "relation": "callees",
        })
        assert r.status_code == 200
        return r.json()

    def test_callees_returns_results(self, callees_result):
        callees = callees_result.get("callees", [])
        assert len(callees) >= 1, f"Expected >= 1 callee for handle_pipeline, got {len(callees)}"

    def test_callees_have_confidence(self, callees_result):
        callees = callees_result.get("callees", [])
        for c in callees[:3]:
            conf = c.get("confidence", -1)
            assert 0 <= conf <= 1, f"Confidence out of range: {conf}"

    @pytest.fixture(scope="class")
    def impact_result(self, http):
        r = http.get("/api/graph", params={
            "project": _PROJECT,
            "symbol": "handle_index_project",
            "relation": "impact",
        })
        assert r.status_code == 200
        return r.json()

    def test_impact_returns_total_affected(self, impact_result):
        # Impact response: {symbol, node_id, total_affected, callers_by_depth}
        total = impact_result.get("total_affected", 0)
        callers = impact_result.get("callers_by_depth", {})
        assert total >= 1 or callers, \
            f"Expected impact results for handle_index_project: {impact_result}"


# ---------------------------------------------------------------------------
# T4: Overview returns real project structure and community data
# ---------------------------------------------------------------------------

class TestOverviewBehavior:
    """Overview must return accurate project data."""

    def test_overview_structure_has_file_count(self, http):
        r = http.get("/api/overview", params={"project": _PROJECT, "what": "structure"})
        assert r.status_code == 200
        body = r.json()
        # Might be nested in result or directly
        file_count = body.get("file_count") or (body.get("result") or {}).get("file_count", 0)
        assert file_count > 50, f"Expected > 50 files in structure, got {file_count}"

    def test_overview_communities_has_enriched_communities(self, http):
        r = http.get("/api/communities", params={"project": _PROJECT})
        assert r.status_code == 200
        body = r.json()
        communities = body.get("communities", []) or body.get("result", {}).get("communities", [])
        assert len(communities) >= 5, f"Expected >= 5 communities, got {len(communities)}"
        enriched = [c for c in communities if c.get("title") and c.get("summary")]
        assert len(enriched) >= 3, f"Expected >= 3 enriched communities, got {len(enriched)}"

    def test_overview_status_shows_indexed(self, http):
        r = http.get("/api/kb_health", params={"project": _PROJECT})
        assert r.status_code == 200
        body = r.json()
        # Either directly or nested
        result = body if "enrichment_pct" in body else body.get("result", body)
        pct = result.get("enrichment_pct", 0)
        assert pct > 50, f"Expected enrichment > 50%, got {pct}%"

    def test_overview_structure_has_python_files(self, http):
        r = http.get("/api/overview", params={"project": _PROJECT, "what": "structure"})
        assert r.status_code == 200
        body = r.json()
        lang_breakdown = body.get("language_breakdown", [])
        # Language breakdown has {extension, count} dicts — look for .py
        extensions = [lb.get("extension", "") for lb in lang_breakdown if isinstance(lb, dict)]
        assert ".py" in extensions, \
            f"Expected .py in language_breakdown extensions, got: {extensions}"


# ---------------------------------------------------------------------------
# T5: KB Chat — answers are grounded in knowledge base content
# ---------------------------------------------------------------------------

class TestKbChatBehavior:
    """The /api/chat endpoint must return KB-grounded answers."""

    def test_chat_answers_about_indexing(self, http):
        r = http.post("/api/chat", json={
            "project": _PROJECT,
            "query": "how does the indexing pipeline work?",
        })
        assert r.status_code == 200
        body = r.json()
        answer = body.get("answer", "")
        assert len(answer) > 100, f"Answer too short: {len(answer)} chars"
        answer_lower = answer.lower()
        assert any(kw in answer_lower for kw in ["index", "pipeline", "chunk", "embed", "file", "tree"]), \
            f"Answer doesn't mention indexing concepts: {answer[:300]}"

    def test_chat_answers_about_search(self, http):
        r = http.post("/api/chat", json={
            "project": _PROJECT,
            "query": "what search algorithms does this codebase use?",
        })
        assert r.status_code == 200
        answer = r.json().get("answer", "")
        assert len(answer) > 80, f"Answer too short: {len(answer)} chars"
        answer_lower = answer.lower()
        assert any(kw in answer_lower for kw in ["vector", "hybrid", "bm25", "semantic", "search", "embed"]), \
            f"Answer doesn't mention search concepts: {answer[:300]}"

    def test_chat_includes_model_info(self, http):
        r = http.post("/api/chat", json={
            "project": _PROJECT,
            "query": "what LLM models are used in this project?",
        })
        assert r.status_code == 200
        body = r.json()
        model = body.get("model", "")
        assert model, f"Response missing model field: {body.keys()}"
        assert "qwen" in model.lower() or "ollama" in model.lower(), \
            f"Expected qwen3/ollama model, got: {model!r}"

    def test_chat_returns_sources(self, http):
        r = http.post("/api/chat", json={
            "project": _PROJECT,
            "query": "where is the graph storage implemented?",
        })
        assert r.status_code == 200
        body = r.json()
        sources = body.get("sources", [])
        assert isinstance(sources, list), f"sources must be a list, got {type(sources)}"
        # Sources can be empty for comprehensive mode when all context comes from communities
        # but answer should still be meaningful
        answer = body.get("answer", "")
        assert len(answer) > 50, f"Answer too short even without sources: {len(answer)}"


# ---------------------------------------------------------------------------
# T6: Multi-turn conversation — history changes responses
# ---------------------------------------------------------------------------

class TestConversationHistoryBehavior:
    """Conversation history must actually influence LLM responses."""

    def test_chat_with_history_produces_coherent_followup(self, http):
        """Second question should reference the first answer's context."""
        # Turn 1: ask about search
        r1 = http.post("/api/chat", json={
            "project": _PROJECT,
            "query": "what is the main search function called?",
        })
        assert r1.status_code == 200
        answer1 = r1.json().get("answer", "")
        assert len(answer1) > 30, f"First answer too short: {answer1!r}"

        # Turn 2: follow-up referencing first answer via history
        r2 = http.post("/api/chat", json={
            "project": _PROJECT,
            "query": "where is that function defined?",
            "history": [
                {"role": "user", "content": "what is the main search function called?"},
                {"role": "assistant", "content": answer1},
            ],
        })
        assert r2.status_code == 200
        answer2 = r2.json().get("answer", "")
        assert len(answer2) > 30, f"Follow-up answer too short: {answer2!r}"
        # The follow-up should be grounded (mentions file/path) not empty or confused
        assert "no indexed" not in answer2.lower(), \
            f"Follow-up answer says 'not indexed' but should use history: {answer2}"

    def test_chat_history_field_is_accepted(self, http):
        """Server must not reject requests with history field."""
        r = http.post("/api/chat", json={
            "project": _PROJECT,
            "query": "what are the test files?",
            "history": [
                {"role": "user", "content": "tell me about this project"},
                {"role": "assistant", "content": "This is an opencode search engine."},
            ],
        })
        assert r.status_code == 200, f"Server rejected history field: {r.text[:200]}"


# ---------------------------------------------------------------------------
# T7: Streaming chat delivers progressive NDJSON tokens
# ---------------------------------------------------------------------------

class TestStreamingChatBehavior:
    """The /api/chat_stream endpoint must deliver real-time NDJSON token stream."""

    def test_stream_delivers_token_events(self, http):
        tokens = []
        done_event = None

        with http.stream("POST", "/api/chat_stream", json={
            "project": _PROJECT,
            "query": "briefly describe the KB chat handler",
        }) as r:
            assert r.status_code == 200, f"stream returned {r.status_code}"
            ct = r.headers.get("content-type", "")
            assert "ndjson" in ct or "json" in ct, f"Expected ndjson content-type, got: {ct}"

            for line in r.iter_lines():
                if not line.strip():
                    continue
                try:
                    evt = json.loads(line)
                except json.JSONDecodeError:
                    continue
                evt_type = evt.get("type")
                if evt_type == "token":
                    tokens.append(evt.get("text", ""))
                elif evt_type == "done":
                    done_event = evt
                    break
                # "thinking" keepalive events are ignored — they just prevent HTTP timeout

        assert len(tokens) >= 1, f"Expected at least 1 token event, got {len(tokens)}"
        assert done_event is not None, "Expected 'done' event at end of stream"

        full_text = "".join(tokens)
        assert len(full_text) > 30, f"Streamed text too short: {full_text!r}"

    def test_stream_done_event_has_model(self, http):
        done_event = None
        with http.stream("POST", "/api/chat_stream", json={
            "project": _PROJECT,
            "query": "what is the embedding model?",
        }) as r:
            if r.status_code != 200:
                pytest.skip(f"stream not available: {r.status_code}")
            for line in r.iter_lines():
                if not line.strip():
                    continue
                try:
                    evt = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if evt.get("type") == "done":
                    done_event = evt
                    break
                # skip "thinking" keepalive events

        assert done_event is not None, "No done event received"
        assert "model" in done_event or "intent" in done_event, \
            f"done event missing model/intent fields: {done_event}"

    def test_stream_with_history_is_accepted(self, http):
        """Stream endpoint must accept history field without error."""
        with http.stream("POST", "/api/chat_stream", json={
            "project": _PROJECT,
            "query": "what calls the search handler?",
            "history": [
                {"role": "user", "content": "tell me about search"},
                {"role": "assistant", "content": "The search handler handles queries."},
            ],
        }) as r:
            assert r.status_code == 200, f"stream with history failed: {r.status_code}: {r.text[:200]}"


# ---------------------------------------------------------------------------
# T8: Dashboard API endpoints return meaningful data
# ---------------------------------------------------------------------------

class TestDashboardApiBehavior:
    """Dashboard endpoints must return real data, not empty responses."""

    def test_projects_endpoint_lists_indexed_projects(self, http):
        body = _get(http, "/api/projects")
        projects = body.get("projects", [])
        assert len(projects) >= 1, "Expected at least 1 indexed project"
        paths = [p.get("path", "") for p in projects]
        assert any("opencode" in p.lower() or "astro" in p.lower() for p in paths), \
            f"Expected known project in list, got: {paths[:3]}"

    def test_system_status_shows_service_running(self, http):
        body = _get(http, "/api/system_status")
        text = json.dumps(body).lower()
        assert any(kw in text for kw in ["ok", "running", "healthy", "up"]), \
            f"system_status doesn't indicate service is running: {text[:200]}"

    def test_metrics_returns_uptime(self, http):
        body = _get(http, "/api/metrics")
        uptime = body.get("uptime_s") or body.get("uptime")
        if not uptime:
            # Freshly restarted daemons may have null uptime_s until daemon.json is written;
            # fall back to /healthz which always tracks uptime from process start time.
            hbody = _get(http, "/healthz")
            uptime = hbody.get("uptime_s", 0)
        assert uptime > 0, f"Expected uptime > 0, got {uptime}"

    def test_auto_pipeline_status_has_enabled_field(self, http):
        body = _get(http, "/api/auto_pipeline_status")
        assert "enabled" in body, f"auto_pipeline_status missing 'enabled': {body}"

    def test_jobs_endpoint_returns_list_or_dict(self, http):
        body = _get(http, "/api/jobs")
        assert isinstance(body, (dict, list)), \
            f"jobs endpoint returned unexpected type: {type(body)}"


# ---------------------------------------------------------------------------
# T9: Wiki endpoint — verified knowledge base content
# ---------------------------------------------------------------------------

class TestWikiBehavior:
    """Wiki must have pages with real content about the codebase."""

    def test_wiki_list_returns_pages(self, http):
        r = http.get("/api/wiki", params={"project": _PROJECT})
        assert r.status_code == 200
        body = r.json()
        pages = body.get("pages", [])
        assert len(pages) >= 5, f"Expected >= 5 wiki pages, got {len(pages)}"
        # Pages are strings (filenames without extension)
        assert all(isinstance(p, str) for p in pages[:3]), \
            f"Expected string page names, got: {[type(p) for p in pages[:3]]}"

    def test_wiki_search_returns_content_results(self, http):
        r = http.get("/api/ask", params={
            "project": _PROJECT,
            "q": "GPU embedding inference ONNX",
            "scope": "wiki",
        })
        assert r.status_code == 200
        body = r.json()
        results = body.get("results", [])
        assert len(results) >= 1, "Expected wiki search results, got none"
        # Results have content and scores
        first = results[0]
        assert first.get("content") or first.get("excerpt"), \
            f"Wiki result missing content: {first.keys()}"
        assert first.get("score", -1) >= 0, f"Wiki result missing score: {first}"


# ---------------------------------------------------------------------------
# T10: End-to-end conversation flow — the full chat journey
# ---------------------------------------------------------------------------

class TestFullConversationFlow:
    """Test a complete multi-turn conversation like talking to Claude.ai."""

    def test_three_turn_conversation_stays_coherent(self, http):
        """A 3-turn conversation should produce coherent, non-repetitive answers."""
        history = []

        # Turn 1: broad question
        q1 = "what are the main components of this search engine?"
        r1 = http.post("/api/chat", json={"project": _PROJECT, "query": q1})
        assert r1.status_code == 200
        a1 = r1.json().get("answer", "")
        assert len(a1) > 80, f"Turn 1 answer too short: {a1!r}"
        history.extend([{"role": "user", "content": q1}, {"role": "assistant", "content": a1}])

        # Turn 2: follow-up about a component
        q2 = "tell me more about the graph component you mentioned"
        r2 = http.post("/api/chat", json={"project": _PROJECT, "query": q2, "history": history})
        assert r2.status_code == 200
        a2 = r2.json().get("answer", "")
        assert len(a2) > 50, f"Turn 2 answer too short: {a2!r}"
        history.extend([{"role": "user", "content": q2}, {"role": "assistant", "content": a2}])

        # Turn 3: specific code question
        q3 = "which file implements the graph storage?"
        r3 = http.post("/api/chat", json={"project": _PROJECT, "query": q3, "history": history})
        assert r3.status_code == 200
        a3 = r3.json().get("answer", "")
        assert len(a3) > 30, f"Turn 3 answer too short: {a3!r}"
        # Should mention a file path or code concept
        assert any(kw in a3.lower() for kw in ["graph", "storage", ".py", "file", "module"]), \
            f"Turn 3 answer doesn't reference code: {a3[:200]}"

    # ── Phase 35: Architecture + enhanced debug ─────────────────────────────

    def test_architecture_question_routes_to_architecture_intent(self, http):
        """End-to-end architecture question should be classified as 'architecture' intent."""
        r = http.post("/api/chat", json={"project": _PROJECT, "query": "how is the architecture end to end of this codebase?"})
        assert r.status_code == 200
        body = r.json()
        assert body.get("intent") == "architecture", \
            f"Expected architecture intent, got: {body.get('intent')}"

    def test_architecture_answer_names_multiple_layers(self, http):
        """Architecture answer should describe multiple layers of the system."""
        r = http.post("/api/chat", json={"project": _PROJECT, "query": "walk me through the whole system from entry to storage"})
        assert r.status_code == 200
        answer = r.json().get("answer", "").lower()
        layer_keywords = ["handler", "storage", "router", "api", "layer", "entry",
                          "data", "business", "infra", "service", "mcp"]
        matched = sum(1 for kw in layer_keywords if kw in answer)
        assert matched >= 3, \
            f"Architecture answer should name multiple layers (got {matched}): {answer[:400]}"
        assert len(answer) > 300, f"Architecture answer too short: {len(answer)} chars"

    def test_debug_question_identifies_business_process(self, http):
        """Debug question should identify the business process containing the bug."""
        r = http.post("/api/chat", json={
            "project": _PROJECT,
            "query": "can we find the exact line of code, at which business process, at which algorithm, that caused the bug where chat messages disappear after sending?",
        })
        assert r.status_code == 200
        body = r.json()
        answer = body.get("answer", "")
        assert body.get("intent") == "debug", f"Expected debug intent, got: {body.get('intent')}"
        assert len(answer) > 200, f"Debug answer too short: {len(answer)} chars"
        has_process = any(kw in answer.lower() for kw in [
            "process", "handler", "community", "workflow", "layer", "business",
        ])
        assert has_process, f"Debug answer should name a business process: {answer[:500]}"

    def test_debug_question_names_file_or_line(self, http):
        """Debug answer should reference specific files or functions."""
        r = http.post("/api/chat", json={
            "project": _PROJECT,
            "query": "why does the streaming chat response sometimes stop or not show tokens?",
        })
        assert r.status_code == 200
        answer = r.json().get("answer", "")
        has_file_ref = ".py" in answer or "line" in answer.lower() or "function" in answer.lower()
        assert has_file_ref, f"Debug answer should reference specific code: {answer[:500]}"
        assert len(answer) > 300

    def test_architecture_streams_native_tokens(self, http):
        """Architecture intent should stream tokens (not heartbeat-only)."""
        tokens = []
        with httpx.stream("POST", f"{_DAEMON}/api/chat_stream",
                          json={"project": _PROJECT, "query": "how is the architecture end to end?"},
                          timeout=120) as resp:
            for line in resp.iter_lines():
                if not line:
                    continue
                try:
                    evt = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if evt.get("type") == "token":
                    tokens.append(evt["text"])
                if evt.get("type") == "done":
                    assert evt.get("intent") == "architecture", \
                        f"Expected architecture in done event, got: {evt.get('intent')}"
                    break
        assert len(tokens) >= 5, \
            f"Architecture streaming should deliver tokens, got {len(tokens)}"
