---
type: Component
resource: src/rag_search/index/
title: "index: discovery, chunking, and the vector store"
description: The write path — walk, chunk, embed, store — plus the two things that decide whether stored vectors are still valid and the process boundary every parse crosses.
tags: [index, store, chunker, discover, embed-signature, bounded-parse]
status: active
generated:
  by: claude/claude-opus-5
  at: 2026-08-18T01:45:55Z
---

# index: discovery, chunking, and the vector store

Six files, ~1,900 lines. `discover.py` decides which files exist, `chunker.py` cuts them,
`indexer.py` drives, `store.py` holds the sqlite vector and lexical tables, `validate.py` checks
them, and `bounded_parse.py` is where tree-sitter runs.

Which files exist at all is one predicate, not one per caller:
[one predicate decides what is indexed](../constraints/one-predicate-decides-what-is-indexed.md).
Paths and per-project settings come from [core](core.md); vectors come from
[embed](embed.md), which this package calls and the parse workers must not.

## `embed_signature` is what makes a stored vector comparable

`store.py` folds `EMBED_MODEL`, `EMBED_MAX_TOKENS`, the dimension and `CHUNKER_REV` into one string
and stamps it into the store's `meta` table. A read compares and reports drift; it does not repair.

The consequence worth stating: **bumping `CHUNKER_REV` invalidates every stored vector in the
fleet.** It is a chunker revision, so it sits in the embedding identity even though nothing about
the model changed — different cuts are different text. Re-embedding is not automatic;
`daemon/sweeps.py` gates it on `AUTO_MIGRATE_VECTORS` and otherwise logs the mismatch and moves on,
so a fleet can sit stale behind a healthy daemon.

## Parses cross a process boundary, always

`bounded_parse.py` runs a persistent pool on the **spawn** context. Never `fork` — the daemon holds
a CUDA context and many threads. Workers are CPU-only.

Tree-sitter `Node` objects are not picklable, so the tree must be built *and* consumed inside the
worker; a worker that returns a tree returns nothing usable. The guard bans the direct-call pattern
rather than auditing known callers, because the risk is the next caller. Full reasoning:
[every parse is bounded out of process](../constraints/every-parse-is-bounded-out-of-process.md).

## `validate.py` is pure SQL by contract

No inference, no GPU, no model load. It is reachable from `overview(what="validate")`, which means
it runs on request against a live daemon; anything expensive added here is paid on the event loop's
thread pool by whoever asked.

## Guards

| Claim | Guard | File |
|---|---|---|
| a real chunker/pooling change moves the signature | `test_es4_a_real_pooling_change_moves_the_signature` | `test_embed_signature.py` |
| an old stamp reads stale rather than passing | `test_es6_legacy_stamped_store_reads_stale` | `test_embed_signature.py` |
| no parse outside a worker module | `test_no_direct_parse_outside_worker_modules` | `test_no_unbounded_parse.py` |
