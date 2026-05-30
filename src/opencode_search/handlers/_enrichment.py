"""LLM enrichment handlers: symbol intent, community enrichment."""
from __future__ import annotations

import asyncio
import logging
import os
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


_DEFAULT_ENRICH_MAX_COMMUNITIES: int = int(
    os.environ.get("OPENCODE_ENRICH_MAX_COMMUNITIES", "200")
)


async def handle_enrich_project(
    project_path: str,
    scope: str = "communities",
    max_communities: int | None = None,
) -> dict[str, Any]:
    """Trigger LLM enrichment. scope: symbols|communities|wiki|all.

    Args:
        max_communities: Cap on the number of communities to enrich in this call.
            Defaults to OPENCODE_ENRICH_MAX_COMMUNITIES env var (default 200).
            Communities are processed largest-first (by node_count). Set to a
            small value (e.g. 10) for a quick smoke-test on a large project.
    """
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

    cap = max_communities if max_communities is not None else _DEFAULT_ENRICH_MAX_COMMUNITIES
    t0 = time.perf_counter()
    enriched_symbols = 0
    enriched_communities = 0

    try:
        if scope in ("symbols", "all"):
            enriched_symbols = await _enrich_symbols(gs, llm)
        if scope in ("communities", "all"):
            enriched_communities = await _enrich_communities(gs, llm, max_communities=cap)
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


_LLM_CONCURRENCY: int = int(os.environ.get("OPENCODE_LLM_CONCURRENCY", "2"))


async def _enrich_communities(gs: Any, llm: Any, max_communities: int = 200) -> int:
    """Generate titles and summaries for communities without them.

    Args:
        max_communities: Hard cap on communities processed per call. Communities
            are selected largest-first (by node_count) to maximize architectural
            coverage per LLM call. Singletons (node_count == 1) are skipped
            as they carry no structural information.
    """
    # Fetch only unenriched, non-singleton communities largest-first.
    # This replaces the old get_communities() that loaded ALL rows (including
    # 163k singletons on large projects) and filtered in Python.
    all_unenriched = [
        c for c in gs.get_communities(min_node_count=2, order_by_size=True)
        if not c.title
    ]
    communities = all_unenriched[:max_communities]
    if not communities:
        return 0

    log.info(
        "enriching %d communities (max=%d, total unenriched=%d)",
        len(communities), max_communities, len(all_unenriched),
    )

    # Pre-fetch all node summaries on the current thread — SQLite connections
    # are not thread-safe across threads, so graph reads must stay here.
    community_summaries: list[tuple[Any, list[str]]] = []
    for community in communities:
        nodes = gs.get_community_nodes(community.id)
        summaries = [
            f"{n.qualified_name} ({n.kind})"
            + (f": {n.docstring[:80]}" if n.docstring else "")
            for n in nodes[:20]
        ]
        if summaries:
            community_summaries.append((community, summaries))

    if not community_summaries:
        return 0

    # Run LLM calls concurrently — they are the only blocking I/O here.
    # All SQLite writes happen after gather() on the calling thread to avoid
    # concurrent-write races on the shared connection.
    sem = asyncio.Semaphore(_LLM_CONCURRENCY)

    async def _call_llm(community: Any, summaries: list[str]) -> tuple[Any, str, str] | None:
        async with sem:
            try:
                title, summary = await asyncio.to_thread(llm.community_summary, summaries)
                return community, title, summary
            except Exception as exc:  # noqa: BLE001
                log.debug("community LLM call failed for %d: %s", community.id, exc)
                return None

    results = await asyncio.gather(*[_call_llm(c, s) for c, s in community_summaries])

    # Serialize all DB writes on the calling thread (no concurrency needed here)
    count = 0
    now = datetime.now(timezone.utc).isoformat()
    for item in results:
        if item is None:
            continue
        community, title, summary = item
        community.title = title
        community.summary = summary
        community.generated_at = now
        gs.upsert_community(community)
        count += 1
    return count
