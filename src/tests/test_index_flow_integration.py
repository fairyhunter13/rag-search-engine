"""Integration regression tests for indexing and search state transitions."""
# ruff: noqa: E402

from __future__ import annotations

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
        db_path=str(project_root / ".opencode" / "index_budget"),
        dims=dims,
    )
    await storage.open()
    try:
        assert await storage.count() == 1
        hashes = await storage.get_file_hashes()
        assert list(hashes) == [str(source_file)]
    finally:
        await storage.close()
