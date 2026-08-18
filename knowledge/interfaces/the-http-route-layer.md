---
type: Interface
resource: src/rag_search/server/routes.py
title: The HTTP route layer
description: Starlette app.add_route through a per-module register(app) — the fixed registration order, the route table, and the four behaviours the URLs do not reveal.
tags: [http, starlette, routes, dashboard, sse, lease]
status: active
generated:
  by: claude/claude-opus-5
  at: 2026-08-18T01:45:55Z
---

# The HTTP route layer

Not FastAPI. There is no `APIRouter`, no decorator registry, no dependency injection. `create_app()`
starts from `mcp.streamable_http_app()`, adds four routes directly, calls `register(app)` on each
submodule, and mounts `/static` last. Each `routes_*.py` exports one `register(app)` that calls
`app.add_route`.

Registration order is fixed: **admin, project, graph, ops, pipeline, chat**. A module added
elsewhere in that tuple changes which handler wins a path collision.

## The table

| Route | Method | Module |
|---|---|---|
| `/healthz`, `/dashboard`, `/api/projects` | GET | `routes.py` |
| `/api/overview` | POST | `routes.py` |
| `/static` (mount), FastMCP streamable-HTTP | — | `routes.py` |
| `/` (redirect), `/api/watcher` | GET | `routes_admin.py` |
| `/api/storage_health` | GET | `routes_project.py` |
| `/api/graph_export` | GET | `routes_graph.py` |
| `/api/metrics` | GET | `routes_ops.py` |
| `/api/reload`, `/api/sweeps/pause`, `/api/sweeps/resume`, `/api/gpu/release` | POST | `routes_ops.py` |
| `/api/auto_pipeline_status` | GET | `routes_pipeline.py` |
| `/api/chat_stream` (SSE) | POST | `routes_chat.py` |

A former `routes_search.py` held `/api/suggested_questions` and was removed with the chat box's
prompts; the name was always a misnomer, since search itself is served over MCP.

## Four things the URLs do not say

**Blocking handlers run in a thread.** Head-of-line blocking on the event loop is the accepted cost
and the reason: `/api/watcher` looks like a dict read, resolves ~200 paths, and once stalled an
unrelated endpoint into a CI read timeout. Under 16 concurrent calls, `/healthz` median went 301 ms
→ 82 ms after the handoff.

**`/api/graph_export` is node-capped, edges first.** Uncapped, a 136-member federation collected
133,087 nodes; truncating nodes and edges independently made 100% of exported edges dangle there and
51.3% fleet-wide. Edges are selected first and nodes follow, which is the only order that keeps them
consistent — and it yields 88,299 usable edges against the old scheme's 34,546 from 41% of the node
slots.

**The sweeps pause is a lease, not a flag.** `pause_lease_remaining_s()` returns 0.0 past the
deadline, so a client that dies mid-hold cannot wedge sweeps off. `/api/reload` refuses with 409
while a lease is held, carrying the remaining seconds so the caller learns what owns the daemon.

**`/dashboard` has a second copy of its HTML.** `dashboard.py`'s literal page is the fallback when
`static/dashboard.html` is missing, and it must track the static file pane for pane — it has already
drifted, advertising views the real page no longer had.

Related: [server: how the outside world talks to the engine](../components/server.md) and
[the MCP tool surface](the-mcp-tool-surface.md), which shares this app.

## Guards

| Claim | Guard | File |
|---|---|---|
| pause/resume round-trips | `test_api_sweeps_pause_resume` | `test_http_surface.py` |
| export is capped and self-consistent | `test_api_graph_export`, `test_graph_export_edges_are_induced_by_nodes` | `test_http_surface.py` |
| storage health answers | `test_api_storage_health` | `test_http_surface.py` |
