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
    # Five rows left with tier 3: service_mesh/{services}, feature_map/{features},
    # business_rules/{rules}, process_flows/{flows}, patterns/{frameworks}. Each was RED the
    # moment R0a dropped the variant from `_VALID` — handle_overview answers an unknown `what`
    # with {"error": ...}, so `required_keys` went missing rather than empty. The property is not
    # re-pointed here: this file asserts the *shape of an answer*, and the surviving statement
    # about a deleted variant is that there is no answer at all, which is what
    # test_feature_proof.py's fp1 (daemon returns "unknown") and fp2 (absent from `_VALID`)
    # assert over exactly these five names.
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
    from rag_search.server.mcp import overview as overview_tool

    path = "" if what == "projects" else fed_root
    result = asyncio.run(overview_tool(path, what))
    data = json.loads(result)
    missing = required_keys - set(data.keys())
    assert not missing, f"overview(what={what!r}) missing keys {missing}: {result[:200]}"
    if non_empty:
        for k in required_keys:
            v = data.get(k)
            assert v, f"overview(what={what!r})[{k!r}] must be non-empty, got {v!r}"
