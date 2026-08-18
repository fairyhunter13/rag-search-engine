---
type: Component
resource: src/rag_search/server/
title: "server: how the outside world talks to the engine"
description: One Starlette app carrying the FastMCP streamable-HTTP surface and the dashboard API — ten files whose recurring theme is refusing a request rather than answering it broadly.
tags: [server, mcp, starlette, routes, dashboard, overview]
status: active
generated:
  by: claude/claude-opus-5
  at: 2026-08-18T01:45:55Z
---

# server: how the outside world talks to the engine

Ten files, ~1,900 lines. `_overview.py` is the largest by far and assembles every `what=` payload;
`mcp.py` is the tool surface; six `routes_*.py` modules carry the dashboard API. The two contracts
have their own concepts:
[the MCP tool surface](../interfaces/the-mcp-tool-surface.md) and
[the HTTP route layer](../interfaces/the-http-route-layer.md).

## The pattern that repeats: refuse, do not widen

Every tool in `mcp.py` rejects an unrecognised argument instead of falling back to a broader answer,
and each rejection was written after the broad answer cost something measurable:

- an unscoped `search` used to scan every enabled project — 164.78 s against 7.01 s for the same
  query scoped to one, and it answered a question about this repo out of a member's vendored
  JavaScript;
- an unknown `scope` used to map to "no restriction", silently *widening* the corpus, which reads
  from the outside like a routing defect in the engine rather than a caller's typo.

`_overview.py` owns validation of `what`, so `mcp.overview` deliberately passes an unknown value
through rather than failing on project resolution first — otherwise the error names the wrong
problem.

## Blocking work never runs on the event loop

Every blocking route hands off to a thread. The one that was the exception —
`GET /api/watcher` — looks like a cheap dict read and is not: it resolves ~200 paths and may take
the registry's cross-process lock. Held on the loop it stalled every request behind it, and the
symptom was an unrelated endpoint's read timeout in CI. Under 16 concurrent `/api/watcher` calls,
`/healthz` median latency went from 301 ms to 82 ms once the handoff was added.

## The dashboard has two copies of its HTML

`dashboard.py` carries a literal page served only when `static/dashboard.html` is missing. It has
already drifted once — the docstring and fallback said four views while the static file carried six
— so a pane added or removed must be applied to both. The fallback advertising a view the real page
dropped is the failure this guards against.

## Where the daemon meets it

The server is one process with [the daemon](daemon.md)'s watcher, scheduler and sweeps threads —
[the daemon lives inside one core](../constraints/the-daemon-lives-inside-one-core.md) — and
`note_activity()` on every tool call is what keeps the idle unload from firing under load. Route
inventory and registration order live in
[the HTTP route layer](../interfaces/the-http-route-layer.md).
