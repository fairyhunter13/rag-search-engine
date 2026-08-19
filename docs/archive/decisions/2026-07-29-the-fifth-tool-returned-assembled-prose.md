# The fifth MCP tool returned assembled prose

**2026-07-29** · `7adb27c` · guard: the 4-tool assertions in `test_mcp_protocol_http.py` and
`test_mcp_tool_matrix.py`

`ask` was the fifth MCP tool. It returned chunk bodies and community summaries concatenated and
hard-truncated at 3000 chars each.

That is the shape an MCP tool should not return. The client is an agent that can call again, so it
wants compact references it chooses to expand — not context someone else assembled and cut off
mid-chunk. `ask` was also the most expensive tool in the server: a GPU embed, the full federation
fan-out, and *two* cross-encoder passes, for a worse shape than `search` returned more cheaply.
Half its code duplicated `search` outright.

The one axis it reached that nothing else did was architecture-level: "what is this project shaped
like" rather than "where is X". That moved onto `overview(what="communities", query=...)`.

`query/ask.py` survives, and deliberately. It is the context builder for the two callers that
genuinely cannot loop — the CLI and the dashboard's chat. Both want assembled context because
neither can call back for more.

## The mirror that nobody read

A static `_MCP_TOOLS` list stood in `server/mcp.py`, carrying a comment telling the next person to
update it when adding or removing an `@mcp.tool()` handler. Nothing read it. `mcp.list_tools()` is
the registry.

**The transferable lesson:** a mirror nobody consults is a second source of truth that can only
ever be wrong. It cost nothing while it was correct and would have cost a debugging session the
first time it was not.
