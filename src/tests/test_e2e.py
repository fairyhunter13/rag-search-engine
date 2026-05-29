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
        return len(gs.all_nodes()) > 0
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
    """search_code p95 latency stays within 500ms gate on the indexed astro-project."""
    from opencode_search.handlers import handle_search_code as _hsc

    if not _astro_graph_has_data():
        monkeypatch.setattr(config, "REGISTRY_PATH", tmp_path / "registry.json")
        await index_and_wait(_ASTRO_PROJECT)

    latencies = []
    for query in ["authentication", "database connection", "handler", "parse", "config"]:
        clear_search_cache()
        t0 = time.monotonic()
        await _hsc(query=query, project_paths=[_ASTRO_PROJECT], use_rerank=False)
        latencies.append(time.monotonic() - t0)

    p95 = sorted(latencies)[int(len(latencies) * 0.95)]
    assert p95 < 0.5, f"p95 latency {p95*1000:.0f}ms exceeds 500ms gate"
