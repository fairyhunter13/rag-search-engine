"""P14.4: MCP tool actions are read/query-only — NO inline LLM generation.

Static guard: mcp.py must not reference the synthesis/LLM-generation functions
(chat, _ask synthesis, impact_narrative LLM call, semantic_trace LLM call).

Runtime smoke: ask + graph(impact_narrative) + graph(semantic_trace) return
structured data assembled from pre-built DB artifacts, NOT prose from LLM.
"""
import asyncio
import inspect
import json
import re
from pathlib import Path

import pytest

pytestmark = pytest.mark.live


def test_mcp_handlers_have_no_llm_generation():
    """P14.4 static: server/mcp.py tool handlers must not call LLM generation."""
    mcp_path = Path(__file__).parents[2] / "rag_search" / "server" / "mcp.py"
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
    assert "from rag_search.query.ask import ask as _ask" not in text, (
        "server/mcp.py imports ask as _ask — MCP handler must use run_ask() instead"
    )
    # Positive: MCP handlers must delegate to the shared LLM-free helpers
    assert "run_ask" in text, (
        "server/mcp.py must call run_ask() — the shared LLM-free context helper; "
        "do not inline LLM generation in the MCP handler"
    )
    assert "run_graph" in text, (
        "server/mcp.py must call run_graph() — the shared DB-reads-only graph helper"
    )
    # Verify the helpers themselves are LLM-free (inspect source, not just delegation)
    from rag_search.query.ask import run_ask as _run_ask
    from rag_search.query.graph_handler import run_graph as _run_graph
    ask_src = inspect.getsource(_run_ask)
    assert "compose_answer" in ask_src, (
        "run_ask() must call compose_answer() — the LLM-free context assembler"
    )
    assert not re.search(r"\bchat\s*\(", ask_src), (
        "run_ask() must not call chat() — LLM generation is forbidden on the query path"
    )
    graph_src = inspect.getsource(_run_graph)
    assert not re.search(r"\bchat\s*\(", graph_src), (
        "run_graph() must not call chat() — it is deterministic DB-reads only"
    )


def test_ask_mcp_returns_structured_context():
    """P14.4 runtime: MCP ask returns pre-built artifacts (non-empty, fast, no generative LLM on query path)."""
    from rag_search.server.mcp import ask as ask_tool
    from tests.live._projects import federation_root

    fed_root = federation_root()
    result = asyncio.run(ask_tool("How does authentication work?", fed_root, "all"))
    assert isinstance(result, str) and len(result) > 20, (
        f"ask() returned empty/tiny response: {result!r}"
    )


def test_impact_narrative_returns_structured_json():
    """P14.4 runtime: graph(impact_narrative) returns JSON with risk/affected_count, no LLM prose."""
    from rag_search.server.mcp import graph as graph_tool
    from tests.live._projects import service_member
    svc_member = service_member()  # sample promo-svc, not a real project
    result = asyncio.run(graph_tool("Run", svc_member, "impact_narrative"))
    data = json.loads(result)
    assert "risk" in data, f"impact_narrative must return JSON with 'risk' key; got: {result[:200]}"
    assert "affected_count" in data, "impact_narrative must include 'affected_count'"
    assert data["risk"] in ("low", "medium", "high"), f"risk must be low/medium/high; got {data['risk']!r}"


def test_semantic_trace_returns_structured_json():
    """P14.4 runtime: graph(semantic_trace) returns JSON with path data, no LLM prose."""
    from rag_search.server.mcp import graph as graph_tool
    from tests.live._projects import service_member

    svc_member = service_member()  # sample promo-svc, not a real project
    result = asyncio.run(graph_tool("NewService", svc_member, "semantic_trace", "Run"))
    data = json.loads(result)
    assert "from" in data and "to" in data, (
        f"semantic_trace must return JSON with 'from' and 'to' keys; got: {result[:200]}"
    )
    assert "path" in data, "semantic_trace must include 'path' list"
    assert "summary" in data, "semantic_trace must include 'summary' string"


# A2 and A3 were the no-regex half of HR15 applied to two tier-3 modules, and both had to leave
# rather than be re-pointed. A2 read `server/_overview.py`'s `_detect_services`, one of the five
# overview variants R0a deleted; its "no re.compile in _overview.py" half is now carried tree-wide
# by test_no_code_semantic_regex.py, which screens every module outside four exempt files against
# the wide `re` surface. A3 asserted the *opposite* of what R0 established — that `kb/patterns.py`
# must contain `_llm_frameworks` or `deepseek_chat` — so keeping it would have made this file a
# guard against the deletion. That call was the only DeepSeek round trip left on a query path.
