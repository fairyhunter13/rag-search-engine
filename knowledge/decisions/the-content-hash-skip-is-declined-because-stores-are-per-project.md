---
type: Decision
resource: src/coderag/store.py, src/coderag/config.py, src/coderag/discover.py
title: The content-hash skip is declined, because a store cannot see another project's duplicate
description: "A remediation plan asked to wire `files(sha256)` into a skip so near-identical deployed refs cost almost nothing. Each project owns its own sqlite file, so the two copies live in two stores and neither index can see the other. The saving needs a shared embedding cache the engine does not have. The dead index line is deleted, and the fleet prize is measured at 1147 MiB."
tags: [storage, indexing, dedup, declined]
status: stable
generated: { by: claude/opus-5, at: 2026-08-25T00:00:00Z }
---

# The decision

The content-hash skip is not built. `store.py` carried `CREATE INDEX files_sha ON files(sha256)`
and nothing read it, so the plan read the line as a half-built feature. It is dead weight, and the
line is deleted.

# Why the index cannot pay

`config.index_path` keys a store by a hash of the resolved project path, at
`~/.local/share/coderag/indexes/<name>-<hex>/index.db` (`src/coderag/config.py:58`). One sqlite
file per project, and the module docstring at `src/coderag/store.py:1` says the same.

Two near-identical deployed refs are two registered projects, so they are two stores. An index on
`files(sha256)` lives inside one file and can be read by one connection. It can therefore only find
a duplicate the same project already holds, which is a file appearing twice at two paths in one
tree. That is not the case the plan wanted to make free.

The saving the plan claimed needs a store-external cache keyed by content hash. No such thing
exists here. `store.connect` (`src/coderag/store.py:78`) opens exactly one path, `_local.conns` is
keyed by that path, and no function in the module reads a second store.

# What the existing skip already does

`discover.plan` (`src/coderag/discover.py:190`) compares `known.get(rel) != meta.sha256`. `known`
comes from `store.file_digests` (`src/coderag/store.py:169`), which is `path -> sha256` for every
row. The comparison is path-keyed and it scans the whole table, so it uses no index.

That already avoids re-embedding a file whose content did not change between runs of one project.
It is content-aware within a project and it needs no `files_sha`.

# What would have to be true to revisit this

A content-addressed embedding cache outside every store, keyed by chunk sha256, plus an
invalidation story. Two signatures already force work today and both would have to key that cache.
`store.incompatible` (`src/coderag/store.py:144`) compares seven stamped values, including
`embed_model`, `embed_dims` and the four chunker settings, and returns the reason so a rebuild can
name it. `ProjectConfig.signature` (`src/coderag/projcfg.py:60`) hashes what changes which files
get walked.

A cache keyed by sha256 alone would serve a vector from a different model into a different space,
which is the exact failure `incompatible` exists to refuse. So the cache key is the chunk hash plus
the model stamp plus the chunker stamp, and a model swap orphans the whole cache. Revisit when
someone is willing to own that eviction.

# The size of the prize, measured 2026-08-25

`~/.local/share/coderag/indexes/` holds 4.3 GiB over 343 directories. The registry lists 421
projects, 229 of them under a `_worktrees` path, and 143 of those have a store. Those 143 worktree
stores are 1147 MiB, so a perfect cross-ref dedup could not reach the other 3.2 GiB at all.

Two sibling ref pairs of the same service, compared by file and chunk hash:

```
domain-transfer  3c1f29dda25d vs d378b889229a  files 132/132 shared 125 (95%)  chunks 317/317 shared 294 (93%)
domain-inspection 3f31fc4a3753 vs 73a192d67376 files 264/265 shared 243 (92%)  chunks 564/565 shared 489 (87%)
```

So the overlap is real and high. The reachable prize is a fraction of 1147 MiB, against building
and invalidating a fleet-wide cache. That is the trade a future reader has to beat.

# On deleting the line

Nothing queries `files_sha` and nothing asserts the schema text. The engine has no migration step
and no schema version: `store.connect` runs `executescript(SCHEMA)` with `IF NOT EXISTS` on every
open, so the schema is applied idempotently rather than versioned. Deleting the line therefore
changes new stores only. Every store already on disk keeps the index until it is rebuilt, where it
stays unread and costs one B-tree per write to `files`.
