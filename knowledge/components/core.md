---
type: Component
resource: src/rag_search/core/
title: "core: where state lives and which projects exist"
description: Env knobs, on-disk paths, the projects.json registry, per-project .rse-index.yaml and GPU device selection — the package every other package imports, holding the one lock and the one set difference that can destroy a fleet.
tags: [core, registry, config, gpu, orphans, flock]
status: active
generated:
  by: claude/claude-opus-5
  at: 2026-08-18T01:45:55Z
---

# core: where state lives and which projects exist

Seven files, ~1,100 lines, and all six other packages depend on it. Nothing here is expensive; two
things here are dangerous.

## `_mutate()` is not reentrant

`registry.py` guards every write to `projects.json` with an `fcntl.flock` on a sibling lock file.
Three writers share that file in production — reconcile, the server, the CLI — and a dropped row is
a project that stops being watched and rots with nothing left to report it.

The lock is per *open file description*, so a nested `_mutate()` on the same thread blocks on
itself. Nothing nests today; a call that does will deadlock rather than serialise. The drop
predicate is set difference, not `len()` — a re-key removes one path and adds another, which
counting rows calls a no-op, skipping the backup rotation that could undo it.

## The orphan sweep checks its own answer, not its arithmetic

`orphans.py` computes "index dirs minus registry paths" and deletes the remainder. That subtraction
has wiped the fleet's indexes **twice**, and on both occasions the arithmetic was correct — once the
comparison shape was wrong (a registry *path* tested against a directory *name*, so all 179 dirs
read as orphans), once the registry itself was wrong (a test helper removed 198 rows in-process).

So the guard is on the shape of the answer: a sweep concluding it should delete nearly everything it
can see is reporting a broken premise. Any change to how registry paths compare against index
directory names is a fleet-deletion risk, and it will look correct while it runs.

## GPU selection has no CPU entry

`gpu.py` ranks execution providers best-first and CPU is never a valid target — the ladder omits it
rather than ordering it last. See
[inference runs on the GPU or it fails](../constraints/inference-runs-on-the-gpu-or-it-fails.md).

`index_config.py` reads per-project `.rse-index.yaml`; the predicate it feeds is
[one predicate decides what is indexed](../constraints/one-predicate-decides-what-is-indexed.md).
Path-to-index-dir keying is [a federation is a query-time union](../constraints/a-federation-is-a-query-time-union.md).

## Guards

| Claim | Guard | File |
|---|---|---|
| concurrent writers lose no rows | `test_rg1_concurrent_upserts_lose_no_rows` | `test_registry_concurrency.py` |
| an empty registry authorises nothing; taking most of the tree needs force | `test_co7_an_empty_registry_beside_a_full_index_tree_is_refused`, `test_co8_taking_most_of_the_tree_is_refused_until_forced` | `test_clean_orphans.py` |
| CPU is fatal, not last | `test_select_gpu_providers_fatal_on_cpu_only`, `test_gpu_ep_names_excludes_cpu` | `test_gpu_autodetect.py` |
