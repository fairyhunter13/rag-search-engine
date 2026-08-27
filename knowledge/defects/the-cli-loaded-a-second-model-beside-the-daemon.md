---
type: Defect
resource: src/coderag/daemon.py, src/coderag/cli.py, src/coderag/config.py, tests/test_server_tools.py
title: The CLI ran the search in its own process, so it built a second CUDA session
description: "`coderag search` called `search.search` directly, which loaded both models into the CLI process beside the daemon's copy. The card is 16303 MiB, the daemon holds 7660 MiB and one CLI search adds 3114 MiB, so a third consumer exhausts it and `cublasCreate` fails. The CLI asks the daemon now, and falls back to a local search only where nothing answers."
tags: [cli, gpu, mcp, contention, resolved]
status: stable
generated: { by: claude/opus-5, at: 2026-08-28T00:00:00Z }
---

# The lock that cannot reach the other process

`embed.py:11` calls `_GPU_INFER_LOCK` the single GPU serializer, and `embed.py:34` makes it a
`threading.RLock`. It holds inside one process. The daemon and a CLI invocation are two processes,
so each took a full CUDA context with no coordination between them.

`embed.py:45-51` retries an out-of-memory forward pass at half the batch. It matches the literal
`Failed to allocate memory`. A failure at `cublasCreate` is handle setup rather than a forward
pass, so no smaller batch reaches it and the retry cannot fire.

# What it cost

A caller outside this repo drives `coderag search` twenty times in one measurement. A run of that
measurement exited 1 with `CUBLAS failure 3: the resource allocation failed`, while a test suite
was driving the daemon at the same time. Three consumers exhausted the card.

Nothing was wrong with the search. The card was full.

# The CLI speaks the protocol now

`daemon.py` is a client for the running daemon. `cli._search` asks it first, and runs the search
locally only where `Unreachable` says nothing answered. With no daemon there is nothing to share
the card with, so the second session the repair avoids does not exist in that case.

The `2026-07-28` era carries `roots/list` inside `InputRequiredResult`, so a stateless client
answers the pin in a second POST rather than over a back channel. The root the caller typed is the
root the client declares. `scope.require_pin` therefore needs no relaxation, which is the reason
this is a client and not a flag.

Measured after the change: one semantic search returns in 592 ms rather than about 7 s, and
`nvidia-smi` shows one compute process for the whole call.

# What the delegated answer is not

The tool is not the library. `tools.py:258` folds `trace` into the row and pops it, and
`tools.py:263` rewrites `hint`. Delegated output is therefore not byte-identical to the local
path. `tools.py:270-272` also returns `{"error": ...}` rather than raising, so `_search` re-raises
it as `search.SearchError`. Without that, a failed search would print an empty result set and exit
0, which is a false zero the caller cannot see.

`bridge.py` is a pipe and this is not in it. A pipe forwards frames it does not read, and this one
composes two calls and answers a request.
