"""Mini dashboard — read-only browser view of everything the engine produces.

Registers routes on the existing FastMCP Starlette app (no new server/port).
Import this module in mcp.py to attach routes:  from opencode_search import dashboard

Routes are split into per-domain sub-registrars for maintainability:
  _register_root_routes     — /, /dashboard, /static/
  _register_project_routes  — /api/projects, /api/overview, /api/communities, watching
  _register_wiki_routes     — /api/wiki, /api/wiki/page, /api/wiki_lint, /api/suggested_questions
  _register_search_routes   — /api/ask, /api/feature, /api/search, /api/patterns, business
  _register_graph_routes    — /api/graph, /api/graph_export, service_mesh, trace, impact, PR
  _register_chat_routes     — /api/chat, /api/chat_stream, /api/debug
  _register_kb_routes       — /api/kb_health, /api/dedup, /api/vacuum, /api/git_hooks, /api/reload
  _register_ops_routes      — metrics, pipeline, SSE, alerts, jobs

register_dashboard_routes(mcp) is the public entry point — it calls all sub-registrars.
"""
from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import sqlite3
import threading
import time
from pathlib import Path
from typing import TYPE_CHECKING

from starlette.requests import Request
from starlette.responses import (
    FileResponse,
    HTMLResponse,
    JSONResponse,
    RedirectResponse,
    StreamingResponse,
)

if TYPE_CHECKING:
    from mcp.server.fastmcp import FastMCP

# ---------------------------------------------------------------------------
# Metrics persistence (SQLite — stored alongside project registry)
# ---------------------------------------------------------------------------

_DATA_DIR = Path.home() / ".local" / "share" / "opencode-search"
_METRICS_DB = _DATA_DIR / "metrics.db"
_ALERTS_FILE = _DATA_DIR / "alerts.json"
_STATIC_DIR = Path(__file__).parent / "static"

_DEFAULT_ALERTS = [
    {"id": "lat_p95", "name": "Latency p95", "metric": "latency_p95_ms", "op": ">", "threshold": 500, "enabled": True},
    {"id": "zero_results", "name": "Zero-result rate", "metric": "zero_result_pct", "op": ">", "threshold": 20, "enabled": True},
    {"id": "enrichment", "name": "KB enrichment", "metric": "enrichment_pct", "op": "<", "threshold": 80, "enabled": True},
    {"id": "load_avg_1m", "name": "System load 1m", "metric": "load_avg_1m", "op": ">", "threshold": 18, "enabled": True},
]

_db_lock = threading.Lock()


def _get_metrics_db() -> sqlite3.Connection:
    _DATA_DIR.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(_METRICS_DB))
    conn.row_factory = sqlite3.Row
    conn.execute("""
        CREATE TABLE IF NOT EXISTS search_events (
            ts REAL NOT NULL,
            query TEXT,
            scope TEXT,
            result_count INTEGER,
            top_score REAL,
            latency_ms REAL,
            project TEXT
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS indexing_events (
            ts REAL NOT NULL,
            project TEXT,
            action TEXT,
            duration_s REAL,
            files_processed INTEGER
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_search_ts ON search_events(ts)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_index_ts ON indexing_events(ts)")
    conn.commit()
    return conn


def record_search_event(query: str, scope: str, result_count: int,
                         top_score: float, latency_ms: float, project: str) -> None:
    """Record a search event to the metrics DB (call from search handlers)."""
    try:
        with _db_lock:
            conn = _get_metrics_db()
            conn.execute(
                "INSERT INTO search_events VALUES (?,?,?,?,?,?,?)",
                (time.time(), query, scope, result_count, top_score, latency_ms, project),
            )
            # Prune events older than 7 days
            cutoff = time.time() - 7 * 86400
            conn.execute("DELETE FROM search_events WHERE ts < ?", (cutoff,))
            conn.commit()
            conn.close()
    except Exception:
        pass


def record_indexing_event(project: str, action: str, duration_s: float, files_processed: int) -> None:
    """Record an indexing event to the metrics DB."""
    try:
        with _db_lock:
            conn = _get_metrics_db()
            conn.execute(
                "INSERT INTO indexing_events VALUES (?,?,?,?,?)",
                (time.time(), project, action, duration_s, files_processed),
            )
            cutoff = time.time() - 7 * 86400
            conn.execute("DELETE FROM indexing_events WHERE ts < ?", (cutoff,))
            conn.commit()
            conn.close()
    except Exception:
        pass


def _load_alerts() -> list[dict]:
    try:
        if _ALERTS_FILE.exists():
            saved = json.loads(_ALERTS_FILE.read_text())
            # Merge in any new default rules not yet in the saved file
            saved_ids = {r["id"] for r in saved}
            merged = list(saved)
            for rule in _DEFAULT_ALERTS:
                if rule["id"] not in saved_ids:
                    merged.append(rule)
            return merged
    except Exception:
        pass
    return _DEFAULT_ALERTS[:]


def _save_alerts(rules: list[dict]) -> None:
    _DATA_DIR.mkdir(parents=True, exist_ok=True)
    _ALERTS_FILE.write_text(json.dumps(rules, indent=2))

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# HTML template — lives in _dashboard_html.py for maintainability
# ---------------------------------------------------------------------------

from opencode_search._dashboard_html import _DASHBOARD_HTML  # noqa: E402

# Keep a small shim so old code that directly accesses this module still works.
# _DASHBOARD_HTML is imported above.

# ---------------------------------------------------------------------------
# Module-level helpers (hoisted from the former mega-function for clarity)
# ---------------------------------------------------------------------------



def _spawn_daemon_restart_thread(pid: int) -> None:
    """Start a background thread that kills pid and respawns the daemon."""
    import os
    import signal
    import subprocess
    import sys
    import threading
    import time as _t

    def _restart() -> None:
        _t.sleep(1.0)
        subprocess.Popen(
            [
                sys.executable, "-c",
                (
                    "import time, subprocess, sys\n"
                    "time.sleep(2)\n"
                    "subprocess.Popen([sys.executable, '-m', 'opencode_search', 'daemon', 'ensure'],"
                    " start_new_session=True)\n"
                ),
            ],
            close_fds=True,
            start_new_session=True,
        )
        # Notify open SSE streams before killing: clients receive a reload frame
        # instead of having their TCP connection severed (fixes the 2h-hang case).
        from opencode_search.daemon import _broadcast_reload_notice
        _broadcast_reload_notice()
        _t.sleep(0.2)
        os.kill(pid, signal.SIGTERM)

    threading.Thread(target=_restart, daemon=False).start()


def _hierarchy_build_sync(path: str) -> dict:
    """Run Leiden hierarchy build synchronously (called via asyncio.to_thread)."""
    import contextlib as _ctx

    from opencode_search.graph.community import CommunityDetector
    from opencode_search.handlers._graph import _open_graph
    gs = _open_graph(path)
    if gs is None:
        return {"error": "Project not indexed"}
    try:
        levels = CommunityDetector().build_hierarchy(gs)
        return {"status": "ok", "levels_built": levels, "max_level": gs.get_max_community_level()}
    finally:
        with _ctx.suppress(Exception):
            gs.close()


def _run_surprising_sync(path: str, top_n: int) -> dict:
    """Detect cross-community bridges synchronously (called via asyncio.to_thread)."""
    import contextlib as _ctx

    from opencode_search.handlers._graph import _open_graph
    gs = _open_graph(path)
    if gs is None:
        return {"error": "Project not indexed"}
    try:
        bridges = gs.get_cross_community_bridges(top_n=top_n)
        return {
            "project_path": path,
            "surprising_connections": bridges,
            "count": len(bridges),
        }
    finally:
        with _ctx.suppress(Exception):
            gs.close()


# ---------------------------------------------------------------------------
# Sub-registrar: root + static
# ---------------------------------------------------------------------------

def _register_root_routes(mcp: FastMCP) -> None:
    @mcp.custom_route("/", methods=["GET"], include_in_schema=False)
    async def root_redirect(_request: Request) -> RedirectResponse:
        return RedirectResponse(url="/dashboard", status_code=307)

    @mcp.custom_route("/dashboard", methods=["GET"], include_in_schema=False)
    async def dashboard(_request: Request) -> HTMLResponse:
        return HTMLResponse(_DASHBOARD_HTML)

    @mcp.custom_route("/static/{path:path}", methods=["GET"], include_in_schema=False)
    async def static_files(request: Request) -> FileResponse:
        """Serve static assets (chart.min.js, etc.)."""
        filename = request.path_params.get("path", "")
        file_path = _STATIC_DIR / filename
        if file_path.exists() and file_path.is_file():
            return FileResponse(str(file_path))
        return JSONResponse({"error": "not found"}, status_code=404)


# ---------------------------------------------------------------------------
# Sub-registrar: project management
# ---------------------------------------------------------------------------

def _register_project_routes(mcp: FastMCP) -> None:
    @mcp.custom_route("/api/projects", methods=["GET"], include_in_schema=False)
    async def api_projects(_request: Request) -> JSONResponse:
        from opencode_search.handlers import handle_list_indexed_projects, handle_project_status
        data = await handle_list_indexed_projects()
        # Enrich with chunk counts
        projects = []
        for p in data.get("projects", []):
            try:
                status = await handle_project_status(path=p["path"])
                p["chunks"] = status.get("chunks")
                p["watching"] = status.get("watching", False)
            except Exception:
                pass
            projects.append(p)
        return JSONResponse({"projects": projects})

    @mcp.custom_route("/api/overview", methods=["GET"], include_in_schema=False)
    async def api_overview(request: Request) -> JSONResponse:
        from opencode_search.handlers import handle_project_structure
        from opencode_search.mcp import overview as mcp_overview
        project = request.query_params.get("project", "")
        what = request.query_params.get("what", "structure")
        if what == "projects":
            result = await mcp_overview(what="projects")
            return JSONResponse(result)
        if not project:
            return JSONResponse({"error": "project param required"}, status_code=400)
        if what == "structure":
            result = await handle_project_structure(project_path=project, max_depth=4)
        else:
            result = await mcp_overview(project_path=project, what=what)
        return JSONResponse(result)

    @mcp.custom_route("/api/communities", methods=["GET"], include_in_schema=False)
    async def api_communities(request: Request) -> JSONResponse:
        from opencode_search.handlers import handle_get_communities
        project = request.query_params.get("project", "")
        try:
            top_k = int(request.query_params.get("top_k", "50"))
        except (ValueError, TypeError):
            return JSONResponse({"error": "top_k must be an integer"}, status_code=400)
        if not project:
            return JSONResponse({"error": "project param required"}, status_code=400)
        result = await handle_get_communities(project_path=project, top_k=top_k)
        return JSONResponse(result)

    @mcp.custom_route("/api/start_watching", methods=["POST"], include_in_schema=False)
    async def api_start_watching(request: Request) -> JSONResponse:
        """Start (or resume) the file watcher for a project. POST {project}."""
        from opencode_search.handlers._watch import handle_ensure_project_watching
        body: dict = {}
        with contextlib.suppress(Exception):
            body = await request.json()
        project = body.get("project") or request.query_params.get("project", "")
        if not project:
            return JSONResponse({"error": "project param required"}, status_code=400)
        result = await handle_ensure_project_watching(path=project, persist=True)
        return JSONResponse(result)

    @mcp.custom_route("/api/stop_watching", methods=["POST"], include_in_schema=False)
    async def api_stop_watching(request: Request) -> JSONResponse:
        """Stop the file watcher for a project. POST {project}."""
        from opencode_search.handlers._watch import handle_stop_watching
        body: dict = {}
        with contextlib.suppress(Exception):
            body = await request.json()
        project = body.get("project") or request.query_params.get("project", "")
        if not project:
            return JSONResponse({"error": "project param required"}, status_code=400)
        result = await handle_stop_watching(path=project)
        return JSONResponse(result)

    @mcp.custom_route("/api/remove_project", methods=["POST"], include_in_schema=False)
    async def api_remove_project(request: Request) -> JSONResponse:
        """Remove a project from the registry. POST {project, delete_index?}."""
        from opencode_search.handlers._vacuum import handle_remove_project
        body: dict = {}
        with contextlib.suppress(Exception):
            body = await request.json()
        project = body.get("project") or request.query_params.get("project", "")
        if not project:
            return JSONResponse({"error": "project param required"}, status_code=400)
        delete_index = bool(body.get("delete_index", False))
        result = await handle_remove_project(project_path=project, delete_index=delete_index)
        return JSONResponse(result)


# ---------------------------------------------------------------------------
# Sub-registrar: wiki
# ---------------------------------------------------------------------------

def _register_wiki_routes(mcp: FastMCP) -> None:
    @mcp.custom_route("/api/wiki", methods=["GET"], include_in_schema=False)
    async def api_wiki_list(request: Request) -> JSONResponse:
        from opencode_search.config import get_project_wiki_dir
        project = request.query_params.get("project", "")
        if not project:
            return JSONResponse({"error": "project param required"}, status_code=400)
        wiki_dir = get_project_wiki_dir(project)
        pages = sorted(p.stem for p in wiki_dir.glob("*.md")) if wiki_dir.exists() else []
        return JSONResponse({"project": project, "pages": pages, "total": len(pages)})

    @mcp.custom_route("/api/wiki/page", methods=["GET"], include_in_schema=False)
    async def api_wiki_page(request: Request) -> JSONResponse:
        from opencode_search.config import get_project_wiki_dir
        project = request.query_params.get("project", "")
        name = request.query_params.get("name", "")
        if not project or not name:
            return JSONResponse({"error": "project and name params required"}, status_code=400)
        wiki_dir = get_project_wiki_dir(project)
        page_path = (wiki_dir / f"{name}.md").resolve()
        if not str(page_path).startswith(str(wiki_dir.resolve())):
            return JSONResponse({"error": "Invalid page name"}, status_code=400)
        if not page_path.exists():
            return JSONResponse({"error": f"Page not found: {name}"}, status_code=404)
        return JSONResponse({"name": name, "content": page_path.read_text(errors="replace")})

    @mcp.custom_route("/api/wiki_lint", methods=["GET"], include_in_schema=False)
    async def api_wiki_lint(request: Request) -> JSONResponse:
        """Health-check the wiki: page count, stale pages, missing entries."""
        from opencode_search.handlers._wiki import handle_wiki_lint
        project = request.query_params.get("project", "")
        if not project:
            return JSONResponse({"error": "project param required"}, status_code=400)
        result = await handle_wiki_lint(project_path=project)
        return JSONResponse(result)

    @mcp.custom_route("/api/suggested_questions", methods=["GET"], include_in_schema=False)
    async def api_suggested_questions(request: Request) -> JSONResponse:
        """Questions the graph is uniquely positioned to answer."""
        from opencode_search.handlers._graph import handle_suggest_questions
        project = request.query_params.get("project", "")
        if not project:
            return JSONResponse({"error": "project param required"}, status_code=400)
        try:
            top_n = int(request.query_params.get("top_n", "7"))
        except (ValueError, TypeError):
            return JSONResponse({"error": "top_n must be an integer"}, status_code=400)
        result = await handle_suggest_questions(project_path=project, top_n=top_n)
        return JSONResponse(result)


# ---------------------------------------------------------------------------
# Sub-registrar: search + knowledge queries
# ---------------------------------------------------------------------------

def _register_search_routes(mcp: FastMCP) -> None:
    @mcp.custom_route("/api/ask", methods=["GET"], include_in_schema=False)
    async def api_ask(request: Request) -> JSONResponse:
        from opencode_search.handlers import handle_global_search
        from opencode_search.handlers._wiki import handle_wiki_query
        project = request.query_params.get("project", "")
        q = request.query_params.get("q", "")
        scope = request.query_params.get("scope", "all")
        if not project or not q:
            return JSONResponse({"error": "project and q params required"}, status_code=400)
        if scope == "wiki":
            result = await handle_wiki_query(query=q, project_path=project, top_k=10)
        elif scope == "global":
            from opencode_search.handlers._global_search import handle_global_synthesis
            result = await handle_global_synthesis(query=q, project_path=project)
        elif scope == "feature":
            from opencode_search.handlers._feature import handle_ask_feature
            result = await handle_ask_feature(query=q, project_path=project)
        elif scope == "business":
            from opencode_search.handlers._business import handle_ask_business
            result = await handle_ask_business(query=q, project_path=project, top_k=10)
        else:
            result = await handle_global_search(query=q, project_path=project, top_k=10)
        return JSONResponse(result)

    @mcp.custom_route("/api/feature", methods=["GET"], include_in_schema=False)
    async def api_feature(request: Request) -> JSONResponse:
        """Feature trace: entry points + call chain + algorithm + design rationale."""
        from opencode_search.handlers._feature import handle_ask_feature
        project = request.query_params.get("project", "")
        q = request.query_params.get("q", "")
        top_k = int(request.query_params.get("top_k", "15"))
        if not project or not q:
            return JSONResponse({"error": "project and q params required"}, status_code=400)
        result = await handle_ask_feature(query=q, project_path=project, top_k=top_k)
        return JSONResponse(result)

    @mcp.custom_route("/api/search", methods=["GET"], include_in_schema=False)
    async def api_search(request: Request) -> JSONResponse:
        from opencode_search.handlers import handle_search_code
        project = request.query_params.get("project", "")
        q = request.query_params.get("q", "")
        scope = request.query_params.get("scope", "code")
        top_k = int(request.query_params.get("top_k", "10"))
        if not q:
            return JSONResponse({"error": "q param required"}, status_code=400)
        paths = [project] if project else None
        result = await handle_search_code(query=q, project_paths=paths, top_k=top_k)
        if scope == "docs" and "results" in result:
            doc_langs = {"wiki", "knowledge_base", "markdown", "rst", "text"}
            result["results"] = [
                r for r in result["results"]
                if r.get("language", "") in doc_langs or r.get("path", "").endswith((".md", ".rst", ".txt"))
            ]
        return JSONResponse(result)

    @mcp.custom_route("/api/patterns", methods=["GET"], include_in_schema=False)
    async def api_patterns(request: Request) -> JSONResponse:
        from opencode_search.handlers import handle_detect_patterns
        project = request.query_params.get("project", "")
        if not project:
            return JSONResponse({"error": "project param required"}, status_code=400)
        result = await handle_detect_patterns(project_path=project)
        return JSONResponse(result)

    @mcp.custom_route("/api/analyze_patterns", methods=["POST"], include_in_schema=False)
    async def api_analyze_patterns(request: Request) -> JSONResponse:
        from opencode_search.handlers import handle_analyze_patterns_llm
        project = request.query_params.get("project", "")
        force = request.query_params.get("force", "").lower() in ("1", "true", "yes")
        if not project:
            return JSONResponse({"error": "project param required"}, status_code=400)
        result = await handle_analyze_patterns_llm(project_path=project, force=force)
        return JSONResponse(result)

    @mcp.custom_route("/api/feature_map", methods=["GET"], include_in_schema=False)
    async def api_feature_map(request: Request) -> JSONResponse:
        """Business knowledge map: all communities grouped by semantic_type."""
        from opencode_search.handlers._business import handle_feature_map
        project = request.query_params.get("project", "")
        if not project:
            return JSONResponse({"error": "project required"}, status_code=400)
        result = await handle_feature_map(project_path=project)
        return JSONResponse(result)

    @mcp.custom_route("/api/business_rules", methods=["GET"], include_in_schema=False)
    async def api_business_rules(request: Request) -> JSONResponse:
        """Return communities classified as business_rule."""
        from opencode_search.handlers._business import handle_business_rules
        project = request.query_params.get("project", "")
        if not project:
            return JSONResponse({"error": "project required"}, status_code=400)
        result = await handle_business_rules(project_path=project)
        return JSONResponse(result)

    @mcp.custom_route("/api/process_flows", methods=["GET"], include_in_schema=False)
    async def api_process_flows(request: Request) -> JSONResponse:
        """Return communities classified as business_process."""
        from opencode_search.handlers._business import handle_process_flows
        project = request.query_params.get("project", "")
        if not project:
            return JSONResponse({"error": "project required"}, status_code=400)
        result = await handle_process_flows(project_path=project)
        return JSONResponse(result)

    @mcp.custom_route("/api/ask_business", methods=["GET"], include_in_schema=False)
    async def api_ask_business(request: Request) -> JSONResponse:
        """Answer business-domain questions using semantic-type–classified communities."""
        from opencode_search.handlers._business import handle_ask_business
        project = request.query_params.get("project", "")
        q = request.query_params.get("q", "")
        if not project or not q:
            return JSONResponse({"error": "project and q params required"}, status_code=400)
        result = await handle_ask_business(query=q, project_path=project)
        return JSONResponse(result)


# ---------------------------------------------------------------------------
# Sub-registrar: graph analysis
# ---------------------------------------------------------------------------

def _register_graph_routes(mcp: FastMCP) -> None:
    @mcp.custom_route("/api/graph", methods=["GET"], include_in_schema=False)
    async def api_graph(request: Request) -> JSONResponse:
        from opencode_search.handlers import (
            handle_detect_impact,
            handle_get_callees,
            handle_get_callers,
            handle_get_symbol,
            handle_trace_path,
        )
        project = request.query_params.get("project", "")
        symbol = request.query_params.get("symbol", "")
        relation = request.query_params.get("relation", "definition")
        to_sym = request.query_params.get("to", "")
        try:
            depth = int(request.query_params.get("depth", "5"))
        except (ValueError, TypeError):
            return JSONResponse({"error": "depth must be an integer"}, status_code=400)
        if not project or not symbol:
            return JSONResponse({"error": "project and symbol params required"}, status_code=400)
        if relation == "definition":
            result = await handle_get_symbol(name=symbol, project_path=project)
        elif relation == "callers":
            result = await handle_get_callers(symbol=symbol, project_path=project, depth=depth)
        elif relation == "callees":
            result = await handle_get_callees(symbol=symbol, project_path=project, depth=depth)
        elif relation == "impact":
            result = await handle_detect_impact(symbol=symbol, project_path=project)
        elif relation == "impact_narrative":
            from opencode_search.handlers._impact import handle_impact_narrative
            result = await handle_impact_narrative(symbol=symbol, project_path=project)
        elif relation == "path" and to_sym:
            result = await handle_trace_path(from_symbol=symbol, to_symbol=to_sym, project_path=project)
        elif relation == "semantic_trace" and to_sym:
            from opencode_search.handlers._trace import handle_semantic_trace
            result = await handle_semantic_trace(from_query=symbol, to_query=to_sym, project_path=project)
        else:
            return JSONResponse({"error": "Invalid relation or missing to param"}, status_code=400)
        return JSONResponse(result)

    @mcp.custom_route("/api/graph_export", methods=["GET"], include_in_schema=False)
    async def api_graph_export(request: Request):
        from starlette.responses import Response

        from opencode_search.handlers import handle_graph_export
        project = request.query_params.get("project", "")
        fmt = request.query_params.get("format", "json")
        try:
            max_nodes = int(request.query_params.get("max_nodes", "5000"))
        except (ValueError, TypeError):
            return JSONResponse({"error": "max_nodes must be an integer"}, status_code=400)
        if not project:
            return JSONResponse({"error": "project param required"}, status_code=400)
        result = await handle_graph_export(project_path=project, format=fmt, max_nodes=max_nodes)
        if fmt == "graphml" and "graphml" in result:
            return Response(
                content=result["graphml"],
                media_type="application/xml",
                headers={"Content-Disposition": "attachment; filename=knowledge_graph.graphml"},
            )
        return JSONResponse(result)

    @mcp.custom_route("/api/service_mesh", methods=["GET"], include_in_schema=False)
    async def api_service_mesh(request: Request) -> JSONResponse:
        """Detect inter-service communication patterns for a project."""
        from opencode_search.handlers._service_mesh import handle_detect_service_mesh
        project = request.query_params.get("project", "")
        if not project:
            return JSONResponse({"error": "project param required"}, status_code=400)
        result = await handle_detect_service_mesh(project_path=project)
        return JSONResponse(result)

    @mcp.custom_route("/api/impact_narrative", methods=["GET"], include_in_schema=False)
    async def api_impact_narrative(request: Request) -> JSONResponse:
        """Generate natural-language impact analysis for a symbol."""
        from opencode_search.handlers._impact import handle_impact_narrative
        project = request.query_params.get("project", "")
        symbol = request.query_params.get("symbol", "")
        if not project or not symbol:
            return JSONResponse({"error": "project and symbol params required"}, status_code=400)
        result = await handle_impact_narrative(symbol=symbol, project_path=project)
        return JSONResponse(result)

    @mcp.custom_route("/api/semantic_trace", methods=["GET"], include_in_schema=False)
    async def api_semantic_trace(request: Request) -> JSONResponse:
        """Trace a call flow from one concept to another."""
        from opencode_search.handlers._trace import handle_semantic_trace
        project = request.query_params.get("project", "")
        from_q = request.query_params.get("from", "")
        to_q = request.query_params.get("to", "")
        if not project or not from_q or not to_q:
            return JSONResponse({"error": "project, from, and to params required"}, status_code=400)
        result = await handle_semantic_trace(from_query=from_q, to_query=to_q, project_path=project)
        return JSONResponse(result)

    @mcp.custom_route("/api/build_hierarchy", methods=["POST"], include_in_schema=False)
    async def api_build_hierarchy(request: Request) -> JSONResponse:
        """Trigger recursive Leiden hierarchy build for a project."""
        body = {}
        with contextlib.suppress(Exception):
            body = await request.json()
        project = body.get("project") or request.query_params.get("project", "")
        if not project:
            return JSONResponse({"error": "project required"}, status_code=400)
        result = await asyncio.to_thread(_hierarchy_build_sync, project)
        return JSONResponse(result)

    @mcp.custom_route("/api/enrich_hierarchy", methods=["POST"], include_in_schema=False)
    async def api_enrich_hierarchy(request: Request) -> JSONResponse:
        """Submit background job to LLM-enrich level-2+ macro-communities."""
        from opencode_search.handlers._enrichment import handle_enrich_hierarchy
        from opencode_search.jobs import submit_job
        body = {}
        with contextlib.suppress(Exception):
            body = await request.json()
        project = body.get("project") or request.query_params.get("project", "")
        if not project:
            return JSONResponse({"error": "project required"}, status_code=400)
        job = submit_job(
            handle_enrich_hierarchy(project_path=project),
            action="enrich_hierarchy",
            project_path=project,
        )
        return JSONResponse({
            "status": "started",
            "job_id": job.id,
            "poll_url": f"/api/jobs/{job.id}",
            "message": "Hierarchy enrichment running in background.",
        })

    @mcp.custom_route("/api/import_cycles", methods=["GET"], include_in_schema=False)
    async def api_import_cycles(request: Request) -> JSONResponse:
        """Circular import dependencies — Tarjan SCC on file-level IMPORTS graph."""
        from opencode_search.handlers._graph import handle_import_cycles
        project = request.query_params.get("project", "")
        if not project:
            return JSONResponse({"error": "project param required"}, status_code=400)
        try:
            max_cycle_length = int(request.query_params.get("max_cycle_length", "8"))
            top_n = int(request.query_params.get("top_n", "20"))
        except (ValueError, TypeError):
            return JSONResponse({"error": "max_cycle_length and top_n must be integers"}, status_code=400)
        result = await handle_import_cycles(
            project_path=project, max_cycle_length=max_cycle_length, top_n=top_n,
        )
        return JSONResponse(result)

    @mcp.custom_route("/api/graph_diff", methods=["GET"], include_in_schema=False)
    async def api_graph_diff(request: Request) -> JSONResponse:
        """What changed in the graph since a given ISO timestamp."""
        from opencode_search.handlers._graph import handle_graph_diff
        project = request.query_params.get("project", "")
        since = request.query_params.get("since", "")
        if not project:
            return JSONResponse({"error": "project param required"}, status_code=400)
        result = await handle_graph_diff(project_path=project, since=since)
        return JSONResponse(result)

    @mcp.custom_route("/api/callflow_html", methods=["GET"], include_in_schema=False)
    async def api_callflow_html(request: Request) -> HTMLResponse:
        """Render a call chain as a standalone Mermaid HTML page.

        Params: project, symbol, direction=callees|callers, depth=5, format=html|mermaid
        """
        from opencode_search.handlers._graph import handle_callflow_html as _handle
        project = request.query_params.get("project", "")
        symbol = request.query_params.get("symbol", "")
        direction = request.query_params.get("direction", "callees")
        depth = int(request.query_params.get("depth", "5"))
        fmt = request.query_params.get("format", "html")
        if not project or not symbol:
            return HTMLResponse("<h1>Error: project and symbol params required</h1>", status_code=400)
        result = await _handle(
            symbol=symbol, project_path=project, direction=direction, depth=depth, fmt=fmt,
        )
        if "error" in result:
            return HTMLResponse(f"<h1>Error: {result['error']}</h1>", status_code=404)
        if fmt == "mermaid":
            from starlette.responses import PlainTextResponse
            return PlainTextResponse(result.get("mermaid", ""))
        return HTMLResponse(result.get("html", "<html><body>No diagram</body></html>"))

    @mcp.custom_route("/api/surprising_connections", methods=["GET"], include_in_schema=False)
    async def api_surprising_connections(request: Request) -> JSONResponse:
        """Cross-community bridges: edges connecting nodes in different architectural clusters."""
        project = request.query_params.get("project", "")
        try:
            top_n = int(request.query_params.get("top_n", "20"))
        except (ValueError, TypeError):
            return JSONResponse({"error": "top_n must be an integer"}, status_code=400)
        if not project:
            return JSONResponse({"error": "project param required"}, status_code=400)
        return JSONResponse(await asyncio.to_thread(_run_surprising_sync, project, top_n))

    @mcp.custom_route("/api/pr_impact", methods=["GET", "POST"], include_in_schema=False)
    async def api_pr_impact(request: Request) -> JSONResponse:
        """PR impact: changed files → communities touched + risk level.

        GET  ?project=...&base_branch=main
        POST {project, files: [...], base_branch: "main"}
        """
        from opencode_search.handlers._pr_impact import handle_pr_impact
        if request.method == "POST":
            body: dict = {}
            with contextlib.suppress(Exception):
                body = await request.json()
            project = body.get("project") or request.query_params.get("project", "")
            files = body.get("files") or None
            base_branch = body.get("base_branch", "main")
        else:
            project = request.query_params.get("project", "")
            files = None
            base_branch = request.query_params.get("base_branch", "main")
        if not project:
            return JSONResponse({"error": "project param required"}, status_code=400)
        result = await handle_pr_impact(project_path=project, files=files, base_branch=base_branch)
        return JSONResponse(result)

    @mcp.custom_route("/api/tree_html", methods=["GET"], include_in_schema=False)
    async def api_tree_html(request: Request):
        """Interactive file tree HTML. ?project=...&format=html|json&max_files=2000"""
        from starlette.responses import Response as _Resp

        from opencode_search.handlers._tree_html import handle_tree_html
        project = request.query_params.get("project", "")
        fmt = request.query_params.get("format", "html")
        try:
            max_files = int(request.query_params.get("max_files", "2000"))
        except (ValueError, TypeError):
            return JSONResponse({"error": "max_files must be an integer"}, status_code=400)
        if not project:
            return JSONResponse({"error": "project param required"}, status_code=400)
        result = await handle_tree_html(project_path=project, fmt=fmt, max_files=max_files)
        if "error" in result:
            return JSONResponse(result, status_code=400)
        if fmt == "html" and "html" in result:
            return _Resp(content=result["html"], media_type="text/html")
        return JSONResponse(result)


# ---------------------------------------------------------------------------
# Sub-registrar: chat + debug
# ---------------------------------------------------------------------------

def _register_chat_routes(mcp: FastMCP) -> None:
    @mcp.custom_route("/api/chat", methods=["POST"], include_in_schema=False)
    async def api_kb_chat(request: Request) -> JSONResponse:
        """Unified chat: auto-detects intent, returns humanized prose.

        Body JSON: {"project": str, "query": str, "history": list[dict] | null}
        """
        from opencode_search.handlers._chat_router import handle_chat_auto
        body: dict = {}
        with contextlib.suppress(Exception):
            body = await request.json()
        project = body.get("project", "")
        query = body.get("query", "")
        history = body.get("history") or []
        if not project or not query:
            return JSONResponse({"error": "project and query required"}, status_code=400)
        result = await handle_chat_auto(
            query=query,
            project_path=project,
            conversation_history=history,
        )
        return JSONResponse(result)

    @mcp.custom_route("/api/chat_stream", methods=["POST"], include_in_schema=False)
    async def api_kb_chat_stream(request: Request) -> StreamingResponse | JSONResponse:
        """Streaming chat: yields NDJSON tokens as they are generated.

        Body JSON: {"project": str, "query": str, "history": list[dict] | null}
        Each line: {"type":"token","text":"..."} or {"type":"done","intent":"...",...}
        """
        body: dict = {}
        with contextlib.suppress(Exception):
            body = await request.json()
        project = body.get("project", "")
        query = body.get("query", "")
        history = body.get("history") or []
        if not project or not query:
            return JSONResponse({"error": "project and query required"}, status_code=400)

        from opencode_search.handlers._chat_router import handle_chat_auto_stream
        from opencode_search.metrics import record_stream_cancelled

        async def _gen():
            from opencode_search.daemon_runtime import reload_pending
            from opencode_search.metrics import record_stream_error
            _RELOAD_NOTICE = json.dumps({"type": "reload", "retry_after_ms": 3000})
            try:
                async for chunk in handle_chat_auto_stream(
                    query=query,
                    project_path=project,
                    conversation_history=history,
                ):
                    if reload_pending.is_set():
                        yield f"data: {_RELOAD_NOTICE}\n\n"
                        return
                    if await request.is_disconnected():
                        record_stream_cancelled()
                        return
                    yield f"data: {json.dumps(chunk)}\n\n"
            except Exception as exc:
                record_stream_error("unknown")
                err_payload = json.dumps({"type": "error", "message": str(exc)[:500]})
                yield f"data: {err_payload}\n\n"

        return StreamingResponse(
            _gen(),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "X-Accel-Buffering": "no",
            },
        )

    @mcp.custom_route("/api/debug", methods=["POST"], include_in_schema=False)
    async def api_debug_trace(request: Request) -> JSONResponse:
        """Root-cause trace from a stack trace.

        Body JSON: {"project": str, "traceback": str, "error_message": str, "include_fix": bool}
        """
        from opencode_search.handlers._debug_trace import handle_debug_trace
        body: dict = {}
        with contextlib.suppress(Exception):
            body = await request.json()
        project = body.get("project", "")
        traceback_text = body.get("traceback", "")
        error_message = body.get("error_message", "")
        include_fix = bool(body.get("include_fix", True))
        if not project or not traceback_text:
            return JSONResponse({"error": "project and traceback required"}, status_code=400)
        result = await handle_debug_trace(
            traceback=traceback_text,
            project_path=project,
            error_message=error_message,
            include_fix=include_fix,
        )
        return JSONResponse(result)


# ---------------------------------------------------------------------------
# Sub-registrar: knowledge-base health + admin ops
# ---------------------------------------------------------------------------

def _register_kb_routes(mcp: FastMCP) -> None:
    @mcp.custom_route("/api/kb_health", methods=["GET"], include_in_schema=False)
    async def api_kb_health(request: Request) -> JSONResponse:
        """KB completeness snapshot: enrichment %, wiki count, patterns cache, last pipeline event."""
        from pathlib import Path

        from opencode_search.config import get_project_graph_db_path, get_project_wiki_dir
        from opencode_search.handlers._autopipeline import (
            auto_pipeline_enabled,
            get_pipeline_events,
        )
        from opencode_search.handlers._patterns import load_patterns_cache

        project = request.query_params.get("project", "")
        if not project:
            return JSONResponse({"error": "project param required"}, status_code=400)

        result: dict = {"project_path": project, "auto_pipeline_enabled": auto_pipeline_enabled()}

        # Enrichment stats from graph DB
        try:
            from opencode_search.graph.storage import GraphStorage
            db_path = get_project_graph_db_path(project)
            if Path(db_path).exists():
                gs = GraphStorage(db_path)
                gs.open()
                try:
                    communities = gs.get_communities(min_node_count=2)
                    total = len(communities)
                    all_enriched = sum(
                        1 for c in communities if c.title and f"Community {c.id}" != c.title
                    )
                    result["total_communities"] = total
                    result["enriched_communities"] = all_enriched
                    result["enrichment_pct"] = round(all_enriched / total * 100, 1) if total else 0.0
                    # Per-level breakdown so users can see if only level-1 is enriched
                    max_level = gs.get_max_community_level()
                    levels_data: dict[str, dict] = {}
                    for lvl in range(1, max_level + 1):
                        lvl_comms = [c for c in communities if c.level == lvl]
                        e = sum(
                            1 for c in lvl_comms
                            if c.title and f"Community {c.id}" != c.title
                        )
                        levels_data[str(lvl)] = {
                            "total": len(lvl_comms),
                            "enriched": e,
                            "pct": round(e / len(lvl_comms) * 100, 1) if lvl_comms else 0.0,
                        }
                    result["enrichment_by_level"] = levels_data
                finally:
                    gs.close()
        except Exception:
            result["total_communities"] = None
            result["enriched_communities"] = None
            result["enrichment_pct"] = None

        # Wiki page count
        try:
            wiki_dir = get_project_wiki_dir(project)
            md_files = list(wiki_dir.glob("*.md")) if wiki_dir.exists() else []
            result["wiki_page_count"] = len(md_files)
        except Exception:
            result["wiki_page_count"] = None

        # Patterns cache
        try:
            cached = load_patterns_cache(project)
            result["patterns_cached"] = cached is not None
            result["patterns_cached_at"] = cached.get("cached_at") if cached else None
            result["patterns_steps"] = cached.get("steps", []) if cached else []
        except Exception:
            result["patterns_cached"] = False
            result["patterns_cached_at"] = None
            result["patterns_steps"] = []

        # Last pipeline event
        events = get_pipeline_events()
        proj_events = [e for e in events if project in e.get("project_path", "")]
        result["last_pipeline_event"] = proj_events[-1] if proj_events else None

        return JSONResponse(result)

    @mcp.custom_route("/api/dedup", methods=["GET", "POST"], include_in_schema=False)
    async def api_dedup(request: Request) -> JSONResponse:
        """Deduplicate graph nodes. GET=dry_run preview; POST with {project,dry_run,threshold}."""
        from opencode_search.handlers._graph import handle_dedup_nodes
        if request.method == "POST":
            body: dict = {}
            with contextlib.suppress(Exception):
                body = await request.json()
            project = body.get("project") or request.query_params.get("project", "")
            dry_run = bool(body.get("dry_run", False))
            try:
                threshold = float(body.get("threshold", 0.88))
            except (ValueError, TypeError):
                return JSONResponse({"error": "threshold must be a float"}, status_code=400)
        else:
            project = request.query_params.get("project", "")
            dry_run = request.query_params.get("dry_run", "true").lower() != "false"
            try:
                threshold = float(request.query_params.get("threshold", "0.88"))
            except (ValueError, TypeError):
                return JSONResponse({"error": "threshold must be a float"}, status_code=400)
        if not project:
            return JSONResponse({"error": "project param required"}, status_code=400)
        result = await handle_dedup_nodes(project_path=project, threshold=threshold, dry_run=dry_run)
        return JSONResponse(result)

    @mcp.custom_route("/api/vacuum", methods=["GET", "POST"], include_in_schema=False)
    async def api_vacuum(request: Request) -> JSONResponse:
        """Storage cleanup: remove orphan index tier dirs. GET=dry_run; POST with {project,dry_run}."""
        from opencode_search.handlers._vacuum import handle_vacuum
        if request.method == "POST":
            body: dict = {}
            with contextlib.suppress(Exception):
                body = await request.json()
            project = body.get("project") or request.query_params.get("project", "")
            dry_run = bool(body.get("dry_run", False))
        else:
            project = request.query_params.get("project", "")
            dry_run = request.query_params.get("dry_run", "true").lower() != "false"
        if not project:
            return JSONResponse({"error": "project param required"}, status_code=400)
        result = await handle_vacuum(project_path=project, dry_run=dry_run)
        return JSONResponse(result)

    @mcp.custom_route("/api/git_hooks", methods=["GET", "POST"], include_in_schema=False)
    async def api_git_hooks(request: Request) -> JSONResponse:
        """Install (POST action=install) or uninstall (POST action=uninstall) git post-commit hooks."""
        from opencode_search.handlers._hooks import handle_git_hooks
        if request.method == "GET":
            project = request.query_params.get("project", "")
            if not project:
                return JSONResponse({"error": "project param required"}, status_code=400)
            from pathlib import Path as _Path
            git_dir = _Path(project).expanduser().resolve() / ".git"
            hook_path = git_dir / "hooks" / "post-commit"
            installed = hook_path.exists() and "opencode-search managed hook" in hook_path.read_text()
            return JSONResponse({"installed": installed, "hook_path": str(hook_path), "project_path": project})
        body: dict = {}
        with contextlib.suppress(Exception):
            body = await request.json()
        project = body.get("project") or request.query_params.get("project", "")
        action = body.get("action", "install")
        if not project:
            return JSONResponse({"error": "project required"}, status_code=400)
        result = await handle_git_hooks(project_path=project, install=(action == "install"))
        return JSONResponse(result)

    @mcp.custom_route("/api/reload", methods=["POST"], include_in_schema=False)
    async def api_reload(_request: Request) -> JSONResponse:
        import os
        pid = os.getpid()
        _spawn_daemon_restart_thread(pid)
        return JSONResponse({"status": "reloading", "pid": pid,
                             "note": "daemon restarting in ~3s"})


# ---------------------------------------------------------------------------
# Sub-registrar: ops (metrics, pipeline, SSE, alerts, jobs)
# ---------------------------------------------------------------------------

def _register_ops_routes(mcp: FastMCP) -> None:
    @mcp.custom_route("/api/metrics", methods=["GET"], include_in_schema=False)
    async def api_metrics(_request: Request) -> JSONResponse:
        import json as _json
        import time

        from opencode_search.daemon import _META_PATH
        from opencode_search.daemon_runtime import runtime_state
        from opencode_search.embeddings import get_cublas_metrics
        from opencode_search.metrics import get_metrics, get_stream_metrics
        data = get_metrics()
        data["chat_stream"] = get_stream_metrics()
        snap = runtime_state.snapshot()
        data["connected_clients"] = snap.get("active_clients", 0)
        data["client_ids"] = snap.get("client_ids", [])
        try:
            info = _json.loads(_META_PATH.read_text(encoding="utf-8"))
            started_at = info.get("started_at")
            data["uptime_s"] = round(time.time() - started_at, 1) if started_at else None
        except Exception:
            data["uptime_s"] = None
        data["cublas_breaker"] = get_cublas_metrics()
        return JSONResponse(data)

    @mcp.custom_route("/api/auto_pipeline_status", methods=["GET"], include_in_schema=False)
    async def api_auto_pipeline_status(_request: Request) -> JSONResponse:
        from opencode_search.handlers._autopipeline import (
            auto_pipeline_enabled,
            get_pipeline_events,
        )
        return JSONResponse({
            "enabled": auto_pipeline_enabled(),
            "events": get_pipeline_events()[-20:],  # last 20 events
        })

    @mcp.custom_route("/api/federation", methods=["GET"], include_in_schema=False)
    async def api_federation(request: Request) -> JSONResponse:
        from opencode_search.handlers._federation import (
            handle_add_federation_member,
            handle_discover_federation,
            handle_index_federation,
            handle_list_federation,
            handle_remove_federation_member,
        )
        project = request.query_params.get("project", "")
        action = request.query_params.get("action", "list")
        member = request.query_params.get("member", "")

        if not project:
            return JSONResponse({"error": "project param required"}, status_code=400)

        if action == "discover":
            result = await handle_discover_federation(project_path=project)
        elif action == "add":
            if not member:
                return JSONResponse({"error": "member param required for action=add"}, status_code=400)
            result = await handle_add_federation_member(root_path=project, member_path=member)
        elif action == "remove":
            if not member:
                return JSONResponse({"error": "member param required for action=remove"}, status_code=400)
            result = await handle_remove_federation_member(root_path=project, member_path=member)
        elif action == "index":
            result = await handle_index_federation(root_path=project)
        else:
            result = await handle_list_federation(project_path=project)

        return JSONResponse(result)

    # ── Metrics history, SSE, alerts, system status ──────────────────────

    @mcp.custom_route("/api/metrics/history", methods=["GET"], include_in_schema=False)
    async def api_metrics_history(request: Request) -> JSONResponse:
        """Return bucketed time-series search metrics for charting."""
        try:
            hours = float(request.query_params.get("hours", "24"))
            bucket_m = int(request.query_params.get("bucket_m", "5"))
        except ValueError:
            hours, bucket_m = 24, 5
        bucket_s = bucket_m * 60
        cutoff = time.time() - hours * 3600

        try:
            with _db_lock:
                conn = _get_metrics_db()
                rows = conn.execute(
                    "SELECT ts, latency_ms, result_count FROM search_events WHERE ts > ? ORDER BY ts",
                    (cutoff,),
                ).fetchall()
                conn.close()
        except Exception:
            rows = []

        # Bucket into time windows
        buckets: dict[int, list] = {}
        for row in rows:
            bucket_key = int(row["ts"] // bucket_s) * bucket_s
            buckets.setdefault(bucket_key, []).append(row)

        now_bucket = int(time.time() // bucket_s) * bucket_s
        start_bucket = int(cutoff // bucket_s) * bucket_s
        ts_list, p50_list, p95_list, zero_pct_list, count_list = [], [], [], [], []

        t = start_bucket
        while t <= now_bucket:
            events = buckets.get(t, [])
            ts_list.append(t * 1000)  # milliseconds for JS Date
            if events:
                lats = sorted(e["latency_ms"] for e in events if e["latency_ms"] is not None)
                p50 = lats[len(lats) // 2] if lats else 0
                p95 = lats[int(len(lats) * 0.95)] if lats else 0
                zero_pct = 100 * sum(1 for e in events if (e["result_count"] or 0) == 0) / len(events)
            else:
                p50, p95, zero_pct = 0, 0, 0
            p50_list.append(round(p50, 1))
            p95_list.append(round(p95, 1))
            zero_pct_list.append(round(zero_pct, 1))
            count_list.append(len(events))
            t += bucket_s

        return JSONResponse({
            "timestamps": ts_list,
            "latency_p50": p50_list,
            "latency_p95": p95_list,
            "zero_result_pct": zero_pct_list,
            "search_count": count_list,
            "hours": hours,
            "bucket_m": bucket_m,
        })

    @mcp.custom_route("/api/events/stream", methods=["GET"], include_in_schema=False)
    async def api_events_stream(request: Request) -> StreamingResponse:
        """SSE endpoint — emits live metrics every 5 seconds.

        ?max_events=N stops after N events (used by tests to avoid infinite stream).
        """
        from opencode_search.daemon_runtime import runtime_state
        from opencode_search.metrics import get_metrics

        _start_time = time.time()
        try:
            max_events = int(request.query_params.get("max_events", 0))
        except (ValueError, TypeError):
            max_events = 0

        async def _generate():
            from opencode_search.daemon_runtime import reload_pending
            from opencode_search.jobs import list_jobs
            _RELOAD_NOTICE = json.dumps({"type": "reload", "retry_after_ms": 3000})
            count = 0
            _last_job_states: dict[str, str] = {}
            try:
                interval = 5
                while True:
                    if reload_pending.is_set():
                        yield f"data: {_RELOAD_NOTICE}\n\n"
                        return
                    if max_events and count >= max_events:
                        return
                    if await request.is_disconnected():
                        return
                    m = get_metrics()
                    lms = m.get("latency_ms", {})
                    payload = json.dumps({
                        "type": "metrics",
                        "call_count": m.get("call_count", 0),
                        "latency_p50_ms": lms.get("p50") or 0,
                        "latency_p95_ms": lms.get("p95") or 0,
                        "zero_result_pct": round(
                            100 * m.get("zero_result_count", 0) / max(1, m.get("call_count", 1)), 1
                        ),
                        "avg_top_score": round(m.get("avg_top_score") or 0, 3),
                        "connected_clients": len(runtime_state.active_clients),
                        "uptime_s": int(time.time() - _start_time),
                    })
                    yield f"data: {payload}\n\n"
                    # Emit job status change events (no polling needed on client side)
                    for job in list_jobs():
                        prev = _last_job_states.get(job.id)
                        if prev != job.status:
                            _last_job_states[job.id] = job.status
                            job_event = json.dumps({
                                "type": "job",
                                "job_id": job.id,
                                "action": job.action,
                                "project": job.project_path,
                                "status": job.status,
                                "error": job.error,
                            })
                            yield f"data: {job_event}\n\n"
                    count += 1
                    if max_events and count >= max_events:
                        return
                    # Sleep in small increments so disconnect/reload is detected quickly
                    for _ in range(interval * 10):
                        if reload_pending.is_set():
                            yield f"data: {_RELOAD_NOTICE}\n\n"
                            return
                        if await request.is_disconnected():
                            return
                        await asyncio.sleep(0.1)
            except (asyncio.CancelledError, GeneratorExit):
                return

        return StreamingResponse(
            _generate(),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "X-Accel-Buffering": "no",
            },
        )

    @mcp.custom_route("/api/alerts", methods=["GET"], include_in_schema=False)
    async def api_alerts_get(request: Request) -> JSONResponse:
        """Return alert rules and their current violation status."""
        from opencode_search.metrics import get_metrics

        rules = _load_alerts()
        m = get_metrics()
        lms = m.get("latency_ms", {})
        import os as _os
        try:
            _load1, _, _ = _os.getloadavg()
        except OSError:
            _load1 = 0.0
        current = {
            "latency_p95_ms": lms.get("p95") or 0,
            "zero_result_pct": round(100 * m.get("zero_result_count", 0) / max(1, m.get("call_count", 1)), 1),
            "load_avg_1m": round(_load1, 2),
        }
        violations = []
        for rule in rules:
            if not rule.get("enabled", True):
                continue
            metric_val = current.get(rule["metric"])
            if metric_val is None:
                continue
            op = rule.get("op", ">")
            threshold = rule.get("threshold", 0)
            triggered = (op == ">" and metric_val > threshold) or \
                        (op == "<" and metric_val < threshold) or \
                        (op == ">=" and metric_val >= threshold) or \
                        (op == "<=" and metric_val <= threshold)
            if triggered:
                violations.append({
                    "rule_id": rule["id"],
                    "name": rule["name"],
                    "message": f"{rule['name']}: {metric_val} {op} {threshold}",
                    "current": metric_val,
                    "threshold": threshold,
                })
        return JSONResponse({"rules": rules, "violations": violations, "current_metrics": current})

    @mcp.custom_route("/api/alerts", methods=["POST"], include_in_schema=False)
    async def api_alerts_post(request: Request) -> JSONResponse:
        """Save updated alert rules."""
        try:
            body = await request.json()
            rules = body.get("rules", [])
            _save_alerts(rules)
            return JSONResponse({"saved": len(rules)})
        except Exception as e:
            return JSONResponse({"error": str(e)}, status_code=400)

    @mcp.custom_route("/api/system_status", methods=["GET"], include_in_schema=False)
    async def api_system_status(request: Request) -> JSONResponse:
        """Serve cached ocs_status report, refreshing if stale (> 60s old)."""
        import sys
        cache_path = Path(__file__).parent.parent.parent.parent / ".ocs_status_cache.json"
        # Also check adjacent to the project root
        alt_cache = Path.home() / ".local" / "share" / "opencode-search" / "status_cache.json"

        for cp in [cache_path, alt_cache]:
            if cp.exists():
                age = time.time() - cp.stat().st_mtime
                if age < 120:
                    try:
                        return JSONResponse(json.loads(cp.read_text()))
                    except Exception:
                        pass

        # Build a quick status report synchronously (no tests)
        scripts_dir = Path(__file__).parent.parent.parent / "scripts"
        ocs_script = scripts_dir / "ocs_status.py"
        if not ocs_script.exists():
            return JSONResponse({"error": "ocs_status.py not found"}, status_code=503)

        try:
            import asyncio as _asyncio
            proc = await _asyncio.create_subprocess_exec(
                sys.executable, str(ocs_script), "--json", "--no-tests",
                "--cache", str(alt_cache),
                stdout=_asyncio.subprocess.PIPE,
                stderr=_asyncio.subprocess.PIPE,
                cwd=str(scripts_dir.parent),
            )
            try:
                stdout_b, _ = await _asyncio.wait_for(proc.communicate(), timeout=30)
            except TimeoutError:
                proc.kill()
                await proc.communicate()
                return JSONResponse({"error": "ocs_status.py timed out"}, status_code=500)
            stdout = stdout_b.decode() if stdout_b else ""
            if proc.returncode in (0, 1) and stdout.strip():
                try:
                    return JSONResponse(json.loads(stdout))
                except Exception:
                    pass
            # Serve the cache if it was just written
            if alt_cache.exists():
                return JSONResponse(json.loads(alt_cache.read_text()))
        except Exception as e:
            return JSONResponse({"error": str(e)}, status_code=500)

        return JSONResponse({"status": "unavailable"})

    @mcp.custom_route("/api/integrations_status", methods=["GET"], include_in_schema=False)
    async def api_integrations_status(_request: Request) -> JSONResponse:
        """Return all integration states by running configure_integrations.py --check --json."""
        import asyncio as _aio
        import sys as _sys
        from pathlib import Path as _Path
        scripts_dir = _Path(__file__).parent.parent.parent / "scripts"
        cfg_script = scripts_dir / "configure_integrations.py"
        if not cfg_script.exists():
            return JSONResponse({"error": "configure_integrations.py not found"}, status_code=404)
        try:
            proc = await _aio.create_subprocess_exec(
                _sys.executable, str(cfg_script), "--check", "--json",
                stdout=_aio.subprocess.PIPE, stderr=_aio.subprocess.PIPE,
            )
            stdout, _ = await _aio.wait_for(proc.communicate(), timeout=15.0)
            import json as _json
            return JSONResponse(_json.loads(stdout.decode(errors="replace")))
        except Exception as exc:
            return JSONResponse({"error": str(exc)}, status_code=500)

    @mcp.custom_route("/api/jobs", methods=["GET"], include_in_schema=False)
    async def api_jobs_list(request: Request) -> JSONResponse:
        """List background build jobs. ?project=...&action=... to filter."""
        from opencode_search.jobs import job_to_dict, list_jobs
        project = request.query_params.get("project") or None
        action = request.query_params.get("action") or None
        jobs = list_jobs(project_path=project, action=action)
        return JSONResponse({"jobs": [job_to_dict(j) for j in jobs], "total": len(jobs)})

    @mcp.custom_route("/api/jobs/{job_id}", methods=["GET"], include_in_schema=False)
    async def api_job_status(request: Request) -> JSONResponse:
        """Get status of a specific background job."""
        from opencode_search.jobs import get_job, job_to_dict
        job_id = request.path_params.get("job_id", "")
        job = get_job(job_id)
        if job is None:
            return JSONResponse({"error": f"Job {job_id!r} not found"}, status_code=404)
        return JSONResponse(job_to_dict(job))

    @mcp.custom_route("/api/jobs/{job_id}/cancel", methods=["POST"], include_in_schema=False)
    async def api_job_cancel(request: Request) -> JSONResponse:
        """Request cancellation of a running job."""
        from opencode_search.jobs import cancel_job, get_job, job_to_dict
        job_id = request.path_params.get("job_id", "")
        if get_job(job_id) is None:
            return JSONResponse({"error": f"Job {job_id!r} not found"}, status_code=404)
        cancelled = cancel_job(job_id)
        job = get_job(job_id)
        return JSONResponse({"cancelled": cancelled, "job": job_to_dict(job) if job else None})


# ---------------------------------------------------------------------------
# Public entry point — orchestrator
# ---------------------------------------------------------------------------

def register_dashboard_routes(mcp: FastMCP) -> None:
    """Attach all dashboard routes to the FastMCP instance."""
    _register_root_routes(mcp)
    _register_project_routes(mcp)
    _register_wiki_routes(mcp)
    _register_search_routes(mcp)
    _register_graph_routes(mcp)
    _register_chat_routes(mcp)
    _register_kb_routes(mcp)
    _register_ops_routes(mcp)
