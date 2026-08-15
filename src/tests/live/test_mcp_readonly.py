"""MCP tool actions are read/query-only — NO inline LLM generation.

Static guard: mcp.py must not reference the synthesis/LLM-generation functions
(chat, _ask synthesis, impact_narrative LLM call, semantic_trace LLM call).

Runtime smoke: overview(communities) + graph(impact_narrative) + graph(path) return
structured data assembled from pre-built DB artifacts, NOT prose from LLM.

The static guard still names `semantic_trace` deliberately: the function is long deleted, so
the assertion matches nothing, which is the point — it is the reintroduction guard, not a
description of a live call site.
"""
import inspect
import json
import re
from pathlib import Path

import pytest

from tests.live._run import run_tool

pytestmark = pytest.mark.live


def test_mcp_handlers_have_no_llm_generation():
    """server/mcp.py tool handlers must not call LLM generation."""
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
        "use gh.impact() + structured JSON instead"
    )
    assert "gh.semantic_trace(" not in text, (
        "server/mcp.py calls gh.semantic_trace() — this calls LLM; "
        "use gh.path_between() + structured JSON instead"
    )
    assert "query.ask" not in text and "run_ask" not in text, (
        "server/mcp.py references query/ask.py — `ask` was retired from the MCP surface; "
        "the architecture axis is overview(what='communities'), and ask.py is now reachable only "
        "from the CLI and the dashboard's chat"
    )
    # Positive: MCP handlers must delegate to the shared LLM-free helpers
    assert "run_graph" in text, (
        "server/mcp.py must call run_graph() — the shared DB-reads-only graph helper"
    )
    # Verify the helper itself is LLM-free (inspect source, not just delegation)
    from rag_search.query.graph_handler import run_graph as _run_graph
    graph_src = inspect.getsource(_run_graph)
    assert not re.search(r"\bchat\s*\(", graph_src), (
        "run_graph() must not call chat() — it is deterministic DB-reads only"
    )


def test_run_ask_is_llm_free():
    """run_ask() assembles context from DB artifacts and never generates.

    Split out of the mcp.py guard above, where it rode along because `ask` was an MCP tool. It is
    not vestigial now that it is off the MCP surface — it matters *more*. run_ask()'s two remaining
    callers are the CLI and the dashboard's chat, and chat is the only surviving LLM consumer in
    the system. If the thing that builds chat's context could itself generate, "grounded in
    retrieval" would stop being checkable at the seam.
    """
    from rag_search.query.ask import run_ask as _run_ask
    src = inspect.getsource(_run_ask)
    assert "compose_answer" in src, (
        "run_ask() must call compose_answer() — the LLM-free context assembler"
    )
    assert not re.search(r"\bchat\s*\(", src), (
        "run_ask() must not call chat() — LLM generation is forbidden on the query path"
    )


def test_overview_communities_carries_the_architecture_axis():
    """overview(what='communities') is what `ask`'s architecture scope used to be.

    This replaces test_ask_mcp_returns_structured_context, which asserted only that the retired
    `ask` tool returned a string longer than 20 characters — true of every error message it could
    produce, so it never discriminated anything. The successor surface deserves a real one.

    Two assertions, each aimed at a specific way the move could have been botched:

    - **The payload actually carries the axis.** `communities` previously returned `{id,title,level}`
      only. Without `summary` there is nothing to read and nothing to rank, and the architecture
      question would have been silently downgraded rather than moved.
    - **`query` is wired, not decorative.** Comparing against the unranked order would be the
      obvious check and is the flaky one — the reranker is free to agree with `member_count DESC`.
      Two semantically opposed queries agreeing with *each other* across 50 communities is what an
      ignored parameter looks like, and is not something a working cross-encoder does.
    """
    from rag_search.server.mcp import overview as overview_tool
    from tests.live._projects import federation_root

    fed_root = federation_root()
    plain = json.loads(run_tool(overview_tool(fed_root, "communities")))["communities"]
    assert plain, "overview(what='communities') returned no communities for the federation root"
    assert all("summary" in c and "member_count" in c for c in plain), (
        "communities rows must carry summary + member_count — the architecture axis `ask` reached"
    )
    counts = [c["member_count"] for c in plain]
    assert counts == sorted(counts, reverse=True), (
        f"communities must be ordered by member_count DESC so the cap keeps the largest; got {counts[:8]}"
    )

    _ids = lambda q: [c["id"] for c in json.loads(  # noqa: E731
        run_tool(overview_tool(fed_root, "communities", q)))["communities"]]
    a = _ids("database storage and persistence")
    b = _ids("http request routing and handlers")
    assert a != b, "query= did not change the community ordering — the parameter is inert"


def test_impact_narrative_returns_structured_json():
    """graph(impact_narrative) returns JSON with risk/affected_count, no LLM prose."""
    from rag_search.server.mcp import graph as graph_tool
    from tests.live._projects import service_member
    svc_member = service_member()  # sample promo-svc, not a real project
    result = run_tool(graph_tool("Run", svc_member, "impact_narrative"))
    data = json.loads(result)
    assert "risk" in data, f"impact_narrative must return JSON with 'risk' key; got: {result[:200]}"
    assert "affected_count" in data, "impact_narrative must include 'affected_count'"
    assert data["risk"] in ("low", "medium", "high"), f"risk must be low/medium/high; got {data['risk']!r}"


def test_path_returns_structured_json():
    """graph(path) returns JSON with path data, no LLM prose.

    This test was written against `semantic_trace` and passed by calling it with a `to_symbol`,
    which the old fallthrough handed straight to the `path` branch — so it asserted the `path`
    contract under another name and would have gone on passing after `semantic_trace` was gutted.
    It now names the relation it actually exercises.
    """
    from rag_search.server.mcp import graph as graph_tool
    from tests.live._projects import service_member

    svc_member = service_member()  # sample promo-svc, not a real project
    result = run_tool(graph_tool("NewService", svc_member, "path", "Run"))
    data = json.loads(result)
    assert "from" in data and "to" in data, (
        f"path must return JSON with 'from' and 'to' keys; got: {result[:200]}"
    )
    assert "path" in data, "path must include 'path' list"
    assert "summary" in data, "path must include 'summary' string"


# A2 and A3 were the no-regex half of HR15 applied to two tier-3 modules, and both had to leave
# rather than be re-pointed. A2 read `server/_overview.py`'s `_detect_services`, one of the five
# overview variants R0a deleted; its "no re.compile in _overview.py" half is now carried tree-wide
# by test_no_code_semantic_regex.py, which screens every module outside four exempt files against
# the wide `re` surface. A3 asserted the *opposite* of what R0 established — that `kb/patterns.py`
# must contain `_llm_frameworks` or `deepseek_chat` — so keeping it would have made this file a
# guard against the deletion. That call was the only DeepSeek round trip left on a query path.
