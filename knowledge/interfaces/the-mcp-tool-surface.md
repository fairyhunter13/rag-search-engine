---
type: Interface
resource: src/rag_search/server/mcp.py
title: The MCP tool surface
description: Four tools — search, graph, overview, index — their signatures, what each refuses and what the refusal cost to learn, plus the behaviour all four share.
tags: [mcp, interface, tools, fastmcp, refusal]
status: active
generated:
  by: claude/claude-opus-5
  at: 2026-08-18T01:45:55Z
---

# The MCP tool surface

`mcp = FastMCP("rag-search", instructions=_PROMPT)`, four `@mcp.tool()` coroutines. `list_tools()`
**is** the registry — a static mirror list must not be added beside it, because a mirror is a copy
that can be wrong. A fifth tool, `ask`, was retired 2026-07-29:
[the fifth tool returned assembled prose](../../docs/decisions/2026-07-29-the-fifth-tool-returned-assembled-prose.md).

```python
async def search(query: str, scope: str = "code", project_paths: list[str] | None = None,
                 top_k: int = 8, verbosity: str = "compact", ctx: Context | None = None) -> str
async def graph(symbol: str, project_path: str = "", relation: str = "definition",
                to_symbol: str = "", ctx: Context | None = None) -> str
async def overview(project_path: str = "", what: str = "structure", query: str = "",
                   ctx: Context | None = None) -> str
async def index(project_path: str, enabled: bool = True, confirm_members: bool = False) -> str
```

## What each refuses

**`search` with no `project_paths`** resolves a default or errors — it must never fall back to all
projects. Measured: 164.78 s unscoped against 7.01 s scoped, and the unscoped answer came out of a
member's vendored JavaScript.

**`search` with an unknown `scope`** returns an error naming the valid set. `scope_languages` maps
an unrecognised value to "no restriction", so accepting it *widens* the corpus silently. The case
that found it was a project path passed as `scope`; the call was accepted and answered, which reads
as an engine routing defect rather than a caller mistake.

**`graph` with an unlisted `relation`** is rejected against `query.graph_handler._RELATIONS`, which
the CLI and MCP docstrings must match word for word.

**`overview` with an unknown `what`** is deliberately *not* rejected here. `what="projects"` and
`what="metrics"` are global and bypass project resolution entirely; an unknown `what` is a usage
error independent of any project, so it passes through to `handle_overview`, which owns the
valid-set message. Failing on project resolution first would name the wrong problem.

**`index(enabled=False)` on a federation root** returns a preview and does nothing. It fans out from
one path to N, and the asymmetry is what earns the round trip: a membership row deleted here is
rediscovered on the next federation walk, but the embeddings under it are not — undoing it costs GPU
time proportional to the members. It acts only on a second call with `confirm_members=True`; a
single project needs no confirmation.

`index` canonicalizes the path and **never** resolves to an enclosing root, matching CLI `init`.
Enclosing-root resolution would register a child under its parent.

## Shared behaviour

Every tool calls `note_activity()` first, which feeds `daemon.runtime_state` and holds off the idle
model unload. `search`, `graph` and `overview` dispatch their blocking work through
`asyncio.to_thread`; `index` does its registry work inline.

Scoped calls fan out through
[a federation is a query-time union](../constraints/a-federation-is-a-query-time-union.md); the work
itself lands in [query](../components/query.md), and the registry writes `index` makes land in
[core](../components/core.md).

## Guards

| Claim | Guard | File |
|---|---|---|
| unscoped search fails loud; index canonicalizes only | `test_t2_unscoped_search_fails_loud_instead_of_scanning_the_fleet`, `test_s3_index_tool_canonicalizes_symlink` | `test_path_resolution.py` |
| unlisted scope and relation are rejected | `test_sc5b_unlisted_scope_is_rejected`, `test_sc4b_unlisted_relation_is_rejected` | `test_surface_consistency.py` |
| removing a root previews, then acts on confirm | `test_ir2_the_preview_names_the_members_and_the_cost`, `test_ir3_confirming_removes_the_root_and_every_member` | `test_index_remove_preview.py` |
| a scope with no store errors | `test_su1_a_scope_with_no_store_is_an_error_not_an_empty_result` | `test_search_unindexed_scope.py` |
