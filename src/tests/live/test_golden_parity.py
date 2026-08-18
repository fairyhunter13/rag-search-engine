"""golden-diff parity — live MCP overview outputs match expected shape."""
from __future__ import annotations

import json

import pytest

from tests.live._run import run_tool

pytestmark = pytest.mark.live

# (what, required_top_level_keys, non_empty_if_indexed)
OVERVIEW_SHAPE: list[tuple[str, set[str], bool]] = [
    ("structure",            {"path", "symbols", "communities", "files_with_symbols"}, True),
    ("communities",          {"communities"},                                  True),
    ("status",               {"path", "symbols", "communities"},              True),
    ("import_cycles",        {"cycles", "cycle_count", "has_cycles"},         False),
    ("surprising_connections", {"connections"},                                False),
    # This table asserts the shape of an *answer*, so every row must name a live `what`. A variant
    # that is gone belongs in test_feature_proof.py instead, whose fp1/fp2 assert the daemon
    # answers "unknown" and the name is absent from `_VALID` — there is no shape to check when
    # handle_overview replies {"error": ...} and `required_keys` goes missing rather than empty.
    # An unqueried `projects` call deliberately returns counts and roots, never the row list —
    # 236 rows is the fleet echoing itself. The rows come back when `query` asks for them, which
    # is the shape test_mcp_protocol_*.py assert.
    ("projects",             {"project_count", "enabled_count", "enabled_roots"}, True),
]


@pytest.fixture(scope="module")
def fed_root(sample_workspace) -> str:
    from tests.live._sample_workspace import SampleWorkspace
    assert isinstance(sample_workspace, SampleWorkspace)
    return sample_workspace.fed_root


@pytest.mark.parametrize("what,required_keys,non_empty", OVERVIEW_SHAPE)
def test_overview_shape(what, required_keys, non_empty, fed_root):
    """Live overview(what=X) must return the expected top-level keys."""
    from rag_search.server.mcp import overview as overview_tool

    path = "" if what == "projects" else fed_root
    result = run_tool(overview_tool(path, what))
    data = json.loads(result)
    missing = required_keys - set(data.keys())
    assert not missing, f"overview(what={what!r}) missing keys {missing}: {result[:200]}"
    if non_empty:
        for k in required_keys:
            v = data.get(k)
            assert v, f"overview(what={what!r})[{k!r}] must be non-empty, got {v!r}"
