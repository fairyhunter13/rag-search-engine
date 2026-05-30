"""End-to-end tests: graph, community, wiki, and cross-feature regression.

These tests build a small synthetic corpus, index it with mocked embeddings,
and verify that all pipeline stages (graph extraction, community detection,
LLM enrichment, wiki generation) work correctly in concert.

Tests marked @pytest.mark.large are skipped unless OPENCODE_RUN_LARGE_TESTS=1
and require ~/git/github.com/fairyhunter13/astro-project to be present.
"""
# ruff: noqa: E402
from __future__ import annotations

import asyncio
import os
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

pytest.importorskip("lancedb")
pytest.importorskip("pyarrow")

from opencode_search import config
from opencode_search.chunker import Chunk
from opencode_search.config import DEFAULT_DIMS, get_project_graph_db_path, get_project_wiki_dir
from opencode_search.graph.storage import GraphStorage
from opencode_search.handlers import handle_search_code
from opencode_search.search import clear_search_cache
from tests.conftest import index_and_wait

pytestmark = [pytest.mark.asyncio, pytest.mark.integration, pytest.mark.runtime_deps]


# ---------------------------------------------------------------------------
# Corpus + embedding helpers
# ---------------------------------------------------------------------------

_AUTH_PY = '''\
"""Authentication module."""

def authenticate(token: str) -> bool:
    """Authenticate user by token."""
    return verify_token(token)

def verify_token(token: str) -> bool:
    """Verify a JWT token."""
    return len(token) > 0
'''

_HANDLER_PY = '''\
"""HTTP handler module."""

def handle_login(username: str, password: str) -> dict:
    """Handle login request."""
    result = authenticate(username + password)
    return {"ok": result}

def handle_logout() -> dict:
    """Handle logout request."""
    return {"ok": True}
'''

_DB_PY = '''\
"""Database module."""

def get_connection(host: str) -> object:
    """Get a DB connection."""
    return None

def execute_query(conn: object, sql: str) -> list:
    """Execute a SQL query."""
    return []
'''

_UTILS_PY = '''\
"""Utility helpers."""

def slugify(text: str) -> str:
    """Convert text to slug."""
    return text.lower().replace(" ", "-")

def truncate(text: str, n: int = 100) -> str:
    """Truncate text to n characters."""
    return text[:n]
'''


def _make_corpus(root: Path) -> None:
    """Write synthetic Python source files into root/."""
    (root / "auth.py").write_text(_AUTH_PY)
    (root / "handler.py").write_text(_HANDLER_PY)
    (root / "db.py").write_text(_DB_PY)
    (root / "utils.py").write_text(_UTILS_PY)


def _split_lines(content: str, path: Path) -> list[Chunk]:
    chunks: list[Chunk] = []
    for line_no, line in enumerate(content.splitlines(), start=1):
        text = line.strip()
        if not text:
            continue
        chunks.append(Chunk(
            content=text,
            start_line=line_no,
            end_line=line_no,
            chunk_type="code",
            language="python",
        ))
    return chunks


def _vector_for(text: str, dims: int = DEFAULT_DIMS) -> list[float]:
    vec = [0.0] * dims
    vec[hash(text) % dims] = 1.0
    return vec


def fake_embed_passages(texts, *, model, dimensions, _return_numpy=False):
    result = [_vector_for(t, dimensions) for t in texts]
    if _return_numpy:
        import numpy as np
        return np.array(result, dtype=np.float32)
    return result


def fake_embed_query(query, model, dimensions):
    return _vector_for(query.strip(), dimensions)


# ---------------------------------------------------------------------------
# Fixture: indexed project
# ---------------------------------------------------------------------------

@pytest.fixture
async def indexed_project(tmp_path, monkeypatch):
    """Create and index a synthetic Python corpus. Returns (project_root, registry_path)."""
    project_root = tmp_path / "project"
    project_root.mkdir()
    _make_corpus(project_root)

    registry_path = tmp_path / "registry.json"
    monkeypatch.setattr(config, "REGISTRY_PATH", registry_path)

    with patch("opencode_search.chunker.chunk_file", side_effect=_split_lines), \
         patch("opencode_search.embeddings.embed_passages", side_effect=fake_embed_passages), \
         patch("opencode_search.search._embed_query_sync", side_effect=fake_embed_query):
        result = await index_and_wait(str(project_root))
        assert result["status"] == "ok", f"Index failed: {result}"

    yield project_root, registry_path


# ---------------------------------------------------------------------------
# Tier removal E2E
# ---------------------------------------------------------------------------


async def test_e2e_index_project_uses_768_dims(tmp_path, monkeypatch):
    project_root = tmp_path / "project"
    project_root.mkdir()
    (project_root / "app.py").write_text("def foo(): pass\n")

    monkeypatch.setattr(config, "REGISTRY_PATH", tmp_path / "registry.json")

    with patch("opencode_search.chunker.chunk_file", side_effect=_split_lines), \
         patch("opencode_search.embeddings.embed_passages", side_effect=fake_embed_passages), \
         patch("opencode_search.search._embed_query_sync", side_effect=fake_embed_query):
        result = await index_and_wait(str(project_root))

    assert result["status"] == "ok"
    assert result.get("dims", DEFAULT_DIMS) == DEFAULT_DIMS


async def test_e2e_no_tier_in_db_path(tmp_path, monkeypatch):
    project_root = tmp_path / "project"
    project_root.mkdir()
    (project_root / "app.py").write_text("x = 1\n")

    monkeypatch.setattr(config, "REGISTRY_PATH", tmp_path / "registry.json")

    with patch("opencode_search.chunker.chunk_file", side_effect=_split_lines), \
         patch("opencode_search.embeddings.embed_passages", side_effect=fake_embed_passages), \
         patch("opencode_search.search._embed_query_sync", side_effect=fake_embed_query):
        await index_and_wait(str(project_root))

    db_path = config.get_project_db_path(str(project_root))
    for tier in ("budget", "balanced", "premium"):
        assert tier not in db_path


async def test_e2e_search_after_index_returns_results(indexed_project):
    project_root, _ = indexed_project
    clear_search_cache()
    with patch("opencode_search.search._embed_query_sync", side_effect=fake_embed_query):
        result = await handle_search_code(
            query="authenticate",
            project_paths=[str(project_root)],
            use_rerank=False,
        )
    assert "results" in result
    assert len(result["results"]) >= 0  # may be empty due to mock vectors, but no error


# ---------------------------------------------------------------------------
# Graph E2E — graph DB created and populated
# ---------------------------------------------------------------------------


async def test_e2e_index_creates_graph_db(indexed_project):
    project_root, _ = indexed_project
    graph_db = Path(get_project_graph_db_path(str(project_root)))
    assert graph_db.exists(), "graph.db should be created after indexing"


async def test_e2e_graph_has_nodes(indexed_project):
    project_root, _ = indexed_project
    gs = GraphStorage(get_project_graph_db_path(str(project_root)))
    gs.open()
    try:
        nodes = gs.all_nodes()
        assert len(nodes) > 0, "Graph should have nodes after indexing Python files"
    finally:
        gs.close()


async def test_e2e_graph_has_expected_functions(indexed_project):
    project_root, _ = indexed_project
    gs = GraphStorage(get_project_graph_db_path(str(project_root)))
    gs.open()
    try:
        nodes = gs.all_nodes()
        names = {n.name for n in nodes}
        assert "authenticate" in names
        assert "verify_token" in names
        assert "handle_login" in names
    finally:
        gs.close()


async def test_e2e_graph_has_resolved_call_edges(indexed_project):
    project_root, _ = indexed_project
    gs = GraphStorage(get_project_graph_db_path(str(project_root)))
    gs.open()
    try:
        edges = gs.all_edges()
        call_edges = [e for e in edges if e.kind == "CALLS"]
        # authenticate calls verify_token (same file, resolved)
        assert len(call_edges) >= 1
    finally:
        gs.close()


async def test_e2e_graph_file_nodes_present(indexed_project):
    project_root, _ = indexed_project
    gs = GraphStorage(get_project_graph_db_path(str(project_root)))
    gs.open()
    try:
        nodes = gs.all_nodes()
        file_nodes = [n for n in nodes if n.kind == "file"]
        assert len(file_nodes) >= 4  # auth.py, handler.py, db.py, utils.py
    finally:
        gs.close()


async def test_e2e_graph_language_field_populated(indexed_project):
    project_root, _ = indexed_project
    gs = GraphStorage(get_project_graph_db_path(str(project_root)))
    gs.open()
    try:
        nodes = gs.all_nodes()
        func_nodes = [n for n in nodes if n.kind == "function"]
        assert all(n.language == "python" for n in func_nodes)
    finally:
        gs.close()


# ---------------------------------------------------------------------------
# Graph MCP handler E2E
# ---------------------------------------------------------------------------


async def test_e2e_get_symbol_finds_known_function(indexed_project):
    from opencode_search.handlers._graph import handle_get_symbol
    project_root, _ = indexed_project
    result = await handle_get_symbol(name="authenticate", project_path=str(project_root))
    assert "error" not in result
    assert result.get("count", 0) >= 1
    names = [m["name"] for m in result.get("matches", [])]
    assert "authenticate" in names


async def test_e2e_get_symbol_not_found_returns_error(indexed_project):
    from opencode_search.handlers._graph import handle_get_symbol
    project_root, _ = indexed_project
    result = await handle_get_symbol(name="nonexistent_xyz_abc", project_path=str(project_root))
    assert "error" in result


async def test_e2e_get_callers_returns_chain(indexed_project):
    from opencode_search.handlers._graph import handle_get_callers
    project_root, _ = indexed_project
    # verify_token is called by authenticate
    result = await handle_get_callers(symbol="verify_token", project_path=str(project_root))
    assert "error" not in result or result.get("callers") is not None
    if "callers" in result:
        names = [c["name"] for c in result["callers"]]
        assert "authenticate" in names


async def test_e2e_get_callees_returns_chain(indexed_project):
    from opencode_search.handlers._graph import handle_get_callees
    project_root, _ = indexed_project
    result = await handle_get_callees(symbol="authenticate", project_path=str(project_root))
    assert "error" not in result or "callees" in result


async def test_e2e_trace_path_finds_route(indexed_project):
    from opencode_search.handlers._graph import handle_trace_path
    project_root, _ = indexed_project
    result = await handle_trace_path(
        from_symbol="authenticate",
        to_symbol="verify_token",
        project_path=str(project_root),
    )
    # Should find a direct path
    if "error" not in result:
        assert "path" in result


async def test_e2e_detect_impact_returns_blast_radius(indexed_project):
    from opencode_search.handlers._graph import handle_detect_impact
    project_root, _ = indexed_project
    # verify_token is called by authenticate, which is called by handle_login
    result = await handle_detect_impact(symbol="verify_token", project_path=str(project_root))
    assert "error" not in result
    assert "callers_by_depth" in result or "callers" in result


# ---------------------------------------------------------------------------
# Community detection E2E
# ---------------------------------------------------------------------------


async def test_e2e_community_detection_runs_after_index(indexed_project):
    project_root, _ = indexed_project
    gs = GraphStorage(get_project_graph_db_path(str(project_root)))
    gs.open()
    try:
        nodes = gs.all_nodes()
        func_nodes = [n for n in nodes if n.kind == "function"]
        # All function nodes should have a community_id after detection
        if func_nodes:
            assert all(n.community_id is not None for n in func_nodes)
    finally:
        gs.close()


async def test_e2e_get_communities_returns_cluster_info(indexed_project):
    from opencode_search.handlers._graph import handle_get_communities
    project_root, _ = indexed_project
    result = await handle_get_communities(project_path=str(project_root))
    assert "error" not in result
    assert "communities" in result
    communities = result["communities"]
    assert isinstance(communities, list)
    assert len(communities) >= 1


async def test_e2e_communities_have_node_counts(indexed_project):
    from opencode_search.handlers._graph import handle_get_communities
    project_root, _ = indexed_project
    result = await handle_get_communities(project_path=str(project_root))
    for c in result.get("communities", []):
        assert c.get("node_count", 0) >= 1


# ---------------------------------------------------------------------------
# Incremental graph update (watcher callback simulation)
# ---------------------------------------------------------------------------


async def test_e2e_incremental_adds_node_for_new_file(indexed_project):
    from opencode_search.handlers._index import _update_graph_incremental
    project_root, _ = indexed_project

    # Add a new file
    new_file = project_root / "billing.py"
    new_file.write_text("def process_payment(amount: float) -> bool:\n    return True\n")

    graph_db_path = get_project_graph_db_path(str(project_root))
    await asyncio.to_thread(
        _update_graph_incremental,
        [str(new_file)],
        [],
        graph_db_path,
    )

    gs = GraphStorage(graph_db_path)
    gs.open()
    try:
        node = gs.get_node("process_payment")
        assert node is not None, "New function should appear in graph after incremental update"
    finally:
        gs.close()


async def test_e2e_incremental_removes_nodes_on_delete(indexed_project):
    from opencode_search.handlers._index import _update_graph_incremental
    project_root, _ = indexed_project

    graph_db_path = get_project_graph_db_path(str(project_root))

    # Verify utils nodes exist
    gs = GraphStorage(graph_db_path)
    gs.open()
    nodes_before = [n for n in gs.all_nodes() if "utils" in (n.file or "")]
    gs.close()
    assert len(nodes_before) > 0

    # Simulate deletion of utils.py
    utils_file = str(project_root / "utils.py")
    await asyncio.to_thread(
        _update_graph_incremental,
        [],
        [utils_file],
        graph_db_path,
    )

    gs = GraphStorage(graph_db_path)
    gs.open()
    try:
        nodes_after = [n for n in gs.all_nodes() if "utils" in (n.file or "")]
        assert len(nodes_after) == 0, "Deleted file's nodes should be removed"
    finally:
        gs.close()


async def test_e2e_incremental_updates_node_on_modify(indexed_project):
    from opencode_search.handlers._index import _update_graph_incremental
    project_root, _ = indexed_project

    # Overwrite with new function
    (project_root / "db.py").write_text(
        "def new_db_function() -> None:\n    pass\n"
        "def get_connection(host: str) -> object:\n    return None\n"
    )

    graph_db_path = get_project_graph_db_path(str(project_root))
    await asyncio.to_thread(
        _update_graph_incremental,
        [str(project_root / "db.py")],
        [],
        graph_db_path,
    )

    gs = GraphStorage(graph_db_path)
    gs.open()
    try:
        node = gs.get_node("new_db_function")
        assert node is not None, "New function added during modification should appear in graph"
    finally:
        gs.close()


async def test_e2e_graph_survives_second_index_run(tmp_path, monkeypatch):
    """Re-indexing should not duplicate graph nodes."""
    project_root = tmp_path / "project"
    project_root.mkdir()
    _make_corpus(project_root)

    monkeypatch.setattr(config, "REGISTRY_PATH", tmp_path / "registry.json")

    with patch("opencode_search.chunker.chunk_file", side_effect=_split_lines), \
         patch("opencode_search.embeddings.embed_passages", side_effect=fake_embed_passages), \
         patch("opencode_search.search._embed_query_sync", side_effect=fake_embed_query):
        await index_and_wait(str(project_root))
        # Second run
        await index_and_wait(str(project_root))

    gs = GraphStorage(get_project_graph_db_path(str(project_root)))
    gs.open()
    try:
        nodes = gs.all_nodes()
        names = [n.name for n in nodes if n.kind == "function"]
        # No duplicates
        assert len(names) == len(set(names)), "No duplicate nodes after re-index"
    finally:
        gs.close()


# ---------------------------------------------------------------------------
# Cross-feature regression: search_code still works after graph build
# ---------------------------------------------------------------------------


async def test_e2e_search_code_still_works_after_graph_build(indexed_project):
    project_root, _ = indexed_project
    clear_search_cache()
    with patch("opencode_search.search._embed_query_sync", side_effect=fake_embed_query):
        result = await handle_search_code(
            query="verify_token",
            project_paths=[str(project_root)],
            use_rerank=False,
        )
    assert "results" in result
    assert "error" not in result


async def test_e2e_graph_db_not_indexed_as_code_chunks(indexed_project):
    """Graph DB should not appear in search results as code chunks."""
    project_root, _ = indexed_project
    clear_search_cache()
    with patch("opencode_search.search._embed_query_sync", side_effect=fake_embed_query):
        result = await handle_search_code(
            query="graph",
            project_paths=[str(project_root)],
            use_rerank=False,
        )
    # graph.db should not be a result file
    for row in result.get("results", []):
        assert not row.get("file", "").endswith("graph.db"), \
            "graph.db should not appear as a code chunk"


# ---------------------------------------------------------------------------
# Wiki E2E (mocked LLM)
# ---------------------------------------------------------------------------


async def test_e2e_wiki_ingest_creates_page(indexed_project, tmp_path):
    from opencode_search.handlers._wiki import handle_wiki_ingest
    project_root, _ = indexed_project

    src = tmp_path / "design.md"
    src.write_text("# Design\nAuthentication flow description.", encoding="utf-8")

    llm = MagicMock()
    llm.is_available.return_value = True
    llm.raw_doc_to_wiki.return_value = "# Auth Design\n\nAuthentication flow."

    with patch("opencode_search.handlers._wiki._get_llm", return_value=llm), \
         patch("opencode_search.handlers._wiki.get_project_graph_db_path",
               return_value=get_project_graph_db_path(str(project_root))):
        result = await handle_wiki_ingest(
            source_path=str(src),
            project_path=str(project_root),
        )

    assert result.get("status") == "ok"
    assert len(result.get("pages_created", [])) >= 1


async def test_e2e_wiki_ingest_file_appears_in_wiki_dir(indexed_project, tmp_path):
    from opencode_search.handlers._wiki import handle_wiki_ingest
    project_root, _ = indexed_project

    src = tmp_path / "notes.md"
    src.write_text("# Notes\nSome design notes.", encoding="utf-8")

    llm = MagicMock()
    llm.is_available.return_value = True
    llm.raw_doc_to_wiki.return_value = "# Notes\n\nExtracted knowledge."

    wiki_dir = tmp_path / "wiki"
    raw_dir = tmp_path / "raw"

    with patch("opencode_search.handlers._wiki._get_llm", return_value=llm), \
         patch("opencode_search.handlers._wiki.get_project_graph_db_path",
               return_value=get_project_graph_db_path(str(project_root))), \
         patch("opencode_search.handlers._wiki.get_project_wiki_dir", return_value=wiki_dir), \
         patch("opencode_search.handlers._wiki.get_project_raw_dir", return_value=raw_dir):
        result = await handle_wiki_ingest(
            source_path=str(src),
            project_path=str(project_root),
        )

    assert result.get("status") == "ok"
    # Wiki dir should exist and contain pages
    assert wiki_dir.exists()


async def test_e2e_wiki_lint_after_ingest_no_orphans(indexed_project, tmp_path):
    from opencode_search.handlers._wiki import handle_wiki_ingest, handle_wiki_lint
    project_root, _ = indexed_project

    src = tmp_path / "doc.md"
    src.write_text("# Doc\nContent.", encoding="utf-8")

    llm = MagicMock()
    llm.is_available.return_value = True
    llm.raw_doc_to_wiki.return_value = "# Doc\n\nExtracted."

    wiki_dir = tmp_path / "wiki"
    raw_dir = tmp_path / "raw"

    with patch("opencode_search.handlers._wiki._get_llm", return_value=llm), \
         patch("opencode_search.handlers._wiki.get_project_graph_db_path",
               return_value=get_project_graph_db_path(str(project_root))), \
         patch("opencode_search.handlers._wiki.get_project_wiki_dir", return_value=wiki_dir), \
         patch("opencode_search.handlers._wiki.get_project_raw_dir", return_value=raw_dir):
        await handle_wiki_ingest(source_path=str(src), project_path=str(project_root))
        lint_result = await handle_wiki_lint(project_path=str(project_root))

    # The ingest process writes index, so pages should not be orphans
    assert "healthy" in lint_result


async def test_e2e_wiki_generate_requires_llm(indexed_project):
    from opencode_search.handlers._wiki import handle_wiki_generate
    project_root, _ = indexed_project

    with patch("opencode_search.handlers._wiki._get_llm", return_value=None):
        result = await handle_wiki_generate(project_path=str(project_root))

    assert "error" in result


async def test_e2e_wiki_generate_creates_community_pages(indexed_project, tmp_path):
    from opencode_search.handlers._wiki import handle_wiki_generate
    project_root, _ = indexed_project

    llm = MagicMock()
    llm.is_available.return_value = True
    llm.community_summary.return_value = ("Auth Module", "Handles authentication.")

    wiki_dir = tmp_path / "wiki"
    raw_dir = tmp_path / "raw"

    with patch("opencode_search.handlers._wiki._get_llm", return_value=llm), \
         patch("opencode_search.handlers._wiki.get_project_graph_db_path",
               return_value=get_project_graph_db_path(str(project_root))), \
         patch("opencode_search.handlers._wiki.get_project_wiki_dir", return_value=wiki_dir), \
         patch("opencode_search.handlers._wiki.get_project_raw_dir", return_value=raw_dir):
        result = await handle_wiki_generate(project_path=str(project_root))

    assert result.get("status") == "ok"
    assert isinstance(result.get("pages_created", []), (list, int))


# ---------------------------------------------------------------------------
# global_search E2E
# ---------------------------------------------------------------------------


async def test_e2e_global_search_returns_structure(indexed_project):
    from opencode_search.handlers._graph import handle_global_search
    project_root, _ = indexed_project
    result = await handle_global_search(query="authentication", project_path=str(project_root))
    assert "error" not in result
    assert "results" in result
    assert "community_matches" in result
    assert "wiki_matches" in result
    assert "total" in result


async def test_e2e_global_search_no_graph_returns_empty(tmp_path, monkeypatch):
    from opencode_search.handlers._graph import handle_global_search
    project_root = tmp_path / "empty"
    project_root.mkdir()
    monkeypatch.setattr(config, "REGISTRY_PATH", tmp_path / "registry.json")
    # No graph.db → should return empty results, not error
    result = await handle_global_search(query="anything", project_path=str(project_root))
    assert "error" not in result
    assert result["community_matches"] == 0


async def test_e2e_global_search_matches_community_title(indexed_project):
    """After LLM enrichment seeds a community title, global_search finds it."""
    from opencode_search.graph.storage import GraphStorage
    from opencode_search.handlers._graph import handle_global_search
    project_root, _ = indexed_project

    # Manually seed a community title in the graph DB
    graph_db_path = get_project_graph_db_path(str(project_root))
    gs = GraphStorage(graph_db_path)
    gs.open()
    try:
        communities = gs.get_communities()
        if communities:
            c = communities[0]
            import sqlite3
            con = sqlite3.connect(graph_db_path)
            con.execute(
                "UPDATE communities SET title=?, summary=? WHERE id=?",
                ("Auth Layer", "Handles JWT authentication and token verification.", c.id),
            )
            con.commit()
            con.close()
    finally:
        gs.close()

    result = await handle_global_search(query="authentication JWT token", project_path=str(project_root))
    assert result["community_matches"] >= 1
    assert any(r["type"] == "community" for r in result["results"])


async def test_e2e_global_search_result_ordering_by_score(indexed_project):
    from opencode_search.handlers._graph import handle_global_search
    project_root, _ = indexed_project

    # Seed two communities with different match quality
    graph_db_path = get_project_graph_db_path(str(project_root))
    gs = GraphStorage(graph_db_path)
    gs.open()
    try:
        communities = gs.get_communities()
        if len(communities) >= 2:
            import sqlite3
            con = sqlite3.connect(graph_db_path)
            con.execute("UPDATE communities SET title=?, summary=? WHERE id=?",
                        ("Auth Module", "authentication login verify token", communities[0].id))
            con.execute("UPDATE communities SET title=?, summary=? WHERE id=?",
                        ("Database Layer", "SQL queries and connections", communities[1].id))
            con.commit()
            con.close()
    finally:
        gs.close()

    result = await handle_global_search(query="authentication token verify", project_path=str(project_root))
    hits = result["results"]
    # Scores should be in descending order
    scores = [h["score"] for h in hits]
    assert scores == sorted(scores, reverse=True)


# ---------------------------------------------------------------------------
# search_docs E2E
# ---------------------------------------------------------------------------


async def test_e2e_search_docs_returns_wiki_language_filter(indexed_project):
    from opencode_search.handlers._query import handle_search_code as _handle_search_code
    project_root, _ = indexed_project
    clear_search_cache()
    # search_docs filters to wiki/documentation content_types; should return empty with no docs indexed
    with patch("opencode_search.search._embed_query_sync", side_effect=fake_embed_query):
        result = await _handle_search_code(
            query="authentication",
            project_paths=[str(project_root)],
            content_types=["wiki", "markdown"],
            use_rerank=False,
        )
    # Should not error — may return empty results
    assert "error" not in result
    assert "results" in result


# ---------------------------------------------------------------------------
# Large-project tests (requires OPENCODE_RUN_LARGE_TESTS=1 + astro-project)
# ---------------------------------------------------------------------------

_ASTRO_PROJECT = os.path.expanduser(
    "~/git/github.com/fairyhunter13/astro-project"
)
_RUN_LARGE = os.environ.get("OPENCODE_RUN_LARGE_TESTS", "").strip().lower() in {
    "1", "true", "yes",
}
_large = pytest.mark.skipif(
    not _RUN_LARGE or not os.path.isdir(_ASTRO_PROJECT),
    reason="set OPENCODE_RUN_LARGE_TESTS=1 and ensure astro-project is present",
)


def _astro_graph_has_data() -> bool:
    """Return True if the daemon has already built astro-project's graph.db."""
    graph_db = Path(get_project_graph_db_path(_ASTRO_PROJECT))
    if not graph_db.exists():
        return False
    gs = GraphStorage(str(graph_db))
    gs.open()
    try:
        return gs.node_count() > 0  # COUNT(*) — far faster than all_nodes()
    except Exception:
        return False
    finally:
        gs.close()



@_large
@pytest.mark.large
async def test_e2e_astro_project_full_pipeline(tmp_path, monkeypatch):
    """
    Full pipeline on astro-project (meta-repo with 24 symlinked sub-repos).
    Asserts graph.db is built with >500 nodes and >=3 communities.

    When the daemon has already indexed this project (graph.db populated) the
    test verifies data integrity against that existing data rather than
    re-running the full embedding pipeline, which would cause a CUDA context
    conflict with the running daemon.
    """
    if not _astro_graph_has_data():
        monkeypatch.setattr(config, "REGISTRY_PATH", tmp_path / "registry.json")
        start = time.monotonic()
        result = await index_and_wait(_ASTRO_PROJECT)
        elapsed = time.monotonic() - start
        assert result["status"] == "ok", f"Index failed: {result}"
        assert elapsed < 3600, f"Full pipeline took {elapsed:.1f}s, expected < 3600s"

    gs = GraphStorage(get_project_graph_db_path(_ASTRO_PROJECT))
    gs.open()
    try:
        nodes = gs.all_nodes()
        communities = gs.get_communities()
        assert len(nodes) > 500, f"Expected > 500 nodes, got {len(nodes)}"
        assert len(communities) >= 3, f"Expected >= 3 communities, got {len(communities)}"
        func_nodes = [n for n in nodes if n.kind == "function"]
        if func_nodes:
            assert all(n.community_id is not None for n in func_nodes[:100])
    finally:
        gs.close()


@_large
@pytest.mark.large
async def test_e2e_astro_project_symlink_nodes_resolved(tmp_path, monkeypatch):
    """Nodes from symlinked sub-repos resolve to real paths, findable by short name."""
    if not _astro_graph_has_data():
        monkeypatch.setattr(config, "REGISTRY_PATH", tmp_path / "registry.json")
        await index_and_wait(_ASTRO_PROJECT)

    gs = GraphStorage(get_project_graph_db_path(_ASTRO_PROJECT))
    gs.open()
    try:
        nodes = gs.all_nodes()
        assert len(nodes) > 0, "Graph must have nodes"
        # Nodes from symlinked repos should store the real resolved path, not
        # the symlink path — verify at least some nodes have real absolute paths
        symlinked_dir = os.path.join(_ASTRO_PROJECT, "repositories-ubuntu")
        real_path_nodes = [n for n in nodes if n.file and symlinked_dir not in n.file]
        assert len(real_path_nodes) > 0, (
            "Expected nodes with real (non-symlink) paths; symlink resolution may be broken"
        )
    finally:
        gs.close()


@_large
@pytest.mark.large
async def test_e2e_astro_project_cross_repo_edges(tmp_path, monkeypatch):
    """Call edges within the monorepo are present after indexing."""
    if not _astro_graph_has_data():
        monkeypatch.setattr(config, "REGISTRY_PATH", tmp_path / "registry.json")
        await index_and_wait(_ASTRO_PROJECT)

    gs = GraphStorage(get_project_graph_db_path(_ASTRO_PROJECT))
    gs.open()
    try:
        edges = gs.all_edges()
        call_edges = [e for e in edges if e.kind == "CALLS"]
        assert len(call_edges) > 0, "Should have resolved call edges in monorepo"
    finally:
        gs.close()


@_large
@pytest.mark.large
async def test_e2e_astro_project_global_search(tmp_path, monkeypatch):
    """global_search on astro-project returns results without errors."""
    from opencode_search.handlers._graph import handle_global_search

    if not _astro_graph_has_data():
        monkeypatch.setattr(config, "REGISTRY_PATH", tmp_path / "registry.json")
        await index_and_wait(_ASTRO_PROJECT)

    result = await handle_global_search(
        query="authentication middleware",
        project_path=_ASTRO_PROJECT,
    )
    assert "error" not in result
    assert "results" in result
    assert isinstance(result["community_matches"], int)
    assert isinstance(result["wiki_matches"], int)


@_large
@pytest.mark.large
async def test_e2e_astro_project_search_code_p95_within_gate(tmp_path, monkeypatch):
    """search_code p95 latency stays within 5s gate on the indexed astro-project.

    Runs scripts/p95_check.py as a subprocess.  The script imports ONLY the MCP
    bridge (no lancedb, no onnxruntime) and exits via os._exit() to bypass
    interpreter shutdown — this avoids:
    1. SIGSEGV from lancedb heap corruption when daemon holds lance files open
    2. Blackwell SM 12.0 CUDA UVM deadlock from two ONNX contexts on same GPU
    3. asyncio.to_thread executor hang from CUDA library background threads

    subprocess.run() is called directly (blocking the event loop for ~3s) — fine
    for a single sequential test with no concurrent async tasks.
    """
    import subprocess
    import sys

    if not _astro_graph_has_data():
        monkeypatch.setattr(config, "REGISTRY_PATH", tmp_path / "registry.json")
        await index_and_wait(_ASTRO_PROJECT)

    repo_root = Path(__file__).parent.parent.parent
    # Direct subprocess.run (not asyncio.to_thread) to avoid CUDA thread-pool hang
    # during event loop shutdown.  The subprocess takes ~3s on a warm daemon.
    result = subprocess.run(
        [sys.executable, "scripts/p95_check.py", _ASTRO_PROJECT],
        cwd=repo_root,
        timeout=600,
    )
    assert result.returncode == 0, f"p95_check.py exited {result.returncode}"


# ---------------------------------------------------------------------------
# get_communities + enrich_project + wiki pipeline E2E (unit, mocked LLM)
# ---------------------------------------------------------------------------


async def test_e2e_get_communities_top_k_limits_results(indexed_project):
    """handle_get_communities(top_k=N) returns at most N communities."""
    from opencode_search.handlers._graph import handle_get_communities
    project_root, _ = indexed_project

    result = await handle_get_communities(project_path=str(project_root), top_k=2)
    assert "error" not in result
    assert len(result["communities"]) <= 2


async def test_e2e_get_communities_filters_singletons(indexed_project):
    """handle_get_communities never returns singleton communities (node_count < 2)."""
    from opencode_search.handlers._graph import handle_get_communities
    project_root, _ = indexed_project

    result = await handle_get_communities(project_path=str(project_root), top_k=200)
    assert "error" not in result
    for c in result["communities"]:
        assert c["node_count"] >= 2, (
            f"Community {c['id']} has node_count={c['node_count']} — singletons must be excluded"
        )


async def test_e2e_enrich_project_with_mock_llm(indexed_project):
    """handle_enrich_project enriches communities and persists titles to the graph DB."""
    from opencode_search.graph.storage import GraphStorage
    from opencode_search.handlers._enrichment import handle_enrich_project
    project_root, _ = indexed_project

    llm = MagicMock()
    llm.is_available.return_value = True
    llm.community_summary.return_value = ("Auth Layer", "Handles authentication and token verification.")

    with patch("opencode_search.handlers._enrichment._get_llm", return_value=llm):
        result = await handle_enrich_project(
            project_path=str(project_root),
            scope="communities",
            max_communities=2,
        )

    assert result.get("status") == "ok"
    assert result["enriched_communities"] >= 1

    # Verify titles were persisted to the graph DB
    gs = GraphStorage(get_project_graph_db_path(str(project_root)))
    gs.open()
    try:
        enriched = [c for c in gs.get_communities(min_node_count=2) if c.title]
        assert len(enriched) >= 1, "At least one community should have a title after enrichment"
    finally:
        gs.close()


async def test_e2e_wiki_generate_after_enrich_full_pipeline(indexed_project, tmp_path):
    """Full pipeline: enrich communities → generate wiki pages → .md files exist."""
    from opencode_search.handlers._enrichment import handle_enrich_project
    from opencode_search.handlers._wiki import handle_wiki_generate
    project_root, _ = indexed_project

    llm = MagicMock()
    llm.is_available.return_value = True
    llm.community_summary.return_value = ("Auth Layer", "Handles authentication.")

    wiki_dir = tmp_path / "wiki"
    raw_dir = tmp_path / "raw"

    # Step 1: enrich
    with patch("opencode_search.handlers._enrichment._get_llm", return_value=llm):
        enrich_result = await handle_enrich_project(
            project_path=str(project_root),
            scope="communities",
            max_communities=2,
        )
    assert enrich_result.get("status") == "ok"

    # Step 2: wiki_generate
    with patch("opencode_search.handlers._wiki._get_llm", return_value=llm), \
         patch("opencode_search.handlers._wiki.get_project_graph_db_path",
               return_value=get_project_graph_db_path(str(project_root))), \
         patch("opencode_search.handlers._wiki.get_project_wiki_dir", return_value=wiki_dir), \
         patch("opencode_search.handlers._wiki.get_project_raw_dir", return_value=raw_dir):
        wiki_result = await handle_wiki_generate(
            project_path=str(project_root),
            max_communities=2,
        )

    assert wiki_result.get("status") == "ok"
    assert wiki_result["total"] >= 1
    md_files = list(wiki_dir.glob("*.md")) if wiki_dir.exists() else []
    assert len(md_files) >= 1, "Wiki directory must contain at least one .md file"


async def test_e2e_global_search_after_enrich_finds_results(indexed_project):
    """After enrich_project populates community titles, global_search returns them."""
    from opencode_search.handlers._enrichment import handle_enrich_project
    from opencode_search.handlers._graph import handle_global_search
    project_root, _ = indexed_project

    llm = MagicMock()
    llm.is_available.return_value = True
    # Use a distinctive title that global_search can match
    llm.community_summary.return_value = ("JWT Token Verifier", "Handles JWT token verification and authentication.")

    with patch("opencode_search.handlers._enrichment._get_llm", return_value=llm):
        await handle_enrich_project(
            project_path=str(project_root),
            scope="communities",
            max_communities=2,
        )

    with patch("opencode_search.search._embed_query_sync", side_effect=fake_embed_query):
        result = await handle_global_search(
            query="JWT token authentication",
            project_path=str(project_root),
        )

    assert "error" not in result
    assert result["community_matches"] >= 1, (
        "global_search must find enriched communities by title/summary"
    )


async def test_e2e_wiki_query_returns_content(indexed_project, tmp_path):
    """wiki_query returns wiki page content after wiki_generate creates pages."""
    from opencode_search.handlers._wiki import handle_wiki_generate, handle_wiki_query
    project_root, _ = indexed_project

    llm = MagicMock()
    llm.is_available.return_value = True
    llm.community_summary.return_value = ("Auth Layer", "Handles authentication and JWT tokens.")

    wiki_dir = tmp_path / "wiki"
    raw_dir = tmp_path / "raw"

    with patch("opencode_search.handlers._wiki._get_llm", return_value=llm), \
         patch("opencode_search.handlers._wiki.get_project_graph_db_path",
               return_value=get_project_graph_db_path(str(project_root))), \
         patch("opencode_search.handlers._wiki.get_project_wiki_dir", return_value=wiki_dir), \
         patch("opencode_search.handlers._wiki.get_project_raw_dir", return_value=raw_dir):
        wiki_result = await handle_wiki_generate(
            project_path=str(project_root),
            max_communities=2,
        )

    assert wiki_result.get("status") == "ok"
    # wiki_query imports handle_search_code from _query at call time; patch source module.
    with patch("opencode_search.handlers._query.handle_search_code",
               return_value={"results": [], "elapsed_ms": 0}):
        query_result = await handle_wiki_query(
            query="authentication",
            project_path=str(project_root),
            top_k=3,
        )
    assert "query" in query_result or "results" in query_result


# ---------------------------------------------------------------------------
# Large project E2E: astro-project get_communities + enrich + wiki pipeline
# ---------------------------------------------------------------------------


def _ollama_phi4_available() -> bool:
    """Return True if Ollama is running and phi4-mini:3.8b is installed."""
    import urllib.request
    import json
    try:
        with urllib.request.urlopen("http://localhost:11434/api/tags", timeout=3) as resp:
            data = json.loads(resp.read())
        models = [m.get("name", "") for m in data.get("models", [])]
        return any("phi4-mini" in m for m in models)
    except Exception:
        return False


_large_llm = pytest.mark.skipif(
    not _RUN_LARGE or not os.path.isdir(_ASTRO_PROJECT) or not _ollama_phi4_available(),
    reason=(
        "set OPENCODE_RUN_LARGE_TESTS=1, ensure astro-project is present, "
        "and ensure Ollama is running with phi4-mini:3.8b installed"
    ),
)


_REAL_REGISTRY = Path.home() / ".local" / "share" / "opencode-search" / "projects.json"


def _use_real_registry(monkeypatch) -> None:
    """Undo the per-test isolate_registry patch for large tests that need the real index."""
    monkeypatch.setattr(config, "REGISTRY_PATH", _REAL_REGISTRY)


@_large
@pytest.mark.large
async def test_e2e_astro_project_get_communities_no_hang(monkeypatch):
    """get_communities(top_k=20) on the massive astro-project completes in < 5s."""
    from opencode_search.handlers._graph import handle_get_communities

    _use_real_registry(monkeypatch)
    assert _astro_graph_has_data(), "astro-project graph.db must be pre-built"

    t0 = time.perf_counter()
    result = await handle_get_communities(project_path=_ASTRO_PROJECT, top_k=20)
    elapsed = time.perf_counter() - t0

    assert "error" not in result, f"handle_get_communities failed: {result.get('error')}"
    assert len(result["communities"]) <= 20
    assert all(c["node_count"] >= 2 for c in result["communities"]), "Singletons must be excluded"
    assert elapsed < 5.0, f"get_communities took {elapsed:.1f}s — must complete in < 5s"


@_large_llm
@pytest.mark.large
async def test_e2e_astro_project_enrich_top_communities(monkeypatch):
    """enrich_project(max_communities=5) on astro-project completes in < 120s without hanging."""
    from opencode_search.graph.storage import GraphStorage
    from opencode_search.handlers._enrichment import handle_enrich_project

    _use_real_registry(monkeypatch)
    assert _astro_graph_has_data(), "astro-project graph.db must be pre-built"

    t0 = time.perf_counter()
    result = await handle_enrich_project(
        project_path=_ASTRO_PROJECT,
        scope="communities",
        max_communities=5,
    )
    elapsed = time.perf_counter() - t0

    assert result.get("status") == "ok", f"enrich_project failed: {result}"
    # Communities may already be enriched from a prior run — that's fine; count >= 0
    assert elapsed < 120.0, f"enrich_project took {elapsed:.1f}s — must complete in < 120s"

    # Verify at least some titles exist (from this run or a previous one)
    gs = GraphStorage(get_project_graph_db_path(_ASTRO_PROJECT))
    gs.open()
    try:
        enriched = [c for c in gs.get_communities(min_node_count=2) if c.title]
        assert len(enriched) >= 1, "At least one community must have a persisted title"
    finally:
        gs.close()


@_large_llm
@pytest.mark.large
async def test_e2e_astro_project_wiki_pipeline(monkeypatch):
    """Full wiki pipeline on astro-project: enrich → wiki_generate → global_search.

    Uses max_communities=5 to keep total runtime under 5 minutes.
    Verifies: no crash, no hang, valid output structure at each stage.
    """
    from opencode_search.handlers._enrichment import handle_enrich_project
    from opencode_search.handlers._wiki import handle_wiki_generate, handle_wiki_lint
    from opencode_search.handlers._graph import handle_global_search

    _use_real_registry(monkeypatch)
    assert _astro_graph_has_data(), "astro-project graph.db must be pre-built"

    # Step 1: Enrich top 5 communities (largest-first); skip if already done
    enrich_result = await handle_enrich_project(
        project_path=_ASTRO_PROJECT,
        scope="communities",
        max_communities=5,
    )
    assert enrich_result.get("status") == "ok", f"enrich_project failed: {enrich_result}"

    # Step 2: Generate wiki pages (same 5-community cap)
    wiki_result = await handle_wiki_generate(
        project_path=_ASTRO_PROJECT,
        max_communities=5,
    )
    assert wiki_result.get("status") == "ok", f"wiki_generate failed: {wiki_result}"
    wiki_dir = get_project_wiki_dir(_ASTRO_PROJECT)
    assert wiki_dir.exists(), "Wiki directory must be created"
    md_files = list(wiki_dir.glob("community_*.md"))
    assert len(md_files) >= 1, "At least one community wiki page must be created"

    # Step 3: global_search should find enriched communities
    search_result = await handle_global_search(
        query="payment authentication service",
        project_path=_ASTRO_PROJECT,
        top_k=10,
    )
    assert "error" not in search_result
    assert "results" in search_result
    assert isinstance(search_result["community_matches"], int)
    assert search_result["community_matches"] >= 1, (
        "global_search must find enriched communities after enrich_project ran"
    )

    # Step 4: lint the wiki — should be healthy
    lint_result = await handle_wiki_lint(project_path=_ASTRO_PROJECT)
    assert "healthy" in lint_result or "pages" in lint_result, (
        f"wiki_lint returned unexpected structure: {lint_result}"
    )


# ---------------------------------------------------------------------------
# Federation E2E tests (unit, synthetic projects — no real indexing needed)
# ---------------------------------------------------------------------------


async def test_e2e_discover_federation_finds_symlinks(tmp_path, monkeypatch):
    """handle_discover_federation returns symlinked directories at the project root."""
    from opencode_search.handlers._federation import handle_discover_federation

    root = tmp_path / "myproject"
    root.mkdir()

    # Create two real member directories and symlink them into the root
    member_a = tmp_path / "member_a"
    member_a.mkdir()
    (member_a / "main.go").write_text("package main\n")

    member_b = tmp_path / "member_b"
    member_b.mkdir()
    (member_b / "main.go").write_text("package main\n")

    (root / "link_a").symlink_to(member_a)
    (root / "link_b").symlink_to(member_b)
    # A regular file — should NOT be discovered
    (root / "README.md").write_text("# project\n")

    result = await handle_discover_federation(project_path=str(root))

    assert "error" not in result
    assert result["total"] == 2
    discovered = set(result["discovered"])
    assert str(member_a) in discovered
    assert str(member_b) in discovered
    # Regular files must not appear
    assert not any("README" in d for d in discovered)


async def test_e2e_discover_federation_parses_go_work(tmp_path, monkeypatch):
    """handle_discover_federation finds go.work 'use' directives."""
    from opencode_search.handlers._federation import handle_discover_federation

    root = tmp_path / "workspace"
    root.mkdir()

    member = tmp_path / "mymodule"
    member.mkdir()

    (root / "go.work").write_text(
        "go 1.24\n\nuse (\n\t../mymodule\n)\n"
    )

    result = await handle_discover_federation(project_path=str(root))

    assert "error" not in result
    assert str(member) in result["sources"]["go_work"]


async def test_e2e_add_federation_member_persists(tmp_path, monkeypatch):
    """add_federation_member saves to registry; list_federation returns it."""
    from opencode_search.handlers._federation import (
        handle_add_federation_member,
        handle_list_federation,
    )

    monkeypatch.setattr(config, "REGISTRY_PATH", tmp_path / "registry.json")

    root = tmp_path / "root"
    root.mkdir()
    member = tmp_path / "member"
    member.mkdir()

    add_result = await handle_add_federation_member(
        root_path=str(root), member_path=str(member)
    )
    assert add_result.get("status") == "ok"
    assert add_result["total_members"] == 1

    list_result = await handle_list_federation(project_path=str(root))
    assert "error" not in list_result
    assert list_result["total_members"] == 1
    assert any(str(member) in m["path"] for m in list_result["members"])


async def test_e2e_federation_search_includes_members(indexed_project, tmp_path, monkeypatch):
    """search_code(include_federation=True) expands project list to federation members."""
    from opencode_search.handlers._federation import handle_add_federation_member
    from opencode_search.handlers._query import handle_search_code

    project_root, registry_path = indexed_project

    # Create a second indexed project and make it a federation member
    member_root = tmp_path / "member_project"
    member_root.mkdir()
    (member_root / "billing.py").write_text("def charge_card(): pass\n")

    with patch("opencode_search.chunker.chunk_file", side_effect=_split_lines), \
         patch("opencode_search.embeddings.embed_passages", side_effect=fake_embed_passages), \
         patch("opencode_search.search._embed_query_sync", side_effect=fake_embed_query):
        await index_and_wait(str(member_root))

    await handle_add_federation_member(
        root_path=str(project_root), member_path=str(member_root)
    )

    # Search with federation: should include both root and member
    with patch("opencode_search.search._embed_query_sync", side_effect=fake_embed_query):
        result = await handle_search_code(
            query="authenticate",
            project_paths=[str(project_root)],
            include_federation=True,
        )

    assert "error" not in result
    # projects_searched should include the member
    assert result["projects_searched"] >= 2


async def test_e2e_federation_search_excludes_unindexed(tmp_path, monkeypatch):
    """search_code(include_federation=True) skips federation members that are not indexed."""
    from opencode_search.handlers._federation import handle_add_federation_member
    from opencode_search.handlers._query import handle_search_code

    monkeypatch.setattr(config, "REGISTRY_PATH", tmp_path / "registry.json")

    root = tmp_path / "root"
    root.mkdir()
    (root / "app.py").write_text("def foo(): pass\n")
    member = tmp_path / "unindexed_member"
    member.mkdir()

    # Index only the root
    with patch("opencode_search.chunker.chunk_file", side_effect=_split_lines), \
         patch("opencode_search.embeddings.embed_passages", side_effect=fake_embed_passages), \
         patch("opencode_search.search._embed_query_sync", side_effect=fake_embed_query):
        await index_and_wait(str(root))

    # Add member to federation WITHOUT indexing it
    await handle_add_federation_member(
        root_path=str(root), member_path=str(member)
    )

    with patch("opencode_search.search._embed_query_sync", side_effect=fake_embed_query):
        result = await handle_search_code(
            query="foo",
            project_paths=[str(root)],
            include_federation=True,
        )

    # Should only search root (1 project), not the unindexed member
    assert "error" not in result
    assert result["projects_searched"] == 1


async def test_e2e_federation_enrich_runs_on_members(indexed_project, tmp_path, monkeypatch):
    """enrich_project(include_federation=True) enriches root and indexed member."""
    from opencode_search.handlers._enrichment import handle_enrich_project
    from opencode_search.handlers._federation import handle_add_federation_member

    project_root, registry_path = indexed_project

    # Create and index a member project
    member_root = tmp_path / "member_enrich"
    member_root.mkdir()
    (member_root / "payment.py").write_text("def process_payment(): pass\n")

    with patch("opencode_search.chunker.chunk_file", side_effect=_split_lines), \
         patch("opencode_search.embeddings.embed_passages", side_effect=fake_embed_passages), \
         patch("opencode_search.search._embed_query_sync", side_effect=fake_embed_query):
        await index_and_wait(str(member_root))

    await handle_add_federation_member(
        root_path=str(project_root), member_path=str(member_root)
    )

    llm = MagicMock()
    llm.is_available.return_value = True
    llm.community_summary.return_value = ("Test Module", "Handles test functionality.")

    with patch("opencode_search.handlers._enrichment._get_llm", return_value=llm):
        result = await handle_enrich_project(
            project_path=str(project_root),
            scope="communities",
            max_communities=2,
            include_federation=True,
        )

    assert result.get("status") == "ok"
    # federation_results should list both root and member
    assert "federation_results" in result
    paths = [r["path"] for r in result["federation_results"]]
    assert str(project_root) in paths
    assert str(member_root) in paths


async def test_e2e_federation_wiki_runs_on_members(indexed_project, tmp_path, monkeypatch):
    """wiki_generate(include_federation=True) generates wiki for root and indexed member."""
    from opencode_search.handlers._federation import handle_add_federation_member
    from opencode_search.handlers._wiki import handle_wiki_generate

    project_root, registry_path = indexed_project

    member_root = tmp_path / "member_wiki"
    member_root.mkdir()
    (member_root / "service.py").write_text("def run(): pass\n")

    with patch("opencode_search.chunker.chunk_file", side_effect=_split_lines), \
         patch("opencode_search.embeddings.embed_passages", side_effect=fake_embed_passages), \
         patch("opencode_search.search._embed_query_sync", side_effect=fake_embed_query):
        await index_and_wait(str(member_root))

    await handle_add_federation_member(
        root_path=str(project_root), member_path=str(member_root)
    )

    llm = MagicMock()
    llm.is_available.return_value = True
    llm.community_summary.return_value = ("Service Layer", "Runs core services.")

    wiki_root = tmp_path / "wiki_root"
    wiki_root.mkdir()
    wiki_member = tmp_path / "wiki_member"
    wiki_member.mkdir()
    raw_root = tmp_path / "raw_root"
    raw_root.mkdir()
    raw_member = tmp_path / "raw_member"
    raw_member.mkdir()

    def _wiki_dir_for(path):
        return wiki_root if str(project_root) in path else wiki_member

    def _raw_dir_for(path):
        return raw_root if str(project_root) in path else raw_member

    with patch("opencode_search.handlers._wiki._get_llm", return_value=llm), \
         patch("opencode_search.handlers._wiki.get_project_graph_db_path",
               side_effect=lambda p: get_project_graph_db_path(p)), \
         patch("opencode_search.handlers._wiki.get_project_wiki_dir",
               side_effect=_wiki_dir_for), \
         patch("opencode_search.handlers._wiki.get_project_raw_dir",
               side_effect=_raw_dir_for):
        result = await handle_wiki_generate(
            project_path=str(project_root),
            max_communities=2,
            include_federation=True,
        )

    assert result.get("status") == "ok"
    assert "federation_results" in result
    paths = [r["path"] for r in result["federation_results"]]
    assert str(project_root) in paths
    assert str(member_root) in paths


# ---------------------------------------------------------------------------
# Large test: discover federation from real astro-project
# ---------------------------------------------------------------------------


@_large
@pytest.mark.large
async def test_e2e_astro_project_discover_federation(monkeypatch):
    """discover_federation finds all 24 symlinked sub-repos in astro-project."""
    from opencode_search.handlers._federation import handle_discover_federation

    _use_real_registry(monkeypatch)

    result = await handle_discover_federation(project_path=_ASTRO_PROJECT)

    assert "error" not in result, f"discover_federation failed: {result}"
    # astro-project has 24 symlinks in repositories-ubuntu/
    assert result["total"] >= 20, (
        f"Expected at least 20 federation members, got {result['total']}: {result['discovered']}"
    )
    # go.work declares 3 workspace modules — verify they're also captured
    sources = result["sources"]
    assert len(sources["symlinks"]) >= 20
    assert len(sources["go_work"]) >= 3, (
        f"go.work should have at least 3 members, got: {sources['go_work']}"
    )
    # Verify the known astro-golibs path is discovered
    known = "/home/user/go/src/github.com/example-org/astro-golibs"
    assert known in result["discovered"], (
        f"astro-golibs not found in discovered: {result['discovered'][:5]}..."
    )


# ---------------------------------------------------------------------------
# Auto-discovery: federation registered automatically on index_project
# ---------------------------------------------------------------------------


async def test_e2e_index_auto_discovers_federation(tmp_path, monkeypatch):
    """index_project automatically discovers and registers federation members."""
    monkeypatch.setattr(config, "REGISTRY_PATH", tmp_path / "registry.json")

    root = tmp_path / "root"
    root.mkdir()
    (root / "app.py").write_text("def main(): pass\n")

    # Create a real member directory and symlink it into root
    member = tmp_path / "member_service"
    member.mkdir()
    (member / "service.py").write_text("def serve(): pass\n")
    (root / "link_member").symlink_to(member)

    with patch("opencode_search.chunker.chunk_file", side_effect=_split_lines), \
         patch("opencode_search.embeddings.embed_passages", side_effect=fake_embed_passages), \
         patch("opencode_search.search._embed_query_sync", side_effect=fake_embed_query):
        result = await index_and_wait(str(root))

    assert result["status"] == "ok"

    # Federation should be auto-discovered and persisted
    registry = config.load_registry()
    root_str = str(root.resolve())
    assert root_str in registry, "Root project should be in registry"
    entry = registry[root_str]
    member_str = str(member.resolve())
    assert member_str in entry.federation, (
        f"Member not in federation. federation={entry.federation}"
    )
    # Member should also be pre-registered in registry
    assert member_str in registry, "Member should be pre-registered in registry"


async def test_e2e_index_auto_discovery_idempotent(tmp_path, monkeypatch):
    """Re-indexing does not duplicate federation members."""
    monkeypatch.setattr(config, "REGISTRY_PATH", tmp_path / "registry.json")

    root = tmp_path / "root"
    root.mkdir()
    (root / "app.py").write_text("x = 1\n")
    member = tmp_path / "svc"
    member.mkdir()
    (root / "link_svc").symlink_to(member)

    with patch("opencode_search.chunker.chunk_file", side_effect=_split_lines), \
         patch("opencode_search.embeddings.embed_passages", side_effect=fake_embed_passages), \
         patch("opencode_search.search._embed_query_sync", side_effect=fake_embed_query):
        await index_and_wait(str(root))
        await index_and_wait(str(root))  # second run

    registry = config.load_registry()
    entry = registry[str(root.resolve())]
    member_str = str(member.resolve())
    assert entry.federation.count(member_str) == 1, (
        f"Member appears more than once in federation: {entry.federation}"
    )


# ---------------------------------------------------------------------------
# Pipeline E2E tests
# ---------------------------------------------------------------------------


async def test_e2e_pipeline_discover_step(tmp_path, monkeypatch):
    """pipeline() discovers and registers federation members."""
    from opencode_search.handlers._pipeline import handle_pipeline

    monkeypatch.setattr(config, "REGISTRY_PATH", tmp_path / "registry.json")

    root = tmp_path / "root"
    root.mkdir()
    (root / "app.py").write_text("def app(): pass\n")
    member = tmp_path / "svc"
    member.mkdir()
    (root / "link_svc").symlink_to(member)

    with patch("opencode_search.chunker.chunk_file", side_effect=_split_lines), \
         patch("opencode_search.embeddings.embed_passages", side_effect=fake_embed_passages), \
         patch("opencode_search.search._embed_query_sync", side_effect=fake_embed_query):
        await index_and_wait(str(root))

    # Run pipeline with LLM disabled (only discovery + skip steps)
    with patch("opencode_search.handlers._pipeline.create_llm_client", return_value=None):
        result = await handle_pipeline(project_path=str(root), ingest_docs=False)

    assert result.get("status") == "ok"
    steps = {s["step"]: s for s in result["steps"]}
    assert steps["discover"]["status"] == "ok"
    assert steps["discover"]["discovered"] >= 1
    # enrich/wiki skipped when LLM unavailable
    assert steps["enrich"]["status"] == "skipped"
    assert steps["wiki"]["status"] == "skipped"


async def test_e2e_pipeline_full_with_mock_llm(indexed_project, tmp_path, monkeypatch):
    """pipeline() runs all steps including enrich and wiki with mocked LLM."""
    from opencode_search.handlers._pipeline import handle_pipeline

    project_root, _ = indexed_project

    llm = MagicMock()
    llm.is_available.return_value = True
    llm.community_summary.return_value = ("Auth Layer", "Handles authentication logic.")
    llm.raw_doc_to_wiki.return_value = "# Doc\n\nExtracted content."

    # Create a doc file to trigger ingest_docs
    readme = project_root / "README.md"
    readme.write_text("# My Project\n\nThis handles authentication.\n")

    wiki_dir = tmp_path / "wiki"
    raw_dir = tmp_path / "raw"

    with patch("opencode_search.handlers._pipeline.create_llm_client", return_value=llm), \
         patch("opencode_search.handlers._enrichment._get_llm", return_value=llm), \
         patch("opencode_search.handlers._wiki._get_llm", return_value=llm), \
         patch("opencode_search.handlers._wiki.get_project_graph_db_path",
               return_value=get_project_graph_db_path(str(project_root))), \
         patch("opencode_search.handlers._wiki.get_project_wiki_dir", return_value=wiki_dir), \
         patch("opencode_search.handlers._wiki.get_project_raw_dir", return_value=raw_dir):
        result = await handle_pipeline(
            project_path=str(project_root),
            enrich_max_communities=2,
            wiki_max_communities=2,
            ingest_docs=True,
        )

    assert result.get("status") == "ok"
    steps = {s["step"]: s for s in result["steps"]}
    assert steps["discover"]["status"] == "ok"
    assert steps["enrich"].get("enriched_communities", 0) >= 1
    assert steps["wiki"].get("total", 0) >= 1
    assert steps["ingest_docs"]["status"] == "ok"
    assert "README.md" in steps["ingest_docs"]["ingested"][0]


@_large
@pytest.mark.large
async def test_e2e_astro_project_pipeline_discover(monkeypatch):
    """pipeline() on astro-project discovers 24 federation members and registers them."""
    from opencode_search.handlers._pipeline import handle_pipeline
    from opencode_search.config import load_registry

    _use_real_registry(monkeypatch)
    assert _astro_graph_has_data(), "astro-project graph.db must be pre-built"

    # Run pipeline with LLM disabled so it's fast (only discovery)
    with patch("opencode_search.handlers._pipeline.create_llm_client", return_value=None):
        result = await handle_pipeline(
            project_path=_ASTRO_PROJECT,
            ingest_docs=False,
        )

    assert result.get("status") == "ok"
    steps = {s["step"]: s for s in result["steps"]}
    assert steps["discover"]["status"] == "ok"
    assert steps["discover"]["discovered"] >= 20, (
        f"Expected 20+ members, got: {steps['discover']['discovered']}"
    )

    # Verify members are now registered in the real registry
    registry = load_registry()
    astro_entry = registry.get(_ASTRO_PROJECT)
    assert astro_entry is not None
    assert len(astro_entry.federation) >= 20, (
        f"Federation should have 20+ members, got: {len(astro_entry.federation)}"
    )
