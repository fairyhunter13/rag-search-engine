"""Property-based tests using Hypothesis — invariants that hold for ANY valid input.

These complement example-based tests by automatically exploring edge cases.
Requires: hypothesis (pip install hypothesis)

Run: .venv/bin/pytest src/tests/test_invariants.py -v
"""
from __future__ import annotations

import asyncio
import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch

from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st


def _run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


# ---------------------------------------------------------------------------
# MCP tool parameter validation properties
# (No monkeypatch fixtures — use context managers instead so Hypothesis works)
# ---------------------------------------------------------------------------

@settings(max_examples=30, suppress_health_check=[HealthCheck.too_slow])
@given(scope=st.text(min_size=1, max_size=50))
def test_search_scope_validation_property(scope):
    """search() must either accept a valid scope or return error — never crash."""
    from opencode_search import mcp as mcp_mod
    with patch.object(mcp_mod.runtime_state, "note_activity"), patch("opencode_search.mcp.handle_search_code") as mock_handler:
        mock_handler.return_value = {"results": [], "total": 0}
        result = _run(mcp_mod.search(query="test", scope=scope))
    assert isinstance(result, dict), f"search() returned {type(result)} for scope={scope!r}"
    valid_scopes = {"code", "docs", "all", "similar"}
    if scope not in valid_scopes:
        assert "error" in result, (
            f"search() must return error for invalid scope {scope!r}, got {result}"
        )


@settings(max_examples=30, suppress_health_check=[HealthCheck.too_slow])
@given(relation=st.text(min_size=1, max_size=50))
def test_graph_relation_validation_property(relation):
    """graph() must return error for invalid relation — never crash."""
    from opencode_search import mcp as mcp_mod
    with patch.object(mcp_mod.runtime_state, "note_activity"):
        result = _run(mcp_mod.graph(symbol="foo", project_path="/tmp/nonexistent", relation=relation))
    assert isinstance(result, dict), f"graph() returned {type(result)}"
    valid_relations = {"definition", "callers", "callees", "impact", "path"}
    if relation not in valid_relations:
        assert "error" in result, (
            f"graph() must return error for relation={relation!r}, got {result}"
        )


@settings(max_examples=30, suppress_health_check=[HealthCheck.too_slow])
@given(action=st.text(min_size=1, max_size=30))
def test_build_action_validation_property(action):
    """build() must return error for invalid action — never crash."""
    from opencode_search import mcp as mcp_mod
    with patch.object(mcp_mod.runtime_state, "note_activity"):
        result = _run(mcp_mod.build(project_path="/tmp/nonexistent", action=action))
    assert isinstance(result, dict), f"build() returned {type(result)}"
    valid_actions = {"index", "pipeline", "enrich", "wiki", "ingest",
                     "reindex_wiki", "describe_symbol", "analyze_patterns"}
    if action not in valid_actions:
        assert "error" in result, (
            f"build() must return error for action={action!r}, got {result}"
        )


# ---------------------------------------------------------------------------
# LLM client output properties
# ---------------------------------------------------------------------------

@settings(max_examples=20, suppress_health_check=[HealthCheck.too_slow])
@given(summaries=st.lists(st.text(min_size=1, max_size=100), min_size=0, max_size=30))
def test_community_summary_always_returns_tuple_property(summaries):
    """community_summary() always returns (str, str, str) regardless of input length."""
    from opencode_search.enricher.client import LLMClient

    mock_client = MagicMock(spec=LLMClient)
    mock_client.chat.return_value = "TITLE: Test Cluster\nSUMMARY: A test cluster.\nTYPE: utility"

    real_method = LLMClient.community_summary
    result = real_method(mock_client, summaries)

    assert isinstance(result, tuple), f"community_summary returned {type(result)}"
    assert len(result) == 3, f"community_summary returned {len(result)}-tuple, expected 3"
    title, summary, stype = result
    assert isinstance(title, str), f"title is {type(title)}"
    assert isinstance(summary, str), f"summary is {type(summary)}"
    assert isinstance(stype, str), f"semantic_type is {type(stype)}"
    assert len(title) > 0, "title must be non-empty"


# ---------------------------------------------------------------------------
# Graph storage invariant properties
# (Use tempfile.mkdtemp instead of tmp_path fixture so Hypothesis works)
# ---------------------------------------------------------------------------

@settings(max_examples=20, suppress_health_check=[HealthCheck.too_slow], deadline=None)
@given(file_paths=st.lists(
    st.text(min_size=1, max_size=50, alphabet=st.characters(whitelist_categories=("L", "N"))),
    min_size=0, max_size=10,
))
def test_get_communities_for_files_empty_input_always_empty(file_paths):
    """get_communities_for_files([]) always returns []."""
    from opencode_search.graph.storage import GraphStorage
    with tempfile.TemporaryDirectory() as tmp:
        gs = GraphStorage(str(Path(tmp) / "test.db"))
        gs.open()
        try:
            result = gs.get_communities_for_files([])
            assert result == [], f"Empty input must return [], got {result}"
        finally:
            gs.close()


@settings(max_examples=10, suppress_health_check=[HealthCheck.too_slow], deadline=None)
@given(depth=st.integers(min_value=1, max_value=10))
def test_get_callers_returns_list_for_any_depth(depth):
    """get_callers() returns a list for any valid depth, even for nonexistent nodes."""
    from opencode_search.graph.storage import GraphStorage
    with tempfile.TemporaryDirectory() as tmp:
        gs = GraphStorage(str(Path(tmp) / "depth_test.db"))
        gs.open()
        try:
            result = gs.get_callers("nonexistent_node_id", depth=depth)
            assert isinstance(result, list), f"get_callers returned {type(result)} for depth={depth}"
        finally:
            gs.close()


# ---------------------------------------------------------------------------
# Configuration invariant properties
# ---------------------------------------------------------------------------

def test_config_env_vars_have_safe_defaults():
    """All critical config env vars must have safe non-None defaults."""
    import importlib
    import os
    vars_to_test = [
        "OPENCODE_SCHEMA_VERSION",
        "OPENCODE_FTS_THRESHOLD",
        "OPENCODE_DEBOUNCE_DELAY_MS",
    ]
    original = {k: os.environ.pop(k, None) for k in vars_to_test}
    try:
        import opencode_search.config as cfg
        importlib.reload(cfg)
        assert cfg.SCHEMA_VERSION is not None, "SCHEMA_VERSION must have a default"
        assert isinstance(cfg.FTS_THRESHOLD, int) and cfg.FTS_THRESHOLD > 0
        assert isinstance(cfg.DEBOUNCE_DELAY_MS, int) and cfg.DEBOUNCE_DELAY_MS >= 100
    finally:
        for k, v in original.items():
            if v is not None:
                os.environ[k] = v
        importlib.reload(cfg)


# ---------------------------------------------------------------------------
# Registry invariant properties
# ---------------------------------------------------------------------------

def test_load_registry_never_raises_on_corrupt_file(tmp_path, monkeypatch):
    """load_registry() must return {} on corrupt JSON, never raise."""
    from opencode_search import config as cfg
    corrupt_file = tmp_path / "registry.json"
    corrupt_file.write_text("{not valid json!@#}")
    monkeypatch.setattr(cfg, "REGISTRY_PATH", corrupt_file)
    result = cfg.load_registry()
    assert isinstance(result, dict), f"load_registry on corrupt file returned {type(result)}"


def test_load_registry_never_raises_on_missing_file(tmp_path, monkeypatch):
    """load_registry() must return {} on missing file, never raise."""
    from opencode_search import config as cfg
    monkeypatch.setattr(cfg, "REGISTRY_PATH", tmp_path / "nonexistent_registry.json")
    result = cfg.load_registry()
    assert isinstance(result, dict), f"load_registry on missing file returned {type(result)}"
