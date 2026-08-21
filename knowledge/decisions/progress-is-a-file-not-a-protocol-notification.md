---
type: Decision
resource: src/coderag/progress.py
title: Progress is a file, not a protocol notification
description: MCP has two mechanisms for long-running work and both are the wrong shape for an indexer whose work outlives the request that started it, so the counter is a throttled JSON file that any reader can poll without importing the GPU stack.
tags: [mcp, indexing, observability]
status: stable
generated: { by: claude/opus-5, at: 2026-08-19T16:10:00Z }
sources:
  - id: mcp-progress
    resource: https://modelcontextprotocol.io/specification/2026-07-28/basic/patterns/progress
    title: MCP 2026-07-28 — progress notifications
    author: team:model-context-protocol
---

# The gap

`index.State` reported `state`, `queue_depth`, `current`, `completed`, `failed`. That is job
granularity: on a 7,000-file repo it names one path and then says nothing for ~28 minutes. The
`index` tool's contract — *"call it again to read status"* — was already right; there was simply
nothing underneath it that moved.

# Two protocol mechanisms, both refused

**`notifications/progress`** requires a `progressToken` supplied on the originating request, and the
[spec](https://modelcontextprotocol.io/specification/2026-07-28/basic/patterns/progress) is explicit
that notifications **MUST** only reference tokens "associated with an in-progress
operation".[^mcp-progress] The
`index` tool returns immediately and the work outlives the request by design — there is no live
request to hang a token on. Adopting it would mean making `index` block, which reverses the decision
in `tools.py` that there is no `wait` parameter.

**The Tasks extension** (`io.modelcontextprotocol/tasks`, official in `2026-07-28`) is poll-based via
`tasks/get`, having dropped the blocking `tasks/result`. That is the right shape — and it is the
shape `index` already has. Adopting it would add an optional extension and a second status surface
to express a semantic this tool exposes today. Revisit only if a client wants to poll indexing
*without* knowing it is coderag.

# What it is instead

A `Progress` counter written where the hours actually go — inside `_write_files`, the only loop that
runs for hours — and merged into the existing `index.status()` under a `progress` key. No new tool,
no new endpoint: `/healthz` and the `index` tool both already return `status()`.

It also lands in `STATE_DIR/progress.json`, because **the readers are out of process**. Each bake-off
arm is its own subprocess with its own `STATE_DIR`, and `import coderag.index` pulls in onnxruntime —
so a reader routed through the indexer would pay a GPU-stack import to print a percentage. `jq` on
that file is the whole CLI, which is why no `coderag progress` subcommand was added.

# Three things that are not obvious

**Terminal writes bypass the throttle.** Writes are rate-limited to `PROGRESS_WRITE_S` (2 s) so the
path that already commits every 64 files does not gain an fsync per file. `begin`, `phase`, `cooled`
and `finish` force through anyway: the last write is the one a reader needs most, and throttling it
away is exactly how `eval.py` lost five arms to a 0-byte file.

**The ETA is off the wall clock.** Projecting from working time and re-inflating by the cooling duty
cycle is *the same number* — the `working` term cancels: `(remaining/(done/working)) × (elapsed/working)`
reduces to `remaining × elapsed / done`. The first version did the long form and a comment claimed it
corrected for something. `files_per_s` is still reported off working time, because throughput-while-
working and time-to-finish are different questions.

**`phase` is only meaningful next to `updated_at`.** A process killed mid-index leaves its last line
saying `indexing` forever. Liveness is the reader's call, from `updated_at` and `pid`; the file does
not pretend to know it is dead.

[^mcp-progress]: §on progress — a notification may reference only a token from an in-progress
operation. Cited rather than linked alone so the nightly drift run reports the next revision of a
spec that has already changed a dated version in place once.
