---
type: Decision
resource: src/coderag/searchledger.py, src/coderag/search.py, src/coderag/tools.py, src/coderag/config.py, src/coderag/cli.py, tests/test_searchledger.py
title: A search writes one structured row, and the fleet log level stays where it is
description: "The pool cut starved 307 projects and finding it needed an offline replay that rebuilt the pool by hand, because `search.py` and `rank.py` hold zero log calls and the reply carries only `took_ms` and a project count. A louder journal was already refused on a measurement, so the record is a JSONL row per search rather than prose at INFO."
tags: [observability, search, ledger, logging]
status: stable
generated: { by: claude/opus-5, at: 2026-08-26T00:00:00Z }
---

# The refusal this decision is built around

[A failure that resolved itself left no
trace](../defects/a-failure-that-resolved-itself-left-no-trace.md) closes by refusing a louder
journal, and it refuses it on a number: 8,140 lines in 24 h at INFO is already the volume, and the
defect was that errors were ephemeral and unstructured, not that they went unlogged. Re-measured
for this change: **5,716 lines in 24 h, of which 3,778 are the watcher `detected` lines**. The
refusal holds. So nothing here adds prose at the default level.

# What a search recorded, which was two numbers at the ends

`search` returned `took_ms` and `searched: {projects, files, chunks}`. Between them the pool is
built, filtered, cut, reranked and diversified, and no stage size was written anywhere.
`src/coderag/search.py` and `src/coderag/rank.py` hold **zero** log calls between them, and those
are the two files [the pool cut
defect](../defects/the-pool-cut-starved-the-members-it-was-built-to-reach.md) lived in. The whole
engine holds 14 log call sites and had **no `log.debug` at all**, with the level hardcoded at
`server.py`, so a debug level could not be asked for and would have printed nothing.

The error path recorded least of all. `tools.search_code` returned `{"error": ...}` and wrote no
line and no row, so the one call a reader most wants to find was the one leaving nothing behind.

# The shape, and it is not a new mechanism

`pinledger.py` already solves this shape in 53 lines, for the same reason stated in its own
docstring: a population that lived only in journald is capped at seven days, holds no client name,
and is countable only by grep. `searchledger.py` follows it exactly — one JSONL row under
`STATE_DIR`, one generation rotated by rename at a size cap, every write best-effort under
`contextlib.suppress(OSError)`, and `path()` resolved per call so the test fixture can move the
directory.

A row carries the trace id, the caller and root, the mode, and every stage boundary with its own
elapsed milliseconds: `unit`, `pool` and `pool_projects`, `filtered`, `cut` and `cut_projects`,
`returned` and `result_projects`, against `embed_ms`, `retrieve_ms` and `rerank_ms`. The pair that
names a starved round robin is `pool` against `cut`, and one without the other is what forced the
replay.

**The reply never carries the stages.** They go into the row, and `search_code` pops them off what
it returns. A model reading a pool size spends context on a number it cannot act on.

**An error writes the same row** with the error string, and the trace id goes into the returned
message so a caller can quote it back. `read` walks both generations, because a rotation the
moment before a question is asked would otherwise answer it with an empty file.

# What is askable, rather than always on

`CODERAG_LOG_LEVEL` defaults to `INFO` and the fleet default does not move. It exists so the new
`log.debug` at the stage boundaries is reachable for one run. `coderag trace` reads the ledger
back, tails it and filters to errors. A ledger nobody can read is a file, not an instrument.

# What this would have shown on the defect that scopes it

One row: `unit 358`, `pool 14720 from 338 projects`, `cut 60 from 31 projects`. Two numbers side
by side, and 307 projects between them.
