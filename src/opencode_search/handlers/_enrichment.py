"""LLM enrichment handlers: symbol intent, community enrichment."""
from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any

from opencode_search.config import get_project_graph_db_path

if TYPE_CHECKING:
    from opencode_search.enricher.client import LLMClient
    from opencode_search.graph.storage import GraphStorage

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


async def handle_enrich_project(
    project_path: str,
    scope: str = "communities",
) -> dict[str, Any]:
    """Trigger LLM enrichment. scope: symbols|communities|wiki|all."""
    llm = _get_llm()
    if llm is None:
        return {
            "error": "LLM enrichment requires OPENCODE_LLM_PROVIDER=ollama|anthropic|openai",
            "project_path": project_path,
        }

    if not llm.is_available():
        return {
            "error": "LLM provider is not reachable. Check OPENCODE_LLM_BASE_URL / API key.",
            "project_path": project_path,
        }

    gs = _open_graph(project_path)
    if gs is None:
        return {"error": "graph not built", "project_path": project_path}

    t0 = time.perf_counter()
    enriched_symbols = 0
    enriched_communities = 0

    try:
        if scope in ("symbols", "all"):
            enriched_symbols = await _enrich_symbols(gs, llm)
        if scope in ("communities", "all"):
            enriched_communities = await _enrich_communities(gs, llm)
    finally:
        gs.close()

    return {
        "status": "ok",
        "project_path": project_path,
        "scope": scope,
        "enriched_symbols": enriched_symbols,
        "enriched_communities": enriched_communities,
        "elapsed_s": round(time.perf_counter() - t0, 2),
    }


async def handle_get_symbol_intent(name: str, project_path: str) -> dict[str, Any]:
    """Get LLM-generated intent for a function or class. Returns cached if fresh."""
    llm = _get_llm()
    if llm is None:
        return {
            "error": "LLM enrichment requires OPENCODE_LLM_PROVIDER=ollama|anthropic|openai",
            "name": name,
        }

    gs = _open_graph(project_path)
    if gs is None:
        return {"error": "graph not built", "name": name}

    try:
        node = gs.get_node(name)
        if node is None:
            return {"error": f"symbol '{name}' not found"}

        # Return cached intent if present
        if node.intent:
            return {
                "name": name,
                "qualified_name": node.qualified_name,
                "intent": node.intent,
                "intent_at": node.intent_at,
                "cached": True,
            }

        # Generate via LLM
        if not llm.is_available():
            return {"error": "LLM provider not reachable", "name": name}

        intent = await asyncio.to_thread(
            llm.symbol_intent,
            node.name,
            node.signature or node.qualified_name,
            node.docstring,
        )
        now = datetime.now(timezone.utc).isoformat()
        gs.set_node_intent(node.id, intent, now)

        return {
            "name": name,
            "qualified_name": node.qualified_name,
            "intent": intent,
            "intent_at": now,
            "cached": False,
        }
    finally:
        gs.close()


async def _enrich_symbols(gs: Any, llm: Any) -> int:
    """Enrich all nodes without intent."""
    nodes = [n for n in gs.all_nodes() if not n.intent and n.kind in ("function", "method")]
    count = 0
    for node in nodes[:100]:  # cap per call to avoid long-running enrichment
        try:
            intent = await asyncio.to_thread(
                llm.symbol_intent,
                node.name,
                node.signature or node.qualified_name,
                node.docstring,
            )
            now = datetime.now(timezone.utc).isoformat()
            gs.set_node_intent(node.id, intent, now)
            count += 1
        except Exception as exc:  # noqa: BLE001
            log.debug("intent generation failed for %s: %s", node.name, exc)
    return count


async def _enrich_communities(gs: Any, llm: Any) -> int:
    """Generate titles and summaries for communities without them."""
    communities = [c for c in gs.get_communities() if not c.title]
    count = 0
    for community in communities:
        nodes = gs.get_community_nodes(community.id)
        summaries = [
            f"{n.qualified_name} ({n.kind})"
            + (f": {n.docstring[:80]}" if n.docstring else "")
            for n in nodes[:20]
        ]
        if not summaries:
            continue
        try:
            title, summary = await asyncio.to_thread(llm.community_summary, summaries)
            community.title = title
            community.summary = summary
            community.generated_at = datetime.now(timezone.utc).isoformat()
            gs.upsert_community(community)
            count += 1
        except Exception as exc:  # noqa: BLE001
            log.debug("community enrichment failed for %d: %s", community.id, exc)
    return count
