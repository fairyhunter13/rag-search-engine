"""LLM enrichment handlers: symbol intent, community enrichment."""
from __future__ import annotations

import asyncio
import logging
import os
import time
from collections import defaultdict
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

from opencode_search.config import get_project_graph_db_path

if TYPE_CHECKING:
    from opencode_search.enricher.client import LLMClient
    from opencode_search.graph.storage import GraphStorage

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


_DEFAULT_ENRICH_MAX_COMMUNITIES: int = int(
    os.environ.get("OPENCODE_ENRICH_MAX_COMMUNITIES", "200")
)


async def handle_enrich_project(
    project_path: str,
    scope: str = "communities",
    max_communities: int | None = None,
    community_ids: list[int] | None = None,
    include_federation: bool = False,
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

    # Build effective project list (root + indexed federation members if requested)
    from opencode_search.config import load_registry
    registry = load_registry()
    paths_to_enrich = [project_path]
    if include_federation:
        from opencode_search.handlers._federation import _expand_with_federation
        paths_to_enrich = _expand_with_federation([project_path], registry)

    cap = max_communities if max_communities is not None else _DEFAULT_ENRICH_MAX_COMMUNITIES
    t0 = time.perf_counter()
    total_symbols = 0
    total_communities = 0
    results_per_path: list[dict[str, Any]] = []

    root_path = paths_to_enrich[0] if paths_to_enrich else project_path
    for path in paths_to_enrich:
        gs = _open_graph(path)
        if gs is None:
            results_per_path.append({"path": path, "error": "graph not built"})
            continue
        enriched_symbols = 0
        enriched_communities = 0
        # community_ids is scoped to the root project — federation members
        # use different community ID namespaces, so pass None for them
        # to get a full unenriched-communities refresh.
        this_community_ids = community_ids if path == root_path else None
        try:
            if scope in ("symbols", "all"):
                enriched_symbols = await _enrich_symbols(gs, llm)
            if scope in ("communities", "all"):
                enriched_communities = await _enrich_communities(
                    gs, llm, max_communities=cap, community_ids=this_community_ids,
                )
        finally:
            gs.close()
        total_symbols += enriched_symbols
        total_communities += enriched_communities
        results_per_path.append({
            "path": path,
            "enriched_symbols": enriched_symbols,
            "enriched_communities": enriched_communities,
        })

    result: dict[str, Any] = {
        "status": "ok",
        "project_path": project_path,
        "scope": scope,
        "enriched_symbols": total_symbols,
        "enriched_communities": total_communities,
        "elapsed_s": round(time.perf_counter() - t0, 2),
    }
    if include_federation and len(paths_to_enrich) > 1:
        result["federation_results"] = results_per_path
    return result


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
        now = datetime.now(UTC).isoformat()
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
    """Enrich all function/method nodes without intent, using batched LLM calls."""
    nodes = [n for n in gs.all_nodes() if not n.intent and n.kind in ("function", "method")]
    if not nodes:
        return 0
    sem = asyncio.Semaphore(_LLM_CONCURRENCY)
    count = 0
    now = datetime.now(UTC).isoformat()

    async def _process_batch(batch: list[Any]) -> int:
        items = [(n.name, n.signature or n.qualified_name, n.docstring) for n in batch]
        async with sem:
            try:
                intents = await asyncio.to_thread(llm.symbol_intent_batch, items)
            except Exception as exc:
                log.debug("batch intent failed: %s", exc)
                return 0
        enriched = 0
        for node, intent in zip(batch, intents, strict=False):
            if intent:
                gs.set_node_intent(node.id, intent, now)
                enriched += 1
        return enriched

    batches = [nodes[i:i + _SYMBOL_INTENT_BATCH_SIZE] for i in range(0, len(nodes), _SYMBOL_INTENT_BATCH_SIZE)]
    results = await asyncio.gather(*[_process_batch(b) for b in batches])
    count = sum(results)
    return count


async def handle_enrich_symbols_background(project_path: str) -> dict[str, Any]:
    """Enrich all unenriched function/method nodes in batches. Resumable + idempotent.

    Runs as a background job — the pipeline returns immediately after submitting this.
    Progress is written to _enrich_symbols_progress[project_path] every 500 nodes
    and is available via GET /api/jobs/{id}.
    """
    llm = _get_llm()
    if llm is None:
        return {"error": "LLM not available", "project_path": project_path}
    if not llm.is_available():
        return {"error": "LLM provider not reachable", "project_path": project_path}

    gs = _open_graph(project_path)
    if gs is None:
        return {"error": "graph not built", "project_path": project_path}

    t0 = time.perf_counter()
    try:
        all_nodes = [n for n in gs.all_nodes() if n.kind in ("function", "method")]
        unenriched = [n for n in all_nodes if not n.intent]
        total = len(all_nodes)
        already_done = total - len(unenriched)

        if not unenriched:
            _enrich_symbols_progress[project_path] = {"enriched": total, "total": total}
            return {"status": "ok", "enriched": 0, "total": total, "elapsed_s": 0.0}

        log.info(
            "handle_enrich_symbols_background: %s — %d/%d to enrich",
            project_path, len(unenriched), total,
        )
        _enrich_symbols_progress[project_path] = {
            "enriched": already_done, "total": total,
        }

        sem = asyncio.Semaphore(_LLM_CONCURRENCY)
        enriched_count = already_done
        now = datetime.now(UTC).isoformat()

        async def _process_batch(batch: list[Any]) -> int:
            items = [(n.name, n.signature or n.qualified_name, n.docstring) for n in batch]
            async with sem:
                try:
                    intents = await asyncio.to_thread(llm.symbol_intent_batch, items)
                except Exception as exc:
                    log.debug("symbol_intent_batch failed: %s", exc)
                    return 0
            done = 0
            for node, intent in zip(batch, intents, strict=False):
                if intent:
                    gs.set_node_intent(node.id, intent, now)
                    done += 1
            return done

        batches = [
            unenriched[i:i + _SYMBOL_INTENT_BATCH_SIZE]
            for i in range(0, len(unenriched), _SYMBOL_INTENT_BATCH_SIZE)
        ]
        for i in range(0, len(batches), _LLM_CONCURRENCY):
            chunk = batches[i:i + _LLM_CONCURRENCY]
            results = await asyncio.gather(*[_process_batch(b) for b in chunk])
            enriched_count += sum(results)
            if enriched_count % 500 < _SYMBOL_INTENT_BATCH_SIZE * _LLM_CONCURRENCY:
                _enrich_symbols_progress[project_path] = {
                    "enriched": enriched_count, "total": total,
                }
                log.info(
                    "enrich_symbols_background: %s — %d/%d enriched",
                    project_path, enriched_count, total,
                )

        _enrich_symbols_progress[project_path] = {
            "enriched": enriched_count, "total": total,
        }
        elapsed = round(time.perf_counter() - t0, 2)
        log.info(
            "enrich_symbols_background done: %s — %d new intents in %.1fs",
            project_path, enriched_count - already_done, elapsed,
        )
        return {
            "status": "ok",
            "enriched": enriched_count - already_done,
            "total": total,
            "elapsed_s": elapsed,
        }
    finally:
        gs.close()


_LLM_CONCURRENCY: int = int(os.environ.get("OPENCODE_LLM_CONCURRENCY", "2"))
_SYMBOL_INTENT_BATCH_SIZE: int = int(os.environ.get("OPENCODE_SYMBOL_BATCH_SIZE", "20"))

# Per-project progress for long-running background enrichment — polled via /api/jobs
_enrich_symbols_progress: dict[str, dict[str, int]] = {}

_VALID_SEMANTIC_TYPES = frozenset([
    "api_boundary", "data_model", "business_process", "business_rule",
    "feature", "infrastructure", "utility",
])

_SEMANTIC_TYPE_SYSTEM = (
    "Classify the following code community into exactly ONE semantic type.\n\n"
    "Types:\n"
    "  api_boundary    — HTTP/gRPC handlers, routers, middleware, API endpoints, interceptors\n"
    "  data_model      — entities, schemas, DTOs, database models, domain objects\n"
    "  business_process — workflows, pipelines, jobs, queues, event flows, schedulers\n"
    "  business_rule   — validation logic, policies, constraints, guards, compliance checks\n"
    "  feature         — product features, user-facing functionality, domain capabilities\n"
    "  infrastructure  — deployment, config, caching, monitoring, messaging, external adapters\n"
    "  utility         — helpers, common utilities, shared/generic code\n\n"
    "Respond with ONLY the type name, nothing else."
)


async def _classify_semantic_type_llm(title: str, summary: str, llm: Any) -> str:
    """LLM-based semantic type classification — accurate, no keyword heuristics."""
    import asyncio
    try:
        messages = [
            {"role": "system", "content": _SEMANTIC_TYPE_SYSTEM},
            {"role": "user", "content": f"Title: {title}\nSummary: {summary[:400]}"},
        ]
        raw = await asyncio.to_thread(llm.chat, messages, max_tokens=16)
        text = (raw or "").strip().lower().split()[0] if raw else ""
        if text in _VALID_SEMANTIC_TYPES:
            return text
    except Exception:
        pass
    return "utility"


async def _backfill_semantic_types(gs: Any, all_communities: list[Any], llm: Any) -> int:
    """LLM-classify semantic_type for enriched communities that have none."""
    import asyncio
    missing = [c for c in all_communities if c.title and not c.semantic_type]
    if not missing:
        return 0
    sem = asyncio.Semaphore(_LLM_CONCURRENCY)

    async def _classify_one(c: Any) -> None:
        async with sem:
            c.semantic_type = await _classify_semantic_type_llm(
                c.title or "", c.summary or "", llm
            )
            gs.upsert_community(c)

    await asyncio.gather(*[_classify_one(c) for c in missing])
    return len(missing)


async def _enrich_communities(
    gs: Any,
    llm: Any,
    max_communities: int = 200,
    community_ids: list[int] | None = None,
) -> int:
    """Generate titles and summaries for communities.

    Args:
        max_communities: Hard cap on communities processed per call. Communities
            are selected largest-first (by node_count).  Singletons are skipped.
        community_ids: When set, only enrich these specific community IDs (used
            for incremental refresh after file changes). Already-enriched
            communities in this set are re-enriched to pick up code changes.
    """
    all_communities = gs.get_communities(min_node_count=2, order_by_size=True)

    if community_ids is not None:
        # Selective incremental refresh: include target communities regardless
        # of whether they were previously enriched.
        id_set = set(community_ids)
        communities = [c for c in all_communities if c.id in id_set][:max_communities]
    else:
        # LLM enrichment for communities without title/summary.
        communities = [c for c in all_communities if not c.title][:max_communities]

    if not communities:
        # LLM backfill: assign semantic_type for enriched communities that never got it
        # (enriched before semantic_type field was introduced).
        backfill_count = await _backfill_semantic_types(gs, all_communities, llm)
        if backfill_count:
            log.info("backfilled semantic_type for %d communities via LLM", backfill_count)
        return backfill_count

    log.info(
        "enriching %d communities (max=%d, selective=%s)",
        len(communities), max_communities, community_ids is not None,
    )

    # Pre-fetch node summaries and sample actual code — SQLite reads must stay
    # on this thread; code file reads happen in the same loop.
    # Yield to the event loop every 50 communities so HTTP requests are not
    # blocked during large pre-load passes (otherwise 10k communities would
    # block the loop for ~30s, causing ConnectError on concurrent requests).
    community_data: list[tuple[Any, list[str], list[tuple[str, str]]]] = []
    for i, community in enumerate(communities):
        if i > 0 and i % 50 == 0:
            await asyncio.sleep(0)
        nodes = gs.get_community_nodes(community.id)
        summaries = [
            f"{n.qualified_name} ({n.kind})"
            + (f": {n.docstring[:80]}" if n.docstring else "")
            for n in nodes[:20]
        ]
        code_samples = _sample_community_code(nodes[:5])
        if summaries:
            community_data.append((community, summaries, code_samples))

    if not community_data:
        return 0

    # Run LLM calls concurrently (semaphore limits parallelism).
    # Write each result to DB immediately after completion so partial progress
    # is preserved if the process is killed before all calls finish.
    sem = asyncio.Semaphore(_LLM_CONCURRENCY)
    count = 0

    async def _call_llm(
        community: Any,
        summaries: list[str],
        code_samples: list[tuple[str, str]],
    ) -> None:
        nonlocal count
        async with sem:
            try:
                title, summary, semantic_type = await asyncio.to_thread(
                    llm.community_summary, summaries, code_samples,
                )
            except Exception as exc:
                log.warning("community LLM call failed for community %d: %s", community.id, exc)
                return
            now = datetime.now(UTC).isoformat()
            community.title = title
            community.summary = summary
            community.semantic_type = semantic_type
            community.generated_at = now
            gs.upsert_community(community)
            count += 1

    # Yield GPU to interactive queries before the L1 enrichment gather.
    from opencode_search.daemon_runtime import yield_while_busy
    await yield_while_busy()
    await asyncio.gather(*[_call_llm(c, s, cs) for c, s, cs in community_data])
    return count


async def handle_enrich_hierarchy(
    project_path: str,
    max_level: int | None = None,
) -> dict:
    """Enrich all hierarchy levels above level-1 using their children's summaries.

    Generates LLM titles+summaries for macro-communities by synthesizing
    from their children's summaries (bottom-up, like GraphRAG's rollup).
    Call after build(action="hierarchy") to get enriched architecture domains.
    """
    from opencode_search.enricher import create_llm_client
    from opencode_search.graph.storage import CommunityData
    from opencode_search.handlers._graph import _open_graph

    gs = _open_graph(project_path)
    if gs is None:
        return {"error": "Project not indexed"}

    try:
        top_level = gs.get_max_community_level()
        if top_level <= 1:
            return {"status": "ok", "message": "No hierarchy built yet. Run build(action='hierarchy') first.", "enriched": 0}

        llm = await asyncio.to_thread(create_llm_client)
        total_enriched = 0
        effective_max = max_level or top_level

        for level in range(2, effective_max + 1):
            level_comms = gs.get_communities(level=level, order_by_size=True)
            unenriched = [c for c in level_comms if not c.title or c.title == f"Community {c.id}"]
            if not unenriched:
                continue

            # Pre-load child communities ONCE per level (not per-community)
            # to avoid O(n×m) repeated full-table scans.
            child_level = level - 1
            child_comms = gs.get_communities(level=child_level)
            children_by_parent: dict[int, list[CommunityData]] = defaultdict(list)
            for c in child_comms:
                if c.parent_community_id is not None and c.title and c.summary:
                    children_by_parent[c.parent_community_id].append(c)

            # Hierarchy enrichment is text-only (summaries → title), no code I/O,
            # so we can safely use higher concurrency than level-1 enrichment.
            # Process in batches of _HIER_BATCH_SIZE so progress is saved
            # incrementally — avoids losing all work if the process is killed.
            # Cap at OLLAMA_NUM_PARALLEL (1) + 1 queue slot = 2 max.
            # Old value (_LLM_CONCURRENCY * 2 = 4) caused sustained 100% GPU
            # load across the full enrichment run, contributing to thermal crashes.
            _hier_concurrency = min(_LLM_CONCURRENCY, 2)
            _hier_batch_size = 50
            _sem = asyncio.Semaphore(_hier_concurrency)

            async def _enrich_macro(
                comm: CommunityData,
                _map: dict = children_by_parent,
                _s: asyncio.Semaphore = _sem,
            ) -> tuple[CommunityData, str, str, str] | None:
                children = _map.get(comm.id, [])
                if not children:
                    return None
                child_summaries = [
                    f"[{c.title}] {c.summary}" for c in children[:15]
                ]
                if not child_summaries:
                    return None
                async with _s:
                    try:
                        title, summary, semantic_type = await asyncio.to_thread(
                            llm.community_summary,
                            child_summaries,
                            None,
                        )
                        return comm, title, summary, semantic_type
                    except Exception as exc:
                        log.warning("macro community LLM failed for %d: %s", comm.id, exc)
                        return None

            # Process in batches: write after each batch so partial progress
            # is visible in the DB and survives if the process is killed.
            for batch_start in range(0, len(unenriched), _hier_batch_size):
                # Yield GPU to interactive queries between batches.
                from opencode_search.daemon_runtime import yield_while_busy
                await yield_while_busy()
                batch = unenriched[batch_start:batch_start + _hier_batch_size]
                log.info(
                    "enrich_hierarchy[L%d]: batch %d/%d (%d communities)",
                    level,
                    batch_start // _hier_batch_size + 1,
                    (len(unenriched) + _hier_batch_size - 1) // _hier_batch_size,
                    len(batch),
                )
                results = await asyncio.gather(*[_enrich_macro(c) for c in batch])
                now = datetime.now(UTC).isoformat()
                updated_batch: list[CommunityData] = []
                for item in results:
                    if item is None:
                        continue
                    comm, title, summary, semantic_type = item
                    comm.title = title
                    comm.summary = summary
                    if semantic_type:
                        comm.semantic_type = semantic_type
                    comm.generated_at = now
                    updated_batch.append(comm)
                    total_enriched += 1
                if updated_batch:
                    gs.upsert_communities_batch(updated_batch)
                    log.info(
                        "enrich_hierarchy[L%d]: wrote %d/%d (total=%d)",
                        level, len(updated_batch),
                        len(unenriched), total_enriched,
                    )

        return {"status": "ok", "enriched": total_enriched, "max_level": top_level}
    finally:
        import contextlib
        with contextlib.suppress(Exception):
            gs.close()


_MAX_CODE_SAMPLE_FILES = 3
_MAX_CODE_SAMPLE_BYTES = 800


def _sample_community_code(nodes: list[Any]) -> list[tuple[str, str]]:
    """Read code snippets from the top nodes' source files for LLM context.

    Returns a list of (relative_path, code_snippet) tuples — at most
    _MAX_CODE_SAMPLE_FILES entries, each capped at _MAX_CODE_SAMPLE_BYTES.
    """
    seen_files: dict[str, str] = {}  # file_path → snippet
    for node in nodes:
        if len(seen_files) >= _MAX_CODE_SAMPLE_FILES:
            break
        if not node.file or node.file in seen_files:
            continue
        try:
            file_path = Path(node.file)
            if not file_path.is_file():
                continue
            text = file_path.read_text(encoding="utf-8", errors="replace")
            # Extract the relevant block by line range if available
            if node.start_line and node.end_line:
                lines = text.splitlines()
                start = max(0, node.start_line - 1)
                end = min(len(lines), node.end_line)
                snippet = "\n".join(lines[start:end])
            else:
                snippet = text
            snippet = snippet[:_MAX_CODE_SAMPLE_BYTES]
            # Use a short relative name for readability in the prompt
            seen_files[node.file] = snippet
        except Exception:
            continue
    return list(seen_files.items())
