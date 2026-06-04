"""MCP server for opencode-search — stdio and streamable-HTTP transports via FastMCP.

Exposes 7 intent-focused tools (v2 API, May 2026):
  search     — find code/docs by query (scope: code|docs|all|similar)
  ask        — answer architectural questions (scope: architecture|wiki|all)
  graph      — code graph: definition|callers|callees|impact|path
  overview   — project structure|communities|status|projects|metrics
  build      — index|pipeline|enrich|wiki|ingest|reindex_wiki|describe_symbol
  federation — discover|list|add|remove|index federation members
  manage     — stop_watching|wiki_lint

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
    handle_add_federation_member,
    handle_analyze_patterns_llm,
    handle_detect_impact,
    handle_detect_patterns,
    handle_discover_federation,
    handle_ask_feature,
    handle_enrich_hierarchy,
    handle_enrich_project,
    handle_ensure_project_watching,
    handle_get_callees,
    handle_get_callers,
    handle_get_communities,
    handle_get_symbol,
    handle_get_symbol_intent,
    handle_global_search,
    handle_global_synthesis,
    handle_graph_export,
    handle_index_federation,
    handle_index_project,
    handle_list_federation,
    handle_list_indexed_projects,
    handle_pipeline,
    handle_project_status,
    handle_project_structure,
    handle_release_project_watch,
    handle_remove_federation_member,
    handle_search_code,
    handle_stop_watching,
    handle_trace_path,
    handle_wiki_generate,
    handle_wiki_ingest,
    handle_wiki_lint,
    handle_wiki_query,
    handle_wiki_reindex,
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
                    released = await asyncio.to_thread(cleanup_models)
                    if released:
                        _models_cleaned = True
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.warning("stale cleanup failed: %s", exc)


async def resume_watchers() -> None:
    """Restart any watchers that were persisted with watch=True in the registry.

    Must be called from inside the running event loop so watcher coroutines
    bind to the correct loop.
    """
    from opencode_search.config import load_registry

    registry = load_registry()
    for path_str, entry in registry.items():
        if not entry.watch:
            continue
        result = await handle_ensure_project_watching(path_str, persist=True)
        if result.get("watching"):
            log.info("Resumed watcher for %s", path_str)


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
    project_path: not required for what="projects" or what="metrics"
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
        since = ""
        from opencode_search.handlers._graph import handle_graph_diff
        return await handle_graph_diff(project_path=project_path, since=since)
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
        from opencode_search.metrics import get_metrics
        return get_metrics()


@mcp.tool()
async def build(
    project_path: str,
    action: Literal[
        "index", "pipeline", "enrich", "wiki", "ingest",
        "reindex_wiki", "describe_symbol", "analyze_patterns", "hierarchy",
    ] = "pipeline",
    source_path: str | None = None,
    symbol: str | None = None,
    max_communities: int = 200,
    include_federation: bool = True,
    force: bool = False,
    watch: bool = True,
) -> dict[str, Any]:
    """Index a project or build/update its knowledge base.

    Use this to index a new project or build/refresh the knowledge base.
    Start with action='pipeline' for a complete one-call setup.
    Do NOT use for searching — use `search` or `ask` for that.

    action: "pipeline" (default, recommended) | "index" | "enrich" | "wiki"
            | "ingest" | "reindex_wiki" | "describe_symbol"
            | "analyze_patterns" (LLM-powered deep code pattern analysis; requires LLM provider)
            | "hierarchy" (build recursive Leiden hierarchy — run after pipeline for GraphRAG-like levels)
            | "enrich_hierarchy" (LLM-enrich level-2+ macro-communities — run after hierarchy)
    source_path: required for action="ingest"
    symbol: required for action="describe_symbol"
    max_communities: cap on communities to enrich/wiki (default 200)
    force: for analyze_patterns, re-run even if cached result exists
    """
    runtime_state.note_activity()
    valid = {
        "index", "pipeline", "enrich", "wiki", "ingest",
        "reindex_wiki", "describe_symbol", "analyze_patterns", "hierarchy",
        "enrich_hierarchy",
    }
    if action not in valid:
        return {"error": f"Invalid action {action!r}", "valid_actions": sorted(valid)}

    from opencode_search.jobs import submit_job

    if action == "index":
        async def _post_index(result: dict) -> None:
            pp = str(result.get("path", ""))
            if result.get("status") == "ok" and pp and runtime_state.bind_clients_to_project(pp) > 0:
                await handle_ensure_project_watching(pp, persist=False)
                # schedule_auto_pipeline is now called from within _run_index_project
                # so the pipeline fires regardless of who called handle_index_project.
        return await handle_index_project(
            path=project_path, watch=watch, force=force,
            follow_symlinks=True, on_complete=_post_index,
        )
    elif action == "pipeline":
        job = submit_job(
            handle_pipeline(
                project_path=project_path,
                enrich_max_communities=max_communities,
                wiki_max_communities=max_communities,
                ingest_docs=True,
                watch=watch,
            ),
            action="pipeline",
            project_path=project_path,
        )
        return {
            "status": "started",
            "job_id": job.id,
            "poll_url": f"/api/jobs/{job.id}",
            "message": "Pipeline running in background. Poll /api/jobs/{job_id} for progress.",
        }
    elif action == "enrich":
        job = submit_job(
            handle_enrich_project(
                project_path=project_path,
                scope="communities",
                max_communities=max_communities,
                include_federation=include_federation,
            ),
            action="enrich",
            project_path=project_path,
        )
        return {"status": "started", "job_id": job.id, "poll_url": f"/api/jobs/{job.id}"}
    elif action == "wiki":
        job = submit_job(
            handle_wiki_generate(
                project_path=project_path,
                max_communities=max_communities,
                include_federation=include_federation,
            ),
            action="wiki",
            project_path=project_path,
        )
        return {"status": "started", "job_id": job.id, "poll_url": f"/api/jobs/{job.id}"}
    elif action == "ingest":
        if not source_path:
            return {"error": "action='ingest' requires source_path parameter"}
        return await handle_wiki_ingest(source_path=source_path, project_path=project_path)
    elif action == "reindex_wiki":
        return await handle_wiki_reindex(project_path=project_path)
    elif action == "analyze_patterns":
        job = submit_job(
            handle_analyze_patterns_llm(project_path=project_path, force=force),
            action="analyze_patterns",
            project_path=project_path,
        )
        return {"status": "started", "job_id": job.id, "poll_url": f"/api/jobs/{job.id}"}
    elif action == "hierarchy":
        import contextlib

        from opencode_search.graph.community import CommunityDetector
        from opencode_search.handlers._graph import _open_graph

        async def _build_hierarchy_async(path: str) -> dict:
            import asyncio as _aio
            def _run() -> dict:
                gs = _open_graph(path)
                if gs is None:
                    return {"error": "Project not indexed or graph DB not found"}
                try:
                    levels = CommunityDetector().build_hierarchy(gs)
                    return {
                        "status": "ok",
                        "levels_built": levels,
                        "max_level": gs.get_max_community_level(),
                        "project_path": path,
                    }
                finally:
                    with contextlib.suppress(Exception):
                        gs.close()
            return await _aio.to_thread(_run)

        job = submit_job(
            _build_hierarchy_async(project_path),
            action="hierarchy",
            project_path=project_path,
        )
        return {"status": "started", "job_id": job.id, "poll_url": f"/api/jobs/{job.id}"}
    elif action == "enrich_hierarchy":
        job = submit_job(
            handle_enrich_hierarchy(project_path=project_path),
            action="enrich_hierarchy",
            project_path=project_path,
        )
        return {
            "status": "started",
            "job_id": job.id,
            "poll_url": f"/api/jobs/{job.id}",
            "message": "Hierarchy enrichment running in background. Poll /api/jobs/{job_id} for progress.",
        }
    else:
        if not symbol:
            return {"error": "action='describe_symbol' requires symbol parameter"}
        return await handle_get_symbol_intent(name=symbol, project_path=project_path)


@mcp.tool()
async def federation(
    root_path: str,
    action: Literal["discover", "list", "add", "remove", "index"] = "list",
    member_path: str | None = None,
    watch: bool = False,
) -> dict[str, Any]:
    """Manage multi-repo federation: discover, list, add, remove, or index members.

    Use this to manage which sub-repos are included in federation-aware operations.
    Members are auto-discovered from symlinks, go.work, pnpm-workspace.yaml, package.json.
    Do NOT use this to search or build KB — use `search`, `ask`, or `build` for that.

    action: "list" (default) | "discover" | "add" | "remove" | "index"
    member_path: required for action="add" or "remove"
    """
    runtime_state.note_activity()
    valid = {"discover", "list", "add", "remove", "index"}
    if action not in valid:
        return {"error": f"Invalid action {action!r}", "valid_actions": sorted(valid)}

    if action == "discover":
        return await handle_discover_federation(project_path=root_path)
    elif action == "list":
        return await handle_list_federation(project_path=root_path)
    elif action == "add":
        if not member_path:
            return {"error": "action='add' requires member_path parameter"}
        return await handle_add_federation_member(root_path=root_path, member_path=member_path)
    elif action == "remove":
        if not member_path:
            return {"error": "action='remove' requires member_path parameter"}
        return await handle_remove_federation_member(root_path=root_path, member_path=member_path)
    else:
        return await handle_index_federation(root_path=root_path, watch=watch)


@mcp.tool()
async def manage(
    project_path: str,
    action: Literal[
        "stop_watching", "wiki_lint", "install_hooks", "uninstall_hooks",
        "dedup", "vacuum", "jobs", "remove_project",
    ] = "wiki_lint",
    dry_run: bool = False,
    job_id: str | None = None,
    delete_index: bool = False,
) -> dict[str, Any]:
    """Project lifecycle: stop watchers, health-check wiki, manage git hooks, dedup graph, or check jobs.

    action: "wiki_lint" (default) | "stop_watching"
            | "install_hooks" — install git post-commit hook for auto-reindex
            | "uninstall_hooks" — remove git post-commit hook
            | "dedup" — deduplicate graph nodes (MinHash/LSH + Jaro-Winkler when available)
            | "vacuum" — remove orphan index_budget/index_balanced tier dirs; free disk space
            | "jobs" — list background build jobs (or check one with job_id=)
            | "remove_project" — remove project from registry (delete_index=True also deletes index)
    dry_run: for "dedup" — preview merges; for "vacuum" — report without deleting
    job_id: for action="jobs" — return status of a specific job instead of all jobs
    delete_index: for action="remove_project" — also delete the on-disk index (frees space)
    """
    runtime_state.note_activity()
    valid = {"stop_watching", "wiki_lint", "install_hooks", "uninstall_hooks", "dedup", "vacuum", "jobs", "remove_project"}
    if action not in valid:
        return {"error": f"Invalid action {action!r}", "valid_actions": sorted(valid)}

    if action == "stop_watching":
        return await handle_stop_watching(path=project_path)
    if action in ("install_hooks", "uninstall_hooks"):
        from opencode_search.handlers._hooks import handle_git_hooks
        return await handle_git_hooks(
            project_path=project_path,
            install=(action == "install_hooks"),
        )
    if action == "dedup":
        from opencode_search.handlers._graph import handle_dedup_nodes
        return await handle_dedup_nodes(project_path=project_path, dry_run=dry_run)
    if action == "vacuum":
        from opencode_search.handlers._vacuum import handle_vacuum
        return await handle_vacuum(project_path=project_path, dry_run=dry_run)
    if action == "jobs":
        from opencode_search.jobs import get_job, job_to_dict, list_jobs
        if job_id:
            job = get_job(job_id)
            if job is None:
                return {"error": f"Job {job_id!r} not found"}
            return job_to_dict(job)
        jobs = list_jobs(project_path=project_path if project_path else None)
        return {"jobs": [job_to_dict(j) for j in jobs], "total": len(jobs)}
    if action == "remove_project":
        from opencode_search.handlers._vacuum import handle_remove_project
        return await handle_remove_project(project_path=project_path, delete_index=delete_index)
    return await handle_wiki_lint(project_path=project_path)


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
        cleanup_task = asyncio.create_task(_stale_cleanup_loop(), name="opencode-stale-cleanup")
        resume_task = asyncio.create_task(resume_watchers(), name="opencode-resume-watchers")
        resume_task.add_done_callback(lambda t: t.exception() if not t.cancelled() else None)
        async with mcp.session_manager.run():
            _sd_notify("READY=1\n")
            _sd_notify(f"STATUS=listening on http://{host}:{port}/mcp\n")
            yield
        cleanup_task.cancel()
        with suppress(asyncio.CancelledError):
            await cleanup_task
        _sd_notify("STOPPING=1\n")

    starlette_app.router.lifespan_context = _lifespan

    log.info("Starting opencode-search MCP server on http://%s:%s/mcp", host, port)

    async def _serve() -> None:
        config = uvicorn.Config(starlette_app, host=host, port=port, log_level="info")
        await uvicorn.Server(config).serve()

    anyio.run(_serve)
