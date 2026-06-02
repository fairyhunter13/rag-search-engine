"""Wiki MCP handlers: generate, ingest, search, and lint wiki pages."""
from __future__ import annotations

import asyncio
import hashlib
import logging
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any

from opencode_search.config import (
    DEFAULT_DIMS,
    DEFAULT_EMBED_MODEL,
    get_project_db_path,
    get_project_graph_db_path,
    get_project_raw_dir,
    get_project_wiki_dir,
    load_registry,
)

if TYPE_CHECKING:
    from opencode_search.enricher.client import LLMClient
    from opencode_search.graph.storage import GraphStorage
    from opencode_search.wiki.storage import WikiStorage

log = logging.getLogger(__name__)


def _get_llm() -> LLMClient | None:
    from opencode_search.enricher.client import create_llm_client
    return create_llm_client()


def _open_graph(project_path: str) -> GraphStorage | None:
    from opencode_search.graph.storage import GraphStorage

    db_path = get_project_graph_db_path(project_path)
    if not Path(db_path).exists():
        return None
    gs = GraphStorage(db_path)
    gs.open()
    return gs


def _make_wiki(project_path: str) -> WikiStorage:
    from opencode_search.wiki.storage import WikiStorage
    return WikiStorage(
        wiki_dir=get_project_wiki_dir(project_path),
        raw_dir=get_project_raw_dir(project_path),
    )


def _chunk_id(path: str, position: int) -> int:
    raw = f"{path}:{position}"
    return int(hashlib.sha256(raw.encode()).hexdigest()[:16], 16) % (2**62)


async def _embed_wiki_pages(project_path: str, page_paths: list[Path]) -> int:
    """Embed wiki page files into the project's LanceDB vector index.

    Makes pages searchable via wiki_query and search_code immediately after
    generation or ingestion — without waiting for the file watcher.
    """
    from opencode_search.chunker import chunk_file
    from opencode_search.embeddings import embed_passages
    from opencode_search.storage import ChunkData, Storage

    existing = [p for p in page_paths if p.exists()]
    if not existing:
        return 0

    registry = load_registry()
    entry = registry.get(project_path)
    dims = entry.dims if entry else DEFAULT_DIMS
    db_path = get_project_db_path(project_path)

    all_chunks: list[ChunkData] = []
    now_us = int(time.time() * 1_000_000)

    for page_path in existing:
        try:
            content = page_path.read_text(encoding="utf-8", errors="replace")
            if not content.strip():
                continue
            chunks = await asyncio.to_thread(chunk_file, content, page_path)
            if not chunks:
                continue
            texts = [c.content for c in chunks]
            vectors = await asyncio.to_thread(
                embed_passages, texts, model=DEFAULT_EMBED_MODEL, dimensions=dims
            )
            if len(vectors) != len(chunks):
                continue
            page_str = str(page_path)
            file_hash = hashlib.sha256(content.encode()).hexdigest()
            for i, (c, vec) in enumerate(zip(chunks, vectors, strict=False)):
                all_chunks.append(ChunkData(
                    chunk_id=_chunk_id(page_str, i),
                    path=page_str,
                    file_hash=file_hash,
                    language="wiki",
                    position=i,
                    content=c.content,
                    content_hash=hashlib.sha256(c.content.encode()).hexdigest()[:16],
                    start_line=c.start_line,
                    end_line=c.end_line,
                    vector=vec,
                    created_at=now_us,
                ))
        except Exception as exc:
            log.debug("wiki embed failed for %s: %s", page_path, exc)

    if not all_chunks:
        return 0

    storage = Storage(db_path=db_path, dims=dims)
    await storage.open()
    try:
        await storage.write_chunks(all_chunks)
    finally:
        await storage.close()

    return len(all_chunks)


async def handle_wiki_reindex(project_path: str) -> dict[str, Any]:
    """Embed all existing wiki pages into the vector index.

    Use this to make wiki pages searchable after they were generated without
    the embedding step (e.g. pages created before this feature was added).
    Safe to call multiple times — uses merge_insert (upsert) semantics.
    """
    wiki_dir = get_project_wiki_dir(project_path)
    if not wiki_dir.exists():
        return {"status": "ok", "project_path": project_path, "pages_found": 0, "embedded_chunks": 0}

    page_paths = sorted(wiki_dir.glob("*.md"))
    embedded = await _embed_wiki_pages(project_path, page_paths)
    log.info("wiki_reindex[%s]: embedded %d chunks from %d pages", project_path, embedded, len(page_paths))
    return {
        "status": "ok",
        "project_path": project_path,
        "pages_found": len(page_paths),
        "embedded_chunks": embedded,
    }


async def handle_wiki_generate(
    project_path: str,
    max_communities: int = 200,
    include_federation: bool = False,
    embed: bool = True,
) -> dict[str, Any]:
    """Auto-generate wiki pages from code graph.

    Args:
        max_communities: Cap on the number of community pages to generate.
            Communities are selected largest-first (most architectural coverage).
            Singleton communities are excluded. Default 200. Use a smaller value
            (e.g. 10) for a quick smoke-test on large projects.
        embed: If True (default), embed generated pages into LanceDB immediately.
            Pass False when the caller will handle embedding separately (e.g. the
            document_federation script, which embeds under _EMBED_SEM in stage2).
    """
    import os as _os
    llm = _get_llm()
    if llm is None:
        return {
            "error": "Wiki generation requires OPENCODE_LLM_PROVIDER=ollama|anthropic|openai",
            "project_path": project_path,
        }

    if not llm.is_available():
        return {"error": "LLM provider not reachable", "project_path": project_path}

    cap = int(_os.environ.get("OPENCODE_WIKI_MAX_COMMUNITIES", str(max_communities)))

    # Build effective project list (root + indexed federation members if requested)
    from opencode_search.config import load_registry
    registry = load_registry()
    paths_to_generate = [project_path]
    if include_federation:
        from opencode_search.handlers._federation import _expand_with_federation
        paths_to_generate = _expand_with_federation([project_path], registry)

    from opencode_search.wiki.generator import WikiGenerator
    all_pages_created: list[str] = []
    results_per_path: list[dict] = []

    for path in paths_to_generate:
        gs = _open_graph(path)
        if gs is None:
            results_per_path.append({"path": path, "error": "graph not built"})
            continue
        wiki = _make_wiki(path)
        gen = WikiGenerator(llm=llm, wiki=wiki, graph=gs)
        pages_created: list[str] = []
        try:
            communities = gs.get_communities(
                limit=cap, min_node_count=2, order_by_size=True
            )
            for c in communities:
                try:
                    await gen.generate_community_page(c.id)
                    pages_created.append(f"community_{c.id}")
                except Exception as exc:
                    log.debug("wiki gen failed for community %d in %s: %s", c.id, path, exc)
            await gen.generate_index()
        finally:
            gs.close()
        all_pages_created.extend(pages_created)

        # Embed generated pages into LanceDB so wiki_query finds them immediately.
        # Skip when embed=False — caller will embed under a GPU semaphore instead.
        wiki_dir = get_project_wiki_dir(path)
        page_file_paths = [wiki_dir / f"{name}.md" for name in pages_created]
        embedded = 0
        if embed:
            try:
                embedded = await _embed_wiki_pages(path, page_file_paths)
                log.info("wiki_generate[%s]: embedded %d wiki chunks", path, embedded)
            except Exception as exc:
                log.warning("wiki_generate[%s]: embedding failed: %s", path, exc)

        results_per_path.append({"path": path, "pages_created": len(pages_created), "embedded_chunks": embedded})

    result: dict = {
        "status": "ok",
        "project_path": project_path,
        "pages_created": all_pages_created,
        "total": len(all_pages_created),
    }
    if include_federation and len(paths_to_generate) > 1:
        result["federation_results"] = results_per_path
    return result


async def handle_wiki_ingest(source_path: str, project_path: str) -> dict[str, Any]:
    """Ingest a raw document into the wiki."""
    llm = _get_llm()
    if llm is None:
        return {
            "error": "Wiki ingest requires OPENCODE_LLM_PROVIDER=ollama|anthropic|openai",
            "source_path": source_path,
        }

    if not llm.is_available():
        return {"error": "LLM provider not reachable", "source_path": source_path}

    src = Path(source_path)
    if not src.exists():
        return {"error": f"Source not found: {source_path}", "source_path": source_path}

    gs = _open_graph(project_path)
    wiki = _make_wiki(project_path)

    from opencode_search.wiki.generator import WikiGenerator
    if gs is None:
        # Still ingest even without graph
        from opencode_search.graph.storage import GraphStorage
        db_path = get_project_graph_db_path(project_path)
        gs = GraphStorage(db_path)
        gs.open()

    gen = WikiGenerator(llm=llm, wiki=wiki, graph=gs)

    pages: list[str] = []
    try:
        pages = await gen.ingest_raw_source(source_path, project_path)
    except FileNotFoundError as exc:
        return {"error": str(exc), "source_path": source_path}
    finally:
        gs.close()

    # Embed the ingested pages into LanceDB so wiki_query finds them immediately
    wiki_dir = get_project_wiki_dir(project_path)
    page_file_paths = [wiki_dir / f"{name}.md" for name in pages]
    try:
        await _embed_wiki_pages(project_path, page_file_paths)
    except Exception as exc:
        log.debug("wiki_ingest: embedding failed for %s: %s", source_path, exc)

    return {
        "status": "ok",
        "source_path": source_path,
        "pages_created": pages,
    }


async def handle_wiki_query(
    query: str,
    project_path: str,
    top_k: int = 5,
) -> dict[str, Any]:
    """Search wiki pages using a language-filtered vector search.

    Searches only within wiki-language chunks so results are never buried
    by the much larger code chunk population.
    """
    from opencode_search.config import (
        DEFAULT_DIMS,
        DEFAULT_EMBED_MODEL,
        get_project_db_path,
        load_registry,
    )
    from opencode_search.embeddings import embed_query
    from opencode_search.storage import Storage

    registry = load_registry()
    entry = registry.get(project_path)
    if entry is None:
        return {"query": query, "results": [], "total": 0, "error": "project not indexed"}

    dims = entry.dims if entry else DEFAULT_DIMS
    db_path = get_project_db_path(project_path)

    try:
        query_vec = await asyncio.to_thread(embed_query, query, model=DEFAULT_EMBED_MODEL, dimensions=dims)
    except Exception as exc:
        log.warning("wiki_query: embed failed: %s", exc)
        return {"query": query, "results": [], "total": 0}

    storage = Storage(db_path=db_path, dims=dims)
    await storage.open()
    try:
        rows = await storage.search_vector_language(query_vec, language="wiki", limit=top_k)
    finally:
        await storage.close()

    results = [
        {
            "path": r.get("path", ""),
            "content": r.get("content", ""),
            "language": r.get("language", "wiki"),
            "start_line": int(r.get("start_line", 0)),
            "end_line": int(r.get("end_line", 0)),
            "score": round(float(r.get("_score", 0.0)), 4),
            "project_path": project_path,
        }
        for r in rows
    ]
    return {"query": query, "results": results, "total": len(results)}


async def handle_wiki_lint(project_path: str) -> dict[str, Any]:
    """Health check the wiki."""
    wiki = _make_wiki(project_path)

    from opencode_search.wiki.generator import WikiGenerator
    gs = _open_graph(project_path)
    if gs is None:
        # Lint wiki without graph context
        from opencode_search.graph.storage import GraphStorage
        db_path = get_project_graph_db_path(project_path)
        gs = GraphStorage(db_path)
        gs.open()

    llm = _get_llm()
    if llm is None:
        # Create a dummy LLM for lint (no LLM calls needed)
        from unittest.mock import MagicMock
        llm = MagicMock()

    gen = WikiGenerator(llm=llm, wiki=wiki, graph=gs)
    try:
        return await gen.lint()
    finally:
        gs.close()
