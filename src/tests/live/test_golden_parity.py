"""P16.10: golden-diff parity — live MCP overview outputs match expected shape."""
from __future__ import annotations

import asyncio
import json

import pytest

pytestmark = pytest.mark.live

# (what, required_top_level_keys, non_empty_if_indexed)
OVERVIEW_SHAPE: list[tuple[str, set[str], bool]] = [
    ("structure",            {"path", "symbols", "communities", "file_count"}, True),
    ("communities",          {"communities"},                                  True),
    ("status",               {"path", "symbols", "communities"},              True),
    ("hierarchy",            {"hierarchy"},                                     True),
    ("architecture_domains", {"architecture_domains"},                         True),
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
def acme_path():
    from opencode_search.core.registry import list_projects
    p = next(
        (p.path for p in list_projects()
         if "redacted-name-3" in p.path and "promo" not in p.path and p.enabled),
        None,
    )
    assert p, "redacted-name-3 must be registered (run P8)"
    return p


@pytest.mark.parametrize("what,required_keys,non_empty", OVERVIEW_SHAPE)
def test_overview_shape(what, required_keys, non_empty, acme_path):
    """Live overview(what=X) must return the expected top-level keys."""
    from opencode_search.server.mcp import overview as overview_tool

    path = "" if what == "projects" else acme_path
    result = asyncio.run(overview_tool(path, what))
    data = json.loads(result)
    missing = required_keys - set(data.keys())
    assert not missing, f"overview(what={what!r}) missing keys {missing}: {result[:200]}"
    if non_empty:
        for k in required_keys:
            v = data.get(k)
            assert v, f"overview(what={what!r})[{k!r}] must be non-empty, got {v!r}"
