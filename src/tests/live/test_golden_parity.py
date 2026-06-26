"""P16.10: golden-diff parity — live MCP overview outputs match expected shape."""
from __future__ import annotations

import asyncio
import json

import pytest

pytestmark = pytest.mark.live

# (what, required_top_level_keys, non_empty_if_indexed)
OVERVIEW_SHAPE: list[tuple[str, set[str], bool]] = [
    ("structure",            {"path", "symbols", "communities", "files_with_symbols"}, True),
    ("communities",          {"communities"},                                  True),
    ("status",               {"path", "symbols", "communities"},              True),
    ("import_cycles",        {"cycles", "cycle_count", "has_cycles"},         False),
    ("surprising_connections", {"connections"},                                False),
    ("suggested_questions",  {"questions"},                                    True),
    ("service_mesh",         {"services"},                                     False),
    ("feature_map",          {"features"},                                     False),
    ("business_rules",       {"rules"},                                        False),
    ("process_flows",        {"flows"},                                        False),
    ("patterns",             {"frameworks"},                                   False),
    ("projects",             {"projects"},                                     True),
]


@pytest.fixture(scope="module")
def fed_root(sample_workspace) -> str:
    from tests.live._sample_workspace import SampleWorkspace
    assert isinstance(sample_workspace, SampleWorkspace)
    return sample_workspace.fed_root


@pytest.mark.parametrize("what,required_keys,non_empty", OVERVIEW_SHAPE)
def test_overview_shape(what, required_keys, non_empty, fed_root):
    """Live overview(what=X) must return the expected top-level keys."""
    from opencode_search.server.mcp import overview as overview_tool

    path = "" if what == "projects" else fed_root
    result = asyncio.run(overview_tool(path, what))
    data = json.loads(result)
    missing = required_keys - set(data.keys())
    assert not missing, f"overview(what={what!r}) missing keys {missing}: {result[:200]}"
    if non_empty:
        for k in required_keys:
            v = data.get(k)
            assert v, f"overview(what={what!r})[{k!r}] must be non-empty, got {v!r}"
