"""MCP server for opencode-search — stdio and streamable-HTTP transports via FastMCP.

Exposes 5 intent-focused tools (v3 API, June 2026 Phase 100):
  search   — find code/docs by query (scope: code|docs|all|similar)
  ask      — answer architectural questions (scope: architecture|wiki|all)
  graph    — code graph: definition|callers|callees|impact|path
  overview — project structure|communities|status|projects|metrics
  index    — flag project for indexing (enabled=True) or delete all data (enabled=False)

The daemon handles ALL indexing, KB building, watching, federation, and maintenance
automatically. The MCP is strictly read-only + the single `index` flag tool.

Usage:
  opencode-search mcp           # stdio (for AI assistants)
  python -m opencode_search mcp # equivalent

Startup sequence (HTTP daemon):
  1. GPU guard runs synchronously before the event loop starts — exits with code 1
     if no CUDA provider is available (CPU fallback is forbidden).
  2. run_mcp_http_server builds the Starlette app, injects a combined lifespan that
     wraps session_manager.run() with our background tasks (stale-cleanup, watcher
     resumption) and fires sd_notify READY=1 after the session manager has started.
     On shutdown it cancels the cleanup task and fires STOPPING=1.

Note: FastMCP's lifespan= constructor param wires into _mcp_server, which for the
streamable-HTTP transport fires once per MCP session (not once per process), so it
cannot be used for process-level background tasks. The Starlette-level lifespan
injected in run_mcp_http_server is used instead.
"""

from __future__ import annotations

import asyncio
import logging
import os
import sys
from contextlib import asynccontextmanager, suppress
from typing import Any, Literal

try:
    from mcp.server.fastmcp import FastMCP
except ModuleNotFoundError as exc:  # pragma: no cover
    from opencode_search._fastmcp_stub import FastMCPStub as FastMCP  # type: ignore[assignment]

    _MCP_IMPORT_ERROR: ModuleNotFoundError | None = exc
else:
    _MCP_IMPORT_ERROR = None

from starlette.requests import Request
from starlette.responses import JSONResponse

from opencode_search.daemon import (
    DEFAULT_CLIENT_STALE_S,
    DEFAULT_MODEL_IDLE_UNLOAD_S,
    _global_prompt_text,
    _sd_notify,
)
from opencode_search.daemon_runtime import runtime_state
from opencode_search.handlers import (
    handle_ask_feature,
    handle_detect_impact,
    handle_detect_patterns,
    handle_ensure_project_watching,
    handle_get_callees,
    handle_get_callers,
    handle_get_communities,
    handle_get_symbol,
    handle_global_search,
    handle_global_synthesis,
    handle_graph_export,
    handle_list_indexed_projects,
    handle_project_status,
    handle_project_structure,
    handle_release_project_watch,
    handle_search_code,
    handle_stop_watching,
    handle_trace_path,
    handle_wiki_query,
    resolve_indexed_project_path,
)

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Background maintenance
# ---------------------------------------------------------------------------


async def _release_stale_project_watches() -> None:
    for project_path in runtime_state.releaseable_stale_projects(DEFAULT_CLIENT_STALE_S):
        await handle_release_project_watch(project_path)


async def _stale_cleanup_loop() -> None:
    interval_s = max(1.0, min(float(DEFAULT_CLIENT_STALE_S) / 3.0, 5.0))
    _watchdog_usec = int(os.environ.get("WATCHDOG_USEC", "0"))
    _watchdog_every = max(1, int(_watchdog_usec / 2_000_000)) if _watchdog_usec else 0
    _tick = 0
    # One-shot idle cleanup: once models are unloaded after the idle threshold,
    # skip further cleanup_models calls until new inference resets the idle clock.
    # This prevents the tight 5-second loop that caused 13k+ cleanup_models calls
    # (each firing gc.collect×2 + torch.cuda.synchronize — pure CPU/GPU overhead).
    _models_cleaned = False
    while True:
        try:
            await asyncio.sleep(interval_s)
            _tick += 1
            await _release_stale_project_watches()
            if _watchdog_every and _tick % _watchdog_every == 0:
                _sd_notify("WATCHDOG=1\n")
            if DEFAULT_MODEL_IDLE_UNLOAD_S > 0:
                from opencode_search.embeddings import (
                    cleanup_models,
                    seconds_since_last_inference,
                )
                idle_s = seconds_since_last_inference()
                if idle_s <= DEFAULT_MODEL_IDLE_UNLOAD_S:
                    # Recent inference — reset so we'll clean again after next idle period
                    _models_cleaned = False
                elif not _models_cleaned:
                    log.info("models idle >%ds — unloading to free RAM/VRAM", DEFAULT_MODEL_IDLE_UNLOAD_S)
                    await asyncio.to_thread(cleanup_models)
                    _models_cleaned = True
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.warning("stale cleanup failed: %s", exc)


async def resume_watchers() -> None:
    """Start/resume watchers for every indexed (file_count > 0) registry entry.

    Watches every project AND every federation member (members are separate registry
    entries with their own file_count>0 once indexed — Phase 102 federation-first
    design). Previously only entries with watch=True were resumed, leaving all 24
    astro members and payment-gateway un-watched after daemon restart (Gap C).

    Must be called from inside the running event loop so watcher coroutines
    bind to the correct loop.
    """
    from opencode_search.config import load_registry

    registry = load_registry()
    started = 0
    skipped_empty = 0
    for path_str, entry in registry.items():
        if not entry.file_count:
            skipped_empty += 1
            continue
        result = await handle_ensure_project_watching(path_str, persist=True)
        if result.get("watching"):
            log.info("Resumed watcher for %s", path_str)
            started += 1
        elif not result.get("error"):
            log.debug("Watcher already active for %s", path_str)
        else:
            # Fail loud — a silent skip means incremental re-index silently stops working.
            log.warning(
                "resume_watchers: failed to start watcher for %s: %s",
                path_str, result.get("error", "unknown"),
            )
    log.info("resume_watchers: started=%d skipped_empty=%d", started, skipped_empty)


async def resume_stalled_pipelines(startup_delay: float = 10.0) -> None:
    """On daemon startup, resume KB pipeline for projects stranded at 0 communities.

    A project can be stranded when the daemon restarts mid-pipeline (fire-and-forget
    asyncio.create_task dies with the process). This scan detects projects that have
    a vector index (file_count > 0) but no enrichment yet (_project_is_fresh), and
    re-schedules the auto-pipeline for each — idempotent because _project_is_fresh
    returns False once enrichment completes.

    Args:
        startup_delay: Seconds to wait before scanning, giving watchers and symbol-
            enrichment tasks time to start first. Tests pass 0 to skip the wait.
    """
    import asyncio as _aio

    from opencode_search.config import load_registry
    from opencode_search.handlers._autopipeline import (
        _project_is_fresh,
        _project_kb_incomplete,
        auto_pipeline_enabled,
        schedule_auto_pipeline,
    )

    if not auto_pipeline_enabled():
        return

    # Delay so watchers and symbol-enrichment tasks start first, avoiding GPU
    # contention on the hot path (embedding warmup, qwen3 load).
    if startup_delay > 0:
        await _aio.sleep(startup_delay)

    registry = load_registry()
    scheduled = 0
    for path_str, entry in registry.items():
        if not entry.file_count:
            continue
        try:
            # _project_is_fresh: never enriched (0-community fresh project)
            # _project_kb_incomplete: enriched but KB is present-but-incomplete
            #   (edges>0/0-communities, enrichment plateau, or wiki pages missing)
            if _project_is_fresh(path_str) or _project_kb_incomplete(path_str):
                schedule_auto_pipeline(path_str)
                log.info("resume_stalled_pipelines: scheduled pipeline for %s", path_str)
                scheduled += 1
        except Exception as exc:
            log.debug("resume_stalled_pipelines: skipped %s: %s", path_str, exc)

    if scheduled:
        log.info("resume_stalled_pipelines: %d stalled project(s) re-queued", scheduled)


async def resume_symbol_enrichment() -> None:
    """On daemon startup, resume background symbol enrichment for any indexed project
    that has function/method nodes with intent IS NULL.

    Two paths:
    1. SQLite replay: if jobs.db has non-terminal enrich_symbols jobs from the
       last run, re-submit them (idempotent — per-node writes mean no re-work).
    2. Registry scan: for any project in the registry that has unenriched nodes
       but no in-flight job (not in SQLite), submit a new job.

    Uses dedup=True so concurrent re-submissions are collapsed.
    """
    import asyncio as _aio

    from opencode_search.config import load_registry
    from opencode_search.handlers._enrichment import handle_enrich_symbols_background
    from opencode_search.handlers._graph import _open_graph
    from opencode_search.jobs import submit_job
    from opencode_search.jobs_store import load_nonterminal_jobs, mark_interrupted

    # Small delay: let the server finish binding and the session manager start up.
    await _aio.sleep(2)

    # Path 1: replay non-terminal jobs from SQLite
    resumed_paths: set[str] = set()
    for job_row in load_nonterminal_jobs():
        path_str = job_row["project_path"]
        action = job_row["action"]
        if action == "enrich_symbols":
            submit_job(
                handle_enrich_symbols_background(path_str),
                action="enrich_symbols",
                project_path=path_str,
                dedup=True,
            )
            resumed_paths.add(path_str)
            log.info("resume_symbol_enrichment: replayed job[%s] for %s", job_row["id"], path_str)
        else:
            mark_interrupted(job_row["id"])
            log.info("resume_symbol_enrichment: marked job[%s] (%s) as interrupted", job_row["id"], action)

    # Path 2: registry scan for projects with unenriched nodes not already replayed
    registry = load_registry()
    for path_str in list(registry.keys()):
        if path_str in resumed_paths:
            continue
        try:
            gs = _open_graph(path_str)
            if gs is None:
                continue
            try:
                needs_enrich = gs.has_unenriched_symbols()
            finally:
                gs.close()
            if not needs_enrich:
                continue
            submit_job(
                handle_enrich_symbols_background(path_str),
                action="enrich_symbols",
                project_path=path_str,
                dedup=True,
            )
            log.info("resume_symbol_enrichment: submitted job for %s", path_str)
        except Exception as exc:
            log.debug("resume_symbol_enrichment: skipped %s: %s", path_str, exc)


# ---------------------------------------------------------------------------
# FastMCP instance
# ---------------------------------------------------------------------------

_mcp_kwargs: dict[str, Any] = {
    "name": "opencode-search",
    "instructions": (
        "GPU-accelerated local semantic code search — all embedding and reranking runs locally"
        " on your GPU, no data leaves your machine.\n\n"
        + _global_prompt_text()
    ),
    # No lifespan= here: the HTTP process-level lifespan is injected in
    # run_mcp_http_server (Starlette layer). FastMCP's lifespan= wires into
    # _mcp_server which fires per-session for HTTP — unsuitable for background tasks.
}
if _MCP_IMPORT_ERROR is not None:
    _mcp_kwargs["missing_exc"] = _MCP_IMPORT_ERROR

mcp = FastMCP(**_mcp_kwargs)


# ---------------------------------------------------------------------------
# v2 Intent API — 7 outcome-focused tools (May 2026)
# Each tool is one capability domain. Use these. Legacy tools are DEPRECATED.
# ---------------------------------------------------------------------------


@mcp.tool()
async def search(
    query: str,
    scope: Literal["code", "docs", "all", "similar"] = "code",
    project_paths: list[str] | None = None,
    top_k: int = 10,
    include_federation: bool = True,
) -> dict[str, Any]:
    """Find code, documentation, or similar snippets matching a query.

    Use this to find SPECIFIC code: functions, classes, files, patterns.
    Do NOT use for 'how does X work?' — use `ask` for architectural questions.
    Do NOT use for 'index/enrich/wiki' actions — use `build` for those.

    scope: "code" (default) | "docs" (wiki/markdown only) | "all" | "similar"
    """
    runtime_state.note_activity()
    runtime_state.note_query()
    valid = {"code", "docs", "all", "similar"}
    if scope not in valid:
        return {"error": f"Invalid scope {scope!r}", "valid_scopes": sorted(valid)}

    result = await handle_search_code(
        query=query,
        project_paths=project_paths,
        top_k=top_k,
        use_rerank=True,
        include_federation=include_federation,
    )
    if scope == "docs" and "results" in result:
        doc_langs = {"wiki", "knowledge_base", "markdown", "rst", "text"}
        result["results"] = [
            r for r in result["results"]
            if r.get("language", "") in doc_langs
            or r.get("path", "").endswith((".md", ".rst", ".txt"))
        ]
        result["total"] = len(result["results"])
    return result


@mcp.tool()
async def ask(
    query: str,
    project_path: str,
    scope: Literal["architecture", "wiki", "all", "global", "feature", "business"] = "all",
    top_k: int = 10,
    include_federation: bool = True,
) -> dict[str, Any]:
    """Answer architectural and conceptual questions about a project.

    Use this for 'how does X work?', 'which layer handles Y?', 'where is Z?'.
    Do NOT use to find specific functions/files — use `search` for that.
    Do NOT use to index or build knowledge — use `build` for that.

    scope: "all" (default) | "architecture" (communities only) | "wiki" (pages only)
           | "global" (map-reduce over ALL community summaries — GraphRAG-style holistic answer)
           | "feature" (feature trace: entry points + call chain + algorithm + design rationale)
           | "business" (answer from business-classified communities: features, processes, rules)
    """
    runtime_state.note_activity()
    runtime_state.note_query()
    valid = {"architecture", "wiki", "all", "global", "feature", "business"}
    if scope not in valid:
        return {"error": f"Invalid scope {scope!r}", "valid_scopes": sorted(valid)}

    if scope == "global":
        return await handle_global_synthesis(
            query=query, project_path=project_path,
            include_federation=include_federation,
        )
    if scope == "feature":
        return await handle_ask_feature(
            query=query, project_path=project_path, top_k=top_k,
        )
    if scope == "business":
        from opencode_search.handlers._business import handle_ask_business
        return await handle_ask_business(
            query=query, project_path=project_path, top_k=top_k,
            include_federation=include_federation,
        )
    if scope == "wiki":
        return await handle_wiki_query(query=query, project_path=project_path, top_k=top_k)
    return await handle_global_search(
        query=query, project_path=project_path, top_k=top_k,
        include_federation=include_federation,
    )


@mcp.tool()
async def graph(
    symbol: str,
    project_path: str,
    relation: Literal["definition", "callers", "callees", "impact", "path",
                       "impact_narrative", "semantic_trace"] = "definition",
    to_symbol: str | None = None,
    depth: int = 5,
    include_federation: bool = True,
) -> dict[str, Any]:
    """Explore the code graph: definition, callers, callees, impact, or call path.

    Use this for call-graph analysis, tracing business flows, impact assessment.
    Do NOT use for text search — use `search` or `ask` for that.

    relation: "definition" (default) | "callers" | "callees" | "impact" | "path"
              | "impact_narrative" (LLM summary of blast radius — risk level, affected domains)
              | "semantic_trace" (find path from one concept to another using natural language)
    to_symbol: required when relation="path" or "semantic_trace"
               For "semantic_trace": to_symbol is the exit concept (e.g. "database write")
    symbol: For "semantic_trace": the entry concept (e.g. "HTTP request handler")
    depth: BFS depth for callers/callees (default 5)
    """
    runtime_state.note_activity()
    runtime_state.note_query()
    valid = {"definition", "callers", "callees", "impact", "path",
             "impact_narrative", "semantic_trace"}
    if relation not in valid:
        return {"error": f"Invalid relation {relation!r}", "valid_relations": sorted(valid)}
    if relation == "path" and not to_symbol:
        return {"error": "relation='path' requires to_symbol parameter"}
    if relation == "semantic_trace" and not to_symbol:
        return {"error": "relation='semantic_trace' requires to_symbol (exit concept)"}

    if relation == "definition":
        return await handle_get_symbol(name=symbol, project_path=project_path)
    elif relation == "callers":
        return await handle_get_callers(symbol=symbol, project_path=project_path, depth=depth)
    elif relation == "callees":
        return await handle_get_callees(symbol=symbol, project_path=project_path, depth=depth)
    elif relation == "impact":
        return await handle_detect_impact(symbol=symbol, project_path=project_path)
    elif relation == "impact_narrative":
        from opencode_search.handlers._impact import handle_impact_narrative
        return await handle_impact_narrative(
            symbol=symbol, project_path=project_path,
            depth=depth, include_federation=include_federation,
        )
    elif relation == "semantic_trace":
        from opencode_search.handlers._trace import handle_semantic_trace
        return await handle_semantic_trace(
            from_query=symbol, to_query=to_symbol,
            project_path=project_path, include_federation=include_federation,
        )
    else:
        return await handle_trace_path(
            from_symbol=symbol, to_symbol=to_symbol, project_path=project_path,
        )


@mcp.tool()
async def overview(
    project_path: str | None = None,
    what: Literal[
        "structure", "communities", "status", "projects", "metrics", "graph_export",
        "patterns", "architecture_domains", "hierarchy", "service_mesh",
        "import_cycles", "suggested_questions", "graph_diff", "surprising_connections",
        "pr_impact", "feature_map", "business_rules", "process_flows",
    ] = "structure",
    max_depth: int = 4,
    top_k: int = 100,
    export_format: Literal["json", "graphml"] = "json",
    max_nodes: int = 5000,
    since_hours: int | None = None,
) -> dict[str, Any]:
    """Get a structural or status overview of a project or the search engine.

    Use this to orient yourself before searching or building the knowledge base.
    Do NOT use to search code — use `search` or `ask` for that.

    what: "structure" (default) | "communities" | "status" | "projects" | "metrics"
          | "graph_export" (download knowledge graph for Gephi/Cytoscape)
          | "patterns" (languages, dependencies, conventions, frameworks, architecture)
          | "architecture_domains" (top-level Leiden hierarchy = highest-level communities)
          | "hierarchy" (full community hierarchy tree, all levels)
          | "import_cycles" (circular import dependencies using Tarjan's SCC)
          | "suggested_questions" (questions the graph is uniquely positioned to answer)
          | "graph_diff" (what changed in the graph recently)
          | "surprising_connections" (edges spanning architectural community boundaries)
          | "pr_impact" (changed files → communities touched + risk level; auto-detects git diff)
          | "feature_map" (all communities grouped by business semantic type: feature|process|rule|...)
          | "business_rules" (communities classified as constraints/policies/validations)
          | "process_flows" (communities classified as workflows/business processes)
    project_path: not required for what="projects" or "metrics"
    export_format: "json" | "graphml" (only for what="graph_export")
    max_nodes: cap for graph_export (default 5000)
    """
    runtime_state.note_activity()
    valid = {"structure", "communities", "status", "projects", "metrics", "graph_export",
             "patterns", "architecture_domains", "hierarchy", "service_mesh",
             "import_cycles", "suggested_questions", "graph_diff", "surprising_connections",
             "pr_impact", "feature_map", "business_rules", "process_flows"}
    if what not in valid:
        return {"error": f"Invalid what={what!r}", "valid_values": sorted(valid)}

    if what == "structure":
        if not project_path:
            return {"error": "project_path required for what='structure'"}
        return await handle_project_structure(project_path=project_path, max_depth=max_depth)
    elif what == "patterns":
        if not project_path:
            return {"error": "project_path required for what='patterns'"}
        return await handle_detect_patterns(project_path=project_path)
    elif what == "communities":
        if not project_path:
            return {"error": "project_path required for what='communities'"}
        return await handle_get_communities(project_path=project_path, top_k=top_k)
    elif what == "status":
        if not project_path:
            return {"error": "project_path required for what='status'"}
        return await handle_project_status(path=project_path)
    elif what == "projects":
        return await handle_list_indexed_projects()
    elif what == "graph_export":
        if not project_path:
            return {"error": "project_path required for what='graph_export'"}
        return await handle_graph_export(
            project_path=project_path, format=export_format, max_nodes=max_nodes,
        )
    elif what == "service_mesh":
        if not project_path:
            return {"error": "project_path required for what='service_mesh'"}
        from opencode_search.handlers._service_mesh import handle_detect_service_mesh
        return await handle_detect_service_mesh(project_path=project_path)
    elif what == "architecture_domains":
        if not project_path:
            return {"error": "project_path required for what='architecture_domains'"}
        import contextlib

        from opencode_search.handlers._graph import _open_graph
        def _get_top_level(path: str) -> dict:
            gs = _open_graph(path)
            if gs is None:
                return {"error": "Project not indexed"}
            try:
                max_level = gs.get_max_community_level()
                top = gs.get_communities(level=max_level, order_by_size=True)
                return {
                    "hierarchy_levels": max_level,
                    "architecture_domains": [
                        {"id": c.id, "title": c.title, "summary": c.summary,
                         "node_count": c.node_count, "level": c.level}
                        for c in top
                    ],
                    "domain_count": len(top),
                }
            finally:
                with contextlib.suppress(Exception):
                    gs.close()
        import asyncio
        return await asyncio.to_thread(_get_top_level, project_path)
    elif what == "hierarchy":
        if not project_path:
            return {"error": "project_path required for what='hierarchy'"}
        import contextlib

        from opencode_search.handlers._graph import _open_graph
        def _get_hierarchy(path: str) -> dict:
            gs = _open_graph(path)
            if gs is None:
                return {"error": "Project not indexed"}
            try:
                hierarchy = gs.get_community_hierarchy()
                return {
                    "levels": {
                        str(lvl): [
                            {"id": c.id, "title": c.title, "summary": c.summary,
                             "node_count": c.node_count, "parent_community_id": c.parent_community_id}
                            for c in comms
                        ]
                        for lvl, comms in hierarchy.items()
                    },
                    "max_level": gs.get_max_community_level(),
                }
            finally:
                with contextlib.suppress(Exception):
                    gs.close()
        import asyncio
        return await asyncio.to_thread(_get_hierarchy, project_path)
    elif what == "surprising_connections":
        if not project_path:
            return {"error": "project_path required for what='surprising_connections'"}
        import contextlib

        from opencode_search.handlers._graph import _open_graph
        def _get_bridges(path: str) -> dict:
            gs = _open_graph(path)
            if gs is None:
                return {"error": "Project not indexed"}
            try:
                bridges = gs.get_cross_community_bridges(top_n=20)
                return {
                    "project_path": path,
                    "surprising_connections": bridges,
                    "count": len(bridges),
                }
            finally:
                with contextlib.suppress(Exception):
                    gs.close()
        import asyncio
        return await asyncio.to_thread(_get_bridges, project_path)
    elif what == "import_cycles":
        if not project_path:
            return {"error": "project_path required for what='import_cycles'"}
        from opencode_search.handlers._graph import handle_import_cycles
        return await handle_import_cycles(project_path=project_path)
    elif what == "suggested_questions":
        if not project_path:
            return {"error": "project_path required for what='suggested_questions'"}
        from opencode_search.handlers._graph import handle_suggest_questions
        return await handle_suggest_questions(project_path=project_path)
    elif what == "graph_diff":
        if not project_path:
            return {"error": "project_path required for what='graph_diff'"}
        from opencode_search.handlers._graph import handle_graph_diff
        return await handle_graph_diff(project_path=project_path, since_hours=since_hours)
    elif what == "pr_impact":
        if not project_path:
            return {"error": "project_path required for what='pr_impact'"}
        from opencode_search.handlers._pr_impact import handle_pr_impact
        return await handle_pr_impact(project_path=project_path)
    elif what == "feature_map":
        if not project_path:
            return {"error": "project_path required for what='feature_map'"}
        from opencode_search.handlers._business import handle_feature_map
        return await handle_feature_map(project_path=project_path)
    elif what == "business_rules":
        if not project_path:
            return {"error": "project_path required for what='business_rules'"}
        from opencode_search.handlers._business import handle_business_rules
        return await handle_business_rules(project_path=project_path)
    elif what == "process_flows":
        if not project_path:
            return {"error": "project_path required for what='process_flows'"}
        from opencode_search.handlers._business import handle_process_flows
        return await handle_process_flows(project_path=project_path)
    else:
        from opencode_search.metrics import get_metrics, get_stream_metrics
        return {"search": get_metrics(), "chat_stream": get_stream_metrics()}


@mcp.tool()
async def index(
    project_path: str,
    enabled: bool = True,
) -> dict[str, Any]:
    """Flag a project for the search engine. The ONLY write operation the agent can perform.

    enabled=True  → Register/flag the project and return immediately. The daemon then
                    indexes it, builds the knowledge base, starts watching, and indexes
                    federation members — all automatically in the background.
    enabled=False → DESTRUCTIVE: stop watching, remove from the registry, and delete the
                    on-disk index + knowledge base. All search-engine data for this
                    project is permanently gone. Use with care.
    """
    runtime_state.note_activity()
    from pathlib import Path as _Path

    resolved = str(_Path(project_path).expanduser().resolve())

    if enabled:
        from opencode_search.config import (
            ProjectEntry,
            get_project_db_path,
            load_registry,
            save_registry,
        )
        registry = load_registry()
        if resolved in registry:
            return {"status": "already_registered", "path": resolved,
                    "note": "Already flagged. Daemon will index if not yet indexed."}
        entry = ProjectEntry(
            path=resolved,
            db_path=str(get_project_db_path(resolved)),
        )
        registry[resolved] = entry
        save_registry(registry)
        return {
            "status": "flagged",
            "path": resolved,
            "note": (
                "Project registered. The daemon will index it, build the knowledge base, "
                "start watching for changes, and index federation members automatically."
            ),
        }
    else:
        # DESTRUCTIVE: stop watching → remove from registry → delete on-disk index
        await handle_stop_watching(path=resolved)
        from opencode_search.handlers._vacuum import handle_remove_project
        result = await handle_remove_project(project_path=resolved, delete_index=True)
        return result


# ---------------------------------------------------------------------------
# Dashboard + API routes (browser-viewable at http://127.0.0.1:8765/dashboard)
# ---------------------------------------------------------------------------

from opencode_search.dashboard import register_dashboard_routes  # noqa: E402

register_dashboard_routes(mcp)


# ---------------------------------------------------------------------------
# HTTP admin and health routes
# ---------------------------------------------------------------------------


_healthz_start = __import__("time").monotonic()


@mcp.custom_route("/healthz", methods=["GET"], include_in_schema=False)
async def healthz(_request: Request) -> JSONResponse:
    import os as _os
    import time as _time
    try:
        load1, load5, load15 = _os.getloadavg()
    except OSError:
        load1 = load5 = load15 = 0.0
    try:
        import multiprocessing as _mp
        cpu_count = _mp.cpu_count()
    except Exception:
        cpu_count = 1
    return JSONResponse(
        {
            "ok": True,
            "service": "opencode-search",
            "transport": "streamable-http",
            "uptime_s": round(_time.monotonic() - _healthz_start, 1),
            "load_avg": {"1m": round(load1, 2), "5m": round(load5, 2), "15m": round(load15, 2)},
            "cpu_count": cpu_count,
            **runtime_state.snapshot(),
        }
    )


@mcp.custom_route("/admin/client/open", methods=["POST"], include_in_schema=False)
async def client_open(request: Request) -> JSONResponse:
    payload = await request.json()
    client_id = str(payload.get("client_id", ""))
    cwd = str(payload.get("cwd", ""))
    project_path = resolve_indexed_project_path(cwd) if cwd else None
    runtime_state.client_open(client_id, cwd=cwd or None, project_path=project_path)
    if project_path:
        await handle_ensure_project_watching(project_path, persist=False)
    return JSONResponse({"ok": True, **runtime_state.snapshot()})


@mcp.custom_route("/admin/client/heartbeat", methods=["POST"], include_in_schema=False)
async def client_heartbeat(request: Request) -> JSONResponse:
    payload = await request.json()
    runtime_state.client_heartbeat(str(payload.get("client_id", "")))
    return JSONResponse({"ok": True, **runtime_state.snapshot()})


@mcp.custom_route("/admin/client/close", methods=["POST"], include_in_schema=False)
async def client_close(request: Request) -> JSONResponse:
    payload = await request.json()
    runtime_state.client_close(str(payload.get("client_id", "")))
    return JSONResponse({"ok": True, **runtime_state.snapshot()})


@mcp.custom_route("/admin/status", methods=["GET"], include_in_schema=False)
async def admin_status(_request: Request) -> JSONResponse:
    return JSONResponse({"ok": True, **runtime_state.snapshot()})


# ---------------------------------------------------------------------------
# Server entrypoints
# ---------------------------------------------------------------------------


def run_mcp_server() -> None:
    """Start the MCP server with stdio transport (for AI assistant subprocesses)."""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        stream=sys.stderr,
    )
    try:
        from opencode_search.embeddings import assert_gpu_available
        assert_gpu_available()
    except Exception as exc:
        log.critical("GPU guard failed: %s", exc)
        sys.exit(1)

    log.info("Starting opencode-search MCP server on stdio…")
    mcp.run(transport="stdio")


def run_mcp_http_server(host: str = "127.0.0.1", port: int = 8765) -> None:
    """Start the MCP server over streamable HTTP for shared daemon usage."""
    import anyio
    import uvicorn

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        stream=sys.stderr,
    )
    try:
        from opencode_search.embeddings import assert_gpu_available
        assert_gpu_available()
    except Exception as exc:
        log.critical("GPU guard failed: %s", exc)
        _sd_notify("STATUS=GPU guard failed — CUDA not available, refusing to start\n")
        sys.exit(1)

    starlette_app = mcp.streamable_http_app()

    @asynccontextmanager
    async def _lifespan(app: Any) -> Any:
        # Warmup: load and pin the query embedder + reranker FIRST so they claim
        # VRAM before indexing/enrichment starts.  Runs in the build executor so
        # it doesn't block the event loop; awaited before background tasks arm.
        async def _warmup() -> None:
            try:
                from opencode_search.embeddings import _BUILD_INFER_EXECUTOR, warmup_query_models
                loop = asyncio.get_running_loop()
                await loop.run_in_executor(_BUILD_INFER_EXECUTOR, warmup_query_models)
            except Exception as exc:
                log.warning("warmup_query_models failed (non-fatal): %s", exc)

        await _warmup()

        cleanup_task = asyncio.create_task(_stale_cleanup_loop(), name="opencode-stale-cleanup")
        resume_task = asyncio.create_task(resume_watchers(), name="opencode-resume-watchers")
        resume_task.add_done_callback(lambda t: t.exception() if not t.cancelled() else None)
        enrich_task = asyncio.create_task(resume_symbol_enrichment(), name="opencode-resume-enrich")
        enrich_task.add_done_callback(lambda t: t.exception() if not t.cancelled() else None)
        pipeline_task = asyncio.create_task(resume_stalled_pipelines(), name="opencode-resume-pipelines")
        pipeline_task.add_done_callback(lambda t: t.exception() if not t.cancelled() else None)
        async with mcp.session_manager.run():
            # Publish the running event loop so background sweep threads can find it
            # (Python 3.12 asyncio.get_event_loop() raises RuntimeError in non-main threads).
            from opencode_search import daemon as _daemon_mod
            _daemon_mod._DAEMON_LOOP = asyncio.get_running_loop()
            _daemon_mod._DAEMON_LOOP_READY.set()
            _sd_notify("READY=1\n")
            _sd_notify(f"STATUS=listening on http://{host}:{port}/mcp\n")
            yield
        cleanup_task.cancel()
        enrich_task.cancel()
        pipeline_task.cancel()
        with suppress(asyncio.CancelledError):
            await cleanup_task
        with suppress(asyncio.CancelledError):
            await enrich_task
        with suppress(asyncio.CancelledError):
            await pipeline_task
        _sd_notify("STOPPING=1\n")

    starlette_app.router.lifespan_context = _lifespan

    log.info("Starting opencode-search MCP server on http://%s:%s/mcp", host, port)

    async def _serve() -> None:
        config = uvicorn.Config(starlette_app, host=host, port=port, log_level="info")
        await uvicorn.Server(config).serve()

    anyio.run(_serve)
