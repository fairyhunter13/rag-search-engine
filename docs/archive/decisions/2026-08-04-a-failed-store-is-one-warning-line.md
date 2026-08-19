# A failed store is one warning line

**2026-08-04.** Shipped as `a0d8005`, during the embedding track's fleet re-embed
(`nomic-embed-text-v1.5` + task prefixes @512).

## What happened

The re-embed swept 208 projects and reported completion in 9635 s. The stale count did not reach
zero. One store had failed at 22:22:19 with:

```
reconcile <store>: UNIQUE constraint failed on vec_chunks primary key
```

and reconcile had logged that and walked on. Every subsequent pass failed identically. The store sat
at the old embedder behind a `/healthz` that was, correctly, green.

The store held **33 `vec_chunks` rows against 28 `chunks` rows**. The five orphans were unreachable
by `delete_by_path` (which enumerates from `chunks`) and by `insert`'s probe (same table), so no
write path could ever remove them and every re-embed of that store aborted on the same constraint —
permanently.

## The defect

`insert` gated the FTS delete *and* both vec0 deletes on one `chunks` probe. That made `chunks` the
authority on what `vec_chunks` contains, and once the two diverged there was no path back.

They do not need the same gate. An external-content FTS5 delete needs **the text that was indexed**,
which only `chunks` holds — that one genuinely requires the probe. vec0 needs **the key alone**.
Ungating the vec0 deletes makes them a no-op on the PK miss that is the normal case, and self-healing
on the divergence. `vec0` has no upsert and does not implement `ON CONFLICT`; deleting by hand first
is the only shape available.

BQ7 in `test_binary_quantization.py` pins it: delete a `chunks` row out
from under a live vector, re-insert the same `chunk_id`, assert one vector and a searchable chunk.
Verified as a gate rather than decoration — stashing the `store.py` change alone turns BQ7 red.

## The lesson that is not in the code

The bug was cheap. Finding it was not, and the reason is worth stating separately:

```python
except Exception as exc:
    log.warning("reconcile %s: %s", ...)
```

**Reconcile swallows per-project exceptions.** A store that can never converge is one `warning` line
in the journal, and every summary above it — completion count, elapsed time, `/healthz` — reports
success. The fleet-wide stale count was the only instrument that saw it, which is exactly why
CLAUDE.md says to verify a stamp move *by watching the stale count fall*, never by the absence of
errors. This is the incident that makes that rule concrete: there were no errors to be absent.

Nothing here argues for making reconcile fail loudly — one bad store must not stop 207 good ones.
It argues that **the completion line is not a result**, and a sweep is verified by counting what
converged.
