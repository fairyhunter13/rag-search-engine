---
type: Runbook
resource: src/rag_search/daemon/sweeps.py
title: Moving the pipeline stamp
description: Bumping `EXTRACTOR_REV` or `ALGO_VERSION` re-derives every graph in the fleet — take the lease before the first edit, release then restart, and verify by watching the stale count fall rather than by the absence of errors.
tags: [migration, extractor-rev, algo-version, reconcile, operations]
status: active
generated:
  by: claude/claude-opus-5
  at: 2026-08-18T01:45:55Z
---

# Moving the pipeline stamp

`_pipeline_algo_version()` folds `graph.community.ALGO_VERSION`, `graph.extractor.EXTRACTOR_REV` and
the source of the fingerprinted modules. Changing any of them makes every store in the fleet stale
and schedules a re-derive of each. That is the intended mechanism — see
[structure is read, never classified](../constraints/structure-is-read-never-classified.md) — and it
has three traps, each of which has cost a session.

## 1. Take the lease before the first edit

Not before the re-derive — **before the first edit.**

`_code_fingerprint` re-reads the fingerprinted modules from disk on every call, so a running daemon
restamps the moment `graph/extractor.py` is *saved*. No commit needed, and a docstring counts.

Measured 2026-07-31: one store re-derived using the daemon's old in-memory code while carrying the
new fingerprint. It then looked current and was not, which is worse than looking stale.

```bash
curl -s -X POST localhost:8765/api/sweeps/pause
```

Bump `EXTRACTOR_REV` in the **same commit** as the extraction change. `sweeps.py` is deliberately
not fingerprinted, so a resolver living there moves no stamp on its own.

## 2. Release, then restart — in that order, and both

A release is a permission, not a schedule.

The reconcile pass is startup-once and then parks; periodic resync is off by default, and steady
state is watcher-driven. **Moving a stamp touches no file, so it fires no watcher event.** If that
one startup pass already ran inside your lease window, it logged
`reconcile: abandoned before start`, parked, and the fleet stays stale behind a perfectly healthy
`/healthz`.

```bash
curl -s -X POST localhost:8765/api/sweeps/resume
systemctl --user restart rag-search-mcp-daemon
```

Reasoning:
[releasing a lease schedules nothing](../../docs/decisions/2026-07-31-releasing-a-lease-schedules-nothing.md).

## 3. Verify by the number, not by the silence

The stale count is `overview(what="metrics")` → `pipeline_version` → `stale_stores`. It counts every
store — unstamped ones included — whose `graph.db` `meta.algo_version` differs from
`_pipeline_algo_version()`.

Two properties worth knowing before you read it:

- It is **fleet-wide**. `project_path` does not change it.
- It answers even on an unscoped call, where `extraction` refuses for want of a project.

Built by `_fleet_pipeline_block` / `_pipeline_block` in `server/_overview.py`, guarded by AU5
(arithmetic) and AU6 (the surface can still reach it) in `test_extraction_ladder.py`.

Watch it fall. **The absence of errors is not convergence** — one session lost the number and had to
report the migration as unverifiable. The instruction to watch a count is useless without its
source, which is why the source is written down here.

## Related

`CHUNKER_REV` is the other stamp and it works differently: it folds into `embed_signature` alongside
the model name and dimension, so bumping it invalidates every stored **vector**, and re-embedding is
gated on `AUTO_MIGRATE_VECTORS` rather than on reconcile.

Record: [the EXTRACTOR_REV log](../../docs/decisions/2026-08-14-the-extractor-rev-log.md).
