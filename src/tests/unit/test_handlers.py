"""Tests for opencode_search.handlers — index, search, enrichment, wiki, and graph MCP handlers.

All GPU-dependent calls are mocked. No GPU required unless @pytest.mark.gpu.
"""
# ruff: noqa: N806
from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from opencode_search.config import DEFAULT_DIMS, ProjectEntry, get_project_db_path
from opencode_search.graph.storage import CommunityData, EdgeData, GraphStorage, NodeData
from opencode_search.handlers._enrichment import handle_enrich_project, handle_get_symbol_intent
from opencode_search.handlers._graph import (
    handle_detect_impact,
    handle_get_callees,
    handle_get_callers,
    handle_get_communities,
    handle_get_symbol,
    handle_trace_path,
)
from opencode_search.handlers._wiki import (
    handle_wiki_generate,
    handle_wiki_ingest,
    handle_wiki_lint,
)

pytestmark = pytest.mark.asyncio

_IDX = "opencode_search.handlers._index"
_QRY = "opencode_search.handlers._query"
_WCH = "opencode_search.handlers._watch"
_CMN = "opencode_search.handlers._common"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


@dataclass
class _FakeIndexResult:
    files_indexed: int = 3
    files_unchanged: int = 1
    files_removed: int = 0
    chunks_total: int = 12
    errors: int = 0
    elapsed_s: float = 0.5


def _make_entry(path: str) -> ProjectEntry:
    return ProjectEntry(
        path=path,
        db_path=get_project_db_path(path),
        dims=DEFAULT_DIMS,
    )


# ---------------------------------------------------------------------------
# handle_index_project
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_handle_index_project_missing_dir():
    from opencode_search.handlers import handle_index_project
    result = await handle_index_project(path="/nonexistent/path/xyz")
    assert "error" in result
    assert "not found" in result["error"].lower() or "directory" in result["error"].lower()


@pytest.mark.asyncio
async def test_handle_index_project_success(tmp_path):
    import asyncio

    from opencode_search.handlers import _indexing_status, handle_index_project
    expected_db_path = get_project_db_path(tmp_path)

    with patch(f"{_IDX}._index_project", AsyncMock(return_value=_FakeIndexResult())), \
         patch(f"{_IDX}.load_registry", return_value={}), \
         patch(f"{_IDX}.save_registry"), \
         patch(f"{_IDX}.clear_search_cache"), \
         patch(f"{_IDX}.Storage") as MockStorage, \
         patch(f"{_IDX}.watcher_manager") as MockWatcher:
        mock_st = MagicMock()
        mock_st.open = AsyncMock()
        mock_st.close = AsyncMock()
        mock_st.compact_before_index = AsyncMock()
        MockStorage.return_value = mock_st
        MockWatcher.is_active.return_value = False

        result = await handle_index_project(path=str(tmp_path))
        assert result.get("status") == "indexing"
        assert "started_at" in result

        pending = [t for t in asyncio.all_tasks() if t is not asyncio.current_task()]
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)

        MockStorage.assert_called_once_with(db_path=expected_db_path, dims=DEFAULT_DIMS)

    path_str = str(tmp_path.resolve())
    assert _indexing_status[path_str]["running"] is False
    assert _indexing_status[path_str].get("status") == "ok"
    assert _indexing_status[path_str]["files_indexed"] == 3
    assert _indexing_status[path_str]["chunks_total"] == 12
    assert _indexing_status[path_str]["errors"] == 0


@pytest.mark.asyncio
async def test_handle_index_project_no_duplicate_run(tmp_path):
    from opencode_search.handlers import _indexing_status, handle_index_project

    path_str = str(tmp_path)
    _indexing_status[path_str] = {"running": True}

    result = await handle_index_project(path=path_str)
    assert result.get("status") == "already_indexing"

    del _indexing_status[path_str]


@pytest.mark.asyncio
async def test_handle_index_project_clears_running_on_exception(tmp_path):
    import asyncio

    from opencode_search.handlers import _indexing_status, handle_index_project

    with patch(f"{_IDX}.Storage") as MockStorage:
        mock_st = MagicMock()
        mock_st.open = AsyncMock(side_effect=RuntimeError("db failed"))
        mock_st.close = AsyncMock()
        MockStorage.return_value = mock_st

        result = await handle_index_project(path=str(tmp_path))
        assert result.get("status") == "indexing"

        pending = [t for t in asyncio.all_tasks() if t is not asyncio.current_task()]
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)

    path_str = str(tmp_path.resolve())
    assert _indexing_status[path_str]["running"] is False
    assert _indexing_status[path_str].get("status") == "error"


@pytest.mark.asyncio
async def test_handle_index_project_preserves_existing_watch_on_plain_reindex(tmp_path):
    from opencode_search.handlers import handle_index_project

    path_str = str(tmp_path.resolve())
    existing = _make_entry(path_str)
    existing.watch = True

    saved_registry: dict[str, ProjectEntry] = {}

    def _capture_save(registry):
        saved_registry.clear()
        saved_registry.update(registry)

    with patch(f"{_IDX}._index_project", AsyncMock(return_value=_FakeIndexResult())), \
         patch(f"{_IDX}.load_registry", return_value={path_str: existing}), \
         patch(f"{_IDX}.save_registry", side_effect=_capture_save), \
         patch(f"{_IDX}.clear_search_cache"), \
         patch(f"{_IDX}.Storage") as MockStorage, \
         patch(f"{_IDX}.watcher_manager") as MockWatcher:
        mock_st = MagicMock()
        mock_st.open = AsyncMock()
        mock_st.close = AsyncMock()
        mock_st.compact_before_index = AsyncMock()
        MockStorage.return_value = mock_st
        MockWatcher.is_active.return_value = True

        result = await handle_index_project(path=path_str, watch=False)
        assert result.get("status") == "indexing"

        import asyncio
        pending = [t for t in asyncio.all_tasks() if t is not asyncio.current_task()]
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)

    assert saved_registry[path_str].watch is True


@pytest.mark.asyncio
async def test_handle_project_status_skips_missing_db_without_recreating(tmp_path):
    from opencode_search.handlers import handle_project_status

    project_root = tmp_path / "project"
    project_root.mkdir()
    entry = ProjectEntry(
        path=str(project_root),
        db_path=str(tmp_path / "central-index" / "index"),
        dims=DEFAULT_DIMS,
    )

    with patch(f"{_QRY}.load_registry", return_value={str(project_root): entry}), \
         patch(f"{_QRY}.watcher_manager") as MockWatcher, \
         patch(f"{_QRY}.Storage") as MockStorage:
        MockWatcher.is_active.return_value = False
        result = await handle_project_status(path=str(project_root))

    MockStorage.assert_not_called()
    assert result["indexed"] is True
    assert result["chunks"] is None


# ---------------------------------------------------------------------------
# handle_search_code
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_handle_search_code_empty_query():
    from opencode_search.handlers import handle_search_code
    result = await handle_search_code(query="")
    assert "error" in result


@pytest.mark.asyncio
async def test_handle_search_code_no_registry():
    from opencode_search.handlers import handle_search_code

    with patch(f"{_QRY}.load_registry", return_value={}):
        result = await handle_search_code(query="find something")

    assert "note" in result or "results" in result


@pytest.mark.asyncio
async def test_handle_search_code_with_results():
    from opencode_search.handlers import handle_search_code
    from opencode_search.search import SearchResult

    fake_results = [
        SearchResult(
            path="/tmp/foo.py",
            content="def foo(): pass",
            language="python",
            start_line=1,
            end_line=5,
            score=0.95,
            project_path="/tmp",
        )
    ]

    with patch(f"{_QRY}.load_registry", return_value={"/tmp": _make_entry("/tmp")}), \
         patch(f"{_QRY}.search", AsyncMock(return_value=fake_results)):
        result = await handle_search_code(query="find function")

    assert "results" in result
    assert len(result["results"]) == 1
    assert result["results"][0]["path"] == "/tmp/foo.py"
    assert result["results"][0]["score"] == 0.95


@pytest.mark.asyncio
async def test_handle_search_code_filters_by_project_paths():
    from opencode_search.handlers import handle_search_code

    registry = {
        "/tmp/a": _make_entry("/tmp/a"),
        "/tmp/b": _make_entry("/tmp/b"),
    }

    searched_projects = []

    async def capture_search(query, *, projects, **kwargs):
        searched_projects.extend(projects)
        return []

    with patch(f"{_QRY}.load_registry", return_value=registry), \
         patch(f"{_QRY}.search", side_effect=capture_search):
        await handle_search_code(query="test", project_paths=["/tmp/a"])

    assert len(searched_projects) == 1
    assert searched_projects[0].path == "/tmp/a"


@pytest.mark.asyncio
async def test_handle_search_code_missing_project_paths():
    from opencode_search.handlers import handle_search_code

    with patch(f"{_QRY}.load_registry", return_value={"/tmp/a": _make_entry("/tmp/a")}):
        result = await handle_search_code(query="test", project_paths=["/tmp/nonexistent"])

    assert "error" in result


@pytest.mark.asyncio
async def test_handle_search_code_no_tier_validation():
    """search_code no longer raises mixed-tier errors — all projects use same model."""
    from opencode_search.handlers import handle_search_code

    registry = {
        "/tmp/a": _make_entry("/tmp/a"),
        "/tmp/b": _make_entry("/tmp/b"),
    }

    with patch(f"{_QRY}.load_registry", return_value=registry), \
         patch(f"{_QRY}.search", AsyncMock(return_value=[])):
        result = await handle_search_code(query="test")

    assert "error" not in result or "Mixed-tier" not in result.get("error", "")


# ---------------------------------------------------------------------------
# handle_project_status
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_handle_project_status_not_indexed():
    from opencode_search.handlers import handle_project_status

    with patch(f"{_QRY}.load_registry", return_value={}):
        result = await handle_project_status(path="/tmp/unknown")

    assert result["indexed"] is False


@pytest.mark.asyncio
async def test_handle_project_status_indexed(tmp_path):
    from opencode_search.handlers import handle_project_status

    project_root = tmp_path / "proj"
    project_root.mkdir()
    entry = _make_entry(str(project_root))
    Path(entry.db_path).mkdir(parents=True)
    registry = {str(project_root): entry}

    mock_storage = MagicMock()
    mock_storage.open = AsyncMock()
    mock_storage.close = AsyncMock()
    mock_storage.count = AsyncMock(return_value=42)

    with patch(f"{_QRY}.load_registry", return_value=registry), \
         patch(f"{_QRY}.Storage", return_value=mock_storage), \
         patch(f"{_QRY}.watcher_manager") as MockWatcher:
        MockWatcher.is_active.return_value = False

        result = await handle_project_status(path=str(project_root))

    assert result["indexed"] is True
    assert "tier" not in result, "tier key should not be in project_status result"
    assert result["chunks"] == 42


# ---------------------------------------------------------------------------
# handle_list_indexed_projects
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_handle_list_indexed_projects_empty():
    from opencode_search.handlers import handle_list_indexed_projects

    with patch(f"{_QRY}.load_registry", return_value={}), \
         patch(f"{_QRY}.watcher_manager") as MockWatcher:
        MockWatcher.list_active.return_value = []
        result = await handle_list_indexed_projects()

    assert result["projects"] == []


@pytest.mark.asyncio
async def test_handle_list_indexed_projects_with_entries():
    from opencode_search.handlers import handle_list_indexed_projects

    registry = {
        "/tmp/a": _make_entry("/tmp/a"),
        "/tmp/b": _make_entry("/tmp/b"),
    }

    with patch(f"{_QRY}.load_registry", return_value=registry), \
         patch(f"{_QRY}.watcher_manager") as MockWatcher:
        MockWatcher.list_active.return_value = ["/tmp/a"]
        result = await handle_list_indexed_projects()

    assert len(result["projects"]) == 2
    paths = {p["path"] for p in result["projects"]}
    assert paths == {"/tmp/a", "/tmp/b"}
    watching = {p["path"]: p["watching"] for p in result["projects"]}
    assert watching["/tmp/a"] is True
    assert watching["/tmp/b"] is False
    for p in result["projects"]:
        assert "tier" not in p, "tier key should not appear in list_indexed_projects"


# ---------------------------------------------------------------------------
# auto watch lifecycle helpers
# ---------------------------------------------------------------------------


async def test_resolve_indexed_project_path_prefers_nearest_ancestor():
    from opencode_search.handlers import resolve_indexed_project_path

    registry = {
        "/tmp/work": _make_entry("/tmp/work"),
        "/tmp/work/repo": _make_entry("/tmp/work/repo"),
    }

    with patch(f"{_CMN}.load_registry", return_value=registry):
        resolved = resolve_indexed_project_path("/tmp/work/repo/src/module.py")

    assert resolved == "/tmp/work/repo"


@pytest.mark.asyncio
async def test_handle_ensure_project_watching_starts_for_indexed_ancestor():
    from opencode_search.handlers import handle_ensure_project_watching

    entry = _make_entry("/tmp/work/repo")

    started: dict[str, object] = {}

    async def _mock_start(root, *, on_change):
        started["root"] = root
        started["callback"] = on_change
        return True

    with patch(f"{_WCH}.resolve_indexed_project_path", return_value=entry.path), \
         patch(f"{_WCH}.load_registry", return_value={entry.path: entry}), \
         patch(f"{_WCH}.watcher_manager") as MockWatcher:
        MockWatcher.is_active.return_value = False
        MockWatcher.start = AsyncMock(side_effect=_mock_start)

        result = await handle_ensure_project_watching("/tmp/work/repo/src/module.py")

    assert result["status"] == "ok"
    assert result["path"] == entry.path
    assert started["root"] == entry.path
    assert started["callback"] is not None


@pytest.mark.asyncio
async def test_handle_release_project_watch_keeps_persisted_watch():
    from opencode_search.handlers import handle_release_project_watch

    entry = _make_entry("/tmp/proj")
    entry.watch = True

    with patch(f"{_WCH}.resolve_indexed_project_path", return_value=entry.path), \
         patch(f"{_WCH}.load_registry", return_value={entry.path: entry}), \
         patch(f"{_WCH}.watcher_manager") as MockWatcher:
        MockWatcher.is_active.return_value = True
        MockWatcher.stop = AsyncMock()

        result = await handle_release_project_watch("/tmp/proj")

    assert result["status"] == "kept_persisted"
    MockWatcher.stop.assert_not_called()


@pytest.mark.asyncio
async def test_handle_release_project_watch_stops_non_persisted_watch():
    from opencode_search.handlers import handle_release_project_watch

    entry = _make_entry("/tmp/proj")
    entry.watch = False

    with patch(f"{_WCH}.resolve_indexed_project_path", return_value=entry.path), \
         patch(f"{_WCH}.load_registry", return_value={entry.path: entry}), \
         patch(f"{_WCH}.watcher_manager") as MockWatcher:
        MockWatcher.is_active.return_value = True
        MockWatcher.stop = AsyncMock()

        result = await handle_release_project_watch("/tmp/proj/subdir")

    assert result["status"] == "stopped"
    MockWatcher.stop.assert_called_once_with("/tmp/proj")


# ---------------------------------------------------------------------------
# handle_stop_watching
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_handle_stop_watching_not_active():
    from opencode_search.handlers import handle_stop_watching

    with patch(f"{_WCH}.watcher_manager") as MockWatcher, \
         patch(f"{_WCH}.load_registry", return_value={}), \
         patch(f"{_WCH}.save_registry"):
        MockWatcher.is_active.return_value = False
        MockWatcher.stop = AsyncMock()

        result = await handle_stop_watching(path="/tmp/proj")

    assert result["was_watching"] is False
    assert result["status"] == "stopped"


@pytest.mark.asyncio
async def test_handle_stop_watching_was_active():
    from opencode_search.handlers import handle_stop_watching

    entry = _make_entry("/tmp/proj")

    with patch(f"{_WCH}.watcher_manager") as MockWatcher, \
         patch(f"{_WCH}.load_registry", return_value={"/tmp/proj": entry}), \
         patch(f"{_WCH}.save_registry"):
        MockWatcher.is_active.return_value = True
        MockWatcher.stop = AsyncMock()

        result = await handle_stop_watching(path="/tmp/proj")

    assert result["was_watching"] is True
    MockWatcher.stop.assert_called_once()


# ===========================================================================
# Enrichment & wiki handlers  (was test_enrichment_handlers.py)
# ===========================================================================

def _node_id_enrichment(file: str, qn: str) -> str:
    return hashlib.sha256(f"{file}::{qn}".encode()).hexdigest()[:16]


def _make_node_enrichment(file: str, name: str, qn: str | None = None) -> NodeData:
    qualified = qn or f"mod.{name}"
    return NodeData(
        id=_node_id_enrichment(file, qualified),
        name=name, qualified_name=qualified, kind="function",
        file=file, start_line=1, end_line=10, language="python",
        created_at="", updated_at="",
    )


@pytest.fixture
def project_with_graph_enrichment(tmp_path):

    project_root = tmp_path / "project"
    project_root.mkdir()

    # Use a unique path for this test
    graph_db_path = str(tmp_path / "graph.db")

    gs = GraphStorage(graph_db_path)
    gs.open()

    nodes = [
        _make_node_enrichment("/project/auth.py", "authenticate", "auth.authenticate"),
        _make_node_enrichment("/project/auth.py", "verify", "auth.verify"),
    ]
    gs.upsert_nodes(nodes)
    gs.upsert_community(CommunityData(id=0, node_count=2))
    for n in nodes:
        gs.set_community(n.id, 0)
    gs.close()

    with patch(
        "opencode_search.handlers._enrichment.get_project_graph_db_path",
        return_value=graph_db_path,
    ), patch(
        "opencode_search.handlers._wiki.get_project_graph_db_path",
        return_value=graph_db_path,
    ), patch(
        "opencode_search.handlers._wiki.get_project_wiki_dir",
        return_value=tmp_path / "wiki",
    ), patch(
        "opencode_search.handlers._wiki.get_project_raw_dir",
        return_value=tmp_path / "raw",
    ):
        yield str(project_root), nodes, graph_db_path


# ---------------------------------------------------------------------------
# handle_enrich_project
# ---------------------------------------------------------------------------


async def test_handle_enrich_project_returns_error_when_no_llm(project_with_graph_enrichment):
    project_path, _, _ = project_with_graph_enrichment
    with patch("opencode_search.handlers._enrichment._get_llm", return_value=None):
        result = await handle_enrich_project(project_path=project_path)
    assert "error" in result


async def test_handle_enrich_project_returns_error_when_ollama_not_available(project_with_graph_enrichment):
    project_path, _, _ = project_with_graph_enrichment
    llm = MagicMock()
    llm.is_available.return_value = False
    with patch("opencode_search.handlers._enrichment._get_llm", return_value=llm):
        result = await handle_enrich_project(project_path=project_path)
    assert "error" in result


async def test_handle_enrich_project_communities_scope(project_with_graph_enrichment):
    project_path, _, _graph_db_path = project_with_graph_enrichment
    llm = MagicMock()
    llm.is_available.return_value = True
    llm.community_summary.return_value = ("Auth Layer", "Handles auth.")
    with patch("opencode_search.handlers._enrichment._get_llm", return_value=llm):
        result = await handle_enrich_project(project_path=project_path, scope="communities")
    assert result.get("status") == "ok"
    assert result.get("enriched_communities") >= 0


async def test_handle_enrich_project_symbols_scope(project_with_graph_enrichment):
    project_path, _, _ = project_with_graph_enrichment
    llm = MagicMock()
    llm.is_available.return_value = True
    llm.symbol_intent.return_value = "Authenticates a user."
    with patch("opencode_search.handlers._enrichment._get_llm", return_value=llm):
        result = await handle_enrich_project(project_path=project_path, scope="symbols")
    assert result.get("status") == "ok"
    assert result.get("enriched_symbols") >= 0


async def test_handle_enrich_project_returns_elapsed_s(project_with_graph_enrichment):
    project_path, _, _ = project_with_graph_enrichment
    llm = MagicMock()
    llm.is_available.return_value = True
    llm.community_summary.return_value = ("Title", "Summary.")
    with patch("opencode_search.handlers._enrichment._get_llm", return_value=llm):
        result = await handle_enrich_project(project_path=project_path)
    assert "elapsed_s" in result
    assert result["elapsed_s"] >= 0


# ---------------------------------------------------------------------------
# handle_get_symbol_intent
# ---------------------------------------------------------------------------


async def test_handle_get_symbol_intent_returns_error_when_no_llm(project_with_graph_enrichment):
    project_path, _nodes, _ = project_with_graph_enrichment
    with patch("opencode_search.handlers._enrichment._get_llm", return_value=None):
        result = await handle_get_symbol_intent(name="authenticate", project_path=project_path)
    assert "error" in result


async def test_handle_get_symbol_intent_calls_llm_when_stale(project_with_graph_enrichment):
    project_path, _nodes, _ = project_with_graph_enrichment
    llm = MagicMock()
    llm.is_available.return_value = True
    llm.symbol_intent.return_value = "Authenticates a user by token."
    with patch("opencode_search.handlers._enrichment._get_llm", return_value=llm):
        result = await handle_get_symbol_intent(name="authenticate", project_path=project_path)
    assert "intent" in result or "error" in result
    if "intent" in result:
        assert result["intent"] == "Authenticates a user by token."
        assert result["cached"] is False


async def test_handle_get_symbol_intent_caches_result_in_db(project_with_graph_enrichment):
    project_path, _nodes, _graph_db_path = project_with_graph_enrichment
    llm = MagicMock()
    llm.is_available.return_value = True
    llm.symbol_intent.return_value = "Cached intent value."
    with patch("opencode_search.handlers._enrichment._get_llm", return_value=llm):
        # First call generates
        result1 = await handle_get_symbol_intent(name="authenticate", project_path=project_path)
        assert "intent" in result1, f"handle_get_symbol_intent returned no 'intent' key: {result1}"
        # Second call should return cached
        result2 = await handle_get_symbol_intent(name="authenticate", project_path=project_path)
    assert result2.get("cached") is True
    assert result2.get("intent") == "Cached intent value."
    llm.symbol_intent.assert_called_once()  # Not called again


async def test_handle_get_symbol_intent_symbol_not_found(project_with_graph_enrichment):
    project_path, _, _ = project_with_graph_enrichment
    llm = MagicMock()
    llm.is_available.return_value = True
    with patch("opencode_search.handlers._enrichment._get_llm", return_value=llm):
        result = await handle_get_symbol_intent(name="nonexistent_xyz", project_path=project_path)
    assert "error" in result


# ---------------------------------------------------------------------------
# handle_wiki_generate
# ---------------------------------------------------------------------------


async def test_handle_wiki_generate_returns_error_when_no_llm(project_with_graph_enrichment):
    project_path, _, _ = project_with_graph_enrichment
    with patch("opencode_search.handlers._wiki._get_llm", return_value=None):
        result = await handle_wiki_generate(project_path=project_path)
    assert "error" in result


async def test_handle_wiki_generate_returns_error_when_ollama_not_available(project_with_graph_enrichment):
    project_path, _, _ = project_with_graph_enrichment
    llm = MagicMock()
    llm.is_available.return_value = False
    with patch("opencode_search.handlers._wiki._get_llm", return_value=llm):
        result = await handle_wiki_generate(project_path=project_path)
    assert "error" in result


async def test_handle_wiki_generate_creates_pages(project_with_graph_enrichment):
    project_path, _, _ = project_with_graph_enrichment
    llm = MagicMock()
    llm.is_available.return_value = True
    llm.community_summary.return_value = ("Auth Layer", "Handles auth.")
    with patch("opencode_search.handlers._wiki._get_llm", return_value=llm):
        result = await handle_wiki_generate(project_path=project_path)
    assert result.get("status") == "ok"
    assert "pages_created" in result


# ---------------------------------------------------------------------------
# handle_wiki_ingest
# ---------------------------------------------------------------------------


async def test_handle_wiki_ingest_invalid_path_returns_error(project_with_graph_enrichment):
    project_path, _, _ = project_with_graph_enrichment
    llm = MagicMock()
    llm.is_available.return_value = True
    with patch("opencode_search.handlers._wiki._get_llm", return_value=llm):
        result = await handle_wiki_ingest(
            source_path="/nonexistent/path/doc.md",
            project_path=project_path,
        )
    assert "error" in result


async def test_handle_wiki_ingest_creates_page(project_with_graph_enrichment, tmp_path):
    project_path, _, _ = project_with_graph_enrichment
    # Create a source file
    src = tmp_path / "design.md"
    src.write_text("# Design\nThis is a design document.", encoding="utf-8")
    llm = MagicMock()
    llm.is_available.return_value = True
    llm.raw_doc_to_wiki.return_value = "# Design\n\nKey information extracted."
    with patch("opencode_search.handlers._wiki._get_llm", return_value=llm):
        result = await handle_wiki_ingest(
            source_path=str(src),
            project_path=project_path,
        )
    assert result.get("status") == "ok"
    assert len(result.get("pages_created", [])) >= 1


async def test_handle_wiki_ingest_no_llm_returns_error(project_with_graph_enrichment, tmp_path):
    project_path, _, _ = project_with_graph_enrichment
    src = tmp_path / "doc.md"
    src.write_text("content", encoding="utf-8")
    with patch("opencode_search.handlers._wiki._get_llm", return_value=None):
        result = await handle_wiki_ingest(source_path=str(src), project_path=project_path)
    assert "error" in result


# ---------------------------------------------------------------------------
# handle_wiki_lint
# ---------------------------------------------------------------------------


async def test_handle_wiki_lint_clean_wiki_no_issues(project_with_graph_enrichment):
    project_path, _, _ = project_with_graph_enrichment
    result = await handle_wiki_lint(project_path=project_path)
    assert "healthy" in result


async def test_handle_wiki_lint_returns_valid_structure(project_with_graph_enrichment):
    project_path, _, _ = project_with_graph_enrichment
    result = await handle_wiki_lint(project_path=project_path)
    assert "total_pages" in result
    assert "orphans" in result
    assert "empty_pages" in result


# ===========================================================================
# Graph MCP handlers  (was test_graph_handlers.py)
# ===========================================================================

def _node_id_graph(file: str, qn: str) -> str:
    return hashlib.sha256(f"{file}::{qn}".encode()).hexdigest()[:16]


def _make_node_graph(file: str, name: str, qualified_name: str | None = None) -> NodeData:
    qn = qualified_name or f"mod.{name}"
    return NodeData(
        id=_node_id_graph(file, qn),
        name=name,
        qualified_name=qn,
        kind="function",
        file=file,
        start_line=1,
        end_line=10,
        language="python",
        created_at="2026-01-01T00:00:00",
        updated_at="2026-01-01T00:00:00",
    )


@pytest.fixture
def project_with_graph_graph(tmp_path):
    """Create a temp project with a pre-populated graph DB."""
    import unittest.mock as mock

    project_root = tmp_path / "project"
    project_root.mkdir()

    # Patch config to use tmp path
    graph_db_path = str(tmp_path / "graph.db")

    gs = GraphStorage(graph_db_path)
    gs.open()

    nodes = [
        _make_node_graph("/project/auth.py", "authenticate", "auth.authenticate"),
        _make_node_graph("/project/auth.py", "verify_token", "auth.verify_token"),
        _make_node_graph("/project/handler.py", "handle_login", "handler.handle_login"),
        _make_node_graph("/project/db.py", "get_connection", "db.get_connection"),
    ]
    gs.upsert_nodes(nodes)

    # handle_login calls authenticate, authenticate calls verify_token
    gs.upsert_edges([
        EdgeData(
            from_id=nodes[2].id,  # handle_login
            to_id=nodes[0].id,    # authenticate
            kind="CALLS",
            confidence=0.9,
            resolution_strategy="same_module",
        ),
        EdgeData(
            from_id=nodes[0].id,  # authenticate
            to_id=nodes[1].id,    # verify_token
            kind="CALLS",
            confidence=0.85,
            resolution_strategy="unique_name",
        ),
    ])
    gs.close()

    # Patch get_project_graph_db_path to return our test path
    with mock.patch(
        "opencode_search.handlers._graph.get_project_graph_db_path",
        return_value=graph_db_path,
    ):
        yield str(project_root), nodes, graph_db_path


async def test_handle_get_symbol_found_by_name(project_with_graph_graph):
    project_path, _nodes, _ = project_with_graph_graph
    result = await handle_get_symbol(name="authenticate", project_path=project_path)
    assert "matches" in result
    assert result["count"] >= 1
    match = result["matches"][0]
    assert match["name"] == "authenticate"
    assert match["kind"] == "function"
    assert match["file"] == "/project/auth.py"


async def test_handle_get_symbol_found_by_qualified_name(project_with_graph_graph):
    project_path, _, _ = project_with_graph_graph
    result = await handle_get_symbol(name="auth.authenticate", project_path=project_path)
    assert result["count"] >= 1
    assert result["matches"][0]["qualified_name"] == "auth.authenticate"


async def test_handle_get_symbol_not_found_returns_error_dict(project_with_graph_graph):
    project_path, _, _ = project_with_graph_graph
    result = await handle_get_symbol(name="nonexistent_xyz", project_path=project_path)
    assert "error" in result


async def test_handle_get_symbol_includes_caller_count(project_with_graph_graph):
    project_path, _nodes, _ = project_with_graph_graph
    # authenticate is called by handle_login
    result = await handle_get_symbol(name="authenticate", project_path=project_path)
    match = result["matches"][0]
    assert match["caller_count"] >= 1


async def test_handle_get_callers_returns_chain(project_with_graph_graph):
    project_path, _nodes, _ = project_with_graph_graph
    # verify_token is called by authenticate
    result = await handle_get_callers(
        symbol="verify_token", project_path=project_path, depth=2,
    )
    assert "callers" in result
    assert result["total"] >= 1
    caller_names = {c["name"] for c in result["callers"]}
    assert "authenticate" in caller_names


async def test_handle_get_callers_respects_depth_param(project_with_graph_graph):
    project_path, _nodes, _ = project_with_graph_graph
    result = await handle_get_callers(
        symbol="verify_token", project_path=project_path, depth=1,
    )
    for c in result["callers"]:
        assert c["depth"] <= 1


async def test_handle_get_callers_symbol_not_found(project_with_graph_graph):
    project_path, _, _ = project_with_graph_graph
    result = await handle_get_callers(symbol="ghost_fn", project_path=project_path)
    assert "error" in result


async def test_handle_get_callees_returns_chain(project_with_graph_graph):
    project_path, _nodes, _ = project_with_graph_graph
    result = await handle_get_callees(
        symbol="authenticate", project_path=project_path, depth=2,
    )
    assert "callees" in result
    assert result["total"] >= 1
    callee_names = {c["name"] for c in result["callees"]}
    assert "verify_token" in callee_names


async def test_handle_trace_path_returns_ordered_steps(project_with_graph_graph):
    project_path, _nodes, _ = project_with_graph_graph
    result = await handle_trace_path(
        from_symbol="handle_login",
        to_symbol="verify_token",
        project_path=project_path,
    )
    assert result.get("connected") is True
    assert len(result["path"]) >= 2
    assert result["hops"] >= 1


async def test_handle_trace_path_no_connection_returns_empty(project_with_graph_graph):
    project_path, _nodes, _ = project_with_graph_graph
    # get_connection has no edges
    result = await handle_trace_path(
        from_symbol="get_connection",
        to_symbol="authenticate",
        project_path=project_path,
    )
    assert result.get("connected") is False or "path" in result


async def test_handle_detect_impact_grouped_by_depth(project_with_graph_graph):
    project_path, _nodes, _ = project_with_graph_graph
    # verify_token is at the bottom; handle_login → authenticate → verify_token
    result = await handle_detect_impact(symbol="verify_token", project_path=project_path)
    assert "callers_by_depth" in result
    assert result["total_affected"] >= 1


async def test_handle_detect_impact_empty_when_leaf_node(project_with_graph_graph):
    project_path, _nodes, _ = project_with_graph_graph
    # get_connection has no callers
    result = await handle_detect_impact(symbol="get_connection", project_path=project_path)
    assert result.get("total_affected", 0) == 0


async def test_handle_get_communities_returns_list(project_with_graph_graph):
    project_path, _, graph_db_path = project_with_graph_graph
    from opencode_search.graph.storage import CommunityData

    gs = GraphStorage(graph_db_path)
    gs.open()
    gs.upsert_community(CommunityData(
        id=0, title="Auth layer", summary="Handles auth",
        node_count=3, key_entry_points=["auth.authenticate"],
    ))
    gs.close()

    result = await handle_get_communities(project_path=project_path)
    assert "communities" in result
    assert result["total"] >= 1
    assert result["communities"][0]["title"] == "Auth layer"


async def test_handle_get_symbol_no_graph_returns_error(tmp_path):
    """When graph DB doesn't exist, return error dict instead of crashing."""
    import unittest.mock as mock

    with mock.patch(
        "opencode_search.handlers._graph.get_project_graph_db_path",
        return_value=str(tmp_path / "nonexistent.db"),
    ):
        result = await handle_get_symbol(name="anything", project_path="/tmp/nonexistent")
    assert "error" in result


# ===========================================================================
# handle_import_cycles / handle_suggest_questions / handle_graph_diff
# ===========================================================================


@pytest.fixture
def project_with_import_graph(tmp_path):
    """Graph DB with import edges forming a cycle: a.py → b.py → c.py → a.py."""
    import unittest.mock as mock

    graph_db_path = str(tmp_path / "graph.db")
    gs = GraphStorage(graph_db_path)
    gs.open()

    def _node(file, name):
        nid = _node_id_graph(file, name)
        return NodeData(
            id=nid, name=name, qualified_name=name, kind="function", file=file,
            language="python", created_at="2026-01-01T00:00:00", updated_at="2026-01-01T00:00:00",
        )

    na = _node("/proj/a.py", "fa")
    nb = _node("/proj/b.py", "fb")
    nc = _node("/proj/c.py", "fc")
    gs.upsert_nodes([na, nb, nc])
    # a.py imports b.py, b.py imports c.py, c.py imports a.py → cycle
    gs.upsert_edges([
        EdgeData(from_id=na.id, to_id=nb.id, kind="IMPORTS", confidence=1.0, resolution_strategy=""),
        EdgeData(from_id=nb.id, to_id=nc.id, kind="IMPORTS", confidence=1.0, resolution_strategy=""),
        EdgeData(from_id=nc.id, to_id=na.id, kind="IMPORTS", confidence=1.0, resolution_strategy=""),
    ])
    gs.close()

    with mock.patch(
        "opencode_search.handlers._graph.get_project_graph_db_path",
        return_value=graph_db_path,
    ):
        yield str(tmp_path / "proj"), graph_db_path


async def test_handle_import_cycles_detects_cycle(project_with_import_graph):
    from opencode_search.handlers._graph import handle_import_cycles
    project_path, _ = project_with_import_graph
    result = await handle_import_cycles(project_path=project_path)
    assert "cycles" in result
    assert "cycle_count" in result
    assert result["has_cycles"] is True
    assert result["cycle_count"] >= 1
    cycle = result["cycles"][0]
    assert "cycle" in cycle
    assert "length" in cycle
    assert "severity" in cycle
    assert cycle["severity"] in ("high", "medium", "low")


async def test_handle_import_cycles_no_cycles(tmp_path):
    """A graph with only CALLS edges (no IMPORTS) returns no cycles."""
    import unittest.mock as mock

    graph_db_path = str(tmp_path / "graph.db")
    gs = GraphStorage(graph_db_path)
    gs.open()
    na = _make_node_graph("/proj/a.py", "fa")
    nb = _make_node_graph("/proj/b.py", "fb")
    gs.upsert_nodes([na, nb])
    gs.upsert_edges([
        EdgeData(from_id=na.id, to_id=nb.id, kind="CALLS", confidence=1.0, resolution_strategy=""),
    ])
    gs.close()

    with mock.patch(
        "opencode_search.handlers._graph.get_project_graph_db_path",
        return_value=graph_db_path,
    ):
        from opencode_search.handlers._graph import handle_import_cycles
        result = await handle_import_cycles(project_path=str(tmp_path / "proj"))
    assert result["has_cycles"] is False
    assert result["cycle_count"] == 0


async def test_handle_import_cycles_no_graph_returns_error(tmp_path):
    import unittest.mock as mock
    with mock.patch(
        "opencode_search.handlers._graph.get_project_graph_db_path",
        return_value=str(tmp_path / "nonexistent.db"),
    ):
        from opencode_search.handlers._graph import handle_import_cycles
        result = await handle_import_cycles(project_path="/tmp/nonexistent")
    assert "error" in result


async def test_handle_suggest_questions_returns_list(project_with_graph_graph):
    from opencode_search.handlers._graph import handle_suggest_questions
    project_path, _, _ = project_with_graph_graph
    result = await handle_suggest_questions(project_path=project_path)
    assert "questions" in result
    assert "count" in result
    assert isinstance(result["questions"], list)


async def test_handle_suggest_questions_each_has_type_and_question(project_with_graph_graph):
    from opencode_search.handlers._graph import handle_suggest_questions
    project_path, _, _ = project_with_graph_graph
    result = await handle_suggest_questions(project_path=project_path, top_n=10)
    for q in result["questions"]:
        assert "type" in q
        assert "why" in q


async def test_handle_suggest_questions_no_graph_returns_error(tmp_path):
    import unittest.mock as mock
    with mock.patch(
        "opencode_search.handlers._graph.get_project_graph_db_path",
        return_value=str(tmp_path / "nonexistent.db"),
    ):
        from opencode_search.handlers._graph import handle_suggest_questions
        result = await handle_suggest_questions(project_path="/tmp/nonexistent")
    assert "error" in result


async def test_handle_graph_diff_returns_structure(project_with_graph_graph):
    from opencode_search.handlers._graph import handle_graph_diff
    project_path, _, _ = project_with_graph_graph
    result = await handle_graph_diff(project_path=project_path, since="2020-01-01T00:00:00")
    assert "new_nodes" in result or "error" in result
    if "error" not in result:
        assert "since" in result
        assert isinstance(result.get("new_nodes", []), list)


async def test_handle_graph_diff_future_since_returns_empty(project_with_graph_graph):
    from opencode_search.handlers._graph import handle_graph_diff
    project_path, _, _ = project_with_graph_graph
    result = await handle_graph_diff(project_path=project_path, since="2099-01-01T00:00:00")
    if "error" not in result:
        assert result.get("new_nodes", []) == []
        assert result.get("new_edge_count", 0) == 0


async def test_handle_graph_diff_no_graph_returns_error(tmp_path):
    import unittest.mock as mock
    with mock.patch(
        "opencode_search.handlers._graph.get_project_graph_db_path",
        return_value=str(tmp_path / "nonexistent.db"),
    ):
        from opencode_search.handlers._graph import handle_graph_diff
        result = await handle_graph_diff(project_path="/tmp/nonexistent", since="2020-01-01T00:00:00")
    assert "error" in result


# ---------------------------------------------------------------------------
# handle_dedup_nodes tests
# ---------------------------------------------------------------------------

@pytest.fixture
def project_with_duplicate_nodes(tmp_path):
    """Graph DB with two near-duplicate nodes in the same file."""
    import unittest.mock as mock

    graph_db_path = str(tmp_path / "graph.db")
    gs = GraphStorage(graph_db_path)
    gs.open()

    # Two nodes in the same file with nearly-identical qualified names
    n1 = NodeData(
        id="aaa111000000001",
        name="process_order",
        qualified_name="orders.process_order",
        kind="function",
        file="/proj/orders.py",
        language="python",
        created_at="2026-01-01T00:00:00",
        updated_at="2026-01-01T00:00:00",
    )
    # Slightly different ID but same logical symbol (duplicate indexing scenario)
    n2 = NodeData(
        id="aaa111000000002",
        name="process_order",
        qualified_name="orders.process_order",  # exact same qualified_name → exact dedup catches it
        kind="function",
        file="/proj/orders.py",
        language="python",
        created_at="2026-01-01T00:00:01",
        updated_at="2026-01-01T00:00:01",
    )
    # A third unrelated node
    n3 = NodeData(
        id="bbb222000000001",
        name="cancel_order",
        qualified_name="orders.cancel_order",
        kind="function",
        file="/proj/orders.py",
        language="python",
        created_at="2026-01-01T00:00:00",
        updated_at="2026-01-01T00:00:00",
    )
    gs.upsert_nodes([n1, n2, n3])
    gs.upsert_edges([
        EdgeData(from_id=n3.id, to_id=n1.id, kind="CALLS", confidence=1.0),
        EdgeData(from_id=n3.id, to_id=n2.id, kind="CALLS", confidence=1.0),
    ])
    gs.close()

    with mock.patch(
        "opencode_search.handlers._graph.get_project_graph_db_path",
        return_value=graph_db_path,
    ):
        yield str(tmp_path / "proj"), graph_db_path


async def test_handle_dedup_nodes_dry_run_finds_duplicates(project_with_duplicate_nodes):
    """dry_run=True reports duplicates without modifying the DB."""
    from opencode_search.handlers._graph import handle_dedup_nodes

    project_path, graph_db_path = project_with_duplicate_nodes
    result = await handle_dedup_nodes(project_path=project_path, dry_run=True)

    assert "project_path" in result
    assert result["dry_run"] is True
    assert "strategy" in result
    assert result["strategy"] in ("exact", "fuzzy")
    assert "merged_count" in result
    assert "candidate_pairs_checked" in result
    assert "fuzzy_available" in result
    assert "errors" in result
    # Exact-mode dedup should detect the two identical qualified-name nodes
    assert result["merged_count"] >= 1, f"Expected ≥1 duplicate, got: {result}"


async def test_handle_dedup_nodes_applies_merge(project_with_duplicate_nodes):
    """dry_run=False actually removes the duplicate node."""
    from opencode_search.handlers._graph import handle_dedup_nodes
    from opencode_search.graph.storage import GraphStorage

    project_path, graph_db_path = project_with_duplicate_nodes
    result = await handle_dedup_nodes(project_path=project_path, dry_run=False)

    assert result["merged_count"] >= 1, f"Expected merge to happen, got: {result}"
    assert not result["errors"], f"Unexpected errors: {result['errors']}"

    # Verify the DB was actually modified — duplicate should be gone
    gs = GraphStorage(graph_db_path)
    gs.open()
    try:
        nodes = gs.all_nodes()
        all_qnames = [n.qualified_name for n in nodes]
        # After dedup, only one node with "orders.process_order" should remain
        process_order_nodes = [n for n in nodes if n.qualified_name == "orders.process_order"]
        assert len(process_order_nodes) == 1, (
            f"Expected 1 node after dedup, got {len(process_order_nodes)}"
        )
    finally:
        gs.close()


async def test_handle_dedup_nodes_no_graph_returns_error(tmp_path):
    """Returns error dict when the project has no graph DB."""
    import unittest.mock as mock
    with mock.patch(
        "opencode_search.handlers._graph.get_project_graph_db_path",
        return_value=str(tmp_path / "nonexistent.db"),
    ):
        from opencode_search.handlers._graph import handle_dedup_nodes
        result = await handle_dedup_nodes(project_path="/tmp/nonexistent")
    assert "error" in result


async def test_handle_dedup_nodes_no_duplicates_is_noop(tmp_path):
    """A graph with no duplicates results in merged_count=0."""
    import unittest.mock as mock
    from opencode_search.graph.storage import GraphStorage, NodeData

    graph_db_path = str(tmp_path / "graph.db")
    gs = GraphStorage(graph_db_path)
    gs.open()
    na = _make_node_graph("/proj/a.py", "func_alpha", "mod.func_alpha")
    nb = _make_node_graph("/proj/b.py", "func_beta", "mod.func_beta")
    gs.upsert_nodes([na, nb])
    gs.close()

    with mock.patch(
        "opencode_search.handlers._graph.get_project_graph_db_path",
        return_value=graph_db_path,
    ):
        from opencode_search.handlers._graph import handle_dedup_nodes
        result = await handle_dedup_nodes(project_path=str(tmp_path / "proj"), dry_run=False)

    assert result["merged_count"] == 0
    assert not result["errors"]


# ============================================================
# _graph_to_mermaid unit tests (no graph storage needed)
# ============================================================

def test_graph_to_mermaid_basic_structure():
    """_graph_to_mermaid returns a flowchart TD string with nodes and edges."""
    from opencode_search.handlers._graph import _graph_to_mermaid
    nodes = [
        {"id": "n1", "name": "alpha", "community_id": 1},
        {"id": "n2", "name": "beta", "community_id": 1},
        {"id": "n3", "name": "gamma", "community_id": None},
    ]
    edges = [
        {"from": "n1", "to": "n2", "kind": "calls"},
        {"from": "n2", "to": "n3", "kind": "calls"},
    ]
    communities = [{"id": 1, "title": "Core", "node_count": 2}]
    diagram = _graph_to_mermaid(nodes, edges, communities)
    assert diagram.startswith("flowchart TD")
    assert "alpha" in diagram
    assert "beta" in diagram
    assert "gamma" in diagram
    assert "-->" in diagram


def test_graph_to_mermaid_empty_graph():
    """Empty graph produces a minimal flowchart header."""
    from opencode_search.handlers._graph import _graph_to_mermaid
    diagram = _graph_to_mermaid([], [], [])
    assert diagram.startswith("flowchart TD")


def test_graph_to_mermaid_no_cross_boundary_edges():
    """Edges referencing nodes not in the export set are omitted."""
    from opencode_search.handlers._graph import _graph_to_mermaid
    nodes = [{"id": "n1", "name": "only_node", "community_id": None}]
    edges = [
        {"from": "n1", "to": "outside_node", "kind": "calls"},
        {"from": "n1", "to": "n1", "kind": "self"},
    ]
    diagram = _graph_to_mermaid(nodes, edges, [])
    arrow_count = diagram.count("-->")
    assert arrow_count == 1  # only the self-edge (n1 → n1 is in-set)


@pytest.mark.asyncio
async def test_handle_graph_export_mermaid_format(tmp_path):
    """handle_graph_export with format=mermaid returns a mermaid key."""
    import unittest.mock as mock
    from opencode_search.graph.storage import GraphStorage
    from opencode_search.handlers._graph import handle_graph_export

    graph_db_path = str(tmp_path / "graph.db")
    gs = GraphStorage(graph_db_path)
    gs.open()
    na = _make_node_graph(str(tmp_path / "a.py"), "func_a", "mod.func_a")
    nb = _make_node_graph(str(tmp_path / "b.py"), "func_b", "mod.func_b")
    gs.upsert_nodes([na, nb])
    gs.close()

    with mock.patch(
        "opencode_search.handlers._graph.get_project_graph_db_path",
        return_value=graph_db_path,
    ):
        result = await handle_graph_export(
            project_path=str(tmp_path / "proj"), format="mermaid", max_nodes=100
        )

    assert result.get("format") == "mermaid"
    assert "mermaid" in result
    assert result["mermaid"].startswith("flowchart TD")