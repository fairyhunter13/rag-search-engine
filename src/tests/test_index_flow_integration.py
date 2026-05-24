"""Integration regression tests for indexing and search state transitions."""
# ruff: noqa: E402

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

import pytest

pytest.importorskip("lancedb")
pytest.importorskip("pyarrow")

from opencode_search import config
from opencode_search.chunker import Chunk
from opencode_search.handlers import handle_index_project, handle_search_code
from opencode_search.search import clear_search_cache
from opencode_search.storage import Storage

pytestmark = [pytest.mark.integration, pytest.mark.runtime_deps]


def _split_lines(content: str, path: Path) -> list[Chunk]:
    chunks: list[Chunk] = []
    for line_no, line in enumerate(content.splitlines(), start=1):
        text = line.strip()
        if not text:
            continue
        chunks.append(
            Chunk(
                content=text,
                start_line=line_no,
                end_line=line_no,
                chunk_type="code",
                language="python",
            )
        )
    return chunks


def _vector_for(text: str, dims: int) -> list[float]:
    vec = [0.0] * dims
    vec[hash(text) % dims] = 1.0
    return vec


@pytest.mark.asyncio
async def test_reindex_shrinks_chunks_and_removes_searchable_stale_content(tmp_path, monkeypatch):
    project_root = tmp_path / "project"
    project_root.mkdir()
    source_file = project_root / "app.py"
    source_file.write_text("alpha\nbeta\ngamma\n")

    registry_path = tmp_path / "registry.json"
    monkeypatch.setattr(config, "REGISTRY_PATH", registry_path)

    dims = config.get_tier_dims("budget")

    def fake_embed_passages(texts, *, model, dimensions):
        assert dimensions == dims
        return [_vector_for(text, dimensions) for text in texts]

    def fake_embed_query(query, model, dimensions):
        assert dimensions == dims
        return _vector_for(query.strip(), dimensions)

    with patch("opencode_search.chunker.chunk_file", side_effect=_split_lines), \
         patch("opencode_search.embeddings.embed_passages", side_effect=fake_embed_passages), \
         patch("opencode_search.search._embed_query_sync", side_effect=fake_embed_query):
        first = await handle_index_project(path=str(project_root), tier="budget")
        assert first["status"] == "ok"
        assert first["chunks_total"] == 3

        clear_search_cache()
        before = await handle_search_code(
            query="gamma",
            project_paths=[str(project_root)],
            use_rerank=False,
        )
        assert any(row["content"] == "gamma" for row in before["results"])

        source_file.write_text("alpha\n")

        second = await handle_index_project(path=str(project_root), tier="budget")
        assert second["status"] == "ok"
        assert second["chunks_total"] == 1

        clear_search_cache()
        after = await handle_search_code(
            query="gamma",
            project_paths=[str(project_root)],
            use_rerank=False,
        )
        assert not any(row["content"] == "gamma" for row in after["results"])

    storage = Storage(
        db_path=config.get_project_db_path(project_root, "budget"),
        dims=dims,
    )
    await storage.open()
    try:
        assert await storage.count() == 1
        hashes = await storage.get_file_hashes()
        assert list(hashes) == [str(source_file)]
    finally:
        await storage.close()


@pytest.mark.asyncio
async def test_legacy_local_index_is_migrated_to_centralized_root(tmp_path, monkeypatch):
    project_root = tmp_path / "project"
    project_root.mkdir()
    source_file = project_root / "app.py"
    source_file.write_text("legacy_token\n")

    registry_path = tmp_path / "registry.json"
    monkeypatch.setattr(config, "REGISTRY_PATH", registry_path)

    dims = config.get_tier_dims("budget")
    legacy_db_path = project_root / ".opencode" / "index_budget"
    canonical_db_path = Path(config.get_project_db_path(project_root, "budget"))

    def fake_embed_passages(texts, *, model, dimensions):
        assert dimensions == dims
        return [_vector_for(text, dimensions) for text in texts]

    def fake_embed_query(query, model, dimensions):
        assert dimensions == dims
        return _vector_for(query.strip(), dimensions)

    with patch("opencode_search.chunker.chunk_file", side_effect=_split_lines), \
         patch("opencode_search.embeddings.embed_passages", side_effect=fake_embed_passages), \
         patch("opencode_search.search._embed_query_sync", side_effect=fake_embed_query):
        indexed = await handle_index_project(path=str(project_root), tier="budget")
        assert indexed["status"] == "ok"
        assert canonical_db_path.exists()

        canonical_db_path.parent.mkdir(parents=True, exist_ok=True)
        legacy_db_path.parent.mkdir(parents=True, exist_ok=True)
        canonical_db_path.rename(legacy_db_path)

        registry_path.write_text(
            json.dumps(
                {
                    str(project_root): {
                        "path": str(project_root),
                        "db_path": str(legacy_db_path),
                        "tier": "budget",
                        "dims": dims,
                        "watch": False,
                    }
                },
                indent=2,
            ),
            encoding="utf-8",
        )

        clear_search_cache()
        results = await handle_search_code(
            query="legacy_token",
            project_paths=[str(project_root)],
            use_rerank=False,
        )

    assert any(row["content"] == "legacy_token" for row in results["results"])
    assert canonical_db_path.exists()
    assert not legacy_db_path.exists()


@pytest.mark.asyncio
async def test_search_prefers_source_over_stale_docs(tmp_path, monkeypatch):
    project_root = tmp_path / "project"
    src_dir = project_root / "src"
    docs_dir = project_root / "docs"
    src_dir.mkdir(parents=True)
    docs_dir.mkdir(parents=True)
    # Exclude planning docs from the index (preferred fix; no query hardcoding).
    (project_root / ".opencode-index.yaml").write_text(
        "index:\n"
        "  exclude:\n"
        "    - \"docs/**\"\n",
        encoding="utf-8",
    )

    (src_dir / "config.py").write_text(
        "REGISTRY_PATH = '~/.local/share/opencode-search/projects.json'\n",
        encoding="utf-8",
    )
    (docs_dir / "MIGRATION_PLAN.md").write_text(
        "Registry path is ~/.opencode/projects.json in the legacy design.\n",
        encoding="utf-8",
    )

    registry_path = tmp_path / "registry.json"
    monkeypatch.setattr(config, "REGISTRY_PATH", registry_path)
    dims = config.get_tier_dims("budget")

    def fake_embed_passages(texts, *, model, dimensions):
        assert dimensions == dims
        return [_vector_for(text, dimensions) for text in texts]

    def fake_embed_query(query, model, dimensions):
        assert dimensions == dims
        return _vector_for(query.strip(), dimensions)

    with patch("opencode_search.chunker.chunk_file", side_effect=_split_lines), \
         patch("opencode_search.embeddings.embed_passages", side_effect=fake_embed_passages), \
         patch("opencode_search.search._embed_query_sync", side_effect=fake_embed_query):
        indexed = await handle_index_project(path=str(project_root), tier="budget")
        assert indexed["status"] == "ok"

        clear_search_cache()
        results = await handle_search_code(
            query="Where is the registry path stored?",
            project_paths=[str(project_root)],
            use_rerank=False,
        )

    assert results["results"], "expected search results"
    assert results["results"][0]["path"].endswith("src/config.py")


@pytest.mark.asyncio
async def test_federated_and_symlinked_projects_return_valid_results(tmp_path, monkeypatch):
    root_project = tmp_path / "root-project"
    other_project = tmp_path / "other-project"
    shared_real = tmp_path / "shared-real"
    root_project.mkdir()
    other_project.mkdir()
    shared_real.mkdir()

    (root_project / "main.py").write_text("ROOT_TOKEN = 'root-service'\n", encoding="utf-8")
    (other_project / "api.py").write_text("FEDERATED_TOKEN = 'federated-auth'\n", encoding="utf-8")
    (shared_real / "shared.py").write_text("SYMLINK_TOKEN = 'symlink-shared'\n", encoding="utf-8")
    (root_project / "shared").symlink_to(shared_real, target_is_directory=True)

    registry_path = tmp_path / "registry.json"
    monkeypatch.setattr(config, "REGISTRY_PATH", registry_path)
    dims = config.get_tier_dims("budget")

    def fake_embed_passages(texts, *, model, dimensions):
        assert dimensions == dims
        return [_vector_for(text, dimensions) for text in texts]

    def fake_embed_query(query, model, dimensions):
        assert dimensions == dims
        return _vector_for(query.strip(), dimensions)

    with patch("opencode_search.chunker.chunk_file", side_effect=_split_lines), \
         patch("opencode_search.embeddings.embed_passages", side_effect=fake_embed_passages), \
         patch("opencode_search.search._embed_query_sync", side_effect=fake_embed_query):
        indexed_root = await handle_index_project(path=str(root_project), tier="budget")
        indexed_other = await handle_index_project(path=str(other_project), tier="budget")
        assert indexed_root["status"] == "ok"
        assert indexed_other["status"] == "ok"

        clear_search_cache()
        symlink_results = await handle_search_code(
            query="SYMLINK_TOKEN symlink-shared",
            project_paths=[str(root_project)],
            use_rerank=False,
        )
        clear_search_cache()
        federated_results = await handle_search_code(
            query="ROOT_TOKEN FEDERATED_TOKEN root-service federated-auth",
            project_paths=[str(root_project), str(other_project)],
            use_rerank=False,
            top_k=5,
        )

    assert any("shared/shared.py" in row["path"] for row in symlink_results["results"])
    paths = {row["project_path"] for row in federated_results["results"]}
    assert str(root_project) in paths
    assert str(other_project) in paths
