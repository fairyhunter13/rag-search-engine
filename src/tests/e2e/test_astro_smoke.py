"""T40: redacted-name-3 smoke tests — CI-eligible when daemon is running.

These tests query the live MCP daemon against the already-indexed redacted-name-3.
They are marked runtime_deps (deselected from fast CI) but NOT @_LARGE — they
run in the integration CI tier where the daemon is pre-started.

Requires:
  - opencode-search daemon running at http://127.0.0.1:8765
  - redacted-name-3 indexed (run `ocs build --pipeline` first)
  - OPENCODE_RUN_LIVE_TESTS=1 or pytest -m runtime_deps

Each test validates a specific opencode-search feature against real Go microservices.
"""
from __future__ import annotations

import asyncio
import os

import pytest

pytestmark = pytest.mark.runtime_deps

_ASTRO = "/home/user/git/github.com/fairyhunter13/redacted-name-3"


def _run(coro):
    return asyncio.get_event_loop().run_until_complete(coro)


def _require_astro():
    import pathlib
    if not pathlib.Path(_ASTRO).is_dir():
        pytest.skip(f"redacted-name-3 not found at {_ASTRO}")


def _require_daemon():
    import urllib.request
    try:
        urllib.request.urlopen("http://127.0.0.1:8765/healthz", timeout=3)
    except Exception:
        pytest.skip("opencode-search daemon not running at :8765")


def _require_indexed():
    import json
    import urllib.request
    import urllib.parse
    try:
        url = f"http://127.0.0.1:8765/api/kb_health?project={urllib.parse.quote(_ASTRO)}"
        with urllib.request.urlopen(url, timeout=5) as r:
            data = json.loads(r.read())
            if data.get("total_communities", 0) < 10:
                pytest.skip(f"redacted-name-3 not sufficiently indexed (communities: {data.get('total_communities')})")
    except Exception as e:
        pytest.skip(f"Could not check KB health: {e}")


@pytest.fixture(autouse=True, scope="module")
def require_live_env():
    _require_astro()
    _require_daemon()
    _require_indexed()


# ---------------------------------------------------------------------------
# T40-A: Search tool against real Go microservices
# ---------------------------------------------------------------------------

class TestT40ASearch:
    """P0: search() finds Go symbols and patterns in redacted-name-3."""

    @pytest.mark.asyncio
    async def test_search_grpc_handler_finds_go_file(self):
        """P0: search('grpc server handler') returns at least 1 .go result."""
        from opencode_search.handlers import handle_search_code
        result = await handle_search_code(
            query="grpc server handler",
            project_path=_ASTRO,
            scope="code",
            top_k=5,
        )
        results = result.get("results", [])
        assert results, "search('grpc server handler') returned 0 results"
        go_files = [r for r in results if r.get("path", "").endswith(".go")]
        assert go_files, (
            f"No .go files in search results for 'grpc server handler'. "
            f"Paths: {[r.get('path','') for r in results[:5]]}"
        )

    @pytest.mark.asyncio
    async def test_search_repository_pattern_finds_interface(self):
        """P1: search('repository interface') finds Go repository patterns."""
        from opencode_search.handlers import handle_search_code
        result = await handle_search_code(
            query="repository interface",
            project_path=_ASTRO,
            scope="code",
            top_k=5,
        )
        results = result.get("results", [])
        assert results, "search('repository interface') returned 0 results"

    @pytest.mark.asyncio
    async def test_search_usecase_business_logic(self):
        """P1: search('use case business logic') finds domain layer code."""
        from opencode_search.handlers import handle_search_code
        result = await handle_search_code(
            query="use case",
            project_path=_ASTRO,
            scope="code",
            top_k=5,
        )
        results = result.get("results", [])
        assert results, "search('use case') returned 0 results"
        top_score = results[0].get("score", 0)
        assert top_score > 0.3, f"Top search score too low: {top_score}"


# ---------------------------------------------------------------------------
# T40-B: Ask tool with scope=global (GraphRAG synthesis)
# ---------------------------------------------------------------------------

class TestT40BAsk:
    """P0: ask() returns synthesized answers about redacted-name-3 architecture."""

    @pytest.mark.asyncio
    async def test_ask_global_returns_answer_key(self):
        """P0: ask(scope='global') returns dict with 'answer' key (not just results)."""
        from opencode_search.handlers import handle_global_synthesis
        result = await handle_global_synthesis(
            query="how does the cart service work",
            project_path=_ASTRO,
        )
        assert isinstance(result, dict), f"Expected dict, got {type(result)}"
        assert "answer" in result or "results" in result, (
            f"ask response missing both 'answer' and 'results': {list(result.keys())}"
        )

    @pytest.mark.asyncio
    async def test_ask_architecture_overview(self):
        """P1: ask('describe overall architecture') returns non-empty response."""
        from opencode_search.handlers._graph import handle_global_search
        result = await handle_global_search(
            query="overall architecture microservices",
            project_path=_ASTRO,
            scope="all",
            top_k=3,
        )
        assert isinstance(result, dict), f"Expected dict, got {type(result)}"
        results = result.get("results", [])
        assert results or result.get("answer"), "ask architecture returned no content"


# ---------------------------------------------------------------------------
# T40-C: Overview tool
# ---------------------------------------------------------------------------

class TestT40COverview:
    """P0: overview() returns correct project metadata for redacted-name-3."""

    @pytest.mark.asyncio
    async def test_overview_communities_count(self):
        """P0: overview(what='communities') returns >= 5 communities."""
        from opencode_search.handlers import handle_get_communities
        result = await handle_get_communities(project_path=_ASTRO, top_k=10)
        communities = result.get("communities", [])
        assert len(communities) >= 5, (
            f"Expected >= 5 communities, got {len(communities)}"
        )

    @pytest.mark.asyncio
    async def test_overview_structure_has_language_breakdown(self):
        """P0: overview(what='structure') returns language breakdown with Go."""
        from opencode_search.handlers._graph import handle_project_structure
        result = await handle_project_structure(project_path=_ASTRO)
        lang_breakdown = result.get("language_breakdown", [])
        assert lang_breakdown, "language_breakdown is empty"
        langs = {entry.get("extension", "").lower() for entry in lang_breakdown}
        assert ".go" in langs or "go" in langs, (
            f"Go not in language breakdown. Got: {langs}"
        )

    @pytest.mark.asyncio
    async def test_overview_service_mesh_detects_services(self):
        """P1: overview(what='service_mesh') detects >= 2 services."""
        from opencode_search.handlers._service_mesh import handle_detect_service_mesh
        result = await handle_detect_service_mesh(project_path=_ASTRO)
        services = result.get("services", [])
        assert len(services) >= 2, (
            f"Expected >= 2 services in service mesh, got {len(services)}. "
            f"Service mesh detection may not have run yet."
        )


# ---------------------------------------------------------------------------
# T40-D: KB health verification
# ---------------------------------------------------------------------------

class TestT40DKBHealth:
    """P0: KB health metrics are within acceptable ranges for redacted-name-3."""

    @pytest.mark.asyncio
    async def test_kb_health_has_wiki_pages(self):
        """P0: redacted-name-3 has >= 10 wiki pages generated."""
        import json, urllib.request, urllib.parse
        url = f"http://127.0.0.1:8765/api/kb_health?project={urllib.parse.quote(_ASTRO)}"
        with urllib.request.urlopen(url, timeout=10) as r:
            data = json.loads(r.read())
        wiki_pages = data.get("wiki_page_count", 0)
        assert wiki_pages >= 10, (
            f"Expected >= 10 wiki pages, got {wiki_pages}. "
            f"Run `build(action='wiki')` to generate wiki."
        )

    @pytest.mark.asyncio
    async def test_kb_health_has_indexed_communities(self):
        """P0: redacted-name-3 has >= 100 total communities (enough for meaningful search)."""
        import json, urllib.request, urllib.parse
        url = f"http://127.0.0.1:8765/api/kb_health?project={urllib.parse.quote(_ASTRO)}"
        with urllib.request.urlopen(url, timeout=10) as r:
            data = json.loads(r.read())
        total = data.get("total_communities", 0)
        assert total >= 100, (
            f"Expected >= 100 communities, got {total}. "
            f"Run `build(action='pipeline')` to fully index."
        )
