"""Wiki MCP handlers: ingest, search, and lint wiki pages."""
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
    get_project_raw_dir,
    get_project_wiki_dir,
    load_registry,
)

if TYPE_CHECKING:
    from opencode_search.wiki.storage import WikiStorage

log = logging.getLogger(__name__)


def _get_llm():
    from opencode_search.enricher.client import create_llm_client
    return create_llm_client()


def _make_wiki(project_path: str) -> WikiStorage:
    from opencode_search.wiki.storage import WikiStorage
    return WikiStorage(
        wiki_dir=get_project_wiki_dir(project_path),
        raw_dir=get_project_raw_dir(project_path),
    )


def _chunk_id(path: str, position: int) -> int:
    raw = f"{path}:{position}"
    return int(hashlib.sha256(raw.encode()).hexdigest()[:16], 16) % (2**62)


def _safe_name(name: str) -> str:
    return "".join(c if (c.isalnum() or c in "_-") else "_" for c in name).strip("_")


async def _embed_wiki_pages(project_path: str, page_paths: list[Path]) -> int:
    """Embed wiki page files into the project's LanceDB vector index."""
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

    wiki = _make_wiki(project_path)
    wiki.register_raw_source(source_path)
    content = src.read_text(encoding="utf-8", errors="replace")

    wiki_content = await asyncio.to_thread(llm.raw_doc_to_wiki, content, src.name)
    page_name = _safe_name(src.stem)
    wiki.write_wiki_page(page_name, wiki_content)
    wiki.append_log(f"Ingested raw source: {src.name} → {page_name}.md")

    # Regenerate index
    pages = wiki.list_wiki_pages()
    lines = ["# Wiki Index\n"]
    for name in sorted(pages):
        lines.append(f"- [{name}]({name}.md)")
    wiki.write_index("\n".join(lines))

    wiki_dir = get_project_wiki_dir(project_path)
    try:
        await _embed_wiki_pages(project_path, [wiki_dir / f"{page_name}.md"])
    except Exception as exc:
        log.debug("wiki_ingest: embedding failed for %s: %s", source_path, exc)

    return {
        "status": "ok",
        "source_path": source_path,
        "pages_created": [page_name],
    }


async def handle_wiki_query(
    query: str,
    project_path: str,
    top_k: int = 5,
) -> dict[str, Any]:
    """Search wiki pages using a language-filtered vector search."""
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
        from opencode_search.search import _GPU_INFER_EXECUTOR
        loop = asyncio.get_event_loop()
        query_vec = await loop.run_in_executor(
            _GPU_INFER_EXECUTOR,
            lambda: embed_query(query, model=DEFAULT_EMBED_MODEL, dimensions=dims),
        )
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
    """Health check the wiki: orphaned pages, empty pages."""
    wiki = _make_wiki(project_path)
    pages = wiki.list_wiki_pages()
    issues: list[str] = []

    index_content = ""
    index_path = wiki.index_path()
    if index_path.exists():
        index_content = index_path.read_text(encoding="utf-8")

    orphans = []
    empty_pages = []
    for name in pages:
        if name not in index_content and name != "index":
            orphans.append(name)
        content = wiki.read_wiki_page(name) or ""
        if not content.strip():
            empty_pages.append(name)

    if orphans:
        issues.append(f"Orphaned pages: {orphans}")
    if empty_pages:
        issues.append(f"Empty pages: {empty_pages}")

    return {
        "healthy": len(issues) == 0,
        "total_pages": len(pages),
        "orphans": orphans,
        "empty_pages": empty_pages,
        "issues": issues,
    }
