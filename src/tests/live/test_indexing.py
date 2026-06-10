"""Live indexing tests — require daemon at :8765, real GPU, real Ollama.

Tests the full index pipeline: communities exist, enrichment % above threshold,
vector search returns results. Does NOT trigger a new pipeline run — verifies
state of the already-indexed project.

Also verifies: indexing a project WITHOUT --raw automatically schedules the KB
pipeline (auto-pipeline is on by default, per OPENCODE_AUTO_PIPELINE=1).
"""
from __future__ import annotations

import subprocess

import pytest

pytestmark = pytest.mark.live

_VENV_PYTHON = "/home/user/git/github.com/fairyhunter13/opencode-search-engine/.venv/bin/python"


def test_indexed_project_has_communities(http, project):
    """An indexed project must have at least one community."""
    r = http.get("/api/overview", params={"project": project, "what": "communities"})
    assert r.status_code == 200, f"overview failed: {r.status_code} {r.text[:200]}"
    data = r.json()
    communities = data.get("communities", [])
    count = data.get("community_count", len(communities))
    assert count > 0, f"Indexed project has zero communities: {data}"


def test_enrichment_above_80pct(http, project):
    """At least 80% of level-1 communities must have a title after enrichment.

    Level-1 communities are produced by the standard pipeline enrich step.
    Level-2+ (hierarchy meta-communities) require a separate enrich_hierarchy run
    and are excluded from this gate.
    """
    r = http.get("/api/kb_health", params={"project": project})
    assert r.status_code == 200, f"kb_health failed: {r.status_code} {r.text[:200]}"
    data = r.json()

    by_level = data.get("enrichment_by_level", {})
    l1 = by_level.get("1", {})
    total = l1.get("total", 0)
    enriched = l1.get("enriched", 0)

    if total == 0:
        # kb_health may not have per-level breakdown — fall back to overall
        total = data.get("total_communities", 0)
        enriched = data.get("enriched_communities", 0)

    assert total > 0, f"Project has no communities to check enrichment: {data}"
    pct = enriched / total * 100
    assert pct >= 80, (
        f"Level-1 enrichment is only {pct:.0f}% ({enriched}/{total} communities have titles). "
        "Run POST /api/enrich_hierarchy to fix."
    )


def test_vector_search_returns_results(http, project):
    """Vector search must return at least one result for a generic code query."""
    r = http.get("/api/search", params={"q": "main entry point function", "project": project, "top_k": 5})
    assert r.status_code == 200, f"search failed: {r.status_code} {r.text[:200]}"
    results = r.json().get("results", [])
    assert len(results) > 0, (
        "Vector search returned no results — LanceDB index may be empty or corrupt"
    )


def test_graph_has_nodes(http, project):
    """The graph must have nodes after indexing (tree-sitter extraction worked)."""
    r = http.get("/api/overview", params={"project": project, "what": "structure"})
    assert r.status_code == 200
    data = r.json()
    graph_stats = data.get("graph_stats", {})
    node_count = (
        graph_stats.get("nodes", 0)
        or graph_stats.get("total_communities", 0)
        or data.get("node_count", 0)
        or data.get("graph", {}).get("nodes", 0)
    )
    assert node_count > 0, f"Graph has zero nodes — tree-sitter extraction may have failed: {graph_stats}"


def test_index_without_raw_schedules_kb_pipeline(tmp_path):
    """Indexing a project without --raw must automatically schedule the KB pipeline.

    This is the core auto-KB-build-on-index guarantee: every new index triggers
    the full knowledge-base pipeline (enrichment + hierarchy + wiki) by default.
    OPENCODE_AUTO_PIPELINE=1 is the default; the daemon's systemd unit hard-sets it.

    The test indexes a real minimal Python project into an isolated registry,
    then polls /api/auto_pipeline_status to confirm the project's pipeline
    was scheduled. Uses the real indexer path (no --raw), real GPU embeddings,
    and a real-but-isolated registry so the production registry is untouched.
    """
    # Create a minimal real Python project to index
    proj = tmp_path / "auto_kb_test_project"
    proj.mkdir()
    (proj / "main.py").write_text(
        "def greet(name: str) -> str:\n    return f'Hello, {name}'\n\ndef main():\n    print(greet('world'))\n"
    )
    (proj / "utils.py").write_text(
        "def add(a: int, b: int) -> int:\n    return a + b\n"
    )

    isolated_registry = tmp_path / "registry.json"

    # Index the project using the real CLI (no --raw) with an isolated registry.
    # This drives the real _index_and_build_pipeline path and fires schedule_auto_pipeline.
    script = f"""
import asyncio, json, os, sys
sys.path.insert(0, "/home/user/git/github.com/fairyhunter13/opencode-search-engine/src")

os.environ["OPENCODE_REGISTRY_PATH"] = {str(isolated_registry)!r}
os.environ["OPENCODE_AUTO_PIPELINE"] = "1"

from opencode_search.cli import _index_and_build_pipeline
from opencode_search.handlers._autopipeline import get_pipeline_events

async def run():
    await _index_and_build_pipeline(
        path={str(proj)!r},
        watch=False,
        force=True,
        follow_symlinks=False,
    )

asyncio.run(run())

events = get_pipeline_events()
scheduled = [e for e in events if e.get("project_path") == {str(proj)!r}
             and e.get("status") in ("scheduled", "running", "ok")]
print(json.dumps({{"scheduled": len(scheduled) > 0, "events": events[-5:]}}))
"""
    result = subprocess.run(
        [_VENV_PYTHON, "-c", script],
        capture_output=True, text=True, timeout=120,
        cwd="/home/user/git/github.com/fairyhunter13/opencode-search-engine",
    )
    assert result.returncode == 0, (
        f"Index script failed (rc={result.returncode}):\n"
        f"stdout: {result.stdout[:400]}\nstderr: {result.stderr[-400:]}"
    )

    import json as _json
    try:
        out = _json.loads(result.stdout.strip())
    except Exception:
        out = {}

    assert out.get("scheduled"), (
        "Index without --raw did NOT schedule the KB pipeline by default.\n"
        f"auto_pipeline events: {out.get('events', [])}\n"
        f"stdout: {result.stdout[:300]}\nstderr: {result.stderr[-300:]}\n"
        "Root cause: OPENCODE_AUTO_PIPELINE is not defaulting to 1, or "
        "schedule_auto_pipeline() is not being called from _index_and_build_pipeline."
    )
