"""Wiki MCP handlers: generate, ingest, search, and lint wiki pages."""
from __future__ import annotations

import logging
from pathlib import Path
from typing import TYPE_CHECKING, Any

from opencode_search.config import get_project_graph_db_path, get_project_wiki_dir, get_project_raw_dir

if TYPE_CHECKING:
    from opencode_search.enricher.client import LLMClient
    from opencode_search.graph.storage import GraphStorage
    from opencode_search.wiki.storage import WikiStorage

log = logging.getLogger(__name__)


def _get_llm() -> "LLMClient | None":
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


async def handle_wiki_generate(project_path: str) -> dict[str, Any]:
    """Auto-generate wiki pages from code graph."""
    llm = _get_llm()
    if llm is None:
        return {
            "error": "Wiki generation requires OPENCODE_LLM_PROVIDER=ollama|anthropic|openai",
            "project_path": project_path,
        }

    if not llm.is_available():
        return {"error": "LLM provider not reachable", "project_path": project_path}

    gs = _open_graph(project_path)
    if gs is None:
        return {"error": "graph not built", "project_path": project_path}

    wiki = _make_wiki(project_path)

    from opencode_search.wiki.generator import WikiGenerator
    gen = WikiGenerator(llm=llm, wiki=wiki, graph=gs)

    try:
        communities = gs.get_communities()
        pages_created: list[str] = []
        for c in communities:
            try:
                await gen.generate_community_page(c.id)
                pages_created.append(f"community_{c.id}")
            except Exception as exc:  # noqa: BLE001
                log.debug("wiki gen failed for community %d: %s", c.id, exc)

        await gen.generate_index()
        return {
            "status": "ok",
            "project_path": project_path,
            "pages_created": pages_created,
            "total": len(pages_created),
        }
    finally:
        gs.close()


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

    try:
        pages = await gen.ingest_raw_source(source_path, project_path)
        return {
            "status": "ok",
            "source_path": source_path,
            "pages_created": pages,
        }
    except FileNotFoundError as exc:
        return {"error": str(exc), "source_path": source_path}
    finally:
        gs.close()


async def handle_wiki_query(
    query: str,
    project_path: str,
    top_k: int = 5,
) -> dict[str, Any]:
    """Search wiki pages using the existing vector search pipeline."""
    from opencode_search.handlers._query import handle_search_code

    result = await handle_search_code(
        query=query,
        project_paths=[project_path],
        top_k=top_k,
        use_rerank=False,
    )

    # Filter to wiki language only
    if "results" in result:
        wiki_results = [
            r for r in result["results"]
            if r.get("language") in ("wiki", "knowledge_base", "markdown")
        ]
        return {
            "query": query,
            "results": wiki_results,
            "total": len(wiki_results),
        }
    return result


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
