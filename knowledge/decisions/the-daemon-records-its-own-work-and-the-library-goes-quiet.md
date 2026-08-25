---
type: Decision
resource: src/coderag/ledger.py, src/coderag/runledger.py, src/coderag/index.py, src/coderag/watch.py, src/coderag/server.py, src/coderag/cli.py, tests/test_runledger.py
title: The daemon records its own work, and the library that filled the journal goes quiet
description: "454 index passes ran in 24 h and the journal described none of them, while 3,800 of its 5,912 lines were `watchfiles` announcing a change count with no project name. One JSONL row per index pass, watch batch, sweep and re-arm replaces that, and `watchfiles` drops to WARNING, so the journal gets quieter rather than louder."
tags: [observability, indexer, watcher, scheduler, ledger, logging]
status: stable
generated: { by: claude/opus-5, at: 2026-08-26T00:00:00Z }
---

# The measurement, and it inverts

24 h of `journalctl --user -u coderag`, 5,912 lines:

| shape | lines | share | names a project |
|---|---|---|---|
| `watchfiles` saying `N change(s) detected` | 3,800 | 64% | no |
| the MCP conformance suite's expected `ValueError: Unknown prompt` | ~780 | 13% | no |
| `workspace pin: client=...` | 559 | 9% | no |
| `watching N projects` | 50 | 0.8% | no |
| describing an index pass | **0** | **0%** | -- |

The registry says **454 projects were indexed in those same 24 hours**. Not one produced a line.
Two thirds of the journal is a third-party library counting changes it does not attribute. The
daemon's own work is invisible.

This is [the dated refusal](../defects/a-failure-that-resolved-itself-left-no-trace.md) one level
deeper. It points the same way [the search ledger
does](a-search-writes-one-row-and-the-log-level-stays.md). Volume was never the missing thing.
Structure was, and there is less of it than the volume suggests.

# What each surface could not answer

**The indexer.** `index_project` computes `written`, `deleted`, `files` and `chunks` and returns
them. `_drain`, the worker that calls it, threw the whole return value away. A pass left
`indexed_at` and two counts in the registry, and the next pass overwrote them. Four things stayed
unrecorded: how long the pass took, which phase was slow, why it ran, and how long it waited in
the queue. A failure calls `registry.record_error`, and the next failure overwrites that too.
**0 of 538 rows carried one**, so every failure the fleet has had is gone.

**The watcher.** `_dispatch` dropped an event in three places, each a bare `continue`. So "I
edited a file and it is still not searchable" had five answers. The project was not armed, the
event was not owned, the path was filtered, gitignore caught it, or the job ran and failed. The
daemon told none of them apart.

**The scheduler.** `_sweep` logged only when it claimed a member, and nearly every sweep claims
nothing. `_tick_errors` and `watch._error` live in memory. So a restart erases the scheduler
failure that caused it.

# The decision

**One primitive.** `pinledger` and `searchledger` held the same append-rotate-read block twice.
`ledger.py` takes the path and the cap as arguments. Each ledger keeps its own `NAME` and its own
`path()`, which is what the test fixtures move.

**One file, four kinds.** `runs.jsonl` holds `index`, `watch`, `sweep`, `arm` and `sched` rows,
because the question that needs them is one question. A watch row says the event was dropped. An
index row says the pass ran and failed. A reader holding one of the two cannot tell which
happened.

**The library goes quiet.** `watchfiles` is held at `WARNING` unless `CODERAG_LOG_LEVEL=DEBUG`
asks for it. It is 64% of the journal and names no project, and the watch row names one. This is
what makes the change honour the refusal rather than argue with it. The journal gets quieter, not
louder.

**`coderag trace <kind>`** reads them back. It defaults to `search`, so the invocation that
existed before the other kinds did still means what it meant.

# The cost, stated plainly

A busy hour is hundreds of watch batches. So `runs.jsonl` gets 8 MB, twice the search ledger's
cap, and one rotated generation. An empty watch batch records nothing: a timeout yield is the
common case, and a row for each is the volume the refusal ruled out.

The rows are best-effort, like every ledger here. The daemon must not fail on its own bookkeeping.
