# The vendor prize buys edges nobody reads

**2026-08-04.** The one item the declined residual pass left queued — indexing third-party `vendor/`
trees into `graph.db` "for resolution only" — is **declined without running it**. This records why,
because the number attached to it is real and will look like an opportunity again.

## What was left on the table

The residual pilot (`2026-08-04-the-residual-has-no-types-to-resolve.md`) failed on accuracy: 0 of 718
untyped receivers narrowed to exactly one. But the same run turned up a second, unrelated figure —
scip-php named **2,453** call sites whose callee our index holds **no symbol for at all**: `is_null`,
`ucfirst`, `app`, framework internals. Those are not ambiguous, they are absent, and they sit in the
45.5% "no candidate in index" bucket rather than the 25.2% "dropped as ambiguous" one.

That was recorded as a *coverage* prize reachable far more cheaply than by adopting SCIP — just index
`vendor/` into `graph.db`. It was never gated, so it survived as an open item with a big number on it.

## Why it is declined

**It requires reversing a standing decision, not adding a feature.** `vendor` is already in
`IGNORED_DIRS` (`core/config.py`), listed beside `node_modules`, `.venv`, `target` and `dist`. The
repo has already decided that dependency trees are not source. Carving a per-language exception into
a fleet-wide ignore list is the opposite of a cheap change.

**Nothing that consumes edges would be improved.** After the PageRank revert, `edges` feed exactly
three readers, and retrieval is not among them:

| reader | what vendor edges would add |
|---|---|
| `graph` MCP tool — `callers`/`callees`/`path` (`server/mcp.py`) | `callees` of a method would name `Illuminate\…::where`. A developer already knows that call is framework. |
| `impact` (`query/graph_handler.py`) | BFS over **callers**. Vendor code does not call your code, so the impact set barely moves. |
| community detection (`graph/community.py`) | see below — this is where it goes actively wrong |

Ranking reads none of them. The prize buys graph completeness whose retrieval value this repo has
already measured at zero, twice: PageRank was built, measured and reverted, and re-closed on
2026-08-04 when R1's edges landed without moving it.

**And it would degrade a shipped feature.** `community_fastgreedy` partitions the *whole* edged
subgraph, and `overview(what="communities")` serves that partition as the architecture axis — what a
project is shaped like. The pilot root alone unpacked **12,436 vendor PHP files** against 402 of its
own. Folding that in on 55 Laravel roots lets framework internals dominate every community map in the
fleet: the answer to "what is this project shaped like" becomes "it is shaped like Laravel." That is a
regression, and it is paid for with roughly **9 GB** of `vendor/` across the fleet plus a
`--ignore-platform-reqs` install per root.

## The rule

A number is not a prize until something reads it. 2,453 absent callees is a true measurement of a real
gap, and the gap is in a graph whose only ranking consumer was measured and deleted. **Before
recovering missing information, confirm something consumes it** — the sibling of the rule the residual
pilot earned, which was to confirm the information exists at all.

Re-open only if a reader appears that (a) is measured to improve retrieval and (b) needs third-party
symbols specifically. Not on the 2,453.
