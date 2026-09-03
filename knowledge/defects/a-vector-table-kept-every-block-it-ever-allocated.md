---
type: Defect
resource: src/coderag/store.py, src/coderag/doctor.py, src/coderag/disk.py
title: A vector table kept every block it ever allocated, and the compactor could not see it
description: "sqlite-vec holds its vectors in blocks of 1024 slots and frees a block only when every slot in it is free. A watched project rewrites a few chunks anywhere, all day, so the dead slots land scattered and every block keeps a live row. Fleet-wide: 2.97 GB allocated against 1.23 GB live, 2.4x. The busiest store is 6.3x. A KNN reads every block whole, so that is what a search pays. VACUUM gave back 59 MiB of 689 MiB and moved no block."
tags: [vectors, storage, latency, compaction]
status: stable
generated: { by: claude/opus-5, at: 2026-09-03T12:00:00Z }
---

# What happened

A search from the largest federated root on this fleet aborted at the client. The client had
waited 329 s for a first byte against its own 300 s idle timeout. Nothing errored. The server was
still working.

One of the two causes is here. The other is
[the fan-out was sequential](../constraints/what-a-root-and-135-members-cost.md).

`sqlite-vec` stores its vectors in `chunks_vec_vector_chunks00`, in blocks of 1024 slots. A KNN
reads every block whole. A block is given back only when **every** slot in it is free.

A watched project does not delete in runs. It rewrites a few chunks anywhere, on every save, all
day. So the dead slots land scattered: every block keeps at least one live row, every block stays,
and every KNN keeps reading all of them. The table grows while the row count does not.

Measured across the fleet: **2.97 GB allocated against 1.23 GB live, 2.4x**. On the store the
watcher rewrites most: **6.3x**, and 40,745 live rows spread over 249 blocks of 1024.

# Why the compactor did not find it

`doctor --compact` was a full `VACUUM` for its whole life, and a `VACUUM` cannot reach these
slots. They sit **inside blob rows**, not in the page freelist. Measured on a copy of that store:

| | file | vector bytes | blocks | warm KNN |
|---|---|---|---|---|
| before | 1095 MiB | 747 MiB | 249 | 300 ms |
| after `VACUUM` | 1036 MiB | 747 MiB | 249 | 300 ms |
| after a repack | **406 MiB** | **120 MiB** | **40** | **69 ms** |

The 59 MiB the `VACUUM` returned is the freelist, and it is real. It is also not the number a
search pays. The block count is, and the `VACUUM` did not move it.

# The fix

`store.repack_vectors` reads the live rows, drops `chunks_vec`, rebuilds it from `_vec_ddl()` and
re-inserts. `store.vector_blocks` reports the count before and after. `doctor --compact` runs the
repack first and the `VACUUM` second — the repack frees the pages and the `VACUUM` returns them —
and prints the blocks beside the MiB, so a store that was already packed is visible as one.

Three properties, because the dropped rows are the only copy of the vectors:

- One transaction. A store is never left with no vector table.
- The read is counted against what the table claims, and a short read refuses the drop. A short
  read that reached the `DROP` would delete vectors only a full re-index could put back.
- It stays a command a human types. Nothing automatic calls it: the repack took 15.1 s on the
  largest store here, and it rewrites the file.

# The trap in testing this

The first fixture deleted a contiguous tail and the arm failed at 1 block, not 3. vec0 **does**
free a block when a delete empties it whole, so a tail delete leaves nothing to repack and the
test passes against code that does nothing. Scattered is the whole defect, and it has to be the
whole fixture: `tests/test_disk.py::_churned` deletes 3 of every 4 rows across 3 blocks.

The negative arm is `test_a_vacuum_leaves_every_vector_block_where_it_was`. It runs `disk.compact`
against the same fixture and asserts the block count does not move. That is what makes the new
code necessary rather than merely green.

# What the fleet-wide repack actually returned

The table above is from a copy of one store. The whole fleet was repacked on 2026-09-03, with the
daemon stopped so no `DROP TABLE` could race an index write. 423 stores:

| | before | after |
|---|---|---|
| indexes directory | 5.38 GB | 4.16 GB |
| summed store size | 4821 MiB | 3686 MiB |
| summed vector blocks | 940 | 674 |
| the largest store | 1094 MiB, 251 blocks | **408 MiB, 40 blocks** |

Most stores are one block and gave back nothing, which is the point of printing per store rather
than as a total. Nearly the whole 1.14 GB came from the handful the watcher rewrites.

Against the same root, `retrieve_ms` over its previous 454 searches has a p50 of **16.8 s**. The
three searches after the repack and the threaded fan-out read **3.30 s, 3.55 s and 2.98 s**. A
three-query MCP call over the same 361-project unit, 93,200 files and 298,031 chunks, returned in
**22.8 s** — one round trip, no abort, against a client timeout of 300 s.

Both changes landed together, so neither number separates them. The bar was the abort, and the
abort is gone.

# Why the weekly timer skips most of the fleet

The first pass took **8 minutes 58 seconds** and the daemon was down for all of it. Almost none of
that time bought anything. Of the 423 stores:

- **362 gave back 0 MiB.** They were already tight, and the `VACUUM` rewrote each one anyway.
- **339 were already at one block**, which is the floor and not a saving.
- **14 moved a block at all.**
- The **top 8** stores were **91%** of the 1135 MiB freed. The largest alone was 686 MiB and 211
  blocks.

So `_compact` now tests each store before it touches it, and a store that passes the test is
skipped whole. The test is `store.vector_waste`: the blocks the table holds, minus the blocks its
live rows need. A store with a surplus of 0 has nothing a repack can give back.

**The surplus is a count of blocks and not a ratio, and the first version of it was a ratio.** A
ratio cannot tell a bloated store from a small one. 39 live rows sit in one block, which is 26x
allocated over live, and a repack of that store returns nothing because one block is the floor.
Most stores here are small, so a ratio would have rewritten the whole fleet every week and called
it maintenance. `tests/test_disk.py::test_a_thin_store_is_not_read_as_a_bloated_one` is that
mistake, kept as an arm.

The second half of the test is `disk.freelist_bytes`. A store can be packed in vec0 terms and still
hold pages that no table uses, and `disk.reclaim` cannot reach them where the header says
`auto_vacuum=0` — which is every store written before 4.4. Over 8 MiB of freelist and the store is
walked anyway.

`coderag-compact.timer` fires it weekly, `OnCalendar=Sun 04:00` with `Persistent=true`. The service
stops the daemon in `ExecStartPre` and starts it again in **`ExecStopPost`**, not in a second
`ExecStart`: `ExecStopPost` runs whether the pass succeeded, failed or timed out, and a compaction
that dies with the daemon left down is worse than the bloat it was written to remove.

Each pass writes one `runledger` row — `walked`, `skipped`, `blocks_before`, `blocks_after`, `mib`.
The weekly interval is a guess until two of those rows exist. The difference between them is how
many blocks the fleet regrows in a week, and that is the number that sets the real interval.
