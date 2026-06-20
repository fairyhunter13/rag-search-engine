"""P14.4: MCP tool actions are read/query-only — NO inline LLM generation.

Static guard: mcp.py must not reference the synthesis/LLM-generation functions
(chat, _ask synthesis, impact_narrative LLM call, semantic_trace LLM call).

Runtime smoke: ask + graph(impact_narrative) + graph(semantic_trace) return
structured data assembled from pre-built DB artifacts, NOT prose from LLM.
"""
import asyncio
import json
import re
from pathlib import Path

import pytest

pytestmark = pytest.mark.live


def test_mcp_handlers_have_no_llm_generation():
    """P14.4 static: server/mcp.py tool handlers must not call LLM generation."""
    mcp_path = Path(__file__).parents[2] / "opencode_search" / "server" / "mcp.py"
    text = mcp_path.read_text()

    # chat() is the LLM generation function — must not appear as a call
    assert not re.search(r"\bchat\s*\(", text), (
        "server/mcp.py calls chat() — LLM generation is forbidden in MCP handlers; "
        "move synthesis to daemon background sweep"
    )
    # The LLM-backed helpers from graph_handler must not be called in mcp.py
    assert "gh.impact_narrative(" not in text, (
        "server/mcp.py calls gh.impact_narrative() — this calls LLM; "
        "use gh.impact() + structured JSON instead (P14.2)"
    )
    assert "gh.semantic_trace(" not in text, (
        "server/mcp.py calls gh.semantic_trace() — this calls LLM; "
        "use gh.path_between() + structured JSON instead (P14.2)"
    )
    # The full ask() (LLM version) must not be imported into mcp.py for tool use
    assert "from opencode_search.query.ask import ask as _ask" not in text, (
        "server/mcp.py imports ask as _ask — MCP handler must use compose_answer() instead"
    )
    # Positive: MCP ask must call compose_answer (LLM-free context assembly, not synthesis)
    assert "compose_answer" in text, (
        "server/mcp.py must call compose_answer() — the LLM-free context composer; "
        "do not replace with the synthesizing ask() regardless of import alias"
    )


def test_ask_mcp_returns_structured_context():
    """P14.4 runtime: MCP ask returns pre-built artifacts (non-empty, fast, no generative LLM on query path)."""
    from opencode_search.core.registry import list_projects
    from opencode_search.server.mcp import ask as ask_tool

    astro = next(
        (p.path for p in list_projects()
         if "astro-project" in p.path and "promo" not in p.path and p.enabled),
        None,
    )
    assert astro, "astro-project must be registered (run P8)"
    result = asyncio.run(ask_tool("How does authentication work?", astro, "all"))
    assert isinstance(result, str) and len(result) > 20, (
        f"ask() returned empty/tiny response: {result!r}"
    )


def test_impact_narrative_returns_structured_json():
    """P14.4 runtime: graph(impact_narrative) returns JSON with risk/affected_count, no LLM prose."""
    from opencode_search.core.registry import list_projects
    from opencode_search.server.mcp import graph as graph_tool

    be = next((p.path for p in list_projects() if "astro-promo-be" in p.path and p.enabled), None)
    assert be, "astro-promo-be must be registered (run P8)"
    result = asyncio.run(graph_tool("Run", be, "impact_narrative"))
    data = json.loads(result)
    assert "risk" in data, f"impact_narrative must return JSON with 'risk' key; got: {result[:200]}"
    assert "affected_count" in data, "impact_narrative must include 'affected_count'"
    assert data["risk"] in ("low", "medium", "high"), f"risk must be low/medium/high; got {data['risk']!r}"


def test_semantic_trace_returns_structured_json():
    """P14.4 runtime: graph(semantic_trace) returns JSON with path data, no LLM prose."""
    from opencode_search.core.registry import list_projects
    from opencode_search.server.mcp import graph as graph_tool

    be = next((p.path for p in list_projects() if "astro-promo-be" in p.path and p.enabled), None)
    assert be, "astro-promo-be must be registered (run P8)"
    result = asyncio.run(graph_tool("NewService", be, "semantic_trace", "Run"))
    data = json.loads(result)
    assert "from" in data and "to" in data, (
        f"semantic_trace must return JSON with 'from' and 'to' keys; got: {result[:200]}"
    )
    assert "path" in data, "semantic_trace must include 'path' list"
    assert "summary" in data, "semantic_trace must include 'summary' string"
