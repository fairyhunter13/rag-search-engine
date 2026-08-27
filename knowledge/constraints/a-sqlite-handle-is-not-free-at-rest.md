---
type: Constraint
resource: src/coderag/conns.py, src/coderag/store.py, src/coderag/search.py
title: A cached SQLite handle is not free at rest, and the cache was per thread and per project
description: Every connection carries its own page cache, 2.12 MB once filled at SQLite's default. The daemon held 1,506 handles over 415 stores on four threads and never closed one, which is 3.2 GB of the 3.5 GiB it was resident at. The cap, the reap and the deleted second fan-out are the three parts of the fix.
tags: [memory, sqlite, federation, daemon]
status: stable
generated: { by: claude/opus-5, at: 2026-08-27T00:00:00Z }
---

# The measurement

`coderag.service` sat at 3.56 GiB RSS against `MemoryHigh=4G`, with 1.39 GiB swapped, where its own
unit comment records the design point as 2.14-2.29 GiB with both models resident. The GPU side was
not the cause: `release_models` ran on schedule, 16 idle unloads in 30 h, and `malloc_trim` with it.
The memory was live.

The live process held 2,699 fds over 415 stores, 1,506 of them `index.db`: four threads times 361
stores. No `cache_size` pragma was set anywhere, so each handle carried the 2 MiB default.

| per connection | idle after one small query | after its page cache fills |
|---|---|---|
| default (`cache_size=-2000`) | 0.27 MB | **2.12 MB** |

1,506 x 2.12 MB is 3.2 GB, against 3.4 GB of anonymous RSS spread over 98 glibc arenas.

# Why one search opened the whole fleet twice

A federated search opens every member of the unit, about 358 stores, on whichever anyio worker runs
it. It did so twice: once in `_project_candidates`, and again at the end of `search` purely to fill
the `searched: {files, chunks}` display field. The second walk cost 8.3 ms and 716 statements, and
it is what made the whole handle set resident for a number nobody acts on. The registry row already
carries `file_count` and `chunk_count`, written by the same pass, so the field is read from there
and is now as of each project's last pass.

anyio's default limiter is 40, so that handle set could be multiplied by 40 threads.

# What the three settings buy

- `SQLITE_CACHE_KIB=256`: 2.12 MB to 0.27 MB a handle. A 40-store query pass goes 5.3 ms to 6.9 ms.
- `STORE_IDLE_S=600` with `store.reap_idle` on the scheduler tick: idle RAM falls to near zero.
  Reopening 358 stores is 79 ms at 0.22 ms each, paid by the first search after a quiet spell.
- `THREAD_LIMIT=8`: the worst case is 8 handle sets, not 40.

# The two things a reap has to get right

The cache is keyed by path, so an **unlinked** store keeps its handle and its disk blocks. 15 fds
pinned 4 deleted test-fixture stores. `reap_idle` closes those whatever the idle stamp says.

And a pass can outlive the threshold. `reap_idle` skips a cache it cannot lock without blocking, and
`store.session()` holds that lock for the whole of `index_project` and the retrieval loop of
`search`, so nothing is closed under a live cursor.
