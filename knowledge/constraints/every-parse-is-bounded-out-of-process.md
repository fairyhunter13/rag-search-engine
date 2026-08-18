---
type: Constraint
resource: src/rag_search/index/bounded_parse.py
title: Every parse is bounded out of process
description: HR39 — py-tree-sitter has no working in-process cancellation, so every parse runs inside a spawn-context worker that can be killed, and the guard bans the direct call pattern rather than auditing known callers.
tags: [tree-sitter, timeout, subprocess, hr39]
status: active
generated:
  by: claude/claude-opus-5
  at: 2026-08-18T01:45:55Z
---

# Every parse is bounded out of process

A pathological grammar fed the wrong bytes — the measured case is `cobol` given non-cobol input —
pins a CPU core forever. There is no way to stop it from inside the process, and that was
established by testing rather than assumed:

- py-tree-sitter 0.25's `progress_callback` **never fires during a stuck parse**. It is ignored for
  bytestring input, and it is not invoked even with a read-callback source. Both variants hung to a
  12 s OS kill.
- `tree_sitter_language_pack.get_parser` returns a bundled parser with **no callback mechanism at
  all**.

So cancellation has to be a process boundary.

## The pool

`bounded_parse.py` holds a persistent worker pool on the **spawn** context — never `fork`, because
the daemon holds a CUDA context and many threads, and forking that is undefined at best.

`run_bounded(func_ref, args, deadline_s)` runs the extraction function **entirely inside the
worker**. That is not an implementation detail: a tree-sitter `Node` is not picklable, so the tree
must be built and consumed on the far side and only plain results cross back.

Workers never import the embedder, so the GPU-only doctrine is untouched, and the overhead is
indexing-time only — query and idle paths never parse.

## On timeout

The worker is `terminate()`d and respawned, `parse_timeout_count` increments and is exposed via
`overview(what="metrics")`, a **path hash** is logged rather than the real path (the tracked tree
stays device-neutral — see
[the tracked tree is publishable](the-tracked-tree-is-publishable.md)), and extraction continues.
Timed-out files are *recorded*, never silently skipped: a file that failed to parse and a file with
no symbols look identical in the graph otherwise.

## Why the guard bans a pattern

`test_no_unbounded_parse.py` bans a direct `get_parser(...).parse(` anywhere outside
`bounded_parse.py`. Auditing the call sites that exist today would leave the invariant open for the
next one written, and the failure mode — a hung core with no error — is exactly the kind nobody
notices in review. Same shape as the tree-wide scan in
[no generative model runs inside the package](no-generative-model-runs-inside-the-package.md).

`RSE_BOUNDED_PARSE_WORKERS` defaults to 1, and the reason is
[the daemon lives inside one core](the-daemon-lives-inside-one-core.md): under a one-core quota two
workers only time-slice the same core, with extra context-switch and RSS cost and no throughput.

## Sources

Row HR39 in [§13b](../../docs/architecture/federation-ops-and-invariants.md).

| Claim | Guard | File |
|---|---|---|
| pool kills and respawns only the stuck slot | `test_pool_timeout_kills_and_respawns_only_that_slot`, `test_pool_healthy_after_timeout`, `test_sigkill_mid_task_recovers` | `test_bounded_parse.py` |
| no direct parse outside this module | `test_no_direct_parse_outside_worker_modules`, `test_worker_functions_only_invoked_via_run_bounded`, `test_worker_modules_are_exhaustive` | `test_no_unbounded_parse.py` |
| workers never import the embedder | `test_bounded_parse_workers_never_import_embedder` | `test_no_unbounded_parse.py` |
