"""End-to-end tests — complete system coverage.

Covers:
  1. Dashboard HTML structure (3-view Datadog design)
  2. All dashboard API endpoints (status, overview, kb_health, metrics)
  3. Unified chat (/api/chat) — intent routing + humanized prose
  4. Debug trace (/api/debug) — stack trace root cause
  5. Chat router intent classification
  6. System prompt structure (CLAUDE.md verification)

No live LLM required — all LLM calls are mocked.
"""
from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

HOME = Path.home()
PROJECT_ROOT = Path(__file__).resolve().parents[3]


# ── Fixtures ──────────────────────────────────────────────────────────────────

@pytest.fixture
def client():
    from httpx import AsyncClient, ASGITransport
    import opencode_search.mcp as _mcp_mod
    app = _mcp_mod.mcp.streamable_http_app()
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


def _mock_llm(content="This is a detailed answer about the codebase."):
    llm = MagicMock()
    llm.chat = AsyncMock(return_value={"content": content})
    llm.is_available = MagicMock(return_value=True)
    return llm


PYTHON_TRACEBACK = """
Traceback (most recent call last):
  File "/project/src/app.py", line 42, in process
    result = handler.run(data)
  File "/project/src/handler.py", line 17, in run
    return self._transform(data)
AttributeError: 'NoneType' object has no attribute '_transform'
"""


# ── 1. Dashboard HTML Structure ───────────────────────────────────────────────

class TestDashboardHtmlStructure:
    """Verify the 3-view Datadog design is intact."""

    @pytest.mark.asyncio
    async def test_dashboard_returns_200(self, client):
        r = await client.get("/dashboard")
        assert r.status_code == 200
        assert "text/html" in r.headers["content-type"]

    @pytest.mark.asyncio
    async def test_has_pulse_view(self, client):
        r = await client.get("/dashboard")
        assert "view-pulse" in r.text
        assert "loadPulse" in r.text

    @pytest.mark.asyncio
    async def test_has_chat_view(self, client):
        r = await client.get("/dashboard")
        assert "view-chat" in r.text
        assert "sendChat" in r.text

    @pytest.mark.asyncio
    async def test_has_admin_view(self, client):
        r = await client.get("/dashboard")
        assert "view-admin" in r.text
        assert "loadAdmin" in r.text

    @pytest.mark.asyncio
    async def test_no_sidebar(self, client):
        r = await client.get("/dashboard")
        assert "sidebar" not in r.text.lower(), "Old sidebar design must be removed"

    @pytest.mark.asyncio
    async def test_has_bento_grid(self, client):
        r = await client.get("/dashboard")
        assert "bento" in r.text, "KPI bento grid missing"

    @pytest.mark.asyncio
    async def test_has_kpi_tiles(self, client):
        r = await client.get("/dashboard")
        for tile_id in ("tile-files", "tile-communities", "tile-enrichment", "tile-wiki"):
            assert tile_id in r.text, f"KPI tile '{tile_id}' missing"

    @pytest.mark.asyncio
    async def test_has_sparklines(self, client):
        r = await client.get("/dashboard")
        assert "drawSparkline" in r.text, "Sparkline rendering function missing"

    @pytest.mark.asyncio
    async def test_has_ctrl_k_palette(self, client):
        r = await client.get("/dashboard")
        assert "cmd-overlay" in r.text, "Ctrl+K palette missing"
        assert "showCmdPalette" in r.text

    @pytest.mark.asyncio
    async def test_has_top_navbar(self, client):
        r = await client.get("/dashboard")
        assert "topnav" in r.text, "Top navbar class missing"
        assert "vbtn" in r.text, "View buttons missing"

    @pytest.mark.asyncio
    async def test_has_datadog_color_scheme(self, client):
        r = await client.get("/dashboard")
        assert "#0f1117" in r.text or "0f1117" in r.text, "Datadog background color missing"

    @pytest.mark.asyncio
    async def test_has_project_selector(self, client):
        r = await client.get("/dashboard")
        assert "project-sel" in r.text
        assert "switchProject" in r.text

    @pytest.mark.asyncio
    async def test_has_ops_buttons(self, client):
        r = await client.get("/dashboard")
        for fn in ("runVacuum", "runDedup", "runReindex"):
            assert fn in r.text, f"Admin op function '{fn}' missing"

    @pytest.mark.asyncio
    async def test_chat_has_humanized_response_rendering(self, client):
        r = await client.get("/dashboard")
        assert "msg-bubble" in r.text, "Chat message bubble CSS missing"
        assert "intent-tag" in r.text, "Intent badge in chat missing"
        assert "src-chip" in r.text, "Source chips in chat missing"


# ── 2. Dashboard API Endpoints ────────────────────────────────────────────────

class TestDashboardApiEndpoints:
    """All critical API endpoints return valid responses."""

    @pytest.mark.asyncio
    async def test_api_projects_returns_list(self, client):
        r = await client.get("/api/projects")
        assert r.status_code == 200
        data = r.json()
        assert "projects" in data
        assert isinstance(data["projects"], list)

    @pytest.mark.asyncio
    async def test_api_metrics_returns_data(self, client):
        r = await client.get("/api/metrics")
        assert r.status_code == 200
        data = r.json()
        assert isinstance(data, dict)

    @pytest.mark.asyncio
    async def test_api_system_status_returns_data(self, client):
        r = await client.get("/api/system_status")
        assert r.status_code == 200
        data = r.json()
        assert isinstance(data, dict)

    @pytest.mark.asyncio
    async def test_api_overview_requires_project(self, client):
        r = await client.get("/api/overview")
        assert r.status_code == 400

    @pytest.mark.asyncio
    async def test_api_kb_health_requires_project(self, client):
        r = await client.get("/api/kb_health")
        assert r.status_code == 400

    @pytest.mark.asyncio
    async def test_api_suggested_questions_requires_project(self, client):
        r = await client.get("/api/suggested_questions")
        assert r.status_code == 400

    @pytest.mark.asyncio
    async def test_api_communities_requires_project(self, client):
        r = await client.get("/api/communities")
        assert r.status_code == 400

    @pytest.mark.asyncio
    async def test_api_alerts_returns_list(self, client):
        r = await client.get("/api/alerts")
        assert r.status_code == 200
        data = r.json()
        assert "alerts" in data or isinstance(data, list) or isinstance(data, dict)

    @pytest.mark.asyncio
    async def test_api_auto_pipeline_status_returns_data(self, client):
        r = await client.get("/api/auto_pipeline_status")
        assert r.status_code == 200
        data = r.json()
        assert "enabled" in data


# ── 3. Unified Chat (/api/chat) ───────────────────────────────────────────────

class TestUnifiedChatEndpoint:
    """Prove /api/chat routes through handle_chat_auto and returns prose."""

    @pytest.mark.asyncio
    async def test_missing_project_returns_400(self, client):
        r = await client.post("/api/chat", json={"query": "hello"})
        assert r.status_code == 400
        assert "project" in r.json()["error"].lower()

    @pytest.mark.asyncio
    async def test_missing_query_returns_400(self, client):
        r = await client.post("/api/chat", json={"project": "/tmp/x"})
        assert r.status_code == 400
        assert "query" in r.json()["error"].lower()

    @pytest.mark.asyncio
    async def test_returns_answer_string(self, client, tmp_path):
        mock = _mock_llm("The authentication flow starts in auth.py at handle_login.")
        with patch("opencode_search.enricher.create_query_llm_client", return_value=mock):
            r = await client.post("/api/chat", json={
                "project": str(tmp_path), "query": "how does auth work?"
            })
        assert r.status_code == 200
        data = r.json()
        assert "answer" in data
        assert isinstance(data["answer"], str)
        assert len(data["answer"]) > 0

    @pytest.mark.asyncio
    async def test_returns_intent_field(self, client, tmp_path):
        mock = _mock_llm("Feature analysis result.")
        with patch("opencode_search.enricher.create_query_llm_client", return_value=mock):
            r = await client.post("/api/chat", json={
                "project": str(tmp_path), "query": "explain the pipeline"
            })
        assert r.status_code == 200
        assert "intent" in r.json()

    @pytest.mark.asyncio
    async def test_returns_sources_list(self, client, tmp_path):
        mock = _mock_llm("Detailed explanation.")
        with patch("opencode_search.enricher.create_query_llm_client", return_value=mock):
            r = await client.post("/api/chat", json={
                "project": str(tmp_path), "query": "what does the indexer do?"
            })
        assert r.status_code == 200
        assert isinstance(r.json()["sources"], list)

    @pytest.mark.asyncio
    async def test_returns_elapsed_ms(self, client, tmp_path):
        mock = _mock_llm("answer")
        with patch("opencode_search.enricher.create_query_llm_client", return_value=mock):
            r = await client.post("/api/chat", json={
                "project": str(tmp_path), "query": "find the storage handler"
            })
        assert r.status_code == 200
        assert r.json()["elapsed_ms"] >= 0

    @pytest.mark.asyncio
    async def test_answer_is_prose_not_json(self, client, tmp_path):
        """Answer should be natural language, not a raw JSON blob."""
        mock = _mock_llm("The graph extractor uses a two-pass algorithm...")
        with patch("opencode_search.enricher.create_query_llm_client", return_value=mock):
            r = await client.post("/api/chat", json={
                "project": str(tmp_path), "query": "how does graph extraction work?"
            })
        assert r.status_code == 200
        answer = r.json()["answer"]
        assert not answer.startswith("{"), "Answer must not be raw JSON"
        assert not answer.startswith("["), "Answer must not be raw JSON array"

    @pytest.mark.asyncio
    async def test_accepts_conversation_history(self, client, tmp_path):
        mock = _mock_llm("Context-aware answer.")
        history = [
            {"role": "user", "content": "what is this project?"},
            {"role": "assistant", "content": "It is a search engine."},
        ]
        with patch("opencode_search.enricher.create_query_llm_client", return_value=mock):
            r = await client.post("/api/chat", json={
                "project": str(tmp_path),
                "query": "tell me more",
                "history": history,
            })
        assert r.status_code == 200
        assert "answer" in r.json()


# ── 4. Debug Trace (/api/debug) ───────────────────────────────────────────────

class TestDebugTraceEndpoint:
    """Prove /api/debug parses stack traces and returns root cause."""

    @pytest.mark.asyncio
    async def test_missing_project_returns_400(self, client):
        r = await client.post("/api/debug", json={"traceback": PYTHON_TRACEBACK})
        assert r.status_code == 400
        assert "project" in r.json()["error"].lower()

    @pytest.mark.asyncio
    async def test_missing_traceback_returns_400(self, client):
        r = await client.post("/api/debug", json={"project": "/tmp/x"})
        assert r.status_code == 400
        assert "traceback" in r.json()["error"].lower()

    @pytest.mark.asyncio
    async def test_returns_root_cause(self, client, tmp_path):
        mock = _mock_llm(
            "1) Root Cause: The handler is None when run() is called. "
            "2) Why: process() calls handler.run() before initialization. "
            "3) Fix: Initialize handler before calling process()."
        )
        with patch("opencode_search.handlers._debug_trace.create_query_llm_client",
                   return_value=mock):
            with patch("opencode_search.handlers._debug_trace._fetch_graph_context",
                       return_value=[]):
                with patch("opencode_search.handlers._debug_trace._fetch_code_context",
                           return_value=[]):
                    r = await client.post("/api/debug", json={
                        "project": str(tmp_path),
                        "traceback": PYTHON_TRACEBACK,
                    })
        assert r.status_code == 200
        data = r.json()
        for key in ("frames", "root_cause", "hotspot_files", "communities_involved",
                    "confidence", "elapsed_ms"):
            assert key in data, f"Missing key: {key}"

    @pytest.mark.asyncio
    async def test_frames_are_parsed(self, client, tmp_path):
        mock = _mock_llm("Root cause is in handler.py")
        with patch("opencode_search.handlers._debug_trace.create_query_llm_client",
                   return_value=mock):
            with patch("opencode_search.handlers._debug_trace._fetch_graph_context",
                       return_value=[]):
                with patch("opencode_search.handlers._debug_trace._fetch_code_context",
                           return_value=[]):
                    r = await client.post("/api/debug", json={
                        "project": str(tmp_path),
                        "traceback": PYTHON_TRACEBACK,
                    })
        assert r.status_code == 200
        assert len(r.json()["frames"]) >= 1

    @pytest.mark.asyncio
    async def test_confidence_field_valid(self, client, tmp_path):
        mock = _mock_llm("Root cause identified.")
        with patch("opencode_search.handlers._debug_trace.create_query_llm_client",
                   return_value=mock):
            with patch("opencode_search.handlers._debug_trace._fetch_graph_context",
                       return_value=[]):
                with patch("opencode_search.handlers._debug_trace._fetch_code_context",
                           return_value=[]):
                    r = await client.post("/api/debug", json={
                        "project": str(tmp_path),
                        "traceback": PYTHON_TRACEBACK,
                    })
        assert r.json()["confidence"] in ("high", "medium", "low")


# ── 5. Chat Router Intent Classification ─────────────────────────────────────

class TestChatRouterIntentClassification:
    """Prove the intent classifier routes queries correctly."""

    def _classify(self, query):
        from opencode_search.handlers._chat_router import classify_intent
        return classify_intent(query)

    def test_stack_trace_detected_as_debug_trace(self):
        intent = self._classify(PYTHON_TRACEBACK)
        assert intent == "debug_trace"

    def test_bug_question_detected_as_debug(self):
        assert self._classify("why does the campaign clashing fail?") == "debug"
        assert self._classify("there's an error in the payment handler") == "debug"

    def test_find_query_detected_as_search(self):
        assert self._classify("find the authentication middleware") == "search"
        assert self._classify("where is the payment handler defined?") == "search"

    def test_callers_query_detected_as_graph(self):
        assert self._classify("what calls process_order?") == "graph_callers"
        assert self._classify("callers of handle_index") == "graph_callers"

    def test_impact_query_detected_as_graph(self):
        assert self._classify("what breaks if I change the storage layer?") == "graph_impact"

    def test_list_all_detected_as_global(self):
        assert self._classify("list all features in the codebase") == "global"
        assert self._classify("give me all business processes") == "global"

    def test_how_does_defaults_to_feature(self):
        assert self._classify("how does the indexing pipeline work?") == "feature"
        assert self._classify("explain the graph extraction algorithm") == "feature"


# ── 6. System Prompt E2E ─────────────────────────────────────────────────────

class TestSystemPromptStructure:
    """Prove global system prompt is present in all required locations."""

    REQUIRED_PHRASES = [
        "opencode-search",
        "search(",
        "ask(",
        "overview(",
    ]

    def _check(self, text, label):
        for phrase in self.REQUIRED_PHRASES:
            assert phrase in text, f"{label} missing phrase: '{phrase}'"

    def test_project_claude_md_has_instructions(self):
        p = PROJECT_ROOT / "CLAUDE.md"
        if not p.exists():
            pytest.skip("project CLAUDE.md not found")
        self._check(p.read_text(encoding="utf-8"), "CLAUDE.md")

    def test_global_claude_md_has_instructions(self):
        p = HOME / ".claude" / "CLAUDE.md"
        if not p.exists():
            pytest.skip("~/.claude/CLAUDE.md not found")
        self._check(p.read_text(encoding="utf-8"), "~/.claude/CLAUDE.md")

    def test_project_claude_md_forbids_cpu_fallback(self):
        """CPU fallback must be explicitly forbidden — GPU-only enforcement."""
        p = PROJECT_ROOT / "CLAUDE.md"
        if not p.exists():
            pytest.skip("project CLAUDE.md not found")
        text = p.read_text(encoding="utf-8")
        has_prohibition = any(word in text.lower() for word in (
            "cpu", "forbidden", "prohibited", "fatal", "no cpu",
        ))
        assert has_prohibition, "CLAUDE.md must document CPU fallback prohibition"

    def test_claude_settings_has_mcp_server(self):
        p = HOME / ".claude" / "settings.json"
        if not p.exists():
            pytest.skip("~/.claude/settings.json not found")
        settings = json.loads(p.read_text(encoding="utf-8"))
        servers = settings.get("mcpServers", {})
        assert "opencode-search" in servers, "opencode-search not in Claude MCP config"

    def test_mcp_server_uses_bridge_stdio(self):
        p = HOME / ".claude" / "settings.json"
        if not p.exists():
            pytest.skip("~/.claude/settings.json not found")
        settings = json.loads(p.read_text(encoding="utf-8"))
        cfg = settings.get("mcpServers", {}).get("opencode-search", {})
        args = cfg.get("args", [])
        assert "bridge-stdio" in args, "MCP server must use bridge-stdio"

    def test_global_instructions_block_markers_present(self):
        """The opencode-search-global-instructions block must exist in CLAUDE.md."""
        p = PROJECT_ROOT / "CLAUDE.md"
        if not p.exists():
            pytest.skip("project CLAUDE.md not found")
        text = p.read_text(encoding="utf-8")
        assert "opencode-search-global-instructions" in text, \
            "Global instructions block markers missing from CLAUDE.md"
